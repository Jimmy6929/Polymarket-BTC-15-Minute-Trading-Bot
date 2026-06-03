"""Fetch a BROAD universe of Binance USD-M perps for the cross-sectional momentum
kill-test (the "last roll" — see memory crypto-perps-pivot).

Selects the top-N USDT perpetuals by 24h quote volume (TRADING, PERPETUAL), then
pulls daily klines + funding for each (reusing perps_fetch.py's paginators). Writes
build-steps/data/perps/universe.json.

SURVIVORSHIP CAVEAT (honest): selecting by CURRENT liquidity omits delisted/dead
coins — a known upward bias in crypto cross-sectional studies. The recent sealed
holdout (trading the live universe) is the real mitigant.

Usage:  python build-steps/perps_fetch_universe.py --top 80
"""
import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import perps_fetch as pf  # noqa: E402  (reuse fetch_klines/fetch_funding/_incremental/_get)


def select_universe(client: httpx.Client, top: int):
    info = pf._get(client, "/fapi/v1/exchangeInfo", {})
    perp_usdt = {
        s["symbol"] for s in info["symbols"]
        if s.get("contractType") == "PERPETUAL"
        and s.get("status") == "TRADING"
        and s.get("quoteAsset") == "USDT"
    }
    tickers = pf._get(client, "/fapi/v1/ticker/24hr", {})
    ranked = sorted(
        ((t["symbol"], float(t["quoteVolume"])) for t in tickers if t["symbol"] in perp_usdt),
        key=lambda x: -x[1],
    )
    return ranked[:top]


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch broad perp universe")
    ap.add_argument("--top", type=int, default=80)
    args = ap.parse_args()

    pf.DATA_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {}
    with httpx.Client(headers={"User-Agent": "perps-fetch/1.0"}) as client:
        now_ms = pf.server_now_ms(client)
        universe = select_universe(client, args.top)
        print(f"[universe] selected top {len(universe)} USDT perps by 24h volume")
        kept = []
        for idx, (sym, qv) in enumerate(universe, 1):
            try:
                kstat = pf._incremental(
                    pf.DATA_DIR / f"{sym}_1d.csv",
                    ["open_time_ms", "close_time_ms", "open", "high", "low", "close", "volume"],
                    0, lambda s: pf.fetch_klines(client, sym, s, now_ms))
                fstat = pf._incremental(
                    pf.DATA_DIR / f"{sym}_funding.csv",
                    ["funding_time_ms", "funding_rate"],
                    0, lambda s: pf.fetch_funding(client, sym, s))
            except Exception as e:
                print(f"  [{idx:>2}/{len(universe)}] {sym:<14} SKIPPED ({type(e).__name__}: {e})")
                continue
            kept.append(sym)
            manifest[sym] = {"quote_volume_24h": qv, "kline_rows": kstat["rows"],
                             "kline_range": kstat["date_range_utc"], "funding_rows": fstat["rows"]}
            print(f"  [{idx:>2}/{len(universe)}] {sym:<14} qv={qv:>15,.0f}  "
                  f"klines={kstat['rows']:>4} ({kstat['date_range_utc'][0][:10] if kstat['date_range_utc'] else '—'})")
        universe = [(s, qv) for s, qv in universe if s in kept]

    pf._atomic_write(
        pf.DATA_DIR / "universe.json",
        lambda p: p.write_text(json.dumps({
            "generated_at": pf._iso(now_ms),
            "selection": "top USDT PERPETUAL TRADING by 24h quoteVolume",
            "survivorship_note": "current-liquidity selection omits delisted coins (upward bias); holdout is the mitigant",
            "n": len(universe),
            "symbols": [s for s, _ in universe],
            "detail": manifest,
        }, indent=2)))
    print(f"[universe] → {pf.DATA_DIR / 'universe.json'}  ({len(universe)} symbols)")


if __name__ == "__main__":
    main()
