"""Alembic muhiti.

Havola va metadata `database.py`dan olinadi, shuning uchun `.env`dagi
`DATABASE_URL` (Supabase PostgreSQL) yoki lokal SQLite avtomatik ishlatiladi.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Loyiha ildizini sys.path'ga qo'shamiz — alembic boshqa papkadan chaqirilsa ham
# `database` moduli topilishi uchun.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database import DATABASE_URL, Base  # noqa: E402

config = context.config

# Alembic o'z log konfiguratsiyasini qo'llaydi.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `alembic.ini`dagi bo'sh `sqlalchemy.url` o'rniga haqiqiy havola.
# `%` belgisi ConfigParser uchun escape qilinadi (URL-encoded parollar uchun).
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))

target_metadata = Base.metadata

# SQLite'da ALTER TABLE cheklangan — batch rejimi jadvalni qayta quradi.
IS_SQLITE = DATABASE_URL.startswith("sqlite")


def run_migrations_offline() -> None:
    """`--sql` rejimi: migratsiyalarni bajarish o'rniga SQL sifatida chiqaradi."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=IS_SQLITE,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Bazaga ulanib migratsiyalarni bajaradi."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=IS_SQLITE,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
