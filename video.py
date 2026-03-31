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
import requests
from shutil import which
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

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


def get_temp_dir(source_url):
    """Create a deterministic temp directory based on URL hash for resume support."""
    url_hash = hashlib.sha256(source_url.encode()).hexdigest()[:16]
    temp_dir = os.path.join(os.getcwd(), f'.vimeo-dl-{url_hash}')
    os.makedirs(temp_dir, exist_ok=True)
    return temp_dir


def load_progress(temp_dir):
    """Load download progress from the manifest file."""
    manifest_path = os.path.join(temp_dir, 'progress.json')
    if os.path.exists(manifest_path):
        with open(manifest_path, 'r') as f:
            return json.load(f)
    return {'completed_segments': {}}


def save_progress(temp_dir, progress):
    """Save download progress to the manifest file."""
    manifest_path = os.path.join(temp_dir, 'progress.json')
    with open(manifest_path, 'w') as f:
        json.dump(progress, f)


def is_segment_complete(segment_path, progress, segment_key):
    """Check if a segment has already been downloaded successfully."""
    if segment_key not in progress['completed_segments']:
        return False
    if not os.path.exists(segment_path):
        return False
    # Verify file size matches what we recorded
    expected_size = progress['completed_segments'][segment_key]
    actual_size = os.path.getsize(segment_path)
    return actual_size == expected_size and actual_size > 0


def download_segment(segment_url, segment_path, segment_key, temp_dir, progress):
    """Download a single segment with retry logic."""
    # Skip if already downloaded
    if is_segment_complete(segment_path, progress, segment_key):
        return segment_key, True, 'skipped'

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(segment_url, stream=True, timeout=60)
            if resp.status_code != 200:
                print(f'\nsegment {segment_key}: HTTP {resp.status_code} (attempt {attempt}/{max_retries})')
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                continue

            with open(segment_path, 'wb') as segment_file:
                for chunk in resp.iter_content(chunk_size=8192):
                    segment_file.write(chunk)

            file_size = os.path.getsize(segment_path)
            if file_size == 0:
                print(f'\nsegment {segment_key}: empty file (attempt {attempt}/{max_retries})')
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                continue

            # Record successful download
            progress['completed_segments'][segment_key] = file_size
            save_progress(temp_dir, progress)
            return segment_key, True, 'downloaded'

        except (requests.exceptions.RequestException, IOError) as e:
            print(f'\nsegment {segment_key}: {e} (attempt {attempt}/{max_retries})')
            if attempt < max_retries:
                time.sleep(2 ** attempt)

    return segment_key, False, 'failed'


def download(what, to, base, temp_dir, stream_type):
    print('saving', what['mime_type'], 'to', to)
    init_segment = base64.b64decode(what['init_segment'])

    segment_urls = [base + segment['url'] for segment in what['segments']]
    segment_paths = [os.path.join(temp_dir, f'{stream_type}_segment_{i}.tmp') for i in range(len(segment_urls))]
    segment_keys = [f'{stream_type}_{i}' for i in range(len(segment_urls))]

    progress = load_progress(temp_dir)

    # Count how many are already done
    already_done = sum(1 for key in segment_keys if is_segment_complete(
        os.path.join(temp_dir, f'{stream_type}_segment_{segment_keys.index(key)}.tmp'),
        progress, key
    ))
    if already_done > 0:
        print(f'resuming: {already_done}/{len(segment_urls)} segments already downloaded')

    failed_segments = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                download_segment, seg_url, seg_path, seg_key, temp_dir, progress
            ): seg_key
            for seg_url, seg_path, seg_key in zip(segment_urls, segment_paths, segment_keys)
        }

        with tqdm(total=len(segment_urls), initial=already_done) as pbar:
            for future in as_completed(futures):
                seg_key, success, status = future.result()
                if status != 'skipped':
                    pbar.update(1)
                if not success:
                    failed_segments.append(seg_key)

    if failed_segments:
        print(f'\nerror: {len(failed_segments)} segments failed to download after {max_retries} retries')
        print('failed segments:', failed_segments)
        print(f'run the command again to retry. progress is saved in {temp_dir}')
        sys.exit(1)

    # All segments downloaded - assemble the file
    with open(to, 'wb') as file:
        file.write(init_segment)
        for segment_path in segment_paths:
            with open(segment_path, 'rb') as segment_file:
                file.write(segment_file.read())

    print('done')


name += '.mp4'
base_url = url[:url.rfind('/', 0, -26) + 1]
response = requests.get(url)
if response.status_code >= 400:
    print('error: cant get url content, test your link in browser, code=', response.status_code, '\ncontent:\n', response.content)
    sys.exit(1)

content = response.json()

vid_heights = [(i, d['height']) for (i, d) in enumerate(content['video'])]
vid_idx, _ = max(vid_heights, key=lambda _h: _h[1])

audio_present = True
if not content['audio']:
    audio_present = False

audio_quality = None
audio_idx = None
if audio_present:
    audio_quality = [(i, d['bitrate']) for (i, d) in enumerate(content['audio'])]
    audio_idx, _ = max(audio_quality, key=lambda _h: _h[1])

base_url = base_url + content['base_url']

# Use deterministic temp directory for resume support
temp_dir = get_temp_dir(url)

video_tmp_file = os.path.join(temp_dir, 'video.mp4')
video = content['video'][vid_idx]
download(video, video_tmp_file, base_url + video['base_url'], temp_dir, 'video')

audio_tmp_file = None
if audio_present:
    audio_tmp_file = os.path.join(temp_dir, 'audio.mp4')
    audio = content['audio'][audio_idx]
    download(audio, audio_tmp_file, base_url + audio['base_url'], temp_dir, 'audio')

if not audio_present:
    os.rename(video_tmp_file, name)
    # Clean up temp directory after successful completion
    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)
    sys.exit(0)

if has_ffmpeg:
    subprocess.run(['ffmpeg', '-i', video_tmp_file, '-i', audio_tmp_file, '-c:v', 'copy', '-c:a', 'copy', name])
    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)
    sys.exit(0)

video_clip = VideoFileClip(video_tmp_file)
audio_clip = AudioFileClip(audio_tmp_file)

final_clip = None
if moviepy_deprecated:
    final_clip = video_clip.set_audio(audio_clip)
else:
    final_clip = video_clip.with_audio(audio_clip)

final_clip.write_videofile(name)

import shutil
shutil.rmtree(temp_dir, ignore_errors=True)
