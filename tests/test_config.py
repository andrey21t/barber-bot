"""T3.1 — bot/config.py property branches coverage.

Covers:
- async_database_url: postgresql:// → postgresql+asyncpg:// (line 41)
- sync_database_url: DATABASE_URL_SYNC truthy → return as-is (line 54)
- sync_database_url: postgres:// prefix → postgresql+psycopg2:// (line 57)

These branches run only in prod (Render Postgres env). Tests instantiate
Settings with monkeypatched env to exercise each branch without a real DB.
"""

from __future__ import annotations

from bot.config import Settings


def _make_settings(**env_values: str) -> Settings:
    """Build Settings with explicit env overrides (BOT_TOKEN + ADMIN_ID
    are required, provide defaults for tests)."""
    base = {
        "BOT_TOKEN": "123:test",
        "ADMIN_ID": 1,
    }
    base.update(env_values)
    return Settings(**base)  # type: ignore[arg-type]


def test_async_database_url_postgresql_prefix_converted_to_asyncpg() -> None:
    """postgresql://... → postgresql+asyncpg://... (line 41)."""
    settings = _make_settings(DATABASE_URL="postgresql://user:pass@host:5432/db")
    assert settings.async_database_url == "postgresql+asyncpg://user:pass@host:5432/db"


def test_async_database_url_postgres_prefix_converted_to_asyncpg() -> None:
    """postgres://... → postgresql+asyncpg://... (line 41 second replace)."""
    settings = _make_settings(DATABASE_URL="postgres://user:pass@host:5432/db")
    assert settings.async_database_url == "postgresql+asyncpg://user:pass@host:5432/db"


def test_sync_database_url_explicit_sync_env_takes_priority() -> None:
    """DATABASE_URL_SYNC non-empty → returned as-is, ignoring DATABASE_URL
    prefix (line 54)."""
    settings = _make_settings(
        DATABASE_URL="postgresql://user:pass@host:5432/db",
        DATABASE_URL_SYNC="postgresql+psycopg2://custom:pass@other:5432/db_sync",
    )
    assert settings.sync_database_url == "postgresql+psycopg2://custom:pass@other:5432/db_sync"


def test_sync_database_url_postgres_prefix_converted_to_psycopg2() -> None:
    """postgres://... (no DATABASE_URL_SYNC) → postgresql+psycopg2://... (line 57)."""
    settings = _make_settings(DATABASE_URL="postgres://user:pass@host:5432/db")
    assert settings.sync_database_url == "postgresql+psycopg2://user:pass@host:5432/db"
