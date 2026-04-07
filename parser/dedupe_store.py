"""
Persistent deduplication store.

Stores dedupe keys in a local JSON file as:
{
  "key": <unix_timestamp_utc>,
  ...
}
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class DedupeStore:
    def __init__(self, path: Path | None, ttl_seconds: int | None = None):
        """
        path=None -> in-memory mode (no reads/writes to disk).
        """
        self.path = Path(path) if path is not None else None
        self.ttl_seconds = ttl_seconds
        self._data: dict[str, float] = {}
        if self.path is not None:
            self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            self._data = {}
            return

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            self._data = {}
            return

        if not isinstance(raw, dict):
            self._data = {}
            return

        parsed: dict[str, float] = {}
        for key, value in raw.items():
            if not isinstance(key, str):
                continue
            try:
                parsed[key] = float(value)
            except (TypeError, ValueError):
                continue
        self._data = parsed

    def _to_epoch(self, now_utc: datetime) -> float:
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        else:
            now_utc = now_utc.astimezone(timezone.utc)
        return now_utc.timestamp()

    def is_duplicate(self, key: str, now_utc: datetime) -> bool:
        if self.ttl_seconds is not None:
            self.cleanup_expired(now_utc)
        return key in self._data

    def mark_seen(self, key: str, now_utc: datetime) -> None:
        self._data[key] = self._to_epoch(now_utc)

    def cleanup_expired(self, now_utc: datetime) -> None:
        if self.ttl_seconds is None:
            return
        now_ts = self._to_epoch(now_utc)
        min_ts = now_ts - self.ttl_seconds
        self._data = {k: ts for k, ts in self._data.items() if ts >= min_ts}

    def flush(self) -> None:
        if self.path is None:
            return  # in-memory: no persistence

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=f"{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                json.dump(self._data, tmp_file, ensure_ascii=False, indent=2)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_path, self.path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
