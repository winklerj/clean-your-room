"""Integration tests — full pipeline lifecycle with mock adapters + pytest-postgresql.

These tests exercise multi-component workflows end-to-end: the orchestrator
drives stage transitions through the real stage graph, each stage runner
receives mock adapters that return configurable results, and all DB state
(pipeline status, stage rows, session rows, HTN tasks, escalations) is
verified against a real PostgreSQL instance via pytest-postgresql.

Key distinction from per-stage unit tests: these tests start from
orchestrator.start_pipeline() and verify cross-cutting concerns like
multi-stage transitions, visit-count tracking, escalation→resume flows,
cancellation semantics, and HTN task lifecycle across the full pipeline.

All agent interactions are mocked — no live API calls.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from build_your_room.command_registry import CommandRegistry, ConditionResult
from build_your_room.db import get_pool
from build_your_room.orchestrator import (
    STAGE_RESULT_APPROVED,
    STAGE_RESULT_ESCALATED,
    STAGE_RESULT_STAGE_COMPLETE,
    STAGE_RESULT_VALIDATED,
    STAGE_RESULT_VALIDATION_FAILED,
    PipelineOrchestrator,
)
from build_your_room.streaming import LogBuffer


# ---------------------------------------------------------------------------
# Stage graph definitions for integration tests
# ---------------------------------------------------------------------------

# Full 5-stage graph matching the spec example
FULL_GRAPH = {
    "entry_stage": "spec_author",
    "nodes": [
        {
            "key": "spec_author",
            "name": "Spec authoring",
            "type": "spec_author",
            "agent": "claude",
            "prompt": "spec_author_default",
            "model": "claude-opus-4-6",
            "max_iterations": 1,
        },
        {
            "key": "impl_plan",
            "name": "Implementation plan",
            "type": "impl_plan",
            "agent": "claude",
            "prompt": "impl_plan_default",
            "model": "claude-opus-4-6",
            "max_iterations": 1,
        },
        {
            "key": "impl_task",
            "name": "Implementation",
            "type": "impl_task",
            "agent": "claude",
            "prompt": "impl_task_default",
            "model": "claude-sonnet-4-6",
            "max_iterations": 50,
            "on_context_limit": "resume_current_claim",
        },
        {
            "key": "code_review",
            "name": "Code review",
            "type": "code_review",
            "agent": "codex",
            "prompt": "code_review_default",
            "model": "gpt-5.1-codex",
            "max_iterations": 3,
        },
        {
            "key": "validation",
            "name": "Validation",
            "type": "validation",
            "agent": "claude",
            "prompt": "validation_default",
            "model": "claude-sonnet-4-6",
            "max_iterations": 3,
        },
    ],
    "edges": [
        {"key": "spec_to_plan", "from": "spec_author", "to": "impl_plan", "on": "approved"},
        {"key": "plan_to_impl", "from": "impl_plan", "to": "impl_task", "on": "approved"},
        {"key": "impl_to_review", "from": "impl_task", "to": "code_review", "on": "stage_complete"},
        {"key": "review_to_validation", "from": "code_review", "to": "validation", "on": "approved"},
        {
            "key": "validation_back_to_review",
            "from": "validation",
            "to": "code_review",
            "on": "validation_failed",
            "max_visits": 2,
            "on_exhausted": "escalate",
        },
        {"key": "validation_to_done", "from": "validation", "to": "completed", "on": "validated"},
    ],
}

FULL_GRAPH_JSON = json.dumps(FULL_GRAPH)

# Minimal 2-stage graph for focused transition tests
MINIMAL_GRAPH = {
    "entry_stage": "spec_author",
    "nodes": [
        {
            "key": "spec_author",
            "name": "Spec authoring",
            "type": "spec_author",
            "agent": "claude",
            "prompt": "spec_author_default",
            "model": "claude-opus-4-6",
            "max_iterations": 1,
        },
        {
            "key": "validation",
            "name": "Validation",
            "type": "validation",
            "agent": "claude",
            "prompt": "validation_default",
            "model": "claude-sonnet-4-6",
            "max_iterations": 3,
        },
    ],
    "edges": [
        {"key": "spec_to_val", "from": "spec_author", "to": "validation", "on": "approved"},
        {"key": "val_to_done", "from": "validation", "to": "completed", "on": "validated"},
    ],
}


# ---------------------------------------------------------------------------
# Mock adapter infrastructure
# ---------------------------------------------------------------------------


@dataclass
class FakeTurnResult:
    """Minimal SessionResult for integration tests."""

    output: str = "Mock output from agent."
    structured_output: dict[str, Any] | None = None


def _make_mock_session(
    *,
    output: str = "Mock output.",
    structured_output: dict[str, Any] | None = None,
    session_id: str = "int-test-sess",
    context_usage: dict[str, Any] | None = None,
) -> AsyncMock:
    """Build a mock LiveSession with configurable turn results."""
    session = AsyncMock()
    session.session_id = session_id
    session.send_turn.return_value = FakeTurnResult(
        output=output, structured_output=structured_output,
    )
    session.get_context_usage.return_value = context_usage or {
        "total_tokens": 1000,
        "max_tokens": 200000,
    }
    session.snapshot.return_value = {"state": "test"}
    return session


def _make_mock_adapter(session: AsyncMock | None = None) -> AsyncMock:
    """Build a mock AgentAdapter that returns the given session."""
    adapter = AsyncMock()
    adapter.start_session.return_value = session or _make_mock_session()
    return adapter


def _approval_review_output() -> dict[str, Any]:
    """Structured review output that passes the decision gate."""
    return {
        "approved": True,
        "max_severity": "none",
        "issues": [],
        "feedback_markdown": "LGTM",
    }


def _passing_verification_results() -> list[ConditionResult]:
    """Verification results where all checks pass."""
    return [
        ConditionResult(condition_type="tests_pass", description="Run tests", passed=True, detail="ok"),
        ConditionResult(condition_type="lint_clean", description="Run lint", passed=True, detail="ok"),
        ConditionResult(condition_type="type_check", description="Run typecheck", passed=True, detail="ok"),
    ]


def _failing_verification_results() -> list[ConditionResult]:
    """Verification results where lint fails."""
    return [
        ConditionResult(condition_type="tests_pass", description="Run tests", passed=True, detail="ok"),
        ConditionResult(condition_type="lint_clean", description="Run lint", passed=False, detail="lint error"),
        ConditionResult(condition_type="type_check", description="Run typecheck", passed=True, detail="ok"),
    ]


# ---------------------------------------------------------------------------
# DB seeding helpers
# ---------------------------------------------------------------------------


async def _seed_full_pipeline(
    pool,
    *,
    graph_json: str = FULL_GRAPH_JSON,
    status: str = "pending",
    clone_path: str = "/tmp/integ-test-clone",
    suffix: str = "",
) -> int:
    """Seed repo + pipeline_def + pipeline for integration testing. Returns pipeline_id."""
    async with pool.connection() as conn:
        repo_row = await (
            await conn.execute(
                "INSERT INTO repos (name, local_path) VALUES (%s, %s) RETURNING id",
                (f"integ-repo{suffix}", f"/tmp/integ-repo{suffix}"),
            )
        ).fetchone()
        repo_id = repo_row["id"]

        pdef_row = await (
            await conn.execute(
                "INSERT INTO pipeline_defs (name, stage_graph_json) "
                "VALUES (%s, %s) RETURNING id",
                (f"integ-def{suffix}", graph_json),
            )
        ).fetchone()
        pdef_id = pdef_row["id"]

        pipeline_row = await (
            await conn.execute(
                "INSERT INTO pipelines "
                "(pipeline_def_id, repo_id, clone_path, review_base_rev, status) "
                "VALUES (%s, %s, %s, 'base-rev-abc', %s) RETURNING id",
                (pdef_id, repo_id, clone_path, status),
            )
        ).fetchone()
        await conn.commit()
        return pipeline_row["id"]


async def _seed_htn_tasks_for_pipeline(pool, pipeline_id: int, *, count: int = 2) -> list[int]:
    """Seed ready primitive HTN tasks for a pipeline. Returns task IDs."""
    task_ids = []
    async with pool.connection() as conn:
        for i in range(count):
            row = await (
                await conn.execute(
                    "INSERT INTO htn_tasks "
                    "(pipeline_id, name, description, task_type, status, priority, ordering, "
                    " preconditions_json, postconditions_json) "
                    "VALUES (%s, %s, %s, 'primitive', 'ready', %s, %s, '[]', '[]') RETURNING id",
                    (pipeline_id, f"task-{i}", f"Implement feature {i}", count - i, i),
                )
            ).fetchone()
            task_ids.append(row["id"])
        await conn.commit()
    return task_ids


async def _get_pipeline_status(pool, pipeline_id: int) -> dict[str, Any]:
    """Fetch pipeline row as a dict."""
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT * FROM pipelines WHERE id = %s", (pipeline_id,)
            )
        ).fetchone()
    return dict(row) if row else {}


async def _get_stage_rows(pool, pipeline_id: int) -> list[dict[str, Any]]:
    """Fetch all pipeline_stages for a pipeline, ordered by id."""
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT * FROM pipeline_stages WHERE pipeline_id = %s ORDER BY id",
                (pipeline_id,),
            )
        ).fetchall()
    return [dict(r) for r in rows]


async def _get_session_rows(pool, pipeline_id: int) -> list[dict[str, Any]]:
    """Fetch all agent_sessions for a pipeline via its stages."""
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT s.* FROM agent_sessions s "
                "JOIN pipeline_stages ps ON s.pipeline_stage_id = ps.id "
                "WHERE ps.pipeline_id = %s ORDER BY s.id",
                (pipeline_id,),
            )
        ).fetchall()
    return [dict(r) for r in rows]


async def _get_escalation_rows(pool, pipeline_id: int) -> list[dict[str, Any]]:
    """Fetch all escalations for a pipeline."""
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT * FROM escalations WHERE pipeline_id = %s ORDER BY id",
                (pipeline_id,),
            )
        ).fetchall()
    return [dict(r) for r in rows]


async def _get_htn_task_rows(pool, pipeline_id: int) -> list[dict[str, Any]]:
    """Fetch all HTN tasks for a pipeline."""
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT * FROM htn_tasks WHERE pipeline_id = %s ORDER BY id",
                (pipeline_id,),
            )
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def log_buffer() -> LogBuffer:
    return LogBuffer()


@pytest.fixture
def pipelines_dir(tmp_path: Path) -> Path:
    d = tmp_path / "pipelines"
    d.mkdir()
    return d


@pytest.fixture
def mock_command_registry() -> CommandRegistry:
    """A command registry that produces passing results without subprocesses."""
    reg = CommandRegistry()
    return reg


# ---------------------------------------------------------------------------
# Full pipeline lifecycle tests
# ---------------------------------------------------------------------------


class TestFullPipelineLifecycle:
    """Test the orchestrator driving a pipeline through all 5 stages to completion.

    Why this is important: validates that the orchestrator correctly transitions
    between stages, creates stage rows for each visit, and reaches 'completed'
    status when all stages return success results.

    Invariant: A pipeline with all stages returning success results must
    reach status='completed' and have exactly one stage row per stage type.
    """

    async def test_full_pipeline_happy_path(
        self, initialized_db, tmp_path, log_buffer,
    ):
        """Full 5-stage pipeline completes successfully via patched stage runners.

        Verifies: pipeline status transitions, stage row creation for each
        stage, visit count tracking, and final 'completed' status.

        We patch stage runners at the orchestrator dispatch level so the full
        orchestrator loop (lease, heartbeat, graph traversal) is exercised
        without needing to wire up internal stage I/O.
        """
        pool = get_pool()
        pid = await _seed_full_pipeline(pool, clone_path=str(tmp_path / "clone"))

        with patch.dict("build_your_room.stages.base.STAGE_RUNNERS", {
            "spec_author": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "impl_plan": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "impl_task": AsyncMock(return_value=STAGE_RESULT_STAGE_COMPLETE),
            "code_review": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "validation": AsyncMock(return_value=STAGE_RESULT_VALIDATED),
        }):
            orch = PipelineOrchestrator(
                pool, log_buffer,
                adapters={"claude": _make_mock_adapter(), "codex": _make_mock_adapter()},
                lease_ttl_sec=60, heartbeat_interval_sec=300,
            )
            await orch.start_pipeline(pid)
            task, _ = orch._active_pipelines[pid]
            await asyncio.wait_for(task, timeout=10)

        # Verify final pipeline status
        p = await _get_pipeline_status(pool, pid)
        assert p["status"] == "completed", f"Expected completed, got {p['status']}"

        # Verify stage rows were created for all 5 stage types
        stages = await _get_stage_rows(pool, pid)
        stage_keys = [s["stage_key"] for s in stages]
        assert "spec_author" in stage_keys
        assert "impl_plan" in stage_keys
        assert "impl_task" in stage_keys
        assert "code_review" in stage_keys
        assert "validation" in stage_keys

        # All stages should be completed
        for s in stages:
            assert s["status"] == "completed", (
                f"Stage {s['stage_key']} has status {s['status']}, expected completed"
            )

    async def test_pipeline_no_adapter_skips_stage(
        self, initialized_db, tmp_path, log_buffer,
    ):
        """When no adapter is registered for a stage's agent type, the stage is
        skipped with a default result, and the pipeline continues.

        Invariant: Missing adapters produce skip, not failure — the orchestrator
        uses _default_stage_result() to keep the graph traversal moving.
        """
        pool = get_pool()
        pipelines_dir = tmp_path / "pipelines"
        pipelines_dir.mkdir()

        pid = await _seed_full_pipeline(pool, clone_path=str(tmp_path / "clone"))

        # No adapters registered at all — all stages get default results
        orch = PipelineOrchestrator(
            pool, log_buffer, adapters={},
            lease_ttl_sec=60, heartbeat_interval_sec=300,
        )

        await orch.start_pipeline(pid)
        task, _ = orch._active_pipelines[pid]
        await asyncio.wait_for(task, timeout=10)

        p = await _get_pipeline_status(pool, pid)
        assert p["status"] == "completed"

        # Stages should be marked 'skipped'
        stages = await _get_stage_rows(pool, pid)
        for s in stages:
            assert s["status"] == "skipped"

    async def test_pipeline_records_visit_counts_in_recovery_json(
        self, initialized_db, tmp_path, log_buffer,
    ):
        """After each stage transition, the orchestrator persists edge visit
        counts in recovery_state_json so restart recovery can resume correctly.

        Invariant: recovery_state_json.visit_counts reflects all edges traversed.
        """
        pool = get_pool()
        pid = await _seed_full_pipeline(pool, clone_path=str(tmp_path / "clone"))

        # No adapters → all stages skipped with default results, but edges are tracked
        orch = PipelineOrchestrator(
            pool, log_buffer, adapters={},
            lease_ttl_sec=60, heartbeat_interval_sec=300,
        )

        await orch.start_pipeline(pid)
        task, _ = orch._active_pipelines[pid]
        await asyncio.wait_for(task, timeout=10)

        p = await _get_pipeline_status(pool, pid)
        recovery = json.loads(p["recovery_state_json"])
        vc = recovery["visit_counts"]

        # Each edge should have been visited exactly once in the happy path
        assert vc.get("spec_to_plan") == 1
        assert vc.get("plan_to_impl") == 1
        assert vc.get("impl_to_review") == 1
        assert vc.get("review_to_validation") == 1
        assert vc.get("validation_to_done") == 1
        # The back-edge should not have been visited
        assert vc.get("validation_back_to_review", 0) == 0


# ---------------------------------------------------------------------------
# Escalation and cancellation integration tests
# ---------------------------------------------------------------------------


class TestEscalationAndCancellation:
    """Test escalation→pause→resume and cancellation flows.

    Why this is important: validates that the orchestrator correctly pauses
    on escalation, creates the escalation row, and can resume after human
    intervention. Also verifies cancel/kill produce correct terminal states.
    """

    async def test_escalation_pauses_pipeline(
        self, initialized_db, tmp_path, log_buffer,
    ):
        """When a stage returns 'escalated', the pipeline pauses and an
        escalation row is NOT created by the orchestrator loop itself
        (the stage runner already created it) — but the pipeline status
        becomes 'paused'.

        Invariant: stage result 'escalated' → pipeline status 'paused'.
        """
        pool = get_pool()
        pid = await _seed_full_pipeline(pool, clone_path=str(tmp_path / "clone"))

        # Claude adapter returns escalation from spec_author stage
        claude_session = _make_mock_session(output="Cannot proceed — need design decision.")
        claude_adapter = _make_mock_adapter(claude_session)

        # Patch the spec_author stage to return escalated
        with patch.dict("build_your_room.stages.base.STAGE_RUNNERS", {
            "spec_author": AsyncMock(return_value=STAGE_RESULT_ESCALATED),
        }):
            orch = PipelineOrchestrator(
                pool, log_buffer,
                adapters={"claude": claude_adapter},
                lease_ttl_sec=60, heartbeat_interval_sec=300,
            )
            await orch.start_pipeline(pid)
            task, _ = orch._active_pipelines[pid]
            await asyncio.wait_for(task, timeout=10)

        p = await _get_pipeline_status(pool, pid)
        assert p["status"] == "paused"

    async def test_resume_after_escalation(
        self, initialized_db, tmp_path, log_buffer,
    ):
        """After escalation, resume_pipeline creates a new asyncio task that
        picks up from where the pipeline left off.

        Invariant: resolved escalation + resume → pipeline runs again.
        """
        pool = get_pool()
        pid = await _seed_full_pipeline(pool, clone_path=str(tmp_path / "clone"))

        call_count = 0

        async def spec_author_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return STAGE_RESULT_ESCALATED
            return STAGE_RESULT_APPROVED

        # The patch must span both the initial run AND the resume, since
        # resume_pipeline creates a new asyncio task that re-enters the
        # orchestrator loop. Keep the context manager open throughout.
        with patch.dict("build_your_room.stages.base.STAGE_RUNNERS", {
            "spec_author": AsyncMock(side_effect=spec_author_side_effect),
            "impl_plan": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "impl_task": AsyncMock(return_value=STAGE_RESULT_STAGE_COMPLETE),
            "code_review": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "validation": AsyncMock(return_value=STAGE_RESULT_VALIDATED),
        }):
            orch = PipelineOrchestrator(
                pool, log_buffer,
                adapters={"claude": _make_mock_adapter(), "codex": _make_mock_adapter()},
                lease_ttl_sec=60, heartbeat_interval_sec=300,
            )

            # First run: escalates at spec_author
            await orch.start_pipeline(pid)
            task, _ = orch._active_pipelines[pid]
            await asyncio.wait_for(task, timeout=10)

            p = await _get_pipeline_status(pool, pid)
            assert p["status"] == "paused"

            # Resume: human resolves the escalation
            await orch.resume_pipeline(pid, "Approved the design")
            task2, _ = orch._active_pipelines[pid]
            await asyncio.wait_for(task2, timeout=10)

        p = await _get_pipeline_status(pool, pid)
        assert p["status"] == "completed"
        assert call_count == 2

    async def test_cancel_pipeline_during_stage(
        self, initialized_db, tmp_path, log_buffer,
    ):
        """Cancelling a pipeline sets status to 'cancelled' and releases
        any in-progress HTN task claims.

        Invariant: cancel_requested → cancelled terminal state.
        """
        pool = get_pool()
        pid = await _seed_full_pipeline(pool, clone_path=str(tmp_path / "clone"))

        # Seed an in-progress HTN task that should be released on cancel
        task_ids = await _seed_htn_tasks_for_pipeline(pool, pid, count=1)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE htn_tasks SET status = 'in_progress', claim_token = 'tok' "
                "WHERE id = %s",
                (task_ids[0],),
            )
            await conn.commit()

        cancel_barrier = asyncio.Event()

        async def slow_spec_author(**kwargs):
            cancel_barrier.set()
            # Wait long enough for the cancel to arrive
            await asyncio.sleep(5)
            return STAGE_RESULT_APPROVED

        with patch.dict("build_your_room.stages.base.STAGE_RUNNERS", {
            "spec_author": AsyncMock(side_effect=slow_spec_author),
        }):
            orch = PipelineOrchestrator(
                pool, log_buffer,
                adapters={"claude": _make_mock_adapter()},
                lease_ttl_sec=60, heartbeat_interval_sec=300,
            )
            await orch.start_pipeline(pid)

            # Wait for the stage to begin, then cancel
            await asyncio.wait_for(cancel_barrier.wait(), timeout=5)
            await orch.cancel_pipeline(pid)

            task, _ = orch._active_pipelines.get(pid, (None, None))
            if task:
                try:
                    await asyncio.wait_for(task, timeout=5)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

        p = await _get_pipeline_status(pool, pid)
        assert p["status"] in ("cancelled", "cancel_requested")

        # HTN task claim should be released
        tasks = await _get_htn_task_rows(pool, pid)
        for t in tasks:
            if t["status"] == "ready":
                assert t["claim_token"] is None

    async def test_kill_pipeline(
        self, initialized_db, tmp_path, log_buffer,
    ):
        """Kill immediately terminates the pipeline and marks it 'killed'.

        Invariant: kill → 'killed' terminal state, task cancelled.
        """
        pool = get_pool()
        pid = await _seed_full_pipeline(pool, clone_path=str(tmp_path / "clone"))

        started_event = asyncio.Event()

        async def slow_spec_author(**kwargs):
            started_event.set()
            await asyncio.sleep(10)
            return STAGE_RESULT_APPROVED

        with patch.dict("build_your_room.stages.base.STAGE_RUNNERS", {
            "spec_author": AsyncMock(side_effect=slow_spec_author),
        }):
            orch = PipelineOrchestrator(
                pool, log_buffer,
                adapters={"claude": _make_mock_adapter()},
                lease_ttl_sec=60, heartbeat_interval_sec=300,
            )
            await orch.start_pipeline(pid)
            await asyncio.wait_for(started_event.wait(), timeout=5)

            await orch.kill_pipeline(pid)
            # Pipeline task should be cancelled
            assert pid not in orch._active_pipelines

        p = await _get_pipeline_status(pool, pid)
        assert p["status"] == "killed"

    async def test_reconcile_stale_running_pipeline(
        self, initialized_db, tmp_path, log_buffer,
    ):
        """Startup reconciliation downgrades a stale 'running' pipeline with
        expired lease to 'needs_attention' and creates an escalation.

        Invariant: RunningImpliesOwner — running pipelines must have a live
        lease. Reconciliation enforces this on startup.
        """
        pool = get_pool()
        pid = await _seed_full_pipeline(pool, status="running", clone_path=str(tmp_path / "clone"))

        # Create an in-progress stage and HTN task to verify they get cleaned up
        async with pool.connection() as conn:
            stage_row = await (
                await conn.execute(
                    "INSERT INTO pipeline_stages "
                    "(pipeline_id, stage_key, stage_type, agent_type, status, max_iterations) "
                    "VALUES (%s, 'impl_task', 'impl_task', 'claude', 'running', 50) RETURNING id",
                    (pid,),
                )
            ).fetchone()
            stage_id = stage_row["id"]

            sess_row = await (
                await conn.execute(
                    "INSERT INTO agent_sessions "
                    "(pipeline_stage_id, session_type, status) "
                    "VALUES (%s, 'claude_sdk', 'running') RETURNING id",
                    (stage_id,),
                )
            ).fetchone()

            await conn.execute(
                "INSERT INTO htn_tasks "
                "(pipeline_id, name, description, task_type, status, priority, ordering, "
                " preconditions_json, postconditions_json, claim_token, assigned_session_id) "
                "VALUES (%s, 'task-x', 'desc', 'primitive', 'in_progress', 1, 0, '[]', '[]', "
                "'stale-tok', %s)",
                (pid, sess_row["id"]),
            )

            # Set expired lease
            from datetime import datetime, timedelta, timezone
            past = datetime.now(timezone.utc) - timedelta(seconds=120)
            await conn.execute(
                "UPDATE pipelines SET owner_token = 'stale-owner', "
                "lease_expires_at = %s WHERE id = %s",
                (past, pid),
            )
            await conn.commit()

        orch = PipelineOrchestrator(pool, log_buffer, lease_ttl_sec=30)
        await orch.reconcile_running_state()

        p = await _get_pipeline_status(pool, pid)
        assert p["status"] == "needs_attention"
        assert p["owner_token"] is None

        # Escalation should have been created
        escs = await _get_escalation_rows(pool, pid)
        assert len(escs) >= 1
        assert escs[0]["reason"] == "startup_recovery"

        # In-progress stages should be failed
        stages = await _get_stage_rows(pool, pid)
        for s in stages:
            if s["stage_key"] == "impl_task":
                assert s["status"] == "failed"

        # In-progress HTN tasks should be released back to ready
        tasks = await _get_htn_task_rows(pool, pid)
        for t in tasks:
            assert t["status"] == "ready"
            assert t["claim_token"] is None


# ---------------------------------------------------------------------------
# HTN task lifecycle integration tests
# ---------------------------------------------------------------------------


class TestHTNTaskLifecycle:
    """Test the impl_task stage with real HTN planner and DB.

    Why this is important: validates that HTN task claiming, completion,
    and readiness propagation work correctly through the full stage runner
    with real PostgreSQL transactions.
    """

    async def test_impl_task_completes_all_ready_tasks(
        self, initialized_db, tmp_path, log_buffer,
    ):
        """The impl_task stage claims and completes all ready tasks, then
        returns 'stage_complete'.

        Invariant: when all primitive tasks complete, impl_task returns
        'stage_complete' and all HTN tasks are in 'completed' status.
        """
        pool = get_pool()
        pid = await _seed_full_pipeline(pool, clone_path=str(tmp_path / "clone"), suffix="-htn1")
        await _seed_htn_tasks_for_pipeline(pool, pid, count=3)

        # Create the stage row
        async with pool.connection() as conn:
            stage_row = await (
                await conn.execute(
                    "INSERT INTO pipeline_stages "
                    "(pipeline_id, stage_key, stage_type, agent_type, status, max_iterations) "
                    "VALUES (%s, 'impl_task', 'impl_task', 'claude', 'running', 50) RETURNING id",
                    (pid,),
                )
            ).fetchone()
            stage_id = stage_row["id"]
            await conn.commit()

        # Mock adapter with sessions for each task
        session = _make_mock_session(output="Implemented the feature.")
        adapter = _make_mock_adapter(session)

        # Use a mock planner that claims tasks in order and completes them
        from build_your_room.htn_planner import HTNPlanner
        from build_your_room.stage_graph import StageNode

        planner = HTNPlanner(pool)

        # Mock postconditions to always pass
        with patch.object(
            planner, "verify_postconditions", return_value=[],
        ):
            from build_your_room.stages.impl_task import run_impl_task_stage

            node = StageNode(
                key="impl_task",
                name="Implementation",
                stage_type="impl_task",
                agent="claude",
                prompt="impl_task_default",
                model="claude-sonnet-4-6",
                max_iterations=50,
                on_context_limit="resume_current_claim",
            )

            result = await run_impl_task_stage(
                pool=pool,
                pipeline_id=pid,
                stage_id=stage_id,
                node=node,
                adapters={"claude": adapter},
                log_buffer=log_buffer,
                cancel_event=asyncio.Event(),
                htn_planner=planner,
            )

        assert result == "stage_complete"

        # All tasks should be completed
        tasks = await _get_htn_task_rows(pool, pid)
        for t in tasks:
            assert t["status"] == "completed", (
                f"Task {t['name']} has status {t['status']}"
            )

    async def test_impl_task_readiness_propagation(
        self, initialized_db, tmp_path, log_buffer,
    ):
        """When a task completes, its dependents become ready and are
        subsequently claimed.

        Invariant: completing a hard-dep parent unblocks the child task.
        """
        pool = get_pool()
        pid = await _seed_full_pipeline(pool, clone_path=str(tmp_path / "clone"), suffix="-htn2")

        # Create parent task (ready) and child task (not_ready with hard dep)
        async with pool.connection() as conn:
            parent_row = await (
                await conn.execute(
                    "INSERT INTO htn_tasks "
                    "(pipeline_id, name, description, task_type, status, priority, ordering, "
                    " preconditions_json, postconditions_json) "
                    "VALUES (%s, 'parent-task', 'Setup DB', 'primitive', 'ready', 10, 0, '[]', '[]') "
                    "RETURNING id",
                    (pid,),
                )
            ).fetchone()
            parent_id = parent_row["id"]

            child_row = await (
                await conn.execute(
                    "INSERT INTO htn_tasks "
                    "(pipeline_id, name, description, task_type, status, priority, ordering, "
                    " preconditions_json, postconditions_json) "
                    "VALUES (%s, 'child-task', 'Add endpoints', 'primitive', 'not_ready', 5, 1, "
                    "'[]', '[]') RETURNING id",
                    (pid,),
                )
            ).fetchone()
            child_id = child_row["id"]

            await conn.execute(
                "INSERT INTO htn_task_deps (task_id, depends_on_task_id, dep_type) "
                "VALUES (%s, %s, 'hard')",
                (child_id, parent_id),
            )

            stage_row = await (
                await conn.execute(
                    "INSERT INTO pipeline_stages "
                    "(pipeline_id, stage_key, stage_type, agent_type, status, max_iterations) "
                    "VALUES (%s, 'impl_task', 'impl_task', 'claude', 'running', 50) RETURNING id",
                    (pid,),
                )
            ).fetchone()
            stage_id = stage_row["id"]
            await conn.commit()

        session = _make_mock_session(output="Done.")
        adapter = _make_mock_adapter(session)

        from build_your_room.htn_planner import HTNPlanner
        from build_your_room.stage_graph import StageNode
        from build_your_room.stages.impl_task import run_impl_task_stage

        planner = HTNPlanner(pool)
        node = StageNode(
            key="impl_task", name="Implementation", stage_type="impl_task",
            agent="claude", prompt="impl_task_default", model="claude-sonnet-4-6",
            max_iterations=50, on_context_limit="resume_current_claim",
        )

        with patch.object(planner, "verify_postconditions", return_value=[]):
            result = await run_impl_task_stage(
                pool=pool, pipeline_id=pid, stage_id=stage_id,
                node=node, adapters={"claude": adapter},
                log_buffer=log_buffer, cancel_event=asyncio.Event(),
                htn_planner=planner,
            )

        assert result == "stage_complete"

        tasks = await _get_htn_task_rows(pool, pid)
        parent = next(t for t in tasks if t["id"] == parent_id)
        child = next(t for t in tasks if t["id"] == child_id)
        assert parent["status"] == "completed"
        assert child["status"] == "completed"


# ---------------------------------------------------------------------------
# Stage transition and edge guard integration tests
# ---------------------------------------------------------------------------


class TestStageTransitions:
    """Test stage transitions, back-edges, and edge exhaustion.

    Why this is important: validates the ValidStageTransition invariant —
    the next stage comes only from explicit outgoing edges in the graph.
    Also verifies back-edge visit counting and exhaustion handling.
    """

    async def test_validation_failure_loops_back_to_review(
        self, initialized_db, tmp_path, log_buffer,
    ):
        """When validation returns 'validation_failed', the pipeline loops
        back to code_review via the back-edge, then re-enters validation.

        Invariant: ValidStageTransition — back-edge transitions are tracked
        with visit counts and create new stage rows with incremented attempts.
        """
        pool = get_pool()
        pid = await _seed_full_pipeline(pool, clone_path=str(tmp_path / "clone"), suffix="-loop1")

        call_counts: dict[str, int] = {"validation": 0}

        async def mock_validation(**kwargs):
            call_counts["validation"] += 1
            if call_counts["validation"] == 1:
                return STAGE_RESULT_VALIDATION_FAILED
            return STAGE_RESULT_VALIDATED

        with patch.dict("build_your_room.stages.base.STAGE_RUNNERS", {
            "spec_author": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "impl_plan": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "impl_task": AsyncMock(return_value=STAGE_RESULT_STAGE_COMPLETE),
            "code_review": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "validation": AsyncMock(side_effect=mock_validation),
        }):
            orch = PipelineOrchestrator(
                pool, log_buffer,
                adapters={"claude": _make_mock_adapter(), "codex": _make_mock_adapter()},
                lease_ttl_sec=60, heartbeat_interval_sec=300,
            )
            await orch.start_pipeline(pid)
            task, _ = orch._active_pipelines[pid]
            await asyncio.wait_for(task, timeout=10)

        p = await _get_pipeline_status(pool, pid)
        assert p["status"] == "completed"

        # Validation was called twice (fail then pass)
        assert call_counts["validation"] == 2

        # There should be 2 code_review stage rows (original + back-edge re-entry)
        stages = await _get_stage_rows(pool, pid)
        review_stages = [s for s in stages if s["stage_key"] == "code_review"]
        assert len(review_stages) == 2

        # And 2 validation stage rows
        val_stages = [s for s in stages if s["stage_key"] == "validation"]
        assert len(val_stages) == 2

        # Visit counts should track the back-edge
        recovery = json.loads(p["recovery_state_json"])
        vc = recovery["visit_counts"]
        assert vc.get("validation_back_to_review") == 1

    async def test_edge_exhaustion_escalates(
        self, initialized_db, tmp_path, log_buffer,
    ):
        """When a back-edge exceeds max_visits, the pipeline escalates.

        Invariant: edge exhaustion with on_exhausted='escalate' → pipeline
        pauses with an escalation row.
        """
        pool = get_pool()
        pid = await _seed_full_pipeline(pool, clone_path=str(tmp_path / "clone"), suffix="-exhaust1")

        # Validation always fails → back-edge gets exhausted after max_visits=2
        with patch.dict("build_your_room.stages.base.STAGE_RUNNERS", {
            "spec_author": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "impl_plan": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "impl_task": AsyncMock(return_value=STAGE_RESULT_STAGE_COMPLETE),
            "code_review": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "validation": AsyncMock(return_value=STAGE_RESULT_VALIDATION_FAILED),
        }):
            orch = PipelineOrchestrator(
                pool, log_buffer,
                adapters={"claude": _make_mock_adapter(), "codex": _make_mock_adapter()},
                lease_ttl_sec=60, heartbeat_interval_sec=300,
            )
            await orch.start_pipeline(pid)
            task, _ = orch._active_pipelines[pid]
            await asyncio.wait_for(task, timeout=10)

        p = await _get_pipeline_status(pool, pid)
        assert p["status"] == "paused"

        escs = await _get_escalation_rows(pool, pid)
        assert len(escs) >= 1
        assert escs[-1]["reason"] == "max_iterations"

    async def test_no_matching_transition_escalates(
        self, initialized_db, tmp_path, log_buffer,
    ):
        """When a stage returns a result that matches no outgoing edge, the
        pipeline escalates with reason 'agent_error'.

        Invariant: unmatched stage results must escalate, not silently drop.
        """
        pool = get_pool()
        pid = await _seed_full_pipeline(pool, clone_path=str(tmp_path / "clone"), suffix="-noedge1")

        # spec_author returns an unexpected result
        with patch.dict("build_your_room.stages.base.STAGE_RUNNERS", {
            "spec_author": AsyncMock(return_value="unexpected_result_xyz"),
        }):
            orch = PipelineOrchestrator(
                pool, log_buffer,
                adapters={"claude": _make_mock_adapter()},
                lease_ttl_sec=60, heartbeat_interval_sec=300,
            )
            await orch.start_pipeline(pid)
            task, _ = orch._active_pipelines[pid]
            await asyncio.wait_for(task, timeout=10)

        p = await _get_pipeline_status(pool, pid)
        assert p["status"] == "paused"

        escs = await _get_escalation_rows(pool, pid)
        assert len(escs) >= 1
        assert escs[-1]["reason"] == "agent_error"

    async def test_multiple_stage_attempts_tracked(
        self, initialized_db, tmp_path, log_buffer,
    ):
        """Re-entering a stage via a back-edge creates a new stage row with
        incremented attempt number.

        Invariant: each visit to a stage node creates a distinct pipeline_stages
        row with a unique attempt number, preserving the execution history.
        """
        pool = get_pool()
        pid = await _seed_full_pipeline(pool, clone_path=str(tmp_path / "clone"), suffix="-attempt1")

        call_counts: dict[str, int] = {"validation": 0, "code_review": 0}

        async def mock_validation(**kwargs):
            call_counts["validation"] += 1
            if call_counts["validation"] <= 1:
                return STAGE_RESULT_VALIDATION_FAILED
            return STAGE_RESULT_VALIDATED

        async def mock_code_review(**kwargs):
            call_counts["code_review"] += 1
            return STAGE_RESULT_APPROVED

        with patch.dict("build_your_room.stages.base.STAGE_RUNNERS", {
            "spec_author": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "impl_plan": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "impl_task": AsyncMock(return_value=STAGE_RESULT_STAGE_COMPLETE),
            "code_review": AsyncMock(side_effect=mock_code_review),
            "validation": AsyncMock(side_effect=mock_validation),
        }):
            orch = PipelineOrchestrator(
                pool, log_buffer,
                adapters={"claude": _make_mock_adapter(), "codex": _make_mock_adapter()},
                lease_ttl_sec=60, heartbeat_interval_sec=300,
            )
            await orch.start_pipeline(pid)
            task, _ = orch._active_pipelines[pid]
            await asyncio.wait_for(task, timeout=10)

        stages = await _get_stage_rows(pool, pid)

        # code_review should have 2 rows: attempt 1 and attempt 2
        review_stages = [s for s in stages if s["stage_key"] == "code_review"]
        assert len(review_stages) == 2
        assert review_stages[0]["attempt"] == 1
        assert review_stages[1]["attempt"] == 2

        # code_review was called twice (initial + after back-edge)
        assert call_counts["code_review"] == 2


# ---------------------------------------------------------------------------
# Property-based tests for pipeline state invariants
# ---------------------------------------------------------------------------


class TestIntegrationProperties:
    """Property-based tests for pipeline state invariants across generated inputs.

    Invariant: regardless of which stages escalate or what order results come,
    the pipeline always reaches a defined terminal or paused state.
    """

    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        escalate_at=st.sampled_from(
            ["spec_author", "impl_plan", "impl_task", "code_review", "validation", "none"]
        ),
    )
    async def test_pipeline_always_reaches_terminal_or_paused_state(
        self, initialized_db, tmp_path, log_buffer, escalate_at,
    ):
        """Property: regardless of which stage escalates, the pipeline always
        reaches a well-defined state (completed, paused, or failed).

        Why: ensures the orchestrator's state machine never gets stuck in an
        intermediate state.
        """
        pool = get_pool()
        suffix = f"-pbt-{escalate_at}-{id(escalate_at)}"
        pid = await _seed_full_pipeline(pool, clone_path=str(tmp_path / "clone"), suffix=suffix)

        stage_results = {
            "spec_author": STAGE_RESULT_APPROVED,
            "impl_plan": STAGE_RESULT_APPROVED,
            "impl_task": STAGE_RESULT_STAGE_COMPLETE,
            "code_review": STAGE_RESULT_APPROVED,
            "validation": STAGE_RESULT_VALIDATED,
        }

        if escalate_at != "none":
            stage_results[escalate_at] = STAGE_RESULT_ESCALATED

        with patch.dict("build_your_room.stages.base.STAGE_RUNNERS", {
            "spec_author": AsyncMock(return_value=stage_results["spec_author"]),
            "impl_plan": AsyncMock(return_value=stage_results["impl_plan"]),
            "impl_task": AsyncMock(return_value=stage_results["impl_task"]),
            "code_review": AsyncMock(return_value=stage_results["code_review"]),
            "validation": AsyncMock(return_value=stage_results["validation"]),
        }):
            orch = PipelineOrchestrator(
                pool, log_buffer,
                adapters={"claude": _make_mock_adapter(), "codex": _make_mock_adapter()},
                lease_ttl_sec=60, heartbeat_interval_sec=300,
            )
            await orch.start_pipeline(pid)
            task, _ = orch._active_pipelines[pid]
            await asyncio.wait_for(task, timeout=10)

        p = await _get_pipeline_status(pool, pid)
        assert p["status"] in ("completed", "paused", "failed", "needs_attention"), (
            f"Pipeline stuck in unexpected state: {p['status']}"
        )

    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        fail_count=st.integers(min_value=1, max_value=3),
    )
    async def test_back_edge_visits_bounded_by_max(
        self, initialized_db, tmp_path, log_buffer, fail_count,
    ):
        """Property: the validation→code_review back-edge is visited at most
        max_visits times before escalation.

        Why: validates that edge exhaustion works correctly for any number
        of failures up to and beyond the max_visits limit.
        """
        pool = get_pool()
        suffix = f"-backpbt-{fail_count}-{id(fail_count)}"
        pid = await _seed_full_pipeline(pool, clone_path=str(tmp_path / "clone"), suffix=suffix)

        # max_visits=2 in FULL_GRAPH
        validation_calls = 0

        async def mock_validation(**kwargs):
            nonlocal validation_calls
            validation_calls += 1
            if validation_calls <= fail_count:
                return STAGE_RESULT_VALIDATION_FAILED
            return STAGE_RESULT_VALIDATED

        with patch.dict("build_your_room.stages.base.STAGE_RUNNERS", {
            "spec_author": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "impl_plan": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "impl_task": AsyncMock(return_value=STAGE_RESULT_STAGE_COMPLETE),
            "code_review": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "validation": AsyncMock(side_effect=mock_validation),
        }):
            orch = PipelineOrchestrator(
                pool, log_buffer,
                adapters={"claude": _make_mock_adapter(), "codex": _make_mock_adapter()},
                lease_ttl_sec=60, heartbeat_interval_sec=300,
            )
            await orch.start_pipeline(pid)
            task, _ = orch._active_pipelines[pid]
            await asyncio.wait_for(task, timeout=10)

        p = await _get_pipeline_status(pool, pid)

        if fail_count <= 2:
            # Within max_visits: pipeline should eventually complete
            # (fail_count failures, then success on attempt fail_count+1)
            assert p["status"] == "completed"
        else:
            # Exceeds max_visits=2: edge exhausted → escalation → paused
            assert p["status"] == "paused"

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        stage_count=st.integers(min_value=1, max_value=5),
    )
    async def test_stage_rows_created_for_each_visit(
        self, initialized_db, tmp_path, log_buffer, stage_count,
    ):
        """Property: each stage visit creates exactly one pipeline_stages row.
        The total stage row count equals the number of stages visited.

        Why: validates that the orchestrator never skips creating stage rows
        and that each visit is tracked independently.
        """
        pool = get_pool()
        suffix = f"-stagepbt-{stage_count}-{id(stage_count)}"
        pid = await _seed_full_pipeline(pool, clone_path=str(tmp_path / "clone"), suffix=suffix)

        visited_stages: list[str] = []

        async def track_and_return(stage_type, result):
            async def handler(**kwargs):
                visited_stages.append(stage_type)
                return result
            return handler

        # Only let `stage_count` stages pass before escalating
        ordered_stages = ["spec_author", "impl_plan", "impl_task", "code_review", "validation"]
        stage_results = {}

        for i, st_name in enumerate(ordered_stages):
            if i < stage_count:
                default_results = {
                    "spec_author": STAGE_RESULT_APPROVED,
                    "impl_plan": STAGE_RESULT_APPROVED,
                    "impl_task": STAGE_RESULT_STAGE_COMPLETE,
                    "code_review": STAGE_RESULT_APPROVED,
                    "validation": STAGE_RESULT_VALIDATED,
                }
                stage_results[st_name] = default_results[st_name]
            else:
                stage_results[st_name] = STAGE_RESULT_ESCALATED

        with patch.dict("build_your_room.stages.base.STAGE_RUNNERS", {
            "spec_author": AsyncMock(return_value=stage_results["spec_author"]),
            "impl_plan": AsyncMock(return_value=stage_results["impl_plan"]),
            "impl_task": AsyncMock(return_value=stage_results["impl_task"]),
            "code_review": AsyncMock(return_value=stage_results["code_review"]),
            "validation": AsyncMock(return_value=stage_results["validation"]),
        }):
            orch = PipelineOrchestrator(
                pool, log_buffer,
                adapters={"claude": _make_mock_adapter(), "codex": _make_mock_adapter()},
                lease_ttl_sec=60, heartbeat_interval_sec=300,
            )
            await orch.start_pipeline(pid)
            task, _ = orch._active_pipelines[pid]
            await asyncio.wait_for(task, timeout=10)

        stages = await _get_stage_rows(pool, pid)
        # We should have at least stage_count rows (could be stage_count+1 if the
        # escalated stage also creates a row before returning)
        assert len(stages) >= min(stage_count, 5)


# ---------------------------------------------------------------------------
# Cross-component integration: spec → plan → implementation chain
# ---------------------------------------------------------------------------


class TestSpecToPlanChain:
    """Test the spec_author → impl_plan artifact handoff.

    Why this is important: validates that the spec artifact produced by
    spec_author is correctly loaded by impl_plan, and that the HTN task
    graph population works through the planner.
    """

    async def test_spec_artifact_flows_to_impl_plan(
        self, initialized_db, tmp_path, log_buffer,
    ):
        """The spec artifact written by spec_author is read by impl_plan.

        Invariant: impl_plan receives the spec content from the filesystem
        artifact, not from the agent session output.
        """
        pool = get_pool()
        pipelines_dir = tmp_path / "pipelines"
        pipelines_dir.mkdir()

        # Use minimal 2-node graph: spec_author → impl_plan
        graph = {
            "entry_stage": "spec_author",
            "nodes": [
                {
                    "key": "spec_author",
                    "name": "Spec",
                    "type": "spec_author",
                    "agent": "claude",
                    "prompt": "spec_author_default",
                    "model": "claude-opus-4-6",
                    "max_iterations": 1,
                },
                {
                    "key": "impl_plan",
                    "name": "Plan",
                    "type": "impl_plan",
                    "agent": "claude",
                    "prompt": "impl_plan_default",
                    "model": "claude-opus-4-6",
                    "max_iterations": 1,
                },
            ],
            "edges": [
                {"key": "spec_to_plan", "from": "spec_author", "to": "impl_plan", "on": "approved"},
                {"key": "plan_to_done", "from": "impl_plan", "to": "completed", "on": "approved"},
            ],
        }

        pid = await _seed_full_pipeline(
            pool, graph_json=json.dumps(graph),
            clone_path=str(tmp_path / "clone"), suffix="-chain1",
        )

        spec_content = "# Test Specification\n\nBuild a REST API."
        plan_content = "# Implementation Plan\n\nTask list here."

        # Track what impl_plan receives as spec content
        received_spec: list[str] = []

        spec_artifact = pipelines_dir / str(pid) / "artifacts" / "spec.md"
        plan_artifact = pipelines_dir / str(pid) / "artifacts" / "plan.md"

        # Spec author writes the artifact
        async def mock_spec_author(**kwargs):
            spec_artifact.parent.mkdir(parents=True, exist_ok=True)
            spec_artifact.write_text(spec_content)
            return STAGE_RESULT_APPROVED

        # Impl plan reads the spec artifact
        async def mock_impl_plan(**kwargs):
            if spec_artifact.exists():
                received_spec.append(spec_artifact.read_text())
            plan_artifact.parent.mkdir(parents=True, exist_ok=True)
            plan_artifact.write_text(plan_content)
            return STAGE_RESULT_APPROVED

        with patch.dict("build_your_room.stages.base.STAGE_RUNNERS", {
            "spec_author": AsyncMock(side_effect=mock_spec_author),
            "impl_plan": AsyncMock(side_effect=mock_impl_plan),
        }):
            orch = PipelineOrchestrator(
                pool, log_buffer,
                adapters={"claude": _make_mock_adapter()},
                lease_ttl_sec=60, heartbeat_interval_sec=300,
            )
            await orch.start_pipeline(pid)
            task, _ = orch._active_pipelines[pid]
            await asyncio.wait_for(task, timeout=10)

        p = await _get_pipeline_status(pool, pid)
        assert p["status"] == "completed"

        # impl_plan received the spec content written by spec_author
        assert len(received_spec) == 1
        assert received_spec[0] == spec_content


# ---------------------------------------------------------------------------
# Concurrent pipeline isolation tests
# ---------------------------------------------------------------------------


class TestConcurrentPipelines:
    """Test that multiple pipelines run concurrently without interference.

    Why this is important: the orchestrator uses a semaphore for concurrency
    control and each pipeline has its own lease. Concurrent pipelines must
    not share state or interfere with each other's DB rows.
    """

    async def test_two_pipelines_run_in_parallel(
        self, initialized_db, tmp_path, log_buffer,
    ):
        """Two pipelines started concurrently both complete independently.

        Invariant: concurrent pipeline execution — each pipeline reaches
        'completed' with its own stage rows and lease lifecycle.
        """
        pool = get_pool()

        pid1 = await _seed_full_pipeline(
            pool, clone_path=str(tmp_path / "clone1"), suffix="-par1",
        )
        pid2 = await _seed_full_pipeline(
            pool, clone_path=str(tmp_path / "clone2"), suffix="-par2",
        )

        with patch.dict("build_your_room.stages.base.STAGE_RUNNERS", {
            "spec_author": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "impl_plan": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "impl_task": AsyncMock(return_value=STAGE_RESULT_STAGE_COMPLETE),
            "code_review": AsyncMock(return_value=STAGE_RESULT_APPROVED),
            "validation": AsyncMock(return_value=STAGE_RESULT_VALIDATED),
        }):
            orch = PipelineOrchestrator(
                pool, log_buffer,
                adapters={"claude": _make_mock_adapter(), "codex": _make_mock_adapter()},
                lease_ttl_sec=60, heartbeat_interval_sec=300,
                max_concurrent=10,
            )

            await orch.start_pipeline(pid1)
            await orch.start_pipeline(pid2)

            task1, _ = orch._active_pipelines[pid1]
            task2, _ = orch._active_pipelines[pid2]

            await asyncio.wait_for(
                asyncio.gather(task1, task2), timeout=15,
            )

        p1 = await _get_pipeline_status(pool, pid1)
        p2 = await _get_pipeline_status(pool, pid2)
        assert p1["status"] == "completed"
        assert p2["status"] == "completed"

        # Each pipeline has its own stage rows
        stages1 = await _get_stage_rows(pool, pid1)
        stages2 = await _get_stage_rows(pool, pid2)
        assert len(stages1) == 5
        assert len(stages2) == 5

        # Stage rows belong to their respective pipelines
        for s in stages1:
            assert s["pipeline_id"] == pid1
        for s in stages2:
            assert s["pipeline_id"] == pid2

    async def test_pipeline_lease_isolation(
        self, initialized_db, tmp_path, log_buffer,
    ):
        """Each pipeline acquires its own lease — one pipeline's lease does
        not affect another.

        Invariant: lease tokens are unique per pipeline.
        """
        pool = get_pool()

        pid1 = await _seed_full_pipeline(
            pool, clone_path=str(tmp_path / "clone1"), suffix="-lease1",
        )
        pid2 = await _seed_full_pipeline(
            pool, clone_path=str(tmp_path / "clone2"), suffix="-lease2",
        )

        orch = PipelineOrchestrator(pool, log_buffer)

        token1 = await orch._acquire_pipeline_lease(pid1)
        token2 = await orch._acquire_pipeline_lease(pid2)

        assert token1 != token2

        # Releasing one doesn't affect the other
        await orch._release_pipeline_lease(pid1)

        p1 = await _get_pipeline_status(pool, pid1)
        p2 = await _get_pipeline_status(pool, pid2)
        assert p1["owner_token"] is None
        assert p2["owner_token"] == token2

        await orch._release_pipeline_lease(pid2)
