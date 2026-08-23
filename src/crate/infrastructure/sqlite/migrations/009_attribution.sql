-- Phase 2 (multi-user): attribute every request/batch to the user who asked for
-- it, and add the fair-scheduling ledger so one user's big import can't starve
-- others. Additive columns + one-shot backfill of legacy rows to the synthetic
-- 'service' user seeded in 008.

ALTER TABLE requests ADD COLUMN user_id TEXT REFERENCES users(id);
ALTER TABLE batches  ADD COLUMN user_id TEXT REFERENCES users(id);

CREATE INDEX IF NOT EXISTS idx_requests_user ON requests(user_id);
CREATE INDEX IF NOT EXISTS idx_batches_user  ON batches(user_id);

-- ADD COLUMN can't backfill inline; attribute all pre-existing rows to 'service'.
-- (batches.owner stays as-is — it holds the Spotify playlist display name, not a user.)
UPDATE requests SET user_id = 'service' WHERE user_id IS NULL;
UPDATE batches  SET user_id = 'service' WHERE user_id IS NULL;

-- Fair scheduling: last time each user's request was claimed. claim_next serves
-- the user whose last claim is oldest (round-robin across users), FIFO within a user.
CREATE TABLE IF NOT EXISTS owner_claims (
  user_id       TEXT PRIMARY KEY,
  last_claim_at TEXT NOT NULL
);
