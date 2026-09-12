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
from aiogram.types import InlineKeyboardMarkup, Message, User
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
# cmd_openday — 11 branches (lines 354-427)
# ============================================================


@pytest.mark.asyncio
async def test_cmd_openday_non_admin_silent_ignore(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Non-admin → _is_admin False → return silently (no answer, no DB write)."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=NON_ADMIN_TG_ID, text=f"/openday {tomorrow} 11:00 18:00")
    await admin_handlers.cmd_openday(msg, _make_command(f"{tomorrow} 11:00 18:00"))

    assert _answer_call_count(msg) == 0


@pytest.mark.asyncio
async def test_cmd_openday_wrong_args_count_shows_format(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """args != 3 → 'Формат: /openday ГГГГ-ММ-ДД ЧЧ:ММ ЧЧ:ММ' hint."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/openday 2026-09-17 11:00")
    await admin_handlers.cmd_openday(msg, _make_command("2026-09-17 11:00"))

    text = _answer_text(msg)
    assert "Формат:" in text
    assert "/openday" in text


@pytest.mark.asyncio
async def test_cmd_openday_invalid_iso_date_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Bad date → 'Неверная дата. Формат: ГГГГ-ММ-ДД'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/openday 17-09-2026 11:00 18:00")
    await admin_handlers.cmd_openday(msg, _make_command("17-09-2026 11:00 18:00"))

    text = _answer_text(msg)
    assert "Неверная дата" in text


@pytest.mark.asyncio
async def test_cmd_openday_invalid_time_format_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Bad time format (not HH:MM) → 'Время должно быть ЧЧ:ММ'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/openday {tomorrow} 11-00 18:00")
    await admin_handlers.cmd_openday(msg, _make_command(f"{tomorrow} 11-00 18:00"))

    text = _answer_text(msg)
    assert "Время должно быть ЧЧ:ММ" in text


@pytest.mark.asyncio
async def test_cmd_openday_master_not_found_shows_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Admin telegram_id not linked to Master → 'Мастер не найден'.

    No _seed_admin_stack → _resolve_master_and_business returns None.
    """
    # Deliberately NO _seed_admin_stack — admin_id resolves to no master.
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/openday {tomorrow} 11:00 18:00")
    await admin_handlers.cmd_openday(msg, _make_command(f"{tomorrow} 11:00 18:00"))

    text = _answer_text(msg)
    assert "Мастер не найден" in text


@pytest.mark.asyncio
async def test_cmd_openday_past_date_rejected(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Past date → 'Нельзя открыть день в прошлом'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/openday {yesterday} 11:00 18:00")
    await admin_handlers.cmd_openday(msg, _make_command(f"{yesterday} 11:00 18:00"))

    text = _answer_text(msg)
    assert "Нельзя открыть день в прошлом" in text


@pytest.mark.asyncio
async def test_cmd_openday_value_error_from_open_workday(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """open_workday raises ValueError (e.g. end <= start) → '❌ <exc>'."""
    from unittest.mock import AsyncMock, patch

    async with session_factory() as session:
        await _seed_admin_stack(session)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/openday {tomorrow} 11:00 18:00")

    with patch(
        "bot.handlers.admin.open_workday",
        new_callable=AsyncMock,
        side_effect=ValueError("end must be after start"),
    ):
        await admin_handlers.cmd_openday(msg, _make_command(f"{tomorrow} 11:00 18:00"))

    text = _answer_text(msg)
    assert "❌" in text
    assert "end must be after start" in text


@pytest.mark.asyncio
async def test_cmd_openday_workday_shrink_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """open_workday raises WorkDayShrinkError → 'Нельзя сократить окно' hint."""
    from unittest.mock import AsyncMock, patch

    from bot.services.workday import WorkDayShrinkError

    async with session_factory() as session:
        await _seed_admin_stack(session)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/openday {tomorrow} 11:00 12:00")

    with patch(
        "bot.handlers.admin.open_workday",
        new_callable=AsyncMock,
        side_effect=WorkDayShrinkError("active booking blocks shrink"),
    ):
        await admin_handlers.cmd_openday(msg, _make_command(f"{tomorrow} 11:00 12:00"))

    text = _answer_text(msg)
    assert "Нельзя сократить окно" in text


@pytest.mark.asyncio
async def test_cmd_openday_sqlalchemy_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """open_workday raises SQLAlchemyError → 'Ошибка БД' (S2 fix, code-review 5.1)."""
    from unittest.mock import AsyncMock, patch

    from sqlalchemy.exc import SQLAlchemyError

    async with session_factory() as session:
        await _seed_admin_stack(session)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/openday {tomorrow} 11:00 18:00")

    with patch(
        "bot.handlers.admin.open_workday",
        new_callable=AsyncMock,
        side_effect=SQLAlchemyError("simulated DB error"),
    ):
        await admin_handlers.cmd_openday(msg, _make_command(f"{tomorrow} 11:00 18:00"))

    text = _answer_text(msg)
    assert "Ошибка БД" in text


@pytest.mark.asyncio
async def test_cmd_openday_happy_new_workday(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Happy path — open new workday → '✅ День открыт' + WorkDay row in DB."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/openday {tomorrow} 11:00 18:00")
    await admin_handlers.cmd_openday(msg, _make_command(f"{tomorrow} 11:00 18:00"))

    text = _answer_text(msg)
    assert "✅ День открыт" in text
    assert "11:00" in text and "18:00" in text
    # No "день был закрыт" suffix — fresh new day
    assert "открыт заново" not in text

    async with session_factory() as verify:
        from bot.models import WorkDay

        wd = (await verify.execute(select(WorkDay))).scalars().all()
        assert len(wd) == 1
        assert wd[0].is_active is True


@pytest.mark.asyncio
async def test_cmd_openday_happy_reopens_closed_workday(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Re-open existing inactive workday → '✅ День открыт' + 'открыт заново' suffix.

    F1 fix (Session 5.18, variant B): captures was_closed BEFORE open_workday
    re-opens the day. After open_workday, is_active is always True → without
    capture, the 'открыт заново' suffix never showed.
    """
    from datetime import time as dt_time

    from bot.models import WorkDay

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        master_id = ctx["master_id"]
        # Pre-existing INACTIVE workday for tomorrow
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        session.add(
            WorkDay(
                master_id=master_id,
                work_date=tomorrow,
                start_time=dt_time(9, 0),
                end_time=dt_time(17, 0),
                is_active=False,
            )
        )
        await session.commit()

    msg = _make_message(user_id=ADMIN_TG_ID, text=f"/openday {tomorrow} 11:00 18:00")
    await admin_handlers.cmd_openday(msg, _make_command(f"{tomorrow} 11:00 18:00"))

    text = _answer_text(msg)
    assert "✅ День открыт" in text
    assert "день был закрыт — открыт заново" in text

    async with session_factory() as verify:
        wd = (await verify.execute(select(WorkDay))).scalars().all()
        assert len(wd) == 1
        assert wd[0].is_active is True


# ============================================================
# admin_openday_start_msg — 5 branches (lines 1248-1286)
# FSM: StateFilter(AdminStates.opening_workday_start), F.text, ~"/"
# ============================================================


@pytest.mark.asyncio
async def test_admin_openday_start_msg_non_admin_silent(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Non-admin text in opening_workday_start state → silent return (no answer)."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=NON_ADMIN_TG_ID, text="11:00")
    state = _make_mock_state({"selected_date": "2099-01-01"})

    await admin_handlers.admin_openday_start_msg(msg, state)

    assert _answer_call_count(msg) == 0
    state.clear.assert_not_called()
    state.set_state.assert_not_called()


@pytest.mark.asyncio
async def test_admin_openday_start_msg_no_selected_date_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """State has no selected_date (e.g. FSM entered without cmd_openday) → state.clear + hint."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="11:00")
    state = _make_mock_state({})  # no selected_date key

    await admin_handlers.admin_openday_start_msg(msg, state)

    text = _answer_text(msg)
    assert "Дата не выбрана" in text
    assert "/menu" in text
    state.clear.assert_awaited_once()
    state.set_state.assert_not_called()


@pytest.mark.asyncio
async def test_admin_openday_start_msg_bad_stored_date_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """selected_date in state is not ISO-parseable (stale/corrupted) → state.clear + hint."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="11:00")
    state = _make_mock_state({"selected_date": "not-a-date"})

    await admin_handlers.admin_openday_start_msg(msg, state)

    text = _answer_text(msg)
    assert "Ошибка даты" in text
    state.clear.assert_awaited_once()
    state.set_state.assert_not_called()


@pytest.mark.asyncio
async def test_admin_openday_start_msg_bad_time_format_keeps_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Bad time format (not HH:MM / ЧЧ.ММ / ЧЧ,ММ) → error hint, state STAYS (admin can retry)."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="25")
    state = _make_mock_state({"selected_date": "2099-01-01"})

    await admin_handlers.admin_openday_start_msg(msg, state)

    text = _answer_text(msg)
    assert "Формат ЧЧ:ММ" in text
    state.clear.assert_not_called()
    state.set_state.assert_not_called()
    state.update_data.assert_not_called()


@pytest.mark.asyncio
async def test_admin_openday_start_msg_happy_transitions_to_end(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Happy: '11:00' → state.update_data(start_time='11:00') + set_state(opening_workday_end)
    + answer 'Начало: 11:00 / Введите время окончания'.
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="11:00")
    state = _make_mock_state({"selected_date": "2099-01-01"})

    await admin_handlers.admin_openday_start_msg(msg, state)

    text = _answer_text(msg)
    assert text.startswith("Начало: <b>11:00</b>")
    assert "Введите время окончания" in text

    state.update_data.assert_awaited_once()
    update_args, update_kwargs = state.update_data.call_args
    update_payload = update_args[0] if update_args else update_kwargs
    # time(11, 0).isoformat() == "11:00:00" (with seconds)
    assert update_payload["start_time"] == "11:00:00"

    state.set_state.assert_awaited_once_with(
        admin_handlers.AdminStates.opening_workday_end
    )
    state.clear.assert_not_called()


# ============================================================
# admin_addslots_cb — 4 branches (lines 925-952)
# ============================================================


@pytest.mark.asyncio
async def test_admin_addslots_cb_non_admin_silent(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Non-admin tap → _is_admin_callback False → callback.answer() + return.

    No state.clear, no set_state — admin flow not entered.
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(NON_ADMIN_TG_ID)
    state = _make_mock_state()

    await admin_handlers.admin_addslots_cb(callback, state)

    callback.answer.assert_called_once()
    state.clear.assert_not_called()
    state.set_state.assert_not_called()


@pytest.mark.asyncio
async def test_admin_addslots_cb_master_not_found(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Admin with no Master row → '❌ Мастер не найден' alert + return.

    No _seed_admin_stack → _resolve_master_and_business returns None.
    """
    # No _seed_admin_stack deliberately
    callback = _make_callback(ADMIN_TG_ID)
    state = _make_mock_state()

    await admin_handlers.admin_addslots_cb(callback, state)

    callback.answer.assert_called_once()
    args, _ = callback.answer.call_args
    assert "Мастер не найден" in str(args)
    state.clear.assert_not_called()
    state.set_state.assert_not_called()


@pytest.mark.asyncio
async def test_admin_addslots_cb_happy_enters_adding_slots_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Happy: admin tap → state.clear + set_state(adding_slots_date) +
    calendar keyboard + callback.answer (dismiss loading spinner).
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    state = _make_mock_state()

    await admin_handlers.admin_addslots_cb(callback, state)

    state.clear.assert_called_once()
    state.set_state.assert_called_once_with(admin_handlers.AdminStates.adding_slots_date)

    # Message shown with calendar keyboard
    callback.message.answer.assert_called_once()
    answer_args, answer_kwargs = callback.message.answer.call_args
    assert "Выберите дату" in str(answer_args[0])
    assert "reply_markup" in answer_kwargs

    callback.answer.assert_called_once()


@pytest.mark.asyncio
async def test_admin_addslots_cb_message_none_skips_calendar(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Edge: callback.message is None (rare — e.g. inline mode) → skip
    calendar answer, still set_state + callback.answer.
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    callback.message = None  # simulate missing message
    state = _make_mock_state()

    await admin_handlers.admin_addslots_cb(callback, state)

    state.clear.assert_called_once()
    state.set_state.assert_called_once_with(admin_handlers.AdminStates.adding_slots_date)
    # No message.answer call (message is None)
    callback.answer.assert_called_once()


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
    """Future booking → '📅 Ближайшие записи:' (now shows all upcoming, not just 7 days)."""
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
    assert "Ближайшие записи" in text
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
    """No future bookings → 'Ближайших записей нет.'"""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/week")
    await admin_handlers.cmd_week(msg)

    text = _answer_text(msg)
    assert "Ближайших записей нет" in text


@pytest.mark.asyncio
async def test_cmd_week_shows_far_future_booking_beyond_7_days(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Booking 30 days ahead (beyond old 7-day limit) → still appears in /week.

    New behavior (get_all_future_bookings): no upper bound. Old behavior
    (get_week_bookings days_ahead=7) hid bookings past day 7. Master wants
    to see ALL upcoming bookings, even if weeks ahead.
    """
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        # 30 days ahead at 15:00 Moscow — well beyond old 7-day window
        in_30_days_local = (datetime.now(ZoneInfo(TZ)) + timedelta(days=30)).replace(
            hour=15, minute=0, second=0, microsecond=0
        )
        slot = await _seed_slot(
            session,
            master_id=ctx["master_id"],
            slot_date=in_30_days_local.date(),
            hour=15,
            status="open",
        )
        await _seed_booking(
            session,
            ctx=ctx,
            slot=slot,
            start_at_utc_naive=_local_to_utc_naive(in_30_days_local),
            status="confirmed",
        )

    msg = _make_message(user_id=ADMIN_TG_ID, text="/week")
    await admin_handlers.cmd_week(msg)

    text = _answer_text(msg)
    assert "Ближайшие записи" in text, f"Header should be 'Ближайшие записи'; got: {text!r}"
    assert "Паша" in text, f"Far-future booking must appear (no 7-day limit); got: {text!r}"


# ============================================================
# 5.60 P3 — admin_week_cb: edit_text vs answer (variant A)
# ============================================================


@pytest.mark.asyncio
async def test_admin_week_cb_state_none_uses_edit_text(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """5.60 P3 (variant A): state is None (admin in main menu) → edit_text
    replaces menu message with «Ближайшие записи» in same message. Avoids
    chat clutter — old «Ближайшие записи» doesn't accumulate as sediment.
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    state = _make_mock_state({})
    state.get_state = AsyncMock(return_value=None)

    await admin_handlers.admin_week_cb(callback, state)

    # edit_text called (preferred path), answer NOT called (no fallback needed).
    assert callback.message.edit_text.called, "edit_text should be called when state is None"
    assert not callback.message.answer.called, (
        "answer must NOT be called when edit_text succeeds"
    )
    args, _ = callback.message.edit_text.call_args
    text = str(args[0])
    assert "Ближайших записей нет" in text, (
        f"No bookings seeded → empty message; got: {text!r}"
    )


@pytest.mark.asyncio
async def test_admin_week_cb_state_active_uses_answer(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """5.60 P3 (variant A): state is not None (admin mid-flow, e.g.
    opening_week_days) → answer in new message. Avoids replacing flow
    message (step 3 keyboard) with «Ближайшие записи» — admin keeps the
    flow keyboard visible and can continue toggling days.
    """
    from bot.states import AdminStates

    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    state = _make_mock_state({})
    state.get_state = AsyncMock(return_value=AdminStates.opening_week_days)

    await admin_handlers.admin_week_cb(callback, state)

    # answer called (mid-flow path), edit_text NOT called.
    assert callback.message.answer.called, (
        "answer should be called when state is active (mid-flow)"
    )
    assert not callback.message.edit_text.called, (
        "edit_text must NOT be called mid-flow — would replace flow keyboard"
    )
    args, _ = callback.message.answer.call_args
    text = str(args[0])
    assert "Ближайших записей нет" in text


@pytest.mark.asyncio
async def test_admin_week_cb_edit_text_falls_back_to_answer_on_bad_request(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """5.60 P3 (variant A): state is None but edit_text raises
    TelegramBadRequest (message older than 48h or deleted) → answer in new
    message as fallback. No silent failure — admin still gets the list.
    """
    from aiogram.exceptions import TelegramBadRequest

    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    # Force edit_text to raise — simulates >48h or deleted message.
    # TelegramBadRequest requires (method, message) — pass MagicMock as method.
    bad_request = TelegramBadRequest(
        method=MagicMock(),
        message="Bad Request: message to edit not found",
    )
    callback.message.edit_text = AsyncMock(side_effect=bad_request)
    state = _make_mock_state({})
    state.get_state = AsyncMock(return_value=None)

    await admin_handlers.admin_week_cb(callback, state)

    # Both called: edit_text attempted (raised), answer fallback succeeds.
    assert callback.message.edit_text.called, "edit_text should be attempted first"
    assert callback.message.answer.called, (
        "answer fallback should be called after TelegramBadRequest"
    )
    args, _ = callback.message.answer.call_args
    text = str(args[0])
    assert "Ближайших записей нет" in text


@pytest.mark.asyncio
async def test_cmd_week_ignores_past_bookings(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Past booking (yesterday) must NOT appear in /week.

    get_all_future_bookings: window [start_of_today_utc, +∞). Past bookings
    (start_at < today) excluded. Edge case: booking yesterday at 23:00 with
    status='confirmed' must not leak.
    """
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        # Yesterday at 15:00 Moscow
        yesterday_local = (datetime.now(ZoneInfo(TZ)) - timedelta(days=1)).replace(
            hour=15, minute=0, second=0, microsecond=0
        )
        slot = await _seed_slot(
            session,
            master_id=ctx["master_id"],
            slot_date=yesterday_local.date(),
            hour=15,
            status="open",
        )
        await _seed_booking(
            session,
            ctx=ctx,
            slot=slot,
            start_at_utc_naive=_local_to_utc_naive(yesterday_local),
            status="confirmed",
        )

    msg = _make_message(user_id=ADMIN_TG_ID, text="/week")
    await admin_handlers.cmd_week(msg)

    text = _answer_text(msg)
    assert "Ближайших записей нет" in text, f"Past booking must NOT appear; got: {text!r}"


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
async def test_cmd_services_unknown_sub_shows_format(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """/services unknown_sub (args[0] not in {add,list,del}) → format hint.

    Renamed from test_cmd_services_wrong_first_arg_shows_format (B.11): 'list' and
    'del' are now valid sub-commands, so we use a truly unknown sub to test the
    fallback format hint branch.
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services unknown_sub")
    await admin_handlers.cmd_services(msg, _make_command("unknown_sub"))

    text = _answer_text(msg)
    assert "Формат" in text
    assert "/services add" in text
    assert "/services list" in text
    assert "/services del" in text


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
# cmd_services — B.11: /services list + /services del
# (8 new tests; 1 existing test adapted — see test_cmd_services_unknown_sub_shows_format)
# ============================================================


@pytest.mark.asyncio
async def test_cmd_services_list_empty(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """/services list без активных услуг → 'У вас нет активных услуг'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services list")
    await admin_handlers.cmd_services(msg, _make_command("list"))

    text = _answer_text(msg)
    assert "У вас нет активных услуг" in text
    assert "/services add" in text


@pytest.mark.asyncio
async def test_cmd_services_list_with_services(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """/services list с 3 услугами → 'Услуги (3):\\n1. ...\\n2. ...\\n3. ...'."""
    async with session_factory() as session:
        seed = await _seed_admin_stack(session)
        for name, duration in (("Стрижка", 60), ("Окрашивание", 120), ("Укладка", 30)):
            session.add(
                Service(
                    business_id=seed["business_id"],
                    name=name,
                    duration_minutes=duration,
                    is_active=True,
                )
            )
        await session.commit()

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services list")
    await admin_handlers.cmd_services(msg, _make_command("list"))

    text = _answer_text(msg)
    assert "Услуги (3):" in text
    assert "1. Стрижка — 60 мин" in text
    assert "2. Окрашивание — 120 мин" in text
    assert "3. Укладка — 30 мин" in text
    # price nullable → no ₽ suffix
    assert "₽" not in text


@pytest.mark.asyncio
async def test_cmd_services_list_excludes_inactive(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """/services list не показывает soft-deleted (is_active=False)."""
    async with session_factory() as session:
        seed = await _seed_admin_stack(session)
        session.add(
            Service(
                business_id=seed["business_id"],
                name="Активная",
                duration_minutes=60,
                is_active=True,
            )
        )
        session.add(
            Service(
                business_id=seed["business_id"],
                name="Удалённая",
                duration_minutes=45,
                is_active=False,
            )
        )
        await session.commit()

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services list")
    await admin_handlers.cmd_services(msg, _make_command("list"))

    text = _answer_text(msg)
    assert "Услуги (1):" in text
    assert "Активная" in text
    assert "Удалённая" not in text


@pytest.mark.asyncio
async def test_cmd_services_del_happy_no_bookings(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """/services del Стрижка — нет active bookings → UPDATE is_active=False, '✅ Услуга удалена'."""
    async with session_factory() as session:
        seed = await _seed_admin_stack(session)
        svc = Service(
            business_id=seed["business_id"], name="Стрижка", duration_minutes=60, is_active=True
        )
        session.add(svc)
        await session.commit()
        svc_id = svc.id

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services del Стрижка")
    await admin_handlers.cmd_services(msg, _make_command("del Стрижка"))

    text = _answer_text(msg)
    assert "✅ Услуга удалена" in text
    assert "Стрижка" in text

    async with session_factory() as verify:
        svc_after = (await verify.execute(select(Service).where(Service.id == svc_id))).scalar_one()
        assert svc_after.is_active is False


@pytest.mark.asyncio
async def test_cmd_services_del_blocked_with_active_bookings(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """/services del с active bookings → '❌ Сначала отмените N запись/записи/записей'."""
    async with session_factory() as session:
        seed = await _seed_admin_stack(session)
        svc = Service(
            business_id=seed["business_id"], name="Стрижка", duration_minutes=60, is_active=True
        )
        session.add(svc)
        await session.flush()
        # 2 active bookings + 1 cancelled (cancelled must NOT block).
        for status in ("confirmed", "transferred", "cancelled"):
            session.add(
                Booking(
                    business_id=seed["business_id"],
                    master_id=seed["master_id"],
                    client_id=seed["client_id"],
                    service_id=svc.id,
                    service_title_snapshot="Стрижка",
                    service_price_snapshot=None,
                    client_name_snapshot="Паша",
                    start_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
                    end_at=datetime(2026, 10, 1, 13, 0, tzinfo=UTC),
                    status=status,
                )
            )
        await session.commit()

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services del Стрижка")
    await admin_handlers.cmd_services(msg, _make_command("del Стрижка"))

    text = _answer_text(msg)
    assert "Сначала отмените" in text
    assert "2 запис" in text  # 2 → "2 записи"
    assert "Стрижка" in text

    async with session_factory() as verify:
        svc_after = (await verify.execute(select(Service))).scalar_one()
        assert svc_after.is_active is True  # NOT deactivated


@pytest.mark.asyncio
async def test_cmd_services_del_not_found(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """/services del Неизвестная → '❌ Услуга «Неизвестная» не найдена'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services del Неизвестная")
    await admin_handlers.cmd_services(msg, _make_command("del Неизвестная"))

    text = _answer_text(msg)
    assert "не найдена" in text
    assert "Неизвестная" in text


@pytest.mark.asyncio
async def test_cmd_services_del_already_inactive_idempotent(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """/services del Удалённая — уже is_active=False → 'ℹ️ Услуга «X» уже удалена'."""
    async with session_factory() as session:
        seed = await _seed_admin_stack(session)
        session.add(
            Service(
                business_id=seed["business_id"],
                name="Удалённая",
                duration_minutes=45,
                is_active=False,
            )
        )
        await session.commit()

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services del Удалённая")
    await admin_handlers.cmd_services(msg, _make_command("del Удалённая"))

    text = _answer_text(msg)
    assert "уже удалена" in text
    assert "Удалённая" in text
    assert "✅" not in text


@pytest.mark.asyncio
async def test_cmd_services_del_no_name_shows_format(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """/services del без аргумента имени → format hint '❌ Формат: /services del НАЗВАНИЕ'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services del")
    await admin_handlers.cmd_services(msg, _make_command("del"))

    text = _answer_text(msg)
    assert "Формат" in text
    assert "/services del" in text


@pytest.mark.asyncio
async def test_cmd_services_del_underscore_decodes_to_space(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """/services del Стрижка_мужская → name 'Стрижка мужская' (mirror of add)."""
    async with session_factory() as session:
        seed = await _seed_admin_stack(session)
        session.add(
            Service(
                business_id=seed["business_id"],
                name="Стрижка мужская",
                duration_minutes=60,
                is_active=True,
            )
        )
        await session.commit()

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services del Стрижка_мужская")
    await admin_handlers.cmd_services(msg, _make_command("del Стрижка_мужская"))

    text = _answer_text(msg)
    assert "✅ Услуга удалена" in text
    assert "Стрижка мужская" in text


@pytest.mark.asyncio
async def test_cmd_services_del_strip_matches_create_normalization(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """W1 fix (code-review B.11): 'Стрижка_' (created) → stored 'Стрижка' (strip)
    → '/services del Стрижка_' must find it (deactivate_service now strips name
    too, mirroring create_service admin.py:243).
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)
        # add via 'Стрижка_' — handler replaces '_' with ' ', create_service strips
        msg_add = _make_message(user_id=ADMIN_TG_ID, text="/services add Стрижка_ 60")
        await admin_handlers.cmd_services(msg_add, _make_command("add Стрижка_ 60"))
        # verify stored as "Стрижка" (stripped)
        svc = (await session.execute(select(Service))).scalar_one()
        assert svc.name == "Стрижка"
        assert svc.is_active is True

    # delete via the SAME spelling the user used to add — must succeed.
    msg_del = _make_message(user_id=ADMIN_TG_ID, text="/services del Стрижка_")
    await admin_handlers.cmd_services(msg_del, _make_command("del Стрижка_"))

    text = _answer_text(msg_del)
    assert "✅ Услуга удалена" in text, f"expected success, got: {text!r}"

    async with session_factory() as verify:
        svc_after = (await verify.execute(select(Service))).scalar_one()
        assert svc_after.is_active is False


@pytest.mark.asyncio
async def test_cmd_services_del_aggregate_block_with_duplicate_names(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """S2 (code-review B.11): 2 active services with same name 'Стрижка'.
    Service A has 1 active booking, Service B has 0 → aggregate block-check
    blocks BOTH (no partial deactivation).
    """
    async with session_factory() as session:
        seed = await _seed_admin_stack(session)
        svc_a = Service(
            business_id=seed["business_id"], name="Стрижка", duration_minutes=60, is_active=True
        )
        svc_b = Service(
            business_id=seed["business_id"], name="Стрижка", duration_minutes=45, is_active=True
        )
        session.add_all([svc_a, svc_b])
        await session.flush()
        # Only svc_a has an active booking.
        session.add(
            Booking(
                business_id=seed["business_id"],
                master_id=seed["master_id"],
                client_id=seed["client_id"],
                service_id=svc_a.id,
                service_title_snapshot="Стрижка",
                service_price_snapshot=None,
                client_name_snapshot="Паша",
                start_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
                end_at=datetime(2026, 10, 1, 13, 0, tzinfo=UTC),
                status="confirmed",
            )
        )
        await session.commit()

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services del Стрижка")
    await admin_handlers.cmd_services(msg, _make_command("del Стрижка"))

    text = _answer_text(msg)
    assert "Сначала отмените" in text
    assert "1 запис" in text  # 1 → "запись"

    async with session_factory() as verify:
        stmt = select(Service).order_by(Service.created_at)
        svcs = list((await verify.execute(stmt)).scalars().all())
        assert len(svcs) == 2
        # Both blocked — neither deactivated.
        assert all(s.is_active for s in svcs)


@pytest.mark.asyncio
async def test_cmd_services_del_multiple_same_name_no_bookings_deactivates_all(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """S4 (code-review B.11): 2 active services 'Стрижка', no bookings → both
    deactivated, handler shows 'Удалено услуг: 2' (plural branch).
    """
    async with session_factory() as session:
        seed = await _seed_admin_stack(session)
        for _ in range(2):
            session.add(
                Service(
                    business_id=seed["business_id"],
                    name="Стрижка",
                    duration_minutes=60,
                    is_active=True,
                )
            )
        await session.commit()

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services del Стрижка")
    await admin_handlers.cmd_services(msg, _make_command("del Стрижка"))

    text = _answer_text(msg)
    assert "Удалено услуг: 2" in text
    assert "Стрижка" in text

    async with session_factory() as verify:
        svcs = list((await verify.execute(select(Service))).scalars().all())
        assert len(svcs) == 2
        assert all(not s.is_active for s in svcs)


@pytest.mark.parametrize(
    ("n_bookings", "expected_word"),
    [
        (1, "запись"),
        (2, "записи"),
        (3, "записи"),
        (4, "записи"),
        (5, "записей"),
        (11, "записей"),
        (21, "запись"),
        (22, "записи"),
        (25, "записей"),
        (101, "запись"),
        (111, "записей"),
        (121, "запись"),
    ],
)
@pytest.mark.asyncio
async def test_cmd_services_del_plural_form(
    session_factory: Any,
    patched_session_factory: Any,
    n_bookings: int,
    expected_word: str,
) -> None:
    """S3 (code-review B.11): Russian plural form 'запись/записи/записей' by
    count n. Validates edge cases n=1, 5, 11, 21, 22, 101, 111, 121 where
    the formula has boundaries (n%10, n%100).
    """
    async with session_factory() as session:
        seed = await _seed_admin_stack(session)
        svc = Service(
            business_id=seed["business_id"], name="Стрижка", duration_minutes=60, is_active=True
        )
        session.add(svc)
        await session.flush()
        # Create n_bookings confirmed bookings — all on svc.id.
        for i in range(n_bookings):
            session.add(
                Booking(
                    business_id=seed["business_id"],
                    master_id=seed["master_id"],
                    client_id=seed["client_id"],
                    service_id=svc.id,
                    service_title_snapshot="Стрижка",
                    service_price_snapshot=None,
                    client_name_snapshot="Паша",
                    start_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC) + timedelta(hours=i),
                    end_at=datetime(2026, 10, 1, 13, 0, tzinfo=UTC) + timedelta(hours=i),
                    status="confirmed",
                )
            )
        await session.commit()

    msg = _make_message(user_id=ADMIN_TG_ID, text="/services del Стрижка")
    await admin_handlers.cmd_services(msg, _make_command("del Стрижка"))

    text = _answer_text(msg)
    assert f"{n_bookings} {expected_word}" in text, (
        f"plural form mismatch for n={n_bookings}: expected '{expected_word}', got: {text!r}"
    )


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
    """Extract text from callback.message — supports both edit_text (preferred
    when handler replaces the inline-keyboard message) and answer (fallback for
    new message or when edit_text raised TelegramBadRequest).
    """
    if callback.message.edit_text.called:
        args, _ = callback.message.edit_text.call_args
        return str(args[0])
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
async def test_admin_move_simple_calendar_missing_booking_id_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.30: admin_move_simple_calendar_cb, booking_id missing in state
    (state corruption) → state.clear + 'Данные потеряны. /today чтобы начать'
    + callback.answer. Defensive check BEFORE fetch slots.
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
            is_active=True,
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

    # No admin_move_booking_id in state — simulates state corruption.
    state = _make_mock_state(data={})

    from unittest.mock import patch

    with patch(
        "aiogram_calendar.SimpleCalendar.process_selection",
        return_value=(True, future_date),
    ):
        await admin_handlers.admin_move_simple_calendar_cb(cb, cal_cb_data, state)

    state.clear.assert_awaited_once()
    text = callback_answer_text(cb)
    assert "Данные потеряны" in text
    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_move_simple_calendar_booking_not_found_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.30: admin_move_simple_calendar_cb, booking_id in state but Booking
    not in DB (deleted between select and calendar) → state.clear + retry hint.
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
            is_active=True,
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

    # booking_id present but Booking not in DB (deleted).
    bogus_id = "00000000-0000-0000-0000-000000000000"
    state = _make_mock_state(data={"admin_move_booking_id": bogus_id})

    from unittest.mock import patch

    with patch(
        "aiogram_calendar.SimpleCalendar.process_selection",
        return_value=(True, future_date),
    ):
        await admin_handlers.admin_move_simple_calendar_cb(cb, cal_cb_data, state)

    state.clear.assert_awaited_once()
    text = callback_answer_text(cb)
    assert "не найдена" in text or "запись не найдена" in text.lower()
    state.set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_move_simple_calendar_filters_slots_by_service_duration(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.30: admin_move_simple_calendar_cb filters 30-min slots by
    the booking's service duration (same overlap-fix as client.py Task 2).
    Booking 16:00-18:00 (Окрашивание 120 мин) → slot 15:30 should NOT appear
    in picker (15:30+120=17:30 overlaps 16:00-18:00 half-open). Slot 14:00
    should appear (14:00+120=16:00, half-open no overlap — 16:00 NOT < 16:00).
    14:30 filtered (14:30+120=16:30, overlap: 16:00<16:30 AND 18:00>14:30).

    Workday 13:00-20:00, booking 16:00-18:00, duration 120:
      13:00 → 13:00+120=15:00, no overlap → shown
      13:30 → 13:30+120=15:30, no overlap → shown
      14:00 → 14:00+120=16:00, half-open no overlap (16:00 NOT < 16:00) → shown
      14:30 → 14:30+120=16:30, overlap (16:00<16:30 AND 18:00>14:30) → filtered
      15:00 → 15:00+120=17:00, overlap → filtered
      15:30 → filtered (same)
      16:00-17:30 → all overlap → filtered
      18:00 → 18:00+120=20:00, half-open no overlap (18:00 NOT > 18:00) → shown
      18:30 → 18:30+120=20:30 > 20:00 → filtered (workday end)
    Expected: {13:00, 13:30, 14:00, 18:00} = 4 slots.
    """
    from aiogram_calendar import SimpleCalendarCallback
    from aiogram_calendar.schemas import SimpleCalAct

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        from datetime import time as dt_time

        from bot.models import Booking, Service, WorkDay

        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        workday = WorkDay(
            master_id=ctx["master_id"],
            work_date=tomorrow,
            start_time=dt_time(13, 0),
            end_time=dt_time(20, 0),
            max_concurrent_clients=1,
            is_active=True,
        )
        session.add(workday)
        await session.flush()

        service = Service(
            business_id=ctx["business_id"],
            name="Окрашивание",
            duration_minutes=120,
            is_active=True,
        )
        session.add(service)
        await session.flush()

        # Booking 16:00-18:00 on tomorrow (LOCAL 16:00 = UTC 13:00 if MSK tz).
        tz = ZoneInfo(TZ)
        start_local = datetime.combine(tomorrow, dt_time(16, 0), tzinfo=tz)
        end_local = start_local + timedelta(hours=2)
        booking = Booking(
            business_id=ctx["business_id"],
            master_id=ctx["master_id"],
            client_id=ctx["client_id"],
            service_id=service.id,
            service_title_snapshot="Окрашивание",
            client_name_snapshot="Паша",
            start_at=start_local.astimezone(UTC),
            end_at=end_local.astimezone(UTC),
            status="confirmed",
        )
        session.add(booking)
        await session.commit()

        booking_id = str(booking.id)

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

    state = _make_mock_state(data={"admin_move_booking_id": booking_id})

    from unittest.mock import patch

    with patch(
        "aiogram_calendar.SimpleCalendar.process_selection",
        return_value=(True, future_date),
    ):
        await admin_handlers.admin_move_simple_calendar_cb(cb, cal_cb_data, state)

    # Extract button labels from picker reply_markup (W1 fix: verify absence
    # of 15:30, not just picker presence — guards regression of min_duration_min).
    answer_calls = cb.message.answer.await_args_list
    picker_call = None
    for call in answer_calls:
        args, kwargs = call
        if args and "Выберите новое время" in str(args[0]):
            picker_call = call
            break
    assert picker_call is not None, "Slot picker message not found"
    reply_markup = picker_call.kwargs["reply_markup"]
    button_texts = [
        btn.text for row in reply_markup.inline_keyboard for btn in row
    ]
    # Expected slots: 13:00, 13:30, 14:00, 18:00 (4 slots).
    assert "13:00" in button_texts
    assert "14:00" in button_texts
    assert "18:00" in button_texts
    # 15:30 should NOT appear — overlaps 16:00-18:00 with 120-min duration.
    assert "15:30" not in button_texts
    # 14:30 should NOT appear — 14:30+120=16:30 overlaps 16:00-18:00 half-open.
    assert "14:30" not in button_texts
    # 16:00 should NOT appear — overlaps booking start.
    assert "16:00" not in button_texts


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


@pytest.mark.asyncio
async def test_admin_window_booked_cb_alerts_no_state_change(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[🔒 слот занят] (string F.data == 'admin_window_booked') → show_alert с
    подсказкой /today + /closeday. State НЕ трогается (accidental tap не теряет FSM).
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_window_booked"
    state = _make_mock_state({"picked_start_minute": 600})  # FSM есть — не должен чиститься

    await admin_handlers.admin_window_booked_cb(callback)

    # state.clear НЕ вызывался — admin может тапнуть 🔒 случайно без потери FSM.
    state.clear.assert_not_called()
    # callback.answer с show_alert=True — текст содержит подсказки.
    args, kwargs = callback.answer.call_args
    alert_text = args[0] if args else kwargs.get("text", "")
    assert "🔒" in alert_text
    assert "/today" in alert_text and "Перенести" in alert_text
    assert "/closeday" in alert_text
    assert kwargs.get("show_alert") is True


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
    # edit_text (preferred) or answer fallback — callback_answer_text handles both.
    text = callback_answer_text(callback)
    assert "Шаг 2" in text
    # reply_markup is on edit_text (preferred) or answer (fallback).
    if callback.message.edit_text.called:
        _, kwargs = callback.message.edit_text.call_args
    else:
        _, kwargs = callback.message.answer.call_args
    reply_markup = kwargs.get("reply_markup")
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
    # edit_text (preferred) or answer fallback — callback_answer_text handles both.
    text = callback_answer_text(callback)
    assert "Шаг 3" in text
    # reply_markup is on edit_text (preferred) or answer (fallback).
    if callback.message.edit_text.called:
        _, kwargs = callback.message.edit_text.call_args
    else:
        _, kwargs = callback.message.answer.call_args
    reply_markup = kwargs.get("reply_markup")
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


# ============================================================
# 5.60 P2 — past weekdays marked with ❌ on /openweek step 3 keyboard
# ============================================================


def test_past_weekdays_for_week_returns_past_only() -> None:
    """5.60 P2 helper: returns weekday ints whose work_date < today_local.

    Friday 11.09 (monday=07.09) → Пн-Чт (07-10.09) are past, Пт-Вс future.
    Sunday today → all 7 days of current week are past.
    Monday today → no past days (today is the first day of the work week).
    """
    from datetime import date

    from bot.handlers.admin import _past_weekdays_for_week

    # Friday 11.09 — Пн-Чт past, Пт-Вс future.
    monday = date(2026, 9, 7)
    today = date(2026, 9, 11)
    assert _past_weekdays_for_week(monday, today) == frozenset({0, 1, 2, 3})

    # Sunday 13.09 — Пн-Сб (07-12.09) past, Вс (13.09) is today, not past.
    today_sunday = date(2026, 9, 13)
    assert _past_weekdays_for_week(monday, today_sunday) == frozenset({0, 1, 2, 3, 4, 5})

    # Monday 07.09 (today = monday) — no past days.
    today_monday = date(2026, 9, 7)
    assert _past_weekdays_for_week(monday, today_monday) == frozenset()


def test_admin_week_days_keyboard_marks_past_days_with_x() -> None:
    """5.60 P2: keyboard adds ' ❌' suffix for past_weekdays. ✅ prefix stays
    for selected days (variant A — admin can still tap → toggle, callback_data
    preserved; _apply_openweek:3079-3081 filters past days at confirm step).
    """
    from bot.keyboards.admin import AdminOpenWeekCallbackData, admin_week_days_keyboard

    kb = admin_week_days_keyboard({0, 2}, past_weekdays=frozenset({0, 1, 2, 3}))
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    labels = [btn.text for btn in buttons]

    # Past days (Пн-Чт) get ❌ suffix. Selected days also get ✅ prefix.
    assert "✅ Пн ❌" in labels, f"Selected past day Mon; got: {labels}"
    assert "Вт ❌" in labels, f"Unselected past day Tue; got: {labels}"
    assert "✅ Ср ❌" in labels, f"Selected past day Wed; got: {labels}"
    assert "Чт ❌" in labels, f"Unselected past day Thu; got: {labels}"
    # Future days (Пт, Сб, Вс) — no ❌ suffix.
    assert "Пт" in labels, f"Fri is future; got: {labels}"
    assert "Пт ❌" not in labels, f"Fri must NOT have ❌; got: {labels}"
    assert "✅ Пт" not in labels, f"Fri not selected; got: {labels}"
    assert "Сб" in labels
    assert "Вс" in labels

    # Variant A invariant: all 7 weekday buttons keep callback_data (toggle
    # works on past days too, filtering happens later in _apply_openweek).
    weekday_buttons = buttons[:7]
    for wd in range(7):
        cb_data_str = weekday_buttons[wd].callback_data
        assert cb_data_str is not None, f"callback_data missing for weekday {wd}"
        unpacked = AdminOpenWeekCallbackData.unpack(cb_data_str)
        assert unpacked.weekday == wd, (
            f"callback_data must encode weekday={wd}; got: {unpacked.weekday}"
        )


def test_week_monday_offset_zero_equals_current_week_monday() -> None:
    """5.61 backward compat: _week_monday(tz, 0) == _current_week_monday(tz).

    Without this invariant, existing callsites that still use
    _current_week_monday (kept as thin wrapper) would drift from new
    _week_monday-based code.
    """
    from bot.handlers.admin import _current_week_monday, _week_monday

    tz = "Europe/Moscow"
    assert _week_monday(tz, 0) == _current_week_monday(tz), (
        "offset=0 must match _current_week_monday for backward compat"
    )


def test_week_monday_offset_one_plus_seven_days() -> None:
    """5.61: _week_monday(tz, 1) == _current_week_monday(tz) + 7 days."""
    from datetime import timedelta

    from bot.handlers.admin import _current_week_monday, _week_monday

    tz = "Europe/Moscow"
    base = _current_week_monday(tz)
    assert _week_monday(tz, 1) == base + timedelta(days=7), (
        f"offset=1 must be +7 days from base; base={base}, "
        f"got={_week_monday(tz, 1)}"
    )
    assert _week_monday(tz, 2) == base + timedelta(days=14), (
        "offset=2 must be +14 days"
    )
    assert _week_monday(tz, 4) == base + timedelta(days=28), (
        "offset=4 (cap) must be +28 days"
    )


def test_week_monday_negative_offset_raises() -> None:
    """5.61: negative offset raises ValueError (prev-week navigation capped
    at offset=0 by keyboard, defense-in-depth here)."""
    from bot.handlers.admin import _week_monday

    with pytest.raises(ValueError, match="offset must be >= 0"):
        _week_monday("Europe/Moscow", -1)


def test_admin_week_days_keyboard_scheduled_marker_yellow() -> None:
    """5.61: scheduled_weekdays (active WorkDay) get ` 🟡` suffix."""
    from bot.keyboards.admin import admin_week_days_keyboard

    kb = admin_week_days_keyboard(
        set(),
        scheduled_weekdays=frozenset({5}),  # Сб has active WorkDay
    )
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    labels = [btn.text for btn in buttons]

    assert "Сб 🟡" in labels, f"Scheduled Sat must have 🟡; got: {labels}"
    assert "Пн" in labels, "Mon not scheduled — no marker"
    assert "Пн 🟡" not in labels, "Mon must NOT have 🟡"


def test_admin_week_days_keyboard_closed_marker_white_circle() -> None:
    """5.61: closed_weekdays (is_active=False) get ` ⚪` suffix."""
    from bot.keyboards.admin import admin_week_days_keyboard

    kb = admin_week_days_keyboard(
        set(),
        closed_weekdays=frozenset({6}),  # Вс closed via /closeday
    )
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    labels = [btn.text for btn in buttons]

    assert "Вс ⚪" in labels, f"Closed Sun must have ⚪; got: {labels}"
    assert "Пн ⚪" not in labels, "Mon not closed — no ⚪"


def test_admin_week_days_keyboard_past_overrides_scheduled() -> None:
    """5.61 suffix priority: ❌ (past) > 🟡 (scheduled) > ⚪ (closed).

    Past day with active WorkDay → only ❌ shown (apply will filter it anyway,
    no point confusing admin with 🟡 on a past day).
    """
    from bot.keyboards.admin import admin_week_days_keyboard

    kb = admin_week_days_keyboard(
        set(),
        past_weekdays=frozenset({0}),  # Пн past
        scheduled_weekdays=frozenset({0}),  # Пн also has active WorkDay
    )
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    labels = [btn.text for btn in buttons]

    assert "Пн ❌" in labels, f"Past must win over scheduled; got: {labels}"
    assert "Пн 🟡" not in labels, "Past day must NOT show 🟡"


def test_admin_week_days_keyboard_nav_buttons_default_present() -> None:
    """5.61: by default both ← Пред. and След. → buttons are present."""
    from bot.keyboards.admin import admin_week_days_keyboard

    kb = admin_week_days_keyboard(set())
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    labels = [btn.text for btn in buttons]

    assert "← Пред." in labels, "Default: prev button present"
    assert "След. →" in labels, "Default: next button present"


def test_admin_week_days_keyboard_nav_prev_hidden_at_offset_zero() -> None:
    """5.61: at week_offset=0 (current week), ← Пред. is hidden — previous
    week is fully in past, no point navigating there."""
    from bot.keyboards.admin import admin_week_days_keyboard

    kb = admin_week_days_keyboard(set(), can_go_prev=False)
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    labels = [btn.text for btn in buttons]

    assert "← Пред." not in labels, "can_go_prev=False → prev hidden"
    assert "След. →" in labels, "can_go_next default True → next present"


def test_admin_week_days_keyboard_nav_next_hidden_at_cap() -> None:
    """5.61: at week_offset=MAX (4), След. → is hidden — can't go beyond cap."""
    from bot.keyboards.admin import admin_week_days_keyboard

    kb = admin_week_days_keyboard(set(), can_go_next=False)
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    labels = [btn.text for btn in buttons]

    assert "След. →" not in labels, "can_go_next=False → next hidden"
    assert "← Пред." in labels, "can_go_prev default True → prev present"


def test_admin_week_days_keyboard_nav_callback_data_packs_delta() -> None:
    """5.61: nav buttons use AdminOpenWeekNavCallbackData with delta=-1/+1."""
    from bot.keyboards.admin import (
        AdminOpenWeekNavCallbackData,
        admin_week_days_keyboard,
    )

    kb = admin_week_days_keyboard(set())
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    nav_buttons = [btn for btn in buttons if btn.text in ("← Пред.", "След. →")]

    assert len(nav_buttons) == 2, f"Expected 2 nav buttons; got: {len(nav_buttons)}"
    deltas = []
    for btn in nav_buttons:
        assert btn.callback_data is not None
        unpacked = AdminOpenWeekNavCallbackData.unpack(btn.callback_data)
        deltas.append(unpacked.delta)
    assert -1 in deltas, "← Пред. must have delta=-1"
    assert 1 in deltas, "След. → must have delta=+1"


@pytest.mark.asyncio
@freeze_time("2026-09-11 14:00:00", tz_offset=0)  # Friday UTC 14:00 → Moscow 17:00
async def test_admin_openweek_end_cb_seeds_week_offset_zero(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """5.61: end_cb seeds week_offset=0 in state (current week on entry)."""
    from uuid import UUID as _UUID

    from bot.keyboards.admin import AdminWindowSlot30CallbackData

    async with session_factory() as session:
        await _seed_admin_stack(session)

    sentinel = _UUID(int=0)
    cb_data = AdminWindowSlot30CallbackData(workday_id=sentinel, start_minute=1080)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    state = _make_mock_state({"business_tz": TZ, "picked_start_minute": 600})

    await admin_handlers.admin_openweek_end_cb(callback, cb_data, state)

    data = _state_all_updates(state)
    assert data["week_offset"] == 0, (
        f"week_offset must be 0 on entry; got: {data.get('week_offset')}"
    )
    assert data["scheduled_weekdays"] == [], (
        f"No WorkDays seeded → scheduled empty; got: {data.get('scheduled_weekdays')}"
    )
    assert data["closed_weekdays"] == [], (
        f"No WorkDays seeded → closed empty; got: {data.get('closed_weekdays')}"
    )


@pytest.mark.asyncio
@freeze_time("2026-09-11 14:00:00", tz_offset=0)  # Friday 11.09 MSK 17:00
async def test_admin_openweek_week_nav_cb_next_increments_offset(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """5.61: tap След. → (delta=+1) from week_offset=0 → week_offset=1,
    selected_weekdays reset to [].

    Friday 11.09: base monday=07.09. Next week (offset=1) monday=14.09, all
    7 days future → past_weekdays=[]. No WorkDays seeded → scheduled/closed=[].
    """
    from bot.keyboards.admin import AdminOpenWeekNavCallbackData

    async with session_factory() as session:
        await _seed_admin_stack(session)

    cb_data = AdminOpenWeekNavCallbackData(delta=1)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    callback.message.edit_text = AsyncMock()
    state = _make_mock_state(
        {
            "business_tz": TZ,
            "picked_start_minute": 600,
            "picked_end_minute": 1080,
            "selected_weekdays": [1, 3],  # Tue + Thu selected — will reset
            "past_weekdays": [0, 1, 2, 3],
            "scheduled_weekdays": [],
            "closed_weekdays": [],
            "week_offset": 0,
        }
    )

    await admin_handlers.admin_openweek_week_nav_cb(callback, cb_data, state)

    data = _state_all_updates(state)
    assert data["week_offset"] == 1, (
        f"Next from offset=0 → offset=1; got: {data.get('week_offset')}"
    )
    assert data["selected_weekdays"] == [], (
        f"Selected must reset on week change; got: {data.get('selected_weekdays')}"
    )
    # Friday 11.09 + 1 week = 14-20.09, all future → past_weekdays=[]
    assert data["past_weekdays"] == [], (
        f"Next week all future → no past; got: {data.get('past_weekdays')}"
    )
    # Alert about reset should have been shown (selected was non-empty).
    args, kwargs = callback.answer.call_args
    assert "Выбор сброшен" in (args[0] if args else kwargs.get("text", "")), (
        f"Reset alert must be shown when selected was non-empty; "
        f"got: {callback.answer.call_args}"
    )


@pytest.mark.asyncio
@freeze_time("2026-09-11 14:00:00", tz_offset=0)  # Friday 11.09 MSK 17:00
async def test_admin_openweek_week_nav_cb_prev_at_zero_no_op(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """5.61: tap ← Пред. (delta=-1) at week_offset=0 → no-op (clamped to 0).

    Defense-in-depth: keyboard hides ← Пред. at offset=0, but if stale callback
    slips through (rapid tap, race), handler clamps and does nothing.
    """
    from bot.keyboards.admin import AdminOpenWeekNavCallbackData

    async with session_factory() as session:
        await _seed_admin_stack(session)

    cb_data = AdminOpenWeekNavCallbackData(delta=-1)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    callback.message.edit_text = AsyncMock()
    state = _make_mock_state(
        {
            "business_tz": TZ,
            "picked_start_minute": 600,
            "picked_end_minute": 1080,
            "selected_weekdays": [],
            "past_weekdays": [0, 1, 2, 3],
            "scheduled_weekdays": [],
            "closed_weekdays": [],
            "week_offset": 0,
        }
    )

    await admin_handlers.admin_openweek_week_nav_cb(callback, cb_data, state)

    # No-op: handler must NOT call update_data (state unchanged) and NOT
    # re-render message (edit_text not called).
    assert not state.update_data.called, (
        "Clamped no-op must NOT call update_data"
    )
    assert not callback.message.edit_text.called, (
        "Clamped no-op must NOT re-render message"
    )
    # No-op MUST dismiss loading spinner via callback.answer() — without
    # this, Telegram shows infinite spinner on the button.
    callback.answer.assert_called_once()
    # Defense-in-depth: no-op must not mutate FSM state (no clear, no set_state).
    state.clear.assert_not_called()
    state.set_state.assert_not_called()


@pytest.mark.asyncio
@freeze_time("2026-09-11 14:00:00", tz_offset=0)  # Friday 11.09 MSK 17:00
async def test_admin_openweek_week_nav_cb_next_at_cap_no_op(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """5.61: tap След. → (delta=+1) at week_offset=MAX (4) → no-op (clamped)."""
    from bot.keyboards.admin import AdminOpenWeekNavCallbackData

    async with session_factory() as session:
        await _seed_admin_stack(session)

    cb_data = AdminOpenWeekNavCallbackData(delta=1)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    callback.message.edit_text = AsyncMock()
    state = _make_mock_state(
        {
            "business_tz": TZ,
            "picked_start_minute": 600,
            "picked_end_minute": 1080,
            "selected_weekdays": [],
            "past_weekdays": [],
            "scheduled_weekdays": [],
            "closed_weekdays": [],
            "week_offset": 4,  # at cap
        }
    )

    await admin_handlers.admin_openweek_week_nav_cb(callback, cb_data, state)

    assert not state.update_data.called, (
        "Clamped no-op at cap must NOT call update_data"
    )
    assert not callback.message.edit_text.called, (
        "Clamped no-op at cap must NOT re-render"
    )
    callback.answer.assert_called_once()
    state.clear.assert_not_called()
    state.set_state.assert_not_called()


@pytest.mark.asyncio
@freeze_time("2026-09-11 14:00:00", tz_offset=0)  # Friday UTC 14:00 → Moscow 17:00
async def test_admin_openweek_end_cb_caches_past_weekdays_in_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """5.60 P2: end_cb caches past_weekdays in state on entry to step 3.
    Friday 11.09 → monday=07.09, past_weekdays=[0,1,2,3] (Пн-Чт 07-10.09).
    Toggle handler reads from state without recomputing (monday is fixed).
    """
    from uuid import UUID as _UUID

    from bot.keyboards.admin import AdminWindowSlot30CallbackData

    async with session_factory() as session:
        await _seed_admin_stack(session)

    sentinel = _UUID(int=0)
    cb_data = AdminWindowSlot30CallbackData(workday_id=sentinel, start_minute=1080)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    state = _make_mock_state({"business_tz": TZ, "picked_start_minute": 600})

    await admin_handlers.admin_openweek_end_cb(callback, cb_data, state)

    data = _state_all_updates(state)
    assert data["past_weekdays"] == [0, 1, 2, 3], (
        f"Friday 11.09 → Пн-Чт past; got: {data.get('past_weekdays')}"
    )
    assert data["selected_weekdays"] == []


@pytest.mark.asyncio
async def test_admin_openweek_days_cb_passes_past_weekdays_to_keyboard(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """5.60 P2: days_cb reads past_weekdays from state and passes to keyboard.
    Tap on weekday=4 (Fri, not past) → re-render with past_weekdays from state
    (Пн-Чт past). Past days get ❌ suffix, Fri gets ✅ prefix (selected).
    """
    from bot.keyboards.admin import AdminOpenWeekCallbackData

    async with session_factory() as session:
        await _seed_admin_stack(session)

    cb_data = AdminOpenWeekCallbackData(weekday=4)
    callback = _make_callback(ADMIN_TG_ID, callback_data=cb_data)
    callback.message.edit_reply_markup = AsyncMock()
    state = _make_mock_state(
        {
            "selected_weekdays": [],
            "past_weekdays": [0, 1, 2, 3],
        }
    )

    await admin_handlers.admin_openweek_days_cb(callback, cb_data, state)

    assert callback.message.edit_reply_markup.called, "Should re-render keyboard"
    args, kwargs = callback.message.edit_reply_markup.call_args
    reply_markup = kwargs.get("reply_markup")
    assert reply_markup is not None, "Expected reply_markup"
    buttons = [btn for row in reply_markup.inline_keyboard for btn in row]
    labels = [btn.text for btn in buttons]
    assert "Пн ❌" in labels, f"Past Mon gets ❌ suffix; got: {labels}"
    assert "✅ Пт" in labels, f"Fri selected via toggle; got: {labels}"
    assert "Пт ❌" not in labels, f"Fri is future, no ❌; got: {labels}"


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
@freeze_time("2026-08-29 14:00:00", tz_offset=0)  # Saturday UTC 14:00 → Moscow 17:00
async def test_admin_openweek_confirm_cb_skips_past_days(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """[✅ Открыть] on Saturday with [Mon, Tue, Wed] selected — all 3 are past
    (Mon 24, Tue 25, Wed 26 Aug, current week). All get ❌ 'прошедшая дата',
    no WorkDay created. Saturday keeps current week (Sunday rule excluded —
    on Sunday /openweek targets next week instead, see
    test_admin_openweek_confirm_cb_sunday_targets_next_week).
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
@freeze_time("2026-08-30 14:00:00", tz_offset=0)  # Sunday UTC 14:00 → Moscow 17:00
async def test_admin_openweek_confirm_cb_sunday_targets_next_week(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Sunday rule: /openweek on Sunday targets NEXT week, not current.

    Without the rule, on Sunday 30 Aug the current week's Mon-Sat (24-29 Aug)
    are all past → 6 'прошедшая дата' buttons, useless UX. User on Sunday wants
    to plan next week (Mon 31 Aug - Sun 6 Sep).

    Selected [Mon, Tue, Wed] → work_dates Mon 31 Aug, Tue 1 Sep, Wed 2 Sep
    (all future) → all 3 should open successfully (✅ lines, no past-skip).
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
            "selected_weekdays": [0, 1, 2],  # Mon, Tue, Wed
            "business_tz": TZ,
        }
    )

    await admin_handlers.admin_openweek_confirm_cb(callback, state)

    text = callback_answer_text(callback)
    assert "прошедшая дата" not in text, f"Sunday must target next week (all future); got: {text!r}"
    assert "✅ Пн" in text and "✅ Вт" in text and "✅ Ср" in text
    # Verify WorkDay created for next week's Mon/Tue/Wed (31 Aug, 1, 2 Sep).
    today_local = datetime.now(ZoneInfo(TZ)).date()
    monday_this = today_local - timedelta(days=today_local.weekday())
    monday_next = monday_this + timedelta(days=7)
    async with session_factory() as session:
        for weekday in [0, 1, 2]:
            wd_date = monday_next + timedelta(days=weekday)
            wd = await session.scalar(
                select(WorkDay).where(
                    WorkDay.master_id == ctx["master_id"],
                    WorkDay.work_date == wd_date,
                )
            )
            assert wd is not None, (
                f"WorkDay for next-week weekday={weekday} ({wd_date}) should be created"
            )


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
@freeze_time("2026-09-06 14:00:00", tz_offset=0)  # Sunday UTC 14:00 → Moscow 17:00
async def test_admin_openweek_confirm_cb_ignores_past_week_bookings(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Past-week booking must NOT appear in /openweek "📅 Записи на неделю:" block.

    Regression: get_week_bookings(days_ahead=7) on Sunday returns 06.09..12.09
    (today + 6 days) → includes past-week booking 06.09 19:00 — but /openweek
    header promises 07.09–13.09 (next week). Mismatch confused master.

    Fix: /openweek uses get_bookings_for_date_range(monday, sunday) — strict
    range match to header. Booking on 06.09 (last week) must NOT appear.
    """
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        # Booking on 06.09 (Sunday, today in freeze) at 19:00 Moscow — past week
        booking_local = datetime(2026, 9, 6, 19, 0, tzinfo=ZoneInfo(TZ))
        slot = await _seed_slot(
            session,
            master_id=ctx["master_id"],
            slot_date=booking_local.date(),
            hour=19,
            status="open",
        )
        await _seed_booking(
            session,
            ctx=ctx,
            slot=slot,
            start_at_utc_naive=_local_to_utc_naive(booking_local),
            status="confirmed",
        )

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_openweek_confirm"
    state = _make_mock_state(
        {
            "picked_start_minute": 600,  # 10:00
            "picked_end_minute": 1200,  # 20:00
            "selected_weekdays": [0, 1, 3, 5],  # Mon, Tue, Thu, Sat (next week)
            "business_tz": TZ,
        }
    )

    await admin_handlers.admin_openweek_confirm_cb(callback, state)

    text = callback_answer_text(callback)
    # Header must show next week (07.09–13.09)
    assert "07.09 – 13.09" in text, f"Header should be next week; got: {text!r}"
    # Past-week booking (06.09) must NOT leak into "Записи на неделю" block
    assert "06 сен" not in text, (
        f"Past-week booking 06.09 must NOT appear in next-week block; got: {text!r}"
    )


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
# /openweek overwrite guard (Session 5.27 B)
# ============================================================


@pytest.mark.asyncio
@freeze_time("2026-09-06 14:00:00", tz_offset=0)  # Sunday UTC 14:00 → Moscow 17:00
async def test_openweek_confirm_warns_when_days_already_open(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """B: confirm shows overwrite alert if any selected day already has WorkDay.

    Setup: Sunday 06.09 → next week 07.09–13.09. Seed WorkDay for Mon 07.09 and
    Wed 09.09 (10:00–19:00). Select Mon+Wed+Fri (Fri has no WorkDay). Confirm
    with new window 09:00–18:00.

    Expected: alert "⚠️ Уже открыты: ... Пн 07.09 10:00–19:00, Ср 09.09 10:00–19:00
    Перезаписать на 09:00–18:00?"; state NOT cleared (yes-handler needs it);
    open_workday NOT called (WorkDay rows unchanged).
    """
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        # Next week Mon 07.09 and Wed 09.09 — already opened 10-19
        await _seed_workday(
            session,
            ctx=ctx,
            work_date=datetime(2026, 9, 7).date(),
            start_time_str="10:00",
            end_time_str="19:00",
        )
        await _seed_workday(
            session,
            ctx=ctx,
            work_date=datetime(2026, 9, 9).date(),
            start_time_str="10:00",
            end_time_str="19:00",
        )

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_openweek_confirm"
    state = _make_mock_state(
        {
            "picked_start_minute": 540,  # 09:00
            "picked_end_minute": 1080,  # 18:00
            "selected_weekdays": [0, 2, 4],  # Mon, Wed, Fri
            "business_tz": TZ,
        }
    )

    await admin_handlers.admin_openweek_confirm_cb(callback, state)

    # state NOT cleared — yes-handler needs picked_start_minute etc.
    state.clear.assert_not_called()
    text = callback_answer_text(callback)
    assert "Уже есть окно" in text, f"Should warn about existing days; got: {text!r}"
    assert "Пн 07.09 10:00–19:00" in text, f"Should list existing Mon window; got: {text!r}"
    assert "Ср 09.09 10:00–19:00" in text, f"Should list existing Wed window; got: {text!r}"
    assert "Перезаписать окно на 09:00–18:00" in text, f"Should show new window; got: {text!r}"
    # No silent-apply artifacts
    assert "✅" not in text, f"Should NOT apply silently; got: {text!r}"
    assert "📅 Записи на неделю" not in text, f"Should NOT show bookings block; got: {text!r}"


@pytest.mark.asyncio
@freeze_time("2026-09-06 14:00:00", tz_offset=0)
async def test_openweek_overwrite_yes_applies_overwrite(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """B: [✅ Да, перезаписать] applies open_workday to all selected, overwrites
    existing windows.

    Pre-condition: WorkDay Mon 07.09 10-19 and Wed 09.09 10-19, Fri 11.09 none.
    After yes: Mon and Wed become 09-18, Fri created 09-18. state.clear called.
    """
    from bot.services.workday import select_workday

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        master_id = ctx["master_id"]
        await _seed_workday(
            session,
            ctx=ctx,
            work_date=datetime(2026, 9, 7).date(),
            start_time_str="10:00",
            end_time_str="19:00",
        )
        await _seed_workday(
            session,
            ctx=ctx,
            work_date=datetime(2026, 9, 9).date(),
            start_time_str="10:00",
            end_time_str="19:00",
        )

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_openweek_overwrite_yes"
    state = _make_mock_state(
        {
            "picked_start_minute": 540,  # 09:00
            "picked_end_minute": 1080,  # 18:00
            "selected_weekdays": [0, 2, 4],  # Mon, Wed, Fri
            "business_tz": TZ,
        }
    )

    await admin_handlers.admin_openweek_overwrite_yes_cb(callback, state)

    state.clear.assert_called_once()
    text = callback_answer_text(callback)
    # All 3 days applied with new window 09:00–18:00
    assert "Пн 07.09 09:00–18:00" in text, f"Mon should be overwritten; got: {text!r}"
    assert "Ср 09.09 09:00–18:00" in text, f"Wed should be overwritten; got: {text!r}"
    assert "Пт 11.09 09:00–18:00" in text, f"Fri should be created; got: {text!r}"

    # Verify DB state — WorkDay rows updated to 09:00–18:00
    async with session_factory() as session:
        mon_wd = await select_workday(session, master_id, datetime(2026, 9, 7).date())
        wed_wd = await select_workday(session, master_id, datetime(2026, 9, 9).date())
        fri_wd = await select_workday(session, master_id, datetime(2026, 9, 11).date())
    assert mon_wd is not None and str(mon_wd.start_time) == "09:00:00", "Mon overwritten"
    assert wed_wd is not None and str(wed_wd.end_time) == "18:00:00", "Wed overwritten"
    assert fri_wd is not None, "Fri created"


@pytest.mark.asyncio
@freeze_time("2026-09-06 14:00:00", tz_offset=0)
async def test_openweek_overwrite_no_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """B: [❌ Нет, отмена] clears state, answers 'Открытие недели отменено'."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_openweek_overwrite_no"
    state = _make_mock_state(
        {
            "picked_start_minute": 540,
            "picked_end_minute": 1080,
            "selected_weekdays": [0, 2, 4],
            "business_tz": TZ,
        }
    )

    await admin_handlers.admin_openweek_overwrite_no_cb(callback, state)

    state.clear.assert_called_once()
    text = callback_answer_text(callback)
    assert "Открытие недели отменено" in text, f"Should say cancelled; got: {text!r}"


@pytest.mark.asyncio
@freeze_time("2026-09-06 14:00:00", tz_offset=0)
async def test_openweek_confirm_silent_when_no_existing(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """B regression: if NO existing WorkDays among selected, confirm applies
    silently (current behavior, no alert). Avoid false-positive alerts when
    opening a fresh week.

    Setup: no WorkDay seeded. Select Mon+Wed. Confirm with 09:00–18:00.
    Expected: silent apply, summary with ✅ lines, state.clear called, no
    "Уже открыты" alert.
    """
    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_openweek_confirm"
    state = _make_mock_state(
        {
            "picked_start_minute": 540,  # 09:00
            "picked_end_minute": 1080,  # 18:00
            "selected_weekdays": [0, 2],  # Mon, Wed
            "business_tz": TZ,
        }
    )

    await admin_handlers.admin_openweek_confirm_cb(callback, state)

    state.clear.assert_called_once()
    text = callback_answer_text(callback)
    assert "Уже есть окно" not in text, f"Should NOT warn when no existing; got: {text!r}"
    assert "Пн 07.09 09:00–18:00" in text, f"Mon should be applied; got: {text!r}"
    assert "Ср 09.09 09:00–18:00" in text, f"Wed should be applied; got: {text!r}"


@pytest.mark.asyncio
@freeze_time("2026-09-06 14:00:00", tz_offset=0)
async def test_openweek_confirm_alert_marks_closed_days(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """B W1 fix: alert marks closed days (is_active=False) with "(закрыт)" suffix.

    Setup: Mon 07.09 closed (is_active=False), Wed 09.09 active. Select both.
    Expected: alert lists "Пн 07.09 10:00–19:00 (закрыт)" and "Ср 09.09 10:00–19:00"
    (no suffix). Master sees closed status explicitly — no confusion between
    "already open" and "closed, can re-open".
    """
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        await _seed_workday(
            session,
            ctx=ctx,
            work_date=datetime(2026, 9, 7).date(),
            start_time_str="10:00",
            end_time_str="19:00",
            is_active=False,  # closed via /closeday
        )
        await _seed_workday(
            session,
            ctx=ctx,
            work_date=datetime(2026, 9, 9).date(),
            start_time_str="10:00",
            end_time_str="19:00",
            is_active=True,
        )

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_openweek_confirm"
    state = _make_mock_state(
        {
            "picked_start_minute": 540,
            "picked_end_minute": 1080,
            "selected_weekdays": [0, 2],  # Mon (closed), Wed (active)
            "business_tz": TZ,
        }
    )

    await admin_handlers.admin_openweek_confirm_cb(callback, state)

    state.clear.assert_not_called()
    text = callback_answer_text(callback)
    assert "Пн 07.09 10:00–19:00 (закрыт)" in text, f"Closed day should be marked; got: {text!r}"
    assert "Ср 09.09 10:00–19:00 (закрыт)" not in text, (
        f"Active day should NOT have (закрыт) suffix; got: {text!r}"
    )


@pytest.mark.asyncio
@freeze_time("2026-09-06 14:00:00", tz_offset=0)
async def test_openweek_confirm_alert_all_closed_reopen_text(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """5.60 P1: all selected existing days are closed → alert 'День закрыт.
    Открыть заново' + button '✅ Да, открыть' (NOT 'перезаписать').

    Setup: Mon 07.09 and Wed 09.09 both closed (is_active=False). Select both.
    Confirm with new window 09:00–18:00. Expected: alert title 'День закрыт',
    action text 'Открыть заново на 09:00–18:00?', confirm button labeled
    '✅ Да, открыть'. callback_data stays 'admin_openweek_overwrite_yes' so
    yes-handler logic is unchanged.
    """
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        await _seed_workday(
            session,
            ctx=ctx,
            work_date=datetime(2026, 9, 7).date(),
            start_time_str="10:00",
            end_time_str="19:00",
            is_active=False,
        )
        await _seed_workday(
            session,
            ctx=ctx,
            work_date=datetime(2026, 9, 9).date(),
            start_time_str="10:00",
            end_time_str="19:00",
            is_active=False,
        )

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_openweek_confirm"
    state = _make_mock_state(
        {
            "picked_start_minute": 540,
            "picked_end_minute": 1080,
            "selected_weekdays": [0, 2],
            "business_tz": TZ,
        }
    )

    await admin_handlers.admin_openweek_confirm_cb(callback, state)

    state.clear.assert_not_called()
    text = callback_answer_text(callback)
    assert "День закрыт" in text, f"All-closed alert should say 'День закрыт'; got: {text!r}"
    assert "Открыть заново на 09:00–18:00" in text, (
        f"All-closed alert should propose re-open; got: {text!r}"
    )
    assert "Перезаписать" not in text, (
        f"All-closed alert must NOT say 'Перезаписать'; got: {text!r}"
    )
    # Verify confirm button label — extract from edit_text or answer reply_markup.
    args, kwargs = callback.message.edit_text.call_args or callback.message.answer.call_args
    reply_markup = kwargs.get("reply_markup") or (args[1] if len(args) > 1 else None)
    assert isinstance(reply_markup, InlineKeyboardMarkup), "Should have overwrite keyboard"
    button_texts = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "✅ Да, открыть" in button_texts, (
        f"Confirm button should say '✅ Да, открыть'; got: {button_texts}"
    )
    assert "✅ Да, перезаписать" not in button_texts, (
        f"All-closed must NOT show 'перезаписать' button; got: {button_texts}"
    )


@pytest.mark.asyncio
@freeze_time("2026-09-06 14:00:00", tz_offset=0)
async def test_openweek_confirm_alert_mixed_open_and_overwrite_text(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """5.60 P1: mixed (some closed, some active) → alert 'Часть дней закрыта,
    часть открыта' + button '✅ Да, открыть/перезаписать'.

    Setup: Mon 07.09 closed, Wed 09.09 active. Select both. Confirm 09–18.
    Expected: alert 'Часть дней закрыта, часть открыта', action 'Открыть
    закрытые и перезаписать активные на 09:00–18:00?', button '✅ Да,
    открыть/перезаписать'.
    """
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        await _seed_workday(
            session,
            ctx=ctx,
            work_date=datetime(2026, 9, 7).date(),
            start_time_str="10:00",
            end_time_str="19:00",
            is_active=False,
        )
        await _seed_workday(
            session,
            ctx=ctx,
            work_date=datetime(2026, 9, 9).date(),
            start_time_str="10:00",
            end_time_str="19:00",
            is_active=True,
        )

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_openweek_confirm"
    state = _make_mock_state(
        {
            "picked_start_minute": 540,
            "picked_end_minute": 1080,
            "selected_weekdays": [0, 2],
            "business_tz": TZ,
        }
    )

    await admin_handlers.admin_openweek_confirm_cb(callback, state)

    state.clear.assert_not_called()
    text = callback_answer_text(callback)
    assert "Часть дней закрыта, часть открыта" in text, (
        f"Mixed alert should explain both states; got: {text!r}"
    )
    assert "Открыть закрытые и перезаписать активные на 09:00–18:00" in text, (
        f"Mixed alert should propose both actions; got: {text!r}"
    )
    # Verify button label.
    args, kwargs = callback.message.edit_text.call_args or callback.message.answer.call_args
    reply_markup = kwargs.get("reply_markup") or (args[1] if len(args) > 1 else None)
    assert isinstance(reply_markup, InlineKeyboardMarkup), "Should have overwrite keyboard"
    button_texts = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "✅ Да, открыть/перезаписать" in button_texts, (
        f"Mixed button should say '✅ Да, открыть/перезаписать'; got: {button_texts}"
    )


@pytest.mark.asyncio
@freeze_time("2026-09-06 14:00:00", tz_offset=0)
async def test_openweek_confirm_alert_all_active_button_text_unchanged(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """5.60 P1 backward compat: all selected existing days active → button
    '✅ Да, перезаписать' (default, unchanged). Guards against regression
    of the original overwrite flow when no closed days are involved.
    """
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        await _seed_workday(
            session,
            ctx=ctx,
            work_date=datetime(2026, 9, 7).date(),
            start_time_str="10:00",
            end_time_str="19:00",
            is_active=True,
        )
        await _seed_workday(
            session,
            ctx=ctx,
            work_date=datetime(2026, 9, 9).date(),
            start_time_str="10:00",
            end_time_str="19:00",
            is_active=True,
        )

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_openweek_confirm"
    state = _make_mock_state(
        {
            "picked_start_minute": 540,
            "picked_end_minute": 1080,
            "selected_weekdays": [0, 2],
            "business_tz": TZ,
        }
    )

    await admin_handlers.admin_openweek_confirm_cb(callback, state)

    state.clear.assert_not_called()
    text = callback_answer_text(callback)
    assert "Уже есть окно" in text, f"All-active alert keeps current title; got: {text!r}"
    assert "Перезаписать окно на 09:00–18:00" in text, (
        f"All-active alert keeps current action; got: {text!r}"
    )
    args, kwargs = callback.message.edit_text.call_args or callback.message.answer.call_args
    reply_markup = kwargs.get("reply_markup") or (args[1] if len(args) > 1 else None)
    assert isinstance(reply_markup, InlineKeyboardMarkup), "Should have overwrite keyboard"
    button_texts = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "✅ Да, перезаписать" in button_texts, (
        f"All-active keeps 'перезаписать' button; got: {button_texts}"
    )


# ============================================================
# /openweek per-day edit (Session 5.28 D — Variant D)
# ============================================================


@pytest.mark.asyncio
@freeze_time("2026-09-06 14:00:00", tz_offset=0)
async def test_openweek_apply_renders_edit_keyboard(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """D: after silent apply (no existing), summary shows [✏️ Пн] [✏️ Ср] ...
    [✅ Готово] keyboard under text.

    Setup: no existing WorkDays. Select Mon+Wed. Confirm 09:00–18:00.
    Expected: summary text + reply_markup with [✏️ Пн] [✏️ Ср] [✅ Готово]
    buttons (AdminOpenweekEditCallbackData for edit, "admin_openweek_done"
    string for done).
    """
    from aiogram.types import InlineKeyboardMarkup

    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_openweek_confirm"
    state = _make_mock_state(
        {
            "picked_start_minute": 540,
            "picked_end_minute": 1080,
            "selected_weekdays": [0, 2],  # Mon, Wed
            "business_tz": TZ,
        }
    )

    await admin_handlers.admin_openweek_confirm_cb(callback, state)

    state.clear.assert_called_once()
    text = callback_answer_text(callback)
    assert "Пн 07.09 09:00–18:00" in text, f"Mon should be in summary; got: {text!r}"
    assert "Ср 09.09 09:00–18:00" in text, f"Wed should be in summary; got: {text!r}"
    # Verify edit keyboard rendered
    args, kwargs = callback.message.edit_text.call_args
    reply_markup = kwargs.get("reply_markup") or (args[1] if len(args) > 1 else None)
    assert isinstance(reply_markup, InlineKeyboardMarkup), "Should have edit keyboard"
    buttons = [btn for row in reply_markup.inline_keyboard for btn in row]
    button_texts = [btn.text for btn in buttons]
    assert any("✏️ Пн" in t for t in button_texts), f"Should have [✏️ Пн] button; got: {button_texts}"
    assert any("✏️ Ср" in t for t in button_texts), f"Should have [✏️ Ср] button; got: {button_texts}"
    assert any("Готово" in t for t in button_texts), (
        f"Should have [✅ Готово] button; got: {button_texts}"
    )


@pytest.mark.asyncio
@freeze_time("2026-09-06 14:00:00", tz_offset=0)
async def test_openweek_edit_cb_starts_picker(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """D: [✏️ Пн] (state=None) → set_state(opening_week_edit_start), save
    edit_weekday/edit_workday_id/edit_monday_iso in state, show start picker.

    Setup: seed WorkDay for Mon 07.09 10-19. Tap [✏️ Пн].
    Expected: state.set_state(opening_week_edit_start), state.update_data with
    edit_weekday=0, edit_workday_id=<UUID str>, edit_monday_iso="2026-09-07",
    business_tz=TZ. callback.message.answer called with picker keyboard
    (mode="start").
    """
    from bot.keyboards.admin import AdminOpenweekEditCallbackData

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        await _seed_workday(
            session,
            ctx=ctx,
            work_date=datetime(2026, 9, 7).date(),
            start_time_str="10:00",
            end_time_str="19:00",
        )

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = AdminOpenweekEditCallbackData(weekday=0, work_date_iso="2026-09-07").pack()
    state = _make_mock_state()  # state=None initially (post-apply)

    await admin_handlers.admin_openweek_edit_cb(
        callback,
        AdminOpenweekEditCallbackData(weekday=0, work_date_iso="2026-09-07"),
        state,
    )

    state.set_state.assert_called_once_with(admin_handlers.AdminStates.opening_week_edit_start)
    update = _state_data_passed(state)
    assert update["edit_weekday"] == 0, f"Should save edit_weekday=0; got: {update}"
    assert "edit_workday_id" in update, f"Should save edit_workday_id; got: {update}"
    assert update["edit_monday_iso"] == "2026-09-07", (
        f"Should save monday ISO (=work_date's Monday); got: {update}"
    )
    # Picker rendered with day label (UX improvement S3)
    args, kwargs = callback.message.answer.call_args
    text = args[0] if args else kwargs.get("text", "")
    assert "Редактирование Пн 07.09" in text, f"Should show day label in picker text; got: {text!r}"


@pytest.mark.asyncio
@freeze_time("2026-09-06 14:00:00", tz_offset=0)
async def test_openweek_edit_end_applies_update_workday(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """D: end picker tap → state.clear() → update_workday → re-render summary
    with new window for that day.

    Setup: WorkDay Mon 07.09 10-19. User taps [✏️ Пн] → start picker → 09:00 →
    end picker → 18:00. Simulate end picker tap (state pre-populated with
    edit_workday_id, edit_monday_iso, edit_picked_start_minute=540).

    Expected: state.clear called, update_workday called with workday_id,
    09:00, 18:00. Re-render summary shows "Пн 07.09 09:00–18:00".
    """
    from bot.keyboards.admin import AdminWindowSlot30CallbackData
    from bot.services.workday import select_workday

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        master_id = ctx["master_id"]
        wd = await _seed_workday(
            session,
            ctx=ctx,
            work_date=datetime(2026, 9, 7).date(),
            start_time_str="10:00",
            end_time_str="19:00",
        )
        workday_id = wd.id

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = AdminWindowSlot30CallbackData(
        workday_id=workday_id,
        start_minute=1080,  # 18:00
    ).pack()
    state = _make_mock_state(
        {
            "edit_weekday": 0,
            "edit_workday_id": str(workday_id),
            "edit_monday_iso": "2026-09-07",
            "edit_picked_start_minute": 540,  # 09:00
            "business_tz": TZ,
        }
    )

    await admin_handlers.admin_openweek_edit_end_cb(
        callback,
        AdminWindowSlot30CallbackData(workday_id=workday_id, start_minute=1080),
        state,
    )

    state.clear.assert_called_once()
    # Verify DB state — WorkDay updated to 09:00–18:00
    async with session_factory() as session:
        wd_after = await select_workday(session, master_id, datetime(2026, 9, 7).date())
    assert wd_after is not None, "WorkDay should still exist"
    assert str(wd_after.start_time) == "09:00:00", (
        f"Start should be 09:00; got: {wd_after.start_time}"
    )
    assert str(wd_after.end_time) == "18:00:00", f"End should be 18:00; got: {wd_after.end_time}"
    # Re-render summary
    text = callback_answer_text(callback)
    assert "Пн 07.09 09:00–18:00" in text, f"Summary should show new window; got: {text!r}"


@pytest.mark.asyncio
@freeze_time("2026-09-06 14:00:00", tz_offset=0)
async def test_openweek_edit_end_shows_shrink_error(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """D: shrink-check protects existing bookings — edit to narrower window
    that conflicts with booking → alert in summary, WorkDay unchanged.

    Setup: WorkDay Mon 07.09 09-19, booking 13:00 (Стрижка, 60 min). Try to
    edit to 09:00–12:00 (shrink past 13:00 booking) → WorkDayShrinkError.
    Expected: summary shows "❌ Нельзя сузить" + conflict details, WorkDay
    unchanged (still 09-19), edit keyboard re-rendered for retry.
    """
    from bot.keyboards.admin import AdminWindowSlot30CallbackData
    from bot.services.workday import select_workday

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        master_id = ctx["master_id"]
        wd = await _seed_workday(
            session,
            ctx=ctx,
            work_date=datetime(2026, 9, 7).date(),
            start_time_str="09:00",
            end_time_str="19:00",
        )
        workday_id = wd.id
        # Booking at 13:00 Moscow (conflicts with shrink to 09-12)
        booking_local = datetime(2026, 9, 7, 13, 0, tzinfo=ZoneInfo(TZ))
        slot = await _seed_slot(
            session,
            master_id=master_id,
            slot_date=booking_local.date(),
            hour=13,
            status="open",
        )
        await _seed_booking(
            session,
            ctx=ctx,
            slot=slot,
            start_at_utc_naive=_local_to_utc_naive(booking_local),
            status="confirmed",
        )

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = AdminWindowSlot30CallbackData(
        workday_id=workday_id,
        start_minute=720,  # 12:00 — shrink to 09:00–12:00
    ).pack()
    state = _make_mock_state(
        {
            "edit_weekday": 0,
            "edit_workday_id": str(workday_id),
            "edit_monday_iso": "2026-09-07",
            "edit_picked_start_minute": 540,  # 09:00
            "business_tz": TZ,
        }
    )

    await admin_handlers.admin_openweek_edit_end_cb(
        callback,
        AdminWindowSlot30CallbackData(workday_id=workday_id, start_minute=720),
        state,
    )

    state.clear.assert_called_once()
    text = callback_answer_text(callback)
    assert "Нельзя сузить" in text, f"Should show shrink alert; got: {text!r}"
    # WorkDay unchanged
    async with session_factory() as session:
        wd_after = await select_workday(session, master_id, datetime(2026, 9, 7).date())
    assert wd_after is not None and str(wd_after.start_time) == "09:00:00", "Start unchanged"
    assert str(wd_after.end_time) == "19:00:00", (
        f"End unchanged (still 19:00); got: {wd_after.end_time}"
    )


@pytest.mark.asyncio
@freeze_time("2026-09-06 14:00:00", tz_offset=0)
async def test_openweek_done_cb_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """D: [✅ Готово] (state=None) → state.clear (defensive) + show /menu."""
    async with session_factory() as session:
        await _seed_admin_stack(session)

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = "admin_openweek_done"
    state = _make_mock_state()

    await admin_handlers.admin_openweek_done_cb(callback, state)

    state.clear.assert_called_once()
    text = callback_answer_text(callback)
    assert "/menu" in text, f"Should show /menu hint; got: {text!r}"


@pytest.mark.asyncio
@freeze_time("2026-09-06 14:00:00", tz_offset=0)
async def test_openweek_edit_cb_alerts_for_past_day(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """D: [✏️ Пн] where Пн is past → alert "Прошедшая дата", no state change.

    Edge: if user has stale edit keyboard from previous week and taps [✏️ Пн]
    where Пн is now past (e.g. next week became current week), handler refuses.
    """
    # freeze: Sunday 06.09 → next week is 07.09-13.09 (Mon=07 is FUTURE).
    # To test past-day skip, use a different freeze (mid-week, where Mon past).
    pass  # placeholder — see test below with different freeze_time


@pytest.mark.asyncio
@freeze_time("2026-09-09 14:00:00", tz_offset=0)  # Wednesday 09.09 → Mon 07.09 past
async def test_openweek_edit_cb_alerts_for_past_day_wed(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """D: tap [✏️ Пн] where Mon 07.09 is past (today Wed 09.09) → alert.
    _current_week_monday on Wed returns Mon 07.09 (current week) — work_date
    = 07.09 < today_local (09.09) → past-day guard triggers.
    """
    from bot.keyboards.admin import AdminOpenweekEditCallbackData

    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        await _seed_workday(
            session,
            ctx=ctx,
            work_date=datetime(2026, 9, 7).date(),
            start_time_str="10:00",
            end_time_str="19:00",
        )

    callback = _make_callback(ADMIN_TG_ID)
    callback.data = AdminOpenweekEditCallbackData(weekday=0, work_date_iso="2026-09-07").pack()
    state = _make_mock_state()

    await admin_handlers.admin_openweek_edit_cb(
        callback,
        AdminOpenweekEditCallbackData(weekday=0, work_date_iso="2026-09-07"),
        state,
    )

    # state NOT changed (alert, no set_state)
    state.set_state.assert_not_called()
    # alert shown via callback.answer (show_alert=True)
    callback.answer.assert_called_once()
    args, kwargs = callback.answer.call_args
    alert_text = args[0] if args else kwargs.get("text", "")
    assert "Прошедшая дата" in alert_text, f"Should alert past day; got: {alert_text!r}"


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


# ============================================================
# Session 5.46 (B.10) — phone in /today + /week render
# _render_bookings: client_phones dict → "📞 +7..." or "без телефона" suffix
# cmd_today: end-to-end booking with Client.phone set → render shows phone
# ============================================================


def test_render_bookings_with_phone_suffix() -> None:
    """B.10: _render_bookings(client_phones={client_id: "+79991234567"}) →
    booking line ends with ", 📞 +79991234567". Phone is rendered RAW (no escape)
    — phone column is deprecated (5.50), historical rows are digits+'+' only.
    """
    from typing import cast

    client_id = UUID("11111111-1111-1111-1111-111111111111")
    bookings = cast(
        list[Booking],
        [
            MagicMock(
                start_at=datetime(2026, 3, 17, 11, 0, tzinfo=UTC),
                client_name_snapshot="Паша",
                service_title_snapshot="Стрижка",
                client_id=client_id,
            ),
        ],
    )

    result = admin_handlers._render_bookings(
        "📅 Записи:", bookings, TZ, client_phones={client_id: "+79991234567"}
    )

    bullet_line = [line for line in result.split("\n") if line.startswith("•")][0]
    assert "📞 +79991234567" in bullet_line, "phone suffix shown when phone is set"


def test_render_bookings_without_phone_shows_bez_telefona() -> None:
    """B.10: _render_bookings(client_phones={client_id: None}) → booking line
    ends with ", без телефона" (NOT ", 📞 None"). The 'bez telefona' suffix
    is rendered when client_phones dict has the client_id key but value is None.
    """
    from typing import cast

    client_id = UUID("22222222-2222-2222-2222-222222222222")
    bookings = cast(
        list[Booking],
        [
            MagicMock(
                start_at=datetime(2026, 3, 17, 11, 0, tzinfo=UTC),
                client_name_snapshot="Иван",
                service_title_snapshot="Стрижка",
                client_id=client_id,
            ),
        ],
    )

    result = admin_handlers._render_bookings(
        "📅 Записи:", bookings, TZ, client_phones={client_id: None}
    )

    bullet_line = [line for line in result.split("\n") if line.startswith("•")][0]
    assert "без телефона" in bullet_line, "None phone → 'без телефона' suffix"
    assert "📞 None" not in bullet_line, "no literal 'None' in render"
    assert "📞" not in bullet_line, "no phone icon when phone is None"


def test_render_bookings_client_phones_none_omits_suffix() -> None:
    """B.10 backwards compat: _render_bookings(client_phones=None) → no phone
    suffix in the render. Legacy callers (/closeday, etc.) that haven't been
    updated to pass client_phones still get the old format — phone column
    omitted entirely, NOT "без телефона".
    """
    from typing import cast

    bookings = cast(
        list[Booking],
        [
            MagicMock(
                start_at=datetime(2026, 3, 17, 11, 0, tzinfo=UTC),
                client_name_snapshot="Олег",
                service_title_snapshot="Укладка",
                client_id=UUID("33333333-3333-3333-3333-333333333333"),
            ),
        ],
    )

    result = admin_handlers._render_bookings("📅 Записи:", bookings, TZ, client_phones=None)

    bullet_line = [line for line in result.split("\n") if line.startswith("•")][0]
    assert "📞" not in bullet_line, "no phone icon when client_phones=None (legacy callers)"
    assert "без телефона" not in bullet_line, "no 'без телефона' when caller didn't pass dict"


@pytest.mark.asyncio
async def test_cmd_today_with_client_phone(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """B.10 end-to-end: /today shows "📞 +7999..." when Client.phone is set.
    Seed a booking with a Client that has phone='+79991234567', invoke
    cmd_today → render shows the phone suffix in the bullet line.
    """
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        # Set phone on the client (mirror what create_booking would do via
        # payload.phone — direct UPDATE here, no create_booking needed since
        # we test the render, not the booking flow).
        client = ctx["client"]
        client.phone = "+79991234567"
        await session.commit()

        # Today's booking at 14:00 Moscow.
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

    text = _answer_text(msg)
    assert "📞 +79991234567" in text, "phone shown in /today when Client.phone set"


@pytest.mark.asyncio
async def test_cmd_today_without_phone_shows_bez_telefona(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """B.10 end-to-end: /today shows "без телефона" when Client.phone is None.
    Mirror of test_cmd_today_with_client_phone, but Client.phone stays None
    (the conftest default — no fixture value). Render shows "без телефона" suffix.
    """
    async with session_factory() as session:
        ctx = await _seed_admin_stack(session)
        # Client.phone stays None (default — no UPDATE)
        now_local = datetime.now(ZoneInfo(TZ))
        today_local_at_15 = now_local.replace(hour=15, minute=0, second=0, microsecond=0)
        slot = await _seed_slot(
            session,
            master_id=ctx["master_id"],
            slot_date=today_local_at_15.date(),
            hour=15,
            status="open",
        )
        await _seed_booking(
            session,
            ctx=ctx,
            slot=slot,
            start_at_utc_naive=_local_to_utc_naive(today_local_at_15),
            status="confirmed",
        )

    msg = _make_message(user_id=ADMIN_TG_ID, text="/today")
    await admin_handlers.cmd_today(msg)

    text = _answer_text(msg)
    assert "без телефона" in text, "phone=None → 'без телефона' in /today render"
    assert "📞 None" not in text, "no literal 'None' in render"


# ============================================================
# Этап 3.5 — admin_no_state_catchall_text (SkipHandler contract, 5.52)
# ============================================================


@pytest.mark.asyncio
async def test_admin_no_state_catchall_text_admin_gets_menu_hint() -> None:
    """Этап 3.5: админ в State(None) + произвольный текст → /menu hint
    (а не клиентский "Начните запись через /book" — был баг smoke-теста 5.9).
    """
    msg = _make_message(user_id=ADMIN_TG_ID, text="12")

    await admin_handlers.admin_no_state_catchall_text(msg)

    msg.answer.assert_awaited_once()
    assert "/menu" in _answer_text(msg)


@pytest.mark.asyncio
async def test_admin_no_state_catchall_text_non_admin_raises_skip_handler() -> None:
    """5.52: non-admin → SkipHandler + НЕ отвечает сам.

    Прет-фикс поведение (Session 5.9 → 5.52): plain `return` здесь молча
    съедал update (aiogram: первый сматченный handler = обработано, никакого
    проваливания) → любой клиент в State(None), набравший текст, не получал
    ответа ВООБЩЕ — client_router.no_state_fallback был мёртв для non-admin.
    SkipHandler — единственный способ отказаться от сматченного handler'а:
    dispatch продолжается к client_router (пинован интеграционным тестом
    test_cancel_command_works_in_service_step: "ещё текст" после /cancel →
    "Начните запись через /book").
    """
    from aiogram.dispatcher.event.bases import SkipHandler

    msg = _make_message(user_id=NON_ADMIN_TG_ID, text="привет")

    with pytest.raises(SkipHandler):
        await admin_handlers.admin_no_state_catchall_text(msg)

    # Handler сам НЕ отвечает — ответ даёт следующий handler (client_router).
    msg.answer.assert_not_awaited()
