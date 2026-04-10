import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch

from build_your_room.db import init_db
from build_your_room.main import app


@pytest.fixture
async def test_app(tmp_path):
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    # Create a real directory to use as local_path
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()
    with patch("build_your_room.routes.repos.DB_PATH", db_path):
        yield app, repo_dir


@pytest.fixture
async def client(test_app):
    test_app_instance, _ = test_app
    transport = ASGITransport(app=test_app_instance)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def repo_dir(test_app):
    _, repo_dir = test_app
    return repo_dir


@pytest.mark.asyncio
async def test_add_repo(client, repo_dir):
    """POST /repos creates a repo record for a local path."""
    resp = await client.post("/repos", data={
        "name": "my-project",
        "local_path": str(repo_dir),
    }, follow_redirects=False)
    assert resp.status_code == 303


@pytest.mark.asyncio
async def test_add_repo_nonexistent_path(client, tmp_path):
    """POST /repos with nonexistent path returns 400."""
    resp = await client.post("/repos", data={
        "name": "bad-project",
        "local_path": str(tmp_path / "does-not-exist"),
    }, follow_redirects=False)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_repo_detail(client, repo_dir):
    """GET /repos/{id} shows repo info."""
    await client.post("/repos", data={
        "name": "my-project",
        "local_path": str(repo_dir),
    }, follow_redirects=False)
    resp = await client.get("/repos/1")
    assert resp.status_code == 200
    assert "my-project" in resp.text


@pytest.mark.asyncio
async def test_archive_repo(client, repo_dir):
    """POST /repos/{id}/archive marks repo as archived."""
    await client.post("/repos", data={
        "name": "my-project",
        "local_path": str(repo_dir),
    }, follow_redirects=False)
    resp = await client.post("/repos/1/archive", follow_redirects=False)
    assert resp.status_code == 303
