# ADR-0019: Fingerprint-gated review re-download (audio similarity, not ISRC)

- **Status:** accepted
- **Date:** 2026-07-11
- **Revives / narrows:** [ADR-0005](0005-chromaprint-acoustid.md) §1 (chromaprint
  identity), which Phase 4 had deferred. Supersedes the AcoustID-*lookup* idea as
  a matching mechanism — see the spike below.

## Context

The Review tab ([`docs/roadmap/review-tab.md`](../roadmap/review-tab.md)) lets a
user re-download a better source for a **failed** request or a **lossy** grab.
Today candidates are ranked only by a fuzzy metadata `match %` (title · artist ·
duration). The real disease in the review queue is **wrong-version / wrong-song**
grabs: the Tier-3 yt-dlp fallback lands an acoustic take, an instrumental, or an
entirely different recording that merely shares a title, and the user has no
reliable way to tell before replacing the file.

The original instinct (and an earlier draft of this feature) was: fingerprint the
existing file → resolve its **ISRC** via AcoustID → feed that ISRC into every
source adapter's existing ISRC short-circuit, turning fuzzy matching into exact
identity matching.

## The spike that changed the design

Before building, we measured AcoustID coverage against **the real review queue**
(160 lossy items, a heavily Indian/Punjabi/Tamil + indie-electronic library) with
`fpcalc` 1.5.1 + the AcoustID lookup API:

| Tier | Result |
|---|---|
| Fingerprinted cleanly (`fpcalc`) | **160 / 160 (100%)** |
| Fingerprint known to AcoustID | 116 / 160 (72%) |
| → resolves to a MusicBrainz recording (MBID) | 88 / 160 (55%) |
| → recovers a canonical artist/title | 82 / 160 (51%) |
| **→ resolves to an ISRC** | **0 / 160 (0%)** |

**ISRC resolution is zero** for this library (verified with correct `meta`
encoding — an early `+`-in-querystring bug that masked the recordings was fixed
and the result held). MusicBrainz has the *recordings* but no ISRCs attached for
this catalog. Two consequences kill the original plan:

1. The **ISRC short-circuit can't be fed** — there is no ISRC to inject.
2. A **"verified identical" badge has no shared key** — Qobuz/Deezer expose ISRCs,
   not MBIDs, so a candidate can't be cross-matched against an MBID-only identity.

What the spike *did* surface: the canonical title AcoustID returns exposes the
wrong-version problem directly (real samples: `Avicii – Lonely Together` →
`(acoustic)`; `AllttA – Connery` → `(instrumental)`; an A.R. Rahman Tamil track →
a different song). And `fpcalc` itself is 100% reliable — so **audio-to-audio
similarity**, which needs no external DB, ISRC, or MBID, works everywhere the
lookup does not.

## Decision

Build **fingerprint-gated re-download**, keyed on audio similarity, with AcoustID
demoted to an optional advisory hint and human playback as the final arbiter.

- **`fpcalc` wrapper** — `infrastructure/audio/fingerprint.py`: subprocess with a
  bounded timeout → `(duration_s, fingerprint)`. Binary presence detected at
  startup; the whole feature is **silently off when absent** (core Docker image
  may omit `libchromaprint-tools`; full image adds it). No new Python dep.
- **Pure similarity** — `domain/fingerprint.py`: `similarity(fp_a, fp_b) -> float`
  (0..1) via offset-tolerant bit-error-rate over the decoded chromaprint. Pure,
  zero-IO, unit-tested; this is the accuracy core. Threshold **0.95** (configurable).
- **Pre-commit gate** — on a review re-download, fingerprint the newly downloaded
  candidate and compare it to the existing file before the atomic replace:
  - ≥ 0.95 → replace, record `match_similarity`.
  - < 0.95 → **do not silently replace.** The request enters a new
    `needs_confirm` state carrying the score; the user must re-submit with
    `force=true` (an explicit "replace anyway") to override. This is user-driven,
    so the gate *informs and guards* rather than hard-blocking a deliberate choice.
- **AcoustID hint (optional)** — `infrastructure/acoustid/client.py`, env-gated
  `CRATE_ACOUSTID_KEY`, absent = off. Looks up the existing file → canonical
  `{artist, title, mbid}` (**no ISRC dependency**). Advisory only, shown to flag
  version mismatches; never auto-trusted, never drives selection.
- **Sample playback (human arbiter)** — the review UI streams the existing file
  (`GET /api/review/{id}/audio`, HTTP Range, keyed by review id — no user path)
  and plays source-provided 30 s candidate previews (`Candidate.preview_url`,
  Deezer anchor). Requires mounting the library **read-only** into the console
  container (today only `serve` mounts it). Score flags, ear confirms.

## Consequences

- **Positive:** works for the actual (regional) library the ISRC plan failed on;
  targets the wrong-version disease directly; no new runtime Python dep; feature
  degrades to metadata-only when `fpcalc` is absent. The `:ro` library mount on
  console also unlocks future "play the library" features.
- **Negative / accepted:** `fpcalc` binary in the full image (~a few MB) + ~100 ms
  per fingerprint; the gate downloads the one committed candidate to fingerprint
  it (cheap — no fan-out). AcoustID hint covers ~half the queue and is
  occasionally wrong, hence advisory-only. Candidate preview exists only where the
  source ships a clip (Deezer reliably; others best-effort).
- **Neutral:** threshold 0.95 left configurable; may need field tuning for
  loudness-war remasters that legitimately differ.

## Alternatives considered

- **AcoustID ISRC → source ISRC gate** (the original plan): **rejected by data** —
  0/160 ISRC resolution on the real library.
- **"Verified identical" badge via shared ID:** no shared identifier exists
  (MBID-only identity vs ISRC-only candidates).
- **Fingerprint every candidate up front** to rank by similarity: too many
  speculative downloads; the gate on the single committed candidate gets the
  safety benefit at a fraction of the cost.
- **Metadata `match %` only** (status quo): leaves the wrong-version problem
  unsolved — the whole reason the review tab exists.

## References

- Spike data: run 2026-07-11 against the live review queue (160 lossy items).
- [ADR-0005](0005-chromaprint-acoustid.md), [`docs/roadmap/phase-4-deep-verification.md`](../roadmap/phase-4-deep-verification.md) §1.
- AcoustID Web Service: https://acoustid.org/webservice
