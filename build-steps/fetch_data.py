"""Data fetcher for the real-data backtest — incremental + full-range.

Pulls three independent sources into build-steps/data/:
1. Coinbase BTC-USD 1-min candles — public, no auth
2. Fear & Greed Index full history — public, no auth
3. Polymarket BTC 15-min market metadata + resolutions + minute price history
   via Gamma + CLOB — public, no auth

This fetcher is INCREMENTAL and CRASH-SAFE:
  - It fetches the FULL available range (default: --full → from EARLIEST_SINCE_ISO
    to now) but on re-run only pulls what's missing (markets without price history,
    the unresolved tail, and any spot minutes outside the existing span).
  - Every file is written atomically (tmp + fsync + os.replace) so a crash never
    leaves a truncated file; a one-time `.bak` of each big file is kept.
  - A merge never overwrites a good record (one with price history) with an empty
    re-fetch, so partial/aborted runs can only add data, never lose it.

The large raw files (polymarket_btc_15m.json, coinbase_btc_1min.csv) are gitignored;
`MANIFEST.json` (committed) records their range / count / sha256 so the dataset
state is reproducible from git without committing the bytes.
"""
import argparse
import csv
import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)

MANIFEST_PATH = DATA_DIR / "MANIFEST.json"
# The BTC-updown-15m product launched ~2025-10-09 on Polymarket; nothing exists before.
EARLIEST_SINCE_ISO = "2025-10-09"


# ---------------------------------------------------------------------------
# Shared helpers: atomic write, one-time backup, since-parsing, hashing
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, write_fn: Callable[[Any], None]) -> None:
    """Write via a same-dir temp file + fsync + os.replace (atomic rename).

    A crash mid-write leaves the original file fully intact; readers never see a
    partial file. `write_fn(file_obj)` does the actual json.dump / csv.writer work.
    """
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "w", newline="") as f:
            write_fn(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _backup_once(path: Path) -> None:
    """Copy `path` → `path.bak` exactly once (only if no .bak exists yet)."""
    bak = path.with_name(f"{path.name}.bak")
    if path.exists() and not bak.exists():
        shutil.copy2(path, bak)
        print(f"[backup] {path.name} → {bak.name} (one-time pristine copy)")


def _parse_since(s: str) -> int:
    """Accept a bare unix int OR an ISO date/datetime → unix seconds (UTC)."""
    s = str(s).strip()
    if s.isdigit():
        return int(s)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _iso(ts: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts is not None else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Coinbase 1-min BTC candles — incremental (fetch only ranges outside existing)
# ---------------------------------------------------------------------------

def _coinbase_fetch_range(client: httpx.Client, start_dt: datetime, end_dt: datetime) -> List[List[Any]]:
    """Page Coinbase /candles over [start_dt, end_dt) in 5-hour (300-row) windows.

    Robust to old-data limits: a non-200 or empty window ADVANCES the cursor
    instead of spinning (the original code's `continue`-without-advance bug),
    so a permanently-rejected old window can never hang the run.
    """
    window = timedelta(minutes=300)
    cursor = start_dt
    rows: List[List[Any]] = []
    n_calls = 0
    while cursor < end_dt:
        window_end = min(cursor + window, end_dt)
        try:
            r = client.get(
                "/products/BTC-USD/candles",
                params={"granularity": 60, "start": cursor.isoformat(), "end": window_end.isoformat()},
            )
        except Exception as e:  # noqa: BLE001
            print(f"[coinbase] request error near {cursor.date()}: {e} — advancing")
            cursor = window_end
            continue
        if r.status_code != 200:
            print(f"[coinbase] HTTP {r.status_code} near {cursor.date()}: {r.text[:120]} — advancing")
            cursor = window_end          # KEY FIX: advance, don't spin forever
            time.sleep(0.3)
            continue
        data = r.json()
        rows.extend(data)                # [ts_unix, low, high, open, close, volume], descending
        n_calls += 1
        if n_calls % 20 == 0:
            print(f"[coinbase] {n_calls} calls, {len(rows)} rows in this range so far")
        cursor = window_end
        time.sleep(0.15)                 # well under the 10 req/s limit
    return rows


def fetch_coinbase_btc(since_ts: int, end_ts: Optional[int] = None) -> Path:
    """Pull 1-min BTC-USD candles, fetching only the date ranges OUTSIDE what we have.

    Merges into the existing CSV (dedup by ts_unix), atomic write. Returns the path.
    """
    out_path = DATA_DIR / "coinbase_btc_1min.csv"
    end = (datetime.fromtimestamp(end_ts, timezone.utc) if end_ts
           else datetime.now(timezone.utc).replace(second=0, microsecond=0))
    since = datetime.fromtimestamp(since_ts, timezone.utc)

    rows_by_ts: Dict[int, List[Any]] = {}
    if out_path.exists():
        _backup_once(out_path)
        with out_path.open() as f:
            rd = csv.reader(f)
            next(rd, None)  # header
            for row in rd:
                if row and len(row) >= 6:
                    rows_by_ts[int(row[0])] = row
    existing_min = min(rows_by_ts) if rows_by_ts else None
    existing_max = max(rows_by_ts) if rows_by_ts else None

    # Compute the gap ranges to fetch (only what's outside [existing_min, existing_max]).
    ranges: List[tuple] = []
    if existing_min is None:
        ranges.append((since, end))
    else:
        if since_ts < existing_min:
            ranges.append((since, datetime.fromtimestamp(existing_min, timezone.utc)))
        if int(end.timestamp()) > existing_max:
            ranges.append((datetime.fromtimestamp(existing_max + 60, timezone.utc), end))

    if not ranges:
        print("[coinbase] already covers the requested range — nothing to fetch")
    else:
        client = httpx.Client(
            base_url="https://api.exchange.coinbase.com",
            headers={"User-Agent": "PolymarketBacktest/1.0", "Accept": "application/json"},
            timeout=30.0,
        )
        try:
            for s, e in ranges:
                print(f"[coinbase] fetching {s.isoformat()} → {e.isoformat()}")
                for row in _coinbase_fetch_range(client, s, e):
                    if row and len(row) >= 6:
                        rows_by_ts[int(row[0])] = row
        finally:
            client.close()

    deduped = [rows_by_ts[k] for k in sorted(rows_by_ts)]

    def _w(f):
        w = csv.writer(f)
        w.writerow(["ts_unix", "low", "high", "open", "close", "volume"])
        w.writerows(deduped)

    _atomic_write(out_path, _w)
    if deduped:
        print(f"[coinbase] wrote {len(deduped)} rows → {out_path}  "
              f"({_iso(min(rows_by_ts))} → {_iso(max(rows_by_ts))})")
        if since_ts < min(rows_by_ts):
            print(f"[coinbase] NOTE: requested back to {_iso(since_ts)} but earliest available is "
                  f"{_iso(min(rows_by_ts))} — Coinbase 1-min depth limit (source limit, not a bug)")
    return out_path


# ---------------------------------------------------------------------------
# Fear & Greed Index (alternative.me) — full daily history (atomic write)
# ---------------------------------------------------------------------------

def fetch_fear_greed() -> Path:
    """Fetch full daily Fear & Greed history. Tiny dataset, refetched wholesale."""
    out_path = DATA_DIR / "fear_greed.json"
    print("[fng] fetching full daily history")

    r = httpx.get(
        "https://api.alternative.me/fng/",
        params={"limit": 0, "format": "json"},
        timeout=30.0,
        headers={"User-Agent": "PolymarketBacktest/1.0"},
    )
    r.raise_for_status()
    rows = r.json().get("data", [])
    print(f"[fng] received {len(rows)} daily values")
    _atomic_write(out_path, lambda f: json.dump(rows, f, indent=2))
    print(f"[fng] wrote → {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# Polymarket BTC 15-min markets via Gamma + CLOB (no auth) — incremental
# ---------------------------------------------------------------------------

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


def fetch_polymarket_btc_15m(since_ts: int, end_ts: Optional[int] = None) -> Path:
    """Pull the BTC 15-min dataset incrementally over [since_ts, now].

    Loads the existing JSON, fetches only slugs that are missing OR lack price
    history (covers new markets, the unresolved UMA-lag tail, and prior CLOB
    failures), merges by slug_ts preferring records that actually have history,
    and atomic-writes. Re-running converges to a fixpoint.
    """
    out_path = DATA_DIR / "polymarket_btc_15m.json"

    now_ts = end_ts if end_ts else int(datetime.now(timezone.utc).timestamp())
    end_ts_aligned = (now_ts // 900) * 900       # last completed 15-min boundary
    start_ts = (since_ts // 900) * 900

    by_ts: Dict[int, Dict[str, Any]] = {}
    if out_path.exists():
        _backup_once(out_path)
        with out_path.open() as f:
            for m in json.load(f):
                if "slug_ts" in m:
                    by_ts[m["slug_ts"]] = m

    # A market is "complete" iff it's present AND has price history. Fetch the rest.
    to_fetch: List[tuple] = []
    ts = start_ts
    while ts <= end_ts_aligned:
        m = by_ts.get(ts)
        if not (m and m.get("yes_price_history")):
            to_fetch.append((ts, f"btc-updown-15m-{ts}"))
        ts += 900

    print(f"[gamma] {len(by_ts)} markets already loaded; "
          f"{len(to_fetch)} slugs to (re)fetch in [{_iso(start_ts)} → {_iso(end_ts_aligned)}]")

    client = httpx.Client(timeout=30.0, headers={"User-Agent": "PolymarketBacktest/1.0"})
    new_markets: List[Dict[str, Any]] = []
    skipped_unresolved = skipped_404 = skipped_missing_meta = 0

    try:
        # --- Gamma metadata pass (only over to_fetch) ---
        for i, (slug_ts, slug) in enumerate(to_fetch):
            try:
                # closed=true required: Gamma's default filter EXCLUDES closed markets
                r = client.get(f"{GAMMA_BASE}/markets", params={"slug": slug, "closed": "true"})
            except Exception as e:  # noqa: BLE001
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
            final_price = event_meta.get("finalPrice")
            price_to_beat = event_meta.get("priceToBeat")

            try:
                clob_tokens = json.loads(m["clobTokenIds"])
                outcome_prices = json.loads(m["outcomePrices"])
            except Exception:  # noqa: BLE001
                skipped_missing_meta += 1
                continue

            new_markets.append({
                "slug": slug,
                "slug_ts": slug_ts,
                "market_id": m.get("id"),
                "condition_id": m.get("conditionId"),
                "start_time_iso": m.get("startTime") or (events_list[0].get("startDate") if events_list else None),
                "end_date_iso": m.get("endDate"),
                "yes_token_id": clob_tokens[0],
                "no_token_id": clob_tokens[1],
                "yes_resolved_to": float(outcome_prices[0]),
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
                print(f"[gamma] progress {i+1}/{len(to_fetch)} | newly resolved: {len(new_markets)}")
            time.sleep(0.10)

        print(f"[gamma] done. newly resolved: {len(new_markets)} | unresolved: {skipped_unresolved} "
              f"| missing meta: {skipped_missing_meta} | not found: {skipped_404}")

        # --- CLOB prices-history pass (minute YES prices for the new markets) ---
        print(f"[clob] pulling minute prices for {len(new_markets)} markets")
        for j, m in enumerate(new_markets):
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
            except Exception as e:  # noqa: BLE001
                m["yes_price_history"] = []
                m["clob_error"] = str(e)

            if (j + 1) % 100 == 0:
                with_data = sum(1 for x in new_markets[:j+1] if x.get("yes_price_history"))
                print(f"[clob] progress {j+1}/{len(new_markets)} | with price data: {with_data}")
            time.sleep(0.10)

    finally:
        client.close()

    # --- Merge: never overwrite a good record (one with history) with an empty re-fetch ---
    for m in new_markets:
        old = by_ts.get(m["slug_ts"])
        if m.get("yes_price_history") or old is None:
            by_ts[m["slug_ts"]] = m

    merged = sorted(by_ts.values(), key=lambda x: x["slug_ts"])
    _atomic_write(out_path, lambda f: json.dump(merged, f, indent=2))

    with_prices = sum(1 for m in merged if m.get("yes_price_history"))
    ts_all = [m["slug_ts"] for m in merged]
    print(f"[polymarket] dataset now {len(merged)} markets ({with_prices} with price history) → {out_path}")
    if ts_all:
        print(f"[polymarket] range {_iso(min(ts_all))} → {_iso(max(ts_all))}")
    return out_path


# ---------------------------------------------------------------------------
# Manifest — committed record of the (gitignored) raw data's state
# ---------------------------------------------------------------------------

def write_manifest() -> Path:
    """Write build-steps/data/MANIFEST.json describing the current raw data."""
    manifest: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "regenerate": "python build-steps/fetch_data.py --full",
        "sources": {},
    }

    pm = DATA_DIR / "polymarket_btc_15m.json"
    if pm.exists():
        data = json.load(pm.open())
        ts = sorted(m["slug_ts"] for m in data if "slug_ts" in m)
        manifest["sources"]["polymarket_btc_15m.json"] = {
            "markets": len(data),
            "with_price_history": sum(1 for m in data if m.get("yes_price_history")),
            "slug_ts_min": ts[0] if ts else None,
            "slug_ts_max": ts[-1] if ts else None,
            "date_range_utc": [_iso(ts[0]), _iso(ts[-1])] if ts else None,
            "sha256": _sha256(pm),
            "schema": "list of market dicts keyed by slug_ts; each has yes_price_history (per-min YES prices)",
        }

    cb = DATA_DIR / "coinbase_btc_1min.csv"
    if cb.exists():
        with cb.open() as f:
            rd = csv.reader(f)
            next(rd, None)
            tss = [int(row[0]) for row in rd if row]
        manifest["sources"]["coinbase_btc_1min.csv"] = {
            "rows": len(tss),
            "date_range_utc": [_iso(min(tss)), _iso(max(tss))] if tss else None,
            "sha256": _sha256(cb),
            "schema": "ts_unix,low,high,open,close,volume (1-min BTC-USD)",
        }

    fng = DATA_DIR / "fear_greed.json"
    if fng.exists():
        rows = json.load(fng.open())
        manifest["sources"]["fear_greed.json"] = {
            "rows": len(rows),
            "sha256": _sha256(fng),
            "schema": "alternative.me daily Fear & Greed values",
        }

    _atomic_write(MANIFEST_PATH, lambda f: json.dump(manifest, f, indent=2))
    print(f"[manifest] wrote → {MANIFEST_PATH}")
    return MANIFEST_PATH


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Incremental, full-range data fetcher.")
    parser.add_argument("--days", type=int, default=14,
                        help="Back-compat: fetch the last N days (used only if --since/--full absent).")
    parser.add_argument("--since", default=None,
                        help="Start of fetch: ISO date/datetime or unix ts. Overrides --days.")
    parser.add_argument("--full", action="store_true",
                        help=f"Fetch the entire available history (from {EARLIEST_SINCE_ISO}).")
    parser.add_argument("--api-key", help="(unused; kept for back-compat)")
    parser.add_argument("--sources", nargs="+", default=["coinbase", "fng", "polymarket"],
                        choices=["coinbase", "fng", "polymarket"])
    args = parser.parse_args()

    now_ts = int(datetime.now(timezone.utc).timestamp())
    if args.full:
        since_ts = _parse_since(EARLIEST_SINCE_ISO)
    elif args.since is not None:
        since_ts = _parse_since(args.since)
    else:
        since_ts = now_ts - args.days * 86400
    print(f"[fetch] since={_iso(since_ts)}  →  now={_iso(now_ts)}  sources={args.sources}")

    if "coinbase" in args.sources:
        fetch_coinbase_btc(since_ts=since_ts)
    if "fng" in args.sources:
        fetch_fear_greed()
    if "polymarket" in args.sources:
        fetch_polymarket_btc_15m(since_ts=since_ts)

    write_manifest()


if __name__ == "__main__":
    main()
