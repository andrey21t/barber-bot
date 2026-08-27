"""Tests for bot.handlers.start — /start command with admin/client branch.

Coverage (AUTONOMOUS_COVERAGE_PROMPT.md T6):
- /start from admin → welcome message + admin_keyboard
- /start from client → client welcome message ('Запишитесь командой /book')

Pattern: direct handler invocation with mock Message + User. No DB, no FSM.
Gates: qa-verify-and-fix only (trivial, deep-analysis + code-review skipped per
AUTONOMOUS_COVERAGE_PROMPT.md T6).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message, User
from bot.config import get_settings
from bot.handlers import start as start_handlers

ADMIN_TG_ID: int = get_settings().ADMIN_ID
NON_ADMIN_TG_ID: int = 999111222


def _make_user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def _make_message(user_id: int) -> MagicMock:
    """Mock aiogram.Message — answer is AsyncMock for assertion."""
    msg = MagicMock(spec=Message)
    msg.from_user = _make_user(user_id)
    msg.answer = AsyncMock()
    return msg


def _make_state() -> MagicMock:
    """Mock FSMContext — clear() is AsyncMock (cmd_start calls state.clear()).

    Этап 3 fix W1 (code-review 5.9): cmd_start now clears state at entry.
    """
    state = MagicMock()
    state.clear = AsyncMock()
    return state


def _answer_text(msg: MagicMock) -> str:
    args = msg.answer.call_args.args
    return str(args[0]) if args else str(msg.answer.call_args.kwargs.get("text", ""))


# ============================================================
# /start — admin branch
# ============================================================


@pytest.mark.asyncio
async def test_cmd_start_admin_shows_welcome_and_inline_menu() -> None:
    """/start from admin (ADMIN_ID) → cleanup reply keyboard + inline menu.

    Этап 3 (Session 5.9): cmd_start показывает admin_inline_menu() (5 inline
    кнопок) вместо admin_keyboard() (reply keyboard). Текст больше НЕ
    перечисляет /addslots /closeslot /today /week /services — только welcome.

    Fix (post-deploy): admin-ветка отправляет 2 сообщения — сначала cleanup
    (ReplyKeyboardRemove), потом inline menu. Telegram не убирает старую
    reply keyboard автоматически, нужен явный ReplyKeyboardRemove.
    """
    msg = _make_message(user_id=ADMIN_TG_ID)
    state = _make_state()
    await start_handlers.cmd_start(msg, state)

    # 2 вызова answer: (1) cleanup reply keyboard, (2) inline menu
    assert msg.answer.await_count == 2
    state.clear.assert_awaited_once()  # W1 fix: /start clears FSM state

    # Второй вызов — inline menu
    second_call = msg.answer.await_args_list[1]
    text = str(second_call.args[0]) if second_call.args else str(
        second_call.kwargs.get("text", "")
    )
    assert "Привет, Екатерина" in text
    # Этап 3: текст больше НЕ перечисляет команды (только welcome)
    assert "/addslots" not in text
    assert "/closeslot" not in text
    reply_markup = second_call.kwargs.get("reply_markup")
    assert reply_markup is not None, "admin /start must include inline menu"
    from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardRemove
    assert isinstance(reply_markup, InlineKeyboardMarkup), "must be inline keyboard"

    # Первый вызов — cleanup reply keyboard
    first_call = msg.answer.await_args_list[0]
    first_rm = first_call.kwargs.get("reply_markup")
    assert isinstance(first_rm, ReplyKeyboardRemove), (
        "first message must cleanup old reply keyboard"
    )


# ============================================================
# /start — client branch
# ============================================================


@pytest.mark.asyncio
async def test_cmd_start_client_shows_booking_hint() -> None:
    """/start from non-admin → 'Привет! Я бот для записи...' + '/book' hint.

    No admin_keyboard — client gets booking instructions only.
    """
    msg = _make_message(user_id=NON_ADMIN_TG_ID)
    state = _make_state()
    await start_handlers.cmd_start(msg, state)

    msg.answer.assert_awaited_once()
    state.clear.assert_awaited_once()  # W1 fix: /start clears FSM state (any branch)
    text = _answer_text(msg)
    assert "Привет" in text
    assert "бот для записи к парикмахеру" in text
    assert "/book" in text
    # No admin_keyboard for client
    reply_markup = msg.answer.call_args.kwargs.get("reply_markup")
    assert reply_markup is None, "client /start must NOT include admin_keyboard"
