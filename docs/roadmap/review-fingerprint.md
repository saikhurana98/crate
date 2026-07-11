# Review fingerprint guard — spec

Builds on [`review-tab.md`](review-tab.md). Decision + rationale + the negative
ISRC spike: [ADR-0019](../adr/0019-fingerprint-review-verification.md). **Read the
ADR first** — it explains why this is audio-similarity, not ISRC matching.

## What it adds
Three reinforcing signals so a user never silently replaces a good file with a
wrong-version grab:

1. **Similarity gate** — auto-compare the new download's fingerprint to the
   existing file; block a silent replace below 0.95.
2. **AcoustID hint** (optional) — "fingerprint reads this as: Artist — Title",
   flags `(acoustic)`/`(instrumental)`/different-song. Advisory.
3. **Sample playback** — play the current file + candidate 30 s previews for an
   A/B ear test. Human is the final arbiter.

Feature is **opt-in and degrades cleanly**: no `fpcalc` binary → gate + hint off,
review stays metadata-only; no `CRATE_ACOUSTID_KEY` → hint off, gate still works
(needs no external data); no candidate `preview_url` → that preview button absent.

## Backend

### Fingerprinting (new)
- `infrastructure/audio/fingerprint.py` — `fpcalc -json <path>` subprocess, bounded
  timeout (30 s), → `(duration_s: int, fingerprint: str)`. `fpcalc_available()`
  probes the binary once at startup; absent → feature silently off. No new Python
  dep (subprocess only).
- `domain/fingerprint.py` (pure, zero-IO) — decode chromaprint base64 → `list[int]`
  (`decode_fingerprint`), `similarity(fp_a, fp_b) -> float` in 0..1 via
  offset-tolerant bit-error-rate over the overlapping window (small ± offset search
  to tolerate encoder padding). `THRESHOLD = 0.95`, importable/configurable. This is
  the accuracy core — unit-tested with fixtures (identical, shifted, different).

### AcoustID hint (new, optional)
- `infrastructure/acoustid/client.py` — POST `client + duration + fingerprint`,
  `meta="recordings releases isrcs"` (**space-separated**; `+` gets `%2B`-escaped and
  silently drops the meta — the spike bug), parse top result → `{canonical_artist,
  canonical_title, mbid}`. Do **not** rely on ISRC (0% on the real library). 3 req/s
  aware; env-gated `CRATE_ACOUSTID_KEY`, raise-if-absent so the factory skips it.
- `application/identify.py` — pure-ish use-case: existing file → fingerprint →
  optional AcoustID lookup → `IdentityHint(canonical_artist, canonical_title, mbid,
  ref_fingerprint)`. All IO injected; tested with fakes.

### Gate on re-download (`application/review.py`)
`run_review_download` gains an injected `fingerprinter` + `similarity` + a `force`
flag and a `threshold`:
- Fingerprint `outcome.path` (the new file) and, for an upgrade, `target_path` (the
  existing file). Compute `match_similarity`.
- If `is_upgrade and match_similarity < threshold and not force`: **raise a typed
  `MatchTooLow(score)`** *before* `replace` — nothing is overwritten. The worker
  marks the request `needs_confirm` with the score (not `failed`).
- Else proceed as today; thread `match_similarity` into the `DownloadReport` /
  `downloads` row.
- When `fingerprinter is None` (binary absent) → skip the gate entirely (current
  behaviour), so the feature is truly optional.

### Store / schema (`store.py`, migration `007_fingerprint.sql`)
- `007_fingerprint.sql`: `requests.ref_fingerprint TEXT`, `requests.acoustid_artist
  TEXT`, `requests.acoustid_title TEXT`, `requests.acoustid_mbid TEXT`; `downloads.
  match_similarity REAL`; a `needs_confirm` status value (no enum change — status is
  free text) + partial index for the review list. Additive, nullable, back-compatible.
- `list_review` / `review_stats`: add the `needs_confirm` bucket (third category),
  surface `match_similarity` + the cached AcoustID hint + `ref_fingerprint` on rows.
- `queue_review_download`: carry `force` on the armed candidate row.

### API (`inbound/ops_api.py`)
- `POST /review/{id}/identify` → run `identify` on the item's current file (offload
  via `asyncio.to_thread`), cache the hint on the request row, return
  `{canonical_artist, canonical_title, mbid}` (or `{}` when unavailable). Idempotent
  / cached.
- `POST /review/{id}/download` gains `force: bool` (default false) → threaded to the
  armed row; a `needs_confirm` item is re-submitted with `force=true` to override.
- `GET /review/{id}/audio` → **stream the current file with HTTP Range** (206 +
  `Accept-Ranges: bytes`), content-type from extension. Path is resolved from the
  review row's `file_path` — **never** from a query param (no traversal). Needs the
  library mounted read-only into the console (compose: `/mnt/Media/Music:/library:ro`
  on `crate-console`; today only `serve` mounts it).
- `GET /review` response: items carry `status` (`failed`|`lossy`|`needs_confirm`),
  `match_similarity`, the AcoustID hint, and a `has_audio` flag.

### Candidate preview (`domain/candidate.py` + adapters)
- `Candidate` gains `preview_url: str | None`.
- `deezer.parse_search`: capture `preview` (public 30 s CDN MP3) → `preview_url`.
  Qobuz/arcod best-effort where a sample URL exists; monochrome/ytdlp leave it
  `None`. `_candidate_row` serialises it.

## Frontend (`apps/console/src/app.ts`, vanilla TS)
- **Third state:** review items are `failed` | `lossy` | `needs_confirm`; add the
  `needs_confirm` stat card + badge/filter ("attention required", amber). Wire the
  `force=true` re-submit ("Replace anyway") + "Keep current" (dismiss) actions.
- **AcoustID hint line** on an expanded lossy item (muted; warning chip when it
  differs from the request, esp. version tags).
- **Match confidence** after a re-download: green "✓ matches — 98%" (≥0.95) or the
  amber needs-confirm panel "⚠ doesn't match — 42%".
- **Playback:** one shared `<audio>` element for the whole page (starting one
  pauses others). Current-file play/pause + slim scrubber + monospace timecode
  streaming `/api/review/{id}/audio`; per-candidate preview button enabled only when
  `preview_url` present. Tiny inline controls, dot-matrix tokens; no volume/playlist.
- Reuse the morphdom loop + event delegation. Keep the bundle framework-free < 100 KB.

## Tests
- `domain/fingerprint`: `decode_fingerprint`; `similarity` on identical (=1.0),
  shifted-but-same (≥0.95), and different (<0.5) fixtures; threshold boundary.
- `infrastructure/audio/fingerprint`: fake subprocess (stdout json / nonzero /
  timeout); `fpcalc_available` true/false.
- `infrastructure/acoustid`: fake HTTP — space-`meta` on the wire, hint parse, no
  ISRC assumption, absent-key skip.
- `application/review`: gate passes ≥threshold (replaces); `MatchTooLow` <threshold
  raises before replace (existing file untouched); `force` overrides; `fingerprinter
  is None` skips the gate.
- `application/identify`: hint assembled, AcoustID-absent path.
- API (FastAPI TestClient): `identify`; `download` `force` round-trip →
  `needs_confirm`; `/audio` Range (206, partial bytes, bad-id 404); preview_url in
  candidate rows.
- Console: `tsc --noEmit` + `npm run build` clean; render smoke incl. the three
  states + a play control.

## Non-goals (v1)
Fingerprinting every candidate up front; a full media player (volume, playlist,
waveform); AcoustID for *selection* (advisory only); ISRC/MBID exact matching
(spike: 0% ISRC — see ADR-0019); previews for sources that ship none.
