"""Конфигурация из переменных окружения.

Все секреты берём из env (на Railway — из Variables сервиса).
Сервисы должны стартовать даже без заполненных токенов: тогда
соответствующий сборщик просто логирует, что ждёт токен, и не падает.
"""
import os


def _clean(value: str) -> str:
    return (value or "").strip()


# Подключение к БД (Railway прокидывает DATABASE_URL в оба сервиса).
DATABASE_URL = _clean(os.getenv("DATABASE_URL"))

# Токен Telegram-бота от @BotFather. Нужен worker'у (приём комментов
# из групп обсуждения) и web (валидация initData мини-аппа).
BOT_TOKEN = _clean(os.getenv("BOT_TOKEN"))

# Токен доступа ВК (сервисный или пользовательский) для поиска по стене.
VK_TOKEN = _clean(os.getenv("VK_TOKEN"))

# Ключевые слова мониторинга бренда/продукта, через запятую.
# Например: "Anna бот, t.me/Anna634212, имя продукта"
VK_KEYWORDS = [k.strip() for k in _clean(os.getenv("VK_KEYWORDS")).split(",") if k.strip()]

# Telegram id владельца(ев) — кому можно открывать ленту мини-аппа.
# Через запятую. Если пусто — лента доступна любому, кто прошёл
# валидацию initData (на этапе настройки удобно, потом заполнить).
OWNER_IDS = {int(x) for x in _clean(os.getenv("OWNER_IDS")).replace(" ", "").split(",") if x.strip().isdigit()}

# Период опроса ВК в секундах.
POLL_INTERVAL = int(_clean(os.getenv("POLL_INTERVAL")) or "300")

# Версия VK API.
VK_API_VERSION = _clean(os.getenv("VK_API_VERSION")) or "5.199"
