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
