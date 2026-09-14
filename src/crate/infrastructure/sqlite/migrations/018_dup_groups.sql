-- Deduplicate grouping:
--   - dup_edges: evidence that two library files are the same recording;
--   - dup_ignores: pairs a person marked "intentionally different";
--   - dup_groups / dup_members: the duplicate groups, rebuilt from the edges
--     after every scan, each member ranked (rank 0 = the copy to keep).
CREATE TABLE IF NOT EXISTS dup_edges (
  file_a TEXT NOT NULL,              -- always file_a < file_b
  file_b TEXT NOT NULL,
  kind   TEXT NOT NULL,              -- exact | acoustid | fp
  score  REAL NOT NULL,
  PRIMARY KEY (file_a, file_b, kind)
);
CREATE INDEX IF NOT EXISTS idx_dup_edges_b ON dup_edges(file_b);

CREATE TABLE IF NOT EXISTS dup_ignores (
  file_a     TEXT NOT NULL,          -- always file_a < file_b
  file_b     TEXT NOT NULL,
  user_id    TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (file_a, file_b)
);

CREATE TABLE IF NOT EXISTS dup_groups (
  id                TEXT PRIMARY KEY,   -- signature: hash of the sorted member ids
  reason            TEXT NOT NULL,      -- strongest evidence: exact | acoustid | fp
  confidence        REAL NOT NULL,
  variant           INTEGER NOT NULL DEFAULT 0,  -- lengths differ > 10 s: edit / live / extended
  keeper_file_id    TEXT NOT NULL,
  reclaimable_bytes INTEGER NOT NULL DEFAULT 0,  -- 0 for variants (never suggested)
  created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dup_members (
  group_id        TEXT NOT NULL REFERENCES dup_groups(id) ON DELETE CASCADE,
  file_id         TEXT NOT NULL,
  rank            INTEGER NOT NULL,     -- 0 = keeper
  score_vs_keeper REAL,                 -- fingerprint similarity to the keeper
  reasons_json    TEXT NOT NULL,        -- why the keeper beats this copy
  facts_json      TEXT NOT NULL,        -- the ranking inputs (tags, cover, canonical, referenced)
  PRIMARY KEY (group_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_dup_members_file ON dup_members(file_id);

CREATE TABLE IF NOT EXISTS dedup_state (
  key   TEXT PRIMARY KEY,               -- e.g. last_grouped_at
  value TEXT
);
