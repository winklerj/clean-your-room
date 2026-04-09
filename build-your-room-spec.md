# build-your-room specification

> Local agent orchestrator for parallel coding pipelines — Claude Agent SDK + Codex app-server

---

## Overview

**build-your-room** is a local agent orchestrator that manages parallel coding pipelines. Each pipeline is an explicit directed stage graph with bounded review/fix loops — spec authoring, review loops, implementation planning, task-by-task coding, and validation — executed by Claude Agent SDK sessions and Codex app-server sessions working in concert.

**Fork basis:** `winklerj/clean-your-room` → gutted and extended. We keep: FastAPI async-first, LogBuffer pub/sub for SSE, HTMX+Jinja2 templates, cooperative cancellation via asyncio.Event, `uv` package management.

**We replace:** The single-agent JobRunner with a pipeline orchestrator. The GitHub-repo-centric model with a local-repo-centric model. The specs-monorepo pattern (removed — primary artifacts live in the pipeline clone, while harness logs/review artifacts live in app-owned per-pipeline directories). SQLite+WAL/`aiosqlite` with PostgreSQL + `psycopg` async connections so the implementation matches this repo's execution contract.

**Key constraints:**
- 10+ parallel pipelines, each with its own clone of the repo
- Context compaction must be avoided — configurable threshold (default 60%), no 1M context window
- Human intervention only when escalated — the dashboard is the primary interface
- Agents use CLAUDE.md and AGENTS.md in the target repo for project-specific instructions
- dev-browser (SawyerHood) for browser-based validation with recording
- Property-based testing over unit tests: Hypothesis (Python), fast-check (JS), Bombadil (web UI)
- TLA+ reasoning (simulated, not generated) for formal verification of specs
- All agent and verifier side effects must stay inside the pipeline clone or app-owned `logs/`, `artifacts/`, and `state/` directories for that pipeline

**Parallelization strategy:**
- Pipeline-level: 10+ pipelines run in parallel, each on its own repo clone
- Stage-level: Sequential within a pipeline (sufficient when enough pipelines run concurrently)
- Task-level (future): Claude Code's Agent Teams beta (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) can parallelize work within the implementation stage. The team lead decomposes the HTN task list and spawns teammates that claim tasks from a shared task list, communicate via peer-to-peer messaging, and merge changes continuously. This requires Opus 4.6 and v2.1.32+. We design the HTN task schema to be compatible with this model — tasks have clear boundaries, preconditions, postconditions, and explicit claim leases that make them safe to parallelize.

**Clone management:**
- Each pipeline gets an isolated clone at `~/.build-your-room/clones/{pipeline_id}/`
- Each pipeline also gets app-owned support directories at `~/.build-your-room/pipelines/{pipeline_id}/logs/`, `artifacts/`, and `state/`
- `state/` stores recovery metadata, dirty-workspace snapshots, staged diffs, and persisted resume state
- Clones persist after completion for inspection
- Manual cleanup via dashboard button (per-pipeline or bulk "clean completed")

## Core invariants

- **SideEffectsContained**: agents, review tooling, and postcondition verifiers may write only under `pipeline.clone_path` plus the app-owned `logs/`, `artifacts/`, and `state/` directories for that pipeline. No adapter runs with full-access sandboxing, and no verifier executes raw model-authored shell.
- **RunningImpliesOwner**: any `running` pipeline, stage, session, or task claim must have a non-null owner token and an unexpired lease. If not, startup reconciliation must recover or downgrade it.
- **ValidStageTransition**: the next stage comes only from an explicit outgoing edge in the stage graph. Numeric `+1` progression is forbidden.
- **UniqueTaskClaim**: at most one live lease may own a primitive HTN task at a time.
- **ClaimedTaskResumedOrReleased**: if an `impl_task` session rotates, crashes, is cancelled, or is killed before postconditions pass, the orchestrator must either resume that same claimed task or explicitly snapshot/reset/release the claim before any other task can be selected.
- **WorkspaceMatchesHeadUnlessOwned**: when no live session lease exists, the clone must match `head_rev` (or `review_base_rev` before the first checkpoint). Dirty workspaces without a live owner must be snapshotted into `state/recovery/` and reset before recovery or review.
- **ReviewCoversHead**: code review always inspects the full proposed diff from the pipeline's immutable base revision to its current head revision.

These are non-regression requirements carried forward from the current app's startup reconciliation in `src/clean_room/main.py` and path-guarded file access in `src/clean_room/runner.py`.

---

## Data model

### Tables (PostgreSQL, replacing clean-your-room's SQLite schema)

**repos** (kept, modified)
- id INTEGER PRIMARY KEY
- name TEXT — display name
- local_path TEXT — absolute path to the "golden" clone
- git_url TEXT NULLABLE — remote origin if applicable  
- default_branch TEXT DEFAULT 'main'
- created_at TEXT
- archived INTEGER DEFAULT 0

**prompts** (kept, extended)
- id INTEGER PRIMARY KEY
- name TEXT UNIQUE
- body TEXT — the prompt template, supports `{{variables}}`
- stage_type TEXT — enum: 'spec_author', 'spec_review', 'impl_plan', 'impl_plan_review', 'impl_task', 'code_review', 'bug_fix', 'validation', 'custom'
- agent_type TEXT — enum: 'claude', 'codex'
- created_at TEXT
- updated_at TEXT

**pipeline_defs** (new — composable stage-graph definitions)
- id INTEGER PRIMARY KEY
- name TEXT UNIQUE — e.g. 'full-coding-pipeline', 'spec-only'
- stage_graph_json TEXT — JSON object with stage nodes and transition edges (see stage graph schema below)
- created_at TEXT

**pipelines** (new — running instances)
- id INTEGER PRIMARY KEY
- pipeline_def_id INTEGER FK → pipeline_defs
- repo_id INTEGER FK → repos
- clone_path TEXT — path to this pipeline's isolated clone
- workspace_ref TEXT NULLABLE — mutable branch/bookmark/ref used inside the clone if the VCS needs one
- review_base_rev TEXT — immutable revision captured when the clone is created
- head_rev TEXT NULLABLE — latest accepted revision in the pipeline workspace; NULL means `review_base_rev` is still the accepted baseline
- workspace_state TEXT DEFAULT 'clean' — 'clean', 'dirty_live', 'dirty_snapshot_pending'
- dirty_snapshot_artifact TEXT NULLABLE — patch/archive captured when recovery, cancel, or kill finds uncheckpointed edits
- status TEXT — 'pending', 'running', 'paused', 'cancel_requested', 'cancelled', 'killed', 'completed', 'failed', 'needs_attention'
- current_stage_key TEXT NULLABLE — active node in the stage graph
- owner_token TEXT NULLABLE — orchestrator process that currently owns this pipeline lease
- last_heartbeat_at TEXT NULLABLE
- lease_expires_at TEXT NULLABLE
- recovery_state_json TEXT NULLABLE — persisted snapshot used for restart recovery
- config_json TEXT — runtime overrides (model, context threshold, max iterations per stage)
- created_at TEXT
- updated_at TEXT

**pipeline_stages** (new — stage execution state)
- id INTEGER PRIMARY KEY
- pipeline_id INTEGER FK → pipelines
- stage_key TEXT — key from `pipeline_defs.stage_graph_json.nodes[*].key`
- attempt INTEGER DEFAULT 1 — increments when the graph re-enters the same node
- entry_edge_key TEXT NULLABLE — edge used to enter this execution
- stage_type TEXT
- agent_type TEXT — 'claude' or 'codex'
- status TEXT — 'pending', 'running', 'review_loop', 'cancel_requested', 'cancelled', 'killed', 'completed', 'failed', 'needs_attention', 'skipped'
- entry_rev TEXT NULLABLE — `pipelines.head_rev` when the stage started
- exit_rev TEXT NULLABLE — `pipelines.head_rev` when the stage completed
- iteration INTEGER DEFAULT 0
- max_iterations INTEGER
- output_artifact TEXT NULLABLE — path to the markdown doc produced
- escalation_reason TEXT NULLABLE
- owner_token TEXT NULLABLE
- last_heartbeat_at TEXT NULLABLE
- lease_expires_at TEXT NULLABLE
- started_at TEXT NULLABLE
- completed_at TEXT NULLABLE

**agent_sessions** (new — individual agent invocations within a stage)
- id INTEGER PRIMARY KEY
- pipeline_stage_id INTEGER FK → pipeline_stages
- session_type TEXT — 'claude_sdk', 'codex_app_server'
- session_id TEXT NULLABLE — Claude SDK session_id or Codex thread_id
- prompt_id INTEGER FK → prompts NULLABLE
- prompt_override TEXT NULLABLE — when we need to inject dynamic context
- status TEXT — 'running', 'completed', 'failed', 'interrupted', 'context_limit', 'cancelled', 'killed'
- context_usage_pct REAL NULLABLE — last known context usage
- cost_usd REAL DEFAULT 0
- token_input INTEGER DEFAULT 0
- token_output INTEGER DEFAULT 0
- resume_state_json TEXT NULLABLE — persisted artifact paths, pending feedback, current task claim context, and continuation metadata
- owner_token TEXT NULLABLE
- last_heartbeat_at TEXT NULLABLE
- lease_expires_at TEXT NULLABLE
- started_at TEXT
- completed_at TEXT NULLABLE

**session_logs** (replaces job_logs — more granular)
- id INTEGER PRIMARY KEY
- agent_session_id INTEGER FK → agent_sessions
- event_type TEXT — 'assistant_message', 'tool_use', 'command_exec', 'file_change', 'error', 'context_warning', 'review_feedback', 'escalation', 'dirty_snapshot', 'cancellation'
- content TEXT — the log payload
- created_at TEXT

**escalations** (new — human attention queue)
- id INTEGER PRIMARY KEY
- pipeline_id INTEGER FK → pipelines
- pipeline_stage_id INTEGER FK → pipeline_stages NULLABLE
- reason TEXT — 'max_iterations', 'review_divergence', 'test_failure', 'agent_error', 'design_decision', 'context_exhausted'
- context_json TEXT — relevant state snapshot for the human
- status TEXT — 'open', 'resolved', 'dismissed'
- resolution TEXT NULLABLE — what the human decided
- created_at TEXT
- resolved_at TEXT NULLABLE

---

### HTN task planning tables

**htn_tasks** (new — Hierarchical Task Network decomposition)
- id INTEGER PRIMARY KEY
- pipeline_id INTEGER FK → pipelines
- parent_task_id INTEGER FK → htn_tasks NULLABLE — NULL for root tasks
- name TEXT — short task name
- description TEXT — detailed description of what to accomplish
- task_type TEXT — 'compound' (has subtasks), 'primitive' (executable), 'decision' (needs human)
- status TEXT — 'not_ready', 'ready', 'in_progress', 'completed', 'failed', 'blocked', 'skipped'
- priority INTEGER DEFAULT 0 — higher = do first among ready peers
- ordering INTEGER — sibling order within parent
- assigned_session_id INTEGER FK → agent_sessions NULLABLE
- claim_token TEXT NULLABLE — unique live ownership claim for a primitive task
- claim_owner_token TEXT NULLABLE — orchestrator owner that currently holds the claim
- claim_expires_at TEXT NULLABLE
- preconditions_json TEXT — JSON array of condition objects (see below)
- postconditions_json TEXT — JSON array of condition objects
- invariants_json TEXT NULLABLE — JSON array of invariant checks
- output_artifacts_json TEXT NULLABLE — JSON array of file paths produced
- checkpoint_rev TEXT NULLABLE — local checkpoint revision when the task completed
- estimated_complexity TEXT NULLABLE — 'trivial', 'small', 'medium', 'large', 'epic'
- diary_entry TEXT NULLABLE — learnings from executing this task
- created_at TEXT
- started_at TEXT NULLABLE
- completed_at TEXT NULLABLE

**htn_task_deps** (new — explicit dependency edges beyond parent-child)
- id INTEGER PRIMARY KEY
- task_id INTEGER FK → htn_tasks — the dependent task
- depends_on_task_id INTEGER FK → htn_tasks — must complete first
- dep_type TEXT — 'hard' (blocks), 'soft' (preferred ordering)

### HTN condition schema (preconditions_json / postconditions_json):

```json
[
  {
    "type": "file_exists",
    "path": "src/auth/login.py",
    "description": "Login endpoint module must exist"
  },
  {
    "type": "task_completed",
    "task_name": "Implement database schema",
    "description": "DB tables must be created first"
  },
  {
    "type": "tests_pass",
    "pattern": "tests/test_auth*",
    "description": "All auth tests must pass"
  },
  {
    "type": "lint_clean",
    "scope": "src/auth/",
    "description": "Auth module must lint clean"
  },
  {
    "type": "custom_verifier",
    "verifier_id": "python_symbol_exists",
    "args": {
      "path": "src/models.py",
      "symbol": "User"
    },
    "description": "User model must be defined"
  }
]
```

### HTN task lifecycle:

1. **Decomposition**: The spec/plan stage produces compound tasks. The implementation planner decomposes them into primitives.
2. **Readiness**: A primitive task becomes 'ready' when all hard deps are completed and all preconditions are satisfiable.
3. **Assignment**: The orchestrator atomically claims the highest-priority ready task inside a DB transaction, assigns it to the current agent session, and starts a per-task lease.
4. **Execution**: The agent works on the task. The harness can verify postconditions after completion.
5. **Verification**: Postconditions are checked programmatically where possible (file_exists, tests_pass, lint_clean). Custom checks resolve to app-owned verifier IDs, never arbitrary shell authored by the model.
6. **Propagation**: Completing a task may unblock dependent tasks, changing their status from 'not_ready' to 'ready'.

The orchestrator claims the next task with a single PostgreSQL transaction so Agent Teams workers cannot race each other:
```sql
BEGIN;
WITH candidate AS (
  SELECT id
  FROM htn_tasks
  WHERE pipeline_id = $1 AND status = 'ready' AND task_type = 'primitive'
  ORDER BY priority DESC, ordering ASC
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
UPDATE htn_tasks AS t
SET status = 'in_progress',
    assigned_session_id = $2,
    claim_token = $3,
    claim_owner_token = $4,
    claim_expires_at = $5
FROM candidate
WHERE t.id = candidate.id
RETURNING t.*;
COMMIT;
```

If the `UPDATE ... RETURNING` yields no row, no claim was obtained and the worker must retry later.

If a claimed task rotates for context before postconditions pass, the claim stays on that same task. The orchestrator updates `assigned_session_id` to the replacement session and resumes the claimed task; it does not select another ready task until the claim is released or completed.

---

## HTN planner

### HTNPlanner class

The HTN planner manages the task graph lifecycle. It's a pure data-layer component — no agent interaction, just graph operations and verification.

**Core methods:**

```python
class HTNPlanner:
    async def populate_from_structured_output(
        self, pipeline_id: int, tasks_json: list[dict]
    ) -> None:
        """Parse agent's task decomposition and populate htn_tasks + htn_task_deps."""
    
    async def claim_next_ready_task(
        self,
        pipeline_id: int,
        session_id: int,
        owner_token: str,
        claim_expires_at: str,
    ) -> HtnTask | None:
        """Atomically claim the highest-priority ready primitive task."""
    
    async def verify_postconditions(
        self, task_id: int, working_dir: str
    ) -> list[ConditionResult]:
        """Run postcondition checks. Returns pass/fail for each condition."""
    
    async def complete_task(
        self, task_id: int, checkpoint_rev: str | None, diary: str
    ) -> list[int]:
        """Mark task completed, propagate readiness, return newly-ready task IDs."""
    
    async def fail_task(self, task_id: int, reason: str) -> None:
        """Mark task failed. Block dependents."""
    
    async def create_decision_escalation(
        self, task_id: int, pipeline_id: int, description: str
    ) -> int:
        """Create an escalation for a decision-type task. Returns escalation ID."""
    
    async def resolve_decision(
        self, task_id: int, resolution: str
    ) -> list[int]:
        """Resolve a decision task with the human's answer. Unblock dependents."""
    
    async def get_task_tree(self, pipeline_id: int) -> list[HtnTask]:
        """Return full task tree for dashboard visualization."""
    
    async def get_progress_summary(self, pipeline_id: int) -> dict:
        """Return counts by status for dashboard cards."""
    
    async def sync_to_markdown(
        self, pipeline_id: int, working_dir: str
    ) -> None:
        """Write the current task state to specs/task-list.md for agent readability."""
```

**Postcondition verification engine:**

Each postcondition type maps to a verifier function:

```python
PROJECT_COMMANDS = {
    "tests_pass": lambda cond, cwd: run_cmd(
        ["uv", "run", "pytest", *expand_test_targets(cond), "-v"], cwd
    ) == 0,
    "lint_clean": lambda cond, cwd: run_cmd(
        ["uv", "run", "ruff", "check", *expand_paths(cond)], cwd
    ) == 0,
    "type_check": lambda cond, cwd: run_cmd(
        ["uv", "run", "mypy", "src/", "--ignore-missing-imports"], cwd
    ) == 0,
}

VERIFIERS = {
    "file_exists": lambda cond, cwd: Path(cwd, cond["path"]).exists(),
    "tests_pass": PROJECT_COMMANDS["tests_pass"],
    "lint_clean": PROJECT_COMMANDS["lint_clean"],
    "type_check": PROJECT_COMMANDS["type_check"],
    "custom_verifier": lambda cond, cwd: verifier_registry.run(
        cond["verifier_id"], cond.get("args", {}), cwd
    ),
}
```

Verifier subprocesses execute with `shell=False`, `cwd=pipeline.clone_path`, scrubbed env, and the same workspace sandbox/path guard as the agent adapters. `custom_verifier` resolves to app-authored verifier functions or allowlisted scripts bundled with build-your-room; the planner may reference them by `verifier_id` but may not synthesize new commands.

Command verifiers come from an app-owned command-template registry derived from repo instructions (`AGENTS.md` / `CLAUDE.md`), not from model text. For this repo the defaults are `uv run pytest ... -v`, `uv run ruff check ...`, and `uv run mypy src/ --ignore-missing-imports`. Test isolation uses `pytest-postgresql` fixtures against the local PostgreSQL instance.

The verifier runs after the agent session completes a task. If any postcondition fails, the task stays in `in_progress` and the agent gets a follow-up prompt: "Postcondition failed: {description}. Please fix and retry."

**Readiness propagation:**

When a task completes, the planner checks all tasks that depend on it:
1. For each dependent task, check if ALL hard deps are now completed
2. For newly-unblocked tasks, verify preconditions
3. Tasks that pass both checks transition from `not_ready` → `ready`
4. The dashboard is notified via the LogBuffer (a `task_ready` event)

**Agent Teams compatibility (future):**

The HTN task schema is designed to work with Claude Code Agent Teams when that path is enabled:
- Tasks have explicit boundaries (preconditions/postconditions) so teammates can work independently
- The `assigned_session_id` + `claim_token` fields track which teammate owns which task
- The atomic claim/lease protocol prevents duplicate task claims; dependency edges only encode ordering
- `invariants_json` can be checked continuously during parallel execution
- The `diary_entry` field enables cross-teammate knowledge sharing

---

## Pipeline stage graph schema

Each `pipeline_def.stage_graph_json` is a JSON object with stage nodes and explicit transition edges. It is a directed graph, not a positional array. Back-edges are allowed only when the edge has a loop budget or an escalation exit.

```json
{
  "entry_stage": "spec_author",
  "nodes": [
    {
      "key": "spec_author",
      "name": "Spec authoring",
      "type": "spec_author",
      "agent": "claude",
      "prompt": "spec_author_default",
      "model": "claude-opus-4-6",
      "max_iterations": 1,
      "review": {
        "agent": "codex",
        "prompt": "spec_review_default",
        "model": "gpt-5.1-codex",
        "max_review_rounds": 5,
        "exit_condition": "structured_approval",
        "on_max_rounds": "escalate"
      },
      "context_threshold_pct": 60
    },
    {
      "key": "impl_plan",
      "name": "Implementation plan",
      "type": "impl_plan",
      "agent": "claude",
      "prompt": "impl_plan_default",
      "model": "claude-opus-4-6",
      "max_iterations": 1,
      "review": {
        "agent": "codex",
        "prompt": "impl_plan_review_default",
        "model": "gpt-5.1-codex",
        "max_review_rounds": 5,
        "exit_condition": "structured_approval",
        "on_max_rounds": "escalate"
      },
      "context_threshold_pct": 60
    },
    {
      "key": "impl_task",
      "name": "Implementation",
      "type": "impl_task",
      "agent": "claude",
      "prompt": "impl_task_default",
      "model": "claude-sonnet-4-6",
      "max_iterations": 50,
      "context_threshold_pct": 60,
      "on_context_limit": "resume_current_claim"
    },
    {
      "key": "code_review",
      "name": "Code review + bug fix",
      "type": "code_review",
      "agent": "codex",
      "prompt": "code_review_default",
      "model": "gpt-5.1-codex",
      "max_iterations": 3,
      "fix_agent": "codex",
      "fix_prompt": "bug_fix_default",
      "on_max_rounds": "escalate"
    },
    {
      "key": "validation",
      "name": "Validation",
      "type": "validation",
      "agent": "claude",
      "prompt": "validation_default",
      "model": "claude-sonnet-4-6",
      "max_iterations": 3,
      "uses_devbrowser": true,
      "record_on_success": true
    }
  ],
  "edges": [
    {"key": "spec_to_plan", "from": "spec_author", "to": "impl_plan", "on": "approved"},
    {"key": "plan_to_impl", "from": "impl_plan", "to": "impl_task", "on": "approved"},
    {"key": "impl_to_review", "from": "impl_task", "to": "code_review", "on": "stage_complete"},
    {"key": "review_to_validation", "from": "code_review", "to": "validation", "on": "approved"},
    {
      "key": "validation_back_to_review",
      "from": "validation",
      "to": "code_review",
      "on": "validation_failed",
      "max_visits": 3,
      "on_exhausted": "escalate"
    },
    {"key": "validation_to_done", "from": "validation", "to": "completed", "on": "validated"}
  ]
}
```

`pipelines.current_stage_key` tracks the active node. `pipeline_stages` records each visit to a node, so looping from validation back to code review creates a new stage execution row with a higher `attempt`. The pipeline keeps one canonical workspace head (`review_base_rev`, `head_rev`) instead of inventing a new branch per stage.

**Exit conditions for review loops:**
- `structured_approval`: The review agent's structured output includes `{"approved": true, "max_severity": "low"}`. Only "low" or no issues = approved.
- `escalate`: Create an escalation and pause the pipeline.
- `proceed_with_warnings`: Log warnings but continue.

**on_context_limit options:**
- `resume_current_claim`: For `impl_task` only. Spawn a replacement session that resumes the same claimed primitive task. The claim remains `in_progress` until verification succeeds or the orchestrator explicitly releases it.
- `new_session_continue`: For artifact-authoring stages, or for implementation only after the orchestrator has checkpointed work and released the current claim.
- `escalate`: Pause and ask the human.

---

## Orchestrator state machine

### PipelineOrchestrator class

The orchestrator is the core engine. It replaces clean-your-room's JobRunner.

**Lifecycle per pipeline:**
1. Clone the repo to an isolated directory: `~/.build-your-room/clones/{pipeline_id}/`
2. Capture `review_base_rev` and optionally create a single pipeline-local `workspace_ref`
3. Acquire a pipeline lease, persist `owner_token`, and initialize `recovery_state_json`
4. Follow explicit stage-graph edges from `current_stage_key`
5. For each stage, create a `pipeline_stages` row with `entry_rev`, then run the appropriate agent adapter
6. Between stages, verify output artifacts, update `head_rev`, reset the workspace to the accepted baseline if needed, and choose the next edge whose guard is satisfied
7. On escalation, lease loss, dirty-workspace recovery, or restart recovery failure, pause the pipeline and push to the escalation queue

**Key methods:**
- `async run_pipeline(pipeline_id)` — main loop
- `async run_stage(pipeline_id, stage_key)` — dispatch to agent adapter
- `async run_review_loop(stage, primary_session)` — review loop logic using a live session handle
- `async check_context_and_maybe_rotate(session)` — hook called after each agent turn
- `async snapshot_dirty_workspace(pipeline_id, baseline_rev)` — capture uncheckpointed edits into `state/recovery/`
- `async renew_leases(pipeline_id, stage_id, session_id)` — heartbeat loop for durable ownership
- `async reconcile_running_state()` — startup recovery / downgrade logic
- `async escalate(pipeline_id, stage_id, reason, context)` — create escalation, set pipeline status
- `async resume_pipeline(pipeline_id, resolution)` — human resumes after escalation

**Concurrency model:**
- Each pipeline runs as an `asyncio.Task`
- A semaphore limits concurrent pipelines (configurable, default 10)
- Each pipeline holds its own cancel Event (inherited from clean-your-room pattern)
- The `active_pipelines` dict maps pipeline_id → (Task, Event), but it is only an in-memory cache; DB lease state is the source of truth
- A heartbeat updates `last_heartbeat_at` and `lease_expires_at` for the pipeline, current stage, live session, and claimed task
- On startup, the reconciler scans `running` rows. If there is no live owner or the lease is expired, it reconstructs from `recovery_state_json` only if the workspace is clean at the accepted baseline; otherwise it snapshots dirty changes to `state/recovery/`, resets the clone, and marks the pipeline `needs_attention`. This preserves the current app's startup reconciliation guarantee instead of trusting local subprocesses

**Workspace cleanliness rule:**
- While a live session lease exists, the clone may differ from `head_rev`.
- Once no live session owns the workspace, the clone must equal `head_rev` (or `review_base_rev` if `head_rev` is still NULL).
- Recovery, cancel, and kill all use the same rule: if the workspace is dirty without a live owner, capture a patch plus changed-file manifest into `state/recovery/{timestamp}/`, set `dirty_snapshot_artifact`, reset the clone to the accepted baseline, and only then allow review, resume, or cleanup.

**Context rotation logic (hook in agent session):**
After every agent turn, call `get_context_usage()` (Claude SDK) or check token counts (Codex).
If usage > threshold:
1. Log a `context_warning` event
2. Capture the current output artifact path and persist a restartable `resume_state_json`
3. If the current stage is `impl_task` and a primitive task claim is still open, keep that task `in_progress`, end the current session gracefully, and spawn a replacement session that resumes the SAME claimed task using the stored `{task_id, claim_token, prompt_context}`
4. Update `htn_tasks.assigned_session_id` to the replacement session when the task is resumed
5. For non-claimed stages, spawn a generic continuation session for the same stage artifact
6. Only after postconditions pass and `head_rev` is updated may an `impl_task` release its claim and let the scheduler select another ready task

**Cancellation semantics:**
- `POST /pipelines/{id}/cancel` sets `pipelines.status='cancel_requested'`, signals the cancel Event, and stops at the next safe boundary. If the current `impl_task` has uncheckpointed edits, the orchestrator snapshots dirty changes, resets the workspace to the accepted baseline, releases the claim, returns the task to `ready`, and ends the pipeline as `cancelled`.
- `POST /pipelines/{id}/kill` terminates live sessions immediately. The orchestrator then snapshots any dirty workspace it can recover, resets the clone to the accepted baseline, releases claims, and marks the pipeline `killed`.
- `paused` is resumable. `cancelled` and `killed` are terminal states.

---

## Agent adapters

Adapters expose a live session handle instead of a one-shot `run_session()` call. That is required for same-session feedback loops and durable recovery state. Each stage carries an explicit tool profile. Claude sessions never receive `Bash`, shell, or arbitrary process-spawn tools; implementation and validation rely on typed harness MCP tools instead.

```python
class LiveSession(Protocol):
    session_id: str | None

    async def send_turn(
        self, prompt: str, output_schema: dict | None = None
    ) -> SessionResult:
        """Send a new turn on the existing session/thread."""

    async def get_context_usage(self) -> dict | None:
        """Return provider-native context usage if available."""

    async def snapshot(self) -> dict:
        """Return restartable state to persist in agent_sessions.resume_state_json."""

    async def close(self) -> None:
        """Release provider resources."""


class AgentAdapter(Protocol):
    async def start_session(self, config: SessionConfig) -> LiveSession:
        """Open a live session in the pipeline sandbox."""
```

### ClaudeAgentAdapter

Wraps the `claude-agent-sdk` Python package. Uses `ClaudeSDKClient` for bidirectional sessions with hooks.

```python
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    tool, create_sdk_mcp_server,
    AssistantMessage, TextBlock
)

class ClaudeAgentAdapter:
    async def start_session(self, config: SessionConfig) -> LiveSession:
        options = ClaudeAgentOptions(
            model=config.model,
            cwd=config.clone_path,
            system_prompt=config.system_prompt,
            permission_mode="acceptEdits",
            allowed_tools=config.allowed_tools,
            max_turns=config.max_turns,
            setting_sources=["project"],  # reads CLAUDE.md
            can_use_tool=make_path_guard(config.allowed_roots),
            # Explicitly disable 1M context
            # betas=[]  # do NOT include context-1m beta
        )

        client = ClaudeSDKClient(options=options)
        await client.__aenter__()
        return ClaudeLiveSession(client, config, self.log_buffer)
```

**Key capabilities used:**
- `get_context_usage()` — monitor context window
- `setting_sources=["project"]` — reads CLAUDE.md from the repo
- `permission_mode="acceptEdits"` + the generalized clean-room path guard — confined to clone/artifact/log/state roots, not bypass permissions
- Stage tool profiles are explicit:
  `spec_author` / `impl_plan`: `Read`, `Write`, `Edit`, `Glob`, `Grep`
  `impl_task` / `validation`: the same file tools plus typed harness MCP tools such as `run_tests`, `run_lint`, `run_typecheck`, `start_dev_server`, `browser_validate`, and `record_browser_artifact`
- `Bash`, shell, and unrestricted process tools are never enabled for Claude sessions
- Custom MCP tools may expose harness-level capabilities, but those tools must enforce the same workspace roots and command templates

### CodexAppServerAdapter

Wraps the Codex app-server stdio JSON-RPC protocol.

```python
import asyncio, json

class CodexAppServerAdapter:
    async def start_session(self, config: SessionConfig) -> LiveSession:
        proc = await asyncio.create_subprocess_exec(
            'codex', 'app-server',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=config.clone_path
        )

        # Initialize
        await self._send(proc, {"method": "initialize", "id": 0, "params": {
            "clientInfo": {"name": "build_your_room", "title": "Build Your Room", "version": "0.1.0"}
        }})
        await self._send(proc, {"method": "initialized", "params": {}})

        # Start thread
        resp = await self._send_and_wait(proc, {
            "method": "thread/start", "id": 1, "params": {
                "model": config.model,
                "cwd": config.clone_path,
                "approvalPolicy": "never",
                "sandbox": {
                    "mode": "workspace-write",
                    "writableRoots": config.allowed_roots
                }
            }
        })
        return CodexLiveSession(proc, resp["result"]["thread"]["id"], config, self.log_buffer)
```

**Review mode for code review:**
Use `review/start` with the full proposed diff from `pipelines.review_base_rev` to `pipelines.head_rev`. If app-server review mode cannot ingest an explicit revision range directly, materialize the diff to an artifact in `artifacts/review/` and review that file instead. Parse `exitedReviewMode` item for structured feedback.

**Structured output for approval decisions:**
Use `outputSchema` on `turn/start`:
```json
{
  "type": "object",
  "properties": {
    "approved": {"type": "boolean"},
    "max_severity": {"type": "string", "enum": ["none", "low", "medium", "high", "critical"]},
    "issues": {"type": "array", "items": {
      "type": "object",
      "properties": {
        "severity": {"type": "string"},
        "description": {"type": "string"},
        "file": {"type": "string"},
        "line": {"type": "integer"}
      }
    }},
    "feedback_markdown": {"type": "string"}
  },
  "required": ["approved", "max_severity", "issues", "feedback_markdown"]
}
```

---

## Review loop protocol

The review loop is the core feedback mechanism between Claude and Codex. It runs within a stage that has a `review` sub-config.

### Protocol:

**Round N:**
1. **Primary agent** (Claude) uses a live `LiveSession` handle to produce or revise an artifact (spec markdown, implementation plan markdown)
2. **Review agent** (Codex) receives the artifact with a review prompt
3. Codex returns structured output: `{approved, max_severity, issues, feedback_markdown}`
4. **Decision gate:**
   - If `approved == true` AND `max_severity in ['none', 'low']` → stage complete
   - If `max_severity in ['medium']` and iteration < max → feed feedback back to primary agent
   - If `max_severity in ['high', 'critical']` → always feed back regardless of iteration count
   - If iteration >= max_review_rounds → escalate or proceed based on `on_max_rounds`

**Feeding feedback back:**
`run_review_loop(stage, primary_session)` always receives the live session handle, not just the previous output text. That makes same-session continuation implementable while the lease is alive. Two continuation modes exist:

**Same-session feedback** (if context permits):
Send the feedback markdown directly to the existing Claude session as a new turn on the same session handle.

**New-session feedback** (if context is tight):
1. Persist `{artifact_path, feedback_markdown, round_number}` to `agent_sessions.resume_state_json`
2. End the current Claude session
3. Start a replacement session with prompt: "You previously wrote {artifact_path}. A reviewer provided this feedback: {feedback_markdown}. Please revise the document addressing all issues."

The context monitor decides which mode while the process is live. After a server restart, same-session feedback is not assumed to be resumable; the orchestrator recreates a new session from `resume_state_json` or escalates if the state is insufficient.

### Review prompt template (Codex):

```
Review the following {artifact_type} document. Mentally simulate a TLA+ specification: define invariants, preconditions, and postconditions for the system described. Use this mental model to validate the document.

Do NOT create a TLA+ file. Use the formal reasoning to find:
- Logical contradictions
- Missing edge cases  
- Violated invariants
- Unspecified preconditions
- Ambiguous postconditions

Return structured JSON output with your assessment.

Document to review:
{artifact_content}
```

---

## Implementation loop

The implementation stage is a multi-iteration loop where each iteration completes one task from the HTN task graph stored in the database.

### Task selection (orchestrator-driven):

Before each iteration, the orchestrator:
1. Atomically claims the highest-priority ready primitive task and starts/renews its lease
2. Verifies preconditions programmatically where possible
3. Constructs a focused prompt including the task description, preconditions, and context
4. Passes it to the agent session

After each iteration, the orchestrator:
1. Checks postconditions programmatically
2. Updates task status in the DB
3. Propagates readiness to dependent tasks
4. Records the new `head_rev` (and optional local checkpoint commit/revision) plus diary entry
5. Releases the task claim only after verification succeeds or an explicit snapshot/reset path runs

### Per-iteration prompt (dynamically constructed):

```
You are implementing task: "{task.name}"
Description: {task.description}
Estimated complexity: {task.estimated_complexity}

Preconditions (verified by harness):
{formatted_preconditions}

Postconditions (will be verified after you finish):
{formatted_postconditions}

Invariants to maintain:
{formatted_invariants}

Study the existing specs in specs/* for context.

Important:
- For design decisions, stop and explain. The harness will create an escalation.
- Use the harness verification tools, which execute repo-standard commands from `AGENTS.md` / `CLAUDE.md`. In this repo that means `uv run pytest tests/ -v`, `uv run ruff check src/ tests/`, and `uv run mypy src/ --ignore-missing-imports`
- Write a diary entry summarizing what you learned
- If the harness rotates you for context, resume THIS SAME task unless it explicitly tells you the claim was released
- Do not push or publish anything. The harness records local checkpoint revisions after verification.

Focus on THIS task only. Make the code production-ready.
```

### HTN task decomposition (during planning stage):

The implementation planning stage creates the initial HTN task graph. The planner agent receives:

```
Study the spec documents and produce a hierarchical task decomposition.

For each task, provide:
- name: short identifier
- description: what to implement
- type: "compound" (needs subtasks) or "primitive" (directly implementable)
- preconditions: what must be true before starting
- postconditions: what must be true after completion
- invariants: what must remain true throughout
- dependencies: which other tasks must complete first
- estimated_complexity: trivial/small/medium/large/epic
- priority: relative importance (higher = do first)

Output as JSON matching this schema:
{htn_task_schema}

Decompose compound tasks until all leaves are primitive tasks that a single agent session can complete in one focused iteration. Each primitive task should be completable within ~40% of the context window.
```

The orchestrator parses this structured output and populates the `htn_tasks` and `htn_task_deps` tables.

### Context rotation in the implementation loop:

This is where context management matters most. Each task consumes context — reading files, running commands, fixing lint errors. The hook checks after every agent turn:

1. `get_context_usage()` returns category breakdown
2. If total > threshold before postconditions pass, persist `{task_id, claim_token, task_prompt, artifact_paths}` to `resume_state_json`
3. Start a replacement session for THAT SAME claimed task; update `assigned_session_id`, keep the task `in_progress`, and preserve the claim lease
4. Only after postconditions pass and `head_rev` is checkpointed may the orchestrator clear the claim and select another ready task
5. If a process dies before checkpointing, recovery snapshots dirty edits into `state/recovery/`, resets the clone to the accepted baseline, clears `assigned_session_id`, and then either returns the task to `ready` (when replay is safe) or marks it `blocked` with an escalation (when the partial work needs human judgment)
6. The diary entries (stored in `htn_tasks.diary_entry` and in `diary/` files) provide continuity between sessions

### Task progress tracking:

The dashboard shows real-time HTN task progress by querying the database:

```sql
-- Summary stats for a pipeline
SELECT 
  COUNT(*) as total,
  SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
  SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) as in_progress,
  SUM(CASE WHEN status = 'ready' THEN 1 ELSE 0 END) as ready,
  SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) as blocked,
  SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
FROM htn_tasks 
WHERE pipeline_id = $1 AND task_type = 'primitive'
```

The agent still maintains a human-readable task list in `specs/task-list.md` as a convenience, but the DB is the source of truth. The harness syncs the markdown from the DB after each task completes.

### Diary format (per task, stored in both DB and filesystem):

```markdown
# Task: Implement user authentication
## Session: 7 | Complexity: medium | Revision: abc123

### What I did
- Implemented /auth/login and /auth/register endpoints
- Added JWT token generation and validation

### Learnings
- The existing User model uses email as PK, not UUID — had to adapt
- Rate limiting middleware conflicts with auth middleware ordering

### Postcondition verification
- [PASS] file_exists: src/auth/login.py
- [PASS] tests_pass: tests/test_auth*
- [PASS] lint_clean: src/auth/

### Open Questions
- Should refresh tokens be stored in Redis or PostgreSQL?
```

---

## Validation stage

### Code review phase (Codex)

Uses Codex app-server's `review/start` against the full proposed diff from `pipelines.review_base_rev` to `pipelines.head_rev`. `uncommittedChanges` is not sufficient because implementation work is checkpointed between tasks.

Each round of issues found triggers a fresh Codex session to fix bugs:
1. Codex reviewer produces structured issue list
2. A new Codex session receives: "Fix these issues: {issues_json}. Run tests after each fix."
3. After fixing, update `head_rev` and loop back to review the same full diff range
4. Max 3 rounds, then escalate remaining issues

### Browser validation phase (harness-owned dev-browser)

For projects with a web UI component, the validation stage includes browser testing.

**Setup (automated through typed harness tools):**
1. Agent requests `start_dev_server`; the harness runs the repo-standard dev command inside the clone, captures stdout/stderr under `state/devserver/`, and returns the URL/process handle
2. Agent uses typed browser-validation tools to:
   - Navigate to the running app
   - Execute test scenarios
   - Check for console errors
   - Verify UI functionality

**dev-browser integration:**
`dev-browser` is wrapped by a harness-owned browser runner. The agent never invokes `bash` from `~/.claude/skills/dev-browser` directly. Instead the harness starts/stops the runner outside the model session and exposes typed tools such as `browser_open`, `browser_run_scenario`, `browser_console_errors`, and `browser_record_artifact`. All logs, recordings, and temporary browser state are redirected into the pipeline's `logs/`, `artifacts/`, and `state/` directories.

**Recording on success:**
When validation passes, the agent asks the harness to record a GIF/video of the working functionality. This recording is saved as a validation artifact.

### Property-based testing integration

The validation prompt instructs the agent to prefer property-based tests:

```
When writing tests, prefer property-based testing over unit tests:
- Python projects: use Hypothesis
- JavaScript/TypeScript projects: use fast-check
- Web UI testing: use Bombadil (antithesishq/bombadil)
- Only write unit tests for pure edge-case coverage that properties can't capture

Properties to test:
- Idempotency: f(f(x)) == f(x) where applicable
- Round-trip: deserialize(serialize(x)) == x  
- Invariant preservation: state transitions maintain invariants
- Commutativity: where operations should be order-independent
```

### Verification checklist (agent self-check before completing):

1. All tests pass (including property-based)
2. Linting clean (ruff/eslint/biome per project)
3. Type checking clean (mypy/tsc per project)
4. If web UI: harness-owned browser validation passed, recording saved
5. Pipeline `head_rev` updated and, if enabled, a local checkpoint revision recorded
6. Task list updated
7. Diary entry written

---

## Dashboard design

The dashboard is designed for monitoring 10+ parallel pipelines at a glance.

### Main dashboard (/)

**Pipeline cards grid** — each card shows:
- Pipeline name + repo name
- Current stage (highlighted in the stage-graph mini-visualization)
- Stage progress: "Impl task 7/50" or "Review round 2/5"
- HTN task progress: "12/28 tasks completed" with a mini progress bar
- Status badge: running (blue), needs_attention (amber), completed (green), failed (red), paused (gray), cancelled (slate), killed (black)
- Lease health / last heartbeat when running
- Last activity timestamp
- Context usage bar (if stage is running)
- Cost accumulator (total USD spent on this pipeline)
- Clone cleanup button (visible when pipeline is completed/failed/cancelled/killed)

**Escalation banner** — if any escalations are open, a persistent banner at top:
"3 pipelines need your attention" — click to see the escalation queue.

### Escalation queue (/escalations)

List of open escalations with:
- Pipeline name + stage name
- Reason (max iterations, design decision, etc.)
- Context snapshot (what the agents were working on)
- Action buttons: Resolve (with text input), Dismiss, Pause pipeline, Kill pipeline

### Pipeline detail (/pipelines/{id})

**Stage graph visualization** — the pipeline's stage nodes and transition edges, with the active node highlighted and loop attempts annotated.

**Stage tabs** — click each stage to see:
- Session list with logs
- Output artifacts (rendered markdown)
- Review feedback history
- Context usage chart over time

**HTN task tree** — interactive tree view of the hierarchical task network:
- Compound tasks expand to show subtasks
- Color-coded by status (green=done, blue=in progress, gray=not ready, amber=blocked, red=failed, pink=needs human)
- Each task shows: name, assigned session, checkpoint rev, precondition/postcondition status
- Click a task to see its diary entry and full details
- "Decision" type tasks have a resolve button that creates an escalation

**Clone management** — at the bottom of the pipeline detail:
- "Open in terminal" button (copies the clone path to clipboard)
- "Clean up clone" button (deletes the clone directory, marks pipeline as cleaned)
- Clone size indicator

**Live log stream** — SSE-powered, same LogBuffer pattern from clean-your-room but scoped to the current pipeline's active session.

### Repo management (/repos)

Add a repo by local path or git URL. Repos show:
- All pipelines that have run against them
- The golden clone path
- Links to create new pipelines

### Prompt management (/prompts)

CRUD for prompt templates. Each prompt tagged with stage_type and agent_type. Inline editing via HTMX partials (inherited from clean-your-room).

### Pipeline builder (/pipeline-defs)

Visual stage-graph builder for defining pipeline definitions. Start simple: a form with stage node entries plus explicit transition edge entries. Each node selects: stage type, agent type, prompt, model, max iterations, review config. Each edge selects: `from`, `to`, `on`, optional `max_visits`, and `on_exhausted`.

---

## Project structure

```
build-your-room/
├── src/build_your_room/
│   ├── __init__.py
│   ├── main.py                    # FastAPI app, lifespan, route registration
│   ├── config.py                  # Env vars, paths, defaults
│   ├── models.py                  # Dataclasses for internal types
│   ├── db.py                      # Schema DDL, init_db, get_db, migrations
│   ├── streaming.py               # LogBuffer (kept from clean-your-room)
│   ├── orchestrator.py            # PipelineOrchestrator — the core engine
│   ├── lease_manager.py           # Lease/heartbeat ownership for pipelines, stages, sessions, tasks
│   ├── recovery.py                # Startup reconciliation + restart recovery
│   ├── command_templates.py       # Repo-standard uv-run command templates + verifier command policy
│   ├── tool_profiles.py           # Per-stage tool allowlists and typed MCP tool profiles
│   ├── browser_runner.py          # Harness-owned dev-server + browser validation bridge
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── base.py                # AgentAdapter ABC
│   │   ├── claude_adapter.py      # ClaudeAgentAdapter (claude-agent-sdk)
│   │   └── codex_adapter.py       # CodexAppServerAdapter (stdio JSON-RPC)
│   ├── stages/
│   │   ├── __init__.py
│   │   ├── base.py                # StageRunner ABC
│   │   ├── spec_author.py         # Spec authoring stage
│   │   ├── review_loop.py         # Generic review loop (used by spec + impl plan)
│   │   ├── impl_plan.py           # Implementation planning stage
│   │   ├── impl_task.py           # Implementation loop stage
│   │   ├── code_review.py         # Code review + bug fix stage
│   │   └── validation.py          # Validation stage (tests + devbrowser)
│   ├── context_monitor.py         # Context usage tracking + rotation logic
│   ├── clone_manager.py           # Repo cloning, workspace-ref management, cleanup
│   ├── sandbox.py                 # Workspace roots + path guard reused by adapters and verifiers
│   ├── htn_planner.py             # HTN task graph management, atomic claims, readiness propagation, verifier registry
│   ├── routes/
│   │   ├── dashboard.py           # Main dashboard, escalation queue
│   │   ├── pipelines.py           # Pipeline CRUD, detail view, SSE stream
│   │   ├── pipeline_defs.py       # Pipeline definition builder
│   │   ├── repos.py               # Repo management
│   │   ├── prompts.py             # Prompt CRUD (kept + extended)
│   │   └── api.py                 # JSON API for programmatic access
│   └── templates/
│       ├── base.html
│       ├── dashboard.html
│       ├── escalations.html
│       ├── pipeline_detail.html
│       ├── pipeline_builder.html
│       ├── repo_detail.html
│       ├── prompts.html
│       └── partials/
│           ├── pipeline_card.html
│           ├── stage_detail.html
│           ├── escalation_card.html
│           ├── prompt_form.html
│           └── prompt_row.html
├── static/
│   └── style.css
├── tests/
│   ├── test_orchestrator.py
│   ├── test_adapters.py
│   ├── test_stages.py
│   ├── test_context_monitor.py
│   ├── test_routes.py
│   └── conftest.py
├── docs/                           # Diataxis docs (inherited pattern)
├── specs/                          # Default prompt templates
│   ├── default_prompts.json
│   └── example_pipeline_def.json
├── CLAUDE.md
├── AGENTS.md
├── pyproject.toml
├── uv.lock
└── README.md
```

**Dependencies (pyproject.toml):**
- fastapi, uvicorn, jinja2, python-multipart
- psycopg[binary]
- claude-agent-sdk (bundles Claude Code CLI)
- sse-starlette (for SSE endpoints)
- httpx (for any HTTP needs)
- pytest-postgresql (dev/test)

**External requirements (not pip-installable):**
- PostgreSQL local instance
- Codex CLI (`codex` binary on PATH)
- dev-browser runner available to the harness
- Node.js (for the harness-owned browser runner)

---

## Configuration

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| BUILD_YOUR_ROOM_DIR | ~/.build-your-room | Base directory for clones, logs, artifacts, and recovery state |
| DATABASE_URL | postgres:///build_your_room | PostgreSQL DSN for orchestrator state |
| DEFAULT_CLAUDE_MODEL | claude-sonnet-4-6 | Default model for Claude stages |
| DEFAULT_CODEX_MODEL | gpt-5.1-codex | Default model for Codex stages |
| SPEC_CLAUDE_MODEL | claude-opus-4-6 | Model for spec authoring (higher capability) |
| CONTEXT_THRESHOLD_PCT | 60 | Default context usage threshold |
| MAX_CONCURRENT_PIPELINES | 10 | Semaphore limit |
| PIPELINE_LEASE_TTL_SEC | 30 | How long a running owner lease remains valid without heartbeat |
| PIPELINE_HEARTBEAT_INTERVAL_SEC | 10 | How often running workers renew leases |
| ANTHROPIC_API_KEY | (required) | For Claude Agent SDK |
| OPENAI_API_KEY | (required for Codex) | For Codex app-server |
| DEVBROWSER_SKILL_PATH | ~/.claude/skills/dev-browser | Path to the harness-owned dev-browser runner bundle |
| LOG_LEVEL | INFO | Python logging level |

### Runtime config per pipeline (config_json)

```json
{
  "claude_model": "claude-opus-4-6",
  "codex_model": "gpt-5.1-codex",
  "context_threshold_pct": 60,
  "disable_1m_context": true,
  "max_concurrent_stages": 1,
  "impl_task_rotation_policy": "resume_current_claim",
  "lease_ttl_sec": 30,
  "checkpoint_commits": true,
  "snapshot_dirty_workspace_on_cancel_or_kill": true,
  "remote_publish": "manual",
  "devbrowser_enabled": true,
  "property_test_framework": "auto"
}
```

`property_test_framework` options: "auto" (detect from project), "hypothesis", "fast-check", "bombadil", "none"

---

## API surface

### HTML routes (HTMX)

GET  /                          → dashboard
GET  /escalations               → escalation queue
GET  /pipelines/new             → create pipeline form
POST /pipelines                 → create + start pipeline
GET  /pipelines/{id}            → pipeline detail
POST /pipelines/{id}/pause      → pause pipeline
POST /pipelines/{id}/resume     → resume pipeline  
POST /pipelines/{id}/cancel     → cancel pipeline
POST /pipelines/{id}/kill       → force kill pipeline
POST /pipelines/{id}/cleanup    → delete pipeline clone directory
POST /pipelines/cleanup-completed → bulk cleanup all completed pipeline clones
GET  /pipelines/{id}/stream     → SSE log stream
GET  /pipelines/{id}/tasks      → HTN task tree view
POST /pipelines/{id}/tasks/{task_id}/resolve → resolve a decision task
GET  /pipeline-defs             → list pipeline definitions
GET  /pipeline-defs/new         → pipeline builder form
POST /pipeline-defs             → create pipeline definition
GET  /repos                     → repo list
GET  /repos/new                 → add repo form
POST /repos                     → add repo
GET  /repos/{id}                → repo detail
GET  /prompts                   → prompt management
POST /prompts                   → create prompt
PUT  /prompts/{id}              → update prompt
DELETE /prompts/{id}            → delete prompt

POST /escalations/{id}/resolve  → resolve escalation
POST /escalations/{id}/dismiss  → dismiss escalation

### JSON API (for programmatic access / future CLI)

GET  /api/pipelines             → list pipelines (filterable)
POST /api/pipelines             → create pipeline
GET  /api/pipelines/{id}/status → pipeline status summary
POST /api/pipelines/{id}/cancel → graceful cancel
POST /api/pipelines/{id}/kill   → force kill
GET  /api/pipelines/{id}/tasks  → HTN task tree (JSON)
GET  /api/pipelines/{id}/tasks/progress → task progress summary
POST /api/pipelines/{id}/cleanup → delete clone
GET  /api/escalations           → open escalations
POST /api/escalations/{id}      → resolve/dismiss

### SSE endpoints

GET /pipelines/{id}/stream      → pipeline-scoped log stream
GET /sessions/{id}/stream       → session-scoped log stream

Both use the LogBuffer pub/sub pattern from clean-your-room. The LogBuffer is keyed by session_id, and the pipeline stream multiplexes all active sessions within that pipeline.

---

## Implementation plan

### Phase 1: Foundation (scaffold + DB + core loop)
1. Fork clean-your-room → build-your-room, strip specs-monorepo and GitHub-specific code
2. New PostgreSQL schema + import path from old SQLite tables (including HTN task tables)
3. PipelineOrchestrator skeleton with stage-graph dispatch, durable leases, dirty-workspace recovery, and startup reconciliation
4. CloneManager for repo cloning, workspace refs, cleanup, and reset-to-head behavior
5. Sandbox/path guard abstraction + per-stage tool profiles reused by adapters and verifiers
6. Command-template registry for repo-standard `uv run` verification commands
7. ContextMonitor as a reusable hook
8. Config module with all env vars
9. HTNPlanner: task graph CRUD, atomic claims, readiness propagation, postcondition verification

### Phase 2: Agent adapters
10. ClaudeAgentAdapter with live session handles, explicit tool profiles, context monitoring, and workspace confinement
11. CodexAppServerAdapter with stdio JSON-RPC protocol, persistent threads, and workspace-write sandboxing
12. Review loop logic with structured approval parsing, same-session continuation, and restart fallback
13. Adapter unit tests with mocked agent responses

### Phase 3: Stage runners
14. SpecAuthorStage + review loop integration
15. ImplPlanStage + review loop integration + HTN task graph population from structured output
16. ImplTaskStage with atomic HTN task claims, same-task context rotation, and postcondition verification
17. CodeReviewStage with full-head diff review and bug-fix loop
18. ValidationStage with typed browser tools and harness-owned dev-browser integration

### Phase 4: Dashboard
19. Dashboard template with pipeline cards grid + HTN progress indicators
20. Escalation queue page
21. Pipeline detail page with stage-graph viz, HTN task tree, lease health, dirty-snapshot visibility, and live logs
22. Pipeline builder form with explicit node/edge editing
23. Prompt management (extend existing)
24. Clone cleanup buttons (per-pipeline and bulk)

### Phase 5: Polish + validation
25. Property-based tests for orchestrator state machine, stage-transition guards, and HTN claims (Hypothesis)
26. Integration tests with mock adapters + `pytest-postgresql`
27. Devbrowser recording integration in validation stage
28. Documentation (README, CLAUDE.md, AGENTS.md)
29. Default prompt templates for all stage types

Each phase is a natural checkpoint. The system is usable after Phase 3 (CLI-driven). Phase 4 adds the monitoring dashboard. Phase 5 hardens it.

---
