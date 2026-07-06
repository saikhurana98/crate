# ADR-0015: Tier-cascade scheduler (Explo / batch mode)

- **Status:** accepted
- **Date:** 2026-07-02
- **Deciders:** @saikhurana98

## Context

[ADR-0003](0003-two-tier-scheduler.md) designed the scheduler around **live DJ sets**: fire a lossy "rip" pipeline and a FLAC "endgoal" pipeline *simultaneously*, deliver a playable file in seconds, upgrade to FLAC in the background. That ADR explicitly rejected "strict priority with fallback" because, for the live use case, waiting on a degraded top-priority source adds unacceptable latency.

The Explo integration ([ADR-0014](0014-slskd-compat-shim.md)) is a **different use case**: a non-interactive weekly batch job. Nobody is waiting on a dance floor. What matters is getting the **highest-quality file that the priority order allows**, not time-to-first-playable. The user's requirement for this mode is an explicit, ordered cascade:

- **Tier 1 — ripping sites → 24-bit FLAC:** Squid/Qobuz, Lucida, Monochrome (Soulseek folds in here).
- **Tier 2 — streaming lossless rippers:** Spotify / Apple Music lossless.
- **Tier 3 — lossy fallback @ 320 kbps:** yt-dlp / YT Music (or equivalent).

Priority must be honoured **1 → 2 → 3**; a lower tier is only reached when the tiers above it fail or blow a deadline. This is precisely the "strict priority with fallback" model ADR-0003 rejected — correct there, wrong here. The two use cases need two scheduling strategies, selected by mode, not one strategy retrofitted.

## Decision

**Add a `download-cascade` application use-case, selected by request `mode: "cascade"`,** alongside the existing live-set/bulk use-cases. It reuses the Phase-4 race primitive but composes it differently:

1. **Between tiers: strict sequential cascade.** Attempt Tier 1; only on failure/deadline attempt Tier 2; only then Tier 3. First **verified** file wins the whole job — there is no provisional-lossy swap in this mode.
2. **Within a tier: parallel race** across that tier's eligible adapters (reuse `application/scheduler/race.ts` from [4A]). First-byte wins, losers cancelled via `AbortSignal` (same polite-cancel contract as ADR-0003).
3. **Per-tier deadline.** Each tier gets a bounded budget (`CRATE_CASCADE_TIER_DEADLINE_MS`, default: T1 180s, T2 120s, T3 60s). Deadline breach or all-adapters-failed → advance to the next tier. The aggregate must resolve inside Explo's 15-min no-progress window ([ADR-0014](0014-slskd-compat-shim.md)).
4. **Verification is mandatory and unchanged.** Chromaprint identity match + FLAC health check + fake-FLAC spectrum-cutoff detector ([ADR-0005](0005-chromaprint-acoustid.md)). Ripping sites and Soulseek routinely serve transcoded fake-FLACs, so a Tier-1 "FLAC" that fails the cutoff detector is **rejected and the tier continues/advances** rather than accepted.
5. **Eligibility filter** is the ADR-0003 filter plus a `tier` predicate: an adapter is eligible for tier N only if its `capabilities.tier === N`. Source adapters gain a `tier: 1 | 2 | 3` capability flag.

The live-set two-pipeline strategy (rip ‖ endgoal, provisional swap, `final-native`, `pending → provisional → final` state machine) is **retained unchanged** for `mode: "live"` / interactive clients. Cascade mode short-circuits it: state goes `pending → final` (or `permanent-provisional` only if Tier 3 also fails), with no `provisional` intermediate.

### Adapter tier assignments

| Tier | Adapters (`SourceAdapter.capabilities.tier`) |
|---|---|
| 1 | `squid-qobuz`, `lucida`, `monochrome`, `soulseek` |
| 2 | `spotify` (lossless rip), `apple` (lossless rip) |
| 3 | `ytdlp-ytmusic`, `onthespot` (as last-resort lossy) |

(Exact placement of `onthespot`/`soulseek` is an adapter-capability flag, changeable without touching the scheduler.)

## Consequences

- **Positive:** Honours the user's explicit quality priority for the Explo/batch use case. Simpler mental model than the live pipeline — one file, one winner, no swap. Reuses the existing race primitive and verification stack; the cascade is just an ordering wrapper. Fake-FLAC rejection can't silently downgrade a Tier-1 result into an accepted lie.
- **Negative / accepted trade-offs:** Higher worst-case latency than a global race (a degraded Tier-1 source can burn its full deadline before Tier-2 starts). Accepted: the batch use case is not latency-sensitive, and per-tier deadlines bound the damage. Two scheduling strategies now coexist — more surface area, mitigated by both sharing the race + eligibility + verification primitives and differing only in composition.
- **Neutral:** Adds a `tier` capability flag to `SourceAdapter` and a `mode` field to the download request. `ARCHITECTURE.md` §3 (scheduler) documents both strategies side by side.

## Alternatives considered

- **Reuse the ADR-0003 two-pipeline race for Explo too.** A global first-byte race ignores the requested priority — a fast Tier-3 lossy source would routinely beat a slower Tier-1 FLAC, defeating the "FLAC-first" requirement. Rejected.
- **Single strategy with tier as a scoring weight** (score-based race biased toward higher tiers). Probabilistic, not strict — a heavily-weighted Tier-1 source can still lose to Tier-3 under latency variance. The requirement is *strict* order, so a soft bias doesn't satisfy it. Rejected.
- **Fully replace ADR-0003 with the cascade.** Would regress the live-DJ time-to-playable goal that motivated the whole project. Rejected — both modes coexist.

## References

- [ADR-0003](0003-two-tier-scheduler.md) — the live-set strategy this ADR branches from (mode-selected, not superseded).
- [ADR-0005](0005-chromaprint-acoustid.md) — verification (mandatory in cascade mode).
- [ADR-0014](0014-slskd-compat-shim.md) — the Explo transport that requests `mode: "cascade"`.
- `core-design.md` — original Tier 1/2/3 requirement.
