# Фаза 3: Проверка баланса перед рассылкой

## Контекст
Перед "Массовой рассылкой" проверяем что у пользователя хватает постов. Перед "Тестом" проверяем что работают группы, но посты не тратятся. В меню подтверждения показываем сколько будет потрачено.

---

## Архитектура

### Где искать:
- `bot/main.py` — handlers `broadcast_test_v2()` (строка ~2595) и `broadcast_mass()` (строка ~2703)
- `bot/broadcast_manager.py` — метод `get_balance()`

---

## 🎨 Визуальный дизайн

### 1. Меню подтверждения МАССОВОЙ рассылки

```
📢 ПОДТВЕРЖДЕНИЕ РАССЫЛКИ

Группы: 10 (все активные)
Расписание: Пн-Пт 08:12

💰 ЗАТРАТЫ:
├─ Потратится: 10 постов
├─ Текущий баланс: 600 постов
└─ Останется: 590 постов

⚠️ Платите только за успешные публикации

[✅ Подтвердить] [❌ Отменить]
```

### 2. Если баланс не хватает

```
❌ НЕДОСТАТОЧНО БАЛАНСА

Требуется: 10 постов
Доступно: 5 постов

Пополните баланс!

[💳 Купить тариф] [◀️ Назад]
```

### 3. Во время теста

```
🧪 ТЕСТИРОВАНИЕ ГРУПП
⏳ Ожидайте 60 секунд...

Не закрывайте чат!
```

### 4. После теста (результаты)

```
✅ ТЕСТ ЗАВЕРШЁН

✅ Работают: 8 групп
❌ Не работают: 2 группы
  - @blocked_group (забан)
  - @deleted_group (удалено)

✅ Баланс не потрачен (это был тест)
💰 Баланс: 600 постов (без изменений)

Готовы к массовой рассылке!

[🔄 Тест ещё раз] [📢 Массовая] [◀️ Назад]
```

---

## 💻 Бэкэнд логика

### 1. Обновить broadcast_mass() handler

**Главная логика:**

```python
@dp.callback_query(F.data == "bc_mass")
async def broadcast_mass(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    state = bm.load()
    groups_all = scoped_load_broadcast_groups(user_id)
    
    # Проверить prerequisite steps
    ready, reason = is_campaign_ready(state, user_id=user_id, groups=groups_all)
    if not ready:
        await query.answer(f"❌ {reason}", show_alert=True)
        return
    
    # Получить активные группы
    active_groups = get_active_selected_groups_from(state, groups_all)
    if not active_groups:
        await query.answer("❌ Нет активных групп.", show_alert=True)
        return
    
    # НОВОЕ - Проверить баланс
    balance = bm.get_balance()
    posts_needed = len(active_groups)
    
    if balance < posts_needed:
        # Баланса не хватает
        text = f"""❌ НЕДОСТАТОЧНО БАЛАНСА

Требуется: {posts_needed} постов
Доступно: {balance} постов

Пополните баланс!"""
        
        rows = [
            [InlineKeyboardButton(text="💳 Купить тариф", callback_data="bc_tariffs")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="bc_main")],
        ]
        
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await query.answer()
        return
    
    # Показать подтверждение с информацией о потратах
    campaign = state.get("campaign", {})
    posts = campaign.get("posts", [])
    current_post = posts[campaign.get("rotation_index", 0)] if posts else {}
    preview = current_post.get("preview", "")[:50]
    
    text = f"""📢 ПОДТВЕРЖДЕНИЕ РАССЫЛКИ

Группы: {len(active_groups)} (все активные)

💰 ЗАТРАТЫ:
├─ Потратится: {posts_needed} постов
├─ Текущий баланс: {balance} постов
└─ Останется: {balance - posts_needed} постов

⚠️ Платите только за успешные публикации"""
    
    rows = [
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="bc_mass_confirm")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="bc_main")],
    ]
    
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await query.answer()
```

### 2. Обновить broadcast_test_v2() handler

**Главное: НЕ ТРАТИТЬ посты при тесте**

```python
@dp.callback_query(F.data == "bc_test")
async def broadcast_test_v2(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    # ... существующая логика проверки steps ...
    
    test_groups = get_active_selected_groups_from(state, groups_all)
    
    await query.message.edit_text(
        "🧪 ТЕСТИРОВАНИЕ ГРУПП\n\n"
        "⏳ Ожидайте 60 секунд...\n"
        "Не закрывайте чат!"
    )
    
    # Передать флаг что это тест
    result = await execute_broadcast(
        user_id,
        test_groups,
        advance_rotation=False,  # Не двигаем ротацию
        is_test=True  # НОВОЕ: флаг теста
    )
    
    # ВАЖНО: НЕ вычитаем посты!
    # bm.subtract_balance() НЕ вызываем
    
    # Показать результаты (как было)
    # ...
```

---

## 📝 Файлы для изменения

1. `bot/main.py` — обновить `broadcast_mass()` и `broadcast_test_v2()`
2. `parser/broadcast_sender.py` — добавить параметр `is_test` в `execute_broadcast()`

---

## 📋 Предусловия для Фазы 3

- ✅ Фаза 1 закончена
- ✅ Фаза 2 закончена

---

## 🎯 Результат

После этой фазы:
- ✅ Рассылка проверяет баланс перед стартом
- ✅ Тест НЕ тратит посты
- ✅ Пользователь видит сколько постов будет потрачено
- ✅ Если баланса нет → показывается кнопка для покупки
