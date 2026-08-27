from aiogram.fsm.state import State, StatesGroup


class BookingStates(StatesGroup):
    """FSM states для booking flow (spec.md 221-227).

    Порядок: date → slot → name → service → confirm.
    Single-master (BB-001): select_specialist skip'ается.
    """

    selecting_date = State()
    selecting_slot = State()
    entering_name = State()
    entering_service = State()
    confirming = State()


class TransferStates(StatesGroup):
    """FSM states для transfer flow (spec.md 318).

    Re-uses date_picker + slot_picker keyboards (BookDateCallbackData /
    BookSlotCallbackData) but distinct from BookingStates so handlers can
    branch on StateFilter. Only 2 steps (date → slot) — transfer skips
    client_name/service (snapshots from the existing booking are preserved).
    """

    selecting_date = State()
    selecting_slot = State()


class AdminStates(StatesGroup):
    """FSM states для admin inline-menu flow (Вариант B, spec.md 251).

    Multi-step flows для 3 из 5 кнопок админ-меню:
    - adding_slots: date (SimpleCalendar) → hours (text input) → create
    - closing_slot: date (SimpleCalendar) → hour (text input) → close
    - entering_service: name → duration → price → create

    Today/week — мгновенные callback handlers БЕЗ FSM (read-only queries).
    """

    adding_slots_date = State()
    adding_slots_hours = State()
    closing_slot_date = State()
    closing_slot_hour = State()
    entering_service_name = State()
    entering_service_duration = State()
    entering_service_price = State()
