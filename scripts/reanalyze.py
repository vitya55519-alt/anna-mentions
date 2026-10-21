"""Переразметить сохранённые сообщения текущим набором статей УК РФ.

Нужно после смены набора: строки, размеченные старым `rubrics/default.json`,
сохраняют коды вроде TERROR и INSULT, пока их не прогонят заново.

Запуск:
    python scripts/reanalyze.py             # только старые коды и непроверенные строки
    python scripts/reanalyze.py --all       # все сообщения
    python scripts/reanalyze.py --no-llm    # только правила, без обращения к LLM
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import or_, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, dispose_db, init_db  # noqa: E402
from app.models import Comment  # noqa: E402
from app.services.analyze import analyze_comments  # noqa: E402
from app.services.rubrics import VERDICT_PENDING, get_rubric_set  # noqa: E402

BATCH = 200


async def run(all_rows: bool, use_llm: bool, limit: int | None) -> int:
    settings = get_settings()
    if not use_llm:
        settings = settings.model_copy(update={"llm_base_url": ""})
    codes = tuple(get_rubric_set(settings.rubrics_file).codes)

    await init_db()
    async with SessionLocal() as session:
        stmt = select(Comment.id).order_by(Comment.id)
        if not all_rows:
            stmt = stmt.where(
                or_(
                    Comment.verdict == VERDICT_PENDING,
                    Comment.rubric.is_(None),
                    Comment.rubric.notin_(codes),
                )
            )
        ids = list((await session.execute(stmt)).scalars())
        if limit:
            ids = ids[:limit]

        print(f"Набор: {settings.rubrics_file} ({len(codes)} статей)")
        print(f"К переразметке: {len(ids)}")

        checked = 0
        flagged = 0
        for start in range(0, len(ids), BATCH):
            chunk = ids[start : start + BATCH]
            comments = list(
                (await session.execute(select(Comment).where(Comment.id.in_(chunk)))).scalars()
            )
            stats = await analyze_comments(session, comments, settings)
            await session.commit()
            checked += stats["checked"]
            flagged += stats["flagged"]
            print(f"  {start + len(chunk)}/{len(ids)}: проверено {checked}, с отметкой {flagged}")

    await dispose_db()
    print(f"Готово: проверено {checked}, с отметкой {flagged}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Переразметка сообщений текущим набором статей")
    parser.add_argument(
        "--all",
        action="store_true",
        help="перепроверить все сообщения, а не только строки со старыми кодами",
    )
    parser.add_argument("--no-llm", action="store_true", help="только правила, без LLM")
    parser.add_argument("--limit", type=int, default=0, help="сколько сообщений взять (0 — все)")
    args = parser.parse_args()
    return asyncio.run(run(args.all, not args.no_llm, args.limit or None))


if __name__ == "__main__":
    raise SystemExit(main())
