"""Live paper-trade resolution via Polymarket Gamma — the ACTUAL settled outcome.

This mirrors the proven resolution logic in `build-steps/fetch_data.py` (the path that
built the historical dataset's `yes_won` field): query Gamma `/markets?slug=...&closed=true`,
require both `closed == true` AND `umaResolutionStatus == "resolved"`, then read the
on-chain `outcomePrices`. Read-only, public endpoint, no auth, no order placement.

Used by the forward paper-trading harness so a paper trade resolves on what the market
*actually* settled to — not a Coinbase entry-vs-exit proxy.
"""
from __future__ import annotations

import json
from typing import Optional

import httpx

GAMMA_BASE = "https://gamma-api.polymarket.com"


def fetch_market_resolution(slug: str, timeout: float = 10.0) -> Optional[bool]:
    """Return the settled outcome for a 15-min BTC up/down market.

    Returns:
        True  — YES/UP won (outcomePrices[0] settled to ~1.0)
        False — NO/DOWN won
        None  — not resolved yet (market still open, or UMA not finalised, or the
                request failed). The caller MUST treat None as "retry later" and
                leave the paper trade pending — never as a loss.

    We require BOTH `closed` and `umaResolutionStatus == "resolved"` before trusting
    `outcomePrices`, exactly as the dataset builder does, so we never read a
    half-settled market.
    """
    if not slug:
        return None
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(f"{GAMMA_BASE}/markets", params={"slug": slug, "closed": "true"})
            r.raise_for_status()
            arr = r.json()
    except Exception:
        return None

    if not arr:
        return None
    m = arr[0] if isinstance(arr, list) else arr

    if not m.get("closed"):
        return None
    if m.get("umaResolutionStatus") != "resolved":
        return None

    raw = m.get("outcomePrices")
    if raw is None:
        return None
    try:
        outcome_prices = json.loads(raw) if isinstance(raw, str) else raw
        return float(outcome_prices[0]) > 0.5
    except (ValueError, TypeError, IndexError, KeyError):
        return None
