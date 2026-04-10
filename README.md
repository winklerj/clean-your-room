# Build Your Room

Local agent orchestrator for parallel coding pipelines — Claude Agent SDK + Codex app-server.

## Overview

**build-your-room** manages parallel coding pipelines against local repositories. Each pipeline follows an explicit directed stage graph — spec authoring, review loops, implementation planning, task-by-task coding, code review, and validation — executed by Claude SDK and Codex app-server sessions working in concert.

Key capabilities:
- **10+ parallel pipelines**, each with its own isolated repo clone
- **HTN task decomposition** — hierarchical task network with atomic claims, readiness propagation, and postcondition verification
- **Stage graph orchestration** — explicit directed graphs with bounded review/fix loops, back-edges, and escalation exits
- **Context rotation** — configurable threshold (default 60%) with same-task resume for implementation claims
- **Dual agent backends** — Claude Agent SDK (live session handles) and Codex app-server (stdio JSON-RPC)
- **Human-in-the-loop** — escalation queue for design decisions, max-iteration exhaustion, and review divergence
- **Browser validation** — dev-browser integration with recording for web UI projects
- **Dashboard** — HTMX-powered monitoring with pipeline cards, stage graph visualization, HTN task trees, and live log streaming

## Prerequisites

- Python 3.12
- [uv](https://docs.astral.sh/uv/getting-started/installation/) package manager
- PostgreSQL (local instance)
- `ANTHROPIC_API_KEY` environment variable (for Claude Agent SDK)
- `OPENAI_API_KEY` environment variable (for Codex app-server, optional)
- Codex CLI on PATH (optional, for Codex stages)
- Node.js (optional, for dev-browser validation)

## Install

```bash
uv sync --extra dev
```

## Database Setup

Create the PostgreSQL database:

```bash
createdb build_your_room
```

The schema is auto-created on first startup. To use a custom DSN:

```bash
export DATABASE_URL="postgres:///build_your_room"
```

## Run

```bash
uv run uvicorn build_your_room.main:app --reload --port 8317
```

Open [http://localhost:8317](http://localhost:8317) to access the dashboard.

## Quick Workflow

1. Navigate to **Repos** and add a local repository path
2. Navigate to **Pipeline Defs** and create a stage graph definition (or use a built-in one)
3. Create a new pipeline against your repo with the chosen definition
4. Monitor progress on the dashboard — pipeline cards show stage progress, HTN task completion, context usage, and cost
5. Respond to escalations when agents need human decisions
6. Clean up completed pipeline clones from the dashboard

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `BUILD_YOUR_ROOM_DIR` | `~/.build-your-room` | Base directory for clones, logs, artifacts, state |
| `DATABASE_URL` | `postgres:///build_your_room` | PostgreSQL DSN |
| `DEFAULT_CLAUDE_MODEL` | `claude-sonnet-4-6` | Default model for Claude stages |
| `DEFAULT_CODEX_MODEL` | `gpt-5.1-codex` | Default model for Codex stages |
| `SPEC_CLAUDE_MODEL` | `claude-opus-4-6` | Model for spec authoring |
| `CONTEXT_THRESHOLD_PCT` | `60` | Context usage threshold before rotation |
| `MAX_CONCURRENT_PIPELINES` | `10` | Semaphore limit for parallel pipelines |
| `PIPELINE_LEASE_TTL_SEC` | `30` | Lease validity without heartbeat |
| `PIPELINE_HEARTBEAT_INTERVAL_SEC` | `10` | Heartbeat renewal interval |
| `ANTHROPIC_API_KEY` | _(required)_ | Claude Agent SDK API key |
| `OPENAI_API_KEY` | _(required for Codex)_ | Codex app-server API key |
| `DEVBROWSER_SKILL_PATH` | `~/.claude/skills/dev-browser` | Path to dev-browser runner |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Development

```bash
uv run pytest tests/ -v                        # Run tests (876 tests)
uv run ruff check src/ tests/                   # Lint
uv run mypy src/ --ignore-missing-imports       # Type check
```

Tests use `pytest-postgresql` for ephemeral per-test PostgreSQL databases and `hypothesis` for property-based testing. See `testing.md` for the full testing strategy.

This project uses [Jujutsu (`jj`)](https://martinvonz.github.io/jj/) for version control, not git.

## Architecture

```
Pipeline Lifecycle:
  Clone repo → Acquire lease → Follow stage graph edges →
  Per-stage: create stage row → run agent adapter → verify artifacts →
  Update head_rev → resolve next edge → Loop or complete

Stage Types:
  spec_author → impl_plan → impl_task → code_review → validation

Agent Adapters:
  ClaudeAgentAdapter  — Claude SDK live session handles
  CodexAppServerAdapter — stdio JSON-RPC with persistent threads

Core Components:
  PipelineOrchestrator — stage graph dispatch, leases, recovery
  HTNPlanner           — task graph CRUD, atomic claims, readiness
  ContextMonitor       — usage tracking, rotation decisions
  CloneManager         — repo cloning, workspace ops, cleanup
  WorkspaceSandbox     — path guard for agent confinement
  CommandRegistry      — repo-standard verification commands
  BrowserRunner        — dev-server + dev-browser bridge
```

## Project Structure

```
src/build_your_room/
├── main.py              # FastAPI app, lifespan, route registration
├── config.py            # Env vars, paths, PipelineConfig dataclass
├── models.py            # Internal dataclasses
├── db.py                # PostgreSQL schema DDL, pool management
├── streaming.py         # LogBuffer pub/sub for SSE
├── orchestrator.py      # PipelineOrchestrator — core engine
├── stage_graph.py       # Typed StageGraph with frozen nodes/edges
├── clone_manager.py     # Repo cloning, workspace refs, cleanup
├── sandbox.py           # Workspace roots + path guard
├── tool_profiles.py     # Per-stage tool allowlists
├── command_registry.py  # Verification command templates
├── context_monitor.py   # Context usage tracking + rotation
├── htn_planner.py       # HTN task graph management
├── browser_runner.py    # Dev-server + dev-browser bridge
├── adapters/
│   ├── base.py          # AgentAdapter/LiveSession protocols
│   ├── claude_adapter.py
│   └── codex_adapter.py
├── stages/
│   ├── review_loop.py   # Generic review loop
│   ├── spec_author.py
│   ├── impl_plan.py
│   ├── impl_task.py
│   ├── code_review.py
│   └── validation.py
├── routes/
│   ├── dashboard.py     # Main dashboard + pipeline cards
│   ├── escalations.py   # Escalation queue
│   ├── pipelines.py     # Pipeline CRUD + detail + cleanup
│   ├── pipeline_defs.py # Pipeline definition builder
│   ├── repos.py         # Repo management
│   └── prompts.py       # Prompt CRUD
└── templates/           # Jinja2 + HTMX templates
```
