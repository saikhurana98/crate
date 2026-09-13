# ADR-0020: Background jobs engine + WebSocket job logs

- **Status:** Accepted (2026-09-14)
- **Amends:** ADR-0018 (management console): adds a WebSocket transport for job logs only.

## Context

Library Sync (every 2 h, plus "Run now") and Dedup (multi-hour library scans, file
purges) need durable background work that runs whether or not a browser is open,
survives restarts, and streams logs live to anyone watching.

## Decision

1. **Jobs run in `serve`**, the only process with `/library` read-write. A
   `jobs` / `job_events` pair in the shared WAL DB is the queue and the log.
   Runner threads are split into **lanes** (`sync`, `scan`, `mutate`), so a long
   scan never blocks the sync. Claims use the guarded-UPDATE pattern from
   `claim_next`. A partial unique index makes single-flight kinds race-safe.
   A DB-timed scheduler enqueues periodic kinds. A startup reaper fails
   interrupted jobs, except resumable kinds, which it re-queues. Retention
   deletes finished jobs.
2. **The console only enqueues and reads.** Every status transition is also an
   event row, so a live view only has to tail one table.
3. **Live logs use a WebSocket, `/api/ws/jobs`.** Each socket tails `job_events`
   by id cursor about once a second and pushes new rows. A client reconnects
   with `?since=<cursor>`. The transport between processes is still the shared DB
   (ADR-0018's "no cross-process bus" holds); WebSocket is only the browser
   push. Auth is the same session cookie or `X-API-Key` as HTTP. The upgrade
   requires same-origin (`Origin` host == `Host`), which guards against
   cross-site WebSocket hijacking. Lines tagged with a `user_id` reach only that
   user.
4. **Dependency:** `websockets` (uvicorn's WebSocket implementation). This was
   explicitly requested and has no lighter alternative.

## Consequences

- Each open socket costs one indexed query per second. That's fine at
  personal-tool scale. A shared hub is the upgrade path if viewers ever number
  in the dozens.
- Jobs survive console restarts entirely, and `serve` restarts via the reaper.
