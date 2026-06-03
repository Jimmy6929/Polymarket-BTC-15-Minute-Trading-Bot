"""Crypto-perp TSMOM prop-eval KILL-TEST (BTC/ETH/SOL daily).

Phase 1 of the crypto-perps pivot. Falsification tool, not an optimiser. The gate
is NOT "positive Sharpe" but "would a parameter-light vol-scaled time-series-momentum
strategy PASS a crypto prop-firm evaluation (hit +target before breaching a trailing
drawdown) after realistic frictions (fees + funding)."

Reuses the repo's statistical-honesty machinery: Deflated Sharpe (imported, frozen),
bootstrap EV CI + Wilson CI (copied from real_backtester.py), sealed holdout, and a
FULL sweep grid (never argmax silently). Consumes data from perps_fetch.py.

Strategy (parameter-light):
  signal  : TSMOM, sign of trailing-L-day return per asset (long >0 / short <0)
  sizing  : vol-scaled to a constant risk target, w_i = sign * vt/(sqrt(n)*rv_i),
            clipped to +/-leverage cap, portfolio gross-capped, compounded on equity
  funding : a held perp pays the 8h funding on notional (long pays when funding>0)

Look-ahead guard (the validity core): the signal and realized-vol at day t use closes
strictly THROUGH t-1; the day-t close enters ONLY the realized P&L close(t-1)->close(t).

Usage:
  python build-steps/perps_backtester.py --build-splits --holdout-months 6
  python build-steps/perps_backtester.py --split train --sweep-lookbacks 7,14,30,60,90
"""
import argparse
import json
import math
import random
import sys
from datetime import timedelta
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stats.deflated_sharpe import deflated_sharpe  # noqa: E402  (frozen, import-only)

DATA_DIR = Path(__file__).resolve().parent / "data" / "perps"
RUNS_DIR = Path(__file__).resolve().parent / "data" / "runs"
SPLITS_PATH = DATA_DIR / "splits.json"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
ANN = 365.0  # crypto trades 365d/yr


# --------------------------------------------------------------------------- #
# Data loading + alignment
# --------------------------------------------------------------------------- #
def load_panel():
    """Return (dates, close[N,3], funding[N,3], logret[N,3]) on the common date index."""
    per_sym = {}
    for sym in SYMBOLS:
        k = pd.read_csv(DATA_DIR / f"{sym}_1d.csv")
        k["date"] = pd.to_datetime(k["open_time_ms"], unit="ms", utc=True).dt.floor("D")
        k = k.set_index("date").sort_index()
        f = pd.read_csv(DATA_DIR / f"{sym}_funding.csv")
        f["date"] = pd.to_datetime(f["funding_time_ms"], unit="ms", utc=True).dt.floor("D")
        # Daily funding = SUM of the (~3) 8h prints during that day (a long pays each).
        fund_daily = f.groupby("date")["funding_rate"].sum()
        df = pd.DataFrame({"close": k["close"].astype(float)})
        df["funding"] = fund_daily.reindex(df.index).fillna(0.0).astype(float)
        per_sym[sym] = df

    common = None
    for df in per_sym.values():
        common = df.index if common is None else common.intersection(df.index)
    common = common.sort_values()

    close = np.column_stack([per_sym[s].loc[common, "close"].values for s in SYMBOLS])
    funding = np.column_stack([per_sym[s].loc[common, "funding"].values for s in SYMBOLS])
    logret = np.full_like(close, np.nan)
    logret[1:] = np.log(close[1:] / close[:-1])
    return common, close, funding, logret


# --------------------------------------------------------------------------- #
# Splits
# --------------------------------------------------------------------------- #
def build_splits(holdout_months: int, embargo_days: int) -> Dict:
    dates, *_ = load_panel()
    max_date = dates[-1]
    holdout_start = max_date - timedelta(days=holdout_months * 30)
    train_end = holdout_start - timedelta(days=embargo_days)
    payload = {
        "boundary_date": holdout_start.isoformat(),
        "train": [dates[0].isoformat(), train_end.isoformat()],
        "holdout": [holdout_start.isoformat(), max_date.isoformat()],
        "embargo_days": embargo_days,
        "n_train": int((dates < holdout_start - timedelta(days=embargo_days)).sum()),
        "n_holdout": int((dates >= holdout_start).sum()),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SPLITS_PATH.write_text(json.dumps(payload, indent=2))
    print("[splits] generated:")
    for k, v in payload.items():
        print(f"  {k}: {v}")
    return payload


def split_mask(dates: pd.DatetimeIndex, split: str) -> np.ndarray:
    if split == "both":
        return np.ones(len(dates), dtype=bool)
    if not SPLITS_PATH.exists():
        raise FileNotFoundError(f"{SPLITS_PATH} missing — run --build-splits first.")
    s = json.loads(SPLITS_PATH.read_text())
    hold_start = pd.Timestamp(s["boundary_date"])
    embargo = timedelta(days=s["embargo_days"])
    if split == "train":
        return np.asarray(dates < hold_start - embargo)
    if split == "holdout":
        return np.asarray(dates >= hold_start)
    raise ValueError(split)


# --------------------------------------------------------------------------- #
# Backtest one lookback (look-ahead-safe; vectors indexed by day)
# --------------------------------------------------------------------------- #
def run_lookback(close, funding, logret, L, vol_window, vol_target,
                 lcap, gross_cap, fee_bps, slip_bps) -> np.ndarray:
    N, na = close.shape
    warmup = max(L, vol_window) + 1
    rets = np.full(N, np.nan)
    prev_w = np.zeros(na)
    cost_rate = (fee_bps + slip_bps) / 1e4
    for t in range(warmup, N):
        w = np.zeros(na)
        for i in range(na):
            mom = close[t - 1, i] / close[t - 1 - L, i] - 1.0   # closes through t-1 only
            window = logret[t - vol_window:t, i]                # logret through t-1 only
            rv = np.nanstd(window, ddof=1) * math.sqrt(ANN)
            if rv > 0 and math.isfinite(rv):
                w[i] = np.clip(np.sign(mom) * vol_target / (math.sqrt(na) * rv), -lcap, lcap)
        g = float(np.sum(np.abs(w)))
        if g > gross_cap and g > 0:
            w *= gross_cap / g
        r = close[t, :] / close[t - 1, :] - 1.0                 # day-t close: realized P&L ONLY
        f = funding[t, :]
        turn = np.abs(w - prev_w)
        rets[t] = float(np.sum(w * r - w * f - turn * cost_rate))
        prev_w = w
    return rets


# --------------------------------------------------------------------------- #
# Stats helpers (deflated_sharpe imported; bootstrap/Wilson copied from real_backtester.py)
# --------------------------------------------------------------------------- #
def sharpe_ann(rets: np.ndarray) -> float:
    sd = np.std(rets, ddof=1)
    return float(np.mean(rets) / sd * math.sqrt(ANN)) if sd > 0 else 0.0


def max_drawdown(rets: np.ndarray) -> float:
    eq = np.cumprod(1.0 + rets)
    peak = np.maximum.accumulate(eq)
    return float(np.min(eq / peak - 1.0))


def cagr(rets: np.ndarray) -> float:
    eq = float(np.prod(1.0 + rets))
    return eq ** (ANN / len(rets)) - 1.0 if len(rets) > 0 and eq > 0 else float("nan")


def bootstrap_ci(rets: np.ndarray):
    random.seed(42)
    pnls = list(rets)
    n = len(pnls)
    boots = sorted(sum(random.choice(pnls) for _ in range(n)) / n for _ in range(10000))
    return boots[250], boots[9750]


def wilson_ci(k: int, n: int):
    if n == 0:
        return 0.0, 0.0
    p = k / n
    z = 1.96
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def prop_eval(rets: np.ndarray, target: float, max_dd: float, horizon: int) -> Dict:
    """Rolling prop-eval: from each start, pass if equity reaches 1+target before an
    EOD-trailing-DD breach within `horizon` days."""
    n = len(rets)
    if n < horizon + 1:
        return {"n": 0, "pass_rate": float("nan"), "breach_rate": float("nan"),
                "timeout_rate": float("nan"), "median_days": float("nan"),
                "wlo": float("nan"), "whi": float("nan")}
    npass = nbreach = ntimeout = 0
    days: List[int] = []
    for s in range(0, n - horizon + 1):
        eq = 1.0
        hwm = 1.0
        outcome = "timeout"
        for k in range(s, s + horizon):
            eq *= (1.0 + rets[k])
            if eq >= 1.0 + target:
                outcome = "pass"
                days.append(k - s + 1)
                break
            hwm = max(hwm, eq)               # end-of-day high-water mark
            if eq <= hwm * (1.0 - max_dd):
                outcome = "breach"
                break
        npass += outcome == "pass"
        nbreach += outcome == "breach"
        ntimeout += outcome == "timeout"
    nstarts = n - horizon + 1
    wlo, whi = wilson_ci(npass, nstarts)
    return {"n": nstarts, "pass_rate": npass / nstarts, "breach_rate": nbreach / nstarts,
            "timeout_rate": ntimeout / nstarts,
            "median_days": float(np.median(days)) if days else float("nan"),
            "wlo": wlo, "whi": whi}


# --------------------------------------------------------------------------- #
def run(args) -> None:
    dates, close, funding, logret = load_panel()
    mask = split_mask(dates, args.split)
    lookbacks = [int(x) for x in args.sweep_lookbacks.split(",") if x.strip()]
    n_configs = len(lookbacks)

    print(f"\nPERP TSMOM PROP-EVAL KILL-TEST  [split={args.split}]")
    print(f"  panel: {len(dates)} days {dates[0].date()}..{dates[-1].date()}  "
          f"({SYMBOLS})  |  split days={int(mask.sum())}")
    print(f"  vol_target={args.vol_target} vol_window={args.vol_window} "
          f"max_lev={args.max_leverage} gross_cap={args.gross_cap} "
          f"fee={args.fee_bps}bps slip={args.slippage_bps}bps")
    print(f"  prop eval: +{args.profit_target:.0%} target / {args.max_dd:.0%} EOD-trailing DD / "
          f"{args.eval_horizon}d horizon")
    print("  LOOK-AHEAD: signal & vol use closes through t-1; day-t close = realized P&L only.\n")

    grid = []
    best = None
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  {'L':>4} {'ndays':>6} {'CAGR':>8} {'Sharpe':>7} {'maxDD':>7} {'DSR':>6} "
          f"{'pass%':>7} {'wlo%':>6} {'breach%':>8} {'med_d':>6}")
    for L in lookbacks:
        full = run_lookback(close, funding, logret, L, args.vol_window, args.vol_target,
                            args.max_leverage, args.gross_cap, args.fee_bps, args.slippage_bps)
        sl = full[mask]
        sl = sl[~np.isnan(sl)]
        if len(sl) < 30:
            print(f"  {L:>4}  too few days ({len(sl)})")
            continue
        ds = deflated_sharpe(list(sl), n_configs)
        ev = prop_eval(sl, args.profit_target, args.max_dd, args.eval_horizon)
        row = {"L": L, "ndays": len(sl), "cagr": cagr(sl), "sharpe": sharpe_ann(sl),
               "maxdd": max_drawdown(sl), "dsr": ds["dsr"], "rets": sl, **ev}
        grid.append(row)
        print(f"  {L:>4} {len(sl):>6} {row['cagr']:>+7.1%} {row['sharpe']:>+7.2f} "
              f"{row['maxdd']:>+7.1%} {ds['dsr']:>6.3f} {ev['pass_rate']:>6.1%} "
              f"{ev['wlo']:>5.1%} {ev['breach_rate']:>7.1%} {ev['median_days']:>6.0f}")
        if best is None or (ev["pass_rate"], row["sharpe"]) > (best["pass_rate"], best["sharpe"]):
            best = row

    if best is None:
        print("\nNo config produced enough days. Stop.")
        return

    # Detailed block for the best-by-pass-rate config (deflated by the full grid).
    b = best
    lo, hi = bootstrap_ci(b["rets"])
    print(f"\nBEST-BY-PASS-RATE: L={b['L']}  (DSR deflated by n_configs={n_configs} = full grid)")
    print(f"  days={b['ndays']}  CAGR={b['cagr']:+.1%}  Sharpe={b['sharpe']:+.2f}  "
          f"maxDD={b['maxdd']:+.1%}  DSR={b['dsr']:.3f}")
    print(f"  daily-return bootstrap 95% CI: [{lo:+.5f}, {hi:+.5f}]   "
          f"(mean {np.mean(b['rets']):+.5f})")
    print(f"  prop-eval pass={b['pass_rate']:.1%} (Wilson95 [{b['wlo']:.1%},{b['whi']:.1%}], "
          f"n={b['n']} overlapping starts — CI understates uncertainty)  "
          f"breach={b['breach_rate']:.1%}  timeout={b['timeout_rate']:.1%}  "
          f"median_days_to_pass={b['median_days']:.0f}")

    # Per-year stability (regime check).
    sl_dates = dates[mask][-len(b["rets"]):]
    yr = pd.Series(b["rets"], index=sl_dates).groupby(lambda d: d.year)
    print("  per-year:  " + "  ".join(
        f"{y}:CAGR{(np.prod(1+g.values)**(ANN/len(g))-1):+.0%}/Shrp{sharpe_ann(g.values):+.1f}"
        for y, g in yr if len(g) > 20))

    # Persist the best config's daily series.
    out = RUNS_DIR / f"perps_{args.split}_L{b['L']}.csv"
    pd.DataFrame({"date": sl_dates, "ret": b["rets"],
                  "equity": np.cumprod(1 + b["rets"])}).to_csv(out, index=False)
    print(f"  daily series → {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Perp TSMOM prop-eval kill-test")
    ap.add_argument("--build-splits", action="store_true")
    ap.add_argument("--holdout-months", type=int, default=6)
    ap.add_argument("--embargo-days", type=int, default=1)
    ap.add_argument("--split", choices=["train", "holdout", "both"], default="train")
    ap.add_argument("--unseal-holdout", action="store_true",
                    help="required to run --split holdout/both (pre-registration guard)")
    ap.add_argument("--sweep-lookbacks", type=str, default="7,14,30,60,90")
    ap.add_argument("--vol-target", type=float, default=0.20)
    ap.add_argument("--vol-window", type=int, default=30)
    ap.add_argument("--max-leverage", type=float, default=3.0)
    ap.add_argument("--gross-cap", type=float, default=5.0)
    ap.add_argument("--profit-target", type=float, default=0.10)
    ap.add_argument("--max-dd", type=float, default=0.06)
    ap.add_argument("--eval-horizon", type=int, default=60)
    ap.add_argument("--fee-bps", type=float, default=4.5)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    args = ap.parse_args()

    if args.build_splits:
        build_splits(args.holdout_months, args.embargo_days)
        return
    if args.split in ("holdout", "both") and not args.unseal_holdout:
        print("REFUSED: --split holdout/both needs --unseal-holdout (the holdout is "
              "pre-registered single-shot). Run train first and pre-register the config.")
        return
    run(args)


if __name__ == "__main__":
    main()
