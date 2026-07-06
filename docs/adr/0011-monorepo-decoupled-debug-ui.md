# ADR-0011: Monorepo with decoupled debug UI

- **Status:** accepted
- **Date:** 2026-05-03

## Context

We need a small browser-based debug companion that connects to the crate WS server, lets a developer fire test requests, and visualises the verbose event stream (attempts, races, verification details). It is strictly a **debugging tool** — not a production UI, not deployed alongside the service. Other consumers (DJ mobile app, control deck) are out of scope for this repo.

We also need a way to keep the server's protocol and the debug UI's idea of the protocol from drifting. Two natural approaches:

A. **Coupled**: debug UI imports types from `@crate/core/protocol`. Single source of truth in code; risks accidental coupling beyond the protocol module.
B. **Decoupled**: debug UI is an independent package with its own protocol module; both are validated against a shared JSON Schema in `contracts/`.

The user has explicitly chosen B: the debug UI must live in the monorepo but be **completely unlinked** from `@crate/core`. This forces a clean boundary that mirrors how a real third-party client would integrate.

## Decision

- **Monorepo layout** with pnpm workspaces:
  - `packages/core/` — `@crate/core`, the headless service.
  - `apps/debug-ui/` — `@crate/debug-ui`, the debug companion. **Zero imports from `@crate/core`**, including type-only imports.
  - `contracts/` — language-agnostic protocol spec (JSON Schema at minimum, possibly examples/fixtures) consumed by both packages.
- **Shared spec, not shared code**:
  - `packages/core/src/protocol/messages.ts` defines zod schemas + derived TS types for the server side.
  - `apps/debug-ui/src/protocol/messages.ts` defines its own zod schemas + derived TS types for the client side.
  - Both files are independently exported as JSON Schema (via `zod-to-json-schema`) in CI and diffed against `contracts/ws-protocol.schema.json`. Drift fails the build.
- **Debug UI tech**: vanilla TypeScript + Vite + Solid.js. Solid because reactive signals map naturally onto the WS event stream and the bundle stays small.
- **Debug UI scope**: connect to server URL (configurable via local storage), submit any input + mode, render single-bar opaque view OR full verbose breakdown (toggleable), surface disambiguation candidates, surface review-pending jobs, manual cancel. Smoke-test only — no UX polish.
- **Hosting**: developers run with `pnpm --filter @crate/debug-ui dev`. No production hosting concern; the UI is not part of the service deliverable.

## Consequences

- **Positive**: The boundary the debug UI crosses is exactly the boundary a third-party integrator would cross — so we discover protocol pain early. Server can refactor its internal types freely without touching the debug UI. CI prevents silent drift via the JSON-Schema diff. pnpm workspaces give us one install, one lint config, one test runner.
- **Negative / accepted trade-offs**: The protocol is duplicated (zod schema in two packages). When we add a new message type, we must edit both files and update `contracts/`. CI catches drift, so the cost is human, not silent. We accept this as the price of decoupling.
- **Neutral**: The debug UI build artefact is not currently shipped anywhere. If we want to colocate it later (`packages/core/public/`), nothing about the boundary changes — it's just a deployment decision.

## Alternatives considered

- **Coupled** (debug UI imports from `@crate/core/protocol`): fewer files to update; high risk of creep where the UI imports more than it should. User explicitly rejected.
- **Generate TS types from JSON Schema**: deletes one of the duplications, but `json-schema-to-typescript` output is awkward and loses zod's runtime parsing. Could revisit if duplication becomes painful.
- **Polyrepo**: would split CI, lint, tooling, and require a release-and-publish dance to consume the contract from `core`. Worse DX for one developer.
- **No debug UI**: would leave us debugging via raw `wscat` and JSON dumps. Unacceptable.

## References

- pnpm workspaces: https://pnpm.io/workspaces
- Solid.js: https://www.solidjs.com
- zod-to-json-schema: https://github.com/StefanTerdell/zod-to-json-schema
