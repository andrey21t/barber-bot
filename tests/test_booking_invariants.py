"""Tests for WorkDay invariants in create_booking (Этап 5.3, PLANS.md Gap 6).

Coverage:
- happy path: booking inside [workday.start_time, end_time] → succeeds
- outside_start: slot_hour earlier than workday.start_time → BookingOutsideWorkDayError
- outside_end: slot_hour + service.duration > workday.end_time → BookingOutsideWorkDayError
- no_workday_skip: slot without WorkDay → invariant skipped (backwards compat, booking ok)
- workday_inactive: WorkDay.is_active=False → invariant still enforced (window bounds)

Context (PLANS.md 5.3):
- WorkDay model lives alongside Slot (deprecated, drop in migration 006).
- create_booking SELECTs WorkDay for (slot.master_id, slot.slot_date):
  - found → validate [start_at, end_at] ∈ [workday.start_time, end_time] in UTC
  - not found → skip check (slot-only data via legacy /addslots before /openday in 5.1)
- _build_start_at still uses slot.slot_hour (drop in 5.4 when /slots switches to WorkDay).

Baseline: 274 tests after 5.2 → +5 tests in 5.3 = 279 expected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Any
from uuid import UUID

import pytest
from bot.models import Service, Slot, WorkDay
from bot.schemas import BookingCreate
from bot.services.booking import (
    BookingOutsideWorkDayError,
    create_booking,
)
from sqlalchemy.ext.asyncio import AsyncSession


async def _make_payload(slot_id: UUID, *, service_id: UUID | None = None) -> BookingCreate:
    """Build BookingCreate payload — minimal, mirroring test_booking._make_payload."""
    return BookingCreate(
        slot_id=slot_id,
        client_name="Паша",
        service_title="Стрижка",
        service_id=service_id,
    )


async def _make_slot_with_workday(
    session: AsyncSession,
    seed_data: dict[str, Any],
    *,
    slot_hour: int,
    workday_start_hour: int,
    workday_end_hour: int,
    days_ahead: int = 5,
    workday_active: bool = True,
) -> tuple[Slot, WorkDay]:
    """Seed a fresh slot + matching WorkDay for a NEW date (N days ahead).

    Why new date: seed_data.slot already has its own WorkDay (tomorrow [10:00, 20:00]).
    Adding a slot on tomorrow with different hours would conflict with the existing
    WorkDay (UNIQUE master_id+work_date). So we use a separate date (5 days ahead by
    default) to isolate the invariant under test.
    """
    new_date = (datetime.now(UTC) + timedelta(days=days_ahead)).date()
    slot = Slot(
        master_id=seed_data["master_id"],
        slot_date=new_date,
        slot_hour=slot_hour,
        status="open",
    )
    workday = WorkDay(
        master_id=seed_data["master_id"],
        work_date=new_date,
        start_time=dt_time(workday_start_hour, 0),
        end_time=dt_time(workday_end_hour, 0),
        max_concurrent_clients=1,
        is_active=workday_active,
    )
    session.add_all([slot, workday])
    await session.commit()
    return slot, workday


async def _make_service(
    session: AsyncSession,
    seed_data: dict[str, Any],
    *,
    duration_minutes: int,
) -> Service:
    """Seed a service with the given duration — for outside_end test (long service)."""
    service = Service(
        business_id=seed_data["business_id"],
        name=f"Test service {duration_minutes}min",
        duration_minutes=duration_minutes,
        price=None,
        is_active=True,
    )
    session.add(service)
    await session.commit()
    return service


# ---------------------------------------------------------------------------
# Invariant: booking inside WorkDay window → succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_booking_inside_workday_window(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Booking [14:00, 15:00] LOCAL fits WorkDay [10:00, 20:00] LOCAL → create_booking ok.

    Mirrors test_create_booking_happy_path but on a fresh slot+workday (5 days ahead)
    to isolate the invariant from seed_data's slot/workday.
    """
    slot, workday = await _make_slot_with_workday(
        session,
        seed_data,
        slot_hour=14,
        workday_start_hour=10,
        workday_end_hour=20,
        days_ahead=5,
    )

    payload = await _make_payload(slot.id)
    result = await create_booking(
        session,
        payload,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )

    assert result.slot_id == slot.id
    # 14:00 LOCAL MSK = 11:00 UTC; end_at = 12:00 UTC (default 60min duration).
    assert result.start_at.hour == 11
    assert result.end_at.hour == 12


# ---------------------------------------------------------------------------
# Invariant: booking start before workday start → BookingOutsideWorkDayError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_booking_outside_workday_start(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Booking start_at=08:00 LOCAL < workday.start_time=10:00 LOCAL → raise.

    Slot.slot_hour=8 → _build_start_at → 08:00 MSK = 05:00 UTC.
    WorkDay [10:00, 20:00] LOCAL → 07:00..17:00 UTC.
    05:00 UTC < 07:00 UTC → BookingOutsideWorkDayError.
    """
    slot, _ = await _make_slot_with_workday(
        session,
        seed_data,
        slot_hour=8,
        workday_start_hour=10,
        workday_end_hour=20,
        days_ahead=6,
    )

    payload = await _make_payload(slot.id)
    with pytest.raises(BookingOutsideWorkDayError) as exc_info:
        await create_booking(
            session,
            payload,
            business_id=seed_data["business_id"],
            master_id=seed_data["master_id"],
            telegram_id=seed_data["client_telegram_id"],
        )

    # Error mentions which bound is violated (start_at < workday start).
    msg = str(exc_info.value)
    assert "workday start" in msg, f"Expected 'workday start' in error, got: {msg}"


# ---------------------------------------------------------------------------
# Invariant: booking end after workday end → BookingOutsideWorkDayError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_booking_outside_workday_end(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Booking end_at > workday.end_time → raise (long service extends beyond window).

    Slot.slot_hour=19 (19:00 LOCAL MSK), service.duration_minutes=120 →
    end_at = 21:00 LOCAL. WorkDay.end_time=20:00 LOCAL → 21:00 > 20:00 → raise.

    Validates that invariant checks BOTH bounds (start AND end), not just start.
    """
    slot, _ = await _make_slot_with_workday(
        session,
        seed_data,
        slot_hour=19,
        workday_start_hour=10,
        workday_end_hour=20,
        days_ahead=7,
    )
    service = await _make_service(session, seed_data, duration_minutes=120)
    # Build payload referencing the long-duration service.
    payload = await _make_payload(slot.id, service_id=service.id)

    with pytest.raises(BookingOutsideWorkDayError) as exc_info:
        await create_booking(
            session,
            payload,
            business_id=seed_data["business_id"],
            master_id=seed_data["master_id"],
            telegram_id=seed_data["client_telegram_id"],
        )

    msg = str(exc_info.value)
    assert "workday end" in msg, f"Expected 'workday end' in error, got: {msg}"


# ---------------------------------------------------------------------------
# Invariant: no WorkDay → skip check (backwards compat)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_booking_no_workday_skip_invariant(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Slot WITHOUT matching WorkDay → invariant skipped, booking succeeds.

    Backwards compat: legacy /addslots creates Slot without WorkDay (until /openday
    rolls out in 5.1). create_booking's _select_workday_for_slot returns None →
    skip _validate_booking_within_workday → no BookingOutsideWorkDayError.

    Slot.slot_hour=23 (any hour OK without WorkDay bound), no WorkDay for that date.
    """
    # New slot 8 days ahead, slot_hour=23, NO WorkDay created.
    new_date = (datetime.now(UTC) + timedelta(days=8)).date()
    slot = Slot(
        master_id=seed_data["master_id"],
        slot_date=new_date,
        slot_hour=23,
        status="open",
    )
    session.add(slot)
    await session.commit()

    payload = await _make_payload(slot.id)
    result = await create_booking(
        session,
        payload,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )

    # Booking created — invariant skipped because WorkDay not found.
    assert result.slot_id == slot.id
    # 23:00 LOCAL MSK = 20:00 UTC; end_at = 21:00 UTC (default 60min).
    assert result.start_at.hour == 20
    assert result.end_at.hour == 21


# ---------------------------------------------------------------------------
# Invariant: WorkDay.is_active=False → invariant still enforced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_booking_workday_inactive_still_enforced(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """WorkDay.is_active=False → invariant still validates the window.

    Why: an inactive WorkDay is a "closed" day — slots shouldn't be shown in 5.6
    ('/slots' UI filters by is_active). But IF a booking request reaches create_booking
    (e.g. legacy /addslots created a slot under an inactive WorkDay), the invariant
    still bounds the booking to the declared window. This is defense-in-depth: the
    invariant is about window shape, not slot availability.

    Slot.slot_hour=8, WorkDay.is_active=False, window [10:00, 20:00] →
    booking [08:00, 09:00] still outside [10:00, 20:00] → raise.
    """
    slot, _ = await _make_slot_with_workday(
        session,
        seed_data,
        slot_hour=8,
        workday_start_hour=10,
        workday_end_hour=20,
        days_ahead=9,
        workday_active=False,
    )

    payload = await _make_payload(slot.id)
    with pytest.raises(BookingOutsideWorkDayError):
        await create_booking(
            session,
            payload,
            business_id=seed_data["business_id"],
            master_id=seed_data["master_id"],
            telegram_id=seed_data["client_telegram_id"],
        )


# ---------------------------------------------------------------------------
# Boundary tests: operators <, > (strict) — equal bounds accepted, not raised
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_booking_start_equals_workday_start(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Boundary: start_at == workday.start_time → accepted (operator <, not <=).

    Slot.slot_hour=10, WorkDay [10:00, 20:00] → start_at=10:00 MSK = 07:00 UTC.
    workday_start_utc = 10:00 MSK = 07:00 UTC. start_at == workday_start_utc →
    NOT (start_at < workday_start_utc) → no raise. Locks the strict < semantics
    so a regression to <= would fail this test.
    """
    slot, _ = await _make_slot_with_workday(
        session,
        seed_data,
        slot_hour=10,
        workday_start_hour=10,
        workday_end_hour=20,
        days_ahead=10,
    )

    payload = await _make_payload(slot.id)
    result = await create_booking(
        session,
        payload,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )

    # Booking succeeds — start_at == workday.start_time is on the boundary.
    assert result.slot_id == slot.id
    # 10:00 MSK = 07:00 UTC, default 60min duration → end_at 08:00 UTC.
    assert result.start_at.hour == 7
    assert result.end_at.hour == 8


@pytest.mark.asyncio
async def test_create_booking_end_equals_workday_end(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Boundary: end_at == workday.end_time → accepted (operator >, not >=).

    Slot.slot_hour=19 (19:00 MSK = 16:00 UTC), service.duration=60 → end_at=20:00
    MSK = 17:00 UTC. WorkDay.end_time=20:00 → workday_end_utc=17:00 UTC.
    end_at == workday_end_utc → NOT (end_at > workday_end_utc) → no raise.
    Locks the strict > semantics so a regression to >= would fail this test.
    """
    slot, _ = await _make_slot_with_workday(
        session,
        seed_data,
        slot_hour=19,
        workday_start_hour=10,
        workday_end_hour=20,
        days_ahead=11,
    )
    # Default service (60min) — start_at 19:00 MSK + 60min = 20:00 MSK = end_time.
    payload = await _make_payload(slot.id)
    result = await create_booking(
        session,
        payload,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )

    assert result.slot_id == slot.id
    # 19:00 MSK = 16:00 UTC, end_at 17:00 UTC == workday end (20:00 MSK = 17:00 UTC).
    assert result.start_at.hour == 16
    assert result.end_at.hour == 17
