# Фаза 1: Система баланса постов в JSON + UI отображения

## Контекст
Введение системы отслеживания баланса постов для каждого пользователя. Бесплатно 30 постов при первом подключении. Баланс хранится в JSON (в Railway), отображается в UI, проверяется перед рассылкой.

---

## Архитектура

### Где искать:
- `bot/main.py` — handlers для меню баланса
- `bot/broadcast_manager.py` — методы управления балансом
- `bot/broadcast_state.json` — структура баланса в JSON

### Что меняется в структуре данных:

**bot/broadcast_state.json** (добавить в root):
```json
{
  "balance": {
    "posts": 30,              // текущий баланс (по-умолчанию 30)
    "total_purchased": 0,      // всего куплено когда-либо
    "total_spent": 0,          // всего потрачено
    "created_at": "ISO_timestamp",  // когда создан аккаунт
    "balance_history": [       // история изменений
      {
        "type": "initial_free",
        "amount": 30,
        "date": "ISO_timestamp"
      }
    ]
  },
  // ... остальное поле broadcast_state
}
```

---

## 🎨 Визуальный дизайн

### 1. Кнопка "Баланс постов" в меню Рассылки

**Где:** В `broadcast_main_keyboard()` (строка ~505)

**Текущее:** Нет кнопки баланса

**Новое:**
```
📋 Меню рассылки
│
├─ 💰 Баланс: 600 постов
│   └─ [Купить тариф]
│
├─ 📝 Мой текст
├─ 🎯 Выбрать группы
├─ 🧪 Тест
├─ 📢 Массовая рассылка
└─ ⏰ Расписание
```

### 2. Меню "Баланс постов"

**Что показывать:**
```
💰 МОЙ БАЛАНС

🟢 Доступно: 600 постов
📊 Всего куплено: 600
📤 Всего потрачено: 0
📅 Аккаунт создан: 13 апр 2026

[Купить тариф] [История] [Назад]
```

### 3. Меню "Купить тариф"

**Визуал:**
```
🛍 ВЫБЕРИТЕ ПАКЕТ

🟢 Пробный
   80 постов / €5
   (€0.0625 за пост)
   [Купить]

🔵 Стартовый
   250 постов / €12
   Экономия: 23%
   [Купить]

🔥 Оптимальный ⭐ ПОПУЛЯРНЫЙ
   600 постов / €20
   Экономия: 47%
   [Купить]

🔴 Оптовый
   1500 постов / €40
   Экономия: 57%
   [Купить]

Оплата через Stripe | Только успешные посты
```

---

## 💻 Бэкэнд логика

### 1. broadcast_manager.py — новые методы

Добавить в класс `BroadcastManager`:

```python
def get_balance(self) -> int:
    """Вернуть текущий баланс"""
    return self.state.get("balance", {}).get("posts", 0)

def set_balance(self, posts: int):
    """Установить баланс (для Stripe webhook)"""
    old = self.get_balance()
    self.state.setdefault("balance", {})["posts"] = posts
    self.state["balance"].setdefault("balance_history", []).append({
        "type": "purchase",
        "amount": posts - old,  # дельта
        "date": datetime.now(timezone.utc).isoformat(),
    })
    self.save()

def subtract_balance(self, count: int) -> bool:
    """Вычесть посты (при успешной рассылке)"""
    current = self.get_balance()
    if current < count:
        return False  # недостаточно баланса
    self.state["balance"]["posts"] -= count
    self.state["balance"]["total_spent"] = self.state["balance"].get("total_spent", 0) + count
    self.state["balance"].setdefault("balance_history", []).append({
        "type": "spend",
        "amount": -count,
        "date": datetime.now(timezone.utc).isoformat(),
    })
    self.save()
    return True

def init_free_balance(self):
    """Инициализировать 30 бесплатных постов при первом подключении"""
    if "balance" not in self.state:
        self.state["balance"] = {
            "posts": 30,
            "total_purchased": 0,
            "total_spent": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "balance_history": [{
                "type": "initial_free",
                "amount": 30,
                "date": datetime.now(timezone.utc).isoformat(),
            }],
        }
        self.save()
```

### 2. main.py — обработчики

**Callback handlers (добавить в конец файла перед @dp.startup):**

```python
# Кнопка "Баланс постов" в меню Рассылки
@dp.callback_query(F.data == "bc_balance")
async def broadcast_balance(query: CallbackQuery):
    """Показать баланс и кнопку купить"""
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    balance = bm.get_balance()
    
    text = f"""💰 МОЙ БАЛАНС

🟢 Доступно: {balance} постов
📊 Всего куплено: {bm.state.get('balance', {}).get('total_purchased', 0)}
📤 Всего потрачено: {bm.state.get('balance', {}).get('total_spent', 0)}

Платите только за успешные публикации.
"""
    
    rows = [
        [InlineKeyboardButton(text="🛍 Купить тариф", callback_data="bc_tariffs")],
        [InlineKeyboardButton(text="📋 История", callback_data="bc_balance_history")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="bc_main")],
    ]
    
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await query.answer()

# Кнопка "Купить тариф"
@dp.callback_query(F.data == "bc_tariffs")
async def broadcast_tariffs(query: CallbackQuery):
    """Показать список тарифов с кнопками покупки"""
    text = """🛍 ВЫБЕРИТЕ ПАКЕТ

🟢 Пробный
   80 постов / €5 (€0.0625/пост)

🔵 Стартовый
   250 постов / €12 (€0.048/пост)
   Экономия: 23%

🔥 ОПТИМАЛЬНЫЙ ⭐ ПОПУЛЯРНЫЙ
   600 постов / €20 (€0.0333/пост)
   Экономия: 47%

🔴 Оптовый
   1500 постов / €40 (€0.0267/пост)
   Экономия: 57%

💳 Оплата через Stripe
✅ Платите только за успешные публикации
"""
    
    rows = [
        [InlineKeyboardButton(text="🟢 Пробный €5", callback_data="buy_trial")],
        [InlineKeyboardButton(text="🔵 Стартовый €12", callback_data="buy_starter")],
        [InlineKeyboardButton(text="🔥 Оптимальный €20", callback_data="buy_optimal")],
        [InlineKeyboardButton(text="🔴 Оптовый €40", callback_data="buy_wholesale")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="bc_balance")],
    ]
    
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await query.answer()

# На этом этапе кнопки покупки будут stub'ы (в Фазе 2 добавим Stripe)
@dp.callback_query(F.data.startswith("buy_"))
async def buy_tariff(query: CallbackQuery):
    await query.answer("⏳ Интеграция Stripe в разработке...", show_alert=True)
```

### 3. Обновить broadcast_main_keyboard()

**В функции broadcast_main_keyboard (строка ~505):**

Добавить первой строкой после открытия rows:

```python
balance = bm.get_balance()  # Получить баланс
rows.append([InlineKeyboardButton(text=f"💰 Баланс: {balance} постов", callback_data="bc_balance")])
```

### 4. Инициализация баланса при /start

**В /start handler (строка ~917):**

После создания broadcast_manager добавить:

```python
bm = scoped_broadcast_manager(message.from_user.id)
bm.init_free_balance()  # Добавить эту строку
```

---

## 🔍 Проверка (Verification)

### Что тестировать:
1. `/start` → баланс = 30 постов
2. Открыть "Рассылка" → видим "💰 Баланс: 30 постов"
3. Нажать на баланс → открывается меню баланса с 30 постов
4. Нажать "Купить тариф" → показываются 4 пакета
5. Проверить JSON файл — есть поле `balance` с начальными значениями

### Что НЕ должно ломаться:
- Рассылка (пока не добавили проверку баланса)
- Тест (пока не добавили проверку баланса)
- Существующие меню

---

## 📝 Файлы для изменения

1. `bot/broadcast_manager.py` — добавить методы баланса
2. `bot/main.py` — добавить обработчики и кнопку в меню
3. Структура `broadcast_state.json` обновится автоматически при init_free_balance()

---

## 🎯 Результат

После этой фазы:
- ✅ Пользователь видит свой баланс (30 постов)
- ✅ Есть UI для выбора тарифа (4 варианта)
- ✅ Баланс инициализируется с 30 бесплатными постами
- ✅ История изменений баланса ведется в JSON
- ⏳ Stripe заглушка (будет в Фазе 2)
- ⏳ Проверка баланса перед рассылкой (будет в Фазе 3)
