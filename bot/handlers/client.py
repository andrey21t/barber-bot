"""Booking flow handlers — Pure I/O layer (Telegram API + db session).

Contract (deep-analysis-protocol Pass 3):
- NO business logic in handlers — validation, html.escape, timezone conversion live in services
- 8 handlers + 1 fallback (no_state_fallback for State(None) after bot restart):
  1. cmd_book: /book, StateFilter(None) → selecting_date
  2. simple_calendar_cb: callback simple_calendar, StateFilter(selecting_date) → entering_service
     (Session 5.29 Task 2 — FSM reorder; was selecting_slot pre-5.29. Stale-keyboard only
     — new clients use book_date_cb. aiogram_calendar month navigation, callback.answer
     contract verified against lib source)
  2b. book_date_cb: callback book_date, StateFilter(selecting_date) → entering_service
      (BB-110 flat date picker; Session 5.29 Task 2 — was selecting_slot pre-5.29)
  3. slot_cb: callback book_slot:<uuid>, StateFilter(selecting_slot) → entering_name
  3b. slot_30_cb: callback book_slot_30, StateFilter(selecting_slot) → entering_name
  4. service_picker_cb: callback book_service, StateFilter(entering_service) → selecting_slot
     (Session 5.27 FEAT — tap-to-select; Session 5.29 Task 2 moved from confirming to
     selecting_slot, fetches slots filtered by service.duration_minutes)
  4b. service_msg: text, StateFilter(entering_service) → tap-only hint
      (Session 5.51 — free-text service input DISABLED: unknown duration
      corrupted the slot grid; typed text answers "выберите кнопкой")
  5. name_msg: text, StateFilter(entering_name) → confirming + render summary
     (Session 5.29 Task 2 — summary rendering moved here from service_msg/service_picker_cb)
  6. confirm_cb: callback book_confirm, StateFilter(confirming) → State(None) + create_booking
  7. cancel_msg: /cancel, StateFilter("*") → state.clear() + message
  + mybookings_msg / mybookings_cancel_cb / mybookings_transfer_cb / transfer_simple_calendar_cb
  + transfer_slot_cb + no_state_fallback: State(None), F.text, ~F.text.startswith("/")
    → "Начните через /book"

Flow (Session 5.29 Task 2 — услуга ДО слота):
  booking:  date → service → slot → name → confirm
  transfer: date → slot (existing booking's service reused) → confirm

Invariants (spec.md + MY-VIBE-RULES.md):
- state.clear() BEFORE event.answer (race condition)
- /cancel handler registered BEFORE /mybookings in router order (spec 491)
- create_booking exceptions caught: SlotAlreadyBookedError, SlotInPastError, SlotClosedError
- Bot restart mid-FSM → MemoryStorage loses state → no_state_fallback catches
"""

import logging
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback
from aiogram_calendar.schemas import SimpleCalAct
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from scheduler import schedule_for_booking
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings, get_settings
from bot.db import async_session_factory
from bot.keyboards.client import (
    CLIENT_REPLY_BOOK_LABEL,
    CLIENT_REPLY_MYBOOKINGS_LABEL,
    BookCancelCallbackData,
    BookConfirmCallbackData,
    BookDateCallbackData,
    BookServiceCallbackData,
    BookSlot30CallbackData,
    BookSlotCallbackData,
    ClientMenuBookCallbackData,
    ClientMenuMyBookingsCallbackData,
    MyBookingsCancelCallbackData,
    MyBookingsTransferCallbackData,
    NamePreFillOtherCallbackData,
    NamePreFillYesCallbackData,
    _format_booking_summary_from_start_at,
    calendar_keyboard,
    client_reply_keyboard,
    confirm_keyboard,
    date_picker_keyboard,
    mybookings_keyboard,
    name_pre_fill_keyboard,
    service_picker_keyboard,
    slot_picker_keyboard,
    slot_picker_keyboard_30min,
)
from bot.models import Booking, Master, Slot, WorkDay
from bot.schemas import BookingCreate
from bot.services.booking import (
    BookingAlreadyCancelledError,
    BookingAlreadyTransferredError,
    BookingNotFoundError,
    BookingOutsideWorkDayError,
    CancelResult,
    CancelTooLateError,
    SlotAlreadyBookedError,
    SlotClosedError,
    SlotInPastError,
    SlotNotAvailableError,
    TransferResult,
    WorkDayCapacityExceededError,
    _build_start_at_from_workday,
    _select_workday_for_slot,
    cancel_booking,
    create_booking,
    transfer_booking,
)
from bot.services.slots import (
    get_available_slots,
    get_available_slots_30,
    get_bookable_dates,
)
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
# Session 5.36 (B.13) — Reply keyboard + pre-fill name helpers
# ============================================================
def _client_first_name(callback: CallbackQuery) -> str:
    """Extract a non-empty first_name from callback.from_user, or ''.

    Returns '' (falsy) when:
    - callback.from_user is None (inaccessible message edge case)
    - from_user.first_name is None (private accounts without a profile name)
    - from_user.first_name is empty string

    Caller treats '' as "no pre-fill, fall back to text input" (slot_cb /
    slot_30_cb). When non-empty, caller shows the [✅ Да, это я] /
    [👤 Другое имя] inline keyboard in entering_name_pre_fill state.

    NB: strip()'ing the name so whitespace-only ("   ") also falls back to
    text input — avoids showing "Записать на    ?" with 3 spaces in the prompt.
    """
    if callback.from_user is None or callback.from_user.first_name is None:
        return ""
    name = callback.from_user.first_name.strip()
    return name


def _html_escape(text: str) -> str:
    """Escape user-supplied text for safe rendering in Telegram HTML parse mode.

    Used for the pre-fill prompt "Записать на <b>{name}</b>?" — first_name
    comes from the Telegram profile (user-controlled) and could contain
    HTML metacharacters (<, >, &). Without escaping, a name like "<script>"
    would break the <b> tag and render raw HTML.

    Mirror of bot.services.booking html.escape pattern (spec.md:315 —
    sanitize in service ПЕРЕД INSERT, render in HTML parse mode без
    повторного escape). Here we escape at render-time only (no DB write)
    because the name is displayed but NOT persisted on the pre-fill step —
    persistence happens later in name_msg / name_pre_fill_yes_cb via
    create_booking which does its own escape.
    """
    import html

    return html.escape(text, quote=False)


def _is_master(message: Message) -> bool:
    """Check if the message sender is the master (ADMIN_ID).

    Defense-in-depth for reply keyboard handlers (reply_book_msg /
    reply_mybookings_msg). Master should never see the client reply keyboard
    (cmd_start branches on ADMIN_ID), but a stale reply keyboard from a
    pre-B.13 session could linger on master's device. This guard prevents
    master from accidentally entering the client booking flow via a stale
    reply button tap.

    NB: returns False when from_user is None (defensive — treat unknown as
    non-master, so the reply handlers proceed normally for clients).
    """
    if message.from_user is None:
        return False
    return message.from_user.id == get_settings().ADMIN_ID


async def _restore_reply_keyboard_async(message: Message) -> None:
    """Send the client reply keyboard back to the user (Session 5.36 / B.13).

    Called from cancel_msg and confirm_cb AFTER the inline-keyboard message
    was sent. Guarded by _is_master — master does NOT get the client reply
    keyboard (they have admin_inline_menu).

    The text "👇 Кнопки внизу" is a visual anchor for the reply keyboard
    appearing below. Without it, Telegram sometimes delays showing the
    reply keyboard until the next user action; the explicit message forces
    immediate render.

    NB: this sends a SEPARATE message with the reply keyboard. Telegram
    forbids combining reply + inline keyboards in one message, so the
    booking flow messages stay in their own messages and this helper
    adds the reply keyboard in a follow-up. Minor visual noise (2 messages
    after confirm_cb: "✅ Вы записаны" + "👇 Кнопки внизу" with reply
    keyboard) but keeps the reply keyboard always visible.
    """
    if _is_master(message):
        return
    await message.answer("👇 Кнопки внизу", reply_markup=client_reply_keyboard())


async def _clear_source_keyboard(callback: CallbackQuery) -> None:
    """Remove the inline keyboard from the message the user just tapped (Session 5.51).

    Telegram keeps inline keyboards alive forever. Without this the chat
    accumulates dead keyboards from past flow steps (date picker, service
    picker, slot picker, ✅/❌ confirm) — tapping them after the flow moved
    on silently drops (state no longer matches) — the "кнопки не работают"
    UX complaint. Called at the START of flow callback handlers: the tapped
    message loses its buttons immediately, the fresh step message carries
    the live keyboard.

    Best-effort by design — never blocks the flow:
    - TelegramBadRequest "message is not modified" — already stripped (e.g.
      double-tap re-entered the handler); suppressed.
    - TelegramBadRequest "message to edit not found" — message deleted; suppressed.
    - InaccessibleMessage (>48h old channel post) — has no .edit_reply_markup;
      isinstance guard returns silently.
    """
    if not isinstance(callback.message, Message):
        return
    with suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(reply_markup=None)


# ============================================================
# 0a. reply_book_msg — [💇 Записаться] reply keyboard tap (Session 5.36 / B.13)
# ============================================================
@router.message(F.text == CLIENT_REPLY_BOOK_LABEL, StateFilter(None))
async def reply_book_msg(message: Message, state: FSMContext) -> None:
    """Client tapped [💇 Записаться] in the always-on reply keyboard.

    Same effect as /book (cmd_book) and client_book_cb (inline /start menu
    button, kept for stale keyboards from pre-B.13 sessions). Sets
    BookingStates.selecting_date + is_slots_path=False + shows date picker.

    Master guard (_is_master): master should never see the reply keyboard
    (cmd_start branches on ADMIN_ID), but a stale keyboard from a pre-B.13
    session could linger. If master taps it anyway → early return, no FSM
    entry. This is defense-in-depth — the primary guard is in cmd_start.

    Empty bookable list → text-only empty state (mirrors cmd_book). Non-empty
    → entering FSM + showing date picker.
    """
    if _is_master(message):
        return
    settings = get_settings()
    async with async_session_factory() as session:
        master = await _select_master(session, settings)
        if master is None:
            await message.answer("❌ Не удалось найти мастера. Обратитесь к администратору.")
            return
        dates = await get_bookable_dates(
            session,
            master.id,
            settings.TIMEZONE,
            include_legacy_slots=True,
            min_duration_min=settings.SERVICE_DEFAULT_DURATION_MIN,
            days_ahead=settings.MAX_BOOKING_DAYS_AHEAD,
        )
    if not dates:
        await message.answer("Сейчас нет свободных дат для записи. Загляните позже 🙏")
        return
    await state.set_state(BookingStates.selecting_date)
    await state.update_data(is_slots_path=False)
    today_local = datetime.now(ZoneInfo(settings.TIMEZONE)).date()
    await message.answer(
        "📅 Выберите дату записи:",
        reply_markup=date_picker_keyboard(dates, today=today_local),
    )


# ============================================================
# 0b. reply_mybookings_msg — [📋 Мои записи] reply keyboard tap (Session 5.36 / B.13)
# ============================================================
@router.message(F.text == CLIENT_REPLY_MYBOOKINGS_LABEL, StateFilter(None))
async def reply_mybookings_msg(message: Message) -> None:
    """Client tapped [📋 Мои записи] in the always-on reply keyboard.

    Same effect as /mybookings (mybookings_msg) — delegates to the shared
    _render_mybookings helper. Single source of truth for the list rendering.

    Master guard (_is_master): master should never see the reply keyboard.
    If master taps a stale button → early return.
    """
    if _is_master(message) or message.from_user is None:
        return
    await _render_mybookings(message, message.from_user.id)


# ============================================================
# 1. cmd_book — entry point (/book)
# ============================================================
@router.message(Command("book"), StateFilter(None))
async def cmd_book(message: Message, state: FSMContext) -> None:
    """Show date picker (Session 5.28 — BB-110, replaces SimpleCalendar).

    /book → flat list of bookable dates (active WorkDay with >=1 free 30-min
    slot fitting min_duration, OR legacy open slots) in
    [today, today+MAX_BOOKING_DAYS_AHEAD]. Past days excluded by the date
    range; non-working days excluded by the pre-filter — no more 'Мастер
    не работает' dead-end (PLANS.md:827).

    Empty bookable list → text-only empty state + state.clear (don't enter
    FSM with a dead-end keyboard). Non-empty → selecting_date + picker,
    is_slots_path=False (consistent with the legacy _handle_simple_calendar
    branches reachable via stale-calendar keyboards after the 5.28 deploy).
    """
    settings = get_settings()
    async with async_session_factory() as session:
        master = await _select_master(session, settings)
        if master is None:
            await message.answer("❌ Не удалось найти мастера. Обратитесь к администратору.")
            return
        dates = await get_bookable_dates(
            session,
            master.id,
            settings.TIMEZONE,
            include_legacy_slots=True,
            min_duration_min=settings.SERVICE_DEFAULT_DURATION_MIN,
            days_ahead=settings.MAX_BOOKING_DAYS_AHEAD,
        )
    if not dates:
        await message.answer("Сейчас нет свободных дат для записи. Загляните позже 🙏")
        return
    await state.set_state(BookingStates.selecting_date)
    await state.update_data(is_slots_path=False)
    today_local = datetime.now(ZoneInfo(settings.TIMEZONE)).date()
    await message.answer(
        "📅 Выберите дату записи:",
        reply_markup=date_picker_keyboard(dates, today=today_local),
    )


# ============================================================
# 1a. client_book_cb — [💇 Записаться] tap from /start menu (2026-09-06)
# ============================================================
@router.callback_query(ClientMenuBookCallbackData.filter(), StateFilter(None))
async def client_book_cb(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Client tapped [💇 Записаться] in /start inline menu — start booking flow.

    Same effect as /book (cmd_book): set BookingStates.selecting_date +
    is_slots_path=False + show date_picker_keyboard. Extracted as a callback
    handler so clients who tap the /start menu button don't need to know
    about the /book text command (solves "client sees empty chat" — before
    this fix /start replied with bare text "Запишитесь командой /book"
    and clients without bot experience closed the chat).

    Empty bookable list → text-only empty state + state.clear (mirrors
    cmd_book). Non-empty → entering FSM + showing date picker.

    StateFilter(None) — booking entry only works outside FSM. If user is
    mid-flow and somehow taps a stale /start menu button, aiogram dispatch
    falls through to no_state_callback_fallback (line ~1600) which shows
    "Сессия истекла" alert.

    callback.answer() closes the Telegram loading spinner on the inline
    button (UX contract — every callback handler must answer).

    Session 5.51: strips the tapped stale /start menu keyboard.
    """
    await _clear_source_keyboard(callback)
    settings = get_settings()
    async with async_session_factory() as session:
        master = await _select_master(session, settings)
        if master is None:
            if callback.message is not None:
                await callback.message.answer(
                    "❌ Не удалось найти мастера. Обратитесь к администратору."
                )
            await callback.answer()
            return
        dates = await get_bookable_dates(
            session,
            master.id,
            settings.TIMEZONE,
            include_legacy_slots=True,
            min_duration_min=settings.SERVICE_DEFAULT_DURATION_MIN,
            days_ahead=settings.MAX_BOOKING_DAYS_AHEAD,
        )
    if not dates:
        if callback.message is not None:
            await callback.message.answer("Сейчас нет свободных дат для записи. Загляните позже 🙏")
        await callback.answer()
        return
    await state.set_state(BookingStates.selecting_date)
    await state.update_data(is_slots_path=False)
    today_local = datetime.now(ZoneInfo(settings.TIMEZONE)).date()
    if callback.message is not None:
        await callback.message.answer(
            "📅 Выберите дату записи:",
            reply_markup=date_picker_keyboard(dates, today=today_local),
        )
    await callback.answer()


# ============================================================
# 1b. cmd_slots — entry point (/slots, Этап 5.8b — WorkDay path)
# ============================================================
@router.message(Command("slots"), StateFilter(None))
async def cmd_slots(message: Message, state: FSMContext) -> None:
    """Entry point for /slots — date picker (BB-110), WorkDay-only scope.

    Mirrors cmd_book but uses include_legacy_slots=False: /slots is the
    workday-only command (cmd_slots _handle_simple_calendar branch never
    falls back to legacy slots), so a date opened only via /addslots must
    NOT appear under /slots. Bookable date = active WorkDay with >=1 free
    30-min slot fitting min_duration.

    Sets `is_slots_path=True` in FSM data (unchanged since 5.8b): the
    shared _process_selected_date helper branches on this flag — True →
    workday-only slot picker (slot_picker_keyboard_30min); False → legacy
    /book path with WorkDay fallback (slot_picker_keyboard or 30-min).
    BookingStates.selecting_date is shared between /book and /slots —
    single-master MVP doesn't warrant a separate SlotsBookingStates group
    (see Pass 3 state-pollution tradeoff in deep-analysis-protocol
    Session 5.23 critic iter 2 — pragmatic for 2 flows; if a 3rd client
    flow is added, refactor to SlotsBookingStates).
    """
    settings = get_settings()
    async with async_session_factory() as session:
        master = await _select_master(session, settings)
        if master is None:
            await message.answer("❌ Не удалось найти мастера. Обратитесь к администратору.")
            return
        dates = await get_bookable_dates(
            session,
            master.id,
            settings.TIMEZONE,
            include_legacy_slots=False,
            min_duration_min=settings.SERVICE_DEFAULT_DURATION_MIN,
            days_ahead=settings.MAX_BOOKING_DAYS_AHEAD,
        )
    if not dates:
        await message.answer("Сейчас нет свободных дат для записи. Загляните позже 🙏")
        return
    await state.set_state(BookingStates.selecting_date)
    await state.update_data(is_slots_path=True)
    today_local = datetime.now(ZoneInfo(settings.TIMEZONE)).date()
    await message.answer(
        "📅 Выберите дату записи:",
        reply_markup=date_picker_keyboard(dates, today=today_local),
    )


# ============================================================
# 1c. _select_master / _retry_markup / _fetch_slots_for_service / _process_selected_date
# Shared helpers for cmd_book/cmd_slots + simple_calendar_cb (stale keyboards)
# + book_date_cb (BB-110 date picker).
# ============================================================


async def _select_master(session: AsyncSession, settings: Settings) -> Master | None:
    """Resolve single-master by ADMIN_ID (single-master MVP, BB-001).

    Used by cmd_book/cmd_slots (BB-110 date picker entry) and
    _process_selected_date (post-tap slot fetching) to avoid duplicating
    the lookup inline — Inline `from sqlalchemy import select`/`from bot.models
    import Master` blocks previously repeated the same 4-line block in every
    site (4 occurrences pre-5.28).
    """
    stmt = select(Master).where(Master.telegram_id == settings.ADMIN_ID).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _retry_markup(
    session: AsyncSession,
    master: Master,
    settings: Settings,
    *,
    is_transfer: bool,
    is_slots_path: bool | None,
) -> InlineKeyboardMarkup:
    """Re-render date selector after a race (workday closed / slots gone).

    Transfer flow keeps SimpleCalendar (BB-110 scope = /book and /slots
    only — /transfer semantics differ: user picks a new date for an existing
    booking; the calendar lets them navigate freely across the month).

    /book and /slots re-query get_bookable_dates and render the new picker;
    empty result renders the defensive 'Нет свободных дат' button (see
    date_picker_keyboard). min_duration_min mirrors the value the slot picker
    will pass to get_available_slots_30 (BUG2 fix consistency).
    include_legacy mirrors the cmd entry (/slots workday-only; /book union).
    is_slots_path=None (pre-5.8b in-flight FSM data) is treated as /book
    legacy semantics — falsy → include_legacy_slots=True — backward compat.

    Caller responsibility: pass is_slots_path from FSM data.
    """
    if is_transfer:
        return await calendar_keyboard(*_calendar_range(settings))
    dates = await get_bookable_dates(
        session,
        master.id,
        settings.TIMEZONE,
        include_legacy_slots=not is_slots_path,
        min_duration_min=settings.SERVICE_DEFAULT_DURATION_MIN,
        days_ahead=settings.MAX_BOOKING_DAYS_AHEAD,
    )
    today_local = datetime.now(ZoneInfo(settings.TIMEZONE)).date()
    return date_picker_keyboard(dates, today=today_local)


async def _fetch_slot_picker_for_service(
    session: AsyncSession,
    master: Master,
    slot_date: date,
    settings: Settings,
    *,
    is_slots_path: bool,
    min_duration_min: int,
) -> InlineKeyboardMarkup | None:
    """Fetch slots filtered by service duration and build slot picker keyboard
    (Session 5.29 Task 2).

    Used by service_picker_cb and service_msg AFTER service selection —
    min_duration_min is the real service.duration_minutes (or
    SERVICE_DEFAULT_DURATION_MIN for free-text). The overlap filter in
    get_available_slots_30 uses this duration (slots.py:219 fix), so a
    slot 15:30 is hidden when an 120-min booking starts at 16:00 (15:30+120
    = 17:30 overlaps 16:00-18:00) but shown for a 60-min booking where
    15:30+60 = 16:30 still overlaps (also hidden — for shorter windows the
    30-min grid step is the fallback when min_duration_min=0).

    Returns:
        InlineKeyboardMarkup — slot picker keyboard ready to render
        (slot_picker_keyboard_30min for WorkDay path, slot_picker_keyboard
        for legacy Slot path). Caller renders it directly via
        callback.message.answer("Выберите время:", reply_markup=keyboard).
        None — no slots available (workday closed / master doesn't work /
        all slots booked). Caller shows "no slots" hint and a retry date picker.

    Why keyboard instead of slots list (deviation from plan B.1):
        Plan B.1 proposed `-> list[Slot] | list[TimeSlot30] | None` with
        keyboard build in the caller. mypy rejects the union type passed
        to slot_picker_keyboard_30min (expects list[TimeSlot30]) /
        slot_picker_keyboard (expects list[Slot]) — list element type is
        not narrowed by the workday None/not-None check (correlation is
        runtime, not type-level). Returning the keyboard from the helper
        encapsulates the type discrimination at the build site — single
        source of truth, no caller-side cast/assert.

    Branch mirrors _process_selected_date transfer flow (is_slots_path True
    → workday-only; False → legacy Slot with workday fallback). NOT used by
    transfer (transfer has its own flow inside _process_selected_date —
    service is taken from the existing booking snapshot, no service picker).
    """
    if is_slots_path:
        # === /slots workday branch (Этап 5.8b) ===
        workday = await _select_workday_for_slot(session, master.id, slot_date)
        if workday is None or not workday.is_active:
            return None
        slots_30 = await get_available_slots_30(
            session,
            workday,
            settings.TIMEZONE,
            min_duration_min=min_duration_min,
        )
        if not slots_30:
            return None
        return slot_picker_keyboard_30min(slots_30, workday.id)

    # === /book legacy slot branch with WorkDay fallback (5.27) ===
    slots = await get_available_slots(session, master.id, slot_date)
    if slots:
        return slot_picker_keyboard(slots)

    # Legacy slots empty → try WorkDay (openweek writes to work_days, not slots).
    workday = await _select_workday_for_slot(session, master.id, slot_date)
    if workday is None or not workday.is_active:
        return None
    slots_30 = await get_available_slots_30(
        session,
        workday,
        settings.TIMEZONE,
        min_duration_min=min_duration_min,
    )
    if not slots_30:
        return None
    return slot_picker_keyboard_30min(slots_30, workday.id)


async def _process_selected_date(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    *,
    next_state: State,
    is_transfer: bool,
    slot_date: date,
) -> None:
    """Shared body of simple_calendar_cb act=day (stale-keyboard retry) and
    book_date_cb tap (BB-110).

    Session 5.29 (Task 2 — FSM reorder: услуга ДО слота):
    - is_transfer=True: unchanged — workday/legacy branching, fetch slots,
      set next_state=TransferStates.selecting_slot, render slot picker.
      Service is taken from the existing booking snapshot, no service picker.
    - is_transfer=False (booking): NEW — fetch services for master's business,
      set next_state=BookingStates.entering_service, render service picker
      (or free-text prompt if no services in DB). Slot fetching moved to
      service_picker_cb/service_msg (after service selection — uses real
      service.duration_minutes for overlap filter against existing bookings,
      fixes the 15:30+Стрижка vs 16:00-18:00-Окрашивание overlap bug).

    Transfer retry paths (workday None/closed, no free slots) use _retry_markup
    — calendar for /transfer, date picker for /book and /slots. Race
    protection: between picker render and tap, all slots may have been
    booked by another client or /closeday may have deactivated the workday.
    Booking flow has no retry paths here — service picker is always renderable;
    slot retry happens after service selection (service_picker_cb/service_msg).

    Session 5.51: the tapped date-picker message loses its inline keyboard
    immediately (_clear_source_keyboard) — every branch below either renders
    the next step or a fresh retry picker, so the old keyboard is always dead.
    """
    # Strip the tapped date picker's keyboard FIRST — every branch renders
    # a new message (next-step picker / retry picker / terminal hint), so
    # the old keyboard is dead from this moment on.
    await _clear_source_keyboard(callback)

    fsm_data = await state.get_data()
    is_slots_path: bool | None = fsm_data.get("is_slots_path")

    async with async_session_factory() as session:
        master = await _select_master(session, settings)
        if master is None:
            await state.clear()
            if callback.message is not None:
                await callback.message.answer(
                    "❌ Не удалось найти мастера. Обратитесь к администратору."
                )
            await callback.answer()
            return

        if not is_transfer:
            # === Booking flow (Session 5.29 Task 2): дата → услуга → слот ===
            # Fetch services for master's business, render service picker.
            # Slot fetching moved to service_picker_cb (after service
            # selection — uses real service.duration_minutes for overlap
            # filter against existing bookings, fixes the 15:30+Стрижка vs
            # 16:00-18:00-Окрашивание overlap bug).
            # Session 5.51: free-text fallback REMOVED. No services in DB →
            # booking impossible (unknown duration would corrupt the slot
            # grid) → clear FSM + ask to come back later.
            from bot.models import Business, Service  # noqa: PLC0415

            stmt_b = select(Business).where(Business.id == master.business_id).limit(1)
            business = (await session.execute(stmt_b)).scalar_one_or_none()
            if business is None:
                services = []
            else:
                stmt_s = (
                    select(Service)
                    .where(Service.business_id == business.id, Service.is_active == True)  # noqa: E712
                    .order_by(Service.name)
                )
                services = list((await session.execute(stmt_s)).scalars().all())
            if not services:
                await state.clear()
                if callback.message is not None:
                    await callback.message.answer(
                        "Мастер пока не настроил услуги. Загляните позже 🙏"
                    )
                await callback.answer()
                return

            await state.update_data(selected_date=slot_date.isoformat())
            await state.set_state(next_state)
            if callback.message is not None:
                await callback.message.answer(
                    "Выберите услугу:",
                    reply_markup=service_picker_keyboard(services),
                )
            await callback.answer()
            return

        # === Transfer flow (unchanged): workday/legacy branching, slot picker ===
        if is_slots_path:
            # === /slots workday branch (Этап 5.8b) ===
            # Fetch WorkDay for (master_id, slot_date). If None → master doesn't
            # work that day (no /openday). If is_active=False → closed via
            # /closeday. Both → user-facing hint, no slot picker shown.
            # BB-110: /slots entry pre-filters dates so this branch is only
            # reachable via stale-keyboard retry or race (workday closed
            # between picker render and tap).
            workday = await _select_workday_for_slot(session, master.id, slot_date)
            if workday is None:
                # W2 fallback: no WorkDay — try legacy slots (mirror /book branch
                # fallback at line 765). Migration 005 converted all Slot → WorkDay,
                # but edge-case masters without WorkDay or stale Slot rows are
                # handled here. Legacy slots → BookSlotCallbackData → transfer_slot_cb.
                slots = await get_available_slots(session, master.id, slot_date)
                if slots:
                    await state.update_data(selected_date=slot_date.isoformat())
                    await state.set_state(next_state)
                    if callback.message is not None:
                        await callback.message.answer(
                            "Выберите новое время:" if is_transfer else "Выберите время:",
                            reply_markup=slot_picker_keyboard(slots, show_back=not is_transfer),
                        )
                    await callback.answer()
                    return
                if callback.message is not None:
                    await callback.message.answer(
                        "Мастер не работает в этот день. Выберите другую дату:",
                        reply_markup=await _retry_markup(
                            session,
                            master,
                            settings,
                            is_transfer=is_transfer,
                            is_slots_path=is_slots_path,
                        ),
                    )
                await callback.answer()
                return
            if not workday.is_active:
                if callback.message is not None:
                    await callback.message.answer(
                        "День закрыт мастером. Выберите другую дату:",
                        reply_markup=await _retry_markup(
                            session,
                            master,
                            settings,
                            is_transfer=is_transfer,
                            is_slots_path=is_slots_path,
                        ),
                    )
                await callback.answer()
                return
            # WorkDay active — fetch 30-min slots with capacity check.
            # get_available_slots_30 filters past slots via now_utc injection
            # (default datetime.now(UTC) inside — caller doesn't need to pass).
            # Session 5.27 BUG2: pass min_duration_min=SERVICE_DEFAULT_DURATION_MIN
            # so slots that don't fit a default 60-min booking are hidden —
            # prevents misleading BookingOutsideWorkDayError at confirm.
            # NB: transfer uses SERVICE_DEFAULT_DURATION_MIN (snapshot's
            # service duration is not re-applied here — same overlap-bug as
            # admin_move, NOT fixed in this task; see slots.py:219 comment).
            slots_30 = await get_available_slots_30(
                session,
                workday,
                settings.TIMEZONE,
                min_duration_min=settings.SERVICE_DEFAULT_DURATION_MIN,
            )
            if not slots_30:
                if callback.message is not None:
                    await callback.message.answer(
                        "На эту дату нет свободного времени. Выберите другую дату:",
                        reply_markup=await _retry_markup(
                            session,
                            master,
                            settings,
                            is_transfer=is_transfer,
                            is_slots_path=is_slots_path,
                        ),
                    )
                await callback.answer()
                return
            await state.update_data(selected_date=slot_date.isoformat())
            await state.set_state(next_state)
            if callback.message is not None:
                await callback.message.answer(
                    "Выберите новое время:" if is_transfer else "Выберите время:",
                    # S1 review fix F2: suppress back button in transfer flow (no service
                    # step in transfer → "back to service" has no meaning, dead button).
                    reply_markup=slot_picker_keyboard_30min(
                        slots_30, workday.id, show_back=not is_transfer
                    ),
                )
            await callback.answer()
            return

        # === /book legacy slot branch (existing) ===
        slots = await get_available_slots(session, master.id, slot_date)
        if not slots:
            # Session 5.27 fallback: legacy slots empty → try WorkDay.
            # /openweek (Session 5.26) writes to work_days, not slots.
            # Without this fallback, /book users can't book days opened
            # via /openweek — only /slots could. Maintain backward compat
            # by transparently switching /book to 30-min WorkDay picker
            # when no legacy slots exist for the date.
            # BB-110: /book entry pre-filters dates so this is reachable via
            # stale-keyboard retry or race only.
            workday = await _select_workday_for_slot(session, master.id, slot_date)
            if workday is not None and workday.is_active:
                slots_30 = await get_available_slots_30(
                    session,
                    workday,
                    settings.TIMEZONE,
                    min_duration_min=settings.SERVICE_DEFAULT_DURATION_MIN,
                )
                if slots_30:
                    await state.update_data(selected_date=slot_date.isoformat())
                    await state.set_state(next_state)
                    if callback.message is not None:
                        await callback.message.answer(
                            "Выберите новое время:" if is_transfer else "Выберите время:",
                            # S1 review fix F2: suppress back button in transfer flow.
                            reply_markup=slot_picker_keyboard_30min(
                                slots_30, workday.id, show_back=not is_transfer
                            ),
                        )
                    await callback.answer()
                    return
                # workday active but no free slots — fall through to message below.
            elif workday is not None and not workday.is_active:
                # workday exists but closed via /closeday — show closed hint.
                if callback.message is not None:
                    await callback.message.answer(
                        "День закрыт мастером. Выберите другую дату:",
                        reply_markup=await _retry_markup(
                            session,
                            master,
                            settings,
                            is_transfer=is_transfer,
                            is_slots_path=is_slots_path,
                        ),
                    )
                await callback.answer()
                return
            if callback.message is not None:
                await callback.message.answer(
                    "На эту дату нет свободных слотов. Выберите другую дату:",
                    reply_markup=await _retry_markup(
                        session,
                        master,
                        settings,
                        is_transfer=is_transfer,
                        is_slots_path=is_slots_path,
                    ),
                )
            await callback.answer()
            return
        await state.update_data(selected_date=slot_date.isoformat())
        await state.set_state(next_state)
        if callback.message is not None:
            await callback.message.answer(
                "Выберите новое время:" if is_transfer else "Выберите время:",
                # S1 review fix F2: suppress back button in transfer flow (no service
                # step in transfer → "back to service" has no meaning, dead button).
                # Booking flow keeps back via _fetch_slot_picker_for_service (line 410,
                # default show_back=True) — this line 664 only reached in transfer
                # (booking returns earlier at line 513 entering service picker).
                reply_markup=slot_picker_keyboard(slots, show_back=not is_transfer),
            )
        await callback.answer()
        return


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
#
# Session 5.28 (BB-110): this handler REMAINS for /transfer (its picker is
# still SimpleCalendar) AND for stale-calendar keyboards from before the
# 5.28 deploy — clients with an old picker message on screen can still
# tap a day; the act=day body delegates to _process_selected_date, which
# re-renders the NEW date picker on retry (graceful upgrade path). New
# clients go through cmd_book/cmd_slots → date_picker_keyboard →
# book_date_cb instead.
async def _handle_simple_calendar(
    callback: CallbackQuery,
    callback_data: SimpleCalendarCallback,
    state: FSMContext,
    next_state: State,
    is_transfer: bool,
) -> None:
    """Shared logic for booking + transfer SimpleCalendar handlers.

    Branches on callback_data.act to call callback.answer() only when lib has
    not already answered (F1 fix). For act=day: delegates to
    _process_selected_date which sets next_state (entering_service for booking
    since Session 5.29 Task 2; selecting_slot for transfer). For act=cancel:
    clears FSM state.
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
        # BB-110: act=day here means a STALE SimpleCalendar keyboard (pre-5.28
        # deploy) or a /transfer keyboard — both still navigate via this
        # handler. Body extracted to _process_selected_date (shared with
        # book_date_cb) so retries render the NEW date picker for /book and
        # /slots while /transfer keeps SimpleCalendar.
        await _process_selected_date(
            callback,
            state,
            settings,
            next_state=next_state,
            is_transfer=is_transfer,
            slot_date=slot_date,
        )
        return

    if callback_data.act == SimpleCalAct.cancel:
        # Этап 5.8b W1 (code-review iter 2): /slots vs /book hint branching.
        # Read is_slots_path ДО state.clear() — после clear флаг потерян.
        # is_transfer branch уже отличает transfer flow — добавляем /slots
        # различие внутри not-transfer (booking flow) для согласованности с
        # SlotAlreadyBooked/SlotInPast retry-cmd branching в confirm_cb.
        if not is_transfer:
            fsm_data_cancel = await state.get_data()
            is_slots_path_cancel: bool | None = fsm_data_cancel.get("is_slots_path")
        # state.clear() BEFORE callback.answer (race condition, MY-VIBE-RULES.md:23)
        await state.clear()
        if callback.message is not None:
            if is_transfer:
                hint = "Перенос отменён. /mybookings чтобы начать заново"
            elif is_slots_path_cancel:
                hint = "Ввод отменён. /slots чтобы начать заново"
            else:
                hint = "Ввод отменён. /book чтобы начать заново"
            await callback.message.answer(hint)
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
    """SimpleCalendar navigation + day select for booking flow (cmd_book).

    Session 5.29 (Task 2): only reachable via stale keyboards from before the
    5.29 deploy (BB-110 replaced the calendar with the flat date picker for
    /book and /slots; Task 2 moved slot fetching to service_picker_cb). New
    clients go through book_date_cb. The act=day branch delegates to
    _process_selected_date which sets next_state=entering_service (booking
    flow renders service picker first, slot picker after service selection).
    Stale-keyboard taps upgrade the UX in-place via _retry_markup.
    """
    await _handle_simple_calendar(
        callback=callback,
        callback_data=callback_data,
        state=state,
        next_state=BookingStates.entering_service,
        is_transfer=False,
    )


# ============================================================
# 2b. book_date_cb — user tapped a date button in the BB-110 picker
# ============================================================
@router.callback_query(BookDateCallbackData.filter(), StateFilter(BookingStates.selecting_date))
async def book_date_cb(
    callback: CallbackQuery,
    callback_data: BookDateCallbackData,
    state: FSMContext,
) -> None:
    """Date picker tap → fetch services for that date (Session 5.28 — BB-110,
    Session 5.29 Task 2 — FSM reorder).

    Parses ISO work_date, delegates to _process_selected_date (same body
    the stale-keyboard simple_calendar_cb act=day uses) — single source of
    truth for the service-fetch branches (booking) / slot-fetch branches
    (transfer). is_transfer=False → next_state=entering_service (renders
    service picker; slot picker moved to service_picker_cb). is_transfer via
    TransferStates.selecting_date + transfer_simple_calendar_cb is unaffected.

    Defensive parse: callback_data could be tampered (Telegram allows users
    to send arbitrary callback_data). date.fromisoformat raises ValueError
    on invalid input — caught, answer '❌ Неверная дата' and return (FSM
    state preserved so user can tap another button or cancel).
    """
    settings = get_settings()
    try:
        slot_date = date.fromisoformat(callback_data.work_date)
    except ValueError:
        if callback.message is not None:
            await callback.message.answer("❌ Неверная дата. Выберите другую:")
        await callback.answer()
        return
    await _process_selected_date(
        callback,
        state,
        settings,
        next_state=BookingStates.entering_service,
        is_transfer=False,
        slot_date=slot_date,
    )


# ============================================================
# 2c. book_date_cancel_cb — user tapped '❌ Отмена' in the BB-110 picker
# ============================================================
@router.callback_query(F.data == "book_date_cancel", StateFilter(BookingStates.selecting_date))
async def book_date_cancel_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Cancel date selection — clears FSM with /book or /slots hint.

    Mirrors the simple_calendar_cb act=cancel W1 logic (read is_slots_path
    BEFORE state.clear — flag is lost after clear) and applies the same
    /book vs /slots hint branching. /transfer doesn't reach this handler
    (uses TransferStates.selecting_date + transfer_simple_calendar_cb +
    SimpleCalendar's own 'Отмена' button → SimpleCalAct.cancel branch).

    Session 5.51: strips the tapped picker's keyboard (flow terminates).
    """
    fsm_data = await state.get_data()
    is_slots_path: bool | None = fsm_data.get("is_slots_path")
    # state.clear() BEFORE callback.answer (race condition, MY-VIBE-RULES.md:23).
    await state.clear()
    await _clear_source_keyboard(callback)
    if callback.message is not None:
        if is_slots_path:
            hint = "Ввод отменён. /slots чтобы начать заново"
        else:
            hint = "Ввод отменён. /book чтобы начать заново"
        await callback.message.answer(hint)
    await callback.answer()


# ============================================================
# 3. slot_cb — user picked a slot → ask for name
# ============================================================
@router.callback_query(BookSlotCallbackData.filter(), StateFilter(BookingStates.selecting_slot))
async def slot_cb(
    callback: CallbackQuery,
    callback_data: BookSlotCallbackData,
    state: FSMContext,
) -> None:
    """User selected a slot — save slot_id, ask for client name.

    Defensive: service_title should be set by service_picker_cb/service_msg
    before selecting_slot. State corruption (e.g. in-flight session carried
    over from pre-5.29 flow with selecting_slot+slot_id but no service_title)
    → state.clear + retry hint, no stale entering_name state.
    Checked BEFORE set_state(entering_name) so defensive-clear path does
    not leave a stale entering_name state in FSM (Session 5.29 Task 2, W2).
    """
    data = await state.get_data()
    if not data.get("service_title"):
        await state.clear()
        if callback.message is not None:
            await callback.message.answer(
                "❌ Данные потеряны. Начните заново через /book или /slots"
            )
        await callback.answer()
        return
    await _clear_source_keyboard(callback)
    await state.update_data(slot_id=str(callback_data.slot_id))
    # Session 5.36 (B.13): pre-fill name branch. If from_user.first_name is
    # non-empty, show inline [✅ Да, это я] / [👤 Другое имя] instead of the
    # old text-only prompt. Client taps "Да" → skip entering_name (one tap
    # less for the 80% case of booking yourself). "Другое имя" → text input
    # as before (booking child/husband/etc.).
    first_name = _client_first_name(callback)
    if first_name:
        await state.set_state(BookingStates.entering_name_pre_fill)
        if callback.message is not None:
            # ReplyKeyboardRemove — reply keyboard мешает текстовому вводу и
            # занимает экран. Pre-fill state ловит только callback (не текст),
            # но reply-кнопки все равно убираем для визуальной чистоты — inline
            # [✅ Да] / [👤 Другое имя] заменяют их на этом шаге.
            await callback.message.answer(
                f"Записать на <b>{_html_escape(first_name)}</b>? "
                "(ваше имя в Telegram)",
                reply_markup=ReplyKeyboardRemove(),
            )
            await callback.message.answer(
                "Выберите:",
                reply_markup=name_pre_fill_keyboard(first_name),
            )
    else:
        # Fallback: from_user is None OR first_name is None/empty (private
        # accounts without a profile name). Old text-input flow unchanged.
        await state.set_state(BookingStates.entering_name)
        if callback.message is not None:
            await callback.message.answer(
                "На чьё имя записываем? (например: Паша, я сам, сын 5 лет)",
                reply_markup=ReplyKeyboardRemove(),
            )
    await callback.answer()


# ============================================================
# 3b. slot_30_cb — user picked a 30-min WorkDay slot → ask for name (Этап 5.8b)
# ============================================================
@router.callback_query(BookSlot30CallbackData.filter(), StateFilter(BookingStates.selecting_slot))
async def slot_30_cb(
    callback: CallbackQuery,
    callback_data: BookSlot30CallbackData,
    state: FSMContext,
) -> None:
    """User selected a 30-min WorkDay slot (via /slots) — save workday_id and
    start_minute, ask for client name.

    Defensive range check: aiogram CallbackData validates types at pack/unpack,
    but a malicious/tampered callback could carry out-of-range start_minute.
    Range 0-1439 (00:00 - 23:59). Reject → state.clear() + hint, no crash.

    Defensive: service_title should be set by service_picker_cb/service_msg
    before selecting_slot. State corruption (e.g. in-flight session carried
    over from pre-5.29 flow) → state.clear + retry hint, no stale entering_name.
    Checked BEFORE set_state(entering_name) so defensive-clear path does
    not leave a stale entering_name state in FSM (Session 5.29 Task 2, W2).

    Registration BEFORE no_state_callback_fallback (router order — registered
    top-down, callback dispatch first-match). Same StateFilter(selecting_slot)
    as slot_cb but distinct CallbackData prefix (book_slot_30 vs book_slot) —
    aiogram dispatch is exact-prefix match (callback_data.py:117-125).
    """
    start_minute = callback_data.start_minute
    if not (0 <= start_minute <= 1439):
        await state.clear()
        if callback.message is not None:
            await callback.message.answer("❌ Ошибка выбора времени. Начните заново через /slots")
        await callback.answer()
        return
    data = await state.get_data()
    if not data.get("service_title"):
        await state.clear()
        if callback.message is not None:
            await callback.message.answer(
                "❌ Данные потеряны. Начните заново через /book или /slots"
            )
        await callback.answer()
        return
    await _clear_source_keyboard(callback)
    await state.update_data(
        workday_id=str(callback_data.workday_id),
        start_minute=start_minute,
    )
    # Session 5.36 (B.13): pre-fill name branch (mirror slot_cb).
    first_name = _client_first_name(callback)
    if first_name:
        await state.set_state(BookingStates.entering_name_pre_fill)
        if callback.message is not None:
            await callback.message.answer(
                f"Записать на <b>{_html_escape(first_name)}</b>? "
                "(ваше имя в Telegram)",
                reply_markup=ReplyKeyboardRemove(),
            )
            await callback.message.answer(
                "Выберите:",
                reply_markup=name_pre_fill_keyboard(first_name),
            )
    else:
        await state.set_state(BookingStates.entering_name)
        if callback.message is not None:
            await callback.message.answer(
                "На чьё имя записываем? (например: Паша, я сам, сын 5 лет)",
                reply_markup=ReplyKeyboardRemove(),
            )
    await callback.answer()


# ============================================================
# 3d. name_pre_fill_yes_cb — [✅ Да, это я] tap (Session 5.36 / B.13)
# ============================================================
@router.callback_query(
    NamePreFillYesCallbackData.filter(),
    StateFilter(BookingStates.entering_name_pre_fill),
)
async def name_pre_fill_yes_cb(
    callback: CallbackQuery,
    callback_data: NamePreFillYesCallbackData,
    state: FSMContext,
) -> None:
    """Client confirmed their Telegram first_name — skip text input, go to
    confirming directly (phone step removed — name → confirm).

    Reads from_user.first_name (NOT from callback payload — kept minimal,
    avoids stale-name race). Saves client_name in state data and transitions
    to confirming via _render_summary_and_set_confirming.

    Defensive: if first_name is missing at this point (race — user revoked
    profile between slot_cb and this tap), fall back to entering_name text
    input (no crash, no lost state).
    """
    await _clear_source_keyboard(callback)
    first_name = _client_first_name(callback)
    if not first_name:
        # Race: from_user became None OR first_name stripped to empty between
        # slot_cb (which gated on first_name truthy) and this callback. Fall
        # back to text input — safer than failing silently.
        await state.set_state(BookingStates.entering_name)
        if callback.message is not None:
            await callback.message.answer(
                "На чьё имя записываем? (например: Паша, я сам, сын 5 лет)"
            )
        await callback.answer()
        return

    await state.update_data(client_name=first_name)

    # Defensive: service_title should be set earlier in the flow. If missing,
    # state.clear + retry (mirror name_msg defensive path).
    data = await state.get_data()
    service_title = data.get("service_title")
    if not service_title:
        await state.clear()
        if callback.message is not None:
            await callback.message.answer(
                "❌ Данные потеряны. Начните заново через /book или /slots"
            )
        await callback.answer()
        return

    # Transition directly to confirming — phone step removed.
    # _render_summary_and_set_confirming renders summary + confirm_keyboard.
    # Type narrowing: InaccessibleMessage (old channel post) has no .answer()
    # — same isinstance pattern as confirm_cb. Pre-existing mypy error fix.
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    if not await _render_summary_and_set_confirming(callback.message, state, first_name):
        await callback.answer()
        return
    await callback.answer()


# ============================================================
# 3e. name_pre_fill_other_cb — [👤 Другое имя] tap (Session 5.36 / B.13)
# ============================================================
@router.callback_query(
    NamePreFillOtherCallbackData.filter(),
    StateFilter(BookingStates.entering_name_pre_fill),
)
async def name_pre_fill_other_cb(
    callback: CallbackQuery,
    callback_data: NamePreFillOtherCallbackData,
    state: FSMContext,
) -> None:
    """Client chose to enter a different name — transition to text input.

    No payload, no state data update — just a state transition from
    entering_name_pre_fill to entering_name. The text input handler
    (name_msg) takes over from here, same as the old pre-B.13 flow.

    NB: ReplyKeyboardRemove was already sent in slot_cb/slot_30_cb when
    entering entering_name_pre_fill, so the reply keyboard is already
    hidden — no need to send it again here.

    Session 5.51: strips the tapped [✅ Да, это я] keyboard.
    """
    await _clear_source_keyboard(callback)
    await state.set_state(BookingStates.entering_name)
    if callback.message is not None:
        await callback.message.answer(
            "На чьё имя записываем? (например: Паша, я сам, сын 5 лет)"
        )
    await callback.answer()


# ============================================================
# 3c. book_back_to_service_cb — user tapped '↩️ Назад' in slot picker
# ============================================================
@router.callback_query(F.data == "book_back_to_service", StateFilter(BookingStates.selecting_slot))
async def book_back_to_service_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Back from slot picker to service picker (Session 5.30 S1).

    Lets user change service without /cancel + /book restart. Re-loads
    services for master's business, renders service_picker_keyboard.

    State transitions: selecting_slot → entering_service.
    Defensive: master not found → state.clear + retry hint (race).
    Session 5.51: no business / no services → booking impossible (free-text
    removed) → state.clear + "не настроил услуги". Strips the tapped
    slot-picker keyboard — every branch renders a new message or ends the flow.
    """
    await _clear_source_keyboard(callback)
    settings = get_settings()
    async with async_session_factory() as session:
        master = await _select_master(session, settings)
        if master is None:
            await state.clear()
            if callback.message is not None:
                await callback.message.answer("❌ Мастер не найден. /book чтобы начать")
            await callback.answer()
            return
        from bot.models import Business, Service  # noqa: PLC0415

        stmt_b = select(Business).where(Business.id == master.business_id).limit(1)
        business = (await session.execute(stmt_b)).scalar_one_or_none()
        if business is None:
            services = []
        else:
            stmt_s = (
                select(Service)
                .where(Service.business_id == business.id, Service.is_active == True)  # noqa: E712
                .order_by(Service.name)
            )
            services = list((await session.execute(stmt_s)).scalars().all())
        if not services:
            await state.clear()
            if callback.message is not None:
                await callback.message.answer(
                    "Мастер пока не настроил услуги. Загляните позже 🙏"
                )
            await callback.answer()
            return
    await state.set_state(BookingStates.entering_service)
    if callback.message is not None:
        await callback.message.answer(
            "Выберите услугу:",
            reply_markup=service_picker_keyboard(services),
        )
    await callback.answer()


# ============================================================
# 4. name_msg — user typed name → ask for service
# ============================================================
@router.message(StateFilter(BookingStates.entering_name))
async def name_msg(message: Message, state: FSMContext) -> None:
    """User typed client name — save, transition to confirming.

    Phone step removed: name → confirming directly (was name → phone → confirm).
    _render_summary_and_set_confirming renders summary + confirm_keyboard.

    Defensive: if workday_id/slot_id missing in FSM (state corruption) →
    state.clear + retry hint.
    """
    name = message.text.strip() if message.text else ""
    if not name:
        await message.answer("Имя не может быть пустым. Введите имя:")
        return
    if len(name) > 255:
        await message.answer("Имя слишком длинное (макс. 255 символов). Введите короче:")
        return

    await state.update_data(client_name=name)

    data = await state.get_data()
    service_title = data.get("service_title")
    if not service_title:
        await state.clear()
        await message.answer("❌ Данные потеряны. Начните заново через /book или /slots")
        return

    await _render_summary_and_set_confirming(message, state, name)


# ============================================================
# 5b. _render_summary_and_set_confirming — shared helper (B.10)
# ============================================================
async def _render_summary_and_set_confirming(
    message: Message,
    state: FSMContext,
    client_name: str,
) -> bool:
    """Render booking summary + set state to confirming. Returns True on success.

    Called by name_msg and name_pre_fill_yes_cb after name is set — renders
    summary + confirm_keyboard and transitions to confirming.

    Reads workday_id / slot_id / service_title from state. Branches on path
    (workday / legacy slot) and builds summary via _format_booking_summary
    or _format_booking_summary_from_start_at. Sets state to confirming and
    sends summary + confirm_keyboard via message.answer.

    Defensive: if service_title / slot_id / workday_id missing (state
    corruption — e.g. Redis
    flush mid-flow), state.clear + retry hint. Returns False — caller
    should NOT proceed further (e.g. skip callback.answer() — already
    answered by retry hint).

    Args:
        message: aiogram Message to answer on (text-input path) OR
            callback.message (callback path). Caller MUST ensure this is a
            real Message (not InaccessibleMessage) — InaccessibleMessage has
            no .answer() method. name_pre_fill_yes_cb checks
            `if callback.message is not None` before calling; production
            path assumes Message. If Telegram ever delivers
            InaccessibleMessage, .answer() raises AttributeError — caller
            should add isinstance check if that becomes a real edge case.
        state: FSM context — read workday_id/slot_id/service_title, set
            state to confirming.
        client_name: client name from state (already validated non-empty
            by name_msg / pre_fill_yes_cb upstream).

    Returns:
        True — summary rendered, state set to confirming, message sent.
        False — state corrupted, defensive clear + retry hint sent, caller
        should bail out (no further answer).
    """
    data = await state.get_data()
    settings = get_settings()
    workday_id_str = data.get("workday_id")
    slot_id_str = data.get("slot_id")
    service_title = data.get("service_title")
    if not service_title:
        await state.clear()
        await message.answer("❌ Данные потеряны. Начните заново через /book или /slots")
        return False

    async with async_session_factory() as session:
        if workday_id_str is not None:
            # === /slots workday path (Этап 5.8b) ===
            start_minute = data.get("start_minute")
            if (
                start_minute is None
                or not isinstance(start_minute, int)
                or not (0 <= start_minute <= 1439)
            ):
                await state.clear()
                await message.answer("❌ Ошибка времени. Начните заново через /slots")
                return False
            stmt = select(WorkDay).where(WorkDay.id == UUID(workday_id_str))
            workday = (await session.execute(stmt)).scalar_one_or_none()
            if workday is None:
                await state.clear()
                await message.answer("❌ Рабочий день не найден. Начните заново через /slots")
                return False
            start_time_local = dt_time(start_minute // 60, start_minute % 60)
            start_at = _build_start_at_from_workday(workday, start_time_local, settings.TIMEZONE)
            summary = _format_booking_summary_from_start_at(
                start_at=start_at,
                client_name=client_name,
                service_title=service_title,
                business_timezone=settings.TIMEZONE,
            )
        elif slot_id_str:
            # === /book legacy slot path ===
            slot_stmt = select(Slot).where(Slot.id == UUID(slot_id_str))
            slot = (await session.execute(slot_stmt)).scalar_one_or_none()
            if slot is None:
                await state.clear()
                await message.answer("❌ Слот не найден. Начните заново через /book")
                return False
            from bot.keyboards.client import _format_booking_summary  # noqa: PLC0415

            summary = _format_booking_summary(
                slot=slot,
                client_name=client_name,
                service_title=service_title,
                business_timezone=settings.TIMEZONE,
            )
        else:
            # Neither workday_id nor slot_id — state corruption.
            await state.clear()
            await message.answer("❌ Данные потеряны. Начните заново через /book или /slots")
            return False

    await state.set_state(BookingStates.confirming)
    sent = await message.answer(
        f"Подтвердите запись:\n\n{summary}",
        reply_markup=confirm_keyboard(),
    )
    # Review W3 (5.51): remember the summary message so cancel_msg (/cancel)
    # can strip its ✅/❌ keyboard. cancel_msg is a TEXT handler — it cannot
    # edit another message implicitly, only via bot.edit_message_reply_markup
    # with this id. Without it, /cancel from confirming leaves a live-looking
    # confirm keyboard (dead buttons — the exact bug class of this session).
    await state.update_data(summary_msg_id=sent.message_id)
    return True


# ============================================================
# 4b. service_picker_cb — user tapped a service button (Session 5.27 FEAT)
# ============================================================
@router.callback_query(
    BookServiceCallbackData.filter(),
    StateFilter(BookingStates.entering_service),
)
async def service_picker_cb(
    callback: CallbackQuery,
    callback_data: BookServiceCallbackData,
    state: FSMContext,
) -> None:
    """User tapped a service from the inline picker — save service_id +
    service_title (= Service.name from DB), fetch slots filtered by
    service.duration_minutes, transition to selecting_slot, render slot picker.

    Session 5.29 (Task 2 — FSM reorder): moved from "service → confirming"
    to "service → selecting_slot". Slot fetching uses the REAL service
    duration (not SERVICE_DEFAULT_DURATION_MIN) so the overlap filter
    (slots.py:219 fix) hides slots that would collide with existing bookings
    of any duration (fixes 15:30+Стрижка vs 16:00-18:00-Окрашивание bug).

    Defensive: re-SELECT Service by id (callback_data could be stale/tampered
    — the inline keyboard was built from a DB snapshot at _process_selected_date
    time, but master could have archived the service between picker render
    and tap). Session 5.51: service deleted/archived → re-render a FRESH
    service picker from DB (free-text fallback removed — unknown duration
    corrupts the slot grid). If no services remain → state.clear + hint.

    Defensive: if selected_date missing in FSM (state corruption after bot
    restart with MemoryStorage) → state.clear + retry hint (NEW edge case
    from critic finding 4, Session 5.29).

    Session 5.51: strips the tapped service-picker keyboard (every branch
    advances the flow or ends it).
    """
    await _clear_source_keyboard(callback)
    settings = get_settings()
    fsm_data = await state.get_data()
    selected_date_str: str | None = fsm_data.get("selected_date")
    is_slots_path: bool | None = fsm_data.get("is_slots_path")
    if not selected_date_str:
        # State corruption: entering_service without selected_date means
        # _process_selected_date never ran (FSM persisted across restart,
        # bot was killed mid-flow, MemoryStorage lost data). Cannot fetch
        # slots without a date — abort cleanly.
        await state.clear()
        if callback.message is not None:
            await callback.message.answer(
                "❌ Данные потеряны. Начните заново через /book или /slots"
            )
        await callback.answer()
        return

    try:
        slot_date = date.fromisoformat(selected_date_str)
    except ValueError:
        await state.clear()
        if callback.message is not None:
            await callback.message.answer("❌ Ошибка даты. Начните заново через /book или /slots")
        await callback.answer()
        return

    async with async_session_factory() as session:
        from bot.models import Business, Service  # noqa: PLC0415

        master = await _select_master(session, settings)
        if master is None:
            await state.clear()
            if callback.message is not None:
                await callback.message.answer(
                    "❌ Не удалось найти мастера. Обратитесь к администратору."
                )
            await callback.answer()
            return

        stmt = select(Service).where(Service.id == callback_data.service_id)
        service = (await session.execute(stmt)).scalar_one_or_none()
        if service is None or not service.is_active:
            # Service was archived/deleted between picker render and tap.
            # Session 5.51: re-render a FRESH service picker from DB (no
            # free-text fallback). If nothing remains → booking impossible.
            stmt_b = select(Business).where(Business.id == master.business_id).limit(1)
            business = (await session.execute(stmt_b)).scalar_one_or_none()
            if business is None:
                services = []
            else:
                stmt_s = (
                    select(Service)
                    .where(Service.business_id == business.id, Service.is_active == True)  # noqa: E712
                    .order_by(Service.name)
                )
                services = list((await session.execute(stmt_s)).scalars().all())
            if not services:
                await state.clear()
                if callback.message is not None:
                    await callback.message.answer(
                        "Мастер пока не настроил услуги. Загляните позже 🙏"
                    )
                await callback.answer()
                return
            if callback.message is not None:
                await callback.message.answer(
                    "Эта услуга больше недоступна. Выберите другую:",
                    reply_markup=service_picker_keyboard(services),
                )
            await callback.answer()
            return

        await state.update_data(
            service_id=str(service.id),
            service_title=service.name,
        )

        keyboard = await _fetch_slot_picker_for_service(
            session,
            master,
            slot_date,
            settings,
            is_slots_path=bool(is_slots_path),
            min_duration_min=service.duration_minutes,
        )
        if keyboard is None:
            # No slots for this service duration on this date — retry date picker.
            # Stay in entering_service is wrong (no slots); roll back to
            # selecting_date so user picks another date. Clear service_id/title
            # too — the new date fetch will render a fresh service picker.
            await state.update_data(
                service_id=None,
                service_title=None,
                selected_date=None,
            )
            await state.set_state(BookingStates.selecting_date)
            if callback.message is not None:
                await callback.message.answer(
                    f"На эту дату нет окна под услугу «{service.name}» "
                    f"({service.duration_minutes} мин). Выберите другую дату:",
                    reply_markup=await _retry_markup(
                        session,
                        master,
                        settings,
                        is_transfer=False,
                        is_slots_path=is_slots_path,
                    ),
                )
            await callback.answer()
            return

        await state.set_state(BookingStates.selecting_slot)
        if callback.message is not None:
            await callback.message.answer(
                "Выберите время:",
                reply_markup=keyboard,
            )
    await callback.answer()


# ============================================================
# 4c. book_back_to_date_cb — user tapped '↩️ Назад' in service picker
# ============================================================
@router.callback_query(F.data == "book_back_to_date", StateFilter(BookingStates.entering_service))
async def book_back_to_date_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Back from service picker to date picker (Session 5.30 S1).

    Lets user change date without /cancel + /book restart. Re-renders
    date_picker_keyboard via _retry_markup (same path as race-retry). Keeps
    is_slots_path from FSM data (consistent /book vs /slots semantics).

    State transitions: entering_service → selecting_date.
    Defensive: master not found → state.clear + retry hint (race with
    business config change between render and back-tap).

    Session 5.51: strips the tapped service-picker keyboard.
    """
    await _clear_source_keyboard(callback)
    fsm_data = await state.get_data()
    is_slots_path: bool | None = fsm_data.get("is_slots_path")
    settings = get_settings()
    async with async_session_factory() as session:
        master = await _select_master(session, settings)
        if master is None:
            await state.clear()
            if callback.message is not None:
                await callback.message.answer("❌ Мастер не найден. /book чтобы начать")
            await callback.answer()
            return
        markup = await _retry_markup(
            session,
            master,
            settings,
            is_transfer=False,
            is_slots_path=is_slots_path,
        )
    await state.set_state(BookingStates.selecting_date)
    if callback.message is not None:
        await callback.message.answer("📅 Выберите дату записи:", reply_markup=markup)
    await callback.answer()


# ============================================================
# 5. service_msg — user typed text while service picker is on screen
# ============================================================
@router.message(StateFilter(BookingStates.entering_service))
async def service_msg(message: Message) -> None:
    """Free-text service input is DISABLED (Session 5.51).

    Rationale: a free-text service has no duration in DB → booking silently
    used SERVICE_DEFAULT_DURATION_MIN (60) → the calendar and master's
    /today /week showed a window that didn't match the real job length.
    Services are tap-only: the client picks from the master's own list,
    where every option has a known duration_minutes.

    This handler exists so a typed text doesn't silently drop: aiogram
    answers with a hint to use the picker. The service-picker message (with
    its buttons) is still in the chat above this text.
    """
    await message.answer("Пожалуйста, выберите услугу кнопкой 👇")


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

    Session 5.51: strips the ✅/❌ keyboard from the summary message — after
    booking (or any terminal error below) those buttons are dead; the client
    gets the always-on reply keyboard back instead of the removed
    post_booking_keyboard (it duplicated [Записаться]/[Мои записи]).
    """
    # W3 (code-review iter 2): early guard — callback.from_user is None in
    # inaccessible-message edge cases. Mirrors mybookings_cancel_cb /
    # mybookings_transfer_cb / transfer_slot_30_cb pattern. Without this,
    # line ~1851 `callback.from_user.id` would raise AttributeError.
    if callback.from_user is None:
        await callback.answer()
        return
    await _clear_source_keyboard(callback)
    data = await state.get_data()
    slot_id_str = data.get("slot_id")
    workday_id_str = data.get("workday_id")
    start_minute = data.get("start_minute")
    client_name = data.get("client_name")
    service_title = data.get("service_title")
    service_id_str = data.get("service_id")  # Session 5.27: tap-to-select path
    # @username from Telegram profile — master taps it to contact client.
    # None if user has no @username (notification shows telegram_id fallback).
    # W3: callback.from_user guaranteed non-None by early guard above.
    telegram_username = callback.from_user.username

    # XOR contract with service_msg: slot_id (legacy /book) XOR
    # (workday_id + start_minute) (workday /slots). Both branches require
    # client_name + service_title to be set by name_msg + service_msg.
    has_slot_path = slot_id_str is not None
    has_workday_path = workday_id_str is not None and start_minute is not None
    if not client_name or not service_title or not (has_slot_path ^ has_workday_path):
        # state.clear() BEFORE answer (race condition, MY-VIBE-RULES.md 24)
        # Этап 5.8b W3 (code-review iter 2): retry-cmd branching для согласованности
        # с SlotAlreadyBooked/SlotInPast except'ами ниже (lines 615, 627). Если
        # state corrupted с workday_id set (FSM from Redis after upgrade) —
        # user был в /slots flow, hint должен быть /slots.
        retry_cmd = "/slots" if has_workday_path else "/book"
        await state.clear()
        if callback.message is not None:
            await callback.message.answer(f"❌ Данные потеряны. Начните заново через {retry_cmd}")
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

        # Type narrowing: guaranteed by earlier XOR + presence check
        assert client_name is not None
        assert service_title is not None

        # Note: client_id intentionally NOT in BookingCreate — service resolves
        # client by telegram_id via _select_or_create_client(telegram_id).
        if has_workday_path:
            # === /slots workday path (Этап 5.8b) ===
            # start_minute range 0-1439 guaranteed by slot_30_cb, but defensively
            # re-check here too — same rationale as service_msg: corrupted FSM
            # storage across upgrade would otherwise yield a wrong BookingCreate.
            if not isinstance(start_minute, int) or not (0 <= start_minute <= 1439):
                await state.clear()
                if callback.message is not None:
                    await callback.message.answer("❌ Ошибка времени. Начните заново через /slots")
                await callback.answer()
                return
            payload = BookingCreate(
                workday_id=UUID(workday_id_str),
                start_time_local=dt_time(start_minute // 60, start_minute % 60),
                client_name=client_name,
                service_title=service_title,
                service_id=UUID(service_id_str) if service_id_str else None,
                telegram_username=telegram_username,
            )
        else:
            # === /book legacy slot path ===
            assert slot_id_str is not None  # type narrowing for mypy
            payload = BookingCreate(
                slot_id=UUID(slot_id_str),
                client_name=client_name,
                service_title=service_title,
                service_id=UUID(service_id_str) if service_id_str else None,
                telegram_username=telegram_username,
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
                # Этап 5.8b W3: SlotAlreadyBookedError is now reachable on
                # workday-path too (booking.py IntegrityError remap). Direct
                # /slots users to /slots, /book users to /book.
                retry_cmd = "/slots" if has_workday_path else "/book"
                await callback.message.answer(
                    f"😔 Слот только что заняли. Начните заново через {retry_cmd}"
                )
            await callback.answer()
            return
        except SlotInPastError:
            await state.clear()
            if callback.message is not None:
                # Этап 5.8b W3: SlotInPastError reachable on workday-path
                # (booking.py:489-492 raise if start_at <= now). User waited
                # >30 min before ✅ on a workday slot — direct to /slots.
                retry_cmd = "/slots" if has_workday_path else "/book"
                await callback.message.answer(
                    f"❌ Это время уже прошло. Выберите другое через {retry_cmd}"
                )
            await callback.answer()
            return
        except SlotClosedError:
            await state.clear()
            if callback.message is not None:
                # SlotClosedError is slot-only (workday-path uses is_active
                # → BookingOutsideWorkDayError). Message stays /book.
                await callback.message.answer(
                    "❌ Слот закрыт мастером. Выберите другой через /book"
                )
            await callback.answer()
            return
        except BookingOutsideWorkDayError:
            # Этап 5.8b: workday-path race — between service_msg (summary shown)
            # and confirm_cb (✅ tapped) the master either closed the day via
            # /closeday (is_active=False) or the WorkDay record was deleted.
            # Critic iter 2 P0: confirm_cb previously did NOT catch this —
            # the race leaked through as a 500 to the user.
            await state.clear()
            if callback.message is not None:
                await callback.message.answer(
                    "❌ День закрыт мастером. Выберите другую дату через /slots"
                )
            await callback.answer()
            return
        except WorkDayCapacityExceededError:
            # Этап 5.8b: another booking grabbed the same 30-min window
            # between service_msg and confirm_cb. Service-side capacity check
            # (overlapping active bookings >= capacity) raised.
            await state.clear()
            if callback.message is not None:
                await callback.message.answer(
                    "😔 Это время только что заняли. Начните заново через /slots"
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
        # Session 5.51: no inline post_booking_keyboard — the always-on
        # reply keyboard (restored right below) covers both actions
        # ([💇 Записаться] / [📋 Мои записи]). Duplicated inline buttons
        # confused clients ("extra buttons that don't work").
        await callback.message.answer(
            "✅ Вы записаны. Напомню за 24ч и за 1ч.",
        )
        # Session 5.36 (B.13): restore client reply keyboard after booking.
        # ReplyKeyboardRemove was sent in slot_cb/slot_30_cb; now that the
        # booking is confirmed and state cleared, bring the always-on reply
        # keyboard back. Skipped for master (they have admin_inline_menu).
        # Guard: callback.message is Message | InaccessibleMessage — only
        # Message has .answer(), InaccessibleMessage is for old channel posts.
        if isinstance(callback.message, Message):
            await _restore_reply_keyboard_async(callback.message)
    await callback.answer()


# ============================================================
# 6a. cancel_flow_cb — user tapped ❌ Отмена in the confirm keyboard
#     (Session 5.51: handler was MISSING — dead button since 5.27)
# ============================================================
@router.callback_query(BookCancelCallbackData.filter(), StateFilter("*"))
async def cancel_flow_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Cancel the booking flow from the ✅/❌ confirm keyboard.

    Bug fix (Session 5.51): the ❌ Отмена button (BookCancelCallbackData,
    keyboards/client.py confirm_keyboard) had NO handler since Session 5.27 —
    tapping it silently dropped (aiogram logged "callback query not
    answered"). Now it clears FSM state like /cancel does.

    StateFilter("*"): the button only appears on the confirming summary,
    but a STALE ❌ (flow restarted via /book, state now selecting_date)
    must also work — "Отмена" means "stop whatever flow I'm in".

    Mirrors cancel_msg: /book vs /slots hint from is_slots_path (read
    BEFORE state.clear), strip the tapped keyboard, restore the reply
    keyboard.
    """
    fsm_data = await state.get_data()
    is_slots_path: bool | None = fsm_data.get("is_slots_path")
    # Review W2 (5.51): transfer flow ALSO sets is_slots_path=True (B.1), so
    # the /slots hint below would misdirect a user who tapped stale ❌ while
    # transferring a booking from /mybookings. Branch on transfer_booking_id
    # (read BEFORE state.clear) — after cancelling a transfer the user should
    # go back to their bookings list, not start a fresh booking.
    transfer_booking_id: str | None = fsm_data.get("transfer_booking_id")
    # state.clear() BEFORE answer (race condition, MY-VIBE-RULES.md 24)
    await state.clear()
    await _clear_source_keyboard(callback)
    if transfer_booking_id is not None:
        hint = "Перенос отменён. /mybookings чтобы вернуться к записям"
    elif is_slots_path:
        hint = "Ввод отменён. /slots чтобы начать заново"
    else:
        hint = "Ввод отменён. /book чтобы начать заново"
    if isinstance(callback.message, Message):
        await callback.message.answer(hint)
        await _restore_reply_keyboard_async(callback.message)
    await callback.answer()


# ============================================================
# 7. cancel_msg — /cancel inside FSM (StateFilter("*"))
# ============================================================
@router.message(Command("cancel"), StateFilter("*"))
async def cancel_msg(message: Message, state: FSMContext) -> None:
    """Cancel booking flow — clears FSM state (works in any state).

    Registered BEFORE /mybookings handler (spec.md 491) — /cancel from /mybookings
    will be added in Урок 2.5 with StateFilter(None).

    Этап 5.8b W2 (code-review iter 2): /slots vs /book hint branching. Read
    is_slots_path ДО state.clear() — после clear флаг потерян. StateFilter("*")
    ловит cancel из любого state, включая /slots entering_name/entering_service.
    """
    # Read is_slots_path ДО state.clear() (race-condition pattern preserves).
    fsm_data_cancel = await state.get_data()
    is_slots_path_cancel: bool | None = fsm_data_cancel.get("is_slots_path")
    # Review W2 (5.51): transfer sets is_slots_path=True too — branch on the
    # actual transfer marker so a cancelled transfer points back to
    # /mybookings (same as the cancel_flow_cb stale-❌ path below).
    transfer_booking_id: str | None = fsm_data_cancel.get("transfer_booking_id")
    # Review W3 (5.51): /cancel from confirming must strip the ✅/❌ keyboard
    # from the summary message — read its id BEFORE state.clear wipes it.
    summary_msg_id: int | None = fsm_data_cancel.get("summary_msg_id")
    # state.clear() BEFORE answer (race condition, MY-VIBE-RULES.md 24)
    await state.clear()
    if summary_msg_id is not None and message.bot is not None:
        with suppress(TelegramBadRequest):
            await message.bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=summary_msg_id,
                reply_markup=None,
            )
    if transfer_booking_id is not None:
        hint = "Перенос отменён. /mybookings чтобы вернуться к записям"
    elif is_slots_path_cancel:
        hint = "Ввод отменён. /slots чтобы начать заново"
    else:
        hint = "Ввод отменён. /book чтобы начать заново"
    await message.answer(hint)
    # Session 5.36 (B.13): restore client reply keyboard after /cancel.
    # ReplyKeyboardRemove was sent in slot_cb/slot_30_cb when entering the
    # booking flow — now that the user cancelled, bring the always-on reply
    # keyboard back so they can tap [💇 Записаться] / [📋 Мои записи] again.
    # Skipped for master (admin_inline_menu is master's UI, not reply keyboard).
    await _restore_reply_keyboard_async(message)


# ============================================================
# 8. mybookings_msg — /mybookings (StateFilter(None)) — list client bookings
# ============================================================
# 8a. _render_mybookings — shared renderer (Session 6 — Task 1, 2026-09-06)
# ============================================================
async def _render_mybookings(message: Message, user_id: int) -> None:
    """Render the user's upcoming bookings list + inline [Отменить]/[Перенести]
    buttons for cancelable bookings (start_at - CANCEL_MIN_HOURS > now).

    Extracted from mybookings_msg (Session 6 — Task 1) so the new
    client_mybookings_cb handler (post-booking [📋 Мои записи] tap) can re-use
    the exact same rendering path as /mybookings — single source of truth,
    no drift between "list from command" and "list from inline button".

    Args:
        message: the aiogram Message to answer on. For /mybookings this is
            the user's /mybookings text message; for client_mybookings_cb
            this is the callback.message the inline button was attached to.
            Both expose .answer(text, reply_markup=...) — same API.
        user_id: telegram user id to resolve the Client row. Caller passes
            message.from_user.id (command handler) OR callback.from_user.id
            (callback handler) — NOT message.from_user.id, because in a
            callback context message.from_user is the BOT, not the user who
            tapped (would yield None / wrong user → no bookings shown).

    Empty cases (no Client row, no bookings) answer a hint with /book —
    consistent with pre-extract behavior (test_mybookings_msg_no_*).
    """
    from zoneinfo import ZoneInfo

    from sqlalchemy import select

    from bot.config import get_settings
    from bot.models import Business, Client
    from bot.services.admin import get_client_bookings

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
# 8b. mybookings_msg — /mybookings command (delegates to _render_mybookings)
# ============================================================
@router.message(Command("mybookings"), StateFilter(None))
async def mybookings_msg(message: Message) -> None:
    """List confirmed/transferred upcoming bookings for the current user.

    Spec.md 41: `/mybookings` → отмена (>24ч) или перенос (>24ч).
    This handler is a thin entry point — rendering lives in _render_mybookings
    (shared with client_mybookings_cb since Session 6 — Task 1). Cancellation
    itself is performed by mybookings_cancel_cb (next handler).

    Resolution: client by telegram_id (booking.py pattern, _select_or_create_client).
    Filter: upcoming (start_at > now UTC), status IN (confirmed, transferred).
    """
    if message.from_user is None:
        return
    await _render_mybookings(message, message.from_user.id)


# ============================================================
# 8c. client_mybookings_cb — [📋 Мои записи] tap from post-booking keyboard
# ============================================================
@router.callback_query(ClientMenuMyBookingsCallbackData.filter(), StateFilter(None))
async def client_mybookings_cb(
    callback: CallbackQuery,
    callback_data: ClientMenuMyBookingsCallbackData,
) -> None:
    """Client tapped [📋 Мои записи] in the post-booking keyboard (Session 6 — Task 1).

    Re-uses _render_mybookings (same code path as /mybookings) so the list
    rendered from the inline button is byte-identical to the one rendered
    from the text command — single source of truth, no drift.

    StateFilter(None): the post-booking keyboard is shown after confirm_cb
    succeeded and state.clear() ran, so the client is in State(None) when
    they tap. If they re-enter FSM via /book and a stale post-booking button
    is tapped mid-flow, NEITHER this handler NOR no_state_callback_fallback
    matches (both use StateFilter(None) — the catch-all at the bottom of
    this router is also State(None)-scoped). The callback silently drops
    (aiogram logs "callback query not answered"); consistent with all other
    StateFilter(None) callback handlers (mybookings_cancel_cb,
    mybookings_transfer_cb). To avoid the silent drop, the client would
    need to /cancel first to reach State(None) — same UX as before Task 1.

    `callback_data` is required by aiogram dispatch (CallbackData.filter()
    injects the unpacked payload) — the dataclass itself carries no fields,
    but the parameter must be declared for aiogram to populate it. Same
    pattern as confirm_cb / mybookings_cancel_cb.

    callback.answer() closes the Telegram loading spinner on the inline
    button (UX contract — every callback handler must answer). Empty
    bookings case: helper already answers "У вас нет активных записей",
    callback.answer still runs to clear the spinner.

    Session 5.51: strips the tapped stale post-booking menu keyboard.
    """
    await _clear_source_keyboard(callback)
    if callback.from_user is None:
        await callback.answer()
        return
    # Type narrowing: callback.message is `Message | InaccessibleMessage` in
    # aiogram 3.x — only Message has .answer(). InaccessibleMessage covers the
    # "inaccessible message" edge case (channel posts older than 48h, etc) —
    # _render_mybookings needs a real Message to call .answer() on. Same guard
    # pattern as confirm_cb / mybookings_cancel_cb above.
    if isinstance(callback.message, Message):
        await _render_mybookings(callback.message, callback.from_user.id)
    await callback.answer()


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

        # Session 5.51: cancel succeeded → the /mybookings list (with its
        # [❌ Отменить]/[🔄 Перенести] buttons) is stale — strip its keyboard.
        # Early error popups above keep the keyboard (other bookings' buttons
        # are still valid, user can tap another one).
        await _clear_source_keyboard(callback)

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

    # Session 5.51: validation passed → transfer FSM starts, the /mybookings
    # list message (with its [❌]/[🔄] buttons) goes stale — strip it. Early
    # popups above keep the keyboard (other bookings remain actionable).
    await _clear_source_keyboard(callback)

    # Save booking_id + is_slots_path in FSM (B.1: is_slots_path=True so
    # _process_selected_date enters workday branch (line 684) for transfer flow
    # → renders BookSlot30CallbackData picker instead of legacy slot picker).
    # state.set_state AFTER save — order is safe because state.update_data
    # doesn't trigger handlers, set_state does).
    await state.update_data(
        transfer_booking_id=str(callback_data.booking_id),
        is_slots_path=True,
    )
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
        next_state=TransferStates.selecting_slot,
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

    Session 5.51: strips the tapped slot-picker keyboard (every branch
    terminates the transfer flow — success or terminal error).
    """
    from sqlalchemy import select

    from bot.models import Client

    if callback.from_user is None:
        await callback.answer()
        return
    await _clear_source_keyboard(callback)

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
        except BookingOutsideWorkDayError:
            if callback.message is not None:
                await callback.message.answer(
                    "❌ Время вне рабочего дня мастера. Выберите другое время"
                )
            await callback.answer()
            return
        except WorkDayCapacityExceededError:
            if callback.message is not None:
                await callback.message.answer("❌ Нет мест на это время. Выберите другое")
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
# 13b. transfer_slot_30_cb — user picked a 30-min WorkDay slot → call transfer_booking (B.1)
# ============================================================
@router.callback_query(BookSlot30CallbackData.filter(), StateFilter(TransferStates.selecting_slot))
async def transfer_slot_30_cb(
    callback: CallbackQuery,
    callback_data: BookSlot30CallbackData,
    state: FSMContext,
    scheduler: AsyncIOScheduler,
) -> None:
    """User selected a 30-min WorkDay slot for transfer — call transfer_booking workday-path.

    B.1 workday-path: converts BookSlot30CallbackData (workday_id + start_minute)
    to transfer_booking(new_workday_id=..., new_start_time_local=dt_time(...)).

    start_minute → dt_time conversion: dt_time(start_minute // 60, start_minute % 60)
    (mirror slot_30_cb:1991, admin_move_confirm_cb:2632).

    Error mapping (mirror transfer_slot_cb + workday-specific exceptions):
      BookingNotFoundError            → "Запись не найдена"
      BookingAlreadyCancelledError    → "Запись уже отменена"
      CancelTooLateError              → "❌ Перенос возможен только за 24+ часов"
      BookingAlreadyTransferredError  → "❌ Запись уже перенесена (конкурентный запрос)"
      SlotAlreadyBookedError           → "😔 Это время только что заняли"
      SlotInPastError                 → "❌ Это время уже прошло"
      WorkDayNotFoundError            → "❌ Этот день не найден"
      WorkDayInactiveError            → "❌ День закрыт мастером"
      BookingOutsideWorkDayError      → "❌ Время вне рабочего дня мастера"
      WorkDayCapacityExceededError    → "❌ Нет мест на это время"

    `scheduler` injected from dp["scheduler"] workflow_data (same as transfer_slot_cb).
    state.clear() BEFORE service call (race condition, same as transfer_slot_cb).

    Session 5.51: strips the tapped slot-picker keyboard (every branch
    terminates the transfer flow).
    """
    from datetime import time as dt_time

    from sqlalchemy import select

    from bot.models import Client
    from bot.services.booking import (
        BookingAlreadyCancelledError,
        BookingAlreadyTransferredError,
        BookingNotFoundError,
        BookingOutsideWorkDayError,
        CancelTooLateError,
        SlotAlreadyBookedError,
        SlotInPastError,
        WorkDayCapacityExceededError,
        WorkDayInactiveError,
        WorkDayNotFoundError,
    )

    if callback.from_user is None:
        await callback.answer()
        return
    await _clear_source_keyboard(callback)

    # Defensive range check (mirror slot_30_cb:1127).
    start_minute = callback_data.start_minute
    if not (0 <= start_minute <= 1439):
        await state.clear()
        if callback.message is not None:
            await callback.message.answer("❌ Ошибка выбора времени. /mybookings чтобы начать")
        await callback.answer()
        return

    data = await state.get_data()
    booking_id_str = data.get("transfer_booking_id")
    if not booking_id_str:
        await state.clear()
        if callback.message is not None:
            await callback.message.answer("❌ Данные потеряны. /mybookings чтобы начать")
        await callback.answer()
        return

    settings = get_settings()
    async with async_session_factory() as session:
        stmt_c = select(Client).where(Client.telegram_id == callback.from_user.id)
        client = (await session.execute(stmt_c)).scalar_one_or_none()
        if client is None:
            await callback.answer("У вас нет записей")
            return

        # state.clear() BEFORE service call (race condition).
        await state.clear()
        new_start_time_local = dt_time(start_minute // 60, start_minute % 60)
        try:
            result: TransferResult = await transfer_booking(
                session,
                UUID(booking_id_str),
                None,  # new_slot_id — None for workday path
                client.id,
                scheduler,
                new_workday_id=callback_data.workday_id,
                new_start_time_local=new_start_time_local,
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
                    "😔 Это время только что заняли. /mybookings чтобы выбрать другое"
                )
            await callback.answer()
            return
        except SlotInPastError:
            if callback.message is not None:
                await callback.message.answer("❌ Это время уже прошло.")
            await callback.answer()
            return
        except WorkDayNotFoundError:
            if callback.message is not None:
                await callback.message.answer("❌ Этот день не найден. Выберите другую дату")
            await callback.answer()
            return
        except WorkDayInactiveError:
            if callback.message is not None:
                await callback.message.answer("❌ День закрыт мастером. Выберите другую дату")
            await callback.answer()
            return
        except BookingOutsideWorkDayError:
            if callback.message is not None:
                await callback.message.answer(
                    "❌ Время вне рабочего дня мастера. Выберите другое время"
                )
            await callback.answer()
            return
        except WorkDayCapacityExceededError:
            if callback.message is not None:
                await callback.message.answer("❌ Нет мест на это время. Выберите другое")
            await callback.answer()
            return

        # Send master notification (Pure/IO — service prepared text, handler sends).
        if callback.bot is not None:
            await callback.bot.send_message(
                chat_id=settings.ADMIN_ID,
                text=result.master_notification_text,
            )

    if callback.message is not None:
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

    Session 5.51 (review W1): strip the tapped keyboard. A callback that
    lands here by definition has no live handler in the current state — the
    keyboard is dead; without the strip every re-tap repeats the alert
    forever. Same _clear_source_keyboard pattern as every flow step.
    """
    await _clear_source_keyboard(callback)
    await callback.answer("Сессия истекла — начните через /book", show_alert=True)
