# SESSION_PROMPT — barber-bot Блок 3 часть 4 — что после transfer

> Полный промт для старта следующей сессии. Self-contained — можно скопировать
> целиком в opencode. Файл `~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md`
> рекомендуется прочитать для деталей, но этот промт самодостаточен.

---

## Копировать в opencode для старта (вся секция ниже между маркерами)

---

Продолжаем barber-bot Блок 3 часть 4 — что после transfer.

## Контекст проекта

**Репозиторий:** `~/PycharmProjects/barber-bot/` (личный pet-проект, коммитить свободно по AGENTS.md § git-repo-categories — без переспроса и без pre-push ревью).
**Стек:** Python 3.12, aiogram 3.x, SQLAlchemy 2.0 async, SQLite (dev) → Postgres (prod), APScheduler 3.x.
**Спека (SSOT):** `~/PycharmProjects/barber-bot/spec.md` — строки 41, 318, 408-409 (контракты переноса — реализованы полностью).
**Формат работы:** `~/PycharmProjects/barber-bot/MY-VIBE-RULES.md` — dev-режим (без педагогики, код сразу), резюме после блока, гейты: deep-analysis на нетривиальное → реализация → verify → code-review.

## Что уже сделано в прошлой сессии (3 коммита, всё зелёное)

### commit `97da237` — feat(transfer_booking): service + 11 tests
- `bot/services/booking.py`: `transfer_booking(session, booking_id, new_slot_id, client_id, scheduler, *, now_utc=None)` — 15 шагов, ~174 строк. Race protection через `Booking.start_at == old_start_at` в WHERE clause UPDATE booking (loser's WHERE не матчит после winner'a). Bug fix: `old_slot_id` захвачен ПЕРЕД UPDATE (SQLAlchemy auto-mutate'ит `booking.slot_id`).
- `tests/test_booking.py`: 11 transfer тестов (10 service edge cases + 1 static invariant lock для concurrent race protection через `inspect.getsource`).

### commit `c38087b` — feat(transfer): mybookings [Перенести] button + transfer FSM handlers + tests
- `bot/keyboards/client.py`: `MyBookingsTransferCallbackData(prefix="mybook_transfer", booking_id: UUID)` + `mybookings_keyboard` эмиттит 2 кнопки на booking (adjust(2)): `[❌ Отменить]` + `[🔄 Перенести]`.
- `bot/states.py`: `TransferStates(selecting_date, selecting_slot)` — отдельный от BookingStates.
- `bot/handlers/client.py`: 3 новых handler'а: `mybookings_transfer_cb` (entry, StateFilter(None)), `transfer_date_cb` (BookDateCallbackData, TransferStates.selecting_date), `transfer_slot_cb` (BookSlotCallbackData, TransferStates.selecting_slot). `state.clear()` BEFORE service call (race condition). Мапит 8 exceptions на user-facing messages.
- `bot/services/booking.py` (code-review fixes): W1 — `old_start_at.replace(tzinfo=UTC).astimezone(...)` (naive → aware для cross-system TZ correctness). W2 — rowcount=0 re-SELECT'ит booking.status для disambiguate concurrent cancel (BookingAlreadyCancelledError) vs concurrent transfer (BookingAlreadyTransferredError).
- `tests/test_client_handlers.py`: 6 новых transfer handler тестов + обновлены 2 старых под new keyboard (2 кнопки/booking).

### commit `34ef619` — docs(NEXT_SESSION_PROMPT): transfer complete
Документация следующего шага.

### Состояние тестов

```
pytest (full suite): 77 passed in 2.3s
ruff: All checks passed
mypy: 32 source files, no issues
```

Code-review subagent — VERDICT LGTM, W1+W2 фиксы применены.

## Что делать этой сессии (нет блокирующих багов)

Выбери одно из 3 направлений (по приоритету ресурса). Если пользователь не уточнил — спроси что он хочет.

### Опц. 1 — Concurrent race runtime test (~30-40 мин, deep-analysis нужен)

**Цель:** заменить static-invariant lock (`test_transfer_booking_concurrent_transfer_race_protection` через `inspect.getsource`) на faithful runtime test с 2 sessions + asyncio.gather.

**Зачем:** static lock проверяет наличие `"Booking.start_at =="` в source, но не проверяет что runtime действительно rejects loser'а при concurrent transfer. Faithful test закрывает Pass 3 [blocker] finding по-настоящему.

**Паттерн (аналог):** `test_create_booking_idempotency_unique_guard` — использует 2 sessions с StaticPool + check_same_thread=False. Найди его через `grep -n "test_create_booking_idempotency\|StaticPool" tests/test_booking.py` и `tests/conftest.py`.

**Шаги:**
1. Прочитай существующий static-invariant test (`tests/test_booking.py::test_transfer_booking_concurrent_transfer_race_protection`) — он сейчас делает `inspect.getsource` для `"Booking.start_at =="`.
2. Прочитай `test_create_booking_idempotency_unique_guard` (или аналог) — паттерн для 2 sessions.
3. Создай `test_transfer_booking_concurrent_race_runtime` — 2 AsyncSession на одном booking, asyncio.gather 2 concurrent transfer_booking, один выигрывает (rowcount=1, TransferResult), второй проигрывает (rowcount=0 → BookingAlreadyTransferredError).
4. Удали или оставь static-invariant test (как regression lock — он дешёвый, можно оставить как sanity check).
5. Verify: pytest + ruff + mypy зелёные.
6. Commit: `test(transfer_booking): concurrent race runtime test (2 sessions + asyncio.gather)`.

**Гейты:** deep-analysis Pass 1-4 (новый fixture, не тривиальный — concurrency edge cases). qa-verify-and-fix. qa-code-review (logic-change на test fixture).

### Опц. 2 — cancel_booking W1 fix (~5 мин, deep-analysis НЕ нужен)

**Цель:** применить тот же naive → aware UTC фикс к `cancel_booking` что был применён к `transfer_booking` (W1 в code-review). Consistency.

**Зачем:** `cancel_booking` (строка 380) имеет тот же pre-existing issue: `local_time = booking.start_at.astimezone(ZoneInfo(business_tz))` на naive datetime из SQLite → на dev Mac (TZ=MSK) показывает неправильное время в "Отмена:" master notification. На Render (TZ=UTC) — корректно (по accident). transfer_booking уже починен в commit c38087b.

**Шаги:**
1. Прочитай `bot/services/booking.py` строку 380 — `local_time = booking.start_at.astimezone(ZoneInfo(business_tz))`.
2. Замени на `local_time = booking.start_at.replace(tzinfo=UTC).astimezone(ZoneInfo(business_tz))` (1 строка).
3. Проверь существующие cancel_booking тесты — `grep -n "master_notification_text\|Отмена:" tests/test_booking.py` — если assertion на точное время есть, может потребоваться update (но `result.start_at` в CancelResult уже naive — фикс только в render text, не в dataclass).
4. Verify: pytest + ruff + mypy зелёные.
5. Commit: `fix(cancel_booking): mark naive start_at as UTC before astimezone (consistency with transfer)`.

**Гейты:** deep-analysis НЕ нужен (mirror существующего pattern из transfer_booking, 1 строка). qa-verify-and-fix. qa-code-review НЕ нужен (trivial, mirror pattern).

### Опц. 3 — Урок 2.6 Postgres migration (большая задача, отдельная сессия)

**Цель:** мигрировать с SQLite на Postgres. Часть pre-existing warnings (W1 naive datetime, W2 get_client_bookings) фиксится автоматически при правильной schema (TIMESTAMP WITH TZ).

**Зачем:** production deploys на Render используют Postgres. SQLite — dev-only. Pre-existing warnings (naive datetime interpretation, datetime.now(tz=None) в admin.py:152) исчезнут если Postgres schema использует TIMESTAMP WITH TZ и service-код конвертирует naive → aware.

**Это БОЛЬШАЯ задача** — отдельная сессия, не комбинировать с Опц. 1 или 2. Включает:
- alembic migration (создать Postgres schema)
- Docker compose для локального Postgres
- Изменить `bot/db.py` engine URL (env-driven)
- Audit всех `datetime.now()` calls в service — убедиться что все aware UTC
- Прогнать все тесты на Postgres (not SQLite) — найти SQLite-специфичные assumptions (naive datetime, CHECK constraints)
- Update README + spec.md

Если пользователь хочет это — лучше спроси "готов ли он потратить сессию целиком на миграцию", потому что это не вписывается в остаток ресурса.

## Гейты (напоминание, MY-VIBE-RULES.md)

- **Deep-analysis** на нетривиальное (Опц. 1 — да, Опц. 2 — нет, Опц. 3 — да). Skip для trivial (typo, rename, 1-строка mirror).
- **Verify-and-fix** перед «готово»: `pytest`, `ruff check .`, `mypy bot scheduler.py tests` — все зелёные.
- **Code-review** subagent после verify для logic-change (Опц. 1 — да, Опц. 2 — нет, Опц. 3 — да).
- **Pre-push ревью**: НЕ нужно — barber-bot личный репо (AGENTS.md § git-repo-categories).
- **Коммитить свободно** — pet-проект, без переспроса. Коммит = конец логической единицы работы.

## Файлы для быстрого ориентирования

| Файл | Что | Строк |
|---|---|---|
| `spec.md` | SSOT — строки 41, 318, 408-409 — контракты переноса (реализованы) | 541 |
| `MY-VIBE-RULES.md` | Формат работы + FSM edge cases + rules | 79 |
| `bot/handlers/client.py` | Booking flow + /mybookings + cancel + transfer FSM | ~840 |
| `bot/services/booking.py` | create + cancel + transfer_booking (race protection + W1/W2 fixes) | ~660 |
| `bot/services/admin.py` | get_today/week_bookings, create_service, get_client_bookings | 160 |
| `bot/services/slots.py` | add_slots, close_slot, get_available_slots | 102 |
| `bot/keyboards/client.py` | Inline keyboards + Cancel + Transfer CallbackData + mybookings_keyboard (2 кнопки/booking) | ~180 |
| `bot/models.py` | 7 таблиц — Booking (status confirmed/transferred/cancelled), Slot, NotificationLog | 184 |
| `bot/states.py` | BookingStates + TransferStates | 28 |
| `bot/config.py` | Settings: CANCEL_MIN_HOURS=24, REMINDER_24H_BEFORE, REMINDER_1H_BEFORE | 29 |
| `bot/db.py` | async engine, async_session_factory (expire_on_commit=False) | ~30 |
| `scheduler.py` | build_scheduler, schedule_for_booking, remove_jobs_for_booking, on_startup_scan | 126 |
| `tests/test_booking.py` | booking service tests + cancel + transfer (12 тестов) | ~1080 |
| `tests/test_admin.py` | Pattern для тестов на services (timezone edge cases, _utc_naive helper) | 545 |
| `tests/test_client_handlers.py` | Handler тесты mybookings_msg + cancel_cb + transfer_cb (6 тестов) | ~990 |
| `tests/conftest.py` | session_factory fixture (expire_on_commit=False), seed_data | ~120 |

## Pre-existing Warnings (зафиксировано, не блокирующее)

### W1 — naive datetime + astimezone (в transfer_booking ИСПРАВЛЕНО, в cancel_booking — нет → Опц. 2)

`booking.start_at.astimezone(ZoneInfo(business_tz))` на naive datetime из SQLite интерпретирует naive как system-local TZ. На Render (TZ=UTC) — корректно. На dev Mac (TZ=MSK) — "Отмена:" master notification показывает неправильное время. Фикс — Опц. 2 выше (1 строка).

### W2 — get_client_bookings (admin.py:152)

`datetime.now(tz=None)` возвращает NAIVE LOCAL (не UTC). Handler mybookings_msg:430-431 уже обходит это (использует `datetime.now(UTC)` и strip tzinfo). Service-уровень admin.py:152 всё ещё naive local. Фикс — Урок 2.6 (Postgres migration, Опц. 3).

### FLAKY test

`test_scheduler::test_on_startup_scan_phase_2_reschedules_upcoming` — time-of-day dependent. Не регрессия. Можно stabilise через freezegun, но это отдельная задача.

## Если пользователь спрашивает "что мы делали в прошлой сессии"

Краткое резюме (3-5 строк):
- Реализовали перенос записи клиентом полностью (Блок 3 часть 3): service `transfer_booking` (15 шагов, race protection через `start_at` pin в WHERE clause) + UI (3 handler'а: entry → date_picker → slot_picker → service call) + keyboard (2 кнопки/booking: Отменить + Перенести) + 17 тестов.
- Code-review subagent — VERDICT LGTM, 2 фикса применены (W1: naive → aware UTC для cross-system TZ correctness, W2: disambiguate concurrent cancel от concurrent transfer через re-SELECT).
- 3 коммита: `97da237` (service + tests), `c38087b` (handlers + keyboard + states + tests + W1/W2 fixes), `34ef619` (docs).
- 77 тестов зелёные, ruff + mypy чисто.
- Нет блокирующих багов. Следующий шаг — опциональный (concurrent race runtime test, cancel_booking W1 consistency fix, или Postgres migration).

---
