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
