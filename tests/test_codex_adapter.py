"""Tests for CodexAppServerAdapter — JSON-RPC protocol, thread management,
turn handling, context usage estimation, snapshot/resume, cleanup, sandbox
config, structured output, and error handling.

All Codex app-server interactions are mocked via fake subprocess stdin/stdout.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from build_your_room.adapters.base import SessionConfig
from build_your_room.adapters.codex_adapter import (
    CodexAppServerAdapter,
    CodexLiveSession,
    CodexProtocolError,
    CodexTurnResult,
    _read_message,
    _send_and_wait,
    _send_message,
)
from build_your_room.streaming import LogBuffer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> SessionConfig:
    """Build a SessionConfig with sensible test defaults for Codex."""
    defaults: dict[str, Any] = {
        "model": "gpt-5.1-codex",
        "clone_path": "/tmp/test-clone",
        "system_prompt": "You are a test review assistant.",
        "allowed_tools": [],
        "allowed_roots": ["/tmp/test-clone", "/tmp/test-logs"],
        "max_turns": 10,
        "context_threshold_pct": 60.0,
        "pipeline_id": 1,
        "stage_id": 42,
        "session_db_id": 8,
    }
    defaults.update(overrides)
    return SessionConfig(**defaults)


class FakeStdin:
    """Mock stdin that records written bytes."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self._closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self._closed = True


class FakeStdout:
    """Mock stdout that yields pre-configured JSON-RPC responses."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        for resp in responses:
            self._queue.put_nowait(json.dumps(resp).encode() + b"\n")

    def add_response(self, resp: dict[str, Any]) -> None:
        self._queue.put_nowait(json.dumps(resp).encode() + b"\n")

    async def readline(self) -> bytes:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            return b""


def _make_fake_proc(
    responses: list[dict[str, Any]] | None = None,
) -> tuple[MagicMock, FakeStdin, FakeStdout]:
    """Build a mock subprocess with fake stdin/stdout for JSON-RPC testing."""
    stdin = FakeStdin()
    stdout = FakeStdout(responses or [])

    proc = MagicMock()
    proc.stdin = stdin
    proc.stdout = stdout
    proc.stderr = MagicMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    proc.pid = 12345

    return proc, stdin, stdout


def _make_turn_response(
    request_id: int,
    message: str = "Done.",
    structured_output: dict[str, Any] | None = None,
    input_tokens: int = 300,
    output_tokens: int = 150,
    cost_usd: float = 0.005,
    is_error: bool = False,
) -> dict[str, Any]:
    """Build a fake turn/start response."""
    result: dict[str, Any] = {
        "message": message,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
        "cost_usd": cost_usd,
        "is_error": is_error,
    }
    if structured_output is not None:
        result["structured_output"] = structured_output
    return {"id": request_id, "result": result}


# ---------------------------------------------------------------------------
# JSON-RPC transport tests
# ---------------------------------------------------------------------------


class TestSendMessage:
    """Verify _send_message serializes JSON-RPC messages to stdin."""

    @pytest.mark.asyncio
    async def test_writes_json_line(self) -> None:
        """_send_message writes a JSON line terminated with newline."""
        proc, stdin, _ = _make_fake_proc()
        msg = {"method": "test", "id": 1, "params": {}}

        await _send_message(proc, msg)

        assert len(stdin.written) == 1
        decoded = json.loads(stdin.written[0].decode().strip())
        assert decoded == msg

    @pytest.mark.asyncio
    async def test_preserves_message_structure(self) -> None:
        """Complex nested params survive serialization roundtrip."""
        proc, stdin, _ = _make_fake_proc()
        msg = {
            "method": "thread/start",
            "id": 99,
            "params": {"sandbox": {"mode": "workspace-write", "writableRoots": ["/a", "/b"]}},
        }

        await _send_message(proc, msg)

        decoded = json.loads(stdin.written[0].decode().strip())
        assert decoded["params"]["sandbox"]["writableRoots"] == ["/a", "/b"]


class TestReadMessage:
    """Verify _read_message parses JSON-RPC responses from stdout."""

    @pytest.mark.asyncio
    async def test_reads_valid_json(self) -> None:
        """Reads and parses a single JSON line."""
        proc, _, stdout = _make_fake_proc([{"id": 0, "result": {"ok": True}}])

        msg = await _read_message(proc, timeout=5.0)

        assert msg == {"id": 0, "result": {"ok": True}}

    @pytest.mark.asyncio
    async def test_skips_blank_lines(self) -> None:
        """Blank lines between responses are silently skipped."""
        proc, _, _ = _make_fake_proc()
        # Manually push a blank line then a real response
        proc.stdout._queue.put_nowait(b"\n")
        proc.stdout._queue.put_nowait(json.dumps({"id": 1, "result": {}}).encode() + b"\n")

        msg = await _read_message(proc, timeout=5.0)

        assert msg["id"] == 1

    @pytest.mark.asyncio
    async def test_raises_on_invalid_json(self) -> None:
        """Invalid JSON raises CodexProtocolError."""
        proc, _, _ = _make_fake_proc()
        proc.stdout._queue.put_nowait(b"not-json\n")

        with pytest.raises(CodexProtocolError, match="Invalid JSON"):
            await _read_message(proc, timeout=5.0)

    @pytest.mark.asyncio
    async def test_raises_on_eof(self) -> None:
        """Empty read (EOF) raises CodexProtocolError."""
        proc, _, _ = _make_fake_proc()
        proc.stdout._queue.put_nowait(b"")

        with pytest.raises(CodexProtocolError, match="closed stdout"):
            await _read_message(proc, timeout=5.0)


class TestSendAndWait:
    """Verify _send_and_wait matches responses by request id."""

    @pytest.mark.asyncio
    async def test_matches_response_by_id(self) -> None:
        """Returns the response matching the request id."""
        proc, _, stdout = _make_fake_proc()
        # Queue a notification (no matching id) then the real response
        stdout.add_response({"method": "some/notification", "params": {}})
        stdout.add_response({"id": 5, "result": {"data": "found"}})

        resp = await _send_and_wait(
            proc, {"method": "test", "id": 5, "params": {}}, timeout=5.0
        )

        assert resp["result"]["data"] == "found"

    @pytest.mark.asyncio
    async def test_raises_on_rpc_error(self) -> None:
        """JSON-RPC error response raises CodexProtocolError."""
        proc, _, stdout = _make_fake_proc()
        stdout.add_response({
            "id": 3,
            "error": {"code": -32600, "message": "Invalid request"},
        })

        with pytest.raises(CodexProtocolError, match="Invalid request"):
            await _send_and_wait(
                proc, {"method": "bad", "id": 3, "params": {}}, timeout=5.0
            )

    @pytest.mark.asyncio
    async def test_skips_mismatched_ids(self) -> None:
        """Responses with wrong ids are skipped until the right one arrives."""
        proc, _, stdout = _make_fake_proc()
        stdout.add_response({"id": 99, "result": {"wrong": True}})
        stdout.add_response({"id": 7, "result": {"right": True}})

        resp = await _send_and_wait(
            proc, {"method": "test", "id": 7, "params": {}}, timeout=5.0
        )

        assert resp["result"]["right"] is True


# ---------------------------------------------------------------------------
# CodexTurnResult tests
# ---------------------------------------------------------------------------


class TestCodexTurnResult:
    """Verify CodexTurnResult data structure."""

    def test_basic_result(self) -> None:
        """Basic turn result stores output text and defaults."""
        r = CodexTurnResult(output="hello", structured_output=None)
        assert r.output == "hello"
        assert r.structured_output is None
        assert r.is_error is False
        assert r.thread_id is None

    def test_with_structured_output(self) -> None:
        """Structured output from review decisions is preserved."""
        data = {"approved": True, "max_severity": "low", "issues": []}
        r = CodexTurnResult(
            output="Approved.", structured_output=data, thread_id="t-abc"
        )
        assert r.structured_output == data
        assert r.thread_id == "t-abc"

    def test_error_result(self) -> None:
        """Error turns set is_error flag."""
        r = CodexTurnResult(output="Error", structured_output=None, is_error=True)
        assert r.is_error is True


# ---------------------------------------------------------------------------
# CodexLiveSession tests
# ---------------------------------------------------------------------------


class TestCodexLiveSession:
    @pytest.fixture
    def config(self) -> SessionConfig:
        return _make_config()

    @pytest.fixture
    def log_buffer(self) -> LogBuffer:
        return LogBuffer()

    @pytest.mark.asyncio
    async def test_session_id_is_thread_id(self, config: SessionConfig) -> None:
        """session_id property returns the Codex thread id."""
        proc, _, _ = _make_fake_proc()
        session = CodexLiveSession(proc, "thread-xyz", config)

        assert session.session_id == "thread-xyz"

    @pytest.mark.asyncio
    async def test_send_turn_collects_text(self, config: SessionConfig) -> None:
        """send_turn extracts message text from the response."""
        proc, _, stdout = _make_fake_proc()
        stdout.add_response(_make_turn_response(100, message="All done."))
        session = CodexLiveSession(proc, "t-1", config)

        result = await session.send_turn("Do something")

        assert result.output == "All done."
        assert result.is_error is False

    @pytest.mark.asyncio
    async def test_send_turn_captures_structured_output(self, config: SessionConfig) -> None:
        """Structured output from review/approval is captured."""
        data = {"approved": True, "max_severity": "none"}
        proc, _, stdout = _make_fake_proc()
        stdout.add_response(_make_turn_response(100, structured_output=data))
        session = CodexLiveSession(proc, "t-1", config)

        result = await session.send_turn("review this")

        assert result.structured_output == data

    @pytest.mark.asyncio
    async def test_send_turn_with_output_schema(self, config: SessionConfig) -> None:
        """outputSchema is included in the turn/start params."""
        proc, stdin, stdout = _make_fake_proc()
        stdout.add_response(_make_turn_response(100))
        session = CodexLiveSession(proc, "t-1", config)

        schema = {"type": "object", "properties": {"approved": {"type": "boolean"}}}
        await session.send_turn("review", output_schema=schema)

        # Check that the sent message includes outputSchema
        sent = json.loads(stdin.written[0].decode().strip())
        assert sent["params"]["outputSchema"] == schema

    @pytest.mark.asyncio
    async def test_send_turn_accumulates_tokens(self, config: SessionConfig) -> None:
        """Token counts accumulate across multiple turns."""
        proc, _, stdout = _make_fake_proc()
        stdout.add_response(
            _make_turn_response(100, input_tokens=200, output_tokens=100)
        )
        stdout.add_response(
            _make_turn_response(101, input_tokens=300, output_tokens=150)
        )
        session = CodexLiveSession(proc, "t-1", config)

        await session.send_turn("turn 1")
        await session.send_turn("turn 2")

        snap = await session.snapshot()
        assert snap["total_input_tokens"] == 500
        assert snap["total_output_tokens"] == 250

    @pytest.mark.asyncio
    async def test_send_turn_accumulates_cost(self, config: SessionConfig) -> None:
        """Cost accumulates across multiple turns."""
        proc, _, stdout = _make_fake_proc()
        stdout.add_response(_make_turn_response(100, cost_usd=0.01))
        stdout.add_response(_make_turn_response(101, cost_usd=0.02))
        session = CodexLiveSession(proc, "t-1", config)

        await session.send_turn("turn 1")
        await session.send_turn("turn 2")

        snap = await session.snapshot()
        assert snap["total_cost_usd"] == pytest.approx(0.03, abs=0.001)

    @pytest.mark.asyncio
    async def test_send_turn_handles_error_flag(self, config: SessionConfig) -> None:
        """Error turns are flagged in the result."""
        proc, _, stdout = _make_fake_proc()
        stdout.add_response(_make_turn_response(100, is_error=True, message="Failed"))
        session = CodexLiveSession(proc, "t-1", config)

        result = await session.send_turn("fail")

        assert result.is_error is True

    @pytest.mark.asyncio
    async def test_send_turn_handles_missing_usage(self, config: SessionConfig) -> None:
        """Turns with no usage data don't break token tracking."""
        proc, _, stdout = _make_fake_proc()
        stdout.add_response({"id": 100, "result": {"message": "ok"}})
        session = CodexLiveSession(proc, "t-1", config)

        result = await session.send_turn("hello")

        assert result.output == "ok"
        snap = await session.snapshot()
        assert snap["total_input_tokens"] == 0

    @pytest.mark.asyncio
    async def test_send_turn_increments_request_id(self, config: SessionConfig) -> None:
        """Each turn uses an incrementing request id."""
        proc, stdin, stdout = _make_fake_proc()
        stdout.add_response(_make_turn_response(100))
        stdout.add_response(_make_turn_response(101))
        session = CodexLiveSession(proc, "t-1", config)

        await session.send_turn("turn 1")
        await session.send_turn("turn 2")

        sent_ids = [json.loads(b.decode().strip())["id"] for b in stdin.written]
        assert sent_ids == [100, 101]

    @pytest.mark.asyncio
    async def test_get_context_usage_default(self, config: SessionConfig) -> None:
        """Context usage estimates percentage from token counts."""
        proc, _, stdout = _make_fake_proc()
        stdout.add_response(
            _make_turn_response(100, input_tokens=64000, output_tokens=0)
        )
        session = CodexLiveSession(proc, "t-1", config)
        await session.send_turn("big prompt")

        usage = await session.get_context_usage()

        assert usage is not None
        assert usage["total_tokens"] == 64000
        assert usage["max_tokens"] == 128_000
        assert usage["percentage"] == 50.0

    @pytest.mark.asyncio
    async def test_get_context_usage_custom_max(self) -> None:
        """Custom max_context_tokens from config.extra is respected."""
        config = _make_config(extra={"max_context_tokens": 200_000})
        proc, _, stdout = _make_fake_proc()
        stdout.add_response(
            _make_turn_response(100, input_tokens=100_000, output_tokens=0)
        )
        session = CodexLiveSession(proc, "t-1", config)
        await session.send_turn("prompt")

        usage = await session.get_context_usage()

        assert usage is not None
        assert usage["max_tokens"] == 200_000
        assert usage["percentage"] == 50.0

    @pytest.mark.asyncio
    async def test_get_context_usage_zero_max_returns_none(self) -> None:
        """Zero max_context_tokens returns None (avoid division by zero)."""
        config = _make_config(extra={"max_context_tokens": 0})
        proc, _, _ = _make_fake_proc()
        session = CodexLiveSession(proc, "t-1", config)

        usage = await session.get_context_usage()

        assert usage is None

    @pytest.mark.asyncio
    async def test_snapshot_includes_session_metadata(self, config: SessionConfig) -> None:
        """Snapshot contains all fields needed for resume."""
        proc, _, stdout = _make_fake_proc()
        stdout.add_response(_make_turn_response(100))
        session = CodexLiveSession(proc, "t-snap", config)
        await session.send_turn("work")

        snap = await session.snapshot()

        assert snap["session_id"] == "t-snap"
        assert snap["model"] == "gpt-5.1-codex"
        assert snap["clone_path"] == "/tmp/test-clone"
        assert snap["pipeline_id"] == 1
        assert snap["stage_id"] == 42
        assert snap["turn_count"] == 1

    @pytest.mark.asyncio
    async def test_close_terminates_process(self, config: SessionConfig) -> None:
        """close() terminates the subprocess and waits."""
        proc, _, _ = _make_fake_proc()
        session = CodexLiveSession(proc, "t-1", config)

        await session.close()

        proc.terminate.assert_called_once()
        proc.wait.assert_awaited()

    @pytest.mark.asyncio
    async def test_close_kills_on_timeout(self, config: SessionConfig) -> None:
        """close() kills the process if terminate doesn't finish in time."""
        proc, _, _ = _make_fake_proc()
        # First wait (after terminate) times out, second wait (after kill) succeeds
        proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError, 0])
        session = CodexLiveSession(proc, "t-1", config)

        await session.close()

        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_handles_already_dead(self, config: SessionConfig) -> None:
        """close() handles ProcessLookupError if process already exited."""
        proc, _, _ = _make_fake_proc()
        proc.terminate = MagicMock(side_effect=ProcessLookupError)
        session = CodexLiveSession(proc, "t-1", config)

        await session.close()  # should not raise

    @pytest.mark.asyncio
    async def test_logs_to_buffer(
        self, config: SessionConfig, log_buffer: LogBuffer
    ) -> None:
        """Turn completion logs to the pipeline log buffer."""
        proc, _, stdout = _make_fake_proc()
        stdout.add_response(_make_turn_response(100))
        session = CodexLiveSession(proc, "t-1", config, log_buffer)

        await session.send_turn("hello")

        history = log_buffer.get_history(1)
        assert any("[codex]" in msg for msg in history)

    @pytest.mark.asyncio
    async def test_no_log_without_buffer(self, config: SessionConfig) -> None:
        """No error when log buffer is None."""
        proc, _, stdout = _make_fake_proc()
        stdout.add_response(_make_turn_response(100))
        session = CodexLiveSession(proc, "t-1", config, None)

        await session.send_turn("hello")  # should not raise

    @pytest.mark.asyncio
    async def test_no_log_without_pipeline_id(self, log_buffer: LogBuffer) -> None:
        """No error when pipeline_id is None."""
        config = _make_config(pipeline_id=None)
        proc, _, stdout = _make_fake_proc()
        stdout.add_response(_make_turn_response(100))
        session = CodexLiveSession(proc, "t-1", config, log_buffer)

        await session.send_turn("hello")  # should not raise
        assert log_buffer.get_history(1) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CodexAppServerAdapter tests
# ---------------------------------------------------------------------------


class TestCodexAppServerAdapter:
    @pytest.mark.asyncio
    @patch("build_your_room.adapters.codex_adapter.asyncio.create_subprocess_exec")
    async def test_start_session_performs_handshake(
        self, mock_create: AsyncMock
    ) -> None:
        """start_session sends initialize, initialized, thread/start sequence."""
        proc, stdin, stdout = _make_fake_proc()
        # Queue: initialize response, thread/start response
        stdout.add_response({"id": 0, "result": {"capabilities": {}}})
        stdout.add_response(
            {"id": 1, "result": {"thread": {"id": "thread-abc123"}}}
        )
        mock_create.return_value = proc

        adapter = CodexAppServerAdapter()
        config = _make_config()

        session = await adapter.start_session(config)

        assert isinstance(session, CodexLiveSession)
        assert session.session_id == "thread-abc123"
        # Verify 3 messages sent: initialize, initialized, thread/start
        assert len(stdin.written) == 3

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.codex_adapter.asyncio.create_subprocess_exec")
    async def test_start_session_sends_correct_initialize(
        self, mock_create: AsyncMock
    ) -> None:
        """Initialize message contains correct clientInfo."""
        proc, stdin, stdout = _make_fake_proc()
        stdout.add_response({"id": 0, "result": {}})
        stdout.add_response({"id": 1, "result": {"thread": {"id": "t-1"}}})
        mock_create.return_value = proc

        adapter = CodexAppServerAdapter()
        await adapter.start_session(_make_config())

        init_msg = json.loads(stdin.written[0].decode().strip())
        assert init_msg["method"] == "initialize"
        assert init_msg["params"]["clientInfo"]["name"] == "build_your_room"

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.codex_adapter.asyncio.create_subprocess_exec")
    async def test_start_session_sends_initialized_notification(
        self, mock_create: AsyncMock
    ) -> None:
        """Initialized notification has no id (it's a notification)."""
        proc, stdin, stdout = _make_fake_proc()
        stdout.add_response({"id": 0, "result": {}})
        stdout.add_response({"id": 1, "result": {"thread": {"id": "t-1"}}})
        mock_create.return_value = proc

        adapter = CodexAppServerAdapter()
        await adapter.start_session(_make_config())

        notif_msg = json.loads(stdin.written[1].decode().strip())
        assert notif_msg["method"] == "initialized"
        assert "id" not in notif_msg

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.codex_adapter.asyncio.create_subprocess_exec")
    async def test_start_session_passes_sandbox_config(
        self, mock_create: AsyncMock
    ) -> None:
        """thread/start includes workspace-write sandbox with writableRoots."""
        proc, stdin, stdout = _make_fake_proc()
        stdout.add_response({"id": 0, "result": {}})
        stdout.add_response({"id": 1, "result": {"thread": {"id": "t-1"}}})
        mock_create.return_value = proc

        adapter = CodexAppServerAdapter()
        roots = ["/clone", "/logs", "/artifacts"]
        config = _make_config(allowed_roots=roots)

        await adapter.start_session(config)

        thread_msg = json.loads(stdin.written[2].decode().strip())
        sandbox = thread_msg["params"]["sandbox"]
        assert sandbox["mode"] == "workspace-write"
        assert set(sandbox["writableRoots"]) == set(roots)

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.codex_adapter.asyncio.create_subprocess_exec")
    async def test_start_session_passes_model(
        self, mock_create: AsyncMock
    ) -> None:
        """thread/start uses the model from SessionConfig."""
        proc, stdin, stdout = _make_fake_proc()
        stdout.add_response({"id": 0, "result": {}})
        stdout.add_response({"id": 1, "result": {"thread": {"id": "t-1"}}})
        mock_create.return_value = proc

        adapter = CodexAppServerAdapter()
        config = _make_config(model="gpt-5.1-codex")

        await adapter.start_session(config)

        thread_msg = json.loads(stdin.written[2].decode().strip())
        assert thread_msg["params"]["model"] == "gpt-5.1-codex"

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.codex_adapter.asyncio.create_subprocess_exec")
    async def test_start_session_passes_cwd(
        self, mock_create: AsyncMock
    ) -> None:
        """Subprocess and thread/start both use config.clone_path as cwd."""
        proc, stdin, stdout = _make_fake_proc()
        stdout.add_response({"id": 0, "result": {}})
        stdout.add_response({"id": 1, "result": {"thread": {"id": "t-1"}}})
        mock_create.return_value = proc

        adapter = CodexAppServerAdapter()
        config = _make_config(clone_path="/my/repo")

        await adapter.start_session(config)

        # Check subprocess cwd
        mock_create.assert_awaited_once()
        call_kwargs = mock_create.call_args
        assert call_kwargs.kwargs["cwd"] == "/my/repo"

        # Check thread/start cwd
        thread_msg = json.loads(stdin.written[2].decode().strip())
        assert thread_msg["params"]["cwd"] == "/my/repo"

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.codex_adapter.asyncio.create_subprocess_exec")
    async def test_start_session_sets_approval_policy_never(
        self, mock_create: AsyncMock
    ) -> None:
        """thread/start sets approvalPolicy to 'never' for automated operation."""
        proc, stdin, stdout = _make_fake_proc()
        stdout.add_response({"id": 0, "result": {}})
        stdout.add_response({"id": 1, "result": {"thread": {"id": "t-1"}}})
        mock_create.return_value = proc

        adapter = CodexAppServerAdapter()
        await adapter.start_session(_make_config())

        thread_msg = json.loads(stdin.written[2].decode().strip())
        assert thread_msg["params"]["approvalPolicy"] == "never"

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.codex_adapter.asyncio.create_subprocess_exec")
    async def test_start_session_with_log_buffer(
        self, mock_create: AsyncMock
    ) -> None:
        """Log buffer is passed through to the session."""
        proc, _, stdout = _make_fake_proc()
        stdout.add_response({"id": 0, "result": {}})
        stdout.add_response({"id": 1, "result": {"thread": {"id": "t-1"}}})
        mock_create.return_value = proc

        log_buffer = LogBuffer()
        adapter = CodexAppServerAdapter(log_buffer=log_buffer)
        session = await adapter.start_session(_make_config())

        assert session._log_buffer is log_buffer

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.codex_adapter.asyncio.create_subprocess_exec")
    async def test_start_session_custom_binary(
        self, mock_create: AsyncMock
    ) -> None:
        """Custom codex binary path is used for subprocess."""
        proc, _, stdout = _make_fake_proc()
        stdout.add_response({"id": 0, "result": {}})
        stdout.add_response({"id": 1, "result": {"thread": {"id": "t-1"}}})
        mock_create.return_value = proc

        adapter = CodexAppServerAdapter(codex_binary="/usr/local/bin/codex")
        await adapter.start_session(_make_config())

        args = mock_create.call_args[0]
        assert args[0] == "/usr/local/bin/codex"
        assert args[1] == "app-server"

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.codex_adapter.asyncio.create_subprocess_exec")
    async def test_start_session_cleans_up_on_handshake_failure(
        self, mock_create: AsyncMock
    ) -> None:
        """Subprocess is terminated if the handshake fails."""
        proc, _, stdout = _make_fake_proc()
        # Initialize returns an error
        stdout.add_response({
            "id": 0,
            "error": {"code": -32600, "message": "Bad init"},
        })
        mock_create.return_value = proc

        adapter = CodexAppServerAdapter()

        with pytest.raises(CodexProtocolError, match="Bad init"):
            await adapter.start_session(_make_config())

        proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    @patch("build_your_room.adapters.codex_adapter.asyncio.create_subprocess_exec")
    async def test_start_session_cleans_up_on_thread_start_failure(
        self, mock_create: AsyncMock
    ) -> None:
        """Subprocess is terminated if thread/start fails."""
        proc, _, stdout = _make_fake_proc()
        stdout.add_response({"id": 0, "result": {}})
        stdout.add_response({
            "id": 1,
            "error": {"code": -32603, "message": "Thread failed"},
        })
        mock_create.return_value = proc

        adapter = CodexAppServerAdapter()

        with pytest.raises(CodexProtocolError, match="Thread failed"):
            await adapter.start_session(_make_config())

        proc.terminate.assert_called_once()


# ---------------------------------------------------------------------------
# Integration: context monitor + codex context usage estimation
# ---------------------------------------------------------------------------


class TestContextMonitorIntegration:
    @pytest.mark.asyncio
    async def test_estimated_usage_works_with_context_monitor(self) -> None:
        """Verify that get_context_usage output feeds into ContextMonitor.parse_codex_usage.

        parse_codex_usage takes (token_input, token_output, max_tokens) — we
        extract these from the dict returned by get_context_usage plus the
        session's accumulated token counts.
        """
        from build_your_room.context_monitor import ContextMonitor

        config = _make_config()
        proc, _, stdout = _make_fake_proc()
        stdout.add_response(
            _make_turn_response(100, input_tokens=70000, output_tokens=10000)
        )
        session = CodexLiveSession(proc, "t-1", config)
        await session.send_turn("big work")

        raw_usage = await session.get_context_usage()
        assert raw_usage is not None

        # parse_codex_usage expects individual token counts + max
        parsed = ContextMonitor.parse_codex_usage(
            token_input=session._total_input_tokens,
            token_output=session._total_output_tokens,
            max_tokens=raw_usage["max_tokens"],
        )
        assert parsed is not None
        assert parsed.total_tokens == 80000
        assert parsed.max_tokens == 128_000
        assert parsed.usage_pct == pytest.approx(62.5)


# ---------------------------------------------------------------------------
# Integration: sandbox config
# ---------------------------------------------------------------------------


class TestSandboxIntegration:
    def test_codex_sandbox_config_from_workspace(self, tmp_path) -> None:
        """WorkspaceSandbox.writable_roots_list feeds into CodexSandboxConfig."""
        from build_your_room.sandbox import WorkspaceSandbox

        sandbox = WorkspaceSandbox.for_pipeline(
            clone_path=tmp_path / "clone",
            pipelines_dir=tmp_path / "pipelines",
            pipeline_id=42,
        )
        config = _make_config(
            clone_path=str(sandbox.clone_path),
            allowed_roots=sandbox.writable_roots_list,
        )

        assert len(config.allowed_roots) == 4
        assert str(sandbox.clone_path) in config.allowed_roots

    def test_codex_sandbox_config_produces_correct_mode(self) -> None:
        """get_codex_sandbox_config returns workspace-write mode."""
        from build_your_room.tool_profiles import get_codex_sandbox_config

        cfg = get_codex_sandbox_config(["/a", "/b"])
        assert cfg.mode == "workspace-write"
        assert cfg.writable_roots == ("/a", "/b")
