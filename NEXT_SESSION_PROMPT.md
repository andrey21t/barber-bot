# NEXT_SESSION_PROMPT — Block 4 завершён. Coverage 99%, behavior 100% для main.py.

> Дата: 2026-08-22 (продолжение) · 4 коммита этой сессии.
> 222 тестов (было 217), 0 skipped, ruff + mypy чисто. Coverage 99% (8 miss → 2 miss).
> Code-reviewer LGTM на B+D, C+F trivial skip. Все гейты зелёные.

## Контекст проекта

**Репозиторий:** `~/PycharmProjects/barber-bot/` (личный pet-проект, коммитить свободно по AGENTS.md § git-repo-categories)
**Стек:** Python 3.12, aiogram 3.x, SQLAlchemy 2.0 async, SQLite (dev) → Postgres (prod), APScheduler 3.x
**Спека (SSOT):** `~/PycharmProjects/barber-bot/spec.md`
**Формат работы:** `~/PycharmProjects/barber-bot/MY-VIBE-RULES.md` — dev-режим, гейты: deep-analysis на нетривиальное → реализация → verify → code-review

## Состояние на момент сохранения

### Завершено в этой сессии (4 коммита)

**`68df6ab` — F: ruff format 9 файлов (trivial)**
- ruff 0.15 compaction — многострочные string'и компактифицируются до 100 chars
- 9 файлов: bot/handlers/{client,start}.py, bot/keyboards/client.py, bot/middlewares/session_timeout.py, bot/models.py, bot/services/notifications.py, tests/{test_admin,test_notifications,test_scheduler}.py
- AST unchanged, no behavior change (+117/-149)

**`0fac563` — B: stabilize flaky test_add_slots_concurrent_race (logic)**
- Заменил asyncio.gather (flaky 1/10) на manual orchestration через patched SELECT
- Pattern из `test_transfer_booking_concurrent_race_runtime` (test_booking.py:1122)
- Race simulation faithful: B видит stale snapshot (patched SELECT), INSERT violates UNIQUE → IntegrityError catch → rollback → SlotAlreadyExistsError
- Применил 2 из 3 suggestions от code-reviewer (S1 wider verify query, S2 docstring dedup)
- Code-reviewer: LGTM, 0 critical, 0 warnings
- 20/20 PASS после всех правок (было 1/10 FAIL)

**`92bcf99` — C: db.py coverage (trivial)**
- Создан `tests/test_db.py` (4 теста, 109 строк)
- Покрыты 6 miss lines: `create_all`, `drop_all`, `dispose`, `utcnow`
- db.py 70% → 100%, TOTAL 8 miss → 2 miss
- monkeypatch engine + run_sync inspect (greenlet-safe, sync_engine.inspect падает вне async ctx)
- Trivial — code-review skip per skill rules (personal repo + utilities)

**`3597229` — D: subprocess test for `if __name__` block (state)**
- Добавлен `test_main_name_block_starts_process_via_subprocess` в tests/test_main.py
- Subprocess — единственный способ покрыть `if __name__ == "__main__"` (pytest импортирует bot.main как модуль)
- 2 acceptable outcomes: 401 от Telegram / timeout network blocked
- Key assertion: `"Polling started" in stderr` (main.py:103) — отличает "main() ran" от "import crashed"
- **Code-reviewer W1+W2 поймали реальный баг**: первый version давал false positive на `ModuleNotFoundError: No module named 'scheduler'` (editable install не настроен, scheduler.py в repo_root, не в bot/ package). Fixed: `PYTHONPATH=repo_root` в env subprocess'а
- Coverage main.py: 95% → 95% (subprocess coverage не ловится pytest-cov), но **behavior coverage 100%**
- Code-reviewer: LGTM, 0 critical, 4 warnings (W1+W2 fixed, W3 env inheritance OK для pet, W4 future-proofed via W1 fix)

### Состояние тестов

```
pytest: 222 passed, 0 skipped in ~12.8s
ruff check: All checks passed
ruff format --check: 36 files already formatted
mypy: 22 source files, no issues
coverage: 99% (1275 stmts, 2 miss)
```

Flaky: НЕТ (test_add_slots_concurrent_race стабилен 20/20).

### Финальный coverage breakdown

```
bot/db.py                  20   0  100%   ← было 70% (C)
bot/handlers/admin.py     212   0  100%
bot/handlers/client.py    386   0  100%
bot/keyboards/client.py    63   0  100%
bot/services/booking.py   229   0  100%
bot/services/slots.py      51   0  100%
bot/services/admin.py       47   0  100%
bot/services/notifications.py 33 0 100%
bot/middlewares/session_timeout.py 27 0 100%
bot/main.py                44   2   95%   110-112 (if __name__, behavior covered via subprocess)
bot/session.py             24   0  100%
bot/models.py              73   0  100%
bot/schemas.py             26   0  100%
bot/config.py              15   0  100%
bot/states.py              10   0  100%
TOTAL                    1275   2    99%
```

### Что НЕ покрыто (2 miss)

- `bot/main.py:110-112` (3 строки) — `if __name__ == "__main__": asyncio.run(main())` — subprocess test покрывает BEHAVIOR, но pytest-cov не ловит coverage subprocess'а (нет `COVERAGE_PROCESS_START` config). Для line coverage 100% нужно добавить `COVERAGE_PROCESS_START=.coveragerc` + coverage.process_start() в bot/main.py. Trade-off: +complexity, низкая ценность (3 строки обёртки). Acceptable.

## Что делать следующей сессии

Нет блокирующих багов, нет flaky, coverage 99%. Оставшиеся направления:

### Опц. A — Урок 2.6 Postgres migration (наиболее значимое, high-stakes)

**Pre-existing warning** (зафиксировано, не блокирующее на dev):
- `booking.start_at.astimezone(ZoneInfo(business_tz))` на naive datetime из SQLite интерпретирует naive как system-local TZ. На Render (TZ=UTC) — корректно. Баг проявится при миграции на Postgres с TIMESTAMP WITHOUT TZ на сервере вне UTC.
- `get_client_bookings` в admin.py:152 — `datetime.now(tz=None)` возвращает NAIVE LOCAL (не UTC). Handler `mybookings_msg:430-431` обходит через `datetime.now(UTC)` + strip tzinfo. Service-уровень всё ещё naive local для filter.

**Фикс** — миграция на Postgres с TIMESTAMP WITH TZ (или явная конвертация naive → aware UTC в service).

**Сложность:** большая задача, отдельная сессия.
**Гейты:** high-stakes → `deep-analysis-protocol` Pass 1-4 + `deep-analysis-critic` subagent обязателен.
**Примерный объём:** 4-8 часов (schema migration + service fixes + tests update + Render deployment).

### Опц. E — Урок 2.5 aiogram_calendar month-navigation

Сейчас `date_picker_keyboard(days_ahead=7)` — простые 7 кнопок. aiogram_calendar в deps для будущей month-navigation.
**Не блокирующее** — 7 дней хватает для MVP.
**Сложность:** medium (~2-3 часа), UX change.
**Гейты:** deep-analysis (new feature, logic), без critic.

## Рекомендация

| Если есть ресурс | Что делать | Почему |
|---|---|---|
| 2-3 часа | **Опц. E** (aiogram_calendar) | Реальная UX-польза для продукта, medium-сложность |
| 4-8 часов | **Опц. A** (Postgres) | Главная техническая задача проекта, high-stakes |

## Quick start prompt для opencode

```
Продолжаем barber-bot Блок 4 завершён — coverage 99%, behavior 100%, 4 коммита сессии 2026-08-22.

Прочитай ~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md — там полный контекст:
- Сессия 2026-08-22 (продолжение): F ruff format (68df6ab) + B flaky stabilize (0fac563) + C db.py coverage (92bcf99) + D main.py subprocess test (3597229)
- 222 теста, 0 skipped, coverage 99% (2 miss: main.py __name__ — behavior covered via subprocess, pytest-cov не ловит subprocess coverage)
- Code-reviewer LGTM на B+D, C+F trivial skip. Все гейты зелёные.

Нет блокирующих багов, нет flaky. Выбери направление:
A. Postgres migration (Урок 2.6, 4-8 часов, high-stakes, deep-analysis-critic)
E. aiogram_calendar month-navigation (~2-3 часа, UX, deep-analysis)

Гейты: deep-analysis для E (logic), critic для A (high-stakes).
Pre-push НЕ нужен (личный репо). Коммитить свободно.
```

## Файлы для быстрого ориентирования

| Файл | Что | Строк |
|---|---|---|
| `spec.md` | SSOT — контракты переноса (реализованы) | 541 |
| `MY-VIBE-RULES.md` | Формат работы + FSM edge cases + rules | 79 |
| `bot/handlers/client.py` | Booking flow + /mybookings + cancel + transfer FSM | 837 |
| `bot/handlers/admin.py` | /addslots, /closeslot, /today, /week, /services + 3 helpers | 383 |
| `bot/services/booking.py` | create_booking + cancel_booking + transfer_booking | ~660 |
| `bot/services/admin.py` | get_today/week_bookings, create_service, get_client_bookings | 160 |
| `bot/services/slots.py` | add_slots, close_slot, get_available_slots | 102 |
| `bot/keyboards/client.py` | Inline keyboards + Cancel/Transfer CallbackData + _no_op_button | 180 |
| `bot/models.py` | 7 таблиц — Booking (status confirmed/transferred/cancelled), Slot, NotificationLog | 184 |
| `bot/states.py` | BookingStates + TransferStates | 28 |
| `bot/config.py` | Settings: CANCEL_MIN_HOURS=24, REMINDER_24H_BEFORE, REMINDER_1H_BEFORE | 29 |
| `bot/main.py` | Entry point: Bot, Dispatcher, scheduler lifecycle, polling | 112 |
| `bot/session.py` | CorpAiohttpSession — corp CA bundle auto-detection | 40 |
| `bot/db.py` | Base, engine, async_session_factory, create_all/drop_all/dispose/utcnow | 43 |
| `scheduler.py` | build_scheduler, schedule_for_booking, remove_jobs_for_booking, on_startup_scan | 126 |
| `tests/test_booking.py` | booking service tests + 12 transfer tests + concurrent race runtime | ~2053 |
| `tests/test_admin_handlers.py` | admin handlers tests (94 + T9) | 1282 |
| `tests/test_client_handlers.py` | client handlers tests (57 + T10 + bonus) | ~2600 |
| `tests/test_admin.py` | services/admin tests (timezone edge cases, _utc_naive helper) | 703 |
| `tests/test_main.py` | bot/main.py wiring + lifecycle (6 тестов) + subprocess __name__ test (1 тест) | 227 |
| `tests/test_session.py` | bot/session.py ssl context + CorpAiohttpSession (6 тестов) | 124 |
| `tests/test_db.py` | bot/db.py create_all/drop_all/dispose/utcnow (4 теста) | 109 |
| `tests/test_slots.py` | slots service tests + stabilised concurrent race (15 тестов) | 285 |

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

### W3 — assert в production при python -O (admin.py:129, 205, 339)

`assert admin_id is not None` strip'ается при `python -O`. Graceful degradation: `_resolve_master_and_business(None)` → `Master.telegram_id == None` → no rows → "Мастер не найден" (не crash). Acceptable для pet-проекта без `-O` в deploy. Альтернатива: `if admin_id is None: return` для defense-in-depth.

### RUFF FORMAT DRIFT — ЗАКРЫТО (commit 68df6ab)

ruff 0.15 compaction применён ко всем 36 файлам. `ruff format --check` зелёный.

### FLAKY test_add_slots_concurrent_race — ЗАКРЫТО (commit 0fac563)

Стабилизирован через manual orchestration (patched SELECT). 20/20 PASS после всех правок.
