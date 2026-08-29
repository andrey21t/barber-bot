"""Master/admin commands + inline-menu callbacks — Pure I/O layer.

Contract (spec.md 200-213, 251, 307-309):
- Auth: F.from_user.id == settings.ADMIN_ID (MVP single-master). TODO Ур. 2.6:
  extract to middleware/role.py (spec 258) — DB lookup current_business, current_master.
  _is_admin — для Message, _is_admin_callback — для CallbackQuery (callback.from_user).
- Stateful: YES — 5 command handlers (/addslots /closeslot /today /week /services)
  run StateFilter(None) (no FSM). 5 inline-menu callbacks (Этап 1.3a) — StateFilter("*"),
  стартуют FSM-потоки через state.set_state(AdminStates.*). FSM addslots (1.3b) —
  calendar handler + hours message handler, переход calendar→hours через edit_message_text
  (INL-001). FSM closeslot (1.3c) — TODO. FSM services (1.3d) — TODO.
- 5 command handlers + 5 inline-menu callbacks + 2 addslots FSM handlers (1.3b).
- Pure I/O: parse args, call service, render result. NO business logic here.
- HTML escape: client_name_snapshot already escaped in DB (booking.py), render
  WITHOUT re-escape (Telegram parse_mode=HTML). Newlines in snapshot are
  replaced with space in _render_bookings (html.escape(quote=False) skips \n).
- Timezone: today/week use business.timezone (from DB), NOT UTC. slot.slot_date is LOCAL.
- slot_date validation: /addslots rejects past dates (spec.md 401, slot_date >= today_local).
"""

import html
import logging
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback
from aiogram_calendar.schemas import SimpleCalAct
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.exc import SQLAlchemyError

from bot.config import get_settings
from bot.db import async_session_factory
from bot.keyboards.admin import (
    AdminAddslotsCallbackData,
    AdminCloseslotCallbackData,
    AdminMenuCallbackData,
    AdminMoveCallbackData,
    AdminMoveConfirmCallbackData,
    AdminMoveSlot30CallbackData,
    AdminOpendayCallbackData,
    AdminServicesCallbackData,
    AdminTodayCallbackData,
    AdminWeekCallbackData,
    admin_calendar_keyboard,
    admin_inline_menu,
    admin_move_confirm_keyboard,
    admin_today_keyboard,
)
from bot.models import Booking, WorkDay
from bot.services.admin import (
    create_service,
    get_today_bookings,
    get_week_bookings,
)
from bot.services.admin_move import (
    AdminMoveResult,
    WorkDayInactiveError,
    WorkDayNotFoundError,
    admin_move_booking,
)
from bot.services.booking import (
    BookingAlreadyCancelledError,
    BookingAlreadyTransferredError,
    BookingNotFoundError,
    BookingOutsideWorkDayError,
    SlotAlreadyBookedError,
    SlotInPastError,
    WorkDayCapacityExceededError,
)
from bot.services.slots import (
    SlotAlreadyExistsError,
    add_slots,
    close_slot,
    get_available_slots_30,
)
from bot.services.workday import (
    WorkDayShrinkError,
    open_workday,
    select_workday,
)
from bot.states import AdminMoveStates, AdminStates

logger = logging.getLogger(__name__)

router = Router(name="admin")


# ============================================================
# Helpers (DB lookups, kept here for MVP — TODO Ур. 2.6: middleware)
# ============================================================
async def _resolve_master_and_business(telegram_id: int) -> tuple[UUID, UUID, str] | None:
    """Return (master_id, business_id, business_timezone) or None.

    Single-master MVP: master by telegram_id, business via FK.
    TODO Ур. 2.6: move to role middleware, inject into workflow_data.
    """
    from sqlalchemy import select

    from bot.models import Business, Master

    async with async_session_factory() as session:
        stmt_m = select(Master).where(Master.telegram_id == telegram_id).limit(1)
        master = (await session.execute(stmt_m)).scalar_one_or_none()
        if master is None:
            return None
        stmt_b = select(Business).where(Business.id == master.business_id).limit(1)
        business = (await session.execute(stmt_b)).scalar_one_or_none()
        if business is None:
            return None
        return master.id, business.id, business.timezone


def _is_admin(message: Message) -> bool:
    """MVP auth: single-master check against settings.ADMIN_ID.

    TODO Ур. 2.6: replace with middleware/role.py (spec 258).
    """
    settings = get_settings()
    user = message.from_user
    return user is not None and user.id == settings.ADMIN_ID


def _is_admin_callback(callback: CallbackQuery) -> bool:
    """Auth for CallbackQuery — parallel to _is_admin for Message.

    _is_admin проверяет `message.from_user` — для CallbackQuery это БОТ
    (callback.message.from_user = bot), НЕ тапер. Здесь нужен
    `callback.from_user` — кто нажал кнопку.

    TODO Ур. 2.6: заменить на middleware/role.py (spec 258).
    """
    settings = get_settings()
    user = callback.from_user
    return user is not None and user.id == settings.ADMIN_ID


def _require_admin_or_silent(message: Message) -> int | None:
    """Return admin telegram_id if admin, else None (caller returns silently).

    Single helper for handlers — covers _is_admin + user.id narrowing in one call.
    """
    if not _is_admin(message):
        return None
    return message.from_user.id if message.from_user is not None else None


# ============================================================
# Этап 3 (Session 5.9) — /menu command + admin_menu_cb callback
# ============================================================
# /menu — re-show inline menu (13 сообщений в admin.py ссылаются на
# "/menu — заново" — без команды dead-end UX). StateFilter(None) — НЕ ловит
# mid-FSM (там /cancel сначала, потом /menu). admin_menu_cb — для кнопки
# "📋 Меню" в welcome (если будет добавлена в admin_inline_menu() в будущем).
# ============================================================


@router.message(Command("menu"), StateFilter(None))
async def cmd_menu(message: Message) -> None:
    """Show admin inline menu (re-show after actions).

    13 сообщений в admin.py ссылаются на '/menu — заново' (grep) — без
    этой команды dead-end UX. StateFilter(None) — НЕ ловит mid-FSM (там
    /cancel сначала, потом /menu). _is_admin check — non-admin silent.
    """
    if not _is_admin(message):
        return
    await message.answer("📋 Меню:", reply_markup=admin_inline_menu())


@router.callback_query(AdminMenuCallbackData.filter(), StateFilter("*"))
async def admin_menu_cb(callback: CallbackQuery) -> None:
    """Re-show admin inline menu from inline button '📋 Меню'.

    AdminMenuCallbackData (keyboards/admin.py:28) — empty callback для
    кнопки '📋 Меню' в welcome. Кнопка пока НЕ добавлена в admin_inline_menu()
    (5 кнопок по spec.md 251), но callback handler зарегистрирован для
    будущего использования (если пользователь добавит 6-ю кнопку).
    StateFilter("*") — матчит в любом state (включая admin FSM), НЕ чистит
    state (только re-show menu — пользователь может вернуться к flow).
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return
    if callback.message is not None:
        await callback.message.answer("📋 Меню:", reply_markup=admin_inline_menu())
    await callback.answer()


# ============================================================
# 1. /addslots <YYYY-MM-DD> <h1> <h2> ...
# ============================================================
@router.message(Command("addslots"), StateFilter(None))
async def cmd_addslots(message: Message, command: CommandObject) -> None:
    """Open slots on a date for master.

    Parse: `addslots 2026-03-17 11 12 13 14 15`
    Idempotent — skips already-existing slots (service handles).
    """
    if not _is_admin(message):
        return  # silently ignore non-admin

    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer(
            "Формат: <code>/addslots ГГГГ-ММ-ДД ЧАС1 ЧАС2 ...</code>\n"
            "Пример: <code>/addslots 2026-03-17 11 12 13 14</code>"
        )
        return

    iso = args[0]
    try:
        slot_date = date.fromisoformat(iso)
    except ValueError:
        await message.answer("❌ Неверная дата. Формат: ГГГГ-ММ-ДД (например 2026-03-17)")
        return

    # Parse hours + dedup (composite UNIQUE on (master_id, slot_date, slot_hour)
    # will reject duplicate hours in same request if not deduped here).
    try:
        hours_raw = [int(h) for h in args[1:]]
    except ValueError:
        await message.answer("❌ Часы должны быть числами 0-23 (например 11 12 13)")
        return

    hours = sorted(set(hours_raw))  # dedup

    admin_id = _require_admin_or_silent(message)
    assert admin_id is not None
    resolved = await _resolve_master_and_business(admin_id)
    if resolved is None:
        await message.answer("❌ Мастер не найден. Обратитесь к администратору.")
        return
    master_id, _business_id, tz = resolved

    # spec.md 401: slot_date must not be in the past (LOCAL today in business.timezone).
    today_local = datetime.now(ZoneInfo(tz)).date()
    if slot_date < today_local:
        await message.answer(
            f"❌ Нельзя создать слот в прошлом. Сегодня: {today_local.strftime('%d.%m.%Y')}"
        )
        return

    async with async_session_factory() as session:
        try:
            created = await add_slots(session, master_id, slot_date, hours)
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return
        except SlotAlreadyExistsError:
            await message.answer("❌ Один из слотов уже существует (гонка). Попробуйте ещё раз.")
            return

    if created:
        hours_str = ", ".join(str(s.slot_hour) for s in created)
        await message.answer(
            f"✅ Открыты слоты на {slot_date.strftime('%d %B')}:\n<b>{hours_str}</b>"
        )
    else:
        await message.answer(
            f"Все слоты на {slot_date.strftime('%d %B')} уже открыты — ничего не добавлено."
        )


# ============================================================
# 1b. /openday <YYYY-MM-DD> <HH:MM> <HH:MM>  (Этап 5.1, Вариант B)
# ============================================================
# Заменяет /addslots как primary 'open window' action — WorkDay [start_time,
# end_time] вместо per-hour Slot list. Idempotent: повторный /openday на ту
# же дату → update_workday (Gap 6 shrink checks через UNIQUE master/work_date).
# /addslots остаётся deprecated alias до 5.10 (PLANS.md Plan of Work п.11).
# ============================================================
@router.message(Command("openday"), StateFilter(None))
async def cmd_openday(message: Message, command: CommandObject) -> None:
    """Open or update the workday window: `/openday 2026-03-17 11:00 18:00`.

    Idempotent via UNIQUE INDEX ux_work_days_master_date — repeated /openday
    on the same date → update_workday (Gap 6 shrink checks). Shrink with
    active bookings → WorkDayShrinkError (admin cancels them first).

    FSM alternative (calendar → start_time → end_time) is admin_openday_cb
    + admin_openday_calendar_cb + admin_openday_start_msg + admin_openday_end_msg.
    """
    if not _is_admin(message):
        return

    args = (command.args or "").split()
    if len(args) != 3:
        await message.answer(
            "Формат: <code>/openday ГГГГ-ММ-ДД ЧЧ:ММ ЧЧ:ММ</code>\n"
            "Пример: <code>/openday 2026-03-17 11:00 18:00</code>"
        )
        return

    iso = args[0]
    try:
        work_date = date.fromisoformat(iso)
    except ValueError:
        await message.answer("❌ Неверная дата. Формат: ГГГГ-ММ-ДД (например 2026-03-17)")
        return

    try:
        start_time = _parse_hhmm(args[1])
        end_time = _parse_hhmm(args[2])
    except ValueError:
        await message.answer("❌ Время должно быть ЧЧ:ММ (например 11:00 18:00)")
        return

    admin_id = _require_admin_or_silent(message)
    assert admin_id is not None
    resolved = await _resolve_master_and_business(admin_id)
    if resolved is None:
        await message.answer("❌ Мастер не найден. Обратитесь к администратору.")
        return
    master_id, _business_id, tz = resolved

    today_local = datetime.now(ZoneInfo(tz)).date()
    if work_date < today_local:
        await message.answer(
            f"❌ Нельзя открыть день в прошлом. Сегодня: {today_local.strftime('%d.%m.%Y')}"
        )
        return

    async with async_session_factory() as session:
        # F1 fix (Session 5.18, variant B): capture was_closed BEFORE open_workday
        # re-opens the day. After open_workday, workday.is_active is always True →
        # the UX message "день был закрыт — открыт заново" never showed.
        existing = await select_workday(session, master_id, work_date)
        was_closed = existing is not None and not existing.is_active
        try:
            await open_workday(session, master_id, work_date, start_time, end_time, business_tz=tz)
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return
        except WorkDayShrinkError as exc:
            await message.answer(
                f"❌ Нельзя сократить окно — есть активные записи.\n{exc}\n"
                "Сначала отмените записи командой /cancelbooking (или попросите клиентов)."
            )
            return
        except SQLAlchemyError:
            # S2 fix (code-review 5.1): DB-level ошибки — не ValueError, не
            # ловились выше. cmd_openday не в FSM (state.clear() не нужен),
            # но без catch ошибка propagates в aiogram top-level handler →
            # generic error для Екатерины.
            await message.answer("❌ Ошибка БД. Попробуйте позже или через /menu (календарь).")
            return

    await message.answer(
        f"✅ День открыт на {work_date.strftime('%d %B %Y')}:\n"
        f"<b>{start_time.strftime('%H:%M')}–{end_time.strftime('%H:%M')}</b>"
        + ("\n(день был закрыт — открыт заново)" if was_closed else "")
    )


def _parse_hhmm(s: str) -> dt_time:
    """Parse 'HH:MM' → datetime.time. Raises ValueError on bad format.

    Used by cmd_openday (text args) and admin_openday_start_msg / end_msg (FSM
    text input). Centralised parse keeps error messages consistent.
    """
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"expected HH:MM, got {s!r}")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"HH 0-23, MM 0-59, got {s!r}")
    return dt_time(hh, mm)


# ============================================================
# 2. /closeslot <YYYY-MM-DD> <hour>
# ============================================================
@router.message(Command("closeslot"), StateFilter(None))
async def cmd_closeslot(message: Message, command: CommandObject) -> None:
    """Close a slot. Refuses if slot has an active booking.

    Parse: `closeslot 2026-03-17 14`
    """
    if not _is_admin(message):
        return

    args = (command.args or "").split()
    if len(args) != 2:
        await message.answer(
            "Формат: <code>/closeslot ГГГГ-ММ-ДД ЧАС</code>\n"
            "Пример: <code>/closeslot 2026-03-17 14</code>"
        )
        return

    iso = args[0]
    try:
        slot_date = date.fromisoformat(iso)
    except ValueError:
        await message.answer("❌ Неверная дата. Формат: ГГГГ-ММ-ДД")
        return

    try:
        hour = int(args[1])
    except ValueError:
        await message.answer("❌ Час должен быть числом 0-23")
        return
    if not 0 <= hour <= 23:
        await message.answer("❌ Час должен быть 0-23")
        return

    admin_id = _require_admin_or_silent(message)
    assert admin_id is not None
    resolved = await _resolve_master_and_business(admin_id)
    if resolved is None:
        await message.answer("❌ Мастер не найден")
        return
    master_id, _business_id, _tz = resolved

    # Find slot by (master, date, hour) — close_slot service takes slot_id
    from sqlalchemy import select

    from bot.models import Slot

    async with async_session_factory() as session:
        stmt = select(Slot).where(
            Slot.master_id == master_id,
            Slot.slot_date == slot_date,
            Slot.slot_hour == hour,
        )
        slot = (await session.execute(stmt)).scalar_one_or_none()
        if slot is None:
            await message.answer(f"❌ Слот {slot_date} {hour}:00 не найден")
            return

        try:
            updated = await close_slot(session, slot.id)
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return

    if updated:
        await message.answer(f"✅ Слот {slot_date.strftime('%d %B')} {hour}:00 закрыт")
    else:
        await message.answer("Слот уже был закрыт")


# ============================================================
# 3. /today — bookings for today (LOCAL date)
# ============================================================
@router.message(Command("today"), StateFilter(None))
async def cmd_today(message: Message) -> None:
    """List confirmed/transferred bookings for today."""
    admin_id = _require_admin_or_silent(message)
    if admin_id is None:
        return
    resolved = await _resolve_master_and_business(admin_id)
    if resolved is None:
        await message.answer("❌ Мастер не найден")
        return
    master_id, _business_id, tz = resolved

    async with async_session_factory() as session:
        bookings = await get_today_bookings(session, master_id, tz)

    if not bookings:
        await message.answer("На сегодня записей нет.")
        return

    await message.answer(
        _render_bookings("📅 Записи на сегодня:", bookings, tz),
        reply_markup=admin_today_keyboard(bookings, tz),
    )


# ============================================================
# 4. /week — bookings for next 7 days
# ============================================================
@router.message(Command("week"), StateFilter(None))
async def cmd_week(message: Message) -> None:
    """List confirmed/transferred bookings for next 7 days (LOCAL today → today+7)."""
    admin_id = _require_admin_or_silent(message)
    if admin_id is None:
        return
    resolved = await _resolve_master_and_business(admin_id)
    if resolved is None:
        await message.answer("❌ Мастер не найден")
        return
    master_id, _business_id, tz = resolved

    async with async_session_factory() as session:
        bookings = await get_week_bookings(session, master_id, tz, days_ahead=7)

    if not bookings:
        await message.answer("На ближайшую неделю записей нет.")
        return

    await message.answer(_render_bookings("📅 Записи на неделю:", bookings, tz))


# ============================================================
# 5. /services add <name> <duration_min>
# ============================================================
@router.message(Command("services"), StateFilter(None))
async def cmd_services(message: Message, command: CommandObject) -> None:
    """Add a service: `services add Стрижка 60`.

    Note: name with spaces NOT supported in this MVP (no quotes parsing).
    Use single-word name or replace spaces with underscores: "Стрижка_мужская".

    Price убран в Session 5.10 — мастер озвучивает цену отдельно в чате.
    Service.price остаётся nullable в БД для будущего использования.
    """
    if not _is_admin(message):
        return

    args = (command.args or "").split()
    if not args or args[0] != "add":
        await message.answer(
            "Формат: <code>/services add НАЗВАНИЕ ДЛИТЕЛЬНОСТЬ_МИН</code>\n"
            "Пример: <code>/services add Стрижка 60</code>\n"
            "Внимание: название без пробелов (замените на _)"
        )
        return

    if len(args) != 3:
        await message.answer(
            "❌ Нужно 2 параметра: НАЗВАНИЕ ДЛИТЕЛЬНОСТЬ_МИН\n"
            "Пример: <code>/services add Стрижка 60</code>"
        )
        return

    name = args[1].replace("_", " ")
    try:
        duration = int(args[2])
    except ValueError:
        await message.answer("❌ Длительность должна быть числом минут (например 60)")
        return

    if duration <= 0:
        await message.answer("❌ Длительность должна быть > 0")
        return

    admin_id = _require_admin_or_silent(message)
    assert admin_id is not None
    resolved = await _resolve_master_and_business(admin_id)
    if resolved is None:
        await message.answer("❌ Мастер не найден")
        return
    _master_id, business_id, _tz = resolved

    async with async_session_factory() as session:
        try:
            service = await create_service(session, business_id, name, duration)
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return

    await message.answer(
        f"✅ Услуга добавлена:\n"
        f"💇 <b>{html.escape(service.name, quote=False)}</b>\n"
        f"⏱ {service.duration_minutes} мин"
    )


# ============================================================
# Render helper — shared by /today and /week
# ============================================================
def _render_bookings(title: str, bookings: list[Booking], business_timezone: str) -> str:
    """Render bookings list. client_name_snapshot + service_title_snapshot
    are already html.escape()'d in DB — no re-escape needed.

    Newline defense: `html.escape(quote=False)` does NOT strip `\n` (verified
    2026-08-21). A multi-line client_name_snapshot would break list formatting.
    We replace `\n` with space here (display-only, DB stays intact).
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(business_timezone)
    lines = [title, ""]
    for b in bookings:
        # b.start_at: naive on SQLite, aware UTC on Postgres. Inject tzinfo=UTC
        # (no-op on Postgres) before .astimezone — Python interprets naive as
        # system-local TZ otherwise.
        local_time = b.start_at.replace(tzinfo=UTC).astimezone(tz)
        when = local_time.strftime("%d %b, %H:%M")
        # Strip newlines from already-escaped snapshots to preserve list layout
        name = b.client_name_snapshot.replace("\n", " ")
        service = b.service_title_snapshot.replace("\n", " ")
        lines.append(f"• {when} — {name}, {service}")
    return "\n".join(lines)


# ============================================================
# Inline menu callbacks (Вариант B, spec.md 251) — Этап 1.3a
#
# 5 точек входа в flow. StateFilter("*") чтобы поймать тап в любом
# state (включая mid-FSM другого flow) — critic UX-edge: тап по меню
# прерывает текущий flow через state.clear() + set_state(new).
#
# Для today/week (read-only, мгновенный список) — state НЕ трогаем
# (админ может посмотреть расписание mid-flow без отмены).
#
# Auth: _is_admin_callback (callback.from_user, не message.from_user).
# Non-admin tap → silent callback.answer() (admin-menu клиенту не виден).
# ============================================================


def _admin_calendar_range(business_timezone: str) -> tuple[datetime, datetime]:
    """(min_date, max_date) для admin SimpleCalendar — naive local midnight.

    min = today_local @ 00:00, max = today_local + 365d @ 00:00.
    Аналог _calendar_range в client.py:77, но использует business.timezone
    из DB (а не settings.TIMEZONE) — MVP single-master совпадают, но
    семантически calendar должен следовать бизнес-таймзоне.
    """
    tz = ZoneInfo(business_timezone)
    today_local = datetime.now(tz).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    max_local = today_local + timedelta(days=365)
    return today_local, max_local


@router.callback_query(AdminAddslotsCallbackData.filter(), StateFilter("*"))
async def admin_addslots_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Menu tap: открыть слоты — start addslots flow.

    UX-edge: tap mid-FSM → state.clear() + set_state(adding_slots_date).
    Show SimpleCalendar for date selection (next handler — 1.3b).
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    # _is_admin_callback guarantees callback.from_user is not None (mypy не сужает тип
    # после вызова функции — assert здесь как type narrowing, runtime no-op).
    assert callback.from_user is not None
    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    _master_id, _business_id, tz = resolved

    await state.clear()
    await state.set_state(AdminStates.adding_slots_date)

    if callback.message is not None:
        await callback.message.answer(
            "📅 Выберите дату для открытия слотов:",
            reply_markup=await admin_calendar_keyboard(*_admin_calendar_range(tz)),
        )
    await callback.answer()


# ============================================================
# Inline menu callbacks (Вариант B, spec.md 251) — Этап 1.3b
#
# FSM для addslots: date (SimpleCalendar) → hours (text input) → create.
# Calendar handler закрыл W3 (calendar-tap dangling из 1.3a code-review).
#
# INL-001 (donor: UznetDev/Aiogram-Bot-Template/main_admin_panel.py:30-34):
# при переходе calendar → ask hours ИСПОЛЬЗУЕМ edit_message_text (НЕ answer),
# чат админа не засоряется. Fallback на answer если message нельзя edit
# (>48h / удалено — TelegramBadRequest).
# ============================================================


@router.callback_query(SimpleCalendarCallback.filter(), StateFilter(AdminStates.adding_slots_date))
async def admin_addslots_calendar_cb(
    callback: CallbackQuery,
    callback_data: SimpleCalendarCallback,
    state: FSMContext,
) -> None:
    """SimpleCalendar для addslots — навигация + выбор даты.

    Branch by callback_data.act (паттерн из client.py:124-211):
    - ignore/today+same-month: callback.answer(cache_time=60) — lib не вызывается
    - day: сохраняем selected_date, set_state(adding_slots_hours), edit_message_text
    - cancel: state.clear() + edit "Отменено"
    - navigation (prev_y/next_y/prev_m/next_m/today-diff-month): lib сделал
      edit_reply_markup, handler answers
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    # _is_admin_callback guarantees callback.from_user is not None (type narrowing).
    assert callback.from_user is not None
    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await state.clear()
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    _master_id, _business_id, tz = resolved

    # act=ignore/today+same-month: lib не вызывается (early return), handler answers
    # cache_time=60 — contract с aiogram_calendar 0.6.0 (lib answer на этих ветках
    # сам если бы вызвался, handler делает это вместо).
    if callback_data.act == SimpleCalAct.ignore:
        await callback.answer(cache_time=60)
        return
    if callback_data.act == SimpleCalAct.today:
        # lib использует system-local datetime.now() для same-month check
        # (simple_calendar.py:173) — handler матч'ит чтобы избежать TZ-mismatch.
        today_sys = datetime.now().replace(tzinfo=None)
        if today_sys.year == callback_data.year and today_sys.month == callback_data.month:
            await callback.answer(cache_time=60)
            return  # same-month: lib бы answer cache_time=60, handler вместо

    cal = SimpleCalendar(locale="ru_RU.UTF-8", cancel_btn="Отмена", today_btn="Сегодня")
    cal.set_dates_range(*_admin_calendar_range(tz))
    selected, selected_date = await cal.process_selection(callback, callback_data)

    if callback_data.act == SimpleCalAct.day:
        if not selected:
            return  # out-of-range — lib answered alert, do nothing
        slot_date = selected_date.date()
        await state.update_data(selected_date=slot_date.isoformat())
        await state.set_state(AdminStates.adding_slots_hours)

        # INL-001: edit_message_text — чат не засоряется.
        # reply_markup=None убирает calendar keyboard.
        # isinstance сужает Message | InaccessibleMessage → Message (у
        # InaccessibleMessage нет edit_text, mypy:union-attr).
        ask_text = (
            f"Дата: <b>{slot_date.strftime('%d %B %Y')}</b>\n"
            "Введите часы через пробел (например <code>11 12 13 14</code>):"
        )
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(ask_text, reply_markup=None)
            except TelegramBadRequest:
                # message нельзя edit (>48h, удалено) — fallback на новое сообщение
                await callback.message.answer(ask_text)
        await callback.answer()
        return

    if callback_data.act == SimpleCalAct.cancel:
        await state.clear()
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(
                    "❌ Открытие слотов отменено. /menu — заново",
                    reply_markup=None,
                )
            except TelegramBadRequest:
                await callback.message.answer("❌ Открытие слотов отменено. /menu — заново")
        await callback.answer()
        return

    # Navigation (prev_y/next_y/prev_m/next_m/today-diff-month):
    # lib сделал edit_reply_markup, handler answers.
    await callback.answer()


@router.message(StateFilter(AdminStates.adding_slots_hours), F.text, ~F.text.startswith("/"))
async def admin_addslots_hours_msg(message: Message, state: FSMContext) -> None:
    """User typed hours → parse + add_slots + render result.

    Re-use парсинга из cmd_addslots:148-180. State терминальный — state.clear() в конце.

    NB W1 (code-review 1.3b): `~F.text.startswith("/")` в фильтре — НЕ ловит команды.
    `/cancel` из этого state проваливается в client_router (client.py:443,
    Command("cancel") + StateFilter("*")) — документированный escape hatch.
    Полный catch-all (для non-/ текст) — Этап 1.3e.
    """
    if not _is_admin(message):
        return  # silently ignore non-admin

    data = await state.get_data()
    selected_date_iso = data.get("selected_date")
    if not selected_date_iso:
        # State loss mid-flow (restart, timeout) — graceful exit, ask заново.
        await state.clear()
        await message.answer("❌ Дата не выбрана. Откройте слоты заново через /menu")
        return

    try:
        slot_date = date.fromisoformat(selected_date_iso)
    except ValueError:
        # Защита от протухшего/повреждённого state (defense-in-depth).
        await state.clear()
        await message.answer("❌ Ошибка даты в сессии. Начните заново через /menu")
        return

    # Parse hours (re-use cmd_addslots:148-154 logic).
    text = message.text or ""
    try:
        hours_raw = [int(h) for h in text.split()]
    except ValueError:
        await message.answer("❌ Часы должны быть числами 0-23 (например 11 12 13)")
        return  # state stays — ask again

    if not hours_raw:
        await message.answer("❌ Введите хотя бы один час (например 11 12 13)")
        return  # state stays

    hours = sorted(set(hours_raw))  # dedup (composite UNIQUE в DB)

    admin_id = _require_admin_or_silent(message)
    assert admin_id is not None
    resolved = await _resolve_master_and_business(admin_id)
    if resolved is None:
        # W2 (code-review 1.3b): clear state — unrecoverable error, consistency
        # с calendar handler (514-517) и past-date check ниже.
        await state.clear()
        await message.answer("❌ Мастер не найден. Обратитесь к администратору.")
        return
    master_id, _business_id, tz = resolved

    # spec.md 401: slot_date must not be in the past (defensive — calendar уже фильтровал,
    # но между выбором даты и вводом часов мог пройти день).
    today_local = datetime.now(ZoneInfo(tz)).date()
    if slot_date < today_local:
        await message.answer(
            f"❌ Нельзя создать слот в прошлом. Сегодня: {today_local.strftime('%d.%m.%Y')}"
        )
        await state.clear()
        return

    async with async_session_factory() as session:
        try:
            created = await add_slots(session, master_id, slot_date, hours)
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return  # state stays — может исправить часы
        except SlotAlreadyExistsError:
            await message.answer("❌ Один из слотов уже существует (гонка). Попробуйте ещё раз.")
            return

    await state.clear()  # терминальный
    if created:
        hours_str = ", ".join(str(s.slot_hour) for s in created)
        await message.answer(
            f"✅ Открыты слоты на {slot_date.strftime('%d %B')}:\n<b>{hours_str}</b>"
        )
    else:
        await message.answer(
            f"Все слоты на {slot_date.strftime('%d %B')} уже открыты — ничего не добавлено."
        )


# ============================================================
# Inline menu callbacks (Вариант B + Этап 5.1) — admin_openday
#
# FSM для openday: date (SimpleCalendar) → start_time (HH:MM text) →
# end_time (HH:MM text) → open_workday (idempotent UPDATE через UNIQUE).
#
# Адаптация паттерна 1.3b (addslots), отличия:
# - state: opening_workday_date → opening_workday_start → opening_workday_end
#   (3 state вместо 2 — start_time + end_time вместо одного списка часов)
# - service: open_workday (НЕ add_slots) — WorkDay [start, end] вместо Slot list
# - parse: HH:MM (НЕ целые часы 0-23) — _parse_hhmm helper (cmd_openday:280)
# - error: WorkDayShrinkError если shrink с активными bookings (Gap 6)
# - render: "✅ День открыт на {date}: {start}–{end}" (НЕ список часов)
# - INL-001: edit_message_text при calendar → ask start (fallback на answer)
# - W1: ~F.text.startswith("/") в фильтре start/end_msg → /cancel в client_router
# - W2: state.clear() в branch "Мастер не найден" в start/end_msg
# ============================================================


@router.callback_query(AdminOpendayCallbackData.filter(), StateFilter("*"))
async def admin_openday_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Menu tap: открыть день — start opening_workday flow.

    UX-edge: tap mid-FSM → state.clear() + set_state(opening_workday_date).
    Show SimpleCalendar for date selection (next handler — calendar_cb).
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    assert callback.from_user is not None  # type narrowing, runtime no-op
    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    _master_id, _business_id, tz = resolved

    await state.clear()
    await state.set_state(AdminStates.opening_workday_date)

    if callback.message is not None:
        await callback.message.answer(
            "📅 Выберите дату для открытия рабочего дня:",
            reply_markup=await admin_calendar_keyboard(*_admin_calendar_range(tz)),
        )
    await callback.answer()


@router.callback_query(
    SimpleCalendarCallback.filter(), StateFilter(AdminStates.opening_workday_date)
)
async def admin_openday_calendar_cb(
    callback: CallbackQuery,
    callback_data: SimpleCalendarCallback,
    state: FSMContext,
) -> None:
    """SimpleCalendar для openday — навигация + выбор даты.

    Branch by callback_data.act (копия admin_addslots_calendar_cb, отличия:
    state opening_workday_* вместо adding_slots_*, текст "Введите время начала
    (ЧЧ:ММ):" вместо "Введите часы через пробел", "Открытие дня отменено" вместо
    "Открытие слотов отменено").
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    assert callback.from_user is not None
    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await state.clear()
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    _master_id, _business_id, tz = resolved

    if callback_data.act == SimpleCalAct.ignore:
        await callback.answer(cache_time=60)
        return
    if callback_data.act == SimpleCalAct.today:
        today_sys = datetime.now().replace(tzinfo=None)
        if today_sys.year == callback_data.year and today_sys.month == callback_data.month:
            await callback.answer(cache_time=60)
            return

    cal = SimpleCalendar(locale="ru_RU.UTF-8", cancel_btn="Отмена", today_btn="Сегодня")
    cal.set_dates_range(*_admin_calendar_range(tz))
    selected, selected_date = await cal.process_selection(callback, callback_data)

    if callback_data.act == SimpleCalAct.day:
        if not selected:
            return  # out-of-range — lib answered alert
        work_date = selected_date.date()
        await state.update_data(selected_date=work_date.isoformat())
        await state.set_state(AdminStates.opening_workday_start)

        ask_text = (
            f"Дата: <b>{work_date.strftime('%d %B %Y')}</b>\n"
            "Введите время начала (ЧЧ:ММ, например <code>11:00</code>):"
        )
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(ask_text, reply_markup=None)
            except TelegramBadRequest:
                await callback.message.answer(ask_text)
        await callback.answer()
        return

    if callback_data.act == SimpleCalAct.cancel:
        await state.clear()
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(
                    "❌ Открытие дня отменено. /menu — заново",
                    reply_markup=None,
                )
            except TelegramBadRequest:
                await callback.message.answer("❌ Открытие дня отменено. /menu — заново")
        await callback.answer()
        return

    # Navigation (prev_y/next_y/prev_m/next_m/today-diff-month):
    await callback.answer()


@router.message(StateFilter(AdminStates.opening_workday_start), F.text, ~F.text.startswith("/"))
async def admin_openday_start_msg(message: Message, state: FSMContext) -> None:
    """User typed start_time (HH:MM) → store + ask end_time.

    State stays on parse error (admin can retry). state.clear() in unrecoverable
    branches (date missing in state, master not found).
    """
    if not _is_admin(message):
        return

    data = await state.get_data()
    selected_date_iso = data.get("selected_date")
    if not selected_date_iso:
        await state.clear()
        await message.answer("❌ Дата не выбрана. Откройте день заново через /menu")
        return

    # Validate stored date (defense-in-depth against stale state) — work_date
    # itself is only used in admin_openday_end_msg (past-date check + open_workday).
    try:
        date.fromisoformat(selected_date_iso)
    except ValueError:
        await state.clear()
        await message.answer("❌ Ошибка даты в сессии. Начните заново через /menu")
        return

    text = message.text or ""
    try:
        start_time = _parse_hhmm(text.strip())
    except ValueError:
        await message.answer("❌ Формат ЧЧ:ММ (например <code>11:00</code>)")
        return  # state stays — ask again

    await state.update_data(start_time=start_time.isoformat())
    await state.set_state(AdminStates.opening_workday_end)
    await message.answer(
        f"Начало: <b>{start_time.strftime('%H:%M')}</b>\n"
        "Введите время окончания (ЧЧ:ММ, например <code>18:00</code>):"
    )


@router.message(StateFilter(AdminStates.opening_workday_end), F.text, ~F.text.startswith("/"))
async def admin_openday_end_msg(message: Message, state: FSMContext) -> None:
    """User typed end_time (HH:MM) → open_workday + render result.

    State терминальный — state.clear() в success / unrecoverable error ветках.
    State stays в parse-error / shrink-error (admin can retry end_time or shrink
    via different input).
    """
    if not _is_admin(message):
        return

    data = await state.get_data()
    selected_date_iso = data.get("selected_date")
    start_time_iso = data.get("start_time")
    if not selected_date_iso or not start_time_iso:
        await state.clear()
        await message.answer("❌ Данные сессии потеряны. Начните заново через /menu")
        return

    try:
        work_date = date.fromisoformat(selected_date_iso)
        start_time = dt_time.fromisoformat(start_time_iso)
    except ValueError:
        await state.clear()
        await message.answer("❌ Ошибка данных в сессии. Начните заново через /menu")
        return

    text = message.text or ""
    try:
        end_time = _parse_hhmm(text.strip())
    except ValueError:
        await message.answer("❌ Формат ЧЧ:ММ (например <code>18:00</code>)")
        return  # state stays — ask again

    admin_id = _require_admin_or_silent(message)
    assert admin_id is not None
    resolved = await _resolve_master_and_business(admin_id)
    if resolved is None:
        # W2: clear state — unrecoverable error, consistency с calendar/start handlers.
        await state.clear()
        await message.answer("❌ Мастер не найден. Обратитесь к администратору.")
        return
    master_id, _business_id, tz = resolved

    # Defensive past-date re-check (как admin_addslots_hours_msg:689-694) —
    # между выбором даты и вводом end_time мог пройти день.
    today_local = datetime.now(ZoneInfo(tz)).date()
    if work_date < today_local:
        await message.answer(
            f"❌ Нельзя открыть день в прошлом. Сегодня: {today_local.strftime('%d.%m.%Y')}"
        )
        await state.clear()
        return

    async with async_session_factory() as session:
        # F1 fix (Session 5.18, variant B): capture was_closed BEFORE open_workday
        # re-opens the day (mirror cmd_openday).
        existing = await select_workday(session, master_id, work_date)
        was_closed = existing is not None and not existing.is_active
        try:
            await open_workday(session, master_id, work_date, start_time, end_time, business_tz=tz)
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return  # state stays — admin can retry end_time
        except WorkDayShrinkError as exc:
            await message.answer(
                f"❌ Нельзя сократить окно — есть активные записи.\n{exc}\n"
                "Сначала отмените записи (/cancelbooking) или выберите другое время."
            )
            return  # state stays — admin can retry end_time (расширяя окно)
        except SQLAlchemyError:
            # S2 fix (code-review 5.1): DB-level ошибки (IntegrityError на UNIQUE
            # race despite advisory lock, OperationalError на connection loss) —
            # НЕ ValueError, не ловились except выше. State hang — unrecoverable,
            # очищаем. Mirror admin_service_duration_msg:1467-1474.
            await state.clear()
            await message.answer("❌ Ошибка БД. Начните заново через /menu")
            return

    await state.clear()  # терминальный
    await message.answer(
        f"✅ День открыт на {work_date.strftime('%d %B %Y')}:\n"
        f"<b>{start_time.strftime('%H:%M')}–{end_time.strftime('%H:%M')}</b>"
        + ("\n(день был закрыт — открыт заново)" if was_closed else "")
    )


@router.callback_query(AdminCloseslotCallbackData.filter(), StateFilter("*"))
async def admin_closeslot_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Menu tap: закрыть слот — start closeslot flow.

    UX-edge: tap mid-FSM → state.clear() + set_state(closing_slot_date).
    Show SimpleCalendar for date selection (next handler — 1.3c).
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    # _is_admin_callback guarantees callback.from_user is not None (type narrowing).
    assert callback.from_user is not None
    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    _master_id, _business_id, tz = resolved

    await state.clear()
    await state.set_state(AdminStates.closing_slot_date)

    if callback.message is not None:
        await callback.message.answer(
            "📅 Выберите дату слота для закрытия:",
            reply_markup=await admin_calendar_keyboard(*_admin_calendar_range(tz)),
        )
    await callback.answer()


# ============================================================
# FSM closeslot (Вариант B, spec.md 251) — Этап 1.3c
#
# Адаптация паттерна 1.3b (addslots), отличия:
# - state: closing_slot_date → closing_slot_hour (НЕ adding_slots_*)
# - hour input: SINGLE час (не список через пробел)
# - service: close_slot (НЕ add_slots) — нужен find slot по (master, date, hour)
# - render: "✅ Слот {date} {hour}:00 закрыт" (НЕ список часов)
# - past-date re-check ОТСУТСТВУЕТ (отличие от 1.3b) — закрыть slot в прошлом
#   валидно (отменить невыполненную запись). cmd_closeslot тоже не проверяет.
#   Calendar min=today всё равно не даёт выбрать прошлое.
# - INL-001: edit_message_text при calendar → ask hour (fallback на answer).
# - W1 analog: ~F.text.startswith("/") в фильтре hour_msg → /cancel проваливается
#   в client_router (escape hatch).
# - W2 analog: state.clear() в branch "Мастер не найден" в hour_msg.
# ============================================================


@router.callback_query(SimpleCalendarCallback.filter(), StateFilter(AdminStates.closing_slot_date))
async def admin_closeslot_calendar_cb(
    callback: CallbackQuery,
    callback_data: SimpleCalendarCallback,
    state: FSMContext,
) -> None:
    """SimpleCalendar для closeslot — навигация + выбор даты.

    Branch by callback_data.act (копия admin_addslots_calendar_cb, отличия:
    state closing_slot_* вместо adding_slots_*, текст "Введите час (один, 0-23):"
    вместо "Введите часы через пробел", "Закрытие слота отменено" вместо
    "Открытие слотов отменено").
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    # _is_admin_callback guarantees callback.from_user is not None (type narrowing).
    assert callback.from_user is not None
    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await state.clear()
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    _master_id, _business_id, tz = resolved

    # act=ignore/today+same-month: lib не вызывается (early return), handler answers
    if callback_data.act == SimpleCalAct.ignore:
        await callback.answer(cache_time=60)
        return
    if callback_data.act == SimpleCalAct.today:
        today_sys = datetime.now().replace(tzinfo=None)
        if today_sys.year == callback_data.year and today_sys.month == callback_data.month:
            await callback.answer(cache_time=60)
            return

    cal = SimpleCalendar(locale="ru_RU.UTF-8", cancel_btn="Отмена", today_btn="Сегодня")
    cal.set_dates_range(*_admin_calendar_range(tz))
    selected, selected_date = await cal.process_selection(callback, callback_data)

    if callback_data.act == SimpleCalAct.day:
        if not selected:
            return  # out-of-range — lib answered alert
        slot_date = selected_date.date()
        await state.update_data(selected_date=slot_date.isoformat())
        await state.set_state(AdminStates.closing_slot_hour)

        # INL-001: edit_message_text — чат не засоряется.
        ask_text = f"Дата: <b>{slot_date.strftime('%d %B %Y')}</b>\nВведите час (один, 0-23):"
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(ask_text, reply_markup=None)
            except TelegramBadRequest:
                await callback.message.answer(ask_text)
        await callback.answer()
        return

    if callback_data.act == SimpleCalAct.cancel:
        await state.clear()
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(
                    "❌ Закрытие слота отменено. /menu — заново",
                    reply_markup=None,
                )
            except TelegramBadRequest:
                await callback.message.answer("❌ Закрытие слота отменено. /menu — заново")
        await callback.answer()
        return

    # Navigation (prev_y/next_y/prev_m/next_m/today-diff-month):
    # lib сделал edit_reply_markup, handler answers.
    await callback.answer()


@router.message(StateFilter(AdminStates.closing_slot_hour), F.text, ~F.text.startswith("/"))
async def admin_closeslot_hour_msg(message: Message, state: FSMContext) -> None:
    """User typed hour → parse + find slot + close_slot + render result.

    Re-use cmd_closeslot:218-267 logic (parse hour, resolve master, find slot
    by composite key, close_slot service). State терминальный — state.clear()
    в success / already-closed / unrecoverable error ветках. State stays в
    "slot not found" / "slot booked" / parse-error (админ может поправить ввод).

    NB W1 (code-review 1.3b): `~F.text.startswith("/")` в фильтре — НЕ ловит
    команды. `/cancel` из этого state проваливается в client_router (escape
    hatch). Полный catch-all — Этап 1.3e.
    """
    if not _is_admin(message):
        return  # silently ignore non-admin

    data = await state.get_data()
    selected_date_iso = data.get("selected_date")
    if not selected_date_iso:
        # State loss mid-flow (restart, timeout) — graceful exit, ask заново.
        await state.clear()
        await message.answer("❌ Дата не выбрана. Закройте слот заново через /menu")
        return

    try:
        slot_date = date.fromisoformat(selected_date_iso)
    except ValueError:
        # Защита от протухшего/повреждённого state (defense-in-depth).
        await state.clear()
        await message.answer("❌ Ошибка даты в сессии. Начните заново через /menu")
        return

    # Parse SINGLE hour (отличие от 1.3b — int, не list[int] из text.split()).
    text = (message.text or "").strip()
    try:
        hour = int(text)
    except ValueError:
        await message.answer("❌ Час должен быть числом 0-23 (например 14)")
        return  # state stays — ask again
    if not 0 <= hour <= 23:
        await message.answer("❌ Час должен быть 0-23")
        return  # state stays

    admin_id = _require_admin_or_silent(message)
    assert admin_id is not None
    resolved = await _resolve_master_and_business(admin_id)
    if resolved is None:
        # W2 analog (code-review 1.3b): clear state — unrecoverable error,
        # consistency с calendar handler выше.
        await state.clear()
        await message.answer("❌ Мастер не найден. Обратитесь к администратору.")
        return
    master_id, _business_id, _tz = resolved

    # NB: past-date re-check ОТСУТСТВУЕТ (отличие от 1.3b:635-643). Закрыть slot
    # в прошлом валидно (отменить невыполненную запись). cmd_closeslot тоже не
    # проверяет. Calendar min=today не даёт выбрать прошлое через UI, но если
    # админ ввёл дату вручную через /closeslot — past валиден.

    # Find slot by (master, date, hour) — re-use cmd_closeslot:243-253 logic.
    from sqlalchemy import select

    from bot.models import Slot

    async with async_session_factory() as session:
        stmt = select(Slot).where(
            Slot.master_id == master_id,
            Slot.slot_date == slot_date,
            Slot.slot_hour == hour,
        )
        slot = (await session.execute(stmt)).scalar_one_or_none()
        if slot is None:
            # State stays — админ мог опечататься в часе, попробовать другой.
            await message.answer(f"❌ Слот {slot_date.strftime('%d %B')} {hour}:00 не найден")
            return

        try:
            updated = await close_slot(session, slot.id)
        except ValueError as exc:
            # Slot booked — close_slot raises ValueError. State stays — админ
            # может попробовать другой час (этот slot не закрывается).
            await message.answer(f"❌ {exc}")
            return

    await state.clear()  # терминальный
    if updated:
        await message.answer(f"✅ Слот {slot_date.strftime('%d %B')} {hour}:00 закрыт")
    else:
        # close_slot вернул False — slot не найден (гонка: удалён между
        # SELECT и UPDATE). Редкий кейс, считаем терминальным.
        await message.answer(f"❌ Слот {slot_date.strftime('%d %B')} {hour}:00 не найден")


@router.callback_query(AdminTodayCallbackData.filter(), StateFilter("*"))
async def admin_today_cb(callback: CallbackQuery) -> None:
    """Menu tap: сегодня — мгновенный список записей (no FSM).

    State НЕ трогаем — админ может посмотреть расписание mid-flow без отмены.
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    # _is_admin_callback guarantees callback.from_user is not None (type narrowing).
    assert callback.from_user is not None
    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    master_id, _business_id, tz = resolved

    async with async_session_factory() as session:
        bookings = await get_today_bookings(session, master_id, tz)

    if callback.message is not None:
        if not bookings:
            await callback.message.answer("На сегодня записей нет.")
        else:
            await callback.message.answer(
                _render_bookings("📅 Записи на сегодня:", bookings, tz),
                reply_markup=admin_today_keyboard(bookings, tz),
            )
    await callback.answer()


@router.callback_query(AdminWeekCallbackData.filter(), StateFilter("*"))
async def admin_week_cb(callback: CallbackQuery) -> None:
    """Menu tap: неделя — мгновенный список записей на 7 дней (no FSM).

    State НЕ трогаем — админ может посмотреть расписание mid-flow без отмены.
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    # _is_admin_callback guarantees callback.from_user is not None (type narrowing).
    assert callback.from_user is not None
    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    master_id, _business_id, tz = resolved

    async with async_session_factory() as session:
        bookings = await get_week_bookings(session, master_id, tz, days_ahead=7)

    if callback.message is not None:
        if not bookings:
            await callback.message.answer("На ближайшую неделю записей нет.")
        else:
            await callback.message.answer(_render_bookings("📅 Записи на неделю:", bookings, tz))
    await callback.answer()


@router.callback_query(AdminServicesCallbackData.filter(), StateFilter("*"))
async def admin_services_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """Menu tap: добавить услугу — start entering_service flow (2 шага).

    UX-edge: tap mid-FSM → state.clear() + set_state(entering_service_name).
    Next handlers (1.3d) ask for name → duration → create_service.

    NB W1 (code-review 1.3a): resolve master+business на entry (НЕ в последнем
    handler duration_msg) — business_id нужен для create_service в конце flow.
    Сохраняем в state, чтобы duration_msg не делал повторный DB lookup.

    Price убран в Session 5.10 — мастер озвучивает цену отдельно в чате.
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    # _is_admin_callback guarantees callback.from_user is not None (type narrowing).
    assert callback.from_user is not None
    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    _master_id, business_id, _tz = resolved

    await state.clear()
    await state.update_data(business_id=str(business_id))
    await state.set_state(AdminStates.entering_service_name)

    if callback.message is not None:
        await callback.message.answer("💇 Введите название услуги (можно с пробелами):")
    await callback.answer()


# ============================================================
# FSM services (Вариант B, spec.md 251) — Этап 1.3d
#
# 2 шага: name → duration → create_service (price убран в Session 5.10).
# Re-use cmd_services logic (parse name/duration, validate, create_service).
# State terminal в duration_msg success — state.clear().
#
# NB INL-001 НЕ ПРИМЕНИМ для 1.3d: edit_message_text работает только для
# callback→text переходов (calendar→ask hour в 1.3b/c), где edit убирает
# календарь. В 1.3d между шагами — message от юзера + новое message от бота:
# edit_message_text нечего редактировать (сообщение юзера нельзя править
# ботом, предыдущее сообщение бота уже показано). Используем answer (как в
# cmd_services) — чат засоряется, но для MVP acceptable.
#
# NB name: НЕ делаем replace("_", " ") (отличие от cmd_services). В 1.3d
# юзер вводит name целиком через message.text, как написано так и сохраняем.
# cmd_services делал replace из-за arg-splitting в одной строке ("/services
# add Стрижка_мужская 60" — args[1] = "Стрижка_мужская").
#
# W1 analog: ~F.text.startswith("/") в фильтре всех 2 msg handlers → /cancel
# проваливается в client_router (escape hatch).
# W2 analog: state.clear() в unrecoverable error ветках (state loss, master
# not found).
# ============================================================


@router.message(StateFilter(AdminStates.entering_service_name), F.text, ~F.text.startswith("/"))
async def admin_service_name_msg(message: Message, state: FSMContext) -> None:
    """User typed service name → save + ask duration.

    State stays в empty-name (ask again). set_state(entering_service_duration)
    в success. /cancel через client_router escape hatch (W1 analog).
    """
    if not _is_admin(message):
        return  # silently ignore non-admin

    data = await state.get_data()
    business_id_str = data.get("business_id")
    if not business_id_str:
        # State loss mid-flow (restart, timeout) — graceful exit, ask заново.
        await state.clear()
        await message.answer("❌ Сессия утеряна. Добавьте услугу заново через /menu")
        return

    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ Название не может быть пустым. Введите название:")
        return  # state stays — ask again
    if len(name) > 255:
        # S1 (code-review 1.3d): early validation — не ждать duration_msg чтобы
        # узнать про name>255 (create_service бы поднял ValueError, но после
        # 1 впустую потраченного шага). Лучше early fail.
        await message.answer("❌ Слишком длинное название (макс 255 символов). Введите короче:")
        return  # state stays

    await state.update_data(name=name)
    await state.set_state(AdminStates.entering_service_duration)
    await message.answer(
        f"Название: <b>{html.escape(name, quote=False)}</b>\nВведите длительность (мин, число > 0):"
    )


@router.message(StateFilter(AdminStates.entering_service_duration), F.text, ~F.text.startswith("/"))
async def admin_service_duration_msg(message: Message, state: FSMContext) -> None:
    """User typed duration → parse + validate + create_service + render result.

    Terminal step (Session 5.10): price убран, после duration сразу создаём
    услугу. State.clear() в success и unrecoverable error. State stays в
    parse-error / <=0 (ask again). /cancel через client_router escape hatch.
    """
    if not _is_admin(message):
        return  # silently ignore non-admin

    data = await state.get_data()
    name = data.get("name")
    business_id_str = data.get("business_id")
    # Defense-in-depth: проверяем что name и business_id ещё живы (state loss
    # между шагами name→duration).
    if not name or not business_id_str:
        await state.clear()
        await message.answer("❌ Сессия утеряна. Добавьте услугу заново через /menu")
        return

    text = (message.text or "").strip()
    try:
        duration = int(text)
    except ValueError:
        await message.answer("❌ Длительность должна быть числом минут (например 60)")
        return  # state stays
    if duration <= 0:
        await message.answer("❌ Длительность должна быть > 0")
        return  # state stays

    try:
        business_id = UUID(business_id_str)
    except ValueError:
        # Corrupted state (business_id not valid UUID) — defense-in-depth.
        await state.clear()
        await message.answer("❌ Ошибка сессии (business_id). Начните заново через /menu")
        return

    async with async_session_factory() as session:
        try:
            service = await create_service(session, business_id, name, duration)
        except ValueError as exc:
            # create_service defense-in-depth (name >255 — уже в name_msg,
            # duration<=0 — здесь выше). Если service всё же поднимает
            # ValueError на edge case — state stays, retryable.
            await message.answer(f"❌ {exc}")
            return  # state stays
        except SQLAlchemyError:
            # W2 fix (code-review 1.3d): DB-level ошибки (IntegrityError на
            # constraint, DataError на invalid value, OperationalError на
            # connection loss) — НЕ ValueError, не ловились except выше.
            # State hang — unrecoverable, очищаем.
            await state.clear()
            await message.answer("❌ Ошибка БД. Начните заново через /menu")
            return

    await state.clear()  # терминальный
    await message.answer(
        f"✅ Услуга добавлена:\n"
        f"💇 <b>{html.escape(service.name, quote=False)}</b>\n"
        f"⏱ {service.duration_minutes} мин"
    )


# ============================================================
# Этап 5.9 — admin_move flow (/today → [🔄 Перенести] → calendar → 30-min slot → confirm)
# ============================================================
# 5 handlers for admin-initiated booking move (mirrors transfer flow in
# client.py:904-1130 but with AdminMoveStates + admin_move_booking service):
#   1. admin_move_select_cb    — [🔄 Перенести] tap → set_state(selecting_date)
#   2. admin_move_simple_calendar_cb — SimpleCalendar nav + day select → fetch
#      30-min slots from WorkDay → set_state(selecting_slot)
#   3. admin_move_slot_30_cb   — slot tap → save workday_id + start_minute →
#      set_state(confirming) + show summary
#   4. admin_move_confirm_cb   — [✅ Перенести] → call admin_move_booking,
#      notify client, clear state
#   5. admin_move_cancel_cb    — [❌ Отмена] string callback → clear state
#
# Distinct from TransferStates via StateFilter (handler dispatch by state,
# NOT by is_admin_move flag — avoids flag pollution, see states.py:74-76).
# Distinct from BookingStates.selecting_date via same StateFilter mechanism.
# ============================================================


@router.callback_query(AdminMoveCallbackData.filter(), StateFilter("*"))
async def admin_move_select_cb(
    callback: CallbackQuery,
    callback_data: AdminMoveCallbackData,
    state: FSMContext,
) -> None:
    """[🔄 Перенести] tap in /today list — start admin_move FSM.

    Saves booking_id in FSM (state.set_state AFTER save — order is safe because
    state.update_data doesn't trigger handlers, set_state does).

    StateFilter("*") + state.clear() — admin can start move mid-FSM. clear()
    mirrors admin_addslots_cb:644 (prevents state pollution from prior flow,
    e.g. addslots `selected_date` leaking into admin_move FSM data dict).
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return
    assert callback.from_user is not None

    # Resolve tz here for calendar range (same pattern as admin_addslots_cb).
    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    _master_id, _business_id, tz = resolved

    await state.clear()
    await state.update_data(admin_move_booking_id=str(callback_data.booking_id))
    await state.set_state(AdminMoveStates.selecting_date)
    if callback.message is not None:
        await callback.message.answer(
            "📅 Выберите новую дату для переноса:",
            reply_markup=await admin_calendar_keyboard(*_admin_calendar_range(tz)),
        )
    await callback.answer()


@router.callback_query(
    SimpleCalendarCallback.filter(), StateFilter(AdminMoveStates.selecting_date)
)
async def admin_move_simple_calendar_cb(
    callback: CallbackQuery,
    callback_data: SimpleCalendarCallback,
    state: FSMContext,
) -> None:
    """SimpleCalendar navigation + day select for admin_move flow.

    Distinct from admin_addslots_calendar_cb (StateFilter(AdminStates.adding_slots_date))
    and from client._handle_simple_calendar (which branches on is_slots_path flag
    — admin_move is ALWAYS workday path, no flag needed).

    On day select: fetch WorkDay for (master_id, slot_date). If None → "не работает
    в этот день" hint. If is_active=False → "день закрыт" hint. Both → re-show
    calendar (user can pick another date). If workday found → fetch 30-min
    slots via get_available_slots_30 → render slot_picker_keyboard_30min with
    AdminMoveSlot30CallbackData → set_state(selecting_slot).
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return
    assert callback.from_user is not None

    resolved = await _resolve_master_and_business(callback.from_user.id)
    if resolved is None:
        await state.clear()
        await callback.answer("❌ Мастер не найден", show_alert=True)
        return
    master_id, _business_id, tz = resolved

    # act=ignore/today+same-month: lib не вызывается (early return), handler answers
    # cache_time=60 — contract с aiogram_calendar 0.6.0 (mirror admin_addslots_calendar_cb).
    if callback_data.act == SimpleCalAct.ignore:
        await callback.answer(cache_time=60)
        return
    if callback_data.act == SimpleCalAct.today:
        today_sys = datetime.now().replace(tzinfo=None)
        if today_sys.year == callback_data.year and today_sys.month == callback_data.month:
            await callback.answer(cache_time=60)
            return  # same-month: lib answer cache_time=60, handler вместо

    cal = SimpleCalendar(locale="ru_RU.UTF-8", cancel_btn="Отмена", today_btn="Сегодня")
    cal.set_dates_range(*_admin_calendar_range(tz))
    selected, selected_date = await cal.process_selection(callback, callback_data)

    if callback_data.act == SimpleCalAct.day:
        if not selected:
            return  # out-of-range — lib answered alert, do nothing
        slot_date = selected_date.date()

        # Fetch WorkDay for (master_id, slot_date). No workday → master не работает.
        # Inactive workday → closed via /closeday. Both → hint + re-show calendar.
        async with async_session_factory() as session:
            workday = await select_workday(session, master_id, slot_date)

        if workday is None:
            if callback.message is not None:
                await callback.message.answer(
                    "❌ Мастер не работает в этот день. Выберите другую дату.",
                    reply_markup=await admin_calendar_keyboard(*_admin_calendar_range(tz)),
                )
            await callback.answer()
            return
        if not workday.is_active:
            if callback.message is not None:
                await callback.message.answer(
                    "❌ Этот день закрыт. Выберите другую дату.",
                    reply_markup=await admin_calendar_keyboard(*_admin_calendar_range(tz)),
                )
            await callback.answer()
            return

        # Workday found + active → fetch 30-min available slots.
        async with async_session_factory() as session:
            slots = await get_available_slots_30(session, workday, tz)

        # Save new_workday_id for slot_30_cb + confirm_cb.
        await state.update_data(admin_move_new_workday_id=str(workday.id))
        await state.set_state(AdminMoveStates.selecting_slot)

        if callback.message is not None:
            # Build keyboard inline with AdminMoveSlot30CallbackData (distinct
            # prefix from BookSlot30CallbackData — no dispatch conflict).
            from aiogram.utils.keyboard import InlineKeyboardBuilder

            builder_kb = InlineKeyboardBuilder()
            if not slots:
                builder_kb.button(text="Нет свободных слотов", callback_data="noop")
                await callback.message.answer(
                    "На эту дату нет свободных слотов. Выберите другую дату.",
                    reply_markup=builder_kb.as_markup(),
                )
                await callback.answer()
                return
            for slot in slots:
                start_minute = slot.start_time_local.hour * 60 + slot.start_time_local.minute
                cb = AdminMoveSlot30CallbackData(
                    workday_id=workday.id,
                    start_minute=start_minute,
                )
                builder_kb.button(text=slot.label, callback_data=cb.pack())
            builder_kb.adjust(3)
            await callback.message.answer(
                "⏰ Выберите новое время:",
                reply_markup=builder_kb.as_markup(),
            )
    elif callback_data.act == SimpleCalAct.cancel:
        await state.clear()
        if callback.message is not None:
            await callback.message.answer("❌ Перенос отменён.")
    # navigation (prev_y/next_y/prev_m/next_m/today-diff-month): lib did
    # edit_reply_markup, handler answers.
    await callback.answer()


@router.callback_query(
    AdminMoveSlot30CallbackData.filter(), StateFilter(AdminMoveStates.selecting_slot)
)
async def admin_move_slot_30_cb(
    callback: CallbackQuery,
    callback_data: AdminMoveSlot30CallbackData,
    state: FSMContext,
) -> None:
    """Slot tap → save workday_id + start_minute → set_state(confirming) + show summary.

    Summary fetches booking.start_at + WorkDay.work_date from DB for old/new time
    display. booking_id comes from FSM data (saved in admin_move_select_cb).
    workday_id + start_minute come from callback_data (admin_move_simple_calendar_cb
    saved workday_id in FSM too, but callback_data is the source of truth —
    avoids stale FSM state race).
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return
    assert callback.from_user is not None

    data = await state.get_data()
    booking_id_str = data.get("admin_move_booking_id")
    if not booking_id_str:
        await state.clear()
        if callback.message is not None:
            await callback.message.answer("❌ Данные потеряны. /today чтобы начать")
        await callback.answer()
        return

    # Save new_workday_id + new_start_minute for confirm_cb.
    await state.update_data(
        admin_move_new_workday_id=str(callback_data.workday_id),
        admin_move_new_start_minute=callback_data.start_minute,
    )
    await state.set_state(AdminMoveStates.confirming)

    # Build summary: fetch booking + workday for old/new times.
    new_start_minute = callback_data.start_minute
    new_time_local = dt_time(new_start_minute // 60, new_start_minute % 60)

    async with async_session_factory() as session:
        from sqlalchemy import select as sa_select

        booking = (
            await session.execute(
                sa_select(Booking).where(Booking.id == UUID(booking_id_str))
            )
        ).scalar_one_or_none()
        if booking is None:
            await state.clear()
            if callback.message is not None:
                await callback.message.answer("❌ Запись не найдена. /today чтобы начать")
            await callback.answer()
            return
        workday = (
            await session.execute(
                sa_select(WorkDay).where(WorkDay.id == callback_data.workday_id)
            )
        ).scalar_one_or_none()
        if workday is None:
            await state.clear()
            if callback.message is not None:
                await callback.message.answer("❌ Рабочий день не найден. /today чтобы начать")
            await callback.answer()
            return

    # Render old + new LOCAL times for summary (mirror transfer_booking:1077-1086).
    settings = get_settings()
    tz_obj = ZoneInfo(settings.TIMEZONE)
    old_local = booking.start_at.replace(tzinfo=UTC).astimezone(tz_obj)
    old_formatted = old_local.strftime("%d %b %Y, %H:%M")
    # new date = workday.work_date + new_time_local (LOCAL) → formatted.
    new_local_dt = datetime.combine(workday.work_date, new_time_local, tzinfo=tz_obj)
    new_formatted = new_local_dt.strftime("%d %b %Y, %H:%M")

    if callback.message is not None:
        await callback.message.answer(
            f"Подтвердите перенос:\n\n"
            f"📅 Было: {old_formatted}\n"
            f"📅 Станет: {new_formatted}\n"
            f"👤 {booking.client_name_snapshot}\n"
            f"💇 {booking.service_title_snapshot}",
            reply_markup=admin_move_confirm_keyboard(),
        )
    await callback.answer()


@router.callback_query(
    AdminMoveConfirmCallbackData.filter(), StateFilter(AdminMoveStates.confirming)
)
async def admin_move_confirm_cb(
    callback: CallbackQuery,
    state: FSMContext,
    scheduler: AsyncIOScheduler,
) -> None:
    """[✅ Перенести] → call admin_move_booking service, notify client, clear state.

    state.clear() BEFORE service call (race condition, mirror transfer_slot_cb:1062).
    admin_move_booking is idempotent on race (start_at pin), but if user taps twice
    in quick succession, the second tap should NOT reuse stale state.

    `scheduler` injected from dp["scheduler"] workflow_data (same as transfer_slot_cb).

    Error mapping (mirror transfer_slot_cb:1071-1111, minus CancelTooLateError
    and SlotClosedError/SlotNotAvailableError — admin_move has no 24h rule and no
    legacy slot lookup):
      BookingNotFoundError             → "Запись не найдена"
      BookingAlreadyCancelledError      → "Запись уже отменена"  (defensive — service
                                          doesn't raise this for admin_move, but keep
                                          for safety if service changes)
      BookingAlreadyTransferredError    → "❌ Запись уже перенесена (конкурентный запрос)"
      SlotAlreadyBookedError            → "😔 Слот только что заняли"
      SlotInPastError                   → "❌ Это время уже прошло"
      WorkDayNotFoundError               → "❌ Рабочий день не найден"
      WorkDayInactiveError               → "❌ День закрыт"
      BookingOutsideWorkDayError         → "❌ Время вне рабочего окна"
      WorkDayCapacityExceededError       → "❌ Нет мест (все слоты заняты)"
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return
    assert callback.from_user is not None

    data = await state.get_data()
    booking_id_str = data.get("admin_move_booking_id")
    new_workday_id_str = data.get("admin_move_new_workday_id")
    new_start_minute = data.get("admin_move_new_start_minute")
    if not (booking_id_str and new_workday_id_str and new_start_minute is not None):
        await state.clear()
        if callback.message is not None:
            await callback.message.answer("❌ Данные потеряны. /today чтобы начать")
        await callback.answer()
        return

    # Build new_start_at_local time from int minute (0-1439).
    new_time_local = dt_time(int(new_start_minute) // 60, int(new_start_minute) % 60)

    # state.clear() BEFORE service call (race condition, mirror transfer_slot_cb:1062).
    await state.clear()
    async with async_session_factory() as session:
        try:
            result: AdminMoveResult = await admin_move_booking(
                session,
                UUID(booking_id_str),
                UUID(new_workday_id_str),
                new_time_local,
                scheduler,
            )
        except BookingNotFoundError:
            await callback.answer("Запись не найдена")
            return
        except BookingAlreadyCancelledError:
            await callback.answer("Запись уже отменена")
            return
        except BookingAlreadyTransferredError:
            if callback.message is not None:
                await callback.message.answer(
                    "❌ Запись уже перенесена (конкурентный запрос). "
                    "/today чтобы увидеть актуальный список"
                )
            await callback.answer()
            return
        except SlotAlreadyBookedError:
            if callback.message is not None:
                await callback.message.answer(
                    "😔 Слот только что заняли. /today чтобы выбрать другой"
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
                await callback.message.answer("❌ Рабочий день не найден. /today чтобы начать")
            await callback.answer()
            return
        except WorkDayInactiveError:
            if callback.message is not None:
                await callback.message.answer("❌ Этот день закрыт. /today чтобы начать")
            await callback.answer()
            return
        except BookingOutsideWorkDayError:
            if callback.message is not None:
                await callback.message.answer("❌ Время вне рабочего окна.")
            await callback.answer()
            return
        except WorkDayCapacityExceededError:
            if callback.message is not None:
                await callback.message.answer("❌ Нет мест — все слоты заняты.")
            await callback.answer()
            return

        # Send CLIENT notification (NOT master — admin already knows, client needs to know).
        # Mirror transfer_slot_cb:1115-1119 but chat_id = client_telegram_id (not ADMIN_ID).
        if result.client_telegram_id is not None and callback.bot is not None:
            tz_obj = ZoneInfo(result.business_timezone)
            old_local = result.old_start_at.astimezone(tz_obj)
            new_local = result.new_start_at.astimezone(tz_obj)
            old_formatted = old_local.strftime("%d %b %Y, %H:%M")
            new_formatted = new_local.strftime("%d %b %Y, %H:%M")
            client_text = (
                f"📢 Ваша запись перенесена мастером:\n\n"
                f"📅 Было: {old_formatted}\n"
                f"📅 Станет: {new_formatted}\n"
                f"👤 {result.client_name_snapshot}\n"
                f"💇 {result.service_title_snapshot}"
            )
            try:
                await callback.bot.send_message(
                    chat_id=result.client_telegram_id,
                    text=client_text,
                )
            except TelegramBadRequest:
                # Client blocked the bot — log and skip (booking is still moved).
                logger.warning(
                    "admin_move: client %s blocked the bot — notification skipped "
                    "(booking %s moved)",
                    result.client_telegram_id,
                    result.booking_id,
                )

    if callback.message is not None:
        settings = get_settings()
        tz_obj = ZoneInfo(settings.TIMEZONE)
        new_local = result.new_start_at.astimezone(tz_obj)
        new_formatted = new_local.strftime("%d %b %Y, %H:%M")
        await callback.message.answer(
            f"✅ Запись перенесена на {new_formatted}. Клиент уведомлён."
        )
    await callback.answer()


@router.callback_query(F.data == "admin_move_cancel", StateFilter(AdminMoveStates))
async def admin_move_cancel_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """[❌ Отмена] string callback in confirming state — clear FSM, answer.

    Uses F.data == "admin_move_cancel" (string callback_data from
    admin_move_confirm_keyboard, keyboards/admin.py:233) — no CallbackData class
    needed for plain string. StateFilter(AdminMoveStates) ensures this only
    catches cancel within admin_move flow (not other admin states).
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return
    await state.clear()
    if callback.message is not None:
        await callback.message.answer("❌ Перенос отменён.")
    await callback.answer()


# ============================================================
# Этап 1.3e — /cancel в admin_router + admin-state catch-all
# (ПОСЛЕДНИЙ подэтап 1.3, Session 5.9)
# ============================================================
# Регистрация В КОНЦЕ admin.py — aiogram router обрабатывает в порядке
# декораторов. Catch-all должен быть ПОСЛЕ всех специфичных handlers
# (cmd_addslots, calendar_cb, hours_msg, hour_msg, name_msg, duration_msg),
# иначе перехватит раньше. /cancel — ПОСЛЕ FSM handlers, но
# ПЕРЕД catch-all (точка 4 в порядке регистрации ниже).
# Порядок внутри admin_router:
#   1. Специфичные commands (/addslots /closeslot /today /week /services) — StateFilter(None)
#   2. Menu callbacks (admin_addslots_cb и др.) — StateFilter("*") + callback filter
#   3. FSM handlers (calendar_cb, hours_msg, hour_msg, name_msg,
#      duration_msg) — StateFilter(specific)
#   4. /cancel (admin_cancel_msg) — StateFilter(AdminStates) + Command("cancel")
#   5. Catch-all text — StateFilter(AdminStates) + F.text + ~startswith("/")
#   6. Catch-all callback — StateFilter(AdminStates)
# admin_router включается ПЕРЕД client_router (main.py:118-119), поэтому
# /cancel из admin FSM states ловится ЗДЕСЬ, не в client_router cancel_msg
# (client.py:443, booking-specific "Ввод отменён. /book чтобы начать заново").
# ============================================================


@router.message(Command("cancel"), StateFilter(AdminStates))
async def admin_cancel_msg(message: Message, state: FSMContext) -> None:
    """Cancel admin FSM flow — clears state, admin-specific message.

    StateFilter(AdminStates) матчит ЛЮБОЙ из 6 admin states. /cancel в
    BookingStates или StateFilter(None) НЕ матчится — проваливается в
    client_router cancel_msg (client.py:443).
    state.clear() BEFORE answer (race condition, MY-VIBE-RULES.md 24).
    """
    if not _is_admin(message):
        return  # non-admin в admin state — edge case (storage isolation), silent
    await state.clear()
    await message.answer("Админ-режим отменён. /menu для меню")


@router.message(StateFilter(AdminStates), F.text, ~F.text.startswith("/"))
async def admin_state_catchall_text(message: Message) -> None:
    """Catch-all для non-/ текста в admin FSM state.

    Если специфичный handler (name/duration/hours/hour) не заматчился
    (например, юзер в entering_service_name, но ввёл что-то не то) — бот
    НЕ молчит, а подсказывает /cancel. ~F.text.startswith("/") — /commands
    (включая /cancel) НЕ ловит, проваливаются в свои handlers.
    """
    if not _is_admin(message):
        return
    await message.answer("Используйте /cancel для отмены")


@router.callback_query(StateFilter(AdminStates))
async def admin_state_catchall_callback(callback: CallbackQuery) -> None:
    """Catch-all для stale callback в admin FSM state.

    Stale calendar tap после отмены flow (callback от старого keyboard).
    Без catch-all бот молчит, callback.answer() убирает loading spinner.
    """
    if not _is_admin_callback(callback):
        await callback.answer()
        return
    await callback.answer("Используйте /cancel для отмены", show_alert=False)


# ============================================================
# Этап 3.5 — admin no-state catch-all (UX fix post-deploy Session 5.9)
# ============================================================
# Баг (smoke test на prod): админ завершил FSM (state.clear), затем ввёл
# произвольный текст (не команду, например "12") → проваливается в
# client_router fallback → "Начните запись через /book" (клиентский hint,
# не админский). Confusing для Екатерины — она админ, а не клиент.
#
# Fix: перехватываем в admin_router (registered ПЕРЕД client_router в
# main.py:117-119). StateFilter(None) — только no-FSM state (НЕ трогает
# admin FSM states — там admin_state_catchall_text). _is_admin check —
# non-admin silent return, проваливается в client_router fallback (OK
# для клиентов — /book hint правильный).
# ============================================================


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def admin_no_state_catchall_text(message: Message) -> None:
    """Catch-all для произвольного текста от админа в no-FSM state.

    Перехватывает произвольный текст (НЕ команды) от админа в State(None),
    подсказывает /menu вместо того, чтобы проваливаться в client_router
    fallback ("Начните запись через /book" — клиентский hint для админа).

    Non-admin → silent return, проваливается в client_router fallback (OK).
    /commands → НЕ матчит (~F.text.startswith("/")), идут в свои handlers.
    """
    if not _is_admin(message):
        return
    await message.answer("📋 /menu для действий")
