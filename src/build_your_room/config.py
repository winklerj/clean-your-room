"""Configuration for build-your-room.

Environment variables, derived paths, and the typed PipelineConfig
dataclass for per-pipeline runtime overrides (stored as config_json).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Environment variables — global defaults
# ---------------------------------------------------------------------------

BUILD_YOUR_ROOM_DIR = Path(
    os.getenv("BUILD_YOUR_ROOM_DIR", str(Path.home() / ".build-your-room"))
)
CLONES_DIR = BUILD_YOUR_ROOM_DIR / "clones"
PIPELINES_DIR = BUILD_YOUR_ROOM_DIR / "pipelines"

DATABASE_URL = os.getenv("DATABASE_URL", "postgres:///build_your_room")

DEFAULT_PORT = int(os.getenv("BUILD_YOUR_ROOM_PORT", "8317"))

DEFAULT_CLAUDE_MODEL = os.getenv("DEFAULT_CLAUDE_MODEL", "claude-sonnet-4-6")
DEFAULT_CODEX_MODEL = os.getenv("DEFAULT_CODEX_MODEL", "gpt-5.1-codex")
SPEC_CLAUDE_MODEL = os.getenv("SPEC_CLAUDE_MODEL", "claude-opus-4-6")

CONTEXT_THRESHOLD_PCT = int(os.getenv("CONTEXT_THRESHOLD_PCT", "60"))
MAX_CONCURRENT_PIPELINES = int(os.getenv("MAX_CONCURRENT_PIPELINES", "10"))
PIPELINE_LEASE_TTL_SEC = int(os.getenv("PIPELINE_LEASE_TTL_SEC", "30"))
PIPELINE_HEARTBEAT_INTERVAL_SEC = int(
    os.getenv("PIPELINE_HEARTBEAT_INTERVAL_SEC", "10")
)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

DEVBROWSER_SKILL_PATH = Path(
    os.getenv(
        "DEVBROWSER_SKILL_PATH",
        str(Path.home() / ".claude" / "skills" / "dev-browser"),
    )
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

PropertyTestFramework = Literal[
    "auto", "hypothesis", "fast-check", "bombadil", "none"
]
RemotePublishPolicy = Literal["manual", "auto", "disabled"]
RotationPolicy = Literal["resume_current_claim", "release_and_reclaim"]


class ConfigError(ValueError):
    """Raised when a configuration value fails validation."""


def _validate_pct(value: int, name: str) -> int:
    if not 0 <= value <= 100:
        raise ConfigError(f"{name} must be between 0 and 100, got {value}")
    return value


def _validate_positive(value: int, name: str) -> int:
    if value < 1:
        raise ConfigError(f"{name} must be >= 1, got {value}")
    return value


# ---------------------------------------------------------------------------
# PipelineConfig — typed runtime config per pipeline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineConfig:
    """Typed representation of a pipeline's config_json.

    Provides defaults matching the spec so callers can construct with
    no arguments and get a valid config.  Use ``from_json`` to parse
    a stored config_json string, merging supplied keys over defaults.
    """

    claude_model: str = DEFAULT_CLAUDE_MODEL
    codex_model: str = DEFAULT_CODEX_MODEL
    context_threshold_pct: int = CONTEXT_THRESHOLD_PCT
    disable_1m_context: bool = True
    max_concurrent_stages: int = 1
    impl_task_rotation_policy: RotationPolicy = "resume_current_claim"
    lease_ttl_sec: int = PIPELINE_LEASE_TTL_SEC
    checkpoint_commits: bool = True
    snapshot_dirty_workspace_on_cancel_or_kill: bool = True
    remote_publish: RemotePublishPolicy = "manual"
    devbrowser_enabled: bool = True
    property_test_framework: PropertyTestFramework = "auto"

    def __post_init__(self) -> None:
        _validate_pct(
            self.context_threshold_pct, "context_threshold_pct"
        )
        _validate_positive(
            self.max_concurrent_stages, "max_concurrent_stages"
        )
        _validate_positive(self.lease_ttl_sec, "lease_ttl_sec")

    @classmethod
    def from_json(cls, raw: str | dict[str, Any] | None) -> PipelineConfig:
        """Parse a config_json string or dict, merging over defaults.

        Unknown keys are silently ignored so forward-compatible fields
        in stored JSON don't break older code.
        """
        if raw is None or raw == "":
            return cls()
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    def to_json(self) -> str:
        """Serialize to a JSON string suitable for config_json storage."""
        data: dict[str, Any] = {}
        for f in fields(self):
            data[f.name] = getattr(self, f.name)
        return json.dumps(data)

    def merge(self, overrides: dict[str, Any]) -> PipelineConfig:
        """Return a new PipelineConfig with the given overrides applied."""
        data: dict[str, Any] = {}
        known = {f.name for f in fields(type(self))}
        for f in fields(type(self)):
            data[f.name] = getattr(self, f.name)
        for k, v in overrides.items():
            if k in known:
                data[k] = v
        return PipelineConfig(**data)
