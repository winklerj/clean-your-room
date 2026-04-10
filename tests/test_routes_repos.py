from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport
from hypothesis import HealthCheck, given, settings, strategies as st

from build_your_room.main import app


@pytest.fixture
async def client(initialized_db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---- Helpers ----


async def _seed_repo(
    client: AsyncClient, tmp_path, *, name: str | None = None, suffix: str = ""
) -> int:
    """Create a repo and return its id."""
    repo_dir = tmp_path / f"repo-{suffix or uuid.uuid4().hex[:8]}"
    repo_dir.mkdir(exist_ok=True)
    resp = await client.post("/repos", data={
        "name": name or f"project-{suffix or uuid.uuid4().hex[:8]}",
        "local_path": str(repo_dir),
    }, follow_redirects=False)
    assert resp.status_code == 303
    # Extract id from redirect location /repos/{id}
    return int(resp.headers["location"].split("/")[-1])


async def _seed_pipeline_def(client: AsyncClient, name: str | None = None) -> int:
    """Create a pipeline definition and return its id."""
    from build_your_room.db import get_pool

    def_name = name or f"def-{uuid.uuid4().hex[:8]}"
    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipeline_defs (name, stage_graph_json) "
            "VALUES (%s, %s) RETURNING id",
            (def_name, '{"entry_stage":"s","nodes":[{"key":"s","name":"S","type":"spec_author","agent":"claude"}],"edges":[]}'),
        )
        row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row["id"]


async def _seed_pipeline(
    repo_id: int, def_id: int, *, status: str = "completed"
) -> int:
    """Insert a pipeline row directly."""
    from build_your_room.db import get_pool

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO pipelines "
            "(pipeline_def_id, repo_id, clone_path, review_base_rev, status, config_json) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (def_id, repo_id, "/tmp/clone", "abc123", status, "{}"),
        )
        row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row["id"]


# ---- Existing tests ----


@pytest.mark.asyncio
async def test_add_repo(client, tmp_path):
    """POST /repos creates a repo record for a local path."""
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()
    resp = await client.post("/repos", data={
        "name": "my-project",
        "local_path": str(repo_dir),
    }, follow_redirects=False)
    assert resp.status_code == 303


@pytest.mark.asyncio
async def test_add_repo_nonexistent_path(client, tmp_path):
    """POST /repos with nonexistent path returns 400."""
    resp = await client.post("/repos", data={
        "name": "bad-project",
        "local_path": str(tmp_path / "does-not-exist"),
    }, follow_redirects=False)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_repo_detail(client, tmp_path):
    """GET /repos/{id} shows repo info."""
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()
    await client.post("/repos", data={
        "name": "my-project",
        "local_path": str(repo_dir),
    }, follow_redirects=False)
    resp = await client.get("/repos/1")
    assert resp.status_code == 200
    assert "my-project" in resp.text


@pytest.mark.asyncio
async def test_archive_repo(client, tmp_path):
    """POST /repos/{id}/archive marks repo as archived."""
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()
    await client.post("/repos", data={
        "name": "my-project",
        "local_path": str(repo_dir),
    }, follow_redirects=False)
    resp = await client.post("/repos/1/archive", follow_redirects=False)
    assert resp.status_code == 303


# ---- New tests: GET /repos list page ----


@pytest.mark.asyncio
async def test_repo_list_empty(client):
    """GET /repos renders an empty state when no repos exist."""
    resp = await client.get("/repos")
    assert resp.status_code == 200
    assert "No repos yet" in resp.text
    assert "Add one" in resp.text


@pytest.mark.asyncio
async def test_repo_list_shows_repos(client, tmp_path):
    """GET /repos lists all non-archived repos with their names and paths."""
    repo_id = await _seed_repo(client, tmp_path, name="alpha-project", suffix="alpha")
    resp = await client.get("/repos")
    assert resp.status_code == 200
    assert "alpha-project" in resp.text
    assert f"/repos/{repo_id}" in resp.text


@pytest.mark.asyncio
async def test_repo_list_hides_archived(client, tmp_path):
    """GET /repos excludes archived repos by default."""
    await _seed_repo(client, tmp_path, name="visible-repo", suffix="vis")
    repo_id_archived = await _seed_repo(
        client, tmp_path, name="archived-repo", suffix="arch"
    )
    await client.post(f"/repos/{repo_id_archived}/archive", follow_redirects=False)

    resp = await client.get("/repos")
    assert resp.status_code == 200
    assert "visible-repo" in resp.text
    assert "archived-repo" not in resp.text


@pytest.mark.asyncio
async def test_repo_list_show_archived(client, tmp_path):
    """GET /repos?show_archived=true includes archived repos."""
    await _seed_repo(client, tmp_path, name="active-repo", suffix="act")
    repo_id_archived = await _seed_repo(
        client, tmp_path, name="hidden-repo", suffix="hid"
    )
    await client.post(f"/repos/{repo_id_archived}/archive", follow_redirects=False)

    resp = await client.get("/repos?show_archived=true")
    assert resp.status_code == 200
    assert "active-repo" in resp.text
    assert "hidden-repo" in resp.text
    assert "archived" in resp.text.lower()


@pytest.mark.asyncio
async def test_repo_list_pipeline_counts(client, tmp_path):
    """GET /repos shows pipeline counts per repo."""
    repo_id = await _seed_repo(client, tmp_path, name="counted-repo", suffix="cnt")
    def_id = await _seed_pipeline_def(client, name="count-def")
    await _seed_pipeline(repo_id, def_id, status="completed")
    await _seed_pipeline(repo_id, def_id, status="running")

    resp = await client.get("/repos")
    assert resp.status_code == 200
    assert "2 pipelines" in resp.text
    assert "1 running" in resp.text
    assert "1 completed" in resp.text


@pytest.mark.asyncio
async def test_repo_list_no_pipelines(client, tmp_path):
    """GET /repos shows 'No pipelines yet' when a repo has none."""
    await _seed_repo(client, tmp_path, name="empty-repo", suffix="empty")
    resp = await client.get("/repos")
    assert resp.status_code == 200
    assert "No pipelines yet" in resp.text


@pytest.mark.asyncio
async def test_repo_list_latest_pipeline(client, tmp_path):
    """GET /repos shows latest pipeline info with def name and status."""
    repo_id = await _seed_repo(client, tmp_path, name="latest-repo", suffix="lat")
    def_id = await _seed_pipeline_def(client, name="latest-def")
    pid = await _seed_pipeline(repo_id, def_id, status="completed")

    resp = await client.get("/repos")
    assert resp.status_code == 200
    assert "latest-def" in resp.text
    assert f"/pipelines/{pid}" in resp.text


@pytest.mark.asyncio
async def test_repo_list_new_pipeline_link(client, tmp_path):
    """GET /repos shows a 'New Pipeline' link per repo."""
    repo_id = await _seed_repo(client, tmp_path, name="linked-repo", suffix="link")
    resp = await client.get("/repos")
    assert resp.status_code == 200
    assert f"/pipelines/new?repo_id={repo_id}" in resp.text


@pytest.mark.asyncio
async def test_repo_list_nav_link(client):
    """GET /repos page includes 'Repos' in navigation."""
    resp = await client.get("/repos")
    assert resp.status_code == 200
    assert 'href="/repos"' in resp.text


@pytest.mark.asyncio
async def test_repo_list_add_repo_button(client):
    """GET /repos shows an 'Add Repo' button linking to /repos/new."""
    resp = await client.get("/repos")
    assert resp.status_code == 200
    assert "/repos/new" in resp.text


# ---- New repo form (GET /repos/new) ----


@pytest.mark.asyncio
async def test_new_repo_form_renders(client):
    """GET /repos/new returns 200 with the add-repo form page."""
    resp = await client.get("/repos/new")
    assert resp.status_code == 200
    assert "Add Repo" in resp.text


@pytest.mark.asyncio
async def test_new_repo_form_has_required_fields(client):
    """GET /repos/new contains name, local_path, git_url, and default_branch fields."""
    resp = await client.get("/repos/new")
    assert resp.status_code == 200
    body = resp.text
    assert 'name="name"' in body
    assert 'name="local_path"' in body
    assert 'name="git_url"' in body
    assert 'name="default_branch"' in body


@pytest.mark.asyncio
async def test_new_repo_form_posts_to_repos(client):
    """GET /repos/new form action submits to POST /repos."""
    resp = await client.get("/repos/new")
    assert resp.status_code == 200
    assert 'action="/repos"' in resp.text


@pytest.mark.asyncio
async def test_new_repo_form_cancel_link(client):
    """GET /repos/new has a cancel link back to /repos."""
    resp = await client.get("/repos/new")
    assert resp.status_code == 200
    assert 'href="/repos"' in resp.text


@pytest.mark.asyncio
async def test_new_repo_form_uses_styled_template(client):
    """GET /repos/new serves the styled new_repo.html, not the stale add_repo.html.

    Invariant: the form uses new-pipeline-form CSS class and has a styled cancel button.
    Context: a stale duplicate route in dashboard.py previously shadowed the correct route.
    """
    resp = await client.get("/repos/new")
    assert resp.status_code == 200
    assert "new-pipeline-form" in resp.text
    assert "btn-cancel" in resp.text
    assert "field-label" in resp.text


@pytest.mark.asyncio
async def test_new_repo_form_default_branch_value(client):
    """GET /repos/new pre-fills default_branch with 'main'."""
    resp = await client.get("/repos/new")
    assert resp.status_code == 200
    assert 'value="main"' in resp.text


@pytest.mark.asyncio
async def test_new_repo_form_submit_creates_repo(client, tmp_path):
    """Submitting the add-repo form creates a repo and redirects to its detail page."""
    repo_dir = tmp_path / "submit-test"
    repo_dir.mkdir()
    resp = await client.post("/repos", data={
        "name": "submit-repo",
        "local_path": str(repo_dir),
        "default_branch": "main",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/repos/")


# ---- Enhanced repo detail ----


@pytest.mark.asyncio
async def test_repo_detail_shows_pipelines(client, tmp_path):
    """GET /repos/{id} shows pipelines that ran against the repo."""
    repo_id = await _seed_repo(client, tmp_path, name="detail-repo", suffix="det")
    def_id = await _seed_pipeline_def(client, name="detail-def")
    pid = await _seed_pipeline(repo_id, def_id, status="completed")

    resp = await client.get(f"/repos/{repo_id}")
    assert resp.status_code == 200
    assert "detail-def" in resp.text
    assert f"/pipelines/{pid}" in resp.text
    assert "completed" in resp.text


@pytest.mark.asyncio
async def test_repo_detail_no_pipelines(client, tmp_path):
    """GET /repos/{id} shows empty state when no pipelines exist."""
    repo_id = await _seed_repo(client, tmp_path, name="nopipe-repo", suffix="np")
    resp = await client.get(f"/repos/{repo_id}")
    assert resp.status_code == 200
    assert "No pipelines have run" in resp.text


@pytest.mark.asyncio
async def test_repo_detail_new_pipeline_link(client, tmp_path):
    """GET /repos/{id} has a 'New Pipeline' link pre-filled with repo_id."""
    repo_id = await _seed_repo(client, tmp_path, name="newpipe-repo", suffix="newp")
    resp = await client.get(f"/repos/{repo_id}")
    assert resp.status_code == 200
    assert f"/pipelines/new?repo_id={repo_id}" in resp.text


@pytest.mark.asyncio
async def test_repo_detail_404(client):
    """GET /repos/{id} returns 404 for nonexistent repo."""
    resp = await client.get("/repos/99999")
    assert resp.status_code == 404


# ---- Property-based tests ----


# ---- Folder browser (GET /repos/browse) ----


@pytest.mark.asyncio
async def test_browse_default_lists_home(client):
    """GET /repos/browse with no path param lists the user's home directory.

    Invariant: response contains the resolved home path in browse-current.
    """
    resp = await client.get("/repos/browse")
    assert resp.status_code == 200
    assert "browse-current" in resp.text


@pytest.mark.asyncio
async def test_browse_lists_subdirectories(client, tmp_path):
    """GET /repos/browse?path=... returns child directories as browse-entry elements.

    Invariant: every non-hidden subdirectory appears in the listing.
    """
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "file.txt").write_text("hi")

    resp = await client.get(f"/repos/browse?path={tmp_path}")
    assert resp.status_code == 200
    assert "alpha" in resp.text
    assert "beta" in resp.text
    assert "file.txt" not in resp.text


@pytest.mark.asyncio
async def test_browse_hides_dotfiles(client, tmp_path):
    """GET /repos/browse omits directories starting with a dot.

    Invariant: hidden directories are excluded from the listing.
    """
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "visible").mkdir()

    resp = await client.get(f"/repos/browse?path={tmp_path}")
    assert resp.status_code == 200
    assert ".hidden" not in resp.text
    assert "visible" in resp.text


@pytest.mark.asyncio
async def test_browse_nonexistent_path(client, tmp_path):
    """GET /repos/browse with a nonexistent path shows an error message.

    Invariant: the response contains browse-error, not a 4xx status.
    """
    resp = await client.get(f"/repos/browse?path={tmp_path / 'nope'}")
    assert resp.status_code == 200
    assert "browse-error" in resp.text
    assert "not found" in resp.text.lower()


@pytest.mark.asyncio
async def test_browse_shows_parent_link(client, tmp_path):
    """GET /repos/browse includes a parent (..) navigation entry.

    Invariant: the parent link uses the resolved parent path.
    """
    child = tmp_path / "sub"
    child.mkdir()

    resp = await client.get(f"/repos/browse?path={child}")
    assert resp.status_code == 200
    assert "browse-parent" in resp.text
    assert str(tmp_path) in resp.text


@pytest.mark.asyncio
async def test_browse_empty_directory(client, tmp_path):
    """GET /repos/browse on an empty directory shows a 'no subdirectories' message.

    Invariant: browse-empty class appears when there are no child directories.
    """
    empty = tmp_path / "empty"
    empty.mkdir()

    resp = await client.get(f"/repos/browse?path={empty}")
    assert resp.status_code == 200
    assert "browse-empty" in resp.text


@pytest.mark.asyncio
async def test_browse_entries_have_hx_get(client, tmp_path):
    """Each browse entry includes an hx-get attribute for htmx navigation.

    Invariant: every directory entry's hx-get points to /repos/browse with its path.
    """
    (tmp_path / "mydir").mkdir()

    resp = await client.get(f"/repos/browse?path={tmp_path}")
    assert resp.status_code == 200
    assert f'hx-get="/repos/browse?path={tmp_path / "mydir"}"' in resp.text


@pytest.mark.asyncio
async def test_browse_entries_have_data_path(client, tmp_path):
    """Each browse entry includes a data-path attribute for double-click selection.

    Invariant: data-path contains the full resolved path of the directory.
    """
    (tmp_path / "pickme").mkdir()

    resp = await client.get(f"/repos/browse?path={tmp_path}")
    assert resp.status_code == 200
    assert f'data-path="{tmp_path / "pickme"}"' in resp.text


@pytest.mark.asyncio
async def test_new_repo_form_has_browse_button(client):
    """GET /repos/new includes a Browse button for the folder picker.

    Invariant: the form contains the browse toggle and folder picker elements.
    """
    resp = await client.get("/repos/new")
    assert resp.status_code == 200
    assert "browse-toggle" in resp.text
    assert "folder-picker" in resp.text
    assert "folder-list" in resp.text


# ---- Property-based tests ----


@settings(
    max_examples=5,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(n=st.integers(min_value=1, max_value=4))
@pytest.mark.asyncio
async def test_repo_list_contains_all_seeded(initialized_db, tmp_path, n):
    """Every seeded repo appears on the repo list page.

    Invariant: each uniquely-named repo is present in the response HTML.
    """
    import tempfile

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        names = []
        with tempfile.TemporaryDirectory() as td:
            for i in range(n):
                name = f"pbt-{uuid.uuid4().hex[:8]}"
                names.append(name)
                d = Path(td) / name
                d.mkdir()
                await client.post("/repos", data={
                    "name": name,
                    "local_path": str(d),
                }, follow_redirects=False)

            resp = await client.get("/repos")
            assert resp.status_code == 200
            for name in names:
                assert name in resp.text


@settings(
    max_examples=5,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    statuses=st.lists(
        st.sampled_from(["completed", "running", "failed", "pending"]),
        min_size=1,
        max_size=4,
    )
)
@pytest.mark.asyncio
async def test_repo_detail_pipeline_count_matches(initialized_db, tmp_path, statuses):
    """Repo detail page shows exactly the seeded pipelines.

    Invariant: number of pipeline table rows matches len(statuses).
    """
    import tempfile

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td) / f"pbt-{uuid.uuid4().hex[:8]}"
            repo_dir.mkdir()
            resp = await client.post("/repos", data={
                "name": f"pbt-{uuid.uuid4().hex[:8]}",
                "local_path": str(repo_dir),
            }, follow_redirects=False)
            repo_id = int(resp.headers["location"].split("/")[-1])

            def_name = f"pbt-def-{uuid.uuid4().hex[:8]}"
            def_id = await _seed_pipeline_def(client, name=def_name)
            for s in statuses:
                await _seed_pipeline(repo_id, def_id, status=s)

            resp = await client.get(f"/repos/{repo_id}")
            assert resp.status_code == 200
            assert resp.text.count(def_name) == len(statuses)
