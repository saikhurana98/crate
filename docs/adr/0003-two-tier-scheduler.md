# ADR-0003: Two-tier scheduler — rip and endgoal pipelines in parallel

- **Status:** accepted
- **Date:** 2026-05-03

## Context

The primary use case is **live DJ sets**, where the crowd requests a song and the DJ needs a playable file as fast as possible. The secondary goal is to maintain a high-quality FLAC catalog. Bandwidth is **not** a concern (per requirements). Available sources split cleanly into two natural tiers:

- **Rip tier** (latency-optimised, lossy): OnTheSpot (Spotify rip), `yt-dlp` (YouTube Music best-audio). Output ≥ 320 kbps lossy. Time-to-first-byte typically a few seconds.
- **Endgoal tier** (quality-optimised, FLAC): Squid/Qobuz mirror, Lucida, Monochrome, Soulseek. Output FLAC 24/192 where source allows. Time-to-complete-FLAC typically tens of seconds to minutes.

A single-pipeline approach forces a trade-off between speed and quality. A two-pipeline approach in parallel removes the trade-off entirely: deliver speed first, upgrade to quality in the background.

The earlier draft of this ADR considered a sophisticated probe-race + fetch-race-of-top-N + stagger model designed to conserve bandwidth. The user explicitly stated bandwidth is free in their environment, so we abandoned that complexity in favour of pure-race within each tier.

## Decision

**Two pipelines, fired simultaneously, per request:**

1. **Rip pipeline**: pure parallel `fetch` race across all `tier: "rip"` adapters. First completed file wins, others cancelled via `AbortSignal`. Provisional file written to `live-incoming/<job-id>.<ext>`. Emit `job.playable`.
2. **Endgoal pipeline**: pure parallel `fetch` race across all `tier: "endgoal"` adapters. First-byte wins (not first-completed) — cancel losers immediately to avoid them streaming pointless bytes. Stream the winner into `library/.tmp/`. Verify (health, identity vs provisional fingerprint, fake-FLAC check). On accept, atomic rename into `library/<Artist>/<Album>/<Track>.flac`, delete provisional, emit `job.upgraded`.

If endgoal completes **before** rip (cold-start case), skip the provisional altogether; status goes directly to `final-native`. A single `job.playable` + `job.upgraded` (or just `job.completed`) milestone is collapsed.

**Per-source concurrency limits** live inside each adapter (`p-limit` instance). The scheduler stays dumb — it fires; the adapter queues internally if at limit.

**Eligibility filter** (synchronous, before every race): adapter must have `capabilities.maxQuality >= req.minAcceptableQuality`, no active cooldown, `success_rate >= 0.3`, and support the input link kind.

## Consequences

- **Positive**: Time-to-playable is bounded by the fastest rip source, not the slowest FLAC source. The DJ gets responsiveness, the library gets quality, in one mechanism. No probe-race latency added (probe step kept in the contract for non-live use cases but skipped in live-set mode).
- **Negative / accepted trade-offs**: We bleed bandwidth — every request fires up to 6 simultaneous fetches. User has explicitly stated this is acceptable. Per-source rate limits are a real concern; mitigated by per-source `p-limit` and exponential cooldown. Telegram bots and Soulseek peers may eventually blacklist us if we cancel-spam them; mitigated by polite cancellation (graceful socket close within 100 ms — test contract enforces).
- **Neutral**: The state machine has a `final-native` state distinct from `final` to record whether a provisional was ever written. Useful for observability ("how often does endgoal beat rip?").

## Alternatives considered

- **Single-pipeline pure race** (the original spec): "fastest wins, discard rest" applied to all sources together. Either rips would always win (FLAC takes too long) defeating the point of having endgoal sources, or FLAC would block delivery. No way to satisfy speed + quality together.
- **Probe-race + fetch-race-of-top-2 + stagger**: bandwidth-conserving; rejected because user said bandwidth is free and the simpler model wins.
- **Strict priority with fallback**: try Squid first, then Lucida, then Monochrome, etc. Slow when the top-priority source is degraded. We use score-based pure race instead.
- **Split-load downloading** (one FLAC file fetched in chunks from N sources concurrently): rejected — most sources don't support range requests, FLAC files are small (~50 MB), coordination overhead exceeds savings.

## References

- Original requirements: `core-design.md` "Load Balance Setup".
- §3 of `ARCHITECTURE.md`.
