-- Phase D (continuous playlist sync): track a user's playlists and enqueue only
-- newly-added tracks on each poll. synced_playlists holds the subscription (one
-- per user+playlist); synced_tracks is the per-playlist dedupe ledger so a track
-- is enqueued once even across restarts. The watcher (serve process, behind
-- CRATE_PLAYLIST_SYNC=1) polls active rows, short-circuits on an unchanged
-- snapshot_id, diffs against the ledger, and enqueues the difference.

CREATE TABLE IF NOT EXISTS synced_playlists (
  id              TEXT PRIMARY KEY,
  user_id         TEXT NOT NULL REFERENCES users(id),
  spotify_ref     TEXT NOT NULL,            -- the playlist id
  name            TEXT NOT NULL,
  snapshot_id     TEXT,                     -- last seen Spotify snapshot; NULL until first poll
  active          INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT NOT NULL,
  last_checked_at TEXT,
  UNIQUE(user_id, spotify_ref)
);
CREATE INDEX IF NOT EXISTS idx_synced_active ON synced_playlists(active);

CREATE TABLE IF NOT EXISTS synced_tracks (
  synced_playlist_id TEXT NOT NULL REFERENCES synced_playlists(id) ON DELETE CASCADE,
  track_key          TEXT NOT NULL,         -- ISRC, else normalized artist+title tokens
  enqueued_at        TEXT NOT NULL,
  PRIMARY KEY (synced_playlist_id, track_key)
);
