from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from build_your_room.db import get_pool

router = APIRouter(prefix="/repos")


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
        repo = await cur.fetchone()
    if not repo:
        raise HTTPException(404, "Repo not found")
    return templates.TemplateResponse("repo_detail.html", {
        "request": request, "repo": repo,
    })


@router.post("/{repo_id}/archive", response_class=RedirectResponse)
async def archive_repo(repo_id: int):
    pool = get_pool()
    async with pool.connection() as conn:
        await conn.execute("UPDATE repos SET archived=1 WHERE id=%s", (repo_id,))
        await conn.commit()
    return RedirectResponse(f"/repos/{repo_id}", status_code=303)
