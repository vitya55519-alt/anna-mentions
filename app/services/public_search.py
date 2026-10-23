"""Поиск упоминаний в публичных каналах Телеграма.

Сам Телеграм не умеет искать «по всему сразу», зато публичные каналы видны
в обычном интернет-поиске. Поисковики напрямую с сервера закрыты, поэтому
запрос идёт через бесплатный посредник r.jina.ai, а найденные посты
Телеграм отдаёт своим веб-просмотром t.me/<канал>/<номер>.

Ищутся именно посты каналов. Комментарии под постами в интернете не видны —
их читает только основной сканер через аккаунты.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from urllib.parse import quote, unquote

import httpx

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
READER = "https://r.jina.ai/"
POST_RE = re.compile(r"https?://t\.me/([A-Za-z0-9_]{3,})/(\d+)")
TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
DATE_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
OWNER_RE = re.compile(r'tgme_widget_message_owner_name[^>]*>(?:<span[^>]*>)?([^<]+)')
TAG_RE = re.compile(r"<[^>]+>")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
CACHE_TTL = 300.0
CACHE_SIZE = 64
_cache: dict[str, tuple[float, dict]] = {}


def build_query(raw: str) -> str:
    """@ник и ссылка t.me превращаются в запрос «упоминания на t.me»."""
    text = (raw or "").strip()
    if not text:
        return ""
    link = POST_RE.search(text)
    if link:
        return f"@{link.group(1)} site:t.me"
    if text.startswith("@"):
        return f"{text} site:t.me"
    return f'"{text}" site:t.me'


def parse_links(markdown: str) -> list[str]:
    """Ссылки на посты t.me из ответа поиска — в порядке выдачи, без повторов."""
    found: list[tuple[int, str]] = []
    for match in re.finditer(r"https?://t\.me/[A-Za-z0-9_]+/\d+", markdown):
        found.append((match.start(), match.group(0)))
    for match in re.finditer(r"uddg=([^)&\s\"']+)", markdown):
        found.append((match.start(), unquote(match.group(1))))

    links: list[str] = []
    seen: set[str] = set()
    for _, value in sorted(found):
        match = POST_RE.search(value)
        if not match:
            continue
        url = f"https://t.me/{match.group(1)}/{match.group(2)}"
        if url in seen:
            continue
        seen.add(url)
        links.append(url)
    return links


def needles_for(raw: str) -> list[str]:
    """Что именно ищем в тексте поста: ник целиком или все слова запроса."""
    text = (raw or "").strip()
    link = POST_RE.search(text)
    if link:
        return [f"@{link.group(1)}".lower()]
    if text.startswith("@"):
        return [text.lower()]
    words = [word for word in re.findall(r"[\w@]+", text.lower()) if len(word) >= 3]
    return words or [text.lower()]


def matches(text: str, raw: str) -> bool:
    """Подходит ли текст поста под запрос.

    Для ника достаточно самого ника — с собакой или отдельным словом.
    Для текста нужны все значимые слова — как отдельные слова, не куски.
    """
    haystack = (text or "").lower()
    if not haystack:
        return False
    words = needles_for(raw)
    if not words:
        return False
    if words[0].startswith("@"):
        name = words[0][1:]
        if f"@{name}" in haystack:
            return True
        return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", haystack) is not None
    return all(
        re.search(rf"(?<!\w){re.escape(word)}(?!\w)", haystack) is not None for word in words
    )


def parse_post(page: str, url: str) -> dict:
    """Текст, дата и название канала из страницы веб-просмотра поста."""
    text = ""
    found = TEXT_RE.search(page)
    if found:
        text = html.unescape(TAG_RE.sub(" ", found.group(1)))
        text = " ".join(text.split())
    date = ""
    stamp = DATE_RE.search(page)
    if stamp:
        date = stamp.group(1)
    name = ""
    owner = OWNER_RE.search(page)
    if owner:
        name = " ".join(html.unescape(owner.group(1)).split())
    parts = url.split("/")
    return {
        "url": url,
        "channel": name or parts[3],
        "channel_url": f"https://t.me/{parts[3]}",
        "text": text,
        "date": date,
    }


def _clean_markdown(markdown: str) -> str:
    """Грубый запасной разбор, если веб-просмотр поста не открылся."""
    text = IMAGE_RE.sub(" ", markdown)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[*_#>`]+", " ", text)
    return " ".join(text.split())[:800]


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url, headers={"User-Agent": UA, "Accept-Language": "ru,en"})
    response.raise_for_status()
    return response.text


async def fetch_post(client: httpx.AsyncClient, url: str) -> dict | None:
    """Пост целиком: сначала сам Телеграм, потом — через посредника."""
    try:
        page = await _fetch(client, f"{url}?embed=1&mode=tme")
        post = parse_post(page, url)
        if post["text"]:
            return post
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("Веб-просмотр поста не открылся: %s (%s)", url, exc)
    try:
        markdown = await _fetch(client, READER + url)
        post = parse_post(markdown, url)
        if not post["text"]:
            post["text"] = _clean_markdown(markdown)
        if post["text"]:
            return post
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("Пост через посредника не открылся: %s (%s)", url, exc)
    return None


async def search_public(raw_query: str, limit: int = 10, scan: int = 15) -> dict:
    """Найти посты публичных каналов, где встречается запрос."""
    query = build_query(raw_query)
    result: dict = {
        "query": raw_query,
        "engine_query": query,
        "items": [],
        "checked": 0,
        "error": "",
    }
    if not query:
        result["error"] = "Пустой запрос"
        return result

    cached = _cache.get(query)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    markdown = ""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        try:
            markdown = await _fetch(
                client, READER + "https://lite.duckduckgo.com/lite/?q=" + quote(query, safe="")
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Поиск через посредника не ответил: %s", exc)
        if not parse_links(markdown):
            try:
                markdown = await _fetch(
                    client, "https://html.duckduckgo.com/html/?q=" + quote(query, safe="")
                )
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("Запасной поиск не ответил: %s", exc)

        links = parse_links(markdown)[:scan]
        if not links:
            result["error"] = "Поисковик ничего не вернул — попробуйте другой запрос"
            _remember(query, result)
            return result

        gate = asyncio.Semaphore(5)

        async def one(url: str) -> dict | None:
            async with gate:
                return await fetch_post(client, url)

        posts = await asyncio.gather(*(one(link) for link in links))

    items: list[dict] = []
    link = POST_RE.search(raw_query or "")
    if link:
        nick = link.group(1).lower()
    elif (raw_query or "").strip().startswith("@"):
        nick = raw_query.strip()[1:].lower()
    else:
        nick = ""
    for post in posts:
        if not post or not post["text"]:
            continue
        # Публичный канал самого человека — тоже находка, даже без упоминания в тексте.
        own = bool(nick) and post["url"].split("/")[3].lower() == nick
        if not own and not matches(post["text"], raw_query):
            continue
        items.append({key: post[key] for key in ("url", "channel", "channel_url", "text", "date")})
        if len(items) >= limit:
            break

    result["items"] = items
    result["checked"] = len(links)
    _remember(query, result)
    return result


def _remember(query: str, result: dict) -> None:
    """Короткий кеш: одинаковый запрос не дёргает поисковик повторно."""
    if len(_cache) >= CACHE_SIZE:
        oldest = min(_cache, key=lambda key: _cache[key][0])
        _cache.pop(oldest, None)
    _cache[query] = (time.time(), result)
