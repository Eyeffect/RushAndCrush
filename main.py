"""
RushAndCrush - entrypoint.

  python main.py --upgrade    convert the account to a BOT account (one-time, irreversible)
  python main.py              run the bot: hunt, chat, fight

The Lichess token is read from the LICHESS_BOT_TOKEN environment variable,
or from the `token` field in config.yml.
"""

import argparse
import json
import logging
import os
import sys
import threading

import requests
import yaml

from engine import Engine
from game_manager import GameHandler
from hunter import Hunter
from learning import Learner
from lichess_api import LichessAPI, TokenError
from persona import Persona
from state import HunterMemory, NemesisStore, StatsStore

log = logging.getLogger("rushandcrush")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("rushandcrush.log", encoding="utf-8"),
        ],
    )


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Bot:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        token = os.environ.get("LICHESS_BOT_TOKEN") or cfg.get("token", "")
        if not token:
            raise SystemExit(
                "No token found. Set the LICHESS_BOT_TOKEN environment variable "
                'or put it in config.yml under "token".')
        self.api = LichessAPI(token, cfg.get("url", "https://lichess.org"))
        self.api.verify_token()

        profile = self.api.profile()
        self.me = profile.get("username", "?")
        cfg.setdefault("account", {})["username"] = self.me

        self.engine = Engine(cfg.get("engine", {}))
        self.persona = Persona(cfg.get("persona", {}), self.me)
        self.stores = type("Stores", (), {})()
        self.stores.stats = StatsStore("data/stats.json")
        self.stores.nemesis = NemesisStore("data/nemesis.json")
        self.stores.hunter = HunterMemory("data/hunter.json")
        self.stores.learner = Learner("data/learning.json", cfg.get("learning", {}))

        self.handlers: dict[str, GameHandler] = {}
        self.handlers_lock = threading.Lock()
        self.stopping = threading.Event()

        self.hunter = Hunter(self.api, cfg, self.stores, self._active_games)

    # ------------------------------------------------------------------

    def _active_games(self) -> int:
        with self.handlers_lock:
            return sum(1 for h in self.handlers.values() if h.is_alive() and not h.done)

    def _start_game(self, game_id: str) -> None:
        with self.handlers_lock:
            existing = self.handlers.get(game_id)
            if existing and existing.is_alive() and not existing.done:
                return
            handler = GameHandler(self.api, self.engine, self.persona,
                                  self.stores, self.cfg, game_id)
            self.handlers[game_id] = handler
        handler.start()

    def _handle_challenge(self, challenge: dict) -> None:
        cid = challenge.get("id")
        challenger = ((challenge.get("challenger") or {}).get("name") or "")
        if challenger.lower() == self.me.lower():
            # echo of our own outgoing challenge - never accept (404) and
            # never decline (that would withdraw our own challenge)
            log.info("Ignoring self-echo for challenge %s", cid)
            return
        accept, reason = self.hunter.decide_incoming(challenge)
        try:
            if accept and cid:
                self.api.accept(cid)
            elif cid:
                self.api.decline(cid, reason or "generic")
        except requests.RequestException as e:
            log.warning("Challenge %s decision failed: %s", cid, e)

    # ------------------------------------------------------------------

    def run(self) -> None:
        log.info("RushAndCrush (%s) reporting for duty.", self.me)
        log.info("Career: %s", self.stores.stats.summary())

        self.hunter.start()

        # resume games that survived a restart
        try:
            for game in self.api.playing():
                gid = game.get("gameId")
                if gid:
                    log.info("Resuming ongoing game %s", gid)
                    self._start_game(gid)
        except Exception as e:
            log.warning("Could not resume games: %s", e)

        backoff = 5
        while not self.stopping.is_set():
            try:
                resp = self.api.event_stream()
                backoff = 5
                log.info("Connected to the event stream. Hunting...")
                for event in self.api._ndjson(resp):
                    if self.stopping.is_set():
                        break
                    self._dispatch(event)
            except KeyboardInterrupt:
                break
            except (requests.RequestException, json.JSONDecodeError) as e:
                log.warning("Event stream dropped (%s). Reconnecting in %ss.", e, backoff)
                self.stopping.wait(backoff)
                backoff = min(backoff * 2, 120)

        log.info("RushAndCrush standing down. Career: %s",
                 self.stores.stats.summary())

    def _dispatch(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "gameStart":
            game = event.get("game") or {}
            self._start_game(game.get("id"))
        elif etype == "gameFinish":
            game = event.get("game") or {}
            gid = game.get("id")
            if game.get("winner"):
                log.info("[%s] gameFinish, winner: %s", gid, game.get("winner"))
        elif etype == "challenge":
            threading.Thread(
                target=self._handle_challenge,
                args=(event.get("challenge") or {},),
                daemon=True,
                name="challenge-decision",
            ).start()
        elif etype == "challengeDeclined":
            self.hunter.on_declined(event)


def main() -> None:
    parser = argparse.ArgumentParser(description="RushAndCrush lichess bot")
    parser.add_argument("--config", default="config.yml", help="path to config.yml")
    parser.add_argument("--upgrade", action="store_true",
                        help="upgrade the account to a BOT account (irreversible)")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config(args.config)

    token = os.environ.get("LICHESS_BOT_TOKEN") or cfg.get("token", "")
    if not token:
        raise SystemExit("No token. Set LICHESS_BOT_TOKEN or edit config.yml.")
    api = LichessAPI(token, cfg.get("url", "https://lichess.org"))

    if args.upgrade:
        api.verify_token()
        print("Upgrading account to a BOT account. This is IRREVERSIBLE.")
        print("The account will never play normally again. Continue? [y/N] ", end="")
        if input().strip().lower() != "y":
            print("Aborted.")
            return
        api.upgrade()
        print("Done. The account is now a bot. Run `python main.py` to play.")
        return

    bot = Bot(cfg)
    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("Interrupted.")


if __name__ == "__main__":
    try:
        main()
    except TokenError as e:
        print(f"TOKEN ERROR: {e}", file=sys.stderr)
        sys.exit(1)
