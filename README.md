# Tutor Finder Bot

Telegram бот для поиска репетиторов испанского в испанских Telegram-группах.

## Структура проекта

```
tutor_finder_bot/
├── bot/                    # Код бота (aiogram 3)
│   ├── main.py            # Основной файл бота
│   ├── .env               # Токен бота (ЗАПОЛНИТЬ)
│   └── .env.example       # Пример конфига
├── parser/                # Скрипты парсера (Telethon)
│   ├── telegram_history_scan.py     # Сканирование истории
│   ├── telegram_keyword_monitor.py  # Реалтайм мониторинг
│   └── .env              # API ID, Hash, Phone
└── requirements.txt       # Зависимости
```

## Установка

1. **Скопируй файл конфига:**
```bash
cd bot
cp .env.example .env
```

2. **Заполни `.env` файл:**
```
BOT_TOKEN=твой_токен_от_BotFather
TG_API_ID=твой_api_id
TG_API_HASH=твой_api_hash
TG_PHONE=+твой_номер
```

3. **Установи зависимости:**
```bash
pip install -r requirements.txt
```

4. **Запусти бота:**
```bash
cd bot
python main.py
```

## Использование

В Telegram отправь боту:
- `/start` — информация о боте
- `/scan` — сканировать все группы
- `/monitor` — включить мониторинг
- `/help` — справка

## Парсер

Скрипты парсера находятся в папке `parser/`:

```bash
# Сканирование истории за 30 дней
cd parser
python telegram_history_scan.py

# Реалтайм мониторинг новых сообщений
python telegram_keyword_monitor.py
```

Результаты приходят в Telegram Избранное (Saved Messages).
