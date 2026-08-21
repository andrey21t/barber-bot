"""Tests for bot.services.slots."""

import asyncio
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest
from bot.models import Business, Master, Slot
from bot.services.slots import (
    SlotAlreadyExistsError,
    add_slots,
    close_slot,
    get_available_slots,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_add_slots_creates_new(session: AsyncSession, tomorrow_date: date) -> None:
    """Happy path — add 3 new slots."""
    from uuid import uuid4

    master_id = uuid4()
    slots = await add_slots(session, master_id, tomorrow_date, [11, 12, 13])

    assert len(slots) == 3
    assert {s.slot_hour for s in slots} == {11, 12, 13}
    assert all(s.status == "open" for s in slots)


@pytest.mark.asyncio
async def test_add_slots_skips_existing(session: AsyncSession, tomorrow_date: date) -> None:
    """Idempotent — calling twice with same hours skips existing."""
    from uuid import uuid4

    master_id = uuid4()
    await add_slots(session, master_id, tomorrow_date, [11, 12])
    slots = await add_slots(session, master_id, tomorrow_date, [11, 12, 13])

    assert len(slots) == 1
    assert slots[0].slot_hour == 13


@pytest.mark.asyncio
async def test_close_slot_happy(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """close_slot updates status from open to closed."""
    slot_id = seed_data["slot"].id
    updated = await close_slot(session, slot_id)
    assert updated is True

    # Verify in DB
    from sqlalchemy import select

    stmt = select(Slot.status).where(Slot.id == slot_id)
    res = await session.execute(stmt)
    assert res.scalar_one() == "closed"


@pytest.mark.asyncio
async def test_close_slot_idempotent_if_closed(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """close_slot on already-closed slot is idempotent (returns True)."""
    slot_id = seed_data["slot"].id
    await close_slot(session, slot_id)
    updated = await close_slot(session, slot_id)
    assert updated is True


@pytest.mark.asyncio
async def test_close_slot_refuses_if_booked(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """close_slot raises ValueError if slot already has a booking."""
    slot = seed_data["slot"]
    slot.status = "booked"
    await session.commit()

    with pytest.raises(ValueError, match="already has a booking"):
        await close_slot(session, slot.id)


@pytest.mark.asyncio
async def test_close_slot_not_found(session: AsyncSession) -> None:
    """close_slot returns False if slot doesn't exist."""
    from uuid import uuid4

    updated = await close_slot(session, uuid4())
    assert updated is False


@pytest.mark.asyncio
async def test_get_available_filters_open_only(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """get_available_slots returns only status='open' slots (including seed slot)."""
    master_id = seed_data["master_id"]
    slot_date = seed_data["slot_date"]

    # Add 3 more slots: 1 open, 1 closed, 1 booked (seed already has 1 open at 14:00)
    s1 = Slot(master_id=master_id, slot_date=slot_date, slot_hour=10, status="open")
    s2 = Slot(master_id=master_id, slot_date=slot_date, slot_hour=11, status="closed")
    s3 = Slot(master_id=master_id, slot_date=slot_date, slot_hour=12, status="booked")
    session.add_all([s1, s2, s3])
    await session.commit()

    available = await get_available_slots(session, master_id, slot_date)
    # 2 open: seed slot (14:00) + new s1 (10:00)
    assert len(available) == 2
    open_hours = {s.slot_hour for s in available}
    assert open_hours == {10, 14}


@pytest.mark.asyncio
async def test_add_slots_invalid_hour(session: AsyncSession, tomorrow_date: date) -> None:
    """add_slots raises ValueError for hours outside 0-23."""
    from uuid import uuid4

    with pytest.raises(ValueError, match="0-23"):
        await add_slots(session, uuid4(), tomorrow_date, [25])


@pytest.mark.asyncio
async def test_add_slots_empty_hours_raises_value_error(
    session: AsyncSession,
    tomorrow_date: date,
) -> None:
    """add_slots with empty hours list raises ValueError.

    Covers bot/services/slots.py:26 — `if not hours: raise ValueError(...)`.
    Pre-check happens before `min(hours)`/`max(hours)` (which would raise
    a different ValueError on empty sequence).
    """
    with pytest.raises(ValueError, match="at least one hour required"):
        await add_slots(session, uuid4(), tomorrow_date, [])


@pytest.mark.asyncio
async def test_add_slots_concurrent_race_raises_slot_exists(
    session_factory_concurrent: async_sessionmaker[AsyncSession],
) -> None:
    """Concurrent add_slots with same (master_id, slot_date, slot_hour) —
    loser catches IntegrityError and raises SlotAlreadyExistsError.

    Covers bot/services/slots.py:48-51 — IntegrityError catch + rollback +
    re-raise as SlotAlreadyExistsError. The composite unique constraint
    `ux_slots_master_date_hour` rejects the loser's INSERT at commit time.

    Uses asyncio.gather: both coroutines start, both SELECT existing (none
    committed yet), both INSERT. SQLite file-based serializes writes — the
    loser's flush() blocks on the writer lock (default sqlite3 timeout 5.0s,
    passed through aiosqlite), waits for the winner's COMMIT to release the
    lock, then sees the now-visible unique constraint and raises
    IntegrityError. add_slots catches it (slots.py:48) and re-raises as
    SlotAlreadyExistsError.

    Manual orchestration (like test_booking.py:1082) is overkill here —
    no WHERE-clause pin to verify, just unique constraint enforcement.
    asyncio.gather is sufficient and simpler.

    Flakiness risk (LOW): if asyncio.gather runs coroutine A to full
    completion (SELECT+INSERT+COMMIT) before B's SELECT starts, B sees A's
    committed slot and takes the idempotent path (slots.py:39-40), returning
    [] — `len(success) == 2` would fail the test. In practice both SELECTs
    fire before either flush, but a heavily loaded CI could trigger this.
    If observed, switch to manual orchestration (s_a INSERT without commit,
    s_b add_slots inside `async with s_a`).
    """
    # Seed business + master (no slots yet) through a dedicated session
    async with session_factory_concurrent() as s_seed:
        biz = Business(
            name="Race Barbershop",
            telegram_owner_id=461355056,
            timezone="Europe/Moscow",
        )
        s_seed.add(biz)
        await s_seed.flush()
        master = Master(
            business_id=biz.id,
            name="Тестер",
            telegram_id=461355056,
            role="owner",
        )
        s_seed.add(master)
        await s_seed.commit()
        master_id = master.id

    slot_date = datetime.now(UTC).date()

    # Two concurrent sessions, same (master_id, slot_date, hour=14)
    async with (
        session_factory_concurrent() as s_a,
        session_factory_concurrent() as s_b,
    ):
        results = await asyncio.gather(
            add_slots(s_a, master_id, slot_date, [14]),
            add_slots(s_b, master_id, slot_date, [14]),
            return_exceptions=True,
        )

    success = [r for r in results if not isinstance(r, Exception)]
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(success) == 1, f"expected 1 winner, got {len(success)}: {results!r}"
    assert len(errors) == 1, f"expected 1 loser, got {len(errors)}: {results!r}"
    assert isinstance(errors[0], SlotAlreadyExistsError), (
        f"expected SlotAlreadyExistsError, got {type(errors[0]).__name__}: {errors[0]!r}"
    )

    # Verify exactly one slot was committed to DB
    async with session_factory_concurrent() as s_verify:
        from sqlalchemy import select

        stmt = select(Slot).where(
            Slot.master_id == master_id,
            Slot.slot_date == slot_date,
            Slot.slot_hour == 14,
        )
        slots = (await s_verify.execute(stmt)).scalars().all()
        assert len(slots) == 1, f"expected 1 slot in DB, got {len(slots)}"
