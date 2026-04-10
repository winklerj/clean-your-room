"""ValidationStage — runs harness-owned verification and optional browser validation.

The stage runner:
1. Runs harness-owned verification commands (tests, lint, typecheck) via command_registry
2. If any verification command fails → return "validation_failed" immediately
3. If uses_devbrowser is enabled:
   a. Start dev server via BrowserRunner
   b. Create an agent session for browser validation with structured output
   c. Parse the agent's browser validation result
   d. If record_on_success and passed: record browser artifact
4. Return "validated" on success, "validation_failed" on failure
5. Escalate when max iterations are exceeded (per on_max_rounds config)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from psycopg_pool import AsyncConnectionPool

from build_your_room.adapters.base import AgentAdapter, SessionConfig
from build_your_room.browser_runner import BrowserRunner, BrowserRunnerError
from build_your_room.command_registry import (
    CommandRegistry,
    ConditionResult,
    get_default_command_registry,
    run_cmd,
)
from build_your_room.config import DEVBROWSER_SKILL_PATH, PIPELINES_DIR, PipelineConfig
from build_your_room.sandbox import WorkspaceSandbox
from build_your_room.stage_graph import StageNode
from build_your_room.streaming import LogBuffer
from build_your_room.tool_profiles import get_tool_profile

logger = logging.getLogger(__name__)

STAGE_RESULT_VALIDATED = "validated"
STAGE_RESULT_VALIDATION_FAILED = "validation_failed"
STAGE_RESULT_ESCALATED = "escalated"

# Verification command names run by the harness before the agent session.
_VERIFICATION_CHECKS: tuple[str, ...] = ("tests_pass", "lint_clean", "type_check")

# Structured output schema for the browser-validation agent.
VALIDATION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "validated": {"type": "boolean"},
        "tests_passed": {"type": "boolean"},
        "lint_clean": {"type": "boolean"},
        "typecheck_clean": {"type": "boolean"},
        "browser_validated": {"type": "boolean"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "description": {"type": "string"},
                    "severity": {"type": "string"},
                },
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["validated", "tests_passed", "lint_clean", "typecheck_clean"],
}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationIssue:
    """A single validation issue reported by the agent."""

    category: str
    description: str
    severity: str = "medium"


@dataclass(frozen=True)
class ValidationResult:
    """Parsed validation result from the agent."""

    validated: bool
    tests_passed: bool
    lint_clean: bool
    typecheck_clean: bool
    browser_validated: bool = True
    issues: list[ValidationIssue] = field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# Parsing and decision logic
# ---------------------------------------------------------------------------


def parse_validation_result(
    structured: dict[str, Any] | None,
) -> ValidationResult | None:
    """Parse structured output from the validation agent.

    Returns None if the output cannot be parsed.
    """
    if structured is None:
        return None

    try:
        validated = bool(structured.get("validated", False))
        tests_passed = bool(structured.get("tests_passed", False))
        lint_clean = bool(structured.get("lint_clean", False))
        typecheck_clean = bool(structured.get("typecheck_clean", False))
        browser_validated = bool(structured.get("browser_validated", True))

        raw_issues = structured.get("issues") or []
        issues = [
            ValidationIssue(
                category=str(i.get("category", "unknown")),
                description=str(i.get("description", "")),
                severity=str(i.get("severity", "medium")),
            )
            for i in raw_issues
            if isinstance(i, dict)
        ]

        return ValidationResult(
            validated=validated,
            tests_passed=tests_passed,
            lint_clean=lint_clean,
            typecheck_clean=typecheck_clean,
            browser_validated=browser_validated,
            issues=issues,
            summary=str(structured.get("summary", "")),
        )
    except (TypeError, AttributeError):
        return None


def should_pass_validation(result: ValidationResult) -> bool:
    """Determine if the validation result constitutes a pass.

    All automated checks must pass: tests, lint, typecheck. If browser
    validation was attempted, it must also pass.
    """
    return (
        result.validated
        and result.tests_passed
        and result.lint_clean
        and result.typecheck_clean
        and result.browser_validated
    )


# ---------------------------------------------------------------------------
# Harness-owned verification commands
# ---------------------------------------------------------------------------


async def run_verification_commands(
    clone_path: str,
    command_registry: CommandRegistry | None = None,
) -> list[ConditionResult]:
    """Run harness-owned verification commands (tests, lint, typecheck).

    Uses the command_registry to build safe subprocess commands.
    Returns a list of ConditionResult for each check.
    """
    reg = command_registry or get_default_command_registry()
    results: list[ConditionResult] = []

    for check_name in _VERIFICATION_CHECKS:
        template = reg.get(check_name)
        if template is None:
            results.append(
                ConditionResult(
                    condition_type=check_name,
                    description=f"Run {check_name}",
                    passed=False,
                    detail=f"No {check_name} command template registered",
                )
            )
            continue

        args = template.build_args()
        rc, stdout, stderr = await run_cmd(args, clone_path)
        passed = rc == 0
        results.append(
            ConditionResult(
                condition_type=check_name,
                description=f"Run {check_name}",
                passed=passed,
                detail=stdout if passed else (stderr or stdout),
            )
        )

    return results


# ---------------------------------------------------------------------------
# Stage runner
# ---------------------------------------------------------------------------


async def run_validation_stage(
    *,
    pool: AsyncConnectionPool,
    pipeline_id: int,
    stage_id: int,
    node: StageNode,
    adapters: dict[str, AgentAdapter],
    log_buffer: LogBuffer,
    cancel_event: asyncio.Event,
    pipelines_dir: Path | None = None,
    browser_runner: BrowserRunner | None = None,
    command_registry: CommandRegistry | None = None,
) -> str:
    """Run the validation stage: verification commands + optional browser validation.

    Phase 1 runs harness-owned verification (tests, lint, typecheck) without
    burning an LLM call. If any fail, returns "validation_failed" immediately
    so the stage graph routes back to code_review.

    Phase 2 (when uses_devbrowser is enabled) starts the dev server and creates
    a browser-validation agent session that uses typed browser tools.

    Returns ``"validated"`` on success, ``"validation_failed"`` when checks
    fail, or ``"escalated"`` when max iterations are exceeded.
    """
    base_dir = pipelines_dir or PIPELINES_DIR

    pipeline = await _load_pipeline(pool, pipeline_id)
    clone_path = pipeline["clone_path"]
    config = PipelineConfig.from_json(pipeline.get("config_json"))

    sandbox = WorkspaceSandbox.for_pipeline(clone_path, base_dir, pipeline_id)

    # -- Resolve prompt -------------------------------------------------------
    prompt_body = await _resolve_prompt(pool, node.prompt)
    tool_profile = get_tool_profile(node.stage_type)

    session_config = SessionConfig(
        model=node.model,
        clone_path=clone_path,
        system_prompt=prompt_body,
        allowed_tools=list(tool_profile.all_tools),
        allowed_roots=sandbox.writable_roots_list,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
    )

    # -- Get adapter ----------------------------------------------------------
    adapter = adapters.get(node.agent)
    if adapter is None:
        _log(log_buffer, pipeline_id, f"No adapter for agent {node.agent!r}, escalating")
        return STAGE_RESULT_ESCALATED

    # -- Determine devbrowser availability ------------------------------------
    use_devbrowser = node.uses_devbrowser and config.devbrowser_enabled

    # -- Set up browser runner if needed --------------------------------------
    runner = browser_runner
    if use_devbrowser and runner is None:
        runner = BrowserRunner.for_pipeline(
            clone_path=clone_path,
            pipelines_dir=base_dir,
            pipeline_id=pipeline_id,
            devbrowser_skill_path=DEVBROWSER_SKILL_PATH,
        )

    # -- Cancellation gate 1 --------------------------------------------------
    if cancel_event.is_set():
        _log(log_buffer, pipeline_id, "Cancelled before verification")
        return STAGE_RESULT_ESCALATED

    # -- Phase 1: Harness-owned verification commands -------------------------
    _log(log_buffer, pipeline_id, "Running verification commands (tests, lint, typecheck)")

    verification_results = await run_verification_commands(
        clone_path, command_registry=command_registry,
    )

    all_verification_passed = all(r.passed for r in verification_results)

    for r in verification_results:
        status = "PASS" if r.passed else "FAIL"
        _log(log_buffer, pipeline_id, f"  [{status}] {r.condition_type}: {r.detail[:200]}")

    # Persist verification results as artifact
    artifact_dir = base_dir / str(pipeline_id) / "artifacts" / "validation"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    verification_artifact = artifact_dir / "verification_results.json"
    verification_artifact.write_text(
        json.dumps(
            [
                {
                    "check": r.condition_type,
                    "passed": r.passed,
                    "detail": r.detail[:500],
                }
                for r in verification_results
            ],
            indent=2,
        )
    )
    await _set_stage_artifact(pool, stage_id, str(verification_artifact))

    if not all_verification_passed:
        failed = [r.condition_type for r in verification_results if not r.passed]
        _log(log_buffer, pipeline_id, f"Verification failed: {', '.join(failed)}")
        return STAGE_RESULT_VALIDATION_FAILED

    _log(log_buffer, pipeline_id, "All verification commands passed")

    # -- Cancellation gate 2 --------------------------------------------------
    if cancel_event.is_set():
        _log(log_buffer, pipeline_id, "Cancelled after verification")
        return STAGE_RESULT_ESCALATED

    # -- Phase 2: Browser validation (if enabled) -----------------------------
    if not use_devbrowser:
        _log(log_buffer, pipeline_id, "No browser validation required — validated")
        return STAGE_RESULT_VALIDATED

    assert runner is not None  # ensured by use_devbrowser guard above

    # Check dev-browser availability and launch bridge
    devbrowser_available = BrowserRunner.is_available(runner.devbrowser_skill_path)
    if devbrowser_available:
        bridge_started = await runner.launch_bridge()
        if bridge_started:
            _log(log_buffer, pipeline_id, "Dev-browser bridge started for browser validation")
        else:
            _log(
                log_buffer, pipeline_id,
                "Dev-browser bridge failed to start — browser validation will use fallback mode",
            )
    else:
        _log(
            log_buffer, pipeline_id,
            "Dev-browser skill not installed — browser validation will use fallback mode",
        )

    _log(log_buffer, pipeline_id, "Starting browser validation")

    browser_passed = await _run_browser_validation(
        runner=runner,
        adapter=adapter,
        session_config=session_config,
        node=node,
        prompt_body=prompt_body,
        sandbox=sandbox,
        clone_path=clone_path,
        pool=pool,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        log_buffer=log_buffer,
        cancel_event=cancel_event,
        base_dir=base_dir,
    )

    if not browser_passed:
        await runner.cleanup()
        _log(log_buffer, pipeline_id, "Browser validation failed")
        return STAGE_RESULT_VALIDATION_FAILED

    # Record browser artifact on success if configured
    if node.record_on_success:
        try:
            artifact = await runner.browser_record_artifact(
                name="validation_success",
            )
            artifact_path = artifact.get("path", "")
            if artifact_path:
                await _set_stage_artifact(pool, stage_id, artifact_path)
                mode = "via bridge" if runner.has_bridge else "placeholder"
                _log(
                    log_buffer, pipeline_id,
                    f"Validation recording saved ({mode}): {artifact_path}",
                )
        except Exception:
            logger.warning(
                "Failed to record browser artifact for pipeline %d",
                pipeline_id,
                exc_info=True,
            )

    await runner.cleanup()
    _log(log_buffer, pipeline_id, "Validation passed (verification + browser)")
    return STAGE_RESULT_VALIDATED


# ---------------------------------------------------------------------------
# Browser validation
# ---------------------------------------------------------------------------


async def _run_browser_validation(
    *,
    runner: BrowserRunner,
    adapter: AgentAdapter,
    session_config: SessionConfig,
    node: StageNode,
    prompt_body: str,
    sandbox: WorkspaceSandbox,
    clone_path: str,
    pool: AsyncConnectionPool,
    pipeline_id: int,
    stage_id: int,
    log_buffer: LogBuffer,
    cancel_event: asyncio.Event,
    base_dir: Path,
) -> bool:
    """Start dev server, run agent browser validation session, parse result.

    Returns ``True`` if browser validation passed, ``False`` otherwise.
    """
    # Start dev server
    try:
        server_info = await runner.start_dev_server()
        dev_url = server_info.get("url", "http://localhost:3000")
        _log(log_buffer, pipeline_id, f"Dev server started at {dev_url}")
    except BrowserRunnerError as exc:
        _log(log_buffer, pipeline_id, f"Dev server failed to start: {exc}")
        return False

    if cancel_event.is_set():
        await runner.stop_dev_server()
        return False

    # Create agent session for browser validation
    session_db_id = await _create_session_row(
        pool, stage_id, node.agent, prompt_body,
    )

    browser_prompt = _build_browser_validation_prompt(prompt_body, dev_url)

    try:
        session = await adapter.start_session(session_config)
        try:
            if session.session_id:
                await _update_session_id(pool, session_db_id, session.session_id)

            turn_result = await session.send_turn(
                browser_prompt, output_schema=VALIDATION_OUTPUT_SCHEMA,
            )

            result = parse_validation_result(turn_result.structured_output)
            if result is None:
                _log(log_buffer, pipeline_id, "Browser validation returned unparseable output")
                await _complete_session(pool, session_db_id, "failed")
                return False

            for issue in result.issues:
                _log(
                    log_buffer, pipeline_id,
                    f"  [{issue.severity}] ({issue.category}) {issue.description[:200]}",
                )

            passed = result.browser_validated and result.validated
            await _complete_session(
                pool, session_db_id,
                "completed" if passed else "failed",
            )

            if passed:
                _log(log_buffer, pipeline_id, "Browser validation passed")
            else:
                _log(
                    log_buffer, pipeline_id,
                    f"Browser validation failed: {result.summary[:200]}",
                )

            # Save browser validation report
            report_path = base_dir / str(pipeline_id) / "artifacts" / "validation" / "browser_report.md"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(_build_validation_report(result))

            return passed

        finally:
            await session.close()

    except Exception as exc:
        _log(log_buffer, pipeline_id, f"Browser validation session error: {exc}")
        await _complete_session(pool, session_db_id, "failed")
        return False


def _build_browser_validation_prompt(base_prompt: str, dev_url: str) -> str:
    """Build the browser validation prompt with the dev server URL."""
    return (
        f"{base_prompt}\n\n"
        f"## Browser Validation\n\n"
        f"The dev server is running at: {dev_url}\n\n"
        f"Please validate the web application:\n"
        f"1. Navigate to the running app\n"
        f"2. Check for JavaScript console errors\n"
        f"3. Verify key UI functionality works correctly\n"
        f"4. Test critical user flows\n\n"
        f"Return structured JSON output with your assessment."
    )


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


def _build_validation_report(result: ValidationResult) -> str:
    """Build a human-readable validation report."""
    parts = [
        "# Validation Report\n",
        f"**Result:** {'PASSED' if result.validated else 'FAILED'}\n\n",
        "## Check Results\n",
        f"- Tests: {'PASS' if result.tests_passed else 'FAIL'}\n",
        f"- Lint: {'PASS' if result.lint_clean else 'FAIL'}\n",
        f"- Type checking: {'PASS' if result.typecheck_clean else 'FAIL'}\n",
        f"- Browser: {'PASS' if result.browser_validated else 'FAIL'}\n",
    ]

    if result.issues:
        parts.append(f"\n## Issues ({len(result.issues)})\n")
        for i, issue in enumerate(result.issues, 1):
            parts.append(
                f"  {i}. [{issue.severity}] ({issue.category}) {issue.description}\n"
            )

    if result.summary:
        parts.append(f"\n## Summary\n{result.summary}\n")

    return "".join(parts)


# ---------------------------------------------------------------------------
# Artifact path
# ---------------------------------------------------------------------------


def _report_artifact_path(pipelines_dir: Path, pipeline_id: int) -> Path:
    return (
        pipelines_dir / str(pipeline_id) / "artifacts"
        / "validation" / "report.md"
    )


# ---------------------------------------------------------------------------
# DB helpers (same pattern as other stage runners)
# ---------------------------------------------------------------------------


async def _load_pipeline(
    pool: AsyncConnectionPool, pipeline_id: int
) -> dict[str, Any]:
    async with pool.connection() as conn:
        row: dict[str, Any] | None = await (  # type: ignore[assignment]
            await conn.execute(
                "SELECT clone_path, review_base_rev, head_rev, config_json "
                "FROM pipelines WHERE id = %s",
                (pipeline_id,),
            )
        ).fetchone()
    if not row:
        raise ValueError(f"Pipeline {pipeline_id} not found")
    return dict(row)


async def _resolve_prompt(pool: AsyncConnectionPool, prompt_name: str) -> str:
    """Look up a prompt template by name, falling back to the name itself."""
    async with pool.connection() as conn:
        row: dict[str, Any] | None = await (  # type: ignore[assignment]
            await conn.execute(
                "SELECT body FROM prompts WHERE name = %s", (prompt_name,)
            )
        ).fetchone()
    if row:
        return row["body"]
    return prompt_name


async def _create_session_row(
    pool: AsyncConnectionPool,
    stage_id: int,
    agent_type: str,
    prompt_override: str,
) -> int:
    """Insert an agent_sessions row and return its ID."""
    async with pool.connection() as conn:
        row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO agent_sessions "
                "(pipeline_stage_id, session_type, prompt_override, status) "
                "VALUES (%s, %s, %s, 'running') RETURNING id",
                (stage_id, agent_type, prompt_override),
            )
        ).fetchone()
        await conn.commit()
    return row["id"]


async def _update_session_id(
    pool: AsyncConnectionPool, session_db_id: int, session_id: str
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agent_sessions SET session_id = %s WHERE id = %s",
            (session_id, session_db_id),
        )
        await conn.commit()


async def _complete_session(
    pool: AsyncConnectionPool, session_db_id: int, status: str
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agent_sessions SET status = %s, completed_at = now() WHERE id = %s",
            (status, session_db_id),
        )
        await conn.commit()


async def _set_stage_artifact(
    pool: AsyncConnectionPool, stage_id: int, artifact_path: str
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE pipeline_stages SET output_artifact = %s WHERE id = %s",
            (artifact_path, stage_id),
        )
        await conn.commit()


async def _create_escalation(
    pool: AsyncConnectionPool,
    pipeline_id: int,
    stage_id: int,
    reason: str,
    context: dict[str, Any],
) -> int:
    async with pool.connection() as conn:
        row: dict[str, Any] = await (  # type: ignore[assignment]
            await conn.execute(
                "INSERT INTO escalations "
                "(pipeline_id, pipeline_stage_id, reason, context_json) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (pipeline_id, stage_id, reason, json.dumps(context)),
            )
        ).fetchone()
        await conn.commit()
    return row["id"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(log_buffer: LogBuffer, pipeline_id: int, message: str) -> None:
    log_buffer.append(pipeline_id, f"[validation] {message}")
