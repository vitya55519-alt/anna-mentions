from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import AdapterError, get_adapter
from app.config import Settings, get_settings
from app.models import Comment, CrawlRun, Post, Source
from app.services.analyze import analyze_comments
from app.services.present import comment_card

logger = logging.getLogger(__name__)

ERROR_BACKOFF_MINUTES = 15
MAX_NOTIFICATIONS_PER_SOURCE = 5


@dataclass(slots=True)
class SyncResult:
    run: CrawlRun
    new_comment_ids: list[int] = field(default_factory=list)
    flagged: int = 0
    checked: int = 0


async def claim_due_sources(session: AsyncSession, limit: int = 3) -> list[Source]:
    """Берёт источники, которым пора обновляться, и сразу сдвигает им срок.

    Живые источники (ingest_mode="live") наполняет бот, планировщик их пропускает.
    """
    stmt = (
        select(Source)
        .where(
            Source.is_active.is_(True),
            Source.ingest_mode != "live",
            Source.next_crawl_at <= func.now(),
        )
        .order_by(Source.next_crawl_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    sources = list((await session.execute(stmt)).scalars().all())

    for source in sources:
        source.next_crawl_at = datetime.now(timezone.utc) + timedelta(
            minutes=source.crawl_interval_minutes
        )
    await session.commit()
    return sources


async def sync_source(
    session: AsyncSession,
    source: Source,
    settings: Settings | None = None,
) -> SyncResult:
    settings = settings or get_settings()
    adapter = get_adapter(source.network)

    run = CrawlRun(source_id=source.id, status="running")
    session.add(run)
    # Коммитим сразу: даже если обход упадёт, запись о запуске останется.
    await session.commit()
    run_id = run.id

    initialized = bool((source.cursor or {}).get("initialized"))
    posts_limit = settings.crawl_posts_per_run if initialized else settings.crawl_backfill_posts

    new_comment_ids: list[int] = []
    posts_seen = 0
    comments_seen = 0
    error: str | None = None

    try:
        async for post_data in adapter.fetch(
            source,
            posts_limit=posts_limit,
            comments_limit=settings.crawl_comments_per_post,
        ):
            posts_seen += 1
            post_id = await _upsert_post(session, source, post_data)
            if post_data.comments:
                comments_seen += len(post_data.comments)
                new_comment_ids.extend(
                    await _insert_comments(session, source, post_id, post_data.comments)
                )
            # Коммитим порциями, чтобы падение на середине не теряло собранное.
            await session.commit()
    except AdapterError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 — источник не должен валить воркер
        logger.exception("Сбой обхода источника %s", source.id)
        await session.rollback()
        error = f"внутренняя ошибка: {exc.__class__.__name__}"

    flagged = 0
    checked = 0
    if new_comment_ids and settings.analyze_on_ingest:
        comments = list(
            (
                await session.execute(select(Comment).where(Comment.id.in_(new_comment_ids)))
            ).scalars()
        )
        stats = await analyze_comments(session, comments, settings)
        flagged, checked = stats["flagged"], stats["checked"]
        await session.commit()

    await _update_counters(session, source)

    run = await session.get(CrawlRun, run_id)
    if run is None:
        run = CrawlRun(source_id=source.id)
        session.add(run)

    run.finished_at = datetime.now(timezone.utc)
    run.posts_seen = posts_seen
    run.comments_seen = comments_seen
    run.comments_new = len(new_comment_ids)
    run.status = "error" if error else "ok"
    run.error = error

    source.last_crawl_at = datetime.now(timezone.utc)
    source.cursor = {**(source.cursor or {}), "initialized": True}
    source.last_error = error
    if error:
        source.next_crawl_at = datetime.now(timezone.utc) + timedelta(
            minutes=max(ERROR_BACKOFF_MINUTES, source.crawl_interval_minutes // 2)
        )
    else:
        source.next_crawl_at = datetime.now(timezone.utc) + timedelta(
            minutes=source.crawl_interval_minutes
        )

    await session.commit()
    return SyncResult(run=run, new_comment_ids=new_comment_ids, flagged=flagged, checked=checked)


async def _upsert_post(session: AsyncSession, source: Source, data) -> int:
    stmt = (
        pg_insert(Post)
        .values(
            source_id=source.id,
            network=source.network,
            external_id=data.external_id,
            url=data.url,
            text=data.text or "",
            author_name=data.author_name,
            published_at=data.published_at,
            comments_count=data.comments_count,
        )
        .on_conflict_do_update(
            constraint="uq_posts_source_external",
            set_={
                "text": data.text or "",
                "url": data.url,
                "comments_count": data.comments_count,
            },
        )
        .returning(Post.id)
    )
    return int((await session.execute(stmt)).scalar_one())


async def _insert_comments(session: AsyncSession, source: Source, post_id: int, comments) -> list[int]:
    rows: list[dict] = []
    seen: set[str] = set()

    for item in comments:
        text = (item.text or "").strip()
        if not text or item.external_id in seen:
            continue
        seen.add(item.external_id)
        rows.append(
            {
                "post_id": post_id,
                "source_id": source.id,
                "network": source.network,
                "external_id": str(item.external_id),
                "author_name": item.author_name,
                "author_url": item.author_url,
                "author_external_id": item.author_external_id,
                "text": text,
                "published_at": item.published_at,
                "likes": item.likes,
                "parent_external_id": item.parent_external_id,
                "url": item.url,
            }
        )

    if not rows:
        return []

    stmt = (
        pg_insert(Comment)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_comments_post_external")
        .returning(Comment.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _update_counters(session: AsyncSession, source: Source) -> None:
    source.posts_count = int(
        (await session.execute(select(func.count()).select_from(Post).where(Post.source_id == source.id)))
        .scalar_one()
    )
    source.comments_count = int(
        (
            await session.execute(
                select(func.count()).select_from(Comment).where(Comment.source_id == source.id)
            )
        ).scalar_one()
    )


async def notify_matches(session: AsyncSession, source: Source, comment_ids: list[int], bot) -> int:
    """Присылает владельцу источника комментарии, попавшие под его ключевые слова."""
    if not comment_ids or bot is None:
        return 0

    from app.models import User, Watch

    user = await session.get(User, source.user_id)
    if user is None:
        return 0

    watches = list(
        (
            await session.execute(
                select(Watch).where(Watch.user_id == source.user_id, Watch.is_active.is_(True))
            )
        ).scalars()
    )
    if not watches:
        return 0

    rows = (
        await session.execute(
            select(Comment, Post)
            .join(Post, Comment.post_id == Post.id)
            .where(Comment.id.in_(comment_ids))
        )
    ).all()

    sent = 0
    for watch in watches:
        matched = [
            (comment, post)
            for comment, post in rows
            if watch.keyword.casefold() in (comment.text or "").casefold()
            and (watch.network is None or watch.network == comment.network)
            and (watch.source_id is None or watch.source_id == source.id)
        ]
        if not matched:
            continue

        watch.hits_count += len(matched)
        watch.last_hit_at = datetime.now(timezone.utc)

        for comment, post in matched[:MAX_NOTIFICATIONS_PER_SOURCE]:
            text = (
                f"<b>Слово:</b> {watch.keyword}\n\n"
                + comment_card(comment, source, post)
            )
            try:
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не смог отправить уведомление: %s", exc)
                break
            await asyncio.sleep(0.05)

    await session.commit()
    return sent
