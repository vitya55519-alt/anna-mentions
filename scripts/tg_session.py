"""Генерирует строки сессий TG_SESSION_1..5 для аккаунтов-сборщиков.

Запуск:
    python scripts/tg_session.py

Нужны TG_API_ID и TG_API_HASH с https://my.telegram.org → API development tools
(одно приложение на все аккаунты). Скрипт по очереди входит в каждый аккаунт:
спрашивает номер телефона, код из Telegram и пароль 2FA, если он включён.

Файлы сессий никуда не пишутся: строка выводится в консоль и передаётся
в переменную окружения TG_SESSION_1, TG_SESSION_2 и так далее.

Строка сессии = полный доступ к аккаунту. Храните её как пароль
и не публикуйте в репозитории.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

MAX_ACCOUNTS = 5


def normalize_phone(raw: str) -> str:
    """8..., 9... и разный мусор превращает в +7XXXXXXXXXX."""
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits


async def login(index: int, api_id: int, api_hash: str) -> str:
    print(f"\n--- Аккаунт {index} ---")
    phone = ""
    while not phone:
        phone = normalize_phone(input("Номер телефона аккаунта (можно 8..., 9... или +7...): "))
        if phone:
            print(f"Отправляю код на {phone}")

    client = TelegramClient(StringSession(), api_id, api_hash)
    password = input("Пароль 2FA, если включён (ввод отображается; нет — просто Enter): ").strip()
    if password:
        await client.start(phone=phone, password=password)
    else:
        print("Пароль не введён. Если включена двухэтапная проверка, будет вопрос")
        print("'Please enter your password:' — ввод скрытый, наберите пароль и Enter.")
        await client.start(phone=phone)
    try:
        me = await client.get_me()
        name = " ".join(part for part in (me.first_name, me.last_name) if part) or "без имени"
        username = f" (@{me.username})" if me.username else ""
        print(f"Аккаунт {index}: {name}{username}, id {me.id}")
        return client.session.save()
    finally:
        await client.disconnect()


async def main() -> None:
    api_id = int(os.getenv("TG_API_ID") or input("TG_API_ID: ").strip() or 0)
    api_hash = (os.getenv("TG_API_HASH") or input("TG_API_HASH: ").strip()).strip()
    if not api_id or not api_hash:
        raise SystemExit("Нужны TG_API_ID и TG_API_HASH")

    sessions: dict[int, str] = {}
    for index in range(1, MAX_ACCOUNTS + 1):
        try:
            sessions[index] = await login(index, api_id, api_hash)
        except KeyboardInterrupt:
            print("\nВход прерван")
            break

        answer = input("Добавить ещё аккаунт? (y/N): ").strip().lower()
        if answer not in {"y", "yes", "д", "да"}:
            break

    if not sessions:
        raise SystemExit("Ни одного аккаунта не привязано")

    print("\n=== Переменные для Railway / .env ===")
    print("Скопируйте строки целиком, без переносов:\n")
    for index, session in sessions.items():
        print(f"TG_SESSION_{index}={session}")

    print(
        "\nВ интерфейсе бота: /scan — обойти группы, /acc — сколько аккаунтов на связи."
    )
    print("Строки сессий больше нигде не показываются — сохраните их сейчас.")
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено")
