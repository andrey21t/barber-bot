"""Slot operations — Pure service layer (no Telegram API).

Этап 5.4 introduces WorkDay-based 30-min slot generation (PLANS.md Gap 1, Blocker C):
- `get_30min_slots_from_workday` + `TimeSlot30` dataclass: generates candidate
  30-min windows from WorkDay [start_time, end_time] for /slots UI (Этап 5.8).
- `add_slots` / `close_slot` / `get_available_slots` remain for the legacy
  slot-based /book flow (deprecated after 5.8, but kept until /slots rollout).
- Slot model is deprecated — migration 005 transferred data to WorkDay, migration
  006 drops the slots table after smoke-test (PLANS.md:253).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Booking, Slot, WorkDay
from bot.services.workday import _window_bounds_utc

# ============================================================
# 30-мин slots from WorkDay (Этап 5.4 — for /slots UI in 5.8)
# ============================================================


@dataclass(frozen=True)
class TimeSlot30:
    """Candidate 30-min booking window generated from WorkDay.

    Этап 5.4 (PLANS.md Gap 1, Blocker C): /slots UI shows buttons T, T+30,
    T+60, ... from workday.start_time to end_time. Each TimeSlot30 represents
    one candidate start time. /book creates a Booking with start_at=start_at_utc,
    end_at=start_at_utc + service.duration_minutes (may extend beyond 30 min —
    booking occupies its full service duration, the 30-min is just the GRID).

    Attributes:
        start_at_utc: aware UTC datetime — value for Booking.start_at.
        start_time_local: LOCAL time (HH:MM) — for keyboard label generation
            and for _build_start_at_from_workday (which takes work_date +
            start_time_local → UTC).
        label: pre-formatted "HH:MM" string in business_timezone — used as
            button label in slot_picker_keyboard_30min. Caller passes through
            to keyboard without re-formatting (avoids double tz conversion).
    """

    start_at_utc: datetime
    start_time_local: time
    label: str


async def get_30min_slots_from_workday(
    workday: WorkDay,
    business_timezone: str,
    *,
    now_utc: datetime | None = None,  # must be tz-aware UTC (datetime.now(UTC))
) -> list[TimeSlot30]:
    """Generate candidate 30-min booking windows from WorkDay.

    Этап 5.4 (PLANS.md Gap 1, Blocker C): /slots UI shows T, T+30, T+60, ...
    from workday.start_time to end_time (exclusive end — booking that starts
    exactly at end_time would have end_at > end_time, violating WorkDay
    invariant from 5.3). With 30-min step, the last slot starts at
    end_time - 30min.

    Past slots filtered out (now_utc injected for tests): slot.start_at_utc <=
    now_utc excluded — booking in the past is invalid (SlotInPastError in
    create_booking would catch it later, but /slots UI hides them upfront for
    better UX).

    NB: occupancy check (Этап 5.6 «Мест нет») is NOT here — this helper
    returns ALL candidate slots from the workday grid, regardless of existing
    bookings. The /slots handler (5.8) filters by occupancy using count of
    overlapping bookings vs WorkDay.max_concurrent_clients (5.5). Keeping
    occupancy out of this helper: separation of concerns (grid generation vs
    availability), and 5.8 may want to render "disabled" buttons for full
    slots (not hide them).

    Args:
        workday: WorkDay record with start_time, end_time, work_date.
        business_timezone: IANA tz name for LOCAL → UTC conversion.
        now_utc: injected for tests (production uses datetime.now(UTC)).

    Returns:
        List of TimeSlot30 ordered by start_at_utc ascending. Empty if
        workday.end_time <= workday.start_time (defensive — CheckConstraint
        already enforces end > start, but no raise for invalid input).
    """
    tz = ZoneInfo(business_timezone)
    ref = now_utc or datetime.now(UTC)

    # Generate LOCAL times: start_time, +30, +60, ..., until end_time.
    # Half-open [start_time, end_time) — last slot starts at end_time - 30min.
    # If end_time - start_time < 30min, no slots fit (defensive).
    slots: list[TimeSlot30] = []
    cursor = datetime.combine(workday.work_date, workday.start_time, tzinfo=tz)
    end_dt = datetime.combine(workday.work_date, workday.end_time, tzinfo=tz)
    while cursor + timedelta(minutes=30) <= end_dt:
        start_at_utc = cursor.astimezone(UTC)
        # Skip past slots (UX: hide unavailable past windows from /slots).
        if start_at_utc > ref:
            local_time = cursor.time()
            label = local_time.strftime("%H:%M")
            slots.append(
                TimeSlot30(
                    start_at_utc=start_at_utc,
                    start_time_local=local_time,
                    label=label,
                )
            )
        cursor += timedelta(minutes=30)
    return slots


# ============================================================
# Occupancy filter (Этап 5.6 «Мест нет» — capacity check on 30-min grid)
# ============================================================


async def get_available_slots_30(
    session: AsyncSession,
    workday: WorkDay,
    business_timezone: str,
    *,
    now_utc: datetime | None = None,
    min_duration_min: int = 0,
) -> list[TimeSlot30]:
    """Filter 30-min slots from workday grid by occupancy (capacity check).

    Этап 5.6 (PLANS.md:15): /slots UI (5.8) shows 30-min slots that have at
    least one free capacity — count of overlapping active bookings (status IN
    'confirmed'/'transferred', half-open overlap) < workday.max_concurrent_clients.

    Half-open overlap (mirror booking.py:_check_multi_client_capacity:226-227):
        booking.start_at < slot.end_utc AND booking.end_at > slot.start_at_utc
    Booking range crosses multiple grid cells (service.duration_minutes > 30):
    all cells overlapped become occupied — overlap measured against **full
    booking range**, not the 30-min grid cell.

    Status filter: only 'confirmed'/'transferred' (cancelled excluded, mirror
    _check_multi_client_capacity:228).
    Past slots: filtered in get_30min_slots_from_workday via now_utc injection.
    Closed WorkDay (is_active=False): NOT filtered here — handler 5.8 decides
    (separation of concerns; this function is read-only, no policy).

    Session 5.27 (BUG2 fix): slots where slot.start + min_duration_min > end_time
    are filtered OUT when min_duration_min > 0. Without this, /slots and /book
    fallback would show e.g. 15:30 slot in workday 13:30–16:00, but create_booking
    with SERVICE_DEFAULT_DURATION_MIN=60 raises BookingOutsideWorkDayError
    (15:30+60=16:30 > 16:00), surfaced as "День закрыт мастером" — misleading.
    Default 0 = no filter (preserves old behaviour for occupancy-only tests);
    handlers pass SERVICE_DEFAULT_DURATION_MIN explicitly.

    NB: SQLite stores Booking.start_at naive, Postgres aware. Normalize in
    Python loop (b.start_at.replace(tzinfo=UTC) if b.start_at.tzinfo is None).
    SQLAlchemy handles at SQL level, but Python-level comparison does not.

    Args:
        session: SQLAlchemy AsyncSession (read-only SELECT).
        workday: WorkDay record — provides master_id, work_date, start_time,
            end_time, max_concurrent_clients.
        business_timezone: IANA tz name (e.g. "Europe/Moscow") for LOCAL → UTC.
        now_utc: injected for tests (production uses datetime.now(UTC)).
        min_duration_min: when > 0, slots where start + min_duration_min >
            workday.end_time are filtered out. Handlers that know the actual
            service duration should pass it (default 0 = no filter, preserves
            backward-compat for occupancy-only callers).

    Returns:
        List of TimeSlot30 (subset of get_30min_slots_from_workday output)
        ordered by start_at_utc ascending. Empty if all slots are occupied OR
        workday window < 30 min OR no slot fits the min_duration.
    """
    candidates = await get_30min_slots_from_workday(workday, business_timezone, now_utc=now_utc)
    if not candidates:
        return []

    workday_start_utc, workday_end_utc = _window_bounds_utc(
        workday.work_date, workday.start_time, workday.end_time, business_timezone
    )

    # Session 5.27 BUG2: drop slots where start + min_duration_min crosses end_time.
    # create_booking with SERVICE_DEFAULT_DURATION_MIN=60 enforces [start_at, end_at]
    # ⊆ [workday.start_time, end_time] via _validate_booking_within_workday — a
    # slot visible in picker but rejected at confirm causes misleading error.
    # Default min_duration_min=0 means no filter (preserves old behaviour for
    # occupancy-only callers/tests); handlers pass SERVICE_DEFAULT_DURATION_MIN.
    if min_duration_min > 0:
        candidates = [
            s
            for s in candidates
            if s.start_at_utc + timedelta(minutes=min_duration_min) <= workday_end_utc
        ]
        if not candidates:
            return []

    bookings_stmt = select(Booking).where(
        Booking.master_id == workday.master_id,
        Booking.start_at < workday_end_utc,
        Booking.end_at > workday_start_utc,
        Booking.status.in_(("confirmed", "transferred")),
    )
    bookings = (await session.execute(bookings_stmt)).scalars().all()
    # Normalize naive SQLite datetimes to aware UTC for Python-level comparison.
    normalized = [
        (
            b.start_at if b.start_at.tzinfo else b.start_at.replace(tzinfo=UTC),
            b.end_at if b.end_at.tzinfo else b.end_at.replace(tzinfo=UTC),
        )
        for b in bookings
    ]

    available: list[TimeSlot30] = []
    for slot in candidates:
        slot_end_utc = slot.start_at_utc + timedelta(minutes=30)
        overlap_count = sum(
            1 for bs, be in normalized if bs < slot_end_utc and be > slot.start_at_utc
        )
        if overlap_count < workday.max_concurrent_clients:
            available.append(slot)
    return available


# ============================================================
# Legacy Slot operations (deprecated after 5.8 /slots rollout)
# ============================================================


async def add_slots(
    session: AsyncSession,
    master_id: UUID,
    slot_date: date,
    hours: list[int],
) -> list[Slot]:
    """Open slots on a date for master. Skip already-existing (idempotent).

    DEPRECATED (Этап 5.4): /addslots deprecated alias — master opens workday
    window via /openday (5.1) instead of listing hourly slots. This function
    remains for backwards compat until 5.10 inline-часы replace text input.
    Returns list of newly created slots (existing ones skipped).
    """
    if not hours:
        raise ValueError("at least one hour required")
    if not 0 <= min(hours) <= 23 or not 0 <= max(hours) <= 23:
        raise ValueError("slot_hour must be 0-23")

    # Get already-open slots on this date for this master
    existing_stmt = select(Slot).where(Slot.master_id == master_id, Slot.slot_date == slot_date)
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
            # Composite unique (master_id, slot_date, slot_hour) violated by concurrent insert.
            # Don't reference `hour` loop variable here — it carries the last iterated value,
            # not the conflicting hour (B2 fix). Generalise the message instead.
            await session.rollback()
            raise SlotAlreadyExistsError(
                f"Slot {master_id}/{slot_date} — one of hours already exists"
            ) from exc
    await session.commit()
    return new_slots


class SlotAlreadyExistsError(Exception):
    """Raised when slot (master_id, slot_date, slot_hour) already exists."""


async def close_slot(session: AsyncSession, slot_id: UUID) -> bool:
    """Close a slot (set status='closed'). Refuse if already booked.

    DEPRECATED (Этап 5.4): /closeslot deprecated alias — master closes the
    entire workday window via /closeday or shrinks end_time (5.10 inline).
    This function remains for backwards compat.

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

    DEPRECATED (Этап 5.4): use get_30min_slots_from_workday for the WorkDay
    grid. This function remains for the legacy slot-based /book flow until
    5.8 introduces /slots command + WorkDay-based booking.

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
