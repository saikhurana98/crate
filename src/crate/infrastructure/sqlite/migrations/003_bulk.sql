-- Bulk downloads: a batch groups many queued requests (a resolved playlist or a
-- pasted list). Requests gain an optional batch_id; the queue worker skips
-- requests whose batch is paused or cancelled.
CREATE TABLE IF NOT EXISTS batches (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  origin     TEXT NOT NULL,              -- spotify | youtube | paste
  owner      TEXT NOT NULL DEFAULT '',
  total      INTEGER NOT NULL DEFAULT 0,
  paused     INTEGER NOT NULL DEFAULT 0,
  cancelled  INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

ALTER TABLE requests ADD COLUMN batch_id TEXT REFERENCES batches(id);
CREATE INDEX IF NOT EXISTS idx_requests_batch ON requests(batch_id);
