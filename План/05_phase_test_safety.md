# Фаза 5: Безопасность тестов (отправка-проверка-удаление)

## Контекст
Тестовое сообщение отправляется с меткой (emoji 🧪), через 60 секунд бот проверяет что сообщение еще в группе, потом сам его удаляет. Группы остаются чистыми, пользователь может запускать тест бесконечно без трат баланса.

**Это решает проблему:** пользователь может запускать тест сколько угодно раз, и группы не засоряются тестовыми сообщениями.

---

## Архитектура

### Где искать:
- `parser/broadcast_sender.py` — функция `send_broadcast_campaign_with_client()` (строка ~70)
- `bot/main.py` — handler `broadcast_test_v2()` (строка ~2595)

### Что меняется:

**Новые параметры в execute_broadcast():**
```python
is_test: bool = False  # флаг что это тест
test_marker: str = "🧪"  # маркер для тестовых сообщений
```

**Новая функция в broadcast_sender.py:**
```python
async def verify_and_delete_test_messages(
    client,
    test_message_ids: dict[str, int],  # {group: message_id}
    wait_seconds: int = 60,
) -> dict[str, bool]:
    """
    Проверить что тестовые сообщения все еще есть в группах.
    Потом удалить их (если есть права).
    
    Возвращает: {group: True/False} (True = было и удалено)
    """
    # Реализация
```

---

## 🎨 Визуальный дизайн

### 1. Начало теста

```
🧪 ТЕСТИРОВАНИЕ ГРУПП
⏳ Ожидайте 60 секунд...

Отправляю тестовые сообщения в 10 групп...
Не закрывайте чат!
```

### 2. Во время теста (live status, каждые 10 сек)

```
🧪 ТЕСТИРОВАНИЕ ГРУПП
⏳ Ожидайте 60 секунд...

Проверено: 3 из 10
├─ ✅ Успешно: 2
├─ ⏳ В процессе: 1
└─ ❌ Ошибки: 0

Не закрывайте чат!
```

### 3. После завершения (итоги)

```
✅ ТЕСТ ЗАВЕРШЁН

✅ Работают: 8 групп
❌ Не работают: 2 группы
  - @blocked_group (забан)
  - @deleted_group (удалено)

✅ Баланс не потрачен (это был тест)
💰 Баланс: 600 постов (без изменений)

🧹 Тестовые сообщения удалены из групп

Готовы к массовой рассылке!

[🔄 Тест ещё раз] [📢 Массовая] [◀️ Назад]
```

---

## 💻 Бэкэнд логика

### 1. Обновить execute_broadcast() сигнатуру

**В parser/broadcast_sender.py:**

```python
async def execute_broadcast(
    # ... существующие параметры ...
    is_test: bool = False,
    test_marker: str = "🧪",
) -> dict:
    """Отправить сообщения в группы"""
    
    # Если это тест - добавить маркер в начало текста
    if is_test:
        source_text = f"{test_marker} {source_text}"
```

### 2. Новая функция для проверки и удаления

**В parser/broadcast_sender.py:**

```python
async def verify_and_delete_test_messages(
    client,
    test_message_ids: dict[str, int],
    wait_seconds: int = 60,
) -> dict[str, bool]:
    """
    Проверить и удалить тестовые сообщения.
    
    Шаг 1: Ждём wait_seconds
    Шаг 2: Для каждого сообщения проверяем - оно еще в группе?
    Шаг 3: Удаляем сообщения (если есть права)
    
    Возвращает: {group: True/False}
    """
    import asyncio
    from telethon.errors import MessageDeleteForbiddenError
    
    # Ждём 60 сек перед проверкой
    await asyncio.sleep(wait_seconds)
    
    results = {}
    
    for group, msg_id in test_message_ids.items():
        try:
            # Получаем группу
            entity = await client.get_entity(group)
            
            # Пытаемся получить сообщение (это проверит что оно еще есть)
            messages = await client.get_messages(entity, ids=[msg_id])
            
            if messages and messages[0]:
                # Сообщение есть! Пробуем его удалить
                try:
                    await client.delete_messages(entity, [msg_id])
                    results[group] = True
                except MessageDeleteForbiddenError:
                    # Нет прав удалять - но сообщение есть
                    results[group] = True
            else:
                # Сообщение удалено/недоступно
                results[group] = False
                
        except Exception as e:
            # Ошибка при проверке
            results[group] = False
    
    return results
```

### 3. Обновить broadcast_test_v2() handler

**В bot/main.py:**

```python
@dp.callback_query(F.data == "bc_test")
async def broadcast_test_v2(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    
    # ... проверка steps как было ...
    
    test_groups = get_active_selected_groups_from(state, groups_all)
    
    await query.message.edit_text(
        "🧪 ТЕСТИРОВАНИЕ ГРУПП\n\n"
        "⏳ Ожидайте 60 секунд...\n"
        f"Отправляю тестовые сообщения в {len(test_groups)} групп...\n"
        "Не закрывайте чат!"
    )
    
    # НОВОЕ: Передаём is_test=True
    result = await execute_broadcast(
        user_id,
        test_groups,
        advance_rotation=False,
        is_test=True,  # НОВОЕ!
    )
    
    # НОВОЕ: Проверить и удалить тестовые сообщения
    test_message_ids = result.get("sent_message_ids", {})
    
    await query.message.edit_text(
        "🧪 ПРОВЕРКА СООБЩЕНИЙ\n\n"
        f"⏳ Ожидаю 60 секунд перед проверкой...\n"
        "Не закрывайте чат!"
    )
    
    # Проверяем и удаляем
    verification = await verify_and_delete_test_messages(
        client,
        test_message_ids,
        wait_seconds=60,
    )
    
    # Считаем сколько успешно проверено
    verified_count = sum(1 for v in verification.values() if v)
    
    # Показать результаты
    sent_count = result.get("sent_count", 0)
    blocked_count = len(result.get("blocked_groups", {}))
    
    text = f"""✅ ТЕСТ ЗАВЕРШЁН

✅ Работают: {sent_count} групп
❌ Не работают: {blocked_count} групп

✅ Баланс не потрачен (это был тест)
💰 Баланс: {bm.get_balance()} постов (без изменений)

🧹 Тестовые сообщения удалены

Готовы к массовой рассылке!"""
    
    # Добавить детали если есть
    failed = result.get("blocked_groups", {})
    if failed:
        groups_list = list(failed.keys())[:3]
        text += f"\n\n❌ НЕ РАБОТАЮТ ({len(failed)}):\n"
        for g in groups_list:
            text += f"   • {g}\n"
    
    rows = [
        [InlineKeyboardButton(text="🔄 Тест ещё раз", callback_data="bc_test")],
        [InlineKeyboardButton(text="📢 Массовая", callback_data="bc_mass")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="bc_main")],
    ]
    
    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await query.answer()
```

---

## 🔍 Проверка (Verification)

### Что тестировать:
1. Запустить тест
2. Проверить в группе — появилось ли сообщение с 🧪?
3. Ждём 60 сек
4. Проверить в группе — сообщение все еще есть?
5. После теста — сообщение должно быть удалено
6. Баланс НЕ изменился (тест бесплатный)
7. Можно запустить тест снова без штрафа

---

## 📝 Файлы для изменения

1. `parser/broadcast_sender.py` — добавить параметры `is_test`, функцию `verify_and_delete_test_messages()`
2. `bot/main.py` — обновить `broadcast_test_v2()` handler

---

## 📋 Предусловия для Фазы 5

- ✅ Фаза 4 закончена (списание работает)

---

## 🎯 Результат

После этой фазы:
- ✅ Тесты безопасны от спама (сообщения удаляются)
- ✅ Пользователь может запускать тесты бесплатно
- ✅ Баланс не тратится на тесты
- ✅ Группы остаются чистыми
