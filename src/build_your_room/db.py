from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    local_path TEXT NOT NULL,
    git_url TEXT,
    default_branch TEXT NOT NULL DEFAULT 'main',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    body TEXT NOT NULL,
    stage_type TEXT NOT NULL DEFAULT 'custom',
    agent_type TEXT NOT NULL DEFAULT 'claude',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

DEFAULT_PROMPTS = [
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
]


async def init_db(db_path: Path) -> None:
    """Initialize database schema and seed default prompts."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(SCHEMA)
        cursor = await db.execute("SELECT COUNT(*) FROM prompts")
        row = await cursor.fetchone()
        assert row is not None
        count = row[0]
        if count == 0:
            for name, body, stage_type, agent_type in DEFAULT_PROMPTS:
                await db.execute(
                    "INSERT INTO prompts (name, body, stage_type, agent_type) "
                    "VALUES (?, ?, ?, ?)",
                    (name, body, stage_type, agent_type),
                )
            await db.commit()


async def get_db(db_path: Path) -> aiosqlite.Connection:
    """Get a database connection with row factory and foreign keys enabled."""
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    return db
