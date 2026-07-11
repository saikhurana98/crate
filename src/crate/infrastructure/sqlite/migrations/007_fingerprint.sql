-- 007_fingerprint: review fingerprint guard (ADR-0019).
-- Two things: the advisory AcoustID hint cached on the request, and the
-- audio-similarity gate on review re-downloads. A review upgrade that fails the
-- gate parks the request at status='needs_confirm' (a free-text status value, no
-- schema change) carrying its measured similarity for the user to override.
-- All additions are nullable / defaulted, so this is back-compatible; the runner
-- records a version only on success and never re-applies, so it runs exactly once.

-- Advisory identity hint (canonical artist/title/MBID from a fingerprint lookup)
-- and the raw fingerprint cache. Deliberately no ISRC column — the spike measured
-- 0% ISRC resolution on the real library (ADR-0019).
ALTER TABLE requests ADD COLUMN acoustid_artist TEXT;
ALTER TABLE requests ADD COLUMN acoustid_title TEXT;
ALTER TABLE requests ADD COLUMN acoustid_mbid TEXT;
ALTER TABLE requests ADD COLUMN ref_fingerprint TEXT;

-- Last measured fingerprint similarity for this request's re-download: set on a
-- clean upgrade (>= threshold) and on a parked needs_confirm (< threshold) so the
-- review UI can render "matches 98%" / "doesn't match 42%".
ALTER TABLE requests ADD COLUMN match_similarity REAL;

-- The user's explicit "replace anyway" carried on the armed candidate; when 1 the
-- worker skips the gate for this re-download.
ALTER TABLE review_candidates ADD COLUMN force INTEGER NOT NULL DEFAULT 0;
