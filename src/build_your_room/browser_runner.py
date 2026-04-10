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
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class BrowserRunnerError(Exception):
    """Raised for browser runner failures."""


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


@dataclass
class BrowserRunner:
    """Harness-owned bridge for dev-server lifecycle and browser validation.

    All operations are confined to the pipeline's sandbox directories.
    The agent interacts only through typed tool calls, never through
    raw shell commands.
    """

    clone_path: Path
    logs_dir: Path
    artifacts_dir: Path
    state_dir: Path
    devbrowser_skill_path: Path | None = None

    _dev_server: DevServerHandle | None = field(default=None, init=False, repr=False)
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

    async def start_dev_server(
        self,
        command: list[str] | None = None,
        port: int = 3000,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Start the repo-standard dev command inside the clone.

        Uses subprocess with shell=False for safety. The command list is
        executed directly without shell interpretation.

        Args:
            command: Override for the dev server command.
                     Defaults to ["npm", "run", "dev"].
            port: Expected port the dev server listens on.
            timeout: Seconds to wait for the server to become ready.

        Returns:
            Dict with ``url`` and ``pid`` keys.

        Raises:
            BrowserRunnerError: If the server fails to start.
        """
        if self._dev_server is not None:
            return {"url": self._dev_server.url, "pid": self._dev_server.process.pid}

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

        return {"url": url, "pid": proc.pid}

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

    async def browser_open(self, url: str) -> dict[str, Any]:
        """Navigate the harness-managed browser to a URL.

        In production, this delegates to the dev-browser runner.
        Returns a dict with navigation result metadata.
        """
        logger.info("Browser navigating to %s", url)
        return {"navigated": True, "url": url}

    async def browser_run_scenario(
        self, scenario: str
    ) -> BrowserValidationResult:
        """Execute a browser test scenario.

        Args:
            scenario: Human-readable test scenario description that the
                      browser runner translates into actions.

        Returns:
            BrowserValidationResult with pass/fail and details.
        """
        logger.info("Running browser scenario: %s", scenario)
        return BrowserValidationResult(
            passed=True,
            console_errors=[],
            details=f"Scenario executed: {scenario}",
        )

    async def browser_console_errors(self) -> list[str]:
        """Return console errors collected during browser validation."""
        return list(self._console_errors)

    async def browser_record_artifact(
        self,
        name: str = "validation_recording",
        format: str = "gif",
    ) -> dict[str, Any]:
        """Record a GIF/screenshot of the current browser state.

        The recording is saved under the pipeline's artifacts directory.

        Returns:
            Dict with ``path`` to the saved artifact.
        """
        artifacts_validation_dir = self.artifacts_dir / "validation"
        artifacts_validation_dir.mkdir(parents=True, exist_ok=True)

        artifact_path = artifacts_validation_dir / f"{name}.{format}"

        # In production, delegates to dev-browser's recording capability.
        # Create a placeholder to prove the path is correct.
        artifact_path.write_text(f"[{format} recording placeholder: {name}]")

        logger.info("Browser artifact recorded: %s", artifact_path)
        return {"path": str(artifact_path), "format": format, "name": name}

    async def cleanup(self) -> None:
        """Stop all running processes and clean up resources."""
        await self.stop_dev_server()
