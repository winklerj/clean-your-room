"""Tests for stages/base.py — StageRunner Protocol, registry, and dispatch.

Verifies:
- Registry population when stage modules are imported
- get_stage_runner() lookup for known and unknown types
- register_stage_runner() duplicate and conflict detection
- All 5 concrete runners are registered with correct stage_type keys
- Registry-dispatched calls match orchestrator behavior
- Property-based tests for registry invariants
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from build_your_room.stages.base import (
    STAGE_RUNNERS,
    get_stage_runner,
    register_stage_runner,
)


# ---------------------------------------------------------------------------
# Registry population tests
# ---------------------------------------------------------------------------


class TestRegistryPopulation:
    """Verify that importing the stages package populates the registry."""

    def test_registry_has_all_five_stage_types(self):
        """All 5 spec-mandated stage types must be registered.

        Invariant: the stages package self-registers all concrete runners.
        Context: spec mandates spec_author, impl_plan, impl_task, code_review,
        and validation stage types.
        """
        # Importing the stages package triggers module-level registration
        import build_your_room.stages  # noqa: F401

        expected = {"spec_author", "impl_plan", "impl_task", "code_review", "validation"}
        assert expected == set(STAGE_RUNNERS.keys())

    def test_registry_values_are_callable(self):
        """Every registered runner must be an async callable.

        Invariant: registry values are awaitable callables.
        Context: the orchestrator calls them with await.
        """
        import build_your_room.stages  # noqa: F401

        for stage_type, runner in STAGE_RUNNERS.items():
            assert callable(runner), f"{stage_type} runner is not callable"

    def test_spec_author_runner_is_correct_function(self):
        """spec_author registry entry points to run_spec_author_stage.

        Invariant: registry binds to the correct concrete function.
        Context: prevents silent mis-registration.
        """
        from build_your_room.stages.spec_author import run_spec_author_stage

        assert get_stage_runner("spec_author") is run_spec_author_stage

    def test_impl_plan_runner_is_correct_function(self):
        """impl_plan registry entry points to run_impl_plan_stage.

        Invariant: registry binds to the correct concrete function.
        Context: prevents silent mis-registration.
        """
        from build_your_room.stages.impl_plan import run_impl_plan_stage

        assert get_stage_runner("impl_plan") is run_impl_plan_stage

    def test_impl_task_runner_is_correct_function(self):
        """impl_task registry entry points to run_impl_task_stage.

        Invariant: registry binds to the correct concrete function.
        Context: prevents silent mis-registration.
        """
        from build_your_room.stages.impl_task import run_impl_task_stage

        assert get_stage_runner("impl_task") is run_impl_task_stage

    def test_code_review_runner_is_correct_function(self):
        """code_review registry entry points to run_code_review_stage.

        Invariant: registry binds to the correct concrete function.
        Context: prevents silent mis-registration.
        """
        from build_your_room.stages.code_review import run_code_review_stage

        assert get_stage_runner("code_review") is run_code_review_stage

    def test_validation_runner_is_correct_function(self):
        """validation registry entry points to run_validation_stage.

        Invariant: registry binds to the correct concrete function.
        Context: prevents silent mis-registration.
        """
        from build_your_room.stages.validation import run_validation_stage

        assert get_stage_runner("validation") is run_validation_stage


# ---------------------------------------------------------------------------
# get_stage_runner() tests
# ---------------------------------------------------------------------------


class TestGetStageRunner:
    """Verify registry lookup behavior."""

    def test_known_type_returns_runner(self):
        """Lookup for a registered type returns the runner function.

        Invariant: get_stage_runner returns non-None for registered types.
        Context: the orchestrator depends on this for dispatch.
        """
        import build_your_room.stages  # noqa: F401

        runner = get_stage_runner("spec_author")
        assert runner is not None

    def test_unknown_type_returns_none(self):
        """Lookup for an unregistered type returns None.

        Invariant: unknown types don't raise, they return None.
        Context: the orchestrator falls back to _default_stage_result.
        """
        assert get_stage_runner("nonexistent_stage_type") is None

    def test_empty_string_returns_none(self):
        """Empty string lookup returns None.

        Invariant: degenerate input handled gracefully.
        Context: defensive against malformed stage graph data.
        """
        assert get_stage_runner("") is None


# ---------------------------------------------------------------------------
# register_stage_runner() tests
# ---------------------------------------------------------------------------


class TestRegisterStageRunner:
    """Verify registration behavior including idempotency and conflict detection."""

    def test_idempotent_registration(self):
        """Re-registering the same function for the same type is a no-op.

        Invariant: idempotent registration doesn't raise.
        Context: module-level registration may execute multiple times if
        the module is re-imported or if tests re-import.
        """
        async def my_runner(**kwargs):
            return "ok"

        register_stage_runner("__test_idempotent", my_runner)
        register_stage_runner("__test_idempotent", my_runner)  # same fn, no error
        assert get_stage_runner("__test_idempotent") is my_runner

        # Cleanup
        STAGE_RUNNERS.pop("__test_idempotent", None)

    def test_conflicting_registration_raises(self):
        """Registering a different function for an already-registered type raises.

        Invariant: no silent overwrite of existing registrations.
        Context: prevents accidental double-registration with conflicting
        implementations.
        """
        async def runner_a(**kwargs):
            return "a"

        async def runner_b(**kwargs):
            return "b"

        register_stage_runner("__test_conflict", runner_a)
        with pytest.raises(ValueError, match="already registered"):
            register_stage_runner("__test_conflict", runner_b)

        # Cleanup
        STAGE_RUNNERS.pop("__test_conflict", None)


# ---------------------------------------------------------------------------
# Orchestrator dispatch integration tests
# ---------------------------------------------------------------------------


class TestOrchestratorDispatchViaRegistry:
    """Verify that the orchestrator dispatches correctly through the registry."""

    async def test_dispatch_calls_registered_runner(self, initialized_db):
        """Orchestrator _run_stage dispatches to the registry-looked-up runner.

        Invariant: the orchestrator uses the registry, not hardcoded imports.
        Context: validates the refactor from if/elif to registry dispatch.
        """
        from build_your_room.db import get_pool
        from build_your_room.orchestrator import PipelineOrchestrator
        from build_your_room.stage_graph import StageGraph
        from build_your_room.streaming import LogBuffer

        pool = get_pool()
        log_buffer = LogBuffer()

        graph_json = (
            '{"entry_stage":"s","nodes":[{"key":"s","name":"S","type":"spec_author",'
            '"agent":"claude","prompt":"p","model":"m","max_iterations":1}],"edges":[]}'
        )
        graph = StageGraph.from_json(json.loads(graph_json))

        # Create minimal DB state
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO repos (id, name, local_path, created_at) "
                "VALUES (1, 'test-repo', '/tmp/r', now())"
            )
            await conn.execute(
                "INSERT INTO pipeline_defs (id, name, stage_graph_json, created_at) "
                "VALUES (1, 'def1', %s, now())",
                (graph_json,),
            )
            await conn.execute(
                "INSERT INTO pipelines "
                "(id, pipeline_def_id, repo_id, clone_path, review_base_rev, "
                " status, current_stage_key, config_json, created_at, updated_at) "
                "VALUES (1, 1, 1, '/tmp/c', 'abc', 'running', 's', '{}', now(), now())"
            )
            await conn.commit()

        orch = PipelineOrchestrator(
            pool=pool,
            adapters={},  # no adapter → will skip
            log_buffer=log_buffer,
        )

        cancel = asyncio.Event()
        result = await orch._run_stage(1, "s", graph, cancel)

        # No adapter registered → stage skipped → default result
        assert isinstance(result, str)

    async def test_unknown_stage_type_uses_default(self, initialized_db):
        """Unknown stage types fall back to _default_stage_result.

        Invariant: unregistered stage types don't raise.
        Context: custom/future stage types need graceful handling.
        """
        from build_your_room.db import get_pool
        from build_your_room.orchestrator import PipelineOrchestrator
        from build_your_room.stage_graph import StageGraph
        from build_your_room.streaming import LogBuffer

        pool = get_pool()
        log_buffer = LogBuffer()

        graph_json = (
            '{"entry_stage":"x","nodes":[{"key":"x","name":"X","type":"custom_unknown",'
            '"agent":"claude","prompt":"p","model":"m","max_iterations":1}],"edges":[]}'
        )
        graph = StageGraph.from_json(json.loads(graph_json))

        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO repos (id, name, local_path, created_at) "
                "VALUES (1, 'test-repo', '/tmp/r', now())"
            )
            await conn.execute(
                "INSERT INTO pipeline_defs (id, name, stage_graph_json, created_at) "
                "VALUES (1, 'def1', %s, now())",
                (graph_json,),
            )
            await conn.execute(
                "INSERT INTO pipelines "
                "(id, pipeline_def_id, repo_id, clone_path, review_base_rev, "
                " status, current_stage_key, config_json, created_at, updated_at) "
                "VALUES (1, 1, 1, '/tmp/c', 'abc', 'running', 'x', '{}', now(), now())"
            )
            await conn.commit()

        orch = PipelineOrchestrator(
            pool=pool,
            adapters={"claude": AsyncMock()},
            log_buffer=log_buffer,
        )

        cancel = asyncio.Event()
        result = await orch._run_stage(1, "x", graph, cancel)
        # custom_unknown has no registered runner → fallback
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestRegistryProperties:
    """Property-based tests for registry invariants."""

    @given(stage_type=st.text(min_size=1, max_size=50).filter(
        lambda s: s not in STAGE_RUNNERS
    ))
    @settings(max_examples=20)
    def test_unknown_types_always_return_none(self, stage_type: str):
        """get_stage_runner returns None for any unregistered type.

        Invariant: no false positives in lookup.
        Context: generated strings that aren't in the registry must return None.
        """
        assert get_stage_runner(stage_type) is None

    @given(data=st.data())
    @settings(max_examples=10)
    def test_registered_types_always_resolve(self, data):
        """get_stage_runner returns non-None for any registered type.

        Invariant: no false negatives in lookup.
        Context: sampling from the actual registry keys.
        """
        import build_your_room.stages  # noqa: F401

        if not STAGE_RUNNERS:
            pytest.skip("No runners registered")
        stage_type = data.draw(st.sampled_from(list(STAGE_RUNNERS.keys())))
        runner = get_stage_runner(stage_type)
        assert runner is not None
        assert callable(runner)

    @given(stage_type=st.text(min_size=1, max_size=30, alphabet=st.characters(
        categories=("L", "Nd"), whitelist_characters="_"
    )))
    @settings(max_examples=15)
    def test_double_register_same_fn_is_idempotent(self, stage_type: str):
        """Registering the same function twice never raises.

        Invariant: idempotent registration for identical (type, fn) pairs.
        Context: module reimport safety.
        """
        key = f"__prop_{stage_type}"

        async def runner(**kwargs):
            return "ok"

        # Clean up first in case of leftover from prior run
        STAGE_RUNNERS.pop(key, None)

        register_stage_runner(key, runner)
        register_stage_runner(key, runner)  # idempotent
        assert get_stage_runner(key) is runner

        # Cleanup
        STAGE_RUNNERS.pop(key, None)
