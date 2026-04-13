# Фаза 7: Защита от злоупотребления (rate limiting, auto-delete tests)

## Контекст
Защита от спама: 
- Тестовые сообщения **автоматически удаляются через 60 сек** (через Telethon)
- Cooldown между кликами на "Тест" 
- Лимит на количество тестов в день
- Логирование подозрительной активности
- OWNER_ID может запускать тесты бесконечно (разработчик)

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

### 0. AUTO-DELETE механизм (ключевое изменение!)

**Когда тест публикуется через Telethon:**

```python
# В broadcast_sender.py или где публикуется тест:

# 1. Отправляем тестовое сообщение от имени пользователя
message = await client.send_message(
    chat_id=group_id,
    message="🧪 TEST MESSAGE"
)
message_id = message.id

# 2. Через 60 секунд УДАЛЯЕМ от имени пользователя (Telethon)
await asyncio.sleep(60)
try:
    await client.delete_messages(chat_id=group_id, message_ids=[message_id])
    # ✅ Сообщение удалено! Группа не удаляла его = безопасна
    test_result = "PASSED"
except Exception as e:
    # Если ошибка при удалении = группа уже удалила или заблокировала
    test_result = "FAILED"
```

**Почему это работает:**
- Бот публикует от имени пользователя (через StringSession)
- Пользователь МОЖЕТ удалять свои сообщения в любой группе (не нужны права админа)
- Через 60 сек проверяем: жив ли пост? Если да → группа безопасна

---

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

### 2. broadcast_sender.py — обновить отправку тестов с auto-delete

**Добавить функцию для публикации теста с удалением:**

```python
async def send_test_message_with_auto_delete(
    client,  # Telethon client от пользователя
    chat_id: int,
    test_message: str = "🧪 TEST MESSAGE",
    delete_after_seconds: int = 60,
) -> dict:
    """
    Отправить тестовое сообщение и автоматически удалить через N секунд.
    
    Возвращает: {
        "sent": True/False,
        "message_id": int,
        "deleted": True/False,
        "was_already_deleted": True/False  # Если группа удалила раньше нас
    }
    """
    try:
        # 1. Отправляем тестовое сообщение от имени пользователя
        message = await client.send_message(chat_id=chat_id, message=test_message)
        message_id = message.id
        
        # 2. Ждем N секунд (проверяем: удалила ли группа сообщение)
        await asyncio.sleep(delete_after_seconds)
        
        # 3. Пытаемся удалить сообщение от имени пользователя
        try:
            await client.delete_messages(chat_id=chat_id, message_ids=[message_id])
            return {
                "sent": True,
                "message_id": message_id,
                "deleted": True,
                "was_already_deleted": False,
                "status": "PASSED"  # Группа не удаляла = безопасна
            }
        except errors.MessageDeleteForbiddenError:
            # Сообщение уже было удалено группой/админом
            return {
                "sent": True,
                "message_id": message_id,
                "deleted": False,
                "was_already_deleted": True,
                "status": "FAILED"  # Группа удалила = опасна
            }
    
    except Exception as e:
        return {
            "sent": False,
            "message_id": None,
            "deleted": False,
            "error": str(e),
            "status": "ERROR"
        }
```

**Использование в тест-обработчике:**

```python
test_result = await send_test_message_with_auto_delete(
    client=user_client,  # Telethon client пользователя
    chat_id=group_id,
    delete_after_seconds=60
)

if test_result["status"] == "PASSED":
    await bot.send_message(user_id, "✅ Группа безопасна!")
elif test_result["status"] == "FAILED":
    await bot.send_message(user_id, "❌ Группа удаляет посты")
```

---

### 3. main.py — обновить broadcast_test_v2() handler

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
# Auto-delete tests
TEST_MESSAGE_AUTO_DELETE_SECONDS = 60  # Удалять тестовое сообщение через N сек

# Rate limiting
COOLDOWN_SECONDS = 30                  # Cooldown между кликами на тест
MAX_TESTS_PER_DAY = 5                  # Макс тестов в день для обычных пользователей
OWNER_UNLIMITED = True                 # OWNER_ID может тестировать бесконечно
```

---

## 📝 Файлы для изменения

1. **`parser/broadcast_sender.py`** — добавить функцию `send_test_message_with_auto_delete()` 
   - Отправляет тест от имени пользователя (Telethon)
   - Удаляет через 60 сек
   - Возвращает результат: PASSED/FAILED/ERROR

2. **`bot/broadcast_manager.py`** — добавить методы `can_run_test()`, `record_test_run()`
   - Rate limiting: cooldown 30 сек
   - Дневной лимит: max 5 тестов/день
   - OWNER_ID = бесконечные тесты

3. **`bot/main.py`** — обновить `broadcast_test_v2()` handler
   - Вызвать проверку `can_run_test()`
   - Запустить тест через broadcast_sender
   - Записать результат через `record_test_run()`

---

## 📋 Предусловия для Фазы 7

- ✅ Фаза 5 закончена (работает тестирование broadcast_sender.py)
- ✅ Фаза 6 закончена (уведомления о балансе)
- ✅ broadcast_sender.py работает и публикует в группы через Telethon

---

## 🎯 Результат

После этой фазы:
- ✅ **Auto-delete тестовых сообщений через 60 сек** (через Telethon, не видно в группе)
- ✅ Защита от спама тестов (cooldown 30 сек)
- ✅ Лимит 5 тестов в день для обычных пользователей
- ✅ OWNER_ID может тестировать бесконечно (разработчик)
- ✅ Логирование подозрительной активности
- ✅ Пользователь видит понятные сообщения об ограничениях

**Механизм защиты от абьюза:**
1. Пользователь нажимает "Тест" → публикуется 🧪 сообщение
2. Через 60 сек → бот удаляет его от имени пользователя (Telethon)
3. Пользователь видит результат: жив ли пост = группа безопасна или нет
4. **Спам невозможен** — каждый тест удаляется автоматически
