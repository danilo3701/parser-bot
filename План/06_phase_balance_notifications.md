# Фаза 6: Уведомления о низком балансе

## Контекст
Когда баланс падает ниже определённого порога (по-умолчанию 30 постов), бот отправляет уведомление один раз в день. Пользователь может изменить порог в настройках.

---

## Архитектура

### Где искать:
- `bot/broadcast_manager.py` — методы управления порогом
- `bot/main.py` — scheduler_loop() (строка ~857)
- `bot/main.py` — меню настроек (Settings)

---

## 🎨 Визуальный дизайн

### 1. Уведомление о низком балансе

```
⚠️ БАЛАНС ЗАКАНЧИВАЕТСЯ

Осталось: 20 постов

При таком балансе вы сможете опубликовать:
• 2 рассылки по 10 групп
• 4 рассылки по 5 групп

Пополните баланс!

[💳 Купить тариф] [⏭ Позже]
```

### 2. Меню настроек уведомлений

**В главном меню Settings:**
```
⚙️ НАСТРОЙКИ

→ Баланс постов
  ├─ Порог уведомления: 30 постов
  ├─ [Изменить] [Отключить]
  └─ Последнее уведомление: 13 апр 08:30

→ Язык
→ Временная зона
→ Помощь
```

### 3. При нажатии "Изменить порог"

```
🔔 ПОРОГ УВЕДОМЛЕНИЯ

Текущий порог: 30 постов

Отправлять уведомление когда баланс ≤ X постов

Выберите порог:
[ 10 ] [ 20 ] [ 30 ] [ 50 ] [ 100 ]

Или напишите свое число (10-500):
_______
```

---

## 💻 Бэкэнд логика

### 1. broadcast_manager.py — новые методы

```python
def get_low_balance_threshold(self) -> int:
    """Получить порог уведомления"""
    return self.state.get("balance", {}).get("low_balance_threshold", 30)

def set_low_balance_threshold(self, threshold: int):
    """Установить порог уведомления"""
    if threshold < 10 or threshold > 500:
        raise ValueError("Threshold must be between 10 and 500")
    
    self.state.setdefault("balance", {})["low_balance_threshold"] = threshold
    self.save()

def get_last_low_balance_notification(self) -> str | None:
    """Получить дату последнего уведомления о низком балансе"""
    return self.state.get("balance", {}).get("last_low_balance_notification")

def mark_low_balance_notification_sent(self):
    """Отметить что уведомление отправлено сегодня"""
    self.state.setdefault("balance", {})["last_low_balance_notification"] = \
        datetime.now(timezone.utc).isoformat()
    self.save()
```

### 2. main.py — обновить scheduler_loop()

**Добавить в scheduler_loop() после проверки баланса:**

```python
async def scheduler_loop():
    while True:
        try:
            # ... существующий код ...
            
            for user_id in sorted(user_ids):
                bm = scoped_broadcast_manager(user_id)
                
                # НОВОЕ: Проверить низкий баланс
                balance = bm.get_balance()
                threshold = bm.get_low_balance_threshold()
                
                if balance <= threshold:
                    # Проверить что уведомление не отправлялось сегодня
                    last_notification = bm.get_last_low_balance_notification()
                    
                    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    last_date = last_notification[:10] if last_notification else None
                    
                    if last_date != today:
                        # Отправить уведомление
                        text = f"""⚠️ БАЛАНС ЗАКАНЧИВАЕТСЯ

Осталось: {balance} постов

При таком балансе вы сможете опубликовать:
• {balance // 10} рассылок по 10 групп
• {balance // 5} рассылок по 5 групп

Пополните баланс!"""
                        
                        rows = [
                            [InlineKeyboardButton(
                                text="💳 Купить тариф",
                                callback_data="bc_tariffs"
                            )],
                        ]
                        
                        await bot.send_message(
                            user_id,
                            text,
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
                        )
                        
                        bm.mark_low_balance_notification_sent()
                
                # ... остальной код ...
            
            await asyncio.sleep(20)
        except Exception:
            await asyncio.sleep(20)
```

### 3. main.py — меню настроек

**Добавить callback для настройки порога:**

```python
@dp.callback_query(F.data == "settings_balance_threshold")
async def settings_balance_threshold(query: CallbackQuery):
    """Меню настройки порога уведомлений"""
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    current = bm.get_low_balance_threshold()
    
    text = f"""🔔 ПОРОГ УВЕДОМЛЕНИЯ

Текущий порог: {current} постов

Отправлять уведомление когда баланс ≤ X постов

Выберите:"""
    
    rows = [
        [
            InlineKeyboardButton(text="10", callback_data="set_threshold_10"),
            InlineKeyboardButton(text="20", callback_data="set_threshold_20"),
            InlineKeyboardButton(text="30", callback_data="set_threshold_30"),
        ],
        [
            InlineKeyboardButton(text="50", callback_data="set_threshold_50"),
            InlineKeyboardButton(text="100", callback_data="set_threshold_100"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="settings")],
    ]
    
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await query.answer()

@dp.callback_query(F.data.startswith("set_threshold_"))
async def set_threshold(query: CallbackQuery):
    """Установить порог"""
    threshold = int(query.data.split("_")[2])
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    bm.set_low_balance_threshold(threshold)
    
    await query.answer(f"✅ Порог установлен на {threshold} постов")
    
    # Вернуться в меню настроек баланса
    await settings_balance_threshold(query)
```

---

## 📝 Файлы для изменения

1. `bot/broadcast_manager.py` — добавить методы порога и уведомлений
2. `bot/main.py` — обновить `scheduler_loop()` и добавить handlers настроек

---

## 📋 Предусловия для Фазы 6

- ✅ Фаза 5 закончена

---

## 🎯 Результат

После этой фазы:
- ✅ Пользователь получает уведомление когда баланс низкий
- ✅ Уведомление отправляется не более одного раза в день
- ✅ Пользователь может изменить порог в настройках
- ✅ По-умолчанию порог = 30 постов
