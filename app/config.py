from __future__ import annotations

from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_dsn(raw: str) -> str:
    """Railway отдаёт postgresql://…?sslmode=require — asyncpg таких параметров не понимает."""
    if not raw:
        return raw
    parts = urlsplit(raw.strip())
    scheme = "postgresql+asyncpg" if parts.scheme in {"postgres", "postgresql"} else parts.scheme
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query)
        if key not in {"sslmode", "ssl", "channel_binding"}
    ]
    netloc = parts.netloc
    if scheme.startswith("postgresql"):
        netloc = netloc.replace(" ", "")
    return urlunsplit((scheme, netloc, parts.path, urlencode(query), parts.fragment))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram -----------------------------------------------------------
    bot_token: str = ""
    # Публичный HTTPS-адрес сервиса. На Railway можно не задавать:
    # платформа сама выставляет RAILWAY_PUBLIC_DOMAIN, адрес соберётся из него.
    webapp_url: str = ""
    railway_public_domain: str = ""
    # Бот читает сообщения из чатов, куда его добавили, без MTProto-сессии.
    tg_live_ingest: bool = True

    # --- Хранилище ----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/comments"

    # --- VK -----------------------------------------------------------------
    vk_token: str = ""
    vk_user_token: str = ""
    vk_api_version: str = "5.199"

    # --- OK -----------------------------------------------------------------
    ok_app_key: str = ""
    ok_app_secret: str = ""
    ok_access_token: str = ""
    ok_adapter_enabled: bool = False

    # --- Telegram-источники (MTProto) ---------------------------------------
    # Одно приложение (api_id/api_hash) и несколько аккаунтов-сборщиков.
    # TG_SESSION_1..5 — строки сессий: python scripts/tg_login.py
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session_1: str = ""
    tg_session_2: str = ""
    tg_session_3: str = ""
    tg_session_4: str = ""
    tg_session_5: str = ""
    tg_session_string: str = ""

    # --- Instagram ----------------------------------------------------------
    ig_access_token: str = ""
    ig_business_id: str = ""
    instagram_enabled: bool = False

    # --- Какие сети включены в интерфейсе ------------------------------------
    # Код адаптеров ВК/ОК/Instagram остаётся в проекте: достаточно перечислить их
    # здесь через запятую и заполнить токены, чтобы они снова появились в приложении.
    enabled_networks: str = "telegram"

    # --- Проверка по рубрикам ------------------------------------------------
    rubrics_file: str = "default.json"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "qwen2.5:7b"
    llm_timeout_seconds: float = 40.0
    llm_min_score: float = 0.45
    analyze_on_ingest: bool = True

    # --- Сканирование групп аккаунтами --------------------------------------
    tg_scan_enabled: bool = True
    # Сколько диалогов брать у каждого аккаунта (сортировка по активности)
    tg_scan_dialogs_limit: int = 100
    # Сколько последних сообщений читать из группы за один проход
    tg_scan_messages_per_dialog: int = 120
    # Сканировать ли каналы, а не только группы
    tg_scan_channels: bool = True
    # Кому принадлежат найденные группы (0 — первый запустивший бота)
    owner_telegram_id: int = 0
    # Пауза между сообщениями: снижает риск FloodWait
    tg_scan_pause_seconds: float = 0.3
    # Как часто перечитывать список групп аккаунтов
    tg_scan_interval_minutes: int = 60

    # --- Прочее -------------------------------------------------------------
    dev_mode: bool = False
    dev_telegram_id: int = 1

    default_crawl_interval_minutes: int = 60
    crawl_posts_per_run: int = 20
    crawl_backfill_posts: int = 40
    crawl_comments_per_post: int = 200
    worker_poll_seconds: int = 20
    search_page_size: int = 30
    notify_on_new_comments: bool = True

    @field_validator("database_url")
    @classmethod
    def _validate_dsn(cls, value: str) -> str:
        return normalize_dsn(value)

    @property
    def dsn(self) -> str:
        return self.database_url

    @property
    def public_webapp_url(self) -> str:
        """Адрес Mini App: из WEBAPP_URL или из домена, который дал Railway."""
        raw = (self.webapp_url or "").strip()
        if not raw and self.railway_public_domain:
            raw = f"https://{self.railway_public_domain.strip()}"
        if raw and not raw.startswith(("http://", "https://")):
            raw = f"https://{raw}"
        return raw.rstrip("/")

    @property
    def enabled_network_values(self) -> set[str]:
        return {item.strip().lower() for item in self.enabled_networks.split(",") if item.strip()}

    @property
    def vk_access_token(self) -> str:
        """Токен пользователя даёт доступ к wall.getComments, сервисный — только к части методов."""
        return self.vk_user_token or self.vk_token

    @property
    def telegram_sources_ready(self) -> bool:
        return bool(self.tg_api_id and self.tg_api_hash and self.tg_session_string)

    @property
    def tg_sessions(self) -> list[str]:
        """Аккаунты-сборщики: TG_SESSION_1..5 плюс старая TG_SESSION_STRING."""
        values = [
            self.tg_session_1,
            self.tg_session_2,
            self.tg_session_3,
            self.tg_session_4,
            self.tg_session_5,
            self.tg_session_string,
        ]
        return [value.strip() for value in values if value and value.strip()]

    @property
    def tg_accounts_ready(self) -> bool:
        return bool(self.tg_api_id and self.tg_api_hash and self.tg_sessions)

    @property
    def ok_ready(self) -> bool:
        return bool(self.ok_app_key and self.ok_app_secret and self.ok_access_token)

    @property
    def llm_ready(self) -> bool:
        """Любой OpenAI-совместимый эндпоинт: Ollama (/v1), OpenRouter, OpenAI, локальный vLLM."""
        return bool(self.llm_base_url and self.llm_model)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
