"""Tests for CodeReviewStage — code review with bug-fix loop.

Covers: happy-path approval on first review, bug-fix loop (rejected then
approved), escalation on max rounds, proceed-with-warnings policy,
cancellation at multiple gates, missing adapters, empty diff handling,
diff artifact persistence, DB session rows, unparseable review output,
fix agent failure propagation, property-based tests for review decisions.

All agent interactions are mocked — no live API calls.
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

from build_your_room.stages.code_review import (
    STAGE_RESULT_APPROVED,
    STAGE_RESULT_ESCALATED,
    _build_fix_prompt,
    _diff_artifact_path,
    run_code_review_stage,
)
from build_your_room.stages.review_loop import (
    SEVERITY_ORDER,
    ReviewIssue,
    ReviewResult,
)
from build_your_room.stage_graph import StageNode
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

    output: str = "Fixed the issues."
    structured_output: dict[str, Any] | None = None


def _make_node(**overrides: Any) -> StageNode:
    defaults: dict[str, Any] = {
        "key": "code_review",
        "name": "Code review + bug fix",
        "stage_type": "code_review",
        "agent": "codex",
        "prompt": "code_review_default",
        "model": "gpt-5.1-codex",
        "max_iterations": 3,
        "fix_agent": "codex",
        "fix_prompt": "bug_fix_default",
        "on_max_rounds": "escalate",
    }
    defaults.update(overrides)
    return StageNode(**defaults)


def _approved_output(
    max_severity: str = "low",
    issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "approved": True,
        "max_severity": max_severity,
        "issues": issues or [],
        "feedback_markdown": "LGTM!",
    }


def _rejected_output(
    max_severity: str = "medium",
    issues: list[dict[str, Any]] | None = None,
    feedback: str = "Issues found.",
) -> dict[str, Any]:
    return {
        "approved": False,
        "max_severity": max_severity,
        "issues": issues
        or [{"severity": max_severity, "description": "Bug in auth module"}],
        "feedback_markdown": feedback,
    }


def _make_mock_session(
    output: str = "Fixed.",
    structured_output: dict[str, Any] | None = None,
    session_id: str | None = "review-sess-1",
) -> AsyncMock:
    """Build a mock LiveSession."""
    session = AsyncMock()
    session.session_id = session_id
    session.send_turn.return_value = FakeTurnResult(
        output=output, structured_output=structured_output
    )
    return session


def _make_mock_adapter(session: AsyncMock | None = None) -> AsyncMock:
    adapter = AsyncMock()
    adapter.start_session.return_value = session or _make_mock_session()
    return adapter


def _make_review_adapter(structured: dict[str, Any] | None = None) -> AsyncMock:
    """Build a mock adapter returning structured review output."""
    return _make_mock_adapter(
        _make_mock_session(
            output="review feedback",
            structured_output=structured or _approved_output("none"),
            session_id="review-sess-1",
        )
    )


def _make_fix_adapter() -> AsyncMock:
    """Build a mock adapter for the fix agent."""
    return _make_mock_adapter(
        _make_mock_session(output="Fixed all issues.", session_id="fix-sess-1")
    )


# Mock for _capture_full_diff — avoids needing a real git repo
_MOCK_DIFF = """\
diff --git a/src/main.py b/src/main.py
index abc1234..def5678 100644
--- a/src/main.py
+++ b/src/main.py
@@ -1,3 +1,5 @@
+import os
+
 def main():
-    pass
+    print("hello")
"""


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
        repo_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO repos (name, local_path) "
                "VALUES ('test-repo-cr', '/tmp/test-repo-cr') RETURNING id"
            )
        ).fetchone()
        repo_id = repo_row["id"]

        graph_json = json.dumps(
            {
                "entry_stage": "code_review",
                "nodes": [
                    {
                        "key": "code_review",
                        "name": "Code review",
                        "type": "code_review",
                        "agent": "codex",
                        "prompt": "code_review_default",
                        "model": "gpt-5.1-codex",
                        "max_iterations": 3,
                        "fix_agent": "codex",
                        "fix_prompt": "bug_fix_default",
                        "on_max_rounds": "escalate",
                    }
                ],
                "edges": [],
            }
        )
        pdef_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO pipeline_defs (name, stage_graph_json) "
                "VALUES ('test-def-cr', %s) RETURNING id",
                (graph_json,),
            )
        ).fetchone()
        pdef_id = pdef_row["id"]

        p_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO pipelines "
                "(pipeline_def_id, repo_id, clone_path, review_base_rev, "
                " head_rev, status) "
                "VALUES (%s, %s, '/tmp/test-clone-cr', 'abc123', 'def456', 'running') "
                "RETURNING id",
                (pdef_id, repo_id),
            )
        ).fetchone()
        pipeline_id = p_row["id"]

        stage_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO pipeline_stages "
                "(pipeline_id, stage_key, attempt, stage_type, agent_type, "
                " status, max_iterations, started_at) "
                "VALUES (%s, 'code_review', 1, 'code_review', 'codex', "
                "'running', 3, now()) RETURNING id",
                (pipeline_id,),
            )
        ).fetchone()
        stage_id = stage_row["id"]

        await conn.commit()

    yield pool, pipeline_id, stage_id


# ---------------------------------------------------------------------------
# Unit tests — artifact path
# ---------------------------------------------------------------------------


class TestDiffArtifactPath:
    """Tests for diff artifact path construction."""

    def test_produces_correct_path(self, tmp_path: Path) -> None:
        """Diff artifact path should be under pipelines/{id}/artifacts/review/."""
        result = _diff_artifact_path(tmp_path, 42)
        assert result == tmp_path / "42" / "artifacts" / "review" / "full_diff.patch"

    @given(pipeline_id=st.integers(min_value=1, max_value=9999))
    def test_path_contains_pipeline_id(self, pipeline_id: int) -> None:
        """Pipeline ID should appear in the artifact path."""
        base = Path("/tmp/pipelines")
        result = _diff_artifact_path(base, pipeline_id)
        assert str(pipeline_id) in str(result)
        assert result.name == "full_diff.patch"


# ---------------------------------------------------------------------------
# Unit tests — fix prompt builder
# ---------------------------------------------------------------------------


class TestBuildFixPrompt:
    """Tests for bug-fix prompt construction."""

    def test_includes_base_prompt(self) -> None:
        """Base prompt should appear at the start."""
        result = ReviewResult(
            approved=False,
            max_severity="medium",
            issues=[],
            feedback_markdown="",
        )
        prompt = _build_fix_prompt("Fix bugs.", result)
        assert prompt.startswith("Fix bugs.")

    def test_includes_severity(self) -> None:
        """Max severity from the review should be in the fix prompt."""
        result = ReviewResult(
            approved=False,
            max_severity="high",
            issues=[],
            feedback_markdown="",
        )
        prompt = _build_fix_prompt("Fix.", result)
        assert "high" in prompt

    def test_includes_issues_with_location(self) -> None:
        """Issues with file and line should be formatted with location."""
        result = ReviewResult(
            approved=False,
            max_severity="medium",
            issues=[
                ReviewIssue(
                    severity="medium",
                    description="SQL injection",
                    file="src/db.py",
                    line=42,
                ),
            ],
            feedback_markdown="",
        )
        prompt = _build_fix_prompt("Fix.", result)
        assert "SQL injection" in prompt
        assert "src/db.py:42" in prompt

    def test_includes_feedback_markdown(self) -> None:
        """Feedback markdown should be included in the fix prompt."""
        result = ReviewResult(
            approved=False,
            max_severity="low",
            issues=[],
            feedback_markdown="Use parameterized queries.",
        )
        prompt = _build_fix_prompt("Fix.", result)
        assert "Use parameterized queries." in prompt

    @given(data=review_structured_outputs())
    @settings(max_examples=20)
    def test_fix_prompt_never_crashes(self, data: dict[str, Any]) -> None:
        """Building a fix prompt from any valid review output should not crash."""
        from build_your_room.stages.review_loop import parse_review_result

        result = parse_review_result(data)
        if result is not None:
            prompt = _build_fix_prompt("base prompt", result)
            assert isinstance(prompt, str)
            assert len(prompt) > 0


# ---------------------------------------------------------------------------
# Integration tests — happy path (approved on first review)
# ---------------------------------------------------------------------------


class TestCodeReviewApproved:
    """Tests for the happy path where code is approved on first review."""

    @pytest.mark.asyncio
    async def test_approved_on_first_review(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """When the reviewer approves with low severity, stage returns approved."""
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()
        review_adapter = _make_review_adapter(_approved_output("none"))

        with patch(
            "build_your_room.stages.code_review._capture_full_diff",
            return_value=_MOCK_DIFF,
        ):
            result = await run_code_review_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"codex": review_adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
            )

        assert result == STAGE_RESULT_APPROVED

        history = log_buffer.get_history(pipeline_id)
        assert any("approved" in msg.lower() for msg in history)

    @pytest.mark.asyncio
    async def test_creates_review_session_row(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """An agent_sessions row should be created for the review session."""
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()
        review_adapter = _make_review_adapter(_approved_output("none"))

        with patch(
            "build_your_room.stages.code_review._capture_full_diff",
            return_value=_MOCK_DIFF,
        ):
            await run_code_review_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"codex": review_adapter},
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
        assert len(rows) >= 1
        assert rows[0]["session_type"] == "codex"
        assert rows[0]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_diff_artifact_saved(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """The diff should be persisted as an artifact file."""
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()
        review_adapter = _make_review_adapter(_approved_output("none"))

        with patch(
            "build_your_room.stages.code_review._capture_full_diff",
            return_value=_MOCK_DIFF,
        ):
            await run_code_review_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"codex": review_adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
            )

        artifact = _diff_artifact_path(tmp_pipelines_dir, pipeline_id)
        assert artifact.exists()
        assert "diff --git" in artifact.read_text()

    @pytest.mark.asyncio
    async def test_stage_artifact_recorded_in_db(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """The stage's output_artifact column should point to the diff file."""
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()
        review_adapter = _make_review_adapter(_approved_output("none"))

        with patch(
            "build_your_room.stages.code_review._capture_full_diff",
            return_value=_MOCK_DIFF,
        ):
            await run_code_review_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"codex": review_adapter},
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
        assert "full_diff.patch" in row["output_artifact"]

    @pytest.mark.asyncio
    async def test_session_closed_after_review(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """The review session should be closed after use."""
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()
        mock_session = _make_mock_session(
            structured_output=_approved_output("none")
        )
        review_adapter = _make_mock_adapter(mock_session)

        with patch(
            "build_your_room.stages.code_review._capture_full_diff",
            return_value=_MOCK_DIFF,
        ):
            await run_code_review_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"codex": review_adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
            )

        mock_session.close.assert_called()


# ---------------------------------------------------------------------------
# Integration tests — bug-fix loop
# ---------------------------------------------------------------------------


class TestCodeReviewBugFixLoop:
    """Tests for the review/fix cycle where issues are found and fixed."""

    @pytest.mark.asyncio
    async def test_rejected_then_approved(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """When rejected on round 1 and approved on round 2, returns approved."""
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()

        # Review adapter: reject first, approve second
        review_session_1 = _make_mock_session(
            structured_output=_rejected_output("medium"),
            session_id="review-1",
        )
        review_session_2 = _make_mock_session(
            structured_output=_approved_output("none"),
            session_id="review-2",
        )
        # Both review and fix use "codex", so one adapter handles all sessions.
        # Order: review1 (rejected), fix, review2 (approved).
        fix_session = _make_mock_session(output="Fixed.", session_id="fix-1")
        review_adapter = AsyncMock()
        review_adapter.start_session.side_effect = [
            review_session_1,
            fix_session,
            review_session_2,
        ]

        with patch(
            "build_your_room.stages.code_review._capture_full_diff",
            return_value=_MOCK_DIFF,
        ):
            result = await run_code_review_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"codex": review_adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
            )

        assert result == STAGE_RESULT_APPROVED

        # Should have 3 sessions: review1, fix, review2
        assert review_adapter.start_session.call_count == 3

    @pytest.mark.asyncio
    async def test_fix_session_row_created(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Both review and fix agent sessions should create DB rows."""
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()

        review_s1 = _make_mock_session(
            structured_output=_rejected_output("medium"), session_id="r-1"
        )
        fix_s = _make_mock_session(output="Fixed.", session_id="f-1")
        review_s2 = _make_mock_session(
            structured_output=_approved_output("none"), session_id="r-2"
        )
        adapter = AsyncMock()
        adapter.start_session.side_effect = [review_s1, fix_s, review_s2]

        with patch(
            "build_your_room.stages.code_review._capture_full_diff",
            return_value=_MOCK_DIFF,
        ):
            await run_code_review_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"codex": adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
            )

        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT * FROM agent_sessions WHERE pipeline_stage_id = %s "
                    "ORDER BY id",
                    (stage_id,),
                )
            ).fetchall()
        # 3 sessions: review1, fix, review2
        assert len(rows) == 3
        assert all(r["status"] == "completed" for r in rows)

    @pytest.mark.asyncio
    async def test_logs_fix_activity(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """The log should contain entries about sending issues to fix agent."""
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()

        review_s1 = _make_mock_session(
            structured_output=_rejected_output("medium"), session_id="r-1"
        )
        fix_s = _make_mock_session(output="Fixed.", session_id="f-1")
        review_s2 = _make_mock_session(
            structured_output=_approved_output("none"), session_id="r-2"
        )
        adapter = AsyncMock()
        adapter.start_session.side_effect = [review_s1, fix_s, review_s2]

        with patch(
            "build_your_room.stages.code_review._capture_full_diff",
            return_value=_MOCK_DIFF,
        ):
            await run_code_review_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"codex": adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
            )

        history = log_buffer.get_history(pipeline_id)
        assert any("fix agent" in msg.lower() for msg in history)


# ---------------------------------------------------------------------------
# Integration tests — escalation on max rounds
# ---------------------------------------------------------------------------


class TestCodeReviewEscalation:
    """Tests for escalation when max review rounds are exceeded."""

    @pytest.mark.asyncio
    async def test_escalates_on_max_rounds(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """When all rounds reject, stage should escalate."""
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node(max_iterations=1, on_max_rounds="escalate")

        review_adapter = _make_review_adapter(_rejected_output("medium"))

        with patch(
            "build_your_room.stages.code_review._capture_full_diff",
            return_value=_MOCK_DIFF,
        ):
            result = await run_code_review_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"codex": review_adapter},
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
        """With proceed_with_warnings policy, should return approved on max rounds."""
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node(max_iterations=1, on_max_rounds="proceed_with_warnings")

        review_adapter = _make_review_adapter(_rejected_output("medium"))

        with patch(
            "build_your_room.stages.code_review._capture_full_diff",
            return_value=_MOCK_DIFF,
        ):
            result = await run_code_review_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"codex": review_adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
            )

        assert result == STAGE_RESULT_APPROVED

        history = log_buffer.get_history(pipeline_id)
        assert any("proceeding with warnings" in msg.lower() for msg in history)

    @pytest.mark.asyncio
    async def test_unparseable_review_escalates(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Unparseable review output should escalate with review_divergence."""
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()

        # Return None structured_output to trigger parse failure
        bad_session = _make_mock_session(
            structured_output=None, session_id="bad-review"
        )
        review_adapter = _make_mock_adapter(bad_session)

        with patch(
            "build_your_room.stages.code_review._capture_full_diff",
            return_value=_MOCK_DIFF,
        ):
            result = await run_code_review_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"codex": review_adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
            )

        assert result == STAGE_RESULT_ESCALATED

        async with pool.connection() as conn:
            esc_rows = await (
                await conn.execute(
                    "SELECT * FROM escalations WHERE pipeline_id = %s",
                    (pipeline_id,),
                )
            ).fetchall()
        assert len(esc_rows) == 1
        assert esc_rows[0]["reason"] == "review_divergence"


# ---------------------------------------------------------------------------
# Edge cases — empty diff, cancellation, missing adapters
# ---------------------------------------------------------------------------


class TestCodeReviewEdgeCases:
    """Tests for edge cases: empty diff, cancellation, missing adapters."""

    @pytest.mark.asyncio
    async def test_empty_diff_approves_immediately(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """When there are no changes to review, stage should approve."""
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()
        review_adapter = _make_review_adapter()

        with patch(
            "build_your_room.stages.code_review._capture_full_diff",
            return_value="",
        ):
            result = await run_code_review_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"codex": review_adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
            )

        assert result == STAGE_RESULT_APPROVED
        # No review session should have been started
        review_adapter.start_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_review_adapter_escalates(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Missing review adapter should escalate immediately."""
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()

        result = await run_code_review_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=node,
            adapters={},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        assert result == STAGE_RESULT_ESCALATED

    @pytest.mark.asyncio
    async def test_missing_fix_adapter_escalates(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Missing fix adapter should escalate before the loop starts."""
        pool, pipeline_id, stage_id = pool_with_stage
        # Use different agents for review and fix
        node = _make_node(agent="codex", fix_agent="claude")
        review_adapter = _make_review_adapter()

        result = await run_code_review_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=node,
            adapters={"codex": review_adapter},  # no claude adapter
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

        assert result == STAGE_RESULT_ESCALATED

    @pytest.mark.asyncio
    async def test_cancel_before_review(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Pre-cancelled event should abort before starting review."""
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()
        review_adapter = _make_review_adapter()

        cancel_event = asyncio.Event()
        cancel_event.set()

        with patch(
            "build_your_room.stages.code_review._capture_full_diff",
            return_value=_MOCK_DIFF,
        ):
            result = await run_code_review_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"codex": review_adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
            )

        assert result == STAGE_RESULT_ESCALATED
        review_adapter.start_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_between_review_and_fix(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Cancellation between review and fix should abort."""
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()

        cancel_event = asyncio.Event()

        # Review adapter returns rejected; we set cancel after review
        review_session = _make_mock_session(
            structured_output=_rejected_output("medium"),
            session_id="review-1",
        )

        async def cancel_on_start(config: Any) -> Any:
            cancel_event.set()
            return review_session

        adapter = AsyncMock()
        adapter.start_session.side_effect = cancel_on_start

        with patch(
            "build_your_room.stages.code_review._capture_full_diff",
            return_value=_MOCK_DIFF,
        ):
            result = await run_code_review_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"codex": adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
            )

        assert result == STAGE_RESULT_ESCALATED

    @pytest.mark.asyncio
    async def test_fix_agent_failure_propagates(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """Fix agent failure should propagate as an exception."""
        pool, pipeline_id, stage_id = pool_with_stage
        node = _make_node()

        review_session = _make_mock_session(
            structured_output=_rejected_output("medium"), session_id="r-1"
        )
        fix_session = _make_mock_session(session_id="f-1")
        fix_session.send_turn.side_effect = RuntimeError("LLM error")

        adapter = AsyncMock()
        adapter.start_session.side_effect = [review_session, fix_session]

        with patch(
            "build_your_room.stages.code_review._capture_full_diff",
            return_value=_MOCK_DIFF,
        ):
            with pytest.raises(RuntimeError, match="LLM error"):
                await run_code_review_stage(
                    pool=pool,
                    pipeline_id=pipeline_id,
                    stage_id=stage_id,
                    node=node,
                    adapters={"codex": adapter},
                    log_buffer=log_buffer,
                    cancel_event=cancel_event,
                    pipelines_dir=tmp_pipelines_dir,
                )

        # Fix session should be marked failed
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT status FROM agent_sessions "
                    "WHERE pipeline_stage_id = %s ORDER BY id",
                    (stage_id,),
                )
            ).fetchall()
        statuses = [r["status"] for r in rows]
        assert "failed" in statuses

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

        session = _make_mock_session(
            structured_output=_approved_output("none"),
            session_id=None,
        )
        adapter = _make_mock_adapter(session)

        with patch(
            "build_your_room.stages.code_review._capture_full_diff",
            return_value=_MOCK_DIFF,
        ):
            result = await run_code_review_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"codex": adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
            )

        assert result == STAGE_RESULT_APPROVED

    @pytest.mark.asyncio
    async def test_head_rev_null_uses_review_base_rev(
        self,
        pool_with_stage: Any,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
        tmp_pipelines_dir: Path,
    ) -> None:
        """When head_rev is NULL, should use review_base_rev as the diff endpoint."""
        pool, pipeline_id, stage_id = pool_with_stage

        # Set head_rev to NULL
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET head_rev = NULL WHERE id = %s",
                (pipeline_id,),
            )
            await conn.commit()

        node = _make_node()
        review_adapter = _make_review_adapter(_approved_output("none"))

        with patch(
            "build_your_room.stages.code_review._capture_full_diff",
            return_value="",
        ) as mock_diff:
            result = await run_code_review_stage(
                pool=pool,
                pipeline_id=pipeline_id,
                stage_id=stage_id,
                node=node,
                adapters={"codex": review_adapter},
                log_buffer=log_buffer,
                cancel_event=cancel_event,
                pipelines_dir=tmp_pipelines_dir,
            )

        # head_rev was NULL, so it should use review_base_rev for both args
        assert result == STAGE_RESULT_APPROVED
        mock_diff.assert_called_once()
        call_args = mock_diff.call_args
        # Both revs should be review_base_rev when head_rev is NULL
        assert call_args[0][1] == call_args[0][2]  # review_base == head


# ---------------------------------------------------------------------------
# Prompt resolution tests
# ---------------------------------------------------------------------------


class TestPromptResolution:
    """Tests for prompt resolution from the database."""

    @pytest.mark.asyncio
    async def test_resolves_code_review_prompt(
        self, pool_with_stage: Any
    ) -> None:
        """code_review_default prompt should be found in the DB."""
        from build_your_room.stages.code_review import _resolve_prompt

        pool, _, _ = pool_with_stage
        body = await _resolve_prompt(pool, "code_review_default")
        assert "review" in body.lower() or "code" in body.lower()

    @pytest.mark.asyncio
    async def test_resolves_bug_fix_prompt(
        self, pool_with_stage: Any
    ) -> None:
        """bug_fix_default prompt should be found in the DB."""
        from build_your_room.stages.code_review import _resolve_prompt

        pool, _, _ = pool_with_stage
        body = await _resolve_prompt(pool, "bug_fix_default")
        assert "fix" in body.lower() or "bug" in body.lower()

    @pytest.mark.asyncio
    async def test_fallback_to_name(
        self, pool_with_stage: Any
    ) -> None:
        """Unknown prompt name should fall back to the name itself."""
        from build_your_room.stages.code_review import _resolve_prompt

        pool, _, _ = pool_with_stage
        body = await _resolve_prompt(pool, "nonexistent_prompt_xyz")
        assert body == "nonexistent_prompt_xyz"


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestCodeReviewProperties:
    """Property-based tests for review decision logic."""

    @given(data=review_structured_outputs())
    @settings(max_examples=25)
    def test_review_decision_is_deterministic(
        self, data: dict[str, Any]
    ) -> None:
        """Any well-formed review output should produce a deterministic
        approval decision without crashing."""
        from build_your_room.stages.review_loop import (
            parse_review_result,
            should_approve,
        )

        result = parse_review_result(data)
        assert result is not None
        decision = should_approve(result)
        assert isinstance(decision, bool)

    @given(data=review_structured_outputs())
    @settings(max_examples=20)
    def test_approved_implies_low_severity(
        self, data: dict[str, Any]
    ) -> None:
        """If should_approve returns True, max_severity must be none or low."""
        from build_your_room.stages.review_loop import (
            parse_review_result,
            should_approve,
        )

        result = parse_review_result(data)
        assert result is not None
        if should_approve(result):
            assert result.max_severity in ("none", "low")
            assert result.approved is True

    @given(
        pipeline_id=st.integers(min_value=1, max_value=9999),
        diff_content=st.text(
            min_size=1,
            max_size=200,
            alphabet=st.characters(
                blacklist_characters="\r",
                blacklist_categories=("Cs",),
            ),
        ),
    )
    @settings(max_examples=15)
    def test_diff_artifact_write_roundtrip(
        self, pipeline_id: int, diff_content: str
    ) -> None:
        """Any diff content written to the artifact path should be readable."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            path = _diff_artifact_path(Path(td), pipeline_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(diff_content)
            assert path.read_text() == diff_content
