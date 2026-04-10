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
_HARNESS_MCP_TOOLS: tuple[str, ...] = (
    "run_tests",
    "run_lint",
    "run_typecheck",
    "start_dev_server",
    "browser_validate",
    "record_browser_artifact",
)


@dataclass(frozen=True)
class ToolProfile:
    """Tool configuration for a pipeline stage.

    allowed_tools: tool names the Claude session may use
    harness_mcp_tools: additional MCP tools provided by the harness
    """

    allowed_tools: tuple[str, ...]
    harness_mcp_tools: tuple[str, ...] = ()

    @property
    def all_tools(self) -> tuple[str, ...]:
        """Combined list of all available tools."""
        return self.allowed_tools + self.harness_mcp_tools


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
