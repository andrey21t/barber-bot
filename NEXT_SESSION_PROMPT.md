# NEXT_SESSION_PROMPT — Session 5.20: шаг 5.8 `/slots` command + slot picker keyboard.

> Дата: 2026-08-29 · Session 5.19 завершена (commits `94de7c9` + `2aaab58` pushed, **326 passed + 2 skipped**). Deep-analysis Pass 1-4 для 5.19-prep был выполнен, code-reviewer вернул LGTM (W1+S1 fixed). Эта сессия (5.19-prep+impl, 2026-08-29) — Pass 1-4 + impl + code-review в одной сессии.
> Prod остаётся на `c64b31d` (Session 5.9). Миграция 005 НЕ накатывалась на prod — smoke-test на dev-копии обязателен (one-way door, данные Екатерины).

## ⚡ TL;DR для Session 5.20

**Цель 5.20:** шаг **5.8 `/slots` command + slot picker keyboard** — UI для нового WorkDay-based flow. /slots показывает клиенту 30-мин свободные окна (через `get_available_slots_30` из 5.6). Inline-кнопки рендерятся через `slot_picker_keyboard_30min` (5.4). Клик по кнопке → FSM → выбор услуги → подтверждение (по аналогии с cmd_book, но WorkDay-based).

**Главный артефакт:** этот файл + `PLANS.md` (Session 5.19 section). Читается ПЕРВЫМ для контекста.

**Risk-класс 5.20: logic (предварительно, уточнишь Pass 1).** Handler + UI — ≥2 файла (handlers/client.py + keyboards/client.py). Может потребоваться новая FSM state (или переиспользовать `BookingStates.selecting_slot` с другим callback). Без миграции, без security. Pass 1-4 обязателен в начале Session 5.20 (Pass 1 в новой сессии, не переиспользовать 5.19 — другая задача).

## Статус Этапа 5 Вариант B (на 2026-08-29):

- ✅ 5.2 WorkDay + миграция 005 (commits `90c0000` + `9345a7b`)
- ✅ 5.3 WorkDay invariants (commit `aedf1a0`, +7 → 281)
- ✅ 5.4 30-мин шаг + admin.py Booking.start_at filter (commit `069274d`, +5 → 286)
- ✅ 5.5 multi-client (commit `379ebd6`, +10 → 296)
- ✅ 5.1 `/openday` command + FSM (commit `147081e`, +13 → 309)
- ✅ 5.18 F1 UX-fix + 5 handler-тестов cmd_openday (commits `48d80d6` + `163ba33`, 314 passed)
- ✅ **5.19 5.6 «Мест нет» occupancy check** (commits `94de7c9` + `2aaab58`, **326 passed + 2 skipped**, code-review LGTM)
- 5.8 / 5.9 / 5.10 / aliases / миграция 006 — backlog

## 5.19 — что сделано (детали в PLANS.md)

- **`bot/services/slots.py` +87 строк:** `get_available_slots_30(session, workday, business_tz, *, now_utc=None) -> list[TimeSlot30]` — фильтр 30-мин grid из `get_30min_slots_from_workday` по occupancy. Half-open overlap (mirror `_check_multi_client_capacity`), status filter confirmed/transferred, naive SQLite → aware UTC normalize в Python loop, closed workday NOT filtered (separation of concerns — handler 5.8 решает).
- **`tests/test_slots.py` +315 строк:** 12 новых тестов (cap=1 happy + booking covers 1 slot, booking crosses 3 cells, cap=2 with 1/2 bookings, cancelled excluded, transferred included, touch edge, past filter all + partial, closed workday not filtered, window < 30min) + helpers `_insert_booking`, `_make_workday`, `_local_to_utc`, `_future_workdate`.
- **Code-reviewer subagent (BP-10 A2, independent review):** VERDICT LGTM, 0 Critical. W1 (transferred untested) + S1 (partial past filter) — fixed. S2/S3/S4 — skipped (pet project, non-critical).

## 5.8 `/slots` command — что проектировать в 5.20

### Контракт (из Plan of Work в PLANS.md)

`/slots <date>` (или FSM календарь → дата) показывает inline-клавиатуру 30-мин свободных слотов. Клик по кнопке → выбор услуги (если в команде не указана) → подтверждение → создание booking (через `create_booking`, не через slot_id, см. 5.3). Inline-кнопки disabled (или не рендерятся), если occupancy >= capacity (реализовано в 5.6).

### Гипотезы для deep-analysis Pass 1 (проверить в новой сессии)

- **FSM state:** переиспользовать `BookingStates.selecting_slot` или новый `BookingStates.selecting_slot_30`? `BookingStates` определён в `bot/states.py` (проверь). Если один state — callback routing по `BookSlotCallbackData` vs `BookSlot30CallbackData` (filter различает).
- **Calendar:** использовать существующий SimpleCalendar (`simple_calendar_cb:214`) или новый? Решение зависит от UX — /book и /slots могут разделять calendar, но slot picker разный.
- **callback_data:** `slot_picker_keyboard_30min:152` использует placeholder `"noop"` — 5.8 должен заменить на `BookSlot30CallbackData(workday_id, start_time_local_iso)`. Проверь лимит callback_data (64 байта в Telegram Bot API) — UUID (36) + ":" + time_iso (8) + prefix OK.
- **Service selection:** cmd_book просит название услуги (`entering_service` state). /slots может либо наследовать flow (выбор даты → выбор slot → выбор услуги → подтверждение), либо упростить (если в команде указана услуга). Решить в 5.20 — UX вопрос.
- **create_booking integration:** после клика по slot — `create_booking(BookingCreate(slot_id=None, workday_id=..., start_at=..., ...))`. Проверь contract BookingCreate — принимает ли workday_id без slot_id? Если нет — расширить (но это правка booking.py, не 5.8).

### Что НЕ трогать в 5.20

- `bot/services/slots.py` существующие функции (`get_30min_slots_from_workday`, `get_available_slots_30` — корректны, verified в 5.19).
- `bot/services/workday.py` (open/update/close/select_workday — 5.1+5.18 verified).
- `bot/services/booking.py` `_check_multi_client_capacity` (5.5 verified). **НО** возможно понадобится расширить `BookingCreate` contract — обсудить в Pass 1.
- `bot/handlers/admin.py` cmd_openday (F1 fix 5.18 — корректен).
- `tests/test_slots.py`, `tests/test_workday_service.py`, `tests/test_openday_handlers.py`, `tests/test_multi_client.py` — 0 регресса ожидается.

### Что НУЖНО сделать в 5.20 (приблизительно — уточни в Pass 1)

| Файл | Что | Объём (оценка) |
|---|---|---|
| `bot/handlers/client.py` | Новый `cmd_slots` handler + `BookSlot30CallbackData` callback handler + интеграция с существующим entering_service/confirming FSM | ~100-150 строк |
| `bot/keyboards/client.py` | `slot_picker_keyboard_30min` (существующий) — заменить `"noop"` на реальный `BookSlot30CallbackData.pack()`. Возможно disabled-state для занятых слотов (UX решение). | ~30 строк |
| `bot/keyboards/client.py` | Новая `BookSlot30CallbackData` factory (prefix="book_slot_30", workday_id: UUID, start_time_local: str) | ~15 строк |
| `tests/test_client_handlers.py` | Тесты на `cmd_slots` + callback handler (mirror pattern в test_openday_handlers) | ~150 строк |
| (опционально) `bot/services/booking.py` | Расширить `BookingCreate` для workday_id без slot_id — если текущий contract не позволяет | ~20 строк + тесты |

## Гейты для 5.20

- **deep-analysis-protocol:** logic change (handler + UI, ≥2 файла) → Pass 1-4 обязателен в НАЧАЛЕ Session 5.20 (Pass 1 risk-class: logic, не high-stakes — без security/state/concurrency, pet project). Pass 4 self-verify обязательный. deep-analysis-critic skip (logic < 150 строк, не high-stakes). Если Pass 1 выявит contract-правку в booking.py (BookingCreate без slot_id) — risk может стать high-stakes (изменение API/contract) → критик обязателен.
- **qa-verify-and-fix:** ruff + mypy --strict bot/+tests/ + pytest (326 → ~340, +14 handler тестов).
- **qa-code-review:** рекомендуется (новая logic в handler, UI flow change). Auto-trigger: logic-change scope к playtest+woodworking (barber-bot personal). Запускать по user-фразе «проверь себя» (как в 5.19) ИЛИ явно решить, что handler-UI logic достаточно Pass 1-4 для pet project.
- **Pre-push review:** skip (barber-bot personal pet-project).

## grep-карта для Session 5.20

### 5.8 — читать по якорям (file:line confirmed в 5.19-prep):

| Якорь | Что читать | Зачем |
|---|---|---|
| `PLANS.md:15` | 5.8 backlog entry | Что проектировать |
| `PLANS.md` Session 5.19 section | 5.6 + 5.19 итоги | Что готово (5.19) |
| `bot/handlers/client.py:100-194` | `cmd_book` + `_handle_simple_calendar` + `simple_calendar_cb` | Образец handler + FSM, переиспользовать calendar |
| `bot/handlers/client.py:213-248` | `simple_calendar_cb` (booking flow) | Calendar callback pattern |
| `bot/handlers/client.py:232-248` | `slot_cb` (BookSlotCallbackData handler) | Образец slot callback, переделать под 30-мин |
| `bot/handlers/client.py:249-322` | `name_msg` + `service_msg` (entering FSM) | Образец FSM transition |
| `bot/handlers/client.py:323-442` | `confirm_cb` + `cmd_cancel` | Подтверждение + cancel — переиспользовать |
| `bot/keyboards/client.py:35-49` | `BookSlotCallbackData` factory | Образец для `BookSlot30CallbackData` |
| `bot/keyboards/client.py:102-123` | `slot_picker_keyboard` (legacy) | Для сравнения с `slot_picker_keyboard_30min` |
| `bot/keyboards/client.py:126-154` | `slot_picker_keyboard_30min` (5.4) | Заменить `"noop"` на `BookSlot30CallbackData.pack()` |
| `bot/services/slots.py:124-200` | `get_available_slots_30` (5.6) | Основной вызов из handler 5.8 |
| `bot/services/slots.py:31-116` | `get_30min_slots_from_workday` + `TimeSlot30` | Grid gen + dataclass |
| `bot/services/workday.py:221-230` | `select_workday` | Поиск WorkDay по (master_id, work_date) для /slots |
| `bot/states.py` (проверь путь) | `BookingStates` enum | Решить — новый state или переиспользовать `selecting_slot` |
| `bot/services/booking.py` `BookingCreate` (проверь сигнатуру) | Contract create_booking | Возможная правка для workday_id без slot_id |
| `tests/test_client_handlers.py` | Образец handler тестов | Шаблон для cmd_slots тестов |
| `tests/test_openday_handlers.py` | Образец handler тестов на cmd_* (5.18) | Шаблон, более свежий |

## Quick start prompt для Session 5.20

```
Продолжаем barber-bot Session 5.20 — шаг 5.8 `/slots` command + slot picker
keyboard. Сначала deep-analysis Pass 1-4 (Pass 1 в новой сессии, не
переиспользовать 5.19 — другая задача).

Сначала:
1. Прочитай ~/PycharmProjects/barber-bot/PLANS.md (Session 5.19 section — что
   готово; line 15 — что 5.8 значит; Plan of Work п.8).
2. Прочитай ~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md (этот файл) —
   гипотезы для deep-analysis Pass 1 + что НЕ трогать + grep-карта.

== ЦЕЛЬ 5.20 ==
Шаг 5.8: /slots command + slot picker keyboard. /slots показывает клиенту
30-мин свободные окна (через get_available_slots_30 из 5.6) в виде
inline-клавиатуры. Клик по кнопке → FSM → выбор услуги → подтверждение →
create_booking (WorkDay-based, не slot_id-based).

== РИСК ==
MEDIUM (предварительно — уточнишь Pass 1). Handler + UI (≥2 файла).
Без миграции, без security, без concurrency. Если Pass 1 выявит правку
BookingCreate contract (workday_id без slot_id) — risk может стать
high-stakes (изменение API/contract) → deep-analysis-critic обязателен.

== ГЕЙТЫ ==
- deep-analysis: logic → Pass 1-4 обязателен в начале 5.20. Если high-stakes
  (contract правка) → deep-analysis-critic subagent pass обязателен.
- qa-verify-and-fix: ruff + mypy --strict bot/+tests/ + pytest (326 → ~340)
- qa-code-review: рекомендуется. Auto-trigger для personal repos по user-фразе
  «проверь себя» (как в 5.19).
- Pre-push: skip (pet-project git free per AGENTS.md § git-repo-categories)
- Commit message: feat(handlers): /slots command + slot picker keyboard
  (Этап 5.8)

== ФОРМАТ ==
Pet-project, git free per AGENTS.md § git-repo-categories. Commits без
переспроса после зелёных гейтов.

== ВАЖНО ==
НЕ копируй файлы целиком. Используй grep-карту выше. PLANS.md — читать
Session 5.19 section + line 15 (5.8 backlog).
```

## Исторический контекст (Sessions 5.16-5.19 — для быстрой ориентации)

> Детальные итоги сессий 5.10 и ранее — в архиве (не нужны для 5.20).
>
> **Сводка по последним сессиям:**
>
> - **Session 5.19** (commits `94de7c9` + `2aaab58`): 5.6 occupancy check + 12 тестов. Code-reviewer LGTM, W1+S1 fixed. **326 passed + 2 skipped.**
> - **Session 5.18** (commits `48d80d6` + `163ba33`): F1 UX-fix (variant B) + 5 handler-тестов cmd_openday. 314 passed.
> - **Session 5.17** (commit `147081e`): impl 5.1 /openday + workday service. 309 passed.
> - **Session 5.16** (commit `379ebd6`): impl 5.5 multi-client + LBTM fixes. 296 passed.
> - **Session 5.15** (БЕЗ коммитов): deep-analysis Pass 1-4 + critic iter 1 NEEDS_MORE_ANALYSIS → iter 2 plan.
> - Sessions 5.10 и ранее — в архиве (blueprint.md:30 convention, не нужны для 5.20).
