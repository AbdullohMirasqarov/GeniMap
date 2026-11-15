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

from aiogram import Bot, Dispatcher, F, Router, html
from dotenv import load_dotenv
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from database import (
    accept_friend_request,
    are_friends,
    get_user_by_telegram_id,
    session_scope,
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
        f"👋 Salom, <b>{name}</b>! GeniMap'ga xush kelibsiz 🗺\n\n"
        "Bu yerda siz do'stlaringiz qayerdaligini real vaqtda ko'rishingiz mumkin. "
        "Xaritani oching va o'z joylashuvingizni ulashing!"
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
        f"👀 Do'stingiz <b>{safe_sender}</b> sizni xaritada qidiryapti!\n\n"
        "📍 Qayerdasiz? Xaritaga kirib joylashuvingizni yangilang."
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


async def send_friend_request(
    target_tg_id: int, sender_name: str, sender_tg_id: int
) -> bool:
    """Foydalanuvchiga do'stlik taklifini inline tugma bilan yuboradi.

    Qaytaradi: `True` — xabar yetkazildi, `False` — yetkazilmadi
    (foydalanuvchi botni bloklagan, chat topilmadi va h.k.).
    """
    safe_sender = html.quote(sender_name)
    text = (
        f"🔔 <b>{safe_sender}</b> siz bilan GeniMap'da do'stlashmoqchi!\n\n"
        "Do'stlashsangiz, xaritada bir-biringizni aniq ko'ra olasiz."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Qabul qilish",
                    callback_data=f"accept_friend_{int(sender_tg_id)}",
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
        logger.warning(
            "Do'stlik so'rovi yuborilmadi (chat_id=%s): %s", target_tg_id, exc
        )
        return False


@router.callback_query(F.data.startswith("accept_friend_"))
async def handle_accept_friend(callback: CallbackQuery) -> None:
    """`accept_friend_<sender_tg_id>` callback'ini qayta ishlaydi."""
    if callback.from_user is None or not callback.data:
        await callback.answer(
            "So'rovni qayta ishlab bo'lmadi. Iltimos, qaytadan urinib ko'ring.",
            show_alert=True,
        )
        return

    raw_sender_id = callback.data[len("accept_friend_") :]
    try:
        sender_tg_id = int(raw_sender_id)
    except (TypeError, ValueError):
        await callback.answer("So'rov ma'lumoti buzuq. Qaytadan urinib ko'ring.", show_alert=True)
        return

    recipient_tg_id = int(callback.from_user.id)
    if sender_tg_id == recipient_tg_id:
        await callback.answer("O'zingizni do'stga qo'shib bo'lmaydi 😅", show_alert=True)
        return

    with session_scope() as session:
        already_friends = are_friends(session, sender_tg_id, recipient_tg_id)
        accepted = already_friends or accept_friend_request(
            session, sender_tg_id, recipient_tg_id
        )
        sender = get_user_by_telegram_id(session, sender_tg_id)
        sender_name = sender.full_name if sender and sender.full_name else "Do'stingiz"

    if not accepted:
        await callback.answer(
            "So'rov topilmadi — u allaqachon qabul qilingan yoki bekor qilingan.",
            show_alert=True,
        )
        return

    safe_name = html.quote(sender_name)
    if isinstance(callback.message, Message):
        if already_friends:
            done_text = (
                f"😊 Siz allaqachon <b>{safe_name}</b> bilan do'stsiz.\n\n"
                "Xaritadan uni topib ko'ring 👇"
            )
        else:
            done_text = (
                f"🎉 Zo'r! Siz endi <b>{safe_name}</b> bilan do'stsiz. "
                "Xaritaga kirib uni toping!"
            )
        try:
            await callback.message.edit_text(
                done_text,
                reply_markup=_webapp_keyboard(),
            )
        except TelegramAPIError as exc:
            logger.info("Do'stlik xabarini tahrirlab bo'lmadi: %s", exc)

    await callback.answer()
