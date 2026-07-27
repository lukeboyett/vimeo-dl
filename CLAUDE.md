# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`vimeo-dl` is a **single-file Python CLI** (`video.py`, ~550 lines) that downloads segmented
videos from the Vimeo CDN with resume, retry-with-backoff, and byte-based progress bars.
It is a fork of [davidecavestro/vimeo-dl](https://github.com/davidecavestro/vimeo-dl)
(`upstream`); this repo (`origin`, `lukeboyett/vimeo-dl`) added the resume/retry/CLI/batch
features in 0.3.0–0.4.0.

There is **no package, no module hierarchy, and no test suite.** All logic lives in `video.py`.

## Running

Dependencies (`requests`, `tqdm`, `moviepy`) are **auto-installed at runtime** by
`ensure_deps()` on first run — there is no `requirements.txt` (the `scripts/setup` reference
to one is dead). So just run the script:

```bash
python3 video.py 'https://...playlist.json?...' -o my_video        # native segmented download
python3 video.py 'https://...master.json?...'   -o my_video        # delegates to yt-dlp/youtube-dl
python3 video.py --batch urls.txt -w 10                            # batch: URL<tab>OUTPUT_NAME per line
```

Always **single-quote the URL** — it contains `&`, `=`, `?`. Flags: `-o/--output`,
`-w/--workers` (default 5, max 15), `-r/--retries` (default 5), `-t/--temp-dir`,
`--clean`, `-b/--batch`, `-v/--version`. CLI args > env vars (`SRC_URL`, `OUT_FILE`,
`MAX_WORKERS`, `MAX_RETRIES`) > interactive prompt.

Optional external tools (auto-detected via `which` in `detect_tools()`): `ffmpeg`
(preferred muxer), `yt-dlp`/`youtube-dl` (required for `master.json` URLs).

### Docker

```bash
docker build -t vimeo-dl .
docker run -e 'SRC_URL=...' -e 'OUT_FILE=/downloads/video.mp4' -v $(pwd)/out:/downloads --rm -it vimeo-dl
```

The Dockerfile bakes deps in at build time by running `video.py` with the sentinel
`SRC_URL=deps://install` (see `download_single()` early return).

## Architecture (the parts that span the file)

**Two URL paths** (`download_single()`): a `master.json` URL is rewritten to `master.mpd`
and handed to `yt-dlp`/`youtube-dl`; everything else is treated as a `playlist.json` and
goes through the native pipeline below. Most of the codebase serves the native path.

**Native download pipeline** for `playlist.json`:
1. Fetch JSON; pick **highest-resolution** video stream and **highest-bitrate** audio stream.
2. `download()` each stream via a `ThreadPoolExecutor` (`args.workers` threads), one
   `download_segment()` task per segment, with exponential backoff retry (`2 ** attempt`).
3. Concatenate the base64 `init_segment` + all segment `.tmp` files into `video.mp4`/`audio.mp4`.
4. **Mux** (`total_phases == 3` when audio present): `ffmpeg -c copy` (no re-encode) if available,
   else fall back to `moviepy` (handles both old `moviepy.editor` and new flat import).

**Resume mechanism** — the core feature:
- Temp dir is deterministic: `.vimeo-dl-<sha256(url)[:16]>` under cwd (or `--temp-dir`).
  Same URL → same dir → automatic resume on re-run.
- `progress.json` maps `segment_key` → byte size. `is_segment_complete()` re-validates
  by checking the file exists AND its on-disk size matches the manifest (guards against
  truncated/partial writes).
- `save_progress()` is thread-safe (`_progress_lock`) and atomic (write `.tmp` + `os.replace`).
- Temp dir is only removed **after** the final muxed file exists. Failed/cancelled runs
  leave it in place for resume.

**Graceful shutdown** (`_handle_signal` + `_shutdown` `threading.Event`): first Ctrl-C sets
the event so in-flight segments finish and progress is saved (workers poll `_shutdown`
between chunks); second Ctrl-C calls `os._exit(1)`. In batch mode `_shutdown` is cleared
between jobs.

**PyInstaller compatibility** — several non-obvious workarounds; do not remove when editing:
- `sys.argv` is filtered of interpreter flags (`-B -S -I -c`) and `multiprocessing`
  bootstrap args before `argparse` sees them (`parse_args()`).
- `__main__` calls `multiprocessing.freeze_support()` and early-exits any process whose
  argv mentions `multiprocessing` (child re-invocations of the frozen binary).
- The top-level `try/except` swallows `zlib`/`pyimod` errors thrown during interpreter
  shutdown so a completed download still exits 0.
- `ensure_deps()` no-ops when frozen (`sys._MEIPASS` set); deps are bundled instead.

## Fork delta vs upstream

Upstream (`davidecavestro/vimeo-dl`, fork point `597879b`) was a 162-line flat script run
top-to-bottom with `sys.exit()` calls — no functions beyond `download`/`download_segment`,
no resume, no retry, no CLI, no progress beyond a per-segment count bar. This fork tripled
it (→550 lines) across 13 commits: resume, byte-based progress + thread safety, the
`argparse`/`main()`/`download_single()` refactor, graceful Ctrl-C + disk check + batch mode,
and a cluster of seven PyInstaller fixes.

Two behavioral tradeoffs this fork introduced (not obvious from `CHANGELOG.md`):

1. **Temp dirs are keyed on the URL, not randomized.** Upstream used a random per-download
   suffix so two downloads could share a folder safely. The deterministic
   `.vimeo-dl-<sha256(url)[:16]>` dir is what enables resume — but running the *same URL*
   twice concurrently would now share a temp dir and collide. Preserve URL-keyed naming
   when touching `get_temp_dir()`; it is load-bearing for resume.
2. **Segments are kept until the final mux, not deleted during assembly.** Upstream
   `os.remove`'d each segment as it concatenated; this fork keeps them so re-runs can resume,
   raising peak disk use — which is why the disk-space check exists. Don't add
   mid-assembly cleanup without breaking resume.

## Conventions

- **Version** lives in `__version__` (`video.py:22`); bump it alongside a `CHANGELOG.md`
  entry (Keep a Changelog format, semver). Release tags trigger the
  `.github/workflows/docker-image.yml` multi-arch build + GitHub release.
- 4-space indent, LF line endings, trim trailing whitespace (see `.devcontainer.json`).
- New runtime deps must be added to the `required` set in `ensure_deps()` (and lazy-imported),
  not to a requirements file.
