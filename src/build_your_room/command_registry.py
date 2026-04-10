"""Command-template registry and postcondition verifiers.

App-owned command templates for repo-standard verification commands.
Command verifiers come from this registry — not from model text — so
agents cannot synthesize arbitrary shell commands. Subprocesses execute
with ``shell=False``, ``cwd=pipeline.clone_path``, and a scrubbed env.

Condition types supported:
- file_exists: check a file exists under the working directory
- tests_pass: run ``uv run pytest`` against specified targets
- lint_clean: run ``uv run ruff check`` against specified paths
- type_check: run ``uv run mypy src/ --ignore-missing-imports``
- task_completed: check an HTN task name is completed (DB lookup delegated to caller)
- custom_verifier: dispatch to an app-registered verifier function
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from build_your_room.sandbox import is_path_within_roots

logger = logging.getLogger(__name__)

# Minimal env vars forwarded to verifier subprocesses.
_SAFE_ENV_KEYS: frozenset[str] = frozenset({
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "VIRTUAL_ENV",
    "UV_CACHE_DIR",
    "TMPDIR",
    "TMP",
    "TEMP",
})


def _scrubbed_env() -> dict[str, str]:
    """Build a minimal environment for verifier subprocesses."""
    return {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConditionResult:
    """Result of a single postcondition or precondition check."""

    condition_type: str
    description: str
    passed: bool
    detail: str = ""


# ---------------------------------------------------------------------------
# Command templates
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandTemplate:
    """An app-owned command template for verification subprocesses.

    ``base_args`` is the fixed prefix (e.g. ``["uv", "run", "pytest"]``).
    ``suffix_args`` is appended after expanded dynamic args (e.g. ``["-v"]``).
    """

    name: str
    base_args: tuple[str, ...]
    suffix_args: tuple[str, ...] = ()

    def build_args(self, extra: Sequence[str] = ()) -> list[str]:
        """Construct the full argument list with optional dynamic args."""
        return [*self.base_args, *extra, *self.suffix_args]


# Default command templates for this repo.
DEFAULT_TEMPLATES: dict[str, CommandTemplate] = {
    "tests_pass": CommandTemplate(
        name="tests_pass",
        base_args=("uv", "run", "pytest"),
        suffix_args=("-v",),
    ),
    "lint_clean": CommandTemplate(
        name="lint_clean",
        base_args=("uv", "run", "ruff", "check"),
    ),
    "type_check": CommandTemplate(
        name="type_check",
        base_args=("uv", "run", "mypy", "src/", "--ignore-missing-imports"),
    ),
}


class CommandRegistry:
    """Registry of command templates for verification.

    Starts with default templates and allows overrides/additions.
    """

    def __init__(
        self,
        templates: dict[str, CommandTemplate] | None = None,
    ) -> None:
        self._templates: dict[str, CommandTemplate] = dict(DEFAULT_TEMPLATES)
        if templates:
            self._templates.update(templates)

    def get(self, name: str) -> CommandTemplate | None:
        return self._templates.get(name)

    def register(self, template: CommandTemplate) -> None:
        self._templates[template.name] = template

    def names(self) -> frozenset[str]:
        return frozenset(self._templates.keys())


# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------

async def run_cmd(
    args: Sequence[str],
    cwd: str | Path,
    *,
    timeout_sec: float = 300.0,
    allowed_roots: Sequence[Path] | None = None,
) -> tuple[int, str, str]:
    """Run a verification command as an async subprocess.

    Returns ``(returncode, stdout, stderr)``.

    Security:
    - ``shell=False`` (uses create_subprocess_exec, not shell)
    - Scrubbed environment (only safe vars forwarded)
    - ``cwd`` must be within ``allowed_roots`` if provided
    """
    cwd_path = Path(cwd)
    if allowed_roots and not is_path_within_roots(cwd_path, list(allowed_roots)):
        msg = f"cwd {cwd_path} not within allowed roots"
        logger.warning("run_cmd denied: %s", msg)
        return (1, "", msg)

    env = _scrubbed_env()

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd_path),
            env=env,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_sec
        )
        return (
            proc.returncode or 0,
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
        )
    except asyncio.TimeoutError:
        logger.error("run_cmd timed out after %.0fs: %s", timeout_sec, args)
        return (1, "", f"Command timed out after {timeout_sec}s")
    except FileNotFoundError as exc:
        logger.error("run_cmd executable not found: %s", exc)
        return (1, "", f"Executable not found: {exc}")
    except OSError as exc:
        logger.error("run_cmd OS error: %s", exc)
        return (1, "", f"OS error: {exc}")


# ---------------------------------------------------------------------------
# Path expansion helpers
# ---------------------------------------------------------------------------

def expand_test_targets(condition: dict[str, Any]) -> list[str]:
    """Extract pytest target paths/patterns from a condition dict.

    Supports:
    - ``pattern``: glob-style test file pattern (e.g. ``"tests/test_auth*"``)
    - ``paths``: explicit list of test file paths
    - Neither: returns empty list (run all tests)
    """
    targets: list[str] = []
    if "pattern" in condition:
        targets.append(condition["pattern"])
    if "paths" in condition:
        targets.extend(condition["paths"])
    return targets


def expand_paths(condition: dict[str, Any]) -> list[str]:
    """Extract file/directory paths from a condition dict.

    Supports:
    - ``scope``: a single path/directory to check
    - ``paths``: explicit list of paths
    - Neither: returns empty list (check default scope)
    """
    paths: list[str] = []
    if "scope" in condition:
        paths.append(condition["scope"])
    if "paths" in condition:
        paths.extend(condition["paths"])
    return paths


# ---------------------------------------------------------------------------
# Custom verifier registry
# ---------------------------------------------------------------------------

# Signature: (args_dict, cwd) -> (passed, detail)
CustomVerifierFn = Callable[[dict[str, Any], str | Path], tuple[bool, str]]


class VerifierRegistry:
    """Registry of custom verifier functions.

    Custom verifiers are app-authored functions registered by ID.
    Agents reference them by ``verifier_id`` but cannot synthesize new ones.
    """

    def __init__(self) -> None:
        self._verifiers: dict[str, CustomVerifierFn] = {}

    def register(self, verifier_id: str, fn: CustomVerifierFn) -> None:
        self._verifiers[verifier_id] = fn

    def get(self, verifier_id: str) -> CustomVerifierFn | None:
        return self._verifiers.get(verifier_id)

    def run(
        self, verifier_id: str, args: dict[str, Any], cwd: str | Path
    ) -> tuple[bool, str]:
        fn = self._verifiers.get(verifier_id)
        if fn is None:
            return (False, f"Unknown custom verifier: {verifier_id}")
        try:
            return fn(args, cwd)
        except Exception as exc:
            logger.error("Custom verifier %s failed: %s", verifier_id, exc)
            return (False, f"Verifier {verifier_id} raised: {exc}")

    def ids(self) -> frozenset[str]:
        return frozenset(self._verifiers.keys())


# Built-in custom verifier: check a Python symbol exists in a file.
def _python_symbol_exists(args: dict[str, Any], cwd: str | Path) -> tuple[bool, str]:
    """Check that a Python file contains a given symbol definition."""
    file_path = Path(cwd) / args.get("path", "")
    symbol = args.get("symbol", "")
    if not file_path.exists():
        return (False, f"File not found: {file_path}")
    content = file_path.read_text(encoding="utf-8", errors="replace")
    # Look for class/def/variable assignment matching the symbol name
    for line in content.splitlines():
        stripped = line.lstrip()
        if (
            stripped.startswith(f"class {symbol}")
            or stripped.startswith(f"def {symbol}")
            or stripped.startswith(f"{symbol} =")
            or stripped.startswith(f"{symbol}:")
        ):
            return (True, f"Found '{symbol}' in {args.get('path', '')}")
    return (False, f"Symbol '{symbol}' not found in {args.get('path', '')}")


# Singleton registries
_default_command_registry = CommandRegistry()
_default_verifier_registry = VerifierRegistry()
_default_verifier_registry.register("python_symbol_exists", _python_symbol_exists)


def get_default_command_registry() -> CommandRegistry:
    return _default_command_registry


def get_default_verifier_registry() -> VerifierRegistry:
    return _default_verifier_registry


# ---------------------------------------------------------------------------
# Condition verification dispatcher
# ---------------------------------------------------------------------------

async def verify_condition(
    condition: dict[str, Any],
    cwd: str | Path,
    *,
    command_registry: CommandRegistry | None = None,
    verifier_registry: VerifierRegistry | None = None,
    allowed_roots: Sequence[Path] | None = None,
    task_status_lookup: Callable[[str], bool] | None = None,
) -> ConditionResult:
    """Verify a single postcondition/precondition.

    ``condition`` must have a ``type`` key and a ``description`` key.
    Additional keys depend on the condition type.

    ``task_status_lookup`` is an optional callback that checks whether
    a named HTN task is completed — the caller (HTNPlanner) provides this.
    """
    cmd_reg = command_registry or _default_command_registry
    ver_reg = verifier_registry or _default_verifier_registry
    cond_type = condition.get("type", "unknown")
    description = condition.get("description", cond_type)

    if cond_type == "file_exists":
        target = Path(cwd) / condition.get("path", "")
        exists = target.exists()
        return ConditionResult(
            condition_type=cond_type,
            description=description,
            passed=exists,
            detail=f"{'Found' if exists else 'Missing'}: {condition.get('path', '')}",
        )

    if cond_type == "tests_pass":
        template = cmd_reg.get("tests_pass")
        if template is None:
            return ConditionResult(
                condition_type=cond_type,
                description=description,
                passed=False,
                detail="No tests_pass command template registered",
            )
        targets = expand_test_targets(condition)
        args = template.build_args(targets)
        rc, stdout, stderr = await run_cmd(
            args, cwd, allowed_roots=allowed_roots
        )
        passed = rc == 0
        detail = stdout if passed else stderr or stdout
        return ConditionResult(
            condition_type=cond_type,
            description=description,
            passed=passed,
            detail=detail,
        )

    if cond_type == "lint_clean":
        template = cmd_reg.get("lint_clean")
        if template is None:
            return ConditionResult(
                condition_type=cond_type,
                description=description,
                passed=False,
                detail="No lint_clean command template registered",
            )
        paths = expand_paths(condition)
        args = template.build_args(paths)
        rc, stdout, stderr = await run_cmd(
            args, cwd, allowed_roots=allowed_roots
        )
        passed = rc == 0
        detail = stdout if passed else stderr or stdout
        return ConditionResult(
            condition_type=cond_type,
            description=description,
            passed=passed,
            detail=detail,
        )

    if cond_type == "type_check":
        template = cmd_reg.get("type_check")
        if template is None:
            return ConditionResult(
                condition_type=cond_type,
                description=description,
                passed=False,
                detail="No type_check command template registered",
            )
        args = template.build_args()
        rc, stdout, stderr = await run_cmd(
            args, cwd, allowed_roots=allowed_roots
        )
        passed = rc == 0
        detail = stdout if passed else stderr or stdout
        return ConditionResult(
            condition_type=cond_type,
            description=description,
            passed=passed,
            detail=detail,
        )

    if cond_type == "task_completed":
        task_name = condition.get("task_name", "")
        if task_status_lookup is None:
            return ConditionResult(
                condition_type=cond_type,
                description=description,
                passed=False,
                detail="No task_status_lookup provided",
            )
        completed = task_status_lookup(task_name)
        return ConditionResult(
            condition_type=cond_type,
            description=description,
            passed=completed,
            detail=f"Task '{task_name}' {'completed' if completed else 'not completed'}",
        )

    if cond_type == "custom_verifier":
        verifier_id = condition.get("verifier_id", "")
        vargs = condition.get("args", {})
        passed, detail = ver_reg.run(verifier_id, vargs, cwd)
        return ConditionResult(
            condition_type=cond_type,
            description=description,
            passed=passed,
            detail=detail,
        )

    return ConditionResult(
        condition_type=cond_type,
        description=description,
        passed=False,
        detail=f"Unknown condition type: {cond_type}",
    )
