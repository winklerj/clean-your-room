from pathlib import Path

import aiosqlite

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

        Identify one specification for the clean room deep research specifications and improve the specification file. We will be creating a CLI deep research CLI tool from the specifications. Persist jj changes when done.

        Important:
        Describe behavioral contracts and constraints, not implementation. Do not reference variable names, function names, file paths, migration IDs, or internal state fields from the source. A reader should be able to build a compatible system from the spec without reproducing the original code's structure or naming.

        Focus on ONE specification
        Include:
        - Provable Properties Catalog: Which invariants, safety properties, and correctness guarantees must be formally verified, not just tested? Distinguish between properties that should be proven (critical path, security boundaries, financial calculations) and properties where test coverage is sufficient (UI formatting, logging, non-critical defaults).
        - Purity Boundary Map: A clear architectural separation between the deterministic, side-effect-free core (where formal verification can operate) and the effectful shell (I/O, network, database, user interaction). It dictates module boundaries, dependency direction, and how state flows through the system. The pure core must be designed so that verification tools can reason about it without mocking the entire universe.
        - Verification Tooling Selection: Based on the language and the properties to be proven, the Builder selects the appropriate formal verification stack (Kani for Rust, CBMC for C/C++, Dafny, TLA+ for distributed systems, Antithesis Bombadil for frontend, Lean 4 for system verification, Promela, Raft, Paxos, Alloy, PRISM, etc.) and identifies any constraints these tools impose on code structure.
        - Propery Specifications: Where possible, draft the actual formal property definitions (e.g., Kani proof harnesses, Dafny contracts, TLA+ invariants) alongside the behavioral spec. These aren't implementation. They are the formal expression of what the spec already says in natural language. They serve as a second, mathematically precise encoding of the requirements.

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
        row = await cursor.fetchone()
        assert row is not None
        count = row[0]
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
