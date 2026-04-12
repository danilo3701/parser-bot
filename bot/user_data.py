import json
import os
import re
from pathlib import Path


def _user_data_dir() -> Path:
    raw = (os.getenv("USER_DATA_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path(__file__).parent / "user_data").resolve()


def user_dir(user_id: int) -> Path:
    return _user_data_dir() / str(int(user_id))


def user_broadcast_state_path(user_id: int) -> Path:
    return user_dir(user_id) / "broadcast_state.json"


def user_broadcast_groups_path(user_id: int) -> Path:
    return user_dir(user_id) / "broadcast_groups.json"


def list_user_ids_from_disk() -> list[int]:
    d = _user_data_dir()
    if not d.exists():
        return []
    out: list[int] = []
    for p in d.iterdir():
        if p.is_dir() and p.name.isdigit():
            try:
                out.append(int(p.name))
            except Exception:
                pass
    return sorted(set(out))


_TG_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def normalize_group_ref(raw: str) -> str | None:
    """
    Accepts:
      - @username
      - t.me/username (any scheme)
      - username
      - numeric chat id (-100...)
    Returns normalized reference string:
      - username without '@' for public chats
      - numeric string for ids
    """
    s = (raw or "").strip()
    if not s:
        return None

    # t.me/<username>
    s = re.sub(r"^\s*(https?://)?t\.me/", "", s, flags=re.IGNORECASE).strip()

    if s.startswith("@"):
        s = s[1:].strip()

    # numeric chat id
    if re.fullmatch(r"-?\d{5,}", s):
        return s

    if _TG_USERNAME_RE.match(s):
        return s

    return None


def format_group_ref(ref: str) -> str:
    s = str(ref or "").strip()
    if not s:
        return ""
    if re.fullmatch(r"-?\d{5,}", s):
        return f"id:{s}"
    if s.startswith("@"):
        return s
    return f"@{s}"


def load_user_broadcast_groups(user_id: int) -> list[str]:
    path = user_broadcast_groups_path(user_id)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_user_broadcast_groups(user_id: int, groups: list[str]) -> None:
    path = user_broadcast_groups_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(list(groups or []), f, ensure_ascii=False, indent=2)


def add_user_broadcast_group(user_id: int, ref: str) -> bool:
    ref = (ref or "").strip()
    if not ref:
        return False
    groups = load_user_broadcast_groups(user_id)
    if ref in groups:
        return False
    groups.insert(0, ref)
    save_user_broadcast_groups(user_id, groups)
    return True


def delete_user_broadcast_group(user_id: int, ref: str) -> bool:
    ref = (ref or "").strip()
    if not ref:
        return False
    groups = load_user_broadcast_groups(user_id)
    if ref not in groups:
        return False
    groups = [g for g in groups if g != ref]
    save_user_broadcast_groups(user_id, groups)
    return True

