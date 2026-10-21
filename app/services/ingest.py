from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Comment, Network, Post, Source
from app.services.analyze import analyze_comments
from app.services.crawl import notify_matches
from app.services.search import ensure_user

logger = logging.getLogger(__name__)

# Живой источник читает бот, поэтому планировщику его трогать не нужно.
LIVE_SOURCE_HORIZON_DAYS = 3650


@dataclass(slots=True)
class IncomingComment:
    """Сообщение из чата, приведённое к виду, в котором его можно положить в базу."""

    external_id: str
    text: str
    published_at: datetime
    url: str
    post_external_id: str
    post_url: str
    parent_external_id: str | None = None
    post_text: str = ""
    post_published_at: datetime | None = None
    author_name: str | None = None
    author_url: str | None = None
    author_external_id: str | None = None


async def ensure_chat_source(
    session: AsyncSession,
    *,
    chat_id: int,
    title: str,
    handle: str,
    url: str,
    owner_telegram_id: int | None,
    owner_username: str | None = None,
    owner_first_name: str | None = None,
) -> Source | None:
    """Находит источник для чата или заводит его на владельца — того, кто добавил бота."""
    existing = (
        (
            await session.execute(
                select(Source).where(
                    Source.network == Network.telegram,
                    Source.external_id == str(chat_id),
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing
    if not owner_telegram_id:
        return None

    owner = await ensure_user(
        session,
        owner_telegram_id,
        first_name=owner_first_name,
        username=owner_username,
    )
    source = Source(
        user_id=owner.id,
        network=Network.telegram,
        external_id=str(chat_id),
        handle=handle or str(chat_id),
        title=title or handle or str(chat_id),
        url=url,
        ingest_mode="live",
        is_active=True,
        next_crawl_at=datetime.now(timezone.utc) + timedelta(days=LIVE_SOURCE_HORIZON_DAYS),
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)
    logger.info("Подключён живой источник «%s» (%s)", source.title, chat_id)
    return source


async def store_incoming(session: AsyncSession, source: Source, item: IncomingComment) -> int | None:
    """Кладёт сообщение в ленту. Возвращает id нового комментария или None, если он уже был."""
    inserted_post = (
        pg_insert(Post)
        .values(
            source_id=source.id,
            network=source.network,
            external_id=item.post_external_id,
            url=item.post_url or item.url,
            text=item.post_text or "",
            published_at=item.post_published_at or item.published_at,
            comments_count=0,
        )
        .on_conflict_do_nothing(constraint="uq_posts_source_external")
        .returning(Post.id)
    )
    post_id = (await session.execute(inserted_post)).scalar_one_or_none()
    if post_id is None:
        post_id = int(
            (
                await session.execute(
                    select(Post.id).where(
                        Post.source_id == source.id,
                        Post.external_id == item.post_external_id,
                    )
                )
            ).scalar_one()
        )
    else:
        source.posts_count = (source.posts_count or 0) + 1

    inserted_comment = (
        pg_insert(Comment)
        .values(
            post_id=post_id,
            source_id=source.id,
            network=source.network,
            external_id=item.external_id,
            author_name=item.author_name,
            author_url=item.author_url,
            author_external_id=item.author_external_id,
            text=item.text,
            published_at=item.published_at,
            parent_external_id=item.parent_external_id,
            url=item.url,
        )
        .on_conflict_do_nothing(constraint="uq_comments_post_external")
        .returning(Comment.id)
    )
    new_id = (await session.execute(inserted_comment)).scalar_one_or_none()
    if new_id is not None:
        source.comments_count = (source.comments_count or 0) + 1

    await session.commit()
    return int(new_id) if new_id is not None else None


async def ingest_telegram_message(
    session: AsyncSession,
    source: Source,
    item: IncomingComment,
    settings: Settings | None = None,
    bot=None,
) -> dict[str, int | bool]:
    """Проверяет новое сообщение по рубрикам и уведомляет владельца о совпадениях."""
    settings = settings or get_settings()
    new_id = await store_incoming(session, source, item)
    if new_id is None:
        return {"new": False, "flagged": 0}

    flagged = 0
    if settings.analyze_on_ingest:
        comment = await session.get(Comment, new_id)
        if comment is not None:
            stats = await analyze_comments(session, [comment], settings)
            flagged = stats["flagged"]
            await session.commit()

    if bot is not None:
        await notify_matches(session, source, [new_id], bot)

    return {"new": True, "flagged": flagged}
