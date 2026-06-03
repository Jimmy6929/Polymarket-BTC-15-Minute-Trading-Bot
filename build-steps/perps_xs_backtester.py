"""Cross-sectional momentum KILL-TEST across a broad perp universe (the "last roll").

The 3-asset directional momentum test died OOS (regime/survivorship artifact). This
tests the REAL documented edge: cross-sectional momentum — each day rank the available
universe by L-day return, go LONG the top quantile and SHORT the bottom quantile,
equal-weight, dollar-neutral. This is (a) diversified across the whole universe (not
3 ex-post winners) and (b) ~market-neutral (better drawdown profile). If THIS dies
OOS too, the pre-committed stop rule fires: stop edge-hunting, pivot to skill-monetization.

Primary verdict = EDGE PERSISTENCE (Sharpe / DSR / bootstrap EV CI), which is
scale-free — does the long/short spread generalize to the sealed holdout? Prop-eval
pass-rate is secondary (sizing-dependent).

Look-ahead guard: ranking uses closes through t-1; the day-t close enters only realized
P&L. Universe is time-varying (a name is eligible only when it has the needed history).

Reuses stats from perps_backtester.py (sharpe/DD/bootstrap/prop_eval/splits) + the
frozen deflated_sharpe. Consumes data from perps_fetch_universe.py.

Usage:
  python build-steps/perps_xs_backtester.py --split train --sweep-lookbacks 7,14,30,60,90
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stats.deflated_sharpe import deflated_sharpe  # noqa: E402
import perps_backtester as pb  # noqa: E402  (sharpe_ann, max_drawdown, cagr, bootstrap_ci, prop_eval, split_mask)

DATA_DIR = Path(__file__).resolve().parent / "data" / "perps"
RUNS_DIR = Path(__file__).resolve().parent / "data" / "runs"
ANN = pb.ANN


def load_universe_panel():
    uni = json.loads((DATA_DIR / "universe.json").read_text())
    closes, fundings = {}, {}
    for sym in uni["symbols"]:
        kp = DATA_DIR / f"{sym}_1d.csv"
        if not kp.exists():
            continue
        k = pd.read_csv(kp)
        k["date"] = pd.to_datetime(k["open_time_ms"], unit="ms", utc=True).dt.floor("D")
        closes[sym] = k.set_index("date")["close"].astype(float)
        fp = DATA_DIR / f"{sym}_funding.csv"
        if fp.exists():
            f = pd.read_csv(fp)
            f["date"] = pd.to_datetime(f["funding_time_ms"], unit="ms", utc=True).dt.floor("D")
            fundings[sym] = f.groupby("date")["funding_rate"].sum()
    close_df = pd.DataFrame(closes).sort_index()
    funding_df = pd.DataFrame(fundings).reindex(close_df.index).reindex(columns=close_df.columns).fillna(0.0)
    return close_df.index, close_df.values, funding_df.values, list(close_df.columns)


def run_xs(close, funding, L, q, min_names, fee_bps, slip_bps) -> np.ndarray:
    """Dollar-neutral cross-sectional momentum daily returns. NaN before warmup."""
    N, M = close.shape
    warmup = L + 1
    rets = np.full(N, np.nan)
    prev_w = np.zeros(M)
    cost_rate = (fee_bps + slip_bps) / 1e4
    for t in range(warmup, N):
        c_prev, c_base, c_now = close[t - 1], close[t - 1 - L], close[t]
        valid = (np.isfinite(c_prev) & np.isfinite(c_base) & np.isfinite(c_now)
                 & (c_base > 0) & (c_prev > 0))
        idx = np.where(valid)[0]
        w = np.zeros(M)
        if len(idx) >= min_names:
            mom = c_prev[idx] / c_base[idx] - 1.0
            order = idx[np.argsort(mom)]            # ascending: losers first
            n_side = max(1, int(len(idx) * q))
            w[order[-n_side:]] = 1.0 / n_side       # long top quantile
            w[order[:n_side]] = -1.0 / n_side       # short bottom quantile
        r = np.where(np.isfinite(c_now) & np.isfinite(c_prev) & (c_prev > 0), c_now / c_prev - 1.0, 0.0)
        f = np.nan_to_num(funding[t])
        turn = np.abs(w - prev_w)
        rets[t] = float(np.sum(w * r) - np.sum(w * f) - np.sum(turn) * cost_rate)
        prev_w = w
    return rets


def run(args) -> None:
    dates, close, funding, symbols = load_universe_panel()
    mask = pb.split_mask(dates, args.split)
    lookbacks = [int(x) for x in args.sweep_lookbacks.split(",") if x.strip()]
    n_configs = len(lookbacks)
    avg_names = int(np.nanmean(np.sum(np.isfinite(close), axis=1)))

    print(f"\nCROSS-SECTIONAL MOMENTUM KILL-TEST  [split={args.split}]")
    print(f"  universe: {len(symbols)} perps, {len(dates)} days "
          f"{dates[0].date()}..{dates[-1].date()}  (avg {avg_names} names/day live)")
    print(f"  quantile={args.quantile} (long top / short bottom)  min_names={args.min_names}  "
          f"fee={args.fee_bps}bps slip={args.slippage_bps}bps  split days={int(mask.sum())}")
    print(f"  prop eval: +{args.profit_target:.0%}/{args.max_dd:.0%}-trailing/{args.eval_horizon}d  "
          f"(secondary — Sharpe/DSR/EV are the primary verdict)")
    print("  LOOK-AHEAD: ranking uses closes through t-1; day-t close = realized P&L only.\n")

    grid, best = [], None
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  {'L':>4} {'ndays':>6} {'CAGR':>8} {'Sharpe':>7} {'maxDD':>7} {'DSR':>6} "
          f"{'EV_lo':>9} {'pass%':>6} {'breach%':>8}")
    for L in lookbacks:
        full = run_xs(close, funding, L, args.quantile, args.min_names, args.fee_bps, args.slippage_bps)
        sl = full[mask]
        sl = sl[~np.isnan(sl)]
        if len(sl) < 30:
            print(f"  {L:>4}  too few days ({len(sl)})")
            continue
        ds = deflated_sharpe(list(sl), n_configs)
        ev = pb.prop_eval(sl, args.profit_target, args.max_dd, args.eval_horizon)
        lo, hi = pb.bootstrap_ci(sl)
        row = {"L": L, "ndays": len(sl), "cagr": pb.cagr(sl), "sharpe": pb.sharpe_ann(sl),
               "maxdd": pb.max_drawdown(sl), "dsr": ds["dsr"], "ev_lo": lo, "ev_hi": hi,
               "rets": sl, **ev}
        grid.append(row)
        print(f"  {L:>4} {len(sl):>6} {row['cagr']:>+7.1%} {row['sharpe']:>+7.2f} "
              f"{row['maxdd']:>+7.1%} {ds['dsr']:>6.3f} {lo:>+9.5f} {ev['pass_rate']:>5.1%} "
              f"{ev['breach_rate']:>7.1%}")
        # rank by edge (Sharpe), not pass-rate — primary verdict is edge persistence
        if best is None or row["sharpe"] > best["sharpe"]:
            best = row

    if best is None:
        print("\nNo config produced enough days. Stop.")
        return

    b = best
    print(f"\nBEST-BY-SHARPE: L={b['L']}  (DSR deflated by n_configs={n_configs})")
    print(f"  days={b['ndays']}  CAGR={b['cagr']:+.1%}  Sharpe={b['sharpe']:+.2f}  "
          f"maxDD={b['maxdd']:+.1%}  DSR={b['dsr']:.3f}")
    print(f"  daily-return bootstrap 95% CI: [{b['ev_lo']:+.5f}, {b['ev_hi']:+.5f}]  "
          f"(mean {np.mean(b['rets']):+.5f})")
    print(f"  prop-eval (secondary): pass={b['pass_rate']:.1%} breach={b['breach_rate']:.1%} "
          f"(market-neutral book; sizing-dependent)")
    sl_dates = dates[mask][-len(b["rets"]):]
    yr = pd.Series(b["rets"], index=sl_dates).groupby(lambda d: d.year)
    print("  per-year:  " + "  ".join(
        f"{y}:Shrp{pb.sharpe_ann(g.values):+.1f}" for y, g in yr if len(g) > 20))
    out = RUNS_DIR / f"perps_xs_{args.split}_L{b['L']}.csv"
    pd.DataFrame({"date": sl_dates, "ret": b["rets"], "equity": np.cumprod(1 + b["rets"])}).to_csv(out, index=False)
    print(f"  daily series → {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Cross-sectional perp momentum kill-test")
    ap.add_argument("--split", choices=["train", "holdout", "both"], default="train")
    ap.add_argument("--unseal-holdout", action="store_true")
    ap.add_argument("--sweep-lookbacks", type=str, default="7,14,30,60,90")
    ap.add_argument("--quantile", type=float, default=0.25)
    ap.add_argument("--min-names", type=int, default=8)
    ap.add_argument("--profit-target", type=float, default=0.10)
    ap.add_argument("--max-dd", type=float, default=0.06)
    ap.add_argument("--eval-horizon", type=int, default=60)
    ap.add_argument("--fee-bps", type=float, default=4.5)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    args = ap.parse_args()

    if args.split in ("holdout", "both") and not args.unseal_holdout:
        print("REFUSED: --split holdout/both needs --unseal-holdout (pre-registered single-shot).")
        return
    run(args)


if __name__ == "__main__":
    main()
