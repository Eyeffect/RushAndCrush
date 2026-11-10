"""
RushAndCrush - a homemade chess engine with a soldier's heart.

No Stockfish. No neural networks. Just alpha-beta search, piece-square
tables, and an evaluation that believes the only good piece is an
advancing piece.

Playing style:
  * pawns dream of the enemy back rank (advance bonuses)
  * every piece is graded on how close it gets to the enemy king
  * our own king safety is not evaluated - we don't defend
  * the "style layer" occasionally swaps the best move for the most
    violent move within a small evaluation budget

All persona bonuses are soft-capped (~1.5 pawns per piece) so the soldier
prefers attacking among equal options but never trades a piece for a
pawn storm that "looks even".
"""

import math
import random
import threading
import time
from collections import defaultdict

import chess

MATE = 1_000_000
MATE_BOUND = MATE - 10_000  # scores beyond this are mate-in-N; stored ply-adjusted
INF = 10_000_000
TT_MAX = 500_000  # transposition table entries before it is pensioned off
CAP = 150  # soft cap (cp) on per-piece persona bonus: material + CAP max

PIECE_VALUE = {
    chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
    chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0,
}

# ---------------------------------------------------------------------------
# PeSTO piece-square tables (Rofchade, via CPW) - the proven baseline.
# MG/EG pairs, tapered by game phase in evaluate(). NOTE: these raw tables
# are in CPW's printed order - a8 first (diagram order, row 1 = rank 7).
# The a1=0 flip for python-chess happens once, inside _build_psq below.
# ---------------------------------------------------------------------------

MG_VALUE = {chess.PAWN: 82, chess.KNIGHT: 337, chess.BISHOP: 365,
            chess.ROOK: 477, chess.QUEEN: 1025, chess.KING: 0}
EG_VALUE = {chess.PAWN: 94, chess.KNIGHT: 281, chess.BISHOP: 297,
            chess.ROOK: 512, chess.QUEEN: 936, chess.KING: 0}

PAWN_MG = [
      0,   0,   0,   0,   0,   0,  0,   0,
     98, 134,  61,  95,  68, 126, 34, -11,
     -6,   7,  26,  31,  65,  56, 25, -20,
    -14,  13,   6,  21,  23,  12, 17, -23,
    -27,  -2,  -5,  12,  17,   6, 10, -25,
    -26,  -4,  -4, -10,   3,   3, 33, -12,
    -35,  -1, -20, -23, -15,  24, 38, -22,
      0,   0,   0,   0,   0,   0,  0,   0,
]
PAWN_EG = [
      0,   0,   0,   0,   0,   0,   0,   0,
    178, 173, 158, 134, 147, 132, 165, 187,
     94, 100,  85,  67,  56,  53,  82,  84,
     32,  24,  13,   5,  -2,   4,  17,  17,
     13,   9,  -3,  -7,  -7,  -8,   3,  -1,
      4,   7,  -6,   1,   0,  -5,  -1,  -8,
     13,   8,   8,  10,  13,   0,   2,  -7,
      0,   0,   0,   0,   0,   0,   0,   0,
]
KNIGHT_MG = [
    -167, -89, -34, -49,  61, -97, -15, -107,
     -73, -41,  72,  36,  23,  62,   7,  -17,
     -47,  60,  37,  65,  84, 129,  73,   44,
      -9,  17,  19,  53,  37,  69,  18,   22,
     -13,   4,  16,  13,  28,  19,  21,   -8,
     -23,  -9,  12,  10,  19,  17,  25,  -16,
     -29, -53, -12,  -3,  -1,  18, -14,  -19,
    -105, -21, -58, -33, -17, -28, -19,  -23,
]
KNIGHT_EG = [
    -58, -38, -13, -28, -31, -27, -63, -99,
    -25,  -8, -25,  -2,  -9, -25, -24, -52,
    -24, -20,  10,   9,  -1,  -9, -19, -41,
    -17,   3,  22,  22,  22,  11,   8, -18,
    -18,  -6,  16,  25,  16,  17,   4, -18,
    -23,  -3,  -1,  15,  10,  -3, -20, -22,
    -42, -20, -10,  -5,  -2, -20, -23, -44,
    -29, -51, -23, -15, -22, -18, -50, -64,
]
BISHOP_MG = [
    -29,   4, -82, -37, -25, -42,   7,  -8,
    -26,  16, -18, -13,  30,  59,  18, -47,
    -16,  37,  43,  40,  35,  50,  37,  -2,
     -4,   5,  19,  50,  37,  37,   7,  -2,
     -6,  13,  13,  26,  34,  12,  10,   4,
      0,  15,  15,  15,  14,  27,  18,  10,
      4,  15,  16,   0,   7,  21,  33,   1,
    -33,  -3, -14, -21, -13, -12, -39, -21,
]
BISHOP_EG = [
    -14, -21, -11,  -8, -7,  -9, -17, -24,
     -8,  -4,   7, -12, -3, -13,  -4, -14,
      2,  -8,   0,  -1, -2,   6,   0,   4,
     -3,   9,  12,   9, 14,  10,   3,   2,
     -6,   3,  13,  19,  7,  10,  -3,  -9,
    -12,  -3,   8,  10, 13,   3,  -7, -15,
    -14, -18,  -7,  -1,  4,  -9, -15, -27,
    -23,  -9, -23,  -5, -9, -16,  -5, -17,
]
ROOK_MG = [
     32,  42,  32,  51, 63,  9,  31,  43,
     27,  32,  58,  62, 80, 67,  26,  44,
     -5,  19,  26,  36, 17, 45,  61,  16,
    -24, -11,   7,  26, 24, 35,  -8, -20,
    -36, -26, -12,  -1,  9, -7,   6, -23,
    -45, -25, -16, -17,  3,  0,  -5, -33,
    -44, -16, -20,  -9, -1, 11,  -6, -71,
    -19, -13,   1,  17, 16,  7, -37, -26,
]
ROOK_EG = [
    13, 10, 18, 15, 12,  12,   8,   5,
    11, 13, 13, 11, -3,   3,   8,   3,
     7,  7,  7,  5,  4,  -3,  -5,  -3,
     4,  3, 13,  1,  2,   1,  -1,   2,
     3,  5,  8,  4, -5,  -6,  -8, -11,
    -4,  0, -5, -1, -7, -12,  -8, -16,
    -6, -6,  0,  2, -9,  -9, -11,  -3,
    -9,  2,  3, -1, -5, -13,   4, -20,
]
QUEEN_MG = [
    -28,   0,  29,  12,  59,  44,  43,  45,
    -24, -39,  -5,   1, -16,  57,  28,  54,
    -13, -17,   7,   8,  29,  56,  47,  57,
    -27, -27, -16, -16,  -1,  17,  -2,   1,
     -9, -26,  -9, -10,  -2,  -4,   3,  -3,
    -14,   2, -11,  -2,  -5,   2,  14,   5,
    -35,  -8,  11,   2,   8,  15,  -3,   1,
     -1, -18,  -9,  10, -15, -25, -31, -50,
]
QUEEN_EG = [
     -9,  22,  22,  27,  27,  19,  10,  20,
    -17,  20,  32,  41,  58,  25,  30,   0,
    -20,   6,   9,  49,  47,  35,  19,   9,
      3,  22,  24,  45,  57,  40,  57,  36,
    -18,  28,  19,  47,  31,  34,  39,  23,
    -16, -27,  15,   6,   9,  17,  10,   5,
    -22, -23, -30, -16, -16, -23, -36, -32,
    -33, -28, -22, -43,  -5, -32, -20, -41,
]
KING_MG = [
    -65,  23,  16, -15, -56, -34,   2,  13,
     29,  -1, -20,  -7,  -8,  -4, -38, -29,
     -9,  24,   2, -16, -20,   6,  22, -22,
    -17, -20, -12, -27, -30, -25, -14, -36,
    -49,  -1, -27, -39, -46, -44, -33, -51,
    -14, -14, -22, -46, -44, -30, -15, -27,
      1,   7,  -8, -64, -43, -16,   9,   8,
    -15,  36,  12, -54,   8, -28,  24,  14,
]
KING_EG = [
    -74, -35, -18, -18, -11,  15,   4, -17,
    -12,  17,  14,  17,  17,  38,  23,  11,
     10,  17,  23,  15,  20,  45,  44,  13,
     -8,  22,  24,  27,  26,  33,  26,   3,
    -18,  -4,  21,  24,  27,  23,   9, -11,
    -19,  -3,  11,  21,  23,  16,   7,  -9,
    -27, -11,   4,  13,  14,   4,  -5, -17,
    -53, -34, -21, -11, -28, -14, -24, -43,
]

PESTO_MG = {chess.PAWN: PAWN_MG, chess.KNIGHT: KNIGHT_MG, chess.BISHOP: BISHOP_MG,
            chess.ROOK: ROOK_MG, chess.QUEEN: QUEEN_MG, chess.KING: KING_MG}
PESTO_EG = {chess.PAWN: PAWN_EG, chess.KNIGHT: KNIGHT_EG, chess.BISHOP: BISHOP_EG,
            chess.ROOK: ROOK_EG, chess.QUEEN: QUEEN_EG, chess.KING: KING_EG}

# eval extras (tapered), tuned conservatively - the persona terms dominate
BISHOP_PAIR_MG, BISHOP_PAIR_EG = 30, 50
ROOK_OPEN_MG, ROOK_OPEN_EG = 25, 35
ROOK_SEMI_MG, ROOK_SEMI_EG = 10, 15
PASSED_MG = (0, 5, 10, 20, 35, 60, 100, 0)   # indexed by relative rank
PASSED_EG = (0, 10, 15, 25, 40, 70, 120, 0)

FILES = [sum(1 << (r * 8 + f) for r in range(8)) for f in range(8)]

# SPAN_W[sq] / SPAN_B[sq]: squares on files sq_f-1..sq_f+1 strictly ahead
# of sq - a pawn there means this pawn is not passed.
SPAN_W = [0] * 64
SPAN_B = [0] * 64
for _sq in range(64):
    _f, _r = _sq & 7, _sq >> 3
    for _df in (-1, 0, 1):
        _ff = _f + _df
        if 0 <= _ff <= 7:
            for _rr in range(_r + 1, 8):
                SPAN_W[_sq] |= 1 << (_rr * 8 + _ff)
            for _rr in range(0, _r):
                SPAN_B[_sq] |= 1 << (_rr * 8 + _ff)

# RING[d][sq] = bitboard of squares at Chebyshev distance exactly d from sq.
RING = [[0] * 64 for _ in range(6)]
for _sq in range(64):
    for _d in range(1, 6):
        for _t in range(64):
            if chess.square_distance(_sq, _t) == _d:
                RING[_d][_sq] |= 1 << _t

# CENTER_DIST[sq] = Chebyshev distance to the closest of d4/e4/d5/e5.
_CENTERS = (chess.D4, chess.E4, chess.D5, chess.E5)
CENTER_DIST = [min(chess.square_distance(s, c) for c in _CENTERS)
               for s in range(64)]

CONTEMPT = 50  # cp a winning soldier "loses" by allowing a repetition


def _build_psq(table: list[int], material: int, mult: float = 1.0
               ) -> tuple[list[int], list[int]]:
    """White-POV piece-square values with the persona flair baked in:
    material + CAP*tanh(positional/CAP). Computed ONCE at import - the
    tanh soft cap keeps small bonuses ~linear and saturates big ones (the
    soldier still prefers advancing, never at piece-sacrifice delusions).

    CPW prints the PeSTO tables a8-first (diagram order); python-chess
    indexes a1=0 - the rank flip happens HERE, once, at import.
    """
    return [material + int(CAP * math.tanh(table[sq ^ 56] * mult / CAP))
            for sq in range(64)]


def _mirror_table(table: list[int]) -> list[int]:
    """Black's view of the same table, indexed by real square."""
    return [table[chess.square_mirror(sq)] for sq in range(64)]


PSQ_MG_W = {
    chess.KNIGHT: _build_psq(KNIGHT_MG, MG_VALUE[chess.KNIGHT]),
    chess.BISHOP: _build_psq(BISHOP_MG, MG_VALUE[chess.BISHOP]),
    chess.ROOK: _build_psq(ROOK_MG, MG_VALUE[chess.ROOK]),
    chess.QUEEN: _build_psq(QUEEN_MG, MG_VALUE[chess.QUEEN]),
    chess.KING: _build_psq(KING_MG, 0),
}
PSQ_EG_W = {
    chess.KNIGHT: _build_psq(KNIGHT_EG, EG_VALUE[chess.KNIGHT]),
    chess.BISHOP: _build_psq(BISHOP_EG, EG_VALUE[chess.BISHOP]),
    chess.ROOK: _build_psq(ROOK_EG, EG_VALUE[chess.ROOK]),
    chess.QUEEN: _build_psq(QUEEN_EG, EG_VALUE[chess.QUEEN]),
    chess.KING: _build_psq(KING_EG, 0),
}
PSQ_MG_B = {pt: _mirror_table(tbl) for pt, tbl in PSQ_MG_W.items()}
PSQ_EG_B = {pt: _mirror_table(tbl) for pt, tbl in PSQ_EG_W.items()}

# pawn tables depend on the learned pawn_adv gene - cached per gene value
_PAWN_TABLES: dict[float, tuple[list[int], list[int], list[int], list[int]]] = {}


def _pawn_tables(pawn_adv: float
                 ) -> tuple[list[int], list[int], list[int], list[int]]:
    """(mg_white, eg_white, mg_black, eg_black) pawn tables."""
    key = round(pawn_adv, 4)
    tables = _PAWN_TABLES.get(key)
    if tables is None:
        mg_w = _build_psq(PAWN_MG, MG_VALUE[chess.PAWN], mult=key)
        eg_w = _build_psq(PAWN_EG, EG_VALUE[chess.PAWN], mult=key)
        tables = (mg_w, eg_w, _mirror_table(mg_w), _mirror_table(eg_w))
        _PAWN_TABLES[key] = tables
    return tables


class _SearchTimeout(Exception):
    pass


class Engine:
    """Alpha-beta engine with a persona-flavored evaluation and style layer."""

    def __init__(self, cfg: dict) -> None:
        cfg = cfg or {}
        self.min_time = float(cfg.get("min_time", 0.35))
        self.max_time = float(cfg.get("max_time", 8.0))
        self.time_fraction = float(cfg.get("time_fraction", 0.05))
        self.overhead_s = float(cfg.get("overhead_ms", 300)) / 1000.0

        self.tt: dict[tuple, tuple[int, int, int, chess.Move | None]] = {}
        self.killers: list[list[chess.Move | None]] = [[None, None] for _ in range(256)]
        self.history: dict[tuple[int, int], int] = defaultdict(int)
        self.nodes = 0
        self.stop_at = 0.0
        self.search_lock = threading.Lock()  # one search at a time
        # pondering protocol: ponder searches hold the lock opportunistically
        # and abort within ~2048 nodes the moment a REAL search wants in.
        # _urgent counts real searches waiting for the lock (checked by
        # ponder); ponder_interrupt lets the owner kill its own ponder.
        self._urgent = 0
        self._urgent_lock = threading.Lock()
        self.pondering = False
        self._stop_event: threading.Event | None = None
        # eval cache: leaf positions repeat constantly through qsearch - one
        # dict lookup beats a full tapered evaluation. Bound to the persona
        # dict by identity (genes constant within a search) and cleared per
        # search. A position's eval depends only on placement + side to move,
        # NOT on castling/ep rights - more hits, still exact.
        self._eval_P: dict | None = None
        self._eval_cache: dict[tuple, int] = {}
        self.use_eval_cache = bool((cfg or {}).get("eval_cache", True))
        # repetition context, rebuilt per search: game-history keys come in
        # from the handler, path keys are maintained during recursion
        self._rep_hist: dict = {}
        self._rep_path: dict = {}

    # ------------------------------------------------------------------
    # Evaluation (White's perspective, centipawns)
    # ------------------------------------------------------------------

    def evaluate(self, board: chess.Board, params: dict | None = None) -> int:
        """Static eval, White's perspective (negamax flips sign).

        Tapered PeSTO: material + MG/EG piece-square tables blended by game
        phase, plus eval extras (bishop pair, rook files, passed pawns).
        The persona flair (tanh soft cap) is pre-baked into the tables;
        ring-mask proximity, mop-up and the learned shield ride on top of
        the blended score. Pure bitboard iteration - leaf-node hot path.

        params (learned genes may override):
          aggression 0..1, pawn_adv, prox (multipliers), king_shield 0..0.5
        """
        P = params or {}
        if self.use_eval_cache:
            if P is not self._eval_P:
                self._eval_P = P
                self._eval_cache = {}
            key = (board.pawns, board.knights, board.bishops, board.rooks,
                   board.queens, board.kings, board.occupied, board.turn)
            cached = self._eval_cache.get(key)
            if cached is not None:
                return cached

        agg = float(P.get("aggression", 0.0))
        prox = float(P.get("prox", 1.0))
        shield = float(P.get("king_shield", 0.0))

        occ_w = board.occupied_co[chess.WHITE]
        occ_b = board.occupied_co[chess.BLACK]
        pw = board.pawns & occ_w
        pb = board.pawns & occ_b
        n_w = board.knights & occ_w
        n_b = board.knights & occ_b
        b_w = board.bishops & occ_w
        b_b = board.bishops & occ_b
        r_w = board.rooks & occ_w
        r_b = board.rooks & occ_b
        q_w = board.queens & occ_w
        q_b = board.queens & occ_b

        # piece counts, computed once: they feed the mop-up trigger, the
        # shield's "endgame" switch and the MG/EG blend alike
        cn_w = n_w.bit_count()
        cn_b = n_b.bit_count()
        cb_w = b_w.bit_count()
        cb_b = b_b.bit_count()
        cr_w = r_w.bit_count()
        cr_b = r_b.bit_count()
        cq_w = q_w.bit_count()
        cq_b = q_b.bit_count()
        np_w = cn_w * 320 + cb_w * 330 + cr_w * 500 + cq_w * 900
        np_b = cn_b * 320 + cb_b * 330 + cr_b * 500 + cq_b * 900
        endgame = (np_w + np_b) < 1600

        # game phase for the MG/EG blend: 24 at the start, 0 in K-only endings
        phase = min(24, cn_w + cn_b + cb_w + cb_b
                    + 2 * (cr_w + cr_b) + 4 * (cq_w + cq_b))

        pawn_mg_w, pawn_eg_w, pawn_mg_b, pawn_eg_b = \
            _pawn_tables(float(P.get("pawn_adv", 1.0)))

        # material + PeSTO tables, both phases in a single pass
        # white adds, black subtracts - negamax convention
        score_mg = 0
        score_eg = 0
        for bb, mg_t, eg_t, sign in (
            (pw, pawn_mg_w, pawn_eg_w, 1), (pb, pawn_mg_b, pawn_eg_b, -1),
            (n_w, PSQ_MG_W[chess.KNIGHT], PSQ_EG_W[chess.KNIGHT], 1),
            (n_b, PSQ_MG_B[chess.KNIGHT], PSQ_EG_B[chess.KNIGHT], -1),
            (b_w, PSQ_MG_W[chess.BISHOP], PSQ_EG_W[chess.BISHOP], 1),
            (b_b, PSQ_MG_B[chess.BISHOP], PSQ_EG_B[chess.BISHOP], -1),
            (r_w, PSQ_MG_W[chess.ROOK], PSQ_EG_W[chess.ROOK], 1),
            (r_b, PSQ_MG_B[chess.ROOK], PSQ_EG_B[chess.ROOK], -1),
            (q_w, PSQ_MG_W[chess.QUEEN], PSQ_EG_W[chess.QUEEN], 1),
            (q_b, PSQ_MG_B[chess.QUEEN], PSQ_EG_B[chess.QUEEN], -1),
        ):
            while bb:
                lsb = bb & -bb
                sq = lsb.bit_length() - 1
                score_mg += sign * mg_t[sq]
                score_eg += sign * eg_t[sq]
                bb ^= lsb

        # kings: exactly one each in any searched position
        kw = board.kings & occ_w
        kb = board.kings & occ_b
        wk = (kw & -kw).bit_length() - 1 if kw else None
        bk = (kb & -kb).bit_length() - 1 if kb else None
        if wk is not None:
            score_mg += PSQ_MG_W[chess.KING][wk]
            score_eg += PSQ_EG_W[chess.KING][wk]
        if bk is not None:
            score_mg -= PSQ_MG_B[chess.KING][bk]
            score_eg -= PSQ_EG_B[chess.KING][bk]

        # --- eval extras (tapered) ---
        # bishop pair: long-range coherence (separate ifs - both sides can
        # hold a pair, and the eval must stay mirror-symmetric)
        if b_w.bit_count() >= 2:
            score_mg += BISHOP_PAIR_MG
            score_eg += BISHOP_PAIR_EG
        if b_b.bit_count() >= 2:
            score_mg -= BISHOP_PAIR_MG
            score_eg -= BISHOP_PAIR_EG

        # rooks love open files, tolerate semi-open ones
        for rooks_bb, own_pawns, their_pawns, sign in (
                (r_w, pw, pb, 1), (r_b, pb, pw, -1)):
            bb = rooks_bb
            while bb:
                lsb = bb & -bb
                file_bb = FILES[lsb.bit_length() - 1 & 7]
                if not own_pawns & file_bb:
                    if not their_pawns & file_bb:
                        mg, eg = ROOK_OPEN_MG, ROOK_OPEN_EG
                    else:
                        mg, eg = ROOK_SEMI_MG, ROOK_SEMI_EG
                    score_mg += sign * mg
                    score_eg += sign * eg
                bb ^= lsb

        # passed pawns: the persona in one term - pawns dream of promotion
        bb = pw
        while bb:
            lsb = bb & -bb
            sq = lsb.bit_length() - 1
            if not pb & SPAN_W[sq]:
                score_mg += PASSED_MG[sq >> 3]
                score_eg += PASSED_EG[sq >> 3]
            bb ^= lsb
        bb = pb
        while bb:
            lsb = bb & -bb
            sq = lsb.bit_length() - 1
            if not pw & SPAN_B[sq]:
                score_mg -= PASSED_MG[7 - (sq >> 3)]
                score_eg -= PASSED_EG[7 - (sq >> 3)]
            bb ^= lsb

        # tapered blend into a single score, extras included.
        # Symmetric rounding: eval(mirror) must be exactly -eval, so no
        # plain floor division (it biases negatives down by one).
        total = score_mg * phase + score_eg * (24 - phase)
        score = ((total + 12) // 24) if total >= 0 else -((12 - total) // 24)

        # aggression proximity: how much of the army is near the enemy king
        if agg:
            w_army = occ_w & ~board.kings
            b_army = occ_b & ~board.kings
            for d in (1, 2, 3, 4, 5):
                w = 6 - d
                if bk is not None:
                    ring = RING[d][bk]
                    score += int(w * (3 * (pw & ring).bit_count()
                                      + 2 * (w_army & ring).bit_count())
                                 * agg * prox)
                if wk is not None:
                    ring = RING[d][wk]
                    score -= int(w * (3 * (pb & ring).bit_count()
                                      + 2 * (b_army & ring).bit_count())
                                 * agg * prox)

        # mop-up: when decisively ahead vs a bare-ish king, hunt it down
        mat_w = np_w + 100 * pw.bit_count()
        mat_b = np_b + 100 * pb.bit_count()
        if wk is not None and bk is not None:
            if mat_w - mat_b >= 500 and not pb:
                score += self._mopup(bk, wk)
            elif mat_b - mat_w >= 500 and not pw:
                score -= self._mopup(wk, bk)

        # LEARNED defense: normally zero. Grows only via checkmate losses.
        if shield > 0 and not endgame:
            for k_sq, pawns_of_side, sign in ((wk, pw, 1), (bk, pb, -1)):
                if k_sq is None:
                    continue
                kf = k_sq & 7  # file
                fwd = 8 if sign == 1 else -8
                shields = 0
                for df in (-1, 0, 1):
                    f = kf + df
                    t = k_sq + fwd + df
                    if 0 <= f <= 7 and 0 <= t < 64 and (pawns_of_side >> t) & 1:
                        shields += 1
                score += sign * (shields - 3) * 12 * shield

        score = score if board.turn == chess.WHITE else -score
        if self.use_eval_cache:
            self._eval_cache[key] = score
        return score

    @staticmethod
    def _mopup(losing_king: int, winning_king: int) -> int:
        """Drive the bare enemy king to the edge, bring our king closer."""
        kd = abs(chess.square_file(winning_king) - chess.square_file(losing_king))
        kr = abs(chess.square_rank(winning_king) - chess.square_rank(losing_king))
        return 10 * CENTER_DIST[losing_king] + 4 * (14 - (kd + kr))

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    @staticmethod
    def _tt_key(board: chess.Board) -> tuple:
        """Exact position key from bitboards (faster than recomputing zobrist)."""
        return (board.pawns, board.knights, board.bishops, board.rooks,
                board.queens, board.kings, board.occupied_co[chess.WHITE],
                board.turn, board.castling_rights, board.ep_square)

    def search(self, board: chess.Board, budget_s: float, persona: dict | None,
               rep_keys: dict | None = None, stop_event: threading.Event | None = None,
               ponder: bool = False) -> dict:
        """Iterative-deepening search. Returns best move, score and top-k.

        Root window: alpha sits at best - slack - 1, so every move within
        the style slack of the best gets an EXACT score; moves outside are
        fail-low upper bounds and are excluded from style picks.
        Best/top only come from fully completed depths.

        rep_keys: position keys (from engine._tt_key) already seen in the
        real game, so the search can score repetitions with contempt.

        ponder=True marks an opportunistic background search (TT warming
        while the opponent thinks): it refuses to wait for the lock, and
        aborts within ~2048 nodes when a real search wants in or the
        stop_event fires. Real searches declare themselves via _urgent.
        """
        if not ponder:
            with self._urgent_lock:
                self._urgent += 1
        try:
            if ponder:
                if not self.search_lock.acquire(timeout=1.0):
                    return {"move": None, "score": 0, "depth": 0, "top": []}
            else:
                self.search_lock.acquire()
            try:
                if ponder and self._urgent > 0:
                    return {"move": None, "score": 0, "depth": 0, "top": []}
                self.pondering = ponder
                self._stop_event = stop_event
                return self._search_locked(board, budget_s, persona, rep_keys)
            finally:
                self.pondering = False
                self._stop_event = None
                self.search_lock.release()
        finally:
            if not ponder:
                with self._urgent_lock:
                    self._urgent -= 1

    def _search_locked(self, board: chess.Board, budget_s: float,
                       persona: dict | None, rep_keys: dict | None) -> dict:
        P = persona or {}
        slack = int(P.get("style_eval_slack", 45))
        # the TT persists across moves - overlapping positions are the
        # whole point. It only resets when it outgrows TT_MAX.
        if len(self.tt) > TT_MAX:
            self.tt.clear()
        # the eval cache is per-move: same persona, fresh positions
        self._eval_P = P
        self._eval_cache = {}
        self.killers = [[None, None] for _ in range(256)]
        self.nodes = 0
        self.stop_at = time.time() + max(0.05, budget_s)
        self._rep_hist = dict(rep_keys) if rep_keys else {}
        self._rep_path = {}

        work = board.copy(stack=False)
        root_moves = list(work.legal_moves)
        if not root_moves:
            return {"move": None, "score": 0, "depth": 0, "top": []}

        random.shuffle(root_moves)  # variety between games
        prev_scores = {m: 0 for m in root_moves}
        best = root_moves[0]
        best_score = 0
        depth_reached = 0
        top: list[tuple[chess.Move, int]] = []

        for depth in range(1, 41):
            if time.time() > self.stop_at - 0.05:
                break
            # aspiration on beta: expect the score near last depth's,
            # re-search wide open if the position jumped (fail high)
            beta = best_score + 40 if depth >= 3 else INF
            while True:
                alpha = -INF
                best_this = None
                best_score_this = -INF
                scores: list[tuple[chess.Move, int]] = []
                try:
                    for move in sorted(root_moves, key=lambda m: prev_scores[m],
                                       reverse=True):
                        work.push(move)
                        score = -self._negamax(work, depth - 1, -beta, -alpha,
                                               1, P)
                        work.pop()
                        scores.append((move, score))
                        if score > best_score_this:
                            best_score_this, best_this = score, move
                        # keep exact scores for rivals within style slack
                        alpha = best_score_this - slack - 1
                except _SearchTimeout:
                    # the timeout unwound the recursion mid-push: every
                    # level below the root leaked its pop, so drain back
                    # to the base position before this board is reused
                    # (fail-high re-search, next depth)
                    while work.move_stack:
                        work.pop()
                    # a real deadline keeps the previous depth's result and
                    # lets the outer loop wind down - but a ponder abort
                    # (a real search wants the lock, or the owner pulled
                    # the plug) must LEAVE now, or the root just starts the
                    # next depth and keeps squatting on the table it was
                    # told to vacate
                    if self.pondering and (self._urgent > 0 or (
                            self._stop_event is not None
                            and self._stop_event.is_set())):
                        return {
                            "move": best,
                            "score": best_score,
                            "depth": depth_reached,
                            "top": top,
                        }
                    break  # depth incomplete: keep the previous depth's result
                if best_score_this >= beta:
                    beta = INF  # fail high: the move was better than expected
                    continue    # ...re-search this depth for exact scores
                # depth fully searched: commit
                best, best_score = best_this, best_score_this
                depth_reached = depth
                top = sorted(scores, key=lambda x: x[1], reverse=True)[:6]
                prev_scores = dict(scores)
                if best_score > MATE - 1000:   # mate found, stop digging
                    return {
                        "move": best,
                        "score": best_score,
                        "depth": depth_reached,
                        "top": top,
                    }
                break

        return {
            "move": best,
            "score": best_score,
            "depth": depth_reached,
            "top": top,
        }

    def _negamax(self, board: chess.Board, depth: int, alpha: int, beta: int,
                 ply: int, P: dict, can_null: bool = True) -> int:
        self.nodes += 1
        if self.nodes % 2048 == 0:
            if time.time() > self.stop_at:
                raise _SearchTimeout()
            # a ponder search is a guest: it leaves the moment a real
            # search wants the table or its owner pulls the plug
            if self.pondering and (self._urgent > 0 or (
                    self._stop_event is not None and self._stop_event.is_set())):
                raise _SearchTimeout()

        key = self._tt_key(board)
        path_n = self._rep_path.get(key, 0) + 1
        self._rep_path[key] = path_n
        try:
            # --- repetition & 50-move, scored with contempt ---
            # A position seen once before (on path or in the real game)
            # makes a third occurrence trivially reachable: winning sides
            # must not walk in circles, losing sides happily shake hands.
            # Never stored in the TT - repetition scores are path-dependent.
            if (path_n + self._rep_hist.get(key, 0) >= 2
                    or board.halfmove_clock >= 100):
                return self._draw_score(board, P)

            entry = self.tt.get(key)
            tt_move = None
            if entry:
                e_depth, flag, e_score, tt_move = entry
                # mate scores were stored with the ply they were found at:
                # re-anchor them to THIS node's distance to the mate
                if e_score > MATE_BOUND:
                    e_score -= ply
                elif e_score < -MATE_BOUND:
                    e_score += ply
                if e_depth >= depth:
                    if flag == 0:
                        return e_score
                    if flag == 1 and e_score >= beta:
                        return e_score
                    if flag == 2 and e_score <= alpha:
                        return e_score

            # one movegen per node (was three: is_checkmate + is_stalemate
            # + the move loop each walked the legal moves)
            legal = list(board.legal_moves)
            if not legal:
                return -MATE + ply if board.is_check() else 0
            if depth <= 0:
                return self._quiesce(board, alpha, beta, ply, P)

            # --- null-move pruning: hand the opponent a free move; if we
            # STILL beat beta, the real position is almost surely winning.
            # Skipped in check, at mate scores, and in pawn-only endings
            # (zugzwang is for lesser soldiers, but let's not gamble).
            if (can_null and depth >= 3 and -900_000 < beta < 900_000
                    and not board.is_check()
                    and board.occupied_co[board.turn] & ~board.pawns & ~board.kings
                    and self.evaluate(board, P) >= beta):
                board.push(chess.Move.null())
                score = -self._negamax(board, depth - 3, -beta, -beta + 1,
                                       ply + 1, P, can_null=False)
                board.pop()
                if score >= beta:
                    return beta

            legal.sort(key=lambda m: self._order(board, m, tt_move, ply),
                       reverse=True)

            best_score = -INF
            best_move = None
            old_alpha = alpha
            in_check = board.is_check()
            move_idx = 0
            for move in legal:
                is_quiet = not (board.is_capture(move) or move.promotion)
                board.push(move)
                new_depth = depth - 1
                # check extension: the soldier digs deeper through checks
                gives_check = board.is_check()
                if gives_check and new_depth >= 1:
                    new_depth += 1
                if move_idx == 0:
                    # PVS: the first move gets the full window - it is the
                    # one most likely to be best
                    score = -self._negamax(board, new_depth, -beta, -alpha,
                                           ply + 1, P)
                else:
                    if (depth >= 3 and move_idx >= 4 and is_quiet
                            and not gives_check and not in_check):
                        # LMR: late, quiet, unprovoked moves are probably
                        # noise - peek at them shallow, trust nothing until
                        # a full re-search proves them right
                        r = 1 + (1 if move_idx >= 8 else 0) \
                              + (1 if depth >= 6 else 0)
                        score = -self._negamax(board, max(1, new_depth - r),
                                               -alpha - 1, -alpha, ply + 1, P)
                    else:
                        # null-window scout: a yes/no question, cheap to ask
                        score = -self._negamax(board, new_depth, -alpha - 1,
                                               -alpha, ply + 1, P)
                    if alpha < score < beta:
                        # surprise: worth a full search after all
                        score = -self._negamax(board, new_depth, -beta, -alpha,
                                               ply + 1, P)
                board.pop()
                move_idx += 1
                if score > best_score:
                    best_score, best_move = score, move
                if score > alpha:
                    alpha = score
                if alpha >= beta:
                    if is_quiet:
                        self.killers[ply % 256] = [move, self.killers[ply % 256][0]]
                        self.history[(board.piece_type_at(move.from_square), move.to_square)] += depth * depth
                    break

            # mate scores go into the TT anchored to this node's ply, so a
            # probe at any other distance re-anchors correctly
            s = best_score
            if s > MATE_BOUND:
                s += ply
            elif s < -MATE_BOUND:
                s -= ply
            flag = 0 if old_alpha < best_score < beta else (1 if best_score >= beta else 2)
            self.tt[key] = (depth, flag, s, best_move)
            return best_score
        finally:
            if path_n <= 1:
                del self._rep_path[key]
            else:
                self._rep_path[key] = path_n - 1

    def _draw_score(self, board: chess.Board, P: dict) -> int:
        """Contempt, from the side-to-move's perspective: a draw is a
        defeat for whoever is pushing ahead, a relief for whoever bleeds."""
        static = self.evaluate(board, P)
        if static > 100:
            return -CONTEMPT
        if static < -100:
            return CONTEMPT
        return 0

    def _quiesce(self, board: chess.Board, alpha: int, beta: int,
                 ply: int, P: dict) -> int:
        self.nodes += 1
        if self.nodes % 2048 == 0:
            if time.time() > self.stop_at:
                raise _SearchTimeout()
            # a ponder search is a guest: it leaves the moment a real
            # search wants the table or its owner pulls the plug
            if self.pondering and (self._urgent > 0 or (
                    self._stop_event is not None and self._stop_event.is_set())):
                raise _SearchTimeout()
        if ply >= 96:  # safety valve for pathological check chains
            return self.evaluate(board, P)

        if board.is_check():
            # In check there is no standing pat: search every evasion.
            # This also means qsearch finally SEES mated leaves - the old
            # version evaluated a lost position as if nothing happened.
            evasions = list(board.legal_moves)
            if not evasions:
                return -MATE + ply
            evasions.sort(key=lambda m: self._capture_score(board, m)
                          + (500 if m.promotion else 0), reverse=True)
            best = -INF
            for mv in evasions:
                board.push(mv)
                score = -self._quiesce(board, -beta, -alpha, ply + 1, P)
                board.pop()
                if score > best:
                    best = score
                if score >= beta:
                    return beta
                if score > alpha:
                    alpha = score
            return best

        stand = self.evaluate(board, P)
        if stand >= beta:
            return stand
        if stand > alpha:
            alpha = stand

        moves = list(board.generate_legal_captures())
        # quiet queen promotions are tactical too: a pawn about to crown
        # is exactly the kind of thing the horizon hides
        if board.turn == chess.WHITE:
            promo_from = board.pawns & board.occupied_co[chess.WHITE] & chess.BB_RANK_7
            promo_to = chess.BB_RANK_8
        else:
            promo_from = board.pawns & board.occupied_co[chess.BLACK] & chess.BB_RANK_2
            promo_to = chess.BB_RANK_1
        if promo_from:
            moves.extend(board.generate_legal_moves(promo_from, promo_to))
        moves.sort(key=lambda m: self._capture_score(board, m)
                   + (500 if m.promotion else 0), reverse=True)

        best = stand
        for mv in moves:
            # delta pruning: even in the best case this capture cannot
            # raise alpha - not worth a make/unmake
            victim = board.piece_type_at(mv.to_square)
            gain = (PIECE_VALUE[victim if victim else chess.PAWN]
                    + (800 if mv.promotion else 0) + 200)
            if stand + gain <= alpha:
                continue
            board.push(mv)
            score = -self._quiesce(board, -beta, -alpha, ply + 1, P)
            board.pop()
            if score > best:
                best = score
            if score >= beta:
                return beta
            if score > alpha:
                alpha = score
        return best

    # ------------------------------------------------------------------
    # Move ordering
    # ------------------------------------------------------------------

    def _capture_score(self, board: chess.Board, move: chess.Move) -> int:
        victim = board.piece_type_at(move.to_square) or chess.PAWN  # en passant
        attacker = board.piece_type_at(move.from_square) or chess.PAWN
        return 10 * PIECE_VALUE[victim] - PIECE_VALUE[attacker]

    def _order(self, board: chess.Board, move: chess.Move,
               tt_move: chess.Move | None, ply: int) -> int:
        # NOTE: no gives_check here - python-chess implements it as
        # push/is_check/pop, i.e. a full extra make/unmake per move.
        # The check extension already rewards checks for free after push.
        if tt_move is not None and move == tt_move:
            return 10_000_000
        s = 0
        if board.is_capture(move):
            s += 1_000_000 + self._capture_score(board, move)
        if move.promotion:
            s += 900_000
        if self.killers[ply % 256][0] == move:
            s += 400_000
        elif self.killers[ply % 256][1] == move:
            s += 350_000
        # aggressive pawn marching gets an ordering boost
        if board.piece_type_at(move.from_square) == chess.PAWN:
            rank = chess.square_rank(move.to_square)
            if (board.turn == chess.WHITE and rank >= 4) or \
               (board.turn == chess.BLACK and rank <= 3):
                s += 20_000
        s += self.history.get(
            (board.piece_type_at(move.from_square), move.to_square), 0)
        return s

    # ------------------------------------------------------------------
    # Persona: pick the move
    # ------------------------------------------------------------------

    def pick_move(self, board: chess.Board, budget_s: float,
                  persona: dict | None, rep_keys: dict | None = None) -> dict:
        """Search, then optionally trade a little eval for extra violence.

        Only uses exact scores from the last fully completed depth.
        """
        result = self.search(board, budget_s, persona, rep_keys)
        if result["move"] is None:
            return result

        p = persona or {}
        prob = float(p.get("style_pick_prob", 0.0))
        slack = int(p.get("style_eval_slack", 40))
        top = result["top"]

        if (prob > 0 and len(top) >= 2 and result["score"] > -300
                and random.random() < prob):
            best_score = top[0][1]
            rivals = [m for m, s in top[1:] if s >= best_score - slack]
            if rivals:
                best_violence = self._violence(board, top[0][0])
                for rival in rivals:
                    if self._violence(board, rival) > best_violence:
                        result["move"] = rival
                        result["style_pick"] = True
                        break
        return result

    def _violence(self, board: chess.Board, move: chess.Move) -> float:
        """How much does this move smell like an attack?"""
        v = 0.0
        if board.is_capture(move):
            victim = board.piece_type_at(move.to_square) or chess.PAWN
            v += 30 + PIECE_VALUE[victim] / 10
        if move.promotion:
            v += 15
        if board.piece_type_at(move.from_square) == chess.PAWN:
            rank = chess.square_rank(move.to_square)
            if board.turn == chess.WHITE:
                v += {4: 8, 5: 14, 6: 22}.get(rank, 0)
            else:
                v += {3: 8, 2: 14, 1: 22}.get(rank, 0)
        board.push(move)
        try:
            if board.is_check():
                v += 25
            if board.is_checkmate():
                v += 1000
            enemy_king = board.king(not board.turn)
            if enemy_king is not None:
                d = chess.square_distance(move.to_square, enemy_king)
                v += max(0, 4 - d) * 4
        finally:
            board.pop()
        return v
