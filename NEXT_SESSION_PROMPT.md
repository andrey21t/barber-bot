# NEXT_SESSION_PROMPT.md — Session 5.26 handoff

> Pet-project git free (AGENTS.md § git-repo-categories): commit + push без переспроса.
> VPS deploy через ssh root@188.225.82.248 (password rotated 27 авг, нет в .env).

## Контекст сессии

Сессия 5.25 (a-f) завершена: inline-picker для /addslots + remove shrink flow +
picker range 09:00-20:00 + auto-show menu + picker показывает только свободные
слоты с header «🔒 Занято» (donor-standard, winnerxxx13 Phase 2 deep dive).
Push `e22fe2c` на GitHub.

Сделан Phase 2 donor research (winnerxxx13/barbershop-telegram-bot) — 15
паттернов BB-100..BB-114, детально в `donor-research/donors/` + INSIGHT в
`donor-research/topics/booking-bot-architecture.md`.

Решено: продолжать /openweek (наш подход ЛУЧШЕ для плавающего графика
Екатерины — donor weekly schedule хуже для нестабильного графика). После
/openweek — 4 статуса booking (BB-107) как разблокировка для reviews/CSV.

## Цель Session 5.26

Реализовать 2 фичи:

### A. /openweek — batch открыть неделю (главная задача)

**Flow:**
1. `/openweek` → picker start (09:00-19:30) — reuses
   `admin_window_slot_picker_keyboard(mode="start")` (С bookmarker для
   picked_start_minute, но БЕЗ booked_slots — новый день, не modify).
   ИЛИ если хотим reuse с booked_slots: SELECT bookings для каждого дня
   недели (7 queries) — too much. Решение: picker start БЕЗ booked_slots
   (свободный выбор, protection на apply — WorkDayShrinkError по дню).
2. → picker end (start+30 → 20:00) — reuses
   `admin_window_slot_picker_keyboard(mode="end", picked_start_minute=...)`.
3. → 7 toggle-кнопок дней (Пн/Вт/Ср/Чт/Пт/Сб/Вс) + «✅ Открыть» + «❌ Отмена».
   Кнопка `AdminOpenWeekCallbackData` (prefix `admin_openweek_days`,
   `weekday: int 0-6`). Toggle UX: тап → ✅ Пн, повторный тап → Пн. State
   хранит set выбранных weekdays.
4. → «✅ Открыть» → применить `open_workday(start, end)` к каждому выбранному
   дню текущей недели. Пн = начало недели (`date - timedelta(days=weekday)`).
   WorkDayShrinkError → ❌ для этого дня, продолжаем остальные. Итог:
   `✅ Открыто: Пн 11-18, Ср 11-18, Пт 11-18\n❌ Вт: нельзя сузить, есть бронь`.

**FSM (bot/states.py):**
```python
class AdminStates(StatesGroup):
    # ... existing ...
    opening_week_start = State()  # NEW (after picking_window_end)
    opening_week_end = State()    # NEW
    opening_week_days = State()   # NEW (toggle days)
```

**Handlers (bot/handlers/admin.py):**
- `cmd_openweek` (Command("openweek"), StateFilter(None)) — entry point
- `admin_openweek_start_cb` (AdminWindowSlot30CallbackData, opening_week_start)
- `admin_openweek_end_cb` (AdminWindowSlot30CallbackData, opening_week_end)
- `admin_openweek_days_cb` (AdminOpenWeekCallbackData, opening_week_days) —
  toggle weekday in state
- `admin_openweek_confirm_cb` (F.data == "admin_openweek_confirm",
  opening_week_days) — apply to all selected days

**Keyboards (bot/keyboards/admin.py):**
- `AdminOpenWeekCallbackData(CallbackData, prefix="admin_openweek_days")` с
  `weekday: int` payload
- `admin_week_days_keyboard(selected: set[int]) -> InlineKeyboardMarkup` — 7
  toggle-кнопок + «✅ Открыть» (callback "admin_openweek_confirm") + «❌ Отмена»
  (callback "admin_openweek_cancel"). Selected weekdays помечены ✅.

**admin_inline_menu:** добавить кнопку «🗓 Открыть неделю»
(`AdminOpenWeekCallbackData` или новый `AdminOpenWeekEntryCallbackData` для
menu tap → picker start). 6 кнопок всего, adjust(2,2,1,1) или подобный layout.

**Tests (tests/test_admin_handlers.py):**
- `test_cmd_openweek_entry_shows_start_picker`
- `test_admin_openweek_start_cb_picks_start_shows_end_picker`
- `test_admin_openweek_end_cb_shows_days_toggle`
- `test_admin_openweek_days_cb_toggles_weekday` (state save)
- `test_admin_openweek_confirm_cb_applies_to_all_selected` (success case)
- `test_admin_openweek_confirm_cb_partial_failure` (1 day WorkDayShrinkError)
- `test_admin_openweek_confirm_cb_no_days_selected` (hint «выберите день»)
- `test_admin_openweek_cancel_cb_clears_state` (✅ меню после cancel)

### B. /closeday — закрыть конкретный день (маленькая, ~30 мин)

**Flow:**
1. `/closeday` (или кнопка «📅 Закрыть день» в admin_inline_menu) → SimpleCalendar.
2. → выбрать дату → если WorkDay is_active=False → «уже закрыт» hint.
3. → если есть active bookings → показать список + «Отменить все N записей?»
   confirm. Если bookings нет → сразу закрыть.
4. → confirm → `close_workday(workday_id)` (services/workday.py, УЖЕ ЕСТЬ) →
   `is_active=False` + отменить все bookings (status='cancelled') + уведомить
   клиентов через notifications.py (scheduler, skeleton уже есть).
5. → «✅ День закрыт, N записей отменено» + admin_inline_menu.

**Handlers:** `cmd_closeday` + `admin_closeday_calendar_cb` +
`admin_closeday_confirm_cb`.

**Tests:** 3-5 тестов (no workday, already closed, active bookings → confirm,
confirm → bookings cancelled + clients notified).

## Verify before commit

`.venv/bin/ruff check bot/ tests/` ✅ + `.venv/bin/mypy bot/ tests/` ✅ +
`.venv/bin/python -m pytest tests/` ✅ (expect ~390+ passed after new tests).

## Deploy

Push origin main → VPS deploy command:
```bash
ssh root@188.225.82.248 "cd /opt/barber-bot && git pull && docker compose up -d --build && docker compose restart bot && docker compose logs bot --tail 20"
```

## Smoke-test в Telegram

### /openweek:
1. `/menu` → кнопка «🗓 Открыть неделю»
2. → picker start (09:00-19:30, без booked_slots) → выбрать 11:00
3. → picker end → выбрать 18:00
4. → 7 toggle дней (Пн-Вс) → тапнуть Пн, Ср, Пт → ✅ все три подсвечены
5. → «✅ Открыть» → «✅ Открыто: Пн 11-18, Ср 11-18, Пт 11-18» + меню
6. Проверить через /today что записи на Пн/Ср/Пт появились (или через /addslots)

### /closeday:
1. Создать бронь на завтра (через /book как клиент со второго аккаунта)
2. `/closeday` → календарь → выбрать завтра
3. → «В этот день 1 запись: Иван, 14:00. Отменить?» → «✅ Да»
4. → «✅ День закрыт, 1 запись отменена» + меню
5. Проверить с клиента: уведомление «запись отменена мастером»
6. `/book` на эту дату → «мастер не работает в этот день»

## "Продолжи с прошлого места" — что сказать в начале следующей сессии

```
Продолжи Session 5.26 — реализуй /openweek + /closeday. Прочитай
NEXT_SESSION_PROMPT.md для деталей. Pet-project git free — commit + push без
переспроса. Начни с /openweek (главная задача): FSM states (opening_week_*),
handlers (cmd_openweek + 4 cb), keyboards (AdminOpenWeekCallbackData +
admin_week_days_keyboard), admin_inline_menu + кнопка «🗓 Открыть неделю»,
tests. После /openweek → /closeday (reuse close_workday, cancel bookings +
notify). Verify: ruff + mypy + pytest. Push + VPS deploy + smoke-test.
```
