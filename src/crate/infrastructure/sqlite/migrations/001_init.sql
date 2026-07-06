-- 001_init: request/attempt/download history for the management console.
-- All DDL is idempotent (IF NOT EXISTS) so a partially-applied migration is
-- safe to re-run; the migration runner records the version only on success.

CREATE TABLE IF NOT EXISTS requests (
  id            TEXT PRIMARY KEY,
  query         TEXT NOT NULL,
  requested_via TEXT NOT NULL,            -- explo | cli | api
  status        TEXT NOT NULL,            -- downloading | done | failed
  tier_won      INTEGER,
  source_won    TEXT,
  error         TEXT,
  created_at    TEXT NOT NULL,
  finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_status  ON requests(status);
CREATE INDEX IF NOT EXISTS idx_requests_created ON requests(created_at);

CREATE TABLE IF NOT EXISTS attempts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id  TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
  source      TEXT NOT NULL,
  tier        INTEGER NOT NULL,
  ok          INTEGER NOT NULL,           -- 0/1
  detail      TEXT NOT NULL,              -- quality label (ok) or error (fail)
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_request ON attempts(request_id);
CREATE INDEX IF NOT EXISTS idx_attempts_source  ON attempts(source);

CREATE TABLE IF NOT EXISTS downloads (
  request_id    TEXT PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
  path          TEXT NOT NULL,
  size_bytes    INTEGER NOT NULL,
  codec         TEXT NOT NULL,
  bitrate       INTEGER,
  bit_depth     INTEGER,
  sample_rate   INTEGER,
  duration_s    REAL,
  elapsed_s     REAL,
  lossless      INTEGER NOT NULL,         -- 0/1
  quality_label TEXT NOT NULL
);
