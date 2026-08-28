"""Booking flow handlers — Pure I/O layer (Telegram API + db session).

Contract (deep-analysis-protocol Pass 3):
- NO business logic in handlers — validation, html.escape, timezone conversion live in services
- 8 handlers + 1 fallback (no_state_fallback for State(None) after bot restart):
  1. cmd_book: /book, StateFilter(None) → selecting_date
  2. simple_calendar_cb: callback simple_calendar, StateFilter(selecting_date) → selecting_slot
     (aiogram_calendar month navigation, callback.answer contract verified against lib source)
  3. slot_cb: callback book_slot:<uuid>, StateFilter(selecting_slot) → entering_name
  4. name_msg: text, StateFilter(entering_name) → entering_service
  5. service_msg: text, StateFilter(entering_service) → confirming
  6. confirm_cb: callback book_confirm, StateFilter(confirming) → State(None) + create_booking
  7. cancel_msg: /cancel, StateFilter("*") → state.clear() + message
  + mybookings_msg / mybookings_cancel_cb / mybookings_transfer_cb / transfer_simple_calendar_cb
  + transfer_slot_cb + no_state_fallback: State(None), F.text, ~F.text.startswith("/")
    → "Начните через /book"

Invariants (spec.md + MY-VIBE-RULES.md):
- state.clear() BEFORE event.answer (race condition)
- /cancel handler registered BEFORE /mybookings in router order (spec 491)
- create_booking exceptions caught: SlotAlreadyBookedError, SlotInPastError, SlotClosedError
- Bot restart mid-FSM → MemoryStorage loses state → no_state_fallback catches
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Message
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback
from aiogram_calendar.schemas import SimpleCalAct
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from scheduler import schedule_for_booking

from bot.config import Settings, get_settings
from bot.db import async_session_factory
from bot.keyboards.client import (
    BookConfirmCallbackData,
    BookSlotCallbackData,
    MyBookingsCancelCallbackData,
    MyBookingsTransferCallbackData,
    calendar_keyboard,
    confirm_keyboard,
    mybookings_keyboard,
    slot_picker_keyboard,
)
from bot.models import Booking, Slot
from bot.schemas import BookingCreate
from bot.services.booking import (
    BookingAlreadyCancelledError,
    BookingAlreadyTransferredError,
    BookingNotFoundError,
    CancelResult,
    CancelTooLateError,
    SlotAlreadyBookedError,
    SlotClosedError,
    SlotInPastError,
    SlotNotAvailableError,
    TransferResult,
    cancel_booking,
    create_booking,
    transfer_booking,
)
from bot.services.slots import get_available_slots
from bot.states import BookingStates, TransferStates

logger = logging.getLogger(__name__)

router = Router(name="client")


def _calendar_range(settings: Settings) -> tuple[datetime, datetime]:
    """Compute (min_date, max_date) for SimpleCalendar — NAIVE local in business TZ.

    aiogram_calendar's process_day_select (common.py:56) builds
    `datetime(year, month, day)` — naive, AT MIDNIGHT. If min_date has a time
    component (e.g. 13:45), then `min_date > date` for today → user clicking
    "Сегодня" gets alert "date have to be later <today>". So we strip time
    too: min_date = today @ 00:00:00, max_date = (today + N days) @ 00:00:00.

    Also strip tzinfo — aiogram_calendar compares with naive datetime
    (TypeError comparing aware vs naive).

    Returns (today_naive_local_midnight, today + MAX_BOOKING_DAYS_AHEAD naive local midnight).
    """
    tz = ZoneInfo(settings.TIMEZONE)
    today_local = datetime.now(tz).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    max_local = today_local + timedelta(days=settings.MAX_BOOKING_DAYS_AHEAD)
    return today_local, max_local


# ============================================================
# 1. cmd_book — entry point (/book)
# ============================================================
@router.message(Command("book"), StateFilter(None))
async def cmd_book(message: Message, state: FSMContext) -> None:
    """Show SimpleCalendar (month navigation) for date selection."""
    settings = get_settings()
    await state.set_state(BookingStates.selecting_date)
    await message.answer(
        "📅 Выберите дату записи:",
        reply_markup=await calendar_keyboard(*_calendar_range(settings)),
    )


# ============================================================
# 2. simple_calendar_cb — user navigated/picked date (aiogram_calendar)
# ============================================================
# callback.answer() contract (verified from aiogram_calendar 0.6.0 source):
#   - act=ignore: lib calls query.answer(cache_time=60) → handler skips lib,
#     answers explicitly
#   - act=today + same-month: lib calls query.answer(cache_time=60) → handler
#     skips lib, answers explicitly
#   - act=today + diff-month: lib calls edit_reply_markup (no answer) → handler answers
#   - act=prev_y/next_y/prev_m/next_m: lib calls edit_reply_markup (no answer) → handler answers
#   - act=cancel: lib calls delete_reply_markup (no answer) → handler answers + state.clear()
#   - act=day + out-of-range: lib calls query.answer(alert) → handler returns (selected=False)
#   - act=day + in-range: lib calls delete_reply_markup (no answer) → handler answers + fetch slots
async def _handle_simple_calendar(
    callback: CallbackQuery,
    callback_data: SimpleCalendarCallback,
    state: FSMContext,
    selecting_slot_state: State,
    is_transfer: bool,
) -> None:
    """Shared logic for booking + transfer SimpleCalendar handlers.

    Branches on callback_data.act to call callback.answer() only when lib has
    not already answered (F1 fix). For act=day: fetches slots, transitions FSM
    to selecting_slot. For act=cancel: clears FSM state.
    """
    settings = get_settings()

    # For act=ignore and act=today+same-month: lib calls query.answer(cache_time=60)
    # (simple_calendar.py:147, 177). Handler does NOT call lib for these branches
    # (early return before cal.process_selection) — so handler must answer explicitly
    # to avoid Telegram spinner (~10s timeout). cache_time=60 matches lib's contract.
    if callback_data.act == SimpleCalAct.ignore:
        await callback.answer(cache_time=60)
        return
    if callback_data.act == SimpleCalAct.today:
        # lib uses system-local datetime.now() for same-month check (simple_calendar.py:173);
        # handler matches to avoid TZ-mismatch double-answer at month boundary (N1 fix).
        today_sys = datetime.now().replace(tzinfo=None)
        if today_sys.year == callback_data.year and today_sys.month == callback_data.month:
            await callback.answer(cache_time=60)
            return  # same-month: lib would answer cache_time=60, handler does it instead

    cal = SimpleCalendar(locale="ru_RU.UTF-8", cancel_btn="Отмена", today_btn="Сегодня")
    cal.set_dates_range(*_calendar_range(settings))
    selected, selected_date = await cal.process_selection(callback, callback_data)

    if callback_data.act == SimpleCalAct.day:
        if not selected:
            return  # F7 fix: out-of-range, lib answered alert, do nothing
        slot_date = selected_date.date()
        async with async_session_factory() as session:
            from sqlalchemy import select

            from bot.models import Master

            stmt = select(Master).where(Master.telegram_id == settings.ADMIN_ID).limit(1)
            master = (await session.execute(stmt)).scalar_one_or_none()
            if master is None:
                await state.clear()
                if callback.message is not None:
                    await callback.message.answer(
                        "❌ Не удалось найти мастера. Обратитесь к администратору."
                    )
                await callback.answer()
                return
            slots = await get_available_slots(session, master.id, slot_date)
        if not slots:
            if callback.message is not None:
                await callback.message.answer(
                    "На эту дату нет свободных слотов. Выберите другую дату:",
                    reply_markup=await calendar_keyboard(*_calendar_range(settings)),
                )
            await callback.answer()
            return
        await state.update_data(selected_date=slot_date.isoformat())
        await state.set_state(selecting_slot_state)
        if callback.message is not None:
            await callback.message.answer(
                "Выберите новое время:" if is_transfer else "Выберите время:",
                reply_markup=slot_picker_keyboard(slots),
            )
        await callback.answer()
        return

    if callback_data.act == SimpleCalAct.cancel:
        # state.clear() BEFORE callback.answer (race condition, MY-VIBE-RULES.md:23)
        await state.clear()
        if callback.message is not None:
            await callback.message.answer(
                "Перенос отменён. /mybookings чтобы начать заново"
                if is_transfer
                else "Ввод отменён. /book чтобы начать заново"
            )
        await callback.answer()
        return

    # Navigation actions (prev_y/next_y/prev_m/next_m/today-diff-month):
    # lib did edit_reply_markup, no answer from lib → handler answers (F1 fix)
    await callback.answer()


@router.callback_query(SimpleCalendarCallback.filter(), StateFilter(BookingStates.selecting_date))
async def simple_calendar_cb(
    callback: CallbackQuery,
    callback_data: SimpleCalendarCallback,
    state: FSMContext,
) -> None:
    """SimpleCalendar navigation + day select for booking flow (cmd_book)."""
    await _handle_simple_calendar(
        callback=callback,
        callback_data=callback_data,
        state=state,
        selecting_slot_state=BookingStates.selecting_slot,
        is_transfer=False,
    )


# ============================================================
# 3. slot_cb — user picked a slot → ask for name
# ============================================================
@router.callback_query(BookSlotCallbackData.filter(), StateFilter(BookingStates.selecting_slot))
async def slot_cb(
    callback: CallbackQuery,
    callback_data: BookSlotCallbackData,
    state: FSMContext,
) -> None:
    """User selected a slot — save slot_id, ask for client name."""
    await state.update_data(slot_id=str(callback_data.slot_id))
    await state.set_state(BookingStates.entering_name)
    if callback.message is not None:
        await callback.message.answer("На чьё имя записываем? (например: Паша, я сам, сын 5 лет)")
    await callback.answer()


# ============================================================
# 4. name_msg — user typed name → ask for service
# ============================================================
@router.message(StateFilter(BookingStates.entering_name))
async def name_msg(message: Message, state: FSMContext) -> None:
    """User typed client name — save, ask for service."""
    name = message.text.strip() if message.text else ""
    if not name:
        await message.answer("Имя не может быть пустым. Введите имя:")
        return
    if len(name) > 255:
        await message.answer("Имя слишком длинное (макс. 255 символов). Введите короче:")
        return

    await state.update_data(client_name=name)
    await state.set_state(BookingStates.entering_service)
    await message.answer("Какая услуга? (например: стрижка, окрашивание+стрижка)")


# ============================================================
# 5. service_msg — user typed service → show confirmation
# ============================================================
@router.message(StateFilter(BookingStates.entering_service))
async def service_msg(message: Message, state: FSMContext) -> None:
    """User typed service — save, show confirmation with summary."""
    service = message.text.strip() if message.text else ""
    if not service:
        await message.answer("Услуга не может быть пустой. Введите услугу:")
        return
    if len(service) > 255:
        await message.answer("Услуга слишком длинная (макс. 255 символов). Введите короче:")
        return

    await state.update_data(service_title=service)
    await state.set_state(BookingStates.confirming)

    # Render summary
    data = await state.get_data()
    slot_id_str = data.get("slot_id")
    if not slot_id_str:
        await message.answer("❌ Ошибка: слот не выбран. Начните заново через /book")
        await state.clear()
        return

    settings = get_settings()
    async with async_session_factory() as session:
        from sqlalchemy import select

        # Slot.id is Uuid column — convert str to UUID to avoid AttributeError
        # on SQLite (Uuid.bind_processor calls value.hex, str.hex doesn't exist)
        # and TypeError on Postgres.
        stmt = select(Slot).where(Slot.id == UUID(slot_id_str))
        result = await session.execute(stmt)
        slot = result.scalar_one_or_none()
        if slot is None:
            await message.answer("❌ Слот не найден. Начните заново через /book")
            await state.clear()
            return

        from bot.keyboards.client import _format_booking_summary

        summary = _format_booking_summary(
            slot=slot,
            client_name=data["client_name"],
            service_title=service,
            business_timezone=settings.TIMEZONE,
        )

    await message.answer(
        f"Подтвердите запись:\n\n{summary}",
        reply_markup=confirm_keyboard(),
    )


# ============================================================
# 6. confirm_cb — user tapped ✅ → create_booking
# ============================================================
@router.callback_query(BookConfirmCallbackData.filter(), StateFilter(BookingStates.confirming))
async def confirm_cb(
    callback: CallbackQuery,
    callback_data: BookConfirmCallbackData,
    state: FSMContext,
    scheduler: AsyncIOScheduler,
) -> None:
    """User confirmed — call create_booking service, handle exceptions.

    `scheduler` injected from dp["scheduler"] workflow_data (set in bot.main).
    """
    data = await state.get_data()
    slot_id_str = data.get("slot_id")
    client_name = data.get("client_name")
    service_title = data.get("service_title")

    if not all([slot_id_str, client_name, service_title]):
        # state.clear() BEFORE answer (race condition, MY-VIBE-RULES.md 24)
        await state.clear()
        if callback.message is not None:
            await callback.message.answer("❌ Данные потеряны. Начните заново через /book")
        await callback.answer()
        return

    settings = get_settings()
    async with async_session_factory() as session:
        from sqlalchemy import select

        from bot.models import Business, Master

        # Single-master MVP: master from ADMIN_ID
        stmt_m = select(Master).where(Master.telegram_id == settings.ADMIN_ID).limit(1)
        master = (await session.execute(stmt_m)).scalar_one_or_none()
        if master is None:
            await state.clear()
            if callback.message is not None:
                await callback.message.answer("❌ Мастер не найден")
            await callback.answer()
            return

        stmt_b = select(Business).where(Business.id == master.business_id).limit(1)
        business = (await session.execute(stmt_b)).scalar_one_or_none()
        if business is None:
            await state.clear()
            if callback.message is not None:
                await callback.message.answer("❌ Бизнес не найден")
            await callback.answer()
            return

        from uuid import UUID

        # Type narrowing: slot_id_str, client_name, service_title are guaranteed by earlier check
        assert slot_id_str is not None
        assert client_name is not None
        assert service_title is not None

        # Note: client_id intentionally NOT in BookingCreate — service resolves
        # client by telegram_id via _select_or_create_client(telegram_id).
        payload = BookingCreate(
            slot_id=UUID(slot_id_str),
            client_name=client_name,
            service_title=service_title,
            service_id=None,
        )

        try:
            result = await create_booking(
                session,
                payload,
                business_id=business.id,
                master_id=master.id,
                telegram_id=callback.from_user.id,
            )
        except SlotAlreadyBookedError:
            # state.clear() BEFORE answer (race condition)
            await state.clear()
            if callback.message is not None:
                await callback.message.answer(
                    "😔 Слот только что заняли. Начните заново через /book"
                )
            await callback.answer()
            return
        except SlotInPastError:
            await state.clear()
            if callback.message is not None:
                await callback.message.answer(
                    "❌ Это время уже прошло. Выберите другое через /book"
                )
            await callback.answer()
            return
        except SlotClosedError:
            await state.clear()
            if callback.message is not None:
                await callback.message.answer(
                    "❌ Слот закрыт мастером. Выберите другой через /book"
                )
            await callback.answer()
            return

        # Send master notification (Pure/IO — service prepared text, handler sends)
        if callback.bot is not None:
            await callback.bot.send_message(
                chat_id=settings.ADMIN_ID,
                text=result.master_notification_text,
            )

        # Schedule reminders — uses global scheduler from dp["scheduler"]
        # (injected as kwarg; do NOT create per-request build_scheduler())
        schedule_for_booking(scheduler, result.booking_id, result.start_at)

    # state.clear() BEFORE answer (race condition, MY-VIBE-RULES.md 24)
    await state.clear()
    if callback.message is not None:
        await callback.message.answer("✅ Вы записаны. Напомню за 24ч и за 1ч.")
    await callback.answer()


# ============================================================
# 7. cancel_msg — /cancel inside FSM (StateFilter("*"))
# ============================================================
@router.message(Command("cancel"), StateFilter("*"))
async def cancel_msg(message: Message, state: FSMContext) -> None:
    """Cancel booking flow — clears FSM state (works in any state).

    Registered BEFORE /mybookings handler (spec.md 491) — /cancel from /mybookings
    will be added in Урок 2.5 with StateFilter(None).
    """
    # state.clear() BEFORE answer (race condition, MY-VIBE-RULES.md 24)
    await state.clear()
    await message.answer("Ввод отменён. /book чтобы начать заново")


# ============================================================
# 8. mybookings_msg — /mybookings (StateFilter(None)) — list client bookings
# ============================================================
@router.message(Command("mybookings"), StateFilter(None))
async def mybookings_msg(message: Message) -> None:
    """List confirmed/transferred upcoming bookings for the current user.

    Spec.md 41: `/mybookings` → отмена (>24ч) или перенос (>24ч).
    This handler renders the list AND shows inline [Отменить] buttons for
    bookings that are still within the cancellation window (start_at - 24h > now).
    Cancellation itself is performed by mybookings_cancel_cb (next handler).

    Resolution: client by telegram_id (booking.py pattern, _select_or_create_client).
    Filter: upcoming (start_at > now UTC), status IN (confirmed, transferred).
    """
    from zoneinfo import ZoneInfo

    from sqlalchemy import select

    from bot.config import get_settings
    from bot.models import Business, Client
    from bot.services.admin import get_client_bookings

    if message.from_user is None:
        return
    user_id = message.from_user.id

    settings = get_settings()
    async with async_session_factory() as session:
        # Resolve client by telegram_id (auth-register pattern from booking.py:101)
        stmt_c = select(Client).where(Client.telegram_id == user_id)
        client = (await session.execute(stmt_c)).scalar_one_or_none()
        if client is None:
            await message.answer("У вас пока нет записей. /book чтобы записаться")
            return

        bookings = await get_client_bookings(session, client.id)

        if not bookings:
            await message.answer("У вас нет активных записей. /book чтобы записаться")
            return

        # Resolve business.timezone for rendering local time (single-master MVP —
        # all bookings belong to the same business; we take tz from the first booking).
        stmt_b = select(Business).where(Business.id == bookings[0].business_id).limit(1)
        business = (await session.execute(stmt_b)).scalar_one_or_none()
        tz_name = business.timezone if business is not None else settings.TIMEZONE
        tz = ZoneInfo(tz_name)

    # Partition bookings: cancelable (start_at - CANCEL_MIN_HOURS > now) vs too-late.
    # Cross-DB aware-aware comparison: b.start_at naive on SQLite / aware UTC on
    # Postgres (TIMESTAMPTZ + asyncpg). Inject tzinfo=UTC on DB-read so naive becomes
    # aware (no-op on Postgres). now_utc is aware UTC.
    now_utc = datetime.now(UTC)
    cancelable: list[Booking] = []
    lines = ["📋 Ваши записи:", ""]
    for b in bookings:
        # b.start_at: naive on SQLite, aware UTC on Postgres. Inject tzinfo=UTC
        # (no-op on Postgres) before .astimezone — otherwise Python interprets naive
        # as system-local TZ (Mac default Europe/Moscow → wrong render; Render UTC
        # correct by accident). Same pattern as booking.py:380 cancel_booking.
        local_time = b.start_at.replace(tzinfo=UTC).astimezone(tz)
        when = local_time.strftime("%d %b %Y, %H:%M")
        # Snapshots already escaped, strip newlines for list safety (consistent with
        # admin._render_bookings — html.escape(quote=False) skips \n).
        name = b.client_name_snapshot.replace("\n", " ")
        service = b.service_title_snapshot.replace("\n", " ")
        lines.append(f"• {when}\n  💇 {service}\n  👤 {name}")

        # Aware-aware comparison: b.start_at.replace(tzinfo=UTC) - timedelta(...)
        # yields aware UTC deadline; now_utc is aware UTC. Both SQLite (after strip
        # injection) and Postgres compare correctly.
        deadline = b.start_at.replace(tzinfo=UTC) - timedelta(hours=settings.CANCEL_MIN_HOURS)
        if now_utc < deadline:
            cancelable.append(b)
        else:
            lines.append("  ⏰ Отмена недоступна (менее 24ч до записи)")

    lines.append("")
    if cancelable:
        lines.append("Чтобы отменить — нажмите кнопку под этим сообщением.")
        await message.answer(
            "\n".join(lines),
            reply_markup=mybookings_keyboard(cancelable, business_timezone=tz_name),
        )
    else:
        lines.append("Отменить запись нельзя — все записи менее чем через 24ч.")
        await message.answer("\n".join(lines))


# ============================================================
# 9. mybookings_cancel_cb — inline [Отменить] button callback
# ============================================================
@router.callback_query(MyBookingsCancelCallbackData.filter(), StateFilter(None))
async def mybookings_cancel_cb(
    callback: CallbackQuery,
    callback_data: MyBookingsCancelCallbackData,
    scheduler: AsyncIOScheduler,
) -> None:
    """User tapped [Отменить <date>] in /mybookings list — cancel that booking.

    Spec.md 41, 317, 405-407:
      - Resolve client by telegram_id (defensive: booking_id in callback could
        belong to another user; cancel_booking enforces ownership via
        `WHERE client_id=?`).
      - cancel_booking raises:
          BookingNotFoundError       → "Запись не найдена"
          BookingAlreadyCancelledError → "Запись уже отменена"
          CancelTooLateError         → "❌ Отмена возможна только за 24+ часов до записи"
      - On success: send master notification + "✅ Запись отменена" to client.

    `scheduler` injected from dp["scheduler"] workflow_data (set in bot.main),
    same as confirm_cb (line 246). Service calls remove_jobs_for_booking internally.
    """
    from sqlalchemy import select

    from bot.models import Client

    if callback.from_user is None:
        await callback.answer()
        return

    settings = get_settings()
    async with async_session_factory() as session:
        # Resolve client by telegram_id (defense-in-depth: cancel_booking ALSO
        # checks ownership, but we need client.id to pass to it).
        stmt_c = select(Client).where(Client.telegram_id == callback.from_user.id)
        client = (await session.execute(stmt_c)).scalar_one_or_none()
        if client is None:
            await callback.answer("У вас нет записей")
            return

        try:
            result: CancelResult = await cancel_booking(
                session,
                callback_data.booking_id,
                client.id,
                scheduler,
            )
        except BookingNotFoundError:
            await callback.answer("Запись не найдена")
            return
        except BookingAlreadyCancelledError:
            await callback.answer("Запись уже отменена")
            return
        except CancelTooLateError:
            if callback.message is not None:
                await callback.message.answer("❌ Отмена возможна только за 24+ часов до записи")
            await callback.answer()
            return

        # Send master notification (Pure/IO — service prepared text, handler sends).
        # Single-master MVP: master.telegram_id == settings.ADMIN_ID (verified
        # line 99: Master.telegram_id == settings.ADMIN_ID lookup).
        if callback.bot is not None:
            await callback.bot.send_message(
                chat_id=settings.ADMIN_ID,
                text=result.master_notification_text,
            )

    if callback.message is not None:
        await callback.message.answer("✅ Запись отменена. Мастер уведомлён.")
    await callback.answer()


# ============================================================
# 10. no_state_fallback — bot restart mid-FSM, state lost
# ============================================================
@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def no_state_fallback(message: Message) -> None:
    """Catch text when no FSM state active (bot restart mid-FSM, MemoryStorage lost state)."""
    await message.answer("Начните запись через /book")


# ============================================================
# 11. mybookings_transfer_cb — [🔄 Перенести] entry (StateFilter(None))
# ============================================================
@router.callback_query(MyBookingsTransferCallbackData.filter(), StateFilter(None))
async def mybookings_transfer_cb(
    callback: CallbackQuery,
    callback_data: MyBookingsTransferCallbackData,
    state: FSMContext,
) -> None:
    """User tapped [🔄 Перенести <date>] in /mybookings — start transfer FSM.

    Validates the booking is still cancelable (start_at - 24h > now). Transfer
    shares the same 24h window as cancel (spec.md 41 — "отмена (>24ч) или перенос
    (>24ч)"), so we re-use the partition logic from mybookings_msg.

    Saves booking_id in FSM data so subsequent simple_calendar_cb (transfer) /
    slot_cb (transfer) can resolve it (FSM data survives between handler
    invocations — MemoryStorage in dev, RedisStorage in prod).

    state.set_state(TransferStates.selecting_date) so the SimpleCalendar picker
    dispatches to transfer_simple_calendar_cb (not simple_calendar_cb which is
    StateFilter(BookingStates.selecting_date)).
    """
    from sqlalchemy import select

    from bot.models import Client

    if callback.from_user is None:
        await callback.answer()
        return

    settings = get_settings()
    async with async_session_factory() as session:
        # Resolve client by telegram_id (same as mybookings_cancel_cb).
        stmt_c = select(Client).where(Client.telegram_id == callback.from_user.id)
        client = (await session.execute(stmt_c)).scalar_one_or_none()
        if client is None:
            await callback.answer("У вас нет записей")
            return

        # Re-fetch booking to validate ownership + cancel-window (defensive — the
        # booking_id in callback could be stale; user might have cancelled via web
        # or admin between /mybookings render and this tap).
        stmt_b = select(Booking).where(
            Booking.id == callback_data.booking_id,
            Booking.client_id == client.id,
        )
        booking = (await session.execute(stmt_b)).scalar_one_or_none()
        if booking is None:
            await callback.answer("Запись не найдена")
            return
        if booking.status == "cancelled":
            await callback.answer("Запись уже отменена")
            return

        # 24h rule — same partition as mybookings_msg (aware-aware comparison).
        # We re-check here (NOT in service) to give a clear error before entering FSM
        # (transfer_booking also raises CancelTooLateError, but we want a popup now,
        # not after the user has picked a date+slot).
        now_utc = datetime.now(UTC)
        deadline = booking.start_at.replace(tzinfo=UTC) - timedelta(hours=settings.CANCEL_MIN_HOURS)
        if now_utc >= deadline:
            if callback.message is not None:
                await callback.message.answer("❌ Перенос возможен только за 24+ часов до записи")
            await callback.answer()
            return

    # Save booking_id in FSM (state.set_state AFTER save — order is safe because
    # state.update_data doesn't trigger handlers, set_state does).
    await state.update_data(transfer_booking_id=str(callback_data.booking_id))
    await state.set_state(TransferStates.selecting_date)
    if callback.message is not None:
        await callback.message.answer(
            "📅 Выберите новую дату для переноса:",
            reply_markup=await calendar_keyboard(*_calendar_range(settings)),
        )
    await callback.answer()


# ============================================================
# 12. transfer_simple_calendar_cb — user navigated/picked date (aiogram_calendar)
# ============================================================
# Same callback.answer contract as simple_calendar_cb (booking flow) but dispatched
# on TransferStates.selecting_date (distinct from BookingStates.selecting_date).
# aiogram dispatches by state filter, so the two handlers coexist without conflict.
@router.callback_query(SimpleCalendarCallback.filter(), StateFilter(TransferStates.selecting_date))
async def transfer_simple_calendar_cb(
    callback: CallbackQuery,
    callback_data: SimpleCalendarCallback,
    state: FSMContext,
) -> None:
    """SimpleCalendar navigation + day select for transfer flow (mybookings_transfer_cb)."""
    await _handle_simple_calendar(
        callback=callback,
        callback_data=callback_data,
        state=state,
        selecting_slot_state=TransferStates.selecting_slot,
        is_transfer=True,
    )


# ============================================================
# 13. transfer_slot_cb — user picked a slot → call transfer_booking
# ============================================================
@router.callback_query(BookSlotCallbackData.filter(), StateFilter(TransferStates.selecting_slot))
async def transfer_slot_cb(
    callback: CallbackQuery,
    callback_data: BookSlotCallbackData,
    state: FSMContext,
    scheduler: AsyncIOScheduler,
) -> None:
    """User selected a new slot — call transfer_booking service, handle all 7 errors.

    Error mapping (spec.md 41, 318, 408-409 + service contracts):
      BookingNotFoundError            → "Запись не найдена"
      BookingAlreadyCancelledError     → "Запись уже отменена"
      CancelTooLateError               → "❌ Перенос возможен только за 24+ часов"
      BookingAlreadyTransferredError   → "❌ Запись уже перенесена (конкурентный запрос)"
      SlotAlreadyBookedError            → "😔 Слот только что заняли, выберите другой"
      SlotInPastError                  → "❌ Это время уже прошло"
      SlotClosedError                  → "❌ Слот закрыт мастером"
      SlotNotAvailableError            → "❌ Слот недоступен" (defensive — _select_open_slot
                                         raises SlotClosedError or SlotAlreadyBookedError
                                         for known cases; SlotNotAvailableError covers
                                         unexpected states like slot not found at all)

    `scheduler` injected from dp["scheduler"] workflow_data (same as confirm_cb).
    state.clear() BEFORE service call (race condition, MY-VIBE-RULES.md 24 — same as
    confirm_cb:355 and mybookings_cancel_cb does NOT clear because it has no FSM state).
    """
    from sqlalchemy import select

    from bot.models import Client

    if callback.from_user is None:
        await callback.answer()
        return

    data = await state.get_data()
    booking_id_str = data.get("transfer_booking_id")
    if not booking_id_str:
        # state.clear() BEFORE answer (race condition)
        await state.clear()
        if callback.message is not None:
            await callback.message.answer("❌ Данные потеряны. /mybookings чтобы начать")
        await callback.answer()
        return

    settings = get_settings()
    async with async_session_factory() as session:
        # Resolve client by telegram_id (same as mybookings_cancel_cb:491-507).
        stmt_c = select(Client).where(Client.telegram_id == callback.from_user.id)
        client = (await session.execute(stmt_c)).scalar_one_or_none()
        if client is None:
            # No FSM clear here — user has no records at all; state stays for retry.
            await callback.answer("У вас нет записей")
            return

        # state.clear() BEFORE service call (race condition, MY-VIBE-RULES.md 24).
        # transfer_booking is idempotent on race (start_at pin), but if user taps
        # twice in quick succession, the second tap should NOT reuse stale state.
        await state.clear()
        try:
            result: TransferResult = await transfer_booking(
                session,
                UUID(booking_id_str),
                callback_data.slot_id,
                client.id,
                scheduler,
            )
        except BookingNotFoundError:
            await callback.answer("Запись не найдена")
            return
        except BookingAlreadyCancelledError:
            await callback.answer("Запись уже отменена")
            return
        except CancelTooLateError:
            if callback.message is not None:
                await callback.message.answer("❌ Перенос возможен только за 24+ часов до записи")
            await callback.answer()
            return
        except BookingAlreadyTransferredError:
            if callback.message is not None:
                await callback.message.answer(
                    "❌ Запись уже перенесена (конкурентный запрос). "
                    "/mybookings чтобы увидеть актуальный список"
                )
            await callback.answer()
            return
        except SlotAlreadyBookedError:
            if callback.message is not None:
                await callback.message.answer(
                    "😔 Слот только что заняли. /mybookings чтобы выбрать другой"
                )
            await callback.answer()
            return
        except SlotInPastError:
            if callback.message is not None:
                await callback.message.answer("❌ Это время уже прошло.")
            await callback.answer()
            return
        except SlotClosedError:
            if callback.message is not None:
                await callback.message.answer("❌ Слот закрыт мастером.")
            await callback.answer()
            return
        except SlotNotAvailableError:
            if callback.message is not None:
                await callback.message.answer("❌ Слот недоступен.")
            await callback.answer()
            return

        # Send master notification (Pure/IO — service prepared text, handler sends).
        # Single-master MVP: master.telegram_id == settings.ADMIN_ID.
        if callback.bot is not None:
            await callback.bot.send_message(
                chat_id=settings.ADMIN_ID,
                text=result.master_notification_text,
            )

    if callback.message is not None:
        # Render client confirmation using result.new_start_at (UTC) → LOCAL.
        # new_start_at is aware UTC (transfer_booking returns aware); convert to
        # business tz for display (same pattern as cancel_booking:380).
        from zoneinfo import ZoneInfo

        new_local = result.new_start_at.astimezone(ZoneInfo(settings.TIMEZONE))
        when = new_local.strftime("%d %b %Y, %H:%M")
        await callback.message.answer(f"✅ Запись перенесена на {when}. Мастер уведомлён.")
    await callback.answer()


# ============================================================
# 14. no_state_callback_fallback — inline button tap with no FSM state (L1)
# ============================================================
# MUST be registered LAST in client_router — catches only callbacks not
# matched by more specific handlers above (mybookings_cancel_cb at line 548,
# mybookings_transfer_cb at line 649). Both have StateFilter(None) + specific
# callback_data filter and win by specificity (registered earlier = matched
# first by aiogram router dispatch).
@router.callback_query(StateFilter(None))
async def no_state_callback_fallback(callback: CallbackQuery) -> None:
    """Catch inline button tap when no FSM state active (L1, spec.md Session 4).

    Scenario: user tapped an inline button (e.g. calendar, slot picker from
    old message) after bot restart or session timeout cleared FSM state.
    Without this handler aiogram logs "callback query not answered" and the
    button silently fails. Reply with popup telling user to start fresh.

    Note: mybookings_cancel_cb and mybookings_transfer_cb are registered
    EARLIER (lines 548, 649) with StateFilter(None) + specific callback_data
    filter — they win by specificity. This fallback only catches unmatched
    callbacks (e.g. stale slot picker from a previous bot run).
    """
    await callback.answer("Сессия истекла — начните через /book", show_alert=True)
