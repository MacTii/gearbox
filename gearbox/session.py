from __future__ import annotations

import time

from .config import TIER_ORDER


class SessionStore:
    """Remembers the current tier per conversation so continuations and policies stay consistent."""

    def __init__(self, ttl_seconds: int = 6 * 3600):
        self.ttl = ttl_seconds
        self._data: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> str | None:
        item = self._data.get(key)
        if not item:
            return None
        tier, ts = item
        if time.time() - ts > self.ttl:
            del self._data[key]
            return None
        return tier

    def set(self, key: str, tier: str) -> None:
        self._data[key] = (tier, time.time())
        if len(self._data) > 5000:
            cutoff = time.time() - self.ttl
            self._data = {k: v for k, v in self._data.items() if v[1] > cutoff}

    def reset(self, key: str) -> None:
        self._data.pop(key, None)


def apply_policy(policy: str, current: str | None, proposed: str, forced: bool) -> str:
    """Combine the session's current tier with a new classification."""
    if current is None or forced or policy == "per_prompt":
        return proposed
    if policy == "sticky":
        return current
    # upgrade_only
    return max(current, proposed, key=TIER_ORDER.index)
