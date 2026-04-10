"""ContextMonitor — reusable hook for context-window usage tracking and rotation.

Called after every agent turn to check whether context usage exceeds the
configured threshold.  When it does, the monitor returns a rotation plan
that the adapter/stage-runner uses to persist resume state, end the current
session gracefully, and spawn a replacement session.

The monitor is a **pure decision-maker** — it does not manage sessions,
touch the database, or interact with providers.  Adapters own the
side-effects; the monitor owns the policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class ContextAction(str, Enum):
    """Action recommended by the monitor after a context check."""

    CONTINUE = "continue"
    ROTATE = "rotate"


@dataclass(frozen=True)
class ContextUsage:
    """Parsed context-window usage from an agent provider."""

    total_tokens: int
    max_tokens: int
    usage_pct: float
    categories: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class StageContext:
    """Execution context the monitor needs to build a rotation plan."""

    stage_type: str
    pipeline_id: int
    stage_id: int
    session_id: int
    artifact_path: str | None = None
    # HTN task claim fields — only meaningful for impl_task stages
    active_task_id: int | None = None
    active_claim_token: str | None = None
    prompt_context: str | None = None


@dataclass(frozen=True)
class RotationPlan:
    """Instructions the caller needs to rotate a session."""

    resume_state: dict[str, Any]
    has_active_claim: bool


@dataclass(frozen=True)
class ContextCheckResult:
    """Outcome of a single context check."""

    action: ContextAction
    usage: ContextUsage
    rotation_plan: RotationPlan | None = None
    warning_message: str | None = None


# ---------------------------------------------------------------------------
# ContextMonitor
# ---------------------------------------------------------------------------


class ContextMonitor:
    """Reusable hook that checks context usage and recommends rotation.

    Instantiate one per stage execution.  After every agent turn, call
    :meth:`check` with the latest :class:`ContextUsage` and the current
    :class:`StageContext`.  The returned :class:`ContextCheckResult` tells
    the adapter whether to continue or rotate.

    The monitor tracks how many checks and warnings have been issued so
    stage runners can include diagnostics in session logs.
    """

    def __init__(self, threshold_pct: float = 60.0) -> None:
        if threshold_pct <= 0 or threshold_pct > 100:
            raise ValueError(
                f"threshold_pct must be in (0, 100], got {threshold_pct}"
            )
        self._threshold_pct = threshold_pct
        self._check_count: int = 0
        self._warning_count: int = 0

    # -- Properties ----------------------------------------------------------

    @property
    def threshold_pct(self) -> float:
        return self._threshold_pct

    @property
    def check_count(self) -> int:
        return self._check_count

    @property
    def warning_count(self) -> int:
        return self._warning_count

    # -- Core API ------------------------------------------------------------

    def check(
        self, usage: ContextUsage, stage_context: StageContext
    ) -> ContextCheckResult:
        """Check *usage* against the threshold and return a recommendation.

        If usage is at or below the threshold the action is ``CONTINUE``.
        Otherwise the action is ``ROTATE`` and a :class:`RotationPlan` is
        attached with the resume state the adapter must persist.
        """
        self._check_count += 1

        if usage.usage_pct <= self._threshold_pct:
            return ContextCheckResult(action=ContextAction.CONTINUE, usage=usage)

        self._warning_count += 1
        rotation_plan = self._build_rotation_plan(stage_context)
        warning_msg = (
            f"Context usage {usage.usage_pct:.1f}% exceeds threshold "
            f"{self._threshold_pct:.1f}% — recommending session rotation"
        )
        return ContextCheckResult(
            action=ContextAction.ROTATE,
            usage=usage,
            rotation_plan=rotation_plan,
            warning_message=warning_msg,
        )

    # -- Parsers -------------------------------------------------------------

    @staticmethod
    def parse_claude_usage(raw: dict[str, Any] | None) -> ContextUsage | None:
        """Parse Claude SDK ``get_context_usage()`` into :class:`ContextUsage`.

        Returns ``None`` when the input is ``None`` or ``max_tokens`` is not
        positive (meaning the provider did not report capacity).
        """
        if raw is None:
            return None
        total = int(raw.get("total_tokens", 0))
        max_tokens = int(raw.get("max_tokens", 0))
        if max_tokens <= 0:
            return None
        pct = (total / max_tokens) * 100
        categories = {
            k: int(v)
            for k, v in raw.items()
            if k not in ("total_tokens", "max_tokens") and isinstance(v, (int, float))
        }
        return ContextUsage(
            total_tokens=total,
            max_tokens=max_tokens,
            usage_pct=pct,
            categories=categories,
        )

    @staticmethod
    def parse_codex_usage(
        token_input: int, token_output: int, max_tokens: int
    ) -> ContextUsage | None:
        """Build :class:`ContextUsage` from Codex-style token counts.

        Returns ``None`` when *max_tokens* is not positive.
        """
        if max_tokens <= 0:
            return None
        total = token_input + token_output
        pct = (total / max_tokens) * 100
        return ContextUsage(
            total_tokens=total,
            max_tokens=max_tokens,
            usage_pct=pct,
            categories={"input": token_input, "output": token_output},
        )

    # -- Internal ------------------------------------------------------------

    @staticmethod
    def _build_rotation_plan(ctx: StageContext) -> RotationPlan:
        """Build a :class:`RotationPlan` from the current stage context."""
        has_claim = (
            ctx.stage_type == "impl_task"
            and ctx.active_task_id is not None
            and ctx.active_claim_token is not None
        )

        resume_state: dict[str, Any] = {
            "stage_type": ctx.stage_type,
            "pipeline_id": ctx.pipeline_id,
            "stage_id": ctx.stage_id,
            "session_id": ctx.session_id,
        }
        if ctx.artifact_path is not None:
            resume_state["artifact_path"] = ctx.artifact_path

        if has_claim:
            resume_state["task_id"] = ctx.active_task_id
            resume_state["claim_token"] = ctx.active_claim_token
            if ctx.prompt_context is not None:
                resume_state["prompt_context"] = ctx.prompt_context

        return RotationPlan(resume_state=resume_state, has_active_claim=has_claim)
