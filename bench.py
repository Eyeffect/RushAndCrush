"""Quick benchmark: search depth at blitz-like budgets."""
import time

import chess

from engine import Engine

eng = Engine({"min_time": 0.2, "max_time": 5.0})
b = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4")
persona = {"aggression": 0.9, "style_pick_prob": 0.25, "style_eval_slack": 45}

for budget in (1.0, 3.0):
    t0 = time.time()
    r = eng.pick_move(b, budget, persona)
    dt = time.time() - t0
    print(f"budget={budget}s -> {b.san(r['move'])} depth={r['depth']} "
          f"eval={r['score']} time={dt:.1f}s nodes={eng.nodes:,}")
