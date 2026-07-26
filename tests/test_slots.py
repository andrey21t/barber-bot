"""Tests for bot.services.slots."""

from datetime import date
from typing import Any

import pytest
from bot.models import Slot
from bot.services.slots import (
    add_slots,
    close_slot,
    get_available_slots,
)
from sqlalchemy.ext.asyncio import AsyncSession


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
