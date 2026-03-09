# Clean Your Room

A FastAPI web application that uses the Claude Agent SDK to run iterative AI agents on GitHub repositories, generating formal clean room specifications with provable properties, purity boundaries, and verification tooling.

## Getting Started

### Prerequisites

- Python 3.12
- [uv](https://docs.astral.sh/uv/getting-started/installation/) package manager
- An Anthropic API key exported as `ANTHROPIC_API_KEY`

### Install

```bash
uv sync --extra dev
```

### Run

```bash
uv run uvicorn clean_room.main:app --reload --port 8317
```

Open [http://localhost:8317](http://localhost:8317) to access the dashboard.

### Quick workflow

1. Click **Add Repo** and paste a GitHub URL
2. On the repo detail page, select a prompt and click **Create Job**
3. Watch the agent generate specs in real time via the SSE log stream
4. Find generated specs in `~/.clean-room/specs-monorepo/`

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CLEAN_ROOM_DIR` | `~/.clean-room` | Base directory for repos, specs, and database |
| `DEFAULT_MODEL` | `claude-sonnet-4-20250514` | Claude model used by the agent |

## Development

```bash
uv run pytest tests/ -v                        # Run tests
uv run ruff check src/ tests/                   # Lint
uv run mypy src/ --ignore-missing-imports       # Type check
```

This project uses [Jujutsu (`jj`)](https://martinvonz.github.io/jj/) for version control, not git.

## Documentation

Full documentation is in the [`docs/`](docs/index.md) directory, organized using the [Diataxis](https://diataxis.fr) framework:

- [**Tutorial**: Getting Started](docs/tutorials/getting-started.md) -- Step-by-step first run walkthrough
- **How-to Guides**
  - [Manage Prompts](docs/how-to/manage-prompts.md)
  - [Manage Repos](docs/how-to/manage-repos.md)
  - [Run Jobs](docs/how-to/run-jobs.md)
  - [Run Tests](docs/how-to/run-tests.md)
- **Reference**
  - [API Endpoints](docs/reference/api-endpoints.md)
  - [Configuration](docs/reference/configuration.md)
  - [Database Schema](docs/reference/database-schema.md)
  - [Project Structure](docs/reference/project-structure.md)
- [**Explanation**: Architecture](docs/explanation/architecture.md) -- Design decisions and trade-offs
