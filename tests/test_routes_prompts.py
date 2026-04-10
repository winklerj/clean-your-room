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
async def test_list_prompts_returns_200(client):
    """GET /prompts returns 200 with seeded prompts."""
    resp = await client.get("/prompts")
    assert resp.status_code == 200
    assert "spec_author_default" in resp.text
    assert "spec_review_default" in resp.text


@pytest.mark.asyncio
async def test_create_prompt(client):
    """POST /prompts creates a new prompt and returns partial."""
    resp = await client.post("/prompts", data={
        "name": "Test Prompt",
        "body": "Do the thing",
        "stage_type": "custom",
        "agent_type": "claude",
    })
    assert resp.status_code == 200
    assert "Test Prompt" in resp.text


@pytest.mark.asyncio
async def test_delete_prompt(client):
    """DELETE /prompts/{id} removes the prompt."""
    await client.post("/prompts", data={
        "name": "To Delete", "body": "temp",
        "stage_type": "custom", "agent_type": "claude",
    })
    resp = await client.delete("/prompts/5")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_update_prompt(client):
    """PUT /prompts/{id} updates name and body."""
    resp = await client.put("/prompts/1", data={
        "name": "Updated Name",
        "body": "Updated body",
        "stage_type": "impl_task",
        "agent_type": "codex",
    })
    assert resp.status_code == 200
    assert "Updated Name" in resp.text
