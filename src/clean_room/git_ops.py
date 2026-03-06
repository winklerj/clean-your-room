import asyncio
from pathlib import Path


async def _run(cmd: list[str], cwd: Path | None = None) -> str:
    """Run a subprocess command asynchronously."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{stderr.decode()}")
    return stdout.decode()


async def clone_repo(url: str, dest: Path) -> None:
    """Clone a git repository to dest."""
    await _run(["git", "clone", url, str(dest)])


async def pull_repo(repo_path: Path) -> None:
    """Pull latest changes in an existing clone."""
    await _run(["git", "pull"], cwd=repo_path)


async def init_specs_monorepo(path: Path) -> None:
    """Initialize a git repo for the specs monorepo if it doesn't exist."""
    if not (path / ".git").is_dir():
        path.mkdir(parents=True, exist_ok=True)
        await _run(["git", "init"], cwd=path)
        await _run(["git", "-c", "user.name=clean-room", "-c",
                     "user.email=clean-room@local", "commit",
                     "--allow-empty", "-m", "init specs monorepo"], cwd=path)


async def commit_specs(monorepo_path: Path, message: str) -> None:
    """Stage all changes and commit to the specs monorepo."""
    await _run(["git", "add", "."], cwd=monorepo_path)
    try:
        await _run(["git", "diff", "--cached", "--quiet"], cwd=monorepo_path)
        return  # Nothing to commit
    except RuntimeError:
        pass  # There are changes
    await _run(
        ["git", "-c", "user.name=clean-room", "-c", "user.email=clean-room@local",
         "commit", "-m", message],
        cwd=monorepo_path,
    )
