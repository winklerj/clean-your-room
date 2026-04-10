"""Tests for the generic review loop — structured approval parsing,
decision gate logic, same-session continuation, new-session fallback,
max-rounds escalation, proceed-with-warnings, and context rotation.

All agent interactions are mocked — no live API calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from build_your_room.adapters.base import SessionConfig
from build_your_room.context_monitor import (
    ContextMonitor,
    StageContext,
)
from build_your_room.stage_graph import ReviewConfig
from build_your_room.stages.review_loop import (
    REVIEW_OUTPUT_SCHEMA,
    SEVERITY_ORDER,
    ReviewIssue,
    ReviewResult,
    _build_feedback_prompt,
    _severity_index,
    parse_review_result,
    run_review_loop,
    should_always_feed_back,
    should_approve,
)
from build_your_room.streaming import LogBuffer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_review_config(**overrides: Any) -> ReviewConfig:
    defaults = {
        "agent": "codex",
        "prompt": "review_default",
        "model": "gpt-5.1-codex",
        "max_review_rounds": 5,
        "exit_condition": "structured_approval",
        "on_max_rounds": "escalate",
    }
    defaults.update(overrides)
    return ReviewConfig(**defaults)


def _make_session_config(**overrides: Any) -> SessionConfig:
    defaults: dict[str, Any] = {
        "model": "gpt-5.1-codex",
        "clone_path": "/tmp/test-clone",
        "system_prompt": "You are a test reviewer.",
        "allowed_tools": ["Read", "Glob", "Grep"],
        "allowed_roots": ["/tmp/test-clone"],
        "pipeline_id": 1,
        "stage_id": 10,
    }
    defaults.update(overrides)
    return SessionConfig(**defaults)


def _make_stage_context(**overrides: Any) -> StageContext:
    defaults = {
        "stage_type": "spec_author",
        "pipeline_id": 1,
        "stage_id": 10,
        "session_id": 100,
    }
    defaults.update(overrides)
    return StageContext(**defaults)


@dataclass
class FakeTurnResult:
    """Minimal SessionResult for tests."""

    output: str = "revised artifact"
    structured_output: dict[str, Any] | None = None


def _make_mock_review_session(
    structured: dict[str, Any] | None,
) -> AsyncMock:
    """Build a mock LiveSession that returns the given structured output."""
    session = AsyncMock()
    session.session_id = "review-sess-1"
    session.send_turn.return_value = FakeTurnResult(
        output="review output", structured_output=structured
    )
    return session


def _make_mock_adapter(
    structured: dict[str, Any] | None = None,
    sessions: list[AsyncMock] | None = None,
) -> AsyncMock:
    """Build a mock AgentAdapter that creates review sessions."""
    adapter = AsyncMock()
    if sessions is not None:
        adapter.start_session.side_effect = sessions
    else:
        session = _make_mock_review_session(structured)
        adapter.start_session.return_value = session
    return adapter


def _make_low_usage_monitor() -> ContextMonitor:
    """Monitor that always says CONTINUE."""
    return ContextMonitor(threshold_pct=99.0)


def _make_primary_session(
    context_usage: dict[str, Any] | None = None,
    output: str = "revised artifact content",
) -> AsyncMock:
    """Build a mock primary LiveSession."""
    session = AsyncMock()
    session.session_id = "primary-sess-1"
    session.send_turn.return_value = FakeTurnResult(output=output)
    session.get_context_usage.return_value = context_usage or {
        "total_tokens": 1000,
        "max_tokens": 100000,
        "percentage": 1.0,
    }
    return session


def _approved_output(
    max_severity: str = "low",
    issues: list[dict] | None = None,
) -> dict[str, Any]:
    return {
        "approved": True,
        "max_severity": max_severity,
        "issues": issues or [],
        "feedback_markdown": "Looks good!",
    }


def _rejected_output(
    max_severity: str = "medium",
    issues: list[dict] | None = None,
    feedback: str = "Please fix issues.",
) -> dict[str, Any]:
    return {
        "approved": False,
        "max_severity": max_severity,
        "issues": issues
        or [{"severity": max_severity, "description": "Found a problem"}],
        "feedback_markdown": feedback,
    }


# ===================================================================
# parse_review_result tests
# ===================================================================


class TestParseReviewResult:
    def test_parse_valid_approved(self) -> None:
        raw = _approved_output()
        result = parse_review_result(raw)
        assert result is not None
        assert result.approved is True
        assert result.max_severity == "low"
        assert result.issues == []
        assert result.feedback_markdown == "Looks good!"

    def test_parse_valid_rejected_with_issues(self) -> None:
        issues = [
            {"severity": "medium", "description": "Missing edge case", "file": "spec.md", "line": 42},
            {"severity": "low", "description": "Typo"},
        ]
        raw = _rejected_output(issues=issues)
        result = parse_review_result(raw)
        assert result is not None
        assert result.approved is False
        assert result.max_severity == "medium"
        assert len(result.issues) == 2
        assert result.issues[0].file == "spec.md"
        assert result.issues[0].line == 42
        assert result.issues[1].file is None
        assert result.issues[1].line is None

    def test_parse_none_returns_none(self) -> None:
        assert parse_review_result(None) is None

    def test_parse_missing_approved_key_returns_none(self) -> None:
        raw = {"max_severity": "low", "issues": [], "feedback_markdown": "ok"}
        assert parse_review_result(raw) is None

    def test_parse_invalid_severity_normalizes_to_none(self) -> None:
        raw = {
            "approved": True,
            "max_severity": "unknown_level",
            "issues": [],
            "feedback_markdown": "ok",
        }
        result = parse_review_result(raw)
        assert result is not None
        assert result.max_severity == "none"

    def test_parse_non_dict_issues_skipped(self) -> None:
        raw = {
            "approved": False,
            "max_severity": "medium",
            "issues": ["not a dict", {"severity": "low", "description": "valid"}],
            "feedback_markdown": "fix it",
        }
        result = parse_review_result(raw)
        assert result is not None
        assert len(result.issues) == 1

    def test_parse_preserves_raw(self) -> None:
        raw = _approved_output()
        result = parse_review_result(raw)
        assert result is not None
        assert result.raw == raw


# ===================================================================
# should_approve / should_always_feed_back tests
# ===================================================================


class TestApprovalLogic:
    def test_approved_none_severity(self) -> None:
        r = ReviewResult(approved=True, max_severity="none", issues=[], feedback_markdown="")
        assert should_approve(r) is True

    def test_approved_low_severity(self) -> None:
        r = ReviewResult(approved=True, max_severity="low", issues=[], feedback_markdown="")
        assert should_approve(r) is True

    def test_approved_medium_severity_not_approved(self) -> None:
        r = ReviewResult(approved=True, max_severity="medium", issues=[], feedback_markdown="")
        assert should_approve(r) is False

    def test_not_approved_low_severity(self) -> None:
        r = ReviewResult(approved=False, max_severity="low", issues=[], feedback_markdown="")
        assert should_approve(r) is False

    def test_high_severity_always_feeds_back(self) -> None:
        r = ReviewResult(approved=False, max_severity="high", issues=[], feedback_markdown="")
        assert should_always_feed_back(r) is True

    def test_critical_severity_always_feeds_back(self) -> None:
        r = ReviewResult(approved=False, max_severity="critical", issues=[], feedback_markdown="")
        assert should_always_feed_back(r) is True

    def test_medium_severity_does_not_always_feed_back(self) -> None:
        r = ReviewResult(approved=False, max_severity="medium", issues=[], feedback_markdown="")
        assert should_always_feed_back(r) is False

    def test_low_severity_does_not_always_feed_back(self) -> None:
        r = ReviewResult(approved=False, max_severity="low", issues=[], feedback_markdown="")
        assert should_always_feed_back(r) is False


# ===================================================================
# _severity_index tests
# ===================================================================


class TestSeverityIndex:
    def test_all_severities_ordered(self) -> None:
        for i, sev in enumerate(SEVERITY_ORDER):
            assert _severity_index(sev) == i

    def test_case_insensitive(self) -> None:
        assert _severity_index("HIGH") == _severity_index("high")

    def test_unknown_defaults_to_zero(self) -> None:
        assert _severity_index("banana") == 0


# ===================================================================
# _build_feedback_prompt tests
# ===================================================================


class TestBuildFeedbackPrompt:
    def test_includes_severity_and_issues(self) -> None:
        result = ReviewResult(
            approved=False,
            max_severity="medium",
            issues=[
                ReviewIssue(severity="medium", description="Missing edge case", file="spec.md", line=42),
            ],
            feedback_markdown="Please fix.",
        )
        prompt = _build_feedback_prompt(result, "specification")
        assert "medium" in prompt
        assert "Missing edge case" in prompt
        assert "spec.md:42" in prompt
        assert "Please fix." in prompt
        assert "specification" in prompt

    def test_issue_without_file(self) -> None:
        result = ReviewResult(
            approved=False,
            max_severity="low",
            issues=[ReviewIssue(severity="low", description="Minor issue")],
            feedback_markdown="",
        )
        prompt = _build_feedback_prompt(result, "plan")
        assert "Minor issue" in prompt
        # No file/line location
        assert "(" not in prompt.split("Minor issue")[1].split("\n")[0]

    def test_no_issues(self) -> None:
        result = ReviewResult(
            approved=False,
            max_severity="medium",
            issues=[],
            feedback_markdown="General concerns.",
        )
        prompt = _build_feedback_prompt(result, "spec")
        assert "General concerns." in prompt


# ===================================================================
# REVIEW_OUTPUT_SCHEMA tests
# ===================================================================


class TestReviewOutputSchema:
    def test_required_fields_present(self) -> None:
        assert set(REVIEW_OUTPUT_SCHEMA["required"]) == {
            "approved",
            "max_severity",
            "issues",
            "feedback_markdown",
        }

    def test_severity_enum_matches_constant(self) -> None:
        enum_values = REVIEW_OUTPUT_SCHEMA["properties"]["max_severity"]["enum"]
        assert tuple(enum_values) == SEVERITY_ORDER


# ===================================================================
# run_review_loop integration tests
# ===================================================================


class TestRunReviewLoopApproval:
    """Test the happy path: reviewer immediately approves."""

    @pytest.mark.asyncio
    async def test_immediate_approval(self) -> None:
        primary = _make_primary_session()
        review_adapter = _make_mock_adapter(structured=_approved_output())
        config = _make_review_config(max_review_rounds=5)
        session_config = _make_session_config()
        monitor = _make_low_usage_monitor()
        ctx = _make_stage_context()

        outcome = await run_review_loop(
            primary_session=primary,
            review_adapter=review_adapter,
            review_config=config,
            review_session_config=session_config,
            artifact_content="# My Spec",
            artifact_type="specification",
            context_monitor=monitor,
            stage_context=ctx,
        )

        assert outcome.approved is True
        assert outcome.escalated is False
        assert outcome.rounds_completed == 1
        assert outcome.last_review is not None
        assert outcome.last_review.approved is True
        # Review adapter called once
        assert review_adapter.start_session.call_count == 1


class TestRunReviewLoopFeedbackThenApproval:
    """Test feedback loop: reject on round 1, approve on round 2."""

    @pytest.mark.asyncio
    async def test_one_rejection_then_approval(self) -> None:
        primary = _make_primary_session()

        # First review: rejected; second review: approved
        reject_session = _make_mock_review_session(_rejected_output())
        approve_session = _make_mock_review_session(_approved_output())
        review_adapter = _make_mock_adapter(
            sessions=[reject_session, approve_session]
        )

        config = _make_review_config(max_review_rounds=5)
        session_config = _make_session_config()
        monitor = _make_low_usage_monitor()
        ctx = _make_stage_context()

        outcome = await run_review_loop(
            primary_session=primary,
            review_adapter=review_adapter,
            review_config=config,
            review_session_config=session_config,
            artifact_content="# My Spec v1",
            artifact_type="specification",
            context_monitor=monitor,
            stage_context=ctx,
        )

        assert outcome.approved is True
        assert outcome.rounds_completed == 2
        # Primary got feedback
        assert primary.send_turn.call_count == 1


class TestRunReviewLoopMaxRoundsEscalation:
    """Test max rounds triggers escalation."""

    @pytest.mark.asyncio
    async def test_max_rounds_escalate(self) -> None:
        primary = _make_primary_session()

        # All rounds reject with medium severity
        review_sessions = [
            _make_mock_review_session(_rejected_output(max_severity="medium"))
            for _ in range(3)
        ]
        review_adapter = _make_mock_adapter(sessions=review_sessions)

        config = _make_review_config(max_review_rounds=2, on_max_rounds="escalate")
        session_config = _make_session_config()
        monitor = _make_low_usage_monitor()
        ctx = _make_stage_context()

        outcome = await run_review_loop(
            primary_session=primary,
            review_adapter=review_adapter,
            review_config=config,
            review_session_config=session_config,
            artifact_content="# Spec",
            artifact_type="specification",
            context_monitor=monitor,
            stage_context=ctx,
        )

        assert outcome.approved is False
        assert outcome.escalated is True
        assert outcome.escalation_reason == "max_iterations"
        assert outcome.rounds_completed == 2


class TestRunReviewLoopProceedWithWarnings:
    """Test max rounds with proceed_with_warnings policy."""

    @pytest.mark.asyncio
    async def test_proceed_with_warnings(self) -> None:
        primary = _make_primary_session()

        # All rounds reject with medium severity
        review_sessions = [
            _make_mock_review_session(_rejected_output(max_severity="medium"))
            for _ in range(3)
        ]
        review_adapter = _make_mock_adapter(sessions=review_sessions)

        config = _make_review_config(
            max_review_rounds=2, on_max_rounds="proceed_with_warnings"
        )
        session_config = _make_session_config()
        monitor = _make_low_usage_monitor()
        ctx = _make_stage_context()

        outcome = await run_review_loop(
            primary_session=primary,
            review_adapter=review_adapter,
            review_config=config,
            review_session_config=session_config,
            artifact_content="# Spec",
            artifact_type="specification",
            context_monitor=monitor,
            stage_context=ctx,
        )

        assert outcome.approved is True
        assert outcome.warnings_proceeded is True
        assert outcome.rounds_completed == 2


class TestRunReviewLoopHighSeverityExtraRound:
    """Test that high/critical severity always triggers feedback, even at max rounds."""

    @pytest.mark.asyncio
    async def test_high_severity_gets_extra_round(self) -> None:
        primary = _make_primary_session()

        # Round 1: high severity rejection
        # Extra review after feedback: still rejected → escalate
        reject_high = _make_mock_review_session(
            _rejected_output(max_severity="high")
        )
        reject_again = _make_mock_review_session(
            _rejected_output(max_severity="high")
        )
        review_adapter = _make_mock_adapter(sessions=[reject_high, reject_again])

        config = _make_review_config(max_review_rounds=1, on_max_rounds="escalate")
        session_config = _make_session_config()
        monitor = _make_low_usage_monitor()
        ctx = _make_stage_context()

        outcome = await run_review_loop(
            primary_session=primary,
            review_adapter=review_adapter,
            review_config=config,
            review_session_config=session_config,
            artifact_content="# Spec",
            artifact_type="specification",
            context_monitor=monitor,
            stage_context=ctx,
        )

        assert outcome.approved is False
        assert outcome.escalated is True
        assert outcome.rounds_completed == 2  # got the extra round
        # Primary received feedback
        assert primary.send_turn.call_count == 1

    @pytest.mark.asyncio
    async def test_high_severity_extra_round_approves(self) -> None:
        primary = _make_primary_session()

        reject_high = _make_mock_review_session(
            _rejected_output(max_severity="high")
        )
        approve_final = _make_mock_review_session(_approved_output())
        review_adapter = _make_mock_adapter(
            sessions=[reject_high, approve_final]
        )

        config = _make_review_config(max_review_rounds=1, on_max_rounds="escalate")
        session_config = _make_session_config()
        monitor = _make_low_usage_monitor()
        ctx = _make_stage_context()

        outcome = await run_review_loop(
            primary_session=primary,
            review_adapter=review_adapter,
            review_config=config,
            review_session_config=session_config,
            artifact_content="# Spec",
            artifact_type="specification",
            context_monitor=monitor,
            stage_context=ctx,
        )

        assert outcome.approved is True
        assert outcome.rounds_completed == 2


class TestRunReviewLoopContextRotation:
    """Test new-session fallback when context monitor triggers rotation."""

    @pytest.mark.asyncio
    async def test_context_rotation_creates_new_primary_session(self) -> None:
        # Primary session reports high context usage → rotation triggered
        primary = _make_primary_session(
            context_usage={
                "total_tokens": 90000,
                "max_tokens": 100000,
                "percentage": 90.0,
            }
        )

        # Round 1: reject, Round 2: approve
        reject_session = _make_mock_review_session(
            _rejected_output(max_severity="medium")
        )
        approve_session = _make_mock_review_session(_approved_output())
        review_adapter = _make_mock_adapter(
            sessions=[reject_session, approve_session]
        )

        # Replacement primary session
        replacement_primary = _make_primary_session(output="revised content v2")
        primary_adapter = AsyncMock()
        primary_adapter.start_session.return_value = replacement_primary
        primary_config = _make_session_config(model="claude-opus-4-6")

        config = _make_review_config(max_review_rounds=5)
        session_config = _make_session_config()
        monitor = ContextMonitor(threshold_pct=60.0)  # 90% > 60% → rotate
        ctx = _make_stage_context()

        outcome = await run_review_loop(
            primary_session=primary,
            review_adapter=review_adapter,
            review_config=config,
            review_session_config=session_config,
            artifact_content="# Spec v1",
            artifact_type="specification",
            context_monitor=monitor,
            stage_context=ctx,
            primary_adapter=primary_adapter,
            primary_session_config=primary_config,
        )

        assert outcome.approved is True
        assert outcome.rounds_completed == 2
        # Old session closed, new session used
        primary.close.assert_called_once()
        primary_adapter.start_session.assert_called_once()
        replacement_primary.send_turn.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_rotation_when_within_threshold(self) -> None:
        primary = _make_primary_session(
            context_usage={
                "total_tokens": 5000,
                "max_tokens": 100000,
                "percentage": 5.0,
            }
        )

        reject_session = _make_mock_review_session(
            _rejected_output(max_severity="medium")
        )
        approve_session = _make_mock_review_session(_approved_output())
        review_adapter = _make_mock_adapter(
            sessions=[reject_session, approve_session]
        )

        primary_adapter = AsyncMock()
        config = _make_review_config(max_review_rounds=5)
        session_config = _make_session_config()
        monitor = ContextMonitor(threshold_pct=60.0)
        ctx = _make_stage_context()

        outcome = await run_review_loop(
            primary_session=primary,
            review_adapter=review_adapter,
            review_config=config,
            review_session_config=session_config,
            artifact_content="# Spec",
            artifact_type="specification",
            context_monitor=monitor,
            stage_context=ctx,
            primary_adapter=primary_adapter,
        )

        assert outcome.approved is True
        # Old session NOT closed, same session reused
        primary.close.assert_not_called()
        primary_adapter.start_session.assert_not_called()
        # Primary got feedback on same session
        primary.send_turn.assert_called_once()

    @pytest.mark.asyncio
    async def test_rotation_falls_back_to_same_session_without_adapter(self) -> None:
        """If no primary_adapter is provided, rotation can't happen — use same session."""
        primary = _make_primary_session(
            context_usage={
                "total_tokens": 90000,
                "max_tokens": 100000,
                "percentage": 90.0,
            }
        )

        reject_session = _make_mock_review_session(
            _rejected_output(max_severity="medium")
        )
        approve_session = _make_mock_review_session(_approved_output())
        review_adapter = _make_mock_adapter(
            sessions=[reject_session, approve_session]
        )

        config = _make_review_config(max_review_rounds=5)
        session_config = _make_session_config()
        monitor = ContextMonitor(threshold_pct=60.0)
        ctx = _make_stage_context()

        outcome = await run_review_loop(
            primary_session=primary,
            review_adapter=review_adapter,
            review_config=config,
            review_session_config=session_config,
            artifact_content="# Spec",
            artifact_type="specification",
            context_monitor=monitor,
            stage_context=ctx,
            # No primary_adapter → same-session fallback
        )

        assert outcome.approved is True
        primary.close.assert_not_called()
        primary.send_turn.assert_called_once()


class TestRunReviewLoopParseFailure:
    """Test handling of unparseable review output."""

    @pytest.mark.asyncio
    async def test_unparseable_review_escalates(self) -> None:
        primary = _make_primary_session()
        review_adapter = _make_mock_adapter(structured=None)  # No structured output

        config = _make_review_config(max_review_rounds=5)
        session_config = _make_session_config()
        monitor = _make_low_usage_monitor()
        ctx = _make_stage_context()

        outcome = await run_review_loop(
            primary_session=primary,
            review_adapter=review_adapter,
            review_config=config,
            review_session_config=session_config,
            artifact_content="# Spec",
            artifact_type="specification",
            context_monitor=monitor,
            stage_context=ctx,
        )

        assert outcome.approved is False
        assert outcome.escalated is True
        assert outcome.escalation_reason == "review_parse_failure"


class TestRunReviewLoopLogBuffer:
    """Test that the review loop logs to the log buffer."""

    @pytest.mark.asyncio
    async def test_logs_review_rounds(self) -> None:
        primary = _make_primary_session()
        review_adapter = _make_mock_adapter(structured=_approved_output())
        config = _make_review_config()
        session_config = _make_session_config()
        monitor = _make_low_usage_monitor()
        ctx = _make_stage_context(pipeline_id=42)
        log_buffer = LogBuffer()

        await run_review_loop(
            primary_session=primary,
            review_adapter=review_adapter,
            review_config=config,
            review_session_config=session_config,
            artifact_content="# Spec",
            artifact_type="specification",
            context_monitor=monitor,
            stage_context=ctx,
            log_buffer=log_buffer,
        )

        history = log_buffer.get_history(42)
        assert len(history) >= 2  # "Review round 1/5" + "Review approved"
        assert any("[review]" in msg for msg in history)


class TestRunReviewLoopMultipleRejections:
    """Test multi-round feedback before final approval."""

    @pytest.mark.asyncio
    async def test_three_rejections_then_approval(self) -> None:
        primary = _make_primary_session()

        sessions = [
            _make_mock_review_session(_rejected_output(max_severity="medium")),
            _make_mock_review_session(_rejected_output(max_severity="medium")),
            _make_mock_review_session(_rejected_output(max_severity="low")),
            _make_mock_review_session(_approved_output()),
        ]
        review_adapter = _make_mock_adapter(sessions=sessions)

        config = _make_review_config(max_review_rounds=5)
        session_config = _make_session_config()
        monitor = _make_low_usage_monitor()
        ctx = _make_stage_context()

        outcome = await run_review_loop(
            primary_session=primary,
            review_adapter=review_adapter,
            review_config=config,
            review_session_config=session_config,
            artifact_content="# Spec",
            artifact_type="specification",
            context_monitor=monitor,
            stage_context=ctx,
        )

        assert outcome.approved is True
        assert outcome.rounds_completed == 4
        # 3 feedback rounds to primary
        assert primary.send_turn.call_count == 3


class TestRunReviewLoopReviewSessionCleanup:
    """Test that review sessions are properly closed after each round."""

    @pytest.mark.asyncio
    async def test_review_sessions_closed(self) -> None:
        primary = _make_primary_session()

        session1 = _make_mock_review_session(_rejected_output())
        session2 = _make_mock_review_session(_approved_output())
        review_adapter = _make_mock_adapter(sessions=[session1, session2])

        config = _make_review_config(max_review_rounds=5)
        session_config = _make_session_config()
        monitor = _make_low_usage_monitor()
        ctx = _make_stage_context()

        await run_review_loop(
            primary_session=primary,
            review_adapter=review_adapter,
            review_config=config,
            review_session_config=session_config,
            artifact_content="# Spec",
            artifact_type="specification",
            context_monitor=monitor,
            stage_context=ctx,
        )

        session1.close.assert_called_once()
        session2.close.assert_called_once()


class TestRunReviewLoopContextUsageNone:
    """Test graceful handling when context usage is unavailable."""

    @pytest.mark.asyncio
    async def test_none_context_usage_continues_same_session(self) -> None:
        primary = _make_primary_session(context_usage=None)
        primary.get_context_usage.return_value = None

        reject_session = _make_mock_review_session(_rejected_output())
        approve_session = _make_mock_review_session(_approved_output())
        review_adapter = _make_mock_adapter(
            sessions=[reject_session, approve_session]
        )

        config = _make_review_config(max_review_rounds=5)
        session_config = _make_session_config()
        monitor = ContextMonitor(threshold_pct=60.0)
        ctx = _make_stage_context()

        outcome = await run_review_loop(
            primary_session=primary,
            review_adapter=review_adapter,
            review_config=config,
            review_session_config=session_config,
            artifact_content="# Spec",
            artifact_type="specification",
            context_monitor=monitor,
            stage_context=ctx,
        )

        assert outcome.approved is True
        primary.close.assert_not_called()


class TestRunReviewLoopSingleRound:
    """Test with max_review_rounds=1."""

    @pytest.mark.asyncio
    async def test_single_round_approve(self) -> None:
        primary = _make_primary_session()
        review_adapter = _make_mock_adapter(structured=_approved_output())
        config = _make_review_config(max_review_rounds=1)
        session_config = _make_session_config()
        monitor = _make_low_usage_monitor()
        ctx = _make_stage_context()

        outcome = await run_review_loop(
            primary_session=primary,
            review_adapter=review_adapter,
            review_config=config,
            review_session_config=session_config,
            artifact_content="# Spec",
            artifact_type="specification",
            context_monitor=monitor,
            stage_context=ctx,
        )

        assert outcome.approved is True
        assert outcome.rounds_completed == 1

    @pytest.mark.asyncio
    async def test_single_round_reject_escalates(self) -> None:
        primary = _make_primary_session()
        review_adapter = _make_mock_adapter(
            structured=_rejected_output(max_severity="medium")
        )
        config = _make_review_config(max_review_rounds=1, on_max_rounds="escalate")
        session_config = _make_session_config()
        monitor = _make_low_usage_monitor()
        ctx = _make_stage_context()

        outcome = await run_review_loop(
            primary_session=primary,
            review_adapter=review_adapter,
            review_config=config,
            review_session_config=session_config,
            artifact_content="# Spec",
            artifact_type="specification",
            context_monitor=monitor,
            stage_context=ctx,
        )

        assert outcome.approved is False
        assert outcome.escalated is True
        assert outcome.escalation_reason == "max_iterations"


class TestRunReviewLoopApprovedHighSeverity:
    """Edge case: reviewer says approved=True but max_severity=high."""

    @pytest.mark.asyncio
    async def test_approved_high_severity_not_accepted(self) -> None:
        """approved=True with high severity is not a real approval."""
        primary = _make_primary_session()

        # This contradictory output should NOT count as approved
        contradictory = {
            "approved": True,
            "max_severity": "high",
            "issues": [{"severity": "high", "description": "Major issue"}],
            "feedback_markdown": "Has serious problems but approved anyway",
        }
        # After feedback: clean approval
        approve_clean = _approved_output()

        session1 = _make_mock_review_session(contradictory)
        session2 = _make_mock_review_session(approve_clean)
        review_adapter = _make_mock_adapter(sessions=[session1, session2])

        config = _make_review_config(max_review_rounds=5)
        session_config = _make_session_config()
        monitor = _make_low_usage_monitor()
        ctx = _make_stage_context()

        outcome = await run_review_loop(
            primary_session=primary,
            review_adapter=review_adapter,
            review_config=config,
            review_session_config=session_config,
            artifact_content="# Spec",
            artifact_type="specification",
            context_monitor=monitor,
            stage_context=ctx,
        )

        # Should not have been accepted on round 1 despite approved=True
        assert outcome.approved is True
        assert outcome.rounds_completed == 2
