"""Tests for context_monitor.py — context usage tracking and rotation logic.

Verifies that the ContextMonitor correctly classifies context usage against
a configurable threshold, builds rotation plans that preserve HTN task claims
for impl_task stages, and parses provider-specific usage payloads.  Uses
property-based tests for threshold invariants and unit tests for edge cases.
"""

from __future__ import annotations

import pytest
from hypothesis import given, assume
from hypothesis import strategies as st

from build_your_room.context_monitor import (
    ContextAction,
    ContextMonitor,
    ContextUsage,
    RotationPlan,
    StageContext,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_stage_types = st.sampled_from([
    "spec_author", "spec_review", "impl_plan", "impl_plan_review",
    "impl_task", "code_review", "bug_fix", "validation", "custom",
])

_positive_int = st.integers(min_value=1, max_value=10_000_000)


@st.composite
def context_usages(draw: st.DrawFn) -> ContextUsage:
    """Generate valid ContextUsage instances with consistent fields."""
    max_tokens = draw(st.integers(min_value=1, max_value=10_000_000))
    total_tokens = draw(st.integers(min_value=0, max_value=max_tokens * 2))
    pct = (total_tokens / max_tokens) * 100
    return ContextUsage(
        total_tokens=total_tokens,
        max_tokens=max_tokens,
        usage_pct=pct,
    )


@st.composite
def stage_contexts(draw: st.DrawFn) -> StageContext:
    """Generate valid StageContext instances."""
    stage_type = draw(_stage_types)
    pipeline_id = draw(_positive_int)
    stage_id = draw(_positive_int)
    session_id = draw(_positive_int)
    artifact_path = draw(st.none() | st.from_regex(r"[a-z/]{1,40}", fullmatch=True))

    # Only impl_task stages get claim fields
    if stage_type == "impl_task" and draw(st.booleans()):
        task_id = draw(_positive_int)
        claim_token = draw(st.uuids().map(str))
        prompt_ctx = draw(st.none() | st.text(min_size=1, max_size=50))
    else:
        task_id = None
        claim_token = None
        prompt_ctx = None

    return StageContext(
        stage_type=stage_type,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        session_id=session_id,
        artifact_path=artifact_path,
        active_task_id=task_id,
        active_claim_token=claim_token,
        prompt_context=prompt_ctx,
    )


# ---------------------------------------------------------------------------
# ContextUsage dataclass
# ---------------------------------------------------------------------------


class TestContextUsage:
    """Tests for the ContextUsage frozen dataclass."""

    def test_construction_preserves_fields(self) -> None:
        """ContextUsage stores all fields correctly.

        Invariant: construction preserves all provided values without mutation.
        """
        usage = ContextUsage(
            total_tokens=60_000,
            max_tokens=100_000,
            usage_pct=60.0,
            categories={"input": 40_000, "output": 20_000},
        )
        assert usage.total_tokens == 60_000
        assert usage.max_tokens == 100_000
        assert usage.usage_pct == 60.0
        assert usage.categories == {"input": 40_000, "output": 20_000}

    def test_default_categories_is_empty_dict(self) -> None:
        """ContextUsage.categories defaults to empty dict.

        Invariant: omitting categories produces empty dict, not None.
        """
        usage = ContextUsage(total_tokens=0, max_tokens=100, usage_pct=0.0)
        assert usage.categories == {}

    def test_frozen(self) -> None:
        """ContextUsage is immutable.

        Invariant: instances cannot be mutated after creation.
        """
        usage = ContextUsage(total_tokens=0, max_tokens=100, usage_pct=0.0)
        with pytest.raises(AttributeError):
            usage.total_tokens = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# StageContext dataclass
# ---------------------------------------------------------------------------


class TestStageContext:
    """Tests for the StageContext frozen dataclass."""

    def test_minimal_construction(self) -> None:
        """StageContext can be constructed with only required fields.

        Invariant: optional fields default to None.
        """
        ctx = StageContext(
            stage_type="spec_author",
            pipeline_id=1,
            stage_id=10,
            session_id=100,
        )
        assert ctx.artifact_path is None
        assert ctx.active_task_id is None
        assert ctx.active_claim_token is None
        assert ctx.prompt_context is None

    def test_full_construction(self) -> None:
        """StageContext stores all fields when fully specified.

        Invariant: all fields preserved including claim context.
        """
        ctx = StageContext(
            stage_type="impl_task",
            pipeline_id=1,
            stage_id=10,
            session_id=100,
            artifact_path="/tmp/artifact.md",
            active_task_id=42,
            active_claim_token="tok-abc",
            prompt_context="Implement the auth module",
        )
        assert ctx.active_task_id == 42
        assert ctx.active_claim_token == "tok-abc"
        assert ctx.prompt_context == "Implement the auth module"


# ---------------------------------------------------------------------------
# RotationPlan dataclass
# ---------------------------------------------------------------------------


class TestRotationPlan:
    """Tests for the RotationPlan frozen dataclass."""

    def test_construction(self) -> None:
        """RotationPlan stores resume_state and has_active_claim.

        Invariant: construction preserves provided values.
        """
        plan = RotationPlan(
            resume_state={"stage_type": "impl_task", "task_id": 7},
            has_active_claim=True,
        )
        assert plan.resume_state["task_id"] == 7
        assert plan.has_active_claim is True


# ---------------------------------------------------------------------------
# ContextMonitor — constructor
# ---------------------------------------------------------------------------


class TestContextMonitorInit:
    """Tests for ContextMonitor construction and configuration."""

    def test_default_threshold(self) -> None:
        """Default threshold is 60%.

        Invariant: monitor uses spec default when threshold is not overridden.
        """
        monitor = ContextMonitor()
        assert monitor.threshold_pct == 60.0

    def test_custom_threshold(self) -> None:
        """Custom threshold is stored correctly.

        Invariant: provided threshold value is accessible via property.
        """
        monitor = ContextMonitor(threshold_pct=75.0)
        assert monitor.threshold_pct == 75.0

    @given(threshold=st.floats(min_value=-1000, max_value=0, allow_nan=False))
    def test_rejects_non_positive_threshold(self, threshold: float) -> None:
        """Monitor rejects threshold <= 0.

        Invariant: for all threshold <= 0, construction raises ValueError.
        A zero or negative threshold is meaningless — every turn would rotate.
        """
        with pytest.raises(ValueError, match="threshold_pct must be in"):
            ContextMonitor(threshold_pct=threshold)

    @given(threshold=st.floats(min_value=100.01, max_value=1000, allow_nan=False))
    def test_rejects_over_100_threshold(self, threshold: float) -> None:
        """Monitor rejects threshold > 100.

        Invariant: for all threshold > 100, construction raises ValueError.
        A threshold above 100% would never trigger rotation.
        """
        with pytest.raises(ValueError, match="threshold_pct must be in"):
            ContextMonitor(threshold_pct=threshold)

    def test_initial_counters_are_zero(self) -> None:
        """Monitor starts with zero check and warning counters.

        Invariant: fresh monitor has no history.
        """
        monitor = ContextMonitor()
        assert monitor.check_count == 0
        assert monitor.warning_count == 0


# ---------------------------------------------------------------------------
# ContextMonitor.check — property-based
# ---------------------------------------------------------------------------


class TestContextMonitorCheckProperties:
    """Property-based tests for the check() method."""

    @given(
        threshold=st.floats(min_value=0.01, max_value=100.0, allow_nan=False),
        usage=context_usages(),
        ctx=stage_contexts(),
    )
    def test_below_threshold_always_continues(
        self, threshold: float, usage: ContextUsage, ctx: StageContext
    ) -> None:
        """Property: usage at or below threshold always yields CONTINUE.

        Invariant: for all usage_pct <= threshold, action == CONTINUE.
        This guarantees the monitor never triggers premature rotation.
        """
        assume(usage.usage_pct <= threshold)
        monitor = ContextMonitor(threshold_pct=threshold)
        result = monitor.check(usage, ctx)
        assert result.action == ContextAction.CONTINUE
        assert result.rotation_plan is None
        assert result.warning_message is None

    @given(
        threshold=st.floats(min_value=0.01, max_value=99.99, allow_nan=False),
        usage=context_usages(),
        ctx=stage_contexts(),
    )
    def test_above_threshold_always_rotates(
        self, threshold: float, usage: ContextUsage, ctx: StageContext
    ) -> None:
        """Property: usage above threshold always yields ROTATE with a plan.

        Invariant: for all usage_pct > threshold, action == ROTATE and
        rotation_plan is not None.  The adapter always gets clear instructions.
        """
        assume(usage.usage_pct > threshold)
        monitor = ContextMonitor(threshold_pct=threshold)
        result = monitor.check(usage, ctx)
        assert result.action == ContextAction.ROTATE
        assert result.rotation_plan is not None
        assert result.warning_message is not None

    @given(
        threshold=st.floats(min_value=0.01, max_value=100.0, allow_nan=False),
        usage=context_usages(),
        ctx=stage_contexts(),
    )
    def test_result_always_contains_usage(
        self, threshold: float, usage: ContextUsage, ctx: StageContext
    ) -> None:
        """Property: every check result includes the original usage data.

        Invariant: result.usage == input usage for all inputs.
        Adapters rely on this for logging context_usage_pct to agent_sessions.
        """
        monitor = ContextMonitor(threshold_pct=threshold)
        result = monitor.check(usage, ctx)
        assert result.usage is usage

    @given(
        threshold=st.floats(min_value=0.01, max_value=100.0, allow_nan=False),
        usages=st.lists(context_usages(), min_size=1, max_size=20),
        ctx=stage_contexts(),
    )
    def test_check_count_equals_calls(
        self, threshold: float, usages: list[ContextUsage], ctx: StageContext
    ) -> None:
        """Property: check_count always equals the number of check() calls.

        Invariant: check_count == len(calls) regardless of whether usage was
        above or below threshold.
        """
        monitor = ContextMonitor(threshold_pct=threshold)
        for u in usages:
            monitor.check(u, ctx)
        assert monitor.check_count == len(usages)

    @given(
        threshold=st.floats(min_value=0.01, max_value=100.0, allow_nan=False),
        usages=st.lists(context_usages(), min_size=1, max_size=20),
        ctx=stage_contexts(),
    )
    def test_warning_count_le_check_count(
        self, threshold: float, usages: list[ContextUsage], ctx: StageContext
    ) -> None:
        """Property: warning_count <= check_count after any sequence of checks.

        Invariant: warnings are a subset of checks — you cannot have more
        warnings than total checks.
        """
        monitor = ContextMonitor(threshold_pct=threshold)
        for u in usages:
            monitor.check(u, ctx)
        assert monitor.warning_count <= monitor.check_count


# ---------------------------------------------------------------------------
# ContextMonitor.check — unit tests
# ---------------------------------------------------------------------------


class TestContextMonitorCheckUnit:
    """Targeted unit tests for specific check() scenarios."""

    def test_exact_threshold_continues(self) -> None:
        """Usage exactly at threshold is CONTINUE, not ROTATE.

        Invariant: threshold boundary is inclusive — we only rotate when
        *exceeding* the threshold, not when matching it.  This avoids
        unnecessary rotation at the boundary.
        """
        monitor = ContextMonitor(threshold_pct=60.0)
        usage = ContextUsage(total_tokens=60_000, max_tokens=100_000, usage_pct=60.0)
        ctx = StageContext(stage_type="spec_author", pipeline_id=1, stage_id=1, session_id=1)
        result = monitor.check(usage, ctx)
        assert result.action == ContextAction.CONTINUE

    def test_just_above_threshold_rotates(self) -> None:
        """Usage just above threshold triggers ROTATE.

        Invariant: even 0.1% above triggers rotation for safety.
        """
        monitor = ContextMonitor(threshold_pct=60.0)
        usage = ContextUsage(total_tokens=60_100, max_tokens=100_000, usage_pct=60.1)
        ctx = StageContext(stage_type="spec_author", pipeline_id=1, stage_id=1, session_id=1)
        result = monitor.check(usage, ctx)
        assert result.action == ContextAction.ROTATE

    def test_warning_message_includes_percentages(self) -> None:
        """Warning message includes actual and threshold percentages.

        Invariant: the adapter can log the warning without computing percentages itself.
        """
        monitor = ContextMonitor(threshold_pct=60.0)
        usage = ContextUsage(total_tokens=75_000, max_tokens=100_000, usage_pct=75.0)
        ctx = StageContext(stage_type="impl_plan", pipeline_id=1, stage_id=2, session_id=3)
        result = monitor.check(usage, ctx)
        assert result.warning_message is not None
        assert "75.0%" in result.warning_message
        assert "60.0%" in result.warning_message

    def test_warning_count_increments_only_on_rotate(self) -> None:
        """Warning count only increments when rotation is recommended.

        Invariant: CONTINUE checks do not increment warning_count.
        """
        monitor = ContextMonitor(threshold_pct=60.0)
        low = ContextUsage(total_tokens=30_000, max_tokens=100_000, usage_pct=30.0)
        high = ContextUsage(total_tokens=80_000, max_tokens=100_000, usage_pct=80.0)
        ctx = StageContext(stage_type="spec_author", pipeline_id=1, stage_id=1, session_id=1)

        monitor.check(low, ctx)
        monitor.check(low, ctx)
        assert monitor.warning_count == 0

        monitor.check(high, ctx)
        assert monitor.warning_count == 1
        assert monitor.check_count == 3


# ---------------------------------------------------------------------------
# Rotation plan — impl_task claim preservation
# ---------------------------------------------------------------------------


class TestRotationPlanClaims:
    """Tests verifying that rotation plans preserve HTN task claims."""

    def test_impl_task_with_claim_preserves_task_id(self) -> None:
        """impl_task rotation plan includes task_id and claim_token.

        Invariant: when an impl_task stage has an active claim, the rotation
        plan must include task_id and claim_token so the replacement session
        resumes the SAME claimed task (UniqueTaskClaim invariant).
        """
        monitor = ContextMonitor(threshold_pct=50.0)
        usage = ContextUsage(total_tokens=60_000, max_tokens=100_000, usage_pct=60.0)
        ctx = StageContext(
            stage_type="impl_task",
            pipeline_id=1,
            stage_id=10,
            session_id=100,
            artifact_path="/artifacts/task_7.md",
            active_task_id=42,
            active_claim_token="claim-xyz",
            prompt_context="Implement login endpoint",
        )
        result = monitor.check(usage, ctx)
        assert result.rotation_plan is not None
        plan = result.rotation_plan
        assert plan.has_active_claim is True
        assert plan.resume_state["task_id"] == 42
        assert plan.resume_state["claim_token"] == "claim-xyz"
        assert plan.resume_state["prompt_context"] == "Implement login endpoint"
        assert plan.resume_state["artifact_path"] == "/artifacts/task_7.md"

    def test_impl_task_without_claim_has_no_claim_fields(self) -> None:
        """impl_task without an active claim omits claim fields.

        Invariant: when active_task_id or active_claim_token is None,
        has_active_claim is False and resume_state has no task_id.
        """
        monitor = ContextMonitor(threshold_pct=50.0)
        usage = ContextUsage(total_tokens=60_000, max_tokens=100_000, usage_pct=60.0)
        ctx = StageContext(
            stage_type="impl_task",
            pipeline_id=1,
            stage_id=10,
            session_id=100,
        )
        result = monitor.check(usage, ctx)
        assert result.rotation_plan is not None
        assert result.rotation_plan.has_active_claim is False
        assert "task_id" not in result.rotation_plan.resume_state

    def test_non_impl_task_never_has_active_claim(self) -> None:
        """Non-impl_task stages never produce has_active_claim=True.

        Invariant: only impl_task stages can have HTN task claims.
        Even if task_id fields are somehow set on other stage types,
        the monitor ignores them.
        """
        monitor = ContextMonitor(threshold_pct=50.0)
        usage = ContextUsage(total_tokens=60_000, max_tokens=100_000, usage_pct=60.0)
        ctx = StageContext(
            stage_type="code_review",
            pipeline_id=1,
            stage_id=10,
            session_id=100,
            active_task_id=42,
            active_claim_token="claim-xyz",
        )
        result = monitor.check(usage, ctx)
        assert result.rotation_plan is not None
        assert result.rotation_plan.has_active_claim is False
        assert "task_id" not in result.rotation_plan.resume_state

    @given(ctx=stage_contexts())
    def test_resume_state_always_has_core_fields(self, ctx: StageContext) -> None:
        """Property: resume_state always contains stage_type, pipeline_id, stage_id, session_id.

        Invariant: regardless of stage type or claim status, the core
        identification fields are always present.  The adapter uses these
        to create the replacement session and update DB rows.
        """
        monitor = ContextMonitor(threshold_pct=1.0)  # force rotation
        usage = ContextUsage(total_tokens=100, max_tokens=100, usage_pct=100.0)
        result = monitor.check(usage, ctx)
        assert result.rotation_plan is not None
        rs = result.rotation_plan.resume_state
        assert rs["stage_type"] == ctx.stage_type
        assert rs["pipeline_id"] == ctx.pipeline_id
        assert rs["stage_id"] == ctx.stage_id
        assert rs["session_id"] == ctx.session_id

    @given(ctx=stage_contexts())
    def test_artifact_path_included_iff_present(self, ctx: StageContext) -> None:
        """Property: artifact_path in resume_state iff ctx.artifact_path is not None.

        Invariant: the resume_state includes artifact_path only when there is
        a real artifact to resume from.  This prevents the replacement session
        from trying to load a nonexistent artifact.
        """
        monitor = ContextMonitor(threshold_pct=1.0)
        usage = ContextUsage(total_tokens=100, max_tokens=100, usage_pct=100.0)
        result = monitor.check(usage, ctx)
        assert result.rotation_plan is not None
        rs = result.rotation_plan.resume_state
        if ctx.artifact_path is not None:
            assert rs["artifact_path"] == ctx.artifact_path
        else:
            assert "artifact_path" not in rs


# ---------------------------------------------------------------------------
# parse_claude_usage
# ---------------------------------------------------------------------------


class TestParseClaudeUsage:
    """Tests for ContextMonitor.parse_claude_usage()."""

    def test_none_input_returns_none(self) -> None:
        """None raw input returns None.

        Invariant: when the provider returns no data, the parser yields None
        rather than a bogus ContextUsage.
        """
        assert ContextMonitor.parse_claude_usage(None) is None

    def test_zero_max_tokens_returns_none(self) -> None:
        """Zero max_tokens returns None.

        Invariant: division by zero is avoided; missing capacity means
        we cannot compute usage_pct.
        """
        assert ContextMonitor.parse_claude_usage({"total_tokens": 50, "max_tokens": 0}) is None

    def test_negative_max_tokens_returns_none(self) -> None:
        """Negative max_tokens returns None.

        Invariant: nonsensical max_tokens is treated as missing.
        """
        assert ContextMonitor.parse_claude_usage({"total_tokens": 50, "max_tokens": -1}) is None

    def test_valid_usage_parsed_correctly(self) -> None:
        """Valid Claude usage payload is parsed into ContextUsage.

        Invariant: total_tokens, max_tokens, and computed usage_pct are correct.
        """
        raw = {"total_tokens": 60_000, "max_tokens": 200_000, "system": 5_000, "user": 55_000}
        result = ContextMonitor.parse_claude_usage(raw)
        assert result is not None
        assert result.total_tokens == 60_000
        assert result.max_tokens == 200_000
        assert result.usage_pct == pytest.approx(30.0)
        assert result.categories == {"system": 5_000, "user": 55_000}

    @given(
        total=st.integers(min_value=0, max_value=1_000_000),
        max_tok=st.integers(min_value=1, max_value=1_000_000),
    )
    def test_computed_pct_matches_ratio(self, total: int, max_tok: int) -> None:
        """Property: parsed usage_pct equals (total / max) * 100.

        Invariant: the parser always computes usage_pct from the raw counts,
        never trusts a pre-computed percentage from the provider.
        """
        raw = {"total_tokens": total, "max_tokens": max_tok}
        result = ContextMonitor.parse_claude_usage(raw)
        assert result is not None
        expected = (total / max_tok) * 100
        assert result.usage_pct == pytest.approx(expected)

    def test_non_numeric_categories_excluded(self) -> None:
        """Non-numeric extra fields are excluded from categories.

        Invariant: categories only contain int values; string metadata
        from the provider is silently dropped.
        """
        raw = {"total_tokens": 100, "max_tokens": 200, "model": "opus", "system": 50}
        result = ContextMonitor.parse_claude_usage(raw)
        assert result is not None
        assert "model" not in result.categories
        assert result.categories == {"system": 50}


# ---------------------------------------------------------------------------
# parse_codex_usage
# ---------------------------------------------------------------------------


class TestParseCodexUsage:
    """Tests for ContextMonitor.parse_codex_usage()."""

    def test_zero_max_tokens_returns_none(self) -> None:
        """Zero max_tokens returns None.

        Invariant: division by zero is avoided.
        """
        assert ContextMonitor.parse_codex_usage(100, 50, 0) is None

    def test_negative_max_tokens_returns_none(self) -> None:
        """Negative max_tokens returns None.

        Invariant: nonsensical capacity is treated as missing.
        """
        assert ContextMonitor.parse_codex_usage(100, 50, -10) is None

    def test_valid_usage_parsed_correctly(self) -> None:
        """Valid Codex token counts are parsed into ContextUsage.

        Invariant: total = input + output, categories split preserved.
        """
        result = ContextMonitor.parse_codex_usage(40_000, 20_000, 100_000)
        assert result is not None
        assert result.total_tokens == 60_000
        assert result.max_tokens == 100_000
        assert result.usage_pct == pytest.approx(60.0)
        assert result.categories == {"input": 40_000, "output": 20_000}

    @given(
        inp=st.integers(min_value=0, max_value=500_000),
        out=st.integers(min_value=0, max_value=500_000),
        max_tok=st.integers(min_value=1, max_value=1_000_000),
    )
    def test_total_equals_input_plus_output(self, inp: int, out: int, max_tok: int) -> None:
        """Property: total_tokens == token_input + token_output.

        Invariant: the Codex parser always sums input and output; it never
        uses only one side.
        """
        result = ContextMonitor.parse_codex_usage(inp, out, max_tok)
        assert result is not None
        assert result.total_tokens == inp + out

    @given(
        inp=st.integers(min_value=0, max_value=500_000),
        out=st.integers(min_value=0, max_value=500_000),
        max_tok=st.integers(min_value=1, max_value=1_000_000),
    )
    def test_codex_pct_matches_ratio(self, inp: int, out: int, max_tok: int) -> None:
        """Property: parsed usage_pct equals ((inp + out) / max) * 100.

        Invariant: consistent computation across all valid inputs.
        """
        result = ContextMonitor.parse_codex_usage(inp, out, max_tok)
        assert result is not None
        expected = ((inp + out) / max_tok) * 100
        assert result.usage_pct == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Integration: parse → check pipeline
# ---------------------------------------------------------------------------


class TestParseAndCheck:
    """Integration tests verifying parse + check work together."""

    def test_claude_parse_then_check_continues(self) -> None:
        """Claude parse producing low usage results in CONTINUE when checked.

        Invariant: the full pipeline (parse → check) correctly classifies
        low usage from a real-looking Claude response.
        """
        raw = {"total_tokens": 30_000, "max_tokens": 200_000, "system": 5_000}
        usage = ContextMonitor.parse_claude_usage(raw)
        assert usage is not None

        monitor = ContextMonitor(threshold_pct=60.0)
        ctx = StageContext(stage_type="spec_author", pipeline_id=1, stage_id=1, session_id=1)
        result = monitor.check(usage, ctx)
        assert result.action == ContextAction.CONTINUE

    def test_codex_parse_then_check_rotates(self) -> None:
        """Codex parse producing high usage results in ROTATE when checked.

        Invariant: the full pipeline (parse → check) correctly triggers
        rotation for a Codex session approaching its context limit.
        """
        usage = ContextMonitor.parse_codex_usage(70_000, 30_000, 128_000)
        assert usage is not None

        monitor = ContextMonitor(threshold_pct=60.0)
        ctx = StageContext(
            stage_type="impl_task",
            pipeline_id=5,
            stage_id=50,
            session_id=500,
            active_task_id=7,
            active_claim_token="claim-123",
        )
        result = monitor.check(usage, ctx)
        assert result.action == ContextAction.ROTATE
        assert result.rotation_plan is not None
        assert result.rotation_plan.has_active_claim is True
        assert result.rotation_plan.resume_state["task_id"] == 7
