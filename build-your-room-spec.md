# build-your-room specification

> Local agent orchestrator for parallel coding pipelines — Claude Agent SDK + Codex app-server

---

## Overview

**build-your-room** is a local agent orchestrator that manages parallel coding pipelines. Each pipeline is a composable DAG of stages — spec authoring, review loops, implementation planning, task-by-task coding, and validation — executed by Claude Agent SDK sessions and Codex app-server sessions working in concert.

**Fork basis:** `winklerj/clean-your-room` → gutted and extended. We keep: FastAPI async-first, SQLite+WAL, aiosqlite, LogBuffer pub/sub for SSE, HTMX+Jinja2 templates, cooperative cancellation via asyncio.Event, `uv` package management.

**We replace:** The single-agent JobRunner with a pipeline orchestrator. The GitHub-repo-centric model with a local-repo-centric model. The specs-monorepo pattern (removed — output artifacts live in the repo itself).

**Key constraints:**
- 10+ parallel pipelines, each with its own clone of the repo
- Context compaction must be avoided — configurable threshold (default 60%), no 1M context window
- Human intervention only when escalated — the dashboard is the primary interface
- Agents use CLAUDE.md and AGENTS.md in the target repo for project-specific instructions
- dev-browser (SawyerHood) for browser-based validation with recording
- Property-based testing over unit tests: Hypothesis (Python), fast-check (JS), Bombadil (web UI)
- TLA+ reasoning (simulated, not generated) for formal verification of specs

**Parallelization strategy:**
- Pipeline-level: 10+ pipelines run in parallel, each on its own repo clone
- Stage-level: Sequential within a pipeline (sufficient when enough pipelines run concurrently)
- Task-level (future): Claude Code's Agent Teams beta (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) can parallelize work within the implementation stage. The team lead decomposes the HTN task list and spawns teammates that claim tasks from a shared task list, communicate via peer-to-peer messaging, and merge changes continuously. This requires Opus 4.6 and v2.1.32+. We design the HTN task schema to be compatible with this model — tasks have clear boundaries, preconditions, and postconditions that make them safe to parallelize.

**Clone management:**
- Each pipeline gets an isolated clone at `~/.build-your-room/clones/{pipeline_id}/`
- Clones persist after completion for inspection
- Manual cleanup via dashboard button (per-pipeline or bulk "clean completed")

---

## Data model

### Tables (SQLite, extending clean-your-room schema)

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

**pipeline_defs** (new — composable DAG definitions)
- id INTEGER PRIMARY KEY
- name TEXT UNIQUE — e.g. 'full-coding-pipeline', 'spec-only'
- stages_json TEXT — JSON array of stage definitions (see DAG schema below)
- created_at TEXT

**pipelines** (new — running instances)
- id INTEGER PRIMARY KEY
- pipeline_def_id INTEGER FK → pipeline_defs
- repo_id INTEGER FK → repos
- clone_path TEXT — path to this pipeline's isolated clone
- branch TEXT — git branch this pipeline operates on
- status TEXT — 'pending', 'running', 'paused', 'completed', 'failed', 'needs_attention'
- current_stage_index INTEGER DEFAULT 0
- config_json TEXT — runtime overrides (model, context threshold, max iterations per stage)
- created_at TEXT
- updated_at TEXT

**pipeline_stages** (new — stage execution state)
- id INTEGER PRIMARY KEY
- pipeline_id INTEGER FK → pipelines
- stage_index INTEGER
- stage_type TEXT
- agent_type TEXT — 'claude' or 'codex'
- status TEXT — 'pending', 'running', 'review_loop', 'completed', 'failed', 'needs_attention', 'skipped'
- iteration INTEGER DEFAULT 0
- max_iterations INTEGER
- output_artifact TEXT NULLABLE — path to the markdown doc produced
- escalation_reason TEXT NULLABLE
- started_at TEXT NULLABLE
- completed_at TEXT NULLABLE

**agent_sessions** (new — individual agent invocations within a stage)
- id INTEGER PRIMARY KEY
- pipeline_stage_id INTEGER FK → pipeline_stages
- session_type TEXT — 'claude_sdk', 'codex_app_server'
- session_id TEXT NULLABLE — Claude SDK session_id or Codex thread_id
- prompt_id INTEGER FK → prompts NULLABLE
- prompt_override TEXT NULLABLE — when we need to inject dynamic context
- status TEXT — 'running', 'completed', 'failed', 'interrupted', 'context_limit'
- context_usage_pct REAL NULLABLE — last known context usage
- cost_usd REAL DEFAULT 0
- token_input INTEGER DEFAULT 0
- token_output INTEGER DEFAULT 0
- started_at TEXT
- completed_at TEXT NULLABLE

**session_logs** (replaces job_logs — more granular)
- id INTEGER PRIMARY KEY
- agent_session_id INTEGER FK → agent_sessions
- event_type TEXT — 'assistant_message', 'tool_use', 'command_exec', 'file_change', 'error', 'context_warning', 'review_feedback', 'escalation'
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
- preconditions_json TEXT — JSON array of condition objects (see below)
- postconditions_json TEXT — JSON array of condition objects
- invariants_json TEXT NULLABLE — JSON array of invariant checks
- output_artifacts_json TEXT NULLABLE — JSON array of file paths produced
- commit_sha TEXT NULLABLE — git commit when task completed
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
    "type": "custom",
    "check": "grep -q 'class User' src/models.py",
    "description": "User model must be defined"
  }
]
```

### HTN task lifecycle:

1. **Decomposition**: The spec/plan stage produces compound tasks. The implementation planner decomposes them into primitives.
2. **Readiness**: A primitive task becomes 'ready' when all hard deps are completed and all preconditions are satisfiable.
3. **Assignment**: The orchestrator picks the highest-priority ready task and assigns it to the current agent session.
4. **Execution**: The agent works on the task. The harness can verify postconditions after completion.
5. **Verification**: Postconditions are checked programmatically where possible (file_exists, tests_pass, lint_clean). Custom checks run as bash commands.
6. **Propagation**: Completing a task may unblock dependent tasks, changing their status from 'not_ready' to 'ready'.

The orchestrator queries the task graph to determine what to tell the agent:
```sql
SELECT * FROM htn_tasks 
WHERE pipeline_id = ? AND status = 'ready' AND task_type = 'primitive'
ORDER BY priority DESC, ordering ASC LIMIT 1
```

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
    
    async def get_next_ready_task(self, pipeline_id: int) -> HtnTask | None:
        """Return highest-priority primitive task that is ready."""
    
    async def mark_in_progress(self, task_id: int, session_id: int) -> None:
        """Assign task to a session and set status to in_progress."""
    
    async def verify_postconditions(
        self, task_id: int, working_dir: str
    ) -> list[ConditionResult]:
        """Run postcondition checks. Returns pass/fail for each condition."""
    
    async def complete_task(
        self, task_id: int, commit_sha: str | None, diary: str
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
VERIFIERS = {
    "file_exists": lambda cond, cwd: Path(cwd, cond["path"]).exists(),
    "tests_pass": lambda cond, cwd: run_cmd(f"pytest {cond['pattern']}", cwd) == 0,
    "lint_clean": lambda cond, cwd: run_cmd(f"ruff check {cond['scope']}", cwd) == 0,
    "type_check": lambda cond, cwd: run_cmd("mypy src/", cwd) == 0,
    "custom": lambda cond, cwd: run_cmd(cond["check"], cwd) == 0,
}
```

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
- The `assigned_session_id` field tracks which teammate owns which task
- The dependency graph prevents two teammates from working on conflicting tasks
- `invariants_json` can be checked continuously during parallel execution
- The `diary_entry` field enables cross-teammate knowledge sharing

---

## Pipeline DAG schema

Each pipeline_def.stages_json is an ordered array of stage definitions. The orchestrator executes them in sequence, with review loops modeled as sub-stages.

```json
[
  {
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
    "name": "Implementation",
    "type": "impl_task",
    "agent": "claude",
    "prompt": "impl_task_default",
    "model": "claude-sonnet-4-6",
    "max_iterations": 50,
    "context_threshold_pct": 60,
    "on_context_limit": "new_session_continue"
  },
  {
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
    "name": "Validation",
    "type": "validation",
    "agent": "claude",
    "prompt": "validation_default",
    "model": "claude-sonnet-4-6",
    "max_iterations": 3,
    "uses_devbrowser": true,
    "record_on_success": true,
    "on_failure": "loop_to_code_review"
  }
]
```

**Exit conditions for review loops:**
- `structured_approval`: The review agent's structured output includes `{"approved": true, "severity": "low"}`. Only "low" or no issues = approved.
- `escalate`: Create an escalation and pause the pipeline.
- `proceed_with_warnings`: Log warnings but continue.

**on_context_limit options:**
- `new_session_continue`: Spawn a new agent session with the generic continuation prompt. The new session studies the spec/plan and picks up where the last left off.
- `escalate`: Pause and ask the human.

---

## Orchestrator state machine

### PipelineOrchestrator class

The orchestrator is the core engine. It replaces clean-your-room's JobRunner.

**Lifecycle per pipeline:**
1. Clone the repo to an isolated directory: `~/.build-your-room/clones/{pipeline_id}/`
2. Create a git branch: `pipeline/{pipeline_id}/{stage_name}`
3. Walk the DAG stages in order
4. For each stage, run the appropriate agent adapter
5. Between stages, check the output artifact exists and the exit condition is met
6. On escalation, pause the pipeline and push to the escalation queue

**Key methods:**
- `async run_pipeline(pipeline_id)` — main loop
- `async run_stage(pipeline_id, stage_index)` — dispatch to agent adapter
- `async run_review_loop(stage, primary_session_output)` — review loop logic
- `async check_context_and_maybe_rotate(session)` — hook called after each agent turn
- `async escalate(pipeline_id, stage_id, reason, context)` — create escalation, set pipeline status
- `async resume_pipeline(pipeline_id, resolution)` — human resumes after escalation

**Concurrency model:**
- Each pipeline runs as an `asyncio.Task`
- A semaphore limits concurrent pipelines (configurable, default 10)
- Each pipeline holds its own cancel Event (inherited from clean-your-room pattern)
- The `active_pipelines` dict maps pipeline_id → (Task, Event)

**Context rotation logic (hook in agent session):**
After every agent turn, call `get_context_usage()` (Claude SDK) or check token counts (Codex).
If usage > threshold:
1. Log a `context_warning` event
2. Capture the current output artifact path
3. End the current session gracefully
4. Spawn a new session with the continuation prompt: "Study the specs in specs/* and the task list. Pick up the next incomplete task."
5. The new session inherits the same working directory but starts fresh context

---

## Agent adapters

### ClaudeAgentAdapter

Wraps the `claude-agent-sdk` Python package. Uses `ClaudeSDKClient` for bidirectional sessions with hooks.

```python
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    tool, create_sdk_mcp_server,
    AssistantMessage, TextBlock
)

class ClaudeAgentAdapter:
    async def run_session(self, config: SessionConfig) -> SessionResult:
        options = ClaudeAgentOptions(
            model=config.model,
            cwd=config.working_dir,
            system_prompt=config.system_prompt,
            permission_mode="bypassPermissions",
            allow_dangerously_skip_permissions=True,
            max_turns=config.max_turns,
            setting_sources=["project"],  # reads CLAUDE.md
            # Explicitly disable 1M context
            # betas=[]  # do NOT include context-1m beta
        )
        
        async with ClaudeSDKClient(options=options) as client:
            await client.query(config.prompt)
            async for msg in client.receive_response():
                # Stream to LogBuffer
                await self.log_buffer.append(session_id, msg)
                
                # Context check hook
                usage = await client.get_context_usage()
                if usage and self._over_threshold(usage, config.threshold):
                    return SessionResult(
                        status='context_limit',
                        context_pct=self._calc_pct(usage)
                    )
            
            return SessionResult(status='completed')
```

**Key capabilities used:**
- `get_context_usage()` — monitor context window
- `setting_sources=["project"]` — reads CLAUDE.md from the repo
- `permission_mode="bypassPermissions"` — fully autonomous
- Custom MCP tools (optional): can expose harness-level tools to the agent

### CodexAppServerAdapter

Wraps the Codex app-server stdio JSON-RPC protocol.

```python
import asyncio, json

class CodexAppServerAdapter:
    async def run_session(self, config: SessionConfig) -> SessionResult:
        proc = await asyncio.create_subprocess_exec(
            'codex', 'app-server',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=config.working_dir
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
                "cwd": config.working_dir,
                "approvalPolicy": "never",
                "sandbox": "dangerFullAccess"
            }
        })
        thread_id = resp["result"]["thread"]["id"]
        
        # Start turn with the prompt
        await self._send(proc, {
            "method": "turn/start", "id": 2, "params": {
                "threadId": thread_id,
                "input": [{"type": "text", "text": config.prompt}]
            }
        })
        
        # Stream events until turn/completed
        async for event in self._read_events(proc):
            await self.log_buffer.append(session_id, event)
            if event.get("method") == "turn/completed":
                status = event["params"]["turn"]["status"]
                return SessionResult(
                    status='completed' if status == 'completed' else 'failed'
                )
        
        return SessionResult(status='failed')
```

**Review mode for code review:**
Use `review/start` with `target: {"type": "uncommittedChanges"}` to trigger Codex's built-in reviewer. Parse `exitedReviewMode` item for structured feedback.

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
1. **Primary agent** (Claude) produces an artifact (spec markdown, implementation plan markdown)
2. **Review agent** (Codex) receives the artifact with a review prompt
3. Codex returns structured output: `{approved, max_severity, issues, feedback_markdown}`
4. **Decision gate:**
   - If `approved == true` AND `max_severity in ['none', 'low']` → stage complete
   - If `max_severity in ['medium']` and iteration < max → feed feedback back to primary agent
   - If `max_severity in ['high', 'critical']` → always feed back regardless of iteration count
   - If iteration >= max_review_rounds → escalate or proceed based on `on_max_rounds`

**Feeding feedback back:**
When the review loop continues, we need to send feedback to the primary agent. Two modes:

**Same-session feedback** (if context permits):
Send the feedback markdown directly to the existing Claude session as a follow-up message.

**New-session feedback** (if context is tight):
1. End the current Claude session
2. Start a new session with prompt: "You previously wrote {artifact_path}. A reviewer provided this feedback: {feedback_markdown}. Please revise the document addressing all issues."

The context monitor decides which mode. At 60% threshold, there's typically room for 1-2 more feedback rounds in the same session.

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
1. Queries `htn_tasks` for the highest-priority ready primitive task
2. Verifies preconditions programmatically where possible
3. Constructs a focused prompt including the task description, preconditions, and context
4. Passes it to the agent session

After each iteration, the orchestrator:
1. Checks postconditions programmatically
2. Updates task status in the DB
3. Propagates readiness to dependent tasks
4. Records the commit SHA and diary entry

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
- Lint, run tests and type checking after making changes until all pass
- Write a diary entry summarizing what you learned
- Git commit the changes (no Claude/OpenAI attribution), push if remote exists

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
2. If total > threshold → graceful rotate
3. New session gets a fresh prompt for the next ready task from the HTN graph
4. The diary entries (stored in `htn_tasks.diary_entry` and in `diary/` files) provide continuity between sessions

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
WHERE pipeline_id = ? AND task_type = 'primitive'
```

The agent still maintains a human-readable task list in `specs/task-list.md` as a convenience, but the DB is the source of truth. The harness syncs the markdown from the DB after each task completes.

### Diary format (per task, stored in both DB and filesystem):

```markdown
# Task: Implement user authentication
## Session: 7 | Complexity: medium | Commit: abc123

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
- Should refresh tokens be stored in Redis or SQLite?
```

---

## Validation stage

### Code review phase (Codex)

Uses Codex app-server's `review/start` with target `uncommittedChanges` (or the diff since pipeline start).

Each round of issues found triggers a fresh Codex session to fix bugs:
1. Codex reviewer produces structured issue list
2. A new Codex session receives: "Fix these issues: {issues_json}. Run tests after each fix."
3. After fixing, loop back to review
4. Max 3 rounds, then escalate remaining issues

### Browser validation phase (dev-browser)

For projects with a web UI component, the validation stage includes browser testing.

**Setup (automated by the agent):**
1. Agent starts the dev server(s) per CLAUDE.md instructions
2. Agent uses dev-browser skill commands to:
   - Navigate to the running app
   - Execute test scenarios
   - Check for console errors
   - Verify UI functionality

**dev-browser integration:**
The skill is installed in the Claude Code environment. The agent invokes it via bash:
```bash
# Start the dev-browser server (runs in background)
cd ~/.claude/skills/dev-browser && npm run start-server &

# Then the agent uses Bash(npx tsx:*) tool calls to interact
```

**Recording on success:**
When validation passes, the agent uses dev-browser to record a GIF/video of the working functionality. This recording is saved as a validation artifact.

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
4. If web UI: dev-browser validation passed, recording saved
5. Git committed and pushed
6. Task list updated
7. Diary entry written

---

## Dashboard design

The dashboard is designed for monitoring 10+ parallel pipelines at a glance.

### Main dashboard (/)

**Pipeline cards grid** — each card shows:
- Pipeline name + repo name
- Current stage (highlighted in the DAG mini-visualization)
- Stage progress: "Impl task 7/50" or "Review round 2/5"
- HTN task progress: "12/28 tasks completed" with a mini progress bar
- Status badge: running (blue), needs_attention (amber), completed (green), failed (red), paused (gray)
- Last activity timestamp
- Context usage bar (if stage is running)
- Cost accumulator (total USD spent on this pipeline)
- Clone cleanup button (visible when pipeline is completed/failed)

**Escalation banner** — if any escalations are open, a persistent banner at top:
"3 pipelines need your attention" — click to see the escalation queue.

### Escalation queue (/escalations)

List of open escalations with:
- Pipeline name + stage name
- Reason (max iterations, design decision, etc.)
- Context snapshot (what the agents were working on)
- Action buttons: Resolve (with text input), Dismiss, Pause pipeline, Kill pipeline

### Pipeline detail (/pipelines/{id})

**DAG visualization** — the pipeline's stages shown as a horizontal flow with the current position highlighted.

**Stage tabs** — click each stage to see:
- Session list with logs
- Output artifacts (rendered markdown)
- Review feedback history
- Context usage chart over time

**HTN task tree** — interactive tree view of the hierarchical task network:
- Compound tasks expand to show subtasks
- Color-coded by status (green=done, blue=in progress, gray=not ready, amber=blocked, red=failed, pink=needs human)
- Each task shows: name, assigned session, commit SHA, precondition/postcondition status
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

Visual DAG builder for defining pipeline definitions. Start simple: a form with ordered stage entries. Each entry selects: stage type, agent type, prompt, model, max iterations, review config.

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
│   ├── clone_manager.py           # Repo cloning, branch management, cleanup
│   ├── htn_planner.py             # HTN task graph management, readiness propagation, postcondition verification
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
- aiosqlite
- claude-agent-sdk (bundles Claude Code CLI)
- sse-starlette (for SSE endpoints)
- httpx (for any HTTP needs)

**External requirements (not pip-installable):**
- Codex CLI (`codex` binary on PATH)
- dev-browser skill (installed to ~/.claude/skills/)
- Node.js (for dev-browser)

---

## Configuration

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| BUILD_YOUR_ROOM_DIR | ~/.build-your-room | Base directory for clones, database |
| DEFAULT_CLAUDE_MODEL | claude-sonnet-4-6 | Default model for Claude stages |
| DEFAULT_CODEX_MODEL | gpt-5.1-codex | Default model for Codex stages |
| SPEC_CLAUDE_MODEL | claude-opus-4-6 | Model for spec authoring (higher capability) |
| CONTEXT_THRESHOLD_PCT | 60 | Default context usage threshold |
| MAX_CONCURRENT_PIPELINES | 10 | Semaphore limit |
| ANTHROPIC_API_KEY | (required) | For Claude Agent SDK |
| OPENAI_API_KEY | (required for Codex) | For Codex app-server |
| DEVBROWSER_SKILL_PATH | ~/.claude/skills/dev-browser | Path to dev-browser skill |
| LOG_LEVEL | INFO | Python logging level |

### Runtime config per pipeline (config_json)

```json
{
  "claude_model": "claude-opus-4-6",
  "codex_model": "gpt-5.1-codex",
  "context_threshold_pct": 60,
  "disable_1m_context": true,
  "max_concurrent_stages": 1,
  "auto_push": true,
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
2. New DB schema with migration from old tables (including HTN task tables)
3. PipelineOrchestrator skeleton with stage dispatch
4. CloneManager for repo cloning, branch management, and cleanup
5. ContextMonitor as a reusable hook
6. Config module with all env vars
7. HTNPlanner: task graph CRUD, readiness propagation, postcondition verification

### Phase 2: Agent adapters
8. ClaudeAgentAdapter with streaming, context monitoring, session rotation
9. CodexAppServerAdapter with stdio JSON-RPC protocol, structured output parsing
10. Review loop logic with structured approval parsing
11. Adapter unit tests with mocked agent responses

### Phase 3: Stage runners
12. SpecAuthorStage + review loop integration
13. ImplPlanStage + review loop integration + HTN task graph population from structured output
14. ImplTaskStage with HTN task selection, context rotation, postcondition verification
15. CodeReviewStage with Codex review/start and bug fix loop
16. ValidationStage with test runner detection and devbrowser integration

### Phase 4: Dashboard
17. Dashboard template with pipeline cards grid + HTN progress indicators
18. Escalation queue page
19. Pipeline detail page with DAG viz, HTN task tree, and live logs
20. Pipeline builder form
21. Prompt management (extend existing)
22. Clone cleanup buttons (per-pipeline and bulk)

### Phase 5: Polish + validation
23. Property-based tests for orchestrator state machine and HTN planner (Hypothesis)
24. Integration tests with mock adapters
25. Devbrowser recording integration in validation stage
26. Documentation (README, CLAUDE.md, AGENTS.md)
27. Default prompt templates for all stage types

Each phase is a natural checkpoint. The system is usable after Phase 3 (CLI-driven). Phase 4 adds the monitoring dashboard. Phase 5 hardens it.

---

