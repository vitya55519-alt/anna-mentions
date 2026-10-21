from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.adapters import close_adapters
from app.bot import create_dispatcher, setup_bot
from app.config import get_settings
from app.db import SessionLocal, dispose_db, init_db
from app.services.crawl import claim_due_sources, notify_matches, sync_source

logger = logging.getLogger("app.worker")


async def crawl_cycle(bot: Bot | None, limit: int = 2) -> int:
    async with SessionLocal() as session:
        sources = await claim_due_sources(session, limit=limit)
        processed = 0

        for source in sources:
            result = await sync_source(session, source)
            processed += 1

            logger.info(
                "Источник %s (%s): постов %s, новых комментариев %s, отмечено %s, ошибка %s",
                source.title,
                source.network.value,
                result.run.posts_seen,
                result.run.comments_new,
                result.flagged,
                result.run.error or "нет",
            )

            if result.new_comment_ids and bot is not None:
                sent = await notify_matches(session, source, result.new_comment_ids, bot)
                if sent:
                    logger.info("Отправлено уведомлений: %s", sent)

        return processed


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    await init_db()
    logger.info("Воркер запущен, интервал опроса %s с", settings.worker_poll_seconds)

    bot: Bot | None = None
    polling: asyncio.Task | None = None

    if settings.bot_token:
        bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        await setup_bot(bot)
        polling = asyncio.create_task(
            create_dispatcher().start_polling(bot, handle_signals=False),
            name="bot-polling",
        )
        logger.info("Бот подключён, команды доступны")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        while not stop.is_set():
            try:
                processed = await crawl_cycle(bot)
            except Exception:  # noqa: BLE001 — цикл не должен умирать
                logger.exception("Сбой цикла обхода")
                processed = 0

            if processed:
                continue
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
    finally:
        logger.info("Останавливаюсь")
        if polling is not None:
            polling.cancel()
            with suppress(asyncio.CancelledError):
                await polling
        if bot is not None:
            await bot.session.close()
        await close_adapters()
        await dispose_db()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main())
