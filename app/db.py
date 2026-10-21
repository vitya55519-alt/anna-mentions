from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


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


async def _ensure_trgm(conn) -> None:
    """GIN-индексы pg_trgm для поиска по подстроке (ILIKE '%...%').

    Без них такой поиск — последовательный проход по всем комментариям.
    Расширение необязательное: если нет прав, поиск работает, просто медленнее.
    """
    statements = (
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        "CREATE INDEX IF NOT EXISTS ix_comments_text_trgm ON comments USING gin (text gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS ix_comments_author_name_trgm "
        "ON comments USING gin (author_name gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS ix_comments_author_url_trgm "
        "ON comments USING gin (author_url gin_trgm_ops)",
    )
    try:
        # SAVEPOINT: при ошибке (нет прав на CREATE EXTENSION) основная
        # транзакция с созданием таблиц не откатится.
        async with conn.begin_nested():
            for statement in statements:
                await conn.execute(text(statement))
    except Exception:  # noqa: BLE001
        logger.warning(
            "pg_trgm недоступен: поиск по подстроке пойдёт без индекса",
            exc_info=True,
        )


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
        await conn.execute(
            text(
                "ALTER TABLE join_tasks ADD COLUMN IF NOT EXISTS "
                "group_joined BOOLEAN NOT NULL DEFAULT false"
            )
        )
        await _check_schema(conn)
        await _ensure_trgm(conn)


async def dispose_db() -> None:
    await engine.dispose()
