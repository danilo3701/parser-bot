import os
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient

root = Path(r"C:/Users/DEDE/Desktop/tutor_finder_bot")
load_dotenv(root / "bot" / ".env")

api_id = int(os.getenv("TG_API_ID", "0"))
api_hash = os.getenv("TG_API_HASH", "")
phone = os.getenv("TG_PHONE", "")
session = str(root / "tutor_bot_scan.session")

async def main():
    client = TelegramClient(session, api_id, api_hash)
    await client.start(phone=phone)
    me = await client.get_me()
    print("OK AUTH:", me.id, me.username, me.phone)
    await client.disconnect()

asyncio.run(main())
