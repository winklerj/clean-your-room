"""Base types and protocols for agent adapters.

Defines the SessionConfig dataclass (adapter-agnostic session configuration)
and the Protocol classes that concrete adapters must satisfy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class SessionConfig:
    """Adapter-agnostic configuration for starting an agent session.

    Built by the orchestrator from pipeline state, stage graph node config,
    tool profile, and workspace sandbox.
    """

    model: str
    clone_path: str
    system_prompt: str
    allowed_tools: list[str]
    allowed_roots: list[str]
    max_turns: int | None = None
    context_threshold_pct: float = 60.0
    output_format: dict[str, Any] | None = None
    resume_session_id: str | None = None
    pipeline_id: int | None = None
    stage_id: int | None = None
    session_db_id: int | None = None
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def clone_path_obj(self) -> Path:
        return Path(self.clone_path)


class SessionResult(Protocol):
    """Result from an agent session turn."""

    output: str
    structured_output: dict[str, Any] | None


class LiveSession(Protocol):
    """A live agent session handle for multi-turn interaction."""

    session_id: str | None

    async def send_turn(
        self, prompt: str, output_schema: dict[str, Any] | None = None
    ) -> SessionResult: ...

    async def get_context_usage(self) -> dict[str, Any] | None: ...

    async def snapshot(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class AgentAdapter(Protocol):
    """Opens live sessions in a pipeline sandbox."""

    async def start_session(self, config: SessionConfig) -> LiveSession: ...
