import importlib.metadata
import subprocess
import sys

required = {'requests', 'tqdm', 'moviepy'}
installed = {pkg.metadata['Name'] for pkg in importlib.metadata.distributions()}
missing = required - installed

if missing:
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--upgrade', 'pip'])
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', *missing])


import os
import json
import hashlib
import base64
import time
import shutil
import threading
import requests
from shutil import which
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

# Lock for thread-safe progress updates
_progress_lock = threading.Lock()

has_ffmpeg = False
moviepy_deprecated = False
has_youtube_dl = False
has_yt_dlp = False

if which('ffmpeg') is not None:
    has_ffmpeg = True

if which('youtube-dl') is not None:
    has_youtube_dl = True

if which('yt-dlp') is not None:
    has_yt_dlp = True

if not has_ffmpeg:
    try:
        from moviepy.editor import *  # before 2.0, deprecated
        moviepy_deprecated = True
    except ImportError:
        from moviepy import *  # after 2.0

url = os.getenv("SRC_URL") or input('enter [master|playlist].json url: ')
name = os.getenv("OUT_FILE") or input('enter output name: ')
max_workers = min(int(os.getenv("MAX_WORKERS", 5)), 15)
max_retries = int(os.getenv("MAX_RETRIES", 5))

if 'deps://install' == url:
    print('exiting after installing dependencies')
    sys.exit(0)

if 'master.json' in url:
    url = url[:url.find('?')] + '?query_string_ranges=1'
    url = url.replace('master.json', 'master.mpd')
    print(url)

    if has_youtube_dl:
        subprocess.run(['youtube-dl', url, '-o', name])
        sys.exit(0)

    if has_yt_dlp:
        subprocess.run(['yt-dlp', url, '-o', name])
        sys.exit(0)

    print('you should have youtube-dl or yt-dlp in your PATH to download master.json like links')
    sys.exit(1)


def format_size(nbytes):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if nbytes < 1024:
            return f'{nbytes:.1f}{unit}'
        nbytes /= 1024
    return f'{nbytes:.1f}PB'


def print_header(text):
    width = 60
    print()
    print(f'{"=" * width}')
    print(f'  {text}')
    print(f'{"=" * width}')


def print_phase(phase_num, total_phases, label):
    print(f'\n[{phase_num}/{total_phases}] {label}')
    print(f'{"-" * 50}')


def get_temp_dir(source_url):
    url_hash = hashlib.sha256(source_url.encode()).hexdigest()[:16]
    temp_dir = os.path.join(os.getcwd(), f'.vimeo-dl-{url_hash}')
    os.makedirs(temp_dir, exist_ok=True)
    return temp_dir


def load_progress(temp_dir):
    manifest_path = os.path.join(temp_dir, 'progress.json')
    if os.path.exists(manifest_path):
        with open(manifest_path, 'r') as f:
            return json.load(f)
    return {'completed_segments': {}}


def save_progress(temp_dir, progress):
    manifest_path = os.path.join(temp_dir, 'progress.json')
    with _progress_lock:
        tmp_path = manifest_path + '.tmp'
        with open(tmp_path, 'w') as f:
            json.dump({'completed_segments': dict(progress['completed_segments'])}, f)
        os.replace(tmp_path, manifest_path)


def is_segment_complete(segment_path, progress, segment_key):
    if segment_key not in progress['completed_segments']:
        return False
    if not os.path.exists(segment_path):
        return False
    expected_size = progress['completed_segments'][segment_key]
    actual_size = os.path.getsize(segment_path)
    return actual_size == expected_size and actual_size > 0


def download_segment(segment_url, segment_path, segment_key, segment_size,
                     temp_dir, progress, phase_bar, overall_bar):
    if is_segment_complete(segment_path, progress, segment_key):
        return segment_key, True, 'skipped'

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(segment_url, stream=True, timeout=60)
            if resp.status_code != 200:
                print(f'\n  ! segment {segment_key}: HTTP {resp.status_code} (attempt {attempt}/{max_retries})')
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                continue

            with open(segment_path, 'wb') as segment_file:
                for chunk in resp.iter_content(chunk_size=8192):
                    segment_file.write(chunk)

            file_size = os.path.getsize(segment_path)
            if file_size == 0:
                print(f'\n  ! segment {segment_key}: empty file (attempt {attempt}/{max_retries})')
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                continue

            with _progress_lock:
                progress['completed_segments'][segment_key] = file_size
            save_progress(temp_dir, progress)

            # Update progress bars by actual bytes downloaded
            phase_bar.update(file_size)
            overall_bar.update(file_size)
            return segment_key, True, 'downloaded'

        except (requests.exceptions.RequestException, IOError) as e:
            print(f'\n  ! segment {segment_key}: {e} (attempt {attempt}/{max_retries})')
            if attempt < max_retries:
                time.sleep(2 ** attempt)

    return segment_key, False, 'failed'


def download(what, to, base, temp_dir, stream_type, phase_num, total_phases, overall_bar):
    label = 'Video' if stream_type == 'video' else 'Audio'
    segments = what['segments']
    total_segments = len(segments)
    total_bytes = sum(seg.get('size', 0) for seg in segments)

    print_phase(phase_num, total_phases, f'Downloading {label} ({total_segments} segments, {format_size(total_bytes)})')

    init_segment = base64.b64decode(what['init_segment'])

    segment_urls = [base + seg['url'] for seg in segments]
    segment_sizes = [seg.get('size', 0) for seg in segments]
    segment_paths = [os.path.join(temp_dir, f'{stream_type}_segment_{i}.tmp') for i in range(total_segments)]
    segment_keys = [f'{stream_type}_{i}' for i in range(total_segments)]

    progress = load_progress(temp_dir)

    # Calculate already-downloaded bytes
    already_done_bytes = 0
    for i, key in enumerate(segment_keys):
        if is_segment_complete(segment_paths[i], progress, key):
            already_done_bytes += progress['completed_segments'].get(key, 0)

    if already_done_bytes > 0:
        already_done_count = sum(1 for i, key in enumerate(segment_keys)
                                 if is_segment_complete(segment_paths[i], progress, key))
        print(f'  Resuming: {already_done_count}/{total_segments} segments ({format_size(already_done_bytes)}) already downloaded')

    failed_segments = []

    bar_format = f'  {label}    |{{bar:40}}| {{percentage:3.0f}}% {{n_fmt}}/{{total_fmt}} [{{elapsed}}<{{remaining}}, {{rate_fmt}}]'
    with tqdm(total=total_bytes, initial=already_done_bytes, bar_format=bar_format,
              unit='B', unit_scale=True, unit_divisor=1024, file=sys.stdout) as phase_bar:

        # Advance overall bar for already-completed bytes
        overall_bar.update(already_done_bytes)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    download_segment, seg_url, seg_path, seg_key, seg_size,
                    temp_dir, progress, phase_bar, overall_bar
                ): seg_key
                for seg_url, seg_path, seg_key, seg_size
                in zip(segment_urls, segment_paths, segment_keys, segment_sizes)
            }

            for future in as_completed(futures):
                seg_key, success, status = future.result()
                if not success:
                    failed_segments.append(seg_key)

    if failed_segments:
        print(f'\n  ERROR: {len(failed_segments)} segments failed after {max_retries} retries each')
        print(f'  Failed: {failed_segments[:10]}{"..." if len(failed_segments) > 10 else ""}')
        print(f'  Run the command again to retry. Progress saved in {temp_dir}')
        sys.exit(1)

    # Assemble segments into output file
    print(f'  Assembling {total_segments} segments...', end=' ', flush=True)
    with open(to, 'wb') as file:
        file.write(init_segment)
        for segment_path in segment_paths:
            with open(segment_path, 'rb') as segment_file:
                file.write(segment_file.read())

    output_size = os.path.getsize(to)
    print(f'{format_size(output_size)}')


name += '.mp4'
base_url = url[:url.rfind('/', 0, -26) + 1]
response = requests.get(url)
if response.status_code >= 400:
    print('error: cant get url content, test your link in browser, code=', response.status_code, '\ncontent:\n', response.content)
    sys.exit(1)

content = response.json()

vid_heights = [(i, d['height']) for (i, d) in enumerate(content['video'])]
vid_idx, _ = max(vid_heights, key=lambda _h: _h[1])

audio_present = bool(content['audio'])

audio_idx = None
if audio_present:
    audio_quality = [(i, d['bitrate']) for (i, d) in enumerate(content['audio'])]
    audio_idx, _ = max(audio_quality, key=lambda _h: _h[1])

base_url = base_url + content['base_url']

# Calculate total download size across all streams
video_info = content['video'][vid_idx]
video_total_bytes = sum(seg.get('size', 0) for seg in video_info['segments'])
audio_total_bytes = 0
if audio_present:
    audio_info = content['audio'][audio_idx]
    audio_total_bytes = sum(seg.get('size', 0) for seg in audio_info['segments'])
grand_total_bytes = video_total_bytes + audio_total_bytes

# Determine phases
total_phases = 1
if audio_present:
    total_phases = 3  # video + audio + mux

# Summary header
print_header(f'vimeo-dl -> {name}')
print(f'  Resolution:  {video_info["width"]}x{video_info["height"]}')
print(f'  Total size:  {format_size(grand_total_bytes)}')
print(f'  Video:       {len(video_info["segments"])} segments ({format_size(video_total_bytes)})')
if audio_present:
    print(f'  Audio:       {len(audio_info["segments"])} segments ({format_size(audio_total_bytes)})')
    print(f'  Audio rate:  {audio_info["bitrate"]//1000}kbps')
print(f'  Workers: {max_workers} | Retries: {max_retries}')

# Use deterministic temp directory for resume support
temp_dir = get_temp_dir(url)

# Overall progress bar (bytes across all streams)
overall_format = '  Overall |{bar:40}| {percentage:3.0f}% {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]'
overall_bar = tqdm(total=grand_total_bytes, bar_format=overall_format,
                   unit='B', unit_scale=True, unit_divisor=1024, file=sys.stdout,
                   position=0, leave=True)

video_tmp_file = os.path.join(temp_dir, 'video.mp4')
video = content['video'][vid_idx]
download(video, video_tmp_file, base_url + video['base_url'], temp_dir, 'video', 1, total_phases, overall_bar)

if not audio_present:
    overall_bar.close()
    os.rename(video_tmp_file, name)
    shutil.rmtree(temp_dir, ignore_errors=True)
    print_header(f'Complete: {name}')
    sys.exit(0)

audio_tmp_file = os.path.join(temp_dir, 'audio.mp4')
audio = content['audio'][audio_idx]
download(audio, audio_tmp_file, base_url + audio['base_url'], temp_dir, 'audio', 2, total_phases, overall_bar)

overall_bar.close()

# Mux
print_phase(3, total_phases, 'Muxing video + audio')

if has_ffmpeg:
    print(f'  Using ffmpeg (codec copy, no re-encode)...', flush=True)
    result = subprocess.run(
        ['ffmpeg', '-y', '-i', video_tmp_file, '-i', audio_tmp_file, '-c:v', 'copy', '-c:a', 'copy', name],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f'  ffmpeg error: {result.stderr[-200:]}')
        sys.exit(1)
else:
    print(f'  Using moviepy (no ffmpeg found)...', flush=True)
    video_clip = VideoFileClip(video_tmp_file)
    audio_clip = AudioFileClip(audio_tmp_file)
    if moviepy_deprecated:
        final_clip = video_clip.set_audio(audio_clip)
    else:
        final_clip = video_clip.with_audio(audio_clip)
    final_clip.write_videofile(name)

final_size = os.path.getsize(name)
shutil.rmtree(temp_dir, ignore_errors=True)

print_header(f'Complete: {name} ({format_size(final_size)})')
