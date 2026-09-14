-- Deduplicate purge: a journal of every file moved to the library trash.
-- A purge does three things, in order:
--   1. renames the file into <root>/.crate-trash/<date>/<original path>. This is
--      always a rename on the same filesystem, never a copy + delete;
--   2. repoints download records at the kept copy;
--   3. marks the index row missing.
-- The journal row (pending -> trashed) makes each step recoverable. Restore
-- renames the file back; emptying the trash deletes it for good.
CREATE TABLE IF NOT EXISTS trash_items (
  id             TEXT PRIMARY KEY,
  file_id        TEXT NOT NULL,        -- library_files.id of the trashed copy
  keeper_file_id TEXT,                 -- the copy kept in its place
  group_id       TEXT,
  original_path  TEXT NOT NULL,        -- relative to the library root
  trash_path     TEXT NOT NULL,        -- relative to the library root
  size           INTEGER NOT NULL,
  state          TEXT NOT NULL,        -- pending | trashed | restored | purged | failed
  error          TEXT,
  job_id         TEXT,
  user_id        TEXT,
  created_at     TEXT NOT NULL,
  trashed_at     TEXT,
  restored_path  TEXT,
  restored_at    TEXT,
  purged_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_trash_items_state ON trash_items(state, trashed_at);
