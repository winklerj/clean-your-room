import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch

from clean_room.db import init_db
from clean_room.main import app


@pytest.fixture
async def test_app(tmp_path):
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    with patch("clean_room.main.DB_PATH", db_path), \
         patch("clean_room.routes.prompts.DB_PATH", db_path):
        yield app


@pytest.fixture
async def client(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_list_prompts_returns_200(client):
    """GET /prompts returns 200 with seeded prompts."""
    resp = await client.get("/prompts")
    assert resp.status_code == 200
    assert "Create Spec" in resp.text
    assert "Improve Spec" in resp.text


@pytest.mark.asyncio
async def test_create_prompt(client):
    """POST /prompts creates a new prompt and returns partial."""
    resp = await client.post("/prompts", data={
        "name": "Test Prompt",
        "template": "Do the thing",
    })
    assert resp.status_code == 200
    assert "Test Prompt" in resp.text


@pytest.mark.asyncio
async def test_delete_prompt(client):
    """DELETE /prompts/{id} removes the prompt."""
    await client.post("/prompts", data={"name": "To Delete", "template": "temp"})
    resp = await client.delete("/prompts/3")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_update_prompt(client):
    """PUT /prompts/{id} updates name and template."""
    resp = await client.put("/prompts/1", data={
        "name": "Updated Name",
        "template": "Updated template",
    })
    assert resp.status_code == 200
    assert "Updated Name" in resp.text
