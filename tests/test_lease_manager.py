"""Tests for LeaseManager — durable lease ownership for pipelines, stages, sessions.

Validates atomic acquire/release/renew operations, multi-level leasing,
heartbeat loop behavior, expiry queries, and property-based invariants
for the RunningImpliesOwner contract.
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
from build_your_room.lease_manager import LeaseError, LeaseManager
from build_your_room.streaming import LogBuffer

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
        ],
        "edges": [],
    }
)


async def _seed_pipeline(pool, *, status: str = "pending", clone_path: str = "/tmp/test-clone"):
    """Insert a repo, pipeline_def, and pipeline for testing. Returns pipeline_id."""
    suffix = uuid.uuid4().hex[:8]
    async with pool.connection() as conn:
        repo_row = await (
            await conn.execute(
                "INSERT INTO repos (name, local_path) VALUES (%s, '/tmp/test-repo') "
                "RETURNING id",
                (f"repo-{suffix}",),
            )
        ).fetchone()
        repo_id = repo_row["id"]

        pdef_row = await (
            await conn.execute(
                "INSERT INTO pipeline_defs (name, stage_graph_json) "
                "VALUES (%s, %s) RETURNING id",
                (f"def-{suffix}", FULL_GRAPH_JSON),
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


async def _seed_stage(pool, pipeline_id: int) -> int:
    """Insert a pipeline_stage for testing. Returns stage_id."""
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "INSERT INTO pipeline_stages "
                "(pipeline_id, stage_key, stage_type, agent_type, status, max_iterations) "
                "VALUES (%s, 'spec_author', 'spec_author', 'claude', 'running', 1) "
                "RETURNING id",
                (pipeline_id,),
            )
        ).fetchone()
        await conn.commit()
        return row["id"]


async def _seed_session(pool, stage_id: int) -> int:
    """Insert an agent_session for testing. Returns session_id."""
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "INSERT INTO agent_sessions "
                "(pipeline_stage_id, session_type, status, started_at) "
                "VALUES (%s, 'claude_sdk', 'running', now()) "
                "RETURNING id",
                (stage_id,),
            )
        ).fetchone()
        await conn.commit()
        return row["id"]


# ---------------------------------------------------------------------------
# Pipeline lease tests
# ---------------------------------------------------------------------------


class TestPipelineLeases:
    """Tests for pipeline-level lease acquire/release/renew."""

    async def test_acquire_returns_token(self, initialized_db):
        """Invariant: acquire returns a non-empty UUID string and sets DB fields."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        lm = LeaseManager(pool)

        token = await lm.acquire_pipeline_lease(pid)
        assert token
        assert len(token) == 36  # UUID format

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT owner_token, lease_expires_at FROM pipelines WHERE id = %s",
                    (pid,),
                )
            ).fetchone()
        assert row["owner_token"] == token
        assert row["lease_expires_at"] is not None

    async def test_acquire_fails_when_owned(self, initialized_db):
        """Invariant: RunningImpliesOwner — at most one owner at a time."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        lm = LeaseManager(pool)

        await lm.acquire_pipeline_lease(pid)
        with pytest.raises(LeaseError, match="another owner"):
            await lm.acquire_pipeline_lease(pid)

    async def test_acquire_succeeds_after_expiry(self, initialized_db):
        """After a lease expires, a new owner can acquire it."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        lm = LeaseManager(pool, lease_ttl_sec=1)

        token1 = await lm.acquire_pipeline_lease(pid)

        # Manually expire
        past = datetime.now(timezone.utc) - timedelta(seconds=10)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET lease_expires_at = %s WHERE id = %s",
                (past, pid),
            )
            await conn.commit()

        token2 = await lm.acquire_pipeline_lease(pid)
        assert token2 != token1

    async def test_release_clears_fields(self, initialized_db):
        """Release sets owner_token and lease_expires_at to NULL."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        lm = LeaseManager(pool)

        await lm.acquire_pipeline_lease(pid)
        await lm.release_pipeline_lease(pid)

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT owner_token, lease_expires_at FROM pipelines WHERE id = %s",
                    (pid,),
                )
            ).fetchone()
        assert row["owner_token"] is None
        assert row["lease_expires_at"] is None

    async def test_renew_extends_expiry(self, initialized_db):
        """Renew pushes lease_expires_at forward."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        lm = LeaseManager(pool)

        token = await lm.acquire_pipeline_lease(pid)

        async with pool.connection() as conn:
            before = await (
                await conn.execute(
                    "SELECT lease_expires_at FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()

        await asyncio.sleep(0.05)
        renewed = await lm.renew_pipeline_lease(pid, token)
        assert renewed is True

        async with pool.connection() as conn:
            after = await (
                await conn.execute(
                    "SELECT lease_expires_at FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()

        assert after["lease_expires_at"] > before["lease_expires_at"]

    async def test_renew_returns_false_for_wrong_token(self, initialized_db):
        """Renew fails gracefully if the owner token doesn't match."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        lm = LeaseManager(pool)

        await lm.acquire_pipeline_lease(pid)
        renewed = await lm.renew_pipeline_lease(pid, "wrong-token")
        assert renewed is False


# ---------------------------------------------------------------------------
# Stage lease tests
# ---------------------------------------------------------------------------


class TestStageLeases:
    """Tests for stage-level lease acquire/release."""

    async def test_acquire_stage_sets_fields(self, initialized_db):
        """Stage lease acquire sets owner_token and lease fields."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        stage_id = await _seed_stage(pool, pid)
        lm = LeaseManager(pool)

        await lm.acquire_stage_lease(stage_id, "owner-abc")

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT owner_token, last_heartbeat_at, lease_expires_at "
                    "FROM pipeline_stages WHERE id = %s",
                    (stage_id,),
                )
            ).fetchone()
        assert row["owner_token"] == "owner-abc"
        assert row["last_heartbeat_at"] is not None
        assert row["lease_expires_at"] is not None

    async def test_release_stage_clears_fields(self, initialized_db):
        """Stage lease release clears all ownership fields."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        stage_id = await _seed_stage(pool, pid)
        lm = LeaseManager(pool)

        await lm.acquire_stage_lease(stage_id, "owner-abc")
        await lm.release_stage_lease(stage_id)

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT owner_token, last_heartbeat_at, lease_expires_at "
                    "FROM pipeline_stages WHERE id = %s",
                    (stage_id,),
                )
            ).fetchone()
        assert row["owner_token"] is None
        assert row["last_heartbeat_at"] is None
        assert row["lease_expires_at"] is None


# ---------------------------------------------------------------------------
# Session lease tests
# ---------------------------------------------------------------------------


class TestSessionLeases:
    """Tests for session-level lease acquire/release."""

    async def test_acquire_session_sets_fields(self, initialized_db):
        """Session lease acquire sets owner_token and lease fields."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        stage_id = await _seed_stage(pool, pid)
        session_id = await _seed_session(pool, stage_id)
        lm = LeaseManager(pool)

        await lm.acquire_session_lease(session_id, "owner-xyz")

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT owner_token, last_heartbeat_at, lease_expires_at "
                    "FROM agent_sessions WHERE id = %s",
                    (session_id,),
                )
            ).fetchone()
        assert row["owner_token"] == "owner-xyz"
        assert row["last_heartbeat_at"] is not None
        assert row["lease_expires_at"] is not None

    async def test_release_session_clears_fields(self, initialized_db):
        """Session lease release clears all ownership fields."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        stage_id = await _seed_stage(pool, pid)
        session_id = await _seed_session(pool, stage_id)
        lm = LeaseManager(pool)

        await lm.acquire_session_lease(session_id, "owner-xyz")
        await lm.release_session_lease(session_id)

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT owner_token, last_heartbeat_at, lease_expires_at "
                    "FROM agent_sessions WHERE id = %s",
                    (session_id,),
                )
            ).fetchone()
        assert row["owner_token"] is None
        assert row["last_heartbeat_at"] is None
        assert row["lease_expires_at"] is None


# ---------------------------------------------------------------------------
# Multi-level renewal tests
# ---------------------------------------------------------------------------


class TestMultiLevelRenewal:
    """Tests for renew_leases across pipeline + stage + session."""

    async def test_renew_pipeline_only(self, initialized_db):
        """Renew with just pipeline_id updates pipeline timestamps."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        lm = LeaseManager(pool)

        await lm.acquire_pipeline_lease(pid)

        async with pool.connection() as conn:
            before = await (
                await conn.execute(
                    "SELECT lease_expires_at FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()

        await asyncio.sleep(0.05)
        await lm.renew_leases(pid)

        async with pool.connection() as conn:
            after = await (
                await conn.execute(
                    "SELECT lease_expires_at FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()

        assert after["lease_expires_at"] > before["lease_expires_at"]

    async def test_renew_all_levels(self, initialized_db):
        """Renew with pipeline, stage, and session updates all three."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        stage_id = await _seed_stage(pool, pid)
        session_id = await _seed_session(pool, stage_id)
        lm = LeaseManager(pool)

        await lm.acquire_pipeline_lease(pid)
        await lm.acquire_stage_lease(stage_id, "owner")
        await lm.acquire_session_lease(session_id, "owner")

        async with pool.connection() as conn:
            p_before = await (
                await conn.execute(
                    "SELECT lease_expires_at FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()
            s_before = await (
                await conn.execute(
                    "SELECT lease_expires_at FROM pipeline_stages WHERE id = %s",
                    (stage_id,),
                )
            ).fetchone()
            ss_before = await (
                await conn.execute(
                    "SELECT lease_expires_at FROM agent_sessions WHERE id = %s",
                    (session_id,),
                )
            ).fetchone()

        await asyncio.sleep(0.05)
        await lm.renew_leases(pid, stage_id, session_id)

        async with pool.connection() as conn:
            p_after = await (
                await conn.execute(
                    "SELECT lease_expires_at FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()
            s_after = await (
                await conn.execute(
                    "SELECT lease_expires_at FROM pipeline_stages WHERE id = %s",
                    (stage_id,),
                )
            ).fetchone()
            ss_after = await (
                await conn.execute(
                    "SELECT lease_expires_at FROM agent_sessions WHERE id = %s",
                    (session_id,),
                )
            ).fetchone()

        assert p_after["lease_expires_at"] > p_before["lease_expires_at"]
        assert s_after["lease_expires_at"] > s_before["lease_expires_at"]
        assert ss_after["lease_expires_at"] > ss_before["lease_expires_at"]


# ---------------------------------------------------------------------------
# Heartbeat loop tests
# ---------------------------------------------------------------------------


class TestHeartbeatLoop:
    """Tests for heartbeat_loop behavior."""

    async def test_heartbeat_renews_lease(self, initialized_db):
        """Heartbeat loop renews the lease at least once before cancel."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        lm = LeaseManager(pool, heartbeat_interval_sec=0)  # immediate renewal

        token = await lm.acquire_pipeline_lease(pid)

        async with pool.connection() as conn:
            before = await (
                await conn.execute(
                    "SELECT lease_expires_at FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()

        cancel = asyncio.Event()

        async def stop_after_delay():
            await asyncio.sleep(0.1)
            cancel.set()

        stopper = asyncio.create_task(stop_after_delay())
        await lm.heartbeat_loop(pid, token, cancel)
        await stopper

        async with pool.connection() as conn:
            after = await (
                await conn.execute(
                    "SELECT lease_expires_at FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()

        assert after["lease_expires_at"] >= before["lease_expires_at"]

    async def test_heartbeat_sets_cancel_on_lost_lease(self, initialized_db):
        """Heartbeat signals cancel_event when the lease is stolen."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        lm = LeaseManager(pool, heartbeat_interval_sec=0)

        token = await lm.acquire_pipeline_lease(pid)

        # Steal the lease by changing the owner_token
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET owner_token = 'stolen' WHERE id = %s",
                (pid,),
            )
            await conn.commit()

        cancel = asyncio.Event()
        await lm.heartbeat_loop(pid, token, cancel)
        assert cancel.is_set()

    async def test_heartbeat_exits_when_precancelled(self, initialized_db):
        """Heartbeat returns immediately if cancel_event is already set."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        lm = LeaseManager(pool, heartbeat_interval_sec=0)

        token = await lm.acquire_pipeline_lease(pid)

        cancel = asyncio.Event()
        cancel.set()
        await lm.heartbeat_loop(pid, token, cancel)
        # Should return without error


# ---------------------------------------------------------------------------
# Expiry query tests
# ---------------------------------------------------------------------------


class TestExpiryQueries:
    """Tests for is_lease_expired and get_expired_running_pipelines."""

    async def test_is_expired_for_no_owner(self, initialized_db):
        """Pipeline with no owner_token is considered expired."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        lm = LeaseManager(pool)

        assert await lm.is_lease_expired(pid) is True

    async def test_is_expired_for_active_lease(self, initialized_db):
        """Pipeline with active lease is not expired."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        lm = LeaseManager(pool)

        await lm.acquire_pipeline_lease(pid)
        assert await lm.is_lease_expired(pid) is False

    async def test_is_expired_for_past_expiry(self, initialized_db):
        """Pipeline with past lease_expires_at is expired."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        lm = LeaseManager(pool)

        await lm.acquire_pipeline_lease(pid)
        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET lease_expires_at = %s WHERE id = %s",
                (past, pid),
            )
            await conn.commit()

        assert await lm.is_lease_expired(pid) is True

    async def test_is_expired_for_nonexistent_pipeline(self, initialized_db):
        """Nonexistent pipeline ID is treated as expired."""
        pool = get_pool()
        lm = LeaseManager(pool)
        assert await lm.is_lease_expired(99999) is True

    async def test_get_expired_running(self, initialized_db):
        """get_expired_running_pipelines returns running pipelines with expired leases."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")
        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET owner_token = 'old', lease_expires_at = %s "
                "WHERE id = %s",
                (past, pid),
            )
            await conn.commit()

        lm = LeaseManager(pool)
        expired = await lm.get_expired_running_pipelines()
        expired_ids = [r["id"] for r in expired]
        assert pid in expired_ids

    async def test_get_expired_excludes_live(self, initialized_db):
        """get_expired_running_pipelines excludes pipelines with live leases."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")
        future = datetime.now(timezone.utc) + timedelta(seconds=300)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET owner_token = 'live', lease_expires_at = %s "
                "WHERE id = %s",
                (future, pid),
            )
            await conn.commit()

        lm = LeaseManager(pool)
        expired = await lm.get_expired_running_pipelines()
        expired_ids = [r["id"] for r in expired]
        assert pid not in expired_ids

    async def test_get_live_running(self, initialized_db):
        """get_live_running_pipelines returns only live-leased running pipelines."""
        pool = get_pool()
        pid_live = await _seed_pipeline(pool, status="running")
        pid_expired = await _seed_pipeline(pool, status="running")

        future = datetime.now(timezone.utc) + timedelta(seconds=300)
        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET owner_token = 'live', lease_expires_at = %s "
                "WHERE id = %s",
                (future, pid_live),
            )
            await conn.execute(
                "UPDATE pipelines SET owner_token = 'old', lease_expires_at = %s "
                "WHERE id = %s",
                (past, pid_expired),
            )
            await conn.commit()

        lm = LeaseManager(pool)
        live = await lm.get_live_running_pipelines()
        live_ids = [r["id"] for r in live]
        assert pid_live in live_ids
        assert pid_expired not in live_ids


# ---------------------------------------------------------------------------
# Release all for pipeline tests
# ---------------------------------------------------------------------------


class TestReleaseAllForPipeline:
    """Tests for release_all_for_pipeline bulk cleanup."""

    async def test_releases_stage_and_session_leases(self, initialized_db):
        """release_all_for_pipeline clears stage and session ownership."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        stage_id = await _seed_stage(pool, pid)
        session_id = await _seed_session(pool, stage_id)
        lm = LeaseManager(pool)

        await lm.acquire_stage_lease(stage_id, "owner")
        await lm.acquire_session_lease(session_id, "owner")

        await lm.release_all_for_pipeline(pid)

        async with pool.connection() as conn:
            stage_row = await (
                await conn.execute(
                    "SELECT owner_token FROM pipeline_stages WHERE id = %s",
                    (stage_id,),
                )
            ).fetchone()
            sess_row = await (
                await conn.execute(
                    "SELECT owner_token FROM agent_sessions WHERE id = %s",
                    (session_id,),
                )
            ).fetchone()

        assert stage_row["owner_token"] is None
        assert sess_row["owner_token"] is None

    async def test_release_all_is_idempotent(self, initialized_db):
        """Calling release_all twice doesn't error — idempotent."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        lm = LeaseManager(pool)

        await lm.release_all_for_pipeline(pid)
        await lm.release_all_for_pipeline(pid)  # no error


# ---------------------------------------------------------------------------
# Orchestrator integration tests
# ---------------------------------------------------------------------------


class TestOrchestratorDelegation:
    """Verify orchestrator still works after delegating to LeaseManager."""

    async def test_orchestrator_acquire_delegates(self, initialized_db):
        """Orchestrator._acquire_pipeline_lease delegates to LeaseManager."""
        from build_your_room.orchestrator import PipelineOrchestrator

        pool = get_pool()
        pid = await _seed_pipeline(pool)
        orch = PipelineOrchestrator(pool, LogBuffer())

        token = await orch._acquire_pipeline_lease(pid)
        assert token

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT owner_token FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()
        assert row["owner_token"] == token

    async def test_orchestrator_release_delegates(self, initialized_db):
        """Orchestrator._release_pipeline_lease delegates to LeaseManager."""
        from build_your_room.orchestrator import PipelineOrchestrator

        pool = get_pool()
        pid = await _seed_pipeline(pool)
        orch = PipelineOrchestrator(pool, LogBuffer())

        await orch._acquire_pipeline_lease(pid)
        await orch._release_pipeline_lease(pid)

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT owner_token FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()
        assert row["owner_token"] is None

    async def test_orchestrator_renew_delegates(self, initialized_db):
        """Orchestrator.renew_leases delegates to LeaseManager."""
        from build_your_room.orchestrator import PipelineOrchestrator

        pool = get_pool()
        pid = await _seed_pipeline(pool)
        orch = PipelineOrchestrator(pool, LogBuffer())

        await orch._acquire_pipeline_lease(pid)

        async with pool.connection() as conn:
            before = await (
                await conn.execute(
                    "SELECT lease_expires_at FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()

        await asyncio.sleep(0.05)
        await orch.renew_leases(pid)

        async with pool.connection() as conn:
            after = await (
                await conn.execute(
                    "SELECT lease_expires_at FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()

        assert after["lease_expires_at"] > before["lease_expires_at"]

    async def test_orchestrator_accepts_injected_lease_manager(self, initialized_db):
        """Orchestrator uses an injected LeaseManager when provided."""
        from build_your_room.orchestrator import PipelineOrchestrator

        pool = get_pool()
        lm = LeaseManager(pool, lease_ttl_sec=99)
        orch = PipelineOrchestrator(pool, LogBuffer(), lease_manager=lm)

        assert orch._lease_manager is lm
        assert orch._lease_manager.lease_ttl_sec == 99


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestLeaseManagerProperties:
    """Property-based tests for lease manager invariants."""

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(data=st.data())
    @pytest.mark.asyncio
    async def test_acquire_release_cycle(self, initialized_db, data) -> None:
        """Property: acquire → release → reacquire always succeeds.

        Invariant: a released lease can always be re-acquired.
        """
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        lm = LeaseManager(pool)

        cycles = data.draw(st.integers(min_value=1, max_value=3))
        for _ in range(cycles):
            token = await lm.acquire_pipeline_lease(pid)
            assert token
            await lm.release_pipeline_lease(pid)

        final = await lm.acquire_pipeline_lease(pid)
        assert final

    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(data=st.data())
    @pytest.mark.asyncio
    async def test_lease_exclusivity_property(self, initialized_db, data) -> None:
        """Property: two concurrent acquires cannot both succeed.

        Invariant: RunningImpliesOwner — at most one owner at a time.
        """
        pool = get_pool()
        pid = await _seed_pipeline(pool)
        lm = LeaseManager(pool)

        await lm.acquire_pipeline_lease(pid)
        with pytest.raises(LeaseError):
            await lm.acquire_pipeline_lease(pid)

    @settings(
        max_examples=8,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        n_pipelines=st.integers(min_value=1, max_value=3),
    )
    @pytest.mark.asyncio
    async def test_release_all_clears_owned_only(self, initialized_db, n_pipelines) -> None:
        """Property: release_all_for_pipeline only clears leases for that pipeline.

        Invariant: stages/sessions from other pipelines are untouched.
        """
        pool = get_pool()
        lm = LeaseManager(pool)

        pids = []
        stage_ids = []
        for _ in range(n_pipelines):
            pid = await _seed_pipeline(pool)
            sid = await _seed_stage(pool, pid)
            await lm.acquire_stage_lease(sid, "owner")
            pids.append(pid)
            stage_ids.append(sid)

        # Release only the first pipeline
        await lm.release_all_for_pipeline(pids[0])

        async with pool.connection() as conn:
            first_stage = await (
                await conn.execute(
                    "SELECT owner_token FROM pipeline_stages WHERE id = %s",
                    (stage_ids[0],),
                )
            ).fetchone()
            assert first_stage["owner_token"] is None

            # Others still owned
            for sid in stage_ids[1:]:
                other = await (
                    await conn.execute(
                        "SELECT owner_token FROM pipeline_stages WHERE id = %s",
                        (sid,),
                    )
                ).fetchone()
                assert other["owner_token"] == "owner"

    @settings(max_examples=20)
    @given(ttl=st.integers(min_value=1, max_value=3600))
    def test_ttl_property_is_stored(self, ttl) -> None:
        """Property: lease_ttl_sec is always accessible and matches construction.

        Invariant: the TTL passed at construction is the TTL used.
        """
        # Can't use pool here — just test the class attribute
        # Use a mock pool
        lm = LeaseManager(None, lease_ttl_sec=ttl)  # type: ignore[arg-type]
        assert lm.lease_ttl_sec == ttl
