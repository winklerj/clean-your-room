from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from build_your_room.config import DB_PATH
from build_your_room.db import get_db

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

    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute(
            "SELECT id FROM repos WHERE local_path=?", (str(resolved),)
        )
        existing = await cursor.fetchone()
        if existing:
            return RedirectResponse(f"/repos/{existing[0]}", status_code=303)

        cursor = await db.execute(
            "INSERT INTO repos (name, local_path, git_url, default_branch) "
            "VALUES (?, ?, ?, ?) RETURNING id",
            (name, str(resolved), git_url or None, default_branch),
        )
        row = await cursor.fetchone()
        assert row is not None
        repo_id = row[0]
        await db.commit()
    finally:
        await db.close()
    return RedirectResponse(f"/repos/{repo_id}", status_code=303)


@router.get("/{repo_id}", response_class=HTMLResponse)
async def repo_detail(request: Request, repo_id: int):
    from build_your_room.main import templates

    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute("SELECT * FROM repos WHERE id=?", (repo_id,))
        repo = await cursor.fetchone()
        if not repo:
            raise HTTPException(404, "Repo not found")
        return templates.TemplateResponse("repo_detail.html", {
            "request": request, "repo": repo,
        })
    finally:
        await db.close()


@router.post("/{repo_id}/archive", response_class=RedirectResponse)
async def archive_repo(repo_id: int):
    db = await get_db(DB_PATH)
    try:
        await db.execute("UPDATE repos SET archived=1 WHERE id=?", (repo_id,))
        await db.commit()
    finally:
        await db.close()
    return RedirectResponse(f"/repos/{repo_id}", status_code=303)
