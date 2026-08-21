"""Tests for bot.services.booking.create_booking + cancel_booking.

Coverage (Acceptance Contract):
- happy path: INSERT booking + slot.status='booked' via SELECT
- idempotency: повторный INSERT того же slot_id → SlotAlreadyBookedError
- SlotInPastError: start_at <= now
- SlotClosedError: slot.status='closed' or not found
- timezone conversion: slot.slot_hour LOCAL → start_at UTC
- html.escape: client_name_snapshot + service_title_snapshot — stored escaped

cancel_booking coverage (spec.md 41, 298, 317, 405-407):
- happy path: UPDATE booking.status='cancelled' + slot.status='open' + scheduler.remove_job
- CancelTooLateError: now >= start_at - CANCEL_MIN_HOURS (24h)
- BookingNotFoundError: booking belongs to another client (ownership check)
- BookingAlreadyCancelledError: double cancel (idempotent)
"""

import html
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bot.models import Booking, NotificationLog, Slot
from bot.schemas import BookingCreate
from bot.services.booking import (
    BookingAlreadyCancelledError,
    BookingCreatedData,
    BookingNotFoundError,
    CancelResult,
    CancelTooLateError,
    SlotAlreadyBookedError,
    SlotClosedError,
    SlotInPastError,
    cancel_booking,
    create_booking,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def _make_payload(slot_id: UUID) -> BookingCreate:
    return BookingCreate(
        slot_id=slot_id,
        client_name="Паша",
        service_title="Стрижка",
        service_id=None,
    )


@pytest.mark.asyncio
async def test_create_booking_happy_path(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Acceptance #2: INSERT booking + slot.status='booked' via SELECT."""
    slot = seed_data["slot"]
    payload = await _make_payload(slot.id)

    result = await create_booking(
        session,
        payload,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )

    # Acceptance: BookingCreatedData returned with all fields
    assert isinstance(result, BookingCreatedData)
    assert result.slot_id == slot.id
    assert result.master_id == seed_data["master_id"]
    assert result.business_id == seed_data["business_id"]
    assert result.master_notification_text  # non-empty

    # Acceptance #2: SELECT confirms booking row + slot.status='booked'
    await session.rollback()  # invalidate cache from this session
    stmt_b = select(Booking).where(Booking.id == result.booking_id)
    booking = (await session.execute(stmt_b)).scalar_one()
    assert booking.status == "confirmed"
    assert booking.slot_id == slot.id

    stmt_s = select(Slot.status).where(Slot.id == slot.id)
    slot_status = (await session.execute(stmt_s)).scalar_one()
    assert slot_status == "booked"


@pytest.mark.asyncio
async def test_create_booking_idempotency_unique_guard(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Double-booking same slot → second call raises SlotAlreadyBookedError (UNIQUE guard)."""
    slot = seed_data["slot"]
    payload = await _make_payload(slot.id)

    # First booking succeeds
    await create_booking(
        session,
        payload,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )

    # Second booking on same slot_id should raise
    payload2 = await _make_payload(slot.id)
    with pytest.raises(SlotAlreadyBookedError):
        await create_booking(
            session,
            payload2,
            business_id=seed_data["business_id"],
            master_id=seed_data["master_id"],
            telegram_id=seed_data["client_telegram_id"],
        )


@pytest.mark.asyncio
async def test_create_booking_slot_in_past(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """slot.start_at <= now() → SlotInPastError."""
    slot = seed_data["slot"]
    # Override slot_date to yesterday
    slot.slot_date = (datetime.now(UTC) - timedelta(days=1)).date()
    await session.commit()

    payload = await _make_payload(slot.id)
    with pytest.raises(SlotInPastError):
        await create_booking(
            session,
            payload,
            business_id=seed_data["business_id"],
            master_id=seed_data["master_id"],
            telegram_id=seed_data["client_telegram_id"],
        )


@pytest.mark.asyncio
async def test_create_booking_slot_closed(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """slot.status='closed' → SlotClosedError."""
    slot = seed_data["slot"]
    slot.status = "closed"
    await session.commit()

    payload = await _make_payload(slot.id)
    with pytest.raises(SlotClosedError):
        await create_booking(
            session,
            payload,
            business_id=seed_data["business_id"],
            master_id=seed_data["master_id"],
            telegram_id=seed_data["client_telegram_id"],
        )


@pytest.mark.asyncio
async def test_create_booking_slot_not_found(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Non-existent slot_id → SlotClosedError (slot not found)."""
    payload = await _make_payload(uuid4())
    with pytest.raises(SlotClosedError):
        await create_booking(
            session,
            payload,
            business_id=seed_data["business_id"],
            master_id=seed_data["master_id"],
            telegram_id=seed_data["client_telegram_id"],
        )


@pytest.mark.asyncio
async def test_create_booking_html_escape_name(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """client_name_snapshot and service_title_snapshot are html.escape()'d in service."""
    slot = seed_data["slot"]
    payload = BookingCreate(
        slot_id=slot.id,
        client_name="<script>alert(1)</script>",
        service_title="Стрижка <b>мужская</b>",
        service_id=None,
    )

    result = await create_booking(
        session,
        payload,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )

    expected_name = html.escape("<script>alert(1)</script>", quote=False)
    expected_service = html.escape("Стрижка <b>мужская</b>", quote=False)

    assert result.client_name_snapshot == expected_name
    assert result.service_title_snapshot == expected_service

    # Also verify stored in DB (escaped, not raw)
    await session.rollback()
    stmt = select(Booking).where(Booking.id == result.booking_id)
    booking = (await session.execute(stmt)).scalar_one()
    assert booking.client_name_snapshot == expected_name
    assert booking.service_title_snapshot == expected_service


@pytest.mark.asyncio
async def test_create_booking_timezone_conversion(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """slot.slot_hour (LOCAL in business.timezone) → start_at (UTC).

    slot_hour=14 (LOCAL Moscow = UTC+3) → start_at should be 11:00 UTC.
    """
    slot = seed_data["slot"]
    assert slot.slot_hour == 14

    payload = await _make_payload(slot.id)
    result = await create_booking(
        session,
        payload,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )

    # Moscow is UTC+3, so 14:00 local → 11:00 UTC
    assert result.start_at.hour == 11
    assert result.start_at.tzinfo is not None
    # End at should be start_at + 60 minutes (SERVICE_DEFAULT_DURATION_MIN)
    assert (result.end_at - result.start_at).total_seconds() == 3600


# ============================================================
# cancel_booking tests (spec.md 41, 298, 317, 405-407)
# ============================================================


def _mock_scheduler() -> MagicMock:
    """Build a MagicMock spec=AsyncIOScheduler for cancel_booking tests.

    `remove_job` is sync (not async), so MagicMock (not AsyncMock) is fine.
    spec=AsyncIOScheduler ensures attribute access matches real API (no typos).
    """
    return MagicMock(spec=AsyncIOScheduler)


async def _seed_confirmed_booking(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> Booking:
    """Create a confirmed booking via create_booking — returns the Booking row.

    Helper for cancel_booking tests: needs a persisted confirmed booking to cancel.
    Re-reads from DB after create_booking's commit to get a fresh attached row.
    """
    slot = seed_data["slot"]
    payload = await _make_payload(slot.id)
    result = await create_booking(
        session,
        payload,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )
    # Re-read booking (create_booking committed; session autoexpired under default
    # engine, but our test fixture uses expire_on_commit=False — booking attached).
    stmt = select(Booking).where(Booking.id == result.booking_id)
    booking = (await session.execute(stmt)).scalar_one()
    return booking


@pytest.mark.asyncio
async def test_cancel_booking_happy_path(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Spec.md 317: cancel sets booking.status='cancelled', slot.status='open',
    inserts NotificationLog(master_cancel), removes scheduler jobs for the booking.
    """
    booking = await _seed_confirmed_booking(session, seed_data)
    # Capture IDs before any rollback — `session.rollback()` expires attached
    # instances, so accessing `booking.id` afterwards triggers async lazy-load
    # (MissingGreenlet error in sync test context). Capture upfront, use after.
    booking_id = booking.id
    slot_id = booking.slot_id
    mock_scheduler = _mock_scheduler()

    # Inject now_utc well before the 24h deadline.
    # slot is tomorrow 14:00 MSK = tomorrow 11:00 UTC.
    # deadline = start_at - 24h = today 11:00 UTC. Use today 00:00 UTC → safe.
    ref = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    result = await cancel_booking(
        session,
        booking_id=booking_id,
        client_id=seed_data["client"].id,
        scheduler=mock_scheduler,
        now_utc=ref,
    )

    # Acceptance: CancelResult returned with all fields
    assert isinstance(result, CancelResult)
    assert result.booking_id == booking_id
    assert result.slot_id == slot_id
    assert result.master_notification_text.startswith("Отмена:")

    # Acceptance: booking.status='cancelled' (re-read after commit)
    await session.rollback()
    stmt_b = select(Booking).where(Booking.id == booking_id)
    booking_after = (await session.execute(stmt_b)).scalar_one()
    assert booking_after.status == "cancelled"

    # Acceptance: slot.status='open' (released for new bookings)
    stmt_s = select(Slot.status).where(Slot.id == slot_id)
    slot_status = (await session.execute(stmt_s)).scalar_one()
    assert slot_status == "open"

    # Acceptance: NotificationLog has exactly one master_cancel row (UNIQUE guard)
    stmt_n = (
        select(NotificationLog)
        .where(
            NotificationLog.booking_id == booking_id,
            NotificationLog.kind == "master_cancel",
        )
    )
    notif_rows = (await session.execute(stmt_n)).scalars().all()
    assert len(notif_rows) == 1

    # Acceptance: scheduler.remove_job called for both remind_24h and remind_1h
    expected_calls = {
        f"remind_24h_{booking_id}",
        f"remind_1h_{booking_id}",
    }
    actual_job_ids = {call.args[0] for call in mock_scheduler.remove_job.call_args_list}
    assert actual_job_ids == expected_calls


@pytest.mark.asyncio
async def test_cancel_booking_too_late(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Spec.md 406: now >= start_at - CANCEL_MIN_HOURS (24h) → CancelTooLateError,
    no DB changes, scheduler.remove_job NOT called.
    """
    booking = await _seed_confirmed_booking(session, seed_data)
    booking_id = booking.id  # capture before rollback
    slot_id = booking.slot_id
    mock_scheduler = _mock_scheduler()

    # slot is tomorrow 14:00 MSK = tomorrow 11:00 UTC.
    # deadline = start_at - 24h = today 11:00 UTC. Use today 23:59 UTC → past deadline.
    ref = datetime.now(UTC).replace(hour=23, minute=59, second=0, microsecond=0)

    with pytest.raises(CancelTooLateError):
        await cancel_booking(
            session,
            booking_id=booking_id,
            client_id=seed_data["client"].id,
            scheduler=mock_scheduler,
            now_utc=ref,
        )

    # No DB changes — booking.status still 'confirmed', slot.status still 'booked'.
    # cancel_booking raised before any UPDATE, so session has only read operations
    # pending — no rollback needed, just SELECT to verify state.
    stmt_b = select(Booking.status).where(Booking.id == booking_id)
    assert (await session.execute(stmt_b)).scalar_one() == "confirmed"
    stmt_s = select(Slot.status).where(Slot.id == slot_id)
    assert (await session.execute(stmt_s)).scalar_one() == "booked"

    # Scheduler NOT touched
    mock_scheduler.remove_job.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_booking_not_owner(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Spec.md 41: only the booking owner can cancel — booking_id from another
    client → BookingNotFoundError (same error as not-found — defense-in-depth,
    avoids leaking existence of bookings the caller doesn't own).
    """
    booking = await _seed_confirmed_booking(session, seed_data)
    booking_id = booking.id  # capture before rollback
    slot_id = booking.slot_id
    mock_scheduler = _mock_scheduler()
    ref = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    # Use a random client_id (not the booking's owner)
    stranger_client_id = uuid4()

    with pytest.raises(BookingNotFoundError):
        await cancel_booking(
            session,
            booking_id=booking_id,
            client_id=stranger_client_id,
            scheduler=mock_scheduler,
            now_utc=ref,
        )

    # No DB changes — cancel_booking raised BookingNotFoundError before any UPDATE,
    # so session is clean (only a SELECT was performed). No rollback needed.
    stmt_b = select(Booking.status).where(Booking.id == booking_id)
    assert (await session.execute(stmt_b)).scalar_one() == "confirmed"
    stmt_s = select(Slot.status).where(Slot.id == slot_id)
    assert (await session.execute(stmt_s)).scalar_one() == "booked"
    mock_scheduler.remove_job.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_booking_already_cancelled(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Idempotent cancel: second call on already-cancelled booking raises
    BookingAlreadyCancelledError (catches double-click and concurrent cancels).
    """
    booking = await _seed_confirmed_booking(session, seed_data)
    booking_id = booking.id  # capture before any rollback
    mock_scheduler = _mock_scheduler()
    ref = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    # First cancel succeeds
    result1 = await cancel_booking(
        session,
        booking_id=booking_id,
        client_id=seed_data["client"].id,
        scheduler=mock_scheduler,
        now_utc=ref,
    )
    assert isinstance(result1, CancelResult)

    # Second cancel on the same booking → BookingAlreadyCancelledError
    # (status='cancelled' fails the SELECT-stage fast-path check)
    with pytest.raises(BookingAlreadyCancelledError):
        await cancel_booking(
            session,
            booking_id=booking_id,
            client_id=seed_data["client"].id,
            scheduler=mock_scheduler,
            now_utc=ref,
        )

    # scheduler.remove_job called twice total (once per first cancel — 2 jobs),
    # NOT a third+fourth time on the failed second cancel.
    assert mock_scheduler.remove_job.call_count == 2


# ============================================================
# cancel_booking edge cases (NEXT_SESSION_PROMPT.md service gaps)
# ============================================================

@pytest.mark.asyncio
async def test_cancel_booking_not_found_random_uuid(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Random uuid4 (no such booking in DB) → BookingNotFoundError.

    Distinct from `test_cancel_booking_not_owner` (which seeds a booking and
    uses a stranger client_id → also raises BookingNotFoundError via the same
    `WHERE id=? AND client_id=?` clause). Defense-in-depth: same error class
    for both cases — caller cannot distinguish "no such booking" from
    "not your booking" (avoids leaking existence of bookings).
    """
    mock_scheduler = _mock_scheduler()
    ref = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    # No booking seeded — random uuid4 hits "no row matches WHERE id=?" path.
    with pytest.raises(BookingNotFoundError):
        await cancel_booking(
            session,
            booking_id=uuid4(),
            client_id=seed_data["client"].id,
            scheduler=mock_scheduler,
            now_utc=ref,
        )

    # No DB changes (only SELECT performed — no rollback needed).
    mock_scheduler.remove_job.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_booking_transferred_status(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Booking.status='transferred' (NOT 'confirmed') → cancel succeeds.

    Spec.md 41: cancel/transfer allowed for status IN ('confirmed', 'transferred').
    cancel_booking UPDATE WHERE status IN ('confirmed','transferred') must accept
    'transferred' — the same rule as 'confirmed' (pending Блок 3 часть 3 introduces
    transfer; this test locks the contract ahead of implementation).
    """
    slot = seed_data["slot"]
    # Build naive UTC start_at matching slot.slot_hour LOCAL Moscow (14:00 MSK = 11:00 UTC).
    # Pattern from test_admin.py `_utc_naive` (round-trip aware → naive for SQLite storage).
    from datetime import time as dtime
    from zoneinfo import ZoneInfo

    local_dt = datetime.combine(
        slot.slot_date, dtime(hour=slot.slot_hour), tzinfo=ZoneInfo("Europe/Moscow")
    )
    start_at_naive = local_dt.astimezone(UTC).replace(tzinfo=None)
    booking = Booking(
        slot_id=slot.id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        client_id=seed_data["client"].id,
        service_id=None,
        service_title_snapshot="Стрижка",
        service_price_snapshot=None,
        client_name_snapshot="Паша",
        start_at=start_at_naive,
        end_at=start_at_naive + timedelta(minutes=60),
        status="transferred",
    )
    # Set slot.status='booked' to match transferred-booking invariant
    # (close_slot refuses to close a booked slot → during booking lifetime
    # slot.status is always 'booked' regardless of booking.status).
    slot.status = "booked"
    session.add(booking)
    await session.commit()
    booking_id = booking.id
    slot_id = slot.id

    mock_scheduler = _mock_scheduler()
    # Use far-past ref to ensure deadline check passes (start_at > 24h from ref).
    ref = datetime.now(UTC) - timedelta(days=10)

    result = await cancel_booking(
        session,
        booking_id=booking_id,
        client_id=seed_data["client"].id,
        scheduler=mock_scheduler,
        now_utc=ref,
    )

    assert isinstance(result, CancelResult)
    assert result.booking_id == booking_id
    assert result.slot_id == slot_id

    # Booking now 'cancelled' (UPDATE WHERE status IN ('confirmed','transferred') matched).
    await session.rollback()
    stmt_b = select(Booking).where(Booking.id == booking_id)
    booking_after = (await session.execute(stmt_b)).scalar_one()
    assert booking_after.status == "cancelled"

    # Slot released back to 'open' (mirrors confirmed-booking cancel path).
    stmt_s = select(Slot.status).where(Slot.id == slot_id)
    assert (await session.execute(stmt_s)).scalar_one() == "open"


@pytest.mark.asyncio
async def test_cancel_booking_now_utc_default_production_path(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """now_utc=None (default → datetime.now(UTC) inside service).

    Production path: caller omits now_utc, service uses real current time.
    To pass the 24h-rule check, slot.start_at must be >24h from now → use
    a slot 48h ahead (seed_data slot is only ~24h ahead — too borderline).

    This test exercises the default-arg branch (line booking.py:314 `now_utc or datetime.now(UTC)`)
    to ensure no TypeError when ref has tzinfo=UTC (must be stripped to naive
    before comparison with booking.start_at naive-from-SQLite).
    """
    # Build a slot 48h ahead so deadline = start_at - 24h > now (any time of day).
    future_date = (datetime.now(UTC) + timedelta(hours=48)).date()
    slot = Slot(
        master_id=seed_data["master_id"],
        slot_date=future_date,
        slot_hour=14,
        status="open",
    )
    session.add(slot)
    await session.commit()

    payload = await _make_payload(slot.id)
    create_result = await create_booking(
        session,
        payload,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )
    booking_id = create_result.booking_id

    mock_scheduler = _mock_scheduler()

    # now_utc=None → service calls datetime.now(UTC) internally.
    # Booking start_at is ~48h ahead, so deadline (~24h ahead) is well in the future.
    result = await cancel_booking(
        session,
        booking_id=booking_id,
        client_id=seed_data["client"].id,
        scheduler=mock_scheduler,
        now_utc=None,
    )

    assert isinstance(result, CancelResult)
    assert result.booking_id == booking_id
    # Booking cancelled (production default path works end-to-end).
    await session.rollback()
    stmt_b = select(Booking.status).where(Booking.id == booking_id)
    assert (await session.execute(stmt_b)).scalar_one() == "cancelled"
    # Scheduler jobs removed (default now_utc doesn't skip cleanup).
    expected_calls = {f"remind_24h_{booking_id}", f"remind_1h_{booking_id}"}
    actual_job_ids = {call.args[0] for call in mock_scheduler.remove_job.call_args_list}
    assert actual_job_ids == expected_calls


# ============================================================
# transfer_booking tests (spec.md 41, 318, 408-409 — Блок 3 часть 3)
# ============================================================

from bot.services.booking import (  # noqa: E402 — local import to keep transfer section self-contained
    BookingAlreadyTransferredError,
    SlotNotAvailableError,
    TransferResult,
    transfer_booking,
)


async def _make_open_slot(
    session: AsyncSession,
    seed_data: dict[str, Any],
    *,
    days_ahead: int = 5,
    hour_local: int = 15,
) -> Slot:
    """Insert a new open Slot N days ahead at HOUR LOCAL Moscow → for transfer tests
    that need a destination slot distinct from the booking's source slot.

    Default: 5 days ahead at 15:00 MSK = 12:00 UTC. Caller can override days_ahead
    or hour_local for slot_in_past / slot_in_future edge cases.
    """
    slot_date = (datetime.now(UTC) + timedelta(days=days_ahead)).date()
    slot = Slot(
        master_id=seed_data["master_id"],
        slot_date=slot_date,
        slot_hour=hour_local,
        status="open",
    )
    session.add(slot)
    await session.commit()
    return slot


@pytest.mark.asyncio
async def test_transfer_booking_happy_path(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Spec.md 318: transfer UPDATEs booking (slot_id, start_at, end_at, status='transferred'),
    releases old slot to 'open', books new slot to 'booked', logs master_transfer,
    scheduler removes old jobs + schedules new jobs at new start_at.
    """
    booking = await _seed_confirmed_booking(session, seed_data)
    booking_id = booking.id
    old_slot_id = booking.slot_id
    old_start_at = booking.start_at  # naive UTC, captured for assertion

    new_slot = await _make_open_slot(session, seed_data, days_ahead=5, hour_local=15)
    new_slot_id = new_slot.id

    mock_scheduler = _mock_scheduler()
    # ref far in the past → 24h rule passes (start_at - 24h is well in the future from ref).
    ref = datetime.now(UTC) - timedelta(days=10)

    result = await transfer_booking(
        session,
        booking_id=booking_id,
        new_slot_id=new_slot_id,
        client_id=seed_data["client"].id,
        scheduler=mock_scheduler,
        now_utc=ref,
    )

    # Acceptance: TransferResult has all fields, old + new start_at.
    assert isinstance(result, TransferResult)
    assert result.booking_id == booking_id
    assert result.old_slot_id == old_slot_id
    assert result.new_slot_id == new_slot_id
    assert result.old_start_at == old_start_at
    assert result.new_start_at != old_start_at
    # Master notification text follows spec.md 318: "Перенос: <old> → <new>".
    assert result.master_notification_text.startswith("Перенос:")
    assert "→" in result.master_notification_text

    # DB: booking now 'transferred', slot_id and start_at updated.
    await session.rollback()
    stmt_b = select(Booking).where(Booking.id == booking_id)
    booking_after = (await session.execute(stmt_b)).scalar_one()
    assert booking_after.status == "transferred"
    assert booking_after.slot_id == new_slot_id
    assert booking_after.start_at != old_start_at
    # New start_at: slot.slot_hour=15 LOCAL MSK = 12:00 UTC.
    assert booking_after.start_at.hour == 12

    # DB: old slot released to 'open'.
    stmt_old = select(Slot.status).where(Slot.id == old_slot_id)
    assert (await session.execute(stmt_old)).scalar_one() == "open"

    # DB: new slot booked.
    stmt_new = select(Slot.status).where(Slot.id == new_slot_id)
    assert (await session.execute(stmt_new)).scalar_one() == "booked"

    # NotificationLog master_transfer exactly one row (UNIQUE guard).
    stmt_n = (
        select(NotificationLog)
        .where(
            NotificationLog.booking_id == booking_id,
            NotificationLog.kind == "master_transfer",
        )
    )
    notif_rows = (await session.execute(stmt_n)).scalars().all()
    assert len(notif_rows) == 1

    # Scheduler: remove_job called for both old reminder jobs.
    expected_remove = {f"remind_24h_{booking_id}", f"remind_1h_{booking_id}"}
    actual_remove = {call.args[0] for call in mock_scheduler.remove_job.call_args_list}
    assert actual_remove == expected_remove

    # Scheduler: add_job called twice (new remind_24h + new remind_1h) via schedule_for_booking.
    actual_add_ids = {call.kwargs.get("id") for call in mock_scheduler.add_job.call_args_list}
    assert actual_add_ids == {f"remind_24h_{booking_id}", f"remind_1h_{booking_id}"}
    # New run_date derived from new_start_at, NOT old_start_at.
    # old_start_at is naive UTC (SQLite); run_date is aware UTC (scheduler API).
    old_start_at_aware = old_start_at.replace(tzinfo=UTC)
    for call in mock_scheduler.add_job.call_args_list:
        run_date = call.kwargs.get("run_date")
        assert run_date is not None
        assert run_date > old_start_at_aware  # new jobs scheduled AFTER old start_at


@pytest.mark.asyncio
async def test_transfer_booking_re_transfer_from_transferred_status(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Re-transfer: booking.status='transferred' (from previous transfer) →
    new transfer succeeds. Status IN ('confirmed','transferred') accepts 'transferred'.
    Validates that the same booking can be moved multiple times (spec.md 318 —
    one booking row, status flows confirmed→transferred→transferred).
    """
    # First transfer: confirmed → transferred (using happy-path fixture logic inline).
    booking = await _seed_confirmed_booking(session, seed_data)
    booking_id = booking.id
    old_slot_id_v1 = booking.slot_id

    new_slot_v1 = await _make_open_slot(session, seed_data, days_ahead=5, hour_local=15)
    mock_scheduler = _mock_scheduler()
    ref = datetime.now(UTC) - timedelta(days=10)
    await transfer_booking(
        session, booking_id=booking_id, new_slot_id=new_slot_v1.id,
        client_id=seed_data["client"].id, scheduler=mock_scheduler, now_utc=ref,
    )

    # Second transfer: transferred → transferred (different new_slot).
    await session.rollback()  # invalidate stale booking object
    booking_v2 = (await session.execute(
        select(Booking).where(Booking.id == booking_id)
    )).scalar_one()
    assert booking_v2.status == "transferred"
    old_slot_id_v2 = booking_v2.slot_id  # new_slot_v1.id (from previous transfer)
    assert old_slot_id_v2 == new_slot_v1.id

    new_slot_v2 = await _make_open_slot(session, seed_data, days_ahead=7, hour_local=16)
    mock_scheduler_v2 = _mock_scheduler()
    result_v2 = await transfer_booking(
        session, booking_id=booking_id, new_slot_id=new_slot_v2.id,
        client_id=seed_data["client"].id, scheduler=mock_scheduler_v2, now_utc=ref,
    )

    assert isinstance(result_v2, TransferResult)
    assert result_v2.old_slot_id == old_slot_id_v2
    assert result_v2.new_slot_id == new_slot_v2.id

    # DB: still 'transferred' (not double-marked), slot_id = newest.
    await session.rollback()
    booking_after = (await session.execute(
        select(Booking).where(Booking.id == booking_id)
    )).scalar_one()
    assert booking_after.status == "transferred"
    assert booking_after.slot_id == new_slot_v2.id

    # Slots: original (v0) is 'open' (released in first transfer), v1 is 'open'
    # (released in second transfer), v2 is 'booked' (current).
    slot_v0_status = (await session.execute(
        select(Slot.status).where(Slot.id == old_slot_id_v1)
    )).scalar_one()
    slot_v1_status = (await session.execute(
        select(Slot.status).where(Slot.id == new_slot_v1.id)
    )).scalar_one()
    slot_v2_status = (await session.execute(
        select(Slot.status).where(Slot.id == new_slot_v2.id)
    )).scalar_one()
    assert slot_v0_status == "open"
    assert slot_v1_status == "open"
    assert slot_v2_status == "booked"

    # master_transfer still exactly one row (UNIQUE guard, idempotent SAVEPOINT).
    notif_count = len((await session.execute(
        select(NotificationLog).where(
            NotificationLog.booking_id == booking_id,
            NotificationLog.kind == "master_transfer",
        )
    )).scalars().all())
    assert notif_count == 1


@pytest.mark.asyncio
async def test_transfer_booking_too_late(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """now >= start_at - CANCEL_MIN_HOURS → CancelTooLateError (24h rule).
    Re-uses cancel_booking's error (same rule per spec.md 41 — "перенос (>24ч)").
    """
    booking = await _seed_confirmed_booking(session, seed_data)
    booking_id = booking.id
    new_slot = await _make_open_slot(session, seed_data, days_ahead=5, hour_local=15)

    mock_scheduler = _mock_scheduler()
    # ref today 23:59 UTC → past deadline (= tomorrow 11:00 UTC - 24h = today 11:00 UTC).
    ref = datetime.now(UTC).replace(hour=23, minute=59, second=0, microsecond=0)

    with pytest.raises(CancelTooLateError):
        await transfer_booking(
            session, booking_id=booking_id, new_slot_id=new_slot.id,
            client_id=seed_data["client"].id, scheduler=mock_scheduler, now_utc=ref,
        )

    # No DB changes — booking still 'confirmed', old slot still 'booked', new slot still 'open'.
    stmt_b = select(Booking.status).where(Booking.id == booking_id)
    assert (await session.execute(stmt_b)).scalar_one() == "confirmed"
    stmt_new = select(Slot.status).where(Slot.id == new_slot.id)
    assert (await session.execute(stmt_new)).scalar_one() == "open"
    # Scheduler NOT touched.
    mock_scheduler.remove_job.assert_not_called()
    mock_scheduler.add_job.assert_not_called()


@pytest.mark.asyncio
async def test_transfer_booking_not_found(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Random uuid4 (no such booking) → BookingNotFoundError (defense-in-depth,
    same error class as not_owner — caller cannot distinguish).
    """
    new_slot = await _make_open_slot(session, seed_data, days_ahead=5, hour_local=15)
    mock_scheduler = _mock_scheduler()
    ref = datetime.now(UTC) - timedelta(days=10)

    with pytest.raises(BookingNotFoundError):
        await transfer_booking(
            session, booking_id=uuid4(), new_slot_id=new_slot.id,
            client_id=seed_data["client"].id, scheduler=mock_scheduler, now_utc=ref,
        )
    mock_scheduler.remove_job.assert_not_called()


@pytest.mark.asyncio
async def test_transfer_booking_not_owner(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Booking belongs to another client (stranger client_id) → BookingNotFoundError.
    Same defense-in-depth as cancel_booking: WHERE id=? AND client_id=? returns None.
    """
    booking = await _seed_confirmed_booking(session, seed_data)
    booking_id = booking.id
    new_slot = await _make_open_slot(session, seed_data, days_ahead=5, hour_local=15)
    mock_scheduler = _mock_scheduler()
    ref = datetime.now(UTC) - timedelta(days=10)
    stranger_client_id = uuid4()

    with pytest.raises(BookingNotFoundError):
        await transfer_booking(
            session, booking_id=booking_id, new_slot_id=new_slot.id,
            client_id=stranger_client_id, scheduler=mock_scheduler, now_utc=ref,
        )
    # No DB changes (only SELECT performed, no rollback needed).
    stmt_b = select(Booking.status).where(Booking.id == booking_id)
    assert (await session.execute(stmt_b)).scalar_one() == "confirmed"
    mock_scheduler.remove_job.assert_not_called()


@pytest.mark.asyncio
async def test_transfer_booking_already_cancelled(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Booking.status='cancelled' → BookingAlreadyCancelledError (defensive —
    cannot transfer a cancelled booking; user must re-book via /book).
    """
    booking = await _seed_confirmed_booking(session, seed_data)
    booking_id = booking.id
    new_slot = await _make_open_slot(session, seed_data, days_ahead=5, hour_local=15)
    new_slot_id = new_slot.id  # capture before any rollback — rollback expires objects
    ref = datetime.now(UTC) - timedelta(days=10)

    # First: cancel the booking (service sets status='cancelled').
    await cancel_booking(
        session, booking_id=booking_id, client_id=seed_data["client"].id,
        scheduler=_mock_scheduler(), now_utc=ref,
    )

    # Then: try to transfer the cancelled booking → must raise.
    mock_scheduler_v2 = _mock_scheduler()
    with pytest.raises(BookingAlreadyCancelledError):
        await transfer_booking(
            session, booking_id=booking_id, new_slot_id=new_slot_id,
            client_id=seed_data["client"].id, scheduler=mock_scheduler_v2, now_utc=ref,
        )
    # No DB changes from transfer attempt — booking still 'cancelled'.
    await session.rollback()
    booking_status = (await session.execute(
        select(Booking.status).where(Booking.id == booking_id)
    )).scalar_one()
    assert booking_status == "cancelled"
    # New slot was not touched by transfer (status='open' or default).
    new_slot_status = (await session.execute(
        select(Slot.status).where(Slot.id == new_slot_id)
    )).scalar_one()
    assert new_slot_status == "open"
    mock_scheduler_v2.remove_job.assert_not_called()


@pytest.mark.asyncio
async def test_transfer_booking_slot_in_past(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """New slot start_at <= now → SlotInPastError (re-use create_booking error).
    Validates that transfer cannot move a booking to a past time.
    """
    booking = await _seed_confirmed_booking(session, seed_data)
    booking_id = booking.id
    # New slot in the past: yesterday at 14:00 LOCAL MSK = yesterday 11:00 UTC.
    past_date = (datetime.now(UTC) - timedelta(days=1)).date()
    past_slot = Slot(
        master_id=seed_data["master_id"],
        slot_date=past_date,
        slot_hour=14,
        status="open",
    )
    session.add(past_slot)
    await session.commit()

    mock_scheduler = _mock_scheduler()
    ref = datetime.now(UTC) - timedelta(days=10)

    with pytest.raises(SlotInPastError):
        await transfer_booking(
            session, booking_id=booking_id, new_slot_id=past_slot.id,
            client_id=seed_data["client"].id, scheduler=mock_scheduler, now_utc=ref,
        )
    # No DB changes — booking still 'confirmed', past slot still 'open'.
    stmt_b = select(Booking.status).where(Booking.id == booking_id)
    assert (await session.execute(stmt_b)).scalar_one() == "confirmed"
    stmt_past = select(Slot.status).where(Slot.id == past_slot.id)
    assert (await session.execute(stmt_past)).scalar_one() == "open"
    mock_scheduler.remove_job.assert_not_called()


@pytest.mark.asyncio
async def test_transfer_booking_slot_closed(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """New slot.status='closed' → SlotClosedError (slot closed by master, not available).
    Re-uses create_booking's _select_open_slot helper which raises SlotClosedError.
    """
    booking = await _seed_confirmed_booking(session, seed_data)
    booking_id = booking.id
    closed_slot = await _make_open_slot(session, seed_data, days_ahead=5, hour_local=15)
    closed_slot.status = "closed"
    await session.commit()

    mock_scheduler = _mock_scheduler()
    ref = datetime.now(UTC) - timedelta(days=10)

    with pytest.raises(SlotClosedError):
        await transfer_booking(
            session, booking_id=booking_id, new_slot_id=closed_slot.id,
            client_id=seed_data["client"].id, scheduler=mock_scheduler, now_utc=ref,
        )
    stmt_b = select(Booking.status).where(Booking.id == booking_id)
    assert (await session.execute(stmt_b)).scalar_one() == "confirmed"
    mock_scheduler.remove_job.assert_not_called()


@pytest.mark.asyncio
async def test_transfer_booking_slot_already_booked(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """New slot.status='booked' → SlotAlreadyBookedError (slot taken before transfer).
    Re-uses create_booking's _select_open_slot helper which raises SlotAlreadyBookedError.
    """
    booking = await _seed_confirmed_booking(session, seed_data)
    booking_id = booking.id
    booked_slot = await _make_open_slot(session, seed_data, days_ahead=5, hour_local=15)
    booked_slot.status = "booked"
    await session.commit()

    mock_scheduler = _mock_scheduler()
    ref = datetime.now(UTC) - timedelta(days=10)

    with pytest.raises(SlotAlreadyBookedError):
        await transfer_booking(
            session, booking_id=booking_id, new_slot_id=booked_slot.id,
            client_id=seed_data["client"].id, scheduler=mock_scheduler, now_utc=ref,
        )
    stmt_b = select(Booking.status).where(Booking.id == booking_id)
    assert (await session.execute(stmt_b)).scalar_one() == "confirmed"
    mock_scheduler.remove_job.assert_not_called()


@pytest.mark.asyncio
async def test_transfer_booking_concurrent_transfer_race_protection(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Deep-analysis Pass 3 [blocker] finding: concurrent transfer race protection.

    Static-invariant lock: confirms transfer_booking service has `Booking.start_at ==`
    in the UPDATE WHERE clause. Without this pin, two concurrent transfers on the same
    booking could both match status IN ('confirmed','transferred') and overwrite each
    other (loser's UPDATE rowcount=1 instead of 0 → no BookingAlreadyTransferredError).

    A faithful runtime test needs 2 sessions + asyncio.gather (callers A and B both
    SELECT at T0, B wins at T1, A's UPDATE at T2 has WHERE start_at=OLD → rowcount=0).
    That's deferred to NEXT_SESSION_PROMPT — for MVP we lock the invariant statically.

    We DO seed a booking to validate the service loads cleanly with a real row, but
    we don't invoke transfer_booking on it (the static check is the protection test).
    """
    booking = await _seed_confirmed_booking(session, seed_data)
    booking_id = booking.id

    # Sanity: booking exists and is 'confirmed' (transfer-eligible).
    assert booking_id is not None

    # Static check: confirm transfer_booking service has start_at in WHERE clause.
    import inspect

    from bot.services import booking as booking_svc

    source = inspect.getsource(booking_svc.transfer_booking)
    assert "Booking.start_at ==" in source, (
        "transfer_booking UPDATE must include start_at in WHERE clause for "
        "concurrent-transfer race protection (deep-analysis Pass 3 [blocker] finding)"
    )

    # Sanity: BookingAlreadyTransferredError is exported (handler maps it to user-facing text).
    assert BookingAlreadyTransferredError is not None
    assert SlotNotAvailableError is not None
