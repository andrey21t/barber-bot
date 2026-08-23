"""Alembic environment — converts async DATABASE_URL → sync URL for migrations.

Cross-DB support:
- prod: DATABASE_URL=postgresql+asyncpg://... → postgresql+psycopg2://...
- prod: DATABASE_URL=postgresql://... (Render fromDatabase) → postgresql+psycopg2://...
- prod: DATABASE_URL=postgres://... (Render legacy) → postgresql+psycopg2://...
- dev:  DATABASE_URL=sqlite+aiosqlite:///./barber.db → sqlite:///./barber.db
- fallback: DATABASE_URL_SYNC env var (sync engine for SQLAlchemyJobStore, may differ)

target_metadata = Base.metadata (auto-detects models via bot.models import).

Lazy imports (Session 3 change): `from bot import models` and `from bot.db import Base`
are deferred INSIDE _get_target_metadata() (called from run_migrations_online/offline).
Reason: at module-level import time, bot.models → bot.db → bot.config →
Settings(BOT_TOKEN, ADMIN_ID) would raise ValidationError if env not loaded.
Lazy import defers this to function-call time so the alembic config block (env.py
lines 32-49: sync_url resolution from DATABASE_URL) runs first.

NOTE: This does NOT make alembic work with ONLY DATABASE_URL — Settings() is still
triggered when _get_target_metadata() runs (in run_migrations_online/offline).
If BOT_TOKEN/ADMIN_ID are not set, alembic WILL fail at that point. This is
acceptable for the render.yaml preDeployCommand workflow (envVars including
BOT_TOKEN/ADMIN_ID are available at preDeploy time on Render). If you need to run
alembic in an environment without BOT_TOKEN/ADMIN_ID, consider making those
fields Optional in Settings or using a separate alembic-only config.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure bot package is importable (prepend_sys_path = . in alembic.ini handles
# the project root, but be explicit for robustness).
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

# Alembic config object.
config = context.config

# Resolve sync DB URL from env (priority: DATABASE_URL_SYNC, then convert async DATABASE_URL).
sync_url = os.getenv("DATABASE_URL_SYNC", "")
if not sync_url:
    async_url = os.getenv("DATABASE_URL", "sqlite:///./barber.db")
    # Handle ALL Render formats: postgresql+asyncpg://, postgresql://, postgres://
    for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
        if async_url.startswith(prefix):
            sync_url = async_url.replace(prefix, "postgresql+psycopg2://", 1)
            break
    else:
        sync_url = async_url.replace("sqlite+aiosqlite://", "sqlite://", 1)
config.set_main_option("sqlalchemy.url", sync_url)

# Logging from alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _get_target_metadata() -> Any:
    """Lazy import bot.models + bot.db.Base → return Base.metadata.

    Deferred until called (run_migrations_online/offline) so Settings() is
    NOT triggered at alembic module import time. Without this, CI/pre-deploy
    fails: Settings requires BOT_TOKEN + ADMIN_ID, alembic only has DATABASE_URL.
    """
    from bot import models  # noqa: F401 — register models with Base.metadata
    from bot.db import Base

    return Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL script without DB connection (offline mode)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=_get_target_metadata(),
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
        context.configure(connection=connection, target_metadata=_get_target_metadata())
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
