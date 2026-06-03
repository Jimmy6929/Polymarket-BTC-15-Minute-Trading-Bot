"""Binance USD-M perpetual-futures data fetcher (klines + funding).

Phase 1 of the crypto-perps pivot (see ~/.claude/plans + memory crypto-perps-pivot).
Pulls daily klines and 8h funding-rate history for BTC/ETH/SOL perps into
build-steps/data/perps/, for the TSMOM prop-eval kill-test (perps_backtester.py).

Mirrors fetch_data.py's discipline: incremental/idempotent re-fetch, atomic writes,
a manifest with sha256 + date ranges. Standalone (no heavy imports).

Two confirmed Binance gotchas this handles:
  1. fundingRate with startTime=0 returns only the RECENT tail — must paginate
     FORWARD from an explicit early startTime (advance startTime = last+1).
  2. The final kline is in-progress (closeTime in the future) — dropped, so an
     unclosed bar never enters the backtest.

Usage:
    python build-steps/perps_fetch.py                 # incremental fetch all symbols
    python build-steps/perps_fetch.py --symbols BTCUSDT
"""
import argparse
import csv
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import httpx

BASE = "https://fapi.binance.com"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
DATA_DIR = Path(__file__).resolve().parent / "data" / "perps"
# Explicit early epoch to paginate forward from (before any symbol listed).
HISTORY_START_MS = 1546300800000  # 2019-01-01 UTC
KLINE_INTERVAL = "1d"
KLINE_LIMIT = 1500   # fapi klines max
FUNDING_LIMIT = 1000  # fapi fundingRate max
RATE_SLEEP = 0.25


# --- tiny crash-safe helpers (mirror fetch_data.py:47-96, re-implemented standalone) ---
def _atomic_write(path: Path, write_fn: Callable[[Path], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        write_fn(tmp_path)
        with tmp_path.open("rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _get(client: httpx.Client, path: str, params: Dict) -> list:
    """GET with simple 429/5xx backoff."""
    for attempt in range(6):
        r = client.get(BASE + path, params=params, timeout=20.0)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 418):
            wait = int(r.headers.get("Retry-After", 2 ** attempt))
            print(f"[perps_fetch] rate-limited ({r.status_code}); sleeping {wait}s")
            time.sleep(wait)
            continue
        if 500 <= r.status_code < 600:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
    raise RuntimeError(f"GET {path} failed after retries")


def server_now_ms(client: httpx.Client) -> int:
    return int(_get(client, "/fapi/v1/time", {})["serverTime"])


# --- CSV round-trip ---
def _read_existing(path: Path, ts_col: int) -> Tuple[List[List[str]], Optional[int]]:
    if not path.exists():
        return [], None
    with path.open() as f:
        rows = list(csv.reader(f))
    if len(rows) <= 1:
        return rows, None
    last_ts = int(rows[-1][ts_col])
    return rows, last_ts


def _write_csv(path: Path, header: List[str], rows: List[List]) -> None:
    def _w(p: Path):
        with p.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
    _atomic_write(path, _w)


# --- klines ---
def fetch_klines(client: httpx.Client, symbol: str, start_ms: int, now_ms: int) -> List[List]:
    """Daily klines from start_ms forward. Returns rows
    [open_time_ms, close_time_ms, open, high, low, close, volume], CLOSED bars only."""
    out: List[List] = []
    cur = start_ms
    while True:
        data = _get(client, "/fapi/v1/klines",
                    {"symbol": symbol, "interval": KLINE_INTERVAL,
                     "startTime": cur, "limit": KLINE_LIMIT})
        if not data:
            break
        for k in data:
            open_t, close_t = int(k[0]), int(k[6])
            if close_t > now_ms:
                continue  # in-progress bar — never enters the dataset
            out.append([open_t, close_t, k[1], k[2], k[3], k[4], k[5]])
        cur = int(data[-1][0]) + 1
        if len(data) < KLINE_LIMIT:
            break
        time.sleep(RATE_SLEEP)
    return out


# --- funding ---
def fetch_funding(client: httpx.Client, symbol: str, start_ms: int) -> List[List]:
    """Funding history from start_ms FORWARD (never startTime=0). Returns rows
    [funding_time_ms, funding_rate]."""
    out: List[List] = []
    cur = start_ms
    while True:
        data = _get(client, "/fapi/v1/fundingRate",
                    {"symbol": symbol, "startTime": cur, "limit": FUNDING_LIMIT})
        if not data:
            break
        for d in data:
            out.append([int(d["fundingTime"]), d["fundingRate"]])
        cur = int(data[-1]["fundingTime"]) + 1
        if len(data) < FUNDING_LIMIT:
            break
        time.sleep(RATE_SLEEP)
    return out


def _incremental(path: Path, header: List[str], ts_col: int,
                 fetch_fn: Callable[[int], List[List]]) -> Dict:
    """Read existing, fetch only the missing tail, dedupe by ts, atomic-write."""
    existing_rows, last_ts = _read_existing(path, ts_col)
    start = (last_ts + 1) if last_ts is not None else HISTORY_START_MS
    new_rows = fetch_fn(start)

    # merge by timestamp key (existing data rows minus header)
    by_ts: Dict[int, List] = {}
    for r in existing_rows[1:] if existing_rows else []:
        by_ts[int(r[ts_col])] = r
    for r in new_rows:
        by_ts[int(r[ts_col])] = r
    merged = [by_ts[k] for k in sorted(by_ts)]
    _write_csv(path, header, merged)
    return {
        "rows": len(merged),
        "added": len(new_rows),
        "date_range_utc": [_iso(int(merged[0][ts_col])), _iso(int(merged[-1][ts_col]))] if merged else None,
        "sha256": _sha256(path),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Binance perp klines + funding fetcher")
    ap.add_argument("--symbols", nargs="*", default=SYMBOLS)
    args = ap.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    manifest: Dict[str, Dict] = {}
    with httpx.Client(headers={"User-Agent": "perps-fetch/1.0"}) as client:
        now_ms = server_now_ms(client)
        print(f"[perps_fetch] server now = {_iso(now_ms)}")
        for sym in args.symbols:
            kpath = DATA_DIR / f"{sym}_1d.csv"
            fpath = DATA_DIR / f"{sym}_funding.csv"
            print(f"[perps_fetch] {sym}: klines…")
            kstat = _incremental(
                kpath, ["open_time_ms", "close_time_ms", "open", "high", "low", "close", "volume"],
                0, lambda s: fetch_klines(client, sym, s, now_ms))
            print(f"  klines  rows={kstat['rows']} (+{kstat['added']}) {kstat['date_range_utc']}")
            print(f"[perps_fetch] {sym}: funding…")
            fstat = _incremental(
                fpath, ["funding_time_ms", "funding_rate"],
                0, lambda s: fetch_funding(client, sym, s))
            print(f"  funding rows={fstat['rows']} (+{fstat['added']}) {fstat['date_range_utc']}")
            manifest[f"{sym}_1d.csv"] = {**kstat, "schema": "open_time_ms,close_time_ms,open,high,low,close,volume"}
            manifest[f"{sym}_funding.csv"] = {**fstat, "schema": "funding_time_ms,funding_rate"}

    man = {
        "generated_at": _iso(now_ms),
        "regenerate": "python build-steps/perps_fetch.py",
        "source": "Binance USD-M Futures (fapi) /klines 1d + /fundingRate",
        "sources": manifest,
    }
    _atomic_write(DATA_DIR / "perps_MANIFEST.json",
                  lambda p: p.write_text(json.dumps(man, indent=2)))
    print(f"[perps_fetch] manifest → {DATA_DIR / 'perps_MANIFEST.json'}")


if __name__ == "__main__":
    main()
