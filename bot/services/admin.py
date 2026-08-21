"""Admin queries — bookings list + service CRUD (master side).

Contract (spec.md 200-213, 307-309):
- get_today_bookings / get_week_bookings: filter by LOCAL date in business.timezone,
  NOT UTC. slot.slot_hour stored as LOCAL hour — JOIN via Booking.slot_id → Slot.slot_date.
- Booking.client_name_snapshot / service_title_snapshot — already html.escape()'d
  in booking.py:create_booking BEFORE INSERT. Render in /today /week WITHOUT re-escape.
- create_service: no name uniqueness check in MVP (master may add "Стрижка" twice —
  service_id is NULLable in Booking, snapshot catches the actual service at booking time).
- get_client_bookings: список записей клиента (для /mybookings) — по telegram_id,
  только upcoming + active status (confirmed/transferred), без past/cancelled.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Booking, Service, Slot


async def get_today_bookings(
    session: AsyncSession,
    master_id: UUID,
    business_timezone: str,
    *,
    now_utc: datetime | None = None,
) -> list[Booking]:
    """Confirmed/transferred bookings for today (LOCAL date in business.timezone).

    `now_utc` injected for tests (production uses datetime.now(UTC)).
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(business_timezone)
    ref = now_utc or datetime.now(tz)
    today_local = ref.astimezone(tz).date()

    stmt = (
        select(Booking)
        .join(Slot, Booking.slot_id == Slot.id)
        .where(
            Booking.master_id == master_id,
            Slot.slot_date == today_local,
            Booking.status.in_(("confirmed", "transferred")),
        )
        .order_by(Slot.slot_hour)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_week_bookings(
    session: AsyncSession,
    master_id: UUID,
    business_timezone: str,
    *,
    days_ahead: int = 7,
    now_utc: datetime | None = None,
) -> list[Booking]:
    """Confirmed/transferred bookings for next N days (today inclusive → today+N).

    Spec.md says "/week = 7 days" → with days_ahead=7, returns today + 6 future days = 7 days
    total (NOT today + 7 = 8 days). `end_local = today + (days_ahead - 1)`.

    `now_utc` injected for tests.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(business_timezone)
    ref = now_utc or datetime.now(tz)
    today_local = ref.astimezone(tz).date()
    end_local = today_local + timedelta(days=days_ahead - 1)

    stmt = (
        select(Booking)
        .join(Slot, Booking.slot_id == Slot.id)
        .where(
            Booking.master_id == master_id,
            Slot.slot_date >= today_local,
            Slot.slot_date <= end_local,
            Booking.status.in_(("confirmed", "transferred")),
        )
        .order_by(Slot.slot_date, Slot.slot_hour)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def create_service(
    session: AsyncSession,
    business_id: UUID,
    name: str,
    duration_minutes: int,
    price: Decimal,
) -> Service:
    """Create a service. No name uniqueness check in MVP (see module docstring).

    Raises ValueError on invalid input (caller-handler validates, but defense-in-depth).
    """
    if not name or not name.strip():
        raise ValueError("service name must not be empty")
    if len(name) > 255:
        raise ValueError("service name too long (max 255 chars)")
    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be > 0")
    if price < 0:
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
    now_utc: datetime | None = None,
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
        ref = now_utc or datetime.now(tz=None)  # naive comparison — SQLite stores naive
        # Filter on Booking.start_at (UTC). SQLite stores datetime naive (verified
        # test_admin.py 2026-08-21), so compare with naive UTC.
        if ref.tzinfo is not None:
            ref = ref.replace(tzinfo=None)
        stmt = stmt.where(Booking.start_at > ref)
    stmt = stmt.order_by(Booking.start_at)
    result = await session.execute(stmt)
    return list(result.scalars().all())
