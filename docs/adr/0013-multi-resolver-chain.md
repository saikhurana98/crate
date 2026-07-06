# ADR-0013: Multiple metadata resolvers chained, first-success

- **Status:** accepted
- **Date:** 2026-05-03

## Context

A single canonical metadata source (MusicBrainz) covers most popular music well, but has gaps:

- New releases not yet imported.
- Niche electronic / underground tracks.
- Region-locked or service-exclusive titles.
- DJ edits, bootlegs, white labels.
- Spotify / Apple Music IDs that don't have a clean MBID mapping.

Conversely, Spotify and Apple have near-universal coverage of *what they sell* but offer no canonical identity beyond their own IDs (no shared MBID, sometimes wrong release-year metadata). YouTube Music is yet another catalog with its own quirks. AcoustID can recover MBIDs from audio fingerprints when nothing else works.

We want to:

1. Try a sensible default chain — MusicBrainz first (open, canonical), commercial APIs as gap-fillers.
2. Allow operators to reorder or disable resolvers per their preference.
3. Treat cover art separately, because not all metadata resolvers have great art and the criteria for picking art (size threshold) differ.
4. Support tracks with **no MBID at all** (DJ edits, bootlegs) without forcing a fake MBID.

## Decision

- **Two chains**, not one:
  - **Metadata chain**: ordered list of `MetadataResolver` adapters. Default: `["musicbrainz", "spotify", "apple", "ytmusic"]`. Strategy: **first-success**. The first resolver to return `Result.ok(track)` wins; downstream resolvers skipped.
  - **Cover-art chain**: ordered list of `CoverArtFetcher` adapters. Default: `["coverartarchive", "spotify-art", "apple-art", "lastfm-art"]`. Strategy: **first-success with size gate**. If the returned art is below a configured threshold (default 1000×1000), it's rejected and the next fetcher is tried.
- **Capability filter** before each call: a resolver that `cannotResolveByExternalRef` is skipped when given a Spotify URL, etc. Avoids wasted round-trips.
- **AcoustID as a fingerprint-driven MBID recoverer**: not a pure metadata resolver, but the chain can include it after MusicBrainz so that when MusicBrainz lookup misses, we post the chromaprint and try to resolve to an MBID that way. Cached in `tracks.fingerprint`.
- **Track-without-MBID is a valid state**:
  - `tracks.mbid` is `NULLABLE`.
  - `tracks.local_id` (ULID) is **always** present and the canonical key when MBID is absent.
  - DJ edits, bootlegs, brand-new releases all flow through the system without forcing fabrication.
  - Display: when MBID is absent, the UI shows `[local]` next to the track instead of an MBID badge.
- **Merge strategy**: first-success only in v1. The port also defines `merge-all` (run all in parallel, merge fields by resolver order) but no use case yet ships with it; held in reserve.
- **Configuration** in `crate.config.json` under `resolvers.metadata` and `resolvers.coverArt` arrays — operator can reorder, drop entries, or add new ones (provided the adapter is registered in the composition root).

## Consequences

- **Positive**: Coverage is the union of all resolvers, not the intersection. Operators can swap providers when one degrades (e.g., Spotify rate-limiting a key) without code changes. Tracks lacking MBIDs flow through cleanly. Cover art quality has a minimum bar; tiny thumbnails don't poison the library.
- **Negative / accepted trade-offs**: First-success means metadata quality depends heavily on the head-of-list resolver. We've chosen MusicBrainz first because its data, when present, is the most canonical. If MusicBrainz returns thin metadata where Spotify would have richer metadata, we accept the thin version. Not a real loss for v1; revisit with `merge-all` if it becomes annoying.
- **Neutral**: Adding a new resolver = one file in `adapters/metadata/`, one entry in the contract test parameter list, one entry in the config. No application code changes.

## Alternatives considered

- **Single resolver (MusicBrainz only)**: gaps for newer / niche tracks would surface as `not_found` and require manual intervention.
- **Always-merge**: better metadata fidelity, but slower (every request hits N services), more complex conflict-resolution rules, and per-resolver rate-limit concerns multiply. Held in reserve.
- **Voting / consensus**: fancy, doesn't solve the "MusicBrainz has nothing" case.
- **Force MBID synthesis** (use SHA-1 of artist+title): would let us use MBID as a uniform PK but pollutes the column with non-canonical IDs and confuses downstream tooling that expects real MBIDs. We chose `local_id` instead, kept distinct from `mbid`.

## References

- MusicBrainz API: https://musicbrainz.org/doc/MusicBrainz_API
- AcoustID: https://acoustid.org
- `tracks` schema in §12 of `ARCHITECTURE.md`.
