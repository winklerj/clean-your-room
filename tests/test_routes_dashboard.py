import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch

from clean_room.db import init_db
from clean_room.main import app


@pytest.fixture
async def test_app(tmp_path):
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    with patch("clean_room.routes.dashboard.DB_PATH", db_path), \
         patch("clean_room.main.DB_PATH", db_path):
        yield app


@pytest.fixture
async def client(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_dashboard_empty(client):
    """Dashboard renders with no repos."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Clean Room" in resp.text
    assert "Add Repo" in resp.text
