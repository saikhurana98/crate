# Roadmap: performance, accuracy, multi-user readiness

Full-codebase audit (2026-07-07, code at `690c20b`) + phased fix plan. Goals, in
the owner's words: no memory loss, no performance hit, shallow dependency tree,
lightweight yet extremely fluid and great to look at — and ready to go
multi-user.

Every finding below carries file:line evidence. Phases are ordered by
risk-per-effort; each phase is independently shippable behind the existing CI
gate (pytest ≥90% / ruff / mypy).

---

## Audit findings

### A. Memory (the "memory loss" problem)

| # | Finding | Evidence | Impact |
|---|---------|----------|--------|
| A1 | **Whole audio files buffered in RAM.** Every HTTP source downloads via `resp.content` — the full FLAC (50–200 MB hi-res) as one `bytes`. | `infrastructure/http.py:21`, `qobuz/client.py:62-65`, `deezer/client.py:77-80` | 4 workers × 200 MB = 800 MB spikes |
| A2 | **Deezer doubles it**: whole encrypted file + whole decrypted copy live simultaneously; `decrypt_track` builds a third growing `bytearray`. | `adapters/sources/deezer.py:111-112,55-65` | 2× file per download → OOM class |
| A3 | Monochrome writes the encrypted MP4 to disk, then **reads the whole file back** to extract a 16-byte KID. | `adapters/sources/monochrome.py:180-181` | +1 full-file read per track |
| A4 | `ShimState._transfers` dict never pruned — every download ever made stays in memory. | `adapters/inbound/slskd_shim.py:35,104` | Slow leak in `serve` |
| A5 | Playwright browser + Telethon client held for process lifetime, never recycled; page state accumulates across Turnstile challenges. | `browser/amz_session.py:29-60`, `telegram/telethon_client.py:33-37` | Creeping RSS in long-lived serve |
| A6 | Console UI: `state.inflight[]`, `state.enqueued{}`, `state.batchDetails{}` grow unbounded in long-lived tabs. | `apps/console/src/app.ts:100,120,162` | Week-old PWA tab bloats |

### B. Performance

| # | Finding | Evidence | Impact |
|---|---------|----------|--------|
| B1 | **One SQLite connection + one `threading.Lock` serializes *all* reads and writes** across 4 queue workers + shim threads + console. WAL's concurrent readers are unused. | `sqlite/store.py:28`, `sqlite/db.py:33` | Everything queues on one mutex |
| B2 | `stats()` holds that lock across **10 aggregate full-scan queries**, and the SSE stream recomputes it **every 2 s per connected client**. | `store.py:175-226`, `ops_api.py:44,315-331` | N viewers = N× full scans/2s; saturates uvicorn threadpool |
| B3 | **Missing indexes**: `find_completed` filters `(query,status)` — full scan per download (dedup path); `claim_next` has no `(status,created_at)` composite and an unindexed `batches(paused,cancelled)` subquery; `attempts GROUP BY source` unindexed. | `store.py:332-338,56-70`, `migrations/001_init.sql` | Dedup + claim get slower with every row |
| B4 | `attempts` grows unbounded; no retention. `stats()` scans it. | `store.py:81-89` | O(history) dashboard |
| B5 | **Sources within a tier run sequentially**; no per-source wall-clock budget. One hung source stalls the entire cascade (and its queue worker). | `application/multiplex.py:77-99` | Throughput cliff |
| B6 | **No subprocess timeouts**: `ffprobe`, `mp4decrypt`, and `yt-dlp` can hang a worker thread forever. Telegram `request()` can wait forever on a silent bot. | `audio/probe.py:59-76`, `audio/decrypt.py:14-38`, `adapters/sources/ytdlp.py:51-68`, `telethon_client.py:39-70` | Permanent worker loss (the class of bug behind the stuck-batch incident) |
| B7 | UI polls on 4 independent timers (1 s tick, 3 s batches, 5 s player, 15 s history) regardless of tab visibility, on top of SSE. | `app.ts:1754-1768` | Chatty network, wasted repaints |
| B8 | History/batch lists render all rows (no virtualization/pagination); >1k rows or a 1k-track batch janks. | `app.ts:731-762,1332` | Fluidity cliff |
| B9 | `httpx.Client`s never closed; FD drift. | `deezer/client.py:22`, `qobuz/client.py:27` | Minor |

### C. Accuracy (the "wrong song" problem)

| # | Finding | Evidence | Impact |
|---|---------|----------|--------|
| C1 | **yt-dlp = `ytsearch1:` — first YouTube hit, zero verification** (no title/artist/duration check). Source of the "Full Video Song ｜ …" junk in the library. | `adapters/sources/ytdlp.py:28` | Worst accuracy offender |
| C2 | **ISRC plumbing exists but is dead**: `select_track(isrc=)` in qobuz/deezer is never fed by composition; requests carry only a flat query string. | `qobuz.py:77`, `deezer.py:85`, `composition/sources.py:56-62,113-119` | Exact-identity matching unused |
| C3 | **Duration parsed by every source, used by none** — no ±tolerance guard against remixes/extended/live cuts. | all adapters | Same-title wrong-cut risk |
| C4 | Requests are a **flat text query** end-to-end; Spotify search/bulk already knows artist/title/duration (and can know ISRC) but it's flattened at enqueue. | `store.py:42-53`, `ops_api` enqueue path | Root cause under C1–C3 |
| C5 | De-dup (`find_completed`) matches the exact query string: same song ≠ same string re-downloads; **a lossy ytdlp grab permanently blocks a lossless upgrade** of the same track. | `composition/sources.py:299-311,343-353` | Quality ceiling + duplicates |
| C6 | No identity/quality verification post-download: `classify()` reads codec only; ADR-0005 (chromaprint identity, fake-FLAC spectrum) never ported. The Mammoth bug "verified" fine. | `domain/audio.py:34-47` | Wrong/fake files pass |
| C7 | Soulseek/Telegram select by filename heuristics only (artist unscored since fix #24 covers qobuz/deezer/monochrome only). | `soulseek.py:72-85`, `telegram_bot.py:70-78` | Residual wrong-pick risk |

### D. Multi-user gaps

| # | Finding | Impact |
|---|---------|--------|
| D1 | No auth: console API key optional (unset in prod), shim key was dead until the compose fix. No user identity anywhere — `requested_via` is the only attribution. | Anyone on LAN = admin |
| D2 | Queue is global FIFO — one user's 500-track batch starves everyone. `batches.owner` exists but is decorative. | Fairness |
| D3 | Spotify OAuth stores a **single operator refresh token** in the global config store. | One account only |
| D4 | SSE: one poller loop *per client*, each independently hammering `stats()` (see B2). | Scales O(clients) |
| D5 | Single shared write connection is fine for one process, but B1's design leaves no headroom for concurrent users. | Foundation issue |

### E. Dependency tree & footprint

- Runtime Python deps are already lean: typer, httpx, pydantic, fastapi, uvicorn, pycryptodome, mutagen. Two trims available: **typer drags rich+pygments+shellingham** (~9 MB, unused — CLI prints plain text); **fastapi** could become bare starlette (already a transitive dep) since ops_api uses little of it. Optional heavies (playwright, telethon) are correctly lazy/dev-gated.
- **Docker image 628 MB** — Chromium + Xvfb (Monochrome only) + ffmpeg dominate. A headless-core image without the browser would be ~250 MB.
- Console UI is exemplary: **zero runtime deps, 85 KB bundle (~30 KB gz)**, hand-rolled RAF-batched morphdom. Keep it framework-free — do not add React/etc.

---

## The plan

### Phase 0 — Stop the bleeding (small diffs, ship this week)

1. **Subprocess + source timeouts** (B6): `timeout=` on ffprobe (30 s), mp4decrypt (60 s), yt-dlp (300 s), Telegram overall deadline, Turnstile outer deadline. A timed-out source raises → cascade advances; a worker can no longer die.
2. **Stream downloads to disk** (A1–A3): replace `get_bytes` with `stream_to(url, path)` using `httpx.stream()` + chunked writes; Deezer's stripe cipher decrypts per 2048-byte chunk (it's chunk-aligned by design) — encrypted→decrypted streaming, no full-file buffers; Monochrome reads only the header window for the KID. Peak RAM per worker: ~MBs, independent of file size.
3. **Indexes + retention** (B3, B4, A4): add `requests(query,status)`, `requests(status,created_at)`, `attempts(source)`, `batches(paused,cancelled)` in migration 00X; nightly prune of `attempts` older than 90 days; cap `ShimState._transfers` (drop completed after 24 h / keep last 200).
4. **Stats cache** (B2): memoize `stats()` for 2 s (single shared frame for all SSE clients + `/stats`); invalidate on write. One recompute per tick total, not per viewer.
5. **ytdlp verification** (C1): `ytsearch5:` + `--print` metadata pass, score candidates with existing `match_score` (+ duration sanity when known), download the winner only. Kills the YouTube-junk class.

*Effort: ~2–3 days. No schema/behavior breaks. Biggest risk reduction per line changed.*

### Phase 1 — Track identity end-to-end (accuracy core)

1. **Structured requests** (C4): add nullable `artist`, `title`, `isrc`, `duration_s` columns to `requests`; console search/bulk enqueue fills them from Spotify (it already has artist/title/duration; request ISRC via `external_ids`). Free-text queries keep the flat string as fallback.
2. **Feed the sources** (C2, C3): cascade passes identity down; qobuz/deezer use `isrc=` (exact match short-circuits fuzzy entirely); all sources apply a **duration guard** (reject candidates >7 s off when expected duration known); extend artist-aware `match_score` to soulseek/telegram ranking (C7).
3. **Identity-based de-dup + upgrade path** (C5): `find_completed` matches on ISRC (fallback normalized artist+title), and only reuses a **lossless** file; a lossy hit re-enters the cascade at Tier 1/2 for upgrade, replacing the file on success.
4. **Post-download sanity** (C6-lite): after probe, compare measured duration vs expected — mismatch = failed attempt, cascade advances. (Full chromaprint identity = Phase 4.)

*Effort: ~1 week. This eliminates the Mammoth class structurally instead of heuristically.*

### Phase 2 — Multi-user foundation

1. **DB layer** (B1, D5): thread-local reader connections (WAL gives free concurrent reads) + one dedicated writer connection behind the existing lock; `stats()`/`list_requests`/SSE move to readers. Contention drops to writes only. SQLite stays — right choice for a single-node appliance; revisit only if multi-node ever happens.
2. **Users + auth** (D1): `users` table + session tokens (cookie), login page; API key remains for Explo/automation. All mutating endpoints require a session; `requests.owner` FK recorded at enqueue.
3. **Fair scheduling** (D2): `claim_next` becomes round-robin across owners with queued work (oldest-first within an owner). One SQL change + tests; big batches stop starving interactive requests.
4. **Per-user Spotify** (D3): OAuth refresh tokens keyed by user in the config store; "Listening" panel and search quota become per-user.
5. **SSE hub** (D4): one background sampler produces the live frame; all client streams fan out from it (combines with the Phase-0 stats cache).

*Effort: ~1–2 weeks. After this, N users cost ~O(1) DB pressure + per-user auth context.*

### Phase 3 — Lightweight & fluid (footprint + UI polish)

1. **Deps** (E): swap typer for plain click or `typer-slim` (drops rich/pygments/shellingham); evaluate fastapi→starlette for ops_api (pydantic stays for DTO validation). Runtime venv shrinks ~15–20 MB and the tree gets one level shallower.
2. **Image split** (E): publish `crate:core` (no Chromium/Xvfb, ~250 MB) and `crate:full` (Monochrome-capable, current 628 MB). Compose picks per deployment.
3. **UI network discipline** (B7): one coalesced scheduler tick; pause all polling when `document.hidden`; player poll only on the active tab; history refresh via SSE hint instead of blind 15 s pull.
4. **UI scale** (B8, A6): virtualize History (render visible ~12 rows of the 430 px viewport); paginate batch track lists (50 at a time); prune `inflight`/`enqueued`/`batchDetails` on terminal states.
5. **Look & feel**: skeleton states for every panel, 150–200 ms ease transitions on row insert/upgrade, live progress shimmer on in-flight rows, toast consistency — all in the existing vanilla-TS/no-framework budget (bundle stays <100 KB).

*Effort: ~1 week. This is the "extremely fluid, great to look at" phase; zero new runtime deps.*

### Phase 4 — Deep verification (optional, revives ADR-0005)

1. Chromaprint (`fpcalc` subprocess) identity check when an expected fingerprint or a prior file exists; AcoustID lookup as MBID fallback.
2. Fake-FLAC spectral cutoff detector (ffmpeg `astats`/`showspectrum` heuristic) for untrusted sources (soulseek/telegram).
3. Cover-art fetch in the librarian (qobuz/deezer/TIDAL art URLs are already in search responses) — replaces the external beets pass for new downloads.

*Effort: ~1 week. Ship last; Phases 0–1 already remove most wrong-file risk.*

---

## Sequencing & guardrails

- Order: **0 → 1 → 2 → 3 → 4**. Each phase is a PR series behind the existing gate; deploy after each phase via the standing release/deploy pipeline.
- Non-negotiables preserved throughout: hexagonal layering (ruff-enforced), coverage ≥90 %, console stays framework-free, SQLite stays, no new always-on runtime dependency without a line in this doc.
- Explicit non-goals: multi-node scaling, resumable downloads (parking lot), rewriting the UI in a framework, WebSocket protocol revival.
