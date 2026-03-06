import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, AsyncMock

from clean_room.db import init_db
from clean_room.main import app


@pytest.fixture
async def test_app(tmp_path):
    db_path = tmp_path / "test.db"
    repos_dir = tmp_path / "repos"
    repos_dir.mkdir()
    await init_db(db_path)
    with patch("clean_room.routes.repos.DB_PATH", db_path), \
         patch("clean_room.routes.repos.REPOS_DIR", repos_dir), \
         patch("clean_room.routes.repos.clone_repo", new_callable=AsyncMock):
        yield app


@pytest.fixture
async def client(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_add_repo(client):
    """POST /repos creates a repo record and triggers clone."""
    resp = await client.post("/repos", data={
        "github_url": "https://github.com/anthropics/claude-code",
    }, follow_redirects=False)
    assert resp.status_code == 303


@pytest.mark.asyncio
async def test_repo_detail(client):
    """GET /repos/{id} shows repo info and jobs list."""
    await client.post("/repos", data={
        "github_url": "https://github.com/anthropics/claude-code",
    }, follow_redirects=False)
    resp = await client.get("/repos/1")
    assert resp.status_code == 200
    assert "anthropics" in resp.text
    assert "claude-code" in resp.text


@pytest.mark.asyncio
async def test_archive_repo(client):
    """POST /repos/{id}/archive sets status to archived."""
    await client.post("/repos", data={
        "github_url": "https://github.com/anthropics/claude-code",
    }, follow_redirects=False)
    resp = await client.post("/repos/1/archive", follow_redirects=False)
    assert resp.status_code == 303
