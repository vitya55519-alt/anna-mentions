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
from app.bot import chat_keyboard, chat_link, incoming_from_message  # noqa: E402
from app.config import Settings  # noqa: E402
from app.models import Comment, Network, Source  # noqa: E402
from app.security import validate_init_data  # noqa: E402
from app.services.rubrics import get_rubric_set  # noqa: E402
from app.services.search import build_tsquery  # noqa: E402
from app.services.tg_scan import dialog_key  # noqa: E402

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

    check_title("статьи УК РФ")
    rubric = get_rubric_set("uk_rf.json")
    print(f"    статей в наборе: {len(rubric.codes)}")
    expect(len(rubric.codes) >= 9, "набор статей загружен")
    expect("205.2" in rubric.codes and "280.4" in rubric.codes, "коды — номера статей")
    cases = {
        "Правильно сделали, что взорвали мост, оправдываю теракт и поддерживаю исполнителей": "205.2",
        "Российская армия намеренно расстреливает мирных жителей, это задокументированный факт": "207.3",
        "Все на блокировку военкоматов и зданий ФСБ, не даём им работать, срываем мобилизацию": "280.4",
        "Курильские острова надо срочно передать Японии, они исторически их": "280.2",
        "Купил новый телефон, посоветуйте нормальные приложения для работы": None,
    }
    for text, expected in cases.items():
        hits = rubric.match(text)
        top = hits[0].code if hits else None
        print(f"    {top or '—':8} ← {text[:52]}")
        expect(top == expected, f"ожидалось {expected or '—'}")

    hits = rubric.match("Надо уебать дроном по Кремлю, оправдываю такие удары")
    expect(bool(hits), "спорный текст попал в набор")
    expect(hits[0].score < 0.8, "одного маркера хватает только на «пограничное»")

    check_title("tsquery")
    query = build_tsquery("блокировка кремль")
    print("   ", query)
    expect(query == "'блокировка':* & 'кремль':*", "префиксы и объединение по AND")
    expect(build_tsquery("   ") is None, "пустой запрос → None")
    expect(build_tsquery("123456789") is None, "id автора ищется точно, без tsquery")
    expect(build_tsquery("205.2") is None, "номер статьи ищется фильтром, а не словами")
    expect(build_tsquery("280") is None, "старая нумерация без точки тоже не идёт в слова")

    check_title("рубрики по умолчанию")
    expect(get_rubric_set().id == "uk_rf", "по умолчанию берётся набор статей УК РФ")
    expect(get_rubric_set().codes == rubric.codes, "набор из настроек совпадает с набором статей")

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

    check_title("адрес приложения")
    expect(
        Settings(railway_public_domain="demo.up.railway.app").public_webapp_url
        == "https://demo.up.railway.app",
        "адрес собирается из домена Railway",
    )
    expect(
        Settings(webapp_url="https://scan.example.com/").public_webapp_url
        == "https://scan.example.com",
        "лишний слэш в конце не мешает",
    )
    expect(Settings().public_webapp_url == "", "без адреса кнопка не ставится")
    expect(chat_keyboard("group") is None, "в группе кнопку приложения не отправляем")

    check_title("аккаунты-сборщики")
    settings = Settings(tg_api_id=1, tg_api_hash="x", tg_session_1="a", tg_session_3="b")
    expect(settings.tg_sessions == ["a", "b"], "сессии собираются по порядку номеров")
    expect(settings.tg_accounts_ready, "аккаунты готовы при api_id, api_hash и сессии")
    expect(not Settings(tg_session_1="a").tg_accounts_ready, "без api_id сборщик выключен")
    expect(dialog_key(-1001234567890) == "dialog:-1001234567890", "ключ группы в базе")

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
