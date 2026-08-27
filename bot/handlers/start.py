from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardRemove

from bot.config import get_settings
from bot.keyboards.admin import admin_inline_menu

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Branch: master (ADMIN_ID) gets inline menu, client gets booking hint.

    state.clear() в начале — /start как универсальный "fresh start". Без этого
    /start в mid-FSM (admin или booking) показывал бы welcome, но state
    оставался dirty → confusing behavior (code-review W1, Session 5.9).

    ReplyKeyboardRemove в admin-ветке — убирает старую reply keyboard из
    прошлых сессий (до Этапа 3 была admin_keyboard reply keyboard). Без этого
    Telegram продолжает показывать старые кнопки /addslots /closeslot /today
    /week /services add снизу, даже после перехода на inline menu.

    TODO Ур. 2.6: extract to role middleware — DB lookup master by telegram_id,
    inject is_master into workflow_data, handler reads flag not settings.ADMIN_ID.
    """
    await state.clear()
    settings = get_settings()
    if message.from_user and message.from_user.id == settings.ADMIN_ID:
        # Сначала cleanup reply keyboard (если осталась из прошлой сессии).
        # Нельзя комбинировать ReplyKeyboardRemove и InlineKeyboardMarkup в
        # одном reply_markup — отправляем двумя сообщениями.
        await message.answer("👋", reply_markup=ReplyKeyboardRemove())
        await message.answer(
            "Привет, Екатерина! 👋\nУправление записями — кнопки ниже:",
            reply_markup=admin_inline_menu(),
        )
    else:
        await message.answer("Привет! Я бот для записи к парикмахеру.\nЗапишитесь командой /book")
