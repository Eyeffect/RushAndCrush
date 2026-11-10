"""PyPy warmup bench: repeated searches in ONE process so the JIT heats up.
CPython nps should be flat across rounds; PyPy should climb and settle."""
import time

import chess

from engine import Engine

eng = Engine({"min_time": 0.2, "max_time": 5.0})
persona = {"aggression": 0.9, "style_pick_prob": 0.25, "style_eval_slack": 45}
boards = [
    chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4"),
    chess.Board("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"),
    chess.Board("r4rk1/1pp1qppp/p1np1n2/2b1p1B1/2B1P1b1/P1NP1N2/1PP1QPPP/R4RK1 w - - 0 10"),
]

for rnd in range(1, 7):
    t0 = time.time()
    nodes = 0
    for b in boards:
        r = eng.pick_move(b, 2.0, persona)
        nodes += eng.nodes
    dt = time.time() - t0
    print(f"round {rnd}: {nodes:,} nodes in {dt:.1f}s = {nodes / dt:,.0f} nps")
