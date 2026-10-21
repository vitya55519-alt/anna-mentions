from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from aiogram import Bot
from aiogram.types import MenuButtonWebApp, WebAppInfo
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
logger = logging.getLogger(__name__)


async def ensure_menu_button() -> None:
    """Ставит кнопку Mini App рядом с полем ввода.

    Делаем это и из web: именно у web-сервиса Railway всегда есть публичный домен,
    а воркер может быть без него.
    """
    url = settings.public_webapp_url
    if not (settings.bot_token and url):
        if not url:
            logger.warning("Адрес приложения неизвестен — кнопка Mini App не поставлена")
        return

    try:
        async with Bot(settings.bot_token) as bot:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Открыть сканер", web_app=WebAppInfo(url=url))
            )
        logger.info("Кнопка Mini App установлена на %s", url)
    except Exception as exc:  # noqa: BLE001 — кнопка не критична для работы API
        logger.warning("Не смог поставить кнопку Mini App: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    await ensure_menu_button()
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
