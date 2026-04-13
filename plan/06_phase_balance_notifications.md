# Фаза 6: Система уведомлений (4 типа + меню настроек)

## Контекст
Минималистичная система уведомлений — только то что реально нужно пользователю.
Из 4 типов только 1 настраивается, остальные 3 обязательные.

**4 типа уведомлений:**
| # | Тип | Можно отключить | Когда |
|---|-----|-----------------|-------|
| 1 | ⚠️ Баланс заканчивается | ✅ Да | Когда баланс ≤ порог (настраивается) |
| 2 | 📊 Аналитика авторассылки | ❌ Нет | После каждой авторассылки по расписанию |
| 3 | 💳 Платеж успешен | ❌ Нет | Stripe подтвердил оплату |
| 4 | ❌ Платеж отклонен | ❌ Нет | Stripe отклонил карту |

---

## Архитектура

### Где искать:
- `bot/broadcast_manager.py` — методы уведомлений
- `bot/main.py` — scheduler_loop() (строка ~857) — отправка аналитики и проверка баланса
- `bot/main.py` — handlers для меню настроек уведомлений
- `bot/broadcast_state.json` — структура notifications

### Что меняется в JSON:

```json
{
  "notifications": {
    "balance_low": {
      "enabled": true,
      "threshold": 30,
      "last_sent": null
    }
  }
}
```

> Простая структура: только баланс хранит настройки. Остальные 3 уведомления — без состояния, отправляются всегда.

---

## 🎨 Визуальный дизайн

### 1. Меню настроек → Уведомления

**Путь:** ⚙️ Настройки → 🔔 Уведомления

```
🔔 УВЕДОМЛЕНИЯ

☑️ Баланс заканчивается
   Порог: 30 постов
   [Изменить порог] [Отключить]

✅ Аналитика рассылки (обязательное)
   Приходит после каждой авторассылки

✅ Платёж успешен (обязательное)
✅ Платёж отклонён (обязательное)

[◀️ Назад]
```

### 2. Меню изменения порога баланса

```
⚠️ ПОРОГ УВЕДОМЛЕНИЯ О БАЛАНСЕ

Текущий порог: 30 постов

Уведомить когда остаётся ≤ X постов:

[10]  [20]  [30]  [50]  [100]

[◀️ Назад]
```

---

## 📲 Примеры уведомлений

### 1. Баланс заканчивается

```
⚠️ БАЛАНС ЗАКАНЧИВАЕТСЯ

Осталось: 20 постов

Вы можете опубликовать ещё:
• 2 рассылки по 10 групп
• 4 рассылки по 5 групп

[💳 Купить тариф]
```

### 2. Аналитика авторассылки (по расписанию)

```
📊 АВТОРАССЫЛКА ЗАВЕРШЕНА
🕐 Пн 14 апр, 08:12

Группы: 10
├─ ✅ Отправлено: 8
└─ ❌ Не отправлено: 2

💰 Потрачено: 8 постов
📉 Баланс: 92 поста

⚠️ 2 группы не ответили — проверьте тест
```

### 3. Платёж успешен

```
💳 ПЛАТЁЖ УСПЕШЕН

Пакет: Оптимальный (600 постов)
Сумма: €20

Новый баланс: 692 поста

[📢 Начать рассылку]
```

### 4. Платёж отклонён

```
❌ ПЛАТЁЖ ОТКЛОНЁН

Причина: Недостаточно средств

Попробуйте другую карту.

[💳 Попробовать снова]
```

---

## 💻 Бэкэнд логика

### 1. broadcast_manager.py — методы уведомлений

```python
def init_notifications(self):
    """Инициализировать настройки уведомлений (только баланс)"""
    if "notifications" not in self.state:
        self.state["notifications"] = {
            "balance_low": {
                "enabled": True,
                "threshold": 30,
                "last_sent": None,
            }
        }
        self.save()

def get_balance_notif_enabled(self) -> bool:
    return self.state.get("notifications", {}).get("balance_low", {}).get("enabled", True)

def set_balance_notif_enabled(self, enabled: bool):
    self.state.setdefault("notifications", {}).setdefault("balance_low", {})["enabled"] = enabled
    self.save()

def get_balance_notif_threshold(self) -> int:
    return self.state.get("notifications", {}).get("balance_low", {}).get("threshold", 30)

def set_balance_notif_threshold(self, threshold: int):
    if threshold < 10 or threshold > 500:
        raise ValueError("Threshold must be between 10 and 500")
    self.state.setdefault("notifications", {}).setdefault("balance_low", {})["threshold"] = threshold
    self.save()

def was_balance_notif_sent_today(self) -> bool:
    last = self.state.get("notifications", {}).get("balance_low", {}).get("last_sent")
    if not last:
        return False
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return last[:10] == today

def mark_balance_notif_sent(self):
    self.state.setdefault("notifications", {}).setdefault("balance_low", {})["last_sent"] = \
        datetime.now(timezone.utc).isoformat()
    self.save()
```

### 2. main.py — scheduler_loop() — аналитика + баланс

**После каждой авторассылки добавить (в конец цикла):**

```python
# Уже существует: result = await execute_broadcast(...)
# НОВОЕ: Отправить аналитику автоматической рассылки

sent_count = result.get("sent_count", 0)
total_groups = len(groups)
failed_count = total_groups - sent_count
new_balance = bm.get_balance()
slot_display = f"{weekday.capitalize()} {now_local.strftime('%d %b, %H:%M')}"

analytic_text = f"""📊 АВТОРАССЫЛКА ЗАВЕРШЕНА
🕐 {slot_display}

Группы: {total_groups}
├─ ✅ Отправлено: {sent_count}
└─ ❌ Не отправлено: {failed_count}

💰 Потрачено: {sent_count} постов
📉 Баланс: {new_balance} постов"""

if failed_count > 0:
    analytic_text += "\n\n⚠️ Есть неудачные группы — запустите тест"

await bot.send_message(user_id, analytic_text)

# НОВОЕ: Проверить низкий баланс (раз в день)
if bm.get_balance_notif_enabled():
    threshold = bm.get_balance_notif_threshold()
    if new_balance <= threshold and not bm.was_balance_notif_sent_today():
        rows = [[InlineKeyboardButton(text="💳 Купить тариф", callback_data="bc_tariffs")]]
        await bot.send_message(
            user_id,
            f"""⚠️ БАЛАНС ЗАКАНЧИВАЕТСЯ

Осталось: {new_balance} постов

Вы можете опубликовать ещё:
• {new_balance // max(1, total_groups)} рассылок

[Пополните баланс!]""",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
        bm.mark_balance_notif_sent()
```

### 3. main.py — handlers меню уведомлений

```python
@dp.callback_query(F.data == "settings_notifications")
async def settings_notifications(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)

    enabled = bm.get_balance_notif_enabled()
    threshold = bm.get_balance_notif_threshold()
    status_icon = "☑️" if enabled else "☐"

    text = f"""🔔 УВЕДОМЛЕНИЯ

{status_icon} Баланс заканчивается
   Порог: {threshold} постов

✅ Аналитика рассылки (обязательное)
✅ Платёж успешен (обязательное)
✅ Платёж отклонён (обязательное)"""

    toggle_label = "Отключить баланс" if enabled else "Включить баланс"

    rows = [
        [InlineKeyboardButton(text="⚠️ Изменить порог", callback_data="notif_threshold_menu")],
        [InlineKeyboardButton(text=toggle_label, callback_data="notif_balance_toggle")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="settings")],
    ]

    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await query.answer()


@dp.callback_query(F.data == "notif_threshold_menu")
async def notif_threshold_menu(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    current = bm.get_balance_notif_threshold()

    text = f"""⚠️ ПОРОГ УВЕДОМЛЕНИЯ О БАЛАНСЕ

Текущий порог: {current} постов

Уведомить когда остаётся ≤ X постов:"""

    rows = [
        [
            InlineKeyboardButton(text="10", callback_data="notif_set_threshold_10"),
            InlineKeyboardButton(text="20", callback_data="notif_set_threshold_20"),
            InlineKeyboardButton(text="30", callback_data="notif_set_threshold_30"),
        ],
        [
            InlineKeyboardButton(text="50", callback_data="notif_set_threshold_50"),
            InlineKeyboardButton(text="100", callback_data="notif_set_threshold_100"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="settings_notifications")],
    ]

    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await query.answer()


@dp.callback_query(F.data.startswith("notif_set_threshold_"))
async def notif_set_threshold(query: CallbackQuery):
    threshold = int(query.data.split("_")[-1])
    bm = scoped_broadcast_manager(query.from_user.id)
    bm.set_balance_notif_threshold(threshold)
    await query.answer(f"✅ Порог: {threshold} постов")
    await notif_threshold_menu(query)


@dp.callback_query(F.data == "notif_balance_toggle")
async def notif_balance_toggle(query: CallbackQuery):
    bm = scoped_broadcast_manager(query.from_user.id)
    current = bm.get_balance_notif_enabled()
    bm.set_balance_notif_enabled(not current)
    new_label = "включено" if not current else "отключено"
    await query.answer(f"✅ Уведомление о балансе {new_label}")
    await settings_notifications(query)
```

### 4. Инициализация при /start

```python
bm.init_free_balance()
bm.init_notifications()  # ДОБАВИТЬ
```

---

## 🔍 Проверка (Verification)

1. `/start` → JSON обновлён с `notifications.balance_low`
2. ⚙️ Настройки → 🔔 Уведомления → показывает 4 типа
3. Нажать "Изменить порог" → выбрать 50 → порог изменился
4. Нажать "Отключить баланс" → ☐ уведомление выключено
5. Дождаться авторассылки → пришло аналитическое сообщение
6. Если баланс ≤ порог → пришло уведомление раз в день

---

## 📝 Файлы для изменения

1. `bot/broadcast_manager.py` — добавить методы уведомлений
2. `bot/main.py` — scheduler_loop(), handlers настроек, init в /start

---

## 📋 Предусловия

- ✅ Фаза 5 закончена

---

## 🎯 Результат

- ✅ 4 уведомления (только нужные)
- ✅ Только баланс настраивается (порог + вкл/выкл)
- ✅ Аналитика приходит после каждой авторассылки (обязательное)
- ✅ Платёжные уведомления обязательные
- ✅ Простая структура JSON (нет лишних полей)
