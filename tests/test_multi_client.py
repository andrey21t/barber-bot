"""Tests for multi-client capacity check (Этап 5.5, PLANS.md Blocker B).

Coverage (10 tests):
- capacity=1 blocks 2nd overlapping booking → WorkDayCapacityExceededError
- capacity=2 allows 2 overlapping bookings (boundary: 3rd blocked)
- cancelled booking excluded from overlap count
- transferred booking counted in overlap count
- boundary touching (end == next start) NOT counted as overlap (half-open)
- no WorkDay → capacity check skipped (backwards compat)
- WorkDay.is_active=False → capacity still enforced (defense-in-depth)
- transfer_booking capacity check on new slot
- concurrent create_booking race (skipif SQLite — needs pg_advisory_xact_lock)
- concurrent transfer_booking race (skipif SQLite)

Cross-DB overlap SQL (works on SQLite + Postgres):
    Booking.start_at < new_end_at AND Booking.end_at > new_start_at
    AND status IN ('confirmed', 'transferred')

Half-open: [14:00, 15:00] + [15:00, 16:00] do NOT overlap (boundary inclusive
on start, exclusive on end). Equivalent to tstzrange && on Postgres.

Baseline: 286 tests after 5.4 → +10 in 5.5 = 296 expected.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bot.models import Booking, Service, Slot, WorkDay
from bot.schemas import BookingCreate
from bot.services.booking import (
    WorkDayCapacityExceededError,
    create_booking,
    transfer_booking,
)
from sqlalchemy.ext.asyncio import AsyncSession

# Postgres-only race tests: pg_advisory_xact_lock semantics are Postgres-only.
# SQLite serializes writes via DB-level lock, so race tests skip on SQLite
# (engine_concurrent fixture uses file-based SQLite, no advisory lock support).
POSTGRES_TESTS_ENABLED = os.environ.get("DATABASE_URL", "sqlite+aiosqlite").startswith("postgresql")


async def _make_payload(slot_id: UUID, *, service_id: UUID | None = None) -> BookingCreate:
    """Build BookingCreate payload — minimal, mirroring test_booking._make_payload."""
    return BookingCreate(
        slot_id=slot_id,
        client_name="Паша",
        service_title="Стрижка",
        service_id=service_id,
    )


async def _seed_capacity_test(
    session: AsyncSession,
    seed_data: dict[str, Any],
    *,
    workday_cap: int = 1,
    workday_active: bool = True,
    days_ahead: int = 5,
    no_workday: bool = False,
) -> tuple[list[Slot], list[Service], WorkDay | None]:
    """Seed 3 slots (slot_hour 14/15/16) + 3 services (60/90/150 min) + WorkDay on new date.

    Slot.slot_hour is int 0-23 (legacy, deprecated in 5.4). For multi-client tests
    we use slot_hour + service.duration_minutes to construct overlapping ranges:
        booking [14:00, 16:30] = slot_hour=14 + service 150min
        booking [15:00, 16:30] = slot_hour=15 + service 90min
        booking [16:00, 17:00] = slot_hour=16 + service 60min (default)
    All within WorkDay [10:00, 20:00] LOCAL Moscow window.
    """
    new_date = (datetime.now(UTC) + timedelta(days=days_ahead)).date()
    slots: list[Slot] = []
    for hour in (14, 15, 16):
        slot = Slot(
            master_id=seed_data["master_id"],
            slot_date=new_date,
            slot_hour=hour,
            status="open",
        )
        session.add(slot)
        slots.append(slot)
    services: list[Service] = []
    for duration in (60, 90, 150):
        service = Service(
            business_id=seed_data["business_id"],
            name=f"test service {duration}min",
            duration_minutes=duration,
            price=None,
            is_active=True,
        )
        session.add(service)
        services.append(service)
    workday: WorkDay | None = None
    if not no_workday:
        workday = WorkDay(
            master_id=seed_data["master_id"],
            work_date=new_date,
            start_time=dt_time(10, 0),
            end_time=dt_time(20, 0),
            max_concurrent_clients=workday_cap,
            is_active=workday_active,
        )
        session.add(workday)
    await session.commit()
    return slots, services, workday


async def _direct_insert_booking(
    session: AsyncSession,
    seed_data: dict[str, Any],
    *,
    slot: Slot,
    start_at: datetime,
    end_at: datetime,
    status: str = "confirmed",
) -> Booking:
    """Direct INSERT a Booking bypassing create_booking — for test setup only.

    Used to seed 'cancelled' or 'transferred' bookings, or bookings with arbitrary
    start_at/end_at (slot.slot_hour doesn't support minutes; for capacity edge cases
    we need exact ranges that slot-based create_booking can't construct).

    CheckConstraint end_at > start_at is enforced by SQLAlchemy on flush.
    """
    booking = Booking(
        slot_id=slot.id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        client_id=seed_data["client"].id,
        service_id=None,
        service_title_snapshot="test",
        client_name_snapshot="test",
        start_at=start_at,
        end_at=end_at,
        status=status,
    )
    session.add(booking)
    await session.commit()
    return booking


def _local_to_utc(work_date: Any, hour: int, minute: int = 0) -> datetime:
    """Convert (work_date, HH:MM) LOCAL Moscow → aware UTC datetime.

    Mirror of booking._build_start_at logic (without slot dependency) — used by
    _direct_insert_booking to set start_at for seed bookings.
    """
    tz = ZoneInfo("Europe/Moscow")
    return datetime.combine(work_date, dt_time(hour, minute), tzinfo=tz).astimezone(UTC)


# ---------------------------------------------------------------------------
# Test 1: capacity=1 blocks 2nd overlapping booking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_booking_capacity_1_blocks_2nd_overlap(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """WorkDay cap=1: 2nd overlapping booking → WorkDayCapacityExceededError.

    booking1 = slot1 (slot_hour=14, service 90min) → [14:00, 15:30] LOCAL
    booking2 = slot2 (slot_hour=15, default 60min) → [15:00, 16:00] LOCAL
    overlap [15:00, 15:30] → count=1 (booking1), cap=1 → 1 >= 1 → raise.
    """
    slots, services, _ = await _seed_capacity_test(session, seed_data, workday_cap=1)
    # services: [60, 90, 150] — services[1] is 90min for slot1, services[0] default 60min for slot2
    payload1 = await _make_payload(slots[0].id, service_id=services[1].id)
    await create_booking(
        session,
        payload1,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )

    payload2 = await _make_payload(slots[1].id)  # default 60min service
    with pytest.raises(WorkDayCapacityExceededError) as exc_info:
        await create_booking(
            session,
            payload2,
            business_id=seed_data["business_id"],
            master_id=seed_data["master_id"],
            telegram_id=seed_data["client_telegram_id"],
        )

    msg = str(exc_info.value)
    assert "capacity 1 exceeded" in msg, f"Expected capacity error, got: {msg}"


# ---------------------------------------------------------------------------
# Test 2: capacity=2 allows 2 overlapping bookings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_booking_capacity_2_allows_2_overlaps(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """WorkDay cap=2: 2 overlapping bookings → both succeed (count < capacity).

    booking1 = slot1 (slot_hour=14, service 90min) → [14:00, 15:30]
    booking2 = slot2 (slot_hour=15, default 60min) → [15:00, 16:00]
    overlap [15:00, 15:30] → count=1, cap=2 → 1 < 2 → OK for both.
    """
    slots, services, _ = await _seed_capacity_test(session, seed_data, workday_cap=2)
    payload1 = await _make_payload(slots[0].id, service_id=services[1].id)
    result1 = await create_booking(
        session,
        payload1,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )
    payload2 = await _make_payload(slots[1].id)
    result2 = await create_booking(
        session,
        payload2,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )

    assert result1.slot_id == slots[0].id
    assert result2.slot_id == slots[1].id


# ---------------------------------------------------------------------------
# Test 3: capacity=2 blocks 3rd overlapping booking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_booking_capacity_2_blocks_3rd_overlap(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """WorkDay cap=2: 3rd overlapping booking → WorkDayCapacityExceededError.

    booking1 = slot1 (slot_hour=14, service 150min) → [14:00, 16:30]
    booking2 = slot2 (slot_hour=15, service 90min)  → [15:00, 16:30]
    booking3 = slot3 (slot_hour=16, default 60min)  → [16:00, 17:00]

    Overlap for booking3:
      booking3.start_at < booking1.end_at: 16:00 < 16:30 → True
      booking3.start_at < booking2.end_at: 16:00 < 16:30 → True
      count=2, cap=2 → 2 >= 2 → raise.
    """
    slots, services, _ = await _seed_capacity_test(session, seed_data, workday_cap=2)
    # services: [60, 90, 150] — services[2]=150 for slot1, services[1]=90 for slot2,
    # default 60 for slot3.
    p1 = await _make_payload(slots[0].id, service_id=services[2].id)
    await create_booking(
        session,
        p1,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )
    p2 = await _make_payload(slots[1].id, service_id=services[1].id)
    await create_booking(
        session,
        p2,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )

    p3 = await _make_payload(slots[2].id)
    with pytest.raises(WorkDayCapacityExceededError):
        await create_booking(
            session,
            p3,
            business_id=seed_data["business_id"],
            master_id=seed_data["master_id"],
            telegram_id=seed_data["client_telegram_id"],
        )


# ---------------------------------------------------------------------------
# Test 4: cancelled booking excluded from capacity count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_booking_cancelled_excluded_from_capacity(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Cancelled booking is NOT counted in overlap — slot freed for new booking.

    Setup: WorkDay cap=1, booking1 (cancelled, [14:00, 15:30] LOCAL),
    new booking2 on slot2 (slot_hour=15, [15:00, 16:00]).
    Overlap [15:00, 15:30] but booking1.status='cancelled' → excluded → count=0.
    cap=1, 0 >= 1 = False → OK.
    """
    slots, services, workday = await _seed_capacity_test(session, seed_data, workday_cap=1)
    assert workday is not None
    work_date = workday.work_date

    # Direct INSERT cancelled booking on slot1: [14:00, 15:30] LOCAL
    start1 = _local_to_utc(work_date, 14, 0)
    end1 = _local_to_utc(work_date, 15, 30)
    await _direct_insert_booking(
        session,
        seed_data,
        slot=slots[0],
        start_at=start1,
        end_at=end1,
        status="cancelled",
    )

    # New booking2 on slot2 (slot_hour=15, default 60 → [15:00, 16:00])
    p2 = await _make_payload(slots[1].id)
    result2 = await create_booking(
        session,
        p2,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )
    assert result2.slot_id == slots[1].id


# ---------------------------------------------------------------------------
# Test 5: transferred booking counted in capacity count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_booking_transferred_counted_in_capacity(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Transferred booking IS counted in overlap (status IN confirmed/transferred).

    Setup: WorkDay cap=1, booking1 (transferred, [14:00, 15:30] LOCAL),
    new booking2 on slot2 (slot_hour=15, [15:00, 16:00]).
    Overlap [15:00, 15:30] with booking1 (transferred) → count=1, cap=1 → raise.
    """
    slots, services, workday = await _seed_capacity_test(session, seed_data, workday_cap=1)
    assert workday is not None
    work_date = workday.work_date

    # Direct INSERT transferred booking on slot1: [14:00, 15:30] LOCAL
    start1 = _local_to_utc(work_date, 14, 0)
    end1 = _local_to_utc(work_date, 15, 30)
    await _direct_insert_booking(
        session,
        seed_data,
        slot=slots[0],
        start_at=start1,
        end_at=end1,
        status="transferred",
    )

    # New booking2 on slot2 (slot_hour=15, default 60 → [15:00, 16:00])
    p2 = await _make_payload(slots[1].id)
    with pytest.raises(WorkDayCapacityExceededError):
        await create_booking(
            session,
            p2,
            business_id=seed_data["business_id"],
            master_id=seed_data["master_id"],
            telegram_id=seed_data["client_telegram_id"],
        )


# ---------------------------------------------------------------------------
# Test 6: boundary touching ranges do NOT overlap (half-open semantics)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_booking_boundary_touching_no_overlap(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Touching ranges [14:00, 15:00] + [15:00, 16:00] → NOT overlap, cap=1 OK.

    Half-open: start_at < new_end_at AND end_at > new_start_at.
    booking1.end_at = 15:00, booking2.start_at = 15:00 → 15:00 < 15:00 = False → no overlap.
    Locks strict half-open semantics so a regression to <= would fail this test.
    """
    slots, services, workday = await _seed_capacity_test(session, seed_data, workday_cap=1)
    assert workday is not None

    # booking1 on slot1 (slot_hour=14, default 60 → [14:00, 15:00] LOCAL)
    p1 = await _make_payload(slots[0].id)
    await create_booking(
        session,
        p1,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )

    # booking2 on slot2 (slot_hour=15, default 60 → [15:00, 16:00] LOCAL)
    # booking1.end_at = 15:00, booking2.start_at = 15:00 → touching, no overlap.
    p2 = await _make_payload(slots[1].id)
    result2 = await create_booking(
        session,
        p2,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )
    assert result2.slot_id == slots[1].id


# ---------------------------------------------------------------------------
# Test 7: no WorkDay → capacity check skipped (backwards compat)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_booking_no_workday_skip_capacity_check(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """No WorkDay for the date → capacity check skipped, overlapping bookings OK.

    Backwards compat: legacy /addslots (before /openday rollout in 5.1) creates
    Slot without WorkDay. create_booking's _select_workday_for_slot returns None
    → skip _check_multi_client_capacity → no WorkDayCapacityExceededError.
    """
    slots, services, _ = await _seed_capacity_test(session, seed_data, no_workday=True)

    # booking1 on slot1 (slot_hour=14, service 90 → [14:00, 15:30] LOCAL)
    p1 = await _make_payload(slots[0].id, service_id=services[1].id)
    await create_booking(
        session,
        p1,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )

    # booking2 on slot2 (slot_hour=15, default → [15:00, 16:00] LOCAL) — overlap with booking1
    # No WorkDay → capacity check skipped → no raise.
    p2 = await _make_payload(slots[1].id)
    result2 = await create_booking(
        session,
        p2,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )
    assert result2.slot_id == slots[1].id


# ---------------------------------------------------------------------------
# Test 8: WorkDay.is_active=False → capacity still enforced (defense-in-depth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_booking_workday_inactive_still_enforces_capacity(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """WorkDay.is_active=False: capacity STILL enforced (defense-in-depth).

    An inactive WorkDay means "closed day" — /slots UI filters by is_active (5.6),
    so clients can't see slots. But IF a booking request reaches create_booking
    (e.g. legacy /addslots created a slot under inactive WorkDay), capacity is
    still bounded to max_concurrent_clients. Window shape + capacity are
    orthogonal to slot visibility.
    """
    slots, services, _ = await _seed_capacity_test(
        session, seed_data, workday_cap=1, workday_active=False
    )

    p1 = await _make_payload(slots[0].id, service_id=services[1].id)
    await create_booking(
        session,
        p1,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )

    p2 = await _make_payload(slots[1].id)
    with pytest.raises(WorkDayCapacityExceededError):
        await create_booking(
            session,
            p2,
            business_id=seed_data["business_id"],
            master_id=seed_data["master_id"],
            telegram_id=seed_data["client_telegram_id"],
        )


# ---------------------------------------------------------------------------
# Test 9: transfer_booking capacity check on new slot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transfer_booking_capacity_check_on_new_slot(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """transfer_booking: new slot capacity check enforced (B2 fix, Этап 5.5).

    Setup: WorkDay cap=1 on new_date, booking1 on slot_new1 ([14:00, 15:30] confirmed),
    booking2 on slot_old ([12:00, 13:00] confirmed, in past-safe window for transfer).
    transfer booking2 → slot_new2 (slot_hour=15, [15:00, 16:00] overlap with booking1).
    count=1 (booking1), cap=1 → raise WorkDayCapacityExceededError.

    Note: transfer_booking uses _build_start_at(slot, business_tz) → builds start_at
    from new_slot.slot_hour. We can't use create_booking for booking1 (it would
    lock slot_new1 status to 'booked', and transfer_booking's _select_open_slot
    would refuse slot_new2 if it's 'booked'). So booking1 is direct INSERT.
    """
    slots, services, workday = await _seed_capacity_test(session, seed_data, workday_cap=1)
    assert workday is not None
    work_date = workday.work_date

    # booking1 direct INSERT on slots[0] (slot_hour=14): [14:00, 15:30] LOCAL
    start1 = _local_to_utc(work_date, 14, 0)
    end1 = _local_to_utc(work_date, 15, 30)
    await _direct_insert_booking(
        session,
        seed_data,
        slot=slots[0],
        start_at=start1,
        end_at=end1,
        status="confirmed",
    )

    # booking2 direct INSERT on a SEPARATE slot (need a free slot in the FUTURE
    # for transfer_booking's 24h rule to pass). Use seed_data.slot (tomorrow, slot_hour=14)
    # — already exists from conftest seed_data, but slot.status='open' there.
    # We need booking2 to be confirmed on some slot to transfer it. Direct INSERT
    # on a new slot 12 days ahead (different date, no WorkDay conflict):
    other_date = (datetime.now(UTC) + timedelta(days=12)).date()
    other_slot = Slot(
        master_id=seed_data["master_id"],
        slot_date=other_date,
        slot_hour=14,
        status="booked",
    )
    session.add(other_slot)
    await session.commit()
    # booking2 on other_slot, status='confirmed', [14:00, 15:00] LOCAL on other_date
    start2 = _local_to_utc(other_date, 14, 0)
    end2 = _local_to_utc(other_date, 15, 0)
    booking2 = await _direct_insert_booking(
        session,
        seed_data,
        slot=other_slot,
        start_at=start2,
        end_at=end2,
        status="confirmed",
    )

    # Now transfer booking2 → slots[1] (slot_hour=15 on work_date, [15:00, 16:00]).
    # slots[1].status='open' (no booking on it yet — booking1 is on slots[0]).
    # transfer_booking should: SELECT new slot, build start_at, capacity check.
    # Overlap: booking2_new [15:00, 16:00] vs booking1 [14:00, 15:30] → [15:00, 15:30].
    # count=1 (booking1), cap=1 → raise WorkDayCapacityExceededError.
    sched = AsyncIOScheduler(jobstores={"default": MemoryJobStore()})
    with pytest.raises(WorkDayCapacityExceededError):
        await transfer_booking(
            session,
            booking2.id,
            slots[1].id,
            seed_data["client"].id,
            sched,
        )


# ---------------------------------------------------------------------------
# Test 9b: same-date transfer excludes booking itself from overlap count (F1 fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transfer_booking_same_date_overlap_excludes_self(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """transfer_booking: same-date transfer excludes the booking itself (F1 fix).

    Setup: WorkDay cap=1 on work_date. Booking on slots[0] [14:00, 15:30] confirmed,
    owned by seed_data.client. Transfer to slots[1] [15:00, 16:00] on SAME work_date.

    Pre-F1 bug: capacity check counts the booking itself in overlap (its OLD range
    [14:00, 15:30] is 'confirmed' pre-UPDATE, master_id matches, OLD range satisfies
    `start_at < 16:00 AND end_at > 15:00` for new [15:00, 16:00]). count=1, cap=1 →
    raise WorkDayCapacityExceededError → can't transfer booking to overlapping slot
    on the same date even though it's "moving itself".

    Post-F1 fix: `excluded_booking_id=booking_id` passed by transfer_booking →
    capacity check `WHERE ... AND id != booking_id` → count=0 (only OTHER active
    bookings counted) → 0 < 1 → no raise → transfer succeeds.
    """
    slots, services, workday = await _seed_capacity_test(session, seed_data, workday_cap=1)
    assert workday is not None
    work_date = workday.work_date

    # booking on slots[0] (slot_hour=14, service 90min) → [14:00, 15:30] LOCAL confirmed
    booking = await _direct_insert_booking(
        session,
        seed_data,
        slot=slots[0],
        start_at=_local_to_utc(work_date, 14, 0),
        end_at=_local_to_utc(work_date, 15, 30),
        status="confirmed",
    )

    # Transfer booking → slots[1] (slot_hour=15, default 60min service) → [15:00, 16:00]
    # Own OLD range [14:00, 15:30] overlaps with NEW [15:00, 16:00] on intersection
    # [15:00, 15:30]. After F1 fix: excluded_booking_id=booking.id → count=0 → succeeds.
    sched = AsyncIOScheduler(jobstores={"default": MemoryJobStore()})
    result = await transfer_booking(
        session,
        booking.id,
        slots[1].id,
        seed_data["client"].id,
        sched,
    )
    assert result.new_slot_id == slots[1].id
    # Verify booking actually moved (status='transferred', slot_id updated).
    await session.refresh(booking)
    assert booking.status == "transferred"
    assert booking.slot_id == slots[1].id


# ---------------------------------------------------------------------------
# Test 10: concurrent create_booking race — skipif SQLite (Postgres advisory lock)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not POSTGRES_TESTS_ENABLED,
    reason="Race tests require Postgres pg_advisory_xact_lock; SQLite serializes via DB-level lock",
)
@pytest.mark.asyncio
async def test_create_booking_concurrent_race_postgres(
    session_factory_concurrent: Any,
    seed_data: dict[str, Any],
) -> None:
    """Concurrent create_booking on overlapping slots: only one succeeds.

    Postgres-only: pg_advisory_xact_lock serializes the two transactions. First
    acquires lock, INSERTs booking1, commits, releases lock. Second acquires lock
    (after first commits), sees booking1 in overlap count, raises
    WorkDayCapacityExceededError.

    SQLite: skipped — file-based SQLite serializes writes via DB-level lock, but
    the advisory lock is no-op, so the test semantics don't transfer. Run with
    `DATABASE_URL=postgresql://... pytest tests/test_multi_client.py` and pass
    `-k concurrent_race_postgres`.

    NOTE: seed_data fixture is bound to `session` (in-memory), but this test uses
    session_factory_concurrent (file-based). We re-seed inside the test using the
    concurrent engine. This is a known setup cost for race tests.
    """
    # Set up schema + seed via session_factory_concurrent
    from bot.models import Business, Client, Master

    biz = Business(name="Test", telegram_owner_id=461355056, timezone="Europe/Moscow")
    master = Master(business_id=biz.id, name="Екатерина", telegram_id=461355056, role="owner")
    client = Client(telegram_id=999888777, name="Test")
    new_date = (datetime.now(UTC) + timedelta(days=20)).date()
    workday = WorkDay(
        master_id=master.id,
        work_date=new_date,
        start_time=dt_time(10, 0),
        end_time=dt_time(20, 0),
        max_concurrent_clients=1,
        is_active=True,
    )
    slot1 = Slot(master_id=master.id, slot_date=new_date, slot_hour=14, status="open")
    slot2 = Slot(master_id=master.id, slot_date=new_date, slot_hour=15, status="open")
    service_long = Service(
        business_id=biz.id, name="long", duration_minutes=90, price=None, is_active=True
    )
    async with session_factory_concurrent() as setup_session:
        setup_session.add_all([biz, master, client, workday, slot1, slot2, service_long])
        await setup_session.commit()

    # Two concurrent create_booking calls — both target overlapping ranges
    async def _book(slot: Slot, telegram_id: int) -> tuple[bool, str | None]:
        async with session_factory_concurrent() as s:
            payload = await _make_payload(slot.id, service_id=service_long.id)
            try:
                await create_booking(
                    s,
                    payload,
                    business_id=biz.id,
                    master_id=master.id,
                    telegram_id=telegram_id,
                )
                return True, None
            except WorkDayCapacityExceededError as e:
                return False, str(e)

    results = await asyncio.gather(
        _book(slot1, 111222333),
        _book(slot2, 444555666),
    )
    successes = sum(1 for ok, _ in results if ok)
    # Exactly one wins; the other raises WorkDayCapacityExceededError
    assert successes == 1, f"Expected 1 success, got {successes}: {results}"
    loser_msg = next(msg for ok, msg in results if not ok)
    assert "capacity 1 exceeded" in (loser_msg or "")


# ---------------------------------------------------------------------------
# Test 11: concurrent transfer_booking race — skipif SQLite
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not POSTGRES_TESTS_ENABLED,
    reason="Race tests require Postgres pg_advisory_xact_lock; SQLite serializes via DB-level lock",
)
@pytest.mark.asyncio
async def test_transfer_booking_concurrent_race_postgres(
    session_factory_concurrent: Any,
) -> None:
    """Concurrent transfer of two bookings to overlapping new slots: only one wins.

    Postgres-only (see test_create_booking_concurrent_race_postgres for rationale).

    Setup: 2 existing bookings on 2 separate slots (different dates, both
    confirmed). WorkDay cap=1 on a third date with 2 free slots (slot_a, slot_b).
    Both transfers target the third date — booking1 → slot_a, booking2 → slot_b.
    slot_a and slot_b have overlapping ranges (via service duration). After first
    transfer commits, second's capacity check sees 1 overlap → raise.

    Skipped on SQLite — advisory lock is no-op, race semantics don't transfer.
    """
    from bot.models import Business, Client, Master

    biz = Business(name="Test", telegram_owner_id=461355056, timezone="Europe/Moscow")
    master = Master(business_id=biz.id, name="Екатерина", telegram_id=461355056, role="owner")
    client1 = Client(telegram_id=111222333, name="c1")
    client2 = Client(telegram_id=444555666, name="c2")

    # Date 1: holds booking1 (to be transferred). Slot already 'booked'.
    date1 = (datetime.now(UTC) + timedelta(days=20)).date()
    slot_old1 = Slot(master_id=master.id, slot_date=date1, slot_hour=10, status="booked")
    # Date 2: holds booking2. Slot already 'booked'.
    date2 = (datetime.now(UTC) + timedelta(days=21)).date()
    slot_old2 = Slot(master_id=master.id, slot_date=date2, slot_hour=10, status="booked")
    # Date 3: target date for transfer. WorkDay cap=1, 2 free slots.
    date3 = (datetime.now(UTC) + timedelta(days=22)).date()
    workday = WorkDay(
        master_id=master.id,
        work_date=date3,
        start_time=dt_time(10, 0),
        end_time=dt_time(20, 0),
        max_concurrent_clients=1,
        is_active=True,
    )
    slot_a = Slot(master_id=master.id, slot_date=date3, slot_hour=14, status="open")
    slot_b = Slot(master_id=master.id, slot_date=date3, slot_hour=15, status="open")
    service_long = Service(
        business_id=biz.id, name="long", duration_minutes=90, price=None, is_active=True
    )

    async with session_factory_concurrent() as setup_session:
        setup_session.add_all(
            [
                biz,
                master,
                client1,
                client2,
                slot_old1,
                slot_old2,
                workday,
                slot_a,
                slot_b,
                service_long,
            ]
        )
        await setup_session.commit()
        # booking1 on slot_old1 (date1, [10:00, 11:30] LOCAL), confirmed, owned by client1
        b1 = Booking(
            slot_id=slot_old1.id,
            business_id=biz.id,
            master_id=master.id,
            client_id=client1.id,
            service_id=service_long.id,
            service_title_snapshot="long",
            client_name_snapshot="c1",
            start_at=_local_to_utc(date1, 10, 0),
            end_at=_local_to_utc(date1, 11, 30),
            status="confirmed",
        )
        b2 = Booking(
            slot_id=slot_old2.id,
            business_id=biz.id,
            master_id=master.id,
            client_id=client2.id,
            service_id=service_long.id,
            service_title_snapshot="long",
            client_name_snapshot="c2",
            start_at=_local_to_utc(date2, 10, 0),
            end_at=_local_to_utc(date2, 11, 30),
            status="confirmed",
        )
        setup_session.add_all([b1, b2])
        await setup_session.commit()
        booking1_id = b1.id
        booking2_id = b2.id
        client1_id = client1.id
        client2_id = client2.id
        slot_a_id = slot_a.id
        slot_b_id = slot_b.id

    mock_scheduler = AsyncIOScheduler(jobstores={"default": MemoryJobStore()})

    async def _transfer(
        booking_id: UUID, new_slot_id: UUID, client_id: UUID
    ) -> tuple[bool, str | None]:
        async with session_factory_concurrent() as s:
            try:
                await transfer_booking(
                    s,
                    booking_id,
                    new_slot_id,
                    client_id,
                    mock_scheduler,
                )
                return True, None
            except WorkDayCapacityExceededError as e:
                return False, str(e)

    # Both transfers target date3, slot_a [14:00, 15:30] + slot_b [15:00, 16:30] → overlap.
    # service_long = 90min → slot_a end_at = 15:30, slot_b end_at = 16:30.
    # First transfer wins, second's capacity check sees 1 overlap → raise.
    results = await asyncio.gather(
        _transfer(booking1_id, slot_a_id, client1_id),
        _transfer(booking2_id, slot_b_id, client2_id),
    )
    successes = sum(1 for ok, _ in results if ok)
    assert successes == 1, f"Expected 1 success, got {successes}: {results}"
