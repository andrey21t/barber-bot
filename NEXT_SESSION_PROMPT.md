# NEXT_SESSION_PROMPT — Session 5.19: шаг 5.6 «Мест нет» (occupancy check) — Pass 1-4 готов, можно impl.

> Дата: 2026-08-29 · Session 5.18 завершена (commits `48d80d6` + `163ba33` pushed, 314 passed + 2 skipped). Эта сессия (5.19-prep, 2026-08-29) — deep-analysis Pass 1-4 для шага 5.6 + handoff. Impl в Session 5.19.
> Prod остаётся на `c64b31d` (Session 5.9). Миграция 005 НЕ накатывалась на prod — smoke-test на dev-копии обязателен (one-way door, данные Екатерины).

## ⚡ TL;DR для Session 5.19 (impl)

**Цель 5.19:** шаг **5.6 «Мест нет»** — occupancy check для /slots (по `max_concurrent_clients`, не slot UNIQUE). `get_30min_slots_from_workday` (5.4) генерирует grid; новый `get_available_slots_30` фильтрует по occupancy. /slots UI (5.8, backlog после 5.19) вызывает обе.

**Главный артефакт:** этот файл + `PLANS.md` (Session 5.18 section + Session 5.5 known limitations). Читается ПЕРВЫМ для контекста.

**Risk-класс 5.19: `logic`** (не MEDIUM, не high-stakes — уточнено Pass 1). Новая функция в сервисе, persistence touch — читает Bookings через SELECT; без миграции, без security, без concurrency (pet project single admin single process). Pass 1-4 обязателен, deep-analysis-critic skip (logic < 80 строк, не high-stakes). Pass 1-4 УЖЕ ВЫПОЛНЕН в 5.19-prep сессии (2026-08-29) — см. раздел «Pass 1-4 resolved» ниже.

## Статус Этапа 5 Вариант B (на 2026-08-28):

- ✅ 5.2 WorkDay + миграция 005 (commits `90c0000` + `9345a7b`)
- ✅ 5.3 WorkDay invariants (commit `aedf1a0`, +7 → 281)
- ✅ 5.4 30-мин шаг + admin.py Booking.start_at filter (commit `069274d`, +5 → 286)
- ✅ 5.5 multi-client (commit `379ebd6`, +10 → 296)
- ✅ 5.1 `/openday` command + FSM (commit `147081e`, +13 → 309)
- ✅ **5.18 F1 UX-fix + 5 handler-тестов на cmd_openday** (готово, 314 passed, без commit на момент записи)
- 5.6 / 5.8 / 5.9 / 5.10 / aliases / миграция 006 — backlog

## 5.18 — что сделано (детали в PLANS.md:222-256)

- **F1 fix (variant B, handler-level):** `bot/services/workday.py:221` переименование `_select_workday` → `select_workday` (drop underscore, экспорт). `bot/handlers/admin.py` cmd_openday + admin_openday_end_msg — перед `open_workday` вычисляют `was_closed = existing is not None and not existing.is_active`, используют `was_closed` для UX-сообщения (вместо `workday.is_active`, который всегда True после re-open).
- **5 handler-тестов (NEW, `tests/test_openday_handlers.py`):**
  - `test_openday_command_text_args_creates_workday` — happy path
  - `test_openday_idempotent_update_no_duplicate_row` — UPDATE, не INSERT
  - `test_openday_shrink_error_message_rendered` — WorkDayShrinkError UX
  - `test_openday_reopen_shows_message` — **F1 regression**: was_closed=True → сообщение показывается
  - `test_openday_open_no_reopen_message_when_was_active` — **F1 complement**: was_closed=False → сообщение НЕ показывается
- **Гейты 5.18:** deep-analysis trivial (skip Pass 2-3, 1-строчный self-verify), qa-verify-and-fix (ruff + mypy + pytest 314 passed), qa-code-review skip (semantic UX, не logic; auto-trigger scope к playtest, не barber-bot), pre-push skip (pet-project).

## 5.6 «Мест нет» — что проектировать в 5.19

### Контракт

`/slots` (команда 5.8 backlog) показывает список 30-мин свободных окон в WorkDay мастера на выбранную дату. Свободное окно = кол-во активных bookings в нём < `WorkDay.max_concurrent_clients`. Если таких окон нет → пользователь видит «мест нет на эту дату».

5.6 — реализовать occupancy calculation (часть 5.8). 5.8 — UI (/slots command + slot picker keyboard). Решить в 5.19: 5.6 + 5.8 одной сессией или 5.6 отдельно.

### Pass 1-4 resolved (2026-08-29, 5.19-prep)

**Pass 1 — Понимание:**
- Класс: новая фича (occupancy calculation)
- Risk: `logic` (не high-stakes — < 80 строк, без security/state/concurrency)
- Discover existing work: 6 находок (см. ниже «grep-карта» — все file:line confirmed)

**Pass 2 — Edge cases (resolved):**
- [✓] Touch vs overlap: half-open `Booking.start_at < slot_end AND Booking.end_at > slot_start` (mirror `_check_multi_client_capacity` booking.py:226-227)
- [✓] Booking > 30 мин (service.duration_minutes может быть > 30): booking [11:00, 12:30] занимает 3 grid cells [11:00-11:30, 11:30-12:00, 12:00-12:30] — все 3 unavailable. Overlap measured against **full booking range**, не против 30-min cell.
- [✓] Status filter: `confirmed`/`transferred` only (mirror `_check_multi_client_capacity:228`)
- [✓] Booking выходит за WorkDay окно: невозможно — `_validate_booking_within_workday` (5.3 invariant) + shrink check (5.1 Gap 6)
- [✓] Empty workday (window < 30 мин): `get_30min_slots_from_workday` → `[]` → `get_available_slots_30` → `[]`
- [✓] Capacity=1, 2, any ≥1: CheckConstraint `ck_workday_capacity_positive` (models.py:163) — no ZeroDivision
- [⚠️] Closed workday (is_active=False): НЕ фильтруется в `get_available_slots_30` — handler 5.8 решает (separation of concerns). Документирую в docstring.
- [⚠️] Race (SELECT vs /book): pet project single admin; `create_booking` (5.5 B1+B2) обрабатывает advisory lock + `_check_multi_client_capacity` — final safety net. UX: handler 5.8 показывает «место только что заняли».
- [⚠️] **Naive vs aware datetime** (SQLite vs Postgres): `_window_bounds_utc` возвращает aware UTC. `Booking.start_at` на SQLite — naive. Python-level сравнение `b.start_at < slot_end_utc` упадёт с TypeError. **Решение: normalize в Python loop** — `b.start_at if b.start_at.tzinfo else b.start_at.replace(tzinfo=UTC)`. SQLAlchemy handles на SQL уровне, но Python loop — нет.

**Pass 2.5 — Research Path:**
- Owning layer: `bot/services/slots.py` — рядом с `get_30min_slots_from_workday` (тема одна — slots). НЕ в workday.py (он про open/close/shrink). НЕ в handler (UI mapping).
- Horizontal neighbors: `bot/services/slots.py` (extend), `tests/test_slots.py` (extend). `bot/handlers/client.py:177` legacy `get_available_slots` — НЕ трогать.

**Pass 3 — State-переходы:**
- Lifecycle: pure read-only (только SELECT, no mutation)
- Invariants preserved: `max_concurrent_clients >= 1` (models.py:163), half-open overlap, status filter, UTC storage
- One-way doors: нет (read-only, без миграции)

**Pass 4 — Self-verify (VERDICT: pass):**

Verified (реальные tool calls в 5.19-prep):
- `grep -n "get_30min_slots_from_workday" bot/services/slots.py` → slots.py:55 (function exists, returns `list[TimeSlot30]`)
- `grep -n "_window_bounds_utc" bot/services/workday.py` → workday.py:233 (private, returns `tuple[datetime, datetime]` aware UTC)
- `grep -n "_acquire_advisory_lock" bot/services/workday.py` → workday.py:45 (pattern import private from booking — ok import `_window_bounds_utc`)
- `grep -n "max_concurrent_clients" bot/models.py` → models.py:154 `default=1`, 163 `CheckConstraint >= 1` (no ZeroDivision)
- `grep "get_30min_slots_from_workday\|TimeSlot30\|occupancy" tests/test_slots.py` → empty (gap — нет тестов на grid generation, закрыть в 5.19)
- `wc -l tests/test_slots.py` → 272 (extend безопасно, тема одна — slots)
- `pytest --co -q | tail -1` → 314 tests baseline confirmed

Not verified: реальный `pytest` на новой функции (напишу в 5.19 impl). Postgres-специфичное (только SQLite в test session).

Run needed (для 5.19 impl): `ruff check bot/services/slots.py tests/test_slots.py` + `mypy --strict bot/services/slots.py tests/test_slots.py` + `pytest tests/test_slots.py -v` + `pytest -q` (full: 314 → ~324, +10)

### Что НУЖНО сделать в 5.19 (planned diff, после Pass 1-4)

| Файл | Что | Объём |
|---|---|---|
| `bot/services/slots.py` | Добавить `get_available_slots_30(session, workday, business_tz, *, now_utc=None) -> list[TimeSlot30]` + import `Booking` (models), `_window_bounds_utc` (workday), `timedelta` (datetime), `UTC` (datetime — если ещё не импортирован) | ~40 строк |
| `tests/test_slots.py` | Extend: +8-10 occupancy тестов + `_insert_booking` helper (mirror `test_workday_service._direct_insert_booking:41`) | ~150 строк |

**Алгоритм `get_available_slots_30`** (copy-paste в slots.py, документация в docstring):

```python
async def get_available_slots_30(
    session: AsyncSession,
    workday: WorkDay,
    business_timezone: str,
    *,
    now_utc: datetime | None = None,
) -> list[TimeSlot30]:
    """Filter 30-min slots from workday grid by occupancy (capacity check).

    Этап 5.6 (PLANS.md:15): /slots UI (5.8) shows 30-min slots that have at least
    one free capacity — count of overlapping active bookings (status IN
    'confirmed'/'transferred', half-open overlap) < workday.max_concurrent_clients.

    Half-open overlap (mirror booking.py:_check_multi_client_capacity:226-227):
        booking.start_at < slot.end_utc AND booking.end_at > slot.start_at_utc
    Booking range crosses multiple grid cells (service.duration_minutes > 30):
    all cells overlapped become occupied — overlap measured against **full
    booking range**, not the 30-min grid cell.

    Status filter: only 'confirmed'/'transferred' (cancelled excluded, mirror
    _check_multi_client_capacity:228).
    Past slots: filtered in get_30min_slots_from_workday via now_utc injection.
    Closed WorkDay (is_active=False): NOT filtered here — handler 5.8 decides
    (separation of concerns; this function is read-only, no policy).

    NB: SQLite stores Booking.start_at naive, Postgres aware. Normalize in
    Python loop (b.start_at.replace(tzinfo=UTC) if b.start_at.tzinfo is None).
    SQLAlchemy handles at SQL level, but Python-level comparison does not.

    Args:
        session: SQLAlchemy AsyncSession (read-only SELECT).
        workday: WorkDay record — provides master_id, work_date, start_time,
            end_time, max_concurrent_clients.
        business_timezone: IANA tz name (e.g. "Europe/Moscow") for LOCAL → UTC.
        now_utc: injected for tests (production uses datetime.now(UTC)).

    Returns:
        List of TimeSlot30 (subset of get_30min_slots_from_workday output)
        ordered by start_at_utc ascending. Empty if all slots are occupied OR
        workday window < 30 min.
    """
    candidates = await get_30min_slots_from_workday(
        workday, business_timezone, now_utc=now_utc
    )
    if not candidates:
        return []

    workday_start_utc, workday_end_utc = _window_bounds_utc(
        workday.work_date, workday.start_time, workday.end_time, business_timezone
    )
    bookings_stmt = (
        select(Booking)
        .where(
            Booking.master_id == workday.master_id,
            Booking.start_at < workday_end_utc,
            Booking.end_at > workday_start_utc,
            Booking.status.in_(("confirmed", "transferred")),
        )
    )
    bookings = (await session.execute(bookings_stmt)).scalars().all()
    # Normalize naive SQLite datetimes to aware UTC for Python-level comparison.
    normalized = [
        (
            b.start_at if b.start_at.tzinfo else b.start_at.replace(tzinfo=UTC),
            b.end_at if b.end_at.tzinfo else b.end_at.replace(tzinfo=UTC),
        )
        for b in bookings
    ]

    available: list[TimeSlot30] = []
    for slot in candidates:
        slot_end_utc = slot.start_at_utc + timedelta(minutes=30)
        overlap_count = sum(
            1 for bs, be in normalized
            if bs < slot_end_utc and be > slot.start_at_utc
        )
        if overlap_count < workday.max_concurrent_clients:
            available.append(slot)
    return available
```

**Тест-план (8-10 тестов, extend `tests/test_slots.py`):**
1. `test_get_available_slots_30_empty_workday_no_bookings` — all grid slots available (cap=1)
2. `test_get_available_slots_30_capacity_1_one_booking_covers_slot` — slot unavailable
3. `test_get_available_slots_30_capacity_1_booking_crosses_3_cells` — 3 slots unavailable
4. `test_get_available_slots_30_capacity_2_one_booking` — slot still available (count=1 < 2)
5. `test_get_available_slots_30_capacity_2_two_bookings_overlap_slot` — slot unavailable (count=2)
6. `test_get_available_slots_30_cancelled_excluded` — cancelled booking не блокирует slot
7. `test_get_available_slots_30_touch_edge_no_overlap` — booking [11:00, 11:30] не блокирует slot [11:30, 12:00]
8. `test_get_available_slots_30_past_slots_filtered` — now_utc injection, past grid cells не возвращаются
9. (optional) `test_get_available_slots_30_closed_workday_not_filtered` — функция не падает на is_active=False (handler 5.8 filter)
10. (optional) `test_get_available_slots_30_window_less_than_30min` — defensive, empty result

### Что НЕ трогать в 5.19

- `bot/services/workday.py` существующие функции (`open_workday`, `update_workday`, `close_workday`, `select_workday`) — корректны, verified в 5.1 + 5.18.
- `bot/services/booking.py` `_check_multi_client_capacity` (5.5, korректен).
- `bot/handlers/admin.py` cmd_openday + admin_openday_end_msg (F1 fix 5.18 — корректен).
- `tests/test_workday_service.py` (13 тестов), `tests/test_openday_handlers.py` (5 тестов 5.18) — 0 регресса ожидается.

### Гейты для 5.19

- **deep-analysis-protocol: logic change** (новая функция расчёта occupancy) → Pass 1-4 обязателен. Pass 1 risk-class: logic (не trivial, не high-stakes). Pass 4 self-verify обязательный. Если сомнения в edge cases (touch, status filter, DST) → optional `deep-analysis-critic` subagent pass.
- **qa-verify-and-fix:** ruff + mypy --strict + pytest (314 → ~324, +10 occupancy тестов).
- **qa-code-review:** рекомендуется (новая logic в сервисе, не handler UX). Auto-trigger: logic-change → playtest+woodworking (но barber-bot personal). Запустить по желанию.
- **Pre-push review:** skip (barber-bot personal pet-project).

## grep-карта для Session 5.19

### 5.6 — читать по якорям (file:line confirmed в 5.19-prep Pass 4):

| Якорь | Что читать | Зачем |
|---|---|---|
| `PLANS.md:15` | 5.6 backlog entry | Что проектировать |
| `PLANS.md:222-256` | Session 5.18 — F1 fix + handler-тесты | Что готово (5.18) |
| `bot/services/slots.py:55-117` | `get_30min_slots_from_workday` + `TimeSlot30` (5.4) | Переиспользовать для grid gen |
| `bot/services/slots.py:79-82` | Комментарий про occupancy (5.6 NOT here) | Доказательство separation of concerns |
| `bot/services/booking.py:178-240` | `_check_multi_client_capacity` (5.5) | Образец overlap + status filter + `excluded_booking_id` |
| `bot/services/workday.py:233-251` | `_window_bounds_utc` | UTC-окно из LOCAL time, возвращает aware tuple |
| `bot/services/workday.py:45` | `from bot.services.booking import _acquire_advisory_lock` | Pattern: import private из соседнего сервиса — ok |
| `bot/models.py:154,163` | `max_concurrent_clients` default=1, CheckConstraint ≥1 | Invariant — no ZeroDivision |
| `bot/models.py:175-211` | `class Booking` + CheckConstraint + partial index | Schema: status IN confirmed/transferred (DB-level optimization) |
| `tests/test_workday_service.py:41-82` | `_direct_insert_booking` + `_local_to_utc` helpers | Шаблон для occupancy тестов (переиспользовать) |
| `tests/test_slots.py:1-80` | Образец теста на slots service | Шаблон + убедиться что не дублирует (legacy slots — не 30-мин) |
| `tests/test_multi_client.py` | Образец capacity tests | Шаблон для cap=2 cases |

## Quick start prompt для Session 5.19 (impl)

```
Продолжаем barber-bot Session 5.19 — impl шага 5.6 «Мест нет» (occupancy check
для /slots по max_concurrent_clients). Deep-analysis Pass 1-4 УЖЕ ВЫПОЛНЕН в
5.19-prep (2026-08-29) — см. NEXT_SESSION_PROMPT.md раздел «Pass 1-4 resolved».

== ЧТО ДЕЛАТЬ (impl) ==
1. Прочитай ~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md (этот файл) —
   раздел «Pass 1-4 resolved» + «Что НУЖНО сделать» с готовым алгоритмом
   get_available_slots_30 (copy-paste) + «Тест-план» (8-10 тестов).
2. Реализуй get_available_slots_30 в bot/services/slots.py (~40 строк).
3. Extend tests/test_slots.py: +8-10 occupancy тестов + _insert_booking helper
   (mirror test_workday_service._direct_insert_booking:41).
4. Гейты: ruff + mypy --strict bot/+tests/ + pytest (314 → ~324, +10).
5. qa-code-review (recommended, не skip — logic change в сервисе).
6. Pet-project git free per AGENTS.md § git-repo-categories — коммить без
   переспроса после зелёных гейтов.

== Risk-класс: logic (УТОЧНЁН в Pass 1, не MEDIUM) ==
- НЕ high-stakes (logic < 80 строк, без security/state/concurrency)
- Pass 1-4 уже выполнен в 5.19-prep — VERDICT: pass (см. раздел)
- deep-analysis-critic SKIP (logic, не high-stakes)
- Deep-analysis-gate плагин: поиск VERDICT в assistant text — Pass 4 verdict
  уже в NEXT_SESSION_PROMPT.md, процитируй при начале impl

== НЕ ТРОГАТЬ ==
- bot/services/workday.py существующие функции (open/update/close/select_workday)
- bot/services/booking.py _check_multi_client_capacity (5.5)
- bot/handlers/admin.py cmd_openday (F1 fix 5.18)
- bot/handlers/client.py:177 legacy get_available_slots (Slot-based, deprecated)
- tests/test_workday_service.py (13), tests/test_openday_handlers.py (5) — 0 регресса

== ГЕЙТЫ ==
- deep-analysis: ✓ Pass 1-4 готов в 5.19-prep (VERDICT: pass)
- qa-verify-and-fix: ruff + mypy --strict bot/+tests/ + pytest (314 → ~324, +10)
- qa-code-review: рекомендуется (новая logic в сервисе; auto-trigger scope к
  playtest+woodworking, barber-bot personal — по желанию, но logic change
  рекомендую запустить для ловли edge cases)
- Pre-push: skip (pet-project git free)
- Commit message: feat(services): /slots occupancy check — get_available_slots_30
  + 8-10 tests (Этап 5.6)
```

## Исторический контекст (Sessions 5.15-5.18 — для быстрой ориентации)

> Детальные итоги сессий 5.10 и ранее — в архиве (не нужны для 5.19).
>
> **Сводка по последним сессиям:**
>
> - **Session 5.18** (без commit на момент записи): F1 UX-fix (variant B) + 5 handler-тестов на cmd_openday. 314 passed. См. PLANS.md Session 5.18 section.
> - **Session 5.17** (commit `147081e`, pushed): impl 5.1 /openday + workday service. 309 passed. Known UX-bug F1 (отсрочен в 5.18, fixed).
> - **Session 5.16** (commit `379ebd6`, pushed): impl 5.5 multi-client + LBTM fixes (F1+W1+W2). 296 passed.
> - **Session 5.15** (БЕЗ коммитов, analysis only): deep-analysis Pass 1-4 + critic iter 1 NEEDS_MORE_ANALYSIS → iter 2 plan.
> - Sessions 5.10 и ранее — в архиве (blueprint.md:30 convention, не нужны для 5.19).
