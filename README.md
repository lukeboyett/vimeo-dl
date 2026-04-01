# vimeo-dl

Download segmented videos from Vimeo CDN with **automatic resume**, retry, and progress tracking.

Based on [Javi3rV's script](https://gist.github.com/alexeygrigorev/a1bc540925054b71e1a7268e50ad55cd?permalink_comment_id=5279414#gistcomment-5279414). Supports _playlist.json_ and _master.json_ URLs.

## Features

- **Resume support** — if a download is interrupted (network hiccup, WiFi drop, Ctrl-C), re-run the same command and it picks up where it left off
- **Retry with backoff** — failed segments are retried with exponential backoff
- **Byte-based progress** — progress bars track actual data volume, not just segment count
- **Multi-phase display** — separate progress bars for video, audio, and overall download
- **CLI interface** — proper argument parsing with `--help`

## Usage

> **Always quote the URL** — it contains `&`, `=`, `?` and other characters your shell will interpret.

```bash
# Basic usage
vimeo-dl 'https://...playlist.json?...' -o my_video

# Specify output path
vimeo-dl 'https://...playlist.json?...' -o /path/to/my_video

# More parallel workers
vimeo-dl 'https://...playlist.json?...' -o my_video -w 10

# Start fresh (discard previous partial download)
vimeo-dl 'https://...playlist.json?...' -o my_video --clean

# master.json URLs (delegates to yt-dlp/youtube-dl)
vimeo-dl 'https://...master.json?...' -o my_video
```

### Options

```
usage: vimeo-dl [-h] [-o NAME] [-w N] [-r N] [-t DIR] [--clean] [-v] [url]

  url                 playlist.json or master.json URL
  -o, --output NAME   output filename without .mp4 extension (can include path)
  -w, --workers N     parallel download threads (default: 5, max: 15)
  -r, --retries N     retry attempts per failed segment (default: 5)
  -t, --temp-dir DIR  directory for temp/resume files (default: current directory)
  --clean             remove previous temp/resume files and start fresh
  -v, --version       show version
  -h, --help          show help
```

### Environment variables (fallback)

CLI args take priority. If not provided, these env vars are checked before prompting interactively.

| Variable | CLI equivalent | Default |
|---|---|---|
| `SRC_URL` | positional `url` | _(prompt)_ |
| `OUT_FILE` | `-o` | _(prompt)_ |
| `MAX_WORKERS` | `-w` | `5` |
| `MAX_RETRIES` | `-r` | `5` |

## How resume works

- Segments are saved to a deterministic temp directory (`.vimeo-dl-<hash>`) based on the source URL
- A progress manifest tracks completed segments with file size validation
- On re-run, already-downloaded segments are skipped automatically
- Temp files are only cleaned up after the final video is fully assembled

## Docker

### Docker CLI

```bash
docker run \
  -e 'SRC_URL=https://...' \
  -e 'OUT_FILE=/downloads/video.mp4' \
  -v $(pwd)/out:/downloads \
  --rm -it davidecavestro/vimeo-dl
```

### Docker Compose

```yaml
version: "3"

services:
  downloader:
    build:
      context: .
    volumes:
    - ./out:/downloads
    environment:
    - SRC_URL=${SRC_URL}
    - OUT_FILE=${OUT_FILE}
    - MAX_WORKERS=${MAX_WORKERS}
```

## Disclaimer

This software is released just for educational purposes.
**Please do not use it for illegal activities.**

## Credits

Based on [alexeygrigorev](https://github.com/alexeygrigorev)'s [vimeo-download.py gist](https://gist.github.com/alexeygrigorev/a1bc540925054b71e1a7268e50ad55cd) and [davidecavestro](https://github.com/davidecavestro)'s [vimeo-dl](https://github.com/davidecavestro/vimeo-dl).
