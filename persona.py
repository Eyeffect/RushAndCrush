"""
The chat brain. Rule-based, soldier-voiced, anti-spam.

Triggers arrive with game context; the Persona decides whether to speak
(rate limits, cooldowns, never repeating a line until its pool is
exhausted) and returns the message text.
"""

import random
import threading
import time
from collections import defaultdict


class Persona:
    def __init__(self, cfg: dict, username: str) -> None:
        chat = (cfg or {}).get("chat", {})
        self.enabled = bool(chat.get("enabled", True))
        self.lines: dict[str, list[str]] = chat.get("lines", {}) or {}
        self.min_gap = float(chat.get("min_gap_seconds", 25))
        self.trigger_cooldown = float(chat.get("trigger_cooldown", 120))
        self.random_skip = float(chat.get("random_skip", 0.35))
        self.username = username

        self._bag: dict[str, list[str]] = defaultdict(list)
        self._last_global = 0.0
        self._last_trigger: dict[str, float] = defaultdict(float)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def compose(self, trigger: str, subs: dict | None = None,
                force: bool = False) -> str | None:
        """Return a chat line for this trigger, or None if silenced."""
        if not self.enabled:
            return None
        pool = self.lines.get(trigger)
        if not pool:
            return None

        with self._lock:
            now = time.time()
            if not force:
                if now - self._last_global < self.min_gap:
                    return None
                if now - self._last_trigger[trigger] < self.trigger_cooldown:
                    return None
                if random.random() < self.random_skip:
                    return None
            line = self._next_line(trigger)
            if line is None:
                return None
            self._last_global = now
            self._last_trigger[trigger] = now

        subs = {"me": self.username, "opponent": "soldier", **(subs or {})}
        try:
            return line.format(**subs)
        except (KeyError, IndexError):
            return line  # template referenced an unknown field; send it raw

    # ------------------------------------------------------------------

    def _next_line(self, trigger: str) -> str | None:
        bag = self._bag[trigger]
        if not bag:
            bag = random.sample(self.lines[trigger], k=len(self.lines[trigger]))
            self._bag[trigger] = bag
        return bag.pop()
