"""Tests for the extended prompt management routes."""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient, ASGITransport
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from build_your_room.main import app
from build_your_room.routes.prompts import extract_template_variables


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(initialized_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _create_prompt(client: AsyncClient, name: str, **kwargs) -> None:
    data = {
        "name": name,
        "body": kwargs.get("body", "Test body"),
        "stage_type": kwargs.get("stage_type", "custom"),
        "agent_type": kwargs.get("agent_type", "claude"),
    }
    resp = await client.post("/prompts", data=data)
    assert resp.status_code == 200


async def _create_pipeline_def(client: AsyncClient, name: str, prompt_name: str) -> None:
    """Create a pipeline def that references a prompt name."""
    from build_your_room.db import get_pool

    stage_graph = {
        "entry_stage": "s1",
        "nodes": [
            {
                "key": "s1",
                "name": "Stage 1",
                "type": "spec_author",
                "agent": "claude",
                "prompt": prompt_name,
                "model": "claude-sonnet-4-6",
                "max_iterations": 1,
            }
        ],
        "edges": [],
    }
    pool = get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO pipeline_defs (name, stage_graph_json) VALUES (%s, %s)",
            (name, json.dumps(stage_graph)),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Page rendering tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_prompts_returns_200(client):
    """GET /prompts returns 200 with seeded prompts and stat cards."""
    resp = await client.get("/prompts")
    assert resp.status_code == 200
    assert "spec_author_default" in resp.text
    assert "spec_review_default" in resp.text
    # Stat cards present
    assert "Total" in resp.text


@pytest.mark.asyncio
async def test_list_prompts_shows_agent_badges(client):
    """Prompt rows show agent type badges."""
    resp = await client.get("/prompts")
    assert resp.status_code == 200
    assert "claude" in resp.text
    assert "codex" in resp.text


@pytest.mark.asyncio
async def test_list_prompts_shows_stage_badges(client):
    """Prompt rows show stage type badges."""
    resp = await client.get("/prompts")
    assert resp.status_code == 200
    assert "spec author" in resp.text  # replaced underscore


# ---------------------------------------------------------------------------
# Filter tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_by_stage_type(client):
    """GET /prompts?stage_type=spec_author only returns spec_author prompts."""
    resp = await client.get("/prompts?stage_type=spec_author")
    assert resp.status_code == 200
    assert "spec_author_default" in resp.text
    # Codex review prompts should not appear
    assert "code_review_default" not in resp.text


@pytest.mark.asyncio
async def test_filter_by_agent_type(client):
    """GET /prompts?agent_type=codex only returns codex prompts."""
    resp = await client.get("/prompts?agent_type=codex")
    assert resp.status_code == 200
    assert "spec_review_default" in resp.text
    # Claude prompts should not appear
    assert "spec_author_default" not in resp.text


@pytest.mark.asyncio
async def test_filter_combined(client):
    """Combined stage_type + agent_type filter."""
    resp = await client.get("/prompts?stage_type=code_review&agent_type=codex")
    assert resp.status_code == 200
    assert "code_review_default" in resp.text
    assert "spec_author_default" not in resp.text


@pytest.mark.asyncio
async def test_filter_no_results(client):
    """Filter that matches nothing shows empty state."""
    resp = await client.get("/prompts?stage_type=validation&agent_type=codex")
    assert resp.status_code == 200
    assert "No prompts found" in resp.text


# ---------------------------------------------------------------------------
# CRUD tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_prompt(client):
    """POST /prompts creates a new prompt and returns partial."""
    resp = await client.post("/prompts", data={
        "name": "test_new_prompt",
        "body": "Do the thing with {{repo_name}}",
        "stage_type": "custom",
        "agent_type": "claude",
    })
    assert resp.status_code == 200
    assert "test_new_prompt" in resp.text
    # Variable badge rendered
    assert "repo_name" in resp.text


@pytest.mark.asyncio
async def test_create_duplicate_name_returns_422(client):
    """POST /prompts with duplicate name returns 422 with error message."""
    await _create_prompt(client, "dup_test_prompt")
    resp = await client.post("/prompts", data={
        "name": "dup_test_prompt",
        "body": "Another body",
        "stage_type": "custom",
        "agent_type": "claude",
    })
    assert resp.status_code == 422
    assert "already exists" in resp.text


@pytest.mark.asyncio
async def test_update_prompt(client):
    """PUT /prompts/{id} updates name and body."""
    resp = await client.put("/prompts/1", data={
        "name": "Updated Name",
        "body": "Updated body with {{var1}}",
        "stage_type": "impl_task",
        "agent_type": "codex",
    })
    assert resp.status_code == 200
    assert "Updated Name" in resp.text
    assert "var1" in resp.text


@pytest.mark.asyncio
async def test_update_duplicate_name_returns_422(client):
    """PUT /prompts/{id} with name collision returns 422."""
    # spec_author_default and spec_review_default are seeded
    resp = await client.put("/prompts/1", data={
        "name": "spec_review_default",
        "body": "Some body",
        "stage_type": "custom",
        "agent_type": "claude",
    })
    assert resp.status_code == 422
    assert "already exists" in resp.text


@pytest.mark.asyncio
async def test_delete_prompt(client):
    """DELETE /prompts/{id} removes an unused prompt."""
    await _create_prompt(client, "to_delete_prompt")
    # Find the id
    from build_your_room.db import get_pool

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM prompts WHERE name='to_delete_prompt'"
        )
        row = await cur.fetchone()
    prompt_id = row["id"]  # type: ignore[index]

    resp = await client.delete(f"/prompts/{prompt_id}")
    assert resp.status_code == 200
    assert resp.text == ""


@pytest.mark.asyncio
async def test_delete_nonexistent_prompt(client):
    """DELETE /prompts/{id} for missing prompt returns empty 200."""
    resp = await client.delete("/prompts/99999")
    assert resp.status_code == 200
    assert resp.text == ""


# ---------------------------------------------------------------------------
# Delete protection tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_in_use_prompt_returns_409(client):
    """DELETE /prompts/{id} for a prompt used by a pipeline def returns 409."""
    await _create_prompt(client, "protected_prompt")
    await _create_pipeline_def(client, "test_def_uses_protected", "protected_prompt")

    from build_your_room.db import get_pool

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM prompts WHERE name='protected_prompt'"
        )
        row = await cur.fetchone()
    prompt_id = row["id"]  # type: ignore[index]

    resp = await client.delete(f"/prompts/{prompt_id}")
    assert resp.status_code == 409
    assert "Cannot delete" in resp.text
    assert "test_def_uses_protected" in resp.text


@pytest.mark.asyncio
async def test_delete_protection_fix_prompt(client):
    """Prompt referenced as fix_prompt in a pipeline def is protected."""
    await _create_prompt(client, "fix_protected")

    from build_your_room.db import get_pool

    stage_graph = {
        "entry_stage": "s1",
        "nodes": [{
            "key": "s1", "name": "S1", "type": "code_review",
            "agent": "codex", "prompt": "other_prompt",
            "model": "gpt-5.1-codex", "max_iterations": 1,
            "fix_prompt": "fix_protected",
        }],
        "edges": [],
    }
    pool = get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO pipeline_defs (name, stage_graph_json) VALUES (%s, %s)",
            ("def_with_fix", json.dumps(stage_graph)),
        )
        await conn.commit()
        cur = await conn.execute(
            "SELECT id FROM prompts WHERE name='fix_protected'"
        )
        row = await cur.fetchone()
    prompt_id = row["id"]  # type: ignore[index]

    resp = await client.delete(f"/prompts/{prompt_id}")
    assert resp.status_code == 409
    assert "Cannot delete" in resp.text


@pytest.mark.asyncio
async def test_delete_protection_review_prompt(client):
    """Prompt referenced as review.prompt in a pipeline def is protected."""
    await _create_prompt(client, "review_protected")

    from build_your_room.db import get_pool

    stage_graph = {
        "entry_stage": "s1",
        "nodes": [{
            "key": "s1", "name": "S1", "type": "spec_author",
            "agent": "claude", "prompt": "other_prompt",
            "model": "claude-sonnet-4-6", "max_iterations": 1,
            "review": {
                "agent": "codex", "prompt": "review_protected",
                "model": "gpt-5.1-codex", "max_review_rounds": 3,
                "exit_condition": "structured_approval",
                "on_max_rounds": "escalate",
            },
        }],
        "edges": [],
    }
    pool = get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO pipeline_defs (name, stage_graph_json) VALUES (%s, %s)",
            ("def_with_review", json.dumps(stage_graph)),
        )
        await conn.commit()
        cur = await conn.execute(
            "SELECT id FROM prompts WHERE name='review_protected'"
        )
        row = await cur.fetchone()
    prompt_id = row["id"]  # type: ignore[index]

    resp = await client.delete(f"/prompts/{prompt_id}")
    assert resp.status_code == 409
    assert "Cannot delete" in resp.text


# ---------------------------------------------------------------------------
# Clone tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clone_prompt(client):
    """POST /prompts/{id}/clone duplicates a prompt with _copy suffix."""
    await _create_prompt(client, "original_prompt", body="Hello {{world}}")

    from build_your_room.db import get_pool

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM prompts WHERE name='original_prompt'"
        )
        row = await cur.fetchone()
    prompt_id = row["id"]  # type: ignore[index]

    resp = await client.post(f"/prompts/{prompt_id}/clone")
    assert resp.status_code == 200
    assert "original_prompt_copy" in resp.text
    assert "world" in resp.text  # variable badge preserved


@pytest.mark.asyncio
async def test_clone_prompt_increments_suffix(client):
    """Cloning when _copy already exists appends _copy_2, _copy_3, etc."""
    await _create_prompt(client, "inc_prompt", body="Body")
    await _create_prompt(client, "inc_prompt_copy", body="Body")

    from build_your_room.db import get_pool

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM prompts WHERE name='inc_prompt'"
        )
        row = await cur.fetchone()
    prompt_id = row["id"]  # type: ignore[index]

    resp = await client.post(f"/prompts/{prompt_id}/clone")
    assert resp.status_code == 200
    assert "inc_prompt_copy_2" in resp.text


@pytest.mark.asyncio
async def test_clone_nonexistent_returns_404(client):
    """POST /prompts/99999/clone returns 404."""
    resp = await client.post("/prompts/99999/clone")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Usage tracking tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_tracking_shows_def_count(client):
    """Prompt used by a pipeline def shows usage count."""
    await _create_pipeline_def(
        client, "usage_test_def", "spec_author_default"
    )
    resp = await client.get("/prompts")
    assert resp.status_code == 200
    # Should show "1 def" for spec_author_default
    assert "1 def" in resp.text


@pytest.mark.asyncio
async def test_usage_tracking_unused_prompt(client):
    """Prompt not used shows 'unused'."""
    await _create_prompt(client, "lonely_prompt")
    resp = await client.get("/prompts")
    assert resp.status_code == 200
    assert "unused" in resp.text


# ---------------------------------------------------------------------------
# Edit form tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_form_renders(client):
    """GET /prompts/{id}/edit returns edit form partial."""
    resp = await client.get("/prompts/1/edit")
    assert resp.status_code == 200
    assert "Save" in resp.text
    assert "Cancel" in resp.text
    # Stage types dropdown
    assert "spec_author" in resp.text


@pytest.mark.asyncio
async def test_prompt_row_renders(client):
    """GET /prompts/{id}/row returns row partial."""
    resp = await client.get("/prompts/1/row")
    assert resp.status_code == 200
    assert "spec_author_default" in resp.text


# ---------------------------------------------------------------------------
# Template variable extraction (unit tests)
# ---------------------------------------------------------------------------


def test_extract_variables_basic():
    """Extract simple {{var}} templates."""
    body = "Hello {{name}}, welcome to {{project}}"
    assert extract_template_variables(body) == ["name", "project"]


def test_extract_variables_deduplicates():
    """Duplicate variables only listed once."""
    body = "{{x}} then {{y}} then {{x}}"
    assert extract_template_variables(body) == ["x", "y"]


def test_extract_variables_none():
    """Body with no variables returns empty list."""
    assert extract_template_variables("No variables here") == []


def test_extract_variables_nested_braces():
    """Only double-brace patterns match, not single braces."""
    body = "{single} {{double}} {{{triple}}}"
    # {{double}} matches, {{{triple}}} contains {{triple}} inside
    result = extract_template_variables(body)
    assert "double" in result
    assert "single" not in result


def test_extract_variables_multiline():
    """Variables extracted from multiline body."""
    body = "Line 1: {{var1}}\nLine 2: {{var2}}\nLine 3: {{var1}}"
    assert extract_template_variables(body) == ["var1", "var2"]


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


@given(body=st.text(
    alphabet=st.characters(exclude_characters="\r"),
    min_size=0,
    max_size=200,
))
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_extract_variables_never_raises(body):
    """extract_template_variables never raises on arbitrary input."""
    result = extract_template_variables(body)
    assert isinstance(result, list)
    for v in result:
        assert isinstance(v, str)
        assert len(v) > 0


@given(
    var_names=st.lists(
        st.from_regex(r"[a-zA-Z_]\w{0,19}", fullmatch=True),
        min_size=1,
        max_size=5,
    )
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_extract_variables_roundtrip(var_names):
    """Variables embedded as {{name}} are always extracted."""
    body = " ".join(f"{{{{{v}}}}}" for v in var_names)
    result = extract_template_variables(body)
    # All unique names should be found
    unique_names = list(dict.fromkeys(var_names))
    assert result == unique_names


@given(stage=st.sampled_from([
    "spec_author", "spec_review", "impl_plan", "impl_plan_review",
    "impl_task", "code_review", "bug_fix", "validation", "custom",
]))
@settings(max_examples=9, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_stage_types_all_valid(stage):
    """All stage types from the constants list are valid filter values."""
    from build_your_room.routes.prompts import STAGE_TYPES
    assert stage in STAGE_TYPES
