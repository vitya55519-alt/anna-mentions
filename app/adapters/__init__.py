from __future__ import annotations

from functools import lru_cache

from app.adapters.base import (
    AdapterError,
    AdapterUnavailable,
    CommentData,
    PostData,
    ResolvedRef,
    SourceAdapter,
)
from app.adapters.instagram import InstagramAdapter
from app.adapters.ok import OKAdapter
from app.adapters.telegram import TelegramAdapter
from app.adapters.vk import VKAdapter
from app.config import get_settings
from app.models import Network

__all__ = [
    "AdapterError",
    "AdapterUnavailable",
    "CommentData",
    "PostData",
    "ResolvedRef",
    "SourceAdapter",
    "capabilities",
    "close_adapters",
    "get_adapter",
]

_ADAPTERS: dict[Network, type[SourceAdapter]] = {
    Network.vk: VKAdapter,
    Network.ok: OKAdapter,
    Network.telegram: TelegramAdapter,
    Network.instagram: InstagramAdapter,
}


@lru_cache(maxsize=len(_ADAPTERS))
def get_adapter(network: Network) -> SourceAdapter:
    return _ADAPTERS[network](get_settings())


def capabilities() -> list[dict]:
    """Показывает только включённые сети: остальные адаптеры живут в коде, но скрыты."""
    enabled = get_settings().enabled_network_values
    result = []
    for network, _ in _ADAPTERS.items():
        if network.value not in enabled:
            continue
        adapter = get_adapter(network)
        result.append(
            {
                "network": network,
                "title": network.title,
                "mode": adapter.mode,
                "ready": adapter.ready(),
                "hint": adapter.hint,
            }
        )
    return result


async def close_adapters() -> None:
    for network in _ADAPTERS:
        await get_adapter(network).close()
