# ADR-0010: Opaque progress by default, stats-for-nerds opt-in

- **Status:** accepted
- **Date:** 2026-05-03

## Context

Most clients of this service (the crowd-request mobile app, the DJ deck) want to render a single progress bar per job and per group. They have no business knowing about the rip vs endgoal split, the per-source race, the cooldown ladders, or the verification thresholds. Leaking that complexity into the public protocol guarantees:

- Future internal changes (e.g., adding a third tier, swapping a source) become breaking changes for clients.
- Client UIs become noisy (per-attempt timelines that no DJ wants to look at mid-set).
- Telemetry surface and the public protocol surface become entangled.

Meanwhile, a debug UI, a Prometheus exporter consumer, and the developer running `crate` locally absolutely **do** want the gritty details: which source won, how long each attempt took, why a candidate was rejected, race timelines. A single protocol must serve both audiences.

## Decision

The WS protocol exposes **opaque, summary-level events by default** and **detail events only when explicitly opted in**.

### Opaque (default)

Job lifecycle:

```ts
job.queued       — track summary, no source info
job.progress     — { percent: 0..100, phase: "queued"|"acquiring"|"verifying"|"writing"|"done", playable: boolean }
job.playable     — { path } (no quality leaked to default subscribers)
job.upgraded     — { newPath }
job.completed    — { finalPath }
job.failed       — { reason: FailureReason, permanent: boolean } (closed enum, see ADR-0009)
job.review-pending — { reason }
```

Group lifecycle:

```ts
group.created    — { kind, title, trackCount }
group.progress   — { percent, playableCount, completedCount }
group.completed  — { finalProgress }
```

The single `percent` per job is computed from the underlying two-pipeline state (see §10 of ARCHITECTURE.md for the mapping table). It is **monotonically non-decreasing** and always reaches 100 on success.

`phase` is intentionally vague — "acquiring" covers both rip and endgoal pipelines without disclosing the split.

### Verbose (opt-in)

`subscribe { kind, verbose: true }` adds `job.detail` and `group.detail` events on top of the opaque ones. Detail events carry source IDs, race outcomes, attempt timings, verification scores, rate-limit hits, fingerprint similarities — everything the debug UI needs.

**Inheritance**: `subscribe { kind: "group", verbose: true }` automatically marks all child jobs verbose for that subscription.

**Replay**: server keeps last 200 detail events per active job in a ring buffer. Toggling `verbose: false → true` mid-session replays from the buffer head, so a debug UI joining late doesn't miss the early race detail.

### Strict failure enums, human-readable on client

Failure reasons are a closed `kind` enum (`not_found`, `all_sources_failed`, `verification_failed`, `service_restart`, `user_cancelled`, `rate_limited_global`, `fingerprint_mismatch_strict`, `network_unreachable`, `unsupported_input`). The server never sends free-text reason strings. The client owns the human-readable mapping (`FAILURE_MESSAGES` registry shipped in `contracts/`), so localisation is trivial. Adding a new reason = contract bump.

## Consequences

- **Positive**: Clean separation between the public surface (opaque, stable) and internal surface (verbose, evolves freely). DJ-facing UIs stay simple. Internal scheduler changes don't break clients. Localisation is trivial because all reason text is client-side. Debug UI gets full visibility for free.
- **Negative / accepted trade-offs**: Server keeps a small ring buffer (~20 KB/active job) to enable replay. For 100 active jobs that's 2 MB; trivial. Adding new opaque events still requires a contract update; we accept that as the cost of stability.
- **Neutral**: The single-bar percent calculation has subjective weighting (we chose 0-25 for rip, 25-90 for endgoal stream, 90-95 for verify, 95-99 for write). Tunable in config if a downstream UI prefers different curve.

## Alternatives considered

- **Always send full detail, let clients filter**: simpler server, noisier protocol, harder backwards compatibility (every detail field becomes a public surface).
- **Two separate WS endpoints (`/stream` and `/stream-verbose`)**: doubles connection count for debug clients, complicates subscription semantics.
- **Free-text reason strings**: kills localisation and machine-readable error handling. No.

## References

- §10 of `ARCHITECTURE.md`.
- `contracts/ws-protocol.schema.json` for the closed enums.
