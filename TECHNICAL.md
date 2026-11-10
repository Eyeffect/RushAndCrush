# RushAndCrush — Technical System Document

*Hand this document to a reviewer (human or LLM) alongside a request like:
"Review this architecture and suggest concrete improvements. Prioritize by
impact." It is intentionally self-contained; no source code access needed.*

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

- Windows 10/11, Python 3.12, run as a foreground/background console process.
- Dependencies: `python-chess` (move generation, legality, zobrist),
  `requests`, `PyYAML`. Nothing else.
- Account: dedicated Lichess account upgraded to a **BOT account**
  (irreversible). OAuth token stored in env var `LICHESS_BOT_TOKEN`
  (never in logs; the pasted token has broader scopes than needed and
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
  │    ├─ gameStart      → spawn GameHandler thread
  │    ├─ gameFinish     → log
  │    ├─ challenge      → spawn decision thread → hunter.decide_incoming()
  │    │                   → api.accept() / api.decline(reason)
  │    └─ challengeDeclined → hunter.on_declined() (cooldown memory)
  ├─ on boot: GET /api/account/playing → resume handlers for ongoing games
  └─ Hunter thread (matchmaking daemon)
        ├─ every 15s tick: if capacity and cooldown ok → challenge someone
        └─ nemesis list prioritized over fresh random bot targets

GameHandler thread (one per live game)
  game stream: GET /api/bot/game/stream/{id} (NDJSON)
    gameFull → identify colors, greet (player+spectator rooms), sync board
    gameState→ resync board, triggers, and if our turn: think & move
    chatLine → logged only
  move pipeline: learned book? → else search → style pick → POST move
  on finish → persona farewell, stats, nemesis, learner.report_game()
```

Shared state (all JSON, atomic writes via tmp-file + `os.replace`):
- `data/stats.json` — career W/L/D
- `data/nemesis.json` — who beat us, when (revenge targeting)
- `data/hunter.json` — outgoing challenge attempts (anti-spam memory)
- `data/learning.json` — genes, opening book, blunder ledger, evolution window

Thread-safety model: `JsonStore` uses a per-store lock; the engine has a
`search_lock` so only one search runs at a time across game threads;
persona has a lock for cooldown bookkeeping. Everything else is
thread-confined.

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

Retry policy: one shared `_request` wrapper — retries on 429/5xx/connect
errors/timeouts with delays (1,2,4,8,15s); 4xx raises immediately; streams
reconnect at the main loop with 5s→120s exponential backoff.

## 5. Component specifications

### 5.1 Engine (`engine.py`) — the chess brain

Board representation: python-chess `Board`; search works on
`board.copy(stack=False)`. No bitboards, no NNUE. This caps speed at
roughly 20–60k nodes/s; typical blitz search reaches depth 4–6 (quiescence
extends tactical vision).

**Evaluation** (static, hand-written, White-POV; negamax flips sign):

```
score = Σ pieces: material + PST[piece][sq]
      + aggression proximity term   (learnable)
      + king shield term            (learnable, usually 0)
```

- Material: P=100, N=320, B=330, R=500, Q=900, K=0.
- PSTs: one table per piece type, 64 entries (a1=0..h8=63), monotonically
  forward-biased for White (mirrored for Black): a pawn on the 7th earns
  +120, on the 2nd +2; pieces score more the more advanced/central.
  Pawns on ranks 5–7 get outsized bonuses (the "pawn storm" incentive).
- Aggression proximity (scaled by genes `aggression` ∈ [0,1], `prox`):
  every non-king piece within Chebyshev distance 5 of the *enemy king*
  adds `(6 − d) × (3 if pawn else 2) × aggression × prox`.
- **Deliberately absent**: mobility, pawn structure, own-king safety
  (except the learned shield below), tapered eval, tempo bonus. This is
  the persona: the engine under-values defense on purpose.
- King shield (scaled by learned gene `king_shield`, default 0.0):
  in non-endgame, each side missing pawns on its king's 3 front squares
  loses `(3 − shields) × 12 × king_shield` cp. Only grows via mate losses
  (see 5.6).
- Endgame detection: total non-pawn material < 1600 → king uses a
  centralized endgame PST (king "joins the assault").

**Search**: iterative deepening 1..40, soft time check between depths.
Per depth: full-window alpha-beta, fail-soft, root moves re-ordered by the
previous depth's scores (initial order randomized once for game variety).

- Transposition table: `chess.polyglot.zobrist_hash` keys, depth-preferred,
  exact/lower/upper flags, best move stored for ordering; **cleared every
  `search()` call** (per move).
- Check extension: if the move gives check and remaining depth ≥ 1,
  add +1 ply (the attacker digs deeper through checks).
- Quiescence: stand-pat, then captures only (`generate_legal_captures`,
  MVV-LVA order). No checks in qsearch, no SEE, no delta pruning.
- Not implemented: null-move pruning, LMR, PVS, aspiration windows, SEE,
  repetition/50-move detection inside the tree (a root-level threefold
  guard was removed as ineffective), pondering.
- Time control: node counter; every 2048 nodes, wall-clock check against
  `stop_at` raises a search timeout; the last fully completed depth's
  results are kept (a partially searched depth may update the best move —
  accepted behavior).

**Move ordering**: TT move (10M) > captures 1M + `10×victim − attacker` >
promotions (900k) > checks via `gives_check` (500k) > killers (400k/350k)
> history (depth²) > advanced-pawn pushes (+20k).

**Style layer** (`pick_move`): with probability `style_pick_prob`
(only if score > −300 and a rival is within `style_eval_slack` cp of the
best move), swap the best move for the rival with the highest **violence
score**: capture 30 + victim_value/10; promotion +15; pawn landing ranks
5/6 → +8/+14 (mirror for Black); resulting check +25; checkmate +1000;
landing within 4 of the enemy king → +4 per step closer. Sets a
`style_pick` flag that the learner uses to attribute later blunders.

### 5.2 Game handler (`game_manager.py`) — one thread per game

State machine over the game stream:

- `gameFull`: parse `initialFen`, assign `my_color` by username, record
  opponent name/title; send forced greeting (player room; nemesis variant
  if the opponent is currently on the nemesis list) + spectator greeting;
  process the embedded state.
- `gameState`: status check (non-`started` → finish); **board sync**: apply
  the streamed move list difference to our local board (on any illegal
  move or ply regression: rebuild from `initialFen` and replay all);
  opponent-clock < 15s → one-time time-pressure taunt; if it's our turn
  → move pipeline.
- `chatLine`: log only.

**Move pipeline** (our turn):
1. Budget: `remaining×0.05 + inc×0.4`, × `endgame_time_mult` after ply 60,
   clamped to [0.35, 8.0]s; if clock < 20s: ≤ max(0.12, s/25); if < 5s:
   ≤ 0.25; minus 300ms network overhead.
2. Params: config persona dict merged over with learned genes
   (`genes()` wins); if clock < 20s, `style_pick_prob` ←
   `style_pick_lowtime` (learnable, can be 0).
3. **Learned book**: if ply < 8 and the exact move-sequence key has a
   reliable, non-cursed follow-up (see 5.6) → play it instantly
   (POST; `last_search_score` invalidated).
4. Else `pick_move` (search + style layer).
5. **Blunder detection**: if the new search score is ≥250cp *below* the
   previous own-search score, our previous move was a mistake →
   `learner.record_blunder(phase, clock, was_style_pick)`; if the same
   phase repeats (≥3rd time) → `repeat_mistake` chat line. Conversely a
   ≥250cp rise after the opponent's move → taunt trigger.
6. Read capture flags pre-push; push locally; record
   `{uci, eval, clock, style}`; post-move triggers: check_given,
   queen_taken (we captured their queen), pawn_storm (our pawn to rank ≥5,
   max 2 per game).
7. `POST /bot/game/{id}/move/{uci}`; on HTTP failure log and set
   `last_search_score = None` (phantom evals must not become lessons).
8. Optional resignation: 3 consecutive own searches ≤ −1200cp after
   ply 50 (disabled by default — "a soldier does not surrender").

**Finish**: status `aborted` → skip; else result from `winner` field
(None → draw). Forced win/loss/draw + spectator farewell chat;
`stats.record`; on loss `nemesis.add_loss(opponent)`;
`learner.report_game(result, status, all_game_moves, we_are_white,
opponent)`.

Known resume race: after a restart, the first computed move may be posted
against a pre-sync position → Lichess 400 → self-heals on the next
gameState (board resyncs; the failed eval is discarded). Frequency: a few
400s right after restarts; no permanent damage observed.

### 5.3 Persona (`persona.py`) — rule-based chat brain

Trigger → line banks live in `config.yml` (13 triggers: game_start,
game_start_nemesis, taunt, self_blunder, repeat_mistake, queen_lost/
taken, check_given, pawn_storm, time_pressure, win/loss/draw/resign,
spectator_*). `compose(trigger, subs, force)`:
- gate: global min gap 25s between any messages, per-trigger cooldown
  120s, 35% random skip; `force=True` bypasses gates (game start/end).
- line selection: shuffled bag per trigger — **no line repeats until its
  whole pool has been used**; `{me}`/`{opponent}` templating.
- unknown trigger / disabled chat → silent.

### 5.4 Hunter (`hunter.py`) — matchmaking daemon

Tick every 15s. Conditions to hunt: capacity (live games < `max_games` =
2), ≥60s since last outgoing challenge, not paused by rate limit.

1. **Nemesis priority**: most-beaten opponent whose cooldown (30 min)
   expired → challenge them again (humans only if actually a bot? no —
   humans challenged casually; bot title checked via public data).
2. Else: `GET /api/bot/online` (512) → filter: not self, not challenged
   in the last 15 min, blitz rating reliable (≥5 games), rating within
   window. Window starts 600–2200; once our own blitz rating has ≥5
   games it re-centers to [own−250, own+350] (clamped, updated every
   10 min). Provisional 3000 default rating is ignored for targeting.
3. Challenge: random time control from {[180,0],[180,2],[300,0],[300,3]},
   rated vs bots / casual vs humans, random color. Response handling:
   `error` → log; if the error carries `ratelimit.seconds`
   (`bot.vsBot.day` daily cap) → pause hunting for that duration.
4. Incoming challenges (`decide_incoming`): non-standard variant →
   decline `standard`; ultraBullet/bullet/correspondence → decline
   `timeControl`; no capacity → `later`; bots → accept (rated fine);
   humans → must be casual (decline `rated` if set; Lichess enforces
   casual for human-vs-bot anyway). Declines feed the 15-min memory.

### 5.5 Learning (`learning.py`) — memory + drill sergeant

One JSON file, four mechanisms:

**Genes** (defaults): `pawn_adv=1.0` (multiplies pawn PST),
`prox=1.0` (multiplies proximity term), `king_shield=0.0`,
`style_pick_prob=0.25`, `style_eval_slack=45`, `style_pick_lowtime=0.25`,
`endgame_time_mult=1.0`. Genes are merged over the static persona config
at every move.

**Blunder ledger → immediate lessons** (`record_blunder`): counters by
phase (opening ≤20 plies, middlegame ≤60, endgame >60), time trouble
(clock <20s at move time), style-pick attribution.
- ≥6 blunders and style-pick share >50% → `style_pick_prob` ×0.8 (floor 0.05).
- ≥5 blunders and time-trouble share >40% → `style_pick_lowtime = 0`.
- ≥8 blunders and endgame share >60% → `endgame_time_mult = 1.3`.
- Returns whether this phase's mistake count hit 3 (persona quips).

**Mate lessons** (`report_game`): loss by `status="mate"` →
`king_shield += 0.05` (cap 0.5) — the only path to learning defense;
win by mate → `king_shield −= 0.02` (floor 0).

**Results-graded opening book**: for each of the first 8 plies of every
game, key = exact move sequence prefix, value = per-follow-up {w,l,d}
counted with the game result. Prune keeps entries with ≥1 sample.
Lookup (`get_book_move`): exact key, parity must be our turn, follow-ups
with ≥`book_min_samples` (3) games and score `(w + 0.5d)/n ≥ 0.45`;
best score wins; otherwise fall through to search (a line that only loses
is "cursed" → engine thinks for itself).

**Window evolution** (every 10 games): win rate `wr = (W + 0.5D)/games`;
- if `wr < snapshot.wr − 0.10` → revert genes to snapshot ("drill sergeant");
- elif `wr ≥ 0.55` → snapshot current genes;
- else mutate `style_pick_prob ±0.05` ∈ [0.05, 0.35] and
  `style_eval_slack ±10` ∈ [20, 80] (explore, with revert safety net).
Window then resets.

Learning signals are coarse by design (result-level fitness, tiny param
space); there is no gradient/Texel tuning of PSTs, no supervised data.

### 5.6 Main loop (`main.py`)

`Bot` wires API + engine + persona + stores + hunter; resumes ongoing
games from `/api/account/playing`; consumes the event stream forever;
reconnects with backoff on network errors; Ctrl+C → clean exit. Incoming
challenge decisions run on short-lived threads so a slow HTTP call never
blocks the stream loop.

## 6. Configuration surface (`config.yml`)

- `engine`: min/max think time (0.35/8s), time_fraction 0.05, overhead 300ms.
- `persona`: aggression 0.9, style_pick_prob 0.25, style_eval_slack 45,
  resign disabled, all chat lines + cadence knobs.
- `hunter`: max_games 2, challenge_interval 60s, time controls,
  rating window, nemesis cooldown 30 min.
- `learning`: book_depth 8, book_min_samples 3, window 10.

## 7. Observed behavior so far (first session)

- 2 games played, both lost by checkmate (to a 1550 bot and a deliberately
  weak novelty bot). Eval trajectories show the expected pattern: evals
  near 0 early, then steady collapse (−1000..−2500) once material is
  sacrificed without defensive compensation. resignation never triggers
  (disabled); the bot fights to mate.
- STYLE PICKS observed live (e.g., giving 11cp for a knight hop with
  attacking intent). Book is empty (needs completed games). Learning
  state fresh as of the latest deploy.
- Resume-after-kill produces transient 400s (handled; see 5.2).

## 8. Known limitations & risks (honest list)

**Strength / engine**
1. Pure-Python speed ceiling (~20–60k nps) → depth 4–6. Fundamental without
   C/Rust movegen or NNUE; question: is further investment worth it for a
   personality bot, or is 1500–1800 the right home?
2. No repetition/50-move detection in search → can shuffle won positions
   into draws (mitigated only by root randomness + aggression eval).
3. No SEE / null-move / LMR / aspiration → tactical blind spots at shallow
   depth; check extension partially compensates.
4. Aggression proximity term can actively prefer hanging a piece near the
   enemy king (eval rewards closeness even if the piece hangs after
   qsearch-exhausted lines).
5. TT rebuilt from scratch each move (cheap at 64MB scale but wasteful).

**Learning**
6. Fitness signal is noisy: 10-game windows with varying opponent strength;
   no opponent-strength normalization (beating an 800 bot counts the same
   as beating a 1900).
7. Genes couple to outcomes only through coarse rules; PSTs and material
   values never adapt. Is lightweight Texel tuning from self-generated
   positions feasible at this nps, or overkill?
8. Book keyed by exact move sequence (no transposition/symmetry
   normalization); depth 8 plies; min 3 samples → slow to become useful.
9. Blunder detection threshold (250cp) measured between own successive
   searches conflates "our move was bad" with "opponent's reply was
   excellent". No separation of the two.

**Integration / ops**
10. Resume race causes 400s (self-healing but ugly); a pre-POST legality
    re-check against the latest streamed state would eliminate it.
11. Opponent takeback requests and draw offers are not handled (ignored —
    unclear whether a pending takeback can stall a game; lichess-bot
    implements explicit accept/decline).
12. Only one process per account; no Docker/systemd unit; restarts are
    manual. Windows console process management is primitive (taskkill).
13. Chat has no spectator mid-game commentary and no opponent-command
    handling (e.g., `!help`) — greeting promises commands that don't exist.
14. Token currently has more scopes than needed (past leakage through chat
    history); rotation pending.
15. No tests in CI, no mypy; smoke tests exist (`smoke_test.py`) and are
    run manually before deploys.

## 9. Questions for the reviewer

1. Highest-leverage engine improvements given the 1500–1800 target and
   pure-Python constraint? (Candidates: repetition detection in search;
   SEE for qsearch; null-move; persistent TT; multiprocessing root split.)
2. Is the learning design sound? In particular: should `king_shield` be
   the only defensive gene, or should mate-loss events also damp
   `aggression`/`prox` temporarily ("soldier adapts strategy, not soul")?
3. How would you normalize the fitness signal for window evolution
   (rating-delta-weighted Elo? opponent-strength-adjusted win rate?)
   with so few games?
4. Any Lichess-specific pitfalls we're missing (rate limits, takebacks,
   challenge etiquette, tournament participation, anti-abuse heuristics
   beyond the daily cap)?
5. Suggested structure for logging/game archival (PGN per game) to enable
   future post-hoc analysis without runtime cost?

---

*End of document. Source layout: `main.py`, `lichess_api.py`,
`engine.py`, `game_manager.py`, `persona.py`, `hunter.py`,
`learning.py`, `state.py`, `config.yml`, `smoke_test.py`,
`learn_report.py`, `check_token.py`, `bench.py`.*
