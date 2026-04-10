"""Tests for the escalation queue page — list, resolve, dismiss escalations."""

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


async def _seed_pipeline_def(name: str = "test-def") -> int:
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
    status: str = "needs_attention",
    clone_path: str = "/tmp/clone",
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipelines "
            "(pipeline_def_id, repo_id, clone_path, review_base_rev, status, "
            " current_stage_key, config_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (def_id, repo_id, clone_path, "abc123", status,
             "spec_author", "{}"),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_stage(
    pipeline_id: int,
    stage_key: str = "spec_author",
    stage_type: str = "spec_author",
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipeline_stages "
            "(pipeline_id, stage_key, stage_type, agent_type, status, "
            " iteration, max_iterations) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (pipeline_id, stage_key, stage_type, "claude", "running", 1, 3),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_escalation(
    pipeline_id: int,
    status: str = "open",
    stage_id: int | None = None,
    reason: str = "max_iterations",
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
# Tests — page rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_queue_empty(client):
    """Empty queue renders the all-clear message.

    Invariant: when no open escalations exist, the page shows an empty-state
    indicator and no escalation cards.
    """
    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert "Escalation Queue" in resp.text
    assert "All clear" in resp.text
    assert "escalation-card" not in resp.text


@pytest.mark.asyncio
async def test_escalation_queue_shows_open_escalation(client):
    """Open escalations render as cards with pipeline and stage context.

    Invariant: each open escalation is displayed with its pipeline name,
    repo name, stage type, and reason badge.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def(name="full-pipeline")
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id, reason="max_iterations")

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert "escalation-card" in resp.text
    assert "full-pipeline" in resp.text
    assert "my-project" in resp.text
    assert "spec_author" in resp.text
    assert "max iterations" in resp.text


@pytest.mark.asyncio
async def test_escalation_queue_shows_reason_badge(client):
    """Each escalation reason gets a human-readable badge.

    Invariant: the reason field is rendered with underscores replaced by spaces.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id, reason="design_decision")

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert "design decision" in resp.text
    assert "reason-design_decision" in resp.text


@pytest.mark.asyncio
async def test_escalation_queue_shows_context_snapshot(client):
    """Context JSON is shown in an expandable details element.

    Invariant: when context_json is non-empty, a context snapshot section
    appears with the raw JSON.
    """
    context = json.dumps({"task": "implement auth", "iteration": 5})
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id, context_json=context)

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert "Context snapshot" in resp.text
    assert "implement auth" in resp.text


@pytest.mark.asyncio
async def test_escalation_queue_action_buttons_for_open(client):
    """Open escalations show resolve and dismiss buttons.

    Invariant: action buttons (resolve form + dismiss button) are present
    only for open escalations.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id)

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert "btn-resolve" in resp.text
    assert "btn-dismiss" in resp.text
    assert 'name="resolution"' in resp.text


@pytest.mark.asyncio
async def test_escalation_queue_hides_resolved(client):
    """Resolved escalations are excluded from the default view.

    Invariant: the default queue (without show_all) shows only open escalations.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id, status="resolved")

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert "escalation-card" not in resp.text
    assert "All clear" in resp.text


@pytest.mark.asyncio
async def test_escalation_queue_show_all_includes_resolved(client):
    """The show_all=1 parameter includes resolved and dismissed escalations.

    Invariant: when show_all is set, all escalations regardless of status
    are returned and displayed.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id, status="resolved")
    await _seed_escalation(pid, stage_id=stage_id, status="open")

    resp = await client.get("/escalations?show_all=1")
    assert resp.status_code == 200
    # Both cards should appear
    assert resp.text.count("escalation-card") >= 2
    assert "Show open only" in resp.text


@pytest.mark.asyncio
async def test_escalation_queue_multiple_escalations(client):
    """Multiple open escalations render as separate cards.

    Invariant: each escalation gets its own card element.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id, reason="max_iterations")
    await _seed_escalation(pid, stage_id=stage_id, reason="design_decision")
    await _seed_escalation(pid, stage_id=stage_id, reason="test_failure")

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert resp.text.count("escalation-card") >= 3


@pytest.mark.asyncio
async def test_escalation_queue_stat_counts(client):
    """Stat cards show correct open/resolved/dismissed counts.

    Invariant: the summary section shows counts that match the actual DB state.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id, status="open")
    await _seed_escalation(pid, stage_id=stage_id, status="open")
    await _seed_escalation(pid, stage_id=stage_id, status="resolved")

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    text = resp.text
    # Open count = 2, Resolved count = 1, Dismissed count = 0
    assert ">2<" in text  # open
    assert ">1<" in text  # resolved


@pytest.mark.asyncio
async def test_escalation_queue_no_stage(client):
    """Escalations without a stage_id still render (stage fields are optional).

    Invariant: LEFT JOIN on pipeline_stages means null stage info doesn't
    cause errors.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    await _seed_escalation(pid, stage_id=None)

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert "escalation-card" in resp.text


@pytest.mark.asyncio
async def test_escalation_queue_pipeline_status_shown(client):
    """The pipeline's current status is displayed in the card footer.

    Invariant: each escalation card shows the pipeline status for context.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="needs_attention")
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id)

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert "needs_attention" in resp.text


@pytest.mark.asyncio
async def test_escalation_queue_filter_toggle(client):
    """The filter link toggles between open-only and show-all views.

    Invariant: the default view shows a link to show_all, and the show_all
    view shows a link back to open-only.
    """
    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert "show_all=1" in resp.text
    assert "Show open only" not in resp.text

    resp2 = await client.get("/escalations?show_all=1")
    assert resp2.status_code == 200
    assert "Show open only" in resp2.text


# ---------------------------------------------------------------------------
# Tests — resolve action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_escalation(client):
    """POST /escalations/{id}/resolve sets status=resolved with resolution text.

    Invariant: after resolution, the escalation moves from open to resolved
    and stores the human's decision.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    esc_id = await _seed_escalation(pid, stage_id=stage_id)

    resp = await client.post(
        f"/escalations/{esc_id}/resolve",
        data={"resolution": "Use PostgreSQL for refresh tokens"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    # Verify in DB
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status, resolution, resolved_at FROM escalations WHERE id=%s",
            (esc_id,),
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["status"] == "resolved"
        assert row["resolution"] == "Use PostgreSQL for refresh tokens"
        assert row["resolved_at"] is not None


@pytest.mark.asyncio
async def test_resolve_escalation_redirects_to_queue(client):
    """Resolving an escalation redirects back to the escalation queue.

    Invariant: POST endpoints use 303 redirect to prevent form resubmission.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    esc_id = await _seed_escalation(pid, stage_id=stage_id)

    resp = await client.post(
        f"/escalations/{esc_id}/resolve",
        data={"resolution": "approved"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/escalations"


@pytest.mark.asyncio
async def test_resolve_only_affects_open(client):
    """Resolving an already-resolved escalation is a no-op.

    Invariant: the WHERE clause includes status='open', so resolved/dismissed
    escalations cannot be re-resolved.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    esc_id = await _seed_escalation(pid, stage_id=stage_id, status="resolved")

    resp = await client.post(
        f"/escalations/{esc_id}/resolve",
        data={"resolution": "should not apply"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT resolution FROM escalations WHERE id=%s", (esc_id,),
        )
        row = await cur.fetchone()
        assert row is not None
        # Resolution should remain None (original state) because status was not 'open'
        assert row["resolution"] is None


# ---------------------------------------------------------------------------
# Tests — dismiss action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dismiss_escalation(client):
    """POST /escalations/{id}/dismiss sets status=dismissed without resolution text.

    Invariant: dismissal closes the escalation without storing a resolution.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    esc_id = await _seed_escalation(pid, stage_id=stage_id)

    resp = await client.post(
        f"/escalations/{esc_id}/dismiss",
        follow_redirects=True,
    )
    assert resp.status_code == 200

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status, resolution, resolved_at FROM escalations WHERE id=%s",
            (esc_id,),
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["status"] == "dismissed"
        assert row["resolution"] is None
        assert row["resolved_at"] is not None


@pytest.mark.asyncio
async def test_dismiss_escalation_redirects(client):
    """Dismissing an escalation redirects back to the queue.

    Invariant: POST endpoints use 303 redirect to prevent form resubmission.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    esc_id = await _seed_escalation(pid, stage_id=stage_id)

    resp = await client.post(
        f"/escalations/{esc_id}/dismiss",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/escalations"


@pytest.mark.asyncio
async def test_dismiss_only_affects_open(client):
    """Dismissing a non-open escalation is a no-op.

    Invariant: the WHERE clause includes status='open'.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    esc_id = await _seed_escalation(pid, stage_id=stage_id, status="dismissed")

    resp = await client.post(
        f"/escalations/{esc_id}/dismiss",
        follow_redirects=True,
    )
    assert resp.status_code == 200

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT resolved_at FROM escalations WHERE id=%s", (esc_id,),
        )
        row = await cur.fetchone()
        assert row is not None
        # resolved_at should remain None since it was already dismissed (not open)
        assert row["resolved_at"] is None


# ---------------------------------------------------------------------------
# Tests — resolved escalation display
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolved_escalation_shows_resolution_text(client):
    """Resolved escalations display the resolution text in show_all mode.

    Invariant: the resolution field is visible for resolved escalations.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    esc_id = await _seed_escalation(pid, stage_id=stage_id)

    # Resolve it first
    await client.post(
        f"/escalations/{esc_id}/resolve",
        data={"resolution": "Use Redis for caching"},
        follow_redirects=True,
    )

    resp = await client.get("/escalations?show_all=1")
    assert resp.status_code == 200
    assert "Use Redis for caching" in resp.text


@pytest.mark.asyncio
async def test_resolved_escalation_no_action_buttons(client):
    """Resolved escalations do not show resolve/dismiss buttons.

    Invariant: action buttons appear only for open escalations.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id, status="resolved")

    resp = await client.get("/escalations?show_all=1")
    assert resp.status_code == 200
    assert "escalation-card" in resp.text
    assert "btn-resolve" not in resp.text
    assert "btn-dismiss" not in resp.text


@pytest.mark.asyncio
async def test_escalation_links_to_pipeline(client):
    """Escalation card title links to the pipeline detail page.

    Invariant: the pipeline name is a clickable link to /pipelines/{id}.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id)

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert f'href="/pipelines/{pid}"' in resp.text


@pytest.mark.asyncio
async def test_escalation_ordering_newest_first(client):
    """Escalations are ordered newest first (DESC by created_at).

    Invariant: the most recently created escalation appears first in the list.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id, reason="max_iterations")
    await _seed_escalation(pid, stage_id=stage_id, reason="design_decision")

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    text = resp.text
    # design_decision was inserted second, should appear first (newest first)
    pos_design = text.index("design decision")
    pos_max = text.index("max iterations")
    assert pos_design < pos_max


@pytest.mark.asyncio
async def test_escalation_from_different_pipelines(client):
    """Escalations from multiple pipelines render with respective pipeline names.

    Invariant: cards show the correct pipeline def name for each escalation.
    """
    repo_id = await _seed_repo()
    def1 = await _seed_pipeline_def(name="pipeline-alpha")
    def2 = await _seed_pipeline_def(name="pipeline-beta")
    pid1 = await _seed_pipeline(repo_id, def1, clone_path="/tmp/c1")
    pid2 = await _seed_pipeline(repo_id, def2, clone_path="/tmp/c2")
    s1 = await _seed_stage(pid1)
    s2 = await _seed_stage(pid2)
    await _seed_escalation(pid1, stage_id=s1)
    await _seed_escalation(pid2, stage_id=s2)

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert "pipeline-alpha" in resp.text
    assert "pipeline-beta" in resp.text


@pytest.mark.asyncio
async def test_escalation_invalid_context_json(client):
    """Escalation with invalid JSON in context_json renders without error.

    Invariant: malformed context_json is handled gracefully by _fetch_escalation_data.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id, context_json="not-valid-json")

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert "escalation-card" in resp.text


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


@hyp_settings(max_examples=20, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    reason=st.sampled_from([
        "max_iterations", "review_divergence", "test_failure",
        "agent_error", "design_decision", "context_exhausted",
    ]),
)
@pytest.mark.asyncio
async def test_every_reason_renders_as_badge(initialized_db, reason):
    """Property: all valid escalation reason types render as human-readable badges.

    Invariant: for all reason ∈ spec reasons, GET /escalations returns 200 and
    the badge text (underscores replaced with spaces) is present.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        uid = uuid.uuid4().hex[:12]
        repo_id = await _seed_repo(
            name=f"prop-{uid}", local_path=f"/tmp/prop-{uid}"
        )
        def_id = await _seed_pipeline_def(name=f"prop-def-{uid}")
        pid = await _seed_pipeline(
            repo_id, def_id, clone_path=f"/tmp/prop-c-{uid}"
        )
        stage_id = await _seed_stage(pid)
        await _seed_escalation(pid, stage_id=stage_id, reason=reason)

        resp = await c.get("/escalations")
        assert resp.status_code == 200
        assert reason.replace("_", " ") in resp.text


@hyp_settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    resolution_text=st.text(
        alphabet=st.characters(
            categories=("L", "N", "Z", "P"), exclude_characters="\r"
        ),
        min_size=1,
        max_size=200,
    ),
)
@pytest.mark.asyncio
async def test_resolve_roundtrips_resolution_text(initialized_db, resolution_text):
    """Property: resolution text round-trips through the resolve endpoint.

    Invariant: for all valid resolution strings, POST resolve stores the
    exact text in the DB.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        uid = uuid.uuid4().hex[:12]
        repo_id = await _seed_repo(
            name=f"rt-{uid}", local_path=f"/tmp/rt-{uid}"
        )
        def_id = await _seed_pipeline_def(name=f"rt-def-{uid}")
        pid = await _seed_pipeline(
            repo_id, def_id, clone_path=f"/tmp/rt-c-{uid}"
        )
        stage_id = await _seed_stage(pid)
        esc_id = await _seed_escalation(pid, stage_id=stage_id, status="open")

        await c.post(
            f"/escalations/{esc_id}/resolve",
            data={"resolution": resolution_text},
            follow_redirects=False,
        )

        pool = get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT resolution FROM escalations WHERE id = %s", (esc_id,),
            )
            row = await cur.fetchone()
            assert row is not None
            assert row["resolution"] == resolution_text


@hyp_settings(max_examples=10, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    status=st.sampled_from(["open", "resolved", "dismissed"]),
)
@pytest.mark.asyncio
async def test_status_filter_consistency(initialized_db, status):
    """Property: default view shows only open; show_all shows all statuses.

    Invariant: for any escalation status, the default view excludes non-open
    and show_all=1 includes it.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        uid = uuid.uuid4().hex[:12]
        repo_id = await _seed_repo(
            name=f"sf-{uid}", local_path=f"/tmp/sf-{uid}"
        )
        def_id = await _seed_pipeline_def(name=f"sf-def-{uid}")
        pid = await _seed_pipeline(
            repo_id, def_id, clone_path=f"/tmp/sf-c-{uid}"
        )
        stage_id = await _seed_stage(pid)
        await _seed_escalation(pid, stage_id=stage_id, status=status)

        # Show all view always includes the escalation
        resp_all = await c.get("/escalations?show_all=1")
        assert resp_all.status_code == 200
        assert "escalation-card" in resp_all.text

        # Default view only shows open
        resp_default = await c.get("/escalations")
        assert resp_default.status_code == 200
        if status == "open":
            assert "escalation-card" in resp_default.text


# ---------------------------------------------------------------------------
# Tests — pause/kill pipeline action buttons on escalation cards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_card_shows_pause_kill_for_running_pipeline(client):
    """Open escalation for a running pipeline shows Pause and Kill buttons.

    Invariant: when the associated pipeline is running, both pause and kill
    pipeline action buttons are present on the escalation card.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id)

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert "btn-action-pause" in resp.text
    assert "Pause Pipeline" in resp.text
    assert "btn-action-kill" in resp.text
    assert "Kill Pipeline" in resp.text
    assert f"/pipelines/{pid}/pause" in resp.text
    assert f"/pipelines/{pid}/kill" in resp.text


@pytest.mark.asyncio
async def test_escalation_card_shows_kill_only_for_paused_pipeline(client):
    """Open escalation for a paused pipeline shows Kill but not Pause.

    Invariant: a paused pipeline can be killed but pausing again is a no-op,
    so only the Kill button appears.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="paused")
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id)

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert "btn-action-pause" not in resp.text
    assert "btn-action-kill" in resp.text
    assert "Kill Pipeline" in resp.text
    assert f"/pipelines/{pid}/kill" in resp.text


@pytest.mark.asyncio
async def test_escalation_card_shows_kill_for_needs_attention_pipeline(client):
    """Open escalation for a needs_attention pipeline shows Kill but not Pause.

    Invariant: a pipeline that needs attention can be killed to abort the run.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="needs_attention")
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id)

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert "btn-action-pause" not in resp.text
    assert "btn-action-kill" in resp.text


@pytest.mark.asyncio
async def test_escalation_card_shows_kill_for_cancel_requested_pipeline(client):
    """Open escalation for a cancel_requested pipeline shows Kill.

    Invariant: a pipeline awaiting graceful cancel can be force-killed.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="cancel_requested")
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id)

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert "btn-action-pause" not in resp.text
    assert "btn-action-kill" in resp.text


@pytest.mark.asyncio
async def test_escalation_card_no_pipeline_actions_for_terminal_status(client):
    """Open escalation for a completed pipeline shows no pause/kill buttons.

    Invariant: terminal pipeline states (completed, cancelled, killed, failed)
    do not show lifecycle action buttons since no action is meaningful.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="completed")
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id)

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert "btn-action-pause" not in resp.text
    assert "btn-action-kill" not in resp.text


@pytest.mark.asyncio
async def test_escalation_card_no_pipeline_actions_for_pending_status(client):
    """Open escalation for a pending pipeline shows no pause/kill buttons.

    Invariant: a pending pipeline has not started, so pause/kill are not offered.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="pending")
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id)

    resp = await client.get("/escalations")
    assert resp.status_code == 200
    assert "btn-action-pause" not in resp.text
    assert "btn-action-kill" not in resp.text


@pytest.mark.asyncio
async def test_resolved_escalation_no_pipeline_actions(client):
    """Resolved escalations show no pipeline action buttons.

    Invariant: only open escalations display any action buttons including
    pipeline lifecycle controls.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id, status="running")
    stage_id = await _seed_stage(pid)
    await _seed_escalation(pid, stage_id=stage_id, status="resolved")

    resp = await client.get("/escalations?show_all=1")
    assert resp.status_code == 200
    assert "escalation-card" in resp.text
    assert "btn-action-pause" not in resp.text
    assert "btn-action-kill" not in resp.text


@hyp_settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    pipeline_status=st.sampled_from([
        "running", "paused", "needs_attention", "cancel_requested",
        "completed", "cancelled", "killed", "failed", "pending",
    ]),
)
@pytest.mark.asyncio
async def test_pause_button_only_for_running_pipeline(initialized_db, pipeline_status):
    """Property: the Pause Pipeline button appears only when the pipeline is running.

    Invariant: for all pipeline statuses, the pause action URL for this specific
    pipeline is present iff the pipeline status is 'running'.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        uid = uuid.uuid4().hex[:12]
        repo_id = await _seed_repo(
            name=f"pa-{uid}", local_path=f"/tmp/pa-{uid}"
        )
        def_id = await _seed_pipeline_def(name=f"pa-def-{uid}")
        pid = await _seed_pipeline(
            repo_id, def_id, status=pipeline_status,
            clone_path=f"/tmp/pa-c-{uid}",
        )
        stage_id = await _seed_stage(pid)
        await _seed_escalation(pid, stage_id=stage_id)

        resp = await c.get("/escalations")
        assert resp.status_code == 200
        pause_url = f"/pipelines/{pid}/pause"
        if pipeline_status == "running":
            assert pause_url in resp.text
        else:
            assert pause_url not in resp.text


@hyp_settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    pipeline_status=st.sampled_from([
        "running", "paused", "needs_attention", "cancel_requested",
        "completed", "cancelled", "killed", "failed", "pending",
    ]),
)
@pytest.mark.asyncio
async def test_kill_button_only_for_active_pipeline(initialized_db, pipeline_status):
    """Property: the Kill Pipeline button appears only for active pipeline states.

    Invariant: for all pipeline statuses, the kill action URL for this specific
    pipeline is present iff the pipeline status is in
    {running, paused, needs_attention, cancel_requested}.
    """
    killable = {"running", "paused", "needs_attention", "cancel_requested"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        uid = uuid.uuid4().hex[:12]
        repo_id = await _seed_repo(
            name=f"ka-{uid}", local_path=f"/tmp/ka-{uid}"
        )
        def_id = await _seed_pipeline_def(name=f"ka-def-{uid}")
        pid = await _seed_pipeline(
            repo_id, def_id, status=pipeline_status,
            clone_path=f"/tmp/ka-c-{uid}",
        )
        stage_id = await _seed_stage(pid)
        await _seed_escalation(pid, stage_id=stage_id)

        resp = await c.get("/escalations")
        assert resp.status_code == 200
        kill_url = f"/pipelines/{pid}/kill"
        if pipeline_status in killable:
            assert kill_url in resp.text
        else:
            assert kill_url not in resp.text
