from __future__ import annotations

import logging
from html import escape as html_escape
from urllib.parse import urlencode

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MenuButtonWebApp,
    WebAppInfo,
)

from app.config import get_settings
from app.db import SessionLocal
from app.models import Watch
from app.services.ingest import IncomingComment, ensure_chat_source, ingest_telegram_message
from app.services.present import card_from_item
from app.services.search import (
    SearchParams,
    build_stats,
    ensure_user,
    export_csv,
    search_comments,
)
from app.services.tg_accounts import get_pool
from app.services.tg_scan import discover_dialogs, queue_scan

logger = logging.getLogger(__name__)
router = Router()

WELCOME = (
    "<b>Коммент-сканер</b>\n"
    "Собираю сообщения из чатов, куда добавлен бот, и помечаю те, что попадают под рубрики.\n\n"
    "<b>Как подключить чат</b>\n"
    "Добавьте бота в группу или канал. Для групп выключите Privacy Mode в @BotFather, "
    "иначе бот не увидит сообщения.\n\n"
    "<b>Группы ваших аккаунтов</b>\n"
    "/scan — обойти группы и каналы привязанных аккаунтов\n"
    "/acc — сколько аккаунтов и сколько групп в базе\n\n"
    "<b>Команды</b>\n"
    "/find &lt;запрос&gt; — обычный поиск, например <code>/find блокировка</code>\n"
    "/art &lt;код&gt; — находки по рубрике, например <code>/art VIOLENCE</code>\n"
    "/csv &lt;запрос&gt; — выгрузка в таблицу\n"
    "/add &lt;слово&gt; — присылать новые сообщения с этим словом\n"
    "/words — список слов\n"
    "/stats — сколько проверено и сколько найдено\n\n"
    "Полный интерфейс — кнопка ниже."
)


def webapp_url(**params: str) -> str:
    settings = get_settings()
    base = settings.webapp_url or "https://example.com"
    if not params:
        return base
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urlencode({k: v for k, v in params.items() if v})}"


def app_keyboard(**params: str) -> InlineKeyboardMarkup | None:
    settings = get_settings()
    if not settings.webapp_url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть сканер", web_app=WebAppInfo(url=webapp_url(**params)))]
        ]
    )


async def _ensure_user(session, message: Message):
    tg = message.from_user
    if tg is None:
        return None
    return await ensure_user(
        session,
        tg.id,
        first_name=tg.first_name,
        last_name=tg.last_name,
        username=tg.username,
        language_code=tg.language_code,
    )


@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message) -> None:
    await message.answer(WELCOME, reply_markup=app_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(WELCOME, reply_markup=app_keyboard())


@router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject) -> None:
    await _search(message, (command.args or "").strip())


@router.message(Command("art"))
async def cmd_art(message: Message, command: CommandObject) -> None:
    args = (command.args or "").strip().split(maxsplit=1)
    if not args:
        await message.answer("Укажите код рубрики: <code>/art VIOLENCE</code>")
        return
    code = args[0].upper()
    query = args[1] if len(args) > 1 else ""
    await _search(message, query, rubric=code, empty_ok=True)


@router.message(Command("csv"))
async def cmd_csv(message: Message, command: CommandObject) -> None:
    query = (command.args or "").strip()
    async with SessionLocal() as session:
        user = await _ensure_user(session, message)
        if user is None:
            return
        payload = await export_csv(session, user.id, SearchParams(query=query, limit=2000))

    document = BufferedInputFile(payload.encode("utf-8"), filename="comments.csv")
    await message.answer_document(document, caption=f"Выгрузка: {query or 'все комментарии'}")


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject) -> None:
    keyword = (command.args or "").strip()
    if len(keyword) < 2:
        await message.answer("Слишком короткое слово: <code>/add блокировка</code>")
        return

    async with SessionLocal() as session:
        user = await _ensure_user(session, message)
        if user is None:
            return
        exists = [
            watch
            for watch in (
                await session.execute(_watches_of(user.id))
            ).scalars()
            if watch.keyword.casefold() == keyword.casefold()
        ]
        if exists:
            await message.answer("Это слово уже отслеживается")
            return
        session.add(Watch(user_id=user.id, keyword=keyword))
        await session.commit()

    await message.answer(
        f"Готово. Пришлю новые комментарии со словом «{keyword}» после следующего обхода."
    )


@router.message(Command("words"))
async def cmd_words(message: Message) -> None:
    async with SessionLocal() as session:
        user = await _ensure_user(session, message)
        if user is None:
            return
        watches = list((await session.execute(_watches_of(user.id))).scalars())

    if not watches:
        await message.answer("Пока нет ни одного слова. Добавьте: <code>/add блокировка</code>")
        return

    lines = [f"• «{w.keyword}» — срабатываний: {w.hits_count}" for w in watches]
    await message.answer("<b>Отслеживаемые слова</b>\n" + "\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    async with SessionLocal() as session:
        user = await _ensure_user(session, message)
        if user is None:
            return
        stats = await build_stats(session, user.id)

    await message.answer(
        "<b>Сводка</b>\n"
        f"Источников: {stats['sources_active']} из {stats['sources_total']}\n"
        f"Комментариев в базе: {stats['comments_total']}\n"
        f"Проверено: {stats['checked_total']}\n"
        f"С отметкой: {stats['flagged_total']}\n"
        f"Новых за сутки: {stats['comments_last_24h']}"
    )


@router.message(Command("scan"))
async def cmd_scan(message: Message) -> None:
    """Обходит группы и каналы всех привязанных аккаунтов прямо сейчас."""
    settings = get_settings()
    pool = get_pool(settings)
    if not pool.ready():
        await message.answer(pool.hint())
        return

    status = await message.answer("Смотрю группы в аккаунтах, это может занять минуту…")
    async with SessionLocal() as session:
        found = await discover_dialogs(session, settings, pool)
        queued = await queue_scan(session)

    await status.edit_text(
        "<b>Готово</b>\n"
        f"Аккаунтов: {found['accounts']}\n"
        f"Групп и каналов: {found['dialogs']}\n"
        f"Новых источников: {found['created']}\n"
        f"В очереди на обход: {queued}\n\n"
        "Сообщения появятся в ленте по мере обхода групп."
    )


@router.message(Command("acc"))
async def cmd_acc(message: Message) -> None:
    from sqlalchemy import func, select

    from app.models import Source

    settings = get_settings()
    pool = get_pool(settings)

    async with SessionLocal() as session:
        total = (
            await session.execute(
                select(func.count()).select_from(Source).where(Source.ingest_mode == "scan")
            )
        ).scalar_one()
        done = (
            await session.execute(
                select(func.count())
                .select_from(Source)
                .where(Source.ingest_mode == "scan", Source.last_crawl_at.is_not(None))
            )
        ).scalar_one()

    if not pool.ready():
        await message.answer(pool.hint())
        return

    await message.answer(
        "<b>Сборщики групп</b>\n"
        f"Аккаунтов привязано: {len(pool.accounts)}\n"
        f"Групп в базе: {total}\n"
        f"Уже обойдено: {done}\n\n"
        "Обновить список групп: /scan"
    )


def chat_link(chat, message_id: int | None = None) -> str:
    """Ссылка на сообщение в публичном чате или во внутреннем суперчате."""
    if chat.username:
        base = f"https://t.me/{chat.username}"
    else:
        base = f"https://t.me/c/{str(chat.id).removeprefix('-100')}"
    return f"{base}/{message_id}" if message_id else base


def author_name(message: Message) -> str | None:
    if message.from_user is not None:
        return message.from_user.full_name
    if message.sender_chat is not None:
        return message.sender_chat.title
    return None


def incoming_from_message(message: Message) -> IncomingComment:
    chat = message.chat
    root = message.reply_to_message
    if root is not None and getattr(root, "forum_topic_created", None) is not None:
        root = None  # сообщение в теме форума — корень ветки не тема, а само сообщение

    author = message.from_user
    root_id = root.message_id if root else message.message_id
    return IncomingComment(
        external_id=str(message.message_id),
        text=(message.text or message.caption or "").strip(),
        published_at=message.date,
        url=chat_link(chat, message.message_id),
        post_external_id=str(root_id),
        post_url=chat_link(chat, root_id),
        post_text=((root.text or root.caption or "").strip() if root else ""),
        post_published_at=root.date if root else None,
        parent_external_id=str(root_id) if root else None,
        author_name=author_name(message),
        author_url=f"https://t.me/{author.username}" if author and author.username else None,
        author_external_id=str(author.id) if author else None,
    )


@router.my_chat_member()
async def on_bot_membership(event: ChatMemberUpdated, bot: Bot) -> None:
    """Бота добавили в чат — сразу заводим источник на того, кто добавил."""
    chat = event.chat
    if chat.type not in {"group", "supergroup", "channel"}:
        return
    status = getattr(event.new_chat_member.status, "value", event.new_chat_member.status)
    if status not in {"member", "administrator", "creator"}:
        return

    owner = event.from_user
    async with SessionLocal() as session:
        source = await ensure_chat_source(
            session,
            chat_id=chat.id,
            title=chat.title or "",
            handle=chat.username or "",
            url=chat_link(chat),
            owner_telegram_id=owner.id if owner else None,
            owner_username=owner.username if owner else None,
            owner_first_name=owner.first_name if owner else None,
        )

    if source is None or owner is None:
        return

    text = (
        f"Подключил чат «{html_escape(source.title)}».\n"
        "Новые сообщения оттуда попадают в ленту и проверяются по рубрикам."
    )
    if chat.type in {"group", "supergroup"}:
        text += (
            "\n\nЕсли сообщения не идут — выключите боту Privacy Mode в @BotFather "
            "(Bot Settings → Group Privacy) или сделайте его администратором."
        )
    else:
        text += "\n\nБот должен остаться администратором канала, иначе не увидит посты."

    try:
        await bot.send_message(owner.id, text, reply_markup=app_keyboard())
    except Exception as exc:  # noqa: BLE001 — владелец мог не начинать диалог
        logger.warning("Не смог написать владельцу %s: %s", owner.id, exc)


@router.message(F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_chat_message(message: Message, bot: Bot) -> None:
    """Каждое новое сообщение из подключённого чата — в ленту и в проверку по рубрикам."""
    text = (message.text or message.caption or "").strip()
    if not text or text.startswith("/"):
        return
    if message.from_user is not None and message.from_user.is_bot:
        return

    chat = message.chat
    author = message.from_user
    async with SessionLocal() as session:
        source = await ensure_chat_source(
            session,
            chat_id=chat.id,
            title=chat.title or "",
            handle=chat.username or "",
            url=chat_link(chat),
            owner_telegram_id=author.id if author else None,
            owner_username=author.username if author else None,
            owner_first_name=author.first_name if author else None,
        )
        if source is None:
            logger.info("Чат %s без владельца — добавьте бота кнопкой из приложения", chat.id)
            return
        await ingest_telegram_message(session, source, incoming_from_message(message), bot=bot)


@router.message(F.text & ~F.text.startswith("/"), F.chat.type == "private")
async def free_text(message: Message) -> None:
    await _search(message, (message.text or "").strip())


async def _search(
    message: Message,
    query: str,
    *,
    rubric: str | None = None,
    empty_ok: bool = False,
) -> None:
    if not query and not rubric:
        await message.answer("Напишите запрос: <code>/find блокировка</code>")
        return
    if not query and rubric and not empty_ok:
        return

    async with SessionLocal() as session:
        user = await _ensure_user(session, message)
        if user is None:
            return
        params = SearchParams(
            query=query,
            rubric=rubric,
            min_score=0.5 if rubric and not query else 0.0,
            sort="relevance" if query else "date",
            limit=5,
        )
        result = await search_comments(session, user.id, params)

    seconds = max(1, round(result["took_ms"] / 1000))
    await message.answer(
        f"Готово за {seconds} с. Проверено {result['checked']} сообщений, "
        f"совпадений — {result['matched']}."
    )

    if not result["items"]:
        await message.answer(
            "Ничего не нашёл. Добавьте источники в приложении — поиск идёт только по ним.",
            reply_markup=app_keyboard(),
        )
        return

    for item in result["items"]:
        await message.answer(card_from_item(item), disable_web_page_preview=True)

    rest = result["matched"] - len(result["items"])
    if rest > 0:
        await message.answer(
            f"Показать остальные {rest} — в приложении.",
            reply_markup=app_keyboard(q=query, rubric=rubric or ""),
        )


def _watches_of(user_id: int):
    from sqlalchemy import select

    return select(Watch).where(Watch.user_id == user_id).order_by(Watch.created_at.desc())


def create_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher


BOT_COMMANDS = [
    BotCommand(command="find", description="Найти комментарии"),
    BotCommand(command="art", description="Находки по рубрике"),
    BotCommand(command="scan", description="Обойти группы аккаунтов"),
    BotCommand(command="acc", description="Аккаунты и группы"),
    BotCommand(command="csv", description="Выгрузить в CSV"),
    BotCommand(command="add", description="Следить за словом"),
    BotCommand(command="words", description="Мои слова"),
    BotCommand(command="stats", description="Сводка"),
]


async def setup_bot(bot: Bot) -> None:
    settings = get_settings()
    try:
        await bot.set_my_commands(BOT_COMMANDS)
        if settings.webapp_url:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Открыть сканер",
                    web_app=WebAppInfo(url=settings.webapp_url),
                )
            )
    except Exception as exc:  # noqa: BLE001 — меню не критично для работы
        logger.warning("Не смог настроить меню бота: %s", exc)
