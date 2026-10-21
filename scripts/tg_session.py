"""Генерирует TG_SESSION_STRING для Telegram-адаптера.

Запуск:
    python scripts/tg_session.py

Нужны TG_API_ID и TG_API_HASH с https://my.telegram.org → API development tools.
Скрипт попросит номер телефона и код из Telegram. Файл сессии никуда не пишется:
строка выводится в консоль и передаётся в переменную окружения TG_SESSION_STRING.
"""

from __future__ import annotations

import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    api_id = int(input("TG_API_ID: ").strip())
    api_hash = input("TG_API_HASH: ").strip()
    if not api_id or not api_hash:
        raise SystemExit("Нужны api_id и api_hash")

    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        me = await client.get_me()
        print("\nАккаунт:", me.first_name, f"(@{me.username})" if me.username else "")
        print("\nTG_SESSION_STRING (скопируйте целиком):\n")
        print(client.session.save())


if __name__ == "__main__":
    asyncio.run(main())
