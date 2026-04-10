"""Tests for POST /pipelines/{id}/tasks/{task_id}/resolve — HTN decision task resolution.

Covers: resolving decision tasks from the pipeline detail page, validation guards,
and template rendering of the inline resolution form.
"""

from __future__ import annotations

import json
from typing import Any

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
    ],
    "edges": [],
})


async def _seed_repo(
    name: str = "htn-repo", local_path: str = "/tmp/htn-repo",
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


async def _seed_pipeline_def(name: str = "htn-def") -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipeline_defs (name, stage_graph_json) "
            "VALUES (%s, %s) RETURNING id",
            (name, _FULL_GRAPH),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_pipeline(
    repo_id: int,
    def_id: int,
    status: str = "running",
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipelines "
            "(pipeline_def_id, repo_id, clone_path, review_base_rev, status, config_json) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (def_id, repo_id, "/tmp/clone", "abc123", status, "{}"),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_decision_task(
    pipeline_id: int,
    *,
    name: str = "design-choice",
    status: str = "blocked",
) -> int:
    """Insert a decision-type HTN task and return its id."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO htn_tasks "
            "(pipeline_id, name, description, task_type, status, ordering, "
            " preconditions_json, postconditions_json) "
            "VALUES (%s, %s, %s, 'decision', %s, 0, '[]', '[]') RETURNING id",
            (pipeline_id, name, "Choose an approach", status),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_primitive_task(
    pipeline_id: int,
    *,
    name: str = "impl-work",
    status: str = "not_ready",
) -> int:
    """Insert a primitive-type HTN task and return its id."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO htn_tasks "
            "(pipeline_id, name, description, task_type, status, ordering, "
            " preconditions_json, postconditions_json) "
            "VALUES (%s, %s, %s, 'primitive', %s, 1, '[]', '[]') RETURNING id",
            (pipeline_id, name, "Do implementation work", status),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_dependency(task_id: int, depends_on_id: int) -> None:
    pool = get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO htn_task_deps (task_id, depends_on_task_id, dep_type) "
            "VALUES (%s, %s, 'hard')",
            (task_id, depends_on_id),
        )
        await conn.commit()


async def _seed_escalation(
    pipeline_id: int, task_id: int, status: str = "open",
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO escalations "
            "(pipeline_id, reason, context_json, status) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (
                pipeline_id,
                "design_decision",
                json.dumps({"task_id": task_id, "description": "Choose an approach"}),
                status,
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _get_task(task_id: int) -> dict[str, Any]:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT * FROM htn_tasks WHERE id = %s", (task_id,)
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        assert row is not None
        return row


async def _get_escalation_status(esc_id: int) -> str:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status FROM escalations WHERE id = %s", (esc_id,)
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        assert row is not None
        return row["status"]


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_decision_task(client):
    """POST /pipelines/{pid}/tasks/{tid}/resolve completes the decision task.

    Invariant: resolved decision tasks have status='completed' and diary_entry
    set to the resolution text, with a 303 redirect to the pipeline detail.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    tid = await _seed_decision_task(pid)

    resp = await client.post(
        f"/pipelines/{pid}/tasks/{tid}/resolve",
        data={"resolution": "Use approach A"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/pipelines/{pid}"

    task = await _get_task(tid)
    assert task["status"] == "completed"
    assert task["diary_entry"] == "Use approach A"


@pytest.mark.asyncio
async def test_resolve_decision_resolves_linked_escalation(client):
    """Resolving a decision task also resolves the linked escalation.

    Invariant: the escalation tied to this decision task transitions to 'resolved'.
    """
    repo_id = await _seed_repo(name="esc-repo", local_path="/tmp/esc-repo")
    def_id = await _seed_pipeline_def(name="esc-def")
    pid = await _seed_pipeline(repo_id, def_id)
    tid = await _seed_decision_task(pid, name="esc-decision")
    esc_id = await _seed_escalation(pid, tid)

    resp = await client.post(
        f"/pipelines/{pid}/tasks/{tid}/resolve",
        data={"resolution": "Go with plan B"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert await _get_escalation_status(esc_id) == "resolved"


@pytest.mark.asyncio
async def test_resolve_decision_unblocks_dependents(client):
    """Resolving a decision task propagates readiness to dependent tasks.

    Invariant: tasks whose only hard dependency was the resolved decision
    transition from 'not_ready' to 'ready'.
    """
    repo_id = await _seed_repo(name="dep-repo", local_path="/tmp/dep-repo")
    def_id = await _seed_pipeline_def(name="dep-def")
    pid = await _seed_pipeline(repo_id, def_id)
    decision_id = await _seed_decision_task(pid, name="dep-decision")
    dependent_id = await _seed_primitive_task(pid, name="dep-impl")
    await _seed_dependency(dependent_id, decision_id)

    await client.post(
        f"/pipelines/{pid}/tasks/{decision_id}/resolve",
        data={"resolution": "Proceed with X"},
        follow_redirects=False,
    )

    dependent = await _get_task(dependent_id)
    assert dependent["status"] == "ready"


# ---------------------------------------------------------------------------
# Tests — validation guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_404_pipeline_not_found(client):
    """POST resolve returns 404 when the pipeline does not exist.

    Invariant: non-existent pipeline IDs yield 404.
    """
    resp = await client.post(
        "/pipelines/99999/tasks/1/resolve",
        data={"resolution": "test"},
        follow_redirects=False,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_resolve_404_task_not_found(client):
    """POST resolve returns 404 when the task does not exist.

    Invariant: non-existent task IDs yield 404.
    """
    repo_id = await _seed_repo(name="tnf-repo", local_path="/tmp/tnf-repo")
    def_id = await _seed_pipeline_def(name="tnf-def")
    pid = await _seed_pipeline(repo_id, def_id)

    resp = await client.post(
        f"/pipelines/{pid}/tasks/99999/resolve",
        data={"resolution": "test"},
        follow_redirects=False,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_resolve_404_task_wrong_pipeline(client):
    """POST resolve returns 404 when the task belongs to a different pipeline.

    Invariant: task must belong to the pipeline in the URL.
    """
    repo_id = await _seed_repo(name="wp-repo", local_path="/tmp/wp-repo")
    def_id = await _seed_pipeline_def(name="wp-def")
    pid1 = await _seed_pipeline(repo_id, def_id)
    pid2 = await _seed_pipeline(repo_id, def_id)
    tid = await _seed_decision_task(pid1, name="wp-decision")

    resp = await client.post(
        f"/pipelines/{pid2}/tasks/{tid}/resolve",
        data={"resolution": "test"},
        follow_redirects=False,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_resolve_409_non_decision_task(client):
    """POST resolve returns 409 for a primitive (non-decision) task.

    Invariant: only decision tasks can be resolved via this endpoint.
    """
    repo_id = await _seed_repo(name="nd-repo", local_path="/tmp/nd-repo")
    def_id = await _seed_pipeline_def(name="nd-def")
    pid = await _seed_pipeline(repo_id, def_id)
    tid = await _seed_primitive_task(pid, name="nd-prim")

    resp = await client.post(
        f"/pipelines/{pid}/tasks/{tid}/resolve",
        data={"resolution": "test"},
        follow_redirects=False,
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_resolve_already_completed_is_noop(client):
    """POST resolve on an already-completed decision redirects without error.

    Invariant: completing a decision task twice is idempotent — no DB error,
    just a redirect back to the pipeline.
    """
    repo_id = await _seed_repo(name="ac-repo", local_path="/tmp/ac-repo")
    def_id = await _seed_pipeline_def(name="ac-def")
    pid = await _seed_pipeline(repo_id, def_id)
    tid = await _seed_decision_task(pid, name="ac-decision", status="completed")

    resp = await client.post(
        f"/pipelines/{pid}/tasks/{tid}/resolve",
        data={"resolution": "another answer"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/pipelines/{pid}"


# ---------------------------------------------------------------------------
# Tests — template rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detail_page_shows_decision_form(client):
    """Pipeline detail page renders an inline resolution form for decision tasks.

    Invariant: unresolved decision tasks show a form with action pointing to
    the resolve endpoint and a resolution input field.
    """
    repo_id = await _seed_repo(name="tmpl-repo", local_path="/tmp/tmpl-repo")
    def_id = await _seed_pipeline_def(name="tmpl-def")
    pid = await _seed_pipeline(repo_id, def_id)
    tid = await _seed_decision_task(pid, name="tmpl-decision")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert f"/pipelines/{pid}/tasks/{tid}/resolve" in resp.text
    assert 'name="resolution"' in resp.text
    assert "Resolve" in resp.text


@pytest.mark.asyncio
async def test_detail_page_hides_form_for_completed_decision(client):
    """Pipeline detail page does not show a resolution form for completed decisions.

    Invariant: completed decision tasks display diary entry instead of form.
    """
    repo_id = await _seed_repo(name="cd-repo", local_path="/tmp/cd-repo")
    def_id = await _seed_pipeline_def(name="cd-def")
    pid = await _seed_pipeline(repo_id, def_id)

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO htn_tasks "
            "(pipeline_id, name, description, task_type, status, ordering, "
            " preconditions_json, postconditions_json, diary_entry) "
            "VALUES (%s, %s, %s, 'decision', 'completed', 0, '[]', '[]', %s) "
            "RETURNING id",
            (pid, "completed-decision", "Already resolved", "We chose plan B"),
        )
        row = await cur.fetchone()
        assert row is not None
        tid = row["id"]
        await conn.commit()

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert f"/pipelines/{pid}/tasks/{tid}/resolve" not in resp.text
    assert "We chose plan B" in resp.text


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


_NON_DECISION_TYPES = st.sampled_from(["primitive", "compound"])


@pytest.mark.asyncio
@hyp_settings(
    max_examples=4,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(task_type=_NON_DECISION_TYPES)
async def test_resolve_rejects_non_decision_types(client, task_type: str):
    """POST resolve always returns 409 for non-decision task types.

    Invariant: only 'decision' task_type is resolvable.
    """
    repo_id = await _seed_repo(
        name=f"pbt-{task_type}", local_path=f"/tmp/pbt-{task_type}",
    )
    def_id = await _seed_pipeline_def(name=f"pbt-def-{task_type}")
    pid = await _seed_pipeline(repo_id, def_id)

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO htn_tasks "
            "(pipeline_id, name, description, task_type, status, ordering, "
            " preconditions_json, postconditions_json) "
            "VALUES (%s, %s, %s, %s, 'ready', 0, '[]', '[]') RETURNING id",
            (pid, f"task-{task_type}", "test", task_type),
        )
        row = await cur.fetchone()
        assert row is not None
        tid = row["id"]
        await conn.commit()

    resp = await client.post(
        f"/pipelines/{pid}/tasks/{tid}/resolve",
        data={"resolution": "test"},
        follow_redirects=False,
    )
    assert resp.status_code == 409
