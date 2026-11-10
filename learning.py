"""
The Learner: RushAndCrush's memory and his drill sergeant.

Everything the bot learns from his own games lives in one JSON file:

  * Genes - tunable aspects of his play:
      pawn_adv            how much he pays pawns to advance
      prox                how much he pays pieces to approach the enemy king
      king_shield         penalty for missing pawn shields (starts at 0:
                          the soldier learns defense ONLY by being mated)
      style_pick_prob     chance of choosing a violent move over the best one
      style_eval_slack    centipawns he'll pay for violence
      style_pick_lowtime  style pick chance when under 20s
      endgame_time_mult   extra think time in endgames
  * Opening book - learned from results: lines that win get followed,
    lines that lose get abandoned
  * Blunder ledger - every mistake filed by phase / cause, and the
    lessons derived from them
  * Window evolution - every N games, compare win rate: improving ->
    snapshot genes + mutate slightly; declining -> revert the experiment
"""

import json
import logging
import os
import random
import time

from state import JsonStore

log = logging.getLogger("rushandcrush.learn")

DEFAULT_GENES = {
    "pawn_adv": 1.0,
    "prox": 1.0,
    "king_shield": 0.0,
    "style_pick_prob": 0.25,
    "style_eval_slack": 45,
    "style_pick_lowtime": 0.25,
    "endgame_time_mult": 1.0,
}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class Learner:
    def __init__(self, path: str, cfg: dict | None = None) -> None:
        cfg = cfg or {}
        self.book_depth = int(cfg.get("book_depth", 8))
        self.book_min_samples = int(cfg.get("book_min_samples", 3))
        self.window_size = int(cfg.get("window", 10))
        self.freeze_until = int(cfg.get("freeze_until_games", 100))
        self.seed_cfg = cfg.get("seed_book", {}) or {}
        self.blunder_log = os.path.join(
            os.path.dirname(os.path.abspath(path)), "blunders.jsonl")
        self.store = JsonStore(path, default=self._empty())
        data = self.store.load()
        if "genes" not in data:  # first boot
            self.store.save(data)
        self._seed_book()

    def _seed_book(self) -> None:
        """Load the doctrine: aggressive openings pre-graded so they play
        immediately, with real results free to demote and kill them.

        Each seed line is a full variation in UCI moves; we file OUR turns
        (even plies for white lines, odd for black) with a 2W/1L record -
        exactly book_min_samples, so the move is trusted at once. Seeds
        never overwrite real experience: if the position was ever played,
        the learned counts stand."""
        if not self.seed_cfg:
            return
        data = self.store.load()
        book = data.setdefault("book", {})
        added = 0
        for color, lines in self.seed_cfg.items():
            we_are_white = color == "white"
            if not isinstance(lines, list):
                continue
            for line in lines:
                moves = str(line).split()
                for i in range(min(len(moves), self.book_depth)):
                    if (i % 2 == 0) != we_are_white:
                        continue  # not our turn in this variation
                    node = book.setdefault(
                        " ".join(moves[:i]), {}).setdefault("next", {})
                    if moves[i] not in node:
                        node[moves[i]] = {"w": 2, "l": 1, "d": 0}
                        added += 1
        if added:
            self.store.save(data)
            log.info("REPERTOIRE: %d aggressive opening moves seeded into "
                     "the book (results may kill them, as with any line).",
                     added)

    @staticmethod
    def _empty() -> dict:
        return {
            "genes": dict(DEFAULT_GENES),
            "window": {"games": 0, "wins": 0, "losses": 0, "draws": 0},
            "snapshot": None,
            "book": {},
            "blunders": {
                "total": 0, "style_pick": 0, "time_trouble": 0,
                "opening": 0, "middlegame": 0, "endgame": 0,
                "phases": {},
            },
            "mate_losses": 0,
            "mate_wins": 0,
            "games_total": 0,
            "rematches": {},
        }

    # ------------------------------------------------------------------
    # genes
    # ------------------------------------------------------------------

    def genes(self) -> dict:
        """Current genes, merged over defaults (learned values win)."""
        data = self.store.load()
        g = dict(DEFAULT_GENES)
        g.update(data.get("genes", {}))
        return g

    def _update_genes(self, mutate) -> None:
        data = self.store.load()
        genes = dict(DEFAULT_GENES)
        genes.update(data.get("genes", {}))
        mutate(genes)
        data["genes"] = genes
        self.store.save(data)

    # ------------------------------------------------------------------
    # blunders (detected live by the game handler)
    # ------------------------------------------------------------------

    def record_blunder(self, phase: str, clock_s: float | None,
                       was_style_pick: bool, fen: str | None = None,
                       uci: str | None = None, drop: int = 0) -> bool:
        """File a mistake. Returns True if this exact lesson repeats.

        Hygiene rules (the ledger only teaches when it tells the truth):
          * callers skip already-decided positions (eval beyond +/-600) and
            mate-scale swings - a dying game is not a classroom;
          * counters reset when a lesson fires, so a lesson can't ratchet
            a gene to its floor inside one long bad game.
        """
        data = self.store.load()
        b = data["blunders"]
        b["total"] += 1
        if was_style_pick:
            b["style_pick"] += 1
        if clock_s is not None and clock_s < 20:
            b["time_trouble"] += 1
        if phase in ("opening", "middlegame", "endgame"):
            b[phase] += 1
        phases = b.setdefault("phases", {})
        phases[phase] = phases.get(phase, 0) + 1

        lessons = []

        total = b["total"]
        if total >= 6:
            share = b["style_pick"] / total
            if share > 0.5 and data["genes"].get("style_pick_prob", 0.25) > 0.05:
                data["genes"]["style_pick_prob"] = (
                    _clamp(data["genes"]["style_pick_prob"] * 0.8, 0.05, 0.35))
                lessons.append(f"bravado costs games (style picks in {share:.0%} "
                               "of blunders) - toning it down")
                b["style_pick"] = 0

        if total >= 5 and b["time_trouble"] / total > 0.4:
            if data["genes"].get("style_pick_lowtime", 0.25) > 0:
                data["genes"]["style_pick_lowtime"] = 0.0
                lessons.append("too many mistakes with a fast clock - no more "
                               "gambling in time trouble")
                b["time_trouble"] = 0

        if total >= 8 and b["endgame"] / total > 0.6:
            if data["genes"].get("endgame_time_mult", 1.0) < 1.3:
                data["genes"]["endgame_time_mult"] = 1.3
                lessons.append("endgames keep breaking me - thinking longer there")
                b["endgame"] = 0

        if lessons:
            b["total"] = 0  # a fired lesson starts a fresh ledger era

        self.store.save(data)
        for lesson in lessons:
            log.info("LESSON: %s", lesson)

        self._log_blunder(phase, clock_s, was_style_pick, fen, uci, drop)
        return phases.get(phase, 0) >= 3

    def _log_blunder(self, phase: str, clock_s: float | None,
                     was_style_pick: bool, fen: str | None,
                     uci: str | None, drop: int) -> None:
        """One JSONL line per blunder - the future regression suite."""
        try:
            rec = {
                "ts": int(time.time()),
                "phase": phase,
                "clock_s": round(clock_s, 1) if clock_s is not None else None,
                "style_pick": bool(was_style_pick),
                "fen": fen,
                "uci": uci,
                "drop": drop,
            }
            with open(self.blunder_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as e:
            log.warning("blunder log write failed: %s", e)

    # ------------------------------------------------------------------
    # end of game report
    # ------------------------------------------------------------------

    def report_game(self, result: str, status: str, all_moves: list[str],
                    we_are_white: bool, opponent: str) -> None:
        data = self.store.load()
        data["games_total"] = data.get("games_total", 0) + 1

        # --- mate lessons: the only way he learns to defend ---
        if status == "mate":
            if result == "loss":
                data["mate_losses"] += 1
                old = data["genes"].get("king_shield", 0.0)
                data["genes"]["king_shield"] = _clamp(old + 0.05, 0.0, 0.5)
                log.info("LESSON: mated by %s. The king gets a small shield "
                         "(%.2f -> %.2f).", opponent, old,
                         data["genes"]["king_shield"])
            elif result == "win":
                data["mate_wins"] += 1
                data["genes"]["king_shield"] = _clamp(
                    data["genes"].get("king_shield", 0.0) - 0.02, 0.0, 0.5)

        # --- opening book: attribute the result to early positions ---
        book = data["book"]
        for i in range(min(len(all_moves), self.book_depth)):
            if (i % 2 == 0) != we_are_white:
                continue  # only file positions where it was OUR turn
            key = " ".join(all_moves[:i])
            nxt = all_moves[i]
            slot = book.setdefault(key, {}).setdefault("next", {}).setdefault(
                nxt, {"w": 0, "l": 0, "d": 0})
            if result == "win":
                slot["w"] += 1
            elif result == "loss":
                slot["l"] += 1
            else:
                slot["d"] += 1
        # prune lines with no data (keep everything with at least one game;
        # get_book_move filters by reliability)
        for key in list(book):
            nxt = book[key].get("next", {})
            book[key]["next"] = {
                u: c for u, c in nxt.items()
                if c["w"] + c["l"] + c["d"] >= 1
            }
            if not book[key]["next"]:
                del book[key]

        # --- window evolution ---
        w = data["window"]
        w["games"] += 1
        key_map = {"win": "wins", "loss": "losses", "draw": "draws"}
        if result in key_map:
            w[key_map[result]] += 1
        fresh_window = {"games": 0, "wins": 0, "losses": 0, "draws": 0}
        if w["games"] >= self.window_size:
            if data["games_total"] < self.freeze_until:
                # 10-game windows are statistically meaningless (stderr ~0.16);
                # until 100 games are logged: count, log, do not evolve.
                log.info("GENE FREEZE: %d/%d games logged - window recorded, "
                         "evolution starts at %d games.", data["games_total"],
                         self.freeze_until, self.freeze_until)
                data["window"] = fresh_window
                self.store.save(data)
                return
            wr = (w["wins"] + 0.5 * w["draws"]) / w["games"]
            snap = data.get("snapshot")
            genes = data["genes"]
            if snap and wr < snap["wr"] - 0.10:
                log.info("DRILL SERGEANT: %.0f%% win rate is worse than the "
                         "snapshot (%.0f%%). Reverting the experiment.", wr * 100,
                         snap["wr"] * 100)
                data["genes"] = dict(snap["genes"])
            elif wr >= 0.55:
                log.info("Good window (%.0f%%). Snapshotting genes.", wr * 100)
                data["snapshot"] = {"wr": wr, "genes": dict(genes)}
            else:
                # explore: nudge style genes while keeping the revert safety net
                before = (genes.get("style_pick_prob"), genes.get("style_eval_slack"))
                genes["style_pick_prob"] = _clamp(
                    genes.get("style_pick_prob", 0.25) + random.choice((-0.05, 0.05)),
                    0.05, 0.35)
                genes["style_eval_slack"] = int(_clamp(
                    genes.get("style_eval_slack", 45) + random.choice((-10, 10)),
                    20, 80))
                log.info("Exploring: style_pick_prob %.2f -> %.2f, slack %s -> %s",
                         before[0], genes["style_pick_prob"], before[1],
                         genes["style_eval_slack"])
            data["window"] = fresh_window

        self.store.save(data)

    # ------------------------------------------------------------------
    # learned opening book lookup
    # ------------------------------------------------------------------

    def get_book_move(self, all_moves: list[str], we_are_white: bool) -> str | None:
        """A follow-up move from a line that has been winning for us."""
        i = len(all_moves)
        if i >= self.book_depth:
            return None
        if (i % 2 == 0) != we_are_white:
            return None  # not our turn pattern
        entries = self.store.load().get("book", {}).get(
            " ".join(all_moves), {}).get("next", {})
        scored = []
        for uci, c in entries.items():
            n = c["w"] + c["l"] + c["d"]
            if n >= self.book_min_samples:
                scored.append(((c["w"] + 0.5 * c["d"]) / n, n, uci))
        if not scored:
            return None
        scored.sort(reverse=True)
        best = scored[0]
        if best[0] < 0.45:
            return None  # cursed line - think for yourself
        return best[2]

    # ------------------------------------------------------------------
    # nemesis rematch caps (the hunter only sends bots here)
    # ------------------------------------------------------------------

    def can_rematch(self, username: str) -> bool:
        """At most 2 rematches per opponent per day - no boosting, no stalking."""
        data = self.store.load()
        today = time.strftime("%Y-%m-%d")
        return data.get("rematches", {}).get(today, {}).get(username, 0) < 2

    def record_rematch(self, username: str) -> None:
        data = self.store.load()
        today = time.strftime("%Y-%m-%d")
        today_counts = data.get("rematches", {}).get(today, {})
        today_counts[username] = today_counts.get(username, 0) + 1
        data["rematches"] = {today: today_counts}  # older days expire
        self.store.save(data)

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def summary(self) -> str:
        data = self.store.load()
        g = dict(DEFAULT_GENES)
        g.update(data.get("genes", {}))
        b = data.get("blunders", {})
        book_moves = sum(
            1 for key in data.get("book", {}).values()
            for c in key.get("next", {}).values()
            if c["w"] + c["l"] + c["d"] >= self.book_min_samples)
        return (f"genes: shield={g['king_shield']:.2f} pawn_adv={g['pawn_adv']:.2f} "
                f"style={g['style_pick_prob']:.2f} | blunders: {b.get('total', 0)} "
                f"(style {b.get('style_pick', 0)}, time {b.get('time_trouble', 0)}, "
                f"endgame {b.get('endgame', 0)}) | book moves: {book_moves} | "
                f"mated {data.get('mate_losses', 0)}x")
