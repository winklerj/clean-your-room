"""Tests for harness_mcp.py — typed MCP tool surface for Claude sessions.

Covers:

- Naming: bare → ``mcp__harness__<n>`` qualification.
- ``build_harness_tools`` exposes exactly the spec-mandated tool surface.
- ``run_tests`` / ``run_lint`` / ``run_typecheck`` hand the registry
  template's args to ``run_cmd`` and shape stdout/stderr into an MCP
  reply, with ``is_error`` set from the exit code.
- Browser tools delegate to the injected ``BrowserRunner`` and return an
  ``is_error`` reply when no runner is configured.
- ``session_mcp_servers_for("claude", ...)`` returns a non-empty server
  config; non-Claude agents get ``{}``.

All ``run_cmd`` and ``BrowserRunner`` interactions are mocked — no real
subprocesses or network I/O.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from build_your_room.browser_runner import BrowserRunnerError, BrowserValidationResult
from build_your_room.command_registry import CommandRegistry, get_default_command_registry
from build_your_room.harness_mcp import (
    HARNESS_SERVER_NAME,
    HARNESS_TOOL_NAMES,
    build_harness_tools,
    qualified_tool_name,
    qualified_tool_names,
    session_mcp_servers_for,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_by_name(tools: list, bare_name: str) -> Any:
    for t in tools:
        if t.name == bare_name:
            return t
    raise AssertionError(f"Tool {bare_name!r} not in surface {[t.name for t in tools]}")


def _make_browser_runner_mock() -> MagicMock:
    runner = MagicMock()
    runner.start_dev_server = AsyncMock(
        return_value={"url": "http://localhost:3000", "pid": 4242, "ready": True},
    )
    runner.browser_run_scenario = AsyncMock(
        return_value=BrowserValidationResult(
            passed=True, console_errors=[], details={"steps": 3},
        ),
    )
    runner.browser_record_artifact = AsyncMock(
        return_value={"name": "validation_recording", "format": "gif",
                      "path": "/tmp/x.gif"},
    )
    return runner


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


class TestQualifiedNames:
    """``qualified_tool_name`` and ``qualified_tool_names`` produce SDK-shaped names."""

    def test_qualified_tool_name_uses_server_namespace(self) -> None:
        assert qualified_tool_name("run_tests") == f"mcp__{HARNESS_SERVER_NAME}__run_tests"

    def test_qualified_names_cover_all_bare_tools(self) -> None:
        bare = set(HARNESS_TOOL_NAMES)
        qualified = set(qualified_tool_names())
        assert {qualified_tool_name(n) for n in bare} == qualified

    def test_bare_tool_names_match_spec(self) -> None:
        """Spec lines 608-612 mandate this exact tool surface."""
        assert set(HARNESS_TOOL_NAMES) == {
            "run_tests",
            "run_lint",
            "run_typecheck",
            "start_dev_server",
            "browser_validate",
            "record_browser_artifact",
        }


# ---------------------------------------------------------------------------
# Tool factory surface
# ---------------------------------------------------------------------------


class TestBuildHarnessTools:
    """``build_harness_tools`` produces the full MCP tool surface."""

    def test_emits_one_tool_per_bare_name(self) -> None:
        tools = build_harness_tools(
            clone_path="/tmp/clone",
            allowed_roots=["/tmp/clone"],
            command_registry=get_default_command_registry(),
        )
        assert {t.name for t in tools} == set(HARNESS_TOOL_NAMES)

    def test_browser_tools_present_even_without_runner(self) -> None:
        """The tool *surface* is stable — runner=None still emits browser tools.

        The agent calls them and gets an ``is_error`` reply; without this
        invariant, the SDK ``allowed_tools`` filter would let them through
        but the dispatch would 500.
        """
        tools = build_harness_tools(
            clone_path="/tmp/clone",
            allowed_roots=["/tmp/clone"],
            command_registry=get_default_command_registry(),
            browser_runner=None,
        )
        assert {t.name for t in tools} == set(HARNESS_TOOL_NAMES)


# ---------------------------------------------------------------------------
# Subprocess-backed tools
# ---------------------------------------------------------------------------


class TestSubprocessTools:
    """run_tests/run_lint/run_typecheck delegate to ``run_cmd`` and shape replies."""

    @pytest.mark.asyncio
    @patch("build_your_room.harness_mcp.run_cmd", new_callable=AsyncMock)
    async def test_run_tests_invokes_template_with_targets(
        self, mock_run: AsyncMock,
    ) -> None:
        mock_run.return_value = (0, "passed\n", "")
        tools = build_harness_tools(
            clone_path="/tmp/clone",
            allowed_roots=["/tmp/clone"],
            command_registry=get_default_command_registry(),
        )
        run_tests = _tool_by_name(tools, "run_tests")

        reply = await run_tests.handler({"pattern": "tests/test_auth*"})

        mock_run.assert_awaited_once()
        called_args = mock_run.call_args.args[0]
        # Default tests_pass template is `uv run pytest ... -v`
        assert called_args[:3] == ["uv", "run", "pytest"]
        assert "tests/test_auth*" in called_args
        assert reply["is_error"] is False
        assert "passed" in reply["content"][0]["text"]

    @pytest.mark.asyncio
    @patch("build_your_room.harness_mcp.run_cmd", new_callable=AsyncMock)
    async def test_run_tests_marks_error_on_nonzero_exit(
        self, mock_run: AsyncMock,
    ) -> None:
        mock_run.return_value = (1, "", "AssertionError: nope\n")
        tools = build_harness_tools(
            clone_path="/tmp/clone",
            allowed_roots=["/tmp/clone"],
            command_registry=get_default_command_registry(),
        )
        reply = await _tool_by_name(tools, "run_tests").handler({})
        assert reply["is_error"] is True
        assert "AssertionError" in reply["content"][0]["text"]

    @pytest.mark.asyncio
    @patch("build_your_room.harness_mcp.run_cmd", new_callable=AsyncMock)
    async def test_run_lint_uses_lint_template(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = (0, "All checks passed!\n", "")
        tools = build_harness_tools(
            clone_path="/tmp/clone",
            allowed_roots=["/tmp/clone"],
            command_registry=get_default_command_registry(),
        )
        reply = await _tool_by_name(tools, "run_lint").handler({"scope": "src/"})

        called_args = mock_run.call_args.args[0]
        assert called_args[:4] == ["uv", "run", "ruff", "check"]
        assert "src/" in called_args
        assert reply["is_error"] is False

    @pytest.mark.asyncio
    @patch("build_your_room.harness_mcp.run_cmd", new_callable=AsyncMock)
    async def test_run_typecheck_uses_typecheck_template(
        self, mock_run: AsyncMock,
    ) -> None:
        mock_run.return_value = (0, "Success: no issues found\n", "")
        tools = build_harness_tools(
            clone_path="/tmp/clone",
            allowed_roots=["/tmp/clone"],
            command_registry=get_default_command_registry(),
        )
        reply = await _tool_by_name(tools, "run_typecheck").handler({})

        called_args = mock_run.call_args.args[0]
        assert called_args[:4] == ["uv", "run", "mypy", "src/"]
        assert reply["is_error"] is False

    @pytest.mark.asyncio
    async def test_command_registry_without_template_returns_error(self) -> None:
        """An empty registry surfaces a clean error reply, not an exception."""
        empty_reg = CommandRegistry(templates={})
        # Wipe defaults so 'tests_pass' is genuinely missing.
        empty_reg._templates.clear()  # type: ignore[attr-defined]

        tools = build_harness_tools(
            clone_path="/tmp/clone",
            allowed_roots=["/tmp/clone"],
            command_registry=empty_reg,
        )
        reply = await _tool_by_name(tools, "run_tests").handler({})
        assert reply["is_error"] is True
        assert "tests_pass" in reply["content"][0]["text"]

    @pytest.mark.asyncio
    @patch("build_your_room.harness_mcp.run_cmd", new_callable=AsyncMock)
    async def test_run_cmd_receives_clone_cwd_and_roots(
        self, mock_run: AsyncMock,
    ) -> None:
        """The harness must hand its own clone_path/allowed_roots to run_cmd."""
        mock_run.return_value = (0, "", "")
        tools = build_harness_tools(
            clone_path="/tmp/cloneA",
            allowed_roots=["/tmp/cloneA", "/tmp/logs"],
            command_registry=get_default_command_registry(),
        )
        await _tool_by_name(tools, "run_tests").handler({})

        kwargs = mock_run.call_args.kwargs
        assert mock_run.call_args.args[1] == "/tmp/cloneA"
        assert [str(r) for r in kwargs["allowed_roots"]] == ["/tmp/cloneA", "/tmp/logs"]


# ---------------------------------------------------------------------------
# Browser-backed tools
# ---------------------------------------------------------------------------


class TestBrowserTools:
    """start_dev_server / browser_validate / record_browser_artifact behaviour."""

    @pytest.mark.asyncio
    async def test_start_dev_server_returns_error_when_no_runner(self) -> None:
        tools = build_harness_tools(
            clone_path="/tmp/clone",
            allowed_roots=["/tmp/clone"],
            command_registry=get_default_command_registry(),
            browser_runner=None,
        )
        reply = await _tool_by_name(tools, "start_dev_server").handler({})
        assert reply["is_error"] is True
        assert "browser runner" in reply["content"][0]["text"].lower()

    @pytest.mark.asyncio
    async def test_start_dev_server_delegates_to_runner(self) -> None:
        runner = _make_browser_runner_mock()
        tools = build_harness_tools(
            clone_path="/tmp/clone",
            allowed_roots=["/tmp/clone"],
            command_registry=get_default_command_registry(),
            browser_runner=runner,
        )
        reply = await _tool_by_name(tools, "start_dev_server").handler(
            {"port": 5173, "timeout": 10},
        )
        runner.start_dev_server.assert_awaited_once()
        kwargs = runner.start_dev_server.call_args.kwargs
        assert kwargs["port"] == 5173
        assert kwargs["timeout"] == 10
        assert reply["is_error"] is False
        assert "http://localhost:3000" in reply["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_start_dev_server_marks_error_on_failure(self) -> None:
        runner = _make_browser_runner_mock()
        runner.start_dev_server = AsyncMock(side_effect=BrowserRunnerError("boom"))
        tools = build_harness_tools(
            clone_path="/tmp/clone",
            allowed_roots=["/tmp/clone"],
            command_registry=get_default_command_registry(),
            browser_runner=runner,
        )
        reply = await _tool_by_name(tools, "start_dev_server").handler({})
        assert reply["is_error"] is True
        assert "boom" in reply["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_browser_validate_requires_scenario(self) -> None:
        runner = _make_browser_runner_mock()
        tools = build_harness_tools(
            clone_path="/tmp/clone",
            allowed_roots=["/tmp/clone"],
            command_registry=get_default_command_registry(),
            browser_runner=runner,
        )
        reply = await _tool_by_name(tools, "browser_validate").handler({"scenario": ""})
        assert reply["is_error"] is True
        assert "scenario" in reply["content"][0]["text"].lower()
        runner.browser_run_scenario.assert_not_called()

    @pytest.mark.asyncio
    async def test_browser_validate_delegates_and_reports_pass(self) -> None:
        runner = _make_browser_runner_mock()
        tools = build_harness_tools(
            clone_path="/tmp/clone",
            allowed_roots=["/tmp/clone"],
            command_registry=get_default_command_registry(),
            browser_runner=runner,
        )
        reply = await _tool_by_name(tools, "browser_validate").handler(
            {"scenario": "homepage_smoke"},
        )
        runner.browser_run_scenario.assert_awaited_once_with("homepage_smoke")
        assert reply["is_error"] is False
        assert "passed=True" in reply["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_browser_validate_marks_error_on_failure(self) -> None:
        runner = _make_browser_runner_mock()
        runner.browser_run_scenario = AsyncMock(
            return_value=BrowserValidationResult(
                passed=False, console_errors=["TypeError: x"], details={},
            ),
        )
        tools = build_harness_tools(
            clone_path="/tmp/clone",
            allowed_roots=["/tmp/clone"],
            command_registry=get_default_command_registry(),
            browser_runner=runner,
        )
        reply = await _tool_by_name(tools, "browser_validate").handler(
            {"scenario": "checkout"},
        )
        assert reply["is_error"] is True
        assert "TypeError: x" in reply["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_record_browser_artifact_returns_error_when_no_runner(self) -> None:
        tools = build_harness_tools(
            clone_path="/tmp/clone",
            allowed_roots=["/tmp/clone"],
            command_registry=get_default_command_registry(),
            browser_runner=None,
        )
        reply = await _tool_by_name(tools, "record_browser_artifact").handler({})
        assert reply["is_error"] is True

    @pytest.mark.asyncio
    async def test_record_browser_artifact_delegates(self) -> None:
        runner = _make_browser_runner_mock()
        tools = build_harness_tools(
            clone_path="/tmp/clone",
            allowed_roots=["/tmp/clone"],
            command_registry=get_default_command_registry(),
            browser_runner=runner,
        )
        reply = await _tool_by_name(tools, "record_browser_artifact").handler(
            {"name": "after_validation", "format": "mp4"},
        )
        runner.browser_record_artifact.assert_awaited_once_with(
            name="after_validation", format="mp4",
        )
        assert reply["is_error"] is False


# ---------------------------------------------------------------------------
# Session-level wiring
# ---------------------------------------------------------------------------


class TestSessionMcpServersFor:
    """Per-agent dispatcher used by stage runners."""

    def test_claude_agent_gets_harness_server(self) -> None:
        servers = session_mcp_servers_for(
            "claude",
            clone_path="/tmp/clone",
            allowed_roots=["/tmp/clone"],
            command_registry=get_default_command_registry(),
        )
        assert HARNESS_SERVER_NAME in servers

    def test_non_claude_agent_gets_empty(self) -> None:
        servers = session_mcp_servers_for(
            "codex",
            clone_path="/tmp/clone",
            allowed_roots=["/tmp/clone"],
            command_registry=get_default_command_registry(),
        )
        assert servers == {}
