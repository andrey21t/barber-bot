"""Reply keyboards for master (admin).

Spec.md 245: reply-клавиатура для мастера с командами
/addslots /closeslot /today /week /services.

Shown to ADMIN_ID on /start. Client never sees this keyboard — only inline buttons
in booking flow (keyboards/client.py).
"""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def admin_keyboard() -> ReplyKeyboardMarkup:
    """Reply keyboard with 5 master commands. Resize + one-time per spec.md 245."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/addslots"), KeyboardButton(text="/closeslot")],
            [KeyboardButton(text="/today"), KeyboardButton(text="/week")],
            [KeyboardButton(text="/services add")],
        ],
        resize_keyboard=True,
        is_persistent=False,
    )
