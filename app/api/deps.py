from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import User
from app.security import TelegramAuthError, validate_init_data
from app.services.search import ensure_user

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _extract_init_data(value: str | None) -> str:
    if not value:
        return ""
    parts = value.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() in {"tma", "bearer"}:
        return parts[1].strip()
    return value.strip()


async def get_current_user(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
    x_dev_telegram_id: Annotated[int | None, Header()] = None,
) -> User:
    settings = get_settings()

    if settings.dev_mode:
        telegram_id = x_dev_telegram_id or settings.dev_telegram_id
        return await ensure_user(session, telegram_id, first_name="Dev", username="dev_mode")

    init_data = _extract_init_data(authorization)
    try:
        tg_user = validate_init_data(init_data, settings.bot_token)
    except TelegramAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"X-Auth-Reason": "telegram-init-data"},
        ) from exc

    user = await ensure_user(
        session,
        tg_user.id,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
        username=tg_user.username,
        language_code=tg_user.language_code,
    )
    if user.is_blocked:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Доступ к приложению закрыт")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
