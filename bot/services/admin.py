"""Admin queries — bookings list + service CRUD (master side).

Contract (spec.md 200-213, 307-309):
- get_today_bookings / get_week_bookings (Этап 5.4): filter by LOCAL date in business.timezone
  via Booking.start_at (UTC), NOT Slot.slot_date. Booking.start_at already stores the full
  UTC datetime of the booking window start — JOIN Slot is unnecessary and would miss bookings
  where slot.slot_date and booking.start_at LOCAL date diverge (legacy /addslots created Slot
  with slot_date=LOCAL date, but booking.start_at is built from slot.slot_hour LOCAL → UTC).
  Filter: `start_at >= start_of_today_local_utc AND start_at < start_of_tomorrow_local_utc`
  (computed via combine(today_local, time(0,0), tzinfo=tz).astimezone(UTC)).
- Booking.client_name_snapshot / service_title_snapshot — already html.escape()'d
  in booking.py:create_booking BEFORE INSERT. Render in /today /week WITHOUT re-escape.
- create_service: no name uniqueness check in MVP (master may add "Стрижка" twice —
  service_id is NULLable in Booking, snapshot catches the actual service at booking time).
- get_client_bookings: список записей клиента (для /mybookings) — по telegram_id,
  только upcoming + active status (confirmed/transferred), без past/cancelled.
"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import NamedTuple
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Booking, Service, WorkDay


async def get_today_bookings(
    session: AsyncSession,
    master_id: UUID,
    business_timezone: str,
    *,
    now_utc: datetime | None = None,  # must be tz-aware UTC (datetime.now(UTC))
) -> list[Booking]:
    """Confirmed/transferred bookings for today (LOCAL date in business.timezone).

    Filter by Booking.start_at (UTC) — start_at already stores the full UTC datetime
    of the booking window start, so JOIN Slot is unnecessary (Этап 5.4 Gap 1).

    Window: [start_of_today_local_utc, start_of_tomorrow_local_utc) where
    today_local = ref.astimezone(tz).date(). Half-open interval avoids off-by-one
    on bookings exactly at midnight (edge case, but defensive).

    `now_utc` injected for tests (production uses datetime.now(UTC)).
    Bug fix 5.4: default was `datetime.now(tz)` (aware in tz, NOT UTC — naming lie).
    Now `datetime.now(UTC)` matches the parameter name and downstream UTC arithmetic.
    """
    tz = ZoneInfo(business_timezone)
    ref = now_utc or datetime.now(UTC)
    today_local = ref.astimezone(tz).date()
    start_of_today_utc = datetime.combine(today_local, time(0, 0), tzinfo=tz).astimezone(UTC)
    # Combine through LOCAL date, NOT +timedelta(days=1) on UTC datetime —
    # DST-safe (same pattern as get_week_bookings:94-95). For non-DST tz (Russia
    # since 2014) both are equivalent; for DST tz this avoids ±1h off-by-one
    # on the spring-forward / fall-back day.
    start_of_tomorrow_utc = datetime.combine(
        today_local + timedelta(days=1), time(0, 0), tzinfo=tz
    ).astimezone(UTC)

    stmt = (
        select(Booking)
        .where(
            Booking.master_id == master_id,
            Booking.start_at >= start_of_today_utc,
            Booking.start_at < start_of_tomorrow_utc,
            Booking.status.in_(("confirmed", "transferred")),
        )
        .order_by(Booking.start_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_bookings_for_date(
    session: AsyncSession,
    master_id: UUID,
    business_timezone: str,
    target_day: date,
) -> list[Booking]:
    """Active bookings (confirmed/transferred) overlapping a given LOCAL day.

    Used by /openday FSM to pre-prompt master with active bookings on the
    chosen date BEFORE typing the workday window — master sees which
    bookings block a narrow window upfront, doesn't reach WorkDayShrinkError
    on confirm with UUIDs.

    Window: [start_of_target_day_local_utc, start_of_next_day_local_utc) —
    half-open. Identical to get_today_bookings (lines 30-73) but for arbitrary
    target_day instead of ref.astimezone(tz).date().

    Status filter: ('confirmed', 'transferred') — same as update_workday
    and WorkDayShrinkError semantics (workday.py:150-154).
    """
    return await get_bookings_for_date_range(
        session, master_id, business_timezone, target_day, target_day
    )


async def get_bookings_for_date_range(
    session: AsyncSession,
    master_id: UUID,
    business_timezone: str,
    start_day: date,
    end_day: date,
) -> list[Booking]:
    """Active bookings (confirmed/transferred) in LOCAL date range [start_day, end_day].

    Used by /openweek to show bookings only for the OPENED week (Monday..Sunday),
    not "today + 7 days" — get_week_bookings uses rolling window from today,
    which includes past-week bookings on Sunday (today=06.09 → returns 06.09..12.09,
    but /openweek header promises 07.09–13.09).

    Window: [start_of_start_day_local_utc, start_of_(end_day+1)_local_utc) —
    half-open. Identical semantics to get_bookings_for_date but for range.

    Status filter: ('confirmed', 'transferred') — same as get_bookings_for_date.
    """
    tz = ZoneInfo(business_timezone)
    start_utc = datetime.combine(start_day, time(0, 0), tzinfo=tz).astimezone(UTC)
    end_utc = datetime.combine(end_day + timedelta(days=1), time(0, 0), tzinfo=tz).astimezone(UTC)

    stmt = (
        select(Booking)
        .where(
            Booking.master_id == master_id,
            Booking.start_at >= start_utc,
            Booking.start_at < end_utc,
            Booking.status.in_(("confirmed", "transferred")),
        )
        .order_by(Booking.start_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_week_bookings(
    session: AsyncSession,
    master_id: UUID,
    business_timezone: str,
    *,
    days_ahead: int = 7,
    now_utc: datetime | None = None,  # must be tz-aware UTC (datetime.now(UTC))
) -> list[Booking]:
    """Confirmed/transferred bookings for next N days (today inclusive → today+N).

    Spec.md says "/week = 7 days" → with days_ahead=7, returns today + 6 future days = 7 days
    total (NOT today + 7 = 8 days). `end_local = today + (days_ahead - 1)`.

    Filter by Booking.start_at (UTC) — Этап 5.4 Gap 1 (see get_today_bookings docstring).
    Window: [start_of_today_local_utc, start_of_(today+N)_local_utc). Half-open.

    `now_utc` injected for tests. Bug fix 5.4: default `datetime.now(UTC)` (was
    `datetime.now(tz)` — aware in tz, naming lie).
    """
    tz = ZoneInfo(business_timezone)
    ref = now_utc or datetime.now(UTC)
    today_local = ref.astimezone(tz).date()
    end_local = today_local + timedelta(days=days_ahead - 1)
    start_of_today_utc = datetime.combine(today_local, time(0, 0), tzinfo=tz).astimezone(UTC)
    start_of_end_utc = datetime.combine(
        end_local + timedelta(days=1), time(0, 0), tzinfo=tz
    ).astimezone(UTC)

    stmt = (
        select(Booking)
        .where(
            Booking.master_id == master_id,
            Booking.start_at >= start_of_today_utc,
            Booking.start_at < start_of_end_utc,
            Booking.status.in_(("confirmed", "transferred")),
        )
        .order_by(Booking.start_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_all_future_bookings(
    session: AsyncSession,
    master_id: UUID,
    business_timezone: str,
    *,
    now_utc: datetime | None = None,  # must be tz-aware UTC (datetime.now(UTC))
) -> list[Booking]:
    """All confirmed/transferred bookings from today onwards (no upper bound).

    Used by /week command and "неделя" menu button — master wants to see ALL
    upcoming bookings, not just next 7 days. If master opened weeks ahead
    (e.g. 14.09–20.09 already open) and clients booked there, those bookings
    appear too. Replaces get_week_bookings(days_ahead=7) which hid bookings
    beyond day 7.

    Window: [start_of_today_local_utc, +∞). Filter by Booking.start_at (UTC).

    `now_utc` injected for tests. Default `datetime.now(UTC)`.
    """
    tz = ZoneInfo(business_timezone)
    ref = now_utc or datetime.now(UTC)
    today_local = ref.astimezone(tz).date()
    start_of_today_utc = datetime.combine(today_local, time(0, 0), tzinfo=tz).astimezone(UTC)

    stmt = (
        select(Booking)
        .where(
            Booking.master_id == master_id,
            Booking.start_at >= start_of_today_utc,
            Booking.status.in_(("confirmed", "transferred")),
        )
        .order_by(Booking.start_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def create_service(
    session: AsyncSession,
    business_id: UUID,
    name: str,
    duration_minutes: int,
    price: Decimal | None = None,
) -> Service:
    """Create a service. No name uniqueness check in MVP (see module docstring).

    Price is optional (Session 5.10): master announces price separately in
    chat, not via FSM. Field kept nullable for future use.

    Raises ValueError on invalid input (caller-handler validates, but defense-in-depth).
    """
    if not name or not name.strip():
        raise ValueError("service name must not be empty")
    if len(name) > 255:
        raise ValueError("service name too long (max 255 chars)")
    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be > 0")
    if price is not None and price < 0:
        raise ValueError("price must be >= 0")

    service = Service(
        business_id=business_id,
        name=name.strip(),
        duration_minutes=duration_minutes,
        price=price,
        is_active=True,
    )
    session.add(service)
    await session.commit()
    return service


async def get_client_bookings(
    session: AsyncSession,
    client_id: UUID,
    *,
    include_past: bool = False,
    now_utc: datetime | None = None,  # must be tz-aware UTC (datetime.now(UTC))
) -> list[Booking]:
    """Confirmed/transferred bookings for a client.

    /mybookings (spec.md 41): client sees their upcoming bookings.
    - Filter by client_id (resolved by telegram_id in handler/service).
    - Status IN (confirmed, transferred) — cancelled bookings excluded.
    - By default only upcoming (start_at > now_utc); include_past=True returns all.
    - Ordered by start_at ascending (chronological).

    `now_utc` injected for tests.
    """
    from bot.models import Client

    stmt = (
        select(Booking)
        .join(Client, Booking.client_id == Client.id)
        .where(
            Booking.client_id == client_id,
            Booking.status.in_(("confirmed", "transferred")),
        )
    )
    if not include_past:
        # ref is aware UTC (caller passes datetime.now(UTC) or test-aware). SQLAlchemy
        # variant strips tzinfo on SQLite bind (verified empirically 2026-08-23), so
        # aware UTC bind → naive UTC string compared lexicographically with stored
        # naive UTC string — correct. On Postgres, TIMESTAMPTZ vs aware UTC — correct.
        ref = now_utc or datetime.now(UTC)
        stmt = stmt.where(Booking.start_at > ref)
    stmt = stmt.order_by(Booking.start_at)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_active_bookings_for_workday(
    session: AsyncSession,
    workday: WorkDay,
    business_timezone: str,
) -> list[Booking]:
    """Active bookings (status confirmed/transferred) for a given WorkDay.

    Used by admin MODIFY picker (5.10 UX Variant A, donor-standard) to:
    1. Render header «🔒 Занято: HH:MM Имя (услуга)» above slot picker.
    2. Filter picker slots — keep only slots that don't cut bookings
       (start picker: slot <= min(booking.start_at); end picker:
       slot >= max(booking.end_at)).

    Same status set + overlap range as update_workday (workday.py:150-154)
    and get_available_slots_30 (slots.py:172-177) — consistent with
    WorkDayShrinkError semantics on confirm.

    Args:
        session: SQLAlchemy AsyncSession (read-only SELECT).
        workday: WorkDay record — master_id + work_date bounds the query.
            Overlap is computed against workday window in UTC via
            _window_bounds_utc (slots.py:25).
        business_timezone: IANA tz name (e.g. "Europe/Moscow") for LOCAL → UTC.

    Returns:
        List[Booking] ordered by start_at ascending. Empty if no active
        bookings overlap the workday window.
    """
    from bot.services.workday import _window_bounds_utc

    workday_start_utc, workday_end_utc = _window_bounds_utc(
        workday.work_date, workday.start_time, workday.end_time, business_timezone
    )
    stmt = (
        select(Booking)
        .where(
            Booking.master_id == workday.master_id,
            Booking.start_at < workday_end_utc,
            Booking.end_at > workday_start_utc,
            Booking.status.in_(("confirmed", "transferred")),
        )
        .order_by(Booking.start_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ============================================================
# Service CRUD helpers — B.11 (list + soft delete)
# ============================================================


async def list_services(
    session: AsyncSession,
    business_id: UUID,
) -> list[Service]:
    """Active services for a business, ordered by created_at.

    Soft-deleted (is_active=False) are excluded — they remain in DB for
    Booking history integrity (Booking.service_id FK nullable + service_title_snapshot
    catches the actual name at booking time).
    """
    stmt = (
        select(Service)
        .where(
            Service.business_id == business_id,
            Service.is_active.is_(True),
        )
        .order_by(Service.created_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


class ServiceDeactivationResult(NamedTuple):
    """Outcome of deactivate_service — handler renders from this.

    Fields:
        found: services matching the name (active or not) — for "not found" detection.
        deactivated: services actually flipped True → False in this call.
        blocked_bookings: count of active bookings on the would-be-deactivated
            services (non-zero means the operation was rejected).
        already_inactive: services that were already is_active=False (idempotent case).
    """

    found: list[Service]
    deactivated: list[Service]
    blocked_bookings: int
    already_inactive: list[Service]


async def deactivate_service(
    session: AsyncSession,
    business_id: UUID,
    name: str,
) -> ServiceDeactivationResult:
    """Soft-delete services by name (no name uniqueness — see admin.py:13).

    Contract:
    - If no Service rows match (active OR inactive) → found=[], deactivated=[],
      blocked_bookings=0, already_inactive=[] → handler renders "не найдена".
    - If matches exist:
      1. Count active bookings (status in confirmed/transferred) on the ACTIVE
         matches. If > 0 → reject: deactivated=[], blocked_bookings=N.
      2. If 0 active bookings: UPDATE is_active=False for ACTIVE matches.
         already_inactive = matches that were already is_active=False (idempotent).

    Name normalization mirrors cmd_services add (handlers/admin.py:644): caller
    passes raw name, service compares against Service.name as stored. Underscore-
    to-space decode happens in the handler (single source of truth for arg parsing).

    Strip whitespace to mirror create_service (admin.py:243) — without this, a
    service created as "Стрижка_" (stored as "Стрижка" after strip) cannot be
    deleted by the same spelling ("/services del Стрижка_" → "Стрижка " → no
    match). Caller-handler boundary keeps the raw arg; service normalizes.
    """
    name = name.strip()
    stmt = select(Service).where(
        Service.business_id == business_id,
        Service.name == name,
    )
    found = list((await session.execute(stmt)).scalars().all())

    if not found:
        return ServiceDeactivationResult(
            found=[], deactivated=[], blocked_bookings=0, already_inactive=[]
        )

    active = [s for s in found if s.is_active]
    already_inactive = [s for s in found if not s.is_active]

    if not active:
        # All matches already soft-deleted — idempotent.
        return ServiceDeactivationResult(
            found=found,
            deactivated=[],
            blocked_bookings=0,
            already_inactive=already_inactive,
        )

    # Active bookings on the to-be-deactivated services (future OR past —
    # spec keeps history bookings visible via snapshot, but we block ANY
    # active-status booking to avoid orphaned confirmed bookings the master
    # would lose track of). See plan v3.3 B.11 DoD: "Сначала отмените записи".
    active_ids = [s.id for s in active]
    count_stmt = (
        select(func.count())
        .select_from(Booking)
        .where(
            Booking.service_id.in_(active_ids),
            Booking.status.in_(("confirmed", "transferred")),
        )
    )
    blocked = int((await session.execute(count_stmt)).scalar_one())

    if blocked > 0:
        return ServiceDeactivationResult(
            found=found,
            deactivated=[],
            blocked_bookings=blocked,
            already_inactive=already_inactive,
        )

    await session.execute(
        update(Service)
        .where(Service.id.in_(active_ids))
        .values(is_active=False)
    )
    await session.commit()

    return ServiceDeactivationResult(
        found=found,
        deactivated=active,
        blocked_bookings=0,
        already_inactive=already_inactive,
    )
