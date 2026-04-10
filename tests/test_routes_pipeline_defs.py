"""Tests for the pipeline definition builder — list, create, validate."""

from __future__ import annotations

import json
import uuid

import pytest
from hypothesis import HealthCheck, given, settings as hyp_settings
from hypothesis import strategies as st
from httpx import ASGITransport, AsyncClient

from build_your_room.db import get_pool
from build_your_room.main import app
from build_your_room.routes.pipeline_defs import (
    _parse_edges_from_form,
    _parse_nodes_from_form,
)


@pytest.fixture
async def client(initialized_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_pipeline_def(
    name: str | None = None,
    entry: str = "spec_author",
) -> int:
    """Insert a minimal pipeline def and return its id."""
    pool = get_pool()
    if name is None:
        name = f"def-{uuid.uuid4().hex[:8]}"
    graph = json.dumps({
        "entry_stage": entry,
        "nodes": [
            {
                "key": entry,
                "name": "Spec",
                "type": "spec_author",
                "agent": "claude",
                "prompt": "spec_author_default",
                "model": "claude-sonnet-4-6",
                "max_iterations": 1,
            }
        ],
        "edges": [],
    })
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipeline_defs (name, stage_graph_json) "
            "VALUES (%s, %s) RETURNING id",
            (name, graph),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
        return row["id"]


def _minimal_node_form(idx: int = 0, key: str = "stage_a") -> dict[str, str]:
    """Return the minimum form fields to define one valid node."""
    return {
        f"node_{idx}_key": key,
        f"node_{idx}_name": "Stage A",
        f"node_{idx}_type": "spec_author",
        f"node_{idx}_agent": "claude",
        f"node_{idx}_prompt": "spec_author_default",
        f"node_{idx}_model": "claude-sonnet-4-6",
        f"node_{idx}_max_iterations": "1",
        f"node_{idx}_context_threshold_pct": "60",
    }


def _minimal_edge_form(
    idx: int = 0,
    key: str = "e1",
    from_stage: str = "stage_a",
    to_stage: str = "completed",
    on: str = "approved",
) -> dict[str, str]:
    """Return the minimum form fields to define one valid edge."""
    return {
        f"edge_{idx}_key": key,
        f"edge_{idx}_from": from_stage,
        f"edge_{idx}_to": to_stage,
        f"edge_{idx}_on": on,
    }


# ---------------------------------------------------------------------------
# List page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_page_empty(client):
    """GET /pipeline-defs returns 200 with builder form when no defs exist."""
    resp = await client.get("/pipeline-defs")
    assert resp.status_code == 200
    assert "Pipeline Definitions" in resp.text
    assert "Create Pipeline Definition" in resp.text


@pytest.mark.asyncio
async def test_list_page_shows_defs(client):
    """GET /pipeline-defs lists existing definitions with node/edge counts."""
    await _seed_pipeline_def(name="my-pipeline")
    resp = await client.get("/pipeline-defs")
    assert resp.status_code == 200
    assert "my-pipeline" in resp.text
    assert "1 stages" in resp.text
    assert "0 edges" in resp.text


@pytest.mark.asyncio
async def test_list_shows_multiple_defs(client):
    """Multiple pipeline definitions all appear on the list page."""
    await _seed_pipeline_def(name="pipeline-alpha")
    await _seed_pipeline_def(name="pipeline-beta")
    resp = await client.get("/pipeline-defs")
    assert resp.status_code == 200
    assert "pipeline-alpha" in resp.text
    assert "pipeline-beta" in resp.text


# ---------------------------------------------------------------------------
# Builder form
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_builder_form_renders(client):
    """GET /pipeline-defs/new returns the builder form."""
    resp = await client.get("/pipeline-defs/new")
    assert resp.status_code == 200
    assert "Create Pipeline Definition" in resp.text
    assert "Stage Nodes" in resp.text
    assert "Transition Edges" in resp.text


@pytest.mark.asyncio
async def test_builder_form_includes_prompt_datalist(client):
    """Builder form includes prompt names in a datalist for autocomplete."""
    resp = await client.get("/pipeline-defs/new")
    assert resp.status_code == 200
    assert "prompt-names" in resp.text
    assert "spec_author_default" in resp.text


@pytest.mark.asyncio
async def test_builder_form_includes_stage_types(client):
    """Builder form select options include all stage types."""
    resp = await client.get("/pipeline-defs/new")
    assert resp.status_code == 200
    for st_name in ["spec_author", "impl_plan", "impl_task", "code_review", "validation"]:
        assert st_name in resp.text


# ---------------------------------------------------------------------------
# Create pipeline definition — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_valid_single_node(client):
    """POST /pipeline-defs with a valid single-node graph redirects to list."""
    form = {
        "name": "simple-def",
        "entry_stage": "stage_a",
        **_minimal_node_form(0, "stage_a"),
    }
    resp = await client.post("/pipeline-defs", data=form, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/pipeline-defs"

    # Verify it appears on the list
    list_resp = await client.get("/pipeline-defs")
    assert "simple-def" in list_resp.text


@pytest.mark.asyncio
async def test_create_multi_node_with_edges(client):
    """POST /pipeline-defs with multiple nodes and edges creates a valid def."""
    form = {
        "name": "full-pipeline",
        "entry_stage": "spec",
        **_minimal_node_form(0, "spec"),
        **{
            "node_1_key": "impl",
            "node_1_name": "Implementation",
            "node_1_type": "impl_task",
            "node_1_agent": "claude",
            "node_1_prompt": "impl_plan_default",
            "node_1_model": "claude-sonnet-4-6",
            "node_1_max_iterations": "10",
            "node_1_context_threshold_pct": "60",
        },
        **_minimal_edge_form(0, "spec_to_impl", "spec", "impl", "approved"),
        **_minimal_edge_form(1, "impl_done", "impl", "completed", "stage_complete"),
    }
    resp = await client.post("/pipeline-defs", data=form, follow_redirects=False)
    assert resp.status_code == 303

    # Verify stored JSON
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT stage_graph_json FROM pipeline_defs WHERE name = %s",
            ("full-pipeline",),
        )
        row = await cur.fetchone()
    assert row is not None
    graph = json.loads(row["stage_graph_json"])
    assert graph["entry_stage"] == "spec"
    assert len(graph["nodes"]) == 2
    assert len(graph["edges"]) == 2


@pytest.mark.asyncio
async def test_create_with_review_config(client):
    """POST /pipeline-defs with review sub-config stores it correctly."""
    form = {
        "name": "review-def",
        "entry_stage": "spec",
        **_minimal_node_form(0, "spec"),
        "node_0_review_agent": "codex",
        "node_0_review_prompt": "spec_review_default",
        "node_0_review_model": "gpt-5.1-codex",
        "node_0_review_max_rounds": "3",
        "node_0_review_exit_condition": "structured_approval",
        "node_0_review_on_max_rounds": "escalate",
    }
    resp = await client.post("/pipeline-defs", data=form, follow_redirects=False)
    assert resp.status_code == 303

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT stage_graph_json FROM pipeline_defs WHERE name = %s",
            ("review-def",),
        )
        row = await cur.fetchone()
    graph = json.loads(row["stage_graph_json"])
    review = graph["nodes"][0]["review"]
    assert review["agent"] == "codex"
    assert review["max_review_rounds"] == 3


@pytest.mark.asyncio
async def test_create_with_edge_max_visits(client):
    """Edges with max_visits and on_exhausted are stored correctly."""
    form = {
        "name": "loop-def",
        "entry_stage": "review",
        "node_0_key": "review",
        "node_0_name": "Review",
        "node_0_type": "code_review",
        "node_0_agent": "codex",
        "node_0_prompt": "code_review_default",
        "node_0_model": "gpt-5.1-codex",
        "node_0_max_iterations": "3",
        "node_0_context_threshold_pct": "60",
        "node_1_key": "validate",
        "node_1_name": "Validate",
        "node_1_type": "validation",
        "node_1_agent": "claude",
        "node_1_prompt": "validation_default",
        "node_1_model": "claude-sonnet-4-6",
        "node_1_max_iterations": "3",
        "node_1_context_threshold_pct": "60",
        "edge_0_key": "review_to_validate",
        "edge_0_from": "review",
        "edge_0_to": "validate",
        "edge_0_on": "approved",
        "edge_1_key": "validate_back",
        "edge_1_from": "validate",
        "edge_1_to": "review",
        "edge_1_on": "validation_failed",
        "edge_1_max_visits": "3",
        "edge_1_on_exhausted": "escalate",
    }
    resp = await client.post("/pipeline-defs", data=form, follow_redirects=False)
    assert resp.status_code == 303

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT stage_graph_json FROM pipeline_defs WHERE name = %s",
            ("loop-def",),
        )
        row = await cur.fetchone()
    graph = json.loads(row["stage_graph_json"])
    back_edge = [e for e in graph["edges"] if e["key"] == "validate_back"][0]
    assert back_edge["max_visits"] == 3
    assert back_edge["on_exhausted"] == "escalate"


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_missing_name(client):
    """POST with empty name returns 422 with error message."""
    form = {
        "name": "",
        "entry_stage": "stage_a",
        **_minimal_node_form(0, "stage_a"),
    }
    resp = await client.post("/pipeline-defs", data=form)
    assert resp.status_code == 422
    assert "name is required" in resp.text


@pytest.mark.asyncio
async def test_create_missing_entry_stage(client):
    """POST with empty entry_stage returns 422."""
    form = {
        "name": "no-entry",
        "entry_stage": "",
        **_minimal_node_form(0, "stage_a"),
    }
    resp = await client.post("/pipeline-defs", data=form)
    assert resp.status_code == 422
    assert "Entry stage is required" in resp.text


@pytest.mark.asyncio
async def test_create_no_nodes(client):
    """POST with no node fields returns 422."""
    form = {"name": "empty-def", "entry_stage": "missing"}
    resp = await client.post("/pipeline-defs", data=form)
    assert resp.status_code == 422
    assert "At least one stage node" in resp.text


@pytest.mark.asyncio
async def test_create_invalid_graph_bad_entry(client):
    """POST where entry_stage doesn't match any node key returns 422."""
    form = {
        "name": "bad-entry",
        "entry_stage": "nonexistent",
        **_minimal_node_form(0, "stage_a"),
    }
    resp = await client.post("/pipeline-defs", data=form)
    assert resp.status_code == 422
    assert "not found in nodes" in resp.text


@pytest.mark.asyncio
async def test_create_invalid_graph_bad_edge_from(client):
    """POST with edge referencing unknown from_stage returns 422."""
    form = {
        "name": "bad-edge",
        "entry_stage": "stage_a",
        **_minimal_node_form(0, "stage_a"),
        **_minimal_edge_form(0, "bad_e", "unknown_stage", "stage_a", "approved"),
    }
    resp = await client.post("/pipeline-defs", data=form)
    assert resp.status_code == 422
    assert "unknown from_stage" in resp.text


@pytest.mark.asyncio
async def test_create_duplicate_name(client):
    """POST with a name that already exists returns 422."""
    await _seed_pipeline_def(name="dup-name")
    form = {
        "name": "dup-name",
        "entry_stage": "stage_a",
        **_minimal_node_form(0, "stage_a"),
    }
    resp = await client.post("/pipeline-defs", data=form)
    assert resp.status_code == 422
    assert "already exists" in resp.text


@pytest.mark.asyncio
async def test_create_duplicate_node_keys(client):
    """POST with duplicate node keys returns 422."""
    form = {
        "name": "dup-nodes",
        "entry_stage": "stage_a",
        **_minimal_node_form(0, "stage_a"),
        **_minimal_node_form(1, "stage_a"),  # same key
    }
    resp = await client.post("/pipeline-defs", data=form)
    assert resp.status_code == 422
    assert "duplicate node key" in resp.text


@pytest.mark.asyncio
async def test_create_duplicate_edge_keys(client):
    """POST with duplicate edge keys returns 422."""
    form = {
        "name": "dup-edges",
        "entry_stage": "sa",
        "node_0_key": "sa",
        "node_0_name": "SA",
        "node_0_type": "spec_author",
        "node_0_agent": "claude",
        "node_0_prompt": "p",
        "node_0_model": "m",
        "node_0_max_iterations": "1",
        "node_0_context_threshold_pct": "60",
        "node_1_key": "sb",
        "node_1_name": "SB",
        "node_1_type": "impl_task",
        "node_1_agent": "claude",
        "node_1_prompt": "p",
        "node_1_model": "m",
        "node_1_max_iterations": "1",
        "node_1_context_threshold_pct": "60",
        **_minimal_edge_form(0, "same_key", "sa", "sb", "approved"),
        **_minimal_edge_form(1, "same_key", "sb", "completed", "stage_complete"),
    }
    resp = await client.post("/pipeline-defs", data=form)
    assert resp.status_code == 422
    assert "duplicate edge key" in resp.text


# ---------------------------------------------------------------------------
# Optional node fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_with_optional_fields(client):
    """Optional fields like uses_devbrowser, on_context_limit are stored."""
    form = {
        "name": "opt-fields",
        "entry_stage": "val",
        "node_0_key": "val",
        "node_0_name": "Validation",
        "node_0_type": "validation",
        "node_0_agent": "claude",
        "node_0_prompt": "validation_default",
        "node_0_model": "claude-sonnet-4-6",
        "node_0_max_iterations": "3",
        "node_0_context_threshold_pct": "50",
        "node_0_on_context_limit": "escalate",
        "node_0_uses_devbrowser": "on",
        "node_0_record_on_success": "on",
    }
    resp = await client.post("/pipeline-defs", data=form, follow_redirects=False)
    assert resp.status_code == 303

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT stage_graph_json FROM pipeline_defs WHERE name = %s",
            ("opt-fields",),
        )
        row = await cur.fetchone()
    graph = json.loads(row["stage_graph_json"])
    node = graph["nodes"][0]
    assert node["uses_devbrowser"] is True
    assert node["record_on_success"] is True
    assert node["on_context_limit"] == "escalate"
    assert node["context_threshold_pct"] == 50


# ---------------------------------------------------------------------------
# Form parsing helpers (unit tests)
# ---------------------------------------------------------------------------


class TestParseNodesFromForm:
    """Unit tests for _parse_nodes_from_form."""

    def test_empty_form(self):
        """Empty form returns no nodes."""
        assert _parse_nodes_from_form({}) == []

    def test_single_node(self):
        """Parse a single node from indexed fields."""
        form = _minimal_node_form(0, "my_stage")
        nodes = _parse_nodes_from_form(form)
        assert len(nodes) == 1
        assert nodes[0]["key"] == "my_stage"
        assert nodes[0]["type"] == "spec_author"
        assert nodes[0]["max_iterations"] == 1

    def test_multiple_nodes(self):
        """Parse multiple nodes with different indices."""
        form = {
            **_minimal_node_form(0, "stage_a"),
            **_minimal_node_form(2, "stage_b"),  # non-sequential index
        }
        nodes = _parse_nodes_from_form(form)
        assert len(nodes) == 2
        assert nodes[0]["key"] == "stage_a"
        assert nodes[1]["key"] == "stage_b"

    def test_skips_empty_key(self):
        """Nodes with empty key are skipped."""
        form = _minimal_node_form(0, "")
        assert _parse_nodes_from_form(form) == []

    def test_whitespace_only_key_skipped(self):
        """Nodes with whitespace-only key are skipped."""
        form = _minimal_node_form(0, "  ")
        assert _parse_nodes_from_form(form) == []

    def test_review_config_parsed(self):
        """Review sub-config is parsed when review_agent is present."""
        form = {
            **_minimal_node_form(0, "s"),
            "node_0_review_agent": "codex",
            "node_0_review_prompt": "rp",
            "node_0_review_model": "rm",
            "node_0_review_max_rounds": "7",
            "node_0_review_exit_condition": "structured_approval",
            "node_0_review_on_max_rounds": "proceed_with_warnings",
        }
        nodes = _parse_nodes_from_form(form)
        assert nodes[0]["review"]["agent"] == "codex"
        assert nodes[0]["review"]["max_review_rounds"] == 7
        assert nodes[0]["review"]["on_max_rounds"] == "proceed_with_warnings"

    def test_no_review_when_agent_empty(self):
        """Review config is omitted when review_agent is empty."""
        form = {
            **_minimal_node_form(0, "s"),
            "node_0_review_agent": "",
        }
        nodes = _parse_nodes_from_form(form)
        assert "review" not in nodes[0]

    def test_default_values(self):
        """Missing optional fields get defaults."""
        form = {"node_0_key": "k", "node_0_prompt": "p", "node_0_model": "m"}
        nodes = _parse_nodes_from_form(form)
        assert len(nodes) == 1
        assert nodes[0]["max_iterations"] == 1
        assert nodes[0]["context_threshold_pct"] == 60
        assert nodes[0]["agent"] == "claude"


class TestParseEdgesFromForm:
    """Unit tests for _parse_edges_from_form."""

    def test_empty_form(self):
        """Empty form returns no edges."""
        assert _parse_edges_from_form({}) == []

    def test_single_edge(self):
        """Parse a single edge from indexed fields."""
        form = _minimal_edge_form(0, "e1", "a", "b", "approved")
        edges = _parse_edges_from_form(form)
        assert len(edges) == 1
        assert edges[0]["key"] == "e1"
        assert edges[0]["from"] == "a"
        assert edges[0]["to"] == "b"
        assert edges[0]["on"] == "approved"

    def test_edge_with_max_visits(self):
        """Edge max_visits is parsed as integer."""
        form = {
            **_minimal_edge_form(0, "e1", "a", "b", "approved"),
            "edge_0_max_visits": "5",
            "edge_0_on_exhausted": "escalate",
        }
        edges = _parse_edges_from_form(form)
        assert edges[0]["max_visits"] == 5
        assert edges[0]["on_exhausted"] == "escalate"

    def test_skips_empty_key(self):
        """Edges with empty key are skipped."""
        form = _minimal_edge_form(0, "", "a", "b", "approved")
        assert _parse_edges_from_form(form) == []

    def test_optional_fields_omitted_when_empty(self):
        """max_visits and on_exhausted are omitted when empty."""
        form = {
            **_minimal_edge_form(0, "e1", "a", "b", "approved"),
            "edge_0_max_visits": "",
            "edge_0_on_exhausted": "",
        }
        edges = _parse_edges_from_form(form)
        assert "max_visits" not in edges[0]
        assert "on_exhausted" not in edges[0]

    def test_multiple_edges_sorted_by_index(self):
        """Multiple edges are returned sorted by index."""
        form = {
            **_minimal_edge_form(3, "e3", "c", "d", "validated"),
            **_minimal_edge_form(1, "e1", "a", "b", "approved"),
        }
        edges = _parse_edges_from_form(form)
        assert len(edges) == 2
        assert edges[0]["key"] == "e1"
        assert edges[1]["key"] == "e3"


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


# Valid stage key strategy: lowercase alphanum + underscore, non-empty
_stage_key_st = st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True)


@hyp_settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(key=_stage_key_st, name=st.text(min_size=1, max_size=30).filter(lambda s: "\r" not in s))
@pytest.mark.asyncio
async def test_parse_nodes_roundtrip(client, key, name):
    """Parsed node key and name match what was put into form fields."""
    form = {**_minimal_node_form(0, key)}
    form["node_0_name"] = name
    nodes = _parse_nodes_from_form(form)
    assert len(nodes) == 1
    assert nodes[0]["key"] == key
    assert nodes[0]["name"] == name


@hyp_settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    key=_stage_key_st,
    from_st=_stage_key_st,
    to_st=_stage_key_st,
)
@pytest.mark.asyncio
async def test_parse_edges_roundtrip(client, key, from_st, to_st):
    """Parsed edge fields match form input."""
    form = _minimal_edge_form(0, key, from_st, to_st, "approved")
    edges = _parse_edges_from_form(form)
    assert len(edges) == 1
    assert edges[0]["key"] == key
    assert edges[0]["from"] == from_st
    assert edges[0]["to"] == to_st


@hyp_settings(
    max_examples=15,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    node_count=st.integers(min_value=1, max_value=8),
)
@pytest.mark.asyncio
async def test_parse_nodes_count_matches(client, node_count):
    """Number of parsed nodes matches number of form node groups."""
    form: dict[str, str] = {}
    for i in range(node_count):
        form.update(_minimal_node_form(i, f"stage_{i}"))
    nodes = _parse_nodes_from_form(form)
    assert len(nodes) == node_count
