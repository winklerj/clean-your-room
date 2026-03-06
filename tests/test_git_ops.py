import pytest
import subprocess
from pathlib import Path

from clean_room.git_ops import clone_repo, pull_repo, init_specs_monorepo, commit_specs


@pytest.fixture
def fake_remote(tmp_path):
    """Create a bare git repo to act as a fake GitHub remote."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(remote), str(work)], check=True, capture_output=True)
    (work / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=work, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@test.com",
         "commit", "-m", "init"],
        cwd=work, check=True, capture_output=True,
    )
    subprocess.run(["git", "push"], cwd=work, check=True, capture_output=True)
    return remote


@pytest.mark.asyncio
async def test_clone_repo(fake_remote, tmp_path):
    """Clone creates a local copy with files from the remote."""
    dest = tmp_path / "clone"
    await clone_repo(str(fake_remote), dest)
    assert (dest / "README.md").exists()
    assert (dest / "README.md").read_text() == "hello"


@pytest.mark.asyncio
async def test_pull_repo(fake_remote, tmp_path):
    """Pull fetches latest changes into existing clone."""
    dest = tmp_path / "clone"
    await clone_repo(str(fake_remote), dest)
    work = tmp_path / "work2"
    subprocess.run(["git", "clone", str(fake_remote), str(work)], check=True, capture_output=True)
    (work / "new.txt").write_text("new")
    subprocess.run(["git", "add", "."], cwd=work, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@test.com",
         "commit", "-m", "add new"],
        cwd=work, check=True, capture_output=True,
    )
    subprocess.run(["git", "push"], cwd=work, check=True, capture_output=True)
    await pull_repo(dest)
    assert (dest / "new.txt").exists()


@pytest.mark.asyncio
async def test_init_specs_monorepo(tmp_path):
    """Init creates a git repo for specs if it doesn't exist."""
    mono = tmp_path / "specs"
    await init_specs_monorepo(mono)
    assert (mono / ".git").is_dir()
    await init_specs_monorepo(mono)
    assert (mono / ".git").is_dir()


@pytest.mark.asyncio
async def test_commit_specs(tmp_path):
    """Commit stages and commits all changes in the specs monorepo."""
    mono = tmp_path / "specs"
    await init_specs_monorepo(mono)
    slug_dir = mono / "org--repo"
    slug_dir.mkdir()
    (slug_dir / "spec-001.md").write_text("# Spec 1")
    await commit_specs(mono, "Add specs for org/repo")
    result = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=mono, capture_output=True, text=True,
    )
    assert "Add specs for org/repo" in result.stdout
