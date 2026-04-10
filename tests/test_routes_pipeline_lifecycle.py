"""Tests for pipeline creation form, lifecycle control HTML routes.

Covers: GET /pipelines/new, POST /pipelines, POST /pipelines/{id}/cancel,
POST /pipelines/{id}/kill, POST /pipelines/{id}/pause, POST /pipelines/{id}/resume.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

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
    name: str = "test-repo", local_path: str = "/tmp/test-repo",
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


async def _seed_pipeline_def(name: str = "test-def") -> int:
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


async def _seed_escalation(pipeline_id: int, status: str = "open") -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO escalations "
            "(pipeline_id, reason, context_json, status) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (pipeline_id, "max_iterations", "{}", status),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _get_pipeline_status(pipeline_id: int) -> str:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status FROM pipelines WHERE id = %s", (pipeline_id,)
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        assert row is not None
        return row["status"]


async def _get_escalation_status(escalation_id: int) -> str:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status FROM escalations WHERE id = %s", (escalation_id,)
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        assert row is not None
        return row["status"]


# ---------------------------------------------------------------------------
# Tests — creation form rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_pipeline_form_renders(client):
    """GET /pipelines/new returns 200 with the creation form.

    Invariant: the form renders with Repository and Pipeline Definition labels.
    """
    resp = await client.get("/pipelines/new")
    assert resp.status_code == 200
    assert "Create Pipeline" in resp.text
    assert "Repository" in resp.text
    assert "Pipeline Definition" in resp.text


@pytest.mark.asyncio
async def test_new_pipeline_form_shows_repos_and_defs(client):
    """GET /pipelines/new lists available repos and pipeline defs.

    Invariant: seeded repos and defs appear as options in the form.
    """
    await _seed_repo(name="my-awesome-repo")
    await _seed_pipeline_def(name="full-coding-pipeline")

    resp = await client.get("/pipelines/new")
    assert resp.status_code == 200
    assert "my-awesome-repo" in resp.text
    assert "full-coding-pipeline" in resp.text


@pytest.mark.asyncio
async def test_new_pipeline_form_empty_state(client):
    """GET /pipelines/new shows guidance when no repos or defs exist.

    Invariant: empty state links to /repos/new and /pipeline-defs/new.
    """
    resp = await client.get("/pipelines/new")
    assert resp.status_code == 200
    assert "/repos/new" in resp.text
    assert "/pipeline-defs/new" in resp.text


@pytest.mark.asyncio
async def test_new_pipeline_form_excludes_archived_repos(client):
    """GET /pipelines/new does not list archived repos.

    Invariant: archived repos are not available for pipeline creation.
    """
    pool = get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO repos (name, local_path, archived) VALUES (%s, %s, %s)",
            ("archived-repo", "/tmp/archived", 1),
        )
        await conn.commit()

    resp = await client.get("/pipelines/new")
    assert resp.status_code == 200
    assert "archived-repo" not in resp.text


# ---------------------------------------------------------------------------
# Tests — pipeline creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_pipeline_success(client):
    """POST /pipelines creates a pipeline and redirects to its detail page.

    Invariant: a new pipeline row is inserted with 'pending' status, and the
    response is a 303 redirect to /pipelines/{id}.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()

    with patch("build_your_room.main.orchestrator", None):
        resp = await client.post(
            "/pipelines",
            data={"repo_id": str(repo_id), "pipeline_def_id": str(def_id)},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert "/pipelines/" in resp.headers["location"]

    # Verify pipeline was created
    pid = int(resp.headers["location"].split("/")[-1])
    status = await _get_pipeline_status(pid)
    assert status == "pending"


@pytest.mark.asyncio
async def test_create_pipeline_starts_orchestrator(client):
    """POST /pipelines calls orchestrator.start_pipeline when available.

    Invariant: when the orchestrator is available, it starts the new pipeline.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()

    mock_orch = AsyncMock()
    with patch("build_your_room.main.orchestrator", mock_orch):
        resp = await client.post(
            "/pipelines",
            data={"repo_id": str(repo_id), "pipeline_def_id": str(def_id)},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    mock_orch.start_pipeline.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_pipeline_invalid_def(client):
    """POST /pipelines with invalid def_id re-renders form with error.

    Invariant: no pipeline is created when the definition doesn't exist.
    """
    repo_id = await _seed_repo()

    resp = await client.post(
        "/pipelines",
        data={"repo_id": str(repo_id), "pipeline_def_id": "99999"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "not found" in resp.text.lower()


@pytest.mark.asyncio
async def test_create_pipeline_invalid_repo(client):
    """POST /pipelines with invalid repo_id re-renders form with error.

    Invariant: no pipeline is created when the repo doesn't exist.
    """
    def_id = await _seed_pipeline_def()

    resp = await client.post(
        "/pipelines",
        data={"repo_id": "99999", "pipeline_def_id": str(def_id)},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "not found" in resp.text.lower()


# ---------------------------------------------------------------------------
# Tests — creation form UX (preview, hints)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_pipeline_form_has_def_preview_target(client):
    """GET /pipelines/new includes the def-preview container for htmx previews.

    Invariant: the form has the target div and htmx wiring on the select.
    """
    await _seed_repo()
    await _seed_pipeline_def()
    resp = await client.get("/pipelines/new")
    assert resp.status_code == 200
    assert 'id="def-preview"' in resp.text
    assert 'id="def-selector"' in resp.text


@pytest.mark.asyncio
async def test_new_pipeline_form_has_field_hint(client):
    """GET /pipelines/new shows a hint under the pipeline def label.

    Invariant: the form contains a field-hint element describing what defs do.
    """
    resp = await client.get("/pipelines/new")
    assert resp.status_code == 200
    assert "field-hint" in resp.text
    assert "what work" in resp.text.lower()


@pytest.mark.asyncio
async def test_new_pipeline_form_preselects_repo(client):
    """GET /pipelines/new?repo_id=N pre-selects the repo in the dropdown.

    Invariant: the matching option has the 'selected' attribute.
    """
    repo_id = await _seed_repo(name="presel-repo")
    resp = await client.get(f"/pipelines/new?repo_id={repo_id}")
    assert resp.status_code == 200
    assert "selected" in resp.text
    assert "presel-repo" in resp.text


# ---------------------------------------------------------------------------
# Tests — pipeline detail pending state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_pipeline_shows_banner(client):
    """GET /pipelines/{id} for a pending pipeline shows a starting-up banner.

    Invariant: the pending banner explains the pipeline is initializing
    and does not show the cancel button outside the banner.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="pending")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "pipeline-pending-banner" in resp.text
    assert "Starting up" in resp.text
    assert "orchestrator" in resp.text.lower()


@pytest.mark.asyncio
async def test_running_pipeline_no_pending_banner(client):
    """GET /pipelines/{id} for a running pipeline does not show the pending banner.

    Invariant: once running, the pending-specific messaging is gone.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "pipeline-pending-banner" not in resp.text


@pytest.mark.asyncio
async def test_completed_pipeline_no_pending_banner(client):
    """GET /pipelines/{id} for a completed pipeline does not show the pending banner.

    Invariant: terminal states never show the pending banner.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="completed")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "pipeline-pending-banner" not in resp.text


# ---------------------------------------------------------------------------
# Tests — cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_running_pipeline(client):
    """POST /pipelines/{id}/cancel on a running pipeline sets cancel_requested.

    Invariant: cancellation updates DB status and redirects to detail page.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.post(
        f"/pipelines/{pid}/cancel", follow_redirects=False,
    )
    assert resp.status_code == 303
    assert f"/pipelines/{pid}" in resp.headers["location"]
    assert await _get_pipeline_status(pid) == "cancel_requested"


@pytest.mark.asyncio
async def test_cancel_paused_pipeline(client):
    """POST /pipelines/{id}/cancel on a paused pipeline sets cancel_requested.

    Invariant: paused pipelines can be cancelled.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="paused")

    resp = await client.post(
        f"/pipelines/{pid}/cancel", follow_redirects=False,
    )
    assert resp.status_code == 303
    assert await _get_pipeline_status(pid) == "cancel_requested"


@pytest.mark.asyncio
async def test_cancel_completed_pipeline_noop(client):
    """POST /pipelines/{id}/cancel on a completed pipeline is a no-op redirect.

    Invariant: completed pipelines cannot be cancelled — status stays unchanged.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="completed")

    resp = await client.post(
        f"/pipelines/{pid}/cancel", follow_redirects=False,
    )
    assert resp.status_code == 303
    assert await _get_pipeline_status(pid) == "completed"


@pytest.mark.asyncio
async def test_cancel_404(client):
    """POST /pipelines/{id}/cancel returns 404 for non-existent pipeline.

    Invariant: non-existent pipeline IDs yield 404.
    """
    resp = await client.post("/pipelines/99999/cancel", follow_redirects=False)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests — kill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_running_pipeline(client):
    """POST /pipelines/{id}/kill on a running pipeline sets killed.

    Invariant: kill updates DB status to 'killed' and redirects.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.post(
        f"/pipelines/{pid}/kill", follow_redirects=False,
    )
    assert resp.status_code == 303
    assert await _get_pipeline_status(pid) == "killed"


@pytest.mark.asyncio
async def test_kill_paused_pipeline(client):
    """POST /pipelines/{id}/kill on a paused pipeline sets killed.

    Invariant: paused pipelines can be killed.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="paused")

    resp = await client.post(
        f"/pipelines/{pid}/kill", follow_redirects=False,
    )
    assert resp.status_code == 303
    assert await _get_pipeline_status(pid) == "killed"


@pytest.mark.asyncio
async def test_kill_completed_pipeline_noop(client):
    """POST /pipelines/{id}/kill on a completed pipeline is a no-op redirect.

    Invariant: terminal pipelines cannot be killed — status stays unchanged.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="completed")

    resp = await client.post(
        f"/pipelines/{pid}/kill", follow_redirects=False,
    )
    assert resp.status_code == 303
    assert await _get_pipeline_status(pid) == "completed"


@pytest.mark.asyncio
async def test_kill_404(client):
    """POST /pipelines/{id}/kill returns 404 for non-existent pipeline.

    Invariant: non-existent pipeline IDs yield 404.
    """
    resp = await client.post("/pipelines/99999/kill", follow_redirects=False)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests — pause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_running_pipeline(client):
    """POST /pipelines/{id}/pause on a running pipeline sets paused.

    Invariant: pause transitions running → paused and redirects.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.post(
        f"/pipelines/{pid}/pause", follow_redirects=False,
    )
    assert resp.status_code == 303
    assert await _get_pipeline_status(pid) == "paused"


@pytest.mark.asyncio
async def test_pause_non_running_pipeline_noop(client):
    """POST /pipelines/{id}/pause on a paused pipeline is a no-op.

    Invariant: only running pipelines can be paused — non-running stays unchanged.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="paused")

    resp = await client.post(
        f"/pipelines/{pid}/pause", follow_redirects=False,
    )
    assert resp.status_code == 303
    assert await _get_pipeline_status(pid) == "paused"


# ---------------------------------------------------------------------------
# Tests — resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_paused_pipeline(client):
    """POST /pipelines/{id}/resume on a paused pipeline transitions to pending.

    Invariant: resume resolves open escalations and sets pipeline to 'pending'.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="paused")
    esc_id = await _seed_escalation(pid, status="open")

    resp = await client.post(
        f"/pipelines/{pid}/resume",
        data={"resolution": "All good now"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert await _get_pipeline_status(pid) == "pending"
    assert await _get_escalation_status(esc_id) == "resolved"


@pytest.mark.asyncio
async def test_resume_default_resolution(client):
    """POST /pipelines/{id}/resume with empty resolution uses default text.

    Invariant: escalation resolved_at is set and resolution has default text.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="paused")
    esc_id = await _seed_escalation(pid, status="open")

    resp = await client.post(
        f"/pipelines/{pid}/resume",
        data={"resolution": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT resolution FROM escalations WHERE id = %s", (esc_id,)
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
    assert row is not None
    assert "Resumed from dashboard" in row["resolution"]


@pytest.mark.asyncio
async def test_resume_non_paused_pipeline_noop(client):
    """POST /pipelines/{id}/resume on a running pipeline doesn't change status.

    Invariant: only paused pipelines can be resumed.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.post(
        f"/pipelines/{pid}/resume",
        data={"resolution": "test"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert await _get_pipeline_status(pid) == "running"


# ---------------------------------------------------------------------------
# Tests — detail page shows action buttons
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detail_page_running_shows_cancel_kill_pause(client):
    """Pipeline detail page for a running pipeline shows Cancel, Kill, Pause buttons.

    Invariant: running pipelines expose all three control actions.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert f"/pipelines/{pid}/cancel" in resp.text
    assert f"/pipelines/{pid}/kill" in resp.text
    assert f"/pipelines/{pid}/pause" in resp.text


@pytest.mark.asyncio
async def test_detail_page_paused_shows_resume_and_cancel(client):
    """Pipeline detail page for a paused pipeline shows Resume and Cancel buttons.

    Invariant: paused pipelines expose resume with resolution input and cancel.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="paused")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert f"/pipelines/{pid}/resume" in resp.text
    assert f"/pipelines/{pid}/cancel" in resp.text
    assert "resolution" in resp.text


@pytest.mark.asyncio
async def test_detail_page_completed_no_actions(client):
    """Pipeline detail page for a completed pipeline shows no lifecycle actions.

    Invariant: terminal pipelines don't expose control actions.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="completed")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert f"/pipelines/{pid}/cancel" not in resp.text
    assert f"/pipelines/{pid}/kill" not in resp.text
    assert f"/pipelines/{pid}/pause" not in resp.text
    assert f"/pipelines/{pid}/resume" not in resp.text


# ---------------------------------------------------------------------------
# Tests — dashboard new pipeline link
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_new_pipeline_link(client):
    """Dashboard includes a link to create a new pipeline.

    Invariant: /pipelines/new is always accessible from the dashboard.
    """
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "/pipelines/new" in resp.text


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


_NON_CANCELLABLE_STATUSES = st.sampled_from(
    ["completed", "failed", "cancelled", "killed"]
)
_CANCELLABLE_STATUSES = st.sampled_from(["running", "paused"])


@pytest.mark.asyncio
@hyp_settings(
    max_examples=8,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(status=_NON_CANCELLABLE_STATUSES)
async def test_cancel_noop_for_terminal_statuses(client, status: str):
    """POST /pipelines/{id}/cancel is always a no-op for terminal pipelines.

    Invariant: cancel never changes the status of a terminal pipeline.
    """
    repo_id = await _seed_repo(name=f"r-{status}", local_path=f"/tmp/r-{status}")
    def_id = await _seed_pipeline_def(name=f"d-{status}")
    pid = await _seed_pipeline(repo_id, def_id, status=status)

    await client.post(f"/pipelines/{pid}/cancel", follow_redirects=False)
    assert await _get_pipeline_status(pid) == status


@pytest.mark.asyncio
@hyp_settings(
    max_examples=8,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(status=_NON_CANCELLABLE_STATUSES)
async def test_kill_noop_for_terminal_statuses(client, status: str):
    """POST /pipelines/{id}/kill is always a no-op for terminal pipelines.

    Invariant: kill never changes the status of a terminal pipeline.
    """
    repo_id = await _seed_repo(name=f"kr-{status}", local_path=f"/tmp/kr-{status}")
    def_id = await _seed_pipeline_def(name=f"kd-{status}")
    pid = await _seed_pipeline(repo_id, def_id, status=status)

    await client.post(f"/pipelines/{pid}/kill", follow_redirects=False)
    assert await _get_pipeline_status(pid) == status


@pytest.mark.asyncio
@hyp_settings(
    max_examples=4,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(status=_CANCELLABLE_STATUSES)
async def test_cancel_always_transitions_cancellable(client, status: str):
    """POST /pipelines/{id}/cancel always transitions running/paused to cancel_requested.

    Invariant: cancel_requested is the only valid cancel transition target.
    """
    repo_id = await _seed_repo(name=f"ct-{status}", local_path=f"/tmp/ct-{status}")
    def_id = await _seed_pipeline_def(name=f"ctd-{status}")
    pid = await _seed_pipeline(repo_id, def_id, status=status)

    await client.post(f"/pipelines/{pid}/cancel", follow_redirects=False)
    assert await _get_pipeline_status(pid) == "cancel_requested"
