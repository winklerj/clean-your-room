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
        # Aggregate job stats
        cursor = await db.execute("""
            SELECT status, COUNT(*) as count
            FROM jobs
            GROUP BY status
        """)
        stats_rows = await cursor.fetchall()
        job_stats = {row["status"]: row["count"] for row in stats_rows}

        # Repos with last job info + last completed timestamp
        cursor = await db.execute("""
            SELECT r.*,
                   j.status as last_status,
                   j.completed_at as last_run,
                   j.current_iteration as last_iteration,
                   j.max_iterations as last_max_iterations,
                   j.id as last_job_id,
                   c.completed_at as last_completed_at
            FROM repos r
            LEFT JOIN (
                SELECT repo_id, status, completed_at,
                       current_iteration, max_iterations, id,
                       ROW_NUMBER() OVER (PARTITION BY repo_id ORDER BY id DESC) as rn
                FROM jobs
            ) j ON j.repo_id = r.id AND j.rn = 1
            LEFT JOIN (
                SELECT repo_id, completed_at,
                       ROW_NUMBER() OVER (PARTITION BY repo_id ORDER BY id DESC) as rn
                FROM jobs
                WHERE status = 'completed'
            ) c ON c.repo_id = r.id AND c.rn = 1
            WHERE r.status = 'active'
            ORDER BY r.created_at DESC
        """)
        repos = list(await cursor.fetchall())

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "repos": repos,
            "job_stats": job_stats,
            "total_repos": len(repos),
        })
    finally:
        await db.close()


@router.get("/repos/new", response_class=HTMLResponse)
async def add_repo_page(request: Request):
    from clean_room.main import templates
    return templates.TemplateResponse("add_repo.html", {"request": request})
