-- Preserve the cover URL selected in Review/direct source search until the
-- separate queue-worker process downloads and finalizes the candidate.
ALTER TABLE review_candidates ADD COLUMN art_url TEXT;

