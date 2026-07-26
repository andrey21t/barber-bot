"""Slot operations — Pure service layer (no Telegram API)."""

from datetime import date
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Slot


async def add_slots(
    session: AsyncSession,
    master_id: UUID,
    slot_date: date,
    hours: list[int],
) -> list[Slot]:
    """Open slots on a date for master. Skip already-existing (idempotent).

    Returns list of newly created slots (existing ones skipped).
    """
    if not 0 <= min(hours, default=0) <= 23 or not 0 <= max(hours, default=0) <= 23:
        raise ValueError("slot_hour must be 0-23")

    # Get already-open slots on this date for this master
    existing_stmt = select(Slot).where(
        Slot.master_id == master_id, Slot.slot_date == slot_date
    )
    existing = await session.execute(existing_stmt)
    existing_hours = {s.slot_hour for s in existing.scalars().all()}

    new_slots: list[Slot] = []
    for hour in hours:
        if hour in existing_hours:
            continue
        slot = Slot(master_id=master_id, slot_date=slot_date, slot_hour=hour, status="open")
        session.add(slot)
        new_slots.append(slot)

    if new_slots:
        try:
            await session.flush()
        except IntegrityError as exc:
            # Composite unique (master_id, slot_date, slot_hour) violated by concurrent insert
            await session.rollback()
            raise SlotAlreadyExistsError(
                f"Slot {master_id}/{slot_date}/{hour} already exists"
            ) from exc
    await session.commit()
    return new_slots


class SlotAlreadyExistsError(Exception):
    """Raised when slot (master_id, slot_date, slot_hour) already exists."""


async def close_slot(session: AsyncSession, slot_id: UUID) -> bool:
    """Close a slot (set status='closed'). Refuse if already booked.

    Returns True if updated, False if slot not found.
    Raises ValueError if slot is already booked.
    """
    # First check status
    stmt = select(Slot.status).where(Slot.id == slot_id)
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        return False
    current_status = row[0]
    if current_status == "booked":
        raise ValueError(f"Slot {slot_id} already has a booking — cannot close")
    if current_status == "closed":
        return True  # Idempotent

    upd = update(Slot).where(Slot.id == slot_id, Slot.status == "open").values(status="closed")
    res = await session.execute(upd)
    await session.commit()
    return cast("CursorResult[Any]", res).rowcount > 0


async def get_available_slots(
    session: AsyncSession,
    master_id: UUID,
    slot_date: date,
) -> list[Slot]:
    """Get open slots for master on a given date.

    Filters: status='open' AND slot_date matches.
    Caller (handler) is responsible for ensuring slot_date >= today.
    """
    stmt = (
        select(Slot)
        .where(Slot.master_id == master_id, Slot.slot_date == slot_date, Slot.status == "open")
        .order_by(Slot.slot_hour)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
