from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from build_your_room.config import BUILD_YOUR_ROOM_DIR, DB_PATH
from build_your_room.db import init_db
from build_your_room.routes.dashboard import router as dashboard_router
from build_your_room.routes.prompts import router as prompts_router
from build_your_room.routes.repos import router as repos_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    BUILD_YOUR_ROOM_DIR.mkdir(parents=True, exist_ok=True)
    await init_db(DB_PATH)
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
