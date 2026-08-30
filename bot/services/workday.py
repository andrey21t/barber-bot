"""WorkDay service — open/update/close a master's workday window (Этап 5.1).

Replaces deprecated slots.add_slots / slots.close_slot (Slot-логика — slot_hour
int 0-23). WorkDay stores a window [start_time, end_time] on a concrete date;
30-мин слоты для /slots UI генерируются на лету (Этап 5.4 / 5.6).

Idempotency: UNIQUE INDEX ux_work_days_master_date(master_id, work_date)
in models.py:165 — repeated /openday on the same date → UPDATE start_time /
end_time (NOT INSERT). INSERT race (two concurrent /openday) handled by
the UNIQUE constraint; UPDATE race (shrink checks vs concurrent new booking)
handled by _acquire_advisory_lock — same key as create_booking (booking.py:243)
so they serialise without deadlock.

One-way door (Gap 6, PLANS.md:287-295): shrinking end_time earlier than the
latest active Booking.end_at (OR pushing start_time later than the earliest
active Booking.start_at) leaves existing bookings outside the window.
update_workday SELECTs active bookings violating the new bounds and raises
WorkDayShrinkError listing the conflicts — admin cancels them first.

Expansion (start_time earlier / end_time later) skips the shrink check —
no existing booking can land outside a strictly wider window.

Cross-DB SQL: parens `(start_at < new_start OR end_at > new_end) AND status
IN (...)` are MANDATORY (Gap 8 — critic iter 2 fix: SQL precedence `AND`
binds tighter than `OR`; without parens cancelled bookings would
false-positive the check).

Timezone: Booking.start_at / end_at are stored in UTC (DateTime aware on
Postgres, naive on SQLite). The new window bounds are LOCAL time-of-day on
work_date; we combine (work_date, new_start_time, business_tz) → UTC for the
SQL comparison. business_tz is passed by the caller (admin handler resolves
it via _resolve_master_and_business) — service layer stays config-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Booking, NotificationLog, WorkDay
from bot.services.booking import _acquire_advisory_lock


class WorkDayShrinkError(Exception):
    """Raised by update_workday / close_workday when active bookings would be excluded.

    Triggered when shrinking end_time earlier than max(active Booking.end_at)
    OR pushing start_time later than min(active Booking.start_at), OR when
    closing a workday with any active bookings. Caller (admin handler) renders
    the conflict list to the master so they can cancel the affected bookings.

    Active bookings = status IN ('confirmed', 'transferred') — same set as
    the multi-client capacity check (booking.py:_check_multi_client_capacity)
    and the EXCLUDE constraint (migration 002, dropped in 005). Cancelled
    bookings are excluded — they no longer occupy the window.
    """


async def open_workday(
    session: AsyncSession,
    master_id: UUID,
    work_date: date,
    start_time: time,
    end_time: time,
    business_tz: str,
) -> WorkDay:
    """Open or update the workday window for (master_id, work_date).

    Idempotent via UNIQUE INDEX ux_work_days_master_date:
    - No WorkDay for (master_id, work_date) → INSERT new (start_time, end_time,
      max_concurrent_clients=1 default, is_active=True).
    - Existing WorkDay → delegate to update_workday (Gap 6 shrink checks +
      UPDATE start_time/end_time). is_active=True restored if it was False
      (re-opening a closed day), max_concurrent_clients preserved.

    Race: _acquire_advisory_lock (same key as create_booking) serialises
    concurrent /openday + create_booking on the same (master_id, work_date).

    Raises:
        ValueError: end_time <= start_time (UX-friendly; CheckConstraint
            ck_workday_window_positive also blocks at flush, but raising early
            gives a clearer message before the advisory lock).
    """
    if end_time <= start_time:
        raise ValueError(f"end_time {end_time} must be > start_time {start_time}")

    await _acquire_advisory_lock(session, master_id, work_date)

    existing = await select_workday(session, master_id, work_date)
    if existing is None:
        workday = WorkDay(
            master_id=master_id,
            work_date=work_date,
            start_time=start_time,
            end_time=end_time,
            max_concurrent_clients=1,
            is_active=True,
        )
        session.add(workday)
        await session.commit()
        await session.refresh(workday)
        return workday

    updated = await update_workday(session, existing.id, start_time, end_time, business_tz)

    # Re-open: restore is_active=True if it was False.
    if not existing.is_active:
        await session.execute(
            update(WorkDay).where(WorkDay.id == existing.id).values(is_active=True)
        )
        await session.commit()
        await session.refresh(updated)
    return updated


async def update_workday(
    session: AsyncSession,
    workday_id: UUID,
    new_start_time: time,
    new_end_time: time,
    business_tz: str,
) -> WorkDay:
    """Update start_time / end_time of an existing WorkDay with Gap 6 shrink checks.

    Raises:
        ValueError: new_end_time <= new_start_time (UX message).
        WorkDayShrinkError: active bookings (status IN
            ('confirmed','transferred')) exist with start_at < new_start_utc
            OR end_at > new_end_utc — admin cancels them first.
    """
    if new_end_time <= new_start_time:
        raise ValueError(f"end_time {new_end_time} must be > start_time {new_start_time}")

    workday = await session.get(WorkDay, workday_id)
    if workday is None:
        raise ValueError(f"WorkDay {workday_id} not found")

    new_start_utc, new_end_utc = _window_bounds_utc(
        workday.work_date, new_start_time, new_end_time, business_tz
    )

    # Gap 8: parens MANDATORY — `AND` binds tighter than `OR`, without parens
    # cancelled bookings would false-positive (cancelled.status would bypass
    # the inner OR branch when start_at < new_start_utc but the AND clause
    # enforces status IN ('confirmed','transferred')).
    conflict_stmt = select(Booking).where(
        Booking.master_id == workday.master_id,
        (Booking.start_at < new_start_utc) | (Booking.end_at > new_end_utc),
        Booking.status.in_(("confirmed", "transferred")),
    )
    conflicts = (await session.execute(conflict_stmt)).scalars().all()
    if conflicts:
        raise WorkDayShrinkError(
            f"WorkDay {workday_id} shrink to [{new_start_time}, {new_end_time}] "
            f"would leave {len(conflicts)} active booking(s) outside the window. "
            f"Cancel them first: {[str(b.id) for b in conflicts]}"
        )

    await session.execute(
        update(WorkDay)
        .where(WorkDay.id == workday_id)
        .values(start_time=new_start_time, end_time=new_end_time)
    )
    await session.commit()
    await session.refresh(workday)
    return workday


async def close_workday(
    session: AsyncSession,
    workday_id: UUID,
    business_tz: str,
) -> bool:
    """Mark a WorkDay inactive (is_active=False). Refuse if active bookings exist.

    Returns True if updated or already-closed (idempotent), False if WorkDay
    not found.

    Refuses with WorkDayShrinkError when active bookings overlap the window —
    closing the day hides it from /slots (5.6, is_active filter), but bookings
    under a closed day still bound create_booking (test_create_booking_workday_
    inactive in test_booking_invariants.py). Admin should cancel bookings
    first OR shrink end_time via update_workday to before the earliest booking.
    """
    workday = await session.get(WorkDay, workday_id)
    if workday is None:
        return False

    if workday.is_active is False:
        return True  # idempotent

    # Find bookings OVERLAPPING the existing window (half-open, same semantics
    # as _check_multi_client_capacity: start_at < new_end_utc AND end_at >
    # new_start_utc — touching ranges do not overlap). Any overlap = the
    # booking was created under this workday → refuse close until cancelled.
    new_start_utc, new_end_utc = _window_bounds_utc(
        workday.work_date, workday.start_time, workday.end_time, business_tz
    )
    conflict_stmt = select(Booking).where(
        Booking.master_id == workday.master_id,
        Booking.start_at < new_end_utc,
        Booking.end_at > new_start_utc,
        Booking.status.in_(("confirmed", "transferred")),
    )
    conflicts = (await session.execute(conflict_stmt)).scalars().all()
    if conflicts:
        raise WorkDayShrinkError(
            f"WorkDay {workday_id} cannot be closed — {len(conflicts)} active "
            f"booking(s) remain. Cancel them first: {[str(b.id) for b in conflicts]}"
        )

    await session.execute(update(WorkDay).where(WorkDay.id == workday_id).values(is_active=False))
    await session.commit()
    return True


async def select_workday(session: AsyncSession, master_id: UUID, work_date: date) -> WorkDay | None:
    """Lookup WorkDay by (master_id, work_date). Mirrors _select_workday_for_slot
    in booking.py:117 but exposed here for /openday handler use (was_closed UX fix,
    Session 5.18 F1 — handler reads is_active BEFORE open_workday re-opens it).
    """
    stmt = select(WorkDay).where(WorkDay.master_id == master_id, WorkDay.work_date == work_date)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


@dataclass(frozen=True)
class ClosedDayResult:
    """Result of close_workday_with_cancellations (Session 5.26).

    Returned to /closeday handler for Telegram I/O:
    - workday_id: closed WorkDay.id
    - work_date: closed date (for "✅ День <date> закрыт" message)
    - cancelled_bookings: list of Booking objects (cancelled in this call) —
      handler reads client_id + start_at + client_name_snapshot for each to
      send client notifications "Ваша запись отменена мастером".
    - was_already_closed: True if WorkDay.is_active was already False (idempotent
      no-op close on already-closed day — cancelled_bookings is empty).

    Mirrors CancelResult pattern (booking.py:637) — snapshots already html.escape()'d
    in DB; handler renders WITHOUT re-escape.
    """

    workday_id: UUID
    work_date: date
    cancelled_bookings: list[Booking]
    was_already_closed: bool


async def close_workday_with_cancellations(
    session: AsyncSession,
    workday_id: UUID,
    business_tz: str,
) -> ClosedDayResult | None:
    """Close a WorkDay AND cancel all active bookings on it (Session 5.26).

    Used by /closeday admin command — atomic within one transaction:
      1. SELECT WorkDay. None → return None (caller shows "день не открыт").
      2. If is_active=False → return ClosedDayResult(was_already_closed=True,
         cancelled_bookings=[]) — idempotent no-op close.
      3. SELECT active bookings overlapping the workday window (status IN
         ('confirmed','transferred'), same overlap semantics as close_workday
         — start_at < window_end_utc AND end_at > window_start_utc).
      4. UPDATE Booking SET status='cancelled' WHERE id IN (cancelled ids).
      5. INSERT NotificationLog(booking_id, kind='master_cancel') per cancelled
         booking — SAVEPOINT idempotency (mirror cancel_booking step 7).
      6. UPDATE WorkDay SET is_active=False.
      7. commit.
      8. Return ClosedDayResult (booking objects alive — fixture uses
         expire_on_commit=False).

    Distinct from close_workday (workday.py:173): close_workday REFUSES with
    WorkDayShrinkError if active bookings remain (admin must cancel them first).
    close_workday_with_cancellations AUTO-CANCELS them — that's the whole point
    of /closeday (master wants to close the day, all bookings go away).

    Scheduler jobs (remind_24h / remind_1h) are NOT removed here — service layer
    has no scheduler ref. Handler calls remove_jobs_for_booking per cancelled
    booking AFTER commit (mirror cancel_booking step 10).
    """
    workday = await session.get(WorkDay, workday_id)
    if workday is None:
        return None

    if workday.is_active is False:
        return ClosedDayResult(
            workday_id=workday.id,
            work_date=workday.work_date,
            cancelled_bookings=[],
            was_already_closed=True,
        )

    new_start_utc, new_end_utc = _window_bounds_utc(
        workday.work_date, workday.start_time, workday.end_time, business_tz
    )
    conflict_stmt = select(Booking).where(
        Booking.master_id == workday.master_id,
        Booking.start_at < new_end_utc,
        Booking.end_at > new_start_utc,
        Booking.status.in_(("confirmed", "transferred")),
    )
    conflicts = (await session.execute(conflict_stmt)).scalars().all()

    if conflicts:
        # UPDATE bookings → 'cancelled' (single bulk UPDATE — preserves
        # race protection via WHERE status IN ('confirmed','transferred')).
        booking_ids = [b.id for b in conflicts]
        await session.execute(
            update(Booking)
            .where(
                Booking.id.in_(booking_ids),
                Booking.status.in_(("confirmed", "transferred")),
            )
            .values(status="cancelled")
        )
        # Log NotificationLog(master_cancel) per booking — SAVEPOINT idempotency
        # (mirror cancel_booking.py:749-759). If already logged (duplicate retry),
        # only the savepoint rolls back, main transaction survives.
        for booking_id in booking_ids:
            log_entry = NotificationLog(booking_id=booking_id, kind="master_cancel")
            try:
                async with session.begin_nested():
                    session.add(log_entry)
                    await session.flush()
            except IntegrityError:
                pass  # already logged — idempotent

    await session.execute(
        update(WorkDay).where(WorkDay.id == workday_id).values(is_active=False)
    )
    await session.commit()

    return ClosedDayResult(
        workday_id=workday.id,
        work_date=workday.work_date,
        cancelled_bookings=list(conflicts),
        was_already_closed=False,
    )


def _window_bounds_utc(
    work_date: date,
    start_time: time,
    end_time: time,
    business_tz: str,
) -> tuple[datetime, datetime]:
    """Build UTC datetimes for the workday window [start_time, end_time] on work_date.

    Same pattern as _validate_booking_within_workday in booking.py:159-165 —
    combine work_date + time-of-day + business_tz → UTC. Caller's Booking
    start_at / end_at are stored in UTC (naive on SQLite, aware on Postgres);
    the resulting bounds compare correctly in both (SQLAlchemy handles the
    naive/aware comparison transparently; SQLite stores naive UTC, Postgres
    stores aware UTC, both match an aware UTC datetime on the right side).
    """
    tz = ZoneInfo(business_tz)
    start_utc = datetime.combine(work_date, start_time, tzinfo=tz).astimezone(UTC)
    end_utc = datetime.combine(work_date, end_time, tzinfo=tz).astimezone(UTC)
    return start_utc, end_utc
