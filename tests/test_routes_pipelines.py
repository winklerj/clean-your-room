"""Tests for the pipeline detail page — stage graph, HTN tree, sessions, logs, clone mgmt."""

from __future__ import annotations

import json
import uuid

import pytest
from hypothesis import HealthCheck, given, settings as hyp_settings
from hypothesis import strategies as st
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

_FULL_GRAPH = json.dumps({
    "entry_stage": "spec_author",
    "nodes": [
        {"key": "spec_author", "name": "Spec authoring", "type": "spec_author",
         "agent": "claude", "prompt": "spec_author_default",
         "model": "claude-sonnet-4-6", "max_iterations": 1},
        {"key": "impl_plan", "name": "Implementation plan", "type": "impl_plan",
         "agent": "claude", "prompt": "impl_plan_default",
         "model": "claude-sonnet-4-6", "max_iterations": 1},
        {"key": "impl_task", "name": "Implementation", "type": "impl_task",
         "agent": "claude", "prompt": "impl_task_default",
         "model": "claude-sonnet-4-6", "max_iterations": 50},
    ],
    "edges": [
        {"key": "spec_to_plan", "from": "spec_author", "to": "impl_plan",
         "on": "approved"},
        {"key": "plan_to_impl", "from": "impl_plan", "to": "impl_task",
         "on": "approved"},
    ],
})


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
    name: str = "test-def", graph: str | None = None,
) -> int:
    pool = get_pool()
    g = graph or _FULL_GRAPH
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipeline_defs (name, stage_graph_json) "
            "VALUES (%s, %s) RETURNING id",
            (name, g),
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
    review_base_rev: str = "abc123def456",
    head_rev: str | None = None,
    workspace_state: str = "clean",
    dirty_snapshot_artifact: str | None = None,
    owner_token: str | None = "owner-1",
    lease_expires_at: str | None = "2099-01-01T00:00:00Z",
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipelines "
            "(pipeline_def_id, repo_id, clone_path, review_base_rev, head_rev, "
            " workspace_state, dirty_snapshot_artifact, status, "
            " current_stage_key, owner_token, lease_expires_at, config_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (def_id, repo_id, clone_path, review_base_rev, head_rev,
             workspace_state, dirty_snapshot_artifact, status,
             current_stage_key, owner_token, lease_expires_at, "{}"),
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
    attempt: int = 1,
    iteration: int = 1,
    max_iterations: int = 3,
    output_artifact: str | None = None,
    escalation_reason: str | None = None,
    entry_rev: str | None = None,
    exit_rev: str | None = None,
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipeline_stages "
            "(pipeline_id, stage_key, attempt, stage_type, agent_type, status, "
            " iteration, max_iterations, output_artifact, escalation_reason, "
            " entry_rev, exit_rev) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (pipeline_id, stage_key, attempt, stage_type, "claude", status,
             iteration, max_iterations, output_artifact, escalation_reason,
             entry_rev, exit_rev),
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
    token_input: int = 0,
    token_output: int = 0,
    session_type: str = "claude_sdk",
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO agent_sessions "
            "(pipeline_stage_id, session_type, status, context_usage_pct, "
            " cost_usd, token_input, token_output) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (stage_id, session_type, status, context_usage_pct,
             cost_usd, token_input, token_output),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_log(
    session_id: int,
    event_type: str = "assistant_message",
    content: str = "test log entry",
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO session_logs "
            "(agent_session_id, event_type, content) "
            "VALUES (%s, %s, %s) RETURNING id",
            (session_id, event_type, content),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_htn_task(
    pipeline_id: int,
    name: str = "task-1",
    description: str = "A test task",
    task_type: str = "primitive",
    status: str = "ready",
    ordering: int = 0,
    parent_task_id: int | None = None,
    estimated_complexity: str | None = None,
    diary_entry: str | None = None,
    assigned_session_id: int | None = None,
    checkpoint_rev: str | None = None,
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO htn_tasks "
            "(pipeline_id, parent_task_id, name, description, task_type, "
            " status, ordering, estimated_complexity, diary_entry, "
            " assigned_session_id, checkpoint_rev) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (pipeline_id, parent_task_id, name, description, task_type,
             status, ordering, estimated_complexity, diary_entry,
             assigned_session_id, checkpoint_rev),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_escalation(
    pipeline_id: int,
    stage_id: int | None = None,
    reason: str = "max_iterations",
    status: str = "open",
    context_json: str = "{}",
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO escalations "
            "(pipeline_id, pipeline_stage_id, reason, context_json, status) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (pipeline_id, stage_id, reason, context_json, status),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


# ---------------------------------------------------------------------------
# Tests — page rendering basics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_detail_404_missing(client):
    """GET /pipelines/{id} returns 404 when pipeline does not exist.

    Invariant: non-existent pipeline IDs yield a clear 404 response.
    """
    resp = await client.get("/pipelines/99999")
    assert resp.status_code == 404
    assert "not found" in resp.text.lower()


@pytest.mark.asyncio
async def test_pipeline_detail_renders(client):
    """GET /pipelines/{id} returns 200 with pipeline info.

    Invariant: the detail page renders the pipeline def name, repo name,
    status badge, and clone path.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def(name="full-coding-pipeline")
    pid = await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "full-coding-pipeline" in resp.text
    assert "my-project" in resp.text
    assert "running" in resp.text
    assert "/tmp/clone" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_cost(client):
    """Pipeline detail shows accumulated cost across all sessions.

    Invariant: total_cost sums cost_usd from all agent_sessions in the pipeline.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid)
    await _seed_session(sid, cost_usd=1.50, status="completed")
    await _seed_session(sid, cost_usd=0.75, status="completed")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "$2.25" in resp.text


# ---------------------------------------------------------------------------
# Tests — stage graph visualization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_detail_stage_graph_nodes(client):
    """Stage graph visualization shows all nodes from the pipeline def.

    Invariant: each node in stage_graph_json is rendered with its name and type.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, current_stage_key="impl_plan")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "Spec authoring" in resp.text
    assert "Implementation plan" in resp.text
    assert "Implementation" in resp.text
    assert "stage-graph" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_active_node_highlighted(client):
    """The current stage node has the active CSS class.

    Invariant: the node matching current_stage_key gets stage-node-active.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, current_stage_key="impl_task")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert 'id="node-impl_task"' in resp.text
    # The active node's div should contain the active class
    text = resp.text
    node_start = text.index('id="node-impl_task"')
    # Look backwards for the class attribute
    div_start = text.rfind("<div", 0, node_start)
    div_snippet = text[div_start:node_start + 50]
    assert "stage-node-active" in div_snippet


@pytest.mark.asyncio
async def test_pipeline_detail_stage_graph_edges(client):
    """Stage graph edges are rendered with from/to and guard conditions.

    Invariant: each edge shows its source, target, and on-condition.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "spec_author" in resp.text
    assert "impl_plan" in resp.text
    assert "approved" in resp.text
    assert "stage-edge" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_stage_visit_counts(client):
    """Nodes show visit counts when a stage has been executed.

    Invariant: if a stage node has been visited N times, "N visits" is shown.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, current_stage_key="spec_author")
    await _seed_stage(pid, stage_key="spec_author", attempt=1, status="completed")
    await _seed_stage(pid, stage_key="spec_author", attempt=2, status="running")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "2 visits" in resp.text


# ---------------------------------------------------------------------------
# Tests — stage execution history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_detail_stages_listed(client):
    """All stage executions are shown with their status.

    Invariant: each pipeline_stages row is rendered in the stages section.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    await _seed_stage(pid, stage_key="spec_author", status="completed",
                      output_artifact="/tmp/spec.md")
    await _seed_stage(pid, stage_key="impl_plan", status="running")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "stage-tab" in resp.text
    assert "spec_author" in resp.text
    assert "impl_plan" in resp.text
    assert "completed" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_stage_has_artifact_hint(client):
    """Stage tab header shows 'has artifact' hint when output_artifact is set.

    Invariant: if output_artifact is non-null, the compact tab summary includes
    the hint. Full artifact content is loaded via HTMX stage detail.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    await _seed_stage(pid, output_artifact="/tmp/pipelines/1/artifacts/spec.md")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "has artifact" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_stage_escalation_in_detail(client):
    """Stage escalation reason is displayed in the HTMX stage detail partial.

    Invariant: if escalation_reason is set, it appears in the stage detail.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid, escalation_reason="max_iterations")

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200
    assert "max iterations" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_stage_iteration_in_detail(client):
    """Multi-iteration stages show iteration progress in stage detail partial.

    Invariant: if max_iterations > 1, "Iteration X/Y" is displayed in detail.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid, stage_key="impl_task", stage_type="impl_task",
                            iteration=7, max_iterations=50)

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200
    assert "7/50" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_no_stages(client):
    """Pipeline with no stages shows empty state message.

    Invariant: when no pipeline_stages rows exist, an empty message is shown.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "No stages executed" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_stage_revisions_in_detail(client):
    """Stage entry and exit revisions are displayed in the stage detail partial.

    Invariant: entry_rev and exit_rev are shown truncated to 12 chars.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid, entry_rev="aabbccddee11223344",
                            exit_rev="ff00112233445566")

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200
    assert "aabbccddee11" in resp.text
    assert "ff0011223344" in resp.text


# ---------------------------------------------------------------------------
# Tests — sessions within stages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_detail_sessions_count_in_tab(client):
    """Stage tab header shows session count summary.

    Invariant: stage tab compact header includes session count.
    Full session details are loaded via HTMX stage detail partial.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    await _seed_session(stage_id, status="completed", cost_usd=0.5,
                        context_usage_pct=45.0, token_input=1000,
                        token_output=500)

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "1 session" in resp.text
    assert "hx-get" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_multiple_sessions_count(client):
    """Stage tab shows plural session count for multiple sessions.

    Invariant: session count in tab header is correct.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    await _seed_session(stage_id, status="completed", cost_usd=0.3)
    await _seed_session(stage_id, status="completed", cost_usd=0.2)
    await _seed_session(stage_id, status="running", cost_usd=0.1)

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "3 sessions" in resp.text


# ---------------------------------------------------------------------------
# Tests — HTN task tree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_detail_htn_tree(client):
    """HTN task tree renders with task names and status badges.

    Invariant: each task appears with its name and status in the tree.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    await _seed_htn_task(pid, name="Setup DB", status="completed", ordering=0)
    await _seed_htn_task(pid, name="Add routes", status="in_progress", ordering=1)
    await _seed_htn_task(pid, name="Write tests", status="ready", ordering=2)

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "Setup DB" in resp.text
    assert "Add routes" in resp.text
    assert "Write tests" in resp.text
    assert "htn-tree" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_htn_progress_bar(client):
    """HTN progress bar shows completion percentage.

    Invariant: completed/total primitive tasks and percentage are displayed.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    for i in range(4):
        await _seed_htn_task(pid, name=f"done-{i}", status="completed", ordering=i)
    for i in range(6):
        await _seed_htn_task(pid, name=f"todo-{i}", status="ready", ordering=i + 4)

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "4/10" in resp.text
    assert "40%" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_htn_nested_tree(client):
    """Compound tasks expand to show child subtasks.

    Invariant: compound tasks contain their children in the tree structure.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    parent_id = await _seed_htn_task(
        pid, name="Phase 1", task_type="compound", status="in_progress", ordering=0
    )
    await _seed_htn_task(
        pid, name="Sub-task A", parent_task_id=parent_id,
        status="completed", ordering=0
    )
    await _seed_htn_task(
        pid, name="Sub-task B", parent_task_id=parent_id,
        status="ready", ordering=1
    )

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "Phase 1" in resp.text
    assert "Sub-task A" in resp.text
    assert "Sub-task B" in resp.text
    assert "htn-task-children" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_htn_status_classes(client):
    """Tasks get status-specific CSS classes.

    Invariant: each task node has a class matching its status.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    await _seed_htn_task(pid, name="t-done", status="completed", ordering=0)
    await _seed_htn_task(pid, name="t-fail", status="failed", ordering=1)

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "task-completed" in resp.text
    assert "task-failed" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_htn_diary_entry(client):
    """Task diary entries are displayed in expandable details.

    Invariant: when diary_entry is set, it appears in a details/summary element.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    await _seed_htn_task(
        pid, name="Impl auth", diary_entry="Learned about JWT refresh",
        status="completed", ordering=0
    )

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "Diary entry" in resp.text
    assert "JWT refresh" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_htn_complexity(client):
    """Task estimated complexity is shown when set.

    Invariant: the complexity badge appears for tasks with estimated_complexity.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    await _seed_htn_task(
        pid, name="Big task", estimated_complexity="large",
        status="ready", ordering=0
    )

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "task-complexity" in resp.text
    assert "large" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_htn_decision_task(client):
    """Decision-type tasks show an inline resolve form.

    Invariant: tasks with task_type='decision' and status != 'completed'
    get an inline resolution form pointing to the resolve endpoint.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    await _seed_htn_task(
        pid, name="Choose DB", task_type="decision",
        status="blocked", ordering=0
    )

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "btn-decision" in resp.text
    assert "Resolve" in resp.text
    assert f"/pipelines/{pid}/tasks/" in resp.text
    assert 'name="resolution"' in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_htn_no_tasks(client):
    """Pipeline without HTN tasks shows empty state.

    Invariant: when no htn_tasks exist, an empty message is shown.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "No HTN tasks" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_htn_checkpoint_rev(client):
    """Task checkpoint revision is displayed when set.

    Invariant: checkpoint_rev appears truncated to 12 characters.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    await _seed_htn_task(
        pid, name="Done task", status="completed",
        checkpoint_rev="aabb11223344556677", ordering=0
    )

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "aabb11223344" in resp.text


# ---------------------------------------------------------------------------
# Tests — lease health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_detail_lease_healthy(client):
    """Running pipeline with active lease shows healthy indicator.

    Invariant: owner_token + lease_expires_at + running status = lease healthy.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(
        repo_id, def_id, status="running",
        owner_token="tok-1", lease_expires_at="2099-01-01T00:00:00Z"
    )

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "lease-healthy" in resp.text
    assert "Lease active" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_lease_unhealthy(client):
    """Running pipeline without owner shows unhealthy indicator.

    Invariant: running + no owner_token = lease unhealthy warning.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(
        repo_id, def_id, status="running",
        owner_token=None, lease_expires_at=None
    )

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "lease-unhealthy" in resp.text
    assert "No active lease" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_no_lease_indicator_for_terminal(client):
    """Completed pipelines don't show lease indicators.

    Invariant: lease health is only relevant for running pipelines.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(
        repo_id, def_id, status="completed",
        owner_token=None, lease_expires_at=None
    )

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "lease-healthy" not in resp.text
    assert "lease-unhealthy" not in resp.text


# ---------------------------------------------------------------------------
# Tests — dirty snapshot visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_detail_dirty_snapshot(client):
    """Dirty snapshot artifact path is displayed when present.

    Invariant: when dirty_snapshot_artifact is set, a banner shows the path.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(
        repo_id, def_id,
        dirty_snapshot_artifact="/state/recovery/2026-04-09/snapshot.patch",
        workspace_state="dirty_snapshot_pending",
    )

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "dirty-snapshot-banner" in resp.text
    assert "snapshot.patch" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_workspace_dirty(client):
    """Dirty workspace state is shown as an indicator.

    Invariant: when workspace_state != 'clean', the indicator appears.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(
        repo_id, def_id,
        workspace_state="dirty_live",
    )

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "workspace-dirty" in resp.text
    assert "dirty_live" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_clean_workspace_no_indicator(client):
    """Clean workspace doesn't show the dirty indicator.

    Invariant: workspace_state='clean' means no dirty indicator.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, workspace_state="clean")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "workspace-dirty" not in resp.text
    assert "dirty-snapshot-banner" not in resp.text


# ---------------------------------------------------------------------------
# Tests — logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_detail_logs(client):
    """Pipeline logs are displayed in chronological order.

    Invariant: session_logs for this pipeline's sessions appear in the log section.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    sess_id = await _seed_session(stage_id)
    await _seed_log(sess_id, event_type="assistant_message", content="Hello world")
    await _seed_log(sess_id, event_type="tool_use", content="Read file.py")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "Hello world" in resp.text
    assert "Read file.py" in resp.text
    assert "pipeline-logs" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_no_logs(client):
    """Pipeline with no logs shows empty message.

    Invariant: when no session_logs exist, an empty state is shown.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "No log entries" in resp.text


@pytest.mark.asyncio
async def test_pipeline_logs_partial(client):
    """GET /pipelines/{id}/logs returns a log partial for HTMX polling.

    Invariant: the logs endpoint returns just the log entries partial.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    sess_id = await _seed_session(stage_id)
    await _seed_log(sess_id, content="HTMX polled log")

    resp = await client.get(f"/pipelines/{pid}/logs")
    assert resp.status_code == 200
    assert "HTMX polled log" in resp.text
    assert "log-entry" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_logs_sse_streaming(client):
    """Running pipelines use SSE for live log streaming with HTMX fallback.

    Invariant: active pipelines have EventSource pointing at the stream endpoint;
    HTMX polling attributes are set via JS fallback (not in initial HTML).
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert f"/pipelines/{pid}/stream" in resp.text
    assert "EventSource" in resp.text
    # HTMX polling is set via JS fallback, not statically in the HTML
    assert f"/pipelines/{pid}/logs" in resp.text


# ---------------------------------------------------------------------------
# Tests — clone management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_detail_clone_path(client):
    """Clone path is displayed with a copy button.

    Invariant: the clone path appears in a code element with a copy action.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, clone_path="/home/user/.build-your-room/clones/42")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "/home/user/.build-your-room/clones/42" in resp.text
    assert "btn-copy-path" in resp.text
    assert "Copy path" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_cleanup_button_terminal(client):
    """Cleanup button appears only for terminal-status pipelines.

    Invariant: completed/failed/cancelled/killed pipelines get the cleanup button.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="completed")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "btn-cleanup" in resp.text
    assert "Clean up clone" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_no_cleanup_button_running(client):
    """Running pipelines don't get the cleanup button.

    Invariant: non-terminal pipelines cannot have their clones deleted.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "btn-cleanup" not in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_base_and_head_rev(client):
    """Base and head revisions are displayed truncated.

    Invariant: review_base_rev and head_rev appear as 12-char prefixes.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(
        repo_id, def_id,
        review_base_rev="aabbccddee1122334455",
        head_rev="ff00112233445566778899",
    )

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "aabbccddee11" in resp.text
    assert "ff0011223344" in resp.text


# ---------------------------------------------------------------------------
# Tests — escalations on detail page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_detail_escalations(client):
    """Escalations for this pipeline are shown on the detail page.

    Invariant: pipeline-specific escalations appear with reason and status.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id, reason="design_decision")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "design decision" in resp.text
    assert "pipeline-escalations" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_no_escalation_section_when_none(client):
    """No escalation section when pipeline has no escalations.

    Invariant: the escalations heading is omitted when the list is empty.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "pipeline-escalations" not in resp.text


# ---------------------------------------------------------------------------
# Tests — edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_detail_invalid_stage_graph_json(client):
    """Pipeline with invalid stage_graph_json still renders.

    Invariant: malformed JSON in the pipeline def doesn't crash the page.
    """
    repo_id = await _seed_repo()
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipeline_defs (name, stage_graph_json) "
            "VALUES (%s, %s) RETURNING id",
            ("bad-graph-def", "not-valid-json"),
        )
        row = await cur.fetchone()
        assert row is not None
        def_id = row["id"]
        await conn.commit()

    pid = await _seed_pipeline(repo_id, def_id)

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    # Should render without crashing, just no graph nodes
    assert "stage-graph" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_all_terminal_statuses(client):
    """All terminal statuses show the cleanup button.

    Invariant: completed, failed, cancelled, killed all get cleanup.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()

    for i, status in enumerate(["completed", "failed", "cancelled", "killed"]):
        uid = uuid.uuid4().hex[:8]
        pid = await _seed_pipeline(
            repo_id, def_id, status=status,
            clone_path=f"/tmp/clone-{uid}",
            owner_token=None, lease_expires_at=None,
        )
        resp = await client.get(f"/pipelines/{pid}")
        assert resp.status_code == 200, f"Failed for status={status}"
        assert "btn-cleanup" in resp.text, f"No cleanup for status={status}"


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


@hyp_settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    status=st.sampled_from([
        "pending", "running", "paused", "cancel_requested",
        "cancelled", "killed", "completed", "failed", "needs_attention",
    ]),
)
@pytest.mark.asyncio
async def test_pipeline_detail_renders_for_all_statuses(initialized_db, status):
    """Property: the detail page renders 200 for any valid pipeline status.

    Invariant: for all status in spec statuses, GET /pipelines/{id} returns 200
    with a status badge containing the status string.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        uid = uuid.uuid4().hex[:12]
        repo_id = await _seed_repo(
            name=f"prop-{uid}", local_path=f"/tmp/prop-{uid}"
        )
        def_id = await _seed_pipeline_def(name=f"prop-def-{uid}")
        pid = await _seed_pipeline(
            repo_id, def_id, status=status,
            clone_path=f"/tmp/prop-c-{uid}",
            owner_token=None, lease_expires_at=None,
        )

        resp = await c.get(f"/pipelines/{pid}")
        assert resp.status_code == 200
        assert status in resp.text


@hyp_settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    task_status=st.sampled_from([
        "not_ready", "ready", "in_progress", "completed",
        "failed", "blocked", "skipped",
    ]),
)
@pytest.mark.asyncio
async def test_htn_task_status_renders_with_class(initialized_db, task_status):
    """Property: all HTN task statuses get a corresponding CSS class.

    Invariant: for all task status values, the rendered HTML contains
    the expected status class.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        uid = uuid.uuid4().hex[:12]
        repo_id = await _seed_repo(
            name=f"htn-{uid}", local_path=f"/tmp/htn-{uid}"
        )
        def_id = await _seed_pipeline_def(name=f"htn-def-{uid}")
        pid = await _seed_pipeline(
            repo_id, def_id,
            clone_path=f"/tmp/htn-c-{uid}",
            owner_token=None, lease_expires_at=None,
        )
        await _seed_htn_task(
            pid, name=f"task-{uid}", status=task_status, ordering=0
        )

        resp = await c.get(f"/pipelines/{pid}")
        assert resp.status_code == 200
        expected_class = f"task-{task_status}"
        assert expected_class in resp.text


@hyp_settings(max_examples=10, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    event_type=st.sampled_from([
        "assistant_message", "tool_use", "command_exec", "file_change",
        "error", "context_warning", "review_feedback", "escalation",
        "dirty_snapshot", "cancellation",
    ]),
)
@pytest.mark.asyncio
async def test_log_event_types_render(initialized_db, event_type):
    """Property: all spec log event types render in the log section.

    Invariant: for all event_type values, the log entry appears with
    the event type and content.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        uid = uuid.uuid4().hex[:12]
        repo_id = await _seed_repo(
            name=f"log-{uid}", local_path=f"/tmp/log-{uid}"
        )
        def_id = await _seed_pipeline_def(name=f"log-def-{uid}")
        pid = await _seed_pipeline(
            repo_id, def_id,
            clone_path=f"/tmp/log-c-{uid}",
            owner_token=None, lease_expires_at=None,
        )
        stage_id = await _seed_stage(pid)
        sess_id = await _seed_session(stage_id)
        await _seed_log(sess_id, event_type=event_type, content=f"log-{uid}")

        resp = await c.get(f"/pipelines/{pid}")
        assert resp.status_code == 200
        assert event_type in resp.text
        assert f"log-{uid}" in resp.text
