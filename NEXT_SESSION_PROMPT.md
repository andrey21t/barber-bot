# NEXT_SESSION_PROMPT — barber-bot Блок 3 часть 3 (перенос записи)

> Дата: 2026-08-21 · Сессия: завершена после Блок 3 часть 2 (cancel_booking)
> Цель следующей сессии: Блок 3 часть 3 — перенос записи клиентом (FSM для новой даты/слота)

## Контекст проекта

**Репозиторий:** `~/PycharmProjects/barber-bot/` (личный, коммитить свободно по AGENTS.md § git-repo-categories)
**Стек:** Python 3.12, aiogram 3.x, SQLAlchemy 2.0 async, SQLite (dev) → Postgres (prod), APScheduler 3.x
**Спека (SSOT):** `~/PycharmProjects/barber-bot/spec.md` — читай перед каждой задачей, если код расходится со spec — прав spec, не код
**Формат работы:** `~/PycharmProjects/barber-bot/MY-VIBE-RULES.md` — dev-режим (без педагогики Уровня 2), резюме после блока, гейты: deep-analysis на нетривиальное → реализация → verify → code-review

## Что уже сделано (коммиты в main)

### Предыдущие сессии (до 2026-08-21)
- MVP skeleton: `bot/main.py`, `bot/handlers/start.py`, `bot/handlers/client.py` (booking flow)
- Services: `bot/services/booking.py` (create_booking), `bot/services/slots.py`, `bot/services/notifications.py`
- Middlewares: `bot/middlewares/session_timeout.py`
- Tests: test_booking.py, test_slots.py, test_notifications.py, test_scheduler.py (1 pre-existing failure: `test_on_startup_scan_phase_2_reschedules_upcoming` — красный, не регрессия, отдельный блок для починки)

### Сессия 2026-08-21 (Блоки 1-3 часть 2)
- **Блок 1**: Master-команды (`handlers/admin.py` + `services/admin.py` + `keyboards/admin.py` + правки `start.py`/`main.py`)
- **Блок 2**: Тесты + Code Review (LBTM → 6 фиксов: slot_date past validation, /week 8→7 дней, newline в render, escape service.name, empty hours, name length 255)
- **Блок 3 часть 1**: `/mybookings` — список записей клиента (без отмены/переноса, отложено)
- **Блок 3 часть 2**: **cancel_booking** (отмена записи клиентом):
  - `bot/services/booking.py`: +170 строк — `cancel_booking()` + 3 exception classes + `CancelResult` dataclass
  - `bot/keyboards/client.py`: +41 строк — `MyBookingsCancelCallbackData(prefix="mybook_cancel")` + `mybookings_keyboard()`
  - `bot/handlers/client.py`: +124 строк — `mybookings_msg` переписан с inline-кнопками + новый `mybookings_cancel_cb` handler
  - `tests/test_booking.py`: +235 строк — 4 новых теста (happy_path, too_late, not_owner, already_cancelled)
  - **Bug found in self-review** (user-triggered "проверь себя"): `mybookings_msg:441` сравнивал aware `datetime.now(UTC)` с naive `b.start_at - 24h` из SQLite → `TypeError` в продакшене при первом /mybookings. Та же бага что я только что пофиксил в `cancel_booking:332`. Фикс: strip tzinfo от now_utc перед сравнением (mirror сервиса). **Гейт handler tests НЕ ловил** — handler считается pure I/O (anti-overengineering правило 3), но этот баг был в display-логике partition cancelable, не в I/O — оправдание не сработало. Урок для следующего блока: партиционирование/математика в handler даже для display = candidate на тест.
  - Tests: 49 passed (full suite, без failures)
  - Code review: LGTM (main agent — subagent Communication Failure subagent-side; 0 Critical, 1 Warning: pre-existing timezone render pattern в `booking.start_at.astimezone(ZoneInfo(business_tz))` на naive SQLite datetime — тот же pattern в `create_booking:217`, фикс отложен до Postgres migration Урок 2.6)

## Что делать в следующей сессии

### Блок 3 часть 3 — ПЕРЕНОС записи клиентом

**Spec.md** (строки 41, 318, 408-409):
- `/mybookings` → inline-кнопка «Перенести» возле каждой записи (аналогично «Отменить», новый callback prefix `mybook_transfer`)
- Правило: `CANCEL_MIN_HOURS = 24` (config.py:23) — клиент может перенести только за 24+ часов до `start_at` (то же правило что и для отмены)
- При переносе:
  1. **FSM** для выбора новой даты (re-use date picker) → нового слота (re-use slot picker)
  2. UPDATE `Booking.status='transferred'` (НЕ cancel+new — UPDATE той же строки, spec.md 318) + `Booking.start_at` + `end_at` + `slot_id` (новый)
  3. UPDATE старого `Slot.status='open'` (освободить старый)
  4. UPDATE нового `Slot.status='booked'` (занять новый) — rowcount check (race protection)
  5. `scheduler.remove_job` для remind_24h + remind_1h (старые jobs)
  6. `scheduler.add_job` новые remind_24h + remind_1h с новым start_at (через `schedule_for_booking` с `replace_existing=True`)
  7. `NotificationLog` `master_transfer` (kind допускается models.py:178-182)
  8. Подтверждение клиенту: «✅ Запись перенесена на ...»

**Где делать:**
- `bot/services/booking.py` — новый метод `transfer_booking(session, booking_id, new_slot_id, client_id, scheduler, *, now_utc=None) -> TransferResult`
- `bot/handlers/client.py` — новые FSM states (или re-use BookingStates с доп. steps для transfer), callback handlers
- `bot/keyboards/client.py` — `MyBookingsTransferCallbackData(prefix="mybook_transfer")` + кнопка «Перенести» под каждой cancelable записью
- `tests/test_booking.py` — тесты на transfer_booking (happy path, <24ч отказ, не-владелец, slot занят, scheduler cleanup + add новые)

**Контракты (не нарушать) — заимствовать из cancel_booking + create_booking:**
- `state.clear()` BEFORE `event.answer` (race condition, MY-VIBE-RULES.md 24)
- HTML escape: `client_name_snapshot` + `service_title_snapshot` сохраняются (НЕ пере-escape — уже escaped в DB)
- Timezone: `start_at` UTC в DB (naive в SQLite), для `CANCEL_MIN_HOURS` сравнение в naive UTC (pattern `admin.py:155-156`, как в `cancel_booking:332-334`)
- SQLite race: `UPDATE slot SET status='booked' WHERE status='open'` + rowcount check (как в `create_booking:184-196`)
- NotificationLog UNIQUE(booking_id, kind) — `master_transfer` через SAVEPOINT (booking.py:198-212 pattern)
- Scheduler: `remove_jobs_for_booking` для старых jobs + `schedule_for_booking(scheduler, booking_id, new_start_at)` для новых (`replace_existing=True` идемпотентен)

### Pre-existing failures (НЕ трогать)

- **`test_scheduler.py::test_on_startup_scan_phase_2_reschedules_upcoming`** — **FLAKY** (зависит от времени суток, не регрессия). Тест создаёт "tomorrow 14:00 MSK = tomorrow 11:00 UTC", on_startup_scan смотрит на 25h вперёд. Если current time >= 10:00 UTC → tomorrow 11:00 через <25h → проходит. Если current time < 10:00 UTC → вне окна → падает. Не чинить в этой сессии — отдельный блок (mock now_utc в on_startup_scan или freeze_time).
- **W6/W7/S2** (отложено с Блока 2) — minor, при миграции на Postgres.
- **Handlers тесты** для master-команд — pure I/O, anti-overengineering правило 3. НО: если в handler есть display-логика (математика, partition, datetime сравнение) — кандидат на тест (урок из Блока 3 часть 2 бага).

## Quick start следующей сессии (prompt для opencode)

```
Продолжаем barber-bot Блок 3 часть 3 — перенос записи клиентом.

Прочитай ~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md (этот файл) — там полный контекст.
Затем открой spec.md строки 41, 318, 408-409 — контракты переноса.
Потом bot/services/booking.py — там свежий pattern для cancel_booking (UPDATE + rowcount + NotificationLog SAVEPOINT + remove_jobs_for_booking). Transfer_booking — это cancel_booking (для старого slot/scheduler) + create_booking (для нового slot/scheduler) в одной транзакции.

Сделай deep-analysis (Pass 1-4) на transfer_booking — это state-change + scheduler side effects (remove старые + add новые jobs).
Реализуй: service transfer_booking → keyboard button «Перенести» → FSM handlers для выбора новой даты/слота → tests на service.
Гейты: verify (pytest+ruff+mypy зелёные) → code-review subagent → резюме блока.

Если останется ресурс — почини pre-existing test_scheduler failure (но это отдельный блок, не основная задача).
```

## Файлы для быстрого ориентирования

| Файл | Что | Строк |
|---|---|---|
| `spec.md` | SSOT — строки 41, 318, 408-409 — контракты переноса | 541 |
| `MY-VIBE-RULES.md` | Формат работы + FSM edge cases + rules | 79 |
| `bot/handlers/client.py` | Booking flow + /mybookings + cancel + (next: transfer FSM) | 543 |
| `bot/services/booking.py` | create_booking + cancel_booking (pattern для transfer) | 408 |
| `bot/services/admin.py` | get_today/week_bookings, create_service, get_client_bookings | 160 |
| `bot/services/slots.py` | add_slots, close_slot, get_available_slots | 102 |
| `bot/keyboards/client.py` | Inline keyboards + MyBookingsCancelCallbackData (next: +Transfer) | 159 |
| `bot/models.py` | 7 таблиц — Booking (status confirmed/transferred/cancelled), Slot, NotificationLog с kind CHECK | 184 |
| `bot/config.py` | Settings: CANCEL_MIN_HOURS=24, REMINDER_24H_BEFORE, REMINDER_1H_BEFORE | 29 |
| `scheduler.py` | build_scheduler, schedule_for_booking, remove_jobs_for_booking, on_startup_scan | 126 |
| `tests/test_booking.py` | Pattern для тестов booking service (happy path, idempotency, errors) + cancel_booking pattern | 462 |
| `tests/test_admin.py` | Pattern для тестов на services (timezone edge cases, _utc_naive helper) | ~480 |

## Гейты (напоминание)

- **Deep-analysis** на transfer_booking (logic-change + scheduler side effects) — Pass 1-4, не skip
- **Verify-and-fix** перед «готово»: pytest + ruff + mypy — все зелёные
- **Code-review** subagent после verify (logic-change) — по AGENTS.md § write-actions-subagents правило 7
- **Pre-push ревью**: НЕ нужно — barber-bot личный репо (AGENTS.md § git-repo-categories)
- **Коммитить свободно** — pet-проект, без переспроса

## Состояние тестов на момент сохранения

```
pytest: 49 passed (full suite, no failures)
ruff: All checks passed
mypy: 30 source files, no issues
```

Note: `test_scheduler::test_on_startup_scan_phase_2_reschedules_upcoming` — FLAKY (time-of-day dependent), иногда падает. Не регрессия от cancel_booking.

## Pre-existing Warning (зафиксировано, не блокирующее)

`booking.start_at.astimezone(ZoneInfo(business_tz))` на naive datetime из SQLite (booking.py:380 в cancel_booking, booking.py:217 в create_booking) — интерпретирует naive как system-local TZ. Корректно на Render (UTC), баг проявится при миграции на Postgres с TIMESTAMP WITHOUT TZ на сервере вне UTC. Фикс — Урок 2.6 (Postgres migration).
