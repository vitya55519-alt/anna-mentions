from __future__ import annotations

import csv
import io
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Comment, Post, Source, User
from app.services.present import verdict_label

TS_CONFIG = literal_column("'russian'")
HEADLINE_OPTIONS = literal_column(
    "'StartSel=<mark>, StopSel=</mark>, MaxWords=42, MinWords=12, ShortWord=3, MaxFragments=2'"
)

TOKEN_RE = re.compile(r'"([^"]*)"|(\S+)')
WORD_RE = re.compile(r"[^\w#@+-]+", re.UNICODE)


@dataclass(slots=True)
class SearchParams:
    query: str = ""
    network: str | None = None
    source_id: int | None = None
    rubric: str | None = None
    verdict: str | None = None
    min_score: float = 0.0
    date_from: datetime | None = None
    date_to: datetime | None = None
    sort: str = "date"
    limit: int = 30
    offset: int = 0


def _escape_token(token: str) -> str:
    cleaned = WORD_RE.sub("", token).strip()
    if not cleaned:
        return ""
    return "'" + cleaned.replace("\\", "\\\\") + "'"


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_tsquery(raw: str) -> str | None:
    """«блокир* кремль» → 'блокир':* & 'кремль':* — префиксы ловят русские окончания."""
    text = (raw or "").strip()
    if not text or text.startswith("@"):
        return None  # @ник ищем по автору, полнотекстовый индекс тут не нужен

    parts: list[str] = []
    for phrase, word in TOKEN_RE.findall(text):
        if phrase:
            items = [item for item in (_escape_token(t) for t in phrase.split()) if item]
            if len(items) > 1:
                parts.append(" & ".join(items))
            elif items:
                parts.append(items[0] + ":*")
        elif word:
            token = _escape_token(word)
            if token:
                parts.append(token + ":*")
    return " & ".join(parts) if parts else None


def _base_conditions(user_id: int, params: SearchParams, skip: frozenset[str] = frozenset()):
    conditions = [Source.user_id == user_id]
    if params.source_id and "source" not in skip:
        conditions.append(Comment.source_id == params.source_id)
    if params.network and "network" not in skip:
        conditions.append(Comment.network == params.network)
    if params.rubric and "rubric" not in skip:
        conditions.append(Comment.rubric == params.rubric.upper())
    if params.verdict and "verdict" not in skip:
        conditions.append(Comment.verdict == params.verdict)
    if params.min_score and "score" not in skip:
        conditions.append(Comment.risk_score >= params.min_score)
    if params.date_from and "dates" not in skip:
        conditions.append(Comment.published_at >= params.date_from)
    if params.date_to and "dates" not in skip:
        conditions.append(Comment.published_at <= params.date_to)
    return conditions


def _text_condition(raw: str, tsquery: str | None):
    if not raw:
        return None

    text = raw.strip()
    if text.startswith("@"):
        handle = _escape_like(text.lstrip("@"))
        if not handle:
            return None
        like = f"%{handle}%"
        return or_(
            Comment.author_url.ilike(like, escape="\\"),
            Comment.author_name.ilike(like, escape="\\"),
        )

    escaped = _escape_like(text)
    conditions = [
        Comment.text.ilike(f"%{escaped}%", escape="\\"),
        Comment.author_name.ilike(f"%{escaped}%", escape="\\"),
    ]
    if tsquery:
        conditions.append(Comment.search_tsv.op("@@")(func.to_tsquery(TS_CONFIG, tsquery)))
    return or_(*conditions)


def _count_stmt(conditions):
    return (
        select(func.count())
        .select_from(Comment)
        .join(Post, Comment.post_id == Post.id)
        .join(Source, Comment.source_id == Source.id)
        .where(and_(*conditions))
    )


async def _count(session: AsyncSession, conditions) -> int:
    return int((await session.execute(_count_stmt(conditions))).scalar_one())


async def search_comments(
    session: AsyncSession,
    user_id: int,
    params: SearchParams,
) -> dict:
    started = time.perf_counter()
    tsquery = build_tsquery(params.query)
    text_condition = _text_condition(params.query, tsquery)

    base = _base_conditions(user_id, params)
    full = list(base) + ([text_condition] if text_condition is not None else [])
    matched = await _count(session, full)
    checked = await _count(session, base) if params.query else matched

    tsq = func.to_tsquery(TS_CONFIG, tsquery) if tsquery else None
    snippet = (
        func.ts_headline(TS_CONFIG, Comment.text, tsq, HEADLINE_OPTIONS).label("snippet")
        if tsq is not None
        else Comment.text.label("snippet")
    )

    stmt = (
        select(Comment, Source, Post, snippet)
        .join(Post, Comment.post_id == Post.id)
        .join(Source, Comment.source_id == Source.id)
        .where(and_(*full))
    )
    if tsq is not None and params.sort == "relevance":
        stmt = stmt.order_by(func.ts_rank(Comment.search_tsv, tsq).desc(), Comment.published_at.desc())
    elif params.sort == "score":
        stmt = stmt.order_by(Comment.risk_score.desc(), Comment.published_at.desc())
    else:
        stmt = stmt.order_by(Comment.published_at.desc())

    rows = (await session.execute(stmt.limit(params.limit).offset(params.offset))).all()

    facet_conditions = (
        _base_conditions(user_id, params, frozenset({"network"}))
        + ([text_condition] if text_condition is not None else [])
    )
    facet_stmt = (
        select(Comment.network, func.count())
        .join(Source, Comment.source_id == Source.id)
        .where(and_(*facet_conditions))
        .group_by(Comment.network)
    )
    facet_rows = (await session.execute(facet_stmt)).all()

    rubric_conditions = (
        _base_conditions(user_id, params, frozenset({"rubric"}))
        + ([text_condition] if text_condition is not None else [])
    )
    rubric_stmt = (
        select(Comment.rubric, func.count())
        .join(Source, Comment.source_id == Source.id)
        .where(and_(*rubric_conditions))
        .where(Comment.rubric.is_not(None))
        .group_by(Comment.rubric)
    )
    rubric_rows = (await session.execute(rubric_stmt)).all()

    items = [_to_item(comment, source, post, text) for comment, source, post, text in rows]

    return {
        "items": items,
        "total": matched,
        "checked": checked,
        "matched": matched,
        "limit": params.limit,
        "offset": params.offset,
        "took_ms": int((time.perf_counter() - started) * 1000),
        "facets": {str(network): int(count) for network, count in facet_rows},
        "rubrics": {str(code): int(count) for code, count in rubric_rows},
        "query": params.query,
    }


def _to_item(comment: Comment, source: Source, post: Post, snippet: str) -> dict:
    return {
        "id": comment.id,
        "network": comment.network,
        "text": comment.text,
        "snippet": snippet or comment.text,
        "author_name": comment.author_name,
        "author_url": comment.author_url,
        "published_at": comment.published_at,
        "likes": comment.likes,
        "url": comment.url,
        "parent_external_id": comment.parent_external_id,
        "verdict": comment.verdict,
        "rubric": comment.rubric,
        "rationale": comment.rationale,
        "score": comment.risk_score,
        "source": {
            "id": source.id,
            "title": source.title,
            "handle": source.handle,
            "network": source.network,
            "avatar_url": source.avatar_url,
        },
        "post": {
            "id": post.id,
            "text": post.text,
            "url": post.url,
            "published_at": post.published_at,
        },
    }


async def export_csv(session: AsyncSession, user_id: int, params: SearchParams) -> str:
    params = replace(params, limit=min(5000, max(1, params.limit)), offset=0)
    result = await search_comments(session, user_id, params)

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(
        ["Дата", "Сеть", "Источник", "Автор", "Текст", "Рубрика", "Вердикт", "Оценка", "Почему", "Ссылка"]
    )
    for item in result["items"]:
        writer.writerow(
            [
                item["published_at"].strftime("%d.%m.%Y %H:%M") if item["published_at"] else "",
                item["network"].title,
                item["source"]["title"],
                item["author_name"] or "",
                item["text"],
                item["rubric"] or "",
                verdict_label(item["verdict"]),
                f"{item['score']:.2f}",
                item["rationale"] or "",
                item["url"] or item["post"]["url"],
            ]
        )
    return "\ufeff" + buffer.getvalue()


async def build_stats(session: AsyncSession, user_id: int) -> dict:
    source_filter = Source.user_id == user_id

    sources_total = int(
        (await session.execute(select(func.count()).select_from(Source).where(source_filter))).scalar_one()
    )
    sources_active = int(
        (
            await session.execute(
                select(func.count()).select_from(Source).where(source_filter, Source.is_active.is_(True))
            )
        ).scalar_one()
    )
    posts_total = int(
        (
            await session.execute(
                select(func.count()).select_from(Post).join(Source, Post.source_id == Source.id).where(source_filter)
            )
        ).scalar_one()
    )
    comments_total = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Comment)
                .join(Source, Comment.source_id == Source.id)
                .where(source_filter)
            )
        ).scalar_one()
    )
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    last_24h = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Comment)
                .join(Source, Comment.source_id == Source.id)
                .where(source_filter, Comment.created_at >= since)
            )
        ).scalar_one()
    )
    checked_total = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Comment)
                .join(Source, Comment.source_id == Source.id)
                .where(source_filter, Comment.verdict != "pending")
            )
        ).scalar_one()
    )
    flagged_total = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Comment)
                .join(Source, Comment.source_id == Source.id)
                .where(source_filter, Comment.verdict.in_(("likely", "borderline")))
            )
        ).scalar_one()
    )

    by_network = {
        str(network): int(count)
        for network, count in (
            await session.execute(
                select(Comment.network, func.count())
                .join(Source, Comment.source_id == Source.id)
                .where(source_filter)
                .group_by(Comment.network)
            )
        ).all()
    }
    by_rubric = {
        str(code): int(count)
        for code, count in (
            await session.execute(
                select(Comment.rubric, func.count())
                .join(Source, Comment.source_id == Source.id)
                .where(source_filter, Comment.rubric.is_not(None))
                .group_by(Comment.rubric)
            )
        ).all()
    }

    last_crawl_at = (
        await session.execute(select(func.max(Source.last_crawl_at)).where(source_filter))
    ).scalar_one()

    return {
        "sources_total": sources_total,
        "sources_active": sources_active,
        "posts_total": posts_total,
        "comments_total": comments_total,
        "comments_last_24h": last_24h,
        "checked_total": checked_total,
        "flagged_total": flagged_total,
        "by_network": by_network,
        "by_rubric": by_rubric,
        "last_crawl_at": last_crawl_at,
    }


async def ensure_user(session: AsyncSession, telegram_id: int, **profile) -> User:
    user = (
        await session.execute(select(User).where(User.telegram_id == telegram_id))
    ).scalar_one_or_none()
    if user is None:
        user = User(telegram_id=telegram_id, **profile)
        session.add(user)
        await session.commit()
        return user

    user.last_seen_at = datetime.now(timezone.utc)
    for key, value in profile.items():
        if value and getattr(user, key, None) != value:
            setattr(user, key, value)
    await session.commit()
    return user
