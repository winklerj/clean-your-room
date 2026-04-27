"""Tests for ImplTaskStage — HTN task claims, context rotation, postconditions.

Covers: happy-path single task completion, multi-task sequencing, context
rotation with claim preservation, postcondition failure and retry,
postcondition max retry escalation, cancellation at claim and execution
boundaries, no-tasks-ready → stage_complete, failed tasks → escalation,
max iterations, missing adapter, prompt construction, and property tests.

All agent interactions are mocked — no live API calls.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from build_your_room.command_registry import ConditionResult
from build_your_room.htn_planner import HTNPlanner
from build_your_room.models import HtnTask
from build_your_room.stage_graph import StageNode
from build_your_room.stages.impl_task import (
    STAGE_RESULT_ESCALATED,
    STAGE_RESULT_STAGE_COMPLETE,
    _build_diary_entry,
    _build_resume_prompt,
    _build_task_prompt,
    _checkpoint_enabled,
    run_impl_task_stage,
)
from build_your_room.streaming import LogBuffer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeTurnResult:
    """Minimal SessionResult for tests."""

    output: str = "Implemented the feature."
    structured_output: dict[str, Any] | None = None


def _make_node(**overrides: Any) -> StageNode:
    defaults: dict[str, Any] = {
        "key": "impl_task",
        "name": "Implementation",
        "stage_type": "impl_task",
        "agent": "claude",
        "prompt": "impl_task_default",
        "model": "claude-sonnet-4-6",
        "max_iterations": 50,
        "context_threshold_pct": 60,
        "on_context_limit": "resume_current_claim",
    }
    defaults.update(overrides)
    return StageNode(**defaults)


def _make_htn_task(
    id: int = 1,
    pipeline_id: int = 1,
    name: str = "Implement login",
    description: str = "Add the login endpoint",
    **overrides: Any,
) -> HtnTask:
    defaults = {
        "id": id,
        "pipeline_id": pipeline_id,
        "parent_task_id": None,
        "name": name,
        "description": description,
        "task_type": "primitive",
        "status": "in_progress",
        "priority": 0,
        "ordering": 0,
        "assigned_session_id": None,
        "claim_token": "tok-1",
        "claim_owner_token": "owner-1",
        "claim_expires_at": None,
        "preconditions_json": "[]",
        "postconditions_json": "[]",
        "invariants_json": None,
        "output_artifacts_json": None,
        "checkpoint_rev": None,
        "estimated_complexity": None,
        "diary_entry": None,
        "created_at": "2026-01-01",
        "started_at": "2026-01-01",
        "completed_at": None,
    }
    defaults.update(overrides)
    return HtnTask(**defaults)


def _make_mock_session(
    output: str = "Done with implementation.",
    session_id: str | None = "sess-impl-1",
    context_usage: dict[str, Any] | None = None,
) -> AsyncMock:
    session = AsyncMock()
    session.session_id = session_id
    session.send_turn.return_value = FakeTurnResult(output=output)
    session.get_context_usage.return_value = context_usage or {
        "total_tokens": 1000,
        "max_tokens": 100000,
    }
    session.snapshot.return_value = {"state": "snapshot"}
    return session


def _make_mock_adapter(session: AsyncMock | None = None) -> AsyncMock:
    adapter = AsyncMock()
    adapter.start_session.return_value = session or _make_mock_session()
    return adapter


def _make_mock_planner(
    *,
    tasks: list[HtnTask] | None = None,
    postcondition_results: list[ConditionResult] | None = None,
    progress_summary: dict[str, int] | None = None,
) -> AsyncMock:
    """Build a mock HTNPlanner with configurable claim/verify behavior."""
    planner = AsyncMock(spec=HTNPlanner)

    if tasks is None:
        # Default: one task claimed, then None
        planner.claim_next_ready_task.side_effect = [_make_htn_task(), None]
    else:
        planner.claim_next_ready_task.side_effect = tasks + [None]

    planner.verify_postconditions.return_value = postcondition_results or []
    planner.complete_task.return_value = []  # no newly ready tasks
    planner.get_progress_summary.return_value = progress_summary or {
        "completed": 1,
    }
    planner.sync_to_markdown.return_value = None
    planner.release_claim.return_value = None
    planner.reassign_claim.return_value = None
    planner.fail_task.return_value = None
    return planner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def log_buffer() -> LogBuffer:
    return LogBuffer()


@pytest.fixture
def cancel_event() -> asyncio.Event:
    return asyncio.Event()


@pytest.fixture
def tmp_pipelines_dir(tmp_path: Path) -> Path:
    return tmp_path / "pipelines"


@pytest.fixture
async def pool_with_stage(initialized_db):
    """Provide an async pool with a seeded pipeline + pipeline_stage row.

    Yields (pool, pipeline_id, stage_id).
    """
    from build_your_room.db import get_pool

    pool = get_pool()
    async with pool.connection() as conn:
        repo_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO repos (name, local_path) "
                "VALUES ('test-repo-impl', '/tmp/test-repo-impl') RETURNING id"
            )
        ).fetchone()
        repo_id = repo_row["id"]

        graph_json = json.dumps(
            {
                "entry_stage": "impl_task",
                "nodes": [
                    {
                        "key": "impl_task",
                        "name": "Implementation",
                        "type": "impl_task",
                        "agent": "claude",
                        "prompt": "impl_task_default",
                        "model": "claude-sonnet-4-6",
                        "max_iterations": 50,
                    }
                ],
                "edges": [],
            }
        )
        pdef_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO pipeline_defs (name, stage_graph_json) "
                "VALUES ('test-impl-def', %s) RETURNING id",
                (graph_json,),
            )
        ).fetchone()
        pdef_id = pdef_row["id"]

        p_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO pipelines "
                "(pipeline_def_id, repo_id, clone_path, review_base_rev, status) "
                "VALUES (%s, %s, '/tmp/test-clone-impl', 'abc123', 'running') RETURNING id",
                (pdef_id, repo_id),
            )
        ).fetchone()
        pipeline_id = p_row["id"]

        stage_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO pipeline_stages "
                "(pipeline_id, stage_key, attempt, stage_type, agent_type, "
                " status, max_iterations, started_at) "
                "VALUES (%s, 'impl_task', 1, 'impl_task', 'claude', "
                "'running', 50, now()) RETURNING id",
                (pipeline_id,),
            )
        ).fetchone()
        stage_id = stage_row["id"]

        await conn.commit()

    yield pool, pipeline_id, stage_id


# ---------------------------------------------------------------------------
# Integration tests — happy path: single task
# ---------------------------------------------------------------------------


class TestImplTaskSingleTask:
    """Single HTN task claimed, postconditions pass, stage completes."""

    @pytest.mark.asyncio
    async def test_single_task_completes_stage(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """When one task is claimed, executed, and postconditions pass,
        the stage returns stage_complete."""
        pool, pipeline_id, stage_id = pool_with_stage
        adapter = _make_mock_adapter()
        planner = _make_mock_planner()

        result = await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
        )

        assert result == STAGE_RESULT_STAGE_COMPLETE
        planner.claim_next_ready_task.assert_called()
        planner.complete_task.assert_called_once()
        planner.sync_to_markdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_adapter_session_started_and_closed(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """The adapter session is started and closed properly."""
        pool, pipeline_id, stage_id = pool_with_stage
        mock_session = _make_mock_session()
        adapter = _make_mock_adapter(mock_session)
        planner = _make_mock_planner()

        await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
        )

        adapter.start_session.assert_called_once()
        mock_session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_logs_task_lifecycle(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Log buffer records claim, completion, and stage-complete events."""
        pool, pipeline_id, stage_id = pool_with_stage
        adapter = _make_mock_adapter()
        planner = _make_mock_planner()

        await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
        )

        history = log_buffer.get_history(pipeline_id)
        assert any("Claimed task" in msg for msg in history)
        assert any("completed" in msg.lower() for msg in history)
        assert any("All HTN tasks complete" in msg for msg in history)

    @pytest.mark.asyncio
    async def test_creates_agent_session_row(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Agent session rows are created in the DB."""
        pool, pipeline_id, stage_id = pool_with_stage
        adapter = _make_mock_adapter()
        planner = _make_mock_planner()

        await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
        )

        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT * FROM agent_sessions WHERE pipeline_stage_id = %s",
                    (stage_id,),
                )
            ).fetchall()
        # One for the task session + one for the "no tasks" check session
        assert len(rows) >= 1
        assert any(r["status"] == "completed" for r in rows)


# ---------------------------------------------------------------------------
# Integration tests — multiple tasks
# ---------------------------------------------------------------------------


class TestImplTaskMultipleTasks:
    """Multiple HTN tasks claimed sequentially."""

    @pytest.mark.asyncio
    async def test_processes_two_tasks_then_completes(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Two tasks claimed and completed before stage_complete."""
        pool, pipeline_id, stage_id = pool_with_stage
        task1 = _make_htn_task(id=1, name="Task A")
        task2 = _make_htn_task(id=2, name="Task B")
        planner = _make_mock_planner(
            tasks=[task1, task2],
            progress_summary={"completed": 2},
        )
        adapter = _make_mock_adapter()

        result = await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
        )

        assert result == STAGE_RESULT_STAGE_COMPLETE
        assert planner.complete_task.call_count == 2
        assert planner.sync_to_markdown.call_count == 2


# ---------------------------------------------------------------------------
# Integration tests — postcondition failure and retry
# ---------------------------------------------------------------------------


class TestPostconditionRetry:
    """Postcondition verification with failures and retries."""

    @pytest.mark.asyncio
    async def test_retries_on_postcondition_failure(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """When postconditions fail, the agent gets a retry prompt and
        succeeds on the second attempt."""
        pool, pipeline_id, stage_id = pool_with_stage
        adapter = _make_mock_adapter()

        fail_result = ConditionResult(
            condition_type="tests_pass",
            description="Auth tests must pass",
            passed=False,
            detail="2 tests failed",
        )
        pass_result = ConditionResult(
            condition_type="tests_pass",
            description="Auth tests must pass",
            passed=True,
            detail="All tests passed",
        )

        planner = _make_mock_planner()
        # First verify fails, second verify passes
        planner.verify_postconditions.side_effect = [[fail_result], [pass_result]]

        result = await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
        )

        assert result == STAGE_RESULT_STAGE_COMPLETE
        # send_turn called once for initial + once for retry
        session = adapter.start_session.return_value
        assert session.send_turn.call_count == 2

        history = log_buffer.get_history(pipeline_id)
        assert any("postcondition retry" in msg.lower() for msg in history)

    @pytest.mark.asyncio
    async def test_escalates_after_max_postcondition_retries(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """When postconditions fail after max retries, the task is failed and escalated."""
        pool, pipeline_id, stage_id = pool_with_stage
        adapter = _make_mock_adapter()

        fail_result = ConditionResult(
            condition_type="tests_pass",
            description="Tests must pass",
            passed=False,
            detail="Still failing",
        )

        planner = _make_mock_planner()
        # Always fail postconditions
        planner.verify_postconditions.return_value = [fail_result]

        result = await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
        )

        assert result == STAGE_RESULT_ESCALATED
        planner.fail_task.assert_called_once()

        # Escalation created
        async with pool.connection() as conn:
            esc_rows = await (
                await conn.execute(
                    "SELECT * FROM escalations WHERE pipeline_id = %s",
                    (pipeline_id,),
                )
            ).fetchall()
        assert len(esc_rows) >= 1
        assert any(r["reason"] == "test_failure" for r in esc_rows)


# ---------------------------------------------------------------------------
# Integration tests — context rotation
# ---------------------------------------------------------------------------


class TestContextRotation:
    """Context rotation preserves task claim and spawns replacement session."""

    @pytest.mark.asyncio
    async def test_rotates_session_when_context_exceeds_threshold(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """When context usage exceeds threshold, the session rotates while
        keeping the task claim in_progress."""
        pool, pipeline_id, stage_id = pool_with_stage

        # First session returns high context usage
        high_usage_session = _make_mock_session(
            context_usage={"total_tokens": 70000, "max_tokens": 100000},
        )
        # Second session (after rotation) returns low usage
        low_usage_session = _make_mock_session(
            session_id="sess-rotated",
            context_usage={"total_tokens": 5000, "max_tokens": 100000},
        )

        adapter = AsyncMock()
        adapter.start_session.side_effect = [high_usage_session, low_usage_session]

        planner = _make_mock_planner()

        result = await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
        )

        assert result == STAGE_RESULT_STAGE_COMPLETE
        # Two sessions started (original + rotated)
        assert adapter.start_session.call_count == 2
        # Claim was reassigned, not released
        planner.reassign_claim.assert_called_once()
        planner.release_claim.assert_not_called()
        # First session was closed
        high_usage_session.close.assert_called_once()
        # Snapshot was captured from first session
        high_usage_session.snapshot.assert_called_once()

        history = log_buffer.get_history(pipeline_id)
        assert any("rotation" in msg.lower() for msg in history)

    @pytest.mark.asyncio
    async def test_resume_state_persisted_on_rotation(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """When session rotates, resume_state_json is saved on the old session."""
        pool, pipeline_id, stage_id = pool_with_stage

        high_usage_session = _make_mock_session(
            context_usage={"total_tokens": 70000, "max_tokens": 100000},
        )
        low_usage_session = _make_mock_session(session_id="sess-2")

        adapter = AsyncMock()
        adapter.start_session.side_effect = [high_usage_session, low_usage_session]

        planner = _make_mock_planner()

        await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
        )

        # Check that the first session has resume_state_json set
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT status, resume_state_json FROM agent_sessions "
                    "WHERE pipeline_stage_id = %s ORDER BY id",
                    (stage_id,),
                )
            ).fetchall()

        # Find the context_limit session
        context_limit_sessions = [r for r in rows if r["status"] == "context_limit"]
        assert len(context_limit_sessions) >= 1
        resume = context_limit_sessions[0]["resume_state_json"]
        assert resume is not None
        parsed = json.loads(resume)
        assert "stage_type" in parsed


# ---------------------------------------------------------------------------
# Integration tests — no tasks / all done
# ---------------------------------------------------------------------------


class TestNoTasks:
    """When no tasks are ready, check completion status."""

    @pytest.mark.asyncio
    async def test_no_tasks_all_completed_returns_stage_complete(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """When no tasks are claimed and progress shows all completed."""
        pool, pipeline_id, stage_id = pool_with_stage
        adapter = _make_mock_adapter()
        planner = _make_mock_planner(
            tasks=[],
            progress_summary={"completed": 5},
        )

        result = await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
        )

        assert result == STAGE_RESULT_STAGE_COMPLETE

    @pytest.mark.asyncio
    async def test_no_tasks_with_blocked_escalates(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """When no tasks are ready but some are blocked, escalate."""
        pool, pipeline_id, stage_id = pool_with_stage
        adapter = _make_mock_adapter()
        planner = _make_mock_planner(
            tasks=[],
            progress_summary={"completed": 2, "blocked": 3},
        )

        result = await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
        )

        assert result == STAGE_RESULT_ESCALATED

    @pytest.mark.asyncio
    async def test_no_tasks_with_failed_escalates(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """When no tasks are ready but some have failed, escalate."""
        pool, pipeline_id, stage_id = pool_with_stage
        adapter = _make_mock_adapter()
        planner = _make_mock_planner(
            tasks=[],
            progress_summary={"completed": 2, "failed": 1},
        )

        result = await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
        )

        assert result == STAGE_RESULT_ESCALATED


# ---------------------------------------------------------------------------
# Integration tests — cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    """Cancellation at various boundaries."""

    @pytest.mark.asyncio
    async def test_cancel_before_first_claim(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Pre-set cancel event prevents any task claim."""
        pool, pipeline_id, stage_id = pool_with_stage
        adapter = _make_mock_adapter()
        planner = _make_mock_planner()

        cancel_event = asyncio.Event()
        cancel_event.set()

        result = await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
        )

        assert result == STAGE_RESULT_ESCALATED
        planner.claim_next_ready_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_during_task_releases_claim(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Cancellation during execution releases the task claim."""
        pool, pipeline_id, stage_id = pool_with_stage
        cancel_event = asyncio.Event()

        mock_session = _make_mock_session()

        async def cancel_on_turn(prompt: str, **kw: Any) -> FakeTurnResult:
            cancel_event.set()
            return FakeTurnResult()

        mock_session.send_turn.side_effect = cancel_on_turn
        adapter = _make_mock_adapter(mock_session)
        planner = _make_mock_planner()

        result = await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
        )

        assert result == STAGE_RESULT_ESCALATED
        planner.release_claim.assert_called_once()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Missing adapter, max iterations, session errors."""

    @pytest.mark.asyncio
    async def test_missing_adapter_escalates(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """No adapter for the requested agent type → immediate escalation."""
        pool, pipeline_id, stage_id = pool_with_stage

        result = await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        assert result == STAGE_RESULT_ESCALATED

    @pytest.mark.asyncio
    async def test_max_iterations_reached(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """When max_iterations is reached with remaining tasks, escalate."""
        pool, pipeline_id, stage_id = pool_with_stage
        adapter = _make_mock_adapter()

        # Always return a new task (infinite stream)
        planner = AsyncMock(spec=HTNPlanner)
        planner.claim_next_ready_task.return_value = _make_htn_task()
        planner.verify_postconditions.return_value = []
        planner.complete_task.return_value = []
        planner.sync_to_markdown.return_value = None
        planner.get_progress_summary.return_value = {"completed": 2, "ready": 1}
        planner.reassign_claim.return_value = None

        result = await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(max_iterations=2),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
        )

        assert result == STAGE_RESULT_ESCALATED

        async with pool.connection() as conn:
            esc_rows = await (
                await conn.execute(
                    "SELECT reason FROM escalations WHERE pipeline_id = %s",
                    (pipeline_id,),
                )
            ).fetchall()
        assert any(r["reason"] == "max_iterations" for r in esc_rows)

    @pytest.mark.asyncio
    async def test_max_iterations_all_done_returns_complete(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """When max_iterations reached but all tasks are done, return stage_complete."""
        pool, pipeline_id, stage_id = pool_with_stage
        adapter = _make_mock_adapter()

        planner = AsyncMock(spec=HTNPlanner)
        planner.claim_next_ready_task.return_value = _make_htn_task()
        planner.verify_postconditions.return_value = []
        planner.complete_task.return_value = []
        planner.sync_to_markdown.return_value = None
        planner.get_progress_summary.return_value = {"completed": 2}
        planner.reassign_claim.return_value = None

        result = await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(max_iterations=2),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
        )

        assert result == STAGE_RESULT_STAGE_COMPLETE

    @pytest.mark.asyncio
    async def test_session_exception_fails_task(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Unhandled exception during session marks task failed and re-raises."""
        pool, pipeline_id, stage_id = pool_with_stage

        mock_session = _make_mock_session()
        mock_session.send_turn.side_effect = RuntimeError("LLM crash")
        adapter = _make_mock_adapter(mock_session)
        planner = _make_mock_planner()

        with pytest.raises(RuntimeError, match="LLM crash"):
            await run_impl_task_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=_make_node(),
                adapters={"claude": adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
                htn_planner=planner,
            )

        planner.fail_task.assert_called_once()
        mock_session.close.assert_called_once()


# ---------------------------------------------------------------------------
# Unit tests — prompt construction
# ---------------------------------------------------------------------------


class TestPromptConstruction:
    """Test _build_task_prompt and _build_resume_prompt."""

    def test_build_task_prompt_includes_task_name(self) -> None:
        """Task prompt must contain the task name and description."""
        task = _make_htn_task(name="Add auth", description="Implement OAuth2 flow")
        prompt = _build_task_prompt("Base prompt here", task)
        assert "Add auth" in prompt
        assert "Implement OAuth2 flow" in prompt
        assert "Base prompt here" in prompt

    def test_build_task_prompt_includes_postconditions(self) -> None:
        """When task has postconditions, they appear in the prompt."""
        postconds = json.dumps([
            {"type": "tests_pass", "description": "Auth tests must pass"},
            {"type": "file_exists", "description": "Login module exists", "path": "src/login.py"},
        ])
        task = _make_htn_task(postconditions_json=postconds)
        prompt = _build_task_prompt("Do the thing", task)
        assert "Auth tests must pass" in prompt
        assert "Login module exists" in prompt
        assert "Postconditions" in prompt

    def test_build_task_prompt_no_postconditions(self) -> None:
        """Empty postconditions should not add a postconditions section."""
        task = _make_htn_task(postconditions_json="[]")
        prompt = _build_task_prompt("Do the thing", task)
        assert "Postconditions" not in prompt

    def test_build_resume_prompt_includes_continuation_context(self) -> None:
        """Resume prompt should include continuation instructions."""
        task = _make_htn_task(name="Add tests", description="Write unit tests")
        prompt = _build_resume_prompt("Base prompt", task)
        assert "Add tests" in prompt
        assert "Continue working" in prompt
        assert "context rotated" in prompt.lower()


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestImplTaskProperties:
    """Property tests for ImplTaskStage invariants."""

    @given(
        name=st.text(min_size=1, max_size=50, alphabet=st.characters(blacklist_characters="\r\x00")),
        description=st.text(min_size=1, max_size=200, alphabet=st.characters(blacklist_characters="\r\x00")),
    )
    @settings(max_examples=20)
    def test_task_prompt_always_contains_name_and_description(
        self, name: str, description: str
    ) -> None:
        """Property: the generated task prompt always contains the task name and description."""
        task = _make_htn_task(name=name, description=description)
        prompt = _build_task_prompt("base", task)
        assert name in prompt
        assert description in prompt

    @given(
        name=st.text(min_size=1, max_size=50, alphabet=st.characters(blacklist_characters="\r\x00")),
        description=st.text(min_size=1, max_size=200, alphabet=st.characters(blacklist_characters="\r\x00")),
    )
    @settings(max_examples=20)
    def test_resume_prompt_always_contains_continuation_marker(
        self, name: str, description: str
    ) -> None:
        """Property: the resume prompt always includes a continuation marker."""
        task = _make_htn_task(name=name, description=description)
        prompt = _build_resume_prompt("base", task)
        assert "Continue working" in prompt
        assert name in prompt

    @given(
        n_postconditions=st.integers(min_value=0, max_value=5),
    )
    @settings(max_examples=15)
    def test_postcondition_count_matches_prompt_bullets(
        self, n_postconditions: int
    ) -> None:
        """Property: number of postcondition bullet items matches the JSON array length."""
        conds = [
            {"type": "tests_pass", "description": f"Condition {i}"}
            for i in range(n_postconditions)
        ]
        task = _make_htn_task(postconditions_json=json.dumps(conds))
        prompt = _build_task_prompt("base", task)

        if n_postconditions == 0:
            assert "Postconditions" not in prompt
        else:
            for i in range(n_postconditions):
                assert f"Condition {i}" in prompt

    @given(
        total=st.integers(min_value=0, max_value=200000),
        max_tok=st.integers(min_value=1, max_value=200000),
        threshold=st.floats(min_value=1.0, max_value=100.0, allow_nan=False),
    )
    @settings(max_examples=30)
    def test_context_monitor_rotation_decision_is_deterministic(
        self, total: int, max_tok: int, threshold: float
    ) -> None:
        """Property: given the same inputs, ContextMonitor always makes the same decision."""
        from build_your_room.context_monitor import ContextAction, ContextMonitor, ContextUsage, StageContext  # noqa: F811

        usage = ContextUsage(
            total_tokens=total,
            max_tokens=max_tok,
            usage_pct=(total / max_tok) * 100,
        )
        ctx = StageContext(
            stage_type="impl_task",
            pipeline_id=1,
            stage_id=1,
            session_id=1,
            active_task_id=1,
            active_claim_token="tok",
        )

        monitor1 = ContextMonitor(threshold_pct=threshold)
        monitor2 = ContextMonitor(threshold_pct=threshold)

        r1 = monitor1.check(usage, ctx)
        r2 = monitor2.check(usage, ctx)

        assert r1.action == r2.action
        if r1.action == ContextAction.ROTATE:
            assert r1.rotation_plan is not None
            assert r1.rotation_plan.has_active_claim is True


# ---------------------------------------------------------------------------
# Checkpoint commits + head_rev advancement
# ---------------------------------------------------------------------------


def _make_mock_clone_manager(
    *, return_rev: str | None = "rev-checkpoint-1"
) -> AsyncMock:
    """Build a mock CloneManager whose create_checkpoint_commit is observable."""
    from build_your_room.clone_manager import CloneManager

    cmgr = AsyncMock(spec=CloneManager)
    cmgr.create_checkpoint_commit.return_value = return_rev
    return cmgr


async def _seed_clone_dir_for_pipeline(
    pool: Any, pipeline_id: int, base: Path
) -> Path:
    """Materialize a real clone_path on disk so impl_task's existence check passes.

    The fixture pool_with_stage seeds clone_path='/tmp/test-clone-impl' which
    may not exist; checkpoint wiring short-circuits on missing dirs (a safety
    valve), so tests that exercise checkpointing must point clone_path at a
    real directory.
    """
    clone_dir = base / f"clone-{pipeline_id}"
    clone_dir.mkdir(parents=True, exist_ok=True)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE pipelines SET clone_path = %s WHERE id = %s",
            (str(clone_dir), pipeline_id),
        )
        await conn.commit()
    return clone_dir


class TestCheckpointEnabledHelper:
    """Tests for the _checkpoint_enabled config parser.

    Why: PipelineConfig defaults checkpoint_commits=True; the impl_task
    runner reads it via pipelines.config_json which can be missing,
    malformed, or partial. Default-True is the production-safe choice.
    """

    def test_none_defaults_to_true(self) -> None:
        assert _checkpoint_enabled(None) is True

    def test_unparseable_string_defaults_to_true(self) -> None:
        assert _checkpoint_enabled("not json") is True

    def test_explicit_true(self) -> None:
        assert _checkpoint_enabled('{"checkpoint_commits": true}') is True

    def test_explicit_false(self) -> None:
        assert _checkpoint_enabled('{"checkpoint_commits": false}') is False

    def test_dict_input_false(self) -> None:
        assert _checkpoint_enabled({"checkpoint_commits": False}) is False

    def test_missing_key_defaults_to_true(self) -> None:
        assert _checkpoint_enabled('{"other": 1}') is True


class TestDiaryEntry:
    """Tests for _build_diary_entry — invariant: includes name + revision."""

    def test_includes_task_name(self) -> None:
        task = _make_htn_task(name="Implement login")
        diary = _build_diary_entry(
            task_name="Implement login",
            task=task,
            retries=0,
            results=[],
            checkpoint_rev="abc123",
        )
        assert "Implement login" in diary
        assert "abc123" in diary

    def test_marks_no_changes_when_no_rev(self) -> None:
        task = _make_htn_task()
        diary = _build_diary_entry(
            task_name="x", task=task, retries=0, results=[], checkpoint_rev=None,
        )
        assert "no workspace changes" in diary

    def test_includes_postcondition_pass_marker(self) -> None:
        task = _make_htn_task()
        result = ConditionResult(
            passed=True, condition_type="tests_pass",
            description="all tests green", detail="",
        )
        diary = _build_diary_entry(
            task_name="x", task=task, retries=2, results=[result], checkpoint_rev="r",
        )
        assert "PASS" in diary
        assert "all tests green" in diary
        assert "retries: 2" in diary


class TestCheckpointCommitWiring:
    """Integration tests: impl_task wires checkpoint commits into completion.

    Spec lines 746/911 require head_rev to advance and a local checkpoint
    revision to be recorded after postconditions pass. Without this wiring,
    code_review's review_base_rev → head_rev diff is always empty and the
    ReviewCoversHead invariant is silently broken.
    """

    @pytest.mark.asyncio
    async def test_checkpoint_called_on_success(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
        tmp_path: Path,
    ) -> None:
        """create_checkpoint_commit is invoked after postconditions pass."""
        pool, pipeline_id, stage_id = pool_with_stage
        await _seed_clone_dir_for_pipeline(pool, pipeline_id, tmp_path)
        adapter = _make_mock_adapter()
        planner = _make_mock_planner()
        cmgr = _make_mock_clone_manager(return_rev="newrev-1")

        await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
            clone_manager=cmgr,
        )

        cmgr.create_checkpoint_commit.assert_called_once()
        # Pipeline head_rev advanced
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT head_rev FROM pipelines WHERE id = %s",
                    (pipeline_id,),
                )
            ).fetchone()
        assert row["head_rev"] == "newrev-1"

    @pytest.mark.asyncio
    async def test_checkpoint_passed_to_complete_task(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
        tmp_path: Path,
    ) -> None:
        """The new revision is recorded on htn_tasks.checkpoint_rev via complete_task."""
        pool, pipeline_id, stage_id = pool_with_stage
        await _seed_clone_dir_for_pipeline(pool, pipeline_id, tmp_path)
        adapter = _make_mock_adapter()
        planner = _make_mock_planner()
        cmgr = _make_mock_clone_manager(return_rev="newrev-2")

        await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
            clone_manager=cmgr,
        )

        planner.complete_task.assert_called_once()
        call_args = planner.complete_task.call_args
        # complete_task(task_id, checkpoint_rev, diary)
        assert call_args.args[1] == "newrev-2"
        assert call_args.args[2]  # non-empty diary entry

    @pytest.mark.asyncio
    async def test_checkpoint_skipped_when_config_disabled(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """When config_json.checkpoint_commits=false, no checkpoint is made.

        Why: operators must be able to opt out (e.g. for read-only repos
        or pipelines that drive an external system).
        """
        pool, pipeline_id, stage_id = pool_with_stage
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET config_json = %s WHERE id = %s",
                (json.dumps({"checkpoint_commits": False}), pipeline_id),
            )
            await conn.commit()

        adapter = _make_mock_adapter()
        planner = _make_mock_planner()
        cmgr = _make_mock_clone_manager()

        await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
            clone_manager=cmgr,
        )

        cmgr.create_checkpoint_commit.assert_not_called()
        # complete_task still called — just with checkpoint_rev=None
        planner.complete_task.assert_called_once()
        assert planner.complete_task.call_args.args[1] is None

    @pytest.mark.asyncio
    async def test_no_op_when_workspace_clean(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Clean workspace (no agent edits) → checkpoint returns None → head_rev unchanged."""
        pool, pipeline_id, stage_id = pool_with_stage
        await _seed_clone_dir_for_pipeline(pool, pipeline_id, tmp_path)
        adapter = _make_mock_adapter()
        planner = _make_mock_planner()
        cmgr = _make_mock_clone_manager(return_rev=None)  # clean workspace

        await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
            clone_manager=cmgr,
        )

        cmgr.create_checkpoint_commit.assert_called_once()
        # head_rev still NULL (or unchanged baseline)
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT head_rev FROM pipelines WHERE id = %s",
                    (pipeline_id,),
                )
            ).fetchone()
        assert row["head_rev"] is None
        assert planner.complete_task.call_args.args[1] is None

    @pytest.mark.asyncio
    async def test_git_error_does_not_abort_completion(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
        tmp_path: Path,
    ) -> None:
        """If checkpoint commit fails (e.g. non-git clone), task still completes.

        Why: postcondition success is the dominant signal. A checkpoint failure
        should be logged but not block readiness propagation, otherwise a
        misconfigured clone bricks the whole pipeline.
        """
        from build_your_room.clone_manager import GitError

        pool, pipeline_id, stage_id = pool_with_stage
        await _seed_clone_dir_for_pipeline(pool, pipeline_id, tmp_path)
        adapter = _make_mock_adapter()
        planner = _make_mock_planner()
        cmgr = _make_mock_clone_manager()
        cmgr.create_checkpoint_commit.side_effect = GitError(
            ["git", "commit"], 128, "fatal: not a git repo"
        )

        result = await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
            clone_manager=cmgr,
        )

        assert result == STAGE_RESULT_STAGE_COMPLETE
        planner.complete_task.assert_called_once()
        assert planner.complete_task.call_args.args[1] is None
        history = log_buffer.get_history(pipeline_id)
        assert any("Checkpoint commit failed" in m for m in history)

    @pytest.mark.asyncio
    async def test_two_tasks_advance_head_rev_in_sequence(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Two completed tasks produce two checkpoint commits; final head_rev = last rev.

        Property: head_rev monotonically advances as tasks complete in order.
        """
        pool, pipeline_id, stage_id = pool_with_stage
        await _seed_clone_dir_for_pipeline(pool, pipeline_id, tmp_path)
        task1 = _make_htn_task(id=1, name="Task A")
        task2 = _make_htn_task(id=2, name="Task B")
        planner = _make_mock_planner(
            tasks=[task1, task2], progress_summary={"completed": 2},
        )
        adapter = _make_mock_adapter()
        cmgr = _make_mock_clone_manager()
        cmgr.create_checkpoint_commit.side_effect = ["rev-A", "rev-B"]

        await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
            clone_manager=cmgr,
        )

        assert cmgr.create_checkpoint_commit.call_count == 2
        # Final head_rev is rev-B (the last commit)
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT head_rev FROM pipelines WHERE id = %s",
                    (pipeline_id,),
                )
            ).fetchone()
        assert row["head_rev"] == "rev-B"

    @pytest.mark.asyncio
    async def test_skipped_when_clone_path_missing_on_disk(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Missing on-disk clone short-circuits checkpoint without calling git.

        Why: tests and recovery scenarios run with synthetic clone_path values
        that do not point to real directories. Calling git there would raise
        a confusing error instead of just degrading to no-op.
        """
        pool, pipeline_id, stage_id = pool_with_stage
        adapter = _make_mock_adapter()
        planner = _make_mock_planner()
        cmgr = _make_mock_clone_manager()

        await run_impl_task_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            htn_planner=planner,
            clone_manager=cmgr,
        )

        # The seeded clone_path '/tmp/test-clone-impl' does not exist on disk
        cmgr.create_checkpoint_commit.assert_not_called()
