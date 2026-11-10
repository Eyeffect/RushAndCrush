# RushAndCrush

A Lichess chess bot with a personality: a **reckless attacking soldier**
that only knows one direction — forward. It hunts matches autonomously,
trash-talks in game chat, and plays with its own homemade engine
(no Stockfish, everything in this folder is ours).

## What it does

- **Homemade engine** (`engine.py`): PVS + LMR alpha-beta with a persistent
  transposition table, tapered PeSTO piece-square tables, killers/history
  ordering, null-move pruning, aspiration windows and a quiescence search
  that sees mated leaves. All ours — no Stockfish anywhere.
- **A soldier's evaluation**: pawns dream of the enemy back rank, every
  piece is graded on how close it gets to the enemy king, and own-king
  safety is weighted by a learned `king_shield` gene. A style layer
  occasionally swaps the best move for the most *violent* move within a
  small evaluation budget.
- **Pondering**: after every move the bot searches the predicted reply on
  the opponent's clock — TT warming only. A real search preempts a ponder
  within ~100 ms, and ponders never queue behind each other.
- **Learning** (`learning.py`): an opening book graded by real game
  results (seeded with a gambit-heavy repertoire), within-game genes that
  drift from outcomes (king shield, pawn advance, style appetite), a
  blunder ledger that tones down what gets it into trouble, and a frozen
  evolution window until 100 games of data.
- **Persona** (`persona.py`): rule-based chat brain with 33 situational
  triggers — first blood, castling, promotions, en passant, comebacks,
  queen funerals, check-ins, pawn storms, situated endings (mated /
  flagged / resigned / stalemate). Anti-spam: cooldowns + never repeats
  a line until its pool is exhausted. Heckling triggers stay bots-only.
- **Reader/worker split** (`game_manager.py`): the stream reader never
  thinks — it applies server state and wakes a worker thread that
  snapshots, searches, verifies the world hasn't moved on, then commits
  and posts. Stale searches are discarded instead of fed to the server
  as 400s.
- **Hunter** (`hunter.py`): challenges online bots near its rating
  (rated blitz), keeps a **nemesis list** of everyone who beats it and
  challenges them again after a cooldown, accepts human challenges
  (casual blitz/rapid), respects Lichess rate limits (including the
  daily bot-vs-bot game cap).
- **State** (`state.py`): win/loss/draw stats and nemesis memory in `data/`.

Runs on CPython, but is built and benchmarked for **PyPy** (~2x node rate).

## Setup (one time)

1. **Create the Lichess account**: sign up at https://lichess.org/signup
   with the username `RushAndCrush` (usernames can't contain `&`).
   Verify your email.

2. **Create an API token** with the **bot:play** scope:
   - Go to https://lichess.org/account/oauth/token
   - Create a token, check **"Play games with the bot API"** only
   - Copy it somewhere safe (you'll only see it once)

3. **Set the token** (PowerShell):
   ```powershell
   setx LICHESS_BOT_TOKEN "lip_your_token_here"
   # close and reopen your terminal after setx
   ```

4. **Install dependencies**:
   ```powershell
   cd C:\Users\Eyeffect\rush-and-crush
   python -m pip install -r requirements.txt
   ```

5. **Test offline** (no account needed):
   ```powershell
   python smoke_test.py
   ```

6. **Upgrade the account to a BOT account** (⚠️ **irreversible** — this
   account can never play normal games again, which is why it must be
   dedicated):
   ```powershell
   python main.py --upgrade
   ```

## Run

```powershell
python main.py
```

That's it. The bot connects, resumes any ongoing games, starts hunting
bots near its rating, accepts human challenges, chats, and grinds.
Stop with `Ctrl+C`. Logs are in `rushandcrush.log`, stats in `data/`.

## Rules of engagement (Lichess side)

- Bots play **rated games only against other bots**; humans can only
  challenge the bot to casual games (Lichess enforces this).
- Bots cannot play ultraBullet.
- There is a **daily cap on bot-vs-bot games**; when it's hit, the hunter
  pauses automatically instead of spamming the API.
- The challenge API is rate-limited; the hunter spaces challenges out.

## Tuning

Everything lives in `config.yml`:

- `persona.aggression` — 0..1, how hard the evaluation favors attacking
- `persona.style_pick_prob` / `style_eval_slack` — how often and how
  wildly it picks a spicy second-best move
- `persona.chat.lines` — every chat line, in the soldier's voice
  (`{opponent}` and `{me}` are substituted)
- `hunter.*` — ratings window, time controls, nemesis cooldown,
  max simultaneous games
- `engine.*` — think-time behavior

## Files

| file | role |
|---|---|
| `main.py` | entrypoint: streams events, dispatches games/challenges |
| `lichess_api.py` | Lichess Bot API client (retries, NDJSON streams) |
| `engine.py` | homemade engine: PVS+LMR search, PeSTO eval, ponder protocol |
| `persona.py` | chat brain: triggers, cooldowns, no-repeat line pools |
| `hunter.py` | matchmaking: bot hunting, nemeses, incoming challenges |
| `game_manager.py` | reader/worker split per game: sync, think, verify, move, talk |
| `learning.py` | opening book, genes, blunder ledger (JSON in `data/learn/`) |
| `state.py` | stats, nemesis list, hunter memory (JSON in `data/`) |
| `config.yml` | every knob: persona, chat lines, hunter, engine, seeds |
| `smoke_test.py` | 16 offline tests: engine, ponder yield, handler loop, persona |
| `bench.py` / `bench_warm.py` | node-rate benchmarks (cold TT / warmed TT) |
| `TECHNICAL.md` | design notes: evaluation terms, search, concurrency rules |

The bot's learned state (`data/`) is runtime-only and regenerates as it
plays; it is git-ignored on purpose.
