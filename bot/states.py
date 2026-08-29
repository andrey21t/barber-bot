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

    Multi-step flows для 4 из 5 кнопок админ-меню:
    - adding_slots: date (SimpleCalendar) → pick window start (inline 30-min)
      → pick window end (inline 30-min) → confirm → open_workday (Этап 5.10
      inline-часы; replaces text input "11 12 13" with two-phase inline picker)
    - closing_slot: date (SimpleCalendar) → pick shrink end (inline 30-min)
      → confirm → update_workday shrink (Этап 5.10 inline-часы; replaces text
      input "14" with inline shrink picker)
    - opening_workday: date (SimpleCalendar) → start_time (HH:MM text) →
      end_time (HH:MM text) → open_workday (Этап 5.1, primary CREATE path —
      /addslots inline = MODIFY only, redirects here if WorkDay missing)
    - entering_service: name → duration → create (price убран в Session 5.10,
      мастер озвучивает цену отдельно в чате; поле Service.price nullable)

    Today/week — мгновенные callback handlers БЕЗ FSM (read-only queries).
    """

    adding_slots_date = State()
    picking_window_start = State()
    picking_window_end = State()
    confirming_window = State()
    closing_slot_date = State()
    picking_shrink_end = State()
    confirming_shrink = State()
    opening_workday_date = State()
    opening_workday_start = State()
    opening_workday_end = State()
    entering_service_name = State()
    entering_service_duration = State()


class AdminMoveStates(StatesGroup):
    """FSM states для admin_move flow (Этап 5.9, spec.md PLANS.md Gap 5).

    Admin (мастер) переносит ЛЮБОЙ booking через /today → [🔄 Перенести]
    кнопка. Distinct от TransferStates (client transfer) — admin skips 24h
    rule, skips client_id pin, уведомление КЛИЕНТУ (не мастеру).

    Flow:
    - selecting_date: admin navigates SimpleCalendar to pick destination date.
      Store: booking_id (str), is_admin_move=True flag implicit via StateFilter.
    - selecting_slot: admin picks 30-min slot from workday window.
      Store: booking_id, new_workday_id (str), new_start_minute (int).
    - confirming: admin sees summary, taps [✅ Перенести] → admin_move_booking.

    Reuses get_30min_slots_from_workday + get_available_slots_30 (slots.py:56,124)
    + select_workday (workday.py:221) + slot_picker_keyboard_30min (keyboards).
    Distinct from BookingStates/TransferStates via StateFilter — handler
    dispatch by state, NOT by is_admin_move flag (avoids flag pollution in
    _handle_simple_calendar, see client.py:168 is_transfer precedent).
    """

    selecting_date = State()
    selecting_slot = State()
    confirming = State()
