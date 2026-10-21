from __future__ import annotations

import asyncio
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

API_BASE = "https://api.vk.com/method/"
RETRY_ERROR_CODES = {6, 9, 10}
COMMENTS_PAGE = 100

LINK_RE = re.compile(
    r"(?:https?://)?(?:m\.|www\.)?(?:vk\.com|vk\.ru|vkontakte\.ru)/(?P<slug>[^/?#\s]+)",
    re.IGNORECASE,
)
CLUB_RE = re.compile(r"^(?:club|public)(?P<id>\d+)$", re.IGNORECASE)


class VKAdapter(SourceAdapter):
    """ВКонтакте через официальный API. Требуется токен с доступом к wall.getComments."""

    network = Network.vk
    mode = "live"
    hint = "Нужен VK_TOKEN (токен пользователя или сообщества с доступом к комментариям)."

    def __init__(self, settings: Settings) -> None:
        self._token = settings.vk_access_token
        self._version = settings.vk_api_version
        self._client: httpx.AsyncClient | None = None

    def ready(self) -> bool:
        return bool(self._token)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        return self._client

    async def _call(self, method: str, **params: object) -> object:
        if not self._token:
            raise AdapterUnavailable("Не задан VK_TOKEN — добавьте токен ВКонтакте в переменные окружения")

        payload = {**params, "access_token": self._token, "v": self._version, "lang": "ru"}
        last_error: str = ""

        for attempt in range(3):
            try:
                response = await self._http().post(API_BASE + method, data=payload)
                body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise AdapterError(f"VK: сеть недоступна ({exc.__class__.__name__})") from exc

            if "error" not in body:
                return body.get("response")

            error = body["error"]
            code = error.get("error_code")
            last_error = str(error.get("error_msg") or "")
            if code in RETRY_ERROR_CODES and attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise AdapterError(f"VK {method}: {last_error} (код {code})")

        raise AdapterError(f"VK {method}: {last_error or 'превышен лимит запросов'}")

    async def resolve(self, ref: str) -> ResolvedRef:
        slug = self._slug(ref)
        club_match = CLUB_RE.match(slug)
        if club_match:
            return await self._resolve_group(int(club_match.group("id")), slug)

        response = await self._call("utils.resolveScreenName", screen_name=slug)
        if not isinstance(response, dict) or not response:
            raise AdapterError(f"VK: не нашёл «{slug}». Проверьте ссылку на сообщество")

        if response.get("type") == "user":
            raise AdapterError(
                "VK: личные страницы недоступны — добавьте сообщество или публичную страницу"
            )

        return await self._resolve_group(int(response["object_id"]), slug)

    async def _resolve_group(self, group_id: int, fallback_slug: str) -> ResolvedRef:
        response = await self._call(
            "groups.getById",
            group_id=group_id,
            fields="photo_100,description,members_count",
        )
        groups = response.get("groups", []) if isinstance(response, dict) else response or []
        group = groups[0] if groups else {}

        screen_name = group.get("screen_name") or fallback_slug
        return ResolvedRef(
            network=self.network,
            external_id=str(group_id),
            handle=screen_name,
            title=group.get("name") or fallback_slug,
            url=f"https://vk.com/{screen_name}",
            avatar_url=group.get("photo_100"),
            description=(group.get("description") or "").strip() or None,
        )

    async def fetch(
        self,
        source: Source,
        *,
        posts_limit: int,
        comments_limit: int,
    ) -> AsyncIterator[PostData]:
        owner_id = -int(source.external_id)
        response = await self._call(
            "wall.get",
            owner_id=owner_id,
            count=posts_limit,
            filter="all",
        )
        items = (response or {}).get("items", []) if isinstance(response, dict) else []

        for item in items:
            post_id = item.get("id")
            if post_id is None:
                continue

            total = int((item.get("comments") or {}).get("count") or 0)
            post = PostData(
                external_id=str(post_id),
                url=f"https://vk.com/wall{owner_id}_{post_id}",
                published_at=datetime.fromtimestamp(int(item.get("date") or 0), tz=timezone.utc),
                text=(item.get("text") or "").strip(),
                author_name=source.title,
                comments_count=min(total, comments_limit),
            )
            if total and comments_limit > 0:
                try:
                    post.comments = await self._comments(owner_id, int(post_id), comments_limit)
                except AdapterError as exc:
                    if "код 15" in str(exc):
                        raise AdapterError(
                            "VK: комментарии этого сообщества закрыты или недоступны токену"
                        ) from exc
                    raise
            yield post

    async def _comments(self, owner_id: int, post_id: int, limit: int) -> list[CommentData]:
        collected: list[CommentData] = []
        offset = 0
        page_url = f"https://vk.com/wall{owner_id}_{post_id}"

        while len(collected) < limit:
            batch = min(COMMENTS_PAGE, limit - len(collected))
            response = await self._call(
                "wall.getComments",
                owner_id=owner_id,
                post_id=post_id,
                count=batch,
                offset=offset,
                sort="asc",
                need_likes=1,
                thread_items_count=10,
                extended=1,
            )
            if not isinstance(response, dict):
                break

            authors = _collect_authors(response)
            items = response.get("items") or []
            if not items:
                break

            for raw in items:
                collected.append(_to_comment(raw, authors, page_url, parent=None))
                for nested in (raw.get("thread") or {}).get("items") or []:
                    if len(collected) >= limit:
                        break
                    collected.append(
                        _to_comment(nested, authors, page_url, parent=str(raw.get("id")))
                    )
                if len(collected) >= limit:
                    break

            if len(items) < batch:
                break
            offset += len(items)

        return collected[:limit]

    @staticmethod
    def _slug(ref: str) -> str:
        text = ref.strip()
        match = LINK_RE.search(text)
        if match:
            text = match.group("slug")
        return text.strip("/@ ").split("?")[0]


def _collect_authors(response: dict) -> dict[int, tuple[str | None, str | None]]:
    authors: dict[int, tuple[str | None, str | None]] = {}
    for profile in response.get("profiles") or []:
        name = " ".join(
            part for part in (profile.get("first_name"), profile.get("last_name")) if part
        ).strip()
        link = (
            f"https://vk.com/{profile['screen_name']}"
            if profile.get("screen_name")
            else f"https://vk.com/id{profile.get('id')}"
        )
        authors[int(profile.get("id", 0))] = (name or None, link)
    for group in response.get("groups") or []:
        link = f"https://vk.com/{group.get('screen_name')}" if group.get("screen_name") else None
        authors[-int(group.get("id", 0))] = (group.get("name"), link)
    return authors


def _to_comment(
    raw: dict,
    authors: dict[int, tuple[str | None, str | None]],
    page_url: str,
    parent: str | None,
) -> CommentData:
    from_id = raw.get("from_id")
    name, link = authors.get(int(from_id), (None, None)) if from_id is not None else (None, None)
    comment_id = raw.get("id")

    return CommentData(
        external_id=str(comment_id),
        text=(raw.get("text") or "").strip(),
        published_at=datetime.fromtimestamp(int(raw.get("date") or 0), tz=timezone.utc),
        author_name=name,
        author_url=link,
        author_external_id=str(from_id) if from_id is not None else None,
        likes=int((raw.get("likes") or {}).get("count") or 0),
        parent_external_id=parent,
        url=f"{page_url}?reply={comment_id}" if comment_id is not None else page_url,
    )
