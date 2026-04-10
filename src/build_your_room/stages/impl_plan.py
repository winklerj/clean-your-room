"""ImplPlanStage — drives implementation planning with review loop and HTN population.

The stage runner:
1. Loads the prompt template and the spec artifact from the previous stage
2. Creates an agent_sessions DB row for tracking
3. Starts a primary agent session and sends the planning prompt + HTN output schema
4. Parses the plan document and structured HTN task list from the output
5. Saves the plan artifact to the pipeline artifacts directory
6. If the stage node has a review sub-config, runs the review loop
7. Populates the HTN task graph via HTNPlanner.populate_from_structured_output()
8. Syncs the task list to markdown in the clone for agent readability
9. Returns "approved" or "escalated" for stage-graph edge resolution
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from psycopg_pool import AsyncConnectionPool

from build_your_room.adapters.base import AgentAdapter, SessionConfig
from build_your_room.config import PIPELINES_DIR
from build_your_room.context_monitor import ContextMonitor, StageContext
from build_your_room.htn_planner import HTNPlanner
from build_your_room.sandbox import WorkspaceSandbox
from build_your_room.stage_graph import StageNode
from build_your_room.stages.review_loop import ReviewLoopOutcome, run_review_loop
from build_your_room.streaming import LogBuffer
from build_your_room.tool_profiles import get_tool_profile

logger = logging.getLogger(__name__)

STAGE_RESULT_APPROVED = "approved"
STAGE_RESULT_ESCALATED = "escalated"

_PLAN_ARTIFACT_NAME = "plan.md"

# JSON schema appended to the planning prompt so the agent returns
# structured HTN tasks alongside the plan document.
HTN_TASK_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "plan_markdown": {
            "type": "string",
            "description": "The full implementation plan as markdown.",
        },
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "task_type": {
                        "type": "string",
                        "enum": ["compound", "primitive", "decision"],
                    },
                    "parent_name": {"type": ["string", "null"]},
                    "priority": {"type": "integer"},
                    "ordering": {"type": "integer"},
                    "preconditions": {"type": "array", "items": {"type": "object"}},
                    "postconditions": {"type": "array", "items": {"type": "object"}},
                    "invariants": {
                        "type": ["array", "null"],
                        "items": {"type": "object"},
                    },
                    "estimated_complexity": {
                        "type": ["string", "null"],
                        "enum": [
                            "trivial",
                            "small",
                            "medium",
                            "large",
                            "epic",
                            None,
                        ],
                    },
                    "dependencies": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["name", "description"],
            },
        },
    },
    "required": ["plan_markdown", "tasks"],
}


def parse_htn_tasks(structured: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """Extract the HTN task list from structured agent output.

    Returns None if the output is missing or malformed.
    """
    if structured is None:
        return None
    try:
        tasks = structured.get("tasks")
        if not isinstance(tasks, list):
            return None
        for task in tasks:
            if not isinstance(task, dict):
                return None
            if "name" not in task or "description" not in task:
                return None
        return tasks
    except (AttributeError, TypeError):
        logger.warning("Failed to parse HTN task structured output: %s", structured)
        return None


async def run_impl_plan_stage(
    *,
    pool: AsyncConnectionPool,
    pipeline_id: int,
    stage_id: int,
    node: StageNode,
    adapters: dict[str, AgentAdapter],
    log_buffer: LogBuffer,
    cancel_event: asyncio.Event,
    pipelines_dir: Path | None = None,
    htn_planner: HTNPlanner | None = None,
) -> str:
    """Run the impl-plan stage: produce a plan, optionally review it, populate HTN tasks.

    Returns ``"approved"`` on success or ``"escalated"`` when the review
    loop exceeds max rounds with an escalation policy.
    """
    base_dir = pipelines_dir or PIPELINES_DIR

    # -- Load pipeline data ---------------------------------------------------
    pipeline = await _load_pipeline(pool, pipeline_id)
    clone_path = pipeline["clone_path"]

    sandbox = WorkspaceSandbox.for_pipeline(clone_path, base_dir, pipeline_id)

    # -- Resolve the planning prompt ------------------------------------------
    prompt_body = await _resolve_prompt(pool, node.prompt)
    tool_profile = get_tool_profile(node.stage_type)

    # Append the spec artifact content as context if available
    spec_context = _load_spec_artifact(base_dir, pipeline_id)
    if spec_context:
        prompt_body = (
            f"{prompt_body}\n\n"
            f"## Spec document (from previous stage)\n\n{spec_context}"
        )

    session_config = SessionConfig(
        model=node.model,
        clone_path=clone_path,
        system_prompt=prompt_body,
        allowed_tools=list(tool_profile.all_tools),
        allowed_roots=sandbox.writable_roots_list,
        context_threshold_pct=float(node.context_threshold_pct),
        pipeline_id=pipeline_id,
        stage_id=stage_id,
    )

    # -- Get the adapter ------------------------------------------------------
    adapter = adapters.get(node.agent)
    if adapter is None:
        _log(log_buffer, pipeline_id, f"No adapter for agent type {node.agent!r}, escalating")
        return STAGE_RESULT_ESCALATED

    # -- Create agent_sessions row --------------------------------------------
    session_db_id = await _create_session_row(
        pool, stage_id, node.agent, prompt_body
    )

    # -- Start primary session ------------------------------------------------
    _log(log_buffer, pipeline_id, "Starting impl plan session")

    if cancel_event.is_set():
        await _complete_session(pool, session_db_id, "cancelled")
        return STAGE_RESULT_ESCALATED

    primary_session = await adapter.start_session(session_config)
    try:
        if primary_session.session_id:
            await _update_session_id(pool, session_db_id, primary_session.session_id)

        # -- Send planning turn -----------------------------------------------
        turn_result = await primary_session.send_turn(
            prompt_body, output_schema=HTN_TASK_OUTPUT_SCHEMA
        )

        # Extract plan markdown and task list from structured or raw output
        plan_content, tasks_json = _parse_plan_output(turn_result)

        # -- Save plan artifact -----------------------------------------------
        artifact_path = _artifact_path(base_dir, pipeline_id)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(plan_content)
        await _set_stage_artifact(pool, stage_id, str(artifact_path))

        _log(log_buffer, pipeline_id, f"Plan artifact saved to {artifact_path}")

        if cancel_event.is_set():
            await _complete_session(pool, session_db_id, "cancelled")
            return STAGE_RESULT_ESCALATED

        # -- Optional review loop ---------------------------------------------
        if node.review is not None:
            _log(log_buffer, pipeline_id, "Entering review loop for plan")

            review_adapter = adapters.get(node.review.agent)
            if review_adapter is None:
                _log(
                    log_buffer,
                    pipeline_id,
                    f"No adapter for review agent {node.review.agent!r}, skipping review",
                )
            else:
                review_tool_profile = get_tool_profile("impl_plan_review")
                review_session_config = SessionConfig(
                    model=node.review.model,
                    clone_path=clone_path,
                    system_prompt="You are an implementation plan reviewer.",
                    allowed_tools=list(review_tool_profile.all_tools),
                    allowed_roots=sandbox.writable_roots_list,
                    pipeline_id=pipeline_id,
                    stage_id=stage_id,
                )

                context_monitor = ContextMonitor(
                    threshold_pct=float(node.context_threshold_pct),
                )
                stage_context = StageContext(
                    stage_type=node.stage_type,
                    pipeline_id=pipeline_id,
                    stage_id=stage_id,
                    session_id=session_db_id,
                    artifact_path=str(artifact_path),
                )

                outcome: ReviewLoopOutcome = await run_review_loop(
                    primary_session=primary_session,
                    review_adapter=review_adapter,
                    review_config=node.review,
                    review_session_config=review_session_config,
                    artifact_content=plan_content,
                    artifact_type="implementation plan",
                    context_monitor=context_monitor,
                    stage_context=stage_context,
                    log_buffer=log_buffer,
                    primary_adapter=adapter,
                    primary_session_config=session_config,
                )

                # Re-read the artifact in case the primary revised it during review
                if outcome.last_review is not None and artifact_path.exists():
                    revised_content = artifact_path.read_text()
                    if revised_content != plan_content:
                        plan_content = revised_content
                        _log(log_buffer, pipeline_id, "Plan revised during review loop")

                await _update_stage_status(
                    pool,
                    stage_id,
                    "completed" if outcome.approved else "failed",
                    escalation_reason=outcome.escalation_reason,
                )

                if outcome.escalated:
                    await _create_escalation(
                        pool,
                        pipeline_id,
                        stage_id,
                        outcome.escalation_reason or "max_iterations",
                        {
                            "rounds_completed": outcome.rounds_completed,
                            "warnings_proceeded": outcome.warnings_proceeded,
                            "artifact_path": str(artifact_path),
                        },
                    )
                    await _complete_session(pool, session_db_id, "completed")
                    _log(log_buffer, pipeline_id, "Plan review escalated")
                    return STAGE_RESULT_ESCALATED

                _log(
                    log_buffer,
                    pipeline_id,
                    f"Plan approved after {outcome.rounds_completed} review round(s)",
                )

        # -- Populate HTN task graph ------------------------------------------
        planner = htn_planner or HTNPlanner(pool)
        if tasks_json:
            created_ids = await planner.populate_from_structured_output(
                pipeline_id, tasks_json
            )
            _log(
                log_buffer,
                pipeline_id,
                f"HTN task graph populated with {len(created_ids)} tasks",
            )
            # Sync task list to markdown for agent readability
            await planner.sync_to_markdown(pipeline_id, clone_path)
            _log(log_buffer, pipeline_id, "Task list synced to markdown")
        else:
            _log(log_buffer, pipeline_id, "No HTN tasks in plan output, skipping population")

        await _complete_session(pool, session_db_id, "completed")
        _log(log_buffer, pipeline_id, "Plan authored and tasks populated")
        return STAGE_RESULT_APPROVED

    except Exception:
        await _complete_session(pool, session_db_id, "failed")
        raise
    finally:
        await primary_session.close()


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


def _parse_plan_output(
    turn_result: Any,
) -> tuple[str, list[dict[str, Any]]]:
    """Extract plan markdown and tasks list from the agent's turn result.

    Handles both structured output (preferred) and raw text fallback.
    Returns (plan_markdown, tasks_json).
    """
    structured = getattr(turn_result, "structured_output", None)

    if structured and isinstance(structured, dict):
        plan_md = structured.get("plan_markdown", "")
        tasks = structured.get("tasks", [])
        if isinstance(tasks, list):
            return str(plan_md), tasks

    # Fallback: try to extract JSON from the raw output
    raw_output = turn_result.output
    tasks = _try_extract_tasks_json(raw_output)
    return raw_output, tasks


def _try_extract_tasks_json(text: str) -> list[dict[str, Any]]:
    """Try to extract a JSON tasks array from raw text output.

    Looks for a ```json fenced block or a top-level JSON object/array.
    Returns an empty list if parsing fails.
    """
    import re

    # Try fenced JSON block
    match = re.search(r"```json\s*\n(.*?)```", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict) and "tasks" in parsed:
                return parsed["tasks"]
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, KeyError):
            pass

    # Try finding a JSON object with "tasks" key
    for start_char in ("{", "["):
        idx = text.find(start_char)
        if idx >= 0:
            try:
                parsed = json.loads(text[idx:])
                if isinstance(parsed, dict) and "tasks" in parsed:
                    return parsed["tasks"]
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, KeyError):
                pass

    return []


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _load_pipeline(
    pool: AsyncConnectionPool, pipeline_id: int
) -> dict[str, Any]:
    async with pool.connection() as conn:
        row: dict[str, Any] | None = await (  # type: ignore[assignment]
            await conn.execute(
                "SELECT clone_path, review_base_rev, head_rev, config_json "
                "FROM pipelines WHERE id = %s",
                (pipeline_id,),
            )
        ).fetchone()
    if not row:
        raise ValueError(f"Pipeline {pipeline_id} not found")
    return dict(row)


async def _resolve_prompt(pool: AsyncConnectionPool, prompt_name: str) -> str:
    """Look up a prompt template by name, falling back to the name itself."""
    async with pool.connection() as conn:
        row: dict[str, Any] | None = await (  # type: ignore[assignment]
            await conn.execute(
                "SELECT body FROM prompts WHERE name = %s", (prompt_name,)
            )
        ).fetchone()
    if row:
        return row["body"]
    return prompt_name


async def _create_session_row(
    pool: AsyncConnectionPool,
    stage_id: int,
    agent_type: str,
    prompt_override: str,
) -> int:
    async with pool.connection() as conn:
        row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO agent_sessions "
                "(pipeline_stage_id, session_type, prompt_override, status) "
                "VALUES (%s, %s, %s, 'running') RETURNING id",
                (stage_id, agent_type, prompt_override),
            )
        ).fetchone()
        await conn.commit()
    return row["id"]


async def _update_session_id(
    pool: AsyncConnectionPool, session_db_id: int, session_id: str
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agent_sessions SET session_id = %s WHERE id = %s",
            (session_id, session_db_id),
        )
        await conn.commit()


async def _complete_session(
    pool: AsyncConnectionPool, session_db_id: int, status: str
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agent_sessions SET status = %s, completed_at = now() WHERE id = %s",
            (status, session_db_id),
        )
        await conn.commit()


async def _set_stage_artifact(
    pool: AsyncConnectionPool, stage_id: int, artifact_path: str
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE pipeline_stages SET output_artifact = %s WHERE id = %s",
            (artifact_path, stage_id),
        )
        await conn.commit()


async def _update_stage_status(
    pool: AsyncConnectionPool,
    stage_id: int,
    status: str,
    *,
    escalation_reason: str | None = None,
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE pipeline_stages SET status = %s, escalation_reason = %s, "
            "completed_at = now() WHERE id = %s",
            (status, escalation_reason, stage_id),
        )
        await conn.commit()


async def _create_escalation(
    pool: AsyncConnectionPool,
    pipeline_id: int,
    stage_id: int,
    reason: str,
    context: dict[str, Any],
) -> int:
    async with pool.connection() as conn:
        row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO escalations "
                "(pipeline_id, pipeline_stage_id, reason, context_json) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (pipeline_id, stage_id, reason, json.dumps(context)),
            )
        ).fetchone()
        await conn.commit()
    return row["id"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _artifact_path(pipelines_dir: Path, pipeline_id: int) -> Path:
    return pipelines_dir / str(pipeline_id) / "artifacts" / _PLAN_ARTIFACT_NAME


def _load_spec_artifact(pipelines_dir: Path, pipeline_id: int) -> str | None:
    """Load the spec artifact from the previous stage, if it exists."""
    spec_path = pipelines_dir / str(pipeline_id) / "artifacts" / "spec.md"
    if spec_path.exists():
        return spec_path.read_text()
    return None


def _log(log_buffer: LogBuffer, pipeline_id: int, message: str) -> None:
    log_buffer.append(pipeline_id, f"[impl_plan] {message}")


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

from build_your_room.stages.base import register_stage_runner  # noqa: E402

register_stage_runner("impl_plan", run_impl_plan_stage)
