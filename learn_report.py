"""Print what RushAndCrush has learned so far.

  python learn_report.py
"""

import json

import yaml

from learning import Learner
from state import JsonStore

try:
    with open("config.yml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
except OSError:
    cfg = {}

learner = Learner("data/learning.json", cfg.get("learning", {}))
data = json.load(open("data/learning.json", encoding="utf-8"))

print("=== RushAndCrush learning report ===")
print()
print("Genes (learned values):")
for k, v in data["genes"].items():
    print(f"  {k:20s} {v}")

print()
print("Blunder ledger:", data["blunders"].get("total", 0), "filed")
b = data["blunders"]
print(f"  style picks in blunders : {b.get('style_pick', 0)}")
print(f"  in time trouble         : {b.get('time_trouble', 0)}")
print(f"  opening/middle/endgame  : {b.get('opening', 0)} / "
      f"{b.get('middlegame', 0)} / {b.get('endgame', 0)}")

print()
print(f"Mated {data.get('mate_losses', 0)}x (shield grew each time), "
      f"mated enemies {data.get('mate_wins', 0)}x")

book = data.get("book", {})
lines = sum(len(k.get("next", {})) for k in book.values())
print(f"Opening book: {len(book)} known positions, {lines} graded follow-ups")

w = data.get("window", {})
print(f"Current window: {w.get('games', 0)} games "
      f"({w.get('wins', 0)}W {w.get('losses', 0)}L {w.get('draws', 0)}D)")
snap = data.get("snapshot")
if snap:
    print(f"Last good snapshot: {snap['wr']:.0%} win rate")

print()
print(learner.summary())
