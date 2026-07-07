-- 004_perf_indexes: composite indexes for the hot read paths (P0-B).
-- All DDL is idempotent (IF NOT EXISTS) so a partially-applied migration is safe
-- to re-run; the runner records the version only on success.
--
-- idx_requests_query_status  — find_completed (de-dup) filters requests(query,status)
--                              once per download; was a full scan.
-- idx_requests_status_created — stats()/claim_next()/list order by created_at within
--                              a status; covers the status-then-time access pattern.
-- idx_attempts_source_ok     — stats() aggregates attempts grouped by source, summing ok.
-- idx_batches_flags          — claim_next()'s NOT IN (paused OR cancelled) batches subquery.
CREATE INDEX IF NOT EXISTS idx_requests_query_status  ON requests(query, status);
CREATE INDEX IF NOT EXISTS idx_requests_status_created ON requests(status, created_at);
CREATE INDEX IF NOT EXISTS idx_attempts_source_ok     ON attempts(source, ok);
CREATE INDEX IF NOT EXISTS idx_batches_flags          ON batches(paused, cancelled);
