# NEXT_SESSION_PROMPT — barber-bot, session 5.60

## Контекст

Продолжаем барбер-бот. Сессия 5.59 закрыта: 3 live-теста (2 из 3),
code-review 5.58 → W1-W3 фиксы.

**main:** `6a77b09` (5.59: 3 фикса по code-review 5.58)
**VPS:** `d2734ef` на проде (runtime) — 5.58/5.59 только тесты, деплой не нужен
**Гейты:** ruff ✅ · mypy ✅ · pytest 562 passed / 2 skipped

## Что закрыто в 5.59

1. ✅ **Variant B** (7 admin-кнопок) — live подтверждён (Ekaterina видела)
2. ✅ **«Нет свободных дат»** — корректное поведение (слотов на 12-13.09 нет в БД)
3. ✅ **Code-review 5.58** → 1 Warning (false-green docstring) + 2 Suggestions
   (spec_set, freeze_time) → все 3 фикса в `6a77b09`
4. ✅ **NEW-reminders live-тест** — НЕ состоялся (все брони на 12-13.09 отменены
   админом через /openweek в 19:37-20:10 MSK). Нужен новый слот + запись.
5. ⏳ **Backup live-тест** — завтра 12.09 06:30 MSK. Проверить:
   ```bash
   CRED=~/.config/opencode/references/barber-bot-deploy-credentials.md
   VPS_HOST=$(grep -E '^HOST:' $CRED | sed 's/^HOST: //')
   BARBER_PASS=$(grep -E '^PASS:' $CRED | sed 's/^PASS: //')
   sshpass -p "$BARBER_PASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
     -o PreferredAuthentications=password -o PubkeyAuthentication=no \
     root@$VPS_HOST 'cat /var/log/barber_backup.log && ls -la /opt/barber-bot/backups/'
   ls -la ~/barber-bot-backups/  # Mac offsite через launchd
   ```
   Ожидание: лог с "OK: ... (verified TOC)" + barber_2026-09-12_*.dump на VPS и Mac.

## Задача 5.60: 3 UX/логические правки админ-флоу слотов

**Причина:** при live-проверке variant B (Ekaterina открыла неделю через
админ-меню) выявлены 3 UX/логических несоответствия. Не runtime-баги, а
логика/UX в admin-флоу /openweek.

### P1 (логический баг) — неправильный заголовок alert при закрытом дне

**Файл:** `bot/handlers/admin.py:3243-3266` (`admin_openweek_confirm_cb`)

**Проблема:** когда админ пытается открыть неделю, а в ней есть **закрытые**
дни (`is_active=False`), код показывает:
```
⚠️ Уже есть окно:
• Сб 12.09 10:30–19:30 (закрыт)
• Вс 13.09 09:00–18:00 (закрыт)

Перезаписать окно на 12:00–19:30?
```
Это семантическая ошибка. «Перезаписать» = заменить одно окно другим.
Но закрытый день — это не «окно, которое заменяют», а **день, который
открывают заново**. Закрыт → действие = re-open.

**Решение:** дифференцировать alert_text по статусу дня:
- Все дни **закрыты** → «День закрыт. Открыть заново на {window}?»
- Все дни **активны** → «Уже есть окно на {window}. Перезаписать?»
- **Смешанный** (некоторые закрыты, некоторые активны) → 2 строки в alert:
  «Открыть заново: ... Перезаписать: ...»
  Или более простой вариант: объединить в одно «Открыть / перезаписать окно?»

**Место правки:**
- `bot/handlers/admin.py:3236-3247` — цикл по existing days, формирует
  `existing_lines` с суффиксом `(закрыт)`. Добавить флаг `all_closed` /
  `all_active` / `mixed`, формировать заголовок в зависимости.
- `bot/keyboards/admin.py:596-613` (`admin_openweek_overwrite_keyboard`) —
  текст кнопки [✅ Да, перезаписать] → при re-open «✅ Да, открыть».

**Deep-analysis (Pass 1-4) обязательна** — логика ветвлений, 3 состояния
(все закрыты / все активны / смешанный), FSM state не трогать (state
preserved на alert path, see :3244).

### P2 (UX) — прошедшие дни недели показаны как тапабельные

**Файл:** `bot/keyboards/admin.py:568-593` (`admin_week_days_keyboard`)

**Проблема:** клавиатура показывает **все 7 дней** (Пн-Вс) без учёта
прошедших. Если сегодня пятница 11.09, то Пн(7.09), Вт(8.09), Ср(9.09),
Чт(10.09) — прошедшие. Админ может тапнуть любой, получить ✅, нажать
«Открыть» — и в summary получит «❌ Пн 07.09: прошедшая дата».

Код уже фильтрует прошедшие в `_apply_openweek:3079-3081`, но UX — кнопка
была тапабельна, а на самом деле ничего не делает. Это вводит в заблуждение.

**Решение:** передать в `admin_week_days_keyboard` множество прошедших
дней недели. Для прошедших дней:
- **Вариант A (проще):** добавить суффикс `❌` к label, callback_data
  остаётся (тап → toggle, потом в apply отфильтруется). Минимальная
  правка, админ видит что день прошедший.
- **Вариант B (лучше UX):** прошедшие дни — без callback_data (или
  callback → `callback.answer("День прошёл", show_alert=True)`). Кнопка
  визуально disabled (нет ✅ prefix, серый `—` вместо `✅`).

Рекомендация: **Вариант A** (минимальная правка, не ломает toggle-логику).
Вариант B — если есть время.

**Место правки:**
- `bot/keyboards/admin.py:568-593` — добавить параметр
  `past_weekdays: set[int] = frozenset()`, для прошедших суффикс `❌`.
- `bot/handlers/admin.py:2988, 3036` — при вызове передать
  `past_weekdays=_past_weekdays_for_week(monday, business_tz)`.
- Добавить helper `_past_weekdays_for_week(monday, tz) -> set[int]` в
  handlers/admin.py (или keyboards/admin.py) — для каждого weekday 0-6
  проверить `monday + timedelta(days=weekday) < today_local`.

### P3 (UX, сложнее) — «Ближайшие записи» как осадок в чате

**Файл:** `bot/handlers/admin.py:2095-2121` (`admin_week_cb`)

**Проблема:** когда админ жмёт «неделя» в inline-меню → бот шлёт
`await callback.message.answer(...)` — **отдельное сообщение в чат**.
После этого админ жмёт «Открыть неделю» → flow работает в **другом**
сообщении (edit_text шагов 1-2-3). Старое «Ближайшие записи» остаётся
висеть в чате как осадок. Во время всего /openweek flow под клавиатурой
шага 2-3 видно старое сообщение с записями.

**Решение (нужен выбор):**
- **Вариант A (минимальный):** в `admin_week_cb` при `callback.message`
  использовать `edit_text` (если message редактируемо) вместо `answer`.
  Так при следующем /openweek edit_text того же сообщения заменяет
  «Ближайшие записи» на первый шаг flow. Но: если message старше 48ч
  или удалено — TelegramBadRequest → fallback на answer.
  **Проблема:** `admin_week_cb` может вызываться из любого сообщения меню
  (в т.ч. из сообщения flow), и edit_text на сообщение flow сломает flow.
  Нужно проверять: если message в FSM state (opening_week) → НЕ edit,
  а ответ в новом сообщении.

- **Вариант B (лучший UX):** убрать «Ближайшие записи» из отдельного
  сообщения, встроить в **inline-меню** как expand/collapse секцию.
  Кнопка «неделя» → edit_text того же сообщения меню (где висит меню)
  → показать список + меню. Это редизайн inline-меню, сложнее.

- **Вариант C (компромисс):** в `/openweek` flow на шаге 1 — `delete`
  (удалить) предыдущее сообщение «Ближайшие записи» (если оно существует
  в чате и было отправлено bot'ом). Но: `delete` не работает на сообщениях
  старше 48ч. Нужно проверить возраст.

Рекомендация: **Вариант A** (минимальный, не ломает существующее).
Вариант B — отдельная сессия (редизайн inline-меню).

**Deep-analysis (Pass 1-4) обязательна** для P3 — 3 варианта, FSM state,
Telegram API ограничения (edit >48h, delete >48h), race с другими callbacks.

## Порядок работы (MY-VIBE-RULES.md)

Для **каждой** правки (P1, P2, P3):
1. **Deep-analysis Pass 1-4** (risk-class: logic — ветвления, 3 состояния)
   - Pass 1: риск-класс (logic, не high-stakes — не migration/security)
   - Pass 2: edge cases (пустые дни, все 7 дней, один день, прошедшие)
   - Pass 3: state-переходы (FSM state preserved на alert path)
   - Pass 4: self-verify (pytest + ruff + mypy)
2. **Реализация** — реальный код, не псевдокод
3. **Verify** — `uv run pytest tests/test_admin_handlers.py -x` + ruff + mypy
4. **Code-review** через `code-reviewer` subagent (logic change — обязателен)
5. **Коммит** — свободный (личный репо, MY-VIBE-RULES:73)
6. **Деплой на VPS** — только если правка влияет на runtime (P1/P2/P3
   ВСЕ влияют на runtime — это не тесты). Деплой:
   ```bash
   CRED=~/.config/opencode/references/barber-bot-deploy-credentials.md
   VPS_HOST=$(grep -E '^HOST:' $CRED | sed 's/^HOST: //')
   BARBER_PASS=$(grep -E '^PASS:' $CRED | sed 's/^PASS: //')
   sshpass -p "$BARBER_PASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
     -o PreferredAuthentications=password -o PubkeyAuthentication=no \
     root@$VPS_HOST 'cd /opt/barber-bot && git pull && docker compose up -d --build && \
     docker logs --tail 20 barber-bot-bot-1' 2>&1 | tail -30
   ```

**Один коммит на одну правку** (P1, P2, P3 — 3 отдельных коммита).
Не объединять — иначе code-review будет путать логику.

## Что НЕ делать

- ❌ Не трогать `scheduler.py` (покрыт 98%, boundary-тест 5.59)
- ❌ Не менять FSM state machine (AdminStates) — только тексты/клавиатуры
- ❌ Не менять `open_workday` / `select_workday` service layer
- ❌ Не коммитить IP-адреса / креды VPS (pre-push hook: IP regex)
- ❌ Не менять `admin_inline_menu` (7 кнопок) — только тексты alert
- ❌ Не делать `delete` на сообщениях старше 48ч (Telegram API)
- ❌ Не использовать `callback.message.delete()` без проверки возраста

## Тесты

Для P1 и P2 — добавить юнит-тесты в `tests/test_admin_handlers.py`:
- P1: тест на 3 состояния (все закрыты → «открыть заново», все активны →
  «перезаписать», смешанный → «открыть/перезаписать»)
- P2: тест что `admin_week_days_keyboard` с `past_weekdays={0,1,2,3}`
  показывает суффикс `❌` на Пн-Чт, callback_data для прошедших
  (вариант A) или его отсутствие (вариант B)
- P3: тест что `admin_week_cb` при редактируемом message делает edit_text,
  при TelegramBadRequest → answer

## Данные для live-тестов

### БД после 5.59 (11.09 ~21:00 MSK)

- **Слотов в будущем: 0** (min_date=28.08, max_date=29.08)
- **Броней в будущем (confirmed/transferred): 0** (все 5 отменены)
- **47bf8c59** (сегодня 15:30 MSK, «Окрашивание и стрижка», client_name=«Андрей») — confirmed, в прошлом
- **Мастер:** Ekaterina (1 шт, active)
- **apscheduler_jobs: 0**

### Как создать слот на 12.09 для live-теста NEW-reminders

После деплоя 5.60 на VPS:
1. Ekaterina: `/openweek` → окно 10:00-19:00 → Сб 12.09 → Открыть
2. Оlesya (client tg=1156374642): `/book` → 12.09 → 13:00 → «Окрашивание» → confirm
3. remind_1h в 12:00 MSK 12.09 → NEW-формат
4. Проверить: notifications_log + docker logs grep send_reminder

## Открытые вопросы к пользователю

1. **P3:** какой вариант (A/B/C)? Рекомендация A.
2. **P2:** вариант A (суффикс `❌`, callback остаётся) или B (callback → alert)?
3. После 5.60 — coverage gaps (admin.py 65%, client.py 79%) или APScheduler
   orphan cleanup?
