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

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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
