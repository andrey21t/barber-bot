# NEXT_COVERAGE_GAPS.md — B1+B2+B3 closed (2026-08-21 session 2)

> Продолжение NEXT_COVERAGE_GAPS.md (T7+T8 closed). Сессия закрыла B1+B2+B3.
> TOTAL coverage: 91% → 92% (B1: booking.py 96% → 100%). Tier 2 НЕ сделан (опционально).

---

## Что пофикшено в этой сессии (B1+B2+B3)

| Bug | Файл:line | Severity | Fix | Commits |
|---|---|---|---|---|
| B1 | bot/services/booking.py:166 | Medium | capture slot_id before _select_or_create_client | 0df168e |
| B2 | bot/services/slots.py:52 | Low | generalise SlotAlreadyExistsError message | 58c64ea |
| B3 (bonus) | bot/services/booking.py:612 | Medium | use new_slot_id param in transfer_booking error | 180ed0e |

**Итог TOTAL:** 1280 stmts, 112 miss (91%) → 1281 stmts, 103 miss (92%).
**Merge commit:** 3117434.
**Все гейты:** ruff format + ruff check + mypy + pytest зелёные (192 passed, 1 skipped FK).
**Independent review:** deep-analysis-critic DEEP_ENOUGH (B1 high-stakes); code-reviewer LGTM (B1, 0 critical, 2 warnings — test non-determinism LOW + misleading comment fixed in commit).

### B1 — slot.id after rollback in create_booking
- **Cause:** `_select_or_create_client` calls `session.rollback()` on concurrent client INSERT race (lines 111-123). Rollback expires all attached instances including `slot` (loaded at line 149). Subsequent `slot.id` access (line 166 Booking constructor, line 183 error msg, line 189 UPDATE WHERE, line 197 error msg, line 230 BookingCreatedData) triggered lazy load → MissingGreenlet (sync access to async session).
- **Fix:** `slot_id = slot.id` capture immediately after `_select_open_slot` (line 149). Use `slot_id` (local UUID var) instead of `slot.id` everywhere after capture.
- **Test:** `test_create_booking_client_race_integrity_error` — removed `@pytest.mark.skip`. Test exercises the race via asyncio.gather on 2 sessions, same new telegram_id, 2 different slots. Assertions: 2 successes, 1 client, 2 bookings.
- **Side-effect:** Updated misleading comment in `_select_or_create_client` (was "Safe to rollback" — now warns about expiration and that callers must capture slot attributes before calling).
- **Bonus from critic + reviewer:** B3 found (see below) — same pattern in `transfer_booking`.

### B2 — wrong hour in SlotAlreadyExistsError message
- **Cause:** `f"Slot {master_id}/{slot_date}/{hour} already exists"` used `hour` loop variable. After IntegrityError catch, `hour` carries last iterated value, NOT the conflicting hour. `add_slots(session, master_id, date, [14, 15])` with conflict on 14 → message said ".../15 already exists" (wrong hour).
- **Fix:** Generalise message to `f"Slot {master_id}/{slot_date} — one of hours already exists"` (no specific hour). More accurate semantically — constraint is composite.
- **Test:** `test_add_slots_concurrent_race_raises_slot_exists` uses hours=[14] (single hour) — bug not exercised. `isinstance(errors[0], SlotAlreadyExistsError)` assertion doesn't match specific message → fix doesn't break test.

### B3 — new_slot.id after rollback in transfer_booking (bonus from B1 code-review)
- **Cause:** Same pattern as B1. `transfer_booking:612` accessed `new_slot.id` on attached Slot instance AFTER `session.rollback()` (line 610, rowcount=0 path on UPDATE new_slot).
- **Latent:** Test `test_transfer_booking_concurrent_new_slot_taken_raises_slot_already_booked` bypasses the bug via DETACHED `stale_new_slot` (Slot(id=...) constructor, never session.add()'d) — test passes, but production on attached instance would fail with MissingGreenlet.
- **Fix:** Use function parameter `new_slot_id` (immutable UUID from signature, already in scope) instead of `new_slot.id` attribute on line 612. Other `new_slot.id` accesses (lines 568, 606, 644) are BEFORE outer rollback (line 610) → safe, left unchanged.
- **Severity:** Medium. Same race condition family as B1. Triggers on concurrent transfer where new_slot gets taken between SELECT (line 536) and UPDATE (line 605).

---

## Остатки — что делать в следующей сессии

### Tier 2 — coverage sweep handlers/keyboards (НЕ сделано в этой сессии)

Промт говорил "После B1+B2 — опционально Tier 2 → ~94%". Решили пропустить: Tier 2 это новые тесты handlers, не bug fixes. Отдельная coverage-sweep сессия (T9+T10).

| Файл | Lines | Stmts | Type |
|---|---|---|---|
| bot/handlers/admin.py | 66, 128-129, 133, 136-137, 210, 242, 345 | 9 | admin edge cases |
| bot/handlers/client.py | 293-297, 406, 501-502, 525-526, 555, 588-589, 612-613, 741-742 | 17 | client handler edges |
| bot/keyboards/client.py | 102-103, 124 | 3 | keyboard edges |
| bot/main.py | 16-112 | 44 | bootstrap (mock aiogram Dispatcher) |
| bot/session.py | 1-40 | 24 | aiogram internals (low value) |
| bot/db.py | 22-23, 28-29, 33, 43 | 6 | dev/test utilities |
| bot/handlers/client.py:293-297 | - | - | business_not_found FK violation (db surgery) |

### TOTAL ceiling
- Текущий: 92% (103 miss).
- После Tier 2 (admin/client/keyboards, 29 stmts): ~94%.
- Реалистичный потолок без main.py/session.py: ~94% (64 miss).
- С main.py/session.py: ~99% (нужно mock aiogram internals, отдельная сессия).

---

## Что НЕ сделано в этой сессии (явно)

- ❌ Tier 2 (admin/client/keyboards edges) — пропущено (опционально, отдельная сессия).
- ❌ main.py / session.py coverage — mock aiogram internals, отдельная сессия.
- ❌ Postgres migration — out of scope (high-stakes — отдельная сессия с deep-analysis-critic).
- ❌ Refactoring — только B1+B2+B3 fixes + тесты (B1 test unskipped).

---

## Итог сессии (резюме)

**Что сделали:**
- B1 (booking.py 96% → 100%, 1 test unskipped) — slot.id после rollback в _select_or_create_client race path.
- B2 (slots.py, error message wording) — wrong hour в SlotAlreadyExistsError.
- B3 (booking.py transfer_booking:612, bonus) — new_slot.id после rollback, same pattern as B1.
- TOTAL coverage: 91% → 92% (1281 stmts, 112 miss → 103 miss).
- 192 тестов проходит, 1 skipped (FK violation edge, отдельный skip — НЕ B1).
- Все гейты зелёные: ruff format + ruff check + mypy + pytest.
- Independent review: deep-analysis-critic DEEP_ENOUGH (B1 high-stakes), code-reviewer LGTM (B1, 0 critical).

**Зачем продукту:** закрыли production race conditions в booking create + transfer. B1 — concurrent client INSERT race (double-tap newUser в Telegram). B3 — concurrent transfer race (пользователь делает transfer, пока кто-то занял new_slot). Оба latent — не проявляются на dev/одиночных запросах, но в production на concurrent actions дают MissingGreenlet → 500 на клиенте.

**Базовая вещь:** SQLAlchemy session.rollback() экспирирует ВСЕ attached instances независимо от expire_on_commit=False. После rollback доступ к любому атрибуту attached instance триггерит lazy load. В async SQLAlchemy sync lazy load = MissingGreenlet. Паттерн fix: capture needed attributes (особенно PK) в local variables BEFORE любой rollback-prone операции. Mirror pattern уже был в transfer_booking (old_slot_id capture at line 534) — теперь применён последовательно.

**Что НЕ сделали:** Tier 2 (admin/client/keyboards edges, 29 stmts, ~2% coverage gain). Промт говорил "опционально". Оставлено для отдельной coverage-sweep сессии (T9+T10).
