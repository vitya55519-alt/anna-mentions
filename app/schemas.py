from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import Network


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MeOut(ORMModel):
    telegram_id: int
    first_name: str | None = None
    username: str | None = None
    # Нужен мини-приложению, чтобы открыть «Добавить бота в чат».
    bot_username: str = ""


class UserOut(ORMModel):
    id: int
    telegram_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class SourceOut(ORMModel):
    id: int
    network: Network
    network_title: str = ""
    mode: str = "live"
    external_id: str
    handle: str
    title: str
    url: str
    avatar_url: str | None = None
    description: str | None = None
    is_active: bool
    crawl_interval_minutes: int
    last_crawl_at: datetime | None = None
    next_crawl_at: datetime | None = None
    last_error: str | None = None
    posts_count: int
    comments_count: int


class SourceResolveIn(BaseModel):
    ref: str = Field(min_length=1, max_length=512)


class ResolvedSource(BaseModel):
    network: Network
    external_id: str
    handle: str
    title: str
    url: str
    avatar_url: str | None = None
    description: str | None = None


class SourceCreateIn(BaseModel):
    network: Network
    ref: str = Field(min_length=1, max_length=512)
    crawl_interval_minutes: int | None = Field(default=None, ge=10, le=1440)


class SourcePatchIn(BaseModel):
    is_active: bool | None = None
    crawl_interval_minutes: int | None = Field(default=None, ge=10, le=1440)


class SourceRefOut(BaseModel):
    id: int
    title: str
    handle: str
    network: Network
    avatar_url: str | None = None


class PostPreview(BaseModel):
    id: int
    text: str
    url: str
    published_at: datetime


class CommentOut(BaseModel):
    id: int
    network: Network
    text: str
    snippet: str
    author_name: str | None = None
    author_url: str | None = None
    published_at: datetime
    likes: int = 0
    url: str | None = None
    parent_external_id: str | None = None
    verdict: str = "pending"
    rubric: str | None = None
    rubric_title: str | None = None
    rationale: str | None = None
    score: float = 0.0
    source: SourceRefOut
    post: PostPreview


class SearchOut(BaseModel):
    items: list[CommentOut]
    total: int
    checked: int
    matched: int
    limit: int
    offset: int
    took_ms: int
    facets: dict[str, int]
    rubrics: dict[str, int]
    query: str = ""


class LookupIn(BaseModel):
    # Список строк как ввёл пользователь: @ник, id, телефон — по одному или через запятую.
    identifiers: list[str] = Field(min_length=1, max_length=10)


class IdentifierGroupOut(BaseModel):
    raw: str
    kind: str
    value: str
    total: int
    items: list[CommentOut]


class LookupOut(BaseModel):
    groups: list[IdentifierGroupOut]
    unknown: list[str] = Field(default_factory=list)
    total: int
    took_ms: int


class WatchOut(ORMModel):
    id: int
    keyword: str
    network: Network | None = None
    source_id: int | None = None
    is_active: bool
    hits_count: int
    last_hit_at: datetime | None = None


class WatchCreateIn(BaseModel):
    keyword: str = Field(min_length=2, max_length=128)
    network: Network | None = None
    source_id: int | None = None


class StatsOut(BaseModel):
    sources_total: int
    sources_active: int
    posts_total: int
    comments_total: int
    comments_last_24h: int
    checked_total: int
    flagged_total: int
    by_network: dict[str, int]
    by_rubric: dict[str, int]
    last_crawl_at: datetime | None = None


class RubricOut(BaseModel):
    code: str
    title: str
    hint: str = ""


class NetworkCapability(BaseModel):
    network: Network
    title: str
    mode: str
    ready: bool
    hint: str


class HealthOut(BaseModel):
    status: str
    version: str
