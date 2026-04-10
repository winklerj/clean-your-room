"""Pipeline definition builder routes — list, create pipeline definitions."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from build_your_room.db import get_pool
from build_your_room.stage_graph import StageGraph, StageGraphError

router = APIRouter()

STAGE_TYPES = [
    "spec_author", "spec_review", "impl_plan", "impl_plan_review",
    "impl_task", "code_review", "bug_fix", "validation", "custom",
]

AGENT_TYPES = ["claude", "codex"]

EDGE_CONDITIONS = [
    "approved", "stage_complete", "validation_failed", "validated",
]

ON_MAX_ROUNDS_OPTIONS = ["escalate", "proceed_with_warnings"]
ON_CONTEXT_LIMIT_OPTIONS = ["resume_current_claim", "new_session_continue", "escalate"]
EXIT_CONDITION_OPTIONS = ["structured_approval", "proceed_with_warnings"]
ON_EXHAUSTED_OPTIONS = ["escalate"]


async def _fetch_pipeline_defs() -> list[dict[str, Any]]:
    """Fetch all pipeline definitions enriched with node/edge counts."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT * FROM pipeline_defs ORDER BY created_at DESC"
        )
        rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

    enriched: list[dict[str, Any]] = []
    for row in rows:
        graph: dict[str, Any] = {}
        try:
            graph = json.loads(row.get("stage_graph_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            pass
        enriched.append({
            **row,
            "node_count": len(graph.get("nodes", [])),
            "edge_count": len(graph.get("edges", [])),
            "entry_stage": graph.get("entry_stage", ""),
        })
    return enriched


async def _fetch_prompts() -> list[dict[str, Any]]:
    """Fetch prompt names for the builder form reference."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT name, stage_type, agent_type FROM prompts ORDER BY name"
        )
        rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
    return rows


def _builder_context(
    *,
    pipeline_defs: list[dict[str, Any]],
    prompts: list[dict[str, Any]],
    error: str | None = None,
) -> dict[str, Any]:
    """Assemble the template context for the pipeline builder page."""
    return {
        "pipeline_defs": pipeline_defs,
        "prompts": prompts,
        "stage_types": STAGE_TYPES,
        "agent_types": AGENT_TYPES,
        "edge_conditions": EDGE_CONDITIONS,
        "on_max_rounds_options": ON_MAX_ROUNDS_OPTIONS,
        "on_context_limit_options": ON_CONTEXT_LIMIT_OPTIONS,
        "exit_condition_options": EXIT_CONDITION_OPTIONS,
        "on_exhausted_options": ON_EXHAUSTED_OPTIONS,
        "error": error,
    }


def _parse_nodes_from_form(form: dict[str, str]) -> list[dict[str, Any]]:
    """Parse indexed node fields (node_0_key, node_0_name, ...) into node dicts."""
    indices: set[int] = set()
    for key in form:
        if key.startswith("node_") and key.endswith("_key"):
            parts = key.split("_")
            if len(parts) >= 3:
                try:
                    indices.add(int(parts[1]))
                except ValueError:
                    pass

    nodes: list[dict[str, Any]] = []
    for idx in sorted(indices):
        p = f"node_{idx}_"
        key = form.get(f"{p}key", "").strip()
        if not key:
            continue

        node: dict[str, Any] = {
            "key": key,
            "name": form.get(f"{p}name", key).strip() or key,
            "type": form.get(f"{p}type", "custom"),
            "agent": form.get(f"{p}agent", "claude"),
            "prompt": form.get(f"{p}prompt", "").strip(),
            "model": form.get(f"{p}model", "claude-sonnet-4-6").strip(),
            "max_iterations": int(form.get(f"{p}max_iterations") or "1"),
            "context_threshold_pct": int(
                form.get(f"{p}context_threshold_pct") or "60"
            ),
        }

        on_context_limit = form.get(f"{p}on_context_limit", "")
        if on_context_limit:
            node["on_context_limit"] = on_context_limit

        on_max_rounds = form.get(f"{p}on_max_rounds", "")
        if on_max_rounds:
            node["on_max_rounds"] = on_max_rounds

        fix_agent = form.get(f"{p}fix_agent", "")
        if fix_agent:
            node["fix_agent"] = fix_agent

        fix_prompt = form.get(f"{p}fix_prompt", "").strip()
        if fix_prompt:
            node["fix_prompt"] = fix_prompt

        if form.get(f"{p}uses_devbrowser") == "on":
            node["uses_devbrowser"] = True

        if form.get(f"{p}record_on_success") == "on":
            node["record_on_success"] = True

        # Review sub-config (only if review agent is specified)
        review_agent = form.get(f"{p}review_agent", "").strip()
        if review_agent:
            node["review"] = {
                "agent": review_agent,
                "prompt": form.get(f"{p}review_prompt", "").strip(),
                "model": form.get(f"{p}review_model", "").strip(),
                "max_review_rounds": int(
                    form.get(f"{p}review_max_rounds") or "5"
                ),
                "exit_condition": form.get(
                    f"{p}review_exit_condition", "structured_approval"
                ),
                "on_max_rounds": form.get(
                    f"{p}review_on_max_rounds", "escalate"
                ),
            }

        nodes.append(node)

    return nodes


def _parse_edges_from_form(form: dict[str, str]) -> list[dict[str, Any]]:
    """Parse indexed edge fields (edge_0_key, edge_0_from, ...) into edge dicts."""
    indices: set[int] = set()
    for key in form:
        if key.startswith("edge_") and key.endswith("_key"):
            parts = key.split("_")
            if len(parts) >= 3:
                try:
                    indices.add(int(parts[1]))
                except ValueError:
                    pass

    edges: list[dict[str, Any]] = []
    for idx in sorted(indices):
        p = f"edge_{idx}_"
        key = form.get(f"{p}key", "").strip()
        if not key:
            continue

        edge: dict[str, Any] = {
            "key": key,
            "from": form.get(f"{p}from", "").strip(),
            "to": form.get(f"{p}to", "").strip(),
            "on": form.get(f"{p}on", "").strip(),
        }

        max_visits = form.get(f"{p}max_visits", "").strip()
        if max_visits:
            edge["max_visits"] = int(max_visits)

        on_exhausted = form.get(f"{p}on_exhausted", "").strip()
        if on_exhausted:
            edge["on_exhausted"] = on_exhausted

        edges.append(edge)

    return edges


def _build_stage_graph_json(
    entry_stage: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> tuple[str | None, str]:
    """Assemble and validate stage graph JSON.

    Returns (error_message, json_string). error_message is None on success.
    """
    stage_graph = {
        "entry_stage": entry_stage,
        "nodes": nodes,
        "edges": edges,
    }

    try:
        StageGraph.from_json(stage_graph)
    except StageGraphError as e:
        return str(e), ""
    except (KeyError, TypeError, ValueError) as e:
        return f"Invalid stage graph data: {e}", ""

    return None, json.dumps(stage_graph)


async def _fetch_pipeline_def_detail(def_id: int) -> dict[str, Any] | None:
    """Fetch a single pipeline def with parsed stage graph for the detail page."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT * FROM pipeline_defs WHERE id = %s", (def_id,)
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]

    if row is None:
        return None

    graph: dict[str, Any] = {}
    try:
        graph = json.loads(row.get("stage_graph_json", "{}"))
    except (json.JSONDecodeError, TypeError):
        pass

    nodes: list[dict[str, Any]] = graph.get("nodes", [])
    edges: list[dict[str, Any]] = graph.get("edges", [])
    entry_stage = graph.get("entry_stage", "")

    return {
        "pipeline_def": row,
        "entry_stage": entry_stage,
        "nodes": nodes,
        "edges": edges,
    }


@router.get("/pipeline-defs", response_class=HTMLResponse)
async def list_pipeline_defs(request: Request):
    from build_your_room.main import templates

    defs = await _fetch_pipeline_defs()
    prompts = await _fetch_prompts()
    ctx = _builder_context(pipeline_defs=defs, prompts=prompts)
    return templates.TemplateResponse(request, "pipeline_builder.html", ctx)


@router.get("/pipeline-defs/new", response_class=HTMLResponse)
async def pipeline_builder_form(request: Request):
    from build_your_room.main import templates

    prompts = await _fetch_prompts()
    ctx = _builder_context(pipeline_defs=[], prompts=prompts)
    return templates.TemplateResponse(request, "pipeline_builder.html", ctx)


@router.get("/pipeline-defs/{def_id}/preview", response_class=HTMLResponse)
async def pipeline_def_preview(def_id: int):
    """Return an HTML fragment summarizing a pipeline definition's stages and flow."""
    data = await _fetch_pipeline_def_detail(def_id)
    if data is None:
        return HTMLResponse("")

    nodes: list[dict[str, Any]] = data["nodes"]
    edges: list[dict[str, Any]] = data["edges"]
    entry = data["entry_stage"]

    lines: list[str] = ['<div class="def-preview">']
    lines.append(f'<div class="def-preview-header">{len(nodes)} stage'
                 f'{"s" if len(nodes) != 1 else ""}, '
                 f'{len(edges)} transition{"s" if len(edges) != 1 else ""}</div>')
    lines.append('<div class="def-preview-flow">')
    for node in nodes:
        marker = " (entry)" if node.get("key") == entry else ""
        lines.append(
            f'<div class="def-preview-stage">'
            f'<span class="def-preview-name">{node.get("name", node.get("key", "?"))}</span>'
            f'<span class="def-preview-type">{node.get("type", "")}{marker}</span>'
            f'<span class="def-preview-agent">{node.get("agent", "")}</span>'
            f'</div>'
        )
    lines.append('</div>')
    if edges:
        lines.append('<div class="def-preview-edges">')
        for edge in edges:
            lines.append(
                f'<div class="def-preview-edge">'
                f'{edge.get("from", "?")} &rarr; {edge.get("to", "?")} '
                f'<span class="def-preview-cond">on {edge.get("on", "?")}</span>'
                f'</div>'
            )
        lines.append('</div>')
    lines.append('</div>')
    return HTMLResponse("\n".join(lines))


@router.get("/pipeline-defs/{def_id}", response_class=HTMLResponse)
async def pipeline_def_detail(request: Request, def_id: int):
    from build_your_room.main import templates

    data = await _fetch_pipeline_def_detail(def_id)
    if data is None:
        return HTMLResponse("<h1>Pipeline definition not found</h1>", status_code=404)

    return templates.TemplateResponse(request, "pipeline_def_detail.html", data)


@router.post("/pipeline-defs")
async def create_pipeline_def(request: Request):
    from build_your_room.main import templates

    form_data = await request.form()
    form = {k: v for k, v in form_data.items() if isinstance(v, str)}

    name = form.get("name", "").strip()
    entry_stage = form.get("entry_stage", "").strip()

    nodes = _parse_nodes_from_form(form)
    edges = _parse_edges_from_form(form)

    # Validation
    error: str | None = None
    if not name:
        error = "Pipeline definition name is required."
    elif not entry_stage:
        error = "Entry stage is required."
    elif not nodes:
        error = "At least one stage node is required."
    else:
        error, stage_graph_json = _build_stage_graph_json(
            entry_stage, nodes, edges,
        )

    if error:
        prompts = await _fetch_prompts()
        ctx = _builder_context(pipeline_defs=[], prompts=prompts, error=error)
        return templates.TemplateResponse(
            request, "pipeline_builder.html", ctx, status_code=422,
        )

    pool = get_pool()
    async with pool.connection() as conn:
        try:
            await conn.execute(
                "INSERT INTO pipeline_defs (name, stage_graph_json) "
                "VALUES (%s, %s)",
                (name, stage_graph_json),
            )
            await conn.commit()
        except Exception as exc:
            exc_str = str(exc).lower()
            if "unique" in exc_str or "duplicate" in exc_str:
                error = f"A pipeline definition named '{name}' already exists."
            else:
                error = f"Database error: {exc}"

            prompts = await _fetch_prompts()
            ctx = _builder_context(pipeline_defs=[], prompts=prompts, error=error)
            return templates.TemplateResponse(
                request, "pipeline_builder.html", ctx, status_code=422,
            )

    return RedirectResponse(url="/pipeline-defs", status_code=303)
