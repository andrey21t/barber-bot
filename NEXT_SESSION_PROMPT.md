# NEXT_SESSION_PROMPT — barber-bot, coverage Tier 2 (admin.py 66% → ~88%)

## Контекст

Продолжаем закрывать coverage gaps Tier 2 в `admin.py` (66% → цель ~88%).

**Репо:** `~/PycharmProjects/barber-bot/` (личный pet-проект, коммитить свободно, AGENTS.md § git-repo-categories).
**Стек:** Python 3.12, aiogram 3.x, SQLAlchemy 2.0 async, SQLite (dev), APScheduler 3.x, freezegun.
**Формат работы:** `~/PycharmProjects/barber-bot/MY-VIBE-RULES.md` — dev-режим (код сразу, без педагогики), резюме после блока, гейты: deep-analysis на нетривиальное → реализация → verify → code-review.
**Last commits:**
- `1098ccf` test(admin): cover admin_addslots_cb 4 branches
- `975ad00` test(admin): cover cmd_openday 11 branches
- `512986b` test(client): mybookings_cancel_cb rebook button
- `7f30e11` feat(admin): 5.61 — week navigation

**Гейты на старте:** ruff ✅ · mypy 2 pre-existing errors (`open_workday`, `AdminStates` attr-defined — НЕ ЧИНИТЬ, не наши) · pytest 601 passed / 2 skipped.

**Coverage baseline:** TOTAL 80% (admin.py 66% / 651 miss из 1937 stmts).

## Проверь себя (ОБЯЗАТЕЛЬНО в начале сессии)

1. `cd ~/PycharmProjects/barber-bot && git log --oneline -5` — последний коммит должен быть `1098ccf` или новее.
2. `uv run pytest --cov=bot --cov-report=term 2>&1 | tail -25` — TOTAL должен быть 80%, admin.py 66%.
3. `uv run ruff check . 2>&1 | tail -3` — должен быть "All checks passed".
4. `uv run mypy bot tests 2>&1 | tail -10` — должно быть 6 errors (pre-existing: 2 в admin_handlers, 2 в admin_move, 2 в client_handlers). НЕ мои.

Если что-то не так — откатись к `1098ccf` (`git reset --hard 1098ccf`), рапорт INCOMPLETE.

## Задача: закрыть ~8% coverage gaps (80% → ~88%)

Промт шёл сверху вниз. Если на задаче идёт 40+ минут — пропусти, оставь на следующую.

**Паттерн тестов** (один для всех, см. `tests/test_admin_handlers.py:301-340` для cmd_*, `:2147-2189` для callback'ов):

```python
# Command handler (cmd_*):
async with session_factory() as session:
    await _seed_admin_stack(session)
tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
msg = _make_message(user_id=ADMIN_TG_ID, text=f"/openday {tomorrow} 11:00 18:00")
await admin_handlers.cmd_openday(msg, _make_command(f"{tomorrow} 11:00 18:00"))
text = _answer_text(msg)
assert "✅" in text

# Callback handler (admin_*_cb):
async with session_factory() as session:
    await _seed_admin_stack(session)
callback = _make_callback(ADMIN_TG_ID)
state = _make_mock_state({...})
await admin_handlers.admin_X_cb(callback, state)
state.set_state.assert_called_once_with(admin_handlers.AdminStates.X)
```

Хелперы: `_make_message`, `_make_callback`, `_make_mock_state`, `_answer_text`, `callback_answer_text`, `_seed_admin_stack`. Все уже есть в `tests/test_admin_handlers.py`.

### T2.1. admin_openday_start_msg (1255-1283, ~28 строк) — ~20 мин

**Файл:** `bot/handlers/admin.py:1249-1289`

**Что покрывать:**
- Non-admin → silent return (no answer)
- Master not found → `❌ Мастер не найден` + state.clear
- Bad time format (не HH:MM) → `❌ Время должно быть ЧЧ:ММ` + state.clear
- Happy → state.update_data(start_time) + state.set_state(picking_window_end) + calendar answer

**Гейты:** deep-analysis Pass 1-2 (FSM state-переходы), pytest+ruff, code-reviewer (FSM state change — logic change).

**Коммит:** `test(admin): cover admin_openday_start_msg 4 branches (T2.1)`

### T2.2. admin_openday_end_msg (1297-1371, ~74 строки) — ~35 мин

**Файл:** `bot/handlers/admin.py:1290-1406`

**Что покрывать:**
- Non-admin → silent return
- Master not found → `❌ Мастер не найден` + state.clear
- Bad time format → error message + state.clear
- end <= start → `❌ Конец должен быть позже начала` + state.clear
- Happy → state.update_data(end_time) + state.set_state(confirming_window) + summary answer with keyboard

**Гейты:** те же. Может потребоваться `_make_mock_state({"start_time": "11:00"})`.

**Коммит:** `test(admin): cover admin_openday_end_msg 5 branches (T2.2)`

### T2.3. admin_openweek_edit_start_cb (1864-1915, ~51 строка) — ~25 мин

**Файл:** `bot/handlers/admin.py:1852-1920`

**Что покрывать:**
- Non-admin → callback.answer + return
- Master not found → `❌ Мастер не найден` alert + return
- State has no selected_date → `❌ Данные сессии потеряны` + state.clear
- Happy → state.set_state(opening_week_edit_start) + answer with keyboard

**Гейты:** те же.

**Коммит:** `test(admin): cover admin_openweek_edit_start_cb 4 branches (T2.3)`

### T2.4. admin_today_cb edges (2070-2093, ~23 строки) — ~15 мин

**Файл:** `bot/handlers/admin.py:2065-2095` (базовый happy уже может быть покрыт — проверить!)

**Что покрывать (только непокрытые ветки):**
- Non-admin → callback.answer + return
- Master not found → alert + return
- message is None → skip answer (edge case)

**Сначала проверь coverage report** — может уже частично покрыто из других тестов. Не дублировать.

**Коммит:** `test(admin): cover admin_today_cb edges (T2.4)`

### T2.5. admin_services_cb + admin_service_name_msg + admin_service_duration_msg (2180-2321, ~92 строки) — ~40 мин

**Файл:** `bot/handlers/admin.py:2168-2321`

**Что покрывать:**
- admin_services_cb: non-admin, master not found, happy (existing services list), no services → hint
- admin_service_name_msg: non-admin, master not found, empty name, happy → state.set_state + answer
- admin_service_duration_msg: non-admin, master not found, bad duration (non-numeric, <= 0), happy → service created + state.clear

**Гейты:** deep-analysis (FSM state), code-reviewer (logic change — new service creation).

**Коммит:** `test(admin): cover admin_services_cb + service_name/duration FSM (T2.5)`

### T2.6. admin_move_confirm_cb edges (2690-2735, ~45 строк) — ~25 мин

**Файл:** `bot/handlers/admin.py:2632-2800`

**Что покрывать (edge cases — happy может быть уже покрыт, проверить):**
- Non-admin → callback.answer + return
- Master not found → alert + return
- State lost (no booking_id) → `❌ Данные сессии потеряны` + state.clear
- Slot already taken (race) → error message + state.clear

**Сначала coverage report** — не дублировать существующие тесты.

**Коммит:** `test(admin): cover admin_move_confirm_cb edges (T2.6)`

### T2.7. client.py edges (~30 строк) — ~20 мин

**Файл:** `bot/handlers/client.py` — проверить coverage report, взять топ непокрытых.

Скорее всего: business_not_found FK violation (293-294), slot_picker edges (754-839).

**Коммит:** `test(client): cover handler edges (T2.7)`

## Порядок работы (MY-VIBE-RULES.md)

Для **каждой** задачи (T2.1-T2.7):
1. **Проверь coverage report** — `uv run pytest --cov=bot.handlers.admin --cov-report=term-missing tests/test_admin_handlers.py 2>&1 | grep "admin.py"` — не дублируй уже покрытое.
2. **Deep-analysis Pass 1-2** (risk: logic — FSM state-переходы; skip для trivial edge cases)
3. **Реализация** — реальные тесты, не псевдокод. Один блок = один коммит.
4. **Verify** — `uv run pytest tests/test_admin_handlers.py -x && uv run ruff check .`
5. **Code-review** через `task(subagent_type="code-reviewer")` — только для logic change (FSM state change, new behavior). Skip для trivial edge cases (non-admin silent return).
6. **Коммит** — свободный (личный репо, MY-VIBE-RULES:73).
7. **Push** после каждого 2-3 коммита: `git push origin main`.
8. **Деплой на VPS НЕ НУЖЕН** — это только тесты, runtime код не трогаем.

**Один коммит на одну задачу** (T2.1, T2.2, ... — отдельные коммиты).

## Что НЕ делать

- ❌ Не трогать `bot/handlers/admin.py`, `bot/keyboards/admin.py`, `bot/handlers/client.py` — только тесты!
- ❌ Не чинить 2 pre-existing mypy errors (`open_workday`, `AdminStates` attr-defined) — не наши.
- ❌ Не трогать `scheduler.py` (покрыт 98%, NEXT_SESSION_PROMPT 5.60 запрет).
- ❌ Не деплоить на VPS — тесты не влияют на runtime.
- ❌ Не запускать code-reviewer для trivial edge cases (non-admin return) — только для logic change (FSM state, new service creation).
- ❌ Не коммитить IP-адреса / креды VPS (pre-push hook: IP regex).

## Телеметрия после каждой задачи

После каждого коммита — записывай в PLANS.md (создай если нет):

```
## T2.N — admin_X (commit_hash)
- Покрыто веток: N
- Coverage admin.py: X% → Y% (miss: A → B)
- Tests: 601 → 6XX
- Время: ~XX мин
```

## Финальный отчёт сессии

В конце — резюме:
- TOTAL coverage: 80% → X% (цель ~88%)
- Коммитов: N
- Тестов добавлено: N
- Что НЕ сделано (если что-то пропустил) — конкретные file:line

## Деплой и live-тесты — НЕ для этой сессии

Деплой 5.60+5.61 уже сделан в прошлой сессии (`b3fdcf6` на проде). Live-тесты фичи 5.61 (week nav) — вручную в Telegram с Ekaterina, не программная задача.

## Backup live-тест (опционально)

Если есть время в конце — проверить backup (по расписанию должен был случиться):
```bash
CRED=~/.config/opencode/references/barber-bot-deploy-credentials.md
VPS_HOST=$(grep -E '^HOST:' $CRED | sed 's/^HOST: //')
BARBER_PASS=$(grep -E '^PASS:' $CRED | sed 's/^PASS: //')
sshpass -p "$BARBER_PASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
  -o PreferredAuthentications=password -o PubkeyAuthentication=no \
  root@$VPS_HOST 'cat /var/log/barber_backup.log && ls -la /opt/barber-bot/backups/' 2>&1 | tail -30
```
Ожидание: лог с "OK: ... (verified TOC)" + barber_2026-09-13_*.dump на VPS.
