"""Tests for bot.services.booking.create_booking.

Coverage (Acceptance Contract):
- happy path: INSERT booking + slot.status='booked' via SELECT
- idempotency: повторный INSERT того же slot_id → SlotAlreadyBookedError
- SlotInPastError: start_at <= now
- SlotClosedError: slot.status='closed' or not found
- timezone conversion: slot.slot_hour LOCAL → start_at UTC
- html.escape: client_name_snapshot + service_title_snapshot — stored escaped
"""

import html
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from bot.models import Booking, Slot
from bot.schemas import BookingCreate
from bot.services.booking import (
    BookingCreatedData,
    SlotAlreadyBookedError,
    SlotClosedError,
    SlotInPastError,
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
