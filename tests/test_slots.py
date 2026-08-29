"""Tests for bot.services.slots."""

from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Any
from uuid import uuid4

import pytest
from bot.models import Booking, Business, Client, Master, Slot, WorkDay
from bot.services.slots import (
    SlotAlreadyExistsError,
    add_slots,
    close_slot,
    get_available_slots,
    get_available_slots_30,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.selectable import Select

BUSINESS_TZ = "Europe/Moscow"


@pytest.mark.asyncio
async def test_add_slots_creates_new(session: AsyncSession, tomorrow_date: date) -> None:
    """Happy path — add 3 new slots."""
    from uuid import uuid4

    master_id = uuid4()
    slots = await add_slots(session, master_id, tomorrow_date, [11, 12, 13])

    assert len(slots) == 3
    assert {s.slot_hour for s in slots} == {11, 12, 13}
    assert all(s.status == "open" for s in slots)


@pytest.mark.asyncio
async def test_add_slots_skips_existing(session: AsyncSession, tomorrow_date: date) -> None:
    """Idempotent — calling twice with same hours skips existing."""
    from uuid import uuid4

    master_id = uuid4()
    await add_slots(session, master_id, tomorrow_date, [11, 12])
    slots = await add_slots(session, master_id, tomorrow_date, [11, 12, 13])

    assert len(slots) == 1
    assert slots[0].slot_hour == 13


@pytest.mark.asyncio
async def test_close_slot_happy(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """close_slot updates status from open to closed."""
    slot_id = seed_data["slot"].id
    updated = await close_slot(session, slot_id)
    assert updated is True

    # Verify in DB
    from sqlalchemy import select

    stmt = select(Slot.status).where(Slot.id == slot_id)
    res = await session.execute(stmt)
    assert res.scalar_one() == "closed"


@pytest.mark.asyncio
async def test_close_slot_idempotent_if_closed(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """close_slot on already-closed slot is idempotent (returns True)."""
    slot_id = seed_data["slot"].id
    await close_slot(session, slot_id)
    updated = await close_slot(session, slot_id)
    assert updated is True


@pytest.mark.asyncio
async def test_close_slot_refuses_if_booked(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """close_slot raises ValueError if slot already has a booking."""
    slot = seed_data["slot"]
    slot.status = "booked"
    await session.commit()

    with pytest.raises(ValueError, match="already has a booking"):
        await close_slot(session, slot.id)


@pytest.mark.asyncio
async def test_close_slot_not_found(session: AsyncSession) -> None:
    """close_slot returns False if slot doesn't exist."""
    from uuid import uuid4

    updated = await close_slot(session, uuid4())
    assert updated is False


@pytest.mark.asyncio
async def test_get_available_filters_open_only(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """get_available_slots returns only status='open' slots (including seed slot)."""
    master_id = seed_data["master_id"]
    slot_date = seed_data["slot_date"]

    # Add 3 more slots: 1 open, 1 closed, 1 booked (seed already has 1 open at 14:00)
    s1 = Slot(master_id=master_id, slot_date=slot_date, slot_hour=10, status="open")
    s2 = Slot(master_id=master_id, slot_date=slot_date, slot_hour=11, status="closed")
    s3 = Slot(master_id=master_id, slot_date=slot_date, slot_hour=12, status="booked")
    session.add_all([s1, s2, s3])
    await session.commit()

    available = await get_available_slots(session, master_id, slot_date)
    # 2 open: seed slot (14:00) + new s1 (10:00)
    assert len(available) == 2
    open_hours = {s.slot_hour for s in available}
    assert open_hours == {10, 14}


@pytest.mark.asyncio
async def test_add_slots_invalid_hour(session: AsyncSession, tomorrow_date: date) -> None:
    """add_slots raises ValueError for hours outside 0-23."""
    from uuid import uuid4

    with pytest.raises(ValueError, match="0-23"):
        await add_slots(session, uuid4(), tomorrow_date, [25])


@pytest.mark.asyncio
async def test_add_slots_empty_hours_raises_value_error(
    session: AsyncSession,
    tomorrow_date: date,
) -> None:
    """add_slots with empty hours list raises ValueError.

    Covers bot/services/slots.py:26 — `if not hours: raise ValueError(...)`.
    Pre-check happens before `min(hours)`/`max(hours)` (which would raise
    a different ValueError on empty sequence).
    """
    with pytest.raises(ValueError, match="at least one hour required"):
        await add_slots(session, uuid4(), tomorrow_date, [])


@pytest.mark.asyncio
async def test_add_slots_concurrent_race_raises_slot_exists(
    session_factory_concurrent: async_sessionmaker[AsyncSession],
) -> None:
    """Runtime race protection — deterministic replacement of asyncio.gather.

    Covers bot/services/slots.py:44-53 — IntegrityError catch + rollback +
    re-raise as SlotAlreadyExistsError. The composite unique constraint
    `ux_slots_master_date_hour` rejects the loser's INSERT at flush time.

    Flaky history: the previous asyncio.gather version flaked 1/10 —
    asyncio.gather doesn't guarantee both SELECTs fire before either
    INSERT. If A fully wins (SELECT+INSERT+COMMIT) before B's SELECT, B sees
    A's committed slot and takes the idempotent path (slots.py:37-38),
    returning [] — `len(success) == 2` would fail the test.

    Approach (deterministic, no asyncio.gather — pattern from
    test_booking.py:1122 `test_transfer_booking_concurrent_race_runtime`):
      1. Seed business + master through s_seed (committed).
      2. s_a: real add_slots(s_a, master_id, slot_date, [14]) — SELECT empty,
         INSERT, COMMIT. slot_a now in DB.
      3. s_b: patch first SELECT FROM Slot → return empty _FakeResult
         (simulates B captured snapshot before A's commit, then resumes).
         add_slots(s_b, master_id, slot_date, [14]) — SELECT returns empty
         (patched), INSERT slot_b same hour → UNIQUE constraint violation
         (slot_a committed) → IntegrityError catch (slots.py:46) → rollback
         → SlotAlreadyExistsError.
      4. Verify final DB state: exactly one slot (slot_a, hour=14).

    Faithfulness: tests the actual service's IntegrityError catch + rollback
    + re-raise behavior at runtime. If the catch were removed, slot_b's
    INSERT would propagate IntegrityError unchanged.

    Why file-based SQLite (not in-memory): default in-memory + QueuePool gives
    each connection its own DB (sessions can't share state). File-based allows
    multiple connections to same DB — A's commit is visible to B's INSERT.
    """
    # Step 1: Seed business + master (no slots yet) through s_seed
    async with session_factory_concurrent() as s_seed:
        biz = Business(
            name="Race Barbershop",
            telegram_owner_id=461355056,
            timezone="Europe/Moscow",
        )
        s_seed.add(biz)
        await s_seed.flush()
        master = Master(
            business_id=biz.id,
            name="Тестер",
            telegram_id=461355056,
            role="owner",
        )
        s_seed.add(master)
        await s_seed.commit()
        master_id = master.id

    slot_date = datetime.now(UTC).date()

    # Step 2: s_a wins fully — real add_slots, slot_a committed to DB
    async with session_factory_concurrent() as s_a:
        result_a = await add_slots(s_a, master_id, slot_date, [14])
        assert len(result_a) == 1
        assert result_a[0].slot_hour == 14

    # DB now has: slot(master_id, slot_date, hour=14, status='open')

    # Step 3: s_b — patch first SELECT FROM Slot to return empty (stale
    # snapshot, simulates B captured SELECT before A's commit, then resumes)
    async with session_factory_concurrent() as s_b:
        original_execute = s_b.execute
        patch_active = True

        async def patched_execute(statement, *args, **kwargs):
            nonlocal patch_active
            if patch_active and isinstance(statement, Select):
                # Detect SELECT FROM Slot — first existing-slots lookup in add_slots.
                # If entity detection fails (e.g. SQLAlchemy API change), raise
                # explicitly — silent bypass would let B see A's committed slot,
                # take the idempotent path, return [] — test would fail with
                # confusing "DID NOT RAISE" instead of clear "patched_execute
                # entity detection failed: ..." (pattern from test_booking.py:1258).
                try:
                    entities = [d.get("entity") for d in statement.column_descriptions]
                    if any(isinstance(e, type) and issubclass(e, Slot) for e in entities):
                        patch_active = False  # only first SELECT (existing slots lookup)

                        # _FakeResult implements only scalars().all() — the only
                        # method add_slots:33 calls on the SELECT result. If service
                        # refactors to .one()/.first()/.scalar_one(), this would raise
                        # AttributeError — add the method here. (W1 analogue.)
                        class _FakeResult:  # type: ignore[no-redef]
                            def scalars(self):
                                class _Scalars:
                                    def all(self):
                                        return []  # stale snapshot: no slots yet

                                return _Scalars()

                        return _FakeResult()
                except (KeyError, AttributeError, TypeError) as e:
                    raise AssertionError(
                        f"patched_execute entity detection failed: {e!r} — "
                        f"SQLAlchemy column_descriptions API may have changed"
                    ) from e
            return await original_execute(statement, *args, **kwargs)

        s_b.execute = patched_execute  # type: ignore[method-assign]  # test patch
        try:
            with pytest.raises(SlotAlreadyExistsError):
                await add_slots(s_b, master_id, slot_date, [14])
        finally:
            s_b.execute = original_execute  # type: ignore[method-assign]  # test patch

    # Step 4: Verify final DB state — exactly one slot (slot_a, hour=14).
    # Filter by (master_id, slot_date) without pinning slot_hour — catches
    # hypothetical future bugs where add_slots creates extra slots with
    # different hours (currently it doesn't, but defensive verify is cheap).
    async with session_factory_concurrent() as s_verify:
        from sqlalchemy import select

        stmt = select(Slot).where(
            Slot.master_id == master_id,
            Slot.slot_date == slot_date,
        )
        slots = (await s_verify.execute(stmt)).scalars().all()
        assert len(slots) == 1, f"expected 1 slot in DB, got {len(slots)}"
        assert slots[0].slot_hour == 14
        assert slots[0].status == "open"


# ============================================================
# Этап 5.6 — get_available_slots_30 occupancy tests
# ============================================================


def _local_to_utc(work_date: date, hour: int, minute: int = 0) -> datetime:
    """Convert (work_date, HH:MM) LOCAL Moscow → aware UTC datetime.

    Mirror of test_workday_service._local_to_utc — used by _insert_booking
    to set start_at/end_at for seed bookings.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(BUSINESS_TZ)
    return datetime.combine(work_date, dt_time(hour, minute), tzinfo=tz).astimezone(UTC)


async def _insert_booking(
    session: AsyncSession,
    seed_data: dict[str, Any],
    *,
    start_at: datetime,
    end_at: datetime,
    status: str = "confirmed",
) -> Booking:
    """Direct INSERT a Booking bypassing create_booking — for test setup only.

    Mirror of test_workday_service._direct_insert_booking:41. Booking.slot_id
    is nullable=False on the model — use seed_data["slot"] for convenience.
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


def _future_workdate(days: int = 14) -> date:
    """Work date N days ahead — far enough from 'today' to avoid past-slot filter."""
    return (datetime.now(UTC) + timedelta(days=days)).date()


async def _make_workday(
    session: AsyncSession,
    seed_data: dict[str, Any],
    work_date: date,
    start: int,
    end: int,
    capacity: int = 1,
) -> WorkDay:
    """Create WorkDay [start:00, end:00] LOCAL Moscow with given capacity."""
    wd = WorkDay(
        master_id=seed_data["master_id"],
        work_date=work_date,
        start_time=dt_time(start, 0),
        end_time=dt_time(end, 0),
        max_concurrent_clients=capacity,
        is_active=True,
    )
    session.add(wd)
    await session.commit()
    return wd


@pytest.mark.asyncio
async def test_get_available_slots_30_empty_no_bookings(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Happy path: WorkDay [10:00, 12:00] cap=1, no bookings → 4 slots available."""
    wd = await _make_workday(session, seed_data, _future_workdate(), 10, 12, capacity=1)
    available = await get_available_slots_30(session, wd, BUSINESS_TZ)
    # [10:00, 10:30, 11:00, 11:30] = 4 slots (last slot start = 11:30, end 12:00)
    assert len(available) == 4
    assert [s.label for s in available] == ["10:00", "10:30", "11:00", "11:30"]


@pytest.mark.asyncio
async def test_get_available_slots_30_capacity_1_one_booking_covers_slot(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """cap=1, booking [11:00, 11:30] → slot [11:00, 11:30] unavailable, others free."""
    work_date = _future_workdate()
    wd = await _make_workday(session, seed_data, work_date, 10, 13, capacity=1)
    await _insert_booking(
        session,
        seed_data,
        start_at=_local_to_utc(work_date, 11, 0),
        end_at=_local_to_utc(work_date, 11, 30),
    )
    available = await get_available_slots_30(session, wd, BUSINESS_TZ)
    labels = [s.label for s in available]
    # WorkDay [10:00, 13:00] = 6 slots: 10:00, 10:30, 11:00, 11:30, 12:00, 12:30
    # booking [11:00, 11:30] covers slot [11:00, 11:30] → 5 available
    assert "11:00" not in labels
    assert len(available) == 5
    assert labels == ["10:00", "10:30", "11:30", "12:00", "12:30"]


@pytest.mark.asyncio
async def test_get_available_slots_30_capacity_1_booking_crosses_3_cells(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """cap=1, booking [11:00, 12:30] (90 min) → 3 grid cells unavailable."""
    work_date = _future_workdate()
    wd = await _make_workday(session, seed_data, work_date, 10, 13, capacity=1)
    await _insert_booking(
        session,
        seed_data,
        start_at=_local_to_utc(work_date, 11, 0),
        end_at=_local_to_utc(work_date, 12, 30),
    )
    available = await get_available_slots_30(session, wd, BUSINESS_TZ)
    labels = [s.label for s in available]
    # WorkDay [10:00, 13:00] = 6 slots. Booking covers [11:00-11:30, 11:30-12:00, 12:00-12:30]
    assert "11:00" not in labels
    assert "11:30" not in labels
    assert "12:00" not in labels
    # 3 unavailable, 3 available
    assert len(available) == 3
    assert labels == ["10:00", "10:30", "12:30"]


@pytest.mark.asyncio
async def test_get_available_slots_30_capacity_2_one_booking(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """cap=2, one booking overlaps slot → slot still available (count=1 < 2)."""
    work_date = _future_workdate()
    wd = await _make_workday(session, seed_data, work_date, 10, 12, capacity=2)
    await _insert_booking(
        session,
        seed_data,
        start_at=_local_to_utc(work_date, 11, 0),
        end_at=_local_to_utc(work_date, 11, 30),
    )
    available = await get_available_slots_30(session, wd, BUSINESS_TZ)
    # All 4 slots available — capacity=2, overlap_count=1 on [11:00, 11:30]
    assert len(available) == 4


@pytest.mark.asyncio
async def test_get_available_slots_30_capacity_2_two_bookings_overlap_slot(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """cap=2, two bookings overlap slot [11:00, 11:30] → slot unavailable (count=2)."""
    work_date = _future_workdate()
    wd = await _make_workday(session, seed_data, work_date, 10, 13, capacity=2)
    # Two bookings both [11:00, 11:30] — need different slot_id (UNIQUE(slot_id))
    # and different client_id.
    client2 = Client(telegram_id=999999999, name="Second")
    session.add(client2)
    await session.flush()
    slot2 = Slot(
        master_id=seed_data["master_id"],
        slot_date=work_date,
        slot_hour=11,
        status="open",
    )
    session.add(slot2)
    await session.flush()
    bookings_data = [
        (seed_data["slot"].id, seed_data["client"].id),
        (slot2.id, client2.id),
    ]
    for slot_id, client_id in bookings_data:
        b = Booking(
            slot_id=slot_id,
            business_id=seed_data["business_id"],
            master_id=seed_data["master_id"],
            client_id=client_id,
            service_id=None,
            service_title_snapshot="test",
            client_name_snapshot="test",
            start_at=_local_to_utc(work_date, 11, 0),
            end_at=_local_to_utc(work_date, 11, 30),
            status="confirmed",
        )
        session.add(b)
    await session.commit()
    available = await get_available_slots_30(session, wd, BUSINESS_TZ)
    labels = [s.label for s in available]
    assert "11:00" not in labels  # count=2 >= capacity=2
    assert len(available) == 5  # 6 grid cells - 1 occupied


@pytest.mark.asyncio
async def test_get_available_slots_30_cancelled_excluded(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Cancelled booking does not occupy the slot — slot still available."""
    work_date = _future_workdate()
    wd = await _make_workday(session, seed_data, work_date, 10, 12, capacity=1)
    await _insert_booking(
        session,
        seed_data,
        start_at=_local_to_utc(work_date, 11, 0),
        end_at=_local_to_utc(work_date, 11, 30),
        status="cancelled",
    )
    available = await get_available_slots_30(session, wd, BUSINESS_TZ)
    # Cancelled booking excluded → all 4 slots available
    assert len(available) == 4
    assert "11:00" in [s.label for s in available]


@pytest.mark.asyncio
async def test_get_available_slots_30_transferred_included(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Transferred booking DOES occupy the slot (mirror _check_multi_client_capacity:228).

    Status filter IN ('confirmed', 'transferred') — 'transferred' must block
    the slot the same way as 'confirmed'. Without this test, future regression
    dropping 'transferred' from the filter (slots.py:179) would not be caught
    (all other capacity tests use status='confirmed' default).
    """
    work_date = _future_workdate()
    wd = await _make_workday(session, seed_data, work_date, 10, 13, capacity=1)
    await _insert_booking(
        session,
        seed_data,
        start_at=_local_to_utc(work_date, 11, 0),
        end_at=_local_to_utc(work_date, 11, 30),
        status="transferred",
    )
    available = await get_available_slots_30(session, wd, BUSINESS_TZ)
    labels = [s.label for s in available]
    assert "11:00" not in labels  # transferred blocks slot [11:00, 11:30]
    assert len(available) == 5  # 6 grid cells - 1 occupied


@pytest.mark.asyncio
async def test_get_available_slots_30_touch_edge_no_overlap(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Touch edge: booking [11:00, 11:30] does NOT block slot [11:30, 12:00] (half-open)."""
    work_date = _future_workdate()
    wd = await _make_workday(session, seed_data, work_date, 10, 13, capacity=1)
    await _insert_booking(
        session,
        seed_data,
        start_at=_local_to_utc(work_date, 11, 0),
        end_at=_local_to_utc(work_date, 11, 30),
    )
    available = await get_available_slots_30(session, wd, BUSINESS_TZ)
    labels = [s.label for s in available]
    # Booking covers [11:00, 11:30]. Touch slot [11:30, 12:00] — half-open, no overlap.
    assert "11:00" not in labels  # booking covers this slot
    assert "11:30" in labels  # touch — NOT overlap
    assert "12:00" in labels


@pytest.mark.asyncio
async def test_get_available_slots_30_past_slots_filtered(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Past slots filtered out via now_utc injection (UX: hide past windows).

    Strategy: inject now_utc at end-of-day for the work_date — all 4 grid slots
    [10:00, 10:30, 11:00, 11:30] are strictly before 23:00 → all filtered as
    'past' → 0 available. Deterministic regardless of test execution time.
    """
    work_date_future = _future_workdate(2)
    wd = await _make_workday(session, seed_data, work_date_future, 10, 12, capacity=1)
    # now_utc = 23:00 LOCAL on the work_date → all [10:00..11:30] slots are past
    far_future_now = _local_to_utc(work_date_future, 23, 0)
    available = await get_available_slots_30(session, wd, BUSINESS_TZ, now_utc=far_future_now)
    # All 4 slots are "past" relative to far_future_now → 0 available
    assert available == []


@pytest.mark.asyncio
async def test_get_available_slots_30_partial_past_filter(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Partial past filter: now_utc mid-workday → past slots filtered, future survive.

    now_utc = 10:45 LOCAL → slots [10:00, 10:30] are past (start_at_utc <= ref),
    slots [11:00, 11:30] are future (start_at_utc > ref). Closes S1 from
    code-review: the all-past test only exercised the early-return path
    (slots.py:168 `if not candidates`), this test verifies past-filter survives
    alongside future slots in the same call.
    """
    work_date = _future_workdate(3)
    wd = await _make_workday(session, seed_data, work_date, 10, 12, capacity=1)
    # now_utc = 10:45 LOCAL on work_date → 07:45 UTC (Moscow UTC+3)
    mid_now = _local_to_utc(work_date, 10, 45)
    available = await get_available_slots_30(session, wd, BUSINESS_TZ, now_utc=mid_now)
    labels = [s.label for s in available]
    # Past: [10:00, 10:30] filtered (start <= 10:45). Future: [11:00, 11:30] kept.
    assert labels == ["11:00", "11:30"]


@pytest.mark.asyncio
async def test_get_available_slots_30_closed_workday_not_filtered(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Function does NOT filter is_active=False — handler 5.8 decides (separation of concerns)."""
    work_date = _future_workdate()
    wd = WorkDay(
        master_id=seed_data["master_id"],
        work_date=work_date,
        start_time=dt_time(10, 0),
        end_time=dt_time(12, 0),
        max_concurrent_clients=1,
        is_active=False,  # closed
    )
    session.add(wd)
    await session.commit()
    available = await get_available_slots_30(session, wd, BUSINESS_TZ)
    # Function still returns slots — handler 5.8 is responsible for is_active filter
    assert len(available) == 4


@pytest.mark.asyncio
async def test_get_available_slots_30_window_less_than_30min(
    session: AsyncSession,
    seed_data: dict[str, Any],
) -> None:
    """Defensive: WorkDay window < 30 min → empty result (no slot fits)."""
    work_date = _future_workdate()
    # 10:00 to 10:15 — 15 min window, < 30 min slot size
    wd = WorkDay(
        master_id=seed_data["master_id"],
        work_date=work_date,
        start_time=dt_time(10, 0),
        end_time=dt_time(10, 15),
        max_concurrent_clients=1,
        is_active=True,
    )
    session.add(wd)
    await session.commit()
    available = await get_available_slots_30(session, wd, BUSINESS_TZ)
    assert available == []
