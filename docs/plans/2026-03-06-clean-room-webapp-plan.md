# Clean Room Webapp Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a personal FastAPI + HTMX webapp that clones GitHub repos and iteratively generates clean room formal verification specs using Claude Agent SDK.

**Architecture:** Single FastAPI monolith with SQLite (aiosqlite), HTMX server-rendered pages, SSE for live log streaming, asyncio background tasks for job execution via Claude Agent SDK. Persistent repo clones and a specs monorepo on the local filesystem.

**Tech Stack:** Python 3.12, uv, FastAPI, Jinja2, HTMX, aiosqlite, claude_agent_sdk, SSE

**Design doc:** `docs/plans/2026-03-06-clean-room-webapp-design.md`

---

### Task 1: Project Scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `src/clean_room/__init__.py`
- Create: `src/clean_room/main.py`
- Create: `src/clean_room/config.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

**Step 1: Create pyproject.toml**

```toml
[project]
name = "clean-room"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.34",
    "jinja2>=3.1",
    "aiosqlite>=0.20",
    "python-dotenv>=1.0",
    "claude-agent-sdk>=0.1",
    "sse-starlette>=2.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "hypothesis>=6.100",
    "httpx>=0.27",
    "ruff>=0.4",
    "mypy>=1.10",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.ruff]
target-version = "py312"
line-length = 100
```

**Step 2: Create config module**

```python
# src/clean_room/config.py
from pathlib import Path
import os

CLEAN_ROOM_DIR = Path(os.getenv("CLEAN_ROOM_DIR", Path.home() / ".clean-room"))
REPOS_DIR = CLEAN_ROOM_DIR / "repos"
SPECS_MONOREPO_DIR = CLEAN_ROOM_DIR / "specs-monorepo"
DB_PATH = CLEAN_ROOM_DIR / "clean_room.db"
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "claude-sonnet-4-20250514")
```

**Step 3: Create minimal FastAPI app**

```python
# src/clean_room/__init__.py
```

```python
# src/clean_room/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from clean_room.config import CLEAN_ROOM_DIR, REPOS_DIR, SPECS_MONOREPO_DIR


@asynccontextmanager
async def lifespan(app: FastAPI):
    CLEAN_ROOM_DIR.mkdir(parents=True, exist_ok=True)
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    SPECS_MONOREPO_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(lifespan=lifespan)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
```

**Step 4: Create test scaffolding**

```python
# tests/__init__.py
```

```python
# tests/conftest.py
import pytest
from pathlib import Path


@pytest.fixture
def tmp_clean_room(tmp_path):
    """Provide an isolated clean room directory for tests."""
    return tmp_path / "clean-room"
```

**Step 5: Install dependencies and verify**

Run: `uv sync --extra dev`
Expected: Clean install, no errors.

**Step 6: Verify app imports**

Run: `uv run python -c "from clean_room.main import app; print('OK')"`
Expected: `OK`

**Step 7: Commit**

```bash
git init
git add pyproject.toml src/ tests/
git commit -m "feat: project scaffolding with FastAPI, config, and test setup"
```

---

### Task 2: Database Layer

**Files:**
- Create: `src/clean_room/db.py`
- Create: `tests/test_db.py`

**Step 1: Write the failing test for schema creation**

```python
# tests/test_db.py
import pytest
import aiosqlite
from pathlib import Path

from clean_room.db import init_db, get_db


@pytest.fixture
async def db_path(tmp_path):
    path = tmp_path / "test.db"
    await init_db(path)
    return path


@pytest.mark.asyncio
async def test_init_db_creates_all_tables(db_path):
    """Schema init must create repos, prompts, jobs, and job_logs tables."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in await cursor.fetchall()]
    assert "repos" in tables
    assert "prompts" in tables
    assert "jobs" in tables
    assert "job_logs" in tables


@pytest.mark.asyncio
async def test_init_db_seeds_default_prompts(db_path):
    """Schema init must seed the two default prompts."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT name FROM prompts ORDER BY id")
        names = [row[0] for row in await cursor.fetchall()]
    assert "Create Spec" in names
    assert "Improve Spec" in names


@pytest.mark.asyncio
async def test_init_db_is_idempotent(db_path):
    """Running init_db twice must not duplicate seed data or error."""
    await init_db(db_path)
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM prompts")
        count = (await cursor.fetchone())[0]
    assert count == 2


@pytest.mark.asyncio
async def test_foreign_keys_enforced(db_path):
    """Foreign key constraints must be active."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("PRAGMA foreign_keys")
        fk = (await cursor.fetchone())[0]
    assert fk == 1
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_db.py -v`
Expected: ImportError — `db` module doesn't exist yet.

**Step 3: Implement db module**

```python
# src/clean_room/db.py
import aiosqlite
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    github_url TEXT NOT NULL,
    org TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    clone_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    template TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    feature_description TEXT,
    prompt_id INTEGER NOT NULL REFERENCES prompts(id),
    max_iterations INTEGER NOT NULL DEFAULT 20,
    status TEXT NOT NULL DEFAULT 'pending',
    current_iteration INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS job_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    iteration INTEGER NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

DEFAULT_PROMPTS = [
    (
        "Create Spec",
        """Study the existing specs/*

Identify one specification that still needs created for the clean room deep research specifications and create the specification file.

Focus on ONE specification
Include:
- Provable Properties Catalog
- Purity Boundary Map
- Verification Tooling Selection
- Property Specifications""",
    ),
    (
        "Improve Spec",
        """Study the existing specs/*

Identify one specification for the clean room deep research specifications and improve the specification file. Persist changes when done.

Focus on ONE specification
Include:
- Provable Properties Catalog
- Purity Boundary Map
- Verification Tooling Selection
- Property Specifications

Note: If creating diagrams make them mermaid diagrams""",
    ),
]


async def init_db(db_path: Path) -> None:
    """Initialize database schema and seed default prompts."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(SCHEMA)
        cursor = await db.execute("SELECT COUNT(*) FROM prompts")
        count = (await cursor.fetchone())[0]
        if count == 0:
            for name, template in DEFAULT_PROMPTS:
                await db.execute(
                    "INSERT INTO prompts (name, template) VALUES (?, ?)",
                    (name, template),
                )
            await db.commit()


async def get_db(db_path: Path) -> aiosqlite.Connection:
    """Get a database connection with row factory and foreign keys enabled."""
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    return db
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_db.py -v`
Expected: All 4 tests PASS.

**Step 5: Commit**

```bash
git add src/clean_room/db.py tests/test_db.py
git commit -m "feat: database layer with schema, seed prompts, and init_db"
```

---

### Task 3: Pydantic Models

**Files:**
- Create: `src/clean_room/models.py`
- Create: `tests/test_models.py`

**Step 1: Write failing tests for model validation**

```python
# tests/test_models.py
import pytest
from hypothesis import given
from hypothesis import strategies as st

from clean_room.models import GitHubUrl, parse_github_url


class TestParseGitHubUrl:
    def test_parses_standard_url(self):
        """Standard GitHub HTTPS URL parses to org and repo."""
        result = parse_github_url("https://github.com/anthropics/claude-code")
        assert result.org == "anthropics"
        assert result.repo_name == "claude-code"
        assert result.slug == "anthropics--claude-code"

    def test_parses_url_with_trailing_slash(self):
        result = parse_github_url("https://github.com/anthropics/claude-code/")
        assert result.org == "anthropics"
        assert result.repo_name == "claude-code"

    def test_parses_url_with_dot_git(self):
        result = parse_github_url("https://github.com/anthropics/claude-code.git")
        assert result.repo_name == "claude-code"

    def test_rejects_non_github_url(self):
        with pytest.raises(ValueError, match="GitHub"):
            parse_github_url("https://gitlab.com/foo/bar")

    def test_rejects_url_missing_repo(self):
        with pytest.raises(ValueError):
            parse_github_url("https://github.com/anthropics")

    @given(
        org=st.from_regex(r"[a-zA-Z][a-zA-Z0-9\-]{0,30}", fullmatch=True),
        repo=st.from_regex(r"[a-zA-Z][a-zA-Z0-9\-_.]{0,30}", fullmatch=True),
    )
    def test_roundtrip_any_valid_org_repo(self, org, repo):
        """Property: any valid org/repo parses and produces correct slug."""
        url = f"https://github.com/{org}/{repo}"
        result = parse_github_url(url)
        assert result.org == org
        assert result.repo_name == repo
        assert result.slug == f"{org}--{repo}"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py -v`
Expected: ImportError.

**Step 3: Implement models**

```python
# src/clean_room/models.py
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class GitHubUrl:
    org: str
    repo_name: str
    slug: str
    url: str


def parse_github_url(url: str) -> GitHubUrl:
    """Parse a GitHub URL into org, repo_name, and slug."""
    parsed = urlparse(url.strip().rstrip("/"))
    if parsed.hostname != "github.com":
        raise ValueError(f"Not a GitHub URL: {url}")
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"URL must include org and repo: {url}")
    org = parts[0]
    repo_name = parts[1].removesuffix(".git")
    return GitHubUrl(
        org=org,
        repo_name=repo_name,
        slug=f"{org}--{repo_name}",
        url=f"https://github.com/{org}/{repo_name}",
    )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: All tests PASS.

**Step 5: Commit**

```bash
git add src/clean_room/models.py tests/test_models.py
git commit -m "feat: GitHub URL parser with validation and property tests"
```

---

### Task 4: Git Operations

**Files:**
- Create: `src/clean_room/git_ops.py`
- Create: `tests/test_git_ops.py`

**Step 1: Write failing tests**

```python
# tests/test_git_ops.py
import pytest
import subprocess
from pathlib import Path

from clean_room.git_ops import clone_repo, pull_repo, init_specs_monorepo, commit_specs


@pytest.fixture
def fake_remote(tmp_path):
    """Create a bare git repo to act as a fake GitHub remote."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(remote), str(work)], check=True, capture_output=True)
    (work / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=work, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@test.com",
         "commit", "-m", "init"],
        cwd=work, check=True, capture_output=True,
    )
    subprocess.run(["git", "push"], cwd=work, check=True, capture_output=True)
    return remote


@pytest.mark.asyncio
async def test_clone_repo(fake_remote, tmp_path):
    """Clone creates a local copy with files from the remote."""
    dest = tmp_path / "clone"
    await clone_repo(str(fake_remote), dest)
    assert (dest / "README.md").exists()
    assert (dest / "README.md").read_text() == "hello"


@pytest.mark.asyncio
async def test_pull_repo(fake_remote, tmp_path):
    """Pull fetches latest changes into existing clone."""
    dest = tmp_path / "clone"
    await clone_repo(str(fake_remote), dest)
    work = tmp_path / "work2"
    subprocess.run(["git", "clone", str(fake_remote), str(work)], check=True, capture_output=True)
    (work / "new.txt").write_text("new")
    subprocess.run(["git", "add", "."], cwd=work, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@test.com",
         "commit", "-m", "add new"],
        cwd=work, check=True, capture_output=True,
    )
    subprocess.run(["git", "push"], cwd=work, check=True, capture_output=True)
    await pull_repo(dest)
    assert (dest / "new.txt").exists()


@pytest.mark.asyncio
async def test_init_specs_monorepo(tmp_path):
    """Init creates a git repo for specs if it doesn't exist."""
    mono = tmp_path / "specs"
    await init_specs_monorepo(mono)
    assert (mono / ".git").is_dir()
    await init_specs_monorepo(mono)
    assert (mono / ".git").is_dir()


@pytest.mark.asyncio
async def test_commit_specs(tmp_path):
    """Commit stages and commits all changes in the specs monorepo."""
    mono = tmp_path / "specs"
    await init_specs_monorepo(mono)
    slug_dir = mono / "org--repo"
    slug_dir.mkdir()
    (slug_dir / "spec-001.md").write_text("# Spec 1")
    await commit_specs(mono, "Add specs for org/repo")
    result = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=mono, capture_output=True, text=True,
    )
    assert "Add specs for org/repo" in result.stdout
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_git_ops.py -v`
Expected: ImportError.

**Step 3: Implement git_ops**

```python
# src/clean_room/git_ops.py
import asyncio
from pathlib import Path


async def _run(cmd: list[str], cwd: Path | None = None) -> str:
    """Run a subprocess command asynchronously."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{stderr.decode()}")
    return stdout.decode()


async def clone_repo(url: str, dest: Path) -> None:
    """Clone a git repository to dest."""
    await _run(["git", "clone", url, str(dest)])


async def pull_repo(repo_path: Path) -> None:
    """Pull latest changes in an existing clone."""
    await _run(["git", "pull"], cwd=repo_path)


async def init_specs_monorepo(path: Path) -> None:
    """Initialize a git repo for the specs monorepo if it doesn't exist."""
    if not (path / ".git").is_dir():
        path.mkdir(parents=True, exist_ok=True)
        await _run(["git", "init"], cwd=path)
        await _run(["git", "-c", "user.name=clean-room", "-c",
                     "user.email=clean-room@local", "commit",
                     "--allow-empty", "-m", "init specs monorepo"], cwd=path)


async def commit_specs(monorepo_path: Path, message: str) -> None:
    """Stage all changes and commit to the specs monorepo."""
    await _run(["git", "add", "."], cwd=monorepo_path)
    try:
        await _run(["git", "diff", "--cached", "--quiet"], cwd=monorepo_path)
        return  # Nothing to commit
    except RuntimeError:
        pass  # There are changes
    await _run(
        ["git", "-c", "user.name=clean-room", "-c", "user.email=clean-room@local",
         "commit", "-m", message],
        cwd=monorepo_path,
    )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_git_ops.py -v`
Expected: All 4 tests PASS.

**Step 5: Commit**

```bash
git add src/clean_room/git_ops.py tests/test_git_ops.py
git commit -m "feat: git operations for clone, pull, and specs monorepo"
```

---

### Task 5: Prompt CRUD Routes

**Files:**
- Create: `src/clean_room/routes/__init__.py`
- Create: `src/clean_room/routes/prompts.py`
- Create: `src/clean_room/templates/base.html`
- Create: `src/clean_room/templates/prompts.html`
- Create: `src/clean_room/templates/partials/prompt_row.html`
- Create: `src/clean_room/templates/partials/prompt_form.html`
- Create: `static/style.css`
- Create: `tests/test_routes_prompts.py`

**Step 1: Write failing test for prompt list endpoint**

```python
# tests/test_routes_prompts.py
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
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_routes_prompts.py -v`
Expected: ImportError.

**Step 3: Create templates and static CSS**

Create `src/clean_room/templates/base.html`:
```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}Clean Room{% endblock %}</title>
    <script src="https://unpkg.com/htmx.org@2.0.4"></script>
    <script src="https://unpkg.com/htmx-ext-sse@2.2.2/sse.js"></script>
    <link rel="stylesheet" href="/static/style.css">
</head>
<body>
    <nav>
        <a href="/">Dashboard</a>
        <a href="/prompts">Prompts</a>
    </nav>
    <main>
        {% block content %}{% endblock %}
    </main>
</body>
</html>
```

Create `src/clean_room/templates/prompts.html`:
```html
{% extends "base.html" %}
{% block title %}Prompts - Clean Room{% endblock %}
{% block content %}
<h1>Prompts</h1>
<table id="prompts-table">
    <thead>
        <tr><th>Name</th><th>Preview</th><th>Actions</th></tr>
    </thead>
    <tbody id="prompt-list">
        {% for prompt in prompts %}
        {% include "partials/prompt_row.html" %}
        {% endfor %}
    </tbody>
</table>
<h2>New Prompt</h2>
<form hx-post="/prompts" hx-target="#prompt-list" hx-swap="beforeend">
    <label>Name <input type="text" name="name" required></label>
    <label>Template <textarea name="template" rows="10" required></textarea></label>
    <button type="submit">Create</button>
</form>
{% endblock %}
```

Create `src/clean_room/templates/partials/prompt_row.html`:
```html
<tr id="prompt-{{ prompt.id }}">
    <td>{{ prompt.name }}</td>
    <td><code>{{ prompt.template[:100] }}{% if prompt.template|length > 100 %}...{% endif %}</code></td>
    <td>
        <button hx-get="/prompts/{{ prompt.id }}/edit" hx-target="#prompt-{{ prompt.id }}" hx-swap="outerHTML">Edit</button>
        <button hx-delete="/prompts/{{ prompt.id }}" hx-target="#prompt-{{ prompt.id }}" hx-swap="outerHTML">Delete</button>
    </td>
</tr>
```

Create `src/clean_room/templates/partials/prompt_form.html`:
```html
<tr id="prompt-{{ prompt.id }}">
    <td colspan="3">
        <form hx-put="/prompts/{{ prompt.id }}" hx-target="#prompt-{{ prompt.id }}" hx-swap="outerHTML">
            <label>Name <input type="text" name="name" value="{{ prompt.name }}" required></label>
            <label>Template <textarea name="template" rows="10" required>{{ prompt.template }}</textarea></label>
            <button type="submit">Save</button>
            <button hx-get="/prompts/{{ prompt.id }}/row" hx-target="#prompt-{{ prompt.id }}" hx-swap="outerHTML">Cancel</button>
        </form>
    </td>
</tr>
```

Create `static/style.css`:
```css
body { font-family: system-ui, sans-serif; max-width: 960px; margin: 0 auto; padding: 1rem; }
nav { display: flex; gap: 1rem; padding: 0.5rem 0; border-bottom: 1px solid #ccc; margin-bottom: 1rem; }
nav a { text-decoration: none; color: #333; font-weight: 600; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 0.5rem; text-align: left; border-bottom: 1px solid #eee; }
code { font-size: 0.85em; }
textarea { font-family: monospace; width: 100%; }
button { cursor: pointer; padding: 0.25rem 0.75rem; }
label { display: block; margin: 0.5rem 0; }
input[type="text"], input[type="url"], input[type="number"] { width: 100%; padding: 0.25rem; }
.status-badge { padding: 0.15rem 0.5rem; border-radius: 3px; font-size: 0.85em; }
.status-running { background: #dff0d8; }
.status-completed { background: #d9edf7; }
.status-stopped { background: #fcf8e3; }
.status-failed { background: #f2dede; }
.status-pending { background: #eee; }
.log-stream { background: #1e1e1e; color: #d4d4d4; font-family: monospace; font-size: 0.85em; padding: 1rem; height: 500px; overflow-y: auto; white-space: pre-wrap; word-wrap: break-word; }
```

**Step 4: Implement prompts route**

```python
# src/clean_room/routes/__init__.py
```

```python
# src/clean_room/routes/prompts.py
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse

from clean_room.config import DB_PATH
from clean_room.db import get_db

router = APIRouter(prefix="/prompts")


@router.get("", response_class=HTMLResponse)
async def list_prompts(request: Request):
    from clean_room.main import templates
    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute("SELECT * FROM prompts ORDER BY id")
        prompts = await cursor.fetchall()
        return templates.TemplateResponse("prompts.html", {
            "request": request, "prompts": prompts,
        })
    finally:
        await db.close()


@router.post("", response_class=HTMLResponse)
async def create_prompt(request: Request, name: str = Form(), template: str = Form()):
    from clean_room.main import templates
    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute(
            "INSERT INTO prompts (name, template) VALUES (?, ?) RETURNING *",
            (name, template),
        )
        prompt = await cursor.fetchone()
        await db.commit()
        return templates.TemplateResponse("partials/prompt_row.html", {
            "request": request, "prompt": prompt,
        })
    finally:
        await db.close()


@router.put("/{prompt_id}", response_class=HTMLResponse)
async def update_prompt(
    request: Request, prompt_id: int, name: str = Form(), template: str = Form(),
):
    from clean_room.main import templates
    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute(
            "UPDATE prompts SET name=?, template=?, updated_at=datetime('now') "
            "WHERE id=? RETURNING *",
            (name, template, prompt_id),
        )
        prompt = await cursor.fetchone()
        await db.commit()
        return templates.TemplateResponse("partials/prompt_row.html", {
            "request": request, "prompt": prompt,
        })
    finally:
        await db.close()


@router.delete("/{prompt_id}", response_class=HTMLResponse)
async def delete_prompt(prompt_id: int):
    db = await get_db(DB_PATH)
    try:
        await db.execute("DELETE FROM prompts WHERE id=?", (prompt_id,))
        await db.commit()
        return HTMLResponse("")
    finally:
        await db.close()


@router.get("/{prompt_id}/edit", response_class=HTMLResponse)
async def edit_prompt_form(request: Request, prompt_id: int):
    from clean_room.main import templates
    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,))
        prompt = await cursor.fetchone()
        return templates.TemplateResponse("partials/prompt_form.html", {
            "request": request, "prompt": prompt,
        })
    finally:
        await db.close()


@router.get("/{prompt_id}/row", response_class=HTMLResponse)
async def prompt_row(request: Request, prompt_id: int):
    from clean_room.main import templates
    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,))
        prompt = await cursor.fetchone()
        return templates.TemplateResponse("partials/prompt_row.html", {
            "request": request, "prompt": prompt,
        })
    finally:
        await db.close()
```

**Step 5: Update main.py — register prompts route, mount static, init DB in lifespan**

Add to `src/clean_room/main.py`:
```python
from clean_room.config import CLEAN_ROOM_DIR, REPOS_DIR, SPECS_MONOREPO_DIR, DB_PATH
from clean_room.db import init_db
from clean_room.routes.prompts import router as prompts_router

# In lifespan, after mkdir calls:
    await init_db(DB_PATH)

# After app creation:
app.mount("/static", StaticFiles(directory=str(BASE_DIR.parent.parent / "static")), name="static")
app.include_router(prompts_router)
```

**Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_routes_prompts.py -v`
Expected: All 4 tests PASS.

**Step 7: Commit**

```bash
git add src/clean_room/routes/ src/clean_room/templates/ static/ tests/test_routes_prompts.py
git commit -m "feat: prompt CRUD with HTMX partials and templates"
```

---

### Task 6: Repo Management Routes

**Files:**
- Create: `src/clean_room/routes/repos.py`
- Create: `src/clean_room/templates/repo_detail.html`
- Create: `tests/test_routes_repos.py`

**Step 1: Write failing tests**

```python
# tests/test_routes_repos.py
import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, AsyncMock

from clean_room.db import init_db
from clean_room.main import app


@pytest.fixture
async def test_app(tmp_path):
    db_path = tmp_path / "test.db"
    repos_dir = tmp_path / "repos"
    repos_dir.mkdir()
    await init_db(db_path)
    with patch("clean_room.routes.repos.DB_PATH", db_path), \
         patch("clean_room.routes.repos.REPOS_DIR", repos_dir), \
         patch("clean_room.routes.repos.clone_repo", new_callable=AsyncMock):
        yield app


@pytest.fixture
async def client(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_add_repo(client):
    """POST /repos creates a repo record and triggers clone."""
    resp = await client.post("/repos", data={
        "github_url": "https://github.com/anthropics/claude-code",
    }, follow_redirects=False)
    assert resp.status_code == 303


@pytest.mark.asyncio
async def test_repo_detail(client):
    """GET /repos/{id} shows repo info and jobs list."""
    await client.post("/repos", data={
        "github_url": "https://github.com/anthropics/claude-code",
    }, follow_redirects=False)
    resp = await client.get("/repos/1")
    assert resp.status_code == 200
    assert "anthropics" in resp.text
    assert "claude-code" in resp.text


@pytest.mark.asyncio
async def test_archive_repo(client):
    """POST /repos/{id}/archive sets status to archived."""
    await client.post("/repos", data={
        "github_url": "https://github.com/anthropics/claude-code",
    }, follow_redirects=False)
    resp = await client.post("/repos/1/archive", follow_redirects=False)
    assert resp.status_code == 303
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_routes_repos.py -v`
Expected: ImportError.

**Step 3: Create repo detail template**

Create `src/clean_room/templates/repo_detail.html`:
```html
{% extends "base.html" %}
{% block title %}{{ repo.org }}/{{ repo.repo_name }} - Clean Room{% endblock %}
{% block content %}
<h1>{{ repo.org }}/{{ repo.repo_name }}</h1>
<p>
    <a href="{{ repo.github_url }}" target="_blank">GitHub</a>
    | Status: <span class="status-badge status-{{ repo.status }}">{{ repo.status }}</span>
    | <form style="display:inline" method="post" action="/repos/{{ repo.id }}/archive">
        <button type="submit">Archive</button>
    </form>
</p>

<h2>New Job</h2>
<form method="post" action="/jobs">
    <input type="hidden" name="repo_id" value="{{ repo.id }}">
    <label>Prompt
        <select name="prompt_id" required>
            {% for prompt in prompts %}
            <option value="{{ prompt.id }}">{{ prompt.name }}</option>
            {% endfor %}
        </select>
    </label>
    <label>Feature Description (optional)
        <textarea name="feature_description" rows="3" placeholder="e.g. the authentication system"></textarea>
    </label>
    <label>Max Iterations
        <input type="number" name="max_iterations" value="20" min="1" max="100">
    </label>
    <button type="submit">Start Job</button>
</form>

<h2>Jobs</h2>
<table>
    <thead>
        <tr><th>ID</th><th>Prompt</th><th>Status</th><th>Iteration</th><th>Started</th><th></th></tr>
    </thead>
    <tbody>
        {% for job in jobs %}
        <tr>
            <td><a href="/jobs/{{ job.id }}">{{ job.id }}</a></td>
            <td>{{ job.prompt_name }}</td>
            <td><span class="status-badge status-{{ job.status }}">{{ job.status }}</span></td>
            <td>{{ job.current_iteration }}/{{ job.max_iterations }}</td>
            <td>{{ job.started_at or "-" }}</td>
            <td><a href="/jobs/{{ job.id }}">View</a></td>
        </tr>
        {% endfor %}
    </tbody>
</table>
{% endblock %}
```

**Step 4: Implement repos route**

```python
# src/clean_room/routes/repos.py
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from clean_room.config import DB_PATH, REPOS_DIR
from clean_room.db import get_db
from clean_room.models import parse_github_url
from clean_room.git_ops import clone_repo

router = APIRouter(prefix="/repos")


@router.post("", response_class=RedirectResponse)
async def add_repo(github_url: str = Form()):
    parsed = parse_github_url(github_url)
    clone_path = REPOS_DIR / parsed.slug
    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute(
            "INSERT INTO repos (github_url, org, repo_name, slug, clone_path) "
            "VALUES (?, ?, ?, ?, ?) RETURNING id",
            (parsed.url, parsed.org, parsed.repo_name, parsed.slug, str(clone_path)),
        )
        row = await cursor.fetchone()
        repo_id = row[0]
        await db.commit()
    finally:
        await db.close()
    if not clone_path.exists():
        await clone_repo(parsed.url, clone_path)
    return RedirectResponse(f"/repos/{repo_id}", status_code=303)


@router.get("/{repo_id}", response_class=HTMLResponse)
async def repo_detail(request: Request, repo_id: int):
    from clean_room.main import templates
    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute("SELECT * FROM repos WHERE id=?", (repo_id,))
        repo = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT j.*, p.name as prompt_name FROM jobs j "
            "JOIN prompts p ON j.prompt_id = p.id "
            "WHERE j.repo_id=? ORDER BY j.id DESC",
            (repo_id,),
        )
        jobs = await cursor.fetchall()
        cursor = await db.execute("SELECT * FROM prompts ORDER BY id")
        prompts = await cursor.fetchall()
        return templates.TemplateResponse("repo_detail.html", {
            "request": request, "repo": repo, "jobs": jobs, "prompts": prompts,
        })
    finally:
        await db.close()


@router.post("/{repo_id}/archive", response_class=RedirectResponse)
async def archive_repo(repo_id: int):
    db = await get_db(DB_PATH)
    try:
        await db.execute("UPDATE repos SET status='archived' WHERE id=?", (repo_id,))
        await db.commit()
    finally:
        await db.close()
    return RedirectResponse(f"/repos/{repo_id}", status_code=303)
```

**Step 5: Register route in main.py**

Add to `src/clean_room/main.py`:
```python
from clean_room.routes.repos import router as repos_router
app.include_router(repos_router)
```

**Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_routes_repos.py -v`
Expected: All 3 tests PASS.

**Step 7: Commit**

```bash
git add src/clean_room/routes/repos.py src/clean_room/templates/repo_detail.html tests/test_routes_repos.py
git commit -m "feat: repo management with add, detail, and archive"
```

---

### Task 7: SSE Streaming Infrastructure

**Files:**
- Create: `src/clean_room/streaming.py`
- Create: `tests/test_streaming.py`

**Step 1: Write failing tests**

```python
# tests/test_streaming.py
import pytest
import asyncio

from clean_room.streaming import LogBuffer


@pytest.mark.asyncio
async def test_append_and_read():
    """Appending a message makes it available to readers."""
    buf = LogBuffer()
    buf.append(1, "hello")
    messages = buf.get_history(1)
    assert messages == ["hello"]


@pytest.mark.asyncio
async def test_subscribe_receives_new_messages():
    """Subscriber receives messages appended after subscription."""
    buf = LogBuffer()
    received = []

    async def reader():
        async for msg in buf.subscribe(1):
            received.append(msg)
            if len(received) >= 2:
                break

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.01)
    buf.append(1, "first")
    buf.append(1, "second")
    await asyncio.wait_for(task, timeout=1.0)
    assert received == ["first", "second"]


@pytest.mark.asyncio
async def test_close_terminates_subscribers():
    """Closing a job's buffer terminates all active subscribers."""
    buf = LogBuffer()
    received = []

    async def reader():
        async for msg in buf.subscribe(1):
            received.append(msg)

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.01)
    buf.append(1, "msg")
    buf.close(1)
    await asyncio.wait_for(task, timeout=1.0)
    assert received == ["msg"]


@pytest.mark.asyncio
async def test_multiple_jobs_isolated():
    """Messages for different jobs don't leak across subscribers."""
    buf = LogBuffer()
    buf.append(1, "job1")
    buf.append(2, "job2")
    assert buf.get_history(1) == ["job1"]
    assert buf.get_history(2) == ["job2"]
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_streaming.py -v`
Expected: ImportError.

**Step 3: Implement LogBuffer**

```python
# src/clean_room/streaming.py
import asyncio
from collections import defaultdict


class LogBuffer:
    """In-memory log buffer with pub/sub for SSE streaming."""

    def __init__(self):
        self._history: dict[int, list[str]] = defaultdict(list)
        self._subscribers: dict[int, list[asyncio.Queue]] = defaultdict(list)
        self._closed: set[int] = set()

    def append(self, job_id: int, message: str) -> None:
        """Append a message and notify all subscribers."""
        self._history[job_id].append(message)
        for queue in self._subscribers[job_id]:
            queue.put_nowait(message)

    def get_history(self, job_id: int) -> list[str]:
        """Get all historical messages for a job."""
        return list(self._history[job_id])

    async def subscribe(self, job_id: int):
        """Async generator that yields new messages for a job."""
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._subscribers[job_id].append(queue)
        try:
            while True:
                if job_id in self._closed and queue.empty():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=0.5)
                    if msg is None:
                        break
                    yield msg
                except asyncio.TimeoutError:
                    if job_id in self._closed:
                        break
        finally:
            self._subscribers[job_id].remove(queue)

    def close(self, job_id: int) -> None:
        """Signal that no more messages will be sent for this job."""
        self._closed.add(job_id)
        for queue in self._subscribers[job_id]:
            queue.put_nowait(None)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_streaming.py -v`
Expected: All 4 tests PASS.

**Step 5: Commit**

```bash
git add src/clean_room/streaming.py tests/test_streaming.py
git commit -m "feat: in-memory log buffer with pub/sub for SSE streaming"
```

---

### Task 8: Job Runner with Claude Agent SDK

**Files:**
- Create: `src/clean_room/runner.py`
- Create: `tests/test_runner.py`

**Step 1: Write failing tests**

```python
# tests/test_runner.py
import pytest
import asyncio
from unittest.mock import AsyncMock
from pathlib import Path

from clean_room.runner import JobRunner
from clean_room.streaming import LogBuffer


@pytest.fixture
def log_buffer():
    return LogBuffer()


@pytest.mark.asyncio
async def test_runner_respects_cancellation(log_buffer):
    """Runner stops iterating when cancel event is set."""
    cancel_event = asyncio.Event()
    cancel_event.set()  # Pre-cancelled

    runner = JobRunner(
        job_id=1,
        repo_path=Path("/tmp/fake"),
        prompt="test prompt",
        max_iterations=10,
        log_buffer=log_buffer,
        cancel_event=cancel_event,
    )

    with pytest.MonkeyPatch.context() as m:
        mock_agent = AsyncMock()
        m.setattr(runner, "_run_agent_iteration", mock_agent)
        await runner.run(db=AsyncMock())
        mock_agent.assert_not_called()


@pytest.mark.asyncio
async def test_runner_iterates_up_to_max(log_buffer):
    """Runner calls agent for each iteration up to max."""
    runner = JobRunner(
        job_id=1,
        repo_path=Path("/tmp/fake"),
        prompt="test prompt",
        max_iterations=3,
        log_buffer=log_buffer,
        cancel_event=asyncio.Event(),
    )

    with pytest.MonkeyPatch.context() as m:
        mock_agent = AsyncMock(return_value="iteration output")
        m.setattr(runner, "_run_agent_iteration", mock_agent)
        await runner.run(db=AsyncMock())
        assert mock_agent.call_count == 3


@pytest.mark.asyncio
async def test_runner_logs_each_iteration(log_buffer):
    """Runner appends to log buffer for each iteration."""
    runner = JobRunner(
        job_id=1,
        repo_path=Path("/tmp/fake"),
        prompt="test prompt",
        max_iterations=2,
        log_buffer=log_buffer,
        cancel_event=asyncio.Event(),
    )

    with pytest.MonkeyPatch.context() as m:
        m.setattr(runner, "_run_agent_iteration", AsyncMock(return_value="output"))
        await runner.run(db=AsyncMock())
    history = log_buffer.get_history(1)
    assert len(history) >= 2


@pytest.mark.asyncio
async def test_runner_closes_buffer_on_completion(log_buffer):
    """Runner closes the log buffer when done."""
    runner = JobRunner(
        job_id=1,
        repo_path=Path("/tmp/fake"),
        prompt="test prompt",
        max_iterations=1,
        log_buffer=log_buffer,
        cancel_event=asyncio.Event(),
    )

    with pytest.MonkeyPatch.context() as m:
        m.setattr(runner, "_run_agent_iteration", AsyncMock(return_value="done"))
        await runner.run(db=AsyncMock())
    assert 1 in log_buffer._closed
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner.py -v`
Expected: ImportError.

**Step 3: Implement runner**

```python
# src/clean_room/runner.py
import asyncio
from pathlib import Path

from clean_room.streaming import LogBuffer


class JobRunner:
    """Runs iterative Claude Agent SDK loops for a job."""

    def __init__(
        self,
        job_id: int,
        repo_path: Path,
        prompt: str,
        max_iterations: int,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
    ):
        self.job_id = job_id
        self.repo_path = repo_path
        self.prompt = prompt
        self.max_iterations = max_iterations
        self.log_buffer = log_buffer
        self.cancel_event = cancel_event

    async def run(self, db) -> None:
        """Execute the iteration loop."""
        try:
            for iteration in range(1, self.max_iterations + 1):
                if self.cancel_event.is_set():
                    self.log_buffer.append(
                        self.job_id, f"--- Stopped at iteration {iteration} ---"
                    )
                    break

                self.log_buffer.append(
                    self.job_id,
                    f"=== Starting iteration {iteration}/{self.max_iterations} ===",
                )

                output = await self._run_agent_iteration(iteration)

                self.log_buffer.append(self.job_id, output)
                self.log_buffer.append(
                    self.job_id,
                    f"=== Completed iteration {iteration}/{self.max_iterations} ===",
                )

                await db.execute(
                    "INSERT INTO job_logs (job_id, iteration, content) VALUES (?, ?, ?)",
                    (self.job_id, iteration, output),
                )
                await db.execute(
                    "UPDATE jobs SET current_iteration=? WHERE id=?",
                    (iteration, self.job_id),
                )
                await db.commit()
        except Exception as e:
            self.log_buffer.append(self.job_id, f"ERROR: {e}")
            await db.execute(
                "UPDATE jobs SET status='failed', completed_at=datetime('now') WHERE id=?",
                (self.job_id,),
            )
            await db.commit()
            raise
        finally:
            self.log_buffer.close(self.job_id)

    async def _run_agent_iteration(self, iteration: int) -> str:
        """Run a single Claude Agent SDK iteration.

        Uses claude_agent_sdk to run an agent with filesystem access
        scoped to self.repo_path.
        """
        from claude_agent_sdk import Agent

        agent = Agent(
            model="claude-sonnet-4-20250514",
            instructions=self.prompt,
        )
        result = await agent.run()
        return result.output
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_runner.py -v`
Expected: All 4 tests PASS.

**Step 5: Commit**

```bash
git add src/clean_room/runner.py tests/test_runner.py
git commit -m "feat: job runner with iteration loop, cancellation, and log buffer"
```

---

### Task 9: Job Routes (Create, View, Stop, Stream)

**Files:**
- Create: `src/clean_room/routes/jobs.py`
- Create: `src/clean_room/templates/job_viewer.html`
- Create: `tests/test_routes_jobs.py`

**Step 1: Write failing tests**

```python
# tests/test_routes_jobs.py
import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, AsyncMock
import aiosqlite

from clean_room.db import init_db
from clean_room.main import app


@pytest.fixture
async def test_app(tmp_path):
    db_path = tmp_path / "test.db"
    repos_dir = tmp_path / "repos"
    repos_dir.mkdir()
    await init_db(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO repos (github_url, org, repo_name, slug, clone_path) "
            "VALUES (?, ?, ?, ?, ?)",
            ("https://github.com/test/repo", "test", "repo", "test--repo",
             str(repos_dir / "test--repo")),
        )
        await db.commit()
    with patch("clean_room.routes.jobs.DB_PATH", db_path), \
         patch("clean_room.routes.repos.DB_PATH", db_path), \
         patch("clean_room.routes.prompts.DB_PATH", db_path), \
         patch("clean_room.main.DB_PATH", db_path):
        yield app


@pytest.fixture
async def client(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_create_job(client):
    """POST /jobs creates a job and redirects to viewer."""
    resp = await client.post("/jobs", data={
        "repo_id": "1",
        "prompt_id": "1",
        "feature_description": "auth system",
        "max_iterations": "5",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "/jobs/" in resp.headers["location"]


@pytest.mark.asyncio
async def test_job_viewer(client):
    """GET /jobs/{id} returns the job viewer page."""
    await client.post("/jobs", data={
        "repo_id": "1",
        "prompt_id": "1",
        "max_iterations": "5",
    }, follow_redirects=False)
    resp = await client.get("/jobs/1")
    assert resp.status_code == 200
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_routes_jobs.py -v`
Expected: ImportError.

**Step 3: Create job viewer template**

Create `src/clean_room/templates/job_viewer.html`:
```html
{% extends "base.html" %}
{% block title %}Job #{{ job.id }} - Clean Room{% endblock %}
{% block content %}
<p><a href="/repos/{{ job.repo_id }}">&larr; Back to {{ repo.org }}/{{ repo.repo_name }}</a></p>
<h1>Job #{{ job.id }}</h1>
<div>
    <span class="status-badge status-{{ job.status }}">{{ job.status }}</span>
    Iteration: {{ job.current_iteration }}/{{ job.max_iterations }}
    | Prompt: {{ prompt_name }}
    {% if job.feature_description %}| Feature: {{ job.feature_description }}{% endif %}
</div>
<div style="margin: 1rem 0;">
    {% if job.status == 'running' %}
    <form method="post" action="/jobs/{{ job.id }}/stop" style="display:inline">
        <button type="submit">Stop</button>
    </form>
    {% endif %}
    {% if job.status in ('stopped', 'failed') %}
    <form method="post" action="/jobs/{{ job.id }}/restart" style="display:inline">
        <button type="submit">Restart</button>
    </form>
    {% endif %}
</div>

<h2>Log</h2>
<div class="log-stream"
     id="log"
     hx-ext="sse"
     sse-connect="/jobs/{{ job.id }}/stream"
     sse-swap="message"
     hx-swap="beforeend">
    {%- for log in logs %}{{ log.content }}
{% endfor -%}
</div>

<script>
    const log = document.getElementById('log');
    const observer = new MutationObserver(() => {
        log.scrollTop = log.scrollHeight;
    });
    observer.observe(log, { childList: true });
</script>
{% endblock %}
```

**Step 4: Implement jobs route**

```python
# src/clean_room/routes/jobs.py
import asyncio
import shutil
from pathlib import Path
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sse_starlette.sse import EventSourceResponse

from clean_room.config import DB_PATH, SPECS_MONOREPO_DIR
from clean_room.db import get_db
from clean_room.runner import JobRunner
from clean_room.streaming import LogBuffer
from clean_room.git_ops import pull_repo, commit_specs

router = APIRouter(prefix="/jobs")

log_buffer = LogBuffer()
active_jobs: dict[int, asyncio.Event] = {}
running_tasks: dict[int, asyncio.Task] = {}


async def _start_job(job_id: int, repo_path: Path, prompt: str, max_iterations: int):
    """Background task that runs the job."""
    db = await get_db(DB_PATH)
    try:
        await db.execute(
            "UPDATE jobs SET status='running', started_at=datetime('now') WHERE id=?",
            (job_id,),
        )
        await db.commit()

        try:
            await pull_repo(repo_path)
        except Exception:
            pass

        cancel_event = active_jobs[job_id]
        runner = JobRunner(
            job_id=job_id,
            repo_path=repo_path,
            prompt=prompt,
            max_iterations=max_iterations,
            log_buffer=log_buffer,
            cancel_event=cancel_event,
        )
        await runner.run(db=db)

        status = "stopped" if cancel_event.is_set() else "completed"
        await db.execute(
            "UPDATE jobs SET status=?, completed_at=datetime('now') WHERE id=?",
            (status, job_id),
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT r.slug FROM repos r JOIN jobs j ON j.repo_id=r.id WHERE j.id=?",
            (job_id,),
        )
        row = await cursor.fetchone()
        if row:
            slug = row[0]
            specs_src = repo_path / "specs"
            specs_dest = SPECS_MONOREPO_DIR / slug
            if specs_src.is_dir():
                specs_dest.mkdir(parents=True, exist_ok=True)
                for f in specs_src.iterdir():
                    shutil.copy2(f, specs_dest / f.name)
                await commit_specs(
                    SPECS_MONOREPO_DIR,
                    f"{'Partial specs' if cancel_event.is_set() else 'Specs'} for {slug}",
                )
    except Exception:
        await db.execute(
            "UPDATE jobs SET status='failed', completed_at=datetime('now') WHERE id=?",
            (job_id,),
        )
        await db.commit()
    finally:
        await db.close()
        active_jobs.pop(job_id, None)
        running_tasks.pop(job_id, None)


@router.post("", response_class=RedirectResponse)
async def create_job(
    repo_id: int = Form(),
    prompt_id: int = Form(),
    feature_description: str = Form(""),
    max_iterations: int = Form(20),
):
    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute(
            "INSERT INTO jobs (repo_id, prompt_id, feature_description, max_iterations) "
            "VALUES (?, ?, ?, ?) RETURNING id",
            (repo_id, prompt_id, feature_description or None, max_iterations),
        )
        row = await cursor.fetchone()
        job_id = row[0]
        await db.commit()

        cursor = await db.execute("SELECT clone_path FROM repos WHERE id=?", (repo_id,))
        repo_row = await cursor.fetchone()
        cursor = await db.execute("SELECT template FROM prompts WHERE id=?", (prompt_id,))
        prompt_row = await cursor.fetchone()

        repo_path = Path(repo_row[0])
        prompt = prompt_row[0]
        if feature_description:
            prompt = f"Feature focus: {feature_description}\n\n{prompt}"
    finally:
        await db.close()

    cancel_event = asyncio.Event()
    active_jobs[job_id] = cancel_event
    task = asyncio.create_task(_start_job(job_id, repo_path, prompt, max_iterations))
    running_tasks[job_id] = task

    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.get("/{job_id}", response_class=HTMLResponse)
async def job_viewer(request: Request, job_id: int):
    from clean_room.main import templates
    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        job = await cursor.fetchone()
        cursor = await db.execute("SELECT * FROM repos WHERE id=?", (job["repo_id"],))
        repo = await cursor.fetchone()
        cursor = await db.execute("SELECT name FROM prompts WHERE id=?", (job["prompt_id"],))
        prompt_row = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT * FROM job_logs WHERE job_id=? ORDER BY id", (job_id,),
        )
        logs = await cursor.fetchall()
        return templates.TemplateResponse("job_viewer.html", {
            "request": request, "job": job, "repo": repo,
            "prompt_name": prompt_row[0], "logs": logs,
        })
    finally:
        await db.close()


@router.post("/{job_id}/stop", response_class=RedirectResponse)
async def stop_job(job_id: int):
    if job_id in active_jobs:
        active_jobs[job_id].set()
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/{job_id}/restart", response_class=RedirectResponse)
async def restart_job(job_id: int):
    """Create a new job with the same parameters."""
    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        old = await cursor.fetchone()
        cursor = await db.execute(
            "INSERT INTO jobs (repo_id, prompt_id, feature_description, max_iterations) "
            "VALUES (?, ?, ?, ?) RETURNING id",
            (old["repo_id"], old["prompt_id"], old["feature_description"],
             old["max_iterations"]),
        )
        row = await cursor.fetchone()
        new_id = row[0]
        await db.commit()

        cursor = await db.execute(
            "SELECT clone_path FROM repos WHERE id=?", (old["repo_id"],),
        )
        repo_row = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT template FROM prompts WHERE id=?", (old["prompt_id"],),
        )
        prompt_row = await cursor.fetchone()

        repo_path = Path(repo_row[0])
        prompt = prompt_row[0]
        if old["feature_description"]:
            prompt = f"Feature focus: {old['feature_description']}\n\n{prompt}"
    finally:
        await db.close()

    cancel_event = asyncio.Event()
    active_jobs[new_id] = cancel_event
    task = asyncio.create_task(
        _start_job(new_id, repo_path, prompt, old["max_iterations"])
    )
    running_tasks[new_id] = task

    return RedirectResponse(f"/jobs/{new_id}", status_code=303)


@router.get("/{job_id}/stream")
async def job_stream(job_id: int):
    async def event_generator():
        for msg in log_buffer.get_history(job_id):
            yield {"data": msg}
        async for msg in log_buffer.subscribe(job_id):
            yield {"data": msg}

    return EventSourceResponse(event_generator())
```

**Step 5: Register route in main.py**

Add to `src/clean_room/main.py`:
```python
from clean_room.routes.jobs import router as jobs_router
app.include_router(jobs_router)
```

**Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_routes_jobs.py -v`
Expected: All 2 tests PASS.

**Step 7: Commit**

```bash
git add src/clean_room/routes/jobs.py src/clean_room/templates/job_viewer.html tests/test_routes_jobs.py
git commit -m "feat: job routes with create, view, stop, restart, and SSE streaming"
```

---

### Task 10: Dashboard Route

**Files:**
- Create: `src/clean_room/routes/dashboard.py`
- Create: `src/clean_room/templates/dashboard.html`
- Create: `src/clean_room/templates/add_repo.html`
- Create: `tests/test_routes_dashboard.py`

**Step 1: Write failing test**

```python
# tests/test_routes_dashboard.py
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
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_routes_dashboard.py -v`
Expected: ImportError.

**Step 3: Create templates**

Create `src/clean_room/templates/dashboard.html`:
```html
{% extends "base.html" %}
{% block title %}Dashboard - Clean Room{% endblock %}
{% block content %}
<h1>Clean Room</h1>
<p><a href="/repos/new">Add Repo</a></p>

{% if repos %}
<table>
    <thead>
        <tr><th>Repo</th><th>Last Job</th><th>Last Run</th></tr>
    </thead>
    <tbody>
        {% for repo in repos %}
        <tr>
            <td><a href="/repos/{{ repo.id }}">{{ repo.slug }}</a></td>
            <td>
                {% if repo.last_status %}
                <span class="status-badge status-{{ repo.last_status }}">{{ repo.last_status }}</span>
                {% else %}
                <span class="status-badge status-pending">no jobs</span>
                {% endif %}
            </td>
            <td>{{ repo.last_run or "-" }}</td>
        </tr>
        {% endfor %}
    </tbody>
</table>
{% else %}
<p>No repos yet. Add one to get started.</p>
{% endif %}
{% endblock %}
```

Create `src/clean_room/templates/add_repo.html`:
```html
{% extends "base.html" %}
{% block title %}Add Repo - Clean Room{% endblock %}
{% block content %}
<h1>Add Repo</h1>
<form method="post" action="/repos">
    <label>GitHub URL
        <input type="url" name="github_url" placeholder="https://github.com/org/repo" required>
    </label>
    <button type="submit">Clone & Add</button>
</form>
{% endblock %}
```

**Step 4: Implement dashboard route**

```python
# src/clean_room/routes/dashboard.py
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from clean_room.config import DB_PATH
from clean_room.db import get_db

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    from clean_room.main import templates
    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute("""
            SELECT r.*,
                   j.status as last_status,
                   j.completed_at as last_run
            FROM repos r
            LEFT JOIN (
                SELECT repo_id, status, completed_at,
                       ROW_NUMBER() OVER (PARTITION BY repo_id ORDER BY id DESC) as rn
                FROM jobs
            ) j ON j.repo_id = r.id AND j.rn = 1
            WHERE r.status = 'active'
            ORDER BY r.created_at DESC
        """)
        repos = await cursor.fetchall()
        return templates.TemplateResponse("dashboard.html", {
            "request": request, "repos": repos,
        })
    finally:
        await db.close()


@router.get("/repos/new", response_class=HTMLResponse)
async def add_repo_page(request: Request):
    from clean_room.main import templates
    return templates.TemplateResponse("add_repo.html", {"request": request})
```

**Step 5: Register route in main.py**

Add to `src/clean_room/main.py`:
```python
from clean_room.routes.dashboard import router as dashboard_router
app.include_router(dashboard_router)
```

**Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_routes_dashboard.py -v`
Expected: PASS.

**Step 7: Commit**

```bash
git add src/clean_room/routes/dashboard.py src/clean_room/templates/dashboard.html src/clean_room/templates/add_repo.html tests/test_routes_dashboard.py
git commit -m "feat: dashboard with repo list and add repo page"
```

---

### Task 11: Final Assembly and Integration Test

**Files:**
- Modify: `src/clean_room/main.py` (final version)
- Create: `tests/test_integration.py`

**Step 1: Write integration test**

```python
# tests/test_integration.py
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
```

**Step 2: Finalize main.py**

```python
# src/clean_room/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from clean_room.config import CLEAN_ROOM_DIR, REPOS_DIR, SPECS_MONOREPO_DIR, DB_PATH
from clean_room.db import init_db
from clean_room.git_ops import init_specs_monorepo


@asynccontextmanager
async def lifespan(app: FastAPI):
    CLEAN_ROOM_DIR.mkdir(parents=True, exist_ok=True)
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    SPECS_MONOREPO_DIR.mkdir(parents=True, exist_ok=True)
    await init_db(DB_PATH)
    await init_specs_monorepo(SPECS_MONOREPO_DIR)
    yield


app = FastAPI(lifespan=lifespan)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

static_dir = BASE_DIR.parent.parent / "static"
if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

from clean_room.routes.dashboard import router as dashboard_router
from clean_room.routes.repos import router as repos_router
from clean_room.routes.jobs import router as jobs_router
from clean_room.routes.prompts import router as prompts_router

app.include_router(dashboard_router)
app.include_router(repos_router)
app.include_router(jobs_router)
app.include_router(prompts_router)
```

**Step 3: Run all tests**

Run: `uv run pytest tests/ -v`
Expected: All tests PASS.

**Step 4: Run linting and type checking**

Run: `uv run ruff check src/ tests/`
Run: `uv run mypy src/ --ignore-missing-imports`
Expected: Clean or minor fixable issues.

**Step 5: Commit**

```bash
git add src/clean_room/main.py tests/test_integration.py
git commit -m "feat: final assembly with all routes and integration test"
```

---

### Task 12: Manual Smoke Test

**Files:** None (verification only)

**Step 1: Start the server**

Run: `uv run uvicorn clean_room.main:app --reload`
Expected: Server starts on http://127.0.0.1:8000

**Step 2: Verify all pages load**

- `http://127.0.0.1:8000` — Dashboard with "Add Repo" link
- `http://127.0.0.1:8000/repos/new` — Add repo form
- `http://127.0.0.1:8000/prompts` — Two seeded prompts, create/edit/delete works

**Step 3: Test full flow (requires ANTHROPIC_API_KEY)**

- Add a small public repo
- Create a job with "Create Spec" prompt, max_iterations=2
- Watch log stream in job viewer
- Stop the job
- Check `~/.clean-room/specs-monorepo/` for output

**Step 4: Commit any fixes**

```bash
git add -A
git commit -m "fix: smoke test adjustments"
```
