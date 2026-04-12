import json
from datetime import datetime, timezone
from pathlib import Path
import uuid


WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class BroadcastManager:
    def __init__(self, path: Path, default_tz: str, default_times: list[str]):
        self.path = Path(path)
        self.default_tz = default_tz
        self.default_times = default_times

    def _default_state(self) -> dict:
        return {
            "send_as_channels": [],
            "broadcast_groups_state": {},
            "broadcast_schedule": {
                "enabled": True,
                "tz": self.default_tz,
            },
            "campaign": {
                "send_mode": "user",
                "send_account": "",
                "send_as_channel": "",
                # Back-compat: older flows used a single source post reference.
                "source_channel": "",
                "source_message_id": None,
                # New: pool of posts stored in a known channel for Telethon to re-send from.
                # Each item: {id, channel, message_id, kind, preview}
                "posts": [],
                "rotation_index": 0,
                "selected_groups": [],
                "test_passed": False,
                "last_test_at": None,
            },
            # New schedule model: weekly day -> {enabled, time}
            "weekly_schedule": {wd: {"enabled": False, "time": None} for wd in WEEKDAYS},
            "last_runs": {},
        }

    def load(self) -> dict:
        if not self.path.exists():
            state = self._default_state()
            self.save(state)
            return state

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = self._default_state()
            self.save(data)
            return data

        state = self._default_state()
        if isinstance(data, dict):
            state.update(data)
        state.setdefault("campaign", self._default_state()["campaign"])
        state.setdefault("broadcast_schedule", self._default_state()["broadcast_schedule"])
        state.setdefault("send_as_channels", [])
        state.setdefault("broadcast_groups_state", {})
        state.setdefault("last_runs", {})
        state["campaign"].setdefault("send_account", "")
        state.setdefault("weekly_schedule", self._default_state()["weekly_schedule"])

        # Back-compat migration: if old source post is set and posts pool is empty, seed it.
        campaign = state.get("campaign", {})
        posts = campaign.get("posts")
        if not isinstance(posts, list):
            posts = []
            campaign["posts"] = posts
        campaign.setdefault("rotation_index", 0)
        if not posts and campaign.get("source_channel") and campaign.get("source_message_id"):
            try:
                mid = int(campaign["source_message_id"])
            except Exception:
                mid = None
            if mid:
                posts.append({
                    "id": uuid.uuid4().hex[:8],
                    "channel": str(campaign["source_channel"]),
                    "message_id": mid,
                    "kind": "legacy",
                    "preview": f"{campaign['source_channel']} #{mid}",
                })
        return state

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def ensure_groups_known(self, groups: list[str]) -> dict:
        state = self.load()
        groups_state = state["broadcast_groups_state"]
        for group in groups:
            if group not in groups_state:
                groups_state[group] = {
                    "status": "active",
                    "reason": "",
                    "updated_at": None,
                    "last_test_status": None,
                    "last_test_reason": None,
                    "last_test_message_id": None,
                    "last_test_sent_at": None,
                    "last_test_verified_at": None,
                }
        state["campaign"]["selected_groups"] = [
            g for g in state["campaign"].get("selected_groups", []) if g in groups
        ]
        self.save(state)
        return state

    def add_send_as_channel(self, channel: str) -> dict:
        state = self.load()
        channels = state["send_as_channels"]
        if channel not in channels:
            channels.append(channel)
            channels.sort()
        self.save(state)
        return state

    def remove_send_as_channel(self, channel: str) -> dict:
        state = self.load()
        channels = state["send_as_channels"]
        if channel in channels:
            channels.remove(channel)
        if state["campaign"].get("send_as_channel") == channel:
            state["campaign"]["send_as_channel"] = ""
            self.reset_test_flag_in_state(state)
        self.save(state)
        return state

    def set_send_as_channel(self, channel: str) -> dict:
        state = self.load()
        state["campaign"]["send_as_channel"] = channel
        self.reset_test_flag_in_state(state)
        self.save(state)
        return state

    def set_send_account(self, account: str | None) -> dict:
        state = self.load()
        state["campaign"]["send_account"] = account or ""
        self.reset_test_flag_in_state(state)
        self.save(state)
        return state

    def set_source(self, source_channel: str, source_message_id: int) -> dict:
        state = self.load()
        state["campaign"]["source_channel"] = source_channel
        state["campaign"]["source_message_id"] = source_message_id
        self.reset_test_flag_in_state(state)
        self.save(state)
        return state

    # ─── Posts pool ─────────────────────────────────────────────────────────────

    def list_posts(self) -> list[dict]:
        state = self.load()
        posts = state.get("campaign", {}).get("posts", [])
        return posts if isinstance(posts, list) else []

    def add_post(
        self,
        *,
        channel: str,
        message_id: int,
        kind: str,
        preview: str,
        max_posts: int = 10,
    ) -> dict:
        state = self.load()
        campaign = state["campaign"]
        posts = campaign.setdefault("posts", [])
        if not isinstance(posts, list):
            posts = []
            campaign["posts"] = posts

        if len(posts) >= max_posts:
            return state

        posts.append({
            "id": uuid.uuid4().hex[:8],
            "channel": channel,
            "message_id": int(message_id),
            "kind": kind,
            "preview": (preview or "").strip()[:140],
        })
        self.reset_test_flag_in_state(state)
        self.save(state)
        return state

    def delete_post(self, post_id: str) -> dict:
        state = self.load()
        campaign = state["campaign"]
        posts = campaign.get("posts", [])
        if isinstance(posts, list):
            campaign["posts"] = [p for p in posts if str(p.get("id")) != post_id]
        # Clamp rotation index
        try:
            idx = int(campaign.get("rotation_index") or 0)
        except Exception:
            idx = 0
        n = len(campaign.get("posts", []))
        campaign["rotation_index"] = 0 if n <= 0 else min(idx, n - 1)
        self.reset_test_flag_in_state(state)
        self.save(state)
        return state

    def choose_next_post(self) -> dict | None:
        state = self.load()
        campaign = state.get("campaign", {})
        posts = campaign.get("posts", [])
        if not isinstance(posts, list) or not posts:
            return None
        try:
            idx = int(campaign.get("rotation_index") or 0)
        except Exception:
            idx = 0
        idx = max(0, min(idx, len(posts) - 1))
        return posts[idx]

    def advance_rotation_if_sent(self) -> dict:
        state = self.load()
        campaign = state.get("campaign", {})
        posts = campaign.get("posts", [])
        if not isinstance(posts, list) or not posts:
            campaign["rotation_index"] = 0
            self.save(state)
            return state
        try:
            idx = int(campaign.get("rotation_index") or 0)
        except Exception:
            idx = 0
        campaign["rotation_index"] = (idx + 1) % len(posts)
        self.save(state)
        return state

    # ─── Weekly schedule ────────────────────────────────────────────────────────

    def get_weekly_schedule(self) -> dict:
        state = self.load()
        sched = state.get("weekly_schedule")
        if not isinstance(sched, dict):
            sched = self._default_state()["weekly_schedule"]
            state["weekly_schedule"] = sched
            self.save(state)
        return sched

    def set_weekday_time(self, weekday: str, time_value: str | None) -> dict:
        state = self.load()
        sched = state.setdefault("weekly_schedule", self._default_state()["weekly_schedule"])
        if weekday not in WEEKDAYS:
            return state
        day = sched.setdefault(weekday, {"enabled": False, "time": None})
        day["time"] = time_value
        if time_value:
            day["enabled"] = True
        self.reset_test_flag_in_state(state)
        self.save(state)
        return state

    def set_weekday_enabled(self, weekday: str, enabled: bool) -> dict:
        state = self.load()
        sched = state.setdefault("weekly_schedule", self._default_state()["weekly_schedule"])
        if weekday not in WEEKDAYS:
            return state
        day = sched.setdefault(weekday, {"enabled": False, "time": None})
        day["enabled"] = bool(enabled)
        self.reset_test_flag_in_state(state)
        self.save(state)
        return state

    def copy_weekday(self, source_weekday: str, target_weekday: str) -> dict:
        state = self.load()
        sched = state.setdefault("weekly_schedule", self._default_state()["weekly_schedule"])
        if source_weekday not in WEEKDAYS or target_weekday not in WEEKDAYS:
            return state
        src = sched.get(source_weekday) or {"enabled": False, "time": None}
        sched[target_weekday] = {"enabled": bool(src.get("enabled")), "time": src.get("time")}
        self.reset_test_flag_in_state(state)
        self.save(state)
        return state

    def toggle_group_selected(self, group: str) -> dict:
        state = self.load()
        selected = set(state["campaign"].get("selected_groups", []))
        if group in selected:
            selected.remove(group)
        else:
            selected.add(group)
        state["campaign"]["selected_groups"] = sorted(selected)
        self.reset_test_flag_in_state(state)
        self.save(state)
        return state

    def set_group_blocked(self, group: str, reason: str) -> dict:
        state = self.load()
        group_state = state["broadcast_groups_state"].setdefault(
            group,
            {"status": "active", "reason": "", "updated_at": None},
        )
        group_state["status"] = "blocked"
        group_state["reason"] = reason
        group_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.save(state)
        return state

    def set_group_active(self, group: str) -> dict:
        state = self.load()
        group_state = state["broadcast_groups_state"].setdefault(
            group,
            {"status": "active", "reason": "", "updated_at": None},
        )
        group_state["status"] = "active"
        group_state["reason"] = ""
        group_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.reset_test_flag_in_state(state)
        self.save(state)
        return state

    def unselect_groups(self, groups: list[str]) -> dict:
        state = self.load()
        selected = set(state["campaign"].get("selected_groups", []))
        changed = False
        for g in groups or []:
            if g in selected:
                selected.remove(g)
                changed = True
        if changed:
            state["campaign"]["selected_groups"] = sorted(selected)
            self.reset_test_flag_in_state(state)
            self.save(state)
        return state

    def set_group_last_test(
        self,
        group: str,
        *,
        status: str,
        reason: str | None = None,
        message_id: int | None = None,
        sent_at: str | None = None,
        verified_at: str | None = None,
    ) -> dict:
        state = self.load()
        group_state = state["broadcast_groups_state"].setdefault(
            group,
            {
                "status": "active",
                "reason": "",
                "updated_at": None,
                "last_test_status": None,
                "last_test_reason": None,
                "last_test_message_id": None,
                "last_test_sent_at": None,
                "last_test_verified_at": None,
            },
        )
        group_state["last_test_status"] = status
        group_state["last_test_reason"] = reason or ""
        group_state["last_test_message_id"] = int(message_id) if message_id is not None else None
        group_state["last_test_sent_at"] = sent_at
        group_state["last_test_verified_at"] = verified_at
        group_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.save(state)
        return state

    def set_schedule_enabled(self, enabled: bool) -> dict:
        state = self.load()
        state["broadcast_schedule"]["enabled"] = enabled
        self.save(state)
        return state

    def set_schedule_times(self, times: list[str]) -> dict:
        state = self.load()
        state["broadcast_schedule"]["times"] = times
        self.save(state)
        return state

    def mark_test_passed(self) -> dict:
        state = self.load()
        state["campaign"]["test_passed"] = True
        state["campaign"]["last_test_at"] = datetime.now(timezone.utc).isoformat()
        self.save(state)
        return state

    def reset_test_flag_in_state(self, state: dict) -> None:
        state["campaign"]["test_passed"] = False
        state["campaign"]["last_test_at"] = None

    def reset_test_flag(self) -> dict:
        state = self.load()
        self.reset_test_flag_in_state(state)
        self.save(state)
        return state

    def was_slot_run(self, date_str: str, slot_time: str) -> bool:
        state = self.load()
        slot_key = f"{date_str}_{slot_time}"
        return slot_key in state.get("last_runs", {})

    def mark_slot_run(self, date_str: str, slot_time: str, status: str, summary: str) -> dict:
        state = self.load()
        slot_key = f"{date_str}_{slot_time}"
        state["last_runs"][slot_key] = {
            "status": status,
            "summary": summary,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.save(state)
        return state

    def set_send_mode(self, mode: str) -> dict:
        state = self.load()
        state["campaign"]["send_mode"] = mode
        self.reset_test_flag_in_state(state)
        self.save(state)
        return state
