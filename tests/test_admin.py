"""Tests for bot.services.admin — bookings list + service CRUD.

Coverage:
- get_today_bookings: only today (LOCAL), only confirmed/transferred, timezone edge case
- get_week_bookings: 7-day range, only within window, only active statuses
- create_service: happy path + validation (empty name, 0 duration, negative price)

NOT covered here: handlers (Pure I/O per client.py:1 — argparse + render only,
business logic in services). Skipping handler tests per AGENTS.md § anti-overengineering
rule 3 (single occurrence ≠ pain; tests on arg parsing add no coverage beyond services).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from bot.models import Booking, Service, Slot
from bot.services.admin import (
    create_service,
    get_client_bookings,
    get_today_bookings,
    get_week_bookings,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


# ============================================================
# Helpers — extend seed_data with bookings in multiple dates
# ============================================================
async def _make_booking(
    session: AsyncSession,
    *,
    slot_id: UUID,
    business_id: UUID,
    master_id: UUID,
    client_id: UUID,
    start_at_utc: datetime,
    status: str = "confirmed",
    client_name: str = "Паша",
    service_title: str = "Стрижка",
) -> Booking:
    """Insert a Booking row + update linked slot to 'booked' (consistent with booking.py)."""
    booking = Booking(
        slot_id=slot_id,
        business_id=business_id,
        master_id=master_id,
        client_id=client_id,
        service_id=None,
        service_title_snapshot=service_title,
        service_price_snapshot=None,
        client_name_snapshot=client_name,
        start_at=start_at_utc,
        end_at=start_at_utc + timedelta(minutes=60),
        status=status,
    )
    session.add(booking)
    # Update slot to match booking status (mirror create_booking side effect)
    from sqlalchemy import update

    await session.execute(
        update(Slot)
        .where(Slot.id == slot_id)
        .values(status="booked" if status in ("confirmed", "transferred") else "open")
    )
    await session.commit()
    return booking


async def _make_slot(
    session: AsyncSession,
    *,
    master_id: UUID,
    slot_date_local: "datetime",
    hour_local: int,
    status: str = "open",
) -> Slot:
    """Insert a Slot row. slot_date_local.date() is used for slot.slot_date."""
    slot = Slot(
        master_id=master_id,
        slot_date=slot_date_local.date(),
        slot_hour=hour_local,
        status=status,
    )
    session.add(slot)
    await session.flush()
    return slot


def _local_dt(local_date_str: str, hour: int, tz: str = "Europe/Moscow") -> datetime:
    """Build a tz-aware LOCAL datetime → return as UTC datetime (for start_at)."""
    y, m, d = map(int, local_date_str.split("-"))
    local = datetime(y, m, d, hour, tzinfo=ZoneInfo(tz))
    return local.astimezone(UTC)


def _utc_naive(local_date_str: str, hour: int, tz: str = "Europe/Moscow") -> datetime:
    """Build LOCAL datetime → return as naive UTC datetime.

    SQLite DateTime(timezone=True) actually stores TIMESTAMP WITHOUT TZ on SQLite
    (verified in test_admin.py 2026-08-21). Round-trip from tz-aware insert → naive read.
    Use _utc_naive for both INSERT (consistent with SQLite storage) AND assertions
    so both sides are naive UTC.
    """
    return _local_dt(local_date_str, hour, tz).replace(tzinfo=None)


# ============================================================
# get_today_bookings
# ============================================================
@pytest.mark.asyncio
async def test_get_today_returns_only_today_confirmed(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Bookings: 1 today + 1 tomorrow + 1 today-cancelled → returns only today+confirmed."""
    tz = "Europe/Moscow"
    # "now" — fixed at 2026-03-15 12:00 Moscow for deterministic test
    now_utc = _local_dt("2026-03-15", 12, tz)
    today_local = "2026-03-15"

    # Booking today at 14:00 Moscow (confirmed)
    slot_today = await _make_slot(
        session,
        master_id=seed_data["master_id"],
        slot_date_local=datetime.fromisoformat(f"{today_local}T00:00:00+03:00"),
        hour_local=14,
    )
    await _make_booking(
        session,
        slot_id=slot_today.id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        client_id=seed_data["client"].id,
        start_at_utc=_utc_naive(today_local, 14, tz),
        status="confirmed",
    )

    # Booking tomorrow at 15:00 Moscow (should NOT appear)
    slot_tomorrow = await _make_slot(
        session,
        master_id=seed_data["master_id"],
        slot_date_local=datetime.fromisoformat("2026-03-16T00:00:00+03:00"),
        hour_local=15,
    )
    await _make_booking(
        session,
        slot_id=slot_tomorrow.id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        client_id=seed_data["client"].id,
        start_at_utc=_utc_naive("2026-03-16", 15, tz),
        status="confirmed",
    )

    # Booking today at 16:00 Moscow (cancelled — should NOT appear)
    slot_today_cancel = await _make_slot(
        session,
        master_id=seed_data["master_id"],
        slot_date_local=datetime.fromisoformat(f"{today_local}T00:00:00+03:00"),
        hour_local=16,
    )
    await _make_booking(
        session,
        slot_id=slot_today_cancel.id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        client_id=seed_data["client"].id,
        start_at_utc=_utc_naive(today_local, 16, tz),
        status="cancelled",
    )

    result = await get_today_bookings(session, seed_data["master_id"], tz, now_utc=now_utc)
    assert len(result) == 1
    assert result[0].start_at == _utc_naive(today_local, 14, tz)


@pytest.mark.asyncio
async def test_get_today_timezone_edge_case(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """UTC midnight edge case: 23:00 UTC = 02:00 next-day Moscow.

    If now=2026-03-15 23:00 UTC (02:00 March 16 Moscow), today_local=March 16 in Moscow.
    A booking on March 16 at 11:00 Moscow (08:00 UTC) should be returned as "today".
    """
    tz = "Europe/Moscow"
    # now_utc = 2026-03-15 23:00 UTC = 2026-03-16 02:00 Moscow
    now_utc = datetime(2026, 3, 15, 23, 0, tzinfo=UTC)
    # today_local per Moscow = 2026-03-16
    today_local = "2026-03-16"

    slot = await _make_slot(
        session,
        master_id=seed_data["master_id"],
        slot_date_local=datetime.fromisoformat(f"{today_local}T00:00:00+03:00"),
        hour_local=11,
    )
    await _make_booking(
        session,
        slot_id=slot.id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        client_id=seed_data["client"].id,
        start_at_utc=_utc_naive(today_local, 11, tz),
        status="confirmed",
    )

    result = await get_today_bookings(session, seed_data["master_id"], tz, now_utc=now_utc)
    assert len(result) == 1
    assert result[0].start_at == _utc_naive(today_local, 11, tz)


@pytest.mark.asyncio
async def test_get_today_returns_transferred_status(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Bookings with status='transferred' should also appear (per spec.md 209)."""
    tz = "Europe/Moscow"
    now_utc = _local_dt("2026-03-15", 12, tz)
    today_local = "2026-03-15"

    slot = await _make_slot(
        session,
        master_id=seed_data["master_id"],
        slot_date_local=datetime.fromisoformat(f"{today_local}T00:00:00+03:00"),
        hour_local=14,
    )
    await _make_booking(
        session,
        slot_id=slot.id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        client_id=seed_data["client"].id,
        start_at_utc=_utc_naive(today_local, 14, tz),
        status="transferred",
    )

    result = await get_today_bookings(session, seed_data["master_id"], tz, now_utc=now_utc)
    assert len(result) == 1
    assert result[0].status == "transferred"


@pytest.mark.asyncio
async def test_get_today_no_bookings_returns_empty(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """No bookings today → empty list (not None, not error)."""
    tz = "Europe/Moscow"
    now_utc = _local_dt("2026-03-15", 12, tz)
    result = await get_today_bookings(session, seed_data["master_id"], tz, now_utc=now_utc)
    assert result == []


# ============================================================
# get_week_bookings
# ============================================================
@pytest.mark.asyncio
async def test_get_week_returns_7_day_window(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """days_ahead=7 → today + 6 future days = 7 days total (NOT 8 — see service docstring).

    Day 0 (today), day 3, day 6 — all in window. Day 7 — outside.
    """
    tz = "Europe/Moscow"
    now_utc = _local_dt("2026-03-15", 12, tz)
    base = "2026-03-15"

    # Day 0 — today
    slot_d0 = await _make_slot(
        session,
        master_id=seed_data["master_id"],
        slot_date_local=datetime.fromisoformat(f"{base}T00:00:00+03:00"),
        hour_local=10,
    )
    await _make_booking(
        session,
        slot_id=slot_d0.id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        client_id=seed_data["client"].id,
        start_at_utc=_utc_naive(base, 10, tz),
    )

    # Day 3
    slot_d3 = await _make_slot(
        session,
        master_id=seed_data["master_id"],
        slot_date_local=datetime.fromisoformat("2026-03-18T00:00:00+03:00"),
        hour_local=11,
    )
    await _make_booking(
        session,
        slot_id=slot_d3.id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        client_id=seed_data["client"].id,
        start_at_utc=_utc_naive("2026-03-18", 11, tz),
    )

    # Day 6 — last day in 7-day window (today + 6)
    slot_d6 = await _make_slot(
        session,
        master_id=seed_data["master_id"],
        slot_date_local=datetime.fromisoformat("2026-03-21T00:00:00+03:00"),
        hour_local=12,
    )
    await _make_booking(
        session,
        slot_id=slot_d6.id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        client_id=seed_data["client"].id,
        start_at_utc=_utc_naive("2026-03-21", 12, tz),
    )

    # Day 7 — outside 7-day window (today + 7 = 8th day, excluded)
    slot_d7 = await _make_slot(
        session,
        master_id=seed_data["master_id"],
        slot_date_local=datetime.fromisoformat("2026-03-22T00:00:00+03:00"),
        hour_local=13,
    )
    await _make_booking(
        session,
        slot_id=slot_d7.id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        client_id=seed_data["client"].id,
        start_at_utc=_utc_naive("2026-03-22", 13, tz),
    )

    result = await get_week_bookings(
        session, seed_data["master_id"], tz, days_ahead=7, now_utc=now_utc
    )
    assert len(result) == 3  # day 0, 3, 6 — not day 7


@pytest.mark.asyncio
async def test_get_week_excludes_cancelled(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Cancelled bookings excluded from week view."""
    tz = "Europe/Moscow"
    now_utc = _local_dt("2026-03-15", 12, tz)

    slot = await _make_slot(
        session,
        master_id=seed_data["master_id"],
        slot_date_local=datetime.fromisoformat("2026-03-16T00:00:00+03:00"),
        hour_local=10,
    )
    await _make_booking(
        session,
        slot_id=slot.id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        client_id=seed_data["client"].id,
        start_at_utc=_utc_naive("2026-03-16", 10, tz),
        status="cancelled",
    )

    result = await get_week_bookings(
        session, seed_data["master_id"], tz, days_ahead=7, now_utc=now_utc
    )
    assert result == []


@pytest.mark.asyncio
async def test_get_week_orders_by_date_then_hour(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Bookings ordered by slot_date, then slot_hour (spec.md 211 — chronological list)."""
    tz = "Europe/Moscow"
    now_utc = _local_dt("2026-03-15", 12, tz)

    # Insert out-of-order: day2 16:00, day1 11:00, day2 10:00
    for date_str, hour in [("2026-03-17", 16), ("2026-03-16", 11), ("2026-03-17", 10)]:
        slot = await _make_slot(
            session,
            master_id=seed_data["master_id"],
            slot_date_local=datetime.fromisoformat(f"{date_str}T00:00:00+03:00"),
            hour_local=hour,
        )
        await _make_booking(
            session,
            slot_id=slot.id,
            business_id=seed_data["business_id"],
            master_id=seed_data["master_id"],
            client_id=seed_data["client"].id,
            start_at_utc=_utc_naive(date_str, hour, tz),
        )

    result = await get_week_bookings(
        session, seed_data["master_id"], tz, days_ahead=7, now_utc=now_utc
    )
    assert len(result) == 3
    # Expected order: 2026-03-16 11:00, 2026-03-17 10:00, 2026-03-17 16:00
    assert result[0].start_at == _utc_naive("2026-03-16", 11, tz)
    assert result[1].start_at == _utc_naive("2026-03-17", 10, tz)
    assert result[2].start_at == _utc_naive("2026-03-17", 16, tz)


# ============================================================
# create_service
# ============================================================
@pytest.mark.asyncio
async def test_create_service_happy_path(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Happy path — service created, is_active=True, persisted."""
    service = await create_service(
        session,
        business_id=seed_data["business_id"],
        name="Стрижка мужская",
        duration_minutes=60,
        price=Decimal("1500"),
    )

    assert service.id is not None
    assert service.is_active is True
    assert service.duration_minutes == 60
    assert service.price == Decimal("1500")

    # Verify persisted
    await session.rollback()
    stmt = select(Service).where(Service.id == service.id)
    persisted = (await session.execute(stmt)).scalar_one()
    assert persisted.name == "Стрижка мужская"


@pytest.mark.asyncio
async def test_create_service_strips_whitespace(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Name with surrounding whitespace stripped (defense against fat-finger input)."""
    service = await create_service(
        session,
        business_id=seed_data["business_id"],
        name="  Стрижка  ",
        duration_minutes=60,
        price=Decimal("1500"),
    )
    assert service.name == "Стрижка"


@pytest.mark.asyncio
async def test_create_service_empty_name_raises(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Empty name → ValueError."""
    with pytest.raises(ValueError, match="empty"):
        await create_service(
            session,
            business_id=seed_data["business_id"],
            name="",
            duration_minutes=60,
            price=Decimal("1500"),
        )


@pytest.mark.asyncio
async def test_create_service_whitespace_only_name_raises(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Whitespace-only name → ValueError (after strip check)."""
    with pytest.raises(ValueError, match="empty"):
        await create_service(
            session,
            business_id=seed_data["business_id"],
            name="   ",
            duration_minutes=60,
            price=Decimal("1500"),
        )


@pytest.mark.asyncio
async def test_create_service_zero_duration_raises(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """duration_minutes=0 → ValueError.

    Also covered by DB CheckConstraint, but service validates first.
    """
    with pytest.raises(ValueError, match="duration"):
        await create_service(
            session,
            business_id=seed_data["business_id"],
            name="Стрижка",
            duration_minutes=0,
            price=Decimal("1500"),
        )


@pytest.mark.asyncio
async def test_create_service_negative_price_raises(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Negative price → ValueError."""
    with pytest.raises(ValueError, match="price"):
        await create_service(
            session,
            business_id=seed_data["business_id"],
            name="Стрижка",
            duration_minutes=60,
            price=Decimal("-100"),
        )


@pytest.mark.asyncio
async def test_create_service_name_too_long_raises(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Service.name longer than 255 chars → ValueError (defense for SQLite silent accept)."""
    long_name = "А" * 256
    with pytest.raises(ValueError, match="too long"):
        await create_service(
            session,
            business_id=seed_data["business_id"],
            name=long_name,
            duration_minutes=60,
            price=Decimal("1500"),
        )


@pytest.mark.asyncio
async def test_create_service_price_none_persists(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """price=None (Session 5.10) → service.price is None, no ValueError.

    Price стал nullable: мастер озвучивает цену в чате, не через FSM.
    Проверяем что create_service без price работает и сохраняет None.
    """
    service = await create_service(
        session,
        business_id=seed_data["business_id"],
        name="Стрижка",
        duration_minutes=60,
        # price не передаётся — дефолт None
    )
    assert service.price is None


# ============================================================
# Edge case: extreme timezone (Asia/Kamchatka UTC+12)
# ============================================================
@pytest.mark.asyncio
async def test_get_today_extreme_timezone_utc_plus_12(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Asia/Kamchatka (UTC+12): if now=2026-03-15 12:00 UTC, local=2026-03-16 00:00.

    today_local should be 2026-03-16 (next day in Moscow, but same calculation).
    """
    tz = "Asia/Kamchatka"
    # 12:00 UTC = 2026-03-16 00:00 Kamchatka
    now_utc = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
    today_local = "2026-03-16"

    slot = await _make_slot(
        session,
        master_id=seed_data["master_id"],
        slot_date_local=datetime.fromisoformat(f"{today_local}T00:00:00+12:00"),
        hour_local=11,
    )
    await _make_booking(
        session,
        slot_id=slot.id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        client_id=seed_data["client"].id,
        # 11:00 Kamchatka = 23:00 UTC previous day (2026-03-15)
        start_at_utc=_utc_naive(today_local, 11, tz),
        status="confirmed",
    )

    result = await get_today_bookings(session, seed_data["master_id"], tz, now_utc=now_utc)
    assert len(result) == 1
    assert result[0].start_at == _utc_naive(today_local, 11, tz)


# ============================================================
# get_client_bookings — cross-DB aware-aware SQL WHERE (T3, updated 2026-08-23 Урок 2.6)
# ============================================================
# Coverage (AUTONOMOUS_COVERAGE_PROMPT.md T3):
#   ref = now_utc or datetime.now(UTC) — aware UTC (was: datetime.now(tz=None) naive local).
#   Booking.start_at: naive UTC on SQLite, aware UTC on Postgres (TIMESTAMPTZ + asyncpg).
#   SQLAlchemy variant strips aware→naive on SQLite bind (verified empirically 2026-08-23),
#   so aware UTC vs stored naive UTC compares correctly. On Postgres: TIMESTAMPTZ vs
#   aware UTC — native comparison. Tests inject now_utc (aware UTC) for deterministic verify.
#   Regression test on the original bug (naive datetime.now(tz=None)) requires non-UTC
#   system TZ (freezegun returns frozen time, not system-local) — not reproducible in CI
#   without TZ env manipulation, so we test the post-fix contract directly.


@pytest.mark.asyncio
async def test_get_client_bookings_filters_past_naive(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """W2 fix contract: booking in past (naive UTC) excluded by default."""
    past_utc_naive = datetime(2025, 1, 1, 12, 0)  # naive UTC, in the past
    slot = await _make_slot(
        session,
        master_id=seed_data["master_id"],
        slot_date_local=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
        hour_local=12,
    )
    await _make_booking(
        session,
        slot_id=slot.id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        client_id=seed_data["client"].id,
        start_at_utc=past_utc_naive,
        status="confirmed",
    )

    now_utc = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    result = await get_client_bookings(session, seed_data["client"].id, now_utc=now_utc)
    assert len(result) == 0, "past booking (naive UTC) must be excluded by default"


@pytest.mark.asyncio
async def test_get_client_bookings_returns_upcoming_naive_utc(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """W2 fix contract: booking in future (naive UTC) returned as upcoming."""
    now_utc = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    future_utc = now_utc + timedelta(days=1)
    future_naive = future_utc.replace(hour=15, minute=0, second=0, microsecond=0, tzinfo=None)

    slot = await _make_slot(
        session,
        master_id=seed_data["master_id"],
        slot_date_local=future_utc.replace(hour=15, minute=0, second=0, microsecond=0),
        hour_local=15,
    )
    await _make_booking(
        session,
        slot_id=slot.id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        client_id=seed_data["client"].id,
        start_at_utc=future_naive,
        status="confirmed",
    )

    result = await get_client_bookings(session, seed_data["client"].id, now_utc=now_utc)
    assert len(result) == 1
    assert result[0].start_at == future_naive


@pytest.mark.asyncio
async def test_get_client_bookings_include_past_returns_all(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """include_past=True → no date filter, all confirmed/transferred returned
    (covers the `if not include_past` branch=False path).
    """
    past_naive = datetime(2025, 1, 1, 12, 0)  # naive UTC, in the past
    slot = await _make_slot(
        session,
        master_id=seed_data["master_id"],
        slot_date_local=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
        hour_local=12,
    )
    await _make_booking(
        session,
        slot_id=slot.id,
        business_id=seed_data["business_id"],
        master_id=seed_data["master_id"],
        client_id=seed_data["client"].id,
        start_at_utc=past_naive,
        status="confirmed",
    )

    result = await get_client_bookings(session, seed_data["client"].id, include_past=True)
    assert len(result) == 1
    assert result[0].start_at == past_naive


@pytest.mark.asyncio
async def test_get_client_bookings_default_now_utc_uses_utc_not_local(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """W2 fix contract: when now_utc not passed, service uses datetime.now(UTC)
    (NOT datetime.now(tz=None)) so ref is UTC even on non-UTC system.

    Seed booking exactly at now_utc, call without now_utc. After fix:
    ref = datetime.now(UTC).replace(tzinfo=None) ≈ now UTC.
    Filter `start_at > ref` strict → booking at exactly now excluded.

    freeze_time fixes 'now' on both sides — deterministic.
    """
    from freezegun import freeze_time

    with freeze_time("2026-08-21 12:00:00", tz_offset=0):
        now_utc = datetime.now(UTC)
        start_at_naive = now_utc.replace(tzinfo=None)
        slot = await _make_slot(
            session,
            master_id=seed_data["master_id"],
            slot_date_local=now_utc,
            hour_local=12,
        )
        await _make_booking(
            session,
            slot_id=slot.id,
            business_id=seed_data["business_id"],
            master_id=seed_data["master_id"],
            client_id=seed_data["client"].id,
            start_at_utc=start_at_naive,
            status="confirmed",
        )

        # No now_utc passed — service falls back to datetime.now(UTC) (post-fix)
        result = await get_client_bookings(session, seed_data["client"].id)

    # start_at == now (strict >) → excluded
    assert len(result) == 0, (
        "Booking at exactly now_utc must be excluded (strict >). "
        "If this fails, datetime.now(UTC) was not used — bug W2 regression."
    )
