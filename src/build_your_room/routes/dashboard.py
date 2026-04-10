from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from build_your_room.db import get_pool

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    from build_your_room.main import templates

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT * FROM repos WHERE archived=0 ORDER BY created_at DESC"
        )
        repos = await cur.fetchall()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "repos": repos,
        "total_repos": len(repos),
    })


@router.get("/repos/new", response_class=HTMLResponse)
async def add_repo_page(request: Request):
    from build_your_room.main import templates

    return templates.TemplateResponse("add_repo.html", {"request": request})
