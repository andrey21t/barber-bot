"""Tests for bot.handlers.client — mybookings_msg + mybookings_cancel_cb.

Coverage (NEXT_SESSION_PROMPT.md Приоритет 1 — handler gaps surfaced by self-review):
- mybookings_msg: cancelable booking shows [Отменить] inline button (test #4)
- mybookings_msg: too-late booking shows "⏰ Отмена недоступна" + NO button (test #5)
- mybookings_msg: no bookings → "У вас нет активных записей" (test #6)
- mybookings_cancel_cb: happy path → booking.status='cancelled' + master notified (test #7)
- mybookings_cancel_cb: too-late → CancelTooLateError → "Отмена возможна только за 24+" (test #8)
- mybookings_cancel_cb: not-owner (stranger telegram_id) → "Запись не найдена" (test #9)
- mybookings_keyboard: N bookings → N buttons with correct callback_data (test #10)

Why handler tests (not just service tests, AGENTS.md § anti-overengineering rule 3):
mybookings_msg contains a partition decision (cancelable vs too-late) computed in
the handler — NOT in the service. Bug in `mybookings_msg:441` (naive vs aware datetime
comparison) shipped because handler logic was not covered by any test. Display
math that mirrors service invariants is a logic-change risk, not pure I/O.

Pattern (NEXT_SESSION_PROMPT.md 38): direct handler invocation with mock Message/
CallbackQuery + monkeypatch of `bot.handlers.client.async_session_factory` so the
handler reads/writes our in-memory test SQLite engine (NOT the global prod engine
pointed at by bot.db.async_session_factory). Avoids `dp.feed_update` ceremony —
no Dispatcher/MemoryStorage/router-wiring needed for these stateless handlers
(both mybookings_msg and mybookings_cancel_cb use StateFilter(None) only).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bot.handlers import client as client_handlers
from bot.keyboards.client import (
    MyBookingsCancelCallbackData,
    mybookings_keyboard,
)
from bot.models import Booking, Business, Client, Master, Slot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# ============================================================
# Fixtures — patch async_session_factory, mock Telegram objects
# ============================================================


@pytest.fixture
def patched_session_factory(
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Replace `bot.handlers.client.async_session_factory` with the test engine's
    session factory so handler DB calls hit in-memory SQLite.

    `async_session_factory` is imported into `bot.handlers.client` at module load
    (line 34). Patching the handler module's attribute (NOT bot.db) is what makes
    the test engine take effect — re-importing bot.db wouldn't help here because
    the handler already has its own reference.
    """
    monkeypatch.setattr(client_handlers, "async_session_factory", session_factory)
    return session_factory


@pytest.fixture
def mock_scheduler() -> MagicMock:
    """AsyncIOScheduler mock — remove_job is sync, so MagicMock (not AsyncMock)."""
    return MagicMock(spec=AsyncIOScheduler)


def _make_user(user_id: int) -> User:
    """Build a minimal aiogram User (required fields per Bot API)."""
    return User(id=user_id, is_bot=False, first_name="Test")


def _make_message(
    user_id: int,
    text: str = "/mybookings",
) -> MagicMock:
    """Mock aiogram.Message with the fields mybookings_msg reads.

    mybookings_msg touches: message.from_user.id, message.answer (async).
    Other Message fields are left as MagicMock defaults (spec=Message blocks
    unknown attribute access, but `from_user` and `answer` we set explicitly).
    """
    msg = MagicMock(spec=Message)
    msg.from_user = _make_user(user_id)
    msg.text = text
    msg.answer = AsyncMock()
    return msg


def _make_callback(
    user_id: int,
    booking_id: UUID,
    bot: AsyncMock | None = None,
) -> tuple[MagicMock, MyBookingsCancelCallbackData]:
    """Mock aiogram.CallbackQuery for mybookings_cancel_cb + matching callback_data.

    mybookings_cancel_cb reads: callback.from_user.id, callback.message (optional
    but present in tests), callback.answer (async), callback.bot.send_message (async).
    """
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(user_id)
    cb.message = _make_message(user_id, text="<unused for cancel cb>")
    cb.answer = AsyncMock()
    cb.bot = bot or AsyncMock()
    cb.bot.send_message = AsyncMock()
    callback_data = MyBookingsCancelCallbackData(booking_id=booking_id)
    return cb, callback_data


def _answer_text(mock_msg: MagicMock) -> str:
    """Extract `message.answer(text, ...)` first positional arg.

    All `message.answer` calls in client.py pass text POSITIONALLY (not as kwarg).
    Helper centralizes the access pattern so each test reads cleanly.
    """
    args = mock_msg.answer.call_args.args
    text: str = args[0] if args else mock_msg.answer.call_args.kwargs.get("text", "")
    return text


def _answer_reply_markup(mock_msg: MagicMock) -> Any:
    """Extract `reply_markup` kwarg from message.answer call (None if not passed)."""
    return mock_msg.answer.call_args.kwargs.get("reply_markup")


async def _seed_full_stack(
    session: AsyncSession,
    *,
    client_telegram_id: int = 111222333,
) -> dict[str, Any]:
    """Insert business + master + client (no slot/booking yet) — for handlers that
    resolve client by telegram_id before listing bookings.

    Mirrors conftest.seed_data but lets each test add its own slot/booking combo
    with chosen start_at (instead of seed_data's fixed tomorrow-14:00 slot).
    """
    biz = Business(name="Test Barbershop", telegram_owner_id=461355056, timezone="Europe/Moscow")
    session.add(biz)
    await session.flush()

    master = Master(
        business_id=biz.id, name="Екатерина", telegram_id=461355056, role="owner"
    )
    session.add(master)
    await session.flush()

    client = Client(telegram_id=client_telegram_id, name="Паша")
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


async def _seed_booking(
    session: AsyncSession,
    *,
    ctx: dict[str, Any],
    start_at_local: datetime,
    status: str = "confirmed",
    client_id: UUID | None = None,
) -> Booking:
    """Insert Slot + Booking with chosen start_at (LOCAL tz-aware datetime).

    `start_at_local` is tz-aware LOCAL (e.g. datetime(2026,3,15,14, tzinfo=MSK));
    we convert to naive UTC for SQLite storage (pattern from test_admin.py _utc_naive).
    Slot.slot_date/.slot_hour are derived from start_at_local (LOCAL calendar).
    """
    tz = ZoneInfo("Europe/Moscow")
    local_aware = start_at_local if start_at_local.tzinfo else start_at_local.replace(tzinfo=tz)
    start_at_utc_naive = local_aware.astimezone(UTC).replace(tzinfo=None)

    slot = Slot(
        master_id=ctx["master_id"],
        slot_date=local_aware.date(),
        slot_hour=local_aware.hour,
        status="booked" if status in ("confirmed", "transferred") else "open",
    )
    session.add(slot)
    await session.flush()

    booking = Booking(
        slot_id=slot.id,
        business_id=ctx["business_id"],
        master_id=ctx["master_id"],
        client_id=client_id or ctx["client_id"],
        service_id=None,
        service_title_snapshot="Стрижка",
        service_price_snapshot=None,
        client_name_snapshot="Паша",
        start_at=start_at_utc_naive,
        end_at=start_at_utc_naive + timedelta(minutes=60),
        status=status,
    )
    session.add(booking)
    await session.commit()
    return booking


# ============================================================
# mybookings_msg — list bookings + cancelable/too-late partition (test #4-6)
# ============================================================


@pytest.mark.asyncio
async def test_mybookings_msg_with_cancelable_booking(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Test #4 (NEXT_SESSION_PROMPT.md): booking 3 days ahead → cancelable →
    inline [Отменить] button appears in /mybookings response.

    This test would have caught the naive-datetime bug fixed in commit da66f01
    (handler compared `now_utc` aware vs `booking.start_at` naive from SQLite
    → partition was wrong: cancelable bookings flagged as too-late or vice-versa).
    The fix stripped tzinfo from now_utc (line 431); this test locks that contract.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        # Booking 3 days ahead at 14:00 Moscow — deadline (= start_at - 24h = 2 days
        # ahead) is always in the future relative to now, regardless of time of day.
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=future_local)

    # Sanity: booking exists, start_at is naive UTC in SQLite.
    async with session_factory() as verify_session:
        b = (await verify_session.execute(
            select(Booking).where(Booking.id == booking.id)
        )).scalar_one()
        assert b.status == "confirmed"
        assert b.start_at.tzinfo is None  # SQLite stores naive

    msg = _make_message(user_id=111222333, text="/mybookings")
    await client_handlers.mybookings_msg(msg)

    # message.answer called once with the bookings list + inline keyboard.
    msg.answer.assert_called_once()
    text = _answer_text(msg)
    assert "📋 Ваши записи:" in text
    assert "Стрижка" in text

    reply_markup = _answer_reply_markup(msg)
    assert isinstance(reply_markup, InlineKeyboardMarkup), (
        "cancelable booking must produce an inline keyboard with [Отменить] button"
    )
    # InlineKeyboardMarkup has .inline_keyboard: list[list[InlineKeyboardButton]]
    buttons = [btn for row in reply_markup.inline_keyboard for btn in row]
    assert len(buttons) == 1, "exactly one [Отменить] button for one cancelable booking"
    button = buttons[0]
    assert "Отменить" in button.text
    # callback_data packs MyBookingsCancelCallbackData(booking_id=<uuid>)
    # InlineKeyboardButton.callback_data is typed `str | None` in aiogram stubs;
    # mybookings_keyboard always packs a real callback_data string, so assert
    # non-None before unpack to satisfy mypy without losing the runtime check.
    assert button.callback_data is not None
    cb_data = MyBookingsCancelCallbackData.unpack(button.callback_data)
    assert cb_data.booking_id == booking.id


@pytest.mark.asyncio
async def test_mybookings_msg_with_too_late_booking(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Test #5: booking within 24h → too-late → "⏰ Отмена недоступна" in text,
    NO inline keyboard (cancel window closed per spec.md 406 — 24h rule).

    start_at = +12h (LOCAL) — chosen to be robust on both Render (UTC system TZ)
    AND dev (Europe/Moscow TZ): `get_client_bookings` filters via
    `datetime.now(tz=None)` (admin.py:152) which returns NAIVE LOCAL TIME (not UTC),
    a pre-existing warning documented in NEXT_SESSION_PROMPT.md:137. The handler
    partition (mybookings_msg:430-431) is correct — uses `datetime.now(UTC)` then
    strips tzinfo to naive UTC, fixed in commit da66f01.

    With start_at = +12h:
      - Render (system TZ=UTC): now_local == now_utc, filter passes (12h>0).
      - dev (system TZ=MSK): now_local = now_utc+3h, filter: 12h - 3h = 9h > 0, passes.
      - Handler partition: deadline = start_at - 24h = -12h (past), now_utc > deadline
        → too-late (correct on any system TZ, uses datetime.now(UTC)).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        # 12h ahead at the next hour mark (LOCAL) — robust window for the reasons above.
        soon_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(hours=12)
        soon_local = soon_local.replace(minute=0, second=0, microsecond=0)
        await _seed_booking(session, ctx=ctx, start_at_local=soon_local)

    msg = _make_message(user_id=111222333, text="/mybookings")
    await client_handlers.mybookings_msg(msg)

    msg.answer.assert_called_once()
    text = _answer_text(msg)
    assert "⏰ Отмена недоступна" in text, (
        "booking within 24h must show 'Отмена недоступна' marker in /mybookings list"
    )
    # NO inline keyboard — cancel is not available, so no button to offer.
    assert _answer_reply_markup(msg) is None, (
        "too-late booking must NOT produce inline keyboard (no cancel possible)"
    )


@pytest.mark.asyncio
async def test_mybookings_msg_no_bookings(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Test #6: client exists but has NO bookings → "У вас нет активных записей.

    /book чтобы записаться". Covers the `if not bookings:` branch (line 415).
    Client exists via _seed_full_stack; no booking is seeded.
    """
    async with session_factory() as session:
        await _seed_full_stack(session)  # client created, no booking

    msg = _make_message(user_id=111222333, text="/mybookings")
    await client_handlers.mybookings_msg(msg)

    msg.answer.assert_called_once()
    text = _answer_text(msg)
    assert "У вас нет активных записей" in text
    assert _answer_reply_markup(msg) is None


@pytest.mark.asyncio
async def test_mybookings_msg_no_client_record(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Bonus #6b: telegram user has never booked → no Client row → handler
    returns "У вас пока нет записей" (covers `if client is None:` branch, line 409).
    Distinct from test #6 (client exists, no bookings).
    """
    msg = _make_message(user_id=999888777, text="/mybookings")  # never-seen telegram_id
    await client_handlers.mybookings_msg(msg)

    msg.answer.assert_called_once()
    text = _answer_text(msg)
    assert "У вас пока нет записей" in text


# ============================================================
# mybookings_cancel_cb — inline button callback (test #7-9)
# ============================================================


@pytest.mark.asyncio
async def test_mybookings_cancel_cb_happy_path(
    session_factory: Any,
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
) -> None:
    """Test #7: user taps [Отменить <date>] → cancel_booking succeeds →
    booking.status='cancelled' in DB, master receives notification via
    callback.bot.send_message, client gets "✅ Запись отменена".
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=future_local)
        booking_id = booking.id

    bot = AsyncMock()
    cb, cb_data = _make_callback(user_id=111222333, booking_id=booking_id, bot=bot)

    await client_handlers.mybookings_cancel_cb(cb, cb_data, mock_scheduler)

    # Master notification sent (text starts with "Отмена:" per cancel_booking:383).
    bot.send_message.assert_called_once()
    sent_kwargs = bot.send_message.call_args.kwargs
    assert sent_kwargs["text"].startswith("Отмена:"), (
        "master notification text must start with 'Отмена:' (cancel_booking contract)"
    )
    # chat_id is settings.ADMIN_ID (461355056 — conftest.py:26 sets env var).
    assert sent_kwargs["chat_id"] == 461355056

    # Client gets confirmation (text passed positionally to message.answer).
    cb.message.answer.assert_called_once()
    assert "✅ Запись отменена" in _answer_text(cb.message)

    # callback.answer called (Telegram requires ACK of callback_query).
    cb.answer.assert_awaited()

    # DB: booking.status now 'cancelled' (cancel_booking committed).
    async with session_factory() as verify_session:
        b = (await verify_session.execute(
            select(Booking).where(Booking.id == booking_id)
        )).scalar_one()
        assert b.status == "cancelled"

    # Scheduler.remove_job called for both remind_24h and remind_1h.
    actual_job_ids = {c.args[0] for c in mock_scheduler.remove_job.call_args_list}
    assert actual_job_ids == {f"remind_24h_{booking_id}", f"remind_1h_{booking_id}"}


@pytest.mark.asyncio
async def test_mybookings_cancel_cb_too_late(
    session_factory: Any,
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
) -> None:
    """Test #8: booking within 24h → CancelTooLateError → handler replies
    "❌ Отмена возможна только за 24+ часов до записи", booking stays 'confirmed'.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        soon_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(hours=1)
        soon_local = soon_local.replace(minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=soon_local)
        booking_id = booking.id

    bot = AsyncMock()
    cb, cb_data = _make_callback(user_id=111222333, booking_id=booking_id, bot=bot)

    await client_handlers.mybookings_cancel_cb(cb, cb_data, mock_scheduler)

    # Master NOT notified (cancel raised before service returned a result).
    bot.send_message.assert_not_called()
    # Client gets the "too late" reply on callback.message (NOT on callback.answer,
    # because the error message is long — handler uses message.answer, then answer()).
    cb.message.answer.assert_called_once()
    err_text = _answer_text(cb.message)
    assert "❌ Отмена возможна только за 24+ часов до записи" in err_text
    cb.answer.assert_awaited()

    # DB: booking STILL 'confirmed' (cancel_booking raised before UPDATE commit).
    async with session_factory() as verify_session:
        b = (await verify_session.execute(
            select(Booking).where(Booking.id == booking_id)
        )).scalar_one()
        assert b.status == "confirmed"

    # Scheduler NOT touched (service raised before remove_jobs_for_booking).
    mock_scheduler.remove_job.assert_not_called()


@pytest.mark.asyncio
async def test_mybookings_cancel_cb_not_owner(
    session_factory: Any,
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
) -> None:
    """Test #9: stranger telegram_id (no Client row OR different client) taps
    [Отменить] → handler resolves to a different Client → cancel_booking's
    `WHERE id=? AND client_id=?` returns no row → BookingNotFoundError →
    handler calls callback.answer("Запись не найдена") (Telegram popup).

    Defense-in-depth: same error text for "no such booking" and "not your booking"
    (avoids leaking existence of bookings the caller doesn't own).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session, client_telegram_id=111222333)
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=future_local)
        booking_id = booking.id

    # ALSO seed a second Client under a different telegram_id (so the handler
    # resolves a non-None client and proceeds to cancel_booking — which then
    # rejects on ownership check, NOT on the "client is None" early return).
    async with session_factory() as session:
        session.add(Client(telegram_id=999888777, name="Stranger"))
        await session.commit()

    bot = AsyncMock()
    # Stranger's telegram_id — handler resolves stranger's Client, passes
    # stranger's client_id to cancel_booking, which raises BookingNotFoundError.
    cb, cb_data = _make_callback(user_id=999888777, booking_id=booking_id, bot=bot)

    await client_handlers.mybookings_cancel_cb(cb, cb_data, mock_scheduler)

    # callback.answer called with the popup text "Запись не найдена".
    cb.answer.assert_awaited()
    answer_args = cb.answer.call_args.args
    assert answer_args and "Запись не найдена" in answer_args[0], (
        "BookingNotFoundError must surface as callback.answer('Запись не найдена')"
    )
    # No message.answer (handler early-returns after callback.answer on this branch).
    cb.message.answer.assert_not_called()
    bot.send_message.assert_not_called()

    # DB: booking STILL 'confirmed' (cancel_booking rejected on ownership).
    async with session_factory() as verify_session:
        b = (await verify_session.execute(
            select(Booking).where(Booking.id == booking_id)
        )).scalar_one()
        assert b.status == "confirmed"


@pytest.mark.asyncio
async def test_mybookings_cancel_cb_unknown_user_no_client(
    session_factory: Any,
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
) -> None:
    """Bonus #9b: telegram_id with no Client row (never booked) → handler early
    return via `if client is None:` (line 505) → callback.answer("У вас нет записей"),
    cancel_booking NOT called. Distinct from test #9 (stranger has a Client row).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session, client_telegram_id=111222333)
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=future_local)
        booking_id = booking.id

    bot = AsyncMock()
    # 555444333 — no Client row exists for this telegram_id.
    cb, cb_data = _make_callback(user_id=555444333, booking_id=booking_id, bot=bot)

    await client_handlers.mybookings_cancel_cb(cb, cb_data, mock_scheduler)

    cb.answer.assert_awaited()
    answer_args = cb.answer.call_args.args
    assert answer_args and "У вас нет записей" in answer_args[0]
    cb.message.answer.assert_not_called()
    bot.send_message.assert_not_called()
    mock_scheduler.remove_job.assert_not_called()


# ============================================================
# mybookings_keyboard — button rendering (test #10)
# ============================================================


@pytest.mark.asyncio
async def test_mybookings_keyboard_buttons_match_bookings(
    session_factory: Any,
) -> None:
    """Test #10: mybookings_keyboard(bookings) → N buttons with correct
    callback_data packing MyBookingsCancelCallbackData(booking_id=<uuid>).

    Pure keyboard test — no handler invocation, no DB patch. Verifies the
    rendering layer that mybookings_msg relies on (test #4 implicitly covers
    this via end-to-end, but explicit keyboard test isolates the contract).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        b1 = await _seed_booking(
            session,
            ctx=ctx,
            start_at_local=datetime(2026, 3, 18, 14, tzinfo=ZoneInfo("Europe/Moscow")),
        )
        b2 = await _seed_booking(
            session,
            ctx=ctx,
            start_at_local=datetime(2026, 3, 19, 15, tzinfo=ZoneInfo("Europe/Moscow")),
        )

    # Re-read bookings as detached objects to feed into the keyboard
    # (mybookings_keyboard reads .start_at and .id — needs attached-or-loaded rows).
    async with session_factory() as session:
        bookings = (
            await session.execute(
                select(Booking).where(Booking.id.in_([b1.id, b2.id])).order_by(Booking.start_at)
            )
        ).scalars().all()

    markup = mybookings_keyboard(bookings, business_timezone="Europe/Moscow")
    assert isinstance(markup, InlineKeyboardMarkup)
    buttons = [btn for row in markup.inline_keyboard for btn in row]
    assert len(buttons) == 2, "two bookings → two [Отменить] buttons"

    # Each button text starts with "❌ Отменить", callback_data packs the booking_id.
    for button, expected_booking in zip(buttons, bookings, strict=True):
        assert button.text.startswith("❌ Отменить")
        # button.callback_data is `str | None` in aiogram stubs — keyboard builder
        # always packs a non-None string; assert narrows type for mypy.
        assert button.callback_data is not None
        cb_data = MyBookingsCancelCallbackData.unpack(button.callback_data)
        assert cb_data.booking_id == expected_booking.id

    # adjust(1) — one button per row → 2 rows for 2 bookings.
    assert len(markup.inline_keyboard) == 2
    assert all(len(row) == 1 for row in markup.inline_keyboard)
