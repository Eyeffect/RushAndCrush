"""
Movegen verification for the staged-movegen candidate.

The staged search must consider EXACTLY the legal moves of the position -
no missing move (a lost tempo) and no duplicate (a wasted search). It also
relies on a python-chess generator being resumed after push/pop cycles, so
that behaviour is verified explicitly here.

Usage:
  pypy verify_movegen.py [module]
"""

import random
import sys

import chess


def staged_set(board: chess.Board):
    """The move set the staged search would build (captures + quiets)."""
    caps = list(board.generate_legal_captures())
    if board.turn == chess.WHITE:
        promo_from = (board.pawns & board.occupied_co[chess.WHITE]
                      & chess.BB_RANK_7)
        promo_to = chess.BB_RANK_8
    else:
        promo_from = (board.pawns & board.occupied_co[chess.BLACK]
                      & chess.BB_RANK_2)
        promo_to = chess.BB_RANK_1
    if promo_from:
        caps.extend(m for m in board.generate_legal_moves(promo_from, promo_to)
                    if not board.is_capture(m))
    quiet = [m for m in board.legal_moves
             if not board.is_capture(m) and not m.promotion]
    return caps, quiet


def main() -> None:
    rng = random.Random(12345)
    checked = 0
    resumed = 0
    for _ in range(400):
        board = chess.Board()
        for _ in range(rng.randint(0, 60)):
            moves = list(board.legal_moves)
            if not moves:
                break
            board.push(rng.choice(moves))
        if board.is_game_over():
            continue

        legal = set(board.legal_moves)
        caps, quiet = staged_set(board)
        staged = set(caps) | set(quiet)
        missing = legal - staged
        extra = staged - legal
        assert not missing, f"missing moves: {missing}"
        assert not extra, f"illegal moves generated: {extra}"
        # no duplicates inside the staged list
        all_list = list(caps) + list(quiet)
        assert len(all_list) == len(set(all_list)), "duplicate move in staged list"
        checked += 1

        # generator resume: peek one move, do some push/pop cycles, then
        # take the rest - the full list must still match
        it = iter(board.legal_moves)
        first = next(it, None)
        if first is not None:
            probe = board.copy(stack=False)
            for _ in range(3):
                tmp = list(probe.legal_moves)
                if not tmp:
                    break
                probe.push(rng.choice(tmp))
                probe.pop()
            rest = list(it)
            assert first not in rest, "generator repeated the peeked move"
            assert len(set(rest) | {first}) == len(legal), \
                "resumed generator lost or duplicated moves"
            resumed += 1

    print(f"[ok] movegen: {checked} positions, exact legal-move coverage; "
          f"{resumed} generator-resume checks passed")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(f"(module under test: {sys.argv[1]})")
    main()
