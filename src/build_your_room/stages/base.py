"""Base types and registry for stage runners.

Defines the StageRunnerFn callable type, the STAGE_RUNNERS registry mapping
stage_type strings to runner functions, and the get_stage_runner() lookup.

Each concrete stage module (spec_author, impl_plan, etc.) registers itself
via register_stage_runner() at module level so that importing the stages
package populates the registry.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stage runner callable type
# ---------------------------------------------------------------------------

# All stage runners share this common keyword-only signature.  Some runners
# accept additional optional keyword arguments (e.g. htn_planner,
# browser_runner) for dependency injection in tests; those extras are not
# part of the base contract.
#
# Callable[..., Awaitable[str]] is used instead of a Protocol to avoid
# friction with optional kwargs that vary between runners.
StageRunnerFn = Callable[..., Awaitable[str]]

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

STAGE_RUNNERS: dict[str, StageRunnerFn] = {}


def register_stage_runner(stage_type: str, runner: StageRunnerFn) -> None:
    """Register a stage runner function for a given stage type.

    Raises ValueError if *stage_type* is already registered to a different
    runner (prevents accidental double-registration with conflicting
    implementations).
    """
    existing = STAGE_RUNNERS.get(stage_type)
    if existing is not None and existing is not runner:
        raise ValueError(
            f"Stage type {stage_type!r} already registered to {existing!r}"
        )
    STAGE_RUNNERS[stage_type] = runner
    logger.debug("Registered stage runner for %r", stage_type)


def get_stage_runner(stage_type: str) -> StageRunnerFn | None:
    """Look up the runner for *stage_type*, or return ``None``."""
    return STAGE_RUNNERS.get(stage_type)
