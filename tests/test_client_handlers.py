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
from bot.models import Booking, Business, Client, Master, Slot, WorkDay
from bot.states import BookingStates, TransferStates
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
    """T5a: cmd_book (client.py:75-81) — /book sets FSM to selecting_date + shows
    date picker keyboard with 7-day window.
    """
    msg = _make_message(user_id=111222333, text="/book")
    state = _make_state()

    await client_handlers.cmd_book(msg, state)

    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.selecting_date

    msg.answer.assert_awaited_once()
    text = _answer_text(msg)
    assert "Выберите дату" in text
    reply_markup = _answer_reply_markup(msg)
    assert isinstance(reply_markup, InlineKeyboardMarkup), (
        "cmd_book must show date picker inline keyboard"
    )


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
async def test_simple_calendar_cb_day_select_happy_shows_slot_picker(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """simple_calendar_cb act=day, in-range, slots exist →
    state.update_data(selected_date) + set_state(selecting_slot) + slot picker.
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

    # Patch process_selection to simulate user clicked target_date (in-range)
    target_dt = datetime.combine(target_date, time(12, 0))
    _patch_process_selection(monkeypatch, selected=True, selected_date=target_dt)

    callback_data = _make_simple_calendar_callback(SimpleCalAct.day)
    state = _make_state()
    await client_handlers.simple_calendar_cb(cb, callback_data, state)

    state.update_data.assert_awaited()
    assert state.update_data.call_args.kwargs.get("selected_date") == target_date.isoformat()
    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.selecting_slot

    text = _answer_text(cb.message)
    assert "Выберите время" in text
    reply_markup = _answer_reply_markup(cb.message)
    assert isinstance(reply_markup, InlineKeyboardMarkup)
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
async def test_simple_calendar_cb_day_select_no_slots_shows_retry(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """simple_calendar_cb act=day, in-range, master exists, NO open slots →
    'На эту дату нет свободных слотов...' + calendar_keyboard retry.
    FSM NOT advanced.
    """

    async with session_factory() as session:
        await _seed_full_stack(session)  # master exists, but no slots seeded

    target_date = (datetime.now(UTC) + timedelta(days=30)).date()
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

    assert "нет свободных слотов" in _answer_text(cb.message)
    assert isinstance(_answer_reply_markup(cb.message), InlineKeyboardMarkup)
    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()
    cb.answer.assert_awaited()


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
    assert isinstance(_answer_reply_markup(cb.message), InlineKeyboardMarkup)
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
async def test_slot_cb_saves_slot_id_and_asks_for_name(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T5a: slot_cb (client.py:154-166) — user picked a slot →
    state.update_data(slot_id) + set_state(entering_name) + 'На чьё имя записываем?'
    """
    from bot.keyboards.client import BookSlotCallbackData

    slot_id = UUID("12345678-1234-5678-1234-567812345678")
    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = BookSlotCallbackData(slot_id=slot_id)

    state = _make_state()
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
async def test_name_msg_happy_saves_and_asks_for_service(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T5a: name_msg (client.py:183-185) — happy: name ok →
    state.update_data(client_name) + set_state(entering_service) + 'Какая услуга?'
    """
    msg = _make_message(user_id=111222333, text="Паша")
    msg.text = "Паша"
    state = _make_state()

    await client_handlers.name_msg(msg, state)

    state.update_data.assert_awaited_once()
    assert state.update_data.call_args.kwargs.get("client_name") == "Паша"
    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.entering_service

    text = _answer_text(msg)
    assert "Какая услуга" in text


@pytest.mark.asyncio
async def test_service_msg_empty_service_rejected(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T5a: service_msg (client.py:194-197) — empty service → 'Услуга не может быть
    пустой...' (retry, state NOT advanced).
    """
    msg = _make_message(user_id=111222333, text="   ")
    msg.text = "   "
    state = _make_state()

    await client_handlers.service_msg(msg, state)

    text = _answer_text(msg)
    assert "Услуга не может быть пустой" in text
    state.update_data.assert_not_awaited()
    state.set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_msg_too_long_service_rejected(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T5a: service_msg (client.py:198-200) — service > 255 chars → retry."""
    msg = _make_message(user_id=111222333, text="А" * 300)
    msg.text = "А" * 300
    state = _make_state()

    await client_handlers.service_msg(msg, state)

    text = _answer_text(msg)
    assert "слишком длинная" in text
    assert "255" in text
    state.update_data.assert_not_awaited()
    state.set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_msg_slot_id_missing_in_state_clears(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T5a: service_msg (client.py:208-211) — state has no 'slot_id' key (FSM
    corruption or stale state) → message.answer('❌ Ошибка: слот не выбран...')
    + state.clear (abort).
    """
    msg = _make_message(user_id=111222333, text="Стрижка")
    msg.text = "Стрижка"

    # State with client_name but NO slot_id (simulates corrupted FSM)
    state = _make_state()
    await state.update_data(client_name="Паша")  # populate state, no slot_id

    await client_handlers.service_msg(msg, state)

    text = _answer_text(msg)
    assert "слот не выбран" in text
    state.clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_msg_slot_not_found_clears(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T5a: service_msg (client.py:222-226) — slot_id in state but no Slot row
    in DB (slot was deleted between steps) → message.answer('Слот не найден...')
    + state.clear (abort).
    """
    async with session_factory() as session:
        await _seed_full_stack(session)  # master exists, no slot

    msg = _make_message(user_id=111222333, text="Стрижка")
    msg.text = "Стрижка"

    state = _make_state()
    fake_slot_id = UUID("00000000-0000-0000-0000-000000000001")
    await state.update_data(client_name="Паша", slot_id=str(fake_slot_id))

    await client_handlers.service_msg(msg, state)

    text = _answer_text(msg)
    assert "Слот не найден" in text
    state.clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_msg_happy_shows_confirmation(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T5a: service_msg (client.py:230-240) — happy: slot exists in DB →
    state.update_data(service_title) + set_state(confirming) + confirmation
    message with summary + confirm_keyboard.
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

    msg = _make_message(user_id=111222333, text="Стрижка")
    msg.text = "Стрижка"

    state = _make_state()
    await state.update_data(client_name="Паша", slot_id=str(slot_id))

    await client_handlers.service_msg(msg, state)

    state.update_data.assert_awaited()
    assert state.update_data.call_args.kwargs.get("service_title") == "Стрижка"
    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.confirming

    text = _answer_text(msg)
    assert "Подтвердите запись" in text
    reply_markup = _answer_reply_markup(msg)
    assert reply_markup is not None, "happy service_msg must show confirm_keyboard"


# ============================================================
# Booking flow — T5b: confirm_cb + cancel_msg
# Coverage: client.py:249-364, 370-379
# ============================================================


def _make_confirm_callback(
    *,
    user_id: int = 111222333,
) -> tuple[MagicMock, Any]:
    """Mock CallbackQuery for confirm_cb (BookConfirmCallbackData filter)."""
    from bot.keyboards.client import BookConfirmCallbackData

    bot = AsyncMock()
    bot.send_message = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(user_id)
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
    text = _answer_text(cb.message)
    assert "Вы записаны" in text
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_cancel_msg_clears_state_and_answers(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T5b: cancel_msg (client.py:370-379) — /cancel inside FSM → state.clear()
    BEFORE answer (race) + 'Ввод отменён. /book чтобы начать заново'.
    """
    msg = _make_message(user_id=111222333, text="/cancel")
    state = _make_state()

    await client_handlers.cancel_msg(msg, state)

    state.clear.assert_awaited_once()
    text = _answer_text(msg)
    assert "Ввод отменён" in text
    assert "/book" in text


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
    """Covers keyboards/client.py:102-103 — `if not slots: button("Нет свободных
    слотов", callback_data="noop"); return markup`.

    Empty slots list → single disabled-style button with noop callback (no
    slot to select). Verifies the edge case branch (vs the for-loop default
    path that builds per-slot buttons).
    """
    from bot.keyboards.client import slot_picker_keyboard

    markup = slot_picker_keyboard([])

    assert isinstance(markup, InlineKeyboardMarkup)
    # Single button "Нет свободных слотов" with callback_data="noop"
    buttons = markup.inline_keyboard
    assert len(buttons) == 1
    assert len(buttons[0]) == 1
    button = buttons[0][0]
    assert button.text == "Нет свободных слотов"
    assert button.callback_data == "noop"


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
    callback_data = BookSlot30CallbackData(
        workday_id=workday_id, start_minute=start_minute
    )
    return cb, callback_data


@pytest.mark.asyncio
async def test_cmd_slots_sets_state_and_shows_date_picker(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Этап 5.8b: cmd_slots — /slots sets FSM selecting_date + is_slots_path=True.

    Same calendar picker as /book; the slots-branch flag is read by
    _handle_simple_calendar to dispatch to WorkDay lookup + 30-min slot picker.
    """
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
async def test_simple_calendar_cb_slots_path_shows_30min_slot_picker(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Этап 5.8b: /slots path — calendar day-select with active WorkDay →
    get_available_slots_30 returns TimeSlot30 list → slot_picker_keyboard_30min
    with BookSlot30CallbackData callback_data (NOT legacy slot picker).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        await _seed_workday(
            session, ctx, work_date=target_date, start_time=time(10, 0), end_time=time(12, 0)
        )

    target_dt = datetime.combine(target_date, time(11, 0))
    _patch_process_selection(monkeypatch, selected=True, selected_date=target_dt)

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = _make_simple_calendar_callback(SimpleCalAct.day)

    state = _make_state()
    # cmd_slots sets is_slots_path=True before calendar is opened
    await state.update_data(is_slots_path=True)
    await client_handlers.simple_calendar_cb(cb, callback_data, state)

    state.update_data.assert_awaited()
    assert state.update_data.call_args.kwargs.get("selected_date") == target_date.isoformat()
    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.selecting_slot

    text = _answer_text(cb.message)
    assert "Выберите время" in text
    reply_markup = _answer_reply_markup(cb.message)
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    # 30-min picker has BookSlot30CallbackData on buttons (prefix book_slot_30),
    # NOT legacy BookSlotCallbackData (prefix book_slot).
    rows = reply_markup.inline_keyboard
    assert rows, "30-min picker must have buttons"
    first_cb = rows[0][0].callback_data
    assert first_cb is not None, "callback_data must be set on slot buttons"
    assert first_cb.startswith("book_slot_30:"), (
        f"workday-path callback must use BookSlot30CallbackData, got {first_cb!r}"
    )


@pytest.mark.asyncio
async def test_simple_calendar_cb_slots_path_no_workday_shows_hint(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Этап 5.8b: /slots path, no WorkDay for date → 'Мастер не работает в этот
    день. /book для записи по часам.' (NOT generic 'нет свободных слотов').
    FSM stays in selecting_date (no slot picker shown).
    """
    async with session_factory() as session:
        await _seed_full_stack(session)  # master exists, no WorkDay seeded

    target_date = (datetime.now(UTC) + timedelta(days=30)).date()
    target_dt = datetime.combine(target_date, time(12, 0))
    _patch_process_selection(monkeypatch, selected=True, selected_date=target_dt)

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = AsyncMock()
    callback_data = _make_simple_calendar_callback(SimpleCalAct.day)

    state = _make_state()
    await state.update_data(is_slots_path=True)
    await client_handlers.simple_calendar_cb(cb, callback_data, state)

    text = _answer_text(cb.message)
    assert "не работает в этот день" in text
    state.set_state.assert_not_awaited()
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_simple_calendar_cb_slots_path_inactive_workday_shows_hint(
    session_factory: Any,
    patched_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Этап 5.8b: /slots path, WorkDay exists but is_active=False (master
    closed the day via /closeday) → 'День закрыт мастером.' + calendar retry.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=2)).date()
        await _seed_workday(
            session, ctx, work_date=target_date, is_active=False
        )

    target_dt = datetime.combine(target_date, time(12, 0))
    _patch_process_selection(monkeypatch, selected=True, selected_date=target_dt)

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = AsyncMock()
    callback_data = _make_simple_calendar_callback(SimpleCalAct.day)

    state = _make_state()
    await state.update_data(is_slots_path=True)
    await client_handlers.simple_calendar_cb(cb, callback_data, state)

    text = _answer_text(cb.message)
    assert "День закрыт мастером" in text
    state.set_state.assert_not_awaited()
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_slot_30_cb_saves_workday_id_start_minute() -> None:
    """Этап 5.8b: slot_30_cb — valid BookSlot30CallbackData → state.update_data
    (workday_id, start_minute) + set_state(entering_name) + ask name message.
    """
    workday_id = uuid4()
    cb, callback_data = _make_slot_30_callback(
        workday_id=workday_id, start_minute=630  # 10:30
    )

    state = _make_state()
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
    cb, callback_data = _make_slot_30_callback(
        workday_id=workday_id, start_minute=1500
    )

    state = _make_state()
    await client_handlers.slot_30_cb(cb, callback_data, state)

    state.clear.assert_awaited_once()
    assert "Ошибка выбора времени" in _answer_text(cb.message)
    state.set_state.assert_not_awaited()
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
    assert "Вы записаны" in _answer_text(cb.message)
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_service_msg_workday_path_workday_not_found_clears_state(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Этап 5.8b W1: service_msg workday-path — workday_id in state but WorkDay
    row deleted (race: master deleted day between service_msg call and user
    typed service) → state.clear + 'Рабочий день не найден. ... /slots'.
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        wd = await _seed_workday(session, ctx, work_date=target_date)
        workday_id = wd.id
        # Now delete the WorkDay (simulating race with /closeday cascade or manual
        # cleanup). Service_msg's SELECT will return None.
        from sqlalchemy import delete as sa_delete

        await session.execute(sa_delete(WorkDay).where(WorkDay.id == workday_id))
        await session.commit()

    msg = _make_message(user_id=111222333, text="Стрижка")
    state = _make_state()
    await state.update_data(
        workday_id=str(workday_id),
        start_minute=600,
        client_name="Паша",
    )

    await client_handlers.service_msg(msg, state)

    state.clear.assert_awaited_once()
    assert "Рабочий день не найден" in _answer_text(msg)
    assert "/slots" in _answer_text(msg)


@pytest.mark.asyncio
async def test_service_msg_workday_path_happy_shows_summary(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Этап 5.8b W1: service_msg workday-path happy — workday_id + start_minute
    in state → fetch WorkDay → _build_start_at_from_workday → summary via
    _format_booking_summary_from_start_at → 'Подтвердите запись' + confirm_kb.
    Verifies the summary contains LOCAL-formatted date/time + client_name +
    service_title (no Slot entity involved).
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

    msg = _make_message(user_id=111222333, text="Стрижка")
    state = _make_state()
    await state.update_data(
        workday_id=str(workday_id),
        start_minute=600,  # 10:00 LOCAL
        client_name="Паша",
    )

    await client_handlers.service_msg(msg, state)

    state.set_state.assert_awaited_once()
    assert state.set_state.call_args.args[0] == BookingStates.confirming
    text = _answer_text(msg)
    assert "Подтвердите запись" in text
    # Summary rendered via _format_booking_summary_from_start_at — uses LOCAL
    # strftime "%d %B %Y, %H:%M" → "10:00" appears in summary.
    assert "10:00" in text
    assert "Паша" in text
    assert "Стрижка" in text
    assert isinstance(_answer_reply_markup(msg), InlineKeyboardMarkup)


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
async def test_mybookings_keyboard_hides_transfer_for_workday_only_booking(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """Этап 5.8b Gap 5: mybookings_keyboard — booking with slot_id=None
    (workday-only, post-migration 006) must NOT show [🔄 Перенести] button.
    Transfer flow expects slot_id (legacy /book path) and would fail with
    AttributeError on a workday-only booking. Keyboard hides the button instead.

    Booking with slot_id SET (legacy /book path) keeps [🔄 Перенести] as before.
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

        # Re-fetch with fresh session to detach from identity map.
        booking_workday_id = booking_workday.id
        booking_legacy_id = booking_legacy.id

    async with session_factory() as session:
        wd_stmt = select(Booking).where(Booking.id == booking_workday_id)
        legacy_stmt = select(Booking).where(Booking.id == booking_legacy_id)
        booking_wd = (await session.execute(wd_stmt)).scalar_one()
        booking_lg = (await session.execute(legacy_stmt)).scalar_one()

        # Workday-only booking: slot_id is None → transfer hidden.
        kb_wd = mybookings_keyboard([booking_wd])
        rows_wd = kb_wd.inline_keyboard
        flat_texts_wd = [btn.text for row in rows_wd for btn in row]
        assert not any("Перенести" in t for t in flat_texts_wd), (
            "workday-only booking (slot_id=None) must hide transfer button"
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
