# ADR-0009: WebSocket as the single client protocol

- **Status:** accepted
- **Date:** 2026-05-03

## Context

This service is the backbone for other apps — a DJ-facing mobile UI for crowd requests, a control deck, a dashboard, and a debug companion. The communication needs:

1. **Bidirectional streaming**: clients submit requests; the server pushes job/group lifecycle and progress events.
2. **Multi-client**: several apps may be connected at the same time (DJ phone + laptop monitor + dashboard).
3. **Subscription model**: each client cares about a subset of activity (a single group, a single job, or the firehose).
4. **Low latency**: progress updates must reach the DJ within hundreds of milliseconds.
5. **Local network only** (per requirements). No public exposure, no auth in v1.
6. **Trivial to debug** with a small companion web app.

REST + polling is wasteful and slow for live progress. Server-Sent Events is one-way. gRPC adds a heavy ecosystem buy-in for clients we haven't built yet. Plain WebSockets give us the bidirectional, multi-client, push semantics we want, with a near-universal browser API for the debug UI.

## Decision

- **Single WebSocket endpoint**: `ws://host:8787/v1/stream`.
- **JSON envelopes**, all messages tagged with a `type` discriminator. Validated with zod at the boundary.
- **Versioned envelope**: every server-initiated message carries `v: 1`. Additive evolution within `v: 1`; breaking changes bump major. Clients pin major version.
- **No HTTP companion endpoint** in v1. If clients need one-shot reads later, we add `GET /v1/jobs/:id` (deferred).
- **Auth: none** for v1; trusted local-network assumption. If we ever expose this beyond LAN, see future-work for auth options.
- **Subscription scopes**: `{ kind: "job"|"group"|"all", verbose?: boolean }`. After `request.track` / `request.bulk`, requester is auto-subscribed to that job/group. `subscribe` / `unsubscribe` are explicit for cross-client visibility (e.g., a dashboard subscribes to `{ kind: "all" }`).
- **Backpressure**: when `ws.bufferedAmount > 8 MB` for a connection, drop `*.progress` events to that client (lossy by design); never drop lifecycle events.
- **Replay buffer** for verbose mode: per active job, server keeps the last 200 detail events in memory (~20 KB/job). When a client toggles `verbose: false → true`, the buffer is replayed. Buffer cleared when job reaches a terminal status.
- **Disambiguation**: when free-text input yields multiple candidates, server emits `disambiguate` with `expiresAt`. Client responds with selection within the window or the candidates are discarded.
- **Cancellation** is a first-class client → server message: `{ type: "cancel", target: { kind: "job"|"group", id } }`.

The full schema is the JSON Schema in `contracts/ws-protocol.schema.json`. Both `@crate/core` and `@crate/debug-ui` independently validate their internal protocol modules against it in CI.

## Consequences

- **Positive**: One protocol, one port, easy mental model. Browser debug UI connects with two lines of code. Multi-client is free. Real-time progress is free. Test contracts can simulate clients with the standard `ws` library.
- **Negative / accepted trade-offs**: No auth or transport encryption — acceptable for local-network deployment, dangerous if exposed. We document this loudly in the README and `readyz` does not check auth presence.
- **Neutral**: WebSocket reconnect logic is the client's problem; we provide a `pong` for client-side liveness checks.

## Alternatives considered

- **REST + Server-Sent Events**: works, but two endpoints to maintain (one POST for requests, one GET for the event stream). Cancellation requires a third. Not worth the split.
- **gRPC bidirectional streaming**: powerful, typed, but pulls in `protoc`, generated clients per language, and an HTTP/2 transport. Overkill for one server and a debug UI.
- **GraphQL subscriptions**: doable but bolts a query language on top of what is essentially RPC. Not a fit.
- **MQTT**: pub/sub is a clean fit, but requires running a broker (Mosquitto) as a separate process, defeating the "single Node binary" goal.

## References

- §10 of `ARCHITECTURE.md`.
- ADR-0010 (opaque-progress mode) and ADR-0011 (decoupled debug UI).
