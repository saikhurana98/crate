# ADR-0001: Language, runtime, package manager — TypeScript + Node + pnpm

- **Status:** accepted
- **Date:** 2026-05-03

## Context

This is a long-running daemon that talks to many third-party services (HTTP, Telegram, Soulseek, browser-automated scrapers), spawns CLI subprocesses (`yt-dlp`, `fpcalc`, `flac`), and serves a WebSocket protocol. The most important properties for the codebase are:

1. **Strong static typing** at every boundary — adapter contracts must be enforceable, and we want refactors of port shapes to fail loudly.
2. **First-class async / streaming** — fetches are streamed, races are everywhere, cancellation must propagate.
3. **Mature ecosystem** for the integrations we need: Telegram clients, Playwright, SQLite drivers, stream processing, audio CLI wrappers.
4. **Test-first ergonomics** — fast watch mode, easy mocks at the port boundary.
5. **Single binary / single process** deployment is fine; we don't need multi-core CPU-bound parallelism.

## Decision

- **Language: TypeScript**, strict mode, with `strict: true`, `noUncheckedIndexedAccess: true`, `exactOptionalPropertyTypes: true`, `noImplicitOverride: true`.
- **Runtime: Node.js LTS** (≥20). Node-native `fetch`, `ReadableStream`, `AbortController`, `Worker`, `fs/promises` cover everything we need.
- **Package manager: pnpm** with workspaces. Strict by default (no phantom deps), fast installs, content-addressable store works well on NAS.
- **No bundler for `@crate/core`** — ship it as TS sources compiled to ESM with `tsc`. Bundling matters for the browser-side debug UI; not for the server.

## Consequences

- **Positive**: One language across server and debug UI keeps the mental model simple. Strict TS catches a class of bugs at refactor time. pnpm workspaces give us a clean, fast monorepo without a heavyweight tool like Nx.
- **Negative / accepted trade-offs**: We do not get the performance ceiling of Rust or Go. For this workload (network-bound, lots of streaming), Node is more than fast enough. Single-process model means we cannot pin pipelines to CPU cores; we accept this.
- **Neutral**: Node ESM resolution requires `.ts` / `.js` extensions in imports — convention enforced by linter.

## Alternatives considered

- **Rust**: maximum typesafety and performance, but ecosystem for Telegram + Playwright + sketchy-music-source HTTP scraping is much smaller; iteration speed during early architecture would suffer. Reconsider if this app scales to many simultaneous DJ sets and Node bottlenecks.
- **Go**: great for daemons and concurrency, but its type system is weaker (no sum types, no generic-friendly Result), which contradicts the "elegant typesafe" goal.
- **Python**: rich audio / metadata ecosystem (`mutagen`, `acoustid`, `yt-dlp` is Python-native) but type system is too loose for the contract-first approach we want, and async story is messier.
- **Bun**: tempting (faster startup, built-in SQLite, native test runner) but stability for long-running daemons with Playwright is still maturing. Revisit in a year.

## References

- Node 20 LTS feature notes: native `fetch`, `ReadableStream`, `AbortSignal.timeout`.
- pnpm workspaces docs: https://pnpm.io/workspaces.
