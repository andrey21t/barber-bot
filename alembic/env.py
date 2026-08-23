"""Alembic environment — converts async DATABASE_URL → sync URL for migrations.

Cross-DB support:
- prod: DATABASE_URL=postgresql+asyncpg://... → postgresql+psycopg2://...
- dev:  DATABASE_URL=sqlite+aiosqlite:///./barber.db → sqlite:///./barber.db
- fallback: DATABASE_URL_SYNC env var (sync engine for SQLAlchemyJobStore, may differ)

target_metadata = Base.metadata (auto-detects models via bot.models import).
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure bot package is importable (prepend_sys_path = . in alembic.ini handles
# the project root, but be explicit for robustness).
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from bot import models  # noqa: E402, F401 — register models with Base.metadata
from bot.db import Base  # noqa: E402

# Alembic config object.
config = context.config

# Resolve sync DB URL from env (priority: DATABASE_URL_SYNC, then convert async DATABASE_URL).
sync_url = os.getenv("DATABASE_URL_SYNC", "")
if not sync_url:
    async_url = os.getenv("DATABASE_URL", "sqlite:///./barber.db")
    # asyncpg → psycopg2 (prod Postgres migrations)
    sync_url = async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    # aiosqlite → sqlite (dev SQLite migrations)
    sync_url = sync_url.replace("sqlite+aiosqlite://", "sqlite://")
config.set_main_option("sqlalchemy.url", sync_url)

# Logging from alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL script without DB connection (offline mode)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against live DB (online mode)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
