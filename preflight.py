#!/usr/bin/env python3
"""Pre-flight check for the forward paper-trading harness.

Run this BEFORE committing weeks to `python bot.py` (paper mode). It exercises the
*real* startup paths the bot depends on — not abstractions — so a missing dep, a
bad credential, or a dead endpoint fails here in 10 seconds instead of silently
corrupting a multi-week validation run.

What it verifies, and why each matters to a *paper* run (no orders, no capital):
  1. Interpreter + imports   — you're in the project venv with nautilus/redis/clob present.
  2. .env credentials        — present & well-formed. Paper mode still needs them: the
                               Polymarket DATA client authenticates to read the live book.
  3. Redis ping              — bot.py::init_redis() params exactly; holds the sim-mode flag.
  4. Polymarket CLOB auth    — REAL L2-authenticated handshake via py_clob_client. If your
                               API creds are wrong, the bot's data client never connects and
                               you get zero fills. This catches it now.
  5. Gamma market discovery  — replicates the bot's slug generation and confirms ≥1 live
                               BTC 15-min market exists right now (else nothing to trade).
  6. Live order book         — fetches the book for a live market token. The paper fill model
                               (ask for LONG / bid for SHORT) is meaningless without this.
  7. Gamma resolver          — paper_resolution.fetch_market_resolution() against a settled
                               past market. This is how a paper trade learns if it won.

Exit code 0 = all FATAL checks passed (safe to launch). Non-zero = do NOT launch yet.
WARN means "works but worth an eyeball" — it never blocks launch on its own.

Usage:
    ./venv/bin/python preflight.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

# --- tiny report harness -----------------------------------------------------
_GREEN, _YELLOW, _RED, _DIM, _RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
_fatal_failures: list[str] = []
_warnings: list[str] = []


def ok(label: str, detail: str = "") -> None:
    print(f"  {_GREEN}PASS{_RESET}  {label}" + (f"  {_DIM}{detail}{_RESET}" if detail else ""))


def warn(label: str, detail: str = "") -> None:
    _warnings.append(label)
    print(f"  {_YELLOW}WARN{_RESET}  {label}" + (f"  {_DIM}{detail}{_RESET}" if detail else ""))


def fail(label: str, detail: str = "") -> None:
    _fatal_failures.append(label)
    print(f"  {_RED}FAIL{_RESET}  {label}" + (f"  {_DIM}{detail}{_RESET}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}")


def mask(secret: str | None) -> str:
    """Show only enough of a secret to confirm it's the right one — never the whole thing."""
    if not secret:
        return "<empty>"
    s = secret.strip()
    return f"{s[:4]}…{s[-4:]} (len {len(s)})" if len(s) > 10 else f"<set, len {len(s)}>"


# --- 1. interpreter + imports ------------------------------------------------
section("1. Interpreter & imports")

here = os.path.dirname(os.path.abspath(__file__))
expected_venv = os.path.join(here, "venv")
if not sys.executable.startswith(expected_venv):
    warn(
        "interpreter is not the project venv",
        f"running {sys.executable} — re-run as ./venv/bin/python preflight.py",
    )
else:
    ok("project venv interpreter", sys.executable)

_missing = []
for mod in ("nautilus_trader", "redis", "httpx", "dotenv", "py_clob_client", "eth_account"):
    try:
        __import__(mod)
    except Exception as e:  # noqa: BLE001
        _missing.append(mod)
        fail(f"import {mod}", repr(e))
if not _missing:
    import nautilus_trader  # noqa: E402

    ok("core imports", f"nautilus_trader {nautilus_trader.__version__}")

# If imports are broken there is no point continuing — nothing else can run.
if _missing:
    print(f"\n{_RED}Aborting: missing dependencies. Run: ./venv/bin/pip install -r requirements.txt{_RESET}")
    sys.exit(2)

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

# --- 2. .env credentials -----------------------------------------------------
section("2. .env credentials")

env_path = os.path.join(here, ".env")
if not os.path.exists(env_path):
    fail(".env file", "not found — `cp .env.template .env` and fill in your Polymarket creds")
else:
    load_dotenv(env_path)
    ok(".env file", env_path)

REQUIRED = ["POLYMARKET_PK", "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_PASSPHRASE", "POLYMARKET_FUNDER"]
_creds_present = True
for key in REQUIRED:
    val = os.getenv(key)
    if not val or not val.strip():
        _creds_present = False
        fail(f"{key} set", "missing or empty in .env")
    elif val.strip().startswith("#"):
        # python-dotenv (this version) does not strip inline comments, so a template
        # line like `KEY=   # note` reads the comment AS the value. Reject it loudly.
        _creds_present = False
        fail(f"{key} set", "value looks like a leftover template comment — paste the real value, no inline #")
    else:
        ok(f"{key} set", mask(val))

# Format sanity — cheap checks that catch the classic copy-paste mistakes.
pk = (os.getenv("POLYMARKET_PK") or "").strip()
if pk:
    hexpart = pk[2:] if pk.lower().startswith("0x") else pk
    if len(hexpart) != 64 or any(c not in "0123456789abcdefABCDEF" for c in hexpart):
        warn("POLYMARKET_PK format", "expected a 64-char hex private key (optionally 0x-prefixed)")
    else:
        ok("POLYMARKET_PK format", "64-char hex")
funder = (os.getenv("POLYMARKET_FUNDER") or "").strip()
if funder and not (funder.lower().startswith("0x") and len(funder) == 42):
    warn("POLYMARKET_FUNDER format", "expected a 0x… 42-char wallet address")
elif funder:
    ok("POLYMARKET_FUNDER format", "0x address")

# --- 3. Redis ----------------------------------------------------------------
section("3. Redis (sim-mode flag store)")

import redis  # noqa: E402

redis_host = os.getenv("REDIS_HOST", "localhost")
redis_port = int(os.getenv("REDIS_PORT", 6379))
redis_db = int(os.getenv("REDIS_DB", 2))
try:
    rc = redis.Redis(
        host=redis_host, port=redis_port, db=redis_db,
        decode_responses=True, socket_connect_timeout=5, socket_keepalive=True,
    )
    rc.ping()
    ok("redis ping", f"{redis_host}:{redis_port} db={redis_db}")
except Exception as e:  # noqa: BLE001
    # bot.py degrades to a static sim flag if Redis is down, so this is a WARN, not FATAL —
    # but you wanted mode-switching, and a silently-static flag is a foot-gun.
    warn("redis ping", f"{e!r} — bot will fall back to a STATIC sim flag from .env. Start: redis-server")

# --- 4. Polymarket CLOB authenticated handshake ------------------------------
section("4. Polymarket CLOB auth (real L2 handshake)")

clob_client = None
if not _creds_present:
    fail("CLOB auth", "skipped — credentials missing above")
else:
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        creds = ApiCreds(
            api_key=os.getenv("POLYMARKET_API_KEY"),
            api_secret=os.getenv("POLYMARKET_API_SECRET"),
            api_passphrase=os.getenv("POLYMARKET_PASSPHRASE"),
        )
        # Mirror the bot's REAL L2 identity. bot.py omits `funder` on
        # PolymarketDataClientConfig, but the Nautilus adapter's factory fills it from env:
        #   factories.py: funder = funder or get_polymarket_funder()  # reads POLYMARKET_FUNDER
        # So under signature_type=1 (proxy) the bot authenticates as POLYMARKET_FUNDER — we
        # pass the same here so this handshake validates the identity the bot will actually use.
        clob_client = ClobClient(
            host="https://clob.polymarket.com",
            key=pk,
            chain_id=137,  # Polygon
            creds=creds,
            signature_type=1,
            funder=funder or None,
        )
        # 4a. server reachable (no auth)
        try:
            clob_client.get_ok()
            ok("CLOB server reachable", "clob.polymarket.com")
        except Exception as e:  # noqa: BLE001
            warn("CLOB server reachable", repr(e))

        # 4b. signer address derives from the private key
        try:
            addr = clob_client.get_address()
            ok("signer address derived", addr)
        except Exception as e:  # noqa: BLE001
            warn("signer address derived", repr(e))

        # 4c. THE real test: an L2-authenticated call. Wrong api_key/secret/passphrase fails here.
        try:
            keys = clob_client.get_api_keys()
            n = len(keys.get("apiKeys", [])) if isinstance(keys, dict) else "?"
            ok("L2 API-credential auth", f"credentials accepted ({n} api key(s) on account)")
        except Exception as e:  # noqa: BLE001
            fail(
                "L2 API-credential auth",
                f"{e!r} — your API_KEY/SECRET/PASSPHRASE are rejected. The bot's data "
                f"client will not connect and you will get ZERO fills.",
            )
    except Exception as e:  # noqa: BLE001
        fail("CLOB auth", f"could not build client: {e!r}")

# --- 5 & 6. Gamma market discovery + live order book -------------------------
section("5. Gamma live BTC-15m market discovery")

GAMMA = "https://gamma-api.polymarket.com"
# Replicate bot.py's slug generation: current 15-min boundary, scan a window around now.
now = datetime.now(timezone.utc)
boundary = (int(now.timestamp()) // 900) * 900
candidate_slugs = [f"btc-updown-15m-{boundary + i * 900}" for i in range(-1, 8)]

live_token_id = None
live_slug = None
try:
    params = [("closed", "false"), ("active", "true"), ("limit", "100")]
    params += [("slug", s) for s in candidate_slugs]
    r = httpx.get(f"{GAMMA}/markets", params=params, timeout=15)
    r.raise_for_status()
    markets = r.json()
    live = [m for m in markets if m.get("active") and not m.get("closed")] if isinstance(markets, list) else []
    if not live:
        warn(
            "live BTC-15m market found",
            "none active in the current window — markets roll every 15 min; harmless if transient, "
            "but if it persists the bot has nothing to trade",
        )
    else:
        ok("live BTC-15m market found", f"{len(live)} active (e.g. {live[0].get('slug')})")
        # Pull a CLOB token id so we can prove the order-book path the paper fills depend on.
        for m in live:
            raw = m.get("clobTokenIds")
            try:
                toks = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                toks = None
            if toks:
                live_token_id, live_slug = toks[0], m.get("slug")
                break
except Exception as e:  # noqa: BLE001
    warn("Gamma market discovery", repr(e))

section("6. Live order book (paper-fill source)")
if not live_token_id:
    warn("order book fetch", "no live token id available from discovery above — skipped")
elif clob_client is None:
    warn("order book fetch", "CLOB client unavailable — skipped")
else:
    try:
        book = clob_client.get_order_book(live_token_id)
        bids = getattr(book, "bids", None) or []
        asks = getattr(book, "asks", None) or []
        if bids and asks:
            best_bid = max(float(b.price) for b in bids)
            best_ask = min(float(a.price) for a in asks)
            spread = best_ask - best_bid
            detail = f"{live_slug}: bid {best_bid:.3f} / ask {best_ask:.3f} / spread {spread:.3f}"
            ok("order book fetch", detail)
            # The whole pivot is about spread cost — flag a suspiciously wide book now.
            if spread > 0.05:
                warn("book spread", f"{spread:.3f} is wide vs the dataset's ~0.01 median half-spread basis")
        else:
            warn("order book fetch", "book returned but empty (thin/just-opened market) — re-run later")
    except Exception as e:  # noqa: BLE001
        warn("order book fetch", repr(e))

# --- 7. Gamma resolver -------------------------------------------------------
section("7. Gamma resolver (how a paper trade learns its outcome)")
try:
    from paper_resolution import fetch_market_resolution

    # A market ~2h in the past should be closed AND UMA-resolved by now.
    past_boundary = boundary - (8 * 900)
    past_slug = f"btc-updown-15m-{past_boundary}"
    outcome = fetch_market_resolution(past_slug)
    if outcome is None:
        warn(
            "resolver smoke test",
            f"{past_slug} returned None (not yet settled or UMA lag). Re-run later; "
            f"the harness correctly treats None as 'retry', never a loss.",
        )
    else:
        ok("resolver smoke test", f"{past_slug} → {'UP/YES won' if outcome else 'DOWN/NO won'}")
except Exception as e:  # noqa: BLE001
    fail("resolver smoke test", repr(e))

# --- verdict -----------------------------------------------------------------
print("\n" + "=" * 70)
if _fatal_failures:
    print(f"{_RED}NOT READY{_RESET} — {len(_fatal_failures)} fatal check(s) failed:")
    for f in _fatal_failures:
        print(f"  {_RED}•{_RESET} {f}")
    if _warnings:
        print(f"{_DIM}(+{len(_warnings)} warning(s) to eyeball){_RESET}")
    print("\nFix the above, then re-run. Do NOT launch bot.py until this passes.")
    sys.exit(1)
else:
    print(f"{_GREEN}READY{_RESET} — all fatal checks passed.")
    if _warnings:
        print(f"{_YELLOW}{len(_warnings)} warning(s){_RESET} worth a glance:")
        for w in _warnings:
            print(f"  {_YELLOW}•{_RESET} {w}")
    print(
        "\nLaunch paper trading (NO --live):  ./venv/bin/python bot.py"
        "\nThen, after one cycle, sanity-read paper_trades.json before committing weeks."
    )
    sys.exit(0)
