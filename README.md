# lbmd — load-balanced music downloader

Tiered FLAC acquisition as a CLI, compatible with [Explo](https://github.com/LumePart/Explo)
via an slskd-compatible REST shim.

- **Tier 1** ripping sites (Squid/Qobuz, Lucida, Monochrome, Soulseek) → 24-bit FLAC
- **Tier 2** streaming lossless rippers (Spotify / Apple Music) → FLAC
- **Tier 3** yt-dlp / equivalent → 320 kbps lossy fallback

Strict priority cascade 1 → 2 → 3; parallel race within each tier; mandatory
audio verification before a file is accepted. See [`ARCHITECTURE.md`](ARCHITECTURE.md)
and [`docs/adr/`](docs/adr/).

## Develop

```bash
uv sync                 # create venv + install deps
uv run pytest           # test-first; the bar is coverage >= 90%
uv run ruff check .      # lint
uv run ruff format .     # format
uv run mypy              # types (strict)
```

## Run

```bash
uv run lbmd --help
```
