from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport

from build_your_room.db import get_pool
from build_your_room.main import app


@pytest.fixture
async def client(initialized_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_repo(name: str = "my-project", local_path: str = "/tmp/my-project") -> int:
    """Insert a repo and return its ID."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO repos (name, local_path) VALUES (%s, %s) RETURNING id",
            (name, local_path),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


@pytest.mark.asyncio
async def test_dashboard_empty(client):
    """Dashboard renders with no repos."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Build Your Room" in resp.text
    assert "Add Repo" in resp.text
    assert "No repos yet" in resp.text


@pytest.mark.asyncio
async def test_dashboard_with_repo(client):
    """Dashboard shows repos when they exist."""
    await _seed_repo(name="test-repo", local_path="/tmp/test-repo")
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "test-repo" in resp.text


@pytest.mark.asyncio
async def test_dashboard_hides_archived_repos(client):
    """Archived repos should not appear on the dashboard."""
    repo_id = await _seed_repo(name="archived-repo", local_path="/tmp/archived")
    pool = get_pool()
    async with pool.connection() as conn:
        await conn.execute("UPDATE repos SET archived=1 WHERE id=%s", (repo_id,))
        await conn.commit()
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "archived-repo" not in resp.text
