import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch
import aiosqlite

from clean_room.db import init_db
from clean_room.main import app


@pytest.fixture
async def test_app(tmp_path):
    db_path = tmp_path / "test.db"
    repos_dir = tmp_path / "repos"
    repos_dir.mkdir()
    await init_db(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO repos (github_url, org, repo_name, slug, clone_path) "
            "VALUES (?, ?, ?, ?, ?)",
            ("https://github.com/test/repo", "test", "repo", "test--repo",
             str(repos_dir / "test--repo")),
        )
        await db.commit()
    with patch("clean_room.routes.jobs.DB_PATH", db_path), \
         patch("clean_room.routes.repos.DB_PATH", db_path), \
         patch("clean_room.routes.prompts.DB_PATH", db_path), \
         patch("clean_room.main.DB_PATH", db_path):
        yield app


@pytest.fixture
async def client(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_create_job(client):
    """POST /jobs creates a job and redirects to viewer."""
    resp = await client.post("/jobs", data={
        "repo_id": "1",
        "prompt_id": "1",
        "feature_description": "auth system",
        "max_iterations": "5",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "/jobs/" in resp.headers["location"]


@pytest.mark.asyncio
async def test_job_viewer(client):
    """GET /jobs/{id} returns the job viewer page."""
    await client.post("/jobs", data={
        "repo_id": "1",
        "prompt_id": "1",
        "max_iterations": "5",
    }, follow_redirects=False)
    resp = await client.get("/jobs/1")
    assert resp.status_code == 200
