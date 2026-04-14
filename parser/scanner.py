"""
Модуль для интеграции парсера с ботом.
Экспортирует функции для сканирования и мониторинга.
"""

import os
import re
import sys
import asyncio
import sqlite3
import contextlib
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import telethon.errors
try:
    import snowballstemmer
except ImportError:
    snowballstemmer = None

sys.path.insert(0, str(Path(__file__).parent.parent / "bot"))
from groups_manager import load_groups
from dedupe_store import DedupeStore

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


# ─── Custom Exceptions ───────────────────────────────────────────────────────

class ScannerNeedsAuthError(Exception):
    """Raised when scanner needs user to enter Telegram auth code via bot."""
    def __init__(self, phone: str):
        self.phone = phone
        super().__init__(f"Scanner needs auth code for {phone}")

API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
PHONE = os.getenv("TG_PHONE", "")
PASSWORD = os.getenv("TG_PASSWORD", "") or None
# Явный путь к Telethon session; по умолчанию — внутри parser/
SESSION_PATH = Path(os.getenv("TG_SESSION_PATH", Path(__file__).parent / "tutor_bot_scan.session")).resolve()

# Ключевые слова загружаются динамически из categories.json
KEYWORDS = []  # Будет заполнено при сканировании
_kw_pattern = None  # Будет скомпилирован при сканировании
_kw_stem_sequences: list[list[str]] = []  # стем-последовательности для ключей

# Стоп-слова (анти-ключевые слова)
ANTI_KEYWORDS = []  # Будет заполнено при сканировании
_anti_kw_pattern = None  # Будет скомпилирован при сканировании

# Русские предлоги для игнорирования при поиске (для/на/в считаются одним и тем же)
STOP_WORDS_RU = {"для", "на", "в"}


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[\\w]+", text.lower())


def _stem_words(words: list[str]) -> list[str]:
    if snowballstemmer:
        stemmer = snowballstemmer.RussianStemmer()
        return stemmer.stemWords(words)
    return [w.lower() for w in words]


def _compile_keywords(keywords: list):
    """Компилирует паттерны и стем-последовательности для ключевых слов."""
    global _kw_pattern, _kw_stem_sequences
    cleaned: list[str] = []
    for kw in keywords:
        if not kw:
            continue
        kw = kw.strip()
        if not kw:
            continue
        cleaned.append(kw)

    # dedup while preserving order
    seen = set()
    unique = []
    for kw in cleaned:
        if kw.lower() in seen:
            continue
        seen.add(kw.lower())
        unique.append(kw)

    if not unique:
        _kw_pattern = None
        _kw_stem_sequences = []
        return

    parts = []
    stem_sequences: list[list[str]] = []
    for kw in unique:
        tokens = _tokenize(kw)
        stems = _stem_words(tokens)
        # Убираем русские предлоги (для/на/в считаются одним и тем же)
        stems = [s for s in stems if s not in STOP_WORDS_RU]
        stem_sequences.append(stems)

        if " " in kw:
            parts.append(re.escape(kw))
        else:
            parts.append(rf"(?<!\\w){re.escape(kw)}(?!\\w)")

    _kw_stem_sequences = stem_sequences
    _kw_pattern = re.compile("|".join(parts), re.IGNORECASE) if parts else None


def _compile_anti_keywords(anti_keywords: list):
    """Компилирует паттерн из стоп-слов"""
    global _anti_kw_pattern
    if not anti_keywords:
        _anti_kw_pattern = None
        return
    _anti_kw_pattern = re.compile(
        "|".join(re.escape(kw) for kw in anti_keywords),
        re.IGNORECASE,
    )


async def scan_groups_history(
    days: int = 30,
    max_messages: int = 10000,
    keywords: list = None,
    anti_keywords: list = None,
    results_channel: int = None,
    include_source_header: bool = False,
    session_path: Path | str | None = None,
    session_string: str = "",
) -> tuple:
    """
    Сканирует историю групп за N дней.
    Найденные сообщения сразу пересылаются в results_channel.

    Возвращает: (total_count, processed_groups, skipped_groups)
    """
    if keywords is None:
        keywords = KEYWORDS
    if anti_keywords is None:
        anti_keywords = ANTI_KEYWORDS

    _compile_keywords(keywords)
    if _kw_pattern is None and not _kw_stem_sequences:
        return 0, 0, "Нет ключевых слов для сканирования"
    _compile_anti_keywords(anti_keywords)

    groups = load_groups()
    # Дедуп только на время текущего сканирования (в памяти, без файла)
    dedupe_store = DedupeStore(None, ttl_seconds=None)

    # Clear legacy author_day: keys on first run with new format
    _sample_keys = list(dedupe_store._data.keys())[:1]
    if _sample_keys and _sample_keys[0].startswith("author_day:"):
        print("  ℹ️  Очищаю устаревший dedupe_state.json (старый формат)")
        dedupe_store._data = {}
        dedupe_store.flush()

    resolved_session = Path(session_path).resolve() if session_path else SESSION_PATH

    async def _start_client():
        # Use StringSession if provided (from saved persistent session), otherwise use file session
        if session_string:
            client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        else:
            client = TelegramClient(str(resolved_session), API_ID, API_HASH)

        await client.connect()

        # Check if already authorized; if not, signal bot to trigger auth flow
        if not await client.is_user_authorized():
            await client.disconnect()
            raise ScannerNeedsAuthError(phone=PHONE)

        return client

    async def _recover_and_retry(err_msg: str):
        # Удаляем journal, переименовываем сессию в .bak и пробуем снова
        journal = resolved_session.with_suffix(resolved_session.suffix + "-journal")
        if journal.exists():
            try:
                journal.unlink()
            except Exception:
                pass
        if resolved_session.exists():
            try:
                resolved_session.rename(resolved_session.with_suffix(resolved_session.suffix + ".bak"))
            except Exception:
                pass
        try:
            client2 = await _start_client()
            return client2
        except Exception as e2:
            return f"{err_msg}; повторная попытка не удалась: {e2}"

    try:
        client = await _start_client()
    except ScannerNeedsAuthError as e:
        # Signal to bot that auth is needed
        raise e
    except sqlite3.OperationalError as e:
        if "database is locked" in str(e).lower():
            recovery = await _recover_and_retry("Ошибка авторизации: database is locked")
            if isinstance(recovery, TelegramClient):
                client = recovery
            else:
                return 0, 0, recovery
        else:
            return 0, 0, f"Ошибка авторизации: {e}"
    except Exception as e:
        return 0, 0, f"Ошибка авторизации: {e}"

    # Получаем entity канала результатов
    try:
        target = await client.get_entity(results_channel)
    except Exception as e:
        await client.disconnect()
        return 0, 0, f"Ошибка доступа к каналу результатов: {e}"

    # Стартовое сообщение
    await client.send_message(
        target,
        f"🔍 <b>Сканирование начато</b>\n"
        f"📅 Период: {days} дн. | 📌 Групп: {len(groups)}\n"
        f"🔑 Ключевых слов: {len(keywords)}",
        parse_mode="html",
    )

    matched_total = 0
    sent_total = 0
    duplicates_skipped = 0
    processed_groups = 0
    skipped_groups = 0

    for group in groups:
        if "/" in group:
            print(f"  ⏭️  Пропуск '{group}' (некорректное имя)")
            skipped_groups += 1
            continue

        try:
            entity = await client.get_entity(group)
        except Exception as e:
            print(f"  ⏭️  Пропуск '{group}': группа недоступна ({type(e).__name__})")
            skipped_groups += 1
            continue

        try:
            title = getattr(entity, "title", group)
            username = getattr(entity, "username", None)

            print(f"  📡 Сканирование: {title} ...")
            min_date = datetime.now() - timedelta(days=days)
            group_matched = 0
            group_sent = 0
            group_duplicates = 0

            async for message in client.iter_messages(entity, limit=max_messages):
                try:
                    if message.date.replace(tzinfo=None) < min_date:
                        break

                    text = message.raw_text or ""
                    found = list(set(_kw_pattern.findall(text))) if _kw_pattern else []
                    # Стем-поиск: проверяем стемы текста против стем-последовательностей ключей
                    tokens = _tokenize(text)
                    text_stems = _stem_words(tokens)
                    # Убираем русские предлоги (для/на/в считаются одним и тем же)
                    text_stems = [s for s in text_stems if s not in STOP_WORDS_RU]
                    stem_set = set(text_stems)

                    matched_stem_keywords = []
                    for seq in _kw_stem_sequences:
                        if not seq:
                            continue
                        if len(seq) == 1:
                            if seq[0] in stem_set:
                                matched_stem_keywords.append(" ".join(seq))
                        else:
                            # поиск подпоследовательности
                            for i in range(0, len(text_stems) - len(seq) + 1):
                                if text_stems[i : i + len(seq)] == seq:
                                    matched_stem_keywords.append(" ".join(seq))
                                    break

                    if not found and not matched_stem_keywords:
                        continue

                    # Check anti-keywords — if any match, skip this message
                    if _anti_kw_pattern is not None:
                        anti_found = _anti_kw_pattern.search(text)
                        if anti_found:
                            continue

                    matched_total += 1
                    group_matched += 1

                    now_utc = datetime.now(timezone.utc)

                    # Content-based deduplication: check first 20 chars
                    content_preview = text[:20].lower()
                    content_key = f"content:{content_preview}"
                    if dedupe_store.is_duplicate(content_key, now_utc):
                        duplicates_skipped += 1
                        group_duplicates += 1
                        continue
                    sender_id = getattr(message, "sender_id", None)
                    if sender_id is not None:
                        id_dedupe_key = f"user_id:{sender_id}"
                    else:
                        chat_id = getattr(entity, "id", "unknown_chat")
                        id_dedupe_key = f"unknown:{chat_id}:{message.id}"

                    if dedupe_store.is_duplicate(id_dedupe_key, now_utc):
                        duplicates_skipped += 1
                        group_duplicates += 1
                        continue

                    # Extract forward origin info (for sending and tracking, not for filtering)
                    fwd_id_key = None
                    fwd_from = getattr(message, "fwd_from", None)
                    if fwd_from:
                        from_id = getattr(fwd_from, "from_id", None)
                        if from_id is not None:
                            if hasattr(from_id, "channel_id"):
                                fwd_id_key = f"user_id:-100{from_id.channel_id}"
                            elif hasattr(from_id, "user_id"):
                                fwd_id_key = f"user_id:{from_id.user_id}"

                    # Fetch sender for username-based dedup (only fires if ID is new)
                    username_dedupe_key = None
                    msg_sender = None
                    try:
                        msg_sender = await message.get_sender()
                        raw_username = getattr(msg_sender, "username", None)
                        if raw_username:
                            username_dedupe_key = f"username:{raw_username.lower()}"
                            if dedupe_store.is_duplicate(username_dedupe_key, now_utc):
                                duplicates_skipped += 1
                                group_duplicates += 1
                                continue
                    except Exception:
                        pass  # Username check is best-effort

                    # Пересылаем сообщение сразу
                    sent_ok = False
                    try:
                        await client.forward_messages(target, messages=message)
                        sent_ok = True
                    except telethon.errors.ChatForwardsRestrictedError:
                        # Группа запрещает пересылку — отправляем текст с ссылкой
                        try:
                            sender_obj = msg_sender if msg_sender is not None else await message.get_sender()
                            name = (
                                getattr(sender_obj, "username", None)
                                or getattr(sender_obj, "first_name", None)
                                or "?"
                            )
                            link = f"https://t.me/{username}/{message.id}" if username else ""
                            text_preview = str(text[:500]) if text else ""
                            await client.send_message(
                                target,
                                f"📩 <b>@{name}</b> | {title}\n"
                                f"🔑 {', '.join(found)}\n\n"
                                f"{text_preview}"
                                + (f"\n\n🔗 {link}" if link else ""),
                                parse_mode="html",
                            )
                            sent_ok = True
                        except Exception as send_err:
                            print(f"  ⚠️  Ошибка отправки (ChatForwardsRestricted): {type(send_err).__name__}")
                    except telethon.errors.FloodWaitError as e:
                        print(f"  ⏳ FloodWait: жду {e.seconds}с...")
                        await asyncio.sleep(e.seconds + 1)
                        try:
                            await client.forward_messages(target, messages=message)
                            sent_ok = True
                        except Exception:
                            pass  # Пропускаем если повтор тоже не удался

                    if not sent_ok:
                        continue

                    if include_source_header:
                        try:
                            source_link = f"https://t.me/{username}/{message.id}" if username else ""
                            header_text = f"📌 <b>{title}</b>\n"
                            if source_link:
                                header_text += f"🔗 <a href='{source_link}'>Открыть источник</a>"
                            else:
                                header_text += "🔒 Источник без публичной ссылки"
                            await client.send_message(target, header_text, parse_mode="html", link_preview=False)
                        except Exception:
                            pass

                    dedupe_store.mark_seen(id_dedupe_key, now_utc)
                    if fwd_id_key:
                        dedupe_store.mark_seen(fwd_id_key, now_utc)
                    if username_dedupe_key:
                        dedupe_store.mark_seen(username_dedupe_key, now_utc)

                    group_sent += 1
                    sent_total += 1
                    # Debug info for matched keywords
                    debug_matches = found + matched_stem_keywords
                    if debug_matches:
                        print(f"DEBUG match in {title}: {debug_matches}")
                    await asyncio.sleep(0.3)

                except Exception as msg_err:
                    # Skip this message on any error, continue with next
                    print(f"  ⚠️  Ошибка обработки сообщения: {type(msg_err).__name__}")
                    continue

            print(
                f"    ✅ Завершено: найдено={group_matched}, отправлено={group_sent}, дубликатов={group_duplicates}"
            )
            processed_groups += 1

            # Прогресс по группе
            await client.send_message(
                target,
                f"✅ @{group} — найдено: {group_matched}, отправлено: {group_sent}, дубликатов: {group_duplicates}",
            )

        except Exception as e:
            print(f"  ⏭️  Ошибка при сканировании '{group}': {type(e).__name__}")
            skipped_groups += 1
            continue

        await asyncio.sleep(0.5)

    # in-memory store: flush() is a no-op; kept for API compatibility
    dedupe_store.flush()

    print(
        f"\n📊 Итого: групп={processed_groups}, найдено={matched_total}, отправлено={sent_total}, "
        f"дубликатов={duplicates_skipped}, пропущено групп={skipped_groups}"
    )

    # Итоговое сообщение
    await client.send_message(
        target,
        f"📊 <b>Сканирование завершено</b>\n"
        f"📌 Групп проверено: {processed_groups} | ⏭️ Пропущено: {skipped_groups}\n"
        f"🔎 Найдено совпадений: <b>{matched_total}</b>\n"
        f"📨 Отправлено уникальных: <b>{sent_total}</b>\n"
        f"🧹 Дубликатов пропущено: <b>{duplicates_skipped}</b>",
        parse_mode="html",
    )

    await client.disconnect()

    return sent_total, processed_groups, skipped_groups


async def monitor_groups_realtime(
    keywords: list | None = None,
    anti_keywords: list | None = None,
    results_channel: int | None = None,
    include_source_header: bool = False,
    session_path: Path | str | None = None,
    groups: list[str] | None = None,
    stop_event: asyncio.Event | None = None,
    session_string: str = "",
) -> None:
    """Realtime monitor for new messages in groups with keyword matching."""
    if keywords is None:
        keywords = KEYWORDS
    if anti_keywords is None:
        anti_keywords = ANTI_KEYWORDS
    if groups is None:
        groups = load_groups()
    if stop_event is None:
        stop_event = asyncio.Event()

    _compile_keywords(keywords)
    if _kw_pattern is None and not _kw_stem_sequences:
        raise ValueError("Нет ключевых слов для мониторинга")
    _compile_anti_keywords(anti_keywords)

    dedupe_store = DedupeStore(None, ttl_seconds=None)
    resolved_session = Path(session_path).resolve() if session_path else SESSION_PATH

    async def _start_client():
        # Use StringSession if provided (from saved persistent session), otherwise use file session
        if session_string:
            client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        else:
            client = TelegramClient(str(resolved_session), API_ID, API_HASH)

        await client.connect()

        # Check if already authorized; if not, signal bot to trigger auth flow
        if not await client.is_user_authorized():
            await client.disconnect()
            raise ScannerNeedsAuthError(phone=PHONE)

        return client

    async def _recover_and_retry(err_msg: str):
        journal = resolved_session.with_suffix(resolved_session.suffix + "-journal")
        if journal.exists():
            try:
                journal.unlink()
            except Exception:
                pass
        if resolved_session.exists():
            try:
                resolved_session.rename(resolved_session.with_suffix(resolved_session.suffix + ".bak"))
            except Exception:
                pass
        try:
            return await _start_client()
        except Exception as e2:
            raise RuntimeError(f"{err_msg}; повторная попытка не удалась: {e2}") from e2

    try:
        client = await _start_client()
    except ScannerNeedsAuthError as e:
        # Signal to bot that auth is needed
        raise e
    except sqlite3.OperationalError as e:
        if "database is locked" in str(e).lower():
            client = await _recover_and_retry("Ошибка авторизации: database is locked")
        else:
            raise RuntimeError(f"Ошибка авторизации: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Ошибка авторизации: {e}") from e

    try:
        target = await client.get_entity(results_channel)
    except Exception as e:
        await client.disconnect()
        raise RuntimeError(f"Ошибка доступа к каналу результатов: {e}") from e

    monitor_entities = []
    for group in groups:
        if "/" in group:
            continue
        try:
            entity = await client.get_entity(group)
            monitor_entities.append(entity)
        except Exception:
            continue

    if not monitor_entities:
        await client.disconnect()
        raise RuntimeError("Нет доступных групп для мониторинга")

    @client.on(events.NewMessage(chats=monitor_entities))
    async def on_new_message(event):
        message = event.message
        text = message.raw_text or ""
        found = list(set(_kw_pattern.findall(text))) if _kw_pattern else []

        tokens = _tokenize(text)
        text_stems = _stem_words(tokens)
        text_stems = [s for s in text_stems if s not in STOP_WORDS_RU]
        stem_set = set(text_stems)

        matched_stem_keywords = []
        for seq in _kw_stem_sequences:
            if not seq:
                continue
            if len(seq) == 1:
                if seq[0] in stem_set:
                    matched_stem_keywords.append(" ".join(seq))
            else:
                for i in range(0, len(text_stems) - len(seq) + 1):
                    if text_stems[i : i + len(seq)] == seq:
                        matched_stem_keywords.append(" ".join(seq))
                        break

        if not found and not matched_stem_keywords:
            return

        if _anti_kw_pattern is not None and _anti_kw_pattern.search(text):
            return

        now_utc = datetime.now(timezone.utc)
        content_preview = text[:20].lower()
        content_key = f"content:{content_preview}"
        if dedupe_store.is_duplicate(content_key, now_utc):
            return

        sender_id = getattr(message, "sender_id", None)
        if sender_id is not None:
            id_dedupe_key = f"user_id:{sender_id}"
        else:
            chat_id = getattr(event, "chat_id", "unknown_chat")
            id_dedupe_key = f"unknown:{chat_id}:{message.id}"
        if dedupe_store.is_duplicate(id_dedupe_key, now_utc):
            return

        username_dedupe_key = None
        msg_sender = None
        try:
            msg_sender = await message.get_sender()
            raw_username = getattr(msg_sender, "username", None)
            if raw_username:
                username_dedupe_key = f"username:{raw_username.lower()}"
                if dedupe_store.is_duplicate(username_dedupe_key, now_utc):
                    return
        except Exception:
            pass

        sent_ok = False
        try:
            await client.forward_messages(target, messages=message)
            sent_ok = True
        except telethon.errors.ChatForwardsRestrictedError:
            try:
                chat = await event.get_chat()
                chat_title = getattr(chat, "title", "unknown")
                chat_username = getattr(chat, "username", None)
                sender_obj = msg_sender if msg_sender is not None else await message.get_sender()
                sender_name = (
                    getattr(sender_obj, "username", None)
                    or getattr(sender_obj, "first_name", None)
                    or "?"
                )
                link = f"https://t.me/{chat_username}/{message.id}" if chat_username else ""
                text_preview = str(text[:500]) if text else ""
                await client.send_message(
                    target,
                    f"📩 <b>@{sender_name}</b> | {chat_title}\n"
                    f"🔑 {', '.join(found)}\n\n"
                    f"{text_preview}"
                    + (f"\n\n🔗 {link}" if link else ""),
                    parse_mode="html",
                )
                sent_ok = True
            except Exception:
                sent_ok = False
        except telethon.errors.FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
            try:
                await client.forward_messages(target, messages=message)
                sent_ok = True
            except Exception:
                sent_ok = False

        if not sent_ok:
            return

        if include_source_header:
            try:
                chat = await event.get_chat()
                chat_title = getattr(chat, "title", "unknown")
                chat_username = getattr(chat, "username", None)
                source_link = f"https://t.me/{chat_username}/{message.id}" if chat_username else ""
                header_text = f"📌 <b>{chat_title}</b>\n"
                if source_link:
                    header_text += f"🔗 <a href='{source_link}'>Открыть источник</a>"
                else:
                    header_text += "🔒 Источник без публичной ссылки"
                await client.send_message(target, header_text, parse_mode="html", link_preview=False)
            except Exception:
                pass

        dedupe_store.mark_seen(id_dedupe_key, now_utc)
        if username_dedupe_key:
            dedupe_store.mark_seen(username_dedupe_key, now_utc)

    runner = asyncio.create_task(client.run_until_disconnected())
    stopper = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        {runner, stopper},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()

    if stopper in done:
        await client.disconnect()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
        return

    if runner in done:
        exc = runner.exception()
        if exc:
            raise exc
