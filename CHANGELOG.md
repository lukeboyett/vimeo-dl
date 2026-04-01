# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-03-31
### :sparkles: New Features
- Resume support for interrupted downloads — segments are saved to a deterministic temp directory with a progress manifest; re-running the same command skips already-downloaded segments
- Retry with exponential backoff for failed segments (configurable via `--retries`, default: 5)
- CLI argument parsing with `--help`, `-o`, `-w`, `-r`, `-t`, `--clean`, `-v` flags
- Byte-based progress tracking — overall progress bar reflects actual data volume, not segment count
- Per-phase progress bars (Video, Audio) plus an Overall bar with percentage, transfer rate, and ETA
- Summary header showing resolution, total size, segment counts, and audio bitrate
- Phase indicators ([1/3], [2/3], [3/3]) for video download, audio download, and mux
- `--clean` flag to start fresh by removing previous temp/resume files
- `--temp-dir` flag to specify where temp/resume files are stored
- Thread-safe progress manifest with atomic file writes
- PyInstaller compatibility for compiled binary distribution

### :bug: Bug Fixes
- Thread-safe progress file writes prevent race condition crash during concurrent segment downloads
- Truncated verbose SSL/connection error messages for readable retry output
- Suppressed PyInstaller multiprocessing resource_tracker warnings

## [0.2.1] - 2025-01-31
### :wrench: Chores
- [`cb11e69`](https://github.com/davidecavestro/vimeo-dl/commit/cb11e6972022db657b55eb0d0b2874725ef48501) - autoinstall deps *(commit by [@davidecavestro](https://github.com/davidecavestro))*


## [0.2.0] - 2025-01-31
### :sparkles: New Features
- [`6feea0a`](https://github.com/davidecavestro/vimeo-dl/commit/6feea0aaf9fa0241035729188290f39d3a563dd0) - align to script version from kbabanov at 2025-01-27 *(commit by [@davidecavestro](https://github.com/davidecavestro))*

### :bug: Bug Fixes
- [`0f1e69e`](https://github.com/davidecavestro/vimeo-dl/commit/0f1e69e8bf14ad0649116a7bc0240b4984d6f909) - restore env vars support to ease automating *(commit by [@davidecavestro](https://github.com/davidecavestro))*

### :wrench: Chores
- [`c59fd00`](https://github.com/davidecavestro/vimeo-dl/commit/c59fd00280410d96547aad13e4e923226b922726) - better dev UX *(commit by [@davidecavestro](https://github.com/davidecavestro))*


## [0.1.0] - 2024-11-17
### :sparkles: New Features
- [`07874d0`](https://github.com/davidecavestro/vimeo-dl/commit/07874d0615feb37fe3062e3bb947da5a9de7b60d) - initial commit *(commit by [@davidecavestro](https://github.com/davidecavestro))*

[0.1.0]: https://github.com/davidecavestro/vimeo-dl/compare/0.0.0...0.1.0
[0.2.0]: https://github.com/davidecavestro/vimeo-dl/compare/0.1.0...0.2.0
[0.2.1]: https://github.com/davidecavestro/vimeo-dl/compare/0.2.0...0.2.1
[0.3.0]: https://github.com/lukeboyett/vimeo-dl/compare/0.2.1...main
