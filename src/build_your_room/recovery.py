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
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        log_buffer: LogBuffer,
        *,
        pipelines_dir: Path | None = None,
    ) -> None:
        self._pool = pool
        self._log_buffer = log_buffer
        self._pipelines_dir = pipelines_dir or PIPELINES_DIR

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

                if row["workspace_state"] != "clean":
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
        """Capture uncheckpointed edits into state/recovery/.

        Returns the snapshot artifact path, or None if the workspace was clean.
        """
        recovery_dir = self._pipelines_dir / str(pipeline_id) / "state" / "recovery"
        recovery_dir.mkdir(parents=True, exist_ok=True)

        timestamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
        snapshot_path = str(recovery_dir / timestamp)
        Path(snapshot_path).mkdir(parents=True, exist_ok=True)

        metadata = {
            "pipeline_id": pipeline_id,
            "baseline_rev": baseline_rev,
            "clone_path": clone_path,
            "snapshot_at": timestamp,
        }
        metadata_file = Path(snapshot_path) / "recovery_metadata.json"
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
            "Snapshotted dirty workspace for pipeline %d to %s",
            pipeline_id,
            snapshot_path,
        )
        return snapshot_path

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

            if pipeline_row and pipeline_row["workspace_state"] != "clean":
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
