from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from app.adapters.base import AdapterUnavailable
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Аккаунты из базы «tg_accounts» получают индексы с большим сдвигом,
# чтобы не пересекаться с TG_SESSION_1..5 из переменных окружения.
DB_INDEX_BASE = 1000
REFRESH_INTERVAL_SECONDS = 15.0


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
        # Сессии из базы: индекс → строка сессии. Наполняется в refresh().
        self._extra: dict[int, str] = {}
        self._refreshed_at: float = 0.0
        self._refresh_lock = asyncio.Lock()

    @property
    def accounts(self) -> list[Account]:
        env = [
            Account(index=index, session=session)
            for index, session in enumerate(self._settings.tg_sessions, start=1)
        ]
        env.extend(
            Account(index=index, session=session)
            for index, session in sorted(self._extra.items())
        )
        return env

    def ready(self) -> bool:
        return bool(self._settings.tg_sessions) or bool(self._extra)

    async def refresh(self, *, force: bool = False) -> None:
        """Подтягивает аккаунты, добавленные через админку (таблица tg_accounts)."""
        now = time.monotonic()
        if not force and now - self._refreshed_at < REFRESH_INTERVAL_SECONDS:
            return
        async with self._refresh_lock:
            again = time.monotonic()
            if not force and again - self._refreshed_at < REFRESH_INTERVAL_SECONDS:
                return
            from sqlalchemy import select

            from app.db import SessionLocal
            from app.models import TgAccount

            try:
                async with SessionLocal() as session:
                    rows = (
                        await session.execute(
                            select(TgAccount).where(TgAccount.is_active.is_(True))
                        )
                    ).scalars().all()
            except Exception as exc:  # noqa: BLE001 — таблицы может ещё не быть
                logger.debug("Пул аккаунтов: база недоступна (%s)", exc)
                self._refreshed_at = again
                return

            fresh = {DB_INDEX_BASE + row.id: row.session for row in rows}
            stale = [index for index in self._clients if index >= DB_INDEX_BASE and index not in fresh]
            changed = [index for index in fresh if self._extra.get(index) != fresh[index]]
            self._extra = fresh
            self._refreshed_at = again

            for index in set(stale) | set(changed):
                client = self._clients.pop(index, None)
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001 — при остановке это не важно
                        pass
            if stale:
                logger.info("Пул аккаунтов: отключены %s", stale)
            if fresh and not self._clients:
                logger.info("Пул аккаунтов: доступны %s", sorted(fresh))

    def hint(self) -> str:
        return (
            "Аккаунты не настроены: добавьте их в админке приложения «Источники» — «Аккаунты» "
            "или переменными TG_SESSION_1..3 (scripts/tg_session.py)"
        )

    async def client(self, index: int = 1):
        if not self.ready():
            raise AdapterUnavailable(self.hint())

        sessions = self._settings.tg_sessions
        session: str | None = None
        if 1 <= index <= len(sessions):
            session = sessions[index - 1]
        else:
            session = self._extra.get(index)
        if session is None:
            index = 1
            session = sessions[0] if sessions else next(iter(self._extra.values()))

        async with self._lock:
            cached = self._clients.get(index)
            if cached is not None:
                return cached

            from telethon import TelegramClient
            from telethon.sessions import StringSession

            client = TelegramClient(
                StringSession(session),
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
                    f"Сессия аккаунта {index} не авторизована — добавьте его заново в админке"
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
