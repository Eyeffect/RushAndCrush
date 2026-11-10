"""Persistent state: nemesis list, hunting memory, and win/loss stats."""

import json
import os
import threading
import time


class JsonStore:
    """Thread-safe JSON file store with atomic writes."""

    def __init__(self, path: str, default: dict | None = None) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.default = default if default is not None else {}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def load(self) -> dict:
        with self.lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                return dict(self.default)

    def save(self, data: dict) -> None:
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, self.path)


class NemesisStore:
    """Everyone who beat us, plus how many times we beat them back.
    The hunter prioritizes revenge; the chat reads the head-to-head
    record so we never taunt about a loss we already avenged."""

    def __init__(self, path: str) -> None:
        self.store = JsonStore(path, default={"losses": {}, "wins": {}})
        self.cooldown_s = 1800  # overridden from config by hunter

    def add_loss(self, username: str) -> None:
        if not username:
            return
        data = self.store.load()
        entry = data["losses"].setdefault(username, {"count": 0, "last": 0})
        entry["count"] += 1
        entry["last"] = time.time()
        self.store.save(data)

    def add_win(self, username: str) -> None:
        if not username:
            return
        data = self.store.load()
        wins = data.setdefault("wins", {})
        wins[username] = wins.get(username, 0) + 1
        self.store.save(data)

    def head_to_head(self, username: str) -> tuple[int, int]:
        """(wins, losses) vs this opponent. (0, 0) means never played."""
        data = self.store.load()
        return (data.get("wins", {}).get(username, 0),
                data.get("losses", {}).get(username, {}).get("count", 0))

    def next_target(self) -> str | None:
        """The most-worthy nemesis whose cooldown expired and who STILL
        leads the series - an avenged opponent is off the revenge list."""
        data = self.store.load()
        wins = data.get("wins", {})
        now = time.time()
        best, best_count = None, 0
        for name, e in data.get("losses", {}).items():
            if now - e["last"] < self.cooldown_s:
                continue
            if e["count"] <= wins.get(name, 0):
                continue
            if e["count"] > best_count:
                best, best_count = name, e["count"]
        return best


class HunterMemory:
    """Remembers recent outgoing challenges so we don't spam anyone."""

    def __init__(self, path: str) -> None:
        self.store = JsonStore(path, default={"tried": {}})
        self.remember_s = 900  # don't re-challenge the same bot for 15 min

    def record_attempt(self, username: str) -> None:
        data = self.store.load()
        data["tried"][username] = time.time()
        # prune entries older than a day
        cutoff = time.time() - 86400
        data["tried"] = {u: t for u, t in data["tried"].items() if t > cutoff}
        self.store.save(data)

    def recently_tried(self, username: str) -> bool:
        t = self.store.load()["tried"].get(username, 0)
        return time.time() - t < self.remember_s


class StatsStore:
    """Lifetime results."""

    def __init__(self, path: str) -> None:
        self.store = JsonStore(path, default={"wins": 0, "losses": 0, "draws": 0, "games": 0})

    def record(self, result: str) -> None:
        data = self.store.load()
        data["games"] += 1
        key = {"win": "wins", "loss": "losses", "draw": "draws"}.get(result)
        if key:
            data[key] += 1
        self.store.save(data)

    def summary(self) -> str:
        d = self.store.load()
        return f"{d['games']} games | {d['wins']}W {d['losses']}L {d['draws']}D"
