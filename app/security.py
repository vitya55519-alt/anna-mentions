from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

INIT_DATA_MAX_AGE = 24 * 60 * 60


class TelegramAuthError(Exception):
    """Подпись initData не сошлась, данные просрочены или отсутствуют."""


@dataclass(slots=True)
class TelegramUser:
    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None


def validate_init_data(init_data: str, bot_token: str) -> TelegramUser:
    """Проверяет подпись initData по алгоритму Telegram WebApp (HMAC-SHA256)."""
    if not init_data:
        raise TelegramAuthError("Пустой initData")
    if not bot_token:
        raise TelegramAuthError("BOT_TOKEN не задан — проверить подпись невозможно")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise TelegramAuthError("В initData нет поля hash")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise TelegramAuthError("Подпись initData неверна")

    auth_date = int(pairs.get("auth_date") or 0)
    if not auth_date or time.time() - auth_date > INIT_DATA_MAX_AGE:
        raise TelegramAuthError("Сессия Mini App устарела, откройте приложение заново")

    try:
        raw_user = json.loads(pairs.get("user") or "{}")
    except json.JSONDecodeError as exc:
        raise TelegramAuthError("Некорректный JSON пользователя") from exc

    if "id" not in raw_user:
        raise TelegramAuthError("В initData нет пользователя")

    return TelegramUser(
        id=int(raw_user["id"]),
        first_name=raw_user.get("first_name"),
        last_name=raw_user.get("last_name"),
        username=raw_user.get("username"),
        language_code=raw_user.get("language_code"),
    )
