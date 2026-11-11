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
  * Opening book - keyed by POSITION (zobrist hash), not move prefix, so
    transpositions pool their samples. Results are weighted by opponent
    strength; beyond the core depth a move needs a bigger sample.
  * Blunder ledger - every mistake filed by phase / cause, and the
    lessons derived from them
  * Window evolution - every N games, score the window as a z-statistic
    of (result - expected) against the rating gap: improving -> snapshot
    genes; declining -> revert the experiment; in between -> explore.
    Without ratings, falls back to raw win rate.
"""

import json
import logging
import math
import os
import random
import time

import chess
from chess.polyglot import zobrist_hash

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
        self.book_deep_plies = int(cfg.get("book_deep_plies", 16))
        self.book_deep_min_samples = int(
            cfg.get("book_deep_min_samples", 5))
        self.window_size = int(cfg.get("window", 10))
        self.freeze_until = int(cfg.get("freeze_until_games", 100))
        self.seed_cfg = cfg.get("seed_book", {}) or {}
        self.blunder_log = os.path.join(
            os.path.dirname(os.path.abspath(path)), "blunders.jsonl")
        self.store = JsonStore(path, default=self._empty())
        data = self.store.load()
        if "genes" not in data:  # first boot
            self.store.save(data)
        self._migrate_book()
        self._seed_book()

    @staticmethod
    def _pos_key(board: chess.Board) -> str:
        """The book's key for a position: zobrist hash, 16 hex digits."""
        return format(zobrist_hash(board), "016x")

    @staticmethod
    def _is_pos_key(key: str) -> bool:
        return len(key) == 16 and all(c in "0123456789abcdef" for c in key)

    @staticmethod
    def _ensure_weighted(slot: dict) -> None:
        """Give an old raw w/l/d slot its weight-1.0 equivalent, so new
        strength-weighted results mix in without biasing the record."""
        if "wgt" not in slot:
            n = slot["w"] + slot["l"] + slot["d"]
            slot["pts"] = slot["w"] + 0.5 * slot["d"]
            slot["wgt"] = float(n)

    def _migrate_book(self) -> None:
        """One-time re-key: move-prefix keys ("e2e4 e7e5") become position
        keys (zobrist, 16 hex), so transposed move orders share their
        samples. Old keys are replayed on a board and merged; unreadable
        ones are dropped, not fatal."""
        data = self.store.load()
        book = data.get("book", {})
        if not book or all(self._is_pos_key(k) for k in book):
            return
        new_book: dict = {}
        moved = 0
        for key, entry in book.items():
            if self._is_pos_key(key):
                new_book.setdefault(key, {}).update(entry)
                continue
            board = chess.Board()
            try:
                for uci in key.split():
                    board.push_uci(uci)
            except ValueError:
                log.warning("BOOK MIGRATE: dropping unreadable key %r", key)
                continue
            dest = new_book.setdefault(self._pos_key(board), {})
            for uci, slot in entry.get("next", {}).items():
                if uci in dest:
                    for f in ("w", "l", "d"):
                        dest[uci][f] = dest[uci].get(f, 0) + slot.get(f, 0)
                else:
                    dest[uci] = slot
                moved += 1
        data["book"] = new_book
        self.store.save(data)
        log.info("BOOK MIGRATED: %d move-prefix entries re-keyed by "
                 "position - transpositions now share samples.", moved)

    def _seed_book(self) -> None:
        """Load the doctrine: aggressive openings pre-graded so they play
        immediately, with real results free to demote and kill them.

        Each seed line is a full variation in UCI moves; we file OUR turns
        (by actual board turn, so the line must be legal) with a 2W/1L
        record - exactly book_min_samples, so the move is trusted at once.
        Seeds never overwrite real experience: if the position was ever
        played, the learned counts stand."""
        if not self.seed_cfg:
            return
        data = self.store.load()
        book = data.setdefault("book", {})
        added = 0
        for color, lines in self.seed_cfg.items():
            our_turn = chess.WHITE if color == "white" else chess.BLACK
            if not isinstance(lines, list):
                continue
            for line in lines:
                board = chess.Board()
                try:
                    for i, uci in enumerate(
                            str(line).split()[:self.book_depth]):
                        if board.turn == our_turn and uci not in book.get(
                                self._pos_key(board), {}):
                            book.setdefault(self._pos_key(board), {})[uci] = {
                                "w": 2, "l": 1, "d": 0}
                            added += 1
                        board.push_uci(uci)
                except ValueError:
                    log.warning("REPERTOIRE: seed line is not legal, "
                                "skipping: %r", line)
        if added:
            self.store.save(data)
            log.info("REPERTOIRE: %d aggressive opening moves seeded into "
                     "the book (results may kill them, as with any line).",
                     added)

    @staticmethod
    def _empty() -> dict:
        return {
            "genes": dict(DEFAULT_GENES),
            "window": {"games": 0, "wins": 0, "losses": 0, "draws": 0,
                       "score": 0.0, "expected": 0.0, "var": 0.0, "rated": 0},
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
                    we_are_white: bool, opponent: str,
                    opp_rating: int | None = None,
                    my_rating: int | None = None) -> None:
        data = self.store.load()
        data["games_total"] = data.get("games_total", 0) + 1

        rated = (isinstance(my_rating, int) and my_rating > 0
                 and isinstance(opp_rating, int) and opp_rating > 0)
        pts_val = {"win": 1.0, "draw": 0.5}.get(result, 0.0)

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
        # Keyed by the position BEFORE our move (zobrist), so transposed
        # move orders pool their samples. Each result is weighted by
        # opponent strength: beating a 2000 bot teaches more than
        # beating a straw man.
        weight = 1.0
        if rated:
            weight = 1.0 + max(0, opp_rating - my_rating) / 800.0
        book = data["book"]
        board = chess.Board()
        our_turn = chess.WHITE if we_are_white else chess.BLACK
        for uci in all_moves[: self.book_deep_plies]:
            if board.turn == our_turn:
                slot = book.setdefault(self._pos_key(board), {}).setdefault(
                    uci, {"w": 0, "l": 0, "d": 0})
                self._ensure_weighted(slot)
                if result == "win":
                    slot["w"] += 1
                elif result == "loss":
                    slot["l"] += 1
                else:
                    slot["d"] += 1
                slot["pts"] = slot.get("pts", 0.0) + pts_val * weight
                slot["wgt"] = slot.get("wgt", 0.0) + weight
            try:
                board.push_uci(uci)
            except ValueError:
                break  # corrupted move list - keep what we already filed
        # prune lines with no data (keep everything with at least one game;
        # get_book_move filters by reliability)
        for key in list(book):
            slots = book[key]
            for uci in list(slots):
                c = slots[uci]
                if c["w"] + c["l"] + c["d"] < 1:
                    del slots[uci]
            if not slots:
                del book[key]

        # --- window evolution ---
        # #6: the fitness signal is a z-score of sum(result - expected),
        # with the expected score from the rating gap. Raw win rate on a
        # 10-game window has a stderr of ~0.16, so the old fixed 0.10
        # threshold fired on noise; a z-score knows what it doesn't know.
        w = data["window"]
        w["games"] += 1
        key_map = {"win": "wins", "loss": "losses", "draw": "draws"}
        if result in key_map:
            w[key_map[result]] += 1
        if rated:
            expected = 1.0 / (1.0 + 10.0 ** ((opp_rating - my_rating) / 400.0))
            w["score"] = w.get("score", 0.0) + pts_val
            w["expected"] = w.get("expected", 0.0) + expected
            w["var"] = w.get("var", 0.0) + expected * (1.0 - expected)
            w["rated"] = w.get("rated", 0) + 1
        fresh_window = {"games": 0, "wins": 0, "losses": 0, "draws": 0,
                        "score": 0.0, "expected": 0.0, "var": 0.0, "rated": 0}
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
            # trust the z-signal only with enough rated games and real
            # variance behind it; otherwise fall back to raw win rate
            if (w.get("rated", 0) >= max(1, self.window_size // 2)
                    and w.get("var", 0.0) >= 0.5):
                z = ((w.get("score", 0.0) - w.get("expected", 0.0))
                     / math.sqrt(w["var"]))
                revert = False
                if snap:
                    # the snapshot is the reference the experiment answers
                    # to; legacy wr-only snapshots get the absolute test
                    revert = z <= (snap["z"] - 1.5 if "z" in snap else -1.5)
                if revert:
                    log.info("DRILL SERGEANT: z=%.2f is 1.5+ sigma below the "
                             "snapshot (z=%.2f). Reverting the experiment.",
                             z, snap["z"])
                    data["genes"] = dict(snap["genes"])
                elif z >= 1.0:
                    log.info("Outperforming the field (z=%.2f). Snapshotting "
                             "genes.", z)
                    data["snapshot"] = {"z": z, "wr": wr, "genes": dict(genes)}
                else:
                    # explore: nudge style genes while keeping the revert
                    # safety net
                    before = (genes.get("style_pick_prob"),
                              genes.get("style_eval_slack"))
                    genes["style_pick_prob"] = _clamp(
                        genes.get("style_pick_prob", 0.25)
                        + random.choice((-0.05, 0.05)), 0.05, 0.35)
                    genes["style_eval_slack"] = int(_clamp(
                        genes.get("style_eval_slack", 45)
                        + random.choice((-10, 10)), 20, 80))
                    log.info("Exploring (z=%.2f): style_pick_prob %.2f -> "
                             "%.2f, slack %s -> %s", z, before[0],
                             genes["style_pick_prob"], before[1],
                             genes["style_eval_slack"])
            elif snap and wr < snap["wr"] - 0.10:
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
        """A follow-up move from a line that has been good for us.

        Positions are keyed by zobrist hash, so transposed move orders
        share samples. Past the core book_depth a move needs a bigger
        sample (book_deep_min_samples) before we trust it with the game."""
        if len(all_moves) >= self.book_deep_plies:
            return None
        board = chess.Board()
        try:
            for uci in all_moves:
                board.push_uci(uci)
        except ValueError:
            return None
        our_turn = chess.WHITE if we_are_white else chess.BLACK
        if board.turn != our_turn:
            return None  # not our turn
        entries = self.store.load().get("book", {}).get(
            self._pos_key(board), {})
        min_n = (self.book_deep_min_samples if len(all_moves) >= self.book_depth
                 else self.book_min_samples)
        scored = []
        for uci, c in entries.items():
            n = c["w"] + c["l"] + c["d"]
            if n < min_n:
                continue
            if c.get("wgt"):
                sc = c["pts"] / c["wgt"]
            else:
                sc = (c["w"] + 0.5 * c["d"]) / n  # raw legacy slot
            scored.append((sc, n, uci))
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
            1 for entry in data.get("book", {}).values()
            for c in (entry["next"] if "next" in entry else entry).values()
            if c["w"] + c["l"] + c["d"] >= self.book_min_samples)
        return (f"genes: shield={g['king_shield']:.2f} pawn_adv={g['pawn_adv']:.2f} "
                f"style={g['style_pick_prob']:.2f} | blunders: {b.get('total', 0)} "
                f"(style {b.get('style_pick', 0)}, time {b.get('time_trouble', 0)}, "
                f"endgame {b.get('endgame', 0)}) | book moves: {book_moves} | "
                f"mated {data.get('mate_losses', 0)}x")
