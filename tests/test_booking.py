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
