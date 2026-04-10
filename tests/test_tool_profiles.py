"""Tests for tool_profiles.py — per-stage tool allowlists.

Verifies that each stage type receives the correct set of tools, that
Bash/shell tools are never included in any Claude profile, and that
the Codex sandbox config is correctly constructed.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from build_your_room.sandbox import DENIED_TOOLS
from build_your_room.tool_profiles import (
    StageType,
    ToolProfile,
    get_codex_sandbox_config,
    get_tool_profile,
)


# ---------------------------------------------------------------------------
# File-only stage types (no harness MCP tools)
# ---------------------------------------------------------------------------

_FILE_ONLY_STAGES = [
    StageType.SPEC_AUTHOR,
    StageType.SPEC_REVIEW,
    StageType.IMPL_PLAN,
    StageType.IMPL_PLAN_REVIEW,
    StageType.CUSTOM,
]

_HARNESS_STAGES = [
    StageType.IMPL_TASK,
    StageType.CODE_REVIEW,
    StageType.BUG_FIX,
    StageType.VALIDATION,
]


class TestStageProfiles:
    """Tests for stage type → tool profile mapping."""

    @pytest.mark.parametrize("stage_type", _FILE_ONLY_STAGES)
    def test_file_only_stages_have_correct_tools(self, stage_type: StageType) -> None:
        """Authoring/review stages get only file tools, no harness MCP tools.

        Invariant: file-only stages have exactly {Read, Write, Edit, Glob, Grep}
        and no harness_mcp_tools.

        Prevents spec authoring stages from executing tests or starting servers.
        """
        profile = get_tool_profile(stage_type.value)
        assert set(profile.allowed_tools) == {"Read", "Write", "Edit", "Glob", "Grep"}
        assert profile.harness_mcp_tools == ()

    @pytest.mark.parametrize("stage_type", _HARNESS_STAGES)
    def test_harness_stages_include_mcp_tools(self, stage_type: StageType) -> None:
        """Implementation/validation stages get file tools plus harness MCP tools.

        Invariant: harness stages have file tools AND run_tests, run_lint,
        run_typecheck, start_dev_server, browser_validate, record_browser_artifact.

        These stages need to execute verification commands.
        """
        profile = get_tool_profile(stage_type.value)
        assert set(profile.allowed_tools) == {"Read", "Write", "Edit", "Glob", "Grep"}
        expected_mcp = {
            "run_tests",
            "run_lint",
            "run_typecheck",
            "start_dev_server",
            "browser_validate",
            "record_browser_artifact",
        }
        assert set(profile.harness_mcp_tools) == expected_mcp

    @given(stage_type=st.sampled_from([s.value for s in StageType]))
    def test_no_profile_contains_denied_tools(self, stage_type: str) -> None:
        """Property: no stage profile includes Bash, Shell, or other denied tools.

        Invariant: for all stage types S:
            DENIED_TOOLS ∩ get_tool_profile(S).all_tools == ∅

        This is the primary defense against arbitrary command execution
        in Claude sessions. If this fails, agents could bypass the sandbox.
        """
        profile = get_tool_profile(stage_type)
        all_tool_set = set(profile.all_tools)
        assert all_tool_set.isdisjoint(DENIED_TOOLS), (
            f"Stage {stage_type} includes denied tools: "
            f"{all_tool_set & DENIED_TOOLS}"
        )

    def test_unknown_stage_type_gets_safe_default(self) -> None:
        """Unknown stage types fall back to file-tools-only profile.

        Invariant: get_tool_profile("nonexistent") returns a safe default
        with only file tools and no harness MCP tools.

        Prevents new stage types from accidentally getting execution tools.
        """
        profile = get_tool_profile("nonexistent_stage")
        assert set(profile.allowed_tools) == {"Read", "Write", "Edit", "Glob", "Grep"}
        assert profile.harness_mcp_tools == ()


class TestToolProfile:
    """Tests for ToolProfile dataclass behavior."""

    def test_all_tools_combines_both_tuples(self) -> None:
        """all_tools returns the union of allowed_tools and harness_mcp_tools.

        Invariant: all_tools == allowed_tools + harness_mcp_tools.
        """
        profile = ToolProfile(
            allowed_tools=("Read", "Write"),
            harness_mcp_tools=("run_tests",),
        )
        assert profile.all_tools == ("Read", "Write", "run_tests")

    def test_all_tools_empty_harness(self) -> None:
        """all_tools with no harness tools returns just allowed_tools.

        Invariant: all_tools == allowed_tools when harness_mcp_tools is empty.
        """
        profile = ToolProfile(allowed_tools=("Read",))
        assert profile.all_tools == ("Read",)

    def test_frozen_profile_is_immutable(self) -> None:
        """ToolProfile is frozen — fields cannot be reassigned.

        Invariant: profile immutability prevents runtime tampering.
        """
        profile = ToolProfile(allowed_tools=("Read",))
        with pytest.raises(AttributeError):
            profile.allowed_tools = ("Bash",)  # type: ignore[misc]


class TestCodexSandboxConfig:
    """Tests for Codex sandbox configuration."""

    def test_get_codex_sandbox_config_structure(self) -> None:
        """get_codex_sandbox_config produces correct mode and roots.

        Invariant: mode is always "workspace-write", writable_roots matches input.
        """
        roots = ["/path/to/clone", "/path/to/logs"]
        config = get_codex_sandbox_config(roots)
        assert config.mode == "workspace-write"
        assert config.writable_roots == ("/path/to/clone", "/path/to/logs")

    def test_empty_roots_produces_empty_config(self) -> None:
        """Empty writable roots is valid (for read-only stages).

        Invariant: get_codex_sandbox_config([]).writable_roots == ().
        """
        config = get_codex_sandbox_config([])
        assert config.writable_roots == ()

    @given(
        roots=st.lists(
            st.from_regex(r"/[a-z]{3,15}(/[a-z]{3,15}){0,3}", fullmatch=True),
            min_size=1,
            max_size=6,
        ),
    )
    def test_writable_roots_preserved_as_tuple(self, roots: list[str]) -> None:
        """Property: all input roots appear in config.writable_roots.

        Invariant: for all root lists R,
            set(get_codex_sandbox_config(R).writable_roots) == set(R)
        """
        config = get_codex_sandbox_config(roots)
        assert set(config.writable_roots) == set(roots)

    def test_frozen_config_is_immutable(self) -> None:
        """CodexSandboxConfig is frozen — fields cannot be reassigned.

        Invariant: config immutability prevents runtime tampering.
        """
        config = get_codex_sandbox_config(["/path"])
        with pytest.raises(AttributeError):
            config.mode = "full-access"  # type: ignore[misc]
