import pytest
import aiosqlite

from clean_room.db import init_db, get_db


@pytest.fixture
async def db_path(tmp_path):
    path = tmp_path / "test.db"
    await init_db(path)
    return path


@pytest.mark.asyncio
async def test_init_db_creates_all_tables(db_path):
    """Schema init must create repos, prompts, jobs, and job_logs tables."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in await cursor.fetchall()]
    assert "repos" in tables
    assert "prompts" in tables
    assert "jobs" in tables
    assert "job_logs" in tables


@pytest.mark.asyncio
async def test_init_db_seeds_default_prompts(db_path):
    """Schema init must seed the two default prompts."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT name FROM prompts ORDER BY id")
        names = [row[0] for row in await cursor.fetchall()]
    assert "Create Spec" in names
    assert "Improve Spec" in names


@pytest.mark.asyncio
async def test_init_db_is_idempotent(db_path):
    """Running init_db twice must not duplicate seed data or error."""
    await init_db(db_path)
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM prompts")
        count = (await cursor.fetchone())[0]
    assert count == 2


@pytest.mark.asyncio
async def test_foreign_keys_enforced(db_path):
    """Foreign key constraints must be active via get_db."""
    db = await get_db(db_path)
    try:
        cursor = await db.execute("PRAGMA foreign_keys")
        fk = (await cursor.fetchone())[0]
    finally:
        await db.close()
    assert fk == 1
