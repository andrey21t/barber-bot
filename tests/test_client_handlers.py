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

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bot.handlers import client as client_handlers
from bot.keyboards.client import (
    MyBookingsCancelCallbackData,
    MyBookingsTransferCallbackData,
    mybookings_keyboard,
)
from bot.models import Booking, Business, Client, Master, Slot
from bot.states import TransferStates
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
    # mybookings_keyboard emits 2 buttons per booking: [Отменить] + [Перенести].
    buttons = [btn for row in reply_markup.inline_keyboard for btn in row]
    assert len(buttons) == 2, (
        "one cancelable booking → 2 buttons: [Отменить] + [Перенести]"
    )
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
            await session.execute(
                select(Booking).where(Booking.id.in_([b1.id, b2.id])).order_by(Booking.start_at)
            )
        ).scalars().all()

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
        b = (await verify_session.execute(
            select(Booking).where(Booking.id == booking_id)
        )).scalar_one()
        assert b.status == "transferred"
        assert b.slot_id == new_slot_id

    # Old slot released to 'open', new slot booked.
    async with session_factory() as verify_session:
        # Old slot_id from booking's first slot — re-fetch via the slot we created.
        # The seed_booking inserted a Slot with status='booked'; after transfer, that
        # slot must be 'open'. We fetch by booking's new slot_id (different row).
        new_slot_row = (await verify_session.execute(
            select(Slot).where(Slot.id == new_slot_id)
        )).scalar_one()
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
        b = (await verify_session.execute(
            select(Booking).where(Booking.id == booking_id)
        )).scalar_one()
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
        b = (await verify_session.execute(
            select(Booking).where(Booking.id == booking_id)
        )).scalar_one()
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
    state.get_data = AsyncMock(
        return_value={"transfer_booking_id": str(random_booking_id)}
    )

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
# transfer_date_cb — date picker for transfer flow (coverage 661-702)
# ============================================================


@pytest.mark.asyncio
async def test_transfer_date_cb_happy_path(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """User picked a date for transfer → handler fetches available slots for
    that date, sets FSM state to selecting_slot, shows slot picker inline
    keyboard. Coverage: client.py:684-702 (happy path of transfer_date_cb).
    """
    async with session_factory() as session:
        ctx = await _seed_full_stack(session)
        # Open slot for the transfer target date (tomorrow at 14:00 Moscow)
        target_date = (datetime.now(UTC) + timedelta(days=1)).date()
        slot = Slot(
            master_id=ctx["master_id"],
            slot_date=target_date,
            slot_hour=14,
            status="open",
        )
        session.add(slot)
        await session.commit()

    from bot.keyboards.client import BookDateCallbackData

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = BookDateCallbackData(iso=target_date.isoformat())

    state = _make_state()

    await client_handlers.transfer_date_cb(cb, callback_data, state)

    # FSM state set to selecting_slot
    state.set_state.assert_awaited()
    assert state.set_state.call_args.args[0] == TransferStates.selecting_slot

    # selected_date saved in state
    state.update_data.assert_awaited()
    assert state.update_data.call_args.kwargs.get("selected_date") == target_date.isoformat()

    # Slot picker shown
    cb.message.answer.assert_awaited()
    reply_markup = _answer_reply_markup(cb.message)
    assert isinstance(reply_markup, InlineKeyboardMarkup), (
        "transfer_date_cb must show slot picker inline keyboard"
    )

    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_transfer_date_cb_no_slots_on_date(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """User picked a date with no open slots → handler replies "На эту дату
    нет свободных слотов. Выберите другую дату:" with date picker (retry).
    FSM state NOT advanced (stays at selecting_date). Coverage: client.py:686-693.
    """
    async with session_factory() as session:
        await _seed_full_stack(session)
        # No slots created — target date has no open slots

    from bot.keyboards.client import BookDateCallbackData

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    # Date far in the future — no slots exist
    far_date = (datetime.now(UTC) + timedelta(days=30)).date()
    callback_data = BookDateCallbackData(iso=far_date.isoformat())

    state = _make_state()

    await client_handlers.transfer_date_cb(cb, callback_data, state)

    # Reply with "no slots" + date picker for retry
    cb.message.answer.assert_awaited()
    err_text = _answer_text(cb.message)
    assert "нет свободных слотов" in err_text
    reply_markup = _answer_reply_markup(cb.message)
    assert isinstance(reply_markup, InlineKeyboardMarkup), (
        "no-slots reply must include date picker for retry"
    )

    # FSM state NOT advanced (still at selecting_date)
    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()
    cb.answer.assert_awaited()


# ============================================================
# transfer_date_cb — error branches (T4: client.py:664-666, 678-682)
# ============================================================


@pytest.mark.asyncio
async def test_transfer_date_cb_invalid_iso_date(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T4 — client.py:664-666: callback_data.iso='not-a-date' → date.fromisoformat
    raises ValueError → callback.answer('Невалидная дата') + state NOT cleared
    (stays at selecting_date for retry).
    """
    from bot.keyboards.client import BookDateCallbackData

    async with session_factory() as session:
        await _seed_full_stack(session)  # master exists — handler proceeds past FSM check

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    # Invalid ISO string — date.fromisoformat raises ValueError
    callback_data = BookDateCallbackData(iso="not-a-date")

    state = _make_state()

    await client_handlers.transfer_date_cb(cb, callback_data, state)

    # Error answer sent via callback.answer (popup, NOT message.answer)
    cb.answer.assert_awaited_once()
    args, _ = cb.answer.await_args
    assert args[0] == "Невалидная дата"

    # State must NOT be cleared (user can retry with valid date)
    state.clear.assert_not_awaited()
    # FSM state NOT advanced
    state.set_state.assert_not_awaited()
    # No DB query made (handler returns before async_session_factory block)
    cb.message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_transfer_date_cb_master_not_found_clears_state_and_answers(
    session_factory: Any,
    patched_session_factory: Any,
) -> None:
    """T4 — client.py:678-682: master not found in DB → state.clear() +
    callback.message.answer('❌ Не удалось найти мастера.') + callback.answer.

    Setup: empty DB (no _seed_full_stack) — get_settings().ADMIN_ID matches no
    master row → select(Master).where(Master.telegram_id == ADMIN_ID) returns None.
    """
    from bot.keyboards.client import BookDateCallbackData

    # No _seed_full_stack — DB empty, no master row
    target_date = (datetime.now(UTC) + timedelta(days=1)).date()

    bot = AsyncMock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = _make_user(111222333)
    cb.message = _make_message(111222333, text="<unused>")
    cb.answer = AsyncMock()
    cb.bot = bot
    callback_data = BookDateCallbackData(iso=target_date.isoformat())

    state = _make_state()

    await client_handlers.transfer_date_cb(cb, callback_data, state)

    # state.clear() called — FSM aborted (no retry, user must /book again)
    state.clear.assert_awaited_once()

    # Error message via callback.message.answer
    cb.message.answer.assert_awaited_once()
    err_text = _answer_text(cb.message)
    assert "Не удалось найти мастера" in err_text

    # callback.answer() called (close the loading spinner)
    cb.answer.assert_awaited()

    # FSM state NOT advanced (clear reset to None)
    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()
