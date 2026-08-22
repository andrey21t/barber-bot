# NEXT_SESSION_PROMPT — Опц. E (aiogram_calendar) закрыта. Coverage 99%, 226 тестов.

> Дата: 2026-08-22 (продолжение) · 2 коммита этой сессии.
> 226 тестов (было 222), 0 skipped, ruff + mypy чисто. Coverage 99% (2 miss: main.py __name__).
> Code-reviewer LBTM → 3 находки fixed (F1+W1+W2) → regression test for F1 → ready.

## Контекст проекта

**Репозиторий:** `~/PycharmProjects/barber-bot/` (личный pet-проект, коммитить свободно по AGENTS.md § git-repo-categories)
**Стек:** Python 3.12, aiogram 3.x, SQLAlchemy 2.0 async, SQLite (dev) → Postgres (prod), APScheduler 3.x
**Спека (SSOT):** `~/PycharmProjects/barber-bot/spec.md`
**Формат работы:** `~/PycharmProjects/barber-bot/MY-VIBE-RULES.md` — dev-режим, гейты: deep-analysis на нетривиальное → реализация → verify → code-review

## Состояние на момент сохранения

### Завершено в этой сессии (2 коммита)

**`3f55355` — feat(calendar): migrate date picker to aiogram_calendar.SimpleCalendar**
- Заменил 7-дневный button picker на SimpleCalendar (month navigation, ru_RU, cancel='Отмена', today='Сегодня')
- Range bounded by min_date/max_date (today @ midnight .. today+MAX_BOOKING_DAYS_AHEAD @ midnight, business TZ)
- `bot/keyboards/client.py`: `date_picker_keyboard` → `calendar_keyboard`, удалён `BookDateCallbackData`
- `bot/handlers/client.py`: `date_cb`/`transfer_date_cb` → `simple_calendar_cb`/`transfer_simple_calendar_cb`, оба делегируют в общий `_handle_simple_calendar` (DRY). Добавлен `_calendar_range(settings)`.
- `bot/config.py`: `MAX_BOOKING_DAYS_AHEAD: int = 60`
- `tests/test_client_handlers.py`: удалено 8 старых date_cb/transfer_date_cb тестов, добавлено 9 новых
- **Code-reviewer LBTM → fixed:**
  - F1 [blocker]: `_calendar_range` возвращал `today_local` с time-компонентом (13:45) → aiogram_calendar сравнивал `min_date > datetime(year, month, day) @ midnight` → "Сегодня" alert "должно быть позже сегодня". Fixed: `.replace(hour=0, minute=0, second=0, microsecond=0)` — midnight
  - W1 [warning]: `act=ignore`/`today+same-month` возвращались без `callback.answer()` → Telegram spinner 10s. Fixed: явный `callback.answer(cache_time=60)`
  - W2 [warning]: flaky test на TZ boundary (MSK vs UTC в 21:00 last day of month). Fixed: test использует system-local `datetime.now()`

**`17b2a19` — test(calendar): regression test for _calendar_range midnight (F1)**
- Code-reviewer указал: тесты мокают `process_selection` → range-check в lib никогда не запускается → F1 fix не покрыт regression test
- Добавлен `test_calendar_range_returns_midnight_naive_local` (48 строк):
  - assert naive (no tzinfo)
  - assert midnight (hour=minute=second=0)
  - assert span == MAX_BOOKING_DAYS_AHEAD
  - assert min_date.date() == today в business TZ
- Если кто-то вернёт `replace(tzinfo=None)` без midnight → тест упадёт

### Состояние тестов

```
pytest: 226 passed, 0 skipped in ~7s
ruff check: All checks passed
ruff format --check: 38 files already formatted
mypy: 37 source files, no issues
coverage: 99% (1269 stmts, 2 miss)
```

Flaky: НЕТ (W2 fixed — test_simple_calendar_cb_today_same_month_answers_explicitly использует system-local datetime).

### Финальный coverage breakdown

```
bot/db.py                            20   0  100%
bot/handlers/admin.py               212   0  100%
bot/handlers/client.py              384   0  100%  ← _calendar_range + _handle_simple_calendar покрыты
bot/keyboards/client.py              56   0  100%  ← calendar_keyboard покрыта
bot/services/booking.py             229   0  100%
bot/services/slots.py                51   0  100%
bot/services/admin.py                47   0  100%
bot/services/notifications.py        33   0  100%
bot/middlewares/session_timeout.py   27   0  100%
bot/main.py                          44   2   95%   110-112 (if __name__, behavior covered via subprocess)
bot/session.py                       24   0  100%
bot/models.py                        73   0  100%
bot/schemas.py                       26   0  100%
bot/config.py                        16   0  100%
bot/states.py                        10   0  100%
TOTAL                              1269   2    99%
```

### Что НЕ покрыто (2 miss, pre-existing)

- `bot/main.py:110-112` — `if __name__ == "__main__": asyncio.run(main())` — subprocess test покрывает BEHAVIOR, pytest-cov не ловит subprocess coverage. Trade-off: +complexity (COVERAGE_PROCESS_START) vs 3 строки обёртки. Acceptable.

## Что делать следующей сессии

Нет блокирующих багов, нет flaky, coverage 99%. Оставшиеся направления:

### Опц. A — Урок 2.6 Postgres migration (главная техническая задача, high-stakes)

**Pre-existing warning** (зафиксировано, не блокирующее на dev):
- `booking.start_at.astimezone(ZoneInfo(business_tz))` на naive datetime из SQLite интерпретирует naive как system-local TZ. На Render (TZ=UTC) — корректно. Баг проявится при миграции на Postgres с TIMESTAMP WITHOUT TZ на сервере вне UTC.
- `get_client_bookings` в admin.py:152 — `datetime.now(tz=None)` возвращает NAIVE LOCAL (не UTC). Handler `mybookings_msg:430-431` обходит через `datetime.now(UTC)` + strip tzinfo. Service-уровень всё ещё naive local для filter.

**Фикс** — миграция на Postgres с TIMESTAMP WITH TZ (или явная конвертация naive → aware UTC в service).

**Сложность:** большая задача, отдельная сессия.
**Гейты:** high-stakes → `deep-analysis-protocol` Pass 1-4 + `deep-analysis-critic` subagent ОБЯЗАТЕЛЕН.
**Примерный объём:** 4-8 часов (schema migration + service fixes + tests update + Render deployment).
**Risk:** one-way door (миграция данных), cross-project (DB + backend + deploy).

## Quick start prompt для opencode

```
Продолжаем barber-bot Опц. E (aiogram_calendar) закрыта — 226 тестов, coverage 99%.

Прочитай ~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md — там полный контекст:
- Сессия 2026-08-22 (продолжение): feat SimpleCalendar (3f55355) + regression test F1 (17b2a19)
- 226 тестов, 0 skipped, coverage 99% (2 miss: main.py __name__ — behavior covered via subprocess)
- Code-reviewer LBTM → 3 находки fixed (F1+W1+W2) + regression test → готово
- Flaky: НЕТ (W2 fixed)

Нет блокирующих багов. Главная оставшаяся задача:

A. Postgres migration (Урок 2.6, 4-8 часов, high-stakes, deep-analysis-critic ОБЯЗАТЕЛЕН)
   - Главная техническая задача проекта
   - Schema migration SQLite → Postgres + TIMESTAMP WITH TZ для W1/W2 fix
   - Гейты: deep-analysis Pass 1-4 + deep-analysis-critic subagent ОБЯЗАТЕЛЬНО (high-stakes)
   - Объём: schema + service fixes + tests update + Render deployment
   - Risk: one-way door (миграция данных), cross-project (DB + backend + deploy)

Гейты: deep-analysis + critic для A (high-stakes).
Pre-push НЕ нужен (личный репо). Коммитить свободно.
```

## Файлы для быстрого ориентирования

| Файл | Что | Строк |
|---|---|---|
| `spec.md` | SSOT — контракты переноса (реализованы) | 541 |
| `MY-VIBE-RULES.md` | Формат работы + FSM edge cases + rules | 79 |
| `bot/handlers/client.py` | Booking flow + /mybookings + cancel + transfer FSM + SimpleCalendar handlers | ~846 |
| `bot/handlers/admin.py` | /addslots, /closeslot, /today, /week, /services + 3 helpers | 383 |
| `bot/services/booking.py` | create_booking + cancel_booking + transfer_booking | ~660 |
| `bot/services/admin.py` | get_today/week_bookings, create_service, get_client_bookings | 160 |
| `bot/services/slots.py` | add_slots, close_slot, get_available_slots | 102 |
| `bot/keyboards/client.py` | Inline keyboards + calendar_keyboard + slot_picker + Cancel/Transfer CallbackData | 174 |
| `bot/models.py` | 7 таблиц — Booking (status confirmed/transferred/cancelled), Slot, NotificationLog | 184 |
| `bot/states.py` | BookingStates + TransferStates | 28 |
| `bot/config.py` | Settings: CANCEL_MIN_HOURS=24, MAX_BOOKING_DAYS_AHEAD=60, REMINDER_24H_BEFORE | 30 |
| `bot/main.py` | Entry point: Bot, Dispatcher, scheduler lifecycle, polling | 112 |
| `bot/session.py` | CorpAiohttpSession — corp CA bundle auto-detection | 40 |
| `bot/db.py` | Base, engine, async_session_factory, create_all/drop_all/dispose/utcnow | 43 |
| `scheduler.py` | build_scheduler, schedule_for_booking, remove_jobs_for_booking, on_startup_scan | 126 |
| `tests/test_booking.py` | booking service tests + 12 transfer tests + concurrent race runtime | ~2053 |
| `tests/test_admin_handlers.py` | admin handlers tests (94 + T9) | 1282 |
| `tests/test_client_handlers.py` | client handlers tests + SimpleCalendar (10 new) + _calendar_range regression | ~2720 |
| `tests/test_admin.py` | services/admin tests (timezone edge cases, _utc_naive helper) | 703 |
| `tests/test_main.py` | bot/main.py wiring + lifecycle (6 тестов) + subprocess __name__ test (1 тест) | 227 |
| `tests/test_session.py` | bot/session.py ssl context + CorpAiohttpSession (6 тестов) | 124 |
| `tests/test_db.py` | bot/db.py create_all/drop_all/dispose/utcnow (4 теста) | 109 |
| `tests/test_slots.py` | slots service tests + stabilised concurrent race (15 тестов) | 285 |

## Гейты (напоминание)

- **Deep-analysis** на нетривиальное (багфикс с logic, новая фича, рефакторинг)
- **Deep-analysis-critic** subagent ОБЯЗАТЕЛЕН для high-stakes (миграции, security, рефакторинг > 5 файлов) — Опц. A подходит
- **Verify-and-fix** перед «готово»: pytest + ruff + mypy — все зелёные
- **Code-review** subagent на logic-change (через `qa-code-review`)
- **Pre-push ревью**: НЕ нужно — barber-bot личный репо (AGENTS.md § git-repo-categories)
- **Коммитить свободно** — pet-проект, без переспроса

## Pre-existing Warnings (зафиксировано, не блокирующее на dev)

### W1 — naive datetime + astimezone (FIXED в transfer_booking И cancel_booking)

`booking.start_at.replace(tzinfo=UTC).astimezone(ZoneInfo(business_tz))` — фикс применён в `transfer_booking` (commit c38087b) и `cancel_booking` (commit bfab1fb). Systemic fix — Урок 2.6 (Postgres migration с TIMESTAMP WITH TZ).

### W2 — get_client_bookings naive local datetime (admin.py:152)

`datetime.now(tz=None)` возвращает NAIVE LOCAL. Handler `mybookings_msg:430-431` обходит через `datetime.now(UTC)` + strip tzinfo. Service-уровень всё ещё naive local. Фикс — Урок 2.6.

### W3 — assert в production при python -O (admin.py:129, 205, 339)

`assert admin_id is not None` strip'ается при `python -O`. Graceful degradation: `_resolve_master_and_business(None)` → `Master.telegram_id == None` → no rows → "Мастер не найден" (не crash). Acceptable для pet-проекта без `-O` в deploy. Альтернатива: `if admin_id is None: return` для defense-in-depth.

### RUFF FORMAT DRIFT — ЗАКРЫТО (commit 68df6ab)

ruff 0.15 compaction применён ко всем 36 файлам. `ruff format --check` зелёный.

### FLAKY test_add_slots_concurrent_race — ЗАКРЫТО (commit 0fac563)

Стабилизирован через manual orchestration (patched SELECT). 20/20 PASS после всех правок.

### F1 (code-reviewer) — ЗАКРЫТО (commit 3f55355 + 17b2a19)

_calendar_range midnight regression. Fixed + regression test added.
