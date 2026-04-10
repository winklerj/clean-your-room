"""Tests for CloneManager — repo cloning, workspace operations, and cleanup."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from build_your_room.clone_manager import CloneManager, CloneResult, GitError, _run_git
from build_your_room.db import get_pool

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_GRAPH_JSON = json.dumps(
    {
        "entry_stage": "spec_author",
        "nodes": [
            {
                "key": "spec_author",
                "name": "Spec",
                "type": "spec_author",
                "agent": "claude",
                "prompt": "p",
                "model": "m",
                "max_iterations": 1,
            }
        ],
        "edges": [],
    }
)


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    """Create a temporary git repo with one commit as the clone source."""
    repo = tmp_path / "source_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, check=True, capture_output=True,
    )
    (repo / "README.md").write_text("# Test repo\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo


@pytest.fixture
def source_repo_with_two_commits(source_repo: Path) -> tuple[Path, str, str]:
    """Source repo with two commits. Returns (repo_path, rev1, rev2)."""
    rev1 = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_repo, check=True, capture_output=True, text=True,
    ).stdout.strip()

    (source_repo / "extra.txt").write_text("extra content\n")
    subprocess.run(["git", "add", "."], cwd=source_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Second commit"],
        cwd=source_repo, check=True, capture_output=True,
    )
    rev2 = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_repo, check=True, capture_output=True, text=True,
    ).stdout.strip()

    return source_repo, rev1, rev2


async def _seed_repo_and_pipeline(
    pool,
    local_path: str,
    *,
    git_url: str | None = None,
    default_branch: str = "main",
    status: str = "pending",
) -> tuple[int, int]:
    """Insert a repo and pipeline for testing. Returns (repo_id, pipeline_id)."""
    suffix = uuid.uuid4().hex[:8]
    async with pool.connection() as conn:
        repo_row = await (
            await conn.execute(
                "INSERT INTO repos (name, local_path, git_url, default_branch) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (f"test-repo-{suffix}", local_path, git_url, default_branch),
            )
        ).fetchone()
        repo_id = repo_row["id"]

        pdef_row = await (
            await conn.execute(
                "INSERT INTO pipeline_defs (name, stage_graph_json) "
                "VALUES (%s, %s) RETURNING id",
                (f"test-def-{suffix}", MINIMAL_GRAPH_JSON),
            )
        ).fetchone()
        pdef_id = pdef_row["id"]

        pipeline_row = await (
            await conn.execute(
                "INSERT INTO pipelines "
                "(pipeline_def_id, repo_id, clone_path, review_base_rev, status) "
                "VALUES (%s, %s, '', 'placeholder', %s) RETURNING id",
                (pdef_id, repo_id, status),
            )
        ).fetchone()
        await conn.commit()
        return repo_id, pipeline_row["id"]


# ---------------------------------------------------------------------------
# _run_git helper tests
# ---------------------------------------------------------------------------


class TestRunGit:
    """Tests for the low-level _run_git helper."""

    async def test_returns_stdout(self, source_repo: Path) -> None:
        """_run_git returns stripped stdout from successful commands."""
        result = await _run_git(["rev-parse", "HEAD"], cwd=source_repo)
        assert len(result) == 40  # SHA-1 hex length
        assert all(c in "0123456789abcdef" for c in result)

    async def test_raises_git_error_on_failure(self, tmp_path: Path) -> None:
        """_run_git raises GitError when the git command exits non-zero."""
        with pytest.raises(GitError) as exc_info:
            await _run_git(["rev-parse", "HEAD"], cwd=tmp_path)
        assert exc_info.value.returncode != 0
        assert "git" in str(exc_info.value)

    async def test_git_error_contains_stderr(self, tmp_path: Path) -> None:
        """GitError includes the stderr output from the failed command."""
        with pytest.raises(GitError) as exc_info:
            await _run_git(["rev-parse", "HEAD"], cwd=tmp_path)
        assert exc_info.value.stderr  # non-empty stderr


# ---------------------------------------------------------------------------
# Clone creation tests
# ---------------------------------------------------------------------------


class TestCreateClone:
    """Tests for CloneManager.create_clone."""

    async def test_creates_clone_from_local_path(
        self, initialized_db, source_repo: Path, tmp_path: Path
    ) -> None:
        """create_clone clones the repo and returns a valid CloneResult."""
        pool = get_pool()
        repo_id, pid = await _seed_repo_and_pipeline(pool, str(source_repo))
        mgr = CloneManager(pool, clones_dir=tmp_path / "clones", pipelines_dir=tmp_path / "pipes")

        result = await mgr.create_clone(pid, repo_id)

        assert isinstance(result, CloneResult)
        assert result.clone_path.exists()
        assert (result.clone_path / "README.md").exists()
        assert len(result.review_base_rev) == 40
        assert result.workspace_ref is None

    async def test_clone_captures_matching_review_base_rev(
        self, initialized_db, source_repo: Path, tmp_path: Path
    ) -> None:
        """review_base_rev matches the source repo's HEAD at clone time."""
        expected_rev = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source_repo, check=True, capture_output=True, text=True,
        ).stdout.strip()

        pool = get_pool()
        repo_id, pid = await _seed_repo_and_pipeline(pool, str(source_repo))
        mgr = CloneManager(pool, clones_dir=tmp_path / "clones", pipelines_dir=tmp_path / "pipes")

        result = await mgr.create_clone(pid, repo_id)
        assert result.review_base_rev == expected_rev

    async def test_clone_creates_workspace_ref(
        self, initialized_db, source_repo: Path, tmp_path: Path
    ) -> None:
        """create_clone with workspace_ref_name creates and checks out that branch."""
        pool = get_pool()
        repo_id, pid = await _seed_repo_and_pipeline(pool, str(source_repo))
        mgr = CloneManager(pool, clones_dir=tmp_path / "clones", pipelines_dir=tmp_path / "pipes")

        result = await mgr.create_clone(pid, repo_id, workspace_ref_name="pipeline-work")

        assert result.workspace_ref == "pipeline-work"
        # Verify the branch exists in the clone
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=result.clone_path, check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert branch == "pipeline-work"

    async def test_clone_updates_pipeline_row(
        self, initialized_db, source_repo: Path, tmp_path: Path
    ) -> None:
        """create_clone updates the pipeline DB row with clone metadata."""
        pool = get_pool()
        repo_id, pid = await _seed_repo_and_pipeline(pool, str(source_repo))
        mgr = CloneManager(pool, clones_dir=tmp_path / "clones", pipelines_dir=tmp_path / "pipes")

        result = await mgr.create_clone(pid, repo_id, workspace_ref_name="work")

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT clone_path, review_base_rev, workspace_ref, workspace_state "
                    "FROM pipelines WHERE id = %s",
                    (pid,),
                )
            ).fetchone()

        assert row["clone_path"] == str(result.clone_path)
        assert row["review_base_rev"] == result.review_base_rev
        assert row["workspace_ref"] == "work"
        assert row["workspace_state"] == "clean"

    async def test_clone_creates_pipeline_dirs(
        self, initialized_db, source_repo: Path, tmp_path: Path
    ) -> None:
        """create_clone creates the pipeline support directories."""
        pool = get_pool()
        repo_id, pid = await _seed_repo_and_pipeline(pool, str(source_repo))
        pipes_dir = tmp_path / "pipes"
        mgr = CloneManager(pool, clones_dir=tmp_path / "clones", pipelines_dir=pipes_dir)

        await mgr.create_clone(pid, repo_id)

        base = pipes_dir / str(pid)
        assert (base / "logs").is_dir()
        assert (base / "artifacts").is_dir()
        assert (base / "state").is_dir()

    async def test_clone_dir_already_exists_raises(
        self, initialized_db, source_repo: Path, tmp_path: Path
    ) -> None:
        """create_clone raises FileExistsError if the clone directory already exists."""
        pool = get_pool()
        repo_id, pid = await _seed_repo_and_pipeline(pool, str(source_repo))
        clones_dir = tmp_path / "clones"
        (clones_dir / str(pid)).mkdir(parents=True)
        mgr = CloneManager(pool, clones_dir=clones_dir, pipelines_dir=tmp_path / "pipes")

        with pytest.raises(FileExistsError):
            await mgr.create_clone(pid, repo_id)

    async def test_repo_not_found_raises(
        self, initialized_db, tmp_path: Path
    ) -> None:
        """create_clone raises ValueError if the repo_id doesn't exist."""
        pool = get_pool()
        mgr = CloneManager(pool, clones_dir=tmp_path / "clones", pipelines_dir=tmp_path / "pipes")

        with pytest.raises(ValueError, match="not found"):
            await mgr.create_clone(1, 99999)

    async def test_local_path_missing_no_git_url_raises(
        self, initialized_db, tmp_path: Path
    ) -> None:
        """create_clone raises ValueError when local_path doesn't exist and no git_url."""
        pool = get_pool()
        repo_id, pid = await _seed_repo_and_pipeline(
            pool, "/nonexistent/path/to/repo"
        )
        mgr = CloneManager(pool, clones_dir=tmp_path / "clones", pipelines_dir=tmp_path / "pipes")

        with pytest.raises(ValueError, match="does not exist"):
            await mgr.create_clone(pid, repo_id)


# ---------------------------------------------------------------------------
# Workspace operation tests
# ---------------------------------------------------------------------------


class TestWorkspaceOperations:
    """Tests for workspace query and manipulation methods."""

    async def test_get_current_rev(self, source_repo: Path) -> None:
        """get_current_rev returns a valid 40-char hex SHA."""
        mgr = CloneManager.__new__(CloneManager)  # no pool needed for pure git ops
        rev = await mgr.get_current_rev(source_repo)
        assert len(rev) == 40
        assert all(c in "0123456789abcdef" for c in rev)

    async def test_is_workspace_clean_on_fresh_repo(self, source_repo: Path) -> None:
        """A freshly committed repo workspace is clean."""
        mgr = CloneManager.__new__(CloneManager)
        assert await mgr.is_workspace_clean(source_repo) is True

    async def test_is_workspace_dirty_after_modification(self, source_repo: Path) -> None:
        """Modifying a tracked file makes the workspace dirty."""
        (source_repo / "README.md").write_text("modified\n")
        mgr = CloneManager.__new__(CloneManager)
        assert await mgr.is_workspace_clean(source_repo) is False

    async def test_is_workspace_dirty_with_untracked_file(self, source_repo: Path) -> None:
        """Adding an untracked file makes the workspace dirty."""
        (source_repo / "new_file.txt").write_text("new\n")
        mgr = CloneManager.__new__(CloneManager)
        assert await mgr.is_workspace_clean(source_repo) is False

    async def test_reset_to_rev_restores_clean_state(
        self, source_repo_with_two_commits: tuple[Path, str, str]
    ) -> None:
        """reset_to_rev restores workspace to the target revision and cleans it."""
        repo, rev1, rev2 = source_repo_with_two_commits
        mgr = CloneManager.__new__(CloneManager)

        # Dirty the workspace
        (repo / "README.md").write_text("dirty\n")
        (repo / "untracked.txt").write_text("junk\n")

        await mgr.reset_to_rev(repo, rev1)

        # Workspace should be clean at rev1
        assert await mgr.is_workspace_clean(repo) is True
        current = await mgr.get_current_rev(repo)
        assert current == rev1
        # extra.txt from rev2 should not exist
        assert not (repo / "extra.txt").exists()
        # untracked file should be gone
        assert not (repo / "untracked.txt").exists()

    async def test_reset_to_rev_forward(
        self, source_repo_with_two_commits: tuple[Path, str, str]
    ) -> None:
        """reset_to_rev can move forward to a newer revision."""
        repo, rev1, rev2 = source_repo_with_two_commits
        mgr = CloneManager.__new__(CloneManager)

        # Go back to rev1 first
        await mgr.reset_to_rev(repo, rev1)
        assert await mgr.get_current_rev(repo) == rev1

        # Now go forward to rev2
        await mgr.reset_to_rev(repo, rev2)
        assert await mgr.get_current_rev(repo) == rev2
        assert (repo / "extra.txt").exists()

    async def test_create_workspace_ref(self, source_repo: Path) -> None:
        """create_workspace_ref creates and checks out a new branch."""
        mgr = CloneManager.__new__(CloneManager)
        await mgr.create_workspace_ref(source_repo, "my-branch")

        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=source_repo, check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert branch == "my-branch"


# ---------------------------------------------------------------------------
# Dirty diff capture tests
# ---------------------------------------------------------------------------


class TestCaptureDirtyDiff:
    """Tests for capture_dirty_diff."""

    async def test_empty_when_clean(self, source_repo: Path) -> None:
        """capture_dirty_diff returns empty string when workspace matches baseline."""
        mgr = CloneManager.__new__(CloneManager)
        rev = await mgr.get_current_rev(source_repo)
        diff = await mgr.capture_dirty_diff(source_repo, rev)
        assert diff == ""

    async def test_nonempty_for_tracked_changes(self, source_repo: Path) -> None:
        """capture_dirty_diff captures tracked file modifications."""
        mgr = CloneManager.__new__(CloneManager)
        rev = await mgr.get_current_rev(source_repo)

        (source_repo / "README.md").write_text("changed content\n")
        diff = await mgr.capture_dirty_diff(source_repo, rev)

        assert "changed content" in diff
        assert len(diff) > 0

    async def test_captures_untracked_files(self, source_repo: Path) -> None:
        """capture_dirty_diff lists untracked files."""
        mgr = CloneManager.__new__(CloneManager)
        rev = await mgr.get_current_rev(source_repo)

        (source_repo / "new_file.py").write_text("print('hello')\n")
        diff = await mgr.capture_dirty_diff(source_repo, rev)

        assert "new_file.py" in diff
        assert "Untracked files" in diff


# ---------------------------------------------------------------------------
# Cleanup tests
# ---------------------------------------------------------------------------


class TestCleanup:
    """Tests for clone and bulk cleanup operations."""

    async def test_cleanup_clone_removes_directory(self, tmp_path: Path) -> None:
        """cleanup_clone removes the clone directory and returns True."""
        clones_dir = tmp_path / "clones"
        clone_path = clones_dir / "42"
        clone_path.mkdir(parents=True)
        (clone_path / "somefile.txt").write_text("data")

        mgr = CloneManager.__new__(CloneManager)
        mgr._clones_dir = clones_dir
        result = await mgr.cleanup_clone(42)

        assert result is True
        assert not clone_path.exists()

    async def test_cleanup_clone_nonexistent_returns_false(self, tmp_path: Path) -> None:
        """cleanup_clone returns False when no clone directory exists."""
        mgr = CloneManager.__new__(CloneManager)
        mgr._clones_dir = tmp_path / "clones"
        result = await mgr.cleanup_clone(999)
        assert result is False

    async def test_cleanup_clone_idempotent(self, tmp_path: Path) -> None:
        """Calling cleanup_clone twice doesn't raise — second call returns False."""
        clones_dir = tmp_path / "clones"
        (clones_dir / "1").mkdir(parents=True)

        mgr = CloneManager.__new__(CloneManager)
        mgr._clones_dir = clones_dir

        assert await mgr.cleanup_clone(1) is True
        assert await mgr.cleanup_clone(1) is False

    async def test_cleanup_completed_clones(
        self, initialized_db, source_repo: Path, tmp_path: Path
    ) -> None:
        """cleanup_completed_clones removes clones for terminal-status pipelines."""
        pool = get_pool()
        clones_dir = tmp_path / "clones"
        mgr = CloneManager(pool, clones_dir=clones_dir, pipelines_dir=tmp_path / "pipes")

        # Create two pipelines with clones, mark them completed/cancelled
        repo_id1, pid1 = await _seed_repo_and_pipeline(pool, str(source_repo))
        result1 = await mgr.create_clone(pid1, repo_id1)

        repo_id2, pid2 = await _seed_repo_and_pipeline(pool, str(source_repo))
        result2 = await mgr.create_clone(pid2, repo_id2)

        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET status = 'completed' WHERE id = %s", (pid1,)
            )
            await conn.execute(
                "UPDATE pipelines SET status = 'cancelled' WHERE id = %s", (pid2,)
            )
            await conn.commit()

        cleaned = await mgr.cleanup_completed_clones()

        assert set(cleaned) == {pid1, pid2}
        assert not result1.clone_path.exists()
        assert not result2.clone_path.exists()

    async def test_cleanup_completed_skips_running(
        self, initialized_db, source_repo: Path, tmp_path: Path
    ) -> None:
        """cleanup_completed_clones does not remove clones for running pipelines."""
        pool = get_pool()
        clones_dir = tmp_path / "clones"
        mgr = CloneManager(pool, clones_dir=clones_dir, pipelines_dir=tmp_path / "pipes")

        repo_id, pid = await _seed_repo_and_pipeline(pool, str(source_repo))
        result = await mgr.create_clone(pid, repo_id)

        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE pipelines SET status = 'running' WHERE id = %s", (pid,)
            )
            await conn.commit()

        cleaned = await mgr.cleanup_completed_clones()

        assert cleaned == []
        assert result.clone_path.exists()


# ---------------------------------------------------------------------------
# Pipeline directory tests
# ---------------------------------------------------------------------------


class TestEnsurePipelineDirs:
    """Tests for pipeline support directory creation."""

    async def test_creates_all_dirs(self, tmp_path: Path) -> None:
        """ensure_pipeline_dirs creates logs, artifacts, and state subdirectories."""
        mgr = CloneManager.__new__(CloneManager)
        mgr._pipelines_dir = tmp_path / "pipelines"

        dirs = await mgr.ensure_pipeline_dirs(7)

        assert dirs["logs"].is_dir()
        assert dirs["artifacts"].is_dir()
        assert dirs["state"].is_dir()
        assert "7" in str(dirs["logs"])

    async def test_idempotent(self, tmp_path: Path) -> None:
        """Calling ensure_pipeline_dirs twice doesn't raise."""
        mgr = CloneManager.__new__(CloneManager)
        mgr._pipelines_dir = tmp_path / "pipelines"

        dirs1 = await mgr.ensure_pipeline_dirs(7)
        dirs2 = await mgr.ensure_pipeline_dirs(7)

        assert dirs1 == dirs2
        assert dirs1["logs"].is_dir()
