# NEXT_SESSION_PROMPT — barber-bot Блок 3 (отмена + перенос)

> Дата: 2026-08-21 · Сессия: завершена на 22% ресурса после /mybookings (часть 1)
> Цель следующей сессии: Блок 3 части 2-3 — отмена и перенос записи клиентом

## Контекст проекта

**Репозиторий:** `~/PycharmProjects/barber-bot/` (личный, коммитить свободно по AGENTS.md § git-repo-categories)
**Стек:** Python 3.12, aiogram 3.x, SQLAlchemy 2.0 async, SQLite (dev) → Postgres (prod), APScheduler 3.x
**Спека (SSOT):** `~/PycharmProjects/barber-bot/spec.md` — читай перед каждой задачей, если код расходится со spec — прав spec, не код
**Формат работы:** `~/PycharmProjects/barber-bot/MY-VIBE-RULES.md` — dev-режим (без педагогики Уровня 2), резюме после блока, гейты: deep-analysis на нетривиальное → реализация → verify → code-review

## Что уже сделано (коммиты в main)

### Предыдущие сессии (до 2026-08-21)
- MVP skeleton: `bot/main.py`, `bot/handlers/start.py`, `bot/handlers/client.py` (booking flow /book → date → slot → name → service → confirm, 8 handlers)
- Services: `bot/services/booking.py` (create_booking), `bot/services/slots.py` (add_slots, close_slot, get_available_slots), `bot/services/notifications.py`
- Middlewares: `bot/middlewares/session_timeout.py`
- Tests: test_booking.py, test_slots.py, test_notifications.py, test_scheduler.py (1 pre-existing failure: `test_on_startup_scan_phase_2_reschedules_upcoming` — красный до наших правок, не регрессия, отдельный блок для починки)

### Сессия 2026-08-21 (Блоки 1-3 часть 1)
- **Блок 1**: Master-команды (`handlers/admin.py` + `services/admin.py` + `keyboards/admin.py` + правки `start.py`/`main.py`)
- **Блок 2**: Тесты + Code Review (LBTM → 6 фиксов: slot_date past validation, /week 8→7 дней, newline в render, escape service.name, empty hours, name length 255)
- **Блок 3 часть 1**: `/mybookings` — список записей клиента (без отмены/переноса)

## Что делать в следующей сессии

### Блок 3 часть 2 — ОТМЕНА записи клиентом (приоритет)

**Spec.md** (строки 41, 298, 318):
- `/mybookings` → inline-кнопка «Отменить» возле каждой записи (или текстом: `/cancel <booking_id>`)
- Правило: `CANCEL_MIN_HOURS = 24` (из config.py:23) — клиент может отменить только за 24+ часов до `start_at`
- Если < 24ч — отказ с сообщением «Отмена возможна только за 24+ часов до записи»
- При отмене:
  1. UPDATE `Booking.status='cancelled'` WHERE id=? AND client_id=? (владелец-проверка)
  2. UPDATE `Slot.status='open'` WHERE id=booking.slot_id (освободить слот)
  3. `scheduler.remove_job` для remind_24h + remind_1h (cleanup, не полагаться на UNIQUE guard — spec.md 317)
  4. Уведомление мастеру: «Отмена: ...» (через `notifications_log` с UNIQUE guard, kind=`master_cancel`)
  5. Подтверждение клиенту: «✅ Запись отменена»

**Где делать:**
- `bot/services/booking.py` — новый метод `cancel_booking(session, booking_id, client_id, scheduler, now_utc=None) -> CancelResult`
- `bot/handlers/client.py` — callback-хендлер для inline-кнопки «Отменить» (или text `/cancel <uuid>` после `/mybookings`)
- `bot/keyboards/client.py` — расширить `mybookings` рендер: inline-кнопка `[Отменить]` возле каждой записи (callback `book_cancel:<booking_id>`)
- `tests/test_booking.py` — тесты на cancel_booking (happy path, <24ч отказ, не-владелец отказ, уже cancelled)

**Контракты (не нарушать):**
- `state.clear()` BEFORE `event.answer` (race condition, MY-VIBE-RULES.md 24)
- HTML escape: `client_name_snapshot` уже экранирован в DB (booking.py:158), render без re-escape
- Timezone: `start_at` UTC в DB, для `CANCEL_MIN_HOURS` сравнение в UTC (напрямую, не через LOCAL)
- SQLite race: `UPDATE ... WHERE status='confirmed'` + rowcount check (как в booking.py:184-196)
- NotificationLog UNIQUE(booking_id, kind) — `master_cancel` через SAVEPOINT (booking.py:198-212 pattern)

### Блок 3 часть 3 — ПЕРЕНОС записи (если хватит ресурса)

- Самая большая часть: FSM для выбора новой даты/слота
- Spec.md 41, 318: «перенос (>24ч)» — те же 24ч правило что и отмена
- Реализация: `Booking.status='transferred'` (НЕ cancel+new — UPDATE той же строки, spec.md 318)
- Scheduler: `remove_job` старые remind_24h + remind_1h + `add_job` новые (spec.md 318)
- NotificationLog: kind=`master_transfer`
- Если ресурса не хватит — оставить на следующую сессию, не начинать на 5%

## Что НЕ трогать

- **Pre-existing failure** `test_scheduler.py::test_on_startup_scan_phase_2_reschedules_upcoming` — отдельный блок, scheduler-сторона. Не чинить в этой сессии.
- **W6** (Decimal precision validation для create_service) — minor, отложено, при миграции на Postgres
- **W7** (IntegrityError wrap в add_slots) — minor, MVP master_id всегда валиден
- **S2** (`name.replace("_", " ")` для /services add) — documented MVP limit, кавычки в v2
- **Handlers тесты** для master-команд — pure I/O, anti-overengineering правило 3

## Quick start следующей сессии (prompt для opencode)

```
Продолжаем barber-bot Блок 3 часть 2 — отмена записи клиентом.

Прочитай ~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md (этот файл) — там полный контекст.
Затем открой spec.md строки 41, 298, 318, 318 — контракты отмены/переноса.
Потом bot/services/booking.py — там есть pattern для cancel_booking (UPDATE + rowcount + NotificationLog SAVEPOINT).

Сделай deep-analysis (Pass 1-4) на cancel_booking — это logic-change с scheduler side effects.
Реализуй: service cancel_booking → handler inline-кнопки → tests на service.
Гейты: verify (pytest+ruff+mypy зелёные) → code-review subagent → резюме блока.

Если останется ресурс — Блок 3 часть 3 (перенос записи, FSM).
```

## Файлы для быстрого ориентирования

| Файл | Что | Строк |
|---|---|---|
| `spec.md` | SSOT — строки 41, 200-213, 298-320, 401 — контракты | 541 |
| `MY-VIBE-RULES.md` | Формат работы + FSM edge cases + rules | 90+ |
| `bot/handlers/client.py` | Booking flow + /mybookings + /cancel (FSM) + fallback | ~440 |
| `bot/handlers/admin.py` | Master-команды: /addslots /closeslot /today /week /services | ~370 |
| `bot/services/booking.py` | create_booking — pattern для cancel_booking | 237 |
| `bot/services/admin.py` | get_today/week_bookings, create_service, get_client_bookings | ~150 |
| `bot/services/slots.py` | add_slots, close_slot, get_available_slots | ~100 |
| `bot/models.py` | 7 таблиц — Booking, Slot, NotificationLog с индексами | 184 |
| `bot/config.py` | Settings: CANCEL_MIN_HOURS=24, REMINDER_24H_BEFORE, REMINDER_1H_BEFORE | 29 |
| `bot/keyboards/client.py` | Inline keyboards: date_picker, slot_picker, confirm | 122 |
| `tests/test_admin.py` | Pattern для тестов на services (timezone edge cases, _utc_naive helper) | ~480 |
| `tests/test_booking.py` | Pattern для тестов на booking (happy path, idempotency, errors) | 227 |

## Гейты (напоминание)

- **Deep-analysis** на cancel_booking (logic-change + scheduler side effects) — Pass 1-4, не skip
- **Verify-and-fix** перед «готово»: pytest + ruff + mypy — все зелёные
- **Code-review** subagent после verify (logic-change) — по AGENTS.md § write-actions-subagents правило 7
- **Pre-push ревью**: НЕ нужно — barber-bot личный репо (AGENTS.md § git-repo-categories)
- **Коммитить свободно** — pet-проект, без переспроса

## Состояние тестов на момент сохранения

```
pytest: 44 passed / 1 deselected (pre-existing scheduler failure)
ruff: All checks passed
mypy: 30 source files, no issues
```
