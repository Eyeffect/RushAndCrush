"""
Minimal, robust client for the Lichess Bot API.

Only the endpoints RushAndCrush needs. Handles retries on transient
errors, exposes NDJSON streams, and never logs the token.
"""

import json
import logging
import threading
import time

import requests

log = logging.getLogger("rushandcrush.api")

RETRY_DELAYS = (1, 2, 4, 8, 15)
PAUSE_AFTER_429 = 60.0  # Lichess etiquette: one full minute after any 429
MOVE_RETRY_DELAY = 2.0  # game moves retry fast - flagging is worse
# Move POSTs get their own short ladder: a flaky night must never cost
# 1+2+4+8+15s of wall clock on a 3+0 game. Worst case below is ~9s
# including timeouts, after which the handler discards the eval and the
# next gameState resyncs the board.
MOVE_RETRY_DELAYS = (0.2, 0.5, 1.0)


class TokenError(RuntimeError):
    """The API token is missing, invalid, or lacks the bot:play scope."""


class LichessAPI:
    def __init__(self, token: str, base_url: str = "https://lichess.org") -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self.s = requests.Session()
        self.s.headers.update(
            {"Authorization": f"Bearer {token}", "User-Agent": "RushAndCrush/1.0"}
        )
        self._pause_until = 0.0
        self._pause_lock = threading.Lock()
        # EMA of measured move-POST round-trip (seconds). The time budget
        # reads this instead of guessing a fixed 300ms: a slow night buys
        # less think time, a fast night banks it.
        self.move_overhead = 0.30

    # ---------------- low level ----------------

    def _set_pause(self, seconds: float) -> None:
        """After a 429, ALL non-move traffic must stand down for a while."""
        with self._pause_lock:
            self._pause_until = max(self._pause_until, time.time() + seconds)

    def paused_for(self) -> float:
        """Seconds left in the global 429 pause (the hunter checks this)."""
        with self._pause_lock:
            return max(0.0, self._pause_until - time.time())

    def _wait_pause(self) -> None:
        while True:
            remaining = self.paused_for()
            if remaining <= 0:
                return
            log.info("Global 429 pause: standing down %.0fs", remaining)
            time.sleep(min(remaining, 5.0))

    def _request(self, method: str, path: str, *, stream: bool = False,
                 params: dict | None = None, data=None, payload=None,
                 headers: dict | None = None, timeout: float = 10,
                 is_move: bool = False):
        # moves walk the short ladder: on the clock, surrendering fast
        # beats a doomed 30s retry march
        delays = MOVE_RETRY_DELAYS if is_move else RETRY_DELAYS
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*delays, None)):
            if not is_move:
                self._wait_pause()
            try:
                r = self.s.request(
                    method, self.base + path, params=params, data=data,
                    json=payload, headers=headers, stream=stream, timeout=timeout,
                )
                if r.status_code == 429:
                    # Retrying inside the cooldown can turn a hiccup into a
                    # longer throttle: non-move requests back off globally.
                    # Game moves retry fast - forfeiting on time is worse.
                    log.warning("HTTP 429 on %s (%s)", path,
                                "fast move retry" if is_move else "global pause")
                    last_exc = requests.HTTPError("HTTP 429", response=r)
                    if not is_move:
                        self._set_pause(PAUSE_AFTER_429)
                    if delay is None:
                        r.raise_for_status()
                    time.sleep(MOVE_RETRY_DELAY if is_move else 2.0)
                    continue
                if r.status_code >= 500:
                    log.warning("HTTP %s on %s, retrying in %ss",
                                r.status_code, path, delay)
                    last_exc = requests.HTTPError(f"HTTP {r.status_code}", response=r)
                    if delay is None:
                        r.raise_for_status()
                    time.sleep(delay)
                    continue
                r.raise_for_status()
                r.encoding = "utf-8"
                return r
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                if delay is None:
                    raise
                log.warning("Network error on %s (%s), retrying in %ss", path, e, delay)
                time.sleep(delay)
        raise last_exc  # pragma: no cover

    def _get(self, path, **kw):
        return self._request("GET", path, **kw)

    def _post(self, path, **kw):
        return self._request("POST", path, **kw)

    @staticmethod
    def _ndjson(resp):
        """Iterate a newline-delimited JSON stream."""
        for line in resp.iter_lines():
            if line:
                yield json.loads(line)

    # ---------------- account ----------------

    def verify_token(self) -> dict:
        """Check the token works and has bot:play. Raises TokenError if not."""
        r = self._post("/api/token/test", data=self.token, headers={
            "Content-Type": "application/x-www-form-urlencoded"})
        info = r.json().get(self.token, {})
        if not info:
            raise TokenError("Token rejected by Lichess. Check that it was copied correctly.")
        scopes = info.get("scopes", "")
        if "bot:play" not in scopes.split(","):
            raise TokenError(
                f'Token is missing the "bot:play" scope (has: "{scopes}"). '
                "Create a new token at lichess.org/settings/oauth/token with "
                '"Play games with the bot API" checked.')
        return info

    def upgrade(self) -> None:
        """Irreversibly upgrade the account to a bot account."""
        self._post("/api/bot/account/upgrade")

    def profile(self) -> dict:
        return self._get("/api/account").json()

    def playing(self) -> list[dict]:
        """Ongoing games (used to resume after a restart)."""
        return self._get("/api/account/playing").json().get("nowPlaying", [])

    # ---------------- streams ----------------

    def event_stream(self):
        """Infinite stream of events: challenges, gameStart, gameFinish, chatLine."""
        return self._get("/api/stream/event", stream=True, timeout=25)

    def game_stream(self, game_id: str):
        """Stream of a single game: gameFull, then gameState / chatLine."""
        return self._get(f"/api/bot/game/stream/{game_id}", stream=True, timeout=25)

    # ---------------- in-game actions ----------------

    def move(self, game_id: str, uci: str, offering_draw: bool = False) -> None:
        t0 = time.time()
        self._post(
            f"/api/bot/game/{game_id}/move/{uci}",
            params={"offeringDraw": str(bool(offering_draw)).lower()},
            timeout=2.5,
            is_move=True,  # bypasses the global 429 pause
        )
        # measure the round-trip into the EMA the time budget consumes
        dt = time.time() - t0
        self.move_overhead = max(0.15, min(1.5,
                                           0.8 * self.move_overhead + 0.2 * dt))

    def resign(self, game_id: str) -> None:
        self._post(f"/api/bot/game/{game_id}/resign", timeout=5)

    def abort(self, game_id: str) -> None:
        self._post(f"/api/bot/game/{game_id}/abort", timeout=5)

    def chat(self, game_id: str, room: str, text: str) -> None:
        text = text[:140]
        try:
            self._post(f"/api/bot/game/{game_id}/chat",
                       data={"room": room, "text": text}, timeout=5)
        except requests.RequestException as e:
            log.warning("Chat failed in game %s: %s", game_id, e)

    # ---------------- challenges ----------------

    def accept(self, challenge_id: str) -> None:
        self._post(f"/api/challenge/{challenge_id}/accept", timeout=5)

    def decline(self, challenge_id: str, reason: str = "generic") -> None:
        try:
            self._post(
                f"/api/challenge/{challenge_id}/decline",
                data=f"reason={reason}",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=5,
            )
        except requests.RequestException as e:
            log.warning("Decline failed: %s", e)

    def challenge(self, username: str, payload: dict) -> dict:
        """Create a challenge. Returns response JSON (may contain 'error')."""
        r = self._post(f"/api/challenge/{username}", payload=payload, timeout=10)
        return r.json()

    def cancel(self, challenge_id: str) -> None:
        try:
            self._post(f"/api/challenge/{challenge_id}/cancel", timeout=5)
        except requests.RequestException:
            pass

    # ---------------- opponent intel ----------------

    def online_bots(self, nb: int = 512) -> list[dict]:
        """List of currently online bots with their ratings (NDJSON)."""
        try:
            text = self._get("/api/bot/online", params={"nb": str(nb)}).text
            return [json.loads(l) for l in text.splitlines() if l]
        except requests.RequestException as e:
            log.warning("online_bots failed: %s", e)
            return []

    def public_data(self, username: str) -> dict:
        return self._get(f"/api/user/{username}").json()

    def users_status(self, ids: list[str]) -> list[dict]:
        return self._get("/api/users/status", params={"ids": ",".join(ids)}).json()

    # ---------------- internal ----------------
