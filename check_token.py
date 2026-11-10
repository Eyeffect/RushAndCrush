"""One-off token check: scopes, account info, bot status."""
import os
import sys

from lichess_api import LichessAPI, TokenError

token = os.environ.get("LICHESS_BOT_TOKEN", "")
if not token:
    print("NO_TOKEN: LICHESS_BOT_TOKEN not set in this shell")
    sys.exit(1)
print("token loaded:", token[:8] + "..." + token[-4:])

api = LichessAPI(token)
try:
    info = api.verify_token()
except TokenError as e:
    print("TOKEN_ERROR:", e)
    sys.exit(1)

print("scopes:", info.get("scopes"))
print("userId:", info.get("userId"))

prof = api.profile()
print("username:", prof.get("username"))
print("title:", prof.get("title") or "(none - not a bot account yet)")
print("online:", prof.get("online"))
print("played games:", prof.get("count", {}).get("all", 0))
blitz = (prof.get("perfs") or {}).get("blitz") or {}
print("blitz:", blitz.get("rating", "unrated"), f"({blitz.get('games', 0)} games)")
