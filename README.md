# lbmd — load-balanced music downloader

Tiered FLAC acquisition as a CLI, compatible with [Explo](https://github.com/LumePart/Explo)
via an slskd-compatible REST shim.

- **Tier 1** — **Qobuz** (hi-res 24-bit FLAC, direct) · Monochrome/Amazon (local only, needs a headed browser)
- **Tier 2** — **Deezer** (16-bit FLAC) · **Soulseek** (via slskd) · Telegram bot
- **Tier 3** — **yt-dlp** (lossy fallback, always on)

Each tier is a pool of swappable sources with a fan-out strategy (`all` /
`uniform`); strict priority cascade Tier 1 → 2 → 3; first *verified* file wins.
Every source is opt-in via env. See [`ARCHITECTURE.md`](ARCHITECTURE.md) and
[`docs/adr/`](docs/adr/).

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
uv run lbmd download "Daft Punk Get Lucky" -o ./downloads
```

Enable sources via env (opt-in). Qobuz example:

```bash
export LBMD_QOBUZ=1 LBMD_QOBUZ_APP_ID=... LBMD_QOBUZ_APP_SECRET=... LBMD_QOBUZ_TOKEN=...
uv run lbmd download "Artist Title" -o ./downloads
```

## Deploy (Docker)

A headless image is published to GHCR on every push to `main` and every `v*`
tag. It ships the headless sources — **Qobuz, Deezer, Soulseek (via an external
slskd), yt-dlp**. Monochrome/Amazon is excluded (it needs a headed browser).

```bash
docker pull ghcr.io/saikhurana98/lbmd:latest

docker run --rm \
  -e LBMD_QOBUZ=1 -e LBMD_QOBUZ_APP_ID=... -e LBMD_QOBUZ_APP_SECRET=... -e LBMD_QOBUZ_TOKEN=... \
  -v "$PWD/downloads:/downloads" \
  ghcr.io/saikhurana98/lbmd:latest download "Artist Title" -o /downloads
```

Or with compose (put creds in a `.env`):

```bash
docker compose run --rm lbmd download "Artist Title" -o /downloads
```

CI runs ruff + mypy + pytest on every PR; the release workflow builds a
multi-arch (amd64/arm64) image.
