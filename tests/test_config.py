"""Tests for config.py — environment variables, PipelineConfig, and validation.

Uses property-based tests for PipelineConfig round-trip and merge invariants,
and unit tests for parsing edge cases, validation boundaries, and env var loading.
"""

from __future__ import annotations

import json
from dataclasses import fields

import pytest
from hypothesis import given
from hypothesis import strategies as st

from build_your_room.config import (
    BUILD_YOUR_ROOM_DIR,
    CLONES_DIR,
    PIPELINES_DIR,
    DATABASE_URL,
    DEFAULT_PORT,
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_CODEX_MODEL,
    SPEC_CLAUDE_MODEL,
    CONTEXT_THRESHOLD_PCT,
    MAX_CONCURRENT_PIPELINES,
    PIPELINE_LEASE_TTL_SEC,
    PIPELINE_HEARTBEAT_INTERVAL_SEC,
    ANTHROPIC_API_KEY,
    OPENAI_API_KEY,
    DEVBROWSER_SKILL_PATH,
    LOG_LEVEL,
    ConfigError,
    PipelineConfig,
    _validate_pct,
    _validate_positive,
)


# ---------------------------------------------------------------------------
# Strategies for property-based tests
# ---------------------------------------------------------------------------

_pct = st.integers(min_value=0, max_value=100)
_positive_int = st.integers(min_value=1, max_value=100_000)
_model_str = st.sampled_from([
    "claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5-20251001",
])
_codex_model_str = st.sampled_from(["gpt-5.1-codex", "gpt-5.1-mini"])
_rotation_policy = st.sampled_from(["resume_current_claim", "release_and_reclaim"])
_remote_publish = st.sampled_from(["manual", "auto", "disabled"])
_test_framework = st.sampled_from([
    "auto", "hypothesis", "fast-check", "bombadil", "none",
])


@st.composite
def pipeline_configs(draw: st.DrawFn) -> PipelineConfig:
    """Generate valid PipelineConfig instances."""
    return PipelineConfig(
        claude_model=draw(_model_str),
        codex_model=draw(_codex_model_str),
        context_threshold_pct=draw(_pct),
        disable_1m_context=draw(st.booleans()),
        max_concurrent_stages=draw(_positive_int),
        impl_task_rotation_policy=draw(_rotation_policy),
        lease_ttl_sec=draw(_positive_int),
        checkpoint_commits=draw(st.booleans()),
        snapshot_dirty_workspace_on_cancel_or_kill=draw(st.booleans()),
        remote_publish=draw(_remote_publish),
        devbrowser_enabled=draw(st.booleans()),
        property_test_framework=draw(_test_framework),
    )


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestPipelineConfigProperties:
    """Property-based tests for PipelineConfig invariants."""

    @given(cfg=pipeline_configs())
    def test_roundtrip_json(self, cfg: PipelineConfig) -> None:
        """to_json -> from_json always reconstructs the same config."""
        restored = PipelineConfig.from_json(cfg.to_json())
        assert restored == cfg

    @given(cfg=pipeline_configs())
    def test_to_json_is_valid_json(self, cfg: PipelineConfig) -> None:
        """to_json always produces parseable JSON."""
        data = json.loads(cfg.to_json())
        assert isinstance(data, dict)
        assert set(data.keys()) == {f.name for f in fields(PipelineConfig)}

    @given(cfg=pipeline_configs())
    def test_merge_empty_is_identity(self, cfg: PipelineConfig) -> None:
        """Merging an empty dict returns an equal config."""
        assert cfg.merge({}) == cfg

    @given(cfg=pipeline_configs(), pct=_pct)
    def test_merge_single_override(self, cfg: PipelineConfig, pct: int) -> None:
        """Merging a single key only changes that key."""
        merged = cfg.merge({"context_threshold_pct": pct})
        assert merged.context_threshold_pct == pct
        assert merged.claude_model == cfg.claude_model
        assert merged.lease_ttl_sec == cfg.lease_ttl_sec

    @given(cfg=pipeline_configs())
    def test_merge_unknown_keys_ignored(self, cfg: PipelineConfig) -> None:
        """Unknown keys in overrides are silently dropped."""
        merged = cfg.merge({"nonexistent_key_xyz": 42})
        assert merged == cfg

    @given(pct=_pct)
    def test_validate_pct_accepts_valid(self, pct: int) -> None:
        """Percentages 0–100 pass validation."""
        assert _validate_pct(pct, "test") == pct

    @given(val=_positive_int)
    def test_validate_positive_accepts_valid(self, val: int) -> None:
        """Positive integers pass validation."""
        assert _validate_positive(val, "test") == val


# ---------------------------------------------------------------------------
# Unit tests — PipelineConfig
# ---------------------------------------------------------------------------


class TestPipelineConfigUnit:
    """Unit tests for PipelineConfig construction, parsing, and validation."""

    def test_defaults(self) -> None:
        """Default construction produces spec-matching values."""
        cfg = PipelineConfig()
        assert cfg.context_threshold_pct == CONTEXT_THRESHOLD_PCT
        assert cfg.disable_1m_context is True
        assert cfg.max_concurrent_stages == 1
        assert cfg.impl_task_rotation_policy == "resume_current_claim"
        assert cfg.checkpoint_commits is True
        assert cfg.snapshot_dirty_workspace_on_cancel_or_kill is True
        assert cfg.remote_publish == "manual"
        assert cfg.devbrowser_enabled is True
        assert cfg.property_test_framework == "auto"

    def test_from_json_none(self) -> None:
        """from_json(None) returns defaults."""
        assert PipelineConfig.from_json(None) == PipelineConfig()

    def test_from_json_empty_string(self) -> None:
        """from_json('') returns defaults."""
        assert PipelineConfig.from_json("") == PipelineConfig()

    def test_from_json_empty_dict(self) -> None:
        """from_json({}) returns defaults."""
        assert PipelineConfig.from_json({}) == PipelineConfig()

    def test_from_json_partial(self) -> None:
        """from_json with partial keys fills missing fields with defaults."""
        cfg = PipelineConfig.from_json({"claude_model": "claude-opus-4-6"})
        assert cfg.claude_model == "claude-opus-4-6"
        assert cfg.codex_model == DEFAULT_CODEX_MODEL

    def test_from_json_string(self) -> None:
        """from_json parses a JSON string."""
        raw = json.dumps({"lease_ttl_sec": 45, "devbrowser_enabled": False})
        cfg = PipelineConfig.from_json(raw)
        assert cfg.lease_ttl_sec == 45
        assert cfg.devbrowser_enabled is False

    def test_from_json_ignores_unknown_keys(self) -> None:
        """Unknown keys in JSON don't cause errors."""
        raw = {"claude_model": "claude-opus-4-6", "future_flag": True}
        cfg = PipelineConfig.from_json(raw)
        assert cfg.claude_model == "claude-opus-4-6"
        assert not hasattr(cfg, "future_flag")

    def test_from_json_dict(self) -> None:
        """from_json accepts a plain dict."""
        cfg = PipelineConfig.from_json({"context_threshold_pct": 80})
        assert cfg.context_threshold_pct == 80

    def test_frozen(self) -> None:
        """PipelineConfig is immutable."""
        cfg = PipelineConfig()
        with pytest.raises(AttributeError):
            cfg.claude_model = "something"  # type: ignore[misc]

    def test_to_json_all_fields(self) -> None:
        """to_json includes every field."""
        cfg = PipelineConfig()
        data = json.loads(cfg.to_json())
        expected_keys = {f.name for f in fields(PipelineConfig)}
        assert set(data.keys()) == expected_keys

    def test_merge_returns_new_instance(self) -> None:
        """merge returns a new object, not the same one."""
        cfg = PipelineConfig()
        merged = cfg.merge({"lease_ttl_sec": 99})
        assert merged is not cfg
        assert merged.lease_ttl_sec == 99
        assert cfg.lease_ttl_sec == PIPELINE_LEASE_TTL_SEC


# ---------------------------------------------------------------------------
# Unit tests — Validation
# ---------------------------------------------------------------------------


class TestValidation:
    """Validation boundary tests for config values."""

    def test_pct_below_zero_raises(self) -> None:
        with pytest.raises(ConfigError, match="between 0 and 100"):
            PipelineConfig(context_threshold_pct=-1)

    def test_pct_above_100_raises(self) -> None:
        with pytest.raises(ConfigError, match="between 0 and 100"):
            PipelineConfig(context_threshold_pct=101)

    def test_pct_boundary_zero(self) -> None:
        cfg = PipelineConfig(context_threshold_pct=0)
        assert cfg.context_threshold_pct == 0

    def test_pct_boundary_100(self) -> None:
        cfg = PipelineConfig(context_threshold_pct=100)
        assert cfg.context_threshold_pct == 100

    def test_max_concurrent_stages_zero_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be >= 1"):
            PipelineConfig(max_concurrent_stages=0)

    def test_lease_ttl_zero_raises(self) -> None:
        with pytest.raises(ConfigError, match="must be >= 1"):
            PipelineConfig(lease_ttl_sec=0)

    def test_from_json_invalid_pct_raises(self) -> None:
        with pytest.raises(ConfigError):
            PipelineConfig.from_json({"context_threshold_pct": 200})

    def test_validate_pct_negative(self) -> None:
        with pytest.raises(ConfigError):
            _validate_pct(-5, "test_field")

    def test_validate_positive_zero(self) -> None:
        with pytest.raises(ConfigError):
            _validate_positive(0, "test_field")

    def test_validate_positive_negative(self) -> None:
        with pytest.raises(ConfigError):
            _validate_positive(-3, "test_field")


# ---------------------------------------------------------------------------
# Unit tests — Module-level env vars and derived paths
# ---------------------------------------------------------------------------


class TestModuleLevelConstants:
    """Verify module-level constants have correct types and relationships."""

    def test_clones_dir_under_base(self) -> None:
        assert CLONES_DIR == BUILD_YOUR_ROOM_DIR / "clones"

    def test_pipelines_dir_under_base(self) -> None:
        assert PIPELINES_DIR == BUILD_YOUR_ROOM_DIR / "pipelines"

    def test_database_url_is_string(self) -> None:
        assert isinstance(DATABASE_URL, str)

    def test_default_port_is_int(self) -> None:
        assert isinstance(DEFAULT_PORT, int)

    def test_claude_model_is_string(self) -> None:
        assert isinstance(DEFAULT_CLAUDE_MODEL, str)

    def test_codex_model_is_string(self) -> None:
        assert isinstance(DEFAULT_CODEX_MODEL, str)

    def test_spec_model_is_string(self) -> None:
        assert isinstance(SPEC_CLAUDE_MODEL, str)

    def test_context_threshold_is_int(self) -> None:
        assert isinstance(CONTEXT_THRESHOLD_PCT, int)

    def test_max_concurrent_is_int(self) -> None:
        assert isinstance(MAX_CONCURRENT_PIPELINES, int)

    def test_lease_ttl_is_int(self) -> None:
        assert isinstance(PIPELINE_LEASE_TTL_SEC, int)

    def test_heartbeat_interval_is_int(self) -> None:
        assert isinstance(PIPELINE_HEARTBEAT_INTERVAL_SEC, int)

    def test_api_keys_are_strings(self) -> None:
        assert isinstance(ANTHROPIC_API_KEY, str)
        assert isinstance(OPENAI_API_KEY, str)

    def test_devbrowser_path_is_path(self) -> None:
        from pathlib import Path
        assert isinstance(DEVBROWSER_SKILL_PATH, Path)

    def test_log_level_is_string(self) -> None:
        assert isinstance(LOG_LEVEL, str)
