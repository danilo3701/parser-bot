"""
Сканирование истории публичных Telegram-групп на ключевые слова.
Находит ВСЕ сообщения за последние N дней, которые содержат нужные слова.
Результаты сохраняются в CSV + отправляются в Избранное.

Установка:
    pip install telethon python-dotenv

Запуск:
    python telegram_history_scan.py
"""

import os
import re
import asyncio
import csv
from datetime import datetime, timedelta

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
PHONE = os.getenv("TG_PHONE", "")
NOTIFY_CHAT = os.getenv("TG_NOTIFY_CHAT", "me")

# ══════════════════════════════════════════════════════════════════════════════
# ГРУППЫ ДЛЯ СКАНИРОВАНИЯ — впиши username групп (без @)
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
# КЛЮЧЕВЫЕ СЛОВА
# ══════════════════════════════════════════════════════════════════════════════
KEYWORDS = [
    "репетитор",
    "ученики",
    "испанский",
    "испанского",
    "español",
    "ищу репетитора",
    "преподаватель испанского",
]

# За сколько дней сканировать
DAYS_BACK = 30

# Максимум сообщений на группу
MAX_MESSAGES_PER_GROUP = 10000

# Задержка (секунды) — защита от бана
REQUEST_DELAY = 1.0

_kw_pattern = re.compile(
    "|".join(re.escape(kw) for kw in KEYWORDS),
    re.IGNORECASE,
)


async def scan_group(client, group: str) -> list[dict]:
    results = []
    try:
        entity = await client.get_entity(group)
    except Exception as e:
        print(f"  ❌ Не удалось получить группу '{group}': {e}")
        return results

    title = getattr(entity, "title", group)
    username = getattr(entity, "username", None)
    print(f"  📡 Сканирование: {title} ...")

    min_date = datetime.now() - timedelta(days=DAYS_BACK)
    count = 0

    async for message in client.iter_messages(
        entity,
        limit=MAX_MESSAGES_PER_GROUP,
    ):
        if message.date.replace(tzinfo=None) < min_date:
            break

        count += 1
        text = message.raw_text or ""
        found = list(set(_kw_pattern.findall(text)))
        if not found:
            continue

        sender = await message.get_sender()
        sender_name = ""
        sender_username = ""
        sender_id = None
        if sender:
            sender_name = getattr(sender, "first_name", "") or ""
            if getattr(sender, "last_name", ""):
                sender_name += f" {sender.last_name}"
            sender_username = getattr(sender, "username", "") or ""
            sender_id = getattr(sender, "id", None)

        msg_link = ""
        if username and message.id:
            msg_link = f"https://t.me/{username}/{message.id}"

        results.append({
            "group": title,
            "date": message.date.strftime("%Y-%m-%d %H:%M"),
            "sender": sender_name,
            "username": sender_username,
            "user_id": sender_id,
            "keywords": ", ".join(found),
            "link": msg_link,
            "text": text[:300].replace("\n", " "),
        })

        if count % 100 == 0:
            await asyncio.sleep(REQUEST_DELAY)

    print(f"    ✅ Просмотрено: {count} сообщений, совпадений: {len(results)}")
    return results


async def main():
    client = TelegramClient("tutor_scan_session", API_ID, API_HASH)
    await client.start(phone=PHONE)
    me = await client.get_me()
    print(f"Авторизован: {me.first_name} ({me.phone})")
    print(f"Период: последние {DAYS_BACK} дней")
    print(f"Ключевые слова: {KEYWORDS}\n")

    if not GROUPS:
        print("❌ Список GROUPS пуст! Добавь группы в скрипт.")
        print("Как найти: зайди на tgstat.ru, поищи по теме.")
        return

    all_results = []
    for group in GROUPS:
        results = await scan_group(client, group)
        all_results.extend(results)
        await asyncio.sleep(REQUEST_DELAY)

    # Сохраняем в CSV
    if all_results:
        output_file = "tutor_scan_results.csv"
        with open(output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=all_results[0].keys())
            writer.writeheader()
            writer.writerows(all_results)
        print(f"\n🎯 Найдено: {len(all_results)} совпадений")
        print(f"📄 Результаты сохранены в: {output_file}")

        # Отправляем сводку в Избранное
        summary = f"📊 Результаты сканирования\n"
        summary += f"Групп: {len(GROUPS)}\n"
        summary += f"Совпадений: {len(all_results)}\n\n"

        for r in all_results[:50]:  # Первые 50 результатов
            contact_info = ""
            if r['username']:
                contact_info = f" (@{r['username']})"
            elif r.get('user_id'):
                contact_info = f" (ID: {r['user_id']})"

            summary += (
                f"👤 {r['sender']}{contact_info}"
                + f"\n📌 {r['group']} | {r['date']}"
                + f"\n🔑 {r['keywords']}"
                + (f"\n🔗 {r['link']}" if r['link'] else "")
                + f"\n💬 {r['text'][:150]}"
                + f"\n{'─' * 30}\n"
            )

        if len(all_results) > 50:
            summary += f"\n... и ещё {len(all_results) - 50} результатов (см. CSV файл)"

        try:
            await client.send_message(NOTIFY_CHAT, summary)
            print("📨 Сводка отправлена в Избранное!")
        except Exception as e:
            print(f"Не удалось отправить сводку: {e}")
    else:
        print("\n😕 Совпадений не найдено.")


if __name__ == "__main__":
    asyncio.run(main())
