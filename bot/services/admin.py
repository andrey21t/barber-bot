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

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
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
