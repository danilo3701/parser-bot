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



URL_RE = re.compile(r"(https?://|t\.me/)", re.IGNORECASE)


HARD_PERMISSION_ERRORS = (
    telethon.errors.ChatWriteForbiddenError,
    telethon.errors.ChatAdminRequiredError,
    telethon.errors.UserNotParticipantError,
    telethon.errors.UserBannedInChannelError,
    telethon.errors.ChannelPrivateError,
    telethon.errors.ChatRestrictedError,
)


def _normalize_reason(exc: Exception) -> str:
    if isinstance(exc, telethon.errors.UserNotParticipantError):
        return "not_participant"
    if isinstance(exc, (telethon.errors.ChatWriteForbiddenError, telethon.errors.ChatRestrictedError)):
        return "restricted"
    if isinstance(exc, (telethon.errors.ChatAdminRequiredError, telethon.errors.UserBannedInChannelError)):
        return "admin_required"
    if isinstance(exc, telethon.errors.ChannelPrivateError):
        return "resolve_failed"
    if isinstance(exc, telethon.errors.PeerFloodError):
        return "peer_flood"
    if isinstance(exc, telethon.errors.FloodWaitError):
        return "flood_wait"
    return "other"


def _is_hard_permission_reason(reason: str) -> bool:
    return reason in {"not_participant", "restricted", "admin_required", "resolve_failed"}


def _as_entity_ref(value: str | int | None):
    """
    Best-effort conversion of a group/channel reference into something Telethon can resolve.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"-?\d{5,}", s):
        try:
            return int(s)
        except Exception:
            return s
    return s


async def send_broadcast_campaign_with_client(
    *,
    client,
    groups: list[str],
    source_channel: str | int,
    source_message_id: int,
    send_as_channel: str | None = None,
    delay_seconds: float = 5.0,
    jitter_seconds: float = 1.0,
    as_copy: bool = True,
    is_test: bool = False,
    test_marker: str = "🧪",
) -> dict:
    """
    Sends campaign using an already connected/authorized Telethon client.
    Uses forward as copy by default to avoid "Forwarded from ..." header.
    """
    result = {
        "ok": False,
        "matched_groups": len(groups),
        "sent_count": 0,
        "skipped_count": 0,
        "blocked_groups": {},
        "skipped_groups": {},
        "failed_groups": {},
        "sent_message_ids": {},
        "send_errors": {},
        "summary": "",
        "account": "connected",
    }

    try:
        # Warm up dialogs cache to help resolving numeric ids (private chats)
        try:
            await client.get_dialogs(limit=200)
        except Exception:
            pass

        source_entity = await client.get_entity(_as_entity_ref(source_channel))
        source_message = await client.get_messages(source_entity, ids=int(source_message_id))
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
                send_as_entity = await client.get_entity(_as_entity_ref(send_as_channel))
            except Exception:
                send_as_entity = None
    except Exception as exc:
        result["summary"] = f"Ошибка подготовки рассылки: {type(exc).__name__}"
        return result

    for idx, group in enumerate(groups):
        try:
            group_entity = await client.get_entity(_as_entity_ref(group))
        except Exception as exc:
            result["failed_groups"][group] = f"resolve_failed: {type(exc).__name__}"
            result["send_errors"][group] = "resolve_failed"
            result["skipped_count"] += 1
            continue

        sent = False
        sent_message_id = None
        try:
            if is_test:
                kwargs = {"link_preview": False}
                if send_as_entity is not None:
                    kwargs["send_as"] = send_as_entity
                sent_msg = await client.send_message(
                    group_entity,
                    f"{test_marker} Тестовое сообщение. Проверка доступа.",
                    **kwargs,
                )
                sent = True
                sent_message_id = getattr(sent_msg, "id", None)
            elif as_copy:
                kwargs = {"as_copy": True}
                if send_as_entity is not None:
                    kwargs["send_as"] = send_as_entity
                try:
                    forwarded = await client.forward_messages(
                        entity=group_entity,
                        messages=[source_message_id],
                        from_peer=source_entity,
                        **kwargs,
                    )
                except TypeError:
                    # Older Telethon builds may not support send_as/as_copy in forward_messages
                    forwarded = await client.forward_messages(
                        entity=group_entity,
                        messages=[source_message_id],
                        from_peer=source_entity,
                        as_copy=True,
                    )
                if isinstance(forwarded, list) and forwarded:
                    sent_message_id = getattr(forwarded[0], "id", None)
                else:
                    sent_message_id = getattr(forwarded, "id", None)
                sent = True
            else:
                kwargs = {"link_preview": False}
                if send_as_entity is not None:
                    kwargs["send_as"] = send_as_entity
                sent_msg = await client.send_message(group_entity, source_message, **kwargs)
                sent = True
                sent_message_id = getattr(sent_msg, "id", None)
        except telethon.errors.FloodWaitError as exc:
            await asyncio.sleep(exc.seconds + 1)
            try:
                if is_test:
                    kwargs = {"link_preview": False}
                    if send_as_entity is not None:
                        kwargs["send_as"] = send_as_entity
                    sent_msg = await client.send_message(
                        group_entity,
                        f"{test_marker} Тестовое сообщение. Проверка доступа.",
                        **kwargs,
                    )
                    sent = True
                    sent_message_id = getattr(sent_msg, "id", None)
                else:
                    kwargs = {"as_copy": True}
                    if send_as_entity is not None:
                        kwargs["send_as"] = send_as_entity
                    try:
                        forwarded = await client.forward_messages(
                            entity=group_entity,
                            messages=[source_message_id],
                            from_peer=source_entity,
                            **kwargs,
                        )
                    except TypeError:
                        # Older Telethon builds may not support send_as/as_copy in forward_messages
                        forwarded = await client.forward_messages(
                            entity=group_entity,
                            messages=[source_message_id],
                            from_peer=source_entity,
                            as_copy=True,
                        )
                    if isinstance(forwarded, list) and forwarded:
                        sent_message_id = getattr(forwarded[0], "id", None)
                    else:
                        sent_message_id = getattr(forwarded, "id", None)
                    sent = True
            except Exception as retry_exc:
                reason = _normalize_reason(retry_exc)
                if _is_hard_permission_reason(reason):
                    result["blocked_groups"][group] = type(retry_exc).__name__
                else:
                    result["failed_groups"][group] = type(retry_exc).__name__
                result["send_errors"][group] = reason
        except Exception as exc:
            reason = _normalize_reason(exc)
            if _is_hard_permission_reason(reason):
                result["blocked_groups"][group] = type(exc).__name__
            else:
                result["failed_groups"][group] = type(exc).__name__
            result["send_errors"][group] = reason

        if sent:
            result["sent_count"] += 1
            if isinstance(sent_message_id, int):
                result["sent_message_ids"][group] = sent_message_id
        else:
            result["skipped_count"] += 1
            result["send_errors"].setdefault(group, "other")

        if idx < len(groups) - 1:
            await asyncio.sleep(delay_seconds + random.uniform(0, jitter_seconds))

    result["ok"] = result["sent_count"] > 0
    result["summary"] = (
        f"Групп: {result['matched_groups']} | "
        f"Отправлено: {result['sent_count']} | "
        f"Пропущено: {result['skipped_count']} | "
        f"Автоблок: {len(result['blocked_groups'])}"
    )
    return result


async def verify_and_delete_test_messages(
    *,
    client,
    test_message_ids: dict[str, int],
    wait_seconds: int = 60,
) -> dict[str, dict[str, bool | str]]:
    """
    Wait, verify that test messages are still present, then try to delete them.

    Returns: {group: {"found": bool, "deleted": bool, ...optional error fields...}}
    """
    if wait_seconds and wait_seconds > 0:
        await asyncio.sleep(wait_seconds)

    delete_forbidden_exc = getattr(telethon.errors, "MessageDeleteForbiddenError", None)

    results: dict[str, dict[str, bool | str]] = {}
    for group, msg_id in (test_message_ids or {}).items():
        found = False
        deleted = False
        delete_error = ""
        verify_error = ""
        try:
            entity = await client.get_entity(_as_entity_ref(group))
            message = await client.get_messages(entity, ids=int(msg_id))
            if isinstance(message, list):
                message = message[0] if message else None
            if message:
                found = True
                try:
                    await client.delete_messages(entity, [int(msg_id)])
                    deleted = True
                except Exception as exc:
                    if delete_forbidden_exc is not None and isinstance(exc, delete_forbidden_exc):
                        deleted = False
                    else:
                        deleted = False
                        delete_error = type(exc).__name__
        except Exception:
            found = False
            deleted = False
            verify_error = "resolve_or_fetch_failed"

        row: dict[str, bool | str] = {"found": bool(found), "deleted": bool(deleted)}
        if delete_error:
            row["delete_error"] = delete_error
        if verify_error:
            row["verify_error"] = verify_error
        results[str(group)] = row

    return results
