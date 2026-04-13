import json
from datetime import datetime, timezone
from pathlib import Path

from storage_paths import state_file, user_data_dir


class BalanceManager:
    """Manages post balance for users in broadcast campaigns."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _default_state(self) -> dict:
        """Default balance state structure."""
        now = datetime.now(timezone.utc).isoformat()
        return {
            "posts": 30,  # Default free tier: 30 posts
            "created_at": now,
            "total_purchased": 0,
            "total_spent": 0,
            "last_purchase": None,
            "history": [
                {
                    "type": "initial_free",
                    "amount": 30,
                    "timestamp": now,
                }
            ],
        }

    def _ensure_initial_history(self, state: dict) -> bool:
        """
        Ensure initial free-tier history exists for a brand-new user state.

        Returns True if the state was modified.
        """
        history = state.get("history")
        if not isinstance(history, list):
            state["history"] = []
            history = state["history"]

        if history:
            return False

        posts = int(state.get("posts") or 0)
        total_purchased = int(state.get("total_purchased") or 0)
        total_spent = int(state.get("total_spent") or 0)
        last_purchase = state.get("last_purchase")

        # Backfill only when the state looks like a never-used free tier.
        if posts == 30 and total_purchased == 0 and total_spent == 0 and not last_purchase:
            created_at = state.get("created_at") or datetime.now(timezone.utc).isoformat()
            state["created_at"] = created_at
            state["history"].append(
                {
                    "type": "initial_free",
                    "amount": 30,
                    "timestamp": created_at,
                }
            )
            return True

        return False

    def load(self) -> dict:
        """Load balance state from JSON file."""
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

        # Merge with defaults to ensure all fields exist
        state = self._default_state()
        if isinstance(data, dict):
            state.update(data)

        # Ensure all required fields exist
        state.setdefault("posts", 30)
        state.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        state.setdefault("total_purchased", 0)
        state.setdefault("history", [])
        state.setdefault("total_spent", 0)
        state.setdefault("last_purchase", None)

        # Ensure history is a list
        if not isinstance(state.get("history"), list):
            state["history"] = []

        modified = self._ensure_initial_history(state)
        if modified:
            self.save(state)

        return state

    def save(self, state: dict) -> None:
        """Save balance state to JSON file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def get_balance(self) -> int:
        """Get current post balance."""
        state = self.load()
        return state.get("posts", 30)

    def check_sufficient(self, required: int) -> bool:
        """Check if there are enough posts for the broadcast."""
        balance = self.get_balance()
        return balance >= required

    def spend_posts(
        self,
        amount: int,
        groups_count: int,
        sent_count: int,
        summary: str,
    ) -> bool:
        """
        Spend posts after successful broadcast.

        Args:
            amount: Number of posts to spend
            groups_count: Total groups attempted
            sent_count: Number of groups that received the message
            summary: Human-readable summary of the broadcast

        Returns:
            True if successful
        """
        if amount <= 0:
            return False

        state = self.load()

        # Check if we have enough
        if state.get("posts", 0) < amount:
            return False

        # Deduct posts
        state["posts"] -= amount
        state["total_spent"] = state.get("total_spent", 0) + amount

        # Record in history
        state["history"].append({
            "type": "spent",
            "amount": amount,
            "groups_count": groups_count,
            "sent_count": sent_count,
            "summary": summary,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        self.save(state)
        return True

    def add_posts(self, amount: int, price_id: str = None) -> bool:
        """
        Add posts (after payment).

        Args:
            amount: Number of posts to add
            price_id: Stripe price ID (for reference)

        Returns:
            True if successful
        """
        if amount <= 0:
            return False

        state = self.load()
        state["posts"] += amount
        state["total_purchased"] = state.get("total_purchased", 0) + amount
        state["last_purchase"] = datetime.now(timezone.utc).isoformat()

        # Record in history
        state["history"].append({
            "type": "purchase",
            "amount": amount,
            "price_id": price_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        self.save(state)
        return True

    def get_history(self, limit: int = 10) -> list:
        """Get transaction history (last N entries)."""
        state = self.load()
        history = state.get("history", [])
        # Return in reverse order (most recent first) and limit
        return list(reversed(history))[:limit]

    def reset_to_free_tier(self) -> None:
        """Reset balance to free tier (30 posts)."""
        state = self.load()
        state["posts"] = 30
        self.save(state)


def scoped_balance_manager(user_id: int, path_bot_dir: Path = None, owner_ids: set = None) -> BalanceManager:
    """
    Get a BalanceManager scoped to a specific user.

    Owner uses global balance, regular users get isolated balance.

    Args:
        user_id: Telegram user ID
        path_bot_dir: Bot directory path (defaults to Path(__file__).parent)
        owner_ids: Set of owner user IDs (defaults to empty set)
    """
    if path_bot_dir is None:
        # Keep legacy signature but default to persisted state location when available.
        path_bot_dir = state_file(".").parent
    if owner_ids is None:
        owner_ids = set()

    if user_id in owner_ids or not owner_ids:
        # Owner uses global balance
        path = state_file("balance_state.json")
    else:
        # User gets isolated balance
        path = user_data_dir() / str(user_id) / "balance_state.json"

    return BalanceManager(path)
