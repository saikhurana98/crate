---
type: Note
---

# Repository Guidelines

## Project Structure & Module Organization

`src/crate/` is the Python 3.11 package. Keep pure rules in `domain/`, use cases in `application/`, external integrations in `adapters/` and `infrastructure/`, and concrete wiring in `composition/`. `cli/` exposes the Typer command. The Vite/TypeScript console lives in `apps/console/src/`; its static assets are in `apps/console/public/`. Python tests mirror package areas under `tests/`. Design decisions live in `docs/adr/`; for implementation details, favor current code and ADR-0017 or newer over older TypeScript-era plans.

## Build, Test, and Development Commands

Run these from the repository root:

```bash
uv sync                         # install locked Python and development dependencies
uv run crate --help             # inspect CLI commands
uv run pytest                   # run tests with the 90% coverage gate
uv run ruff check .             # lint, including package-layer import rules
uv run ruff format --check .    # verify formatting
uv run mypy                     # strict type checking
cd apps/console && npm ci && npm run build  # typecheck and build the console
```

For local console development, run `npm run dev` in `apps/console/`.

## Coding Style & Naming Conventions

Use four-space Python indentation, type annotations, snake_case modules and functions, and Ruff formatting (100-character line length). Ruff prevents `domain/` and `ports/` from importing adapters or infrastructure; keep dependencies pointed inward. Inject I/O collaborators into adapters so tests can use fakes. Use `uv` and `pyproject.toml` for Python dependencies; use the existing npm lockfile for the console.

## Testing Guidelines

Use pytest and pytest-asyncio. Name Python test files `test_*.py` and place them beside the corresponding area under `tests/`. Add regression tests for matching, source fallback, and file verification changes. Run a focused file with `uv run pytest tests/application/test_cascade.py`, then the full suite. Coverage must stay at or above 90%.

## Commits & Pull Requests

Recent commits use Conventional Commit prefixes, often with a scope: `feat(sync): ...`, `fix(spotify): ...`, `ci(deploy): ...`. Keep each commit focused. In PRs, explain what changed and why, list tests run, link related issues or ADRs, and include screenshots for console UI changes. Ensure Python lint, format, typecheck, tests, and the console build pass in CI.

## Security & Configuration

Keep credentials in local environment files; `.crate.env` and runtime databases are ignored by Git. Never commit tokens, downloaded music, or user data. Source integrations are opt-in through `CRATE_*` settings; check current code before using examples from older documentation.
