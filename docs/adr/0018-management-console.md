# ADR-0018: Management console — persistence + read-only observability (Phase 1)

- **Status:** accepted
- **Date:** 2026-07-04

## Context

crate ran fully stateless: the cascade computed a rich result per request
(`CascadeResult`, `DownloadReport`, `AudioInfo`, `QualityVerdict`) and then threw
it away. There was no way to see what was downloading, what had been downloaded,
how each track was acquired (which tier/source won, at what quality), or how the
sources were performing. The operator wanted a dashboard: live queue, historical
record, per-source statistics, and — later — drag-and-drop tier configuration and
credential editing.

This ADR covers **Phase 1 (observability, read-only)**. Config control (Phase 2)
and encrypted credential editing (Phase 3) are deliberately deferred; the schema
and source catalog here are shaped to accept them without rework.

## Decision

- **Persistence:** SQLite (stdlib `sqlite3`, no ORM), finally realising ADR-0004
  in Python. WAL mode, `foreign_keys=ON`, `busy_timeout=5000`. A ~40-line
  migration runner applies idempotent `migrations/NNN_*.sql` files and records
  applied versions in `schema_migrations`. Schema: `requests`, `attempts`,
  `downloads`.
- **DB location — local disk, never NFS.** The write path (shim) and the read
  path (console) are separate processes sharing one DB file; WAL gives them
  read-during-write concurrency, but only on a real local filesystem. SQLite
  locking over NFS is unreliable, so the file lives on CT-local disk
  (`CRATE_DB=/data/crate.db`), *not* on the NAS that holds the music. This
  contradicts ADR-0004's "DB on the NAS" note, which predates the multi-process
  console.
- **Recording via a port.** `run_cascade` takes an optional `RequestRecorder`
  (Protocol in `application/recording.py`): open a request before the first tier,
  record each attempt, finish with the winner's facts or fail with a reason. The
  SQLite `RequestStore` implements it; the composition root injects it. With no
  recorder the core behaves exactly as before — every existing test passes
  untouched. The shim path records `requested_via="explo"`, the CLI `"cli"`.
- **API:** FastAPI + uvicorn as a new `crate console` command on a separate port
  (`:8788`), independent of the slskd shim (`:2416`). Read-only routes:
  `/api/stats`, `/api/requests`, `/api/requests/{id}`, `/api/sources`,
  `/api/events` (SSE), `/api/health`. FastAPI (vs. hand-rolling on the stdlib
  server used by the shim) was chosen for the many-route + SSE surface.
- **Live updates by DB polling, not an event bus.** The console is a *separate
  reader process* from the shim writer, so an in-process pub/sub cannot reach it.
  `/api/events` polls the DB every ~2s and pushes a compact SSE frame. Simple and
  correct across processes.
- **Auth:** an `X-API-Key` header, injected by the reverse proxy (Caddy) so the
  browser never holds the key; `/api/health` is exempt for probes.
- **UI:** vanilla HTML + TypeScript, built with Vite into static assets, served
  by the API's `StaticFiles`. No framework. Tabs: Overview (stat cards + hand-
  rolled SVG bars), Live (SSE), History (searchable, paginated, drill-in drawer
  showing the full cascade trail + winner facts).
- **Source catalog:** `composition/catalog.py` declares each source's tier,
  enable flag, and *required* env vars, and reports credential **status**
  (`ok|partial|missing|none`) — presence only, **never values**.

## Consequences

- **Positive:** The data the cascade already produced is now durable and
  queryable. Live + historical views and per-source reliability come essentially
  for free from three small tables. The recorder port keeps the domain/cascade
  pure and testable (a spy recorder in tests, SQLite in prod). The console is
  isolated from the shim — a crash or restart of one does not affect the other.
- **Negative / accepted:** Two processes share a SQLite file, which mandates
  local disk and rules out putting the DB on the NAS. Live updates carry up to
  ~2s latency (poll interval) and are coarse — per-request, not per-source-byte.
  Adding FastAPI/uvicorn grows the image and dependency surface.
- **Neutral:** Manual `crate download` only records when `CRATE_DB` is set, keeping
  ad-hoc local runs side-effect-free.

## Alternatives considered

- **In-process event bus for live updates:** cleaner push semantics, but the
  console and shim are different processes — a bus cannot span them without a
  broker. DB polling is adequate at this scale.
- **Hand-roll the API on the existing stdlib server:** avoids a dependency but
  makes routing, query validation, and SSE tedious. FastAPI earns its keep here.
- **A UI framework (React/Svelte/Solid):** unnecessary for three read-only views;
  vanilla TS keeps the bundle ~10 kB and the toolchain minimal. (Phase 2's drag-
  and-drop tier board can use the native HTML5 DnD API or a tiny lib.)
- **DB on the NAS (per ADR-0004):** rejected — NFS + SQLite locking + two
  processes is a corruption risk.

## Update (2026-07-04): Phase 2 delivered

The read-only Phase 1 was superseded by the v2 design build:

- **Config endpoints** `GET/PUT /api/config` (tier layout + strategy) and
  `PUT /api/sources/{name}` (enabled + settings + credentials), backed by
  `ConfigStore` (migration `002_config.sql`: `app_config` with a version counter,
  `source_config`, `source_secret`). Layout validation (`validate_layout`) enforces
  full coverage and pins `ytdlp` to Tier 3.
- **Credentials encrypted at rest** — `SecretBox` (AES-256-GCM, pycryptodome), key
  from `CRATE_CONFIG_KEY` or a generated 0600 keyfile beside the DB. Status is
  reported (`ok`/`partial`/`missing`), values never returned.
- **Cascade consumes the stored config** — `build_tiers()` reads the layout
  (membership/order/strategy) and pushes stored secrets into the env for the
  source factories; the shim rebuilds tiers when the config `version` changes
  (true hot-reload, still cross-process via the DB, no restart).
- **UI** is now a single-screen app (vanilla TS + Vite, Space Grotesk / JetBrains
  Mono): the **Cascade map** (drag-and-drop tier board with strategy toggles and a
  disabled tray) plus a Live / Wins / History rail, a history detail drawer (trail
  timeline + winner facts + synthetic download log), and a source config panel.

Deferred: credential "Test/validate" button (stubbed in the UI), per-byte live
progress.

## Update (2026-07-04): search & download from the console

The console can now originate downloads, not just observe them.

- **Search** — `GET /api/search?q=` proxies the Spotify Web API
  (`infrastructure/spotify/client.py`, client-credentials flow). The bearer token
  is cached until ~30 s before expiry and the endpoint keeps a short-TTL
  (20 s / 128-entry) result cache, so debounced/repeated queries skip the network;
  the UI debounces 220 ms and aborts superseded requests (`AbortController`).
  Spotify is **search-only** — never a download source. Absent creds
  (`CRATE_SPOTIFY_CLIENT_ID/_SECRET`) => 503 and the UI hides search.
- **Download** — `POST /api/download {query}` inserts a `queued` request row and
  returns immediately (snappy). Downloading stays in one process: the `serve`
  (shim) process — which owns the NAS mount and full source toolchain — runs a
  background **queue worker** that atomically claims the oldest `queued` row
  (`RequestStore.claim_next`, `queued → downloading`) and runs the cascade bound to
  that existing row (`run_cascade(..., request_id=…)`, so no duplicate request).
  Cross-process handoff is via the shared WAL DB, consistent with the config
  hot-reload path — no cross-process bus.
- The `requests.status` enum gains `queued`; `stats()` reports a `queued` count and
  the History list shows a "Queued" state until the worker picks it up.

## References

- ADR-0004 (SQLite, hand-rolled migrations) — schema intent; superseded on file
  location by this ADR.
- ADR-0014 (slskd shim) — the write path that feeds the console.
- SQLite WAL: https://www.sqlite.org/wal.html — and its NFS caveat.
