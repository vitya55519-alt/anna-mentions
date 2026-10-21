from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.adapters.base import AdapterUnavailable
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Account:
    index: int
    session: str

    @property
    def label(self) -> str:
        return f"аккаунт {self.index}"


class AccountPool:
    """Пул пользовательских сессий: одно приложение Telegram, несколько аккаунтов.

    Каждый аккаунт видит свои группы, поэтому вместе они дают более широкий охват.
    Клиент поднимается лениво и живёт до остановки воркера.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._clients: dict[int, object] = {}
        self._lock = asyncio.Lock()

    @property
    def accounts(self) -> list[Account]:
        return [
            Account(index=index, session=session)
            for index, session in enumerate(self._settings.tg_sessions, start=1)
        ]

    def ready(self) -> bool:
        return self._settings.tg_accounts_ready

    def hint(self) -> str:
        return (
            "Аккаунты не настроены: нужны TG_API_ID, TG_API_HASH и TG_SESSION_1..3 "
            "(строки получаются скриптом scripts/tg_session.py)"
        )

    async def client(self, index: int = 1):
        if not self.ready():
            raise AdapterUnavailable(self.hint())

        sessions = self._settings.tg_sessions
        if not 1 <= index <= len(sessions):
            index = 1

        async with self._lock:
            cached = self._clients.get(index)
            if cached is not None:
                return cached

            from telethon import TelegramClient
            from telethon.sessions import StringSession

            client = TelegramClient(
                StringSession(sessions[index - 1]),
                self._settings.tg_api_id,
                self._settings.tg_api_hash,
                device_model="CommentRadar",
                system_version="1.0",
                app_version="1.0",
            )
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                raise AdapterUnavailable(
                    f"Сессия TG_SESSION_{index} не авторизована — получите новую через scripts/tg_session.py"
                )
            self._clients[index] = client
            logger.info("Подключён аккаунт %s", index)
            return client

    async def close(self) -> None:
        for index, client in list(self._clients.items()):
            try:
                await client.disconnect()
            except Exception as exc:  # noqa: BLE001 — при остановке это не важно
                logger.warning("Не смог отключить аккаунт %s: %s", index, exc)
        self._clients.clear()


_pool: AccountPool | None = None


def get_pool(settings: Settings | None = None) -> AccountPool:
    """Один пул на процесс: бот и воркер работают в одном контейнере."""
    global _pool
    if _pool is None:
        _pool = AccountPool(settings or get_settings())
    return _pool
