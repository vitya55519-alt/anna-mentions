from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Comment
from app.services.llm import LLMClient
from app.services.rubrics import (
    VERDICT_CLEAN,
    CategoryHit,
    RubricSet,
    get_rubric_set,
    rule_rationale,
    verdict_for,
)

logger = logging.getLogger(__name__)

MAX_LLM_PER_BATCH = 8
LLM_CONCURRENCY = 3
VALID_VERDICTS = {"likely", "borderline", "clean"}

SYSTEM_PROMPT = """Ты — аналитик-модератор комментариев. Тебе дают текст одного комментария
и список рубрик. Определи, попадает ли комментарий в одну из рубрик.

Правила:
- Оценивай только текст комментария, не додумывай контекст.
- Нужен прямой признак в тексте: призыв, оправдание, утверждение, оскорбление.
- Ирония, цитата, пересказ чужой позиции или новость без оценки — не попадание.
- Если ни одна рубрика не подходит, верни code: null и verdict: "clean".
- Ответ — только JSON без пояснений и markdown.

Формат ответа:
{"code": "КОД_РУБРИКИ" | null, "confidence": 0.0-1.0, "verdict": "likely" | "borderline" | "clean", "reason": "1-2 предложения по-русски: почему именно эта рубрика или почему не попадает"}"""


def _clamp(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return round(min(1.0, max(0.0, number)), 2)


def _llm_user_prompt(rubric: RubricSet, text: str, hits: list[CategoryHit]) -> str:
    categories = "\n".join(
        f"- {category.code}: {category.title}. {category.description}" for category in rubric.categories
    )
    markers = "; ".join(f"{hit.code}: {', '.join(hit.markers[:3])}" for hit in hits)
    return (
        f"Рубрики:\n{categories}\n\n"
        f"Маркеры, которые уже нашли правила: {markers or 'нет'}\n\n"
        f"Комментарий:\n<<<\n{text[:1500]}\n>>>"
    )


def _apply_rule_result(comment: Comment, hits: list[CategoryHit], now: datetime) -> None:
    top = hits[0]
    comment.rubric = top.code
    comment.risk_score = top.score
    comment.verdict = verdict_for(top.score)
    comment.rationale = rule_rationale(top)
    comment.analyzed_at = now
    comment.analysis = {
        "engine": "rules",
        "hits": [
            {"code": hit.code, "title": hit.title, "score": hit.score, "markers": list(hit.markers)}
            for hit in hits
        ],
    }


async def _refine_with_llm(
    client: LLMClient,
    rubric: RubricSet,
    comment: Comment,
    hits: list[CategoryHit],
    limit: asyncio.Semaphore,
) -> None:
    async with limit:
        payload = await client.complete_json(SYSTEM_PROMPT, _llm_user_prompt(rubric, comment.text, hits))
    if not payload:
        return

    code = str(payload.get("code") or "").upper() or None
    verdict = str(payload.get("verdict") or "").lower()
    confidence = _clamp(payload.get("confidence"))
    reason = str(payload.get("reason") or "").strip()

    if code and rubric.category(code) is None:
        code = hits[0].code
    if verdict not in VALID_VERDICTS:
        verdict = verdict_for(confidence)

    comment.analysis = {**(comment.analysis or {}), "engine": "rules+llm", "llm": payload}

    if verdict == VERDICT_CLEAN:
        comment.rubric = None
        comment.verdict = VERDICT_CLEAN
        comment.risk_score = round((1 - confidence) * 0.4, 2)
        comment.rationale = reason or "LLM не нашёл признаков ни одной рубрики"
        return

    comment.rubric = code or hits[0].code
    comment.verdict = verdict
    comment.risk_score = confidence or hits[0].score
    comment.rationale = reason or rule_rationale(hits[0])


async def analyze_comments(
    session: AsyncSession,
    comments: list[Comment],
    settings: Settings | None = None,
) -> dict[str, int]:
    """Проставляет вердикт, рубрику, оценку и объяснение. Первый проход — правила, второй — LLM."""
    settings = settings or get_settings()
    rubric = get_rubric_set(settings.rubrics_file)
    now = datetime.now(timezone.utc)

    pending: list[tuple[Comment, list[CategoryHit]]] = []
    for comment in comments:
        hits = rubric.match(comment.text)
        if not hits:
            comment.verdict = VERDICT_CLEAN
            comment.risk_score = 0.0
            comment.rubric = None
            comment.rationale = "ни один маркер рубрик не сработал"
            comment.analyzed_at = now
            comment.analysis = {"engine": "rules", "hits": []}
            continue
        _apply_rule_result(comment, hits, now)
        pending.append((comment, hits))

    client = LLMClient(settings) if settings.llm_ready and pending else None
    if client:
        limit = asyncio.Semaphore(LLM_CONCURRENCY)
        batch = [pair for pair in pending if pair[0].risk_score >= settings.llm_min_score]
        await asyncio.gather(
            *(
                _refine_with_llm(client, rubric, comment, hits, limit)
                for comment, hits in batch[:MAX_LLM_PER_BATCH]
            )
        )

    await session.flush()

    return {
        "checked": len(comments),
        "flagged": sum(1 for comment in comments if comment.verdict != VERDICT_CLEAN),
    }
