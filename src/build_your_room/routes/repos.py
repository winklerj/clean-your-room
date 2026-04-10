from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from build_your_room.db import get_pool

router = APIRouter(prefix="/repos")


@router.get("/browse", response_class=HTMLResponse)
async def browse_directories(path: str = Query(default="")):
    """GET /repos/browse?path=... — return directory listing as an htmx HTML fragment."""
    if not path:
        target = Path.home()
    else:
        target = Path(path).resolve()

    if not target.is_dir():
        return HTMLResponse(
            '<div class="browse-error">Directory not found</div>', status_code=200,
        )

    parent = target.parent
    entries: list[dict[str, str]] = []

    try:
        for child in sorted(target.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                entries.append({"name": child.name, "path": str(child)})
    except PermissionError:
        return HTMLResponse(
            '<div class="browse-error">Permission denied</div>', status_code=200,
        )

    lines: list[str] = []
    lines.append(f'<div class="browse-current">{target}</div>')
    if target != target.parent:
        lines.append(
            f'<div class="browse-entry browse-parent" '
            f'hx-get="/repos/browse?path={parent}" hx-target="#folder-list">'
            f'&#x2191; ..</div>'
        )
    for entry in entries:
        lines.append(
            f'<div class="browse-entry" '
            f'hx-get="/repos/browse?path={entry["path"]}" hx-target="#folder-list" '
            f'data-path="{entry["path"]}">'
            f'&#128193; {entry["name"]}</div>'
        )
    if not entries:
        lines.append('<div class="browse-empty">No subdirectories</div>')

    return HTMLResponse("\n".join(lines))


async def _fetch_repos_data(*, include_archived: bool = False) -> dict[str, Any]:
    """Query repos with pipeline counts and recent pipeline info."""
    pool = get_pool()
    async with pool.connection() as conn:
        where = "" if include_archived else "WHERE r.archived = 0"
        cur = await conn.execute(
            f"SELECT r.* FROM repos r {where} ORDER BY r.created_at DESC"
        )
        repos: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

        # Pipeline counts and latest pipeline per repo
        cur = await conn.execute(
            "SELECT p.repo_id, p.status, COUNT(*) AS cnt "
            "FROM pipelines p GROUP BY p.repo_id, p.status"
        )
        status_rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
        status_by_repo: dict[int, dict[str, int]] = {}
        for row in status_rows:
            rid = row["repo_id"]
            if rid not in status_by_repo:
                status_by_repo[rid] = {}
            status_by_repo[rid][row["status"]] = row["cnt"]

        # Most recent pipeline per repo (for "last activity")
        cur = await conn.execute(
            "SELECT DISTINCT ON (p.repo_id) "
            "  p.repo_id, p.id AS pipeline_id, p.status, p.updated_at, "
            "  pd.name AS def_name "
            "FROM pipelines p "
            "JOIN pipeline_defs pd ON pd.id = p.pipeline_def_id "
            "ORDER BY p.repo_id, p.updated_at DESC"
        )
        latest_rows: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]
        latest_by_repo: dict[int, dict[str, Any]] = {
            row["repo_id"]: row for row in latest_rows
        }

    enriched: list[dict[str, Any]] = []
    for r in repos:
        rid = r["id"]
        statuses = status_by_repo.get(rid, {})
        total = sum(statuses.values())
        enriched.append({
            **r,
            "pipeline_total": total,
            "pipeline_running": statuses.get("running", 0),
            "pipeline_completed": statuses.get("completed", 0),
            "pipeline_failed": statuses.get("failed", 0),
            "latest_pipeline": latest_by_repo.get(rid),
        })

    total_repos = len(repos)
    archived_count = sum(1 for r in repos if r.get("archived", 0))

    return {
        "repos": enriched,
        "total_repos": total_repos,
        "archived_count": archived_count,
        "include_archived": include_archived,
    }


@router.get("", response_class=HTMLResponse)
async def repo_list(request: Request, show_archived: str = ""):
    """GET /repos — dedicated repo management page."""
    from build_your_room.main import templates

    include_archived = show_archived == "true"
    data = await _fetch_repos_data(include_archived=include_archived)
    return templates.TemplateResponse(request, "repos.html", data)


@router.get("/new", response_class=HTMLResponse)
async def new_repo_form(request: Request):
    """GET /repos/new — add repo form."""
    from build_your_room.main import templates

    return templates.TemplateResponse(request, "new_repo.html", {})


@router.post("", response_class=RedirectResponse)
async def add_repo(
    name: str = Form(),
    local_path: str = Form(),
    git_url: str = Form(""),
    default_branch: str = Form("main"),
):
    resolved = Path(local_path).resolve()
    if not resolved.is_dir():
        raise HTTPException(400, f"Directory does not exist: {local_path}")

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM repos WHERE local_path=%s", (str(resolved),),
        )
        existing: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        if existing:
            return RedirectResponse(f"/repos/{existing['id']}", status_code=303)

        cur = await conn.execute(
            "INSERT INTO repos (name, local_path, git_url, default_branch) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (name, str(resolved), git_url or None, default_branch),
        )
        row: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        assert row is not None
        repo_id = row["id"]
        await conn.commit()
    return RedirectResponse(f"/repos/{repo_id}", status_code=303)


@router.get("/{repo_id}", response_class=HTMLResponse)
async def repo_detail(request: Request, repo_id: int):
    from build_your_room.main import templates

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM repos WHERE id=%s", (repo_id,))
        repo: dict[str, Any] | None = await cur.fetchone()  # type: ignore[assignment]
        if not repo:
            raise HTTPException(404, "Repo not found")

        # All pipelines for this repo with def name
        cur = await conn.execute(
            "SELECT p.*, pd.name AS def_name "
            "FROM pipelines p "
            "JOIN pipeline_defs pd ON pd.id = p.pipeline_def_id "
            "WHERE p.repo_id = %s "
            "ORDER BY p.updated_at DESC",
            (repo_id,),
        )
        pipelines: list[dict[str, Any]] = await cur.fetchall()  # type: ignore[assignment]

    return templates.TemplateResponse(request, "repo_detail.html", {
        "repo": repo, "pipelines": pipelines,
    })


@router.post("/{repo_id}/archive", response_class=RedirectResponse)
async def archive_repo(repo_id: int):
    pool = get_pool()
    async with pool.connection() as conn:
        await conn.execute("UPDATE repos SET archived=1 WHERE id=%s", (repo_id,))
        await conn.commit()
    return RedirectResponse(f"/repos/{repo_id}", status_code=303)
