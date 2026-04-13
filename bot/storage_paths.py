import os
from pathlib import Path


def base_data_dir() -> Path:
    """
    Resolve a writable directory for persistent app state.

    On Railway, the filesystem inside the container is ephemeral across redeploys.
    A persistent volume is typically mounted at /data (see railway.toml).
    """
    raw = (os.getenv("BOT_DATA_DIR") or os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()

    # Autodetect Railway volume mount when present.
    p = Path("/data")
    if p.exists() and p.is_dir():
        return p

    # Local/dev fallback: keep state next to the bot package.
    return Path(__file__).parent


def state_file(filename: str) -> Path:
    return (base_data_dir() / filename).resolve()


def user_data_dir() -> Path:
    return (base_data_dir() / "user_data").resolve()

