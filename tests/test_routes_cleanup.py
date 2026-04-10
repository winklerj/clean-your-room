"""Tests for clone cleanup endpoints — per-pipeline and bulk."""

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

_SIMPLE_GRAPH = json.dumps({
    "entry_stage": "spec_author",
    "nodes": [
        {"key": "spec_author", "name": "Spec", "type": "spec_author",
         "agent": "claude", "prompt": "spec_author_default",
         "model": "claude-sonnet-4-6", "max_iterations": 1},
    ],
    "edges": [],
})


async def _seed_repo(name: str | None = None) -> int:
    pool = get_pool()
    name = name or f"repo-{uuid.uuid4().hex[:8]}"
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO repos (name, local_path) VALUES (%s, %s) RETURNING id",
            (name, "/tmp/fake-repo"),
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
    status: str = "completed",
    clone_path: str = "/tmp/clone",
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipelines "
            "(pipeline_def_id, repo_id, clone_path, review_base_rev, status, config_json) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (def_id, repo_id, clone_path, "abc123", status, "{}"),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


# ---------------------------------------------------------------------------
# Tests — per-pipeline cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_returns_404_for_missing_pipeline(client):
    """POST /pipelines/{id}/cleanup returns 404 when pipeline does not exist."""
    resp = await client.post("/pipelines/99999/cleanup")
    assert resp.status_code == 404
    assert "not found" in resp.text.lower()


@pytest.mark.asyncio
async def test_cleanup_rejects_non_terminal_pipeline(client):
    """POST /pipelines/{id}/cleanup returns 409 for running pipelines.

    Invariant: only terminal pipelines (completed/failed/cancelled/killed)
    can have their clones cleaned up.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.post(f"/pipelines/{pid}/cleanup")
    assert resp.status_code == 409
    assert "still active" in resp.text.lower()


@pytest.mark.asyncio
async def test_cleanup_deletes_clone_directory(client, tmp_path):
    """POST /pipelines/{id}/cleanup removes the clone directory on disk."""
    clone_dir = tmp_path / "clone-42"
    clone_dir.mkdir()
    (clone_dir / "file.txt").write_text("test content")

    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(
        repo_id, def_id, status="completed", clone_path=str(clone_dir),
    )

    resp = await client.post(
        f"/pipelines/{pid}/cleanup",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert not clone_dir.exists()


@pytest.mark.asyncio
async def test_cleanup_succeeds_when_clone_already_missing(client):
    """POST /pipelines/{id}/cleanup succeeds even if clone dir was already deleted."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(
        repo_id, def_id, status="completed", clone_path="/tmp/nonexistent-clone",
    )

    resp = await client.post(
        f"/pipelines/{pid}/cleanup",
        follow_redirects=False,
    )
    # Should succeed — idempotent operation
    assert resp.status_code == 303


@pytest.mark.asyncio
async def test_cleanup_htmx_returns_card_partial(client):
    """HTMX cleanup request returns updated pipeline card HTML.

    When the HX-Request header is present, the endpoint returns a pipeline
    card partial for in-place swap instead of redirecting.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="completed")

    resp = await client.post(
        f"/pipelines/{pid}/cleanup",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert f'id="pipeline-{pid}"' in resp.text
    assert "pipeline-card" in resp.text


@pytest.mark.asyncio
async def test_cleanup_non_htmx_redirects_to_detail(client):
    """Non-HTMX cleanup request redirects to the pipeline detail page."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="completed")

    resp = await client.post(
        f"/pipelines/{pid}/cleanup",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/pipelines/{pid}"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "killed"])
async def test_cleanup_allowed_for_all_terminal_statuses(client, status):
    """All terminal statuses allow clone cleanup.

    Invariant: completed, failed, cancelled, and killed pipelines can all
    have their clones cleaned up.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status=status)

    resp = await client.post(
        f"/pipelines/{pid}/cleanup",
        follow_redirects=False,
    )
    assert resp.status_code == 303


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "running", "paused", "needs_attention"])
async def test_cleanup_rejected_for_non_terminal_statuses(client, status):
    """Non-terminal statuses reject clone cleanup.

    Invariant: active pipelines cannot have their clones deleted.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status=status)

    resp = await client.post(f"/pipelines/{pid}/cleanup")
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Tests — bulk cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_cleanup_redirects_to_dashboard(client):
    """POST /pipelines/cleanup-completed redirects to the dashboard."""
    resp = await client.post(
        "/pipelines/cleanup-completed",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


@pytest.mark.asyncio
async def test_bulk_cleanup_deletes_completed_clones(client, tmp_path):
    """Bulk cleanup removes clone directories for all terminal pipelines."""
    clone1 = tmp_path / "clone-1"
    clone2 = tmp_path / "clone-2"
    clone_running = tmp_path / "clone-3"
    for d in (clone1, clone2, clone_running):
        d.mkdir()
        (d / "file.txt").write_text("content")

    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    await _seed_pipeline(repo_id, def_id, status="completed", clone_path=str(clone1))
    await _seed_pipeline(repo_id, def_id, status="cancelled", clone_path=str(clone2))
    await _seed_pipeline(repo_id, def_id, status="running", clone_path=str(clone_running))

    await client.post("/pipelines/cleanup-completed", follow_redirects=False)

    assert not clone1.exists(), "completed pipeline clone should be deleted"
    assert not clone2.exists(), "cancelled pipeline clone should be deleted"
    assert clone_running.exists(), "running pipeline clone must not be deleted"


@pytest.mark.asyncio
async def test_bulk_cleanup_succeeds_with_no_terminal_pipelines(client):
    """Bulk cleanup is a no-op when no terminal pipelines exist."""
    resp = await client.post(
        "/pipelines/cleanup-completed",
        follow_redirects=False,
    )
    assert resp.status_code == 303


@pytest.mark.asyncio
async def test_bulk_cleanup_skips_missing_clone_dirs(client):
    """Bulk cleanup silently skips pipelines whose clone dirs don't exist."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    await _seed_pipeline(
        repo_id, def_id, status="completed",
        clone_path="/tmp/nonexistent-bulk-clone",
    )

    resp = await client.post(
        "/pipelines/cleanup-completed",
        follow_redirects=False,
    )
    assert resp.status_code == 303


# ---------------------------------------------------------------------------
# Tests — dashboard bulk button visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_shows_bulk_cleanup_when_terminal_exists(client):
    """Dashboard shows bulk cleanup button when terminal pipelines exist."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    await _seed_pipeline(repo_id, def_id, status="completed")

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "cleanup-completed" in resp.text
    assert "Clean all completed" in resp.text


@pytest.mark.asyncio
async def test_dashboard_hides_bulk_cleanup_when_no_terminal(client):
    """Dashboard hides bulk cleanup button when no terminal pipelines exist."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "cleanup-completed" not in resp.text


@pytest.mark.asyncio
async def test_dashboard_terminal_count_shown(client):
    """Dashboard shows correct count of terminal pipelines in button."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    await _seed_pipeline(repo_id, def_id, status="completed")
    await _seed_pipeline(repo_id, def_id, status="failed")
    await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Clean all completed (2)" in resp.text


# ---------------------------------------------------------------------------
# Tests — pipeline detail page cleanup button
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detail_page_shows_cleanup_for_terminal(client):
    """Pipeline detail page shows cleanup button for terminal pipelines."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="completed")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "Clean up clone" in resp.text
    assert f"/pipelines/{pid}/cleanup" in resp.text


@pytest.mark.asyncio
async def test_detail_page_hides_cleanup_for_running(client):
    """Pipeline detail page hides cleanup button for running pipelines."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "Clean up clone" not in resp.text


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=15,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(status=st.sampled_from(["completed", "failed", "cancelled", "killed"]))
@pytest.mark.asyncio
async def test_prop_any_terminal_status_allows_cleanup(client, status):
    """Property: all terminal statuses permit clone cleanup."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status=status)

    resp = await client.post(
        f"/pipelines/{pid}/cleanup",
        follow_redirects=False,
    )
    assert resp.status_code == 303


@hyp_settings(
    max_examples=15,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(status=st.sampled_from(["pending", "running", "paused", "needs_attention"]))
@pytest.mark.asyncio
async def test_prop_non_terminal_status_rejects_cleanup(client, status):
    """Property: non-terminal statuses reject clone cleanup."""
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status=status)

    resp = await client.post(f"/pipelines/{pid}/cleanup")
    assert resp.status_code == 409
