-- Library Sync (PR 3): the listening ledger tiers 2/3 are computed from.
-- `listen_poll` (every 30 min) appends recently-played plays; the one-time
-- Spotify streaming-history import (PR 5) backfills the rest as source='export'.

CREATE TABLE IF NOT EXISTS plays (
  user_id    TEXT NOT NULL REFERENCES users(id),
  played_at  TEXT NOT NULL,   -- UTC whole seconds 'YYYY-MM-DDTHH:MM:SSZ' (end of play)
  merge_key  TEXT NOT NULL,   -- tok:<primary artist + clean title>; merges relinked ids
  track_uri  TEXT,            -- spotify:track:<id>; NULL for local files
  ms_played  INTEGER,         -- export only; NULL for recently-played polls
  artist     TEXT,
  title      TEXT,
  album      TEXT,
  source     TEXT NOT NULL,   -- recent | export
  PRIMARY KEY (user_id, played_at, merge_key)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_plays_track ON plays(user_id, merge_key, played_at);

CREATE TABLE IF NOT EXISTS listen_cursors (
  user_id            TEXT PRIMARY KEY REFERENCES users(id),
  recent_after_ms    INTEGER,  -- recently-played `after` cursor (epoch ms)
  last_polled_at     TEXT,
  export_start       TEXT,     -- range covered by an imported streaming-history export
  export_end         TEXT,
  export_imported_at TEXT
);

-- Spotify track metadata cache shared by tiers, playlists and previews.
CREATE TABLE IF NOT EXISTS spotify_tracks (
  id         TEXT PRIMARY KEY,
  isrc       TEXT,
  title      TEXT,
  artist     TEXT,
  album      TEXT,
  duration_s INTEGER,
  art        TEXT,
  deezer_id  TEXT,             -- preview resolution cache (filled later)
  fetched_at TEXT NOT NULL
);

-- Top-track lists per run: the pre-export tier approximation, and over months a
-- real "dropped out of short-term" fling signal.
CREATE TABLE IF NOT EXISTS top_snapshots (
  user_id    TEXT NOT NULL REFERENCES users(id),
  time_range TEXT NOT NULL,    -- short_term | medium_term | long_term
  taken_at   TEXT NOT NULL,
  ids_json   TEXT NOT NULL,
  PRIMARY KEY (user_id, time_range, taken_at)
);
