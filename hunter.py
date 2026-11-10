"""
The hunter. Grinds matches autonomously:

  1. challenges online bots near our rating (rated blitz),
  2. prioritizes the nemesis list (players who beat us),
  3. accepts incoming human challenges (casual, blitz/rapid),
  4. respects Lichess rate limits, especially the daily bot-vs-bot cap.
"""

import logging
import random
import threading
import time

import requests

log = logging.getLogger("rushandcrush.hunter")


class Hunter(threading.Thread):
    def __init__(self, api, cfg: dict, stores, active_games_fn) -> None:
        super().__init__(daemon=True, name="hunter")
        self.api = api
        self.cfg = cfg.get("hunter", {})
        self.stores = stores
        self.active_games = active_games_fn
        self.username = cfg.get("account", {}).get("username", "")

        self.enabled = bool(self.cfg.get("enabled", True))
        self.max_games = int(self.cfg.get("max_games", 2))
        self.interval = float(self.cfg.get("challenge_interval", 60))
        self.idle_check = float(self.cfg.get("idle_check", 15))
        self.min_rating = int(self.cfg.get("opponent_min_rating", 600))
        self.max_rating = int(self.cfg.get("opponent_max_rating", 2200))
        self.nemesis_cooldown_min = float(self.cfg.get("nemesis_cooldown_min", 30))

        self.stores.nemesis.cooldown_s = self.nemesis_cooldown_min * 60
        self.stores.hunter.remember_s = float(
            self.cfg.get("challenge_timeout_min", 15)) * 60

        self.last_challenge = 0.0
        self.my_rating: int | None = None
        self.rating_checked = 0.0
        self.pause_until = 0.0  # daily rate-limit backoff
        self._bots_cache: list[dict] = []
        self._bots_at = 0.0
        self._bot_titles: dict[str, bool] = {}

    # ------------------------------------------------------------------

    def run(self) -> None:
        log.info("Hunter awake. Grind mode: %s", "on" if self.enabled else "off")
        while not self.enabled:
            time.sleep(30)  # accepting-only mode; handled via decide_incoming

        while True:
            try:
                time.sleep(self.idle_check)
                if time.time() < max(self.pause_until, self.api.paused_for()):
                    continue
                if self.active_games() >= self.max_games:
                    continue
                if time.time() - self.last_challenge < self.interval:
                    continue
                self._hunt()
            except Exception as e:  # never let the hunter die
                log.warning("hunter tick failed: %s", e)

    # ------------------------------------------------------------------

    def _hunt(self) -> None:
        nemesis = self.stores.nemesis.next_target()
        if nemesis and self._hunt_nemesis(nemesis):
            return

        target = self._pick_bot_target()
        if target:
            name, rating = target
            log.info("Hunting bot %s (blitz %s)", name, rating)
            self._challenge(name, human=False)

    def _hunt_nemesis(self, name: str) -> bool:
        """Revenge is bots-only and capped: humans get peace, and constant
        rated rematches with the same bot look like boosting. Returns True
        if a challenge went out."""
        if not self._is_bot(name):
            log.info("Nemesis %s is not a bot - the soldier lets it go.", name)
            return False
        learner = getattr(self.stores, "learner", None)
        if learner is not None:
            if not learner.can_rematch(name):
                log.info("Nemesis %s already had their rematches today.", name)
                return False
            learner.record_rematch(name)
        log.info("Nemesis spotted: %s. Re-challenging.", name)
        self._challenge(name, human=False)
        return True

    def _online_bots(self) -> list[dict]:
        """Bot list cached for 5 minutes - no reason to refetch every tick."""
        if time.time() - self._bots_at > 300:
            self._bots_cache = self.api.online_bots()
            self._bots_at = time.time()
        return self._bots_cache

    def _pick_bot_target(self) -> tuple[str, int] | None:
        bots = self._online_bots()
        self._refresh_rating()
        cands = []
        for b in bots:
            name = b.get("username", "")
            if name.lower() == self.username.lower():
                continue
            if self.stores.hunter.recently_tried(name):
                continue
            blitz = (b.get("perfs") or {}).get("blitz") or {}
            rating, games = blitz.get("rating", 0), blitz.get("games", 0)
            if games < 5:  # no reliable rating yet
                continue
            if not (self.min_rating <= rating <= self.max_rating):
                continue
            cands.append((name, rating))
        return random.choice(cands) if cands else None

    def _challenge(self, username: str, human: bool) -> None:
        tcs = self.cfg.get("time_controls", [[180, 0], [180, 2], [300, 0], [300, 3]])
        limit, inc = random.choice(tcs)
        payload = {
            "rated": "false" if human else "true",
            "variant": "standard",
            "clock.limit": str(limit),
            "clock.increment": str(inc),
            "color": "random",
        }
        try:
            resp = self.api.challenge(username, payload)
        except requests.HTTPError as e:
            # 4xx raises inside the client; parse the body for rate-limit info
            self.last_challenge = time.time()
            self.stores.hunter.record_attempt(username)
            body = {}
            try:
                if e.response is not None:
                    body = e.response.json()
            except Exception:
                pass
            rl = body.get("ratelimit") or {}
            if rl.get("seconds"):
                wait = float(rl["seconds"])
                log.warning("Rate limited (%s). Pausing hunting for %d min.",
                            rl.get("key"), wait / 60)
                self.pause_until = time.time() + wait
            else:
                log.warning("Challenge to %s refused: %s",
                            username, body.get("error") or e)
            return
        except Exception as e:
            log.warning("Challenge to %s failed: %s", username, e)
            self.last_challenge = time.time()
            return

        self.last_challenge = time.time()
        self.stores.hunter.record_attempt(username)

        err = resp.get("error")
        if err:
            log.info("Challenge to %s refused: %s", username, err)
            rl = resp.get("ratelimit") or {}
            if rl.get("seconds"):
                # bot-vs-bot daily cap hit: pause hard
                wait = float(rl["seconds"])
                log.warning("Rate limited (%s). Pausing hunting for %d min.",
                            rl.get("key"), wait / 60)
                self.pause_until = time.time() + wait
        else:
            ch = resp.get("challenge") or resp  # response shape varies
            log.info("Challenge sent to %s (%d+%d, id %s, status %s)",
                     username, limit, inc, ch.get("id"), ch.get("status"))

    # ------------------------------------------------------------------

    def decide_incoming(self, challenge: dict) -> tuple[bool, str]:
        """Decide whether to accept an incoming challenge. Returns (accept, reason)."""
        cid = challenge.get("id", "?")
        speed = (challenge.get("timeControl") or {}).get("speed", "unknown")
        variant = (challenge.get("variant") or {}).get("key", "standard")
        rated = bool(challenge.get("rated"))
        challenger = challenge.get("challenger") or {}
        name = challenger.get("name", "?")
        is_bot = challenger.get("title") == "BOT"

        if variant != "standard":
            log.info("Declining %s from %s: variant %s", cid, name, variant)
            return False, "standard"
        if speed in ("ultraBullet", "bullet", "correspondence"):
            log.info("Declining %s from %s: time control %s", cid, name, speed)
            return False, "timeControl"
        if self.active_games() >= self.max_games:
            return False, "later"

        if is_bot:
            log.info("Accepting bot challenge %s from %s", cid, name)
            return True, ""

        # human challenger: casual games only (lichess enforces this anyway)
        if rated:
            return False, "rated"
        log.info("Accepting human challenge %s from %s (%s)", cid, name, speed)
        return True, ""

    def on_declined(self, event: dict) -> None:
        name = ((event.get("challenge") or {}).get("destined") or "opponent")
        reason = event.get("reason", "?")
        log.info("Challenge declined by %s (%s)", name, reason)
        self.stores.hunter.record_attempt(name)

    # ------------------------------------------------------------------

    def _refresh_rating(self) -> None:
        if time.time() - self.rating_checked < 600:
            return
        self.rating_checked = time.time()
        try:
            prof = self.api.profile()
            blitz = (prof.get("perfs") or {}).get("blitz") or {}
            rating = blitz.get("rating")
            games = blitz.get("games", 0)
            if games >= 5 and rating:
                # reliable rating: re-center the hunting window on it
                # (rating can DROP after losses - the window must follow)
                self.my_rating = rating
                lo = max(400, rating - 250)
                hi = max(rating + 350, lo + 100)
                self.min_rating = lo
                self.max_rating = hi
            elif self.my_rating is None:
                self.my_rating = rating or 1500  # provisional; display only
            log.info("My blitz rating: %s. Hunting range: %s-%s",
                     self.my_rating, self.min_rating, self.max_rating)
        except Exception as e:
            log.warning("Rating refresh failed: %s", e)

    def _is_bot(self, username: str) -> bool:
        if username in self._bot_titles:
            return self._bot_titles[username]
        try:
            is_bot = self.api.public_data(username).get("title") == "BOT"
        except Exception:
            return False  # unknown - don't cache, don't guess
        self._bot_titles[username] = is_bot
        return is_bot
