"""Tests for SSE streaming routes — pipeline-scoped and session-scoped log streams.

Covers:
  GET /pipelines/{id}/stream — pipeline-scoped SSE
  GET /sessions/{id}/stream — session-scoped SSE
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from build_your_room.db import get_pool
from build_your_room.main import app, log_buffer
from build_your_room.streaming import LogBuffer


@pytest.fixture(autouse=True)
def _reset_log_buffer():
    """Reset the global LogBuffer between tests to prevent state leaking."""
    log_buffer._history.clear()
    log_buffer._subscribers.clear()
    log_buffer._closed.clear()
    yield
    log_buffer._history.clear()
    log_buffer._subscribers.clear()
    log_buffer._closed.clear()


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


async def _seed_repo(name: str | None = None) -> int:
    pool = get_pool()
    name = name or f"repo-{uuid.uuid4().hex[:8]}"
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO repos (name, local_path) VALUES (%s, %s) RETURNING id",
            (name, "/tmp/test-repo"),
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


async def _seed_stage(pipeline_id: int, stage_key: str = "spec_author") -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipeline_stages "
            "(pipeline_id, stage_key, stage_type, agent_type, status, max_iterations) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (pipeline_id, stage_key, "spec_author", "claude", "running", 1),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_session(stage_id: int) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO agent_sessions "
            "(pipeline_stage_id, session_type, status, started_at) "
            "VALUES (%s, %s, %s, now()) RETURNING id",
            (stage_id, "claude_sdk", "running"),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


# ---------------------------------------------------------------------------
# Pipeline stream tests
# ---------------------------------------------------------------------------


class TestPipelineStream:
    """Tests for GET /pipelines/{id}/stream SSE endpoint."""

    @pytest.mark.asyncio
    async def test_pipeline_not_found(self, client: AsyncClient):
        """Returns 404 for nonexistent pipeline."""
        resp = await client.get("/pipelines/99999/stream")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_stream_returns_sse_content_type(self, client: AsyncClient):
        """Stream response has text/event-stream content type."""
        repo_id = await _seed_repo()
        def_id = await _seed_pipeline_def()
        pipeline_id = await _seed_pipeline(repo_id, def_id)

        # Pre-load a message and close so the stream terminates
        log_buffer.append(pipeline_id, "test message")
        log_buffer.close(pipeline_id)

        resp = await client.get("/pipelines/{}/stream".format(pipeline_id))
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_stream_replays_history(self, client: AsyncClient):
        """Stream replays historical messages as SSE events."""
        repo_id = await _seed_repo()
        def_id = await _seed_pipeline_def()
        pipeline_id = await _seed_pipeline(repo_id, def_id)

        log_buffer.append(pipeline_id, "historical msg 1")
        log_buffer.append(pipeline_id, "historical msg 2")
        log_buffer.close(pipeline_id)

        resp = await client.get("/pipelines/{}/stream".format(pipeline_id))
        assert resp.status_code == 200
        body = resp.text
        assert "historical msg 1" in body
        assert "historical msg 2" in body

    @pytest.mark.asyncio
    async def test_stream_receives_live_messages(self, client: AsyncClient):
        """Stream delivers messages appended concurrently via LogBuffer.

        HTTPX ASGI transport buffers the entire response, so we pre-stage a
        delayed append + close and let the stream run to completion.
        """
        repo_id = await _seed_repo()
        def_id = await _seed_pipeline_def()
        pipeline_id = await _seed_pipeline(repo_id, def_id)

        async def delayed_append():
            await asyncio.sleep(0.05)
            log_buffer.append(pipeline_id, "live msg")
            log_buffer.close(pipeline_id)

        asyncio.create_task(delayed_append())

        resp = await client.get(f"/pipelines/{pipeline_id}/stream")
        assert resp.status_code == 200
        assert "live msg" in resp.text

    @pytest.mark.asyncio
    async def test_stream_closes_on_buffer_close(self, client: AsyncClient):
        """Stream terminates when the LogBuffer signals close."""
        repo_id = await _seed_repo()
        def_id = await _seed_pipeline_def()
        pipeline_id = await _seed_pipeline(repo_id, def_id)

        log_buffer.append(pipeline_id, "msg before close")
        log_buffer.close(pipeline_id)

        resp = await client.get("/pipelines/{}/stream".format(pipeline_id))
        assert resp.status_code == 200
        # The response completes (doesn't hang), proving the stream closed
        assert "msg before close" in resp.text

    @pytest.mark.asyncio
    async def test_stream_event_format(self, client: AsyncClient):
        """SSE events use the 'log' event type."""
        repo_id = await _seed_repo()
        def_id = await _seed_pipeline_def()
        pipeline_id = await _seed_pipeline(repo_id, def_id)

        log_buffer.append(pipeline_id, "formatted msg")
        log_buffer.close(pipeline_id)

        resp = await client.get("/pipelines/{}/stream".format(pipeline_id))
        body = resp.text
        assert "event: log" in body
        assert "data: formatted msg" in body

    @pytest.mark.asyncio
    async def test_stream_empty_history(self, client: AsyncClient):
        """Stream works for a pipeline with no log history."""
        repo_id = await _seed_repo()
        def_id = await _seed_pipeline_def()
        pipeline_id = await _seed_pipeline(repo_id, def_id)

        log_buffer.close(pipeline_id)

        resp = await client.get("/pipelines/{}/stream".format(pipeline_id))
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Session stream tests
# ---------------------------------------------------------------------------


class TestSessionStream:
    """Tests for GET /sessions/{id}/stream SSE endpoint."""

    @pytest.mark.asyncio
    async def test_session_not_found(self, client: AsyncClient):
        """Returns 404 for nonexistent session."""
        resp = await client.get("/sessions/99999/stream")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_session_stream_returns_sse(self, client: AsyncClient):
        """Session stream returns SSE content type."""
        repo_id = await _seed_repo()
        def_id = await _seed_pipeline_def()
        pipeline_id = await _seed_pipeline(repo_id, def_id)
        stage_id = await _seed_stage(pipeline_id)
        session_id = await _seed_session(stage_id)

        log_buffer.append(pipeline_id, "[spec_author] test")
        log_buffer.close(pipeline_id)

        resp = await client.get(f"/sessions/{session_id}/stream")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_session_stream_receives_pipeline_messages(
        self, client: AsyncClient
    ):
        """Session stream delivers messages from the parent pipeline's buffer."""
        repo_id = await _seed_repo()
        def_id = await _seed_pipeline_def()
        pipeline_id = await _seed_pipeline(repo_id, def_id)
        stage_id = await _seed_stage(pipeline_id)
        session_id = await _seed_session(stage_id)

        log_buffer.append(pipeline_id, "[spec_author] session msg")
        log_buffer.close(pipeline_id)

        resp = await client.get(f"/sessions/{session_id}/stream")
        assert "[spec_author] session msg" in resp.text

    @pytest.mark.asyncio
    async def test_session_stream_live_delivery(self, client: AsyncClient):
        """Session stream receives live messages via delayed LogBuffer append."""
        repo_id = await _seed_repo()
        def_id = await _seed_pipeline_def()
        pipeline_id = await _seed_pipeline(repo_id, def_id)
        stage_id = await _seed_stage(pipeline_id)
        session_id = await _seed_session(stage_id)

        async def delayed_append():
            await asyncio.sleep(0.05)
            log_buffer.append(pipeline_id, "live session msg")
            log_buffer.close(pipeline_id)

        asyncio.create_task(delayed_append())

        resp = await client.get(f"/sessions/{session_id}/stream")
        assert resp.status_code == 200
        assert "live session msg" in resp.text


# ---------------------------------------------------------------------------
# Pipeline detail template SSE integration
# ---------------------------------------------------------------------------


class TestPipelineDetailSSE:
    """Tests that the pipeline detail page includes SSE script for active pipelines."""

    @pytest.mark.asyncio
    async def test_running_pipeline_has_eventsource(self, client: AsyncClient):
        """Running pipeline detail page includes EventSource script."""
        repo_id = await _seed_repo()
        def_id = await _seed_pipeline_def()
        pipeline_id = await _seed_pipeline(repo_id, def_id, status="running")

        resp = await client.get(f"/pipelines/{pipeline_id}")
        assert resp.status_code == 200
        body = resp.text
        assert "EventSource" in body
        assert f"/pipelines/{pipeline_id}/stream" in body

    @pytest.mark.asyncio
    async def test_terminal_pipeline_no_eventsource(self, client: AsyncClient):
        """Completed pipeline detail page does not include EventSource."""
        repo_id = await _seed_repo()
        def_id = await _seed_pipeline_def()
        pipeline_id = await _seed_pipeline(
            repo_id, def_id, status="completed"
        )

        resp = await client.get(f"/pipelines/{pipeline_id}")
        assert resp.status_code == 200
        body = resp.text
        assert "EventSource" not in body

    @pytest.mark.asyncio
    async def test_paused_pipeline_has_eventsource(self, client: AsyncClient):
        """Paused pipeline (non-terminal) still gets EventSource for updates."""
        repo_id = await _seed_repo()
        def_id = await _seed_pipeline_def()
        pipeline_id = await _seed_pipeline(repo_id, def_id, status="paused")

        resp = await client.get(f"/pipelines/{pipeline_id}")
        assert resp.status_code == 200
        body = resp.text
        assert "EventSource" in body


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestStreamProperties:
    """Property-based tests for streaming behavior."""

    @pytest.mark.asyncio
    async def test_multiple_pipelines_isolated(self, client: AsyncClient):
        """Messages for one pipeline do not appear in another's stream."""
        repo_id = await _seed_repo()
        def_id = await _seed_pipeline_def()
        pid_a = await _seed_pipeline(repo_id, def_id)
        pid_b = await _seed_pipeline(repo_id, def_id)

        log_buffer.append(pid_a, "msg for A")
        log_buffer.append(pid_b, "msg for B")
        log_buffer.close(pid_a)
        log_buffer.close(pid_b)

        resp_a = await client.get(f"/pipelines/{pid_a}/stream")
        resp_b = await client.get(f"/pipelines/{pid_b}/stream")

        assert "msg for A" in resp_a.text
        assert "msg for B" not in resp_a.text
        assert "msg for B" in resp_b.text
        assert "msg for A" not in resp_b.text

    @pytest.mark.asyncio
    async def test_stream_message_ordering(self, client: AsyncClient):
        """Messages are delivered in the order they were appended."""
        repo_id = await _seed_repo()
        def_id = await _seed_pipeline_def()
        pipeline_id = await _seed_pipeline(repo_id, def_id)

        for i in range(5):
            log_buffer.append(pipeline_id, f"msg-{i}")
        log_buffer.close(pipeline_id)

        resp = await client.get(f"/pipelines/{pipeline_id}/stream")
        body = resp.text
        positions = [body.index(f"msg-{i}") for i in range(5)]
        assert positions == sorted(positions), "Messages out of order"

    @pytest.mark.asyncio
    async def test_concurrent_subscribers_both_receive(self):
        """Two concurrent subscribers to the same LogBuffer channel both receive.

        Tests the underlying LogBuffer rather than HTTP streaming, since HTTPX
        ASGI transport doesn't support true concurrent SSE connections.
        """
        buf = LogBuffer()
        results: dict[str, list[str]] = {"a": [], "b": []}

        async def consume(key: str):
            async for msg in buf.subscribe(42):
                results[key].append(msg)

        task_a = asyncio.create_task(consume("a"))
        task_b = asyncio.create_task(consume("b"))
        await asyncio.sleep(0.01)

        buf.append(42, "shared msg")
        buf.close(42)

        await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=2.0)

        assert "shared msg" in results["a"]
        assert "shared msg" in results["b"]
