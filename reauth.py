#!/usr/bin/env python3
"""
Interactive Schwab re-authorization — run this in YOUR terminal when the 7-day
refresh token expires (the feed dies and the server, being backgrounded, can't take
the paste). This foreground script triggers schwabdev's browser auth flow and writes
fresh tokens to ~/.schwabdev/tokens.db, which the server then picks up on restart.

Usage:
    cd schwab-market-depth
    source .venv/bin/activate
    python reauth.py

Then: a browser opens to Schwab → log in + approve → you'll be redirected to a
127.0.0.1 URL that likely shows an error page (that's fine) → COPY the full address-
bar URL and PASTE it here when prompted. Done.
"""
import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

key = os.getenv("SCHWAB_APP_KEY")
secret = os.getenv("SCHWAB_APP_SECRET")
if not key or not secret:
    raise SystemExit("SCHWAB_APP_KEY / SCHWAB_APP_SECRET missing from .env")

import schwabdev

print("Constructing Schwab client — if the refresh token is expired it will open a")
print("browser and ask you to paste the redirect URL...\n")

# same construction as the feed (default tokens_db=~/.schwabdev/tokens.db,
# open_browser_for_auth=True) — this drives the interactive re-auth on expiry.
client = schwabdev.Client(key, secret)

# force a token check + a live call to confirm the new tokens work
try:
    r = client.quote("$VIX").json()
    q = (r.get("$VIX", {}) or {}).get("quote", {}) or {}
    print("\n✅ Re-auth OK. Live check — $VIX last:", q.get("lastPrice") or q.get("mark"))
    print("Fresh tokens written to ~/.schwabdev/tokens.db. Tell the agent to restart the server.")
except Exception as e:
    print("\n⚠️ Constructed client but a test quote failed:", str(e)[:200])
    print("If you completed the paste, tokens may still be written — try restarting the server.")
