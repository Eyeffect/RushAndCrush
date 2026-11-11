# RushAndCrush — Technical System Document

*Hand this document to a reviewer (human or LLM) alongside a request like:
"Review this architecture and suggest concrete improvements. Prioritize by
impact." It is intentionally self-contained; no source code access needed.
State reflects the bot as of Nov 2026: ~76 rated games played, blitz
rating 1523–1563.*

---

## 1. Purpose

RushAndCrush is an autonomous Lichess chess bot with a fixed persona: a
reckless attacking soldier that "always pushes and almost never defends."
It hunts matches by itself, plays them with a **homemade engine** (no
Stockfish, no NNUE, no external chess AI), expresses a rule-based
personality in game chat, and **learns from its own games** by adjusting
its own evaluation genes and maintaining a results-graded opening book.

Design goals, in order:
1. Full ownership: every chess decision is made by code we wrote.
2. Entertain: personality must be visible in both play style and chat.
3. Autonomy: hunts, accepts, plays, learns, repeats — unattended.
4. Robustness over strength: it should never crash, flag, or spam the API;
   being ~1500–1800 strength is acceptable for a personality bot.

## 2. Runtime environment

- Windows 10/11 console process. **Production runs on PyPy 3.12
  (v8.0.1-win64)** — roughly 2× the CPython node rate, which matters at
  pure-Python speeds. CPython 3.12 is used for development and the smoke
  suite (which must stay green on both).
- Dependencies: `python-chess` (move generation, legality, PGN),
  `requests`, `PyYAML`. Nothing else.
- Account: dedicated Lichess account upgraded to a **BOT account**
  (irreversible). OAuth token read from env var `LICHESS_BOT_TOKEN`
  (never committed; the current token has broader scopes than needed and
  should eventually be regenerated with only `bot:play`).
- Lichess bot-account rules that shape the design: rated games only
  bot-vs-bot; humans can only challenge bots casually; no ultraBullet;
  a daily cap (`bot.vsBot.day`) on bot-vs-bot games enforced via 400/429
  challenge responses with a `ratelimit.seconds` payload.

## 3. Architecture overview

Single process, five concurrent thread roles, shared in-memory + JSON
state. No asyncio; blocking `requests` streams with socket read timeouts.

```
main.py (main thread)
  ├─ event stream loop: GET /api/stream/event (NDJSON, reconnect w/ backoff)
  │    ├─ gameStart      → spawn GameHandler (reader thread)
  │    ├─ gameFinish     → logged (handler also sees it on its own stream)
  │    ├─ challenge      → spawn decision thread → hunter.decide_incoming()
  │    │                   → api.accept() / api.decline(reason)
  │    └─ challengeDeclined → hunter.on_declined() (cooldown memory)
  ├─ on boot: GET /api/account/playing → resume handlers for ongoing games
  └─ Hunter thread (matchmaking daemon)
        ├─ every 15s tick: if capacity and cooldown ok → challenge someone
        └─ nemesis list prioritized over fresh random bot targets

GameHandler — READER thread (one per live game)
  game stream: GET /api/bot/game/stream/{id} (NDJSON)
    gameFull → colors, greeting (player+spectator), sync board
    gameState→ apply state under the handler lock; wake worker if our turn
    chatLine → logged only
  The reader NEVER thinks: stream keeps flowing while we search.

WORKER thread (one per game, spawned by the reader)
  wait on condition → snapshot state → stop ponder → think →
  verify the world hasn't moved on → commit → POST move → chat →
  start ponder for the predicted reply

PONDER thread (transient, at most one per game)
  engine.search(ponder=True): TT warming on the opponent's clock
```

Shared state (all JSON, atomic writes via tmp-file + `os.replace`):
- `data/stats.json` — career W/L/D
- `data/nemesis.json` — head-to-head per opponent (revenge targeting)
- `data/hunter.json` — outgoing challenge attempts (anti-spam memory)
- `data/learning.json` — genes, opening book, evolution window
- `data/blunders.jsonl` — one line per blunder (phase, clock, style-pick,
  **fen + uci + eval drop**) — the replay-regression corpus
- `data/pgn/*.pgn` + `data/games.jsonl` — post-game archive (never read
  at runtime)

**Concurrency invariants** (the rules that keep the bot deterministic):

1. The reader owns board sync: `board`, `moves_applied`, `game_moves`,
   `wtime/btime/incs`, `server_status`, `done`. Mutated only under the
   handler `lock`; board sync is idempotent (already-applied moves are
   skipped; illegal/regressed moves rebuild from `initialFen`).
2. The worker owns all thinking state: `our_moves`, `book_hits`,
   `pawn_storm_count`, `recent_scores`, `lost_streak`, `last_stand`,
   `last_search_score`, `first_blood_done`, `our_queen_alive`,
   `comeback_done`, `bad_move_streak`. No reader code touches these.
3. The worker decides on a **snapshot** (ply, clocks,
   `board.copy(stack=False)`, move list) taken under the lock; everything
   after that runs lock-free.
4. Commit is **verify-then-commit**: under the lock, the worker confirms
   the game is alive and `moves_applied == ply`. A state that moved on
   mid-think discards the whole result ("stale search discarded") instead
   of feeding the server a 400.
5. Network and file I/O happen outside the lock. `POST move` failure
   invalidates `last_search_score` (a phantom eval must not become a
   lesson).
6. `_stop_ponder` sets the event and joins **outside** the lock — the
   reader never waits on a search.
7. One `Engine` object is shared across all game handlers. `search_lock`
   serializes searches; the ponder protocol (5.1) lets background
   searches coexist without delaying real ones.
8. Persona composition has its own lock; chat HTTP happens outside game
   locks. Chat can originate from reader (start/finish/time-pressure) and
   worker (mid-game triggers) threads — that is fine by design.

## 4. Lichess API contract used

Endpoints (all with `Authorization: Bearer`):
- `POST /api/token/test` — token self-test (token in request **body**);
  response `{token: {scopes, userId}}`; requires `bot:play` scope.
- `POST /api/bot/account/upgrade` — one-time irreversible bot upgrade.
- `GET /api/account`, `GET /api/account/playing` — profile, resume list.
- `GET /api/stream/event`, `GET /api/bot/game/stream/{id}` — NDJSON
  streams; blank lines are pings; read timeout 25s acts as liveness check.
- `POST /api/bot/game/{id}/move/{uci}?offeringDraw=` — play move.
- `POST /api/bot/game/{id}/resign`, `/abort`; `POST /api/bot/game/{id}/chat`
  (form `room=player|spectator`, text clamped to 140 chars).
- `POST /api/challenge/{username}` — create challenge (payload:
  `rated`, `variant`, `clock.limit`, `clock.increment`, `color`).
- `POST /api/challenge/{id}/accept`, `/decline` (form `reason=...`;
  reason is a Lichess enum like `generic`, `later`, `timeControl`,
  `standard`, `rated`).
- `GET /api/bot/online?nb=512` — online bots (NDJSON, includes perfs/ratings).
- `GET /api/user/{name}`, `GET /api/users/status?ids=` — opponent intel.

Retry policy: one shared `_request` wrapper. **Non-move traffic** walks
the long ladder (1,2,4,8,15s) on 429/5xx/connect errors/timeouts; 4xx
raises immediately. **Move POSTs are clock-aware**: they use a short
ladder (0.2/0.5/1.0s) with a 2.5s timeout — worst case ~9s of wall clock,
after which the handler discards the eval and the next gameState
resyncs — because a doomed 30s retry march is how you forfeit on time.
Streams reconnect at the main loop with 5s→120s exponential backoff. A
move POST that lands after the game ended server-side surfaces as a 4xx,
is logged as a warning, and discards the eval (5.2) — observed only
during flaky network windows, never as a strength regression.

Every successful move POST also updates an EMA of the measured
round-trip (`api.move_overhead`, clamped 0.15–1.5s); the time budget
consumes that number instead of a fixed guess (5.2).

## 5. Component specifications

### 5.1 Engine (`engine.py`) — the chess brain

Board representation: python-chess `Board`; search works on
`board.copy(stack=False)`. No bitboards of our own (python-chess's
internal bitboards are read directly in eval), no NNUE. This caps speed
at roughly 25–35k nodes/s warmed on PyPy (~half on CPython); typical
blitz search reaches depth 5–8, 9–13 in quiet endgames with bigger
budgets.

**Evaluation** (static, White-POV; negamax flips sign):

```
score = tapered blend of:
          Σ pieces: MG/EG material + PeSTO piece-square table
          + bishop pair, rook open/semi-open files, passed pawns (tapered)
        + aggression proximity rings   (learnable multipliers)
        + mop-up (winning vs bare king)
        + king shield                  (learned gene, usually small)
```

- **Tapered PeSTO baseline** (Rofchade tables via CPW): MG values
  {P 82, N 337, B 365, R 477, Q 1025}, EG {P 94, N 281, B 297, R 512,
  Q 936}. Tables are baked at import: `material + CAP·tanh(pos/CAP)`
  with `CAP = 150` — the persona soft cap that keeps small bonuses
  linear and saturates big ones (the soldier prefers advancing, never
  at piece-sacrifice delusions). CPW prints tables a8-first; the
  a1=0 flip happens once in `_build_psq` (`table[sq ^ 56]`); black's
  tables are mirrored (`square_mirror`). Pawn tables are rebuilt per
  learned `pawn_adv` gene value and cached.
- **Phase blend**: `phase = min(24, N + B + 2R + 4Q)`;
  `total = mg·phase + eg·(24 − phase)`, divided with **symmetric
  rounding** so `eval(mirror) == −eval` exactly (plain floor division
  biased negatives by one cp — a smoke test caught this).
- **Eval extras** (tapered, mirror-symmetric): bishop pair 30/50
  (separate ifs so both sides can hold one); rook on open file 25/35,
  semi-open 10/15; passed pawns by relative rank, MG (0,5,10,20,35,60,
  100,0), EG (0,10,15,25,40,70,120,0), detected via precomputed
  front-span bitboards.
- **Aggression proximity** (genes `aggression` ∈ [0,1], `prox`): every
  pawn / other piece within Chebyshev distance d ∈ 1..5 of the *enemy
  king* adds `(6 − d) × (3 pawns / 2 pieces) × aggression × prox`,
  via precomputed `RING[d][king_sq]` bitboards.
- **Mop-up**: ahead by ≥ 500 cp with the opponent pawnless →
  `10 × center_distance(losing king) + 4 × (14 − king_manhattan)`
  (drive the bare king to the edge, bring ours closer).
- **King shield** (gene `king_shield`, 0 → grows only via mate losses,
  cap 0.5): non-endgame, each side missing pawns on its king's three
  front squares loses `(shields − 3) × 12 × king_shield`. The
  "endgame" switch (total non-pawn material < 1600) gates it off.
- **Deliberately absent**: mobility, pawn structure beyond passers,
  tempo. Defense is only valued through the learned shield. This is
  the persona, not an oversight.

**Eval cache**: leaf positions repeat constantly through qsearch, so a
per-search dict maps an 8-tuple of bitboards (pawns, knights, bishops,
rooks, queens, kings, occupied, turn — **no castling/ep rights**, which
do not affect placement eval) to the score. The cache is bound to the
persona dict *by identity* (`is not`): genes are constant within one
search, so a fresh dict invalidates it. Cleared every search.

**Search protocol** (`search()` wrapper) — this is the ponder machinery:

- Real searches increment `_urgent` (under `_urgent_lock`) *before*
  blocking on `search_lock`.
- Ponder searches (`ponder=True`) acquire `search_lock` with
  `timeout=1.0` and refuse to run if `_urgent > 0`. A ponder therefore
  never queues behind a real search and never delays one.
- While pondering, **both** `_negamax` and `_quiesce` check
  `self._urgent > 0 or stop_event.is_set()` every 2048 nodes (the node
  counter is shared, so the abort may land in either function) and
  raise `_SearchTimeout`.
- Measured preemption cost when a real search arrives mid-ponder:
  ~100 ms.

**Root search** (`_search_locked`):

- Persistent TT: it survives across moves (overlapping positions are
  the whole point) and is cleared only when it outgrows `TT_MAX`
  (500k entries). The store is **always-replace**; the *probe* is
  depth-gated (`e_depth >= depth`). Killers, eval cache and node
  counter reset per search; **history persists but is halved each
  search** (aging — fresh games outvote stale bonuses);
  `stop_at = now + budget`.
- Repetition history: the handler passes `_tt_key` counts of the real
  game so far; the search maintains `_rep_path` during recursion.
- Iterative deepening 1..40. Root moves shuffled once per search (game
  variety), re-sorted by the previous depth's scores thereafter.
- **Soft time limit (Batch C #3, measured):** a new depth is started only
  if it is *predicted to finish* — the next iteration costs ≈1.6× the last
  one, and if `now + 1.6 × last_depth_time` would overshoot the deadline,
  the search stops and returns the last **completed** depth instead of
  burning the clock on a partial iteration that gets thrown away.
  Measured in the blitz-clock harness (`match.py --bank 180 --inc 2
  --frac 0.05 --min-move 0.35 --max-move 8`): the old engine burned its
  entire bank and **flagged in 24 of 32 games**; the new engine used
  ~9% less clock and **never flagged** → **Elo −293 ± 166** for the old
  engine. At shallow budgets (128 games) the change is strength-neutral
  (−19 ± 60) with an identical blunder-replay profile (17/33), i.e. it
  costs nothing and buys clock. A "stop when the best move looks stable"
  variant was written and **rejected**: it needs depth ≥ 8, which
  production budgets never reach, so it was unmeasurable and stayed out.
- **Futility pruning + one `is_check()` per node (Batch C #4):** at depth
  ≤ 2 a position so far below beta that no free move could save it is
  cut immediately, and the three `is_check()` calls per node collapsed
  into one. Speed: ~8.6k → ~14k nps on PyPy at equal node counts.
  Fixed-time strength is even (+22 ± 121 for the old engine over 32
  games) — the gain is pure speed, which pays off only in the clock
  regime (see #5).
- **Staged movegen (Batch C #5):** each node peeks one legal move for
  the mate/stalemate test, then generates captures first (plus quiet
  promotions, deduplicated against capture-promotions — a pre-existing
  qsearch bug that searched each capture-promotion twice) and builds
  the quiet list only if the capture stage fails to cut. Speed:
  ~8.6k → ~19.5k nps (2.27× baseline). Movegen coverage verified
  exactly (399 random positions, `verify_movegen.py`). Measured in the
  production clock regime (180s bank, 2% policy): **Elo −145 ± 107 for
  the old engine over 48 games** — the largest measured gain so far,
  shipped with #4 as one package.
- **Lesson from the 30–0 sweep:** the first staged-movegen draft
  double-incremented the move counter, so the first move never got the
  full PVS window; in quiet positions every move failed low and
  `best_move` was picked among meaningless scout upper bounds → the
  candidate lost every game by mate. The blunder replay did NOT catch
  it (in tactical positions the first move beats alpha and re-searches
  exactly) — the full match did. Gate on matches, not on the replay.
- **Aspiration on beta only**: `beta = best_score + 40` from depth ≥ 3;
  a fail-high re-searches the same depth with an infinite window (exact
  scores are what the style layer needs).
- Root alpha rides at `best_so_far − style_slack − 1`, so every rival
  within the style slack of the best gets an **exact** score; moves
  outside it are fail-low bounds and are excluded from style picks.
  Top-6 moves + scores returned from each fully completed depth.
- Timeout unwinds via `_SearchTimeout`; the root handler **drains the
  leaked `push()` stack** (`while work.move_stack: work.pop()`) — a real
  bug found by the smoke suite: the exception skipped every level's
  `pop()`, and the reused board object would otherwise carry garbage.
  A real deadline keeps the previous depth's result; a **ponder abort
  returns immediately** (a second bug the suite caught: the root used to
  treat ponder aborts like time-outs and started the next depth, still
  squatting on the lock it was told to vacate).
- Mate found (`score > MATE − 1000`) → return immediately.

**Node search** (`_negamax`):

- Constants: `MATE = 1_000_000`, `MATE_BOUND = 990_000`, `INF = 10M`,
  `CONTEMPT = 50`.
- **Repetition & 50-move, scored with contempt**: a position seen once
  before (search path or real game) makes a third occurrence trivially
  reachable — winning sides must not walk in circles, losing sides
  happily shake hands. Score = `−CONTEMPT` if static eval > +100,
  `+CONTEMPT` if < −100, else 0 (side-to-move POV). Repetition scores
  are path-dependent and are **never stored in the TT**.
- TT probe: 10-tuple bitboard key (pawns, knights, bishops, rooks,
  queens, kings, `occupied_co[WHITE]`, turn, castling rights, ep square
  — faster than recomputing zobrist). Mate scores stored **ply-anchored**
  and re-anchored on probe (`± ply`) so any node re-derives the true
  distance. Flags exact/lower/upper; replacement is always-replace
  (probe is depth-gated).
- One movegen per node (legal list drives mate/stalemate detection and
  the move loop). No legal moves → `−MATE + ply` if in check else 0.
- **Null-move pruning**: depth ≥ 3, not in check, side to move has
  non-pawn non-king material, static eval ≥ beta, beta not near mate →
  push `Move.null()`, search `depth − 3` with `can_null=False`; ≥ beta
  → return beta (zugzwang guard: pawn-only endings are excluded).
- **PVS**: first move full window; the rest get a null-window scout
  (`−alpha−1, −alpha`), re-searched full-window on surprise.
- **LMR**: depth ≥ 3, move index ≥ 4, quiet move (no capture/promotion),
  no check given, not in check → reduce by
  `r = 1 + (idx ≥ 8) + (depth ≥ 6)`, null-window probe, full re-search
  if it beats alpha.
- **Check extension**: +1 ply when the move gives check and remaining
  depth ≥ 1 (the attacker digs deeper through checks).
- **Quiet-move killers** (2 per ply) and history
  `history[(piece_type, to_sq)] += depth²` on beta cutoffs.
- TT store: `(depth, flag, ply-anchored score, best_move)` — written
  unconditionally (always-replace; the depth gate lives on the probe).

**Quiescence** (`_quiesce`):

- `ply ≥ 96` safety valve.
- **In check there is no stand-pat**: every evasion is searched, and no
  evasions → `−MATE + ply`. This is what makes qsearch *see* mated
  leaves (the old version evaluated a lost position as if nothing had
  happened — a real horizon bug, now fixed).
- Not in check: stand-pat, then captures **plus quiet queen promotions**
  (a pawn on the 7th crowing is exactly what the horizon hides),
  MVV-LVA order (`10×victim − attacker`, +500 for promotions).
- **Delta pruning**: if `stand + victim_value + 800·promo + 200 ≤ alpha`
  the capture is skipped without a make/unmake.

**Move ordering** (`_order`): TT move 10M > capture 1M + MVV-LVA >
promotion 900k > killer 400k/350k > advanced pawn push +20k (to-rank
≥ 4 White / ≤ 3 Black) > history. Note: no `gives_check` in ordering —
python-chess implements it as push/is_check/pop (a full make/unmake per
move), and the check extension already rewards checks after push.

**Style layer** (`pick_move`): the dice are rolled **before** the
search. With probability `style_pick_prob` the search runs with the
full persona slack; on a miss (~75% of moves) the root window tightens
to `best − 1`, so no ray is kept open for rivals that will never be
picked — measurably deeper search (+~0.6 ply in a 1s/smart-position
benchmark), identical persona behavior. On a hit, rivals within
`style_eval_slack` cp of the best (exact root scores only) compete on
**violence score**: capture 30 + victim_value/10; promotion +15; pawn
landing ranks 4/5/6 → +8/+14/+22 (mirrored for Black); resulting check
+25; checkmate +1000; landing within 4 of the enemy king → +4 per step
closer. Sets a `style_pick` flag the learner uses to attribute later
blunders.

### 5.2 Game handler (`game_manager.py`) — reader/worker per game

Reader responsibilities and board sync are described in §3. The
pipeline below runs in the worker on a snapshot.

**Move pipeline**:
1. **Learned book**: `get_book_move(moves, we_are_white)` (exact
   prefix key, depth 8 plies, min 3 samples, score ≥ 0.45 — see 5.5);
   legality re-checked against the snapshot.
2. Budget (below), persona params (`config persona` merged with learned
   genes; clock < 20s swaps `style_pick_prob` ← `style_pick_lowtime`).
3. **Within-game adaptation**:
   - `last_stand` (3 consecutive own searches ≤ −1200): budget ≤ 0.2s,
     `style_pick_prob = 1.0`, slack 10000 (maximum violence, minimum
     clock), one-time chat line, blunder bookkeeping suppressed ("a
     lost position is not a classroom"). A soldier does not resign.
   - `_converting` (both of the last two own searches ≥ +250): turn the
     assault down ×4 (`aggression` and `style_pick_prob`), budget ×0.6
     (play quick, cash in).
   - Exactly one legal move and not last stand → play it instantly.
4. `pick_move` (search + style layer) with repetition keys of the real
   game (`_rep_keys`, so contempt works from move one).
5. Score bookkeeping: `recent_scores` (last 2), `lost_streak`,
   `last_stand` transitions. Blunder/taunt hygiene: skip positions
   already decided (`|prev_eval| > 600`, mate-scale scores, last stand).
   Drop ≥ 250 cp vs our previous search → our previous move was a
   mistake → `record_blunder(phase, clock, was_style_pick, fen, uci,
   drop)`; the same phase hitting 3 mistakes → `repeat_mistake` line.
   Rise ≥ 250 → taunt. **Comeback trigger**: previous eval ≤ −250 and
   new ≥ +50 → one crow per game.
6. Situation flags read pre-push: capture, captured queen, moved pawn,
   castling, en passant, `fen_before`, `our_queen_fell` (queen absent
   after the opponent's move — fires exactly once). Push locally.
7. **Verify-then-commit** under the lock (invariants 4–5); on commit:
   board, `moves_applied = ply + 1`, move list, `our_moves` record
   `{uci, eval, clock, style, book, fen}`.
8. POST outside the lock; failure → log, `last_search_score = None`.
9. Post-move chat triggers (see 5.3); blunder ledger call; ponder start:
   `ponder_enabled and score is not None and my_clock > 10s and not
   last_stand`.
10. Optional resignation (3 consecutive own searches ≤ −1200 after
    ply 50) — **disabled by config**: "a soldier does not surrender."

**Time budget** (`_budget`):
`budget = clock·0.05 + increment·0.4`, × learned `endgame_time_mult`
after ply 60, clamped [0.35, 8.0]s. First decision out of the book
×1.5 (openings are where blitz games are quietly lost). Opponent < 8s
while we > 20s → cap 0.8s (keep the pressure on). Own clock < 20s →
`min(budget, max(0.3, s/25))`; < 5s → ≤ 0.3s. Minus the **measured**
network overhead (EMA of move-POST round-trips, clamped 0.2–1.0s —
§4); dead clock (< 0.8s) lives off the increment at ≥ 0.1s.

**Pondering**: after our move posts, one background search of the
position after our move (predicted reply), ≤ 10s, params as if 60s
clock, repetition keys of the *predicted line* so the TT warms exactly
the positions the next real search will hit. Nothing is played — logs
`ponder: expecting e2e4 (eval -90, depth 8)`; mate-in-1 predictions
appear as `eval 999991`. Aborted within ~2048 nodes by the next real
search or game end (protocol in 5.1). The shared TT makes this pure
profit for the next move.

**Finish** (`_finish`): mark `done` under the lock and stop the ponder
*before anything else* (a worker mid-search then discards instead of
posting to a dead game). Status `aborted` → skip. Result from `winner`.
Forced farewell chat is **situated by how the flag fell**: win_mate /
win_resign / win_time / loss_mate / loss_resign / loss_time /
draw_stalemate (missing pool → plain result pool), with `revenge_win`
overriding when we beat an opponent who led the series. Spectator
farewell; `stats.record`; nemesis `add_loss`/`add_win`;
`learner.report_game(result, status, all_moves, we_are_white,
opponent)`.

**Archival** (post-game only, never paid at search time): one PGN per
game with `[%eval]` / `[%clk]` / `book` / `style` comments on our moves,
plus one JSONL summary (opponent, rating, TC, result, book hits, style
picks, last stand, gene snapshot). This archive is the future Texel
tuning corpus.

The old "resume race" (restart → first move posted against a pre-sync
position → 400) is **fixed** by verify-then-commit: the worker's commit
check `moves_applied == ply` fails and the stale search is discarded;
the next gameState triggers a fresh one. The only 400s observed since
are POST retries landing after the game ended server-side (network
flakiness), which self-heal and discard their evals.

### 5.3 Persona (`persona.py`) — rule-based chat brain

Trigger → line banks live in `config.yml` (33 triggers, 131 lines, all
≤ 140 chars, validated by the smoke suite): game_start (+ nemesis /
rematch / grudge variants chosen by head-to-head), taunt, self_blunder,
repeat_mistake, queen_lost/taken, check_given, pawn_storm, time_pressure,
last_stand, first_blood, castle, promotion, en_passant, comeback,
situated endings (win/loss × mate/resign/time, draw_stalemate),
revenge_win, spectator_*.

`compose(trigger, subs, force)`:
- gates: global min gap 25s between any messages, per-trigger cooldown
  120s, 35% random skip; `force=True` bypasses gates (game start/end).
- line selection: shuffled bag per trigger — **no line repeats until
  its whole pool has been used**; `{me}`/`{opponent}` templating.
- unknown trigger / disabled chat → silent (graceful fallback for new
  situated triggers on old configs).

**Bots-only rule**: `taunt`, `time_pressure`, `repeat_mistake`,
`last_stand` are never sent to humans — unsolicited heckling is reserved
for bots (`HUMAN_QUIET` gate on opponent title). Situated events
(first blood, castling, promotions, queen funerals) go to everyone; the
soldier is loud but not rude.

### 5.4 Hunter (`hunter.py`) — matchmaking daemon

Tick every 15s. Conditions to hunt: capacity (live games < `max_games`
= **1**), ≥60s since last outgoing challenge, not paused by rate limit.

Why one game at a time: there is one engine and one `search_lock` —
two simultaneous searches would halve each other's depth and, worse,
make ponder preemption meaningless. Serial games also make the learning
signal cleaner. (The reader/worker split already tolerates concurrency;
two handlers were run in testing without errors.)

1. **Nemesis priority**: most-beaten bot opponent whose cooldown (30 min,
   from the loss) expired and who still leads the series → challenge
   again, **only** if we haven't tried or played them recently and the
   per-day rematch cap (2/day, bots-only, anti-boosting) allows it.
   Humans get peace.
2. Else: `GET /api/bot/online` (512) → filter: not self, not tried in
   the last 15 min, not *played* in the last 2 h, blitz rating reliable
   (≥5 games), rating within window. Window starts 600–2200; once our
   own blitz rating has ≥5 games it re-centers to [own−250, own+350]
   (clamped, refreshed every 10 min). Provisional 3000 default ratings
   are ignored. Candidates are picked **weighted by staleness** (longest
   since last challenge), so the soldier rotates faces instead of looping
   familiar nicknames.
3. Challenge: random time control from {[180,0],[180,2],[300,0],[300,3]},
   rated vs bots / casual vs humans, random color. Error carrying
   `ratelimit.seconds` (`bot.vsBot.day` daily cap) → pause hunting for
   that duration. Every finished game records the opponent in hunter
   memory (`data/hunter.json`) so even incoming games enforce rotation.
4. Incoming challenges (`decide_incoming`): non-standard variant →
   decline `standard`; ultraBullet/bullet/correspondence → decline
   `timeControl`; no capacity → `later`; bots → accept (rated fine);
   humans → must be casual. Declines feed the 15-min memory.

### 5.5 Learning (`learning.py`) — memory + drill sergeant

One JSON file, five mechanisms:

**Seeded repertoire** (`seed_book`): at first boot the opening book is
seeded with aggressive full variations (per color) filed as if they had
been played — the white doctrine "always 1.e4" is a seed line, so the
bot plays it from game one. Seeds then live or die by real results like
any other book line.

**Genes** (defaults): `pawn_adv=1.0` (multiplies pawn PST),
`prox=1.0` (multiplies proximity term), `king_shield=0.0`,
`style_pick_prob=0.25`, `style_eval_slack=45`, `style_pick_lowtime=0.25`,
`endgame_time_mult=1.0`. Genes are merged over the static persona config
at every move. Observed drift: `king_shield` 0 → ~0.5 after mate-heavy
loss streaks.

**Blunder ledger → immediate lessons** (`record_blunder`): counters by
phase (opening ≤20 plies, middlegame ≤60, endgame >60), time trouble
(clock <20s at move time), style-pick attribution, plus the fen/uci and
eval drop of the offender (for future position-level analysis).
- ≥6 blunders and style-pick share >50% → `style_pick_prob` ×0.8 (floor 0.05).
- ≥5 blunders and time-trouble share >40% → `style_pick_lowtime = 0`.
- ≥8 blunders and endgame share >60% → `endgame_time_mult = 1.3`.
- Returns whether this phase's mistake count hit 3 (persona quips).

**Mate lessons** (`report_game`): loss by `status="mate"` →
`king_shield += 0.05` (cap 0.5) — the only path to learning defense;
win by mate → `king_shield −= 0.02` (floor 0).

**Results-graded opening book** (Batch D #7): keyed by **position**
(zobrist hash, 16 hex digits via `chess.polyglot.zobrist_hash`), not by
move prefix — transposed move orders pool their samples (1.e4 e5 2.Nf3
and 1.Nf3 Nc6 2.e4 e5 are the same entry). Filing and lookup reach
`book_deep_plies` (16); a move past the core `book_depth` (8) needs
`book_deep_min_samples` (5) games before it is followed. Each result is
**weighted by opponent strength**: `weight = 1 + max(0, opp − me)/800`,
stored as `{w, l, d, pts, wgt}` with score `pts/wgt` (old raw {w,l,d}
slots keep scoring raw; slots are converted lazily on first write).
Lookup: position key, turn must be ours, `n ≥ min_n`, score ≥ 0.45;
best score wins; otherwise fall through to search (a line that only
loses is "cursed" → the engine thinks for itself). Old prefix-keyed
books migrate on boot: each prefix is replayed on a board and re-keyed,
unreadable ones are dropped with a warning. Seed lines are replayed
legally now, so a corrupt seed line is skipped instead of poisoning the
book (the old filer trusted game move lists blindly — one illegal prefix
was found and dropped in the real data).

**Window evolution** (every 10 games; Batch D #6): the fitness signal is
a **z-score** of `Σ(result − expected)`, with the expected score from
the rating gap, `expected = 1/(1 + 10^((opp − me)/400))`, and per-game
Bernoulli variance `expected·(1 − expected)`:
- `z ≥ +1.0` → snapshot current genes (snapshot stores `{z, wr, genes}`);
- `z ≤ snapshot.z − 1.5` → revert genes to snapshot ("drill sergeant");
  legacy wr-only snapshots revert on absolute `z ≤ −1.5`;
- else mutate `style_pick_prob ±0.05` ∈ [0.05, 0.35] and
  `style_eval_slack ±10` ∈ [20, 80] (explore, with revert safety net).
The z-signal is trusted only when at least half the window's games were
rated (with both ratings known) and the window variance is ≥ 0.5;
otherwise the old raw path applies (`wr = (W + 0.5D)/games`; revert if
`wr < snapshot.wr − 0.10`, snapshot if `wr ≥ 0.55`, else explore) —
a ratingless opponent never means no evolution. Window then resets.
**Frozen until 100 games are logged** (`freeze_until_games: 100`):
windows are recorded but evolution does not apply — the early population
is too small to trust with gene drift. The freeze lifted at game 103.

Learning signals are coarse by design (result-level fitness, tiny param
space); there is no gradient/Texel tuning of PSTs yet — parked until
~200–300 archived games exist (5.2 archival is the corpus).

### 5.6 Main loop (`main.py`)

`Bot` wires API + engine + persona + stores + hunter; resumes ongoing
games from `/api/account/playing`; consumes the event stream forever;
reconnects with backoff on network errors; Ctrl+C → clean exit. Incoming
challenge decisions run on short-lived threads so a slow HTTP call never
blocks the stream loop.

### 5.7 Offline tooling — the measuring stick

**`match.py` — engine-vs-engine self-play harness.** Every strength
change must win here before it ships; at live-rating noise (~±40 Elo
per session) unmeasured tweaks are blind.

- Players are specs: `module:nodes` shorthand (e.g. `engine:800` vs
  `engine:4000`) or full JSON `{"module": "...", "params": {...}}`.
  A candidate engine can be copied to `engine_cand.py` and raced
  against the baseline without touching git.
- Games are color-swapped **pairs** on shared seeded random openings
  (`--random-plies`, default 6) — first-move bias cancels.
- Fixed **nodes** per move measure strength-per-node (engine
  efficiency); fixed **time** (`--nodes 0 --time 0.2`) is for
  time-management experiments. Node budgets use the engine's
  `max_nodes` knob (0 = off in production; behavior identical).
- **Clock mode** (`--bank S --inc S --frac F --min-move S --max-move S`)
  models a real blitz clock: each side owns a bank of seconds, a move may
  think `remaining × frac + inc × 0.4` seconds (clamped to
  `[min-move, max-move]`), the clock is drained by the actual think
  time, and a side that runs out loses on time. This is the **only**
  mode in which a time-management change can be measured — saved
  seconds buy deeper searches later, exactly like the live bot
  (`budget = remaining × time_fraction + inc × 0.4`).
- Draw policy: python-chess outcomes (mate, stalemate, insufficient
  material, threefold, fifty-move) + ply-cap adjudication by material
  (|diff| ≥ 300 cp decides, else draw). Repetition keys of the real
  game are passed to the search, so winning sides avoid threefolds.
- Statistics: Elo ± 95% CI (binomial approximation) and an SPRT with
  logistic score model (`--sprt`, bounds 0/25 Elo, α=β=0.05) that
  stops the match once the decision is made. Games run in a
  multiprocessing pool (spawn), workers default to CPU count.
- Validated: identical players → Elo −35 ± 153 over 20 games
  (statistically zero, as it must be); 800 vs 4000 nodes →
  **Elo −150 ± 132** over 32 games (the harness sees real strength
  gaps). ~3s per game at 800–4000 nodes with 8 PyPy workers.
  Clock mode validated separately: identical players → Elo −0 ± 340
  with A 47s / B 48s and 0 flags over 4 games (the clock is fair).

**`replay_blunders.py` — blunder regression suite.** Replays the
ledger (`data/blunders.jsonl`, fen + the move actually played + the
eval drop) against the current engine at a fixed node budget and asks
two questions per entry: does the engine still choose the blunder,
and what does it cost now? Baseline (Nov 11, 8000 nodes/move): 33
replayable, **54.5% still chosen**, mean cost 136 cp (vs 420 cp when
filed — the Batch-1 engine already repairs most of the damage).
`--strict N` fails the run above N% still-played. `--engine <module>`
replays against a candidate engine (e.g. `engine_cand`) instead of the
shipped one. Run after every engine change; a change that raises
either number is suspect regardless of what the Elo match says.

## 6. Configuration surface (`config.yml`)

- `engine`: min/max think time (0.35/8s), time_fraction 0.02,
  `ponder` true, `ponder_max_time` 10s, `ponder_min_clock_ms` 10000.
  Network overhead is no longer configured — it is measured (§4).
  time_fraction went 0.05 → 0.02 after measurement: the greedy policy
  burned a whole 3+2 clock by move ~30 and 42% of games reached their
  endgame under 1s/move; the thrifty policy flagged 0/24 harness games
  where the greedy one flagged 12/24.
- `persona`: aggression 0.9, style_pick_prob 0.25, style_eval_slack 45,
  resign disabled, all chat lines + cadence knobs (25s gap, 120s
  trigger cooldown, 0.35 random skip).
- `hunter`: max_games 1, challenge_interval 60s, idle_check 15s,
  challenge_timeout_min 15, time controls, rating window 600–2200,
  nemesis_cooldown_min 30, played_cooldown_min 120 (every finished
  game puts the opponent on a 2h outgoing-challenge cooldown — the
  same-name spam fix), nemesis rematches bots-only and capped at 2/day.
- `learning`: book_depth 8, book_min_samples 3, book_deep_plies 16,
  book_deep_min_samples 5, window 10, freeze_until_games 100,
  seed_book (per-color opening repertoires).

## 7. Observed behavior so far (~90 rated games)

- Career 38W / 46L / 6D; blitz rating 1523 → 1584 (Nov 11).
  Opponents are online bots in the 1300–1900 range; the rating climbed
  through the Batch 1+2 period and two more wins landed right before
  the daily bot-vs-bot cap paused hunting.
- Search depth typically 5–8 in 3+0/3+2, 9–13 in quiet 5+3 endgames;
  warmed PyPy ≈ 30k nodes/s (`bench_warm.py`). Persistent TT makes the
  first search of each move visibly faster (ponder logs show depth 8–12
  on the opponent's clock).
- Ponder predictions verified live, including exact mate-in-1s
  (`ponder: expecting e6f7 (eval 999991)`) — the predicted mate is then
  played instantly by the next real search.
- LAST STAND fires in dead-lost positions and produces the expected
  kamikaze style (log: "Maximum violence, minimum clock"); resignation
  never triggers (disabled).
- Style picks observed live (~25% of moves), e.g. giving 11cp for a
  knight hop with attacking intent; book hits ~49 in the learned
  ledger; the seed repertoire (1.e4 doctrine) plays from game one.
- Zero "stale search" 400s since the reader/worker split; two
  simultaneous resumed games were handled cleanly in testing. A flaky
  network night caused a few outoftime losses via read timeouts —
  environmental, not a regression.

## 8. Known limitations & risks (honest list)

**Strength / engine**
1. Pure-Python speed ceiling (~25–35k nps warm PyPy) → depth 5–8.
   Fundamental without C/Rust movegen or NNUE; question: is further
   investment worth it for a personality bot, or is 1500–1800 the right
   home?
2. Aggression proximity can still prefer keeping a piece near the enemy
   king over saving it; the PeSTO base and qsearch mitigate (this no
   longer causes random hangs like the pre-PeSTO eval did), but the
   term remains persona-risky.
3. No SEE; delta pruning is naive (victim + margin); qsearch only
   handles checks via evasion-at-leaf and the search check extension —
   checks between capture sequences deeper in the line can hide.
4. `TT_MAX` overflow clears the whole table (500k entries ≈ many games;
   rare in blitz, wasteful when it happens).
5. Eval-cache correctness rests on a silent contract: the persona dict
   must be identity-stable within one search (genes constant). A future
   refactor that mutates params mid-search would corrupt evals quietly.

**Pondering**
6. The ponder warms the TT for the *predicted* reply only; a deviating
   opponent wastes the work (still cheap — it is their clock). No
   verify-then-trust: we do not distinguish "opponent played our
   predicted move" from "TT happens to be warm".

**Learning**
7. Fitness signal is noisy: 10-game windows with varying opponent
   strength; no opponent-strength normalization (beating an 800 bot
   counts the same as beating a 1900). Evolution is frozen until
   100 games for this reason.
8. Book keyed by exact move sequence (no transposition/symmetry
   normalization); depth 8 plies; min 3 samples → slow to become useful.
9. Blunder detection threshold (250cp) measured between own successive
   searches conflates "our move was bad" with "opponent's reply was
   excellent". The ledger stores fen/uci/drop for future separation,
   unused so far.
10. Texel tuning of the PeSTO tables from the archived PGN corpus is
    parked until ~200–300 games; unclear it pays off at this nps.

**Integration / ops**
11. Opponent takeback requests and draw offers are not handled (ignored
    — unclear whether a pending takeback can stall a game; lichess-bot
    implements explicit accept/decline).
12. Only one process per account; no Docker/service unit; restarts are
    manual. Windows console process management is primitive (taskkill).
13. Incoming chat is logged only; no opponent-command handling
    (`!help` and friends do nothing).
14. Token currently has more scopes than needed; rotation pending.
15. No CI, no mypy; `smoke_test.py` (16 tests: engine, eval symmetry,
    ponder yield, reader/worker end-to-end with a fake API, persona
    pools, learning) is run manually on CPython + PyPy before deploys.
16. The bot's own outgoing chat is not logged (privacy choice) — new
    persona lines can only be verified in live games.

## 9. Questions for the reviewer

*Review status (Nov 11, 2026): an external review produced 10 ranked
proposals. Landed: style pre-roll (#1), clock-aware move retries +
measured POST overhead (#8), doc-truth + history-aging (#9), the
measurement layer (#2) — `match.py` self-play harness with Elo/SPRT
plus the `replay_blunders.py` regression suite (§5.7) — **#3 (soft
time limits)**, **#4 + #5 (futility pruning, single `is_check()`,
staged movegen)** — shipped after a measured **Elo −145 ± 107** win in
the production clock regime (§5.1) — and **#6 + #7 (learning)**: the
gene-evolution fitness signal is now a rating-gap z-score of
`Σ(result − expected)` with a raw-win-rate fallback (§5.5), and the
opening book is position-keyed with opponent-strength weighting and a
sample-gated deep range (§5.5). Q2 (verify-then-trust pondering) is
deprioritized per review: a fresh search against a hot TT is already
cheap. The review is **closed** as of Nov 11 evening: 9 of the 10 ranked
proposals landed; proposal #10's text was never archived, and the
remaining threads (SEE/qsearch checks, Texel tuning at ~200–300 games,
blunder-cause separation, takeback/draw-offer handling) stay parked in
§8 while the bot banks games for the z-score evolution and deep book.*

1. Highest-leverage strength steps from here, given the ~1500–1550
   level and the pure-Python constraint: SEE for qsearch, better delta
   pruning, history-heuristic refinements, or skipping straight to
   lightweight Texel tuning once ~300 PGNs exist?
2. Pondering: is a verify-then-trust upgrade worth it (re-start a real
   search immediately when the opponent plays the predicted move — the
   TT is already hot), or is the current always-warm approach close to
   the ceiling of what TT-warming can buy at this nps?
3. How would you normalize the fitness signal for window evolution
   (rating-delta-weighted Elo? opponent-strength-adjusted win rate?)
   with so few games?
4. Any Lichess-specific pitfalls we're missing (rate limits, takebacks,
   challenge etiquette, tournament participation, anti-abuse heuristics
   beyond the daily cap)?
5. The king shield gene is the only defensive learning channel. Should
   mate-loss events also damp `aggression`/`prox` temporarily ("soldier
   adapts strategy, not soul"), or does that break the persona contract?

---

*End of document. Source layout: `main.py`, `lichess_api.py`,
`engine.py`, `game_manager.py`, `persona.py`, `hunter.py`,
`learning.py`, `state.py`, `config.yml`, `smoke_test.py`,
`learn_report.py`, `check_token.py`, `bench.py`, `bench_warm.py`.*
