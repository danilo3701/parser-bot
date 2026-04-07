"""
Мониторинг публичных Telegram-групп на ключевые слова.

Установка:
    pip install telethon python-dotenv

Запуск:
    python telegram_keyword_monitor.py
    (при первом запуске попросит код подтверждения из Telegram)
"""

import os
import re
import asyncio
import logging
from datetime import datetime

from dotenv import load_dotenv
from telethon import TelegramClient, events

# ─── Конфигурация ────────────────────────────────────────────────────────────

load_dotenv()

API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
PHONE = os.getenv("TG_PHONE", "")
NOTIFY_CHAT = os.getenv("TG_NOTIFY_CHAT", "me")

# ══════════════════════════════════════════════════════════════════════════════
# ГРУППЫ ДЛЯ МОНИТОРИНГА — впиши сюда username групп (без @)
# Найти группы можно на tgstat.ru или поиском в Telegram
# ══════════════════════════════════════════════════════════════════════════════
GROUPS = [
    "spain_chatss",
    "spain_granitsa",
    "UkrEspana",
    "Ukr_Malaga_Valencia_Marbelia",
    "ukrainci_v_madridi",
    "ucrainci_v_Ispanii",
    "UkraineSpainMIR",
    "amigosalicanteucrania",
    "costablanca_es",
    "uaalicante",
    "servicio24",
    "es_ruso",
    "visadesp",
    "ValenciaUtil",
    "mibarcelona",
    "Barcelona_chatik",
    "espanolukraine",
    "Ukrainci_Benidorm_Alicante",
    "Malaga_Marbella_Spain",
    "moyavalencia",
    "valenciarusia",
]

# ══════════════════════════════════════════════════════════════════════════════
# КЛЮЧЕВЫЕ СЛОВА — бот будет искать эти слова в сообщениях
# ══════════════════════════════════════════════════════════════════════════════
KEYWORDS = [
    "репетитор",
    "ученики",
    "испанский",
    "испанского",
    "español",
    "spanish tutor",
    "ищу репетитора",
    "преподаватель испанского",
]

# ─── Логирование ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── Компилируем паттерн ─────────────────────────────────────────────────────

_kw_pattern = re.compile(
    "|".join(re.escape(kw) for kw in KEYWORDS),
    re.IGNORECASE,
)


def matches_keywords(text: str) -> list[str]:
    if not text:
        return []
    return list(set(_kw_pattern.findall(text)))


# ─── Основная логика ─────────────────────────────────────────────────────────

async def main():
    client = TelegramClient("tutor_monitor_session", API_ID, API_HASH)

    @client.on(events.NewMessage(chats=GROUPS))
    async def on_new_message(event: events.NewMessage.Event):
        text = event.raw_text or ""
        found = matches_keywords(text)
        if not found:
            return

        chat = await event.get_chat()
        sender = await event.get_sender()
        chat_title = getattr(chat, "title", str(chat.id))
        sender_name = ""
        if sender:
            sender_name = getattr(sender, "first_name", "") or ""
            if getattr(sender, "last_name", ""):
                sender_name += f" {sender.last_name}"
            if getattr(sender, "username", ""):
                sender_name += f" (@{sender.username})"

        msg_link = ""
        if getattr(chat, "username", None) and event.id:
            msg_link = f"https://t.me/{chat.username}/{event.id}"

        notification = (
            f"🔍 Найдено совпадение!\n"
            f"📌 Группа: {chat_title}\n"
            f"👤 Автор: {sender_name}\n"
            f"🔑 Ключевые слова: {', '.join(found)}\n"
            f"🔗 Ссылка: {msg_link}\n"
            f"🕐 Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"---\n"
            f"{text[:500]}"
        )

        log.info(f"Совпадение в '{chat_title}': {found}")

        try:
            await client.send_message(NOTIFY_CHAT, notification)
        except Exception as e:
            log.error(f"Не удалось отправить уведомление: {e}")

    await client.start(phone=PHONE)
    me = await client.get_me()
    log.info(f"Авторизован как: {me.first_name} ({me.phone})")

    if not GROUPS:
        log.error("Список GROUPS пуст! Добавь группы в скрипт.")
        log.error("Как найти группы: зайди на tgstat.ru и поищи по теме.")
        return

    joined = []
    for group in GROUPS:
        try:
            entity = await client.get_entity(group)
            title = getattr(entity, "title", group)
            joined.append(title)
            log.info(f"  ✅ Мониторинг: {title}")
        except Exception as e:
            log.warning(f"  ❌ Не удалось подключиться к '{group}': {e}")

    if not joined:
        log.error("Нет доступных групп! Проверь список GROUPS.")
        return

    log.info(f"Ключевые слова: {KEYWORDS}")
    log.info(f"Уведомления -> Избранное (Saved Messages)")
    log.info("Мониторинг запущен. Ctrl+C для остановки.")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
