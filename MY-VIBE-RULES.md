# MY-VIBE-RULES.md — личный codebook по AI-генерации

> Living-документ. Кристаллизация правил пойманных на проекте barber-bot.
> Источник: скилл `vibe-coding-mentor`, методология 4 шагов, фаза "Кристаллизация".

---

## FSM (aiogram 3.x)

### FSM timeout — точка отсчёта

**Правило:** FSM timeout = время от **последнего** сообщения пользователя, НЕ от старта сессии.

**Почему:** каждый message обновляет `last_message_at` в `state.update_data()` → таймер продлевается. Это как банковская сессия — активность = продление.

**Паттерн (middleware):**
```python
state_data = await state.get_data()
last_at = state_data.get("last_message_at")
if last_at:
    elapsed = (datetime.utcnow() - last_at).total_seconds()
    if elapsed > SESSION_TTL_SEC:
        await state.clear()  # ДО event.answer (race condition)
        return await event.answer("⏰ Сессия истекла...")
await state.update_data(last_message_at=datetime.utcnow())  # продлеваем
return await handler(event, data)
```

**Грабли (где ошибся 2026-07-19):**
- Думал что timeout от **старта** сессии → неверно
- Думал что `state.clear()` "очищает счётчик для нового отчёта" → неверно, он сбрасывает FSM в idle
- Думал что `update_data` "стартует новую сессию с начала" → неверно, он продлевает текущую

**Insight:** если юзер писал "Иван" T=0, молчал 35 мин, написал "петров" T=35 →
- `elapsed = 35*60 = 2100 сек > 1800` ✅
- `state.clear()` → state = State(None), "Иван" потерян
- `event.answer("⏰ Сессия истекла")` → юзер видит сообщение
- `return` → "петров" НЕ сохраняется, handler не вызывается
- Юзер должен `/book` заново.

---

## Spec-driven development

### Структура spec.md — не пихать блоки куда попало

**Правило:** новые разделы вставлять **отдельным блоком** с `---` разделителем, не вклинивать в чужой раздел.

**Грабли (2026-07-19):** вставил 3 gap-блока (`/cancel` в FSM, FSM-таймаут, FSM-storage) прямо посреди раздела `/cancel` для клиента → `### FSM таймаут` оказался между Rate limit и Deploy, `## Gap 3` (мой текст-подсказка) попал в файл как заголовок.

**Паттерн:** все дополнения в spec → в конец файла или в новый раздел `## FSM edge cases` с `---` разделителями между блоками.
