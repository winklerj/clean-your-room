"""CodeReviewStage — drives code review of the full pipeline diff with a bug-fix loop.

The stage runner:
1. Captures the full diff from review_base_rev to head_rev (ReviewCoversHead invariant)
2. Sends the diff to a review agent (typically Codex) with structured output
3. Parses the structured review result (approved, max_severity, issues)
4. If approved (approved=True AND max_severity in [none, low]) → return "approved"
5. Otherwise invokes the fix_agent to address reported issues
6. Loops up to max_iterations, then escalates or proceeds per on_max_rounds
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from psycopg_pool import AsyncConnectionPool

from build_your_room.adapters.base import AgentAdapter, SessionConfig
from build_your_room.command_registry import (
    CommandRegistry,
    get_default_command_registry,
)
from build_your_room.config import PIPELINES_DIR
from build_your_room.harness_mcp import session_mcp_servers_for
from build_your_room.sandbox import WorkspaceSandbox
from build_your_room.stage_graph import StageNode
from build_your_room.stages.review_loop import (
    REVIEW_OUTPUT_SCHEMA,
    ReviewResult,
    parse_review_result,
    should_approve,
)
from build_your_room.streaming import LogBuffer
from build_your_room.tool_profiles import get_tool_profile

logger = logging.getLogger(__name__)

STAGE_RESULT_APPROVED = "approved"
STAGE_RESULT_ESCALATED = "escalated"

# Artifact subdirectory for review diffs
_REVIEW_ARTIFACT_DIR = "review"
_DIFF_ARTIFACT_NAME = "full_diff.patch"


async def run_code_review_stage(
    *,
    pool: AsyncConnectionPool,
    pipeline_id: int,
    stage_id: int,
    node: StageNode,
    adapters: dict[str, AgentAdapter],
    log_buffer: LogBuffer,
    cancel_event: asyncio.Event,
    pipelines_dir: Path | None = None,
    command_registry: CommandRegistry | None = None,
) -> str:
    """Run the code-review stage: review the full diff and fix issues.

    Returns ``"approved"`` on success or ``"escalated"`` when the review/fix
    loop exceeds max rounds with an escalation policy.
    """
    base_dir = pipelines_dir or PIPELINES_DIR

    pipeline = await _load_pipeline(pool, pipeline_id)
    clone_path = pipeline["clone_path"]
    review_base_rev = pipeline["review_base_rev"]
    head_rev = pipeline["head_rev"] or review_base_rev

    sandbox = WorkspaceSandbox.for_pipeline(clone_path, base_dir, pipeline_id)
    cmd_reg = command_registry or get_default_command_registry()

    # -- Resolve prompts -------------------------------------------------------
    review_prompt_body = await _resolve_prompt(pool, node.prompt)
    fix_prompt_body = await _resolve_prompt(pool, node.fix_prompt or "bug_fix_default")

    # -- Get adapters ----------------------------------------------------------
    review_adapter = adapters.get(node.agent)
    if review_adapter is None:
        _log(log_buffer, pipeline_id, f"No adapter for review agent {node.agent!r}, escalating")
        return STAGE_RESULT_ESCALATED

    fix_agent_type = node.fix_agent or node.agent
    fix_adapter = adapters.get(fix_agent_type)
    if fix_adapter is None:
        _log(log_buffer, pipeline_id, f"No adapter for fix agent {fix_agent_type!r}, escalating")
        return STAGE_RESULT_ESCALATED

    # -- Capture full diff (ReviewCoversHead invariant) ------------------------
    diff_text = await _capture_full_diff(clone_path, review_base_rev, head_rev)

    if not diff_text.strip():
        _log(log_buffer, pipeline_id, "No changes to review — approving")
        return STAGE_RESULT_APPROVED

    # Persist the diff as an artifact
    diff_artifact_path = _diff_artifact_path(base_dir, pipeline_id)
    diff_artifact_path.parent.mkdir(parents=True, exist_ok=True)
    diff_artifact_path.write_text(diff_text)
    await _set_stage_artifact(pool, stage_id, str(diff_artifact_path))

    _log(log_buffer, pipeline_id, f"Captured diff ({len(diff_text)} chars) for review")

    # -- Tool profiles ---------------------------------------------------------
    review_tool_profile = get_tool_profile(node.stage_type)
    fix_tool_profile = get_tool_profile("bug_fix")

    review_session_config = SessionConfig(
        model=node.model,
        clone_path=clone_path,
        system_prompt=review_prompt_body,
        allowed_tools=list(review_tool_profile.all_tools),
        allowed_roots=sandbox.writable_roots_list,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        mcp_servers=session_mcp_servers_for(
            node.agent,
            clone_path=clone_path,
            allowed_roots=sandbox.writable_roots_list,
            command_registry=cmd_reg,
        ),
    )

    fix_session_config = SessionConfig(
        model=node.model,
        clone_path=clone_path,
        system_prompt=fix_prompt_body,
        allowed_tools=list(fix_tool_profile.all_tools),
        allowed_roots=sandbox.writable_roots_list,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        mcp_servers=session_mcp_servers_for(
            fix_agent_type,
            clone_path=clone_path,
            allowed_roots=sandbox.writable_roots_list,
            command_registry=cmd_reg,
        ),
    )

    # -- Review/fix loop -------------------------------------------------------
    max_rounds = node.max_iterations
    current_diff = diff_text

    for round_num in range(1, max_rounds + 1):
        if cancel_event.is_set():
            _log(log_buffer, pipeline_id, "Cancelled before review round")
            return STAGE_RESULT_ESCALATED

        _log(log_buffer, pipeline_id, f"Review round {round_num}/{max_rounds}")

        # --- Review turn ---
        review_session_db_id = await _create_session_row(
            pool, stage_id, node.agent, review_prompt_body
        )

        review_result = await _run_review_turn(
            adapter=review_adapter,
            session_config=review_session_config,
            diff_text=current_diff,
            review_prompt=review_prompt_body,
            pool=pool,
            session_db_id=review_session_db_id,
        )

        if review_result is None:
            _log(log_buffer, pipeline_id, "Review returned unparseable output, escalating")
            await _complete_session(pool, review_session_db_id, "failed")
            await _create_escalation(
                pool, pipeline_id, stage_id, "review_divergence",
                {"message": "Review output could not be parsed", "round": round_num},
            )
            return STAGE_RESULT_ESCALATED

        _log(
            log_buffer, pipeline_id,
            f"Review round {round_num}: approved={review_result.approved}, "
            f"max_severity={review_result.max_severity}, "
            f"issues={len(review_result.issues)}",
        )

        # --- Decision gate ---
        if should_approve(review_result):
            await _complete_session(pool, review_session_db_id, "completed")
            _log(log_buffer, pipeline_id, f"Code review approved after {round_num} round(s)")
            return STAGE_RESULT_APPROVED

        await _complete_session(pool, review_session_db_id, "completed")

        # Last round — handle max-rounds policy without fixing
        if round_num >= max_rounds:
            return await _handle_max_rounds(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                review_result=review_result,
                rounds_completed=round_num,
                log_buffer=log_buffer,
            )

        if cancel_event.is_set():
            _log(log_buffer, pipeline_id, "Cancelled before fix round")
            return STAGE_RESULT_ESCALATED

        # --- Fix turn ---
        _log(log_buffer, pipeline_id, f"Sending {len(review_result.issues)} issues to fix agent")

        fix_session_db_id = await _create_session_row(
            pool, stage_id, fix_agent_type, fix_prompt_body
        )

        fix_prompt = _build_fix_prompt(fix_prompt_body, review_result)
        await _run_fix_turn(
            adapter=fix_adapter,
            session_config=fix_session_config,
            fix_prompt=fix_prompt,
            pool=pool,
            session_db_id=fix_session_db_id,
        )

        # Re-capture the diff after fixes
        current_diff = await _capture_full_diff(clone_path, review_base_rev, head_rev)

        if not current_diff.strip():
            _log(log_buffer, pipeline_id, "Diff empty after fixes — approving")
            return STAGE_RESULT_APPROVED

    # Should not reach here (loop handles max_rounds), but defensive
    return STAGE_RESULT_ESCALATED


# ---------------------------------------------------------------------------
# Review and fix turns
# ---------------------------------------------------------------------------


async def _run_review_turn(
    *,
    adapter: AgentAdapter,
    session_config: SessionConfig,
    diff_text: str,
    review_prompt: str,
    pool: AsyncConnectionPool,
    session_db_id: int,
) -> ReviewResult | None:
    """Create a review session, send the diff for review, parse the result."""
    prompt = (
        f"{review_prompt}\n\n"
        "## Code Diff to Review\n\n"
        "```diff\n"
        f"{diff_text}\n"
        "```\n\n"
        "Return structured JSON output with your assessment."
    )

    session = await adapter.start_session(session_config)
    try:
        if session.session_id:
            await _update_session_id(pool, session_db_id, session.session_id)
        turn_result = await session.send_turn(prompt, output_schema=REVIEW_OUTPUT_SCHEMA)
        return parse_review_result(turn_result.structured_output)
    finally:
        await session.close()


async def _run_fix_turn(
    *,
    adapter: AgentAdapter,
    session_config: SessionConfig,
    fix_prompt: str,
    pool: AsyncConnectionPool,
    session_db_id: int,
) -> None:
    """Create a fix session, send the fix prompt, let the agent make changes."""
    session = await adapter.start_session(session_config)
    try:
        if session.session_id:
            await _update_session_id(pool, session_db_id, session.session_id)
        await session.send_turn(fix_prompt)
        await _complete_session(pool, session_db_id, "completed")
    except Exception:
        await _complete_session(pool, session_db_id, "failed")
        raise
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def _build_fix_prompt(base_prompt: str, review_result: ReviewResult) -> str:
    """Build the bug-fix prompt from review feedback."""
    parts = [
        base_prompt,
        "\n\n## Code Review Issues to Fix\n",
        f"Max severity: {review_result.max_severity}\n",
    ]

    if review_result.issues:
        parts.append(f"\nIssues ({len(review_result.issues)}):\n")
        for i, issue in enumerate(review_result.issues, 1):
            loc = ""
            if issue.file:
                loc = f" ({issue.file}"
                if issue.line is not None:
                    loc += f":{issue.line}"
                loc += ")"
            parts.append(f"  {i}. [{issue.severity}] {issue.description}{loc}\n")

    if review_result.feedback_markdown:
        parts.append(f"\nDetailed feedback:\n{review_result.feedback_markdown}\n")

    parts.append("\nPlease fix all reported issues in the codebase.")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Max-rounds handling
# ---------------------------------------------------------------------------


async def _handle_max_rounds(
    *,
    pool: AsyncConnectionPool,
    pipeline_id: int,
    stage_id: int,
    node: StageNode,
    review_result: ReviewResult,
    rounds_completed: int,
    log_buffer: LogBuffer,
) -> str:
    """Handle reaching max review/fix rounds based on node config."""
    if node.on_max_rounds == "proceed_with_warnings":
        _log(
            log_buffer, pipeline_id,
            f"Max rounds ({rounds_completed}) reached — proceeding with warnings",
        )
        return STAGE_RESULT_APPROVED

    # Default: escalate
    _log(
        log_buffer, pipeline_id,
        f"Max rounds ({rounds_completed}) reached — escalating",
    )
    await _create_escalation(
        pool, pipeline_id, stage_id, "max_iterations",
        {
            "rounds_completed": rounds_completed,
            "last_severity": review_result.max_severity,
            "issue_count": len(review_result.issues),
        },
    )
    return STAGE_RESULT_ESCALATED


# ---------------------------------------------------------------------------
# Diff capture
# ---------------------------------------------------------------------------


async def _capture_full_diff(
    clone_path: str, review_base_rev: str, head_rev: str
) -> str:
    """Capture the full diff from review_base_rev to head_rev.

    Enforces the ReviewCoversHead invariant: code review always inspects
    the complete proposed diff from the pipeline's immutable base revision
    to its current head revision.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "diff", f"{review_base_rev}...{head_rev}",
        cwd=clone_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.warning(
            "git diff failed (rc=%d): %s", proc.returncode, stderr.decode().strip()
        )
        return ""

    return stdout.decode()


# ---------------------------------------------------------------------------
# Artifact path
# ---------------------------------------------------------------------------


def _diff_artifact_path(pipelines_dir: Path, pipeline_id: int) -> Path:
    return (
        pipelines_dir / str(pipeline_id) / "artifacts"
        / _REVIEW_ARTIFACT_DIR / _DIFF_ARTIFACT_NAME
    )


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


def _log(log_buffer: LogBuffer, pipeline_id: int, message: str) -> None:
    log_buffer.append(pipeline_id, f"[code_review] {message}")


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

from build_your_room.stages.base import register_stage_runner  # noqa: E402

register_stage_runner("code_review", run_code_review_stage)
