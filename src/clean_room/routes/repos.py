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
            "SELECT id FROM repos WHERE slug=?", (parsed.slug,)
        )
        existing = await cursor.fetchone()
        if existing:
            return RedirectResponse(f"/repos/{existing[0]}", status_code=303)
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
