import html
import logging

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import select

from bot.config import get_settings
from bot.db import async_session_factory
from bot.keyboards.admin import admin_reply_keyboard
from bot.keyboards.client import client_reply_keyboard
from bot.models import Master

logger = logging.getLogger(__name__)
router = Router(name="start")


async def _resolve_master_name(telegram_id: int) -> str:
    """Lookup master.name from DB by telegram_id. Fallback to "мастер" if:
    - master not found (new server, migration not applied)
    - DB error (Postgres down)
    - master.name empty/whitespace

    HTML-escape to prevent TelegramBadRequest if name contains <, >, &
    (mirrors scheduler.py:189 pattern).
    """
    try:
        async with async_session_factory() as session:
            master = (
                await session.execute(
                    select(Master).where(Master.telegram_id == telegram_id)
                )
            ).scalar_one_or_none()
            if master and master.name.strip():
                # escape + newline squash — mirrors scheduler.py:189
                return html.escape(master.name, quote=False).replace("\n", " ")
    except Exception:
        logger.warning(
            "cmd_start: DB lookup failed for telegram_id=%s, fallback to 'мастер'",
            telegram_id,
            exc_info=True,
        )
    return "мастер"


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
        master_name = await _resolve_master_name(settings.ADMIN_ID)
        await message.answer(
            f"Привет, {master_name}! 👋\nКнопки внизу — 📋 Меню для остальных действий:",
            reply_markup=admin_reply_keyboard(),
        )
    else:
        await message.answer(
            "Привет! Я бот для записи к парикмахеру. 👋\n"
            "Кнопки внизу — записывайтесь или смотрите свои записи:",
            reply_markup=client_reply_keyboard(),
        )
