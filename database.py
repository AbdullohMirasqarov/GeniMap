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
    UniqueConstraint,
    and_,
    create_engine,
    false,
    inspect,
    or_,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

# Modul alohida import qilinganda ham `.env` o'qilishi uchun.
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

DEFAULT_SQLITE_URL = "sqlite:///genimap.db"

# Maxfiylik (visibility) qiymatlari.
VISIBILITY_ALL = "all"
VISIBILITY_FRIENDS = "friends"
VISIBILITY_NONE = "none"
VISIBILITY_VALUES = (VISIBILITY_ALL, VISIBILITY_FRIENDS, VISIBILITY_NONE)

# Do'stlik holatlari.
FRIENDSHIP_PENDING = "pending"
FRIENDSHIP_ACCEPTED = "accepted"


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
    visibility: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=VISIBILITY_ALL,
        server_default=VISIBILITY_ALL,
    )

    def __repr__(self) -> str:  # pragma: no cover - faqat debug uchun
        return f"<User telegram_id={self.telegram_id} full_name={self.full_name!r}>"


class Friendship(Base):
    """Ikki foydalanuvchi o'rtasidagi do'stlik aloqasi.

    `user_id_1` — so'rov yuborgan, `user_id_2` — qabul qiluvchi Telegram ID.
    `status`: `pending` (so'rov yuborilgan) yoki `accepted` (do'stlar).
    """

    __tablename__ = "friendships"
    __table_args__ = (
        UniqueConstraint("user_id_1", "user_id_2", name="uq_friendships_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id_1: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    user_id_2: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=FRIENDSHIP_PENDING,
        server_default=FRIENDSHIP_PENDING,
    )

    def __repr__(self) -> str:  # pragma: no cover - faqat debug uchun
        return (
            f"<Friendship {self.user_id_1}->{self.user_id_2} "
            f"status={self.status!r}>"
        )


def _ensure_users_visibility_column() -> None:
    """Eski bazadagi `users` jadvaliga `visibility` ustunini qo'shadi.

    `create_all()` mavjud jadvalga ustun qo'shmaydi, shu sababli avval
    ishga tushirilgan bazada ustun yo'q bo'lsa, ALTER TABLE bajariladi.
    """
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("users")}
    if "visibility" in columns:
        return

    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE users ADD COLUMN visibility VARCHAR(16) "
                "NOT NULL DEFAULT 'all'"
            )
        )


def init_db() -> None:
    """Jadvallarni (agar mavjud bo'lmasa) yaratadi va sxemani yangilaydi."""
    Base.metadata.create_all(bind=engine)
    _ensure_users_visibility_column()


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


def _friendship_pair(first_tg_id: int, second_tg_id: int):
    """Ikki foydalanuvchi orasidagi aloqani ikki yo'nalishda ham topuvchi shart."""
    return or_(
        and_(
            Friendship.user_id_1 == first_tg_id,
            Friendship.user_id_2 == second_tg_id,
        ),
        and_(
            Friendship.user_id_1 == second_tg_id,
            Friendship.user_id_2 == first_tg_id,
        ),
    )


def get_friendship(
    session: Session, first_tg_id: int, second_tg_id: int
) -> Optional[Friendship]:
    """Ikki foydalanuvchi orasidagi do'stlik yozuvini qaytaradi (bo'lmasa None)."""
    return session.scalar(
        select(Friendship)
        .where(_friendship_pair(first_tg_id, second_tg_id))
        .order_by(Friendship.id.asc())
    )


def create_friend_request(
    session: Session, sender_tg_id: int, target_tg_id: int
) -> Friendship:
    """Do'stlik so'rovini `pending` holatida yozadi (yoki mavjudini yangilaydi).

    - so'rov mavjud bo'lmasa — yangi `pending` yozuv;
    - aloqa allaqachon `accepted` bo'lsa — o'zgartirilmaydi;
    - qarshi tomondan `pending` so'rov bo'lsa — o'zaro qabul hisoblanadi;
    - aks holda eski `pending` yozuv qayta yuborilgan hisoblanadi.
    """
    friendship = get_friendship(session, sender_tg_id, target_tg_id)

    if friendship is None:
        friendship = Friendship(
            user_id_1=int(sender_tg_id),
            user_id_2=int(target_tg_id),
            status=FRIENDSHIP_PENDING,
        )
        session.add(friendship)
    elif friendship.status == FRIENDSHIP_ACCEPTED:
        return friendship
    elif friendship.user_id_2 == int(sender_tg_id):
        # Nishondagi foydalanuvchi avval so'rov yuborgan — o'zaro do'stlik.
        friendship.status = FRIENDSHIP_ACCEPTED
    else:
        friendship.status = FRIENDSHIP_PENDING

    session.commit()
    session.refresh(friendship)
    return friendship


def accept_friend_request(
    session: Session, sender_tg_id: int, recipient_tg_id: int
) -> bool:
    """`sender_tg_id` → `recipient_tg_id` so'rovini qabul qiladi.

    Qaytaradi: `True` — so'rov topildi va qabul qilindi, `False` — so'rov yo'q.
    """
    friendship = session.scalar(
        select(Friendship).where(
            Friendship.user_id_1 == int(sender_tg_id),
            Friendship.user_id_2 == int(recipient_tg_id),
        )
    )
    if friendship is None:
        return False

    friendship.status = FRIENDSHIP_ACCEPTED
    session.commit()
    return True


def are_friends(session: Session, first_tg_id: int, second_tg_id: int) -> bool:
    """Foydalanuvchilar do'st (status='accepted') ekanini tekshiradi."""
    friendship = get_friendship(session, first_tg_id, second_tg_id)
    return friendship is not None and friendship.status == FRIENDSHIP_ACCEPTED


def get_friend_ids(session: Session, telegram_id: int) -> set[int]:
    """Foydalanuvchining qabul qilingan do'stlari Telegram ID'lari to'plami."""
    rows = session.scalars(
        select(Friendship).where(
            Friendship.status == FRIENDSHIP_ACCEPTED,
            or_(
                Friendship.user_id_1 == int(telegram_id),
                Friendship.user_id_2 == int(telegram_id),
            ),
        )
    ).all()

    friend_ids: set[int] = set()
    for row in rows:
        if row.user_id_1 == int(telegram_id):
            friend_ids.add(row.user_id_2)
        else:
            friend_ids.add(row.user_id_1)
    return friend_ids


def get_friends(session: Session, telegram_id: int) -> list[User]:
    """Qabul qilingan do'stlarni ism bo'yicha tartiblab qaytaradi."""
    friend_ids = get_friend_ids(session, telegram_id)
    if not friend_ids:
        return []

    return list(
        session.scalars(
            select(User)
            .where(User.telegram_id.in_(friend_ids))
            .order_by(User.full_name.asc())
        ).all()
    )


def get_incoming_requests(session: Session, telegram_id: int) -> list[User]:
    """Joriy foydalanuvchiga kelgan, hali qabul qilinmagan so'rovlarni qaytaradi.

    Faqat `pending` holatdagi va `user_id_2` (qabul qiluvchi) joriy
    foydalanuvchi bo'lgan so'rovlar olinadi. Tartib — ism bo'yicha.
    """
    rows = session.scalars(
        select(User)
        .join(Friendship, Friendship.user_id_1 == User.telegram_id)
        .where(
            Friendship.user_id_2 == int(telegram_id),
            Friendship.status == FRIENDSHIP_PENDING,
        )
        .order_by(User.full_name.asc())
    ).all()
    return list(rows)


def decline_friend_request(
    session: Session, sender_tg_id: int, recipient_tg_id: int
) -> bool:
    """`sender_tg_id` → `recipient_tg_id` so'rovini o'chiradi (rad etish).

    Qaytaradi: `True` — so'rov o'chirildi, `False` — `pending` so'rov yo'q.
    """
    friendship = session.scalar(
        select(Friendship).where(
            Friendship.user_id_1 == int(sender_tg_id),
            Friendship.user_id_2 == int(recipient_tg_id),
            Friendship.status == FRIENDSHIP_PENDING,
        )
    )
    if friendship is None:
        return False

    session.delete(friendship)
    session.commit()
    return True


def set_user_visibility(session: Session, user: User, visibility: str) -> User:
    """Foydalanuvchining maxfiylik sozlamasini yangilaydi.

    Noto'g'ri qiymat uchun `ValueError` ko'tariladi.
    """
    if visibility not in VISIBILITY_VALUES:
        raise ValueError(f"visibility qiymati noto'g'ri: {visibility!r}")

    user.visibility = visibility
    session.commit()
    session.refresh(user)
    return user


def _may_view(viewer: User, target: User, friend_ids: set[int]) -> bool:
    """Bitta yo'nalishdagi ruxsatni tekshiradi.

    `all` — hammaga ochiq; `friends` — faqat do'stlarga ochiq; `none` — yopiq.
    """
    if target.visibility == VISIBILITY_NONE:
        return False
    if target.visibility == VISIBILITY_ALL:
        return True
    if target.visibility == VISIBILITY_FRIENDS:
        return target.telegram_id in friend_ids
    return False


def get_visible_users(session: Session, viewer: User) -> list[User]:
    """Simmetriya qoidasi asosida ko'rinadigan foydalanuvchilarni qaytaradi.

    - `viewer.visibility == 'none'` bo'lsa — bo'sh ro'yxat (u hech kimni ko'rmaydi);
    - har bir nishon uchun ikki shart bajarilishi kerak:
      viewer nishonni ko'ra olsin VA nishon viewerga ruxsat bergan bo'lsin;
    - o'zi ro'yxatda qoladi (frontend uni alohida marker sifatida chizadi).
    """
    if viewer.visibility == VISIBILITY_NONE:
        return []

    friend_ids = get_friend_ids(session, viewer.telegram_id)
    visible: list[User] = []

    for target in get_all_users(session):
        if target.telegram_id == viewer.telegram_id:
            visible.append(target)
            continue

        target_is_friend = target.telegram_id in friend_ids
        viewer_can_see = viewer.visibility == VISIBILITY_ALL or (
            viewer.visibility == VISIBILITY_FRIENDS and target_is_friend
        )
        target_allows = _may_view(viewer, target, friend_ids)

        if viewer_can_see and target_allows:
            visible.append(target)

    return visible
