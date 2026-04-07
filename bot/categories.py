"""
Модуль для управления иерархией категорий (направления → подкатегории).
Загружает/сохраняет категории из categories.json с автоматической миграцией из parser_keywords.json.
"""

import json
import os
from pathlib import Path

CATEGORIES_FILE = Path(__file__).parent / "categories.json"
PARSER_KEYWORDS_FILE = Path(__file__).parent.parent / "parser_keywords.json"


def _load_parser_keywords() -> dict:
    """Загружает parser_keywords.json для миграции"""
    try:
        with open(PARSER_KEYWORDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _is_old_format(data: dict) -> bool:
    """Проверяет, является ли структура старой версией (плоская категория)"""
    if not data:
        return True
    # Старый формат: {"tutors": {"name": "...", "keywords": [...], "active": true}}
    # Новый формат: {"active_direction": "...", "active_subcategories": [...], "directions": {...}}
    return "active_direction" not in data and "directions" not in data


def _migrate_from_parser_keywords() -> dict:
    """Мигрирует данные из parser_keywords.json в новый формат"""
    parser_kw = _load_parser_keywords()

    directions = {}

    for direction_id, subcats_data in parser_kw.items():
        # Конвертируем первый уровень в направление
        direction_name = direction_id.upper()
        subcategories = {}

        for subcat_id, kw_data in subcats_data.items():
            # Мержим RU и UK ключевые слова
            keywords = []
            if isinstance(kw_data, dict):
                keywords.extend(kw_data.get("ru", []))
                keywords.extend(kw_data.get("uk", []))

            subcategories[subcat_id] = {
                "name": subcat_id.upper(),  # Базовое название из ID
                "keywords": keywords,
            }

        directions[direction_id] = {
            "name": direction_name,
            "subcategories": subcategories,
        }

    return {
        "active_direction": None,
        "active_subcategories": [],
        "directions": directions,
    }


def load() -> dict:
    """Загружает категории из JSON файла с автоматической миграцией"""
    try:
        with open(CATEGORIES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Если старый формат, мигрируем
        if _is_old_format(data):
            data = _migrate_from_parser_keywords()
            save(data)

        return data
    except FileNotFoundError:
        # Если файла нет, создаём из parser_keywords.json
        data = _migrate_from_parser_keywords()
        save(data)
        return data


def save(state: dict) -> bool:
    """Сохраняет категории в JSON файл"""
    try:
        with open(CATEGORIES_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"Ошибка при сохранении категорий: {e}")
        return False


def get_directions(state: dict) -> dict:
    """Возвращает словарь всех направлений: {id: {name, subcategories}}"""
    return state.get("directions", {})


def get_subcategories(state: dict, dir_id: str) -> dict:
    """Возвращает словарь подкатегорий для направления: {id: {name, keywords}}"""
    directions = get_directions(state)
    direction = directions.get(dir_id, {})
    return direction.get("subcategories", {})


def get_active_direction(state: dict) -> str | None:
    """Возвращает текущий активный direction_id"""
    return state.get("active_direction")


def get_active_subcategory_ids(state: dict) -> list:
    """Возвращает список текущих активных подкатегорий"""
    return state.get("active_subcategories", [])


def set_active_selection(dir_id: str, subcat_ids: list) -> bool:
    """Сохраняет активный выбор направления и подкатегорий"""
    state = load()
    state["active_direction"] = dir_id
    state["active_subcategories"] = subcat_ids
    return save(state)


def get_active_keywords() -> list:
    """Возвращает смерженный список ключевых слов из активных подкатегорий"""
    state = load()
    dir_id = get_active_direction(state)
    subcat_ids = get_active_subcategory_ids(state)

    if not dir_id or not subcat_ids:
        return []

    subcategories = get_subcategories(state, dir_id)
    keywords = []

    for subcat_id in subcat_ids:
        subcat = subcategories.get(subcat_id, {})
        keywords.extend(subcat.get("keywords", []))

    return list(set(keywords))  # Удаляем дубликаты


def _parse_channel_id(value) -> int | None:
    """Convert value to integer channel id or return None."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        try:
            return int(v)
        except ValueError:
            return None
    return None


def resolve_results_channel_for_selection(state: dict, default_channel: int) -> tuple[int | None, str | None]:
    """Resolve results channel for active direction/subcategories.

    Returns:
      (channel_id, None) on success.
      (None, error_message) if selected subcategories map to different channels.
    """
    dir_id = get_active_direction(state)
    subcat_ids = get_active_subcategory_ids(state)
    if not dir_id or not subcat_ids:
        return default_channel, None

    subcategories = get_subcategories(state, dir_id)
    channels = set()

    for subcat_id in subcat_ids:
        subcat = subcategories.get(subcat_id, {})
        override = _parse_channel_id(subcat.get("target_channel_id"))
        channels.add(override if override is not None else default_channel)

    if len(channels) > 1:
        return None, "Выбраны подкатегории с разными каналами результатов."

    return next(iter(channels)) if channels else default_channel, None


def get_keywords_for_search() -> list:
    """Обёртка для совместимости со scanner.py и broadcast_sender.py"""
    return get_active_keywords()


def add_keywords_to_subcat(dir_id: str, subcat_id: str, new_keywords: list[str]) -> tuple[int, int]:
    """Добавить несколько ключевых слов в подкатегорию за один проход.

    Возвращает (added, skipped) — сколько реально добавлено и сколько пропущено (дубликаты/пустые).
    """
    state = load()
    directions = get_directions(state)

    if dir_id not in directions:
        return 0, len(new_keywords)

    direction = directions[dir_id]
    subcats = direction.get("subcategories", {})

    if subcat_id not in subcats:
        return 0, len(new_keywords)

    subcat = subcats[subcat_id]
    keywords = subcat.get("keywords", [])

    existing = set(keywords)
    added = 0
    skipped = 0

    for kw in new_keywords:
        if not kw or kw in existing:
            skipped += 1
            continue
        keywords.append(kw)
        existing.add(kw)
        added += 1

    if added:
        save(state)
    return added, skipped


def add_keyword_to_subcat(dir_id: str, subcat_id: str, keyword: str) -> bool:
    """Добавить одно ключевое слово в подкатегорию"""
    added, _ = add_keywords_to_subcat(dir_id, subcat_id, [keyword])
    return added > 0


def add_subcategory(dir_id: str, sub_id: str, name: str) -> bool:
    """Добавить новую подкатегорию в направление."""
    state = load()
    directions = get_directions(state)

    if dir_id not in directions:
        return False

    subcats = directions[dir_id].get("subcategories", {})

    if sub_id in subcats:
        return False

    subcats[sub_id] = {"name": name, "keywords": []}
    directions[dir_id]["subcategories"] = subcats
    return save(state)


def remove_keywords_from_subcat(dir_id: str, subcat_id: str, words: list[str]) -> tuple[int, int]:
    """Удалить несколько слов из подкатегории.

    Возвращает (removed, not_found).
    Сравнение без учёта регистра, пробелы по краям игнорируются.
    """
    state = load()
    directions = get_directions(state)

    if dir_id not in directions:
        return 0, len(words)

    subcats = directions[dir_id].get("subcategories", {})
    if subcat_id not in subcats:
        return 0, len(words)

    subcat = subcats[subcat_id]
    keywords = subcat.get("keywords", [])

    targets = {w.strip().casefold() for w in words if w and w.strip()}
    if not targets:
        return 0, len(words)

    removed = 0
    remaining = []
    for kw in keywords:
        if kw.strip().casefold() in targets:
            removed += 1
            continue
        remaining.append(kw)

    not_found = len(targets) - removed if len(targets) >= removed else 0

    if removed:
        subcat["keywords"] = remaining
        save(state)

    return removed, not_found


def remove_keyword_from_subcat(dir_id: str, subcat_id: str, keyword_index: int) -> bool:
    """Удалить ключевое слово из подкатегории по индексу"""
    state = load()
    directions = get_directions(state)

    if dir_id not in directions:
        return False

    direction = directions[dir_id]
    subcats = direction.get("subcategories", {})

    if subcat_id not in subcats:
        return False

    subcat = subcats[subcat_id]
    keywords = subcat.get("keywords", [])

    if keyword_index < 0 or keyword_index >= len(keywords):
        return False

    keywords.pop(keyword_index)
    return save(state)


# ===== Старые функции (оставляем для обратной совместимости, но не используем) =====
def get_active_category() -> tuple:
    """DEPRECATED: Используйте get_active_direction() + get_active_subcategory_ids()"""
    state = load()
    dir_id = get_active_direction(state)
    if not dir_id:
        dirs = get_directions(state)
        dir_id = list(dirs.keys())[0] if dirs else None
        set_active_selection(dir_id, [])
    return dir_id, {"keywords": get_active_keywords()}


def set_active_category(category_id: str) -> bool:
    """DEPRECATED: Используйте set_active_selection()"""
    return set_active_selection(category_id, [])
