"""Tests for bot.db — create_all, drop_all, dispose, utcnow utilities.

Covers bot/db.py:22-23, 28-29, 33, 43 (6 miss lines, was 70% → 100%).
These are dev/test utilities — not used in production (alembic for prod
migrations). Coverage for completeness, not for runtime safety.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import bot.db as db_module
import pytest
from bot.db import create_all, dispose, drop_all, utcnow
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


async def _get_table_names(engine: AsyncEngine) -> list[str]:
    """Get table names from async engine via run_sync (greenlet-safe)."""
    async with engine.begin() as conn:
        return await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())


@pytest.mark.asyncio
async def test_create_all_creates_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_all() creates all tables registered on Base.metadata.

    Covers bot/db.py:22-23 — `async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)`.

    Uses in-memory SQLite engine monkeypatched onto bot.db.engine to avoid
    touching the real dev database. After create_all(), inspect the engine
    via run_sync to confirm Booking/Slot/Master/Business tables exist.
    """
    test_engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    monkeypatch.setattr(db_module, "engine", test_engine)

    try:
        await create_all()

        table_names = await _get_table_names(test_engine)
        # Sanity: core tables from bot.models registered on Base.metadata
        assert "bookings" in table_names, f"bookings not in {table_names}"
        assert "slots" in table_names, f"slots not in {table_names}"
        assert "masters" in table_names, f"masters not in {table_names}"
        assert "businesses" in table_names, f"businesses not in {table_names}"
    finally:
        await test_engine.dispose()


@pytest.mark.asyncio
async def test_drop_all_drops_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """drop_all() removes all tables from the database.

    Covers bot/db.py:28-29 — `async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.drop_all)`.

    Sequence: create_all() → assert tables exist → drop_all() → assert
    no tables remain. Same monkeypatch pattern as test_create_all.
    """
    test_engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    monkeypatch.setattr(db_module, "engine", test_engine)

    try:
        await create_all()
        tables_before = await _get_table_names(test_engine)
        assert len(tables_before) > 0, "create_all should have created tables"

        await drop_all()
        tables_after = await _get_table_names(test_engine)
        assert tables_after == [], f"expected no tables after drop_all, got {tables_after}"
    finally:
        await test_engine.dispose()


@pytest.mark.asyncio
async def test_dispose_calls_engine_dispose(monkeypatch: pytest.MonkeyPatch) -> None:
    """dispose() forwards to engine.dispose().

    Covers bot/db.py:33 — `await engine.dispose()`.

    Uses AsyncMock on engine.dispose to verify the call without actually
    disposing a real engine (no need to test SQLAlchemy internals).
    """
    mock_engine = AsyncMock()
    monkeypatch.setattr(db_module, "engine", mock_engine)

    await dispose()

    mock_engine.dispose.assert_awaited_once()


def test_utcnow_returns_aware_datetime_in_utc() -> None:
    """utcnow() returns timezone-aware datetime in UTC.

    Covers bot/db.py:43 — `return datetime.now(UTC)`.

    Replaces deprecated datetime.utcnow() (PEP 587, Python 3.12) — must
    return aware datetime, not naive. Sanity: result is close to now.
    """
    before = datetime.now(UTC)
    result = utcnow()
    after = datetime.now(UTC)

    assert isinstance(result, datetime)
    assert result.tzinfo is not None, "utcnow() must return aware datetime, not naive"
    assert result.tzinfo == UTC, f"expected UTC tzinfo, got {result.tzinfo}"
    # Monotonic sanity: result is within [before, after] window
    assert before <= result <= after, f"result {result} outside [{before}, {after}]"
