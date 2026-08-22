# NEXT_SESSION_PROMPT — Tier 2 (T9+T10) закрыт. Что дальше после coverage 94%.

> Дата: 2026-08-22 · Сессия завершена: T9 admin + T10 client + bonus keyboards + FK surgery fix.
> 205 тестов, 0 skipped, ruff + mypy чисто. Coverage 92% → 94% (103 miss → 79 miss, 24 stmts).
> Code-review subagent VERDICT LGTM, 3 фикса применены (W1, S1, S3).
> Цель следующей сессии: выбрать направление из списка ниже (нет блокирующих багов).

## Контекст проекта

**Репозиторий:** `~/PycharmProjects/barber-bot/` (личный pet-проект, коммитить свободно по AGENTS.md § git-repo-categories)
**Стек:** Python 3.12, aiogram 3.x, SQLAlchemy 2.0 async, SQLite (dev) → Postgres (prod), APScheduler 3.x
**Спека (SSOT):** `~/PycharmProjects/barber-bot/spec.md`
**Формат работы:** `~/PycharmProjects/barber-bot/MY-VIBE-RULES.md` — dev-режим, гейты: deep-analysis на нетривиальное → реализация → verify → code-review

## Состояние на момент сохранения

### Завершено в этой сессии (1 коммит `7e86793`)

Tier 2 coverage sweep — 13 новых тестов в 2 файлах (+477 строк):
- **T9 admin** (3 теста): `cmd_addslots` master not found, `cmd_closeslot` returns False race (monkeypatch), `_resolve_master_and_business` business FK broken (raw DELETE)
- **T10 client** (7 тестов): `from_user is None` early-return ×3 (`mybookings_msg`, `mybookings_cancel_cb`, `mybookings_transfer_cb`, `transfer_slot_cb`), `BookingAlreadyCancelledError` race (monkeypatch `cancel_booking`), `no_state_fallback`, `mybookings_transfer_cb` booking.status='cancelled'
- **Bonus keyboards** (2 теста): `slot_picker_keyboard([])` noop button, `_no_op_button()` helper unit
- **S1 fix**: `confirm_cb` business is None FK surgery (closed pre-existing skip line 2079) — mirrors admin.py:66 raw DELETE pattern

Code-review fixes:
- **W1**: misleading docstring на `_no_op_button` test (helper НЕ вызывается в `slot_picker_keyboard` — independent implementations)
- **S3**: `state.clear.assert_not_awaited()` добавлен в `mybookings_transfer_cb` from_user None test (defense-in-depth)

### Состояние тестов

```
pytest: 205 passed, 0 skipped in 3.47s
ruff check: All checks passed
ruff format: All clean
mypy: 34 source files, no issues
coverage: 94% (1281 stmts, 79 miss)
```

Pre-existing flaky: `test_add_slots_concurrent_race_raises_slot_exists` (одиночный 1 из 10 запусков — `asyncio.gather` nondeterminism, не блокирующее).

### Финальный coverage breakdown

```
bot/handlers/admin.py     218   5   98%   128-129, 133, 210, 345  (dead code)
bot/handlers/client.py   386   0  100%
bot/keyboards/client.py   63   0  100%
bot/services/booking.py  229   0  100%
bot/services/slots.py      51   0  100%
bot/services/admin.py      47   0  100%
bot/services/notifications.py 33   0  100%
bot/middlewares/session_timeout.py 27 0 100%
bot/models.py              73   0  100%
bot/schemas.py             26   0  100%
bot/config.py              15   0  100%
bot/states.py              10   0  100%
bot/db.py                  20   6   70%   22-23, 28-29, 33, 43  (dev/test utilities)
bot/main.py                44  44    0%   16-112  (bootstrap)
bot/session.py             24  24    0%   1-40  (aiogram internals)
TOTAL                     1281  79   94%
```

## Что НЕ сделано (опционально, не блокирующее)

### Опц. 1 — main.py / session.py coverage (mock aiogram internals)

44+24 = 68 stmts (заведёт coverage до ~99%):
- `bot/main.py` — bootstrap (build_scheduler, include_router, SessionTimeoutMiddleware wiring, scheduler.start). Нужен mock aiogram Dispatcher.
- `bot/session.py` — aiogram-internal utilities (low value, обёртки над aiogram API).
- Сложность: medium (~1-2 часа, требует aiogram testing patterns — `dp.feed_update` или unittest mock).

### Опц. 2 — Урок 2.6 Postgres migration (наиболее значимое)

Pre-existing warning (зафиксировано, не блокирующее на dev):
- `booking.start_at.astimezone(ZoneInfo(business_tz))` на naive datetime из SQLite интерпретирует naive как system-local TZ. На Render (TZ=UTC) — корректно. Баг проявится при миграции на Postgres с TIMESTAMP WITHOUT TZ на сервере вне UTC.
- `get_client_bookings` в admin.py:152 — `datetime.now(tz=None)` возвращает NAIVE LOCAL (не UTC). Pre-existing warning — handler mybookings_msg:430-431 уже обходит это (использует `datetime.now(UTC)` и strip tzinfo). Service-уровень всё ещё naive local для filter.

Фикс — миграция на Postgres с TIMESTAMP WITH TZ (или явная конвертация naive → aware UTC в service). Это Урок 2.6, большая задача. High-stakes → нужен `deep-analysis-critic` subagent.

### Опц. 3 — Урок 2.5 aiogram_calendar month-navigation

Сейчас `date_picker_keyboard(days_ahead=7)` — простые 7 кнопок. aiogram_calendar в deps для будущей month-navigation. Не блокирующее — 7 дней хватает для MVP.

### Опц. 4 — Stabilise FLAKY test_add_slots_concurrent_race_raises_slot_exists

`asyncio.gather` nondeterminism — иногда 2 winner вместо 1 (UNIQUE constraint race). Можно стабилизировать через patched SELECT (как в `test_transfer_booking_concurrent_race_runtime`) или оставить как есть (1 из 10 запусков — acceptable для dev).

### Опц. 5 — Dead code 5 строк в admin.py

```
admin.py:128-129  — if not hours (unreachable после len(args) >= 2 check)
admin.py:133, 210, 345 — if admin_id is None (unreachable после _is_admin True)
```
Можно удалить (dead code removal) или оставить как defense-in-depth. Не критично.

## Что делать следующей сессии

Нет блокирующих багов. Выбери направление (по приоритету ресурса):

1. **Если есть ресурс ~5-10 мин → Опц. 5** (dead code removal) — убрать 5 unreachable строк из admin.py.
2. **Если есть ресурс ~1-2 часа → Опц. 1** (main.py / session.py coverage) — mock aiogram Dispatcher, ~99% coverage.
3. **Если хочется большой фичи → Опц. 2** (Postgres migration, Урок 2.6) — отдельная сессия, big-stakes, нужен deep-analysis-critic.

### Quick start prompt для opencode

```
Продолжаем barber-bot Блок 3 — что после Tier 2 (T9+T10 закрыт).

Прочитай ~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md — там полный контекст:
- Tier 2 coverage sweep завершён (commit 7e86793): 205 тестов, 0 skipped, coverage 94%
- Code-review subagent VERDICT LGTM, 3 фикса применены (W1, S1, S3)
- Все гейты зелёные: ruff + mypy + pytest 205 passed

Нет блокирующих багов. Выбери направление:
1. Dead code removal (~5-10 мин) — убрать 5 unreachable строк из admin.py
2. main.py / session.py coverage (~1-2 часа) — mock aiogram Dispatcher, ~99%
3. Урок 2.6 — Postgres migration (отдельная сессия, high-stakes, deep-analysis-critic)

Гейты: deep-analysis НЕ нужен для Опц. 5 (trivial dead code). Для Опц. 1 —
deep-analysis Pass 1-4 (новый fixture aiogram mock). Для Опц. 2 — high-stakes
+ deep-analysis-critic subagent. Pre-push НЕ нужен (личный репо).
Коммитить свободно (pet-проект, AGENTS.md § git-repo-categories).
```

## Файлы для быстрого ориентирования

| Файл | Что | Строк |
|---|---|---|
| `spec.md` | SSOT — контракты переноса (реализованы) | 541 |
| `MY-VIBE-RULES.md` | Формат работы + FSM edge cases + rules | 79 |
| `bot/handlers/client.py` | Booking flow + /mybookings + cancel + transfer FSM | 837 |
| `bot/handlers/admin.py` | /addslots, /closeslot, /today, /week, /services + 3 helpers | 389 |
| `bot/services/booking.py` | create_booking + cancel_booking + transfer_booking | ~660 |
| `bot/services/admin.py` | get_today/week_bookings, create_service, get_client_bookings | 160 |
| `bot/services/slots.py` | add_slots, close_slot, get_available_slots | 102 |
| `bot/keyboards/client.py` | Inline keyboards + Cancel/Transfer CallbackData + _no_op_button | 180 |
| `bot/models.py` | 7 таблиц — Booking (status confirmed/transferred/cancelled), Slot, NotificationLog | 184 |
| `bot/states.py` | BookingStates + TransferStates | 28 |
| `bot/config.py` | Settings: CANCEL_MIN_HOURS=24, REMINDER_24H_BEFORE, REMINDER_1H_BEFORE | 29 |
| `scheduler.py` | build_scheduler, schedule_for_booking, remove_jobs_for_booking, on_startup_scan | 126 |
| `tests/test_booking.py` | booking service tests + 12 transfer tests + concurrent race runtime | ~2053 |
| `tests/test_admin_handlers.py` | admin handlers tests (94 теста + T9) | 1282 |
| `tests/test_client_handlers.py` | client handlers tests (57 тестов + T10 + bonus) | ~2600 |
| `tests/test_admin.py` | services/admin tests (timezone edge cases, _utc_naive helper) | 703 |

## Гейты (напоминание)

- **Deep-analysis** на нетривиальное (багфикс с logic, новая фича, рефакторинг)
- **Verify-and-fix** перед «готово»: pytest + ruff + mypy — все зелёные
- **Code-review** subagent на logic-change (через `qa-code-review`)
- **Pre-push ревью**: НЕ нужно — barber-bot личный репо (AGENTS.md § git-repo-categories)
- **Коммитить свободно** — pet-проект, без переспроса

## Pre-existing Warnings (зафиксировано, не блокирующее на dev)

### W1 — naive datetime + astimezone (FIXED в transfer_booking И cancel_booking)

`booking.start_at.replace(tzinfo=UTC).astimezone(ZoneInfo(business_tz))` — фикс применён в `transfer_booking` (commit c38087b) и `cancel_booking` (commit bfab1fb). Systemic fix — Урок 2.6 (Postgres migration с TIMESTAMP WITH TZ).

### W2 — get_client_bookings naive local datetime (admin.py:152)

`datetime.now(tz=None)` возвращает NAIVE LOCAL. Handler `mybookings_msg:430-431` обходит через `datetime.now(UTC)` + strip tzinfo. Service-уровень всё ещё naive local. Фикс — Урок 2.6.

### FLAKY — test_add_slots_concurrent_race_raises_slot_exists

`asyncio.gather` nondeterminism — 1 из 10 запусков даёт 2 winner вместо 1 (UNIQUE race). Не блокирующее (dev-only, single-threaded локально). Для stabilise — patched SELECT паттерн (как `test_transfer_booking_concurrent_race_runtime`).
