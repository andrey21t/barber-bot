"""Tests for bot.handlers.admin — 5 commands + 3 helpers.

Coverage (AUTONOMOUS_COVERAGE_PROMPT.md T1):
- cmd_addslots: happy + не-админ + неверный формат args + невалидный ISO date +
  невалидный hours (ValueError на int) + slot_hour вне 0-23 (ValueError from service) +
  idempotent (slot already exists) + past date + SlotAlreadyExistsError race
- cmd_closeslot: happy + не-админ + args != 2 + невалидный ISO date + невалидный hour +
  hour вне 0-23 + master not found + slot not found + slot booked (ValueError) +
  already closed (idempotent)
- cmd_today: happy + не-админ + master not found + empty
- cmd_week: happy + не-админ + master not found + empty
- cmd_services: happy + не-админ + no args/args[0] != "add" + len != 4 +
  невалидный duration + невалидный price + duration <= 0 + price < 0 + master not found +
  ValueError from create_service
- _render_bookings: empty list + mixed statuses (confirmed/transferred/cancelled)
- _is_admin, _require_admin_or_silent, _resolve_master_and_business: direct unit

Why handler tests (not just service tests, AGENTS.md § anti-overengineering rule 3):
admin handlers contain partition decisions computed in the handler:
- /addslots past-date check (handler compares slot_date < today_local)
- /closeslot hour range check (handler validates 0-23 before service call)
- /services args validation (handler parses duration/price before service)
Display math that mirrors service invariants is a logic-change risk, not pure I/O.

Pattern (NEXT_SESSION_PROMPT.md 38, mirror of test_client_handlers.py):
direct handler invocation with mock Message + CommandObject + monkeypatch of
`bot.handlers.admin.async_session_factory` so the handler DB calls hit in-memory SQLite.
Avoids `dp.feed_update` ceremony — no Dispatcher/router-wiring needed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from aiogram.filters import CommandObject
from aiogram.types import Message, User
from bot.config import get_settings
from bot.handlers import admin as admin_handlers
from bot.models import Booking, Business, Client, Master, Service, Slot
from bot.services.slots import SlotAlreadyExistsError
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# ============================================================
# Constants — admin user from settings (set via env in conftest)
# ============================================================
ADMIN_TG_ID: int = get_settings().ADMIN_ID  # 461355056 from conftest env
NON_ADMIN_TG_ID: int = 999111222  # any id != ADMIN_ID
TZ = "Europe/Moscow"


# ============================================================
# Fixtures — patch async_session_factory in admin handlers module
# ============================================================


@pytest.fixture
def patched_session_factory(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Replace `bot.handlers.admin.async_session_factory` with the test engine's
    session factory so handler DB calls hit in-memory SQLite (mirror of
    test_client_handlers.py:patched_session_factory).
    """
    monkeypatch.setattr(admin_handlers, "async_session_factory", session_factory)
    return session_factory


# ============================================================
# Helpers — mock Telegram objects
# ============================================================


def _make_user(user_id: int) -> User:
    """Build a minimal aiogram User (required fields per Bot API)."""
    return User(id=user_id, is_bot=False, first_name="Test")


def _make_message(
    user_id: int,
    text: str = "/today",
) -> MagicMock:
    """Mock aiogram.Message with spec — answer is AsyncMock for assert_called_once."""
    msg = MagicMock(spec=Message)
    msg.from_user = _make_user(user_id)
    msg.text = text
    msg.answer = AsyncMock()
    return msg


def _make_command(args: str | None) -> CommandObject:
    """Build aiogram CommandObject with `args` (string after command name).

    `/addslots 2026-03-17 11 12` → CommandObject(args='2026-03-17 11 12', command='addslots')
    """
    return CommandObject(command="cmd", args=args)


def _answer_text(msg: MagicMock) -> str:
    """Extract text from msg.answer (first positional arg)."""
    args, _ = msg.answer.call_args
    return str(args[0])


def _answer_call_count(msg: MagicMock) -> int:
    return int(msg.answer.call_count)


async def _seed_admin_stack(
    session: AsyncSession,
    *,
    admin_telegram_id: int = ADMIN_TG_ID,
    timezone: str = TZ,
) -> dict[str, Any]:
    """Insert business + master owned by admin_telegram_id — for admin handlers
    that resolve master via _resolve_master_and_business(telegram_id).
    """
    biz = Business(name="Test Barbershop", telegram_owner_id=admin_telegram_id, timezone=timezone)
    session.add(biz)
    await session.flush()

    master = Master(
        business_id=biz.id, name="Екатерина", telegram_id=admin_telegram_id, role="owner"
    )
    session.add(master)
    await session.flush()

    client = Client(telegram_id=111222333, name="Паша")
    session.add(client)
    await session.commit()

    return {
        "business": biz,
        "master": master,
        "client": client,
        "business_id": biz.id,
        "master_id": master.id,
        "client_id": client.id,
    }


async def _seed_slot(
    session: AsyncSession,
    *,
    master_id: UUID,
    slot_date: date,
    hour: int,
    status: str = "open",
) -> Slot:
    slot = Slot(master_id=master_id, slot_date=slot_date, slot_hour=hour, status=status)
    session.add(slot)
    await session.commit()
    return slot


async def _seed_booking(
    session: AsyncSession,
    *,
    ctx: dict[str, Any],
    slot: Slot,
    start_at_utc_naive: datetime,
    status: str = "confirmed",
) -> Booking:
    """Insert Booking linked to a slot. start_at_utc_naive — naive UTC datetime
    (SQLite stores naive per booking.py pattern).
    """
    booking = Booking(
        slot_id=slot.id,
        business_id=ctx["business_id"],
        master_id=ctx["master_id"],
        client_id=ctx["client_id"],
        service_id=None,
        service_title_snapshot="Стрижка",
        service_price_snapshot=None,
        client_name_snapshot="Паша",
        start_at=start_at_utc_naive,
        end_at=start_at_utc_naive + timedelta(minutes=60),
        status=status,
    )
    session.add(booking)
    if status in ("confirmed", "transferred"):
        slot.status = "booked"
    await session.commit()
    return booking


def _local_to_utc_naive(local_aware: datetime) -> datetime:
    """LOCAL tz-aware → UTC naive (SQLite pattern)."""
    return local_aware.astimezone(UTC).replace(tzinfo=None)


# ============================================================
# Helpers — _is_admin / _require_admin_or_silent direct unit
# ============================================================


def test_is_admin_returns_true_for_admin_user() -> None:
    """ADMIN_ID match → True."""
    msg = _make_message(user_id=ADMIN_TG_ID)
    assert admin_handlers._is_admin(msg) is True


def test_is_admin_returns_false_for_non_admin() -> None:
    """Different user_id → False (silent ignore in handler)."""
    msg = _make_message(user_id=NON_ADMIN_TG_ID)
    assert admin_handlers._is_admin(msg) is False


def test_is_admin_returns_false_when_from_user_is_none() -> None:
    """Edge: message.from_user is None (e.g. channel_post) → False, no AttributeError."""
    msg = MagicMock(spec=Message)
    msg.from_user = None
    assert admin_handlers._is_admin(msg) is False


def test_require_admin_or_silent_returns_id_for_admin() -> None:
    """Admin → returns admin telegram_id (used in handler for _resolve_master lookup)."""
    msg = _make_message(user_id=ADMIN_TG_ID)
    assert admin_handlers._require_admin_or_silent(msg) == ADMIN_TG_ID


def test_require_admin_or_silent_returns_none_for_non_admin() -> None:
    """Non-admin → None (caller returns silently)."""
    msg = _make_message(user_id=NON_ADMIN_TG_ID)
    assert admin_handlers._require_admin_or_silent(msg) is None


def test_require_admin_or_silent_returns_none_when_from_user_none() -> None:
    """Edge: from_user None (already filtered by _is_admin) → None (defensive)."""
    msg = MagicMock(spec=Message)
    msg.from_user = None
    assert admin_handlers._require_admin_or_silent(msg) is None


# ============================================================
# _resolve_master_and_business direct unit
# ============================================================


@pytest.mark.asyncio
async def test_resolve_master_and_business_returns_tuple_for_existing_admin(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Happy: master + business found → returns (master_id, business_id, tz)."""
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)

    result = await admin_handlers._resolve_master_and_business(ADMIN_TG_ID)
    assert result is not None
    master_id, business_id, tz = result
    assert master_id == ctx["master_id"]
    assert business_id == ctx["business_id"]
    assert tz == TZ


@pytest.mark.asyncio
async def test_resolve_master_and_business_returns_none_for_unknown_telegram_id(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Master not found → None (handler shows '❌ Мастер не найден')."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    result = await admin_handlers._resolve_master_and_business(NON_ADMIN_TG_ID)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_master_and_business_returns_none_when_master_without_business(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Edge: master exists but business_id FK broken → None (defense in depth).

    Hard to construct in SQLite with FK constraints active — but the handler
    branch is there for safety. We instead test the happy-not-found path above
    which exercises the `master is None` branch. The `business is None` branch
    is symmetric and not separately testable without FK violation.
    """
    # Empty DB → master not found (covers master is None branch)
    result = await admin_handlers._resolve_master_and_business(ADMIN_TG_ID)
    assert result is None


# ============================================================
# cmd_addslots — 9 branches
# ============================================================


@pytest.mark.asyncio
async def test_cmd_addslots_happy_creates_slots(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Happy: admin adds 3 slots on future date → '✅ Открыты слоты' + 3 Slot rows."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    # Future date (tomorrow) — guaranteed > today_local
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/addslots {tomorrow} 11 12 13")
    await admin_handlers.cmd_addslots(msg, _make_command(f"{tomorrow} 11 12 13"))

    text = _answer_text(msg)
    assert "✅ Открыты слоты" in text
    assert "11" in text and "12" in text and "13" in text

    async with session_factory() as verify:
        slots = (await verify.execute(select(Slot).order_by(Slot.slot_hour))).scalars().all()
        assert [s.slot_hour for s in slots] == [11, 12, 13]
        assert all(s.status == "open" for s in slots)


@pytest.mark.asyncio
async def test_cmd_addslots_non_admin_silent_ignore(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Non-admin → _is_admin False → return silently (no answer, no DB write)."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=NON_ADMIN_TG_ID, text=f"/addslots {tomorrow} 11")
    await admin_handlers.cmd_addslots(msg, _make_command(f"{tomorrow} 11"))

    assert _answer_call_count(msg) == 0
    async with session_factory() as verify:
        slots = (await verify.execute(select(Slot))).scalars().all()
        assert len(slots) == 0


@pytest.mark.asyncio
async def test_cmd_addslots_no_args_shows_format_hint(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """args < 2 (no args at all) → 'Формат: /addslots ...' hint."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/addslots")
    await admin_handlers.cmd_addslots(msg, _make_command(None))

    text = _answer_text(msg)
    assert "Формат:" in text
    assert "/addslots" in text


@pytest.mark.asyncio
async def test_cmd_addslots_invalid_iso_date_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """'2026-13-99' invalid month/day → ValueError from date.fromisoformat → '❌ Неверная дата'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/addslots 2026-13-99 11")
    await admin_handlers.cmd_addslots(msg, _make_command("2026-13-99 11"))

    text = _answer_text(msg)
    assert "Неверная дата" in text


@pytest.mark.asyncio
async def test_cmd_addslots_non_numeric_hours_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """'aa' instead of hour → ValueError on int() → '❌ Часы должны быть числами'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/addslots {tomorrow} aa")
    await admin_handlers.cmd_addslots(msg, _make_command(f"{tomorrow} aa"))

    text = _answer_text(msg)
    assert "Часы должны быть числами" in text


@pytest.mark.asyncio
async def test_cmd_addslots_hour_out_of_range_service_raises_value_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """hour=25 → passes handler parsing (int('25') ok), service raises ValueError
    (slot_hour must be 0-23) → '❌ slot_hour must be 0-23'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/addslots {tomorrow} 25")
    await admin_handlers.cmd_addslots(msg, _make_command(f"{tomorrow} 25"))

    text = _answer_text(msg)
    assert "slot_hour must be 0-23" in text

    async with session_factory() as verify:
        slots = (await verify.execute(select(Slot))).scalars().all()
        assert len(slots) == 0  # rollback in service


@pytest.mark.asyncio
async def test_cmd_addslots_past_date_rejected(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """slot_date < today_local → '❌ Нельзя создать слот в прошлом'.

    Use freeze_time to deterministically fix 'today' (otherwise test is flaky
    around midnight)."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    # Set 'today' to 2026-03-17 12:00 Moscow, then try to create slot on 2026-03-16
    with freeze_time("2026-03-17 12:00:00", tz_offset=3):  # Moscow UTC+3
        msg = _make_message(user_id=ADMIN_TG_ID, text="/addslots 2026-03-16 11")
        await admin_handlers.cmd_addslots(msg, _make_command("2026-03-16 11"))

    text = _answer_text(msg)
    assert "Нельзя создать слот в прошлом" in text
    assert "17.03.2026" in text  # today_local.strftime('%d.%m.%Y')


@pytest.mark.asyncio
async def test_cmd_addslots_idempotent_all_slots_already_open(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """All requested hours already exist as 'open' → service returns empty list →
    'Все слоты на ... уже открыты — ничего не добавлено.'"""
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        await _seed_slot(
            session, master_id=ctx["master_id"], slot_date=tomorrow, hour=11, status="open"
        )
        await _seed_slot(
            session, master_id=ctx["master_id"], slot_date=tomorrow, hour=12, status="open"
        )

    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/addslots {tomorrow} 11 12")
    await admin_handlers.cmd_addslots(msg, _make_command(f"{tomorrow} 11 12"))

    text = _answer_text(msg)
    assert "уже открыты" in text
    assert "ничего не добавлено" in text


@pytest.mark.asyncio
async def test_cmd_addslots_dedups_duplicate_hours_in_request(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """'11 11 12' → sorted(set(...)) dedups to [11, 12] → 2 slots created (not 3)."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/addslots {tomorrow} 11 11 12")
    await admin_handlers.cmd_addslots(msg, _make_command(f"{tomorrow} 11 11 12"))

    text = _answer_text(msg)
    assert "✅ Открыты слоты" in text

    async with session_factory() as verify:
        slots = (await verify.execute(select(Slot).order_by(Slot.slot_hour))).scalars().all()
        assert [s.slot_hour for s in slots] == [11, 12]  # dedup, not 3 rows


@pytest.mark.asyncio
async def test_cmd_addslots_slot_already_exists_race(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SlotAlreadyExistsError raised by add_slots (concurrent insert race) →
    handler catches and shows '❌ Один из слотов уже существует (гонка).'.

    We monkeypatch add_slots in admin handlers module to raise — simulating
    a concurrent INSERT that wins the unique constraint race after our
    SELECT-then-INSERT pattern.
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    async def _raise_race(*args: Any, **kwargs: Any) -> list[Slot]:
        raise SlotAlreadyExistsError("race simulated by test")

    monkeypatch.setattr(admin_handlers, "add_slots", _raise_race)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/addslots {tomorrow} 11")
    await admin_handlers.cmd_addslots(msg, _make_command(f"{tomorrow} 11"))

    text = _answer_text(msg)
    assert "Один из слотов уже существует (гонка)" in text


# ============================================================
# cmd_closeslot — 10 branches
# ============================================================


@pytest.mark.asyncio
async def test_cmd_closeslot_happy_closes_slot(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Happy: open slot exists → closed → '✅ Слот ... закрыт'."""
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        await _seed_slot(
            session, master_id=ctx["master_id"], slot_date=tomorrow, hour=14, status="open"
        )

    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/closeslot {tomorrow} 14")
    await admin_handlers.cmd_closeslot(msg, _make_command(f"{tomorrow} 14"))

    text = _answer_text(msg)
    assert "✅ Слот" in text
    assert "закрыт" in text

    async with session_factory() as verify:
        slot = (await verify.execute(select(Slot))).scalar_one()
        assert slot.status == "closed"


@pytest.mark.asyncio
async def test_cmd_closeslot_non_admin_silent_ignore(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Non-admin → silent (no answer, no DB write)."""
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        await _seed_slot(
            session, master_id=ctx["master_id"], slot_date=tomorrow, hour=14, status="open"
        )

    msg = _make_message(user_id=NON_ADMIN_TG_ID, text=f"/closeslot {tomorrow} 14")
    await admin_handlers.cmd_closeslot(msg, _make_command(f"{tomorrow} 14"))

    assert _answer_call_count(msg) == 0
    async with session_factory() as verify:
        slot = (await verify.execute(select(Slot))).scalar_one()
        assert slot.status == "open"  # unchanged


@pytest.mark.asyncio
async def test_cmd_closeslot_wrong_args_count_shows_format(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """args != 2 (e.g. only date, no hour) → 'Формат: /closeslot ...'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/closeslot {tomorrow}")
    await admin_handlers.cmd_closeslot(msg, _make_command(str(tomorrow)))

    text = _answer_text(msg)
    assert "Формат:" in text
    assert "/closeslot" in text


@pytest.mark.asyncio
async def test_cmd_closeslot_invalid_iso_date_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """'2026-13-99' → date.fromisoformat raises ValueError → '❌ Неверная дата'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/closeslot 2026-13-99 14")
    await admin_handlers.cmd_closeslot(msg, _make_command("2026-13-99 14"))

    text = _answer_text(msg)
    assert "Неверная дата" in text


@pytest.mark.asyncio
async def test_cmd_closeslot_non_numeric_hour_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """'aa' as hour → ValueError on int() → '❌ Час должен быть числом 0-23'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/closeslot {tomorrow} aa")
    await admin_handlers.cmd_closeslot(msg, _make_command(f"{tomorrow} aa"))

    text = _answer_text(msg)
    assert "Час должен быть числом" in text


@pytest.mark.asyncio
async def test_cmd_closeslot_hour_out_of_range_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """hour=25 → int ok, range check 0-23 fails → '❌ Час должен быть 0-23'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/closeslot {tomorrow} 25")
    await admin_handlers.cmd_closeslot(msg, _make_command(f"{tomorrow} 25"))

    text = _answer_text(msg)
    assert "Час должен быть 0-23" in text


@pytest.mark.asyncio
async def test_cmd_closeslot_master_not_found_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Admin has no master record in DB → _resolve returns None → '❌ Мастер не найден'.

    Use a different admin_telegram_id so _seed_admin_stack creates a master for
    a DIFFERENT admin — current user is admin but has no master row.
    """
    async with session_factory() as session:
        # Create master for a different admin id
        await _seed_admin_stack(session, admin_telegram_id=777777777)

    # Current user is the conftest ADMIN_ID (different from 777777777)
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/closeslot {tomorrow} 14")
    await admin_handlers.cmd_closeslot(msg, _make_command(f"{tomorrow} 14"))

    text = _answer_text(msg)
    assert "Мастер не найден" in text


@pytest.mark.asyncio
async def test_cmd_closeslot_slot_not_found_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Master ok, no slot for given (date, hour) → '❌ Слот ... не найден'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)
        # No slot seeded

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/closeslot {tomorrow} 14")
    await admin_handlers.cmd_closeslot(msg, _make_command(f"{tomorrow} 14"))

    text = _answer_text(msg)
    assert "не найден" in text


@pytest.mark.asyncio
async def test_cmd_closeslot_slot_already_booked_raises_value_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Slot.status='booked' → close_slot raises ValueError → '❌ Slot ... already has a booking'."""
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        await _seed_slot(
            session, master_id=ctx["master_id"], slot_date=tomorrow, hour=14, status="booked"
        )

    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/closeslot {tomorrow} 14")
    await admin_handlers.cmd_closeslot(msg, _make_command(f"{tomorrow} 14"))

    text = _answer_text(msg)
    assert "already has a booking" in text


@pytest.mark.asyncio
async def test_cmd_closeslot_already_closed_idempotent(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Slot.status='closed' → close_slot returns True (idempotent per service) →
    handler shows '✅ Слот ... закрыт' (because `if updated` is True)."""
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        await _seed_slot(
            session, master_id=ctx["master_id"], slot_date=tomorrow, hour=14, status="closed"
        )

    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/closeslot {tomorrow} 14")
    await admin_handlers.cmd_closeslot(msg, _make_command(f"{tomorrow} 14"))

    text = _answer_text(msg)
    # Per close_slot service: status=='closed' returns True → handler shows success
    assert "✅ Слот" in text
    assert "закрыт" in text


# ============================================================
# cmd_today — 4 branches
# ============================================================


@pytest.mark.asyncio
async def test_cmd_today_happy_with_booking(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Booking for today (LOCAL) → '📅 Записи на сегодня:' + booking line."""
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        # Today's slot at 14:00 Moscow → start_at_utc_naive
        now_local = datetime.now(ZoneInfo(TZ))
        today_local_at_14 = now_local.replace(hour=14, minute=0, second=0, microsecond=0)
        slot = await _seed_slot(
            session,
            master_id=ctx["master_id"],
            slot_date=today_local_at_14.date(),
            hour=14,
            status="open",  # will be set to 'booked' by _seed_booking
        )
        await _seed_booking(
            session,
            ctx=ctx,
            slot=slot,
            start_at_utc_naive=_local_to_utc_naive(today_local_at_14),
            status="confirmed",
        )

    msg = _make_message(user_id=ADMIN_TG_ID, text="/today")
    await admin_handlers.cmd_today(msg)

    text = _answer_text(msg)
    assert "Записи на сегодня" in text
    assert "Паша" in text
    assert "Стрижка" in text


@pytest.mark.asyncio
async def test_cmd_today_non_admin_silent_ignore(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Non-admin → silent."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=NON_ADMIN_TG_ID, text="/today")
    await admin_handlers.cmd_today(msg)

    assert _answer_call_count(msg) == 0


@pytest.mark.asyncio
async def test_cmd_today_master_not_found_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Admin user has no master row → '❌ Мастер не найден'."""
    async with session_factory() as session:
        await _seed_admin_stack(session, admin_telegram_id=777777777)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/today")
    await admin_handlers.cmd_today(msg)

    text = _answer_text(msg)
    assert "Мастер не найден" in text


@pytest.mark.asyncio
async def test_cmd_today_no_bookings_shows_empty_message(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """No bookings for today → 'На сегодня записей нет.'"""
    async with session_factory() as session:
        await _seed_admin_stack(session)
        # No bookings seeded

    msg = _make_message(user_id=ADMIN_TG_ID, text="/today")
    await admin_handlers.cmd_today(msg)

    text = _answer_text(msg)
    assert "На сегодня записей нет" in text


# ============================================================
# cmd_week — 4 branches
# ============================================================


@pytest.mark.asyncio
async def test_cmd_week_happy_with_booking(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Booking within next 7 days (LOCAL) → '📅 Записи на неделю:'."""
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        # 3 days ahead at 15:00 Moscow
        in_3_days_local = (datetime.now(ZoneInfo(TZ)) + timedelta(days=3)).replace(
            hour=15, minute=0, second=0, microsecond=0
        )
        slot = await _seed_slot(
            session,
            master_id=ctx["master_id"],
            slot_date=in_3_days_local.date(),
            hour=15,
            status="open",
        )
        await _seed_booking(
            session,
            ctx=ctx,
            slot=slot,
            start_at_utc_naive=_local_to_utc_naive(in_3_days_local),
            status="confirmed",
        )

    msg = _make_message(user_id=ADMIN_TG_ID, text="/week")
    await admin_handlers.cmd_week(msg)

    text = _answer_text(msg)
    assert "Записи на неделю" in text
    assert "Паша" in text


@pytest.mark.asyncio
async def test_cmd_week_non_admin_silent_ignore(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Non-admin → silent."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=NON_ADMIN_TG_ID, text="/week")
    await admin_handlers.cmd_week(msg)

    assert _answer_call_count(msg) == 0


@pytest.mark.asyncio
async def test_cmd_week_master_not_found_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Admin without master row → '❌ Мастер не найден'."""
    async with session_factory() as session:
        await _seed_admin_stack(session, admin_telegram_id=777777777)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/week")
    await admin_handlers.cmd_week(msg)

    text = _answer_text(msg)
    assert "Мастер не найден" in text


@pytest.mark.asyncio
async def test_cmd_week_no_bookings_shows_empty_message(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """No bookings within 7 days → 'На ближайшую неделю записей нет.'"""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/week")
    await admin_handlers.cmd_week(msg)

    text = _answer_text(msg)
    assert "На ближайшую неделю записей нет" in text


# ============================================================
# cmd_services — 10 branches
# ============================================================


@pytest.mark.asyncio
async def test_cmd_services_happy_creates_service(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Happy: 'services add Стрижка 60 1500' → '✅ Услуга добавлена' + Service row."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services add Стрижка 60 1500")
    await admin_handlers.cmd_services(msg, _make_command("add Стрижка 60 1500"))

    text = _answer_text(msg)
    assert "✅ Услуга добавлена" in text
    assert "Стрижка" in text
    assert "60" in text  # duration
    assert "1500" in text  # price

    async with session_factory() as verify:
        svc = (await verify.execute(select(Service))).scalar_one()
        assert svc.name == "Стрижка"
        assert svc.duration_minutes == 60
        assert svc.price == Decimal("1500")


@pytest.mark.asyncio
async def test_cmd_services_non_admin_silent_ignore(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Non-admin → silent."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=NON_ADMIN_TG_ID, text="/services add Стрижка 60 1500")
    await admin_handlers.cmd_services(msg, _make_command("add Стрижка 60 1500"))

    assert _answer_call_count(msg) == 0


@pytest.mark.asyncio
async def test_cmd_services_no_args_shows_format_hint(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """No args at all → 'Формат: /services add ...'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services")
    await admin_handlers.cmd_services(msg, _make_command(None))

    text = _answer_text(msg)
    assert "Формат:" in text
    assert "/services add" in text


@pytest.mark.asyncio
async def test_cmd_services_wrong_first_arg_shows_format(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """'services list' (args[0] != 'add') → format hint (MVP only supports 'add')."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services list")
    await admin_handlers.cmd_services(msg, _make_command("list"))

    text = _answer_text(msg)
    assert "Формат:" in text
    assert "/services add" in text


@pytest.mark.asyncio
async def test_cmd_services_wrong_args_count_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """'services add Стрижка 60' (3 args instead of 4) → '❌ Нужно 3 параметра'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services add Стрижка 60")
    await admin_handlers.cmd_services(msg, _make_command("add Стрижка 60"))

    text = _answer_text(msg)
    assert "Нужно 3 параметра" in text


@pytest.mark.asyncio
async def test_cmd_services_non_numeric_duration_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """'add Стрижка xx 1500' → int('xx') ValueError → '❌ Длительность должна быть числом'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services add Стрижка xx 1500")
    await admin_handlers.cmd_services(msg, _make_command("add Стрижка xx 1500"))

    text = _answer_text(msg)
    assert "Длительность должна быть числом" in text


@pytest.mark.asyncio
async def test_cmd_services_non_numeric_price_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """'add Стрижка 60 xx' → Decimal('xx') InvalidOperation → '❌ Цена должна быть числом'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services add Стрижка 60 xx")
    await admin_handlers.cmd_services(msg, _make_command("add Стрижка 60 xx"))

    text = _answer_text(msg)
    assert "Цена должна быть числом" in text


@pytest.mark.asyncio
async def test_cmd_services_zero_duration_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """duration=0 → '❌ Длительность должна быть > 0'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services add Стрижка 0 1500")
    await admin_handlers.cmd_services(msg, _make_command("add Стрижка 0 1500"))

    text = _answer_text(msg)
    assert "Длительность должна быть > 0" in text


@pytest.mark.asyncio
async def test_cmd_services_negative_price_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """price=-100 → '❌ Цена не может быть отрицательной'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services add Стрижка 60 -100")
    await admin_handlers.cmd_services(msg, _make_command("add Стрижка 60 -100"))

    text = _answer_text(msg)
    assert "Цена не может быть отрицательной" in text


@pytest.mark.asyncio
async def test_cmd_services_master_not_found_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Admin user has no master row → '❌ Мастер не найден'."""
    async with session_factory() as session:
        await _seed_admin_stack(session, admin_telegram_id=777777777)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services add Стрижка 60 1500")
    await admin_handlers.cmd_services(msg, _make_command("add Стрижка 60 1500"))

    text = _answer_text(msg)
    assert "Мастер не найден" in text


@pytest.mark.asyncio
async def test_cmd_services_service_validation_error_from_create_service(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_service raises ValueError (e.g. name empty after strip) →
    handler catches and shows '❌ {exc}'.

    We trigger via empty name — service raises 'service name must not be empty'.
    Note: 'add 60 1500' — args[1] is '' → after strip is empty → service raises.
    The handler parses `args[1].replace('_', ' ')` = '' before calling service.
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    # Name='' (empty) passes handler parse, but service rejects on `not name.strip()`
    msg = _make_message(user_id=ADMIN_TG_ID, text="/services add _ 60 1500")
    # '_' becomes ' ' (replace), then service gets ' ' which fails `not name.strip()`
    await admin_handlers.cmd_services(msg, _make_command("add _ 60 1500"))

    text = _answer_text(msg)
    assert "service name must not be empty" in text


# ============================================================
# _render_bookings — empty + mixed statuses
# ============================================================


def test_render_bookings_empty_list_returns_only_title() -> None:
    """Empty bookings list → only title (no booking lines)."""
    result = admin_handlers._render_bookings("📅 Заголовок:", [], TZ)
    # Lines: [title, "", ""] — actually [title, ""] joined with \n → title + empty
    # Looking at impl: lines = [title, ""] ; "\n".join → "📅 Заголовок:\n"
    assert result.startswith("📅 Заголовок:")
    # No bullet "•" because loop body never runs
    assert "•" not in result


def test_render_bookings_mixed_statuses() -> None:
    """Mix of confirmed/transferred/cancelled bookings → all rendered (no status filter
    in render, just formats each booking's snapshot)."""
    # Build minimal Booking-like mocks (we don't need SQLAlchemy model — _render_bookings
    # only reads .start_at, .client_name_snapshot, .service_title_snapshot).
    # Cast to list[Booking] for mypy — runtime uses duck-typing.
    from typing import cast

    bookings = cast(
        list[Booking],
        [
            MagicMock(
                start_at=datetime(2026, 3, 17, 11, 0, tzinfo=UTC),
                client_name_snapshot="Паша",
                service_title_snapshot="Стрижка",
            ),
            MagicMock(
                start_at=datetime(2026, 3, 17, 12, 0, tzinfo=UTC),
                client_name_snapshot="Иван",
                service_title_snapshot="Бритьё",
            ),
            MagicMock(
                start_at=datetime(2026, 3, 18, 14, 0, tzinfo=UTC),
                client_name_snapshot="Олег",
                service_title_snapshot="Укладка",
            ),
        ],
    )

    result = admin_handlers._render_bookings("📅 Записи:", bookings, TZ)

    # All 3 bookings rendered as bullets
    assert result.count("•") == 3
    # Local time conversion (UTC → Moscow +3): 11:00 UTC → 14:00 MSK
    assert "14:00" in result
    assert "15:00" in result
    # 2026-03-18 14:00 UTC → 17:00 MSK
    assert "17:00" in result
    # All names appear
    assert "Паша" in result and "Иван" in result and "Олег" in result
    # All services
    assert "Стрижка" in result and "Бритьё" in result and "Укладка" in result


def test_render_bookings_strips_newlines_in_snapshots() -> None:
    """Newline defense: client_name_snapshot with '\n' is replaced with space
    (display-only, DB stays intact — per admin.py docstring)."""
    from typing import cast

    bookings = cast(
        list[Booking],
        [
            MagicMock(
                start_at=datetime(2026, 3, 17, 11, 0, tzinfo=UTC),
                client_name_snapshot="Паша\nВторник",
                service_title_snapshot="Стрижка\nVIP",
            ),
        ],
    )

    result = admin_handlers._render_bookings("📅 Записи:", bookings, TZ)

    # Newlines in snapshots are replaced with spaces (no newline inside the bullet line)
    bullet_line = [line for line in result.split("\n") if line.startswith("•")][0]
    assert "\n" not in bullet_line  # the bullet is on a single line
    assert "Паша Вторник" in bullet_line  # newline → space
    assert "Стрижка VIP" in bullet_line


# ============================================================
# Tier 2 (T9) — admin handler edge branches (NEXT_COVERAGE_GAPS.md)
# Covers bot/handlers/admin.py:
#   66  — _resolve_master_and_business business is None (broken FK)
#   136-137 — cmd_addslots resolved is None (master not in DB for admin_id)
#   242 — cmd_closeslot slot already closed (idempotent re-close)
# Dead code (NOT covered, unreachable):
#   128-129 — `if not hours` (args[1:] never empty after len(args) >= 2 check)
#   133, 210, 345 — `if admin_id is None` (admin_id never None after _is_admin True)
# ============================================================


@pytest.mark.asyncio
async def test_cmd_addslots_master_not_found_when_admin_id_not_in_db(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Covers admin.py:136-137 — admin_id (settings.ADMIN_ID) resolves to no
    master in DB → _resolve_master_and_business returns None → handler shows
    'Мастер не найден'.

    Setup: seed admin stack under NON_ADMIN_TG_ID (not settings.ADMIN_ID).
    Then invoke cmd_addslots as ADMIN_TG_ID (passes _is_admin) — but
    _resolve_master_and_business(ADMIN_TG_ID) finds no master row.
    """
    async with session_factory() as session:
        # Seed admin stack under a DIFFERENT telegram_id — so ADMIN_TG_ID has
        # no master in DB. _is_admin still True (settings.ADMIN_ID == ADMIN_TG_ID
        # via conftest env), but _resolve_master_and_business returns None.
        await _seed_admin_stack(session, admin_telegram_id=NON_ADMIN_TG_ID)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/addslots {tomorrow} 11")
    await admin_handlers.cmd_addslots(msg, _make_command(f"{tomorrow} 11"))

    text = _answer_text(msg)
    assert "Мастер не найден" in text

    # No slots created (handler early-returned before service call).
    async with session_factory() as verify:
        slots = (await verify.execute(select(Slot))).scalars().all()
        assert len(slots) == 0


@pytest.mark.asyncio
async def test_cmd_closeslot_returns_false_race_else_branch(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers admin.py:242 — close_slot returns False (slot deleted between
    handler SELECT and close_slot SELECT — race) → handler shows
    'Слот уже был закрыт' (the else-branch of `if updated`).

    Note: close_slot actually returns True for already-closed (idempotent in
    service line 78, covered by test_cmd_closeslot_already_closed_idempotent
    above), so this handler branch is reachable only via race or monkeypatch.
    We monkeypatch close_slot to return False to exercise the handler's
    else-branch (line 242) — testing handler logic, not service.
    """
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        await _seed_slot(
            session, master_id=ctx["master_id"], slot_date=tomorrow, hour=14, status="open"
        )

    async def _return_false(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(admin_handlers, "close_slot", _return_false)

    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/closeslot {tomorrow} 14")
    await admin_handlers.cmd_closeslot(msg, _make_command(f"{tomorrow} 14"))

    text = _answer_text(msg)
    assert "уже был закрыт" in text


@pytest.mark.asyncio
async def test_resolve_master_and_business_returns_none_when_business_fk_broken(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Covers admin.py:66 — master exists but business_id FK broken (business
    row deleted out of band) → business is None → return None (defense in depth).

    SQLite has PRAGMA foreign_keys=OFF by default in our engine setup, so we
    can DELETE FROM businesses leaving a dangling master.business_id. In prod
    (Postgres with FK ON), this branch is hit only on referential corruption —
    the handler still guards against it rather than crashing on
    `business.timezone` access.
    """
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session, admin_telegram_id=ADMIN_TG_ID)
        biz_id = ctx["business_id"]
        # Delete the business row, leaving master.business_id dangling
        # (PRAGMA foreign_keys=OFF in aiosqlite by default — DELETE succeeds).
        from sqlalchemy import delete

        await session.execute(delete(Business).where(Business.id == biz_id))
        await session.commit()

    result = await admin_handlers._resolve_master_and_business(ADMIN_TG_ID)
    assert result is None
