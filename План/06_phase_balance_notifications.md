# Фаза 6: Система уведомлений (8 типов + меню настроек)

## Контекст
Полная система уведомлений: баланс, рассылка, платежи, обновления и т.д. Пользователь может включить/отключить каждое уведомление и настроить параметры. Меню "Настройки уведомлений" в главном Settings.

**8 типов уведомлений:**
1. Баланс заканчивается (⚠️ настраивается)
2. Рассылка завершена (✅ всегда)
3. Группы в черном списке (🚫 отключаемо)
4. Расписание отключено (📅 отключаемо)
5. Ошибки при рассылке (❌ отключаемо)
6. Платеж успешен (💳 обязательное)
7. Платеж отклонен (⚠️ обязательное)
8. Обновления бота (🔔 отключаемо)

---

## Архитектура

### Где искать:
- `bot/broadcast_manager.py` — методы управления уведомлениями
- `bot/main.py` — scheduler_loop() (строка ~857) — отправка уведомлений
- `bot/main.py` — меню Settings → Notifications (новое)
- `bot/broadcast_state.json` — структура notifications preferences

### Что меняется в JSON:

```json
{
  "notifications": {
    "enabled": true,
    "preferences": {
      "balance_low": {"enabled": true, "threshold": 30},
      "broadcast_completed": {"enabled": true},
      "groups_blocked": {"enabled": true},
      "schedule_disabled": {"enabled": true},
      "broadcast_error": {"enabled": true},
      "payment_success": {"enabled": true},   // ОБЯЗАТЕЛЬНОЕ
      "payment_failed": {"enabled": true},    // ОБЯЗАТЕЛЬНОЕ
      "bot_updates": {"enabled": true}
    },
    "last_notifications_sent": {
      "balance_low": "2026-04-13T08:30:00Z",
      "schedule_disabled": "2026-04-13T09:00:00Z"
    }
  }
}
```

---

## 🎨 Визуальный дизайн

### 1. Главное меню настроек

```
⚙️ НАСТРОЙКИ

→ 🔔 Уведомления (новое!)
  └─ [Настроить]

→ 💰 Баланс постов
  └─ Порог: 30

→ 🌍 Язык
→ 🕐 Временная зона
→ 📞 Помощь
```

### 2. Меню уведомлений (список)

```
🔔 УВЕДОМЛЕНИЯ

Выберите что получать:

1️⃣ Баланс заканчивается ☑️
   Порог: 30 постов [Изменить]

2️⃣ Рассылка завершена ☑️
   Результаты после отправки

3️⃣ Группы в черном списке ☑️
   Подробные ошибки

4️⃣ Расписание отключено ☑️
   Напоминание включить

5️⃣ Ошибки при рассылке ☑️
   Как исправить

6️⃣ Платеж успешен ☑️
   (не отключается)

7️⃣ Платеж отклонен ☑️
   (не отключается)

8️⃣ Обновления бота ☑️
   Важные новости

[◀️ Назад] [Сбросить на умолчанию]
```

### 3. Меню настройки одного уведомления

```
🔔 Баланс заканчивается

📋 ОПИСАНИЕ:
Уведомление когда баланс < порога

⚙️ ПАРАМЕТРЫ:

Порог уведомления:
[10] [20] [30] [50] [100] [Свое]

Текущий: 30 постов

☑️ Включено  ☐ Отключено

[Сохранить] [◀️ Назад]
```

### 4. Примеры уведомлений

**Баланс заканчивается:**
```
⚠️ БАЛАНС ЗАКАНЧИВАЕТСЯ

Осталось: 20 постов

Можете опубликовать:
• 2 рассылки по 10 групп
• 4 рассылки по 5 групп

[💳 Купить] [Позже]
```

**Рассылка завершена:**
```
✅ РАССЫЛКА ЗАВЕРШЕНА

Результаты:
├─ Отправлено: 8 из 10
├─ Потрачено: 8 постов
└─ Баланс: 92 поста

⚠️ Забаны: @group1, @group2
```

**Платеж успешен:**
```
💳 ПЛАТЕЖ УСПЕШЕН

Вы купили: 600 постов / €20
Новый баланс: 750 постов

[📢 Рассылка] [Еще пакеты]
```

---

## 💻 Бэкэнд логика

### 1. broadcast_manager.py — новые методы

```python
def init_notifications(self):
    """Инициализировать настройки уведомлений"""
    if "notifications" not in self.state:
        self.state["notifications"] = {
            "enabled": True,
            "preferences": {
                "balance_low": {"enabled": True, "threshold": 30},
                "broadcast_completed": {"enabled": True},
                "groups_blocked": {"enabled": True},
                "schedule_disabled": {"enabled": True},
                "broadcast_error": {"enabled": True},
                "payment_success": {"enabled": True},  # ОБЯЗАТЕЛЬНОЕ
                "payment_failed": {"enabled": True},   # ОБЯЗАТЕЛЬНОЕ
                "bot_updates": {"enabled": True},
            },
            "last_notifications_sent": {},
        }
        self.save()

def is_notification_enabled(self, notification_type: str) -> bool:
    """Проверить включено ли уведомление"""
    prefs = self.state.get("notifications", {}).get("preferences", {})
    return prefs.get(notification_type, {}).get("enabled", True)

def set_notification_enabled(self, notification_type: str, enabled: bool):
    """Включить/отключить уведомление"""
    # Платежи нельзя отключить
    if notification_type in ["payment_success", "payment_failed"]:
        return
    
    notif = self.state.setdefault("notifications", {}).setdefault("preferences", {})
    if notification_type not in notif:
        notif[notification_type] = {}
    notif[notification_type]["enabled"] = enabled
    self.save()

def get_notification_threshold(self, notification_type: str) -> int:
    """Получить порог для уведомления"""
    prefs = self.state.get("notifications", {}).get("preferences", {})
    return prefs.get(notification_type, {}).get("threshold", 30)

def set_notification_threshold(self, notification_type: str, threshold: int):
    """Установить порог"""
    if threshold < 10 or threshold > 500:
        raise ValueError("Threshold must be between 10 and 500")
    
    notif = self.state.setdefault("notifications", {}).setdefault("preferences", {})
    if notification_type not in notif:
        notif[notification_type] = {}
    notif[notification_type]["threshold"] = threshold
    self.save()

def was_notification_sent_today(self, notification_type: str) -> bool:
    """Проверить было ли уведомление отправлено сегодня"""
    last_sent = self.state.get("notifications", {}).get("last_notifications_sent", {}).get(notification_type)
    if not last_sent:
        return False
    
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last_date = last_sent[:10]
    return last_date == today

def mark_notification_sent(self, notification_type: str):
    """Отметить что уведомление отправлено"""
    notif = self.state.setdefault("notifications", {}).setdefault("last_notifications_sent", {})
    notif[notification_type] = datetime.now(timezone.utc).isoformat()
    self.save()
```

### 2. main.py — обновить /start

**В handler /start добавить:**
```python
bm = scoped_broadcast_manager(message.from_user.id)
bm.init_free_balance()
bm.init_notifications()  # ДОБАВИТЬ ЭТУ СТРОКУ
```

### 3. main.py — обновить scheduler_loop()

**Добавить проверку уведомлений (после существующего кода):**

```python
# НОВОЕ: Отправить уведомление о низком балансе
if bm.is_notification_enabled("balance_low"):
    balance = bm.get_balance()
    threshold = bm.get_notification_threshold("balance_low")
    
    if balance <= threshold and not bm.was_notification_sent_today("balance_low"):
        text = f"""⚠️ БАЛАНС ЗАКАНЧИВАЕТСЯ

Осталось: {balance} постов

При таком балансе вы сможете опубликовать:
• {balance // 10} рассылок по 10 групп
• {balance // 5} рассылок по 5 групп

Пополните баланс!"""
        
        rows = [
            [InlineKeyboardButton(text="💳 Купить", callback_data="bc_tariffs")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings_notifications")],
        ]
        
        await bot.send_message(
            user_id,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
        
        bm.mark_notification_sent("balance_low")
```

### 4. main.py — новые handlers для меню уведомлений

```python
@dp.callback_query(F.data == "settings_notifications")
async def settings_notifications(query: CallbackQuery):
    """Главное меню уведомлений"""
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    prefs = bm.state.get("notifications", {}).get("preferences", {})
    
    text = """🔔 УВЕДОМЛЕНИЯ

Выберите что получать:

1️⃣ Баланс заканчивается
2️⃣ Рассылка завершена
3️⃣ Группы в черном списке
4️⃣ Расписание отключено
5️⃣ Ошибки при рассылке
6️⃣ Платеж успешен (обязательное)
7️⃣ Платеж отклонен (обязательное)
8️⃣ Обновления бота"""
    
    rows = [
        [InlineKeyboardButton(text="⚠️ Баланс", callback_data="notif_balance_low")],
        [InlineKeyboardButton(text="✅ Рассылка", callback_data="notif_broadcast_completed")],
        [InlineKeyboardButton(text="🚫 Группы", callback_data="notif_groups_blocked")],
        [InlineKeyboardButton(text="📅 Расписание", callback_data="notif_schedule_disabled")],
        [InlineKeyboardButton(text="❌ Ошибки", callback_data="notif_broadcast_error")],
        [InlineKeyboardButton(text="🔔 Обновления", callback_data="notif_bot_updates")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="settings")],
    ]
    
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await query.answer()

@dp.callback_query(F.data.startswith("notif_"))
async def notif_detail(query: CallbackQuery):
    """Детальные настройки уведомления"""
    notif_type = query.data[6:]  # Remove "notif_"
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    
    enabled = bm.is_notification_enabled(notif_type)
    threshold = bm.get_notification_threshold(notif_type)
    is_required = notif_type in ["payment_success", "payment_failed"]
    
    status = "☑️ Включено" if enabled else "☐ Отключено"
    required_text = " (обязательное)" if is_required else ""
    
    text = f"""🔔 {notif_type.replace('_', ' ').upper()}{required_text}

Статус: {status}"""
    
    if notif_type == "balance_low":
        text += f"\n\nПорог: {threshold} постов"
    
    rows = []
    
    if notif_type == "balance_low":
        rows.extend([
            [
                InlineKeyboardButton(text="10", callback_data="notif_threshold_10"),
                InlineKeyboardButton(text="20", callback_data="notif_threshold_20"),
                InlineKeyboardButton(text="30", callback_data="notif_threshold_30"),
            ],
            [
                InlineKeyboardButton(text="50", callback_data="notif_threshold_50"),
                InlineKeyboardButton(text="100", callback_data="notif_threshold_100"),
            ],
        ])
    
    if not is_required:
        toggle_text = "Отключить" if enabled else "Включить"
        rows.append([InlineKeyboardButton(text=toggle_text, callback_data=f"notif_toggle_{notif_type}")])
    
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="settings_notifications")])
    
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await query.answer()

@dp.callback_query(F.data.startswith("notif_toggle_"))
async def notif_toggle(query: CallbackQuery):
    """Включить/отключить уведомление"""
    notif_type = query.data[13:]  # Remove "notif_toggle_"
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    
    current = bm.is_notification_enabled(notif_type)
    bm.set_notification_enabled(notif_type, not current)
    
    new_status = "включено" if not current else "отключено"
    await query.answer(f"✅ Уведомление {new_status}")
    
    # Обновить экран
    await notif_detail(query)

@dp.callback_query(F.data.startswith("notif_threshold_"))
async def notif_threshold(query: CallbackQuery):
    """Установить порог"""
    threshold = int(query.data.split("_")[2])
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    
    bm.set_notification_threshold("balance_low", threshold)
    
    await query.answer(f"✅ Порог: {threshold} постов")
    
    # Обновить экран
    query.data = "notif_balance_low"
    await notif_detail(query)
```

---

## 🔍 Проверка (Verification)

### Что тестировать:
1. `/start` → инициализация notifications
2. "⚙️ Настройки" → "🔔 Уведомления"
3. Открыть "⚠️ Баланс" → показывает текущий порог (30)
4. Нажать "[20]" → порог изменился на 20
5. Баланс упал до 20 → уведомление (один раз в день)
6. "🔔 Обновления" → нажать "Отключить" → ☐ Отключено
7. Проверить JSON → структура notifications обновлена

---

## 📝 Файлы для изменения

1. `bot/broadcast_manager.py` — добавить методы управления уведомлениями
2. `bot/main.py` — обновить /start, scheduler_loop(), добавить handlers

---

## 📋 Предусловия для Фазы 6

- ✅ Фаза 5 закончена

---

## 🎯 Результат

После этой фазы:
- ✅ Полная система уведомлений (8 типов)
- ✅ Меню "Настройки уведомлений" в Settings
- ✅ Каждое уведомление можно включить/отключить
- ✅ Параметры для каждого уведомления (порог, частота)
- ✅ Платежные уведомления обязательные (нельзя отключить)
- ✅ История последних уведомлений в JSON
