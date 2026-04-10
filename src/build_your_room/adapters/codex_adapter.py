"""CodexAppServerAdapter — wraps the Codex app-server stdio JSON-RPC protocol.

Provides workspace-write sandboxing via writableRoots, persistent thread
management, structured output via outputSchema, and review mode for code
review stages.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from build_your_room.adapters.base import SessionConfig
from build_your_room.streaming import LogBuffer
from build_your_room.tool_profiles import get_codex_sandbox_config

logger = logging.getLogger(__name__)

# Client identity sent in the initialize handshake.
_CLIENT_INFO = {
    "name": "build_your_room",
    "title": "Build Your Room",
    "version": "0.1.0",
}

# JSON-RPC error codes
_PARSE_ERROR = -32700
_INTERNAL_ERROR = -32603


class CodexProtocolError(Exception):
    """Raised when the Codex app-server returns an unexpected response."""


# ---------------------------------------------------------------------------
# JSON-RPC transport helpers
# ---------------------------------------------------------------------------


async def _send_message(
    proc: asyncio.subprocess.Process,
    message: dict[str, Any],
) -> None:
    """Write a JSON-RPC message to the subprocess stdin."""
    assert proc.stdin is not None
    line = json.dumps(message) + "\n"
    proc.stdin.write(line.encode())
    await proc.stdin.drain()


async def _read_message(
    proc: asyncio.subprocess.Process,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Read one JSON-RPC message from the subprocess stdout.

    Skips blank lines.  Returns the first valid JSON message.
    """
    assert proc.stdout is not None
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError("Timed out reading from codex app-server")
        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        if not raw:
            raise CodexProtocolError("Codex app-server closed stdout unexpectedly")
        line = raw.decode().strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("Non-JSON line from codex: %s", line)
            raise CodexProtocolError(f"Invalid JSON from codex: {line!r}") from exc
        return msg


async def _send_and_wait(
    proc: asyncio.subprocess.Process,
    message: dict[str, Any],
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Send a JSON-RPC request and wait for the matching response by id."""
    await _send_message(proc, message)
    request_id = message.get("id")
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError(
                f"Timed out waiting for response to request {request_id}"
            )
        resp = await _read_message(proc, timeout=remaining)
        if "error" in resp and resp.get("id") == request_id:
            err = resp["error"]
            raise CodexProtocolError(
                f"Codex RPC error {err.get('code', '?')}: {err.get('message', '?')}"
            )
        if resp.get("id") == request_id:
            return resp
        # Notification or mismatched id — keep reading


# ---------------------------------------------------------------------------
# CodexTurnResult — concrete SessionResult
# ---------------------------------------------------------------------------


@dataclass
class CodexTurnResult:
    """Concrete result from a Codex app-server turn."""

    output: str
    structured_output: dict[str, Any] | None
    thread_id: str | None = None
    is_error: bool = False


# ---------------------------------------------------------------------------
# CodexLiveSession — live session handle with multi-turn support
# ---------------------------------------------------------------------------


class CodexLiveSession:
    """Live Codex app-server session handle.

    Wraps a persistent subprocess communicating via stdio JSON-RPC.
    Exposes the :class:`LiveSession` protocol: multi-turn ``send_turn``,
    context usage estimation, session snapshot, and cleanup.
    """

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        thread_id: str,
        config: SessionConfig,
        log_buffer: LogBuffer | None = None,
    ) -> None:
        self._proc = proc
        self._thread_id = thread_id
        self._config = config
        self._log_buffer = log_buffer
        self._next_id: int = 100
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._total_cost_usd: float = 0.0
        self._turn_count: int = 0

    @property
    def session_id(self) -> str | None:
        return self._thread_id

    def _next_request_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    async def send_turn(
        self,
        prompt: str,
        output_schema: dict[str, Any] | None = None,
    ) -> CodexTurnResult:
        """Send a turn on the existing thread and collect the result.

        Uses ``turn/start`` to submit a prompt to the persistent thread.
        If *output_schema* is provided, it is sent as ``outputSchema`` to
        request structured JSON output.
        """
        params: dict[str, Any] = {
            "threadId": self._thread_id,
            "prompt": prompt,
        }
        if output_schema is not None:
            params["outputSchema"] = output_schema

        resp = await _send_and_wait(
            self._proc,
            {
                "method": "turn/start",
                "id": self._next_request_id(),
                "params": params,
            },
        )

        result_data = resp.get("result", {})
        self._turn_count += 1

        # Extract token usage if available
        usage = result_data.get("usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        self._total_input_tokens += input_tokens
        self._total_output_tokens += output_tokens
        cost = result_data.get("cost_usd", 0.0)
        if cost:
            self._total_cost_usd += cost

        # Extract text output
        text = result_data.get("message", "")
        structured = result_data.get("structured_output")

        self._log_msg(
            f"Turn {self._turn_count} complete: "
            f"in={input_tokens} out={output_tokens} tokens"
        )

        return CodexTurnResult(
            output=text,
            structured_output=structured,
            thread_id=self._thread_id,
            is_error=result_data.get("is_error", False),
        )

    async def get_context_usage(self) -> dict[str, Any] | None:
        """Estimate context usage from accumulated token counts.

        Codex app-server doesn't expose a direct ``get_context_usage`` API.
        We estimate from accumulated token counts using the model's context
        window size (default 128k for codex models).
        """
        max_tokens = self._config.extra.get("max_context_tokens", 128_000)
        total = self._total_input_tokens + self._total_output_tokens
        if max_tokens <= 0:
            return None
        pct = (total / max_tokens) * 100.0
        return {
            "total_tokens": total,
            "max_tokens": max_tokens,
            "percentage": round(pct, 2),
        }

    async def snapshot(self) -> dict[str, Any]:
        """Return a resumable state dict for ``agent_sessions.resume_state_json``."""
        return {
            "session_id": self._thread_id,
            "model": self._config.model,
            "clone_path": self._config.clone_path,
            "total_cost_usd": self._total_cost_usd,
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "turn_count": self._turn_count,
            "pipeline_id": self._config.pipeline_id,
            "stage_id": self._config.stage_id,
        }

    async def close(self) -> None:
        """Terminate the app-server subprocess."""
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        except ProcessLookupError:
            pass
        except Exception:
            logger.debug("Error closing codex app-server process", exc_info=True)

    def _log_msg(self, message: str) -> None:
        """Append to the pipeline log buffer if available."""
        if self._log_buffer and self._config.pipeline_id is not None:
            self._log_buffer.append(self._config.pipeline_id, f"[codex] {message}")


# ---------------------------------------------------------------------------
# CodexAppServerAdapter — adapter factory
# ---------------------------------------------------------------------------


class CodexAppServerAdapter:
    """Opens Codex app-server sessions confined to a pipeline workspace.

    Registers as the ``'codex'`` adapter in the orchestrator's adapter dict.
    Each call to :meth:`start_session` spawns a new ``codex app-server``
    subprocess, performs the JSON-RPC handshake, and starts a persistent
    thread with workspace-write sandboxing.
    """

    def __init__(
        self,
        log_buffer: LogBuffer | None = None,
        codex_binary: str = "codex",
    ) -> None:
        self._log_buffer = log_buffer
        self._codex_binary = codex_binary

    async def start_session(self, config: SessionConfig) -> CodexLiveSession:
        """Spawn a codex app-server process and initialize a thread."""
        proc = await asyncio.create_subprocess_exec(
            self._codex_binary,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=config.clone_path,
        )

        try:
            # Initialize handshake
            await _send_and_wait(
                proc,
                {
                    "method": "initialize",
                    "id": 0,
                    "params": {"clientInfo": _CLIENT_INFO},
                },
            )
            # Send initialized notification (no id — it's a notification)
            await _send_message(proc, {"method": "initialized", "params": {}})

            # Build sandbox config from allowed roots
            sandbox_cfg = get_codex_sandbox_config(config.allowed_roots)

            # Start thread
            resp = await _send_and_wait(
                proc,
                {
                    "method": "thread/start",
                    "id": 1,
                    "params": {
                        "model": config.model,
                        "cwd": config.clone_path,
                        "approvalPolicy": "never",
                        "sandbox": {
                            "mode": sandbox_cfg.mode,
                            "writableRoots": list(sandbox_cfg.writable_roots),
                        },
                    },
                },
            )

            thread_id = resp["result"]["thread"]["id"]
        except Exception:
            # Clean up the subprocess on handshake failure
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
            raise

        session = CodexLiveSession(proc, thread_id, config, self._log_buffer)
        logger.info(
            "Codex session started for pipeline=%s stage=%s model=%s thread=%s",
            config.pipeline_id,
            config.stage_id,
            config.model,
            thread_id,
        )
        return session
