"""Booking flow handlers — Pure I/O layer (Telegram API + db session).

Contract (deep-analysis-protocol Pass 3):
- NO business logic in handlers — validation, html.escape, timezone conversion live in services
- 7 handlers + 1 fallback (no_state_fallback for State(None) after bot restart):
  1. cmd_book: /book, StateFilter(None) → selecting_date
  2. date_cb: callback book_date:<iso>, StateFilter(selecting_date) → selecting_slot
  3. slot_cb: callback book_slot:<uuid>, StateFilter(selecting_slot) → entering_name
  4. name_msg: text, StateFilter(entering_name) → entering_service
  5. service_msg: text, StateFilter(entering_service) → confirming
  6. confirm_cb: callback book_confirm, StateFilter(confirming) → State(None) + create_booking
  7. cancel_msg: /cancel, StateFilter("*") → state.clear() + message
  + no_state_fallback: State(None), F.text, ~F.text.startswith("/") → "Начните через /book"

Invariants (spec.md + MY-VIBE-RULES.md):
- state.clear() BEFORE event.answer (race condition)
- /cancel handler registered BEFORE /mybookings in router order (spec 491)
- create_booking exceptions caught: SlotAlreadyBookedError, SlotInPastError, SlotClosedError
- Bot restart mid-FSM → MemoryStorage loses state → no_state_fallback catches
"""

import logging
from datetime import date
from uuid import UUID

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from scheduler import schedule_for_booking

from bot.config import get_settings
from bot.db import async_session_factory
from bot.keyboards.client import (
    BookConfirmCallbackData,
    BookDateCallbackData,
    BookSlotCallbackData,
    confirm_keyboard,
    date_picker_keyboard,
    slot_picker_keyboard,
)
from bot.models import Slot
from bot.schemas import BookingCreate
from bot.services.booking import (
    SlotAlreadyBookedError,
    SlotClosedError,
    SlotInPastError,
    create_booking,
)
from bot.services.slots import get_available_slots
from bot.states import BookingStates

logger = logging.getLogger(__name__)

router = Router(name="client")


# ============================================================
# 1. cmd_book — entry point (/book)
# ============================================================
@router.message(Command("book"), StateFilter(None))
async def cmd_book(message: Message, state: FSMContext) -> None:
    """Show date picker — next 7 days."""
    await state.set_state(BookingStates.selecting_date)
    await message.answer(
        "📅 Выберите дату записи:",
        reply_markup=date_picker_keyboard(days_ahead=7),
    )


# ============================================================
# 2. date_cb — user picked a date → show available slots
# ============================================================
@router.callback_query(
    BookDateCallbackData.filter(), StateFilter(BookingStates.selecting_date)
)
async def date_cb(
    callback: CallbackQuery,
    callback_data: BookDateCallbackData,
    state: FSMContext,
) -> None:
    """User selected a date — show available slots for that date."""
    iso = callback_data.iso
    try:
        slot_date = date.fromisoformat(iso)
    except ValueError:
        await callback.answer("Невалидная дата")
        return

    settings = get_settings()
    async with async_session_factory() as session:
        # Single-master MVP: master_id from settings (or first active master)
        # TODO Урок 2.4: real master lookup via business.telegram_owner_id
        from sqlalchemy import select

        from bot.models import Master

        stmt = select(Master).where(Master.telegram_id == settings.ADMIN_ID).limit(1)
        result = await session.execute(stmt)
        master = result.scalar_one_or_none()
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
                reply_markup=date_picker_keyboard(days_ahead=7),
            )
        await callback.answer()
        return

    # Save selected date in FSM for later use.
    # NOTE: master_id intentionally NOT saved here — confirm_cb re-fetches master
    # via Master.telegram_id == settings.ADMIN_ID (lines 264-267). Storing it would
    # be dead data (master could be deactivated between steps; re-fetch is canonical).
    await state.update_data(selected_date=iso)
    await state.set_state(BookingStates.selecting_slot)
    if callback.message is not None:
        await callback.message.answer(
            "Выберите время:",
            reply_markup=slot_picker_keyboard(slots),
        )
    await callback.answer()


# ============================================================
# 3. slot_cb — user picked a slot → ask for name
# ============================================================
@router.callback_query(
    BookSlotCallbackData.filter(), StateFilter(BookingStates.selecting_slot)
)
async def slot_cb(
    callback: CallbackQuery,
    callback_data: BookSlotCallbackData,
    state: FSMContext,
) -> None:
    """User selected a slot — save slot_id, ask for client name."""
    await state.update_data(slot_id=str(callback_data.slot_id))
    await state.set_state(BookingStates.entering_name)
    if callback.message is not None:
        await callback.message.answer(
            "На чьё имя записываем? (например: Паша, я сам, сын 5 лет)"
        )
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
@router.callback_query(
    BookConfirmCallbackData.filter(), StateFilter(BookingStates.confirming)
)
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
        stmt_m = (
            select(Master).where(Master.telegram_id == settings.ADMIN_ID).limit(1)
        )
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

    Spec.md 41: `/mybookings` → отмена (>24ч) или перенос (>24ч) — но в этом блоке
    только список. Отмена/перенос — следующий блок (TODO).

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

    # Render list — snapshots already escaped, strip newlines for list safety
    # (consistent with admin._render_bookings — html.escape(quote=False) skips \n).
    lines = ["📋 Ваши записи:", ""]
    for b in bookings:
        local_time = b.start_at.astimezone(tz)
        when = local_time.strftime("%d %b %Y, %H:%M")
        name = b.client_name_snapshot.replace("\n", " ")
        service = b.service_title_snapshot.replace("\n", " ")
        lines.append(f"• {when}\n  💇 {service}\n  👤 {name}")
    lines.append("")
    lines.append("Отмена/перенос — в разработке. Пока напишите мастеру напрямую.")
    await message.answer("\n".join(lines))


# ============================================================
# 9. no_state_fallback — bot restart mid-FSM, state lost
# ============================================================
@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def no_state_fallback(message: Message) -> None:
    """Catch text when no FSM state active (bot restart mid-FSM, MemoryStorage lost state)."""
    await message.answer("Начните запись через /book")
