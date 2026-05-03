# ADR-0007: zod for runtime validation at every boundary

- **Status:** accepted
- **Date:** 2026-05-03

## Context

TypeScript types disappear at runtime. Anywhere data crosses from outside the program (HTTP responses, WebSocket client messages, SQLite row reads, JSON config files) we have no compile-time guarantee that the data matches our types. A bad Spotify API response or a corrupt SQLite row can otherwise surface as a `TypeError: undefined.title` deep in the stack.

The boundaries we must validate:

1. **WebSocket inbound messages** — clients send JSON; we must reject malformed shapes with a clear error.
2. **Config file load** — `lbmd.config.json` must validate before the daemon boots; bad config = fail-fast.
3. **Source-adapter responses** — third-party JSON (MusicBrainz, Spotify, AcoustID) is not trustworthy; parse, don't blindly cast.
4. **SQLite row reads** — repository functions parse rows into domain types via a runtime check, in case schema and code drift.
5. **External-process output** — `fpcalc`, `yt-dlp` JSON output.

We need a validator that derives TS types from schemas (so we don't duplicate definitions), handles tagged unions cleanly, and gives readable errors with paths.

## Decision

Use **zod** for all runtime validation at boundaries. Specifically:

- Every WS message kind has a `z.discriminatedUnion("type", [...])` schema in `protocol/messages.ts`. The TS types are derived via `z.infer`.
- Config schema is a single `z.object({ ws: ..., ops: ..., nas: ..., ... })`. Defaults baked in via `.default(...)`. Loaded once, all downstream code reads the parsed shape.
- Source adapters parse their HTTP responses via per-source zod schemas before returning. Failures become `SourceError.transport` with the zod issue path in `cause`.
- Repository functions parse rows via zod schemas matching the table shape.

**No `as` casts on external data anywhere**. CI lint rule blocks `as Foo` casts in `adapters/**` and `infrastructure/**` except for clearly justified cases (commented).

**Performance**: zod's per-call overhead is sub-millisecond for our schema sizes. Negligible compared to network IO.

## Consequences

- **Positive**: Bad data fails fast with a precise location ("invalid type at `body.tracks[3].duration`"). TS types and runtime checks share one source of truth. Refactoring a schema automatically refactors derived types. WS protocol drift is detectable in tests.
- **Negative / accepted trade-offs**: Mild verbosity at boundaries — every adapter has a parse step. We accept this as the price for runtime safety.
- **Neutral**: We could swap zod for `valibot` (smaller bundle) later; the public API surface (`.parse`, `.safeParse`, `z.infer`) is similar and migration would be mostly mechanical. The debug UI does want a small bundle, so we may revisit zod-vs-valibot for that package specifically — but `@lbmd/core` stays on zod.

## Alternatives considered

- **`io-ts`**: more academic, less ergonomic, weaker error messages.
- **`yup`**: widely used in form validation, weaker TS inference, less fluent for tagged unions.
- **`valibot`**: ~10x smaller bundle, similar API, less mature ecosystem. Worth revisiting in 6 months. Bundle size irrelevant for the server.
- **TypeBox / JSON Schema**: useful for cross-language schemas (we do use JSON Schema in `contracts/`) but worse DX inside TS code.
- **Hand-rolled type guards**: tedious, error-prone, no path information in errors.

## References

- zod docs: https://zod.dev
- `contracts/ws-protocol.schema.json` is generated from the zod schemas via `zod-to-json-schema` in CI.
