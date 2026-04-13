import json
from pathlib import Path

from storage_paths import state_file

ANTI_KEYWORDS_FILE = state_file("anti_keywords.json")


def load_anti_keywords() -> list:
    """Загрузить список стоп-слов из JSON"""
    if not ANTI_KEYWORDS_FILE.exists():
        return []
    with open(ANTI_KEYWORDS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_anti_keywords(words: list):
    """Сохранить список стоп-слов в JSON"""
    with open(ANTI_KEYWORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False, indent=2)


def add_anti_keyword(word: str):
    """Добавить стоп-слово (если его ещё нет)"""
    words = load_anti_keywords()
    word_lower = word.lower().strip()
    if word_lower and word_lower not in [w.lower() for w in words]:
        words.append(word_lower)
        save_anti_keywords(words)


def remove_anti_keyword(word: str):
    """Удалить стоп-слово"""
    words = load_anti_keywords()
    words_lower = [w.lower() for w in words]
    word_lower = word.lower().strip()
    if word_lower in words_lower:
        idx = words_lower.index(word_lower)
        words.pop(idx)
        save_anti_keywords(words)


def get_anti_keywords() -> list:
    """Получить список стоп-слов"""
    return load_anti_keywords()
