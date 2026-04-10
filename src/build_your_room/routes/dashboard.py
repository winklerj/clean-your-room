"""Dashboard route — pipeline cards grid, HTN progress, escalation banner."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from build_your_room.db import get_pool

router = APIRouter()

# Status values that allow clone cleanup
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "killed"})


def _parse_mini_graph_nodes(stage_graph_json: str | None) -> list[dict[str, str]]:
    """Extract ordered node list from stage_graph_json for mini-visualization.

    Returns a list of dicts with 'key' and 'name' for each stage node,
    ordered as they appear in the definition (which follows the pipeline flow).
    """
    if not stage_graph_json:
        return []
    try:
        graph = json.loads(stage_graph_json)
    except (json.JSONDecodeError, TypeError):
        return []
    raw_nodes = graph.get("nodes")
    if not raw_nodes or not isinstance(raw_nodes, list):
        return []
    return [{"key": n.get("key", ""), "name": n.get("name", "")} for n in raw_nodes]


async def _fetch_dashboard_data() -> dict[str, Any]:
    """Query all data needed for the main dashboard in a single connection."""
    pool = get_pool()
    async with pool.connection() as conn:
        # Pipelines with repo name
        cur = await conn.execute(
            "SELECT p.*, r.name AS repo_name "
            "FROM pipelines p "
            "JOIN repos r ON r.id = p.repo_id "
            "ORDER BY p.updated_at DESC"
        )
        pipelines: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

        # Current stage info for each pipeline
        cur = await conn.execute(
            "SELECT DISTINCT ON (pipeline_id) "
            "  pipeline_id, stage_key, stage_type, status, iteration, max_iterations "
            "FROM pipeline_stages "
            "ORDER BY pipeline_id, attempt DESC, id DESC"
        )
        stage_rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
        stages_by_pipeline: dict[int, dict[str, Any]] = {
            row["pipeline_id"]: row for row in stage_rows
        }

        # HTN progress per pipeline (primitive tasks only, per spec)
        cur = await conn.execute(
            "SELECT pipeline_id, status, COUNT(*) AS cnt "
            "FROM htn_tasks WHERE task_type = 'primitive' "
            "GROUP BY pipeline_id, status"
        )
        htn_rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
        htn_by_pipeline: dict[int, dict[str, int]] = {}
        for row in htn_rows:
            pid = row["pipeline_id"]
            if pid not in htn_by_pipeline:
                htn_by_pipeline[pid] = {}
            htn_by_pipeline[pid][row["status"]] = row["cnt"]

        # Active session context usage per pipeline (latest running session)
        cur = await conn.execute(
            "SELECT DISTINCT ON (ps.pipeline_id) "
            "  ps.pipeline_id, s.context_usage_pct "
            "FROM agent_sessions s "
            "JOIN pipeline_stages ps ON ps.id = s.pipeline_stage_id "
            "WHERE s.status = 'running' "
            "ORDER BY ps.pipeline_id, s.started_at DESC"
        )
        ctx_rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
        ctx_by_pipeline: dict[int, float | None] = {
            row["pipeline_id"]: row["context_usage_pct"] for row in ctx_rows
        }

        # Cost per pipeline (sum of all session costs)
        cur = await conn.execute(
            "SELECT ps.pipeline_id, SUM(s.cost_usd) AS total_cost "
            "FROM agent_sessions s "
            "JOIN pipeline_stages ps ON ps.id = s.pipeline_stage_id "
            "GROUP BY ps.pipeline_id"
        )
        cost_rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
        cost_by_pipeline: dict[int, float] = {
            row["pipeline_id"]: float(row["total_cost"] or 0) for row in cost_rows
        }

        # Open escalation count
        cur = await conn.execute(
            "SELECT COUNT(*) AS cnt FROM escalations WHERE status = 'open'"
        )
        esc_row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        open_escalations: int = esc_row["cnt"] if esc_row else 0

        # Repos (for the secondary section)
        cur = await conn.execute(
            "SELECT * FROM repos WHERE archived=0 ORDER BY created_at DESC"
        )
        repos: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

        # Pipeline def names and stage graphs (for card display)
        cur = await conn.execute("SELECT id, name, stage_graph_json FROM pipeline_defs")
        def_rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
        def_names: dict[int, str] = {row["id"]: row["name"] for row in def_rows}
        def_graphs: dict[int, list[dict[str, str]]] = {}
        for row in def_rows:
            def_graphs[row["id"]] = _parse_mini_graph_nodes(row.get("stage_graph_json"))

    # Enrich each pipeline with computed data
    enriched: list[dict[str, Any]] = []
    for p in pipelines:
        pid = p["id"]
        htn = htn_by_pipeline.get(pid, {})
        htn_total = sum(htn.values())
        htn_completed = htn.get("completed", 0)
        stage = stages_by_pipeline.get(pid)

        # Build mini stage graph with active node marked
        current_key = p.get("current_stage_key")
        raw_nodes = def_graphs.get(p["pipeline_def_id"], [])
        mini_graph = [
            {**n, "is_active": n["key"] == current_key}
            for n in raw_nodes
        ]

        enriched.append({
            **p,
            "def_name": def_names.get(p["pipeline_def_id"], "unknown"),
            "stage": stage,
            "mini_graph": mini_graph,
            "htn_total": htn_total,
            "htn_completed": htn_completed,
            "htn_in_progress": htn.get("in_progress", 0),
            "htn_ready": htn.get("ready", 0),
            "htn_failed": htn.get("failed", 0),
            "htn_blocked": htn.get("blocked", 0),
            "htn_pct": round(htn_completed / htn_total * 100) if htn_total else 0,
            "context_usage_pct": ctx_by_pipeline.get(pid),
            "total_cost": cost_by_pipeline.get(pid, 0.0),
            "is_terminal": p["status"] in _TERMINAL_STATUSES,
        })

    # Summary counts
    status_counts: dict[str, int] = {}
    for p in pipelines:
        s = p["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    terminal_count = sum(
        1 for p in pipelines
        if p["status"] in _TERMINAL_STATUSES and p.get("clone_path") is not None
    )

    return {
        "pipelines": enriched,
        "repos": repos,
        "open_escalations": open_escalations,
        "status_counts": status_counts,
        "total_pipelines": len(pipelines),
        "total_repos": len(repos),
        "terminal_count": terminal_count,
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    from build_your_room.main import templates

    data = await _fetch_dashboard_data()
    return templates.TemplateResponse(request, "dashboard.html", data)
