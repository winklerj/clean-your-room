from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport

from build_your_room.main import app


@pytest.fixture
async def client(initialized_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_add_repo(client, tmp_path):
    """POST /repos creates a repo record for a local path."""
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()
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
async def test_repo_detail(client, tmp_path):
    """GET /repos/{id} shows repo info."""
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()
    await client.post("/repos", data={
        "name": "my-project",
        "local_path": str(repo_dir),
    }, follow_redirects=False)
    resp = await client.get("/repos/1")
    assert resp.status_code == 200
    assert "my-project" in resp.text


@pytest.mark.asyncio
async def test_archive_repo(client, tmp_path):
    """POST /repos/{id}/archive marks repo as archived."""
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()
    await client.post("/repos", data={
        "name": "my-project",
        "local_path": str(repo_dir),
    }, follow_redirects=False)
    resp = await client.post("/repos/1/archive", follow_redirects=False)
    assert resp.status_code == 303
