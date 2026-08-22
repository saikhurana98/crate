-- Phase 2 (multi-user): users + sessions for Login-with-Spotify (ADR-0018 v5).
-- Identity is the Spotify account; there are no passwords. A session is a random
-- token whose sha256 is stored here — the raw token lives only in the browser
-- cookie, so a DB leak never yields a usable session.

CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,            -- app user id (uuid hex)
  spotify_id    TEXT UNIQUE,                 -- Spotify user id from /v1/me (login key)
  display_name  TEXT NOT NULL DEFAULT '',
  email         TEXT,                        -- from /v1/me (scope user-read-email)
  avatar_url    TEXT,
  is_admin      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  last_seen_at  TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash  TEXT PRIMARY KEY,              -- sha256(raw cookie token); raw never stored
  user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL                  -- ISO-8601 UTC; string compare vs now_iso()
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- Synthetic service/legacy user so pre-existing requests/batches attribute cleanly
-- once Phase B backfills their user_id. Never logs in (spotify_id NULL).
INSERT OR IGNORE INTO users(id, spotify_id, display_name, is_admin, created_at)
  VALUES ('service', NULL, 'legacy/service', 0, '1970-01-01T00:00:00+00:00');
