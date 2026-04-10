"""Escalation queue routes — list, resolve, and dismiss escalations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from build_your_room.db import get_pool

router = APIRouter()


async def _fetch_escalation_data(
    *, include_resolved: bool = False,
) -> dict[str, Any]:
    """Query all data needed for the escalation queue page."""
    pool = get_pool()
    async with pool.connection() as conn:
        # Escalations joined with pipeline + stage info
        status_filter = "" if include_resolved else "WHERE e.status = 'open'"
        cur = await conn.execute(
            "SELECT e.*, "
            "  p.clone_path, p.status AS pipeline_status, "
            "  r.name AS repo_name, "
            "  pd.name AS def_name, "
            "  ps.stage_key, ps.stage_type "
            "FROM escalations e "
            "JOIN pipelines p ON p.id = e.pipeline_id "
            "JOIN repos r ON r.id = p.repo_id "
            "JOIN pipeline_defs pd ON pd.id = p.pipeline_def_id "
            "LEFT JOIN pipeline_stages ps ON ps.id = e.pipeline_stage_id "
            f"{status_filter} "
            "ORDER BY e.created_at DESC"
        )
        rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

        # Enrich with parsed context
        enriched: list[dict[str, Any]] = []
        for row in rows:
            ctx = {}
            if row.get("context_json"):
                try:
                    ctx = json.loads(row["context_json"])
                except (json.JSONDecodeError, TypeError):
                    ctx = {}
            enriched.append({**row, "context": ctx})

        # Summary counts
        cur = await conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM escalations GROUP BY status"
        )
        count_rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
        status_counts: dict[str, int] = {
            r["status"]: r["cnt"] for r in count_rows
        }

    return {
        "escalations": enriched,
        "status_counts": status_counts,
        "open_count": status_counts.get("open", 0),
        "resolved_count": status_counts.get("resolved", 0),
        "dismissed_count": status_counts.get("dismissed", 0),
        "include_resolved": include_resolved,
    }


@router.get("/escalations", response_class=HTMLResponse)
async def escalation_queue(request: Request):
    from build_your_room.main import templates

    show_all = request.query_params.get("show_all", "").lower() in ("1", "true")
    data = await _fetch_escalation_data(include_resolved=show_all)
    return templates.TemplateResponse(request, "escalations.html", data)


@router.post("/escalations/{escalation_id}/resolve")
async def resolve_escalation(
    escalation_id: int,
    resolution: str = Form(...),
):
    pool = get_pool()
    async with pool.connection() as conn:
        now = datetime.now(timezone.utc).isoformat()
        await conn.execute(
            "UPDATE escalations SET status = 'resolved', resolution = %s, "
            "resolved_at = %s WHERE id = %s AND status = 'open'",
            (resolution, now, escalation_id),
        )
        await conn.commit()
    return RedirectResponse(url="/escalations", status_code=303)


@router.post("/escalations/{escalation_id}/dismiss")
async def dismiss_escalation(escalation_id: int):
    pool = get_pool()
    async with pool.connection() as conn:
        now = datetime.now(timezone.utc).isoformat()
        await conn.execute(
            "UPDATE escalations SET status = 'dismissed', "
            "resolved_at = %s WHERE id = %s AND status = 'open'",
            (now, escalation_id),
        )
        await conn.commit()
    return RedirectResponse(url="/escalations", status_code=303)
