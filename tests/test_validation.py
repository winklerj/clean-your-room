"""Tests for ValidationStage — harness-owned verification + optional browser validation.

Covers: happy-path validated, validation_failed on command failure, devbrowser
enabled/disabled, recording on success, cancellation at multiple gates, missing
adapter, browser validation pass/fail, DB session rows, artifact persistence,
unparseable browser output, property-based tests for validation decisions,
browser runner lifecycle, verification command registry integration,
prompt resolution, and report generation.

All agent interactions are mocked — no live API calls.
Verification commands use a mock CommandRegistry — no real subprocesses.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from build_your_room.browser_runner import BrowserRunner
from unittest.mock import patch

from build_your_room.command_registry import (
    CommandRegistry,
    CommandTemplate,
    ConditionResult,
)
from build_your_room.stages.validation import (
    STAGE_RESULT_ESCALATED,
    STAGE_RESULT_VALIDATED,
    STAGE_RESULT_VALIDATION_FAILED,
    ValidationIssue,
    ValidationResult,
    _build_browser_validation_prompt,
    _build_validation_report,
    _report_artifact_path,
    parse_validation_result,
    run_validation_stage,
    run_verification_commands,
    should_pass_validation,
)
from build_your_room.stage_graph import StageNode
from build_your_room.streaming import LogBuffer


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_categories = st.sampled_from(["tests", "lint", "typecheck", "browser", "other"])
_severities = st.sampled_from(["low", "medium", "high", "critical"])


@st.composite
def validation_structured_outputs(draw: st.DrawFn) -> dict[str, Any]:
    """Generate well-formed structured validation output dicts."""
    return {
        "validated": draw(st.booleans()),
        "tests_passed": draw(st.booleans()),
        "lint_clean": draw(st.booleans()),
        "typecheck_clean": draw(st.booleans()),
        "browser_validated": draw(st.booleans()),
        "issues": draw(
            st.lists(
                st.fixed_dictionaries(
                    {
                        "category": _categories,
                        "description": st.text(min_size=1, max_size=30).filter(
                            lambda t: "\r" not in t
                        ),
                        "severity": _severities,
                    }
                ),
                max_size=3,
            )
        ),
        "summary": draw(st.text(min_size=0, max_size=50).filter(lambda t: "\r" not in t)),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeTurnResult:
    """Minimal SessionResult for tests."""

    output: str = "Validation complete."
    structured_output: dict[str, Any] | None = None


def _make_node(**overrides: Any) -> StageNode:
    defaults: dict[str, Any] = {
        "key": "validation",
        "name": "Validation",
        "stage_type": "validation",
        "agent": "claude",
        "prompt": "validation_default",
        "model": "claude-sonnet-4-6",
        "max_iterations": 3,
        "uses_devbrowser": False,
        "record_on_success": False,
    }
    defaults.update(overrides)
    return StageNode(**defaults)


def _passed_browser_output() -> dict[str, Any]:
    return {
        "validated": True,
        "tests_passed": True,
        "lint_clean": True,
        "typecheck_clean": True,
        "browser_validated": True,
        "issues": [],
        "summary": "Browser validation passed.",
    }


def _failed_browser_output() -> dict[str, Any]:
    return {
        "validated": False,
        "tests_passed": True,
        "lint_clean": True,
        "typecheck_clean": True,
        "browser_validated": False,
        "issues": [
            {"category": "browser", "description": "Console error found", "severity": "high"}
        ],
        "summary": "Browser validation failed.",
    }


def _make_mock_session(
    output: str = "Validation complete.",
    structured_output: dict[str, Any] | None = None,
    session_id: str | None = "val-sess-1",
) -> AsyncMock:
    """Build a mock LiveSession."""
    session = AsyncMock()
    session.session_id = session_id
    session.send_turn.return_value = FakeTurnResult(
        output=output, structured_output=structured_output
    )
    return session


def _make_mock_adapter(session: AsyncMock | None = None) -> AsyncMock:
    adapter = AsyncMock()
    adapter.start_session.return_value = session or _make_mock_session()
    return adapter


def _all_pass_results() -> list[ConditionResult]:
    """Verification results where all checks pass."""
    return [
        ConditionResult(condition_type="tests_pass", description="Run tests_pass", passed=True, detail="ok"),
        ConditionResult(condition_type="lint_clean", description="Run lint_clean", passed=True, detail="ok"),
        ConditionResult(condition_type="type_check", description="Run type_check", passed=True, detail="ok"),
    ]


def _one_fail_results(failing: str = "tests_pass") -> list[ConditionResult]:
    """Verification results where one check fails."""
    return [
        ConditionResult(
            condition_type=name,
            description=f"Run {name}",
            passed=(name != failing),
            detail="ok" if name != failing else "FAILED",
        )
        for name in ("tests_pass", "lint_clean", "type_check")
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def log_buffer() -> LogBuffer:
    return LogBuffer()


@pytest.fixture
def cancel_event() -> asyncio.Event:
    return asyncio.Event()


@pytest.fixture
def tmp_pipelines_dir(tmp_path: Path) -> Path:
    return tmp_path / "pipelines"


@pytest.fixture
async def pool_with_stage(initialized_db):
    """Provide an async pool with a seeded pipeline + pipeline_stage row.

    Yields (pool, pipeline_id, stage_id).
    """
    from build_your_room.db import get_pool

    pool = get_pool()
    async with pool.connection() as conn:
        repo_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO repos (name, local_path) "
                "VALUES ('test-repo-val', '/tmp/test-repo-val') RETURNING id"
            )
        ).fetchone()
        repo_id = repo_row["id"]

        graph_json = json.dumps(
            {
                "entry_stage": "validation",
                "nodes": [
                    {
                        "key": "validation",
                        "name": "Validation",
                        "type": "validation",
                        "agent": "claude",
                        "prompt": "validation_default",
                        "model": "claude-sonnet-4-6",
                        "max_iterations": 3,
                    }
                ],
                "edges": [],
            }
        )
        pdef_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO pipeline_defs (name, stage_graph_json) "
                "VALUES ('test-def-val', %s) RETURNING id",
                (graph_json,),
            )
        ).fetchone()
        pdef_id = pdef_row["id"]

        p_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO pipelines "
                "(pipeline_def_id, repo_id, clone_path, review_base_rev, "
                " head_rev, status) "
                "VALUES (%s, %s, '/tmp/test-clone-val', 'abc123', 'def456', 'running') "
                "RETURNING id",
                (pdef_id, repo_id),
            )
        ).fetchone()
        pipeline_id = p_row["id"]

        stage_row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO pipeline_stages "
                "(pipeline_id, stage_key, attempt, stage_type, agent_type, "
                " status, max_iterations, started_at) "
                "VALUES (%s, 'validation', 1, 'validation', 'claude', "
                "'running', 3, now()) RETURNING id",
                (pipeline_id,),
            )
        ).fetchone()
        stage_id = stage_row["id"]

        await conn.commit()

    yield pool, pipeline_id, stage_id


# ---------------------------------------------------------------------------
# Unit tests — parse_validation_result
# ---------------------------------------------------------------------------


class TestParseValidationResult:
    """Tests for structured output parsing."""

    def test_parses_valid_output(self) -> None:
        result = parse_validation_result(_passed_browser_output())
        assert result is not None
        assert result.validated is True
        assert result.tests_passed is True
        assert result.lint_clean is True
        assert result.typecheck_clean is True

    def test_returns_none_for_none(self) -> None:
        assert parse_validation_result(None) is None

    def test_parses_issues(self) -> None:
        output = _failed_browser_output()
        result = parse_validation_result(output)
        assert result is not None
        assert len(result.issues) == 1
        assert result.issues[0].category == "browser"

    def test_defaults_missing_fields(self) -> None:
        result = parse_validation_result(
            {"validated": False, "tests_passed": True, "lint_clean": True, "typecheck_clean": True}
        )
        assert result is not None
        assert result.browser_validated is True  # default
        assert result.issues == []
        assert result.summary == ""

    def test_skips_non_dict_issues(self) -> None:
        result = parse_validation_result(
            {
                "validated": True,
                "tests_passed": True,
                "lint_clean": True,
                "typecheck_clean": True,
                "issues": ["not a dict", 42],
            }
        )
        assert result is not None
        assert result.issues == []


# ---------------------------------------------------------------------------
# Unit tests — should_pass_validation
# ---------------------------------------------------------------------------


class TestShouldPassValidation:
    """Tests for the validation decision gate."""

    def test_all_pass(self) -> None:
        result = ValidationResult(
            validated=True, tests_passed=True, lint_clean=True,
            typecheck_clean=True, browser_validated=True,
        )
        assert should_pass_validation(result) is True

    def test_tests_failed(self) -> None:
        result = ValidationResult(
            validated=True, tests_passed=False, lint_clean=True, typecheck_clean=True,
        )
        assert should_pass_validation(result) is False

    def test_lint_failed(self) -> None:
        result = ValidationResult(
            validated=True, tests_passed=True, lint_clean=False, typecheck_clean=True,
        )
        assert should_pass_validation(result) is False

    def test_typecheck_failed(self) -> None:
        result = ValidationResult(
            validated=True, tests_passed=True, lint_clean=True, typecheck_clean=False,
        )
        assert should_pass_validation(result) is False

    def test_browser_failed(self) -> None:
        result = ValidationResult(
            validated=True, tests_passed=True, lint_clean=True,
            typecheck_clean=True, browser_validated=False,
        )
        assert should_pass_validation(result) is False

    def test_not_validated_flag(self) -> None:
        result = ValidationResult(
            validated=False, tests_passed=True, lint_clean=True,
            typecheck_clean=True, browser_validated=True,
        )
        assert should_pass_validation(result) is False


# ---------------------------------------------------------------------------
# Unit tests — report artifact path
# ---------------------------------------------------------------------------


class TestReportArtifactPath:
    """Tests for report artifact path construction."""

    def test_produces_correct_path(self, tmp_path: Path) -> None:
        result = _report_artifact_path(tmp_path, 42)
        assert result == tmp_path / "42" / "artifacts" / "validation" / "report.md"

    @given(pipeline_id=st.integers(min_value=1, max_value=9999))
    def test_path_contains_pipeline_id(self, pipeline_id: int) -> None:
        base = Path("/tmp/pipelines")
        result = _report_artifact_path(base, pipeline_id)
        assert str(pipeline_id) in str(result)
        assert result.name == "report.md"


# ---------------------------------------------------------------------------
# Unit tests — report builder
# ---------------------------------------------------------------------------


class TestBuildValidationReport:
    """Tests for validation report markdown generation."""

    def test_passed_report(self) -> None:
        result = ValidationResult(
            validated=True, tests_passed=True, lint_clean=True, typecheck_clean=True,
        )
        report = _build_validation_report(result)
        assert "PASSED" in report

    def test_failed_report_with_issues(self) -> None:
        result = ValidationResult(
            validated=False, tests_passed=False, lint_clean=True, typecheck_clean=True,
            issues=[
                ValidationIssue(category="tests", description="test_auth failed", severity="high"),
            ],
        )
        report = _build_validation_report(result)
        assert "FAILED" in report
        assert "test_auth failed" in report
        assert "[high]" in report

    def test_report_includes_summary(self) -> None:
        result = ValidationResult(
            validated=True, tests_passed=True, lint_clean=True, typecheck_clean=True,
            summary="Everything looks good.",
        )
        report = _build_validation_report(result)
        assert "Everything looks good." in report


# ---------------------------------------------------------------------------
# Unit tests — browser validation prompt builder
# ---------------------------------------------------------------------------


class TestBuildBrowserValidationPrompt:
    """Tests for browser validation prompt construction."""

    def test_includes_base_prompt(self) -> None:
        prompt = _build_browser_validation_prompt("Base prompt.", "http://localhost:3000")
        assert prompt.startswith("Base prompt.")

    def test_includes_dev_url(self) -> None:
        prompt = _build_browser_validation_prompt("Validate.", "http://localhost:8080")
        assert "http://localhost:8080" in prompt


# ---------------------------------------------------------------------------
# Unit tests — run_verification_commands
# ---------------------------------------------------------------------------


class TestRunVerificationCommands:
    """Tests for harness-owned verification command execution."""

    @pytest.mark.asyncio
    async def test_all_pass(self, tmp_path: Path) -> None:
        """All verification commands pass → all results passed."""
        reg = CommandRegistry()
        # Override defaults with simple 'true' commands
        reg.register(CommandTemplate(name="tests_pass", base_args=("true",)))
        reg.register(CommandTemplate(name="lint_clean", base_args=("true",)))
        reg.register(CommandTemplate(name="type_check", base_args=("true",)))
        results = await run_verification_commands(str(tmp_path), command_registry=reg)
        assert len(results) == 3
        assert all(r.passed for r in results)

    @pytest.mark.asyncio
    async def test_one_fails(self, tmp_path: Path) -> None:
        """One failing command → that result has passed=False."""
        reg = CommandRegistry()
        reg.register(CommandTemplate(name="tests_pass", base_args=("true",)))
        reg.register(CommandTemplate(name="lint_clean", base_args=("false",)))
        reg.register(CommandTemplate(name="type_check", base_args=("true",)))
        results = await run_verification_commands(str(tmp_path), command_registry=reg)
        by_type = {r.condition_type: r for r in results}
        assert by_type["tests_pass"].passed is True
        assert by_type["lint_clean"].passed is False
        assert by_type["type_check"].passed is True

    @pytest.mark.asyncio
    async def test_missing_template(self, tmp_path: Path) -> None:
        """Missing command template → result with passed=False."""
        # Create registry with no matching templates
        reg = CommandRegistry()
        reg._templates.clear()  # clear all defaults
        results = await run_verification_commands(str(tmp_path), command_registry=reg)
        assert len(results) == 3
        assert all(not r.passed for r in results)


# ---------------------------------------------------------------------------
# Integration tests — happy path (verification only, no devbrowser)
# ---------------------------------------------------------------------------


_PATCH_VERIFY = "build_your_room.stages.validation.run_verification_commands"


@pytest.mark.asyncio
async def test_validation_passes_all_checks(
    pool_with_stage, log_buffer, cancel_event, tmp_pipelines_dir
):
    """All verification commands pass, no devbrowser → "validated"."""
    pool, pipeline_id, stage_id = pool_with_stage
    adapter = _make_mock_adapter()

    with patch(_PATCH_VERIFY, return_value=_all_pass_results()) as mock_verify:
        result = await run_validation_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

    assert result == STAGE_RESULT_VALIDATED
    mock_verify.assert_called_once()
    adapter.start_session.assert_not_called()


@pytest.mark.asyncio
async def test_verification_failure_returns_validation_failed(
    pool_with_stage, log_buffer, cancel_event, tmp_pipelines_dir
):
    """Verification command fails → "validation_failed"."""
    pool, pipeline_id, stage_id = pool_with_stage
    adapter = _make_mock_adapter()

    with patch(_PATCH_VERIFY, return_value=_one_fail_results("tests_pass")):
        result = await run_validation_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

    assert result == STAGE_RESULT_VALIDATION_FAILED


# ---------------------------------------------------------------------------
# Integration tests — missing adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_adapter_escalates(
    pool_with_stage, log_buffer, cancel_event, tmp_pipelines_dir
):
    """No adapter for agent type → escalated."""
    pool, pipeline_id, stage_id = pool_with_stage

    result = await run_validation_stage(
        pool=pool,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        node=_make_node(),
        adapters={},  # no adapters
        log_buffer=log_buffer,
        cancel_event=cancel_event,
        pipelines_dir=tmp_pipelines_dir,
    )

    assert result == STAGE_RESULT_ESCALATED


# ---------------------------------------------------------------------------
# Integration tests — cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_before_verification(
    pool_with_stage, log_buffer, cancel_event, tmp_pipelines_dir
):
    """Cancellation before verification → escalated."""
    pool, pipeline_id, stage_id = pool_with_stage
    cancel_event.set()
    adapter = _make_mock_adapter()

    result = await run_validation_stage(
        pool=pool,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        node=_make_node(),
        adapters={"claude": adapter},
        log_buffer=log_buffer,
        cancel_event=cancel_event,
        pipelines_dir=tmp_pipelines_dir,
    )

    assert result == STAGE_RESULT_ESCALATED


# ---------------------------------------------------------------------------
# Integration tests — devbrowser
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_devbrowser_passes(
    pool_with_stage, log_buffer, cancel_event, tmp_pipelines_dir
):
    """Verification passes + browser validation passes → "validated"."""
    pool, pipeline_id, stage_id = pool_with_stage
    session = _make_mock_session(structured_output=_passed_browser_output())
    adapter = _make_mock_adapter(session)

    mock_runner = AsyncMock(spec=BrowserRunner)
    mock_runner.start_dev_server.return_value = {"url": "http://localhost:3000", "pid": 1234}
    mock_runner.stop_dev_server.return_value = {"stopped": True, "pid": 1234}

    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE pipelines SET config_json = %s WHERE id = %s",
            (json.dumps({"devbrowser_enabled": True}), pipeline_id),
        )
        await conn.commit()

    with patch(_PATCH_VERIFY, return_value=_all_pass_results()):
        result = await run_validation_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(uses_devbrowser=True),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            browser_runner=mock_runner,
        )

    assert result == STAGE_RESULT_VALIDATED
    mock_runner.start_dev_server.assert_called_once()
    adapter.start_session.assert_called_once()


@pytest.mark.asyncio
async def test_devbrowser_fails_returns_validation_failed(
    pool_with_stage, log_buffer, cancel_event, tmp_pipelines_dir
):
    """Browser validation fails → "validation_failed"."""
    pool, pipeline_id, stage_id = pool_with_stage
    session = _make_mock_session(structured_output=_failed_browser_output())
    adapter = _make_mock_adapter(session)

    mock_runner = AsyncMock(spec=BrowserRunner)
    mock_runner.start_dev_server.return_value = {"url": "http://localhost:3000", "pid": 1234}

    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE pipelines SET config_json = %s WHERE id = %s",
            (json.dumps({"devbrowser_enabled": True}), pipeline_id),
        )
        await conn.commit()

    with patch(_PATCH_VERIFY, return_value=_all_pass_results()):
        result = await run_validation_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(uses_devbrowser=True),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            browser_runner=mock_runner,
        )

    assert result == STAGE_RESULT_VALIDATION_FAILED
    mock_runner.cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_devbrowser_recording_on_success(
    pool_with_stage, log_buffer, cancel_event, tmp_pipelines_dir
):
    """Record browser artifact on success when record_on_success=True."""
    pool, pipeline_id, stage_id = pool_with_stage
    session = _make_mock_session(structured_output=_passed_browser_output())
    adapter = _make_mock_adapter(session)

    mock_runner = AsyncMock(spec=BrowserRunner)
    mock_runner.start_dev_server.return_value = {"url": "http://localhost:3000", "pid": 1234}
    mock_runner.browser_record_artifact.return_value = {
        "path": "/tmp/recording.gif", "format": "gif", "name": "validation_success",
    }

    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE pipelines SET config_json = %s WHERE id = %s",
            (json.dumps({"devbrowser_enabled": True}), pipeline_id),
        )
        await conn.commit()

    with patch(_PATCH_VERIFY, return_value=_all_pass_results()):
        result = await run_validation_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(uses_devbrowser=True, record_on_success=True),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            browser_runner=mock_runner,
        )

    assert result == STAGE_RESULT_VALIDATED
    mock_runner.browser_record_artifact.assert_called_once_with(name="validation_success")
    mock_runner.cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_devbrowser_disabled_no_browser_session(
    pool_with_stage, log_buffer, cancel_event, tmp_pipelines_dir
):
    """Devbrowser disabled in config → no browser runner usage."""
    pool, pipeline_id, stage_id = pool_with_stage
    adapter = _make_mock_adapter()

    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE pipelines SET config_json = %s WHERE id = %s",
            (json.dumps({"devbrowser_enabled": False}), pipeline_id),
        )
        await conn.commit()

    with patch(_PATCH_VERIFY, return_value=_all_pass_results()):
        result = await run_validation_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(uses_devbrowser=True),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

    assert result == STAGE_RESULT_VALIDATED
    adapter.start_session.assert_not_called()


@pytest.mark.asyncio
async def test_devbrowser_node_disabled(
    pool_with_stage, log_buffer, cancel_event, tmp_pipelines_dir
):
    """Node uses_devbrowser=False → no browser session even with config enabled."""
    pool, pipeline_id, stage_id = pool_with_stage
    adapter = _make_mock_adapter()

    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE pipelines SET config_json = %s WHERE id = %s",
            (json.dumps({"devbrowser_enabled": True}), pipeline_id),
        )
        await conn.commit()

    with patch(_PATCH_VERIFY, return_value=_all_pass_results()):
        result = await run_validation_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(uses_devbrowser=False),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

    assert result == STAGE_RESULT_VALIDATED
    adapter.start_session.assert_not_called()


@pytest.mark.asyncio
async def test_devbrowser_recording_failure_does_not_block(
    pool_with_stage, log_buffer, cancel_event, tmp_pipelines_dir
):
    """Recording failure is logged but doesn't prevent validation success."""
    pool, pipeline_id, stage_id = pool_with_stage
    session = _make_mock_session(structured_output=_passed_browser_output())
    adapter = _make_mock_adapter(session)

    mock_runner = AsyncMock(spec=BrowserRunner)
    mock_runner.start_dev_server.return_value = {"url": "http://localhost:3000", "pid": 1234}
    mock_runner.browser_record_artifact.side_effect = RuntimeError("Recording failed")

    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE pipelines SET config_json = %s WHERE id = %s",
            (json.dumps({"devbrowser_enabled": True}), pipeline_id),
        )
        await conn.commit()

    with patch(_PATCH_VERIFY, return_value=_all_pass_results()):
        result = await run_validation_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(uses_devbrowser=True, record_on_success=True),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            browser_runner=mock_runner,
        )

    assert result == STAGE_RESULT_VALIDATED


@pytest.mark.asyncio
async def test_devbrowser_unparseable_output(
    pool_with_stage, log_buffer, cancel_event, tmp_pipelines_dir
):
    """Unparseable browser agent output → browser validation fails."""
    pool, pipeline_id, stage_id = pool_with_stage
    session = _make_mock_session(structured_output=None)
    adapter = _make_mock_adapter(session)

    mock_runner = AsyncMock(spec=BrowserRunner)
    mock_runner.start_dev_server.return_value = {"url": "http://localhost:3000", "pid": 1234}

    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE pipelines SET config_json = %s WHERE id = %s",
            (json.dumps({"devbrowser_enabled": True}), pipeline_id),
        )
        await conn.commit()

    with patch(_PATCH_VERIFY, return_value=_all_pass_results()):
        result = await run_validation_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(uses_devbrowser=True),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            browser_runner=mock_runner,
        )

    assert result == STAGE_RESULT_VALIDATION_FAILED


# ---------------------------------------------------------------------------
# Integration tests — DB session rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_session_rows_created(
    pool_with_stage, log_buffer, cancel_event, tmp_pipelines_dir
):
    """Browser validation creates and completes session rows."""
    pool, pipeline_id, stage_id = pool_with_stage

    session = _make_mock_session(structured_output=_passed_browser_output())
    adapter = _make_mock_adapter(session)

    mock_runner = AsyncMock(spec=BrowserRunner)
    mock_runner.start_dev_server.return_value = {"url": "http://localhost:3000", "pid": 1234}

    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE pipelines SET config_json = %s WHERE id = %s",
            (json.dumps({"devbrowser_enabled": True}), pipeline_id),
        )
        await conn.commit()

    with patch(_PATCH_VERIFY, return_value=_all_pass_results()):
        await run_validation_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(uses_devbrowser=True),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            browser_runner=mock_runner,
        )

    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT status, session_type, session_id FROM agent_sessions "
                "WHERE pipeline_stage_id = %s ORDER BY id",
                (stage_id,),
            )
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["status"] == "completed"
    assert rows[0]["session_id"] == "val-sess-1"


@pytest.mark.asyncio
async def test_no_session_rows_without_devbrowser(
    pool_with_stage, log_buffer, cancel_event, tmp_pipelines_dir
):
    """No devbrowser → no agent session rows created."""
    pool, pipeline_id, stage_id = pool_with_stage
    adapter = _make_mock_adapter()

    with patch(_PATCH_VERIFY, return_value=_all_pass_results()):
        await run_validation_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT id FROM agent_sessions WHERE pipeline_stage_id = %s",
                (stage_id,),
            )
        ).fetchall()

    assert len(rows) == 0


# ---------------------------------------------------------------------------
# Integration tests — artifact persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verification_artifact_saved(
    pool_with_stage, log_buffer, cancel_event, tmp_pipelines_dir
):
    """Verification results JSON is saved as stage artifact."""
    pool, pipeline_id, stage_id = pool_with_stage
    adapter = _make_mock_adapter()

    with patch(_PATCH_VERIFY, return_value=_all_pass_results()):
        await run_validation_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
        )

    async with pool.connection() as conn:
        row: dict[str, Any] | None = await (  # type: ignore[assignment]
            await conn.execute(
                "SELECT output_artifact FROM pipeline_stages WHERE id = %s",
                (stage_id,),
            )
        ).fetchone()

    assert row is not None
    assert row["output_artifact"] is not None
    assert "verification_results.json" in row["output_artifact"]
    # Verify the file exists and is valid JSON
    artifact_path = Path(row["output_artifact"])
    assert artifact_path.exists()
    data = json.loads(artifact_path.read_text())
    assert len(data) == 3  # tests_pass, lint_clean, type_check


# ---------------------------------------------------------------------------
# Integration tests — prompt resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_resolved_from_db(
    pool_with_stage, log_buffer, cancel_event, tmp_pipelines_dir
):
    """Prompt template resolved from prompts table when devbrowser is active."""
    pool, pipeline_id, stage_id = pool_with_stage

    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO prompts (name, body, stage_type, agent_type) "
            "VALUES ('validation_default', 'Run browser checks.', 'validation', 'claude')"
        )
        await conn.execute(
            "UPDATE pipelines SET config_json = %s WHERE id = %s",
            (json.dumps({"devbrowser_enabled": True}), pipeline_id),
        )
        await conn.commit()

    session = _make_mock_session(structured_output=_passed_browser_output())
    adapter = _make_mock_adapter(session)

    mock_runner = AsyncMock(spec=BrowserRunner)
    mock_runner.start_dev_server.return_value = {"url": "http://localhost:3000", "pid": 1234}

    with patch(_PATCH_VERIFY, return_value=_all_pass_results()):
        await run_validation_stage(
            pool=pool,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            node=_make_node(uses_devbrowser=True),
            adapters={"claude": adapter},
            log_buffer=log_buffer,
            cancel_event=cancel_event,
            pipelines_dir=tmp_pipelines_dir,
            browser_runner=mock_runner,
        )

    # Check the browser validation prompt includes the resolved body
    call_args = session.send_turn.call_args
    assert "Run browser checks." in call_args[0][0]


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestPropertyBasedValidation:
    """Property-based tests for validation result parsing and decisions."""

    @given(data=validation_structured_outputs())
    @settings(max_examples=50)
    def test_parse_roundtrip(self, data: dict[str, Any]) -> None:
        """Every well-formed structured output should parse successfully."""
        result = parse_validation_result(data)
        assert result is not None
        assert result.validated == bool(data["validated"])
        assert result.tests_passed == bool(data["tests_passed"])

    @given(data=validation_structured_outputs())
    @settings(max_examples=50)
    def test_should_pass_requires_all_checks(self, data: dict[str, Any]) -> None:
        """should_pass_validation only returns True when all checks pass."""
        result = parse_validation_result(data)
        assert result is not None
        if should_pass_validation(result):
            assert result.validated is True
            assert result.tests_passed is True
            assert result.lint_clean is True
            assert result.typecheck_clean is True
            assert result.browser_validated is True

    @given(
        validated=st.booleans(),
        tests=st.booleans(),
        lint=st.booleans(),
        tc=st.booleans(),
        browser=st.booleans(),
    )
    @settings(max_examples=50)
    def test_pass_iff_all_true(
        self, validated: bool, tests: bool, lint: bool, tc: bool, browser: bool
    ) -> None:
        """should_pass_validation ↔ all five booleans are True."""
        result = ValidationResult(
            validated=validated, tests_passed=tests, lint_clean=lint,
            typecheck_clean=tc, browser_validated=browser,
        )
        expected = validated and tests and lint and tc and browser
        assert should_pass_validation(result) == expected


# ---------------------------------------------------------------------------
# Browser runner unit tests
# ---------------------------------------------------------------------------


class TestBrowserRunner:
    """Unit tests for the BrowserRunner helper."""

    def test_for_pipeline_factory(self, tmp_path: Path) -> None:
        """Factory constructs correct paths."""
        runner = BrowserRunner.for_pipeline(
            clone_path=tmp_path / "clone",
            pipelines_dir=tmp_path / "pipelines",
            pipeline_id=42,
        )
        assert runner.clone_path == tmp_path / "clone"
        assert runner.logs_dir == tmp_path / "pipelines" / "42" / "logs"
        assert runner.artifacts_dir == tmp_path / "pipelines" / "42" / "artifacts"
        assert runner.state_dir == tmp_path / "pipelines" / "42" / "state"

    @pytest.mark.asyncio
    async def test_stop_dev_server_when_none(self) -> None:
        """Stopping when no server is running returns not-stopped."""
        runner = BrowserRunner(
            clone_path=Path("/tmp"), logs_dir=Path("/tmp"),
            artifacts_dir=Path("/tmp"), state_dir=Path("/tmp"),
        )
        result = await runner.stop_dev_server()
        assert result["stopped"] is False

    @pytest.mark.asyncio
    async def test_browser_open(self) -> None:
        """browser_open returns navigation metadata."""
        runner = BrowserRunner(
            clone_path=Path("/tmp"), logs_dir=Path("/tmp"),
            artifacts_dir=Path("/tmp"), state_dir=Path("/tmp"),
        )
        result = await runner.browser_open("http://localhost:3000")
        assert result["navigated"] is True
        assert result["url"] == "http://localhost:3000"

    @pytest.mark.asyncio
    async def test_browser_run_scenario(self) -> None:
        """browser_run_scenario returns a result."""
        runner = BrowserRunner(
            clone_path=Path("/tmp"), logs_dir=Path("/tmp"),
            artifacts_dir=Path("/tmp"), state_dir=Path("/tmp"),
        )
        result = await runner.browser_run_scenario("Click login button")
        assert result.passed is True
        assert "Click login button" in result.details

    @pytest.mark.asyncio
    async def test_browser_record_artifact(self, tmp_path: Path) -> None:
        """browser_record_artifact creates file under artifacts dir."""
        runner = BrowserRunner(
            clone_path=tmp_path / "clone", logs_dir=tmp_path / "logs",
            artifacts_dir=tmp_path / "artifacts", state_dir=tmp_path / "state",
        )
        result = await runner.browser_record_artifact(name="test_rec", format="gif")
        assert result["format"] == "gif"
        assert Path(result["path"]).exists()

    @pytest.mark.asyncio
    async def test_browser_console_errors_initially_empty(self) -> None:
        """Console errors list starts empty."""
        runner = BrowserRunner(
            clone_path=Path("/tmp"), logs_dir=Path("/tmp"),
            artifacts_dir=Path("/tmp"), state_dir=Path("/tmp"),
        )
        errors = await runner.browser_console_errors()
        assert errors == []

    @pytest.mark.asyncio
    async def test_cleanup_stops_server(self) -> None:
        """cleanup() delegates to stop_dev_server."""
        runner = BrowserRunner(
            clone_path=Path("/tmp"), logs_dir=Path("/tmp"),
            artifacts_dir=Path("/tmp"), state_dir=Path("/tmp"),
        )
        await runner.cleanup()
