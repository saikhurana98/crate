-- Phase C (shared feed): record the two latencies every request has — time to a
-- first playable file (the lossy rip that lands instantly) and time to the final
-- FLAC (the endgoal upgrade). These fall out of one store method: finish_request
-- already runs a second time when dedup._upgrade replaces the lossy file with a
-- lossless one, so first_playable_at (COALESCE-once) and final_flac_at (set on the
-- lossless landing) capture both ends of the split without a new call site.

ALTER TABLE requests ADD COLUMN first_playable_at TEXT;
ALTER TABLE requests ADD COLUMN final_flac_at TEXT;

-- Backfill historical rows: a completed request was playable at its finish time,
-- and if its stored download is lossless that same finish is also the final FLAC.
-- (Pre-Phase-C rows only ever finished once, so both collapse to finished_at.)
UPDATE requests SET first_playable_at = finished_at
  WHERE status = 'done' AND finished_at IS NOT NULL;
UPDATE requests SET final_flac_at = finished_at
  WHERE status = 'done' AND finished_at IS NOT NULL
    AND id IN (SELECT request_id FROM downloads WHERE lossless = 1);
