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


LEGACY_SCHEMA_HINT = (
    "В базе лежит таблица comments от прежней версии приложения. "
    "Удалите старые таблицы и перезапустите сервис: "
    "DROP TABLE IF EXISTS comments, posts, sources, watches, crawl_runs, users CASCADE;"
)


async def _check_schema(conn) -> None:
    """Ловим несовместимую базу от предыдущей версии — иначе ошибки будут невнятные."""
    exists = (await conn.execute(text("SELECT to_regclass('public.comments')"))).scalar_one_or_none()
    if exists is None:
        return
    column = (
        await conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'comments' AND column_name = 'verdict'"
            )
        )
    ).scalar_one_or_none()
    if column is None:
        raise RuntimeError(LEGACY_SCHEMA_HINT)


async def init_db() -> None:
    """Создаёт таблицы при первом запуске. Для продакшена достаточно, миграции — через Alembic."""
    from app import models  # noqa: F401  (нужно для регистрации метаданных)

    async with engine.begin() as conn:
        # web и worker стартуют одновременно: блокировка не даёт им создавать одни
        # и те же таблицы параллельно (иначе Postgres отдаёт duplicate key pg_type).
        await conn.execute(text("SELECT pg_advisory_xact_lock(727001)"))
        await conn.run_sync(Base.metadata.create_all)
        # create_all не трогает уже существующие таблицы — доводим схему руками.
        await conn.execute(
            text(
                "ALTER TABLE sources ADD COLUMN IF NOT EXISTS "
                "ingest_mode VARCHAR(16) NOT NULL DEFAULT 'crawl'"
            )
        )
        await _check_schema(conn)


async def dispose_db() -> None:
    await engine.dispose()
