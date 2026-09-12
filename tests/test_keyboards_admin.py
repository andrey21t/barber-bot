"""T3.2 — bot/keyboards/admin.py edge branches coverage.

Covers:
- admin_keyboard() — deprecated ReplyKeyboardMarkup (line 176-184)
- _minute_to_time() ValueError on out-of-range minute (line 569)
- admin_window_slot_picker_keyboard mode='end' without picked_start_minute → ValueError (line 481)
- admin_window_slot_picker_keyboard unknown mode → ValueError (line 485)
- admin_window_slot_picker_keyboard empty candidates → "Нет слотов" button (line 502-503)
- admin_window_slot_picker_keyboard with booked_slots → busy_minutes (494-497) + 🔒 label (516)
- render_booked_header with non-empty booked_slots (410-415)
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from bot.keyboards.admin import (
    BookedSlot,
    _minute_to_time,
    admin_keyboard,
    admin_window_slot_picker_keyboard,
    render_booked_header,
)


def test_admin_keyboard_returns_reply_keyboard_with_master_commands() -> None:
    """admin_keyboard() deprecated reply keyboard — covers line 176-184.

    Returns ReplyKeyboardMarkup with /addslots, /closeslot, /today, /week,
    /services add buttons. Used by old admin command handlers (legacy).
    """
    kb = admin_keyboard()
    assert isinstance(kb, ReplyKeyboardMarkup)
    flat_texts = [btn.text for row in kb.keyboard for btn in row]
    assert "/addslots" in flat_texts
    assert "/closeslot" in flat_texts
    assert "/today" in flat_texts
    assert "/week" in flat_texts
    assert "/services add" in flat_texts


def test_minute_to_time_negative_minute_raises_value_error() -> None:
    """_minute_to_time(-1) → ValueError 'out of range 0-1439' (line 569)."""
    with pytest.raises(ValueError, match="out of range"):
        _minute_to_time(-1)


def test_minute_to_time_too_large_minute_raises_value_error() -> None:
    """_minute_to_time(1440) → ValueError (line 569)."""
    with pytest.raises(ValueError, match="out of range"):
        _minute_to_time(1440)


def test_admin_window_slot_picker_mode_end_without_picked_start_raises() -> None:
    """mode='end' + picked_start_minute=None → ValueError (line 481)."""
    with pytest.raises(ValueError, match="requires picked_start_minute"):
        admin_window_slot_picker_keyboard(uuid4(), mode="end")


def test_admin_window_slot_picker_unknown_mode_raises() -> None:
    """mode='invalid' → ValueError 'unknown mode' (line 485)."""
    with pytest.raises(ValueError, match="unknown mode"):
        admin_window_slot_picker_keyboard(uuid4(), mode="invalid")


def test_admin_window_slot_picker_mode_end_with_start_ge_1200_returns_empty_button() -> None:
    """mode='end' + picked_start_minute=1200 → empty candidates →
    'Нет слотов' button (line 502-503)."""
    kb = admin_window_slot_picker_keyboard(
        uuid4(), mode="end", picked_start_minute=1200,
    )
    assert isinstance(kb, InlineKeyboardMarkup)
    flat_texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert flat_texts == ["Нет слотов"]


def test_admin_window_slot_picker_with_booked_slots_marks_busy_as_locked() -> None:
    """mode='start' with booked_slots → busy_minutes build (494-497) +
    🔒 label on busy slot (line 516).

    booked_slots=[BookedSlot(540..600)] → 09:00, 09:30 marked as 🔒, others free.
    """
    booked = [BookedSlot(
        start_minute=540,  # 09:00
        end_minute=600,    # 10:00 — half-open: 540, 570 busy (09:00, 09:30)
        client_name="Иван",
        service_title="Стрижка",
    )]
    kb = admin_window_slot_picker_keyboard(
        uuid4(), mode="start", booked_slots=booked,
    )
    assert isinstance(kb, InlineKeyboardMarkup)
    flat_texts = [btn.text for row in kb.inline_keyboard for btn in row]
    # 09:00 and 09:30 must be marked 🔒
    assert "🔒 09:00" in flat_texts
    assert "🔒 09:30" in flat_texts
    # 10:00 must be free (no 🔒)
    assert "10:00" in flat_texts
    assert not any(t == "🔒 10:00" for t in flat_texts)


def test_render_booked_header_with_non_empty_booked_slots_returns_locked_header() -> None:
    """render_booked_header with 1+ booked_slots →
    '🔒 Занято:' + '• HH:MM–HH:MM Имя (услуга)' (lines 410-415).
    """
    booked = [BookedSlot(
        start_minute=540,
        end_minute=600,
        client_name="Иван",
        service_title="Стрижка",
    )]
    header = render_booked_header(booked)
    assert "🔒" in header
    assert "Занято:" in header
    assert "09:00–10:00" in header
    assert "Иван" in header
    assert "Стрижка" in header


def test_render_booked_header_empty_returns_empty_string() -> None:
    """render_booked_header([]) → '' (line 408-409, sanity check)."""
    assert render_booked_header([]) == ""
