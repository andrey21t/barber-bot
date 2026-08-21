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


def _answer_text(msg: MagicMock) -> str:
    args = msg.answer.call_args.args
    return str(args[0]) if args else str(msg.answer.call_args.kwargs.get("text", ""))


# ============================================================
# /start — admin branch
# ============================================================


@pytest.mark.asyncio
async def test_cmd_start_admin_shows_welcome_and_admin_keyboard() -> None:
    """/start from admin (ADMIN_ID) → welcome 'Привет, Екатерина!' + admin_keyboard.

    The admin branch lists master commands (/addslots, /closeslot, /today, /week,
    /services add) and attaches admin_keyboard (reply keyboard).
    """
    msg = _make_message(user_id=ADMIN_TG_ID)
    await start_handlers.cmd_start(msg)

    msg.answer.assert_awaited_once()
    text = _answer_text(msg)
    assert "Привет, Екатерина" in text
    assert "/addslots" in text
    assert "/closeslot" in text
    assert "/today" in text
    assert "/week" in text
    assert "/services" in text
    # admin_keyboard is a reply_keyboard (ReplyKeyboardMarkup), passed as
    # reply_markup kwarg
    reply_markup = msg.answer.call_args.kwargs.get("reply_markup")
    assert reply_markup is not None, "admin /start must include admin_keyboard"


# ============================================================
# /start — client branch
# ============================================================


@pytest.mark.asyncio
async def test_cmd_start_client_shows_booking_hint() -> None:
    """/start from non-admin → 'Привет! Я бот для записи...' + '/book' hint.

    No admin_keyboard — client gets booking instructions only.
    """
    msg = _make_message(user_id=NON_ADMIN_TG_ID)
    await start_handlers.cmd_start(msg)

    msg.answer.assert_awaited_once()
    text = _answer_text(msg)
    assert "Привет" in text
    assert "бот для записи к парикмахеру" in text
    assert "/book" in text
    # No admin_keyboard for client
    reply_markup = msg.answer.call_args.kwargs.get("reply_markup")
    assert reply_markup is None, "client /start must NOT include admin_keyboard"
