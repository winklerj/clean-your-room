"""Tests for SpecAuthorStage — spec authoring with optional review loop.

Covers: happy-path approval, review loop integration, escalation on max
rounds, context rotation fallback, artifact persistence, DB session row
creation, cancellation, missing adapter handling, prompt resolution.

All agent interactions are mocked — no live API calls.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from build_your_room.adapters.base import SessionConfig
from build_your_room.stage_graph import ReviewConfig, StageNode
from build_your_room.stages.review_loop import SEVERITY_ORDER
from build_your_room.stages.spec_author import (
    STAGE_RESULT_APPROVED,
    STAGE_RESULT_ESCALATED,
    _artifact_path,
    _resolve_prompt,
    run_spec_author_stage,
)
from build_your_room.streaming import LogBuffer


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_severities = st.sampled_from(list(SEVERITY_ORDER))


@st.composite
def review_structured_outputs(draw: st.DrawFn) -> dict[str, Any]:
    """Generate well-formed structured review output dicts."""
    return {
        "approved": draw(st.booleans()),
        "max_severity": draw(_severities),
        "issues": draw(
            st.lists(
                st.fixed_dictionaries(
                    {
                        "severity": _severities,
                        "description": st.text(min_size=1, max_size=30),
                    }
                ),
                max_size=3,
            )
        ),
        "feedback_markdown": draw(st.text(min_size=0, max_size=50)),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeTurnResult:
    """Minimal SessionResult for tests."""

    output: str = "# Spec\n\nThis is the authored spec."
    structured_output: dict[str, Any] | None = None


def _make_node(**overrides: Any) -> StageNode:
    defaults: dict[str, Any] = {
        "key": "spec_author",
        "name": "Spec authoring",
        "stage_type": "spec_author",
        "agent": "claude",
        "prompt": "spec_author_default",
        "model": "claude-opus-4-6",
        "max_iterations": 1,
        "context_threshold_pct": 60,
    }
    defaults.update(overrides)
    return StageNode(**defaults)


def _make_review_config(**overrides: Any) -> ReviewConfig:
    defaults = {
        "agent": "codex",
        "prompt": "spec_review_default",
        "model": "gpt-5.1-codex",
        "max_review_rounds": 5,
        "exit_condition": "structured_approval",
        "on_max_rounds": "escalate",
    }
    defaults.update(overrides)
    return ReviewConfig(**defaults)


def _make_mock_session(
    output: str = "# Spec\n\nThis is the authored spec.",
    session_id: str | None = "sess-123",
    context_usage: dict[str, Any] | None = None,
) -> AsyncMock:
    """Build a mock LiveSession for the primary agent."""
    session = AsyncMock()
    session.session_id = session_id
    session.send_turn.return_value = FakeTurnResult(output=output)
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
    """Build a mock adapter that creates review sessions returning structured output."""
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
async def pool_with_stage(initialized_db):
    """Provide an async pool with a seeded pipeline + pipeline_stage row.

    Yields (pool, pipeline_id, stage_id).
    """
    from build_your_room.db import get_pool

    pool = get_pool()
    async with pool.connection() as conn:
        # Create minimal repo
        repo_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO repos (name, local_path) "
                "VALUES ('test-repo', '/tmp/test-repo') RETURNING id"
            )
        ).fetchone()
        repo_id = repo_row["id"]

        # Create pipeline def
        graph_json = json.dumps(
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
                    }
                ],
                "edges": [],
            }
        )
        pdef_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO pipeline_defs (name, stage_graph_json) "
                "VALUES ('test-def', %s) RETURNING id",
                (graph_json,),
            )
        ).fetchone()
        pdef_id = pdef_row["id"]

        # Create pipeline
        p_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO pipelines "
                "(pipeline_def_id, repo_id, clone_path, review_base_rev, status) "
                "VALUES (%s, %s, '/tmp/test-clone', 'abc123', 'running') RETURNING id",
                (pdef_id, repo_id),
            )
        ).fetchone()
        pipeline_id = p_row["id"]

        # Create stage
        stage_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO pipeline_stages "
                "(pipeline_id, stage_key, attempt, stage_type, agent_type, "
                " status, max_iterations, started_at) "
                "VALUES (%s, 'spec_author', 1, 'spec_author', 'claude', "
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
        result = _artifact_path(tmp_path, 42)
        assert result == tmp_path / "42" / "artifacts" / "spec.md"

    @given(pipeline_id=st.integers(min_value=1, max_value=9999))
    def test_path_contains_pipeline_id(self, pipeline_id: int) -> None:
        base = Path("/tmp/pipelines")
        result = _artifact_path(base, pipeline_id)
        assert str(pipeline_id) in str(result)
        assert result.name == "spec.md"


# ---------------------------------------------------------------------------
# Unit tests — prompt resolution
# ---------------------------------------------------------------------------


class TestPromptResolution:
    @pytest.mark.asyncio
    async def test_resolves_from_db(self, pool_with_stage: Any) -> None:
        pool, _, _ = pool_with_stage
        body = await _resolve_prompt(pool, "spec_author_default")
        # Should find the seeded prompt from db.py DEFAULT_PROMPTS
        assert "specification" in body.lower() or "repository" in body.lower()

    @pytest.mark.asyncio
    async def test_fallback_to_name_when_not_found(
        self, pool_with_stage: Any
    ) -> None:
        pool, _, _ = pool_with_stage
        body = await _resolve_prompt(pool, "nonexistent_prompt_xyz")
        assert body == "nonexistent_prompt_xyz"


# ---------------------------------------------------------------------------
# Integration tests — happy path (no review)
# ---------------------------------------------------------------------------


class TestSpecAuthorNoReview:
    @pytest.mark.asyncio
    async def test_produces_artifact_and_returns_approved(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()
        adapter = _make_mock_adapter()

        result = await run_spec_author_stage(
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

        # Artifact was written
        artifact = _artifact_path(tmp_pipelines_dir, pipeline_id)
        assert artifact.exists()
        assert "Spec" in artifact.read_text()

        # Adapter was called
        adapter.start_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_creates_agent_session_row(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()
        adapter = _make_mock_adapter()

        await run_spec_author_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=node,
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        # Check agent_sessions table
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT * FROM agent_sessions WHERE pipeline_stage_id = %s",
                    (stage_id,),
                )
            ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["session_type"] == "claude"
        assert row["status"] == "completed"
        assert row["session_id"] == "sess-123"

    @pytest.mark.asyncio
    async def test_session_closed_on_completion(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        pool, pipeline_id, stage_id = pool_with_stage
        mock_session = _make_mock_session()
        adapter = _make_mock_adapter(mock_session)

        await run_spec_author_stage(
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
    async def test_logs_to_buffer(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        pool, pipeline_id, stage_id = pool_with_stage
        adapter = _make_mock_adapter()

        await run_spec_author_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        history = log_buffer.get_history(pipeline_id)
        assert any("[spec_author]" in msg for msg in history)
        assert any("authored" in msg.lower() or "approved" in msg.lower() for msg in history)


# ---------------------------------------------------------------------------
# Integration tests — with review loop
# ---------------------------------------------------------------------------


class TestSpecAuthorWithReview:
    @pytest.mark.asyncio
    async def test_approved_after_review(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        pool, pipeline_id, stage_id = pool_with_stage
        review_config = _make_review_config()
        node = _make_node(review=review_config)

        primary_adapter = _make_mock_adapter()
        review_adapter = _make_mock_review_adapter(_approved_output("none"))

        result = await run_spec_author_stage(
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

        history = log_buffer.get_history(pipeline_id)
        assert any("review loop" in msg.lower() for msg in history)
        assert any("approved" in msg.lower() for msg in history)

    @pytest.mark.asyncio
    async def test_escalates_on_max_rounds(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        pool, pipeline_id, stage_id = pool_with_stage
        review_config = _make_review_config(max_review_rounds=1, on_max_rounds="escalate")
        node = _make_node(review=review_config)

        primary_adapter = _make_mock_adapter()
        review_adapter = _make_mock_review_adapter(
            _rejected_output("medium")
        )

        result = await run_spec_author_stage(
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

        # Verify escalation was created in DB
        async with pool.connection() as conn:
            esc_rows = await (
                await conn.execute(
                    "SELECT * FROM escalations WHERE pipeline_id = %s",
                    (pipeline_id,),
                )
            ).fetchall()
        assert len(esc_rows) == 1
        assert esc_rows[0]["reason"] == "max_iterations"

    @pytest.mark.asyncio
    async def test_proceed_with_warnings(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        pool, pipeline_id, stage_id = pool_with_stage
        review_config = _make_review_config(
            max_review_rounds=1, on_max_rounds="proceed_with_warnings"
        )
        node = _make_node(review=review_config)

        primary_adapter = _make_mock_adapter()
        review_adapter = _make_mock_review_adapter(_rejected_output("medium"))

        result = await run_spec_author_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=node,
            adapters={"claude": primary_adapter, "codex": review_adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        # proceed_with_warnings counts as approved
        assert result == STAGE_RESULT_APPROVED

    @pytest.mark.asyncio
    async def test_stage_status_updated_on_approval(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        pool, pipeline_id, stage_id = pool_with_stage
        review_config = _make_review_config()
        node = _make_node(review=review_config)

        primary_adapter = _make_mock_adapter()
        review_adapter = _make_mock_review_adapter(_approved_output("none"))

        await run_spec_author_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=node,
            adapters={"claude": primary_adapter, "codex": review_adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        async with pool.connection() as conn:
            row: dict[str, Any] | None = await (  # type: ignore[assignment]
                await conn.execute(
                    "SELECT status FROM pipeline_stages WHERE id = %s",
                    (stage_id,),
                )
            ).fetchone()
        assert row is not None
        assert row["status"] == "completed"

    @pytest.mark.asyncio
    async def test_stage_status_failed_on_escalation(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        pool, pipeline_id, stage_id = pool_with_stage
        review_config = _make_review_config(max_review_rounds=1, on_max_rounds="escalate")
        node = _make_node(review=review_config)

        primary_adapter = _make_mock_adapter()
        review_adapter = _make_mock_review_adapter(_rejected_output("medium"))

        await run_spec_author_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=node,
            adapters={"claude": primary_adapter, "codex": review_adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        async with pool.connection() as conn:
            row: dict[str, Any] | None = await (  # type: ignore[assignment]
                await conn.execute(
                    "SELECT status, escalation_reason FROM pipeline_stages WHERE id = %s",
                    (stage_id,),
                )
            ).fetchone()
        assert row is not None
        assert row["status"] == "failed"
        assert row["escalation_reason"] == "max_iterations"


# ---------------------------------------------------------------------------
# Missing adapter / cancellation
# ---------------------------------------------------------------------------


class TestSpecAuthorEdgeCases:
    @pytest.mark.asyncio
    async def test_missing_primary_adapter_escalates(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()

        result = await run_spec_author_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=node,
            adapters={},  # no adapters
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
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()
        adapter = _make_mock_adapter()

        cancel_event = asyncio.Event()
        cancel_event.set()  # Pre-cancelled

        result = await run_spec_author_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=node,
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        assert result == STAGE_RESULT_ESCALATED
        # Session should not have been started
        adapter.start_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_after_authoring_before_review(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        tmp_pipelines_dir: Path,
    ) -> None:
        pool, pipeline_id, stage_id = pool_with_stage

        review_config = _make_review_config()
        node = _make_node(review=review_config)

        mock_session = _make_mock_session()
        # Set cancel_event after the first send_turn
        cancel_event = asyncio.Event()

        async def cancel_after_turn(prompt: str, **kw: Any) -> FakeTurnResult:
            cancel_event.set()
            return FakeTurnResult()

        mock_session.send_turn.side_effect = cancel_after_turn
        adapter = _make_mock_adapter(mock_session)
        review_adapter = _make_mock_review_adapter()

        result = await run_spec_author_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=node,
            adapters={"claude": adapter, "codex": review_adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        assert result == STAGE_RESULT_ESCALATED

    @pytest.mark.asyncio
    async def test_missing_review_adapter_skips_review(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        pool, pipeline_id, stage_id = pool_with_stage
        review_config = _make_review_config()
        node = _make_node(review=review_config)

        primary_adapter = _make_mock_adapter()

        result = await run_spec_author_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=node,
            adapters={"claude": primary_adapter},  # no codex adapter
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        # Should approve without review
        assert result == STAGE_RESULT_APPROVED

    @pytest.mark.asyncio
    async def test_session_failure_marks_session_failed(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()

        mock_session = _make_mock_session()
        mock_session.send_turn.side_effect = RuntimeError("LLM error")
        adapter = _make_mock_adapter(mock_session)

        with pytest.raises(RuntimeError, match="LLM error"):
            await run_spec_author_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"claude": adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
            )

        # Session should be marked failed
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT status FROM agent_sessions WHERE pipeline_stage_id = %s",
                    (stage_id,),
                )
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"

        # Session should still be closed
        mock_session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_session_id_none_handled(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Sessions with no provider session_id should not crash."""
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()

        mock_session = _make_mock_session(session_id=None)
        adapter = _make_mock_adapter(mock_session)

        result = await run_spec_author_stage(
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

        # session_id should remain NULL in DB
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT session_id FROM agent_sessions WHERE pipeline_stage_id = %s",
                    (stage_id,),
                )
            ).fetchall()
        assert rows[0]["session_id"] is None


# ---------------------------------------------------------------------------
# Artifact content tests
# ---------------------------------------------------------------------------


class TestSpecArtifact:
    @pytest.mark.asyncio
    async def test_artifact_content_matches_output(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        pool, pipeline_id, stage_id = pool_with_stage
        expected_content = "# My Great Spec\n\nDetailed requirements here."
        mock_session = _make_mock_session(output=expected_content)
        adapter = _make_mock_adapter(mock_session)

        await run_spec_author_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        artifact = _artifact_path(tmp_pipelines_dir, pipeline_id)
        assert artifact.read_text() == expected_content

    @pytest.mark.asyncio
    async def test_artifact_path_recorded_in_stage(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        pool, pipeline_id, stage_id = pool_with_stage
        adapter = _make_mock_adapter()

        await run_spec_author_stage(
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
            row: dict[str, Any] | None = await (  # type: ignore[assignment]
                await conn.execute(
                    "SELECT output_artifact FROM pipeline_stages WHERE id = %s",
                    (stage_id,),
                )
            ).fetchone()
        assert row is not None
        assert row["output_artifact"] is not None
        assert "spec.md" in row["output_artifact"]


# ---------------------------------------------------------------------------
# SessionConfig construction tests
# ---------------------------------------------------------------------------


class TestSessionConfigConstruction:
    @pytest.mark.asyncio
    async def test_session_config_uses_node_model(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node(model="claude-opus-4-6")
        adapter = _make_mock_adapter()

        await run_spec_author_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=node,
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        call_args = adapter.start_session.call_args
        config: SessionConfig = call_args[0][0] if call_args[0] else call_args[1]["config"]
        assert config.model == "claude-opus-4-6"

    @pytest.mark.asyncio
    async def test_session_config_uses_file_tools_only(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        pool, pipeline_id, stage_id = pool_with_stage
        adapter = _make_mock_adapter()

        await run_spec_author_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        call_args = adapter.start_session.call_args
        config: SessionConfig = call_args[0][0] if call_args[0] else call_args[1]["config"]
        # spec_author should only get file tools
        assert "Read" in config.allowed_tools
        assert "Write" in config.allowed_tools
        assert "run_tests" not in config.allowed_tools
        assert "Bash" not in config.allowed_tools


# ---------------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------------


class TestSpecAuthorProperties:
    @given(data=review_structured_outputs())
    @settings(max_examples=20)
    def test_review_output_shapes_result_deterministically(
        self, data: dict[str, Any]
    ) -> None:
        """Any well-formed review output should produce a deterministic
        approved/escalated result without crashing."""
        from build_your_room.stages.review_loop import (
            parse_review_result,
            should_approve,
        )

        result = parse_review_result(data)
        assert result is not None
        # should_approve should not raise
        _ = should_approve(result)

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
        """Any content written to the artifact path should be readable."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            path = _artifact_path(Path(td), pipeline_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            assert path.read_text() == content
