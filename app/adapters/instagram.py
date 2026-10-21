from __future__ import annotations

from collections.abc import AsyncIterator

from app.adapters.base import AdapterUnavailable, PostData, ResolvedRef, SourceAdapter
from app.config import Settings
from app.models import Network, Source

MESSAGE = (
    "Instagram не подключён. Graph API отдаёт комментарии только для медиа вашего "
    "бизнес-аккаунта, глобального поиска комментариев у Meta нет. "
    "Скрапинг нарушает ToS и приводит к блокировке аккаунта."
)


class InstagramAdapter(SourceAdapter):
    network = Network.instagram
    mode = "disabled"
    hint = MESSAGE

    def __init__(self, settings: Settings) -> None:
        self._token = settings.ig_access_token
        self._business_id = settings.ig_business_id
        self._enabled = settings.instagram_enabled

    def ready(self) -> bool:
        return bool(self._enabled and self._token and self._business_id)

    async def resolve(self, ref: str) -> ResolvedRef:
        raise AdapterUnavailable(MESSAGE)

    async def fetch(
        self,
        source: Source,
        *,
        posts_limit: int,
        comments_limit: int,
    ) -> AsyncIterator[PostData]:
        raise AdapterUnavailable(MESSAGE)
        yield  # pragma: no cover — делает метод асинхронным генератором
