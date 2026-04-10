"""Prompt management routes — CRUD, usage tracking, filtering, clone."""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse

from build_your_room.db import get_pool

router = APIRouter(prefix="/prompts")

STAGE_TYPES = [
    "spec_author", "spec_review", "impl_plan", "impl_plan_review",
    "impl_task", "code_review", "bug_fix", "validation", "custom",
]

AGENT_TYPES = ["claude", "codex"]

_VARIABLE_RE = re.compile(r"\{\{(\w+)\}\}")


def extract_template_variables(body: str) -> list[str]:
    """Extract unique {{variable}} names from a prompt body, preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for match in _VARIABLE_RE.finditer(body):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


async def _fetch_prompt_usage() -> dict[str, list[str]]:
    """Map prompt names to the pipeline def names that reference them.

    Scans stage_graph_json for node.prompt, node.fix_prompt, and
    node.review.prompt references.
    """
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT name, stage_graph_json FROM pipeline_defs"
        )
        defs: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

    usage: dict[str, list[str]] = {}
    for defn in defs:
        def_name = defn["name"]
        try:
            graph = json.loads(defn.get("stage_graph_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in graph.get("nodes", []):
            for field in ("prompt", "fix_prompt"):
                pname = node.get(field, "")
                if pname:
                    usage.setdefault(pname, [])
                    if def_name not in usage[pname]:
                        usage[pname].append(def_name)
            review = node.get("review") or {}
            rpname = review.get("prompt", "")
            if rpname:
                usage.setdefault(rpname, [])
                if def_name not in usage[rpname]:
                    usage[rpname].append(def_name)
    return usage


async def _fetch_prompts(
    stage_type: str | None = None,
    agent_type: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch prompts with optional filtering."""
    pool = get_pool()
    clauses: list[str] = []
    params: list[str] = []
    if stage_type:
        clauses.append("stage_type = %s")
        params.append(stage_type)
    if agent_type:
        clauses.append("agent_type = %s")
        params.append(agent_type)

    where = ""
    if clauses:
        where = "WHERE " + " AND ".join(clauses)

    async with pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT * FROM prompts {where} ORDER BY stage_type, name",  # noqa: S608
            tuple(params),
        )
        rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
    return rows


def _enrich_prompts(
    prompts: list[dict[str, Any]],
    usage: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """Enrich prompt rows with usage info and template variables."""
    enriched: list[dict[str, Any]] = []
    for p in prompts:
        enriched.append({
            **p,
            "used_by": usage.get(p["name"], []),
            "variables": extract_template_variables(p["body"]),
        })
    return enriched


def _prompt_context(
    request: Request,
    prompts: list[dict[str, Any]],
    *,
    error: str | None = None,
    filter_stage_type: str | None = None,
    filter_agent_type: str | None = None,
) -> dict[str, Any]:
    """Build template context for the prompts page."""
    total = len(prompts)
    by_stage: dict[str, int] = {}
    by_agent: dict[str, int] = {}
    for p in prompts:
        by_stage[p["stage_type"]] = by_stage.get(p["stage_type"], 0) + 1
        by_agent[p["agent_type"]] = by_agent.get(p["agent_type"], 0) + 1

    return {
        "request": request,
        "prompts": prompts,
        "total": total,
        "by_stage": by_stage,
        "by_agent": by_agent,
        "stage_types": STAGE_TYPES,
        "agent_types": AGENT_TYPES,
        "filter_stage_type": filter_stage_type or "",
        "filter_agent_type": filter_agent_type or "",
        "error": error,
    }


@router.get("", response_class=HTMLResponse)
async def list_prompts(
    request: Request,
    stage_type: str | None = None,
    agent_type: str | None = None,
):
    from build_your_room.main import templates

    usage = await _fetch_prompt_usage()
    prompts = await _fetch_prompts(stage_type=stage_type, agent_type=agent_type)
    enriched = _enrich_prompts(prompts, usage)
    ctx = _prompt_context(
        request, enriched,
        filter_stage_type=stage_type,
        filter_agent_type=agent_type,
    )
    return templates.TemplateResponse("prompts.html", ctx)


@router.post("", response_class=HTMLResponse)
async def create_prompt(
    request: Request,
    name: str = Form(),
    body: str = Form(),
    stage_type: str = Form("custom"),
    agent_type: str = Form("claude"),
):
    from build_your_room.main import templates

    pool = get_pool()
    async with pool.connection() as conn:
        try:
            cur = await conn.execute(
                "INSERT INTO prompts (name, body, stage_type, agent_type) "
                "VALUES (%s, %s, %s, %s) RETURNING *",
                (name, body, stage_type, agent_type),
            )
            prompt: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
            await conn.commit()
        except Exception as exc:
            exc_str = str(exc).lower()
            if "unique" in exc_str or "duplicate" in exc_str:
                error_msg = f"A prompt named '{name}' already exists."
            else:
                error_msg = f"Database error: {exc}"

            usage = await _fetch_prompt_usage()
            prompts = await _fetch_prompts()
            enriched = _enrich_prompts(prompts, usage)
            ctx = _prompt_context(request, enriched, error=error_msg)
            return templates.TemplateResponse(
                "prompts.html", ctx, status_code=422,
            )

    enriched_prompt = {
        **prompt,  # type: ignore[dict-item]
        "used_by": [],
        "variables": extract_template_variables(body),
    }
    return templates.TemplateResponse("partials/prompt_row.html", {
        "request": request, "prompt": enriched_prompt,
    })


@router.put("/{prompt_id}", response_class=HTMLResponse)
async def update_prompt(
    request: Request,
    prompt_id: int,
    name: str = Form(),
    body: str = Form(),
    stage_type: str = Form("custom"),
    agent_type: str = Form("claude"),
):
    from build_your_room.main import templates

    pool = get_pool()
    async with pool.connection() as conn:
        try:
            cur = await conn.execute(
                "UPDATE prompts SET name=%s, body=%s, stage_type=%s, agent_type=%s, "
                "updated_at=now() WHERE id=%s RETURNING *",
                (name, body, stage_type, agent_type, prompt_id),
            )
            prompt: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
            await conn.commit()
        except Exception as exc:
            exc_str = str(exc).lower()
            if "unique" in exc_str or "duplicate" in exc_str:
                error_html = (
                    f'<tr id="prompt-{prompt_id}"><td colspan="6">'
                    f'<div class="prompt-error">A prompt named \'{name}\' already exists.</div>'
                    f'</td></tr>'
                )
            else:
                error_html = (
                    f'<tr id="prompt-{prompt_id}"><td colspan="6">'
                    f'<div class="prompt-error">Database error: {exc}</div>'
                    f'</td></tr>'
                )
            return HTMLResponse(error_html, status_code=422)

    usage = await _fetch_prompt_usage()
    enriched_prompt = {
        **prompt,  # type: ignore[dict-item]
        "used_by": usage.get(name, []),
        "variables": extract_template_variables(body),
    }
    return templates.TemplateResponse("partials/prompt_row.html", {
        "request": request, "prompt": enriched_prompt,
    })


@router.delete("/{prompt_id}", response_class=HTMLResponse)
async def delete_prompt(prompt_id: int):
    pool = get_pool()
    async with pool.connection() as conn:
        # Check if prompt is in use by pipeline defs
        cur = await conn.execute(
            "SELECT name FROM prompts WHERE id=%s", (prompt_id,)
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        if not row:
            return HTMLResponse("")

        prompt_name = row["name"]

    usage = await _fetch_prompt_usage()
    used_by = usage.get(prompt_name, [])
    if used_by:
        defs_list = ", ".join(used_by)
        error_html = (
            f'<tr id="prompt-{prompt_id}"><td colspan="6">'
            f'<div class="prompt-error">Cannot delete: used by pipeline def(s): {defs_list}</div>'
            f'</td></tr>'
        )
        return HTMLResponse(error_html, status_code=409)

    async with pool.connection() as conn:
        await conn.execute("DELETE FROM prompts WHERE id=%s", (prompt_id,))
        await conn.commit()
    return HTMLResponse("")


@router.post("/{prompt_id}/clone", response_class=HTMLResponse)
async def clone_prompt(request: Request, prompt_id: int):
    from build_your_room.main import templates

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT * FROM prompts WHERE id=%s", (prompt_id,)
        )
        source: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        if not source:
            return HTMLResponse("Prompt not found", status_code=404)

        # Generate unique clone name
        base_name = source["name"] + "_copy"
        clone_name = base_name
        suffix = 1
        while True:
            check = await conn.execute(
                "SELECT id FROM prompts WHERE name=%s", (clone_name,)
            )
            if not await check.fetchone():
                break
            suffix += 1
            clone_name = f"{base_name}_{suffix}"

        cur = await conn.execute(
            "INSERT INTO prompts (name, body, stage_type, agent_type) "
            "VALUES (%s, %s, %s, %s) RETURNING *",
            (clone_name, source["body"], source["stage_type"], source["agent_type"]),
        )
        prompt: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        await conn.commit()

    enriched_prompt = {
        **prompt,  # type: ignore[dict-item]
        "used_by": [],
        "variables": extract_template_variables(source["body"]),
    }
    return templates.TemplateResponse("partials/prompt_row.html", {
        "request": request, "prompt": enriched_prompt,
    })


@router.get("/{prompt_id}/edit", response_class=HTMLResponse)
async def edit_prompt_form(request: Request, prompt_id: int):
    from build_your_room.main import templates

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM prompts WHERE id=%s", (prompt_id,))
        prompt: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
    return templates.TemplateResponse("partials/prompt_form.html", {
        "request": request, "prompt": prompt,
        "stage_types": STAGE_TYPES,
        "agent_types": AGENT_TYPES,
    })


@router.get("/{prompt_id}/row", response_class=HTMLResponse)
async def prompt_row(request: Request, prompt_id: int):
    from build_your_room.main import templates

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM prompts WHERE id=%s", (prompt_id,))
        prompt: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]

    usage = await _fetch_prompt_usage()
    enriched_prompt = {
        **prompt,  # type: ignore[dict-item]
        "used_by": usage.get(prompt["name"], []),  # type: ignore[index]
        "variables": extract_template_variables(prompt["body"]),  # type: ignore[index]
    }
    return templates.TemplateResponse("partials/prompt_row.html", {
        "request": request, "prompt": enriched_prompt,
    })
