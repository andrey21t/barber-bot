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
from bot.models import Booking, Business, Client, Master, NotificationLog, Slot
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.selectable import Select


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
    stmt_n = select(NotificationLog).where(
        NotificationLog.booking_id == booking_id,
        NotificationLog.kind == "master_cancel",
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
    to ensure no TypeError with the cross-DB aware-aware comparison: ref is aware UTC
    (datetime.now(UTC) default), booking.start_at.replace(tzinfo=UTC) injects tzinfo
    on DB-read side (no-op on Postgres where already aware, makes naive aware on SQLite).
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
    # result.old_start_at is aware UTC (service marks naive SQLite value as UTC
    # for cross-system TZ correctness — see booking.py step 12 comment).
    # Test's local old_start_at is naive (from booking.start_at); compare aware.
    assert isinstance(result, TransferResult)
    assert result.booking_id == booking_id
    assert result.old_slot_id == old_slot_id
    assert result.new_slot_id == new_slot_id
    assert result.old_start_at == old_start_at.replace(tzinfo=UTC)
    assert result.new_start_at != old_start_at.replace(tzinfo=UTC)
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
    stmt_n = select(NotificationLog).where(
        NotificationLog.booking_id == booking_id,
        NotificationLog.kind == "master_transfer",
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
        session,
        booking_id=booking_id,
        new_slot_id=new_slot_v1.id,
        client_id=seed_data["client"].id,
        scheduler=mock_scheduler,
        now_utc=ref,
    )

    # Second transfer: transferred → transferred (different new_slot).
    await session.rollback()  # invalidate stale booking object
    booking_v2 = (
        await session.execute(select(Booking).where(Booking.id == booking_id))
    ).scalar_one()
    assert booking_v2.status == "transferred"
    old_slot_id_v2 = booking_v2.slot_id  # new_slot_v1.id (from previous transfer)
    assert old_slot_id_v2 == new_slot_v1.id

    new_slot_v2 = await _make_open_slot(session, seed_data, days_ahead=7, hour_local=16)
    mock_scheduler_v2 = _mock_scheduler()
    result_v2 = await transfer_booking(
        session,
        booking_id=booking_id,
        new_slot_id=new_slot_v2.id,
        client_id=seed_data["client"].id,
        scheduler=mock_scheduler_v2,
        now_utc=ref,
    )

    assert isinstance(result_v2, TransferResult)
    assert result_v2.old_slot_id == old_slot_id_v2
    assert result_v2.new_slot_id == new_slot_v2.id

    # DB: still 'transferred' (not double-marked), slot_id = newest.
    await session.rollback()
    booking_after = (
        await session.execute(select(Booking).where(Booking.id == booking_id))
    ).scalar_one()
    assert booking_after.status == "transferred"
    assert booking_after.slot_id == new_slot_v2.id

    # Slots: original (v0) is 'open' (released in first transfer), v1 is 'open'
    # (released in second transfer), v2 is 'booked' (current).
    slot_v0_status = (
        await session.execute(select(Slot.status).where(Slot.id == old_slot_id_v1))
    ).scalar_one()
    slot_v1_status = (
        await session.execute(select(Slot.status).where(Slot.id == new_slot_v1.id))
    ).scalar_one()
    slot_v2_status = (
        await session.execute(select(Slot.status).where(Slot.id == new_slot_v2.id))
    ).scalar_one()
    assert slot_v0_status == "open"
    assert slot_v1_status == "open"
    assert slot_v2_status == "booked"

    # master_transfer still exactly one row (UNIQUE guard, idempotent SAVEPOINT).
    notif_count = len(
        (
            await session.execute(
                select(NotificationLog).where(
                    NotificationLog.booking_id == booking_id,
                    NotificationLog.kind == "master_transfer",
                )
            )
        )
        .scalars()
        .all()
    )
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
            session,
            booking_id=booking_id,
            new_slot_id=new_slot.id,
            client_id=seed_data["client"].id,
            scheduler=mock_scheduler,
            now_utc=ref,
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
            session,
            booking_id=uuid4(),
            new_slot_id=new_slot.id,
            client_id=seed_data["client"].id,
            scheduler=mock_scheduler,
            now_utc=ref,
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
            session,
            booking_id=booking_id,
            new_slot_id=new_slot.id,
            client_id=stranger_client_id,
            scheduler=mock_scheduler,
            now_utc=ref,
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
        session,
        booking_id=booking_id,
        client_id=seed_data["client"].id,
        scheduler=_mock_scheduler(),
        now_utc=ref,
    )

    # Then: try to transfer the cancelled booking → must raise.
    mock_scheduler_v2 = _mock_scheduler()
    with pytest.raises(BookingAlreadyCancelledError):
        await transfer_booking(
            session,
            booking_id=booking_id,
            new_slot_id=new_slot_id,
            client_id=seed_data["client"].id,
            scheduler=mock_scheduler_v2,
            now_utc=ref,
        )
    # No DB changes from transfer attempt — booking still 'cancelled'.
    await session.rollback()
    booking_status = (
        await session.execute(select(Booking.status).where(Booking.id == booking_id))
    ).scalar_one()
    assert booking_status == "cancelled"
    # New slot was not touched by transfer (status='open' or default).
    new_slot_status = (
        await session.execute(select(Slot.status).where(Slot.id == new_slot_id))
    ).scalar_one()
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
            session,
            booking_id=booking_id,
            new_slot_id=past_slot.id,
            client_id=seed_data["client"].id,
            scheduler=mock_scheduler,
            now_utc=ref,
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
            session,
            booking_id=booking_id,
            new_slot_id=closed_slot.id,
            client_id=seed_data["client"].id,
            scheduler=mock_scheduler,
            now_utc=ref,
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
            session,
            booking_id=booking_id,
            new_slot_id=booked_slot.id,
            client_id=seed_data["client"].id,
            scheduler=mock_scheduler,
            now_utc=ref,
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


@pytest.mark.asyncio
async def test_transfer_booking_concurrent_race_runtime(
    session_factory_concurrent: async_sessionmaker[AsyncSession],
) -> None:
    """Runtime race protection — faithful replacement of static-invariant lock.

    Closes Pass 3 [blocker] finding: verifies transfer_booking's WHERE-clause
    pin (`Booking.start_at == old_start_at` in UPDATE WHERE) actually rejects
    the loser at runtime, not just by inspect.getsource.

    Approach (deterministic, no asyncio.gather):
      1. Seed business/master/client/2 new slots through s_seed (committed).
         Create confirmed booking via _seed_confirmed_booking (committed).
      2. Open s_snapshot — SELECT booking, capture stale snapshot (status=
         'confirmed', start_at=old_start_at) BEFORE B's concurrent transfer.
         Build a detached stale_booking Booking object with these fields.
      3. Open s_b — call transfer_booking(s_b, ...) with new_slot_b. B wins
         fully: SELECT (sees committed 'confirmed'), UPDATE (rowcount=1),
         commit. DB now has booking with status='transferred', start_at=
         new_B_start_at.
      4. Open s_a — patch its execute to return stale_booking on first
         SELECT FROM booking (simulates A captured snapshot before B's
         commit, then resumes). Call transfer_booking(s_a, ...) with
         new_slot_a. Service's SELECT returns stale_booking (status=
         'confirmed', start_at=old_start_at). Service's UPDATE WHERE
         start_at=old_start_at runs against actual DB (now has new_B_start_at
         from B's commit) → rowcount=0 → recheck status → not 'cancelled'
         → raise BookingAlreadyTransferredError.
      5. Verify final DB state: only B's transfer succeeded.

    Faithfulness: tests the actual service's UPDATE WHERE clause behavior at
    runtime. If `Booking.start_at ==` were removed from WHERE, A's UPDATE
    would match on id+client_id+status (status IN ('confirmed','transferred')
    matches 'transferred' too) and succeed (loser would overwrite winner —
    race condition not caught).

    Why not asyncio.gather: asyncio.gather doesn't guarantee both SELECTs
    happen before either UPDATE — A might fully win before B starts (B then
    re-transfers successfully, race not triggered). Manual orchestration via
    patched SELECT gives deterministic race scenario.

    Why file-based SQLite (not in-memory): default in-memory + QueuePool gives
    each connection its own DB (sessions can't share state). File-based allows
    multiple connections to same DB — B's commit is visible to A's UPDATE.
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
        slot = Slot(master_id=master.id, slot_date=tomorrow, slot_hour=14, status="open")
        s_seed.add(slot)
        await s_seed.commit()
        seed_data_local: dict[str, Any] = {
            "business": biz,
            "master": master,
            "client": client,
            "slot": slot,
            "business_id": biz.id,
            "master_id": master.id,
            "client_telegram_id": 111222333,
            "slot_date": tomorrow,
        }
        booking = await _seed_confirmed_booking(s_seed, seed_data_local)
        new_slot_b = await _make_open_slot(s_seed, seed_data_local, days_ahead=5, hour_local=16)
        new_slot_a = await _make_open_slot(s_seed, seed_data_local, days_ahead=6, hour_local=15)
        await s_seed.commit()
        booking_id = booking.id
        new_slot_b_id = new_slot_b.id
        new_slot_a_id = new_slot_a.id
        client_id = client.id

    # Step 2: Capture A's stale snapshot BEFORE B's transfer
    async with session_factory_concurrent() as s_snapshot:
        stmt = select(Booking).where(Booking.id == booking_id)
        booking_snapshot = (await s_snapshot.execute(stmt)).scalar_one()
        # Build detached stale_booking object with snapshot fields (simulates
        # A's service-internal SELECT returning the row captured before B's
        # concurrent commit).
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

    # Step 3: B wins fully — call transfer_booking(s_b, ...)
    async with session_factory_concurrent() as s_b:
        mock_scheduler_b = _mock_scheduler()
        ref = datetime.now(UTC) - timedelta(days=10)
        result_b = await transfer_booking(
            s_b,
            booking_id=booking_id,
            new_slot_id=new_slot_b_id,
            client_id=client_id,
            scheduler=mock_scheduler_b,
            now_utc=ref,
        )
        assert isinstance(result_b, TransferResult)
        assert result_b.new_slot_id == new_slot_b_id

    # DB now has: booking.status='transferred', start_at=new_B, slot_id=new_slot_b

    # Step 4: A's transfer_booking — patch s_a's SELECT to return stale_booking
    # (simulates A captured snapshot before B's commit, then resumes)
    async with session_factory_concurrent() as s_a:
        original_execute = s_a.execute
        patch_active = True

        async def patched_execute(statement, *args, **kwargs):
            nonlocal patch_active
            if patch_active and isinstance(statement, Select):
                # Detect SELECT FROM Booking — first booking lookup in transfer_booking.
                # If entity detection fails (e.g., SQLAlchemy API change), raise
                # explicitly — silent bypass would let A see B's committed state,
                # re-transfer successfully, and test would fail with confusing
                # "DID NOT RAISE" instead of clear "patched_execute entity
                # detection failed: ...". (Code-review W2.)
                try:
                    entities = [d.get("entity") for d in statement.column_descriptions]
                    if any(isinstance(e, type) and issubclass(e, Booking) for e in entities):
                        patch_active = False  # only first SELECT (booking lookup)

                        # _FakeResult implements only scalar_one_or_none — the only
                        # method transfer_booking:520 calls on the first SELECT result.
                        # If service refactors to .one()/.first()/.scalars().first(),
                        # this would raise AttributeError — add the method here. (W1.)
                        class _FakeResult:  # type: ignore[no-redef]  # type: ignore[no-redef]
                            def scalar_one_or_none(self):
                                return stale_booking

                        return _FakeResult()
                except (KeyError, AttributeError, TypeError) as e:
                    raise AssertionError(
                        f"patched_execute entity detection failed: {e!r} — "
                        f"SQLAlchemy column_descriptions API may have changed"
                    ) from e
            return await original_execute(statement, *args, **kwargs)

        s_a.execute = patched_execute  # type: ignore[method-assign]  # test patch
        try:
            mock_scheduler_a = _mock_scheduler()
            with pytest.raises(BookingAlreadyTransferredError):
                await transfer_booking(
                    s_a,
                    booking_id=booking_id,
                    new_slot_id=new_slot_a_id,
                    client_id=client_id,
                    scheduler=mock_scheduler_a,
                    now_utc=ref,
                )
        finally:
            s_a.execute = original_execute  # type: ignore[method-assign]

    # Step 5: Verify final DB state — only B's transfer succeeded
    async with session_factory_concurrent() as s_verify:
        stmt_b = select(Booking).where(Booking.id == booking_id)
        booking_after = (await s_verify.execute(stmt_b)).scalar_one()
        assert booking_after.status == "transferred"
        assert booking_after.slot_id == new_slot_b_id  # B's slot, not A's
        assert booking_after.start_at != stale_booking.start_at

        # B's new slot is booked, A's would-be new slot is still open
        stmt_b_slot = select(Slot.status).where(Slot.id == new_slot_b_id)
        assert (await s_verify.execute(stmt_b_slot)).scalar_one() == "booked"

        stmt_a_slot = select(Slot.status).where(Slot.id == new_slot_a_id)
        assert (await s_verify.execute(stmt_a_slot)).scalar_one() == "open"

        # Original slot (booking's source before transfer) released by B's step 9
        original_slot_id = stale_booking.slot_id
        stmt_orig_slot = select(Slot.status).where(Slot.id == original_slot_id)
        assert (await s_verify.execute(stmt_orig_slot)).scalar_one() == "open"

        # NotificationLog: only one master_transfer row (B's); A didn't reach INSERT
        stmt_n = select(NotificationLog).where(
            NotificationLog.booking_id == booking_id,
            NotificationLog.kind == "master_transfer",
        )
        notif_rows = (await s_verify.execute(stmt_n)).scalars().all()
        assert len(notif_rows) == 1


# ============================================================
# T8: booking service error branches (NEXT_COVERAGE_GAPS.md)
# Covers bot/services/booking.py:
#   98-100 — _select_service returns None for unknown service_id
#   111-123 — _select_or_create_client race (IntegrityError → rollback → re-SELECT)
#   180-182 — create_booking IntegrityError on booking flush (UNIQUE slot_id race)
#   195-196 — create_booking UPDATE slot rowcount=0 (slot closed between SELECT/UPDATE)
#   212-214 — create_booking NotificationLog IntegrityError (idempotency)
#   353-354 — cancel_booking UPDATE booking rowcount=0 (race with concurrent cancel/transfer)
#   372-374 — cancel_booking NotificationLog IntegrityError (idempotency)
#   601 — transfer_booking recheck status='cancelled' after UPDATE rowcount=0
#   627-628 — transfer_booking UPDATE new_slot rowcount=0 (slot taken between SELECT/UPDATE)
# ============================================================

import asyncio  # noqa: E402 — local import for T8 section

from sqlalchemy.exc import IntegrityError as SAIntegrityError  # noqa: E402
from sqlalchemy.sql.dml import Update  # noqa: E402

# --- Group A: trivial paths (no race) ---


@pytest.mark.asyncio
async def test_create_booking_service_id_random_not_in_db(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Covers bot/services/booking.py:98-100 — _select_service with random
    service_id (not in DB) → SELECT returns None → _build_end_at uses default
    duration. Booking created with service_id=random (FK not enforced on SQLite
    by default — PRAGMA foreign_keys=OFF).
    """
    slot = seed_data["slot"]
    payload = BookingCreate(
        slot_id=slot.id,
        client_name="Паша",
        service_title="Стрижка",
        service_id=uuid4(),  # random — not in DB
    )
    result = await create_booking(
        session,
        payload,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        telegram_id=seed_data["client_telegram_id"],
    )
    assert isinstance(result, BookingCreatedData)
    # End_at uses default duration (service is None, no Service row found)
    assert (result.end_at - result.start_at).total_seconds() == 3600  # 60 min default


@pytest.mark.asyncio
async def test_create_booking_notification_log_idempotency_integrity_error(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Covers bot/services/booking.py:212-214 — IntegrityError on
    NotificationLog(master_new) INSERT inside SAVEPOINT (idempotent retry).

    Approach: monkey-patch session.flush to raise IntegrityError on 2nd call
    (1st = booking INSERT, 2nd = log INSERT inside begin_nested). Service
    catches, savepoint rolls back, main transaction commits.
    """
    slot = seed_data["slot"]
    payload = await _make_payload(slot.id)

    original_flush = session.flush
    flush_count = 0

    async def patched_flush(*args: Any, **kwargs: Any) -> Any:
        nonlocal flush_count
        flush_count += 1
        if flush_count == 2:
            # 2nd flush = log_entry INSERT inside begin_nested → simulate UNIQUE violation
            raise SAIntegrityError(
                "simulated duplicate log INSERT",
                {},
                orig=Exception("UNIQUE constraint failed: notifications_log.booking_id,kind"),
            )
        return await original_flush(*args, **kwargs)

    session.flush = patched_flush  # type: ignore[method-assign]
    try:
        result = await create_booking(
            session,
            payload,
            business_id=seed_data["business_id"],
            master_id=seed_data["master_id"],
            telegram_id=seed_data["client_telegram_id"],
        )
        assert isinstance(result, BookingCreatedData)
    finally:
        session.flush = original_flush  # type: ignore[method-assign]

    # Booking committed (status='confirmed', slot='booked')
    await session.rollback()
    stmt_b = select(Booking).where(Booking.id == result.booking_id)
    booking = (await session.execute(stmt_b)).scalar_one()
    assert booking.status == "confirmed"
    stmt_s = select(Slot.status).where(Slot.id == slot.id)
    assert (await session.execute(stmt_s)).scalar_one() == "booked"
    # NO NotificationLog master_new row (savepoint rolled back)
    stmt_n = select(NotificationLog).where(
        NotificationLog.booking_id == result.booking_id,
        NotificationLog.kind == "master_new",
    )
    notif_rows = (await session.execute(stmt_n)).scalars().all()
    assert len(notif_rows) == 0


@pytest.mark.asyncio
async def test_cancel_booking_notification_log_idempotency_integrity_error(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Covers bot/services/booking.py:372-374 — IntegrityError on
    NotificationLog(master_cancel) INSERT inside SAVEPOINT.

    Approach: monkey-patch session.flush to raise IntegrityError on first call
    (the only flush in cancel_booking post-seeding is the log_entry INSERT
    inside begin_nested — booking/slot UPDATEs use session.execute, not flush).
    """
    booking = await _seed_confirmed_booking(session, seed_data)
    booking_id = booking.id
    slot_id = booking.slot_id
    ref = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    original_flush = session.flush

    async def patched_flush(*args: Any, **kwargs: Any) -> Any:
        # Only flush in cancel_booking after seeding is the log_entry INSERT
        raise SAIntegrityError(
            "simulated duplicate log INSERT",
            {},
            orig=Exception("UNIQUE constraint failed: notifications_log.booking_id,kind"),
        )

    session.flush = patched_flush  # type: ignore[method-assign]
    try:
        result = await cancel_booking(
            session,
            booking_id=booking_id,
            client_id=seed_data["client"].id,
            scheduler=_mock_scheduler(),
            now_utc=ref,
        )
        assert isinstance(result, CancelResult)
        assert result.booking_id == booking_id
    finally:
        session.flush = original_flush  # type: ignore[method-assign]

    # Booking committed (status='cancelled', slot='open')
    await session.rollback()
    stmt_b = select(Booking.status).where(Booking.id == booking_id)
    assert (await session.execute(stmt_b)).scalar_one() == "cancelled"
    stmt_s = select(Slot.status).where(Slot.id == slot_id)
    assert (await session.execute(stmt_s)).scalar_one() == "open"
    # NO NotificationLog master_cancel row (savepoint rolled back)
    stmt_n = select(NotificationLog).where(
        NotificationLog.booking_id == booking_id,
        NotificationLog.kind == "master_cancel",
    )
    notif_rows = (await session.execute(stmt_n)).scalars().all()
    assert len(notif_rows) == 0


# --- Group B: race via asyncio.gather ---


@pytest.mark.asyncio
async def test_create_booking_client_race_integrity_error(
    session_factory_concurrent: async_sessionmaker[AsyncSession],
) -> None:
    """Covers bot/services/booking.py:111-123 — _select_or_create_client race:
    concurrent INSERT of same telegram_id → IntegrityError on flush → rollback
    → re-SELECT finds winner's client.

    Two sessions concurrently call create_booking with same NEW telegram_id
    (no client exists yet) on different slots (no booking race). Loser's INSERT
    client catches IntegrityError (UNIQUE telegram_id), rolls back, re-SELECTs,
    finds winner's committed client, proceeds to booking INSERT. Both bookings
    succeed and share the same client_id.

    Uses asyncio.gather — flakiness risk LOW: even if one coroutine completes
    fully before the other starts, the late-starter's SELECT sees the committed
    client and takes the early-return path (line 109-110), not the race path
    (113-122). To force the race path, both SELECTs must happen before either
    INSERT commits. In practice this works (aiosqlite schedules both SELECTs
    before either flush), but a heavily loaded CI could miss the race.
    """
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
        await s_seed.flush()
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        slot_a = Slot(master_id=master.id, slot_date=tomorrow, slot_hour=10, status="open")
        slot_b = Slot(master_id=master.id, slot_date=tomorrow, slot_hour=11, status="open")
        s_seed.add_all([slot_a, slot_b])
        await s_seed.commit()
        biz_id = biz.id
        master_id = master.id
        slot_a_id = slot_a.id
        slot_b_id = slot_b.id

    # Use a telegram_id that doesn't exist yet (forces client INSERT path)
    new_telegram_id = 999999999

    async with (
        session_factory_concurrent() as s_a,
        session_factory_concurrent() as s_b,
    ):
        results = await asyncio.gather(
            create_booking(
                s_a,
                BookingCreate(
                    slot_id=slot_a_id,
                    client_name="A",
                    service_title="X",
                    service_id=None,
                ),
                business_id=biz_id,
                master_id=master_id,
                telegram_id=new_telegram_id,
            ),
            create_booking(
                s_b,
                BookingCreate(
                    slot_id=slot_b_id,
                    client_name="B",
                    service_title="Y",
                    service_id=None,
                ),
                business_id=biz_id,
                master_id=master_id,
                telegram_id=new_telegram_id,
            ),
            return_exceptions=True,
        )

    errors = [r for r in results if isinstance(r, Exception)]
    success = [r for r in results if not isinstance(r, Exception)]
    assert len(success) == 2, f"both should succeed (race recoverable), got: {results!r}"
    assert len(errors) == 0

    # Both bookings share the same client_id (winner of client race created it)
    async with session_factory_concurrent() as s_verify:
        clients = (
            (await s_verify.execute(select(Client).where(Client.telegram_id == new_telegram_id)))
            .scalars()
            .all()
        )
        assert len(clients) == 1, f"only one client should exist, got {len(clients)}"
        winner_client_id = clients[0].id

        bookings = (
            (await s_verify.execute(select(Booking).where(Booking.client_id == winner_client_id)))
            .scalars()
            .all()
        )
        assert len(bookings) == 2, f"two bookings expected, got {len(bookings)}"


# --- Group C: race via manual orchestration (patched_execute) ---


@pytest.mark.asyncio
async def test_create_booking_concurrent_race_integrity_error(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Covers bot/services/booking.py:180-182 — IntegrityError on booking flush
    (UNIQUE slot_id violated by concurrent insert).

    Approach: pre-create a booking with same slot_id (commits to DB), then
    patch execute so _select_open_slot's SELECT returns a STALE DETACHED slot
    (status='open'). create_booking proceeds to INSERT booking → UNIQUE(slot_id)
    violation at flush → IntegrityError → SlotAlreadyBookedError (line 182).

    Why stale DETACHED slot (not real attached): the service's error message
    at line 183 accesses `slot.id` AFTER `session.rollback()`. On an attached
    instance, rollback expires all attributes → `slot.id` triggers lazy load
    → MissingGreenlet (async SQLAlchemy). A detached instance (created via
    `Slot(id=...)` constructor, never `session.add()`'d) has `id` as a plain
    Python attribute — no lazy load, no MissingGreenlet. This is a production
    latent bug (slot.id after rollback) — recorded in NEXT_COVERAGE_GAPS.md.

    Faithful: tests the actual flush IntegrityError path (line 180-182), NOT
    the _select_open_slot early-exit (line 64) that the existing
    test_create_booking_idempotency_unique_guard covers via sequential double-call
    (where slot.status='booked' is visible on SELECT).
    """
    slot = seed_data["slot"]
    slot_id = slot.id  # capture before any rollback (avoid expired-attr access)
    master_id = seed_data["master_id"]
    slot_date = slot.slot_date
    slot_hour = slot.slot_hour

    # Pre-create a booking with slot.id (commits slot_id to bookings table)
    await _seed_confirmed_booking(session, seed_data)
    # slot.status now 'booked' in DB, booking row exists with slot_id=slot.id

    # Stale DETACHED slot — what _select_open_slot would have seen pre-book
    stale_slot = Slot(
        id=slot_id,
        master_id=master_id,
        slot_date=slot_date,
        slot_hour=slot_hour,
        status="open",  # STALE
    )

    payload = await _make_payload(slot_id)
    original_execute = session.execute
    first_slot_select_done = False

    async def patched_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal first_slot_select_done
        # First SELECT FROM Slot → return stale_slot (status='open')
        if (
            not first_slot_select_done
            and isinstance(statement, Select)
            and any(
                isinstance(e, type) and issubclass(e, Slot)
                for e in [d.get("entity") for d in statement.column_descriptions]
            )
        ):
            first_slot_select_done = True

            class _FakeResult:  # type: ignore[no-redef]
                def scalar_one_or_none(self):
                    return stale_slot

            return _FakeResult()
        return await original_execute(statement, *args, **kwargs)

    session.execute = patched_execute  # type: ignore[method-assign]
    try:
        with pytest.raises(SlotAlreadyBookedError, match="UNIQUE constraint"):
            await create_booking(
                session,
                payload,
                business_id=seed_data["business_id"],
                master_id=master_id,
                telegram_id=seed_data["client_telegram_id"],
            )
    finally:
        session.execute = original_execute  # type: ignore[method-assign]

    # No second booking committed (UNIQUE violation rolled back)
    await session.rollback()
    stmt_b = select(Booking).where(Booking.slot_id == slot_id)
    bookings = (await session.execute(stmt_b)).scalars().all()
    assert len(bookings) == 1, f"only the pre-booked booking, got {len(bookings)}"


@pytest.mark.asyncio
async def test_create_booking_slot_status_changed_between_select_and_update(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Covers bot/services/booking.py:195-196 — UPDATE slot WHERE status='open'
    returns rowcount=0 (slot closed/booked between SELECT and UPDATE).

    Approach: patch execute for TWO interceptions:
    1. First SELECT FROM Slot → return STALE DETACHED slot (status='open').
       Detached (not attached) so service's `slot.id` in error message at
       line 197 doesn't trigger MissingGreenlet after rollback (production
       latent bug — slot.id after rollback, recorded in NEXT_COVERAGE_GAPS.md).
    2. First UPDATE on Slot table → return rowcount=0 (slot status changed
       between SELECT and UPDATE — simulated concurrent close/book).

    Booking INSERT (flush) succeeds with real slot_id, but UPDATE slot fails
    → service rolls back, raises SlotAlreadyBookedError.
    """
    slot = seed_data["slot"]
    slot_id = slot.id  # capture before any patching
    master_id = seed_data["master_id"]
    slot_date = slot.slot_date
    slot_hour = slot.slot_hour

    stale_slot = Slot(
        id=slot_id,
        master_id=master_id,
        slot_date=slot_date,
        slot_hour=slot_hour,
        status="open",  # STALE — what _select_open_slot saw before close
    )

    payload = await _make_payload(slot_id)
    original_execute = session.execute
    patch_state = {"first_slot_select_done": False, "slot_update_done": False}

    async def patched_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        # First SELECT FROM Slot → return stale_slot (detached, status='open')
        if (
            not patch_state["first_slot_select_done"]
            and isinstance(statement, Select)
            and any(
                isinstance(e, type) and issubclass(e, Slot)
                for e in [d.get("entity") for d in statement.column_descriptions]
            )
        ):
            patch_state["first_slot_select_done"] = True

            class _FakeResult:  # type: ignore[no-redef]
                def scalar_one_or_none(self):
                    return stale_slot

            return _FakeResult()

        # First UPDATE on Slot table → rowcount=0 (slot status changed)
        if (
            not patch_state["slot_update_done"]
            and isinstance(statement, Update)
            and statement.table == Slot.__table__
        ):
            patch_state["slot_update_done"] = True

            class _FakeResult:  # type: ignore[no-redef]
                rowcount = 0

            return _FakeResult()

        return await original_execute(statement, *args, **kwargs)

    session.execute = patched_execute  # type: ignore[method-assign]
    try:
        with pytest.raises(
            SlotAlreadyBookedError,
            match="taken/closed between SELECT and UPDATE",
        ):
            await create_booking(
                session,
                payload,
                business_id=seed_data["business_id"],
                master_id=master_id,
                telegram_id=seed_data["client_telegram_id"],
            )
    finally:
        session.execute = original_execute  # type: ignore[method-assign]

    # Service rolled back — booking NOT committed
    await session.rollback()
    stmt_b = select(Booking).where(Booking.slot_id == slot_id)
    bookings = (await session.execute(stmt_b)).scalars().all()
    assert len(bookings) == 0


@pytest.mark.asyncio
async def test_cancel_booking_concurrent_race_booking_already_cancelled(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Covers bot/services/booking.py:353-354 — UPDATE booking SET 'cancelled'
    WHERE status IN ('confirmed','transferred') returns rowcount=0 (concurrent
    cancel/transfer changed status between SELECT and UPDATE).

    Approach: monkey-patch session.execute to intercept UPDATE on Booking table
    and return rowcount=0. Service rolls back, raises BookingAlreadyCancelledError.
    """
    booking = await _seed_confirmed_booking(session, seed_data)
    booking_id = booking.id
    slot_id = booking.slot_id
    ref = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    original_execute = session.execute
    patch_active = True

    async def patched_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal patch_active
        if patch_active and isinstance(statement, Update):
            from bot.models import Booking as BookingModel

            if statement.table == BookingModel.__table__:
                patch_active = False

                class _FakeResult:  # type: ignore[no-redef]  # type: ignore[no-redef]
                    rowcount = 0

                return _FakeResult()
        return await original_execute(statement, *args, **kwargs)

    session.execute = patched_execute  # type: ignore[method-assign]
    try:
        with pytest.raises(
            BookingAlreadyCancelledError,
            match="status changed between SELECT and UPDATE",
        ):
            await cancel_booking(
                session,
                booking_id=booking_id,
                client_id=seed_data["client"].id,
                scheduler=_mock_scheduler(),
                now_utc=ref,
            )
    finally:
        session.execute = original_execute  # type: ignore[method-assign]

    # Booking still 'confirmed' (UPDATE rolled back)
    await session.rollback()
    stmt_b = select(Booking.status).where(Booking.id == booking_id)
    assert (await session.execute(stmt_b)).scalar_one() == "confirmed"
    stmt_s = select(Slot.status).where(Slot.id == slot_id)
    assert (await session.execute(stmt_s)).scalar_one() == "booked"


# --- Group D: transfer_booking race branches ---


@pytest.mark.asyncio
async def test_transfer_booking_concurrent_cancel_race_raises_already_cancelled(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Covers bot/services/booking.py:601 — recheck status='cancelled' branch
    after UPDATE rowcount=0 in transfer_booking.

    Race: A's transfer SELECT sees booking.status='confirmed', B concurrently
    cancels (commits status='cancelled'), A's UPDATE WHERE status IN
    ('confirmed','transferred') AND start_at=old returns rowcount=0, A re-SELECTs
    → 'cancelled' → BookingAlreadyCancelledError (line 601, NOT
    BookingAlreadyTransferredError).

    Approach: pre-cancel booking (commits 'cancelled' to DB), then patch execute:
    1. First SELECT FROM Booking → return stale_booking (status='confirmed')
    2. UPDATE booking → rowcount=0
    3. Re-SELECT status → fall through to real DB (returns 'cancelled')
    """
    booking = await _seed_confirmed_booking(session, seed_data)
    booking_id = booking.id
    new_slot = await _make_open_slot(session, seed_data, days_ahead=5, hour_local=15)
    new_slot_id = new_slot.id

    ref = datetime.now(UTC) - timedelta(days=10)

    # Pre-cancel the booking (commits status='cancelled')
    await cancel_booking(
        session,
        booking_id=booking_id,
        client_id=seed_data["client"].id,
        scheduler=_mock_scheduler(),
        now_utc=ref,
    )

    # Capture fields for stale_booking (start_at/slot_id/end_at don't change on cancel)
    await session.rollback()
    booking_row = (
        await session.execute(select(Booking).where(Booking.id == booking_id))
    ).scalar_one()
    captured_start_at = booking_row.start_at
    captured_end_at = booking_row.end_at
    captured_slot_id = booking_row.slot_id

    stale_booking = Booking(
        id=booking_id,
        client_id=seed_data["client"].id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        slot_id=captured_slot_id,
        start_at=captured_start_at,
        end_at=captured_end_at,
        status="confirmed",  # STALE — what transfer's SELECT would have seen pre-cancel
        service_id=None,
        client_name_snapshot=booking_row.client_name_snapshot,
        service_title_snapshot=booking_row.service_title_snapshot,
    )

    original_execute = session.execute
    patch_state = {"first_select_done": False, "update_done": False}

    async def patched_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        # First SELECT FROM Booking (line 519) → stale_booking (status='confirmed')
        if (
            not patch_state["first_select_done"]
            and isinstance(statement, Select)
            and any(
                isinstance(e, type) and issubclass(e, Booking)
                for e in [d.get("entity") for d in statement.column_descriptions]
            )
        ):
            patch_state["first_select_done"] = True

            class _FakeResult:  # type: ignore[no-redef]
                def scalar_one_or_none(self):
                    return stale_booking

            return _FakeResult()

        # UPDATE booking (line 584) → rowcount=0
        if (
            not patch_state["update_done"]
            and isinstance(statement, Update)
            and statement.table == Booking.__table__
        ):
            patch_state["update_done"] = True

            class _FakeResult:  # type: ignore[no-redef]
                rowcount = 0

            return _FakeResult()

        # Re-SELECT status (line 596-599) → fall through to real DB (returns 'cancelled')
        return await original_execute(statement, *args, **kwargs)

    session.execute = patched_execute  # type: ignore[method-assign]
    try:
        with pytest.raises(
            BookingAlreadyCancelledError,
            match="cancelled by concurrent",
        ):
            await transfer_booking(
                session,
                booking_id=booking_id,
                new_slot_id=new_slot_id,
                client_id=seed_data["client"].id,
                scheduler=_mock_scheduler(),
                now_utc=ref,
            )
    finally:
        session.execute = original_execute  # type: ignore[method-assign]

    # Booking still 'cancelled' (no transfer happened)
    await session.rollback()
    stmt_b = select(Booking.status).where(Booking.id == booking_id)
    assert (await session.execute(stmt_b)).scalar_one() == "cancelled"
    stmt_new = select(Slot.status).where(Slot.id == new_slot_id)
    assert (await session.execute(stmt_new)).scalar_one() == "open"


@pytest.mark.asyncio
async def test_transfer_booking_concurrent_new_slot_taken_raises_slot_already_booked(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Covers bot/services/booking.py:627-628 — UPDATE new_slot WHERE status='open'
    returns rowcount=0 (slot taken between SELECT and UPDATE in transfer_booking).

    Approach: pre-book new_slot (status='booked' in DB), then patch execute:
    1. First SELECT FROM Slot (new_slot lookup, step 5) → return stale_slot (status='open')
    2. UPDATE old slot (step 9, release to 'open') → fall through (real DB)
    3. UPDATE new slot WHERE status='open' (step 10) → rowcount=0
    Service rolls back, raises SlotAlreadyBookedError (line 628).
    """
    booking = await _seed_confirmed_booking(session, seed_data)
    booking_id = booking.id
    new_slot = await _make_open_slot(session, seed_data, days_ahead=5, hour_local=15)
    new_slot_id = new_slot.id

    # Pre-book new_slot (status='booked' in DB — simulates concurrent booking)
    new_slot.status = "booked"
    await session.commit()

    # Re-read fresh state
    await session.rollback()
    new_slot_row = (await session.execute(select(Slot).where(Slot.id == new_slot_id))).scalar_one()
    assert new_slot_row.status == "booked"

    stale_new_slot = Slot(
        id=new_slot_id,
        master_id=new_slot_row.master_id,
        slot_date=new_slot_row.slot_date,
        slot_hour=new_slot_row.slot_hour,
        status="open",  # STALE — what transfer's _select_open_slot saw pre-book
    )

    original_execute = session.execute
    patch_state = {"first_slot_select_done": False, "slot_update_count": 0}

    async def patched_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        # First SELECT FROM Slot (new_slot lookup) → stale_new_slot (status='open')
        if (
            not patch_state["first_slot_select_done"]
            and isinstance(statement, Select)
            and any(
                isinstance(e, type) and issubclass(e, Slot)
                for e in [d.get("entity") for d in statement.column_descriptions]
            )
        ):
            patch_state["first_slot_select_done"] = True

            class _FakeResult:  # type: ignore[no-redef]
                def scalar_one_or_none(self):
                    return stale_new_slot

            return _FakeResult()

        # UPDATE on Slot table: 1st = step 9 (release old), 2nd = step 10 (book new)
        if isinstance(statement, Update) and statement.table == Slot.__table__:
            patch_state["slot_update_count"] += 1
            if patch_state["slot_update_count"] == 2:
                # Step 10: UPDATE new_slot WHERE status='open' → rowcount=0 (taken)

                class _FakeResult:  # type: ignore[no-redef]  # type: ignore[no-redef]
                    rowcount = 0

                return _FakeResult()

        return await original_execute(statement, *args, **kwargs)

    session.execute = patched_execute  # type: ignore[method-assign]
    try:
        with pytest.raises(
            SlotAlreadyBookedError,
            match="taken/closed between SELECT and UPDATE",
        ):
            await transfer_booking(
                session,
                booking_id=booking_id,
                new_slot_id=new_slot_id,
                client_id=seed_data["client"].id,
                scheduler=_mock_scheduler(),
                now_utc=datetime.now(UTC) - timedelta(days=10),
            )
    finally:
        session.execute = original_execute  # type: ignore[method-assign]

    # Booking unchanged (status='confirmed' — transfer rolled back)
    await session.rollback()
    stmt_b = select(Booking.status).where(Booking.id == booking_id)
    assert (await session.execute(stmt_b)).scalar_one() == "confirmed"
