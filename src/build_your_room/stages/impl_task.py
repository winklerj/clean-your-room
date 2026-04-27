"""ImplTaskStage — drives implementation via atomic HTN task claims.

The stage runner:
1. Loops claiming the next ready HTN primitive task
2. Creates an agent session and sends the task prompt
3. After each turn, checks context usage; rotates session if over threshold
   while keeping the same task claim (resume_current_claim)
4. Verifies postconditions after the agent completes; retries on failure
5. Marks the task completed and propagates readiness
6. Returns "stage_complete" when no more tasks remain, or "escalated" on failure
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from psycopg_pool import AsyncConnectionPool

from build_your_room.adapters.base import AgentAdapter, LiveSession, SessionConfig
from build_your_room.clone_manager import CloneManager, GitError
from build_your_room.command_registry import (
    CommandRegistry,
    get_default_command_registry,
)
from build_your_room.config import PIPELINES_DIR
from build_your_room.context_monitor import (
    ContextAction,
    ContextMonitor,
    StageContext,
)
from build_your_room.harness_mcp import session_mcp_servers_for
from build_your_room.htn_planner import HTNPlanner
from build_your_room.sandbox import WorkspaceSandbox
from build_your_room.stage_graph import StageNode
from build_your_room.streaming import LogBuffer
from build_your_room.tool_profiles import get_tool_profile

logger = logging.getLogger(__name__)

STAGE_RESULT_STAGE_COMPLETE = "stage_complete"
STAGE_RESULT_ESCALATED = "escalated"

# Default lease duration for task claims (10 minutes)
_CLAIM_LEASE_SEC = 600

# Maximum postcondition retry rounds per task before escalating
_MAX_POSTCONDITION_RETRIES = 3


async def run_impl_task_stage(
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
    command_registry: CommandRegistry | None = None,
    clone_manager: CloneManager | None = None,
) -> str:
    """Run the impl-task stage: claim, execute, verify, and complete HTN tasks.

    Returns ``"stage_complete"`` when all ready tasks are done, or
    ``"escalated"`` when a task fails or max iterations are exceeded.
    """
    base_dir = pipelines_dir or PIPELINES_DIR

    pipeline = await _load_pipeline(pool, pipeline_id)
    clone_path = pipeline["clone_path"]
    cmgr = clone_manager or CloneManager(pool)
    checkpoint_enabled = _checkpoint_enabled(pipeline.get("config_json"))

    sandbox = WorkspaceSandbox.for_pipeline(clone_path, base_dir, pipeline_id)

    prompt_body = await _resolve_prompt(pool, node.prompt)
    tool_profile = get_tool_profile(node.stage_type)
    cmd_reg = command_registry or get_default_command_registry()

    adapter = adapters.get(node.agent)
    if adapter is None:
        _log(log_buffer, pipeline_id, f"No adapter for agent type {node.agent!r}, escalating")
        return STAGE_RESULT_ESCALATED

    planner = htn_planner or HTNPlanner(pool)
    owner_token = str(uuid.uuid4())
    iteration = 0

    while iteration < node.max_iterations:
        if cancel_event.is_set():
            _log(log_buffer, pipeline_id, "Cancelled before claiming next task")
            return STAGE_RESULT_ESCALATED

        # -- Claim next ready task ----------------------------------------
        claim_expires = _utc_now() + timedelta(seconds=_CLAIM_LEASE_SEC)
        session_db_id = await _create_session_row(pool, stage_id, node.agent, prompt_body)

        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_db_id, owner_token, claim_expires.isoformat()
        )
        if claimed is None:
            # No more ready tasks — check if all are done
            await _complete_session(pool, session_db_id, "completed")
            summary = await planner.get_progress_summary(pipeline_id)
            in_progress = summary.get("in_progress", 0)
            not_ready = summary.get("not_ready", 0)
            blocked = summary.get("blocked", 0)
            failed = summary.get("failed", 0)

            if failed > 0:
                _log(log_buffer, pipeline_id, f"No ready tasks; {failed} failed — escalating")
                await _create_escalation(
                    pool, pipeline_id, stage_id, "test_failure",
                    {"message": f"{failed} task(s) failed", "summary": summary},
                )
                return STAGE_RESULT_ESCALATED

            if in_progress > 0 or not_ready > 0 or blocked > 0:
                _log(
                    log_buffer, pipeline_id,
                    f"No ready tasks; {in_progress} in_progress, {not_ready} not_ready, "
                    f"{blocked} blocked — escalating",
                )
                await _create_escalation(
                    pool, pipeline_id, stage_id, "context_exhausted",
                    {"message": "Tasks remain but none are ready", "summary": summary},
                )
                return STAGE_RESULT_ESCALATED

            _log(log_buffer, pipeline_id, "All HTN tasks complete")
            return STAGE_RESULT_STAGE_COMPLETE

        task_id = claimed.id
        task_name = claimed.name
        _log(log_buffer, pipeline_id, f"Claimed task '{task_name}' (id={task_id})")

        # -- Build task prompt --------------------------------------------
        task_prompt = _build_task_prompt(prompt_body, claimed)

        session_config = SessionConfig(
            model=node.model,
            clone_path=clone_path,
            system_prompt=task_prompt,
            allowed_tools=list(tool_profile.all_tools),
            allowed_roots=sandbox.writable_roots_list,
            context_threshold_pct=float(node.context_threshold_pct),
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            mcp_servers=session_mcp_servers_for(
                node.agent,
                clone_path=clone_path,
                allowed_roots=sandbox.writable_roots_list,
                command_registry=cmd_reg,
            ),
        )

        context_monitor = ContextMonitor(
            threshold_pct=float(node.context_threshold_pct),
        )

        # -- Execute task with context rotation support -------------------
        task_completed = await _execute_task_with_rotation(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            task_id=task_id,
            task_name=task_name,
            claimed=claimed,
            adapter=adapter,
            session_config=session_config,
            session_db_id=session_db_id,
            context_monitor=context_monitor,
            planner=planner,
            clone_path=clone_path,
            sandbox=sandbox,
            node=node,
            owner_token=owner_token,
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            prompt_body=prompt_body,
            clone_manager=cmgr,
            checkpoint_enabled=checkpoint_enabled,
        )

        if not task_completed:
            # Task failed or was escalated
            return STAGE_RESULT_ESCALATED

        iteration += 1

    # Max iterations reached
    _log(log_buffer, pipeline_id, f"Max iterations ({node.max_iterations}) reached")
    summary = await planner.get_progress_summary(pipeline_id)
    ready = summary.get("ready", 0)
    not_ready = summary.get("not_ready", 0)
    if ready > 0 or not_ready > 0:
        await _create_escalation(
            pool, pipeline_id, stage_id, "max_iterations",
            {"message": f"Max iterations reached with {ready + not_ready} tasks remaining"},
        )
        return STAGE_RESULT_ESCALATED

    return STAGE_RESULT_STAGE_COMPLETE


# ---------------------------------------------------------------------------
# Task execution with context rotation
# ---------------------------------------------------------------------------


async def _execute_task_with_rotation(
    *,
    pool: AsyncConnectionPool,
    pipeline_id: int,
    stage_id: int,
    task_id: int,
    task_name: str,
    claimed: Any,
    adapter: AgentAdapter,
    session_config: SessionConfig,
    session_db_id: int,
    context_monitor: ContextMonitor,
    planner: HTNPlanner,
    clone_path: str,
    sandbox: WorkspaceSandbox,
    node: StageNode,
    owner_token: str,
    log_buffer: LogBuffer,
    cancel_event: asyncio.Event,
    prompt_body: str,
    clone_manager: CloneManager,
    checkpoint_enabled: bool,
) -> bool:
    """Execute a single HTN task, handling context rotation and postconditions.

    Returns True if the task was completed successfully, False if it failed/escalated.
    """
    current_session_db_id = session_db_id
    postcondition_retries = 0

    session = await adapter.start_session(session_config)
    try:
        if session.session_id:
            await _update_session_id(pool, current_session_db_id, session.session_id)

        # -- Initial turn -------------------------------------------------
        await session.send_turn(session_config.system_prompt)
        _log(log_buffer, pipeline_id, f"Task '{task_name}' initial turn complete")

        # -- Context check after turn ------------------------------------
        needs_rotation, session, current_session_db_id = await _check_context_and_rotate(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            task_id=task_id,
            task_name=task_name,
            session=session,
            current_session_db_id=current_session_db_id,
            context_monitor=context_monitor,
            adapter=adapter,
            session_config=session_config,
            node=node,
            planner=planner,
            owner_token=owner_token,
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            prompt_body=prompt_body,
            claimed=claimed,
        )

        if cancel_event.is_set():
            await _release_task_on_cancel(pool, planner, task_id, current_session_db_id)
            _log(log_buffer, pipeline_id, f"Cancelled during task '{task_name}'")
            return False

        # -- Postcondition verification loop ------------------------------
        while postcondition_retries <= _MAX_POSTCONDITION_RETRIES:
            results = await planner.verify_postconditions(
                task_id, clone_path, allowed_roots=[Path(r) for r in sandbox.writable_roots_list],
            )

            failures = [r for r in results if not r.passed]

            if not failures:
                # All postconditions passed — checkpoint workspace, advance
                # head_rev, and record the task as completed with the new
                # revision and a diary entry.
                checkpoint_rev = await _maybe_create_checkpoint(
                    pool=pool,
                    pipeline_id=pipeline_id,
                    clone_path=clone_path,
                    clone_manager=clone_manager,
                    enabled=checkpoint_enabled,
                    task_name=task_name,
                    log_buffer=log_buffer,
                )
                diary = _build_diary_entry(
                    task_name=task_name,
                    task=claimed,
                    retries=postcondition_retries,
                    results=results,
                    checkpoint_rev=checkpoint_rev,
                )
                _maybe_write_diary_file(
                    clone_path=clone_path,
                    task_id=task_id,
                    task_name=task_name,
                    content=diary,
                    log_buffer=log_buffer,
                    pipeline_id=pipeline_id,
                )
                newly_ready = await planner.complete_task(
                    task_id, checkpoint_rev, diary
                )
                await _complete_session(pool, current_session_db_id, "completed")
                _log(
                    log_buffer, pipeline_id,
                    f"Task '{task_name}' completed. {len(newly_ready)} tasks unblocked.",
                )
                await planner.sync_to_markdown(pipeline_id, clone_path)
                return True

            postcondition_retries += 1
            if postcondition_retries > _MAX_POSTCONDITION_RETRIES:
                break

            # Send follow-up prompt with failure details
            failure_details = "\n".join(
                f"- {f.description}: {f.detail}" for f in failures
            )
            followup = (
                f"Postcondition failed. Please fix and retry.\n\n{failure_details}"
            )
            _log(
                log_buffer, pipeline_id,
                f"Task '{task_name}' postcondition retry {postcondition_retries}/{_MAX_POSTCONDITION_RETRIES}",
            )

            await session.send_turn(followup)

            # Check context after retry turn
            _, session, current_session_db_id = await _check_context_and_rotate(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                task_id=task_id,
                task_name=task_name,
                session=session,
                current_session_db_id=current_session_db_id,
                context_monitor=context_monitor,
                adapter=adapter,
                session_config=session_config,
                node=node,
                planner=planner,
                owner_token=owner_token,
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                prompt_body=prompt_body,
                claimed=claimed,
            )

            if cancel_event.is_set():
                await _release_task_on_cancel(pool, planner, task_id, current_session_db_id)
                return False

        # Postconditions still failing after max retries
        failure_msg = "; ".join(f.description for f in failures)
        _log(
            log_buffer, pipeline_id,
            f"Task '{task_name}' postconditions failed after {_MAX_POSTCONDITION_RETRIES} retries: {failure_msg}",
        )
        await planner.fail_task(task_id, f"Postconditions failed: {failure_msg}")
        await _complete_session(pool, current_session_db_id, "failed")
        await _create_escalation(
            pool, pipeline_id, stage_id, "test_failure",
            {"task_id": task_id, "task_name": task_name, "failures": failure_msg},
        )
        return False

    except Exception:
        await _complete_session(pool, current_session_db_id, "failed")
        await planner.fail_task(task_id, "Unhandled exception during execution")
        raise
    finally:
        await session.close()


async def _check_context_and_rotate(
    *,
    pool: AsyncConnectionPool,
    pipeline_id: int,
    stage_id: int,
    task_id: int,
    task_name: str,
    session: LiveSession,
    current_session_db_id: int,
    context_monitor: ContextMonitor,
    adapter: AgentAdapter,
    session_config: SessionConfig,
    node: StageNode,
    planner: HTNPlanner,
    owner_token: str,
    log_buffer: LogBuffer,
    cancel_event: asyncio.Event,
    prompt_body: str,
    claimed: Any,
) -> tuple[bool, LiveSession, int]:
    """Check context usage and rotate session if needed.

    Returns (rotated, current_session, current_session_db_id).
    """
    raw_usage = await session.get_context_usage()
    if raw_usage is None:
        return False, session, current_session_db_id

    usage = ContextMonitor.parse_claude_usage(raw_usage)
    if usage is None:
        return False, session, current_session_db_id

    stage_context = StageContext(
        stage_type="impl_task",
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        session_id=current_session_db_id,
        active_task_id=task_id,
        active_claim_token=owner_token,
        prompt_context=prompt_body,
    )

    check_result = context_monitor.check(usage, stage_context)

    if check_result.action == ContextAction.CONTINUE:
        return False, session, current_session_db_id

    # Context limit reached — rotate session
    _log(
        log_buffer, pipeline_id,
        f"Context rotation for task '{task_name}': {check_result.warning_message}",
    )

    # Persist resume state on old session
    resume_state = await session.snapshot()
    if check_result.rotation_plan:
        resume_state.update(check_result.rotation_plan.resume_state)
    await _update_session_resume_state(pool, current_session_db_id, resume_state)
    await _complete_session(pool, current_session_db_id, "context_limit")

    # Close old session
    await session.close()

    # Create new session
    new_session_db_id = await _create_session_row(pool, stage_id, node.agent, prompt_body)

    # Reassign the HTN task claim to the new session
    await planner.reassign_claim(task_id, new_session_db_id)

    # Build a resume prompt
    resume_prompt = _build_resume_prompt(prompt_body, claimed)

    resume_config = SessionConfig(
        model=session_config.model,
        clone_path=session_config.clone_path,
        system_prompt=resume_prompt,
        allowed_tools=session_config.allowed_tools,
        allowed_roots=session_config.allowed_roots,
        context_threshold_pct=session_config.context_threshold_pct,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        mcp_servers=session_config.mcp_servers,
    )

    new_session = await adapter.start_session(resume_config)
    if new_session.session_id:
        await _update_session_id(pool, new_session_db_id, new_session.session_id)

    # Send the resume turn
    await new_session.send_turn(resume_prompt)

    _log(log_buffer, pipeline_id, f"Session rotated for task '{task_name}', claim preserved")
    return True, new_session, new_session_db_id


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def _build_task_prompt(base_prompt: str, task: Any) -> str:
    """Build the implementation prompt for a specific HTN task."""
    postconditions = task.postconditions_json
    postcond_text = ""
    if postconditions:
        try:
            conds = json.loads(postconditions) if isinstance(postconditions, str) else postconditions
            if conds:
                postcond_text = "\n\n## Postconditions (must pass for task completion)\n"
                for c in conds:
                    postcond_text += f"- {c.get('description', c.get('type', 'unknown'))}\n"
        except (json.JSONDecodeError, TypeError):
            pass

    return (
        f"{base_prompt}\n\n"
        f"## Current Task\n\n"
        f"**{task.name}**\n\n"
        f"{task.description}"
        f"{postcond_text}"
    )


def _build_resume_prompt(base_prompt: str, task: Any) -> str:
    """Build the resume prompt after context rotation."""
    return (
        f"{base_prompt}\n\n"
        f"## Resumed Task (context rotated)\n\n"
        f"**{task.name}**\n\n"
        f"{task.description}\n\n"
        f"Continue working on this task. The previous session ran out of "
        f"context space. Review the current state of the code and continue "
        f"from where the previous session left off."
    )


# ---------------------------------------------------------------------------
# Checkpoint + diary helpers
# ---------------------------------------------------------------------------


def _checkpoint_enabled(config_json: Any) -> bool:
    """Return True if the pipeline config opts into local checkpoint commits.

    Defaults to True (matching ``PipelineConfig.checkpoint_commits``) when
    the column is null or unparseable. The field is treated as advisory —
    workspace cleanliness is still required before a commit is made.
    """
    if config_json is None:
        return True
    raw: Any = config_json
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return True
    if not isinstance(raw, dict):
        return True
    value = raw.get("checkpoint_commits", True)
    return bool(value)


async def _maybe_create_checkpoint(
    *,
    pool: AsyncConnectionPool,
    pipeline_id: int,
    clone_path: str,
    clone_manager: CloneManager,
    enabled: bool,
    task_name: str,
    log_buffer: LogBuffer,
) -> str | None:
    """Create a local checkpoint commit and advance pipeline.head_rev.

    Returns the new HEAD revision when a commit was created, ``None`` when
    skipped (disabled, missing clone, clean workspace, or git failure). On
    git error the failure is logged but never propagated — the postcondition
    success path must remain the dominant signal for task completion.
    """
    if not enabled:
        return None
    if not clone_path:
        return None
    if not Path(clone_path).exists():
        _log(
            log_buffer,
            pipeline_id,
            f"Skipping checkpoint for '{task_name}': clone path missing",
        )
        return None
    try:
        new_rev = await clone_manager.create_checkpoint_commit(
            clone_path, f"checkpoint: {task_name}"
        )
    except GitError as exc:
        _log(
            log_buffer,
            pipeline_id,
            f"Checkpoint commit failed for '{task_name}': {exc}",
        )
        return None
    if new_rev is None:
        return None
    await _update_pipeline_head_rev(pool, pipeline_id, new_rev)
    _log(
        log_buffer,
        pipeline_id,
        f"Checkpoint commit {new_rev[:8]} for task '{task_name}'",
    )
    return new_rev


async def _update_pipeline_head_rev(
    pool: AsyncConnectionPool, pipeline_id: int, new_rev: str
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE pipelines SET head_rev = %s, updated_at = now() "
            "WHERE id = %s",
            (new_rev, pipeline_id),
        )
        await conn.commit()


def _build_diary_entry(
    *,
    task_name: str,
    task: Any,
    retries: int,
    results: list[Any],
    checkpoint_rev: str | None,
) -> str:
    """Build a structured diary entry for a completed task.

    The diary captures the task's name, complexity, retry count, postcondition
    pass/fail summary, and checkpoint revision (when one was created). Agent-
    authored learnings can be appended later via ``htn_tasks.diary_entry``.
    """
    complexity = getattr(task, "estimated_complexity", None) or "unspecified"
    rev_line = (
        f"Checkpoint revision: `{checkpoint_rev}`\n"
        if checkpoint_rev
        else "Checkpoint revision: (no workspace changes)\n"
    )
    cond_lines = []
    for r in results:
        status = "PASS" if getattr(r, "passed", False) else "FAIL"
        desc = getattr(r, "description", None) or getattr(r, "type", "condition")
        cond_lines.append(f"- [{status}] {desc}")
    cond_block = "\n".join(cond_lines) if cond_lines else "- (no postconditions)"
    return (
        f"# Task: {task_name}\n"
        f"## Complexity: {complexity} | Postcondition retries: {retries}\n\n"
        f"{rev_line}"
        f"\n### Postcondition verification\n{cond_block}\n"
    )


_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SLUG_MAX_LEN = 64


def _slugify(name: str) -> str:
    """Lowercase ASCII slug for filenames.

    Collapses runs of non-alphanumeric characters into ``-`` and trims
    leading/trailing ``-``. Truncated to 64 chars; falls back to ``"task"``
    when the input contains no slug-eligible characters.
    """
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    if not slug:
        return "task"
    return slug[:_SLUG_MAX_LEN].rstrip("-") or "task"


def _maybe_write_diary_file(
    *,
    clone_path: str,
    task_id: int,
    task_name: str,
    content: str,
    log_buffer: LogBuffer,
    pipeline_id: int,
) -> str | None:
    """Write the diary content to ``{clone_path}/diary/task-{id}-{slug}.md``.

    Spec line 811 requires diary entries to be stored in both
    ``htn_tasks.diary_entry`` and in ``diary/`` files. Best-effort: returns
    ``None`` when the clone path is missing or unwritable, logs OSError but
    never propagates so the postcondition success path stays dominant.
    Idempotent: overwrites existing files (replay/retry safe).
    """
    if not clone_path:
        return None
    clone_dir = Path(clone_path)
    if not clone_dir.exists():
        return None
    diary_dir = clone_dir / "diary"
    file_path = diary_dir / f"task-{task_id}-{_slugify(task_name)}.md"
    try:
        diary_dir.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        _log(
            log_buffer,
            pipeline_id,
            f"Diary file write failed for task {task_id}: {exc}",
        )
        return None
    return str(file_path)


# ---------------------------------------------------------------------------
# Cancellation helper
# ---------------------------------------------------------------------------


async def _release_task_on_cancel(
    pool: AsyncConnectionPool,
    planner: HTNPlanner,
    task_id: int,
    session_db_id: int,
) -> None:
    """Release task claim and mark session cancelled on cancellation."""
    await planner.release_claim(task_id)
    await _complete_session(pool, session_db_id, "cancelled")


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


async def _update_session_resume_state(
    pool: AsyncConnectionPool, session_db_id: int, resume_state: dict[str, Any]
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agent_sessions SET resume_state_json = %s WHERE id = %s",
            (json.dumps(resume_state), session_db_id),
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _log(log_buffer: LogBuffer, pipeline_id: int, message: str) -> None:
    log_buffer.append(pipeline_id, f"[impl_task] {message}")


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

from build_your_room.stages.base import register_stage_runner  # noqa: E402

register_stage_runner("impl_task", run_impl_task_stage)
