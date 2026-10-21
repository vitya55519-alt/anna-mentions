from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.errors import (
    ChannelPrivateError,
    ChannelsTooMuchError,
    ChatAdminRequiredError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UserAlreadyParticipantError,
)

from app.adapters.base import AdapterUnavailable
from app.config import Settings
from app.db import SessionLocal
from app.models import JoinTask, Network, Source, User
from app.services.tg_accounts import AccountPool
from app.services.tg_scan import ensure_dialog_source

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
INVITE_RE = re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]{6,64})/?$",
    re.IGNORECASE,
)
PUBLIC_RE = re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?@?([A-Za-z][A-Za-z0-9_]{3,31})/?$",
    re.IGNORECASE,
)
# Ссылка на пост ведёт в тот же канал: t.me/name/123.
POST_RE = re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})/\d+/?$",
    re.IGNORECASE,
)
USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
NUMBERING_RE = re.compile(r"^\d+[.)]?$")
URL_IN_TEXT_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/[^\s,;]+", re.IGNORECASE)

# Пауза между вступлениями живёт в памяти воркера: после рестарта хуже не будет.
_last_join_at: datetime | None = None
_paused_accounts: dict[int, datetime] = {}

_PERMANENT_ERRORS = (
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    ChannelPrivateError,
    ChatAdminRequiredError,
)


@dataclass(slots=True)
class JoinRef:
    kind: str  # public | invite
    value: str  # username или hash
    ref: str  # нормализованная ссылка для базы


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_midnight() -> datetime:
    return _utc_now().replace(hour=0, minute=0, second=0, microsecond=0)


def parse_ref(raw: str) -> JoinRef | None:
    """Разбирает t.me/name, @name, t.me/+hash, t.me/joinchat/hash и tg://-ссылки."""
    text = (raw or "").strip().strip("\"'«»()[]{}<>,;.!")
    text = text.lstrip("-—–•*").strip()
    if not text or NUMBERING_RE.fullmatch(text):
        return None

    lower = text.lower()
    if lower.startswith("tg://join?invite="):
        value = text.split("=", 1)[1].split("&", 1)[0].strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{6,64}", value):
            return JoinRef("invite", value, f"t.me/+{value}")
        return None
    if lower.startswith("tg://resolve?domain="):
        value = text.split("=", 1)[1].split("&", 1)[0].strip().lstrip("@")
        match = USERNAME_RE.fullmatch(value)
        if match:
            return JoinRef("public", match.group(1).lower(), f"t.me/{match.group(1).lower()}")
        return None

    match = URL_IN_TEXT_RE.search(text)
    if match:
        text = match.group(0).strip("\"'«»()[]{}<>,;.!")
    text = text.split("#", 1)[0].split("?", 1)[0].rstrip("/")

    invite = INVITE_RE.fullmatch(text)
    if invite:
        # Хеш приглашения чувствителен к регистру — не приводим его к нижнему.
        return JoinRef("invite", invite.group(1), f"t.me/+{invite.group(1)}")

    public = PUBLIC_RE.fullmatch(text)
    if public:
        value = public.group(1).lower()
        return JoinRef("public", value, f"t.me/{value}")

    post = POST_RE.fullmatch(text)
    if post:
        value = post.group(1).lower()
        return JoinRef("public", value, f"t.me/{value}")

    match = USERNAME_RE.fullmatch(text)
    if match:
        value = match.group(1).lower()
        return JoinRef("public", value, f"t.me/{value}")
    return None


def parse_refs(text: str) -> tuple[list[JoinRef], list[str]]:
    """Возвращает разобранные ссылки и куски, которые не удалось понять."""
    refs: list[JoinRef] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[\s,;]+", text or ""):
        if not chunk:
            continue
        if NUMBERING_RE.fullmatch(chunk.strip()):
            continue
        ref = parse_ref(chunk)
        if ref is None:
            if len(invalid) < 20:
                invalid.append(chunk[:80])
            continue
        if ref.ref in seen:
            continue
        seen.add(ref.ref)
        refs.append(ref)
    return refs, invalid


async def _pending_count(session: AsyncSession, user_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(JoinTask)
                .where(JoinTask.user_id == user_id, JoinTask.status == "pending")
            )
        ).scalar_one()
    )


async def enqueue_join_tasks(
    session: AsyncSession,
    user: User,
    text: str,
    *,
    limit: int,
) -> dict[str, Any]:
    """Ставит ссылки в очередь. Уже подключённые и повторные — пропускает."""
    refs, invalid = parse_refs(text)
    pending = await _pending_count(session, user.id)
    room = max(0, limit - pending)
    queued = 0
    skipped = 0

    existing_sources = {
        handle.lower()
        for handle in (
            await session.execute(
                select(Source.handle).where(
                    Source.user_id == user.id,
                    Source.network == Network.telegram,
                )
            )
        ).scalars()
        if handle
    }

    existing_tasks = {
        task.ref: task
        for task in (
            await session.execute(
                select(JoinTask).where(
                    JoinTask.user_id == user.id,
                    JoinTask.ref.in_([ref.ref for ref in refs]),
                )
            )
        ).scalars()
    }

    now = _utc_now()
    for ref in refs:
        if ref.kind == "public" and ref.value in existing_sources:
            skipped += 1
            continue

        task = existing_tasks.get(ref.ref)
        if task is not None and task.status == "pending":
            skipped += 1
            continue
        # Источник мог быть удалён вручную: ссылку можно подключить заново.
        if task is not None and task.status in {"joined", "exists"} and task.source_id is not None:
            skipped += 1
            continue

        if queued >= room:
            skipped += 1
            continue

        if task is None:
            session.add(
                JoinTask(
                    user_id=user.id,
                    ref=ref.ref,
                    kind=ref.kind,
                    status="pending",
                    next_at=now,
                )
            )
        else:
            # Ошибку или отмену можно повторить, вставив ссылку ещё раз.
            task.status = "pending"
            task.attempts = 0
            task.last_error = None
            task.account_index = None
            task.source_id = None
            task.group_joined = False
            task.next_at = now
        queued += 1

    await session.commit()
    return {
        "queued": queued,
        "skipped": skipped,
        "invalid": invalid,
        "total_pending": pending + queued,
    }


async def _joined_today(session: AsyncSession, user_id: int | None = None) -> dict[int, int]:
    # Канал занимает один слот, новая группа обсуждения — ещё один: иначе
    # автоподключение групп удвоило бы фактическое число вступлений за сутки.
    weight = case((JoinTask.status == "joined", 1), else_=0) + case(
        (JoinTask.group_joined.is_(True), 1), else_=0
    )
    stmt = (
        select(JoinTask.account_index, func.sum(weight))
        .where(
            JoinTask.status.in_(("joined", "exists")),
            JoinTask.joined_at >= _utc_midnight(),
            JoinTask.account_index.is_not(None),
        )
        .group_by(JoinTask.account_index)
    )
    if user_id is not None:
        stmt = stmt.where(JoinTask.user_id == user_id)
    return {
        int(index): int(total or 0)
        for index, total in (await session.execute(stmt)).all()
    }


async def join_queue_snapshot(session: AsyncSession, user_id: int) -> dict[str, Any]:
    counts = {
        str(status): int(count)
        for status, count in (
            await session.execute(
                select(JoinTask.status, func.count())
                .where(JoinTask.user_id == user_id)
                .group_by(JoinTask.status)
            )
        ).all()
    }
    tasks = list(
        (
            await session.execute(
                select(JoinTask)
                .where(JoinTask.user_id == user_id)
                .order_by(JoinTask.id.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    return {
        "tasks": [
            {
                "id": task.id,
                "ref": task.ref,
                "kind": task.kind,
                "status": task.status,
                "account_index": task.account_index,
                "attempts": task.attempts,
                "error": task.last_error,
                "source_id": task.source_id,
                "group_joined": bool(task.group_joined),
                "created_at": task.created_at,
                "updated_at": task.updated_at,
            }
            for task in tasks
        ],
        "counts": counts,
        "joined_today": {
            str(index): count for index, count in (await _joined_today(session, user_id)).items()
        },
        "pending": counts.get("pending", 0),
    }


async def cancel_join_task(session: AsyncSession, user_id: int, task_id: int) -> bool:
    task = await session.get(JoinTask, task_id)
    if task is None or task.user_id != user_id or task.status != "pending":
        return False
    task.status = "cancelled"
    await session.commit()
    return True


async def _pick_account(session: AsyncSession, settings: Settings, pool: AccountPool) -> int | None:
    """Выбирает аккаунт с наименьшей нагрузкой, у которого не исчерпана суточная квота."""
    used = await _joined_today(session)
    now = _utc_now()
    candidates = [
        account.index
        for account in pool.accounts
        if used.get(account.index, 0) < settings.join_daily_limit
        and _paused_accounts.get(account.index, now) <= now
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda index: (used.get(index, 0), index))


async def _claim_task(session: AsyncSession, now: datetime) -> JoinTask | None:
    return (
        (
            await session.execute(
                select(JoinTask)
                .where(JoinTask.status == "pending", JoinTask.next_at <= now)
                .order_by(JoinTask.next_at, JoinTask.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .first()
    )


def _entity_from_updates(updates: Any) -> Any | None:
    for chat in getattr(updates, "chats", None) or []:
        if getattr(chat, "id", None):
            return chat
    return None


async def _join(client: Any, kind: str, ref: str) -> tuple[Any, bool]:
    """Вступает в чат. Возвращает (сущность чата, уже_состоял_ли)."""
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest

    if kind == "invite":
        value = ref.rsplit("+", 1)[-1]
        try:
            updates = await client(ImportChatInviteRequest(value))
        except UserAlreadyParticipantError:
            info = await client(CheckChatInviteRequest(value))
            chat = getattr(info, "chat", None)
            if chat is None:
                raise
            return chat, True
        entity = _entity_from_updates(updates)
        if entity is None:
            raise RuntimeError("Telegram не вернул чат после вступления по ссылке")
        return entity, False

    value = ref.split("t.me/", 1)[-1].lstrip("@")
    try:
        updates = await client(JoinChannelRequest(value))
    except UserAlreadyParticipantError:
        return await client.get_entity(value), True
    entity = _entity_from_updates(updates)
    if entity is None:
        entity = await client.get_entity(value)
    return entity, False


def discussion_target(entity: Any) -> int | None:
    """ID привязанной группы обсуждения, если сущность — канал, а не группа."""
    if not getattr(entity, "broadcast", False):
        return None
    value = getattr(entity, "linked_chat_id", None)
    return int(value) if value else None


async def _resolve_linked_group(client: Any, entity: Any, linked_id: int) -> Any | None:
    """Находит сущность группы обсуждения, чтобы в неё можно было вступить.

    linked_chat_id — только число без access_hash. Сначала пробуем кэш клиента,
    затем просим Telegram вернуть чат обсуждения последних постов.
    """
    from telethon.tl.functions.messages import GetDiscussionMessageRequest
    from telethon.tl.types import PeerChannel

    try:
        return await client.get_entity(PeerChannel(linked_id))
    except FloodWaitError:
        raise
    except Exception:  # noqa: BLE001 — в кэше может не быть access_hash
        pass

    try:
        messages = await client.get_messages(entity, limit=3)
    except FloodWaitError:
        raise
    except Exception as exc:  # noqa: BLE001 — у канала может не быть доступных постов
        logger.info(
            "Не удалось прочитать посты «%s»: %s", getattr(entity, "title", ""), exc
        )
        return None
    if not messages:
        return None

    last_error: Exception | None = None
    for message in messages:
        try:
            result = await client(
                GetDiscussionMessageRequest(peer=entity, msg_id=int(message.id))
            )
        except FloodWaitError:
            raise
        except Exception as exc:  # noqa: BLE001 — у поста могут быть выключены комментарии
            last_error = exc
            continue
        chats = list(getattr(result, "chats", None) or [])
        for chat in chats:
            if int(getattr(chat, "id", 0) or 0) == linked_id:
                return chat
        # Иногда Telegram кладёт в ответ сам канал: его вступать заново не нужно.
        own_id = int(getattr(entity, "id", 0) or 0)
        for chat in chats:
            chat_id = int(getattr(chat, "id", 0) or 0)
            if chat_id and chat_id != own_id:
                return chat
    if last_error is not None:
        logger.info(
            "Группа обсуждения «%s» не открылась: %s",
            getattr(entity, "title", ""),
            last_error,
        )
    return None


async def _join_linked_group(
    client: Any,
    entity: Any,
    account_index: int,
) -> tuple[Any | None, bool]:
    """Пробует вступить в привязанную группу. Возвращает (группа, вступили_впервые)."""
    linked_id = discussion_target(entity)
    if linked_id is None:
        return None, False

    from telethon.tl.functions.channels import JoinChannelRequest

    try:
        group = await _resolve_linked_group(client, entity, linked_id)
        if group is None:
            return None, False
        try:
            await client(JoinChannelRequest(group))
        except UserAlreadyParticipantError:
            return group, False
        except FloodWaitError:
            raise
        except ChannelsTooMuchError:
            _paused_accounts[account_index] = _utc_now() + timedelta(days=1)
            logger.warning(
                "На аккаунте %s закончились слоты чатов: группа «%s» не подключена",
                account_index,
                getattr(group, "title", ""),
            )
            return None, False
        except Exception as exc:  # noqa: BLE001 — канал уже подключён, группа не критична
            logger.warning(
                "Не удалось вступить в группу обсуждения «%s»: %s",
                getattr(group, "title", ""),
                exc,
            )
            return None, False
    except FloodWaitError as exc:
        seconds = int(getattr(exc, "seconds", 60)) + 60
        _paused_accounts[account_index] = _utc_now() + timedelta(seconds=seconds)
        logger.warning(
            "FloodWait на группе обсуждения, аккаунт %s: пауза %s с",
            account_index,
            seconds,
        )
        return None, False
    except Exception:  # noqa: BLE001 — не роняем уже выполненное вступление в канал
        logger.exception("Сбой автоподключения группы обсуждения")
        return None, False

    logger.info(
        "Вместе с каналом «%s» подключена группа обсуждения «%s»",
        getattr(entity, "title", ""),
        getattr(group, "title", ""),
    )
    return group, True


def _permanent_message(exc: Exception) -> str:
    if isinstance(exc, (InviteHashExpiredError, InviteHashInvalidError)):
        return "ссылка-приглашение недействительна или устарела"
    if isinstance(exc, InviteRequestSentError):
        return "заявка на вступление отправлена — нужно одобрение администратора"
    if isinstance(exc, UsernameNotOccupiedError):
        return "канал с таким адресом не найден"
    if isinstance(exc, UsernameInvalidError):
        return "некорректный адрес канала"
    if isinstance(exc, ChannelPrivateError):
        return "закрытый чат — нужна ссылка-приглашение"
    if isinstance(exc, ChatAdminRequiredError):
        return "для вступления нужны права администратора"
    return f"{type(exc).__name__}: {exc}"[:500]


async def _handle_join_error(
    task_id: int,
    account_index: int,
    exc: Exception,
    settings: Settings,
) -> None:
    permanent = isinstance(exc, _PERMANENT_ERRORS)
    channel_limit = isinstance(exc, ChannelsTooMuchError)
    message = (
        "на аккаунте уже 500 чатов — нужен другой аккаунт"
        if channel_limit
        else _permanent_message(exc)
    )
    if isinstance(exc, AdapterUnavailable):
        message = f"аккаунт {account_index} не авторизован — проверьте TG_SESSION_{account_index}"
    if channel_limit:
        # Этот аккаунт больше не трогаем сутки, задачу отдадим другому.
        _paused_accounts[account_index] = _utc_now() + timedelta(days=1)

    async with SessionLocal() as session:
        task = await session.get(JoinTask, task_id)
        if task is None:
            return
        task.last_error = message
        if permanent:
            task.status = "error"
        elif (task.attempts or 0) >= MAX_ATTEMPTS:
            task.status = "error"
            task.last_error = f"{message} (попыток: {task.attempts})"
        else:
            task.status = "pending"
            task.next_at = _utc_now() + timedelta(minutes=settings.join_retry_minutes)
        await session.commit()

    logger.warning("Вступление #%s (аккаунт %s) не удалось: %s", task_id, account_index, message)


async def process_join_tasks(settings: Settings, pool: AccountPool) -> int:
    """Делает не больше одного вступления за вызов. Возвращает 1, если была попытка."""
    global _last_join_at

    if not settings.join_queue_enabled or not pool.ready():
        return 0

    now = _utc_now()
    if _last_join_at is not None:
        if (now - _last_join_at).total_seconds() < settings.join_pause_seconds:
            return 0

    async with SessionLocal() as session:
        account_index = await _pick_account(session, settings, pool)
        if account_index is None:
            return 0
        task = await _claim_task(session, now)
        if task is None:
            return 0

        # Аренда на 10 минут: если воркер упадёт посреди вступления, задача повторится.
        task.account_index = account_index
        task.attempts = (task.attempts or 0) + 1
        task.last_attempt_at = now
        task.next_at = now + timedelta(minutes=10)
        await session.commit()
        task_id, user_id, ref, kind = task.id, task.user_id, task.ref, task.kind

    try:
        client = await pool.client(account_index)
        entity, existed = await _join(client, kind, ref)
        group = None
        group_joined = False
        if settings.join_discussion_groups:
            group, group_joined = await _join_linked_group(client, entity, account_index)
    except AdapterUnavailable as exc:
        # Битый аккаунт не должен забирать все задачи: отдадим их другим на 6 часов.
        _last_join_at = _utc_now()
        _paused_accounts[account_index] = _utc_now() + timedelta(hours=6)
        await _handle_join_error(task_id, account_index, exc, settings)
        return 1
    except FloodWaitError as exc:
        _last_join_at = _utc_now()
        seconds = int(getattr(exc, "seconds", 60)) + 60
        _paused_accounts[account_index] = _utc_now() + timedelta(seconds=seconds)
        async with SessionLocal() as session:
            task = await session.get(JoinTask, task_id)
            if task is not None:
                task.status = "pending"
                task.attempts = max(0, (task.attempts or 1) - 1)
                task.last_error = f"лимит Telegram: повтор через {seconds} с"
                task.next_at = _utc_now() + timedelta(seconds=seconds)
                await session.commit()
        logger.warning("FloodWait на аккаунте %s: пауза %s с", account_index, seconds)
        return 1
    except Exception as exc:  # noqa: BLE001 — ошибку покажем в очереди, воркер не роняем
        _last_join_at = _utc_now()
        await _handle_join_error(task_id, account_index, exc, settings)
        return 1

    _last_join_at = _utc_now()

    try:
        async with SessionLocal() as session:
            task = await session.get(JoinTask, task_id)
            if task is None:
                return 1
            user = await session.get(User, user_id)
            if user is None:
                task.status = "error"
                task.last_error = "пользователь не найден"
                await session.commit()
                return 1
            source, _ = await ensure_dialog_source(
                session,
                user,
                account_index,
                entity,
                scan_interval_minutes=settings.default_crawl_interval_minutes,
            )
            task.status = "exists" if existed else "joined"
            task.source_id = source.id
            # При повторе сохраняем факт уже выполненного вступления в группу.
            task.group_joined = bool(task.group_joined or group_joined)
            task.last_error = None
            task.joined_at = _utc_now()
            task.next_at = _utc_now()
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — чат уже добавлен, но источник не записался
        logger.exception("Чат добавлен, но источник не сохранился (задача #%s)", task_id)
        await _handle_join_error(task_id, account_index, exc, settings)
        return 1

    if group is not None:
        try:
            # Источник группы заводим отдельной сессией: сбой не должен
            # отменять уже сохранённое вступление в канал.
            async with SessionLocal() as group_session:
                group_user = await group_session.get(User, user_id)
                if group_user is not None:
                    await ensure_dialog_source(
                        group_session,
                        group_user,
                        account_index,
                        group,
                        scan_interval_minutes=settings.default_crawl_interval_minutes,
                    )
        except Exception:  # noqa: BLE001 — группу подхватит плановый обход диалогов
            logger.exception(
                "Группа обсуждения «%s» подключена, но источник не сохранился",
                getattr(group, "title", ""),
            )

    logger.info(
        "Задача #%s: %s «%s» аккаунтом %s",
        task_id,
        "уже состоял в" if existed else "вступил в",
        getattr(entity, "title", ref),
        account_index,
    )
    return 1
