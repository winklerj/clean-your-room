import aiosqlite
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    github_url TEXT NOT NULL,
    org TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    clone_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    template TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    feature_description TEXT,
    prompt_id INTEGER NOT NULL REFERENCES prompts(id),
    max_iterations INTEGER NOT NULL DEFAULT 20,
    status TEXT NOT NULL DEFAULT 'pending',
    current_iteration INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS job_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    iteration INTEGER NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

DEFAULT_PROMPTS = [
    (
        "Create Spec",
        """Study the existing specs/*

Identify one specification that still needs created for the clean room deep research specifications and create the specification file.

Focus on ONE specification
Include:
- Provable Properties Catalog
- Purity Boundary Map
- Verification Tooling Selection
- Property Specifications""",
    ),
    (
        "Improve Spec",
        """Study the existing specs/*

Identify one specification for the clean room deep research specifications and improve the specification file. Persist changes when done.

Focus on ONE specification
Include:
- Provable Properties Catalog
- Purity Boundary Map
- Verification Tooling Selection
- Property Specifications

Note: If creating diagrams make them mermaid diagrams""",
    ),
]


async def init_db(db_path: Path) -> None:
    """Initialize database schema and seed default prompts."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(SCHEMA)
        cursor = await db.execute("SELECT COUNT(*) FROM prompts")
        count = (await cursor.fetchone())[0]
        if count == 0:
            for name, template in DEFAULT_PROMPTS:
                await db.execute(
                    "INSERT INTO prompts (name, template) VALUES (?, ?)",
                    (name, template),
                )
            await db.commit()


async def get_db(db_path: Path) -> aiosqlite.Connection:
    """Get a database connection with row factory and foreign keys enabled."""
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    return db
