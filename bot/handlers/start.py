from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from bot.config import get_settings
from bot.keyboards.admin import admin_keyboard

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Branch: master (ADMIN_ID) gets reply-keyboard, client gets booking hint.

    TODO Ур. 2.6: extract to role middleware — DB lookup master by telegram_id,
    inject is_master into workflow_data, handler reads flag not settings.ADMIN_ID.
    """
    settings = get_settings()
    if message.from_user and message.from_user.id == settings.ADMIN_ID:
        await message.answer(
            "Привет, Екатерина! 👋\n"
            "Управление записями — кнопки внизу:\n"
            "• <b>/addslots</b> — открыть слоты на день\n"
            "• <b>/closeslot</b> — закрыть слот\n"
            "• <b>/today</b> — записи на сегодня\n"
            "• <b>/week</b> — записи на неделю\n"
            "• <b>/services add</b> — добавить услугу",
            reply_markup=admin_keyboard(),
        )
    else:
        await message.answer("Привет! Я бот для записи к парикмахеру.\nЗапишитесь командой /book")
