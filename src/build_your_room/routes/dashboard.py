from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from build_your_room.config import DB_PATH
from build_your_room.db import get_db

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    from build_your_room.main import templates

    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute(
            "SELECT * FROM repos WHERE archived=0 ORDER BY created_at DESC"
        )
        repos = list(await cursor.fetchall())

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "repos": repos,
            "total_repos": len(repos),
        })
    finally:
        await db.close()


@router.get("/repos/new", response_class=HTMLResponse)
async def add_repo_page(request: Request):
    from build_your_room.main import templates

    return templates.TemplateResponse("add_repo.html", {"request": request})
