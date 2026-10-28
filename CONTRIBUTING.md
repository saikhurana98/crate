# Contributing

Read [AGENTS.md](AGENTS.md) for repository structure, coding conventions, and
the complete verification commands. Use a focused Conventional Commit message,
add regression coverage for behavior changes, and update source documentation
when adding or changing a provider.

Before opening a pull request, run:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
cd apps/console && npm ci && npm run build
```

Do not include provider credentials, downloaded media, runtime databases, or
session files. Use placeholders in examples and redact secrets from logs.
