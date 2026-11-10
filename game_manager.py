"""
One thread per game: follows the Lichess game stream, thinks with the
engine, plays moves, and feeds the persona all the battlefield drama.
"""

import json
import logging
import os
import threading
import time

import chess
import chess.pgn

log = logging.getLogger("rushandcrush.game")


class GameHandler(threading.Thread):
    def __init__(self, api, engine, persona, stores, cfg, game_id: str) -> None:
        super().__init__(daemon=True, name=f"game-{game_id}")
        self.api = api
        self.engine = engine
        self.persona = persona
        self.stores = stores
        self.cfg = cfg
        self.game_id = game_id
        self.done = False

        ecfg = cfg.get("engine", {})
        self.min_time = float(ecfg.get("min_time", 0.35))
        self.max_time = float(ecfg.get("max_time", 8.0))
        self.time_fraction = float(ecfg.get("time_fraction", 0.05))

        pcfg = cfg.get("persona", {})
        self.resign_enabled = bool(pcfg.get("resign_enabled", False))
        self.resign_score = int(pcfg.get("resign_score", -1200))
        self.resign_after = int(pcfg.get("resign_after_moves", 50))

        # game state
        self.board = chess.Board()
        self.initial_fen = chess.STARTING_FEN
        self.moves_applied = 0
        self.game_moves: list[str] = []      # every ply, both sides
        self.our_moves: list[dict] = []      # our moves + eval + clock + style flag
        self.my_color = chess.WHITE
        self.me = cfg.get("account", {}).get("username", "me")
        self.opponent = "soldier"
        self.opponent_is_bot = False
        self.last_search_score: int | None = None
        self.bad_move_streak = 0
        self.pawn_storm_count = 0
        self.time_talked = False
        # situated-chat bookkeeping (worker-owned, like pawn_storm_count)
        self.first_blood_done = False   # first capture we land this game
        self.our_queen_alive = True     # queen_lost fires once, when she falls
        self.comeback_done = False      # one crow per game when the eval flips
        self.white_name = "White"
        self.black_name = "Black"
        self.opponent_rating: int | None = None
        self.tc: dict | None = None
        self.book_hits = 0
        # within-game adaptation (the soldier changes tactics, not soul)
        self.recent_scores: list[int] = []  # last two own-search scores
        self.lost_streak = 0                # consecutive searches <= -1200
        self.last_stand = False

        # --- Batch 2: reader/worker split + pondering --------------------
        # The stream reader NEVER thinks: it applies server state and wakes
        # the worker. The worker thinks, verifies the position is still the
        # one it searched for, and only then commits + posts - a state that
        # changed mid-think discards the result instead of feeding the
        # server a 400. After posting, the worker ponders the predicted
        # reply, warming the shared TT on the opponent's clock.
        self.ponder_enabled = bool(ecfg.get("ponder", True))
        self.ponder_max_time = float(ecfg.get("ponder_max_time", 10.0))
        self.ponder_min_my_ms = int(ecfg.get("ponder_min_clock_ms", 10_000))
        self.lock = threading.Lock()
        self.turn_cv = threading.Condition(self.lock)
        self.my_turn = False                 # our move is pending
        self.server_status = "started"
        self.wtime_ms: int | None = None
        self.btime_ms: int | None = None
        self.winc_ms = 0
        self.binc_ms = 0
        self.ponder_stop: threading.Event | None = None
        self.ponder_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        worker = threading.Thread(target=self._work_loop, daemon=True,
                                  name=f"work-{self.game_id}")
        worker.start()
        try:
            resp = self.api.game_stream(self.game_id)
            for line in resp.iter_lines():
                if not line:
                    continue
                msg = json.loads(line)
                mtype = msg.get("type")
                if mtype == "gameFull":
                    self._handle_full(msg)
                elif mtype == "gameState":
                    self._handle_state(msg)
                elif mtype == "chatLine":
                    self._handle_chat_line(msg)
                if self.done:
                    break
        except Exception as e:
            log.warning("[%s] game stream ended: %s", self.game_id, e)
        finally:
            with self.lock:
                self.done = True
                self.turn_cv.notify_all()
            self._stop_ponder()
            worker.join(timeout=5.0)
            log.info("[%s] handler finished", self.game_id)

    # ------------------------------------------------------------------
    # stream events
    # ------------------------------------------------------------------

    def _handle_full(self, msg: dict) -> None:
        # Lichess sends the literal token "startpos" for standard games;
        # normalize ONCE here so every later chess.Board(initial_fen) -
        # resume, repetition keys, PGN archival - sees a real FEN.
        fen = msg.get("initialFen", chess.STARTING_FEN)
        self.initial_fen = chess.STARTING_FEN if fen == "startpos" else fen
        self.board = chess.Board(self.initial_fen)

        white_obj = msg.get("white") or {}
        black_obj = msg.get("black") or {}
        self.white_name = white_obj.get("name", "White")
        self.black_name = black_obj.get("name", "Black")
        self.tc = msg.get("clock")
        if self.white_name.lower() == self.me.lower():
            self.my_color = chess.WHITE
            opp_obj = black_obj
        else:
            self.my_color = chess.BLACK
            opp_obj = white_obj
        self.opponent = opp_obj.get("name", "soldier")
        # only the OPPONENT's title matters - we are a BOT ourselves, so
        # checking both titles would always report "bot"
        self.opponent_is_bot = opp_obj.get("title") == "BOT"
        rating = opp_obj.get("rating")
        self.opponent_rating = rating if isinstance(rating, int) else None

        log.info("[%s] game vs %s (%s), I am %s", self.game_id, self.opponent,
                 "bot" if self.opponent_is_bot else "human",
                 "white" if self.my_color == chess.WHITE else "black")

        subs = {"opponent": self.opponent}
        # the greeting reads the head-to-head record: revenge when THEY
        # lead the series, a victory lap when WE do, a grudge match when
        # it's even - never "you beat me once" right after we beat them
        h2h = self.stores.nemesis.head_to_head(self.opponent)
        if h2h[1] > h2h[0]:
            trigger = "game_start_nemesis"
        elif h2h[0] > h2h[1]:
            trigger = "game_start_rematch"
        elif h2h[0]:  # played before, series tied
            trigger = "game_start_grudge"
        else:
            trigger = "game_start"
        text = self.persona.compose(trigger, subs, force=True)
        if text:
            self.api.chat(self.game_id, "player", text)
        spec = self.persona.compose("spectator_start", force=True)
        if spec:
            self.api.chat(self.game_id, "spectator", spec)

        self._apply_state(msg.get("state", {}))

    def _handle_state(self, msg: dict) -> None:
        self._apply_state(msg)

    def _handle_chat_line(self, msg: dict) -> None:
        username = msg.get("username", "")
        if username in ("lichess", self.me):
            return
        log.info("[%s] %s (%s): %s", self.game_id, username, msg.get("room"), msg.get("text"))

    # ------------------------------------------------------------------

    def _apply_state(self, state: dict) -> None:
        """Reader thread only: sync shared state, wake the worker when it
        is our move. Never blocks on the engine - thinking happens in the
        worker, so the stream keeps flowing while we search."""
        finish_state = None
        with self.lock:
            status = state.get("status", "started")
            self.server_status = status
            if status not in ("started", "created"):
                self.done = True
                finish_state = state
            else:
                moves = state.get("moves", "").split()

                # sync our board with the server's move list. Idempotent:
                # moves we committed ourselves are already in the list.
                if self.moves_applied > len(moves):
                    self.board = chess.Board(self.initial_fen)
                    self.moves_applied = 0
                    self.game_moves = []
                while self.moves_applied < len(moves):
                    try:
                        self.board.push(
                            chess.Move.from_uci(moves[self.moves_applied]))
                    except ValueError:
                        log.warning("[%s] illegal move from stream, resyncing",
                                    self.game_id)
                        self.board = chess.Board(self.initial_fen)
                        self.moves_applied = 0
                        self.game_moves = []
                        continue
                    self.game_moves.append(moves[self.moves_applied])
                    self.moves_applied += 1

                if state.get("wtime") is not None:
                    self.wtime_ms = state.get("wtime")
                    self.btime_ms = state.get("btime")
                    self.winc_ms = state.get("winc") or 0
                    self.binc_ms = state.get("binc") or 0

                if self.board.turn == self.my_color:
                    self.my_turn = True
                    self.turn_cv.notify_all()

        if finish_state is not None:
            self._finish(finish_state)
        if state.get("wtime") is not None:
            self._maybe_time_pressure(state)

    # ------------------------------------------------------------------
    # worker: all thinking and move committing happens here
    # ------------------------------------------------------------------

    def _work_loop(self) -> None:
        while True:
            with self.turn_cv:
                while True:
                    if self.done:
                        return
                    if (self.my_turn and self.board.turn == self.my_color
                            and self.server_status in ("started", "created")):
                        break
                    self.turn_cv.wait(timeout=1.0)
                # snapshot: the reader may keep applying state while we think
                ply = self.moves_applied
                my_ms = (self.wtime_ms if self.my_color == chess.WHITE
                         else self.btime_ms)
                my_inc = (self.winc_ms if self.my_color == chess.WHITE
                          else self.binc_ms) or 0
                opp_ms = (self.btime_ms if self.my_color == chess.WHITE
                          else self.wtime_ms)
                board_snap = self.board.copy(stack=False)
                moves_snap = list(self.game_moves)
                self.my_turn = False
            # a ponder from our previous move must yield the search lock now
            self._stop_ponder()
            try:
                self._play_move(board_snap, moves_snap, ply, my_ms, my_inc,
                                opp_ms)
            except Exception as e:
                log.warning("[%s] worker error: %s", self.game_id, e)

    # ------------------------------------------------------------------

    def _play_move(self, board: chess.Board, moves: list[str], ply: int,
                   my_ms: int | None, my_inc: int, opp_ms: int | None) -> None:
        """Worker thread. `board`/`moves` are the snapshot we decided on;
        nothing is committed until a lock-held check proves the server is
        still at `ply` and the game is alive."""
        my_ms = my_ms or 60000
        subs = {"opponent": self.opponent}

        # --- learned opening book: follow lines that have been winning ---
        book_uci = self.stores.learner.get_book_move(
            moves, self.my_color == chess.WHITE)
        book_move = None
        if book_uci:
            mv = chess.Move.from_uci(book_uci)
            if mv in board.legal_moves:
                book_move = mv
            else:
                log.info("[%s] book move %s illegal, ignoring",
                         self.game_id, book_uci)
        first_out_of_book = (book_move is None and bool(self.our_moves)
                             and self.our_moves[-1].get("book"))

        budget = self._budget(my_ms, my_inc, opp_ms, first_out_of_book)
        params = self._persona_params(my_ms)

        # --- within-game adaptation, from our last two own-search scores ---
        if self.last_stand:
            # three dead-searches in a row: maximum violence, minimum clock,
            # no lessons - a lost position is not a classroom
            budget = min(budget, 0.2)
            params["style_pick_prob"] = 1.0
            params["style_eval_slack"] = 10000
        elif self._converting():
            # clearly ahead: turn the assault down 4x, play quick, cash in
            params["aggression"] = float(params.get("aggression", 0.9)) * 0.25
            params["style_pick_prob"] = float(
                params.get("style_pick_prob", 0.25)) * 0.25
            budget = max(0.3, budget * 0.6)

        # --- decide the move on the snapshot ---
        legal_moves = list(board.legal_moves)
        move = None
        score: int | None = None
        depth = 0
        style = False
        is_book = False

        if book_move is not None:
            move = book_move
            is_book = True
        elif len(legal_moves) == 1 and not self.last_stand:
            move = legal_moves[0]  # forced: not worth a millisecond of clock
        else:
            result = self.engine.pick_move(
                board, budget, params, rep_keys=self._rep_keys(moves))
            move = result["move"]
            if move is None:
                log.info("[%s] no legal moves (mate/stalemate on board)",
                         self.game_id)
                return
            score = result["score"]
            depth = result["depth"]
            style = bool(result.get("style_pick"))

        # --- adaptation + learning, driven by the search ---
        if score is not None:
            self.recent_scores.append(score)
            if len(self.recent_scores) > 2:
                self.recent_scores.pop(0)
            self.lost_streak = self.lost_streak + 1 if score <= -1200 else 0
            if self.lost_streak >= 3 and not self.last_stand:
                self.last_stand = True
                log.info("[%s] LAST STAND: the position is dead. Maximum "
                         "violence, minimum clock - a soldier does not "
                         "surrender.", self.game_id)
                self._say("last_stand", subs)
            elif self.lost_streak == 0:
                self.last_stand = False

        prev_eval = self.last_search_score
        prev = self.our_moves[-1] if self.our_moves else None
        blunder_info = None
        # blunder/taunt hygiene: skip already-decided positions (beyond +/-600)
        # and mate-scale swings, and stay quiet once in last stand - otherwise
        # the ledger fills with noise from games that were already lost
        sane = (prev_eval is not None and prev is not None and score is not None
                and not self.last_stand
                and abs(prev_eval) <= 600 and abs(score) < 100_000)
        taunt = False
        if sane:
            if prev_eval - score >= 250:
                # our PREVIOUS move was a mistake - file it with the learner
                blunder_info = ((prev.get("clock") or 60000) / 1000.0,
                                bool(prev.get("style")), prev.get("fen"),
                                prev.get("uci"), prev_eval - score)
            elif score - prev_eval >= 250:
                taunt = True
        # the comeback: we were staring at a dead position and the eval
        # flipped - a soldier gets exactly one line to crow about it
        if (score is not None and prev_eval is not None
                and not self.comeback_done
                and prev_eval <= -250 and score >= 50):
            self.comeback_done = True
            self._say("comeback", subs)
        self.last_search_score = score

        # situation flags must be read BEFORE we push the move
        is_capture_move = board.is_capture(move)
        captured_queen = (is_capture_move and
                          board.piece_type_at(move.to_square) == chess.QUEEN)
        moved_is_pawn = board.piece_type_at(move.from_square) == chess.PAWN
        is_castle = board.is_castling(move)
        is_ep = board.is_en_passant(move)
        fen_before = board.fen()
        # our queen fell on the opponent's last move: one funeral, one line
        our_queen_fell = (self.our_queen_alive
                          and not board.pieces(chess.QUEEN, self.my_color))
        if our_queen_fell:
            self.our_queen_alive = False
        storm = False
        if moved_is_pawn and self.pawn_storm_count < 2:
            rank = chess.square_rank(move.to_square)
            advanced = (rank >= 5 if self.my_color == chess.WHITE else rank <= 3)
            if advanced and not move.promotion:
                storm = True
        board.push(move)

        # post-move triggers are judged on the snapshot position (before any
        # opponent reply can arrive) and fired only after the commit
        post_triggers: list[str] = []
        if board.is_check():
            post_triggers.append("check_given")
        if captured_queen:
            post_triggers.append("queen_taken")
        if storm:
            post_triggers.append("pawn_storm")
        if our_queen_fell:
            post_triggers.append("queen_lost")
        if is_capture_move and not self.first_blood_done:
            post_triggers.append("first_blood")
        if is_castle:
            post_triggers.append("castle")
        if move.promotion:
            post_triggers.append("promotion")
        if is_ep:
            post_triggers.append("en_passant")

        uci = move.uci()
        record = {"uci": uci, "eval": score, "clock": my_ms,
                  "style": style, "book": is_book, "fen": fen_before}

        # --- verify the world has not moved on, then commit ---
        with self.lock:
            if (self.done or self.server_status not in ("started", "created")
                    or self.moves_applied != ply):
                log.info("[%s] stale search discarded (ply %d vs %d)",
                         self.game_id, ply, self.moves_applied)
                return
            self.board = board       # snapshot + our move; reader resyncs
            self.moves_applied = ply + 1
            self.game_moves.append(uci)
            if is_book:
                self.book_hits += 1
            self.our_moves.append(record)
            if storm:
                self.pawn_storm_count += 1

        try:
            self.api.move(self.game_id, uci)
        except Exception as e:
            # move rejected (game over, race): the eval came from a phantom
            # position; don't let the learner learn from it
            with self.lock:
                self.last_search_score = None
            log.warning("[%s] move %s failed: %s", self.game_id, uci, e)
            return

        if is_book:
            log.info("[%s] played %s (BOOK, learned)", self.game_id, uci)
        elif score is not None:
            log.info("[%s] played %s (eval %d, depth %d%s)", self.game_id,
                     uci, score, depth, ", STYLE PICK" if style else "")
        else:
            log.info("[%s] played %s (forced)", self.game_id, uci)
        if is_capture_move:
            self.first_blood_done = True

        if blunder_info is not None:
            clk_s, prev_style, prev_fen, prev_uci, drop = blunder_info
            repeated = self.stores.learner.record_blunder(
                self._phase(), clk_s, prev_style, fen=prev_fen, uci=prev_uci,
                drop=drop)
            if repeated:
                self._say("repeat_mistake", subs)
        if taunt:
            self._say("taunt")
        for trig in post_triggers:
            self._say(trig, subs)

        # ponder the predicted reply: warm the TT on the opponent's clock
        if (self.ponder_enabled and score is not None
                and my_ms > self.ponder_min_my_ms and not self.last_stand):
            self._start_ponder(board, moves + [uci])

        # optional resignation: the last resort of a broken soldier
        if (self.resign_enabled and score is not None
                and self.moves_applied > self.resign_after):
            if score <= self.resign_score:
                self.bad_move_streak += 1
                if self.bad_move_streak >= 3:
                    self._say("resign", subs, force=True)
                    try:
                        self.api.resign(self.game_id)
                    except Exception:
                        pass
            else:
                self.bad_move_streak = 0

    # ------------------------------------------------------------------

    def _start_ponder(self, board: chess.Board, moves: list[str]) -> None:
        """Search the position after our move on the opponent's clock.
        Nothing is played - the point is a TT full of exact scores for
        their best replies, so our next real search starts hot."""
        stop = threading.Event()
        params = self._persona_params(60_000)
        t = threading.Thread(target=self._ponder_run, daemon=True,
                             name=f"ponder-{self.game_id}",
                             args=(board.copy(stack=False), list(moves),
                                   params, stop))
        with self.lock:
            self.ponder_stop = stop
            self.ponder_thread = t
        t.start()

    def _ponder_run(self, board: chess.Board, moves: list[str],
                    params: dict, stop: threading.Event) -> None:
        try:
            r = self.engine.search(board, self.ponder_max_time, params,
                                   rep_keys=self._rep_keys(moves),
                                   stop_event=stop, ponder=True)
            if r["move"] is not None and not stop.is_set():
                log.info("[%s] ponder: expecting %s (eval %d, depth %d)",
                         self.game_id, r["move"].uci(), r["score"], r["depth"])
        except Exception as e:
            log.debug("[%s] ponder ended: %s", self.game_id, e)

    def _stop_ponder(self) -> None:
        """Kill any running ponder. Idempotent; the join happens outside
        the lock so the reader never waits on the search."""
        with self.lock:
            ev = self.ponder_stop
            t = self.ponder_thread
            self.ponder_stop = None
            self.ponder_thread = None
        if ev is not None:
            ev.set()
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

    # ------------------------------------------------------------------

    def _rep_keys(self, moves: list[str] | None = None) -> dict:
        """Position keys of the real game so far (including the current
        position) - the search adds its own path keys on top, so any
        repetition is scored with contempt instead of blindly shuffled.
        Callers pass their snapshot; ponder passes the predicted line
        (game moves + our uci) so it warms the exact positions the next
        real search will hit."""
        line = self.game_moves if moves is None else moves
        b = chess.Board(self.initial_fen)  # normalized in _handle_full
        cnt: dict[tuple, int] = {self.engine._tt_key(b): 1}
        for u in line:
            b.push(chess.Move.from_uci(u))
            k = self.engine._tt_key(b)
            cnt[k] = cnt.get(k, 0) + 1
        return cnt

    def _finish(self, state: dict) -> None:
        # the game is over NOW: mark it dead before anything else so a
        # worker mid-search discards its result instead of posting a move
        # to a finished game (verify sees done), and stop the ponder -
        # there is nothing left to warm the table for
        with self.lock:
            self.done = True
            self.turn_cv.notify_all()
        self._stop_ponder()

        status = state.get("status", "unknown")
        if status in ("aborted", "aborting"):
            log.info("[%s] game aborted", self.game_id)
            return
        winner = state.get("winner")
        if winner is None:
            result = "draw"
        elif (winner == "white") == (self.my_color == chess.WHITE):
            result = "win"
        else:
            result = "loss"

        subs = {"opponent": self.opponent}
        h2h = self.stores.nemesis.head_to_head(self.opponent)
        # how the flag fell matters to a soldier: pick the situated ending
        # pool for the status; missing pools fall back to the plain result
        finish_map = {
            "win": {"mate": "win_mate", "resign": "win_resign",
                    "outoftime": "win_time", "timeout": "win_time"},
            "loss": {"mate": "loss_mate", "resign": "loss_resign",
                     "outoftime": "loss_time", "timeout": "loss_time"},
            "draw": {"stalemate": "draw_stalemate"},
        }
        trigger = finish_map.get(result, {}).get(status, result)
        # a win over an opponent who led the series deserves its own line
        if result == "win" and h2h[1] > h2h[0]:
            trigger = "revenge_win"
        self._say(trigger, subs, force=True)
        spec = self.persona.compose(f"spectator_{result}", force=True)
        if spec:
            self.api.chat(self.game_id, "spectator", spec)

        self.stores.stats.record(result)
        if result == "loss":
            self.stores.nemesis.add_loss(self.opponent)
        elif result == "win":
            self.stores.nemesis.add_win(self.opponent)

        # hand the whole battle to the learner
        try:
            self.stores.learner.report_game(
                result, status, self.game_moves,
                self.my_color == chess.WHITE, self.opponent)
            log.info("[%s] learning state: %s", self.game_id,
                     self.stores.learner.summary())
        except Exception as e:
            log.warning("[%s] learning report failed: %s", self.game_id, e)

        try:
            self._archive(result, status)
        except Exception as e:
            log.warning("[%s] archival failed: %s", self.game_id, e)

        log.info("[%s] finished: %s (%s) | career: %s",
                 self.game_id, result, status, self.stores.stats.summary())

    # ------------------------------------------------------------------

    def _maybe_time_pressure(self, state: dict) -> None:
        if self.time_talked:
            return
        opp_ms = (state.get("btime") if self.my_color == chess.WHITE
                  else state.get("wtime"))
        if opp_ms is not None and opp_ms < 15_000:
            self.time_talked = True
            self._say("time_pressure", {"opponent": self.opponent})

    # triggers that stay bots-only: no unsolicited heckling of humans
    HUMAN_QUIET = {"taunt", "time_pressure", "repeat_mistake", "last_stand"}

    def _say(self, trigger: str, subs: dict | None = None,
             force: bool = False) -> None:
        if not self.opponent_is_bot and trigger in self.HUMAN_QUIET:
            return
        text = self.persona.compose(trigger, subs or {}, force=force)
        if text:
            self.api.chat(self.game_id, "player", text)

    def _converting(self) -> bool:
        """Clearly ahead per our last two own-searches: cash in the win."""
        return (len(self.recent_scores) >= 2
                and min(self.recent_scores[-2:]) >= 250)

    def _archive(self, result: str, status: str) -> None:
        """Post-game archival: one PGN with [%eval]/[%clk] comments on our
        moves, plus one JSONL summary line. Runs after the game, so the
        search never pays for it."""
        if not self.game_moves:
            return
        we_white = self.my_color == chess.WHITE
        if result == "draw":
            result_str = "1/2-1/2"
        else:
            white_won = (result == "win") == we_white
            result_str = "1-0" if white_won else "0-1"

        game = chess.pgn.Game()
        game.headers["Event"] = "Rated blitz" if self.opponent_is_bot else "Casual game"
        game.headers["Site"] = f"https://lichess.org/{self.game_id}"
        game.headers["Date"] = time.strftime("%Y.%m.%d")
        game.headers["White"] = self.white_name
        game.headers["Black"] = self.black_name
        game.headers["Result"] = result_str
        if self.tc:
            game.headers["TimeControl"] = (
                f"{self.tc.get('initial', 0)}+{self.tc.get('increment', 0)}")
        if self.initial_fen != chess.STARTING_FEN:
            game.setup(chess.Board(self.initial_fen))

        node = game
        ours = iter(self.our_moves)
        for ply, uci in enumerate(self.game_moves):
            node = node.add_variation(chess.Move.from_uci(uci))
            if (ply % 2 == 0) != we_white:
                continue  # not one of ours
            rec = next(ours, None)
            if rec is None:
                continue
            parts = []
            ev = rec.get("eval")
            if ev is not None:
                ev_white = ev if we_white else -ev  # [%eval] is White-POV
                parts.append(f"[%eval {ev_white / 100:.2f}]")
            clk = rec.get("clock")
            if clk is not None:
                s = clk / 1000.0
                parts.append(f"[%clk {int(s // 60)}:{s % 60:04.1f}]")
            if rec.get("book"):
                parts.append("book")
            if rec.get("style"):
                parts.append("style")
            if parts:
                node.comment = " ".join(parts)

        os.makedirs(os.path.join("data", "pgn"), exist_ok=True)
        pgn_path = os.path.join(
            "data", "pgn",
            f"{time.strftime('%Y%m%d_%H%M%S')}_{self.game_id}.pgn")
        with open(pgn_path, "w", encoding="utf-8") as f:
            f.write(str(game))

        summary = {
            "ts": int(time.time()),
            "game_id": self.game_id,
            "opponent": self.opponent,
            "opponent_is_bot": self.opponent_is_bot,
            "opponent_rating": self.opponent_rating,
            "we_are_white": we_white,
            "time_control": self.tc,
            "result": result,
            "status": status,
            "plies": len(self.game_moves),
            "book_hits": self.book_hits,
            "style_picks": sum(1 for m in self.our_moves if m.get("style")),
            "last_stand": self.last_stand,
            "genes": self.stores.learner.genes(),
        }
        with open(os.path.join("data", "games.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(summary) + "\n")
        log.info("[%s] archived: %s", self.game_id, pgn_path)

    def _persona_params(self, my_ms: int) -> dict:
        """Merge config persona with learned genes; tone down bravado in low time."""
        params = dict(self.cfg.get("persona", {}))
        params.update(self.stores.learner.genes())
        if my_ms < 20_000:
            params["style_pick_prob"] = params.get("style_pick_lowtime",
                                                   params.get("style_pick_prob", 0.0))
        return params

    def _phase(self) -> str:
        plies = self.moves_applied
        if plies <= 20:
            return "opening"
        if plies <= 60:
            return "middlegame"
        return "endgame"

    def _budget(self, my_ms: int, my_inc_ms: int, opp_ms: int | None = None,
                out_of_book_first: bool = False) -> float:
        s = my_ms / 1000.0
        inc = my_inc_ms / 1000.0
        budget = s * self.time_fraction + inc * 0.4
        # learned lesson: spend relatively more time in the endgame
        if self.moves_applied > 60:
            budget *= float(self.stores.learner.genes().get("endgame_time_mult", 1.0))
        budget = max(budget, self.min_time)
        budget = min(budget, self.max_time)
        # first real decision after the book: the opening is where games
        # are quietly lost, so this one move earns a deeper look
        if out_of_book_first:
            budget = min(budget * 1.5, self.max_time * 1.5)
        # the opponent is dying on the clock: keep the pace up. Every
        # second we burn is a second they get to breathe and think
        if opp_ms is not None and opp_ms < 8000 and my_ms > 20_000:
            budget = min(budget, 0.8)
        if s < 20:
            budget = min(budget, max(0.3, s / 25))
        if s < 5:
            budget = min(budget, 0.3)
        # network overhead, measured: an EMA of real move-POST round-trips
        # (clamped) replaces the fixed 300ms guess - a slow night buys
        # less think time, a fast night banks it
        ov = max(0.2, min(1.0, float(getattr(self.api, "move_overhead", 0.3))))
        think = budget - ov
        if s < 0.8:
            # dead clock: live off the increment, absolute minimum think
            return max(0.1, min(think, s - 0.25))
        # every move completes at least a couple of plies of search
        return max(0.3, think)
