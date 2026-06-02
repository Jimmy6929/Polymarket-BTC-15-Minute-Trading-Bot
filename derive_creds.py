#!/usr/bin/env python3
"""Derive your Polymarket API credentials from your wallet private key, and write
them into .env — so you only ever hand-enter POLYMARKET_PK and POLYMARKET_FUNDER.

Polymarket's L2 API key/secret/passphrase are derived deterministically by signing
with your wallet key (py_clob_client.create_or_derive_api_creds). This runs that once
and fills the three blank lines in .env for you. Read-only w.r.t. funds: it creates an
API key on your account, it does NOT place orders or move money.

PREREQUISITES (do these first):
  1. A Polymarket account that your wallet is connected to (sign up at polymarket.com,
     connect/import this wallet, complete onboarding so API access is enabled).
  2. .env contains your POLYMARKET_PK (wallet private key) and POLYMARKET_FUNDER
     (your Polymarket proxy/funder address). The 3 API lines may be blank.

Usage:
    ./venv/bin/python derive_creds.py            # derive + write into .env
    ./venv/bin/python derive_creds.py --print    # derive + print only, don't touch .env
"""
from __future__ import annotations

import os
import re
import sys

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
_GREEN, _RED, _DIM, _RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def mask(s: str) -> str:
    return f"{s[:4]}…{s[-4:]} (len {len(s)})" if s and len(s) > 10 else "<set>"


def die(msg: str) -> None:
    print(f"{_RED}error:{_RESET} {msg}")
    sys.exit(1)


load_dotenv(ENV_PATH)
pk = (os.getenv("POLYMARKET_PK") or "").strip()
funder = (os.getenv("POLYMARKET_FUNDER") or "").strip()

if not pk or pk.startswith("#"):
    die("POLYMARKET_PK is empty in .env — paste your wallet private key first.")
hexpart = pk[2:] if pk.lower().startswith("0x") else pk
if len(hexpart) != 64 or any(c not in "0123456789abcdefABCDEF" for c in hexpart):
    die("POLYMARKET_PK doesn't look like a 64-char hex private key.")
if not funder or funder.startswith("#"):
    die("POLYMARKET_FUNDER is empty — paste your Polymarket proxy/funder address first.")

print(f"Deriving API creds for funder {funder} …")
try:
    from py_clob_client.client import ClobClient

    # signature_type=1 + funder must match the identity the bot authenticates as, so the
    # derived creds bind to the same account. The bot omits funder in bot.py but the Nautilus
    # adapter falls back to POLYMARKET_FUNDER (factories.py: funder or get_polymarket_funder()),
    # so deriving with this funder yields creds the bot's data client will accept.
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=pk,
        chain_id=137,  # Polygon
        signature_type=1,
        funder=funder,
    )
    creds = client.create_or_derive_api_creds()
except Exception as e:  # noqa: BLE001
    die(
        f"could not derive creds: {e!r}\n"
        "  Most likely your Polymarket account/API access isn't fully set up yet, "
        "or the wallet isn't the one connected to your Polymarket account."
    )

api_key = creds.api_key
api_secret = creds.api_secret
api_passphrase = creds.api_passphrase
print(f"  {_GREEN}derived{_RESET}  api_key={mask(api_key)}  secret={mask(api_secret)}  passphrase={mask(api_passphrase)}")

if "--print" in sys.argv:
    print("\n--print: not writing to .env. Paste these yourself if you prefer:")
    print(f"  POLYMARKET_API_KEY={api_key}")
    print(f"  POLYMARKET_API_SECRET={api_secret}")
    print(f"  POLYMARKET_PASSPHRASE={api_passphrase}")
    sys.exit(0)

# --- write the three values into .env, replacing the existing KEY= lines in place ---
with open(ENV_PATH, "r") as f:
    lines = f.readlines()

updates = {
    "POLYMARKET_API_KEY": api_key,
    "POLYMARKET_API_SECRET": api_secret,
    "POLYMARKET_PASSPHRASE": api_passphrase,
}
seen = set()
for i, line in enumerate(lines):
    for key, val in updates.items():
        if re.match(rf"^\s*{key}\s*=", line):
            lines[i] = f"{key}={val}\n"
            seen.add(key)
for key, val in updates.items():
    if key not in seen:  # line didn't exist — append it
        lines.append(f"{key}={val}\n")

# atomic write so a crash can't leave a half-written .env
tmp = ENV_PATH + ".tmp"
with open(tmp, "w") as f:
    f.writelines(lines)
os.replace(tmp, ENV_PATH)

print(f"\n{_GREEN}wrote{_RESET} API_KEY / API_SECRET / PASSPHRASE into .env")
print(f"{_DIM}.env is gitignored — these never get committed.{_RESET}")
print("\nNext:  ./venv/bin/python preflight.py")
