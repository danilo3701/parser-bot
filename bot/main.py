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
from pathlib import Path
from datetime import datetime, time
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

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
from broadcast_sender import send_broadcast_campaign

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_RESULTS_CHANNEL = int(os.getenv("DEFAULT_RESULTS_CHANNEL") or os.getenv("RESULTS_CHANNEL", "-1003761773885"))
SOURCE_HEADER_CHANNEL_ID = int(os.getenv("SOURCE_HEADER_CHANNEL_ID", "-1003739349502"))
BROADCAST_TZ = os.getenv("BROADCAST_TZ", "Europe/Madrid")
BROADCAST_TIMES = ["08:12", "11:33", "17:40", "22:30"]
BROADCAST_TIME_OPTIONS = ["07:00", "08:12", "09:00", "11:33", "12:00", "15:00", "17:40", "18:00", "21:00", "22:30"]
OWNER_IDS_ENV = os.getenv("OWNER_IDS") or os.getenv("OWNER_ID", "")
OWNER_IDS = {
    int(item.strip())
    for item in OWNER_IDS_ENV.split(",")
    if item.strip().isdigit()
}
TG_ACCOUNTS_RAW = os.getenv("TG_ACCOUNTS", "")
def parse_broadcast_accounts(raw: str) -> dict[str, dict]:
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

BROADCAST_ACCOUNTS = parse_broadcast_accounts(TG_ACCOUNTS_RAW)
SESSION_PATH = Path(os.getenv("TG_SESSION_PATH", Path(__file__).parent.parent / "parser" / "tutor_bot_scan.session")).resolve()

# ─── Инициализация ───────────────────────────────────────────────────────────

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
broadcast_manager = BroadcastManager(
    Path(__file__).parent / "broadcast_state.json",
    default_tz=BROADCAST_TZ,
    default_times=BROADCAST_TIMES,
)
broadcast_lock = asyncio.Lock()
scheduler_task: asyncio.Task | None = None
current_scan_task: asyncio.Task | None = None
current_monitor_task: asyncio.Task | None = None
monitor_stop_event: asyncio.Event | None = None


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
    setting_broadcast_source = State()


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
            InlineKeyboardButton(
                text="🟢 Рассылка ВКЛ" if schedule_enabled else "🔴 Рассылка ВЫКЛ",
                callback_data="main_bc_toggle",
            ),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"),
        ],
        [
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


def broadcast_summary_text(state: dict) -> str:
    campaign = state.get("campaign", {})
    schedule = state.get("broadcast_schedule", {})
    send_mode = campaign.get("send_mode", "user")
    account = campaign.get("send_account") or ""
    source_channel = campaign.get("source_channel") or "не задан"
    source_message_id = campaign.get("source_message_id")
    source_value = f"{source_channel} #{source_message_id}" if source_message_id else source_channel
    selected_groups = campaign.get("selected_groups", [])
    groups_state = state.get("broadcast_groups_state", {})
    blocked_count = sum(1 for item in groups_state.values() if item.get("status") == "blocked")
    test_status = "✅ пройден" if campaign.get("test_passed") else "❌ не пройден"
    schedule_status = "включено" if schedule.get("enabled", True) else "выключено"
    tz = schedule.get("tz", BROADCAST_TZ)
    times = ", ".join(schedule.get("times", BROADCAST_TIMES))

    if send_mode == "user":
        mode_label = "🧑 От пользователя"
    else:
        channel = campaign.get("send_as_channel", "не выбран")
        mode_label = f"📢 От канала: {channel}"

    if BROADCAST_ACCOUNTS:
        account_label = account if account else "не выбран"
    else:
        account_label = "один аккаунт (env)"

    return (
        "📣 <b>Рассылка</b>\n\n"
        f"Аккаунт: <b>{account_label}</b>\n"
        f"Режим: <b>{mode_label}</b>\n"
        f"Источник поста: <b>{source_value}</b>\n"
        f"Выбрано групп: <b>{len(selected_groups)}</b>\n"
        f"Недоступных групп: <b>{blocked_count}</b>\n"
        f"Тест: <b>{test_status}</b>\n\n"
        f"Расписание: <b>{schedule_status}</b>\n"
        f"TZ: <b>{tz}</b>\n"
        f"Слоты: <b>{times}</b>"
    )


def broadcast_main_keyboard(state: dict) -> InlineKeyboardMarkup:
    campaign = state.get("campaign", {})
    send_mode = campaign.get("send_mode", "user")
    enabled = state.get("broadcast_schedule", {}).get("enabled", True)
    selected_account = campaign.get("send_account", "") or "не выбран"

    rows = []
    if BROADCAST_ACCOUNTS:
        rows.append([InlineKeyboardButton(text=f"👤 Аккаунт: {selected_account}", callback_data="bc_accounts")])
    mode_text = "🧑 Режим: от пользователя" if send_mode == "user" else "📢 Режим: от канала"
    rows.append([InlineKeyboardButton(text=mode_text, callback_data="bc_mode_toggle")])

    if send_mode == "channel":
        rows.append([InlineKeyboardButton(text="📢 Каналы send-as", callback_data="bc_channels")])

    rows.append([InlineKeyboardButton(text="📝 Источник поста", callback_data="bc_source")])
    rows.append([InlineKeyboardButton(text="👥 Группы рассылки", callback_data="bc_groups")])
    rows.append([
        InlineKeyboardButton(text="🧪 Тест", callback_data="bc_test"),
        InlineKeyboardButton(text="✅ Массовая", callback_data="bc_mass"),
    ])
    rows.append([InlineKeyboardButton(text="⏰ Время рассылки", callback_data="bc_times")])
    rows.append([InlineKeyboardButton(
        text="⏰ Расписание: ON" if enabled else "⏰ Расписание: OFF",
        callback_data="bc_schedule_toggle",
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


def broadcast_accounts_keyboard(state: dict) -> InlineKeyboardMarkup:
    selected = state.get("campaign", {}).get("send_account", "")
    if not BROADCAST_ACCOUNTS:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Аккаунты не заданы в TG_ACCOUNTS", callback_data="broadcast")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="broadcast")],
        ])
    buttons = []
    for alias in sorted(BROADCAST_ACCOUNTS.keys()):
        mark = "✅" if alias == selected else "▫️"
        buttons.append([InlineKeyboardButton(text=f"{mark} {alias}", callback_data=f"bc_acc_{alias}")])
    buttons.append([InlineKeyboardButton(text="🚫 Сбросить выбор", callback_data="bc_acc_clear")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="broadcast")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def broadcast_times_keyboard(state: dict) -> InlineKeyboardMarkup:
    """Клавиатура для выбора времени рассылки"""
    current_times = set(state.get("broadcast_schedule", {}).get("times", BROADCAST_TIMES))
    buttons = []
    row = []
    for t in BROADCAST_TIME_OPTIONS:
        mark = "✅" if t in current_times else "▫️"
        row.append(InlineKeyboardButton(text=f"{mark} {t}", callback_data=f"bct_{t}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="broadcast")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


BROADCAST_GROUPS_PER_PAGE = 6


def broadcast_groups_keyboard(state: dict, page: int = 0) -> InlineKeyboardMarkup:
    groups = load_groups()
    campaign = state.get("campaign", {})
    selected = set(campaign.get("selected_groups", []))
    groups_state = state.get("broadcast_groups_state", {})

    total = len(groups)
    total_pages = max(1, (total + BROADCAST_GROUPS_PER_PAGE - 1) // BROADCAST_GROUPS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start = page * BROADCAST_GROUPS_PER_PAGE
    page_groups = groups[start:start + BROADCAST_GROUPS_PER_PAGE]

    buttons = []
    for group in page_groups:
        group_meta = groups_state.get(group, {})
        blocked = group_meta.get("status") == "blocked"
        if blocked:
            text = f"🚫 @{group}"
        else:
            text = f"{'✅' if group in selected else '▫️'} @{group}"
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"bcg_{group}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"bcgp_{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"bcgp_{page + 1}"))
    buttons.append(nav)
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


def get_active_selected_groups(state: dict) -> list[str]:
    selected = set(state.get("campaign", {}).get("selected_groups", []))
    groups_state = state.get("broadcast_groups_state", {})
    groups = load_groups()
    return [
        group
        for group in groups
        if group in selected and groups_state.get(group, {}).get("status") != "blocked"
    ]


async def execute_broadcast(groups: list[str]) -> dict:
    state = broadcast_manager.load()
    campaign = state.get("campaign", {})
    source_channel = campaign.get("source_channel", "")
    source_message_id = campaign.get("source_message_id")
    send_mode = campaign.get("send_mode", "user")
    send_as = campaign.get("send_as_channel", "") if send_mode == "channel" else None
    return await send_broadcast_campaign(
        groups=groups,
        source_channel=source_channel,
        source_message_id=int(source_message_id),
        send_as_channel=send_as,
        account_alias=campaign.get("send_account") or None,
        delay_seconds=5.0,
        jitter_seconds=1.0,
    )


def is_campaign_ready(state: dict) -> tuple[bool, str]:
    campaign = state.get("campaign", {})
    if BROADCAST_ACCOUNTS:
        acc = campaign.get("send_account", "")
        if not acc or acc not in BROADCAST_ACCOUNTS:
            return False, "Не выбран аккаунт отправки (TG_ACCOUNTS)."
    if not campaign.get("source_channel") or not campaign.get("source_message_id"):
        return False, "Не задан источник поста."
    if campaign.get("send_mode") == "channel" and not campaign.get("send_as_channel"):
        return False, "Режим 'от канала': не выбран канал send-as."
    if not get_active_selected_groups(state):
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


async def scheduler_loop():
    tz_name = BROADCAST_TZ
    while True:
        try:
            state = broadcast_manager.load()
            schedule = state.get("broadcast_schedule", {})
            if not schedule.get("enabled", True):
                await asyncio.sleep(20)
                continue

            tz_name = schedule.get("tz", BROADCAST_TZ)
            now_local = datetime.now(ZoneInfo(tz_name))
            date_str = now_local.strftime("%Y-%m-%d")

            for slot in schedule.get("times", BROADCAST_TIMES):
                try:
                    hh, mm = slot.split(":")
                    slot_time = time(hour=int(hh), minute=int(mm))
                except Exception:
                    continue
                if now_local.time() < slot_time:
                    continue
                if broadcast_manager.was_slot_run(date_str, slot):
                    continue
                if broadcast_lock.locked():
                    continue

                ready, reason = is_campaign_ready(state)
                if not ready:
                    broadcast_manager.mark_slot_run(date_str, slot, "skipped", reason)
                    await notify_owner(f"📣 Слот {slot} пропущен: {reason}")
                    continue

                groups = get_active_selected_groups(state)
                async with broadcast_lock:
                    result = await execute_broadcast(groups)
                for group, err in result.get("blocked_groups", {}).items():
                    broadcast_manager.set_group_blocked(group, err)

                status = "ok" if result.get("ok") else "failed"
                broadcast_manager.mark_slot_run(date_str, slot, status, result.get("summary", ""))
                await notify_owner(f"📣 Авторассылка {slot}: {result.get('summary', '')}")

            await asyncio.sleep(20)
        except Exception:
            await asyncio.sleep(20)


# ─── Основные хендлеры ────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.set_state(MainMenu.viewing)
    bm_state = broadcast_manager.load()
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
    bm_state = broadcast_manager.load()
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
    if not await ensure_owner_callback(query):
        return
    bm_state = broadcast_manager.load()
    schedule_enabled = bm_state.get("broadcast_schedule", {}).get("enabled", True)
    new_enabled = not schedule_enabled
    broadcast_manager.set_schedule_enabled(new_enabled)
    cat_state = load()
    active_dir = get_active_direction(cat_state)
    active_name = ""
    if active_dir:
        active_name = get_directions(cat_state).get(active_dir, {}).get("name", "")
    await query.message.edit_text(
        main_menu_text(new_enabled, active_name),
        parse_mode="HTML",
        reply_markup=main_keyboard(new_enabled),
    )
    await query.answer()


# ─── Рассылка ─────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "broadcast")
async def broadcast_menu(query: CallbackQuery):
    state = broadcast_manager.ensure_groups_known(load_groups())
    await query.message.edit_text(
        broadcast_summary_text(state),
        parse_mode="HTML",
        reply_markup=broadcast_main_keyboard(state),
    )
    await query.answer()


@dp.callback_query(F.data == "bc_accounts")
async def broadcast_accounts(query: CallbackQuery):
    state = broadcast_manager.load()
    if not BROADCAST_ACCOUNTS:
        await query.answer("TG_ACCOUNTS не задано.", show_alert=True)
        return
    await query.message.edit_text(
        "👤 <b>Аккаунты отправки</b>\n\n"
        "Список берётся из переменной <code>TG_ACCOUNTS</code> в .env. "
        "Выберите аккаунт, от имени которого идти рассылка.",
        parse_mode="HTML",
        reply_markup=broadcast_accounts_keyboard(state),
    )
    await query.answer()


@dp.callback_query(F.data == "bc_channels")
async def broadcast_channels(query: CallbackQuery):
    state = broadcast_manager.load()
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
    if not is_owner(message.from_user.id):
        await message.answer("⛔️ Доступ только для владельца.")
        return
    value = (message.text or "").strip()
    if not re.match(r"^@[A-Za-z0-9_]{5,32}$", value):
        await message.answer("❌ Неверный формат. Используйте @channel_username")
        return
    current = broadcast_manager.load().get("campaign", {}).get("send_as_channel")
    broadcast_manager.add_send_as_channel(value)
    if not current:
        broadcast_manager.set_send_as_channel(value)
    await state.set_state(MainMenu.viewing)
    state_data = broadcast_manager.load()
    await message.answer(
        "✅ Канал добавлен.",
        reply_markup=broadcast_channels_keyboard(state_data),
    )


@dp.callback_query(F.data.startswith("bc_set_"))
async def broadcast_set_channel(query: CallbackQuery):
    channel = query.data[len("bc_set_"):]
    state = broadcast_manager.load()
    if channel not in state.get("send_as_channels", []):
        await query.answer("Канал не найден.", show_alert=True)
        return
    state = broadcast_manager.set_send_as_channel(channel)
    await query.message.edit_text(
        "📢 <b>Каналы send-as</b>\n\nАктивный канал обновлён.",
        parse_mode="HTML",
        reply_markup=broadcast_channels_keyboard(state),
    )
    await query.answer("Выбрано")


@dp.callback_query(F.data == "bc_del_selected_channel")
async def broadcast_delete_selected_channel(query: CallbackQuery):
    state = broadcast_manager.load()
    selected = state.get("campaign", {}).get("send_as_channel")
    if not selected:
        await query.answer("Сначала выберите канал.", show_alert=True)
        return
    state = broadcast_manager.remove_send_as_channel(selected)
    await query.message.edit_text(
        "📢 <b>Каналы send-as</b>\n\nВыбранный канал удалён.",
        parse_mode="HTML",
        reply_markup=broadcast_channels_keyboard(state),
    )
    await query.answer()


@dp.callback_query(F.data == "bc_acc_clear")
async def broadcast_account_clear(query: CallbackQuery):
    state = broadcast_manager.set_send_account("")
    await query.message.edit_text(
        "👤 <b>Аккаунты отправки</b>\n\nВыбор сброшен.",
        parse_mode="HTML",
        reply_markup=broadcast_accounts_keyboard(state),
    )
    await query.answer("Сброшено")


@dp.callback_query(F.data.startswith("bc_acc_"))
async def broadcast_account_set(query: CallbackQuery):
    alias = query.data[len("bc_acc_"):]
    if alias not in BROADCAST_ACCOUNTS:
        await query.answer("Аккаунт не найден в TG_ACCOUNTS.", show_alert=True)
        return
    state = broadcast_manager.set_send_account(alias)
    await query.message.edit_text(
        "👤 <b>Аккаунты отправки</b>\n\nАктивный аккаунт обновлён.",
        parse_mode="HTML",
        reply_markup=broadcast_accounts_keyboard(state),
    )
    await query.answer("Выбрано")


@dp.callback_query(F.data == "bc_source")
async def broadcast_source_prompt(query: CallbackQuery, state: FSMContext):
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
        reply_markup=broadcast_main_keyboard(current),
    )


@dp.callback_query(F.data == "bc_groups")
async def broadcast_groups(query: CallbackQuery):
    state = broadcast_manager.ensure_groups_known(load_groups())
    await query.message.edit_text(
        "👥 <b>Выбор групп для рассылки</b>\n\n"
        "✅ выбранные, ▫️ невыбранные, 🚫 недоступные.\n"
        "Нажмите на группу, чтобы переключить.",
        parse_mode="HTML",
        reply_markup=broadcast_groups_keyboard(state, page=0),
    )
    await query.answer()


@dp.callback_query(F.data.startswith("bcgp_"))
async def broadcast_groups_page(query: CallbackQuery):
    page = int(query.data.split("_")[-1])
    state = broadcast_manager.ensure_groups_known(load_groups())
    await query.message.edit_text(
        "👥 <b>Выбор групп для рассылки</b>\n\n"
        "✅ выбранные, ▫️ невыбранные, 🚫 недоступные.\n"
        "Нажмите на группу, чтобы переключить.",
        parse_mode="HTML",
        reply_markup=broadcast_groups_keyboard(state, page=page),
    )
    await query.answer()


@dp.callback_query(F.data.startswith("bcg_"))
async def broadcast_group_toggle(query: CallbackQuery):
    group = query.data[len("bcg_"):]
    state = broadcast_manager.ensure_groups_known(load_groups())
    group_meta = state.get("broadcast_groups_state", {}).get(group, {})
    if group_meta.get("status") == "blocked":
        state = broadcast_manager.set_group_active(group)
        await query.answer("Группа разблокирована")
    else:
        state = broadcast_manager.toggle_group_selected(group)
        await query.answer()
    await query.message.edit_reply_markup(reply_markup=broadcast_groups_keyboard(state, page=0))


@dp.callback_query(F.data == "bc_schedule_toggle")
async def broadcast_schedule_toggle(query: CallbackQuery):
    state = broadcast_manager.load()
    enabled = state.get("broadcast_schedule", {}).get("enabled", True)
    state = broadcast_manager.set_schedule_enabled(not enabled)
    await query.message.edit_text(
        broadcast_summary_text(state),
        parse_mode="HTML",
        reply_markup=broadcast_main_keyboard(state),
    )
    await query.answer("Обновлено")


@dp.callback_query(F.data == "bc_times")
async def broadcast_times_menu(query: CallbackQuery):
    state = broadcast_manager.load()
    current = ", ".join(sorted(state.get("broadcast_schedule", {}).get("times", BROADCAST_TIMES)))
    await query.message.edit_text(
        f"⏰ <b>Время рассылки</b>\n\nАктивные слоты: <b>{current}</b>\n\nНажмите на время, чтобы включить или выключить слот.",
        parse_mode="HTML",
        reply_markup=broadcast_times_keyboard(state),
    )
    await query.answer()


@dp.callback_query(F.data.startswith("bct_"))
async def broadcast_time_toggle(query: CallbackQuery):
    if not await ensure_owner_callback(query):
        return
    slot = query.data[len("bct_"):]
    state = broadcast_manager.load()
    times = set(state.get("broadcast_schedule", {}).get("times", BROADCAST_TIMES))
    if slot in times:
        times.discard(slot)
    else:
        times.add(slot)
    # Защита: нельзя убрать все слоты
    if not times:
        await query.answer("❌ Нельзя убрать все слоты!", show_alert=True)
        return
    broadcast_manager.set_schedule_times(sorted(times))
    state = broadcast_manager.load()
    current = ", ".join(sorted(state.get("broadcast_schedule", {}).get("times", [])))
    await query.message.edit_text(
        f"⏰ <b>Время рассылки</b>\n\nАктивные слоты: <b>{current}</b>\n\nНажмите на время, чтобы включить или выключить слот.",
        parse_mode="HTML",
        reply_markup=broadcast_times_keyboard(state),
    )
    await query.answer()


@dp.callback_query(F.data == "bc_test")
async def broadcast_test(query: CallbackQuery):
    if broadcast_lock.locked():
        await query.answer("Рассылка уже выполняется.", show_alert=True)
        return
    state = broadcast_manager.ensure_groups_known(load_groups())
    ready, reason = is_campaign_ready(state)
    if not ready:
        await query.answer(reason, show_alert=True)
        return
    test_groups = get_active_selected_groups(state)

    await query.message.edit_text(
        f"🧪 <b>Тест-рассылка</b>\n\nГрупп: <code>{len(test_groups)}</code>\nВыполняю отправку...",
        parse_mode="HTML",
    )
    async with broadcast_lock:
        result = await execute_broadcast(test_groups)
    for group, err in result.get("blocked_groups", {}).items():
        broadcast_manager.set_group_blocked(group, err)

    if result.get("sent_count", 0) > 0:
        broadcast_manager.mark_test_passed()
    else:
        broadcast_manager.reset_test_flag()
    state = broadcast_manager.load()
    await query.message.edit_text(
        f"🧪 <b>Тест завершён</b>\n\n{result.get('summary', '')}",
        parse_mode="HTML",
        reply_markup=broadcast_main_keyboard(state),
    )
    await query.answer()


@dp.callback_query(F.data == "bc_mode_toggle")
async def broadcast_mode_toggle(query: CallbackQuery):
    state_data = broadcast_manager.load()
    current = state_data.get("campaign", {}).get("send_mode", "user")
    new_mode = "channel" if current == "user" else "user"
    state_data = broadcast_manager.set_send_mode(new_mode)
    await query.message.edit_text(
        broadcast_summary_text(state_data),
        parse_mode="HTML",
        reply_markup=broadcast_main_keyboard(state_data),
    )
    await query.answer()


@dp.callback_query(F.data == "bc_mass")
async def broadcast_mass(query: CallbackQuery):
    if broadcast_lock.locked():
        await query.answer("Рассылка уже выполняется.", show_alert=True)
        return
    state = broadcast_manager.ensure_groups_known(load_groups())
    ready, reason = is_campaign_ready(state)
    if not ready:
        await query.answer(reason, show_alert=True)
        return
    groups = get_active_selected_groups(state)
    await query.message.edit_text(
        f"📣 <b>Массовая рассылка</b>\n\nГрупп к отправке: <b>{len(groups)}</b>\nВыполняю...",
        parse_mode="HTML",
    )
    async with broadcast_lock:
        result = await execute_broadcast(groups)
    for group, err in result.get("blocked_groups", {}).items():
        broadcast_manager.set_group_blocked(group, err)

    state = broadcast_manager.load()
    await query.message.edit_text(
        f"📣 <b>Массовая рассылка завершена</b>\n\n{result.get('summary', '')}",
        parse_mode="HTML",
        reply_markup=broadcast_main_keyboard(state),
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


# ─── Запуск ────────────────────────────────────────────────────────────────────

async def main():
    global scheduler_task, current_monitor_task, monitor_stop_event
    print("🤖 Бот запущен и готов к работе!")
    print("💬 Напиши /start в Telegram")
    print(f"📨 Канал по умолчанию: {DEFAULT_RESULTS_CHANNEL}")
    scheduler_task = asyncio.create_task(scheduler_loop())
    try:
        await dp.start_polling(bot)
    finally:
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
