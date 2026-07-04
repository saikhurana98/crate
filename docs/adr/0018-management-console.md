# ADR-0018: Management console — persistence + read-only observability (Phase 1)

- **Status:** accepted
- **Date:** 2026-07-04

## Context

lbmd ran fully stateless: the cascade computed a rich result per request
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
  (`LBMD_DB=/data/lbmd.db`), *not* on the NAS that holds the music. This
  contradicts ADR-0004's "DB on the NAS" note, which predates the multi-process
  console.
- **Recording via a port.** `run_cascade` takes an optional `RequestRecorder`
  (Protocol in `application/recording.py`): open a request before the first tier,
  record each attempt, finish with the winner's facts or fail with a reason. The
  SQLite `RequestStore` implements it; the composition root injects it. With no
  recorder the core behaves exactly as before — every existing test passes
  untouched. The shim path records `requested_via="explo"`, the CLI `"cli"`.
- **API:** FastAPI + uvicorn as a new `lbmd console` command on a separate port
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
- **Neutral:** Manual `lbmd download` only records when `LBMD_DB` is set, keeping
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

## References

- ADR-0004 (SQLite, hand-rolled migrations) — schema intent; superseded on file
  location by this ADR.
- ADR-0014 (slskd shim) — the write path that feeds the console.
- SQLite WAL: https://www.sqlite.org/wal.html — and its NFS caveat.
