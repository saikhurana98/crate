-- 006_review: user-assisted retry / re-download (the Review tab).
-- Two things land in the review list: failed requests, and `done` rows whose
-- download is lossy (lossless=0) and could be upgraded to FLAC. `dismissed`
-- lets the user remove an item from that list without changing its status.
-- ADD COLUMN is not IF-NOT-EXISTS-able in SQLite, but the runner records a
-- version only on success and never re-applies it, so this runs exactly once.
ALTER TABLE requests ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0;

-- Partial index for the review query: it only ever scans non-dismissed rows
-- that are failed or done, ordered newest-first.
CREATE INDEX IF NOT EXISTS idx_requests_review
  ON requests(status, created_at) WHERE dismissed = 0;

-- The chosen candidate for a targeted re-download, keyed by the request it
-- re-runs. The console writes this row + flips the request back to `queued`; the
-- serve queue worker reads it, downloads exactly this candidate (skipping the
-- cascade), then upgrades/fulfils. `is_upgrade` + `target_path` carry the lossy
-- file to replace in place; a failed request leaves them null.
CREATE TABLE IF NOT EXISTS review_candidates (
  request_id    TEXT PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
  source        TEXT NOT NULL,
  tier          INTEGER NOT NULL DEFAULT 2,
  ref           TEXT NOT NULL,
  title         TEXT NOT NULL,
  artist        TEXT NOT NULL,
  album         TEXT,
  quality_label TEXT NOT NULL DEFAULT '',
  lossless      INTEGER NOT NULL DEFAULT 0,
  duration_s    INTEGER,
  size_bytes    INTEGER,
  is_upgrade    INTEGER NOT NULL DEFAULT 0,
  target_path   TEXT,
  created_at    TEXT NOT NULL
);
