# Фаза 2: Stripe интеграция + обработка платежей

## Контекст
Подключение реального платежного сервиса Stripe для продажи тарифов. Webhook обновляет баланс пользователя при успешной оплате.

---

## Архитектура

### Где искать:
- `bot/.env` — новые переменные STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET
- `bot/stripe_handler.py` — новый файл для обработки платежей
- `bot/main.py` — callback обработчики для кнопок покупки

### Что меняется:

**Новые переменные в .env:**
```
STRIPE_PUBLIC_KEY=pk_test_...
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
BOT_WEBHOOK_URL=https://railway-url.app/stripe-webhook
```

**Новые price_id в коде (constants):**
```python
STRIPE_PRICES = {
    "trial": {
        "price_id": "price_1234567890",
        "posts": 80,
        "price_eur": 5.00,
    },
    "starter": {
        "price_id": "price_0987654321",
        "posts": 250,
        "price_eur": 12.00,
    },
    "optimal": {
        "price_id": "price_abcdefghij",
        "posts": 600,
        "price_eur": 20.00,
    },
    "wholesale": {
        "price_id": "price_xyz789123",
        "posts": 1500,
        "price_eur": 40.00,
    },
}
```

---

## 🎨 Визуальный дизайн

### 1. Нажатие на "Купить тариф"

**После выбора тарифа:**
```
✅ Редирект на Stripe Checkout
↓
https://checkout.stripe.com/pay/cs_live_abc123def456...

Пользователь видит Stripe форму:
- Выбранный пакет (например, "600 постов / €20")
- Поле Email
- Данные карты
- [Заплатить €20]
```

### 2. После успешной оплаты

```
✅ СПАСИБО ЗА ОПЛАТУ!

Вы купили: 600 постов

Баланс обновлён автоматически.
Вернитесь в бот и начните рассылку!

[Вернуться в бот]
```

### 3. После отмены платежа

```
❌ Платёж отменён

Ваш баланс не изменился.
Попробуйте ещё раз или выберите другой тариф.

[Вернуться в бот]
```

---

## 💻 Бэкэнд логика

### 1. Новый файл: bot/stripe_handler.py

```python
import stripe
from datetime import datetime, timezone
import os
import logging

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

logger = logging.getLogger(__name__)

STRIPE_PRICES = {
    "trial": {"price_id": "price_...", "posts": 80},
    "starter": {"price_id": "price_...", "posts": 250},
    "optimal": {"price_id": "price_...", "posts": 600},
    "wholesale": {"price_id": "price_...", "posts": 1500},
}

async def create_checkout_session(user_id: int, tier: str) -> str:
    """
    Создать сессию оплаты Stripe.
    Вернёт URL для редиректа пользователя.
    """
    if tier not in STRIPE_PRICES:
        raise ValueError(f"Unknown tier: {tier}")
    
    price_id = STRIPE_PRICES[tier]["price_id"]
    
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="payment",
        line_items=[{"price": price_id, "quantity": 1}],
        client_reference_id=str(user_id),
        success_url="https://your-railway-url.app/stripe-success?session_id={CHECKOUT_SESSION_ID}",
        cancel_url="https://your-railway-url.app/stripe-cancel",
    )
    
    return session.url

async def process_webhook(payload: bytes, sig_header: str) -> dict:
    """
    Обработать Stripe webhook.
    Проверяет подпись, обновляет баланс пользователя.
    """
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        return {"error": "Invalid payload"}
    except stripe.error.SignatureVerificationError:
        return {"error": "Invalid signature"}
    
    if event["type"] == "payment_intent.succeeded":
        payment_intent = event["data"]["object"]
        user_id = int(payment_intent.get("client_reference_id", 0))
        
        tier = payment_intent.get("metadata", {}).get("tier")
        
        if user_id and tier and tier in STRIPE_PRICES:
            posts_to_add = STRIPE_PRICES[tier]["posts"]
            
            # Обновить баланс (будет реализовано в main.py)
            logger.info(f"Payment confirmed for user {user_id}, adding {posts_to_add} posts")
            
            return {"status": "ok", "user_id": user_id, "posts": posts_to_add}
    
    return {"status": "received"}
```

### 2. main.py — обновить обработчики покупки

**Заменить stub'ы в handlers:**

```python
@dp.callback_query(F.data == "buy_trial")
async def buy_trial(query: CallbackQuery):
    await handle_purchase(query, "trial")

@dp.callback_query(F.data == "buy_starter")
async def buy_starter(query: CallbackQuery):
    await handle_purchase(query, "starter")

@dp.callback_query(F.data == "buy_optimal")
async def buy_optimal(query: CallbackQuery):
    await handle_purchase(query, "optimal")

@dp.callback_query(F.data == "buy_wholesale")
async def buy_wholesale(query: CallbackQuery):
    await handle_purchase(query, "wholesale")

async def handle_purchase(query: CallbackQuery, tier: str):
    """Создать сессию Stripe и отправить ссылку пользователю"""
    try:
        checkout_url = await create_checkout_session(query.from_user.id, tier)
        
        text = f"🛒 Переходите к оплате {tier.upper()}\n\nНажмите кнопку ниже:"
        
        rows = [
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=checkout_url)],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="bc_tariffs")],
        ]
        
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception as e:
        await query.answer(f"❌ Ошибка: {str(e)}", show_alert=True)
```

### 3. requirements.txt

**Добавить:**
```
stripe>=5.0.0
```

---

## 🔍 Проверка (Verification)

### Что тестировать:
1. Нажать "Купить тариф" → выбрать "Оптимальный €20"
2. Откроется Stripe Checkout форма
3. Ввести тестовые данные карты (Stripe predefined):
   - `4242 4242 4242 4242` — успешный платёж
   - `4000 0000 0000 0002` — отклонённый платёж
4. Заплатить → success page
5. Webhook должен обновить баланс

---

## 📝 Файлы для изменения / создания

1. **Создать:** `bot/stripe_handler.py` — логика платежей
2. **Изменить:** `bot/main.py` — callback'и для кнопок покупки
3. **Изменить:** `bot/.env.example` — добавить STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET
4. **Изменить:** `requirements.txt` — добавить stripe

---

## 📋 Предусловия для Фазы 2

- ✅ Фаза 1 закончена (баланс работает)
- ⚠️ Stripe аккаунт создан, тарифы добавлены как products
- ⚠️ price_id скопированы из Stripe dashboard

---

## 🎯 Результат

После этой фазы:
- ✅ Реальные платежи через Stripe
- ✅ Баланс обновляется через webhook
- ✅ Пользователь вижит Stripe Checkout форму
