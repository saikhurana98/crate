# ADR-0004: SQLite with hand-rolled migrations; no ORM

- **Status:** accepted
- **Date:** 2026-05-03

## Context

We need persistent state for tracks, jobs, job groups, attempts, source health, cover art, and schema migrations. The data fits comfortably in a single-process embedded database (single-writer model from §13 of ARCHITECTURE.md). The DB file lives on the same NAS as the music library, so it's backed up alongside the music. Sizes for v1 expectations: thousands of tracks, low tens of thousands of attempt rows. Well under SQLite limits.

Query patterns are small in number and shape:

- `SELECT * FROM jobs WHERE id = ?`
- Optimistic UPDATE with `WHERE status = ?` for state transitions.
- `SELECT * FROM jobs WHERE status IN (...) AND next_retry_at <= ?` for the upgrade-daemon sweep.
- Aggregate progress query for groups.
- A handful of inserts on attempt logging, health updates.

We do not need cross-database portability, complex query composition, or a unit-of-work pattern.

## Decision

- **Storage engine**: SQLite via `better-sqlite3` (synchronous, fast, mature). Single process, single writer, multiple concurrent readers via WAL.
- **PRAGMAs at startup**: `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `temp_store=MEMORY`, `mmap_size=268435456`, `busy_timeout=5000`.
- **No ORM, no query builder**. Repository pattern with hand-written SQL strings, parametrised queries throughout. Each repository function returns `Result<T, RepoError>`.
- **Migrations**: numbered SQL files in `infrastructure/sqlite/migrations/NNN_description.sql`. A small migration runner (~80 lines) reads files in order, applies them in transactions, records `(version, applied_at, description)` in `schema_migrations`. No down-migrations.

The full schema is the source of truth in `001_init.sql`; subsequent migrations are additive.

## Consequences

- **Positive**: Zero ORM overhead and zero hidden N+1s. Schema is a real SQL file, reviewable directly. Migration runner is 80 lines we can reason about completely. WAL gives us read-during-write concurrency, so the upgrade-daemon and metrics endpoint don't block job state transitions.
- **Negative / accepted trade-offs**: Refactors of column names require touching every query referencing them. We accept this — fewer than 30 queries total in v1. No type-level guarantee that a query's columns match a TS type; we mitigate with a thin runtime mapper in each repository function and zod schemas validating row shapes.
- **Neutral**: Single-process / single-writer assumption is baked in. Multi-instance daemons would require pessimistic claim columns (deferred — see future-work ADR).

## Alternatives considered

- **Drizzle ORM**: typesafe SQL builder, schema-first, would give us column-name refactor safety. The cost is buying into Drizzle's evolution and a layer of generated migration code we'd have to learn. The benefit is small at our query count. Reconsider if query count grows beyond 100.
- **Kysely**: typesafe SQL builder without ORM behaviours. Same pros as Drizzle, smaller surface. Genuinely tempting; held in reserve for v2 if hand-rolled queries become tedious.
- **Prisma**: heavier than we need; generates a runtime client; opaque migration model; mediocre SQLite support. No.
- **Postgres**: overkill for embedded use, requires separate process/service to run, and we don't need any Postgres-specific features. NAS-local SQLite wins on ops simplicity.
- **Knex**: ageing, less typesafe, worse than Kysely for our needs.

## References

- `better-sqlite3` performance notes: https://github.com/WiseLibs/better-sqlite3/blob/master/docs/performance.md
- SQLite WAL mode: https://www.sqlite.org/wal.html
