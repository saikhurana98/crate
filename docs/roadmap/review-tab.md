# Review tab — user-assisted retry / re-download

Design reference: `mockups/console-review.html` (from Claude Design "lbmd Console
Review"). Match it pixel-for-pixel — it reuses the existing dot-matrix tokens.

## What it is
A third console page (`console | downloads | review`) that surfaces the two
kinds of tracks worth a second look and lets the user drive a retry:

- **Failed** requests (all tiers missed).
- **Lossy** downloads (a `done` row whose `downloads.lossless = 0` — e.g. a
  Tier-3 yt-dlp grab that should be upgraded to FLAC).

For each, the user can tweak the query, **search every candidate-capable source
without downloading**, compare candidates side by side (source · tier · quality
· duration Δ · size · **match %**), pick the closest, and **download that exact
candidate** — replacing the lossy file (upgrade) or fulfilling the failed
request. Dismiss removes it from the list.

## Backend

### New: a candidate abstraction (search without committing)
Today adapters do search → select → download in one call. The review flow needs
the intermediate step exposed.

- `domain/candidate.py`: frozen `Candidate(source, tier, ref, title, artist,
  album, quality_label, lossless, duration_s, size_bytes)`. `ref` is the
  source-specific handle needed to fetch exactly this result (Qobuz track id,
  Deezer id, arcod trackId, yt video id, TIDAL id) — opaque string.
- Each candidate-capable adapter gains two pure-ish functions (same injected-IO
  style):
  - `search_candidates(query, want, *, <io>) -> list[Candidate]` — run the
    source's search, return ALL title-matching results as candidates (reuse the
    existing `parse_search`; do NOT collapse to one via `select_*`). Apply the
    title gate only; leave artist/duration ranking to the API's `match%`.
  - `download_candidate(candidate, out_dir, want, *, <io>) -> DownloadOutcome` —
    fetch exactly `candidate.ref` (skip search/select) → the existing download
    path by id.
- **v1 candidate sources:** qobuz, deezer, monochrome, arcod, ytdlp (all have a
  clean search→ref→download-by-id path; ytdlp already does `ytsearch5`).
  Soulseek + Telegram are **deferred** (P2P/bot listing is slow and awkward to
  enumerate) — note in the doc, exclude from the source fan-out for now.
- Composition: a `build_candidate_sources()` that returns
  `{name: (search_candidates_closure, download_candidate_closure)}` honoring the
  same enable/layout rules as `build_tiers`.

### Match %
`domain/matching`: `candidate_match(cand, want) -> int` (0–100) = weighted
title-token overlap · artist overlap · duration proximity (the design's
"match % = title · artist · duration similarity"). Pure + tested.

### Store / API (`ops_api.py`, `store.py`)
- `store.list_review()` — failed requests + lossy done rows (join downloads,
  `lossless=0`), minus a `dismissed` set. Add a `reviewed` / `dismissed` flag
  (migration `006_review.sql`: `requests.dismissed INTEGER DEFAULT 0`, partial
  index). Include current-file info (path/quality/duration) for lossy items and
  the failure reason for failed items.
- `GET  /api/review` → the list + the 4 stat-card figures (failed count, lossy
  count, resolved-today, etc — read from the design's `reviewStats`).
- `POST /api/review/{id}/search` `{q}` → fan out `search_candidates` across the
  enabled candidate sources (bounded per-source timeout; failures skipped), sort
  by `candidate_match` desc, return the candidate rows the design renders.
  Offloaded off the event loop (`asyncio.to_thread`) like the other blocking
  endpoints. Cache nothing — it's on-demand.
- `POST /api/review/{id}/download` `{candidate_id}` → enqueue a targeted
  re-download of that candidate: run `download_candidate` → finalize via the
  librarian → for a **lossy** item, atomically replace the existing library file
  and update its `downloads` row (reuse the Phase-1 upgrade path in `dedup.py`);
  for a **failed** item, mark the request done. Records an attempt trail. Runs
  through the existing serve queue worker (write a `queued` row tagged with the
  chosen candidate) so the console stays write-only.
- `POST /api/review/{id}/dismiss` → set `dismissed=1`.

## Frontend (`apps/console/src/app.ts`, vanilla TS, no deps)
- `state.page` becomes `"console" | "bulk" | "review"`; `navTabsHtml` renders
  three segments (`console | downloads | review`) — review carries a red count
  badge (`activeReview()`), matching the design. Add `gotoReview` handler.
- New `reviewPageHtml()` + row/candidate renderers straight from the mockup:
  stat cards, per-item expandable `<section>` (reason badge, query, candCount,
  chevron), expanded body (search input + "search again", expect/current line,
  searching spinner, candidate rows with radio/source/tier/title/flags/quality/
  dur/Δ/size/match, download+dismiss actions, legend), empty state.
- State: `reviewItems`, per-item `{expanded, q, searching, cands, selectedCand,
  downloading}`. Wire `toggleReview / onReviewQuery / rescanReview / selectCand /
  downloadCand / dismissReview` to the API. Poll `/api/review` on the review page
  + reflect SSE where a re-download completes.
- Reuse the existing morphdom render loop, event delegation, and history detail
  drawer (already in the design). Keep the bundle framework-free and < 100 KB.

## Tests
Backend: `candidate_match` scoring; `search_candidates`/`download_candidate` per
adapter (fakes, no network) incl. download-by-ref skipping search; `list_review`
selects failed + lossy-only and honors dismiss; the review API endpoints
(FastAPI TestClient) incl. the fan-out + the lossy-replace upgrade path. Console:
`tsc --noEmit` + `npm run build` clean; render smoke.

## Non-goals (v1)
Soulseek/Telegram candidate listing; automatic (non-user-driven) upgrades (that's
the existing Phase-1 dedup path); chromaprint match confirmation (Phase 4 §1,
deferred).
