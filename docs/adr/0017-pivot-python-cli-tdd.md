# ADR-0017: Pivot to Python CLI, test-first

- **Status:** accepted
- **Date:** 2026-07-02
- **Deciders:** @saikhurana98
- **Supersedes:** ADR-0001, ADR-0009, ADR-0010, ADR-0011
- **Amends:** ADR-0003, ADR-0006, ADR-0007, ADR-0012, ADR-0014, ADR-0016

## Context

The original stack was TypeScript/Node/pnpm with a WebSocket-only protocol and a decoupled Solid.js debug UI, driven by an elaborate multi-agent conductor build process. The owner has since decided:

1. **Language is Python**, not TypeScript.
2. **The client is a CLI**, not a WebSocket server + web UI. The frontend is out of scope.
3. **Development is test-first (TDD)** — red/green/refactor, coverage-gated.

The core *design* decisions (tiered cascade, verification, hexagonal boundaries, SQLite state, NAS atomic writes, the Explo slskd shim) are language-agnostic and stand. What changes is the language, the client transport, and the build process.

## Decision

### Stack

- **Python ≥ 3.11**, `src/` layout, package `lbmd`.
- **uv** for env + dependency management (`uv sync`, `uv run`); `pyproject.toml` is the single manifest. Replaces pnpm.
- **Typer** for the CLI. Entry point `lbmd` → `lbmd.cli.main:app`.
- **pytest** (+ `pytest-asyncio`, `pytest-cov`) for tests. **ruff** for lint + format. **mypy --strict** for types.
- **asyncio + httpx** for the parallel intra-tier source races (replaces Node's `AbortSignal`/`Promise.race` with `asyncio` task cancellation).
- **pydantic v2** for boundary validation and the slskd DTOs (replaces zod, [ADR-0007](0007-zod-runtime-validation.md)).

### Client = CLI (supersedes ADR-0009/0010/0011)

- The interactive client is the `lbmd` CLI: `lbmd download <query|url>`, `lbmd version`, and `lbmd serve` (the Explo shim, below). No WebSocket protocol, no `contracts/ws-protocol.schema.json`, no debug UI, no monorepo.
- Progress is rendered directly in the terminal (rich/Typer), not streamed as opaque WS events. The opaque-vs-verbose idea from [ADR-0010](0010-opaque-progress-stats-for-nerds.md) survives only as a `--verbose` flag.

### Explo integration stays (amends ADR-0014)

Explo talks HTTP REST, so the slskd-compatible shim is still required — it becomes a Python HTTP server (Starlette/FastAPI or stdlib) launched by `lbmd serve`, exposing the same slskd endpoint subset ([ADR-0014](0014-slskd-compat-shim.md)). The shim is the only network server lbmd runs; everything else is CLI.

### Errors: exceptions, not `Result` (supersedes ADR-0006)

Python's idiom is exceptions. Replace the hand-rolled `Result<T,E>` with a typed exception hierarchy (`LbmdError` base + specific subclasses) surfaced at boundaries. Strict failure enums from the protocol become exception types / error codes on the CLI and shim.

### Hexagonal layout preserved (ADR-0002 stands)

`domain → ports → application`; `adapters` + `infrastructure` implement ports; `composition` wires. Ports are `typing.Protocol` classes. Layering enforced by ruff `flake8-tidy-imports` banned-api (domain/ports may not import adapters/infrastructure) plus contract tests parametrised over adapter fixtures.

### Build process simplified

The autonomous conductor + worker-routine machinery ([IMPLEMENTATION.md](../../IMPLEMENTATION.md)) was scaled for a large TS monorepo. For a single-package Python CLI, work proceeds as ordinary test-first feature branches + PRs with a CI gate (lint, format, mypy, pytest ≥ 90% coverage). `IMPLEMENTATION.md`/`WORK.md` to be reworked or retired in a follow-up.

### Unaffected

[ADR-0002](0002-hexagonal-ports-adapters.md) (hexagonal), [ADR-0004](0004-sqlite-hand-rolled-migrations.md) (SQLite via stdlib `sqlite3`), [ADR-0005](0005-chromaprint-acoustid.md) (fpcalc/AcoustID via subprocess), [ADR-0008](0008-nas-atomic-write.md) (atomic temp+rename), [ADR-0013](0013-multi-resolver-chain.md) (resolver chain). [ADR-0012](0012-playwright-browser-pool.md) amended: Playwright **for Python**. [ADR-0016](0016-docker-packaging.md) amended: container base is Python + bundled bins, not Node/pnpm.

## Consequences

- **Positive:** Simpler stack for a solo CLI. Python's ecosystem (yt-dlp, mutagen, playwright, pyacoustid) fits the audio domain natively — several sources are Python-native already. TDD + coverage gate from line one. No frontend to maintain.
- **Negative / accepted trade-offs:** Throws away the TS bootstrap scaffold (never merged — cost is sunk design time only). Several ADRs superseded/amended; history preserved via this ADR rather than rewriting them. Python `asyncio` cancellation is less ergonomic than `AbortSignal` for the race primitive — accepted.
- **Neutral:** `ARCHITECTURE.md`, `IMPLEMENTATION.md`, `WORK.md` now describe a TS design; they trail this ADR until reworked. This ADR is authoritative on any conflict.

## Alternatives considered

- **Keep TypeScript, add a CLI.** Rejected — owner wants Python; the WS/UI apparatus is unwanted overhead for a CLI.
- **Rewrite every affected ADR in place.** Rejected — falsifies the decision record. A superseding ADR is the honest mechanism.
- **Poetry / pip-tools instead of uv.** uv chosen for speed and single-tool env+lock; reversible early if it disappoints.

## References

- [ADR-0002](0002-hexagonal-ports-adapters.md), [ADR-0014](0014-slskd-compat-shim.md), [ADR-0015](0015-tier-cascade-scheduler.md), [ADR-0016](0016-docker-packaging.md)
- `core-design.md` — original Tier 1/2/3 requirement (unchanged by this pivot).
