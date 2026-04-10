from __future__ import annotations

import logging

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from build_your_room.config import DATABASE_URL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema DDL — PostgreSQL
# ---------------------------------------------------------------------------

SCHEMA = """
-- Repos: local repository references
CREATE TABLE IF NOT EXISTS repos (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    local_path TEXT NOT NULL,
    git_url TEXT,
    default_branch TEXT NOT NULL DEFAULT 'main',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived INTEGER NOT NULL DEFAULT 0
);

-- Prompts: prompt templates for agent stages
CREATE TABLE IF NOT EXISTS prompts (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    body TEXT NOT NULL,
    stage_type TEXT NOT NULL DEFAULT 'custom',
    agent_type TEXT NOT NULL DEFAULT 'claude',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Pipeline definitions: composable stage-graph definitions
CREATE TABLE IF NOT EXISTS pipeline_defs (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    stage_graph_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Pipelines: running instances of a pipeline definition
CREATE TABLE IF NOT EXISTS pipelines (
    id SERIAL PRIMARY KEY,
    pipeline_def_id INTEGER NOT NULL REFERENCES pipeline_defs(id),
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    clone_path TEXT NOT NULL,
    workspace_ref TEXT,
    review_base_rev TEXT NOT NULL,
    head_rev TEXT,
    workspace_state TEXT NOT NULL DEFAULT 'clean',
    dirty_snapshot_artifact TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    current_stage_key TEXT,
    owner_token TEXT,
    last_heartbeat_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    recovery_state_json TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Pipeline stages: stage execution state
CREATE TABLE IF NOT EXISTS pipeline_stages (
    id SERIAL PRIMARY KEY,
    pipeline_id INTEGER NOT NULL REFERENCES pipelines(id),
    stage_key TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    entry_edge_key TEXT,
    stage_type TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    entry_rev TEXT,
    exit_rev TEXT,
    iteration INTEGER NOT NULL DEFAULT 0,
    max_iterations INTEGER NOT NULL,
    output_artifact TEXT,
    escalation_reason TEXT,
    owner_token TEXT,
    last_heartbeat_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

-- Agent sessions: individual agent invocations within a stage
CREATE TABLE IF NOT EXISTS agent_sessions (
    id SERIAL PRIMARY KEY,
    pipeline_stage_id INTEGER NOT NULL REFERENCES pipeline_stages(id),
    session_type TEXT NOT NULL,
    session_id TEXT,
    prompt_id INTEGER REFERENCES prompts(id),
    prompt_override TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    context_usage_pct REAL,
    cost_usd REAL NOT NULL DEFAULT 0,
    token_input INTEGER NOT NULL DEFAULT 0,
    token_output INTEGER NOT NULL DEFAULT 0,
    resume_state_json TEXT,
    owner_token TEXT,
    last_heartbeat_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

-- Session logs: granular event log
CREATE TABLE IF NOT EXISTS session_logs (
    id SERIAL PRIMARY KEY,
    agent_session_id INTEGER NOT NULL REFERENCES agent_sessions(id),
    event_type TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Escalations: human attention queue
CREATE TABLE IF NOT EXISTS escalations (
    id SERIAL PRIMARY KEY,
    pipeline_id INTEGER NOT NULL REFERENCES pipelines(id),
    pipeline_stage_id INTEGER REFERENCES pipeline_stages(id),
    reason TEXT NOT NULL,
    context_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

-- HTN tasks: hierarchical task network decomposition
CREATE TABLE IF NOT EXISTS htn_tasks (
    id SERIAL PRIMARY KEY,
    pipeline_id INTEGER NOT NULL REFERENCES pipelines(id),
    parent_task_id INTEGER REFERENCES htn_tasks(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'not_ready',
    priority INTEGER NOT NULL DEFAULT 0,
    ordering INTEGER NOT NULL,
    assigned_session_id INTEGER REFERENCES agent_sessions(id),
    claim_token TEXT,
    claim_owner_token TEXT,
    claim_expires_at TIMESTAMPTZ,
    preconditions_json TEXT NOT NULL DEFAULT '[]',
    postconditions_json TEXT NOT NULL DEFAULT '[]',
    invariants_json TEXT,
    output_artifacts_json TEXT,
    checkpoint_rev TEXT,
    estimated_complexity TEXT,
    diary_entry TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

-- HTN task dependency edges beyond parent-child
CREATE TABLE IF NOT EXISTS htn_task_deps (
    id SERIAL PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES htn_tasks(id),
    depends_on_task_id INTEGER NOT NULL REFERENCES htn_tasks(id),
    dep_type TEXT NOT NULL DEFAULT 'hard'
);
"""

# ---------------------------------------------------------------------------
# Default seed data
# ---------------------------------------------------------------------------

DEFAULT_PROMPTS: list[tuple[str, str, str, str]] = [
    (
        "spec_author_default",
        "Study the repository and produce a formal specification document.",
        "spec_author",
        "claude",
    ),
    (
        "spec_review_default",
        (
            "Review the following specification document. Mentally simulate a TLA+ "
            "specification: define invariants, preconditions, and postconditions for "
            "the system described. Use this mental model to validate the document.\n\n"
            "Do NOT create a TLA+ file. Use the formal reasoning to find:\n"
            "- Logical contradictions\n"
            "- Missing edge cases\n"
            "- Violated invariants\n"
            "- Unspecified preconditions\n"
            "- Ambiguous postconditions\n\n"
            "Return structured JSON output with your assessment."
        ),
        "spec_review",
        "codex",
    ),
    (
        "impl_plan_default",
        (
            "Study the spec documents and produce a hierarchical task decomposition.\n\n"
            "For each task, provide:\n"
            "- name: short identifier\n"
            "- description: what to implement\n"
            "- type: 'compound' (needs subtasks) or 'primitive' (directly implementable)\n"
            "- preconditions: what must be true before starting\n"
            "- postconditions: what must be true after completion\n"
            "- dependencies: which other tasks must complete first\n"
            "- estimated_complexity: trivial/small/medium/large/epic\n"
            "- priority: relative importance (higher = do first)"
        ),
        "impl_plan",
        "claude",
    ),
    (
        "impl_plan_review_default",
        "Review the implementation plan for completeness, ordering, and feasibility.",
        "impl_plan_review",
        "codex",
    ),
    (
        "code_review_default",
        (
            "Review the following code diff. Assess code quality, correctness, "
            "security, and adherence to best practices.\n\n"
            "Look for:\n"
            "- Bugs and logic errors\n"
            "- Security vulnerabilities (OWASP top 10)\n"
            "- Missing error handling\n"
            "- Performance issues\n"
            "- Code style and naming conventions\n"
            "- Missing or inadequate tests\n\n"
            "Return structured JSON output with your assessment."
        ),
        "code_review",
        "codex",
    ),
    (
        "bug_fix_default",
        (
            "You are a bug-fix agent. Address the code review issues reported below. "
            "Fix each issue in the codebase, ensuring tests pass and code is clean.\n\n"
            "Important:\n"
            "- Fix all reported issues\n"
            "- Run tests after making changes\n"
            "- Do not introduce new issues\n"
            "- Keep changes minimal and focused on the reported problems"
        ),
        "bug_fix",
        "codex",
    ),
]

SEED_PROMPTS_SQL = """
INSERT INTO prompts (name, body, stage_type, agent_type)
VALUES (%s, %s, %s, %s)
ON CONFLICT (name) DO NOTHING
"""

# ---------------------------------------------------------------------------
# Pool management
# ---------------------------------------------------------------------------

_pool: AsyncConnectionPool | None = None


async def init_pool(dsn: str | None = None) -> AsyncConnectionPool:
    """Create and open the global async connection pool."""
    global _pool
    if _pool is not None:
        return _pool
    conninfo = dsn or DATABASE_URL
    _pool = AsyncConnectionPool(
        conninfo=conninfo,
        min_size=2,
        max_size=10,
        open=False,
        kwargs={"row_factory": dict_row},
    )
    await _pool.open()
    return _pool


async def close_pool() -> None:
    """Close the global async connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> AsyncConnectionPool:
    """Return the global pool. Raises if not initialized."""
    if _pool is None:
        raise RuntimeError("Connection pool not initialized — call init_pool() first")
    return _pool


# ---------------------------------------------------------------------------
# Schema initialization
# ---------------------------------------------------------------------------


async def init_db(dsn: str | None = None) -> None:
    """Create all tables and seed default prompts."""
    pool = await init_pool(dsn)
    async with pool.connection() as conn:
        # Execute DDL
        await conn.execute(SCHEMA)

        # Seed default prompts (idempotent via ON CONFLICT)
        for name, body, stage_type, agent_type in DEFAULT_PROMPTS:
            await conn.execute(
                SEED_PROMPTS_SQL, (name, body, stage_type, agent_type)
            )
        await conn.commit()
    logger.info("Database schema initialized")


