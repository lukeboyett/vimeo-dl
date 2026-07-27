#!/usr/bin/env python3
"""vimeo-dl: Download segmented videos from Vimeo CDN with resume support."""

import subprocess
import sys
import argparse
import os
import json
import hashlib
import base64
import time
import signal
import shutil
import threading
import warnings
import sysconfig
import venv
from urllib.parse import urljoin, urlsplit, urlunsplit
from shutil import which
from concurrent.futures import ThreadPoolExecutor, as_completed

# Suppress noisy multiprocessing resource_tracker warnings (PyInstaller artifact)
warnings.filterwarnings('ignore', message='resource_tracker:.*', category=UserWarning)

__version__ = '0.5.1'

SEGMENT_SIZE_TOLERANCE = 1

# Lock for thread-safe progress updates
_progress_lock = threading.Lock()

# Graceful shutdown flag
_shutdown = threading.Event()

# Lazy-loaded after ensure_deps()
requests = None
tqdm = None
_json_progress = False


def emit(event, **fields):
    """Emit one stable NDJSON event when machine progress is enabled."""
    if _json_progress:
        print(json.dumps({'event': event, **fields}, separators=(',', ':')), flush=True)


def log(message='', *, end='\n', flush=False):
    """Keep stdout machine-readable in --json-progress mode."""
    print(message, end=end, flush=flush, file=sys.stderr if _json_progress else sys.stdout)


def _handle_signal(signum, frame):
    """Handle Ctrl-C: signal threads to stop, let them finish current work."""
    if _shutdown.is_set():
        # Second Ctrl-C: force exit
        log('\n  Forced exit. Completed segments have been saved.')
        os._exit(1)
    _shutdown.set()
    log('\n  Shutting down gracefully... saving completed segments.')
    log('  (press Ctrl-C again to force quit)')
    emit('shutdown_requested', signal=signum)


def parse_args():
    parser = argparse.ArgumentParser(
        prog='vimeo-dl',
        description='Download segmented videos from Vimeo CDN with resume support.',
        epilog='''examples:
  vimeo-dl 'https://...playlist.json?...' -o my_video
  vimeo-dl 'https://...master.json?...' -o my_video
  vimeo-dl 'https://...playlist.json?...' -o /path/to/my_video -w 10
  vimeo-dl --batch urls.txt -w 10

batch file format (one per line, tab-separated):
  URL<tab>OUTPUT_NAME
  https://...playlist.json?...\tmy_video
  https://...playlist.json?...\tmy_other_video

NOTE: Always quote the URL to prevent shell interpretation of special
characters (?, &, =, etc). Use single quotes to be safe.''',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        'url', nargs='?', default=None,
        help="playlist.json or master.json URL (QUOTE THIS — it contains &, =, etc)",
    )
    parser.add_argument(
        '-o', '--output', default=None, metavar='NAME',
        help='output filename without .mp4 extension (can include path)',
    )
    parser.add_argument(
        '-b', '--batch', default=None, metavar='FILE',
        help='batch file with one URL<tab>OUTPUT_NAME per line',
    )
    parser.add_argument(
        '-w', '--workers', type=int, default=None, metavar='N',
        help='parallel download threads (default: 5, max: 15)',
    )
    parser.add_argument(
        '-r', '--retries', type=int, default=None, metavar='N',
        help='retry attempts per failed segment (default: 5)',
    )
    parser.add_argument(
        '-t', '--temp-dir', default=None, metavar='DIR',
        help='directory for temp/resume files (default: current directory)',
    )
    parser.add_argument(
        '--clean', action='store_true',
        help='remove any existing temp/resume files for this URL and start fresh',
    )
    parser.add_argument(
        '--no-input', action='store_true',
        help='never prompt; fail if URL or output is missing',
    )
    parser.add_argument(
        '--json-progress', action='store_true',
        help='write newline-delimited JSON events to stdout (human logs go to stderr)',
    )
    parser.add_argument(
        '-v', '--version', action='version', version=f'%(prog)s {__version__}',
        help=f'show version ({__version__}) and exit',
    )

    # PyInstaller leaves Python interpreter flags and multiprocessing bootstrap
    # args in sys.argv. Filter them out before parsing.
    pyinstaller_flags = {'-B', '-S', '-I', '-c'}
    filtered_argv = [a for a in sys.argv[1:]
                     if a not in pyinstaller_flags and 'multiprocessing' not in a]
    args = parser.parse_args(filtered_argv)

    # Batch mode doesn't need url/output
    if not args.batch:
        args.url = args.url or os.getenv('SRC_URL')
        args.output = args.output or os.getenv('OUT_FILE')
        can_prompt = not args.no_input and sys.stdin.isatty()
        if not args.url and can_prompt:
            args.url = input("Enter playlist.json or master.json URL (use quotes!): ")
        if not args.output and can_prompt:
            args.output = input("Enter output filename (without .mp4): ")
        if not args.url or not args.output:
            parser.error('URL and --output are required in non-interactive mode')

    args.workers = args.workers if args.workers is not None else int(os.getenv('MAX_WORKERS', 5))
    args.retries = args.retries if args.retries is not None else int(os.getenv('MAX_RETRIES', 5))
    if not 1 <= args.workers <= 15:
        parser.error('--workers must be between 1 and 15')
    if args.retries < 1:
        parser.error('--retries must be at least 1')

    return args


def detect_tools():
    return {
        'ffmpeg': which('ffmpeg') is not None,
        'youtube_dl': which('youtube-dl') is not None,
        'yt_dlp': which('yt-dlp') is not None,
    }


def format_size(nbytes):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if nbytes < 1024:
            return f'{nbytes:.1f}{unit}'
        nbytes /= 1024
    return f'{nbytes:.1f}PB'


def check_disk_space(path, required_bytes):
    """Check if there's enough disk space. Returns (ok, available_bytes)."""
    stat = os.statvfs(path)
    available = stat.f_bavail * stat.f_frsize
    return available >= required_bytes, available


def print_header(text):
    width = 60
    log()
    log(f'{"=" * width}')
    log(f'  {text}')
    log(f'{"=" * width}')


def print_phase(phase_num, total_phases, label):
    log(f'\n[{phase_num}/{total_phases}] {label}')
    log(f'{"-" * 50}')


def get_temp_dir(source_url, base_dir=None):
    url_hash = hashlib.sha256(source_url.encode()).hexdigest()[:16]
    parent = base_dir or os.getcwd()
    temp_dir = os.path.join(parent, f'.vimeo-dl-{url_hash}')
    os.makedirs(temp_dir, exist_ok=True)
    return temp_dir


def load_progress(temp_dir):
    manifest_path = os.path.join(temp_dir, 'progress.json')
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, 'r') as f:
                data = json.load(f)
            if not isinstance(data.get('completed_segments'), dict):
                raise ValueError('completed_segments is not an object')
            return data
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f'invalid resume manifest {manifest_path}: {exc}; use --clean') from exc
    return {'completed_segments': {}, 'stream_fingerprints': {}}


def save_progress(temp_dir, progress):
    manifest_path = os.path.join(temp_dir, 'progress.json')
    tmp_path = manifest_path + '.tmp'
    with _progress_lock:
        snapshot = {
            'completed_segments': dict(progress['completed_segments']),
            'stream_fingerprints': dict(progress.get('stream_fingerprints', {})),
        }
        with open(tmp_path, 'w') as f:
            json.dump(snapshot, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, manifest_path)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def is_segment_complete(segment_path, progress, segment_key, expected_size=0):
    if segment_key not in progress['completed_segments']:
        return False
    if not os.path.exists(segment_path):
        return False
    record = progress['completed_segments'][segment_key]
    recorded_size = record.get('size', 0) if isinstance(record, dict) else record
    actual_size = os.path.getsize(segment_path)
    if actual_size <= 0 or actual_size != recorded_size:
        return False
    return not isinstance(record, dict) or not record.get('sha256') or file_sha256(segment_path) == record['sha256']


def download_segment(segment_url, segment_path, segment_key, segment_size,
                     temp_dir, progress, phase_bar, overall_bar, max_retries):
    if is_segment_complete(segment_path, progress, segment_key, segment_size):
        return segment_key, True, 'skipped'

    if _shutdown.is_set():
        return segment_key, False, 'cancelled'

    for attempt in range(1, max_retries + 1):
        if _shutdown.is_set():
            return segment_key, False, 'cancelled'

        try:
            resp = requests.get(segment_url, stream=True, timeout=(15, 60))
            if resp.status_code != 200:
                log(f'\n  ! segment {segment_key}: HTTP {resp.status_code} (attempt {attempt}/{max_retries})')
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                continue

            partial_path = segment_path + '.part'
            digest = hashlib.sha256()
            with open(partial_path, 'wb') as segment_file:
                for chunk in resp.iter_content(chunk_size=8192):
                    if _shutdown.is_set():
                        return segment_key, False, 'cancelled'
                    if chunk:
                        segment_file.write(chunk)
                        digest.update(chunk)

            file_size = os.path.getsize(partial_path)
            response_size = int(resp.headers.get('Content-Length', 0))
            # Vimeo/VHX playlist sizes can be off by one. Content-Length describes
            # the response actually transferred, so use it as the integrity signal
            # and allow a one-byte transport metadata discrepancy.
            wanted_size = response_size or segment_size
            if file_size == 0 or (response_size and
                                  abs(file_size - response_size) > SEGMENT_SIZE_TOLERANCE):
                log(f'\n  ! segment {segment_key}: size mismatch, expected {wanted_size}, got {file_size} (attempt {attempt}/{max_retries})')
                os.remove(partial_path)
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                continue
            os.replace(partial_path, segment_path)

            with _progress_lock:
                progress['completed_segments'][segment_key] = {
                    'size': file_size, 'sha256': digest.hexdigest(),
                }
            save_progress(temp_dir, progress)

            phase_bar.update(file_size)
            overall_bar.update(file_size)
            emit('segment_complete', stream=segment_key.rsplit('_', 1)[0],
                 segment=int(segment_key.rsplit('_', 1)[1]), bytes=file_size)
            return segment_key, True, 'downloaded'

        except (requests.exceptions.RequestException, IOError) as e:
            err_msg = str(e)
            if 'Caused by' in err_msg:
                err_msg = err_msg[err_msg.rfind('Caused by'):]
            elif len(err_msg) > 120:
                err_msg = err_msg[:120] + '...'
            log(f'\n  ! segment {segment_key}: {err_msg} (attempt {attempt}/{max_retries})')
            try:
                os.remove(segment_path + '.part')
            except FileNotFoundError:
                pass
            if attempt < max_retries:
                time.sleep(2 ** attempt)

    return segment_key, False, 'failed'


def download(what, to, base, temp_dir, stream_type, phase_num, total_phases, overall_bar, args):
    label = 'Video' if stream_type == 'video' else 'Audio'
    segments = what['segments']
    total_segments = len(segments)
    total_bytes = sum(seg.get('size', 0) for seg in segments)

    print_phase(phase_num, total_phases, f'Downloading {label} ({total_segments} segments, {format_size(total_bytes)})')

    init_segment = base64.b64decode(what['init_segment'])

    segment_urls = [urljoin(base, seg['url']) for seg in segments]
    segment_sizes = [seg.get('size', 0) for seg in segments]
    segment_paths = [os.path.join(temp_dir, f'{stream_type}_segment_{i}.tmp') for i in range(total_segments)]
    segment_keys = [f'{stream_type}_{i}' for i in range(total_segments)]

    progress = load_progress(temp_dir)
    fingerprint_data = {
        'init_segment': what['init_segment'],
        'segments': [{'url': seg['url'], 'size': seg.get('size', 0)} for seg in segments],
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_data, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()
    prior_fingerprint = progress.get('stream_fingerprints', {}).get(stream_type)
    if prior_fingerprint and prior_fingerprint != fingerprint:
        raise RuntimeError(
            f'{stream_type} playlist changed since this resume was created; use --clean'
        )
    progress.setdefault('stream_fingerprints', {})[stream_type] = fingerprint
    save_progress(temp_dir, progress)

    already_done_bytes = 0
    for i, key in enumerate(segment_keys):
        if is_segment_complete(segment_paths[i], progress, key, segment_sizes[i]):
            record = progress['completed_segments'][key]
            already_done_bytes += record.get('size', 0) if isinstance(record, dict) else record

    if already_done_bytes > 0:
        already_done_count = sum(1 for i, key in enumerate(segment_keys)
                                 if is_segment_complete(segment_paths[i], progress, key, segment_sizes[i]))
        log(f'  Resuming: {already_done_count}/{total_segments} segments ({format_size(already_done_bytes)}) already downloaded')
        emit('resume', stream=stream_type, completed_segments=already_done_count,
             total_segments=total_segments, completed_bytes=already_done_bytes)

    failed_segments = []
    cancelled = False

    bar_format = f'  {label}    |{{bar:40}}| {{percentage:3.0f}}% {{n_fmt}}/{{total_fmt}} [{{elapsed}}<{{remaining}}, {{rate_fmt}}]'
    emit('phase_start', phase=stream_type, total_segments=total_segments, total_bytes=total_bytes)
    with tqdm(total=total_bytes, initial=already_done_bytes, bar_format=bar_format,
              unit='B', unit_scale=True, unit_divisor=1024,
              file=sys.stderr if _json_progress else sys.stdout, disable=_json_progress) as phase_bar:

        overall_bar.update(already_done_bytes)

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    download_segment, seg_url, seg_path, seg_key, seg_size,
                    temp_dir, progress, phase_bar, overall_bar, args.retries
                ): seg_key
                for seg_url, seg_path, seg_key, seg_size
                in zip(segment_urls, segment_paths, segment_keys, segment_sizes)
            }

            for future in as_completed(futures):
                seg_key, success, status = future.result()
                if status == 'cancelled':
                    cancelled = True
                elif not success:
                    failed_segments.append(seg_key)

    if cancelled:
        progress = load_progress(temp_dir)
        done = sum(1 for i, key in enumerate(segment_keys)
                   if is_segment_complete(segment_paths[i], progress, key, segment_sizes[i]))
        log(f'\n  Stopped. {done}/{total_segments} segments saved. Run again to resume.')
        emit('cancelled', stream=stream_type, completed_segments=done, total_segments=total_segments)
        return False

    if failed_segments:
        log(f'\n  ERROR: {len(failed_segments)} segments failed after {args.retries} retries each')
        log(f'  Failed: {failed_segments[:10]}{"..." if len(failed_segments) > 10 else ""}')
        log(f'  Run the command again to retry. Progress saved in {temp_dir}')
        emit('error', code='segment_download_failed', stream=stream_type,
             failed_segments=failed_segments, temp_dir=temp_dir)
        return False

    log(f'  Assembling {total_segments} segments...', end=' ', flush=True)
    assembled_part = to + '.part'
    with open(assembled_part, 'wb') as file:
        file.write(init_segment)
        for segment_path in segment_paths:
            with open(segment_path, 'rb') as segment_file:
                shutil.copyfileobj(segment_file, file, length=1024 * 1024)
        file.flush()
        os.fsync(file.fileno())
    os.replace(assembled_part, to)

    output_size = os.path.getsize(to)
    log(f'{format_size(output_size)}')
    emit('phase_complete', phase=stream_type, bytes=output_size)
    return True


def download_single(url, name, args, tools):
    """Download a single video. Returns True on success."""
    if 'deps://install' == url:
        log('exiting after installing dependencies')
        return True

    if 'master.json' in url:
        parsed = urlsplit(url)
        url = urlunsplit((parsed.scheme, parsed.netloc,
                          parsed.path.replace('master.json', 'master.mpd'),
                          'query_string_ranges=1', ''))
        log(url)

        if tools['yt_dlp']:
            result = subprocess.run(['yt-dlp', url, '-o', name])
            return result.returncode == 0

        if tools['youtube_dl']:
            result = subprocess.run(['youtube-dl', url, '-o', name])
            return result.returncode == 0

        log('error: yt-dlp or youtube-dl is required for master.json URLs')
        emit('error', code='missing_downloader', message='yt-dlp or youtube-dl is required')
        return False

    if not name.lower().endswith('.mp4'):
        name += '.mp4'
    output_dir = os.path.dirname(os.path.abspath(name)) or os.getcwd()
    if not os.path.isdir(output_dir):
        log(f'error: output directory does not exist: {output_dir}')
        return False
    try:
        response = requests.get(url, timeout=(15, 60))
    except requests.exceptions.RequestException as exc:
        log(f'error: could not fetch playlist: {exc}')
        emit('error', code='playlist_request_failed', message=str(exc))
        return False
    if response.status_code >= 400:
        log(f'error: playlist request failed with HTTP {response.status_code}: {url}')
        return False
    try:
        content = response.json()
        if not content.get('video'):
            raise ValueError('playlist has no video streams')
    except (ValueError, json.JSONDecodeError) as exc:
        log(f'error: invalid Vimeo playlist JSON: {exc}')
        return False

    vid_heights = [(i, d['height']) for (i, d) in enumerate(content['video'])]
    vid_idx, _ = max(vid_heights, key=lambda _h: _h[1])

    audio_present = bool(content['audio'])

    audio_idx = None
    if audio_present:
        audio_quality = [(i, d['bitrate']) for (i, d) in enumerate(content['audio'])]
        audio_idx, _ = max(audio_quality, key=lambda _h: _h[1])

    base_url = urljoin(url, content['base_url'])

    video_info = content['video'][vid_idx]
    video_total_bytes = sum(seg.get('size', 0) for seg in video_info['segments'])
    audio_total_bytes = 0
    if audio_present:
        audio_info = content['audio'][audio_idx]
        audio_total_bytes = sum(seg.get('size', 0) for seg in audio_info['segments'])
    grand_total_bytes = video_total_bytes + audio_total_bytes

    total_phases = 1
    if audio_present:
        total_phases = 3

    print_header(f'vimeo-dl v{__version__} -> {name}')
    log(f'  Resolution:  {video_info["width"]}x{video_info["height"]}')
    log(f'  Total size:  {format_size(grand_total_bytes)}')
    log(f'  Video:       {len(video_info["segments"])} segments ({format_size(video_total_bytes)})')
    if audio_present:
        log(f'  Audio:       {len(audio_info["segments"])} segments ({format_size(audio_total_bytes)})')
        log(f'  Audio rate:  {audio_info["bitrate"]//1000}kbps')
    log(f'  Workers: {args.workers} | Retries: {args.retries}')
    emit('download_start', output=name, resolution=f'{video_info["width"]}x{video_info["height"]}',
         total_bytes=grand_total_bytes, video_segments=len(video_info['segments']),
         audio_segments=len(audio_info['segments']) if audio_present else 0)

    # Check disk space (need room for segments + assembled files + final muxed output)
    # Rough estimate: 2.5x the download size (segments + assembled video/audio + muxed output)
    space_needed = int(grand_total_bytes * 2.5)
    space_ok, space_available = check_disk_space(output_dir, space_needed)
    if not space_ok:
        log('\n  WARNING: Low disk space!')
        log(f'  Available:  {format_size(space_available)}')
        log(f'  Estimated:  {format_size(space_needed)} (download + assembly + mux)')
        log('  Proceeding anyway — monitor disk space during download.')

    temp_dir = get_temp_dir(url, args.temp_dir)

    if args.clean and os.path.exists(temp_dir):
        log(f'  Cleaning previous temp files in {temp_dir}')
        shutil.rmtree(temp_dir)
        os.makedirs(temp_dir, exist_ok=True)

    overall_format = '  Overall |{bar:40}| {percentage:3.0f}% {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]'
    overall_bar = tqdm(total=grand_total_bytes, bar_format=overall_format,
                       unit='B', unit_scale=True, unit_divisor=1024,
                       file=sys.stderr if _json_progress else sys.stdout,
                       position=0, leave=True, disable=_json_progress)

    video_tmp_file = os.path.join(temp_dir, 'video.mp4')
    video = content['video'][vid_idx]
    if not download(video, video_tmp_file, urljoin(base_url, video['base_url']), temp_dir, 'video', 1, total_phases, overall_bar, args):
        overall_bar.close()
        return False

    if not audio_present:
        overall_bar.close()
        os.replace(video_tmp_file, name)
        shutil.rmtree(temp_dir, ignore_errors=True)
        print_header(f'Complete: {name}')
        emit('complete', output=name, bytes=os.path.getsize(name))
        return True

    audio_tmp_file = os.path.join(temp_dir, 'audio.mp4')
    audio = content['audio'][audio_idx]
    if not download(audio, audio_tmp_file, urljoin(base_url, audio['base_url']), temp_dir, 'audio', 2, total_phases, overall_bar, args):
        overall_bar.close()
        return False

    overall_bar.close()

    print_phase(3, total_phases, 'Muxing video + audio')

    if tools['ffmpeg']:
        log('  Using ffmpeg (codec copy, no re-encode)...', flush=True)
        final_part = name + '.part.mp4'
        result = subprocess.run(
            ['ffmpeg', '-y', '-i', video_tmp_file, '-i', audio_tmp_file,
             '-c:v', 'copy', '-c:a', 'copy', final_part],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            log(f'  ffmpeg error: {result.stderr[-500:]}')
            try:
                os.remove(final_part)
            except FileNotFoundError:
                pass
            return False
        os.replace(final_part, name)
    else:
        log('  Using moviepy (no ffmpeg found)...', flush=True)
        try:
            from moviepy.editor import VideoFileClip, AudioFileClip
            moviepy_deprecated = True
        except ImportError:
            from moviepy import VideoFileClip, AudioFileClip
            moviepy_deprecated = False
        video_clip = VideoFileClip(video_tmp_file)
        audio_clip = AudioFileClip(audio_tmp_file)
        if moviepy_deprecated:
            final_clip = video_clip.set_audio(audio_clip)
        else:
            final_clip = video_clip.with_audio(audio_clip)
        final_part = name + '.part.mp4'
        try:
            final_clip.write_videofile(final_part)
            os.replace(final_part, name)
        finally:
            final_clip.close()
            audio_clip.close()
            video_clip.close()

    final_size = os.path.getsize(name)
    shutil.rmtree(temp_dir, ignore_errors=True)

    print_header(f'Complete: {name} ({format_size(final_size)})')
    emit('complete', output=name, bytes=final_size)
    return True


def _externally_managed():
    """Return whether this interpreter is governed by PEP 668."""
    return (sys.prefix == sys.base_prefix and
            os.path.isfile(os.path.join(sysconfig.get_path('stdlib'), 'EXTERNALLY-MANAGED')))


def _bootstrap_venv(missing):
    """Install dependencies in a user-cache venv and restart under it."""
    cache_root = os.getenv('XDG_CACHE_HOME', os.path.join(os.path.expanduser('~'), '.cache'))
    venv_dir = os.path.join(cache_root, 'vimeo-dl',
                            f'py{sys.version_info.major}.{sys.version_info.minor}')
    try:
        venv.EnvBuilder(with_pip=True).create(venv_dir)
        venv_python = os.path.join(venv_dir, 'bin', 'python')
        subprocess.check_call([venv_python, '-m', 'pip', 'install', *sorted(missing)])
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            'This Python is externally managed (PEP 668), and vimeo-dl could not '
            f'create its dependency environment at {venv_dir}. Create a venv manually '
            'and install requests tqdm moviepy, then run video.py with that venv Python.'
        ) from exc
    os.execv(venv_python, [venv_python, os.path.abspath(__file__), *sys.argv[1:]])


def ensure_deps():
    # Skip dep install when running as a compiled binary (PyInstaller)
    if getattr(sys, '_MEIPASS', None):
        return
    import importlib.metadata
    required = {'requests', 'tqdm', 'moviepy'}
    installed = {pkg.metadata['Name'] for pkg in importlib.metadata.distributions()}
    missing = required - installed
    if missing:
        if _externally_managed():
            _bootstrap_venv(missing)
            return
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--upgrade', 'pip'])
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', *missing])


def main():
    # Install signal handler for graceful Ctrl-C
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    args = parse_args()
    global _json_progress
    _json_progress = args.json_progress
    ensure_deps()

    global requests, tqdm
    import requests as _requests
    from tqdm import tqdm as _tqdm
    requests = _requests
    tqdm = _tqdm

    tools = detect_tools()

    # Batch mode
    if args.batch:
        if not os.path.exists(args.batch):
            log(f'error: batch file not found: {args.batch}')
            sys.exit(1)

        jobs = []
        with open(args.batch, 'r') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) != 2:
                    log(f'error: line {line_num}: expected URL<tab>OUTPUT_NAME, got {len(parts)} field(s)')
                    sys.exit(1)
                jobs.append((parts[0].strip(), parts[1].strip()))

        print_header(f'Batch download: {len(jobs)} videos')
        succeeded = 0
        failed = 0

        for i, (job_url, job_name) in enumerate(jobs, 1):
            if _shutdown.is_set():
                log(f'\n  Batch interrupted. {succeeded} completed, {len(jobs) - i + 1} remaining.')
                break

            log(f'\n  [{i}/{len(jobs)}] {job_name}')
            _shutdown.clear()  # Reset for each job
            if download_single(job_url, job_name, args, tools):
                succeeded += 1
            else:
                failed += 1

        print_header(f'Batch complete: {succeeded} succeeded, {failed} failed, {len(jobs) - succeeded - failed} skipped')
        sys.exit(130 if _shutdown.is_set() else (1 if failed > 0 else 0))

    # Single download mode
    success = download_single(args.url, args.output, args, tools)
    sys.exit(0 if success else (130 if _shutdown.is_set() else 1))


if __name__ == '__main__':
    # PyInstaller + multiprocessing: child processes re-invoke the binary with
    # arguments like "from multiprocessing.resource_tracker import main;main(9)".
    # Detect and exit early so they don't get parsed as a URL.
    import multiprocessing
    multiprocessing.freeze_support()

    # Also check for resource_tracker invocations that slip through
    if any('multiprocessing' in arg for arg in sys.argv[1:]):
        sys.exit(0)

    main()
