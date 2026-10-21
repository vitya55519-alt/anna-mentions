from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime

from app.models import Network, Source


class AdapterError(Exception):
    """Ошибка источника, которую можно показать пользователю."""


class AdapterUnavailable(AdapterError):
    """Для сети не хватает настроек (токена, сессии, разрешений приложения)."""


@dataclass(slots=True)
class CommentData:
    external_id: str
    text: str
    published_at: datetime
    author_name: str | None = None
    author_url: str | None = None
    author_external_id: str | None = None
    likes: int = 0
    parent_external_id: str | None = None
    url: str | None = None


@dataclass(slots=True)
class PostData:
    external_id: str
    url: str
    published_at: datetime
    text: str = ""
    author_name: str | None = None
    comments_count: int = 0
    comments: list[CommentData] = field(default_factory=list)


@dataclass(slots=True)
class ResolvedRef:
    network: Network
    external_id: str
    handle: str
    title: str
    url: str
    avatar_url: str | None = None
    description: str | None = None


class SourceAdapter(ABC):
    """Контракт интеграции: разрешить ссылку и отдать посты с комментариями."""

    network: Network
    mode: str = "live"
    hint: str = ""

    def ready(self) -> bool:
        return True

    @abstractmethod
    async def resolve(self, ref: str) -> ResolvedRef: ...

    @abstractmethod
    def fetch(
        self,
        source: Source,
        *,
        posts_limit: int,
        comments_limit: int,
    ) -> AsyncIterator[PostData]: ...

    async def close(self) -> None:
        return None
