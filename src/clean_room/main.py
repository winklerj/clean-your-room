from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from clean_room.config import CLEAN_ROOM_DIR, REPOS_DIR, SPECS_MONOREPO_DIR, DB_PATH
from clean_room.db import init_db, get_db
from clean_room.git_ops import init_specs_monorepo
from clean_room.routes.dashboard import router as dashboard_router
from clean_room.routes.prompts import router as prompts_router
from clean_room.routes.repos import router as repos_router
from clean_room.routes.jobs import router as jobs_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    CLEAN_ROOM_DIR.mkdir(parents=True, exist_ok=True)
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    SPECS_MONOREPO_DIR.mkdir(parents=True, exist_ok=True)
    await init_db(DB_PATH)
    await init_specs_monorepo(SPECS_MONOREPO_DIR)
    db = await get_db(DB_PATH)
    try:
        await db.execute(
            "UPDATE jobs SET status='failed', completed_at=datetime('now') "
            "WHERE status='running'"
        )
        await db.commit()
    finally:
        await db.close()
    yield


app = FastAPI(lifespan=lifespan)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

static_dir = BASE_DIR.parent.parent / "static"
if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(dashboard_router)
app.include_router(prompts_router)
app.include_router(repos_router)
app.include_router(jobs_router)
