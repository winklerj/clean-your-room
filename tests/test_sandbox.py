"""Tests for sandbox.py — workspace path guard enforcement.

Verifies the SideEffectsContained invariant: all file operations must
target paths within the pipeline's allowed roots (clone_path, logs_dir,
artifacts_dir, state_dir). Uses property-based testing to explore path
traversal edge cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, assume, settings
from hypothesis import strategies as st

from build_your_room.sandbox import (
    DENIED_TOOLS,
    WorkspaceSandbox,
    is_path_within_roots,
    make_path_guard,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Safe path segments: alphanumeric, no special chars
_safe_segment = st.from_regex(r"[a-zA-Z0-9_]{1,20}", fullmatch=True)

# Build relative paths under a root
_relative_path = st.lists(_safe_segment, min_size=1, max_size=5).map(
    lambda segs: "/".join(segs)
)


# ---------------------------------------------------------------------------
# is_path_within_roots — property tests
# ---------------------------------------------------------------------------


class TestIsPathWithinRoots:
    """Property-based tests for is_path_within_roots."""

    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        root_name=_safe_segment,
        sub_path=_relative_path,
    )
    def test_path_under_root_is_allowed(self, tmp_path: Path, root_name: str, sub_path: str) -> None:
        """Property: any path constructed as root/sub is within that root.

        Invariant: for all roots R and sub-paths S,
            is_path_within_roots(R/S, [R]) == True

        This is the fundamental containment guarantee. If this fails, the
        sandbox cannot enforce SideEffectsContained.
        """
        root = tmp_path / root_name
        root.mkdir(parents=True, exist_ok=True)
        candidate = root / sub_path
        assert is_path_within_roots(candidate, [root])

    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        root_a=_safe_segment,
        root_b=_safe_segment,
        sub_path=_relative_path,
    )
    def test_path_under_wrong_root_is_rejected(
        self, tmp_path: Path, root_a: str, root_b: str, sub_path: str
    ) -> None:
        """Property: a path under root_a is NOT within root_b (when disjoint).

        Invariant: for disjoint roots A and B and sub-path S,
            is_path_within_roots(A/S, [B]) == False

        Prevents cross-pipeline file access.
        """
        assume(root_a != root_b)
        a = tmp_path / root_a
        b = tmp_path / root_b
        a.mkdir(parents=True, exist_ok=True)
        b.mkdir(parents=True, exist_ok=True)
        candidate = a / sub_path
        assert not is_path_within_roots(candidate, [b])

    def test_empty_roots_rejects_everything(self, tmp_path: Path) -> None:
        """Edge case: no allowed roots means nothing is allowed.

        Invariant: is_path_within_roots(any_path, []) == False

        Catches misconfiguration where sandbox has no roots.
        """
        assert not is_path_within_roots(tmp_path / "anything", [])

    def test_dotdot_traversal_blocked(self, tmp_path: Path) -> None:
        """Traversal attack: ../escape must not bypass the sandbox.

        Invariant: path resolution neutralizes '..' components, so
        root/sub/../../etc/passwd resolves outside root and is rejected.

        Critical security property — without this, agents could escape
        the sandbox via crafted paths.
        """
        root = tmp_path / "sandbox_root"
        root.mkdir()
        # Attempt to escape via ..
        escape_path = root / "sub" / ".." / ".." / "etc" / "passwd"
        assert not is_path_within_roots(escape_path, [root])

    def test_absolute_path_outside_roots(self) -> None:
        """Absolute path not under any root is rejected.

        Invariant: /tmp/evil is not under /home/user/pipeline.
        """
        assert not is_path_within_roots(
            Path("/tmp/evil/file.txt"),
            [Path("/home/user/pipeline")],
        )

    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(sub_path=_relative_path)
    def test_multiple_roots_any_match_allows(
        self, tmp_path: Path, sub_path: str
    ) -> None:
        """Property: a path under any one of multiple roots is allowed.

        Invariant: is_path_within_roots(R2/S, [R1, R2, R3]) == True

        Ensures the sandbox checks all roots, not just the first.
        """
        r1 = tmp_path / "clone"
        r2 = tmp_path / "logs"
        r3 = tmp_path / "artifacts"
        for r in (r1, r2, r3):
            r.mkdir(parents=True, exist_ok=True)
        candidate = r2 / sub_path
        assert is_path_within_roots(candidate, [r1, r2, r3])

    def test_symlink_escape_blocked(self, tmp_path: Path) -> None:
        """Symlink attack: a symlink inside the root pointing outside must be caught.

        Invariant: symlink resolution reveals the true target, which is
        outside the allowed roots, so it must be rejected.

        Prevents agents from creating symlinks to escape the sandbox.
        """
        root = tmp_path / "sandbox"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("sensitive data")

        link = root / "escape_link"
        link.symlink_to(secret)

        # The resolved path of the symlink is outside the root
        assert not is_path_within_roots(link.resolve(), [root])


# ---------------------------------------------------------------------------
# WorkspaceSandbox
# ---------------------------------------------------------------------------


class TestWorkspaceSandbox:
    """Tests for WorkspaceSandbox construction and behavior."""

    def test_for_pipeline_creates_correct_roots(self, tmp_path: Path) -> None:
        """Factory method produces sandbox with expected directory layout.

        Invariant: for_pipeline(clone, pipelines_dir, id) produces roots at
        clone, pipelines_dir/id/logs, pipelines_dir/id/artifacts, pipelines_dir/id/state.
        """
        clone = tmp_path / "clone" / "42"
        pipelines_dir = tmp_path / "pipelines"
        sandbox = WorkspaceSandbox.for_pipeline(clone, pipelines_dir, 42)

        assert sandbox.clone_path == clone
        assert sandbox.logs_dir == pipelines_dir / "42" / "logs"
        assert sandbox.artifacts_dir == pipelines_dir / "42" / "artifacts"
        assert sandbox.state_dir == pipelines_dir / "42" / "state"

    def test_allowed_roots_contains_all_four(self, tmp_path: Path) -> None:
        """allowed_roots returns exactly the four expected roots.

        Invariant: len(sandbox.allowed_roots) == 4 and all are Path objects.
        """
        sandbox = WorkspaceSandbox.for_pipeline(
            tmp_path / "clone", tmp_path / "pipelines", 1
        )
        roots = sandbox.allowed_roots
        assert len(roots) == 4
        assert all(isinstance(r, Path) for r in roots)

    def test_is_allowed_delegates_correctly(self, tmp_path: Path) -> None:
        """is_allowed() is equivalent to is_path_within_roots(path, allowed_roots).

        Verifies the convenience method works for both allowed and disallowed paths.
        """
        clone = tmp_path / "clone"
        clone.mkdir()
        sandbox = WorkspaceSandbox.for_pipeline(clone, tmp_path / "pipelines", 1)

        assert sandbox.is_allowed(clone / "src" / "main.py")
        assert not sandbox.is_allowed(Path("/etc/passwd"))

    def test_writable_roots_list_returns_strings(self, tmp_path: Path) -> None:
        """writable_roots_list returns string paths for Codex sandbox config.

        Invariant: result is a list of strings, one per allowed root.
        """
        sandbox = WorkspaceSandbox.for_pipeline(
            tmp_path / "clone", tmp_path / "pipelines", 1
        )
        roots = sandbox.writable_roots_list
        assert isinstance(roots, list)
        assert len(roots) == 4
        assert all(isinstance(r, str) for r in roots)

    def test_frozen_sandbox_is_immutable(self, tmp_path: Path) -> None:
        """WorkspaceSandbox is frozen — fields cannot be reassigned.

        Invariant: sandbox immutability prevents runtime tampering.
        """
        sandbox = WorkspaceSandbox.for_pipeline(
            tmp_path / "clone", tmp_path / "pipelines", 1
        )
        with pytest.raises(AttributeError):
            sandbox.clone_path = Path("/tmp/evil")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# make_path_guard
# ---------------------------------------------------------------------------


class TestMakePathGuard:
    """Tests for the can_use_tool callback factory."""

    def test_denies_bash_tool(self, tmp_path: Path) -> None:
        """Bash is always denied regardless of arguments.

        Invariant: for all inputs I, guard("Bash", I) == False.

        This is the primary defense against arbitrary command execution.
        """
        guard = make_path_guard([tmp_path])
        for tool in DENIED_TOOLS:
            assert not guard(tool, {}), f"{tool} should be denied"

    def test_allows_file_tool_within_root(self, tmp_path: Path) -> None:
        """File tools with paths inside the root are permitted.

        Invariant: guard("Write", {"file_path": root/x}) == True.
        """
        guard = make_path_guard([tmp_path])
        assert guard("Write", {"file_path": str(tmp_path / "src" / "main.py")})

    def test_denies_file_tool_outside_root(self, tmp_path: Path) -> None:
        """File tools with paths outside the root are blocked.

        Invariant: guard("Write", {"file_path": outside_path}) == False.
        """
        guard = make_path_guard([tmp_path / "sandbox"])
        assert not guard("Write", {"file_path": "/etc/passwd"})

    def test_allows_tool_with_no_file_params(self, tmp_path: Path) -> None:
        """Non-filesystem tools are allowed through.

        Invariant: tools not in _FILE_PATH_PARAMS are always allowed.

        Prevents over-blocking of tools like Agent, TaskCreate, etc.
        """
        guard = make_path_guard([tmp_path])
        assert guard("SomeCustomTool", {"query": "hello"})

    def test_allows_tool_with_none_input(self, tmp_path: Path) -> None:
        """Tools called with no input dict are allowed (except denied tools).

        Invariant: guard(non_denied_tool, None) == True.
        """
        guard = make_path_guard([tmp_path])
        assert guard("Read", None)

    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        root_name=_safe_segment,
        sub_path=_relative_path,
        tool_name=st.sampled_from(["Read", "Write", "Edit"]),
    )
    def test_file_tools_within_root_always_allowed(
        self, tmp_path: Path, root_name: str, sub_path: str, tool_name: str
    ) -> None:
        """Property: file tools targeting paths under the root always pass.

        Invariant: for all file tools T, roots R, and sub-paths S:
            guard(T, {path_param: R/S}) == True
        """
        root = tmp_path / root_name
        root.mkdir(parents=True, exist_ok=True)
        guard = make_path_guard([root])
        path_key = "file_path"
        assert guard(tool_name, {path_key: str(root / sub_path)})

    def test_glob_uses_path_param(self, tmp_path: Path) -> None:
        """Glob tool checks its 'path' parameter, not 'file_path'.

        Invariant: guard("Glob", {"path": outside}) == False.
        """
        guard = make_path_guard([tmp_path / "sandbox"])
        assert not guard("Glob", {"path": "/etc"})
        assert guard("Glob", {"path": str(tmp_path / "sandbox" / "src")})

    def test_grep_uses_path_param(self, tmp_path: Path) -> None:
        """Grep tool checks its 'path' parameter.

        Invariant: guard("Grep", {"path": outside}) == False.
        """
        guard = make_path_guard([tmp_path / "sandbox"])
        assert not guard("Grep", {"path": "/etc"})
        assert guard("Grep", {"path": str(tmp_path / "sandbox" / "tests")})

    def test_missing_path_param_allows_tool(self, tmp_path: Path) -> None:
        """File tool without the path parameter set is allowed.

        Invariant: guard("Read", {"other_key": "value"}) == True.

        The path parameter might be optional for some tool invocations.
        """
        guard = make_path_guard([tmp_path])
        assert guard("Read", {"other_key": "value"})

    def test_multiple_roots_checked(self, tmp_path: Path) -> None:
        """Guard checks all provided roots, not just the first.

        Invariant: path under root_2 is allowed when guard has [root_1, root_2].
        """
        r1 = tmp_path / "clone"
        r2 = tmp_path / "logs"
        r1.mkdir()
        r2.mkdir()
        guard = make_path_guard([r1, r2])
        assert guard("Write", {"file_path": str(r2 / "output.log")})
