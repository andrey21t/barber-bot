# NEXT_SESSION_PROMPT — barber-bot, admin UX refactor (5 пунктов от Екатерины)

## Контекст

Екатерина потестировала 5.61 (week nav) и дала 5 пунктов UX-улучшений.
Продолжаем работу в `~/PycharmProjects/barber-bot/` (личный pet-проект, коммитить свободно, AGENTS.md § git-repo-categories).

**Репо:** `~/PycharmProjects/barber-bot/`
**Стек:** Python 3.12, aiogram 3.x, SQLAlchemy 2.0 async, SQLite (dev), APScheduler, freezegun.
**Правила:** `MY-VIBE-RULES.md` — dev-режим (код сразу, без педагогики), гейты: deep-analysis на нетривиальное → реализация → verify → code-review.
**Деплой:** VPS через ssh+git pull+systemctl restart (детали в `~/.config/opencode/references/barber-bot-deploy-credentials.md`).

## Проверь себя (ОБЯЗАТЕЛЬНО в начале сессии)

1. `cd ~/PycharmProjects/barber-bot && git log --oneline -5` — последний коммит `a642e41`.
2. `uv run pytest --cov=bot --cov-report=term 2>&1 | tail -5` — TOTAL 89%, admin.py 81%, client.py 90%.
3. `uv run ruff check .` — All checks passed.
4. `uv run mypy bot tests 2>&1 | grep -c error:` — 17 (pre-existing, НЕ наши).

Если что-то не так — откатись к `a642e41` (`git reset --hard a642e41`), рапорт INCOMPLETE.

## Что сделано в прошлой сессии (coverage Tier 2+3+4)

- **T2.1-T2.7** (admin.py + client.py edges, 7 коммитов): admin.py 66%→81%, client.py 79%→90%
- **T3.1-T3.3** (config.py + keyboards/admin.py + main.py, 1 коммит `51ddc01`): config.py 100%, keyboards/admin.py 100%, main.py 97%
- **T4.1-T4.2** (book_date_cb + transfer_slot_30_cb error mappings, 1 коммит `a642e41`): +15 тестов
- **Итог:** TOTAL 80%→89% (−389 stmts miss), 601→694 тестов (+93)

## Задачи сессии: 5 UX-улучшений от Екатерины

ЕКАТЕРИНА ПРОСИЛА. Порядок её запросов (по приоритету):

### Пункт 1: Навигация по неделям на Шаге 1 (не на Шаге 3)

**Сейчас:** `/openweek` flow: Шаг 1 (выбор start_time) → Шаг 2 (выбор end_time) → Шаг 3 (выбор дней недели с навигацией ← Пред. / След. →). Навигация по неделям только на шаге 3.

**Хочет:** Навигацию по неделям на шаге 1 (до выбора времени окна). Логика — сначала выбрал неделю, потом время, потом дни.

**Файлы:**
- `bot/handlers/admin.py:2882-2922` — `admin_openweek_entry_cb` (Шаг 1, показывает `admin_window_slot_picker_keyboard` mode="start")
- `bot/handlers/admin.py:2925-3063` — `admin_openweek_start_cb` → `admin_openweek_end_cb` (Шаг 2 → Шаг 3, тут `admin_week_days_keyboard` с навигацией)
- `bot/keyboards/admin.py:588-` — `admin_week_days_keyboard` (кнопки ← Пред. / След. → через `AdminOpenWeekNavCallbackData`)
- `bot/states.py:65-67` — `opening_week_start`, `opening_week_end`, `opening_week_days`

**Архитектурное решение (обдумать):** Сейчас week_offset хранится в FSM state и читается на шаге 3. Чтобы дать навигацию на шаге 1 — нужно добавить nav-кнопки в `admin_window_slot_picker_keyboard` ИЛИ показывать отдельную week-picker клавиатуру ПЕРЕД slot picker. Второй вариант чище: новый шаг 0 "выбор недели" → шаг 1 (start_time) → шаг 2 (end_time) → шаг 3 (дни).

### Пункт 2: Убрать кнопку "Открыть день" (старый текстовый формат)

**Сейчас:** Кнопка "📅 Открыть день" (`admin_inline_menu` row 1) → `AdminOpendayCallbackData` → `admin_openday_cb` → `SimpleCalendar` → текстовый ввод `ЧЧ:ММ` для start_time и end_time. Старый UX с ручным вводом времени.

**Хочет:** Убрать кнопку "Открыть день" совсем. Оставить одну кнопку "Открыть запись" (переименовать "Открыть неделю").

**Файлы:**
- `bot/keyboards/admin.py:139` — кнопка "📅 Открыть день" (`AdminOpendayCallbackData`)
- `bot/handlers/admin.py:1121-1147` — `admin_openday_cb` (entry point)
- `bot/handlers/admin.py:1150-1245` — `admin_openday_calendar_cb` + `admin_openday_start_msg` + `admin_openday_end_msg` (весь текстовый flow)
- `bot/states.py:58-60` — `opening_workday_date`, `opening_workday_start`, `opening_workday_end`

**Что сохранить:** `cmd_openday` (текстовая команда `/openday 2026-09-18 11:00 18:00`) — оставляем как power-user shortcut, не трогаем. Удаляем только inline-кнопку + `admin_openday_cb` + FSM flow (calendar + text message handlers). Тесты на эти handlers — удалить или адаптировать.

### Пункт 3: Убрать кнопку "Закрыть день"

**Сейчас:** Кнопка "📅 Закрыть день" (`admin_inline_menu` row 3) → `AdminCloseDayEntryCallbackData` → отдельный flow.

**Хочет:** Убрать кнопку "Закрыть день". Закрытие дня сделать внутри flow "Открыть запись" (когда день уже открыт — показывать опцию "закрыть" рядом с "изменить окно").

**Файлы:**
- `bot/keyboards/admin.py:144` — кнопка "📅 Закрыть день"
- `bot/handlers/admin.py` — `cmd_closeday` + `admin_closeday_*` handlers (найти через grep `closeday\|CloseDay`)
- `bot/states.py` — closing_day states (если есть)

**Архитектура:** "Закрыть день" — это `update_workday(is_active=False)`. Можно встроить в "Сегодня" view (показать кнопку "Закрыть" рядом с каждым активным днём) ИЛИ в "Изменить окно" flow. Обдумать.

### Пункт 4: Цветовое обозначение дней с записью в календаре

**Сейчас:** В `admin_week_days_keyboard` уже есть маркеры `🟡` (active WorkDay) и `⚪` (closed WorkDay) — сделаны в 5.61, но Екатерина их НЕ видит. Возможные причины:
- Не задеплоено (проверить: `git log --oneline -3` на VPS, сравнить с локальным main)
- Маркеры только в /openweek flow (шаг 3), а не в обычном календаре
- Екатерина смотрит на SimpleCalendar (cmd_openday / admin_addslots), а там нет маркеров

**Хочет:** В календаре (где выбираешь дату) дни с уже открытой записью — выделить цветом/эмодзи, чтобы было видно где уже открыто.

**Файлы:**
- `bot/keyboards/admin.py:588-` — `admin_week_days_keyboard` (маркеры есть, но только в /openweek шаг 3)
- `bot/keyboards/admin.py:150-165` — `admin_calendar_keyboard` (SimpleCalendar — НЕТ маркеров, это для /addslots и /openday)
- `bot/handlers/admin.py` — `_scheduled_closed_weekdays` helper (gather active/closed WorkDays for week)

**Проблема:** aiogram_calendar (SimpleCalendar) НЕ поддерживает кастомные маркеры на днях из коробки. Нужно либо:
- (A) Свой date-picker с маркерами (как BB-110 BookDateCallbackData в client.py — там свой picker с callback_data на каждый день)
- (B) Перед показом календаря — текстовое сообщение "Уже открыто: Пн 15:00-18:00, Ср 10:00-20:00" + потом календарь
- (C) Свой inline keyboard с днями месяца (grid 7 колонок) + эмодзи на открытых днях

### Пункт 5: Плавающее меню для админа (как у клиента)

**Сейчас:** Админ получает inline keyboard (`admin_inline_menu`) — она уезжает вверх по чату, нужно скроллить или вводить `/menu` чтобы вернуть. Клиент имеет reply keyboard (`client_reply_keyboard`) — всегда видна внизу.

**Хочет:** Чтобы админ тоже имел всегда видимое меню внизу (reply keyboard), не нужно вводить `/menu` или рандомные буквы.

**Файлы:**
- `bot/handlers/start.py:39-47` — админ-ветка cmd_start (отправляет `ReplyKeyboardRemove` + inline menu)
- `bot/handlers/start.py:48-53` — клиент-ветка (отправляет `client_reply_keyboard` — reply keyboard, всегда видна)
- `bot/keyboards/admin.py:168-184` — `admin_keyboard()` — deprecated reply keyboard (5 кнопок `/addslots /closeslot /today /week /services add`)
- `bot/keyboards/admin.py:126-147` — `admin_inline_menu()` — текущий inline menu (7 кнопок)

**Архитектурное решение (обдумать):**
- (A) Reply keyboard с одной кнопкой "📋 Меню" → тап → показывает inline menu в сообщении (максимально просто, но inline menu всё равно уедет вверх)
- (B) Reply keyboard с 2-3 главными кнопками ("Открыть запись", "Сегодня", "Меню") → "Меню" разворачивает полный inline menu. Compromise.
- (C) Полностью перейти на reply keyboard для админа (как клиент) — но тогда нельзя показать inline picker'ы (calendar, slot picker) одновременно с reply keyboard. aiogram позволяет комбинировать, но UX сложнее.

**Важно:** `admin_keyboard()` (deprecated reply keyboard) уже существует — можно адаптировать. Но её кнопки (`/addslots /closeslot`) устарели после пунктов 2-3.

## Порядок работы (MY-VIBE-RULES.md)

Для **каждой** задачи:
1. **Deep-analysis Pass 1-4** (risk: logic — FSM state-переходы, UX flow change; обязательно для п.1 и п.5, medium для п.2-4)
2. **Реализация** — код + тесты. Один блок = один коммит.
3. **Verify** — `uv run pytest tests/ -x && uv run ruff check . && uv run mypy bot tests 2>&1 | grep -c error:`
4. **Code-review** через `task(subagent_type="code-reviewer")` — обязательно (logic change, не trivial)
5. **Коммит** — свободный (личный репо). Формат: `feat(admin): <что> (пункт N)` или `refactor(admin): <что> (пункт N)`
6. **Push** после каждого коммита: `git push origin main`
7. **Деплой** на VPS после всех 5 пунктов (или после блока связанных): ssh+git pull+systemctl restart. Live-тест с Екатериной.

**Один коммит на одну задачу** (пункт 1, 2, ... — отдельные коммиты).

## Порядок приоритета (зависимости)

Пункт 2 (убрать "Открыть день") и Пункт 3 (убрать "Закрыть день") — зависят от renaming в пункте 5. Логичный порядок:

1. **Пункт 5** (reply keyboard для админа) — фундамент, меняет start.py + keyboards
2. **Пункт 2** (убрать "Открыть день") — заодно убираем кнопку из нового reply keyboard
3. **Пункт 3** (убрать "Закрыть день") — встраиваем закрытие в "Сегодня" view
4. **Пункт 1** (навигация по неделям на шаге 1) — refactor /openweek flow
5. **Пункт 4** (цветовые маркеры в календаре) — последний, требует решения по aiogram_calendar vs custom picker

## Что НЕ делать

- ❌ Не ломать `cmd_openday` (текстовая команда `/openday 2026-09-18 11:00 18:00`) — power-user shortcut, оставляем
- ❌ Не трогать клиентский flow (bot/handlers/client.py) — только admin UX
- ❌ Не чинить 17 pre-existing mypy errors (см. выше список) — не наши
- ❌ Не коммитить IP-адреса / креды VPS (pre-push hook: IP regex)
- ❌ Не деплоить промежуточные коммиты — только после блока связанных пунктов

## Телеметрия после каждой задачи

После каждого коммита — записывай в PLANS.md:
```
### Пункт N — <что> (commit <hash>, pushed)
- Что изменилось: <кратко>
- Файлов изменено: N
- Тестов: +N (новых) / -N (удалённых) / изменившихся: N
- Coverage: TOTAL X% → Y%
- Время: ~XX мин
```

## Финальный отчёт сессии

В конце — резюме:
- Какие пункты сделаны (1-5)
- Деплой: да/нет
- Live-тест: да/нет (если да — что сказала Екатерина)
- Что НЕ сделано — конкретные file:line + причина
