# Task 5: Sandbox/path guard abstraction + per-stage tool profiles
## Session: 1 | Complexity: medium | Date: 2026-04-09

### What I did
- Created `src/build_your_room/sandbox.py` with WorkspaceSandbox, is_path_within_roots, and make_path_guard
- Created `src/build_your_room/tool_profiles.py` with StageType enum, ToolProfile, per-stage tool mappings, and CodexSandboxConfig
- Wrote 22 tests in `tests/test_sandbox.py` (7 property-based with Hypothesis, 15 unit tests)
- Wrote 18 tests in `tests/test_tool_profiles.py` (2 property-based, 16 unit tests)

### Design decisions
- `WorkspaceSandbox` is a frozen dataclass with four roots (clone_path, logs, artifacts, state), constructed via `for_pipeline()` factory method that follows the spec's directory layout convention
- `make_path_guard()` returns a closure `(tool_name, tool_input) -> bool` suitable for Claude SDK's `can_use_tool` callback. It maps tool names to their file-path parameter keys, resolves paths, and checks containment
- `DENIED_TOOLS` is a frozenset of tool names that are always blocked (Bash, Shell, Terminal, etc.) regardless of arguments
- Tool profiles are a static mapping from stage type to ToolProfile. File-only stages (spec_author, impl_plan, etc.) get Read/Write/Edit/Glob/Grep. Execution stages (impl_task, validation, etc.) additionally get harness MCP tools (run_tests, run_lint, etc.)
- Unknown stage types fall back to file-tools-only as a safe default

### Learnings
- Hypothesis health checks reject function-scoped fixtures (like `tmp_path`) by default because the fixture isn't reset between generated examples. For path containment tests where `tmp_path` is used as a stable base for constructing paths (with `mkdir(exist_ok=True)`), suppressing `HealthCheck.function_scoped_fixture` is safe and avoids refactoring every test
- `Path.resolve(strict=False)` normalizes `..` components without requiring the path to exist, which is exactly what we need for sandbox checks on paths that haven't been created yet
- `Path.relative_to()` raises `ValueError` (not returns False) when the path is not relative to the root — use try/except to check containment
- Separating the path-guard closure from the sandbox dataclass keeps concerns clean: sandbox defines the allowed roots, make_path_guard creates a callback that can be injected into the Claude SDK

### Verification
- [PASS] ruff check: All checks passed
- [PASS] mypy: Success, no issues found in 16 source files
- [PASS] pytest: 136 tests passed (40 new, 96 existing)
