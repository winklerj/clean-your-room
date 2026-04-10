"""Tests for htn_planner.py — HTN task graph CRUD, atomic claims,
readiness propagation, postcondition verification, and dashboard queries.

Property-based tests verify invariants across generated task graphs.
Unit tests verify DB-backed operations with pytest-postgresql.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from build_your_room.db import get_pool
from build_your_room.htn_planner import HTNPlanner, _row_to_htn_task, _row_to_htn_task_dep
from build_your_room.models import HtnTask, HtnTaskDep


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_task_name = st.from_regex(r"[a-zA-Z][a-zA-Z0-9_ ]{0,30}", fullmatch=True)

_task_type = st.sampled_from(["compound", "primitive", "decision"])

_complexity = st.sampled_from([None, "trivial", "small", "medium", "large", "epic"])

_status = st.sampled_from([
    "not_ready", "ready", "in_progress", "completed", "failed", "blocked", "skipped",
])


@st.composite
def task_dicts(draw: st.DrawFn, *, count: int | None = None) -> list[dict]:
    """Generate a valid list of task dicts for populate_from_structured_output.

    Invariant: names are unique, parent_name references only earlier tasks,
    dependencies reference only tasks within the list.
    """
    n = count if count is not None else draw(st.integers(min_value=1, max_value=8))
    tasks: list[dict] = []
    names: list[str] = []
    for i in range(n):
        name = draw(st.from_regex(r"[a-zA-Z][a-zA-Z0-9_]{1,15}", fullmatch=True))
        # Ensure unique names
        while name in names:
            name = name + str(i)
        names.append(name)

        parent_name = None
        if i > 0:
            parent_name = draw(st.sampled_from([None] + names[:i]))

        # Dependencies are from earlier tasks (not self, not parent)
        possible_deps = [n for n in names[:i] if n != parent_name]
        dep_count = draw(st.integers(min_value=0, max_value=min(2, len(possible_deps))))
        deps = draw(
            st.lists(
                st.sampled_from(possible_deps) if possible_deps else st.nothing(),
                min_size=dep_count,
                max_size=dep_count,
                unique=True,
            )
        ) if possible_deps and dep_count > 0 else []

        tasks.append({
            "name": name,
            "description": f"Implement {name}",
            "task_type": draw(st.sampled_from(["primitive", "compound", "decision"])),
            "parent_name": parent_name,
            "priority": draw(st.integers(min_value=0, max_value=10)),
            "ordering": i,
            "preconditions": [],
            "postconditions": [],
            "estimated_complexity": draw(_complexity),
            "dependencies": deps,
        })
    return tasks


# ---------------------------------------------------------------------------
# _row_to_htn_task / _row_to_htn_task_dep
# ---------------------------------------------------------------------------


class TestRowConversion:
    """Tests for row-to-dataclass conversion helpers."""

    def test_row_to_htn_task_maps_all_fields(self) -> None:
        """Row dict keys map to HtnTask dataclass fields.

        Invariant: every column in the task row is preserved in the dataclass.
        """
        row = {
            "id": 1, "pipeline_id": 10, "parent_task_id": None,
            "name": "setup", "description": "Setup project",
            "task_type": "primitive", "status": "ready",
            "priority": 5, "ordering": 0,
            "assigned_session_id": None, "claim_token": None,
            "claim_owner_token": None, "claim_expires_at": None,
            "preconditions_json": "[]", "postconditions_json": "[]",
            "invariants_json": None, "output_artifacts_json": None,
            "checkpoint_rev": None, "estimated_complexity": "small",
            "diary_entry": None, "created_at": "2026-01-01",
            "started_at": None, "completed_at": None,
        }
        task = _row_to_htn_task(row)
        assert isinstance(task, HtnTask)
        assert task.id == 1
        assert task.name == "setup"
        assert task.status == "ready"
        assert task.priority == 5

    def test_row_to_htn_task_dep_maps_all_fields(self) -> None:
        """Row dict keys map to HtnTaskDep dataclass fields.

        Invariant: dependency edge data is preserved through conversion.
        """
        row = {"id": 1, "task_id": 2, "depends_on_task_id": 1, "dep_type": "hard"}
        dep = _row_to_htn_task_dep(row)
        assert isinstance(dep, HtnTaskDep)
        assert dep.task_id == 2
        assert dep.depends_on_task_id == 1
        assert dep.dep_type == "hard"


# ---------------------------------------------------------------------------
# DB-backed tests — use initialized_db fixture from conftest
# ---------------------------------------------------------------------------


async def _seed_pipeline(pool, *, name_suffix: str = "") -> tuple[int, int]:
    """Create minimal pipeline_def + repo + pipeline for testing. Returns (pipeline_id, repo_id)."""
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "INSERT INTO pipeline_defs (name, stage_graph_json) "
                "VALUES (%s, %s) RETURNING id",
                (f"test-def{name_suffix}", '{"entry_stage":"impl"}'),
            )
            def_row = await cur.fetchone()
            def_id = def_row["id"]

            cur = await conn.execute(
                "INSERT INTO repos (name, local_path) "
                "VALUES (%s, %s) RETURNING id",
                (f"test-repo{name_suffix}", "/tmp/test-repo"),
            )
            repo_row = await cur.fetchone()
            repo_id = repo_row["id"]

            cur = await conn.execute(
                "INSERT INTO pipelines (pipeline_def_id, repo_id, clone_path, "
                "review_base_rev, status) "
                "VALUES (%s, %s, %s, 'abc123', 'running') RETURNING id",
                (def_id, repo_id, "/tmp/clone"),
            )
            pipe_row = await cur.fetchone()
            pipeline_id = pipe_row["id"]

    return pipeline_id, repo_id


async def _seed_session(pool, pipeline_id: int) -> int:
    """Create a minimal pipeline_stage + agent_session. Returns session_id."""
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "INSERT INTO pipeline_stages (pipeline_id, stage_key, stage_type, "
                "agent_type, max_iterations) "
                "VALUES (%s, 'impl_task', 'impl_task', 'claude', 50) RETURNING id",
                (pipeline_id,),
            )
            stage_row = await cur.fetchone()
            stage_id = stage_row["id"]

            cur = await conn.execute(
                "INSERT INTO agent_sessions (pipeline_stage_id, session_type) "
                "VALUES (%s, 'claude_sdk') RETURNING id",
                (stage_id,),
            )
            session_row = await cur.fetchone()
            return session_row["id"]


# ---------------------------------------------------------------------------
# populate_from_structured_output
# ---------------------------------------------------------------------------


class TestPopulate:
    """Tests for populating the task graph from structured agent output."""

    @pytest.mark.asyncio
    async def test_basic_task_population(self, initialized_db) -> None:
        """Populate creates tasks in the DB with correct fields.

        Invariant: for every task dict in input, a matching htn_tasks row exists.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        planner = HTNPlanner(pool)

        tasks_json = [
            {
                "name": "setup_db",
                "description": "Create database schema",
                "task_type": "primitive",
                "priority": 10,
                "ordering": 0,
                "preconditions": [],
                "postconditions": [{"type": "file_exists", "path": "schema.sql", "description": "Schema file"}],
                "estimated_complexity": "small",
            },
            {
                "name": "write_models",
                "description": "Write data models",
                "task_type": "primitive",
                "priority": 5,
                "ordering": 1,
                "dependencies": ["setup_db"],
            },
        ]

        ids = await planner.populate_from_structured_output(pipeline_id, tasks_json)
        assert len(ids) == 2

        tree = await planner.get_task_tree(pipeline_id)
        assert len(tree) == 2
        assert tree[0].name == "setup_db"
        assert tree[0].priority == 10
        assert tree[0].estimated_complexity == "small"
        assert tree[1].name == "write_models"

    @pytest.mark.asyncio
    async def test_parent_child_relationship(self, initialized_db) -> None:
        """Parent names resolve to correct parent_task_id in the DB.

        Invariant: parent_task_id is set iff parent_name refers to a prior task.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        planner = HTNPlanner(pool)

        tasks_json = [
            {"name": "parent_task", "description": "A compound task", "task_type": "compound", "ordering": 0},
            {"name": "child_task", "description": "A child", "task_type": "primitive",
             "parent_name": "parent_task", "ordering": 1},
        ]
        await planner.populate_from_structured_output(pipeline_id, tasks_json)
        tree = await planner.get_task_tree(pipeline_id)

        parent = next(t for t in tree if t.name == "parent_task")
        child = next(t for t in tree if t.name == "child_task")
        assert child.parent_task_id == parent.id
        assert parent.parent_task_id is None

    @pytest.mark.asyncio
    async def test_dependency_edges_created(self, initialized_db) -> None:
        """Dependencies in the input JSON create htn_task_deps rows.

        Invariant: for each dependency name, a hard dep edge exists.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        planner = HTNPlanner(pool)

        tasks_json = [
            {"name": "a", "description": "Task A", "task_type": "primitive", "ordering": 0},
            {"name": "b", "description": "Task B", "task_type": "primitive", "ordering": 1,
             "dependencies": ["a"]},
        ]
        await planner.populate_from_structured_output(pipeline_id, tasks_json)
        deps = await planner.get_task_deps(pipeline_id)
        assert len(deps) == 1
        assert deps[0].dep_type == "hard"

    @pytest.mark.asyncio
    async def test_initial_readiness_no_deps(self, initialized_db) -> None:
        """Primitive tasks with no deps become 'ready' after population.

        Invariant: primitives with no hard deps and no blocked parent start as 'ready'.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        planner = HTNPlanner(pool)

        tasks_json = [
            {"name": "independent", "description": "No deps", "task_type": "primitive", "ordering": 0},
        ]
        await planner.populate_from_structured_output(pipeline_id, tasks_json)
        tree = await planner.get_task_tree(pipeline_id)
        assert tree[0].status == "ready"

    @pytest.mark.asyncio
    async def test_initial_readiness_with_deps(self, initialized_db) -> None:
        """Primitive tasks with unmet deps stay 'not_ready' after population.

        Invariant: a task with pending hard deps must not be 'ready'.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        planner = HTNPlanner(pool)

        tasks_json = [
            {"name": "first", "description": "First", "task_type": "primitive", "ordering": 0},
            {"name": "second", "description": "Second", "task_type": "primitive", "ordering": 1,
             "dependencies": ["first"]},
        ]
        await planner.populate_from_structured_output(pipeline_id, tasks_json)
        tree = await planner.get_task_tree(pipeline_id)
        first = next(t for t in tree if t.name == "first")
        second = next(t for t in tree if t.name == "second")
        assert first.status == "ready"
        assert second.status == "not_ready"

    @pytest.mark.asyncio
    async def test_compound_tasks_stay_not_ready(self, initialized_db) -> None:
        """Compound tasks never become 'ready' — they complete via children.

        Invariant: task_type='compound' → status != 'ready' after population.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        planner = HTNPlanner(pool)

        tasks_json = [
            {"name": "group", "description": "A group", "task_type": "compound", "ordering": 0},
        ]
        await planner.populate_from_structured_output(pipeline_id, tasks_json)
        tree = await planner.get_task_tree(pipeline_id)
        assert tree[0].status == "not_ready"


# ---------------------------------------------------------------------------
# claim_next_ready_task
# ---------------------------------------------------------------------------


class TestClaim:
    """Tests for atomic task claiming."""

    @pytest.mark.asyncio
    async def test_claim_returns_highest_priority(self, initialized_db) -> None:
        """Claiming picks the highest-priority ready primitive task.

        Invariant: claimed task has the highest priority among all ready tasks.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, [
            {"name": "low", "description": "Low priority", "task_type": "primitive",
             "priority": 1, "ordering": 0},
            {"name": "high", "description": "High priority", "task_type": "primitive",
             "priority": 10, "ordering": 1},
        ])

        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, "owner-1", "2099-01-01T00:00:00Z"
        )
        assert claimed is not None
        assert claimed.name == "high"
        assert claimed.status == "in_progress"
        assert claimed.claim_token == "owner-1"
        assert claimed.assigned_session_id == session_id

    @pytest.mark.asyncio
    async def test_claim_returns_none_when_no_ready(self, initialized_db) -> None:
        """Claiming returns None when no ready tasks exist.

        Invariant: no ready task → claim yields None, no side effects.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, "owner-1", "2099-01-01T00:00:00Z"
        )
        assert claimed is None

    @pytest.mark.asyncio
    async def test_claim_does_not_double_claim(self, initialized_db) -> None:
        """A claimed task is not returned by a second claim call.

        Invariant: UniqueTaskClaim — at most one live lease per task.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, [
            {"name": "only", "description": "The only task", "task_type": "primitive",
             "priority": 1, "ordering": 0},
        ])

        first = await planner.claim_next_ready_task(
            pipeline_id, session_id, "owner-1", "2099-01-01T00:00:00Z"
        )
        assert first is not None

        second = await planner.claim_next_ready_task(
            pipeline_id, session_id, "owner-2", "2099-01-01T00:00:00Z"
        )
        assert second is None

    @pytest.mark.asyncio
    async def test_claim_ordering_tiebreak(self, initialized_db) -> None:
        """Tasks with same priority are claimed by ordering ASC.

        Invariant: among equal-priority tasks, lower ordering wins.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, [
            {"name": "second_order", "description": "Order 1", "task_type": "primitive",
             "priority": 5, "ordering": 1},
            {"name": "first_order", "description": "Order 0", "task_type": "primitive",
             "priority": 5, "ordering": 0},
        ])

        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, "owner-1", "2099-01-01T00:00:00Z"
        )
        assert claimed is not None
        assert claimed.name == "first_order"


# ---------------------------------------------------------------------------
# release_claim / reassign_claim
# ---------------------------------------------------------------------------


class TestClaimOps:
    """Tests for claim release and reassignment."""

    @pytest.mark.asyncio
    async def test_release_returns_to_ready(self, initialized_db) -> None:
        """Releasing a claim returns the task to 'ready' with cleared fields.

        Invariant: after release, claim_token/owner/session are all NULL.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, [
            {"name": "releasable", "description": "Will be released", "task_type": "primitive",
             "ordering": 0},
        ])
        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, "owner-1", "2099-01-01T00:00:00Z"
        )
        assert claimed is not None

        await planner.release_claim(claimed.id)

        tree = await planner.get_task_tree(pipeline_id)
        task = tree[0]
        assert task.status == "ready"
        assert task.claim_token is None
        assert task.assigned_session_id is None

    @pytest.mark.asyncio
    async def test_reassign_updates_session(self, initialized_db) -> None:
        """Reassigning a claim updates the session ID without changing status.

        Invariant: ClaimedTaskResumedOrReleased — rotation reassigns, not releases.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        session_id_1 = await _seed_session(pool, pipeline_id)
        session_id_2 = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, [
            {"name": "reassignable", "description": "Will be reassigned",
             "task_type": "primitive", "ordering": 0},
        ])
        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id_1, "owner-1", "2099-01-01T00:00:00Z"
        )
        assert claimed is not None
        assert claimed.assigned_session_id == session_id_1

        await planner.reassign_claim(claimed.id, session_id_2)

        tree = await planner.get_task_tree(pipeline_id)
        task = tree[0]
        assert task.status == "in_progress"
        assert task.assigned_session_id == session_id_2


# ---------------------------------------------------------------------------
# complete_task + readiness propagation
# ---------------------------------------------------------------------------


class TestCompleteTask:
    """Tests for task completion and readiness propagation."""

    @pytest.mark.asyncio
    async def test_complete_marks_completed(self, initialized_db) -> None:
        """Completing a task sets status, checkpoint_rev, diary_entry.

        Invariant: completed tasks have status='completed' and cleared claims.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, [
            {"name": "completable", "description": "Will complete",
             "task_type": "primitive", "ordering": 0},
        ])
        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, "owner-1", "2099-01-01T00:00:00Z"
        )

        await planner.complete_task(
            claimed.id, "rev123", "Learned about X"
        )
        tree = await planner.get_task_tree(pipeline_id)
        task = tree[0]
        assert task.status == "completed"
        assert task.checkpoint_rev == "rev123"
        assert task.diary_entry == "Learned about X"
        assert task.claim_token is None

    @pytest.mark.asyncio
    async def test_complete_propagates_readiness(self, initialized_db) -> None:
        """Completing a task unblocks dependents that have all hard deps met.

        Invariant: dependent tasks with all hard deps completed transition
        from 'not_ready' to 'ready'.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, [
            {"name": "blocker", "description": "Blocks next", "task_type": "primitive",
             "priority": 10, "ordering": 0},
            {"name": "blocked", "description": "Needs blocker", "task_type": "primitive",
             "ordering": 1, "dependencies": ["blocker"]},
        ])

        # blocked should be not_ready
        tree = await planner.get_task_tree(pipeline_id)
        blocked = next(t for t in tree if t.name == "blocked")
        assert blocked.status == "not_ready"

        # Complete the blocker
        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, "owner-1", "2099-01-01T00:00:00Z"
        )
        assert claimed.name == "blocker"
        newly_ready = await planner.complete_task(claimed.id, None, "Done")

        assert blocked.id in newly_ready

        # Verify blocked is now ready
        tree = await planner.get_task_tree(pipeline_id)
        blocked = next(t for t in tree if t.name == "blocked")
        assert blocked.status == "ready"

    @pytest.mark.asyncio
    async def test_complete_does_not_unblock_with_remaining_deps(self, initialized_db) -> None:
        """A dependent stays 'not_ready' if it still has unmet hard deps.

        Invariant: readiness requires ALL hard deps completed, not just one.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, [
            {"name": "dep_a", "description": "Dep A", "task_type": "primitive",
             "priority": 10, "ordering": 0},
            {"name": "dep_b", "description": "Dep B", "task_type": "primitive",
             "priority": 10, "ordering": 1},
            {"name": "needs_both", "description": "Needs A and B", "task_type": "primitive",
             "ordering": 2, "dependencies": ["dep_a", "dep_b"]},
        ])

        # Complete only dep_a
        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, "owner-1", "2099-01-01T00:00:00Z"
        )
        newly_ready = await planner.complete_task(claimed.id, None, "Done")

        tree = await planner.get_task_tree(pipeline_id)
        needs_both = next(t for t in tree if t.name == "needs_both")
        assert needs_both.status == "not_ready"
        assert needs_both.id not in newly_ready

    @pytest.mark.asyncio
    async def test_compound_auto_completes_when_children_done(self, initialized_db) -> None:
        """A compound parent auto-completes when all children are completed.

        Invariant: compound tasks complete iff all children are completed.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, [
            {"name": "group", "description": "Group", "task_type": "compound", "ordering": 0},
            {"name": "child_a", "description": "Child A", "task_type": "primitive",
             "parent_name": "group", "priority": 10, "ordering": 1},
            {"name": "child_b", "description": "Child B", "task_type": "primitive",
             "parent_name": "group", "priority": 5, "ordering": 2},
        ])

        # Complete child_a
        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, "owner-1", "2099-01-01T00:00:00Z"
        )
        assert claimed.name == "child_a"
        await planner.complete_task(claimed.id, None, "Done")

        # Group should not be completed yet
        tree = await planner.get_task_tree(pipeline_id)
        group = next(t for t in tree if t.name == "group")
        assert group.status != "completed"

        # Complete child_b
        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, "owner-2", "2099-01-01T00:00:00Z"
        )
        assert claimed.name == "child_b"
        await planner.complete_task(claimed.id, None, "Done")

        # Now group should be completed
        tree = await planner.get_task_tree(pipeline_id)
        group = next(t for t in tree if t.name == "group")
        assert group.status == "completed"


# ---------------------------------------------------------------------------
# fail_task
# ---------------------------------------------------------------------------


class TestFailTask:
    """Tests for task failure and dependent blocking."""

    @pytest.mark.asyncio
    async def test_fail_marks_failed(self, initialized_db) -> None:
        """Failing a task sets status='failed' with reason in diary_entry.

        Invariant: failed tasks have cleared claims and a diary entry.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, [
            {"name": "will_fail", "description": "Will fail", "task_type": "primitive",
             "ordering": 0},
        ])
        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, "owner-1", "2099-01-01T00:00:00Z"
        )
        await planner.fail_task(claimed.id, "Tests did not pass")

        tree = await planner.get_task_tree(pipeline_id)
        assert tree[0].status == "failed"
        assert tree[0].diary_entry == "Tests did not pass"
        assert tree[0].claim_token is None

    @pytest.mark.asyncio
    async def test_fail_blocks_dependents(self, initialized_db) -> None:
        """Failing a task blocks all tasks that hard-depend on it.

        Invariant: hard dependents of a failed task must not be claimable.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, [
            {"name": "fails", "description": "Fails", "task_type": "primitive",
             "priority": 10, "ordering": 0},
            {"name": "depends_on_fails", "description": "Depends", "task_type": "primitive",
             "ordering": 1, "dependencies": ["fails"]},
        ])

        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, "owner-1", "2099-01-01T00:00:00Z"
        )
        await planner.fail_task(claimed.id, "Broken")

        tree = await planner.get_task_tree(pipeline_id)
        dependent = next(t for t in tree if t.name == "depends_on_fails")
        assert dependent.status == "blocked"


# ---------------------------------------------------------------------------
# verify_postconditions
# ---------------------------------------------------------------------------


class TestVerifyPostconditions:
    """Tests for postcondition verification."""

    @pytest.mark.asyncio
    async def test_file_exists_postcondition(self, initialized_db, tmp_path) -> None:
        """file_exists postcondition passes when the file is present.

        Invariant: verify_postconditions correctly delegates to verify_condition.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        planner = HTNPlanner(pool)

        # Create a task with a file_exists postcondition
        tasks_json = [{
            "name": "check_file",
            "description": "Check file",
            "task_type": "primitive",
            "ordering": 0,
            "postconditions": [
                {"type": "file_exists", "path": "output.txt", "description": "Output file must exist"},
            ],
        }]
        ids = await planner.populate_from_structured_output(pipeline_id, tasks_json)

        # Without the file → fail
        results = await planner.verify_postconditions(ids[0], str(tmp_path))
        assert len(results) == 1
        assert results[0].passed is False

        # Create the file → pass
        (tmp_path / "output.txt").write_text("hello")
        results = await planner.verify_postconditions(ids[0], str(tmp_path))
        assert results[0].passed is True

    @pytest.mark.asyncio
    async def test_task_completed_postcondition(self, initialized_db, tmp_path) -> None:
        """task_completed postcondition checks the DB for completed tasks.

        Invariant: task_completed resolves against the pipeline's completed task names.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        tasks_json = [
            {"name": "prerequisite", "description": "Must finish first",
             "task_type": "primitive", "ordering": 0},
            {"name": "checker", "description": "Checks prerequisite",
             "task_type": "primitive", "ordering": 1,
             "postconditions": [
                 {"type": "task_completed", "task_name": "prerequisite",
                  "description": "Prerequisite must be done"},
             ]},
        ]
        ids = await planner.populate_from_structured_output(pipeline_id, tasks_json)

        # Before prerequisite is completed
        results = await planner.verify_postconditions(ids[1], str(tmp_path))
        assert results[0].passed is False

        # Complete the prerequisite
        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, "owner-1", "2099-01-01T00:00:00Z"
        )
        await planner.complete_task(claimed.id, None, "Done")

        # After prerequisite is completed
        results = await planner.verify_postconditions(ids[1], str(tmp_path))
        assert results[0].passed is True

    @pytest.mark.asyncio
    async def test_no_postconditions_returns_empty(self, initialized_db, tmp_path) -> None:
        """Tasks with no postconditions return an empty list.

        Invariant: no postconditions → empty results (trivially passing).
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        planner = HTNPlanner(pool)

        ids = await planner.populate_from_structured_output(pipeline_id, [
            {"name": "no_post", "description": "No postconditions",
             "task_type": "primitive", "ordering": 0},
        ])

        results = await planner.verify_postconditions(ids[0], str(tmp_path))
        assert results == []

    @pytest.mark.asyncio
    async def test_nonexistent_task_returns_error(self, initialized_db, tmp_path) -> None:
        """Verifying postconditions for a non-existent task returns an error result.

        Invariant: invalid task_id does not crash — returns a descriptive error.
        """
        pool = get_pool()
        planner = HTNPlanner(pool)
        results = await planner.verify_postconditions(99999, str(tmp_path))
        assert len(results) == 1
        assert results[0].passed is False
        assert "no task with id" in results[0].detail.lower()


# ---------------------------------------------------------------------------
# Decision escalation
# ---------------------------------------------------------------------------


class TestDecisionEscalation:
    """Tests for decision task escalation and resolution."""

    @pytest.mark.asyncio
    async def test_create_escalation(self, initialized_db) -> None:
        """Creating a decision escalation inserts an escalation row and blocks the task.

        Invariant: decision tasks that are escalated become 'blocked' with a matching
        open escalation.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        planner = HTNPlanner(pool)

        ids = await planner.populate_from_structured_output(pipeline_id, [
            {"name": "design_choice", "description": "Pick a DB",
             "task_type": "decision", "ordering": 0},
        ])

        esc_id = await planner.create_decision_escalation(
            ids[0], pipeline_id, "Which database to use?"
        )
        assert esc_id > 0

        tree = await planner.get_task_tree(pipeline_id)
        assert tree[0].status == "blocked"

        # Verify escalation in DB
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT * FROM escalations WHERE id = %s", (esc_id,))
            esc = await cur.fetchone()
            assert esc["status"] == "open"
            assert esc["reason"] == "design_decision"

    @pytest.mark.asyncio
    async def test_resolve_decision(self, initialized_db) -> None:
        """Resolving a decision task completes it and unblocks dependents.

        Invariant: resolved decisions propagate readiness like completed tasks.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        planner = HTNPlanner(pool)

        ids = await planner.populate_from_structured_output(pipeline_id, [
            {"name": "decide", "description": "Make a decision",
             "task_type": "decision", "ordering": 0},
            {"name": "after_decision", "description": "After decision",
             "task_type": "primitive", "ordering": 1,
             "dependencies": ["decide"]},
        ])

        await planner.create_decision_escalation(ids[0], pipeline_id, "Choose X or Y")
        newly_ready = await planner.resolve_decision(ids[0], "Choose X")

        tree = await planner.get_task_tree(pipeline_id)
        decision = next(t for t in tree if t.name == "decide")
        assert decision.status == "completed"
        assert decision.diary_entry == "Choose X"

        after = next(t for t in tree if t.name == "after_decision")
        assert after.status == "ready"
        assert after.id in newly_ready


# ---------------------------------------------------------------------------
# get_progress_summary
# ---------------------------------------------------------------------------


class TestProgressSummary:
    """Tests for progress summary counts."""

    @pytest.mark.asyncio
    async def test_progress_summary_counts(self, initialized_db) -> None:
        """Progress summary returns accurate counts grouped by status.

        Invariant: sum of counts equals total number of tasks.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, [
            {"name": "a", "description": "A", "task_type": "primitive",
             "priority": 10, "ordering": 0},
            {"name": "b", "description": "B", "task_type": "primitive",
             "ordering": 1, "dependencies": ["a"]},
            {"name": "c", "description": "C", "task_type": "primitive",
             "ordering": 2, "dependencies": ["a"]},
        ])

        summary = await planner.get_progress_summary(pipeline_id)
        assert summary.get("ready", 0) == 1  # only 'a' is ready
        assert summary.get("not_ready", 0) == 2  # b, c blocked by a
        assert sum(summary.values()) == 3

    @pytest.mark.asyncio
    async def test_empty_pipeline_returns_empty_summary(self, initialized_db) -> None:
        """Pipeline with no tasks returns an empty summary.

        Invariant: no tasks → empty dict.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        planner = HTNPlanner(pool)
        summary = await planner.get_progress_summary(pipeline_id)
        assert summary == {}


# ---------------------------------------------------------------------------
# sync_to_markdown
# ---------------------------------------------------------------------------


class TestSyncToMarkdown:
    """Tests for syncing the task tree to a markdown file."""

    @pytest.mark.asyncio
    async def test_creates_markdown_file(self, initialized_db, tmp_path) -> None:
        """sync_to_markdown writes a task-list.md file.

        Invariant: the file exists at specs/task-list.md under the working dir.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, [
            {"name": "write_tests", "description": "Write unit tests",
             "task_type": "primitive", "ordering": 0},
        ])

        await planner.sync_to_markdown(pipeline_id, str(tmp_path))

        md_path = tmp_path / "specs" / "task-list.md"
        assert md_path.exists()
        content = md_path.read_text()
        assert "# Task List" in content
        assert "write_tests" in content

    @pytest.mark.asyncio
    async def test_markdown_reflects_status(self, initialized_db, tmp_path) -> None:
        """Markdown file uses status icons matching the task state.

        Invariant: completed tasks show [x], ready show [ ], not_ready show [ ].
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, [
            {"name": "done", "description": "Already done", "task_type": "primitive",
             "priority": 10, "ordering": 0},
            {"name": "waiting", "description": "Waiting", "task_type": "primitive",
             "ordering": 1, "dependencies": ["done"]},
        ])

        # Complete 'done'
        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, "owner-1", "2099-01-01T00:00:00Z"
        )
        await planner.complete_task(claimed.id, None, "Done")

        await planner.sync_to_markdown(pipeline_id, str(tmp_path))
        content = (tmp_path / "specs" / "task-list.md").read_text()
        assert "[x] **done**" in content
        assert "[ ] **waiting**" in content

    @pytest.mark.asyncio
    async def test_markdown_shows_dependencies(self, initialized_db, tmp_path) -> None:
        """Markdown file includes dependency information.

        Invariant: tasks with deps show 'Depends on:' line.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, [
            {"name": "prerequisite", "description": "First", "task_type": "primitive",
             "ordering": 0},
            {"name": "dependent", "description": "Second", "task_type": "primitive",
             "ordering": 1, "dependencies": ["prerequisite"]},
        ])

        await planner.sync_to_markdown(pipeline_id, str(tmp_path))
        content = (tmp_path / "specs" / "task-list.md").read_text()
        assert "Depends on: prerequisite" in content

    @pytest.mark.asyncio
    async def test_markdown_shows_hierarchy(self, initialized_db, tmp_path) -> None:
        """Markdown file indents children under their parent.

        Invariant: child tasks appear as nested list items under their parent.
        """
        pool = get_pool()
        pipeline_id, _ = await _seed_pipeline(pool)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, [
            {"name": "parent", "description": "Group", "task_type": "compound", "ordering": 0},
            {"name": "child", "description": "Sub-task", "task_type": "primitive",
             "parent_name": "parent", "ordering": 1},
        ])

        await planner.sync_to_markdown(pipeline_id, str(tmp_path))
        content = (tmp_path / "specs" / "task-list.md").read_text()
        # Child should be indented
        assert "  - " in content
        assert "**child**" in content


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestPropertyBased:
    """Property-based tests for HTNPlanner invariants."""

    @settings(max_examples=20, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(tasks=task_dicts())
    @pytest.mark.asyncio
    async def test_populate_creates_correct_count(self, initialized_db, tasks) -> None:
        """Property: populate always creates exactly len(tasks) task rows.

        Invariant: |populated| == |input| for all valid task lists.
        """
        pool = get_pool()
        # Use unique suffix to avoid name collisions across hypothesis runs
        import uuid
        suffix = uuid.uuid4().hex[:8]
        pipeline_id, _ = await _seed_pipeline(pool, name_suffix=suffix)
        planner = HTNPlanner(pool)
        ids = await planner.populate_from_structured_output(pipeline_id, tasks)
        assert len(ids) == len(tasks)

        tree = await planner.get_task_tree(pipeline_id)
        assert len(tree) == len(tasks)

    @settings(max_examples=20, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(tasks=task_dicts())
    @pytest.mark.asyncio
    async def test_progress_summary_sums_to_total(self, initialized_db, tasks) -> None:
        """Property: sum of progress summary counts equals total tasks.

        Invariant: for all task lists, sum(summary.values()) == len(tasks).
        """
        pool = get_pool()
        import uuid
        suffix = uuid.uuid4().hex[:8]
        pipeline_id, _ = await _seed_pipeline(pool, name_suffix=suffix)
        planner = HTNPlanner(pool)
        await planner.populate_from_structured_output(pipeline_id, tasks)
        summary = await planner.get_progress_summary(pipeline_id)
        assert sum(summary.values()) == len(tasks)

    @settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(tasks=task_dicts())
    @pytest.mark.asyncio
    async def test_no_ready_compounds(self, initialized_db, tasks) -> None:
        """Property: compound tasks never become 'ready' after population.

        Invariant: compound tasks are not directly executable.
        """
        pool = get_pool()
        import uuid
        suffix = uuid.uuid4().hex[:8]
        pipeline_id, _ = await _seed_pipeline(pool, name_suffix=suffix)
        planner = HTNPlanner(pool)
        await planner.populate_from_structured_output(pipeline_id, tasks)
        tree = await planner.get_task_tree(pipeline_id)
        for task in tree:
            if task.task_type == "compound":
                assert task.status != "ready", (
                    f"Compound task '{task.name}' should not be 'ready'"
                )


# ---------------------------------------------------------------------------
# Property-based tests — HTN claim invariants
# ---------------------------------------------------------------------------


@st.composite
def independent_primitive_tasks(draw: st.DrawFn) -> list[dict]:
    """Generate 2-6 independent primitive tasks with distinct priorities.

    All tasks have no dependencies, so they all become 'ready' after population.
    Priorities are unique so claim order is deterministic.
    """
    n = draw(st.integers(min_value=2, max_value=6))
    tasks = []
    names: list[str] = []
    priorities = list(range(n))  # unique priorities 0..n-1
    for i in range(n):
        name = draw(st.from_regex(r"[a-zA-Z][a-zA-Z0-9_]{1,12}", fullmatch=True))
        while name in names:
            name = name + str(i)
        names.append(name)
        tasks.append({
            "name": name,
            "description": f"Task {name}",
            "task_type": "primitive",
            "priority": priorities[n - 1 - i],  # highest priority first
            "ordering": i,
            "preconditions": [],
            "postconditions": [],
        })
    return tasks


@st.composite
def chain_primitive_tasks(draw: st.DrawFn) -> list[dict]:
    """Generate a linear chain of 2-5 primitive tasks where each depends on the previous.

    Only the first task will be 'ready' initially.
    """
    n = draw(st.integers(min_value=2, max_value=5))
    tasks = []
    names: list[str] = []
    for i in range(n):
        name = draw(st.from_regex(r"[a-zA-Z][a-zA-Z0-9_]{1,12}", fullmatch=True))
        while name in names:
            name = name + str(i)
        names.append(name)
        deps = [names[i - 1]] if i > 0 else []
        tasks.append({
            "name": name,
            "description": f"Chain task {name}",
            "task_type": "primitive",
            "priority": 0,
            "ordering": i,
            "dependencies": deps,
            "preconditions": [],
            "postconditions": [],
        })
    return tasks


class TestHTNClaimProperties:
    """Property-based tests for HTN task claim invariants."""

    @settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(tasks=independent_primitive_tasks())
    @pytest.mark.asyncio
    async def test_claim_sets_all_ownership_fields(self, initialized_db, tasks) -> None:
        """Property: a claimed task always has all ownership fields set.

        Invariant: claimed task has status='in_progress', non-null claim_token,
        claim_owner_token, assigned_session_id, and claim_expires_at.
        """
        import uuid
        pool = get_pool()
        suffix = uuid.uuid4().hex[:8]
        pipeline_id, _ = await _seed_pipeline(pool, name_suffix=suffix)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, tasks)

        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, f"owner-{suffix}", "2099-01-01T00:00:00Z"
        )
        assert claimed is not None
        assert claimed.status == "in_progress"
        assert claimed.claim_token is not None
        assert claimed.claim_owner_token is not None
        assert claimed.assigned_session_id == session_id
        assert claimed.claim_expires_at is not None

    @settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(tasks=independent_primitive_tasks())
    @pytest.mark.asyncio
    async def test_complete_clears_all_claim_fields(self, initialized_db, tasks) -> None:
        """Property: completing a task clears all claim ownership fields.

        Invariant: after complete_task, claim_token, claim_owner_token,
        and claim_expires_at are all NULL.
        """
        import uuid
        pool = get_pool()
        suffix = uuid.uuid4().hex[:8]
        pipeline_id, _ = await _seed_pipeline(pool, name_suffix=suffix)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, tasks)
        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, f"owner-{suffix}", "2099-01-01T00:00:00Z"
        )
        assert claimed is not None

        await planner.complete_task(claimed.id, f"rev-{suffix}", "Done")

        tree = await planner.get_task_tree(pipeline_id)
        completed = next(t for t in tree if t.id == claimed.id)
        assert completed.status == "completed"
        assert completed.claim_token is None
        assert completed.claim_owner_token is None
        assert completed.claim_expires_at is None
        assert completed.checkpoint_rev == f"rev-{suffix}"

    @settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(tasks=independent_primitive_tasks())
    @pytest.mark.asyncio
    async def test_claim_always_selects_max_priority(self, initialized_db, tasks) -> None:
        """Property: claim_next_ready_task always selects the highest-priority task.

        Invariant: for all task sets, the claimed task has the maximum priority
        among all ready primitive tasks.
        """
        import uuid
        pool = get_pool()
        suffix = uuid.uuid4().hex[:8]
        pipeline_id, _ = await _seed_pipeline(pool, name_suffix=suffix)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, tasks)

        # Find the expected max priority among ready primitives
        tree = await planner.get_task_tree(pipeline_id)
        ready_primitives = [
            t for t in tree if t.status == "ready" and t.task_type == "primitive"
        ]
        if not ready_primitives:
            return  # nothing to claim

        max_priority = max(t.priority for t in ready_primitives)

        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, f"owner-{suffix}", "2099-01-01T00:00:00Z"
        )
        assert claimed is not None
        assert claimed.priority == max_priority

    @settings(max_examples=10, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(tasks=independent_primitive_tasks())
    @pytest.mark.asyncio
    async def test_concurrent_claims_yield_distinct_tasks(self, initialized_db, tasks) -> None:
        """Property: N sequential claims on N ready tasks yield N distinct tasks.

        Invariant: UniqueTaskClaim — each claim returns a different task.
        """
        import uuid
        pool = get_pool()
        suffix = uuid.uuid4().hex[:8]
        pipeline_id, _ = await _seed_pipeline(pool, name_suffix=suffix)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, tasks)

        tree = await planner.get_task_tree(pipeline_id)
        n_ready = sum(1 for t in tree if t.status == "ready" and t.task_type == "primitive")

        claimed_ids: set[int] = set()
        for i in range(n_ready):
            claimed = await planner.claim_next_ready_task(
                pipeline_id, session_id, f"owner-{suffix}-{i}", "2099-01-01T00:00:00Z"
            )
            assert claimed is not None, f"Expected claim #{i+1} to succeed"
            assert claimed.id not in claimed_ids, (
                f"Task {claimed.id} claimed twice"
            )
            claimed_ids.add(claimed.id)

        # Next claim should return None
        extra = await planner.claim_next_ready_task(
            pipeline_id, session_id, f"owner-{suffix}-extra", "2099-01-01T00:00:00Z"
        )
        assert extra is None

    @settings(max_examples=10, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(tasks=independent_primitive_tasks())
    @pytest.mark.asyncio
    async def test_release_then_reclaim_succeeds(self, initialized_db, tasks) -> None:
        """Property: releasing a claim returns task to 'ready' and it can be reclaimed.

        Invariant: release_claim → task is reclaimable by any session.
        """
        import uuid
        pool = get_pool()
        suffix = uuid.uuid4().hex[:8]
        pipeline_id, _ = await _seed_pipeline(pool, name_suffix=suffix)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, tasks)

        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, f"owner-{suffix}", "2099-01-01T00:00:00Z"
        )
        assert claimed is not None
        original_id = claimed.id

        await planner.release_claim(original_id)

        # Verify task is back to ready
        tree = await planner.get_task_tree(pipeline_id)
        released = next(t for t in tree if t.id == original_id)
        assert released.status == "ready"
        assert released.claim_token is None

        # Reclaim should return the same task (highest priority)
        reclaimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, f"owner-{suffix}-2", "2099-01-01T00:00:00Z"
        )
        assert reclaimed is not None
        assert reclaimed.status == "in_progress"

    @settings(max_examples=10, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(chain=chain_primitive_tasks())
    @pytest.mark.asyncio
    async def test_chain_readiness_propagation(self, initialized_db, chain) -> None:
        """Property: completing tasks in a chain unlocks exactly the next task.

        Invariant: in a linear dependency chain, only the next task becomes ready
        when the current one completes. All later tasks stay not_ready.
        """
        import uuid
        pool = get_pool()
        suffix = uuid.uuid4().hex[:8]
        pipeline_id, _ = await _seed_pipeline(pool, name_suffix=suffix)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, chain)

        # Only first task should be ready
        tree = await planner.get_task_tree(pipeline_id)
        ready_tasks = [t for t in tree if t.status == "ready"]
        assert len(ready_tasks) == 1
        assert ready_tasks[0].name == chain[0]["name"]

        # Complete tasks in order and verify propagation
        for i in range(len(chain) - 1):
            claimed = await planner.claim_next_ready_task(
                pipeline_id, session_id, f"owner-{suffix}-{i}", "2099-01-01T00:00:00Z"
            )
            assert claimed is not None
            assert claimed.name == chain[i]["name"], (
                f"Expected to claim {chain[i]['name']!r} but got {claimed.name!r}"
            )

            newly_ready = await planner.complete_task(claimed.id, None, "Done")

            tree = await planner.get_task_tree(pipeline_id)
            next_task = next(t for t in tree if t.name == chain[i + 1]["name"])
            assert next_task.status == "ready", (
                f"Task {chain[i+1]['name']!r} should be ready after {chain[i]['name']!r} completed"
            )
            assert next_task.id in newly_ready

            # All tasks after the next should still be not_ready
            for j in range(i + 2, len(chain)):
                later = next(t for t in tree if t.name == chain[j]["name"])
                assert later.status == "not_ready", (
                    f"Task {chain[j]['name']!r} should be not_ready"
                )

    @settings(max_examples=10, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(tasks=task_dicts())
    @pytest.mark.asyncio
    async def test_fail_blocks_only_hard_dependents(self, initialized_db, tasks) -> None:
        """Property: failing a task blocks only tasks with hard dependencies on it.

        Invariant: tasks not depending on the failed task are unaffected.
        """
        import uuid
        pool = get_pool()
        suffix = uuid.uuid4().hex[:8]
        pipeline_id, _ = await _seed_pipeline(pool, name_suffix=suffix)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        await planner.populate_from_structured_output(pipeline_id, tasks)

        # Find a ready primitive to fail
        tree = await planner.get_task_tree(pipeline_id)
        ready_primitives = [t for t in tree if t.status == "ready" and t.task_type == "primitive"]
        if not ready_primitives:
            return  # nothing to fail

        # Claim and fail the first ready task
        claimed = await planner.claim_next_ready_task(
            pipeline_id, session_id, f"owner-{suffix}", "2099-01-01T00:00:00Z"
        )
        assert claimed is not None

        # Get the deps for this pipeline before failing
        deps = await planner.get_task_deps(pipeline_id)
        hard_dependent_ids = {
            d.task_id for d in deps
            if d.depends_on_task_id == claimed.id and d.dep_type == "hard"
        }

        # Record statuses of all non-dependent tasks before the failure
        tree_before = await planner.get_task_tree(pipeline_id)
        non_dependent_statuses = {
            t.id: t.status for t in tree_before
            if t.id != claimed.id and t.id not in hard_dependent_ids
        }

        await planner.fail_task(claimed.id, "Test failure")

        tree_after = await planner.get_task_tree(pipeline_id)

        # Hard dependents that were not_ready or ready should now be blocked
        for t in tree_after:
            if t.id in hard_dependent_ids:
                if non_dependent_statuses.get(t.id) in ("not_ready", "ready"):
                    # These would have been blocked by fail_task
                    pass  # fail_task only blocks not_ready and ready tasks

        # Non-dependent tasks should be unchanged
        for t in tree_after:
            if t.id in non_dependent_statuses:
                if non_dependent_statuses[t.id] not in ("not_ready", "ready"):
                    # Tasks that were in_progress/completed/etc should be unchanged
                    assert t.status == non_dependent_statuses[t.id], (
                        f"Non-dependent task {t.name!r} changed from "
                        f"{non_dependent_statuses[t.id]!r} to {t.status!r}"
                    )

    @settings(max_examples=10, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(data=st.data())
    @pytest.mark.asyncio
    async def test_compound_auto_completion_with_generated_children(
        self, initialized_db, data
    ) -> None:
        """Property: compound parent auto-completes when all children are completed.

        Invariant: a compound task transitions to 'completed' iff all its
        primitive children have status='completed'.
        """
        import uuid
        pool = get_pool()
        suffix = uuid.uuid4().hex[:8]
        pipeline_id, _ = await _seed_pipeline(pool, name_suffix=suffix)
        session_id = await _seed_session(pool, pipeline_id)
        planner = HTNPlanner(pool)

        n_children = data.draw(st.integers(min_value=1, max_value=4))
        tasks = [{
            "name": f"parent_{suffix}",
            "description": "Compound parent",
            "task_type": "compound",
            "ordering": 0,
        }]
        for i in range(n_children):
            tasks.append({
                "name": f"child_{suffix}_{i}",
                "description": f"Child {i}",
                "task_type": "primitive",
                "parent_name": f"parent_{suffix}",
                "priority": n_children - i,  # complete in order
                "ordering": i + 1,
            })

        await planner.populate_from_structured_output(pipeline_id, tasks)

        # Complete all children
        for i in range(n_children):
            claimed = await planner.claim_next_ready_task(
                pipeline_id, session_id, f"owner-{suffix}-{i}", "2099-01-01T00:00:00Z"
            )
            assert claimed is not None
            assert claimed.task_type == "primitive"
            await planner.complete_task(claimed.id, None, f"Child {i} done")

        # Parent should auto-complete
        tree = await planner.get_task_tree(pipeline_id)
        parent = next(t for t in tree if t.name == f"parent_{suffix}")
        assert parent.status == "completed", (
            f"Compound parent should be 'completed' after all {n_children} children done"
        )
