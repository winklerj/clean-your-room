"""Lease/heartbeat ownership for pipelines, stages, sessions, and tasks.

Provides atomic acquire/release/renew operations at each ownership level.
The DB lease state is the source of truth; in-memory caches are convenience.

Invariant enforced: RunningImpliesOwner — any running pipeline, stage,
session, or task claim must have a non-null owner token and an unexpired
lease.  If not, startup reconciliation must recover or downgrade it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg_pool import AsyncConnectionPool

from build_your_room.config import (
    PIPELINE_HEARTBEAT_INTERVAL_SEC,
    PIPELINE_LEASE_TTL_SEC,
)

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return the current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


class LeaseError(RuntimeError):
    """Raised when a lease operation fails (e.g. contested acquire)."""


class LeaseManager:
    """Durable lease management for pipelines, stages, and sessions.

    Each level (pipeline → stage → session) shares the same TTL and
    heartbeat interval.  Task claim leases remain in HTNPlanner because
    they follow a different atomic-CTE protocol (SKIP LOCKED).
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        lease_ttl_sec: int = PIPELINE_LEASE_TTL_SEC,
        heartbeat_interval_sec: int = PIPELINE_HEARTBEAT_INTERVAL_SEC,
    ) -> None:
        self._pool = pool
        self._lease_ttl_sec = lease_ttl_sec
        self._heartbeat_interval_sec = heartbeat_interval_sec

    @property
    def lease_ttl_sec(self) -> int:
        return self._lease_ttl_sec

    @property
    def heartbeat_interval_sec(self) -> int:
        return self._heartbeat_interval_sec

    # ------------------------------------------------------------------
    # Pipeline leases
    # ------------------------------------------------------------------

    async def acquire_pipeline_lease(self, pipeline_id: int) -> str:
        """Atomically acquire the pipeline lease.

        Returns the new owner_token.  Raises LeaseError if another
        process holds an active (non-expired) lease.
        """
        owner_token = str(uuid.uuid4())
        now = _utc_now()
        expires = now + timedelta(seconds=self._lease_ttl_sec)

        async with self._pool.connection() as conn:
            result: dict[str, Any] | None = await (  # type: ignore[assignment]
                await conn.execute(
                    "UPDATE pipelines SET owner_token = %s, last_heartbeat_at = %s, "
                    "lease_expires_at = %s, updated_at = now() "
                    "WHERE id = %s AND (owner_token IS NULL OR lease_expires_at < %s) "
                    "RETURNING id",
                    (owner_token, now, expires, pipeline_id, now),
                )
            ).fetchone()
            await conn.commit()

        if not result:
            raise LeaseError(
                f"Failed to acquire lease for pipeline {pipeline_id} — "
                "another owner holds an active lease"
            )

        logger.info(
            "Acquired lease for pipeline %d (token=%s)", pipeline_id, owner_token[:8]
        )
        return owner_token

    async def release_pipeline_lease(self, pipeline_id: int) -> None:
        """Release the pipeline lease, clearing owner and expiry."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET owner_token = NULL, "
                "lease_expires_at = NULL, updated_at = now() "
                "WHERE id = %s",
                (pipeline_id,),
            )
            await conn.commit()

    async def renew_pipeline_lease(
        self, pipeline_id: int, owner_token: str
    ) -> bool:
        """Renew the pipeline lease for the given owner.

        Returns True if the lease was renewed, False if the owner no
        longer holds the lease (lost to expiry or stolen).
        """
        now = _utc_now()
        expires = now + timedelta(seconds=self._lease_ttl_sec)

        async with self._pool.connection() as conn:
            result: dict[str, Any] | None = await (  # type: ignore[assignment]
                await conn.execute(
                    "UPDATE pipelines SET last_heartbeat_at = %s, "
                    "lease_expires_at = %s, updated_at = now() "
                    "WHERE id = %s AND owner_token = %s "
                    "RETURNING id",
                    (now, expires, pipeline_id, owner_token),
                )
            ).fetchone()
            await conn.commit()

        return result is not None

    # ------------------------------------------------------------------
    # Stage leases
    # ------------------------------------------------------------------

    async def acquire_stage_lease(
        self, stage_id: int, owner_token: str
    ) -> None:
        """Mark a stage as owned by the given pipeline owner token."""
        now = _utc_now()
        expires = now + timedelta(seconds=self._lease_ttl_sec)

        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pipeline_stages SET owner_token = %s, "
                "last_heartbeat_at = %s, lease_expires_at = %s "
                "WHERE id = %s",
                (owner_token, now, expires, stage_id),
            )
            await conn.commit()

    async def release_stage_lease(self, stage_id: int) -> None:
        """Release the stage lease, clearing owner and expiry."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pipeline_stages SET owner_token = NULL, "
                "last_heartbeat_at = NULL, lease_expires_at = NULL "
                "WHERE id = %s",
                (stage_id,),
            )
            await conn.commit()

    # ------------------------------------------------------------------
    # Session leases
    # ------------------------------------------------------------------

    async def acquire_session_lease(
        self, session_id: int, owner_token: str
    ) -> None:
        """Mark a session as owned by the given pipeline owner token."""
        now = _utc_now()
        expires = now + timedelta(seconds=self._lease_ttl_sec)

        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE agent_sessions SET owner_token = %s, "
                "last_heartbeat_at = %s, lease_expires_at = %s "
                "WHERE id = %s",
                (owner_token, now, expires, session_id),
            )
            await conn.commit()

    async def release_session_lease(self, session_id: int) -> None:
        """Release the session lease, clearing owner and expiry."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE agent_sessions SET owner_token = NULL, "
                "last_heartbeat_at = NULL, lease_expires_at = NULL "
                "WHERE id = %s",
                (session_id,),
            )
            await conn.commit()

    # ------------------------------------------------------------------
    # Multi-level renewal
    # ------------------------------------------------------------------

    async def renew_leases(
        self,
        pipeline_id: int,
        stage_id: int | None = None,
        session_id: int | None = None,
    ) -> None:
        """Renew leases for pipeline and optionally stage/session.

        Called by stage runners during long-running operations to keep
        all active ownership levels alive.
        """
        now = _utc_now()
        expires = now + timedelta(seconds=self._lease_ttl_sec)

        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET last_heartbeat_at = %s, "
                "lease_expires_at = %s, updated_at = now() WHERE id = %s",
                (now, expires, pipeline_id),
            )
            if stage_id is not None:
                await conn.execute(
                    "UPDATE pipeline_stages SET last_heartbeat_at = %s, "
                    "lease_expires_at = %s WHERE id = %s",
                    (now, expires, stage_id),
                )
            if session_id is not None:
                await conn.execute(
                    "UPDATE agent_sessions SET last_heartbeat_at = %s, "
                    "lease_expires_at = %s WHERE id = %s",
                    (now, expires, session_id),
                )
            await conn.commit()

    # ------------------------------------------------------------------
    # Heartbeat loop
    # ------------------------------------------------------------------

    async def heartbeat_loop(
        self,
        pipeline_id: int,
        owner_token: str,
        cancel_event: asyncio.Event,
    ) -> None:
        """Periodically renew the pipeline lease until cancelled.

        Sets the cancel_event if the lease is lost (heartbeat fails).
        """
        while not cancel_event.is_set():
            try:
                await asyncio.sleep(self._heartbeat_interval_sec)
            except asyncio.CancelledError:
                return

            if cancel_event.is_set():
                return

            renewed = await self.renew_pipeline_lease(pipeline_id, owner_token)
            if not renewed:
                logger.error(
                    "Heartbeat failed for pipeline %d — lease lost", pipeline_id
                )
                cancel_event.set()
                return

    # ------------------------------------------------------------------
    # Expiry queries (used by recovery)
    # ------------------------------------------------------------------

    async def is_lease_expired(self, pipeline_id: int) -> bool:
        """Check whether the given pipeline's lease has expired."""
        now = _utc_now()
        async with self._pool.connection() as conn:
            row: dict[str, Any] | None = await (  # type: ignore[assignment]
                await conn.execute(
                    "SELECT owner_token, lease_expires_at "
                    "FROM pipelines WHERE id = %s",
                    (pipeline_id,),
                )
            ).fetchone()

        if not row:
            return True
        if row["owner_token"] is None:
            return True
        if row["lease_expires_at"] is None:
            return True
        return row["lease_expires_at"] <= now

    async def get_expired_running_pipelines(self) -> list[dict[str, Any]]:
        """Return all running pipelines with expired or missing leases.

        Used by startup reconciliation to find pipelines that need
        recovery or downgrade.
        """
        now = _utc_now()
        async with self._pool.connection() as conn:
            rows: list[dict[str, Any]] = await (  # type: ignore[assignment]
                await conn.execute(
                    "SELECT id, clone_path, head_rev, review_base_rev, "
                    "workspace_state, owner_token, lease_expires_at "
                    "FROM pipelines WHERE status = 'running' "
                    "AND (lease_expires_at IS NULL OR lease_expires_at <= %s "
                    "     OR owner_token IS NULL)",
                    (now,),
                )
            ).fetchall()
        return rows

    async def get_live_running_pipelines(self) -> list[dict[str, Any]]:
        """Return all running pipelines with still-valid leases.

        Used by startup reconciliation to identify pipelines to skip.
        """
        now = _utc_now()
        async with self._pool.connection() as conn:
            rows: list[dict[str, Any]] = await (  # type: ignore[assignment]
                await conn.execute(
                    "SELECT id, owner_token, lease_expires_at "
                    "FROM pipelines WHERE status = 'running' "
                    "AND owner_token IS NOT NULL "
                    "AND lease_expires_at > %s",
                    (now,),
                )
            ).fetchall()
        return rows

    async def release_all_for_pipeline(self, pipeline_id: int) -> None:
        """Release all leases (stages + sessions) belonging to a pipeline.

        Used during cancellation, kill, and recovery to ensure clean state.
        """
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pipeline_stages SET owner_token = NULL, "
                "last_heartbeat_at = NULL, lease_expires_at = NULL "
                "WHERE pipeline_id = %s AND owner_token IS NOT NULL",
                (pipeline_id,),
            )
            await conn.execute(
                "UPDATE agent_sessions SET owner_token = NULL, "
                "last_heartbeat_at = NULL, lease_expires_at = NULL "
                "WHERE pipeline_stage_id IN ("
                "  SELECT id FROM pipeline_stages WHERE pipeline_id = %s"
                ") AND owner_token IS NOT NULL",
                (pipeline_id,),
            )
            await conn.commit()
