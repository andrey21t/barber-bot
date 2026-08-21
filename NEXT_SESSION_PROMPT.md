# NEXT_SESSION_PROMPT — barber-bot Блок 3 часть 4 — что дальше после transfer

> Дата: 2026-08-21 · Сессия завершена: transfer_booking service (commit 97da237) + UI handlers (commit c38087b)
> Все 77 тестов зелёные, ruff + mypy чисто. Code-review subagent — VERDICT LGTM.
> Цель следующей сессии: выбрать направление из списка ниже (нет блокирующих багов).

## Контекст проекта

**Репозиторий:** `~/PycharmProjects/barber-bot/` (личный, коммитить свободно по AGENTS.md § git-repo-categories)
**Стек:** Python 3.12, aiogram 3.x, SQLAlchemy 2.0 async, SQLite (dev) → Postgres (prod), APScheduler 3.x
**Спека (SSOT):** `~/PycharmProjects/barber-bot/spec.md` — строки 41, 318, 408-409 — контракты переноса (реализованы)
**Формат работы:** `~/PycharmProjects/barber-bot/MY-VIBE-RULES.md` — dev-режим (без педагогики), резюме после блока, гейты: deep-analysis на нетривиальное → реализация → verify → code-review

## Состояние на момент сохранения

### Завершено в этой сессии (2 коммита)

#### commit `97da237` — feat(transfer_booking): service + 11 tests
- `bot/services/booking.py`: `transfer_booking(session, booking_id, new_slot_id, client_id, scheduler, *, now_utc=None)` — 15 шагов, ~174 строк
  - Race protection: `Booking.start_at == old_start_at` в WHERE clause UPDATE booking — loser's WHERE не матчит после winner'a (Pass 3 [blocker] finding)
  - Bug fix: `old_slot_id = booking.slot_id` захвачен ПЕРЕД UPDATE (SQLAlchemy auto-mutate'ит booking.slot_id после UPDATE — без capture step 9 освобождал NEW slot, TransferResult.old_slot_id нёс new_slot_id)
  - master_transfer NotificationLog через SAVEPOINT idempotency
  - Scheduler remove_jobs + schedule_for_booking AFTER commit
- `tests/test_booking.py`: 11 transfer тестов (10 service edge cases + 1 static invariant lock для concurrent-transfer race protection через `inspect.getsource` check)

#### commit `c38087b` — feat(transfer): mybookings [Перенести] button + transfer FSM handlers + tests
- `bot/keyboards/client.py`: `MyBookingsTransferCallbackData(prefix="mybook_transfer", booking_id: UUID)` + `mybookings_keyboard` эмиттит 2 кнопки на booking в одной строке (adjust(2)): `[❌ Отменить <date>]` + `[🔄 Перенести <date>]`
- `bot/states.py`: `TransferStates(selecting_date, selecting_slot)` — отдельный от BookingStates (transfer не требует client_name/service — snapshots из существующего booking)
- `bot/handlers/client.py`: 3 новых handler'а после `no_state_fallback`:
  - `mybookings_transfer_cb` (entry, StateFilter(None)): валидирует booking exists + cancelable (>24h, та же partition что mybookings_msg), сохраняет transfer_booking_id в state, set_state(TransferStates.selecting_date), показывает date_picker_keyboard
  - `transfer_date_cb` (BookDateCallbackData, StateFilter(TransferStates.selecting_date)): re-uses date picker pattern, сохраняет selected_date, set_state(TransferStates.selecting_slot), показывает slot_picker_keyboard
  - `transfer_slot_cb` (BookSlotCallbackData, StateFilter(TransferStates.selecting_slot)): state.clear() BEFORE service call (race condition), resolves client by telegram_id, вызывает transfer_booking, мапит 8 exceptions на user-facing messages. На success: master notification + client confirmation с result.new_start_at в business tz
- `bot/services/booking.py` (code-review fixes после VERDICT LGTM):
  - **W1 fix**: `old_start_at` теперь stored в TransferResult как aware UTC (`replace(tzinfo=UTC)`) для cross-system TZ correctness — naive SQLite value неправильно интерпретировался как system-local TZ на Mac (Europe/Moscow), ломая "Перенос: old → new" master notification. cancel_booking имеет тот же pre-existing issue, отложено до Урок 2.6 (Postgres migration).
  - **W2 fix**: rowcount=0 в step 8 UPDATE теперь re-SELECT'ит booking.status для disambiguation — concurrent cancel (raise BookingAlreadyCancelledError) vs concurrent transfer (raise BookingAlreadyTransferredError). Без re-check concurrent cancel показывал "Запись уже перенесена" — misleading (booking отменена, не перенесена).
- `tests/test_client_handlers.py`: 6 новых transfer handler тестов + обновлены 2 старых под new keyboard (2 кнопки на booking: cancel + transfer)
- Verify: pytest 77 passed, ruff + mypy green

### Состояние тестов

```
pytest (full suite): 77 passed in 2.3s
ruff: All checks passed
mypy: 32 source files, no issues
```

Pre-existing: `test_scheduler::test_on_startup_scan_phase_2_reschedules_upcoming` — FLAKY (time-of-day dependent). Не регрессия.

## Что НЕ сделано (опционально, не блокирующее)

### Опц. 1 — Concurrent race runtime test (transfer)

Текущий `test_transfer_booking_concurrent_transfer_race_protection` — static-invariant lock (inspect source для `"Booking.start_at =="` в WHERE clause). Faithful runtime test:
- 2 sessions (нужен StaticPool + check_same_thread=False в fixture)
- asyncio.gather → 2 concurrent transfer_booking на одном booking
- Один выигрывает (rowcount=1, TransferResult), второй проигрывает (rowcount=0 → BookingAlreadyTransferredError)
- Аналогично `test_create_booking_idempotency_unique_guard` для UNIQUE-нарушения

Сложность: medium (~30-40 мин). Нужен 2-я session, careful fixture wiring.

### Опц. 2 — Урок 2.4 retry scenario (текущее NEXT_SESSION_PROMPT не указывал — отдельная задача из spec.md)

Spec.md упоминает retry при scheduler restart — `remove_jobs_for_booking` использует `suppress(Exception)` (idempotent). Тест `test_scheduler::test_on_startup_scan_phase_2_reschedules_upcoming` — FLAKY. Можно stabilise (freeze time via freezegun или сделать тест детерминированным).

### Опц. 3 — Урок 2.5 aiogram_calendar month-navigation

Сейчас `date_picker_keyboard(days_ahead=7)` — простые 7 кнопок. aiogram_calendar в deps для будущей month-navigation. Не блокирующее — 7 дней хватает для MVP.

### Опц. 4 — Урок 2.6 Postgres migration (наиболее значимое)

Pre-existing warning (зафиксировано, не блокирующее на dev):
- `booking.start_at.astimezone(ZoneInfo(business_tz))` на naive datetime из SQLite интерпретирует naive как system-local TZ. На Render (UTC) — корректно. Баг проявится при миграции на Postgres с TIMESTAMP WITHOUT TZ на сервере вне UTC.
- `get_client_bookings` в admin.py:152 — `datetime.now(tz=None)` возвращает NAIVE LOCAL (не UTC). Pre-existing warning — handler mybookings_msg:430-431 уже обходит это (использует `datetime.now(UTC)` и strip tzinfo). Service-уровень всё ещё naive local для filter.

Фикс — миграция на Postgres с TIMESTAMP WITH TZ (или явная конвертация naive → aware UTC в service). Это Урок 2.6, большая задача.

### Опц. 5 — Pre-existing Warning 2: cancel_booking тоже имеет W1 bug

W1 фикс применён только в `transfer_booking`. `cancel_booking` (строка 380) имеет тот же naive → astimezone issue: `local_time = booking.start_at.astimezone(ZoneInfo(business_tz))`. На Render (TZ=UTC) — корректно. На dev Mac (TZ=MSK) — "Отмена:" master notification показывает неправильное время.

Фикс — 1 строка: `local_time = booking.start_at.replace(tzinfo=UTC).astimezone(ZoneInfo(business_tz))`. Не блокирующее, но если хочется consistency с transfer — стоит применить.

## Что делать следующей сессии

Нет блокирующих багов. Выбери направление из списка выше. Рекомендация:
- Если есть ресурс ~30-40 мин → **Опц. 1** (concurrent race runtime test) — закрывает Pass 3 [blocker] finding "по-настоящему", не static inspect.
- Если есть ресурс ~5 мин → **Опц. 5** (cancel_booking W1 fix) — consistency с transfer, 1 строка.
- Если хочется большой фичи → **Опц. 4** (Postgres migration, Урок 2.6) — отдельная сессия, большая задача.

### Quick start prompt для opencode

```
Продолжаем barber-bot Блок 3 часть 4 — что после transfer.

Прочитай ~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md — там полный контекст:
- transfer_booking service + UI handlers — завершены и закоммичены (97da237, c38087b)
- Все 77 тестов зелёные, ruff + mypy чисто, code-review VERDICT LGTM

Нет блокирующих багов. Выбери направление (по приоритету ресурса):
1. Concurrent race runtime test (~30-40 мин) — 2 sessions + asyncio.gather,
   faithful test вместо static-invariant lock
2. cancel_booking W1 fix (~5 мин) — 1 строка для consistency с transfer
3. Урок 2.6 — Postgres migration (отдельная сессия, большая задача)

Гейты: deep-analysis НЕ нужен для Опц. 5 (1 строка, mirror существующего
pattern из transfer_booking). Для Опц. 1 — deep-analysis Pass 1-4 (новый
тестовый fixture, не тривиальный). Pre-push НЕ нужен (личный репо).
Коммитить свободно (pet-проект, AGENTS.md § git-repo-categories).
```

## Файлы для быстрого ориентирования

| Файл | Что | Строк |
|---|---|---|
| `spec.md` | SSOT — строки 41, 318, 408-409 — контракты переноса (реализованы) | 541 |
| `MY-VIBE-RULES.md` | Формат работы + FSM edge cases + rules | 79 |
| `bot/handlers/client.py` | Booking flow + /mybookings + cancel + **transfer FSM (NEW)** | ~840 |
| `bot/services/booking.py` | create_booking + cancel_booking + **transfer_booking (NEW, 174 строк, race protection + W1/W2 fixes)** | ~660 |
| `bot/services/admin.py` | get_today/week_bookings, create_service, get_client_bookings | 160 |
| `bot/services/slots.py` | add_slots, close_slot, get_available_slots | 102 |
| `bot/keyboards/client.py` | Inline keyboards + CancelCallbackData + **TransferCallbackData (NEW)** + mybookings_keyboard (2 кнопки/booking) | ~180 |
| `bot/models.py` | 7 таблиц — Booking (status confirmed/transferred/cancelled), Slot, NotificationLog с kind CHECK | 184 |
| `bot/states.py` | BookingStates + **TransferStates (NEW)** | 28 |
| `bot/config.py` | Settings: CANCEL_MIN_HOURS=24, REMINDER_24H_BEFORE, REMINDER_1H_BEFORE | 29 |
| `scheduler.py` | build_scheduler, schedule_for_booking, remove_jobs_for_booking, on_startup_scan | 126 |
| `tests/test_booking.py` | Pattern для booking service tests + cancel + **transfer (12 тестов)** | ~1080 |
| `tests/test_admin.py` | Pattern для тестов на services (timezone edge cases, _utc_naive helper) | 545 |
| `tests/test_client_handlers.py` | Handler тесты mybookings_msg + cancel_cb + **transfer_cb (6 тестов NEW)** | ~990 |

## Гейты (напоминание)

- **Deep-analysis** на transfer_booking — ВЫПОЛНЕН в прошлой сессии (Pass 1-4, verdict pass)
- **Verify-and-fix** перед «готово»: pytest + ruff + mypy — все зелёные (✓ сделано)
- **Code-review** subagent — VERDICT LGTM, W1+W2 фиксы применены (✓ сделано)
- **Pre-push ревью**: НЕ нужно — barber-bot личный репо (AGENTS.md § git-repo-categories)
- **Коммитить свободно** — pet-проект, без переспроса

## Pre-existing Warnings (зафиксировано, не блокирующее на dev)

### W1 — naive datetime + astimezone (в transfer_booking ИСПРАВЛЕНО, в cancel_booking — нет)

`booking.start_at.astimezone(ZoneInfo(business_tz))` на naive datetime из SQLite интерпретирует naive как system-local TZ. На Render (TZ=UTC) — корректно. На dev Mac (TZ=MSK) — "Отмена:" master notification показывает неправильное время.

**Фикс в transfer_booking (commit c38087b):** `old_start_at.replace(tzinfo=UTC).astimezone(...)` — перед astimezone явно отметь naive как UTC.

**Cancel_booking (строка 380) — НЕ починено.** 1 строка: `local_time = booking.start_at.replace(tzinfo=UTC).astimezone(ZoneInfo(business_tz))`. Опц. 5 выше.

### W2 — get_client_bookings (admin.py:152)

`get_client_bookings` в admin.py:152 — `datetime.now(tz=None)` возвращает NAIVE LOCAL (не UTC). Pre-existing warning — handler mybookings_msg:430-431 уже обходит это (использует `datetime.now(UTC)` и strip tzinfo). Service-уровень всё ещё naive local для filter. Отдельная задача (не transfer). Фикс — Урок 2.6 (Postgres migration).
