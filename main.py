"""GeniMap — FastAPI server + Aiogram 3.x bot (Long Polling).

Ishga tushirish:
    1) .env fayliga BOT_TOKEN va WEBAPP_URL yozing (namuna: .env.example)
    2) python main.py

Endpointlar:
    GET  /               — Mini App frontend (templates/index.html)
    POST /auth           — Telegram initData'ni HMAC-SHA256 bilan tekshirish
    POST /update_location— joriy foydalanuvchi lokatsiyasini yangilash
    GET  /get_users      — barcha foydalanuvchilar ro'yxati
    POST /ping_user      — boshqa foydalanuvchiga chaqiruv yuborish
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from bot import BOT_TOKEN, WEBAPP_URL, bot, dp, send_ping_message
from database import (
    User,
    get_all_users,
    get_db,
    get_user_by_telegram_id,
    init_db,
    update_user_location,
    upsert_user,
    utcnow,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("genimap.main")

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "templates" / "index.html"

# Sessiya tokeni va initData amal qilish muddatlari.
SESSION_TTL_SECONDS = int(os.getenv("GENIMAP_SESSION_TTL", str(60 * 60 * 24 * 7)))
INIT_DATA_MAX_AGE_SECONDS = int(os.getenv("GENIMAP_INITDATA_MAX_AGE", str(60 * 60 * 24)))

# Sessiya tokenlari bot tokenidan hosil qilingan maxfiy kalit bilan imzolanadi.
_SESSION_SECRET = hmac.new(
    b"GeniMap session signing key v1",
    BOT_TOKEN.encode("utf-8"),
    hashlib.sha256,
).digest()


class AuthError(Exception):
    """initData yoki sessiya tokeni yaroqsiz bo'lganda ko'tariladi."""


def _telegram_secret_key(bot_token: str) -> bytes:
    """Telegram hujjatlaridagi algoritm: HMAC_SHA256("WebAppData", bot_token)."""
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = INIT_DATA_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Telegram `initData` satrini HMAC-SHA256 orqali tekshiradi.

    Muvaffaqiyatli bo'lsa, `user` kaliti ichida foydalanuvchi ma'lumotlari bo'lgan
    lug'at qaytaradi. Aks holda `AuthError` ko'taradi.
    """
    if not init_data or not isinstance(init_data, str):
        raise AuthError("initData bo'sh")

    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    except ValueError as exc:
        raise AuthError("initData formati noto'g'ri") from exc

    data = dict(pairs)
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise AuthError("initData ichida hash topilmadi")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    calculated_hash = hmac.new(
        _telegram_secret_key(bot_token),
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash.lower()):
        raise AuthError("initData imzosi noto'g'ri")

    try:
        auth_date = int(data.get("auth_date", "0"))
    except (TypeError, ValueError) as exc:
        raise AuthError("auth_date formati noto'g'ri") from exc

    if auth_date <= 0:
        raise AuthError("auth_date topilmadi")

    now = int(time.time())
    if auth_date - now > 300:
        raise AuthError("auth_date kelajakda — soatni tekshiring")
    if max_age_seconds > 0 and now - auth_date > max_age_seconds:
        raise AuthError("initData eskirgan, Mini App'ni qaytadan oching")

    raw_user = data.get("user")
    if not raw_user:
        raise AuthError("initData ichida user ma'lumoti yo'q")

    try:
        user_payload = json.loads(raw_user)
    except json.JSONDecodeError as exc:
        raise AuthError("user ma'lumoti JSON emas") from exc

    if not isinstance(user_payload, dict) or not isinstance(user_payload.get("id"), int):
        raise AuthError("user ma'lumoti to'liq emas")

    validated: dict[str, Any] = dict(data)
    validated["user"] = user_payload
    return validated


def create_session_token(telegram_id: int) -> tuple[str, int]:
    """Imzolangan sessiya tokenini va uning amal qilish muddatini qaytaradi."""
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{int(telegram_id)}:{expires_at}"
    signature = hmac.new(
        _SESSION_SECRET,
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    raw = base64.urlsafe_b64encode(f"{payload}:{signature}".encode("utf-8")).decode("ascii")
    return raw.rstrip("="), expires_at


def parse_session_token(token: str) -> int:
    """Sessiya tokenini tekshiradi va telegram_id'ni qaytaradi."""
    if not token:
        raise AuthError("Token bo'sh")

    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        telegram_id_part, expires_part, signature = raw.split(":")
        telegram_id = int(telegram_id_part)
        expires_at = int(expires_part)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise AuthError("Token formati noto'g'ri") from exc

    expected = hmac.new(
        _SESSION_SECRET,
        f"{telegram_id}:{expires_at}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise AuthError("Token imzosi noto'g'ri")
    if expires_at < int(time.time()):
        raise AuthError("Sessiya muddati tugagan, Mini App'ni qaytadan oching")

    return telegram_id


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    """Datetime'ni ISO-8601 UTC satriga aylantiradi (masalan 2026-11-15T10:00:00Z)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def serialize_user(user: User) -> dict[str, Any]:
    """User modelini frontend uchun JSON lug'atga aylantiradi."""
    return {
        "telegram_id": user.telegram_id,
        "full_name": user.full_name,
        "username": user.username,
        "lat": user.lat,
        "lng": user.lng,
        "last_updated": iso_utc(user.last_updated),
        "is_ghost": bool(user.is_ghost),
        "avatar_url": user.avatar_url,
    }


def current_user(
    authorization: Annotated[Optional[str], Header()] = None,
    db: Session = Depends(get_db),
) -> User:
    """`Authorization: Bearer <token>` sarlavhasi orqali joriy foydalanuvchini topadi."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Avtorizatsiya talab qilinadi")

    token = authorization.split(" ", 1)[1].strip()
    try:
        telegram_id = parse_session_token(token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    user = get_user_by_telegram_id(db, telegram_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Foydalanuvchi bazada topilmadi, qaytadan /auth qiling")

    return user


class AuthRequest(BaseModel):
    """POST /auth tanasi: initData (yoki init_data) maydoni."""

    init_data: str = Field(min_length=1, max_length=8192)

    @model_validator(mode="before")
    @classmethod
    def accept_camel_case(cls, data: Any) -> Any:
        """Kelayotgan JSON'da `initData` bo'lsa, uni `init_data` ga ko'chiradi."""
        if isinstance(data, dict) and "init_data" not in data and "initData" in data:
            return {**data, "init_data": data["initData"]}
        return data


class LocationUpdate(BaseModel):
    """POST /update_location tanasi."""

    model_config = ConfigDict(allow_inf_nan=False)

    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class PingRequest(BaseModel):
    """POST /ping_user tanasi."""

    target_telegram_id: int = Field(gt=0, le=9_007_199_254_740_991)


async def _run_bot_polling() -> None:
    """Aiogram Long Polling'ni fon vazifasi sifatida yurgizadi."""
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except asyncio.CancelledError:
        logger.info("Bot polling to'xtatildi")
        raise
    except Exception:
        logger.exception("Bot polling kutilmagan xato bilan to'xtadi")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Bazani tayyorlaydi va botni FastAPI bilan birga ishga tushiradi."""
    init_db()
    polling_task = asyncio.create_task(_run_bot_polling(), name="genimap-bot-polling")
    app.state.polling_task = polling_task
    logger.info("GeniMap ishga tushdi — WebApp: %s", WEBAPP_URL)
    try:
        yield
    finally:
        polling_task.cancel()
        with suppress(asyncio.CancelledError):
            await polling_task
        with suppress(Exception):
            await bot.session.close()


app = FastAPI(
    title="GeniMap",
    description="Telegram Mini App orqali ishlaydigan ijtimoiy lokatsiya xizmati (MVP).",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Mini App frontendini qaytaradi."""
    if not INDEX_FILE.is_file():
        raise HTTPException(status_code=500, detail="templates/index.html topilmadi")
    return FileResponse(
        INDEX_FILE,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/auth")
def auth(payload: AuthRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Telegram initData'ni tekshiradi, foydalanuvchini bazaga yozadi va token beradi."""
    try:
        validated = validate_init_data(payload.init_data, BOT_TOKEN)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    tg_user = validated["user"]
    telegram_id = int(tg_user["id"])

    first_name = str(tg_user.get("first_name") or "").strip()
    last_name = str(tg_user.get("last_name") or "").strip()
    full_name = " ".join(part for part in (first_name, last_name) if part) or "GeniMap foydalanuvchisi"

    username_raw = tg_user.get("username")
    username = str(username_raw).strip() if username_raw else None
    if username:
        username = username.lstrip("@")[:64]

    avatar_raw = tg_user.get("photo_url")
    avatar_url = str(avatar_raw)[:512] if avatar_raw else None

    user = upsert_user(
        db,
        telegram_id=telegram_id,
        full_name=full_name[:255],
        username=username,
        avatar_url=avatar_url,
    )

    token, expires_at = create_session_token(telegram_id)
    logger.info("Avtorizatsiya: %s (@%s)", telegram_id, username or "username yo'q")

    return {
        "status": "ok",
        "token": token,
        "token_type": "Bearer",
        "expires_at": iso_utc(datetime.fromtimestamp(expires_at, tz=timezone.utc)),
        "user": serialize_user(user),
        "webapp_url": WEBAPP_URL,
    }


@app.post("/update_location")
def update_location(
    payload: LocationUpdate,
    user: Annotated[User, Depends(current_user)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Joriy foydalanuvchining lat/lng/last_updated qiymatlarini yangilaydi."""
    updated = update_user_location(db, user, payload.lat, payload.lng)
    if not updated:
        return {
            "status": "ignored",
            "reason": "ghost_mode",
            "message": "Sharpa rejimi yoqilgan — lokatsiya saqlanmadi",
        }

    return {
        "status": "ok",
        "lat": user.lat,
        "lng": user.lng,
        "last_updated": iso_utc(user.last_updated),
    }


@app.get("/get_users")
def get_users(
    user: Annotated[User, Depends(current_user)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Barcha foydalanuvchilarning lokatsiyasi va oxirgi onlayn vaqtini qaytaradi."""
    users = [serialize_user(item) for item in get_all_users(db)]
    return {
        "status": "ok",
        "server_time": iso_utc(utcnow()),
        "count": len(users),
        "users": users,
    }


@app.post("/ping_user")
async def ping_user(
    payload: PingRequest,
    user: Annotated[User, Depends(current_user)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Tanlangan foydalanuvchiga bot orqali "Qayerdasan?" chaqiruvini yuboradi."""
    if payload.target_telegram_id == user.telegram_id:
        raise HTTPException(status_code=400, detail="O'zingizga chaqiruv yubora olmaysiz")

    target = get_user_by_telegram_id(db, payload.target_telegram_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Foydalanuvchi topilmadi")

    delivered = await send_ping_message(
        target_telegram_id=target.telegram_id,
        sender_name=user.full_name,
        web_app_url=WEBAPP_URL,
    )

    if not delivered:
        return {
            "status": "undelivered",
            "delivered": False,
            "message": "Xabar yetkazilmadi — foydalanuvchi botni bloklagan bo'lishi mumkin",
        }

    return {"status": "ok", "delivered": True, "message": "Chaqiruv yuborildi"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        log_level="info",
    )
