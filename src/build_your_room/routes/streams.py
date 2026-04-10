"""SSE streaming routes — pipeline-scoped and session-scoped log streams.

Both endpoints use the LogBuffer pub/sub pattern for real-time event delivery
via Server-Sent Events (SSE). The pipeline stream delivers all log messages
for a pipeline. The session stream delivers the pipeline stream filtered to
a specific session (looked up by pipeline_id from agent_sessions).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from build_your_room.db import get_pool

router = APIRouter()


@router.get("/pipelines/{pipeline_id}/stream")
async def pipeline_stream(pipeline_id: int):
    """SSE log stream scoped to a pipeline.

    Replays historical messages first, then streams new ones in real time.
    The stream closes when the LogBuffer signals completion (pipeline terminal).
    """
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM pipelines WHERE id = %s", (pipeline_id,)
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Pipeline not found")

    from build_your_room.main import log_buffer

    async def event_generator():
        # Replay history first
        for msg in log_buffer.get_history(pipeline_id):
            yield {"event": "log", "data": msg}

        # Then stream live updates
        async for msg in log_buffer.subscribe(pipeline_id):
            yield {"event": "log", "data": msg}

    return EventSourceResponse(event_generator())


@router.get("/sessions/{session_id}/stream")
async def session_stream(session_id: int):
    """SSE log stream scoped to an agent session.

    Looks up the session's pipeline_id and subscribes to that pipeline's
    log stream. All pipeline-level messages are delivered (the session
    context helps the client filter as needed).
    """
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT s.id, ps.pipeline_id "
            "FROM agent_sessions s "
            "JOIN pipeline_stages ps ON ps.id = s.pipeline_stage_id "
            "WHERE s.id = %s",
            (session_id,),
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        if row is None:
            raise HTTPException(status_code=404, detail="Session not found")

    pipeline_id = row["pipeline_id"]

    from build_your_room.main import log_buffer

    async def event_generator():
        # Replay history
        for msg in log_buffer.get_history(pipeline_id):
            yield {"event": "log", "data": msg}

        # Stream live
        async for msg in log_buffer.subscribe(pipeline_id):
            yield {"event": "log", "data": msg}

    return EventSourceResponse(event_generator())
