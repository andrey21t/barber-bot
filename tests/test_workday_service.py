"""Tests for WorkDay service — open/update/close (Этап 5.1, PLANS.md Gap 6).

Coverage (13 tests):
- open_workday: new (INSERT), idempotent (UPDATE not INSERT), reopen closed,
  end<=start raises ValueError
- update_workday: shrink end_time with active bookings → WorkDayShrinkError
  (Gap 6), shrink start_time with active booking before new_start → refuse
  (OR-clause coverage), expand ok (no conflict), shrink no bookings ok,
  cancelled booking excluded (Gap 8 parens defence-in-depth — actual false-
  positive path: cancelled booking with start_at < new_start)
- close_workday: with active bookings refuse, no bookings ok, idempotent
  (already-closed returns True), not found returns False

Service layer is config-free (business_tz passed from caller). Tests pass
"Europe/Moscow" directly — same as seed_data.business.timezone.

Baseline: 296 tests after 5.5 → +13 in 5.1 = 309 expected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Any
from uuid import uuid4

import pytest
from bot.models import Booking, NotificationLog, WorkDay
from bot.services.workday import (
    WorkDayShrinkError,
    close_workday,
    close_workday_with_cancellations,
    open_workday,
    update_workday,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

BUSINESS_TZ = "Europe/Moscow"


async def _direct_insert_booking(
    session: AsyncSession,
    seed_data: dict[str, Any],
    *,
    start_at: datetime,
    end_at: datetime,
    status: str = "confirmed",
) -> Booking:
    """Direct INSERT a Booking bypassing create_booking — for test setup only.

    Mirror of test_multi_client._direct_insert_booking but without slot_id
    (WorkDay-based tests don't need Slot — the service under test queries by
    master_id, not slot_id). Slot.slot_id is nullable=False on Booking model,
    so we still need a slot — but use seed_data.slot for convenience.
    """
    booking = Booking(
        slot_id=seed_data["slot"].id,
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

    Mirror of multi_client._local_to_utc — used by _direct_insert_booking to
    set start_at/end_at for seed bookings.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(BUSINESS_TZ)
    return datetime.combine(work_date, dt_time(hour, minute), tzinfo=tz).astimezone(UTC)


# ---------------------------------------------------------------------------
# open_workday
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_workday_new(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Open a workday on a new date → INSERT new WorkDay with defaults.

    Defaults: max_concurrent_clients=1, is_active=True (matching model defaults).
    Verifies the INSERT path (no existing WorkDay for this date).
    """
    new_date = (datetime.now(UTC) + timedelta(days=7)).date()
    workday = await open_workday(
        session,
        seed_data["master_id"],
        new_date,
        dt_time(11, 0),
        dt_time(18, 0),
        business_tz=BUSINESS_TZ,
    )

    assert workday.id is not None
    assert workday.master_id == seed_data["master_id"]
    assert workday.work_date == new_date
    assert workday.start_time == dt_time(11, 0)
    assert workday.end_time == dt_time(18, 0)
    assert workday.max_concurrent_clients == 1
    assert workday.is_active is True


@pytest.mark.asyncio
async def test_open_workday_idempotent_update(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Repeated open_workday on the same date → UPDATE start/end (NOT INSERT).

    Idempotency via UNIQUE INDEX ux_work_days_master_date. Second call widens
    the window [11:00, 18:00] → [10:00, 20:00]; only ONE WorkDay row exists.
    """
    new_date = (datetime.now(UTC) + timedelta(days=8)).date()
    await open_workday(
        session,
        seed_data["master_id"],
        new_date,
        dt_time(11, 0),
        dt_time(18, 0),
        business_tz=BUSINESS_TZ,
    )

    # Re-open with a wider window — should UPDATE, not INSERT.
    updated = await open_workday(
        session,
        seed_data["master_id"],
        new_date,
        dt_time(10, 0),
        dt_time(20, 0),
        business_tz=BUSINESS_TZ,
    )

    assert updated.start_time == dt_time(10, 0)
    assert updated.end_time == dt_time(20, 0)

    # Verify only ONE WorkDay row exists for this (master_id, work_date).
    rows = (
        (
            await session.execute(
                select(WorkDay).where(
                    WorkDay.master_id == seed_data["master_id"],
                    WorkDay.work_date == new_date,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_open_workday_reopens_closed(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Open_workday on a previously-closed WorkDay → is_active restored to True.

    Edge: closing a day (close_workday) sets is_active=False; re-opening via
    /openday should flip it back to True so /slots (5.6) shows it again.
    max_concurrent_clients preserved (not reset to default).
    """
    new_date = (datetime.now(UTC) + timedelta(days=9)).date()
    workday = await open_workday(
        session,
        seed_data["master_id"],
        new_date,
        dt_time(11, 0),
        dt_time(18, 0),
        business_tz=BUSINESS_TZ,
    )
    # Bump cap to 2 (как Екатерина в prod после deploy) — preserved on reopen.
    workday.max_concurrent_clients = 2
    await session.commit()

    closed = await close_workday(session, workday.id, business_tz=BUSINESS_TZ)
    assert closed is True

    # Re-open via open_workday — should restore is_active=True, preserve cap=2.
    reopened = await open_workday(
        session,
        seed_data["master_id"],
        new_date,
        dt_time(11, 0),
        dt_time(18, 0),
        business_tz=BUSINESS_TZ,
    )
    assert reopened.is_active is True
    assert reopened.max_concurrent_clients == 2


@pytest.mark.asyncio
async def test_open_workday_end_before_start_raises(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """end_time <= start_time → ValueError (UX-friendly, before advisory lock).

    CheckConstraint ck_workday_window_positive also blocks at flush, but
    open_workday raises early for a clearer message (before the lock acquire).
    """
    new_date = (datetime.now(UTC) + timedelta(days=10)).date()
    with pytest.raises(ValueError, match="end_time .* must be > start_time"):
        await open_workday(
            session,
            seed_data["master_id"],
            new_date,
            dt_time(18, 0),  # start
            dt_time(11, 0),  # end < start
            business_tz=BUSINESS_TZ,
        )


# ---------------------------------------------------------------------------
# update_workday — Gap 6 shrink checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_workday_shrink_with_active_bookings_refuse(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Shrink end_time earlier than active Booking.end_at → WorkDayShrinkError (Gap 6).

    Setup: WorkDay [10:00, 20:00] LOCAL with a booking [19:00, 20:00] LOCAL.
    Shrink end_time to 18:00 → booking.end_at (20:00 LOCAL = 17:00 UTC) >
    new_end_utc (18:00 LOCAL = 15:00 UTC) → conflict → raise.

    Verifies the conflict list is included in the error message (admin needs
    it to cancel the right booking).
    """
    new_date = (datetime.now(UTC) + timedelta(days=11)).date()
    workday = await open_workday(
        session,
        seed_data["master_id"],
        new_date,
        dt_time(10, 0),
        dt_time(20, 0),
        business_tz=BUSINESS_TZ,
    )
    # Booking [19:00, 20:00] LOCAL — inside the original window.
    await _direct_insert_booking(
        session,
        seed_data,
        start_at=_local_to_utc(new_date, 19, 0),
        end_at=_local_to_utc(new_date, 20, 0),
    )

    with pytest.raises(WorkDayShrinkError) as exc_info:
        await update_workday(
            session,
            workday.id,
            dt_time(10, 0),
            dt_time(18, 0),  # shrink end 20:00 → 18:00
            business_tz=BUSINESS_TZ,
        )

    msg = str(exc_info.value)
    assert "1 active booking" in msg or "1 booking" in msg, f"Expected count in: {msg}"


@pytest.mark.asyncio
async def test_update_workday_expand_ok(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Expand window (start earlier / end later) → ok, no conflict (Gap 6 wide).

    Expansion cannot exclude any existing booking — every booking inside the
    old window is also inside the new (wider) window. Shrink check skips.
    """
    new_date = (datetime.now(UTC) + timedelta(days=12)).date()
    workday = await open_workday(
        session,
        seed_data["master_id"],
        new_date,
        dt_time(11, 0),
        dt_time(18, 0),
        business_tz=BUSINESS_TZ,
    )
    # Booking [14:00, 15:00] LOCAL — inside the original window.
    await _direct_insert_booking(
        session,
        seed_data,
        start_at=_local_to_utc(new_date, 14, 0),
        end_at=_local_to_utc(new_date, 15, 0),
    )

    # Expand [11:00, 18:00] → [10:00, 20:00] — booking still inside.
    updated = await update_workday(
        session,
        workday.id,
        dt_time(10, 0),
        dt_time(20, 0),
        business_tz=BUSINESS_TZ,
    )
    assert updated.start_time == dt_time(10, 0)
    assert updated.end_time == dt_time(20, 0)


@pytest.mark.asyncio
async def test_update_workday_shrink_no_bookings_ok(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Shrink window with NO active bookings → ok (no conflict).

    Even though the window narrows, there are no bookings to exclude — the
    shrink check passes (count == 0). The window is updated.
    """
    new_date = (datetime.now(UTC) + timedelta(days=13)).date()
    workday = await open_workday(
        session,
        seed_data["master_id"],
        new_date,
        dt_time(10, 0),
        dt_time(20, 0),
        business_tz=BUSINESS_TZ,
    )

    # No bookings — shrink end_time 20:00 → 16:00 is safe.
    updated = await update_workday(
        session,
        workday.id,
        dt_time(10, 0),
        dt_time(16, 0),
        business_tz=BUSINESS_TZ,
    )
    assert updated.end_time == dt_time(16, 0)


@pytest.mark.asyncio
async def test_update_workday_shrink_cancelled_booking_excluded(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Shrink start_time later with a CANCELLED booking before new_start → ok (Gap 8 parens).

    Defence-in-depth for Gap 8 SQL precedence fix. The bug: WITHOUT the mandatory
    parens `(start_at < new_start OR end_at > new_end) AND status IN (...)`, the
    AND clause binds tighter than OR → `start_at < new_start OR (end_at > new_end
    AND status IN (...))` → cancelled booking with `start_at < new_start`
    TRIGGERS the OR branch alone (no status check) → false-positive
    WorkDayShrinkError. WITH parens, the status IN (...) check applies to the
    whole OR clause → cancelled.status fails the IN check → excluded.

    Setup: WorkDay [10:00, 20:00] LOCAL, cancelled booking [09:00, 10:00] LOCAL
    (start_at=09:00 < new_start=11:00 — triggers the OR branch), shrink start to
    11:00 — cancelled booking should be excluded by the parens-protected
    status IN ('confirmed','transferred') check.

    Truth table (workday.py:152):
        WITH parens:    (start_at < new_start OR end_at > new_end) AND status IN (...)
                        (True OR ?) AND False (cancelled.status not in IN set)
                        = False → ok ✓
        WITHOUT parens: start_at < new_start OR (end_at > new_end AND status IN (...))
                        True OR (? AND False) = True → WorkDayShrinkError ✗ (bug)

    The booking [09:00, 10:00] is OUTSIDE the WorkDay window — _direct_insert_booking
    bypasses create_booking's _validate_booking_within_workday invariant, so the
    setup itself doesn't raise (we only need the booking row for the SELECT in
    update_workday to find it).
    """
    new_date = (datetime.now(UTC) + timedelta(days=14)).date()
    workday = await open_workday(
        session,
        seed_data["master_id"],
        new_date,
        dt_time(10, 0),
        dt_time(20, 0),
        business_tz=BUSINESS_TZ,
    )
    # Cancelled booking BEFORE the original window start (09:00 < 10:00).
    # _direct_insert_booking bypasses _validate_booking_within_workday.
    await _direct_insert_booking(
        session,
        seed_data,
        start_at=_local_to_utc(new_date, 9, 0),
        end_at=_local_to_utc(new_date, 10, 0),
        status="cancelled",
    )

    # Shrink start forward: 10:00 → 11:00. Cancelled booking [09:00, 10:00]
    # has start_at=09:00 < new_start=11:00 — would trigger false-positive
    # without the Gap 8 parens (cancelled.status fails the IN check).
    updated = await update_workday(
        session,
        workday.id,
        dt_time(11, 0),  # new_start pushed later
        dt_time(20, 0),
        business_tz=BUSINESS_TZ,
    )
    assert updated.start_time == dt_time(11, 0)


@pytest.mark.asyncio
async def test_update_workday_shrink_start_time_with_active_bookings_refuse(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Shrink start_time later (push forward) with active booking before new_start → refuse.

    Mirror of test_update_workday_shrink_with_active_bookings_refuse but for the
    start_time bound: a confirmed booking [10:00, 11:00] LOCAL is inside the
    original window [10:00, 20:00] but outside the shrunken window [11:00, 20:00]
    (booking.start_at=10:00 < new_start=11:00) → must refuse.

    Covers the OR-clause `start_at < new_start_utc` branch (workday.py:152).
    Without this test, a future refactor that drops the start_at check (keeping
    only end_at check) would still pass test_update_workday_shrink_with_active_
    bookings_refuse (which only shrinks end_time) — start_time shrink semantics
    would silently regress.
    """
    new_date = (datetime.now(UTC) + timedelta(days=18)).date()
    workday = await open_workday(
        session,
        seed_data["master_id"],
        new_date,
        dt_time(10, 0),
        dt_time(20, 0),
        business_tz=BUSINESS_TZ,
    )
    # Active booking [10:00, 11:00] LOCAL — inside original, outside new window.
    await _direct_insert_booking(
        session,
        seed_data,
        start_at=_local_to_utc(new_date, 10, 0),
        end_at=_local_to_utc(new_date, 11, 0),
        status="confirmed",
    )

    with pytest.raises(WorkDayShrinkError) as exc_info:
        await update_workday(
            session,
            workday.id,
            dt_time(11, 0),  # new_start pushed later — booking.start_at < new_start
            dt_time(20, 0),
            business_tz=BUSINESS_TZ,
        )

    msg = str(exc_info.value)
    assert "1 active booking" in msg or "1 booking" in msg, f"Expected count in: {msg}"


# ---------------------------------------------------------------------------
# close_workday
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_workday_with_active_bookings_refuse(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Close a workday with active bookings → WorkDayShrinkError.

    Closing hides the day from /slots (5.6 is_active filter), but bookings
    under a closed day still bind create_booking (test_create_booking_workday_
    inactive). Admin must cancel bookings first OR shrink end_time before
    the earliest booking via update_workday.
    """
    new_date = (datetime.now(UTC) + timedelta(days=15)).date()
    workday = await open_workday(
        session,
        seed_data["master_id"],
        new_date,
        dt_time(10, 0),
        dt_time(20, 0),
        business_tz=BUSINESS_TZ,
    )
    await _direct_insert_booking(
        session,
        seed_data,
        start_at=_local_to_utc(new_date, 14, 0),
        end_at=_local_to_utc(new_date, 15, 0),
    )

    with pytest.raises(WorkDayShrinkError, match="active"):
        await close_workday(session, workday.id, business_tz=BUSINESS_TZ)

    # Verify the day is NOT closed (state unchanged).
    await session.refresh(workday)
    assert workday.is_active is True


@pytest.mark.asyncio
async def test_close_workday_no_bookings_ok(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Close a workday with NO active bookings → is_active=False (success).

    Returns True (updated). Idempotent: a second close returns True without
    re-writing (already-closed short-circuit).
    """
    new_date = (datetime.now(UTC) + timedelta(days=16)).date()
    workday = await open_workday(
        session,
        seed_data["master_id"],
        new_date,
        dt_time(11, 0),
        dt_time(18, 0),
        business_tz=BUSINESS_TZ,
    )

    closed = await close_workday(session, workday.id, business_tz=BUSINESS_TZ)
    assert closed is True

    await session.refresh(workday)
    assert workday.is_active is False


@pytest.mark.asyncio
async def test_close_workday_idempotent_already_closed(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Close an already-closed workday → returns True (idempotent, no re-write).

    Second close short-circuits on `is_active is False` before the bookings
    check — even if bookings were added after closing (edge case), close is
    idempotent and does not raise. Bookings check applies only when transitioning
    active→inactive.
    """
    new_date = (datetime.now(UTC) + timedelta(days=17)).date()
    workday = await open_workday(
        session,
        seed_data["master_id"],
        new_date,
        dt_time(11, 0),
        dt_time(18, 0),
        business_tz=BUSINESS_TZ,
    )
    await close_workday(session, workday.id, business_tz=BUSINESS_TZ)

    # Second close — idempotent, returns True without raising even though no
    # state change is performed.
    closed_again = await close_workday(session, workday.id, business_tz=BUSINESS_TZ)
    assert closed_again is True


@pytest.mark.asyncio
async def test_close_workday_not_found_returns_false(
    session: AsyncSession,
    seed_data: dict[str, Any],  # noqa: ARG001 — fixture seeds DB schema
) -> None:
    """Close a non-existent WorkDay → returns False (not found, not an error).

    Defensive: handler may call close on a stale workday_id (e.g. admin
    cancelled via direct DB access). Returning False lets the handler render
    'день не найден' instead of crashing.
    """
    fake_id = uuid4()
    closed = await close_workday(session, fake_id, business_tz=BUSINESS_TZ)
    assert closed is False


# ---------------------------------------------------------------------------
# close_workday_with_cancellations (Session 5.26)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_with_cancellations_no_workday_returns_none(
    session: AsyncSession,
    seed_data: dict[str, Any],  # noqa: ARG001 — fixture seeds DB schema
) -> None:
    """close_workday_with_cancellations on a non-existent workday_id → None.

    Handler branch: 'день не открыт' message (admin tried to close a day
    that was never opened via /openweek or /openday).
    """
    fake_id = uuid4()
    result = await close_workday_with_cancellations(
        session, fake_id, business_tz=BUSINESS_TZ
    )
    assert result is None


@pytest.mark.asyncio
async def test_close_with_cancellations_already_closed_idempotent(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Idempotent: WorkDay already is_active=False → return ClosedDayResult
    with was_already_closed=True and empty cancelled_bookings (no second
    cancellation pass — admin can re-run /closeday safely).
    """
    new_date = (datetime.now(UTC) + timedelta(days=18)).date()
    workday = await open_workday(
        session,
        seed_data["master_id"],
        new_date,
        dt_time(10, 0),
        dt_time(20, 0),
        business_tz=BUSINESS_TZ,
    )
    # First close: no bookings → is_active=False
    await close_workday(session, workday.id, business_tz=BUSINESS_TZ)

    # Now add an active booking UNDER the closed day (edge case: admin opened
    # a booking via direct API after closing). Second close must NOT cancel it
    # — was_already_closed short-circuits.
    booking = await _direct_insert_booking(
        session,
        seed_data,
        start_at=_local_to_utc(new_date, 14, 0),
        end_at=_local_to_utc(new_date, 15, 0),
    )

    result = await close_workday_with_cancellations(
        session, workday.id, business_tz=BUSINESS_TZ
    )
    assert result is not None
    assert result.was_already_closed is True
    assert result.cancelled_bookings == []
    assert result.workday_id == workday.id
    assert result.work_date == new_date

    # The post-close booking is NOT cancelled.
    await session.refresh(booking)
    assert booking.status == "confirmed"


@pytest.mark.asyncio
async def test_close_with_cancellations_no_active_bookings(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Active workday, NO active bookings → is_active=False, empty
    cancelled_bookings, was_already_closed=False. Mirrors /closeday happy
    path when master closes an empty day (no client notifications sent).
    """
    new_date = (datetime.now(UTC) + timedelta(days=19)).date()
    workday = await open_workday(
        session,
        seed_data["master_id"],
        new_date,
        dt_time(10, 0),
        dt_time(20, 0),
        business_tz=BUSINESS_TZ,
    )

    result = await close_workday_with_cancellations(
        session, workday.id, business_tz=BUSINESS_TZ
    )
    assert result is not None
    assert result.was_already_closed is False
    assert result.cancelled_bookings == []
    assert result.work_date == new_date

    await session.refresh(workday)
    assert workday.is_active is False


@pytest.mark.asyncio
async def test_close_with_cancellations_cancels_active_bookings(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Active workday + 2 active bookings (one 'confirmed', one 'transferred')
    → both cancelled, NotificationLog('master_cancel') per booking,
    was_already_closed=False, is_active=False.
    """
    new_date = (datetime.now(UTC) + timedelta(days=20)).date()
    workday = await open_workday(
        session,
        seed_data["master_id"],
        new_date,
        dt_time(10, 0),
        dt_time(20, 0),
        business_tz=BUSINESS_TZ,
    )
    booking_confirmed = await _direct_insert_booking(
        session,
        seed_data,
        start_at=_local_to_utc(new_date, 14, 0),
        end_at=_local_to_utc(new_date, 15, 0),
        status="confirmed",
    )
    booking_transferred = await _direct_insert_booking(
        session,
        seed_data,
        start_at=_local_to_utc(new_date, 16, 0),
        end_at=_local_to_utc(new_date, 17, 0),
        status="transferred",
    )

    result = await close_workday_with_cancellations(
        session, workday.id, business_tz=BUSINESS_TZ
    )
    assert result is not None
    assert result.was_already_closed is False
    assert result.work_date == new_date
    cancelled_ids = {b.id for b in result.cancelled_bookings}
    assert cancelled_ids == {booking_confirmed.id, booking_transferred.id}

    # Bookings persisted as 'cancelled'.
    await session.refresh(booking_confirmed)
    await session.refresh(booking_transferred)
    assert booking_confirmed.status == "cancelled"
    assert booking_transferred.status == "cancelled"

    # WorkDay is closed.
    await session.refresh(workday)
    assert workday.is_active is False

    # NotificationLog('master_cancel') per booking (2 entries total).
    log_count = await session.scalar(
        select(func.count())
        .select_from(NotificationLog)
        .where(NotificationLog.kind == "master_cancel")
    )
    assert log_count == 2


@pytest.mark.asyncio
async def test_close_with_cancellations_skips_cancelled_bookings(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Active workday + 1 confirmed + 1 already-cancelled booking → only
    the confirmed one is cancelled (status IN ('confirmed','transferred')
    filter excludes 'cancelled'). NotificationLog count == 1.
    """
    new_date = (datetime.now(UTC) + timedelta(days=21)).date()
    workday = await open_workday(
        session,
        seed_data["master_id"],
        new_date,
        dt_time(10, 0),
        dt_time(20, 0),
        business_tz=BUSINESS_TZ,
    )
    booking_active = await _direct_insert_booking(
        session,
        seed_data,
        start_at=_local_to_utc(new_date, 14, 0),
        end_at=_local_to_utc(new_date, 15, 0),
        status="confirmed",
    )
    booking_cancelled = await _direct_insert_booking(
        session,
        seed_data,
        start_at=_local_to_utc(new_date, 16, 0),
        end_at=_local_to_utc(new_date, 17, 0),
        status="cancelled",
    )

    result = await close_workday_with_cancellations(
        session, workday.id, business_tz=BUSINESS_TZ
    )
    assert result is not None
    cancelled_ids = {b.id for b in result.cancelled_bookings}
    assert cancelled_ids == {booking_active.id}

    # The pre-cancelled booking is unchanged (still 'cancelled', no re-write).
    await session.refresh(booking_cancelled)
    assert booking_cancelled.status == "cancelled"

    log_count = await session.scalar(
        select(func.count())
        .select_from(NotificationLog)
        .where(NotificationLog.kind == "master_cancel")
    )
    assert log_count == 1
