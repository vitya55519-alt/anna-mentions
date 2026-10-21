"""Слой доступа к Postgres на asyncpg.

Одна таблица comments — агрегированная лента упоминаний продукта и
комментов под собственными площадками. Дедуп по (source, external_id).
Никаких профилей на людей: храним сам факт упоминания/коммента,
его текст, автора-источник и ссылку, чтобы владелец мог прочитать и
ответить.
"""
import asyncpg

from . import config

_pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS comments (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT NOT NULL,              -- vk | telegram | ok | instagram
    external_id  TEXT NOT NULL,              -- id записи в источнике
    author       TEXT,                       -- отображаемое имя/хендл автора
    text         TEXT,                       -- текст коммента/поста
    url          TEXT,                       -- прямая ссылка
    matched      TEXT,                       -- по какому ключевому слову поймали
    is_own_page  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ,                -- время в источнике
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS comments_created_idx ON comments (created_at DESC);
CREATE INDEX IF NOT EXISTS comments_source_idx ON comments (source);
"""


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not config.DATABASE_URL:
            raise RuntimeError("DATABASE_URL не задан")
        _pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=5)
    return _pool


async def init_db() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)


async def insert_comment(
    *,
    source: str,
    external_id: str,
    author: str | None,
    text: str | None,
    url: str | None,
    matched: str | None = None,
    is_own_page: bool = False,
    created_at=None,
) -> bool:
    """Вставляет запись. Возвращает True, если это новая строка."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO comments (source, external_id, author, text, url,
                                  matched, is_own_page, created_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (source, external_id) DO NOTHING
            """,
            source, external_id, author, text, url, matched, is_own_page, created_at,
        )
        return result.endswith("1")


async def fetch_comments(source: str | None = None, limit: int = 100) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if source and source != "all":
            rows = await conn.fetch(
                """SELECT source, author, text, url, matched, is_own_page,
                          created_at, fetched_at
                   FROM comments WHERE source = $1
                   ORDER BY COALESCE(created_at, fetched_at) DESC LIMIT $2""",
                source, limit,
            )
        else:
            rows = await conn.fetch(
                """SELECT source, author, text, url, matched, is_own_page,
                          created_at, fetched_at
                   FROM comments
                   ORDER BY COALESCE(created_at, fetched_at) DESC LIMIT $1""",
                limit,
            )
        return [dict(r) for r in rows]
