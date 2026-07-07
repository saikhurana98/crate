-- 005_track_identity: structured request identity (Phase 1).
-- Requests stop being a flat query string: capture the artist/title/ISRC/duration
-- that the edges (Spotify search, playlist resolvers, the shim) already know, so
-- sources can match on ISRC/duration and de-dup keys on identity, not text.
-- ADD COLUMN is not IF-NOT-EXISTS-able in SQLite, but the runner records a
-- version only on success and never re-applies it, so this runs exactly once.
ALTER TABLE requests ADD COLUMN artist     TEXT;
ALTER TABLE requests ADD COLUMN title      TEXT;
ALTER TABLE requests ADD COLUMN isrc       TEXT;
ALTER TABLE requests ADD COLUMN duration_s INTEGER;

-- ISRC de-dup lookup (find_completed): only rows that carry an ISRC are indexed.
CREATE INDEX IF NOT EXISTS idx_requests_isrc ON requests(isrc) WHERE isrc IS NOT NULL;
