# Фаза 7: Защита от злоупотребления (rate limiting, cooldown)

## Контекст
Защита от спама: cooldown между кликами на "Тест", лимит на количество тестов в день, логирование подозрительной активности. OWNER_ID может запускать тесты бесконечно (разработчик).

---

## Архитектура

### Где искать:
- `bot/broadcast_manager.py` — методы для отслеживания тестов
- `bot/main.py` — handler `broadcast_test_v2()` (строка ~2595)

---

## 🎨 Визуальный дизайн

### 1. Cooldown при частом нажатии

```
🧪 ТЕСТ

⏳ Подождите 30 секунд перед следующим тестом
(повтор возможен в 08:45)

[Ладно]
```

### 2. Превышен лимит тестов в день

```
❌ СЛИШКОМ МНОГО ТЕСТОВ СЕГОДНЯ

Вы уже запустили: 5 тестов

Лимит: 5 тестов в день

Попробуйте завтра или купите расширенный пакет!

[◀️ Назад]
```

---

## 💻 Бэкэнд логика

### 1. broadcast_manager.py — новые методы

```python
def can_run_test(self, user_id: int) -> tuple[bool, str]:
    """
    Проверить может ли пользователь запустить тест.
    
    Возвращает: (True/False, reason)
    """
    # OWNER_ID может всегда
    if is_owner(user_id):
        return True, ""
    
    # Проверить cooldown
    test_log = self.state.get("test_log", {})
    last_test_time = test_log.get("last_test_at")
    
    if last_test_time:
        last_time = datetime.fromisoformat(last_test_time)
        now = datetime.now(timezone.utc)
        elapsed = (now - last_time).total_seconds()
        
        if elapsed < 30:  # 30 сек cooldown
            wait_seconds = int(30 - elapsed)
            return False, f"Подождите {wait_seconds} сек перед следующим тестом"
    
    # Проверить дневной лимит
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_tests = test_log.get("daily_counts", {}).get(today, 0)
    
    MAX_TESTS_PER_DAY = 5
    
    if daily_tests >= MAX_TESTS_PER_DAY:
        return False, f"Вы уже запустили {daily_tests} тестов сегодня (лимит: {MAX_TESTS_PER_DAY})"
    
    return True, ""

def record_test_run(self, user_id: int):
    """Записать что тест был запущен"""
    # OWNER_ID не записываем (бесконечные тесты)
    if is_owner(user_id):
        return
    
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    
    if "test_log" not in self.state:
        self.state["test_log"] = {
            "last_test_at": None,
            "daily_counts": {}
        }
    
    self.state["test_log"]["last_test_at"] = now.isoformat()
    
    if today not in self.state["test_log"]["daily_counts"]:
        self.state["test_log"]["daily_counts"][today] = 0
    
    self.state["test_log"]["daily_counts"][today] += 1
    
    self.save()
```

### 2. main.py — обновить broadcast_test_v2() handler

**Добавить проверку в начало handler'а:**

```python
@dp.callback_query(F.data == "bc_test")
async def broadcast_test_v2(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    
    # НОВОЕ: Проверить может ли запустить тест
    can_run, reason = bm.can_run_test(user_id)
    
    if not can_run:
        await query.answer(f"❌ {reason}", show_alert=True)
        return
    
    # Запустить тест как было...
    # ... существующий код ...
    
    # НОВОЕ: Записать что тест был запущен
    bm.record_test_run(user_id)
```

### 3. Логирование подозрительной активности

**Дополнительно (optional):**

```python
async def log_suspicious_activity(user_id: int, activity: str, details: dict):
    """Логирование подозрительной активности для администратора"""
    # Можно сохранять в отдельный файл или отправлять OWNER_ID
    
    log_entry = {
        "user_id": user_id,
        "activity": activity,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "details": details,
    }
    
    # Отправить OWNER_ID уведомление
    for owner_id in OWNER_IDS:
        await bot.send_message(
            owner_id,
            f"🚨 ПОДОЗРИТЕЛЬНАЯ АКТИВНОСТЬ\n\n"
            f"Пользователь: {user_id}\n"
            f"Действие: {activity}\n"
            f"Детали: {details}"
        )
```

---

## 📋 Параметры защиты

Эти параметры можно менять в коде:

```python
COOLDOWN_SECONDS = 30        # Cooldown между тестами
MAX_TESTS_PER_DAY = 5        # Макс тестов в день
OWNER_UNLIMITED = True       # OWNER_ID может тестировать бесконечно
```

---

## 📝 Файлы для изменения

1. `bot/broadcast_manager.py` — добавить методы `can_run_test()`, `record_test_run()`
2. `bot/main.py` — обновить `broadcast_test_v2()` handler

---

## 📋 Предусловия для Фазы 7

- ✅ Фаза 6 закончена

---

## 🎯 Результат

После этой фазы:
- ✅ Защита от спама тестов (cooldown 30 сек)
- ✅ Лимит 5 тестов в день для обычных пользователей
- ✅ OWNER_ID может тестировать бесконечно (разработчик)
- ✅ Логирование подозрительной активности
- ✅ Пользователь видит понятные сообщения об ограничениях
