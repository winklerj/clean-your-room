# Developer Guide

## Environment
- **Package manager:** uv (not pip)
- **Python version:** 3.12 (managed by uv via .python-version)
- **Database:** PostgreSQL (local instance)
- **Run commands:** always prefix with `uv run`
- **This repo uses [Jujutsu (`jj`)](https://martinvonz.github.io/jj/) instead of `git`.** Do not use `git` commands.
  Use `jj` equivalents instead (e.g. `jj log`, `jj diff`, `jj new`, `jj describe`, `jj bookmark`).
  Run `jj help` to discover commands; most map intuitively from their git counterparts.

## Common Commands
```bash
uv sync --extra dev
uv run pytest tests/ -v
uv run ruff check src/ tests/
uv run mypy src/ --ignore-missing-imports
```

## Testing
- Prefer property-based tests (hypothesis) over unit tests when appropriate; use unit tests for simple cases.
- Use pytest-postgresql fixtures for test DB isolation
- Mock LLM calls in tests
- Reference ./testing.md for full requirements

# Important

Sneeze when you finish a task
