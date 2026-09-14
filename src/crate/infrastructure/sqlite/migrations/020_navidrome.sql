-- Per-user Navidrome accounts + playlists mirrored into them.
-- A user links their own Navidrome login once on the Sync page. Importing a
-- Spotify playlist then also creates it in that user's Navidrome account, and
-- the navidrome_push job keeps it filled as its tracks finish downloading.

CREATE TABLE IF NOT EXISTS navidrome_accounts (
  user_id     TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  username    TEXT NOT NULL,
  password_ct BLOB NOT NULL,          -- SecretBox (AES-256-GCM); never returned by the API
  verified_at TEXT NOT NULL,          -- last successful ping with these credentials
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS navidrome_playlists (
  user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  spotify_id     TEXT NOT NULL,       -- Spotify playlist id | 'liked'
  name           TEXT NOT NULL,
  navidrome_id   TEXT,                -- NULL until the first push creates it
  enabled        INTEGER NOT NULL DEFAULT 1,
  matched        INTEGER NOT NULL DEFAULT 0,  -- songs in the Navidrome playlist
  in_library     INTEGER NOT NULL DEFAULT 0,  -- tracks crate has (the ceiling for matched)
  total          INTEGER NOT NULL DEFAULT 0,  -- tracks in the Spotify playlist
  last_pushed_at TEXT,
  error          TEXT,                -- the last push failure, if any
  created_at     TEXT NOT NULL,
  PRIMARY KEY (user_id, spotify_id)
);

-- merge key -> Navidrome song id. Song ids are global in Navidrome, so this is
-- shared by every user; entries are re-checked after a few days.
CREATE TABLE IF NOT EXISTS navidrome_songs (
  merge_key  TEXT PRIMARY KEY,
  song_id    TEXT NOT NULL,
  checked_at TEXT NOT NULL
);
