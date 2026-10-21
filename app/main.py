from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.routes import router
from app.config import get_settings
from app.db import dispose_db, init_db
from app.schemas import HealthOut

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield
    await dispose_db()


app = FastAPI(
    title="Comment Radar",
    description="Поиск и мониторинг комментариев в соцсетях через Telegram Mini App",
    version=__version__,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/api/health", response_model=HealthOut, tags=["system"])
async def health() -> HealthOut:
    return HealthOut(status="ok", version=__version__)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
