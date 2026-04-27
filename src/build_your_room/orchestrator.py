"""PipelineOrchestrator — core engine for managing parallel coding pipelines.

Replaces clean-your-room's JobRunner with a stage-graph-driven orchestrator
that supports durable leases, dirty-workspace recovery, and startup reconciliation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from psycopg_pool import AsyncConnectionPool

from build_your_room.adapters.base import AgentAdapter
from build_your_room.clone_manager import CloneManager
from build_your_room.config import (
    PIPELINE_HEARTBEAT_INTERVAL_SEC,
    PIPELINE_LEASE_TTL_SEC,
)
from build_your_room.lease_manager import LeaseManager
from build_your_room.recovery import RecoveryManager
from build_your_room.stage_graph import StageGraph
from build_your_room.stages import get_stage_runner
from build_your_room.streaming import LogBuffer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage result constants
# ---------------------------------------------------------------------------

STAGE_RESULT_APPROVED = "approved"
STAGE_RESULT_STAGE_COMPLETE = "stage_complete"
STAGE_RESULT_VALIDATION_FAILED = "validation_failed"
STAGE_RESULT_VALIDATED = "validated"
STAGE_RESULT_ESCALATED = "escalated"


# ---------------------------------------------------------------------------
# PipelineOrchestrator
# ---------------------------------------------------------------------------


class PipelineOrchestrator:
    """Core engine that drives pipeline execution through stage graphs.

    Each pipeline runs as an asyncio.Task with its own cancel Event.
    A semaphore limits concurrent pipelines. DB lease state is the
    source of truth; ``_active_pipelines`` is an in-memory cache.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        log_buffer: LogBuffer,
        *,
        max_concurrent: int = 10,
        lease_ttl_sec: int = PIPELINE_LEASE_TTL_SEC,
        heartbeat_interval_sec: int = PIPELINE_HEARTBEAT_INTERVAL_SEC,
        adapters: dict[str, AgentAdapter] | None = None,
        clone_manager: CloneManager | None = None,
        lease_manager: LeaseManager | None = None,
        recovery_manager: RecoveryManager | None = None,
    ) -> None:
        self._pool = pool
        self._log_buffer = log_buffer
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._lease_ttl_sec = lease_ttl_sec
        self._heartbeat_interval_sec = heartbeat_interval_sec
        self._adapters: dict[str, AgentAdapter] = adapters or {}
        self._clone_manager = clone_manager or CloneManager(pool)
        self._lease_manager = lease_manager or LeaseManager(
            pool,
            lease_ttl_sec=lease_ttl_sec,
            heartbeat_interval_sec=heartbeat_interval_sec,
        )
        self._recovery_manager = recovery_manager or RecoveryManager(
            pool, log_buffer, clone_manager=self._clone_manager
        )
        self._active_pipelines: dict[int, tuple[asyncio.Task, asyncio.Event]] = {}  # type: ignore[type-arg]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start_pipeline(self, pipeline_id: int) -> None:
        """Launch a pipeline as a background asyncio.Task."""
        if pipeline_id in self._active_pipelines:
            logger.warning("Pipeline %d already active", pipeline_id)
            return

        cancel_event = asyncio.Event()
        task = asyncio.create_task(
            self._guarded_run(pipeline_id, cancel_event),
            name=f"pipeline-{pipeline_id}",
        )
        self._active_pipelines[pipeline_id] = (task, cancel_event)

    async def cancel_pipeline(self, pipeline_id: int) -> None:
        """Request cooperative cancellation of a running pipeline."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET status = 'cancel_requested', updated_at = now() "
                "WHERE id = %s AND status = 'running'",
                (pipeline_id,),
            )
            await conn.commit()

        entry = self._active_pipelines.get(pipeline_id)
        if entry:
            _, cancel_event = entry
            cancel_event.set()

    async def kill_pipeline(self, pipeline_id: int) -> None:
        """Immediately terminate a pipeline's live sessions."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET status = 'killed', updated_at = now() "
                "WHERE id = %s AND status IN ('running', 'cancel_requested', 'paused')",
                (pipeline_id,),
            )
            await conn.commit()

        entry = self._active_pipelines.pop(pipeline_id, None)
        if entry:
            task, cancel_event = entry
            cancel_event.set()
            task.cancel()

    async def resume_pipeline(self, pipeline_id: int, resolution: str) -> None:
        """Resume a paused pipeline after human escalation resolution."""
        async with self._pool.connection() as conn:
            row: dict[str, Any] | None = await (  # type: ignore[assignment]
                await conn.execute(
                    "SELECT status, current_stage_key FROM pipelines WHERE id = %s",
                    (pipeline_id,),
                )
            ).fetchone()
            if not row or row["status"] != "paused":
                logger.warning(
                    "Cannot resume pipeline %d — status is %s",
                    pipeline_id,
                    row["status"] if row else "not found",
                )
                return

            # Resolve the open escalation
            await conn.execute(
                "UPDATE escalations SET status = 'resolved', resolution = %s, "
                "resolved_at = now() WHERE pipeline_id = %s AND status = 'open'",
                (resolution, pipeline_id),
            )
            await conn.execute(
                "UPDATE pipelines SET status = 'pending', updated_at = now() WHERE id = %s",
                (pipeline_id,),
            )
            await conn.commit()

        await self.start_pipeline(pipeline_id)

    async def reconcile_running_state(self) -> None:
        """Startup recovery: scan for stale running rows and recover or downgrade.

        Delegates to RecoveryManager.
        """
        await self._recovery_manager.reconcile_running_state()

    # ------------------------------------------------------------------
    # Core pipeline loop
    # ------------------------------------------------------------------

    async def _guarded_run(self, pipeline_id: int, cancel_event: asyncio.Event) -> None:
        """Acquire the semaphore, run the pipeline, and clean up."""
        async with self._semaphore:
            try:
                await self._run_pipeline(pipeline_id, cancel_event)
            except asyncio.CancelledError:
                logger.info("Pipeline %d cancelled via task cancellation", pipeline_id)
            except Exception:
                logger.exception("Pipeline %d failed with unhandled error", pipeline_id)
                async with self._pool.connection() as conn:
                    await conn.execute(
                        "UPDATE pipelines SET status = 'failed', updated_at = now() "
                        "WHERE id = %s",
                        (pipeline_id,),
                    )
                    await conn.commit()
            finally:
                self._active_pipelines.pop(pipeline_id, None)

    async def _ensure_clone(self, pipeline_id: int) -> None:
        """Ensure the pipeline has a working clone directory.

        Spec lifecycle steps 1-2: Clone the repo to an isolated directory,
        capture review_base_rev, and create pipeline support directories.
        Skips cloning if clone_path is already set and the directory exists
        (e.g., resumed pipeline after escalation).
        """
        async with self._pool.connection() as conn:
            row: dict[str, Any] | None = await (  # type: ignore[assignment]
                await conn.execute(
                    "SELECT clone_path, repo_id FROM pipelines WHERE id = %s",
                    (pipeline_id,),
                )
            ).fetchone()

        if not row:
            raise ValueError(f"Pipeline {pipeline_id} not found")

        clone_path = row["clone_path"]
        if clone_path and Path(clone_path).exists():
            logger.debug(
                "Pipeline %d already has clone at %s", pipeline_id, clone_path
            )
            return

        if clone_path:
            logger.warning(
                "Pipeline %d clone_path set to %s but directory missing — re-cloning",
                pipeline_id,
                clone_path,
            )

        repo_id = row["repo_id"]
        result = await self._clone_manager.create_clone(pipeline_id, repo_id)
        self._log_buffer.append(
            pipeline_id,
            f"Clone created at {result.clone_path} "
            f"(base_rev={result.review_base_rev[:8]})",
        )

    async def _run_pipeline(self, pipeline_id: int, cancel_event: asyncio.Event) -> None:
        """Main loop: clone repo, acquire lease, walk the stage graph, handle transitions."""
        # Spec lifecycle steps 1-2: ensure clone exists
        await self._ensure_clone(pipeline_id)

        # Spec lifecycle step 3: acquire lease
        owner_token = await self._lease_manager.acquire_pipeline_lease(pipeline_id)

        heartbeat_task = asyncio.create_task(
            self._lease_manager.heartbeat_loop(pipeline_id, owner_token, cancel_event),
            name=f"heartbeat-{pipeline_id}",
        )

        try:
            pipeline, graph = await self._load_pipeline_and_graph(pipeline_id)
            current_key = pipeline["current_stage_key"] or graph.entry_stage
            visit_counts = RecoveryManager.load_visit_counts(pipeline)

            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE pipelines SET status = 'running', current_stage_key = %s, "
                    "updated_at = now() WHERE id = %s",
                    (current_key, pipeline_id),
                )
                await conn.commit()

            self._log_buffer.append(
                pipeline_id,
                f"Pipeline started — entering stage '{current_key}'",
            )

            while current_key != "completed":
                if cancel_event.is_set():
                    await self._handle_cancellation(pipeline_id, owner_token)
                    return

                stage_result = await self._run_stage(pipeline_id, current_key, graph, cancel_event)

                if stage_result == STAGE_RESULT_ESCALATED:
                    async with self._pool.connection() as conn:
                        await conn.execute(
                            "UPDATE pipelines SET status = 'paused', updated_at = now() "
                            "WHERE id = %s",
                            (pipeline_id,),
                        )
                        await conn.commit()
                    self._log_buffer.append(pipeline_id, "Pipeline paused — escalation required")
                    return

                next_key, edge = graph.resolve_next_stage(
                    current_key, stage_result, visit_counts
                )

                if next_key is None and edge is not None:
                    # Edge exhausted with escalation
                    await self.escalate(
                        pipeline_id,
                        stage_id=None,
                        reason="max_iterations",
                        context={
                            "edge": edge.key,
                            "message": f"Edge {edge.key!r} exhausted after "
                            f"{visit_counts.get(edge.key, 0)} visits",
                        },
                    )
                    return

                if next_key is None:
                    logger.error(
                        "No transition from stage %r with result %r",
                        current_key,
                        stage_result,
                    )
                    await self.escalate(
                        pipeline_id,
                        stage_id=None,
                        reason="agent_error",
                        context={
                            "message": f"No valid transition from {current_key!r} "
                            f"with result {stage_result!r}",
                        },
                    )
                    return

                if edge is not None:
                    visit_counts[edge.key] = visit_counts.get(edge.key, 0) + 1

                current_key = next_key
                async with self._pool.connection() as conn:
                    await conn.execute(
                        "UPDATE pipelines SET current_stage_key = %s, "
                        "recovery_state_json = %s, updated_at = now() WHERE id = %s",
                        (current_key, json.dumps({"visit_counts": visit_counts}), pipeline_id),
                    )
                    await conn.commit()

                self._log_buffer.append(
                    pipeline_id, f"Transitioning to stage '{current_key}'"
                )

            # Pipeline completed
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE pipelines SET status = 'completed', updated_at = now() "
                    "WHERE id = %s",
                    (pipeline_id,),
                )
                await conn.commit()
            self._log_buffer.append(pipeline_id, "Pipeline completed successfully")
            self._log_buffer.close(pipeline_id)

        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            await self._lease_manager.release_pipeline_lease(pipeline_id)

    async def _run_stage(
        self,
        pipeline_id: int,
        stage_key: str,
        graph: StageGraph,
        cancel_event: asyncio.Event,
    ) -> str:
        """Execute a single stage and return its result string.

        Creates a pipeline_stages row, dispatches to the appropriate agent
        adapter (stub for now), and returns the result for edge resolution.
        """
        node = graph.get_node(stage_key)

        # Create the stage execution row
        async with self._pool.connection() as conn:
            # Determine the attempt number
            attempt_row: dict[str, Any] | None = await (  # type: ignore[assignment]
                await conn.execute(
                    "SELECT COALESCE(MAX(attempt), 0) + 1 AS next_attempt "
                    "FROM pipeline_stages WHERE pipeline_id = %s AND stage_key = %s",
                    (pipeline_id, stage_key),
                )
            ).fetchone()
            attempt = attempt_row["next_attempt"] if attempt_row else 1

            p_row: dict[str, Any] | None = await (  # type: ignore[assignment]
                await conn.execute(
                    "SELECT head_rev, review_base_rev FROM pipelines WHERE id = %s",
                    (pipeline_id,),
                )
            ).fetchone()
            entry_rev = (
                p_row["head_rev"] or p_row["review_base_rev"]
                if p_row
                else None
            )

            stage_row: dict[str, Any] | None = await (  # type: ignore[assignment]
                await conn.execute(
                    "INSERT INTO pipeline_stages "
                    "(pipeline_id, stage_key, attempt, stage_type, agent_type, "
                    " status, entry_rev, max_iterations, started_at) "
                    "VALUES (%s, %s, %s, %s, %s, 'running', %s, %s, now()) "
                    "RETURNING id",
                    (
                        pipeline_id,
                        stage_key,
                        attempt,
                        node.stage_type,
                        node.agent,
                        entry_rev,
                        node.max_iterations,
                    ),
                )
            ).fetchone()
            stage_id = stage_row["id"] if stage_row else None
            await conn.commit()

        self._log_buffer.append(
            pipeline_id,
            f"Stage '{stage_key}' started (attempt {attempt}, type={node.stage_type})",
        )

        # Dispatch to agent adapter (stub — adapters wired in Tasks 10-12)
        adapter = self._adapters.get(node.agent)
        if adapter is None:
            logger.warning(
                "No adapter registered for agent type %r — stage %r will be skipped",
                node.agent,
                stage_key,
            )
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE pipeline_stages SET status = 'skipped', completed_at = now() "
                    "WHERE id = %s",
                    (stage_id,),
                )
                await conn.commit()
            # For skeleton: return a default result based on stage type
            return self._default_stage_result(node.stage_type)

        # Dispatch to stage-specific runners via registry
        runner = get_stage_runner(node.stage_type) if stage_id is not None else None
        if runner is not None:
            result = await runner(
                pool=self._pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters=self._adapters,
                log_buffer=self._log_buffer,
                cancel_event=cancel_event,
            )
        else:
            result = self._default_stage_result(node.stage_type)

        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pipeline_stages SET status = 'completed', completed_at = now() "
                "WHERE id = %s",
                (stage_id,),
            )
            await conn.commit()

        self._log_buffer.append(
            pipeline_id, f"Stage '{stage_key}' completed with result '{result}'"
        )
        return result

    @staticmethod
    def _default_stage_result(stage_type: str) -> str:
        """Return the expected result for a stage type (skeleton default)."""
        mapping = {
            "spec_author": STAGE_RESULT_APPROVED,
            "impl_plan": STAGE_RESULT_APPROVED,
            "impl_task": STAGE_RESULT_STAGE_COMPLETE,
            "code_review": STAGE_RESULT_APPROVED,
            "validation": STAGE_RESULT_VALIDATED,
        }
        return mapping.get(stage_type, STAGE_RESULT_APPROVED)

    # ------------------------------------------------------------------
    # Lease management (delegated to LeaseManager)
    # ------------------------------------------------------------------

    async def _acquire_pipeline_lease(self, pipeline_id: int) -> str:
        """Atomically acquire the pipeline lease. Returns the owner_token."""
        return await self._lease_manager.acquire_pipeline_lease(pipeline_id)

    async def _release_pipeline_lease(self, pipeline_id: int) -> None:
        """Release the pipeline lease."""
        await self._lease_manager.release_pipeline_lease(pipeline_id)

    async def _heartbeat_loop(
        self,
        pipeline_id: int,
        owner_token: str,
        cancel_event: asyncio.Event,
    ) -> None:
        """Periodically renew the pipeline lease until cancelled."""
        await self._lease_manager.heartbeat_loop(pipeline_id, owner_token, cancel_event)

    async def renew_leases(
        self,
        pipeline_id: int,
        stage_id: int | None = None,
        session_id: int | None = None,
    ) -> None:
        """Renew leases for pipeline and optionally stage/session.

        Called by stage runners during long-running operations.
        Delegates to the LeaseManager.
        """
        await self._lease_manager.renew_leases(pipeline_id, stage_id, session_id)

    # ------------------------------------------------------------------
    # Workspace management
    # ------------------------------------------------------------------

    async def _snapshot_dirty_workspace(
        self,
        pipeline_id: int,
        baseline_rev: str,
        clone_path: str,
        conn: Any | None = None,
    ) -> str | None:
        """Capture uncheckpointed edits into state/recovery/.

        Delegates to RecoveryManager.
        """
        return await self._recovery_manager.snapshot_dirty_workspace(
            pipeline_id, baseline_rev, clone_path, conn=conn
        )

    # ------------------------------------------------------------------
    # Escalation
    # ------------------------------------------------------------------

    async def escalate(
        self,
        pipeline_id: int,
        stage_id: int | None,
        reason: str,
        context: dict,
    ) -> int:
        """Create an escalation, pause the pipeline, and return the escalation ID."""
        async with self._pool.connection() as conn:
            escalation_id = await self._create_escalation(
                conn, pipeline_id, stage_id, reason, context
            )
            await conn.execute(
                "UPDATE pipelines SET status = 'paused', updated_at = now() WHERE id = %s",
                (pipeline_id,),
            )
            await conn.commit()

        self._log_buffer.append(
            pipeline_id,
            f"Escalation created (reason={reason}): {context.get('message', '')}",
        )
        return escalation_id

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

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    async def _handle_cancellation(self, pipeline_id: int, owner_token: str) -> None:
        """Handle cooperative cancellation: snapshot, reset claims, mark cancelled.

        Delegates to RecoveryManager.
        """
        await self._recovery_manager.handle_cancellation(pipeline_id, owner_token)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _load_pipeline_and_graph(
        self, pipeline_id: int
    ) -> tuple[dict, StageGraph]:
        """Load pipeline row and parse its stage graph."""
        async with self._pool.connection() as conn:
            pipeline_row: dict[str, Any] | None = await (  # type: ignore[assignment]
                await conn.execute(
                    "SELECT p.*, pd.stage_graph_json "
                    "FROM pipelines p "
                    "JOIN pipeline_defs pd ON p.pipeline_def_id = pd.id "
                    "WHERE p.id = %s",
                    (pipeline_id,),
                )
            ).fetchone()

        if not pipeline_row:
            raise ValueError(f"Pipeline {pipeline_id} not found")

        graph_data = json.loads(pipeline_row["stage_graph_json"])
        graph = StageGraph.from_json(graph_data)
        return dict(pipeline_row), graph

    @staticmethod
    def _load_visit_counts(pipeline: dict) -> dict[str, int]:
        """Load edge visit counts from recovery_state_json.

        Delegates to RecoveryManager.
        """
        return RecoveryManager.load_visit_counts(pipeline)
