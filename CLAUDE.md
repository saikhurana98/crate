# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`crate` — a tiered FLAC-acquisition **CLI** (Python 3.11, `src/crate` package). Given a track query it walks a strict source cascade (Tier 1 → 2 → 3) and keeps the first file that verifies as the wanted quality. Also runs an slskd-compatible HTTP shim so [Explo](https://github.com/LumePart/Explo) downloads through it, and a read-only management console.

## ⚠️ Docs vs. code drift — read this first

The project **pivoted from TypeScript/Node/pnpm to Python** on 2026-07-02 ([`docs/adr/0017-pivot-python-cli-tdd.md`](docs/adr/0017-pivot-python-cli-tdd.md)). Several top-level docs were written **before** the pivot and describe the dead TS design — **do not trust them for implementation facts**:

- `ARCHITECTURE.md` — TS/WebSocket/monorepo design. Header flags the pivot. The *conceptual* design (tier cascade, hexagonal, SQLite, NAS atomic write, Explo shim) still holds; the language/transport/layout details are stale.
- `IMPLEMENTATION.md`, `WORK.md` — the abandoned multi-agent "conductor" TS build plan (pnpm/biome/vitest/WebSocket). **Obsolete.** Real dev is ordinary test-first branches + PRs.
- ADRs `0001, 0009, 0010, 0011` are **superseded** by 0017; `0003, 0006, 0007, 0012, 0014, 0016` are **amended** by it. On any conflict, **ADR-0017 wins**, then the newer ADRs (0015 tier-cascade, 0018 management-console).

When something in prose contradicts the code, trust the code and the ADRs numbered ≥ 0015.

## Commands

```bash
uv sync                       # create .venv + install (deps + dev group)
uv run crate --help
uv run crate download "Daft Punk Get Lucky" -o ./downloads

uv run pytest                 # full suite; coverage gate --cov-fail-under=90 is enforced in pyproject
uv run pytest tests/application/test_cascade.py            # one file
uv run pytest tests/application/test_cascade.py::test_name  # one test
uv run pytest -k cascade                                   # by keyword

uv run ruff check .           # lint  (also enforces hexagonal layering, see below)
uv run ruff format .          # format
uv run mypy                   # types, strict
```

CI runs ruff + mypy + pytest on every PR. Development is **test-first**; coverage ≥ 90% is a hard gate.

## Architecture

Hexagonal / ports-and-adapters, single package `src/crate`, layers:

- `domain/` — pure, zero-IO. Types + rules only: `tiers.py` (`Tier` enum, `plan_cascade`), `audio.py` (`AudioInfo`, `classify` → `QualityVerdict`), `download.py` (`DownloadOutcome`), `matching.py`.
- `application/` — use-cases over domain. The real acquisition path is **cascade → tier-mux**:
  - `cascade.py::run_cascade` — asks each `TierMux` in strict `Tier` order; first tier to return a verified file wins. Optionally records the request + attempt trail via a `RequestRecorder`.
  - `multiplex.py::TierMux.acquire` — one tier is a **pool** of `SourceEntry` modules + a fan-out `MuxStrategy`: `ALL` (try every source) or `UNIFORM` (round-robin one per request, spreads rate-limit load). Downloads, probes, `classify`s; rejects a file if `expected_lossless` but the verdict isn't lossless, then advances.
  - `download.py::acquire` is an older single-source (Tier-3 only) helper; the cascade is the real entry. Its docstring saying "only Tier 3 is wired" is stale — composition wires all sources.
  - `bulk.py` (playlist/list resolution), `recording.py` (`RequestRecorder` protocol).
- `adapters/` — implement source/inbound behavior. `sources/{qobuz,monochrome,deezer,soulseek,telegram_bot,ytdlp}.py` are thin functions taking their IO collaborators **injected** (search/get_bytes/decrypt/clock…) for testability. `inbound/{slskd_shim,ops_api}.py` are the two HTTP surfaces.
- `infrastructure/` — concrete IO: `sqlite/` (hand-rolled migrations, `store.py` history, `config_store.py` encrypted secrets), `qobuz/ deezer/ soulseek/ spotify/ telegram/` clients, `browser/amz_session.py` (Playwright, lazy-imported), `audio/{probe,decrypt}.py`, `crypto.py` (`SecretBox`), `http.py`, `http_server/serve.py`.
- **Adding a source?** Follow [`docs/adding-a-source.md`](docs/adding-a-source.md) — the authoring contract (adapter/client split, `DownloadOutcome` metadata, catalog + wiring, tests).
- `composition/sources.py` — **the only place concrete adapters + infra get wired.** `build_tiers`, `build_recorder`, `build_shim_download`, `build_queue_worker`, `build_console_app`, Spotify OAuth. Sources are opt-in and self-configuring: each factory reads its creds from env and **raises if absent → that source is silently skipped** from the cascade.
- `cli/main.py` — Typer entry (`crate = crate.cli.main:app`). No business logic; parses args, calls use-cases.

**Layering is enforced by lint, not convention.** `ruff` `flake8-tidy-imports` bans `crate.adapters` / `crate.infrastructure` imports globally; `pyproject.toml` `per-file-ignores` lifts the ban only for the layers allowed to wire concretes (`application`, `adapters`, `infrastructure`, `composition`, `cli`). If you make `domain/` or `ports/` import outward, lint fails — that's the point. Ports are `typing.Protocol` classes.

## The three runnable surfaces

- `crate download <queries…>` — ad-hoc CLI acquisition. Env-only config; writes to `--out`. Records to history DB only if `CRATE_DB` is set.
- `crate serve` (default port 2416) — the **slskd-compatible shim** for Explo. This process owns the NAS mount + full toolchain. It also spawns N background **queue-worker** threads (`CRATE_BULK_CONCURRENCY`) that drain console-queued download rows from the shared DB via atomic `claim_next`. De-dups against already-completed tracks.
- `crate console` (default port 8788) — FastAPI **read-only** dashboard over the history DB (stats, request history, Spotify search/OAuth "listening" panel, bulk paste). The console only writes `queued` rows; `serve`'s workers do the actual downloading.

## Sources & tiers (opt-in via env)

| Tier | Sources | Enable |
|---|---|---|
| 1 (hi-res FLAC) | Qobuz (direct), Monochrome (Amazon, needs headed browser) | `CRATE_QOBUZ=1`, `CRATE_MONOCHROME=1` |
| 2 (lossless) | Deezer (ARL), Soulseek (via external slskd), Telegram bot | `CRATE_DEEZER=1`, `CRATE_SOULSEEK=1`, `CRATE_TELEGRAM=1` |
| 3 (lossy fallback) | yt-dlp | always on |

Per-source credential env vars are read by the factories in `composition/sources.py` (`CRATE_QOBUZ_APP_ID`, `CRATE_DEEZER_ARL`, `CRATE_SLSKD_URL`, `CRATE_TG_*`, etc.); see [`.crate.env.example`](.crate.env.example) and [`SECRETS.md`](SECRETS.md). When `CRATE_DB` is set, a config store can hold encrypted creds + a per-tier layout that overrides the env flags. Docker image ships headless sources only (Monochrome excluded).

## Conventions

- `uv` is the only env/dep tool; `pyproject.toml` is the single manifest. Don't add `requirements.txt` / `poetry` / `pip`.
- Adapters take collaborators as keyword-injected callables (hence `ruff` `max-args = 9`). Test them with fakes, not network — live integration is env-gated.
- Errors surface as exceptions at boundaries (no `Result` type — ADR-0017 reversed ADR-0006). Use-case boundaries like `TierMux.acquire` catch per-source failures and turn them into `CascadeAttempt(ok=False)` so one bad source never breaks the cascade.
- Optional deps (`playwright`, `telethon`) are lazy-imported inside the adapter that needs them; keep it that way.
- The console DB must be on local disk, never NFS (ADR-0018).
