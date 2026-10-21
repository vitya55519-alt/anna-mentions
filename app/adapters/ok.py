from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx

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

API_URL = "https://api.ok.ru/fb.do"
GROUP_RE = re.compile(r"(?:ok\.ru|odnoklassniki\.ru)/(?:group/|groups/)?(?P<slug>[\w.-]+)", re.IGNORECASE)


class OKAdapter(SourceAdapter):
    """Одноклассники через api.ok.ru.

    Требуется зарегистрированное приложение OK (ключ + секрет) и access_token
    с правами на чтение группы. Запросы подписываются MD5-подписью.

    Статус: experimental. OK отдаёт комментарии в рамках обсуждения, а не отдельным
    поисковым методом, поэтому обход идёт по темам группы. Набор полей ответа
    сверяйте с актуальной документацией apiok.ru — сообщение об ошибке API
    показывается в карточке источника как есть.
    """

    network = Network.ok
    mode = "experimental"
    hint = "Нужны OK_APP_KEY, OK_APP_SECRET, OK_ACCESS_TOKEN и OK_ADAPTER_ENABLED=true."

    def __init__(self, settings: Settings) -> None:
        self._key = settings.ok_app_key
        self._secret = settings.ok_app_secret
        self._token = settings.ok_access_token
        self._enabled = settings.ok_adapter_enabled
        self._client: httpx.AsyncClient | None = None

    def ready(self) -> bool:
        return bool(self._key and self._secret and self._token and self._enabled)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _call(self, method: str, **params: object) -> dict:
        if not self.ready():
            raise AdapterUnavailable(
                "Одноклассники отключены: нужны OK_APP_KEY, OK_APP_SECRET, OK_ACCESS_TOKEN "
                "и OK_ADAPTER_ENABLED=true"
            )

        query = {key: str(value) for key, value in params.items()}
        query.update(application_key=self._key, method=method, format="json")
        session_secret = hashlib.md5(f"{self._token}{self._secret}".encode()).hexdigest()
        payload = "".join(f"{key}={value}" for key, value in sorted(query.items()))
        query["sig"] = hashlib.md5(f"{payload}{session_secret}".encode()).hexdigest()
        query["access_token"] = self._token

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)

        try:
            response = await self._client.get(API_URL, params=query)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AdapterError(f"OK: сеть недоступна ({exc.__class__.__name__})") from exc

        if "error_code" in body or "error_msg" in body:
            raise AdapterError(f"OK {method}: {body.get('error_msg') or body.get('error_code')}")
        return body

    async def resolve(self, ref: str) -> ResolvedRef:
        slug = self._slug(ref)
        if not slug.isdigit():
            raise AdapterError(
                "OK: укажите ссылку вида ok.ru/group/<id> — поиск по короткому адресу не поддерживается"
            )

        response = await self._call("group.getInfo", uids=slug, fields="name,description")
        groups = response.get("groups") or []
        group = groups[0] if groups else {}

        return ResolvedRef(
            network=self.network,
            external_id=str(slug),
            handle=f"group/{slug}",
            title=group.get("name") or f"Группа {slug}",
            url=f"https://ok.ru/group/{slug}",
            description=(group.get("description") or "").strip() or None,
        )

    async def fetch(
        self,
        source: Source,
        *,
        posts_limit: int,
        comments_limit: int,
    ) -> AsyncIterator[PostData]:
        listing = await self._call(
            "discussions.get",
            gid=source.external_id,
            offset=0,
            count=posts_limit,
        )
        topics = listing.get("discussions")
        if topics is None:
            raise AdapterError(
                "OK: в ответе discussions.get нет поля discussions — проверьте права токена "
                "и формат ответа API"
            )

        for topic in topics[:posts_limit]:
            topic_id = topic.get("id")
            if not topic_id:
                continue

            post = PostData(
                external_id=str(topic_id),
                url=f"https://ok.ru/group/{source.external_id}/topic/{topic_id}",
                published_at=_to_datetime(topic.get("date") or topic.get("lastCommentDate")),
                text=(topic.get("title") or topic.get("text") or "").strip(),
                author_name=source.title,
                comments_count=int(topic.get("commentsCount") or 0),
            )

            if comments_limit > 0:
                detail = await self._call(
                    "discussions.get",
                    discussionId=str(topic_id),
                    count=comments_limit,
                    offset=0,
                )
                for raw in (detail.get("comments") or [])[:comments_limit]:
                    post.comments.append(
                        CommentData(
                            external_id=str(raw.get("id")),
                            text=(raw.get("text") or "").strip(),
                            published_at=_to_datetime(raw.get("date")),
                            author_name=raw.get("authorName") or raw.get("author", {}).get("name"),
                            author_url=raw.get("authorUrl"),
                            author_external_id=str(raw.get("authorId") or "") or None,
                            likes=int(raw.get("likeCount") or 0),
                            parent_external_id=str(raw.get("parentId") or "") or None,
                            url=f"{post.url}?comment={raw.get('id')}",
                        )
                    )

            yield post

    @staticmethod
    def _slug(ref: str) -> str:
        text = ref.strip()
        match = GROUP_RE.search(text)
        if match:
            text = match.group("slug")
        return text.strip("/@ ")


def _to_datetime(value: object) -> datetime:
    """OK отдаёт время в миллисекундах, VK-подобные методы — в секундах."""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return datetime.now(tz=timezone.utc)
    if number > 10_000_000_000:
        number //= 1000
    return datetime.fromtimestamp(number, tz=timezone.utc)
