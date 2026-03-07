import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch

from clean_room.db import init_db


@pytest.fixture
async def test_app(tmp_path):
    db_path = tmp_path / "test.db"
    repos_dir = tmp_path / "repos"
    specs_dir = tmp_path / "specs"
    repos_dir.mkdir()
    specs_dir.mkdir()
    await init_db(db_path)

    with patch("clean_room.config.DB_PATH", db_path), \
         patch("clean_room.config.REPOS_DIR", repos_dir), \
         patch("clean_room.config.SPECS_MONOREPO_DIR", specs_dir), \
         patch("clean_room.routes.dashboard.DB_PATH", db_path), \
         patch("clean_room.routes.repos.DB_PATH", db_path), \
         patch("clean_room.routes.repos.REPOS_DIR", repos_dir), \
         patch("clean_room.routes.prompts.DB_PATH", db_path), \
         patch("clean_room.routes.jobs.DB_PATH", db_path):
        from clean_room.main import app
        yield app


@pytest.fixture
async def client(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_full_navigation_flow(client):
    """Smoke test: all pages return 200."""
    assert (await client.get("/")).status_code == 200
    assert (await client.get("/prompts")).status_code == 200
    assert (await client.get("/repos/new")).status_code == 200
