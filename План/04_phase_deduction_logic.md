# Фаза 4: Списание постов после успешной рассылки

## Контекст
После завершения рассылки проверяем сколько сообщений реально дошло (sent_count) и списываем только это количество постов. История траты записывается в JSON.

---

## Архитектура

### Где искать:
- `bot/main.py` — handler `broadcast_mass_confirm()` (новый)
- `bot/broadcast_manager.py` — метод `subtract_balance()`
- `parser/broadcast_sender.py` — результат содержит `sent_count`

---

## 🎨 Визуальный дизайн

### 1. Во время рассылки (live status)

```
📢 РАССЫЛКА В ПРОЦЕССЕ

Всего групп: 10
├─ ✅ Отправлено: 7
├─ ⏳ В процессе: 2
├─ ❌ Ошибка: 1 (забан)

⏳ Обработана: 10 из 10 групп...
```

### 2. После завершения (итоги)

```
✅ РАССЫЛКА ЗАВЕРШЕНА

📊 СТАТИСТИКА:
├─ Всего групп: 10
├─ ✅ Успешно: 8
├─ ⚠️ Забаны: 2

💳 ЗАТРАТЫ:
├─ Потрачено: 8 постов (за успешные)
├─ Баланс было: 600 постов
└─ Баланс сейчас: 592 постов

✅ Платите только за успешные!

[◀️ К меню]
```

---

## 💻 Бэкэнд логика

### 1. Новый handler broadcast_mass_confirm()

```python
@dp.callback_query(F.data == "bc_mass_confirm")
async def broadcast_mass_confirm(query: CallbackQuery):
    """Выполнить массовую рассылку и списать посты"""
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    groups_all = scoped_load_broadcast_groups(user_id)
    
    state = bm.load()
    active_groups = get_active_selected_groups_from(state, groups_all)
    
    balance_before = bm.get_balance()
    
    await query.message.edit_text(
        "📢 РАССЫЛКА В ПРОЦЕССЕ\n\n"
        f"Всего групп: {len(active_groups)}\n"
        "⏳ Отправляю сообщения...\n"
        "Не закрывайте чат!"
    )
    
    async with broadcast_lock:
        result = await execute_broadcast(
            user_id,
            active_groups,
            advance_rotation=True
        )
    
    # ВАЖНО: Списать только успешные посты
    sent_count = result.get("sent_count", 0)
    blocked_count = len(result.get("blocked_groups", {}))
    failed_count = len(result.get("failed_groups", {}))
    
    if sent_count > 0:
        bm.subtract_balance(sent_count)
    
    balance_after = bm.get_balance()
    
    # Построить отчёт
    text = f"""✅ РАССЫЛКА ЗАВЕРШЕНА

📊 СТАТИСТИКА:
├─ Всего групп: {len(active_groups)}
├─ ✅ Успешно: {sent_count}
├─ ⚠️ Забаны: {blocked_count}
└─ ❌ Другие ошибки: {failed_count}

💳 ЗАТРАТЫ:
├─ Потрачено: {sent_count} постов (за успешные)
├─ Баланс было: {balance_before} постов
└─ Баланс сейчас: {balance_after} постов

✅ Платите только за успешные!
"""
    
    # Добавить детали ошибок если есть
    if blocked_count > 0:
        blocked_groups = list(result.get("blocked_groups", {}).keys())[:5]
        text += f"\n🚫 ЗАБАНЫ ({blocked_count}):\n"
        for group in blocked_groups:
            text += f"   • {group}\n"
    
    rows = [[InlineKeyboardButton(text="◀️ К меню", callback_data="bc_main")]]
    
    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await query.answer()
```

---

## 📝 Файлы для изменения

1. `bot/main.py` — добавить handler `broadcast_mass_confirm()`
2. `bot/broadcast_manager.py` — метод `subtract_balance()` уже есть из Фазы 1

---

## 📋 Предусловия для Фазы 4

- ✅ Фаза 3 закончена (проверка баланса работает)

---

## 🎯 Результат

После этой фазы:
- ✅ Посты списываются только за успешные публикации
- ✅ История трат ведется в JSON
- ✅ Пользователь видит детальный отчёт (сколько потрачено)
- ✅ Если 7 из 10 групп → потратил 7 постов
