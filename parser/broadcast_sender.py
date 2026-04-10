import asyncio
import os
import random
import re
from pathlib import Path

import telethon.errors
from dotenv import load_dotenv
from telethon import TelegramClient


load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
# Подхватываем и основной .env (в репозитории он в папке bot)
load_dotenv(os.path.join(Path(__file__).resolve().parent.parent, "bot", ".env"))


def _parse_accounts(raw: str) -> dict:
    """Парсит TG_ACCOUNTS в формат alias -> creds dict."""
    accounts = {}
    if not raw:
        return accounts
    for chunk in re.split(r"[,\n;]+", raw):
        item = chunk.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) < 4:
            continue
        alias, api_id, api_hash, phone, *rest = parts
        alias = alias.strip()
        if not re.match(r"^[A-Za-z0-9_]{2,32}$", alias):
            continue
        try:
            api_id_int = int(api_id)
        except ValueError:
            continue
        password = rest[0].strip() if rest else None
        accounts[alias] = {
            "api_id": api_id_int,
            "api_hash": api_hash.strip(),
            "phone": phone.strip(),
            "password": password or None,
        }
    return accounts


ACCOUNTS = _parse_accounts(os.getenv("TG_ACCOUNTS", ""))
DEFAULT_API_ID = int(os.getenv("TG_API_ID", "0"))
DEFAULT_API_HASH = os.getenv("TG_API_HASH", "")
DEFAULT_PHONE = os.getenv("TG_PHONE", "")
DEFAULT_PASSWORD = os.getenv("TG_PASSWORD", "") or None

URL_RE = re.compile(r"(https?://|t\.me/)", re.IGNORECASE)


HARD_PERMISSION_ERRORS = (
    telethon.errors.ChatWriteForbiddenError,
    telethon.errors.ChatAdminRequiredError,
    telethon.errors.UserBannedInChannelError,
    telethon.errors.ChannelPrivateError,
    telethon.errors.ChatRestrictedError,
)


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, HARD_PERMISSION_ERRORS):
        return "hard_permission"
    if isinstance(exc, telethon.errors.PeerFloodError):
        return "peer_flood"
    if isinstance(exc, telethon.errors.FloodWaitError):
        return "flood_wait"
    return "other"


def _choose_credentials(account_alias: str | None) -> tuple[dict | None, str | None, str | None]:
    """
    Возвращает креды и имя session-файла.
    Если настроены TG_ACCOUNTS, требует валидный alias.
    """
    if ACCOUNTS:
        if not account_alias:
            return None, None, "Не выбран аккаунт отправки."
        creds = ACCOUNTS.get(account_alias)
        if not creds:
            return None, None, f"Аккаунт '{account_alias}' не найден в TG_ACCOUNTS."
        session_name = f"tutor_bot_broadcast_{account_alias}"
        return creds, session_name, None

    # Фолбек на старую схему с одним аккаунтом
    if not DEFAULT_API_ID or not DEFAULT_API_HASH or not DEFAULT_PHONE:
        return None, None, "Не заданы TG_API_ID/TG_API_HASH/TG_PHONE."
    creds = {
        "api_id": DEFAULT_API_ID,
        "api_hash": DEFAULT_API_HASH,
        "phone": DEFAULT_PHONE,
        "password": DEFAULT_PASSWORD,
    }
    return creds, "tutor_bot_broadcast", None


async def send_broadcast_campaign(
    groups: list[str],
    source_channel: str,
    source_message_id: int,
    send_as_channel: str | None = None,
    account_alias: str | None = None,
    delay_seconds: float = 5.0,
    jitter_seconds: float = 1.0,
) -> dict:
    result = {
        "ok": False,
        "matched_groups": len(groups),
        "sent_count": 0,
        "skipped_count": 0,
        "blocked_groups": {},
        "skipped_groups": {},
        "failed_groups": {},
        "summary": "",
        "account": account_alias or "default",
    }

    if not groups:
        result["summary"] = "Список групп пуст."
        return result

    creds, session_name, err = _choose_credentials(account_alias)
    if err:
        result["summary"] = err
        return result

    client = TelegramClient(session_name, creds["api_id"], creds["api_hash"])
    try:
        await client.start(phone=creds["phone"], password=creds.get("password"))
    except Exception as exc:
        result["summary"] = f"Ошибка авторизации ({account_alias or 'default'}): {exc}"
        return result

    try:
        source_entity = await client.get_entity(source_channel)
        source_message = await client.get_messages(source_entity, ids=source_message_id)
        if not source_message:
            result["summary"] = "Исходный пост не найден."
            return result

        source_text = source_message.raw_text or ""
        if URL_RE.search(source_text):
            result["summary"] = "Исходный пост содержит URL (http/https/t.me). Политика v1 запрещает такие ссылки."
            return result

        send_as_entity = None
        if send_as_channel:
            try:
                send_as_entity = await client.get_entity(send_as_channel)
            except Exception:
                pass
    except Exception as exc:
        result["summary"] = f"Ошибка подготовки рассылки: {exc}"
        return result

    for idx, group in enumerate(groups):
        try:
            group_entity = await client.get_entity(group)
        except Exception as exc:
            result["failed_groups"][group] = f"resolve_failed: {type(exc).__name__}"
            result["skipped_count"] += 1
            continue

        sent = False
        try:
            kwargs = {"link_preview": False}
            if send_as_entity:
                kwargs["send_as"] = send_as_entity
            await client.send_message(group_entity, source_message, **kwargs)
            sent = True
        except telethon.errors.FloodWaitError as exc:
            await asyncio.sleep(exc.seconds + 1)
            try:
                await client.send_message(
                    group_entity,
                    source_message,
                    send_as=send_as_entity,
                    link_preview=False,
                )
                sent = True
            except Exception as retry_exc:
                category = _classify_error(retry_exc)
                if category == "hard_permission":
                    result["blocked_groups"][group] = type(retry_exc).__name__
                else:
                    result["failed_groups"][group] = type(retry_exc).__name__
        except Exception as exc:
            category = _classify_error(exc)
            if category == "hard_permission":
                result["blocked_groups"][group] = type(exc).__name__
            else:
                result["failed_groups"][group] = type(exc).__name__

        if sent:
            result["sent_count"] += 1
        else:
            result["skipped_count"] += 1

        if idx < len(groups) - 1:
            await asyncio.sleep(delay_seconds + random.uniform(0, jitter_seconds))

    await client.disconnect()

    result["ok"] = result["sent_count"] > 0
    result["summary"] = (
        f"Групп: {result['matched_groups']} | "
        f"Отправлено: {result['sent_count']} | "
        f"Пропущено: {result['skipped_count']} | "
        f"Автоблок: {len(result['blocked_groups'])} | "
        f"Аккаунт: {result['account']}"
    )
    return result
