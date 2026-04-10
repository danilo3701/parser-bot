import json
from datetime import datetime, timezone
from pathlib import Path


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
                "times": list(self.default_times),
                "tz": self.default_tz,
            },
            "campaign": {
                "send_mode": "user",
                "send_account": "",
                "send_as_channel": "",
                "source_channel": "",
                "source_message_id": None,
                "selected_groups": [],
                "test_passed": False,
                "last_test_at": None,
            },
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
