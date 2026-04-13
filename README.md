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

## Railway / Hosting (ENV variables)

## Railway: Persistent Storage (важно)

Railway при redeploy обычно поднимает новый контейнер, и локальные файлы внутри контейнера могут не сохраняться.
Если состояние бота хранится в JSON-файлах (посты рассылки, настройки, группы, категории, стоп-слова, user_data), то без Persistent Volume оно будет сбрасываться при redeploy.

В этом репозитории volume уже описан в `railway.toml`:
- mountPath: `/data`
- name: `bot-data`

Код бота автоматически использует `/data` для хранения состояния, если директория существует (или если задан `DATA_DIR`/`BOT_DATA_DIR`/`RAILWAY_VOLUME_MOUNT_PATH`).

Рекомендуемые ENV в Railway:
```
TG_SESSION_PATH=/data/tutor_bot_scan.session
# опционально, если хотите явно задать директории:
DATA_DIR=/data
USER_DATA_DIR=/data/user_data
```

Если бот развёрнут на Railway (или другом хостинге), значения из `bot/.env` **не подхватываются автоматически** — их нужно добавить в переменные окружения проекта.

Минимальный набор для работы бота + рассылки (Telethon):

```
BOT_TOKEN=...
OWNER_IDS=...            # ваш Telegram user_id (можно несколько через запятую)
RESULTS_CHANNEL=...      # куда слать результаты сканирования (ID канала/чата)

TG_API_ID=...
TG_API_HASH=...
TG_PHONE=...             # +79990001122
# TG_PASSWORD=...        # если включена 2FA (обязательно при 2FA)
```

Опционально (если используете):
```
BROADCAST_TZ=Europe/Madrid
SOURCE_HEADER_CHANNEL_ID=...
BROADCAST_STORAGE_CHANNEL=@your_channel
TG_SESSION_PATH=/data/tutor_bot_scan.session
```

Важно про `TG_ACCOUNTS`:
- Если не нужно несколько аккаунтов для рассылки — **не задавайте `TG_ACCOUNTS`** (или очистите), используйте `TG_API_ID/TG_API_HASH/TG_PHONE`.
- Если нужно несколько аккаунтов — задайте `TG_ACCOUNTS` в формате `alias:api_id:api_hash:+phone[:password]` (несколько записей через запятую).

После добавления/изменения переменных окружения перезапустите сервис на Railway. При первом запуске Telethon попросит код подтверждения и создаст `.session` файл.

Результаты приходят в Telegram Избранное (Saved Messages).
