"""GeniMap — Aiogram 3.x bot mantig'i.

Bot FastAPI bilan bir xil event loop'da, Long Polling rejimida ishlaydi.
Kerakli muhit o'zgaruvchilari loyiha papkasidagi `.env` faylidan o'qiladi:
    BOT_TOKEN  — @BotFather bergan token
    WEBAPP_URL — Mini App'ning HTTPS manzili (masalan https://genimap.example.uz)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, Router, html
from dotenv import load_dotenv
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

logger = logging.getLogger("genimap.bot")

# Maxfiy qiymatlar `.env` faylidan o'qiladi. Allaqachon o'rnatilgan muhit
# o'zgaruvchilari ustuvor qoladi (override=False).
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)


def _require_env(name: str) -> str:
    """Majburiy muhit o'zgaruvchisini o'qiydi, bo'lmasa tushunarli xato beradi."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} muhit o'zgaruvchisi o'rnatilmagan. "
            f"Uni loyiha papkasidagi .env fayliga yozing (namuna: .env.example)."
        )
    return value


BOT_TOKEN = _require_env("BOT_TOKEN")
WEBAPP_URL = _require_env("WEBAPP_URL").rstrip("/")

if not WEBAPP_URL.startswith("https://"):
    raise RuntimeError(
        "WEBAPP_URL https:// bilan boshlanishi shart — Telegram Mini App faqat HTTPS manzillarni qabul qiladi."
    )

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()
router = Router(name="genimap")
dp.include_router(router)


def _webapp_keyboard() -> InlineKeyboardMarkup:
    """Xaritani ochuvchi inline tugma."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Xaritani ochish 🗺",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ]
        ]
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """/start — chiroyli xush kelibsiz xabari va Mini App tugmasi."""
    if message.from_user is None:
        return

    name = html.quote(message.from_user.full_name)
    text = (
        f"👋 Salom, <b>{name}</b>!\n\n"
        "<b>GeniMap</b> — do'stlaringiz hozir qayerdaligini bitta xaritada ko'rish "
        "uchun ijtimoiy lokatsiya ilovasi.\n\n"
        "🗺 Xaritada siz:\n"
        "• joylashuvingizni do'stlaringiz bilan ulashasiz;\n"
        "• kim onlayn ekanini bir qarashda bilasiz;\n"
        "• kerak bo'lganda do'stingizga “Qayerdasan?” deb chaqiruv yuborasiz.\n\n"
        "Boshlash uchun pastdagi tugmani bosing 👇"
    )

    await message.answer(
        text,
        reply_markup=_webapp_keyboard(),
        disable_web_page_preview=True,
    )


async def send_ping_message(target_tg_id: int, sender_name: str, web_app_url: str) -> bool:
    """Foydalanuvchiga "ping" (chaqiruv) xabarini yuboradi.

    Qaytaradi: `True` — xabar yetkazildi, `False` — yetkazilmadi
    (foydalanuvchi botni bloklagan, chat topilmadi va h.k.).
    """
    safe_sender = html.quote(sender_name)
    text = (
        f"📣 <b>{safe_sender}</b> sizni chaqirmoqda!\n\n"
        "Qayerdasan? Xaritani ochib joylashuvingizni ko'rsating 👇"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Xaritani ochish 🗺",
                    web_app=WebAppInfo(url=web_app_url),
                )
            ]
        ]
    )

    try:
        await bot.send_message(
            chat_id=target_tg_id,
            text=text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return True
    except TelegramAPIError as exc:
        logger.warning("Ping yuborilmadi (chat_id=%s): %s", target_tg_id, exc)
        return False
