"""Tests for bot.services.slots."""

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest
from bot.models import Business, Master, Slot
from bot.services.slots import (
    SlotAlreadyExistsError,
    add_slots,
    close_slot,
    get_available_slots,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.selectable import Select


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
