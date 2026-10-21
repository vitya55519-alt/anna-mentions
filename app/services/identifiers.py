"""Поиск по списку идентификаторов: @username, числовой id, номер телефона.

Тип выбирать не нужно: каждая строка разбирается сама, а запрос проверяет все
правдоподобные варианты — ссылку на профиль, имя автора, Telegram id и цифры
в тексте сообщения.

Про телефон: Bot API не отдаёт чужие номера, поэтому номер ищется там, где он
написан — в тексте сообщения или в имени автора. Цифры сравниваются без учёта
пробелов, скобок и дефисов, по последним 10 цифрам.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Comment, Post, Source
from app.services.present import to_item

MAX_IDENTIFIERS = 100
PER_IDENTIFIER = 5

USERNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{3,31}$")
TME_RE = re.compile(r"^(?:https?://)?(?:www\.)?t\.me/(?:s/)?([a-zA-Z0-9_]{4,32})/?$", re.IGNORECASE)
ID_PREFIX_RE = re.compile(r"^(?:id|user|tg)[:\s]*(\d{4,16})$", re.IGNORECASE)
PHONE_CHARS_RE = re.compile(r"[+\-()\u00a0]")
PHONE_ONLY_RE = re.compile(r"^\+?[\d \u00a0()\-]+$")
PHONE_DIGITS_RE = re.compile(r"[^0-9]")
# «id 123456789» с пробелом — одна запись, а не две.
PREFIX_JOIN_RE = re.compile(r"\b(id|user|tg)[:\s]+(\d{4,16})\b", re.IGNORECASE)
# Что может быть разделителем между записями в одной строке.
TOKEN_SPLIT_RE = re.compile(r"[\s,;]+")


@dataclass(frozen=True, slots=True)
class Identifier:
    raw: str
    kind: str  # username | id | phone | unknown
    value: str

    @property
    def digits(self) -> str:
        return re.sub(r"\D", "", self.value)


class ParsedIdentifiers(list[Identifier]):
    """Разобранный список; dropped — сколько записей не влезло в лимит."""

    dropped: int = 0


def parse_identifier(raw: str) -> Identifier | None:
    text = (raw or "").strip().strip("\"'").strip()
    if not text:
        return None

    link = TME_RE.match(text)
    if link:
        return Identifier(raw=text, kind="username", value=link.group(1).lower())

    if text.startswith("@"):
        name = text[1:].strip().lower()
        if USERNAME_RE.match(name):
            return Identifier(raw=text, kind="username", value=name)
        return Identifier(raw=text, kind="unknown", value=text)

    prefixed = ID_PREFIX_RE.match(text.replace(" ", ""))
    if prefixed:
        return Identifier(raw=text, kind="id", value=prefixed.group(1))

    # Ник без собаки: "ivan_petrov" из колонки таблицы тоже принимаем.
    if USERNAME_RE.match(text):
        return Identifier(raw=text, kind="username", value=text.lower())

    digits = re.sub(r"\D", "", text)
    if not digits:
        return Identifier(raw=text, kind="unknown", value=text)

    # Телефон — от 10 цифр с разделителями, с 11 цифрами или с девятки.
    # Короткие числа — id: Telegram id сейчас девяти- и десятизначные.
    looks_phone = len(digits) >= 10 and (
        bool(PHONE_CHARS_RE.search(text))
        or len(digits) == 11
        or digits.startswith("9")
    )
    if looks_phone:
        return Identifier(raw=text, kind="phone", value=digits)
    if 4 <= len(digits) <= 16:
        return Identifier(raw=text, kind="id", value=digits)
    return Identifier(raw=text, kind="unknown", value=text)


def _split_values(text: str) -> list[str]:
    """Режет строку на записи, не ломая телефоны с пробелами и скобками.

    «+7 999 123-45-67» и «8 999 123 45 67» остаются одной записью, а
    «123456789 987654321» — две: короткие числовые куски склеиваются,
    только если вместе дают телефонную длину.
    """
    values: list[str] = []
    buffer: list[str] = []
    text = PREFIX_JOIN_RE.sub(r"\1\2", text or "")

    def digits_of(token: str) -> int:
        return len(PHONE_DIGITS_RE.sub("", token))

    def flush() -> None:
        if not buffer:
            return
        if sum(digits_of(token) for token in buffer) >= 10:
            values.append(" ".join(buffer))
        else:
            values.extend(buffer)
        buffer.clear()

    for token in TOKEN_SPLIT_RE.split(text):
        if not token:
            continue
        number = PHONE_ONLY_RE.match(token) is not None
        size = digits_of(token) if number else 0
        # Короткий числовой кусок может быть частью одного телефона.
        if number and size <= 7:
            if sum(digits_of(part) for part in buffer) + size > 15:
                flush()
            buffer.append(token)
            continue
        flush()
        values.append(token)
    flush()
    return values


def parse_phone(raw: str) -> Identifier | None:
    """Телефон целиком из цифр и разделителей — для обычного поиска по строке."""
    text = (raw or "").strip()
    if not text or not PHONE_ONLY_RE.match(text):
        return None
    identifier = parse_identifier(text)
    return identifier if identifier is not None and identifier.kind == "phone" else None


def parse_identifiers(values: list[str]) -> ParsedIdentifiers:
    """Разворачивает строки списка, убирает повторы и пустые значения."""
    seen: set[tuple[str, str]] = set()
    result = ParsedIdentifiers()
    for value in values:
        for chunk in _split_values(value or ""):
            identifier = parse_identifier(chunk)
            if identifier is None:
                continue
            key = (identifier.kind, identifier.value)
            if key in seen:
                continue
            seen.add(key)
            if len(result) >= MAX_IDENTIFIERS:
                result.dropped += 1
                continue
            result.append(identifier)
    return result


def identifier_condition(identifier: Identifier):
    """SQL-условие: одно и то же имя, id или телефон в любом написании."""
    if identifier.kind == "unknown":
        return None

    if identifier.kind == "username":
        name = identifier.value
        url = func.lower(func.coalesce(Comment.author_url, ""))
        author = func.lower(func.coalesce(Comment.author_name, ""))
        return or_(
            url.endswith("/" + name, autoescape=True),
            url.endswith("/" + name + "/", autoescape=True),
            url.contains("domain=" + name, autoescape=True),
            author == name,
        )

    digits = identifier.digits
    if not digits:
        return None

    conditions = [Comment.author_external_id == digits]
    if len(digits) >= 10:
        tail = digits[-10:]
        text_digits = func.regexp_replace(
            func.coalesce(Comment.text, ""), PHONE_DIGITS_RE.pattern, "", "g"
        )
        name_digits = func.regexp_replace(
            func.coalesce(Comment.author_name, ""), PHONE_DIGITS_RE.pattern, "", "g"
        )
        conditions.append(text_digits.like(f"%{tail}%"))
        conditions.append(name_digits.like(f"%{tail}%"))
    return or_(*conditions)


async def lookup_identifiers(
    session: AsyncSession,
    user_id: int,
    values: list[str],
    per_identifier: int = PER_IDENTIFIER,
) -> dict:
    """Для каждого значения — сколько сообщений нашлось и первые несколько штук."""
    started = time.perf_counter()
    parsed = parse_identifiers(values)

    # Список бывает длинный (до 100 записей): на каждую даём меньше карточек,
    # чтобы ответ не раздулся, — общее число совпадений всё равно видно.
    if len(parsed) > 50:
        per_identifier = min(per_identifier, 2)
    elif len(parsed) > 20:
        per_identifier = min(per_identifier, 3)

    groups: list[dict] = []
    unknown: list[str] = []
    total = 0

    for identifier in parsed:
        if identifier.kind == "unknown":
            unknown.append(identifier.raw)
            continue

        condition = identifier_condition(identifier)
        if condition is None:
            unknown.append(identifier.raw)
            continue

        # Один запрос на запись: count(*) OVER () считает все совпадения,
        # limit оставляет только первые карточки.
        rows = (
            await session.execute(
                select(Comment, Source, Post, func.count().over().label("total"))
                .join(Post, Comment.post_id == Post.id)
                .join(Source, Comment.source_id == Source.id)
                .where(and_(Source.user_id == user_id, condition))
                .order_by(Comment.published_at.desc())
                .limit(per_identifier)
            )
        ).all()

        count = int(rows[0][3]) if rows else 0
        items = [to_item(comment, source, post, comment.text) for comment, source, post, _ in rows]

        total += count
        groups.append(
            {
                "raw": identifier.raw,
                "kind": identifier.kind,
                "value": identifier.value,
                "total": count,
                "items": items,
            }
        )

    return {
        "groups": groups,
        "unknown": unknown,
        "total": total,
        "dropped": parsed.dropped,
        "took_ms": int((time.perf_counter() - started) * 1000),
    }
