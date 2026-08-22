# NEXT_SESSION_PROMPT — Coverage 99% достигнут. Что дальше.

> Дата: 2026-08-22 · 3 коммита: dead code removal + main.py/session.py coverage + S1 fix.
> 217 тестов, 0 skipped, ruff + mypy чисто. Coverage 94% → 99% (79 miss → 8 miss, 71 stmt).
> Independent review: code-reviewer LGTM (0 critical, 0 warnings), S1 применён.
> Цель следующей сессии: выбрать направление из списка ниже (нет блокирующих багов).

## Контекст проекта

**Репозиторий:** `~/PycharmProjects/barber-bot/` (личный pet-проект, коммитить свободно по AGENTS.md § git-repo-categories)
**Стек:** Python 3.12, aiogram 3.x, SQLAlchemy 2.0 async, SQLite (dev) → Postgres (prod), APScheduler 3.x
**Спека (SSOT):** `~/PycharmProjects/barber-bot/spec.md`
**Формат работы:** `~/PycharmProjects/barber-bot/MY-VIBE-RULES.md` — dev-режим, гейты: deep-analysis на нетривиальное → реализация → verify → code-review

## Состояние на момент сохранения

### Завершено в этой сессии (3 коммита)

**`8ca8282` — Dead code removal (admin.py refactor)**
- `cmd_addslots`: убран `if not hours:` (недостижим после `len(args) < 2`)
- `cmd_addslots` / `cmd_closeslot` / `cmd_services`: `if admin_id is None: return` → `assert admin_id is not None` (type narrowing для mypy, паттерн из `client.py:302-304`)
- admin.py: 98% → 100%

**`80d7690` — main.py / session.py coverage (12 тестов)**
- `tests/test_session.py` (6 тестов): `build_ssl_context` 4 ветки + `CorpAiohttpSession.__init__` (ssl в `_connector_init`, kwargs forwarding)
- `tests/test_main.py` (6 тестов): `setup_logging`, `_on_startup`, `_on_shutdown`, `main()` wiring (3 routers identity check, 2 middleware, 2 hooks, scheduler workflow_data, finally session.close), error path
- Паттерн: `monkeypatch bot.main.{Bot,Dispatcher,CorpAiohttpSession,scheduler,setup_logging}` → вызов `main()` → assertions на wiring

**`ba39d6a` — S1 fix (test isolation)**
- `test_corp_aiohttp_session_init_*`: добавлена изоляция `~/okko-ca.pem` + `BARBER_SSL_CA_BUNDLE` env
- S1 от code-reviewer: на dev машине `~/okko-ca.pem` существует (corp CA, 130 certs), на CI — нет. Assertions проходили в обоих, но тест был impure.

### Состояние тестов

```
pytest: 217 passed, 0 skipped in ~3.5s
ruff check: All checks passed
ruff format --check: 9 pre-existing files would be reformatted (НЕ мои, ruff 0.15 новая компактификация)
mypy: 23-24 source files, no issues
coverage: 99% (1275 stmts, 8 miss)
```

Pre-existing flaky: `test_add_slots_concurrent_race_raises_slot_exists` (1 из 10 запусков — `asyncio.gather` nondeterminism, не блокирующее).

### Финальный coverage breakdown

```
bot/handlers/admin.py     212   0  100%   ← было 98% (5 dead code)
bot/handlers/client.py   386   0  100%
bot/keyboards/client.py   63   0  100%
bot/services/booking.py  229   0  100%
bot/services/slots.py      51   0  100%
bot/services/admin.py      47   0  100%
bot/services/notifications.py 33 0 100%
bot/middlewares/session_timeout.py 27 0 100%
bot/main.py               44   2   95%   110-112 (if __name__ block, нужен subprocess)
bot/session.py            24   0  100%   ← было 0%
bot/models.py             73   0  100%
bot/schemas.py            26   0  100%
bot/config.py             15   0  100%
bot/states.py             10   0  100%
bot/db.py                 20   6   70%   22-23, 28-29, 33, 43 (dev/test utilities)
TOTAL                   1275   8   99%
```

### Что НЕ покрыто (8 miss)

- `bot/main.py:110-112` (3 строки) — `if __name__ == "__main__": asyncio.run(main())` — нужен subprocess test, low value
- `bot/db.py:22-23, 28-29, 33, 43` (6 строк) — `create_all()`, `drop_all()`, `dispose()`, `utcnow()` — dev/test utilities

## Что делать следующей сессии

Нет блокирующих багов. Выбери направление (по приоритету ресурса):

### Опц. A — Урок 2.6 Postgres migration (наиболее значимое, high-stakes)

**Pre-existing warning** (зафиксировано, не блокирующее на dev):
- `booking.start_at.astimezone(ZoneInfo(business_tz))` на naive datetime из SQLite интерпретирует naive как system-local TZ. На Render (TZ=UTC) — корректно. Баг проявится при миграции на Postgres с TIMESTAMP WITHOUT TZ на сервере вне UTC.
- `get_client_bookings` в admin.py:152 — `datetime.now(tz=None)` возвращает NAIVE LOCAL (не UTC). Handler `mybookings_msg:430-431` обходит через `datetime.now(UTC)` + strip tzinfo. Service-уровень всё ещё naive local для filter.

**Фикс** — миграция на Postgres с TIMESTAMP WITH TZ (или явная конвертация naive → aware UTC в service).

**Сложность:** большая задача, отдельная сессия.
**Гейты:** high-stakes → `deep-analysis-protocol` Pass 1-4 + `deep-analysis-critic` subagent обязателен.
**Примерный объём:** 4-8 часов (schema migration + service fixes + tests update + Render deployment).

### Опц. B — Stabilise FLAKY test_add_slots_concurrent_race_raises_slot_exists

`asyncio.gather` nondeterminism — иногда 2 winner вместо 1 (UNIQUE constraint race). 1 из 10 запусков.
**Стабилизация:** patched SELECT паттерн (как `test_transfer_booking_concurrent_race_runtime`) — сериализация через DB-level lock или снижение concurrency до 1 (точная race simulation через два последовательных add с проверкой UNIQUE).
**Сложность:** ~30-60 минут, logic-класс.
**Гейты:** `deep-analysis` Pass 1-4 (logic), без critic.

### Опц. C — db.py coverage (6 miss lines → 100% TOTAL)

`create_all`, `drop_all`, `dispose`, `utcnow` — dev/test utilities. Покрытие ~30 минут.
- `create_all` / `drop_all` — запустить на in-memory engine, проверить что таблицы создаются/удаляются
- `dispose` — `await engine.dispose()` + проверить `engine.disposed` flag
- `utcnow` — простая функция, один assert `isinstance(utcnow(), datetime)`

**Сложность:** trivial, без deep-analysis.
**Coverage:** 99% → ~100% (формально, но 2 miss в `if __name__` останутся).

### Опц. D — main.py `if __name__` block (3 miss lines → 100%)

Тест через subprocess: `subprocess.run([sys.executable, "bot/main.py"], env=mock_env, timeout=2)` → проверить что процесс запустился и упал по timeout (или подключился к polling).
**Сложность:** ~20 минут, но subprocess test хрупкий (cross-platform, env isolation).
**Гейты:** deep-analysis Pass 1-4 (subprocess + process lifecycle = state risk).

### Опц. E — Урок 2.5 aiogram_calendar month-navigation

Сейчас `date_picker_keyboard(days_ahead=7)` — простые 7 кнопок. aiogram_calendar в deps для будущей month-navigation.
**Не блокирующее** — 7 дней хватает для MVP.
**Сложность:** medium (~2-3 часа), UX change.

### Опц. F — Pre-existing ruff format drift (9 файлов)

`ruff 0.15` компактирует многострочные string'и до 100 символов. 9 файлов требуют reformat:
`bot/handlers/client.py`, `bot/handlers/start.py`, `bot/keyboards/client.py`, `bot/middlewares/session_timeout.py`, `bot/models.py`, `bot/services/notifications.py`, `tests/test_admin.py`, `tests/test_notifications.py`, `tests/test_scheduler.py`.

**Исправление:** `.venv/bin/ruff format bot/ tests/` + commit.
**Риск:** trivial (reformat не меняет AST), но diff будет на 9 файлах → code-review желателен.
**Сложность:** 5 минут + гейты.

## Рекомендация

| Если есть ресурс | Что делать | Почему |
|---|---|---|
| 5-10 мин | **Опц. F** (ruff format) | Тривиальный reformat, гейты `--check` снова зелёные |
| 30 мин | **Опц. C** (db.py) | Закроет coverage до ~100%, trivial |
| 1 час | **Опц. B** (flaky stabilize) | Уберёт последний известный flaky |
| 4-8 часов | **Опц. A** (Postgres) | Главная техническая задача проекта |

## Quick start prompt для opencode

```
Продолжаем barber-bot Блок 4 — coverage 99% достигнут (3 коммита сессии 2026-08-22).

Прочитай ~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md — там полный контекст:
- Сессия 2026-08-22: dead code (8ca8282) + main/session coverage (80d7690) + S1 fix (ba39d6a)
- 217 тестов, 0 skipped, coverage 99% (8 miss: 3 main.py __name__ + 6 db.py utilities)
- Code-reviewer LGTM, S1 применён, independent review подтвердил

Нет блокирующих багов. Выбери направление:
A. Postgres migration (Урок 2.6, 4-8 часов, high-stakes, deep-analysis-critic)
B. Stabilise flaky test_add_slots_concurrent_race (~30-60 мин, logic, deep-analysis)
C. db.py coverage 6 miss → ~100% (30 мин, trivial)
D. main.py __name__ block subprocess test (20 мин, state risk)
E. aiogram_calendar month-navigation (~2-3 часа, UX)
F. ruff format drift 9 файлов (5 мин, trivial)

Гейты: deep-analysis для B/D (logic/state), critic для A (high-stakes).
Skip для C/F (trivial). Pre-push НЕ нужен (личный репо). Коммитить свободно.
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
| `tests/test_main.py` | bot/main.py wiring + lifecycle (6 тестов) | 158 |
| `tests/test_session.py` | bot/session.py ssl context + CorpAiohttpSession (6 тестов) | 124 |

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

### FLAKY — test_add_slots_concurrent_race_raises_slot_exists

`asyncio.gather` nondeterminism — 1 из 10 запусков даёт 2 winner вместо 1 (UNIQUE race). Не блокирующее (dev-only, single-threaded локально). Для стабилизации — patched SELECT паттерн (как `test_transfer_booking_concurrent_race_runtime`).

### RUFF FORMAT DRIFT — 9 файлов (ruff 0.15 новая компактификация)

`ruff 0.15` компактирует многострочные string'и до 100 символов. 9 файлов не отформатированы под новую версию. Не влияет на AST и runtime. Fix: `.venv/bin/ruff format bot/ tests/` + commit (Опц. F).
