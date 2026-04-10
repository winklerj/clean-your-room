"""Workspace sandbox — path guard enforcement for the SideEffectsContained invariant.

Agents, review tooling, and postcondition verifiers may write only under
the pipeline's clone_path plus the app-owned logs/, artifacts/, and state/
directories. This module provides:

- WorkspaceSandbox: frozen dataclass defining allowed roots for a pipeline
- is_path_within_roots(): resolved-path containment check
- make_path_guard(): returns a can_use_tool callback for Claude Agent SDK
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# Tools that accept file path arguments and the parameter keys to check.
# Claude SDK tools use these parameter names for file paths.
_FILE_PATH_PARAMS: dict[str, tuple[str, ...]] = {
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "Glob": ("path",),
    "Grep": ("path",),
}

# Tools that are always denied regardless of arguments.
DENIED_TOOLS: frozenset[str] = frozenset({
    "Bash",
    "BashCommand",
    "Terminal",
    "Shell",
    "Execute",
})


def _resolve_path(path: str | Path, *, follow_symlinks: bool = True) -> Path:
    """Resolve a path to absolute form, optionally following symlinks."""
    p = Path(path)
    if not p.is_absolute():
        return p
    if follow_symlinks:
        try:
            return p.resolve(strict=False)
        except (OSError, ValueError):
            return p.resolve(strict=False)
    return p.resolve(strict=False)


def is_path_within_roots(
    path: str | Path,
    allowed_roots: Sequence[Path],
) -> bool:
    """Check whether a resolved path is contained within any allowed root.

    Resolves both the candidate path and each root to handle symlinks and
    '..' traversal. Returns False for empty roots or non-absolute paths
    that cannot be resolved.
    """
    if not allowed_roots:
        return False

    resolved = _resolve_path(path)

    for root in allowed_roots:
        resolved_root = _resolve_path(root)
        try:
            resolved.relative_to(resolved_root)
            return True
        except ValueError:
            continue

    return False


@dataclass(frozen=True)
class WorkspaceSandbox:
    """Defines the allowed filesystem roots for a pipeline execution.

    All file operations by agents and verifiers must target paths under
    one of these roots.
    """

    clone_path: Path
    logs_dir: Path
    artifacts_dir: Path
    state_dir: Path

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        """All roots where file operations are permitted."""
        return (self.clone_path, self.logs_dir, self.artifacts_dir, self.state_dir)

    @property
    def writable_roots_list(self) -> list[str]:
        """String list of writable roots for Codex sandbox config."""
        return [str(r) for r in self.allowed_roots]

    def is_allowed(self, path: str | Path) -> bool:
        """Check if a path falls within the sandbox."""
        return is_path_within_roots(path, self.allowed_roots)

    @classmethod
    def for_pipeline(
        cls,
        clone_path: Path | str,
        pipelines_dir: Path | str,
        pipeline_id: int,
    ) -> WorkspaceSandbox:
        """Construct a sandbox from pipeline layout conventions.

        clone_path: the pipeline's isolated repo clone
        pipelines_dir: base directory for pipeline support dirs
        pipeline_id: numeric pipeline ID
        """
        base = Path(pipelines_dir) / str(pipeline_id)
        return cls(
            clone_path=Path(clone_path),
            logs_dir=base / "logs",
            artifacts_dir=base / "artifacts",
            state_dir=base / "state",
        )


def make_path_guard(
    allowed_roots: Sequence[Path | str],
) -> Any:
    """Create a can_use_tool callback for Claude Agent SDK.

    Returns a callable that accepts tool_name and tool_input dict. It:
    - Denies any tool in DENIED_TOOLS (Bash, Shell, etc.)
    - For file-manipulating tools, extracts path parameters and checks
      each against the allowed roots
    - Allows tools with no file path parameters (non-filesystem tools)

    Usage with Claude SDK:
        can_use_tool=make_path_guard(sandbox.allowed_roots)
    """
    resolved_roots = tuple(Path(r).resolve() for r in allowed_roots)

    def _guard(tool_name: str, tool_input: dict[str, Any] | None = None) -> bool:
        if tool_name in DENIED_TOOLS:
            logger.warning("Denied tool %s: in DENIED_TOOLS", tool_name)
            return False

        if tool_input is None:
            return True

        param_keys = _FILE_PATH_PARAMS.get(tool_name)
        if param_keys is None:
            return True

        for key in param_keys:
            path_val = tool_input.get(key)
            if path_val is None:
                continue
            if not is_path_within_roots(path_val, resolved_roots):
                logger.warning(
                    "Denied tool %s: path %r not within allowed roots",
                    tool_name,
                    path_val,
                )
                return False

        return True

    return _guard
