"""Tests for bot.services.notifications — UNIQUE(booking_id, kind) idempotency guard."""

from datetime import UTC, datetime, timedelta
from itertools import count

import pytest
from bot.models import Booking, Business, Client, Master, Slot
from bot.services.notifications import (
    get_overdue_bookings_without_remind_24h,
    get_upcoming_bookings_for_reschedule,
    log_notification,
)
from sqlalchemy.ext.asyncio import AsyncSession

# Counter for unique telegram_id across multiple _seed_booking calls (UNIQUE constraint)
_telegram_counter = count(start=100000000, step=1)


async def _seed_booking(session: AsyncSession, start_at: datetime) -> Booking:
    """Seed a confirmed booking with given start_at (UTC)."""
    biz = Business(name="Test", telegram_owner_id=461355056, timezone="Europe/Moscow")
    session.add(biz)
    await session.flush()

    master = Master(business_id=biz.id, name="Екатерина", telegram_id=461355056)
    # Unique telegram_id per call (UNIQUE constraint on clients.telegram_id)
    client = Client(telegram_id=next(_telegram_counter))
    session.add_all([master, client])
    await session.flush()

    slot = Slot(master_id=master.id, slot_date=start_at.date(), slot_hour=14, status="booked")
    session.add(slot)
    await session.flush()

    booking = Booking(
        slot_id=slot.id,
        business_id=biz.id,
        master_id=master.id,
        client_id=client.id,
        service_id=None,
        service_title_snapshot="Test",
        client_name_snapshot="Test",
        start_at=start_at,
        end_at=start_at + timedelta(hours=1),
        status="confirmed",
    )
    session.add(booking)
    await session.commit()
    return booking


@pytest.mark.asyncio
async def test_log_notification_first_insert_succeeds(session: AsyncSession) -> None:
    """First INSERT to notifications_log succeeds, returns True."""
    booking = await _seed_booking(session, datetime.now(UTC) + timedelta(days=2))
    result = await log_notification(session, booking.id, "remind_24h")
    assert result is True


@pytest.mark.asyncio
async def test_log_notification_duplicate_returns_false(session: AsyncSession) -> None:
    """UNIQUE(booking_id, kind) — second INSERT for same (booking_id, kind) returns False."""
    booking = await _seed_booking(session, datetime.now(UTC) + timedelta(days=2))

    first = await log_notification(session, booking.id, "remind_24h")
    assert first is True

    second = await log_notification(session, booking.id, "remind_24h")
    assert second is False  # UNIQUE guard caught — idempotent return


@pytest.mark.asyncio
async def test_log_notification_different_kinds_succeed(session: AsyncSession) -> None:
    """Different kinds for same booking_id are allowed (master_new + remind_24h)."""
    booking = await _seed_booking(session, datetime.now(UTC) + timedelta(days=2))

    r1 = await log_notification(session, booking.id, "master_new")
    r2 = await log_notification(session, booking.id, "remind_24h")

    assert r1 is True
    assert r2 is True


@pytest.mark.asyncio
async def test_get_overdue_filters_by_window_and_log(session: AsyncSession) -> None:
    """Phase 1 filter: bookings in (now-24h, now) without remind_24h log."""
    # Booking 12h ago — should be returned (in window, no log)
    overdue = await _seed_booking(session, datetime.now(UTC) - timedelta(hours=12))

    # Booking 36h ago — should NOT be returned (outside window)
    too_old = await _seed_booking(session, datetime.now(UTC) - timedelta(hours=36))

    # Booking 12h ago WITH remind_24h log — should NOT be returned (already has log)
    with_log = await _seed_booking(session, datetime.now(UTC) - timedelta(hours=12))
    await log_notification(session, with_log.id, "remind_24h")

    # Future booking — should NOT be returned (start_at > now)
    future = await _seed_booking(session, datetime.now(UTC) + timedelta(days=2))

    # W1 fix (code-review): booking 12h ago with NON-remind_24h log (master_new)
    # → SHOULD be returned. Distinguishes "filter by remind_24h" from "filter by
    # any kind" — guards against future regression that inverts kind logic.
    with_other_log = await _seed_booking(session, datetime.now(UTC) - timedelta(hours=12))
    await log_notification(session, with_other_log.id, "master_new")

    # W2 fix (code-review): booking 12h ago with status='cancelled'
    # → should NOT be returned (only 'confirmed' bookings need overdue reminders).
    cancelled_in_window = await _seed_booking(session, datetime.now(UTC) - timedelta(hours=12))
    cancelled_in_window.status = "cancelled"
    await session.commit()

    overdue_list = await get_overdue_bookings_without_remind_24h(session, datetime.now(UTC))

    overdue_ids = {b.id for b in overdue_list}
    assert overdue.id in overdue_ids
    assert too_old.id not in overdue_ids
    assert with_log.id not in overdue_ids
    assert future.id not in overdue_ids
    # W1: booking with master_new log (no remind_24h) should be returned
    assert with_other_log.id in overdue_ids
    # W2: cancelled booking in window should NOT be returned
    assert cancelled_in_window.id not in overdue_ids


@pytest.mark.asyncio
async def test_get_upcoming_filters_by_window(session: AsyncSession) -> None:
    """Phase 2 filter: bookings in (now, now+25h) with status='confirmed'."""
    # Booking 12h in future — should be returned
    upcoming = await _seed_booking(session, datetime.now(UTC) + timedelta(hours=12))

    # Booking 36h in future — should NOT be returned (outside 25h window)
    too_far = await _seed_booking(session, datetime.now(UTC) + timedelta(hours=36))

    # Booking 12h ago — should NOT be returned (in past)
    past = await _seed_booking(session, datetime.now(UTC) - timedelta(hours=12))

    # Cancelled future booking — should NOT be returned (status != confirmed)
    cancelled = await _seed_booking(session, datetime.now(UTC) + timedelta(hours=12))
    cancelled.status = "cancelled"
    await session.commit()

    upcoming_list = await get_upcoming_bookings_for_reschedule(
        session, datetime.now(UTC), look_ahead_hours=25
    )

    upcoming_ids = {b.id for b in upcoming_list}
    assert upcoming.id in upcoming_ids
    assert too_far.id not in upcoming_ids
    assert past.id not in upcoming_ids
    assert cancelled.id not in upcoming_ids


@pytest.mark.asyncio
async def test_get_upcoming_default_look_ahead_25h(session: AsyncSession) -> None:
    """Default look_ahead is 25h (per spec.md 374-380)."""
    just_in_window = await _seed_booking(session, datetime.now(UTC) + timedelta(hours=24))
    out_of_window = await _seed_booking(session, datetime.now(UTC) + timedelta(hours=26))

    upcoming = await get_upcoming_bookings_for_reschedule(session, datetime.now(UTC))

    ids = {b.id for b in upcoming}
    assert just_in_window.id in ids
    assert out_of_window.id not in ids
