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
    "in_progress": "task-in-progress",
    "ready": "task-ready",
    "not_ready": "task-not-ready",
    "blocked": "task-blocked",
    "failed": "task-failed",
    "skipped": "task-skipped",
}


def _get_clone_size(clone_path: str) -> str | None:
    """Compute total size of a clone directory, returning a human-readable string.

    Returns None if the path is empty, missing, or inaccessible.
    """
    if not clone_path:
        return None
    p = Path(clone_path)
    if not p.is_dir():
        return None
    try:
        total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return None
    if total < 1024:
        return f"{total} B"
    elif total < 1024 * 1024:
        return f"{total / 1024:.1f} KB"
    elif total < 1024 * 1024 * 1024:
        return f"{total / (1024 * 1024):.1f} MB"
    else:
        return f"{total / (1024 * 1024 * 1024):.1f} GB"


# ---------------------------------------------------------------------------
# Pipeline creation
# ---------------------------------------------------------------------------


@router.get("/pipelines/new", response_class=HTMLResponse)
async def new_pipeline_form(
    request: Request, error: str | None = None, repo_id: int | None = None,
):
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

    return templates.TemplateResponse(request, "new_pipeline.html", {
        "repos": repos,
        "pipeline_defs": defs,
        "selected_repo_id": repo_id,
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


def _parse_conditions(raw: str) -> list[dict[str, str]]:
    """Parse a conditions JSON string into a display-friendly list.

    Each returned dict has 'type' and 'description' keys.
    Returns an empty list on parse failure or empty input.
    """
    try:
        conditions = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(conditions, list):
        return []
    result: list[dict[str, str]] = []
    for c in conditions:
        if not isinstance(c, dict):
            continue
        result.append({
            "type": str(c.get("type", "unknown")),
            "description": str(c.get("description", c.get("type", ""))),
        })
    return result


def _build_task_tree(
    tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Organize flat task list into a nested tree structure.

    Root tasks (parent_task_id IS NULL) are top-level.
    Each task gets a 'children' key with nested subtasks.
    Parses preconditions_json/postconditions_json for display.
    """
    by_id: dict[int, dict[str, Any]] = {}
    for t in tasks:
        t["children"] = []
        t["status_class"] = _TASK_STATUS_CLASS.get(t["status"], "task-not-ready")
        # Decision-type tasks get pink "needs human" coloring (spec line 958)
        if t.get("task_type") == "decision" and t.get("status") != "completed":
            t["status_class"] = "task-decision"
        t["preconditions"] = _parse_conditions(t.get("preconditions_json", "[]"))
        t["postconditions"] = _parse_conditions(t.get("postconditions_json", "[]"))
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

    # Clone size
    clone_size = _get_clone_size(pipeline.get("clone_path") or "")

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
        "clone_size": clone_size,
    }


@router.get("/pipelines/{pipeline_id}", response_class=HTMLResponse)
async def pipeline_detail(request: Request, pipeline_id: int):
    from build_your_room.main import templates

    data = await _fetch_pipeline_detail(pipeline_id)
    if data is None:
        return HTMLResponse("<h1>Pipeline not found</h1>", status_code=404)

    return templates.TemplateResponse(request, "pipeline_detail.html", data)


@router.get("/pipelines/{pipeline_id}/tasks", response_class=HTMLResponse)
async def pipeline_tasks_page(
    request: Request,
    pipeline_id: int,
    status_filter: str | None = None,
    type_filter: str | None = None,
):
    """Standalone HTN task tree view for a pipeline.

    Supports optional query-param filters:
      ?status_filter=completed  — show only tasks with this status
      ?type_filter=primitive    — show only this task type
    """
    from build_your_room.main import templates

    pool = get_pool()
    async with pool.connection() as conn:
        # Pipeline header info
        cur = await conn.execute(
            "SELECT p.id, p.status, p.current_stage_key, "
            "  r.name AS repo_name, pd.name AS def_name "
            "FROM pipelines p "
            "JOIN repos r ON r.id = p.repo_id "
            "JOIN pipeline_defs pd ON pd.id = p.pipeline_def_id "
            "WHERE p.id = %s",
            (pipeline_id,),
        )
        pipeline: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        if pipeline is None:
            return HTMLResponse("<h1>Pipeline not found</h1>", status_code=404)

        # All HTN tasks for this pipeline
        cur = await conn.execute(
            "SELECT * FROM htn_tasks WHERE pipeline_id = %s "
            "ORDER BY ordering ASC, id ASC",
            (pipeline_id,),
        )
        tasks: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

        # Task deps
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

    # Enrich with deps
    for t in tasks:
        t["deps"] = deps_by_task.get(t["id"], [])

    # Progress summary (primitives only)
    primitive_tasks = [t for t in tasks if t["task_type"] == "primitive"]
    htn_counts: dict[str, int] = {}
    for t in primitive_tasks:
        htn_counts[t["status"]] = htn_counts.get(t["status"], 0) + 1
    htn_total = len(primitive_tasks)
    htn_completed = htn_counts.get("completed", 0)

    # Count all tasks by type
    type_counts: dict[str, int] = {}
    for t in tasks:
        type_counts[t["task_type"]] = type_counts.get(t["task_type"], 0) + 1

    # Count all tasks by status (all types, not just primitive)
    all_status_counts: dict[str, int] = {}
    for t in tasks:
        all_status_counts[t["status"]] = all_status_counts.get(t["status"], 0) + 1

    # Apply filters (on the full flat list before building tree)
    filtered_ids: set[int] | None = None
    if status_filter or type_filter:
        filtered_ids = set()
        for t in tasks:
            status_match = (not status_filter) or t["status"] == status_filter
            type_match = (not type_filter) or t["task_type"] == type_filter
            if status_match and type_match:
                filtered_ids.add(t["id"])
                # Include ancestor chain so the tree structure is preserved
                parent = t.get("parent_task_id")
                while parent and parent in deps_by_task:
                    filtered_ids.add(parent)
                    parent_task = next((x for x in tasks if x["id"] == parent), None)
                    parent = parent_task.get("parent_task_id") if parent_task else None

    if filtered_ids is not None:
        tasks = [t for t in tasks if t["id"] in filtered_ids]

    task_tree = _build_task_tree(tasks)

    return templates.TemplateResponse(request, "htn_tasks.html", {
        "pipeline": pipeline,
        "task_tree": task_tree,
        "htn_total": htn_total,
        "htn_completed": htn_completed,
        "htn_pct": round(htn_completed / htn_total * 100) if htn_total else 0,
        "htn_counts": htn_counts,
        "type_counts": type_counts,
        "all_status_counts": all_status_counts,
        "status_filter": status_filter or "",
        "type_filter": type_filter or "",
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

    return templates.TemplateResponse(request, "partials/pipeline_logs.html", {
        "logs": logs,
    })


def _build_context_chart(
    sessions: list[dict[str, Any]],
    threshold: int = 60,
) -> dict[str, Any] | None:
    """Build pre-computed SVG chart data for context usage over time.

    Returns None when no sessions have context usage data.
    """
    points: list[dict[str, Any]] = []
    for i, s in enumerate(sessions):
        pct = s.get("context_usage_pct")
        if pct is not None:
            points.append({"label": f"S{i + 1}", "pct": float(pct)})

    if not points:
        return None

    bar_w = 32
    gap = 8
    left_pad = 40
    right_pad = 24
    top_pad = 15
    bottom_pad = 25
    chart_h = 120
    total_w = left_pad + len(points) * (bar_w + gap) + right_pad
    total_h = top_pad + chart_h + bottom_pad

    grid_lines = []
    for pct_val in (0, 25, 50, 75, 100):
        y = round(top_pad + chart_h - (pct_val / 100 * chart_h), 1)
        grid_lines.append({"pct": pct_val, "y": y})

    thresh_y = round(top_pad + chart_h - (threshold / 100 * chart_h), 1)

    bars = []
    for idx, pt in enumerate(points):
        bar_x = round(left_pad + idx * (bar_w + gap) + gap / 2, 1)
        bar_h = max(round(pt["pct"] / 100 * chart_h, 1), 0)
        bar_y = round(top_pad + chart_h - bar_h, 1)
        css_class = (
            "chart-bar-high" if pt["pct"] > 80
            else "chart-bar-warn" if pt["pct"] > 50
            else "chart-bar-ok"
        )
        bars.append({
            "x": bar_x, "y": bar_y,
            "width": bar_w, "height": bar_h,
            "css_class": css_class,
            "label": pt["label"],
            "label_x": round(bar_x + bar_w / 2, 1),
            "label_y": round(top_pad + chart_h + 14, 1),
            "pct": pt["pct"],
        })

    return {
        "total_w": total_w,
        "total_h": total_h,
        "left_pad": left_pad,
        "right_pad": right_pad,
        "grid_lines": grid_lines,
        "threshold": threshold,
        "thresh_y": thresh_y,
        "bars": bars,
    }


async def _fetch_stage_detail(
    pipeline_id: int, stage_id: int,
) -> dict[str, Any] | None:
    """Query all data needed for the stage detail partial.

    Returns the stage row, its sessions with per-session logs,
    review feedback entries, output artifact content, and
    context usage chart data for the stage.
    """
    pool = get_pool()
    async with pool.connection() as conn:
        # Stage row with pipeline ownership check
        cur = await conn.execute(
            "SELECT * FROM pipeline_stages WHERE id = %s AND pipeline_id = %s",
            (stage_id, pipeline_id),
        )
        stage: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        if stage is None:
            return None

        # Pipeline config for context threshold
        cur = await conn.execute(
            "SELECT config_json FROM pipelines WHERE id = %s",
            (pipeline_id,),
        )
        pipeline_cfg_row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]

        # Sessions for this stage
        cur = await conn.execute(
            "SELECT * FROM agent_sessions "
            "WHERE pipeline_stage_id = %s ORDER BY started_at ASC",
            (stage_id,),
        )
        sessions: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

        session_ids = [s["id"] for s in sessions]
        logs_by_session: dict[int, list[dict[str, Any]]] = {
            sid: [] for sid in session_ids
        }
        review_feedback: list[dict[str, Any]] = []

        if session_ids:
            placeholders = ",".join(["%s"] * len(session_ids))
            cur = await conn.execute(
                "SELECT * FROM session_logs "
                f"WHERE agent_session_id IN ({placeholders}) "
                "ORDER BY created_at ASC",
                tuple(session_ids),
            )
            all_logs: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
            for log in all_logs:
                logs_by_session[log["agent_session_id"]].append(log)
                if log["event_type"] == "review_feedback":
                    review_feedback.append(log)

    # Enrich sessions with their logs and context usage
    enriched_sessions: list[dict[str, Any]] = []
    for s in sessions:
        enriched_sessions.append({
            **s,
            "logs": logs_by_session.get(s["id"], []),
        })

    # Read output artifact content (if path exists and file is readable)
    artifact_content: str | None = None
    artifact_path = stage.get("output_artifact")
    if artifact_path:
        try:
            artifact_content = Path(artifact_path).read_text(encoding="utf-8")
        except (OSError, ValueError):
            artifact_content = None

    # Context usage chart data
    context_threshold = 60
    if pipeline_cfg_row:
        try:
            cfg = json.loads(pipeline_cfg_row["config_json"] or "{}")
            context_threshold = int(cfg.get("context_threshold_pct", 60))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    context_chart = _build_context_chart(sessions, context_threshold)

    return {
        "stage": stage,
        "sessions": enriched_sessions,
        "review_feedback": review_feedback,
        "artifact_content": artifact_content,
        "context_chart": context_chart,
    }


@router.get(
    "/pipelines/{pipeline_id}/stages/{stage_id}", response_class=HTMLResponse,
)
async def stage_detail_partial(
    request: Request, pipeline_id: int, stage_id: int,
):
    """HTMX partial: stage detail with sessions, logs, artifacts, review feedback."""
    from build_your_room.main import templates

    data = await _fetch_stage_detail(pipeline_id, stage_id)
    if data is None:
        return HTMLResponse("<p>Stage not found</p>", status_code=404)

    return templates.TemplateResponse(request, "partials/stage_detail.html", {
        "pipeline_id": pipeline_id,
        **data,
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

    # Mark pipeline as cleaned in DB
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE pipelines SET clone_path = NULL, clone_cleaned_at = now(), "
            "updated_at = now() WHERE id = %s",
            (pipeline_id,),
        )
        await conn.commit()

    # Check if this is an HTMX request (card swap) or a full-page request (redirect)
    is_htmx = request.headers.get("HX-Request") == "true"
    if is_htmx:
        card_data = await _fetch_pipeline_card_data(pipeline_id)
        if card_data is None:
            return HTMLResponse("<p>Pipeline not found</p>", status_code=404)
        return templates.TemplateResponse(request, "partials/pipeline_card.html", {
            "pipeline": card_data,
        })

    return RedirectResponse(url=f"/pipelines/{pipeline_id}", status_code=303)


# ---------------------------------------------------------------------------
# HTN decision task resolution
# ---------------------------------------------------------------------------


@router.post("/pipelines/{pipeline_id}/tasks/{task_id}/resolve")
async def resolve_decision_task_html(
    pipeline_id: int,
    task_id: int,
    resolution: str = Form(...),
):
    """Resolve a decision-type HTN task from the pipeline detail page."""
    pool = get_pool()
    async with pool.connection() as conn:
        # Verify pipeline exists
        cur = await conn.execute(
            "SELECT id FROM pipelines WHERE id = %s", (pipeline_id,)
        )
        if await cur.fetchone() is None:
            return HTMLResponse("<h1>Pipeline not found</h1>", status_code=404)

        # Verify task exists, belongs to this pipeline, and is a decision type
        cur = await conn.execute(
            "SELECT id, pipeline_id, task_type, status FROM htn_tasks "
            "WHERE id = %s",
            (task_id,),
        )
        task: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        if task is None or task["pipeline_id"] != pipeline_id:
            return HTMLResponse("<h1>Task not found</h1>", status_code=404)

        if task["task_type"] != "decision":
            return HTMLResponse(
                "<h1>Only decision tasks can be resolved</h1>", status_code=409,
            )

        if task["status"] == "completed":
            return RedirectResponse(
                url=f"/pipelines/{pipeline_id}", status_code=303,
            )

    from build_your_room.htn_planner import HTNPlanner

    planner = HTNPlanner(pool)
    await planner.resolve_decision(task_id, resolution)

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

    cleaned_ids: list[int] = []
    for row in rows:
        clone_path = Path(row["clone_path"])
        if clone_path.exists():
            shutil.rmtree(clone_path)
        cleaned_ids.append(row["id"])

    # Mark all cleaned pipelines in DB
    if cleaned_ids:
        async with pool.connection() as conn:
            for pid in cleaned_ids:
                await conn.execute(
                    "UPDATE pipelines SET clone_path = NULL, clone_cleaned_at = now(), "
                    "updated_at = now() WHERE id = %s",
                    (pid,),
                )
            await conn.commit()

    return RedirectResponse(url="/", status_code=303)
