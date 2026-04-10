# Task 18: ValidationStage

## What was built

Two-phase validation stage runner with harness-owned verification and optional browser validation.

### Phase 1: Harness Verification (no LLM cost)
- `run_verification_commands()` runs tests_pass, lint_clean, type_check via `CommandRegistry`
- Each check uses `run_cmd` (async subprocess, shell=False, scrubbed env)
- Results persisted as `verification_results.json` artifact
- Any failure returns `validation_failed` immediately — no LLM session created

### Phase 2: Browser Validation (optional, uses LLM)
- Only runs when `node.uses_devbrowser=True` AND `config.devbrowser_enabled=True`
- Starts dev server via `BrowserRunner`, creates agent session with structured output
- Agent gets browser validation prompt with dev server URL
- Result parsed via `parse_validation_result()` with defensive field extraction
- Recording captured on success when `node.record_on_success=True`
- `BrowserRunner.cleanup()` called on both success and failure paths

### Files
- `src/build_your_room/stages/validation.py` — stage runner (647 lines)
- `src/build_your_room/browser_runner.py` — already existed from scaffold, unchanged
- `tests/test_validation.py` — 46 tests (1159 lines)

## Key decisions

1. **Harness-first, not agent-first**: The original skeleton had the agent running tests/lint/typecheck via MCP tools, burning LLM tokens. Redesigned so the harness runs verification commands directly — the agent session is only created for browser validation.

2. **No internal retry loop**: The stage graph already handles retries via `validation_failed → code_review → validation` edges with `max_visits`. Internal retries would duplicate that mechanism.

3. **Two cancellation gates**: Gate 1 before verification, Gate 2 between verification and browser validation. Each returns `escalated`.

4. **Recording failure is non-blocking**: `browser_record_artifact` failure is logged but doesn't prevent returning `validated`.

## Learnings

- **Linter race condition with Write tool**: Ruff auto-fixes can modify a file between Read and Write calls, causing "file modified since read" errors. The test file was already complete from a previous attempt that succeeded despite the error message — always check the current state before assuming failure.

- **Mock BrowserRunner needs `spec=BrowserRunner`**: Using `AsyncMock(spec=BrowserRunner)` ensures only real methods are mockable, catching typos in test assertions.

- **`PipelineConfig.from_json` handles None gracefully**: Passing `config.get("config_json")` where the column may be NULL works because `from_json` treats None as empty config with defaults.

- **Test count milestone**: 602 tests total across the project (46 new for validation).
