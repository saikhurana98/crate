-- Library Sync + Dedup (PR 2): a small background-jobs engine.
-- Jobs run in `serve` (the only process with /library read-write, and the one
-- that runs with no browser open). The console only inserts `queued` rows and
-- reads. Every status change is also an event, so a live log (WebSocket hub,
-- next PR) only ever tails `job_events` by id.

CREATE TABLE IF NOT EXISTS jobs (
  id               TEXT PRIMARY KEY,
  kind             TEXT NOT NULL,            -- library_sync | listen_poll | dedup_scan | ...
  lane             TEXT NOT NULL,            -- sync | scan | mutate (one runner thread each)
  status           TEXT NOT NULL,            -- queued | running | done | failed | cancelled
  triggered_by     TEXT NOT NULL,            -- schedule | manual | system
  user_id          TEXT REFERENCES users(id),-- who triggered it (NULL = scheduler)
  dedupe_key       TEXT,                     -- single-flight key; NULL = any number may run
  params_json      TEXT NOT NULL DEFAULT '{}',
  summary_json     TEXT,
  error            TEXT,
  progress_done    INTEGER,
  progress_total   INTEGER,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL,
  started_at       TEXT,
  finished_at      TEXT,
  heartbeat_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(lane, status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_kind  ON jobs(kind, created_at);
-- At most one queued-or-running job per single-flight kind: a manual "Run now"
-- racing the scheduler gets the existing job instead of a second one.
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_active ON jobs(dedupe_key)
  WHERE status IN ('queued', 'running') AND dedupe_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS job_events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,  -- the live-log cursor
  job_id    TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  ts        TEXT NOT NULL,
  level     TEXT NOT NULL,                      -- info | warn | error | status | progress
  code      TEXT,                               -- machine tag: queued/started/finished/...
  message   TEXT NOT NULL,
  user_id   TEXT,                               -- per-user event (only that user sees it); NULL = global
  data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, id);
