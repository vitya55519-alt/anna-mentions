"""Сбор сообщений из групп привязанных аккаунтов Telegram.

Каждый аккаунт видит свой список диалогов, поэтому вместе они дают широкий охват.
Обход двухшаговый:

1. discovery — перечитать диалоги всех аккаунтов и завести на каждую группу или канал
   источник с `ingest_mode="scan"`, закрепив его за нашедшим аккаунтом;
2. scan — дочитать каждую группу с места остановки (`cursor.last_msg_id`), сложить
   сообщения в общие `posts` и `comments` и сразу прогнать по рубрикам.

Дальше лента, поиск, рубрики и выгрузка работают так же, как для живых чатов.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Comment, Network, Post, Source, User
from app.services.analyze import analyze_comments
from app.services.ingest import IncomingComment, store_incoming
from app.services.search import ensure_user
from app.services.tg_accounts import AccountPool

logger = logging.getLogger(__name__)

SOURCE_PREFIX = "dialog:"
ERROR_BACKOFF_MINUTES = 15
ANALYZE_PER_SCAN = 60
SENDER_CACHE_LIMIT = 5000

# id пользователя → (имя, ссылка); общий на процесс, чтобы не дёргать get_sender повторно
_SENDERS: dict[int, tuple[str | None, str | None]] = {}


@dataclass(slots=True)
class ScanResult:
    new_comment_ids: list[int] = field(default_factory=list)
    checked: int = 0
    flagged: int = 0
    error: str | None = None


def dialog_key(entity_id: int | str) -> str:
    return f"{SOURCE_PREFIX}{entity_id}"


def peer_of(entity) -> tuple[str, int | None]:
    """Тип диалога и access_hash: без него канал не открыть в следующем проходе."""
    kind = "channel" if getattr(entity, "megagroup", False) or getattr(entity, "broadcast", False) else "chat"
    return kind, getattr(entity, "access_hash", None)


def peer_ref(cursor: dict, peer_id: int):
    """Собирает ссылку на диалог из данных, сохранённых при обнаружении."""
    from telethon.tl.types import InputPeerChannel, PeerChat

    if cursor.get("peer") == "chat":
        return PeerChat(peer_id)
    access_hash = cursor.get("access_hash")
    if access_hash is None:
        return peer_id
    return InputPeerChannel(peer_id, int(access_hash))


def dialog_link(entity) -> str:
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}"
    return f"https://t.me/c/{getattr(entity, 'id', '')}"


async def resolve_owner(session: AsyncSession, settings: Settings) -> User | None:
    """Владелец найденных групп: заданный в настройках или первый запустивший бота."""
    if settings.owner_telegram_id:
        return await ensure_user(session, settings.owner_telegram_id)
    return (await session.execute(select(User).order_by(User.id).limit(1))).scalar_one_or_none()


async def ensure_dialog_source(
    session: AsyncSession,
    owner: User,
    account_index: int,
    entity,
    *,
    scan_interval_minutes: int,
) -> tuple[Source, bool]:
    key = dialog_key(entity.id)
    # Та же группа могла попасть в базу раньше: живьём (бот в чате) или сканом.
    existing_keys = (key, str(entity.id), f"-100{entity.id}")
    source = (
        (
            await session.execute(
                select(Source).where(
                    Source.user_id == owner.id,
                    Source.network == Network.telegram,
                    Source.external_id.in_(existing_keys),
                )
            )
        )
        .scalars()
        .first()
    )
    if source is not None:
        # Живой источник главнее: бот видит сообщения сразу, скан ему не нужен.
        return source, False

    username = getattr(entity, "username", None)
    peer_kind, access_hash = peer_of(entity)
    source = Source(
        user_id=owner.id,
        network=Network.telegram,
        external_id=key,
        handle=username or str(entity.id),
        title=getattr(entity, "title", None) or username or str(entity.id),
        url=dialog_link(entity),
        ingest_mode="scan",
        is_active=True,
        crawl_interval_minutes=scan_interval_minutes,
        next_crawl_at=datetime.now(timezone.utc),
        cursor={"account": account_index, "peer": peer_kind, "access_hash": access_hash},
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)
    return source, True


async def discover_dialogs(session: AsyncSession, settings: Settings, pool: AccountPool) -> dict[str, int]:
    """Обходит диалоги всех аккаунтов и заводит источник на каждую группу/канал."""
    owner = await resolve_owner(session, settings)
    if owner is None:
        logger.warning("Нет владельца источников: откройте бота и отправьте /start")
        return {"accounts": 0, "dialogs": 0, "created": 0}

    seen: set[str] = set()
    dialogs_total = 0
    created = 0
    connected = 0

    for account in pool.accounts:
        try:
            client = await pool.client(account.index)
        except Exception as exc:  # noqa: BLE001 — один битый аккаунт не мешает остальным
            logger.warning("%s не подключён: %s", account.label, exc)
            continue
        connected += 1

        try:
            # Лимит считаем по группам и каналам, а не по всем диалогам:
            # личные переписки не должны съедать квоту в 500 чатов на аккаунт.
            scanned = 0
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                entity_id = getattr(entity, "id", None)
                if entity_id is None:
                    continue
                is_group = bool(
                    getattr(entity, "megagroup", False) or getattr(dialog, "is_group", False)
                )
                is_channel = bool(getattr(entity, "broadcast", False))
                if not is_group and not (is_channel and settings.tg_scan_channels):
                    continue

                key = dialog_key(entity_id)
                if key in seen:
                    continue
                seen.add(key)
                dialogs_total += 1
                scanned += 1

                _, created_now = await ensure_dialog_source(
                    session,
                    owner,
                    account.index,
                    entity,
                    scan_interval_minutes=settings.default_crawl_interval_minutes,
                )
                created += int(created_now)
                await asyncio.sleep(0.05)

                if settings.tg_scan_dialogs_limit and scanned >= settings.tg_scan_dialogs_limit:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: не удалось прочитать диалоги: %s", account.label, exc)

    logger.info(
        "Обнаружение завершено: аккаунтов %s, диалогов %s, новых источников %s",
        connected,
        dialogs_total,
        created,
    )
    return {"accounts": connected, "dialogs": dialogs_total, "created": created}


async def queue_scan(session: AsyncSession) -> int:
    """Ставит все найденные группы в очередь на ближайший обход."""
    result = await session.execute(
        update(Source)
        .where(Source.ingest_mode == "scan", Source.is_active.is_(True))
        .values(next_crawl_at=datetime.now(timezone.utc))
    )
    await session.commit()
    return int(result.rowcount or 0)


async def scan_source(
    session: AsyncSession,
    source: Source,
    settings: Settings,
    pool: AccountPool,
) -> ScanResult:
    """Читает новые сообщения одной группы через закреплённый за ней аккаунт."""
    from telethon.errors import FloodWaitError

    cursor = dict(source.cursor or {})
    account_index = int(cursor.get("account") or 1)
    min_id = int(cursor.get("last_msg_id") or 0)
    result = ScanResult()
    max_id = min_id

    try:
        client = await pool.client(account_index)
        peer_id = int(source.external_id.removeprefix(SOURCE_PREFIX))
        entity = await client.get_entity(peer_ref(cursor, peer_id))
        base = dialog_link(entity)

        async for msg in client.iter_messages(
            entity, limit=settings.tg_scan_messages_per_dialog, min_id=min_id
        ):
            message_id = getattr(msg, "id", None)
            if not message_id:
                continue
            max_id = max(max_id, int(message_id))

            text = (msg.message or "").strip()
            if not text:
                continue
            result.checked += 1

            item = await _incoming(client, source, msg, base)
            new_id = await store_incoming(session, source, item)
            if new_id is not None:
                result.new_comment_ids.append(new_id)
            if settings.tg_scan_pause_seconds:
                await asyncio.sleep(settings.tg_scan_pause_seconds)

        result.flagged = await _analyze(session, result.new_comment_ids, settings)
    except FloodWaitError as exc:
        result.error = f"лимит Telegram: подождать {exc.seconds} с"
    except Exception as exc:  # noqa: BLE001 — одна группа не должна валить цикл
        logger.warning("Скан «%s» сорвался: %s", source.title, exc)
        result.error = f"{exc.__class__.__name__}: {exc}"

    await _finish(session, source, settings, cursor, max_id, result)
    return result


async def _analyze(
    session: AsyncSession, comment_ids: list[int], settings: Settings
) -> int:
    if not comment_ids or not settings.analyze_on_ingest:
        return 0
    comments = list(
        (
            await session.execute(
                select(Comment).where(Comment.id.in_(comment_ids[:ANALYZE_PER_SCAN]))
            )
        ).scalars()
    )
    stats = await analyze_comments(session, comments, settings)
    await session.commit()
    return int(stats["flagged"])


async def _finish(
    session: AsyncSession,
    source: Source,
    settings: Settings,
    cursor: dict,
    max_id: int,
    result: ScanResult,
) -> None:
    now = datetime.now(timezone.utc)
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
    source.last_crawl_at = now
    source.last_error = result.error
    source.cursor = {**cursor, "last_msg_id": max_id}

    minutes = max(5, source.crawl_interval_minutes or settings.default_crawl_interval_minutes)
    if result.error:
        minutes = max(ERROR_BACKOFF_MINUTES, minutes // 2)
    source.next_crawl_at = now + timedelta(minutes=minutes)
    await session.commit()


async def _incoming(client, source: Source, msg, base: str) -> IncomingComment:
    root_id = msg.id
    reply_to = getattr(msg, "reply_to", None)
    if reply_to is not None and getattr(reply_to, "reply_to_msg_id", None):
        root_id = int(reply_to.reply_to_msg_id)

    name, link, external_id = await _sender(msg)
    return IncomingComment(
        external_id=str(msg.id),
        text=(msg.message or "").strip(),
        published_at=msg.date,
        url=f"{base}/{msg.id}",
        post_external_id=str(root_id),
        post_url=f"{base}/{root_id}",
        parent_external_id=str(root_id) if root_id != msg.id else None,
        author_name=name,
        author_url=link,
        author_external_id=external_id,
    )


async def _sender(msg) -> tuple[str | None, str | None, str | None]:
    sender_id = getattr(msg, "sender_id", None)
    if sender_id is None:
        return None, None, None

    key = int(sender_id)
    cached = _SENDERS.get(key)
    if cached is not None:
        return cached[0], cached[1], str(key)

    try:
        sender = await msg.get_sender()
    except Exception:  # noqa: BLE001 — автор мог удалить аккаунт или скрыть профиль
        sender = None

    if sender is None:
        if len(_SENDERS) > SENDER_CACHE_LIMIT:
            _SENDERS.clear()
        _SENDERS[key] = (None, None)
        return None, None, str(key)

    title = getattr(sender, "title", None)
    username = getattr(sender, "username", None)
    if title:
        name = title
    else:
        name = " ".join(
            part
            for part in (getattr(sender, "first_name", None), getattr(sender, "last_name", None))
            if part
        ).strip() or None
    link = f"https://t.me/{username}" if username else None

    if len(_SENDERS) > SENDER_CACHE_LIMIT:
        _SENDERS.clear()
    _SENDERS[key] = (name, link)
    return name, link, str(key)
