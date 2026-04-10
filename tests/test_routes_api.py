"""Tests for JSON API routes — pipelines, tasks, escalations.

Covers all 10 endpoints in routes/api.py:
  GET  /api/pipelines
  POST /api/pipelines
  GET  /api/pipelines/{id}/status
  POST /api/pipelines/{id}/cancel
  POST /api/pipelines/{id}/kill
  GET  /api/pipelines/{id}/tasks
  GET  /api/pipelines/{id}/tasks/progress
  POST /api/pipelines/{id}/cleanup
  GET  /api/escalations
  POST /api/escalations/{id}
"""

from __future__ import annotations

import json
import uuid

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

_SIMPLE_GRAPH = json.dumps({
    "entry_stage": "spec_author",
    "nodes": [
        {"key": "spec_author", "name": "Spec", "type": "spec_author",
         "agent": "claude", "prompt": "spec_author_default",
         "model": "claude-sonnet-4-6", "max_iterations": 1}
    ],
    "edges": [],
})


async def _seed_repo(
    name: str | None = None, local_path: str = "/tmp/my-project",
) -> int:
    pool = get_pool()
    name = name or f"repo-{uuid.uuid4().hex[:8]}"
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO repos (name, local_path) VALUES (%s, %s) RETURNING id",
            (name, local_path),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_pipeline_def(name: str | None = None) -> int:
    pool = get_pool()
    name = name or f"def-{uuid.uuid4().hex[:8]}"
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipeline_defs (name, stage_graph_json) "
            "VALUES (%s, %s) RETURNING id",
            (name, _SIMPLE_GRAPH),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_pipeline(
    repo_id: int,
    def_id: int,
    *,
    status: str = "running",
    clone_path: str = "/tmp/clone",
    current_stage_key: str | None = "spec_author",
    review_base_rev: str = "abc123",
    head_rev: str | None = None,
    owner_token: str | None = "owner-1",
    lease_expires_at: str | None = "2099-01-01T00:00:00Z",
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipelines "
            "(pipeline_def_id, repo_id, clone_path, review_base_rev, head_rev, "
            " status, current_stage_key, owner_token, lease_expires_at, config_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (def_id, repo_id, clone_path, review_base_rev, head_rev,
             status, current_stage_key, owner_token, lease_expires_at, "{}"),
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
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipeline_stages "
            "(pipeline_id, stage_key, stage_type, agent_type, status, "
            " iteration, max_iterations) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (pipeline_id, stage_key, stage_type, "claude", status, 1, 3),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_session(stage_id: int, cost_usd: float = 0.5) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO agent_sessions "
            "(pipeline_stage_id, session_type, status, cost_usd) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (stage_id, "claude_sdk", "completed", cost_usd),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_htn_task(
    pipeline_id: int,
    *,
    name: str = "task-1",
    task_type: str = "primitive",
    status: str = "ready",
    parent_task_id: int | None = None,
    ordering: int = 0,
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO htn_tasks "
            "(pipeline_id, parent_task_id, name, description, task_type, status, "
            " ordering, preconditions_json, postconditions_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (pipeline_id, parent_task_id, name, f"Desc for {name}", task_type,
             status, ordering, "[]", "[]"),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_htn_dep(task_id: int, depends_on: int) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO htn_task_deps (task_id, depends_on_task_id, dep_type) "
            "VALUES (%s, %s, 'hard') RETURNING id",
            (task_id, depends_on),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_escalation(
    pipeline_id: int,
    stage_id: int | None = None,
    *,
    reason: str = "max_iterations",
    status: str = "open",
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO escalations "
            "(pipeline_id, pipeline_stage_id, reason, context_json, status) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (pipeline_id, stage_id, reason, "{}", status),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


# ===========================================================================
# GET /api/pipelines
# ===========================================================================


@pytest.mark.asyncio
async def test_list_pipelines_empty(client: AsyncClient):
    """Invariant: empty DB returns empty list, not an error."""
    resp = await client.get("/api/pipelines")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_pipelines_returns_all(client: AsyncClient):
    """Invariant: all pipelines are returned with repo and def names enriched."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    p1 = await _seed_pipeline(repo_id, def_id, status="running")
    p2 = await _seed_pipeline(repo_id, def_id, status="completed")

    resp = await client.get("/api/pipelines")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    ids = {p["id"] for p in data}
    assert p1 in ids and p2 in ids
    assert all("repo_name" in p for p in data)
    assert all("def_name" in p for p in data)


@pytest.mark.asyncio
async def test_list_pipelines_filter_by_status(client: AsyncClient):
    """Invariant: status filter returns only matching pipelines."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    await _seed_pipeline(repo_id, def_id, status="running")
    p2 = await _seed_pipeline(repo_id, def_id, status="completed")

    resp = await client.get("/api/pipelines?status=completed")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == p2


@pytest.mark.asyncio
async def test_list_pipelines_filter_by_repo(client: AsyncClient):
    """Invariant: repo_id filter returns only pipelines for that repo."""
    r1 = await _seed_repo()
    r2 = await _seed_repo()
    def_id = await _seed_pipeline_def()
    p1 = await _seed_pipeline(r1, def_id)
    await _seed_pipeline(r2, def_id)

    resp = await client.get(f"/api/pipelines?repo_id={r1}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == p1


# ===========================================================================
# POST /api/pipelines
# ===========================================================================


@pytest.mark.asyncio
async def test_create_pipeline(client: AsyncClient):
    """Invariant: creating a pipeline returns 201 with the new row."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()

    resp = await client.post("/api/pipelines", json={
        "pipeline_def_id": def_id,
        "repo_id": repo_id,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["pipeline_def_id"] == def_id
    assert data["repo_id"] == repo_id
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_create_pipeline_bad_def(client: AsyncClient):
    """Invariant: nonexistent def returns 404."""
    repo_id = await _seed_repo()

    resp = await client.post("/api/pipelines", json={
        "pipeline_def_id": 99999,
        "repo_id": repo_id,
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_pipeline_bad_repo(client: AsyncClient):
    """Invariant: nonexistent repo returns 404."""
    def_id = await _seed_pipeline_def()

    resp = await client.post("/api/pipelines", json={
        "pipeline_def_id": def_id,
        "repo_id": 99999,
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_pipeline_with_config(client: AsyncClient):
    """Invariant: custom config_json is persisted."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()

    resp = await client.post("/api/pipelines", json={
        "pipeline_def_id": def_id,
        "repo_id": repo_id,
        "config_json": {"context_threshold_pct": 50},
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["config_json"] == '{"context_threshold_pct": 50}'


# ===========================================================================
# GET /api/pipelines/{id}/status
# ===========================================================================


@pytest.mark.asyncio
async def test_pipeline_status(client: AsyncClient):
    """Invariant: status endpoint returns pipeline, stage, HTN progress, cost."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")
    sid = await _seed_stage(pid)
    await _seed_session(sid, cost_usd=1.25)
    await _seed_htn_task(pid, name="t1", status="completed")
    await _seed_htn_task(pid, name="t2", status="ready")
    await _seed_htn_task(pid, name="t3", status="not_ready")

    resp = await client.get(f"/api/pipelines/{pid}/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["pipeline"]["id"] == pid
    assert data["pipeline"]["status"] == "running"
    assert data["current_stage"] is not None
    assert data["current_stage"]["stage_key"] == "spec_author"
    assert data["htn_progress"]["total"] == 3
    assert data["htn_progress"]["completed"] == 1
    assert data["htn_progress"]["ready"] == 1
    assert data["total_cost_usd"] == 1.25


@pytest.mark.asyncio
async def test_pipeline_status_not_found(client: AsyncClient):
    """Invariant: 404 for nonexistent pipeline."""
    resp = await client.get("/api/pipelines/99999/status")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pipeline_status_no_stages(client: AsyncClient):
    """Invariant: status works even with no stages or tasks."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="pending")

    resp = await client.get(f"/api/pipelines/{pid}/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["current_stage"] is None
    assert data["htn_progress"]["total"] == 0
    assert data["total_cost_usd"] == 0.0


# ===========================================================================
# POST /api/pipelines/{id}/cancel
# ===========================================================================


@pytest.mark.asyncio
async def test_cancel_pipeline(client: AsyncClient):
    """Invariant: cancelling a running pipeline returns cancel_requested."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.post(f"/api/pipelines/{pid}/cancel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "cancel_requested"

    # Verify DB state
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status FROM pipelines WHERE id = %s", (pid,)
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["status"] == "cancel_requested"


@pytest.mark.asyncio
async def test_cancel_pipeline_not_found(client: AsyncClient):
    """Invariant: 404 for nonexistent pipeline."""
    resp = await client.post("/api/pipelines/99999/cancel")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cancel_completed_pipeline_rejected(client: AsyncClient):
    """Invariant: cannot cancel a completed pipeline (409)."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="completed")

    resp = await client.post(f"/api/pipelines/{pid}/cancel")
    assert resp.status_code == 409


# ===========================================================================
# POST /api/pipelines/{id}/kill
# ===========================================================================


@pytest.mark.asyncio
async def test_kill_pipeline(client: AsyncClient):
    """Invariant: killing a running pipeline returns killed status."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.post(f"/api/pipelines/{pid}/kill")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "killed"


@pytest.mark.asyncio
async def test_kill_pipeline_not_found(client: AsyncClient):
    """Invariant: 404 for nonexistent pipeline."""
    resp = await client.post("/api/pipelines/99999/kill")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_kill_terminal_pipeline_rejected(client: AsyncClient):
    """Invariant: cannot kill an already-terminal pipeline (409)."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="killed")

    resp = await client.post(f"/api/pipelines/{pid}/kill")
    assert resp.status_code == 409


# ===========================================================================
# GET /api/pipelines/{id}/tasks
# ===========================================================================


@pytest.mark.asyncio
async def test_pipeline_tasks_tree(client: AsyncClient):
    """Invariant: tasks are returned as a nested tree with dependencies."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)

    parent = await _seed_htn_task(
        pid, name="compound-1", task_type="compound", status="in_progress", ordering=0,
    )
    child1 = await _seed_htn_task(
        pid, name="prim-1", status="completed", parent_task_id=parent, ordering=0,
    )
    child2 = await _seed_htn_task(
        pid, name="prim-2", status="ready", parent_task_id=parent, ordering=1,
    )
    await _seed_htn_dep(child2, child1)

    resp = await client.get(f"/api/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    data = resp.json()
    # Root is the compound task
    assert len(data) == 1
    root = data[0]
    assert root["name"] == "compound-1"
    assert len(root["children"]) == 2
    # Check dependency annotation
    child2_data = next(c for c in root["children"] if c["name"] == "prim-2")
    assert len(child2_data["dependencies"]) == 1
    assert child2_data["dependencies"][0]["depends_on_task_id"] == child1


@pytest.mark.asyncio
async def test_pipeline_tasks_empty(client: AsyncClient):
    """Invariant: pipeline with no tasks returns empty list."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)

    resp = await client.get(f"/api/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_pipeline_tasks_not_found(client: AsyncClient):
    """Invariant: 404 for nonexistent pipeline."""
    resp = await client.get("/api/pipelines/99999/tasks")
    assert resp.status_code == 404


# ===========================================================================
# GET /api/pipelines/{id}/tasks/progress
# ===========================================================================


@pytest.mark.asyncio
async def test_pipeline_task_progress(client: AsyncClient):
    """Invariant: progress counts only primitive tasks, grouped by status."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)

    # Compound task should not be counted
    await _seed_htn_task(pid, name="c1", task_type="compound", status="in_progress")
    await _seed_htn_task(pid, name="t1", status="completed")
    await _seed_htn_task(pid, name="t2", status="completed")
    await _seed_htn_task(pid, name="t3", status="ready")
    await _seed_htn_task(pid, name="t4", status="not_ready")

    resp = await client.get(f"/api/pipelines/{pid}/tasks/progress")
    assert resp.status_code == 200
    data = resp.json()
    assert data["pipeline_id"] == pid
    assert data["total"] == 4  # only primitives
    assert data["completed"] == 2
    assert data["ready"] == 1
    assert data["not_ready"] == 1
    assert data["pct"] == 50


@pytest.mark.asyncio
async def test_pipeline_task_progress_empty(client: AsyncClient):
    """Invariant: zero tasks returns 0 total and 0 pct."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)

    resp = await client.get(f"/api/pipelines/{pid}/tasks/progress")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["pct"] == 0


@pytest.mark.asyncio
async def test_pipeline_task_progress_not_found(client: AsyncClient):
    """Invariant: 404 for nonexistent pipeline."""
    resp = await client.get("/api/pipelines/99999/tasks/progress")
    assert resp.status_code == 404


# ===========================================================================
# POST /api/pipelines/{id}/cleanup
# ===========================================================================


@pytest.mark.asyncio
async def test_cleanup_pipeline(client: AsyncClient, tmp_path):
    """Invariant: cleanup deletes clone dir and reports cleaned=True."""
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "file.txt").write_text("data")

    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(
        repo_id, def_id, status="completed", clone_path=str(clone),
    )

    resp = await client.post(f"/api/pipelines/{pid}/cleanup")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cleaned"] is True
    assert not clone.exists()


@pytest.mark.asyncio
async def test_cleanup_running_pipeline_rejected(client: AsyncClient):
    """Invariant: cannot cleanup a running pipeline (409)."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.post(f"/api/pipelines/{pid}/cleanup")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_cleanup_nonexistent_clone(client: AsyncClient):
    """Invariant: cleanup of already-gone clone reports cleaned=False."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(
        repo_id, def_id, status="completed", clone_path="/tmp/nonexistent-clone-xyz",
    )

    resp = await client.post(f"/api/pipelines/{pid}/cleanup")
    assert resp.status_code == 200
    assert resp.json()["cleaned"] is False


@pytest.mark.asyncio
async def test_cleanup_not_found(client: AsyncClient):
    """Invariant: 404 for nonexistent pipeline."""
    resp = await client.post("/api/pipelines/99999/cleanup")
    assert resp.status_code == 404


# ===========================================================================
# GET /api/escalations
# ===========================================================================


@pytest.mark.asyncio
async def test_list_escalations_empty(client: AsyncClient):
    """Invariant: empty escalation queue returns empty list."""
    resp = await client.get("/api/escalations")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_escalations(client: AsyncClient):
    """Invariant: escalations are returned with enriched pipeline info."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="paused")
    sid = await _seed_stage(pid, status="completed")
    eid = await _seed_escalation(pid, sid)

    resp = await client.get("/api/escalations")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == eid
    assert data[0]["reason"] == "max_iterations"
    assert "repo_name" in data[0]
    assert "def_name" in data[0]


@pytest.mark.asyncio
async def test_list_escalations_filter_by_status(client: AsyncClient):
    """Invariant: status filter returns only matching escalations."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="paused")
    sid = await _seed_stage(pid, status="completed")
    await _seed_escalation(pid, sid, status="open")
    await _seed_escalation(pid, sid, status="resolved")

    resp = await client.get("/api/escalations?status=open")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["status"] == "open"


# ===========================================================================
# POST /api/escalations/{id}
# ===========================================================================


@pytest.mark.asyncio
async def test_resolve_escalation(client: AsyncClient):
    """Invariant: resolving an open escalation sets status=resolved."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="paused")
    eid = await _seed_escalation(pid)

    resp = await client.post(f"/api/escalations/{eid}", json={
        "action": "resolve",
        "resolution": "Approved with modifications",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "resolved"
    assert data["resolution"] == "Approved with modifications"


@pytest.mark.asyncio
async def test_dismiss_escalation(client: AsyncClient):
    """Invariant: dismissing an open escalation sets status=dismissed."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="paused")
    eid = await _seed_escalation(pid)

    resp = await client.post(f"/api/escalations/{eid}", json={
        "action": "dismiss",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"


@pytest.mark.asyncio
async def test_resolve_escalation_requires_resolution(client: AsyncClient):
    """Invariant: resolve action without resolution text returns 400."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="paused")
    eid = await _seed_escalation(pid)

    resp = await client.post(f"/api/escalations/{eid}", json={
        "action": "resolve",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_resolve_escalation_bad_action(client: AsyncClient):
    """Invariant: invalid action returns 400."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="paused")
    eid = await _seed_escalation(pid)

    resp = await client.post(f"/api/escalations/{eid}", json={
        "action": "invalid",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_resolve_already_resolved_escalation(client: AsyncClient):
    """Invariant: cannot resolve an already-resolved escalation (409)."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="paused")
    eid = await _seed_escalation(pid, status="resolved")

    resp = await client.post(f"/api/escalations/{eid}", json={
        "action": "resolve",
        "resolution": "double resolve",
    })
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_resolve_escalation_not_found(client: AsyncClient):
    """Invariant: 404 for nonexistent escalation."""
    resp = await client.post("/api/escalations/99999", json={
        "action": "resolve",
        "resolution": "test",
    })
    assert resp.status_code == 404
