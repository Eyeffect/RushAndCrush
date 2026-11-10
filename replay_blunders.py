"""
Blunder regression suite: replay the ledger against the current engine.

Every blunder the soldier filed in battle (fen + the move he actually
played + the eval drop) becomes a replayed question: does the CURRENT
engine still play it, and how much does it still cost?

Run after any engine change. The still-played rate and the mean
re-played drop are the two numbers to watch; a change that raises
either is suspect regardless of what the Elo match says.

Usage:
  pypy replay_blunders.py                # full report
  pypy replay_blunders.py --nodes 12000  # deeper check
  pypy replay_blunders.py --strict 10    # exit 1 if >10% still played
"""

import argparse
import collections
import json
import os
import sys

import chess
import yaml

from engine import Engine


def load_blunders(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def load_params(cfg_path: str) -> dict:
    """Production-ish persona: config persona merged over learned genes."""
    params = {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        params.update(cfg.get("persona", {}) or {})
    except Exception:
        params.update({"aggression": 0.9, "prox": 1.0, "pawn_adv": 1.0})
    params.pop("resign_enabled", None)
    params.pop("resign_score", None)
    params.pop("resign_after_moves", None)
    return params


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Replay blunder ledger")
    ap.add_argument("--log",
                    default=os.path.join(here, "data", "blunders.jsonl"))
    ap.add_argument("--config", default=os.path.join(here, "config.yml"))
    ap.add_argument("--nodes", type=int, default=8000)
    ap.add_argument("--strict", type=float, default=None,
                    help="exit 1 if still-played rate exceeds this %%")
    args = ap.parse_args()

    blunders = load_blunders(args.log)
    with_fen = [b for b in blunders if b.get("fen") and b.get("uci")]
    print(f"ledger: {len(blunders)} blunders, {len(with_fen)} replayable "
          f"(fen+uci present)")
    if not with_fen:
        print("nothing to replay - the ledger predates fen/uci logging")
        return

    params = load_params(args.config)
    engine = Engine({"max_nodes": args.nodes})

    still_played = 0
    checked = 0
    drops_now: list[int] = []
    by_phase: dict = collections.defaultdict(
        lambda: {"n": 0, "still": 0, "drops": []})
    worst: list[tuple] = []

    for rec in with_fen:
        try:
            board = chess.Board(rec["fen"])
        except ValueError:
            continue
        blunder = chess.Move.from_uci(rec["uci"])
        if blunder not in board.legal_moves:
            continue  # ledger entry from a differently-keyed position
        checked += 1

        keys = {Engine._tt_key(board): 1}
        board_key = Engine._tt_key(board)
        best = engine.search(board.copy(stack=False), 120.0, params,
                             rep_keys=dict(keys))
        if best["move"] is None:
            continue
        # what does the blunder cost NOW? search the reply to it
        after = board.copy(stack=False)
        after.push(blunder)
        reply = engine.search(after, 120.0, params, rep_keys={board_key: 1})
        drop_now = 0
        if reply["score"] is not None:
            # best is side-to-move POV; the reply search is the opponent's
            drop_now = max(0, best["score"] - (-reply["score"]))
        drops_now.append(drop_now)

        played_it = best["move"] == blunder
        if played_it:
            still_played += 1
        ph = by_phase[rec.get("phase", "?")]
        ph["n"] += 1
        ph["drops"].append(drop_now)
        if played_it:
            ph["still"] += 1
        worst.append((drop_now, played_it, rec.get("phase", "?"),
                      rec["fen"].split()[0][:40], rec["uci"],
                      rec.get("drop", 0)))

    if not checked:
        print("no replayable positions (all fens illegal or stale)")
        return

    n = checked
    rate = 100.0 * still_played / n
    mean_drop = sum(drops_now) / n
    print(f"\n=== BLUNDER REPLAY ({n} positions, {args.nodes} nodes/move) ===")
    print(f"still chosen by current engine : {still_played}/{n} "
          f"({rate:.1f}%)")
    print(f"mean cost if played today      : {mean_drop:.0f} cp "
          f"(ledger said {sum(b.get('drop', 0) for b in with_fen) /
                        max(len(with_fen), 1):.0f} cp)")
    print("\nby phase:")
    for phase, st in sorted(by_phase.items()):
        d = sum(st["drops"]) / len(st["drops"])
        print(f"  {phase:<10} n={st['n']:<4} still={st['still']:<3} "
              f"mean drop now={d:.0f} cp")
    print("\nworst 5 (most expensive to still get wrong):")
    for drop, pl, phase, hdr, uci, old in sorted(worst, reverse=True)[:5]:
        flag = "STILL PLAYED" if pl else ""
        print(f"  {drop:>5} cp  {phase:<10} {hdr:<40} {uci}  "
              f"(filed at {old} cp) {flag}")

    if args.strict is not None and rate > args.strict:
        print(f"\nSTRICT FAIL: still-played rate {rate:.1f}% > "
              f"{args.strict}%")
        sys.exit(1)
    print("\nreplay complete")


if __name__ == "__main__":
    main()
