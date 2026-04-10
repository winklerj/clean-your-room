"""Tests for command_registry.py — command templates and postcondition verifiers.

Verifies the command-template registry, path expansion helpers, subprocess
runner, custom verifier dispatch, and the top-level condition verification
dispatcher. Uses property-based tests for expansion/template invariants
and unit tests for async subprocess and verifier behavior.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from build_your_room.command_registry import (
    CommandRegistry,
    CommandTemplate,
    ConditionResult,
    DEFAULT_TEMPLATES,
    VerifierRegistry,
    _python_symbol_exists,
    _scrubbed_env,
    expand_paths,
    expand_test_targets,
    get_default_command_registry,
    get_default_verifier_registry,
    run_cmd,
    verify_condition,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_safe_segment = st.from_regex(r"[a-zA-Z0-9_]{1,20}", fullmatch=True)

_path_segment = st.from_regex(r"[a-zA-Z0-9_./]{1,30}", fullmatch=True)

_condition_type = st.sampled_from([
    "file_exists", "tests_pass", "lint_clean", "type_check",
    "task_completed", "custom_verifier",
])


# ---------------------------------------------------------------------------
# ConditionResult
# ---------------------------------------------------------------------------


class TestConditionResult:
    """Tests for the ConditionResult frozen dataclass."""

    def test_construction_with_all_fields(self) -> None:
        """ConditionResult stores all fields correctly.

        Invariant: construction preserves all provided values without mutation.
        """
        result = ConditionResult(
            condition_type="file_exists",
            description="Check login.py exists",
            passed=True,
            detail="Found: src/auth/login.py",
        )
        assert result.condition_type == "file_exists"
        assert result.description == "Check login.py exists"
        assert result.passed is True
        assert result.detail == "Found: src/auth/login.py"

    def test_default_detail_is_empty(self) -> None:
        """ConditionResult.detail defaults to empty string.

        Invariant: omitting detail produces empty string, not None.
        """
        result = ConditionResult(
            condition_type="tests_pass",
            description="All tests pass",
            passed=False,
        )
        assert result.detail == ""

    def test_frozen_immutability(self) -> None:
        """ConditionResult is immutable.

        Invariant: frozen dataclass prevents mutation after construction.
        """
        result = ConditionResult(
            condition_type="lint_clean",
            description="Lint clean",
            passed=True,
        )
        with pytest.raises(AttributeError):
            result.passed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CommandTemplate — property tests
# ---------------------------------------------------------------------------


class TestCommandTemplate:
    """Tests for the CommandTemplate frozen dataclass."""

    @given(
        name=_safe_segment,
        base=st.lists(_safe_segment, min_size=1, max_size=5),
        suffix=st.lists(_safe_segment, min_size=0, max_size=3),
        extra=st.lists(_safe_segment, min_size=0, max_size=4),
    )
    def test_build_args_preserves_order(
        self, name: str, base: list[str], suffix: list[str], extra: list[str]
    ) -> None:
        """Property: build_args returns base + extra + suffix in order.

        Invariant: for all templates T and extras E,
            T.build_args(E) == [*T.base_args, *E, *T.suffix_args]

        Ensures command construction is predictable and not reordered.
        """
        template = CommandTemplate(
            name=name,
            base_args=tuple(base),
            suffix_args=tuple(suffix),
        )
        result = template.build_args(extra)
        expected = [*base, *extra, *suffix]
        assert result == expected

    @given(
        name=_safe_segment,
        base=st.lists(_safe_segment, min_size=1, max_size=5),
        suffix=st.lists(_safe_segment, min_size=0, max_size=3),
    )
    def test_build_args_no_extra_is_base_plus_suffix(
        self, name: str, base: list[str], suffix: list[str]
    ) -> None:
        """Property: build_args() with no extra returns base + suffix.

        Invariant: T.build_args() == T.build_args([])
        """
        template = CommandTemplate(
            name=name,
            base_args=tuple(base),
            suffix_args=tuple(suffix),
        )
        assert template.build_args() == template.build_args([])
        assert template.build_args() == [*base, *suffix]

    def test_frozen_immutability(self) -> None:
        """CommandTemplate is immutable.

        Invariant: frozen dataclass prevents mutation.
        """
        template = CommandTemplate(name="test", base_args=("echo",))
        with pytest.raises(AttributeError):
            template.name = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Default templates
# ---------------------------------------------------------------------------


class TestDefaultTemplates:
    """Tests for the default command templates."""

    def test_tests_pass_template_builds_pytest_command(self) -> None:
        """Default tests_pass template produces 'uv run pytest ... -v'.

        Invariant: the default pytest command uses -v and accepts target args.
        """
        template = DEFAULT_TEMPLATES["tests_pass"]
        args = template.build_args(["tests/test_auth.py"])
        assert args == ["uv", "run", "pytest", "tests/test_auth.py", "-v"]

    def test_lint_clean_template_builds_ruff_command(self) -> None:
        """Default lint_clean template produces 'uv run ruff check ...'.

        Invariant: the default lint command uses ruff check and accepts scope args.
        """
        template = DEFAULT_TEMPLATES["lint_clean"]
        args = template.build_args(["src/auth/"])
        assert args == ["uv", "run", "ruff", "check", "src/auth/"]

    def test_type_check_template_builds_mypy_command(self) -> None:
        """Default type_check template produces fixed mypy command.

        Invariant: type_check command is fixed with no dynamic args.
        """
        template = DEFAULT_TEMPLATES["type_check"]
        args = template.build_args()
        assert args == ["uv", "run", "mypy", "src/", "--ignore-missing-imports"]


# ---------------------------------------------------------------------------
# CommandRegistry
# ---------------------------------------------------------------------------


class TestCommandRegistry:
    """Tests for the CommandRegistry class."""

    def test_default_registry_has_all_templates(self) -> None:
        """Registry initializes with all default templates.

        Invariant: a fresh registry contains tests_pass, lint_clean, type_check.
        """
        registry = CommandRegistry()
        assert registry.names() == {"tests_pass", "lint_clean", "type_check"}

    def test_get_existing_template(self) -> None:
        """Registry.get returns the correct template by name.

        Invariant: get(name) returns the template with matching name.
        """
        registry = CommandRegistry()
        template = registry.get("tests_pass")
        assert template is not None
        assert template.name == "tests_pass"

    def test_get_missing_returns_none(self) -> None:
        """Registry.get returns None for unknown names.

        Invariant: get(unknown) returns None, not KeyError.
        """
        registry = CommandRegistry()
        assert registry.get("nonexistent") is None

    def test_register_adds_new_template(self) -> None:
        """Registry.register adds a new template.

        Invariant: after register(T), get(T.name) returns T.
        """
        registry = CommandRegistry()
        custom = CommandTemplate(name="format", base_args=("uv", "run", "black"))
        registry.register(custom)
        assert registry.get("format") is custom
        assert "format" in registry.names()

    def test_register_overrides_existing(self) -> None:
        """Registry.register overrides an existing template.

        Invariant: register with existing name replaces the old template.
        """
        registry = CommandRegistry()
        custom = CommandTemplate(
            name="tests_pass",
            base_args=("python", "-m", "pytest"),
            suffix_args=("--tb=short",),
        )
        registry.register(custom)
        template = registry.get("tests_pass")
        assert template is not None
        assert template.base_args == ("python", "-m", "pytest")

    def test_constructor_with_custom_templates(self) -> None:
        """Registry constructor merges custom templates with defaults.

        Invariant: custom templates override defaults with same name.
        """
        custom = CommandTemplate(name="tests_pass", base_args=("pytest",))
        registry = CommandRegistry(templates={"tests_pass": custom})
        template = registry.get("tests_pass")
        assert template is not None
        assert template.base_args == ("pytest",)
        # Other defaults still present
        assert registry.get("lint_clean") is not None


# ---------------------------------------------------------------------------
# Path expansion helpers — property tests
# ---------------------------------------------------------------------------


class TestExpandTestTargets:
    """Property-based tests for expand_test_targets."""

    @given(pattern=_path_segment)
    def test_pattern_included_in_targets(self, pattern: str) -> None:
        """Property: if 'pattern' key is present, it appears in the result.

        Invariant: expand_test_targets({"pattern": P}) contains P.
        """
        result = expand_test_targets({"pattern": pattern})
        assert pattern in result

    @given(paths=st.lists(_path_segment, min_size=1, max_size=5))
    def test_paths_all_included(self, paths: list[str]) -> None:
        """Property: all entries from 'paths' appear in the result.

        Invariant: for all P in condition["paths"],
            P in expand_test_targets(condition)
        """
        result = expand_test_targets({"paths": paths})
        for p in paths:
            assert p in result

    def test_empty_condition_returns_empty(self) -> None:
        """Empty condition produces empty targets.

        Invariant: expand_test_targets({}) == []
        """
        assert expand_test_targets({}) == []

    @given(
        pattern=_path_segment,
        paths=st.lists(_path_segment, min_size=1, max_size=3),
    )
    def test_pattern_before_paths(self, pattern: str, paths: list[str]) -> None:
        """Property: pattern appears before paths entries.

        Invariant: ordering is [pattern, *paths] when both are present.
        """
        result = expand_test_targets({"pattern": pattern, "paths": paths})
        assert result[0] == pattern
        assert result[1:] == paths


class TestExpandPaths:
    """Property-based tests for expand_paths."""

    @given(scope=_path_segment)
    def test_scope_included(self, scope: str) -> None:
        """Property: if 'scope' key is present, it appears in the result.

        Invariant: expand_paths({"scope": S}) contains S.
        """
        result = expand_paths({"scope": scope})
        assert scope in result

    @given(paths=st.lists(_path_segment, min_size=1, max_size=5))
    def test_paths_all_included(self, paths: list[str]) -> None:
        """Property: all entries from 'paths' appear in the result.

        Invariant: for all P in condition["paths"],
            P in expand_paths(condition)
        """
        result = expand_paths({"paths": paths})
        for p in paths:
            assert p in result

    def test_empty_condition_returns_empty(self) -> None:
        """Empty condition produces empty paths.

        Invariant: expand_paths({}) == []
        """
        assert expand_paths({}) == []


# ---------------------------------------------------------------------------
# Scrubbed env
# ---------------------------------------------------------------------------


class TestScrubEnv:
    """Tests for the scrubbed environment builder."""

    def test_only_safe_keys_forwarded(self) -> None:
        """Scrubbed env contains only allowlisted keys.

        Invariant: every key in _scrubbed_env() is in _SAFE_ENV_KEYS.
        Critical for SideEffectsContained: prevents leaking secrets/tokens.
        """
        env = _scrubbed_env()
        from build_your_room.command_registry import _SAFE_ENV_KEYS
        for key in env:
            assert key in _SAFE_ENV_KEYS

    def test_path_is_forwarded_when_present(self) -> None:
        """PATH is forwarded so executables are discoverable.

        Invariant: if PATH is in os.environ, it's in the scrubbed env.
        """
        import os
        if "PATH" in os.environ:
            env = _scrubbed_env()
            assert "PATH" in env


# ---------------------------------------------------------------------------
# run_cmd — async tests
# ---------------------------------------------------------------------------


class TestRunCmd:
    """Tests for the async subprocess runner."""

    @pytest.mark.asyncio
    async def test_successful_command(self, tmp_path: Path) -> None:
        """run_cmd returns (0, stdout, '') for a successful command.

        Invariant: returncode 0 means the command succeeded.
        """
        rc, stdout, stderr = await run_cmd(
            ["echo", "hello world"], tmp_path
        )
        assert rc == 0
        assert "hello world" in stdout

    @pytest.mark.asyncio
    async def test_failed_command(self, tmp_path: Path) -> None:
        """run_cmd returns nonzero returncode for a failing command.

        Invariant: a command that exits nonzero has rc != 0.
        """
        rc, stdout, stderr = await run_cmd(
            ["python3", "-c", "import sys; sys.exit(1)"], tmp_path
        )
        assert rc == 1

    @pytest.mark.asyncio
    async def test_executable_not_found(self, tmp_path: Path) -> None:
        """run_cmd handles missing executables gracefully.

        Invariant: FileNotFoundError is caught and returned as (1, '', detail).
        """
        rc, stdout, stderr = await run_cmd(
            ["nonexistent_executable_xyz"], tmp_path
        )
        assert rc == 1
        assert "not found" in stderr.lower() or "Executable" in stderr

    @pytest.mark.asyncio
    async def test_cwd_outside_allowed_roots_denied(self, tmp_path: Path) -> None:
        """run_cmd denies execution when cwd is outside allowed roots.

        Invariant: if allowed_roots is set and cwd is not within any root,
        the command is not executed and returns (1, '', denial message).

        Critical for SideEffectsContained: verifiers must run inside the
        pipeline workspace.
        """
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        forbidden = tmp_path / "forbidden"
        forbidden.mkdir()

        rc, stdout, stderr = await run_cmd(
            ["echo", "hello"], forbidden, allowed_roots=[allowed]
        )
        assert rc == 1
        assert "not within allowed roots" in stderr

    @pytest.mark.asyncio
    async def test_cwd_inside_allowed_roots_permitted(self, tmp_path: Path) -> None:
        """run_cmd permits execution when cwd is within allowed roots.

        Invariant: cwd within allowed_roots lets the command proceed.
        """
        allowed = tmp_path / "workspace"
        allowed.mkdir()

        rc, stdout, stderr = await run_cmd(
            ["echo", "hello"], allowed, allowed_roots=[allowed]
        )
        assert rc == 0
        assert "hello" in stdout

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self, tmp_path: Path) -> None:
        """run_cmd returns error on timeout.

        Invariant: commands exceeding timeout_sec are killed and reported.
        """
        rc, stdout, stderr = await run_cmd(
            ["python3", "-c", "import time; time.sleep(10)"],
            tmp_path,
            timeout_sec=0.1,
        )
        assert rc == 1
        assert "timed out" in stderr.lower()


# ---------------------------------------------------------------------------
# VerifierRegistry
# ---------------------------------------------------------------------------


class TestVerifierRegistry:
    """Tests for the custom verifier registry."""

    def test_register_and_run(self) -> None:
        """Registered verifiers are callable by ID.

        Invariant: register(id, fn) then run(id, ...) calls fn.
        """
        registry = VerifierRegistry()
        registry.register("always_pass", lambda args, cwd: (True, "ok"))
        passed, detail = registry.run("always_pass", {}, "/tmp")
        assert passed is True
        assert detail == "ok"

    def test_unknown_verifier_fails(self) -> None:
        """Running an unknown verifier returns failure.

        Invariant: run(unknown_id, ...) returns (False, message).
        """
        registry = VerifierRegistry()
        passed, detail = registry.run("nonexistent", {}, "/tmp")
        assert passed is False
        assert "Unknown" in detail

    def test_verifier_exception_caught(self) -> None:
        """Verifier exceptions are caught and reported.

        Invariant: a raising verifier returns (False, error detail).
        """
        registry = VerifierRegistry()

        def raise_fn(args: dict, cwd: str | Path) -> tuple[bool, str]:
            raise ValueError("test error")

        registry.register("raiser", raise_fn)
        passed, detail = registry.run("raiser", {}, "/tmp")
        assert passed is False
        assert "test error" in detail

    def test_ids_returns_registered_names(self) -> None:
        """ids() returns all registered verifier IDs.

        Invariant: ids() == set of all registered verifier names.
        """
        registry = VerifierRegistry()
        registry.register("a", lambda args, cwd: (True, ""))
        registry.register("b", lambda args, cwd: (True, ""))
        assert registry.ids() == frozenset({"a", "b"})

    def test_get_returns_function(self) -> None:
        """get() returns the registered function or None.

        Invariant: get(id) returns fn if registered, None otherwise.
        """
        registry = VerifierRegistry()

        def my_fn(args: dict, cwd: str | Path) -> tuple[bool, str]:
            return (True, "")

        registry.register("my_fn", my_fn)
        assert registry.get("my_fn") is my_fn
        assert registry.get("missing") is None


# ---------------------------------------------------------------------------
# _python_symbol_exists built-in verifier
# ---------------------------------------------------------------------------


class TestPythonSymbolExists:
    """Tests for the built-in python_symbol_exists custom verifier."""

    def test_finds_class_definition(self, tmp_path: Path) -> None:
        """Detects class definitions.

        Invariant: 'class Foo' in file → symbol 'Foo' found.
        """
        (tmp_path / "models.py").write_text("class User:\n    pass\n")
        passed, detail = _python_symbol_exists(
            {"path": "models.py", "symbol": "User"}, tmp_path
        )
        assert passed is True

    def test_finds_function_definition(self, tmp_path: Path) -> None:
        """Detects function definitions.

        Invariant: 'def foo' in file → symbol 'foo' found.
        """
        (tmp_path / "utils.py").write_text("def helper():\n    pass\n")
        passed, detail = _python_symbol_exists(
            {"path": "utils.py", "symbol": "helper"}, tmp_path
        )
        assert passed is True

    def test_finds_variable_assignment(self, tmp_path: Path) -> None:
        """Detects variable assignments.

        Invariant: 'VAR = value' in file → symbol 'VAR' found.
        """
        (tmp_path / "config.py").write_text("MAX_SIZE = 100\n")
        passed, detail = _python_symbol_exists(
            {"path": "config.py", "symbol": "MAX_SIZE"}, tmp_path
        )
        assert passed is True

    def test_finds_type_annotation(self, tmp_path: Path) -> None:
        """Detects type-annotated assignments.

        Invariant: 'name: type' in file → symbol 'name' found.
        """
        (tmp_path / "types.py").write_text("items: list[str]\n")
        passed, detail = _python_symbol_exists(
            {"path": "types.py", "symbol": "items"}, tmp_path
        )
        assert passed is True

    def test_missing_symbol_fails(self, tmp_path: Path) -> None:
        """Missing symbols return failure.

        Invariant: symbol not in file → (False, detail).
        """
        (tmp_path / "empty.py").write_text("# nothing here\n")
        passed, detail = _python_symbol_exists(
            {"path": "empty.py", "symbol": "Missing"}, tmp_path
        )
        assert passed is False

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        """Missing files return failure.

        Invariant: nonexistent file → (False, detail with 'File not found').
        """
        passed, detail = _python_symbol_exists(
            {"path": "no_such_file.py", "symbol": "Foo"}, tmp_path
        )
        assert passed is False
        assert "not found" in detail.lower()


# ---------------------------------------------------------------------------
# Default singletons
# ---------------------------------------------------------------------------


class TestDefaultSingletons:
    """Tests for the default singleton accessors."""

    def test_default_command_registry_has_templates(self) -> None:
        """Default command registry contains all standard templates.

        Invariant: get_default_command_registry().names() ⊇ {tests_pass, lint_clean, type_check}.
        """
        registry = get_default_command_registry()
        assert {"tests_pass", "lint_clean", "type_check"} <= registry.names()

    def test_default_verifier_registry_has_python_symbol(self) -> None:
        """Default verifier registry includes python_symbol_exists.

        Invariant: get_default_verifier_registry().ids() contains 'python_symbol_exists'.
        """
        registry = get_default_verifier_registry()
        assert "python_symbol_exists" in registry.ids()


# ---------------------------------------------------------------------------
# verify_condition — integration tests
# ---------------------------------------------------------------------------


class TestVerifyCondition:
    """Tests for the top-level condition verification dispatcher."""

    @pytest.mark.asyncio
    async def test_file_exists_passes_when_present(self, tmp_path: Path) -> None:
        """file_exists condition passes when the file exists.

        Invariant: file_exists(path=P) passes iff P exists under cwd.
        """
        (tmp_path / "src" / "auth").mkdir(parents=True)
        (tmp_path / "src" / "auth" / "login.py").write_text("# login")

        result = await verify_condition(
            {"type": "file_exists", "path": "src/auth/login.py", "description": "Login exists"},
            tmp_path,
        )
        assert result.passed is True
        assert result.condition_type == "file_exists"

    @pytest.mark.asyncio
    async def test_file_exists_fails_when_missing(self, tmp_path: Path) -> None:
        """file_exists condition fails when the file is missing.

        Invariant: file_exists(path=P) fails when P doesn't exist.
        """
        result = await verify_condition(
            {"type": "file_exists", "path": "no/such/file.py", "description": "Missing file"},
            tmp_path,
        )
        assert result.passed is False
        assert "Missing" in result.detail

    @pytest.mark.asyncio
    async def test_task_completed_with_lookup(self) -> None:
        """task_completed uses the provided lookup callback.

        Invariant: task_completed calls task_status_lookup with the task name.
        """
        completed_tasks = {"setup_db", "create_models"}

        def lookup(name: str) -> bool:
            return name in completed_tasks

        result = await verify_condition(
            {"type": "task_completed", "task_name": "setup_db", "description": "DB setup done"},
            "/tmp",
            task_status_lookup=lookup,
        )
        assert result.passed is True

        result = await verify_condition(
            {"type": "task_completed", "task_name": "write_tests", "description": "Tests done"},
            "/tmp",
            task_status_lookup=lookup,
        )
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_task_completed_without_lookup_fails(self) -> None:
        """task_completed fails when no lookup callback is provided.

        Invariant: without task_status_lookup, task_completed always fails.
        """
        result = await verify_condition(
            {"type": "task_completed", "task_name": "anything", "description": "test"},
            "/tmp",
        )
        assert result.passed is False
        assert "No task_status_lookup" in result.detail

    @pytest.mark.asyncio
    async def test_custom_verifier_dispatches(self, tmp_path: Path) -> None:
        """custom_verifier dispatches to the verifier registry.

        Invariant: custom_verifier(verifier_id=V) calls registry.run(V, args, cwd).
        """
        (tmp_path / "models.py").write_text("class User:\n    pass\n")

        result = await verify_condition(
            {
                "type": "custom_verifier",
                "verifier_id": "python_symbol_exists",
                "args": {"path": "models.py", "symbol": "User"},
                "description": "User model exists",
            },
            tmp_path,
        )
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_unknown_condition_type_fails(self) -> None:
        """Unknown condition types return failure.

        Invariant: verify_condition with unknown type returns passed=False.
        """
        result = await verify_condition(
            {"type": "nonexistent_type", "description": "unknown"},
            "/tmp",
        )
        assert result.passed is False
        assert "Unknown condition type" in result.detail

    @pytest.mark.asyncio
    async def test_tests_pass_runs_subprocess(self, tmp_path: Path) -> None:
        """tests_pass condition executes pytest via run_cmd.

        Invariant: tests_pass builds command from template and runs it.
        Mocks run_cmd to avoid requiring a real pytest install in the test.
        """
        with patch(
            "build_your_room.command_registry.run_cmd",
            new_callable=AsyncMock,
            return_value=(0, "1 passed", ""),
        ) as mock_run:
            result = await verify_condition(
                {
                    "type": "tests_pass",
                    "pattern": "tests/test_auth*",
                    "description": "Auth tests pass",
                },
                tmp_path,
            )
            assert result.passed is True
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            assert call_args == ["uv", "run", "pytest", "tests/test_auth*", "-v"]

    @pytest.mark.asyncio
    async def test_lint_clean_runs_subprocess(self, tmp_path: Path) -> None:
        """lint_clean condition executes ruff via run_cmd.

        Invariant: lint_clean builds command from template and runs it.
        """
        with patch(
            "build_your_room.command_registry.run_cmd",
            new_callable=AsyncMock,
            return_value=(0, "All checks passed", ""),
        ) as mock_run:
            result = await verify_condition(
                {
                    "type": "lint_clean",
                    "scope": "src/auth/",
                    "description": "Auth lints clean",
                },
                tmp_path,
            )
            assert result.passed is True
            call_args = mock_run.call_args[0][0]
            assert call_args == ["uv", "run", "ruff", "check", "src/auth/"]

    @pytest.mark.asyncio
    async def test_type_check_runs_subprocess(self, tmp_path: Path) -> None:
        """type_check condition executes mypy via run_cmd.

        Invariant: type_check builds command from template and runs it.
        """
        with patch(
            "build_your_room.command_registry.run_cmd",
            new_callable=AsyncMock,
            return_value=(1, "", "src/foo.py:1: error: ..."),
        ) as mock_run:
            result = await verify_condition(
                {"type": "type_check", "description": "Types check"},
                tmp_path,
            )
            assert result.passed is False
            call_args = mock_run.call_args[0][0]
            assert call_args == [
                "uv", "run", "mypy", "src/", "--ignore-missing-imports"
            ]

    @pytest.mark.asyncio
    async def test_custom_registry_override(self, tmp_path: Path) -> None:
        """verify_condition uses custom registries when provided.

        Invariant: custom command_registry and verifier_registry override defaults.
        """
        custom_cmd_reg = CommandRegistry(templates={
            "tests_pass": CommandTemplate(
                name="tests_pass", base_args=("pytest",), suffix_args=("--tb=short",)
            ),
        })

        with patch(
            "build_your_room.command_registry.run_cmd",
            new_callable=AsyncMock,
            return_value=(0, "passed", ""),
        ) as mock_run:
            result = await verify_condition(
                {"type": "tests_pass", "description": "Custom test"},
                tmp_path,
                command_registry=custom_cmd_reg,
            )
            assert result.passed is True
            call_args = mock_run.call_args[0][0]
            assert call_args == ["pytest", "--tb=short"]

    @pytest.mark.asyncio
    async def test_description_defaults_to_type(self) -> None:
        """Missing description falls back to the condition type.

        Invariant: result.description == condition["type"] when "description" is absent.
        """
        result = await verify_condition(
            {"type": "file_exists", "path": "no_such_file"},
            "/tmp",
        )
        assert result.description == "file_exists"

    @pytest.mark.asyncio
    async def test_tests_pass_no_template_fails(self) -> None:
        """tests_pass fails gracefully when template is missing from registry.

        Invariant: missing template returns passed=False with clear message.
        """
        empty_reg = CommandRegistry(templates={})
        # Remove defaults by replacing the internal dict
        empty_reg._templates.clear()

        result = await verify_condition(
            {"type": "tests_pass", "description": "Tests"},
            "/tmp",
            command_registry=empty_reg,
        )
        assert result.passed is False
        assert "No tests_pass command template" in result.detail
