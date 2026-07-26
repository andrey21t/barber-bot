"""Booking creation — Pure service layer.

Contract (MY-VIBE-RULES.md, spec.md 307-309):
- Timezone conversion: slot.slot_hour LOCAL → start_at UTC
- HTML escape: client_name_snapshot + service_title_snapshot — escape in service BEFORE INSERT
- UNIQUE(slot_id) — SQLite fallback for no-double-booking
- SQLite race protection: UPDATE slot ... WHERE status='open' + rowcount check
"""

import html
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.models import Booking, Client, NotificationLog, Service, Slot
from bot.schemas import BookingCreate


class SlotAlreadyBookedError(Exception):
    """UNIQUE(slot_id) violated — someone booked this slot concurrently."""


class SlotInPastError(Exception):
    """slot.start_at <= now() — booking in the past is not allowed."""


class SlotClosedError(Exception):
    """slot.status != 'open' — slot was closed by master."""


@dataclass(frozen=True)
class BookingCreatedData:
    """Result of create_booking — passed to handler for Telegram I/O."""

    booking_id: UUID
    slot_id: UUID
    master_id: UUID
    business_id: UUID
    client_id: UUID
    start_at: datetime
    end_at: datetime
    client_name_snapshot: str  # already escaped
    service_title_snapshot: str  # already escaped
    master_notification_text: str


async def _select_open_slot(session: AsyncSession, slot_id: UUID) -> Slot:
    stmt = select(Slot).where(Slot.id == slot_id)
    result = await session.execute(stmt)
    slot = result.scalar_one_or_none()
    if slot is None:
        raise SlotClosedError(f"Slot {slot_id} not found")
    if slot.status == "booked":
        raise SlotAlreadyBookedError(f"Slot {slot_id} already booked")
    if slot.status == "closed":
        raise SlotClosedError(f"Slot {slot_id} closed by master")
    return slot


async def _select_business_timezone(session: AsyncSession, business_id: UUID) -> str:
    from bot.models import Business

    stmt = select(Business.timezone).where(Business.id == business_id)
    result = await session.execute(stmt)
    tz = result.scalar_one_or_none()
    return tz or "Europe/Moscow"


def _build_start_at(slot: Slot, business_timezone: str) -> datetime:
    """Convert slot (LOCAL hour in business.timezone) to UTC datetime."""
    tz = ZoneInfo(business_timezone)
    local_dt = datetime.combine(slot.slot_date, time(hour=slot.slot_hour), tzinfo=tz)
    return local_dt.astimezone(UTC)


def _build_end_at(
    start_at: datetime, service: Service | None, default_duration_min: int
) -> datetime:
    duration = service.duration_minutes if service is not None else default_duration_min
    return start_at + timedelta(minutes=duration)


async def _select_service(
    session: AsyncSession, service_id: UUID | None
) -> Service | None:
    if service_id is None:
        return None
    stmt = select(Service).where(Service.id == service_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _select_or_create_client(
    session: AsyncSession, telegram_id: int
) -> Client:
    stmt = select(Client).where(Client.telegram_id == telegram_id)
    result = await session.execute(stmt)
    client = result.scalar_one_or_none()
    if client is not None:
        return client
    client = Client(telegram_id=telegram_id)
    session.add(client)
    try:
        await session.flush()  # populate id
    except IntegrityError:
        # Race: another concurrent request created this client between our
        # SELECT and INSERT. UNIQUE(telegram_id) — see bot/models.py:84.
        # Safe to rollback whole session here: slot is persistent (SELECT only),
        # booking not yet added, no uncommitted side effects to preserve.
        await session.rollback()
        result = await session.execute(stmt)
        client = result.scalar_one()
    return client


async def create_booking(
    session: AsyncSession,
    payload: BookingCreate,
    *,
    business_id: UUID,
    master_id: UUID,
    telegram_id: int,
) -> BookingCreatedData:
    """Create booking in a transaction.

    Steps (spec.md 192-198):
      1. SELECT slot WHERE id=slot_id AND status='open'
      2. If slot.status != 'open' → raise SlotClosedError / SlotAlreadyBookedError
      3. Build start_at (UTC) from slot.slot_hour (LOCAL in business.timezone)
      4. If start_at <= now() → raise SlotInPastError
      5. html.escape(client_name, service_title) BEFORE INSERT
      6. INSERT booking (UNIQUE slot_id guard catches double-click)
      7. UPDATE slot.status='booked' WHERE id=? AND status='open' — rowcount check (SQLite race)
      8. INSERT notifications_log(master_new) — UNIQUE guard
      9. Return BookingCreatedData with master_notification_text
    """
    settings = get_settings()

    slot = await _select_open_slot(session, payload.slot_id)
    business_tz = await _select_business_timezone(session, business_id)
    start_at = _build_start_at(slot, business_tz)

    if start_at <= datetime.now(UTC):
        raise SlotInPastError(f"Slot {payload.slot_id} start_at={start_at} is in the past")

    service = await _select_service(session, payload.service_id)
    end_at = _build_end_at(start_at, service, settings.SERVICE_DEFAULT_DURATION_MIN)

    # HTML escape BEFORE INSERT — stored escaped, rendered without double-escape
    escaped_name = html.escape(payload.client_name, quote=False)
    escaped_service = html.escape(payload.service_title, quote=False)

    client = await _select_or_create_client(session, telegram_id)

    booking = Booking(
        slot_id=slot.id,
        business_id=business_id,
        master_id=master_id,
        client_id=client.id,
        service_id=payload.service_id,
        service_title_snapshot=escaped_service,
        client_name_snapshot=escaped_name,
        start_at=start_at,
        end_at=end_at,
        status="confirmed",
    )
    session.add(booking)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise SlotAlreadyBookedError(
            f"Slot {slot.id} already booked (UNIQUE constraint)"
        ) from exc

    # SQLite race protection: UPDATE ... WHERE status='open' + rowcount check
    upd = (
        update(Slot)
        .where(Slot.id == slot.id, Slot.status == "open")
        .values(status="booked")
    )
    res = await session.execute(upd)
    if cast("CursorResult[Any]", res).rowcount == 0:
        # Lost race: slot was closed/booked between SELECT and UPDATE
        await session.rollback()
        raise SlotAlreadyBookedError(
            f"Slot {slot.id} was taken/closed between SELECT and UPDATE"
        )

    # notifications_log idempotency — UNIQUE(booking_id, kind)
    # SAVEPOINT isolates log_entry INSERT from main transaction (booking + slot):
    # if IntegrityError (already logged — idempotent path), only savepoint rolls back,
    # main transaction (booking + slot) survives and commits below.
    # NOTE: session.add(log_entry) MUST go INSIDE begin_nested() — otherwise
    # log_entry remains in session.new after savepoint rollback and outer commit
    # attempts re-INSERT → IntegrityError unhandled → whole outer tx rolls back.
    log_entry = NotificationLog(booking_id=booking.id, kind="master_new")
    try:
        async with session.begin_nested():
            session.add(log_entry)
            await session.flush()
    except IntegrityError:
        # Already logged — idempotent, OK. Savepoint rolled back, log_entry expunged.
        pass

    await session.commit()

    # Format notification text for master (rendered in HTML parse mode, no re-escape needed)
    local_time = start_at.astimezone(ZoneInfo(business_tz))
    formatted_time = local_time.strftime("%d %B %Y, %H:%M")
    master_text = (
        f"Новая запись:\n"
        f"📅 {formatted_time}\n"
        f"👤 {escaped_name}\n"
        f"💇 {escaped_service}"
    )

    return BookingCreatedData(
        booking_id=booking.id,
        slot_id=slot.id,
        master_id=master_id,
        business_id=business_id,
        client_id=client.id,
        start_at=start_at,
        end_at=end_at,
        client_name_snapshot=escaped_name,
        service_title_snapshot=escaped_service,
        master_notification_text=master_text,
    )
