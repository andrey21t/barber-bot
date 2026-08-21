"""Test fixtures — in-memory SQLite async engine, async session, test data factories.

Per deep-analysis-critic verdict: aiogram.tests НЕ существует в aiogram 3.30.
Handler tests use unittest.mock.AsyncMock(Bot) + dp.feed_update(bot, update).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Ensure env is set before importing bot.config (Settings reads .env on import)
os.environ.setdefault("BOT_TOKEN", "test:TOKEN")
os.environ.setdefault("ADMIN_ID", "461355056")

from bot.db import Base  # noqa: E402
from bot.models import Business, Client, Master, Slot  # noqa: E402


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """In-memory SQLite async engine. Schema created per-test."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture
async def engine_concurrent(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """File-based SQLite engine for concurrent-race tests.

    In-memory SQLite + default QueuePool gives each new connection its own DB
    (sessions can't share state). File-based SQLite supports multiple real
    connections to the same file — each session has its own connection, and
    writes serialize via SQLite's database-level lock. After a writer commits,
    new statements on other connections see the committed state.

    Used by test_transfer_booking_concurrent_race_runtime to faithfully test
    the WHERE-clause pin (Booking.start_at ==) at runtime, replacing the
    static-invariant test (inspect.getsource). Closes Pass 3 [blocker] finding.
    """
    db_file = tmp_path / "test_concurrent.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_file}", future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory_concurrent(
    engine_concurrent: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine_concurrent, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Single async session for the test."""
    async with session_factory() as s:
        yield s


@pytest_asyncio.fixture
async def seed_data(
    session: AsyncSession,
) -> dict[str, Any]:
    """Seed one business, one master, one client, one slot — minimal context for booking tests."""
    biz = Business(name="Test Barbershop", telegram_owner_id=461355056, timezone="Europe/Moscow")
    session.add(biz)
    await session.flush()  # populate biz.id

    master = Master(business_id=biz.id, name="Екатерина", telegram_id=461355056, role="owner")
    session.add(master)
    await session.flush()  # populate master.id

    client = Client(telegram_id=111222333, name="Паша")
    session.add(client)
    await session.flush()  # populate client.id

    # Tomorrow at 14:00 (LOCAL Moscow time)
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    slot = Slot(
        master_id=master.id,  # now master.id is populated
        slot_date=tomorrow,
        slot_hour=14,
        status="open",
    )
    session.add(slot)
    await session.commit()

    return {
        "business": biz,
        "master": master,
        "client": client,
        "slot": slot,
        "business_id": biz.id,
        "master_id": master.id,
        "client_telegram_id": 111222333,
        "slot_date": tomorrow,
    }


@pytest.fixture
def mock_bot() -> AsyncMock:
    """Mock aiogram Bot — AsyncMock for all methods."""
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    return bot


@pytest.fixture
def today_date() -> date:
    return datetime.now(UTC).date()


@pytest.fixture
def tomorrow_date(today_date: date) -> date:
    return today_date + timedelta(days=1)
