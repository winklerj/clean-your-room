"""Tests for the dashboard route — pipeline cards grid + HTN progress indicators."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from build_your_room.db import get_pool
from build_your_room.main import app


@pytest.fixture
async def client(initialized_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

async def _seed_repo(
    name: str = "my-project", local_path: str = "/tmp/my-project"
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO repos (name, local_path) VALUES (%s, %s) RETURNING id",
            (name, local_path),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_pipeline_def(
    name: str = "test-def",
) -> int:
    pool = get_pool()
    graph = json.dumps({
        "entry_stage": "spec_author",
        "nodes": [
            {"key": "spec_author", "name": "Spec", "type": "spec_author",
             "agent": "claude", "prompt": "spec_author_default",
             "model": "claude-sonnet-4-6", "max_iterations": 1}
        ],
        "edges": [],
    })
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipeline_defs (name, stage_graph_json) "
            "VALUES (%s, %s) RETURNING id",
            (name, graph),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_pipeline(
    repo_id: int,
    def_id: int,
    status: str = "running",
    clone_path: str = "/tmp/clone",
    current_stage_key: str | None = "spec_author",
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipelines "
            "(pipeline_def_id, repo_id, clone_path, review_base_rev, status, "
            " current_stage_key, config_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (def_id, repo_id, clone_path, "abc123", status,
             current_stage_key, "{}"),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_stage(
    pipeline_id: int,
    stage_key: str = "spec_author",
    stage_type: str = "spec_author",
    status: str = "running",
    iteration: int = 1,
    max_iterations: int = 3,
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipeline_stages "
            "(pipeline_id, stage_key, stage_type, agent_type, status, "
            " iteration, max_iterations) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (pipeline_id, stage_key, stage_type, "claude", status,
             iteration, max_iterations),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_session(
    stage_id: int,
    status: str = "running",
    context_usage_pct: float | None = None,
    cost_usd: float = 0.0,
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO agent_sessions "
            "(pipeline_stage_id, session_type, status, context_usage_pct, cost_usd) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (stage_id, "claude_sdk", status, context_usage_pct, cost_usd),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_htn_tasks(
    pipeline_id: int,
    statuses: list[str],
) -> list[int]:
    """Create primitive HTN tasks with the given statuses."""
    pool = get_pool()
    ids: list[int] = []
    async with pool.connection() as conn:
        for i, s in enumerate(statuses):
            cur = await conn.execute(
                "INSERT INTO htn_tasks "
                "(pipeline_id, name, description, task_type, status, ordering) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (pipeline_id, f"task-{i}", f"desc-{i}", "primitive", s, i),
            )
            row = await cur.fetchone()
            assert row is not None
            ids.append(row["id"])
        await conn.commit()
    return ids


async def _seed_escalation(
    pipeline_id: int,
    status: str = "open",
    stage_id: int | None = None,
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO escalations "
            "(pipeline_id, pipeline_stage_id, reason, context_json, status) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (pipeline_id, stage_id, "test_reason", "{}", status),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_empty(client):
    """Dashboard renders with no pipelines or repos."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Build Your Room" in resp.text
    assert "No pipelines yet" in resp.text
    assert "No repos yet" in resp.text


@pytest.mark.asyncio
async def test_dashboard_shows_pipeline_card(client):
    """Dashboard shows pipeline cards when pipelines exist."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "my-project" in resp.text
    assert "running" in resp.text
    assert "pipeline-card" in resp.text


@pytest.mark.asyncio
async def test_dashboard_status_counts(client):
    """Dashboard stat cards show correct status counts."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    await _seed_pipeline(repo_id, def_id, status="running", clone_path="/tmp/c1")
    await _seed_pipeline(repo_id, def_id, status="running", clone_path="/tmp/c2")
    await _seed_pipeline(repo_id, def_id, status="completed", clone_path="/tmp/c3")
    await _seed_pipeline(repo_id, def_id, status="failed", clone_path="/tmp/c4")

    resp = await client.get("/")
    assert resp.status_code == 200
    text = resp.text
    # The stat card for running should show 2
    assert ">4<" in text  # total pipelines
    assert ">2<" in text  # running count


@pytest.mark.asyncio
async def test_dashboard_htn_progress(client):
    """Dashboard shows HTN task progress bars."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")
    await _seed_htn_tasks(pid, [
        "completed", "completed", "completed",
        "in_progress",
        "ready", "ready",
        "not_ready", "not_ready", "not_ready", "not_ready",
    ])

    resp = await client.get("/")
    assert resp.status_code == 200
    text = resp.text
    # 3 of 10 completed = 30%
    assert "3/10" in text
    assert "30%" in text
    assert "1 active" in text


@pytest.mark.asyncio
async def test_dashboard_htn_failed_count(client):
    """Dashboard shows failed task count when tasks have failed."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")
    await _seed_htn_tasks(pid, ["completed", "failed", "failed"])

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "2 failed" in resp.text


@pytest.mark.asyncio
async def test_dashboard_escalation_banner(client):
    """Dashboard shows escalation banner when open escalations exist."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="needs_attention")
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, status="open", stage_id=stage_id)
    await _seed_escalation(pid, status="open", stage_id=stage_id)

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "2 pipelines need" in resp.text
    assert "escalation-banner" in resp.text


@pytest.mark.asyncio
async def test_dashboard_no_escalation_banner(client):
    """Dashboard hides escalation banner when no open escalations."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "escalation-banner" not in resp.text


@pytest.mark.asyncio
async def test_dashboard_resolved_escalation_not_counted(client):
    """Resolved escalations don't appear in the banner count."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, status="resolved", stage_id=stage_id)

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "escalation-banner" not in resp.text


@pytest.mark.asyncio
async def test_dashboard_stage_progress(client):
    """Dashboard shows current stage info with iteration progress."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")
    await _seed_stage(pid, stage_key="impl_task", stage_type="impl_task",
                      iteration=7, max_iterations=50)

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "impl_task" in resp.text
    assert "7/50" in resp.text


@pytest.mark.asyncio
async def test_dashboard_context_usage(client):
    """Dashboard shows context usage bar for running pipelines."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")
    stage_id = await _seed_stage(pid)
    await _seed_session(stage_id, status="running", context_usage_pct=45.0)

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "45%" in resp.text
    assert "pipeline-card-context" in resp.text


@pytest.mark.asyncio
async def test_dashboard_cost_display(client):
    """Dashboard shows accumulated cost per pipeline."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")
    stage_id = await _seed_stage(pid)
    await _seed_session(stage_id, status="completed", cost_usd=1.23)
    await _seed_session(stage_id, status="completed", cost_usd=0.50)

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "$1.73" in resp.text


@pytest.mark.asyncio
async def test_dashboard_cleanup_button_terminal(client):
    """Cleanup button appears for terminal status pipelines."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    await _seed_pipeline(repo_id, def_id, status="completed")

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "btn-cleanup" in resp.text
    assert "Clean up clone" in resp.text


@pytest.mark.asyncio
async def test_dashboard_no_cleanup_button_running(client):
    """Cleanup button does not appear for running pipelines."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "btn-cleanup" not in resp.text


@pytest.mark.asyncio
async def test_dashboard_pipeline_def_name(client):
    """Dashboard shows pipeline definition name on the card."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def(name="full-coding-pipeline")
    await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "full-coding-pipeline" in resp.text


@pytest.mark.asyncio
async def test_dashboard_multiple_pipelines(client):
    """Dashboard renders multiple pipeline cards."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    await _seed_pipeline(repo_id, def_id, status="running", clone_path="/tmp/c1")
    await _seed_pipeline(repo_id, def_id, status="failed", clone_path="/tmp/c2")

    resp = await client.get("/")
    assert resp.status_code == 200
    assert resp.text.count("pipeline-card") >= 2


@pytest.mark.asyncio
async def test_dashboard_repos_still_shown(client):
    """Repos table is still visible on the new dashboard."""
    await _seed_repo(name="visible-repo", local_path="/tmp/visible")

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "visible-repo" in resp.text
    assert "Add Repo" in resp.text


@pytest.mark.asyncio
async def test_dashboard_nav_links(client):
    """Nav bar includes Escalations link."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert 'href="/escalations"' in resp.text


@pytest.mark.asyncio
async def test_dashboard_hides_archived_repos(client):
    """Archived repos should not appear on the dashboard."""
    repo_id = await _seed_repo(name="archived-repo", local_path="/tmp/archived")
    pool = get_pool()
    async with pool.connection() as conn:
        await conn.execute("UPDATE repos SET archived=1 WHERE id=%s", (repo_id,))
        await conn.commit()
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "archived-repo" not in resp.text


@pytest.mark.asyncio
async def test_dashboard_no_htn_section_without_tasks(client):
    """Pipeline cards without HTN tasks don't render progress bars."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "pipeline-card-htn" not in resp.text


@pytest.mark.asyncio
async def test_dashboard_no_context_for_non_running(client):
    """Context usage bar not shown for completed pipelines."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    await _seed_pipeline(repo_id, def_id, status="completed")

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "pipeline-card-context" not in resp.text
