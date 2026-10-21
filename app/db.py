from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.dsn,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    pool_recycle=1800,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """Создаёт таблицы при первом запуске. Для продакшена достаточно, миграции — через Alembic."""
    from app import models  # noqa: F401  (нужно для регистрации метаданных)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all не трогает уже существующие таблицы — доводим схему руками.
        await conn.execute(
            text(
                "ALTER TABLE sources ADD COLUMN IF NOT EXISTS "
                "ingest_mode VARCHAR(16) NOT NULL DEFAULT 'crawl'"
            )
        )


async def dispose_db() -> None:
    await engine.dispose()
