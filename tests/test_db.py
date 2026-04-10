from __future__ import annotations

import pytest

from build_your_room.db import get_pool, init_db, close_pool


ALL_TABLES = [
    "repos",
    "prompts",
    "pipeline_defs",
    "pipelines",
    "pipeline_stages",
    "agent_sessions",
    "session_logs",
    "escalations",
    "htn_tasks",
    "htn_task_deps",
]


@pytest.mark.asyncio
async def test_init_db_creates_all_tables(initialized_db):
    """Schema init must create all spec-defined tables."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        tables = [row["tablename"] for row in await cur.fetchall()]
    for table in ALL_TABLES:
        assert table in tables, f"Missing table: {table}"


@pytest.mark.asyncio
async def test_init_db_seeds_default_prompts(initialized_db):
    """Schema init must seed default prompts with stage_type and agent_type."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT name, stage_type, agent_type FROM prompts ORDER BY id"
        )
        rows = await cur.fetchall()
    names = [row["name"] for row in rows]
    assert "spec_author_default" in names
    assert "spec_review_default" in names
    assert "impl_plan_default" in names
    assert "impl_plan_review_default" in names

    spec_author = next(r for r in rows if r["name"] == "spec_author_default")
    assert spec_author["stage_type"] == "spec_author"
    assert spec_author["agent_type"] == "claude"

    spec_review = next(r for r in rows if r["name"] == "spec_review_default")
    assert spec_review["stage_type"] == "spec_review"
    assert spec_review["agent_type"] == "codex"


@pytest.mark.asyncio
async def test_init_db_is_idempotent(initialized_db):
    """Running init_db twice must not duplicate seed data or error."""
    # init_db was already called by the fixture; call it again
    await close_pool()
    await init_db(initialized_db)
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) AS cnt FROM prompts")
        row = await cur.fetchone()
        assert row is not None
        count = row["cnt"]
    assert count == 4


@pytest.mark.asyncio
async def test_repos_table_has_correct_columns(initialized_db):
    """Repos table has expected columns and NOT old GitHub-specific ones."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'repos' AND table_schema = 'public'"
        )
        columns = {row["column_name"] for row in await cur.fetchall()}
    assert "local_path" in columns
    assert "name" in columns
    assert "git_url" in columns
    assert "default_branch" in columns
    assert "archived" in columns
    # Old columns must not exist
    assert "github_url" not in columns
    assert "slug" not in columns


@pytest.mark.asyncio
async def test_htn_tasks_table_has_correct_columns(initialized_db):
    """HTN tasks table has all spec-defined columns."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'htn_tasks' AND table_schema = 'public'"
        )
        columns = {row["column_name"] for row in await cur.fetchall()}
    expected = {
        "id", "pipeline_id", "parent_task_id", "name", "description",
        "task_type", "status", "priority", "ordering",
        "assigned_session_id", "claim_token", "claim_owner_token",
        "claim_expires_at", "preconditions_json", "postconditions_json",
        "invariants_json", "output_artifacts_json", "checkpoint_rev",
        "estimated_complexity", "diary_entry",
        "created_at", "started_at", "completed_at",
    }
    assert expected.issubset(columns), f"Missing: {expected - columns}"


@pytest.mark.asyncio
async def test_foreign_keys_enforced(initialized_db):
    """Foreign key constraints must prevent invalid references."""
    pool = get_pool()
    async with pool.connection() as conn:
        with pytest.raises(Exception):
            await conn.execute(
                "INSERT INTO pipelines (pipeline_def_id, repo_id, clone_path, "
                "review_base_rev, status, config_json) "
                "VALUES (9999, 9999, '/tmp', 'abc', 'pending', '{}')"
            )
