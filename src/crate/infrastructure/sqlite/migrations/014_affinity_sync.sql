-- Library Sync (PR 6): listening-affinity tiers.
-- Tier 1 (core) = Liked Songs + your/followed playlists (from Spotify directly);
-- tier 2 (constant) and tier 3 (fling) are computed from the plays ledger.
-- Membership keeps history: entering stamps first_seen_at, leaving stamps left_at
-- (nothing is ever deleted from the library when a song leaves a tier).

CREATE TABLE IF NOT EXISTS affinity_prefs (
  user_id         TEXT PRIMARY KEY REFERENCES users(id),
  thresholds_json TEXT NOT NULL,     -- AffinityThresholds (validated + clamped on read)
  updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS affinity_settings (
  user_id    TEXT NOT NULL REFERENCES users(id),
  tier       INTEGER NOT NULL,       -- 1 core | 2 constant | 3 fling
  auto_sync  INTEGER NOT NULL DEFAULT 0,
  enabled_at TEXT,                   -- when auto-sync was last switched on
  PRIMARY KEY (user_id, tier)
);

CREATE TABLE IF NOT EXISTS affinity_members (
  user_id       TEXT NOT NULL REFERENCES users(id),
  tier          INTEGER NOT NULL,
  merge_key     TEXT NOT NULL,       -- tok:<primary artist + clean title>
  spotify_id    TEXT,                -- NULL for local files (never auto-downloaded)
  artist        TEXT,
  title         TEXT,
  score         REAL,                -- tier-specific rank (plays / burst size / list rank)
  approximate   INTEGER NOT NULL DEFAULT 0,  -- 1 = from top-tracks, before the export
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  left_at       TEXT,                -- set when the song leaves the tier
  stats_json    TEXT,
  PRIMARY KEY (user_id, tier, merge_key)
);
CREATE INDEX IF NOT EXISTS idx_affinity_active ON affinity_members(user_id, tier, left_at);

-- Playlist sync-state: one row per playlist in the user's Spotify library, plus
-- the pseudo-row spotify_id='liked' for Liked Songs. Replaces Phase D's
-- synced_* tables (never enabled in prod; 0 rows), which are dropped below.
CREATE TABLE IF NOT EXISTS sync_playlists (
  user_id          TEXT NOT NULL REFERENCES users(id),
  spotify_id       TEXT NOT NULL,       -- playlist id | 'liked'
  name             TEXT NOT NULL,
  owner_id         TEXT,
  owned            INTEGER NOT NULL DEFAULT 0,
  generated        INTEGER NOT NULL DEFAULT 0,  -- Spotify-made (Discover Weekly, mixes)
  image            TEXT,
  snapshot_id      TEXT,                -- Spotify's; a content hash for Liked Songs
  track_count      INTEGER NOT NULL DEFAULT 0,
  in_library_count INTEGER NOT NULL DEFAULT 0,
  sync_mode        TEXT NOT NULL DEFAULT 'inherit',  -- inherit | on | off
  last_checked_at  TEXT,
  last_changed_at  TEXT,
  removed_at       TEXT,                -- no longer in the user's Spotify library
  PRIMARY KEY (user_id, spotify_id)
);

CREATE TABLE IF NOT EXISTS sync_playlist_items (
  user_id    TEXT NOT NULL,
  spotify_id TEXT NOT NULL,
  position   INTEGER NOT NULL,
  track_key  TEXT NOT NULL,
  track_json TEXT NOT NULL,
  PRIMARY KEY (user_id, spotify_id, position)
);

DROP TABLE IF EXISTS synced_tracks;
DROP TABLE IF EXISTS synced_playlists;
