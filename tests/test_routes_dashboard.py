import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch

from build_your_room.db import init_db, get_db
from build_your_room.main import app


@pytest.fixture
async def db_path(tmp_path):
    path = tmp_path / "test.db"
    await init_db(path)
    return path


@pytest.fixture
async def test_app(db_path):
    with patch("build_your_room.routes.dashboard.DB_PATH", db_path), \
         patch("build_your_room.main.DB_PATH", db_path):
        yield app


@pytest.fixture
async def client(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_repo(db_path, name="my-project", local_path="/tmp/my-project"):
    """Insert a repo and return its ID."""
    db = await get_db(db_path)
    try:
        cursor = await db.execute(
            "INSERT INTO repos (name, local_path) VALUES (?, ?) RETURNING id",
            (name, local_path),
        )
        row = await cursor.fetchone()
        assert row is not None
        await db.commit()
        return row[0]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_dashboard_empty(client):
    """Dashboard renders with no repos."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Build Your Room" in resp.text
    assert "Add Repo" in resp.text
    assert "No repos yet" in resp.text


@pytest.mark.asyncio
async def test_dashboard_with_repo(db_path, client):
    """Dashboard shows repos when they exist."""
    await _seed_repo(db_path, name="test-repo", local_path="/tmp/test-repo")
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "test-repo" in resp.text


@pytest.mark.asyncio
async def test_dashboard_hides_archived_repos(db_path, client):
    """Archived repos should not appear on the dashboard."""
    repo_id = await _seed_repo(db_path, name="archived-repo", local_path="/tmp/archived")
    db = await get_db(db_path)
    try:
        await db.execute("UPDATE repos SET archived=1 WHERE id=?", (repo_id,))
        await db.commit()
    finally:
        await db.close()
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "archived-repo" not in resp.text
