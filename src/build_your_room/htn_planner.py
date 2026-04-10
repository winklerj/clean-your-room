"""HTN (Hierarchical Task Network) Planner.

Pure data-layer component that manages the task graph lifecycle:
task population from structured output, atomic claiming, readiness
propagation, postcondition verification, decision escalations, and
dashboard queries.  No agent interaction — just graph operations
backed by PostgreSQL.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Sequence

from psycopg_pool import AsyncConnectionPool

from build_your_room.command_registry import (
    CommandRegistry,
    ConditionResult,
    VerifierRegistry,
    get_default_command_registry,
    get_default_verifier_registry,
    verify_condition,
)
from build_your_room.models import HtnTask, HtnTaskDep

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TASK_COLUMNS = (
    "id, pipeline_id, parent_task_id, name, description, task_type, status, "
    "priority, ordering, assigned_session_id, claim_token, claim_owner_token, "
    "claim_expires_at, preconditions_json, postconditions_json, invariants_json, "
    "output_artifacts_json, checkpoint_rev, estimated_complexity, diary_entry, "
    "created_at, started_at, completed_at"
)

# Table-qualified version for queries where column names are ambiguous (e.g. CTE joins)
_TASK_COLUMNS_QUALIFIED = (
    "t.id, t.pipeline_id, t.parent_task_id, t.name, t.description, t.task_type, t.status, "
    "t.priority, t.ordering, t.assigned_session_id, t.claim_token, t.claim_owner_token, "
    "t.claim_expires_at, t.preconditions_json, t.postconditions_json, t.invariants_json, "
    "t.output_artifacts_json, t.checkpoint_rev, t.estimated_complexity, t.diary_entry, "
    "t.created_at, t.started_at, t.completed_at"
)


def _row_to_htn_task(row: dict[str, Any]) -> HtnTask:
    """Convert a dict_row from PostgreSQL to an HtnTask dataclass."""
    return HtnTask(
        id=row["id"],
        pipeline_id=row["pipeline_id"],
        parent_task_id=row["parent_task_id"],
        name=row["name"],
        description=row["description"],
        task_type=row["task_type"],
        status=row["status"],
        priority=row["priority"],
        ordering=row["ordering"],
        assigned_session_id=row["assigned_session_id"],
        claim_token=row["claim_token"],
        claim_owner_token=row["claim_owner_token"],
        claim_expires_at=row["claim_expires_at"],
        preconditions_json=row["preconditions_json"],
        postconditions_json=row["postconditions_json"],
        invariants_json=row["invariants_json"],
        output_artifacts_json=row["output_artifacts_json"],
        checkpoint_rev=row["checkpoint_rev"],
        estimated_complexity=row["estimated_complexity"],
        diary_entry=row["diary_entry"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def _row_to_htn_task_dep(row: dict[str, Any]) -> HtnTaskDep:
    return HtnTaskDep(
        id=row["id"],
        task_id=row["task_id"],
        depends_on_task_id=row["depends_on_task_id"],
        dep_type=row["dep_type"],
    )


# ---------------------------------------------------------------------------
# HTNPlanner
# ---------------------------------------------------------------------------


class HTNPlanner:
    """Manages the HTN task graph lifecycle.

    All methods operate on the PostgreSQL connection pool and do not
    interact with agents directly.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        command_registry: CommandRegistry | None = None,
        verifier_registry: VerifierRegistry | None = None,
    ) -> None:
        self._pool = pool
        self._cmd_reg = command_registry or get_default_command_registry()
        self._ver_reg = verifier_registry or get_default_verifier_registry()

    # ------------------------------------------------------------------
    # populate_from_structured_output
    # ------------------------------------------------------------------

    async def populate_from_structured_output(
        self, pipeline_id: int, tasks_json: list[dict[str, Any]]
    ) -> list[int]:
        """Parse an agent's task decomposition and populate htn_tasks + htn_task_deps.

        ``tasks_json`` is a flat list of task dicts, each with:
        - name (str, required)
        - description (str, required)
        - task_type: 'compound' | 'primitive' | 'decision' (default 'primitive')
        - parent_name (str | None): name of parent task
        - priority (int, default 0)
        - ordering (int, default 0)
        - preconditions (list[dict]): condition objects
        - postconditions (list[dict]): condition objects
        - invariants (list[dict] | None): invariant checks
        - estimated_complexity (str | None)
        - dependencies (list[str]): names of tasks this depends on (hard deps)

        Returns the list of created task IDs.
        """
        created_ids: list[int] = []
        # Map task name → DB id for resolving parent_name and dependencies
        name_to_id: dict[str, int] = {}

        async with self._pool.connection() as conn:
            async with conn.transaction():
                # First pass: insert all tasks (without deps)
                for task_dict in tasks_json:
                    name = task_dict["name"]
                    description = task_dict["description"]
                    task_type = task_dict.get("task_type", "primitive")
                    priority = task_dict.get("priority", 0)
                    ordering = task_dict.get("ordering", 0)
                    preconditions = json.dumps(task_dict.get("preconditions", []))
                    postconditions = json.dumps(task_dict.get("postconditions", []))
                    invariants = (
                        json.dumps(task_dict["invariants"])
                        if task_dict.get("invariants") is not None
                        else None
                    )
                    estimated_complexity = task_dict.get("estimated_complexity")

                    # Resolve parent
                    parent_name = task_dict.get("parent_name")
                    parent_task_id = name_to_id.get(parent_name) if parent_name else None

                    # Root primitives with no hard deps start as 'ready'
                    initial_status = "not_ready"

                    cur = await conn.execute(
                        "INSERT INTO htn_tasks "
                        "(pipeline_id, parent_task_id, name, description, task_type, "
                        "status, priority, ordering, preconditions_json, "
                        "postconditions_json, invariants_json, estimated_complexity) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "RETURNING id",
                        (
                            pipeline_id,
                            parent_task_id,
                            name,
                            description,
                            task_type,
                            initial_status,
                            priority,
                            ordering,
                            preconditions,
                            postconditions,
                            invariants,
                            estimated_complexity,
                        ),
                    )
                    row = await cur.fetchone()
                    task_id: int = row["id"]  # type: ignore
                    name_to_id[name] = task_id
                    created_ids.append(task_id)

                # Second pass: insert dependency edges
                for task_dict in tasks_json:
                    dep_names = task_dict.get("dependencies", [])
                    task_id = name_to_id[task_dict["name"]]
                    for dep_name in dep_names:
                        dep_id = name_to_id.get(dep_name)
                        if dep_id is not None:
                            await conn.execute(
                                "INSERT INTO htn_task_deps (task_id, depends_on_task_id, dep_type) "
                                "VALUES (%s, %s, 'hard')",
                                (task_id, dep_id),
                            )

                # Third pass: compute initial readiness
                for task_id in created_ids:
                    await self._recompute_readiness(conn, task_id)

        return created_ids

    # ------------------------------------------------------------------
    # claim_next_ready_task
    # ------------------------------------------------------------------

    async def claim_next_ready_task(
        self,
        pipeline_id: int,
        session_id: int,
        owner_token: str,
        claim_expires_at: str,
    ) -> HtnTask | None:
        """Atomically claim the highest-priority ready primitive task.

        Uses ``FOR UPDATE SKIP LOCKED`` so concurrent Agent Teams
        workers cannot race each other.  Returns the claimed task or
        ``None`` if no ready task is available.
        """
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "WITH candidate AS ("
                    "  SELECT id FROM htn_tasks "
                    "  WHERE pipeline_id = %s AND status = 'ready' "
                    "    AND task_type = 'primitive' "
                    "  ORDER BY priority DESC, ordering ASC "
                    "  FOR UPDATE SKIP LOCKED "
                    "  LIMIT 1"
                    ") "
                    "UPDATE htn_tasks AS t "
                    "SET status = 'in_progress', "
                    "    assigned_session_id = %s, "
                    "    claim_token = %s, "
                    "    claim_owner_token = %s, "
                    "    claim_expires_at = %s, "
                    "    started_at = now() "
                    "FROM candidate "
                    "WHERE t.id = candidate.id "
                    f"RETURNING {_TASK_COLUMNS_QUALIFIED}",
                    (pipeline_id, session_id, owner_token, owner_token, claim_expires_at),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                return _row_to_htn_task(row)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # release_claim
    # ------------------------------------------------------------------

    async def release_claim(self, task_id: int) -> None:
        """Release a claim on a task, returning it to 'ready' status."""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE htn_tasks SET status = 'ready', "
                    "assigned_session_id = NULL, claim_token = NULL, "
                    "claim_owner_token = NULL, claim_expires_at = NULL "
                    "WHERE id = %s AND status = 'in_progress'",
                    (task_id,),
                )

    # ------------------------------------------------------------------
    # reassign_claim
    # ------------------------------------------------------------------

    async def reassign_claim(
        self, task_id: int, new_session_id: int
    ) -> None:
        """Reassign a claimed task to a new session (context rotation)."""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE htn_tasks SET assigned_session_id = %s "
                    "WHERE id = %s AND status = 'in_progress'",
                    (new_session_id, task_id),
                )

    # ------------------------------------------------------------------
    # verify_postconditions
    # ------------------------------------------------------------------

    async def verify_postconditions(
        self,
        task_id: int,
        working_dir: str,
        *,
        allowed_roots: Sequence[Path] | None = None,
    ) -> list[ConditionResult]:
        """Run postcondition checks for a task.

        Returns a list of :class:`ConditionResult` — one per postcondition.
        The ``task_completed`` condition type is resolved against the DB.
        """
        task = await self._get_task(task_id)
        if task is None:
            return [
                ConditionResult(
                    condition_type="error",
                    description="Task not found",
                    passed=False,
                    detail=f"No task with id={task_id}",
                )
            ]

        conditions: list[dict[str, Any]] = json.loads(task.postconditions_json)
        if not conditions:
            return []

        pipeline_id = task.pipeline_id

        def task_status_lookup(task_name: str) -> bool:
            # NOTE: this is synchronous — verify_condition calls it synchronously.
            # We pre-load completed task names before entering the loop.
            return task_name in completed_names

        # Pre-load completed task names for this pipeline
        completed_names = await self._get_completed_task_names(pipeline_id)

        results: list[ConditionResult] = []
        for cond in conditions:
            result = await verify_condition(
                cond,
                working_dir,
                command_registry=self._cmd_reg,
                verifier_registry=self._ver_reg,
                allowed_roots=allowed_roots,
                task_status_lookup=task_status_lookup,
            )
            results.append(result)

        return results

    # ------------------------------------------------------------------
    # complete_task
    # ------------------------------------------------------------------

    async def complete_task(
        self,
        task_id: int,
        checkpoint_rev: str | None,
        diary: str,
    ) -> list[int]:
        """Mark a task completed, propagate readiness, return newly-ready task IDs."""
        newly_ready: list[int] = []

        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE htn_tasks SET status = 'completed', "
                    "checkpoint_rev = %s, diary_entry = %s, "
                    "completed_at = now(), "
                    "claim_token = NULL, claim_owner_token = NULL, "
                    "claim_expires_at = NULL "
                    "WHERE id = %s",
                    (checkpoint_rev, diary, task_id),
                )

                # Find tasks that depend on the completed task
                cur = await conn.execute(
                    "SELECT task_id FROM htn_task_deps WHERE depends_on_task_id = %s",
                    (task_id,),
                )
                dependent_rows = await cur.fetchall()

                for dep_row in dependent_rows:
                    dep_task_id: int = dep_row["task_id"]  # type: ignore
                    became_ready = await self._recompute_readiness(conn, dep_task_id)
                    if became_ready:
                        newly_ready.append(dep_task_id)

                # Also check children of compound parent if this task's parent exists
                cur = await conn.execute(
                    "SELECT parent_task_id FROM htn_tasks WHERE id = %s",
                    (task_id,),
                )
                parent_row = await cur.fetchone()
                if parent_row and parent_row["parent_task_id"] is not None:  # type: ignore
                    # Check if all siblings are completed → auto-complete the parent
                    parent_id: int = parent_row["parent_task_id"]  # type: ignore
                    await self._maybe_complete_compound(conn, parent_id)

        return newly_ready

    # ------------------------------------------------------------------
    # fail_task
    # ------------------------------------------------------------------

    async def fail_task(self, task_id: int, reason: str) -> None:
        """Mark a task failed and block dependents."""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE htn_tasks SET status = 'failed', "
                    "diary_entry = %s, completed_at = now(), "
                    "claim_token = NULL, claim_owner_token = NULL, "
                    "claim_expires_at = NULL "
                    "WHERE id = %s",
                    (reason, task_id),
                )

                # Block all tasks that hard-depend on this one
                cur = await conn.execute(
                    "SELECT task_id FROM htn_task_deps "
                    "WHERE depends_on_task_id = %s AND dep_type = 'hard'",
                    (task_id,),
                )
                dep_rows = await cur.fetchall()
                for dep_row in dep_rows:
                    dep_id: int = dep_row["task_id"]  # type: ignore
                    await conn.execute(
                        "UPDATE htn_tasks SET status = 'blocked' "
                        "WHERE id = %s AND status IN ('not_ready', 'ready')",
                        (dep_id,),
                    )

    # ------------------------------------------------------------------
    # create_decision_escalation
    # ------------------------------------------------------------------

    async def create_decision_escalation(
        self, task_id: int, pipeline_id: int, description: str
    ) -> int:
        """Create an escalation for a decision-type task. Returns escalation ID."""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                # Get the pipeline_stage_id if available
                cur = await conn.execute(
                    "SELECT assigned_session_id FROM htn_tasks WHERE id = %s",
                    (task_id,),
                )
                task_row = await cur.fetchone()
                stage_id = None
                if task_row and task_row["assigned_session_id"]:  # type: ignore
                    stage_cur = await conn.execute(
                        "SELECT pipeline_stage_id FROM agent_sessions WHERE id = %s",
                        (task_row["assigned_session_id"],),  # type: ignore
                    )
                    stage_row = await stage_cur.fetchone()
                    if stage_row:
                        stage_id = stage_row["pipeline_stage_id"]  # type: ignore

                context = json.dumps({
                    "task_id": task_id,
                    "description": description,
                })
                cur = await conn.execute(
                    "INSERT INTO escalations "
                    "(pipeline_id, pipeline_stage_id, reason, context_json, status) "
                    "VALUES (%s, %s, 'design_decision', %s, 'open') "
                    "RETURNING id",
                    (pipeline_id, stage_id, context),
                )
                row = await cur.fetchone()
                escalation_id: int = row["id"]  # type: ignore

                # Mark the task as blocked
                await conn.execute(
                    "UPDATE htn_tasks SET status = 'blocked' WHERE id = %s",
                    (task_id,),
                )

                return escalation_id

    # ------------------------------------------------------------------
    # resolve_decision
    # ------------------------------------------------------------------

    async def resolve_decision(
        self, task_id: int, resolution: str
    ) -> list[int]:
        """Resolve a decision task with the human's answer. Unblock dependents."""
        newly_ready: list[int] = []

        async with self._pool.connection() as conn:
            async with conn.transaction():
                # Complete the decision task
                await conn.execute(
                    "UPDATE htn_tasks SET status = 'completed', "
                    "diary_entry = %s, completed_at = now() "
                    "WHERE id = %s",
                    (resolution, task_id),
                )

                # Resolve the escalation
                await conn.execute(
                    "UPDATE escalations SET status = 'resolved', "
                    "resolution = %s, resolved_at = now() "
                    "WHERE context_json::jsonb @> %s::jsonb AND status = 'open'",
                    (resolution, json.dumps({"task_id": task_id})),
                )

                # Propagate readiness to dependents
                cur = await conn.execute(
                    "SELECT task_id FROM htn_task_deps WHERE depends_on_task_id = %s",
                    (task_id,),
                )
                dep_rows = await cur.fetchall()
                for dep_row in dep_rows:
                    dep_task_id: int = dep_row["task_id"]  # type: ignore
                    became_ready = await self._recompute_readiness(conn, dep_task_id)
                    if became_ready:
                        newly_ready.append(dep_task_id)

        return newly_ready

    # ------------------------------------------------------------------
    # get_task_tree
    # ------------------------------------------------------------------

    async def get_task_tree(self, pipeline_id: int) -> list[HtnTask]:
        """Return full task tree for dashboard visualization.

        Tasks are ordered by parent_task_id (NULLs first) then ordering.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM htn_tasks "
                "WHERE pipeline_id = %s "
                "ORDER BY parent_task_id NULLS FIRST, ordering ASC, id ASC",
                (pipeline_id,),
            )
            rows = await cur.fetchall()
            return [_row_to_htn_task(row) for row in rows]  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # get_task_deps
    # ------------------------------------------------------------------

    async def get_task_deps(self, pipeline_id: int) -> list[HtnTaskDep]:
        """Return all dependency edges for a pipeline's tasks."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT d.id, d.task_id, d.depends_on_task_id, d.dep_type "
                "FROM htn_task_deps d "
                "JOIN htn_tasks t ON d.task_id = t.id "
                "WHERE t.pipeline_id = %s",
                (pipeline_id,),
            )
            rows = await cur.fetchall()
            return [_row_to_htn_task_dep(row) for row in rows]  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # get_progress_summary
    # ------------------------------------------------------------------

    async def get_progress_summary(self, pipeline_id: int) -> dict[str, int]:
        """Return counts by status for dashboard cards."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM htn_tasks "
                "WHERE pipeline_id = %s GROUP BY status",
                (pipeline_id,),
            )
            rows = await cur.fetchall()
            summary: dict[str, int] = {}
            for row in rows:
                summary[row["status"]] = row["cnt"]  # type: ignore
            return summary

    # ------------------------------------------------------------------
    # sync_to_markdown
    # ------------------------------------------------------------------

    async def sync_to_markdown(
        self, pipeline_id: int, working_dir: str
    ) -> None:
        """Write the current task state to specs/task-list.md for agent readability."""
        tasks = await self.get_task_tree(pipeline_id)
        deps = await self.get_task_deps(pipeline_id)

        # Build dep map: task_id → list of dep names
        task_id_to_name = {t.id: t.name for t in tasks}
        dep_map: dict[int, list[str]] = {}
        for d in deps:
            dep_map.setdefault(d.task_id, []).append(
                task_id_to_name.get(d.depends_on_task_id, f"task-{d.depends_on_task_id}")
            )

        status_icons = {
            "completed": "[x]",
            "in_progress": "[-]",
            "ready": "[ ]",
            "not_ready": "[ ]",
            "blocked": "[!]",
            "failed": "[F]",
            "skipped": "[~]",
        }

        lines: list[str] = ["# Task List\n"]

        # Group by parent
        roots = [t for t in tasks if t.parent_task_id is None]
        children_map: dict[int, list[HtnTask]] = {}
        for t in tasks:
            if t.parent_task_id is not None:
                children_map.setdefault(t.parent_task_id, []).append(t)

        for root in roots:
            icon = status_icons.get(root.status, "[ ]")
            lines.append(f"- {icon} **{root.name}** ({root.status})")
            if root.description:
                lines.append(f"  {root.description}")
            task_deps = dep_map.get(root.id, [])
            if task_deps:
                lines.append(f"  Depends on: {', '.join(task_deps)}")

            for child in children_map.get(root.id, []):
                c_icon = status_icons.get(child.status, "[ ]")
                lines.append(f"  - {c_icon} **{child.name}** ({child.status})")
                if child.description:
                    lines.append(f"    {child.description}")
                child_deps = dep_map.get(child.id, [])
                if child_deps:
                    lines.append(f"    Depends on: {', '.join(child_deps)}")

                # Render grandchildren (two levels deep is enough for readability)
                for grandchild in children_map.get(child.id, []):
                    gc_icon = status_icons.get(grandchild.status, "[ ]")
                    lines.append(f"    - {gc_icon} **{grandchild.name}** ({grandchild.status})")

        lines.append("")

        specs_dir = Path(working_dir) / "specs"
        specs_dir.mkdir(parents=True, exist_ok=True)
        md_path = specs_dir / "task-list.md"
        md_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Synced task list to %s", md_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_task(self, task_id: int) -> HtnTask | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM htn_tasks WHERE id = %s",
                (task_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return _row_to_htn_task(row)  # type: ignore[arg-type]

    async def _get_completed_task_names(self, pipeline_id: int) -> set[str]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT name FROM htn_tasks "
                "WHERE pipeline_id = %s AND status = 'completed'",
                (pipeline_id,),
            )
            rows = await cur.fetchall()
            return {row["name"] for row in rows}  # type: ignore

    async def _recompute_readiness(
        self, conn: Any, task_id: int
    ) -> bool:
        """Recompute whether a task should be 'ready'.

        A task becomes ready when:
        1. It is 'not_ready' (not already in progress, completed, etc.)
        2. It is a primitive or decision type (compounds are not directly executable)
        3. All hard dependencies are completed
        4. Its parent (if any) is not failed/blocked

        Returns True if the task transitioned to 'ready'.
        """
        cur = await conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM htn_tasks WHERE id = %s",
            (task_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return False

        task = _row_to_htn_task(row)  # type: ignore[arg-type]

        # Only transition from not_ready
        if task.status != "not_ready":
            return False

        # Compound tasks don't become ready themselves
        if task.task_type == "compound":
            return False

        # Check parent is not blocked/failed
        if task.parent_task_id is not None:
            pcur = await conn.execute(
                "SELECT status FROM htn_tasks WHERE id = %s",
                (task.parent_task_id,),
            )
            parent = await pcur.fetchone()
            if parent and parent["status"] in ("failed", "blocked"):  # type: ignore
                return False

        # Check all hard deps are completed
        dep_cur = await conn.execute(
            "SELECT d.depends_on_task_id, t.status "
            "FROM htn_task_deps d "
            "JOIN htn_tasks t ON d.depends_on_task_id = t.id "
            "WHERE d.task_id = %s AND d.dep_type = 'hard'",
            (task_id,),
        )
        dep_rows = await dep_cur.fetchall()
        for dep in dep_rows:
            if dep["status"] != "completed":  # type: ignore
                return False

        # All checks passed — mark as ready
        await conn.execute(
            "UPDATE htn_tasks SET status = 'ready' WHERE id = %s",
            (task_id,),
        )
        return True

    async def _maybe_complete_compound(
        self, conn: Any, parent_id: int
    ) -> None:
        """Auto-complete a compound parent if all its children are completed."""
        cur = await conn.execute(
            "SELECT status FROM htn_tasks WHERE parent_task_id = %s",
            (parent_id,),
        )
        children = await cur.fetchall()
        if not children:
            return

        all_completed = all(c["status"] == "completed" for c in children)  # type: ignore
        if all_completed:
            await conn.execute(
                "UPDATE htn_tasks SET status = 'completed', completed_at = now() "
                "WHERE id = %s AND task_type = 'compound'",
                (parent_id,),
            )
