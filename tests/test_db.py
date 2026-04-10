import pytest
import aiosqlite

from build_your_room.db import init_db, get_db


@pytest.fixture
async def db_path(tmp_path):
    path = tmp_path / "test.db"
    await init_db(path)
    return path


@pytest.mark.asyncio
async def test_init_db_creates_all_tables(db_path):
    """Schema init must create repos and prompts tables."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in await cursor.fetchall()]
    assert "repos" in tables
    assert "prompts" in tables


@pytest.mark.asyncio
async def test_init_db_seeds_default_prompts(db_path):
    """Schema init must seed default prompts with stage_type and agent_type."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT name, stage_type, agent_type FROM prompts ORDER BY id")
        rows = await cursor.fetchall()
    names = [row["name"] for row in rows]
    assert "spec_author_default" in names
    assert "spec_review_default" in names
    assert "impl_plan_default" in names
    # Check stage_type and agent_type are populated
    spec_author = next(r for r in rows if r["name"] == "spec_author_default")
    assert spec_author["stage_type"] == "spec_author"
    assert spec_author["agent_type"] == "claude"
    spec_review = next(r for r in rows if r["name"] == "spec_review_default")
    assert spec_review["stage_type"] == "spec_review"
    assert spec_review["agent_type"] == "codex"


@pytest.mark.asyncio
async def test_init_db_is_idempotent(db_path):
    """Running init_db twice must not duplicate seed data or error."""
    await init_db(db_path)
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM prompts")
        row = await cursor.fetchone()
        assert row is not None
        count = row[0]
    assert count == 4


@pytest.mark.asyncio
async def test_foreign_keys_enforced(db_path):
    """Foreign key constraints must be active via get_db."""
    db = await get_db(db_path)
    try:
        cursor = await db.execute("PRAGMA foreign_keys")
        row = await cursor.fetchone()
        assert row is not None
        fk = row[0]
    finally:
        await db.close()
    assert fk == 1


@pytest.mark.asyncio
async def test_repos_table_has_local_path_column(db_path):
    """Repos table uses local_path instead of github_url."""
    db = await get_db(db_path)
    try:
        cursor = await db.execute("PRAGMA table_info(repos)")
        columns = {row[1] for row in await cursor.fetchall()}
    finally:
        await db.close()
    assert "local_path" in columns
    assert "name" in columns
    assert "git_url" in columns
    assert "default_branch" in columns
    assert "archived" in columns
    assert "github_url" not in columns
    assert "slug" not in columns
