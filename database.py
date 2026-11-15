"""GeniMap — ma'lumotlar bazasi qatlami (SQLAlchemy 2.x).

Asosiy rejim — Supabase PostgreSQL: `.env` faylidagi `DATABASE_URL` havolasi
orqali ulanadi. Havola bo'sh bo'lsa, lokal SQLite (`sqlite:///genimap.db`)
ishlatiladi. Modul engine'ni sozlaydi, `User` modelini e'lon qiladi va
foydalanuvchilarni o'qish/yozish uchun yordamchi funksiyalarni beradi.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    create_engine,
    false,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

# Modul alohida import qilinganda ham `.env` o'qilishi uchun.
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

DEFAULT_SQLITE_URL = "sqlite:///genimap.db"


def normalize_database_url(raw_url: str) -> str:
    """Supabase bergan havolani SQLAlchemy uchun tayyorlaydi.

    - `postgres://` sxemasini `postgresql://` ga almashtiradi;
    - PostgreSQL havolasiga `sslmode=require` qo'shadi (Supabase SSL talab qiladi);
    - bo'sh havola uchun lokal SQLite manzilini qaytaradi.
    """
    url = (raw_url or "").strip()
    if not url:
        return DEFAULT_SQLITE_URL

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    if url.startswith("postgresql://") or url.startswith("postgresql+"):
        if "sslmode=" not in url:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}sslmode=require"

    return url


# Bitta havola: Supabase → Project Settings → Database → Connection string (URI).
DATABASE_URL = normalize_database_url(
    os.getenv("DATABASE_URL") or os.getenv("GENIMAP_DATABASE_URL") or ""
)

IS_SQLITE = DATABASE_URL.startswith("sqlite")
# Supavisor transaction pooler (6543) prepared statement'larni qo'llab-quvvatlamaydi.
IS_POOLER = ":6543" in DATABASE_URL or "pgbouncer=true" in DATABASE_URL

_connect_args: dict[str, Any] = {}
if IS_SQLITE:
    _connect_args["check_same_thread"] = False
elif IS_POOLER and DATABASE_URL.startswith("postgresql+psycopg://"):
    _connect_args["prepare_threshold"] = None

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
    pool_recycle=-1 if IS_SQLITE else 1800,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
    future=True,
)


class Base(DeclarativeBase):
    """Barcha ORM modellari uchun asosiy sinf."""


class User(Base):
    """GeniMap foydalanuvchisi va uning oxirgi ma'lum lokatsiyasi."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lng: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    is_ghost: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    avatar_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - faqat debug uchun
        return f"<User telegram_id={self.telegram_id} full_name={self.full_name!r}>"


def init_db() -> None:
    """Jadvallarni (agar mavjud bo'lmasa) yaratadi."""
    Base.metadata.create_all(bind=engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Tranzaksiyali sessiya: xatolik bo'lsa rollback qiladi."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency sifatida ishlatiladigan sessiya generatori."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def utcnow() -> datetime:
    """Vaqt mintaqasi bilan jihozlangan hozirgi UTC vaqti."""
    return datetime.now(timezone.utc)


def get_user_by_telegram_id(session: Session, telegram_id: int) -> Optional[User]:
    """Telegram ID bo'yicha foydalanuvchini qaytaradi (topilmasa None)."""
    return session.scalar(select(User).where(User.telegram_id == telegram_id))


def upsert_user(
    session: Session,
    *,
    telegram_id: int,
    full_name: str,
    username: Optional[str] = None,
    avatar_url: Optional[str] = None,
) -> User:
    """Foydalanuvchini qo'shadi yoki mavjudini Telegram ma'lumotlari bilan yangilaydi."""
    user = get_user_by_telegram_id(session, telegram_id)
    if user is None:
        user = User(
            telegram_id=telegram_id,
            full_name=full_name,
            username=username,
            avatar_url=avatar_url,
            is_ghost=False,
            last_updated=utcnow(),
        )
        session.add(user)
    else:
        user.full_name = full_name or user.full_name
        user.username = username
        if avatar_url:
            user.avatar_url = avatar_url
    session.commit()
    session.refresh(user)
    return user


def update_user_location(session: Session, user: User, lat: float, lng: float) -> bool:
    """Lokatsiyani yangilaydi. Sharpa rejimida bo'lsa hech narsa yozmaydi.

    Qaytaradi: `True` — yangilandi, `False` — sharpa rejimi sababli o'tkazib yuborildi.
    """
    if user.is_ghost:
        return False
    user.lat = float(lat)
    user.lng = float(lng)
    user.last_updated = utcnow()
    session.commit()
    session.refresh(user)
    return True


def get_all_users(session: Session) -> list[User]:
    """Bazadagi barcha foydalanuvchilarni qaytaradi."""
    return list(session.scalars(select(User).order_by(User.id.asc())).all())
