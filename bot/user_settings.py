import json
from pathlib import Path

from storage_paths import state_file
from user_data import user_dir, user_broadcast_state_path


DEFAULT_TZ = "Europe/Madrid"

# Fixed Top-20 timezones for UI (IANA ids).
TOP_TIMEZONES: list[str] = [
    "Europe/Madrid",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Paris",
    "Europe/Kyiv",
    "Europe/Moscow",
    "Asia/Almaty",
    "Asia/Jakarta",
    "Asia/Makassar",
    "Asia/Jayapura",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "UTC",
    "Asia/Dubai",
    "Asia/Singapore",
    "Asia/Bangkok",
    "Asia/Tokyo",
    "Australia/Sydney",
]


def settings_path(user_id: int) -> Path:
    return user_dir(int(user_id)) / "settings.json"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _read_broadcast_tz_if_any(user_id: int) -> str | None:
    # Regular user path.
    candidates = [user_broadcast_state_path(int(user_id))]
    # Owner/global path (best effort; keeps migration working for owner too).
    candidates.append(state_file("broadcast_state.json"))

    for p in candidates:
        data = _read_json(p)
        schedule = data.get("broadcast_schedule")
        if isinstance(schedule, dict):
            tz = (schedule.get("tz") or "").strip()
            if tz:
                return tz
    return None


def get_user_tz(user_id: int) -> str:
    """
    Global timezone preference for the user (IANA id).

    Lazy migration:
      - If settings.json has no tz, but broadcast_state.json has broadcast_schedule.tz, seed it.
      - Otherwise default to Europe/Madrid.
    """
    path = settings_path(int(user_id))
    data = _read_json(path)

    tz = (data.get("tz") or "").strip()
    if tz:
        return tz

    migrated = _read_broadcast_tz_if_any(int(user_id))
    if migrated:
        data["tz"] = migrated
        _write_json(path, data)
        return migrated

    data["tz"] = DEFAULT_TZ
    _write_json(path, data)
    return DEFAULT_TZ


def set_user_tz(user_id: int, tz: str) -> None:
    tz = (tz or "").strip()
    if tz not in TOP_TIMEZONES:
        raise ValueError("unsupported_timezone")
    path = settings_path(int(user_id))
    data = _read_json(path)
    data["tz"] = tz
    _write_json(path, data)

