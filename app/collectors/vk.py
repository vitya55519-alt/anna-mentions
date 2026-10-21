"""Сборщик упоминаний продукта во ВКонтакте.

Используем метод newsfeed.search — публичный поиск по стене/новостям
по ключевым словам. Это стандартный brand-monitoring: находим посты,
где упоминают продукт, чтобы владелец видел фидбек. Поиска по людям и
какой-либо «оценки» тут нет — только ключевые слова продукта.
"""
import logging
import time

import httpx

from .. import config, db

log = logging.getLogger("collector.vk")

API = "https://api.vk.com/method/newsfeed.search"


async def poll_once() -> int:
    """Один проход поиска по всем ключевым словам. Возвращает число новых."""
    if not config.VK_TOKEN:
        log.info("VK_TOKEN не задан — пропускаю опрос ВК")
        return 0
    if not config.VK_KEYWORDS:
        log.info("VK_KEYWORDS пуст — нечего искать")
        return 0

    new_count = 0
    async with httpx.AsyncClient(timeout=20) as client:
        for kw in config.VK_KEYWORDS:
            try:
                resp = await client.get(API, params={
                    "q": kw,
                    "count": 100,
                    "access_token": config.VK_TOKEN,
                    "v": config.VK_API_VERSION,
                })
                data = resp.json()
            except Exception as e:  # сеть/парсинг — не роняем воркер
                log.warning("Ошибка запроса ВК по '%s': %s", kw, e)
                continue

            if "error" in data:
                log.warning("VK API error по '%s': %s", kw, data["error"].get("error_msg"))
                continue

            items = data.get("response", {}).get("items", [])
            for it in items:
                owner_id = it.get("owner_id") or it.get("from_id")
                post_id = it.get("id")
                if owner_id is None or post_id is None:
                    continue
                ext = f"{owner_id}_{post_id}"
                url = f"https://vk.com/wall{ext}"
                text = (it.get("text") or "").strip()
                created = it.get("date")
                created_dt = _ts(created)
                try:
                    is_new = await db.insert_comment(
                        source="vk",
                        external_id=ext,
                        author=str(owner_id),
                        text=text,
                        url=url,
                        matched=kw,
                        created_at=created_dt,
                    )
                    if is_new:
                        new_count += 1
                except Exception as e:
                    log.warning("Не удалось сохранить запись ВК %s: %s", ext, e)

    if new_count:
        log.info("ВК: добавлено новых упоминаний: %d", new_count)
    return new_count


def _ts(unix):
    if not unix:
        return None
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(int(unix), tz=timezone.utc)
    except Exception:
        return None
