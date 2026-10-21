"""Web-сервис: Telegram Mini App (лента) + JSON API.

Доступ к ленте защищён валидацией Telegram initData (HMAC по токену
бота). Если задан OWNER_IDS — ленту видят только эти id. Пока OWNER_IDS
пуст (этап настройки), пускаем любого, кто прошёл валидацию initData.
"""
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from . import config, db

log = logging.getLogger("web")

app = FastAPI(title="Лента упоминаний")
STATIC = Path(__file__).parent / "static"


@app.on_event("startup")
async def _startup():
    if config.DATABASE_URL:
        try:
            await db.init_db()
            log.info("БД готова")
        except Exception as e:
            log.warning("init_db не удалось на старте: %s", e)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


def _check_init_data(init_data: str) -> dict | None:
    """Проверяет подпись Telegram initData. Возвращает распарсенного
    пользователя (dict) при успехе, иначе None."""
    if not init_data or not config.BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, received_hash):
        return None

    # Защита от старого initData (не старше суток).
    auth_date = pairs.get("auth_date")
    if auth_date and auth_date.isdigit():
        if time.time() - int(auth_date) > 86400:
            return None

    user_raw = pairs.get("user")
    if not user_raw:
        return None
    try:
        return json.loads(user_raw)
    except Exception:
        return None


def _extract_init_data(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("tma "):
        return auth[4:]
    return request.query_params.get("initData", "")


@app.get("/api/comments")
async def api_comments(request: Request, source: str = Query("all")):
    user = _check_init_data(_extract_init_data(request))
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if config.OWNER_IDS and user.get("id") not in config.OWNER_IDS:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    rows = await db.fetch_comments(source=source, limit=200)
    out = []
    for r in rows:
        created = r.get("created_at") or r.get("fetched_at")
        out.append({
            "source": r["source"],
            "author": r["author"],
            "text": r["text"],
            "url": r["url"],
            "matched": r["matched"],
            "is_own_page": r["is_own_page"],
            "created_at": created.isoformat() if created else None,
        })
    return {"comments": out, "count": len(out)}
