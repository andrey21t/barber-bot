from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.config import get_settings
from bot.keyboards.admin import admin_reply_keyboard
from bot.keyboards.client import client_reply_keyboard

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Branch: master (ADMIN_ID) gets always-on reply keyboard, client gets reply keyboard.

    state.clear() в начале — /start как универсальный "fresh start". Без этого
    /start в mid-FSM (admin или booking) показывал бы welcome, но state
    оставался dirty → confusing behavior (code-review W1, Session 5.9).

    Session 5.62 (пункт 5 от Екатерины): admin-ветка переведена с ReplyKeyboardRemove
    + inline menu на always-on reply keyboard (3 кнопки: 📋 Меню / 📅 Сегодня /
    🗓 Неделя). Inline menu больше не нужен в welcome — admin может тапнуть
    "📋 Меню" чтобы развернуть полный inline menu (7 actions) в сообщении.
    Решает жалобу Екатерины: inline menu уезжал вверх по чату, нужно скроллить
    или вводить /menu. Теперь 3 главные кнопки всегда видны внизу.

    ReplyKeyboardRemove больше НЕ нужен в admin-ветке (он удалял deprecated
    admin_keyboard reply keyboard из до-5.9 сессий). Начиная с 5.62 admin
    сразу получает admin_reply_keyboard — старая reply keyboard перебивается
    автоматически.

    TODO Ур. 2.6: extract to role middleware — DB lookup master by telegram_id,
    inject is_master into workflow_data, handler reads flag not settings.ADMIN_ID.
    """
    await state.clear()
    settings = get_settings()
    if message.from_user and message.from_user.id == settings.ADMIN_ID:
        await message.answer(
            "Привет, Екатерина! 👋\nКнопки внизу — 📋 Меню для остальных действий:",
            reply_markup=admin_reply_keyboard(),
        )
    else:
        await message.answer(
            "Привет! Я бот для записи к парикмахеру. 👋\n"
            "Кнопки внизу — записывайтесь или смотрите свои записи:",
            reply_markup=client_reply_keyboard(),
        )
