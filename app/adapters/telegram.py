from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator

from app.adapters.base import (
    AdapterError,
    AdapterUnavailable,
    CommentData,
    PostData,
    ResolvedRef,
    SourceAdapter,
)
from app.config import Settings
from app.models import Network, Source

logger = logging.getLogger(__name__)

LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:s/)?(?P<slug>[^/?#\s]+)", re.IGNORECASE)


class TelegramAdapter(SourceAdapter):
    """Каналы Telegram через MTProto (Telethon + пользовательская сессия).

    Bot API не умеет читать историю комментариев канала, поэтому нужен user-session.
    Комментарии берём из связанной группы обсуждения по reply_to = id поста.
    """

    network = Network.telegram
    mode = "live"
    hint = "Нужны TG_API_ID, TG_API_HASH и TG_SESSION_STRING (сессия вашего аккаунта)."

    def __init__(self, settings: Settings) -> None:
        self._api_id = settings.tg_api_id
        self._api_hash = settings.tg_api_hash
        self._session = settings.tg_session_string
        self._client = None
        self._lock: asyncio.Lock | None = None

    def ready(self) -> bool:
        return bool(self._api_id and self._api_hash and self._session)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    async def _get_client(self):
        if not self.ready():
            raise AdapterUnavailable(
                "Telegram-источники отключены: задайте TG_API_ID, TG_API_HASH и TG_SESSION_STRING"
            )
        if self._lock is None:
            self._lock = asyncio.Lock()

        async with self._lock:
            if self._client is None:
                from telethon import TelegramClient
                from telethon.sessions import StringSession

                client = TelegramClient(StringSession(self._session), self._api_id, self._api_hash)
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    raise AdapterUnavailable(
                        "Сессия Telegram не авторизована — пересоздайте TG_SESSION_STRING"
                    )
                self._client = client
        return self._client

    async def resolve(self, ref: str) -> ResolvedRef:
        client = await self._get_client()
        slug = self._slug(ref)

        from telethon.tl.types import Channel

        try:
            entity = await client.get_entity(slug)
        except Exception as exc:  # noqa: BLE001 — Telethon бросает разные типы
            raise AdapterError(f"Telegram: не удалось открыть «{slug}» ({exc.__class__.__name__})") from exc

        if not isinstance(entity, Channel):
            raise AdapterError("Telegram: нужен канал или супергруппа с включёнными комментариями")

        username = getattr(entity, "username", None)
        return ResolvedRef(
            network=self.network,
            external_id=str(entity.id),
            handle=username or slug,
            title=entity.title or slug,
            url=f"https://t.me/{username}" if username else f"https://t.me/c/{entity.id}",
            description=None,
        )

    async def fetch(
        self,
        source: Source,
        *,
        posts_limit: int,
        comments_limit: int,
    ) -> AsyncIterator[PostData]:
        client = await self._get_client()

        from telethon.errors import FloodWaitError

        try:
            entity = await client.get_entity(int(source.external_id))
        except Exception as exc:  # noqa: BLE001
            try:
                entity = await client.get_entity(source.handle)
            except Exception:  # noqa: BLE001
                raise AdapterError(f"Telegram: канал {source.handle} недоступен") from exc

        handle = getattr(entity, "username", None) or source.handle

        async def _iter():
            async for message in client.iter_messages(entity, limit=posts_limit):
                yield message

        produced = 0
        try:
            async for message in _iter():
                if produced >= posts_limit:
                    break
                produced += 1

                post_url = f"https://t.me/{handle}/{message.id}"
                replies = getattr(message, "replies", None)
                total = int(getattr(replies, "replies", 0) or 0)

                post = PostData(
                    external_id=str(message.id),
                    url=post_url,
                    published_at=message.date,
                    text=(message.message or "").strip(),
                    author_name=getattr(entity, "title", None),
                    comments_count=min(total, comments_limit),
                )

                if total and comments_limit > 0 and replies and replies.channel_id:
                    post.comments = await self._comments(
                        message_id=message.id,
                        discussion_id=replies.channel_id,
                        handle=handle,
                        limit=comments_limit,
                    )
                yield post
        except FloodWaitError as exc:
            raise AdapterError(
                f"Telegram: лимит запросов, нужно подождать {exc.seconds} с"
            ) from exc

    async def _comments(
        self,
        *,
        message_id: int,
        discussion_id: int,
        handle: str,
        limit: int,
    ) -> list[CommentData]:
        client = await self._get_client()
        from telethon.tl.types import PeerChannel

        discussion = await client.get_entity(PeerChannel(discussion_id))
        result: list[CommentData] = []

        async for reply in client.iter_messages(discussion, reply_to=message_id, limit=limit):
            sender_name, sender_link = await self._sender(client, reply)
            result.append(
                CommentData(
                    external_id=str(reply.id),
                    text=(reply.message or "").strip(),
                    published_at=reply.date,
                    author_name=sender_name,
                    author_url=sender_link,
                    author_external_id=str(reply.sender_id) if reply.sender_id else None,
                    likes=0,
                    parent_external_id=str(reply.reply_to.reply_to_msg_id)
                    if getattr(reply, "reply_to", None)
                    else None,
                    url=f"https://t.me/{handle}/{message_id}?comment={reply.id}",
                )
            )
        return result

    @staticmethod
    async def _sender(client, message) -> tuple[str | None, str | None]:
        try:
            sender = await message.get_sender()
        except Exception:  # noqa: BLE001
            return None, None
        if sender is None:
            return None, None

        title = getattr(sender, "title", None)
        if title:
            username = getattr(sender, "username", None)
            return title, f"https://t.me/{username}" if username else None

        name = " ".join(
            part
            for part in (getattr(sender, "first_name", None), getattr(sender, "last_name", None))
            if part
        ).strip()
        username = getattr(sender, "username", None)
        return (name or None, f"https://t.me/{username}" if username else None)

    @staticmethod
    def _slug(ref: str) -> str:
        text = ref.strip()
        match = LINK_RE.search(text)
        if match:
            text = match.group("slug")
        return text.strip("/@ ")
