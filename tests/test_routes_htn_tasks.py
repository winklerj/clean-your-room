"""Tests for the HTN task tree page — GET /pipelines/{id}/tasks with filtering."""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from httpx import ASGITransport, AsyncClient

from build_your_room.db import get_pool
from build_your_room.main import app


@pytest.fixture
async def client(initialized_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

_GRAPH = json.dumps({
    "entry_stage": "impl_task",
    "nodes": [
        {"key": "impl_task", "name": "Implementation", "type": "impl_task",
         "agent": "claude", "prompt": "impl_task_default",
         "model": "claude-sonnet-4-6", "max_iterations": 50},
    ],
    "edges": [],
})


async def _seed_repo(suffix: str = "") -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO repos (name, local_path) VALUES (%s, %s) RETURNING id",
            (f"task-repo{suffix}", f"/tmp/task-repo{suffix}"),
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_def(suffix: str = "") -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipeline_defs (name, stage_graph_json) VALUES (%s, %s) RETURNING id",
            (f"task-def{suffix}", _GRAPH),
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_pipeline(repo_id: int, def_id: int, status: str = "running") -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipelines "
            "(pipeline_def_id, repo_id, clone_path, review_base_rev, status, "
            " current_stage_key, config_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (def_id, repo_id, "/tmp/clone", "abc123", status, "impl_task", "{}"),
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        assert row is not None
        await conn.commit()
        return row["id"]


async def _seed_task(
    pipeline_id: int,
    name: str,
    task_type: str = "primitive",
    status: str = "ready",
    ordering: int = 0,
    parent_task_id: int | None = None,
    diary_entry: str | None = None,
    estimated_complexity: str | None = None,
    preconditions_json: str = "[]",
    postconditions_json: str = "[]",
) -> int:
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO htn_tasks "
            "(pipeline_id, parent_task_id, name, description, task_type, "
            " status, ordering, diary_entry, estimated_complexity, "
            " preconditions_json, postconditions_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (pipeline_id, parent_task_id, name, f"Description of {name}",
             task_type, status, ordering, diary_entry, estimated_complexity,
             preconditions_json, postconditions_json),
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        assert row is not None
        await conn.commit()
        return row["id"]


# ---------------------------------------------------------------------------
# Tests — 404 and empty state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_page_404_missing_pipeline(client):
    """GET /pipelines/{id}/tasks returns 404 for non-existent pipeline.

    Invariant: non-existent pipeline IDs yield a clear 404.
    Context: the tasks page must validate the pipeline exists before rendering.
    """
    resp = await client.get("/pipelines/99999/tasks")
    assert resp.status_code == 404
    assert "not found" in resp.text.lower()


@pytest.mark.asyncio
async def test_tasks_page_empty(client):
    """GET /pipelines/{id}/tasks renders empty state when no tasks exist.

    Invariant: pipeline with no HTN tasks shows a clear empty-state message.
    Context: a pipeline may not yet have an HTN task tree (e.g. before impl plan).
    """
    repo_id = await _seed_repo("-empty")
    def_id = await _seed_def("-empty")
    pid = await _seed_pipeline(repo_id, def_id)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "No HTN tasks" in resp.text


# ---------------------------------------------------------------------------
# Tests — rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_page_renders_tree(client):
    """GET /pipelines/{id}/tasks renders compound and primitive tasks in tree.

    Invariant: the page displays all tasks with their names and status badges.
    Context: verifies the task tree structure is rendered with hierarchy.
    """
    repo_id = await _seed_repo("-tree")
    def_id = await _seed_def("-tree")
    pid = await _seed_pipeline(repo_id, def_id)

    compound_id = await _seed_task(pid, "Setup phase", task_type="compound",
                                   status="in_progress", ordering=0)
    await _seed_task(pid, "Create database", status="completed", ordering=0,
                     parent_task_id=compound_id)
    await _seed_task(pid, "Add migrations", status="ready", ordering=1,
                     parent_task_id=compound_id)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "Setup phase" in resp.text
    assert "Create database" in resp.text
    assert "Add migrations" in resp.text
    assert "compound" in resp.text


@pytest.mark.asyncio
async def test_tasks_page_progress_bar(client):
    """GET /pipelines/{id}/tasks shows progress summary for primitive tasks.

    Invariant: progress bar reflects completed/total primitive task counts.
    Context: the summary bar must track only primitive tasks, not compound.
    """
    repo_id = await _seed_repo("-prog")
    def_id = await _seed_def("-prog")
    pid = await _seed_pipeline(repo_id, def_id)

    await _seed_task(pid, "task-a", status="completed", ordering=0)
    await _seed_task(pid, "task-b", status="completed", ordering=1)
    await _seed_task(pid, "task-c", status="ready", ordering=2)
    await _seed_task(pid, "task-d", status="not_ready", ordering=3)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "2/4" in resp.text
    assert "50%" in resp.text


@pytest.mark.asyncio
async def test_tasks_page_diary_entry(client):
    """GET /pipelines/{id}/tasks renders diary entries for completed tasks.

    Invariant: diary entries are visible in the task detail section.
    Context: diary entries are critical for cross-session knowledge sharing.
    """
    repo_id = await _seed_repo("-diary")
    def_id = await _seed_def("-diary")
    pid = await _seed_pipeline(repo_id, def_id)

    await _seed_task(pid, "impl-auth", status="completed",
                     diary_entry="Used JWT tokens for session management")

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "JWT tokens" in resp.text


@pytest.mark.asyncio
async def test_tasks_page_decision_resolve_form(client):
    """GET /pipelines/{id}/tasks shows resolve form for unresolved decision tasks.

    Invariant: decision-type tasks that are not completed have a resolve form.
    Context: decision tasks need human intervention via the task tree page.
    """
    repo_id = await _seed_repo("-dec")
    def_id = await _seed_def("-dec")
    pid = await _seed_pipeline(repo_id, def_id)

    await _seed_task(pid, "Choose auth strategy", task_type="decision",
                     status="blocked", ordering=0)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "Choose auth strategy" in resp.text
    assert "decision" in resp.text
    assert 'name="resolution"' in resp.text


# ---------------------------------------------------------------------------
# Tests — filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_page_filter_by_status(client):
    """GET /pipelines/{id}/tasks?status_filter=completed shows only matching tasks.

    Invariant: status filter limits visible tasks to the selected status
    (plus ancestors for tree structure).
    Context: filtering helps users focus on specific task states.
    """
    repo_id = await _seed_repo("-filt-s")
    def_id = await _seed_def("-filt-s")
    pid = await _seed_pipeline(repo_id, def_id)

    await _seed_task(pid, "done-task", status="completed", ordering=0)
    await _seed_task(pid, "pending-task", status="ready", ordering=1)

    resp = await client.get(f"/pipelines/{pid}/tasks?status_filter=completed")
    assert resp.status_code == 200
    assert "done-task" in resp.text
    assert "pending-task" not in resp.text


@pytest.mark.asyncio
async def test_tasks_page_filter_by_type(client):
    """GET /pipelines/{id}/tasks?type_filter=compound shows only compound tasks.

    Invariant: type filter limits visible tasks to the selected task type.
    Context: helps users focus on compound vs. primitive vs. decision tasks.
    """
    repo_id = await _seed_repo("-filt-t")
    def_id = await _seed_def("-filt-t")
    pid = await _seed_pipeline(repo_id, def_id)

    await _seed_task(pid, "parent-task", task_type="compound", status="in_progress", ordering=0)
    await _seed_task(pid, "leaf-task", task_type="primitive", status="ready", ordering=1)

    resp = await client.get(f"/pipelines/{pid}/tasks?type_filter=compound")
    assert resp.status_code == 200
    assert "parent-task" in resp.text
    assert "leaf-task" not in resp.text


@pytest.mark.asyncio
async def test_tasks_page_filter_preserves_ancestors(client):
    """Filtering by child status preserves parent compound task in tree.

    Invariant: when filtering reveals a child, its parent is kept
    so the tree structure is maintained.
    Context: without ancestors, filtered children would become orphaned roots.
    """
    repo_id = await _seed_repo("-anc")
    def_id = await _seed_def("-anc")
    pid = await _seed_pipeline(repo_id, def_id)

    parent_id = await _seed_task(pid, "parent-compound", task_type="compound",
                                 status="in_progress", ordering=0)
    await _seed_task(pid, "child-completed", task_type="primitive",
                     status="completed", ordering=0, parent_task_id=parent_id)
    await _seed_task(pid, "child-ready", task_type="primitive",
                     status="ready", ordering=1, parent_task_id=parent_id)

    resp = await client.get(f"/pipelines/{pid}/tasks?status_filter=completed")
    assert resp.status_code == 200
    # Parent is preserved as ancestor
    assert "parent-compound" in resp.text
    assert "child-completed" in resp.text
    # Sibling with different status is filtered out
    assert "child-ready" not in resp.text


@pytest.mark.asyncio
async def test_tasks_page_clear_filters_link(client):
    """Filtered tasks page shows a 'Clear filters' link.

    Invariant: when any filter is active, the page shows a clear-filters link
    pointing to the unfiltered tasks page.
    Context: users need a way to reset filters.
    """
    repo_id = await _seed_repo("-clear")
    def_id = await _seed_def("-clear")
    pid = await _seed_pipeline(repo_id, def_id)
    await _seed_task(pid, "a-task", status="completed", ordering=0)

    resp = await client.get(f"/pipelines/{pid}/tasks?status_filter=completed")
    assert resp.status_code == 200
    assert "Clear filters" in resp.text
    assert f"/pipelines/{pid}/tasks" in resp.text


@pytest.mark.asyncio
async def test_tasks_page_no_filter_no_clear_link(client):
    """Unfiltered tasks page does not show 'Clear filters' link.

    Invariant: the clear-filters link only appears when filters are active.
    Context: prevents UI clutter when no filters are applied.
    """
    repo_id = await _seed_repo("-noclear")
    def_id = await _seed_def("-noclear")
    pid = await _seed_pipeline(repo_id, def_id)
    await _seed_task(pid, "some-task", status="ready", ordering=0)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "Clear filters" not in resp.text


@pytest.mark.asyncio
async def test_tasks_page_empty_filter_result(client):
    """Filtering with no matching tasks shows appropriate message.

    Invariant: an empty filter result shows a no-match message, not the
    generic 'no tasks' message.
    Context: distinguishes between 'pipeline has no tasks' and 'filter matched nothing'.
    """
    repo_id = await _seed_repo("-nores")
    def_id = await _seed_def("-nores")
    pid = await _seed_pipeline(repo_id, def_id)
    await _seed_task(pid, "ready-task", status="ready", ordering=0)

    resp = await client.get(f"/pipelines/{pid}/tasks?status_filter=failed")
    assert resp.status_code == 200
    assert "No tasks match" in resp.text


# ---------------------------------------------------------------------------
# Tests — status/type chip counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_page_status_chips(client):
    """Tasks page shows status count chips for filtering.

    Invariant: each distinct task status appears as a clickable chip with count.
    Context: chips provide quick visual feedback and filtering shortcuts.
    """
    repo_id = await _seed_repo("-chips")
    def_id = await _seed_def("-chips")
    pid = await _seed_pipeline(repo_id, def_id)

    await _seed_task(pid, "t1", status="completed", ordering=0)
    await _seed_task(pid, "t2", status="completed", ordering=1)
    await _seed_task(pid, "t3", status="ready", ordering=2)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    # Status chips should show counts
    assert "completed" in resp.text.lower()
    assert "ready" in resp.text.lower()


@pytest.mark.asyncio
async def test_tasks_page_back_link(client):
    """Tasks page has a back link to the pipeline detail page.

    Invariant: the back link points to /pipelines/{id}.
    Context: navigation from standalone task tree back to full pipeline detail.
    """
    repo_id = await _seed_repo("-back")
    def_id = await _seed_def("-back")
    pid = await _seed_pipeline(repo_id, def_id)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert f"/pipelines/{pid}" in resp.text
    assert "Back to pipeline detail" in resp.text


@pytest.mark.asyncio
async def test_tasks_page_pipeline_header(client):
    """Tasks page shows pipeline name, repo name, and status in header.

    Invariant: the page header identifies the pipeline with def name, repo, and status.
    Context: users need context about which pipeline's tasks they are viewing.
    """
    repo_id = await _seed_repo("-hdr")
    def_id = await _seed_def("-hdr")
    pid = await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "task-def-hdr" in resp.text
    assert "task-repo-hdr" in resp.text
    assert "running" in resp.text


# ---------------------------------------------------------------------------
# Tests — precondition/postcondition display
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_page_preconditions_shown(client):
    """Tasks with preconditions_json display their preconditions.

    Invariant: preconditions are rendered with type badge and description.
    Context: spec line 958 requires "precondition/postcondition status" on task nodes.
    """
    repo_id = await _seed_repo("-pre")
    def_id = await _seed_def("-pre")
    pid = await _seed_pipeline(repo_id, def_id)

    preconditions = json.dumps([
        {"type": "file_exists", "path": "src/auth.py",
         "description": "Auth module must exist"},
    ])
    await _seed_task(pid, "auth-task", status="ready", ordering=0,
                     preconditions_json=preconditions)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "Preconditions:" in resp.text
    assert "file_exists" in resp.text
    assert "Auth module must exist" in resp.text


@pytest.mark.asyncio
async def test_tasks_page_postconditions_shown(client):
    """Tasks with postconditions_json display their postconditions.

    Invariant: postconditions are rendered with type badge and description.
    Context: spec line 958 requires "precondition/postcondition status" on task nodes.
    """
    repo_id = await _seed_repo("-post")
    def_id = await _seed_def("-post")
    pid = await _seed_pipeline(repo_id, def_id)

    postconditions = json.dumps([
        {"type": "tests_pass", "pattern": "tests/test_auth*",
         "description": "Auth tests must pass"},
        {"type": "lint_clean", "scope": "src/auth/",
         "description": "Auth module lints clean"},
    ])
    await _seed_task(pid, "impl-auth", status="in_progress", ordering=0,
                     postconditions_json=postconditions)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "Postconditions:" in resp.text
    assert "tests_pass" in resp.text
    assert "Auth tests must pass" in resp.text
    assert "lint_clean" in resp.text
    assert "Auth module lints clean" in resp.text


@pytest.mark.asyncio
async def test_tasks_page_completed_task_conditions_passed(client):
    """Completed tasks show checkmarks on both preconditions and postconditions.

    Invariant: completed task conditions display passed indicator (checkmark).
    Context: a completed task implies all conditions were satisfied.
    """
    repo_id = await _seed_repo("-cond-pass")
    def_id = await _seed_def("-cond-pass")
    pid = await _seed_pipeline(repo_id, def_id)

    preconditions = json.dumps([
        {"type": "file_exists", "path": "src/db.py",
         "description": "DB module exists"},
    ])
    postconditions = json.dumps([
        {"type": "tests_pass", "pattern": "tests/test_db*",
         "description": "DB tests pass"},
    ])
    await _seed_task(pid, "db-task", status="completed", ordering=0,
                     preconditions_json=preconditions,
                     postconditions_json=postconditions)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "condition-passed" in resp.text
    assert "condition-pending" not in resp.text


@pytest.mark.asyncio
async def test_tasks_page_ready_task_conditions_pending(client):
    """Ready tasks show pending indicators on postconditions.

    Invariant: not-yet-executed task postconditions display pending indicator.
    Context: preconditions should be satisfied (task is ready) but postconditions
    have not been verified yet.
    """
    repo_id = await _seed_repo("-cond-pend")
    def_id = await _seed_def("-cond-pend")
    pid = await _seed_pipeline(repo_id, def_id)

    postconditions = json.dumps([
        {"type": "tests_pass", "description": "Tests pass"},
    ])
    await _seed_task(pid, "pending-task", status="ready", ordering=0,
                     postconditions_json=postconditions)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "condition-pending" in resp.text


@pytest.mark.asyncio
async def test_tasks_page_failed_task_postconditions_failed(client):
    """Failed tasks show failed indicators on postconditions.

    Invariant: failed task postconditions display failed indicator (X mark).
    Context: a failed task implies postconditions were not met.
    """
    repo_id = await _seed_repo("-cond-fail")
    def_id = await _seed_def("-cond-fail")
    pid = await _seed_pipeline(repo_id, def_id)

    postconditions = json.dumps([
        {"type": "tests_pass", "description": "Tests pass"},
    ])
    await _seed_task(pid, "failed-task", status="failed", ordering=0,
                     postconditions_json=postconditions)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "condition-failed" in resp.text


@pytest.mark.asyncio
async def test_tasks_page_no_conditions_no_section(client):
    """Tasks with empty conditions don't render conditions sections.

    Invariant: tasks with empty [] conditions don't show Preconditions/Postconditions labels.
    Context: avoid visual noise for tasks without explicit conditions.
    """
    repo_id = await _seed_repo("-nocond")
    def_id = await _seed_def("-nocond")
    pid = await _seed_pipeline(repo_id, def_id)

    await _seed_task(pid, "simple-task", status="ready", ordering=0)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert "simple-task" in resp.text
    assert "Preconditions:" not in resp.text
    assert "Postconditions:" not in resp.text


@pytest.mark.asyncio
async def test_tasks_page_conditions_on_pipeline_detail(client):
    """Pipeline detail page also renders conditions on HTN task nodes.

    Invariant: pipeline detail page shows the same condition info as the
    standalone tasks page, since both use the same partial template.
    Context: spec line 958 applies to the pipeline detail HTN task tree too.
    """
    repo_id = await _seed_repo("-detail-cond")
    def_id = await _seed_def("-detail-cond")
    pid = await _seed_pipeline(repo_id, def_id)

    postconditions = json.dumps([
        {"type": "lint_clean", "description": "Code lints clean"},
    ])
    await _seed_task(pid, "lint-task", status="completed", ordering=0,
                     postconditions_json=postconditions)

    resp = await client.get(f"/pipelines/{pid}")
    assert resp.status_code == 200
    assert "Postconditions:" in resp.text
    assert "lint_clean" in resp.text
    assert "Code lints clean" in resp.text
    assert "condition-passed" in resp.text


@pytest.mark.asyncio
async def test_tasks_page_multiple_conditions_all_rendered(client):
    """Tasks with multiple conditions show all of them.

    Invariant: every condition in the JSON array is rendered in the HTML.
    Context: tasks commonly have 2-4 postconditions that all need visibility.
    """
    repo_id = await _seed_repo("-multi")
    def_id = await _seed_def("-multi")
    pid = await _seed_pipeline(repo_id, def_id)

    postconditions = json.dumps([
        {"type": "file_exists", "description": "Module exists"},
        {"type": "tests_pass", "description": "Tests pass"},
        {"type": "lint_clean", "description": "Lint clean"},
    ])
    await _seed_task(pid, "multi-cond", status="in_progress", ordering=0,
                     postconditions_json=postconditions)

    resp = await client.get(f"/pipelines/{pid}/tasks")
    assert resp.status_code == 200
    assert resp.text.count("condition-type-badge") >= 3
    assert "Module exists" in resp.text
    assert "Tests pass" in resp.text
    assert "Lint clean" in resp.text


# ---------------------------------------------------------------------------
# Unit tests — _parse_conditions helper
# ---------------------------------------------------------------------------

from build_your_room.routes.pipelines import _parse_conditions  # noqa: E402


def test_parse_conditions_valid_json():
    """_parse_conditions parses well-formed condition JSON.

    Invariant: valid JSON with type+description yields matching dicts.
    Context: the helper powers condition display in the template.
    """
    raw = json.dumps([
        {"type": "file_exists", "path": "foo.py", "description": "Foo exists"},
        {"type": "tests_pass", "description": "Tests pass"},
    ])
    result = _parse_conditions(raw)
    assert len(result) == 2
    assert result[0] == {"type": "file_exists", "description": "Foo exists"}
    assert result[1] == {"type": "tests_pass", "description": "Tests pass"}


def test_parse_conditions_empty():
    """_parse_conditions returns empty list for empty input.

    Invariant: empty string, '[]', None all produce [].
    Context: most tasks have no explicit conditions; the helper must be safe.
    """
    assert _parse_conditions("[]") == []
    assert _parse_conditions("") == []
    assert _parse_conditions("null") == []


def test_parse_conditions_invalid_json():
    """_parse_conditions returns empty list for malformed JSON.

    Invariant: invalid JSON never crashes, just returns [].
    Context: defensive parsing for potentially corrupt DB data.
    """
    assert _parse_conditions("{bad json}") == []
    assert _parse_conditions("not json at all") == []


def test_parse_conditions_missing_description_falls_back_to_type():
    """_parse_conditions uses type as description fallback.

    Invariant: when 'description' is missing, the 'type' field is used instead.
    Context: some conditions may omit the description field.
    """
    raw = json.dumps([{"type": "lint_clean", "scope": "src/"}])
    result = _parse_conditions(raw)
    assert len(result) == 1
    assert result[0]["description"] == "lint_clean"


def test_parse_conditions_skips_non_dict_entries():
    """_parse_conditions skips non-dict items in the conditions array.

    Invariant: only dict entries are included; strings, numbers, nulls are skipped.
    Context: defensive handling of malformed condition arrays.
    """
    raw = json.dumps([
        {"type": "file_exists", "description": "Valid"},
        "not a dict",
        42,
        None,
    ])
    result = _parse_conditions(raw)
    assert len(result) == 1
    assert result[0]["description"] == "Valid"


# ---------------------------------------------------------------------------
# Property-based test — conditions parsing roundtrip
# ---------------------------------------------------------------------------

_condition_types = st.sampled_from([
    "file_exists", "tests_pass", "lint_clean", "type_check",
    "task_completed", "custom_verifier",
])

_condition_st = st.fixed_dictionaries({
    "type": _condition_types,
    "description": st.text(
        min_size=1, max_size=80,
        alphabet=st.characters(blacklist_categories=("Cs",)),
    ),
})


@given(conditions=st.lists(_condition_st, min_size=0, max_size=10))
@settings(max_examples=50)
def test_parse_conditions_roundtrip(conditions: list[dict[str, str]]):
    """_parse_conditions preserves type and description for valid conditions.

    Invariant: for any list of well-formed condition dicts, parsing the
    JSON-serialized form returns the same type and description values.
    Context: property-based verification that the parser is lossless.
    """
    raw = json.dumps(conditions)
    parsed = _parse_conditions(raw)
    assert len(parsed) == len(conditions)
    for orig, result in zip(conditions, parsed):
        assert result["type"] == orig["type"]
        assert result["description"] == orig["description"]
