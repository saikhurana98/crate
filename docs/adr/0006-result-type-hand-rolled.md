# ADR-0006: Hand-rolled `Result<T, E>` for typed errors at module boundaries

- **Status:** accepted
- **Date:** 2026-05-03

## Context

We want errors at module boundaries (port methods returning data from external systems) to be **part of the type signature**, not surprises caught with `try/catch`. Throwing as a control-flow mechanism makes it impossible for the type checker to enforce error handling, and it makes contract tests harder to write (you can't easily assert "this method returns a tagged `rate_limited` error" without try/catch ceremony).

Options:
- A library like `neverthrow` or `ts-results`.
- The Effect-TS ecosystem (`Effect`, `Either`).
- A small hand-rolled type.

The codebase will use `Result` heavily — every port method returns one, every use-case threads them. Whatever we pick has to be ergonomic in the standard `async/await` style, work with `Promise.race` and `Promise.allSettled`, and not require teaching newcomers a new ecosystem.

## Decision

Hand-roll a minimal `Result<T, E>` with no dependencies:

```ts
// domain/result.ts
export type Result<T, E> =
  | { readonly ok: true;  readonly value: T }
  | { readonly ok: false; readonly error: E };

export const Result = {
  ok:  <T>(value: T):  Result<T, never> => ({ ok: true, value }),
  err: <E>(error: E):  Result<never, E> => ({ ok: false, error }),
  isOk:  <T, E>(r: Result<T, E>): r is { ok: true;  value: T } => r.ok,
  isErr: <T, E>(r: Result<T, E>): r is { ok: false; error: E } => !r.ok,

  map:    <T, U, E>(r: Result<T, E>, f: (t: T) => U): Result<U, E> =>
    r.ok ? Result.ok(f(r.value)) : r,

  mapErr: <T, E, F>(r: Result<T, E>, f: (e: E) => F): Result<T, F> =>
    r.ok ? r : Result.err(f(r.error)),

  flatMap: <T, U, E>(r: Result<T, E>, f: (t: T) => Result<U, E>): Result<U, E> =>
    r.ok ? f(r.value) : r,

  unwrapOr: <T, E>(r: Result<T, E>, fallback: T): T =>
    r.ok ? r.value : fallback,
};
```

**Rules:**

1. Every port method returns `Promise<Result<T, E>>` where `E` is a tagged union of named failures. No throwing across port boundaries.
2. `application/` uses `if (result.ok)` for branching. No `try/catch` except around clearly demarcated foreign-call sites in `adapters/` (HTTP libraries, Playwright, sqlite driver).
3. Adapters convert thrown exceptions into `Result.err({ tag: ... })` at their outer edge.
4. Truly unrecoverable conditions (DB corruption, out-of-disk) may throw — they propagate to a top-level handler that crashes the process via `process.exit(1)`. We do not pretend to recover from invariant violations.

## Consequences

- **Positive**: Errors are typed, named, and exhaustively pattern-matchable. New error variants force compile errors at every consumer. No lock-in to a third-party library; the entire `Result` impl is < 50 lines and reviewable in one minute.
- **Negative / accepted trade-offs**: A small amount of ceremony at every call site (`if (r.ok) ... else ...`). We accept this as the cost of enforced error handling. We do not provide do-notation or pipe operators; if call chains get long, refactor into smaller functions.
- **Neutral**: Naming convention `{ ok, value, error }` matches `neverthrow` and many other libraries, so people familiar with those will find ours instantly readable.

## Alternatives considered

- **`neverthrow`**: nice library, similar shape, but adds a dep and a `ResultAsync` wrapper that fights against `async/await`. We prefer plain `Promise<Result<T, E>>`.
- **Effect-TS / `Either`**: powerful but a heavy ecosystem buy-in (effect tracing, dependency injection via Layer, etc.) that we do not need.
- **Throwing typed exceptions**: TypeScript can't represent thrown types. Doesn't satisfy the "errors in signature" requirement.
- **Tuple `[result, err]` Go-style**: ergonomic enough but loses the type-narrowing behaviour of discriminated unions.
- **`fp-ts`**: too academic for this team and codebase. We'd spend more time learning combinators than shipping features.

## References

- Discriminated unions in TS: https://www.typescriptlang.org/docs/handbook/2/narrowing.html#discriminated-unions
