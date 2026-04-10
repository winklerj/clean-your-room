"""Tests for PipelineOrchestrator — leases, reconciliation, stage dispatch."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from build_your_room.db import get_pool
from build_your_room.orchestrator import PipelineOrchestrator
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

        import build_your_room.orchestrator as orch_mod

        original_pipelines_dir = orch_mod.PIPELINES_DIR
        orch_mod.PIPELINES_DIR = tmp_path / "pipelines"
        try:
            orch = PipelineOrchestrator(pool, LogBuffer())
            await orch.reconcile_running_state()
        finally:
            orch_mod.PIPELINES_DIR = original_pipelines_dir

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
