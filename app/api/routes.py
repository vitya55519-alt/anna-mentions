from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.adapters import AdapterError, capabilities, get_adapter
from app.api.deps import CurrentUser, SessionDep
from app.config import get_settings
from app.models import Network, Source, Watch
from app.schemas import (
    MeOut,
    NetworkCapability,
    ResolvedSource,
    RubricOut,
    SearchOut,
    SourceCreateIn,
    SourceOut,
    SourcePatchIn,
    SourceResolveIn,
    StatsOut,
    WatchCreateIn,
    WatchOut,
)
from app.services.rubrics import get_rubric_set
from app.services.search import (
    SearchParams,
    build_stats,
    export_csv,
    search_comments,
)

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)

_bot_username_cache: str | None = None


async def _bot_username() -> str:
    """Имя бота берём у Telegram один раз — оно нужно для ссылки «добавить в чат»."""
    global _bot_username_cache
    if _bot_username_cache is not None:
        return _bot_username_cache
    token = get_settings().bot_token
    if not token:
        return ""
    try:
        async with Bot(token) as bot:
            me = await bot.get_me()
    except Exception as exc:  # noqa: BLE001 — без имени интерфейс предложит добавить вручную
        logger.warning("Не смог получить имя бота: %s", exc)
        return ""
    _bot_username_cache = me.username or ""
    return _bot_username_cache


def _source_out(source: Source) -> SourceOut:
    adapter = get_adapter(source.network)
    data = SourceOut.model_validate(source)
    data.network_title = source.network.title
    # live — сообщения приходят от бота в реальном времени, а не по расписанию.
    data.mode = "live" if source.ingest_mode == "live" else adapter.mode
    return data


@router.get("/me", response_model=MeOut)
async def me(user: CurrentUser) -> MeOut:
    return MeOut(
        telegram_id=user.telegram_id,
        first_name=user.first_name,
        username=user.username,
        bot_username=await _bot_username(),
    )


@router.get("/capabilities", response_model=list[NetworkCapability])
async def network_capabilities(_: CurrentUser) -> list[NetworkCapability]:
    return [NetworkCapability(**item) for item in capabilities()]


@router.get("/rubrics", response_model=list[RubricOut])
async def rubrics(_: CurrentUser) -> list[RubricOut]:
    from app.config import get_settings

    rubric_set = get_rubric_set(get_settings().rubrics_file)
    return [
        RubricOut(code=item.code, title=item.title, hint=item.description)
        for item in rubric_set.categories
    ]


@router.get("/sources", response_model=list[SourceOut])
async def list_sources(user: CurrentUser, session: SessionDep) -> list[SourceOut]:
    sources = (
        await session.execute(
            select(Source).where(Source.user_id == user.id).order_by(Source.created_at.desc())
        )
    ).scalars()
    return [_source_out(source) for source in sources]


@router.post("/sources/resolve", response_model=ResolvedSource)
async def resolve_source(payload: SourceResolveIn, _: CurrentUser) -> ResolvedSource:
    ref = payload.ref.strip()
    network = _guess_network(ref)
    _require_enabled(network)
    adapter = get_adapter(network)
    try:
        resolved = await adapter.resolve(ref)
    except AdapterError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ResolvedSource(**asdict(resolved))


@router.post("/sources", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
async def create_source(payload: SourceCreateIn, user: CurrentUser, session: SessionDep) -> SourceOut:
    _require_enabled(payload.network)
    adapter = get_adapter(payload.network)
    if not adapter.ready():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=adapter.hint or f"{payload.network.title}: интеграция не настроена",
        )

    try:
        resolved = await adapter.resolve(payload.ref)
    except AdapterError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    existing = (
        await session.execute(
            select(Source).where(
                Source.user_id == user.id,
                Source.network == payload.network,
                Source.external_id == resolved.external_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Такой источник уже добавлен")

    source = Source(
        user_id=user.id,
        network=payload.network,
        external_id=resolved.external_id,
        handle=resolved.handle,
        title=resolved.title,
        url=resolved.url,
        avatar_url=resolved.avatar_url,
        description=resolved.description,
        crawl_interval_minutes=payload.crawl_interval_minutes or _default_interval(),
        next_crawl_at=datetime.now(timezone.utc),
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)
    return _source_out(source)


@router.patch("/sources/{source_id}", response_model=SourceOut)
async def update_source(
    source_id: int,
    payload: SourcePatchIn,
    user: CurrentUser,
    session: SessionDep,
) -> SourceOut:
    source = await _own_source(session, user.id, source_id)
    if payload.is_active is not None:
        source.is_active = payload.is_active
        if payload.is_active:
            source.next_crawl_at = datetime.now(timezone.utc)
    if payload.crawl_interval_minutes is not None:
        source.crawl_interval_minutes = payload.crawl_interval_minutes
    await session.commit()
    await session.refresh(source)
    return _source_out(source)


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(source_id: int, user: CurrentUser, session: SessionDep) -> Response:
    source = await _own_source(session, user.id, source_id)
    await session.delete(source)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sources/{source_id}/crawl", response_model=SourceOut)
async def queue_crawl(source_id: int, user: CurrentUser, session: SessionDep) -> SourceOut:
    """Ставит источник в очередь: воркер подхватит его в ближайшие секунды."""
    source = await _own_source(session, user.id, source_id)
    if source.ingest_mode == "live":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Этот чат бот читает в реальном времени — обход по расписанию не нужен",
        )
    if not source.is_active:
        source.is_active = True
    source.next_crawl_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(source)
    return _source_out(source)


@router.get("/search", response_model=SearchOut)
async def search(
    user: CurrentUser,
    session: SessionDep,
    q: str = "",
    network: Network | None = None,
    source_id: int | None = None,
    rubric: str | None = None,
    verdict: str | None = None,
    min_score: float = 0.0,
    days: int | None = Query(default=None, ge=1, le=3650),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    sort: str = Query(default="date", pattern="^(date|relevance|score)$"),
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
) -> SearchOut:
    if days:
        date_from = datetime.now(timezone.utc) - timedelta(days=days)

    params = SearchParams(
        query=q.strip(),
        network=network.value if network else None,
        source_id=source_id,
        rubric=rubric,
        verdict=verdict,
        min_score=min_score,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    result = await search_comments(session, user.id, params)
    return SearchOut(**result)


@router.get("/search.csv")
async def search_csv(
    user: CurrentUser,
    session: SessionDep,
    q: str = "",
    network: Network | None = None,
    source_id: int | None = None,
    rubric: str | None = None,
    verdict: str | None = None,
    min_score: float = 0.0,
    days: int | None = Query(default=None, ge=1, le=3650),
    limit: int = Query(default=1000, ge=1, le=5000),
) -> Response:
    params = SearchParams(
        query=q.strip(),
        network=network.value if network else None,
        source_id=source_id,
        rubric=rubric,
        verdict=verdict,
        min_score=min_score,
        date_from=datetime.now(timezone.utc) - timedelta(days=days) if days else None,
        limit=limit,
    )
    payload = await export_csv(session, user.id, params)
    filename = f"comments-{datetime.now(timezone.utc):%Y%m%d-%H%M}.csv"
    return Response(
        content=payload.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/stats", response_model=StatsOut)
async def stats(user: CurrentUser, session: SessionDep) -> StatsOut:
    return StatsOut(**await build_stats(session, user.id))


@router.get("/watches", response_model=list[WatchOut])
async def list_watches(user: CurrentUser, session: SessionDep) -> list[WatchOut]:
    watches = (
        await session.execute(
            select(Watch).where(Watch.user_id == user.id).order_by(Watch.created_at.desc())
        )
    ).scalars()
    return [WatchOut.model_validate(watch) for watch in watches]


@router.post("/watches", response_model=WatchOut, status_code=status.HTTP_201_CREATED)
async def create_watch(payload: WatchCreateIn, user: CurrentUser, session: SessionDep) -> WatchOut:
    if payload.source_id is not None:
        await _own_source(session, user.id, payload.source_id)

    watch = Watch(
        user_id=user.id,
        keyword=payload.keyword.strip(),
        network=payload.network,
        source_id=payload.source_id,
    )
    session.add(watch)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Такое слово уже отслеживается"
        ) from exc
    await session.refresh(watch)
    return WatchOut.model_validate(watch)


@router.delete("/watches/{watch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_watch(watch_id: int, user: CurrentUser, session: SessionDep) -> Response:
    watch = await session.get(Watch, watch_id)
    if watch is None or watch.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Слово не найдено")
    await session.delete(watch)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _own_source(session: SessionDep, user_id: int, source_id: int) -> Source:
    source = await session.get(Source, source_id)
    if source is None or source.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Источник не найден")
    return source


def _default_interval() -> int:
    return get_settings().default_crawl_interval_minutes


def _require_enabled(network: Network) -> None:
    """Сети без токенов скрыты: код адаптера остаётся, но включается через ENABLED_NETWORKS."""
    settings = get_settings()
    enabled = settings.enabled_network_values
    if network.value in enabled:
        return
    titles = ", ".join(item.title for item in Network if item.value in enabled) or "ничего"
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"{network.title} сейчас отключён в настройках. Включены: {titles}.",
    )


def _guess_network(ref: str) -> Network:
    text = ref.lower()
    if "ok.ru" in text or "odnoklassniki" in text:
        return Network.ok
    if "t.me" in text or "telegram" in text or text.startswith("@"):
        return Network.telegram
    if "instagram" in text:
        return Network.instagram
    return Network.vk
