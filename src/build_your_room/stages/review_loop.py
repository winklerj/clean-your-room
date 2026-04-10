"""Generic review loop used by spec-author and impl-plan stages.

Drives a bounded feedback cycle between a primary agent (typically Claude)
and a review agent (typically Codex).  The reviewer returns structured JSON
output with approval status; the loop decides whether to feed back, approve,
or escalate based on the review config.

The loop receives *live session handles* rather than one-shot calls, enabling
same-session continuation when context permits and new-session fallback when
context is tight or after a server restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from build_your_room.adapters.base import (
    AgentAdapter,
    LiveSession,
    SessionConfig,
    SessionResult,
)
from build_your_room.context_monitor import (
    ContextAction,
    ContextMonitor,
    StageContext,
)
from build_your_room.stage_graph import ReviewConfig
from build_your_room.streaming import LogBuffer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured review output types
# ---------------------------------------------------------------------------

# Severity levels in ascending order of seriousness
SEVERITY_ORDER = ("none", "low", "medium", "high", "critical")

# The structured output schema sent to the review agent
REVIEW_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "max_severity": {
            "type": "string",
            "enum": list(SEVERITY_ORDER),
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string"},
                    "description": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                },
            },
        },
        "feedback_markdown": {"type": "string"},
    },
    "required": ["approved", "max_severity", "issues", "feedback_markdown"],
}


@dataclass(frozen=True)
class ReviewIssue:
    """A single issue reported by the reviewer."""

    severity: str
    description: str
    file: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class ReviewResult:
    """Parsed structured output from a review agent turn."""

    approved: bool
    max_severity: str
    issues: list[ReviewIssue]
    feedback_markdown: str
    raw: dict[str, Any] = field(default_factory=dict)


def parse_review_result(structured: dict[str, Any] | None) -> ReviewResult | None:
    """Parse reviewer structured output into a ReviewResult.

    Returns None if the structured output is missing or malformed.
    """
    if structured is None:
        return None
    try:
        approved = bool(structured["approved"])
        max_severity = str(structured.get("max_severity", "none")).lower()
        if max_severity not in SEVERITY_ORDER:
            max_severity = "none"
        raw_issues = structured.get("issues", [])
        issues = [
            ReviewIssue(
                severity=str(iss.get("severity", "low")),
                description=str(iss.get("description", "")),
                file=iss.get("file"),
                line=iss.get("line"),
            )
            for iss in raw_issues
            if isinstance(iss, dict)
        ]
        feedback_md = str(structured.get("feedback_markdown", ""))
        return ReviewResult(
            approved=approved,
            max_severity=max_severity,
            issues=issues,
            feedback_markdown=feedback_md,
            raw=dict(structured),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("Failed to parse review structured output: %s", structured)
        return None


def _severity_index(severity: str) -> int:
    """Return numeric severity index (higher = more severe)."""
    try:
        return SEVERITY_ORDER.index(severity.lower())
    except ValueError:
        return 0


def should_approve(result: ReviewResult) -> bool:
    """Check whether a review result constitutes approval.

    Approved when ``approved is True`` AND ``max_severity in ('none', 'low')``.
    """
    return result.approved and _severity_index(result.max_severity) <= 1


def should_always_feed_back(result: ReviewResult) -> bool:
    """High/critical severity always triggers feedback regardless of iteration count."""
    return _severity_index(result.max_severity) >= 3


# ---------------------------------------------------------------------------
# Review loop outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewLoopOutcome:
    """Final outcome of a review loop execution."""

    approved: bool
    escalated: bool = False
    escalation_reason: str | None = None
    rounds_completed: int = 0
    last_review: ReviewResult | None = None
    warnings_proceeded: bool = False


# ---------------------------------------------------------------------------
# Review loop runner
# ---------------------------------------------------------------------------


async def run_review_loop(
    *,
    primary_session: LiveSession,
    review_adapter: AgentAdapter,
    review_config: ReviewConfig,
    review_session_config: SessionConfig,
    artifact_content: str,
    artifact_type: str,
    context_monitor: ContextMonitor,
    stage_context: StageContext,
    log_buffer: LogBuffer | None = None,
    primary_adapter: AgentAdapter | None = None,
    primary_session_config: SessionConfig | None = None,
) -> ReviewLoopOutcome:
    """Run a bounded review loop between a primary agent and a review agent.

    Args:
        primary_session: Live session handle for the primary (authoring) agent.
        review_adapter: Adapter factory for creating review agent sessions.
        review_config: Review sub-config from the stage node.
        review_session_config: SessionConfig template for review sessions.
        artifact_content: The initial artifact produced by the primary agent.
        artifact_type: Human-readable type name (e.g. "specification", "implementation plan").
        context_monitor: Monitors context usage to decide same-session vs new-session.
        stage_context: Current stage execution context for the context monitor.
        log_buffer: Optional log buffer for pipeline-scoped logging.
        primary_adapter: Adapter factory for creating replacement primary sessions
            when context rotation forces a new session.
        primary_session_config: SessionConfig template for replacement primary sessions.

    Returns:
        ReviewLoopOutcome with approval/escalation status and round count.
    """
    max_rounds = review_config.max_review_rounds
    current_artifact = artifact_content
    current_primary_session = primary_session
    pipeline_id = stage_context.pipeline_id

    for round_num in range(1, max_rounds + 1):
        _log(log_buffer, pipeline_id, f"Review round {round_num}/{max_rounds}")

        # --- Review turn ---
        review_result = await _run_review_turn(
            review_adapter=review_adapter,
            review_session_config=review_session_config,
            artifact_content=current_artifact,
            artifact_type=artifact_type,
        )

        if review_result is None:
            _log(log_buffer, pipeline_id, "Review returned unparseable output, escalating")
            return ReviewLoopOutcome(
                approved=False,
                escalated=True,
                escalation_reason="review_parse_failure",
                rounds_completed=round_num,
            )

        _log(
            log_buffer,
            pipeline_id,
            f"Review round {round_num}: approved={review_result.approved}, "
            f"max_severity={review_result.max_severity}, "
            f"issues={len(review_result.issues)}",
        )

        # --- Decision gate ---
        if should_approve(review_result):
            _log(log_buffer, pipeline_id, f"Review approved after {round_num} round(s)")
            return ReviewLoopOutcome(
                approved=True,
                rounds_completed=round_num,
                last_review=review_result,
            )

        # Last round — check on_max_rounds policy before feeding back
        if round_num >= max_rounds and not should_always_feed_back(review_result):
            return _handle_max_rounds(
                review_config=review_config,
                review_result=review_result,
                rounds_completed=round_num,
                log_buffer=log_buffer,
                pipeline_id=pipeline_id,
            )

        # --- Feed feedback back to primary agent ---
        current_primary_session, revised_artifact = await _feed_back(
            primary_session=current_primary_session,
            review_result=review_result,
            round_num=round_num,
            artifact_type=artifact_type,
            context_monitor=context_monitor,
            stage_context=stage_context,
            primary_adapter=primary_adapter,
            primary_session_config=primary_session_config,
            log_buffer=log_buffer,
            pipeline_id=pipeline_id,
        )

        if revised_artifact is not None:
            current_artifact = revised_artifact

        # If high/critical severity and we've reached max rounds after feedback
        if round_num >= max_rounds and should_always_feed_back(review_result):
            # Do one final review to see if the high/critical issues are resolved
            final_review = await _run_review_turn(
                review_adapter=review_adapter,
                review_session_config=review_session_config,
                artifact_content=current_artifact,
                artifact_type=artifact_type,
            )
            if final_review is not None and should_approve(final_review):
                _log(
                    log_buffer,
                    pipeline_id,
                    "Review approved after extra round for high/critical severity",
                )
                return ReviewLoopOutcome(
                    approved=True,
                    rounds_completed=round_num + 1,
                    last_review=final_review,
                )
            return _handle_max_rounds(
                review_config=review_config,
                review_result=final_review or review_result,
                rounds_completed=round_num + 1,
                log_buffer=log_buffer,
                pipeline_id=pipeline_id,
            )

    # Should not reach here (loop handles max_rounds), but defensive
    return ReviewLoopOutcome(
        approved=False,
        escalated=True,
        escalation_reason="max_iterations",
        rounds_completed=max_rounds,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _run_review_turn(
    *,
    review_adapter: AgentAdapter,
    review_session_config: SessionConfig,
    artifact_content: str,
    artifact_type: str,
) -> ReviewResult | None:
    """Create a review session, send the review prompt, parse the result."""
    review_prompt = (
        f"Review the following {artifact_type} document. "
        "Mentally simulate a TLA+ specification: define invariants, "
        "preconditions, and postconditions for the system described. "
        "Use this mental model to validate the document.\n\n"
        "Do NOT create a TLA+ file. Use the formal reasoning to find:\n"
        "- Logical contradictions\n"
        "- Missing edge cases\n"
        "- Violated invariants\n"
        "- Unspecified preconditions\n"
        "- Ambiguous postconditions\n\n"
        "Return structured JSON output with your assessment.\n\n"
        f"Document to review:\n{artifact_content}"
    )

    review_session = await review_adapter.start_session(review_session_config)
    try:
        turn_result: SessionResult = await review_session.send_turn(
            review_prompt,
            output_schema=REVIEW_OUTPUT_SCHEMA,
        )
        return parse_review_result(turn_result.structured_output)
    finally:
        await review_session.close()


async def _feed_back(
    *,
    primary_session: LiveSession,
    review_result: ReviewResult,
    round_num: int,
    artifact_type: str,
    context_monitor: ContextMonitor,
    stage_context: StageContext,
    primary_adapter: AgentAdapter | None,
    primary_session_config: SessionConfig | None,
    log_buffer: LogBuffer | None,
    pipeline_id: int,
) -> tuple[LiveSession, str | None]:
    """Feed review feedback to the primary agent, handling context rotation.

    Returns (session_handle, revised_artifact_text).  The session handle may
    be a new session if context rotation was triggered.
    """
    feedback_prompt = _build_feedback_prompt(review_result, artifact_type)

    # Check context usage on the primary session to decide continuation mode
    use_new_session = False
    raw_usage = await primary_session.get_context_usage()
    if raw_usage is not None:
        usage = ContextMonitor.parse_claude_usage(raw_usage)
        if usage is None:
            usage = ContextMonitor.parse_codex_usage(
                raw_usage.get("total_tokens", 0),
                raw_usage.get("max_tokens", 0) - raw_usage.get("total_tokens", 0),
                raw_usage.get("max_tokens", 0),
            )
        if usage is not None:
            check = context_monitor.check(usage, stage_context)
            if check.action == ContextAction.ROTATE:
                _log(
                    log_buffer,
                    pipeline_id,
                    f"Context rotation triggered at {usage.usage_pct:.1f}% — "
                    "switching to new session for feedback",
                )
                use_new_session = True

    if use_new_session and primary_adapter is not None and primary_session_config is not None:
        # Close old session and start a replacement
        await primary_session.close()
        session = await primary_adapter.start_session(primary_session_config)
        # Build a self-contained prompt for the new session
        new_session_prompt = (
            f"You previously wrote a {artifact_type} document. "
            f"A reviewer provided this feedback:\n\n"
            f"{review_result.feedback_markdown}\n\n"
            f"Please revise the document addressing all issues."
        )
        turn_result = await session.send_turn(new_session_prompt)
        _log(log_buffer, pipeline_id, f"New-session feedback round {round_num} complete")
        return session, turn_result.output
    else:
        # Same-session continuation
        turn_result = await primary_session.send_turn(feedback_prompt)
        _log(log_buffer, pipeline_id, f"Same-session feedback round {round_num} complete")
        return primary_session, turn_result.output


def _build_feedback_prompt(review_result: ReviewResult, artifact_type: str) -> str:
    """Build the feedback prompt sent to the primary agent."""
    parts = [
        f"A reviewer found issues with the {artifact_type}.",
        f"Max severity: {review_result.max_severity}",
    ]
    if review_result.issues:
        parts.append(f"\nIssues ({len(review_result.issues)}):")
        for i, issue in enumerate(review_result.issues, 1):
            loc = ""
            if issue.file:
                loc = f" ({issue.file}"
                if issue.line is not None:
                    loc += f":{issue.line}"
                loc += ")"
            parts.append(f"  {i}. [{issue.severity}] {issue.description}{loc}")
    if review_result.feedback_markdown:
        parts.append(f"\nDetailed feedback:\n{review_result.feedback_markdown}")
    parts.append("\nPlease revise the document addressing all issues raised.")
    return "\n".join(parts)


def _handle_max_rounds(
    *,
    review_config: ReviewConfig,
    review_result: ReviewResult,
    rounds_completed: int,
    log_buffer: LogBuffer | None,
    pipeline_id: int,
) -> ReviewLoopOutcome:
    """Handle the max-rounds boundary based on the on_max_rounds policy."""
    if review_config.on_max_rounds == "proceed_with_warnings":
        _log(
            log_buffer,
            pipeline_id,
            f"Max review rounds ({rounds_completed}) reached — proceeding with warnings",
        )
        return ReviewLoopOutcome(
            approved=True,
            rounds_completed=rounds_completed,
            last_review=review_result,
            warnings_proceeded=True,
        )

    # Default: escalate
    _log(
        log_buffer,
        pipeline_id,
        f"Max review rounds ({rounds_completed}) reached — escalating",
    )
    return ReviewLoopOutcome(
        approved=False,
        escalated=True,
        escalation_reason="max_iterations",
        rounds_completed=rounds_completed,
        last_review=review_result,
    )


def _log(log_buffer: LogBuffer | None, pipeline_id: int, message: str) -> None:
    """Append to the pipeline log buffer if available."""
    if log_buffer is not None:
        log_buffer.append(pipeline_id, f"[review] {message}")
