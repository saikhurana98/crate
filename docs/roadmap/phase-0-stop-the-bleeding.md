# Phase 0 — Stop the bleeding

Goal: remove the OOM class, the hung-worker class, and the O(history) dashboard
cost. No schema-visible behavior changes, no new runtime deps. Three
file-disjoint work packages (P0-A/B/C) that can be built in parallel.

## P0-A · Streaming downloads (memory)

**Problem:** `resp.content` buffers whole FLACs (A1); Deezer holds encrypted +
decrypted copies (A2); Monochrome re-reads the whole file for a 16-byte KID (A3).

**Solution**
1. `infrastructure/http.py`: add `stream_to(url, dest: Path, *, timeout=120.0) -> int`
   — `httpx.stream("GET", …)` + 64 KiB chunk writes, returns bytes written.
   Keep `get_json`; delete/deprecate module-level `get_bytes`.
2. Qobuz: client grows `fetch(url, dest) -> None` (streamed); adapter's injected
   callable becomes `fetch: Callable[[str, Path], None]`; `qobuz_download`
   streams straight to the destination file.
3. Deezer: client `fetch(url, dest)` streams the encrypted payload to
   `<dest>.enc.part`; new pure `decrypt_stream(src: Path, dst: Path, key: bytes)`
   in `adapters/sources/deezer.py` processes 2048-byte stripes file→file
   (`index % 3 == 0 and len == 2048` → Blowfish-CBC decrypt, else passthrough —
   identical rule to `decrypt_track`, which stays for the pure tests and is
   reimplemented on top of the stream version). Temp file unlinked in `finally`.
4. Monochrome: `parse_kid` gets a windowed reader — read 1 MiB, scan for `tenc`,
   extend window up to 8 MiB before failing; never `read_bytes()` the whole file.
5. Rewire `composition/sources.py` factories to the new `fetch` signatures.

**Acceptance:** no code path holds a full audio payload in a `bytes`; peak
per-worker RAM independent of file size; all adapter tests use tmp files.

## P0-B · DB indexes, retention, stats cache (performance)

**Problem:** B2/B3/B4/A4 — full scans per download + per SSE tick per client;
unbounded `attempts` and shim `_transfers`.

**Solution**
1. Migration `004_perf_indexes.sql`:
   `CREATE INDEX idx_requests_query_status ON requests(query, status);`
   `CREATE INDEX idx_requests_status_created ON requests(status, created_at);`
   `CREATE INDEX idx_attempts_source_ok ON attempts(source, ok);`
   `CREATE INDEX idx_batches_flags ON batches(paused, cancelled);`
2. `RequestStore.stats()` cache: `(frame, monotonic ts)` under the existing
   lock, TTL 2.0 s; every write method (`open_request/enqueue/claim_next/
   record_attempt/finish_request/fail_request/requeue/reap_orphans/
   complete_as_duplicate/cancel_batch`) clears it. All SSE clients + `/stats`
   then share one recompute per 2 s.
3. `RequestStore.prune_attempts(older_than_days=90) -> int`; called from `serve`
   startup (after `reap_orphans`) and then daily from a maintenance daemon
   thread (plain `threading.Timer` loop obeying the stop event).
4. `ShimState`: prune `_transfers` on insert — keep at most 200 entries; drop
   oldest `Completed, *` first (never drop in-flight). Add `finished_at`
   monotonic stamp to records.

**Acceptance:** `stats()` recomputes ≤1× per 2 s regardless of viewer count;
`find_completed`/`claim_next` hit indexes (verify with `EXPLAIN QUERY PLAN` in a
test); attempts prune covered by tests; shim dict bounded.

## P0-C · Timeouts + yt-dlp verification (robustness + accuracy)

**Problem:** B6 (ffprobe/mp4decrypt/yt-dlp/Telegram/Turnstile can hang a worker
forever) and C1 (`ytsearch1:` = first YouTube hit, unverified).

**Solution**
1. `infrastructure/audio/probe.py`: `subprocess.run(..., timeout=30)`; map
   `TimeoutExpired` → `RuntimeError("ffprobe timed out")`.
2. `infrastructure/audio/decrypt.py`: `timeout=120` same pattern.
3. `adapters/sources/ytdlp.py`: two-step —
   a. probe search: `yt-dlp "ytsearch5:<query>" --skip-download --no-warnings
      --print "%(id)s\t%(title)s\t%(channel)s\t%(duration)s"` (timeout 60 s);
   b. score candidates with `domain.matching.match_score(title, channel, query)`;
      drop candidates whose title fails `title_matches`; prefer duration within
      ±7 s of `expected_duration_s` when provided (new optional kwarg, default
      None — Phase 1 will feed it);
   c. download only the winner by video id (timeout 600 s, `proc.kill()` on
      expiry). If the probe step fails entirely, fall back to today's
      single-shot `ytsearch1:` (with the 600 s timeout) so Tier 3 never goes
      dark.
4. `infrastructure/telegram/telethon_client.py`: overall deadline
   `request_timeout_s=90` around the send/collect loop → `TimeoutError`.
5. `infrastructure/browser/amz_session.py`: outer deadline on `_ensure_jwt`
   (60 s) + explicit `timeout=` on `page.evaluate` calls.

**Acceptance:** every subprocess/network wait has a bounded deadline that
raises (cascade advances, worker survives); ytdlp picks a scored candidate —
test: 5 fake candidates, junk "Full Video Song" first, right artist second →
second wins; fallback path tested.

## Shared constraints (all packages)

- Gate: `uv run pytest` (≥90 % cov), `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy` — all green.
- Hexagonal layering stays (ruff-enforced); adapters keep injected-callable
  style; no new runtime dependencies.
- One commit per package, conventional-commit message explaining the why.
