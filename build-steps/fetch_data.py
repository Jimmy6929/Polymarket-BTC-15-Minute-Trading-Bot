"""Data fetcher for the real-data backtest.

Pulls three independent sources:
1. Coinbase BTC-USD 1-min candles (last 14 days) — public, no auth
2. Fear & Greed Index full history — public, no auth
3. Polymarket BTC 15-min market trades + resolutions via The Graph subgraph
   — requires an API key (`POLYGRAPH_API_KEY` env var or `--api-key` flag)

Each source writes to build-steps/data/<source>.json so the backtester
can consume them deterministically. Re-running this script overwrites
the files — idempotent.
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Coinbase 1-min BTC candles
# ---------------------------------------------------------------------------

def fetch_coinbase_btc(days: int = 14) -> Path:
    """Page through Coinbase Exchange API to get 1-min BTC-USD candles.

    Returns path to a CSV with columns: ts_unix, low, high, open, close, volume.
    """
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    out_path = DATA_DIR / "coinbase_btc_1min.csv"

    print(f"[coinbase] fetching {start.isoformat()} → {end.isoformat()} (1-min candles)")

    # Coinbase /candles returns max 300 rows per call. With granularity=60 (1-min),
    # 300 candles = 5 hours. Page through 5-hour windows.
    window_minutes = 300
    window = timedelta(minutes=window_minutes)
    cursor = start

    all_rows: List[List[Any]] = []
    n_calls = 0
    client = httpx.Client(
        base_url="https://api.exchange.coinbase.com",
        headers={"User-Agent": "PolymarketBacktest/1.0", "Accept": "application/json"},
        timeout=30.0,
    )

    try:
        while cursor < end:
            window_end = min(cursor + window, end)
            r = client.get(
                "/products/BTC-USD/candles",
                params={
                    "granularity": 60,
                    "start": cursor.isoformat(),
                    "end": window_end.isoformat(),
                },
            )
            if r.status_code != 200:
                print(f"[coinbase] HTTP {r.status_code}: {r.text[:200]}")
                time.sleep(1.0)
                continue
            data = r.json()
            # Format: [ts_unix, low, high, open, close, volume], descending
            all_rows.extend(data)
            n_calls += 1
            if n_calls % 10 == 0:
                print(f"[coinbase] {n_calls} calls, {len(all_rows)} rows so far")
            cursor = window_end
            time.sleep(0.15)  # well under the 10 req/s limit
    finally:
        client.close()

    # Deduplicate + sort ascending
    seen = set()
    deduped = []
    for row in all_rows:
        if not row or len(row) < 6:
            continue
        ts = int(row[0])
        if ts in seen:
            continue
        seen.add(ts)
        deduped.append(row)
    deduped.sort(key=lambda r: r[0])

    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_unix", "low", "high", "open", "close", "volume"])
        w.writerows(deduped)

    print(f"[coinbase] wrote {len(deduped)} rows → {out_path}")
    expected = days * 24 * 60
    coverage = len(deduped) / expected * 100
    print(f"[coinbase] coverage: {coverage:.1f}% of expected {expected} minutes")
    return out_path


# ---------------------------------------------------------------------------
# Fear & Greed Index (alternative.me) — full daily history
# ---------------------------------------------------------------------------

def fetch_fear_greed() -> Path:
    """Fetch full daily Fear & Greed history. Tiny dataset."""
    out_path = DATA_DIR / "fear_greed.json"
    print(f"[fng] fetching full daily history")

    r = httpx.get(
        "https://api.alternative.me/fng/",
        params={"limit": 0, "format": "json"},
        timeout=30.0,
        headers={"User-Agent": "PolymarketBacktest/1.0"},
    )
    r.raise_for_status()
    data = r.json()

    rows = data.get("data", [])
    print(f"[fng] received {len(rows)} daily values")

    with out_path.open("w") as f:
        json.dump(rows, f, indent=2)
    print(f"[fng] wrote → {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# Polymarket BTC 15-min markets via Gamma + CLOB (no auth)
# ---------------------------------------------------------------------------

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

def fetch_polymarket_btc_15m(days: int = 14) -> Path:
    """Pull BTC 15-min market dataset for the last N days.

    Pipeline (no auth required):
      1. Generate predictable slugs `btc-updown-15m-{ts}` for each 15-min boundary.
      2. For each slug, hit Gamma to get: token IDs, resolution status, Chainlink
         finalPrice and priceToBeat, fee schedule, volume.
      3. For each resolved market, hit CLOB /prices-history with fidelity=1
         to get minute-by-minute YES token prices over the 15-min window.
      4. Save unified JSON.
    """
    out_path = DATA_DIR / "polymarket_btc_15m.json"

    # Align "now" down to the last completed 15-min boundary
    now_ts = int(datetime.now(timezone.utc).timestamp())
    end_ts = (now_ts // 900) * 900
    start_ts = end_ts - days * 86400

    slugs = []
    ts = start_ts
    while ts <= end_ts:
        slugs.append((ts, f"btc-updown-15m-{ts}"))
        ts += 900

    print(f"[gamma] querying {len(slugs)} predictable slugs over {days} days")

    client = httpx.Client(timeout=30.0, headers={"User-Agent": "PolymarketBacktest/1.0"})
    markets: List[Dict[str, Any]] = []
    skipped_unresolved = 0
    skipped_404 = 0
    skipped_missing_meta = 0

    try:
        # --- Gamma metadata pass ---
        for i, (slug_ts, slug) in enumerate(slugs):
            try:
                # closed=true required: Gamma's default filter EXCLUDES closed markets
                r = client.get(f"{GAMMA_BASE}/markets", params={"slug": slug, "closed": "true"})
            except Exception as e:
                print(f"[gamma] {slug}: request failed: {e}")
                continue
            if r.status_code != 200:
                skipped_404 += 1
                continue
            data = r.json()
            if not data:
                skipped_404 += 1
                continue
            m = data[0]

            if not m.get("closed"):
                skipped_unresolved += 1
                continue
            if m.get("umaResolutionStatus") != "resolved":
                skipped_unresolved += 1
                continue

            events_list = m.get("events") or []
            event_meta = (events_list[0].get("eventMetadata") if events_list else {}) or {}
            # finalPrice / priceToBeat are NOT guaranteed on older markets.
            # outcomePrices IS — both come from on-chain settlement.
            final_price = event_meta.get("finalPrice")
            price_to_beat = event_meta.get("priceToBeat")

            try:
                clob_tokens = json.loads(m["clobTokenIds"])
                outcome_prices = json.loads(m["outcomePrices"])
            except Exception:
                skipped_missing_meta += 1
                continue

            markets.append({
                "slug": slug,
                "slug_ts": slug_ts,
                "market_id": m.get("id"),
                "condition_id": m.get("conditionId"),
                "start_time_iso": m.get("startTime") or (events_list[0].get("startDate") if events_list else None),
                "end_date_iso": m.get("endDate"),
                "yes_token_id": clob_tokens[0],
                "no_token_id": clob_tokens[1],
                "yes_resolved_to": float(outcome_prices[0]),  # 1.0 if YES (UP) won
                "no_resolved_to": float(outcome_prices[1]),
                "final_btc_price": float(final_price) if final_price is not None else None,
                "btc_price_to_beat": float(price_to_beat) if price_to_beat is not None else None,
                "yes_won": float(outcome_prices[0]) > 0.5,
                "fee_schedule": m.get("feeSchedule"),
                "volume_usdc": float(m.get("volume", 0)),
                "spread": m.get("spread"),
                "last_trade_price": m.get("lastTradePrice"),
                "best_bid": m.get("bestBid"),
                "best_ask": m.get("bestAsk"),
            })

            if (i + 1) % 100 == 0:
                print(f"[gamma] progress {i+1}/{len(slugs)} | resolved so far: {len(markets)}")
            time.sleep(0.10)

        print(f"[gamma] done. resolved: {len(markets)} | unresolved: {skipped_unresolved} "
              f"| missing meta: {skipped_missing_meta} | not found: {skipped_404}")

        # --- CLOB prices-history pass ---
        print(f"[clob] pulling minute prices for {len(markets)} resolved markets")
        for j, m in enumerate(markets):
            try:
                r = client.get(
                    f"{CLOB_BASE}/prices-history",
                    params={
                        "market": m["yes_token_id"],
                        "startTs": m["slug_ts"],
                        "endTs": m["slug_ts"] + 900,
                        "fidelity": 1,
                    },
                )
                if r.status_code == 200:
                    m["yes_price_history"] = r.json().get("history", [])
                else:
                    m["yes_price_history"] = []
                    m["clob_error"] = f"HTTP {r.status_code}"
            except Exception as e:
                m["yes_price_history"] = []
                m["clob_error"] = str(e)

            if (j + 1) % 100 == 0:
                with_data = sum(1 for x in markets[:j+1] if x.get("yes_price_history"))
                print(f"[clob] progress {j+1}/{len(markets)} | with price data: {with_data}")
            time.sleep(0.10)

    finally:
        client.close()

    with out_path.open("w") as f:
        json.dump(markets, f, indent=2)

    with_prices = sum(1 for m in markets if m.get("yes_price_history"))
    print(f"[polymarket] wrote {len(markets)} markets ({with_prices} with price history) → {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--api-key", help="The Graph API key (or POLYGRAPH_API_KEY env)")
    parser.add_argument("--sources", nargs="+", default=["coinbase", "fng", "polymarket"],
                        choices=["coinbase", "fng", "polymarket"])
    args = parser.parse_args()

    if "coinbase" in args.sources:
        fetch_coinbase_btc(days=args.days)
    if "fng" in args.sources:
        fetch_fear_greed()
    if "polymarket" in args.sources:
        fetch_polymarket_btc_15m(days=args.days)


if __name__ == "__main__":
    main()
