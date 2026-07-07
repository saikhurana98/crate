# Phase 1 — Track identity end-to-end

Goal: kill the wrong-song class structurally. Requests stop being a flat string;
sources match on ISRC/duration when known; de-dup keys on identity, not text;
lossy files stop blocking lossless upgrades.

## 1. Structured requests

- Migration `005_track_identity.sql`: nullable `artist`, `title`, `isrc`,
  `duration_s` columns on `requests`; index `idx_requests_isrc ON
  requests(isrc) WHERE isrc IS NOT NULL`.
- New frozen dataclass `domain/identity.py::TrackIdentity(artist, title, isrc,
  duration_s — all optional)`; `display()` renders the legacy query string.
- `RequestStore.enqueue/open_request` accept an optional `TrackIdentity` and
  persist the columns; `claim_next` returns it.

## 2. Fill identity at the edges

- Console Spotify search (`infrastructure/spotify/client.py::search`): request
  `external_ids` → capture `isrc`, `duration_ms`; enqueue passes full identity.
- Bulk: Spotify playlist resolver already has per-track artist/title/duration —
  thread through `application/bulk.py::ResolvedBatch` (tracks become
  `TrackIdentity`, paste-list lines stay title-only identities).
- Explo shim: parse `"Artist - Title"` shape from the advertised filename into
  a best-effort identity; free text stays fallback.

## 3. Feed the sources

- `run_cascade(..., want: TrackIdentity | None)`; `TierMux.acquire` passes it
  down; `SourceEntry.download` signature becomes
  `(query, out_dir, want) -> DownloadOutcome` (adapters may ignore `want`).
- Qobuz/Deezer: pass `want.isrc` into the existing `select_track(isrc=)` —
  exact ISRC short-circuits fuzzy matching.
- All selecting adapters: when `want.duration_s` known, reject candidates with
  `|candidate.duration - want.duration_s| > 7` before scoring.
- Soulseek/Telegram ranking: include `match_score` with `want.artist` tokens
  (extends fix #24 to the filename-based sources).
- ytdlp: feed `expected_duration_s` (wired in Phase 0's scored search).

## 4. Identity de-dup + upgrade path

- `find_completed(identity)`: match by ISRC first, else by
  `norm_tokens(artist) + norm_tokens(title)` equality; only reuse when the
  stored download is `lossless=1` **and** the file exists.
- If a *lossy* completed match exists: enqueue as an **upgrade** — run the
  cascade over Tiers 1–2 only; on a lossless win, atomically replace the
  library file (same librarian `_atomic_move`), update the `downloads` row,
  record `upgraded_from` in the attempt detail. On no win, keep the lossy file
  (request marked done, detail "no lossless source").

## 5. Post-download sanity

- In `TierMux.acquire`, after probe: if `want.duration_s` known and the
  measured duration deviates > 7 s → failed attempt
  (`"duration mismatch: got Xs, want Ys"`), cascade advances.

**Acceptance:** a request enqueued from Spotify search carries ISRC → Qobuz
selects by ISRC (test with two same-title candidates, wrong one first);
duration mismatch rejects (test); lossy file + new request = upgrade attempt
(test); re-imported playlist reuses only lossless files. Console history shows
artist/title (UI reads new columns, falls back to `query`).
