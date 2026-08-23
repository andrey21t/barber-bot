# NEXT_SESSION_PROMPT — Опц. A Postgres migration: готов к external review (4 пробела closed, 5 приняты)

> Дата: 2026-08-23 · Pass 1-4 + 3 critic итерации (iter 3 VERDICT: NEEDS_MORE_ANALYSIS, max 2 LBTM превышен с user consent).
> Код НЕ правили. План augmented с fixes critic iter 3. **Готов к user external review** (last line per Cross-model limitation).
> **После GO от пользователя → Phase 5 implementation.**

## Контекст проекта

**Репозиторий:** `~/PycharmProjects/barber-bot/` (личный pet-проект, коммитить свободно по AGENTS.md § git-repo-categories)
**Стек:** Python 3.12, aiogram 3.x, SQLAlchemy 2.0 async, SQLite (dev) → Postgres (prod), APScheduler
**Spec (SSOT):** `~/PycharmProjects/barber-bot/spec.md` (Урок 2.6 Postgres migration — строки 66-176, 322-393, 413-436)
**Формат работы:** `~/PycharmProjects/barber-bot/MY-VIBE-RULES.md` — dev-режим, гейты: deep-analysis → impl → verify → code-review
**Состояние на старте след. сессии:** 226 тестов, 0 skipped, coverage 99% (2 miss: `bot/main.py __name__`), ruff+mypy чисто, последний коммит `f7aa0a8` (docs close Опц. E)

## Что сделано в сессии 2026-08-23 (анализ, без правок)

1. `deep-analysis-protocol` Pass 1-4 выполнен
2. `deep-analysis-critic` iter 1 → `VERDICT: NEEDS_MORE_ANALYSIS` (10 gaps)
3. 10 gaps применены к плану (overcount 14→11, классификация BREAKS/SAFE, 3 W1 bug locations, keyboards/client.py, test_scheduler.py, alembic.ini, 9 datetime columns, func.now strip, main.py:36 side effect, requirements conflict)
4. `deep-analysis-critic` iter 2 → `VERDICT: NEEDS_MORE_ANALYSIS` (1 CRITICAL blocker)
5. CRITICAL blocker применён к плану: inject `.replace(tzinfo=UTC)` в 4 местах Python comparison (вместо naive removal strip — это вызвало бы TypeError на SQLite)
6. Max 2 LBTM итераций — **INCOMPLETE per protocol**
7. Сессия 2 (продолжение 2026-08-23): user consent на 3-й critic pass
8. **Phase 1 закрыта** — read notifications.py (SAFE), slots.py (SAFE), grep naive assertions в tests (14 найдено, классифицировано)
9. **Phase 3 закрыта** — SQLite scratch stub verify: `with_variant(TIMESTAMP(timezone=True), "postgresql")` возвращает naive на SQLite (то же что без variant, control). Все 226 SQLite тестов продолжат проходить. Postgres asyncpg behavior — docs only (no testcontainers, honest limitation).
10. **Phase 4 — 3-й critic pass** (task tool, subagent_type=deep-analysis-critic, full augmented plan):
    - VERDICT: **NEEDS_MORE_ANALYSIS**
    - **4 CRITICAL gaps**: Python comparisons регрессировали из NEXT_SESSION_PROMPT iter 1. План "aware vs aware" WRONG для 4 deadline computations (`booking.start_at` naive на SQLite → TypeError). Fix: `.replace(tzinfo=UTC)` injection на DB-read side (booking.py:326, 527, client.py:525, 687).
    - **3 MINOR gaps**: alembic 001 missing `idx_bookings_master_start`, `Client.telegram_id unique`, FK `ondelete="CASCADE"` для 2 FK.
11. **Augmented план с critic iter 3 fixes** — ниже. Код НЕ правили.
12. **Статус**: ready for user external review (last line per Cross-model limitation). После GO → Phase 5 implementation.

## Готовый план Session 1 (применять ПОСЛЕ закрытия 9 пробелов)

### Schema (9 columns, `bot/models.py`)
```python
# было: DateTime(timezone=True)
# стало: DateTime(timezone=True).with_variant(TIMESTAMP(timezone=True), "postgresql")
```
9 columns: Business.created_at (34), Master.created_at (48), Service.created_at (66), Client.created_at (81), Slot.created_at (94), Booking.start_at (125), Booking.end_at (126), Booking.created_at (128), NotificationLog.sent_at (156). Убрать TODO comment (23).

### 7 BREAKS — убрать `.replace(tzinfo=None)` workaround'ы + inject `.replace(tzinfo=UTC)` на DB-read side

**CRITICAL (critic iter 3):** в deadline computations `ref` становится aware (после удаления strip), но `booking.start_at` остаётся naive на SQLite → TypeError. Fix: inject `.replace(tzinfo=UTC)` на DB-read side (no-op на Postgres где уже aware, makes naive aware на SQLite).

| File:line | Тип | Fix (final, после critic iter 3) |
|---|---|---|
| `bot/services/booking.py:569` | INSERT start_at (transfer_booking) | убрать strip: `start_at=new_start_at` (SQLAlchemy coerces aware → TIMESTAMPTZ; SQLite strips на bind) |
| `bot/services/booking.py:570` | INSERT end_at (transfer_booking) | убрать strip: `end_at=new_end_at` |
| `bot/services/admin.py:161` | SQL WHERE (get_today_bookings) | убрать strip (SQLAlchemy handles aware vs aware на Postgres; SQLite lexicographic) |
| `bot/services/booking.py:325-326` (cancel_booking) | Python comparison (24h rule) | убрать `ref_naive = ref.replace(tzinfo=None)` → использовать `ref` (aware). **CRITICAL:** `cancel_deadline = booking.start_at.replace(tzinfo=UTC) - timedelta(...)` (was: `booking.start_at - timedelta(...)`) |
| `bot/services/booking.py:520-527` (transfer_booking) | Python comparison (24h rule) | то же: `ref` без strip. **CRITICAL:** `cancel_deadline = old_start_at.replace(tzinfo=UTC) - timedelta(...)` (was: `old_start_at - timedelta(...)`) |
| `bot/handlers/client.py:509` (mybookings) | Python comparison + .astimezone | убрать `now_utc = now_utc_aware.replace(tzinfo=None)` → использовать `now_utc_aware`. **CRITICAL:** `deadline = b.start_at.replace(tzinfo=UTC) - timedelta(...)` (was: `b.start_at - timedelta(...)`) на line 525. W1 fix на line 516: `b.start_at.replace(tzinfo=UTC).astimezone(tz)`. |
| `bot/handlers/client.py:686-687` (transfer pre-check) | Python comparison | убрать `now_utc = datetime.now(UTC).replace(tzinfo=None)` → использовать `datetime.now(UTC)`. **CRITICAL:** `deadline = booking.start_at.replace(tzinfo=UTC) - timedelta(...)` (was: `booking.start_at - timedelta(...)`) |

### 3 W1 explicit fix (inject `.replace(tzinfo=UTC)`)
- `bot/handlers/admin.py:377` — `b.start_at.replace(tzinfo=UTC).astimezone(tz)`
- `bot/handlers/client.py:516` — то же
- `bot/keyboards/client.py:143` — то же

### KEEP (не трогать)
- `bot/handlers/client.py:92, 149` — SAFE (aiogram_calendar/date calc, не DB)
- 8 `.replace(tzinfo=None)` в `tests/*` (test_admin.py:108,693,702; test_admin_handlers.py:197; test_client_handlers.py:193,1499,1740,1754; test_booking.py:515) — SQLite tests, naive OK
- `bot/services/booking.py:377, 635, 655` — уже имеют `.replace(tzinfo=UTC)`

### alembic/versions/001_initial.py (hand-written, cross-DB) — augmented с critic iter 3 fixes

7 tables БЕЗ EXCLUDE (Postgres-only, separate migration 002). Cross-DB compatible:
- `BigInteger().with_variant(Integer, "sqlite")` для NotificationLog.id (models.py:147)
- 4 CHECK constraints: `slot_hour BETWEEN 0 AND 23`, `duration_minutes > 0`, `end_at > start_at`, `kind IN (...)`
- 4 partial indexes с `postgresql_where` (на Postgres, ignored на SQLite)
- 3 UNIQUE indexes: `ux_slots_master_date_hour`, `ux_bookings_slot`, `ux_notifications_booking_kind`
- **(critic iter 3 add)** `idx_bookings_master_start` regular composite index (master_id, start_at) — models.py:138
- **(critic iter 3 add)** `Client.telegram_id` unique=True implicit index — models.py:78
- **(critic iter 3 add)** FK `ondelete="CASCADE"` для `Slot.master_id` (models.py:89) и `NotificationLog.booking_id` (models.py:152) — Alembic должен явно renderить ON DELETE CASCADE на Postgres
- UUID: Python-side `default=uuid.uuid4` (пробел 4 — рекомендация A, pet-project simplicity). Update spec.md note: "механизм = Python-side, не server-side gen_random_uuid()"

### Infra files
- `pyproject.toml`: `[project.optional-dependencies].prod = ["asyncpg>=0.29", "psycopg2-binary>=2.9", "alembic>=1.13"]`
- `alembic.ini` (root): config from `DATABASE_URL` env, `script_location = alembic`
- `alembic/env.py`: reads DATABASE_URL from settings, `target_metadata = Base.metadata`, online mode
- `.env.example`: добавить `DATABASE_URL_SYNC=` placeholder (config.py:18 уже имеет default "")

## 9 пробелов — статус после сессии 2 (анализ продолжен)

### Пробел 1 — `bot/services/notifications.py` — ЗАКРЫТ (SAFE)

`read` выполнен полностью (79 строк). `get_overdue_bookings_without_remind_24h` (строки 35-61) и `get_upcoming_bookings_for_reschedule` (64-79) — все сравнения `Booking.start_at > window_start` (lines 45-47) и `Booking.start_at < now` (line 47) в **SQL WHERE**. Нет `.replace(tzinfo=None)`, нет Python comparison `booking.start_at - timedelta(...)`, нет `.astimezone()`. На Postgres TIMESTAMPTZ aware UTC сравнивается с `datetime.now(UTC)` aware — корректно. Никаких правок не нужно.

### Пробел 2 — `bot/services/slots.py` — ЗАКРЫТ (SAFE)

`read` выполнен полностью (102 строки). `add_slots` (15-55), `close_slot` (62-83), `get_available_slots` (86-102). Поля: `slot_date` (date), `slot_hour` (int), `status`, `created_at` (default=func.now()). Никаких datetime comparisons. Никаких правок не нужно.

### Пробел 3 — `alembic/versions/001_initial.py` content — ЗАКРЫТ (Phase 2 design, augmented с critic iter 3)

См. раздел "alembic/versions/001_initial.py" выше — hand-written, cross-DB, 7 tables + 4 CHECKs + 4 partial indexes + 3 UNIQUE + 1 regular composite + 1 implicit unique (telegram_id) + 2 FK CASCADE. Решение: hand-written для детерминизма (autogenerate может пропустить partial indexes с `postgresql_where`).

### Пробел 4 — UUID default — ЗАКРЫТ (Phase 2 decision: A)

**Решение: A (Python-side `default=uuid.uuid4`)** — простота, работает на обеих БД, pet-project. Spec.md update (механизм = Python-side, не server-side `gen_random_uuid()`). Альтернатива B (server-side) отклонена: на старом Postgres нужен pgcrypto, на SQLite нужен fallback — over-engineering для pet-project.

### Пробел 5 — asyncpg aware UTC — ЗАКРЫТ (Phase 3 verification)

SQLite scratch stub (выполнен в сессии 2):
```python
# Test: with_variant vs без variant, both SQLite
[with_variant] Inserted: aware UTC, Returned: tzinfo=None, value=2026-08-23 12:00:00
[no variant]   Inserted: aware UTC, Returned: tzinfo=None, value=2026-08-23 12:00:00
[with_variant] Inserted: naive,    Returned: tzinfo=None, value=2026-08-23 12:00:00
```

**Вывод:** `DateTime(timezone=True).with_variant(TIMESTAMP(timezone=True), "postgresql")` на SQLite возвращает naive (то же что без variant). Все 226 SQLite тестов ПРОДОЛЖАТ проходить. На Postgres (по SQLAlchemy/asyncpg docs): `TIMESTAMP(timezone=True)` → TIMESTAMPTZ column → asyncpg возвращает aware UTC (стандартное поведение asyncpg). Empirical Postgres test нет (constraint: no testcontainers, overkill для pet-project).

**Verdict:** confirmed (SQLite empirical) + assumed (Postgres docs). Honest limitation.

### Пробел 6 — 3-й critic pass — ЗАВЕРШЁН (NEEDS_MORE_ANALYSIS, max 2 LBTM превышен с user consent)

3-я итерация critic выполнена (task tool, subagent_type=deep-analysis-critic). VERDICT: **NEEDS_MORE_ANALYSIS** — найдено 4 CRITICAL + 3 MINOR gaps. Все fixes применены к augmented плану выше (см. раздел "7 BREAKS" с `.replace(tzinfo=UTC)` injection + alembic section с 3 add'ами).

Per protocol: max 2 LBTM — 3-я требует user consent (получено: "Запусти всё что нужно"). Дальше — **user external review** (last line per Cross-model limitation). Код НЕ правили. Ждём GO от пользователя для Phase 5.

### Пробел 7 — Postgres tests НЕТ (no testcontainers)

Все 226 тестов на SQLite. Postgres behavior (TIMESTAMPTZ, asyncpg aware, EXCLUDE constraint) — предположение. Узнаем только при deploy на Render.

**Что принять:** Honest limitation. Pet-project, constraint 3 (no testcontainers — overkill). Альтернатива: manual smoke test после deploy (запись брони через bot, проверка что `/today` показывает корректное время).

**Зафиксировать в PLANS:** "Postgres behavior не покрыт тестами. Smoke test после Session 2 deploy".

### Пробел 8 — Cross-model correlation (одна модель LiteLLM)

Main и critic на одной модели. Общие слепые пятна не ловятся. Это уменьшение sycophancy/confirmation bias, не панацея.

**Что принять:** Honest limitation (per `deep-analysis-protocol` Cross-model limitation). Mitigation — пробел 6 (C): внешний review пользователя.

### Пробел 9 — naive assertions в tests — ЗАКРЫТ (классификация)

`grep -rn "tzinfo is None\|tzinfo is not None\|assert.*naive" tests/` найдено 14 мест, классифицировано:

**9 BREAK-ON-POSTGRES** (НЕ править в Session 1, остаются для dev SQLite):
- `tests/test_admin.py:178, 215, 409, 410, 411, 579, 651, 681` — `assert result[0].start_at == _utc_naive(...)`. Сравнивают SQLite naive с naive. На Postgres (aware vs naive) → TypeError.
- `tests/test_client_handlers.py:254` — `assert b.start_at.tzinfo is None`. На Postgres будет `timezone.utc`.

**3 SAFE** (продолжат работать на Postgres):
- `tests/test_db.py:106` — `assert result.tzinfo is not None` (db.utcnow() aware — корректно на обеих БД)
- `tests/test_client_handlers.py:1453, 1454` — `assert min_date.tzinfo is None / max_date.tzinfo is None` (calendar range, не DB roundtrip)
- `tests/test_booking.py:239` — `assert result.start_at.tzinfo is not None` (service возвращает in-memory aware объект до roundtrip)

**Honest limitation:** SQLite tests не верифицируют Postgres behavior для datetime comparisons. Smoke test после deploy (запись брони через bot, проверка /today корректного времени). Fix для Session 2+: либо cross-DB helper `_aware_utc()` (возвращает aware на Postgres, naive на SQLite), либо Postgres-only test suite с testcontainers (out of scope MVP).

## План на следующую сессию (фазы)

### Phase 1 — Read missed files (30 мин)
1. `read bot/services/notifications.py` полностью — классифицировать `.replace(tzinfo=None)` и Python comparisons
2. `read bot/services/slots.py` полностью — верифицировать safe
3. `grep -rn "tzinfo is None\|tzinfo is not None\|assert.*naive" tests/` — найти naive assertions
4. `grep -rn "astimezone.*b\.start_at\|booking\.start_at\.astimezone" tests/` — найти `.astimezone()` patterns

**Выход:** обновлённая классификация BREAKS/SAFE + список tests assertions для проверки.

## План на следующую сессию — Phase 5 (после user external review GO)

Phases 1-4 закрыты в сессии 2026-08-23 (продолжение). Осталась только Phase 5.

### Phase 5 — User external review → GO → Session 1 implementation (~2-3 часа)

1. **External review (user)** — пользователь читает augmented план (этот файл, секции 1-5). Если OK → "go" / "правь" / "apply". Если найдёт gaps → корректирует, я дорабатываю план.
2. **Implementation** — Session 1 (применять augmented план):
   - Schema change (models.py: 9 columns with_variant + убрать TODO comment + import TIMESTAMP)
   - 7 BREAKS: убрать `.replace(tzinfo=None)` + inject `.replace(tzinfo=UTC)` на DB-read side (4 deadline computations + 3 .astimezone)
   - alembic/versions/001_initial.py + alembic/env.py + alembic.ini
   - pyproject.toml: `[project.optional-dependencies].prod`
   - .env.example: `DATABASE_URL_SYNC=`
   - spec.md: note "механизм = Python-side, не server-side gen_random_uuid()"
3. **`qa-verify-and-fix`**: pytest 226 + ruff + mypy + coverage (99% preserved — SQLite behavior не меняется)
4. **`qa-code-review`**: code-reviewer subagent на logic-change (strip workaround removed, `.replace(tzinfo=UTC)` injection added)
5. **Коммиты свободно** (pet-project, AGENTS.md § git-repo-categories)
6. **Postgres smoke test** после deploy (Session 2): записать бронь через bot, проверить /today корректное время.

**Выход:** Session 1 закрыта. Готово к Session 2 (Scheduler SQLAlchemyJobStore + Render + EXCLUDE constraint migration 002 — отдельная сессия).

## Quick start prompt для opencode (обновлён после сессии 2)

```
Продолжаем barber-bot Опц. A (Postgres migration) — финальная проверка перед Phase 5 implementation.

Прочитай ~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md — там полный контекст:
- Сессия 2026-08-23 (продолжение): Phase 1-4 закрыты.
- 3-й critic pass выполнен с user consent — VERDICT: NEEDS_MORE_ANALYSIS (4 CRITICAL + 3 MINOR gaps).
- Все critic iter 3 fixes применены к augmented плану (см. секции "7 BREAKS" и "alembic/versions/001_initial.py" выше).
- 4 из 9 пробелов ЗАКРЫТЫ: 1 (notifications.py SAFE), 2 (slots.py SAFE), 5 (asyncpg — SQLite stub + docs), 9 (naive assertions — 14 классифицировано).
- 3 пробела решены в Phase 2: 3 (alembic 001 design), 4 (UUID = A Python-side), 6 (3-й critic done).
- 2 пробела ПРИНЯТЫ как honest limitations: 7 (no Postgres tests, smoke after deploy), 8 (cross-model correlation, mitigation = user external review).

Статус: READY FOR USER EXTERNAL REVIEW (last line per Cross-model limitation).
После GO от пользователя ("go" / "правь" / "apply") → Phase 5 implementation.

Phase 5 (после GO):
1. Implementation: Schema (models.py 9 columns with_variant) + 7 BREAKS (strip + .replace(tzinfo=UTC) injections) + alembic 001 + pyproject + .env.example + spec.md note
2. qa-verify-and-fix: pytest 226 + ruff + mypy
3. qa-code-review: code-reviewer subagent на logic-change
4. Коммиты свободно (pet-project)

Гейты: deep-analysis Pass 1-4 + 3 critic итерации (выполнены). Pre-push НЕ нужен (личный репо).
```

## Файлы для быстрого ориентирования

| Файл | Что | Строк |
|---|---|---|
| `spec.md` | SSOT — Урок 2.6 Postgres migration (строки 66-176, 322-393, 413-436) | 541 |
| `MY-VIBE-RULES.md` | Формат работы + гейты (dev-режим) | 79 |
| `bot/models.py` | 7 SQLAlchemy 2.0 models, 9 DateTime(timezone=True), with_variant precedent (147) | 164 |
| `bot/db.py` | Async engine, session_factory, create_all/drop_all | 43 |
| `bot/services/booking.py` | create/cancel/transfer booking, 7 BREAKS workarounds (325, 520, 569, 570), W1 explicit fix (377, 635, 655) | 671 |
| `bot/services/admin.py` | get_today/week_bookings, get_client_bookings (W2 at 157-161), W1 bug (377) | 165 |
| `bot/services/notifications.py` | on_startup helpers — ЧИТАН, SAFE (все comparisons в SQL WHERE) | 79 |
| `bot/services/slots.py` | add_slots, close_slot, get_available_slots — ЧИТАН, SAFE (нет datetime comparisons) | 102 |
| `bot/handlers/admin.py` | /addslots, /closeslot, /today, /week, /services, W1 bug (377) | 383 |
| `bot/handlers/client.py` | booking FSM + SimpleCalendar + /mybookings + /cancel + /transfer, 2 BREAKS (509, 686), W1 bug (516) | 856 |
| `bot/keyboards/client.py` | inline keyboards, W1 bug (143), comments (6, 75) | 174 |
| `bot/main.py` | entry point, scheduler = build_scheduler() at module level (36) | 112 |
| `bot/config.py` | Settings, DATABASE_URL_SYNC="" placeholder (18) | 30 |
| `scheduler.py` | AsyncIOScheduler + MemoryJobStore (dev), schedule_for_booking, on_startup_scan | 126 |
| `tests/test_scheduler.py` | test_build_scheduler_memory_jobstore (75-80) — продолжит проходить с conditional build_scheduler | 240 |
| `tests/test_booking.py` | booking service tests + 12 transfer + concurrent race | ~2053 |
| `tests/test_admin.py` | services/admin tests, .replace(tzinfo=None) at 108, 693, 702 | 703 |
| `tests/test_admin_handlers.py` | admin handlers tests, .replace(tzinfo=None) at 197 | 1282 |
| `tests/test_client_handlers.py` | client handlers tests, naive assert at 254, .replace(tzinfo=None) at 193, 1499, 1740, 1754 | ~2720 |
| `tests/test_main.py` | bot/main.py wiring + lifecycle + subprocess | 227 |
| `tests/test_session.py` | CorpAiohttpSession ssl context | 124 |
| `tests/test_db.py` | bot/db.py create_all/drop_all/dispose/utcnow | 109 |
| `tests/test_slots.py` | slots service tests + concurrent race | 285 |
| `tests/conftest.py` | in-memory SQLite fixtures, seed_data | 144 |
| `alembic/` | каталог пустой (нет env.py, нет versions/*, нет alembic.ini в root) | — |
| `pyproject.toml` | Python 3.12, deps без asyncpg/psycopg2/alembic | — |
| `.env.example` | BOT_TOKEN, DATABASE_URL=sqlite, ADMIN_ID — нет DATABASE_URL_SYNC | — |

## Гейты (напоминание)

- **deep-analysis-protocol** Pass 1-4 на нетривиальное
- **deep-analysis-critic** ОБЯЗАТЕЛЕН для high-stakes (миграции) — max 2 LBTM, 3-я требует user consent
- **qa-verify-and-fix** после правок: pytest + ruff + mypy + coverage (все зелёные)
- **qa-code-review** на logic-change (через `code-reviewer` subagent)
- **Pre-push**: НЕ нужен — barber-bot личный репо (AGENTS.md § git-repo-categories)
- **Коммитить свободно** — pet-проект, без переспроса

## Pre-existing Warnings (carry-over, не блокирующие на dev)

- **W1** — `booking.start_at.astimezone` на naive (SQLite workaround в booking.py:377, 635, 655). 3 bug locations вне booking.py (admin.py:377, client.py:516, keyboards/client.py:143) — будут explicit fix в Session 1.
- **W2** — `datetime.now(tz=None)` naive local (admin.py:152 comment, fix в admin.py:157-161). Полный fix — Session 1 (убрать workaround).
- **W3** — `assert` в production при `python -O` (admin.py:129, 205, 339). Acceptable для pet-project без `-O` в deploy.
- **F1** (code-reviewer) — ЗАКРЫТО (commit 3f55355 + 17b2a19 regression test).
- **RUFF FORMAT DRIFT** — ЗАКРЫТО (commit 68df6ab).
- **FLAKY test_add_slots_concurrent_race** — ЗАКРЫТО (commit 0fac563).

## Cross-refs

- `~/.config/opencode/skills/deep-analysis-protocol/SKILL.md` — гейт протокол
- `~/.config/opencode/AGENTS.md` § git-repo-categories — barber-bot = personal free
- `~/.config/opencode/references/donor-research/topics/booking-bot-architecture.md` — BB-011 (SQLAlchemyJobStore + psycopg2), BB-012 (on_startup_scan) — подтверждает two-engine подход
