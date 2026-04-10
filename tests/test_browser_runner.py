"""Tests for BrowserRunner — dev-browser bridge, availability detection,
recording integration, dev server readiness, and graceful degradation.

Covers: is_available() detection, _DevBrowserBridge JSON-RPC protocol,
bridge launch/close lifecycle, browser_open/browser_run_scenario/
browser_console_errors/browser_record_artifact delegation to bridge,
fallback behavior when bridge unavailable, dev server readiness probing,
cleanup lifecycle, property-based tests for recording metadata and
availability invariants.

All subprocess interactions are mocked — no real dev-browser or Node.js.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from build_your_room.browser_runner import (
    BrowserRunner,
    DevBrowserBridgeError,
    _DevBrowserBridge,
    _BRIDGE_ENTRY_CANDIDATES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runner(tmp_path: Path, skill_path: Path | None = None) -> BrowserRunner:
    """Build a BrowserRunner with tmp_path-based directories."""
    return BrowserRunner(
        clone_path=tmp_path / "clone",
        logs_dir=tmp_path / "logs",
        artifacts_dir=tmp_path / "artifacts",
        state_dir=tmp_path / "state",
        devbrowser_skill_path=skill_path,
    )


def _make_skill_dir(tmp_path: Path, entry: str = "bridge.js") -> Path:
    """Create a fake dev-browser skill directory with an entry point."""
    skill_dir = tmp_path / "dev-browser-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / entry).write_text("// fake entry")
    return skill_dir


def _make_mock_bridge_process(
    responses: list[dict[str, Any]] | None = None,
) -> AsyncMock:
    """Build a mock subprocess with stdin/stdout for bridge communication."""
    proc = AsyncMock()
    proc.returncode = None
    proc.pid = 12345

    proc.stdin = AsyncMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()

    # Build stdout readline responses from the provided dicts
    if responses:
        lines = [
            (json.dumps(r) + "\n").encode() for r in responses
        ]
        proc.stdout = AsyncMock()
        proc.stdout.readline = AsyncMock(side_effect=lines)
    else:
        proc.stdout = AsyncMock()
        proc.stdout.readline = AsyncMock(return_value=b"")

    return proc


# ---------------------------------------------------------------------------
# is_available() tests
# ---------------------------------------------------------------------------


class TestIsAvailable:
    """Tests for BrowserRunner.is_available() detection."""

    def test_available_with_bridge_js(self, tmp_path: Path) -> None:
        """Skill directory with bridge.js is detected as available.

        Invariant: is_available returns True iff skill path is a directory
        containing at least one known entry-point file.
        """
        skill_dir = _make_skill_dir(tmp_path, "bridge.js")
        assert BrowserRunner.is_available(skill_dir) is True

    def test_available_with_server_js(self, tmp_path: Path) -> None:
        """Skill directory with server.js is detected as available."""
        skill_dir = _make_skill_dir(tmp_path, "server.js")
        assert BrowserRunner.is_available(skill_dir) is True

    def test_available_with_index_js(self, tmp_path: Path) -> None:
        """Skill directory with index.js is detected as available."""
        skill_dir = _make_skill_dir(tmp_path, "index.js")
        assert BrowserRunner.is_available(skill_dir) is True

    def test_not_available_none_path(self) -> None:
        """None skill path is not available.

        Invariant: None path always returns False.
        """
        assert BrowserRunner.is_available(None) is False

    def test_not_available_missing_dir(self, tmp_path: Path) -> None:
        """Non-existent directory is not available."""
        assert BrowserRunner.is_available(tmp_path / "nonexistent") is False

    def test_not_available_empty_dir(self, tmp_path: Path) -> None:
        """Directory without entry-point files is not available."""
        skill_dir = tmp_path / "empty-skill"
        skill_dir.mkdir()
        assert BrowserRunner.is_available(skill_dir) is False

    def test_not_available_wrong_files(self, tmp_path: Path) -> None:
        """Directory with non-matching files is not available."""
        skill_dir = tmp_path / "wrong-skill"
        skill_dir.mkdir()
        (skill_dir / "README.md").write_text("# Dev browser")
        (skill_dir / "package.json").write_text("{}")
        assert BrowserRunner.is_available(skill_dir) is False

    @given(
        entry=st.sampled_from(list(_BRIDGE_ENTRY_CANDIDATES)),
    )
    @settings(max_examples=10)
    def test_any_known_entry_makes_available(self, entry: str) -> None:
        """Property: any recognized entry-point file makes the skill available.

        Invariant: for all e in _BRIDGE_ENTRY_CANDIDATES,
        dir_with(e) => is_available(dir) is True.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "skill"
            skill_dir.mkdir()
            (skill_dir / entry).write_text("// entry")
            assert BrowserRunner.is_available(skill_dir) is True


# ---------------------------------------------------------------------------
# _DevBrowserBridge tests
# ---------------------------------------------------------------------------


class TestDevBrowserBridge:
    """Tests for the JSON-RPC bridge subprocess protocol."""

    @pytest.mark.asyncio
    async def test_send_command_success(self) -> None:
        """Successful command returns the result dict.

        Invariant: when subprocess responds with {"id": N, "result": {...}},
        send_command returns the result dict.
        """
        proc = _make_mock_bridge_process([
            {"id": 1, "result": {"navigated": True, "url": "http://localhost:3000"}},
        ])
        bridge = _DevBrowserBridge(process=proc)

        result = await bridge.send_command("navigate", {"url": "http://localhost:3000"})
        assert result == {"navigated": True, "url": "http://localhost:3000"}

        # Verify the request was written to stdin
        written = proc.stdin.write.call_args[0][0]
        request = json.loads(written.decode())
        assert request["method"] == "navigate"
        assert request["params"]["url"] == "http://localhost:3000"
        assert request["id"] == 1

    @pytest.mark.asyncio
    async def test_send_command_increments_id(self) -> None:
        """Each command gets an incrementing request ID.

        Invariant: request IDs are monotonically increasing.
        """
        proc = _make_mock_bridge_process([
            {"id": 1, "result": {}},
            {"id": 2, "result": {}},
        ])
        bridge = _DevBrowserBridge(process=proc)

        await bridge.send_command("cmd1")
        await bridge.send_command("cmd2")

        calls = proc.stdin.write.call_args_list
        req1 = json.loads(calls[0][0][0].decode())
        req2 = json.loads(calls[1][0][0].decode())
        assert req1["id"] == 1
        assert req2["id"] == 2

    @pytest.mark.asyncio
    async def test_send_command_error_response(self) -> None:
        """Error response from bridge raises DevBrowserBridgeError.

        Invariant: {"error": {...}} in response always raises.
        """
        proc = _make_mock_bridge_process([
            {"id": 1, "error": {"message": "Page not found"}},
        ])
        bridge = _DevBrowserBridge(process=proc)

        with pytest.raises(DevBrowserBridgeError, match="Page not found"):
            await bridge.send_command("navigate", {"url": "http://bad"})

    @pytest.mark.asyncio
    async def test_send_command_closed_stdout(self) -> None:
        """Empty response (closed stdout) raises DevBrowserBridgeError.

        Invariant: EOF on stdout always raises.
        """
        proc = _make_mock_bridge_process()  # returns b""
        bridge = _DevBrowserBridge(process=proc)

        with pytest.raises(DevBrowserBridgeError, match="closed stdout"):
            await bridge.send_command("anything")

    @pytest.mark.asyncio
    async def test_send_command_invalid_json(self) -> None:
        """Invalid JSON from bridge raises DevBrowserBridgeError.

        Invariant: non-JSON response always raises.
        """
        proc = _make_mock_bridge_process()
        proc.stdout.readline = AsyncMock(return_value=b"not json\n")
        bridge = _DevBrowserBridge(process=proc)

        with pytest.raises(DevBrowserBridgeError, match="Invalid JSON"):
            await bridge.send_command("anything")

    @pytest.mark.asyncio
    async def test_send_command_broken_pipe(self) -> None:
        """Broken pipe on stdin raises DevBrowserBridgeError.

        Invariant: write failures always raise.
        """
        proc = _make_mock_bridge_process()
        proc.stdin.write = MagicMock(side_effect=BrokenPipeError("broken"))
        bridge = _DevBrowserBridge(process=proc)

        with pytest.raises(DevBrowserBridgeError, match="Failed to write"):
            await bridge.send_command("anything")

    @pytest.mark.asyncio
    async def test_send_command_no_stdin(self) -> None:
        """Missing stdin raises DevBrowserBridgeError."""
        proc = _make_mock_bridge_process()
        proc.stdin = None
        bridge = _DevBrowserBridge(process=proc)

        with pytest.raises(DevBrowserBridgeError, match="no stdin/stdout"):
            await bridge.send_command("anything")

    @pytest.mark.asyncio
    async def test_close_terminates_process(self) -> None:
        """close() terminates the subprocess.

        Invariant: after close(), process.terminate() was called.
        """
        proc = _make_mock_bridge_process()
        proc.returncode = None
        proc.wait = AsyncMock(return_value=0)
        bridge = _DevBrowserBridge(process=proc)

        await bridge.close()
        proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_already_exited(self) -> None:
        """close() is no-op if process already exited.

        Invariant: close() on exited process does not raise.
        """
        proc = _make_mock_bridge_process()
        proc.returncode = 0  # already exited
        bridge = _DevBrowserBridge(process=proc)

        await bridge.close()
        proc.terminate.assert_not_called()


# ---------------------------------------------------------------------------
# Bridge launch/close lifecycle tests
# ---------------------------------------------------------------------------


class TestBridgeLifecycle:
    """Tests for BrowserRunner bridge launch and close."""

    @pytest.mark.asyncio
    async def test_launch_bridge_with_skill(self, tmp_path: Path) -> None:
        """Bridge launches when skill directory has entry point.

        Invariant: launch_bridge returns True and has_bridge is True.
        """
        skill_dir = _make_skill_dir(tmp_path)
        runner = _make_runner(tmp_path, skill_dir)
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)

        mock_proc = _make_mock_bridge_process()
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner.launch_bridge()

        assert result is True
        assert runner.has_bridge is True

    @pytest.mark.asyncio
    async def test_launch_bridge_no_skill(self, tmp_path: Path) -> None:
        """Bridge does not launch when skill is missing.

        Invariant: launch_bridge returns False and has_bridge stays False.
        """
        runner = _make_runner(tmp_path, None)

        result = await runner.launch_bridge()
        assert result is False
        assert runner.has_bridge is False

    @pytest.mark.asyncio
    async def test_launch_bridge_idempotent(self, tmp_path: Path) -> None:
        """Second launch_bridge call returns True without relaunching.

        Invariant: bridge is launched at most once.
        """
        skill_dir = _make_skill_dir(tmp_path)
        runner = _make_runner(tmp_path, skill_dir)
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)

        mock_proc = _make_mock_bridge_process()
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await runner.launch_bridge()
            result = await runner.launch_bridge()

        assert result is True
        assert mock_exec.call_count == 1  # only launched once

    @pytest.mark.asyncio
    async def test_launch_bridge_node_not_found(self, tmp_path: Path) -> None:
        """Node not found returns False gracefully.

        Invariant: FileNotFoundError for node is caught, returns False.
        """
        skill_dir = _make_skill_dir(tmp_path)
        runner = _make_runner(tmp_path, skill_dir)
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("node not found"),
        ):
            result = await runner.launch_bridge()

        assert result is False
        assert runner.has_bridge is False

    @pytest.mark.asyncio
    async def test_close_bridge(self, tmp_path: Path) -> None:
        """close_bridge() shuts down the bridge and clears state.

        Invariant: after close_bridge(), has_bridge is False.
        """
        skill_dir = _make_skill_dir(tmp_path)
        runner = _make_runner(tmp_path, skill_dir)
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)

        mock_proc = _make_mock_bridge_process()
        mock_proc.wait = AsyncMock(return_value=0)
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await runner.launch_bridge()

        assert runner.has_bridge is True
        await runner.close_bridge()
        assert runner.has_bridge is False


# ---------------------------------------------------------------------------
# Browser tool delegation tests
# ---------------------------------------------------------------------------


class TestBrowserOpenDelegation:
    """Tests for browser_open with and without bridge."""

    @pytest.mark.asyncio
    async def test_with_bridge(self, tmp_path: Path) -> None:
        """browser_open delegates to bridge when available.

        Invariant: bridge.send_command("navigate", ...) is called.
        """
        runner = _make_runner(tmp_path)
        mock_bridge = AsyncMock(spec=_DevBrowserBridge)
        mock_bridge.send_command = AsyncMock(
            return_value={"navigated": True, "url": "http://localhost:3000"}
        )
        runner._bridge = mock_bridge

        result = await runner.browser_open("http://localhost:3000")
        assert result["navigated"] is True
        mock_bridge.send_command.assert_called_once_with(
            "navigate", {"url": "http://localhost:3000"},
        )

    @pytest.mark.asyncio
    async def test_without_bridge(self, tmp_path: Path) -> None:
        """browser_open returns fallback when no bridge.

        Invariant: returns {"navigated": True, "url": url} without bridge.
        """
        runner = _make_runner(tmp_path)
        result = await runner.browser_open("http://localhost:3000")
        assert result == {"navigated": True, "url": "http://localhost:3000"}

    @pytest.mark.asyncio
    async def test_bridge_error_falls_back(self, tmp_path: Path) -> None:
        """browser_open falls back on bridge error.

        Invariant: DevBrowserBridgeError is caught, fallback returned.
        """
        runner = _make_runner(tmp_path)
        mock_bridge = AsyncMock(spec=_DevBrowserBridge)
        mock_bridge.send_command = AsyncMock(
            side_effect=DevBrowserBridgeError("navigate failed")
        )
        runner._bridge = mock_bridge

        result = await runner.browser_open("http://localhost:3000")
        assert result["navigated"] is True  # fallback


class TestBrowserRunScenarioDelegation:
    """Tests for browser_run_scenario with and without bridge."""

    @pytest.mark.asyncio
    async def test_with_bridge(self, tmp_path: Path) -> None:
        """browser_run_scenario delegates to bridge.

        Invariant: bridge result is translated to BrowserValidationResult.
        """
        runner = _make_runner(tmp_path)
        mock_bridge = AsyncMock(spec=_DevBrowserBridge)
        mock_bridge.send_command = AsyncMock(return_value={
            "passed": True,
            "console_errors": ["TypeError: x is not a function"],
            "details": "Scenario passed",
            "screenshot_path": "/tmp/screenshot.png",
        })
        runner._bridge = mock_bridge

        result = await runner.browser_run_scenario("Click login button")
        assert result.passed is True
        assert "TypeError" in result.console_errors[0]
        assert result.screenshot_path == "/tmp/screenshot.png"
        assert result.details == "Scenario passed"

    @pytest.mark.asyncio
    async def test_accumulates_console_errors(self, tmp_path: Path) -> None:
        """Console errors from scenarios are accumulated.

        Invariant: _console_errors grows with each scenario's errors.
        """
        runner = _make_runner(tmp_path)
        mock_bridge = AsyncMock(spec=_DevBrowserBridge)
        mock_bridge.send_command = AsyncMock(
            side_effect=[
                {"passed": True, "console_errors": ["err1"]},
                {"passed": True, "console_errors": ["err2", "err3"]},
            ]
        )
        runner._bridge = mock_bridge

        await runner.browser_run_scenario("Scenario 1")
        await runner.browser_run_scenario("Scenario 2")
        assert len(runner._console_errors) == 3

    @pytest.mark.asyncio
    async def test_bridge_error_returns_failed(self, tmp_path: Path) -> None:
        """Bridge error returns BrowserValidationResult(passed=False).

        Invariant: bridge errors produce failed results, not exceptions.
        """
        runner = _make_runner(tmp_path)
        mock_bridge = AsyncMock(spec=_DevBrowserBridge)
        mock_bridge.send_command = AsyncMock(
            side_effect=DevBrowserBridgeError("scenario failed")
        )
        runner._bridge = mock_bridge

        result = await runner.browser_run_scenario("Click login")
        assert result.passed is False
        assert "Bridge error" in result.details

    @pytest.mark.asyncio
    async def test_without_bridge(self, tmp_path: Path) -> None:
        """Without bridge, returns placeholder passed result."""
        runner = _make_runner(tmp_path)
        result = await runner.browser_run_scenario("Click login")
        assert result.passed is True
        assert "Click login" in result.details


class TestBrowserConsoleErrorsDelegation:
    """Tests for browser_console_errors with and without bridge."""

    @pytest.mark.asyncio
    async def test_with_bridge(self, tmp_path: Path) -> None:
        """Fetches errors from bridge when available.

        Invariant: returns bridge's error list and updates local cache.
        """
        runner = _make_runner(tmp_path)
        mock_bridge = AsyncMock(spec=_DevBrowserBridge)
        mock_bridge.send_command = AsyncMock(
            return_value={"errors": ["Error 1", "Error 2"]}
        )
        runner._bridge = mock_bridge

        errors = await runner.browser_console_errors()
        assert errors == ["Error 1", "Error 2"]
        assert runner._console_errors == ["Error 1", "Error 2"]

    @pytest.mark.asyncio
    async def test_without_bridge(self, tmp_path: Path) -> None:
        """Returns local cache when no bridge.

        Invariant: returns copy of _console_errors.
        """
        runner = _make_runner(tmp_path)
        runner._console_errors = ["cached error"]
        errors = await runner.browser_console_errors()
        assert errors == ["cached error"]

    @pytest.mark.asyncio
    async def test_bridge_error_returns_local(self, tmp_path: Path) -> None:
        """Falls back to local cache on bridge error."""
        runner = _make_runner(tmp_path)
        runner._console_errors = ["local"]
        mock_bridge = AsyncMock(spec=_DevBrowserBridge)
        mock_bridge.send_command = AsyncMock(
            side_effect=DevBrowserBridgeError("failed")
        )
        runner._bridge = mock_bridge

        errors = await runner.browser_console_errors()
        assert errors == ["local"]


class TestBrowserRecordArtifactDelegation:
    """Tests for browser_record_artifact with and without bridge."""

    @pytest.mark.asyncio
    async def test_with_bridge(self, tmp_path: Path) -> None:
        """Records via bridge when available.

        Invariant: bridge receives record command with output_path.
        """
        runner = _make_runner(tmp_path)
        mock_bridge = AsyncMock(spec=_DevBrowserBridge)
        mock_bridge.send_command = AsyncMock(return_value={
            "path": "/tmp/artifacts/validation/test.gif",
            "format": "gif",
        })
        runner._bridge = mock_bridge

        result = await runner.browser_record_artifact(name="test", format="gif")
        assert result["path"] == "/tmp/artifacts/validation/test.gif"
        assert result["format"] == "gif"
        assert result["name"] == "test"

        call_params = mock_bridge.send_command.call_args[1].get(
            "params",
            mock_bridge.send_command.call_args[0][1] if len(mock_bridge.send_command.call_args[0]) > 1 else {},
        )
        assert call_params["name"] == "test"
        assert call_params["format"] == "gif"

    @pytest.mark.asyncio
    async def test_without_bridge_creates_placeholder(self, tmp_path: Path) -> None:
        """Creates placeholder file when no bridge.

        Invariant: placeholder file exists at the expected path.
        """
        runner = _make_runner(tmp_path)
        result = await runner.browser_record_artifact(name="test_rec", format="gif")
        artifact = Path(result["path"])
        assert artifact.exists()
        assert "placeholder" in artifact.read_text()
        assert result["format"] == "gif"
        assert result["name"] == "test_rec"

    @pytest.mark.asyncio
    async def test_bridge_error_creates_placeholder(self, tmp_path: Path) -> None:
        """Bridge error falls back to placeholder.

        Invariant: DevBrowserBridgeError is caught, placeholder created.
        """
        runner = _make_runner(tmp_path)
        mock_bridge = AsyncMock(spec=_DevBrowserBridge)
        mock_bridge.send_command = AsyncMock(
            side_effect=DevBrowserBridgeError("recording failed")
        )
        runner._bridge = mock_bridge

        result = await runner.browser_record_artifact(name="fallback")
        artifact = Path(result["path"])
        assert artifact.exists()
        assert "placeholder" in artifact.read_text()


# ---------------------------------------------------------------------------
# Dev server readiness tests
# ---------------------------------------------------------------------------


class TestDevServerReadiness:
    """Tests for dev server start with readiness probing."""

    @pytest.mark.asyncio
    async def test_already_running_returns_immediately(self, tmp_path: Path) -> None:
        """Second start_dev_server call returns existing server info.

        Invariant: no new subprocess is created.
        """
        runner = _make_runner(tmp_path)
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)

        # Simulate an already-running server
        from build_your_room.browser_runner import DevServerHandle

        mock_proc = AsyncMock()
        mock_proc.pid = 999
        runner._dev_server = DevServerHandle(
            process=mock_proc,
            url="http://localhost:3000",
            log_path=tmp_path / "state" / "devserver",
        )

        result = await runner.start_dev_server()
        assert result["url"] == "http://localhost:3000"
        assert result["pid"] == 999
        assert result["ready"] is True

    @pytest.mark.asyncio
    async def test_wait_for_server_timeout(self, tmp_path: Path) -> None:
        """_wait_for_server returns False on timeout.

        Invariant: unreachable port returns False within timeout.
        """
        runner = _make_runner(tmp_path)
        # Use a port that's unlikely to be listening
        result = await runner._wait_for_server("localhost", 59999, timeout=0.5)
        assert result is False

    @pytest.mark.asyncio
    async def test_stop_dev_server_when_none(self) -> None:
        """Stopping when no server is running returns not-stopped."""
        runner = BrowserRunner(
            clone_path=Path("/tmp"), logs_dir=Path("/tmp"),
            artifacts_dir=Path("/tmp"), state_dir=Path("/tmp"),
        )
        result = await runner.stop_dev_server()
        assert result["stopped"] is False


# ---------------------------------------------------------------------------
# Cleanup tests
# ---------------------------------------------------------------------------


class TestCleanup:
    """Tests for BrowserRunner.cleanup()."""

    @pytest.mark.asyncio
    async def test_cleanup_closes_bridge_and_stops_server(self, tmp_path: Path) -> None:
        """cleanup() closes bridge and stops dev server.

        Invariant: both bridge and server are cleaned up.
        """
        runner = _make_runner(tmp_path)

        # Set up mock bridge
        mock_bridge = AsyncMock(spec=_DevBrowserBridge)
        mock_bridge.process = AsyncMock()
        mock_bridge.process.returncode = None
        mock_bridge.process.wait = AsyncMock(return_value=0)
        runner._bridge = mock_bridge

        # Set up mock server
        from build_your_room.browser_runner import DevServerHandle

        mock_proc = AsyncMock()
        mock_proc.pid = 999
        mock_proc.wait = AsyncMock(return_value=0)
        runner._dev_server = DevServerHandle(
            process=mock_proc,
            url="http://localhost:3000",
            log_path=tmp_path / "state" / "devserver",
        )

        await runner.cleanup()
        assert runner.has_bridge is False
        assert runner._dev_server is None

    @pytest.mark.asyncio
    async def test_cleanup_when_nothing_running(self, tmp_path: Path) -> None:
        """cleanup() is safe when nothing is running.

        Invariant: no-op cleanup does not raise.
        """
        runner = _make_runner(tmp_path)
        await runner.cleanup()  # should not raise


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


class TestForPipeline:
    """Tests for BrowserRunner.for_pipeline() factory."""

    def test_constructs_correct_paths(self, tmp_path: Path) -> None:
        """Factory constructs paths from pipeline layout conventions."""
        runner = BrowserRunner.for_pipeline(
            clone_path=tmp_path / "clone",
            pipelines_dir=tmp_path / "pipelines",
            pipeline_id=42,
        )
        assert runner.clone_path == tmp_path / "clone"
        assert runner.logs_dir == tmp_path / "pipelines" / "42" / "logs"
        assert runner.artifacts_dir == tmp_path / "pipelines" / "42" / "artifacts"
        assert runner.state_dir == tmp_path / "pipelines" / "42" / "state"

    def test_accepts_skill_path(self, tmp_path: Path) -> None:
        """Factory passes through devbrowser_skill_path."""
        skill_dir = tmp_path / "skill"
        runner = BrowserRunner.for_pipeline(
            clone_path=tmp_path / "clone",
            pipelines_dir=tmp_path / "pipelines",
            pipeline_id=1,
            devbrowser_skill_path=skill_dir,
        )
        assert runner.devbrowser_skill_path == skill_dir


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestPropertyBased:
    """Property-based tests for BrowserRunner invariants."""

    @given(
        name=st.from_regex(r"[a-z][a-z0-9_]{0,20}", fullmatch=True),
        fmt=st.sampled_from(["gif", "png", "webm", "mp4"]),
    )
    @settings(max_examples=30)
    def test_record_artifact_always_has_required_keys(
        self, name: str, fmt: str,
    ) -> None:
        """Property: recording result always contains path, format, name.

        Invariant: for all valid name/format inputs, the result dict
        contains 'path', 'format', and 'name' keys.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            runner = _make_runner(Path(td))
            result = asyncio.get_event_loop().run_until_complete(
                runner.browser_record_artifact(name=name, format=fmt)
            )
            assert "path" in result
            assert "format" in result
            assert "name" in result
            assert result["name"] == name
            assert result["format"] == fmt

    @given(
        has_dir=st.booleans(),
        has_entry=st.booleans(),
        entry=st.sampled_from(list(_BRIDGE_ENTRY_CANDIDATES)),
    )
    @settings(max_examples=20)
    def test_availability_matches_dir_and_entry(
        self, has_dir: bool, has_entry: bool, entry: str,
    ) -> None:
        """Property: is_available iff directory exists with known entry.

        Invariant: is_available(path) == (path.is_dir() and
        any(path/e exists for e in candidates)).
        """
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            if has_dir:
                skill_dir = Path(td) / "skill"
                skill_dir.mkdir()
                if has_entry:
                    (skill_dir / entry).write_text("// entry")
                result = BrowserRunner.is_available(skill_dir)
                assert result == (has_dir and has_entry)
            else:
                result = BrowserRunner.is_available(Path(td) / "nonexistent")
                assert result is False

    @given(scenario=st.text(min_size=1, max_size=100).filter(lambda t: "\r" not in t))
    @settings(max_examples=20)
    def test_run_scenario_without_bridge_always_passes(self, scenario: str) -> None:
        """Property: without bridge, run_scenario always returns passed=True.

        Invariant: fallback mode never reports failure.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            runner = _make_runner(Path(td))
            result = asyncio.get_event_loop().run_until_complete(
                runner.browser_run_scenario(scenario)
            )
            assert result.passed is True
            assert scenario in result.details

    @given(url=st.from_regex(r"https?://[a-z]+\.[a-z]{2,4}(:[0-9]{1,5})?", fullmatch=True))
    @settings(max_examples=20)
    def test_browser_open_without_bridge_always_returns_url(self, url: str) -> None:
        """Property: without bridge, browser_open echoes the URL back.

        Invariant: fallback always returns {"navigated": True, "url": <input>}.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            runner = _make_runner(Path(td))
            result = asyncio.get_event_loop().run_until_complete(
                runner.browser_open(url)
            )
            assert result["navigated"] is True
            assert result["url"] == url
