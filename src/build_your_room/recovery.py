"""Startup reconciliation, dirty-workspace snapshot/reset, and recovery state.

Extracts recovery concerns from the orchestrator into a focused module.
Handles: startup scan of stale 'running' rows, workspace snapshot to
state/recovery/, cancellation cleanup, and visit-count persistence.

Invariants enforced:
- WorkspaceMatchesHeadUnlessOwned: dirty workspaces without a live owner
  must be snapshotted and reset before recovery or review.
- RunningImpliesOwner: stale running pipelines with expired leases are
  downgraded to 'needs_attention' with an escalation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from psycopg_pool import AsyncConnectionPool

from build_your_room.clone_manager import CloneManager, GitError
from build_your_room.config import PIPELINES_DIR
from build_your_room.streaming import LogBuffer

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return the current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


class RecoveryManager:
    """Startup reconciliation + dirty-workspace recovery + cancellation cleanup.

    Works alongside LeaseManager: LeaseManager owns lease acquire/release/renew,
    RecoveryManager owns what happens when a lease is found stale or when a
    pipeline needs to be cancelled/snapshotted.

    When a :class:`CloneManager` is injected, snapshot operations capture a
    full patch + changed-files manifest from the clone and reset the working
    tree to the accepted baseline (spec invariant ``WorkspaceMatchesHeadUnlessOwned``).
    Without a clone manager the snapshot falls back to metadata-only.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        log_buffer: LogBuffer,
        *,
        pipelines_dir: Path | None = None,
        clone_manager: CloneManager | None = None,
    ) -> None:
        self._pool = pool
        self._log_buffer = log_buffer
        self._pipelines_dir = pipelines_dir or PIPELINES_DIR
        self._clone_manager = clone_manager

    @property
    def pipelines_dir(self) -> Path:
        return self._pipelines_dir

    # ------------------------------------------------------------------
    # Startup reconciliation
    # ------------------------------------------------------------------

    async def reconcile_running_state(self) -> None:
        """Startup recovery: scan for stale running rows and recover or downgrade.

        Called once during app lifespan startup. For each row with status='running':
        - If the lease is still live, skip it (another process may own it)
        - If the lease is expired and no live owner, downgrade to 'needs_attention'
        - If workspace is dirty without a live owner, snapshot and reset
        """
        now = _utc_now()
        async with self._pool.connection() as conn:
            rows: list[dict[str, Any]] = await (  # type: ignore[assignment]
                await conn.execute(
                    "SELECT id, clone_path, head_rev, review_base_rev, workspace_state, "
                    "owner_token, lease_expires_at "
                    "FROM pipelines WHERE status = 'running'"
                )
            ).fetchall()

            for row in rows:
                pipeline_id = row["id"]
                lease_expires = row["lease_expires_at"]

                if lease_expires and lease_expires > now:
                    logger.info(
                        "Pipeline %d has a live lease (expires %s), skipping",
                        pipeline_id,
                        lease_expires,
                    )
                    continue

                logger.warning(
                    "Pipeline %d has expired/missing lease, recovering", pipeline_id
                )

                baseline_rev = row["head_rev"] or row["review_base_rev"]
                clone_path = row["clone_path"]

                if await self._workspace_appears_dirty(
                    row["workspace_state"], clone_path
                ):
                    snapshot_path = await self.snapshot_dirty_workspace(
                        pipeline_id, baseline_rev, clone_path, conn=conn
                    )
                    logger.warning(
                        "Pipeline %d dirty workspace snapshotted to %s",
                        pipeline_id,
                        snapshot_path,
                    )

                # Fail running/review_loop stages
                await conn.execute(
                    "UPDATE pipeline_stages SET status = 'failed', "
                    "escalation_reason = 'startup_recovery', completed_at = now() "
                    "WHERE pipeline_id = %s AND status IN ('running', 'review_loop')",
                    (pipeline_id,),
                )

                # Interrupt running sessions
                await conn.execute(
                    "UPDATE agent_sessions SET status = 'interrupted', completed_at = now() "
                    "WHERE pipeline_stage_id IN ("
                    "  SELECT id FROM pipeline_stages WHERE pipeline_id = %s"
                    ") AND status = 'running'",
                    (pipeline_id,),
                )

                # Release in-progress HTN task claims
                await conn.execute(
                    "UPDATE htn_tasks SET status = 'ready', assigned_session_id = NULL, "
                    "claim_token = NULL, claim_owner_token = NULL, claim_expires_at = NULL "
                    "WHERE pipeline_id = %s AND status = 'in_progress'",
                    (pipeline_id,),
                )

                # Downgrade the pipeline
                await conn.execute(
                    "UPDATE pipelines SET status = 'needs_attention', "
                    "owner_token = NULL, lease_expires_at = NULL, "
                    "workspace_state = 'clean', updated_at = now() "
                    "WHERE id = %s",
                    (pipeline_id,),
                )

                await self._create_escalation(
                    conn,
                    pipeline_id,
                    stage_id=None,
                    reason="startup_recovery",
                    context={"message": "Pipeline recovered during startup reconciliation"},
                )

            await conn.commit()

        logger.info(
            "Startup reconciliation complete — processed %d running pipelines", len(rows)
        )

    # ------------------------------------------------------------------
    # Dirty workspace snapshot
    # ------------------------------------------------------------------

    async def snapshot_dirty_workspace(
        self,
        pipeline_id: int,
        baseline_rev: str,
        clone_path: str,
        *,
        conn: Any | None = None,
    ) -> str | None:
        """Capture uncheckpointed edits into ``state/recovery/{timestamp}/``.

        Always writes ``recovery_metadata.json``. When a :class:`CloneManager`
        is injected and the clone is a real git repo, additionally captures
        ``patch.diff`` (full diff incl. untracked files) and
        ``changed_files.json`` (per-file porcelain status), then resets the
        working tree to ``baseline_rev`` to satisfy the
        ``WorkspaceMatchesHeadUnlessOwned`` invariant.

        Returns the snapshot artifact path. Reset failures are logged but do
        not raise — the snapshot itself is still recorded so an operator can
        inspect what was lost.
        """
        recovery_dir = self._pipelines_dir / str(pipeline_id) / "state" / "recovery"
        recovery_dir.mkdir(parents=True, exist_ok=True)

        timestamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
        snapshot_path = str(recovery_dir / timestamp)
        snapshot_dir = Path(snapshot_path)
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        patch_captured = await self._capture_patch_artifact(
            snapshot_dir, clone_path, baseline_rev
        )
        manifest_captured = await self._capture_changed_files_manifest(
            snapshot_dir, clone_path
        )
        reset_ok = await self._reset_clone_to_baseline(clone_path, baseline_rev)

        metadata = {
            "pipeline_id": pipeline_id,
            "baseline_rev": baseline_rev,
            "clone_path": clone_path,
            "snapshot_at": timestamp,
            "patch_captured": patch_captured,
            "manifest_captured": manifest_captured,
            "clone_reset": reset_ok,
        }
        metadata_file = snapshot_dir / "recovery_metadata.json"
        metadata_file.write_text(json.dumps(metadata, indent=2))

        if conn is not None:
            await conn.execute(
                "UPDATE pipelines SET workspace_state = 'clean', "
                "dirty_snapshot_artifact = %s, updated_at = now() WHERE id = %s",
                (snapshot_path, pipeline_id),
            )
        else:
            async with self._pool.connection() as new_conn:
                await new_conn.execute(
                    "UPDATE pipelines SET workspace_state = 'clean', "
                    "dirty_snapshot_artifact = %s, updated_at = now() WHERE id = %s",
                    (snapshot_path, pipeline_id),
                )
                await new_conn.commit()

        logger.info(
            "Snapshotted dirty workspace for pipeline %d to %s "
            "(patch=%s, manifest=%s, reset=%s)",
            pipeline_id,
            snapshot_path,
            patch_captured,
            manifest_captured,
            reset_ok,
        )
        return snapshot_path

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------

    async def _workspace_appears_dirty(
        self, workspace_state: str | None, clone_path: str | None
    ) -> bool:
        """Return True if either the workspace_state hint or git status says dirty.

        ``workspace_state`` is a hint that may not reflect reality (the
        orchestrator does not always transition it to ``'dirty_live'``).
        Git status — when available — is authoritative.
        """
        if workspace_state and workspace_state != "clean":
            return True
        if not clone_path or self._clone_manager is None:
            return False
        if not Path(clone_path).is_dir():
            return False
        try:
            return not await self._clone_manager.is_workspace_clean(clone_path)
        except GitError:
            # Not a git repo, or git failed for another reason — fall back
            # to the workspace_state hint (already evaluated above as clean).
            return False

    async def _capture_patch_artifact(
        self, snapshot_dir: Path, clone_path: str | None, baseline_rev: str
    ) -> bool:
        """Write ``patch.diff`` to the snapshot directory if possible.

        Returns True if a patch file was written (even if empty), False if
        the clone could not be read.
        """
        if (
            not clone_path
            or self._clone_manager is None
            or not Path(clone_path).is_dir()
        ):
            return False
        try:
            diff_text = await self._clone_manager.capture_dirty_diff(
                clone_path, baseline_rev
            )
        except GitError as exc:
            logger.warning(
                "capture_dirty_diff failed for %s: %s", clone_path, exc.stderr
            )
            return False
        (snapshot_dir / "patch.diff").write_text(diff_text)
        return True

    async def _capture_changed_files_manifest(
        self, snapshot_dir: Path, clone_path: str | None
    ) -> bool:
        """Write ``changed_files.json`` listing per-file porcelain entries.

        Returns True if the manifest was written, False if git status was
        unavailable.
        """
        if not clone_path or not Path(clone_path).is_dir():
            return False
        from build_your_room.clone_manager import _run_git

        try:
            porcelain = await _run_git(
                ["status", "--porcelain"], cwd=clone_path
            )
        except GitError as exc:
            logger.warning(
                "git status --porcelain failed for %s: %s", clone_path, exc.stderr
            )
            return False

        entries: list[dict[str, str]] = []
        for line in porcelain.splitlines():
            # _run_git strips outer whitespace, collapsing the porcelain " M"
            # leading-space encoding. Split on whitespace once to recover
            # status code + path.
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            entries.append({"status": parts[0], "path": parts[1]})

        manifest_file = snapshot_dir / "changed_files.json"
        manifest_file.write_text(json.dumps(entries, indent=2))
        return True

    async def _reset_clone_to_baseline(
        self, clone_path: str | None, baseline_rev: str
    ) -> bool:
        """Reset the working tree to ``baseline_rev``. Logs and skips on failure."""
        if (
            not clone_path
            or self._clone_manager is None
            or not Path(clone_path).is_dir()
        ):
            return False
        try:
            await self._clone_manager.reset_to_rev(clone_path, baseline_rev)
        except GitError as exc:
            logger.warning(
                "reset_to_rev(%s, %s) failed: %s",
                clone_path,
                baseline_rev,
                exc.stderr,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Cancellation cleanup
    # ------------------------------------------------------------------

    async def handle_cancellation(
        self, pipeline_id: int, owner_token: str
    ) -> None:
        """Handle cooperative cancellation: snapshot, reset claims, mark cancelled."""
        async with self._pool.connection() as conn:
            pipeline_row: dict[str, Any] | None = await (  # type: ignore[assignment]
                await conn.execute(
                    "SELECT clone_path, head_rev, review_base_rev, workspace_state "
                    "FROM pipelines WHERE id = %s",
                    (pipeline_id,),
                )
            ).fetchone()

            if pipeline_row and await self._workspace_appears_dirty(
                pipeline_row["workspace_state"], pipeline_row["clone_path"]
            ):
                baseline = pipeline_row["head_rev"] or pipeline_row["review_base_rev"]
                await self.snapshot_dirty_workspace(
                    pipeline_id, baseline, pipeline_row["clone_path"], conn=conn
                )

            # Release in-progress HTN task claims back to ready
            await conn.execute(
                "UPDATE htn_tasks SET status = 'ready', assigned_session_id = NULL, "
                "claim_token = NULL, claim_owner_token = NULL, claim_expires_at = NULL "
                "WHERE pipeline_id = %s AND status = 'in_progress'",
                (pipeline_id,),
            )

            # Mark running sessions as cancelled
            await conn.execute(
                "UPDATE agent_sessions SET status = 'cancelled', completed_at = now() "
                "WHERE pipeline_stage_id IN ("
                "  SELECT id FROM pipeline_stages WHERE pipeline_id = %s"
                ") AND status = 'running'",
                (pipeline_id,),
            )

            # Mark running stages as cancelled
            await conn.execute(
                "UPDATE pipeline_stages SET status = 'cancelled', completed_at = now() "
                "WHERE pipeline_id = %s AND status IN ('running', 'review_loop')",
                (pipeline_id,),
            )

            await conn.execute(
                "UPDATE pipelines SET status = 'cancelled', updated_at = now() WHERE id = %s",
                (pipeline_id,),
            )
            await conn.commit()

        self._log_buffer.append(pipeline_id, "Pipeline cancelled")
        self._log_buffer.close(pipeline_id)

    # ------------------------------------------------------------------
    # Recovery state helpers
    # ------------------------------------------------------------------

    @staticmethod
    def load_visit_counts(pipeline: dict) -> dict[str, int]:
        """Load edge visit counts from recovery_state_json."""
        recovery = pipeline.get("recovery_state_json")
        if not recovery:
            return {}
        try:
            data = json.loads(recovery)
            if not isinstance(data, dict):
                return {}
            return data.get("visit_counts", {})
        except (json.JSONDecodeError, TypeError):
            return {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _create_escalation(
        conn: Any,
        pipeline_id: int,
        stage_id: int | None,
        reason: str,
        context: dict,
    ) -> int:
        """Insert an escalation row and return its ID."""
        row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO escalations (pipeline_id, pipeline_stage_id, reason, context_json) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (pipeline_id, stage_id, reason, json.dumps(context)),
            )
        ).fetchone()
        return row["id"]
