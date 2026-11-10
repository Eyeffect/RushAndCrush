"""
Engine-vs-engine self-play harness: the measuring stick for engine work.

Every strength change must win here before it ships - at live-ratings
noise (~±40 Elo per session), unmeasured tweaks are blind.

Design:
  * Players are spec dicts: {"module": "engine", "params": {...},
    "nodes": 6000} (fixed nodes per move) or {"time": 0.2} (fixed
    seconds). Different MODULES can race each other, so a candidate
    engine can be copied to engine_cand.py and raced against the
    baseline without touching git.
  * Games come in color-swapped PAIRS on a shared seeded random
    opening, which cancels first-move bias.
  * Fixed NODES measure strength-per-node (engine efficiency); fixed
    TIME is for time-management experiments.
  * Draw policy: python-chess outcomes (mate, stalemate, insufficient
    material, threefold, fifty-move) + adjudication at the ply cap by
    material (|diff| >= 300cp decides, else draw).
  * SPRT (logistic approximation, alpha=beta=0.05, bounds elo0=0 /
    elo1=25) stops the match early once the answer is known.

Usage:
  pypy match.py --games 40 --nodes 6000 --workers 8
  pypy match.py --a '{"module":"engine"}' --b '{"module":"engine_cand"}'
  pypy match.py --a '{"module":"engine","nodes":1500}' \
                --b '{"module":"engine","nodes":6000}'
"""

import argparse
import importlib
import json
import math
import multiprocessing
import os
import random
import sys
import time

import chess

from engine import Engine

PIECE_CP = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
            chess.ROOK: 500, chess.QUEEN: 900}
BIG_BUDGET_S = 120.0   # node-mode search budget: the node cap binds first
DRAW_MAT_CP = 300      # material adjudication threshold at the ply cap


# ----------------------------------------------------------------------
# players
# ----------------------------------------------------------------------

def make_player(spec: dict) -> Engine:
    """Fresh Engine from a player spec (fresh TTs per game by design)."""
    mod = importlib.import_module(spec.get("module", "engine"))
    cls = getattr(mod, "Engine")
    return cls({"max_nodes": int(spec.get("nodes", 0) or 0)})


# ----------------------------------------------------------------------
# openings
# ----------------------------------------------------------------------

def gen_opening(seed: int, plies: int) -> str:
    """Deterministic random opening (legal moves only) for one PAIR."""
    rng = random.Random(seed * 9176 + 13)
    b = chess.Board()
    for _ in range(plies):
        b.push(rng.choice(list(b.legal_moves)))
    return b.fen()


def _rep_keys(start_fen: str, uci_moves: list[str]) -> dict:
    """Repetition keys of the game so far (same scheme as game_manager),
    so a winning side avoids shuffling into threefold."""
    b = chess.Board(start_fen)
    cnt = {Engine._tt_key(b): 1}
    for u in uci_moves:
        b.push(chess.Move.from_uci(u))
        k = Engine._tt_key(b)
        cnt[k] = cnt.get(k, 0) + 1
    return cnt


# ----------------------------------------------------------------------
# one game
# ----------------------------------------------------------------------

def _material_cp(board: chess.Board) -> int:
    """White-minus-black material in centipawns."""
    total = 0
    for pt, v in PIECE_CP.items():
        total += v * (len(board.pieces(pt, chess.WHITE))
                      - len(board.pieces(pt, chess.BLACK)))
    return total


def play_game(spec_a: dict, spec_b: dict, opening_fen: str, a_white: bool,
              max_plies: int, clock: dict | None = None) -> dict:
    """Play one game. Returns {'winner': 'A'|'B'|'draw', 'reason', 'plies'}.

    clock (optional) models a real blitz clock:
      {"bank": seconds on the clock, "inc": increment per move,
       "frac": fraction of the remaining clock spendable per move,
       "min_move": floor per move, "max_move": ceiling per move}
    Each side gets its own clock; think time is deducted from it, and a
    side that runs out loses on time. Saved seconds therefore buy
    DEEPER searches later - exactly how the bot spends its clock, and
    the only way a time-management change can be measured.
    """
    eng_a = make_player(spec_a)
    eng_b = make_player(spec_b)
    board = chess.Board(opening_fen)
    uci_moves: list[str] = []
    left = {"A": float(clock["bank"]), "B": float(clock["bank"])} if clock else None
    spent = {"A": 0.0, "B": 0.0}

    while len(uci_moves) < max_plies:
        if board.turn == (chess.WHITE if a_white else chess.BLACK):
            eng, spec, side = eng_a, spec_a, "A"
        else:
            eng, spec, side = eng_b, spec_b, "B"
        params = spec.get("params") or {}
        nodes = int(spec.get("nodes", 0) or 0)
        if clock is not None:
            if left[side] <= 0.0:
                # out of time: the side that cannot move loses
                other = "B" if side == "A" else "A"
                return {"winner": other, "reason": "flag",
                        "plies": len(uci_moves)}
            budget = (left[side] * float(clock["frac"])
                      + float(clock["inc"]) * 0.4)
            budget = max(float(clock["min_move"]),
                         min(float(clock["max_move"]), budget))
            t0 = time.time()
            r = eng.search(board, budget, params,
                           rep_keys=_rep_keys(opening_fen, uci_moves))
            elapsed = time.time() - t0
            left[side] -= elapsed
            spent[side] += elapsed
        elif nodes > 0:
            r = eng.search(board, BIG_BUDGET_S, params,
                           rep_keys=_rep_keys(opening_fen, uci_moves))
        else:
            r = eng.search(board, float(spec.get("time", 0.2)), params,
                           rep_keys=_rep_keys(opening_fen, uci_moves))
        mv = r["move"]
        if mv is None:
            break  # no legal moves; the outcome check below classifies
        board.push(mv)
        uci_moves.append(mv.uci())

    def win(white_won: bool, reason: str) -> dict:
        a_won = (white_won == a_white)
        return {"winner": "A" if a_won else "B", "reason": reason,
                "plies": len(uci_moves), "spent": spent}

    if board.is_checkmate():
        return win(board.turn == chess.BLACK, "mate")
    if board.is_stalemate():
        return {"winner": "draw", "reason": "stalemate", "plies": len(uci_moves),
                "spent": spent}
    if board.is_insufficient_material():
        return {"winner": "draw", "reason": "material", "plies": len(uci_moves),
                "spent": spent}
    if board.is_fifty_moves():
        return {"winner": "draw", "reason": "fifty", "plies": len(uci_moves),
                "spent": spent}
    if board.is_repetition(3):
        return {"winner": "draw", "reason": "repetition", "plies": len(uci_moves),
                "spent": spent}
    # ply-cap adjudication: material decides, otherwise the soldier's
    # favorite outcome - nobody proved anything, nobody gets the point
    mat = _material_cp(board)
    if abs(mat) >= DRAW_MAT_CP:
        return win(mat > 0, "adjudicated")
    return {"winner": "draw", "reason": "ply-cap", "plies": len(uci_moves),
            "spent": spent}


def _play_task(t: tuple) -> dict:
    return play_game(*t)


# ----------------------------------------------------------------------
# statistics
# ----------------------------------------------------------------------

def elo_report(wins: int, losses: int, draws: int) -> tuple[float, float]:
    """(elo_diff, 95% CI half-width) for player A, binomial approximation."""
    n = wins + losses + draws
    s = (wins + 0.5 * draws) / n
    if s <= 0.0:
        return -9999.0, 0.0
    if s >= 1.0:
        return 9999.0, 0.0
    elo = -400.0 * math.log10(1.0 / s - 1.0)
    se_elo = (400.0 / math.log(10.0)) * math.sqrt(s * (1.0 - s) / n) \
        / (s * (1.0 - s))
    return elo, 1.96 * se_elo


def sprt_llr(wins: int, losses: int, draws: int,
             elo0: float = 0.0, elo1: float = 25.0) -> float:
    """Log-likelihood ratio H1 (elo1) vs H0 (elo0), logistic score model."""
    def expected(elo: float) -> float:
        return 1.0 / (1.0 + 10.0 ** (-elo / 400.0))

    s0, s1 = expected(elo0), expected(elo1)
    llr = 0.0
    for r, cnt in ((1.0, wins), (0.5, draws), (0.0, losses)):
        if cnt:
            p1 = (s1 ** r) * ((1.0 - s1) ** (1.0 - r))
            p0 = (s0 ** r) * ((1.0 - s0) ** (1.0 - r))
            llr += cnt * math.log(p1 / p0)
    return llr


def sprt_decision(llr: float, alpha: float = 0.05,
                  beta: float = 0.05) -> str:
    upper = math.log((1.0 - beta) / alpha)     # accept H1: A is better
    lower = math.log(beta / (1.0 - alpha))     # accept H0: no improvement
    if llr >= upper:
        return "H1 accepted (A wins)"
    if llr <= lower:
        return "H0 accepted (no win for A)"
    return "continue"


# ----------------------------------------------------------------------
# match runner
# ----------------------------------------------------------------------

def run_match(spec_a: dict, spec_b: dict, games: int, workers: int,
              random_plies: int, max_plies: int, seed: int,
              verbose: bool = True, clock: dict | None = None) -> dict:
    """Color-swapped pairs on shared openings; returns aggregate stats."""
    if games % 2:
        games += 1
        print(f"note: --games must be even, using {games}")
    pairs = games // 2
    tasks = []
    for i in range(pairs):
        fen = gen_opening(seed + i, random_plies)
        tasks.append((spec_a, spec_b, fen, True, max_plies, clock))
        tasks.append((spec_a, spec_b, fen, False, max_plies, clock))
    random.Random(seed).shuffle(tasks)

    results: list[dict] = []
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(workers) as pool:
        for k, res in enumerate(pool.imap_unordered(_play_task, tasks), 1):
            results.append(res)
            if verbose:
                extra = ""
                if res.get("spent"):
                    extra = (f", A {res['spent']['A']:.1f}s / "
                             f"B {res['spent']['B']:.1f}s")
                print(f"[{k:>3}/{len(tasks)}] {res['winner']:4s} "
                      f"({res['reason']}, {res['plies']} plies{extra})", flush=True)

    a_wins = sum(1 for r in results if r["winner"] == "A")
    b_wins = sum(1 for r in results if r["winner"] == "B")
    draws = sum(1 for r in results if r["winner"] == "draw")
    return {"a_wins": a_wins, "b_wins": b_wins, "draws": draws,
            "results": results}


def parse_player(s: str, params_s: str, default_nodes: int,
                 default_time: float) -> dict:
    """Player spec from CLI. Shorthand 'module:nodes' avoids the
    PowerShell quote-mangling that makes inline JSON fragile; full JSON
    still works if the string starts with '{'."""
    if s.lstrip().startswith("{"):
        spec = json.loads(s)
    else:
        mod, _, nodes = s.partition(":")
        spec = {"module": mod or "engine",
                "nodes": int(nodes) if nodes else 0}
    params: dict = {}
    if params_s:
        for kv in params_s.split(","):
            if not kv:
                continue
            k, _, v = kv.partition("=")
            try:
                params[k.strip()] = float(v)
            except ValueError:
                params[k.strip()] = v
    if params:
        spec["params"] = params
    if not spec.get("nodes"):
        spec["nodes"] = default_nodes
        if not default_nodes:
            spec.setdefault("time", default_time)
    return spec


def main() -> None:
    ap = argparse.ArgumentParser(
        description="RushAndCrush engine-vs-engine self-play harness")
    ap.add_argument("--games", type=int, default=40,
                    help="total games (even; half per color)")
    ap.add_argument("--nodes", type=int, default=6000,
                    help="default node cap per move (0 = use --time)")
    ap.add_argument("--time", type=float, default=0.2,
                    help="seconds per move when nodes=0")
    ap.add_argument("--workers", type=int,
                    default=max(1, (os.cpu_count() or 2)))
    ap.add_argument("--random-plies", type=int, default=6,
                    help="random opening plies per pair")
    ap.add_argument("--max-plies", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--a", default="engine",
                    help="player A: 'module:nodes' shorthand or JSON")
    ap.add_argument("--b", default="engine",
                    help="player B: 'module:nodes' shorthand or JSON")
    ap.add_argument("--a-params", default="",
                    help="player A persona overrides: k=v,k=v")
    ap.add_argument("--b-params", default="",
                    help="player B persona overrides: k=v,k=v")
    ap.add_argument("--sprt", action="store_true",
                    help="early stop on SPRT decision (bounds 0/25 Elo)")
    ap.add_argument("--bank", type=float, default=0.0,
                    help="clock seconds per side (0 = fixed --time per move)")
    ap.add_argument("--inc", type=float, default=0.0,
                    help="clock increment per move (bank mode)")
    ap.add_argument("--frac", type=float, default=0.05,
                    help="fraction of remaining clock spendable per move")
    ap.add_argument("--min-move", type=float, default=0.05,
                    help="minimum think time per move (bank mode)")
    ap.add_argument("--max-move", type=float, default=1.0,
                    help="maximum think time per move (bank mode)")
    args = ap.parse_args()

    spec_a = parse_player(args.a, args.a_params, args.nodes, args.time)
    spec_b = parse_player(args.b, args.b_params, args.nodes, args.time)
    clock = None
    if args.bank > 0:
        clock = {"bank": args.bank, "inc": args.inc, "frac": args.frac,
                 "min_move": args.min_move, "max_move": args.max_move}
        # the clock decides how long a move may think - node caps would
        # contradict it
        for sp in (spec_a, spec_b):
            sp["nodes"] = 0
            sp.pop("time", None)
    print(f"A = {spec_a}", flush=True)
    print(f"B = {spec_b}", flush=True)
    print(f"clock = {clock}", flush=True)

    wins = losses = draws = 0
    t0 = time.time()
    llr_done = False
    decision = ""
    chunk = max(args.workers, 4)

    if args.sprt:
        # SPRT mode: play in chunks, stop when the decision is made
        played = 0
        while played < args.games and not llr_done:
            n = min(chunk * 2, args.games - played)
            stats = run_match(spec_a, spec_b, n, args.workers,
                              args.random_plies, args.max_plies, args.seed + played,
                              verbose=False, clock=clock)
            for r in stats["results"]:
                if r["winner"] == "A":
                    wins += 1
                elif r["winner"] == "B":
                    losses += 1
                else:
                    draws += 1
            played += n
            llr = sprt_llr(wins, losses, draws)
            decision = sprt_decision(llr)
            print(f"[{played:>3} games] A {wins} - {draws}D - {losses} B | "
                  f"LLR {llr:+.2f} -> {decision}", flush=True)
            if decision != "continue":
                llr_done = True
    else:
        stats = run_match(spec_a, spec_b, args.games, args.workers,
                          args.random_plies, args.max_plies, args.seed,
                          clock=clock)
        wins, losses, draws = (stats["a_wins"], stats["b_wins"],
                               stats["draws"])

    n = wins + losses + draws
    elo, ci = elo_report(wins, losses, draws)
    print("\n=== MATCH RESULT ===")
    print(f"games: {n}   A: {wins}W {draws}D {losses}L   "
          f"score {((wins + 0.5 * draws) / n):.3f}")
    print(f"Elo(A vs B): {elo:+.0f} ± {ci:.0f} (95%)")
    if clock:
        ta = sum(r.get("spent", {}).get("A", 0.0) for r in stats["results"])
        tb = sum(r.get("spent", {}).get("B", 0.0) for r in stats["results"])
        flags = sum(1 for r in stats["results"] if r["reason"] == "flag")
        print(f"clock time: A {ta:.0f}s  B {tb:.0f}s  "
              f"(saving {(tb - ta) / max(ta, 0.01) * 100:+.1f}% for A)  "
              f"flags: {flags}")
    if args.sprt:
        print(f"SPRT: LLR {sprt_llr(wins, losses, draws):+.2f} -> {decision}")
    print(f"wall clock: {time.time() - t0:.0f}s")
    sys.exit(0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
