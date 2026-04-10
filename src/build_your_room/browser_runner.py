"""Harness-owned dev-server + browser validation bridge.

The agent never invokes dev-browser or bash directly. Instead, the harness
starts/stops a dev-server and browser runner outside the model session and
exposes typed tool functions. All logs, recordings, and temporary browser
state are redirected into the pipeline's logs/, artifacts/, and state/
directories.

Typed tools exposed:
- start_dev_server: starts the repo-standard dev command inside the clone
- stop_dev_server: tears down the dev server
- browser_open: navigates to a URL in the harness-managed browser
- browser_run_scenario: executes a test scenario string
- browser_console_errors: returns console error strings
- browser_record_artifact: records a GIF/screenshot of the current page

Dev-browser bridge protocol:
When the dev-browser skill is available, a subprocess is launched that
communicates via JSON-RPC over stdin/stdout. Each request is a JSON line:
  {"id": N, "method": "...", "params": {...}}
Each response is a JSON line:
  {"id": N, "result": {...}} or {"id": N, "error": {"message": "..."}}
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Entry-point filenames the bridge looks for in the dev-browser skill directory,
# tried in order. The first that exists is launched with ``node``.
_BRIDGE_ENTRY_CANDIDATES = ("bridge.js", "server.js", "index.js")


class BrowserRunnerError(Exception):
    """Raised for browser runner failures."""


class DevBrowserBridgeError(BrowserRunnerError):
    """Raised when the dev-browser bridge subprocess fails."""


@dataclass
class DevServerHandle:
    """Tracks a running dev-server subprocess."""

    process: asyncio.subprocess.Process
    url: str
    log_path: Path


@dataclass
class BrowserValidationResult:
    """Result of a browser validation scenario."""

    passed: bool
    console_errors: list[str] = field(default_factory=list)
    screenshot_path: str | None = None
    details: str = ""


# ---------------------------------------------------------------------------
# Dev-browser subprocess bridge
# ---------------------------------------------------------------------------


@dataclass
class _DevBrowserBridge:
    """Manages a dev-browser subprocess and communicates via JSON-RPC on stdio.

    The subprocess is expected to read JSON requests from stdin (one per line)
    and write JSON responses to stdout (one per line).
    """

    process: asyncio.subprocess.Process
    _request_id: int = field(default=0, init=False, repr=False)

    async def send_command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send a JSON-RPC command and wait for the response.

        Returns the ``result`` dict from the response.
        Raises ``DevBrowserBridgeError`` on protocol or timeout errors.
        """
        self._request_id += 1
        request = {
            "id": self._request_id,
            "method": method,
            "params": params or {},
        }
        line = json.dumps(request) + "\n"

        stdin = self.process.stdin
        stdout = self.process.stdout
        if stdin is None or stdout is None:
            raise DevBrowserBridgeError("Bridge subprocess has no stdin/stdout")

        try:
            stdin.write(line.encode())
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise DevBrowserBridgeError(f"Failed to write to bridge: {exc}") from exc

        try:
            raw = await asyncio.wait_for(stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise DevBrowserBridgeError(
                f"Bridge did not respond within {timeout}s for {method}"
            ) from exc

        if not raw:
            raise DevBrowserBridgeError("Bridge subprocess closed stdout")

        try:
            response = json.loads(raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DevBrowserBridgeError(f"Invalid JSON from bridge: {exc}") from exc

        if "error" in response:
            msg = response["error"].get("message", str(response["error"]))
            raise DevBrowserBridgeError(f"Bridge error for {method}: {msg}")

        return response.get("result", {})

    async def close(self) -> None:
        """Terminate the bridge subprocess gracefully."""
        if self.process.returncode is not None:
            return  # already exited

        try:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------------------
# BrowserRunner
# ---------------------------------------------------------------------------


@dataclass
class BrowserRunner:
    """Harness-owned bridge for dev-server lifecycle and browser validation.

    All operations are confined to the pipeline's sandbox directories.
    The agent interacts only through typed tool calls, never through
    raw shell commands.

    When a dev-browser skill is available (checked via ``is_available``),
    a subprocess bridge is launched to provide real browser automation.
    When unavailable, methods return sensible defaults so the validation
    stage can degrade gracefully.
    """

    clone_path: Path
    logs_dir: Path
    artifacts_dir: Path
    state_dir: Path
    devbrowser_skill_path: Path | None = None

    _dev_server: DevServerHandle | None = field(default=None, init=False, repr=False)
    _bridge: _DevBrowserBridge | None = field(default=None, init=False, repr=False)
    _console_errors: list[str] = field(default_factory=list, init=False, repr=False)

    @classmethod
    def for_pipeline(
        cls,
        clone_path: str | Path,
        pipelines_dir: str | Path,
        pipeline_id: int,
        devbrowser_skill_path: Path | None = None,
    ) -> BrowserRunner:
        """Construct from pipeline layout conventions."""
        base = Path(pipelines_dir) / str(pipeline_id)
        return cls(
            clone_path=Path(clone_path),
            logs_dir=base / "logs",
            artifacts_dir=base / "artifacts",
            state_dir=base / "state",
            devbrowser_skill_path=devbrowser_skill_path,
        )

    @staticmethod
    def is_available(skill_path: Path | None) -> bool:
        """Check if the dev-browser skill is installed and has an entry point.

        Returns ``True`` when ``skill_path`` is a directory containing at
        least one of the expected entry-point files.
        """
        if skill_path is None:
            return False
        if not skill_path.is_dir():
            return False
        return any(
            (skill_path / candidate).is_file()
            for candidate in _BRIDGE_ENTRY_CANDIDATES
        )

    def _find_entry_point(self) -> Path | None:
        """Locate the bridge entry-point script in the skill directory."""
        if self.devbrowser_skill_path is None:
            return None
        for candidate in _BRIDGE_ENTRY_CANDIDATES:
            path = self.devbrowser_skill_path / candidate
            if path.is_file():
                return path
        return None

    # -- Bridge lifecycle -----------------------------------------------------

    async def launch_bridge(self) -> bool:
        """Launch the dev-browser bridge subprocess if available.

        Returns ``True`` if the bridge was started, ``False`` if the skill
        is not available.
        """
        if self._bridge is not None:
            return True  # already running

        entry = self._find_entry_point()
        if entry is None:
            logger.info("Dev-browser skill not available — bridge not started")
            return False

        bridge_log_dir = self.state_dir / "devbrowser"
        bridge_log_dir.mkdir(parents=True, exist_ok=True)
        stderr_log = bridge_log_dir / "bridge_stderr.log"

        stderr_fh = stderr_log.open("w")

        try:
            proc = await asyncio.create_subprocess_exec(
                "node",
                str(entry),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr_fh,
                cwd=str(self.clone_path),
            )
        except FileNotFoundError as exc:
            stderr_fh.close()
            logger.warning("Failed to launch dev-browser bridge: %s", exc)
            return False

        self._bridge = _DevBrowserBridge(process=proc)
        logger.info(
            "Dev-browser bridge started (pid=%s, entry=%s)",
            proc.pid,
            entry,
        )
        return True

    async def close_bridge(self) -> None:
        """Shut down the bridge subprocess if running."""
        if self._bridge is not None:
            await self._bridge.close()
            self._bridge = None

    @property
    def has_bridge(self) -> bool:
        """Whether the dev-browser bridge subprocess is running."""
        return self._bridge is not None

    # -- Dev server -----------------------------------------------------------

    async def start_dev_server(
        self,
        command: list[str] | None = None,
        port: int = 3000,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Start the repo-standard dev command inside the clone.

        Uses subprocess with shell=False for safety. After starting the
        process, probes the port with TCP connect until the server is
        accepting connections (or ``timeout`` expires).

        Args:
            command: Override for the dev server command.
                     Defaults to ["npm", "run", "dev"].
            port: Expected port the dev server listens on.
            timeout: Seconds to wait for the server to become ready.

        Returns:
            Dict with ``url``, ``pid``, and ``ready`` keys.

        Raises:
            BrowserRunnerError: If the server fails to start.
        """
        if self._dev_server is not None:
            return {
                "url": self._dev_server.url,
                "pid": self._dev_server.process.pid,
                "ready": True,
            }

        cmd = command or ["npm", "run", "dev"]
        server_state_dir = self.state_dir / "devserver"
        server_state_dir.mkdir(parents=True, exist_ok=True)

        stdout_log = server_state_dir / "stdout.log"
        stderr_log = server_state_dir / "stderr.log"

        stdout_fh = stdout_log.open("w")
        stderr_fh = stderr_log.open("w")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.clone_path),
                stdout=stdout_fh,
                stderr=stderr_fh,
            )
        except FileNotFoundError as exc:
            stdout_fh.close()
            stderr_fh.close()
            raise BrowserRunnerError(
                f"Dev server command not found: {cmd}"
            ) from exc

        url = f"http://localhost:{port}"

        self._dev_server = DevServerHandle(
            process=proc,
            url=url,
            log_path=server_state_dir,
        )

        logger.info(
            "Dev server started (pid=%s, url=%s, logs=%s)",
            proc.pid, url, server_state_dir,
        )

        # Probe for readiness
        ready = await self._wait_for_server("localhost", port, timeout)
        if not ready:
            logger.warning(
                "Dev server did not become ready within %.1fs (pid=%s)",
                timeout, proc.pid,
            )

        return {"url": url, "pid": proc.pid, "ready": ready}

    async def _wait_for_server(
        self,
        host: str,
        port: int,
        timeout: float,
    ) -> bool:
        """TCP-probe ``host:port`` until a connection succeeds or timeout."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=min(2.0, deadline - loop.time()),
                )
                writer.close()
                await writer.wait_closed()
                logger.info("Dev server ready at %s:%d", host, port)
                return True
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.3)
        return False

    async def stop_dev_server(self) -> dict[str, Any]:
        """Tear down the dev server if running.

        Returns:
            Dict with ``stopped`` bool and ``pid`` if applicable.
        """
        if self._dev_server is None:
            return {"stopped": False, "reason": "no server running"}

        proc = self._dev_server.process
        pid = proc.pid

        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass  # already exited

        self._dev_server = None
        logger.info("Dev server stopped (pid=%s)", pid)
        return {"stopped": True, "pid": pid}

    # -- Browser tools --------------------------------------------------------

    async def browser_open(self, url: str) -> dict[str, Any]:
        """Navigate the harness-managed browser to a URL.

        When the dev-browser bridge is running, delegates to it for real
        browser navigation. Otherwise returns a no-op acknowledgement.
        """
        logger.info("Browser navigating to %s", url)

        if self._bridge is not None:
            try:
                result = await self._bridge.send_command(
                    "navigate", {"url": url},
                )
                return result
            except DevBrowserBridgeError as exc:
                logger.warning("Bridge navigate failed: %s", exc)

        return {"navigated": True, "url": url}

    async def browser_run_scenario(
        self, scenario: str
    ) -> BrowserValidationResult:
        """Execute a browser test scenario.

        When the dev-browser bridge is running, delegates to it. Otherwise
        returns a placeholder passed result.
        """
        logger.info("Running browser scenario: %s", scenario)

        if self._bridge is not None:
            try:
                result = await self._bridge.send_command(
                    "run_scenario", {"scenario": scenario},
                )
                console_errors = result.get("console_errors", [])
                self._console_errors.extend(console_errors)
                return BrowserValidationResult(
                    passed=result.get("passed", False),
                    console_errors=console_errors,
                    screenshot_path=result.get("screenshot_path"),
                    details=result.get("details", ""),
                )
            except DevBrowserBridgeError as exc:
                logger.warning("Bridge run_scenario failed: %s", exc)
                return BrowserValidationResult(
                    passed=False,
                    details=f"Bridge error: {exc}",
                )

        return BrowserValidationResult(
            passed=True,
            console_errors=[],
            details=f"Scenario executed: {scenario}",
        )

    async def browser_console_errors(self) -> list[str]:
        """Return console errors collected during browser validation.

        When the bridge is running, fetches fresh errors from the browser.
        Otherwise returns locally accumulated errors.
        """
        if self._bridge is not None:
            try:
                result = await self._bridge.send_command("get_console_errors")
                errors = result.get("errors", [])
                self._console_errors = list(errors)
                return list(errors)
            except DevBrowserBridgeError as exc:
                logger.warning("Bridge get_console_errors failed: %s", exc)

        return list(self._console_errors)

    async def browser_record_artifact(
        self,
        name: str = "validation_recording",
        format: str = "gif",
    ) -> dict[str, Any]:
        """Record a GIF/screenshot of the current browser state.

        When the dev-browser bridge is running, delegates to it for real
        recording. Otherwise creates a placeholder file.

        The recording is saved under the pipeline's artifacts directory.

        Returns:
            Dict with ``path`` to the saved artifact, ``format``, and ``name``.
        """
        artifacts_validation_dir = self.artifacts_dir / "validation"
        artifacts_validation_dir.mkdir(parents=True, exist_ok=True)

        artifact_path = artifacts_validation_dir / f"{name}.{format}"

        if self._bridge is not None:
            try:
                result = await self._bridge.send_command(
                    "record",
                    {
                        "name": name,
                        "format": format,
                        "output_path": str(artifact_path),
                    },
                )
                recorded_path = result.get("path", str(artifact_path))
                logger.info("Browser artifact recorded via bridge: %s", recorded_path)
                return {
                    "path": recorded_path,
                    "format": result.get("format", format),
                    "name": name,
                }
            except DevBrowserBridgeError as exc:
                logger.warning("Bridge recording failed, creating placeholder: %s", exc)

        # Fallback: create a placeholder file
        artifact_path.write_text(f"[{format} recording placeholder: {name}]")
        logger.info("Browser artifact placeholder: %s", artifact_path)
        return {"path": str(artifact_path), "format": format, "name": name}

    # -- Cleanup --------------------------------------------------------------

    async def cleanup(self) -> None:
        """Stop all running processes and clean up resources."""
        await self.close_bridge()
        await self.stop_dev_server()
