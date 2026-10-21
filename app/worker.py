"""Worker-сервис.

Делает две вещи:
  1) Telegram-бот (long polling) — принимает комментарии из групп
     обсуждения, где бот состоит (комменты под постами ТВОЕГО канала),
     и сохраняет их в ленту. Плюс отвечает на /start и /id.
  2) Планировщик — периодически опрашивает ВК по ключевым словам
     продукта (brand monitoring).

Оба механизма защищены: нет токена — соответствующая часть просто
логирует это и не падает, чтобы сервис поднимался даже до настройки.
"""
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import config, db
from .collectors import vk

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("worker")


def _msg_link(chat, message_id: int) -> str | None:
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    chat_id = getattr(chat, "id", None)
    if chat_id is not None and str(chat_id).startswith("-100"):
        internal = str(chat_id)[4:]
        return f"https://t.me/c/{internal}/{message_id}"
    return None


async def run_bot() -> None:
    if not config.BOT_TOKEN:
        log.info("BOT_TOKEN не задан — Telegram-бот не запускается (жду токен)")
        return

    from aiogram import Bot, Dispatcher, F
    from aiogram.filters import Command, CommandStart
    from aiogram.types import Message

    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def on_start(message: Message):
        await message.answer(
            "Привет! Я собираю комментарии о продукте и под твоими постами "
            "в одну ленту. Открой мини-приложение, чтобы её посмотреть.\n\n"
            "Команда /id покажет твой Telegram id (его можно вписать в "
            "OWNER_IDS, чтобы ленту видел только ты)."
        )

    @dp.message(Command("id"))
    async def on_id(message: Message):
        await message.answer(f"Твой id: {message.from_user.id}")

    @dp.message(F.chat.type.in_({"group", "supergroup"}))
    async def on_group_message(message: Message):
        # Пропускаем автопересланный в обсуждение пост самого канала и
        # служебные сообщения — храним только реальные комментарии людей.
        if getattr(message, "is_automatic_forward", False):
            return
        if message.from_user is None or message.from_user.is_bot:
            return
        text = message.text or message.caption
        if not text:
            return
        author = message.from_user.username or message.from_user.full_name
        ext = f"{message.chat.id}_{message.message_id}"
        try:
            await db.insert_comment(
                source="telegram",
                external_id=ext,
                author=author,
                text=text,
                url=_msg_link(message.chat, message.message_id),
                is_own_page=True,
                created_at=message.date,
            )
        except Exception as e:
            log.warning("Не удалось сохранить коммент TG %s: %s", ext, e)

    log.info("Telegram-бот запущен (long polling)")
    await dp.start_polling(bot)


async def run_scheduler() -> None:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        vk.poll_once,
        "interval",
        seconds=config.POLL_INTERVAL,
        id="vk_poll",
        next_run_time=None,
    )
    scheduler.start()
    log.info("Планировщик ВК запущен, интервал %d с", config.POLL_INTERVAL)
    # Один прогон сразу на старте (если токен есть).
    try:
        await vk.poll_once()
    except Exception as e:
        log.warning("Первый прогон ВК не удался: %s", e)
    # Держим корутину живой.
    while True:
        await asyncio.sleep(3600)


async def main() -> None:
    await db.init_db()
    log.info("БД готова")
    await asyncio.gather(run_bot(), run_scheduler())


if __name__ == "__main__":
    asyncio.run(main())
