-- Library Sync auto-sync: the per-scope ledger of what auto-sync has queued
-- (request_id set) or baselined (request_id NULL, "only future additions"), and
-- a download priority so a sync backfill never delays a user's own clicks.
CREATE TABLE IF NOT EXISTS sync_ledger (
  user_id     TEXT NOT NULL REFERENCES users(id),
  scope       TEXT NOT NULL,     -- playlist:liked | playlist:<id> | affinity:2 | affinity:3
  track_key   TEXT NOT NULL,     -- merge key
  request_id  TEXT,              -- NULL = baseline, never queued
  enqueued_at TEXT NOT NULL,
  PRIMARY KEY (user_id, scope, track_key)
);
CREATE INDEX IF NOT EXISTS idx_sync_ledger_queued
  ON sync_ledger(user_id, track_key) WHERE request_id IS NOT NULL;

-- 0 = a user's own request, 1 = auto-sync. claim_next serves lower first within
-- the owner round-robin.
ALTER TABLE requests ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
