"""SpecAuthorStage — drives spec authoring with optional review loop.

The stage runner:
1. Loads the prompt template and builds a SessionConfig
2. Creates an agent_sessions DB row for tracking
3. Starts a primary agent session and sends the authoring prompt
4. Saves the resulting spec to the pipeline artifacts directory
5. If the stage node has a review sub-config, runs the review loop
6. Returns "approved" or "escalated" for stage-graph edge resolution
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
from build_your_room.sandbox import WorkspaceSandbox
from build_your_room.stage_graph import StageNode
from build_your_room.stages.review_loop import ReviewLoopOutcome, run_review_loop
from build_your_room.streaming import LogBuffer
from build_your_room.tool_profiles import get_tool_profile

logger = logging.getLogger(__name__)

STAGE_RESULT_APPROVED = "approved"
STAGE_RESULT_ESCALATED = "escalated"

# Default artifact filename for the spec document
_SPEC_ARTIFACT_NAME = "spec.md"


async def run_spec_author_stage(
    *,
    pool: AsyncConnectionPool,
    pipeline_id: int,
    stage_id: int,
    node: StageNode,
    adapters: dict[str, AgentAdapter],
    log_buffer: LogBuffer,
    cancel_event: asyncio.Event,
    pipelines_dir: Path | None = None,
) -> str:
    """Run the spec-author stage: produce a spec, optionally review it.

    Returns ``"approved"`` on success or ``"escalated"`` when the review
    loop exceeds max rounds with an escalation policy.
    """
    base_dir = pipelines_dir or PIPELINES_DIR

    # -- Load pipeline data ---------------------------------------------------
    pipeline = await _load_pipeline(pool, pipeline_id)
    clone_path = pipeline["clone_path"]

    sandbox = WorkspaceSandbox.for_pipeline(clone_path, base_dir, pipeline_id)

    # -- Resolve the authoring prompt -----------------------------------------
    prompt_body = await _resolve_prompt(pool, node.prompt)
    tool_profile = get_tool_profile(node.stage_type)

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
    _log(log_buffer, pipeline_id, "Starting spec author session")

    if cancel_event.is_set():
        await _complete_session(pool, session_db_id, "cancelled")
        return STAGE_RESULT_ESCALATED

    primary_session = await adapter.start_session(session_config)
    try:
        # Update session row with provider session ID
        if primary_session.session_id:
            await _update_session_id(pool, session_db_id, primary_session.session_id)

        # -- Send authoring turn ----------------------------------------------
        turn_result = await primary_session.send_turn(prompt_body)
        artifact_content = turn_result.output

        # -- Save artifact ----------------------------------------------------
        artifact_path = _artifact_path(base_dir, pipeline_id)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(artifact_content)
        await _set_stage_artifact(pool, stage_id, str(artifact_path))

        _log(log_buffer, pipeline_id, f"Spec artifact saved to {artifact_path}")

        if cancel_event.is_set():
            await _complete_session(pool, session_db_id, "cancelled")
            return STAGE_RESULT_ESCALATED

        # -- Optional review loop ---------------------------------------------
        if node.review is not None:
            _log(log_buffer, pipeline_id, "Entering review loop for spec")

            review_adapter = adapters.get(node.review.agent)
            if review_adapter is None:
                _log(
                    log_buffer,
                    pipeline_id,
                    f"No adapter for review agent {node.review.agent!r}, skipping review",
                )
                await _complete_session(pool, session_db_id, "completed")
                return STAGE_RESULT_APPROVED

            review_tool_profile = get_tool_profile("spec_review")
            review_session_config = SessionConfig(
                model=node.review.model,
                clone_path=clone_path,
                system_prompt="You are a spec reviewer.",
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
                artifact_content=artifact_content,
                artifact_type="specification",
                context_monitor=context_monitor,
                stage_context=stage_context,
                log_buffer=log_buffer,
                primary_adapter=adapter,
                primary_session_config=session_config,
            )

            # Persist the final revised artifact if the review produced changes
            if outcome.last_review is not None:
                # Re-read the artifact in case primary agent revised it on disk
                if artifact_path.exists():
                    final_content = artifact_path.read_text()
                    if final_content != artifact_content:
                        _log(log_buffer, pipeline_id, "Spec revised during review loop")

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
                _log(log_buffer, pipeline_id, "Spec review escalated")
                return STAGE_RESULT_ESCALATED

            await _complete_session(pool, session_db_id, "completed")
            _log(
                log_buffer,
                pipeline_id,
                f"Spec approved after {outcome.rounds_completed} review round(s)",
            )
            return STAGE_RESULT_APPROVED

        # No review configured — accept the first draft
        await _complete_session(pool, session_db_id, "completed")
        _log(log_buffer, pipeline_id, "Spec authored (no review configured)")
        return STAGE_RESULT_APPROVED

    except Exception:
        await _complete_session(pool, session_db_id, "failed")
        raise
    finally:
        await primary_session.close()


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
    """Insert an agent_sessions row and return its ID."""
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
    return pipelines_dir / str(pipeline_id) / "artifacts" / _SPEC_ARTIFACT_NAME


def _log(log_buffer: LogBuffer, pipeline_id: int, message: str) -> None:
    log_buffer.append(pipeline_id, f"[spec_author] {message}")
