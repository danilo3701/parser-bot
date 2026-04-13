# 🛠 Инструкция: Настройка Stripe для бота

## 📊 Наши 4 тарифа (цены):

| Тариф | Постов | Цена (€) | Цена за 1 пост | Скидка |
|-------|--------|----------|-----------------|--------|
| 🟢 Пробный | 80 | €5.00 | €0.0625 | — |
| 🔵 Стартовый | 250 | €12.00 | €0.0480 | 23% |
| 🔥 Оптимальный | 600 | €20.00 | €0.0333 | 47% |
| 🔴 Оптовый | 1500 | €40.00 | €0.0267 | 57% |

---

## ❓ Один продукт или четыре?

### Ответ: **ОДИН ПРОДУКТ с 4 РАЗНЫМИ ЦЕНАМИ**

**Почему?**
- Это один сервис: "Посты для рассылки"
- Разные цены = разные варианты количества
- Как в магазине: одна рубашка, но разные размеры (разные цены)

---

## 🚀 Пошаговая инструкция Stripe

### Шаг 1: Создать продукт

1. Открыть https://dashboard.stripe.com/
2. Левое меню → **Catalog** (или **Products**)
3. Нажать **+ Add product**
4. Заполнить форму:

```
Name: Посты для рассылки
(или на английском: Posts for Broadcasting)

Description (опционально):
"Пакеты постов для автоматической рассылки в Telegram группы.
Платите только за успешные публикации."

Image: (опционально - загрузить картинку 📮)

Type: Standard
```

5. Нажать **Save product**

✅ Продукт создан! Переходим к ценам.

---

### Шаг 2: Добавить 4 цены к одному продукту

Когда создан продукт, откроется его страница.

**На странице продукта:**

#### 2.1 Первая цена (Пробный):

1. Вкладка **Pricing** (должна быть открыта)
2. Кнопка **+ Add price**
3. Заполнить:

```
Billing period: One-time (не подписка!)

Price: 5.00

Currency: EUR (€)

Nickname (внутренний ID): trial_80_posts
(Это для твоей заметки в Stripe)

Lookup key (для API - ВАЖНО!): trial_80_posts
```

4. Нажать **Save price**

> После сохранения Stripe покажет **Price ID** (выглядит так: `price_1Pv...`)
> **СКОПИРУЙ ЭТО ID!** Нам это нужно будет в коде.

#### 2.2 Вторая цена (Стартовый):

Повторить процесс:

```
Price: 12.00
Nickname: starter_250_posts
Lookup key: starter_250_posts
```

#### 2.3 Третья цена (Оптимальный):

```
Price: 20.00
Nickname: optimal_600_posts
Lookup key: optimal_600_posts
```

#### 2.4 Четвёртая цена (Оптовый):

```
Price: 40.00
Nickname: wholesale_1500_posts
Lookup key: wholesale_1500_posts
```

---

## 📋 После создания — скопировать все ID

После создания всех 4 цен скопируй эти ID:

```
Пробный (80 постов / €5):
Price ID: price_... (скопировать из Stripe)

Стартовый (250 постов / €12):
Price ID: price_... (скопировать из Stripe)

Оптимальный (600 постов / €20):
Price ID: price_... (скопировать из Stripe)

Оптовый (1500 постов / €40):
Price ID: price_... (скопировать из Stripe)
```

---

## 🔑 Получить API ключи

### Шаг 3: Получить Secret Key и Webhook Secret

1. Левое меню → **Developers** → **API keys**
2. Вкладка **Secret keys**
3. Под **Standard keys** найти **Secret key**
4. Нажать **Reveal test key** (если ещё не виден)
5. **Скопировать** (выглядит как: `sk_test_...`)

```
STRIPE_SECRET_KEY = sk_test_... (скопировать отсюда)
```

> ⚠️ **ВАЖНО:** Это ключ для разработки (test mode). Для боя нужно переключить на "Live mode" когда запустишь.

---

## 🪝 Webhook для платежей

### Шаг 4: Создать Webhook endpoint

1. Левое меню → **Developers** → **Webhooks**
2. Нажать **+ Add endpoint**
3. Заполнить:

```
Endpoint URL: https://your-railway-url.app/stripe-webhook
(или где-то у тебя размещается бот)

Events to send:
✓ payment_intent.succeeded (самое важное!)
✓ charge.refunded (если понадобится возврат)
```

4. Нажать **Add endpoint**

5. Откроется страница webhook'а. Нажать **Signing secret** → **Reveal**
6. **Скопировать** signing secret (выглядит как: `whsec_...`)

```
STRIPE_WEBHOOK_SECRET = whsec_... (скопировать отсюда)
```

---

## ✅ Итоговый чек-лист

Тебе нужно собрать эти данные и дать мне:

```
=== STRIPE SETUP COMPLETE ===

Product name: Посты для рассылки

Price IDs (4 штуки):
□ Trial (80 posts): price_...
□ Starter (250 posts): price_...
□ Optimal (600 posts): price_...
□ Wholesale (1500 posts): price_...

API Key:
□ STRIPE_SECRET_KEY = sk_test_...

Webhook Secret:
□ STRIPE_WEBHOOK_SECRET = whsec_...

Webhook URL:
□ https://your-railway.app/stripe-webhook
```

Когда соберёшь всё это → дай мне, и я вставлю в код бота.

---

## 💡 Test Mode vs Live Mode

### Сейчас (разработка):
- Stripe в **Test Mode** (переключатель вверху дашборда)
- Используются ключи `sk_test_...` и `pk_test_...`
- Платежи не настоящие (используй тестовые карты)

### Когда запустишь в боевом режиме:
- Переключить на **Live Mode**
- Использовать live ключи `sk_live_...`
- Платежи будут настоящие!

---

## 🧪 Тестовые данные Stripe

Чтобы проверить платежи (на test mode):

**Успешный платёж:**
- Карта: `4242 4242 4242 4242`
- Месяц/год: любой будущий
- CVC: любой трёхзначный код

**Отклонённый платёж:**
- Карта: `4000 0000 0000 0002`
- Остальное: любые данные

---

## 🔗 Полезные ссылки

- Stripe Dashboard: https://dashboard.stripe.com/
- API Documentation: https://stripe.com/docs/api
- Webhook Events: https://stripe.com/docs/api/events

---

## 📝 Структура для кода

Когда будешь готов, вставить в `bot/stripe_handler.py`:

```python
STRIPE_PRICES = {
    "trial": {
        "price_id": "price_...",  # ← вставить сюда
        "posts": 80,
        "price_eur": 5.00,
    },
    "starter": {
        "price_id": "price_...",  # ← вставить сюда
        "posts": 250,
        "price_eur": 12.00,
    },
    "optimal": {
        "price_id": "price_...",  # ← вставить сюда
        "posts": 600,
        "price_eur": 20.00,
    },
    "wholesale": {
        "price_id": "price_...",  # ← вставить сюда
        "posts": 1500,
        "price_eur": 40.00,
    },
}
```

И в `.env`:

```
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
```

---

## 🎯 После завершения

Когда соберёшь все ID и ключи → **дай мне**, и я:
1. ✅ Вставлю в код
2. ✅ Протестирую webhook
3. ✅ Проверю платежи
4. ✅ Подготовлю к запуску на Railway

**Готов начинать? 🚀**
