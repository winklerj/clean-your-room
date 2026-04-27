"""Per-stage tool allowlists and typed MCP tool profiles.

Each pipeline stage type has an explicit set of tools that the agent may
use. Claude sessions never receive Bash, shell, or arbitrary process-spawn
tools. Implementation and validation stages get additional harness MCP
tools for running tests, lint, type-checking, and browser validation.

Codex sessions use a different mechanism — writableRoots sandbox config —
which is produced by WorkspaceSandbox.writable_roots_list.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from build_your_room.harness_mcp import (
    HARNESS_TOOL_NAMES as _HARNESS_BARE_NAMES,
)
from build_your_room.harness_mcp import (
    qualified_tool_name,
)


class StageType(str, Enum):
    """Known pipeline stage types from the spec."""

    SPEC_AUTHOR = "spec_author"
    SPEC_REVIEW = "spec_review"
    IMPL_PLAN = "impl_plan"
    IMPL_PLAN_REVIEW = "impl_plan_review"
    IMPL_TASK = "impl_task"
    CODE_REVIEW = "code_review"
    BUG_FIX = "bug_fix"
    VALIDATION = "validation"
    CUSTOM = "custom"


# File-manipulation tools available to Claude sessions.
_FILE_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep")

# Harness-provided MCP tools for implementation and validation stages.
# These are typed tools that enforce workspace roots and command templates.
# Stored as bare semantic names (``run_tests``, ``run_lint``, …); the
# concrete SDK names are qualified to ``mcp__harness__<n>`` when the
# Claude adapter mounts the harness MCP server. See ``harness_mcp.py``.
_HARNESS_MCP_TOOLS: tuple[str, ...] = _HARNESS_BARE_NAMES


@dataclass(frozen=True)
class ToolProfile:
    """Tool configuration for a pipeline stage.

    allowed_tools: built-in tool names the Claude session may use
    harness_mcp_tools: bare names of harness MCP tools (semantic identity);
        the SDK-qualified names appear in :pyattr:`all_tools`.
    """

    allowed_tools: tuple[str, ...]
    harness_mcp_tools: tuple[str, ...] = ()

    @property
    def all_tools(self) -> tuple[str, ...]:
        """SDK-ready tool list: built-ins plus harness tools qualified for MCP.

        The Claude SDK's ``allowed_tools`` filter compares against the names
        Claude sees, which for harness tools are
        ``mcp__harness__<bare-name>``. This property returns that combined,
        SDK-ready list. Use :pyattr:`harness_mcp_tools` for the bare semantic
        names.
        """
        qualified = tuple(qualified_tool_name(n) for n in self.harness_mcp_tools)
        return self.allowed_tools + qualified


# Stage type → tool profile mapping.
_STAGE_PROFILES: dict[str, ToolProfile] = {
    # Authoring stages: file tools only, no execution
    StageType.SPEC_AUTHOR: ToolProfile(allowed_tools=_FILE_TOOLS),
    StageType.SPEC_REVIEW: ToolProfile(allowed_tools=_FILE_TOOLS),
    StageType.IMPL_PLAN: ToolProfile(allowed_tools=_FILE_TOOLS),
    StageType.IMPL_PLAN_REVIEW: ToolProfile(allowed_tools=_FILE_TOOLS),

    # Implementation and review stages: file tools + harness MCP tools
    StageType.IMPL_TASK: ToolProfile(
        allowed_tools=_FILE_TOOLS,
        harness_mcp_tools=_HARNESS_MCP_TOOLS,
    ),
    StageType.CODE_REVIEW: ToolProfile(
        allowed_tools=_FILE_TOOLS,
        harness_mcp_tools=_HARNESS_MCP_TOOLS,
    ),
    StageType.BUG_FIX: ToolProfile(
        allowed_tools=_FILE_TOOLS,
        harness_mcp_tools=_HARNESS_MCP_TOOLS,
    ),

    # Validation: file tools + all harness MCP tools (including browser)
    StageType.VALIDATION: ToolProfile(
        allowed_tools=_FILE_TOOLS,
        harness_mcp_tools=_HARNESS_MCP_TOOLS,
    ),

    # Custom stages: file tools only (safe default)
    StageType.CUSTOM: ToolProfile(allowed_tools=_FILE_TOOLS),
}


def get_tool_profile(stage_type: str) -> ToolProfile:
    """Look up the tool profile for a stage type.

    Falls back to file-tools-only for unknown stage types.
    """
    return _STAGE_PROFILES.get(stage_type, ToolProfile(allowed_tools=_FILE_TOOLS))


@dataclass(frozen=True)
class CodexSandboxConfig:
    """Codex app-server sandbox configuration."""

    mode: str = "workspace-write"
    writable_roots: tuple[str, ...] = ()


def get_codex_sandbox_config(writable_roots: list[str]) -> CodexSandboxConfig:
    """Build Codex sandbox config from writable root paths."""
    return CodexSandboxConfig(
        mode="workspace-write",
        writable_roots=tuple(writable_roots),
    )
