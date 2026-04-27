"""Tests for PipelineOrchestrator — leases, reconciliation, stage dispatch.

Property-based tests verify orchestrator state machine invariants across
generated inputs: visit count persistence, lease exclusivity, default
stage results, and pipeline status transitions.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from unittest.mock import AsyncMock, MagicMock

from build_your_room.clone_manager import CloneManager, CloneResult
from build_your_room.db import get_pool
from build_your_room.orchestrator import (
    STAGE_RESULT_APPROVED,
    STAGE_RESULT_ESCALATED,
    STAGE_RESULT_STAGE_COMPLETE,
    STAGE_RESULT_VALIDATED,
    STAGE_RESULT_VALIDATION_FAILED,
    PipelineOrchestrator,
)
from build_your_room.recovery import RecoveryManager
from build_your_room.streaming import LogBuffer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FULL_GRAPH_JSON = json.dumps(
    {
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
            {
                "key": "impl_to_review",
                "from": "impl_task",
                "to": "code_review",
                "on": "stage_complete",
            },
            {
                "key": "review_to_validation",
                "from": "code_review",
                "to": "validation",
                "on": "approved",
            },
            {
                "key": "validation_to_done",
                "from": "validation",
                "to": "completed",
                "on": "validated",
            },
        ],
    }
)


async def _seed_pipeline(pool, *, status: str = "pending", clone_path: str = "/tmp/test-clone"):
    """Insert a repo, pipeline_def, and pipeline for testing. Returns pipeline_id."""
    # Ensure clone directory exists so _ensure_clone skips re-cloning
    Path(clone_path).mkdir(parents=True, exist_ok=True)
    async with pool.connection() as conn:
        repo_row = await (
            await conn.execute(
                "INSERT INTO repos (name, local_path) VALUES ('test-repo', '/tmp/test-repo') "
                "RETURNING id"
            )
        ).fetchone()
        repo_id = repo_row["id"]

        pdef_row = await (
            await conn.execute(
                "INSERT INTO pipeline_defs (name, stage_graph_json) "
                "VALUES ('test-pipeline-def', %s) RETURNING id",
                (FULL_GRAPH_JSON,),
            )
        ).fetchone()
        pdef_id = pdef_row["id"]

        pipeline_row = await (
            await conn.execute(
                "INSERT INTO pipelines "
                "(pipeline_def_id, repo_id, clone_path, review_base_rev, status) "
                "VALUES (%s, %s, %s, 'abc123', %s) RETURNING id",
                (pdef_id, repo_id, clone_path, status),
            )
        ).fetchone()
        await conn.commit()
        return pipeline_row["id"]


# ---------------------------------------------------------------------------
# Lease tests
# ---------------------------------------------------------------------------


class TestLeaseManagement:
    async def test_acquire_lease(self, initialized_db):
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        orch = PipelineOrchestrator(pool, LogBuffer())

        token = await orch._acquire_pipeline_lease(pid)
        assert token  # non-empty UUID string

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT owner_token, lease_expires_at FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()
        assert row["owner_token"] == token
        assert row["lease_expires_at"] is not None

    async def test_acquire_lease_fails_when_already_owned(self, initialized_db):
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        orch = PipelineOrchestrator(pool, LogBuffer())

        await orch._acquire_pipeline_lease(pid)
        with pytest.raises(RuntimeError, match="another owner"):
            await orch._acquire_pipeline_lease(pid)

    async def test_acquire_lease_succeeds_after_expiry(self, initialized_db):
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        orch = PipelineOrchestrator(pool, LogBuffer(), lease_ttl_sec=1)

        token1 = await orch._acquire_pipeline_lease(pid)
        # Manually expire the lease
        past = datetime.now(timezone.utc) - timedelta(seconds=10)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET lease_expires_at = %s WHERE id = %s",
                (past, pid),
            )
            await conn.commit()

        token2 = await orch._acquire_pipeline_lease(pid)
        assert token2 != token1

    async def test_release_lease(self, initialized_db):
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        orch = PipelineOrchestrator(pool, LogBuffer())

        await orch._acquire_pipeline_lease(pid)
        await orch._release_pipeline_lease(pid)

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT owner_token, lease_expires_at FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()
        assert row["owner_token"] is None
        assert row["lease_expires_at"] is None

    async def test_renew_leases_updates_timestamps(self, initialized_db):
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        orch = PipelineOrchestrator(pool, LogBuffer())

        await orch._acquire_pipeline_lease(pid)

        async with pool.connection() as conn:
            row_before = await (
                await conn.execute(
                    "SELECT lease_expires_at FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()

        await asyncio.sleep(0.05)
        await orch.renew_leases(pid)

        async with pool.connection() as conn:
            row_after = await (
                await conn.execute(
                    "SELECT lease_expires_at FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()

        assert row_after["lease_expires_at"] > row_before["lease_expires_at"]


# ---------------------------------------------------------------------------
# Reconciliation tests
# ---------------------------------------------------------------------------


class TestReconciliation:
    async def test_reconcile_downgrades_expired_pipeline(self, initialized_db):
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        # Set an expired lease
        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET owner_token = 'old-owner', "
                "lease_expires_at = %s WHERE id = %s",
                (past, pid),
            )
            await conn.commit()

        orch = PipelineOrchestrator(pool, LogBuffer())
        await orch.reconcile_running_state()

        async with pool.connection() as conn:
            row = await (
                await conn.execute("SELECT status, owner_token FROM pipelines WHERE id = %s", (pid,))
            ).fetchone()
        assert row["status"] == "needs_attention"
        assert row["owner_token"] is None

    async def test_reconcile_creates_escalation(self, initialized_db):
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET owner_token = 'old-owner', "
                "lease_expires_at = %s WHERE id = %s",
                (past, pid),
            )
            await conn.commit()

        orch = PipelineOrchestrator(pool, LogBuffer())
        await orch.reconcile_running_state()

        async with pool.connection() as conn:
            esc_row = await (
                await conn.execute(
                    "SELECT reason, status FROM escalations WHERE pipeline_id = %s", (pid,)
                )
            ).fetchone()
        assert esc_row is not None
        assert esc_row["reason"] == "startup_recovery"
        assert esc_row["status"] == "open"

    async def test_reconcile_skips_live_lease(self, initialized_db):
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        future = datetime.now(timezone.utc) + timedelta(seconds=300)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET owner_token = 'live-owner', "
                "lease_expires_at = %s WHERE id = %s",
                (future, pid),
            )
            await conn.commit()

        orch = PipelineOrchestrator(pool, LogBuffer())
        await orch.reconcile_running_state()

        async with pool.connection() as conn:
            row = await (
                await conn.execute("SELECT status FROM pipelines WHERE id = %s", (pid,))
            ).fetchone()
        assert row["status"] == "running"

    async def test_reconcile_releases_in_progress_tasks(self, initialized_db):
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET owner_token = 'old-owner', "
                "lease_expires_at = %s WHERE id = %s",
                (past, pid),
            )
            # Create an in-progress HTN task
            await conn.execute(
                "INSERT INTO htn_tasks "
                "(pipeline_id, name, description, task_type, status, priority, ordering, "
                " claim_token, claim_owner_token) "
                "VALUES (%s, 'test-task', 'a test', 'primitive', 'in_progress', 1, 1, "
                " 'claim-abc', 'old-owner')",
                (pid,),
            )
            await conn.commit()

        orch = PipelineOrchestrator(pool, LogBuffer())
        await orch.reconcile_running_state()

        async with pool.connection() as conn:
            task_row = await (
                await conn.execute(
                    "SELECT status, claim_token FROM htn_tasks WHERE pipeline_id = %s", (pid,)
                )
            ).fetchone()
        assert task_row["status"] == "ready"
        assert task_row["claim_token"] is None

    async def test_reconcile_dirty_workspace_snapshots(self, initialized_db, tmp_path):
        pool = get_pool()
        clone_path = str(tmp_path / "clone")
        pid = await _seed_pipeline(pool, status="running", clone_path=clone_path)

        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET owner_token = 'old-owner', "
                "lease_expires_at = %s, workspace_state = 'dirty_live' WHERE id = %s",
                (past, pid),
            )
            await conn.commit()

        log_buffer = LogBuffer()
        recovery_mgr = RecoveryManager(
            pool, log_buffer, pipelines_dir=tmp_path / "pipelines"
        )
        orch = PipelineOrchestrator(pool, log_buffer, recovery_manager=recovery_mgr)
        await orch.reconcile_running_state()

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT workspace_state, dirty_snapshot_artifact FROM pipelines WHERE id = %s",
                    (pid,),
                )
            ).fetchone()
        assert row["workspace_state"] == "clean"
        assert row["dirty_snapshot_artifact"] is not None


# ---------------------------------------------------------------------------
# Stage dispatch tests
# ---------------------------------------------------------------------------


class TestStageDispatch:
    async def test_run_stage_creates_stage_row(self, initialized_db):
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        orch = PipelineOrchestrator(pool, LogBuffer())
        _, graph = await orch._load_pipeline_and_graph(pid)

        cancel = asyncio.Event()
        result = await orch._run_stage(pid, "spec_author", graph, cancel)

        assert result == "approved"

        async with pool.connection() as conn:
            stage_row = await (
                await conn.execute(
                    "SELECT stage_key, stage_type, agent_type, status, attempt "
                    "FROM pipeline_stages WHERE pipeline_id = %s",
                    (pid,),
                )
            ).fetchone()
        assert stage_row["stage_key"] == "spec_author"
        assert stage_row["stage_type"] == "spec_author"
        assert stage_row["agent_type"] == "claude"
        assert stage_row["status"] == "skipped"  # no adapter registered
        assert stage_row["attempt"] == 1

    async def test_run_stage_increments_attempt(self, initialized_db):
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        orch = PipelineOrchestrator(pool, LogBuffer())
        _, graph = await orch._load_pipeline_and_graph(pid)

        cancel = asyncio.Event()
        await orch._run_stage(pid, "spec_author", graph, cancel)
        await orch._run_stage(pid, "spec_author", graph, cancel)

        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT attempt FROM pipeline_stages "
                    "WHERE pipeline_id = %s AND stage_key = 'spec_author' "
                    "ORDER BY attempt",
                    (pid,),
                )
            ).fetchall()
        assert [r["attempt"] for r in rows] == [1, 2]

    async def test_default_stage_results(self):
        assert PipelineOrchestrator._default_stage_result("spec_author") == "approved"
        assert PipelineOrchestrator._default_stage_result("impl_plan") == "approved"
        assert PipelineOrchestrator._default_stage_result("impl_task") == "stage_complete"
        assert PipelineOrchestrator._default_stage_result("code_review") == "approved"
        assert PipelineOrchestrator._default_stage_result("validation") == "validated"
        assert PipelineOrchestrator._default_stage_result("unknown") == "approved"


# ---------------------------------------------------------------------------
# Escalation tests
# ---------------------------------------------------------------------------


class TestEscalation:
    async def test_escalate_creates_record_and_pauses(self, initialized_db):
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")
        orch = PipelineOrchestrator(pool, LogBuffer())

        esc_id = await orch.escalate(
            pid, stage_id=None, reason="test_reason", context={"message": "test context"}
        )
        assert esc_id > 0

        async with pool.connection() as conn:
            esc_row = await (
                await conn.execute(
                    "SELECT reason, context_json, status FROM escalations WHERE id = %s",
                    (esc_id,),
                )
            ).fetchone()
            pipeline_row = await (
                await conn.execute("SELECT status FROM pipelines WHERE id = %s", (pid,))
            ).fetchone()

        assert esc_row["reason"] == "test_reason"
        assert json.loads(esc_row["context_json"])["message"] == "test context"
        assert esc_row["status"] == "open"
        assert pipeline_row["status"] == "paused"


# ---------------------------------------------------------------------------
# Pipeline lifecycle tests
# ---------------------------------------------------------------------------


class TestPipelineLifecycle:
    async def test_run_pipeline_happy_path(self, initialized_db):
        """Pipeline runs through all stages (skipped — no adapters) and completes."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        log_buffer = LogBuffer()
        orch = PipelineOrchestrator(pool, log_buffer)

        cancel = asyncio.Event()
        await orch._run_pipeline(pid, cancel)

        async with pool.connection() as conn:
            row = await (
                await conn.execute("SELECT status FROM pipelines WHERE id = %s", (pid,))
            ).fetchone()
        assert row["status"] == "completed"

        # Should have created stage rows for all 5 stages
        async with pool.connection() as conn:
            stages = await (
                await conn.execute(
                    "SELECT stage_key FROM pipeline_stages WHERE pipeline_id = %s "
                    "ORDER BY id",
                    (pid,),
                )
            ).fetchall()
        stage_keys = [s["stage_key"] for s in stages]
        assert stage_keys == ["spec_author", "impl_plan", "impl_task", "code_review", "validation"]

    async def test_cancellation_marks_cancelled(self, initialized_db):
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        log_buffer = LogBuffer()
        orch = PipelineOrchestrator(pool, log_buffer)

        cancel = asyncio.Event()
        cancel.set()  # Pre-set so it cancels immediately
        await orch._run_pipeline(pid, cancel)

        async with pool.connection() as conn:
            row = await (
                await conn.execute("SELECT status FROM pipelines WHERE id = %s", (pid,))
            ).fetchone()
        assert row["status"] == "cancelled"

    async def test_load_pipeline_not_found(self, initialized_db):
        pool = get_pool()
        orch = PipelineOrchestrator(pool, LogBuffer())
        with pytest.raises(ValueError, match="not found"):
            await orch._load_pipeline_and_graph(99999)

    async def test_visit_counts_persist_in_recovery_state(self, initialized_db):
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        log_buffer = LogBuffer()
        orch = PipelineOrchestrator(pool, log_buffer)

        cancel = asyncio.Event()
        await orch._run_pipeline(pid, cancel)

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT recovery_state_json FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()

        recovery = json.loads(row["recovery_state_json"])
        assert "visit_counts" in recovery
        # Should have traversed all 5 edges in the happy path
        assert len(recovery["visit_counts"]) == 5

    async def test_resume_pipeline_resolves_escalation(self, initialized_db):
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="paused")
        log_buffer = LogBuffer()
        orch = PipelineOrchestrator(pool, log_buffer)

        # Create an open escalation
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO escalations (pipeline_id, reason, context_json) "
                "VALUES (%s, 'test', '{}')",
                (pid,),
            )
            await conn.commit()

        await orch.resume_pipeline(pid, "go ahead")

        async with pool.connection() as conn:
            esc_row = await (
                await conn.execute(
                    "SELECT status, resolution FROM escalations WHERE pipeline_id = %s", (pid,)
                )
            ).fetchone()
        assert esc_row["status"] == "resolved"
        assert esc_row["resolution"] == "go ahead"

    async def test_kill_pipeline_sets_killed(self, initialized_db):
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")
        log_buffer = LogBuffer()
        orch = PipelineOrchestrator(pool, log_buffer)

        await orch.kill_pipeline(pid)

        async with pool.connection() as conn:
            row = await (
                await conn.execute("SELECT status FROM pipelines WHERE id = %s", (pid,))
            ).fetchone()
        assert row["status"] == "killed"

    async def test_kill_pipeline_releases_htn_claims(self, initialized_db):
        """Kill must release in-progress HTN claims so they aren't orphaned.

        Spec line 540: kill ``releases claims`` even when the pipeline was
        not in ``_active_pipelines`` (server-restart case).
        """
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO htn_tasks "
                "(pipeline_id, name, description, task_type, status, priority, ordering, "
                " claim_token, claim_owner_token) "
                "VALUES (%s, 'task-a', 'desc', 'primitive', 'in_progress', 1, 1, "
                " 'claim-tok', 'owner-tok')",
                (pid,),
            )
            await conn.commit()

        orch = PipelineOrchestrator(pool, LogBuffer())
        await orch.kill_pipeline(pid)

        async with pool.connection() as conn:
            task_row = await (
                await conn.execute(
                    "SELECT status, claim_token FROM htn_tasks WHERE pipeline_id = %s",
                    (pid,),
                )
            ).fetchone()
        assert task_row["status"] == "ready"
        assert task_row["claim_token"] is None

    async def test_kill_pipeline_marks_running_sessions_killed(self, initialized_db):
        """Kill must mark live agent sessions and stages with status='killed'.

        Mirrors the cancel cascade but with the 'killed' terminal status
        from the agent_sessions / pipeline_stages enums.
        """
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        async with pool.connection() as conn:
            stage_row = await (
                await conn.execute(
                    "INSERT INTO pipeline_stages "
                    "(pipeline_id, stage_key, stage_type, agent_type, status, max_iterations) "
                    "VALUES (%s, 'impl_task', 'impl_task', 'claude', 'running', 50) "
                    "RETURNING id",
                    (pid,),
                )
            ).fetchone()
            await conn.execute(
                "INSERT INTO agent_sessions "
                "(pipeline_stage_id, session_type, status) "
                "VALUES (%s, 'claude_sdk', 'running')",
                (stage_row["id"],),
            )
            await conn.commit()

        orch = PipelineOrchestrator(pool, LogBuffer())
        await orch.kill_pipeline(pid)

        async with pool.connection() as conn:
            sess_row = await (
                await conn.execute(
                    "SELECT status FROM agent_sessions "
                    "WHERE pipeline_stage_id IN ("
                    "  SELECT id FROM pipeline_stages WHERE pipeline_id = %s"
                    ")",
                    (pid,),
                )
            ).fetchone()
            stage_check = await (
                await conn.execute(
                    "SELECT status FROM pipeline_stages WHERE pipeline_id = %s",
                    (pid,),
                )
            ).fetchone()

        assert sess_row["status"] == "killed"
        assert stage_check["status"] == "killed"

    async def test_kill_pipeline_drains_active_task(self, initialized_db):
        """Kill cancels the active asyncio task and awaits its drain.

        We register a fake long-running task in ``_active_pipelines`` and
        verify ``kill_pipeline`` cancels it and finishes (i.e. doesn't leave
        the task pending). This guards against the prior behaviour where
        kill returned before the SDK adapter's ``__aexit__`` could run.
        """
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        orch = PipelineOrchestrator(pool, LogBuffer())

        cancel_event = asyncio.Event()
        started = asyncio.Event()

        async def long_running() -> None:
            started.set()
            try:
                # Sleep for a long time; kill will cancel us.
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # Simulate adapter __aexit__ doing brief cleanup.
                await asyncio.sleep(0)
                raise

        fake_task = asyncio.create_task(long_running(), name=f"fake-{pid}")
        orch._active_pipelines[pid] = (fake_task, cancel_event)
        await started.wait()

        await orch.kill_pipeline(pid)

        # The fake task must have been awaited to completion (cancelled).
        assert fake_task.done()
        # The active-pipelines slot must be cleared.
        assert pid not in orch._active_pipelines
        async with pool.connection() as conn:
            row = await (
                await conn.execute("SELECT status FROM pipelines WHERE id = %s", (pid,))
            ).fetchone()
        assert row["status"] == "killed"

    async def test_kill_pipeline_drain_timeout_does_not_block(self, initialized_db):
        """A hung task must not stall ``kill_pipeline`` past the drain timeout.

        Simulates an SDK that ignores ``CancelledError`` (e.g., a subprocess
        write that swallows interrupts). The kill path must still complete
        within roughly ``kill_drain_timeout_sec`` and proceed to the DB
        cascade. Bound the assertion at 2x the timeout to be tolerant of
        scheduling jitter.
        """
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        orch = PipelineOrchestrator(pool, LogBuffer(), kill_drain_timeout_sec=0.1)

        cancel_event = asyncio.Event()
        started = asyncio.Event()

        async def hung_task() -> None:
            started.set()
            # Shielded sleep — ignores cancellation, mimicking a stuck
            # subprocess write.
            try:
                await asyncio.shield(asyncio.sleep(3600))
            except asyncio.CancelledError:
                # Re-enter the shield once more so the timeout actually
                # fires; in production the SDK would eventually give up.
                await asyncio.shield(asyncio.sleep(3600))

        fake_task = asyncio.create_task(hung_task(), name=f"hung-{pid}")
        orch._active_pipelines[pid] = (fake_task, cancel_event)
        await started.wait()

        loop = asyncio.get_event_loop()
        t0 = loop.time()
        await orch.kill_pipeline(pid)
        elapsed = loop.time() - t0
        assert elapsed < 2.0, f"kill_pipeline blocked for {elapsed:.2f}s"

        # Even though the task is still hanging, the DB must reflect killed.
        async with pool.connection() as conn:
            row = await (
                await conn.execute("SELECT status FROM pipelines WHERE id = %s", (pid,))
            ).fetchone()
        assert row["status"] == "killed"

        # Clean up the hung task so pytest doesn't complain about
        # un-awaited coroutines at teardown.
        fake_task.cancel()
        try:
            await asyncio.wait_for(fake_task, timeout=0.2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    async def test_kill_pipeline_works_without_active_task(self, initialized_db):
        """Kill must work for pipelines not in _active_pipelines.

        Covers the server-restart scenario: the orchestrator's in-memory
        cache is empty but the DB still shows status='running'. Kill should
        run the full DB cascade via handle_kill regardless.
        """
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO pipeline_stages "
                "(pipeline_id, stage_key, stage_type, agent_type, status, max_iterations) "
                "VALUES (%s, 'impl_task', 'impl_task', 'claude', 'running', 50)",
                (pid,),
            )
            await conn.commit()

        orch = PipelineOrchestrator(pool, LogBuffer())
        # No entry in _active_pipelines — simulate post-restart kill.
        assert pid not in orch._active_pipelines

        await orch.kill_pipeline(pid)

        async with pool.connection() as conn:
            pipe_row = await (
                await conn.execute("SELECT status FROM pipelines WHERE id = %s", (pid,))
            ).fetchone()
            stage_row = await (
                await conn.execute(
                    "SELECT status FROM pipeline_stages WHERE pipeline_id = %s", (pid,)
                )
            ).fetchone()
        assert pipe_row["status"] == "killed"
        assert stage_row["status"] == "killed"


# ---------------------------------------------------------------------------
# Strategies for property-based tests
# ---------------------------------------------------------------------------

_stage_types = st.sampled_from(
    ["spec_author", "impl_plan", "impl_task", "code_review", "validation"]
)

_visit_count_keys = st.from_regex(r"[a-z][a-z0-9_]{1,20}", fullmatch=True)

_valid_results = [
    STAGE_RESULT_APPROVED,
    STAGE_RESULT_STAGE_COMPLETE,
    STAGE_RESULT_VALIDATION_FAILED,
    STAGE_RESULT_VALIDATED,
    STAGE_RESULT_ESCALATED,
]

_pipeline_statuses = st.sampled_from(
    ["pending", "running", "paused", "cancel_requested", "cancelled",
     "killed", "completed", "failed", "needs_attention"]
)


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestOrchestratorProperties:
    """Property-based tests for orchestrator state machine invariants."""

    @settings(max_examples=50)
    @given(
        visit_counts=st.dictionaries(
            keys=_visit_count_keys,
            values=st.integers(min_value=0, max_value=100),
            min_size=0,
            max_size=15,
        ),
    )
    def test_visit_counts_json_roundtrip(self, visit_counts) -> None:
        """Property: visit counts survive JSON serialization roundtrip.

        Invariant: _load_visit_counts(json.dumps({"visit_counts": vc})) == vc
        for all valid visit count dicts.
        """
        recovery_json = json.dumps({"visit_counts": visit_counts})
        pipeline = {"recovery_state_json": recovery_json}
        loaded = PipelineOrchestrator._load_visit_counts(pipeline)
        assert loaded == visit_counts

    @settings(max_examples=30)
    @given(
        recovery_json=st.one_of(
            st.none(),
            st.just(""),
            st.just("not-json"),
            st.just("null"),
            st.just('{"other_key": 42}'),
        ),
    )
    def test_visit_counts_graceful_on_invalid_json(self, recovery_json) -> None:
        """Property: _load_visit_counts returns empty dict for missing/invalid data.

        Invariant: never raises, always returns dict (possibly empty).
        """
        pipeline = {"recovery_state_json": recovery_json}
        loaded = PipelineOrchestrator._load_visit_counts(pipeline)
        assert isinstance(loaded, dict)

    @settings(max_examples=50)
    @given(stage_type=st.text(min_size=1, max_size=30, alphabet="abcdefghijklmnopqrstuvwxyz_"))
    def test_default_stage_result_always_returns_string(self, stage_type) -> None:
        """Property: _default_stage_result always returns a non-empty string.

        Invariant: for all stage type strings, result is a valid stage result.
        """
        result = PipelineOrchestrator._default_stage_result(stage_type)
        assert isinstance(result, str)
        assert len(result) > 0
        assert result in _valid_results

    @settings(max_examples=30)
    @given(stage_type=_stage_types)
    def test_known_stage_types_have_specific_results(self, stage_type) -> None:
        """Property: known stage types always map to their expected result.

        Invariant: the mapping is deterministic and total for known types.
        """
        result = PipelineOrchestrator._default_stage_result(stage_type)
        expected = {
            "spec_author": STAGE_RESULT_APPROVED,
            "impl_plan": STAGE_RESULT_APPROVED,
            "impl_task": STAGE_RESULT_STAGE_COMPLETE,
            "code_review": STAGE_RESULT_APPROVED,
            "validation": STAGE_RESULT_VALIDATED,
        }
        assert result == expected[stage_type]

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(data=st.data())
    @pytest.mark.asyncio
    async def test_lease_acquire_release_reacquire_cycle(self, initialized_db, data) -> None:
        """Property: acquire → release → reacquire always succeeds.

        Invariant: a released lease can always be re-acquired by any process.
        """
        pool = get_pool()
        suffix = uuid.uuid4().hex[:8]
        async with pool.connection() as conn:
            repo_row = await (
                await conn.execute(
                    "INSERT INTO repos (name, local_path) VALUES (%s, '/tmp/r') RETURNING id",
                    (f"repo-{suffix}",),
                )
            ).fetchone()
            pdef_row = await (
                await conn.execute(
                    "INSERT INTO pipeline_defs (name, stage_graph_json) "
                    "VALUES (%s, %s) RETURNING id",
                    (f"def-{suffix}", FULL_GRAPH_JSON),
                )
            ).fetchone()
            pipe_row = await (
                await conn.execute(
                    "INSERT INTO pipelines (pipeline_def_id, repo_id, clone_path, "
                    "review_base_rev, status) VALUES (%s, %s, '/tmp/c', 'abc', 'pending') "
                    "RETURNING id",
                    (pdef_row["id"], repo_row["id"]),
                )
            ).fetchone()
            await conn.commit()
            pid = pipe_row["id"]

        orch = PipelineOrchestrator(pool, LogBuffer())

        # Cycle N times
        cycles = data.draw(st.integers(min_value=1, max_value=3))
        for _ in range(cycles):
            token = await orch._acquire_pipeline_lease(pid)
            assert token
            await orch._release_pipeline_lease(pid)

        # Final acquire should work
        final_token = await orch._acquire_pipeline_lease(pid)
        assert final_token

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(data=st.data())
    @pytest.mark.asyncio
    async def test_lease_exclusivity(self, initialized_db, data) -> None:
        """Property: two concurrent lease acquisitions cannot both succeed.

        Invariant: RunningImpliesOwner — at most one owner at a time.
        """
        pool = get_pool()
        suffix = uuid.uuid4().hex[:8]
        async with pool.connection() as conn:
            repo_row = await (
                await conn.execute(
                    "INSERT INTO repos (name, local_path) VALUES (%s, '/tmp/r') RETURNING id",
                    (f"repo-exc-{suffix}",),
                )
            ).fetchone()
            pdef_row = await (
                await conn.execute(
                    "INSERT INTO pipeline_defs (name, stage_graph_json) "
                    "VALUES (%s, %s) RETURNING id",
                    (f"def-exc-{suffix}", FULL_GRAPH_JSON),
                )
            ).fetchone()
            pipe_row = await (
                await conn.execute(
                    "INSERT INTO pipelines (pipeline_def_id, repo_id, clone_path, "
                    "review_base_rev, status) VALUES (%s, %s, '/tmp/c', 'abc', 'pending') "
                    "RETURNING id",
                    (pdef_row["id"], repo_row["id"]),
                )
            ).fetchone()
            await conn.commit()
            pid = pipe_row["id"]

        orch = PipelineOrchestrator(pool, LogBuffer())
        token1 = await orch._acquire_pipeline_lease(pid)
        assert token1

        with pytest.raises(RuntimeError, match="another owner"):
            await orch._acquire_pipeline_lease(pid)

    @settings(
        max_examples=8,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        n_expired=st.integers(min_value=1, max_value=3),
    )
    @pytest.mark.asyncio
    async def test_reconciliation_downgrades_all_expired(
        self, initialized_db, n_expired
    ) -> None:
        """Property: all pipelines with expired leases are downgraded during reconciliation.

        Invariant: after reconcile_running_state(), no pipeline has status='running'
        with an expired lease.
        """
        pool = get_pool()
        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        pids = []

        for i in range(n_expired):
            suffix = uuid.uuid4().hex[:8]
            async with pool.connection() as conn:
                repo_row = await (
                    await conn.execute(
                        "INSERT INTO repos (name, local_path) VALUES (%s, '/tmp/r') RETURNING id",
                        (f"repo-rec-{suffix}",),
                    )
                ).fetchone()
                pdef_row = await (
                    await conn.execute(
                        "INSERT INTO pipeline_defs (name, stage_graph_json) "
                        "VALUES (%s, %s) RETURNING id",
                        (f"def-rec-{suffix}", FULL_GRAPH_JSON),
                    )
                ).fetchone()
                pipe_row = await (
                    await conn.execute(
                        "INSERT INTO pipelines (pipeline_def_id, repo_id, clone_path, "
                        "review_base_rev, status, owner_token, lease_expires_at) "
                        "VALUES (%s, %s, '/tmp/c', 'abc', 'running', 'old', %s) "
                        "RETURNING id",
                        (pdef_row["id"], repo_row["id"], past),
                    )
                ).fetchone()
                await conn.commit()
                pids.append(pipe_row["id"])

        orch = PipelineOrchestrator(pool, LogBuffer())
        await orch.reconcile_running_state()

        async with pool.connection() as conn:
            for pid in pids:
                row = await (
                    await conn.execute(
                        "SELECT status, owner_token FROM pipelines WHERE id = %s", (pid,)
                    )
                ).fetchone()
                assert row["status"] == "needs_attention", (
                    f"Pipeline {pid} should be downgraded but is {row['status']}"
                )
                assert row["owner_token"] is None

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(data=st.data())
    @pytest.mark.asyncio
    async def test_completed_pipeline_has_all_stage_rows(self, initialized_db, data) -> None:
        """Property: a completed pipeline run has exactly one stage row per graph node.

        Invariant: after _run_pipeline completes, pipeline_stages contains one row
        per stage in the happy path, all with status='skipped' (no adapters).
        """
        pool = get_pool()
        suffix = uuid.uuid4().hex[:8]
        async with pool.connection() as conn:
            repo_row = await (
                await conn.execute(
                    "INSERT INTO repos (name, local_path) VALUES (%s, '/tmp/r') RETURNING id",
                    (f"repo-comp-{suffix}",),
                )
            ).fetchone()
            pdef_row = await (
                await conn.execute(
                    "INSERT INTO pipeline_defs (name, stage_graph_json) "
                    "VALUES (%s, %s) RETURNING id",
                    (f"def-comp-{suffix}", FULL_GRAPH_JSON),
                )
            ).fetchone()
            pipe_row = await (
                await conn.execute(
                    "INSERT INTO pipelines (pipeline_def_id, repo_id, clone_path, "
                    "review_base_rev, status) VALUES (%s, %s, '/tmp/c', 'abc', 'pending') "
                    "RETURNING id",
                    (pdef_row["id"], repo_row["id"]),
                )
            ).fetchone()
            await conn.commit()
            pid = pipe_row["id"]

        Path("/tmp/c").mkdir(parents=True, exist_ok=True)
        orch = PipelineOrchestrator(pool, LogBuffer())
        cancel = asyncio.Event()
        await orch._run_pipeline(pid, cancel)

        async with pool.connection() as conn:
            row = await (
                await conn.execute("SELECT status FROM pipelines WHERE id = %s", (pid,))
            ).fetchone()
            stages = await (
                await conn.execute(
                    "SELECT stage_key, status FROM pipeline_stages "
                    "WHERE pipeline_id = %s ORDER BY id",
                    (pid,),
                )
            ).fetchall()

        assert row["status"] == "completed"
        assert len(stages) == 5
        for s in stages:
            assert s["status"] == "skipped"  # no adapters registered


# ---------------------------------------------------------------------------
# CloneManager-orchestrator integration tests
# ---------------------------------------------------------------------------


class TestEnsureClone:
    """Tests for _ensure_clone: clone lifecycle wiring in the orchestrator."""

    async def test_skips_when_clone_exists(self, initialized_db, tmp_path):
        """_ensure_clone skips create_clone when clone_path directory already exists.

        Invariant: Existing clone directories are reused (e.g., resumed pipelines).
        """
        pool = get_pool()
        clone_dir = tmp_path / "existing-clone"
        clone_dir.mkdir()
        pid = await _seed_pipeline(pool, clone_path=str(clone_dir))

        mock_cm = MagicMock(spec=CloneManager)
        mock_cm.create_clone = AsyncMock()
        orch = PipelineOrchestrator(pool, LogBuffer(), clone_manager=mock_cm)

        await orch._ensure_clone(pid)

        mock_cm.create_clone.assert_not_called()

    async def test_clones_when_clone_path_empty(self, initialized_db, tmp_path):
        """_ensure_clone calls create_clone when clone_path is empty string.

        Invariant: Fresh pipelines (clone_path='') trigger cloning.
        """
        pool = get_pool()
        pid = await _seed_pipeline(pool, clone_path="")

        clone_dir = tmp_path / "new-clone"
        mock_cm = MagicMock(spec=CloneManager)
        mock_cm.create_clone = AsyncMock(return_value=CloneResult(
            clone_path=clone_dir, review_base_rev="abc12345def", workspace_ref=None,
        ))
        log_buffer = LogBuffer()
        orch = PipelineOrchestrator(pool, log_buffer, clone_manager=mock_cm)

        await orch._ensure_clone(pid)

        mock_cm.create_clone.assert_awaited_once()
        call_args = mock_cm.create_clone.call_args
        assert call_args[0][0] == pid  # pipeline_id
        # Verify the log message was emitted
        assert any("Clone created" in msg for msg in log_buffer._history.get(pid, []))

    async def test_reclones_when_directory_missing(self, initialized_db, tmp_path):
        """_ensure_clone re-clones when clone_path is set but directory doesn't exist.

        Invariant: Missing clone directories (e.g., manual deletion) are recovered.
        """
        import shutil

        pool = get_pool()
        missing_path = str(tmp_path / "gone-clone")
        pid = await _seed_pipeline(pool, clone_path=missing_path)
        # _seed_pipeline creates the directory; remove it to simulate manual deletion
        shutil.rmtree(missing_path)

        new_clone = tmp_path / "recloned"
        mock_cm = MagicMock(spec=CloneManager)
        mock_cm.create_clone = AsyncMock(return_value=CloneResult(
            clone_path=new_clone, review_base_rev="def67890abc", workspace_ref=None,
        ))
        orch = PipelineOrchestrator(pool, LogBuffer(), clone_manager=mock_cm)

        await orch._ensure_clone(pid)

        mock_cm.create_clone.assert_awaited_once()

    async def test_raises_when_pipeline_not_found(self, initialized_db):
        """_ensure_clone raises ValueError for non-existent pipeline_id.

        Invariant: Invalid pipeline IDs produce clear errors, not silent no-ops.
        """
        pool = get_pool()
        orch = PipelineOrchestrator(pool, LogBuffer())

        with pytest.raises(ValueError, match="not found"):
            await orch._ensure_clone(99999)

    async def test_clone_manager_injectable(self, initialized_db):
        """PipelineOrchestrator accepts a custom CloneManager via constructor.

        Invariant: Dependency injection for testing and configuration.
        """
        pool = get_pool()
        mock_cm = MagicMock(spec=CloneManager)
        orch = PipelineOrchestrator(pool, LogBuffer(), clone_manager=mock_cm)

        assert orch._clone_manager is mock_cm

    async def test_default_clone_manager_created(self, initialized_db):
        """PipelineOrchestrator creates a default CloneManager when none provided.

        Invariant: Production usage doesn't require explicit CloneManager.
        """
        pool = get_pool()
        orch = PipelineOrchestrator(pool, LogBuffer())

        assert isinstance(orch._clone_manager, CloneManager)

    async def test_run_pipeline_calls_ensure_clone_before_lease(
        self, initialized_db, tmp_path,
    ):
        """_run_pipeline calls _ensure_clone before acquiring the lease.

        Invariant: Spec lifecycle order is clone -> lease -> stages.
        """
        pool = get_pool()
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()
        pid = await _seed_pipeline(pool, clone_path=str(clone_dir))
        log_buffer = LogBuffer()
        orch = PipelineOrchestrator(pool, log_buffer)

        call_order: list[str] = []
        original_ensure = orch._ensure_clone
        original_acquire = orch._lease_manager.acquire_pipeline_lease

        async def tracked_ensure(pipeline_id: int) -> None:
            call_order.append("ensure_clone")
            await original_ensure(pipeline_id)

        async def tracked_acquire(pipeline_id: int) -> str:
            call_order.append("acquire_lease")
            return await original_acquire(pipeline_id)

        orch._ensure_clone = tracked_ensure  # type: ignore[assignment]
        orch._lease_manager.acquire_pipeline_lease = tracked_acquire  # type: ignore[assignment]

        cancel = asyncio.Event()
        await orch._run_pipeline(pid, cancel)

        assert call_order[0] == "ensure_clone"
        assert call_order[1] == "acquire_lease"

    async def test_clone_created_for_fresh_pipeline_end_to_end(
        self, initialized_db, tmp_path,
    ):
        """Full pipeline lifecycle: fresh pipeline (clone_path='') gets cloned.

        Invariant: The orchestrator creates a clone before running any stages.
        """
        pool = get_pool()
        pid = await _seed_pipeline(pool, clone_path="")

        clone_dir = tmp_path / "e2e-clone"
        clone_dir.mkdir(parents=True, exist_ok=True)

        async def fake_create_clone(
            pipeline_id: int, repo_id: int, **kwargs: object,
        ) -> CloneResult:
            """Simulate real create_clone: update DB and return result."""
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE pipelines SET clone_path = %s, review_base_rev = 'abc12345' "
                    "WHERE id = %s",
                    (str(clone_dir), pipeline_id),
                )
                await conn.commit()
            return CloneResult(
                clone_path=clone_dir, review_base_rev="abc12345", workspace_ref=None,
            )

        mock_cm = MagicMock(spec=CloneManager)
        mock_cm.create_clone = AsyncMock(side_effect=fake_create_clone)

        log_buffer = LogBuffer()
        orch = PipelineOrchestrator(pool, log_buffer, clone_manager=mock_cm)

        cancel = asyncio.Event()
        await orch._run_pipeline(pid, cancel)

        mock_cm.create_clone.assert_awaited_once_with(pid, 1)  # repo_id=1

        # Pipeline should have completed (no adapters = skipped stages)
        async with pool.connection() as conn:
            row: dict = await (  # type: ignore[assignment]
                await conn.execute("SELECT status FROM pipelines WHERE id = %s", (pid,))
            ).fetchone()
        assert row["status"] == "completed"

    @settings(
        max_examples=5,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(data=st.data())
    @pytest.mark.asyncio
    async def test_pbt_ensure_clone_idempotent_when_exists(
        self, initialized_db, data, tmp_path,
    ) -> None:
        """Property: calling _ensure_clone multiple times on an existing clone is a no-op.

        Invariant: Idempotency — repeated calls with existing directory don't trigger cloning.
        """
        pool = get_pool()
        clone_dir = tmp_path / f"pbt-{uuid.uuid4().hex[:6]}"
        clone_dir.mkdir()
        suffix = uuid.uuid4().hex[:8]
        async with pool.connection() as conn:
            repo_row = await (
                await conn.execute(
                    "INSERT INTO repos (name, local_path) VALUES (%s, '/tmp/r') RETURNING id",
                    (f"repo-ec-{suffix}",),
                )
            ).fetchone()
            pdef_row = await (
                await conn.execute(
                    "INSERT INTO pipeline_defs (name, stage_graph_json) "
                    "VALUES (%s, %s) RETURNING id",
                    (f"def-ec-{suffix}", FULL_GRAPH_JSON),
                )
            ).fetchone()
            pipe_row = await (
                await conn.execute(
                    "INSERT INTO pipelines (pipeline_def_id, repo_id, clone_path, "
                    "review_base_rev, status) VALUES (%s, %s, %s, 'abc', 'pending') "
                    "RETURNING id",
                    (pdef_row["id"], repo_row["id"], str(clone_dir)),
                )
            ).fetchone()
            await conn.commit()
            pid = pipe_row["id"]

        n_calls = data.draw(st.integers(min_value=1, max_value=5))
        mock_cm = MagicMock(spec=CloneManager)
        mock_cm.create_clone = AsyncMock()
        orch = PipelineOrchestrator(pool, LogBuffer(), clone_manager=mock_cm)

        for _ in range(n_calls):
            await orch._ensure_clone(pid)

        mock_cm.create_clone.assert_not_called()


# ---------------------------------------------------------------------------
# Kill invariant property tests
# ---------------------------------------------------------------------------


class TestKillInvariants:
    """Property-based invariants for ``kill_pipeline``.

    Spec line 540: kill ``releases claims``. Combined with
    ``UniqueTaskClaim`` (at most one live lease per primitive task), this
    means: after ``kill_pipeline``, no HTN task for that pipeline may
    remain ``in_progress``. This is true regardless of how many tasks were
    in flight or which non-terminal status the pipeline started in.
    """

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        n_in_progress=st.integers(min_value=0, max_value=5),
        starting_status=st.sampled_from(
            ["running", "cancel_requested", "paused", "needs_attention"]
        ),
    )
    @pytest.mark.asyncio
    async def test_kill_releases_all_in_progress_claims(
        self, initialized_db, n_in_progress, starting_status
    ) -> None:
        """For any non-terminal pipeline with N in-progress claims, kill
        leaves zero ``in_progress`` HTN tasks.
        """
        pool = get_pool()
        # Inline seed with uuid-suffixed unique names so Hypothesis can re-run
        # without violating the repos.name / pipeline_defs.name UNIQUE
        # constraints — _seed_pipeline uses a hardcoded name.
        suffix = uuid.uuid4().hex[:8]
        clone_dir = Path(f"/tmp/test-clone-kill-prop-{suffix}")
        clone_dir.mkdir(parents=True, exist_ok=True)
        async with pool.connection() as conn:
            repo_row = await (
                await conn.execute(
                    "INSERT INTO repos (name, local_path) VALUES (%s, '/tmp/r') RETURNING id",
                    (f"repo-kp-{suffix}",),
                )
            ).fetchone()
            pdef_row = await (
                await conn.execute(
                    "INSERT INTO pipeline_defs (name, stage_graph_json) "
                    "VALUES (%s, %s) RETURNING id",
                    (f"def-kp-{suffix}", FULL_GRAPH_JSON),
                )
            ).fetchone()
            pipe_row = await (
                await conn.execute(
                    "INSERT INTO pipelines (pipeline_def_id, repo_id, clone_path, "
                    "review_base_rev, status) VALUES (%s, %s, %s, 'abc123', %s) "
                    "RETURNING id",
                    (pdef_row["id"], repo_row["id"], str(clone_dir), starting_status),
                )
            ).fetchone()
            pid = pipe_row["id"]

            for i in range(n_in_progress):
                await conn.execute(
                    "INSERT INTO htn_tasks "
                    "(pipeline_id, name, description, task_type, status, priority, "
                    " ordering, claim_token, claim_owner_token) "
                    "VALUES (%s, %s, %s, 'primitive', 'in_progress', %s, %s, %s, %s)",
                    (
                        pid,
                        f"task-{i}",
                        "desc",
                        i,
                        i,
                        f"claim-{i}-{suffix}",
                        "owner-tok",
                    ),
                )
            await conn.commit()

        orch = PipelineOrchestrator(pool, LogBuffer())
        await orch.kill_pipeline(pid)

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT COUNT(*) AS c FROM htn_tasks "
                    "WHERE pipeline_id = %s AND status = 'in_progress'",
                    (pid,),
                )
            ).fetchone()
            pipe_row = await (
                await conn.execute(
                    "SELECT status FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()

        assert row["c"] == 0
        assert pipe_row["status"] == "killed"
