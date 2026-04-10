"""Pipeline routes — creation form, lifecycle control, detail page, clone management."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from build_your_room.db import get_pool

router = APIRouter()

# Status values that allow clone cleanup
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "killed"})

# Status → CSS color class mapping for HTN tasks
_TASK_STATUS_CLASS = {
    "completed": "task-completed",
    "in_progress": "task-in_progress",
    "ready": "task-ready",
    "not_ready": "task-not_ready",
    "blocked": "task-blocked",
    "failed": "task-failed",
    "skipped": "task-skipped",
}


# ---------------------------------------------------------------------------
# Pipeline creation
# ---------------------------------------------------------------------------


@router.get("/pipelines/new", response_class=HTMLResponse)
async def new_pipeline_form(request: Request, error: str | None = None):
    """Render the pipeline creation form with repo and def selectors."""
    from build_your_room.main import templates

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, name, local_path FROM repos WHERE archived = 0 ORDER BY name"
        )
        repos: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

        cur = await conn.execute(
            "SELECT id, name FROM pipeline_defs ORDER BY name"
        )
        defs: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

    return templates.TemplateResponse("new_pipeline.html", {
        "request": request,
        "repos": repos,
        "pipeline_defs": defs,
        "error": error,
    })


@router.post("/pipelines")
async def create_pipeline(
    request: Request,
    pipeline_def_id: int = Form(),
    repo_id: int = Form(),
):
    """Create a new pipeline and start it via the orchestrator. Redirects to detail page."""
    pool = get_pool()
    async with pool.connection() as conn:
        # Validate pipeline_def exists
        cur = await conn.execute(
            "SELECT id FROM pipeline_defs WHERE id = %s", (pipeline_def_id,)
        )
        if await cur.fetchone() is None:
            return await new_pipeline_form(request, error="Pipeline definition not found")

        # Validate repo exists and not archived
        cur = await conn.execute(
            "SELECT id FROM repos WHERE id = %s AND archived = 0", (repo_id,)
        )
        if await cur.fetchone() is None:
            return await new_pipeline_form(request, error="Repo not found or archived")

        cur = await conn.execute(
            "INSERT INTO pipelines "
            "(pipeline_def_id, repo_id, clone_path, review_base_rev, status, config_json) "
            "VALUES (%s, %s, %s, %s, 'pending', %s) RETURNING id",
            (pipeline_def_id, repo_id, "", "", "{}"),
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        assert row is not None
        await conn.commit()

    pipeline_id = row["id"]

    from build_your_room.main import orchestrator

    if orchestrator is not None:
        await orchestrator.start_pipeline(pipeline_id)

    return RedirectResponse(url=f"/pipelines/{pipeline_id}", status_code=303)


# ---------------------------------------------------------------------------
# Pipeline lifecycle control
# ---------------------------------------------------------------------------


@router.post("/pipelines/{pipeline_id}/cancel")
async def cancel_pipeline_html(pipeline_id: int):
    """Graceful cancel — signals the cancel event, redirects to detail page."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, status FROM pipelines WHERE id = %s", (pipeline_id,)
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        if row is None:
            return HTMLResponse("<h1>Pipeline not found</h1>", status_code=404)

    if row["status"] in ("running", "paused"):
        from build_your_room.main import orchestrator

        if orchestrator is not None:
            await orchestrator.cancel_pipeline(pipeline_id)
        else:
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE pipelines SET status = 'cancel_requested', updated_at = now() "
                    "WHERE id = %s AND status IN ('running', 'paused')",
                    (pipeline_id,),
                )
                await conn.commit()

    return RedirectResponse(url=f"/pipelines/{pipeline_id}", status_code=303)


@router.post("/pipelines/{pipeline_id}/kill")
async def kill_pipeline_html(pipeline_id: int):
    """Force kill — terminates live sessions immediately, redirects to detail page."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, status FROM pipelines WHERE id = %s", (pipeline_id,)
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        if row is None:
            return HTMLResponse("<h1>Pipeline not found</h1>", status_code=404)

    if row["status"] not in _TERMINAL_STATUSES:
        from build_your_room.main import orchestrator

        if orchestrator is not None:
            await orchestrator.kill_pipeline(pipeline_id)
        else:
            async with pool.connection() as conn:
                placeholders = ",".join(["%s"] * len(_TERMINAL_STATUSES))
                await conn.execute(
                    "UPDATE pipelines SET status = 'killed', updated_at = now() "
                    f"WHERE id = %s AND status NOT IN ({placeholders})",
                    (pipeline_id, *_TERMINAL_STATUSES),
                )
                await conn.commit()

    return RedirectResponse(url=f"/pipelines/{pipeline_id}", status_code=303)


@router.post("/pipelines/{pipeline_id}/pause")
async def pause_pipeline_html(pipeline_id: int):
    """Pause a running pipeline, redirects to detail page."""
    pool = get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE pipelines SET status = 'paused', updated_at = now() "
            "WHERE id = %s AND status = 'running'",
            (pipeline_id,),
        )
        await conn.commit()

    return RedirectResponse(url=f"/pipelines/{pipeline_id}", status_code=303)


@router.post("/pipelines/{pipeline_id}/resume")
async def resume_pipeline_html(
    pipeline_id: int,
    resolution: str = Form(""),
):
    """Resume a paused pipeline after escalation resolution."""
    from build_your_room.main import orchestrator

    if orchestrator is not None:
        await orchestrator.resume_pipeline(pipeline_id, resolution or "Resumed from dashboard")
    else:
        pool = get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE escalations SET status = 'resolved', resolution = %s, "
                "resolved_at = now() WHERE pipeline_id = %s AND status = 'open'",
                (resolution or "Resumed from dashboard", pipeline_id),
            )
            await conn.execute(
                "UPDATE pipelines SET status = 'pending', updated_at = now() "
                "WHERE id = %s AND status = 'paused'",
                (pipeline_id,),
            )
            await conn.commit()

    return RedirectResponse(url=f"/pipelines/{pipeline_id}", status_code=303)


# ---------------------------------------------------------------------------
# Detail page helpers
# ---------------------------------------------------------------------------


def _build_task_tree(
    tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Organize flat task list into a nested tree structure.

    Root tasks (parent_task_id IS NULL) are top-level.
    Each task gets a 'children' key with nested subtasks.
    """
    by_id: dict[int, dict[str, Any]] = {}
    for t in tasks:
        t["children"] = []
        t["status_class"] = _TASK_STATUS_CLASS.get(t["status"], "task-not-ready")
        by_id[t["id"]] = t

    roots: list[dict[str, Any]] = []
    for t in tasks:
        parent = t.get("parent_task_id")
        if parent and parent in by_id:
            by_id[parent]["children"].append(t)
        else:
            roots.append(t)

    return roots


def _parse_stage_graph(stage_graph_json: str) -> dict[str, Any]:
    """Parse stage graph JSON into nodes and edges for visualization."""
    try:
        graph = json.loads(stage_graph_json)
    except (json.JSONDecodeError, TypeError):
        return {"nodes": [], "edges": [], "entry_stage": None}

    return {
        "nodes": graph.get("nodes", []),
        "edges": graph.get("edges", []),
        "entry_stage": graph.get("entry_stage"),
    }


async def _fetch_pipeline_detail(pipeline_id: int) -> dict[str, Any] | None:
    """Query all data needed for the pipeline detail page."""
    pool = get_pool()
    async with pool.connection() as conn:
        # Pipeline with repo and def info
        cur = await conn.execute(
            "SELECT p.*, r.name AS repo_name, r.local_path AS repo_path, "
            "  pd.name AS def_name, pd.stage_graph_json "
            "FROM pipelines p "
            "JOIN repos r ON r.id = p.repo_id "
            "JOIN pipeline_defs pd ON pd.id = p.pipeline_def_id "
            "WHERE p.id = %s",
            (pipeline_id,),
        )
        pipeline: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        if pipeline is None:
            return None

        # All stages for this pipeline, ordered by attempt
        cur = await conn.execute(
            "SELECT * FROM pipeline_stages "
            "WHERE pipeline_id = %s "
            "ORDER BY attempt ASC, id ASC",
            (pipeline_id,),
        )
        stages: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

        # Stage visit counts (for edge traversal annotation)
        stage_visits: dict[str, int] = {}
        for s in stages:
            key = s["stage_key"]
            stage_visits[key] = stage_visits.get(key, 0) + 1

        # Sessions per stage
        stage_ids = [s["id"] for s in stages]
        sessions_by_stage: dict[int, list[dict[str, Any]]] = {
            sid: [] for sid in stage_ids
        }
        if stage_ids:
            placeholders = ",".join(["%s"] * len(stage_ids))
            cur = await conn.execute(
                "SELECT * FROM agent_sessions "
                f"WHERE pipeline_stage_id IN ({placeholders}) "
                "ORDER BY started_at ASC",
                tuple(stage_ids),
            )
            sessions: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
            for sess in sessions:
                sessions_by_stage[sess["pipeline_stage_id"]].append(sess)

        # Recent logs for the active session (latest running or most recent)
        cur = await conn.execute(
            "SELECT sl.* FROM session_logs sl "
            "JOIN agent_sessions s ON s.id = sl.agent_session_id "
            "JOIN pipeline_stages ps ON ps.id = s.pipeline_stage_id "
            "WHERE ps.pipeline_id = %s "
            "ORDER BY sl.created_at DESC LIMIT 50",
            (pipeline_id,),
        )
        logs: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
        logs.reverse()  # chronological order

        # HTN tasks
        cur = await conn.execute(
            "SELECT * FROM htn_tasks "
            "WHERE pipeline_id = %s "
            "ORDER BY ordering ASC, id ASC",
            (pipeline_id,),
        )
        tasks: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

        # HTN task deps
        task_ids = [t["id"] for t in tasks]
        deps_by_task: dict[int, list[int]] = {tid: [] for tid in task_ids}
        if task_ids:
            placeholders = ",".join(["%s"] * len(task_ids))
            cur = await conn.execute(
                "SELECT * FROM htn_task_deps "
                f"WHERE task_id IN ({placeholders})",
                tuple(task_ids),
            )
            dep_rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
            for d in dep_rows:
                deps_by_task[d["task_id"]].append(d["depends_on_task_id"])

        # Escalations for this pipeline
        cur = await conn.execute(
            "SELECT e.*, ps.stage_key, ps.stage_type "
            "FROM escalations e "
            "LEFT JOIN pipeline_stages ps ON ps.id = e.pipeline_stage_id "
            "WHERE e.pipeline_id = %s "
            "ORDER BY e.created_at DESC",
            (pipeline_id,),
        )
        escalations: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

    # Enrich stages with sessions
    enriched_stages: list[dict[str, Any]] = []
    for s in stages:
        enriched_stages.append({
            **s,
            "sessions": sessions_by_stage.get(s["id"], []),
            "visit_number": stage_visits.get(s["stage_key"], 1),
        })

    # Parse stage graph for visualization
    graph = _parse_stage_graph(pipeline.get("stage_graph_json", "{}"))

    # Mark active node in graph
    current_key = pipeline.get("current_stage_key")
    for node in graph["nodes"]:
        node["is_active"] = node.get("key") == current_key
        node["visit_count"] = stage_visits.get(node.get("key", ""), 0)

    # Build HTN task tree
    for t in tasks:
        t["deps"] = deps_by_task.get(t["id"], [])
    task_tree = _build_task_tree(tasks)

    # HTN progress summary
    htn_counts: dict[str, int] = {}
    primitive_tasks = [t for t in tasks if t["task_type"] == "primitive"]
    for t in primitive_tasks:
        htn_counts[t["status"]] = htn_counts.get(t["status"], 0) + 1
    htn_total = len(primitive_tasks)
    htn_completed = htn_counts.get("completed", 0)

    # Lease health
    lease_healthy = (
        pipeline.get("owner_token") is not None
        and pipeline.get("lease_expires_at") is not None
        and pipeline["status"] == "running"
    )

    # Dirty snapshot info
    has_dirty_snapshot = pipeline.get("dirty_snapshot_artifact") is not None
    workspace_dirty = pipeline.get("workspace_state") != "clean"

    # Enrich escalation context
    for esc in escalations:
        ctx = {}
        if esc.get("context_json"):
            try:
                ctx = json.loads(esc["context_json"])
            except (json.JSONDecodeError, TypeError):
                ctx = {}
        esc["context"] = ctx

    # Total cost
    total_cost = 0.0
    for s in stages:
        for sess in sessions_by_stage.get(s["id"], []):
            total_cost += float(sess.get("cost_usd", 0) or 0)

    return {
        "pipeline": pipeline,
        "graph": graph,
        "stages": enriched_stages,
        "task_tree": task_tree,
        "tasks_flat": tasks,
        "htn_total": htn_total,
        "htn_completed": htn_completed,
        "htn_pct": round(htn_completed / htn_total * 100) if htn_total else 0,
        "htn_counts": htn_counts,
        "escalations": escalations,
        "logs": logs,
        "lease_healthy": lease_healthy,
        "has_dirty_snapshot": has_dirty_snapshot,
        "workspace_dirty": workspace_dirty,
        "is_terminal": pipeline["status"] in _TERMINAL_STATUSES,
        "total_cost": total_cost,
    }


@router.get("/pipelines/{pipeline_id}", response_class=HTMLResponse)
async def pipeline_detail(request: Request, pipeline_id: int):
    from build_your_room.main import templates

    data = await _fetch_pipeline_detail(pipeline_id)
    if data is None:
        return HTMLResponse("<h1>Pipeline not found</h1>", status_code=404)

    return templates.TemplateResponse("pipeline_detail.html", {
        "request": request,
        **data,
    })


@router.get("/pipelines/{pipeline_id}/logs", response_class=HTMLResponse)
async def pipeline_logs_partial(request: Request, pipeline_id: int):
    """HTMX partial: fetch latest logs for live polling."""
    from build_your_room.main import templates

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT sl.* FROM session_logs sl "
            "JOIN agent_sessions s ON s.id = sl.agent_session_id "
            "JOIN pipeline_stages ps ON ps.id = s.pipeline_stage_id "
            "WHERE ps.pipeline_id = %s "
            "ORDER BY sl.created_at DESC LIMIT 50",
            (pipeline_id,),
        )
        logs: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
        logs.reverse()

    return templates.TemplateResponse("partials/pipeline_logs.html", {
        "request": request,
        "logs": logs,
    })


async def _fetch_pipeline_card_data(
    pipeline_id: int,
) -> dict[str, Any] | None:
    """Fetch enriched pipeline data suitable for rendering a single card partial."""
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
            return None

        # Latest stage
        cur = await conn.execute(
            "SELECT stage_key, stage_type, status, iteration, max_iterations "
            "FROM pipeline_stages WHERE pipeline_id = %s "
            "ORDER BY attempt DESC, id DESC LIMIT 1",
            (pipeline_id,),
        )
        stage: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]

        # HTN progress
        cur = await conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM htn_tasks "
            "WHERE pipeline_id = %s AND task_type = 'primitive' GROUP BY status",
            (pipeline_id,),
        )
        htn_rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
        htn: dict[str, int] = {r["status"]: r["cnt"] for r in htn_rows}
        htn_total = sum(htn.values())
        htn_completed = htn.get("completed", 0)

        # Cost
        cur = await conn.execute(
            "SELECT COALESCE(SUM(s.cost_usd), 0) AS total_cost "
            "FROM agent_sessions s "
            "JOIN pipeline_stages ps ON ps.id = s.pipeline_stage_id "
            "WHERE ps.pipeline_id = %s",
            (pipeline_id,),
        )
        cost_row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        total_cost = float(cost_row["total_cost"]) if cost_row else 0.0

        # Context usage (latest running session)
        cur = await conn.execute(
            "SELECT s.context_usage_pct FROM agent_sessions s "
            "JOIN pipeline_stages ps ON ps.id = s.pipeline_stage_id "
            "WHERE ps.pipeline_id = %s AND s.status = 'running' "
            "ORDER BY s.started_at DESC LIMIT 1",
            (pipeline_id,),
        )
        ctx_row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]

    return {
        **pipeline,
        "def_name": pipeline["def_name"],
        "stage": stage,
        "htn_total": htn_total,
        "htn_completed": htn_completed,
        "htn_in_progress": htn.get("in_progress", 0),
        "htn_ready": htn.get("ready", 0),
        "htn_failed": htn.get("failed", 0),
        "htn_blocked": htn.get("blocked", 0),
        "htn_pct": round(htn_completed / htn_total * 100) if htn_total else 0,
        "context_usage_pct": ctx_row["context_usage_pct"] if ctx_row else None,
        "total_cost": total_cost,
        "is_terminal": pipeline["status"] in _TERMINAL_STATUSES,
    }


@router.post("/pipelines/{pipeline_id}/cleanup")
async def cleanup_pipeline_clone(request: Request, pipeline_id: int):
    """Delete a pipeline's clone directory. Returns updated card partial for HTMX."""
    from build_your_room.main import templates

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, status, clone_path FROM pipelines WHERE id = %s",
            (pipeline_id,),
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]

    if row is None:
        return HTMLResponse("<p>Pipeline not found</p>", status_code=404)

    if row["status"] not in _TERMINAL_STATUSES:
        return HTMLResponse(
            "<p>Cannot clean up a pipeline that is still active</p>",
            status_code=409,
        )

    # Delete clone directory if it exists
    clone_path = Path(row["clone_path"]) if row["clone_path"] else None
    if clone_path and clone_path.exists():
        shutil.rmtree(clone_path)

    # Check if this is an HTMX request (card swap) or a full-page request (redirect)
    is_htmx = request.headers.get("HX-Request") == "true"
    if is_htmx:
        card_data = await _fetch_pipeline_card_data(pipeline_id)
        if card_data is None:
            return HTMLResponse("<p>Pipeline not found</p>", status_code=404)
        return templates.TemplateResponse("partials/pipeline_card.html", {
            "request": request,
            "pipeline": card_data,
        })

    return RedirectResponse(url=f"/pipelines/{pipeline_id}", status_code=303)


@router.post("/pipelines/cleanup-completed")
async def cleanup_completed_clones():
    """Bulk cleanup clones for all completed/cancelled/killed pipelines."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, clone_path FROM pipelines "
            "WHERE status IN ('completed', 'cancelled', 'killed') "
            "AND clone_path IS NOT NULL"
        )
        rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

    for row in rows:
        clone_path = Path(row["clone_path"])
        if clone_path.exists():
            shutil.rmtree(clone_path)

    return RedirectResponse(url="/", status_code=303)
