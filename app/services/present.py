from __future__ import annotations

import html
from datetime import datetime

from app.models import Comment, Post, Source

VERDICT_LABELS = {
    "likely": "вероятно",
    "borderline": "пограничное",
    "clean": "не попадает",
    "pending": "не проверено",
}


def verdict_label(verdict: str | None) -> str:
    return VERDICT_LABELS.get(verdict or "pending", "не проверено")


def short_date(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y") if value else "—"


def clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def link_label(url: str) -> str:
    return url.replace("https://", "").replace("http://", "")


def comment_card(
    comment: Comment,
    source: Source,
    post: Post | None,
    *,
    limit: int = 420,
) -> str:
    """Карточка находки в стиле «Коммент-сканера»: рубрика, оценка, текст, почему, ссылка."""
    rubric = comment.rubric or "—"
    header = (
        f"<b>{html.escape(rubric)}</b> · {verdict_label(comment.verdict)}"
        f"   <code>{comment.risk_score:.2f}</code> · {short_date(comment.published_at)}"
    )
    quote = f"«{html.escape(clip(comment.text, limit))}»"

    parts = [header, "", quote]
    if comment.rationale:
        parts += ["", f"<blockquote>почему: {html.escape(clip(comment.rationale, 400))}</blockquote>"]

    if comment.url:
        parts += ["", f'<a href="{html.escape(comment.url)}">{html.escape(link_label(comment.url))}</a>']
    else:
        parts += ["", html.escape(source.title)]

    if post and post.text and not comment.url:
        parts += ["", f"<i>{html.escape(clip(post.text, 160))}</i>"]

    return "\n".join(parts)


def card_from_item(item: dict, *, limit: int = 420) -> str:
    """Та же карточка, но из ответа поиска (dict) — для бота и уведомлений."""
    text = (item.get("text") or "").replace("<mark>", "").replace("</mark>", "")
    rationale = (item.get("rationale") or "").replace("<mark>", "").replace("</mark>", "")
    published = item.get("published_at")
    date = published.strftime("%d.%m.%Y") if published else "—"

    header = (
        f"<b>{html.escape(item.get('rubric') or '—')}</b> · {verdict_label(item.get('verdict'))}"
        f"   <code>{float(item.get('score') or 0):.2f}</code> · {date}"
    )

    parts = [header, "", f"«{html.escape(clip(text, limit))}»"]
    if rationale:
        parts += ["", f"<blockquote>почему: {html.escape(clip(rationale, 400))}</blockquote>"]

    url = item.get("url") or (item.get("post") or {}).get("url")
    if url:
        parts += ["", f'<a href="{html.escape(url)}">{html.escape(link_label(url))}</a>']
    return "\n".join(parts)
