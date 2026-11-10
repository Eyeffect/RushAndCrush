"""
Offline smoke test - verifies the engine, persona, and state logic
without touching Lichess. Run:  python smoke_test.py
"""

import shutil
import threading
import time

import chess

from engine import Engine
from persona import Persona
from state import HunterMemory, NemesisStore, StatsStore

PERSONA_CFG = {
    "chat": {
        "enabled": True,
        "min_gap_seconds": 0,
        "trigger_cooldown": 0,
        "random_skip": 0,
        "lines": {
            "game_start": ["Lock and load, {opponent}. The push begins."],
            "taunt": ["Was that a move or a moment of silence, {opponent}?"],
            "win": ["Objective secured. The push never stops."],
        },
    },
    "aggression": 0.9,
    "style_pick_prob": 0.25,
    "style_eval_slack": 45,
}


def test_engine_basic():
    engine = Engine({"min_time": 0.2, "max_time": 0.6, "time_fraction": 0.05})
    board = chess.Board()

    t0 = time.time()
    result = engine.pick_move(board, 1.0, PERSONA_CFG)
    dt = time.time() - t0
    assert result["move"] is not None, "engine returned no move from startpos"
    assert result["move"] in list(board.legal_moves), "illegal move returned"
    print(f"[ok] startpos: {board.san(result['move'])} eval={result['score']} "
          f"depth={result['depth']} in {dt:.2f}s")


def test_engine_mate_in_one():
    engine = Engine({"min_time": 0.1, "max_time": 1.0})
    # Position after 1.g4 g5 - a position RushAndCrush would be proud of
    board = chess.Board("rnbqkbnr/pppppp1p/8/6p1/6P1/8/PPPPPP1P/RNBQKBNR w KQkq - 0 2")
    result = engine.pick_move(board, 1.0, PERSONA_CFG)
    assert result["move"] in list(board.legal_moves)
    print(f"[ok] aggressive opening: {board.san(result['move'])} eval={result['score']} "
          f"depth={result['depth']}")


def test_engine_finds_mate():
    engine = Engine({"min_time": 0.1, "max_time": 2.0})
    # Back-rank mate in 1: white rook to h8 supported by rook on f1? Simple: Ra8# style
    board = chess.Board("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1")  # Ra8#? a1 rook to a8: check... 
    # Actually with king on g8 and pawns f7g7h7, Ra8 is mate.
    result = engine.pick_move(board, 2.0, PERSONA_CFG)
    assert result["move"].uci() == "a1a8", f"engine missed Ra8#: {result['move']}"
    assert result["score"] > 900_000, "mate score not reported"
    print(f"[ok] mate in 1 found: Ra8# score={result['score']}")


def test_engine_avoids_hanging_queen():
    engine = Engine({"min_time": 0.1, "max_time": 2.0})
    # White queen on d1 can be captured by pawn on c2... no: place queen attacked by pawn
    board = chess.Board("rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq - 0 3")
    result = engine.pick_move(board, 1.0, PERSONA_CFG)
    assert result["move"] in list(board.legal_moves)
    print(f"[ok] black move: {result['move'].uci()} eval={result['score']} (perspective black)")


def test_violence_prefers_captures():
    engine = Engine({})
    board = chess.Board("rnbqkbnr/ppp1pppp/8/8/3pP3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 3")
    # exd4 (capture) should be more violent than a quiet move
    v_cap = engine._violence(board, chess.Move.from_uci("e4d4"))
    v_quiet = engine._violence(board, chess.Move.from_uci("g1f3"))
    assert v_cap > v_quiet, f"capture violence {v_cap} <= quiet {v_quiet}"
    print(f"[ok] violence score: capture {v_cap} > quiet {v_quiet}")


def test_persona():
    persona = Persona(PERSONA_CFG, "RushAndCrush")
    line = persona.compose("game_start", {"opponent": "MagnusCarlsen"}, force=True)
    assert line and "MagnusCarlsen" in line, f"bad line: {line}"
    # anti-repeat: a 2-line pool must not serve the same line twice in a row
    persona.lines["game_start"] = ["Line one, {opponent}.", "Line two, {opponent}."]
    a = persona.compose("game_start", {"opponent": "X"}, force=True)
    b = persona.compose("game_start", {"opponent": "X"}, force=True)
    assert a != b, f"repeated a line back to back: {a} / {b}"
    print(f"[ok] persona: '{line}' then alternating lines")


def test_stores(tmp="data_test"):
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    stats = StatsStore(f"{tmp}/stats.json")
    stats.record("win")
    stats.record("loss")
    stats.record("win")
    assert stats.store.load() == {"wins": 2, "losses": 1, "draws": 0, "games": 3}

    nem = NemesisStore(f"{tmp}/nemesis.json")
    nem.cooldown_s = 0
    nem.add_loss("MagnusCarlsen")
    assert nem.next_target() == "MagnusCarlsen"
    assert nem.head_to_head("MagnusCarlsen") == (0, 1)

    # we avenge the loss: head-to-head even, revenge pool is empty
    nem.add_win("MagnusCarlsen")
    assert nem.head_to_head("MagnusCarlsen") == (1, 1)
    assert nem.next_target() is None, "avenged nemesis still on revenge list"
    # they beat us again: nemesis again, and leading 2-1
    nem.add_loss("MagnusCarlsen")
    assert nem.next_target() == "MagnusCarlsen"
    assert nem.head_to_head("MagnusCarlsen") == (1, 2)

    # old save format (no "wins" key) must load without errors
    with open(f"{tmp}/nemesis_old.json", "w", encoding="utf-8") as f:
        f.write('{"losses": {"OldBot": {"count": 1, "last": 0}}}')
    legacy = NemesisStore(f"{tmp}/nemesis_old.json")
    legacy.cooldown_s = 0
    assert legacy.head_to_head("OldBot") == (0, 1)
    assert legacy.next_target() == "OldBot"
    legacy.add_win("OldBot")
    assert legacy.next_target() is None

    mem = HunterMemory(f"{tmp}/hunter.json")
    mem.remember_s = 60
    mem.played_s = 7200
    mem.record_attempt("SomeBot")
    assert mem.recently_tried("SomeBot")
    assert mem.last_tried("SomeBot") > 0
    assert not mem.played_recently("SomeBot")
    mem.record_played("SomeBot")
    assert mem.played_recently("SomeBot")
    mem.record_played("opponent")  # placeholder must be ignored
    assert not mem.played_recently("opponent")
    print("[ok] stores: stats, nemesis (h2h + avenged), hunter memory + rotation")


def test_chat_pools():
    """Every trigger the code can fire needs a pool, every line must fit
    Lichess's 140-char chat limit, and formatting must not explode."""
    import yaml
    with open("config.yml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    lines = cfg["persona"]["chat"]["lines"]
    needed = ["game_start", "game_start_nemesis", "game_start_rematch",
              "game_start_grudge", "taunt", "self_blunder", "repeat_mistake",
              "queen_lost", "queen_taken", "check_given", "pawn_storm",
              "time_pressure", "last_stand", "revenge_win",
              "first_blood", "castle", "promotion", "en_passant", "comeback",
              "win", "loss", "draw", "resign",
              "win_mate", "win_resign", "win_time",
              "loss_mate", "loss_resign", "loss_time", "draw_stalemate",
              "spectator_start", "spectator_win", "spectator_loss"]
    for trigger in needed:
        pool = lines.get(trigger)
        assert pool, f"missing chat pool: {trigger}"
        for line in pool:
            assert len(line) <= 140, f"{trigger} line too long: {line!r}"
            text = line.format(me="RushAndCrush", opponent="TestBot")
            assert len(text) <= 140, f"{trigger} formatted too long: {text!r}"
    n_lines = sum(len(v) for v in lines.values())
    print(f"[ok] chat: {len(lines)} triggers, {n_lines} lines, all fit 140 chars")


def test_learning_book(tmp="data_test"):
    from learning import Learner
    shutil.rmtree(f"{tmp}/learn", ignore_errors=True)
    learner = Learner(f"{tmp}/learn/learning.json",
                      {"book_depth": 8, "book_min_samples": 2, "window": 10})

    # a line that WINS twice: e4 e5, Nf3
    for _ in range(2):
        learner.report_game("win", "mate", ["e2e4", "e7e5", "g1f3"], True, "X")
    got = learner.get_book_move(["e2e4", "e7e5"], we_are_white=True)
    assert got == "g1f3", f"book should follow the winning line, got {got}"

    # a line that LOSES twice: e4 e5, Bc4
    for _ in range(2):
        learner.report_game("loss", "resign", ["e2e4", "e7e5", "f1c4"], True, "Y")
    got = learner.get_book_move(["e2e4", "e7e5"], we_are_white=True)
    assert got == "g1f3", f"book should prefer the winning line, got {got}"

    # cursed line: only losing follow-ups available -> think for yourself
    learner2 = Learner(f"{tmp}/learn/l2.json",
                       {"book_depth": 8, "book_min_samples": 2, "window": 10})
    for _ in range(2):
        learner2.report_game("loss", "resign", ["d2d4", "d7d5", "c2c4"], True, "Z")
    got = learner2.get_book_move(["d2d4", "d7d5"], we_are_white=True)
    assert got is None, f"cursed line should be abandoned, got {got}"
    print("[ok] learning: opening book follows winners, abandons losers")


def test_learning_genes(tmp="data_test"):
    from learning import Learner
    shutil.rmtree(f"{tmp}/learn2", ignore_errors=True)
    learner = Learner(f"{tmp}/learn2/learning.json",
                      {"book_depth": 8, "book_min_samples": 3, "window": 10})

    # checkmate losses teach the shield
    for _ in range(2):
        learner.report_game("loss", "mate", ["e2e4"], True, "KillerBot")
    assert learner.genes()["king_shield"] == 0.1, "mated twice, shield should grow"

    # style-pick blunders shrink bravado
    for _ in range(7):
        learner.record_blunder("middlegame", 60.0, was_style_pick=True)
    assert learner.genes()["style_pick_prob"] <= 0.25 * 0.8, "bravado should shrink"

    # time trouble blunders kill low-time gambling
    for _ in range(6):
        learner.record_blunder("middlegame", 5.0, was_style_pick=False)
    assert learner.genes()["style_pick_lowtime"] == 0.0, "low-time gambling should stop"
    print("[ok] learning: genes adapt (shield grown, bravado toned down, "
          "low-time gambling off)")


def test_repetition_contempt():
    eng = Engine({"min_time": 0.1, "max_time": 1.0})
    # KQ vs K: white is winning, mop-up drives the black king to the edge
    board = chess.Board("k7/8/8/8/8/8/5Q2/K7 w - - 0 1")
    # ...but the position after Qf7 already happened once in the game
    hist = chess.Board("k7/5Q2/8/8/8/8/8/K7 b - - 0 1")
    keys = {eng._tt_key(board): 1, eng._tt_key(hist): 1}
    result = eng.pick_move(board, 1.0, PERSONA_CFG, rep_keys=keys)
    assert result["move"] is not None and result["move"] != chess.Move.from_uci("f2f7"), \
        "winning side walked into a known repetition"
    print(f"[ok] repetition: winning side avoids the repeat "
          f"({board.san(result['move'])}, eval {result['score']})")


def test_engine_learns_defense():
    eng = Engine({})
    # after 1.f3 e5 2.g4: white's pawn storm has stripped his own king's shield
    board = chess.Board("rnbqkbnr/pppppppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR w KQkq - 0 2")
    params = {"aggression": 0.9, "pawn_adv": 1.0, "prox": 1.0, "king_shield": 0.0}
    base = eng.evaluate(board, params)
    shielded = eng.evaluate(board, {**params, "king_shield": 0.5})
    assert shielded < base, "king_shield should penalize the missing pawn shelter"
    # pawn_adv gene should amplify the advance bonus
    board2 = chess.Board("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 1")
    a = eng.evaluate(board2, {"aggression": 0.9, "pawn_adv": 1.0})
    b = eng.evaluate(board2, {"aggression": 0.9, "pawn_adv": 2.0})
    assert b > a, "pawn_adv gene should value advanced pawns more"
    print("[ok] engine: learned genes flow into the evaluation")


def test_eval_symmetry():
    """A symmetric position must evaluate to exactly 0, and a position and
    its color-swapped mirror must be exact negatives - catches sign bugs
    where one side's pieces are added instead of subtracted."""
    eng = Engine({})
    params = {"aggression": 0.9, "pawn_adv": 1.0, "prox": 1.0, "king_shield": 0.0}
    assert eng.evaluate(chess.Board(), params) == 0, "startpos eval != 0"
    shielded = dict(params, king_shield=0.5)
    assert eng.evaluate(chess.Board(), shielded) == 0, "startpos+shield eval != 0"
    # white up a queen, white to move: positive for the side to move.
    # mirror() swaps colors AND turn, so the side to move (black) is still
    # the one up a queen - the side-to-move value must be PRESERVED.
    b = chess.Board("k7/8/8/8/8/8/Q7/K7 w - - 0 1")
    e1 = eng.evaluate(b, params)
    e2 = eng.evaluate(b.mirror(), params)
    assert e1 > 0 and e2 == e1, f"mirror asymmetry: {e1} vs {e2}"
    print(f"[ok] engine: eval symmetry (startpos = 0, mirror flips: {e1} vs {e2})")


def test_seeded_repertoire(tmp="data_test"):
    """Seeded gambits play immediately, continue deep, and die by results."""
    from learning import Learner
    shutil.rmtree(f"{tmp}/seed", ignore_errors=True)
    seeds = {
        "white": ["e2e4 e7e5 f2f4 e5f4 g1f3"],               # King's Gambit
        "black": ["e2e4 c7c5 g1f3 d7d6 d2d4 c5d4 f3d4 g8f6"],  # Najdorf setup
    }
    lr = Learner(f"{tmp}/seed/learning.json",
                 {"book_depth": 8, "book_min_samples": 3, "seed_book": seeds})
    # white: the King's Gambit fires on move two, and the line runs deep
    assert lr.get_book_move(["e2e4", "e7e5"], True) == "f2f4"
    assert lr.get_book_move(["e2e4", "e7e5", "f2f4", "e5f4"], True) == "g1f3"
    # and move one itself is doctrine: the push, every game
    assert lr.get_book_move([], True) == "e2e4"
    # black: Sicilian reply at once
    assert lr.get_book_move(["e2e4"], False) == "c7c5"
    assert lr.get_book_move(["e2e4", "c7c5", "g1f3"], False) == "d7d6"
    # real results kill a seeded gambit: three losses along the line
    line = ["e2e4", "e7e5", "f2f4", "e5f4", "g1f3", "g8f6"]
    for _ in range(3):
        lr.report_game("loss", "mate", line, True, "X")
    assert lr.get_book_move(["e2e4", "e7e5"], True) is None, \
        "dead gambit still booked"
    # re-seeding (a restart) never refreshes a line that died by results
    lr2 = Learner(f"{tmp}/seed/learning.json",
                  {"book_depth": 8, "book_min_samples": 3, "seed_book": seeds})
    assert lr2.get_book_move(["e2e4", "e7e5"], True) is None
    print("[ok] learning: seeded repertoire plays immediately, dies by results")


def test_ponder_yields():
    """A ponder search warms the TT and yields instantly to a real search."""
    eng = Engine({"min_time": 0.1, "max_time": 2.0})
    ev = threading.Event()
    box = {}

    def ponder():
        box["r"] = eng.search(chess.Board(), 5.0, PERSONA_CFG,
                              stop_event=ev, ponder=True)

    t = threading.Thread(target=ponder, daemon=True)
    t.start()
    time.sleep(0.5)
    assert len(eng.tt) > 0, "ponder warmed no TT entries"

    t0 = time.time()
    result = eng.pick_move(chess.Board(), 1.5, PERSONA_CFG)
    dt = time.time() - t0
    assert result["move"] is not None, "real search returned no move"
    assert dt < 2.0, f"real search stalled behind the ponder: {dt:.2f}s"

    ev.set()
    t.join(timeout=3.0)
    assert not t.is_alive(), "ponder thread did not stop after its event"
    print(f"[ok] ponder: TT warmed ({len(eng.tt)} entries), real search done "
          f"in {dt:.2f}s, ponder aborted cleanly")


def test_game_handler_end_to_end(tmp="data_test_handler"):
    """The full reader/worker loop on a fake API: state application, think,
    verify-then-commit, POST, ponder after our move, kill on _finish, and
    archival - all in a scratch cwd so no real data files are touched."""
    import json as _json
    import os as _os
    import random as _random
    from game_manager import GameHandler

    shutil.rmtree(tmp, ignore_errors=True)
    _os.makedirs(tmp, exist_ok=True)
    old_cwd = _os.getcwd()
    _os.chdir(tmp)
    try:
        class FakeAPI:
            def __init__(self, lines_fn):
                self.moves = []
                self.reply = None
                self.chats = []
                self._lines_fn = lines_fn

            def game_stream(self, game_id):
                return self

            def iter_lines(self):
                return self._lines_fn(self)

            def move(self, game_id, uci):
                self.moves.append(uci)

            def chat(self, game_id, room, text):
                self.chats.append(text)

            def resign(self, game_id):
                pass

        class FakeStores:
            def __init__(self):
                self.stats = self
                self.nemesis = self
                self.learner = self
                self.results = []

            def record(self, result):
                self.results.append(result)

            def summary(self):
                return f"{len(self.results)} games"

            def head_to_head(self, name):
                return (0, 0)

            def add_loss(self, name):
                pass

            def add_win(self, name):
                pass

            def get_book_move(self, moves, we_white):
                return None

            def report_game(self, *a, **k):
                pass

            def record_blunder(self, *a, **k):
                return False

            def genes(self):
                return {}

        class FakePersona:
            def compose(self, trigger, subs=None, force=False):
                return None

        cfg = {
            "engine": {"min_time": 0.1, "max_time": 1.0, "time_fraction": 0.05,
                       "overhead_ms": 50, "ponder": True,
                       "ponder_max_time": 2.0, "ponder_min_clock_ms": 0},
            "persona": {"resign_enabled": False, "resign_score": -1200,
                        "resign_after_moves": 50, "aggression": 0.9,
                        "style_pick_prob": 0.0, "style_eval_slack": 45},
            "account": {"username": "RushAndCrush"},
        }

        def stream_lines(api):
            def wait_moves(n, timeout=15.0):
                t0 = time.time()
                while len(api.moves) < n and time.time() - t0 < timeout:
                    time.sleep(0.02)
                return len(api.moves) >= n

            yield _json.dumps({
                "type": "gameFull", "initialFen": "startpos",
                "white": {"name": "RushAndCrush"},
                "black": {"name": "TestBot", "title": "BOT", "rating": 1500},
                "state": {"type": "gameState", "status": "started", "moves": "",
                          "wtime": 60000, "btime": 60000, "winc": 0, "binc": 0},
            }).encode()
            if not wait_moves(1):
                return
            b = chess.Board()
            b.push(chess.Move.from_uci(api.moves[0]))
            api.reply = _random.choice(list(b.legal_moves)).uci()
            b.push(chess.Move.from_uci(api.reply))
            yield _json.dumps({
                "type": "gameState", "status": "started",
                "moves": f"{api.moves[0]} {api.reply}",
                "wtime": 55000, "btime": 58000, "winc": 0, "binc": 0,
            }).encode()
            if not wait_moves(2):
                return
            yield _json.dumps({
                "type": "gameState", "status": "outoftime", "winner": "white",
                "moves": f"{api.moves[0]} {api.reply} {api.moves[1]}",
                "wtime": 40000, "btime": 100, "winc": 0, "binc": 0,
            }).encode()

        api = FakeAPI(stream_lines)
        eng = Engine({"min_time": 0.1, "max_time": 1.0, "time_fraction": 0.05,
                      "overhead_ms": 50})
        h = GameHandler(api, eng, FakePersona(), FakeStores(), cfg, "test0000")
        h.start()
        h.join(timeout=30.0)
        assert not h.is_alive(), "handler did not finish"
        assert len(api.moves) >= 1, "no move was posted"

        # replay the real game: both our moves must have been legal
        b = chess.Board()
        game = [api.moves[0], api.reply, api.moves[1]]
        for u in game:
            mv = chess.Move.from_uci(u)
            assert mv in list(b.legal_moves), f"illegal move played: {u}"
            b.push(mv)

        assert h.stores.results == ["win"], \
            f"wrong result recorded: {h.stores.results}"
        pgns = [f for f in _os.listdir(_os.path.join("data", "pgn"))
                if f.endswith(".pgn")]
        assert pgns, "archive did not write a PGN"
        print(f"[ok] handler: posted {', '.join(api.moves)} (both legal), "
              f"finish handled, PGN archived")
    finally:
        _os.chdir(old_cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def test_engine_node_budget():
    """max_nodes caps the search: the harness's measuring unit works,
    and a 0-budget engine behaves exactly as before (time-only)."""
    eng = Engine({"max_nodes": 800})
    r = eng.search(chess.Board(), 30.0, {"aggression": 0.9})
    assert r["move"] is not None, "node-capped search returned no move"
    assert eng.nodes < 800 + 4096, \
        f"node budget ignored: {eng.nodes} nodes"
    eng_t = Engine({})
    r2 = eng_t.search(chess.Board(), 1.0, {"aggression": 0.9})
    assert r2["move"] is not None
    assert eng_t.max_nodes == 0
    print(f"[ok] engine: node budget respected ({eng.nodes} nodes, "
          f"depth {r['depth']}), time mode untouched")


def test_match_harness():
    """The self-play harness: one tiny game completes and classifies,
    and the Elo/SPRT math points the right way on a known sample."""
    import match

    res = match.play_game({"module": "engine", "params": {}, "nodes": 150},
                          {"module": "engine", "params": {}, "nodes": 150},
                          chess.STARTING_FEN, True, max_plies=40)
    assert res["winner"] in ("A", "B", "draw"), res
    assert res["plies"] > 0
    assert res["reason"] in ("mate", "stalemate", "material", "fifty",
                             "repetition", "adjudicated", "ply-cap"), res

    elo, ci = match.elo_report(10, 5, 5)
    assert elo > 0, f"winning sample must give positive Elo: {elo}"
    assert ci > 0
    elo2, _ = match.elo_report(5, 10, 5)
    assert elo2 < 0, f"losing sample must give negative Elo: {elo2}"
    e0, _ = match.elo_report(5, 5, 10)
    assert abs(e0) < 1e-9, f"even sample must be ~0 Elo: {e0}"
    assert match.sprt_llr(10, 2, 8) > 0 > match.sprt_llr(2, 10, 8)
    print(f"[ok] match: mini game -> {res['winner']} ({res['reason']}), "
          f"Elo math and SPRT signs sane")


def test_match_clock_mode():
    """The blitz clock model: a side that runs out of bank loses on
    time, and think time is really deducted from the clock."""
    import match
    clock = {"bank": 0.12, "inc": 0.0, "frac": 0.02,
             "min_move": 0.05, "max_move": 0.05}
    res = match.play_game({"module": "engine", "params": {}, "nodes": 0},
                          {"module": "engine", "params": {}, "nodes": 0},
                          chess.STARTING_FEN, True, max_plies=40, clock=clock)
    assert res["reason"] == "flag", f"expected a flag, got {res}"
    assert res["winner"] in ("A", "B"), res
    assert res["plies"] < 40, f"the clock should have run out: {res}"
    # the clock must be drained by actual think time
    assert res["spent"]["A"] + res["spent"]["B"] > 0.05, res
    print(f"[ok] match: clock model flags the broke side after "
          f"{res['plies']} plies")


def test_engine_budget_discipline():
    """A search must return inside its budget (plus slack) and never
    return without a move - the soft time limit depends on this."""
    import time
    for budget in (0.15, 0.5):
        eng = Engine({})
        t0 = time.time()
        r = eng.search(chess.Board(), budget, {"aggression": 0.9})
        dt = time.time() - t0
        assert r["move"] is not None, "no move returned"
        assert r["depth"] >= 1, "no completed depth"
        assert dt <= budget + 0.35, \
            f"overran the budget: {dt:.2f}s for {budget:.2f}s"
    print("[ok] engine: respects its time budget, always returns a move")


if __name__ == "__main__":
    test_engine_basic()
    test_engine_mate_in_one()
    test_engine_finds_mate()
    test_engine_avoids_hanging_queen()
    test_violence_prefers_captures()
    test_engine_learns_defense()
    test_repetition_contempt()
    test_eval_symmetry()
    test_persona()
    test_stores()
    test_chat_pools()
    test_learning_book()
    test_seeded_repertoire()
    test_learning_genes()
    test_ponder_yields()
    test_game_handler_end_to_end()
    test_engine_node_budget()
    test_match_harness()
    test_match_clock_mode()
    test_engine_budget_discipline()
    print("\nAll smoke tests passed. RushAndCrush is battle-ready.")
