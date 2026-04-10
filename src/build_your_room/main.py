import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from build_your_room.config import (
    BUILD_YOUR_ROOM_DIR,
    CLONES_DIR,
    DATABASE_URL,
    LOG_LEVEL,
    MAX_CONCURRENT_PIPELINES,
    PIPELINES_DIR,
)
from build_your_room.db import close_pool, get_pool, init_db
from build_your_room.orchestrator import PipelineOrchestrator
from build_your_room.routes.api import router as api_router
from build_your_room.routes.dashboard import router as dashboard_router
from build_your_room.routes.escalations import router as escalations_router
from build_your_room.routes.pipeline_defs import router as pipeline_defs_router
from build_your_room.routes.pipelines import router as pipelines_router
from build_your_room.routes.prompts import router as prompts_router
from build_your_room.routes.repos import router as repos_router
from build_your_room.routes.streams import router as streams_router
from build_your_room.streaming import LogBuffer

logger = logging.getLogger(__name__)
log_buffer = LogBuffer()
orchestrator: PipelineOrchestrator | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global orchestrator

    logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))

    BUILD_YOUR_ROOM_DIR.mkdir(parents=True, exist_ok=True)
    CLONES_DIR.mkdir(parents=True, exist_ok=True)
    PIPELINES_DIR.mkdir(parents=True, exist_ok=True)

    await init_db(DATABASE_URL)

    pool = get_pool()
    orchestrator = PipelineOrchestrator(
        pool, log_buffer, max_concurrent=MAX_CONCURRENT_PIPELINES
    )
    await orchestrator.reconcile_running_state()
    logger.info("Orchestrator initialized, startup reconciliation complete")

    yield

    await close_pool()
    orchestrator = None


app = FastAPI(lifespan=lifespan)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

static_dir = BASE_DIR.parent.parent / "static"
if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(api_router)
app.include_router(dashboard_router)
app.include_router(escalations_router)
app.include_router(pipeline_defs_router)
app.include_router(pipelines_router)
app.include_router(prompts_router)
app.include_router(repos_router)
app.include_router(streams_router)
