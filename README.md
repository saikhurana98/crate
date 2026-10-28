# crate — load-balanced music downloader

Crate searches multiple music sources in parallel, accepts the first verified
result from the highest available tier, tags it, and places it in a local music
library. It includes a browser console, background download worker, CLI, review
workflow, playlist sync, and an slskd-compatible API for
[Explo](https://github.com/LumePart/Explo).

## Sources

| Tier | Providers | Result |
| --- | --- | --- |
| 1 | Qobuz, Monochrome, TIDAL, Octave, Antra | Lossless |
| 2 | Deezer, arcod, Soulseek, Telegram | Lossless or best available |
| 3 | yt-dlp | Lossy fallback |

Providers can be enabled, ordered, configured, and tested from **Provider
Settings**. Some services require your own account or API credentials. Use Crate
only for media you are authorized to download.

## macOS quick start

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/), then:

```bash
git clone https://github.com/saikhurana98/crate.git
cd crate
cp .crate.env.example .crate.env
mkdir -p data downloads slskd-downloads
docker compose up -d
```

Open [http://localhost:8788](http://localhost:8788). The published image supports
both Apple Silicon and Intel Macs. Check startup with:

```bash
docker compose ps
docker compose logs -f crate-console crate-serve
```

The console and slskd-compatible API bind to localhost by default. Finished
tracks appear under `downloads/library/`; work in progress stays in
`downloads/staging/`.

### Configure providers

Use **Provider Settings** in the console to enter credentials and test each
source. Stored values are encrypted in `data/crate.db` using `data/config.key`.
Back up both files together. Provider secrets are deliberately excluded from
Git and are not exported to a plaintext JSON file.

Environment configuration is also supported:

```bash
# Edit only the values you need, then recreate the services.
$EDITOR .crate.env
docker compose up -d --force-recreate
```

The template documents every current provider setting. Never commit
`.crate.env`, `.env`, `creds/`, `data/`, session files, or downloaded media.
When enabling Soulseek, edit the `slskd-downloads` bind mount in
`docker-compose.yml` so it points to the same host directory used by slskd.

### Run the CLI

The CLI shares the same data and download directories:

```bash
docker compose run --rm crate-cli download "Daft Punk Get Lucky" -o /downloads/library
docker compose run --rm crate-cli download --source monochrome "Zara Sa" -o /downloads/library
```

Stop or update the stack with:

```bash
docker compose down
docker compose pull
docker compose up -d
```

## Run without Docker

Python 3.11 or newer, `uv`, ffmpeg, fpcalc, and yt-dlp are required.

```bash
uv sync --frozen
uv run crate --help
uv run crate download "Artist Title" -o ./downloads
```

Without `CRATE_DB`, the CLI uses only the `CRATE_*` environment flags and does
not load credentials saved by the management console.

## Development

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
cd apps/console && npm ci && npm run build
```

Python coverage must remain at or above 90%. See [AGENTS.md](AGENTS.md) for
repository conventions, [ARCHITECTURE.md](ARCHITECTURE.md) for the system
boundaries, and [docs/adding-a-source.md](docs/adding-a-source.md) for provider
integration guidance.

## Security and licensing

Report vulnerabilities through GitHub private vulnerability reporting; see
[SECURITY.md](SECURITY.md). Crate is available under the [MIT License](LICENSE).
