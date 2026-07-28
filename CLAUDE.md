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

- `domain/` — pure, zero-IO. Types + rules only: `tiers.py` (`Tier` enum, `plan_cascade`), `audio.py` (`AudioInfo`, `classify` → `QualityVerdict`), `download.py` (`DownloadOutcome` — path + tier/source + the metadata the source knew: `artist/album/title/track_no/disc_no` and `art_url`), `identity.py` (`TrackIdentity` — the wanted `artist/title/isrc/duration_s`, with `from_query`), `library.py` (pure `<Artist>/<Album>/<NN Title>.<ext>` path rules + sanitising), `matching.py` (`title_matches`, `artist_matches`, `match_score`, `norm_tokens`).
- `application/` — use-cases over domain. The real acquisition path is **cascade → tier-mux**:
  - `cascade.py::run_cascade` — asks each `TierMux` in strict `Tier` order; first tier to return a verified file wins. Takes `want: TrackIdentity | None` (threaded to sources) and an optional `finalizer`; records the request + attempt trail via a `RequestRecorder`.
  - `multiplex.py::TierMux.acquire` — one tier is a **pool** of `SourceEntry` modules + a fan-out `MuxStrategy`: `ALL` (try every source) or `UNIFORM` (round-robin one per request). Downloads, probes, `classify`s; rejects if `expected_lossless` but not lossless, and rejects on **post-download duration mismatch** vs `want` (>7 s), then advances.
  - `librarian.py::make_finalizer` — the per-track finalize (runs when `CRATE_LIBRARY_ROOT` is set): merge source metadata over the file's own tags → write tags → **fetch + embed the source's cover art + write `cover.jpg`** → **atomic move** into `<root>/<Artist>/<Album>/<NN Title>.<ext>`. Turns raw downloads into an organized, tagged, art-complete library the instant each track lands.
  - `dedup.py::resolve_request` — identity de-dup + **lossless upgrade**: reuse an existing file only if it matches (ISRC → normalized artist+title) AND is lossless; a lossy match re-runs Tiers 1–2 and atomically replaces the file on a lossless win (keeps the lossy one on failure).
  - `bulk.py` (playlist/list → `TrackIdentity[]`), `recording.py` (`RequestRecorder` protocol), `download.py::acquire` (older single-source helper — cascade is the real entry).
- `adapters/` — implement source/inbound behavior. `sources/{qobuz,monochrome,deezer,soulseek,telegram_bot,ytdlp,arcod}.py` are thin functions taking their IO collaborators **injected** (search/fetch/decrypt/poll/clock…) for testability; each returns a `DownloadOutcome`. `inbound/{slskd_shim,ops_api}.py` are the two HTTP surfaces.
- `infrastructure/` — concrete IO: `sqlite/` (hand-rolled migrations `001`–`005`, `store.py` history + 2 s stats cache + retention, `config_store.py` encrypted secrets + tier layout), `qobuz/ deezer/ soulseek/ spotify/ telegram/ arcod/` clients, `browser/amz_session.py` (Playwright, lazy-imported), `audio/{probe,decrypt,tags}.py` (`tags.py` = mutagen read/write + `embed_cover`), `crypto.py` (`SecretBox`), `http.py` (`stream_to` streams downloads to disk — no whole-file buffers), `http_server/serve.py`.

### Track identity & selection (why the right song wins)
Requests carry a `TrackIdentity` (from Spotify search/playlist: artist/title/ISRC/duration). Every selecting adapter applies gates in this order: **ISRC exact match → `title_matches` → `artist_matches` → duration prefilter (±7 s) → `match_score` rank**. ISRC short-circuits; the artist gate rejects a right-title/wrong-artist track (the "Alfaaz → Alka Yagnik" class). yt-dlp uses `ytsearch5:` + scored selection (not the first hit). This whole stack is the accuracy core — do not weaken a gate without a regression test.
- **Adding a source?** Follow [`docs/adding-a-source.md`](docs/adding-a-source.md) — the authoring contract (adapter/client split, `DownloadOutcome` metadata, catalog + wiring, tests).
- `composition/sources.py` — **the only place concrete adapters + infra get wired.** `build_tiers`, `build_recorder`, `build_shim_download`, `build_queue_worker`, `build_console_app`, Spotify OAuth. Sources are opt-in and self-configuring: each factory reads its creds from env and **raises if absent → that source is silently skipped** from the cascade.
- `cli/main.py` — Typer entry (`crate = crate.cli.main:app`). No business logic; parses args, calls use-cases.

**Layering is enforced by lint, not convention.** `ruff` `flake8-tidy-imports` bans `crate.adapters` / `crate.infrastructure` imports globally; `pyproject.toml` `per-file-ignores` lifts the ban only for the layers allowed to wire concretes (`application`, `adapters`, `infrastructure`, `composition`, `cli`). If you make `domain/` or `ports/` import outward, lint fails — that's the point. Ports are `typing.Protocol` classes.

## The three runnable surfaces

- `crate download <queries…>` — ad-hoc CLI acquisition. Env-only config; writes to `--out`. Records to history DB only if `CRATE_DB` is set. `--source <name>` replaces the cascade with that **single** source (even one disabled in the stored layout) so a failure is attributable instead of masked by a lower tier taking over — the CLI half of "which source is broken?".
- `crate serve` (default port 2416) — the **slskd-compatible shim** for Explo. Owns the NAS mount + full toolchain. On startup: `reap_orphans` (requeue rows a prior crash left `downloading`) + `prune_attempts` + a daily maintenance thread. Spawns N **queue-worker** threads (`CRATE_BULK_CONCURRENCY`) that drain console-queued rows via atomic `claim_next`; de-dups + upgrades via `dedup.py`. When `CRATE_LIBRARY_ROOT` is set, each winner is finalized into the library (organize + tag + cover art).
- `crate console` (default port 8788) — FastAPI dashboard over the history DB (stats, history, Spotify search/OAuth "listening", bulk paste). Reads are served from a **2 s-cached stats frame** so N SSE viewers cost one recompute. The console only **writes `queued` rows** (+ config/layout); `serve`'s workers do the downloading.

## Sources & tiers (opt-in via env)

| Tier | Sources | Enable |
|---|---|---|
| 1 (hi-res FLAC) | Qobuz (direct), Monochrome (Amazon, needs headed browser) | `CRATE_QOBUZ=1`, `CRATE_MONOCHROME=1` |
| 2 (lossless-ish) | Deezer (ARL), Soulseek (via external slskd), Telegram bot, **arcod** (Qobuz proxy, no creds — may serve MP3, so not lossless-gated) | `CRATE_DEEZER=1`, `CRATE_SOULSEEK=1`, `CRATE_TELEGRAM=1`, `CRATE_ARCOD=1` |
| 3 (lossy fallback) | yt-dlp | always on |

Per-source credential env vars are read by the factories in `composition/sources.py`; see [`.crate.env.example`](.crate.env.example) and [`SECRETS.md`](SECRETS.md). **Important:** when `CRATE_DB` is set the **stored config-store tier layout wins over the env flags** — enabling a source needs it added to that layout (env flags only seed the *first* run), so a brand-new source won't appear just from its `CRATE_*=1` flag on an existing deployment. The Docker image **bundles Chromium + Xvfb + mp4decrypt**, so Monochrome works in-container (it runs headed under a virtual display).

### Library, staging & art (env)
- `CRATE_SERVE_DOWNLOADS` — staging dir downloads land in first.
- `CRATE_LIBRARY_ROOT` — when set, turns on the finalizer: each track is organized + tagged + art-embedded and **atomically moved** here as `<Artist>/<Album>/<NN Title>.<ext>` + `cover.jpg`. Keep it on the **same filesystem** as staging so the move is a rename (ADR-0008). Unset → files stay in staging (legacy batch behaviour).
- Other runtime knobs: `CRATE_ARCOD_BASE`, `CRATE_QUEUE_POLL`, `CRATE_ATTEMPTS_RETENTION_DAYS` (default 90), `CRATE_MAINTENANCE_INTERVAL`, `CRATE_MUX_TIER{1,2,3}` (`all`/`uniform`).

## Conventions

- `uv` is the only env/dep tool; `pyproject.toml` is the single manifest. Don't add `requirements.txt` / `poetry` / `pip`.
- Adapters take collaborators as keyword-injected callables (hence `ruff` `max-args = 9`). Test them with fakes, not network — live integration is env-gated.
- Errors surface as exceptions at boundaries (no `Result` type — ADR-0017 reversed ADR-0006). Use-case boundaries like `TierMux.acquire` catch per-source failures and turn them into `CascadeAttempt(ok=False)` so one bad source never breaks the cascade.
- Optional deps (`playwright`, `telethon`) are lazy-imported inside the adapter that needs them; keep it that way. Keep the stack lean — no new runtime dependency without a strong reason (current runtime deps: typer, httpx, pydantic, fastapi, uvicorn, pycryptodome, mutagen).
- **Stream, never buffer** whole audio files — downloads go to disk via `http.stream_to` / client `fetch`; Deezer decrypts stripe-by-stripe file→file. No `resp.content` on an audio payload.
- **Every subprocess / network / bot / browser wait has a bounded deadline** that raises (so a hung source advances the cascade instead of killing a queue worker). Preserve this when touching probe/decrypt/ytdlp/telegram/browser/arcod-poll.
- The console DB must be on local disk, never NFS (ADR-0018).

## Roadmap & recent work

`docs/roadmap/` holds executable phase specs; `docs/roadmap-perf-accuracy.md` is the audit that drove them. **Shipped:** Phase 0 (streaming, DB indexes/stats-cache/retention, timeouts, scored yt-dlp), Phase 1 (track identity — ISRC/duration/artist gates, identity dedup, lossless upgrades), the artist gate, the arcod source, Phase 4's cover-art embedding. **Deferred:** Phase 2 (multi-user), Phase 3 (dep slimming / image split / UI polish), Phase 4's chromaprint + fake-FLAC (too heavy for the gain). Follow the phase docs when resuming — each is one PR series behind the CI gate.
