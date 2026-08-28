# NEXT_SESSION_PROMPT — Session 5.18 завершён (F1 fix + 5 handler-тестов), Session 5.19 — шаг 5.6 «Мест нет» (occupancy check).

> Дата: 2026-08-28 · Session 5.18 — фикс UX-бага F1 (variant B, handler-level) + 5 handler-тестов на cmd_openday. **309 → 314 passed + 2 skipped** (ruff + mypy --strict зелёные). Без commit на момент записи (рабочее дерево чистое на start).
> Prod остаётся на `c64b31d` (Session 5.9). Миграция 005 НЕ накатывалась на prod — smoke-test на dev-копии обязателен (one-way door, данные Екатерины).

## ⚡ TL;DR для Session 5.19

**Цель 5.19:** шаг **5.6 «Мест нет»** — occupancy check для /slots (по `max_concurrent_clients`, не slot UNIQUE). Предыдущие 5.4 (30-мин шаг) + 5.5 (multi-client capacity check в create_booking) уже готовы. 5.6 = рассчитать доступность 30-мин окон в WorkDay с учётом capacity и показать «мест нет» когда все занято.

**Главный артефакт:** `PLANS.md` (Session 5.18 section + Session 5.5 known limitations). Читается ПЕРВЫМ для контекста.

**Risk-класс 5.19: MEDIUM.** Logic change в сервисе (новая функция расчёта occupancy) + persistence-layer touch (читает Bookings). НЕ one-way door (без миграции). Deep-analysis нужен (Pass 1-4 + опционально critic iter 1, если есть сомнения в алгоритме).

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

### Гипотезы для deep-analysis Pass 1

- **Алгоритм:** для каждого 30-мин слота [t, t+30min] в окне [workday.start_time, workday.end_time]: count bookings с `master_id == X AND status IN ('confirmed', 'transferred') AND start_at < slot.end AND end_at > slot.start`. Если count < capacity → слот свободен.
- **Edge: touch vs overlap.** Booking [11:00, 11:30] и slot [11:30, 12:00] — НЕ overlap (half-open `[)`, как `_check_multi_client_capacity` booking.py:225).
- **Edge: booking выходит за WorkDay окно.** Booking [19:00, 20:00] в WorkDay [10:00, 20:00] — влезает на edge. Если WorkDay сужают до [10:00, 18:00] — `update_workday` уже отказывает (Gap 6 shrink check). Окно всегда содержит bookings (invariant enforced в 5.3).
- **Status filter:** только `confirmed` и `transferred` (mirror `_check_multi_client_capacity`). `cancelled` — не считает.
- **Связанный place для функции:** `bot/services/workday.py` (рядом с `select_workday`) или новый `bot/services/slots_calc.py`. Hypothesis — workday.py (он уже про окна).

### Что НУЖНО сделать в 5.19 (приблизительно)

| Файл | Что | Объём (оценка) |
|---|---|---|
| `bot/services/workday.py` (или новый `slots_calc.py`) | `get_available_slots_30(session, master_id, work_date, business_tz) -> list[Slot30]` — occupancy calculation | ~50-80 строк + тесты |
| `tests/test_workday_slots.py` (extend) | тесты на occupancy: cap=1 happy, cap=2 с 1 booking, cap=2 с 2 bookings → no slots, cancelled excluded, touch-edge OK | ~150 строк |
| (опционально, если 5.6+5.8 одной сессией) `bot/handlers/client.py` + `bot/keyboards/client.py` | `/slots` command + BookSlot30CallbackData picker | ~100-150 строк |

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

### 5.6 — читать по якорям:

| Якорь | Что читать | Зачем |
|---|---|---|
| `PLANS.md:15` | 5.6 backlog entry | Что проектировать |
| `PLANS.md:222-256` | Session 5.18 — F1 fix + handler-тесты | Что готово (5.18) |
| `bot/services/workday.py:63-117` | open_workday + re-open ветка | Понять WorkDay state flow |
| `bot/services/workday.py:221-229` | `select_workday` (5.18 export) | Helper для occupancy |
| `bot/services/booking.py:225-280` | `_check_multi_client_capacity` | Образец overlap calc (half-open, status filter) |
| `bot/services/workday.py:_window_bounds_utc` (232+) | UTC-окно из LOCAL time | Если occupancy работает в UTC |
| `tests/test_workday_service.py:1-100` | Образец теста на workday service | Шаблон для occupancy тестов |
| `tests/test_workday_slots.py` (existing) | Что уже есть по slots | Не дублировать |

## Quick start prompt для Session 5.19

```
Продолжаем barber-bot Session 5.19 — шаг 5.6 «Мест нет» (occupancy check для
/slots по max_concurrent_clients).

Сначала:
1. Прочитай ~/PycharmProjects/barber-bot/PLANS.md (Session 5.18 section — что
   готово; line 15 — что 5.6 значит; Session 5.5 known limitations — про
   capacity check).
2. Прочитай ~/PycharmProjects/barber-bot/NEXT_SESSION_PROMPT.md (этот файл) —
   гипотезы для deep-analysis Pass 1 + что НЕ трогать.

== ЦЕЛЬ 5.19 ==
Шаг 5.6: реализовать occupancy calculation для /slots. /slots показывает
30-мин свободные окна в WorkDay. Свободное окно — count активных bookings
(status IN ('confirmed','transferred'), overlap [start, end) half-open)
< WorkDay.max_concurrent_clients.

== РИСК ==
MEDIUM. Logic change в сервисе (новая функция occupancy). НЕ one-way door
(без миграции). Deep-analysis Pass 1-4 обязателен. Pass 4 self-verify.
Optional critic iter 1 если есть edge case сомнения (touch vs overlap, DST,
status filter consistency с _check_multi_client_capacity).

== ГЕЙТЫ ==
- deep-analysis: logic → Pass 1-4 обязателен. Optional critic subagent pass.
- qa-verify-and-fix: ruff + mypy --strict + pytest (314 → ~324, +10)
- qa-code-review: рекомендуется (новая logic; auto-trigger scope к playtest,
  barber-bot personal — по желанию)
- Pre-push: skip (pet-project)

== ФОРМАТ ==
MY-VIBE-RULES.md — dev-режим, гейты. Pet-project, git free per AGENTS.md
§ git-repo-categories.

== ВАЖНО ==
НЕ копируй файлы целиком. Используй grep-карту выше. PLANS.md — читать
Session 5.18 section + line 15 (5.6 backlog) + 5.5 known limitations.
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
