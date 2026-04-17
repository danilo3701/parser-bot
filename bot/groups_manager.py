import json
from pathlib import Path

from storage_paths import state_file
from user_data import normalize_group_ref

GROUPS_FILE = state_file("groups.json")


def load_groups() -> list:
    if not GROUPS_FILE.exists():
        return []
    with open(GROUPS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_groups(groups: list):
    with open(GROUPS_FILE, "w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)


def add_group(username: str):
    username = normalize_group_ref(username) or ""
    if not username:
        return
    groups = load_groups()
    if username not in groups:
        groups.insert(0, username)
        save_groups(groups)


def delete_group(username: str):
    username = normalize_group_ref(username) or ""
    if not username:
        return
    groups = load_groups()
    if username in groups:
        groups.remove(username)
        save_groups(groups)


def get_groups() -> list:
    return load_groups()
