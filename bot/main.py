"""
Telegram Bot для поиска репетиторов в испанских группах.
Использует aiogram 3.x + система категорий ключевых слов.
"""

import os
import asyncio
import sys
import re
import json
import math
import contextlib
import io
import logging
from pathlib import Path
from datetime import datetime, time, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile, FSInputFile, InputMediaPhoto
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiohttp import web

# Добавляем пути
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "parser"))

from categories import (
    load,
    save,
    get_directions,
    get_subcategories,
    get_active_direction,
    get_active_subcategory_ids,
    set_active_selection,
    get_active_keywords,
    get_keywords_for_search,
    get_active_category,
    set_active_category,
    add_keywords_to_subcat,
    remove_keywords_from_subcat,
    add_subcategory,
    resolve_results_channel_for_selection,
)
from scanner import scan_groups_history, monitor_groups_realtime
from groups_manager import load_groups, add_group, delete_group
from anti_keywords_manager import load_anti_keywords, add_anti_keyword, remove_anti_keyword
from broadcast_manager import BroadcastManager
from balance_manager import BalanceManager, scoped_balance_manager
from broadcast_sender import send_broadcast_campaign_with_client, verify_and_delete_test_messages
from stripe_handler import create_checkout_session, process_webhook, STRIPE_PRICES
from storage_paths import state_file, user_data_dir
from mtproto_accounts import (
    PendingLogin,
    code_ttl_seconds,
    disconnect_account,
    extract_session_string,
    get_account,
    get_session_string,
    list_connected_user_ids,
    make_client_from_string_session,
    new_pending_login,
    set_connected_account,
)
from user_data import (
    add_user_broadcast_group,
    delete_user_broadcast_group,
    format_group_ref,
    list_user_ids_from_disk,
    load_user_broadcast_groups,
    normalize_group_ref,
    user_broadcast_state_path,
)

# Подхватываем bot/.env независимо от того, из какой папки запускают бота
load_dotenv(Path(__file__).parent / ".env")
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_RESULTS_CHANNEL = int(os.getenv("DEFAULT_RESULTS_CHANNEL") or os.getenv("RESULTS_CHANNEL", "-1003761773885"))
SOURCE_HEADER_CHANNEL_ID = int(os.getenv("SOURCE_HEADER_CHANNEL_ID", "-1003739349502"))
BROADCAST_TZ = os.getenv("BROADCAST_TZ", "Europe/Madrid")
BROADCAST_TIMES = ["08:12", "11:33", "17:40", "22:30"]
BROADCAST_TIME_OPTIONS = ["07:00", "08:12", "09:00", "11:33", "12:00", "15:00", "17:40", "18:00", "21:00", "22:30"]
BROADCAST_TEST_VERIFY_SECONDS = int(os.getenv("BROADCAST_TEST_VERIFY_SECONDS", "60") or "60")
TEST_COOLDOWN_SECONDS = int(os.getenv("TEST_COOLDOWN_SECONDS", "30") or "30")
TEST_MAX_PER_DAY = int(os.getenv("TEST_MAX_PER_DAY", "5") or "5")
TEST_FRESH_TTL_SECONDS = int(os.getenv("TEST_FRESH_TTL_SECONDS", "86400") or "86400")
READINESS_TTL_SECONDS = int(os.getenv("READINESS_TTL_SECONDS", "1800") or "1800")
OWNER_IDS_ENV = os.getenv("OWNER_IDS") or os.getenv("OWNER_ID", "")
OWNER_IDS = {
    int(item.strip())
    for item in OWNER_IDS_ENV.split(",")
    if item.strip().isdigit()
}
OWNER_2FA_PASSWORD = (os.getenv("OWNER_2FA_PASSWORD") or "").strip()
SESSION_PATH = Path(os.getenv("TG_SESSION_PATH", Path(__file__).parent.parent / "parser" / "tutor_bot_scan.session")).resolve()
logger = logging.getLogger(__name__)

# Для пула постов: куда бот копирует присланные сообщения, чтобы Telethon мог их переотправлять.
# Можно задать username канала (например, @connect_services). Если не задано, используем активный send-as канал.
BROADCAST_STORAGE_CHANNEL = (os.getenv("BROADCAST_STORAGE_CHANNEL") or "").strip()

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_NAMES = {
    "mon": "ПН",
    "tue": "ВТ",
    "wed": "СР",
    "thu": "ЧТ",
    "fri": "ПТ",
    "sat": "СБ",
    "sun": "ВС",
}


def _normalize_hhmm(raw_value: str) -> str | None:
    value = (raw_value or "").strip().replace(".", ":")
    m = re.match(r"^(\d{1,2}):(\d{2})$", value)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return f"{hh:02d}:{mm:02d}"


def _validate_allowed_time(hhmm: str) -> tuple[bool, str]:
    """
    MVP rule: night is forbidden. Allow 07:00..21:59 inclusive.
    """
    try:
        hh, mm = hhmm.split(":")
        t = time(hour=int(hh), minute=int(mm))
    except Exception:
        return False, "❌ Неверное время. Пример: 10:00 или 10.00"
    if t < time(7, 0) or t > time(21, 59):
        return False, "🚫 Ночь запрещена. Разрешено: 07:00–21:59"
    return True, ""


def _resolve_storage_channel_ref(state: dict) -> str | None:
    """
    Возвращает чат/канал, куда копируем посты (Bot API) и откуда Telethon читает для рассылки.
    Предпочтение:
    1) env BROADCAST_STORAGE_CHANNEL
    2) активный send-as канал кампании
    3) legacy source_channel
    """
    if BROADCAST_STORAGE_CHANNEL:
        return BROADCAST_STORAGE_CHANNEL
    campaign = state.get("campaign", {}) if isinstance(state, dict) else {}
    if campaign.get("send_as_channel"):
        return str(campaign.get("send_as_channel"))
    if campaign.get("source_channel"):
        return str(campaign.get("source_channel"))
    return None


def _bot_chat_id(ref: str | int) -> int | str:
    """
    aiogram Bot API accepts chat_id as int for numeric ids and as str for @username.
    """
    if isinstance(ref, int):
        return ref
    s = str(ref or "").strip()
    if re.fullmatch(r"-?\d{5,}", s):
        try:
            return int(s)
        except Exception:
            return s
    return s

# ─── Инициализация ───────────────────────────────────────────────────────────

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
broadcast_manager = BroadcastManager(
    state_file("broadcast_state.json"),
    default_tz=BROADCAST_TZ,
    default_times=BROADCAST_TIMES,
)
broadcast_lock = asyncio.Lock()
scheduler_task: asyncio.Task | None = None
current_scan_task: asyncio.Task | None = None
current_monitor_task: asyncio.Task | None = None
monitor_stop_event: asyncio.Event | None = None
pending_logins: dict[int, PendingLogin] = {}


# ─── Состояния ───────────────────────────────────────────────────────────────

class MainMenu(StatesGroup):
    viewing = State()
    scanning = State()
    selecting_direction = State()
    selecting_subcats = State()
    editing_keywords = State()
    adding_subcat_keyword = State()
    adding_category_keyword = State()
    deleting_subcat_keyword = State()
    deleting_category_keyword = State()
    adding_subcategory = State()
    editing_category_name = State()
    adding_category_name = State()
    adding_category_keywords = State()
    adding_group = State()
    adding_anti_keyword = State()
    adding_broadcast_channel = State()
    adding_broadcast_group = State()
    setting_broadcast_source = State()
    adding_broadcast_post = State()
    viewing_account_warning = State()
    setting_broadcast_weekday_time = State()
    copying_broadcast_weekday = State()
    connecting_account_phone = State()
    connecting_account_code = State()
    connecting_account_password = State()
    connecting_account_api = State()        # ввод api_id (шаг 1)
    connecting_account_api_hash = State()   # ввод api_hash (шаг 2)
    connecting_account_api_phone = State()  # ввод телефона (шаг 3)


# ─── Хелперы для клавиатур ───────────────────────────────────────────────────

def category_buttons() -> list:
    """Создаёт кнопки для всех направлений (новая система)"""
    cat_state = load()
    directions = get_directions(cat_state)
    active_dir = get_active_direction(cat_state)

    buttons = []
    for dir_id, dir_data in directions.items():
        check = "✅" if dir_id == active_dir else "  "
        name = dir_data.get("name", dir_id)
        buttons.append([InlineKeyboardButton(
            text=f"{check} {name}",
            callback_data=f"dir_select_{dir_id}"
        )])

    return buttons


def main_menu_text(schedule_enabled: bool, active_name: str) -> str:
    """Генерирует текст главного меню с статусом рассылки"""
    bc_status = "включена ✅" if schedule_enabled else "выключена ❌"
    active_display = active_name if active_name else "Не выбрано"
    channel_id, channel_error = resolve_active_results_channel()
    channel_text = format_channel_label(channel_id if channel_id is not None else DEFAULT_RESULTS_CHANNEL)
    if channel_error:
        channel_text += " (конфликт)"
    return (
        "👋 <b>Главное меню</b>\n\n"
        f"📂 Активное направление: {active_display}\n"
        f"📨 Канал результатов: <code>{channel_text}</code>\n"
        f"📣 Рассылка: {bc_status}\n\n"
        "Выбери действие:"
    )


def main_keyboard(schedule_enabled: bool = True):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔍 Сканировать", callback_data="scan"),
            InlineKeyboardButton(text="⏱️ Мониторинг", callback_data="monitor"),
        ],
        [
            InlineKeyboardButton(text="📣 Рассылка", callback_data="broadcast"),
            InlineKeyboardButton(text="📊 Статус", callback_data="status"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"),
            InlineKeyboardButton(text="ℹ️ Справка", callback_data="help"),
        ],
    ])


def settings_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📂 Категории", callback_data="categories"),
            InlineKeyboardButton(text="📊 Группы", callback_data="groups"),
        ],
        [
            InlineKeyboardButton(text="🚫 Стоп-слова", callback_data="anti_keywords"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="back_main"),
        ],
    ])


def back_button():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_main")],
    ])


def settings_notifications_text(enabled: bool, threshold: int) -> str:
    status_icon = "☑️" if enabled else "☐"
    return (
        "🔔 <b>УВЕДОМЛЕНИЯ</b>\n\n"
        f"{status_icon} <b>Баланс заканчивается</b>\n"
        f"Порог: <b>{threshold}</b> постов\n\n"
        "✅ Аналитика рассылки (обязательное)\n"
        "✅ Платеж успешен (обязательное)\n"
        "✅ Платеж отклонен (обязательное)"
    )


def settings_notifications_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    toggle_label = "Отключить баланс" if enabled else "Включить баланс"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Изменить порог", callback_data="notif_threshold_menu")],
        [InlineKeyboardButton(text=toggle_label, callback_data="notif_balance_toggle")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="bc_settings")],
    ])


def notifications_threshold_text(current_threshold: int) -> str:
    return (
        "⚠️ <b>ПОРОГ УВЕДОМЛЕНИЯ О БАЛАНСЕ</b>\n\n"
        f"Текущий порог: <b>{current_threshold}</b> постов\n\n"
        "Уведомить когда остается ≤ X постов:"
    )


def notifications_threshold_keyboard() -> InlineKeyboardMarkup:
    values = [10, 20, 30, 50, 100]
    rows = []
    rows.append([
        InlineKeyboardButton(text=str(values[0]), callback_data=f"notif_set_threshold_{values[0]}"),
        InlineKeyboardButton(text=str(values[1]), callback_data=f"notif_set_threshold_{values[1]}"),
        InlineKeyboardButton(text=str(values[2]), callback_data=f"notif_set_threshold_{values[2]}"),
    ])
    rows.append([
        InlineKeyboardButton(text=str(values[3]), callback_data=f"notif_set_threshold_{values[3]}"),
        InlineKeyboardButton(text=str(values[4]), callback_data=f"notif_set_threshold_{values[4]}"),
    ])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="settings_notifications")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def broadcast_settings_text() -> str:
    return (
        "⚙️ <b>Настройки рассылки</b>\n\n"
        "Здесь находятся настройки, которые относятся только к разделу «Рассылка».\n"
        "Новые параметры будут добавляться сюда."
    )


def broadcast_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔔 Уведомления", callback_data="settings_notifications")],
        [InlineKeyboardButton(text="🧩 Скоро: другие настройки", callback_data="noop")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="broadcast")],
    ])


def broadcast_test_intro_text() -> str:
    return (
        "🧪 <b>Перед тестом рассылки</b>\n\n"
        "Тест проверяет, в каких группах сообщение публикуется без проблем.\n"
        "Это снижает риск жалоб, ограничений и блокировки аккаунта (вплоть до 24 часов).\n\n"
        "Что делает тест:\n"
        "• отправляет тестовые сообщения в выбранные группы;\n"
        "• ждёт 60 секунд и проверяет результат;\n"
        "• автоматически отключает нерабочие группы;\n"
        "• показывает отчёт: сколько прошло и сколько отключено.\n\n"
        "После успешного теста можно безопаснее запускать массовую рассылку."
    )


def broadcast_test_intro_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Начать тест (60 сек)", callback_data="bc_test_start")],
        [InlineKeyboardButton(text="ℹ️ Подробнее", callback_data="bc_test_info")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="bc_launch_menu")],
    ])


def broadcast_test_info_text() -> str:
    return (
        "ℹ️ <b>Зачем тест обязателен</b>\n\n"
        "Без теста массовая рассылка может попадать в группы, где посты удаляются или запрещены.\n"
        "Из-за этого растёт риск жалоб и ограничений Telegram.\n\n"
        "Рекомендуемый порядок всегда один:\n"
        "1) Тест\n"
        "2) Автофильтр нерабочих групп\n"
        "3) Массовая рассылка"
    )


def broadcast_test_info_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Начать тест (60 сек)", callback_data="bc_test_start")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="bc_launch_menu")],
    ])


def format_channel_label(channel_id: int | None) -> str:
    return str(channel_id) if channel_id is not None else "не задан"


def resolve_active_results_channel() -> tuple[int | None, str | None]:
    cat_state = load()
    return resolve_results_channel_for_selection(cat_state, DEFAULT_RESULTS_CHANNEL)


def should_include_source_header(results_channel: int | None) -> bool:
    return results_channel is not None and results_channel == SOURCE_HEADER_CHANNEL_ID


def anti_keywords_keyboard(words: list, page: int = 0):
    """Клавиатура для управления стоп-словами (пагинированная, 3 колонки × 4 строки)"""
    buttons = []
    per_page = 12
    total_pages = max(1, math.ceil(len(words) / per_page))
    page = max(0, min(page, total_pages - 1))  # Обеспечиваем валидность страницы

    # Получаем слова для текущей страницы
    page_words = words[page * per_page : (page + 1) * per_page]

    # Выводим слова в 3 колонки (по 4 строки = 12 слов на странице)
    for i in range(0, len(page_words), 3):
        row = []
        for j in range(3):
            if i + j < len(page_words):
                word = page_words[i + j]
                row.append(InlineKeyboardButton(text=f"🗑 {word}", callback_data=f"antikw_del_{word}"))
        if row:
            buttons.append(row)

    # Навигация по страницам
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀", callback_data=f"antikw_page_{page - 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text=" ", callback_data="antikw_noop"))  # Пустая кнопка для выравнивания

    nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="antikw_noop"))

    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="▶", callback_data=f"antikw_page_{page + 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text=" ", callback_data="antikw_noop"))  # Пустая кнопка для выравнивания

    buttons.append(nav_row)

    # Кнопка добавления
    buttons.append([InlineKeyboardButton(text="➕ Добавить стоп-слово", callback_data="antikw_add")])

    # Кнопка назад
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="settings")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def is_owner(user_id: int | None) -> bool:
    if not OWNER_IDS:
        return True
    if user_id is None:
        return False
    return user_id in OWNER_IDS


async def ensure_owner_callback(query: CallbackQuery) -> bool:
    if is_owner(query.from_user.id):
        return True
    await query.answer("⛔️ Доступ только для владельца бота.", show_alert=True)
    return False


def get_setup_steps(user_id: int, state: dict, groups: list[str]) -> dict:
    """
    Returns a dict of booleans for each setup step required before mass broadcast.
    Steps must be completed in order: account → posts → groups → schedule → readiness → test.
    """
    readiness_ok, _ = is_readiness_fresh(state)
    return {
        "account": get_account(user_id) is not None,
        "posts": len(state.get("campaign", {}).get("posts", [])) > 0,
        "groups": len(groups) > 0,
        "schedule": any(
            v.get("time")
            for v in state.get("weekly_schedule", {}).values()
            if isinstance(v, dict)
        ),
        "readiness": readiness_ok,
        "test": state.get("campaign", {}).get("test_passed", False),
    }


def _readiness_snapshot_from_state(state: dict) -> dict:
    campaign = state.get("campaign", {}) if isinstance(state.get("campaign", {}), dict) else {}
    selected_groups = campaign.get("selected_groups", [])
    return {
        "send_mode": campaign.get("send_mode", "user"),
        "send_as_channel": campaign.get("send_as_channel", ""),
        "selected_groups_count": len(selected_groups) if isinstance(selected_groups, list) else 0,
    }


def _invalidate_readiness_in_state(state: dict, reason: str = "config_changed") -> dict:
    campaign = state.setdefault("campaign", {})
    campaign["readiness_passed"] = False
    campaign["readiness_last_reason"] = reason
    return state


def invalidate_readiness_if_needed(bm: BroadcastManager, reason: str = "config_changed") -> dict:
    state = bm.load()
    _invalidate_readiness_in_state(state, reason=reason)
    bm.save(state)
    return state


def is_readiness_fresh(state: dict, ttl_seconds: int = READINESS_TTL_SECONDS) -> tuple[bool, str]:
    campaign = state.get("campaign", {}) if isinstance(state.get("campaign", {}), dict) else {}

    if not bool(campaign.get("readiness_passed", False)):
        return False, "not_passed"

    try:
        problem_count = int(campaign.get("readiness_problem_count", 0))
    except Exception:
        problem_count = 0
    if problem_count > 0:
        return False, "has_problems"

    checked_at = campaign.get("readiness_checked_at")
    if not checked_at:
        return False, "missing_checked_at"
    try:
        checked_dt = datetime.fromisoformat(str(checked_at))
        if checked_dt.tzinfo is None:
            checked_dt = checked_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return False, "invalid_checked_at"

    now = datetime.now(timezone.utc)
    if now - checked_dt > timedelta(seconds=max(60, ttl_seconds)):
        return False, "stale"

    current_snapshot = _readiness_snapshot_from_state(state)
    saved_snapshot = campaign.get("readiness_mode_snapshot", {})
    if not isinstance(saved_snapshot, dict):
        return False, "snapshot_missing"
    if current_snapshot != {
        "send_mode": saved_snapshot.get("send_mode", "user"),
        "send_as_channel": saved_snapshot.get("send_as_channel", ""),
        "selected_groups_count": int(saved_snapshot.get("selected_groups_count", -1)) if str(saved_snapshot.get("selected_groups_count", "")).lstrip("-").isdigit() else -1,
    }:
        return False, "snapshot_changed"

    return True, "ok"


def is_test_fresh(state: dict, ttl_seconds: int = TEST_FRESH_TTL_SECONDS) -> tuple[bool, str]:
    campaign = state.get("campaign", {}) if isinstance(state.get("campaign", {}), dict) else {}
    if not bool(campaign.get("test_passed", False)):
        return False, "not_passed"

    last_test_at = campaign.get("last_test_at")
    if not isinstance(last_test_at, str) or not last_test_at:
        return False, "missing_last_test_at"

    try:
        tested_dt = datetime.fromisoformat(last_test_at)
        if tested_dt.tzinfo is None:
            tested_dt = tested_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return False, "invalid_last_test_at"

    now = datetime.now(timezone.utc)
    if now - tested_dt > timedelta(seconds=max(60, ttl_seconds)):
        return False, "stale"

    return True, "ok"


def _readiness_reason_human(reason_code: str) -> str:
    mapping = {
        "not_passed": "Готовность еще не пройдена.",
        "has_problems": "В готовности есть проблемные группы.",
        "missing_checked_at": "Готовность не завершена.",
        "invalid_checked_at": "Статус готовности поврежден, пройдите заново.",
        "stale": "Готовность устарела, обновите шаг 1.",
        "snapshot_missing": "Изменились условия кампании, обновите шаг 1.",
        "snapshot_changed": "Вы изменили настройки кампании, пройдите шаг 1 заново.",
        "not_connected": "Аккаунт не подключен.",
        "no_groups_selected": "Не выбраны группы рассылки.",
        "group_issues": "Есть группы с ограничениями на отправку.",
    }
    return mapping.get(reason_code, "Требуется обновить готовность.")


def _launch_block_reason(state: dict, steps: dict) -> str:
    if not steps.get("account"):
        return "Сначала подключите аккаунт."
    if not steps.get("posts"):
        return "Добавьте хотя бы один пост в пул."
    if not steps.get("groups"):
        return "Добавьте и выберите группы рассылки."
    if not steps.get("schedule"):
        return "Настройте расписание (минимум один день)."
    if not steps.get("readiness"):
        _, reason = is_readiness_fresh(state)
        if reason == "not_passed":
            campaign = state.get("campaign", {}) if isinstance(state.get("campaign", {}), dict) else {}
            reason = str(campaign.get("readiness_last_reason") or "not_passed")
        return _readiness_reason_human(reason)
    if not steps.get("test"):
        return "Сначала выполните шаг 2: Тест."
    return "Все шаги выполнены. Можно запускать рассылку."


def broadcast_launch_text(state: dict, *, user_id: int, notice: str | None = None) -> str:
    groups = scoped_load_broadcast_groups(user_id)
    steps = get_setup_steps(user_id, state, groups)
    readiness_done = bool(steps.get("readiness"))
    test_done = bool(steps.get("test"))
    step2_open = all(bool(steps.get(key)) for key in ("account", "posts", "groups", "schedule", "readiness"))
    step3_open = test_done

    s1 = "✅" if readiness_done else "⬜"
    s2 = "✅" if test_done else ("⬜" if step2_open else "🔒")
    s3 = "🟢" if step3_open else "🔒"

    lines = [
        "🚀 <b>ЗАПУСК РАССЫЛКИ</b>",
        "",
        f"1) Готовность: {s1}",
        f"2) Тест: {s2}",
        f"3) Запуск: {s3}",
        "",
        f"Статус: {_launch_block_reason(state, steps)}",
    ]
    if notice:
        lines.extend(["", f"ℹ️ {notice}"])
    return "\n".join(lines)


def broadcast_launch_keyboard(state: dict, *, user_id: int) -> InlineKeyboardMarkup:
    groups = scoped_load_broadcast_groups(user_id)
    steps = get_setup_steps(user_id, state, groups)
    step2_open = all(bool(steps.get(key)) for key in ("account", "posts", "groups", "schedule", "readiness"))
    step3_open = bool(steps.get("test"))

    step2_label = "2️⃣ Тест" if step2_open else "2️⃣ Тест (🔒)"
    step3_label = "3️⃣ Запустить рассылку" if step3_open else "3️⃣ Запустить рассылку (🔒)"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1️⃣ Готовность", callback_data="bc_launch_step_ready")],
        [InlineKeyboardButton(text=step2_label, callback_data="bc_launch_step_test")],
        [InlineKeyboardButton(text=step3_label, callback_data="bc_launch_step_mass")],
        [InlineKeyboardButton(text="◀️ Назад к рассылке", callback_data="broadcast")],
    ])


def scoped_broadcast_manager(user_id: int) -> BroadcastManager:
    """
    Owner uses global broadcast_state.json; regular users have isolated state under bot/user_data/<user_id>/.
    """
    if is_owner(user_id):
        return broadcast_manager
    return BroadcastManager(
        user_broadcast_state_path(user_id),
        default_tz=BROADCAST_TZ,
        default_times=BROADCAST_TIMES,
    )


def scoped_balance_manager(user_id: int) -> BalanceManager:
    """
    Owner uses global balance_state.json; regular users have isolated state under bot/user_data/<user_id>/.
    """
    if is_owner(user_id):
        path = state_file("balance_state.json")
    else:
        path = user_data_dir() / str(user_id) / "balance_state.json"
    return BalanceManager(path)


def scoped_load_broadcast_groups(user_id: int) -> list[str]:
    return load_groups() if is_owner(user_id) else load_user_broadcast_groups(user_id)


def get_active_selected_groups_from(state: dict, groups: list[str]) -> list[str]:
    selected = set(state.get("campaign", {}).get("selected_groups", []))
    groups_state = state.get("broadcast_groups_state", {})
    return [
        group
        for group in (groups or [])
        if group in selected and groups_state.get(group, {}).get("status") != "blocked"
    ]


def broadcast_summary_text(state: dict, *, user_id: int | None = None, groups: list[str] | None = None) -> str:
    campaign = state.get("campaign", {})
    schedule = state.get("broadcast_schedule", {})
    send_mode = campaign.get("send_mode", "user")
    posts = campaign.get("posts", []) if isinstance(campaign.get("posts", []), list) else []
    rotation_index = int(campaign.get("rotation_index") or 0) if posts else 0
    selected_groups = campaign.get("selected_groups", [])
    groups_state = state.get("broadcast_groups_state", {})
    blocked_count = sum(1 for item in groups_state.values() if item.get("status") == "blocked")
    test_fresh, test_reason = is_test_fresh(state)
    if test_fresh:
        test_status = "✅ пройден (свежий)"
    elif campaign.get("test_passed"):
        test_status = "⚠️ устарел — запустите тест заново"
    else:
        test_status = "❌ не пройден"
    last_test_label = ""
    raw_last_test_at = campaign.get("last_test_at")
    if isinstance(raw_last_test_at, str) and raw_last_test_at:
        try:
            last_dt = datetime.fromisoformat(raw_last_test_at)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            last_test_label = f" (посл. тест: {last_dt.astimezone(timezone.utc).strftime('%d.%m %H:%M')} UTC)"
        except Exception:
            if test_reason in {"invalid_last_test_at", "missing_last_test_at"}:
                last_test_label = " (время теста не определено)"
    schedule_status = "включено" if schedule.get("enabled", True) else "выключено"
    tz = schedule.get("tz", BROADCAST_TZ)

    weekly = state.get("weekly_schedule", {}) if isinstance(state.get("weekly_schedule", {}), dict) else {}
    active_days = []
    for wd in WEEKDAYS:
        meta = weekly.get(wd) or {}
        if meta.get("enabled") and meta.get("time"):
            active_days.append(f"{WEEKDAY_NAMES.get(wd, wd)} {meta.get('time')}")
    weekly_label = ", ".join(active_days) if active_days else "не настроено"

    if send_mode == "user":
        mode_label = "🧑 От пользователя"
    else:
        channel = campaign.get("send_as_channel", "не выбран")
        mode_label = f"📢 От канала: {channel}"

    if user_id is not None and not is_owner(user_id):
        meta = get_account(user_id)
        if meta:
            username = (meta.get("username") or "").strip()
            name = (meta.get("first_name") or "").strip()
            account_label = f"@{username}" if username else (name or "аккаунт")
        else:
            account_label = "не подключен"
    else:
        account_label = "один аккаунт (env)"

    next_post_label = f" (следующий: <b>{rotation_index + 1}</b>)" if posts else ""

    # Build step progress indicator (only for non-owner users)
    step_progress = ""
    if user_id is not None and not is_owner(user_id) and groups is not None:
        steps = get_setup_steps(user_id, state, groups)
        if not all(steps.values()):
            step_symbols = []
            step_names = [
                "Подключить аккаунт",
                "Добавить пост",
                "Добавить группы рассылки",
                "Настроить расписание (≥1 день)",
                "Проверить готовность",
                "Пройти тест",
            ]
            for i, (key, completed) in enumerate(steps.items(), 1):
                symbol = "✅" if completed else "⬜"
                step_symbols.append(f"{symbol} Шаг {i}: {step_names[i - 1]}")

            # Find current step (first incomplete one)
            current_step = next((k for k, v in steps.items() if not v), None)
            current_text = ""
            if current_step == "account":
                current_text = "👉 Текущий шаг: Подключите аккаунт"
            elif current_step == "posts":
                current_text = "👉 Текущий шаг: Добавьте хотя бы один пост"
            elif current_step == "groups":
                current_text = "👉 Текущий шаг: Добавьте и выберите группы рассылки"
            elif current_step == "schedule":
                current_text = "👉 Текущий шаг: Настройте расписание — укажите время для дня"
            elif current_step == "readiness":
                current_text = "👉 Текущий шаг: Откройте «🚀 Запустить рассылку» и пройдите шаг 1"
            elif current_step == "test":
                current_text = "👉 Текущий шаг: Откройте «🚀 Запустить рассылку» и пройдите шаг 2"

            step_progress = (
                "📋 <b>Настройка рассылки:</b>\n"
                + "\n".join(step_symbols)
                + f"\n{current_text}\n\n"
                "────────────────────\n"
            )

    status_indicator = "✅ Рассылка активна" if schedule.get("enabled", True) else "❌ Рассылка остановлена"

    return (
        step_progress +
        f"{status_indicator}\n\n"
        "📣 <b>Рассылка</b>\n\n"
        f"Аккаунт: <b>{account_label}</b>\n"
        f"Режим: <b>{mode_label}</b>\n"
        f"Постов в пуле: <b>{len(posts)}</b>{next_post_label}\n"
        f"Выбрано групп: <b>{len(selected_groups)}</b>\n"
        f"Недоступных групп: <b>{blocked_count}</b>\n"
        f"Тест: <b>{test_status}</b>{last_test_label}\n\n"
        f"Расписание: <b>{schedule_status}</b>\n"
        f"TZ: <b>{tz}</b>\n"
        f"Неделя: <b>{weekly_label}</b>"
    )


def broadcast_main_keyboard(state: dict, *, user_id: int) -> InlineKeyboardMarkup:
    campaign = state.get("campaign", {})
    send_mode = campaign.get("send_mode", "user")
    posts = campaign.get("posts", []) if isinstance(campaign.get("posts", []), list) else []
    schedule_enabled = state.get("broadcast_schedule", {}).get("enabled", True)

    # Get balance
    bm = scoped_balance_manager(user_id)
    balance = bm.get_balance()

    rows = []
    # Add balance button at the top
    rows.append([InlineKeyboardButton(text=f"💰 Баланс: {balance} постов", callback_data="bc_balance")])

    # Add account connection button first (goes directly to warning pages)
    rows.append([InlineKeyboardButton(text="🔑 Подключить аккаунт", callback_data="acc_methods")])

    mode_text = "🧑 Режим: от пользователя" if send_mode == "user" else "📢 Режим: от канала"
    rows.append([InlineKeyboardButton(text=mode_text, callback_data="bc_mode_toggle")])

    if send_mode == "channel":
        rows.append([InlineKeyboardButton(text="📢 Каналы send-as", callback_data="bc_channels")])

    rows.append([InlineKeyboardButton(text=f"🗂 Посты ({len(posts)}/10)", callback_data="bc_posts")])
    rows.append([InlineKeyboardButton(text="👥 Группы рассылки", callback_data="bc_groups")])
    rows.append([InlineKeyboardButton(text="🚀 Запустить рассылку", callback_data="bc_launch_menu")])
    rows.append([InlineKeyboardButton(text="⚙️ Настройки", callback_data="bc_settings")])
    rows.append([InlineKeyboardButton(text="📅 Расписание (неделя)", callback_data="bc_schedule")])
    rows.append([InlineKeyboardButton(
        text="⏸ Приостановить рассылку" if schedule_enabled else "▶️ Возобновить рассылку",
        callback_data="main_bc_toggle",
    )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def broadcast_channels_keyboard(state: dict) -> InlineKeyboardMarkup:
    channels = state.get("send_as_channels", [])
    selected = state.get("campaign", {}).get("send_as_channel", "")
    buttons = []
    for channel in channels:
        mark = "✅" if channel == selected else "▫️"
        buttons.append([InlineKeyboardButton(text=f"{mark} {channel}", callback_data=f"bc_set_{channel}")])
    buttons.extend([
        [InlineKeyboardButton(text="➕ Добавить канал", callback_data="bc_add_channel")],
        [InlineKeyboardButton(text="🗑 Удалить выбранный", callback_data="bc_del_selected_channel")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="broadcast")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def broadcast_posts_text(state: dict) -> str:
    campaign = state.get("campaign", {})
    posts = campaign.get("posts", []) if isinstance(campaign.get("posts", []), list) else []
    if not posts:
        return (
            "🗂 <b>Посты (пул)</b>\n\n"
            "Пока нет постов.\n\n"
            "Нажмите <b>➕ Добавить</b> и пришлите пост (текст/фото/видео)."
        )
    lines = ["🗂 <b>Посты (пул)</b>\n", f"Всего: <b>{len(posts)}</b> / 10\n"]
    for idx, p in enumerate(posts, 1):
        kind = str(p.get("kind") or "post")
        preview = (p.get("preview") or "").strip()
        preview = (preview[:80] + "…") if len(preview) > 80 else preview
        lines.append(f"{idx}. <b>{kind}</b> — {preview}")
    return "\n".join(lines)


def broadcast_posts_keyboard(state: dict) -> InlineKeyboardMarkup:
    campaign = state.get("campaign", {})
    posts = campaign.get("posts", []) if isinstance(campaign.get("posts", []), list) else []
    buttons = []
    if posts:
        row = []
        for idx, p in enumerate(posts, 1):
            pid = str(p.get("id") or "")
            if not pid:
                continue
            row.append(InlineKeyboardButton(text=f"🗑 {idx}", callback_data=f"bcp_del_{pid}"))
            if len(row) == 4:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
    add_disabled = len(posts) >= 10
    buttons.append([InlineKeyboardButton(
        text="➕ Добавить" if not add_disabled else "➕ Добавить (лимит 10)",
        callback_data="bcp_add" if not add_disabled else "noop",
    )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="broadcast")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def broadcast_posts_add_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Добавить еще", callback_data="bcp_more"),
            InlineKeyboardButton(text="✅ Готово", callback_data="bcp_done"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="bc_posts")],
    ])


def broadcast_week_text(state: dict) -> str:
    schedule_enabled = state.get("broadcast_schedule", {}).get("enabled", True)
    weekly = state.get("weekly_schedule", {}) if isinstance(state.get("weekly_schedule", {}), dict) else {}
    lines = [
        "📅 <b>Расписание (неделя)</b>",
        "",
        f"Авторассылка: <b>{'ON' if schedule_enabled else 'OFF'}</b>",
        "Правило MVP: <b>1 запуск в день</b> (07:00–21:59).",
        "",
        "Текущие дни:",
    ]
    any_day = False
    for wd in WEEKDAYS:
        meta = weekly.get(wd) or {}
        if meta.get("enabled") and meta.get("time"):
            any_day = True
            lines.append(f"• {WEEKDAY_NAMES.get(wd, wd)} — <b>{meta.get('time')}</b>")
    if not any_day:
        lines.append("• (не настроено)")
    lines.append("")
    lines.append("Выберите день недели для настройки:")
    return "\n".join(lines)


def broadcast_week_keyboard() -> InlineKeyboardMarkup:
    days = [(WEEKDAY_NAMES[wd], f"bcs_day_{wd}") for wd in WEEKDAYS]
    row1 = [InlineKeyboardButton(text=label, callback_data=cb) for label, cb in days[:3]]
    row2 = [InlineKeyboardButton(text=label, callback_data=cb) for label, cb in days[3:6]]
    row3 = [InlineKeyboardButton(text=label, callback_data=cb) for label, cb in days[6:]]
    row3.append(InlineKeyboardButton(text="◀️ Назад", callback_data="broadcast"))
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, row3])


def broadcast_day_text(state: dict, weekday: str) -> str:
    weekly = state.get("weekly_schedule", {}) if isinstance(state.get("weekly_schedule", {}), dict) else {}
    meta = weekly.get(weekday) or {}
    name = WEEKDAY_NAMES.get(weekday, weekday)
    enabled = bool(meta.get("enabled"))
    t = meta.get("time")
    status = "✅ открыт" if enabled else "⛔️ закрыт"
    time_label = f"<b>{t}</b>" if t else "не задано"
    return (
        f"📅 <b>День: {name}</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Время: {time_label}\n\n"
        "Введите время в формате <code>HH:MM</code> (например, <code>10:00</code> или <code>10.00</code>)."
    )


def broadcast_day_keyboard(state: dict, weekday: str) -> InlineKeyboardMarkup:
    weekly = state.get("weekly_schedule", {}) if isinstance(state.get("weekly_schedule", {}), dict) else {}
    meta = weekly.get(weekday) or {}
    enabled = bool(meta.get("enabled"))
    t = meta.get("time")
    buttons = [
        [
            InlineKeyboardButton(text="✏️ Установить время" if not t else "✏️ Изменить время", callback_data=f"bcs_set_{weekday}"),
            InlineKeyboardButton(text="🗑 Убрать время", callback_data=f"bcs_clear_{weekday}"),
        ],
        [
            InlineKeyboardButton(text="⛔️ Закрыть день" if enabled else "✅ Открыть день", callback_data=f"bcs_toggle_{weekday}"),
            InlineKeyboardButton(text="📋 Скопировать на…", callback_data=f"bcs_copy_{weekday}"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="bc_schedule")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def broadcast_copy_target_text(source_weekday: str) -> str:
    name = WEEKDAY_NAMES.get(source_weekday, source_weekday)
    return (
        f"📋 <b>Скопировать время</b>\n\n"
        f"Источник: <b>{name}</b>\n"
        "Выберите целевой день:"
    )


def broadcast_copy_target_keyboard(source_weekday: str) -> InlineKeyboardMarkup:
    days = [(WEEKDAY_NAMES[wd], f"bcs_copy_to_{source_weekday}_{wd}") for wd in WEEKDAYS if wd != source_weekday]
    row1 = [InlineKeyboardButton(text=label, callback_data=cb) for label, cb in days[:3]]
    row2 = [InlineKeyboardButton(text=label, callback_data=cb) for label, cb in days[3:6]]
    rows = [row1, row2]
    if len(days) > 6:
        rows.append([InlineKeyboardButton(text=days[6][0], callback_data=days[6][1])])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"bcs_day_{source_weekday}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_iso_date_short(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.date().isoformat()
    except Exception:
        return str(value)[:10]


def _format_iso_dt_short(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(value)


def broadcast_balance_text(state: dict) -> str:
    """Format balance display with tariff options."""
    balance = int(state.get("posts") or 0)
    total_purchased = int(state.get("total_purchased") or 0)
    total_spent = int(state.get("total_spent") or 0)
    created_at = _format_iso_date_short(state.get("created_at"))
    return (
        "💰 <b>МОЙ БАЛАНС</b>\n\n"
        f"🟢 Доступно: <b>{balance}</b> постов\n"
        f"🛍 Всего куплено: <b>{total_purchased}</b>\n"
        f"📤 Всего потрачено: <b>{total_spent}</b>\n"
        f"📅 Аккаунт создан: <b>{created_at}</b>\n\n"
        "Выберите пакет и пополните баланс:\n\n"
        "<b>📦 Small:</b> 100 постов / €3.99 (€0.0399/пост)\n"
        "<b>⭐ Medium:</b> 300 постов / €7.99 (€0.0266/пост, -33% 📉) — <b>Популярный!</b>\n"
        "<b>💎 Large:</b> 1500 постов / €33.99 (€0.0226/пост, -73% 📉)\n\n"
        "Платите только за успешные публикации."
    )


def broadcast_balance_keyboard() -> InlineKeyboardMarkup:
    """Balance menu with tariff purchase buttons."""
    buttons = [
        [InlineKeyboardButton(text="⭐ €7.99 (300 постов)", callback_data="bc_buy_medium")],
        [
            InlineKeyboardButton(text="📦 €3.99 (100)", callback_data="bc_buy_small"),
            InlineKeyboardButton(text="💎 €33.99 (1500)", callback_data="bc_buy_large"),
        ],
        [
            InlineKeyboardButton(text="📜 История", callback_data="bc_balance_history"),
            InlineKeyboardButton(text="◀️ Назад", callback_data="broadcast"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def broadcast_balance_history_text(state: dict, history: list[dict]) -> str:
    balance = int(state.get("posts") or 0)
    if not history:
        return (
            "📜 <b>История баланса</b>\n\n"
            f"Текущий баланс: <b>{balance}</b> постов\n\n"
            "История пока пустая."
        )

    lines = [
        "📜 <b>История баланса</b>",
        "",
        f"Текущий баланс: <b>{balance}</b> постов",
        "",
    ]

    for idx, item in enumerate(history, 1):
        kind = str(item.get("type") or "unknown")
        amount = item.get("amount")
        ts = _format_iso_dt_short(item.get("timestamp"))
        summary = (item.get("summary") or "").strip()

        if kind == "purchase":
            label = "🛍 Покупка"
            sign = "+"
        elif kind == "spent":
            label = "📤 Рассылка"
            sign = "-"
        elif kind == "initial_free":
            label = "🎁 Старт"
            sign = "+"
        else:
            label = f"ℹ️ {kind}"
            sign = ""

        amount_str = f"{sign}{amount}" if amount is not None else "—"
        line = f"{idx}. {label}: <b>{amount_str}</b> — {ts}"
        if summary and kind == "spent":
            line += f"\n   {summary}"
        lines.append(line)

    return "\n".join(lines)


def broadcast_balance_history_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="bc_balance")],
    ])


def broadcast_test_result_text(
    total_groups: int,
    working_groups: list,
    failed_groups: dict,
    auto_disabled_count: int,
) -> str:
    """Format test broadcast result."""
    working_count = len(working_groups)
    failed_count = len(failed_groups)

    reason_counts: dict[str, int] = {}
    for reason in failed_groups.values():
        key = str(reason or "unknown").strip() or "unknown"
        reason_counts[key] = reason_counts.get(key, 0) + 1

    text = (
        "🧪 <b>Результат тестовой рассылки</b>\n\n"
        f"Проверено: <b>{total_groups}</b>\n"
        f"Пройдено: <b>{working_count}</b>\n"
        f"Отключено автоматически: <b>{auto_disabled_count}</b>\n"
    )

    if working_count > 0:
        text += f"✅ <b>Группы, где работает ({working_count}):</b>\n"
        # Show up to 10 groups
        for group in working_groups[:10]:
            text += f"  • @{group}\n"
        if working_count > 10:
            text += f"  • ... и ещё {working_count - 10}\n"

    if failed_count > 0:
        text += f"\n❌ <b>Группы, где не работает ({failed_count}):</b>\n"
        for group, reason in list(failed_groups.items())[:10]:
            reason_emoji = "🚫" if reason == "blocked" else "⚠️"
            text += f"  {reason_emoji} @{group} ({reason})\n"
        if failed_count > 10:
            text += f"  • ... и ещё {failed_count - 10}\n"
        top_reasons = sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)[:5]
        if top_reasons:
            text += "\nПричины (топ):\n"
            for reason, count in top_reasons:
                text += f"  • {reason}: {count}\n"

    return text


def broadcast_test_result_keyboard() -> InlineKeyboardMarkup:
    """Buttons after test: add groups or proceed to mass broadcast."""
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить группы", callback_data="bc_groups")],
        [InlineKeyboardButton(text="🚀 Открыть запуск рассылки", callback_data="bc_launch_menu")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="broadcast")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


BROADCAST_GROUPS_PER_PAGE = 6


def broadcast_groups_keyboard(
    state: dict,
    *,
    groups: list[str],
    page: int = 0,
    allow_manage: bool = False,
) -> InlineKeyboardMarkup:
    campaign = state.get("campaign", {})
    selected = set(campaign.get("selected_groups", []))
    groups_state = state.get("broadcast_groups_state", {})

    total = len(groups)
    total_pages = max(1, (total + BROADCAST_GROUPS_PER_PAGE - 1) // BROADCAST_GROUPS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start = page * BROADCAST_GROUPS_PER_PAGE
    page_groups = groups[start:start + BROADCAST_GROUPS_PER_PAGE]

    buttons = []
    if allow_manage:
        buttons.append([InlineKeyboardButton(text="➕ Добавить чат/группу", callback_data="bcg_add")])
    for group in page_groups:
        group_meta = groups_state.get(group, {})
        blocked = group_meta.get("status") == "blocked"
        last_status = (group_meta.get("last_test_status") or "").strip()
        status_icon = {
            "ok": "✅",
            "failed": "🚫",
            "deleted": "🗑",
            "unknown": "⚠️",
        }.get(last_status, "")
        label = format_group_ref(group)
        if blocked:
            text = f"🚫 {status_icon} {label}".replace("  ", " ").strip()
        else:
            prefix = "✅" if group in selected else "▫️"
            text = f"{prefix} {status_icon} {label}".replace("  ", " ").strip()
        row = [InlineKeyboardButton(text=text, callback_data=f"bcg_{group}")]
        buttons.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"bcgp_{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"bcgp_{page + 1}"))
    buttons.append(nav)

    if allow_manage and total > 0:
        buttons.append([InlineKeyboardButton(text="🗑 Удалить группу", callback_data="bcg_delete_mode")])

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="broadcast")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


GROUPS_PER_PAGE = 6


def groups_keyboard(page: int) -> InlineKeyboardMarkup:
    groups = load_groups()
    total = len(groups)
    total_pages = max(1, (total + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start = page * GROUPS_PER_PAGE
    page_groups = groups[start:start + GROUPS_PER_PAGE]

    buttons = []
    buttons.append([InlineKeyboardButton(text="➕ Добавить группу", callback_data="group_add")])
    for username in page_groups:
        buttons.append([InlineKeyboardButton(text=f"@{username}", callback_data=f"group_view_{username}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"groups_page_{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"groups_page_{page + 1}"))
    buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def parse_source_input(text: str) -> tuple[str, int] | None:
    match = re.match(r"^\s*@([A-Za-z0-9_]{5,32})\s+(\d+)\s*$", text or "")
    if not match:
        return None
    return f"@{match.group(1)}", int(match.group(2))


def get_active_selected_groups(state: dict, groups: list[str]) -> list[str]:
    return get_active_selected_groups_from(state, groups)


async def execute_broadcast(
    user_id: int,
    groups: list[str],
    *,
    advance_rotation: bool,
    is_test: bool = False,
    test_marker: str = "🧪",
) -> dict:
    mgr = scoped_broadcast_manager(user_id)
    state = mgr.load()
    campaign = state.get("campaign", {})
    post = mgr.choose_next_post()
    if not post:
        return {"ok": False, "sent_count": 0, "skipped_count": len(groups), "summary": "Нет постов в пуле."}

    source_channel = str(post.get("channel") or "")
    source_message_id = int(post.get("message_id") or 0)
    send_mode = campaign.get("send_mode", "user")
    send_as = campaign.get("send_as_channel", "") if send_mode == "channel" else None

    ok, _, client, _ = await _readiness_check_connected_account(user_id)
    if not ok or not client:
        return {"ok": False, "sent_count": 0, "skipped_count": len(groups), "summary": "Аккаунт для рассылки не подключен."}

    try:
        result = await send_broadcast_campaign_with_client(
            client=client,
            groups=groups,
            source_channel=source_channel,
            source_message_id=source_message_id,
            send_as_channel=send_as,
            delay_seconds=1.5 if is_test else 5.0,
            jitter_seconds=0.5 if is_test else 1.0,
            as_copy=True,
            is_test=is_test,
            test_marker=test_marker,
        )
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    if advance_rotation and result.get("sent_count", 0) > 0:
        mgr.advance_rotation_if_sent()
    return result


def is_campaign_ready(state: dict, *, user_id: int, groups: list[str]) -> tuple[bool, str]:
    campaign = state.get("campaign", {})
    if not get_account(user_id):
        return False, "Аккаунт для рассылки не подключен. Нажмите «🔑 Подключить аккаунт»."
    posts = campaign.get("posts", []) if isinstance(campaign.get("posts", []), list) else []
    if not posts:
        return False, "Нет постов в пуле. Добавьте посты в «Посты»."
    if campaign.get("send_mode") == "channel" and not campaign.get("send_as_channel"):
        return False, "Режим 'от канала': не выбран канал send-as."
    if not get_active_selected_groups_from(state, groups):
        return False, "Нет активных выбранных групп."
    return True, ""


async def notify_owner(text: str):
    if not OWNER_IDS:
        return
    owner_id = list(OWNER_IDS)[0]
    try:
        await bot.send_message(owner_id, text, parse_mode="HTML")
    except Exception:
        pass


async def notify_user(user_id: int, text: str):
    try:
        await bot.send_message(int(user_id), text, parse_mode="HTML")
    except Exception:
        pass


async def scheduler_loop():
    while True:
        try:
            owner_id = list(OWNER_IDS)[0] if OWNER_IDS else None
            user_ids = set(list_connected_user_ids() + list_user_ids_from_disk())
            if owner_id is not None:
                user_ids.add(int(owner_id))

            for user_id in sorted(user_ids):
                bm = scoped_broadcast_manager(user_id)
                balance_mgr = scoped_balance_manager(user_id)
                groups_all = scoped_load_broadcast_groups(user_id)
                state = bm.ensure_groups_known(groups_all)

                schedule = state.get("broadcast_schedule", {})
                if not schedule.get("enabled", True):
                    continue

                tz_name = schedule.get("tz", BROADCAST_TZ)
                now_local = datetime.now(ZoneInfo(tz_name))
                date_str = now_local.strftime("%Y-%m-%d")

                weekday = WEEKDAYS[now_local.weekday()]
                weekly = state.get("weekly_schedule", {}) if isinstance(state.get("weekly_schedule", {}), dict) else {}
                meta = weekly.get(weekday) or {}
                slot = meta.get("time")
                if not (meta.get("enabled") and slot):
                    continue

                # Run only at the exact minute; if missed (downtime), skip for the day.
                if now_local.strftime("%H:%M") != slot:
                    continue

                if bm.was_slot_run(date_str, slot):
                    continue
                if broadcast_lock.locked():
                    continue

                ready, reason = is_campaign_ready(state, user_id=user_id, groups=groups_all)
                if not ready:
                    bm.mark_slot_run(date_str, slot, "skipped", reason)
                    await notify_user(user_id, f"📣 Авторассылка пропущена ({WEEKDAY_NAMES.get(weekday, weekday)} {slot}): {reason}")
                    continue

                groups = get_active_selected_groups(state, groups_all)
                if not balance_mgr.check_sufficient(len(groups)):
                    reason = "Недостаточно постов на балансе."
                    bm.mark_slot_run(date_str, slot, "skipped", reason)
                    await notify_user(user_id, "📣 Авторассылка пропущена: недостаточно постов на балансе.")
                    continue

                async with broadcast_lock:
                    # Re-check under lock to avoid races with parallel manual runs.
                    if not balance_mgr.check_sufficient(len(groups)):
                        reason = "Недостаточно постов на балансе."
                        bm.mark_slot_run(date_str, slot, "skipped", reason)
                        await notify_user(user_id, "📣 Авторассылка пропущена: недостаточно постов на балансе.")
                        continue
                    result = await execute_broadcast(user_id, groups, advance_rotation=True)
                    sent_count = int(result.get("sent_count", 0) or 0)
                    if sent_count > 0:
                        balance_mgr.spend_posts(
                            amount=sent_count,
                            groups_count=len(groups),
                            sent_count=sent_count,
                            summary=f"Авторассылка: {sent_count} групп",
                        )
                for group, err in result.get("blocked_groups", {}).items():
                    bm.set_group_blocked(group, err)

                status = "ok" if result.get("ok") else "failed"
                bm.mark_slot_run(date_str, slot, status, result.get("summary", ""))
                new_balance = balance_mgr.get_balance()
                failed_count = max(0, len(groups) - sent_count)
                analytics_text = (
                    "📊 <b>АВТОРАССЫЛКА ЗАВЕРШЕНА</b>\n"
                    f"🕐 {WEEKDAY_NAMES.get(weekday, weekday)} {date_str} {slot}\n\n"
                    f"Группы: {len(groups)}\n"
                    f"├─ ✅ Отправлено: {sent_count}\n"
                    f"└─ ❌ Не отправлено: {failed_count}\n\n"
                    f"💰 Потрачено: {sent_count} постов\n"
                    f"📉 Баланс: {new_balance} постов"
                )
                if failed_count > 0:
                    analytics_text += "\n\n⚠️ Есть неудачные группы — запустите тест."
                await notify_user(user_id, analytics_text)

                if bm.get_balance_notif_enabled():
                    threshold = bm.get_balance_notif_threshold()
                    if new_balance <= threshold and not bm.was_balance_notif_sent_today():
                        broadcasts_left = new_balance // max(1, len(groups))
                        low_balance_text = (
                            "⚠️ <b>БАЛАНС ЗАКАНЧИВАЕТСЯ</b>\n\n"
                            f"Осталось: <b>{new_balance}</b> постов\n\n"
                            "Вы можете опубликовать еще:\n"
                            f"• {broadcasts_left} рассылок по {max(1, len(groups))} групп"
                        )
                        await bot.send_message(
                            int(user_id),
                            low_balance_text,
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="💳 Купить тариф", callback_data="bc_balance")]
                            ]),
                        )
                        bm.mark_balance_notif_sent()

            await asyncio.sleep(20)
        except Exception:
            await asyncio.sleep(20)


# ─── Основные хендлеры ────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.set_state(MainMenu.viewing)
    user_id = message.from_user.id

    # Ensure balance state exists/initialized (free tier is granted only once)
    scoped_balance_manager(user_id).load()

    bm = scoped_broadcast_manager(user_id)
    bm.init_notifications()
    bm_state = bm.load()
    schedule_enabled = bm_state.get("broadcast_schedule", {}).get("enabled", True)
    cat_state = load()
    active_dir = get_active_direction(cat_state)
    active_dir_name = ""
    if active_dir:
        active_dir_name = get_directions(cat_state).get(active_dir, {}).get("name", "")
    active_channel, channel_error = resolve_results_channel_for_selection(cat_state, DEFAULT_RESULTS_CHANNEL)
    channel_text = format_channel_label(active_channel if active_channel is not None else DEFAULT_RESULTS_CHANNEL)
    if channel_error:
        channel_text += " (конфликт выбранных подкатегорий)"

    await message.answer(
        "👋 <b>Добро пожаловать в Tutor Finder Bot!</b>\n\n"
        "🎯 Я помогу тебе найти людей по ключевым словам в Telegram-группах.\n\n"
        f"📂 <b>Активное направление:</b> {active_dir_name or 'Не выбрано'}\n"
        f"📨 <b>Канал результатов:</b> <code>{channel_text}</code>\n\n"
        f"📣 Рассылка: {'включена ✅' if schedule_enabled else 'выключена ❌'}\n\n"
        "Выбери действие:",
        parse_mode="HTML",
        reply_markup=main_keyboard(schedule_enabled)
    )


@dp.callback_query(F.data == "back_main")
async def back_to_main(query: CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(MainMenu.viewing)
    bm_state = scoped_broadcast_manager(query.from_user.id).load()
    schedule_enabled = bm_state.get("broadcast_schedule", {}).get("enabled", True)
    cat_state = load()
    active_dir = get_active_direction(cat_state)
    active_name = ""
    if active_dir:
        active_name = get_directions(cat_state).get(active_dir, {}).get("name", "")

    await query.message.edit_text(
        main_menu_text(schedule_enabled, active_name),
        parse_mode="HTML",
        reply_markup=main_keyboard(schedule_enabled)
    )


@dp.callback_query(F.data == "main_bc_toggle")
async def main_bc_toggle(query: CallbackQuery):
    bm = scoped_broadcast_manager(query.from_user.id)
    bm_state = bm.load()
    schedule_enabled = bm_state.get("broadcast_schedule", {}).get("enabled", True)

    # If schedule is ON, show confirmation before stopping
    if schedule_enabled:
        confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, остановить", callback_data="main_bc_confirm_stop"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="main_bc_cancel_stop"),
            ],
        ])
        await query.message.edit_text(
            "⚠️ <b>Остановить авторассылку?</b>\n\n"
            "Расписание сохранится, но рассылка не будет отправляться автоматически.\n\n"
            "Вы сможете в любой момент возобновить рассылку.",
            parse_mode="HTML",
            reply_markup=confirm_keyboard,
        )
        await query.answer()
        return

    # If schedule is OFF, immediately resume
    bm.set_schedule_enabled(True)
    cat_state = load()
    active_dir = get_active_direction(cat_state)
    active_name = ""
    if active_dir:
        active_name = get_directions(cat_state).get(active_dir, {}).get("name", "")
    await query.message.edit_text(
        main_menu_text(True, active_name),
        parse_mode="HTML",
        reply_markup=main_keyboard(True),
    )
    await query.answer("✅ Рассылка возобновлена")


@dp.callback_query(F.data == "main_bc_confirm_stop")
async def main_bc_confirm_stop(query: CallbackQuery):
    bm = scoped_broadcast_manager(query.from_user.id)
    bm.set_schedule_enabled(False)
    cat_state = load()
    active_dir = get_active_direction(cat_state)
    active_name = ""
    if active_dir:
        active_name = get_directions(cat_state).get(active_dir, {}).get("name", "")
    await query.message.edit_text(
        main_menu_text(False, active_name),
        parse_mode="HTML",
        reply_markup=main_keyboard(False),
    )
    await query.answer("⏸ Рассылка остановлена")


@dp.callback_query(F.data == "main_bc_cancel_stop")
async def main_bc_cancel_stop(query: CallbackQuery):
    cat_state = load()
    active_dir = get_active_direction(cat_state)
    active_name = ""
    if active_dir:
        active_name = get_directions(cat_state).get(active_dir, {}).get("name", "")
    bm = scoped_broadcast_manager(query.from_user.id)
    bm_state = bm.load()
    schedule_enabled = bm_state.get("broadcast_schedule", {}).get("enabled", True)
    await query.message.edit_text(
        main_menu_text(schedule_enabled, active_name),
        parse_mode="HTML",
        reply_markup=main_keyboard(schedule_enabled),
    )
    await query.answer()


# ─── Рассылка ─────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "broadcast")
async def broadcast_menu(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    groups = scoped_load_broadcast_groups(user_id)
    state = bm.ensure_groups_known(groups)
    await query.message.edit_text(
        broadcast_summary_text(state, user_id=user_id, groups=groups),
        parse_mode="HTML",
        reply_markup=broadcast_main_keyboard(state, user_id=user_id),
    )
    await query.answer()


@dp.callback_query(F.data == "bc_launch_menu")
async def broadcast_launch_menu(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    groups = scoped_load_broadcast_groups(user_id)
    state = bm.ensure_groups_known(groups)
    await query.message.edit_text(
        broadcast_launch_text(state, user_id=user_id),
        parse_mode="HTML",
        reply_markup=broadcast_launch_keyboard(state, user_id=user_id),
    )
    await query.answer()


@dp.callback_query(F.data == "bc_launch_step_ready")
async def broadcast_launch_step_ready(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    groups = scoped_load_broadcast_groups(user_id)
    state = bm.ensure_groups_known(groups)
    campaign = state.get("campaign", {}) if isinstance(state.get("campaign", {}), dict) else {}
    selected_groups = campaign.get("selected_groups", []) if isinstance(campaign.get("selected_groups", []), list) else []
    send_mode = campaign.get("send_mode", "user")
    send_as_channel = (campaign.get("send_as_channel") or "").strip()
    snapshot = _readiness_snapshot_from_state(state)

    if not selected_groups:
        state = bm.load()
        campaign_state = state.setdefault("campaign", {})
        campaign_state["readiness_passed"] = False
        campaign_state["readiness_checked_at"] = datetime.now(timezone.utc).isoformat()
        campaign_state["readiness_problem_count"] = 1
        campaign_state["readiness_mode_snapshot"] = snapshot
        campaign_state["readiness_last_reason"] = "no_groups_selected"
        bm.save(state)
        await query.message.edit_text(
            broadcast_launch_text(state, user_id=user_id, notice=_readiness_reason_human("no_groups_selected")),
            parse_mode="HTML",
            reply_markup=broadcast_launch_keyboard(state, user_id=user_id),
        )
        await query.answer("Шаг 1 недоступен")
        return

    ok, _, client, _ = await _readiness_check_connected_account(user_id)
    if not ok or not client:
        state = bm.load()
        campaign_state = state.setdefault("campaign", {})
        campaign_state["readiness_passed"] = False
        campaign_state["readiness_checked_at"] = datetime.now(timezone.utc).isoformat()
        campaign_state["readiness_problem_count"] = 1
        campaign_state["readiness_mode_snapshot"] = snapshot
        campaign_state["readiness_last_reason"] = "not_connected"
        bm.save(state)
        await query.message.edit_text(
            broadcast_launch_text(state, user_id=user_id, notice=_readiness_reason_human("not_connected")),
            parse_mode="HTML",
            reply_markup=broadcast_launch_keyboard(state, user_id=user_id),
        )
        await query.answer("Шаг 1: аккаунт не подключен")
        return

    problems: list[tuple[str, str]] = []
    send_as_status = "ok"
    if send_mode == "channel":
        if not send_as_channel:
            send_as_status = "send_as_missing"
        else:
            try:
                ent = await client.get_entity(send_as_channel)
                ok_send_as, reason = await _telethon_can_send_to_entity(client, ent)
                if not ok_send_as:
                    send_as_status = "send_as_no_access" if reason in ("not_participant", "admin_required", "resolve_failed") else reason
            except Exception:
                send_as_status = "send_as_no_access"

    for group in selected_groups:
        try:
            entity = await client.get_entity(group)
        except Exception:
            problems.append((group, "resolve_failed"))
            continue
        ok_group, reason = await _telethon_can_send_to_entity(client, entity)
        if not ok_group:
            problems.append((group, reason))

    try:
        await client.disconnect()
    except Exception:
        pass

    total_problems = len(problems) + (1 if send_as_status != "ok" else 0)
    state = bm.load()
    campaign_state = state.setdefault("campaign", {})
    campaign_state["readiness_passed"] = total_problems == 0
    campaign_state["readiness_checked_at"] = datetime.now(timezone.utc).isoformat()
    campaign_state["readiness_problem_count"] = total_problems
    campaign_state["readiness_mode_snapshot"] = snapshot
    campaign_state["readiness_last_reason"] = "" if total_problems == 0 else (send_as_status if send_as_status != "ok" else "group_issues")
    bm.save(state)

    notice = "Готовность пройдена. Можно запускать шаг 2." if total_problems == 0 else _readiness_reason_human(campaign_state["readiness_last_reason"])
    await query.message.edit_text(
        broadcast_launch_text(state, user_id=user_id, notice=notice),
        parse_mode="HTML",
        reply_markup=broadcast_launch_keyboard(state, user_id=user_id),
    )
    await query.answer("Шаг 1 выполнен")


@dp.callback_query(F.data == "bc_launch_step_test")
async def broadcast_launch_step_test(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    groups = scoped_load_broadcast_groups(user_id)
    state = bm.ensure_groups_known(groups)
    steps = get_setup_steps(user_id, state, groups)

    if not all(bool(steps.get(key)) for key in ("account", "posts", "groups", "schedule", "readiness")):
        await query.message.edit_text(
            broadcast_launch_text(state, user_id=user_id, notice=_launch_block_reason(state, steps)),
            parse_mode="HTML",
            reply_markup=broadcast_launch_keyboard(state, user_id=user_id),
        )
        await query.answer("Шаг 2 пока недоступен")
        return

    await query.message.edit_text(
        broadcast_test_intro_text(),
        parse_mode="HTML",
        reply_markup=broadcast_test_intro_keyboard(),
    )
    await query.answer()


@dp.callback_query(F.data == "bc_launch_step_mass")
async def broadcast_launch_step_mass(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    groups = scoped_load_broadcast_groups(user_id)
    state = bm.ensure_groups_known(groups)
    steps = get_setup_steps(user_id, state, groups)

    if not bool(steps.get("test")):
        await query.message.edit_text(
            broadcast_launch_text(state, user_id=user_id, notice=_launch_block_reason(state, steps)),
            parse_mode="HTML",
            reply_markup=broadcast_launch_keyboard(state, user_id=user_id),
        )
        await query.answer("Шаг 3 пока недоступен")
        return

    await broadcast_mass(query)


@dp.callback_query(F.data == "bc_settings")
async def broadcast_settings_menu(query: CallbackQuery):
    await query.message.edit_text(
        broadcast_settings_text(),
        parse_mode="HTML",
        reply_markup=broadcast_settings_keyboard(),
    )
    await query.answer()


def _readiness_reason_label(code: str) -> str:
    mapping = {
        "ok": "✅ OK",
        "not_connected": "❌ Аккаунт не подключен",
        "send_as_missing": "❌ Не выбран send-as канал",
        "send_as_no_access": "❌ Нет доступа к send-as каналу",
        "resolve_failed": "❌ Не найден/не доступен",
        "not_participant": "🚫 Не участник",
        "restricted": "🚫 Ограничен на отправку",
        "admin_required": "🚫 Нужны права админа",
        "unknown": "⚠️ Неизвестно",
    }
    return mapping.get(code, code)


async def _telethon_can_send_to_entity(client, entity) -> tuple[bool, str]:
    """
    Лёгкая проверка (без отправки сообщений).
    Возвращает (ok, reason_code).
    """
    import telethon.errors

    try:
        perms = await client.get_permissions(entity, "me")
    except telethon.errors.UserNotParticipantError:
        return False, "not_participant"
    except Exception:
        # Некоторые приватные/недоступные чаты ломаются здесь
        return False, "resolve_failed"

    banned = getattr(perms, "banned_rights", None)
    if banned and getattr(banned, "send_messages", False):
        return False, "restricted"

    # Для каналов (broadcast=True) нужно право постинга (обычно админ)
    is_channel = bool(getattr(entity, "broadcast", False))
    if is_channel:
        if getattr(perms, "is_creator", False):
            return True, "ok"
        if getattr(perms, "is_admin", False):
            rights = getattr(perms, "admin_rights", None)
            if rights is None or getattr(rights, "post_messages", True):
                return True, "ok"
            return False, "admin_required"
        return False, "admin_required"

    # Для обычных групп/супергрупп считаем ок, если не забанен
    return True, "ok"


async def _readiness_check_connected_account(user_id: int) -> tuple[bool, str, object | None, str]:
    meta = get_account(user_id)
    if not meta:
        return False, "not_connected", None, ""
    api_id = int(meta.get("api_id") or 0)
    api_hash = (meta.get("api_hash") or "").strip()
    if not api_id or not api_hash:
        return False, "not_connected", None, ""

    client = make_client_from_string_session(api_id, api_hash, get_session_string(user_id))
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return False, "not_connected", None, ""
        me = await client.get_me()
        who = f"@{me.username}" if getattr(me, "username", None) else (getattr(me, "first_name", None) or "аккаунт")
        return True, "ok", client, who
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            pass
        return False, "not_connected", None, ""


@dp.callback_query(F.data == "bc_ready")
async def broadcast_readiness(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    groups = scoped_load_broadcast_groups(user_id)
    state = bm.ensure_groups_known(groups)
    campaign = state.get("campaign", {})
    selected_groups = campaign.get("selected_groups", []) if isinstance(campaign.get("selected_groups", []), list) else []
    send_mode = campaign.get("send_mode", "user")
    send_as_channel = (campaign.get("send_as_channel") or "").strip()
    snapshot = _readiness_snapshot_from_state(state)

    if not selected_groups:
        state = bm.load()
        campaign_state = state.setdefault("campaign", {})
        campaign_state["readiness_passed"] = False
        campaign_state["readiness_checked_at"] = datetime.now(timezone.utc).isoformat()
        campaign_state["readiness_problem_count"] = 1
        campaign_state["readiness_mode_snapshot"] = snapshot
        campaign_state["readiness_last_reason"] = "no_groups_selected"
        bm.save(state)
        await query.answer("Сначала выберите группы рассылки.", show_alert=True)
        return

    ok, _, client, who = await _readiness_check_connected_account(user_id)
    if not ok or not client:
        state = bm.load()
        campaign_state = state.setdefault("campaign", {})
        campaign_state["readiness_passed"] = False
        campaign_state["readiness_checked_at"] = datetime.now(timezone.utc).isoformat()
        campaign_state["readiness_problem_count"] = 1
        campaign_state["readiness_mode_snapshot"] = snapshot
        campaign_state["readiness_last_reason"] = "not_connected"
        bm.save(state)
        await query.message.edit_text(
            "🧭 <b>Готовность</b>\n\n"
            "❌ Аккаунт для рассылки не подключен.\n\n"
            "Нажмите <b>🔑 Подключить аккаунт</b> и подключите отдельный Telegram-аккаунт (телефон/QR).",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔑 Подключить аккаунт", callback_data="acc_menu")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="broadcast")],
            ]),
        )
        await query.answer()
        return

    problems: list[tuple[str, str]] = []
    oks: list[str] = []

    # Check send-as channel if needed
    send_as_status = "ok"
    send_as_note = ""
    if send_mode == "channel":
        if not send_as_channel:
            send_as_status = "send_as_missing"
        else:
            try:
                ent = await client.get_entity(send_as_channel)
                ok_send_as, reason = await _telethon_can_send_to_entity(client, ent)
                if not ok_send_as:
                    send_as_status = "send_as_no_access" if reason in ("not_participant", "admin_required", "resolve_failed") else reason
            except Exception:
                send_as_status = "send_as_no_access"

        if send_as_status != "ok":
            send_as_note = f"\n{_readiness_reason_label(send_as_status)}: <code>{send_as_channel or 'не выбран'}</code>"

    # Check each selected group
    for group in selected_groups:
        try:
            entity = await client.get_entity(group)
        except Exception:
            problems.append((group, "resolve_failed"))
            continue

        ok_group, reason = await _telethon_can_send_to_entity(client, entity)
        if ok_group:
            oks.append(group)
        else:
            problems.append((group, reason))

    await client.disconnect()

    total_problems = len(problems) + (1 if send_as_status != "ok" else 0)
    state = bm.load()
    campaign_state = state.setdefault("campaign", {})
    campaign_state["readiness_passed"] = total_problems == 0
    campaign_state["readiness_checked_at"] = datetime.now(timezone.utc).isoformat()
    campaign_state["readiness_problem_count"] = total_problems
    campaign_state["readiness_mode_snapshot"] = snapshot
    campaign_state["readiness_last_reason"] = "" if total_problems == 0 else (send_as_status if send_as_status != "ok" else "group_issues")
    bm.save(state)

    # Render summary
    lines = [
        "🧭 <b>Готовность</b>\n",
        f"Аккаунт: <b>{who}</b>",
        f"Режим: <b>{'от канала' if send_mode == 'channel' else 'от пользователя'}</b>",
        f"Групп выбрано: <b>{len(selected_groups)}</b>",
        f"OK: <b>{len(oks)}</b>",
        f"Проблемы: <b>{total_problems}</b>",
    ]
    if send_as_note:
        lines.append(send_as_note)

    if problems:
        lines.append("\n<b>Проблемные группы (первые 12):</b>")
        for group, reason in problems[:12]:
            lines.append(f"- <code>@{group}</code> — {_readiness_reason_label(reason)}")
        if len(problems) > 12:
            lines.append(f"…и ещё <b>{len(problems) - 12}</b>")

    lines.append("\nЧто делать:")
    lines.append("- добавьте аккаунт в проблемные группы и снимите ограничения")
    if send_mode == "channel":
        lines.append("- добавьте аккаунт админом в send-as канал и выдайте право постинга")

    await query.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Обновить", callback_data="bc_ready")],
            [InlineKeyboardButton(text="🔑 Аккаунт", callback_data="acc_menu")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="broadcast")],
        ]),
    )
    await query.answer()


def account_menu_text(user_id: int) -> str:
    meta = get_account(user_id)
    if not meta:
        return (
            "🔑 <b>Подключить аккаунт</b>\n\n"
            "Аккаунт не подключен.\n\n"
            "Рекомендация: подключайте <b>отдельный</b> аккаунт Telegram для рассылок."
        )
    username = (meta.get("username") or "").strip()
    name = (meta.get("first_name") or "").strip()
    who = f"@{username}" if username else (name or "аккаунт")
    phone_masked = meta.get("phone_mask") or ""
    connected_at = meta.get("connected_at") or ""
    return (
        "🔑 <b>Подключить аккаунт</b>\n\n"
        "Статус: <b>подключен</b>\n"
        f"Аккаунт: <b>{who}</b>\n"
        f"Телефон: <code>{phone_masked}</code>\n"
        f"Дата: <code>{connected_at}</code>"
    )


def account_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    connected = bool(get_account(user_id))
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton(text="🧩 Методы подключения", callback_data="acc_methods")])
    if connected:
        rows.append([InlineKeyboardButton(text="🔎 Проверить доступ", callback_data="acc_check")])
        rows.append([InlineKeyboardButton(text="🗑 Отключить", callback_data="acc_disconnect")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="broadcast")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_methods_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 Подключение по QR-коду (15 сек)", callback_data="acc_qr")],
        [InlineKeyboardButton(text="🪪 Подключение по API ID / API Hash (2 мин)", callback_data="acc_api")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="acc_menu")],
    ])


def account_warning_page_text(page: int) -> str:
    """Get warning page text (page 1, 2, or 3)"""
    if page == 1:
        return (
            "🚨 <b>САМЫЙ ВАЖНЫЙ ШАГ ИЗ ВСЕХ</b> 🚨\n\n"
            "⚠️ ЭТО САМОЕ ВАЖНОЕ РЕШЕНИЕ ПЕРЕД ПОДКЛЮЧЕНИЕМ\n\n"
            "──────────────────────────────\n"
            "🔴 <b>ЖИЗНЕННО ВАЖНАЯ РЕКОМЕНДАЦИЯ:</b>\n\n"
            "<b>СОЗДАЙТЕ ОТДЕЛЬНЫЙ TELEGRAM-АККАУНТ</b> на другой номер телефона\n"
            "специально для рассылок.\n\n"
            "<b>ЗАТЕМ ПОДКЛЮЧИТЕ ЭТОТ НОВЫЙ АККАУНТ К БОТУ (НЕ ОСНОВНОЙ).</b>\n"
            "──────────────────────────────\n\n"
            "<b>При подключении аккаунта бот работает от вашего имени</b>\n"
            "через официальный Telegram API.\n\n"
            "<b>Бот технически получает доступ к:</b>\n"
            "📩 личным сообщениям\n"
            "👥 контактам и группам\n"
            "📁 файлам и медиа в ваших чатах\n\n"
            "Это неизбежно при подключении любого аккаунта.\n\n"
            "Страница 1 из 3"
        )
    elif page == 2:
        return (
            "❌ <b>ЧТО ПРОИЗОЙДЁТ, ЕСЛИ ИСПОЛЬЗОВАТЬ ОСНОВНОЙ АККАУНТ?</b>\n\n"
            "1️⃣ <b>Бот видит все ваши личные данные</b>\n"
            "   Личные сообщения, контакты, файлы — всё доступно боту\n\n"
            "2️⃣ <b>Telegram заблокирует аккаунт на 24 часа</b>\n"
            "   (или больше, если жалоб много)\n\n"
            "3️⃣ <b>Вы не сможете отправлять сообщения людям</b>\n"
            "   Личные чаты, группы, друзья — всё заблокировано на время блокировки\n\n"
            "───────────────────────────────\n\n"
            "Это НЕ гарантия, что произойдёт.\n"
            "Но это реальный риск, который вы берёте на себя.\n\n"
            "Страница 2 из 3"
        )
    else:  # page == 3
        return (
            "✅ <b>ПОЧЕМУ НУЖЕН ОТДЕЛЬНЫЙ АККАУНТ?</b>\n\n"
            "Если рассылка идёт с отдельного аккаунта:\n"
            "✓ Бот видит только то, что в отдельном аккаунте (практически ничего)\n"
            "✓ Основной аккаунт и все ваши данные остаются приватными\n"
            "✓ Основной аккаунт остаётся свободным для общения\n"
            "✓ Блокировка рассылочного аккаунта вас не затронет\n\n"
            "───────────────────────────────\n"
            "📱 <b>КАК СДЕЛАТЬ ПРАВИЛЬНО:</b>\n\n"
            "<b>1. Создайте новый Telegram-аккаунт</b> (на отдельный номер)\n"
            "<b>2. С ЭТОГО НОВОГО АККАУНТА откройте бот и подключите его</b>\n"
            "<b>3. Ваш основной аккаунт остаётся полностью приватным</b>\n\n"
            "Это займёт 5 минут. Номер телефона можно купить дёшево.\n\n"
            "───────────────────────────────\n\n"
            "Если вы всё прочитали и готовы создать отдельный аккаунт —\n"
            "нажмите кнопку ниже.\n\n"
            "Страница 3 из 3"
        )


def account_warning_keyboard(page: int) -> InlineKeyboardMarkup:
    """Get warning page keyboard with navigation buttons"""
    rows: list[list[InlineKeyboardButton]] = []

    if page == 1:
        rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="broadcast"),
                     InlineKeyboardButton(text="Далее →", callback_data="acc_warn_page_2")])
    elif page == 2:
        rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="acc_warn_page_1"),
                     InlineKeyboardButton(text="Далее →", callback_data="acc_warn_page_3")])
    else:  # page == 3
        rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="acc_warn_page_2"),
                     InlineKeyboardButton(text="✅ Прочитал/а", callback_data="acc_warn_complete")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="acc_cancel")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="acc_menu")],
    ])


async def _cleanup_pending_login(user_id: int) -> None:
    pending = pending_logins.pop(user_id, None)
    if not pending:
        return
    if pending.bg_task and not pending.bg_task.done():
        pending.bg_task.cancel()
    try:
        await pending.client.disconnect()
    except Exception:
        pass


async def _safe_edit_text(msg: Message, text: str, *, parse_mode: str = "HTML", reply_markup=None, disable_web_page_preview: bool = False) -> None:
    """Edit message text, or delete+reply if the message is a photo/media (e.g. QR code)."""
    if msg.photo or msg.document or msg.sticker or msg.video or msg.animation:
        try:
            await msg.delete()
        except Exception:
            pass
        await msg.answer(text, parse_mode=parse_mode, reply_markup=reply_markup, disable_web_page_preview=disable_web_page_preview)
    else:
        await msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup, disable_web_page_preview=disable_web_page_preview)


@dp.callback_query(F.data == "acc_menu")
async def account_menu(query: CallbackQuery, state: FSMContext):
    await _cleanup_pending_login(query.from_user.id)
    await state.set_state(MainMenu.viewing)
    await _safe_edit_text(
        query.message,
        account_menu_text(query.from_user.id),
        reply_markup=account_menu_keyboard(query.from_user.id),
        disable_web_page_preview=True,
    )
    await query.answer()


@dp.callback_query(F.data == "acc_methods")
async def account_methods(query: CallbackQuery, state: FSMContext):
    await _cleanup_pending_login(query.from_user.id)
    await state.set_state(MainMenu.viewing_account_warning)
    # Store current page in context data
    await state.update_data(warning_page=1)
    await _safe_edit_text(
        query.message,
        account_warning_page_text(1),
        reply_markup=account_warning_keyboard(1),
        disable_web_page_preview=True,
    )
    await query.answer()


@dp.callback_query(F.data == "acc_warn_page_2")
async def account_warning_page_2(query: CallbackQuery, state: FSMContext):
    await state.update_data(warning_page=2)
    await _safe_edit_text(
        query.message,
        account_warning_page_text(2),
        reply_markup=account_warning_keyboard(2),
        disable_web_page_preview=True,
    )
    await query.answer()


@dp.callback_query(F.data == "acc_warn_page_1")
async def account_warning_page_1(query: CallbackQuery, state: FSMContext):
    await state.update_data(warning_page=1)
    await _safe_edit_text(
        query.message,
        account_warning_page_text(1),
        reply_markup=account_warning_keyboard(1),
        disable_web_page_preview=True,
    )
    await query.answer()


@dp.callback_query(F.data == "acc_warn_page_3")
async def account_warning_page_3(query: CallbackQuery, state: FSMContext):
    await state.update_data(warning_page=3)
    await _safe_edit_text(
        query.message,
        account_warning_page_text(3),
        reply_markup=account_warning_keyboard(3),
        disable_web_page_preview=True,
    )
    await query.answer()


@dp.callback_query(F.data == "acc_warn_complete")
async def account_warning_complete(query: CallbackQuery, state: FSMContext):
    """User clicked 'Прочитал/а' button - show methods"""
    data = await state.get_data()
    current_page = data.get("warning_page", 1)

    # Only allow completion from page 3
    if current_page != 3:
        await query.answer("⚠️ Вы не прочитали все условия. Дочитайте до конца.", show_alert=True)
        return

    await state.set_state(MainMenu.viewing)
    await _safe_edit_text(
        query.message,
        "🧩 <b>Выберите метод подключения аккаунта</b>\n\n"
        "1) QR — отсканируйте код из Telegram\n"
        "2) API ID/Hash — расширенный режим\n\n"
        f"Таймаут кода: <b>{code_ttl_seconds()} сек</b> (я проверяю своим таймером).",
        reply_markup=account_methods_keyboard(),
        disable_web_page_preview=True,
    )
    await query.answer()


@dp.callback_query(F.data == "acc_cancel")
async def account_cancel(query: CallbackQuery, state: FSMContext):
    await _cleanup_pending_login(query.from_user.id)
    await state.set_state(MainMenu.viewing)
    await _safe_edit_text(
        query.message,
        account_menu_text(query.from_user.id),
        reply_markup=account_menu_keyboard(query.from_user.id),
    )
    await query.answer("Отменено")


@dp.callback_query(F.data == "acc_phone")
async def account_connect_phone_prompt(query: CallbackQuery, state: FSMContext):
    await _cleanup_pending_login(query.from_user.id)
    await query.message.edit_text(
        "📱 <b>Подключение по номеру</b>\n\n"
        "Отправьте номер в формате <code>+79990001122</code>.\n\n"
        "Совет: используйте отдельный аккаунт для рассылок.",
        parse_mode="HTML",
        reply_markup=account_cancel_keyboard(),
        disable_web_page_preview=True,
    )
    await state.set_state(MainMenu.connecting_account_phone)
    await query.answer()


@dp.callback_query(F.data == "acc_api")
async def account_connect_api_prompt(query: CallbackQuery, state: FSMContext):
    await _cleanup_pending_login(query.from_user.id)
    await query.answer()

    assets = Path(__file__).parent / "assets"

    media_group = [
        InputMediaPhoto(
            media=FSInputFile(assets / "Captura de pantalla 2026-04-12 221400.png"),
            caption="<b>Шаг 1.</b> Откройте my.telegram.org в браузере",
            parse_mode="HTML",
        ),
        InputMediaPhoto(
            media=FSInputFile(assets / "Captura de pantalla 2026-04-12 221723.png"),
            caption="<b>Шаг 2.</b> Введите полученный код",
            parse_mode="HTML",
        ),
        InputMediaPhoto(
            media=FSInputFile(assets / "Captura de pantalla 2026-04-12 221839.png"),
            caption="<b>Шаг 3.</b> Скопируйте App api_id и App api_hash",
            parse_mode="HTML",
        ),
    ]
    await query.message.answer_media_group(media_group)

    await query.message.answer(
        "🪪 <b>Подключение по API</b>\n\n"
        "1. Откройте <b>my.telegram.org</b> в браузере. Введите номер телефона и нажмите <b>Next</b>. Вам придёт код в Telegram — введите его на сайте.\n\n"
        "2. Вам придёт сообщение от <b>Telegram</b> с кодом. Введите его на сайте my.telegram.org, чтобы войти.\n\n"
        "3. После входа нажмите <b>API development tools</b>. Вы увидите <b>App api_id</b> (число) и <b>App api_hash</b> (длинная строка).\n\n"
        "Отправьте оба значения вместе:\n\n"
        "<b>Вариант 1 (через запятую):</b>\n"
        "<code>30705626, 0123456789abcdef0123456789abcdef</code>\n\n"
        "<b>Вариант 2 (на двух строках):</b>\n"
        "<code>30705626\n0123456789abcdef0123456789abcdef</code>",
        parse_mode="HTML",
        reply_markup=account_cancel_keyboard(),
    )

    await state.set_state(MainMenu.connecting_account_api)


@dp.callback_query(F.data == "acc_qr")
async def account_connect_qr(query: CallbackQuery, state: FSMContext):
    await _cleanup_pending_login(query.from_user.id)
    await _start_qr_login(query, state, refresh=False)


@dp.callback_query(F.data == "acc_qr_refresh")
async def account_connect_qr_refresh(query: CallbackQuery, state: FSMContext):
    await _cleanup_pending_login(query.from_user.id)
    await _start_qr_login(query, state, refresh=True)


@dp.callback_query(F.data == "acc_qr_check")
async def account_connect_qr_check(query: CallbackQuery, state: FSMContext):
    import telethon.errors

    user_id = query.from_user.id
    pending = pending_logins.get(user_id)
    if not pending or pending.method != "qr" or not pending.qr_login:
        await query.answer("Нет активного QR.", show_alert=True)
        return

    if datetime.now(timezone.utc) > pending.expires_at:
        await _cleanup_pending_login(user_id)
        await query.answer("Таймаут QR. Создайте заново.", show_alert=True)
        return

    await query.answer("⏳ Проверяю статус QR...")

    # Сначала пробуем короткое ожидание, но если оно не удалось — пробуем напрямую вызвать get_me()
    try:
        # Попробуем ждать, но с коротким таймаутом
        await asyncio.wait_for(pending.qr_login.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        # Может быть, уже авторизован несмотря на timeout — проверим напрямую
        pass
    except Exception:
        pass

    # Теперь проверяем авторизацию напрямую
    try:
        me = await asyncio.wait_for(pending.client.get_me(), timeout=5.0)
        # Успешно — авторизован, финализируем
        await _finalize_account_login(query.message, state, user_id=user_id, phone="")
        await query.answer("✅ QR отсканирован и авторизован!", show_alert=True)

    except telethon.errors.SessionPasswordNeededError:
        # Требуется пароль 2FA
        if user_id in OWNER_IDS and OWNER_2FA_PASSWORD:
            try:
                await pending.client.sign_in(password=OWNER_2FA_PASSWORD)
                await _finalize_account_login(query.message, state, user_id=user_id, phone="")
                await query.answer("✅ QR авторизован! (пароль введён автоматически)", show_alert=True)
                return
            except telethon.errors.PasswordHashInvalidError:
                await query.answer("❌ OWNER_2FA_PASSWORD неверный!", show_alert=True)
                await _cleanup_pending_login(user_id)
                return
            except Exception as exc:
                await query.answer(f"❌ Ошибка пароля: {type(exc).__name__}", show_alert=True)
                await _cleanup_pending_login(user_id)
                return

        # Обычный пользователь — просим ввести пароль
        await state.set_state(MainMenu.connecting_account_password)
        await _safe_edit_text(
            query.message,
            "🔐 <b>QR отсканирован!</b>\n\n"
            "Но этот аккаунт защищен паролем 2FA.\n\n"
            "Введите пароль (не код, а именно пароль):",
            reply_markup=account_cancel_keyboard(),
        )
        await query.answer("Требуется пароль", show_alert=True)

    except asyncio.TimeoutError:
        await query.answer("⏱ Не удалось подтвердить QR. Попробуйте снова.", show_alert=True)

    except Exception as exc:
        await _cleanup_pending_login(user_id)
        await query.answer(f"❌ Ошибка: {type(exc).__name__}", show_alert=True)


@dp.callback_query(F.data == "acc_disconnect")
async def account_disconnect(query: CallbackQuery, state: FSMContext):
    user_id = query.from_user.id
    await _cleanup_pending_login(user_id)
    existed = disconnect_account(user_id)

    await state.set_state(MainMenu.viewing)
    await _safe_edit_text(
        query.message,
        account_menu_text(user_id),
        reply_markup=account_menu_keyboard(user_id),
    )
    await query.answer("Отключено" if existed else "Не было подключения")


@dp.callback_query(F.data == "acc_check")
async def account_check(query: CallbackQuery):
    meta = get_account(query.from_user.id)
    if not meta:
        await query.answer("Аккаунт не подключен.", show_alert=True)
        return

    api_id = int(meta.get("api_id") or 0)
    api_hash = (meta.get("api_hash") or "").strip()
    if not api_id or not api_hash:
        await query.answer("Нет api_id/api_hash для проверки. Переподключите аккаунт.", show_alert=True)
        return

    try:
        client = make_client_from_string_session(api_id, api_hash, get_session_string(query.from_user.id))
        await client.connect()
        me = await client.get_me()
        await client.disconnect()
        who = f"@{me.username}" if getattr(me, "username", None) else (getattr(me, "first_name", None) or "аккаунт")
        await query.answer(f"OK: {who}", show_alert=True)
    except Exception:
        await query.answer("Не удалось проверить сессию. Переподключите аккаунт.", show_alert=True)


@dp.message(MainMenu.connecting_account_api)
async def account_connect_api_id_input(message: Message, state: FSMContext):
    """Получаем api_id и api_hash в одном сообщении."""
    raw = (message.text or "").strip()

    # Попытка 1: разделитель запятая
    if "," in raw:
        parts = [p.strip() for p in raw.split(",", 1)]
    # Попытка 2: разделитель новая строка
    elif "\n" in raw:
        parts = [p.strip() for p in raw.split("\n", 1)]
    else:
        parts = []

    # Валидация
    error_msg = None
    if len(parts) != 2:
        error_msg = "❌ Нужны два значения: api_id и api_hash.\n\n"
    else:
        api_id_str, api_hash = parts
        try:
            api_id = int(api_id_str)
            if api_id <= 0:
                raise ValueError
        except ValueError:
            error_msg = "❌ <b>api_id</b> должно быть положительным числом.\n\n"

        if not error_msg and (len(api_hash) < 10 or " " in api_hash):
            error_msg = "❌ <b>api_hash</b> — строка без пробелов, минимум 10 символов.\n\n"

    if error_msg:
        await message.answer(
            error_msg +
            "<b>Вариант 1 (через запятую):</b>\n"
            "<code>30705626, 0123456789abcdef0123456789abcdef</code>\n\n"
            "<b>Вариант 2 (на двух строках):</b>\n"
            "<code>30705626\n0123456789abcdef0123456789abcdef</code>",
            parse_mode="HTML",
            reply_markup=account_cancel_keyboard(),
        )
        return

    # Успех: сохраняем оба значения и переходим к вводу телефона
    await state.update_data(api_id=api_id, api_hash=api_hash)
    await state.set_state(MainMenu.connecting_account_api_phone)
    await message.answer(
        "✅ api_id и api_hash приняты.\n\n"
        "📱 <b>Номер телефона</b>\n\n"
        "Введите номер телефона того аккаунта, который подключаете.\n"
        "Формат: международный, с <b>+</b> в начале.\n\n"
        "Пример: <code>+34604288463</code> или <code>+79990001122</code>",
        parse_mode="HTML",
        reply_markup=account_cancel_keyboard(),
    )


@dp.message(MainMenu.connecting_account_api_hash)
async def account_connect_api_hash_input(message: Message, state: FSMContext):
    """Шаг 2: получаем api_hash."""
    raw = (message.text or "").strip()
    if len(raw) < 10 or " " in raw:
        await message.answer(
            "❌ <b>api_hash</b> — длинная строка без пробелов.\n\n"
            "Скопируйте точно из поля <b>App api_hash</b> на сайте.\n"
            "Пример: <code>0123456789abcdef0123456789abcdef</code>",
            parse_mode="HTML",
            reply_markup=account_cancel_keyboard(),
        )
        return
    await state.update_data(api_hash=raw)
    await state.set_state(MainMenu.connecting_account_api_phone)
    await message.answer(
        "✅ api_hash принят.\n\n"
        "📱 <b>Шаг 3 из 3 — Номер телефона</b>\n\n"
        "Введите номер телефона того аккаунта, который подключаете.\n"
        "Формат: международный, с <b>+</b> в начале.\n\n"
        "Пример: <code>+34604288463</code> или <code>+79990001122</code>",
        parse_mode="HTML",
        reply_markup=account_cancel_keyboard(),
    )


@dp.message(MainMenu.connecting_account_api_phone)
async def account_connect_api_phone_input(message: Message, state: FSMContext):
    """Шаг 3: получаем телефон, запускаем вход."""
    phone = (message.text or "").strip()
    data = await state.get_data()
    api_id = data.get("api_id", 0)
    api_hash = data.get("api_hash", "")
    await _start_phone_login(message, state, api_id=api_id, api_hash=api_hash, phone=phone)


@dp.message(MainMenu.connecting_account_phone)
async def account_connect_phone_input(message: Message, state: FSMContext):
    phone = (message.text or "").strip()
    api_id = int(os.getenv("TG_API_ID", "0") or "0")
    api_hash = (os.getenv("TG_API_HASH") or "").strip()
    await _start_phone_login(message, state, api_id=api_id, api_hash=api_hash, phone=phone)


async def _start_phone_login(message: Message, state: FSMContext, *, api_id: int, api_hash: str, phone: str) -> None:
    user_id = message.from_user.id
    await _cleanup_pending_login(user_id)

    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) < 7:
        await message.answer("❌ Номер не похож на телефон. Пример: <code>+79990001122</code>", parse_mode="HTML", reply_markup=account_cancel_keyboard())
        return
    if not api_id or not api_hash:
        await message.answer("❌ Не настроены api_id/api_hash для подключения.", reply_markup=account_cancel_keyboard())
        return

    import telethon.errors

    client = make_client_from_string_session(api_id, api_hash)
    try:
        await client.connect()
        await client.send_code_request(phone)
    except telethon.errors.PhoneNumberInvalidError:
        await client.disconnect()
        await message.answer("❌ Неверный номер.", reply_markup=account_cancel_keyboard())
        return
    except telethon.errors.PhoneNumberBannedError:
        await client.disconnect()
        await message.answer("❌ Номер заблокирован Telegram.", reply_markup=account_cancel_keyboard())
        return
    except telethon.errors.FloodWaitError as exc:
        await client.disconnect()
        await message.answer(f"❌ FloodWait: попробуйте позже ({exc.seconds} сек).", reply_markup=account_cancel_keyboard())
        return
    except Exception as exc:
        await client.disconnect()
        await message.answer(f"❌ Ошибка отправки кода: {type(exc).__name__}", reply_markup=account_cancel_keyboard())
        return

    pending = new_pending_login(method="phone", api_id=api_id, api_hash=api_hash, phone=phone, client=client)
    pending_logins[user_id] = pending
    await state.set_state(MainMenu.connecting_account_code)
    await message.answer(
        "✅ Код отправлен.\n\n"
        "Введите код (обычно 5 цифр), пример: <code>12345</code>\n\n"
        "⚠️ <b>Важно:</b> читайте код из <b>уведомления</b> (не открывая чат Telegram). "
        "Если вы откроете сообщение в приложении — код сразу истечёт.\n\n"
        f"Я жду код <b>{code_ttl_seconds()} сек</b>, потом попрошу запросить заново.",
        parse_mode="HTML",
        reply_markup=account_cancel_keyboard(),
    )


@dp.message(MainMenu.connecting_account_code)
async def account_connect_code_input(message: Message, state: FSMContext):
    import telethon.errors

    user_id = message.from_user.id
    pending = pending_logins.get(user_id)
    if not pending:
        await state.set_state(MainMenu.viewing)
        await message.answer("❌ Нет активного подключения.", reply_markup=account_menu_keyboard(user_id))
        return

    if datetime.now(timezone.utc) > pending.expires_at:
        await _cleanup_pending_login(user_id)
        await state.set_state(MainMenu.viewing)
        await message.answer("❌ Таймаут. Запросите код заново.", reply_markup=account_menu_keyboard(user_id))
        return

    code = "".join(ch for ch in (message.text or "") if ch.isdigit())
    if len(code) < 4 or len(code) > 8:
        await message.answer("❌ Введите только цифры, например <code>12345</code>.", parse_mode="HTML", reply_markup=account_cancel_keyboard())
        return

    try:
        await pending.client.sign_in(phone=pending.phone, code=code)
    except telethon.errors.SessionPasswordNeededError:
        await state.set_state(MainMenu.connecting_account_password)
        await message.answer("🔐 Включена 2FA. Введите пароль:", reply_markup=account_cancel_keyboard())
        return
    except telethon.errors.PhoneCodeInvalidError:
        await message.answer("❌ Неверный код. Попробуйте ещё раз.", reply_markup=account_cancel_keyboard())
        return
    except telethon.errors.PhoneCodeExpiredError:
        await _cleanup_pending_login(user_id)
        await state.set_state(MainMenu.viewing)
        await message.answer("❌ Код истёк. Запросите заново.", reply_markup=account_menu_keyboard(user_id))
        return
    except Exception as exc:
        await message.answer(f"❌ Ошибка авторизации: {type(exc).__name__}", reply_markup=account_cancel_keyboard())
        return

    await _finalize_account_login(message, state, user_id=user_id, phone=pending.phone)


@dp.message(MainMenu.connecting_account_password)
async def account_connect_password_input(message: Message, state: FSMContext):
    import telethon.errors

    user_id = message.from_user.id
    pending = pending_logins.get(user_id)
    if not pending:
        await state.set_state(MainMenu.viewing)
        await message.answer("❌ Нет активного подключения.", reply_markup=account_menu_keyboard(user_id))
        return

    if datetime.now(timezone.utc) > pending.expires_at:
        await _cleanup_pending_login(user_id)
        await state.set_state(MainMenu.viewing)
        await message.answer("❌ Таймаут. Запросите код заново.", reply_markup=account_menu_keyboard(user_id))
        return

    password = (message.text or "").strip()
    if not password:
        await message.answer("❌ Пароль пуст.", reply_markup=account_cancel_keyboard())
        return

    try:
        await pending.client.sign_in(password=password)
    except telethon.errors.PasswordHashInvalidError:
        await message.answer("❌ Неверный пароль. Попробуйте ещё раз.", reply_markup=account_cancel_keyboard())
        return
    except Exception as exc:
        await message.answer(f"❌ Ошибка 2FA: {type(exc).__name__}", reply_markup=account_cancel_keyboard())
        return

    await _finalize_account_login(message, state, user_id=user_id, phone=pending.phone)


async def _finalize_account_login(message: Message, state: FSMContext, *, user_id: int, phone: str) -> None:
    pending = pending_logins.get(user_id)
    if not pending:
        return

    me = None
    try:
        me = await pending.client.get_me()
    except Exception:
        me = None

    session_string = extract_session_string(pending.client)

    set_connected_account(
        user_id=user_id,
        phone=phone,
        api_id=pending.api_id,
        api_hash=pending.api_hash,
        me_id=getattr(me, "id", None) if me else None,
        username=getattr(me, "username", None) if me else None,
        first_name=getattr(me, "first_name", None) if me else None,
        session_string=session_string,
    )
    invalidate_readiness_if_needed(scoped_broadcast_manager(user_id), reason="account_reconnected")

    await _cleanup_pending_login(user_id)
    await state.set_state(MainMenu.viewing)
    await message.answer("✅ Аккаунт подключен.", reply_markup=account_menu_keyboard(user_id))


async def _qr_auto_watch(user_id: int, chat_id: int, qr_message_id: int) -> None:
    """Background task: auto-detect QR scan and finalize login (with 2FA support)."""
    import telethon.errors

    pending = pending_logins.get(user_id)
    if not pending or pending.method != "qr" or not pending.qr_login:
        return

    ttl = max((pending.expires_at - datetime.now(timezone.utc)).total_seconds(), 1.0)

    try:
        await asyncio.wait_for(pending.qr_login.wait(), timeout=ttl)

    except asyncio.CancelledError:
        return

    except asyncio.TimeoutError:
        pending_logins.pop(user_id, None)
        try:
            await bot.send_message(
                chat_id,
                "⏰ QR-код истёк. Нажмите <b>Обновить QR</b> или вернитесь назад.",
                parse_mode="HTML",
                reply_markup=account_menu_keyboard(user_id),
            )
        except Exception:
            pass
        return

    except Exception:
        return

    # QR scanned — проверяем требует ли пароль
    pending = pending_logins.get(user_id)
    if not pending:
        return

    # Попытаемся получить мета-информацию — это выкинет ошибку если требуется пароль
    try:
        me = await pending.client.get_me()
        # Успешно получили — авторизован, финализируем
        session_string = extract_session_string(pending.client)

        set_connected_account(
            user_id=user_id,
            phone="",
            api_id=pending.api_id,
            api_hash=pending.api_hash,
            me_id=getattr(me, "id", None) if me else None,
            username=getattr(me, "username", None) if me else None,
            first_name=getattr(me, "first_name", None) if me else None,
            session_string=session_string,
        )
        invalidate_readiness_if_needed(scoped_broadcast_manager(user_id), reason="account_reconnected")

        pending_logins.pop(user_id, None)
        try:
            await pending.client.disconnect()
        except Exception:
            pass

        key = StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=user_id)
        ctx = FSMContext(storage=dp.storage, key=key)
        await ctx.set_state(MainMenu.viewing)

        try:
            await bot.delete_message(chat_id, qr_message_id)
        except Exception:
            pass

        await bot.send_message(
            chat_id,
            "✅ Аккаунт подключен!",
            reply_markup=account_menu_keyboard(user_id),
        )

    except telethon.errors.SessionPasswordNeededError:
        # Требуется пароль 2FA
        # Если это админ и есть переменная пароля — используем её автоматически
        if user_id in OWNER_IDS and OWNER_2FA_PASSWORD:
            try:
                await pending.client.sign_in(password=OWNER_2FA_PASSWORD)
                # Пароль прошёл — финализируем
                me = None
                try:
                    me = await pending.client.get_me()
                except Exception:
                    pass

                session_string = extract_session_string(pending.client)

                set_connected_account(
                    user_id=user_id,
                    phone="",
                    api_id=pending.api_id,
                    api_hash=pending.api_hash,
                    me_id=getattr(me, "id", None) if me else None,
                    username=getattr(me, "username", None) if me else None,
                    first_name=getattr(me, "first_name", None) if me else None,
                    session_string=session_string,
                )
                invalidate_readiness_if_needed(scoped_broadcast_manager(user_id), reason="account_reconnected")

                pending_logins.pop(user_id, None)
                try:
                    await pending.client.disconnect()
                except Exception:
                    pass

                key = StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=user_id)
                ctx = FSMContext(storage=dp.storage, key=key)
                await ctx.set_state(MainMenu.viewing)

                try:
                    await bot.delete_message(chat_id, qr_message_id)
                except Exception:
                    pass

                await bot.send_message(
                    chat_id,
                    "✅ Аккаунт подключен! (пароль введён автоматически)",
                    reply_markup=account_menu_keyboard(user_id),
                )
                return

            except telethon.errors.PasswordHashInvalidError:
                # Неверный пароль в переменной
                await bot.send_message(
                    chat_id,
                    "❌ Пароль из OWNER_2FA_PASSWORD неверный.\n\n"
                    "Проверьте переменную в Railway.",
                    reply_markup=account_menu_keyboard(user_id),
                )
                pending_logins.pop(user_id, None)
                return
            except Exception as exc:
                await bot.send_message(
                    chat_id,
                    f"❌ Ошибка при вводе пароля: {type(exc).__name__}",
                    reply_markup=account_menu_keyboard(user_id),
                )
                pending_logins.pop(user_id, None)
                return

        # Не админ или нет переменной — просим ввести вручную
        key = StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=user_id)
        ctx = FSMContext(storage=dp.storage, key=key)
        await ctx.set_state(MainMenu.connecting_account_password)
        try:
            await bot.delete_message(chat_id, qr_message_id)
        except Exception:
            pass
        await bot.send_message(
            chat_id,
            "🔐 <b>QR отсканирован!</b>\n\n"
            "Но этот аккаунт защищен паролем 2FA.\n\n"
            "Введите пароль (не код, а именно пароль):",
            parse_mode="HTML",
            reply_markup=account_cancel_keyboard(),
        )

    except Exception as exc:
        pending_logins.pop(user_id, None)
        try:
            await bot.send_message(
                chat_id,
                f"❌ Ошибка при QR: {type(exc).__name__}",
                reply_markup=account_menu_keyboard(user_id),
            )
        except Exception:
            pass


async def _start_qr_login(query: CallbackQuery, state: FSMContext, *, refresh: bool) -> None:
    api_id = int(os.getenv("TG_API_ID", "0") or "0")
    api_hash = (os.getenv("TG_API_HASH") or "").strip()
    if not api_id or not api_hash:
        await query.answer("Не настроены TG_API_ID/TG_API_HASH.", show_alert=True)
        return

    user_id = query.from_user.id
    client = make_client_from_string_session(api_id, api_hash)
    try:
        await client.connect()
        qr_login = await client.qr_login()
    except Exception as exc:
        await client.disconnect()
        await query.answer(f"Не удалось создать QR: {type(exc).__name__}", show_alert=True)
        return

    pending = new_pending_login(method="qr", api_id=api_id, api_hash=api_hash, phone="", client=client)
    pending.qr_login = qr_login
    pending_logins[user_id] = pending

    try:
        import qrcode

        img = qrcode.make(qr_login.url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        photo = BufferedInputFile(buf.getvalue(), filename="login-qr.png")
    except Exception:
        await _cleanup_pending_login(user_id)
        await query.answer("Нужен пакет qrcode для QR.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я отсканировал", callback_data="acc_qr_check")],
        [InlineKeyboardButton(text="🔄 Обновить QR", callback_data="acc_qr_refresh")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="acc_cancel")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="acc_menu")],
    ])

    await state.set_state(MainMenu.viewing)
    sent = await query.message.answer_photo(
        photo=photo,
        caption=(
            "📷 <b>QR-вход</b>\n\n"
            "Откройте Telegram → Settings → Devices → Scan QR.\n\n"
            f"Таймаут: <b>{code_ttl_seconds()} сек</b>."
        ),
        parse_mode="HTML",
        reply_markup=kb,
    )
    pending.bg_task = asyncio.create_task(
        _qr_auto_watch(user_id, query.message.chat.id, sent.message_id)
    )
    await query.answer("QR готов" if not refresh else "QR обновлён")


@dp.callback_query(F.data == "bc_channels")
async def broadcast_channels(query: CallbackQuery):
    user_id = query.from_user.id
    state = scoped_broadcast_manager(user_id).load()
    await query.message.edit_text(
        "📢 <b>Каналы send-as</b>\n\n"
        "Выберите активный канал отправки или добавьте новый.",
        parse_mode="HTML",
        reply_markup=broadcast_channels_keyboard(state),
    )
    await query.answer()


@dp.callback_query(F.data == "bc_add_channel")
async def broadcast_add_channel_prompt(query: CallbackQuery, state: FSMContext):
    await query.message.edit_text(
        "➕ <b>Добавить канал send-as</b>\n\n"
        "Отправьте username канала в формате <code>@my_channel</code>.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="bc_channels")],
        ]),
    )
    await state.set_state(MainMenu.adding_broadcast_channel)
    await query.answer()


@dp.message(MainMenu.adding_broadcast_channel)
async def broadcast_add_channel_input(message: Message, state: FSMContext):
    user_id = message.from_user.id
    bm = scoped_broadcast_manager(user_id)
    value = (message.text or "").strip()
    if not re.match(r"^@[A-Za-z0-9_]{5,32}$", value):
        await message.answer("❌ Неверный формат. Используйте @channel_username")
        return
    current = bm.load().get("campaign", {}).get("send_as_channel")
    bm.add_send_as_channel(value)
    if not current:
        bm.set_send_as_channel(value)
    invalidate_readiness_if_needed(bm, reason="send_as_changed")
    await state.set_state(MainMenu.viewing)
    state_data = bm.load()
    await message.answer(
        "✅ Канал добавлен.",
        reply_markup=broadcast_channels_keyboard(state_data),
    )


@dp.callback_query(F.data.startswith("bc_set_"))
async def broadcast_set_channel(query: CallbackQuery):
    channel = query.data[len("bc_set_"):]
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    state = bm.load()
    if channel not in state.get("send_as_channels", []):
        await query.answer("Канал не найден.", show_alert=True)
        return
    state = bm.set_send_as_channel(channel)
    state = invalidate_readiness_if_needed(bm, reason="send_as_changed")
    await query.message.edit_text(
        "📢 <b>Каналы send-as</b>\n\nАктивный канал обновлён.",
        parse_mode="HTML",
        reply_markup=broadcast_channels_keyboard(state),
    )
    await query.answer("Выбрано")


@dp.callback_query(F.data == "bc_del_selected_channel")
async def broadcast_delete_selected_channel(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    state = bm.load()
    selected = state.get("campaign", {}).get("send_as_channel")
    if not selected:
        await query.answer("Сначала выберите канал.", show_alert=True)
        return
    state = bm.remove_send_as_channel(selected)
    state = invalidate_readiness_if_needed(bm, reason="send_as_changed")
    await query.message.edit_text(
        "📢 <b>Каналы send-as</b>\n\nВыбранный канал удалён.",
        parse_mode="HTML",
        reply_markup=broadcast_channels_keyboard(state),
    )
    await query.answer()


@dp.callback_query(F.data == "bc_source")
async def broadcast_source_prompt(query: CallbackQuery, state: FSMContext):
    if not await ensure_owner_callback(query):
        return
    await query.message.edit_text(
        "📝 <b>Источник поста</b>\n\n"
        "Отправьте: <code>@channel message_id</code>\n"
        "Пример: <code>@mychannel 1234</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="broadcast")],
        ]),
    )
    await state.set_state(MainMenu.setting_broadcast_source)
    await query.answer()


@dp.message(MainMenu.setting_broadcast_source)
async def broadcast_source_input(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await message.answer("⛔️ Доступ только для владельца.")
        return
    parsed = parse_source_input(message.text or "")
    if not parsed:
        await message.answer("❌ Неверный формат. Используйте: @channel 123")
        return
    source_channel, source_message_id = parsed
    broadcast_manager.set_source(source_channel, source_message_id)
    await state.set_state(MainMenu.viewing)
    current = broadcast_manager.load()
    await message.answer(
        "✅ Источник поста сохранён.",
        parse_mode="HTML",
        reply_markup=broadcast_main_keyboard(current, user_id=message.from_user.id),
    )


@dp.callback_query(F.data == "bc_posts")
async def broadcast_posts(query: CallbackQuery, state: FSMContext):
    user_id = query.from_user.id
    await state.set_state(MainMenu.viewing)
    data = scoped_broadcast_manager(user_id).load()
    await query.message.edit_text(
        broadcast_posts_text(data),
        parse_mode="HTML",
        reply_markup=broadcast_posts_keyboard(data),
        disable_web_page_preview=True,
    )
    await query.answer()


@dp.callback_query(F.data == "bcp_add")
async def broadcast_posts_add_prompt(query: CallbackQuery, state: FSMContext):
    user_id = query.from_user.id
    data = scoped_broadcast_manager(user_id).load()
    storage_ref = _resolve_storage_channel_ref(data)
    if not storage_ref:
        await query.answer("Не задан канал для хранения постов (storage).", show_alert=True)
        return
    await query.message.edit_text(
        "➕ <b>Добавить пост</b>\n\n"
        "Пришлите пост (текст/фото/видео). Я сохраню его в служебный канал.\n\n"
        "Лимит: <b>10</b> постов.",
        parse_mode="HTML",
        reply_markup=broadcast_posts_add_keyboard(),
        disable_web_page_preview=True,
    )
    await state.set_state(MainMenu.adding_broadcast_post)
    await query.answer()


@dp.callback_query(F.data == "bcp_more")
async def broadcast_posts_add_more(query: CallbackQuery, state: FSMContext):
    await state.set_state(MainMenu.adding_broadcast_post)
    await query.answer("Жду следующий пост")


@dp.callback_query(F.data == "bcp_done")
async def broadcast_posts_add_done(query: CallbackQuery, state: FSMContext):
    user_id = query.from_user.id
    await state.set_state(MainMenu.viewing)
    data = scoped_broadcast_manager(user_id).load()
    await query.message.edit_text(
        broadcast_posts_text(data),
        parse_mode="HTML",
        reply_markup=broadcast_posts_keyboard(data),
        disable_web_page_preview=True,
    )
    await query.answer("Готово")


@dp.callback_query(F.data.startswith("bcp_del_"))
async def broadcast_posts_delete(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    post_id = query.data[len("bcp_del_"):]
    data = bm.delete_post(post_id)
    await query.message.edit_text(
        broadcast_posts_text(data),
        parse_mode="HTML",
        reply_markup=broadcast_posts_keyboard(data),
        disable_web_page_preview=True,
    )
    await query.answer("Удалено")


@dp.message(MainMenu.adding_broadcast_post)
async def broadcast_posts_add_input(message: Message, state: FSMContext):
    user_id = message.from_user.id
    bm = scoped_broadcast_manager(user_id)
    data = bm.load()
    campaign = data.get("campaign", {})
    posts = campaign.get("posts", []) if isinstance(campaign.get("posts", []), list) else []
    if len(posts) >= 10:
        await message.answer("❌ Лимит постов: 10.", reply_markup=broadcast_posts_add_keyboard())
        return

    storage_ref = _resolve_storage_channel_ref(data)
    if not storage_ref:
        await message.answer("❌ Не задан канал для хранения постов (storage).")
        return

    kind = None
    preview = ""
    if message.text:
        kind = "text"
        preview = message.text
    elif message.photo:
        kind = "photo"
        preview = message.caption or "[photo]"
    elif message.video:
        kind = "video"
        preview = message.caption or "[video]"

    if not kind:
        await message.answer("❌ Поддерживается только: текст/фото/видео.", reply_markup=broadcast_posts_add_keyboard())
        return

    try:
        copied = await bot.copy_message(
            chat_id=_bot_chat_id(storage_ref),
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        stored_message_id = int(getattr(copied, "message_id", None) or getattr(copied, "message_id", 0))
        if not stored_message_id:
            stored_message_id = int(getattr(copied, "message_id", 0))
    except Exception as exc:
        await message.answer(f"❌ Не удалось сохранить пост в storage: {type(exc).__name__}", reply_markup=broadcast_posts_add_keyboard())
        return

    bm.add_post(
        channel=str(storage_ref),
        message_id=stored_message_id,
        kind=kind,
        preview=preview,
        max_posts=10,
    )

    await message.answer(
        "✅ Пост сохранён.\n\nМожно добавить ещё или завершить.",
        reply_markup=broadcast_posts_add_keyboard(),
    )


@dp.callback_query(F.data == "bc_groups")
async def broadcast_groups(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    groups = scoped_load_broadcast_groups(user_id)
    state = bm.ensure_groups_known(groups)
    await query.message.edit_text(
        "👥 <b>Выбор групп для рассылки</b>\n\n"
        "✅ выбранные, ▫️ невыбранные, 🚫 недоступные.\n"
        "Нажмите на группу, чтобы переключить.",
        parse_mode="HTML",
        reply_markup=broadcast_groups_keyboard(state, groups=groups, page=0, allow_manage=not is_owner(user_id)),
    )
    await query.answer()


@dp.callback_query(F.data.startswith("bcgp_"))
async def broadcast_groups_page(query: CallbackQuery):
    page = int(query.data.split("_")[-1])
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    groups = scoped_load_broadcast_groups(user_id)
    state = bm.ensure_groups_known(groups)
    await query.message.edit_text(
        "👥 <b>Выбор групп для рассылки</b>\n\n"
        "✅ выбранные, ▫️ невыбранные, 🚫 недоступные.\n"
        "Нажмите на группу, чтобы переключить.",
        parse_mode="HTML",
        reply_markup=broadcast_groups_keyboard(state, groups=groups, page=page, allow_manage=not is_owner(user_id)),
    )
    await query.answer()


@dp.callback_query(F.data == "bcg_add")
async def broadcast_groups_add_prompt(query: CallbackQuery, state: FSMContext):
    user_id = query.from_user.id
    if is_owner(user_id):
        await query.answer("Для админа список групп фиксированный.", show_alert=True)
        return
    await state.set_state(MainMenu.adding_broadcast_group)
    await query.message.edit_text(
        "➕ <b>Добавить чат/группу</b>\n\n"
        "<b>Поддерживаемые форматы:</b>\n"
        "• <code>@username</code>\n"
        "• <code>https://t.me/username</code>\n"
        "• <code>t.me/username</code>\n"
        "• просто <code>username</code> (от 5 символов)\n"
        "• <code>chat_id</code> (числовой, например <code>-1001234567890</code>)\n\n"
        "Или перешлите любое сообщение из нужного чата — я распознаю автоматически.\n\n"
        "⚠️ <b>Отправляйте по одной группе за раз.</b>\n\n"
        "<i>Примечание: рассылка возможна только если подключённый аккаунт состоит в этом чате.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="bc_groups")],
        ]),
        disable_web_page_preview=True,
    )
    await query.answer()


@dp.message(MainMenu.adding_broadcast_group)
async def broadcast_groups_add_input(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if is_owner(user_id):
        await state.set_state(MainMenu.viewing)
        await message.answer("⛔️ Доступ только для пользователей.", reply_markup=back_button())
        return

    ref = None
    # Forwarded message: try to extract chat id from multiple Bot API variants
    forward_from_chat = getattr(message, "forward_from_chat", None)
    if forward_from_chat and getattr(forward_from_chat, "id", None):
        ref = str(forward_from_chat.id)
    if not ref:
        origin = getattr(message, "forward_origin", None)
        sender_chat = getattr(origin, "sender_chat", None) if origin else None
        if sender_chat and getattr(sender_chat, "id", None):
            ref = str(sender_chat.id)
    if not ref:
        # Some clients forward with a nested "chat" object
        origin = getattr(message, "forward_origin", None)
        chat_obj = getattr(origin, "chat", None) if origin else None
        if chat_obj and getattr(chat_obj, "id", None):
            ref = str(chat_obj.id)

    if not ref:
        ref = normalize_group_ref(message.text or "")

    if not ref:
        await message.answer("❌ Не удалось распознать группу/чат. Пришлите @username или перешлите сообщение из чата.")
        return

    added = add_user_broadcast_group(user_id, ref)
    if not added:
        await message.answer("⚠️ Уже есть в списке.")
    else:
        await message.answer(f"✅ Добавлено: <code>{format_group_ref(ref)}</code>", parse_mode="HTML")

    await state.set_state(MainMenu.viewing)
    bm = scoped_broadcast_manager(user_id)
    groups = scoped_load_broadcast_groups(user_id)
    data = bm.ensure_groups_known(groups)
    await message.answer(
        "👥 <b>Выбор групп для рассылки</b>\n\n"
        "✅ выбранные, ▫️ невыбранные, 🚫 недоступные.\n"
        "Нажмите на группу, чтобы переключить.",
        parse_mode="HTML",
        reply_markup=broadcast_groups_keyboard(data, groups=groups, page=0, allow_manage=True),
    )


@dp.callback_query(F.data == "bcg_delete_mode")
async def broadcast_group_delete_mode(query: CallbackQuery):
    user_id = query.from_user.id
    groups = scoped_load_broadcast_groups(user_id)
    if not groups:
        await query.answer("Нет групп для удаления.", show_alert=True)
        return

    # Строим клавиатуру для выбора группы на удаление
    rows = []
    for group in groups:
        label = format_group_ref(group)
        rows.append([InlineKeyboardButton(text=label, callback_data=f"bcgdel_{group}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="bc_groups")])

    await query.message.edit_text(
        "🗑 <b>Выберите группу для удаления:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await query.answer()


@dp.callback_query(F.data.startswith("bcgdel_"))
async def broadcast_group_delete_one(query: CallbackQuery):
    user_id = query.from_user.id
    group = query.data[len("bcgdel_"):]
    bm = scoped_broadcast_manager(user_id)
    if delete_user_broadcast_group(user_id, group):
        bm.unselect_groups([group])
        invalidate_readiness_if_needed(bm, reason="groups_changed")
        await query.answer("✅ Удалено")
    else:
        await query.answer("❌ Не найдено", show_alert=True)
        return

    # Возвращаемся в обычное меню групп
    groups = scoped_load_broadcast_groups(user_id)
    state = bm.ensure_groups_known(groups)
    await query.message.edit_text(
        "👥 <b>Группы рассылки</b>\n\n"
        "✅ выбранные, ▫️ невыбранные, 🚫 недоступные.\n"
        "Нажмите на группу, чтобы переключить.",
        parse_mode="HTML",
        reply_markup=broadcast_groups_keyboard(state, groups=groups, page=0, allow_manage=True)
    )


@dp.callback_query(F.data.startswith("bcg_"))
async def broadcast_group_toggle(query: CallbackQuery):
    group = query.data[len("bcg_"):]
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    groups = scoped_load_broadcast_groups(user_id)
    state = bm.ensure_groups_known(groups)
    group_meta = state.get("broadcast_groups_state", {}).get(group, {})
    last_status = (group_meta.get("last_test_status") or "").strip()
    last_reason = (group_meta.get("last_test_reason") or "").strip()
    status_icon = {
        "ok": "✅",
        "failed": "🚫",
        "deleted": "🗑",
        "unknown": "⚠️",
    }.get(last_status, "")
    if group_meta.get("status") == "blocked":
        state = bm.set_group_active(group)
        state = invalidate_readiness_if_needed(bm, reason="groups_changed")
        await query.answer("Группа разблокирована")
    else:
        state = bm.toggle_group_selected(group)
        state = invalidate_readiness_if_needed(bm, reason="groups_changed")
        if last_status and last_status != "ok":
            hint = f"{status_icon} последний тест: {last_status}"
            if last_reason:
                hint += f" ({last_reason})"
            await query.answer(hint)
        else:
            await query.answer()
    await query.message.edit_reply_markup(
        reply_markup=broadcast_groups_keyboard(state, groups=groups, page=0, allow_manage=not is_owner(user_id))
    )


@dp.callback_query(F.data == "bc_schedule_toggle")
async def broadcast_schedule_toggle(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    groups = scoped_load_broadcast_groups(user_id)
    state = bm.load()
    enabled = state.get("broadcast_schedule", {}).get("enabled", True)
    state = bm.set_schedule_enabled(not enabled)
    await query.message.edit_text(
        broadcast_summary_text(state, user_id=user_id, groups=groups),
        parse_mode="HTML",
        reply_markup=broadcast_main_keyboard(state, user_id=user_id),
    )
    await query.answer("Обновлено")


@dp.callback_query(F.data == "bc_schedule")
async def broadcast_schedule_week(query: CallbackQuery, state: FSMContext):
    user_id = query.from_user.id
    await state.set_state(MainMenu.viewing)
    data = scoped_broadcast_manager(user_id).load()
    await query.message.edit_text(
        broadcast_week_text(data),
        parse_mode="HTML",
        reply_markup=broadcast_week_keyboard(),
        disable_web_page_preview=True,
    )
    await query.answer()


@dp.callback_query(F.data.startswith("bcs_day_"))
async def broadcast_schedule_day_open(query: CallbackQuery, state: FSMContext):
    user_id = query.from_user.id
    weekday = query.data[len("bcs_day_"):]
    if weekday not in WEEKDAYS:
        await query.answer("Неверный день.", show_alert=True)
        return
    await state.set_state(MainMenu.viewing)
    data = scoped_broadcast_manager(user_id).load()
    await query.message.edit_text(
        broadcast_day_text(data, weekday),
        parse_mode="HTML",
        reply_markup=broadcast_day_keyboard(data, weekday),
        disable_web_page_preview=True,
    )
    await query.answer()


@dp.callback_query(F.data.startswith("bcs_set_"))
async def broadcast_schedule_day_set_prompt(query: CallbackQuery, state: FSMContext):
    weekday = query.data[len("bcs_set_"):]
    if weekday not in WEEKDAYS:
        await query.answer("Неверный день.", show_alert=True)
        return
    await state.set_state(MainMenu.setting_broadcast_weekday_time)
    await state.update_data(bcs_weekday=weekday)
    await query.message.edit_text(
        f"✏️ <b>Установить время</b>\n\nДень: <b>{WEEKDAY_NAMES.get(weekday, weekday)}</b>\n"
        "Введите время в формате <code>HH:MM</code> (например, <code>10:00</code> или <code>10.00</code>).\n\n"
        "Ограничение MVP: 07:00–21:59.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"bcs_day_{weekday}")],
        ]),
    )
    await query.answer()


@dp.message(MainMenu.setting_broadcast_weekday_time)
async def broadcast_schedule_day_set_input(message: Message, state: FSMContext):
    user_id = message.from_user.id
    bm = scoped_broadcast_manager(user_id)
    data = await state.get_data()
    weekday = (data.get("bcs_weekday") or "").strip()
    if weekday not in WEEKDAYS:
        await message.answer("❌ Не выбран день недели. Откройте расписание заново.")
        await state.set_state(MainMenu.viewing)
        return

    hhmm = _normalize_hhmm(message.text or "")
    if not hhmm:
        await message.answer("❌ Неверный формат. Пример: 10:00 или 10.00")
        return
    ok, err = _validate_allowed_time(hhmm)
    if not ok:
        await message.answer(err)
        return

    bm.set_weekday_time(weekday, hhmm)
    await state.set_state(MainMenu.viewing)
    st = bm.load()
    await message.answer(
        f"✅ Время сохранено: <b>{WEEKDAY_NAMES.get(weekday, weekday)} {hhmm}</b>",
        parse_mode="HTML",
        reply_markup=broadcast_day_keyboard(st, weekday),
    )


@dp.callback_query(F.data.startswith("bcs_clear_"))
async def broadcast_schedule_day_clear(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    weekday = query.data[len("bcs_clear_"):]
    if weekday not in WEEKDAYS:
        await query.answer("Неверный день.", show_alert=True)
        return
    data = bm.set_weekday_time(weekday, None)
    await query.message.edit_text(
        broadcast_day_text(data, weekday),
        parse_mode="HTML",
        reply_markup=broadcast_day_keyboard(data, weekday),
    )
    await query.answer("Очищено")


@dp.callback_query(F.data.startswith("bcs_toggle_"))
async def broadcast_schedule_day_toggle(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    weekday = query.data[len("bcs_toggle_"):]
    if weekday not in WEEKDAYS:
        await query.answer("Неверный день.", show_alert=True)
        return
    st = bm.load()
    meta = (st.get("weekly_schedule") or {}).get(weekday) or {}
    new_enabled = not bool(meta.get("enabled"))
    data = bm.set_weekday_enabled(weekday, new_enabled)
    await query.message.edit_text(
        broadcast_day_text(data, weekday),
        parse_mode="HTML",
        reply_markup=broadcast_day_keyboard(data, weekday),
    )
    await query.answer("Обновлено")


@dp.callback_query(F.data.startswith("bcs_copy_") & ~F.data.startswith("bcs_copy_to_"))
async def broadcast_schedule_copy_start(query: CallbackQuery, state: FSMContext):
    weekday = query.data[len("bcs_copy_"):]
    if weekday not in WEEKDAYS:
        await query.answer("Неверный день.", show_alert=True)
        return
    await state.set_state(MainMenu.copying_broadcast_weekday)
    await state.update_data(bcs_copy_source=weekday)
    await query.message.edit_text(
        broadcast_copy_target_text(weekday),
        parse_mode="HTML",
        reply_markup=broadcast_copy_target_keyboard(weekday),
    )
    await query.answer()


@dp.callback_query(F.data.startswith("bcs_copy_to_"))
async def broadcast_schedule_copy_confirm(query: CallbackQuery, state: FSMContext):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    parts = query.data.split("_")
    if len(parts) < 5:
        await query.answer("Неверная команда.", show_alert=True)
        return
    source = parts[3]
    target = parts[4]
    if source not in WEEKDAYS or target not in WEEKDAYS:
        await query.answer("Неверный день.", show_alert=True)
        return
    bm.copy_weekday(source, target)
    await state.set_state(MainMenu.viewing)
    data = bm.load()
    await query.message.edit_text(
        broadcast_day_text(data, source),
        parse_mode="HTML",
        reply_markup=broadcast_day_keyboard(data, source),
    )
    await query.answer("✅ Скопировано")


@dp.callback_query(F.data == "bc_test")
async def broadcast_test_intro(query: CallbackQuery):
    await query.message.edit_text(
        broadcast_test_intro_text(),
        parse_mode="HTML",
        reply_markup=broadcast_test_intro_keyboard(),
    )
    await query.answer()


@dp.callback_query(F.data == "bc_test_info")
async def broadcast_test_info(query: CallbackQuery):
    await query.message.edit_text(
        broadcast_test_info_text(),
        parse_mode="HTML",
        reply_markup=broadcast_test_info_keyboard(),
    )
    await query.answer()


@dp.callback_query(F.data == "bc_test_start")
async def broadcast_test_v2(query: CallbackQuery):
    if broadcast_lock.locked():
        await query.answer("Рассылка уже выполняется.", show_alert=True)
        return

    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    groups_all = scoped_load_broadcast_groups(user_id)
    state = bm.ensure_groups_known(groups_all)

    # Step enforcement: check that all prerequisite steps are completed
    steps = get_setup_steps(user_id, state, groups_all)
    if not steps["account"]:
        await query.answer("⚠️ Шаг 1: Сначала подключите аккаунт (кнопка «🔑 Подключить аккаунт»)", show_alert=True)
        return
    if not steps["posts"]:
        await query.answer("⚠️ Шаг 2: Добавьте хотя бы один пост в пул (кнопка «🗂 Посты»)", show_alert=True)
        return
    if not steps["groups"]:
        await query.answer("⚠️ Шаг 3: Добавьте хотя бы одну группу рассылки (кнопка «👥 Группы рассылки»)", show_alert=True)
        return
    active_groups = get_active_selected_groups(state, groups_all)
    if not active_groups:
        await query.answer("⚠️ Выберите хотя бы одну группу для отправки", show_alert=True)
        return
    if not steps["schedule"]:
        await query.answer("⚠️ Шаг 4: Настройте расписание — укажите время хотя бы для одного дня (кнопка «📅 Расписание»)", show_alert=True)
        return
    if not steps["readiness"]:
        readiness_ok, readiness_reason = is_readiness_fresh(state)
        reason_map = {
            "not_passed": "сначала пройдите «🧭 Готовность»",
            "has_problems": "в «🧭 Готовность» есть проблемные группы — устраните их",
            "missing_checked_at": "проверка готовности не завершена, нажмите «🧭 Готовность»",
            "invalid_checked_at": "статус готовности поврежден, запустите «🧭 Готовность» заново",
            "stale": "проверка устарела, обновите «🧭 Готовность»",
            "snapshot_missing": "изменились условия кампании, обновите «🧭 Готовность»",
            "snapshot_changed": "вы изменили настройки кампании, снова пройдите «🧭 Готовность»",
        }
        if readiness_ok:
            await query.answer("⚠️ Сначала пройдите «🧭 Готовность».", show_alert=True)
        else:
            await query.answer(f"⚠️ Перед тестом {reason_map.get(readiness_reason, 'пройдите «🧭 Готовность»')}.", show_alert=True)
        return

    ready, reason = is_campaign_ready(state, user_id=user_id, groups=groups_all)
    if not ready:
        await query.answer(reason, show_alert=True)
        return

    owner_bypass = bool(OWNER_IDS) and (user_id in OWNER_IDS)
    can_run_test, deny_reason = bm.can_run_test(
        cooldown_seconds=TEST_COOLDOWN_SECONDS,
        max_tests_per_day=TEST_MAX_PER_DAY,
        bypass_limits=owner_bypass,
    )
    if not can_run_test:
        test_log = bm.load().get("test_log", {})
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_count = 0
        if isinstance(test_log, dict):
            daily_counts = test_log.get("daily_counts", {})
            if isinstance(daily_counts, dict):
                try:
                    daily_count = int(daily_counts.get(today, 0))
                except Exception:
                    daily_count = 0
        logger.warning(
            "bc_test denied user_id=%s reason=%s daily_count=%s cooldown_seconds=%s max_tests_per_day=%s",
            user_id,
            deny_reason,
            daily_count,
            TEST_COOLDOWN_SECONDS,
            TEST_MAX_PER_DAY,
        )
        await query.answer(f"❌ {deny_reason}", show_alert=True)
        return

    test_groups = get_active_selected_groups(state, groups_all)

    # Test broadcast is always free: no balance check, no deduction.
    balance_mgr = scoped_balance_manager(user_id)
    balance_before_test = balance_mgr.get_balance()

    await query.message.edit_text(
        "🧪 <b>ТЕСТИРОВАНИЕ ГРУПП</b>\n\n"
        "🆓 Тест бесплатный — посты не списываются.\n"
        f"Отправляю тестовые сообщения в <b>{len(test_groups)}</b> групп...\n"
        "Не закрывайте чат!",
        parse_mode="HTML",
    )
    await query.answer()

    started_at = datetime.now(timezone.utc).isoformat()
    bm.record_test_run(bypass_limits=owner_bypass)
    async with broadcast_lock:
        result = await execute_broadcast(
            user_id,
            test_groups,
            advance_rotation=False,
            is_test=True,
            test_marker="🧪",
        )

    sent_message_ids = result.get("sent_message_ids", {}) if isinstance(result.get("sent_message_ids", {}), dict) else {}
    send_errors = result.get("send_errors", {}) if isinstance(result.get("send_errors", {}), dict) else {}
    blocked = result.get("blocked_groups", {}) if isinstance(result.get("blocked_groups", {}), dict) else {}

    for group, err in blocked.items():
        bm.set_group_blocked(group, err)

    for group in test_groups:
        if group in sent_message_ids:
            bm.set_group_last_test(
                group,
                status="ok",
                reason="ok",
                message_id=int(sent_message_ids.get(group) or 0) if sent_message_ids.get(group) else None,
                sent_at=started_at,
                verified_at=None,
            )
        else:
            reason_raw = str(send_errors.get(group) or blocked.get(group) or "other")
            reason_lower = reason_raw.lower()
            status = "deleted" if "delete" in reason_lower else "failed"
            bm.set_group_last_test(
                group,
                status=status,
                reason=reason_raw,
                message_id=None,
                sent_at=started_at,
                verified_at=None,
            )

    if result.get("sent_count", 0) > 0:
        bm.mark_test_passed()
    else:
        bm.reset_test_flag()

    # Build lists of working and failed groups
    working_groups = list(sent_message_ids.keys()) if sent_message_ids else []
    failed_groups: dict[str, str] = {}
    auto_disabled_groups: list[str] = []
    for group in test_groups:
        if group not in working_groups:
            reason = str(send_errors.get(group) or blocked.get(group) or "unknown")
            failed_groups[group] = reason
            bm.set_group_blocked(group, f"test_{reason}")
            auto_disabled_groups.append(group)

    # Show test result with new format
    test_result_text = broadcast_test_result_text(
        len(test_groups),
        working_groups,
        failed_groups,
        len(auto_disabled_groups),
    )
    balance_after_test = balance_mgr.get_balance()

    # Wait 60 seconds with progress updates, then verify/delete test messages.
    total_seconds = max(10, BROADCAST_TEST_VERIFY_SECONDS)
    step_seconds = 10
    test_message_ids = sent_message_ids
    cleanup_summary = ""
    if test_message_ids:
        for elapsed in range(0, total_seconds + step_seconds, step_seconds):
            remaining = max(0, total_seconds - elapsed)
            filled = min(10, int((elapsed / total_seconds) * 10))
            bar = "█" * filled + "░" * (10 - filled)
            try:
                await query.message.edit_text(
                    "🧪 <b>ПРОВЕРКА ТЕСТОВЫХ СООБЩЕНИЙ</b>\n\n"
                    f"⏳ Осталось: <b>{remaining}</b> сек\n"
                    f"<code>{bar}</code>\n\n"
                    "Не закрывайте чат!",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            if remaining > 0:
                await asyncio.sleep(step_seconds)

        ok, _, client, _ = await _readiness_check_connected_account(user_id)
        cleanup = {}
        if ok and client:
            try:
                cleanup = await verify_and_delete_test_messages(
                    client=client,
                    test_message_ids=test_message_ids,
                    wait_seconds=0,
                )
            except Exception:
                cleanup = {}
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

            found_count = sum(1 for v in cleanup.values() if v.get("found"))
            deleted_count = sum(1 for v in cleanup.values() if v.get("deleted"))
            cleanup_summary = f"🧹 Тестовые сообщения: найдено {found_count}, удалено {deleted_count}.\n"
        else:
            cleanup_summary = "🧹 Не удалось подключиться для удаления тестовых сообщений.\n"
    else:
        cleanup_summary = "🧹 Тестовые сообщения не отправлены (удалять нечего).\n"

    full_text = (
        test_result_text
        + "\n\n🆓 <b>Тест бесплатный:</b> потрачено 0 постов\n"
        + f"💰 <b>Баланс:</b> {balance_after_test} постов (было {balance_before_test})"
        + f"\n{cleanup_summary}"
    )

    updated = bm.load()
    await query.message.edit_text(
        full_text,
        parse_mode="HTML",
        reply_markup=broadcast_test_result_keyboard(),
        disable_web_page_preview=True,
    )
    # query.answer() already called at start of the long-running operation


@dp.callback_query(F.data == "bc_mode_toggle")
async def broadcast_mode_toggle(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    groups = scoped_load_broadcast_groups(user_id)
    state_data = bm.load()
    current = state_data.get("campaign", {}).get("send_mode", "user")
    new_mode = "channel" if current == "user" else "user"
    state_data = bm.set_send_mode(new_mode)
    state_data = invalidate_readiness_if_needed(bm, reason="send_mode_changed")
    await query.message.edit_text(
        broadcast_summary_text(state_data, user_id=user_id, groups=groups),
        parse_mode="HTML",
        reply_markup=broadcast_main_keyboard(state_data, user_id=user_id),
    )
    await query.answer()


@dp.callback_query(F.data == "bc_mass")
async def broadcast_mass(query: CallbackQuery):
    if broadcast_lock.locked():
        await query.answer("Рассылка уже выполняется.", show_alert=True)
        return
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    groups_all = scoped_load_broadcast_groups(user_id)
    state = bm.ensure_groups_known(groups_all)

    # Step enforcement: check all 6 setup steps
    steps = get_setup_steps(user_id, state, groups_all)
    if not steps["account"]:
        await query.answer("⚠️ Шаг 1: Сначала подключите аккаунт (кнопка «🔑 Подключить аккаунт»)", show_alert=True)
        return
    if not steps["posts"]:
        await query.answer("⚠️ Шаг 2: Добавьте хотя бы один пост в пул (кнопка «🗂 Посты»)", show_alert=True)
        return
    if not steps["groups"]:
        await query.answer("⚠️ Шаг 3: Добавьте хотя бы одну группу рассылки (кнопка «👥 Группы рассылки»)", show_alert=True)
        return
    active_groups = get_active_selected_groups(state, groups_all)
    if not active_groups:
        await query.answer("⚠️ Выберите хотя бы одну группу для отправки", show_alert=True)
        return
    if not steps["schedule"]:
        await query.answer("⚠️ Шаг 4: Настройте расписание — укажите время хотя бы для одного дня (кнопка «📅 Расписание»)", show_alert=True)
        return
    if not steps["readiness"]:
        await query.answer("⚠️ Шаг 5: Сначала пройдите «🧭 Готовность».", show_alert=True)
        return
    if not steps["test"]:
        await query.answer("⚠️ Шаг 6: Сначала запустите тест (кнопка «🧪 Тест»).", show_alert=True)
        return
    test_fresh, _ = is_test_fresh(state)
    if not test_fresh:
        await query.answer("⚠️ Тест устарел (старше 24 часов). Запустите «🧪 Тест» заново.", show_alert=True)
        return

    ready, reason = is_campaign_ready(state, user_id=user_id, groups=groups_all)
    if not ready:
        await query.answer(reason, show_alert=True)
        return
    groups = get_active_selected_groups(state, groups_all)

    # Check balance before mass broadcast
    balance_mgr = scoped_balance_manager(user_id)
    if not balance_mgr.check_sufficient(len(groups)):
        insufficient_text = (
            f"❌ <b>Недостаточно постов!</b>\n\n"
            f"Требуется: {len(groups)} постов\n"
            f"В наличии: {balance_mgr.get_balance()} постов\n\n"
            "Выберите пакет и пополните баланс:"
        )
        await query.message.edit_text(
            insufficient_text,
            parse_mode="HTML",
            reply_markup=broadcast_balance_keyboard(),
        )
        await query.answer()
        return

    # Show confirmation with balance calculation
    current_balance = balance_mgr.get_balance()
    new_balance = current_balance - len(groups)
    confirmation_text = (
        f"📣 <b>Подтверждение рассылки</b>\n\n"
        f"📊 Параметры:\n"
        f"  Групп к отправке: <b>{len(groups)}</b>\n"
        f"  Расписание: включено\n\n"
        f"💰 Баланс постов:\n"
        f"  Требуется для старта: {len(groups)} постов\n"
        f"  Сейчас: {current_balance} постов\n"
        f"  После рассылки (макс. вычет): ~{new_balance} постов\n\n"
        f"⚠️ Точная сумма списания: только успешные публикации.\n"
        f"Нажмите \"Подтвердить\" для начала рассылки."
    )

    # Create confirmation keyboard with proceed and cancel buttons
    confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="bc_confirm_mass")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="broadcast")],
    ])

    await query.message.edit_text(
        confirmation_text,
        parse_mode="HTML",
        reply_markup=confirm_keyboard,
    )
    await query.answer()


@dp.callback_query(F.data == "bc_confirm_mass")
async def broadcast_confirm_mass(query: CallbackQuery):
    """Execute mass broadcast after confirmation."""
    if broadcast_lock.locked():
        await query.answer("Рассылка уже выполняется.", show_alert=True)
        return

    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    balance_mgr = scoped_balance_manager(user_id)
    balance_before = balance_mgr.get_balance()
    groups_all = scoped_load_broadcast_groups(user_id)
    state = bm.ensure_groups_known(groups_all)
    groups = get_active_selected_groups(state, groups_all)

    steps = get_setup_steps(user_id, state, groups_all)
    if not steps["account"] or not steps["posts"] or not steps["groups"] or not steps["schedule"] or not steps["readiness"] or not steps["test"]:
        await query.answer("Кампания не готова. Откройте раздел «📣 Рассылка» и завершите шаги настройки.", show_alert=True)
        return
    test_fresh, _ = is_test_fresh(state)
    if not test_fresh:
        await query.answer("⚠️ Тест устарел (старше 24 часов). Перед массовой рассылкой запустите тест повторно.", show_alert=True)
        return

    ready, reason = is_campaign_ready(state, user_id=user_id, groups=groups_all)
    if not ready:
        await query.answer(reason, show_alert=True)
        return
    if not groups:
        await query.answer("Нет активных групп.", show_alert=True)
        return

    await query.message.edit_text(
        "📢 <b>РАССЫЛКА В ПРОЦЕССЕ</b>\n\n"
        f"Всего групп: <b>{len(groups)}</b>\n"
        "⏳ Отправляю сообщения...\n"
        "Не закрывайте чат!",
        parse_mode="HTML",
    )

    async with broadcast_lock:
        # Re-check inside the lock to protect from double-click races.
        if not balance_mgr.check_sufficient(len(groups)):
            await query.message.edit_text(
                "❌ <b>Баланс изменился</b>\n\n"
                f"Требуется: {len(groups)} постов\n"
                f"Доступно: {balance_mgr.get_balance()} постов\n\n"
                "Пополните баланс и попробуйте снова.",
                parse_mode="HTML",
                reply_markup=broadcast_balance_keyboard(),
            )
            await query.answer()
            return
        result = await execute_broadcast(user_id, groups, advance_rotation=True)
        blocked_groups = result.get("blocked_groups", {}) if isinstance(result.get("blocked_groups", {}), dict) else {}
        for group, err in blocked_groups.items():
            bm.set_group_blocked(group, err)

        # Spend inside lock so next waiter sees updated balance.
        sent_count = int(result.get("sent_count", 0) or 0)
        spend_ok = True
        if sent_count > 0:
            spend_ok = balance_mgr.spend_posts(
                amount=sent_count,
                groups_count=len(groups),
                sent_count=sent_count,
                summary=f"Массовая рассылка: {sent_count} групп",
            )

    # Get updated balance
    balance_after = balance_mgr.get_balance()
    failed_groups = result.get("failed_groups", {}) if isinstance(result.get("failed_groups", {}), dict) else {}
    blocked_count = len(blocked_groups)
    failed_count = len(failed_groups)

    result_text = (
        "✅ <b>РАССЫЛКА ЗАВЕРШЕНА</b>\n\n"
        "📊 <b>СТАТИСТИКА:</b>\n"
        f"├─ Всего групп: {len(groups)}\n"
        f"├─ ✅ Успешно: {sent_count}\n"
        f"├─ ⚠️ Забаны: {blocked_count}\n"
        f"└─ ❌ Другие ошибки: {failed_count}\n\n"
        "💳 <b>ЗАТРАТЫ:</b>\n"
        f"├─ Потрачено: {sent_count} постов (за успешные)\n"
        f"├─ Баланс было: {balance_before} постов\n"
        f"└─ Баланс сейчас: {balance_after} постов\n\n"
        "✅ Платите только за успешные!"
    )

    if not spend_ok and sent_count > 0:
        result_text += "\n\n⚠️ Не удалось списать посты автоматически. Обратитесь в поддержку."

    if blocked_count > 0:
        shown = list(blocked_groups.keys())[:5]
        result_text += f"\n\n🚫 <b>ЗАБАНЫ</b> ({blocked_count}):\n"
        for group in shown:
            result_text += f"• <code>{group}</code>\n"
        if blocked_count > len(shown):
            result_text += "• ...\n"

    state = bm.load()
    await query.message.edit_text(
        result_text,
        parse_mode="HTML",
        reply_markup=broadcast_main_keyboard(state, user_id=user_id),
    )
    await query.answer()


# ─── Баланс и покупки ─────────────────────────────────────────────────────────

@dp.callback_query(F.data == "bc_balance")
async def broadcast_balance(query: CallbackQuery):
    """Show balance and tariff purchase menu."""
    user_id = query.from_user.id
    bm = scoped_balance_manager(user_id)
    state = bm.load()

    await query.message.edit_text(
        broadcast_balance_text(state),
        parse_mode="HTML",
        reply_markup=broadcast_balance_keyboard(),
    )
    await query.answer()


@dp.callback_query(F.data == "bc_balance_history")
async def broadcast_balance_history(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_balance_manager(user_id)
    state = bm.load()
    history = bm.get_history(limit=10)

    await query.message.edit_text(
        broadcast_balance_history_text(state, history),
        parse_mode="HTML",
        reply_markup=broadcast_balance_history_keyboard(),
    )
    await query.answer()


@dp.callback_query(F.data == "bc_buy_small")
async def broadcast_buy_small(query: CallbackQuery):
    await _handle_purchase(query, "small")


@dp.callback_query(F.data == "bc_buy_medium")
async def broadcast_buy_medium(query: CallbackQuery):
    await _handle_purchase(query, "medium")


@dp.callback_query(F.data == "bc_buy_large")
async def broadcast_buy_large(query: CallbackQuery):
    await _handle_purchase(query, "large")


async def _handle_purchase(query: CallbackQuery, tier: str):
    """Handle tariff purchase - create Stripe Checkout and send link."""
    await query.answer()
    try:
        checkout_url = await create_checkout_session(query.from_user.id, tier)
        price_data = STRIPE_PRICES[tier]
        text = (
            f"🛒 <b>{price_data['label']} / €{price_data['price_eur']}</b>\n\n"
            "Нажмите кнопку ниже для оплаты через Stripe:"
        )
        rows = [
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=checkout_url)],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="bc_balance")],
        ]
        await query.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Stripe checkout error: {e}")
        await query.message.edit_text(
            "❌ Ошибка при создании платежа. Попробуйте позже.",
            reply_markup=broadcast_balance_keyboard(),
        )


@dp.callback_query(F.data == "bc_test_disable_failed")
async def broadcast_test_disable_failed(query: CallbackQuery):
    """Disable failed groups after test."""
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    groups_all = scoped_load_broadcast_groups(user_id)
    state = bm.ensure_groups_known(groups_all)

    # Disable all groups with failed test status
    groups_state = state.get("broadcast_groups_state", {})
    disabled_count = 0
    for group, meta in groups_state.items():
        test_status = meta.get("last_test_status")
        if test_status == "failed" or test_status == "deleted":
            bm.set_group_blocked(group, f"test_{test_status}")
            disabled_count += 1

    state = bm.load()
    await query.message.edit_text(
        f"✅ Отключено групп: <b>{disabled_count}</b>",
        parse_mode="HTML",
        reply_markup=broadcast_main_keyboard(state, user_id=user_id),
    )
    await query.answer()


# ─── Сканирование ─────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "scan")
async def scan_callback(query: CallbackQuery, state: FSMContext):
    """Выбор направления перед сканированием"""
    cat_state = load()
    directions = get_directions(cat_state)

    active_channel, channel_error = resolve_active_results_channel()
    channel_text = format_channel_label(active_channel if active_channel is not None else DEFAULT_RESULTS_CHANNEL)
    if channel_error:
        channel_text += " (конфликт в текущем выборе)"

    text = (
        "🔍 <b>Выбери направление</b>\n\n"
        f"Всего групп: <b>{len(load_groups())}</b>\n"
        f"Канал результатов: <code>{channel_text}</code>\n\n"
    )

    buttons = []
    for dir_id, dir_data in directions.items():
        name = dir_data.get("name", dir_id)
        buttons.append([InlineKeyboardButton(text=name, callback_data=f"scan_dir_{dir_id}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])

    await query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await query.answer()


@dp.callback_query(F.data.startswith("scan_dir_"))
async def scan_dir_callback(query: CallbackQuery, state: FSMContext):
    """Выбрано направление, теперь выбираем подкатегории"""
    dir_id = query.data[len("scan_dir_"):]
    cat_state = load()
    directions = get_directions(cat_state)

    if dir_id not in directions:
        await query.answer("❌ Направление не найдено", show_alert=True)
        return

    direction = directions[dir_id]
    subcategories = direction.get("subcategories", {})

    # Инициализируем FSM data для выбора подкатегорий
    await state.set_state(MainMenu.selecting_subcats)
    await state.update_data(
        current_direction=dir_id,
        selected_subcats=set()
    )

    await _render_subcats_list(query, dir_id, set(), subcategories)
    await query.answer()


async def _render_subcats_list(query: CallbackQuery, dir_id: str, selected: set, subcategories: dict):
    """Отрисовывает список подкатегорий с галочками"""
    direction_name = load()["directions"][dir_id]["name"]

    text = f"<b>{direction_name}</b>\n\nВыбери подкатегории ({len(selected)}/{len(subcategories)}):\n\n"
    buttons = []

    for sub_id, sub_data in subcategories.items():
        check = "✅" if sub_id in selected else "  "
        sub_name = sub_data.get("name", sub_id)
        buttons.append([InlineKeyboardButton(
            text=f"{check} {sub_name}",
            callback_data=f"scan_sub_{dir_id}_{sub_id}"
        )])

    # Кнопки управления
    buttons.append([
        InlineKeyboardButton(text="Неважно", callback_data=f"scan_all_{dir_id}"),
    ])
    buttons.append([
        InlineKeyboardButton(text="✅ ГОТОВО", callback_data=f"scan_done_{dir_id}"),
        InlineKeyboardButton(text="← Направления", callback_data="scan"),
    ])

    await query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith("scan_sub_"))
async def scan_sub_callback(query: CallbackQuery, state: FSMContext):
    """Тоггл подкатегории"""
    rest = query.data[len("scan_sub_"):]
    dir_id, sub_id = rest.split("_", 1)

    data = await state.get_data()
    selected = data.get("selected_subcats", set())

    # Тоггл
    if sub_id in selected:
        selected.discard(sub_id)
    else:
        selected.add(sub_id)

    await state.update_data(selected_subcats=selected)

    cat_state = load()
    subcategories = get_subcategories(cat_state, dir_id)
    await _render_subcats_list(query, dir_id, selected, subcategories)
    await query.answer()


@dp.callback_query(F.data.startswith("scan_all_"))
async def scan_all_callback(query: CallbackQuery, state: FSMContext):
    """Выбрать все / снять все подкатегории"""
    dir_id = query.data[len("scan_all_"):]

    data = await state.get_data()
    selected = data.get("selected_subcats", set())

    cat_state = load()
    subcategories = get_subcategories(cat_state, dir_id)
    all_sub_ids = set(subcategories.keys())

    # Если все выбраны, снимаем все; иначе выбираем все
    if selected == all_sub_ids:
        selected = set()
    else:
        selected = all_sub_ids

    await state.update_data(selected_subcats=selected)
    await _render_subcats_list(query, dir_id, selected, subcategories)
    await query.answer()


@dp.callback_query(F.data.startswith("scan_done_"))
async def scan_done_callback(query: CallbackQuery, state: FSMContext):
    """Готово с выбором подкатегорий, переходим к периоду"""
    dir_id = query.data[len("scan_done_"):]

    data = await state.get_data()
    selected = data.get("selected_subcats", set())

    if not selected:
        await query.answer("❌ Выбери хотя бы одну подкатегорию", show_alert=True)
        return

    # Сохраняем активный выбор
    set_active_selection(dir_id, list(selected))

    # Переходим к выбору периода
    text = (
        f"<b>Направление:</b> {load()['directions'][dir_id]['name']}\n"
        f"<b>Подкатегорий выбрано:</b> {len(selected)}\n\n"
        f"<b>Выбери период сканирования:</b>"
    )

    await query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📅 7 дней", callback_data="scan_period_7"),
                InlineKeyboardButton(text="📅 30 дней", callback_data="scan_period_30"),
            ],
            [
                InlineKeyboardButton(text="📅 90 дней", callback_data="scan_period_90"),
                InlineKeyboardButton(text="📅 Всё", callback_data="scan_period_999"),
            ],
            [
                InlineKeyboardButton(text="← Подкатегории", callback_data=f"scan_dir_{dir_id}"),
            ],
        ])
    )
    await query.answer()


@dp.callback_query(F.data.startswith("scan_period_"))
async def execute_scan(query: CallbackQuery, state: FSMContext):
    """Запускает сканирование"""
    global current_scan_task
    parts = query.data.split("_")
    days = int(parts[2])

    # Получаем активный выбор из categories.json
    cat_state = load()
    dir_id = get_active_direction(cat_state)
    direction = get_directions(cat_state).get(dir_id, {})
    direction_name = direction.get("name", dir_id)
    results_channel, channel_error = resolve_results_channel_for_selection(cat_state, DEFAULT_RESULTS_CHANNEL)
    if channel_error:
        await query.answer(f"❌ {channel_error}", show_alert=True)
        return

    keywords = get_active_keywords()
    if not keywords:
        await query.answer("❌ Нет ключевых слов для выбранных подкатегорий", show_alert=True)
        return

    # DEBUG: Log what we got
    print(f"DEBUG execute_scan: dir_id={dir_id}, selected_subcats={get_active_subcategory_ids(cat_state)}")
    print(f"DEBUG execute_scan: keywords_count={len(keywords)}")
    if keywords:
        print(f"DEBUG execute_scan: sample_keywords={keywords[:3]}")

    # Уведомление о начале
    await query.message.edit_text(
        f"⏳ <b>Сканирование: {direction_name}</b>\n\n"
        f"Период: {days} дней\n"
        f"Ключевых слов: {len(keywords)}\n\n"
        "Это может занять несколько минут...",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛑 Отменить", callback_data="scan_cancel")],
        ])
    )
    await query.answer()

    try:
        # Запускаем сканирование как отдельную задачу для возможности отмены
        current_scan_task = asyncio.create_task(scan_groups_history(
            days=days,
            keywords=keywords,
            anti_keywords=load_anti_keywords(),
            results_channel=results_channel,
            include_source_header=should_include_source_header(results_channel),
            session_path=SESSION_PATH,
        ))
        count, processed, skipped = await current_scan_task

        if isinstance(skipped, str):  # Ошибка авторизации/канала
            await query.message.edit_text(
                f"❌ <b>Ошибка при сканировании</b>\n\n{skipped}",
                parse_mode="HTML",
                reply_markup=back_button()
            )
            return

        await query.message.edit_text(
            f"✅ <b>Сканирование завершено!</b>\n\n"
            f"📂 Направление: {direction_name}\n"
            f"🎯 Найдено: <b>{count}</b> совпадений\n"
            f"📅 Период: {days} дней\n\n"
            f"🔑 Ключевых слов использовано: <b>{len(keywords)}</b>\n\n"
            f"📨 Результаты пересланы в <code>{format_channel_label(results_channel)}</code>",
            parse_mode="HTML",
            reply_markup=back_button()
        )

    except asyncio.CancelledError:
        await query.message.edit_text(
            f"❌ <b>Сканирование отменено</b>\n\n"
            f"📂 Направление: {direction_name}\n"
            f"📅 Период: {days} дней",
            parse_mode="HTML",
            reply_markup=back_button()
        )

    except Exception as e:
        print(f"Ошибка при сканировании: {e}")
        await query.message.edit_text(
            f"❌ <b>Ошибка при сканировании</b>\n\n{str(e)}",
            parse_mode="HTML",
            reply_markup=back_button()
        )

    finally:
        current_scan_task = None


@dp.callback_query(F.data == "scan_cancel")
async def scan_cancel_callback(query: CallbackQuery):
    """Отмена текущего сканирования"""
    global current_scan_task
    if current_scan_task and not current_scan_task.done():
        current_scan_task.cancel()
        await query.answer("🛑 Отмена сканирования...")
    else:
        await query.answer("❌ Сканирование уже завершено", show_alert=True)


# ─── Мониторинг ───────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "monitor")
async def monitor_callback(query: CallbackQuery):
    is_running = current_monitor_task is not None and not current_monitor_task.done()
    channel_id, channel_error = resolve_active_results_channel()
    channel_text = format_channel_label(channel_id if channel_id is not None else DEFAULT_RESULTS_CHANNEL)

    status_text = "🟢 включен" if is_running else "🔴 выключен"
    extra = ""
    if channel_error:
        extra = f"\n⚠️ {channel_error}"

    await query.message.edit_text(
        "⏱️ <b>Реалтайм мониторинг</b>\n\n"
        f"Статус: <b>{status_text}</b>\n"
        f"Канал результатов: <code>{channel_text}</code>\n"
        "Новые посты отслеживаются по активным ключевым словам."
        f"{extra}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Включить" if not is_running else "🔄 Перезапустить", callback_data="monitor_on"),
                InlineKeyboardButton(text="❌ Отключить", callback_data="monitor_off"),
            ],
            [
                InlineKeyboardButton(text="◀️ Назад", callback_data="back_main"),
            ],
        ])
    )
    await query.answer()


async def _monitor_runner(
    keywords: list[str],
    anti_keywords: list[str],
    results_channel: int,
    stop_event: asyncio.Event,
    include_source_header: bool,
):
    try:
        await monitor_groups_realtime(
            keywords=keywords,
            anti_keywords=anti_keywords,
            results_channel=results_channel,
            include_source_header=include_source_header,
            session_path=SESSION_PATH,
            groups=load_groups(),
            stop_event=stop_event,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"Ошибка мониторинга: {e}")


@dp.callback_query(F.data == "monitor_on")
async def monitor_on(query: CallbackQuery):
    global current_monitor_task, monitor_stop_event

    # Перезапуск, если уже работает
    if current_monitor_task and not current_monitor_task.done():
        if monitor_stop_event:
            monitor_stop_event.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(current_monitor_task, timeout=10)
        current_monitor_task = None
        monitor_stop_event = None

    cat_state = load()
    results_channel, channel_error = resolve_results_channel_for_selection(cat_state, DEFAULT_RESULTS_CHANNEL)
    if channel_error:
        await query.answer(f"❌ {channel_error}", show_alert=True)
        return

    keywords = get_active_keywords()
    if not keywords:
        await query.answer("❌ Нет ключевых слов для выбранных подкатегорий", show_alert=True)
        return

    monitor_stop_event = asyncio.Event()
    current_monitor_task = asyncio.create_task(
        _monitor_runner(
            keywords=keywords,
            anti_keywords=load_anti_keywords(),
            results_channel=results_channel,
            stop_event=monitor_stop_event,
            include_source_header=should_include_source_header(results_channel),
        )
    )

    await query.message.edit_text(
        "✅ <b>Мониторинг включен!</b>\n\n"
        f"🔔 Новые совпадения будут приходить в <code>{format_channel_label(results_channel)}</code>",
        parse_mode="HTML",
        reply_markup=back_button()
    )
    await query.answer()


@dp.callback_query(F.data == "monitor_off")
async def monitor_off(query: CallbackQuery):
    global current_monitor_task, monitor_stop_event
    stopped = False

    if current_monitor_task and not current_monitor_task.done():
        stopped = True
        if monitor_stop_event:
            monitor_stop_event.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(current_monitor_task, timeout=10)

    current_monitor_task = None
    monitor_stop_event = None

    await query.message.edit_text(
        "❌ <b>Мониторинг отключен</b>" if stopped else "ℹ️ <b>Мониторинг уже был отключен</b>",
        parse_mode="HTML",
        reply_markup=back_button()
    )
    await query.answer()


# ─── Статус ───────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "status")
async def status_callback(query: CallbackQuery):
    cat_state = load()
    active_dir = get_active_direction(cat_state)
    active_subcat_ids = get_active_subcategory_ids(cat_state)
    keywords = get_active_keywords()

    if active_dir:
        direction = get_directions(cat_state).get(active_dir, {})
        dir_name = direction.get("name", active_dir)
        subcats = get_subcategories(cat_state, active_dir)
        subcat_names = [subcats.get(s_id, {}).get("name", s_id) for s_id in active_subcat_ids]
        subcat_text = ", ".join(subcat_names) if subcat_names else "Не выбрано"
    else:
        dir_name = "Не выбрано"
        subcat_text = "Не выбрано"

    keywords_text = "\n".join([f"  • {kw}" for kw in keywords[:10]])
    results_channel, channel_error = resolve_results_channel_for_selection(cat_state, DEFAULT_RESULTS_CHANNEL)
    channel_text = format_channel_label(results_channel if results_channel is not None else DEFAULT_RESULTS_CHANNEL)
    if channel_error:
        channel_text += " (конфликт выбранных подкатегорий)"

    text = (
        "📊 <b>Статус сканирования</b>\n\n"
        f"<b>📂 Активное направление:</b> {dir_name}\n"
        f"<b>📁 Подкатегории:</b> {subcat_text}\n\n"
        f"<b>🔑 Ключевые слова ({len(keywords)}):</b>\n"
        f"{keywords_text}\n"
        f"{'  ...' if len(keywords) > 10 else ''}\n\n"
        f"<b>📌 Всего групп:</b> {len(load_groups())}\n"
        f"<b>📨 Канал:</b> <code>{channel_text}</code>"
    )

    await query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=back_button()
    )
    await query.answer()


# ─── Настройки ────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "settings")
async def settings_callback(query: CallbackQuery):
    await query.message.edit_text(
        "⚙️ <b>Настройки</b>",
        parse_mode="HTML",
        reply_markup=settings_keyboard()
    )
    await query.answer()


@dp.callback_query(F.data == "settings_notifications")
async def settings_notifications_callback(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    enabled = bm.get_balance_notif_enabled()
    threshold = bm.get_balance_notif_threshold()

    await query.message.edit_text(
        settings_notifications_text(enabled, threshold),
        parse_mode="HTML",
        reply_markup=settings_notifications_keyboard(enabled),
    )
    await query.answer()


@dp.callback_query(F.data == "notif_threshold_menu")
async def notif_threshold_menu_callback(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    current_threshold = bm.get_balance_notif_threshold()

    await query.message.edit_text(
        notifications_threshold_text(current_threshold),
        parse_mode="HTML",
        reply_markup=notifications_threshold_keyboard(),
    )
    await query.answer()


@dp.callback_query(F.data.startswith("notif_set_threshold_"))
async def notif_set_threshold_callback(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    raw_value = (query.data or "").split("_")[-1]
    try:
        threshold = int(raw_value)
        bm.set_balance_notif_threshold(threshold)
    except Exception:
        await query.answer("Неверное значение порога.", show_alert=True)
        return

    await query.answer(f"Порог обновлен: {threshold}")
    current_threshold = bm.get_balance_notif_threshold()
    await query.message.edit_text(
        notifications_threshold_text(current_threshold),
        parse_mode="HTML",
        reply_markup=notifications_threshold_keyboard(),
    )


@dp.callback_query(F.data == "notif_balance_toggle")
async def notif_balance_toggle_callback(query: CallbackQuery):
    user_id = query.from_user.id
    bm = scoped_broadcast_manager(user_id)
    current = bm.get_balance_notif_enabled()
    bm.set_balance_notif_enabled(not current)
    enabled = bm.get_balance_notif_enabled()
    threshold = bm.get_balance_notif_threshold()

    await query.message.edit_text(
        settings_notifications_text(enabled, threshold),
        parse_mode="HTML",
        reply_markup=settings_notifications_keyboard(enabled),
    )
    await query.answer("Настройка уведомления о балансе обновлена.")


@dp.callback_query(F.data == "anti_keywords")
async def anti_keywords_callback(query: CallbackQuery):
    """Показать список стоп-слов"""
    words = load_anti_keywords()
    word_count = len(words)

    # Форматируем текст со списком слов
    words_text = ", ".join(words[:30]) if words else "(пусто)"
    text = f"🚫 <b>Стоп-слова ({word_count})</b>:\n\n{words_text}"

    await query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=anti_keywords_keyboard(words)
    )
    await query.answer()


@dp.callback_query(F.data == "antikw_add")
async def antikw_add_callback(query: CallbackQuery, state: FSMContext):
    """Запрос на добавление нового стоп-слова"""
    await query.message.edit_text(
        "✏️ Введи новое стоп-слово:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="anti_keywords")],
        ])
    )
    await state.set_state(MainMenu.adding_anti_keyword)
    await query.answer()


@dp.callback_query(F.data.startswith("antikw_del_"))
async def antikw_del_callback(query: CallbackQuery):
    """Удалить стоп-слово"""
    word = query.data.replace("antikw_del_", "")
    remove_anti_keyword(word)

    # Обновляем вид
    words = load_anti_keywords()
    word_count = len(words)
    words_text = ", ".join(words[:30]) if words else "(пусто)"
    text = f"🚫 <b>Стоп-слова ({word_count})</b>:\n\n{words_text}"

    await query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=anti_keywords_keyboard(words, page=0)
    )
    await query.answer()


@dp.callback_query(F.data.startswith("antikw_page_"))
async def antikw_page_callback(query: CallbackQuery):
    """Переключение на другую страницу стоп-слов"""
    page = int(query.data.replace("antikw_page_", ""))
    words = load_anti_keywords()
    word_count = len(words)
    words_text = ", ".join(words[:30]) if words else "(пусто)"
    text = f"🚫 <b>Стоп-слова ({word_count})</b>:\n\n{words_text}"

    await query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=anti_keywords_keyboard(words, page=page)
    )
    await query.answer()


@dp.callback_query(F.data == "antikw_noop")
async def antikw_noop_callback(query: CallbackQuery):
    """No-op callback для нейтральных кнопок (счётчик страниц, пустые кнопки)"""
    await query.answer()


@dp.message(MainMenu.adding_anti_keyword)
async def add_anti_keyword_input(message: Message, state: FSMContext):
    """Добавить введённое стоп-слово"""
    word = message.text.strip()

    if not word:
        await message.answer("❌ Пусто слово не добавляем!")
        return

    add_anti_keyword(word)
    words = load_anti_keywords()
    word_count = len(words)
    words_text = ", ".join(words[:30]) if words else "(пусто)"
    text = f"🚫 <b>Стоп-слова ({word_count})</b>:\n\n{words_text}\n\n✅ Стоп-слово добавлено!"

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=anti_keywords_keyboard(words, page=0)
    )
    await state.clear()


@dp.callback_query(F.data == "categories")
async def categories_callback(query: CallbackQuery):
    """Управление категориями - выбор направления"""
    cat_state = load()
    directions = get_directions(cat_state)

    text = "📂 <b>Управление категориями</b>\n\nВыбери направление:"

    buttons = []
    for dir_id, dir_data in directions.items():
        dir_name = dir_data.get("name", dir_id)
        subcat_count = len(dir_data.get("subcategories", {}))
        buttons.append([InlineKeyboardButton(
            text=f"{dir_name} ({subcat_count})",
            callback_data=f"cat_dir_{dir_id}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="settings")])

    await query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await query.answer()


@dp.callback_query(F.data.startswith("cat_dir_"))
async def cat_dir_callback(query: CallbackQuery):
    """Показать подкатегории в направлении"""
    dir_id = query.data[len("cat_dir_"):]
    ok, text, markup = render_dir_subcats(dir_id)
    if not ok:
        await query.answer("Направление не найдено", show_alert=True)
        return

    await query.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    await query.answer()


def render_subcat_keywords(dir_id: str, sub_id: str) -> tuple[str, InlineKeyboardMarkup]:
    """Формирует текст и клавиатуру для списка ключевых слов подкатегории"""
    cat_state = load()
    directions = get_directions(cat_state)

    subcats = directions.get(dir_id, {}).get("subcategories", {})
    subcat = subcats.get(sub_id, {})
    sub_name = subcat.get("name", sub_id)
    keywords = subcat.get("keywords", [])

    text = format_keywords_columns(sub_name, keywords)

    buttons = [
        [InlineKeyboardButton(text="➕ Добавить слово", callback_data=f"kadd_{dir_id}_{sub_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"kaskdel_{dir_id}_{sub_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"cat_dir_{dir_id}")],
    ]

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


def format_keywords_columns(title: str, keywords: list[str], per_col: int = 10, max_show: int = 60) -> str:
    """Формирует текст с колонками (блоками по per_col)."""
    shown = keywords[:max_show]
    blocks = []
    for start in range(0, len(shown), per_col):
        chunk = shown[start:start + per_col]
        lines = [f"{i+1}. {kw}" for i, kw in enumerate(chunk, start=start + 1)]
        blocks.append("\n".join(lines))

    extra = len(keywords) - len(shown)
    extra_text = f"\n... ещё {extra}" if extra > 0 else ""

    return f"<b>{title}</b>\n\n🔑 <b>Ключевые слова ({len(keywords)}):</b>\n\n" + "\n\n".join(blocks) + extra_text


def render_dir_subcats(dir_id: str) -> tuple[bool, str, InlineKeyboardMarkup]:
    """Возвращает (ok, text, markup) для списка подкатегорий направления."""
    cat_state = load()
    directions = get_directions(cat_state)

    if dir_id not in directions:
        return False, "", InlineKeyboardMarkup(inline_keyboard=[])

    direction = directions[dir_id]
    dir_name = direction.get("name", dir_id)
    subcats = direction.get("subcategories", {})

    text = f"<b>{dir_name}</b>\n\nВыбери подкатегорию для управления:"

    buttons = []
    for sub_id, sub_data in subcats.items():
        sub_name = sub_data.get("name", sub_id)
        kw_count = len(sub_data.get("keywords", []))
        buttons.append([InlineKeyboardButton(
            text=f"{sub_name} ({kw_count} слов)",
            callback_data=f"cat_sub_{dir_id}_{sub_id}"
        )])

    buttons.append([InlineKeyboardButton(text="➕ Добавить подкатегорию", callback_data=f"add_sub_{dir_id}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="categories")])

    return True, text, InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data.startswith("cat_sub_"))
async def cat_sub_callback(query: CallbackQuery):
    """Показать ключевые слова подкатегории"""
    rest = query.data[len("cat_sub_"):]
    dir_id, sub_id = rest.split("_", 1)

    cat_state = load()
    directions = get_directions(cat_state)

    if dir_id not in directions:
        await query.answer("Направление не найдено", show_alert=True)
        return

    if sub_id not in directions[dir_id].get("subcategories", {}):
        await query.answer("Подкатегория не найдена", show_alert=True)
        return

    text, markup = render_subcat_keywords(dir_id, sub_id)
    await query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=markup
    )
    await query.answer()


@dp.callback_query(F.data.startswith("kaskdel_"))
async def cat_ask_delete_sub_callback(query: CallbackQuery, state: FSMContext):
    """Запрос ввода слов для удаления"""
    rest = query.data[len("kaskdel_"):]
    dir_id, sub_id = rest.split("_", 1)

    text = (
        "🗑 <b>Удаление слов</b>\n\n"
        "Введи одно слово или несколько через # (пример: слово1 #фраза два #слово3).\n"
        "Совпадения ищутся без учёта регистра."
    )
    await query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"cat_sub_{dir_id}_{sub_id}")],
        ])
    )
    await state.set_state(MainMenu.deleting_subcat_keyword)
    await state.update_data(dir_id=dir_id, sub_id=sub_id)
    await query.answer()


@dp.callback_query(F.data.startswith("kadd_"))
async def cat_addkw_sub_callback(query: CallbackQuery, state: FSMContext):
    """Начать добавление нового ключевого слова в подкатегорию"""
    rest = query.data[len("kadd_"):]
    dir_id, sub_id = rest.split("_", 1)

    cat_state = load()
    directions = get_directions(cat_state)

    if dir_id not in directions:
        await query.answer("Направление не найдено", show_alert=True)
        return

    direction = directions[dir_id]
    subcats = direction.get("subcategories", {})

    if sub_id not in subcats:
        await query.answer("Подкатегория не найдена", show_alert=True)
        return

    subcat = subcats[sub_id]
    sub_name = subcat.get("name", sub_id)

    await query.message.edit_text(
        f"➕ <b>Добавление слова</b>\n\n<b>{sub_name}</b>\n\n"
        f"<b>Введи новое ключевое слово</b>\n"
        f"• одно слово без решёток\n"
        f"• несколько слов через # (пример: слово1 #фраза два #слово3)",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"cat_sub_{dir_id}_{sub_id}")],
        ])
    )

    await state.set_state(MainMenu.adding_subcat_keyword)
    await state.update_data(dir_id=dir_id, sub_id=sub_id)
    await query.answer()


@dp.callback_query(F.data.startswith("add_sub_"))
async def cat_add_subcategory_callback(query: CallbackQuery, state: FSMContext):
    """Начать добавление новой подкатегории"""
    dir_id = query.data[len("add_sub_"):]

    ok, _, _ = render_dir_subcats(dir_id)
    if not ok:
        await query.answer("Направление не найдено", show_alert=True)
        return

    await query.message.edit_text(
        "🆕 <b>Новая подкатегория</b>\n\nВведи название (можно с пробелами). Идентификатор создадим автоматически.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"cat_dir_{dir_id}")],
        ])
    )
    await state.set_state(MainMenu.adding_subcategory)
    await state.update_data(dir_id=dir_id)
    await query.answer()


@dp.message(MainMenu.adding_subcat_keyword)
async def add_subcat_keyword_input(message: Message, state: FSMContext):
    """Обработка ввода нового ключевого слова/слов в подкатегорию"""
    data = await state.get_data()
    dir_id = data.get("dir_id")
    sub_id = data.get("sub_id")

    raw_text = (message.text or "").strip()
    if not raw_text:
        await message.answer("❌ Введи непустое слово!")
        return

    if "#" in raw_text:
        parts = [p.strip() for p in raw_text.split("#") if p.strip()]
    else:
        parts = [raw_text]

    # Убираем дубликаты в рамках ввода, сохраняем порядок
    seen = set()
    keywords = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            keywords.append(p)

    added, skipped = add_keywords_to_subcat(dir_id, sub_id, keywords)

    if added == 0:
        await message.answer("❌ Не удалось добавить слова (возможно, все дубликаты).")
        return

    text, markup = render_subcat_keywords(dir_id, sub_id)
    info = f"✅ Добавлено: {added}"
    if skipped:
        info += f", пропущено (дубликаты/пустые): {skipped}"

    await message.answer(info)
    await message.answer(text, parse_mode="HTML", reply_markup=markup)

    await state.set_state(MainMenu.viewing)


@dp.message(MainMenu.deleting_subcat_keyword)
async def delete_subcat_keyword_input(message: Message, state: FSMContext):
    """Удаление слов по вводу через #"""
    data = await state.get_data()
    dir_id = data.get("dir_id")
    sub_id = data.get("sub_id")

    raw_text = (message.text or "").strip()
    if not raw_text:
        await message.answer("❌ Введи слова для удаления.")
        return

    parts = [p.strip() for p in raw_text.split("#") if p.strip()] if "#" in raw_text else [raw_text]

    # уникализируем для удаления
    seen = set()
    keywords = []
    for p in parts:
        low = p.casefold()
        if low not in seen:
            seen.add(low)
            keywords.append(p)

    removed, not_found = remove_keywords_from_subcat(dir_id, sub_id, keywords)
    info = f"✅ Удалено: {removed}"
    if not_found:
        info += f"; не найдено: {not_found}"
    if removed == 0:
        info = "❌ Ничего не удалено (не найдено совпадений)."

    text, markup = render_subcat_keywords(dir_id, sub_id)
    await message.answer(info)
    await message.answer(text, parse_mode="HTML", reply_markup=markup)
    await state.set_state(MainMenu.viewing)


@dp.message(MainMenu.adding_subcategory)
async def add_subcategory_input(message: Message, state: FSMContext):
    """Создание новой подкатегории"""
    data = await state.get_data()
    dir_id = data.get("dir_id")

    name_raw = (message.text or "").strip()
    if not name_raw:
        await message.answer("❌ Введи название подкатегории.")
        return

    # Генерируем sub_id из названия
    slug = re.sub(r"\s+", "_", name_raw.strip())
    slug = re.sub(r"[^\w_]", "", slug)
    slug = slug.lower()
    if not slug:
        await message.answer("❌ Не удалось сформировать идентификатор, попробуй другое название.")
        return

    # Разрешаем коллизию с суффиксом
    cat_state = load()
    subcats = get_directions(cat_state).get(dir_id, {}).get("subcategories", {})
    candidate = slug
    idx = 1
    while candidate in subcats:
        candidate = f"{slug}_{idx}"
        idx += 1

    if not add_subcategory(dir_id, candidate, name_raw):
        await message.answer("❌ Не удалось добавить подкатегорию.")
        return

    ok, text, markup = render_dir_subcats(dir_id)
    if ok:
        await message.answer("✅ Подкатегория добавлена.")
        await message.answer(text, parse_mode="HTML", reply_markup=markup)
    await state.set_state(MainMenu.viewing)


@dp.callback_query(F.data.startswith("settings_cat_"))
async def settings_cat_callback(query: CallbackQuery):
    """Подменю управления категорией"""
    cat_id = query.data[len("settings_cat_"):]
    categories = get_all_categories()

    if cat_id not in categories:
        await query.answer("❌ Категория не найдена", show_alert=True)
        return

    cat_data = categories[cat_id]
    is_active = cat_data.get("active", False)
    keywords_count = len(cat_data.get("keywords", []))

    text = f"📂 <b>Категория: {cat_data.get('name')}</b>\n\n"
    text += f"Ключевых слов: <b>{keywords_count}</b>\n"
    text += f"Статус: {'✅ Активная' if is_active else '⚪ Неактивная'}\n"

    rows = []
    if not is_active:
        rows.append([InlineKeyboardButton(text="✅ Сделать активной", callback_data=f"cat_activate_{cat_id}")])
    rows.append([InlineKeyboardButton(text="🔑 Ключевые слова", callback_data=f"cat_keywords_{cat_id}")])
    rows.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"cat_del_{cat_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="categories")])

    await query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await query.answer()


@dp.callback_query(F.data.startswith("cat_activate_"))
async def cat_activate_callback(query: CallbackQuery):
    """Сделать категорию активной"""
    cat_id = query.data[len("cat_activate_"):]
    if set_active_category(cat_id):
        categories = get_all_categories()
        cat_data = categories[cat_id]
        await query.message.edit_text(
            f"✅ <b>Категория активирована!</b>\n\n"
            f"{cat_data.get('name')}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="categories")],
            ])
        )
        await query.answer()
    else:
        await query.answer("❌ Ошибка при активировании категории", show_alert=True)


@dp.callback_query(F.data.startswith("cat_del_"))
async def cat_del_callback(query: CallbackQuery):
    """Удаление категории (с подтверждением)"""
    cat_id = query.data[len("cat_del_"):]
    categories = get_all_categories()

    if cat_id not in categories:
        await query.answer("❌ Категория не найдена", show_alert=True)
        return

    if len(categories) <= 1:
        await query.answer("❌ Нельзя удалить единственную категорию", show_alert=True)
        return

    cat_data = categories[cat_id]
    await query.message.edit_text(
        f"🗑 <b>Удалить категорию?</b>\n\n{cat_data.get('name')}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Удалить", callback_data=f"cat_del_confirm_{cat_id}"),
                InlineKeyboardButton(text="◀️ Отмена", callback_data=f"settings_cat_{cat_id}"),
            ],
        ])
    )
    await query.answer()


@dp.callback_query(F.data.startswith("cat_del_confirm_"))
async def cat_del_confirm_callback(query: CallbackQuery):
    """Подтверждение удаления категории"""
    cat_id = query.data[len("cat_del_confirm_"):]
    if delete_category(cat_id):
        await query.message.edit_text(
            "✅ <b>Категория удалена!</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="categories")],
            ])
        )
        await query.answer()
    else:
        await query.answer("❌ Ошибка при удалении категории", show_alert=True)


@dp.callback_query(F.data.startswith("cat_keywords_"))
async def cat_keywords_callback(query: CallbackQuery):
    """Управление ключевыми словами категории"""
    cat_id = query.data[len("cat_keywords_"):]
    categories = get_all_categories()

    if cat_id not in categories:
        await query.answer("❌ Категория не найдена", show_alert=True)
        return

    cat_data = categories[cat_id]
    keywords = cat_data.get("keywords", [])

    text = f"🔑 <b>Ключевые слова: {cat_data.get('name')}</b>\n\n"

    rows = []
    row = []
    for idx, kw in enumerate(keywords):
        row.append(InlineKeyboardButton(text=kw, callback_data=f"kw_{cat_id}_{idx}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton(text="➕ Добавить слово", callback_data=f"cat_addkw_{cat_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"settings_cat_{cat_id}")])

    await query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await query.answer()


@dp.callback_query(F.data.startswith("kw_"))
async def kw_callback(query: CallbackQuery):
    """Показать подтверждение удаления ключевого слова"""
    # Парсим callback_data: kw_{cat_id}_{idx}
    # Используем rsplit чтобы индекс был в конце
    prefix_removed = query.data[3:]  # убираем "kw_"
    cat_id, idx_str = prefix_removed.rsplit("_", 1)

    try:
        idx = int(idx_str)
    except (ValueError, IndexError):
        await query.answer("❌ Некорректные данные", show_alert=True)
        return

    categories = get_all_categories()
    if cat_id not in categories:
        await query.answer("❌ Категория не найдена", show_alert=True)
        return

    keywords = categories[cat_id].get("keywords", [])
    if idx < 0 or idx >= len(keywords):
        await query.answer("❌ Слово не найдено", show_alert=True)
        return

    keyword = keywords[idx]
    cat_name = categories[cat_id].get("name", cat_id)

    await query.message.edit_text(
        f"🗑 <b>Удалить ключевое слово?</b>\n\n<code>{keyword}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Удалить", callback_data=f"kwdel_{cat_id}_{idx}"),
                InlineKeyboardButton(text="◀️ Отмена", callback_data=f"cat_keywords_{cat_id}"),
            ],
        ])
    )
    await query.answer()


@dp.callback_query(F.data.startswith("kwdel_"))
async def kwdel_callback(query: CallbackQuery):
    """Подтверждено удаление ключевого слова"""
    prefix_removed = query.data[6:]  # убираем "kwdel_"
    cat_id, idx_str = prefix_removed.rsplit("_", 1)

    try:
        idx = int(idx_str)
    except (ValueError, IndexError):
        await query.answer("❌ Некорректные данные", show_alert=True)
        return

    if remove_keyword(cat_id, idx):
        # Показываем обновленный список слов
        categories = get_all_categories()
        cat_data = categories.get(cat_id)
        if cat_data:
            keywords = cat_data.get("keywords", [])
            text = f"🔑 <b>Ключевые слова: {cat_data.get('name')}</b>\n\n"

            rows = []
            row = []
            for kw_idx, kw in enumerate(keywords):
                row.append(InlineKeyboardButton(text=kw, callback_data=f"kw_{cat_id}_{kw_idx}"))
                if len(row) == 3:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)

            rows.append([InlineKeyboardButton(text="➕ Добавить слово", callback_data=f"cat_addkw_{cat_id}")])
            rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"settings_cat_{cat_id}")])

            await query.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
            )
            await query.answer("✅ Слово удалено")
        else:
            await query.answer("❌ Категория не найдена", show_alert=True)
    else:
        await query.answer("❌ Ошибка при удалении слова", show_alert=True)


@dp.callback_query(F.data.startswith("cat_addkw_"))
async def cat_addkw_callback(query: CallbackQuery, state: FSMContext):
    """Начинаем добавление нового ключевого слова (старый режим категорий)"""
    cat_id = query.data[len("cat_addkw_"):]
    categories = get_all_categories()

    if cat_id not in categories:
        await query.answer("❌ Категория не найдена", show_alert=True)
        return

    await query.message.edit_text(
        "✏️ <b>Введи новое ключевое слово:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"cat_keywords_{cat_id}")],
        ])
    )
    await state.set_state(MainMenu.adding_category_keyword)
    await state.update_data(category_id=cat_id)
    await query.answer()


@dp.message(MainMenu.adding_category_keyword)
async def add_category_keyword_input(message: Message, state: FSMContext):
    """Обработка ввода нового ключевого слова (старый режим категорий)"""
    data = await state.get_data()
    cat_id = data.get("category_id")

    keyword = message.text.strip() if message.text else ""

    if not keyword:
        await message.answer("❌ Введи ключевое слово!")
        return

    if add_keyword(cat_id, keyword):
        categories = get_all_categories()
        cat_data = categories.get(cat_id)
        if cat_data:
            keywords = cat_data.get("keywords", [])
            text = f"🔑 <b>Ключевые слова: {cat_data.get('name')}</b>\n\n"

            rows = []
            row = []
            for idx, kw in enumerate(keywords):
                row.append(InlineKeyboardButton(text=kw, callback_data=f"kw_{cat_id}_{idx}"))
                if len(row) == 3:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)

            rows.append([InlineKeyboardButton(text="➕ Добавить слово", callback_data=f"cat_addkw_{cat_id}")])
            rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"settings_cat_{cat_id}")])

            await message.answer(
                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
            )
        await state.set_state(MainMenu.viewing)
    else:
        await message.answer("❌ Ошибка при добавлении слова")
        await state.set_state(MainMenu.viewing)


@dp.callback_query(F.data == "keywords")
async def keywords_callback(query: CallbackQuery, state: FSMContext):
    """Редактирование ключевых слов активной категории"""
    _, active = get_active_category()
    cat_id = [k for k, v in get_all_categories().items() if v.get("active")][0]

    keywords = active.get("keywords", [])
    keywords_str = ".".join(keywords)

    text = (
        f"🔑 <b>Редактирование ключевых слов</b>\n\n"
        f"Категория: {active.get('name')}\n\n"
        f"<b>Текущие слова ({len(keywords)}):</b>\n"
        f"<code>{keywords_str}</code>\n\n"
        f"<b>Введи новые слова через точку (.):</b>"
    )

    await query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="settings")],
        ])
    )

    await state.set_state(MainMenu.editing_keywords)
    await state.update_data(category_id=cat_id)
    await query.answer()


@dp.message(MainMenu.editing_keywords)
async def process_keywords_input(message: Message, state: FSMContext):
    """Обработка ввода новых ключевых слов"""
    data = await state.get_data()
    cat_id = data.get("category_id")

    # Парсим ключевые слова (разделены точками)
    keywords = [kw.strip() for kw in message.text.split(".") if kw.strip()]

    if not keywords:
        await message.answer("❌ Введи хотя бы одно слово!")
        return

    # Сохраняем
    if edit_category(cat_id, keywords=keywords):
        categories = get_all_categories()
        cat_data = categories[cat_id]
        keywords_str = ", ".join(keywords)

        await message.answer(
            f"✅ <b>Ключевые слова обновлены!</b>\n\n"
            f"Категория: {cat_data.get('name')}\n"
            f"Новые слова: {keywords_str}",
            parse_mode="HTML",
            reply_markup=back_button()
        )
    else:
        await message.answer("❌ Ошибка при сохранении")

    await state.set_state(MainMenu.viewing)


@dp.callback_query(F.data.startswith("dir_select_"))
async def dir_select_callback(query: CallbackQuery):
    """Выбор направления (для показа подкатегорий в настройках)"""
    dir_id = query.data[len("dir_select_"):]
    cat_state = load()
    direction = get_directions(cat_state).get(dir_id, {})
    subcategories = direction.get("subcategories", {})
    subcat_names = [subcategories.get(s_id, {}).get("name", s_id) for s_id in subcategories.keys()]
    subcat_text = "\n".join([f"  • {name}" for name in subcat_names])

    text = (
        f"<b>{direction.get('name', dir_id)}</b>\n\n"
        f"<b>Подкатегории ({len(subcat_names)}):</b>\n"
        f"{subcat_text}\n\n"
        f"<i>Для изменения выбора используй 'Сканировать'</i>"
    )

    await query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="categories")]
        ])
    )
    await query.answer()


@dp.callback_query(F.data == "groups")
async def groups_callback(query: CallbackQuery):
    groups = load_groups()
    await query.message.edit_text(
        f"👥 <b>Группы для мониторинга</b>\n\nВсего групп: <b>{len(groups)}</b>",
        parse_mode="HTML",
        reply_markup=groups_keyboard(0)
    )
    await query.answer()


@dp.callback_query(F.data.startswith("groups_page_"))
async def groups_page(query: CallbackQuery):
    page = int(query.data.split("_")[-1])
    groups = load_groups()
    await query.message.edit_text(
        f"👥 <b>Группы для мониторинга</b>\n\nВсего групп: <b>{len(groups)}</b>",
        parse_mode="HTML",
        reply_markup=groups_keyboard(page)
    )
    await query.answer()


@dp.callback_query(F.data == "noop")
async def noop_callback(query: CallbackQuery):
    await query.answer()


@dp.callback_query(F.data.startswith("group_view_"))
async def group_view(query: CallbackQuery):
    username = query.data[len("group_view_"):]
    await query.message.edit_text(
        f"👥 <b>@{username}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑️ Удалить группу", callback_data=f"group_del_{username}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="groups")],
        ])
    )
    await query.answer()


@dp.callback_query(F.data.startswith("group_del_"))
async def group_delete(query: CallbackQuery):
    username = query.data[len("group_del_"):]
    delete_group(username)
    groups = load_groups()
    await query.message.edit_text(
        f"✅ Группа @{username} удалена.\n\nВсего групп: <b>{len(groups)}</b>",
        parse_mode="HTML",
        reply_markup=groups_keyboard(0)
    )
    await query.answer()


@dp.callback_query(F.data == "group_add")
async def group_add_prompt(query: CallbackQuery, state: FSMContext):
    await query.message.edit_text(
        "➕ <b>Добавить группу</b>\n\n"
        "Отправьте одно из:\n"
        "• <code>@username</code>\n"
        "• <code>https://t.me/username</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="groups")],
        ])
    )
    await state.set_state(MainMenu.adding_group)
    await query.answer()


@dp.message(MainMenu.adding_group)
async def process_group_input(message: Message, state: FSMContext):
    text = message.text.strip()
    if text.startswith("https://t.me/"):
        username = text.split("t.me/")[-1].strip("/")
    elif text.startswith("@"):
        username = text[1:]
    else:
        await message.answer("❌ Неверный формат. Используй @username или https://t.me/username")
        return

    if "/" in username or " " in username or not username:
        await message.answer("❌ Некорректное имя группы. Попробуй снова.")
        return

    groups = load_groups()
    if username in groups:
        await message.answer(
            f"⚠️ <b>Дубликат не сохранен</b>\n\nГруппа @{username} уже в списке.",
            parse_mode="HTML"
        )
        return

    add_group(username)
    groups = load_groups()
    await state.set_state(MainMenu.viewing)
    await message.answer(
        f"✅ <b>Группа @{username} добавлена!</b>\n\nВсего групп: <b>{len(groups)}</b>",
        parse_mode="HTML",
        reply_markup=groups_keyboard(0)
    )


# ─── Справка ──────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "help")
async def help_callback(query: CallbackQuery):
    channel_id, _ = resolve_active_results_channel()
    channel_text = format_channel_label(channel_id if channel_id is not None else DEFAULT_RESULTS_CHANNEL)
    await query.message.edit_text(
        "ℹ️ <b>Справка</b>\n\n"
        "<b>🔍 Сканирование</b>\n"
        "Полный поиск по всем группам за выбранный период\n\n"
        "<b>⏱️ Мониторинг</b>\n"
        "Реалтайм отслеживание новых сообщений\n\n"
        "<b>📂 Категории</b>\n"
        "Создавай разные наборы ключевых слов для разных поисков\n\n"
        "<b>📊 Результаты</b>\n"
        f"Все результаты отправляются в канал <code>{channel_text}</code>",
        parse_mode="HTML",
        reply_markup=back_button()
    )
    await query.answer()


# ─── Stripe Webhook HTTP Handlers ──────────────────────────────────────────────

async def stripe_webhook_handler(request: web.Request) -> web.Response:
    """Handle Stripe webhook events."""
    payload = await request.read()
    sig_header = request.headers.get("stripe-signature", "")

    result = await process_webhook(payload, sig_header)

    if "error" in result:
        return web.Response(status=400, text=result["error"])

    event_type = result.get("event")
    user_id = result.get("user_id", 0)
    bot_app = request.app["bot"]
    logger = logging.getLogger(__name__)

    if event_type == "succeeded" and user_id:
        tier = result["tier"]
        posts = result["posts"]
        bm = scoped_balance_manager(user_id)
        bm.add_posts(posts, STRIPE_PRICES[tier]["price_id"])
        try:
            await bot_app.send_message(
                user_id,
                f"✅ <b>Оплата подтверждена!</b>\n\n"
                f"Добавлено: <b>{posts} постов</b>\n"
                f"Текущий баланс: <b>{bm.get_balance()} постов</b>\n\n"
                "Возвращайтесь в бот и запускайте рассылку! 🚀",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to notify user {user_id}: {e}")

    elif event_type == "payment_failed" and user_id:
        error_msg = result.get("error", "Неизвестная ошибка")
        try:
            await bot_app.send_message(
                user_id,
                f"❌ <b>Платеж не прошел</b>\n\n"
                f"Причина: {error_msg}\n\n"
                "Попробуйте другую карту или обратитесь в поддержку.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to notify user {user_id}: {e}")

    elif event_type == "payment_canceled" and user_id:
        try:
            await bot_app.send_message(
                user_id,
                "🚫 <b>Платеж отменен</b>\n\n"
                "Ваш баланс не изменился. Попробуйте ещё раз.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to notify user {user_id}: {e}")

    elif event_type == "dispute_created":
        for owner_id in OWNER_IDS:
            try:
                await bot_app.send_message(
                    owner_id,
                    f"⚠️ <b>CHARGEBACK ALERT!</b>\n\nCharge ID: {result.get('charge_id')}",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    return web.Response(status=200, text="ok")


async def stripe_success_handler(request: web.Request) -> web.Response:
    """Success page after Stripe payment."""
    return web.Response(text="✅ Оплата успешна! Вернитесь в Telegram бот.", content_type="text/plain; charset=utf-8")


async def stripe_cancel_handler(request: web.Request) -> web.Response:
    """Cancel page if user cancels payment."""
    return web.Response(text="❌ Платеж отменен. Вернитесь в Telegram бот.", content_type="text/plain; charset=utf-8")


# ─── Запуск ────────────────────────────────────────────────────────────────────

async def main():
    global scheduler_task, current_monitor_task, monitor_stop_event
    print("🤖 Бот запущен и готов к работе!")
    print("💬 Напиши /start в Telegram")
    print(f"📨 Канал по умолчанию: {DEFAULT_RESULTS_CHANNEL}")
    try:
        print(f"State file: {state_file('broadcast_state.json')}")
        print(f"User data dir: {user_data_dir()}")
        print(f"Session path: {SESSION_PATH}")
    except Exception:
        pass

    # Setup aiohttp web app for Stripe webhooks
    web_app = web.Application()
    web_app["bot"] = bot
    web_app.router.add_post("/stripe-webhook", stripe_webhook_handler)
    web_app.router.add_get("/stripe-success", stripe_success_handler)
    web_app.router.add_get("/stripe-cancel", stripe_cancel_handler)

    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Webhook server running on port {port}")

    scheduler_task = asyncio.create_task(scheduler_loop())
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        if current_monitor_task and not current_monitor_task.done():
            if monitor_stop_event:
                monitor_stop_event.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(current_monitor_task, timeout=10)
        current_monitor_task = None
        monitor_stop_event = None
        if scheduler_task:
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task


if __name__ == "__main__":
    asyncio.run(main())
