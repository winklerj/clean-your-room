from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from clean_room.config import CLEAN_ROOM_DIR, REPOS_DIR, SPECS_MONOREPO_DIR, DB_PATH
from clean_room.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    CLEAN_ROOM_DIR.mkdir(parents=True, exist_ok=True)
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    SPECS_MONOREPO_DIR.mkdir(parents=True, exist_ok=True)
    await init_db(DB_PATH)
    yield


app = FastAPI(lifespan=lifespan)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

static_dir = BASE_DIR.parent.parent / "static"
if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

from clean_room.routes.prompts import router as prompts_router
from clean_room.routes.repos import router as repos_router

app.include_router(prompts_router)
app.include_router(repos_router)
