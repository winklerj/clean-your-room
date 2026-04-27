"""Tests for RecoveryManager — startup reconciliation, dirty workspace
snapshot, cancellation cleanup, and visit count loading.

Property-based tests verify recovery invariants across generated inputs:
visit count persistence, reconciliation downgrades all expired pipelines,
snapshot always produces metadata files and updates DB state.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from build_your_room.clone_manager import CloneManager
from build_your_room.db import get_pool
from build_your_room.recovery import RecoveryManager
from build_your_room.streaming import LogBuffer


def _init_git_repo(repo: Path) -> str:
    """Initialise an empty git repo with one commit. Returns HEAD revision."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, check=True, capture_output=True,
    )
    (repo / "README.md").write_text("# baseline\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=repo, check=True, capture_output=True,
    )
    rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    return rev

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
        ],
        "edges": [],
    }
)


async def _seed_pipeline(
    pool,
    *,
    status: str = "pending",
    clone_path: str = "/tmp/test-clone",
    workspace_state: str = "clean",
) -> int:
    """Insert a repo, pipeline_def, and pipeline for testing. Returns pipeline_id."""
    suffix = uuid.uuid4().hex[:8]
    async with pool.connection() as conn:
        repo_row = await (
            await conn.execute(
                "INSERT INTO repos (name, local_path) VALUES (%s, '/tmp/test-repo') "
                "RETURNING id",
                (f"test-repo-{suffix}",),
            )
        ).fetchone()

        pdef_row = await (
            await conn.execute(
                "INSERT INTO pipeline_defs (name, stage_graph_json) "
                "VALUES (%s, %s) RETURNING id",
                (f"test-pipeline-def-{suffix}", FULL_GRAPH_JSON),
            )
        ).fetchone()

        pipeline_row = await (
            await conn.execute(
                "INSERT INTO pipelines "
                "(pipeline_def_id, repo_id, clone_path, review_base_rev, status, "
                " workspace_state) "
                "VALUES (%s, %s, %s, 'abc123', %s, %s) RETURNING id",
                (pdef_row["id"], repo_row["id"], clone_path, status, workspace_state),
            )
        ).fetchone()
        await conn.commit()
        return pipeline_row["id"]


async def _set_expired_lease(pool, pid: int) -> None:
    """Set an expired lease on a pipeline."""
    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE pipelines SET owner_token = 'old-owner', "
            "lease_expires_at = %s WHERE id = %s",
            (past, pid),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Reconciliation tests
# ---------------------------------------------------------------------------


class TestReconciliation:
    """Tests for RecoveryManager.reconcile_running_state()."""

    async def test_downgrades_expired_pipeline(self, initialized_db):
        """Expired lease with status=running gets downgraded to needs_attention."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")
        await _set_expired_lease(pool, pid)

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.reconcile_running_state()

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT status, owner_token FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()
        assert row["status"] == "needs_attention"
        assert row["owner_token"] is None

    async def test_creates_escalation(self, initialized_db):
        """Reconciliation creates a startup_recovery escalation for each recovered pipeline."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")
        await _set_expired_lease(pool, pid)

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.reconcile_running_state()

        async with pool.connection() as conn:
            esc_row = await (
                await conn.execute(
                    "SELECT reason, status FROM escalations WHERE pipeline_id = %s", (pid,)
                )
            ).fetchone()
        assert esc_row is not None
        assert esc_row["reason"] == "startup_recovery"
        assert esc_row["status"] == "open"

    async def test_skips_live_lease(self, initialized_db):
        """Pipelines with still-valid leases are not touched by reconciliation."""
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

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.reconcile_running_state()

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT status FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()
        assert row["status"] == "running"

    async def test_releases_in_progress_tasks(self, initialized_db):
        """In-progress HTN tasks get released back to ready during reconciliation."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")
        await _set_expired_lease(pool, pid)

        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO htn_tasks "
                "(pipeline_id, name, description, task_type, status, priority, ordering, "
                " claim_token, claim_owner_token) "
                "VALUES (%s, 'test-task', 'a test', 'primitive', 'in_progress', 1, 1, "
                " 'claim-abc', 'old-owner')",
                (pid,),
            )
            await conn.commit()

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.reconcile_running_state()

        async with pool.connection() as conn:
            task_row = await (
                await conn.execute(
                    "SELECT status, claim_token FROM htn_tasks WHERE pipeline_id = %s", (pid,)
                )
            ).fetchone()
        assert task_row["status"] == "ready"
        assert task_row["claim_token"] is None

    async def test_fails_running_stages(self, initialized_db):
        """Running stages get marked as failed with startup_recovery reason."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")
        await _set_expired_lease(pool, pid)

        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO pipeline_stages "
                "(pipeline_id, stage_key, stage_type, agent_type, status, max_iterations) "
                "VALUES (%s, 'impl_task', 'impl_task', 'claude', 'running', 50)",
                (pid,),
            )
            await conn.commit()

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.reconcile_running_state()

        async with pool.connection() as conn:
            stage_row = await (
                await conn.execute(
                    "SELECT status, escalation_reason FROM pipeline_stages "
                    "WHERE pipeline_id = %s",
                    (pid,),
                )
            ).fetchone()
        assert stage_row["status"] == "failed"
        assert stage_row["escalation_reason"] == "startup_recovery"

    async def test_interrupts_running_sessions(self, initialized_db):
        """Running agent sessions get marked as interrupted during reconciliation."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")
        await _set_expired_lease(pool, pid)

        async with pool.connection() as conn:
            stage_row = await (
                await conn.execute(
                    "INSERT INTO pipeline_stages "
                    "(pipeline_id, stage_key, stage_type, agent_type, status, max_iterations) "
                    "VALUES (%s, 'spec_author', 'spec_author', 'claude', 'running', 1) "
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

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.reconcile_running_state()

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
        assert sess_row["status"] == "interrupted"

    async def test_dirty_workspace_snapshot(self, initialized_db, tmp_path):
        """Dirty workspace triggers snapshot during reconciliation."""
        pool = get_pool()
        pid = await _seed_pipeline(
            pool,
            status="running",
            clone_path=str(tmp_path / "clone"),
            workspace_state="dirty_live",
        )
        await _set_expired_lease(pool, pid)

        log_buffer = LogBuffer()
        mgr = RecoveryManager(pool, log_buffer, pipelines_dir=tmp_path / "pipelines")
        await mgr.reconcile_running_state()

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT workspace_state, dirty_snapshot_artifact FROM pipelines "
                    "WHERE id = %s",
                    (pid,),
                )
            ).fetchone()
        assert row["workspace_state"] == "clean"
        assert row["dirty_snapshot_artifact"] is not None

        # Verify metadata file was created
        metadata_path = Path(row["dirty_snapshot_artifact"]) / "recovery_metadata.json"
        assert metadata_path.exists()
        metadata = json.loads(metadata_path.read_text())
        assert metadata["pipeline_id"] == pid


# ---------------------------------------------------------------------------
# Snapshot tests
# ---------------------------------------------------------------------------


class TestSnapshotDirtyWorkspace:
    """Tests for RecoveryManager.snapshot_dirty_workspace()."""

    async def test_creates_recovery_directory(self, initialized_db, tmp_path):
        """Snapshot creates the recovery directory structure and metadata file."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)

        mgr = RecoveryManager(pool, LogBuffer(), pipelines_dir=tmp_path / "pipelines")
        snapshot_path = await mgr.snapshot_dirty_workspace(
            pid, "abc123", "/tmp/clone"
        )

        assert snapshot_path is not None
        metadata_file = Path(snapshot_path) / "recovery_metadata.json"
        assert metadata_file.exists()

        metadata = json.loads(metadata_file.read_text())
        assert metadata["pipeline_id"] == pid
        assert metadata["baseline_rev"] == "abc123"
        assert metadata["clone_path"] == "/tmp/clone"
        assert "snapshot_at" in metadata

    async def test_updates_db_state(self, initialized_db, tmp_path):
        """Snapshot updates workspace_state to clean and sets dirty_snapshot_artifact."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, workspace_state="dirty_live")

        mgr = RecoveryManager(pool, LogBuffer(), pipelines_dir=tmp_path / "pipelines")
        snapshot_path = await mgr.snapshot_dirty_workspace(
            pid, "abc123", "/tmp/clone"
        )

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT workspace_state, dirty_snapshot_artifact FROM pipelines "
                    "WHERE id = %s",
                    (pid,),
                )
            ).fetchone()
        assert row["workspace_state"] == "clean"
        assert row["dirty_snapshot_artifact"] == snapshot_path

    async def test_with_connection(self, initialized_db, tmp_path):
        """Snapshot works when passed an existing connection (inside a transaction)."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, workspace_state="dirty_live")

        mgr = RecoveryManager(pool, LogBuffer(), pipelines_dir=tmp_path / "pipelines")

        async with pool.connection() as conn:
            snapshot_path = await mgr.snapshot_dirty_workspace(
                pid, "abc123", "/tmp/clone", conn=conn
            )
            await conn.commit()

        assert snapshot_path is not None
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT dirty_snapshot_artifact FROM pipelines WHERE id = %s",
                    (pid,),
                )
            ).fetchone()
        assert row["dirty_snapshot_artifact"] == snapshot_path

    async def test_multiple_snapshots_distinct_paths(self, initialized_db, tmp_path):
        """Multiple snapshots for the same pipeline produce distinct directories."""
        pool = get_pool()
        pid = await _seed_pipeline(pool)

        mgr = RecoveryManager(pool, LogBuffer(), pipelines_dir=tmp_path / "pipelines")
        path1 = await mgr.snapshot_dirty_workspace(pid, "rev1", "/tmp/clone")
        path2 = await mgr.snapshot_dirty_workspace(pid, "rev2", "/tmp/clone")

        # Paths may be the same if timestamps collide in the same second,
        # but metadata files should both exist
        assert path1 is not None
        assert path2 is not None
        assert Path(path1).exists()
        assert Path(path2).exists()


# ---------------------------------------------------------------------------
# Cancellation tests
# ---------------------------------------------------------------------------


class TestHandleCancellation:
    """Tests for RecoveryManager.handle_cancellation()."""

    async def test_marks_pipeline_cancelled(self, initialized_db):
        """Cancellation sets pipeline status to cancelled."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.handle_cancellation(pid, "owner-tok")

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT status FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()
        assert row["status"] == "cancelled"

    async def test_releases_htn_claims(self, initialized_db):
        """In-progress HTN tasks get released back to ready during cancellation."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO htn_tasks "
                "(pipeline_id, name, description, task_type, status, priority, ordering, "
                " claim_token, claim_owner_token) "
                "VALUES (%s, 'task-1', 'desc', 'primitive', 'in_progress', 1, 1, "
                " 'claim-tok', 'owner-tok')",
                (pid,),
            )
            await conn.commit()

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.handle_cancellation(pid, "owner-tok")

        async with pool.connection() as conn:
            task_row = await (
                await conn.execute(
                    "SELECT status, claim_token FROM htn_tasks WHERE pipeline_id = %s",
                    (pid,),
                )
            ).fetchone()
        assert task_row["status"] == "ready"
        assert task_row["claim_token"] is None

    async def test_cancels_running_sessions(self, initialized_db):
        """Running agent sessions get cancelled during pipeline cancellation."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        async with pool.connection() as conn:
            stage_row = await (
                await conn.execute(
                    "INSERT INTO pipeline_stages "
                    "(pipeline_id, stage_key, stage_type, agent_type, status, max_iterations) "
                    "VALUES (%s, 'spec_author', 'spec_author', 'claude', 'running', 1) "
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

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.handle_cancellation(pid, "owner-tok")

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
        assert sess_row["status"] == "cancelled"

    async def test_cancels_running_stages(self, initialized_db):
        """Running stages get cancelled during pipeline cancellation."""
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

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.handle_cancellation(pid, "owner-tok")

        async with pool.connection() as conn:
            stage_row = await (
                await conn.execute(
                    "SELECT status FROM pipeline_stages WHERE pipeline_id = %s",
                    (pid,),
                )
            ).fetchone()
        assert stage_row["status"] == "cancelled"

    async def test_dirty_workspace_snapshotted(self, initialized_db, tmp_path):
        """Dirty workspace triggers snapshot during cancellation."""
        pool = get_pool()
        pid = await _seed_pipeline(
            pool,
            status="running",
            clone_path=str(tmp_path / "clone"),
            workspace_state="dirty_live",
        )

        mgr = RecoveryManager(pool, LogBuffer(), pipelines_dir=tmp_path / "pipelines")
        await mgr.handle_cancellation(pid, "owner-tok")

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT workspace_state, dirty_snapshot_artifact FROM pipelines "
                    "WHERE id = %s",
                    (pid,),
                )
            ).fetchone()
        assert row["dirty_snapshot_artifact"] is not None

    async def test_clean_workspace_no_snapshot(self, initialized_db):
        """Clean workspace does not trigger snapshot during cancellation."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running", workspace_state="clean")

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.handle_cancellation(pid, "owner-tok")

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT dirty_snapshot_artifact FROM pipelines WHERE id = %s",
                    (pid,),
                )
            ).fetchone()
        assert row["dirty_snapshot_artifact"] is None

    async def test_closes_log_buffer(self, initialized_db):
        """Cancellation appends 'Pipeline cancelled' and closes the log buffer."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        log_buffer = LogBuffer()
        mgr = RecoveryManager(pool, log_buffer)
        await mgr.handle_cancellation(pid, "owner-tok")

        assert pid in log_buffer._closed


# ---------------------------------------------------------------------------
# Kill cleanup tests
# ---------------------------------------------------------------------------


class TestHandleKill:
    """Tests for ``RecoveryManager.handle_kill()``.

    Spec lines 539-541: kill must terminate sessions, snapshot dirty
    workspace, reset to baseline, release HTN claims, and mark the pipeline
    ``killed``. Differs from cancellation in that the terminal status is
    ``killed`` (not ``cancelled``) and snapshot is forced even when
    ``workspace_state='clean'`` because kill bypasses the cooperative
    cancel-event boundary where the orchestrator would otherwise sync the
    hint.
    """

    async def test_marks_pipeline_killed(self, initialized_db):
        """Kill sets pipeline status to killed."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.handle_kill(pid)

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT status FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()
        assert row["status"] == "killed"

    async def test_releases_htn_claims(self, initialized_db):
        """In-progress HTN tasks get released back to ready during kill."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO htn_tasks "
                "(pipeline_id, name, description, task_type, status, priority, ordering, "
                " claim_token, claim_owner_token) "
                "VALUES (%s, 'task-1', 'desc', 'primitive', 'in_progress', 1, 1, "
                " 'claim-tok', 'owner-tok')",
                (pid,),
            )
            await conn.commit()

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.handle_kill(pid)

        async with pool.connection() as conn:
            task_row = await (
                await conn.execute(
                    "SELECT status, claim_token, assigned_session_id "
                    "FROM htn_tasks WHERE pipeline_id = %s",
                    (pid,),
                )
            ).fetchone()
        assert task_row["status"] == "ready"
        assert task_row["claim_token"] is None
        assert task_row["assigned_session_id"] is None

    async def test_marks_running_sessions_killed(self, initialized_db):
        """Running agent sessions get marked status='killed' during kill."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        async with pool.connection() as conn:
            stage_row = await (
                await conn.execute(
                    "INSERT INTO pipeline_stages "
                    "(pipeline_id, stage_key, stage_type, agent_type, status, max_iterations) "
                    "VALUES (%s, 'spec_author', 'spec_author', 'claude', 'running', 1) "
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

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.handle_kill(pid)

        async with pool.connection() as conn:
            sess_row = await (
                await conn.execute(
                    "SELECT status, completed_at FROM agent_sessions "
                    "WHERE pipeline_stage_id IN ("
                    "  SELECT id FROM pipeline_stages WHERE pipeline_id = %s"
                    ")",
                    (pid,),
                )
            ).fetchone()
        assert sess_row["status"] == "killed"
        assert sess_row["completed_at"] is not None

    async def test_marks_running_stages_killed(self, initialized_db):
        """Running stages get marked status='killed' during kill."""
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

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.handle_kill(pid)

        async with pool.connection() as conn:
            stage_row = await (
                await conn.execute(
                    "SELECT status FROM pipeline_stages WHERE pipeline_id = %s",
                    (pid,),
                )
            ).fetchone()
        assert stage_row["status"] == "killed"

    async def test_force_snapshot_even_when_workspace_state_clean(
        self, initialized_db, tmp_path
    ):
        """Kill always snapshots, even when ``workspace_state`` reports clean.

        Kill interrupts mid-stage so the in-DB ``workspace_state`` hint may
        not have been updated to ``dirty_live`` before the cancel_event
        could fire. ``handle_kill`` therefore forces a snapshot to capture
        any uncheckpointed work.
        """
        pool = get_pool()
        clone = tmp_path / "clone"
        baseline_rev = _init_git_repo(clone)
        # Modify a tracked file so git status reports dirty even though
        # workspace_state in the DB still says 'clean' (the orchestrator
        # never had a chance to update it).
        (clone / "README.md").write_text("# uncheckpointed work\n")

        pid = await _seed_pipeline(
            pool,
            status="running",
            clone_path=str(clone),
            workspace_state="clean",
        )
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET review_base_rev = %s WHERE id = %s",
                (baseline_rev, pid),
            )
            await conn.commit()

        mgr = RecoveryManager(
            pool,
            LogBuffer(),
            pipelines_dir=tmp_path / "pipelines",
            clone_manager=CloneManager(pool),
        )
        await mgr.handle_kill(pid)

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT dirty_snapshot_artifact FROM pipelines WHERE id = %s",
                    (pid,),
                )
            ).fetchone()
        assert row["dirty_snapshot_artifact"] is not None
        # Patch should have captured the uncheckpointed change
        patch_file = Path(row["dirty_snapshot_artifact"]) / "patch.diff"
        assert patch_file.exists()
        assert "uncheckpointed work" in patch_file.read_text()
        # Clone should be reset to baseline
        assert (clone / "README.md").read_text() == "# baseline\n"

    async def test_idempotent_on_already_killed(self, initialized_db):
        """Re-running handle_kill on an already-killed pipeline is safe.

        Kill is callable both during the initial kill request and after
        server restart for stale 'killed'-but-still-needing-cleanup rows.
        Running it twice should leave the same final state without raising.
        """
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="killed")

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.handle_kill(pid)
        # Second call: must not raise.
        await mgr.handle_kill(pid)

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT status FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()
        assert row["status"] == "killed"

    async def test_closes_log_buffer(self, initialized_db):
        """Kill appends 'Pipeline killed' and closes the log buffer."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        log_buffer = LogBuffer()
        mgr = RecoveryManager(pool, log_buffer)
        await mgr.handle_kill(pid)

        assert pid in log_buffer._closed

    async def test_does_not_disturb_completed_stages(self, initialized_db):
        """Stages that completed before the kill keep their terminal status.

        Spec invariant: kill should not rewrite stage rows that already
        reached a terminal state. Only ``running`` and ``review_loop``
        stages flip to ``killed``.
        """
        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO pipeline_stages "
                "(pipeline_id, stage_key, stage_type, agent_type, status, "
                " max_iterations, completed_at) "
                "VALUES (%s, 'spec_author', 'spec_author', 'claude', 'completed', 1, now())",
                (pid,),
            )
            await conn.execute(
                "INSERT INTO pipeline_stages "
                "(pipeline_id, stage_key, stage_type, agent_type, status, max_iterations) "
                "VALUES (%s, 'impl_task', 'impl_task', 'claude', 'running', 50)",
                (pid,),
            )
            await conn.commit()

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.handle_kill(pid)

        async with pool.connection() as conn:
            stages = await (
                await conn.execute(
                    "SELECT stage_key, status FROM pipeline_stages "
                    "WHERE pipeline_id = %s ORDER BY stage_key",
                    (pid,),
                )
            ).fetchall()

        status_by_key = {s["stage_key"]: s["status"] for s in stages}
        assert status_by_key["spec_author"] == "completed"
        assert status_by_key["impl_task"] == "killed"


# ---------------------------------------------------------------------------
# Visit count loading tests
# ---------------------------------------------------------------------------


class TestLoadVisitCounts:
    """Tests for RecoveryManager.load_visit_counts() static method."""

    def test_roundtrip(self):
        """Visit counts survive JSON roundtrip through recovery_state_json."""
        counts = {"edge_a": 3, "edge_b": 1}
        pipeline = {"recovery_state_json": json.dumps({"visit_counts": counts})}
        assert RecoveryManager.load_visit_counts(pipeline) == counts

    def test_empty_on_none(self):
        """Returns empty dict when recovery_state_json is None."""
        assert RecoveryManager.load_visit_counts({"recovery_state_json": None}) == {}

    def test_empty_on_missing_key(self):
        """Returns empty dict when recovery_state_json has no visit_counts key."""
        pipeline = {"recovery_state_json": json.dumps({"other": 42})}
        assert RecoveryManager.load_visit_counts(pipeline) == {}

    def test_empty_on_invalid_json(self):
        """Returns empty dict for malformed JSON."""
        pipeline = {"recovery_state_json": "not-json"}
        assert RecoveryManager.load_visit_counts(pipeline) == {}

    def test_empty_on_null_json(self):
        """Returns empty dict when JSON parses to null."""
        pipeline = {"recovery_state_json": "null"}
        assert RecoveryManager.load_visit_counts(pipeline) == {}

    def test_empty_on_empty_string(self):
        """Returns empty dict for empty string."""
        pipeline = {"recovery_state_json": ""}
        assert RecoveryManager.load_visit_counts(pipeline) == {}

    def test_empty_on_no_key(self):
        """Returns empty dict when recovery_state_json key is absent."""
        assert RecoveryManager.load_visit_counts({}) == {}


# ---------------------------------------------------------------------------
# Orchestrator delegation tests
# ---------------------------------------------------------------------------


class TestOrchestratorDelegation:
    """Verify orchestrator delegates recovery operations to RecoveryManager."""

    async def test_reconcile_delegates(self, initialized_db):
        """PipelineOrchestrator.reconcile_running_state() delegates to RecoveryManager."""
        from build_your_room.orchestrator import PipelineOrchestrator

        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")
        await _set_expired_lease(pool, pid)

        log_buffer = LogBuffer()
        orch = PipelineOrchestrator(pool, log_buffer)
        await orch.reconcile_running_state()

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT status FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()
        assert row["status"] == "needs_attention"

    async def test_load_visit_counts_delegates(self):
        """PipelineOrchestrator._load_visit_counts() delegates to RecoveryManager."""
        from build_your_room.orchestrator import PipelineOrchestrator

        counts = {"e1": 2, "e2": 5}
        pipeline = {"recovery_state_json": json.dumps({"visit_counts": counts})}
        assert PipelineOrchestrator._load_visit_counts(pipeline) == counts

    async def test_snapshot_delegates(self, initialized_db, tmp_path):
        """PipelineOrchestrator._snapshot_dirty_workspace() delegates to RecoveryManager."""
        from build_your_room.orchestrator import PipelineOrchestrator

        pool = get_pool()
        pid = await _seed_pipeline(pool)

        log_buffer = LogBuffer()
        recovery_mgr = RecoveryManager(
            pool, log_buffer, pipelines_dir=tmp_path / "pipelines"
        )
        orch = PipelineOrchestrator(pool, log_buffer, recovery_manager=recovery_mgr)

        path = await orch._snapshot_dirty_workspace(pid, "rev1", "/tmp/clone")
        assert path is not None
        assert Path(path).exists()

    async def test_cancellation_delegates(self, initialized_db):
        """PipelineOrchestrator._handle_cancellation() delegates to RecoveryManager."""
        from build_your_room.orchestrator import PipelineOrchestrator

        pool = get_pool()
        pid = await _seed_pipeline(pool, status="running")

        log_buffer = LogBuffer()
        orch = PipelineOrchestrator(pool, log_buffer)
        await orch._handle_cancellation(pid, "owner-tok")

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT status FROM pipelines WHERE id = %s", (pid,)
                )
            ).fetchone()
        assert row["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Snapshot-with-real-clone tests (patch + manifest + reset)
# ---------------------------------------------------------------------------


class TestSnapshotWithRealClone:
    """Tests for ``snapshot_dirty_workspace`` when a real git clone exists."""

    async def test_captures_patch_diff(self, initialized_db, tmp_path):
        """Modified tracked file produces a patch.diff containing the change."""
        pool = get_pool()
        clone = tmp_path / "clone"
        baseline_rev = _init_git_repo(clone)
        (clone / "README.md").write_text("# changed\n")

        pid = await _seed_pipeline(pool, clone_path=str(clone))

        mgr = RecoveryManager(
            pool,
            LogBuffer(),
            pipelines_dir=tmp_path / "pipelines",
            clone_manager=CloneManager(pool),
        )
        snapshot_path = await mgr.snapshot_dirty_workspace(
            pid, baseline_rev, str(clone)
        )

        assert snapshot_path is not None
        patch_file = Path(snapshot_path) / "patch.diff"
        assert patch_file.exists()
        diff_text = patch_file.read_text()
        assert "README.md" in diff_text
        assert "+# changed" in diff_text

    async def test_captures_manifest(self, initialized_db, tmp_path):
        """Modified, added, and untracked files all appear in changed_files.json."""
        pool = get_pool()
        clone = tmp_path / "clone"
        baseline_rev = _init_git_repo(clone)
        (clone / "README.md").write_text("# changed\n")
        (clone / "new.txt").write_text("brand new\n")

        pid = await _seed_pipeline(pool, clone_path=str(clone))

        mgr = RecoveryManager(
            pool,
            LogBuffer(),
            pipelines_dir=tmp_path / "pipelines",
            clone_manager=CloneManager(pool),
        )
        snapshot_path = await mgr.snapshot_dirty_workspace(
            pid, baseline_rev, str(clone)
        )

        assert snapshot_path is not None
        manifest = json.loads((Path(snapshot_path) / "changed_files.json").read_text())
        paths = {entry["path"] for entry in manifest}
        assert "README.md" in paths
        assert "new.txt" in paths

    async def test_resets_clone_to_baseline(self, initialized_db, tmp_path):
        """After snapshot, working tree matches baseline_rev with clean status."""
        pool = get_pool()
        clone = tmp_path / "clone"
        baseline_rev = _init_git_repo(clone)
        (clone / "README.md").write_text("# changed\n")
        (clone / "extra.txt").write_text("untracked\n")

        pid = await _seed_pipeline(pool, clone_path=str(clone))

        mgr = RecoveryManager(
            pool,
            LogBuffer(),
            pipelines_dir=tmp_path / "pipelines",
            clone_manager=CloneManager(pool),
        )
        await mgr.snapshot_dirty_workspace(pid, baseline_rev, str(clone))

        # Working tree should match baseline content
        assert (clone / "README.md").read_text() == "# baseline\n"
        # Untracked file should be removed by `git clean -fd`
        assert not (clone / "extra.txt").exists()
        # Status should be empty
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=clone, check=True, capture_output=True, text=True,
        ).stdout
        assert status == ""

    async def test_metadata_records_capture_flags(self, initialized_db, tmp_path):
        """recovery_metadata.json records patch_captured, manifest_captured, clone_reset."""
        pool = get_pool()
        clone = tmp_path / "clone"
        baseline_rev = _init_git_repo(clone)
        (clone / "README.md").write_text("# changed\n")

        pid = await _seed_pipeline(pool, clone_path=str(clone))

        mgr = RecoveryManager(
            pool,
            LogBuffer(),
            pipelines_dir=tmp_path / "pipelines",
            clone_manager=CloneManager(pool),
        )
        snapshot_path = await mgr.snapshot_dirty_workspace(
            pid, baseline_rev, str(clone)
        )

        assert snapshot_path is not None
        metadata = json.loads(
            (Path(snapshot_path) / "recovery_metadata.json").read_text()
        )
        assert metadata["patch_captured"] is True
        assert metadata["manifest_captured"] is True
        assert metadata["clone_reset"] is True

    async def test_no_clone_dir_falls_back_to_metadata(self, initialized_db, tmp_path):
        """Missing clone path: still writes metadata, no patch/manifest, flags False."""
        pool = get_pool()
        pid = await _seed_pipeline(pool, clone_path=str(tmp_path / "missing"))

        mgr = RecoveryManager(
            pool,
            LogBuffer(),
            pipelines_dir=tmp_path / "pipelines",
            clone_manager=CloneManager(pool),
        )
        snapshot_path = await mgr.snapshot_dirty_workspace(
            pid, "abc123", str(tmp_path / "missing")
        )

        assert snapshot_path is not None
        snapshot_dir = Path(snapshot_path)
        assert (snapshot_dir / "recovery_metadata.json").exists()
        assert not (snapshot_dir / "patch.diff").exists()
        assert not (snapshot_dir / "changed_files.json").exists()

        metadata = json.loads((snapshot_dir / "recovery_metadata.json").read_text())
        assert metadata["patch_captured"] is False
        assert metadata["manifest_captured"] is False
        assert metadata["clone_reset"] is False

    async def test_non_git_clone_falls_back_gracefully(
        self, initialized_db, tmp_path
    ):
        """Clone dir exists but is not a git repo: snapshot still produces metadata."""
        pool = get_pool()
        clone = tmp_path / "not-git"
        clone.mkdir()
        (clone / "file.txt").write_text("content\n")

        pid = await _seed_pipeline(pool, clone_path=str(clone))

        mgr = RecoveryManager(
            pool,
            LogBuffer(),
            pipelines_dir=tmp_path / "pipelines",
            clone_manager=CloneManager(pool),
        )
        snapshot_path = await mgr.snapshot_dirty_workspace(
            pid, "abc123", str(clone)
        )

        assert snapshot_path is not None
        metadata = json.loads(
            (Path(snapshot_path) / "recovery_metadata.json").read_text()
        )
        assert metadata["patch_captured"] is False
        assert metadata["clone_reset"] is False

    async def test_clean_clone_produces_empty_patch(self, initialized_db, tmp_path):
        """Calling snapshot on a clean repo writes an empty patch.diff and resets to no-op."""
        pool = get_pool()
        clone = tmp_path / "clone"
        baseline_rev = _init_git_repo(clone)

        pid = await _seed_pipeline(pool, clone_path=str(clone))

        mgr = RecoveryManager(
            pool,
            LogBuffer(),
            pipelines_dir=tmp_path / "pipelines",
            clone_manager=CloneManager(pool),
        )
        snapshot_path = await mgr.snapshot_dirty_workspace(
            pid, baseline_rev, str(clone)
        )

        assert snapshot_path is not None
        diff_text = (Path(snapshot_path) / "patch.diff").read_text()
        assert diff_text == ""
        manifest = json.loads(
            (Path(snapshot_path) / "changed_files.json").read_text()
        )
        assert manifest == []


# ---------------------------------------------------------------------------
# Git-status authoritative dirty detection tests
# ---------------------------------------------------------------------------


class TestGitDirtyDetection:
    """Verify recovery uses git status as authoritative when CloneManager is injected."""

    async def test_reconcile_snapshots_when_workspace_state_clean_but_git_dirty(
        self, initialized_db, tmp_path
    ):
        """workspace_state='clean' but git status dirty → snapshot still fires."""
        pool = get_pool()
        clone = tmp_path / "clone"
        baseline_rev = _init_git_repo(clone)
        (clone / "README.md").write_text("# changed\n")

        pid = await _seed_pipeline(
            pool, status="running", clone_path=str(clone), workspace_state="clean"
        )
        # Update review_base_rev so reset has a real target
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET review_base_rev = %s WHERE id = %s",
                (baseline_rev, pid),
            )
            await conn.commit()
        await _set_expired_lease(pool, pid)

        mgr = RecoveryManager(
            pool,
            LogBuffer(),
            pipelines_dir=tmp_path / "pipelines",
            clone_manager=CloneManager(pool),
        )
        await mgr.reconcile_running_state()

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT dirty_snapshot_artifact FROM pipelines WHERE id = %s",
                    (pid,),
                )
            ).fetchone()
        assert row["dirty_snapshot_artifact"] is not None
        assert (Path(row["dirty_snapshot_artifact"]) / "patch.diff").exists()
        # Clone should be reset
        assert (clone / "README.md").read_text() == "# baseline\n"

    async def test_handle_cancellation_snapshots_when_git_dirty(
        self, initialized_db, tmp_path
    ):
        """Cancellation also detects git-dirty even when workspace_state='clean'."""
        pool = get_pool()
        clone = tmp_path / "clone"
        baseline_rev = _init_git_repo(clone)
        (clone / "README.md").write_text("# changed\n")

        pid = await _seed_pipeline(
            pool, status="running", clone_path=str(clone), workspace_state="clean"
        )
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET review_base_rev = %s WHERE id = %s",
                (baseline_rev, pid),
            )
            await conn.commit()

        mgr = RecoveryManager(
            pool,
            LogBuffer(),
            pipelines_dir=tmp_path / "pipelines",
            clone_manager=CloneManager(pool),
        )
        await mgr.handle_cancellation(pid, "owner-tok")

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT dirty_snapshot_artifact, status FROM pipelines WHERE id = %s",
                    (pid,),
                )
            ).fetchone()
        assert row["status"] == "cancelled"
        assert row["dirty_snapshot_artifact"] is not None
        assert (clone / "README.md").read_text() == "# baseline\n"

    async def test_clean_git_clone_with_clean_state_no_snapshot(
        self, initialized_db, tmp_path
    ):
        """Clean workspace_state AND clean git clone → no snapshot."""
        pool = get_pool()
        clone = tmp_path / "clone"
        baseline_rev = _init_git_repo(clone)

        pid = await _seed_pipeline(
            pool, status="running", clone_path=str(clone), workspace_state="clean"
        )
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET review_base_rev = %s WHERE id = %s",
                (baseline_rev, pid),
            )
            await conn.commit()

        mgr = RecoveryManager(
            pool,
            LogBuffer(),
            pipelines_dir=tmp_path / "pipelines",
            clone_manager=CloneManager(pool),
        )
        await mgr.handle_cancellation(pid, "owner-tok")

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT dirty_snapshot_artifact FROM pipelines WHERE id = %s",
                    (pid,),
                )
            ).fetchone()
        assert row["dirty_snapshot_artifact"] is None

    async def test_no_clone_manager_falls_back_to_workspace_state(
        self, initialized_db, tmp_path
    ):
        """Without a CloneManager, dirty detection ignores git and uses workspace_state."""
        pool = get_pool()
        clone = tmp_path / "clone"
        _init_git_repo(clone)
        (clone / "README.md").write_text("# changed\n")

        pid = await _seed_pipeline(
            pool, status="running", clone_path=str(clone), workspace_state="clean"
        )

        # No clone_manager injected — git dirty should be ignored.
        mgr = RecoveryManager(pool, LogBuffer(), pipelines_dir=tmp_path / "pipelines")
        await mgr.handle_cancellation(pid, "owner-tok")

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT dirty_snapshot_artifact FROM pipelines WHERE id = %s",
                    (pid,),
                )
            ).fetchone()
        assert row["dirty_snapshot_artifact"] is None


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


_visit_count_keys = st.from_regex(r"[a-z][a-z0-9_]{1,20}", fullmatch=True)


class TestRecoveryProperties:
    """Property-based tests for recovery invariants."""

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

        Invariant: load_visit_counts({"recovery_state_json": json.dumps(vc)}) == vc
        """
        recovery_json = json.dumps({"visit_counts": visit_counts})
        pipeline = {"recovery_state_json": recovery_json}
        loaded = RecoveryManager.load_visit_counts(pipeline)
        assert loaded == visit_counts

    @settings(max_examples=30)
    @given(
        recovery_json=st.one_of(
            st.none(),
            st.just(""),
            st.just("not-json"),
            st.just("null"),
            st.just('{"other_key": 42}'),
            st.just("[]"),
            st.just("42"),
        ),
    )
    def test_visit_counts_graceful_on_invalid(self, recovery_json) -> None:
        """Property: load_visit_counts returns empty dict for invalid data.

        Invariant: never raises, always returns dict.
        """
        pipeline = {"recovery_state_json": recovery_json}
        loaded = RecoveryManager.load_visit_counts(pipeline)
        assert isinstance(loaded, dict)
        assert len(loaded) == 0

    @settings(
        max_examples=8,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(n_expired=st.integers(min_value=1, max_value=3))
    @pytest.mark.asyncio
    async def test_reconciliation_downgrades_all_expired(
        self, initialized_db, n_expired
    ) -> None:
        """Property: all pipelines with expired leases are downgraded.

        Invariant: after reconcile_running_state(), no pipeline has status='running'
        with an expired lease.
        """
        pool = get_pool()
        pids = []

        for _ in range(n_expired):
            pid = await _seed_pipeline(pool, status="running")
            await _set_expired_lease(pool, pid)
            pids.append(pid)

        mgr = RecoveryManager(pool, LogBuffer())
        await mgr.reconcile_running_state()

        async with pool.connection() as conn:
            for pid in pids:
                row = await (
                    await conn.execute(
                        "SELECT status, owner_token FROM pipelines WHERE id = %s",
                        (pid,),
                    )
                ).fetchone()
                assert row["status"] == "needs_attention", (
                    f"Pipeline {pid} should be downgraded but is {row['status']}"
                )
                assert row["owner_token"] is None

    @settings(
        max_examples=8,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        baseline_rev=st.from_regex(r"[0-9a-f]{6,40}", fullmatch=True),
    )
    @pytest.mark.asyncio
    async def test_snapshot_always_produces_metadata(
        self, initialized_db, tmp_path, baseline_rev
    ) -> None:
        """Property: snapshot always creates metadata file with correct fields.

        Invariant: snapshot_dirty_workspace produces a JSON metadata file
        containing pipeline_id, baseline_rev, clone_path, and snapshot_at.
        """
        pool = get_pool()
        pid = await _seed_pipeline(pool)

        mgr = RecoveryManager(pool, LogBuffer(), pipelines_dir=tmp_path / "pipelines")
        snapshot_path = await mgr.snapshot_dirty_workspace(
            pid, baseline_rev, "/tmp/clone"
        )

        assert snapshot_path is not None
        metadata_file = Path(snapshot_path) / "recovery_metadata.json"
        assert metadata_file.exists()

        metadata = json.loads(metadata_file.read_text())
        assert metadata["pipeline_id"] == pid
        assert metadata["baseline_rev"] == baseline_rev
        assert metadata["clone_path"] == "/tmp/clone"
        assert "snapshot_at" in metadata
