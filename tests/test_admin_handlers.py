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
- cmd_services: happy + не-админ + no args/args[0] != "add" + len != 3 +
  невалидный duration + duration <= 0 + master not found +
  ValueError from create_service
  (price убран в Session 5.10 — тесты на price удалены)
- _render_bookings: empty list + mixed statuses (confirmed/transferred/cancelled)
- _is_admin, _require_admin_or_silent, _resolve_master_and_business: direct unit

Why handler tests (not just service tests, AGENTS.md § anti-overengineering rule 3):
admin handlers contain partition decisions computed in the handler:
- /addslots past-date check (handler compares slot_date < today_local)
- /closeslot hour range check (handler validates 0-23 before service call)
- /services args validation (handler parses duration before service)
Display math that mirrors service invariants is a logic-change risk, not pure I/O.

Pattern (NEXT_SESSION_PROMPT.md 38, mirror of test_client_handlers.py):
direct handler invocation with mock Message + CommandObject + monkeypatch of
`bot.handlers.admin.async_session_factory` so the handler DB calls hit in-memory SQLite.
Avoids `dp.feed_update` ceremony — no Dispatcher/router-wiring needed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
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
    """Happy: 'services add Стрижка 60' → '✅ Услуга добавлена' + Service row.

    Price убран в Session 5.10 — поле Service.price nullable, не вводится
    через FSM. Здесь проверяем только name + duration.
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services add Стрижка 60")
    await admin_handlers.cmd_services(msg, _make_command("add Стрижка 60"))

    text = _answer_text(msg)
    assert "✅ Услуга добавлена" in text
    assert "Стрижка" in text
    assert "60" in text  # duration

    async with session_factory() as verify:
        svc = (await verify.execute(select(Service))).scalar_one()
        assert svc.name == "Стрижка"
        assert svc.duration_minutes == 60
        assert svc.price is None  # Session 5.10: price nullable, not entered via FSM


@pytest.mark.asyncio
async def test_cmd_services_non_admin_silent_ignore(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Non-admin → silent."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=NON_ADMIN_TG_ID, text="/services add Стрижка 60")
    await admin_handlers.cmd_services(msg, _make_command("add Стрижка 60"))

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
    """'services add Стрижка' (2 args instead of 3) → '❌ Нужно 2 параметра'.

    Session 5.10: price убран, формат стал 'add NAME DURATION' (3 args
    включая 'add'). Wrong count теперь 2 args ('add Стрижка') или 4 args
    ('add Стрижка 60 1500').
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services add Стрижка")
    await admin_handlers.cmd_services(msg, _make_command("add Стрижка"))

    text = _answer_text(msg)
    assert "Нужно 2 параметра" in text


@pytest.mark.asyncio
async def test_cmd_services_non_numeric_duration_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """'add Стрижка xx' → int('xx') ValueError → '❌ Длительность должна быть числом'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services add Стрижка xx")
    await admin_handlers.cmd_services(msg, _make_command("add Стрижка xx"))

    text = _answer_text(msg)
    assert "Длительность должна быть числом" in text


@pytest.mark.asyncio
async def test_cmd_services_zero_duration_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """duration=0 → '❌ Длительность должна быть > 0'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services add Стрижка 0")
    await admin_handlers.cmd_services(msg, _make_command("add Стрижка 0"))

    text = _answer_text(msg)
    assert "Длительность должна быть > 0" in text


@pytest.mark.asyncio
async def test_cmd_services_master_not_found_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Admin user has no master row → '❌ Мастер не найден'."""
    async with session_factory() as session:
        await _seed_admin_stack(session, admin_telegram_id=777777777)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services add Стрижка 60")
    await admin_handlers.cmd_services(msg, _make_command("add Стрижка 60"))

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
    Note: 'add _ 60' — args[1] is '' → after strip is empty → service raises.
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    # Name='' (empty) passes handler parse, but service rejects on `not name.strip()`
    msg = _make_message(user_id=ADMIN_TG_ID, text="/services add _ 60")
    # '_' becomes ' ' (replace), then service gets ' ' which fails `not name.strip()`
    await admin_handlers.cmd_services(msg, _make_command("add _ 60"))

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
# Dead code removed 2026-08-22 (was: 128-129 if not hours, 133/210/345 if admin_id is None):
#   unreachable after early `if not _is_admin: return` — replaced with `assert admin_id is not None`
#   for type narrowing (mypy) — pattern matches client.py:302-304.
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


# ============================================================
# Этап 5.9 — admin_move flow handler tests (6 tests)
# ============================================================
# Coverage:
# - cmd_today renders move button (reply_markup non-None)
# - admin_move_select_cb sets state + shows calendar
# - admin_move_simple_calendar_cb: no_workday hint + inactive_workday hint
# - admin_move_slot_30_cb saves state + shows summary
# - admin_move_confirm_cb calls service + notifies client + clears state
#
# Pattern: direct handler invocation with mock CallbackQuery + MagicMock state
# (FSMContext is hard to instantiate without Dispatcher; MagicMock suffices
# for set_state/update_data/get_data/clear assertions). scheduler patched via
# patch on admin_move_booking for confirm_cb test (avoids real DB writes).
# ============================================================


def _make_callback(
    user_id: int,
    *,
    callback_data: Any = None,
) -> MagicMock:
    """Mock aiogram.CallbackQuery with spec — answer is AsyncMock for assertions.

    message.answer is AsyncMock; callback.bot.send_message is AsyncMock
    (for confirm_cb test where handler sends client notification).
    message has spec=Message so isinstance(callback.message, Message) in
    handler code passes True (mirror INL-001 edit_text path); edit_text is
    AsyncMock so `await edit_text(...)` works in calendar_cb tests.
    """
    cb = MagicMock(spec=["from_user", "message", "bot", "answer", "data"])
    cb.from_user = _make_user(user_id)
    cb.message = MagicMock(spec=Message)
    cb.message.answer = AsyncMock()
    cb.message.edit_text = AsyncMock()
    cb.bot = MagicMock()
    cb.bot.send_message = AsyncMock()
    cb.answer = AsyncMock()
    cb.data = callback_data if callback_data is not None else "noop"
    return cb


def _make_mock_state(data: dict[str, Any] | None = None) -> MagicMock:
    """Mock FSMContext — AsyncMock for set_state/update_data/get_data/clear.

    `get_data` returns the passed dict (or empty dict) — for tests that
    need state pre-populated (e.g. confirm_cb expects booking_id + workday_id +
    start_minute in FSM data).
    """
    state = MagicMock()
    state.set_state = AsyncMock()
    state.update_data = AsyncMock()
    state.clear = AsyncMock()
    stored = dict(data) if data else {}

    async def _get_data() -> dict[str, Any]:
        return stored

    state.get_data = _get_data
    return state


def callback_answer_text(callback: MagicMock) -> str:
    """Extract text from callback.message.answer (first positional arg)."""
    args, _ = callback.message.answer.call_args
    return str(args[0])


@pytest.mark.asyncio
async def test_cmd_menu_fresh_shows_menu(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """/menu from fresh state (None) → answer '📋 Меню:' + admin_inline_menu.
    No state to clear — state.clear() is a no-op on None.
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(ADMIN_TG_ID, text="/menu")
    state = _make_mock_state()

    await admin_handlers.cmd_menu(msg, state)

    args, kwargs = msg.answer.call_args
    text = args[0] if args else kwargs.get("text", "")
    assert "Меню" in text
    reply_markup = kwargs.get("reply_markup") or (args[1] if len(args) > 1 else None)
    assert reply_markup is not None


@pytest.mark.asyncio
async def test_cmd_menu_escapes_from_fsm_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """/menu mid-FSM (stuck in opening_week_days) → state.clear + show menu.

    Escape hatch: admin stuck in /openweek flow after partial interaction can
    always /menu out without knowing /cancel. StateFilter("*") matches any
    state including the FSM-trapped ones.
    """

    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(ADMIN_TG_ID, text="/menu")
    state = _make_mock_state()  # state proxy, real FSM would have data

    await admin_handlers.cmd_menu(msg, state)

    state.clear.assert_called_once()
    args, kwargs = msg.answer.call_args
    text = args[0] if args else kwargs.get("text", "")
    assert "Меню" in text
    reply_markup = kwargs.get("reply_markup") or (args[1] if len(args) > 1 else None)
    assert reply_markup is not None


@pytest.mark.asyncio
async def test_cmd_today_renders_move_button_for_bookings(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """/today with bookings → answer includes reply_markup (admin_today_keyboard
    with [🔄 Перенести] button per booking)."""
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        now_local = datetime.now(ZoneInfo(TZ))
        today_local_at_14 = now_local.replace(hour=14, minute=0, second=0, microsecond=0)
        slot = await _seed_slot(
            session,
            master_id=ctx["master_id"],
            slot_date=today_local_at_14.date(),
            hour=14,
            status="open",
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

    # msg.answer called with reply_markup — second positional arg or kwarg.
    args, kwargs = msg.answer.call_args
    reply_markup = kwargs.get("reply_markup") or (args[1] if len(args) > 1 else None)
    assert reply_markup is not None, "Expected reply_markup with [🔄 Перенести] button"
    # InlineKeyboardMarkup has .inline_keyboard list — at least one button.
    assert len(reply_markup.inline_keyboard) >= 1


@pytest.mark.asyncio
async def test_admin_move_select_cb_sets_state_and_shows_calendar(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[🔄 Перенести] tap → state.set_state(AdminMoveStates.selecting_date),
    state.update_data(admin_move_booking_id), answer with calendar keyboard.
    """
    from bot.keyboards.admin import AdminMoveCallbackData
    from bot.states import AdminMoveStates

    async with session_factory() as session:
        await _seed_admin_stack(session)

    booking_id = UUID("12345678-1234-5678-1234-567812345678")
    cb_data = AdminMoveCallbackData(booking_id=booking_id)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    state = _make_mock_state()

    await admin_handlers.admin_move_select_cb(callback, cb_data, state)

    state.update_data.assert_called_once()
    update_args, update_kwargs = state.update_data.call_args
    data_passed = update_args[0] if update_args else update_kwargs
    assert data_passed["admin_move_booking_id"] == str(booking_id)

    state.set_state.assert_called_once_with(AdminMoveStates.selecting_date)

    args, kwargs = callback.message.answer.call_args
    reply_markup = kwargs.get("reply_markup") or (args[1] if len(args) > 1 else None)
    assert reply_markup is not None, "Expected calendar reply_markup"


@pytest.mark.asyncio
async def test_admin_move_simple_calendar_no_workday_hint(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Day select with no WorkDay → 'Мастер не работает в этот день' + re-show
    calendar (no slot picker).
    """
    from aiogram_calendar import SimpleCalendarCallback
    from aiogram_calendar.schemas import SimpleCalAct

    async with session_factory() as session:
        await _seed_admin_stack(session)

    cb = MagicMock(spec=["from_user", "message", "bot", "answer"])
    cb.from_user = _make_user(ADMIN_TG_ID)
    cb.message = MagicMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()

    future_date = datetime.now(UTC) + timedelta(days=30)
    cal_cb_data = SimpleCalendarCallback(
        act=SimpleCalAct.day,
        year=future_date.year,
        month=future_date.month,
        day=future_date.day,
    )

    state = _make_mock_state()

    from unittest.mock import patch

    with patch(
        "aiogram_calendar.SimpleCalendar.process_selection",
        return_value=(True, future_date),
    ):
        await admin_handlers.admin_move_simple_calendar_cb(cb, cal_cb_data, state)

    text = callback_answer_text(cb)
    assert "не работает в этот день" in text


@pytest.mark.asyncio
async def test_admin_move_simple_calendar_inactive_workday_hint(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Day select with is_active=False WorkDay → 'Этот день закрыт' + re-show
    calendar.
    """
    from aiogram_calendar import SimpleCalendarCallback
    from aiogram_calendar.schemas import SimpleCalAct

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        from datetime import time as dt_time

        from bot.models import WorkDay

        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        workday = WorkDay(
            master_id=ctx["master_id"],
            work_date=tomorrow,
            start_time=dt_time(10, 0),
            end_time=dt_time(20, 0),
            max_concurrent_clients=1,
            is_active=False,  # closed via /closeday
        )
        session.add(workday)
        await session.commit()

    cb = MagicMock(spec=["from_user", "message", "bot", "answer"])
    cb.from_user = _make_user(ADMIN_TG_ID)
    cb.message = MagicMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()

    future_date = datetime.combine(tomorrow, datetime.min.time())
    cal_cb_data = SimpleCalendarCallback(
        act=SimpleCalAct.day,
        year=future_date.year,
        month=future_date.month,
        day=future_date.day,
    )

    state = _make_mock_state()

    from unittest.mock import patch

    with patch(
        "aiogram_calendar.SimpleCalendar.process_selection",
        return_value=(True, future_date),
    ):
        await admin_handlers.admin_move_simple_calendar_cb(cb, cal_cb_data, state)

    text = callback_answer_text(cb)
    assert "закрыт" in text


@pytest.mark.asyncio
async def test_admin_move_slot_30_cb_saves_state_and_shows_summary(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Slot tap → state.update_data(workday_id+start_minute),
    state.set_state(confirming), answer with summary + confirm_keyboard.
    """
    from bot.keyboards.admin import AdminMoveSlot30CallbackData
    from bot.states import AdminMoveStates

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        from datetime import time as dt_time

        from bot.models import WorkDay

        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        workday = WorkDay(
            master_id=ctx["master_id"],
            work_date=tomorrow,
            start_time=dt_time(10, 0),
            end_time=dt_time(20, 0),
            max_concurrent_clients=1,
            is_active=True,
        )
        session.add(workday)
        now_local = datetime.now(ZoneInfo(TZ))
        today_at_14 = now_local.replace(hour=14, minute=0, second=0, microsecond=0)
        slot = await _seed_slot(
            session,
            master_id=ctx["master_id"],
            slot_date=today_at_14.date(),
            hour=14,
            status="open",
        )
        booking = await _seed_booking(
            session,
            ctx=ctx,
            slot=slot,
            start_at_utc_naive=_local_to_utc_naive(today_at_14),
            status="confirmed",
        )
        await session.commit()

    workday_id = workday.id
    booking_id = booking.id
    cb_data = AdminMoveSlot30CallbackData(workday_id=workday_id, start_minute=15 * 60)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    state = _make_mock_state(data={"admin_move_booking_id": str(booking_id)})

    await admin_handlers.admin_move_slot_30_cb(callback, cb_data, state)

    state.update_data.assert_called_once()
    update_args, update_kwargs = state.update_data.call_args
    data_passed = update_args[0] if update_args else update_kwargs
    assert data_passed["admin_move_new_workday_id"] == str(workday_id)
    assert data_passed["admin_move_new_start_minute"] == 15 * 60
    state.set_state.assert_called_once_with(AdminMoveStates.confirming)

    text = callback_answer_text(callback)
    assert "Подтвердите перенос" in text
    assert "Было:" in text
    assert "Станет:" in text


@pytest.mark.asyncio
async def test_admin_move_confirm_cb_calls_service_and_notifies_client(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[✅ Перенести] → admin_move_booking called with extracted args,
    bot.send_message to client_telegram_id, state.clear, answer with success.

    Patches admin_move_booking to return a stub AdminMoveResult — avoids real
    DB writes (already covered by service tests in test_admin_move.py).
    """
    from bot.services.admin_move import AdminMoveResult

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)

    booking_id = UUID("11111111-1111-1111-1111-111111111111")
    workday_id = UUID("22222222-2222-2222-2222-222222222222")
    state = _make_mock_state(
        data={
            "admin_move_booking_id": str(booking_id),
            "admin_move_new_workday_id": str(workday_id),
            "admin_move_new_start_minute": 15 * 60,  # 15:00
        }
    )

    callback = _make_callback(ADMIN_TG_ID)
    mock_scheduler = MagicMock()

    stub_result = AdminMoveResult(
        booking_id=booking_id,
        old_start_at=datetime.now(UTC) - timedelta(hours=1),
        new_start_at=datetime.now(UTC) + timedelta(days=2),
        client_telegram_id=111222333,
        client_name_snapshot="Паша",
        service_title_snapshot="Стрижка",
        master_id=ctx["master_id"],
        business_id=ctx["business_id"],
        business_timezone=TZ,
        old_slot_id=None,
        notification_logged=True,
    )

    from unittest.mock import patch

    with patch(
        "bot.handlers.admin.admin_move_booking",
        return_value=stub_result,
    ) as mock_service:
        await admin_handlers.admin_move_confirm_cb(callback, state, mock_scheduler)

    mock_service.assert_called_once()
    call_args = mock_service.call_args
    assert call_args.args[1] == booking_id
    assert call_args.args[2] == workday_id
    assert call_args.args[3].hour == 15
    assert call_args.args[3].minute == 0
    assert call_args.args[4] == mock_scheduler

    state.clear.assert_called_once()

    callback.bot.send_message.assert_called_once()
    send_args, send_kwargs = callback.bot.send_message.call_args
    chat_id = send_args[0] if send_args else send_kwargs.get("chat_id")
    text_sent = send_args[1] if len(send_args) > 1 else send_kwargs.get("text")
    assert chat_id == 111222333
    assert "перенесена мастером" in text_sent

    text = callback_answer_text(callback)
    assert "✅ Запись перенесена" in text
    assert "Клиент уведомлён" in text


# ============================================================
# Этап 5.10 inline-часы: /addslots inline window MODIFY flow
#
# /closeslot SHRINK inline flow REMOVED (5.10 simplification) — «Изменить
# окно» handles both shrink+extend+shift via two-phase picker start→end.
#
# Coverage (mirror admin_move tests 1359-1626 pattern):
#   /addslots calendar_cb: no workday redirect + workday shows start picker
#   admin_window_start_cb: pick start → end picker + state loss
#   admin_window_end_cb: pick end → summary + state loss
#   admin_window_confirm_cb: open_workday success + WorkDayShrinkError +
#     SQLAlchemyError + state loss
#   admin_window_cancel_cb: clears state
# ============================================================


async def _seed_workday(
    session: AsyncSession,
    *,
    ctx: dict[str, Any],
    work_date: date,
    start_time_str: str = "10:00",
    end_time_str: str = "20:00",
    is_active: bool = True,
) -> Any:
    """Insert WorkDay for (master, date) — shared helper for inline-часы tests.

    start_time_str / end_time_str: "HH:MM" → datetime.time. is_active controls
    closed-workday branch testing.
    """
    from datetime import time as dt_time

    from bot.models import WorkDay

    sh, sm = (int(x) for x in start_time_str.split(":"))
    eh, em = (int(x) for x in end_time_str.split(":"))
    workday = WorkDay(
        master_id=ctx["master_id"],
        work_date=work_date,
        start_time=dt_time(sh, sm),
        end_time=dt_time(eh, em),
        max_concurrent_clients=1,
        is_active=is_active,
    )
    session.add(workday)
    await session.commit()
    return workday


def _picker_reply_markup(callback: MagicMock) -> Any:
    """Extract reply_markup from callback.message.answer call (mirror admin_move
    tests pattern 1386-1388, 1548-1553).
    """
    args, kwargs = callback.message.answer.call_args
    return kwargs.get("reply_markup") or (args[1] if len(args) > 1 else None)


def _state_data_passed(state: MagicMock) -> dict[str, Any]:
    """Extract dict passed to state.update_data (mirror admin_move tests 1543-1546)."""
    update_args, update_kwargs = state.update_data.call_args
    result: dict[str, Any] = update_args[0] if update_args else update_kwargs
    return result


def _state_all_updates(state: MagicMock) -> dict[str, Any]:
    """Merge ALL update_data calls (Session 5.26: handlers may call update_data
    twice — picked_end_minute then selected_weekdays=[] in openweek_end_cb).
    Later calls overwrite earlier for the same key (mirror FSM semantics).
    """
    merged: dict[str, Any] = {}
    for call in state.update_data.call_args_list:
        args, kwargs = call
        merged.update(args[0] if args else kwargs)
    return merged


# --- /addslots calendar_cb -------------------------------------------------


@pytest.mark.asyncio
async def test_admin_addslots_calendar_cb_no_workday_redirects_to_openday(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Day select with no WorkDay → 'не открыт' redirect hint to /openday."""
    from unittest.mock import patch

    from aiogram_calendar import SimpleCalendarCallback
    from aiogram_calendar.schemas import SimpleCalAct

    async with session_factory() as session:
        await _seed_admin_stack(session)

    future_date = datetime.now(UTC) + timedelta(days=30)
    cal_cb_data = SimpleCalendarCallback(
        act=SimpleCalAct.day,
        year=future_date.year,
        month=future_date.month,
        day=future_date.day,
    )
    callback = _make_callback(ADMIN_TG_ID, callback_data=cal_cb_data)
    callback.message.edit_text = AsyncMock()
    state = _make_mock_state()

    with patch(
        "aiogram_calendar.SimpleCalendar.process_selection",
        return_value=(True, future_date),
    ):
        await admin_handlers.admin_addslots_calendar_cb(callback, cal_cb_data, state)

    # No workday → message edit_text (or answer fallback) with redirect hint.
    text: str
    if callback.message.edit_text.called:
        text = str(callback.message.edit_text.call_args.args[0])
    else:
        text = callback_answer_text(callback)
    assert "не открыт" in text
    assert "/openday" in text


@pytest.mark.asyncio
async def test_admin_addslots_calendar_cb_workday_exists_shows_start_picker(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Day select with active WorkDay → set_state(picking_window_start),
    reply_markup=admin_window_slot_picker_keyboard (start picker).
    """
    from unittest.mock import patch

    from aiogram_calendar import SimpleCalendarCallback
    from aiogram_calendar.schemas import SimpleCalAct
    from bot.states import AdminStates

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        await _seed_workday(session, ctx=ctx, work_date=tomorrow)

    future_date = datetime.combine(tomorrow, datetime.min.time())
    cal_cb_data = SimpleCalendarCallback(
        act=SimpleCalAct.day,
        year=future_date.year,
        month=future_date.month,
        day=future_date.day,
    )
    callback = _make_callback(ADMIN_TG_ID, callback_data=cal_cb_data)
    callback.message.edit_text = AsyncMock()
    state = _make_mock_state()

    with patch(
        "aiogram_calendar.SimpleCalendar.process_selection",
        return_value=(True, future_date),
    ):
        await admin_handlers.admin_addslots_calendar_cb(callback, cal_cb_data, state)

    state.set_state.assert_called_once_with(AdminStates.picking_window_start)
    # update_data called with selected_date + workday_id.
    data_passed = _state_data_passed(state)
    assert data_passed["selected_date"] == tomorrow.isoformat()
    assert "workday_id" in data_passed

    # Edit text with start picker reply_markup (since isinstance Message).
    assert callback.message.edit_text.called
    edit_args, edit_kwargs = callback.message.edit_text.call_args
    reply_markup = edit_kwargs.get("reply_markup") or (edit_args[1] if len(edit_args) > 1 else None)
    assert reply_markup is not None, "Expected start picker reply_markup"


# --- admin_window_start_cb ------------------------------------------------


@pytest.mark.asyncio
async def test_admin_window_start_cb_picks_start_shows_end_picker(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[start slot tap] → state.update_data(picked_start_minute),
    state.set_state(picking_window_end), answer with end-picker keyboard.
    """
    from bot.keyboards.admin import AdminWindowSlot30CallbackData
    from bot.states import AdminStates

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        workday = await _seed_workday(session, ctx=ctx, work_date=tomorrow)

    picked_start_minute = 11 * 60  # 11:00
    cb_data = AdminWindowSlot30CallbackData(workday_id=workday.id, start_minute=picked_start_minute)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    state = _make_mock_state(
        data={
            "selected_date": tomorrow.isoformat(),
            "workday_id": str(workday.id),
        }
    )

    await admin_handlers.admin_window_start_cb(callback, cb_data, state)

    data_passed = _state_data_passed(state)
    assert data_passed["picked_start_minute"] == picked_start_minute
    state.set_state.assert_called_once_with(AdminStates.picking_window_end)

    reply_markup = _picker_reply_markup(callback)
    assert reply_markup is not None, "Expected end-picker reply_markup"
    args, _ = callback.message.answer.call_args
    text = str(args[0])
    assert "окончания" in text


@pytest.mark.asyncio
async def test_admin_window_start_cb_state_loss_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[start slot tap] with no workday_id in state → state.clear + 'Данные
    сессии потеряны' hint (state loss defensive check, mirror admin_move_confirm_cb).
    """
    from bot.keyboards.admin import AdminWindowSlot30CallbackData

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        workday = await _seed_workday(session, ctx=ctx, work_date=tomorrow)

    cb_data = AdminWindowSlot30CallbackData(workday_id=workday.id, start_minute=11 * 60)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    state = _make_mock_state(data={})  # empty state — simulate state loss

    await admin_handlers.admin_window_start_cb(callback, cb_data, state)

    state.clear.assert_called_once()
    state.set_state.assert_not_called()
    text = callback_answer_text(callback)
    assert "Данные сессии потеряны" in text


# --- admin_window_end_cb --------------------------------------------------


@pytest.mark.asyncio
async def test_admin_window_end_cb_picks_end_shows_summary(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[end slot tap] → state.update_data(picked_end_minute),
    state.set_state(confirming_window), answer with summary + confirm keyboard.
    """
    from bot.keyboards.admin import AdminWindowSlot30CallbackData
    from bot.states import AdminStates

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        workday = await _seed_workday(session, ctx=ctx, work_date=tomorrow)

    picked_start_minute = 11 * 60  # 11:00
    picked_end_minute = 18 * 60  # 18:00
    cb_data = AdminWindowSlot30CallbackData(workday_id=workday.id, start_minute=picked_end_minute)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    state = _make_mock_state(
        data={
            "selected_date": tomorrow.isoformat(),
            "workday_id": str(workday.id),
            "picked_start_minute": picked_start_minute,
        }
    )

    await admin_handlers.admin_window_end_cb(callback, cb_data, state)

    data_passed = _state_data_passed(state)
    assert data_passed["picked_end_minute"] == picked_end_minute
    state.set_state.assert_called_once_with(AdminStates.confirming_window)

    reply_markup = _picker_reply_markup(callback)
    assert reply_markup is not None, "Expected admin_window_confirm_keyboard"
    text = callback_answer_text(callback)
    assert "Изменить окно" in text
    assert "11:00" in text
    assert "18:00" in text


@pytest.mark.asyncio
async def test_admin_window_end_cb_state_loss_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[end slot tap] with picked_start_minute missing in state → state.clear
    + 'Данные сессии потеряны' hint.
    """
    from bot.keyboards.admin import AdminWindowSlot30CallbackData

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        workday = await _seed_workday(session, ctx=ctx, work_date=tomorrow)

    cb_data = AdminWindowSlot30CallbackData(workday_id=workday.id, start_minute=18 * 60)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    # picked_start_minute missing → state loss
    state = _make_mock_state(
        data={
            "selected_date": tomorrow.isoformat(),
            "workday_id": str(workday.id),
        }
    )

    await admin_handlers.admin_window_end_cb(callback, cb_data, state)

    state.clear.assert_called_once()
    state.set_state.assert_not_called()
    text = callback_answer_text(callback)
    assert "Данные сессии потеряны" in text


# --- admin_window_confirm_cb ---------------------------------------------


@pytest.mark.asyncio
async def test_admin_window_confirm_cb_calls_open_workday_success(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[✅ Подтвердить] → open_workday called with extracted args, state.clear,
    answer with success message (✅ Окно изменено).
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    work_date = (datetime.now(UTC) + timedelta(days=1)).date()
    picked_start_minute = 11 * 60  # 11:00
    picked_end_minute = 18 * 60  # 18:00
    state = _make_mock_state(
        data={
            "selected_date": work_date.isoformat(),
            "picked_start_minute": picked_start_minute,
            "picked_end_minute": picked_end_minute,
        }
    )
    callback = _make_callback(ADMIN_TG_ID)

    from unittest.mock import AsyncMock, patch

    mock_workday = MagicMock()
    mock_workday.work_date = work_date
    with patch(
        "bot.handlers.admin.open_workday",
        new_callable=AsyncMock,
        return_value=mock_workday,
    ) as mock_service:
        await admin_handlers.admin_window_confirm_cb(callback, state)

    mock_service.assert_called_once()
    call_args = mock_service.call_args
    # args[1]=master_id (skip session), args[2]=work_date, args[3]=start_time,
    # args[4]=end_time, kwargs business_tz.
    assert call_args.args[2] == work_date
    assert call_args.args[3].hour == 11
    assert call_args.args[3].minute == 0
    assert call_args.args[4].hour == 18
    assert call_args.args[4].minute == 0
    assert call_args.kwargs.get("business_tz") == TZ

    state.clear.assert_called_once()
    text = callback_answer_text(callback)
    assert "✅ Окно изменено" in text
    assert "11:00" in text and "18:00" in text


@pytest.mark.asyncio
async def test_admin_window_confirm_cb_value_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """open_workday raises ValueError (business validation) → message renders
    f'❌ {exc}\\n/addslots чтобы начать' (parity with admin_window_confirm_cb
    SQLAlchemyError test — same error-mapping pattern for both ValueError
    and SQLAlchemyError raised from open_workday in MODIFY flow).
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    work_date = (datetime.now(UTC) + timedelta(days=1)).date()
    state = _make_mock_state(
        data={
            "selected_date": work_date.isoformat(),
            "picked_start_minute": 11 * 60,
            "picked_end_minute": 18 * 60,
        }
    )
    callback = _make_callback(ADMIN_TG_ID)

    from unittest.mock import AsyncMock, patch

    with patch(
        "bot.handlers.admin.open_workday",
        new_callable=AsyncMock,
        side_effect=ValueError("invalid time range"),
    ):
        await admin_handlers.admin_window_confirm_cb(callback, state)

    state.clear.assert_called_once()  # state.clear() BEFORE service call
    text = callback_answer_text(callback)
    assert "invalid time range" in text
    assert "/addslots" in text


@pytest.mark.asyncio
async def test_admin_window_confirm_cb_workday_shrink_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """open_workday raises WorkDayShrinkError → 'Нельзя сократить окно' hint
    (race: concurrent create_booking between pick and confirm).
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    work_date = (datetime.now(UTC) + timedelta(days=1)).date()
    state = _make_mock_state(
        data={
            "selected_date": work_date.isoformat(),
            "picked_start_minute": 11 * 60,
            "picked_end_minute": 12 * 60,  # narrow window shrinks
        }
    )
    callback = _make_callback(ADMIN_TG_ID)

    from unittest.mock import AsyncMock, patch

    from bot.services.workday import WorkDayShrinkError

    with patch(
        "bot.handlers.admin.open_workday",
        new_callable=AsyncMock,
        side_effect=WorkDayShrinkError("conflict"),
    ):
        await admin_handlers.admin_window_confirm_cb(callback, state)

    state.clear.assert_called_once()  # state.clear() BEFORE service call
    text = callback_answer_text(callback)
    assert "Нельзя сократить окно" in text
    assert "conflict" in text


@pytest.mark.asyncio
async def test_admin_window_confirm_cb_sqlalchemy_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """open_workday raises SQLAlchemyError → 'Ошибка БД' message."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    work_date = (datetime.now(UTC) + timedelta(days=1)).date()
    state = _make_mock_state(
        data={
            "selected_date": work_date.isoformat(),
            "picked_start_minute": 11 * 60,
            "picked_end_minute": 18 * 60,
        }
    )
    callback = _make_callback(ADMIN_TG_ID)

    from unittest.mock import AsyncMock, patch

    from sqlalchemy.exc import SQLAlchemyError

    with patch(
        "bot.handlers.admin.open_workday",
        new_callable=AsyncMock,
        side_effect=SQLAlchemyError("db down"),
    ):
        await admin_handlers.admin_window_confirm_cb(callback, state)

    state.clear.assert_called_once()
    text = callback_answer_text(callback)
    assert "Ошибка БД" in text


@pytest.mark.asyncio
async def test_admin_window_confirm_cb_state_loss_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[✅ Подтвердить] with picked_end_minute missing in state → state.clear
    + 'Данные сессии потеряны' hint.
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    work_date = (datetime.now(UTC) + timedelta(days=1)).date()
    state = _make_mock_state(
        data={
            "selected_date": work_date.isoformat(),
            "picked_start_minute": 11 * 60,
            # picked_end_minute missing → state loss
        }
    )
    callback = _make_callback(ADMIN_TG_ID)

    await admin_handlers.admin_window_confirm_cb(callback, state)

    state.clear.assert_called_once()
    text = callback_answer_text(callback)
    assert "Данные сессии потеряны" in text


# --- admin_window_cancel_cb -----------------------------------------------


@pytest.mark.asyncio
async def test_admin_window_cancel_cb_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[❌ Отмена] (string F.data == 'admin_window_cancel') → state.clear + answer
    with 'Действие отменено' message.
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_window_cancel"
    state = _make_mock_state()

    await admin_handlers.admin_window_cancel_cb(callback, state)

    state.clear.assert_called_once()
    text = callback_answer_text(callback)
    assert "Действие отменено" in text


# ============================================================
# /openweek handlers (Session 5.26)
# ============================================================


@pytest.mark.asyncio
async def test_cmd_openweek_shows_start_picker(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """/openweek → state.clear + set_state(opening_week_start) + reply with
    start picker (admin_window_slot_picker_keyboard mode='start' БЕЗ booked_slots).
    """
    from bot.states import AdminStates

    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(ADMIN_TG_ID, text="/openweek")
    state = _make_mock_state()

    await admin_handlers.cmd_openweek(msg, state)

    state.clear.assert_called_once()
    state.set_state.assert_called_once_with(AdminStates.opening_week_start)
    data = _state_data_passed(state)
    assert "business_tz" in data

    args, kwargs = msg.answer.call_args
    text = args[0] if args else kwargs.get("text", "")
    assert "Открыть неделю" in text
    reply_markup = kwargs.get("reply_markup") or (args[1] if len(args) > 1 else None)
    assert reply_markup is not None, "Expected start picker reply_markup"


@pytest.mark.asyncio
async def test_cmd_openweek_master_not_found_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """/openweek without seeded master → '❌ Мастер не найден', state untouched
    (handler returns before set_state)."""
    async with session_factory():
        pass  # no seed

    msg = _make_message(ADMIN_TG_ID, text="/openweek")
    state = _make_mock_state()

    await admin_handlers.cmd_openweek(msg, state)

    text = _answer_text(msg)
    assert "Мастер не найден" in text
    state.set_state.assert_not_called()


@pytest.mark.asyncio
async def test_admin_openweek_entry_cb_sets_state_and_shows_picker(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[🗓 Открыть неделю] tap → state.clear + set_state(opening_week_start) +
    edit_text with start picker (callback.message is Message).
    """
    from bot.states import AdminStates

    async with session_factory() as session:
        await _seed_admin_stack(session)

    from bot.keyboards.admin import AdminOpenWeekEntryCallbackData

    cb_data = AdminOpenWeekEntryCallbackData()
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    callback.message.edit_text = AsyncMock()
    state = _make_mock_state()

    await admin_handlers.admin_openweek_entry_cb(callback, state)

    state.clear.assert_called_once()
    state.set_state.assert_called_once_with(AdminStates.opening_week_start)
    assert callback.message.edit_text.called
    edit_args, edit_kwargs = callback.message.edit_text.call_args
    text = edit_args[0] if edit_args else edit_kwargs.get("text", "")
    assert "Открыть неделю" in text


@pytest.mark.asyncio
async def test_admin_openweek_start_cb_saves_start_and_shows_end_picker(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[start slot tap] in opening_week_start → update_data(picked_start_minute),
    set_state(opening_week_end), answer with end picker (mode='end').
    """
    from uuid import UUID as _UUID

    from bot.keyboards.admin import AdminWindowSlot30CallbackData
    from bot.states import AdminStates

    async with session_factory() as session:
        await _seed_admin_stack(session)

    sentinel = _UUID(int=0)
    cb_data = AdminWindowSlot30CallbackData(workday_id=sentinel, start_minute=600)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    state = _make_mock_state({"business_tz": TZ})

    await admin_handlers.admin_openweek_start_cb(callback, cb_data, state)

    state.update_data.assert_called()
    state.set_state.assert_called_once_with(AdminStates.opening_week_end)
    data = _state_data_passed(state)
    assert data["picked_start_minute"] == 600
    args, kwargs = callback.message.answer.call_args
    text = args[0] if args else kwargs.get("text", "")
    assert "Шаг 2" in text
    reply_markup = kwargs.get("reply_markup") or (args[1] if len(args) > 1 else None)
    assert reply_markup is not None, "Expected end picker reply_markup"


@pytest.mark.asyncio
async def test_admin_openweek_end_cb_saves_end_and_shows_days(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[end slot tap] in opening_week_end → update_data(picked_end_minute +
    selected_weekdays=[]), set_state(opening_week_days), answer with 7-toggle
    keyboard.
    """
    from uuid import UUID as _UUID

    from bot.keyboards.admin import AdminWindowSlot30CallbackData
    from bot.states import AdminStates

    async with session_factory() as session:
        await _seed_admin_stack(session)

    sentinel = _UUID(int=0)
    cb_data = AdminWindowSlot30CallbackData(workday_id=sentinel, start_minute=1080)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    state = _make_mock_state({"business_tz": TZ, "picked_start_minute": 600})

    await admin_handlers.admin_openweek_end_cb(callback, cb_data, state)

    state.set_state.assert_called_once_with(AdminStates.opening_week_days)
    data = _state_all_updates(state)
    assert data["picked_end_minute"] == 1080
    assert data["selected_weekdays"] == []
    args, kwargs = callback.message.answer.call_args
    text = args[0] if args else kwargs.get("text", "")
    assert "Шаг 3" in text
    reply_markup = kwargs.get("reply_markup") or (args[1] if len(args) > 1 else None)
    assert reply_markup is not None, "Expected days keyboard reply_markup"


@pytest.mark.asyncio
async def test_admin_openweek_end_cb_state_loss_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[end slot tap] but picked_start_minute missing in state → state.clear()
    + answer 'данные потеряны' + return (mirror admin_window_end_cb).
    """
    from uuid import UUID as _UUID

    from bot.keyboards.admin import AdminWindowSlot30CallbackData

    async with session_factory() as session:
        await _seed_admin_stack(session)

    sentinel = _UUID(int=0)
    cb_data = AdminWindowSlot30CallbackData(workday_id=sentinel, start_minute=1080)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    state = _make_mock_state({"business_tz": TZ})  # no picked_start_minute

    await admin_handlers.admin_openweek_end_cb(callback, cb_data, state)

    state.clear.assert_called_once()
    state.set_state.assert_not_called()
    text = callback_answer_text(callback)
    assert "потеряны" in text


@pytest.mark.asyncio
async def test_admin_openweek_days_cb_toggles_weekday(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[weekday tap] → toggle weekday in selected_weekdays, edit_reply_markup.
    First tap on weekday=0 (Mon) → selected=[0], second tap → [] (deselect).
    """
    from bot.keyboards.admin import AdminOpenWeekCallbackData

    async with session_factory() as session:
        await _seed_admin_stack(session)

    cb_data = AdminOpenWeekCallbackData(weekday=0)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    callback.message.edit_reply_markup = AsyncMock()
    state = _make_mock_state({"selected_weekdays": []})

    await admin_handlers.admin_openweek_days_cb(callback, cb_data, state)

    data = _state_data_passed(state)
    assert data["selected_weekdays"] == [0]
    assert callback.message.edit_reply_markup.called


@pytest.mark.asyncio
async def test_admin_openweek_days_cb_deselect_existing_weekday(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[weekday tap] on already-selected weekday → remove from list, re-render."""
    from bot.keyboards.admin import AdminOpenWeekCallbackData

    async with session_factory() as session:
        await _seed_admin_stack(session)

    cb_data = AdminOpenWeekCallbackData(weekday=2)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    callback.message.edit_reply_markup = AsyncMock()
    state = _make_mock_state({"selected_weekdays": [0, 2, 4]})

    await admin_handlers.admin_openweek_days_cb(callback, cb_data, state)

    data = _state_data_passed(state)
    assert data["selected_weekdays"] == [0, 4]


@pytest.mark.asyncio
async def test_admin_openweek_confirm_cb_no_days_keeps_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[✅ Открыть] with empty selected → callback.answer('выберите хотя бы один
    день', show_alert=True), state NOT cleared (user can toggle and retry).
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_openweek_confirm"
    state = _make_mock_state(
        {
            "picked_start_minute": 600,
            "picked_end_minute": 1080,
            "selected_weekdays": [],
            "business_tz": TZ,
        }
    )

    await admin_handlers.admin_openweek_confirm_cb(callback, state)

    state.clear.assert_not_called()
    callback.answer.assert_called_once()
    call_args = callback.answer.call_args
    text_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("text", "")
    assert "хотя бы один" in text_arg


@pytest.mark.asyncio
async def test_admin_openweek_confirm_cb_state_loss_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[✅ Открыть] but picked_start_minute missing → state.clear + answer
    'данные потеряны' + return.
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_openweek_confirm"
    state = _make_mock_state(
        {
            "picked_end_minute": 1080,
            "selected_weekdays": [0],
            "business_tz": TZ,
        }
    )  # missing picked_start_minute

    await admin_handlers.admin_openweek_confirm_cb(callback, state)

    state.clear.assert_called_once()
    text = callback_answer_text(callback)
    assert "потеряны" in text


@pytest.mark.asyncio
@freeze_time("2026-08-30 14:00:00", tz_offset=0)  # Sunday UTC 14:00 → Moscow 17:00
async def test_admin_openweek_confirm_cb_skips_past_days(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[✅ Открыть] on Sunday with [Mon, Tue, Wed] selected — all 3 are past
    (Mon 24, Tue 25, Wed 26 Aug). All get ❌ 'прошедшая дата', no WorkDay created.
    Mirror /addslots past-date guard (admin.py:291).
    """
    from bot.models import WorkDay

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_openweek_confirm"
    state = _make_mock_state(
        {
            "picked_start_minute": 600,
            "picked_end_minute": 1200,
            "selected_weekdays": [0, 1, 2],  # Mon, Tue, Wed (all past)
            "business_tz": TZ,
        }
    )

    await admin_handlers.admin_openweek_confirm_cb(callback, state)

    text = callback_answer_text(callback)
    assert "прошедшая дата" in text
    assert "Пн" in text and "Вт" in text and "Ср" in text
    # Verify no WorkDay created (all skipped).
    today_local = datetime.now(ZoneInfo(TZ)).date()
    monday = today_local - timedelta(days=today_local.weekday())
    async with session_factory() as session:
        count = 0
        for weekday in [0, 1, 2]:
            wd_date = monday + timedelta(days=weekday)
            wd = await session.scalar(
                select(WorkDay).where(
                    WorkDay.master_id == ctx["master_id"], WorkDay.work_date == wd_date
                )
            )
            count += 1 if wd is not None else 0
    assert count == 0, "No WorkDay should be created for past days"


@pytest.mark.asyncio
@freeze_time("2026-08-25 14:00:00", tz_offset=0)  # Tuesday UTC 14:00 → Moscow 17:00
async def test_admin_openweek_confirm_cb_opens_days(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[✅ Открыть] with 2 selected weekdays (Wed, Fri) → open_workday called
    for Wednesday and Friday of current week (Wed=27, Fri=29 Aug, both future).
    Tuesday Moscow 17:00 → Mon(24)/Tue(25) past, Wed(27)/Fri(29) future.
    """
    from bot.models import WorkDay

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_openweek_confirm"
    state = _make_mock_state(
        {
            "picked_start_minute": 600,  # 10:00
            "picked_end_minute": 1200,  # 20:00
            "selected_weekdays": [2, 4],  # Wed, Fri (both future on Tuesday)
            "business_tz": TZ,
        }
    )

    await admin_handlers.admin_openweek_confirm_cb(callback, state)

    state.clear.assert_called_once()
    text = callback_answer_text(callback)
    assert "✅" in text
    # Compute expected Wed/Fri of frozen week.
    today_local = datetime.now(ZoneInfo(TZ)).date()
    monday = today_local - timedelta(days=today_local.weekday())
    wed_date = monday + timedelta(days=2)
    fri_date = monday + timedelta(days=4)

    async with session_factory() as session:
        wed = await session.scalar(
            select(WorkDay).where(
                WorkDay.master_id == ctx["master_id"],
                WorkDay.work_date == wed_date,
            )
        )
        fri = await session.scalar(
            select(WorkDay).where(
                WorkDay.master_id == ctx["master_id"],
                WorkDay.work_date == fri_date,
            )
        )
    assert wed is not None and wed.is_active
    assert fri is not None and fri.is_active


@pytest.mark.asyncio
@freeze_time("2026-08-25 14:00:00", tz_offset=0)  # Tuesday UTC 14:00
async def test_admin_openweek_confirm_cb_partial_failure_shrinks_lines(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[✅ Открыть] with [Mon, Wed] but open_workday raises WorkDayShrinkError
    for Monday only → summary contains ❌ Mon line AND ✅ Wed line. Both in
    one summary (partial failure is OK — handler continues).
    """
    from unittest.mock import patch

    from bot.services.workday import WorkDayShrinkError

    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_openweek_confirm"
    state = _make_mock_state(
        {
            "picked_start_minute": 600,  # 10:00
            "picked_end_minute": 720,  # 12:00
            "selected_weekdays": [2, 4],  # Wed(2), Fri(4) — both future on Tuesday
            "business_tz": TZ,
        }
    )

    real_open = admin_handlers.open_workday

    async def fake_open(session, master_id, work_date, start_time, end_time, *, business_tz):
        # Compute Monday of frozen week (mirror handler logic).
        today_local = datetime.now(ZoneInfo(business_tz)).date()
        monday = today_local - timedelta(days=today_local.weekday())
        # Fail on Wednesday (weekday=2) of frozen week — Wed selected, raise
        # WorkDayShrinkError to simulate "есть бронь"; Fri passes through.
        wed_fail = monday + timedelta(days=2)
        if work_date == wed_fail:
            raise WorkDayShrinkError("active")
        return await real_open(
            session, master_id, work_date, start_time, end_time, business_tz=business_tz
        )

    with patch.object(admin_handlers, "open_workday", side_effect=fake_open):
        await admin_handlers.admin_openweek_confirm_cb(callback, state)

    text = callback_answer_text(callback)
    assert "Ср" in text and "Пт" in text
    assert "❌" in text and "✅" in text


@pytest.mark.asyncio
async def test_admin_openweek_cancel_cb_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[❌ Отмена] (string F.data == 'admin_openweek_cancel') → state.clear +
    answer 'Открытие недели отменено'.
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_openweek_cancel"
    state = _make_mock_state()

    await admin_handlers.admin_openweek_cancel_cb(callback, state)

    state.clear.assert_called_once()
    text = callback_answer_text(callback)
    assert "Открытие недели отменено" in text


# ============================================================
# /closeday handlers (Session 5.26)
# ============================================================


@pytest.mark.asyncio
async def test_cmd_closeday_shows_calendar(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """/closeday → state.clear + set_state(closing_day_date) + reply with
    SimpleCalendar reply_markup.
    """
    from bot.states import AdminStates

    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(ADMIN_TG_ID, text="/closeday")
    state = _make_mock_state()

    await admin_handlers.cmd_closeday(msg, state)

    state.clear.assert_called_once()
    state.set_state.assert_called_once_with(AdminStates.closing_day_date)
    args, kwargs = msg.answer.call_args
    text = args[0] if args else kwargs.get("text", "")
    assert "Выберите дату" in text
    reply_markup = kwargs.get("reply_markup") or (args[1] if len(args) > 1 else None)
    assert reply_markup is not None, "Expected calendar reply_markup"


@pytest.mark.asyncio
async def test_cmd_closeday_master_not_found(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """/closeday without master → '❌ Мастер не найден'."""
    async with session_factory():
        pass

    msg = _make_message(ADMIN_TG_ID, text="/closeday")
    state = _make_mock_state()

    await admin_handlers.cmd_closeday(msg, state)

    text = _answer_text(msg)
    assert "Мастер не найден" in text
    state.set_state.assert_not_called()


@pytest.mark.asyncio
async def test_admin_closeday_entry_cb_sets_state_and_shows_calendar(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[📅 Закрыть день] tap → state.clear + set_state(closing_day_date) +
    answer with calendar."""
    from bot.states import AdminStates

    async with session_factory() as session:
        await _seed_admin_stack(session)

    from bot.keyboards.admin import AdminCloseDayEntryCallbackData

    cb_data = AdminCloseDayEntryCallbackData()
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    state = _make_mock_state()

    await admin_handlers.admin_closeday_entry_cb(callback, state)

    state.clear.assert_called_once()
    state.set_state.assert_called_once_with(AdminStates.closing_day_date)
    args, kwargs = callback.message.answer.call_args
    text = args[0] if args else kwargs.get("text", "")
    assert "Выберите дату" in text
    reply_markup = kwargs.get("reply_markup") or (args[1] if len(args) > 1 else None)
    assert reply_markup is not None, "Expected calendar reply_markup"


@pytest.mark.asyncio
async def test_admin_closeday_calendar_cb_no_workday_redirects(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Calendar day-select on a date with NO WorkDay → edit_text 'не открыт,
    нечего закрывать' + state.clear (4-branch path 1).
    """
    from unittest.mock import patch

    from aiogram_calendar import SimpleCalendarCallback
    from aiogram_calendar.schemas import SimpleCalAct

    async with session_factory() as session:
        await _seed_admin_stack(session)

    future_date = datetime.now(UTC) + timedelta(days=30)
    cal_cb_data = SimpleCalendarCallback(
        act=SimpleCalAct.day,
        year=future_date.year,
        month=future_date.month,
        day=future_date.day,
    )
    callback = _make_callback(ADMIN_TG_ID, callback_data=cal_cb_data)
    callback.message.edit_text = AsyncMock()
    state = _make_mock_state({"business_tz": TZ})

    with patch(
        "aiogram_calendar.SimpleCalendar.process_selection",
        return_value=(True, future_date),
    ):
        await admin_handlers.admin_closeday_calendar_cb(callback, cal_cb_data, state)

    state.clear.assert_called_once()
    assert callback.message.edit_text.called
    text = str(callback.message.edit_text.call_args.args[0])
    assert "не открыт" in text


@pytest.mark.asyncio
async def test_admin_closeday_calendar_cb_already_closed(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Calendar day-select on is_active=False workday → edit_text 'уже закрыт'
    + state.clear (4-branch path 2).
    """
    from unittest.mock import patch

    from aiogram_calendar import SimpleCalendarCallback
    from aiogram_calendar.schemas import SimpleCalAct

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        future = (datetime.now(UTC) + timedelta(days=15)).date()
        await _seed_workday(session, ctx=ctx, work_date=future, is_active=False)

    future_date = datetime.combine(future, datetime.min.time())
    cal_cb_data = SimpleCalendarCallback(
        act=SimpleCalAct.day,
        year=future_date.year,
        month=future_date.month,
        day=future_date.day,
    )
    callback = _make_callback(ADMIN_TG_ID, callback_data=cal_cb_data)
    callback.message.edit_text = AsyncMock()
    state = _make_mock_state({"business_tz": TZ})

    with patch(
        "aiogram_calendar.SimpleCalendar.process_selection",
        return_value=(True, future_date),
    ):
        await admin_handlers.admin_closeday_calendar_cb(callback, cal_cb_data, state)

    state.clear.assert_called_once()
    text = str(callback.message.edit_text.call_args.args[0])
    assert "уже закрыт" in text


@pytest.mark.asyncio
async def test_admin_closeday_calendar_cb_no_bookings_closes_immediately(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Calendar day-select on is_active=True with NO active bookings →
    close_workday_with_cancellations called, answer '✅ закрыт. Активных
    записей не было.' + state.clear (4-branch path 3).
    """
    from unittest.mock import patch

    from aiogram_calendar import SimpleCalendarCallback
    from aiogram_calendar.schemas import SimpleCalAct

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        future = (datetime.now(UTC) + timedelta(days=16)).date()
        await _seed_workday(session, ctx=ctx, work_date=future, is_active=True)

    future_date = datetime.combine(future, datetime.min.time())
    cal_cb_data = SimpleCalendarCallback(
        act=SimpleCalAct.day,
        year=future_date.year,
        month=future_date.month,
        day=future_date.day,
    )
    callback = _make_callback(ADMIN_TG_ID, callback_data=cal_cb_data)
    callback.message.answer = AsyncMock()
    state = _make_mock_state({"business_tz": TZ})

    with patch(
        "aiogram_calendar.SimpleCalendar.process_selection",
        return_value=(True, future_date),
    ):
        await admin_handlers.admin_closeday_calendar_cb(callback, cal_cb_data, state)

    state.clear.assert_called_once()
    text = callback_answer_text(callback)
    assert "✅" in text and "не было" in text

    # Verify workday is now closed.
    async with session_factory() as session:
        from bot.models import WorkDay as _WD

        wd = await session.scalar(
            select(_WD).where(_WD.master_id == ctx["master_id"], _WD.work_date == future)
        )
    assert wd is not None and wd.is_active is False


@pytest.mark.asyncio
async def test_admin_closeday_calendar_cb_with_bookings_shows_confirm(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Calendar day-select on is_active=True with active bookings → set_state
    (closing_day_confirm) + edit_text list of bookings + admin_closeday_
    confirm_keyboard (4-branch path 4).
    """
    from unittest.mock import patch

    from aiogram_calendar import SimpleCalendarCallback
    from aiogram_calendar.schemas import SimpleCalAct

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        future = (datetime.now(UTC) + timedelta(days=17)).date()
        workday = await _seed_workday(session, ctx=ctx, work_date=future, is_active=True)
        slot = await _seed_slot(
            session, master_id=ctx["master_id"], slot_date=future, hour=14, status="booked"
        )
        future_local = datetime.combine(future, datetime.min.time(), ZoneInfo(TZ))
        await _seed_booking(
            session,
            ctx=ctx,
            slot=slot,
            start_at_utc_naive=future_local.replace(hour=14).astimezone(UTC).replace(tzinfo=None),
            status="confirmed",
        )

    future_date = datetime.combine(future, datetime.min.time())
    cal_cb_data = SimpleCalendarCallback(
        act=SimpleCalAct.day,
        year=future_date.year,
        month=future_date.month,
        day=future_date.day,
    )
    callback = _make_callback(ADMIN_TG_ID, callback_data=cal_cb_data)
    callback.message.edit_text = AsyncMock()
    state = _make_mock_state({"business_tz": TZ})

    with patch(
        "aiogram_calendar.SimpleCalendar.process_selection",
        return_value=(True, future_date),
    ):
        await admin_handlers.admin_closeday_calendar_cb(callback, cal_cb_data, state)

    from bot.states import AdminStates

    state.set_state.assert_called_once_with(AdminStates.closing_day_confirm)
    data = _state_data_passed(state)
    assert data["closing_day_workday_id"] == str(workday.id)
    assert callback.message.edit_text.called
    text = str(callback.message.edit_text.call_args.args[0])
    assert "Закрыть день" in text and "отменить" in text.lower()


@pytest.mark.asyncio
async def test_admin_closeday_calendar_cb_cancel_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Calendar 'Отмена' button (SimpleCalAct.cancel) → state.clear + edit_text
    'Закрытие дня отменено'.
    """
    from unittest.mock import patch

    from aiogram_calendar import SimpleCalendarCallback
    from aiogram_calendar.schemas import SimpleCalAct

    async with session_factory() as session:
        await _seed_admin_stack(session)

    cal_cb_data = SimpleCalendarCallback(act=SimpleCalAct.cancel)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cal_cb_data)
    callback.message.edit_text = AsyncMock()
    state = _make_mock_state({"business_tz": TZ})

    with patch(
        "aiogram_calendar.SimpleCalendar.process_selection",
        return_value=(False, None),
    ):
        await admin_handlers.admin_closeday_calendar_cb(callback, cal_cb_data, state)

    state.clear.assert_called_once()
    text = str(callback.message.edit_text.call_args.args[0])
    assert "Закрытие дня отменено" in text


@pytest.mark.asyncio
async def test_admin_closeday_confirm_cb_closes_and_notifies(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[✅ Да, отменить записи] → close_workday_with_cancellations, send_message
    per cancelled booking, remove_jobs_for_booking per booking, summary shows
    count + notified_count.
    """
    from unittest.mock import AsyncMock as _AsyncMock
    from unittest.mock import patch

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        future = (datetime.now(UTC) + timedelta(days=18)).date()
        workday = await _seed_workday(session, ctx=ctx, work_date=future, is_active=True)
        slot = await _seed_slot(
            session, master_id=ctx["master_id"], slot_date=future, hour=14, status="booked"
        )
        future_local = datetime.combine(future, datetime.min.time(), ZoneInfo(TZ))
        booking = await _seed_booking(
            session,
            ctx=ctx,
            slot=slot,
            start_at_utc_naive=future_local.replace(hour=14).astimezone(UTC).replace(tzinfo=None),
            status="confirmed",
        )

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_closeday_confirm"
    callback.bot.send_message = _AsyncMock()
    state = _make_mock_state(
        {
            "business_tz": TZ,
            "closing_day_workday_id": str(workday.id),
        }
    )

    scheduler = MagicMock()
    with patch("bot.handlers.admin.remove_jobs_for_booking") as rm_jobs:
        await admin_handlers.admin_closeday_confirm_cb(callback, state, scheduler)

    state.clear.assert_called_once()
    text = callback_answer_text(callback)
    assert "✅" in text and "закрыт" in text
    assert "отменено" in text.lower()
    # send_message called for client notification.
    assert callback.bot.send_message.called
    # remove_jobs_for_booking called once per cancelled booking.
    rm_jobs.assert_called_once()
    call_args = rm_jobs.call_args
    assert call_args.args[0] == scheduler
    assert call_args.args[1] == booking.id

    # Verify workday is now closed.
    async with session_factory() as session:
        from bot.models import WorkDay as _WD

        wd = await session.scalar(
            select(_WD).where(_WD.master_id == ctx["master_id"], _WD.work_date == future)
        )
    assert wd is not None and wd.is_active is False


@pytest.mark.asyncio
async def test_admin_closeday_confirm_cb_state_loss_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[✅ Да] but closing_day_workday_id missing in state → state.clear +
    answer 'данные потеряны'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_closeday_confirm"
    state = _make_mock_state({"business_tz": TZ})  # no workday_id

    scheduler = MagicMock()
    await admin_handlers.admin_closeday_confirm_cb(callback, state, scheduler)

    state.clear.assert_called_once()
    text = callback_answer_text(callback)
    assert "потеряны" in text


@pytest.mark.asyncio
async def test_admin_closeday_cancel_cb_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[❌ Не закрывать] (string F.data == 'admin_closeday_cancel') →
    state.clear + answer 'Закрытие дня отменено'.
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_closeday_cancel"
    state = _make_mock_state()

    await admin_handlers.admin_closeday_cancel_cb(callback, state)

    state.clear.assert_called_once()
    text = callback_answer_text(callback)
    assert "отменено" in text.lower()
