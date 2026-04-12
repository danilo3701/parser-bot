import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def accounts_path() -> Path:
    raw = (os.getenv("MT_ACCOUNTS_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path(__file__).parent / "mtproto_accounts.json").resolve()


def code_ttl_seconds() -> int:
    try:
        v = int(os.getenv("MT_CODE_TTL_SECONDS") or "120")
    except Exception:
        v = 120
    return max(30, min(v, 600))


def mask_phone(phone: str) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) <= 4:
        return phone
    return f"+***{digits[-4:]}"


def load_accounts() -> dict[str, dict]:
    path = accounts_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def list_connected_user_ids() -> list[int]:
    """
    Returns user ids that have an entry in MTProto accounts store.
    """
    out: list[int] = []
    for k in load_accounts().keys():
        try:
            out.append(int(k))
        except Exception:
            pass
    return sorted(set(out))


def save_accounts(data: dict[str, dict]) -> None:
    path = accounts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def set_connected_account(
    *,
    user_id: int,
    phone: str,
    api_id: int,
    api_hash: str,
    me_id: int | None,
    username: str | None,
    first_name: str | None,
    session_string: str = "",
) -> None:
    data = load_accounts()
    data[str(user_id)] = {
        "status": "connected",
        "phone_mask": mask_phone(phone),
        "api_id": int(api_id),
        "api_hash": api_hash or "",
        "session_string": session_string or "",
        "me_id": int(me_id) if me_id is not None else None,
        "username": username or "",
        "first_name": first_name or "",
        "connected_at": _now_utc().isoformat(),
    }
    save_accounts(data)


def disconnect_account(user_id: int) -> bool:
    data = load_accounts()
    existed = str(user_id) in data
    if existed:
        data.pop(str(user_id), None)
        save_accounts(data)
    # Session file cleanup is handled by the caller to avoid accidental deletion
    return existed


def get_account(user_id: int) -> dict | None:
    return load_accounts().get(str(user_id))


def get_session_string(user_id: int) -> str:
    meta = get_account(user_id)
    return (meta or {}).get("session_string", "") if meta else ""


def make_client_from_string_session(api_id: int, api_hash: str, session_string: str = "") -> object:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(session_string or ""), api_id, api_hash)


def extract_session_string(client: object) -> str:
    from telethon.sessions import StringSession
    return StringSession.save(client.session)


@dataclass
class PendingLogin:
    method: str  # phone|qr
    created_at: datetime
    expires_at: datetime
    api_id: int
    api_hash: str
    phone: str
    client: object  # TelegramClient
    qr_login: object | None = None
    bg_task: object | None = None  # asyncio.Task for QR auto-watch


def new_pending_login(*, method: str, api_id: int, api_hash: str, phone: str, client: object) -> PendingLogin:
    now = _now_utc()
    ttl = timedelta(seconds=code_ttl_seconds())
    return PendingLogin(
        method=method,
        created_at=now,
        expires_at=now + ttl,
        api_id=api_id,
        api_hash=api_hash,
        phone=phone,
        client=client,
        qr_login=None,
    )
