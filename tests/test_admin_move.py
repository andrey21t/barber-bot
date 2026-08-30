"""Tests for bot.services.admin_move.admin_move_booking (Этап 5.9).

Coverage (Acceptance Contract — 16 tests):
- happy path: workday-only source (slot_id=None) + legacy slot source (slot_id released)
- error paths: not_found, already_cancelled, workday_not_found, workday_inactive,
  capacity_exceeded, outside_workday_window, past_time_rejected
- edge cases: no_op_same_time, already_past_booking_allowed, notification_log_idempotent
- runtime: concurrent_race (file-based SQLite, manual orchestration)
- scheduler: remove_jobs + schedule_for_booking called after commit

Mirror tests/test_booking.py:667-1320 (transfer_booking tests) — same race
protection, same scheduler side-effects, but admin_move skips 24h rule and
client_id pin.

NB: SQLite stores Booking.start_at NAIVE (no tzinfo). Tests compare aware UTC
via .replace(tzinfo=UTC) — same pattern as test_transfer_booking_happy_path:704.
"""

from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bot.models import (
    Booking,
    Business,
    Client,
    Master,
    NotificationLog,
    Slot,
    WorkDay,
)
from bot.schemas import BookingCreate
from bot.services.admin_move import (
    AdminMoveResult,
    WorkDayInactiveError,
    WorkDayNotFoundError,
    admin_move_booking,
)
from bot.services.booking import (
    BookingAlreadyCancelledError,
    BookingAlreadyTransferredError,
    BookingNotFoundError,
    BookingOutsideWorkDayError,
    SlotInPastError,
    WorkDayCapacityExceededError,
    _build_start_at_from_workday,
    create_booking,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _mock_scheduler() -> MagicMock:
    """Build MagicMock spec=AsyncIOScheduler (mirror test_booking.py:253)."""
    return MagicMock(spec=AsyncIOScheduler)


async def _make_workday(
    session: AsyncSession,
    seed_data: dict[str, Any],
    *,
    days_ahead: int = 5,
    start_time: dt_time = dt_time(10, 0),
    end_time: dt_time = dt_time(20, 0),
    max_concurrent_clients: int = 1,
    is_active: bool = True,
) -> WorkDay:
    """Insert a new WorkDay N days ahead — for admin_move destination tests."""
    work_date = (datetime.now(UTC) + timedelta(days=days_ahead)).date()
    workday = WorkDay(
        master_id=seed_data["master_id"],
        work_date=work_date,
        start_time=start_time,
        end_time=end_time,
        max_concurrent_clients=max_concurrent_clients,
        is_active=is_active,
    )
    session.add(workday)
    await session.commit()
    return workday


async def _seed_workday_booking(
    session: AsyncSession,
    seed_data: dict[str, Any],
    *,
    start_time_local: dt_time = dt_time(14, 0),
) -> Booking:
    """Create a confirmed workday-only booking (slot_id=None) via create_booking.

    Re-reads from DB after commit. Mirrors _seed_confirmed_booking in
    test_booking.py:262 but uses workday_payload (5.8a path).
    """
    workday = seed_data["workday"]
    payload = BookingCreate(
        workday_id=workday.id,
        start_time_local=start_time_local,
        client_name="Паша",
        service_title="Стрижка",
        service_id=None,
    )
    result = await create_booking(
        session,
        payload,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )
    stmt = select(Booking).where(Booking.id == result.booking_id)
    return (await session.execute(stmt)).scalar_one()


async def _seed_legacy_slot_booking(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> Booking:
    """Create a confirmed legacy slot-based booking (slot_id is not None).

    Uses seed_data['slot'] (Slot at slot_hour=14, tomorrow). Mirrors
    _seed_confirmed_booking in test_booking.py:262.
    """
    slot = seed_data["slot"]
    payload = BookingCreate(
        slot_id=slot.id,
        client_name="Паша",
        service_title="Стрижка",
        service_id=None,
    )
    result = await create_booking(
        session,
        payload,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )
    stmt = select(Booking).where(Booking.id == result.booking_id)
    return (await session.execute(stmt)).scalar_one()


async def _seed_past_booking(
    session: AsyncSession,
    seed_data: dict[str, Any],
    *,
    days_ago: int = 1,
    hour_local: int = 14,
) -> Booking:
    """Insert a confirmed booking with start_at in the past (direct INSERT).

    create_booking raises SlotInPastError for past start_at, so we bypass it
    via direct INSERT — admin_move allows already-past bookings to be moved
    (no 24h rule, scheduler's misfire_grace_time handles past time gracefully).
    """
    past_date = (datetime.now(UTC) - timedelta(days=days_ago)).date()
    # Build UTC start_at: past_date + hour_local LOCAL Moscow.
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Moscow")
    start_at_local = datetime.combine(past_date, dt_time(hour_local), tzinfo=tz)
    start_at_utc = start_at_local.astimezone(UTC).replace(tzinfo=None)  # naive UTC for SQLite
    # SQLite stores naive; use a Service with 60min duration for end_at.
    booking = Booking(
        client_id=seed_data["client"].id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        slot_id=None,
        start_at=start_at_utc,
        end_at=start_at_utc + timedelta(minutes=60),
        status="confirmed",
        client_name_snapshot="Паша",
        service_title_snapshot="Стрижка",
    )
    session.add(booking)
    await session.commit()
    return booking


# ============================================================
# 1. Happy paths
# ============================================================


@pytest.mark.asyncio
async def test_admin_move_happy_path_workday_only_source(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Workday-only booking (slot_id=None) → move to new workday.

    - status='transferred', slot_id stays None (no legacy slot to release).
    - No Slot UPDATE (slot_id is None — step 19 skipped).
    - NotificationLog 'client_moved' exactly one row.
    - Scheduler remove_jobs + schedule_for_booking called.
    """
    booking = await _seed_workday_booking(session, seed_data, start_time_local=dt_time(14, 0))
    booking_id = booking.id
    old_start_at = booking.start_at  # naive UTC

    new_workday = await _make_workday(session, seed_data, days_ahead=5)
    new_workday_id = new_workday.id

    mock_scheduler = _mock_scheduler()
    result = await admin_move_booking(
        session,
        booking_id=booking_id,
        new_workday_id=new_workday_id,
        new_start_at_local=dt_time(15, 30),
        scheduler=mock_scheduler,
    )

    # AdminMoveResult fields
    assert isinstance(result, AdminMoveResult)
    assert result.booking_id == booking_id
    assert result.old_start_at == old_start_at.replace(tzinfo=UTC)
    assert result.new_start_at != old_start_at.replace(tzinfo=UTC)
    assert result.client_telegram_id == seed_data["client_telegram_id"]
    assert result.client_name_snapshot == "Паша"
    assert result.service_title_snapshot == "Стрижка"
    assert result.old_slot_id is None  # workday-only source
    assert result.notification_logged is True

    # DB: booking status='transferred', slot_id=None (was None, stays None)
    await session.rollback()
    stmt_b = select(Booking).where(Booking.id == booking_id)
    booking_after = (await session.execute(stmt_b)).scalar_one()
    assert booking_after.status == "transferred"
    assert booking_after.slot_id is None  # still None — no legacy slot

    # NotificationLog 'client_moved' exactly one row (UNIQUE guard)
    stmt_n = select(NotificationLog).where(
        NotificationLog.booking_id == booking_id,
        NotificationLog.kind == "client_moved",
    )
    notif_rows = (await session.execute(stmt_n)).scalars().all()
    assert len(notif_rows) == 1

    # Scheduler: remove_job called for both old reminder jobs
    expected_remove = {f"remind_24h_{booking_id}", f"remind_1h_{booking_id}"}
    actual_remove = {call.args[0] for call in mock_scheduler.remove_job.call_args_list}
    assert actual_remove == expected_remove

    # Scheduler: add_job called twice (new remind_24h + new remind_1h)
    actual_add_ids = {call.kwargs.get("id") for call in mock_scheduler.add_job.call_args_list}
    assert actual_add_ids == {f"remind_24h_{booking_id}", f"remind_1h_{booking_id}"}


@pytest.mark.asyncio
async def test_admin_move_happy_path_legacy_slot_source(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Legacy slot-based booking → move to new workday.

    - status='transferred', slot_id → None (unified workday-based destination).
    - OLD slot released to 'open' (step 19 executed — booking.slot_id was not None).
    - NotificationLog 'client_moved' exactly one row.
    """
    booking = await _seed_legacy_slot_booking(session, seed_data)
    booking_id = booking.id
    old_slot_id = booking.slot_id  # not None for legacy
    assert old_slot_id is not None

    new_workday = await _make_workday(session, seed_data, days_ahead=5)
    new_workday_id = new_workday.id

    mock_scheduler = _mock_scheduler()
    result = await admin_move_booking(
        session,
        booking_id=booking_id,
        new_workday_id=new_workday_id,
        new_start_at_local=dt_time(15, 30),
        scheduler=mock_scheduler,
    )

    # Result reflects legacy slot release
    assert result.old_slot_id == old_slot_id

    # DB: booking status='transferred', slot_id → None (was old_slot_id)
    await session.rollback()
    stmt_b = select(Booking).where(Booking.id == booking_id)
    booking_after = (await session.execute(stmt_b)).scalar_one()
    assert booking_after.status == "transferred"
    assert booking_after.slot_id is None  # unified workday-based destination

    # DB: old slot released to 'open'
    from sqlalchemy import select as sa_select

    stmt_old = sa_select(Slot.status).where(Slot.id == old_slot_id)
    assert (await session.execute(stmt_old)).scalar_one() == "open"


# ============================================================
# 2. Error paths
# ============================================================


@pytest.mark.asyncio
async def test_admin_move_booking_not_found(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Random booking_id → BookingNotFoundError (no client_id pin — admin can move
    ANY booking, but random booking_id is not in DB).
    """
    new_workday = await _make_workday(session, seed_data, days_ahead=5)
    mock_scheduler = _mock_scheduler()

    with pytest.raises(BookingNotFoundError):
        await admin_move_booking(
            session,
            booking_id=uuid4(),  # random
            new_workday_id=new_workday.id,
            new_start_at_local=dt_time(15, 30),
            scheduler=mock_scheduler,
        )


@pytest.mark.asyncio
async def test_admin_move_already_cancelled(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """booking.status='cancelled' → BookingAlreadyCancelledError."""
    booking = await _seed_workday_booking(session, seed_data)
    booking_id = booking.id

    # Manually cancel the booking (cancel_booking raises CancelTooLateError for
    # past-deadline bookings, so direct UPDATE is simpler).
    from sqlalchemy import update

    await session.execute(
        update(Booking).where(Booking.id == booking_id).values(status="cancelled")
    )
    await session.commit()

    new_workday = await _make_workday(session, seed_data, days_ahead=5)
    mock_scheduler = _mock_scheduler()

    with pytest.raises(BookingAlreadyCancelledError):
        await admin_move_booking(
            session,
            booking_id=booking_id,
            new_workday_id=new_workday.id,
            new_start_at_local=dt_time(15, 30),
            scheduler=mock_scheduler,
        )


@pytest.mark.asyncio
async def test_admin_move_workday_not_found(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Random workday_id → WorkDayNotFoundError."""
    booking = await _seed_workday_booking(session, seed_data)
    mock_scheduler = _mock_scheduler()

    with pytest.raises(WorkDayNotFoundError):
        await admin_move_booking(
            session,
            booking_id=booking.id,
            new_workday_id=uuid4(),  # random
            new_start_at_local=dt_time(15, 30),
            scheduler=mock_scheduler,
        )


@pytest.mark.asyncio
async def test_admin_move_workday_inactive(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """WorkDay.is_active=False → WorkDayInactiveError."""
    booking = await _seed_workday_booking(session, seed_data)

    # Create inactive workday (closed via /closeday)
    new_workday = await _make_workday(session, seed_data, days_ahead=5, is_active=False)
    mock_scheduler = _mock_scheduler()

    with pytest.raises(WorkDayInactiveError):
        await admin_move_booking(
            session,
            booking_id=booking.id,
            new_workday_id=new_workday.id,
            new_start_at_local=dt_time(15, 30),
            scheduler=mock_scheduler,
        )


@pytest.mark.asyncio
async def test_admin_move_capacity_exceeded(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """WorkDay.max_concurrent_clients=1, existing overlapping booking on NEW workday →
    WorkDayCapacityExceededError (excluded_booking_id skips self on the move).

    Setup: seed booking at 14:00 on source workday. Create new_workday (cap=1)
    5 days ahead, pre-book 14:30-15:30 on it with a different client. Move booking
    to 14:30 on new_workday → overlap with third_booking (14:30-15:30). cap=1,
    overlap_count=1 (excluding self) → exceeded.
    """
    booking = await _seed_workday_booking(session, seed_data, start_time_local=dt_time(14, 0))

    new_workday = await _make_workday(session, seed_data, days_ahead=5)

    # Pre-book 14:30-15:30 on new_workday with a different client.
    third_client = Client(telegram_id=555444333, name="Третий")
    session.add(third_client)
    await session.flush()
    third_payload = BookingCreate(
        workday_id=new_workday.id,
        start_time_local=dt_time(14, 30),
        client_name="Третий",
        service_title="Бритьё",
        service_id=None,
    )
    await create_booking(
        session,
        third_payload,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=555444333,
    )

    mock_scheduler = _mock_scheduler()

    # Move our booking (14:00-15:00) to new_workday at 14:30 → overlap with
    # third_booking (14:30-15:30). cap=1, overlap_count=1 (excluding self) → exceeded.
    with pytest.raises(WorkDayCapacityExceededError):
        await admin_move_booking(
            session,
            booking_id=booking.id,
            new_workday_id=new_workday.id,
            new_start_at_local=dt_time(14, 30),
            scheduler=mock_scheduler,
        )


@pytest.mark.asyncio
async def test_admin_move_outside_workday_window(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """new_start_at_local outside [workday.start_time, end_time] →
    BookingOutsideWorkDayError.

    new_workday start_time=10:00, end_time=20:00. Move to 09:00 → start_at < workday_start_utc.
    """
    booking = await _seed_workday_booking(session, seed_data)
    new_workday = await _make_workday(
        session, seed_data, days_ahead=5, start_time=dt_time(10, 0), end_time=dt_time(20, 0)
    )
    mock_scheduler = _mock_scheduler()

    with pytest.raises(BookingOutsideWorkDayError):
        await admin_move_booking(
            session,
            booking_id=booking.id,
            new_workday_id=new_workday.id,
            new_start_at_local=dt_time(9, 0),  # before start_time
            scheduler=mock_scheduler,
        )


@pytest.mark.asyncio
async def test_admin_move_to_past_time_rejected(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """new_start_at <= datetime.now(UTC) → SlotInPastError.

    new_workday is in the past (days_ahead=-1) — service rejects via
    datetime.now(UTC) check (NOT injected `now_utc` — same rationale as
    transfer_booking:955).
    """
    booking = await _seed_workday_booking(session, seed_data)
    # Create workday 1 day in the past.
    past_workday = await _make_workday(session, seed_data, days_ahead=-1)
    mock_scheduler = _mock_scheduler()

    with pytest.raises(SlotInPastError):
        await admin_move_booking(
            session,
            booking_id=booking.id,
            new_workday_id=past_workday.id,
            new_start_at_local=dt_time(14, 0),  # 14:00 LOCAL yesterday → in the past
            scheduler=mock_scheduler,
        )


# ============================================================
# 3. Edge cases
# ============================================================


@pytest.mark.asyncio
async def test_admin_move_no_op_same_time_same_workday(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Moving booking to same workday + same start_time → success (no-op).

    booking was at 14:00 on seed workday. Move to 14:00 on same workday →
    UPDATE WHERE start_at=old_start_at succeeds (start_at unchanged). Result
    has old_start_at == new_start_at (no-op).

    This is a valid edge case — admin taps [🔄 Перенести], picks the SAME slot.
    Service allows it (race protection WHERE clause matches). UX-wise, the
    handler would still send the "Перенесено" notification — acceptable for MVP.
    """
    booking = await _seed_workday_booking(session, seed_data, start_time_local=dt_time(14, 0))
    booking_id = booking.id
    old_start_at = booking.start_at

    new_workday = seed_data["workday"]  # same workday

    mock_scheduler = _mock_scheduler()
    result = await admin_move_booking(
        session,
        booking_id=booking_id,
        new_workday_id=new_workday.id,
        new_start_at_local=dt_time(14, 0),  # same time
        scheduler=mock_scheduler,
    )

    # old_start_at == new_start_at (no-op move)
    assert result.old_start_at == old_start_at.replace(tzinfo=UTC)
    assert result.new_start_at == old_start_at.replace(tzinfo=UTC)


@pytest.mark.asyncio
async def test_admin_move_already_past_booking_allowed(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """booking.start_at <= now (already in the past) → move ALLOWED (no 24h rule).

    Setup: create booking with start_at in the past via direct INSERT (create_booking
    would raise SlotInPastError). admin_move has no 24h rule, no past-time check on
    the OLD booking's start_at — only on the NEW start_at. Scheduler's
    misfire_grace_time handles past time gracefully.

    Move to a future workday → success. scheduler.remove_jobs + schedule_for_booking
    called with future new_start_at (jobs fire on schedule, not immediately).
    """
    past_booking = await _seed_past_booking(session, seed_data, days_ago=1, hour_local=14)
    booking_id = past_booking.id

    new_workday = await _make_workday(session, seed_data, days_ahead=2)
    mock_scheduler = _mock_scheduler()

    result = await admin_move_booking(
        session,
        booking_id=booking_id,
        new_workday_id=new_workday.id,
        new_start_at_local=dt_time(15, 0),
        scheduler=mock_scheduler,
    )

    # Move succeeded — no 24h rule, no past-booking rejection.
    assert isinstance(result, AdminMoveResult)
    assert result.booking_id == booking_id
    # New start_at is in the future (new_workday is 2 days ahead).
    assert result.new_start_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_admin_move_notification_log_idempotent(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Pre-existing NotificationLog('client_moved') → SAVEPOINT catches
    IntegrityError → result.notification_logged=False (idempotent).

    Setup: insert NotificationLog row directly, then call admin_move. The
    SAVEPOINT in step 20 catches UNIQUE(booking_id, kind) IntegrityError,
    rolls back the savepoint (log_entry expunged), sets notification_logged=False.
    Booking UPDATE + slot release still commit successfully.
    """
    booking = await _seed_workday_booking(session, seed_data)
    booking_id = booking.id

    # Pre-insert NotificationLog row (simulates a prior admin_move attempt that
    # committed the log but failed elsewhere — rare, but UNIQUE guard exists).
    pre_log = NotificationLog(booking_id=booking_id, kind="client_moved")
    session.add(pre_log)
    await session.commit()

    new_workday = await _make_workday(session, seed_data, days_ahead=5)
    mock_scheduler = _mock_scheduler()

    result = await admin_move_booking(
        session,
        booking_id=booking_id,
        new_workday_id=new_workday.id,
        new_start_at_local=dt_time(15, 30),
        scheduler=mock_scheduler,
    )

    # Idempotency: SAVEPOINT caught the IntegrityError, notification_logged=False.
    assert result.notification_logged is False

    # DB: only one NotificationLog row (the pre-inserted one — no duplicate).
    await session.rollback()
    stmt_n = select(NotificationLog).where(
        NotificationLog.booking_id == booking_id,
        NotificationLog.kind == "client_moved",
    )
    notif_rows = (await session.execute(stmt_n)).scalars().all()
    assert len(notif_rows) == 1

    # DB: booking still moved (commit succeeded despite SAVEPOINT rollback).
    stmt_b = select(Booking).where(Booking.id == booking_id)
    booking_after = (await session.execute(stmt_b)).scalar_one()
    assert booking_after.status == "transferred"


# ============================================================
# 4. Runtime race (file-based SQLite, manual orchestration)
# ============================================================


@pytest.mark.asyncio
async def test_admin_move_concurrent_race_runtime(
    session_factory_concurrent: async_sessionmaker[AsyncSession],
) -> None:
    """Runtime race protection — faithful test of UPDATE WHERE start_at pin.

    Mirror test_transfer_booking_concurrent_race_runtime:1126 pattern:
      1. Seed business/master/client/workday + workday-only booking via s_seed.
      2. Capture A's stale snapshot (status='confirmed', old_start_at) BEFORE B's move.
      3. B wins fully: admin_move_booking(s_b, new_workday_b) — commits.
      4. A resumes with stale snapshot: admin_move_booking(s_a, new_workday_a).
         Service's UPDATE WHERE start_at=old_start_at runs against actual DB
         (now has B's new_start_at) → rowcount=0 → recheck status → not 'cancelled'
         → BookingAlreadyTransferredError.
      5. Verify final DB state: only B's move succeeded.

    Distinct from transfer race: admin_move has NO client_id pin, so the
    UPDATE WHERE clause is `id+status+start_at` (no client_id). Test still
    valid — start_at pin is the race-protection invariant.
    """
    # Step 1: Seed context + booking through s_seed (committed)
    async with session_factory_concurrent() as s_seed:
        biz = Business(
            name="Race Barbershop",
            telegram_owner_id=461355056,
            timezone="Europe/Moscow",
        )
        s_seed.add(biz)
        await s_seed.flush()
        master = Master(business_id=biz.id, name="Тестер", telegram_id=461355056, role="owner")
        s_seed.add(master)
        await s_seed.flush()
        client = Client(telegram_id=111222333, name="Паша")
        s_seed.add(client)
        await s_seed.flush()

        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        # Source workday (covers booking start_at)
        src_workday = WorkDay(
            master_id=master.id,
            work_date=tomorrow,
            start_time=dt_time(10, 0),
            end_time=dt_time(20, 0),
            max_concurrent_clients=1,
            is_active=True,
        )
        s_seed.add(src_workday)
        await s_seed.flush()

        # Two destination workdays (for A and B) — 5 and 6 days ahead.
        date_b = (datetime.now(UTC) + timedelta(days=5)).date()
        date_a = (datetime.now(UTC) + timedelta(days=6)).date()
        workday_b = WorkDay(
            master_id=master.id,
            work_date=date_b,
            start_time=dt_time(10, 0),
            end_time=dt_time(20, 0),
            max_concurrent_clients=1,
            is_active=True,
        )
        s_seed.add(workday_b)
        workday_a = WorkDay(
            master_id=master.id,
            work_date=date_a,
            start_time=dt_time(10, 0),
            end_time=dt_time(20, 0),
            max_concurrent_clients=1,
            is_active=True,
        )
        s_seed.add(workday_a)
        await s_seed.flush()

        # Create workday-only booking at 14:00 tomorrow
        booking_payload = BookingCreate(
            workday_id=src_workday.id,
            start_time_local=dt_time(14, 0),
            client_name="Паша",
            service_title="Стрижка",
            service_id=None,
        )
        result_seed = await create_booking(
            s_seed,
            booking_payload,
            business_id=biz.id,
            master_id=master.id,
            telegram_id=111222333,
        )
        booking_id = result_seed.booking_id
        new_workday_b_id = workday_b.id
        new_workday_a_id = workday_a.id
        await s_seed.commit()

    # Step 2: Capture A's stale snapshot BEFORE B's move
    async with session_factory_concurrent() as s_snapshot:
        stmt = select(Booking).where(Booking.id == booking_id)
        booking_snapshot = (await s_snapshot.execute(stmt)).scalar_one()
        stale_booking = Booking(
            id=booking_snapshot.id,
            client_id=booking_snapshot.client_id,
            business_id=booking_snapshot.business_id,
            master_id=booking_snapshot.master_id,
            slot_id=booking_snapshot.slot_id,
            start_at=booking_snapshot.start_at,  # STALE: old_start_at
            end_at=booking_snapshot.end_at,
            status=booking_snapshot.status,  # 'confirmed'
            service_id=booking_snapshot.service_id,
            client_name_snapshot=booking_snapshot.client_name_snapshot,
            service_title_snapshot=booking_snapshot.service_title_snapshot,
        )
        assert stale_booking.status == "confirmed"

    # Step 3: B wins fully — admin_move_booking(s_b, new_workday_b)
    async with session_factory_concurrent() as s_b:
        mock_scheduler_b = _mock_scheduler()
        result_b = await admin_move_booking(
            s_b,
            booking_id=booking_id,
            new_workday_id=new_workday_b_id,
            new_start_at_local=dt_time(15, 0),
            scheduler=mock_scheduler_b,
        )
        assert isinstance(result_b, AdminMoveResult)

    # DB now has: booking.status='transferred', start_at=B's new_start_at, slot_id=None

    # Step 4: A resumes with stale snapshot — admin_move_booking(s_a, new_workday_a)
    # Patch s_a's execute to return stale_booking on first SELECT FROM booking
    # (mirror test_transfer_booking_concurrent_race_runtime:1247-1296 pattern).
    # Without patch: A's SELECT would see B's committed state (start_at=new_B),
    # then UPDATE WHERE start_at=new_B → rowcount=1 → A succeeds (race condition
    # NOT caught). With patch: A's SELECT returns stale_booking (start_at=old),
    # UPDATE WHERE start_at=old runs against actual DB (start_at=new_B from B's
    # commit) → rowcount=0 → recheck status → not 'cancelled' →
    # BookingAlreadyTransferredError.
    from sqlalchemy.sql.selectable import Select

    async with session_factory_concurrent() as s_a:
        original_execute = s_a.execute
        patch_active = True

        async def patched_execute(statement, *args, **kwargs):
            nonlocal patch_active
            if patch_active and isinstance(statement, Select):
                try:
                    entities = [d.get("entity") for d in statement.column_descriptions]
                    if any(isinstance(e, type) and issubclass(e, Booking) for e in entities):
                        patch_active = False  # only first SELECT (booking lookup)

                        class _FakeResult:  # type: ignore[no-redef]
                            def scalar_one_or_none(self):
                                return stale_booking

                        return _FakeResult()
                except (KeyError, AttributeError, TypeError) as e:
                    raise AssertionError(
                        f"patched_execute entity detection failed: {e!r} — "
                        f"SQLAlchemy column_descriptions API may have changed"
                    ) from e
            return await original_execute(statement, *args, **kwargs)

        s_a.execute = patched_execute  # type: ignore[method-assign]
        try:
            mock_scheduler_a = _mock_scheduler()
            with pytest.raises(BookingAlreadyTransferredError):
                await admin_move_booking(
                    s_a,
                    booking_id=booking_id,
                    new_workday_id=new_workday_a_id,
                    new_start_at_local=dt_time(15, 30),
                    scheduler=mock_scheduler_a,
                )
        finally:
            s_a.execute = original_execute  # type: ignore[method-assign]

    # Step 5: Verify final DB state — only B's move succeeded.
    async with session_factory_concurrent() as s_verify:
        stmt = select(Booking).where(Booking.id == booking_id)
        booking_final = (await s_verify.execute(stmt)).scalar_one()
        assert booking_final.status == "transferred"
        # B's new_start_at (workday_b 5 days ahead, 15:00 LOCAL Moscow = 12:00 UTC)

        expected_b_start = _build_start_at_from_workday(
            # We don't have workday_b object here — re-fetch.
            (
                await s_verify.execute(select(WorkDay).where(WorkDay.id == new_workday_b_id))
            ).scalar_one(),
            dt_time(15, 0),
            "Europe/Moscow",
        )
        # SQLite stores naive — compare aware vs naive via .replace(tzinfo=UTC).
        assert booking_final.start_at.replace(tzinfo=UTC) == expected_b_start


# ============================================================
# 5. Scheduler side-effects
# ============================================================


@pytest.mark.asyncio
async def test_admin_move_scheduler_jobs_replaced(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Scheduler: remove_jobs_for_booking (old) + schedule_for_booking (new) called
    AFTER commit. mirror test_transfer_booking_happy_path:737-750.

    remove_job called twice (remind_24h + remind_1h), add_job called twice with
    new run_dates derived from new_start_at (NOT old_start_at).
    """
    booking = await _seed_workday_booking(session, seed_data, start_time_local=dt_time(14, 0))
    booking_id = booking.id
    old_start_at = booking.start_at  # naive UTC

    new_workday = await _make_workday(session, seed_data, days_ahead=5)
    mock_scheduler = _mock_scheduler()

    result = await admin_move_booking(
        session,
        booking_id=booking_id,
        new_workday_id=new_workday.id,
        new_start_at_local=dt_time(15, 30),
        scheduler=mock_scheduler,
    )

    # remove_job called for both old reminder jobs
    expected_remove = {f"remind_24h_{booking_id}", f"remind_1h_{booking_id}"}
    actual_remove = {call.args[0] for call in mock_scheduler.remove_job.call_args_list}
    assert actual_remove == expected_remove

    # add_job called twice (new remind_24h + new remind_1h)
    actual_add_ids = {call.kwargs.get("id") for call in mock_scheduler.add_job.call_args_list}
    assert actual_add_ids == {f"remind_24h_{booking_id}", f"remind_1h_{booking_id}"}

    # New run_dates derived from new_start_at, NOT old_start_at.
    old_start_at_aware = old_start_at.replace(tzinfo=UTC)
    for call in mock_scheduler.add_job.call_args_list:
        run_date = call.kwargs.get("run_date")
        assert run_date is not None
        assert run_date > old_start_at_aware  # new jobs scheduled AFTER old start_at
    assert result.new_start_at > old_start_at_aware
