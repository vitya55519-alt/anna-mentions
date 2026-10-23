"""Вход аккаунта-сборщика по телефону прямо из админки.

Логин идёт в процессе web: отправляем код, принимаем код и пароль 2FA,
сохраняем строку сессии в таблицу tg_accounts. Воркер подхватывает её
из базы при следующем обновлении пула — перезапуск не нужен.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from telethon import TelegramClient
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import TgAccount

logger = logging.getLogger(__name__)

# Незавершённые входы: живы 10 минут, потом клиент закрывается.
PENDING_TTL_SECONDS = 600


@dataclass(slots=True)
class PendingLogin:
    phone: str
    client: TelegramClient
    started_at: float = field(default_factory=time.monotonic)


_PENDING: dict[str, PendingLogin] = {}


def normalize_phone(raw: str) -> str:
    """8..., 9... и разный мусор превращает в +7XXXXXXXXXX; остальное — с плюсом."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits


def _drop_stale() -> None:
    now = time.monotonic()
    for login_id, pending in list(_PENDING.items()):
        if now - pending.started_at > PENDING_TTL_SECONDS:
            _PENDING.pop(login_id, None)
            try:
                pending.client.loop.create_task(pending.client.disconnect())
            except Exception:  # noqa: BLE001 — клиент уже мог закрыться
                pass


async def start_login(settings: Settings, raw_phone: str) -> dict:
    """Отправляет код на номер. Возвращает login_id для следующих шагов."""
    if not settings.tg_api_id or not settings.tg_api_hash:
        raise RuntimeError(
            "На сервере не заданы TG_API_ID и TG_API_HASH — без них код не отправить"
        )

    phone = normalize_phone(raw_phone)
    if not phone or len(phone) < 8:
        raise ValueError("Введите номер телефона, например +79181234567")

    _drop_stale()

    client = TelegramClient(
        StringSession(),
        settings.tg_api_id,
        settings.tg_api_hash,
        device_model="CommentRadar",
        system_version="1.0",
        app_version="1.0",
    )
    await client.connect()
    try:
        await client.send_code_request(phone)
    except PhoneNumberInvalidError as exc:
        await client.disconnect()
        raise ValueError("Telegram считает номер неверным — проверьте формат") from exc
    except Exception:
        await client.disconnect()
        raise

    login_id = uuid.uuid4().hex
    _PENDING[login_id] = PendingLogin(phone=phone, client=client)
    logger.info("Админка: отправлен код на %s", phone)
    return {"login_id": login_id, "phone": phone}


async def _finalize(pending: PendingLogin) -> TgAccount:
    me = await pending.client.get_me()
    session_string = pending.client.session.save()
    await pending.client.disconnect()

    username = getattr(me, "username", None)
    first_name = getattr(me, "first_name", None)
    async with SessionLocal() as session:
        account = (
            await session.execute(select(TgAccount).where(TgAccount.phone == pending.phone))
        ).scalar_one_or_none()
        if account is None:
            account = TgAccount(phone=pending.phone, session=session_string)
            session.add(account)
        else:
            # Повторный вход по тому же номеру обновляет сессию.
            account.session = session_string
            account.is_active = True
            account.last_error = None
        account.tg_id = int(getattr(me, "id", 0) or 0) or None
        account.username = username
        account.first_name = first_name
        await session.commit()
        await session.refresh(account)
        logger.info(
            "Админка: аккаунт %s (@%s) сохранён под #%s",
            account.phone,
            username or "",
            account.id,
        )
        return account


async def submit_code(login_id: str, code: str) -> dict:
    """Принимает код из Telegram. Просит пароль, если включена двухэтапная проверка."""
    _drop_stale()
    pending = _PENDING.get(login_id)
    if pending is None:
        raise ValueError("Вход устарел — начните заново")

    digits = re.sub(r"\D", "", code or "")
    if not digits:
        raise ValueError("Введите код из Telegram")

    try:
        await pending.client.sign_in(phone=pending.phone, code=digits)
    except SessionPasswordNeededError:
        # Двухэтапная проверка включена: ждём пароль тем же login_id.
        return {"status": "password", "account": None}
    except PhoneCodeInvalidError as exc:
        raise ValueError("Неверный код — проверьте и введите ещё раз") from exc
    except PhoneCodeExpiredError as exc:
        _PENDING.pop(login_id, None)
        await pending.client.disconnect()
        raise ValueError("Код устарел — запросите новый") from exc
    except Exception:
        raise

    account = await _finalize(pending)
    _PENDING.pop(login_id, None)
    return {"status": "done", "account": account}


async def submit_password(login_id: str, password: str) -> dict:
    """Принимает облачный пароль (двухэтапную проверку)."""
    _drop_stale()
    pending = _PENDING.get(login_id)
    if pending is None:
        raise ValueError("Вход устарел — начните заново")

    if not password:
        raise ValueError("Введите пароль двухэтапной проверки")

    try:
        await pending.client.sign_in(password=password)
    except PasswordHashInvalidError as exc:
        raise ValueError("Неверный пароль — попробуйте ещё раз") from exc
    except Exception:
        raise

    account = await _finalize(pending)
    _PENDING.pop(login_id, None)
    return {"status": "done", "account": account}


def cancel_login(login_id: str) -> None:
    pending = _PENDING.pop(login_id, None)
    if pending is not None:
        try:
            pending.client.loop.create_task(pending.client.disconnect())
        except Exception:  # noqa: BLE001
            pass
