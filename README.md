# crate — load-balanced music downloader

Tiered FLAC acquisition as a CLI, compatible with [Explo](https://github.com/LumePart/Explo)
via an slskd-compatible REST shim.

- **Tier 1** — **Qobuz**, **Monochrome**, **TIDAL**, **Octave**, and **Antra** lossless sources
- **Tier 2** — **Deezer**, **arcod**, **Soulseek** (via slskd), and Telegram
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
uv run crate --help
uv run crate download "Daft Punk Get Lucky" -o ./downloads
```

Enable sources via env (opt-in). Qobuz example:

```bash
export CRATE_QOBUZ=1 CRATE_QOBUZ_APP_ID=... CRATE_QOBUZ_APP_SECRET=... CRATE_QOBUZ_TOKEN=...
uv run crate download "Artist Title" -o ./downloads
```

Octave uses its official member authorization. Join the free Octave community,
restore the library with the issued `octv_…` account key, then save it in the
provider settings or configure it for the CLI:

```bash
export CRATE_OCTAVE=1 CRATE_OCTAVE_ACCOUNT_KEY=octv_...
uv run crate download "Artist - Title" -o ./downloads
```

## Deploy (Docker)

A headless image is published to GHCR on every push to `main` and every `v*`
tag. It ships all provider adapters listed above and yt-dlp.

```bash
docker pull ghcr.io/saikhurana98/crate:latest

docker run --rm \
  -e CRATE_QOBUZ=1 -e CRATE_QOBUZ_APP_ID=... -e CRATE_QOBUZ_APP_SECRET=... -e CRATE_QOBUZ_TOKEN=... \
  -v "$PWD/downloads:/downloads" \
  ghcr.io/saikhurana98/crate:latest download "Artist Title" -o /downloads
```

Or with compose (put creds in a `.env`):

```bash
docker compose run --rm crate download "Artist Title" -o /downloads
```

CI runs ruff + mypy + pytest on every PR; the release workflow builds the
amd64 image used by both deployment targets.
