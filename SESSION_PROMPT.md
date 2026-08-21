# SESSION_PROMPT — barber-bot Блок 3 часть 5 — что после coverage gap

> Полный промт для старта следующей сессии. Self-contained — можно скопировать
> целиком в opencode. Файл `~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md`
> содержит историю предыдущих сессий (не обязателен к чтению — этот промт
> самодостаточен).

---

## Копировать в opencode для старта (вся секция ниже между маркерами)

---

Продолжаем barber-bot Блок 3 часть 5 — что после coverage gap.

## Контекст проекта

**Репозиторий:** `~/PycharmProjects/barber-bot/` (личный pet-проект, коммитить свободно по AGENTS.md § git-repo-categories — без переспроса и без pre-push ревью).
**Стек:** Python 3.12, aiogram 3.x, SQLAlchemy 2.0 async, SQLite (dev) → Postgres (prod), APScheduler 3.x.
**Спека (SSOT):** `~/PycharmProjects/barber-bot/spec.md` — строки 41, 318, 408-409 (контракты переноса — реализованы полностью).
**Формат работы:** `~/PycharmProjects/barber-bot/MY-VIBE-RULES.md` — dev-режим (без педагогики, код сразу), резюме после блока, гейты: deep-analysis на нетривиальное → реализация → verify → code-review.

## Что уже сделано в прошлой сессии (3 коммита, всё зелёное)

### commit `bfab1fb` — fix(cancel_booking): naive → aware UTC consistency (W1 mirror)
- `bot/services/booking.py:384`: `local_time = booking.start_at.replace(tzinfo=UTC).astimezone(ZoneInfo(business_tz))` — mirror фикса из transfer_booking (commit c38087b, W1). Consistency: cancel_booking теперь использует тот же naive → aware UTC паттерн что transfer_booking. 1 строка + 4 строки комментария. Тесты на точное время не требовали update (assertions только `startswith("Отмена:")`).

### commit `6171a75` — test(transfer_booking): concurrent race runtime test
- `tests/conftest.py`: 2 новых fixture — `engine_concurrent` (file-based SQLite через `tmp_path`, не in-memory — т.к. in-memory + QueuePool даёт каждому connection свою DB) + `session_factory_concurrent`.
- `tests/test_booking.py:1082` — `test_transfer_booking_concurrent_race_runtime` (~200 строк): faithful runtime тест race protection, замена static-invariant lock (`inspect.getsource`).
  - **Подход (deterministic, no asyncio.gather)**: 5 шагов — seed → snapshot (detached stale Booking) → B wins fully (commits new start_at) → A's transfer_booking с patched SELECT (returns stale_booking, simulates A captured snapshot before B's commit) → UPDATE WHERE start_at=old fails (DB has new_B) → rowcount=0 → BookingAlreadyTransferredError.
  - **Почему не asyncio.gather**: не гарантирует оба SELECT'а до UPDATE — A может полностью выиграть, B re-transfer'нёт успешно, гонка не сработает. Manual orchestration через patched SELECT даёт детерминированный race scenario.
  - **Test-the-test**: временно удалял `Booking.start_at == old_start_at` из WHERE clause → тест падал `DID NOT RAISE BookingAlreadyTransferredError`. WHERE pin восстановлен.
- Static-invariant test (`test_transfer_booking_concurrent_transfer_race_protection` через `inspect.getsource`) оставлен как дешёвый regression lock.

### commit `b9185f9` — test(transfer_handler): cover 11 transfer_slot_cb + transfer_date_cb error branches
- `tests/test_client_handlers.py` (+521 строка): 11 новых handler-тестов для покрытия gaps из coverage report.
  - **transfer_slot_cb (9 тестов)**: already_transferred (race protection), already_cancelled, cancel_too_late, slot_in_past, slot_closed, booking_not_found, unknown_user_no_client, state_data_lost, slot_not_available.
  - **transfer_date_cb (2 теста)**: happy_path, no_slots_on_date.
- **Race ordering verification**: helper `_make_state_with_call_order(call_log)` записывает `state.clear` + `transfer_booking` в общий list; assertion `call_log.index("state.clear") < call_log.index("transfer_booking")` ловит переупорядочивание (усиление W1 из code-review — `assert_awaited()` ловил только факт вызова, не ordering). Test-the-test: удалял `await state.clear()` из handler → тест упал. Восстановлено.
- **Pattern**: monkey-patch `client_handlers.transfer_booking` для service-raised exceptions (без concurrent race orchestration — она покрыта на service level). Direct DB seed для non-error тестов (date picker).

### Состояние тестов

```
pytest (full suite): 89 passed in 6.5s
ruff: All checks passed
mypy: 32 source files, no issues

Coverage (pytest-cov):
  bot/services/booking.py:    228 stmts, 26 miss,  89% cover
  bot/handlers/client.py:     386 stmts, 159 miss, 59% cover (было 45%)
  TOTAL:                      614 stmts, 185 miss, 70% cover (было 61%)
```

Code-review subagent по двум задачам (concurrent race test + handler coverage tests) — VERDICT LGTM, 0 critical.

## Что делать этой сессии (нет блокирующих багов)

Выбери одно из 3 направлений (по приоритету ресурса). Если пользователь не уточнил — спроси что он хочет.

### Опц. 1 — Postgres migration (БОЛЬШАЯ задача, отдельная сессия, deep-analysis нужен)

**Цель:** мигрировать с SQLite на Postgres для production deploys на Render.

**Зачем:** production deploys на Render используют Postgres. SQLite — dev-only. Pre-existing warnings (W1 naive datetime в SQLite, W2 get_client_bookings admin.py:152) исчезнут если Postgres schema использует TIMESTAMP WITH TZ и service-код конвертирует naive → aware.

**Это БОЛЬШАЯ задача** — отдельная сессия, не комбинировать с другими опциями. Включает:
- alembic migration (создать Postgres schema)
- Docker compose для локального Postgres
- Изменить `bot/db.py` engine URL (env-driven: `DATABASE_URL` env var)
- Audit всех `datetime.now()` calls в service — убедиться что все aware UTC
- Прогнать все тесты на Postgres (not SQLite) — найти SQLite-специфичные assumptions:
  - `Booking.start_at` naive (SQLite игнорирует `DateTime(timezone=True)`) → Postgres вернёт aware
  - `CHECK constraints` — SQLite vs Postgres синтаксис
  - `StaticPool`-style fixtures — file-based SQLite → Postgres test container
  - `uuid.uuid4` default — Postgres имеет native UUID type
- Update README + spec.md (env vars, docker compose instructions)
- Coverage повторить на Postgres (могут всплыть SQLite-only behavior)

**Гейты:** deep-analysis Pass 1-4 (concurrency risk: Postgres isolation levels vs SQLite, STATE risk: schema migration). qa-verify-and-fix. qa-code-review (logic-change на all service). Возможен deep-analysis-critic subagent (high-stakes — миграция персистентной schema).

**Спроси пользователя готов ли он потратить сессию целиком на миграцию** — это не вписывается в остаток ресурса.

### Опц. 2 — transfer_date_cb coverage gaps (~15-20 мин, logic, deep-analysis нужен)

**Цель:** покрыть 2 оставшиеся ветки `transfer_date_cb` из code-review S1.

**Зачем:** coverage report показал что `transfer_date_cb` (client.py:650-702) покрыт только happy + no-slots. Остались:
- `client.py:664-666` — `ValueError` branch для невалидного ISO date (`callback.answer("Невалидная дата")`)
- `client.py:677-682` — `master is None` branch (state.clear + message.answer("❌ Не удалось найти мастера") + callback.answer)

**Шаги:**
1. `test_transfer_date_cb_invalid_iso` — `BookDateCallbackData(iso="not-a-date")` → `callback.answer("Невалидная дата")`, state NOT cleared.
2. `test_transfer_date_cb_master_not_found` — monkey-patch `select(Master)` query или seed без master под `ADMIN_ID` → state.clear + message.answer + callback.answer. Замечание: `_seed_full_stack` всегда создаёт master — нужно либо переопределить `get_settings` (вернуть другой ADMIN_ID), либо monkeypatch DB query.
3. Verify: pytest + ruff + mypy зелёные.
4. Coverage перепроверить (handler должен подрасти с 59% до ~65%).
5. Commit: `test(transfer_date_cb): cover invalid_iso + master_not_found branches (code-review S1)`.

**Гейты:** deep-analysis Pass 1+2+4 (logic, new test behavior). qa-verify-and-fix. qa-code-review (logic-change на test fixture).

### Опц. 3 — Scheduler FLAKY test stabilization (~10-15 мин, deep-analysis НЕ нужен)

**Цель:** стабилизировать `test_scheduler::test_on_startup_scan_phase_2_reschedules_upcoming` через `freezegun` (или аналог для APScheduler).

**Зачем:** test time-of-day dependent — иногда падает в зависимости от реального текущего времени (например, если запущен в полночь или при переходе через границы слотов). Pre-existing warning, не регрессия этой сессии.

**Шаги:**
1. Прочитать `tests/test_scheduler.py` — найти `test_on_startup_scan_phase_2_reschedules_upcoming` и понять что делает time-of-day dependent.
2. Найти аналоги в проекте (или freezegun convention) — `grep -rn "freezegun\|freeze_time" tests/`.
3. Если freezegun нет в зависимостях — добавить через `uv pip install freezegun --system-certs`.
4. Обернуть тест в `@freeze_time("2026-01-15 12:00:00")` (фиксированное время).
5. Прогнать 3-5 раз подряд — убедиться что стабильно проходит.
6. Verify: pytest + ruff + mypy.
7. Commit: `test(scheduler): stabilize on_startup_scan_phase_2 with freezegun (FLAKY fix)`.

**Гейты:** deep-analysis НЕ нужен (trivial — freeze_time wrapper, no logic change). qa-verify-and-fix. qa-code-review НЕ нужен (trivial test fix, no logic change).

## Гейты (напоминание, MY-VIBE-RULES.md)

- **Deep-analysis** на нетривиальное (Опц. 1 — да + возможен critic, Опц. 2 — да, Опц. 3 — нет). Skip для trivial.
- **Verify-and-fix** перед «готово»: `pytest`, `ruff check .`, `mypy bot scheduler.py tests` — все зелёные.
- **Code-review** subagent после verify для logic-change (Опц. 1 — да, Опц. 2 — да, Опц. 3 — нет).
- **Pre-push ревью**: НЕ нужно — barber-bot личный репо (AGENTS.md § git-repo-categories).
- **Коммитить свободно** — pet-проект, без переспроса. Коммит = конец логической единицы работы.

## Файлы для быстрого ориентирования

| Файл | Что | Строк |
|---|---|---|
| `spec.md` | SSOT — строки 41, 318, 408-409 — контракты переноса (реализованы) | 541 |
| `MY-VIBE-RULES.md` | Формат работы + FSM edge cases + rules | 79 |
| `bot/handlers/client.py` | Booking flow + /mybookings + cancel + transfer FSM (transfer_slot_cb:711-837, transfer_date_cb:650-702) | 837 |
| `bot/services/booking.py` | create + cancel + transfer_booking (race protection + W1/W2 fixes, cancel_booking W1 fixed) | 688 |
| `bot/services/admin.py` | get_today/week_bookings, create_service, get_client_bookings (W2: naive datetime, fixed by Postgres migration) | 160 |
| `bot/services/slots.py` | add_slots, close_slot, get_available_slots | 102 |
| `bot/keyboards/client.py` | Inline keyboards + Cancel + Transfer CallbackData + mybookings_keyboard (2 кнопки/booking) | ~180 |
| `bot/models.py` | 7 таблиц — Booking (status confirmed/transferred/cancelled), Slot, NotificationLog | 184 |
| `bot/states.py` | BookingStates + TransferStates | 28 |
| `bot/config.py` | Settings: CANCEL_MIN_HOURS=24, REMINDER_24H_BEFORE, REMINDER_1H_BEFORE | 29 |
| `bot/db.py` | async engine, async_session_factory (expire_on_commit=False) — SQLite URL hardcoded (нужно env-driven для Postgres migration) | ~30 |
| `scheduler.py` | build_scheduler, schedule_for_booking, remove_jobs_for_booking, on_startup_scan | 126 |
| `tests/test_booking.py` | booking service tests + cancel + transfer (13 тестов, включая concurrent race runtime) | ~1280 |
| `tests/test_admin.py` | Pattern для тестов на services (timezone edge cases, _utc_naive helper) | 545 |
| `tests/test_client_handlers.py` | Handler тесты mybookings_msg + cancel_cb + transfer_cb + transfer_date_cb + transfer_slot_cb (23 теста, +11 в прошлой сессии) | ~1480 |
| `tests/test_scheduler.py` | APScheduler tests (1 FLAKY test — on_startup_scan_phase_2) | ~? |
| `tests/conftest.py` | session_factory fixture + engine_concurrent (file-based SQLite) + seed_data | 144 |

## Pre-existing Warnings (зафиксировано, не блокирующее)

### W1 — naive datetime + astimezone (ИСПРАВЛЕНО в cancel_booking И transfer_booking)
`cancel_booking:384` и `transfer_booking:644` теперь оба используют `replace(tzinfo=UTC).astimezone(ZoneInfo(business_tz))`. Фиксы применены в commits bfab1fb (cancel) и c38087b (transfer).

### W2 — get_client_bookings (admin.py:152) — ЕЩЁ НЕ ИСПРАВЛЕНО
`datetime.now(tz=None)` возвращает NAIVE LOCAL (не UTC). Handler mybookings_msg:430-431 обходит это (использует `datetime.now(UTC)` и strip tzinfo). Service-уровень admin.py:152 всё ещё naive local. Фикс — Урок 2.6 (Postgres migration, Опц. 1).

### FLAKY test (ЕЩЁ НЕ STABILIZED)
`test_scheduler::test_on_startup_scan_phase_2_reschedules_upcoming` — time-of-day dependent. Не регрессия. Stabilize через freezegun — Опц. 3.

## Coverage gaps (остаток после прошлой сессии)

Handler coverage 59% — оставшиеся gaps:
- `client.py:664-666` — `ValueError` на невалидном ISO date в `transfer_date_cb` → Опц. 2
- `client.py:677-682` — `master is None` в `transfer_date_cb` → Опц. 2
- `client.py:96-145, 160-237, 259-364` — booking flow handlers (booking, confirm_cb, cancel_input) — НЕ наш coverage gap, они покрыты косвенно через `test_create_booking_happy_path` и `test_cancel_booking_*` в test_booking.py. Если хотим handler-level coverage для booking flow — отдельная задача (не в этом промте).

## Если пользователь спрашивает "что мы делали в прошлой сессии"

Краткое резюме (5 строк):
- 3 коммита: `bfab1fb` (cancel_booking W1 fix — naive → aware UTC consistency с transfer), `6171a75` (concurrent race runtime test — замена static-invariant lock на faithful runtime test с file-based SQLite + patched SELECT), `b9185f9` (11 handler-тестов transfer_slot_cb + transfer_date_cb — закрыли coverage gap 45% → 59%).
- 89 тестов зелёные (78 → 89, +11), ruff + mypy чисто.
- Race protection теперь покрыта статически (inspect.getsource regression lock) И runtime (faithful test с real UPDATE WHERE clause).
- Race ordering `state.clear() BEFORE service call` теперь проверяется через `call_log.index()` (усиление из code-review W1).
- Code-review subagent по 2 задачам — VERDICT LGTM, 0 critical.
- Нет блокирующих багов. Следующий шаг — опциональный (Postgres migration, transfer_date_cb coverage, или FLAKY test stabilization).

---
