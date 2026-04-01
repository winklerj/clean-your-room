import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch

from clean_room.db import init_db, get_db
from clean_room.main import app


@pytest.fixture
async def db_path(tmp_path):
    path = tmp_path / "test.db"
    await init_db(path)
    return path


@pytest.fixture
async def test_app(db_path):
    with patch("clean_room.routes.dashboard.DB_PATH", db_path), \
         patch("clean_room.main.DB_PATH", db_path):
        yield app


@pytest.fixture
async def client(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_repo(db_path, org="testorg", repo_name="testrepo"):
    """Insert an active repo and return its ID."""
    db = await get_db(db_path)
    try:
        cursor = await db.execute(
            "INSERT INTO repos (github_url, org, repo_name, slug, clone_path) "
            "VALUES (?, ?, ?, ?, ?) RETURNING id",
            (f"https://github.com/{org}/{repo_name}", org, repo_name,
             f"{org}-{repo_name}", f"/tmp/{org}-{repo_name}"),
        )
        row = await cursor.fetchone()
        await db.commit()
        return row[0]
    finally:
        await db.close()


async def _seed_job(db_path, repo_id, status="completed", iteration=10, max_iter=20):
    """Insert a job for a repo and return its ID."""
    db = await get_db(db_path)
    try:
        completed_at = "datetime('now')" if status in ("completed", "failed", "stopped") else "NULL"
        started_at = "datetime('now')" if status != "pending" else "NULL"
        cursor = await db.execute(
            f"INSERT INTO jobs (repo_id, prompt_id, status, current_iteration, "
            f"max_iterations, started_at, completed_at) "
            f"VALUES (?, 1, ?, ?, ?, {started_at}, {completed_at}) RETURNING id",
            (repo_id, status, iteration, max_iter),
        )
        row = await cursor.fetchone()
        await db.commit()
        return row[0]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_dashboard_empty(client):
    """Dashboard renders with no repos."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Clean Room" in resp.text
    assert "Add Repo" in resp.text
    assert "No repos yet" in resp.text


@pytest.mark.asyncio
async def test_dashboard_stats_cards(db_path, client):
    """Summary cards reflect actual job counts."""
    repo_id = await _seed_repo(db_path)
    await _seed_job(db_path, repo_id, status="completed")
    await _seed_job(db_path, repo_id, status="failed")
    await _seed_job(db_path, repo_id, status="completed")

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "stat-card-completed" in resp.text
    assert "stat-card-failed" in resp.text
    assert "stat-card-running" in resp.text


@pytest.mark.asyncio
async def test_dashboard_repo_with_completed_job(db_path, client):
    """Repo with a completed job shows status badge and spec freshness."""
    repo_id = await _seed_repo(db_path)
    await _seed_job(db_path, repo_id, status="completed", iteration=20, max_iter=20)

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "status-completed" in resp.text
    assert "20/20" in resp.text
    assert "no specs yet" not in resp.text


@pytest.mark.asyncio
async def test_dashboard_repo_no_jobs(db_path, client):
    """Repo with no jobs shows 'no jobs' badge and 'no specs yet'."""
    await _seed_repo(db_path)

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "no jobs" in resp.text
    assert "no specs yet" in resp.text
    assert "Run again" not in resp.text


@pytest.mark.asyncio
async def test_dashboard_run_again_button(db_path, client):
    """Run again button appears for repos with at least one prior job."""
    repo_id = await _seed_repo(db_path)
    job_id = await _seed_job(db_path, repo_id, status="completed")

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Run again" in resp.text
    assert f"/jobs/{job_id}/restart" in resp.text
