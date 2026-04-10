"""Tests for the stage detail HTMX partial — sessions, logs, artifacts, review feedback."""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

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


async def _seed_repo(name: str = "my-project", local_path: str = "/tmp/my-project") -> int:
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


async def _seed_pipeline_def(name: str = "test-def", graph: str | None = None) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipeline_defs (name, stage_graph_json) VALUES (%s, %s) RETURNING id",
            (name, graph or _FULL_GRAPH),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_pipeline(repo_id: int, def_id: int, status: str = "running") -> int:
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


# ---------------------------------------------------------------------------
# Tests — 404 and basic rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_detail_404_missing_pipeline(client):
    """GET stage detail returns 404 when pipeline does not exist.

    Invariant: non-existent pipeline/stage combinations yield 404.
    """
    resp = await client.get("/pipelines/99999/stages/1")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_stage_detail_404_missing_stage(client):
    """GET stage detail returns 404 when stage does not exist.

    Invariant: valid pipeline but non-existent stage ID yields 404.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)

    resp = await client.get(f"/pipelines/{pid}/stages/99999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_stage_detail_404_wrong_pipeline(client):
    """GET stage detail returns 404 when stage belongs to a different pipeline.

    Invariant: stage must belong to the specified pipeline.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid1 = await _seed_pipeline(repo_id, def_id)
    pid2 = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid1)

    resp = await client.get(f"/pipelines/{pid2}/stages/{sid}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests — empty stage (no sessions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_detail_empty_stage(client):
    """Stage detail renders correctly with no sessions.

    Invariant: a stage with no sessions shows the stage info and empty state.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid, stage_key="spec_author", stage_type="spec_author")

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200
    assert "spec_author" in resp.text
    assert "No sessions" in resp.text


# ---------------------------------------------------------------------------
# Tests — sessions with logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_detail_sessions_displayed(client):
    """Stage detail shows session type, status, cost, and tokens.

    Invariant: all sessions for the stage are listed with their metadata.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid)
    await _seed_session(sid, status="completed", cost_usd=1.23,
                        token_input=500, token_output=200, context_usage_pct=45.0)

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200
    assert "claude_sdk" in resp.text
    assert "completed" in resp.text
    assert "$1.2300" in resp.text
    assert "700 tokens" in resp.text
    assert "45%" in resp.text


@pytest.mark.asyncio
async def test_stage_detail_session_logs(client):
    """Stage detail includes per-session log entries.

    Invariant: session logs are rendered inside the session's expandable details.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid)
    sess_id = await _seed_session(sid)
    await _seed_log(sess_id, "assistant_message", "Hello from the agent")
    await _seed_log(sess_id, "tool_use", "Reading file.py")

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200
    assert "Hello from the agent" in resp.text
    assert "Reading file.py" in resp.text
    assert "Logs (2 entries)" in resp.text


@pytest.mark.asyncio
async def test_stage_detail_multiple_sessions(client):
    """Stage detail shows all sessions when multiple exist.

    Invariant: session count in header matches actual sessions rendered.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid)
    await _seed_session(sid, status="completed", session_type="claude_sdk")
    await _seed_session(sid, status="running", session_type="codex_app_server")

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200
    assert "Sessions (2)" in resp.text
    assert "claude_sdk" in resp.text
    assert "codex_app_server" in resp.text


# ---------------------------------------------------------------------------
# Tests — review feedback history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_detail_review_feedback(client):
    """Stage detail shows review feedback entries separately.

    Invariant: session_logs with event_type='review_feedback' appear
    in the dedicated review feedback section.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid)
    sess_id = await _seed_session(sid)
    await _seed_log(sess_id, "review_feedback", "Spec missing error handling section")
    await _seed_log(sess_id, "assistant_message", "I will add error handling")

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200
    assert "Review Feedback (1)" in resp.text
    assert "Spec missing error handling section" in resp.text


@pytest.mark.asyncio
async def test_stage_detail_no_review_feedback_hides_section(client):
    """When there is no review feedback, the section is not rendered.

    Invariant: review feedback section only appears when feedback exists.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid)
    sess_id = await _seed_session(sid)
    await _seed_log(sess_id, "assistant_message", "Just a normal message")

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200
    assert "Review Feedback" not in resp.text


# ---------------------------------------------------------------------------
# Tests — output artifact rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_detail_artifact_content(client):
    """Stage detail reads and renders output artifact file content.

    Invariant: when output_artifact points to a readable file, its
    content is rendered in the artifact section.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("# My Spec\n\nThis is the spec content.")
        artifact_path = f.name

    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid, output_artifact=artifact_path)

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200
    assert "Output Artifact" in resp.text
    assert "This is the spec content." in resp.text
    assert artifact_path in resp.text

    Path(artifact_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_stage_detail_artifact_unreadable(client):
    """Stage detail handles missing artifact file gracefully.

    Invariant: when output_artifact path exists in DB but file is not
    readable, the path is shown with an 'unavailable' indicator.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid, output_artifact="/nonexistent/path/spec.md")

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200
    assert "Output Artifact" in resp.text
    assert "/nonexistent/path/spec.md" in resp.text
    assert "not readable" in resp.text


@pytest.mark.asyncio
async def test_stage_detail_no_artifact_hides_section(client):
    """When there is no output artifact, the section is not rendered.

    Invariant: artifact section only appears when output_artifact is set.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid, output_artifact=None)

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200
    assert "Output Artifact" not in resp.text


# ---------------------------------------------------------------------------
# Tests — context usage display
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_detail_context_usage_bar(client):
    """Stage detail shows inline context usage bar for sessions.

    Invariant: sessions with non-null context_usage_pct show a progress bar.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid)
    await _seed_session(sid, context_usage_pct=72.5)

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200
    assert "72%" in resp.text
    assert "Context:" in resp.text


@pytest.mark.asyncio
async def test_stage_detail_context_high_warning(client):
    """Stage detail marks context usage >80% with high-warning CSS class.

    Invariant: context usage above 80% gets the progress-ctx-high class.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid)
    await _seed_session(sid, context_usage_pct=85.0)

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200
    assert "progress-ctx-high" in resp.text


# ---------------------------------------------------------------------------
# Tests — escalation reason display
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_detail_escalation_reason(client):
    """Stage detail shows escalation reason when present.

    Invariant: stages with an escalation_reason display it prominently.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid, escalation_reason="max_iterations")

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200
    assert "max iterations" in resp.text


# ---------------------------------------------------------------------------
# Tests — stage info display
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_detail_entry_exit_revs(client):
    """Stage detail shows entry and exit revisions when present.

    Invariant: revision information is truncated to 12 chars and displayed.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(
        pid, entry_rev="abc123def456789", exit_rev="xyz987uvw654321",
    )

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200
    assert "abc123def456" in resp.text
    assert "xyz987uvw654" in resp.text


# ---------------------------------------------------------------------------
# Tests — HTMX integration in pipeline detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_detail_has_htmx_stage_tabs(client):
    """Pipeline detail page includes HTMX attributes on stage tab headers.

    Invariant: each stage tab header has hx-get pointing to the
    stage detail endpoint for that stage.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid)

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert f"hx-get=\"/pipelines/{pid}/stages/{sid}\"" in resp.text
    assert f"hx-target=\"#stage-detail-{sid}\"" in resp.text
    assert "click to expand" in resp.text


@pytest.mark.asyncio
async def test_pipeline_detail_stage_session_count_summary(client):
    """Pipeline detail stage tabs show session count summary.

    Invariant: each stage tab shows the number of sessions.
    """
    repo_id = await _seed_repo()
    def_id = await _seed_pipeline_def()
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid)
    await _seed_session(sid, status="completed")
    await _seed_session(sid, status="running")

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "2 sessions" in resp.text


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=10,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(event_types=st.lists(
    st.sampled_from([
        "assistant_message", "tool_use", "command_exec", "file_change",
        "error", "context_warning", "review_feedback", "escalation",
    ]),
    min_size=0,
    max_size=8,
))
@pytest.mark.asyncio
async def test_stage_detail_review_feedback_count_matches(client, event_types: list[str]):
    """The review feedback count matches actual review_feedback event_type logs.

    Invariant: the number shown in "Review Feedback (N)" equals the count
    of session_logs with event_type='review_feedback'.
    """
    uid = uuid.uuid4().hex[:8]
    repo_id = await _seed_repo(name=f"proj-{uid}", local_path=f"/tmp/{uid}")
    def_id = await _seed_pipeline_def(name=f"def-{uid}")
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid)
    sess_id = await _seed_session(sid)

    for et in event_types:
        await _seed_log(sess_id, et, f"log content for {et}")

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200

    expected_count = sum(1 for et in event_types if et == "review_feedback")
    if expected_count > 0:
        assert f"Review Feedback ({expected_count})" in resp.text
    else:
        assert "Review Feedback" not in resp.text


@hyp_settings(
    max_examples=10,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(ctx_pct=st.one_of(
    st.none(),
    st.integers(min_value=0, max_value=100).map(float),
))
@pytest.mark.asyncio
async def test_stage_detail_context_css_class_correct(client, ctx_pct: float | None):
    """Context usage CSS class matches the percentage threshold rules.

    Invariant: >80% => progress-ctx-high, >50% => progress-ctx-warn, else progress-ctx-ok.
    """
    uid = uuid.uuid4().hex[:8]
    repo_id = await _seed_repo(name=f"proj-{uid}", local_path=f"/tmp/{uid}")
    def_id = await _seed_pipeline_def(name=f"def-{uid}")
    pid = await _seed_pipeline(repo_id, def_id)
    sid = await _seed_stage(pid)
    await _seed_session(sid, context_usage_pct=ctx_pct)

    resp = await client.get(f"/pipelines/{pid}/stages/{sid}")
    assert resp.status_code == 200

    if ctx_pct is None:
        assert "Context:" not in resp.text
    elif ctx_pct > 80:
        assert "progress-ctx-high" in resp.text
    elif ctx_pct > 50:
        assert "progress-ctx-warn" in resp.text
    else:
        assert "progress-ctx-ok" in resp.text
