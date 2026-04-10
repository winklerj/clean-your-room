"""Tests for the HTN task tree page — GET /pipelines/{id}/tasks with filtering."""

from __future__ import annotations

import json
from typing import Any

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

_GRAPH = json.dumps({
    "entry_stage": "impl_task",
    "nodes": [
        {"key": "impl_task", "name": "Implementation", "type": "impl_task",
         "agent": "claude", "prompt": "impl_task_default",
         "model": "claude-sonnet-4-6", "max_iterations": 50},
    ],
    "edges": [],
})


async def _seed_repo(suffix: str = "") -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO repos (name, local_path) VALUES (%s, %s) RETURNING id",
            (f"task-repo{suffix}", f"/tmp/task-repo{suffix}"),
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_def(suffix: str = "") -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipeline_defs (name, stage_graph_json) VALUES (%s, %s) RETURNING id",
            (f"task-def{suffix}", _GRAPH),
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_pipeline(repo_id: int, def_id: int, status: str = "running") -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipelines "
            "(pipeline_def_id, repo_id, clone_path, review_base_rev, status, "
            " current_stage_key, config_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (def_id, repo_id, "/tmp/clone", "abc123", status, "impl_task", "{}"),
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_task(
    pipeline_id: int,
    name: str,
    task_type: str = "primitive",
    status: str = "ready",
    ordering: int = 0,
    parent_task_id: int | None = None,
    diary_entry: str | None = None,
    estimated_complexity: str | None = None,
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO htn_tasks "
            "(pipeline_id, parent_task_id, name, description, task_type, "
            " status, ordering, diary_entry, estimated_complexity) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (pipeline_id, parent_task_id, name, f"Description of {name}",
             task_type, status, ordering, diary_entry, estimated_complexity),
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        assert row is not None
        await conn.commit()
        return row["id"]


# ---------------------------------------------------------------------------
# Tests — 404 and empty state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_page_404_missing_pipeline(client):
    """GET /pipelines/{id}/tasks returns 404 for non-existent pipeline.

    Invariant: non-existent pipeline IDs yield a clear 404.
    Context: the tasks page must validate the pipeline exists before rendering.
    """
    resp = await client.get("/pipelines/99999/tasks")
    assert resp.status_code == 404
    assert "not found" in resp.text.lower()


@pytest.mark.asyncio
async def test_tasks_page_empty(client):
    """GET /pipelines/{id}/tasks renders empty state when no tasks exist.

    Invariant: pipeline with no HTN tasks shows a clear empty-state message.
    Context: a pipeline may not yet have an HTN task tree (e.g. before impl plan).
    """
    repo_id = await _seed_repo("-empty")
    def_id = await _seed_def("-empty")
    pid = await _seed_pipeline(repo_id, def_id)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "No HTN tasks" in resp.text


# ---------------------------------------------------------------------------
# Tests — rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_page_renders_tree(client):
    """GET /pipelines/{id}/tasks renders compound and primitive tasks in tree.

    Invariant: the page displays all tasks with their names and status badges.
    Context: verifies the task tree structure is rendered with hierarchy.
    """
    repo_id = await _seed_repo("-tree")
    def_id = await _seed_def("-tree")
    pid = await _seed_pipeline(repo_id, def_id)

    compound_id = await _seed_task(pid, "Setup phase", task_type="compound",
                                   status="in_progress", ordering=0)
    await _seed_task(pid, "Create database", status="completed", ordering=0,
                     parent_task_id=compound_id)
    await _seed_task(pid, "Add migrations", status="ready", ordering=1,
                     parent_task_id=compound_id)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "Setup phase" in resp.text
    assert "Create database" in resp.text
    assert "Add migrations" in resp.text
    assert "compound" in resp.text


@pytest.mark.asyncio
async def test_tasks_page_progress_bar(client):
    """GET /pipelines/{id}/tasks shows progress summary for primitive tasks.

    Invariant: progress bar reflects completed/total primitive task counts.
    Context: the summary bar must track only primitive tasks, not compound.
    """
    repo_id = await _seed_repo("-prog")
    def_id = await _seed_def("-prog")
    pid = await _seed_pipeline(repo_id, def_id)

    await _seed_task(pid, "task-a", status="completed", ordering=0)
    await _seed_task(pid, "task-b", status="completed", ordering=1)
    await _seed_task(pid, "task-c", status="ready", ordering=2)
    await _seed_task(pid, "task-d", status="not_ready", ordering=3)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "2/4" in resp.text
    assert "50%" in resp.text


@pytest.mark.asyncio
async def test_tasks_page_diary_entry(client):
    """GET /pipelines/{id}/tasks renders diary entries for completed tasks.

    Invariant: diary entries are visible in the task detail section.
    Context: diary entries are critical for cross-session knowledge sharing.
    """
    repo_id = await _seed_repo("-diary")
    def_id = await _seed_def("-diary")
    pid = await _seed_pipeline(repo_id, def_id)

    await _seed_task(pid, "impl-auth", status="completed",
                     diary_entry="Used JWT tokens for session management")

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "JWT tokens" in resp.text


@pytest.mark.asyncio
async def test_tasks_page_decision_resolve_form(client):
    """GET /pipelines/{id}/tasks shows resolve form for unresolved decision tasks.

    Invariant: decision-type tasks that are not completed have a resolve form.
    Context: decision tasks need human intervention via the task tree page.
    """
    repo_id = await _seed_repo("-dec")
    def_id = await _seed_def("-dec")
    pid = await _seed_pipeline(repo_id, def_id)

    await _seed_task(pid, "Choose auth strategy", task_type="decision",
                     status="blocked", ordering=0)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "Choose auth strategy" in resp.text
    assert "decision" in resp.text
    assert 'name="resolution"' in resp.text


# ---------------------------------------------------------------------------
# Tests — filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_page_filter_by_status(client):
    """GET /pipelines/{id}/tasks?status_filter=completed shows only matching tasks.

    Invariant: status filter limits visible tasks to the selected status
    (plus ancestors for tree structure).
    Context: filtering helps users focus on specific task states.
    """
    repo_id = await _seed_repo("-filt-s")
    def_id = await _seed_def("-filt-s")
    pid = await _seed_pipeline(repo_id, def_id)

    await _seed_task(pid, "done-task", status="completed", ordering=0)
    await _seed_task(pid, "pending-task", status="ready", ordering=1)

    resp = await client.get(f"/pipelines/{pid}/tasks?status_filter=completed")
    assert resp.status_code == 200
    assert "done-task" in resp.text
    assert "pending-task" not in resp.text


@pytest.mark.asyncio
async def test_tasks_page_filter_by_type(client):
    """GET /pipelines/{id}/tasks?type_filter=compound shows only compound tasks.

    Invariant: type filter limits visible tasks to the selected task type.
    Context: helps users focus on compound vs. primitive vs. decision tasks.
    """
    repo_id = await _seed_repo("-filt-t")
    def_id = await _seed_def("-filt-t")
    pid = await _seed_pipeline(repo_id, def_id)

    await _seed_task(pid, "parent-task", task_type="compound", status="in_progress", ordering=0)
    await _seed_task(pid, "leaf-task", task_type="primitive", status="ready", ordering=1)

    resp = await client.get(f"/pipelines/{pid}/tasks?type_filter=compound")
    assert resp.status_code == 200
    assert "parent-task" in resp.text
    assert "leaf-task" not in resp.text


@pytest.mark.asyncio
async def test_tasks_page_filter_preserves_ancestors(client):
    """Filtering by child status preserves parent compound task in tree.

    Invariant: when filtering reveals a child, its parent is kept
    so the tree structure is maintained.
    Context: without ancestors, filtered children would become orphaned roots.
    """
    repo_id = await _seed_repo("-anc")
    def_id = await _seed_def("-anc")
    pid = await _seed_pipeline(repo_id, def_id)

    parent_id = await _seed_task(pid, "parent-compound", task_type="compound",
                                 status="in_progress", ordering=0)
    await _seed_task(pid, "child-completed", task_type="primitive",
                     status="completed", ordering=0, parent_task_id=parent_id)
    await _seed_task(pid, "child-ready", task_type="primitive",
                     status="ready", ordering=1, parent_task_id=parent_id)

    resp = await client.get(f"/pipelines/{pid}/tasks?status_filter=completed")
    assert resp.status_code == 200
    # Parent is preserved as ancestor
    assert "parent-compound" in resp.text
    assert "child-completed" in resp.text
    # Sibling with different status is filtered out
    assert "child-ready" not in resp.text


@pytest.mark.asyncio
async def test_tasks_page_clear_filters_link(client):
    """Filtered tasks page shows a 'Clear filters' link.

    Invariant: when any filter is active, the page shows a clear-filters link
    pointing to the unfiltered tasks page.
    Context: users need a way to reset filters.
    """
    repo_id = await _seed_repo("-clear")
    def_id = await _seed_def("-clear")
    pid = await _seed_pipeline(repo_id, def_id)
    await _seed_task(pid, "a-task", status="completed", ordering=0)

    resp = await client.get(f"/pipelines/{pid}/tasks?status_filter=completed")
    assert resp.status_code == 200
    assert "Clear filters" in resp.text
    assert f"/pipelines/{pid}/tasks" in resp.text


@pytest.mark.asyncio
async def test_tasks_page_no_filter_no_clear_link(client):
    """Unfiltered tasks page does not show 'Clear filters' link.

    Invariant: the clear-filters link only appears when filters are active.
    Context: prevents UI clutter when no filters are applied.
    """
    repo_id = await _seed_repo("-noclear")
    def_id = await _seed_def("-noclear")
    pid = await _seed_pipeline(repo_id, def_id)
    await _seed_task(pid, "some-task", status="ready", ordering=0)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "Clear filters" not in resp.text


@pytest.mark.asyncio
async def test_tasks_page_empty_filter_result(client):
    """Filtering with no matching tasks shows appropriate message.

    Invariant: an empty filter result shows a no-match message, not the
    generic 'no tasks' message.
    Context: distinguishes between 'pipeline has no tasks' and 'filter matched nothing'.
    """
    repo_id = await _seed_repo("-nores")
    def_id = await _seed_def("-nores")
    pid = await _seed_pipeline(repo_id, def_id)
    await _seed_task(pid, "ready-task", status="ready", ordering=0)

    resp = await client.get(f"/pipelines/{pid}/tasks?status_filter=failed")
    assert resp.status_code == 200
    assert "No tasks match" in resp.text


# ---------------------------------------------------------------------------
# Tests — status/type chip counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_page_status_chips(client):
    """Tasks page shows status count chips for filtering.

    Invariant: each distinct task status appears as a clickable chip with count.
    Context: chips provide quick visual feedback and filtering shortcuts.
    """
    repo_id = await _seed_repo("-chips")
    def_id = await _seed_def("-chips")
    pid = await _seed_pipeline(repo_id, def_id)

    await _seed_task(pid, "t1", status="completed", ordering=0)
    await _seed_task(pid, "t2", status="completed", ordering=1)
    await _seed_task(pid, "t3", status="ready", ordering=2)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    # Status chips should show counts
    assert "completed" in resp.text.lower()
    assert "ready" in resp.text.lower()


@pytest.mark.asyncio
async def test_tasks_page_back_link(client):
    """Tasks page has a back link to the pipeline detail page.

    Invariant: the back link points to /pipelines/{id}.
    Context: navigation from standalone task tree back to full pipeline detail.
    """
    repo_id = await _seed_repo("-back")
    def_id = await _seed_def("-back")
    pid = await _seed_pipeline(repo_id, def_id)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert f"/pipelines/{pid}" in resp.text
    assert "Back to pipeline detail" in resp.text


@pytest.mark.asyncio
async def test_tasks_page_pipeline_header(client):
    """Tasks page shows pipeline name, repo name, and status in header.

    Invariant: the page header identifies the pipeline with def name, repo, and status.
    Context: users need context about which pipeline's tasks they are viewing.
    """
    repo_id = await _seed_repo("-hdr")
    def_id = await _seed_def("-hdr")
    pid = await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "task-def-hdr" in resp.text
    assert "task-repo-hdr" in resp.text
    assert "running" in resp.text
