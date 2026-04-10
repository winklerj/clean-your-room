"""ClaudeAgentAdapter — wraps claude-agent-sdk for live multi-turn sessions.

Provides workspace confinement via make_path_guard, explicit tool profiles
from the stage configuration, and context-usage monitoring between turns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from build_your_room.adapters.base import SessionConfig
from build_your_room.sandbox import DENIED_TOOLS, make_path_guard
from build_your_room.streaming import LogBuffer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Permission bridge: adapt sync path guard → async SDK CanUseTool callback
# ---------------------------------------------------------------------------


def _make_sdk_permission_callback(
    allowed_roots: list[str],
) -> Any:
    """Build an async ``can_use_tool`` callback for the Claude Agent SDK.

    The SDK expects ``async (tool_name, input_data, context) -> PermissionResult``.
    We delegate the actual path-containment logic to :func:`make_path_guard` and
    translate its ``bool`` result into the SDK's permission types.
    """
    # Late import to avoid hard dependency at module level — tests can mock
    # the SDK package.
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    guard = make_path_guard([Path(r) for r in allowed_roots])

    async def _callback(
        tool_name: str,
        input_data: dict[str, Any],
        _context: Any,
    ) -> Any:
        allowed = guard(tool_name, input_data)
        if allowed:
            return PermissionResultAllow(updated_input=input_data)
        reason = "blocked by workspace sandbox"
        if tool_name in DENIED_TOOLS:
            reason = f"tool {tool_name!r} is denied by policy"
        return PermissionResultDeny(message=reason)

    return _callback


# ---------------------------------------------------------------------------
# ClaudeTurnResult — concrete implementation of SessionResult protocol
# ---------------------------------------------------------------------------


@dataclass
class ClaudeTurnResult:
    """Concrete result from a Claude session turn."""

    output: str
    structured_output: dict[str, Any] | None
    session_id: str | None = None
    total_cost_usd: float | None = None
    num_turns: int = 0
    is_error: bool = False


# ---------------------------------------------------------------------------
# ClaudeLiveSession — live session handle with multi-turn support
# ---------------------------------------------------------------------------


class ClaudeLiveSession:
    """Live Claude Agent SDK session handle.

    Wraps a :class:`ClaudeSDKClient` to expose the :class:`LiveSession`
    protocol: multi-turn ``send_turn``, context usage queries, session
    snapshot for rotation, and cleanup.
    """

    def __init__(
        self,
        client: ClaudeSDKClient,
        config: SessionConfig,
        log_buffer: LogBuffer | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._log_buffer = log_buffer
        self._session_id: str | None = None
        self._total_cost_usd: float = 0.0
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def send_turn(
        self,
        prompt: str,
        output_schema: dict[str, Any] | None = None,
    ) -> ClaudeTurnResult:
        """Send a turn on the existing session and collect the result.

        If *output_schema* is provided, the SDK's ``output_format`` is set
        to request structured JSON output for this turn.  Because the SDK
        client's options are set at construction time, structured output is
        configured via ``ClaudeAgentOptions.output_format`` at session start.
        Per-turn schema changes are handled by starting a new query.
        """
        await self._client.query(prompt)
        return await self._collect_response()

    async def get_context_usage(self) -> dict[str, Any] | None:
        """Return the SDK's context-usage response as a plain dict.

        The returned dict includes ``totalTokens``, ``maxTokens``, and
        ``percentage`` — matching what :meth:`ContextMonitor.parse_claude_usage`
        expects (after field-name normalisation).
        """
        try:
            usage = await self._client.get_context_usage()  # type: ignore[attr-defined]
            if usage is None:
                return None
            # Normalise to the keys ContextMonitor.parse_claude_usage expects
            return {
                "total_tokens": usage.get("totalTokens", 0),
                "max_tokens": usage.get("maxTokens", 0),
                "percentage": usage.get("percentage", 0.0),
                "categories": usage.get("categories", []),
            }
        except Exception:
            logger.debug("get_context_usage failed", exc_info=True)
            return None

    async def snapshot(self) -> dict[str, Any]:
        """Return a resumable state dict for ``agent_sessions.resume_state_json``."""
        return {
            "session_id": self._session_id,
            "model": self._config.model,
            "clone_path": self._config.clone_path,
            "total_cost_usd": self._total_cost_usd,
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "pipeline_id": self._config.pipeline_id,
            "stage_id": self._config.stage_id,
        }

    async def close(self) -> None:
        """Disconnect the underlying SDK client."""
        try:
            await self._client.disconnect()
        except Exception:
            logger.debug("Error disconnecting Claude SDK client", exc_info=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _collect_response(self) -> ClaudeTurnResult:
        """Drain ``receive_response()`` and build a :class:`ClaudeTurnResult`."""
        text_parts: list[str] = []
        result = ClaudeTurnResult(output="", structured_output=None)

        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
            elif isinstance(msg, ResultMessage):
                result.session_id = msg.session_id
                result.total_cost_usd = msg.total_cost_usd
                result.num_turns = msg.num_turns
                result.is_error = msg.is_error
                result.structured_output = msg.structured_output
                if msg.result:
                    text_parts.append(msg.result)
                self._session_id = msg.session_id
                # Accumulate token usage
                if msg.usage:
                    self._total_input_tokens += msg.usage.get("input_tokens", 0)
                    self._total_output_tokens += msg.usage.get("output_tokens", 0)
                if msg.total_cost_usd:
                    self._total_cost_usd += msg.total_cost_usd
                self._log_msg(
                    f"Turn complete: {msg.num_turns} turns, "
                    f"cost=${msg.total_cost_usd or 0:.4f}"
                )

        result.output = "\n".join(text_parts) if text_parts else ""
        return result

    def _log_msg(self, message: str) -> None:
        """Append to the pipeline log buffer if available."""
        if self._log_buffer and self._config.pipeline_id is not None:
            self._log_buffer.append(self._config.pipeline_id, f"[claude] {message}")


# ---------------------------------------------------------------------------
# ClaudeAgentAdapter — adapter factory
# ---------------------------------------------------------------------------


class ClaudeAgentAdapter:
    """Opens Claude Agent SDK sessions confined to a pipeline workspace.

    Registers as the ``'claude'`` adapter in the orchestrator's adapter dict.
    Each call to :meth:`start_session` creates a new :class:`ClaudeSDKClient`
    with the tool profile, path guard, and model from the :class:`SessionConfig`.
    """

    def __init__(self, log_buffer: LogBuffer | None = None) -> None:
        self._log_buffer = log_buffer

    async def start_session(self, config: SessionConfig) -> ClaudeLiveSession:
        """Create and connect a new Claude live session."""
        options = ClaudeAgentOptions(
            model=config.model,
            cwd=config.clone_path,
            system_prompt=config.system_prompt,
            permission_mode="acceptEdits",
            allowed_tools=config.allowed_tools,
            disallowed_tools=list(DENIED_TOOLS),
            max_turns=config.max_turns,
            setting_sources=["project"],
            can_use_tool=_make_sdk_permission_callback(config.allowed_roots),
            output_format=config.output_format,
            resume=config.resume_session_id,
        )

        client = ClaudeSDKClient(options=options)
        await client.connect()

        session = ClaudeLiveSession(client, config, self._log_buffer)
        logger.info(
            "Claude session started for pipeline=%s stage=%s model=%s",
            config.pipeline_id,
            config.stage_id,
            config.model,
        )
        return session
