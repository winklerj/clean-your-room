"""CloneManager — repo cloning, workspace-ref management, and cleanup.

Manages isolated repository clones for pipeline execution. Each pipeline
gets its own clone under CLONES_DIR/{pipeline_id}/ plus support directories
under PIPELINES_DIR/{pipeline_id}/.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg_pool import AsyncConnectionPool

from build_your_room.config import CLONES_DIR, PIPELINES_DIR

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CloneResult:
    """Result of creating a pipeline clone."""

    clone_path: Path
    review_base_rev: str
    workspace_ref: str | None


class GitError(Exception):
    """Raised when a git subprocess command fails."""

    def __init__(self, command: list[str], returncode: int, stderr: str) -> None:
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"git command failed (rc={returncode}): {' '.join(command)}\n{stderr}"
        )


async def _run_git(args: list[str], cwd: Path | str | None = None) -> str:
    """Run a git command and return stripped stdout. Raises GitError on failure."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise GitError(
            ["git", *args], proc.returncode or 1, stderr.decode().strip()
        )
    return stdout.decode().strip()


class CloneManager:
    """Manages isolated repo clones for pipeline execution."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        clones_dir: Path | None = None,
        pipelines_dir: Path | None = None,
    ) -> None:
        self._pool = pool
        self._clones_dir = clones_dir or CLONES_DIR
        self._pipelines_dir = pipelines_dir or PIPELINES_DIR

    async def create_clone(
        self,
        pipeline_id: int,
        repo_id: int,
        *,
        workspace_ref_name: str | None = None,
    ) -> CloneResult:
        """Clone a repo for a pipeline and update the pipeline row.

        Creates the clone directory, checks out the default branch,
        captures review_base_rev, optionally creates a workspace branch,
        and sets up pipeline support directories.
        """
        async with self._pool.connection() as conn:
            repo_row: dict[str, Any] | None = await (  # type: ignore[assignment]
                await conn.execute(
                    "SELECT local_path, git_url, default_branch FROM repos WHERE id = %s",
                    (repo_id,),
                )
            ).fetchone()

        if not repo_row:
            raise ValueError(f"Repo {repo_id} not found")

        clone_path = self._clones_dir / str(pipeline_id)
        if clone_path.exists():
            raise FileExistsError(f"Clone directory already exists: {clone_path}")

        source = repo_row["local_path"]
        if not Path(source).exists():
            if repo_row["git_url"]:
                source = repo_row["git_url"]
            else:
                raise ValueError(
                    f"Repo {repo_id} local_path {source!r} does not exist "
                    "and no git_url is configured"
                )

        self._clones_dir.mkdir(parents=True, exist_ok=True)

        default_branch = repo_row["default_branch"] or "main"
        await _run_git(["clone", "--branch", default_branch, source, str(clone_path)])

        review_base_rev = await _run_git(["rev-parse", "HEAD"], cwd=clone_path)

        workspace_ref = None
        if workspace_ref_name:
            await _run_git(["checkout", "-b", workspace_ref_name], cwd=clone_path)
            workspace_ref = workspace_ref_name

        await self.ensure_pipeline_dirs(pipeline_id)

        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET clone_path = %s, review_base_rev = %s, "
                "workspace_ref = %s, workspace_state = 'clean', updated_at = now() "
                "WHERE id = %s",
                (str(clone_path), review_base_rev, workspace_ref, pipeline_id),
            )
            await conn.commit()

        result = CloneResult(
            clone_path=clone_path,
            review_base_rev=review_base_rev,
            workspace_ref=workspace_ref,
        )
        logger.info(
            "Created clone for pipeline %d at %s (base_rev=%s)",
            pipeline_id,
            clone_path,
            review_base_rev[:8],
        )
        return result

    async def get_current_rev(self, clone_path: Path | str) -> str:
        """Get the current HEAD revision hash."""
        return await _run_git(["rev-parse", "HEAD"], cwd=clone_path)

    async def is_workspace_clean(self, clone_path: Path | str) -> bool:
        """Check if the workspace has no uncommitted changes."""
        status = await _run_git(["status", "--porcelain"], cwd=clone_path)
        return status == ""

    async def reset_to_rev(self, clone_path: Path | str, rev: str) -> None:
        """Hard-reset the workspace to a specific revision and remove untracked files."""
        await _run_git(["reset", "--hard", rev], cwd=clone_path)
        await _run_git(["clean", "-fd"], cwd=clone_path)

    async def create_workspace_ref(
        self, clone_path: Path | str, ref_name: str
    ) -> None:
        """Create and checkout a new branch for the pipeline workspace."""
        await _run_git(["checkout", "-b", ref_name], cwd=clone_path)

    async def create_checkpoint_commit(
        self, clone_path: Path | str, message: str
    ) -> str | None:
        """Stage all working-tree changes and create a local checkpoint commit.

        The commit uses an in-process committer identity (``-c user.name``,
        ``-c user.email``) so it succeeds inside fresh clones with no global
        git config. The commit is local-only — never pushed.

        Returns the new HEAD revision, or ``None`` if the workspace is clean
        (no commit was created).
        """
        if await self.is_workspace_clean(clone_path):
            return None

        await _run_git(["add", "-A"], cwd=clone_path)

        # Re-check after staging in case `add -A` produced no actual change
        # (e.g. only ignored files were present).
        diff_index = await _run_git(
            ["diff", "--cached", "--name-only"], cwd=clone_path
        )
        if not diff_index:
            return None

        await _run_git(
            [
                "-c",
                "user.name=build-your-room",
                "-c",
                "user.email=build-your-room@local",
                "commit",
                "--no-gpg-sign",
                "--allow-empty-message",
                "-m",
                message,
            ],
            cwd=clone_path,
        )
        return await self.get_current_rev(clone_path)

    async def capture_dirty_diff(
        self, clone_path: Path | str, baseline_rev: str
    ) -> str:
        """Capture a diff of all changes (staged, unstaged, untracked) from baseline.

        Returns the combined diff text, or empty string if workspace is clean.
        """
        diff = await _run_git(["diff", baseline_rev], cwd=clone_path)

        untracked = await _run_git(
            ["ls-files", "--others", "--exclude-standard"], cwd=clone_path
        )

        parts = []
        if diff:
            parts.append(diff)
        if untracked:
            parts.append(f"--- Untracked files ---\n{untracked}")
        return "\n".join(parts)

    async def cleanup_clone(self, pipeline_id: int) -> bool:
        """Delete a pipeline's clone directory.

        Returns True if the directory was removed, False if it didn't exist.
        """
        clone_path = self._clones_dir / str(pipeline_id)
        if not clone_path.exists():
            return False

        shutil.rmtree(clone_path)
        logger.info("Cleaned up clone for pipeline %d", pipeline_id)
        return True

    async def cleanup_completed_clones(self) -> list[int]:
        """Bulk cleanup clones for all completed/cancelled/killed pipelines.

        Returns list of pipeline IDs whose clones were cleaned.
        """
        async with self._pool.connection() as conn:
            rows: list[dict[str, Any]] = await (  # type: ignore[assignment]
                await conn.execute(
                    "SELECT id, clone_path FROM pipelines "
                    "WHERE status IN ('completed', 'cancelled', 'killed') "
                    "AND clone_path IS NOT NULL"
                )
            ).fetchall()

        cleaned: list[int] = []
        for row in rows:
            clone_path = Path(row["clone_path"])
            if clone_path.exists():
                shutil.rmtree(clone_path)
                cleaned.append(row["id"])
                logger.info("Cleaned clone for pipeline %d", row["id"])

        return cleaned

    async def ensure_pipeline_dirs(self, pipeline_id: int) -> dict[str, Path]:
        """Create pipeline support directories (logs, artifacts, state)."""
        base = self._pipelines_dir / str(pipeline_id)
        dirs = {
            "logs": base / "logs",
            "artifacts": base / "artifacts",
            "state": base / "state",
        }
        for d in dirs.values():
            d.mkdir(parents=True, exist_ok=True)
        return dirs
