"""JSON API routes for programmatic access — pipelines, tasks, escalations."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from build_your_room.db import get_pool

router = APIRouter(prefix="/api")

# Status values that allow clone cleanup
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "killed"})


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CreatePipelineRequest(BaseModel):
    pipeline_def_id: int
    repo_id: int
    config_json: dict[str, Any] | None = None


class ResolveEscalationRequest(BaseModel):
    action: str  # "resolve" or "dismiss"
    resolution: str | None = None


# ---------------------------------------------------------------------------
# Pipeline endpoints
# ---------------------------------------------------------------------------


@router.get("/pipelines")
async def list_pipelines(
    status: str | None = None,
    repo_id: int | None = None,
) -> JSONResponse:
    """List pipelines with optional filtering by status or repo."""
    pool = get_pool()
    async with pool.connection() as conn:
        conditions: list[str] = []
        params: list[Any] = []

        if status:
            conditions.append("p.status = %s")
            params.append(status)
        if repo_id is not None:
            conditions.append("p.repo_id = %s")
            params.append(repo_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        cur = await conn.execute(
            "SELECT p.*, r.name AS repo_name, pd.name AS def_name "
            "FROM pipelines p "
            "JOIN repos r ON r.id = p.repo_id "
            "JOIN pipeline_defs pd ON pd.id = p.pipeline_def_id "
            f"{where} "
            "ORDER BY p.updated_at DESC",
            tuple(params),
        )
        rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

    return JSONResponse(content=_serialize_rows(rows))


@router.post("/pipelines")
async def create_pipeline(body: CreatePipelineRequest) -> JSONResponse:
    """Create a new pipeline and start it via the orchestrator."""
    pool = get_pool()
    async with pool.connection() as conn:
        # Validate pipeline_def exists
        cur = await conn.execute(
            "SELECT id FROM pipeline_defs WHERE id = %s", (body.pipeline_def_id,)
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Pipeline definition not found")

        # Validate repo exists
        cur = await conn.execute(
            "SELECT id, local_path FROM repos WHERE id = %s AND archived = 0",
            (body.repo_id,),
        )
        repo_row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        if repo_row is None:
            raise HTTPException(status_code=404, detail="Repo not found or archived")

        config = json.dumps(body.config_json or {})
        cur = await conn.execute(
            "INSERT INTO pipelines "
            "(pipeline_def_id, repo_id, clone_path, review_base_rev, status, config_json) "
            "VALUES (%s, %s, %s, %s, 'pending', %s) RETURNING *",
            (
                body.pipeline_def_id,
                body.repo_id,
                "",  # clone_path set by orchestrator on start
                "",  # review_base_rev set by orchestrator on start
                config,
            ),
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        assert row is not None
        await conn.commit()

    pipeline_id = row["id"]

    # Start the pipeline via the orchestrator if available
    from build_your_room.main import orchestrator

    if orchestrator is not None:
        await orchestrator.start_pipeline(pipeline_id)

    return JSONResponse(content=_serialize_row(row), status_code=201)


@router.get("/pipelines/{pipeline_id}/status")
async def pipeline_status(pipeline_id: int) -> JSONResponse:
    """Pipeline status summary with stage and HTN progress."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT p.*, r.name AS repo_name, pd.name AS def_name "
            "FROM pipelines p "
            "JOIN repos r ON r.id = p.repo_id "
            "JOIN pipeline_defs pd ON pd.id = p.pipeline_def_id "
            "WHERE p.id = %s",
            (pipeline_id,),
        )
        pipeline: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        if pipeline is None:
            raise HTTPException(status_code=404, detail="Pipeline not found")

        # Current stage
        cur = await conn.execute(
            "SELECT stage_key, stage_type, status, iteration, max_iterations "
            "FROM pipeline_stages WHERE pipeline_id = %s "
            "ORDER BY attempt DESC, id DESC LIMIT 1",
            (pipeline_id,),
        )
        stage: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]

        # HTN progress (primitive tasks)
        cur = await conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM htn_tasks "
            "WHERE pipeline_id = %s AND task_type = 'primitive' GROUP BY status",
            (pipeline_id,),
        )
        htn_rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
        htn: dict[str, int] = {r["status"]: r["cnt"] for r in htn_rows}
        htn_total = sum(htn.values())
        htn_completed = htn.get("completed", 0)

        # Total cost
        cur = await conn.execute(
            "SELECT COALESCE(SUM(s.cost_usd), 0) AS total_cost "
            "FROM agent_sessions s "
            "JOIN pipeline_stages ps ON ps.id = s.pipeline_stage_id "
            "WHERE ps.pipeline_id = %s",
            (pipeline_id,),
        )
        cost_row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]

        # Open escalations
        cur = await conn.execute(
            "SELECT COUNT(*) AS cnt FROM escalations "
            "WHERE pipeline_id = %s AND status = 'open'",
            (pipeline_id,),
        )
        esc_row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]

    return JSONResponse(content={
        "pipeline": _serialize_row(pipeline),
        "current_stage": _serialize_row(stage) if stage else None,
        "htn_progress": {
            "total": htn_total,
            "completed": htn_completed,
            "in_progress": htn.get("in_progress", 0),
            "ready": htn.get("ready", 0),
            "blocked": htn.get("blocked", 0),
            "failed": htn.get("failed", 0),
            "pct": round(htn_completed / htn_total * 100) if htn_total else 0,
        },
        "total_cost_usd": float(cost_row["total_cost"]) if cost_row else 0.0,
        "open_escalations": esc_row["cnt"] if esc_row else 0,
    })


@router.post("/pipelines/{pipeline_id}/cancel")
async def cancel_pipeline(pipeline_id: int) -> JSONResponse:
    """Graceful cancel — signals the cancel event and stops at next safe boundary."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, status FROM pipelines WHERE id = %s", (pipeline_id,)
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        if row is None:
            raise HTTPException(status_code=404, detail="Pipeline not found")
        if row["status"] not in ("running", "paused"):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot cancel pipeline in '{row['status']}' state",
            )

    from build_your_room.main import orchestrator

    if orchestrator is not None:
        await orchestrator.cancel_pipeline(pipeline_id)
    else:
        # Fallback: update DB directly when orchestrator is not running
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET status = 'cancel_requested', updated_at = now() "
                "WHERE id = %s AND status IN ('running', 'paused')",
                (pipeline_id,),
            )
            await conn.commit()

    return JSONResponse(content={"pipeline_id": pipeline_id, "status": "cancel_requested"})


@router.post("/pipelines/{pipeline_id}/kill")
async def kill_pipeline(pipeline_id: int) -> JSONResponse:
    """Force kill — immediately terminates live sessions."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, status FROM pipelines WHERE id = %s", (pipeline_id,)
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        if row is None:
            raise HTTPException(status_code=404, detail="Pipeline not found")
        if row["status"] in _TERMINAL_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"Pipeline already in terminal state '{row['status']}'",
            )

    from build_your_room.main import orchestrator

    if orchestrator is not None:
        await orchestrator.kill_pipeline(pipeline_id)

    return JSONResponse(content={"pipeline_id": pipeline_id, "status": "killed"})


# ---------------------------------------------------------------------------
# HTN task endpoints
# ---------------------------------------------------------------------------


@router.get("/pipelines/{pipeline_id}/tasks")
async def pipeline_tasks(pipeline_id: int) -> JSONResponse:
    """Full HTN task tree for a pipeline."""
    pool = get_pool()
    async with pool.connection() as conn:
        # Verify pipeline exists
        cur = await conn.execute(
            "SELECT id FROM pipelines WHERE id = %s", (pipeline_id,)
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Pipeline not found")

        cur = await conn.execute(
            "SELECT * FROM htn_tasks WHERE pipeline_id = %s "
            "ORDER BY ordering ASC, id ASC",
            (pipeline_id,),
        )
        tasks: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

        # Dependencies
        task_ids = [t["id"] for t in tasks]
        deps_by_task: dict[int, list[dict[str, Any]]] = {tid: [] for tid in task_ids}
        if task_ids:
            placeholders = ",".join(["%s"] * len(task_ids))
            cur = await conn.execute(
                "SELECT * FROM htn_task_deps "
                f"WHERE task_id IN ({placeholders})",
                tuple(task_ids),
            )
            dep_rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
            for d in dep_rows:
                deps_by_task[d["task_id"]].append({
                    "depends_on_task_id": d["depends_on_task_id"],
                    "dep_type": d["dep_type"],
                })

    # Build tree structure
    for t in tasks:
        t["dependencies"] = deps_by_task.get(t["id"], [])

    tree = _build_task_tree(tasks)
    return JSONResponse(content=_serialize_rows(tree))


@router.get("/pipelines/{pipeline_id}/tasks/progress")
async def pipeline_task_progress(pipeline_id: int) -> JSONResponse:
    """Task progress summary — counts by status for primitive tasks."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM pipelines WHERE id = %s", (pipeline_id,)
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Pipeline not found")

        cur = await conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM htn_tasks "
            "WHERE pipeline_id = %s AND task_type = 'primitive' GROUP BY status",
            (pipeline_id,),
        )
        rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

    counts: dict[str, int] = {r["status"]: r["cnt"] for r in rows}
    total = sum(counts.values())
    completed = counts.get("completed", 0)

    return JSONResponse(content={
        "pipeline_id": pipeline_id,
        "total": total,
        "completed": completed,
        "in_progress": counts.get("in_progress", 0),
        "ready": counts.get("ready", 0),
        "not_ready": counts.get("not_ready", 0),
        "blocked": counts.get("blocked", 0),
        "failed": counts.get("failed", 0),
        "skipped": counts.get("skipped", 0),
        "pct": round(completed / total * 100) if total else 0,
    })


@router.post("/pipelines/{pipeline_id}/cleanup")
async def cleanup_pipeline(pipeline_id: int) -> JSONResponse:
    """Delete a pipeline's clone directory."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, status, clone_path FROM pipelines WHERE id = %s",
            (pipeline_id,),
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]

    if row is None:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    if row["status"] not in _TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot clean up pipeline in '{row['status']}' state",
        )

    clone_path = Path(row["clone_path"]) if row["clone_path"] else None
    cleaned = False
    if clone_path and clone_path.exists():
        shutil.rmtree(clone_path)
        cleaned = True

    return JSONResponse(content={
        "pipeline_id": pipeline_id,
        "cleaned": cleaned,
        "clone_path": str(clone_path) if clone_path else None,
    })


# ---------------------------------------------------------------------------
# Escalation endpoints
# ---------------------------------------------------------------------------


@router.get("/escalations")
async def list_escalations(
    status: str | None = None,
) -> JSONResponse:
    """List escalations, optionally filtered by status."""
    pool = get_pool()
    async with pool.connection() as conn:
        if status:
            cur = await conn.execute(
                "SELECT e.*, p.status AS pipeline_status, r.name AS repo_name, "
                "  pd.name AS def_name, ps.stage_key, ps.stage_type "
                "FROM escalations e "
                "JOIN pipelines p ON p.id = e.pipeline_id "
                "JOIN repos r ON r.id = p.repo_id "
                "JOIN pipeline_defs pd ON pd.id = p.pipeline_def_id "
                "LEFT JOIN pipeline_stages ps ON ps.id = e.pipeline_stage_id "
                "WHERE e.status = %s "
                "ORDER BY e.created_at DESC",
                (status,),
            )
        else:
            cur = await conn.execute(
                "SELECT e.*, p.status AS pipeline_status, r.name AS repo_name, "
                "  pd.name AS def_name, ps.stage_key, ps.stage_type "
                "FROM escalations e "
                "JOIN pipelines p ON p.id = e.pipeline_id "
                "JOIN repos r ON r.id = p.repo_id "
                "JOIN pipeline_defs pd ON pd.id = p.pipeline_def_id "
                "LEFT JOIN pipeline_stages ps ON ps.id = e.pipeline_stage_id "
                "ORDER BY e.created_at DESC"
            )

        rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

    # Parse context_json for each escalation
    enriched = []
    for row in rows:
        ctx = {}
        if row.get("context_json"):
            try:
                ctx = json.loads(row["context_json"])
            except (json.JSONDecodeError, TypeError):
                ctx = {}
        enriched.append({**row, "context": ctx})

    return JSONResponse(content=_serialize_rows(enriched))


@router.post("/escalations/{escalation_id}")
async def resolve_or_dismiss_escalation(
    escalation_id: int,
    body: ResolveEscalationRequest,
) -> JSONResponse:
    """Resolve or dismiss an escalation."""
    if body.action not in ("resolve", "dismiss"):
        raise HTTPException(
            status_code=400, detail="action must be 'resolve' or 'dismiss'"
        )

    if body.action == "resolve" and not body.resolution:
        raise HTTPException(
            status_code=400, detail="resolution is required when action is 'resolve'"
        )

    pool = get_pool()
    now = datetime.now(timezone.utc).isoformat()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, status FROM escalations WHERE id = %s", (escalation_id,)
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        if row is None:
            raise HTTPException(status_code=404, detail="Escalation not found")
        if row["status"] != "open":
            raise HTTPException(
                status_code=409,
                detail=f"Escalation already '{row['status']}'",
            )

        new_status = "resolved" if body.action == "resolve" else "dismissed"
        await conn.execute(
            "UPDATE escalations SET status = %s, resolution = %s, "
            "resolved_at = %s WHERE id = %s",
            (new_status, body.resolution, now, escalation_id),
        )
        await conn.commit()

    return JSONResponse(content={
        "escalation_id": escalation_id,
        "status": new_status,
        "resolution": body.resolution,
    })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_task_tree(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Organize flat task list into a nested tree."""
    by_id: dict[int, dict[str, Any]] = {}
    for t in tasks:
        t["children"] = []
        by_id[t["id"]] = t

    roots: list[dict[str, Any]] = []
    for t in tasks:
        parent = t.get("parent_task_id")
        if parent and parent in by_id:
            by_id[parent]["children"].append(t)
        else:
            roots.append(t)

    return roots


def _serialize_value(v: Any) -> Any:
    """Convert a single value to a JSON-safe type, recursing into containers."""
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return _serialize_row(v)
    if isinstance(v, list):
        return [_serialize_value(item) for item in v]
    return v


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a DB row dict to JSON-safe types, recursing into nested structures."""
    return {k: _serialize_value(v) for k, v in row.items()}


def _serialize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert a list of DB row dicts to JSON-safe types."""
    return [_serialize_row(r) for r in rows]
