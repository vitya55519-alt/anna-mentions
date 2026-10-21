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
from app.services.tg_accounts import AccountPool, get_pool
from app.services.tg_scan import discover_dialogs, resolve_owner, scan_source

logger = logging.getLogger("app.worker")


async def crawl_cycle(bot: Bot | None, pool: AccountPool, limit: int = 2) -> int:
    settings = get_settings()
    async with SessionLocal() as session:
        sources = await claim_due_sources(session, limit=limit)
        processed = 0

        for source in sources:
            if source.ingest_mode == "scan":
                result = await scan_source(session, source, settings, pool)
                processed += 1
                logger.info(
                    "Скан «%s» (аккаунт %s): новых %s, отмечено %s, ошибка %s",
                    source.title,
                    (source.cursor or {}).get("account"),
                    len(result.new_comment_ids),
                    result.flagged,
                    result.error or "нет",
                )
                if result.new_comment_ids and bot is not None:
                    await notify_matches(session, source, result.new_comment_ids, bot)
                continue

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


async def discovery_loop(pool: AccountPool, stop: asyncio.Event) -> None:
    """Раз в TG_SCAN_INTERVAL_MINUTES обходит диалоги аккаунтов и добавляет новые группы."""
    settings = get_settings()
    while not stop.is_set():
        wait_minutes = settings.tg_scan_interval_minutes
        try:
            async with SessionLocal() as session:
                owner = await resolve_owner(session, settings)
                if owner is None:
                    # Владельца ещё нет: ждём, пока кто-нибудь откроет бота.
                    wait_minutes = 2
                else:
                    await discover_dialogs(session, settings, pool)
        except Exception:  # noqa: BLE001 — цикл не должен умирать
            logger.exception("Сбой обнаружения групп")
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=max(1, wait_minutes) * 60)


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
    discovery: asyncio.Task | None = None
    pool = get_pool(settings)
    stop = asyncio.Event()

    if settings.tg_scan_enabled and pool.ready():
        logger.info("Аккаунтов-сборщиков: %s", len(pool.accounts))
        discovery = asyncio.create_task(discovery_loop(pool, stop), name="tg-discovery")
    else:
        logger.info("Аккаунты не заданы — сканирование групп выключено")

    if settings.bot_token:
        bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        await setup_bot(bot)
        polling = asyncio.create_task(
            create_dispatcher().start_polling(bot, handle_signals=False),
            name="bot-polling",
        )
        logger.info("Бот подключён, команды доступны")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        while not stop.is_set():
            try:
                processed = await crawl_cycle(bot, pool)
            except Exception:  # noqa: BLE001 — цикл не должен умирать
                logger.exception("Сбой цикла обхода")
                processed = 0

            if processed:
                continue
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
    finally:
        logger.info("Останавливаюсь")
        if discovery is not None:
            discovery.cancel()
            with suppress(asyncio.CancelledError):
                await discovery
        if polling is not None:
            polling.cancel()
            with suppress(asyncio.CancelledError):
                await polling
        await pool.close()
        if bot is not None:
            await bot.session.close()
        await close_adapters()
        await dispose_db()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main())
