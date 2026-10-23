from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Network(str, enum.Enum):
    vk = "vk"
    ok = "ok"
    telegram = "telegram"
    instagram = "instagram"

    @property
    def title(self) -> str:
        return {
            "vk": "ВКонтакте",
            "ok": "Одноклассники",
            "telegram": "Telegram",
            "instagram": "Instagram",
        }[self.value]


network_enum = Enum(Network, native_enum=False, length=16, name="network_enum")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(8))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Source(Base):
    """Сообщество, канал или страница, комментарии которых мы индексируем."""

    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("user_id", "network", "external_id", name="uq_sources_owner_network_ext"),
        Index("ix_sources_due", "is_active", "next_crawl_at"),
        Index("ix_sources_network_external", "network", "external_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    network: Mapped[Network] = mapped_column(network_enum)
    external_id: Mapped[str] = mapped_column(String(64))
    handle: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(512))
    avatar_url: Mapped[str | None] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(Text)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # crawl — плановый обход адаптером; live — сообщения приходят от бота в реальном времени.
    ingest_mode: Mapped[str] = mapped_column(String(16), default="crawl", server_default="crawl")
    crawl_interval_minutes: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    next_crawl_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    last_crawl_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    cursor: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

    posts_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    comments_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Post(Base):
    """Публикация, под которой собираются комментарии."""

    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_posts_source_external"),
        Index("ix_posts_source_published", "source_id", "published_at"),
        Index("ix_posts_search_tsv", "search_tsv", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    network: Mapped[Network] = mapped_column(network_enum)
    external_id: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(512))
    text: Mapped[str] = mapped_column(Text, default="")
    author_name: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    comments_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    search_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('russian', coalesce(text, ''))", persisted=True)
    )


class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = (
        UniqueConstraint("post_id", "external_id", name="uq_comments_post_external"),
        Index("ix_comments_source_published", "source_id", "published_at"),
        Index("ix_comments_network_published", "network", "published_at"),
        Index("ix_comments_search_tsv", "search_tsv", postgresql_using="gin"),
        Index("ix_comments_verdict_score", "verdict", "risk_score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    network: Mapped[Network] = mapped_column(network_enum)
    external_id: Mapped[str] = mapped_column(String(64))
    author_name: Mapped[str | None] = mapped_column(String(255))
    author_url: Mapped[str | None] = mapped_column(String(512))
    author_external_id: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    likes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    parent_external_id: Mapped[str | None] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(String(512))

    # Результат проверки по рубрикам: pending → clean / borderline / likely
    verdict: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    risk_score: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    rubric: Mapped[str | None] = mapped_column(String(32))
    rationale: Mapped[str | None] = mapped_column(Text)
    analysis: Mapped[dict | None] = mapped_column(JSONB)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    search_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('russian', coalesce(text, ''))", persisted=True)
    )


class Watch(Base):
    """Ключевое слово: бот присылает комментарии, где оно встретилось."""

    __tablename__ = "watches"
    __table_args__ = (
        UniqueConstraint("user_id", "keyword", "source_id", name="uq_watches_owner_kw_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    keyword: Mapped[str] = mapped_column(String(128))
    network: Mapped[Network | None] = mapped_column(network_enum)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    hits_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JoinTask(Base):
    """Ссылка на чат, в который нужно вступить аккаунту-сборщику."""

    __tablename__ = "join_tasks"
    __table_args__ = (
        UniqueConstraint("user_id", "ref", name="uq_join_tasks_user_ref"),
        Index("ix_join_tasks_due", "status", "next_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Нормализованная ссылка: t.me/name или t.me/+invite_hash.
    ref: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16), default="public", server_default="public")
    # pending / joined / exists / error / cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    account_index: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    # Вместе с каналом вступили в его группу обсуждения: занимает второй слот квоты.
    group_joined: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    next_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TgAccount(Base):
    """Аккаунт-сборщик, добавленный через админку: строка сессии Telethon."""

    __tablename__ = "tg_accounts"
    __table_args__ = (Index("ix_tg_accounts_active", "is_active"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True)
    tg_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    session: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")
    posts_seen: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    comments_seen: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    comments_new: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
