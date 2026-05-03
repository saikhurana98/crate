# ADR-0005: Chromaprint + AcoustID for fingerprinting and MBID fallback

- **Status:** accepted
- **Date:** 2026-05-03

## Context

We need two related capabilities:

1. **Identity verification**: when an endgoal FLAC arrives, confirm it represents the same recording as the provisional rip we already wrote (so we don't replace "Daft Punk - Around the World (Original Mix 1997)" with "Daft Punk - Around the World (2007 Remaster)"). Required only when the source's metadata might be wrong or ambiguous.
2. **Canonical-MBID fallback**: when MusicBrainz direct lookup fails (e.g., we have a Spotify track ID and MusicBrainz doesn't have an exact mapping), we want a way to recover the MBID from the audio itself.

Both capabilities can be served by **chromaprint**, an open-source acoustic fingerprint algorithm, paired with the **AcoustID** lookup service which maps fingerprints to MBIDs.

## Decision

- **Fingerprint algorithm**: chromaprint via the `fpcalc` CLI binary, wrapped by `adapters/fingerprint/chromaprint-acoustid.ts`. We shell out rather than use a Node binding because `fpcalc` is shipped on most systems alongside `flac` and behaves identically across platforms.
- **Identity comparison**: chromaprint similarity score between two fingerprints. Threshold per mode:
  - Live-set: similarity ≥ 0.95 ⇒ accept; else reject.
  - Background daemon: ≥ 0.95 accept; ≥ 0.75 review; else reject.
- **MBID fallback**: when MusicBrainz direct lookup yields nothing, post the chromaprint to AcoustID's web service (`https://api.acoustid.org/v2/lookup`). Use the highest-confidence MBID returned, or skip MBID and store with `local_id` only if no result. AcoustID lookup cached in `tracks.fingerprint` so repeat lookups are skipped.
- **Health check**: separate concern — `flac --test` validates a FLAC file decodes cleanly. Cheap (~50 ms), run on every endgoal arrival.
- **Fake-FLAC detection**: spectrum cutoff analysis on a 5-second sample. If declared FLAC but max-energy bin < 22 kHz cutoff, mark as `fake_flac_detected`. Run for adapters with `costClass: "p2p"` or `"telegram-bot"`; skip for `"rate-limited"` (paid mirrors).

`AudioVerifier` is the port; `chromaprint-acoustid.ts` is the only adapter for v1.

## Consequences

- **Positive**: chromaprint is the de-facto standard, well-maintained, and trusted by MusicBrainz itself. AcoustID gives us free MBID resolution as a backstop. The 0.95 threshold is conservative and protects the DJ from silent track replacement.
- **Negative / accepted trade-offs**: AcoustID API has a rate limit (~3 req/s with a free key); we batch and cache. `fpcalc` shells out, adding ~100 ms per call and a binary dependency. We accept the binary dependency because it's also useful for the user manually.
- **Neutral**: Threshold tuning may need adjustment in the field — left configurable (`verification.thresholds.*`). Album-aware fingerprinting (e.g., gapless album crossfades fooling identity check) is a known sharp edge; documented in future-work.

## Alternatives considered

- **MusicBrainz Picard library directly**: heavy Python tooling, hard to embed in Node. AcoustID gives us the same data over HTTP.
- **EchoPrint** (older Echo Nest fingerprint): defunct; service no longer exists.
- **Custom hash-based comparison** (e.g., MD5 of decoded PCM): no resilience to encoding differences, useless for cross-source identity verification.
- **Skip identity check entirely**: would miss the wrong-version risk. Unacceptable for live-set mode.
- **Audio fingerprinting as a service** (Shazam-like commercial APIs): expensive, closed-source, overkill.

## References

- chromaprint: https://acoustid.org/chromaprint
- AcoustID API: https://acoustid.org/webservice
- fpcalc binary: ships in `chromaprint-tools` package on most distros.
