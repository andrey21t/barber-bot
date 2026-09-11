"""Tests for bot.handlers.client — mybookings_msg + mybookings_cancel_cb + transfer flow.

Coverage (NEXT_SESSION_PROMPT.md Приоритет 1 + 3 — handler gaps surfaced by self-review):
- mybookings_msg: cancelable booking shows [Отменить] inline button (test #4)
- mybookings_msg: too-late booking shows "⏰ Отмена недоступна" + NO button (test #5)
- mybookings_msg: no bookings → "У вас нет активных записей" (test #6)
- mybookings_cancel_cb: happy path → booking.status='cancelled' + master notified (test #7)
- mybookings_cancel_cb: too-late → CancelTooLateError → "Отмена возможна только за 24+" (test #8)
- mybookings_cancel_cb: not-owner (stranger telegram_id) → "Запись не найдена" (test #9)
- mybookings_keyboard: N bookings → 2N buttons (cancel + transfer) (test #10)
- mybookings_transfer_cb: happy path → FSM selecting_date + date picker shown (test #11)
- mybookings_transfer_cb: too-late → "Перенос возможен только за 24+" (test #12)
- mybookings_transfer_cb: unknown user (no Client) → "У вас нет записей" (test #13)
- mybookings_transfer_cb: not-owner (stranger Client) → "Запись не найдена" (test #14)
- transfer_slot_cb: happy path → transfer_booking succeeds, status='transferred' (test #15)
- transfer_slot_cb: slot already booked → "Слот только что заняли" (test #16)

Why handler tests (not just service tests, AGENTS.md § anti-overengineering rule 3):
mybookings_msg contains a partition decision (cancelable vs too-late) computed in
the handler — NOT in the service. Bug in `mybookings_msg:441` (naive vs aware datetime
comparison) shipped because handler logic was not covered by any test. Display
math that mirrors service invariants is a logic-change risk, not pure I/O.

Pattern (NEXT_SESSION_PROMPT.md 38): direct handler invocation with mock Message/
CallbackQuery + monkeypatch of `bot.handlers.client.async_session_factory` so the
handler reads/writes our in-memory test SQLite engine (NOT the global prod engine
pointed at by bot.db.async_session_factory). Avoids `dp.feed_update` ceremony —
no Dispatcher/MemoryStorage/router-wiring needed (transfer flow uses a mock FSMContext
that records set_state/update_data calls for assertion).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User
from aiogram_calendar import SimpleCalendarCallback
from aiogram_calendar.schemas import SimpleCalAct
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bot.config import get_settings
from bot.handlers import client as client_handlers
from bot.keyboards.client import (
    MyBookingsCancelCallbackData,
    MyBookingsTransferCallbackData,
    mybookings_keyboard,
)
from bot.models import Booking, Business, Client, Master, Service, Slot, WorkDay
from bot.states import BookingStates, TransferStates
from sqlalchemy import select
from sqlalchemy import text as sa_text
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


def _make_user(user_id: int, username: str | None = None) -> User:
    """Build a minimal aiogram User (required fields per Bot API).

    `username` defaults to None — mirrors Telegram users without @username.
    Tests that verify @username propagation should pass username explicitly.
    """
    return User(id=user_id, is_bot=False, first_name="Test", username=username)


def _make_message(
    user_id: int,
    text: str = "/mybookings",
) -> MagicMock:
    """Mock aiogram Message with the fields client handlers read.

    Handlers touch: message.from_user.id, message.answer (async),
    message.edit_reply_markup (async — Session 5.51 keyboard stripping).
    Other Message fields are left as MagicMock defaults (spec=Message blocks
    unknown attribute access, but `from_user`, `answer` and
    `edit_reply_markup` we set explicitly).
    """
    msg = MagicMock(spec=Message)
    msg.from_user = _make_user(user_id)
    msg.text = text
    msg.answer = AsyncMock()
    msg.edit_reply_markup = AsyncMock()
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

    master = Master(business_id=biz.id, name="Екатерина", telegram_id=461355056, role="owner")
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
    Original fix stripped tzinfo from now_utc. Updated 2026-08-23 (Урок 2.6):
    handler now uses aware-aware comparison — `booking.start_at.replace(tzinfo=UTC)`
    injects tzinfo on DB-read side (no-op on Postgres where already aware).
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
        b = (
            await verify_session.execute(select(Booking).where(Booking.id == booking.id))
        ).scalar_one()
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
    # mybookings_keyboard emits 2 buttons per booking: [Отменить] + [Перенести].
    buttons = [btn for row in reply_markup.inline_keyboard for btn in row]
    assert len(buttons) == 2, "one cancelable booking → 2 buttons: [Отменить] + [Перенести]"
    cancel_btn = buttons[0]
    transfer_btn = buttons[1]
    assert "Отменить" in cancel_btn.text
    assert "Перенести" in transfer_btn.text
    # callback_data packs MyBookingsCancelCallbackData(booking_id=<uuid>)
    # InlineKeyboardButton.callback_data is typed `str | None` in aiogram stubs;
    # mybookings_keyboard always packs a real callback_data string, so assert
    # non-None before unpack to satisfy mypy without losing the runtime check.
    assert cancel_btn.callback_data is not None
    assert transfer_btn.callback_data is not None
    cancel_cb_data = MyBookingsCancelCallbackData.unpack(cancel_btn.callback_data)
    assert cancel_cb_data.booking_id == booking.id
    from bot.keyboards.client import MyBookingsTransferCallbackData

    transfer_cb_data = MyBookingsTransferCallbackData.unpack(transfer_btn.callback_data)
    assert transfer_cb_data.booking_id == booking.id


@pytest.mark.asyncio
async def test_mybookings_msg_with_too_late_booking(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Test #5: booking within 24h → too-late → "⏰ Отмена недоступна" in text,
    NO inline keyboard (cancel window closed per spec.md 406 — 24h rule).

    start_at = +12h (LOCAL) — chosen to be robust on both Render (UTC system TZ)
    AND dev (Europe/Moscow TZ): `get_client_bookings` filters via
    `datetime.now(UTC)` (admin.py:156) — aware UTC. SQLAlchemy variant strips
    aware→naive on SQLite bind (verified 2026-08-23), so aware UTC vs stored
    naive UTC compares correctly on SQLite; native TIMESTAMPTZ vs aware UTC on
    Postgres. The handler partition (mybookings_msg:508) uses aware-aware
    comparison — `b.start_at.replace(tzinfo=UTC) - timedelta(...)`.

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
        b = (
            await verify_session.execute(select(Booking).where(Booking.id == booking_id))
        ).scalar_one()
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
        b = (
            await verify_session.execute(select(Booking).where(Booking.id == booking_id))
        ).scalar_one()
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
        b = (
            await verify_session.execute(select(Booking).where(Booking.id == booking_id))
        ).scalar_one()
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
    """Test #10: mybookings_keyboard(bookings) → 2N buttons (N cancel + N transfer)
    with correct callback_data packing.

    Two buttons per booking in one row (adjust(2)): [❌ Отменить <date>]
    packs MyBookingsCancelCallbackData(booking_id=<uuid>); [🔄 Перенести <date>]
    packs MyBookingsTransferCallbackData(booking_id=<uuid>).

    Pure keyboard test — no handler invocation, no DB patch. Verifies the
    rendering layer that mybookings_msg relies on (test #4 implicitly covers
    this via end-to-end, but explicit keyboard test isolates the contract).
    """
    from bot.keyboards.client import MyBookingsTransferCallbackData

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
            (
                await session.execute(
                    select(Booking).where(Booking.id.in_([b1.id, b2.id])).order_by(Booking.start_at)
                )
            )
            .scalars()
            .all()
        )

    markup = mybookings_keyboard(bookings, business_timezone="Europe/Moscow")
    assert isinstance(markup, InlineKeyboardMarkup)
    buttons = [btn for row in markup.inline_keyboard for btn in row]
    assert len(buttons) == 4, "two bookings → 4 buttons (2 cancel + 2 transfer)"

    # Each booking produces a [Отменить] + [Перенести] pair.
    # buttons are flattened row-by-row; row order = [cancel, transfer] per booking.
    for cancel_btn, transfer_btn, expected_booking in zip(
        buttons[0::2], buttons[1::2], bookings, strict=True
    ):
        assert cancel_btn.text.startswith("❌ Отменить")
        assert transfer_btn.text.startswith("🔄 Перенести")
        # callback_data packs the booking_id (cancel and transfer share same booking).
        assert cancel_btn.callback_data is not None
        assert transfer_btn.callback_data is not None
        cancel_cb = MyBookingsCancelCallbackData.unpack(cancel_btn.callback_data)
        transfer_cb = MyBookingsTransferCallbackData.unpack(transfer_btn.callback_data)
        assert cancel_cb.booking_id == expected_booking.id
        assert transfer_cb.booking_id == expected_booking.id

    # adjust(2) — 2 buttons per row → 2 rows for 2 bookings (one row each).
    assert len(markup.inline_keyboard) == 2
    assert all(len(row) == 2 for row in markup.inline_keyboard)


# ============================================================
# mybookings_transfer_cb — [🔄 Перенести] entry (test #11-14)
# ============================================================


def _make_state() -> MagicMock:
    """Mock aiogram FSMContext — records set_state/update_data/clear calls.

    `state.get_data()` returns a dict that persists between handler invocations
    (so multi-step FSM flow tests can chain mybookings_transfer_cb → transfer_date_cb
    → transfer_slot_cb). For single-handler tests, override `state.get_data` with
    a fixed return value before invoking the handler.
    """
    state = MagicMock(spec=FSMContext)
    state.set_state = AsyncMock()
    state.update_data = AsyncMock()
    state.clear = AsyncMock()
    # Mutable dict closure for state persistence between handler calls.
    state_data: dict[str, Any] = {}

    async def _get_data() -> dict[str, Any]:
        return dict(state_data)

    async def _update_data(**kwargs: Any) -> None:
        state_data.update(kwargs)

    state.get_data = AsyncMock(side_effect=_get_data)
    state.update_data = AsyncMock(side_effect=_update_data)
    return state


def _make_state_with_call_order(call_log: list[str]) -> MagicMock:
    """Variant of _make_state that records state.clear calls into a shared
    call_log list. Used by tests that verify the race protection contract
    "state.clear() BEFORE service call" (client.py:764).

    Tests using this helper also append "transfer_booking" to the SAME
    call_log inside their _raise_xxx function (monkey-patched service).
    After handler invocation, assert
    `call_log.index("state.clear") < call_log.index("transfer_booking")`.
    """
    state = MagicMock(spec=FSMContext)
    state.set_state = AsyncMock()
    state.update_data = AsyncMock()

    async def _clear() -> None:
        call_log.append("state.clear")

    state.clear = AsyncMock(side_effect=_clear)

    state_data: dict[str, Any] = {}

    async def _get_data() -> dict[str, Any]:
        return dict(state_data)

    async def _update_data(**kwargs: Any) -> None:
        state_data.update(kwargs)

    state.get_data = AsyncMock(side_effect=_get_data)
    state.update_data = AsyncMock(side_effect=_update_data)
    return state


def _make_transfer_callback(
    user_id: int,
    booking_id: UUID,
    bot: AsyncMock | None = None,
) -> tuple[MagicMock, MyBookingsTransferCallbackData]:
    """Mock aiogram.CallbackQuery for mybookings_transfer_cb + matching callback_data.

    Same shape as _make_callback but for MyBookingsTransferCallbackData (booking_id
    payload, prefix="mybook_transfer").
    """
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(user_id)
    cb.message = _make_message(user_id, text="<unused for transfer cb>")
    cb.answer = AsyncMock()
    cb.bot = bot or AsyncMock()
    cb.bot.send_message = AsyncMock()
    callback_data = MyBookingsTransferCallbackData(booking_id=booking_id)
    return cb, callback_data


@pytest.mark.asyncio
async def test_mybookings_transfer_cb_happy_path(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Test #11: user taps [🔄 Перенести] for a cancelable booking → handler sets
    FSM state to TransferStates.selecting_date, saves booking_id in state data,
    and shows date_picker_keyboard for the new date selection.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=future_local)
        booking_id = booking.id

    bot = AsyncMock()
    cb, cb_data = _make_transfer_callback(user_id=111222333, booking_id=booking_id, bot=bot)
    state = _make_state()

    await client_handlers.mybookings_transfer_cb(cb, cb_data, state)

    # FSM state set to selecting_date (transfer FSM entry).
    state.set_state.assert_awaited()
    set_state_arg = state.set_state.call_args.args[0]
    assert set_state_arg == TransferStates.selecting_date

    # booking_id saved in FSM data (as string — consistent with confirm_cb:155).
    state.update_data.assert_awaited()
    update_data_kwargs = state.update_data.call_args.kwargs
    assert update_data_kwargs.get("transfer_booking_id") == str(booking_id)

    # Date picker shown via message.answer with reply_markup.
    cb.message.answer.assert_awaited()
    reply_markup = _answer_reply_markup(cb.message)
    assert isinstance(reply_markup, InlineKeyboardMarkup), (
        "transfer entry must show date picker inline keyboard"
    )

    # callback.answer called (Telegram ACK).
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_mybookings_transfer_cb_too_late(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Test #12: booking within 24h → "❌ Перенос возможен только за 24+ часов до записи",
    FSM state NOT set (handler early-returns after the 24h check).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        soon_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(hours=12)
        soon_local = soon_local.replace(minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=soon_local)
        booking_id = booking.id

    bot = AsyncMock()
    cb, cb_data = _make_transfer_callback(user_id=111222333, booking_id=booking_id, bot=bot)
    state = _make_state()

    await client_handlers.mybookings_transfer_cb(cb, cb_data, state)

    # Client gets the "too late" reply on callback.message.
    cb.message.answer.assert_awaited()
    err_text = _answer_text(cb.message)
    assert "❌ Перенос возможен только за 24+ часов до записи" in err_text

    # FSM state NOT set (early-return before set_state).
    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_mybookings_transfer_cb_unknown_user(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Test #13: telegram_id with no Client row → handler early return via
    `if client is None:` → callback.answer("У вас нет записей"). FSM NOT entered.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session, client_telegram_id=111222333)
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=future_local)
        booking_id = booking.id

    bot = AsyncMock()
    # 555444333 — no Client row exists for this telegram_id.
    cb, cb_data = _make_transfer_callback(user_id=555444333, booking_id=booking_id, bot=bot)
    state = _make_state()

    await client_handlers.mybookings_transfer_cb(cb, cb_data, state)

    cb.answer.assert_awaited()
    answer_args = cb.answer.call_args.args
    assert answer_args and "У вас нет записей" in answer_args[0]
    cb.message.answer.assert_not_called()
    state.set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_mybookings_transfer_cb_not_owner(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Test #14: stranger telegram_id (different Client row) taps [Перенести] →
    booking lookup `WHERE id=? AND client_id=?` returns no row → handler early
    return → callback.answer("Запись не найдена"). FSM NOT entered.

    Defense-in-depth: same error text as cancel flow (test #9).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session, client_telegram_id=111222333)
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=future_local)
        booking_id = booking.id

    # ALSO seed a second Client under a different telegram_id.
    async with session_factory() as session:
        session.add(Client(telegram_id=999888777, name="Stranger"))
        await session.commit()

    bot = AsyncMock()
    cb, cb_data = _make_transfer_callback(user_id=999888777, booking_id=booking_id, bot=bot)
    state = _make_state()

    await client_handlers.mybookings_transfer_cb(cb, cb_data, state)

    cb.answer.assert_awaited()
    answer_args = cb.answer.call_args.args
    assert answer_args and "Запись не найдена" in answer_args[0]
    cb.message.answer.assert_not_called()
    state.set_state.assert_not_awaited()


# ============================================================
# transfer_slot_cb — final step → transfer_booking service call (test #15-16)
# ============================================================


def _make_slot_callback(
    user_id: int,
    slot_id: UUID,
    bot: AsyncMock | None = None,
) -> tuple[MagicMock, Any]:
    """Mock aiogram.CallbackQuery for transfer_slot_cb (BookSlotCallbackData)."""
    from bot.keyboards.client import BookSlotCallbackData

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(user_id)
    cb.message = _make_message(user_id, text="<unused for slot cb>")
    cb.answer = AsyncMock()
    cb.bot = bot or AsyncMock()
    cb.bot.send_message = AsyncMock()
    callback_data = BookSlotCallbackData(slot_id=slot_id)
    return cb, callback_data


async def _seed_open_slot(
    session: AsyncSession,
    ctx: dict[str, Any],
    *,
    days_ahead: int = 5,
    hour_local: int = 15,
) -> Slot:
    """Insert a single 'open' slot at days_ahead, hour_local — for transfer target."""
    slot_date = (datetime.now(UTC) + timedelta(days=days_ahead)).date()
    slot = Slot(
        master_id=ctx["master_id"],
        slot_date=slot_date,
        slot_hour=hour_local,
        status="open",
    )
    session.add(slot)
    await session.commit()
    return slot


@pytest.mark.asyncio
async def test_transfer_slot_cb_happy_path(
    session_factory: Any,
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
) -> None:
    """Test #15: user picked a new slot → transfer_booking succeeds →
    booking.status='transferred', slot_id updated, master notified via
    callback.bot.send_message, client gets "✅ Запись перенесена на ...".

    Direct test of the final FSM step (skips mybookings_transfer_cb entry —
    that's covered by test #11). Pre-populates state with transfer_booking_id
    so transfer_slot_cb can resolve the booking.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=future_local)
        booking_id = booking.id
        new_slot = await _seed_open_slot(session, ctx, days_ahead=5, hour_local=15)
        new_slot_id = new_slot.id

    bot = AsyncMock()
    cb, cb_data = _make_slot_callback(user_id=111222333, slot_id=new_slot_id, bot=bot)
    state = _make_state()
    # Pre-populate FSM data as if mybookings_transfer_cb + transfer_date_cb ran.
    state.get_data = AsyncMock(return_value={"transfer_booking_id": str(booking_id)})

    await client_handlers.transfer_slot_cb(cb, cb_data, state, mock_scheduler)

    # Master notification sent (text starts with "Перенос:" per transfer_booking contract).
    bot.send_message.assert_called_once()
    sent_kwargs = bot.send_message.call_args.kwargs
    assert sent_kwargs["text"].startswith("Перенос:"), (
        "master notification text must start with 'Перенос:' (transfer_booking contract)"
    )
    assert sent_kwargs["chat_id"] == 461355056  # ADMIN_ID

    # Client gets confirmation.
    cb.message.answer.assert_awaited()
    assert "✅ Запись перенесена" in _answer_text(cb.message)

    # state.clear() called BEFORE service call (race condition, MY-VIBE-RULES.md 24).
    state.clear.assert_awaited()

    # callback.answer called (Telegram ACK).
    cb.answer.assert_awaited()

    # DB: booking.status now 'transferred', slot_id points to new_slot.
    async with session_factory() as verify_session:
        b = (
            await verify_session.execute(select(Booking).where(Booking.id == booking_id))
        ).scalar_one()
        assert b.status == "transferred"
        assert b.slot_id == new_slot_id

    # Old slot released to 'open', new slot booked.
    async with session_factory() as verify_session:
        # Old slot_id from booking's first slot — re-fetch via the slot we created.
        # The seed_booking inserted a Slot with status='booked'; after transfer, that
        # slot must be 'open'. We fetch by booking's new slot_id (different row).
        new_slot_row = (
            await verify_session.execute(select(Slot).where(Slot.id == new_slot_id))
        ).scalar_one()
        assert new_slot_row.status == "booked"

    # Scheduler: remove_job called for old reminders, add_job for new ones.
    actual_remove = {c.args[0] for c in mock_scheduler.remove_job.call_args_list}
    assert actual_remove == {f"remind_24h_{booking_id}", f"remind_1h_{booking_id}"}
    actual_add = {c.kwargs.get("id") for c in mock_scheduler.add_job.call_args_list}
    assert actual_add == {f"remind_24h_{booking_id}", f"remind_1h_{booking_id}"}


@pytest.mark.asyncio
async def test_transfer_slot_cb_slot_already_booked(
    session_factory: Any,
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
) -> None:
    """Test #16: user picked a slot, but another caller booked it between SELECT
    and UPDATE → SlotAlreadyBookedError → handler replies "Слот только что заняли",
    booking stays 'confirmed' (no transfer), master NOT notified.

    Simulates the race condition transfer_booking protects against at the slot
    level (UPDATE WHERE status='open' + rowcount=0). For this test we mark the
    target slot as 'booked' before invoking transfer_slot_cb — simulating the
    winner's commit happening before our UPDATE.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=future_local)
        booking_id = booking.id
        new_slot = await _seed_open_slot(session, ctx, days_ahead=5, hour_local=15)
        new_slot_id = new_slot.id
        # Mark new_slot as already 'booked' (simulating concurrent winner).
        new_slot.status = "booked"
        await session.commit()

    bot = AsyncMock()
    cb, cb_data = _make_slot_callback(user_id=111222333, slot_id=new_slot_id, bot=bot)
    state = _make_state()
    state.get_data = AsyncMock(return_value={"transfer_booking_id": str(booking_id)})

    await client_handlers.transfer_slot_cb(cb, cb_data, state, mock_scheduler)

    # Master NOT notified (SlotAlreadyBookedError raised before send_message).
    bot.send_message.assert_not_called()
    # Client gets the "slot taken" reply.
    cb.message.answer.assert_awaited()
    err_text = _answer_text(cb.message)
    assert "Слот только что заняли" in err_text
    cb.answer.assert_awaited()

    # state.clear() called BEFORE service call (even on error — race condition).
    state.clear.assert_awaited()

    # DB: booking STILL 'confirmed' (transfer_booking rolled back on SlotAlreadyBookedError).
    async with session_factory() as verify_session:
        b = (
            await verify_session.execute(select(Booking).where(Booking.id == booking_id))
        ).scalar_one()
        assert b.status == "confirmed"


# ============================================================
# transfer_slot_cb — additional error branches (handler coverage gap)
# ============================================================
#
# These tests cover transfer_slot_cb:776-818 — error branches surfaced by
# coverage report (handler coverage 45% → target ~85%). Pattern mirrors
# test_transfer_slot_cb_slot_already_booked: seed booking + slot, simulate
# service failure via DB state (where possible) or monkey-patch
# bot.handlers.client.transfer_booking (for service-raised exceptions
# without natural DB trigger).
#
# Why monkey-patch instead of natural trigger: BookingAlreadyTransferredError
# race requires concurrent transfer (winner committed before our UPDATE).
# Natural trigger needs 2 sessions + orchestration (covered at service level
# by test_transfer_booking_concurrent_race_runtime). At handler level we
# care about error → user-facing text mapping, not race reproduction — so
# monkey-patch the service to raise the exception.


@pytest.mark.asyncio
async def test_transfer_slot_cb_already_transferred(
    session_factory: Any,
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BookingAlreadyTransferredError (race loser) → handler replies
    "❌ Запись уже перенесена (конкурентный запрос). /mybookings чтобы увидеть
    актуальный список". State.clear called BEFORE service call (race condition
    contract). Master NOT notified, callback.answer is the Telegram ACK.

    Race protection error — surfaced by coverage gap report. Service-level
    coverage exists (test_transfer_booking_concurrent_race_runtime), this
    test locks the user-facing error mapping for the handler.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=future_local)
        booking_id = booking.id
        new_slot = await _seed_open_slot(session, ctx, days_ahead=5, hour_local=15)
        new_slot_id = new_slot.id

    from bot.services import booking as booking_svc

    call_log: list[str] = []

    async def _raise_transferred(*args, **kwargs):
        call_log.append("transfer_booking")
        raise booking_svc.BookingAlreadyTransferredError("race simulation")

    monkeypatch.setattr(client_handlers, "transfer_booking", _raise_transferred)

    bot = AsyncMock()
    cb, cb_data = _make_slot_callback(user_id=111222333, slot_id=new_slot_id, bot=bot)
    state = _make_state_with_call_order(call_log)
    state.get_data = AsyncMock(return_value={"transfer_booking_id": str(booking_id)})

    await client_handlers.transfer_slot_cb(cb, cb_data, state, mock_scheduler)

    bot.send_message.assert_not_called()  # Master NOT notified
    cb.message.answer.assert_awaited()
    err_text = _answer_text(cb.message)
    assert "Запись уже перенесена (конкурентный запрос)" in err_text
    assert "/mybookings" in err_text
    cb.answer.assert_awaited()
    state.clear.assert_awaited()
    # Race condition contract: state.clear BEFORE service call (client.py:764).
    assert call_log.index("state.clear") < call_log.index("transfer_booking")

    # DB: booking unchanged (transfer_booking monkey-patched, no DB write)
    async with session_factory() as verify_session:
        b = (
            await verify_session.execute(select(Booking).where(Booking.id == booking_id))
        ).scalar_one()
        assert b.status == "confirmed"


@pytest.mark.asyncio
async def test_transfer_slot_cb_already_cancelled(
    session_factory: Any,
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BookingAlreadyCancelledError → handler replies "Запись уже отменена"
    (callback.answer — short text, no message.answer). Booking was cancelled
    by concurrent request between SELECT and UPDATE.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=future_local)
        booking_id = booking.id
        new_slot = await _seed_open_slot(session, ctx, days_ahead=5, hour_local=15)
        new_slot_id = new_slot.id

    from bot.services import booking as booking_svc

    async def _raise_cancelled(*args, **kwargs):
        raise booking_svc.BookingAlreadyCancelledError("concurrent cancel")

    monkeypatch.setattr(client_handlers, "transfer_booking", _raise_cancelled)

    bot = AsyncMock()
    cb, cb_data = _make_slot_callback(user_id=111222333, slot_id=new_slot_id, bot=bot)
    state = _make_state()
    state.get_data = AsyncMock(return_value={"transfer_booking_id": str(booking_id)})

    await client_handlers.transfer_slot_cb(cb, cb_data, state, mock_scheduler)

    bot.send_message.assert_not_called()
    cb.message.answer.assert_not_awaited()  # short text via callback.answer
    cb.answer.assert_awaited()
    answer_text = cb.answer.call_args.args[0] if cb.answer.call_args.args else ""
    assert "Запись уже отменена" in answer_text
    state.clear.assert_awaited()


@pytest.mark.asyncio
async def test_transfer_slot_cb_cancel_too_late(
    session_factory: Any,
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CancelTooLateError → handler replies "❌ Перенос возможен только за 24+ часов
    до записи" via callback.message.answer. State.clear called BEFORE service call.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=future_local)
        booking_id = booking.id
        new_slot = await _seed_open_slot(session, ctx, days_ahead=5, hour_local=15)
        new_slot_id = new_slot.id

    from bot.services import booking as booking_svc

    async def _raise_too_late(*args, **kwargs):
        raise booking_svc.CancelTooLateError("too late simulation")

    monkeypatch.setattr(client_handlers, "transfer_booking", _raise_too_late)

    bot = AsyncMock()
    cb, cb_data = _make_slot_callback(user_id=111222333, slot_id=new_slot_id, bot=bot)
    state = _make_state()
    state.get_data = AsyncMock(return_value={"transfer_booking_id": str(booking_id)})

    await client_handlers.transfer_slot_cb(cb, cb_data, state, mock_scheduler)

    bot.send_message.assert_not_called()
    cb.message.answer.assert_awaited()
    err_text = _answer_text(cb.message)
    assert "Перенос возможен только за 24+ часов" in err_text
    cb.answer.assert_awaited()
    state.clear.assert_awaited()


@pytest.mark.asyncio
async def test_transfer_slot_cb_slot_in_past(
    session_factory: Any,
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SlotInPastError → handler replies "❌ Это время уже прошло."
    Triggered when user picks a slot whose start_at <= now (past time slot).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=future_local)
        booking_id = booking.id
        new_slot = await _seed_open_slot(session, ctx, days_ahead=5, hour_local=15)
        new_slot_id = new_slot.id

    from bot.services import booking as booking_svc

    async def _raise_past(*args, **kwargs):
        raise booking_svc.SlotInPastError("past slot")

    monkeypatch.setattr(client_handlers, "transfer_booking", _raise_past)

    bot = AsyncMock()
    cb, cb_data = _make_slot_callback(user_id=111222333, slot_id=new_slot_id, bot=bot)
    state = _make_state()
    state.get_data = AsyncMock(return_value={"transfer_booking_id": str(booking_id)})

    await client_handlers.transfer_slot_cb(cb, cb_data, state, mock_scheduler)

    bot.send_message.assert_not_called()
    cb.message.answer.assert_awaited()
    err_text = _answer_text(cb.message)
    assert "Это время уже прошло" in err_text
    cb.answer.assert_awaited()
    state.clear.assert_awaited()


@pytest.mark.asyncio
async def test_transfer_slot_cb_slot_closed(
    session_factory: Any,
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SlotClosedError → handler replies "❌ Слот закрыт мастером."
    Triggered when new_slot.status='closed' (master closed it before user picked).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=future_local)
        booking_id = booking.id
        new_slot = await _seed_open_slot(session, ctx, days_ahead=5, hour_local=15)
        new_slot_id = new_slot.id

    from bot.services import booking as booking_svc

    async def _raise_closed(*args, **kwargs):
        raise booking_svc.SlotClosedError("closed slot")

    monkeypatch.setattr(client_handlers, "transfer_booking", _raise_closed)

    bot = AsyncMock()
    cb, cb_data = _make_slot_callback(user_id=111222333, slot_id=new_slot_id, bot=bot)
    state = _make_state()
    state.get_data = AsyncMock(return_value={"transfer_booking_id": str(booking_id)})

    await client_handlers.transfer_slot_cb(cb, cb_data, state, mock_scheduler)

    bot.send_message.assert_not_called()
    cb.message.answer.assert_awaited()
    err_text = _answer_text(cb.message)
    assert "Слот закрыт мастером" in err_text
    cb.answer.assert_awaited()
    state.clear.assert_awaited()


@pytest.mark.asyncio
async def test_transfer_slot_cb_booking_not_found(
    session_factory: Any,
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BookingNotFoundError → handler replies "Запись не найдена" via
    callback.answer (short text). Simulates booking_id from FSM state pointing
    to a non-existent booking (e.g. deleted between FSM steps) — service raises
    BookingNotFoundError, handler maps to user text.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        new_slot = await _seed_open_slot(session, ctx, days_ahead=5, hour_local=15)
        new_slot_id = new_slot.id

    from bot.services import booking as booking_svc

    async def _raise_not_found(*args, **kwargs):
        raise booking_svc.BookingNotFoundError("not found")

    monkeypatch.setattr(client_handlers, "transfer_booking", _raise_not_found)

    bot = AsyncMock()
    random_booking_id = UUID("00000000-0000-0000-0000-000000000001")
    cb, cb_data = _make_slot_callback(user_id=111222333, slot_id=new_slot_id, bot=bot)
    state = _make_state()
    state.get_data = AsyncMock(return_value={"transfer_booking_id": str(random_booking_id)})

    await client_handlers.transfer_slot_cb(cb, cb_data, state, mock_scheduler)

    bot.send_message.assert_not_called()
    cb.message.answer.assert_not_awaited()
    cb.answer.assert_awaited()
    answer_text = cb.answer.call_args.args[0] if cb.answer.call_args.args else ""
    assert "Запись не найдена" in answer_text
    state.clear.assert_awaited()


@pytest.mark.asyncio
async def test_transfer_slot_cb_unknown_user_no_client(
    session_factory: Any,
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
) -> None:
    """User with no Client record → handler replies "У вас нет записей" via
    callback.answer. State is NOT cleared (user may retry after /book).
    Coverage: client.py:759-762 defensive branch.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        new_slot = await _seed_open_slot(session, ctx, days_ahead=5, hour_local=15)
        new_slot_id = new_slot.id

    bot = AsyncMock()
    cb, cb_data = _make_slot_callback(user_id=999888777, slot_id=new_slot_id, bot=bot)
    state = _make_state()
    state.get_data = AsyncMock(return_value={"transfer_booking_id": "irrelevant"})

    await client_handlers.transfer_slot_cb(cb, cb_data, state, mock_scheduler)

    bot.send_message.assert_not_called()
    cb.message.answer.assert_not_awaited()
    cb.answer.assert_awaited()
    answer_text = cb.answer.call_args.args[0] if cb.answer.call_args.args else ""
    assert "У вас нет записей" in answer_text
    state.clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_transfer_slot_cb_state_data_lost(
    session_factory: Any,
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
) -> None:
    """Bot restart mid-FSM → state data lost (transfer_booking_id missing) →
    handler replies "❌ Данные потеряны. /mybookings чтобы начать" via
    callback.message.answer. State.clear called BEFORE answer (race condition
    contract). Coverage: client.py:746-752 FSM edge case.
    """
    bot = AsyncMock()
    cb, cb_data = _make_slot_callback(user_id=111222333, slot_id=UUID(int=1), bot=bot)
    state = _make_state()
    state.get_data = AsyncMock(return_value={})

    await client_handlers.transfer_slot_cb(cb, cb_data, state, mock_scheduler)

    bot.send_message.assert_not_called()
    cb.message.answer.assert_awaited()
    err_text = _answer_text(cb.message)
    assert "Данные потеряны" in err_text
    assert "/mybookings" in err_text
    cb.answer.assert_awaited()
    state.clear.assert_awaited()  # cleared BEFORE answer (race condition contract)


@pytest.mark.asyncio
async def test_transfer_slot_cb_slot_not_available(
    session_factory: Any,
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SlotNotAvailableError (defensive) → handler replies "❌ Слот недоступен."
    Covers the last remaining error branch in transfer_slot_cb (client.py:814-818).
    SlotNotAvailableError is a defensive catch-all for unexpected slot states
    not covered by SlotClosedError/SlotAlreadyBookedError.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=future_local)
        booking_id = booking.id
        new_slot = await _seed_open_slot(session, ctx, days_ahead=5, hour_local=15)
        new_slot_id = new_slot.id

    from bot.services import booking as booking_svc

    async def _raise_not_avail(*args, **kwargs):
        raise booking_svc.SlotNotAvailableError("unexpected slot state")

    monkeypatch.setattr(client_handlers, "transfer_booking", _raise_not_avail)

    bot = AsyncMock()
    cb, cb_data = _make_slot_callback(user_id=111222333, slot_id=new_slot_id, bot=bot)
    state = _make_state()
    state.get_data = AsyncMock(return_value={"transfer_booking_id": str(booking_id)})

    await client_handlers.transfer_slot_cb(cb, cb_data, state, mock_scheduler)

    bot.send_message.assert_not_called()
    cb.message.answer.assert_awaited()
    err_text = _answer_text(cb.message)
    assert "Слот недоступен" in err_text
    cb.answer.assert_awaited()
    state.clear.assert_awaited()


# ============================================================
# Booking flow — T5a: cmd_book + simple_calendar_cb + slot_cb + name_msg + service_msg
# Coverage: client.py:75-218, 226-238, 245-257, 264-312
# ============================================================


@pytest.mark.asyncio
async def test_cmd_book_sets_state_and_shows_date_picker(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T5a (BB-110): cmd_book — /book resolves master, queries bookable dates,
    sets FSM to selecting_date + is_slots_path=False + shows date_picker_keyboard
    (replaces SimpleCalendar since Session 5.28).

    Seeded: master + one active WorkDay tomorrow (10:00-18:00) → 1 bookable
    date expected. Past days excluded by get_bookable_dates range; non-working
    days excluded by the pre-filter — BB-110 UX-pain fix (PLANS.md:827).
    """
    tomorrow = (datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=1)).date()
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        await _seed_workday(session, ctx, work_date=tomorrow)

    msg = _make_message(user_id=111222333, text="/book")
    state = _make_state()

    await client_handlers.cmd_book(msg, state)

    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.selecting_date

    update_kwargs = state.update_data.call_args.kwargs
    assert update_kwargs.get("is_slots_path") is False

    msg.answer.assert_awaited_once()
    text = _answer_text(msg)
    assert "Выберите дату" in text
    reply_markup = _answer_reply_markup(msg)
    assert isinstance(reply_markup, InlineKeyboardMarkup), (
        "cmd_book must show date picker inline keyboard"
    )
    # Picker rows: pairs of date buttons (1 here) + last row '❌ Отмена'
    last_row = reply_markup.inline_keyboard[-1]
    assert len(last_row) == 1 and "Отмена" in last_row[0].text


@pytest.mark.asyncio
async def test_cmd_book_no_bookable_dates_shows_empty_message_no_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """BB-110 empty state: master exists but no active WorkDay with free slots
    AND no open legacy slots → message + state NOT entered (no dead-end FSM).
    """
    async with session_factory() as session:
        await _seed_full_stack(session)  # master, no workday, no slots

    msg = _make_message(user_id=111222333, text="/book")
    state = _make_state()

    await client_handlers.cmd_book(msg, state)

    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()
    msg.answer.assert_awaited_once()
    text = _answer_text(msg)
    assert "нет свободных дат" in text.lower(), f"expected empty hint, got: {text!r}"


@pytest.mark.asyncio
async def test_cmd_book_master_not_found_shows_error_no_state(
    patched_session_factory: Any,
) -> None:
    """BB-110 defensive: cmd_book with NO master in DB → 'Не удалось найти
    мастера' + state NOT entered. Mirrors _process_selected_date master-None
    branch (pre-existing) at the entry path.
    """
    # No _seed_full_stack — DB has no master row.
    msg = _make_message(user_id=111222333, text="/book")
    state = _make_state()

    await client_handlers.cmd_book(msg, state)

    state.set_state.assert_not_awaited()
    msg.answer.assert_awaited_once()
    assert "Не удалось найти мастера" in _answer_text(msg)


# ============================================================
# _calendar_range — regression test for F1 (today must be bookable)
# ============================================================


def test_calendar_range_returns_midnight_naive_local() -> None:
    """_calendar_range must return (min_date, max_date) at MIDNIGHT in business TZ.

    Regression test for F1 (code-reviewer LBTM): if min_date has a time component
    (e.g. 13:45), aiogram_calendar's process_day_select (common.py:57) compares
    `min_date > datetime(year, month, day) @ midnight` → today is out-of-range →
    "Сегодня" button alerts "date have to be later <today>". User cannot book today.

    aiogram_calendar builds `datetime(year, month, day)` naive AT MIDNIGHT — so
    min_date must also be at midnight (just tzinfo-stripped is not enough).
    """
    settings = get_settings()
    min_date, max_date = client_handlers._calendar_range(settings)

    # Both naive (no tzinfo) — lib compares with naive datetime
    assert min_date.tzinfo is None, "min_date must be naive (lib compares naive)"
    assert max_date.tzinfo is None, "max_date must be naive (lib compares naive)"

    # Both at midnight — F1 regression: time component breaks today-booking
    assert min_date.hour == 0 and min_date.minute == 0 and min_date.second == 0, (
        f"min_date must be midnight, got {min_date.time()}"
    )
    assert max_date.hour == 0 and max_date.minute == 0 and max_date.second == 0, (
        f"max_date must be midnight, got {max_date.time()}"
    )

    # max_date - min_date == MAX_BOOKING_DAYS_AHEAD days
    delta = (max_date - min_date).days
    assert delta == settings.MAX_BOOKING_DAYS_AHEAD, (
        f"range span = {delta} days, expected {settings.MAX_BOOKING_DAYS_AHEAD}"
    )

    # min_date is today (in business TZ) — F1 fix ensures today is bookable
    tz = ZoneInfo(settings.TIMEZONE)
    today_local_midnight = datetime.now(tz).replace(
        tzinfo=None, hour=0, minute=0, second=0, microsecond=0
    )
    assert min_date.date() == today_local_midnight.date(), (
        f"min_date={min_date.date()} != today={today_local_midnight.date()}"
    )


# ============================================================
# simple_calendar_cb — aiogram_calendar (booking flow, BookingStates.selecting_date)
# and transfer_simple_calendar_cb (transfer flow, TransferStates.selecting_date).
# Replaces test_date_cb_* (deleted with BookDateCallbackData).
# ============================================================


def _make_simple_calendar_callback(
    act: SimpleCalAct,
    *,
    year: int | None = None,
    month: int | None = None,
    day: int | None = None,
) -> SimpleCalendarCallback:
    """Build a SimpleCalendarCallback with given act + date fields.

    All fields default to today (in business TZ) — caller overrides for
    navigation tests (today + diff-month check needs explicit year/month).
    """
    today = datetime.now(ZoneInfo("Europe/Moscow")).replace(tzinfo=None)
    return SimpleCalendarCallback(
        act=act,
        year=year or today.year,
        month=month or today.month,
        day=day or today.day,
    )


def _patch_process_selection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    selected: bool,
    selected_date: datetime | None = None,
) -> None:
    """Patch SimpleCalendar.process_selection to return (selected, date) tuple.

    For act=day: lib returns (True, datetime(...)) on in-range click,
    (False, None) on out-of-range (F7 fix). For other acts: (False, None).
    """
    from aiogram_calendar import SimpleCalendar

    async def _fake(self, callback, data):  # noqa: ARG001
        return selected, selected_date

    monkeypatch.setattr(SimpleCalendar, "process_selection", _fake)


@pytest.mark.asyncio
async def test_simple_calendar_cb_day_select_happy_shows_service_picker(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session 5.29 Task 2 — FSM reorder: simple_calendar_cb act=day, in-range,
    services exist → state.update_data(selected_date) + set_state(entering_service)
    + service picker (NOT slot picker — slot picker moved to service_picker_cb
    after service selection, fixes 15:30+Стрижка vs 16:00-18:00-Окрашивание bug).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        await _seed_service(session, ctx, name="Стрижка", duration_minutes=60)
        await _seed_service(session, ctx, name="Окрашивание", duration_minutes=120)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        # No Slot seeded — booking flow no longer fetches slots on date-select
        # (slots are fetched in service_picker_cb AFTER service selection).

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot

    # Patch process_selection to simulate user clicked target_date (in-range)
    target_dt = datetime.combine(target_date, time(12, 0))
    _patch_process_selection(monkeypatch, selected=True, selected_date=target_dt)

    callback_data = _make_simple_calendar_callback(SimpleCalAct.day)
    state = _make_state()
    await client_handlers.simple_calendar_cb(cb, callback_data, state)

    state.update_data.assert_awaited()
    assert state.update_data.call_args.kwargs.get("selected_date") == target_date.isoformat()
    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.entering_service

    text = _answer_text(cb.message)
    assert "Выберите услугу" in text
    reply_markup = _answer_reply_markup(cb.message)
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    flat_texts = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "Стрижка" in flat_texts
    assert "Окрашивание" in flat_texts
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_simple_calendar_cb_day_select_no_services_aborts_booking(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session 5.51: free-text fallback REMOVED. Master + business exist but
    NO active services in DB → booking impossible (a free-text service has
    no known duration_minutes → SERVICE_DEFAULT_DURATION_MIN would silently
    corrupt the slot grid and /today view).

    New contract: state.clear() + 'Мастер пока не настроил услуги' — no
    entering_service, no keyboard. FSM is fully reset (was: entering_service
    + 'Какая услуга?' free-text prompt pre-5.51).
    """
    async with session_factory() as session:
        await _seed_full_stack(session)  # master + business, NO services

    target_date = (datetime.now(UTC) + timedelta(days=1)).date()
    target_dt = datetime.combine(target_date, time(12, 0))
    _patch_process_selection(monkeypatch, selected=True, selected_date=target_dt)

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = _make_simple_calendar_callback(SimpleCalAct.day)
    state = _make_state()
    await client_handlers.simple_calendar_cb(cb, callback_data, state)

    # FSM is cleared — free-text service entry no longer exists.
    state.clear.assert_awaited_once()
    state.set_state.assert_not_awaited()
    text = _answer_text(cb.message)
    assert "не настроил услуги" in text
    assert _answer_reply_markup(cb.message) is None, (
        "no services in DB → no keyboard at all, booking aborted"
    )
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_simple_calendar_cb_day_select_master_not_found_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """simple_calendar_cb act=day, in-range, NO master in DB →
    state.clear + '❌ Не удалось найти мастера...' + callback.answer.
    """

    # No _seed_full_stack — DB empty
    target_date = (datetime.now(UTC) + timedelta(days=1)).date()
    target_dt = datetime.combine(target_date, time(12, 0))
    _patch_process_selection(monkeypatch, selected=True, selected_date=target_dt)

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = _make_simple_calendar_callback(SimpleCalAct.day)

    state = _make_state()
    await client_handlers.simple_calendar_cb(cb, callback_data, state)

    state.clear.assert_awaited_once()
    assert "Не удалось найти мастера" in _answer_text(cb.message)
    cb.answer.assert_awaited()
    state.set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_simple_calendar_cb_day_out_of_range_returns_silently(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """simple_calendar_cb act=day, OUT-of-range (lib returned selected=False) →
    handler returns without answering (F7 fix — lib already answered alert).
    """

    _patch_process_selection(monkeypatch, selected=False, selected_date=None)

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = _make_simple_calendar_callback(SimpleCalAct.day)

    state = _make_state()
    await client_handlers.simple_calendar_cb(cb, callback_data, state)

    # F7 fix: handler did NOT answer (lib answered alert) and did NOT touch state
    cb.answer.assert_not_awaited()
    state.clear.assert_not_awaited()
    state.set_state.assert_not_awaited()
    cb.message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_simple_calendar_cb_cancel_clears_state_and_answers(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """simple_calendar_cb act=cancel →
    state.clear() BEFORE callback.answer (race condition, MY-VIBE-RULES.md:23) +
    'Ввод отменён. /book чтобы начать заново' (booking flow, is_transfer=False).
    """

    _patch_process_selection(monkeypatch, selected=False, selected_date=None)

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = _make_simple_calendar_callback(SimpleCalAct.cancel)

    state = _make_state_with_call_order([])  # tracks call order for race condition check
    await client_handlers.simple_calendar_cb(cb, callback_data, state)

    assert "Ввод отменён" in _answer_text(cb.message)
    assert "/book" in _answer_text(cb.message)
    state.clear.assert_awaited_once()
    cb.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_simple_calendar_cb_navigation_answers(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """simple_calendar_cb act=next_m (navigation) → lib did edit_reply_markup,
    handler answers (F1 fix). State NOT touched.
    """

    _patch_process_selection(monkeypatch, selected=False, selected_date=None)

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = _make_simple_calendar_callback(SimpleCalAct.next_m)

    state = _make_state()
    await client_handlers.simple_calendar_cb(cb, callback_data, state)

    # F1 fix: navigation actions need handler.answer (lib did not answer)
    cb.answer.assert_awaited_once()
    state.clear.assert_not_awaited()
    state.set_state.assert_not_awaited()
    # Navigation does NOT advance FSM — calendar stays on selecting_date
    cb.message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_simple_calendar_cb_today_same_month_answers_explicitly(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """simple_calendar_cb act=today, SAME month as callback_data →
    handler answers cache_time=60 explicitly (skips lib for this branch).
    No state change.

    Uses system-local datetime.now() (NOT Moscow TZ) — handler's check at
    client.py:140 uses `datetime.now().replace(tzinfo=None)` (system-local).
    If we used Moscow TZ here and CI runs in UTC at month boundary, the
    year/month could differ → test would take the diff-month branch and
    fail (W2 from code-reviewer, flaky at 21:00-00:00 UTC last day of month).
    """
    _patch_process_selection(monkeypatch, selected=False, selected_date=None)

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    # Match handler's check — system-local year/month (not Moscow TZ)
    sys_now = datetime.now().replace(tzinfo=None)
    callback_data = _make_simple_calendar_callback(
        SimpleCalAct.today, year=sys_now.year, month=sys_now.month
    )

    state = _make_state()
    await client_handlers.simple_calendar_cb(cb, callback_data, state)

    # Handler answers cache_time=60 (replaces lib's answer — lib is skipped)
    cb.answer.assert_awaited_once()
    _args, kwargs = cb.answer.await_args
    assert kwargs.get("cache_time") == 60
    state.clear.assert_not_awaited()
    cb.message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_simple_calendar_cb_ignore_answers_explicitly(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """simple_calendar_cb act=ignore → handler answers cache_time=60 explicitly
    (skips lib for this branch, replaces lib's query.answer). Covers client.py:136.
    """
    _patch_process_selection(monkeypatch, selected=False, selected_date=None)

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = _make_simple_calendar_callback(SimpleCalAct.ignore)

    state = _make_state()
    await client_handlers.simple_calendar_cb(cb, callback_data, state)

    cb.answer.assert_awaited_once()
    args, kwargs = cb.answer.await_args
    assert kwargs.get("cache_time") == 60
    state.clear.assert_not_awaited()
    state.set_state.assert_not_awaited()
    cb.message.answer.assert_not_awaited()


# ============================================================
# transfer_simple_calendar_cb — aiogram_calendar for transfer flow
# (mirrors booking tests, but with is_transfer=True and TransferStates)
# ============================================================


@pytest.mark.asyncio
async def test_transfer_simple_calendar_cb_day_select_happy_shows_slot_picker(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """transfer_simple_calendar_cb act=day, in-range, slots exist →
    set_state(TransferStates.selecting_slot) + 'Выберите новое время:' (is_transfer=True).
    """

    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        slot = Slot(master_id=ctx["master_id"], slot_date=target_date, slot_hour=14, status="open")
        session.add(slot)
        await session.commit()

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot

    target_dt = datetime.combine(target_date, time(12, 0))
    _patch_process_selection(monkeypatch, selected=True, selected_date=target_dt)
    callback_data = _make_simple_calendar_callback(SimpleCalAct.day)

    state = _make_state()
    await client_handlers.transfer_simple_calendar_cb(cb, callback_data, state)

    assert state.set_state.call_args.args[0] == TransferStates.selecting_slot
    assert "Выберите новое время" in _answer_text(cb.message)  # is_transfer=True branch
    reply_markup = _answer_reply_markup(cb.message)
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    # S1 review fix F2 + W1 (Session 5.31): transfer flow must NOT render
    # '↩️ Назад' — back handler has BookingStates.selecting_slot StateFilter,
    # doesn't cover TransferStates.selecting_slot → dead button. show_back=False
    # passed via _process_selected_date(is_transfer=True) → slot_picker_keyboard.
    flat_texts = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "↩️ Назад" not in flat_texts, (
        "transfer slot picker must not show back button (no service step in transfer)"
    )
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_transfer_simple_calendar_cb_cancel_clears_state_with_transfer_message(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """transfer_simple_calendar_cb act=cancel →
    state.clear + 'Перенос отменён. /mybookings чтобы начать заново' (is_transfer=True).
    """

    _patch_process_selection(monkeypatch, selected=False, selected_date=None)

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = _make_simple_calendar_callback(SimpleCalAct.cancel)

    state = _make_state()
    await client_handlers.transfer_simple_calendar_cb(cb, callback_data, state)

    assert "Перенос отменён" in _answer_text(cb.message)
    assert "/mybookings" in _answer_text(cb.message)
    state.clear.assert_awaited_once()
    cb.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_transfer_simple_calendar_cb_navigation_answers(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """transfer_simple_calendar_cb act=prev_y (navigation) → handler answers,
    state NOT touched (F1 fix, same as booking flow).
    """

    _patch_process_selection(monkeypatch, selected=False, selected_date=None)

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = _make_simple_calendar_callback(SimpleCalAct.prev_y)

    state = _make_state()
    await client_handlers.transfer_simple_calendar_cb(cb, callback_data, state)

    cb.answer.assert_awaited_once()
    state.clear.assert_not_awaited()
    state.set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_slot_cb_pre_fill_fallback_no_first_name(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T5a (B.13): slot_cb — from_user.first_name is None (private account
    without a profile name) → fallback to text-input flow:
    state.update_data(slot_id) + set_state(entering_name) + 'На чьё имя записываем?'.

    B.13 added a pre-fill branch (entering_name_pre_fill) for clients with a
    non-empty first_name in their Telegram profile. The fallback path here
    covers the 20% case (private accounts / empty first_name) — pre-B.13
    behavior unchanged for these users.
    """
    from bot.keyboards.client import BookSlotCallbackData

    slot_id = UUID("12345678-1234-5678-1234-567812345678")
    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    # B.13: first_name="" (empty string) triggers the fallback branch in
    # slot_cb — _client_first_name strips it to "" which is falsy. Pydantic
    # rejects first_name=None on User (required field), so use "".
    cb.from_user = User(id=111222333, is_bot=False, first_name="")
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = BookSlotCallbackData(slot_id=slot_id)

    state = _make_state()
    # service_title set by service_picker_cb/service_msg BEFORE selecting_slot
    # (Session 5.29 Task 2, W2 defensive check in slot_cb).
    state.get_data = AsyncMock(return_value={"service_title": "Стрижка"})
    await client_handlers.slot_cb(cb, callback_data, state)

    state.update_data.assert_awaited_once()
    assert state.update_data.call_args.kwargs.get("slot_id") == str(slot_id)
    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.entering_name

    text = _answer_text(cb.message)
    assert "На чьё имя" in text
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_name_msg_empty_name_rejected(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T5a: name_msg (client.py:175-178) — empty text or whitespace-only →
    'Имя не может быть пустым. Введите имя:' (retry, state NOT advanced).
    """
    msg = _make_message(user_id=111222333, text="   ")  # whitespace-only
    msg.text = "   "
    state = _make_state()

    await client_handlers.name_msg(msg, state)

    text = _answer_text(msg)
    assert "Имя не может быть пустым" in text
    state.update_data.assert_not_awaited()
    state.set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_name_msg_too_long_name_rejected(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T5a: name_msg (client.py:179-181) — name > 255 chars →
    'Имя слишком длинное (макс. 255 символов)...' (retry, state NOT advanced).
    """
    msg = _make_message(user_id=111222333, text="А" * 300)
    msg.text = "А" * 300
    state = _make_state()

    await client_handlers.name_msg(msg, state)

    text = _answer_text(msg)
    assert "слишком длинное" in text
    assert "255" in text
    state.update_data.assert_not_awaited()
    state.set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_name_msg_happy_renders_summary_slot_path(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """name_msg (slot path) — happy: name ok, slot_id + service_title in state →
    state.update_data(client_name) + set_state(confirming) + summary +
    confirm_keyboard via _render_summary_and_set_confirming.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        slot = Slot(
            master_id=ctx["master_id"],
            slot_date=target_date,
            slot_hour=14,
            status="open",
        )
        session.add(slot)
        await session.commit()
        slot_id = slot.id

    msg = _make_message(user_id=111222333, text="Паша")
    state = _make_state()
    await state.update_data(
        slot_id=str(slot_id),
        service_title="Стрижка",
    )

    await client_handlers.name_msg(msg, state)

    # W3 (5.51): update_data is now called TWICE — (1) client_name by
    # name_msg, (2) summary_msg_id by _render_summary_and_set_confirming.
    # `call_args` returns the LAST call — merge kwargs across all calls.
    saved_kwargs: dict[str, Any] = {}
    for call in state.update_data.await_args_list:
        saved_kwargs.update(call.kwargs)
    assert saved_kwargs.get("client_name") == "Паша"
    assert saved_kwargs.get("summary_msg_id") is not None, (
        "W3: summary message id must be saved for cancel_msg keyboard strip"
    )
    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.confirming

    text = _answer_text(msg)
    assert "Подтвердите" in text


@pytest.mark.asyncio
async def test_service_msg_typed_text_prompts_button_choice(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.51: free-text service input DISABLED. Any text typed while
    the service picker is on screen → hint to tap a button. State is NOT
    advanced, NOT cleared — the picker message above is still actionable.
    """
    msg = _make_message(user_id=111222333, text="Стрижка")
    state = _make_state()
    await state.update_data(selected_date="2026-09-12")
    # Prep update_data above pollutes the mock — reset so assert_not_awaited
    # below verifies the HANDLER's calls, not the setup.
    state.update_data.reset_mock()

    await client_handlers.service_msg(msg)

    text = _answer_text(msg)
    assert "выберите услугу кнопкой" in text
    state.update_data.assert_not_awaited()
    state.set_state.assert_not_awaited()
    state.clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_msg_empty_text_still_prompts_button_choice(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.51: even empty/whitespace text → same button hint (no
    separate empty-validation — free-text is disabled entirely, so there
    is nothing to validate).
    """
    msg = _make_message(user_id=111222333, text="   ")
    state = _make_state()

    await client_handlers.service_msg(msg)

    text = _answer_text(msg)
    assert "выберите услугу кнопкой" in text
    state.clear.assert_not_awaited()


# ============================================================
# Booking flow — T5b: confirm_cb + cancel_msg
# Coverage: client.py:249-364, 370-379
# ============================================================


def _make_confirm_callback(
    *,
    user_id: int = 111222333,
    username: str | None = None,
) -> tuple[MagicMock, Any]:
    """Mock CallbackQuery for confirm_cb (BookConfirmCallbackData filter).

    `username` — Telegram @username (None for users without one). Tests that
    verify @username propagation in confirm_cb should pass a non-None value.
    """
    from bot.keyboards.client import BookConfirmCallbackData

    bot = AsyncMock()
    bot.send_message = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(user_id, username=username)
    cb.message = _make_message(user_id, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = BookConfirmCallbackData()  # no fields, just filter marker
    return cb, callback_data


@pytest.mark.asyncio
async def test_confirm_cb_data_lost_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T5b: confirm_cb (client.py:264-270) — state missing slot_id/client_name/
    service_title → state.clear + 'Данные потеряны...' + callback.answer (abort).
    """
    cb, callback_data = _make_confirm_callback()
    state = _make_state()  # empty state, no keys
    scheduler = MagicMock(spec=AsyncIOScheduler)

    await client_handlers.confirm_cb(cb, callback_data, state, scheduler)

    state.clear.assert_awaited_once()
    text = _answer_text(cb.message)
    assert "Данные потеряны" in text
    cb.answer.assert_awaited()
    # No booking created, no scheduler call
    scheduler.add_job.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_cb_master_not_found_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T5b: confirm_cb (client.py:283-288) — empty DB, no master → state.clear +
    'Мастер не найден' + callback.answer (abort).
    """
    cb, callback_data = _make_confirm_callback()
    state = _make_state()
    # Populate state with required keys so we pass the 264 check
    fake_slot_id = UUID("00000000-0000-0000-0000-000000000001")
    await state.update_data(
        slot_id=str(fake_slot_id),
        client_name="Паша",
        service_title="Стрижка",
    )
    scheduler = MagicMock(spec=AsyncIOScheduler)

    # No _seed_full_stack — DB empty
    await client_handlers.confirm_cb(cb, callback_data, state, scheduler)

    state.clear.assert_awaited_once()
    text = _answer_text(cb.message)
    assert "Мастер не найден" in text
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_confirm_cb_business_not_found_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T5b: confirm_cb (client.py:292-297) — master exists but business_id FK
    broken (business row deleted out of band) → state.clear + 'Бизнес не найден'
    + callback.answer (abort).

    Setup mirrors admin test
    `test_resolve_master_and_business_returns_none_when_business_fk_broken`:
    seed full stack under settings.ADMIN_ID, then raw DELETE the business row
    (SQLite PRAGMA foreign_keys=OFF by default allows this, leaving
    master.business_id dangling).

    In prod (Postgres with FK ON), this branch is hit only on referential
    corruption — handler guards against `business.timezone` AttributeError.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session, client_telegram_id=111222333)
        biz_id = ctx["business_id"]
        # Seed an open slot (confirm_cb needs slot_id in state for the lookup
        # path, though we won't reach the booking service — the branch aborts
        # at business is None before any service call).
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
        slot = Slot(
            master_id=ctx["master_id"],
            slot_date=tomorrow,
            slot_hour=14,
            status="open",
        )
        session.add(slot)
        await session.flush()
        slot_id = slot.id
        # Delete the business row, leaving master.business_id dangling
        # (PRAGMA foreign_keys=OFF in aiosqlite by default — DELETE succeeds).
        from sqlalchemy import delete

        await session.execute(delete(Business).where(Business.id == biz_id))
        await session.commit()

    cb, callback_data = _make_confirm_callback(user_id=461355056)
    state = _make_state()
    await state.update_data(
        slot_id=str(slot_id),
        client_name="Паша",
        service_title="Стрижка",
    )
    scheduler = MagicMock(spec=AsyncIOScheduler)

    await client_handlers.confirm_cb(cb, callback_data, state, scheduler)

    state.clear.assert_awaited_once()
    text = _answer_text(cb.message)
    assert "Бизнес не найден" in text
    cb.answer.assert_awaited()
    # No service call (handler aborts before create_booking)
    scheduler.remove_job.assert_not_called()
    cb.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_cb_slot_already_booked_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T5b: confirm_cb (client.py:323-331) — create_booking raises
    SlotAlreadyBookedError (race: another user booked between date_cb and confirm_cb)
    → state.clear + 'Слот только что заняли...' + callback.answer (abort).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        slot = Slot(
            master_id=ctx["master_id"],
            slot_date=target_date,
            slot_hour=14,
            status="open",
        )
        session.add(slot)
        await session.commit()
        slot_id = slot.id

    async def _raise_already_booked(*args: Any, **kwargs: Any) -> Any:
        from bot.services import booking as booking_svc

        raise booking_svc.SlotAlreadyBookedError("race simulated")

    monkeypatch.setattr(client_handlers, "create_booking", _raise_already_booked)

    cb, callback_data = _make_confirm_callback()
    state = _make_state()
    await state.update_data(
        slot_id=str(slot_id),
        client_name="Паша",
        service_title="Стрижка",
    )
    scheduler = MagicMock(spec=AsyncIOScheduler)

    await client_handlers.confirm_cb(cb, callback_data, state, scheduler)

    state.clear.assert_awaited_once()
    text = _answer_text(cb.message)
    assert "Слот только что заняли" in text
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_confirm_cb_slot_in_past_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T5b: confirm_cb (client.py:332-339) — create_booking raises SlotInPastError
    (slot was in the past, validation failed) → state.clear + 'Это время уже
    прошло...' + callback.answer (abort).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        slot = Slot(
            master_id=ctx["master_id"],
            slot_date=target_date,
            slot_hour=14,
            status="open",
        )
        session.add(slot)
        await session.commit()
        slot_id = slot.id

    async def _raise_in_past(*args: Any, **kwargs: Any) -> Any:
        from bot.services import booking as booking_svc

        raise booking_svc.SlotInPastError("slot in past")

    monkeypatch.setattr(client_handlers, "create_booking", _raise_in_past)

    cb, callback_data = _make_confirm_callback()
    state = _make_state()
    await state.update_data(
        slot_id=str(slot_id),
        client_name="Паша",
        service_title="Стрижка",
    )
    scheduler = MagicMock(spec=AsyncIOScheduler)

    await client_handlers.confirm_cb(cb, callback_data, state, scheduler)

    state.clear.assert_awaited_once()
    text = _answer_text(cb.message)
    assert "время уже прошло" in text
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_confirm_cb_slot_closed_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T5b: confirm_cb (client.py:340-347) — create_booking raises SlotClosedError
    (master closed slot between steps) → state.clear + 'Слот закрыт мастером...'
    + callback.answer (abort).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        slot = Slot(
            master_id=ctx["master_id"],
            slot_date=target_date,
            slot_hour=14,
            status="open",
        )
        session.add(slot)
        await session.commit()
        slot_id = slot.id

    async def _raise_closed(*args: Any, **kwargs: Any) -> Any:
        from bot.services import booking as booking_svc

        raise booking_svc.SlotClosedError("slot closed")

    monkeypatch.setattr(client_handlers, "create_booking", _raise_closed)

    cb, callback_data = _make_confirm_callback()
    state = _make_state()
    await state.update_data(
        slot_id=str(slot_id),
        client_name="Паша",
        service_title="Стрижка",
    )
    scheduler = MagicMock(spec=AsyncIOScheduler)

    await client_handlers.confirm_cb(cb, callback_data, state, scheduler)

    state.clear.assert_awaited_once()
    text = _answer_text(cb.message)
    assert "Слот закрыт мастером" in text
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_confirm_cb_happy_creates_booking_and_schedules(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T5b: confirm_cb (client.py:315-364) — happy: create_booking succeeds →
    master notification sent (callback.bot.send_message) + schedule_for_booking
    called + state.clear + 'Вы записаны' message.

    We monkeypatch create_booking to return a fake result (avoid coupling to
    service internals) and schedule_for_booking to a mock (avoid APScheduler
    global state). This test focuses on handler I/O, not service logic.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        slot = Slot(
            master_id=ctx["master_id"],
            slot_date=target_date,
            slot_hour=14,
            status="open",
        )
        session.add(slot)
        await session.commit()
        slot_id = slot.id

    # Fake create_booking result
    fake_result = MagicMock()
    fake_result.booking_id = UUID("00000000-0000-0000-0000-000000000002")
    fake_result.start_at = datetime.now(UTC) + timedelta(days=1)
    fake_result.master_notification_text = "Новая запись: Паша, Стрижка"

    async def _fake_create(*args: Any, **kwargs: Any) -> Any:
        return fake_result

    monkeypatch.setattr(client_handlers, "create_booking", _fake_create)

    schedule_mock = MagicMock()
    monkeypatch.setattr(client_handlers, "schedule_for_booking", schedule_mock)

    cb, callback_data = _make_confirm_callback()
    state = _make_state()
    await state.update_data(
        slot_id=str(slot_id),
        client_name="Паша",
        service_title="Стрижка",
    )
    scheduler = MagicMock(spec=AsyncIOScheduler)

    await client_handlers.confirm_cb(cb, callback_data, state, scheduler)

    # Master notification sent
    cb.bot.send_message.assert_awaited_once()
    notify_kwargs = cb.bot.send_message.await_args.kwargs
    notify_text = notify_kwargs.get("text", "")
    assert "Паша" in notify_text or "Стрижка" in notify_text

    # schedule_for_booking called with booking_id + start_at
    schedule_mock.assert_called_once()
    schedule_args = schedule_mock.call_args.args
    assert schedule_args[0] is scheduler
    assert schedule_args[1] == fake_result.booking_id

    # state.clear + success message
    state.clear.assert_awaited_once()
    # B.13: confirm_cb sends 2 messages now — (1) "✅ Вы записаны" with inline
    # post_booking_keyboard, (2) "👇 Кнопки внизу" with reply keyboard
    # (via _restore_reply_keyboard_async). Use await_args_list[0] for the
    # success message — _answer_text returns the LAST call ("👇 Кнопки внизу").
    assert cb.message.answer.await_count == 2
    first_text = str(cb.message.answer.await_args_list[0].args[0])
    assert "Вы записаны" in first_text
    second_text = str(cb.message.answer.await_args_list[1].args[0])
    assert "Кнопки внизу" in second_text, "B.13: 2nd message restores reply keyboard"
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_confirm_cb_propagates_username_to_create_booking(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W5 (code-review iter 2): confirm_cb propagates callback.from_user.username
    to BookingCreate.telegram_username. Without this test, all confirm_cb
    unit-tests ran with username=None (default in _make_user) — only the
    fallback (telegram_id) path was exercised at handler level.

    Setup: _make_confirm_callback(username="pasha_ivanov") → cb.from_user has
    @username. Monkeypatch create_booking to capture the BookingCreate payload
    it received. Assert payload.telegram_username == "pasha_ivanov".

    Service-level coverage exists (test_create_booking_username_in_notification
    in test_booking.py:2827), but that tests the service, not the handler's
    extraction of from_user.username → BookingCreate. This test closes that gap.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        slot = Slot(
            master_id=ctx["master_id"],
            slot_date=target_date,
            slot_hour=14,
            status="open",
        )
        session.add(slot)
        await session.commit()
        slot_id = slot.id

    captured_payload: list[Any] = []

    async def _capture_create(_session: Any, payload: Any, **kwargs: Any) -> Any:
        captured_payload.append(payload)
        fake = MagicMock()
        fake.booking_id = UUID("00000000-0000-0000-0000-000000000003")
        fake.start_at = datetime.now(UTC) + timedelta(days=1)
        fake.master_notification_text = "Новая запись: Паша (@pasha_ivanov)"
        return fake

    monkeypatch.setattr(client_handlers, "create_booking", _capture_create)
    schedule_mock = MagicMock()
    monkeypatch.setattr(client_handlers, "schedule_for_booking", schedule_mock)

    cb, callback_data = _make_confirm_callback(username="pasha_ivanov")
    state = _make_state()
    await state.update_data(
        slot_id=str(slot_id),
        client_name="Паша",
        service_title="Стрижка",
    )
    scheduler = MagicMock(spec=AsyncIOScheduler)

    await client_handlers.confirm_cb(cb, callback_data, state, scheduler)

    assert len(captured_payload) == 1, "create_booking called once"
    assert captured_payload[0].telegram_username == "pasha_ivanov", (
        "W5: confirm_cb must propagate callback.from_user.username → "
        "BookingCreate.telegram_username"
    )


@pytest.mark.asyncio
async def test_cancel_msg_clears_state_and_answers(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T5b: cancel_msg (client.py:370-379) — /cancel inside FSM → state.clear()
    BEFORE answer (race) + 'Ввод отменён. /book чтобы начать заново'.

    B.13: cancel_msg now sends a 2nd message ('👇 Кнопки внизу' with reply
    keyboard) via _restore_reply_keyboard_async — the reply keyboard was
    hidden in slot_cb/slot_30_cb via ReplyKeyboardRemove when entering the
    booking flow. Verify both messages via await_args_list.
    """
    msg = _make_message(user_id=111222333, text="/cancel")
    state = _make_state()

    await client_handlers.cancel_msg(msg, state)

    state.clear.assert_awaited_once()
    # B.13: 2 messages — hint + reply keyboard restore.
    assert msg.answer.await_count == 2
    first_text = str(msg.answer.await_args_list[0].args[0])
    assert "Ввод отменён" in first_text
    assert "/book" in first_text
    second_text = str(msg.answer.await_args_list[1].args[0])
    assert "Кнопки внизу" in second_text


@pytest.mark.asyncio
async def test_cancel_msg_slots_path_hint_directs_to_slots() -> None:
    """Этап 5.8b W2 (code-review iter 2): cancel_msg при is_slots_path=True в
    state → hint '/slots' (NOT '/book'). User был в /slots flow, нажал /cancel
    из entering_name/entering_service — должен получить retry-cmd для своего flow.

    B.13: cancel_msg sends a 2nd '👇 Кнопки внизу' reply-keyboard restore
    message — use await_args_list[0] for the hint.
    """
    msg = _make_message(user_id=111222333, text="/cancel")
    state = _make_state()
    await state.update_data(is_slots_path=True)

    await client_handlers.cancel_msg(msg, state)

    state.clear.assert_awaited_once()
    assert msg.answer.await_count == 2
    first_text = str(msg.answer.await_args_list[0].args[0])
    assert "Ввод отменён" in first_text
    assert "/slots" in first_text
    assert "/book" not in first_text, "W2 fix: /slots user must NOT see /book hint"
    second_text = str(msg.answer.await_args_list[1].args[0])
    assert "Кнопки внизу" in second_text


@pytest.mark.asyncio
async def test_cancel_msg_transfer_state_hint_directs_to_mybookings() -> None:
    """Review W2 (5.51): /cancel во время TransferStates. Transfer пишет в FSM
    transfer_booking_id + is_slots_path=True (B.1) — без ветки по transfer
    пользователь получал бы '/slots' хинт вместо возврата к своим записям.
    """
    msg = _make_message(user_id=111222333, text="/cancel")
    state = _make_state()
    await state.update_data(
        transfer_booking_id="00000000-0000-0000-0000-000000000001",
        is_slots_path=True,
    )

    await client_handlers.cancel_msg(msg, state)

    state.clear.assert_awaited_once()
    first_text = str(msg.answer.await_args_list[0].args[0])
    assert "Перенос отменён" in first_text
    assert "/mybookings" in first_text
    assert "/slots" not in first_text, "W2: transfer user must see /mybookings, not /slots"


@pytest.mark.asyncio
async def test_cancel_msg_strips_confirm_keyboard_from_summary_message() -> None:
    """Review W3 (5.51): /cancel из confirming должен погасить ✅/❌ клавиатуру
    на summary-сообщении. _render_summary_and_set_confirming сохраняет
    summary_msg_id в FSM; cancel_msg читает его ДО state.clear и зовёт
    bot.edit_message_reply_markup(reply_markup=None).
    """
    msg = _make_message(user_id=111222333, text="/cancel")
    msg.bot = AsyncMock()
    msg.chat = MagicMock(id=111222333)
    state = _make_state()
    await state.update_data(summary_msg_id=42, is_slots_path=False)

    await client_handlers.cancel_msg(msg, state)

    state.clear.assert_awaited_once()
    msg.bot.edit_message_reply_markup.assert_awaited_once()
    kwargs = msg.bot.edit_message_reply_markup.await_args.kwargs
    assert kwargs.get("message_id") == 42
    assert kwargs.get("reply_markup") is None, "summary ✅/❌ keyboard must be stripped"


@pytest.mark.asyncio
async def test_cancel_msg_without_summary_msg_id_skips_edit() -> None:
    """W3 guard: если summary_msg_id в state нет (не confirming-флоу, /
    cancel из selecting_date и т.п.) — bot.edit_message_reply_markup НЕ
    вызывается (нечего гасить, сообщения с клавиатурой может не быть).
    """
    msg = _make_message(user_id=111222333, text="/cancel")
    msg.bot = AsyncMock()
    state = _make_state()

    await client_handlers.cancel_msg(msg, state)

    msg.bot.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_state_callback_fallback_strips_dead_keyboard() -> None:
    """Review W1 (5.51): no_state_callback_fallback — тап по кнопке без
    живого хендлера (stale пикер после session timeout / рестарта бота).
    Без strip каждый повторный тап снова даёт alert навсегда. Фикс: гасить
    клавиатуру тапнутого сообщения + alert.
    """
    cb = _make_string_callback("some_stale_prefix")
    cb.message = _make_message(111222333, text="<unused>")

    await client_handlers.no_state_callback_fallback(cb)

    cb.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    cb.answer.assert_awaited_once()
    alert_kwargs = cb.answer.await_args.kwargs
    assert alert_kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_cancel_flow_cb_transfer_state_hint_directs_to_mybookings(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Review W2 (5.51): stale ❌ Отмена, тапнутая во время TransferStates —
    cancel_flow_cb (StateFilter('*')) чистит transfer-FSM. Хинт должен быть
    '/mybookings' (пользователь переносил запись из списка), а НЕ '/slots'
    (is_slots_path=True заливается transfer-флоу по B.1).
    """
    cb = _make_string_callback("book_cancel")
    state = _make_state()
    await state.update_data(
        transfer_booking_id="00000000-0000-0000-0000-000000000001",
        is_slots_path=True,
    )

    await client_handlers.cancel_flow_cb(cb, state)

    state.clear.assert_awaited_once()
    # Two answers: (1) hint, (2) '👇 Кнопки внизу' reply-keyboard restore —
    # assert on the FIRST one (hint), _answer_text returns the last.
    assert cb.message.answer.await_count == 2
    first_text = str(cb.message.answer.await_args_list[0].args[0])
    assert "Перенос отменён" in first_text
    assert "/mybookings" in first_text
    assert "/slots" not in first_text, "W2: transfer user must see /mybookings, not /slots"
    # Клавиатура тапнутого сообщения погашена + reply keyboard восстановлена.
    cb.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_simple_calendar_cb_cancel_slots_path_hint_directs_to_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Этап 5.8b W1 (code-review iter 2): simple_calendar_cb act=cancel при
    is_slots_path=True → 'Ввод отменён. /slots ...' (NOT '/book'). /slots user
    нажал 'Отмена' в календаре → должен получить retry-cmd для своего flow.
    """
    _patch_process_selection(monkeypatch, selected=False, selected_date=None)

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = _make_simple_calendar_callback(SimpleCalAct.cancel)

    state = _make_state()
    await state.update_data(is_slots_path=True)
    await client_handlers.simple_calendar_cb(cb, callback_data, state)

    state.clear.assert_awaited_once()
    text = _answer_text(cb.message)
    assert "Ввод отменён" in text
    assert "/slots" in text
    assert "/book" not in text, "W1 fix: /slots user must NOT see /book hint"


@pytest.mark.asyncio
async def test_confirm_cb_xor_abort_slots_path_hint_directs_to_slots(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Этап 5.8b W3 (code-review iter 2): confirm_cb XOR contract abort при
    workday_id в state (но slot_id also set — corrupted FSM) → 'Данные потеряны.
    ... /slots' (NOT '/book'). Согласованность с SlotAlreadyBooked/SlotInPast
    retry-cmd branching в том же handler.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        wd = await _seed_workday(session, ctx, work_date=target_date)
        workday_id = wd.id

    cb, callback_data = _make_confirm_callback()
    state = _make_state()
    # Corrupted FSM: BOTH workday_id и slot_id set (XOR violation). has_workday_path
    # True (workday_id + start_minute), has_slot_path True (slot_id) → XOR False → abort.
    await state.update_data(
        workday_id=str(workday_id),
        start_minute=600,
        slot_id=str(uuid4()),  # also set → XOR contract violation
        client_name="Паша",
        service_title="Стрижка",
    )
    scheduler = MagicMock(spec=AsyncIOScheduler)

    await client_handlers.confirm_cb(cb, callback_data, state, scheduler)

    state.clear.assert_awaited_once()
    text = _answer_text(cb.message)
    assert "Данные потеряны" in text
    assert "/slots" in text, "W3 fix: workday-path abort must direct to /slots"
    assert "/book" not in text, "W3 fix: NOT /book for workday-path"
    cb.answer.assert_awaited()


# ============================================================
# Tier 2 (T10) — client handler edge branches (NEXT_COVERAGE_GAPS.md)
# Covers bot/handlers/client.py:
#   406      — mybookings_msg from_user is None (channel_post edge)
#   501-502  — mybookings_cancel_cb from_user is None (channel_post edge)
#   525-526  — mybookings_cancel_cb BookingAlreadyCancelledError (concurrent cancel)
#   555      — no_state_fallback: text without state, without /, → "Начните через /book"
#   588-589  — mybookings_transfer_cb from_user is None (channel_post edge)
#   612-613  — mybookings_transfer_cb booking.status == 'cancelled' (already cancelled)
#   741-742  — transfer_slot_cb from_user is None (channel_post edge)
# Skipped (FK surgery, separate task):
#   293-297  — confirm_cb business is None (broken FK)
# ============================================================


@pytest.mark.asyncio
async def test_mybookings_msg_from_user_is_none_early_return(
    patched_session_factory: Any,
) -> None:
    """Covers client.py:406 — `if message.from_user is None: return` in
    mybookings_msg. Edge case: channel_post triggers Command filter with no
    from_user (rare but defensive). Handler must early-return without crash.

    Setup: message.from_user = None (MagicMock spec=Message allows this).
    Assert: no message.answer call, no exception raised.
    """
    msg = MagicMock(spec=Message)
    msg.from_user = None
    msg.text = "/mybookings"
    msg.answer = AsyncMock()

    await client_handlers.mybookings_msg(msg)

    msg.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_mybookings_cancel_cb_from_user_none_early_return(
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
) -> None:
    """Covers client.py:501-502 — `if callback.from_user is None: callback.answer();
    return` in mybookings_cancel_cb. Edge case: callback_query without from_user
    (defensive — Telegram always populates from_user for callback queries,
    but the guard prevents AttributeError if Bot API changes).

    Setup: callback.from_user = None. Assert: callback.answer called, no
    message.answer, no DB query, no scheduler call.
    """
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = None
    cb.message = MagicMock(spec=Message)
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    cb.bot = AsyncMock()
    cb.bot.send_message = AsyncMock()
    cb_data = MyBookingsCancelCallbackData(booking_id=UUID("00000000-0000-0000-0000-000000000000"))

    await client_handlers.mybookings_cancel_cb(cb, cb_data, mock_scheduler)

    cb.answer.assert_awaited_once()
    cb.message.answer.assert_not_awaited()
    cb.bot.send_message.assert_not_called()
    mock_scheduler.remove_job.assert_not_called()


@pytest.mark.asyncio
async def test_mybookings_cancel_cb_booking_already_cancelled_race(
    session_factory: Any,
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers client.py:525-526 — `except BookingAlreadyCancelledError:
    callback.answer('Запись уже отменена'); return`.

    Race scenario: user taps [Отменить], but a concurrent request (or admin
    via /closeslot triggering cancel_booking) already cancelled the booking
    between handler SELECT of client and UPDATE in cancel_booking service.
    cancel_booking raises BookingAlreadyCancelledError → handler shows
    short popup (callback.answer, NOT message.answer — short text).

    Setup: seed booking, monkeypatch cancel_booking to raise.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=future_local)
        booking_id = booking.id

    from bot.services import booking as booking_svc

    async def _raise_already_cancelled(*args: Any, **kwargs: Any) -> Any:
        raise booking_svc.BookingAlreadyCancelledError("concurrent cancel race")

    monkeypatch.setattr(client_handlers, "cancel_booking", _raise_already_cancelled)

    bot = AsyncMock()
    cb, cb_data = _make_callback(user_id=111222333, booking_id=booking_id, bot=bot)

    await client_handlers.mybookings_cancel_cb(cb, cb_data, mock_scheduler)

    # Short popup via callback.answer (NOT message.answer — short text per
    # handler contract for 'already cancelled' / 'not found' cases).
    cb.answer.assert_awaited()
    answer_args = cb.answer.call_args.args
    assert answer_args and "Запись уже отменена" in answer_args[0]
    cb.message.answer.assert_not_awaited()
    bot.send_message.assert_not_called()
    mock_scheduler.remove_job.assert_not_called()


@pytest.mark.asyncio
async def test_no_state_fallback_text_without_state_answers_hint(
    patched_session_factory: Any,
) -> None:
    """Covers client.py:555 — no_state_fallback handler: when user sends text
    (not a /command) while FSM is in State(None) (bot restart mid-FSM, lost
    MemoryStorage state), handler replies 'Начните запись через /book'.

    Direct handler invocation (no FSMContext needed — handler doesn't read
    state, just message.answer).
    """
    msg = _make_message(user_id=111222333, text="привет")

    await client_handlers.no_state_fallback(msg)

    text = _answer_text(msg)
    assert "Начните запись через /book" in text


@pytest.mark.asyncio
async def test_mybookings_transfer_cb_from_user_none_early_return(
    patched_session_factory: Any,
) -> None:
    """Covers client.py:588-589 — `if callback.from_user is None: callback.answer();
    return` in mybookings_transfer_cb. Symmetric to mybookings_cancel_cb guard.

    Setup: callback.from_user = None, no state needed (handler early-returns
    before state access).
    """
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = None
    cb.message = MagicMock(spec=Message)
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    cb.bot = AsyncMock()
    cb.bot.send_message = AsyncMock()
    cb_data = MyBookingsTransferCallbackData(
        booking_id=UUID("00000000-0000-0000-0000-000000000000")
    )
    state = _make_state()

    await client_handlers.mybookings_transfer_cb(cb, cb_data, state)

    cb.answer.assert_awaited_once()
    cb.message.answer.assert_not_awaited()
    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()
    state.clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_mybookings_transfer_cb_booking_already_cancelled(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Covers client.py:612-613 — `if booking.status == 'cancelled':
    callback.answer('Запись уже отменена'); return` in mybookings_transfer_cb.

    Scenario: user had a booking, cancelled it via /mybookings, then tapped
    [🔄 Перенести] on a stale keyboard (rendered before cancel). Handler
    re-fetches booking, sees status='cancelled', early-returns with short
    popup.

    Setup: seed booking, mark status='cancelled' (cancelled state), invoke
    transfer entry. Assert: short popup, no FSM state change.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(
            session, ctx=ctx, start_at_local=future_local, status="cancelled"
        )
        booking_id = booking.id

    bot = AsyncMock()
    cb, cb_data = _make_transfer_callback(user_id=111222333, booking_id=booking_id, bot=bot)
    state = _make_state()

    await client_handlers.mybookings_transfer_cb(cb, cb_data, state)

    cb.answer.assert_awaited()
    answer_args = cb.answer.call_args.args
    assert answer_args and "Запись уже отменена" in answer_args[0]
    cb.message.answer.assert_not_awaited()
    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_transfer_slot_cb_from_user_none_early_return(
    patched_session_factory: Any,
    mock_scheduler: MagicMock,
) -> None:
    """Covers client.py:741-742 — `if callback.from_user is None: callback.answer();
    return` in transfer_slot_cb. Symmetric guard to other callback handlers.

    Setup: callback.from_user = None. Handler early-returns before state
    access, before transfer_booking call.
    """
    from bot.keyboards.client import BookSlotCallbackData

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = None
    cb.message = MagicMock(spec=Message)
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    cb.bot = AsyncMock()
    cb.bot.send_message = AsyncMock()
    cb_data = BookSlotCallbackData(slot_id=UUID("00000000-0000-0000-0000-000000000000"))
    state = _make_state()

    await client_handlers.transfer_slot_cb(cb, cb_data, state, mock_scheduler)

    cb.answer.assert_awaited_once()
    cb.message.answer.assert_not_awaited()
    cb.bot.send_message.assert_not_called()
    state.clear.assert_not_awaited()
    mock_scheduler.remove_job.assert_not_called()


# ============================================================
# Tier 2 (Bonus) — keyboards edges (NEXT_COVERAGE_GAPS.md)
# Covers bot/keyboards/client.py:
#   102-103 — slot_picker_keyboard([]) → "Нет свободных слотов" noop button
#   124     — _no_op_button() helper (used in tests + empty-slots branch)
# ============================================================


def test_slot_picker_keyboard_empty_slots_returns_noop_button() -> None:
    """Covers keyboards/client.py — `if not slots: button("Нет свободных
    слотов", callback_data="noop"); return markup`.

    Empty slots list → "Нет свободных слотов" placeholder + "↩️ Назад"
    button (Session 5.30 S1: back-button added for UX consistency with
    slot_picker_keyboard_30min). Verifies the edge case branch (vs the
    for-loop default path that builds per-slot buttons).
    """
    from bot.keyboards.client import slot_picker_keyboard

    markup = slot_picker_keyboard([])

    assert isinstance(markup, InlineKeyboardMarkup)
    # Two buttons on row 0: "Нет свободных слотов" (noop) + "↩️ Назад".
    buttons = markup.inline_keyboard
    assert len(buttons) == 1
    assert len(buttons[0]) == 2  # Session 5.30 S1: +1 back button
    placeholder, back_btn = buttons[0][0], buttons[0][1]
    assert placeholder.text == "Нет свободных слотов"
    assert placeholder.callback_data == "noop"
    assert back_btn.text == "↩️ Назад"
    assert back_btn.callback_data == "book_back_to_service"


def test_slot_picker_keyboard_non_empty_has_back_button() -> None:
    """Session 5.31 S1 review W1: legacy slot_picker_keyboard NON-empty case
    must include "↩️ Назад" (callback_data="book_back_to_service") as the
    last button — UX consistency with slot_picker_keyboard_30min.
    """
    from types import SimpleNamespace
    from uuid import uuid4

    from bot.keyboards.client import slot_picker_keyboard

    slots = [
        SimpleNamespace(id=uuid4(), slot_hour=14),
        SimpleNamespace(id=uuid4(), slot_hour=16),
        SimpleNamespace(id=uuid4(), slot_hour=18),
    ]

    markup = slot_picker_keyboard(slots)

    assert isinstance(markup, InlineKeyboardMarkup)
    flat_buttons = [(btn.text, btn.callback_data) for row in markup.inline_keyboard for btn in row]
    # Last button is back, with correct callback_data (booking flow default).
    last_text, last_cb = flat_buttons[-1]
    assert last_text == "↩️ Назад"
    assert last_cb == "book_back_to_service"
    # All preceding buttons are slot buttons (not back).
    assert all("↩️ Назад" not in t for t, _ in flat_buttons[:-1])


def test_slot_picker_keyboard_show_back_false_suppresses_back_button() -> None:
    """Session 5.31 S1 review F2: slot_picker_keyboard with show_back=False
    (transfer flow) — no "↩️ Назад" button rendered, in both empty and
    non-empty cases. Prevents dead-button UX regression (back handler has
    BookingStates.selecting_slot StateFilter, doesn't cover
    TransferStates.selecting_slot).
    """
    from types import SimpleNamespace
    from uuid import uuid4

    from bot.keyboards.client import slot_picker_keyboard

    # Non-empty case: slots only, no back button.
    slots = [SimpleNamespace(id=uuid4(), slot_hour=14)]
    markup_non_empty = slot_picker_keyboard(slots, show_back=False)
    flat_texts_ne = [btn.text for row in markup_non_empty.inline_keyboard for btn in row]
    assert "↩️ Назад" not in flat_texts_ne
    assert any(":00" in t for t in flat_texts_ne), "slot button must be present"

    # Empty case: placeholder only, no back button.
    markup_empty = slot_picker_keyboard([], show_back=False)
    flat_texts_e = [btn.text for row in markup_empty.inline_keyboard for btn in row]
    assert flat_texts_e == ["Нет свободных слотов"]
    assert "↩️ Назад" not in flat_texts_e


def test_no_op_button_helper_returns_noop_inline_button() -> None:
    """Covers keyboards/client.py:124 — _no_op_button() helper returns an
    InlineKeyboardButton with text="Нет свободных слотов" and callback_data="noop".

    NOTE: this helper is currently NOT called by slot_picker_keyboard (which
    uses builder.button() directly at keyboards/client.py:102). The helper
    is a standalone contract reference (defined "used in tests" per its own
    docstring). The two share the same button text/callback_data contract
    but are independent implementations.
    """
    from bot.keyboards.client import _no_op_button

    button = _no_op_button()

    assert button.text == "Нет свободных слотов"
    assert button.callback_data == "noop"


# ============================================================
# Этап 5.8b — /slots + BookSlot30CallbackData + workday-path
# handlers (cmd_slots, simple_calendar_cb slots branch, slot_30_cb,
# service_msg workday branch, confirm_cb workday branch + new
# exceptions BookingOutsideWorkDayError / WorkDayCapacityExceededError).
# P0 critic iter 2: confirm_cb must catch workday-path race errors.
# ============================================================


async def _seed_workday(
    session: AsyncSession,
    ctx: dict[str, Any],
    *,
    work_date: date,
    is_active: bool = True,
    start_time: time = time(10, 0),
    end_time: time = time(18, 0),
    max_concurrent_clients: int = 1,
) -> WorkDay:
    """Insert a WorkDay row for the seeded master. Returns the WorkDay instance.

    Mirrors the WorkDay schema (5.1 /openday) — max_concurrent_clients defaults
    to 1 to surface overlaps via WorkDayCapacityExceededError.
    """
    wd = WorkDay(
        master_id=ctx["master_id"],
        work_date=work_date,
        start_time=start_time,
        end_time=end_time,
        is_active=is_active,
        max_concurrent_clients=max_concurrent_clients,
    )
    session.add(wd)
    await session.commit()
    return wd


def _make_slot_30_callback(
    workday_id: UUID,
    start_minute: int,
    *,
    user_id: int = 111222333,
) -> tuple[MagicMock, Any]:
    """Mock CallbackQuery for slot_30_cb (BookSlot30CallbackData filter).

    Builds a real BookSlot30CallbackData so .filter() matches on dispatch.
    """
    from bot.keyboards.client import BookSlot30CallbackData

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(user_id)
    cb.message = _make_message(user_id, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = BookSlot30CallbackData(workday_id=workday_id, start_minute=start_minute)
    return cb, callback_data


@pytest.mark.asyncio
async def test_cmd_slots_sets_state_and_shows_date_picker(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Этап 5.8b + BB-110: cmd_slots — /slots resolves master, queries
    bookable WorkDay-only dates (include_legacy_slots=False), sets FSM
    selecting_date + is_slots_path=True + shows date_picker_keyboard.

    is_slots_path=True flag is read by _process_selected_date to dispatch to
    WorkDay lookup + 30-min slot picker (NOT legacy slot picker, which is /book
    only). A date opened via /addslots (legacy) must NOT appear under /slots
    — pre-filter scope is workday-only (mirror cmd_slots post-tap semantics).

    Seeded: master + active WorkDay tomorrow (10:00-18:00).
    """
    tomorrow = (datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=1)).date()
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        await _seed_workday(session, ctx, work_date=tomorrow)

    msg = _make_message(user_id=111222333, text="/slots")
    state = _make_state()

    await client_handlers.cmd_slots(msg, state)

    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.selecting_date

    # state.update_data called with is_slots_path=True (the slots-branch flag)
    update_kwargs = state.update_data.call_args.kwargs
    assert update_kwargs.get("is_slots_path") is True

    msg.answer.assert_awaited_once()
    assert "Выберите дату" in _answer_text(msg)
    assert isinstance(_answer_reply_markup(msg), InlineKeyboardMarkup)


@pytest.mark.asyncio
async def test_cmd_slots_excludes_legacy_only_dates(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """BB-110 scope invariant: /slots pre-filter excludes dates opened ONLY
    via /addslots (legacy slots) — include_legacy_slots=False. A date with
    legacy open slots but NO WorkDay should not appear under /slots (it
    surfaces under /book instead, where legacy fallback path is preserved).

    Seeded: master + 1 legacy open Slot tomorrow, NO WorkDay.
    Expect: empty bookable list → 'нет свободных дат' empty state.
    """
    tomorrow = (datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=1)).date()
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        # Legacy open slot for tomorrow (slot_hour=14).
        session.add(
            Slot(
                master_id=ctx["master_id"],
                slot_date=tomorrow,
                slot_hour=14,
                status="open",
            )
        )
        await session.commit()

    msg = _make_message(user_id=111222333, text="/slots")
    state = _make_state()

    await client_handlers.cmd_slots(msg, state)

    state.set_state.assert_not_awaited()  # empty → no FSM entered
    assert "нет свободных дат" in _answer_text(msg).lower()


@pytest.mark.asyncio
async def test_slot_30_cb_pre_fill_fallback_no_first_name() -> None:
    """Этап 5.8b + B.13: slot_30_cb — from_user.first_name is None → fallback
    to text-input flow (entering_name, NOT entering_name_pre_fill).

    Mirrors test_slot_cb_pre_fill_fallback_no_first_name for the workday-path
    (slot_30_cb). _make_slot_30_callback builds a User with first_name='Test'
    by default — we override cb.from_user to None first_name for the fallback
    branch.
    """
    workday_id = uuid4()
    cb, callback_data = _make_slot_30_callback(
        workday_id=workday_id,
        start_minute=630,  # 10:30
    )
    # B.13: first_name="" (empty string) triggers the fallback branch.
    # _client_first_name strips to "" (falsy) → entering_name (NOT pre_fill).
    cb.from_user = User(id=111222333, is_bot=False, first_name="")

    state = _make_state()
    # service_title set by service_picker_cb/service_msg BEFORE selecting_slot
    # (Session 5.29 Task 2, W2 defensive check in slot_30_cb).
    state.get_data = AsyncMock(return_value={"service_title": "Стрижка"})
    await client_handlers.slot_30_cb(cb, callback_data, state)

    update_kwargs = state.update_data.call_args.kwargs
    assert update_kwargs["workday_id"] == str(workday_id)
    assert update_kwargs["start_minute"] == 630
    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.entering_name
    assert "На чьё имя" in _answer_text(cb.message)
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_slot_30_cb_out_of_range_clears_state() -> None:
    """Этап 5.8b: slot_30_cb, start_minute outside 0-1439 → state.clear +
    '❌ Ошибка выбора времени' + callback.answer. Defensive against tampered
    callback_data (slot_30_cb:range_check).
    """
    workday_id = uuid4()
    # start_minute=1500 is out-of-range (max valid 1439 = 23:59).
    cb, callback_data = _make_slot_30_callback(workday_id=workday_id, start_minute=1500)

    state = _make_state()
    await client_handlers.slot_30_cb(cb, callback_data, state)

    state.clear.assert_awaited_once()
    assert "Ошибка выбора времени" in _answer_text(cb.message)
    state.set_state.assert_not_awaited()
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_slot_cb_missing_service_title_clears_state() -> None:
    """Session 5.29 Task 2 W2: slot_cb, service_title missing in state
    (state corruption from in-flight session carried over from pre-5.29 flow)
    → state.clear + 'Данные потеряны' + callback.answer. Defensive check BEFORE
    set_state(entering_name) so no stale entering_name state.
    """
    from bot.keyboards.client import BookSlotCallbackData

    slot_id = UUID("12345678-1234-5678-1234-567812345678")
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = AsyncMock()
    callback_data = BookSlotCallbackData(slot_id=slot_id)

    state = _make_state()
    # No service_title — simulates state corruption.
    state.get_data = AsyncMock(return_value={})
    await client_handlers.slot_cb(cb, callback_data, state)

    state.clear.assert_awaited_once()
    assert "Данные потеряны" in _answer_text(cb.message)
    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_slot_30_cb_missing_service_title_clears_state() -> None:
    """Session 5.29 Task 2 W2: slot_30_cb, service_title missing in state
    (state corruption) → state.clear + 'Данные потеряны' + callback.answer.
    Defensive check after range check, BEFORE set_state(entering_name).
    """
    workday_id = uuid4()
    cb, callback_data = _make_slot_30_callback(
        workday_id=workday_id,
        start_minute=630,  # 10:30 — valid range
    )

    state = _make_state()
    # No service_title — simulates state corruption.
    state.get_data = AsyncMock(return_value={})
    await client_handlers.slot_30_cb(cb, callback_data, state)

    state.clear.assert_awaited_once()
    assert "Данные потеряны" in _answer_text(cb.message)
    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_confirm_cb_workday_path_happy_creates_booking(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Этап 5.8b: confirm_cb workday path — workday_id + start_minute in state
    → BookingCreate(workday_id, start_time_local) → create_booking succeeds →
    master notification + schedule_for_booking + 'Вы записаны'.

    Mirrors test_confirm_cb_happy_creates_booking_and_schedules but for the
    workday path (no slot_id in state).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        wd = await _seed_workday(
            session, ctx, work_date=target_date, start_time=time(10, 0), end_time=time(12, 0)
        )
        workday_id = wd.id

    fake_result = MagicMock()
    fake_result.booking_id = UUID("00000000-0000-0000-0000-000000000003")
    fake_result.start_at = datetime.now(UTC) + timedelta(days=1)
    fake_result.master_notification_text = "Новая запись: Паша, Стрижка"

    async def _fake_create(*args: Any, **kwargs: Any) -> Any:
        # Verify the payload is workday-path (XOR contract).
        payload = args[1]
        assert payload.workday_id == workday_id
        assert payload.slot_id is None
        # start_minute=630 → 10:30 LOCAL
        assert payload.start_time_local == time(10, 30)
        return fake_result

    monkeypatch.setattr(client_handlers, "create_booking", _fake_create)
    schedule_mock = MagicMock()
    monkeypatch.setattr(client_handlers, "schedule_for_booking", schedule_mock)

    cb, callback_data = _make_confirm_callback()
    state = _make_state()
    await state.update_data(
        workday_id=str(workday_id),
        start_minute=630,
        client_name="Паша",
        service_title="Стрижка",
    )
    scheduler = MagicMock(spec=AsyncIOScheduler)

    await client_handlers.confirm_cb(cb, callback_data, state, scheduler)

    cb.bot.send_message.assert_awaited_once()
    schedule_mock.assert_called_once()
    state.clear.assert_awaited_once()
    # B.13: 2 messages — (1) "✅ Вы записаны" + post_booking_keyboard,
    # (2) "👇 Кнопки внизу" + reply keyboard (via _restore_reply_keyboard_async).
    assert cb.message.answer.await_count == 2
    assert "Вы записаны" in str(cb.message.answer.await_args_list[0].args[0])
    assert "Кнопки внизу" in str(cb.message.answer.await_args_list[1].args[0])
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_service_picker_cb_no_slots_for_service_duration_rolls_back_to_date(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.51 (was 5.29 free-text test): service_picker_cb defensive —
    service tapped, but no slots available for service.duration_minutes
    (workday closed, all slots booked, or no workday) → roll back to
    selecting_date + 'На эту дату нет окна под услугу ...' + retry date
    picker. service_id/title/selected_date cleared (the next date fetch
    renders a fresh service picker).

    Replaces test_service_msg_no_slots_for_default_duration_renders_retry —
    free-text path removed, the no-slots check now lives in service_picker_cb
    with the REAL service duration (not SERVICE_DEFAULT_DURATION_MIN).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        svc = await _seed_service(
            session, ctx, name="Окрашивание", duration_minutes=120, is_active=True
        )
        await session.commit()
        service_id = svc.id
        # NO workday, NO slot for tomorrow — nothing bookable for 120 min.

    cb, callback_data = _make_service_callback(service_id)
    state = _make_state()
    target_date = (datetime.now(UTC) + timedelta(days=2)).date()
    await state.update_data(selected_date=target_date.isoformat(), is_slots_path=True)

    await client_handlers.service_picker_cb(cb, callback_data, state)

    # Rolled back to selecting_date, service_id/title/selected_date cleared.
    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.selecting_date
    calls = [str(c) for c in state.update_data.await_args_list]
    assert any("service_id=None" in c for c in calls), (
        f"stale service_id must be cleared, update_data calls: {calls}"
    )
    text = _answer_text(cb.message)
    assert "нет окна" in text or "Нет окна" in text
    assert "другую дату" in text
    assert isinstance(_answer_reply_markup(cb.message), InlineKeyboardMarkup)
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_confirm_cb_workday_path_slot_in_past_directs_to_slots(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Этап 5.8b W3: confirm_cb workday path — user waited >30 min before ✅,
    start_at is now in the past. create_booking raises SlotInPastError →
    handler catches, message directs to /slots (NOT /book — workday-path users
    must retry via /slots, legacy /book would not find workday slots).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        wd = await _seed_workday(session, ctx, work_date=target_date)
        workday_id = wd.id

    async def _raise_past(*args: Any, **kwargs: Any) -> Any:
        from bot.services.booking import SlotInPastError

        raise SlotInPastError(f"WorkDay {workday_id} start_at in past")

    monkeypatch.setattr(client_handlers, "create_booking", _raise_past)

    cb, callback_data = _make_confirm_callback()
    state = _make_state()
    await state.update_data(
        workday_id=str(workday_id),
        start_minute=600,
        client_name="Паша",
        service_title="Стрижка",
    )
    scheduler = MagicMock(spec=AsyncIOScheduler)

    await client_handlers.confirm_cb(cb, callback_data, state, scheduler)

    state.clear.assert_awaited_once()
    text = _answer_text(cb.message)
    assert "время уже прошло" in text
    # W3 fix: retry hint must be /slots for workday-path (NOT /book).
    assert "/slots" in text
    assert "/book" not in text, "workday-path must NOT direct user to /book"
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_confirm_cb_workday_path_booking_outside_workday_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Этап 5.8b P0 (critic iter 2): confirm_cb workday path — race: between
    service_msg summary and confirm_cb ✅, master closed the day via /closeday
    (is_active=False). create_booking raises BookingOutsideWorkDayError →
    handler catches, state.clear + 'День закрыт мастером. ... /slots'.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        wd = await _seed_workday(session, ctx, work_date=target_date)
        workday_id = wd.id

    async def _raise_outside(*args: Any, **kwargs: Any) -> Any:
        from bot.services.booking import BookingOutsideWorkDayError

        raise BookingOutsideWorkDayError(f"WorkDay {workday_id} closed")

    monkeypatch.setattr(client_handlers, "create_booking", _raise_outside)

    cb, callback_data = _make_confirm_callback()
    state = _make_state()
    await state.update_data(
        workday_id=str(workday_id),
        start_minute=600,  # 10:00 LOCAL
        client_name="Паша",
        service_title="Стрижка",
    )
    scheduler = MagicMock(spec=AsyncIOScheduler)

    await client_handlers.confirm_cb(cb, callback_data, state, scheduler)

    state.clear.assert_awaited_once()
    assert "День закрыт мастером" in _answer_text(cb.message)
    assert "/slots" in _answer_text(cb.message)
    cb.answer.assert_awaited()
    cb.bot.send_message.assert_not_awaited()  # no master notification on error


@pytest.mark.asyncio
async def test_confirm_cb_workday_path_capacity_exceeded_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Этап 5.8b P0 (critic iter 2): confirm_cb workday path — race: another
    booking grabbed the same 30-min window between service_msg and confirm_cb.
    create_booking raises WorkDayCapacityExceededError → handler catches,
    state.clear + 'Это время только что заняли. ... /slots'.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        wd = await _seed_workday(session, ctx, work_date=target_date, max_concurrent_clients=1)
        workday_id = wd.id

    async def _raise_capacity(*args: Any, **kwargs: Any) -> Any:
        from bot.services.booking import WorkDayCapacityExceededError

        raise WorkDayCapacityExceededError("WorkDay capacity exceeded")

    monkeypatch.setattr(client_handlers, "create_booking", _raise_capacity)

    cb, callback_data = _make_confirm_callback()
    state = _make_state()
    await state.update_data(
        workday_id=str(workday_id),
        start_minute=600,
        client_name="Паша",
        service_title="Стрижка",
    )
    scheduler = MagicMock(spec=AsyncIOScheduler)

    await client_handlers.confirm_cb(cb, callback_data, state, scheduler)

    state.clear.assert_awaited_once()
    assert "только что заняли" in _answer_text(cb.message)
    assert "/slots" in _answer_text(cb.message)
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_mybookings_keyboard_shows_transfer_for_workday_only_booking(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """B.1: mybookings_keyboard — booking with slot_id=None (workday-only) now
    shows [🔄 Перенести] button (transfer_booking workday-path implemented).

    Previously (Этап 5.8b Gap 5) the button was hidden because transfer_booking
    raised NotImplementedError for workday-only bookings. B.1 removed that guard.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=2)).date()

        # Workday-only booking (slot_id=None) — workday path post-006.
        booking_workday = Booking(
            slot_id=None,
            business_id=ctx["business_id"],
            master_id=ctx["master_id"],
            client_id=ctx["client_id"],
            service_id=None,
            service_title_snapshot="Стрижка",
            service_price_snapshot=None,
            client_name_snapshot="Паша",
            start_at=datetime.now(UTC) + timedelta(days=2, hours=2),
            end_at=datetime.now(UTC) + timedelta(days=2, hours=2, minutes=60),
            status="confirmed",
        )
        # Legacy slot-based booking (slot_id SET) — pre-006 path.
        slot = Slot(
            master_id=ctx["master_id"],
            slot_date=target_date,
            slot_hour=14,
            status="booked",
        )
        session.add(slot)
        session.add(booking_workday)
        await session.flush()
        booking_legacy = Booking(
            slot_id=slot.id,
            business_id=ctx["business_id"],
            master_id=ctx["master_id"],
            client_id=ctx["client_id"],
            service_id=None,
            service_title_snapshot="Стрижка",
            service_price_snapshot=None,
            client_name_snapshot="Паша",
            start_at=datetime.now(UTC) + timedelta(days=3, hours=2),
            end_at=datetime.now(UTC) + timedelta(days=3, hours=2, minutes=60),
            status="confirmed",
        )
        session.add(booking_legacy)
        await session.commit()

        booking_workday_id = booking_workday.id
        booking_legacy_id = booking_legacy.id

    async with session_factory() as session:
        wd_stmt = select(Booking).where(Booking.id == booking_workday_id)
        legacy_stmt = select(Booking).where(Booking.id == booking_legacy_id)
        booking_wd = (await session.execute(wd_stmt)).scalar_one()
        booking_lg = (await session.execute(legacy_stmt)).scalar_one()

        # Workday-only booking: transfer button NOW shown (B.1).
        kb_wd = mybookings_keyboard([booking_wd])
        rows_wd = kb_wd.inline_keyboard
        flat_texts_wd = [btn.text for row in rows_wd for btn in row]
        assert any("Перенести" in t for t in flat_texts_wd), (
            "workday-only booking (slot_id=None) must show transfer button (B.1)"
        )
        # Cancel still offered (cancel_booking supports slot_id=None).
        assert any("Отменить" in t for t in flat_texts_wd), (
            "cancel button must remain for workday-only booking"
        )

        # Legacy slot-based booking: slot_id SET → transfer shown.
        kb_lg = mybookings_keyboard([booking_lg])
        rows_lg = kb_lg.inline_keyboard
        flat_texts_lg = [btn.text for row in rows_lg for btn in row]
        assert any("Перенести" in t for t in flat_texts_lg), (
            "legacy slot-based booking must keep transfer button"
        )


# ============================================================
# Session 5.27 FEAT — service_picker_cb + service_custom_cb
# Coverage: client.py name_msg (services in DB → picker),
#           service_picker_cb (tap → confirming),
#           service_custom_cb (tap "Своя услуга" → text input),
#           confirm_cb with service_id in FSM (BookingCreate.service_id set).
# ============================================================


async def _seed_service(
    session: AsyncSession,
    ctx: dict[str, Any],
    *,
    name: str = "Стрижка",
    duration_minutes: int = 60,
    is_active: bool = True,
) -> Service:
    """Insert a Service row for the seeded business. Returns the Service."""
    svc = Service(
        business_id=ctx["business_id"],
        name=name,
        duration_minutes=duration_minutes,
        is_active=is_active,
    )
    session.add(svc)
    await session.commit()
    return svc


def _make_service_callback(
    service_id: UUID,
    *,
    user_id: int = 111222333,
) -> tuple[MagicMock, Any]:
    """Mock CallbackQuery for service_picker_cb (BookServiceCallbackData filter)."""
    from bot.keyboards.client import BookServiceCallbackData

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(user_id)
    cb.message = _make_message(user_id, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = BookServiceCallbackData(service_id=service_id)
    return cb, callback_data


def _make_string_callback(
    data: str,
    *,
    user_id: int = 111222333,
) -> MagicMock:
    """Mock CallbackQuery with raw callback_data string (for 'book_service_custom').

    service_custom_cb uses F.data == "book_service_custom" (NOT CallbackData
    factory), so we set cb.data directly and skip factory parsing.
    """
    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(user_id)
    cb.message = _make_message(user_id, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    cb.data = data
    return cb


@pytest.mark.asyncio
async def test_name_msg_happy_renders_summary_workday_path(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """name_msg (workday path) — happy: workday_id + start_minute +
    service_title in state → set_state(confirming) + summary +
    confirm_keyboard via _render_summary_and_set_confirming.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        wd = await _seed_workday(
            session,
            ctx,
            work_date=target_date,
            start_time=time(10, 0),
            end_time=time(12, 0),
        )
        workday_id = wd.id

    msg = _make_message(user_id=111222333, text="Паша")
    state = _make_state()
    await state.update_data(
        workday_id=str(workday_id),
        start_minute=600,  # 10:00 LOCAL
        service_title="Стрижка",
    )

    await client_handlers.name_msg(msg, state)

    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.confirming
    text = _answer_text(msg)
    assert "Подтвердите" in text


@pytest.mark.asyncio
async def test_render_summary_no_slot_no_workday_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.46 (B.10) — _render_summary_and_set_confirming defensive —
    neither slot_id nor workday_id in FSM (state corruption between
    entering_name and entering_phone — e.g. Redis flush mid-flow) →
    state.clear + 'Данные потеряны' + abort. Replaces the pre-B.10
    test_name_msg_no_slot_no_workday_clears_state (defensive moved from
    name_msg to _render_summary_and_set_confirming in B.10).
    """
    msg = _make_message(user_id=111222333, text="unused")
    state = _make_state()
    # Only service_title in state — no workday_id, no slot_id (corrupted).
    await state.update_data(service_title="Стрижка", client_name="Паша")

    ok = await client_handlers._render_summary_and_set_confirming(msg, state, "Паша")

    assert ok is False
    state.clear.assert_awaited_once()
    text = _answer_text(msg)
    assert "Данные потеряны" in text or "Начните заново" in text
    state.set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_name_msg_no_service_title_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.29 Task 2 — FSM reorder: name_msg defensive — service_title
    missing in FSM (state corruption: entering_name reached without going
    through service_picker_cb/service_msg) → state.clear + retry hint + abort.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        slot = Slot(
            master_id=ctx["master_id"],
            slot_date=target_date,
            slot_hour=14,
            status="open",
        )
        session.add(slot)
        await session.commit()
        slot_id = slot.id

    msg = _make_message(user_id=111222333, text="Паша")
    state = _make_state()
    # slot_id set but no service_title (corrupted state).
    await state.update_data(slot_id=str(slot_id))

    await client_handlers.name_msg(msg, state)

    state.clear.assert_awaited_once()
    text = _answer_text(msg)
    assert "Данные потеряны" in text or "Начните заново" in text
    state.set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_picker_cb_happy_saves_and_jumps_to_selecting_slot(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.29 Task 2 — FSM reorder: service_picker_cb happy — tap a
    service button → state.update_data(service_id, service_title) +
    set_state(selecting_slot) + render slot picker keyboard (was
    set_state(confirming) + summary render pre-5.29).

    Uses the /book legacy slot path (Slot row in DB, is_slots_path=False).
    New flow: date → service → slot → name → confirm. Pre-state has only
    selected_date (set by _process_selected_date when entering_service).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        svc = await _seed_service(session, ctx, name="Окрашивание", duration_minutes=120)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        slot = Slot(
            master_id=ctx["master_id"],
            slot_date=target_date,
            slot_hour=14,
            status="open",
        )
        session.add(slot)
        await session.commit()
        service_id = svc.id

    cb, callback_data = _make_service_callback(service_id)
    state = _make_state()
    await state.update_data(
        selected_date=target_date.isoformat(),
        is_slots_path=False,
    )

    await client_handlers.service_picker_cb(cb, callback_data, state)

    state.update_data.assert_awaited()
    saved_kwargs = state.update_data.call_args.kwargs
    assert saved_kwargs.get("service_id") == str(service_id)
    assert saved_kwargs.get("service_title") == "Окрашивание"
    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.selecting_slot

    text = _answer_text(cb.message)
    assert "Выберите время" in text
    assert isinstance(_answer_reply_markup(cb.message), InlineKeyboardMarkup)
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_service_picker_cb_workday_path_shows_slot_picker(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.29 Task 2 — FSM reorder: service_picker_cb on /slots workday
    path — pre-state has selected_date + is_slots_path=True → fetch slots via
    WorkDay, render slot_picker_keyboard_30min (was summary render pre-5.29).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        svc = await _seed_service(session, ctx, name="Окрашивание", duration_minutes=120)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        await _seed_workday(
            session,
            ctx,
            work_date=target_date,
            start_time=time(10, 0),
            end_time=time(12, 0),
        )
        service_id = svc.id

    cb, callback_data = _make_service_callback(service_id)
    state = _make_state()
    await state.update_data(
        selected_date=target_date.isoformat(),
        is_slots_path=True,
    )

    await client_handlers.service_picker_cb(cb, callback_data, state)

    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.selecting_slot
    saved_kwargs = state.update_data.call_args.kwargs
    assert saved_kwargs.get("service_id") == str(service_id)
    assert saved_kwargs.get("service_title") == "Окрашивание"
    text = _answer_text(cb.message)
    assert "Выберите время" in text
    assert isinstance(_answer_reply_markup(cb.message), InlineKeyboardMarkup)


@pytest.mark.asyncio
async def test_service_picker_cb_service_archived_rerenders_fresh_picker(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.51: service_picker_cb defensive — service archived/deleted
    between picker render and tap → re-render a FRESH service picker from DB
    (free-text fallback removed — unknown duration corrupts the slot grid).

    Race scenario: master archived the service while user was looking at the
    inline keyboard. service_picker_cb re-SELECTs Service by id, checks
    is_active — archived → 'Эта услуга больше недоступна' + fresh picker with
    the remaining active services. State stays entering_service, no
    update_data (no service saved on race fallback).

    Was test_service_picker_cb_service_archived_falls_back_to_text (5.27):
    free-text prompt 'Напишите услугу' → fresh picker (5.51).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        svc = await _seed_service(
            session, ctx, name="Окрашивание", duration_minutes=120, is_active=True
        )
        # Second service stays ACTIVE — must appear in the fresh picker.
        await _seed_service(session, ctx, name="Стрижка", duration_minutes=60, is_active=True)
        # Now archive the first (simulate race with master's /closeservice).
        svc.is_active = False
        await session.commit()
        service_id = svc.id
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()

    cb, callback_data = _make_service_callback(service_id)
    state = _make_state()
    await state.update_data(selected_date=target_date.isoformat())
    state.update_data.reset_mock()

    await client_handlers.service_picker_cb(cb, callback_data, state)

    # State stays in entering_service (NOT advanced to selecting_slot),
    # no service saved on race fallback.
    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()
    text = _answer_text(cb.message)
    assert "недоступна" in text
    # Fresh picker rendered with the remaining active service.
    markup = _answer_reply_markup(cb.message)
    assert isinstance(markup, InlineKeyboardMarkup)
    buttons = [btn.text for row in markup.inline_keyboard for btn in row]
    assert any("Стрижка" in b for b in buttons), (
        f"fresh picker must offer remaining active services, got: {buttons}"
    )
    assert not any(b == "Окрашивание" for b in buttons), (
        f"archived service must NOT be offered, got: {buttons}"
    )
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_service_picker_cb_service_deleted_rerenders_fresh_picker(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.51: service_picker_cb defensive — service deleted between
    picker render and tap (callback_data has stale service_id) → Service row
    not found → 'Эта услуга больше недоступна' + fresh picker with remaining
    active services. State stays entering_service, no update_data.

    Was test_service_picker_cb_service_deleted_falls_back_to_text (5.27):
    free-text prompt → fresh picker (5.51). Seed full stack + one ACTIVE
    service so the fresh picker has something to offer (no-DB seed would hit
    the master-not-found branch instead).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        await _seed_service(session, ctx, name="Стрижка", duration_minutes=60)
    target_date = (datetime.now(UTC) + timedelta(days=1)).date()

    cb, callback_data = _make_service_callback(UUID(int=42))  # not in DB
    state = _make_state()
    await state.update_data(selected_date=target_date.isoformat())
    state.update_data.reset_mock()

    await client_handlers.service_picker_cb(cb, callback_data, state)

    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()
    text = _answer_text(cb.message)
    assert "недоступна" in text
    markup = _answer_reply_markup(cb.message)
    assert isinstance(markup, InlineKeyboardMarkup)
    buttons = [btn.text for row in markup.inline_keyboard for btn in row]
    assert any("Стрижка" in b for b in buttons), (
        f"fresh picker must offer remaining active services, got: {buttons}"
    )
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_book_flow_service_before_slot(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session 5.29 Task 2 — NEW unit test: explicit order verification for
    the FSM reorder (услуга ДО слота). Calls simple_calendar_cb (date pick)
    → entering_service + service picker; then service_picker_cb (service
    tap) → selecting_slot + slot picker. Asserts the NEW ordering (pre-5.29
    flow was date → slot → name → service; this test pins the invariant).

    Integration-level E2E coverage is in test_integration_admin_flows.py
    (test_booking_flow_with_service_picker_creates_booking); this unit test
    is a focused invariant pin for the order of state transitions.
    """
    from aiogram_calendar.schemas import SimpleCalAct

    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        svc = await _seed_service(session, ctx, name="Окрашивание", duration_minutes=120)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        # Slot for legacy /book path (so service_picker_cb has slots to render).
        slot = Slot(
            master_id=ctx["master_id"],
            slot_date=target_date,
            slot_hour=14,
            status="open",
        )
        session.add(slot)
        await session.commit()
        service_id = svc.id

    # Step 1: simple_calendar_cb act=day → entering_service + service picker.
    target_dt = datetime.combine(target_date, time(12, 0))
    _patch_process_selection(monkeypatch, selected=True, selected_date=target_dt)
    cb_date = MagicMock(spec=CallbackQuery)
    cb_date.from_user = _make_user(111222333)
    cb_date.message = _make_message(111222333, text="<unused>")
    cb_date.answer = AsyncMock()
    cb_date.bot = AsyncMock()
    callback_data_date = _make_simple_calendar_callback(SimpleCalAct.day)
    state = _make_state()
    await client_handlers.simple_calendar_cb(cb_date, callback_data_date, state)

    assert state.set_state.call_args.args[0] == BookingStates.entering_service, (
        "5.29 Task 2: date pick → entering_service (was selecting_slot pre-5.29)"
    )
    text_after_date = _answer_text(cb_date.message)
    assert "Выберите услугу" in text_after_date, "service picker shown after date, not slot picker"

    # Step 2: service_picker_cb → selecting_slot + slot picker.
    cb_svc, callback_data_svc = _make_service_callback(service_id)
    # Carry selected_date from step 1 (real FSM would persist via update_data).
    state.set_state.reset_mock()
    await state.update_data(selected_date=target_date.isoformat(), is_slots_path=False)

    await client_handlers.service_picker_cb(cb_svc, callback_data_svc, state)

    assert state.set_state.call_args.args[0] == BookingStates.selecting_slot, (
        "5.29 Task 2: service tap → selecting_slot (was confirming pre-5.29)"
    )
    text_after_service = _answer_text(cb_svc.message)
    assert "Выберите время" in text_after_service, "slot picker shown after service, not summary"
    reply_markup = _answer_reply_markup(cb_svc.message)
    assert isinstance(reply_markup, InlineKeyboardMarkup)


@pytest.mark.asyncio
async def test_slots_filtered_by_service_duration(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.29 Task 2 — NEW handler-level test: slot picker rendered by
    service_picker_cb is filtered by service.duration_minutes via the
    overlap fix (slots.py:219). With a 120-min booking at 16:00-18:00 and
    service 'Окрашивание' (120 min), slot 15:30 must NOT appear in the
    keyboard (15:30 + 120 = 17:30 overlaps 16:00-18:00). Pre-fix: 15:30 + 30
    = 16:00 == 16:00 (half-open) → shown (BUG → fall at confirm).

    Unit-level overlap guard is in test_slots.py (test_overlap_uses_min_duration_not_30);
    this test pins the handler-level integration (service_picker_cb →
    _fetch_slot_picker_for_service → slot_picker_keyboard_30min).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        svc = await _seed_service(session, ctx, name="Окрашивание", duration_minutes=120)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        await _seed_workday(
            session,
            ctx,
            work_date=target_date,
            start_time=time(10, 0),
            end_time=time(18, 0),
        )
        # Existing booking 16:00-18:00 LOCAL (120 min) — blocks 15:30 slot
        # for 120-min services, but NOT for 60-min (separate test).
        booking_start_local = datetime.combine(target_date, time(16, 0))
        booking = await _seed_booking(session, ctx=ctx, start_at_local=booking_start_local)
        # _seed_booking creates 60-min booking by default — extend to 120 min
        # to model Окрашивание end_at (16:00-18:00 LOCAL).
        booking.end_at = booking.start_at + timedelta(minutes=120)
        await session.commit()
        service_id = svc.id

    cb, callback_data = _make_service_callback(service_id)
    state = _make_state()
    await state.update_data(
        selected_date=target_date.isoformat(),
        is_slots_path=True,  # workday path → slot_picker_keyboard_30min
    )

    await client_handlers.service_picker_cb(cb, callback_data, state)

    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.selecting_slot
    reply_markup = _answer_reply_markup(cb.message)
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    flat_texts = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "15:30" not in flat_texts, (
        "5.29 Task 2: with 120-min booking 16:00-18:00 + 120-min service, slot "
        "15:30 must NOT appear (15:30+120=17:30 overlaps 16:00-18:00). Pre-fix: "
        "15:30+30=16:00 half-open → shown (BUG)."
    )
    # Sanity: slots whose service-duration window stays clear of the booking
    # remain available. 14:00 + 120 = 16:00 (half-open) — no overlap → shown.
    assert "10:00" in flat_texts
    assert "13:30" in flat_texts
    assert "14:00" in flat_texts
    # 14:30 hidden (14:30+120=16:30 overlaps 16:00-18:00) — same root cause.
    assert "14:30" not in flat_texts


@pytest.mark.asyncio
async def test_slots_shown_for_short_service(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.29 Task 2 — NEW handler-level test: short service (60 min)
    shows slot 14:30 that long service (120 min) hides against the same
    booking (16:00-18:00). 14:30 + 60 = 15:30 < 16:00 → no overlap → shown.
    14:30 + 120 = 16:30 overlaps 16:00-18:00 → hidden.

    The user-visible win of the overlap fix: client picking Стрижка (60)
    sees 14:30 open, while client picking Окрашивание (120) sees it closed —
    reflecting real booking feasibility, not just grid occupancy.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        svc_short = await _seed_service(session, ctx, name="Стрижка", duration_minutes=60)
        svc_long = await _seed_service(session, ctx, name="Окрашивание", duration_minutes=120)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        await _seed_workday(
            session,
            ctx,
            work_date=target_date,
            start_time=time(10, 0),
            end_time=time(18, 0),
        )
        booking_start_local = datetime.combine(target_date, time(16, 0))
        booking = await _seed_booking(session, ctx=ctx, start_at_local=booking_start_local)
        booking.end_at = booking.start_at + timedelta(minutes=120)
        await session.commit()

    # Short service (60) — slot 14:30 available (14:30 + 60 = 15:30 < 16:00).
    cb_short, cd_short = _make_service_callback(svc_short.id)
    state_short = _make_state()
    await state_short.update_data(
        selected_date=target_date.isoformat(),
        is_slots_path=True,
    )
    await client_handlers.service_picker_cb(cb_short, cd_short, state_short)
    reply_markup_short = _answer_reply_markup(cb_short.message)
    flat_short = [btn.text for row in reply_markup_short.inline_keyboard for btn in row]
    assert "14:30" in flat_short, "Стрижка (60): 14:30 + 60 = 15:30 < 16:00 → available"

    # Long service (120) — slot 14:30 hidden (14:30 + 120 = 16:30 overlaps).
    cb_long, cd_long = _make_service_callback(svc_long.id)
    state_long = _make_state()
    await state_long.update_data(
        selected_date=target_date.isoformat(),
        is_slots_path=True,
    )
    await client_handlers.service_picker_cb(cb_long, cd_long, state_long)
    reply_markup_long = _answer_reply_markup(cb_long.message)
    flat_long = [btn.text for row in reply_markup_long.inline_keyboard for btn in row]
    assert "14:30" not in flat_long, (
        "Окрашивание (120): 14:30 + 120 = 16:30 overlaps 16:00-18:00 → hidden"
    )


@pytest.mark.asyncio
async def test_book_back_to_date_cb_returns_to_date_picker(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.30 S1: book_back_to_date_cb — tap '↩️ Назад' in service picker
    → state.set_state(selecting_date) + re-render date_picker_keyboard.
    Lets user change date without /cancel + /book restart.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        await _seed_workday(session, ctx, work_date=target_date)

    cb = _make_string_callback("book_back_to_date")
    state = _make_state()
    await state.update_data(is_slots_path=False, selected_date=target_date.isoformat())

    await client_handlers.book_back_to_date_cb(cb, state)

    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.selecting_date
    text = _answer_text(cb.message)
    assert "Выберите дату" in text
    # W2 (Session 5.30 S1 review): verify date picker keyboard contains the
    # seeded work_date as a BookDateCallbackData payload (not just text —
    # text label depends on weekday/today, callback_data is deterministic).
    reply_markup = _answer_reply_markup(cb.message)
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    flat_cbs = [btn.callback_data for row in reply_markup.inline_keyboard for btn in row]
    assert any(target_date.isoformat() in (cb or "") for cb in flat_cbs), (
        f"date picker keyboard must contain callback for {target_date.isoformat()}"
    )
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_book_back_to_date_cb_master_not_found_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.30 S1: book_back_to_date_cb, master not found (race with
    business config change) → state.clear + 'Мастер не найден' + callback.answer.
    """
    async with session_factory() as session:
        await session.execute(sa_text("DELETE FROM masters"))

    cb = _make_string_callback("book_back_to_date")
    state = _make_state()
    await state.update_data(is_slots_path=False)

    await client_handlers.book_back_to_date_cb(cb, state)

    state.clear.assert_awaited_once()
    text = _answer_text(cb.message)
    assert "Мастер не найден" in text
    state.set_state.assert_not_awaited()
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_book_back_to_service_cb_returns_to_service_picker(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.30 S1: book_back_to_service_cb — tap '↩️ Назад' in slot picker
    → state.set_state(entering_service) + re-render service_picker_keyboard.
    Lets user change service without /cancel + /book restart.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        await _seed_service(session, ctx, name="Стрижка", duration_minutes=60)

    cb = _make_string_callback("book_back_to_service")
    state = _make_state()
    await state.update_data(selected_date="2026-09-08")

    await client_handlers.book_back_to_service_cb(cb, state)

    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.entering_service
    text = _answer_text(cb.message)
    assert "Выберите услугу" in text
    # W2 (Session 5.30 S1 review): verify service picker keyboard contains
    # the seeded service "Стрижка" (catches rendering bugs that text-only
    # assertions miss — mirrors test_simple_calendar_cb_day_select_happy).
    reply_markup = _answer_reply_markup(cb.message)
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    flat_texts = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "Стрижка" in flat_texts, "service picker keyboard must contain seeded service"
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_book_back_to_service_cb_no_services_aborts_booking(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.51: book_back_to_service_cb, no services in DB →
    state.clear + 'Мастер пока не настроил услуги' (free-text fallback
    removed — booking impossible without a known duration).

    Was test_book_back_to_service_cb_no_services_shows_free_text_prompt
    (5.30 S1): 'Какая услуга?' prompt → abort (5.51).
    """
    async with session_factory() as session:
        await _seed_full_stack(session)  # business + master, no services

    cb = _make_string_callback("book_back_to_service")
    state = _make_state()
    await state.update_data(selected_date="2026-09-08")

    await client_handlers.book_back_to_service_cb(cb, state)

    state.clear.assert_awaited_once()
    state.set_state.assert_not_awaited()
    text = _answer_text(cb.message)
    assert "не настроил услуги" in text
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_book_back_to_service_cb_master_not_found_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.30 S1: book_back_to_service_cb, master not found →
    state.clear + 'Мастер не найден' + callback.answer.
    """
    async with session_factory() as session:
        await session.execute(sa_text("DELETE FROM masters"))

    cb = _make_string_callback("book_back_to_service")
    state = _make_state()

    await client_handlers.book_back_to_service_cb(cb, state)

    state.clear.assert_awaited_once()
    text = _answer_text(cb.message)
    assert "Мастер не найден" in text
    state.set_state.assert_not_awaited()
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_confirm_cb_workday_path_passes_service_id_to_booking_create(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5.27 FEAT: confirm_cb with service_id in FSM → BookingCreate.service_id
    is the UUID (not None) → _build_end_at uses service.duration_minutes.

    Verifies the FEAT end-to-end: service_picker_cb saves service_id in FSM,
    confirm_cb reads it and passes to create_booking via BookingCreate.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        svc = await _seed_service(session, ctx, name="Окрашивание", duration_minutes=120)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        wd = await _seed_workday(
            session,
            ctx,
            work_date=target_date,
            start_time=time(10, 0),
            end_time=time(12, 0),
        )
        service_id = svc.id
        workday_id = wd.id

    captured_payload: dict[str, Any] = {}
    fake_result = MagicMock()
    fake_result.booking_id = UUID("00000000-0000-0000-0000-000000000007")
    fake_result.start_at = datetime.now(UTC) + timedelta(days=1)
    fake_result.master_notification_text = "Новая запись"

    async def _fake_create(*args: Any, **kwargs: Any) -> Any:
        payload = args[1]
        captured_payload["service_id"] = payload.service_id
        captured_payload["workday_id"] = payload.workday_id
        return fake_result

    monkeypatch.setattr(client_handlers, "create_booking", _fake_create)
    monkeypatch.setattr(client_handlers, "schedule_for_booking", MagicMock())

    cb, callback_data = _make_confirm_callback()
    state = _make_state()
    await state.update_data(
        workday_id=str(workday_id),
        start_minute=600,
        client_name="Паша",
        service_title="Окрашивание",
        service_id=str(service_id),
    )
    scheduler = MagicMock(spec=AsyncIOScheduler)

    await client_handlers.confirm_cb(cb, callback_data, state, scheduler)

    assert captured_payload.get("service_id") == service_id, (
        "confirm_cb must pass service_id from FSM to BookingCreate"
    )
    state.clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirm_cb_slot_path_passes_service_id_to_booking_create(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5.27 FEAT: confirm_cb on legacy /book slot path with service_id in FSM →
    BookingCreate.service_id is the UUID.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        svc = await _seed_service(session, ctx, name="Стрижка", duration_minutes=30)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        slot = Slot(
            master_id=ctx["master_id"],
            slot_date=target_date,
            slot_hour=14,
            status="open",
        )
        session.add(slot)
        await session.commit()
        service_id = svc.id
        slot_id = slot.id

    captured_payload: dict[str, Any] = {}
    fake_result = MagicMock()
    fake_result.booking_id = UUID("00000000-0000-0000-0000-000000000008")
    fake_result.start_at = datetime.now(UTC) + timedelta(days=1)
    fake_result.master_notification_text = "Новая запись"

    async def _fake_create(*args: Any, **kwargs: Any) -> Any:
        payload = args[1]
        captured_payload["service_id"] = payload.service_id
        captured_payload["slot_id"] = payload.slot_id
        return fake_result

    monkeypatch.setattr(client_handlers, "create_booking", _fake_create)
    monkeypatch.setattr(client_handlers, "schedule_for_booking", MagicMock())

    cb, callback_data = _make_confirm_callback()
    state = _make_state()
    await state.update_data(
        slot_id=str(slot_id),
        client_name="Паша",
        service_title="Стрижка",
        service_id=str(service_id),
    )
    scheduler = MagicMock(spec=AsyncIOScheduler)

    await client_handlers.confirm_cb(cb, callback_data, state, scheduler)

    assert captured_payload.get("service_id") == service_id
    assert captured_payload.get("slot_id") == slot_id
    state.clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_picker_cb_tap_overwrites_stale_service_id_from_previous_flow(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 5.51 — F1 regression, rewritten for the free-text removal.

    Old F1 bug (5.27): stale service_id from an abandoned picker tap leaked
    into BookingCreate when the user later typed a custom service text —
    service_msg had to reset service_id=None. That path no longer exists:
    free-text is disabled, service_msg never touches state, and the ONLY way
    to advance from entering_service is tapping the picker — which always
    overwrites service_id atomically in a single update_data call.

    This test pins the new invariant: pre-state carries a STALE service_id X
    from an abandoned flow; tapping service Y must overwrite it with Y, so
    confirm_cb can never read the stale value.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        svc = await _seed_service(
            session, ctx, name="Окрашивание", duration_minutes=120, is_active=True
        )
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        slot = Slot(
            master_id=ctx["master_id"],
            slot_date=target_date,
            slot_hour=14,
            status="open",
        )
        session.add(slot)
        await session.commit()
        service_id = svc.id

    cb, callback_data = _make_service_callback(service_id)
    state = _make_state()
    stale_service_id = UUID("00000000-0000-0000-0000-000000000099")
    await state.update_data(
        selected_date=target_date.isoformat(),
        slot_id=str(slot.id),
        service_id=str(stale_service_id),  # STALE — must be overwritten
    )
    state.update_data.reset_mock()

    await client_handlers.service_picker_cb(cb, callback_data, state)

    # The tap overwrote stale service_id with the freshly-tapped service.
    saved_kwargs: dict[str, Any] = {}
    for call in state.update_data.await_args_list:
        saved_kwargs.update(call.kwargs)
    assert saved_kwargs.get("service_id") == str(service_id), (
        "picker tap must overwrite stale service_id from a previous abandoned "
        "flow — otherwise confirm_cb reads the stale UUID (F1 bug)"
    )
    assert saved_kwargs.get("service_title") == "Окрашивание"
    final_data = await state.get_data()
    assert final_data.get("service_id") == str(service_id)


# ============================================================
# Post-booking keyboard (Session 6 — Task 1, 2026-09-06)
# ============================================================


@pytest.mark.asyncio
async def test_book_confirm_success_bare_text_no_inline_keyboard(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session 5.51: post_booking_keyboard REMOVED from the success message.

    History: Session 6 — Task 1 added [📋 Мои записи] + [💇 Ещё запись]
    inline buttons after '✅ Вы записаны' because clients dead-ended. But the
    always-on reply keyboard (bottom of screen) already had [Записаться] /
    [Мои записи] — the inline copy duplicated them and users saw 'extra
    buttons that don't work' (user report 2026-09-11: after booking, inline
    [Мои записи][Ещё запись] under the success text + bottom buttons that
    'don't work by fact').

    New contract: (1) '✅ Вы записаны' is BARE text — no reply_markup at
    all; (2) the follow-up message restores the always-on reply keyboard
    (Session 5.36 B.13 — that part is unchanged).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        slot = Slot(
            master_id=ctx["master_id"],
            slot_date=target_date,
            slot_hour=14,
            status="open",
        )
        session.add(slot)
        await session.commit()
        slot_id = slot.id

    fake_result = MagicMock()
    fake_result.booking_id = UUID("00000000-0000-0000-0000-0000000000aa")
    fake_result.start_at = datetime.now(UTC) + timedelta(days=1)
    fake_result.master_notification_text = "Новая запись"

    async def _fake_create(*args: Any, **kwargs: Any) -> Any:
        return fake_result

    monkeypatch.setattr(client_handlers, "create_booking", _fake_create)
    monkeypatch.setattr(client_handlers, "schedule_for_booking", MagicMock())

    cb, callback_data = _make_confirm_callback()
    state = _make_state()
    await state.update_data(
        slot_id=str(slot_id),
        client_name="Паша",
        service_title="Стрижка",
    )
    scheduler = MagicMock(spec=AsyncIOScheduler)

    await client_handlers.confirm_cb(cb, callback_data, state, scheduler)

    # B.13: confirm_cb sends 2 messages — (1) '✅ Вы записаны' (bare text,
    # Session 5.51: no inline keyboard), (2) reply-keyboard restore.
    assert cb.message.answer.await_count == 2
    first_call = cb.message.answer.await_args_list[0]
    first_text = str(first_call.args[0]) if first_call.args else str(
        first_call.kwargs.get("text", "")
    )
    assert "Вы записаны" in first_text
    reply_markup = first_call.kwargs.get("reply_markup")
    assert reply_markup is None, (
        "Session 5.51: success message must be BARE text — the always-on "
        "reply keyboard covers [Записаться]/[Мои записи]; inline duplicates "
        "showed up as dead buttons (user report 2026-09-11)."
    )


@pytest.mark.asyncio
async def test_post_booking_mybookings_button_starts_mybookings_flow(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 6 — Task 1: tapping [📋 Мои записи] on the post-booking keyboard
    routes to client_mybookings_cb → _render_mybookings → renders the same
    list as /mybookings (single source of truth via shared helper).

    Seeds a cancelable booking (3 days ahead) so the rendered list contains
    "📋 Ваши записи:" plus the [❌ Отменить] inline button. Asserts the rendered
    text matches what /mybookings produces (no drift between command and
    inline-button paths).
    """
    from bot.keyboards.client import ClientMenuMyBookingsCallbackData

    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        future_local = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=3)
        future_local = future_local.replace(hour=14, minute=0, second=0, microsecond=0)
        booking = await _seed_booking(session, ctx=ctx, start_at_local=future_local)

    # Mock CallbackQuery for client_mybookings_cb (ClientMenuMyBookingsCallbackData)
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(user_id=111222333, text="<unused>")
    cb.answer = AsyncMock()
    callback_data = ClientMenuMyBookingsCallbackData()

    await client_handlers.client_mybookings_cb(cb, callback_data)

    # _render_mybookings answered with the bookings list on callback.message
    cb.message.answer.assert_awaited_once()
    text = _answer_text(cb.message)
    assert "📋 Ваши записи:" in text, (
        "client_mybookings_cb must render the same list header as /mybookings"
    )
    assert "Стрижка" in text  # service_title_snapshot from _seed_booking

    # Cancelable booking → mybookings_keyboard attached with [Отменить] button
    reply_markup = _answer_reply_markup(cb.message)
    assert isinstance(reply_markup, InlineKeyboardMarkup), (
        "cancelable booking must produce inline [Отменить] buttons (parity with /mybookings)"
    )
    buttons = [btn for row in reply_markup.inline_keyboard for btn in row]
    assert any("Отменить" in btn.text for btn in buttons)

    # callback.answer() called to clear the Telegram loading spinner
    cb.answer.assert_awaited_once()

    # Sanity: the booking shown is the one we seeded (booking_id round-trips
    # via MyBookingsCancelCallbackData on the [Отменить] button).
    cancel_btn = next(btn for btn in buttons if "Отменить" in btn.text)
    assert cancel_btn.callback_data is not None
    cancel_cb_data = MyBookingsCancelCallbackData.unpack(cancel_btn.callback_data)
    assert cancel_cb_data.booking_id == booking.id


@pytest.mark.asyncio
async def test_post_booking_again_button_starts_book_flow(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Session 6 — Task 1: tapping [💇 Ещё запись] on the post-booking keyboard
    routes to client_book_cb (existing ClientMenuBookCallbackData handler) →
    sets BookingStates.selecting_date + shows date picker.

    Confirms the post-booking keyboard reuses the SAME callback_data prefix
    as the /start menu [💇 Записаться] button — single handler, two entry
    points (start menu + post-booking). No new handler needed for [Ещё запись].
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        # Open a workday 3 days ahead so get_bookable_dates returns it (within
        # MAX_BOOKING_DAYS_AHEAD window, active, has free 30-min slots).
        work_date = (datetime.now(UTC) + timedelta(days=3)).date()
        await _seed_workday(
            session,
            ctx=ctx,
            work_date=work_date,
            start_time=time(10, 0),
            end_time=time(18, 0),
        )

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(user_id=111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = AsyncMock()
    state = _make_state()

    # client_book_cb signature is (callback, state) — no callback_data arg
    # (ClientMenuBookCallbackData carries no payload, aiogram doesn't inject it).
    await client_handlers.client_book_cb(cb, state)

    # Same effect as /book: entering selecting_date + date picker shown
    state.set_state.assert_awaited_once()
    set_state_args = state.set_state.call_args.args
    assert set_state_args[0] == BookingStates.selecting_date, (
        "client_book_cb (entered from [Ещё запись]) must set selecting_date state"
    )

    # is_slots_path=False (mirrors cmd_book / client_book_cb contract)
    update_data_kwargs = state.update_data.call_args.kwargs
    assert update_data_kwargs.get("is_slots_path") is False

    # Date picker shown (message.answer with reply_markup=InlineKeyboardMarkup)
    cb.message.answer.assert_awaited_once()
    text = _answer_text(cb.message)
    assert "Выберите дату" in text
    reply_markup = _answer_reply_markup(cb.message)
    assert isinstance(reply_markup, InlineKeyboardMarkup), (
        "date picker must be an inline keyboard (date_picker_keyboard)"
    )

    # callback.answer() called to clear the spinner
    cb.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_client_mybookings_cb_from_user_none_early_return() -> None:
    """Session 6 — Task 1 follow-up (code-review W2): callback.from_user is None
    (rare Telegram edge — channel-post callbacks, anonymous admins) →
    early return + callback.answer() without calling _render_mybookings.

    Mirrors the pattern of test_mybookings_transfer_cb_unknown_user (line 2787)
    and test_transfer_slot_cb_from_user_none_early_return (line 2842) — both
    guard the same `callback.from_user is None` branch in their respective
    handlers. Without this test, a future refactor could accidentally drop
    the callback.answer() call (Telegram spinner would hang forever).
    """
    from bot.keyboards.client import ClientMenuMyBookingsCallbackData

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = None  # edge case under test
    cb.message = _make_message(user_id=111222333, text="<unused>")
    cb.answer = AsyncMock()
    callback_data = ClientMenuMyBookingsCallbackData()

    await client_handlers.client_mybookings_cb(cb, callback_data)

    # Early return: callback.answer called once (clear spinner), message.answer
    # NEVER awaited (would crash on None user_id resolution inside _render_mybookings).
    cb.answer.assert_awaited_once()
    cb.message.answer.assert_not_awaited()


# ============================================================
# Session 5.36 (B.13) — Reply keyboard + pre-fill name tests
# ============================================================
#
# Coverage (NEXT_SESSION_PROMPT.md B.13 — 8 new tests, of which 2 are
# slot_cb/slot_30_cb fallback renames above; 7 net-new here):
# 1. test_slot_cb_pre_fill_yes_path — first_name='Андрей' → pre-fill branch
# 2. test_reply_book_msg_starts_booking_flow — [💇 Записаться] tap → /book flow
# 3. test_reply_book_msg_master_guard — master tap → early return
# 4. test_reply_mybookings_msg_renders_list — [📋 Мои записи] → _render_mybookings
# 5. test_name_pre_fill_yes_cb_happy_path — [✅ Да, это я] → confirming + summary
# 6. test_name_pre_fill_other_cb_transitions_to_text_input — [👤 Другое имя] → entering_name
# 7. test_cancel_msg_restores_reply_keyboard — /cancel → 2nd answer with ReplyKeyboardMarkup
#
# Helper notes (kept inline since each test only needs 1-2 mocks):
# - For pre-fill branch tests, override cb.from_user with a User that has a
#   non-empty first_name (slot_cb gates on `_client_first_name(callback)` truthy).
# - For reply_book_msg DB path tests, seed master + active WorkDay tomorrow
#   (mirrors test_cmd_book_sets_state_and_shows_date_picker:1426-1428).
# - For _restore_reply_keyboard_async assertions, check await_args_list —
#   confirm_cb / cancel_msg send the restore as a SEPARATE message AFTER the
#   primary answer, so last_call shadows it via _answer_text.


@pytest.mark.asyncio
async def test_slot_cb_pre_fill_yes_path(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """B.13: slot_cb with from_user.first_name='Андрей' (truthy) → pre-fill
    branch (entering_name_pre_fill, NOT entering_name).

    Verifies the 80% case (client books themselves, has a Telegram profile
    name). slot_cb sends 2 messages: (1) prompt 'Записать на <b>Андрей</b>?
    (ваше имя в Telegram)' with ReplyKeyboardRemove (hide reply keyboard),
    (2) 'Выберите:' with name_pre_fill_keyboard (inline 2 buttons).
    """
    from bot.keyboards.client import BookSlotCallbackData, NamePreFillYesCallbackData

    slot_id = UUID("12345678-1234-5678-1234-567812345678")
    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    # B.13: non-empty first_name triggers pre-fill branch.
    cb.from_user = User(id=111222333, is_bot=False, first_name="Андрей")
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = BookSlotCallbackData(slot_id=slot_id)

    state = _make_state()
    state.get_data = AsyncMock(return_value={"service_title": "Стрижка"})
    await client_handlers.slot_cb(cb, callback_data, state)

    state.update_data.assert_awaited_once()
    assert state.update_data.call_args.kwargs.get("slot_id") == str(slot_id)
    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.entering_name_pre_fill

    # 2 messages: (1) prompt with ReplyKeyboardRemove, (2) 'Выберите:' with inline.
    assert cb.message.answer.await_count == 2
    first_call = cb.message.answer.await_args_list[0]
    first_text = str(first_call.args[0]) if first_call.args else str(
        first_call.kwargs.get("text", "")
    )
    assert "Записать на" in first_text
    assert "Андрей" in first_text
    first_rm = first_call.kwargs.get("reply_markup")
    from aiogram.types import ReplyKeyboardRemove

    assert isinstance(first_rm, ReplyKeyboardRemove), (
        "1st message must hide reply keyboard (would obstruct inline pre-fill buttons)"
    )
    second_call = cb.message.answer.await_args_list[1]
    second_text = str(second_call.args[0]) if second_call.args else str(
        second_call.kwargs.get("text", "")
    )
    assert second_text == "Выберите:"
    second_rm = second_call.kwargs.get("reply_markup")
    from aiogram.types import InlineKeyboardMarkup

    assert isinstance(second_rm, InlineKeyboardMarkup), (
        "2nd message carries pre-fill inline keyboard"
    )
    flat = [btn for row in second_rm.inline_keyboard for btn in row]
    assert len(flat) == 2, f"pre-fill keyboard has 2 buttons, got {len(flat)}"
    # [✅ Да, это я] + [👤 Другое имя] — match the NamePreFill* callback data packs.
    assert flat[0].callback_data is not None, "yes button must carry callback_data"
    NamePreFillYesCallbackData.unpack(flat[0].callback_data)
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_reply_book_msg_starts_booking_flow(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """B.13: client taps [💇 Записаться] in reply keyboard → same flow as /book:
    selecting_date FSM + is_slots_path=False + date_picker_keyboard.

    Seeded: master + active WorkDay tomorrow → at least 1 bookable date.
    Mirrors test_cmd_book_sets_state_and_shows_date_picker:1413 but enters via
    reply keyboard handler (reply_book_msg), not /book command.
    """
    from bot.keyboards.client import CLIENT_REPLY_BOOK_LABEL

    tomorrow = (datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=1)).date()
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        await _seed_workday(session, ctx, work_date=tomorrow)

    msg = _make_message(user_id=111222333, text=CLIENT_REPLY_BOOK_LABEL)
    state = _make_state()

    await client_handlers.reply_book_msg(msg, state)

    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.selecting_date
    update_kwargs = state.update_data.call_args.kwargs
    assert update_kwargs.get("is_slots_path") is False

    msg.answer.assert_awaited_once()
    text = _answer_text(msg)
    assert "Выберите дату" in text
    reply_markup = _answer_reply_markup(msg)
    assert isinstance(reply_markup, InlineKeyboardMarkup), "date picker is inline keyboard"

@pytest.mark.asyncio
async def test_reply_book_msg_master_guard(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """B.13: master (ADMIN_ID) taps [💇 Записаться] reply keyboard button →
    early return (no FSM, no message answer).

    Defense-in-depth: cmd_start should NOT show the reply keyboard to master
    (they get admin_inline_menu), but a stale reply keyboard from a pre-B.13
    session could linger on master's device. The _is_master guard in
    reply_book_msg prevents master from accidentally entering the client
    booking flow.
    """
    from bot.config import get_settings
    from bot.keyboards.client import CLIENT_REPLY_BOOK_LABEL

    admin_id = get_settings().ADMIN_ID
    async with session_factory() as session:
        await _seed_full_stack(session)  # master + workday not strictly needed

    msg = _make_message(user_id=admin_id, text=CLIENT_REPLY_BOOK_LABEL)
    state = _make_state()

    await client_handlers.reply_book_msg(msg, state)

    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()
    msg.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_mybookings_msg_master_guard(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B.13: master (ADMIN_ID) taps [📋 Мои записи] reply keyboard button →
    early return (no _render_mybookings call). Mirrors reply_book_msg master
    guard — defense-in-depth against stale reply keyboards on master's device.

    Code-review S2: paired with test_reply_book_msg_master_guard so a future
    refactor that drops the _is_master guard from reply_mybookings_msg (but
    keeps it in reply_book_msg) would be caught by this test, not pass silently.
    """
    from bot.config import get_settings
    from bot.keyboards.client import CLIENT_REPLY_MYBOOKINGS_LABEL

    admin_id = get_settings().ADMIN_ID
    async with session_factory() as session:
        await _seed_full_stack(session)

    rendered_calls: list[Any] = []

    async def _spy_render(message: Any, user_id: int) -> None:
        rendered_calls.append((message, user_id))

    monkeypatch.setattr(client_handlers, "_render_mybookings", _spy_render)

    msg = _make_message(user_id=admin_id, text=CLIENT_REPLY_MYBOOKINGS_LABEL)

    await client_handlers.reply_mybookings_msg(msg)

    assert rendered_calls == [], "master must NOT trigger _render_mybookings"
    msg.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_mybookings_msg_renders_list(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B.13: client taps [📋 Мои записи] in reply keyboard → delegates to
    _render_mybookings (same as /mybookings command).

    Monkeypatches _render_mybookings to a spy to assert the handler routes
    correctly without coupling to the list-rendering internals (which are
    covered by test_mybookings_msg_*).
    """
    from bot.keyboards.client import CLIENT_REPLY_MYBOOKINGS_LABEL

    rendered_calls: list[tuple[Any, int]] = []

    async def _spy_render(message: Any, user_id: int) -> None:
        rendered_calls.append((message, user_id))

    monkeypatch.setattr(client_handlers, "_render_mybookings", _spy_render)

    client_tg = 111222333
    msg = _make_message(user_id=client_tg, text=CLIENT_REPLY_MYBOOKINGS_LABEL)

    await client_handlers.reply_mybookings_msg(msg)

    assert len(rendered_calls) == 1, "reply_mybookings_msg must delegate to _render_mybookings"
    assert rendered_calls[0][1] == client_tg, "user_id passed to _render_mybookings"
    assert rendered_calls[0][0] is msg, "message passed to _render_mybookings"


@pytest.mark.asyncio
async def test_name_pre_fill_yes_cb_happy_path(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """B.13 + B.10: client taps [✅ Да, это я] → confirm their Telegram
    first_name as booking name → state.client_name set + set_state(confirming) +
    summary + confirm_keyboard via _render_summary_and_set_confirming
    (phone step removed — name → confirm directly).

    Legacy slot path (slot_id set in state, no workday). DB lookup removed
    from name_pre_fill_yes_cb — only service_title defensive check
    remains (slot/workday resolved in _render_summary_and_set_confirming).
    """
    from bot.keyboards.client import NamePreFillYesCallbackData

    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        slot = Slot(
            master_id=ctx["master_id"],
            slot_date=target_date,
            slot_hour=14,
            status="open",
        )
        session.add(slot)
        await session.commit()
        slot_id = slot.id

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = User(id=111222333, is_bot=False, first_name="Паша")
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = AsyncMock()
    callback_data = NamePreFillYesCallbackData()

    state = _make_state()
    await state.update_data(
        slot_id=str(slot_id),
        service_title="Стрижка",
    )

    await client_handlers.name_pre_fill_yes_cb(cb, callback_data, state)

    # W3 (5.51): update_data called twice (client_name + summary_msg_id) —
    # merge kwargs across all calls instead of reading the last one.
    update_kwargs: dict[str, Any] = {}
    for call in state.update_data.await_args_list:
        update_kwargs.update(call.kwargs)
    assert update_kwargs.get("client_name") == "Паша"
    assert update_kwargs.get("summary_msg_id") is not None, (
        "W3: summary message id must be saved for cancel_msg keyboard strip"
    )
    state.set_state.assert_awaited()
    assert state.set_state.call_args.args[0] == BookingStates.confirming

    text = _answer_text(cb.message)
    assert "Подтвердите" in text
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_name_pre_fill_other_cb_transitions_to_text_input(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """B.13: client taps [👤 Другое имя] → state.set_state(entering_name) +
    'На чьё имя записываем?' prompt (no DB lookup, no state data update).

    The text-input handler (name_msg) takes over from entering_name — same
    as the pre-B.13 flow. This covers the 20% case (booking child/husband/etc).
    """
    from bot.keyboards.client import NamePreFillOtherCallbackData

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = User(id=111222333, is_bot=False, first_name="Андрей")
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = AsyncMock()
    callback_data = NamePreFillOtherCallbackData()

    state = _make_state()
    await state.set_state(BookingStates.entering_name_pre_fill)

    await client_handlers.name_pre_fill_other_cb(cb, callback_data, state)

    state.set_state.assert_awaited()
    assert state.set_state.call_args.args[0] == BookingStates.entering_name
    # No state data update — name_msg will collect client_name from text input.
    state.update_data.assert_not_awaited()
    text = _answer_text(cb.message)
    assert "На чьё имя записываем?" in text
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_name_pre_fill_yes_cb_race_first_name_becomes_empty(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """B.13 code-review S1: name_pre_fill_yes_cb defensive race fallback —
    from_user.first_name became empty/None between slot_cb (which gated on
    truthy first_name) and the [✅ Да, это я] tap. Handler falls back to
    entering_name (text input) instead of crashing or losing state.

    Race scenario: client opened slot_cb with first_name='Андрей' (truthy),
    then revoked their Telegram profile name before tapping [✅ Да, это я].
    Pydantic rejects first_name=None, so we test with first_name='' — same
    effect via _client_first_name (strip → '' which is falsy).
    """
    from bot.keyboards.client import NamePreFillYesCallbackData

    cb = MagicMock(spec=CallbackQuery)
    # Race: first_name became empty between slot_cb and this tap.
    cb.from_user = User(id=111222333, is_bot=False, first_name="")
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = AsyncMock()
    callback_data = NamePreFillYesCallbackData()

    state = _make_state()
    await state.set_state(BookingStates.entering_name_pre_fill)
    await state.update_data(service_title="Стрижка")

    await client_handlers.name_pre_fill_yes_cb(cb, callback_data, state)

    # Defensive fallback: transition to entering_name (text input).
    state.set_state.assert_awaited()
    assert state.set_state.call_args.args[0] == BookingStates.entering_name
    # client_name NOT saved (would have written "" — text input will collect it).
    state_update_kwargs = state.update_data.call_args.kwargs
    assert "client_name" not in state_update_kwargs
    text = _answer_text(cb.message)
    assert "На чьё имя записываем?" in text
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_cancel_msg_restores_reply_keyboard(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """B.13: /cancel inside FSM → state.clear() + hint + reply keyboard restore
    (via _restore_reply_keyboard_async).

    Verifies the 2-message contract for non-master users: (1) 'Ввод отменён'
    hint, (2) '👇 Кнопки внизу' with ReplyKeyboardMarkup. Master (ADMIN_ID)
    would skip the restore (admin_inline_menu is their UI, not reply keyboard).
    """
    from aiogram.types import ReplyKeyboardMarkup

    msg = _make_message(user_id=111222333, text="/cancel")
    state = _make_state()

    await client_handlers.cancel_msg(msg, state)

    state.clear.assert_awaited_once()
    assert msg.answer.await_count == 2
    # 1st message: hint
    first_call = msg.answer.await_args_list[0]
    first_text = str(first_call.args[0]) if first_call.args else str(
        first_call.kwargs.get("text", "")
    )
    assert "Ввод отменён" in first_text
    # 2nd message: reply keyboard restore
    second_call = msg.answer.await_args_list[1]
    second_text = str(second_call.args[0]) if second_call.args else str(
        second_call.kwargs.get("text", "")
    )
    assert "Кнопки внизу" in second_text
    second_rm = second_call.kwargs.get("reply_markup")
    assert isinstance(second_rm, ReplyKeyboardMarkup), (
        "2nd message must carry the client reply keyboard (B.13 restore contract)"
    )
    flat = [btn for row in second_rm.keyboard for btn in row]
    assert len(flat) == 2, "restored reply keyboard has the 2 client buttons"

