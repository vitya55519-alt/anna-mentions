"""Быстрая самопроверка без базы: рубрики, tsquery, подпись initData, DDL, Telegram-режим.

Запуск: python scripts/selftest.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.types import Chat, Message  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.schema import CreateTable  # noqa: E402

from app.adapters import capabilities  # noqa: E402
from app.api.routes import _require_enabled  # noqa: E402
from app.bot import chat_link, incoming_from_message  # noqa: E402
from app.models import Comment, Network, Source  # noqa: E402
from app.security import validate_init_data  # noqa: E402
from app.services.rubrics import get_rubric_set  # noqa: E402
from app.services.search import build_tsquery  # noqa: E402

OK = "ok"


def check_title(title: str) -> None:
    print(f"\n== {title}")


def main() -> int:
    failures = 0

    def expect(condition: bool, label: str) -> None:
        nonlocal failures
        print(f"  [{'ok' if condition else 'FAIL'}] {label}")
        if not condition:
            failures += 1

    check_title("tsquery")
    query = build_tsquery("блокировка кремль")
    print("   ", query)
    expect(query == "'блокировка':* & 'кремль':*", "префиксы и объединение по AND")
    expect(build_tsquery("   ") is None, "пустой запрос → None")

    check_title("рубрики")
    rubric = get_rubric_set("default.json")
    cases = {
        "Правильно сделали, что взорвали мост, оправдываю теракт": "TERROR",
        "Все на блокировку военкоматов, срываем мобилизацию": "UNREST",
        "Купил новый телефон, посоветуйте приложение": None,
        "Ты дурак и идиот, хватит писать": "INSULT",
    }
    for text, expected in cases.items():
        hits = rubric.match(text)
        top = hits[0].code if hits else None
        print(f"    {top or '—':10} ← {text[:52]}")
        expect(top == expected, f"ожидалось {expected or '—'}")

    check_title("initData")
    token = "123456:TEST"
    payload = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF",
        "user": json.dumps({"id": 42, "first_name": "Иван", "username": "ivan"}, ensure_ascii=False),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    payload["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    try:
        user = validate_init_data(urlencode(payload), token)
        expect(user.id == 42, "подпись и разбор пользователя")
    except Exception as exc:  # noqa: BLE001
        expect(False, f"валидная подпись отклонена: {exc}")

    payload["hash"] = "0" * 64
    try:
        validate_init_data(urlencode(payload), token)
        expect(False, "поддельная подпись отклонена")
    except Exception:  # noqa: BLE001
        expect(True, "поддельная подпись отклонена")

    check_title("Telegram-режим")
    networks = [item["network"].value for item in capabilities()]
    print(f"    включено: {', '.join(networks)}")
    expect(networks == ["telegram"], "по умолчанию включён только Telegram")

    chat = Chat(id=-1001234567890, type="supergroup", title="Тестовый чат", username="test_chat")
    expect(chat_link(chat) == "https://t.me/test_chat", "ссылка на публичный чат")
    expect(
        chat_link(Chat(id=-1001234567890, type="supergroup")) == "https://t.me/c/1234567890",
        "ссылка на приватный чат",
    )

    now = datetime.now(timezone.utc)
    post = Message(message_id=3, date=now, chat=chat, text="пост канала")
    reply = Message(message_id=7, date=now, chat=chat, text="комментарий", reply_to_message=post)
    item = incoming_from_message(reply)
    expect(item.external_id == "7" and item.parent_external_id == "3", "ответ привязан к посту")
    expect(item.post_text == "пост канала" and item.text == "комментарий", "тексты поста и комментария")

    try:
        _require_enabled(Network.vk)
        expect(False, "ВК отклонён, пока выключен")
    except HTTPException as exc:
        expect("отключён" in str(exc.detail), "ВК отклонён, пока выключен")
    _require_enabled(Network.telegram)
    expect(True, "Telegram разрешён")

    check_title("DDL")
    ddl = str(CreateTable(Comment.__table__).compile(dialect=postgresql.dialect()))
    expect("to_tsvector" in ddl, "вычисляемая колонка search_tsv")
    expect("verdict" in ddl and "risk_score" in ddl, "поля вердикта")
    sources_ddl = str(CreateTable(Source.__table__).compile(dialect=postgresql.dialect()))
    expect("ingest_mode" in sources_ddl, "режим наполнения источника")

    print(f"\nИтого: {'всё ок' if failures == 0 else f'{failures} провал(ов)'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
