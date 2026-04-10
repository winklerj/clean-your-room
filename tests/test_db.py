from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from build_your_room.db import (
    DEFAULT_PROMPTS,
    SPECS_DIR,
    close_pool,
    get_pool,
    init_db,
    load_default_prompts_json,
)


ALL_TABLES = [
    "repos",
    "prompts",
    "pipeline_defs",
    "pipelines",
    "pipeline_stages",
    "agent_sessions",
    "session_logs",
    "escalations",
    "htn_tasks",
    "htn_task_deps",
]

# Every stage type that can appear in a pipeline node must have a default prompt.
REQUIRED_STAGE_TYPES = {
    "spec_author",
    "spec_review",
    "impl_plan",
    "impl_plan_review",
    "impl_task",
    "code_review",
    "bug_fix",
    "validation",
}


@pytest.mark.asyncio
async def test_init_db_creates_all_tables(initialized_db):
    """Schema init must create all spec-defined tables."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        tables = [row["tablename"] for row in await cur.fetchall()]
    for table in ALL_TABLES:
        assert table in tables, f"Missing table: {table}"


@pytest.mark.asyncio
async def test_init_db_seeds_default_prompts(initialized_db):
    """Schema init must seed default prompts with stage_type and agent_type.

    Invariant: every required stage type has at least one seeded default prompt.
    Context: stage runners fall back to the raw name string if no prompt is found,
    so missing defaults cause incoherent system prompts.
    """
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT name, stage_type, agent_type FROM prompts ORDER BY id"
        )
        rows = await cur.fetchall()
    names = [row["name"] for row in rows]

    # All 8 default prompts must be seeded
    assert "spec_author_default" in names
    assert "spec_review_default" in names
    assert "impl_plan_default" in names
    assert "impl_plan_review_default" in names
    assert "impl_task_default" in names
    assert "code_review_default" in names
    assert "bug_fix_default" in names
    assert "validation_default" in names

    # Verify stage_type/agent_type assignments
    spec_author = next(r for r in rows if r["name"] == "spec_author_default")
    assert spec_author["stage_type"] == "spec_author"
    assert spec_author["agent_type"] == "claude"

    spec_review = next(r for r in rows if r["name"] == "spec_review_default")
    assert spec_review["stage_type"] == "spec_review"
    assert spec_review["agent_type"] == "codex"

    impl_task = next(r for r in rows if r["name"] == "impl_task_default")
    assert impl_task["stage_type"] == "impl_task"
    assert impl_task["agent_type"] == "claude"

    code_review = next(r for r in rows if r["name"] == "code_review_default")
    assert code_review["stage_type"] == "code_review"
    assert code_review["agent_type"] == "codex"

    bug_fix = next(r for r in rows if r["name"] == "bug_fix_default")
    assert bug_fix["stage_type"] == "bug_fix"
    assert bug_fix["agent_type"] == "codex"

    validation = next(r for r in rows if r["name"] == "validation_default")
    assert validation["stage_type"] == "validation"
    assert validation["agent_type"] == "claude"


@pytest.mark.asyncio
async def test_init_db_is_idempotent(initialized_db):
    """Running init_db twice must not duplicate seed data or error."""
    # init_db was already called by the fixture; call it again
    await close_pool()
    await init_db(initialized_db)
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) AS cnt FROM prompts")
        row = await cur.fetchone()
        assert row is not None
        count = row["cnt"]
    assert count == len(DEFAULT_PROMPTS)


@pytest.mark.asyncio
async def test_repos_table_has_correct_columns(initialized_db):
    """Repos table has expected columns and NOT old GitHub-specific ones."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'repos' AND table_schema = 'public'"
        )
        columns = {row["column_name"] for row in await cur.fetchall()}
    assert "local_path" in columns
    assert "name" in columns
    assert "git_url" in columns
    assert "default_branch" in columns
    assert "archived" in columns
    # Old columns must not exist
    assert "github_url" not in columns
    assert "slug" not in columns


@pytest.mark.asyncio
async def test_htn_tasks_table_has_correct_columns(initialized_db):
    """HTN tasks table has all spec-defined columns."""
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'htn_tasks' AND table_schema = 'public'"
        )
        columns = {row["column_name"] for row in await cur.fetchall()}
    expected = {
        "id", "pipeline_id", "parent_task_id", "name", "description",
        "task_type", "status", "priority", "ordering",
        "assigned_session_id", "claim_token", "claim_owner_token",
        "claim_expires_at", "preconditions_json", "postconditions_json",
        "invariants_json", "output_artifacts_json", "checkpoint_rev",
        "estimated_complexity", "diary_entry",
        "created_at", "started_at", "completed_at",
    }
    assert expected.issubset(columns), f"Missing: {expected - columns}"


@pytest.mark.asyncio
async def test_foreign_keys_enforced(initialized_db):
    """Foreign key constraints must prevent invalid references."""
    pool = get_pool()
    async with pool.connection() as conn:
        with pytest.raises(Exception):
            await conn.execute(
                "INSERT INTO pipelines (pipeline_def_id, repo_id, clone_path, "
                "review_base_rev, status, config_json) "
                "VALUES (9999, 9999, '/tmp', 'abc', 'pending', '{}')"
            )


# ---------------------------------------------------------------------------
# Default prompt coverage and JSON consistency tests
# ---------------------------------------------------------------------------


def test_all_stage_types_have_default_prompt():
    """Every required stage type must have exactly one entry in DEFAULT_PROMPTS.

    Invariant: stage runners fall back to the raw name string if the prompt DB
    lookup fails, so every stage type referenced in pipeline graph nodes must
    have a pre-seeded default.
    """
    covered = {tup[2] for tup in DEFAULT_PROMPTS}
    missing = REQUIRED_STAGE_TYPES - covered
    assert not missing, f"Stage types without default prompts: {missing}"


def test_default_prompt_names_follow_convention():
    """All default prompt names must end with '_default'.

    Convention: pipeline graph nodes reference prompts by name, and the default
    convention is {stage_type}_default (or {purpose}_default for bug_fix).
    """
    for name, _body, _st, _at in DEFAULT_PROMPTS:
        assert name.endswith("_default"), f"Prompt name {name!r} missing '_default' suffix"


def test_default_prompt_bodies_are_nonempty():
    """Prompt bodies must be non-empty, multi-line instructional text.

    Context: a single-word body would indicate a stub, not a usable prompt.
    """
    for name, body, _st, _at in DEFAULT_PROMPTS:
        assert len(body) > 50, f"Prompt {name!r} body too short ({len(body)} chars)"
        assert "\n" in body, f"Prompt {name!r} body should be multi-line"


def test_default_prompt_names_are_unique():
    """No two default prompts may share the same name.

    Invariant: the prompts table has a UNIQUE constraint on name;
    duplicate names would cause ON CONFLICT DO NOTHING to silently skip.
    """
    names = [tup[0] for tup in DEFAULT_PROMPTS]
    assert len(names) == len(set(names)), f"Duplicate prompt names: {names}"


def test_default_prompt_agent_types_are_valid():
    """Agent types must be 'claude' or 'codex'.

    Context: adapters are dispatched by agent_type; unknown values would fail.
    """
    valid = {"claude", "codex"}
    for name, _body, _st, agent_type in DEFAULT_PROMPTS:
        assert agent_type in valid, f"Prompt {name!r} has invalid agent_type {agent_type!r}"


# ---------------------------------------------------------------------------
# JSON file tests
# ---------------------------------------------------------------------------


def test_default_prompts_json_is_valid():
    """specs/default_prompts.json must be valid JSON with the expected structure.

    Invariant: the JSON file must be a list of objects, each with name, body,
    stage_type, and agent_type keys.
    """
    prompts = load_default_prompts_json()
    assert isinstance(prompts, list)
    assert len(prompts) == len(DEFAULT_PROMPTS)
    required_keys = {"name", "body", "stage_type", "agent_type"}
    for entry in prompts:
        assert required_keys.issubset(entry.keys()), f"Missing keys in {entry.get('name')}"


def test_default_prompts_json_matches_python_constant():
    """The JSON file must match the DEFAULT_PROMPTS Python constant exactly.

    Invariant: these two sources of truth must stay in sync. If they diverge,
    the DB seeds and the exported JSON describe different prompt sets.
    """
    json_prompts = load_default_prompts_json()
    python_prompts = [
        {"name": n, "body": b, "stage_type": s, "agent_type": a}
        for n, b, s, a in DEFAULT_PROMPTS
    ]
    assert len(json_prompts) == len(python_prompts)
    json_by_name = {p["name"]: p for p in json_prompts}
    for py_prompt in python_prompts:
        name = py_prompt["name"]
        assert name in json_by_name, f"Prompt {name!r} missing from JSON file"
        json_prompt = json_by_name[name]
        assert json_prompt["body"] == py_prompt["body"], (
            f"Body mismatch for {name!r}"
        )
        assert json_prompt["stage_type"] == py_prompt["stage_type"]
        assert json_prompt["agent_type"] == py_prompt["agent_type"]


def test_example_pipeline_def_json_is_valid():
    """specs/example_pipeline_def.json must be valid JSON with required fields.

    Context: the example pipeline def is a reference for users building their own
    pipeline definitions.
    """
    path = SPECS_DIR / "example_pipeline_def.json"
    with open(path) as f:
        data = json.load(f)
    assert "name" in data
    assert "stage_graph_json" in data
    graph = data["stage_graph_json"]
    assert "entry_stage" in graph
    assert "nodes" in graph
    assert "edges" in graph
    assert len(graph["nodes"]) > 0
    assert len(graph["edges"]) > 0


def test_example_pipeline_def_references_existing_prompts():
    """All prompt names referenced in the example pipeline def must exist
    in the default prompts set.

    Invariant: a user loading the example pipeline def should have all referenced
    prompts available after init_db seeds them.
    """
    path = SPECS_DIR / "example_pipeline_def.json"
    with open(path) as f:
        data = json.load(f)
    graph = data["stage_graph_json"]

    default_names = {tup[0] for tup in DEFAULT_PROMPTS}
    for node in graph["nodes"]:
        prompt_name = node.get("prompt")
        if prompt_name:
            assert prompt_name in default_names, (
                f"Node {node['key']!r} references prompt {prompt_name!r} "
                f"not in defaults"
            )
        review = node.get("review")
        if review and review.get("prompt"):
            assert review["prompt"] in default_names, (
                f"Node {node['key']!r} review references prompt "
                f"{review['prompt']!r} not in defaults"
            )
        fix_prompt = node.get("fix_prompt")
        if fix_prompt:
            assert fix_prompt in default_names, (
                f"Node {node['key']!r} fix_prompt {fix_prompt!r} not in defaults"
            )


@pytest.mark.asyncio
async def test_seeded_prompt_bodies_match_json(initialized_db):
    """DB-seeded prompt bodies must match the JSON file exactly.

    Invariant: ensures the DB and JSON stay in sync after init_db runs.
    """
    json_prompts = {p["name"]: p["body"] for p in load_default_prompts_json()}
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT name, body FROM prompts ORDER BY id")
        rows = await cur.fetchall()
    for row in rows:
        name = row["name"]
        assert name in json_prompts, f"DB prompt {name!r} not in JSON"
        assert row["body"] == json_prompts[name], f"Body mismatch for {name!r}"


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


@given(
    stage_type=st.sampled_from(sorted(REQUIRED_STAGE_TYPES)),
)
@settings(max_examples=20)
def test_pbt_every_stage_type_maps_to_default_prompt(stage_type: str):
    """For any valid stage type, DEFAULT_PROMPTS must contain a matching entry.

    Invariant: no stage type is orphaned without a default prompt.
    """
    matching = [tup for tup in DEFAULT_PROMPTS if tup[2] == stage_type]
    assert len(matching) >= 1, f"No default prompt for stage_type={stage_type!r}"


@given(
    prompt_idx=st.integers(min_value=0, max_value=len(DEFAULT_PROMPTS) - 1),
)
@settings(max_examples=20)
def test_pbt_default_prompt_tuple_has_four_nonempty_strings(prompt_idx: int):
    """Every DEFAULT_PROMPTS tuple must have exactly 4 non-empty string elements.

    Invariant: the SEED_PROMPTS_SQL expects (name, body, stage_type, agent_type),
    all as non-empty strings.
    """
    tup = DEFAULT_PROMPTS[prompt_idx]
    assert len(tup) == 4
    for i, val in enumerate(tup):
        assert isinstance(val, str), f"Element {i} is not a string"
        assert len(val) > 0, f"Element {i} is empty"
