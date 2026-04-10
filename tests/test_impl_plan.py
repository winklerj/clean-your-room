"""Tests for ImplPlanStage — implementation planning with optional review loop
and HTN task graph population from structured output.

All agent interactions are mocked — no live API calls.

Test categories:
- Unit tests: _artifact_path, _load_spec_artifact, _parse_plan_output, _try_extract_tasks_json
- Integration tests: run_impl_plan_stage with mocked adapters + real DB
- Property-based tests: HTN task schema parsing, artifact path invariants
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from build_your_room.adapters.base import SessionConfig
from build_your_room.stage_graph import ReviewConfig, StageNode
from build_your_room.stages.impl_plan import (
    HTN_TASK_OUTPUT_SCHEMA,
    STAGE_RESULT_APPROVED,
    STAGE_RESULT_ESCALATED,
    _artifact_path,
    _load_spec_artifact,
    _parse_plan_output,
    _try_extract_tasks_json,
    parse_htn_tasks,
    run_impl_plan_stage,
)
from build_your_room.stages.review_loop import ReviewLoopOutcome
from build_your_room.streaming import LogBuffer


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_complexities = st.sampled_from(["trivial", "small", "medium", "large", "epic"])
_task_types = st.sampled_from(["compound", "primitive", "decision"])


@st.composite
def htn_task_dicts(draw: st.DrawFn) -> dict[str, Any]:
    """Generate well-formed HTN task dicts matching the schema."""
    return {
        "name": draw(st.from_regex(r"[a-z][a-z0-9_]{2,20}", fullmatch=True)),
        "description": draw(st.text(min_size=5, max_size=80)),
        "task_type": draw(_task_types),
        "parent_name": None,
        "priority": draw(st.integers(min_value=0, max_value=100)),
        "ordering": draw(st.integers(min_value=0, max_value=50)),
        "preconditions": [],
        "postconditions": [],
        "invariants": None,
        "estimated_complexity": draw(_complexities),
        "dependencies": [],
    }


@st.composite
def htn_task_lists(draw: st.DrawFn) -> list[dict[str, Any]]:
    """Generate a list of HTN tasks with unique names."""
    tasks = draw(st.lists(htn_task_dicts(), min_size=1, max_size=8))
    # Ensure unique names
    seen: set[str] = set()
    unique_tasks = []
    for t in tasks:
        if t["name"] not in seen:
            seen.add(t["name"])
            unique_tasks.append(t)
    return unique_tasks


# ---------------------------------------------------------------------------
# Fake session result
# ---------------------------------------------------------------------------


@dataclass
class FakeTurnResult:
    """Minimal SessionResult for tests."""

    output: str = "# Implementation Plan\n\n## Tasks\n\n1. Task A\n2. Task B"
    structured_output: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------


def _make_node(**overrides: Any) -> StageNode:
    defaults: dict[str, Any] = {
        "key": "impl_plan",
        "name": "Implementation plan",
        "stage_type": "impl_plan",
        "agent": "claude",
        "prompt": "impl_plan_default",
        "model": "claude-opus-4-6",
        "max_iterations": 1,
        "context_threshold_pct": 60,
    }
    defaults.update(overrides)
    return StageNode(**defaults)


def _make_review_config(**overrides: Any) -> ReviewConfig:
    defaults = {
        "agent": "codex",
        "prompt": "impl_plan_review_default",
        "model": "gpt-5.1-codex",
        "max_review_rounds": 5,
        "exit_condition": "structured_approval",
        "on_max_rounds": "escalate",
    }
    defaults.update(overrides)
    return ReviewConfig(**defaults)


def _make_structured_output(
    tasks: list[dict[str, Any]] | None = None,
    plan_md: str = "# Plan\n\n## Tasks\n\n1. Setup\n2. Implement",
) -> dict[str, Any]:
    """Build a structured output dict matching HTN_TASK_OUTPUT_SCHEMA."""
    return {
        "plan_markdown": plan_md,
        "tasks": tasks or [
            {
                "name": "setup_db",
                "description": "Initialize the database schema",
                "task_type": "primitive",
                "parent_name": None,
                "priority": 10,
                "ordering": 0,
                "preconditions": [],
                "postconditions": [
                    {"type": "file_exists", "path": "src/db.py"}
                ],
                "invariants": None,
                "estimated_complexity": "small",
                "dependencies": [],
            },
            {
                "name": "impl_api",
                "description": "Implement the API endpoints",
                "task_type": "primitive",
                "parent_name": None,
                "priority": 5,
                "ordering": 1,
                "preconditions": [],
                "postconditions": [],
                "estimated_complexity": "medium",
                "dependencies": ["setup_db"],
            },
        ],
    }


def _make_mock_session(
    output: str = "# Plan\n\nDetailed implementation plan.",
    session_id: str | None = "sess-plan-1",
    structured_output: dict[str, Any] | None = None,
    context_usage: dict[str, Any] | None = None,
) -> AsyncMock:
    """Build a mock LiveSession for the primary agent."""
    session = AsyncMock()
    session.session_id = session_id
    session.send_turn.return_value = FakeTurnResult(
        output=output,
        structured_output=structured_output,
    )
    session.get_context_usage.return_value = context_usage or {
        "total_tokens": 1000,
        "max_tokens": 100000,
    }
    return session


def _make_mock_adapter(session: AsyncMock | None = None) -> AsyncMock:
    adapter = AsyncMock()
    adapter.start_session.return_value = session or _make_mock_session()
    return adapter


def _make_mock_review_adapter(
    structured: dict[str, Any] | None = None,
) -> AsyncMock:
    adapter = AsyncMock()
    review_session = AsyncMock()
    review_session.session_id = "review-sess-1"
    review_session.send_turn.return_value = FakeTurnResult(
        output="review feedback",
        structured_output=structured
        or {
            "approved": True,
            "max_severity": "none",
            "issues": [],
            "feedback_markdown": "LGTM",
        },
    )
    adapter.start_session.return_value = review_session
    return adapter


def _approved_output(
    max_severity: str = "low",
    issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "approved": True,
        "max_severity": max_severity,
        "issues": issues or [],
        "feedback_markdown": "Looks good!",
    }


def _rejected_output(
    max_severity: str = "medium",
    issues: list[dict[str, Any]] | None = None,
    feedback: str = "Needs work.",
) -> dict[str, Any]:
    return {
        "approved": False,
        "max_severity": max_severity,
        "issues": issues
        or [{"severity": max_severity, "description": "Fix something"}],
        "feedback_markdown": feedback,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def log_buffer() -> LogBuffer:
    return LogBuffer()


@pytest.fixture
def cancel_event() -> asyncio.Event:
    return asyncio.Event()


@pytest.fixture
def tmp_pipelines_dir(tmp_path: Path) -> Path:
    return tmp_path / "pipelines"


@pytest.fixture
async def pool_with_stage(initialized_db, tmp_path):
    """Provide an async pool with a seeded pipeline + pipeline_stage for impl_plan.

    Uses tmp_path for clone_path so sync_to_markdown can write to disk.
    Yields (pool, pipeline_id, stage_id).
    """
    from build_your_room.db import get_pool

    pool = get_pool()
    clone_path = str(tmp_path / "clone")
    (tmp_path / "clone" / "specs").mkdir(parents=True)

    async with pool.connection() as conn:
        repo_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO repos (name, local_path) "
                "VALUES ('test-repo', %s) RETURNING id",
                (clone_path,),
            )
        ).fetchone()
        repo_id = repo_row["id"]

        graph_json = json.dumps({
            "entry_stage": "impl_plan",
            "nodes": [
                {
                    "key": "impl_plan",
                    "name": "Implementation plan",
                    "type": "impl_plan",
                    "agent": "claude",
                    "prompt": "impl_plan_default",
                    "model": "claude-opus-4-6",
                    "max_iterations": 1,
                }
            ],
            "edges": [],
        })
        pdef_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO pipeline_defs (name, stage_graph_json) "
                "VALUES ('test-plan-def', %s) RETURNING id",
                (graph_json,),
            )
        ).fetchone()
        pdef_id = pdef_row["id"]

        p_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO pipelines "
                "(pipeline_def_id, repo_id, clone_path, review_base_rev, status) "
                "VALUES (%s, %s, %s, 'abc123', 'running') RETURNING id",
                (pdef_id, repo_id, clone_path),
            )
        ).fetchone()
        pipeline_id = p_row["id"]

        stage_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO pipeline_stages "
                "(pipeline_id, stage_key, attempt, stage_type, agent_type, "
                " status, max_iterations, started_at) "
                "VALUES (%s, 'impl_plan', 1, 'impl_plan', 'claude', "
                "'running', 1, now()) RETURNING id",
                (pipeline_id,),
            )
        ).fetchone()
        stage_id = stage_row["id"]

        await conn.commit()

    yield pool, pipeline_id, stage_id


# ---------------------------------------------------------------------------
# Unit tests — artifact path
# ---------------------------------------------------------------------------


class TestArtifactPath:
    def test_produces_correct_path(self, tmp_path: Path) -> None:
        """Artifact path follows convention: pipelines_dir/<id>/artifacts/plan.md.

        Invariant: plan artifact naming is consistent and distinct from spec.md.
        """
        result = _artifact_path(tmp_path, 42)
        assert result == tmp_path / "42" / "artifacts" / "plan.md"

    @given(pipeline_id=st.integers(min_value=1, max_value=9999))
    def test_path_contains_pipeline_id(self, pipeline_id: int) -> None:
        """Property: pipeline ID appears as a directory component.

        Invariant: for all valid IDs, the artifact is stored under the
        pipeline-specific directory for isolation.
        """
        base = Path("/tmp/pipelines")
        result = _artifact_path(base, pipeline_id)
        assert str(pipeline_id) in str(result)
        assert result.name == "plan.md"


# ---------------------------------------------------------------------------
# Unit tests — spec artifact loading
# ---------------------------------------------------------------------------


class TestLoadSpecArtifact:
    def test_loads_existing_spec(self, tmp_path: Path) -> None:
        """_load_spec_artifact reads the spec.md from the artifacts dir.

        Invariant: when a spec exists, its content is returned verbatim
        so the planner gets full context from the prior stage.
        """
        spec_dir = tmp_path / "1" / "artifacts"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text("# My Spec\n\nRequirements here.")

        result = _load_spec_artifact(tmp_path, 1)
        assert result == "# My Spec\n\nRequirements here."

    def test_returns_none_when_no_spec(self, tmp_path: Path) -> None:
        """_load_spec_artifact returns None when no spec artifact exists.

        Invariant: missing spec artifacts are handled gracefully —
        the planner can still operate without prior stage context.
        """
        result = _load_spec_artifact(tmp_path, 999)
        assert result is None


# ---------------------------------------------------------------------------
# Unit tests — output parsing
# ---------------------------------------------------------------------------


class TestParsePlanOutput:
    def test_parses_structured_output(self) -> None:
        """Structured output is preferred over raw text.

        Invariant: when structured output contains plan_markdown and tasks,
        both are extracted correctly.
        """
        structured = _make_structured_output()
        turn = FakeTurnResult(
            output="raw text ignored",
            structured_output=structured,
        )
        plan_md, tasks = _parse_plan_output(turn)
        assert "# Plan" in plan_md
        assert len(tasks) == 2
        assert tasks[0]["name"] == "setup_db"
        assert tasks[1]["name"] == "impl_api"

    def test_falls_back_to_raw_output(self) -> None:
        """Falls back to raw output when structured output is None.

        Invariant: the plan markdown comes from turn_result.output when
        no structured output is available.
        """
        turn = FakeTurnResult(
            output="# Fallback Plan\n\nSome content.",
            structured_output=None,
        )
        plan_md, tasks = _parse_plan_output(turn)
        assert plan_md == "# Fallback Plan\n\nSome content."
        assert tasks == []

    def test_handles_structured_without_tasks_key(self) -> None:
        """Handles structured output missing the 'tasks' key gracefully.

        Invariant: malformed structured output falls back to raw parsing
        rather than crashing.
        """
        turn = FakeTurnResult(
            output="raw plan",
            structured_output={"plan_markdown": "# Plan", "other_key": "value"},
        )
        plan_md, tasks = _parse_plan_output(turn)
        assert plan_md == "# Plan"
        assert tasks == []


class TestTryExtractTasksJson:
    def test_extracts_from_fenced_json_block(self) -> None:
        """Extracts tasks from a ```json fenced block.

        Invariant: fenced JSON blocks with a 'tasks' key are parsed correctly.
        """
        text = '''Here is the plan:

```json
{"tasks": [{"name": "task_a", "description": "Do A"}]}
```

Done.'''
        result = _try_extract_tasks_json(text)
        assert len(result) == 1
        assert result[0]["name"] == "task_a"

    def test_extracts_from_fenced_json_array(self) -> None:
        """Extracts tasks from a fenced JSON array (no wrapper object).

        Invariant: bare JSON arrays in fenced blocks are also parsed.
        """
        text = '''```json
[{"name": "task_a", "description": "Do A"}]
```'''
        result = _try_extract_tasks_json(text)
        assert len(result) == 1
        assert result[0]["name"] == "task_a"

    def test_extracts_from_inline_json_object(self) -> None:
        """Extracts tasks from an inline JSON object in raw text.

        Invariant: JSON objects found by scanning for '{' are parsed.
        """
        text = 'Plan output: {"tasks": [{"name": "t1", "description": "Do 1"}]}'
        result = _try_extract_tasks_json(text)
        assert len(result) == 1
        assert result[0]["name"] == "t1"

    def test_returns_empty_on_no_json(self) -> None:
        """Returns empty list when no JSON is found.

        Invariant: non-JSON text produces an empty task list rather than
        crashing.
        """
        result = _try_extract_tasks_json("Just a plain text plan with no JSON.")
        assert result == []

    def test_returns_empty_on_malformed_json(self) -> None:
        """Returns empty list when JSON is malformed.

        Invariant: malformed JSON is handled gracefully.
        """
        result = _try_extract_tasks_json('{"tasks": [{"name": broken}]}')
        assert result == []


# ---------------------------------------------------------------------------
# Unit tests — parse_htn_tasks
# ---------------------------------------------------------------------------


class TestParseHtnTasks:
    def test_parses_valid_tasks(self) -> None:
        """Valid structured output with tasks returns the task list.

        Invariant: well-formed input produces a non-None list.
        """
        structured = _make_structured_output()
        result = parse_htn_tasks(structured)
        assert result is not None
        assert len(result) == 2
        assert result[0]["name"] == "setup_db"

    def test_returns_none_for_none_input(self) -> None:
        """None input returns None.

        Invariant: missing structured output is handled gracefully.
        """
        assert parse_htn_tasks(None) is None

    def test_returns_none_for_missing_tasks_key(self) -> None:
        """Structured output without 'tasks' returns None.

        Invariant: missing required key is detected.
        """
        assert parse_htn_tasks({"plan_markdown": "# Plan"}) is None

    def test_returns_none_for_non_list_tasks(self) -> None:
        """Structured output with non-list 'tasks' returns None.

        Invariant: type mismatch is detected.
        """
        assert parse_htn_tasks({"tasks": "not a list"}) is None

    def test_returns_none_for_task_missing_name(self) -> None:
        """Tasks without required 'name' field return None.

        Invariant: each task must have name and description.
        """
        assert parse_htn_tasks({"tasks": [{"description": "no name"}]}) is None

    def test_returns_none_for_task_missing_description(self) -> None:
        """Tasks without required 'description' field return None.

        Invariant: each task must have name and description.
        """
        assert parse_htn_tasks({"tasks": [{"name": "no_desc"}]}) is None

    @given(tasks=htn_task_lists())
    @settings(max_examples=15)
    def test_valid_tasks_always_parse(self, tasks: list[dict[str, Any]]) -> None:
        """Property: any well-formed task list with name+description parses.

        Invariant: for all valid task dicts, parse_htn_tasks returns
        a non-None list of the same length.
        """
        result = parse_htn_tasks({"tasks": tasks})
        assert result is not None
        assert len(result) == len(tasks)


# ---------------------------------------------------------------------------
# Integration tests — happy path (no review)
# ---------------------------------------------------------------------------


class TestImplPlanNoReview:
    @pytest.mark.asyncio
    async def test_produces_artifact_and_returns_approved(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Plan authored without review returns approved and saves artifact.

        Invariant: no-review mode produces a plan artifact on disk,
        records it in the DB, and returns APPROVED.
        """
        pool, pipeline_id, stage_id = pool_with_stage
        structured = _make_structured_output()
        session = _make_mock_session(structured_output=structured)
        adapter = _make_mock_adapter(session)
        node = _make_node()

        result = await run_impl_plan_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=node,
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        assert result == STAGE_RESULT_APPROVED

        artifact = _artifact_path(tmp_pipelines_dir, pipeline_id)
        assert artifact.exists()
        assert "Plan" in artifact.read_text()

        adapter.start_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_populates_htn_tasks(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """HTN tasks from structured output are populated in the DB.

        Invariant: after the stage completes, htn_tasks rows exist in the
        DB matching the structured output from the agent.
        """
        pool, pipeline_id, stage_id = pool_with_stage
        tasks = [
            {
                "name": "create_schema",
                "description": "Create the database schema",
                "task_type": "primitive",
                "priority": 10,
                "ordering": 0,
                "preconditions": [],
                "postconditions": [],
                "dependencies": [],
            },
            {
                "name": "write_api",
                "description": "Write the REST API",
                "task_type": "primitive",
                "priority": 5,
                "ordering": 1,
                "preconditions": [],
                "postconditions": [],
                "dependencies": ["create_schema"],
            },
        ]
        structured = _make_structured_output(tasks=tasks)
        session = _make_mock_session(structured_output=structured)
        adapter = _make_mock_adapter(session)

        await run_impl_plan_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        # Verify tasks were created in DB
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT name, description, task_type, priority "
                    "FROM htn_tasks WHERE pipeline_id = %s ORDER BY ordering",
                    (pipeline_id,),
                )
            ).fetchall()

        assert len(rows) == 2
        assert rows[0]["name"] == "create_schema"
        assert rows[1]["name"] == "write_api"

    @pytest.mark.asyncio
    async def test_populates_htn_dependencies(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """HTN task dependencies are created in htn_task_deps.

        Invariant: dependency edges from the structured output are
        persisted so readiness propagation works correctly.
        """
        pool, pipeline_id, stage_id = pool_with_stage
        tasks = [
            {
                "name": "dep_first",
                "description": "First task",
                "task_type": "primitive",
                "priority": 10,
                "ordering": 0,
                "preconditions": [],
                "postconditions": [],
                "dependencies": [],
            },
            {
                "name": "dep_second",
                "description": "Depends on first",
                "task_type": "primitive",
                "priority": 5,
                "ordering": 1,
                "preconditions": [],
                "postconditions": [],
                "dependencies": ["dep_first"],
            },
        ]
        structured = _make_structured_output(tasks=tasks)
        session = _make_mock_session(structured_output=structured)
        adapter = _make_mock_adapter(session)

        await run_impl_plan_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        # Verify dependency edges
        async with pool.connection() as conn:
            deps = await (
                await conn.execute(
                    "SELECT td.dep_type, t1.name AS task_name, t2.name AS dep_name "
                    "FROM htn_task_deps td "
                    "JOIN htn_tasks t1 ON td.task_id = t1.id "
                    "JOIN htn_tasks t2 ON td.depends_on_task_id = t2.id "
                    "WHERE t1.pipeline_id = %s",
                    (pipeline_id,),
                )
            ).fetchall()

        assert len(deps) == 1
        assert deps[0]["task_name"] == "dep_second"
        assert deps[0]["dep_name"] == "dep_first"
        assert deps[0]["dep_type"] == "hard"

    @pytest.mark.asyncio
    async def test_no_tasks_in_output_skips_population(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Empty task list in output skips HTN population without error.

        Invariant: missing or empty tasks in the agent output is a
        non-fatal condition — the plan artifact is still saved.
        """
        pool, pipeline_id, stage_id = pool_with_stage
        session = _make_mock_session(
            output="# Plan\n\nNo structured tasks.",
            structured_output=None,
        )
        adapter = _make_mock_adapter(session)

        result = await run_impl_plan_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        assert result == STAGE_RESULT_APPROVED

        history = log_buffer.get_history(pipeline_id)
        assert any("skipping population" in msg.lower() for msg in history)

    @pytest.mark.asyncio
    async def test_creates_agent_session_row(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """An agent_sessions row is created and completed.

        Invariant: every stage run creates a trackable session record.
        """
        pool, pipeline_id, stage_id = pool_with_stage
        structured = _make_structured_output()
        session = _make_mock_session(structured_output=structured)
        adapter = _make_mock_adapter(session)

        await run_impl_plan_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT * FROM agent_sessions WHERE pipeline_stage_id = %s",
                    (stage_id,),
                )
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["session_type"] == "claude"
        assert rows[0]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_session_closed_on_completion(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Primary session is always closed after the stage.

        Invariant: the finally block ensures session cleanup.
        """
        pool, pipeline_id, stage_id = pool_with_stage
        mock_session = _make_mock_session(
            structured_output=_make_structured_output()
        )
        adapter = _make_mock_adapter(mock_session)

        await run_impl_plan_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        mock_session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_spec_artifact_loaded_as_context(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """When a spec artifact exists, it's appended to the prompt.

        Invariant: the planner receives the spec context from the previous
        stage so it can produce an informed plan.
        """
        pool, pipeline_id, stage_id = pool_with_stage

        # Write a spec artifact
        spec_dir = tmp_pipelines_dir / str(pipeline_id) / "artifacts"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text("# Spec\n\nBuild a widget.")

        structured = _make_structured_output()
        session = _make_mock_session(structured_output=structured)
        adapter = _make_mock_adapter(session)

        await run_impl_plan_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        # Verify the adapter received a config with spec context in the prompt
        call_args = adapter.start_session.call_args
        config: SessionConfig = call_args[0][0] if call_args[0] else call_args[1]["config"]
        assert "Build a widget" in config.system_prompt


# ---------------------------------------------------------------------------
# Integration tests — with review loop
# ---------------------------------------------------------------------------


class TestImplPlanWithReview:
    @pytest.mark.asyncio
    async def test_approved_after_review(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Plan goes through review loop and gets approved.

        Invariant: review approval leads to APPROVED result and
        HTN tasks are still populated after review.
        """
        pool, pipeline_id, stage_id = pool_with_stage
        review_config = _make_review_config()
        node = _make_node(review=review_config)

        structured = _make_structured_output()
        session = _make_mock_session(structured_output=structured)
        primary_adapter = _make_mock_adapter(session)
        review_adapter = _make_mock_review_adapter(_approved_output("none"))

        approved_outcome = ReviewLoopOutcome(
            approved=True,
            rounds_completed=1,
            last_review=None,
        )

        with patch(
            "build_your_room.stages.impl_plan.run_review_loop",
            return_value=approved_outcome,
        ):
            result = await run_impl_plan_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"claude": primary_adapter, "codex": review_adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
            )

        assert result == STAGE_RESULT_APPROVED

        # HTN tasks should be populated
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT name FROM htn_tasks WHERE pipeline_id = %s",
                    (pipeline_id,),
                )
            ).fetchall()
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_escalates_on_max_rounds(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Review loop escalation prevents HTN population.

        Invariant: when the plan is escalated, HTN tasks are NOT populated
        because the plan was not approved. An escalation row is created.
        """
        pool, pipeline_id, stage_id = pool_with_stage
        review_config = _make_review_config(max_review_rounds=1, on_max_rounds="escalate")
        node = _make_node(review=review_config)

        structured = _make_structured_output()
        session = _make_mock_session(structured_output=structured)
        primary_adapter = _make_mock_adapter(session)
        review_adapter = _make_mock_review_adapter(_rejected_output("medium"))

        escalated_outcome = ReviewLoopOutcome(
            approved=False,
            escalated=True,
            escalation_reason="max_iterations",
            rounds_completed=1,
        )

        with patch(
            "build_your_room.stages.impl_plan.run_review_loop",
            return_value=escalated_outcome,
        ):
            result = await run_impl_plan_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"claude": primary_adapter, "codex": review_adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
            )

        assert result == STAGE_RESULT_ESCALATED

        # HTN tasks should NOT have been populated
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT name FROM htn_tasks WHERE pipeline_id = %s",
                    (pipeline_id,),
                )
            ).fetchall()
        assert len(rows) == 0

        # Escalation row should exist
        async with pool.connection() as conn:
            esc = await (
                await conn.execute(
                    "SELECT reason FROM escalations WHERE pipeline_id = %s",
                    (pipeline_id,),
                )
            ).fetchone()
        assert esc is not None
        assert esc["reason"] == "max_iterations"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestImplPlanEdgeCases:
    @pytest.mark.asyncio
    async def test_missing_primary_adapter_escalates(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Missing adapter for the node's agent type triggers escalation.

        Invariant: graceful failure with ESCALATED, not a crash.
        """
        pool, pipeline_id, stage_id = pool_with_stage

        result = await run_impl_plan_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        assert result == STAGE_RESULT_ESCALATED

    @pytest.mark.asyncio
    async def test_cancel_before_session_start(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Pre-set cancel event aborts before starting a session.

        Invariant: no agent interaction occurs when already cancelled.
        """
        pool, pipeline_id, stage_id = pool_with_stage
        adapter = _make_mock_adapter()
        cancel = asyncio.Event()
        cancel.set()

        result = await run_impl_plan_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel,
            pipelines_dir=tmp_pipelines_dir,
        )

        assert result == STAGE_RESULT_ESCALATED
        adapter.start_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_failure_marks_failed(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Exception during session marks it as failed and re-raises.

        Invariant: session rows reflect failures for observability.
        """
        pool, pipeline_id, stage_id = pool_with_stage
        mock_session = _make_mock_session()
        mock_session.send_turn.side_effect = RuntimeError("LLM error")
        adapter = _make_mock_adapter(mock_session)

        with pytest.raises(RuntimeError, match="LLM error"):
            await run_impl_plan_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=_make_node(),
                adapters={"claude": adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
            )

        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT status FROM agent_sessions WHERE pipeline_stage_id = %s",
                    (stage_id,),
                )
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        mock_session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_review_adapter_skips_review(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Missing review adapter skips review and still populates tasks.

        Invariant: a missing reviewer doesn't block plan acceptance or
        HTN task population.
        """
        pool, pipeline_id, stage_id = pool_with_stage
        review_config = _make_review_config()
        node = _make_node(review=review_config)

        structured = _make_structured_output()
        session = _make_mock_session(structured_output=structured)
        primary_adapter = _make_mock_adapter(session)

        result = await run_impl_plan_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=node,
            adapters={"claude": primary_adapter},  # no codex
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        assert result == STAGE_RESULT_APPROVED

        # Tasks should still be populated
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT name FROM htn_tasks WHERE pipeline_id = %s",
                    (pipeline_id,),
                )
            ).fetchall()
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_session_id_none_handled(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Sessions with no provider session_id don't crash.

        Invariant: null session IDs are handled gracefully.
        """
        pool, pipeline_id, stage_id = pool_with_stage
        structured = _make_structured_output()
        session = _make_mock_session(session_id=None, structured_output=structured)
        adapter = _make_mock_adapter(session)

        result = await run_impl_plan_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        assert result == STAGE_RESULT_APPROVED


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestImplPlanProperties:
    @given(tasks=htn_task_lists())
    @settings(max_examples=15)
    def test_parse_structured_output_roundtrip(
        self, tasks: list[dict[str, Any]]
    ) -> None:
        """Property: structured output with valid tasks always parses correctly.

        Invariant: for all well-formed task lists, _parse_plan_output
        extracts the tasks without data loss.
        """
        structured = {
            "plan_markdown": "# Plan",
            "tasks": tasks,
        }
        turn = FakeTurnResult(
            output="raw fallback",
            structured_output=structured,
        )
        _, extracted = _parse_plan_output(turn)
        assert len(extracted) == len(tasks)
        for orig, parsed in zip(tasks, extracted):
            assert orig["name"] == parsed["name"]
            assert orig["description"] == parsed["description"]

    @given(
        pipeline_id=st.integers(min_value=1, max_value=9999),
        content=st.text(
            min_size=1,
            max_size=200,
            alphabet=st.characters(blacklist_characters="\r"),
        ),
    )
    @settings(max_examples=15)
    def test_artifact_write_roundtrip(
        self, pipeline_id: int, content: str
    ) -> None:
        """Property: artifact write-read roundtrip preserves content.

        Invariant: for all valid content strings, writing to the artifact
        path and reading back produces identical text.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            path = _artifact_path(Path(td), pipeline_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            assert path.read_text() == content

    @given(tasks=htn_task_lists())
    @settings(max_examples=10)
    def test_try_extract_from_json_fenced_block(
        self, tasks: list[dict[str, Any]]
    ) -> None:
        """Property: tasks serialized to a fenced JSON block are extractable.

        Invariant: the fallback parser can reconstruct task lists from
        the fenced block format that agents commonly produce.
        """
        json_block = json.dumps({"tasks": tasks})
        text = f"Here is the plan:\n\n```json\n{json_block}\n```\n\nDone."
        extracted = _try_extract_tasks_json(text)
        assert len(extracted) == len(tasks)
        for orig, parsed in zip(tasks, extracted):
            assert orig["name"] == parsed["name"]

    def test_htn_schema_has_required_fields(self) -> None:
        """The HTN output schema requires plan_markdown and tasks.

        Invariant: the schema always specifies the minimum required
        fields for HTN task population.
        """
        assert "plan_markdown" in HTN_TASK_OUTPUT_SCHEMA["properties"]
        assert "tasks" in HTN_TASK_OUTPUT_SCHEMA["properties"]
        assert "plan_markdown" in HTN_TASK_OUTPUT_SCHEMA["required"]
        assert "tasks" in HTN_TASK_OUTPUT_SCHEMA["required"]
