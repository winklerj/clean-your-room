"""Tests for ClaudeAgentAdapter — session creation, turn handling, context usage,
snapshot/resume, cleanup, path guard integration, and tool profile enforcement.

All Claude Agent SDK interactions are mocked — no live API calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from build_your_room.adapters.base import SessionConfig
from build_your_room.adapters.claude_adapter import (
    ClaudeAgentAdapter,
    ClaudeLiveSession,
    ClaudeTurnResult,
    _make_sdk_permission_callback,
)
from build_your_room.sandbox import DENIED_TOOLS
from build_your_room.streaming import LogBuffer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> SessionConfig:
    """Build a SessionConfig with sensible test defaults."""
    defaults: dict[str, Any] = {
        "model": "claude-sonnet-4-6",
        "clone_path": "/tmp/test-clone",
        "system_prompt": "You are a test assistant.",
        "allowed_tools": ["Read", "Write", "Edit", "Glob", "Grep"],
        "allowed_roots": ["/tmp/test-clone", "/tmp/test-logs"],
        "max_turns": 10,
        "context_threshold_pct": 60.0,
        "pipeline_id": 1,
        "stage_id": 42,
        "session_db_id": 7,
    }
    defaults.update(overrides)
    return SessionConfig(**defaults)


def _make_mock_client(
    *,
    session_id: str = "sess-abc123",
    result_text: str = "Done.",
    structured_output: dict | None = None,
    cost: float = 0.01,
    turns: int = 3,
    is_error: bool = False,
    context_usage: dict | None = None,
) -> MagicMock:
    """Build a mock ClaudeSDKClient with configurable response behaviour.

    Uses MagicMock(spec=...) so isinstance() checks in _collect_response pass.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    text_block = MagicMock(spec=TextBlock)
    text_block.text = "Working..."

    assistant_msg = MagicMock(spec=AssistantMessage)
    assistant_msg.content = [text_block]

    result_msg = MagicMock(spec=ResultMessage)
    result_msg.session_id = session_id
    result_msg.result = result_text
    result_msg.structured_output = structured_output
    result_msg.total_cost_usd = cost
    result_msg.num_turns = turns
    result_msg.is_error = is_error
    result_msg.usage = {"input_tokens": 500, "output_tokens": 200}

    async def _receive_response():
        yield assistant_msg
        yield result_msg

    client = MagicMock()
    client.query = AsyncMock()
    client.receive_response = _receive_response
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()

    if context_usage is not None:
        client.get_context_usage = AsyncMock(return_value=context_usage)
    else:
        client.get_context_usage = AsyncMock(return_value={
            "totalTokens": 5000,
            "maxTokens": 200000,
            "percentage": 2.5,
            "categories": [],
        })

    return client


# ---------------------------------------------------------------------------
# SessionConfig tests
# ---------------------------------------------------------------------------


class TestSessionConfig:
    def test_defaults(self) -> None:
        cfg = _make_config()
        assert cfg.model == "claude-sonnet-4-6"
        assert cfg.clone_path == "/tmp/test-clone"
        assert cfg.context_threshold_pct == 60.0
        assert cfg.resume_session_id is None
        assert cfg.extra == {}

    def test_clone_path_obj(self) -> None:
        cfg = _make_config()
        assert cfg.clone_path_obj == Path("/tmp/test-clone")

    def test_frozen(self) -> None:
        cfg = _make_config()
        with pytest.raises(AttributeError):
            cfg.model = "different"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ClaudeTurnResult tests
# ---------------------------------------------------------------------------


class TestClaudeTurnResult:
    def test_basic_result(self) -> None:
        r = ClaudeTurnResult(output="hello", structured_output=None)
        assert r.output == "hello"
        assert r.structured_output is None
        assert r.is_error is False
        assert r.num_turns == 0

    def test_with_structured_output(self) -> None:
        data = {"approved": True, "issues": []}
        r = ClaudeTurnResult(output="ok", structured_output=data, num_turns=5)
        assert r.structured_output == data
        assert r.num_turns == 5


# ---------------------------------------------------------------------------
# Permission callback tests
# ---------------------------------------------------------------------------


class TestSdkPermissionCallback:
    @pytest.fixture
    def callback(self, tmp_path: Path) -> Any:
        """Create a permission callback rooted in tmp_path."""
        roots = [str(tmp_path / "clone"), str(tmp_path / "logs")]
        return _make_sdk_permission_callback(roots)

    @pytest.mark.asyncio
    async def test_allows_path_within_roots(self, callback: Any, tmp_path: Path) -> None:
        result = await callback("Read", {"file_path": str(tmp_path / "clone" / "src" / "main.py")}, None)
        assert hasattr(result, "updated_input")

    @pytest.mark.asyncio
    async def test_denies_path_outside_roots(self, callback: Any) -> None:
        result = await callback("Write", {"file_path": "/etc/passwd"}, None)
        assert hasattr(result, "message")

    @pytest.mark.asyncio
    async def test_denies_bash_tool(self, callback: Any) -> None:
        result = await callback("Bash", {"command": "rm -rf /"}, None)
        assert hasattr(result, "message")
        assert "denied by policy" in result.message

    @pytest.mark.asyncio
    async def test_denies_all_denied_tools(self, callback: Any) -> None:
        for tool in DENIED_TOOLS:
            result = await callback(tool, {}, None)
            assert hasattr(result, "message"), f"Expected denial for {tool}"

    @pytest.mark.asyncio
    async def test_allows_non_file_tool(self, callback: Any) -> None:
        result = await callback("SomeCustomTool", {"data": "value"}, None)
        assert hasattr(result, "updated_input")

    @pytest.mark.asyncio
    async def test_allows_tool_with_no_input(self, callback: Any) -> None:
        result = await callback("Read", {}, None)
        assert hasattr(result, "updated_input")


# ---------------------------------------------------------------------------
# ClaudeLiveSession tests
# ---------------------------------------------------------------------------


class TestClaudeLiveSession:
    @pytest.fixture
    def config(self) -> SessionConfig:
        return _make_config()

    @pytest.fixture
    def log_buffer(self) -> LogBuffer:
        return LogBuffer()

    @pytest.mark.asyncio
    async def test_send_turn_collects_text(self, config: SessionConfig, log_buffer: LogBuffer) -> None:
        client = _make_mock_client(result_text="All done.")
        session = ClaudeLiveSession(client, config, log_buffer)

        result = await session.send_turn("Do something")

        client.query.assert_awaited_once_with("Do something")
        assert "Working..." in result.output
        assert "All done." in result.output
        assert result.is_error is False

    @pytest.mark.asyncio
    async def test_send_turn_captures_session_id(self, config: SessionConfig) -> None:
        client = _make_mock_client(session_id="sess-xyz")
        session = ClaudeLiveSession(client, config)

        await session.send_turn("hello")

        assert session.session_id == "sess-xyz"

    @pytest.mark.asyncio
    async def test_send_turn_captures_structured_output(self, config: SessionConfig) -> None:
        data = {"approved": True, "max_severity": "low"}
        client = _make_mock_client(structured_output=data)
        session = ClaudeLiveSession(client, config)

        result = await session.send_turn("review this")

        assert result.structured_output == data

    @pytest.mark.asyncio
    async def test_send_turn_accumulates_cost(self, config: SessionConfig) -> None:
        client = _make_mock_client(cost=0.05)
        session = ClaudeLiveSession(client, config)

        await session.send_turn("turn 1")
        # Call again — set up a second response
        client.receive_response = _make_mock_client(cost=0.03).receive_response
        await session.send_turn("turn 2")

        snap = await session.snapshot()
        assert snap["total_cost_usd"] == pytest.approx(0.08, abs=0.001)

    @pytest.mark.asyncio
    async def test_send_turn_accumulates_tokens(self, config: SessionConfig) -> None:
        client = _make_mock_client()
        session = ClaudeLiveSession(client, config)

        await session.send_turn("turn 1")

        snap = await session.snapshot()
        assert snap["total_input_tokens"] == 500
        assert snap["total_output_tokens"] == 200

    @pytest.mark.asyncio
    async def test_send_turn_handles_error_result(self, config: SessionConfig) -> None:
        client = _make_mock_client(is_error=True, result_text="Error occurred")
        session = ClaudeLiveSession(client, config)

        result = await session.send_turn("fail")

        assert result.is_error is True

    @pytest.mark.asyncio
    async def test_get_context_usage_normalises_keys(self, config: SessionConfig) -> None:
        client = _make_mock_client(context_usage={
            "totalTokens": 15000,
            "maxTokens": 200000,
            "percentage": 7.5,
            "categories": [{"name": "system", "tokens": 5000}],
        })
        session = ClaudeLiveSession(client, config)

        usage = await session.get_context_usage()

        assert usage is not None
        assert usage["total_tokens"] == 15000
        assert usage["max_tokens"] == 200000
        assert usage["percentage"] == 7.5

    @pytest.mark.asyncio
    async def test_get_context_usage_returns_none_on_none(self, config: SessionConfig) -> None:
        client = _make_mock_client()
        client.get_context_usage = AsyncMock(return_value=None)
        session = ClaudeLiveSession(client, config)

        usage = await session.get_context_usage()

        assert usage is None

    @pytest.mark.asyncio
    async def test_get_context_usage_returns_none_on_error(self, config: SessionConfig) -> None:
        client = _make_mock_client()
        client.get_context_usage = AsyncMock(side_effect=RuntimeError("connection lost"))
        session = ClaudeLiveSession(client, config)

        usage = await session.get_context_usage()

        assert usage is None

    @pytest.mark.asyncio
    async def test_snapshot_includes_session_metadata(self, config: SessionConfig) -> None:
        client = _make_mock_client(session_id="sess-snap")
        session = ClaudeLiveSession(client, config)
        await session.send_turn("do work")

        snap = await session.snapshot()

        assert snap["session_id"] == "sess-snap"
        assert snap["model"] == "claude-sonnet-4-6"
        assert snap["clone_path"] == "/tmp/test-clone"
        assert snap["pipeline_id"] == 1
        assert snap["stage_id"] == 42

    @pytest.mark.asyncio
    async def test_close_calls_disconnect(self, config: SessionConfig) -> None:
        client = _make_mock_client()
        session = ClaudeLiveSession(client, config)

        await session.close()

        client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_swallows_disconnect_error(self, config: SessionConfig) -> None:
        client = _make_mock_client()
        client.disconnect = AsyncMock(side_effect=RuntimeError("already closed"))
        session = ClaudeLiveSession(client, config)

        await session.close()  # should not raise

    @pytest.mark.asyncio
    async def test_logs_to_buffer(self, config: SessionConfig, log_buffer: LogBuffer) -> None:
        client = _make_mock_client()
        session = ClaudeLiveSession(client, config, log_buffer)

        await session.send_turn("hello")

        history = log_buffer.get_history(1)
        assert any("[claude]" in msg for msg in history)

    @pytest.mark.asyncio
    async def test_no_log_without_buffer(self, config: SessionConfig) -> None:
        client = _make_mock_client()
        session = ClaudeLiveSession(client, config, None)

        await session.send_turn("hello")  # should not raise


# ---------------------------------------------------------------------------
# ClaudeAgentAdapter tests
# ---------------------------------------------------------------------------


class TestClaudeAgentAdapter:
    @pytest.mark.asyncio
    @patch("build_your_room.adapters.claude_adapter.ClaudeSDKClient")
    @patch("build_your_room.adapters.claude_adapter.ClaudeAgentOptions")
    async def test_start_session_creates_client_with_correct_options(
        self, mock_options_cls: MagicMock, mock_client_cls: MagicMock
    ) -> None:
        mock_client = _make_mock_client()
        mock_client_cls.return_value = mock_client

        adapter = ClaudeAgentAdapter()
        config = _make_config(model="claude-opus-4-6", max_turns=20)

        await adapter.start_session(config)

        mock_options_cls.assert_called_once()
        call_kwargs = mock_options_cls.call_args
        assert call_kwargs.kwargs["model"] == "claude-opus-4-6"
        assert call_kwargs.kwargs["cwd"] == "/tmp/test-clone"
        assert call_kwargs.kwargs["system_prompt"] == "You are a test assistant."
        assert call_kwargs.kwargs["permission_mode"] == "acceptEdits"
        assert call_kwargs.kwargs["max_turns"] == 20
        assert call_kwargs.kwargs["setting_sources"] == ["project"]
        assert "Bash" in call_kwargs.kwargs["disallowed_tools"]
        mock_client.connect.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.claude_adapter.ClaudeSDKClient")
    @patch("build_your_room.adapters.claude_adapter.ClaudeAgentOptions")
    async def test_start_session_passes_allowed_tools(
        self, mock_options_cls: MagicMock, mock_client_cls: MagicMock
    ) -> None:
        mock_client_cls.return_value = _make_mock_client()

        adapter = ClaudeAgentAdapter()
        tools = ["Read", "Write", "Edit"]
        config = _make_config(allowed_tools=tools)

        await adapter.start_session(config)

        call_kwargs = mock_options_cls.call_args
        assert call_kwargs.kwargs["allowed_tools"] == tools

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.claude_adapter.ClaudeSDKClient")
    @patch("build_your_room.adapters.claude_adapter.ClaudeAgentOptions")
    async def test_start_session_passes_resume_id(
        self, mock_options_cls: MagicMock, mock_client_cls: MagicMock
    ) -> None:
        mock_client_cls.return_value = _make_mock_client()

        adapter = ClaudeAgentAdapter()
        config = _make_config(resume_session_id="sess-old")

        await adapter.start_session(config)

        call_kwargs = mock_options_cls.call_args
        assert call_kwargs.kwargs["resume"] == "sess-old"

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.claude_adapter.ClaudeSDKClient")
    @patch("build_your_room.adapters.claude_adapter.ClaudeAgentOptions")
    async def test_start_session_passes_output_format(
        self, mock_options_cls: MagicMock, mock_client_cls: MagicMock
    ) -> None:
        mock_client_cls.return_value = _make_mock_client()

        schema = {"type": "object", "properties": {"approved": {"type": "boolean"}}}
        adapter = ClaudeAgentAdapter()
        config = _make_config(output_format=schema)

        await adapter.start_session(config)

        call_kwargs = mock_options_cls.call_args
        assert call_kwargs.kwargs["output_format"] == schema

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.claude_adapter.ClaudeSDKClient")
    @patch("build_your_room.adapters.claude_adapter.ClaudeAgentOptions")
    async def test_start_session_returns_live_session(
        self, mock_options_cls: MagicMock, mock_client_cls: MagicMock
    ) -> None:
        mock_client_cls.return_value = _make_mock_client()

        adapter = ClaudeAgentAdapter()
        session = await adapter.start_session(_make_config())

        assert isinstance(session, ClaudeLiveSession)

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.claude_adapter.ClaudeSDKClient")
    @patch("build_your_room.adapters.claude_adapter.ClaudeAgentOptions")
    async def test_start_session_with_log_buffer(
        self, mock_options_cls: MagicMock, mock_client_cls: MagicMock
    ) -> None:
        mock_client_cls.return_value = _make_mock_client()
        log_buffer = LogBuffer()

        adapter = ClaudeAgentAdapter(log_buffer=log_buffer)
        session = await adapter.start_session(_make_config())

        # The session should have the log buffer
        assert session._log_buffer is log_buffer

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.claude_adapter.ClaudeSDKClient")
    @patch("build_your_room.adapters.claude_adapter.ClaudeAgentOptions")
    async def test_start_session_sets_can_use_tool(
        self, mock_options_cls: MagicMock, mock_client_cls: MagicMock
    ) -> None:
        mock_client_cls.return_value = _make_mock_client()

        adapter = ClaudeAgentAdapter()
        await adapter.start_session(_make_config())

        call_kwargs = mock_options_cls.call_args
        assert call_kwargs.kwargs["can_use_tool"] is not None
        assert callable(call_kwargs.kwargs["can_use_tool"])

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.claude_adapter.ClaudeSDKClient")
    @patch("build_your_room.adapters.claude_adapter.ClaudeAgentOptions")
    async def test_start_session_forwards_mcp_servers(
        self, mock_options_cls: MagicMock, mock_client_cls: MagicMock
    ) -> None:
        """SessionConfig.mcp_servers must be forwarded to ClaudeAgentOptions."""
        mock_client_cls.return_value = _make_mock_client()

        servers = {"harness": {"type": "sdk", "name": "harness", "instance": object()}}
        adapter = ClaudeAgentAdapter()
        config = _make_config(mcp_servers=servers)

        await adapter.start_session(config)

        call_kwargs = mock_options_cls.call_args
        assert call_kwargs.kwargs["mcp_servers"] is servers

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.claude_adapter.ClaudeSDKClient")
    @patch("build_your_room.adapters.claude_adapter.ClaudeAgentOptions")
    async def test_start_session_default_mcp_servers_is_empty_dict(
        self, mock_options_cls: MagicMock, mock_client_cls: MagicMock
    ) -> None:
        """When SessionConfig.mcp_servers isn't set, an empty dict reaches the SDK."""
        mock_client_cls.return_value = _make_mock_client()

        adapter = ClaudeAgentAdapter()
        await adapter.start_session(_make_config())

        call_kwargs = mock_options_cls.call_args
        assert call_kwargs.kwargs["mcp_servers"] == {}


# ---------------------------------------------------------------------------
# Integration: context monitor + adapter context usage normalisation
# ---------------------------------------------------------------------------


class TestContextMonitorIntegration:
    @pytest.mark.asyncio
    async def test_normalised_usage_works_with_context_monitor(self) -> None:
        """Verify that get_context_usage output feeds into ContextMonitor.parse_claude_usage."""
        from build_your_room.context_monitor import ContextMonitor

        client = _make_mock_client(context_usage={
            "totalTokens": 130000,
            "maxTokens": 200000,
            "percentage": 65.0,
            "categories": [],
        })
        config = _make_config()
        session = ClaudeLiveSession(client, config)

        raw_usage = await session.get_context_usage()
        assert raw_usage is not None

        parsed = ContextMonitor.parse_claude_usage(raw_usage)
        assert parsed is not None
        assert parsed.total_tokens == 130000
        assert parsed.max_tokens == 200000
        assert parsed.usage_pct == pytest.approx(65.0)


# ---------------------------------------------------------------------------
# Integration: tool profile enforcement
# ---------------------------------------------------------------------------


class TestToolProfileIntegration:
    def test_file_only_profile_matches_allowed_tools(self) -> None:
        """Spec-author stages should only get file tools."""
        from build_your_room.tool_profiles import get_tool_profile

        profile = get_tool_profile("spec_author")
        config = _make_config(allowed_tools=list(profile.all_tools))

        assert "Read" in config.allowed_tools
        assert "Write" in config.allowed_tools
        assert "run_tests" not in config.allowed_tools

    def test_impl_task_profile_includes_harness_tools(self) -> None:
        """Impl-task stages should get file tools + qualified harness MCP tools."""
        from build_your_room.harness_mcp import qualified_tool_name
        from build_your_room.tool_profiles import get_tool_profile

        profile = get_tool_profile("impl_task")
        config = _make_config(allowed_tools=list(profile.all_tools))

        assert "Read" in config.allowed_tools
        assert qualified_tool_name("run_tests") in config.allowed_tools
        assert qualified_tool_name("run_lint") in config.allowed_tools
        # Bare names must NOT leak through — the SDK's allowed_tools
        # filter compares against the qualified MCP names.
        assert "run_tests" not in config.allowed_tools
        assert "run_lint" not in config.allowed_tools

    def test_denied_tools_never_in_any_profile(self) -> None:
        """No stage profile should include denied tools."""
        from build_your_room.tool_profiles import get_tool_profile

        for stage_type in [
            "spec_author", "impl_plan", "impl_task",
            "code_review", "validation", "custom",
        ]:
            profile = get_tool_profile(stage_type)
            for tool in DENIED_TOOLS:
                assert tool not in profile.all_tools, (
                    f"Denied tool {tool!r} found in {stage_type} profile"
                )


# ---------------------------------------------------------------------------
# Integration: workspace sandbox + adapter config
# ---------------------------------------------------------------------------


class TestWorkspaceSandboxIntegration:
    def test_sandbox_roots_match_config_roots(self, tmp_path: Path) -> None:
        """SessionConfig.allowed_roots should match WorkspaceSandbox.allowed_roots."""
        from build_your_room.sandbox import WorkspaceSandbox

        sandbox = WorkspaceSandbox.for_pipeline(
            clone_path=tmp_path / "clone",
            pipelines_dir=tmp_path / "pipelines",
            pipeline_id=99,
        )
        config = _make_config(
            clone_path=str(sandbox.clone_path),
            allowed_roots=[str(r) for r in sandbox.allowed_roots],
        )

        assert len(config.allowed_roots) == 4
        assert str(sandbox.clone_path) in config.allowed_roots
        assert str(sandbox.logs_dir) in config.allowed_roots
        assert str(sandbox.artifacts_dir) in config.allowed_roots
        assert str(sandbox.state_dir) in config.allowed_roots
