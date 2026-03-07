# How to run tests and checks during development

## Install development dependencies

```bash
uv sync --extra dev
```

## Run the test suite

```bash
uv run pytest tests/ -v
```

The pytest configuration sets `asyncio_mode = "auto"`, so async test functions run without manual decoration.

## Lint the codebase

```bash
uv run ruff check src/ tests/
```

## Run type checking

```bash
uv run mypy src/ --ignore-missing-imports
```

## Writing new tests

Prefer **property-based tests** using Hypothesis over standard unit tests when the function under test has a broad input domain or mathematical invariants. Use unit tests for simple, deterministic cases.

## Version control note

This project uses **jj (Jujutsu)**, not git. Use `jj` commands for all version control operations (e.g., `jj log`, `jj diff`, `jj new`, `jj describe`).
