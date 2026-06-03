"""Minute-scale lead-lag KILL-TEST for Polymarket BTC 15-min markets.

Phase 1 of the money-pivot (see ~/.claude/plans + repo notes.txt). The original
bot traded the *lagging* Polymarket book price. This script tests the opposite,
and the only home-latency-reachable, edge: does BTC SPOT lead the Polymarket book
by enough, for long enough (>= 1 minute), and reliably enough that a 1-2s home bot
could capture it net of the ~1.8% taker fee + spread?

This is a FALSIFICATION tool, not a strategy optimiser. Its primary deliverable is
the DISLOCATION DIAGNOSTIC (printed before any EV): when spot and the book DISAGREE
on direction, is spot actually right often enough to trade? That metric is
deliberately FILL-INDEPENDENT — because sim->live edge decay is 50-80% and fills
evaporate first, a home bot facing the same stale book may not get filled at `p`,
but "is spot right?" is a property of the data, not of our execution.

Design (frozen-code-safe): this is a standalone SIBLING of real_backtester.py. It
does NOT edit the guard-frozen backtester; it imports and reuses its data loaders,
fill/fee model, TradeRecord, and the splits, plus stats/deflated_sharpe. New files
in build-steps/ are allowed by guard.py; output goes to data/runs/ (writable).

Strategy logic
--------------
- strike K     = OPEN of the Coinbase candle at slug_ts (window open). Validated
                 against the stored btc_price_to_beat where present (median err
                 0.56 bps; CLOSE would be wrong by up to 73 bps and fake an edge).
- decision     = the latest book tick inside minute `m` of the window.
- spot S       = CLOSE of the candle at (tick_t//60 - 1) — PROVEN complete strictly
                 before the book tick. Never tick_t//60 (in-progress => leakage).
- dist_bps     = (S - K)/K * 1e4 ; spot_dir = sign(dist_bps).
- book_dir     = sign(p - 0.5).
- TRADE only when book_dir != spot_dir (a genuine dislocation), |dist_bps| clears
  the threshold, and the cheap side has room after cost. Buy the side spot favors.

Look-ahead audit, strike validation, full (minute x threshold) sweep grids, and a
Deflated-Sharpe penalty with honest n_configs are all printed. Phase 1 is train-only;
the holdout stays sealed.
"""
import argparse
import math
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --- sibling import of the frozen backtester (mirrors real_backtester.py:30) ---
sys.path.insert(0, str(Path(__file__).resolve().parent))
import real_backtester as rb  # noqa: E402  (loaders, fill/fee, TradeRecord, splits)
from stats.deflated_sharpe import deflated_sharpe  # noqa: E402

# Magnitude buckets for the dislocation histogram (|dist_bps|).
DIST_BUCKETS = [(0.0, 1.0), (1.0, 2.0), (2.0, 5.0), (5.0, 10.0), (10.0, 20.0), (20.0, float("inf"))]


# --------------------------------------------------------------------------- #
# Per-market observation: everything needed for BOTH the diagnostic and trading.
# Computed look-ahead-safe; resolution (yes_won) is used ONLY for scoring/PnL.
# --------------------------------------------------------------------------- #
def observe(
    m: Dict[str, Any],
    decision_minute: int,
    btc_by_minute: Dict[int, Dict[str, float]],
    strike_tol_bps: float,
) -> Optional[Dict[str, Any]]:
    """Return an observation dict for market `m` at `decision_minute`, or None if
    the market is not eligible (missing tick/strike/spot, or flat spot)."""
    slug_ts = m["slug_ts"]
    ticks = m.get("yes_price_history") or []

    ws = slug_ts + decision_minute * 60
    entry_tick = rb.find_entry_tick(ticks, ws, ws + 60)
    if entry_tick is None:
        return None
    p = float(entry_tick["p"])
    tick_t = int(entry_tick["t"])

    # --- strike: OPEN of the slug_ts candle (window open) ---
    strike_candle = btc_by_minute.get(slug_ts // 60)
    if strike_candle is None:
        return None
    K = strike_candle["open"]
    if K <= 0:
        return None

    # --- strike validation against stored price-to-beat (where available) ---
    stored = m.get("btc_price_to_beat")
    if stored:
        err_bps = abs(K - float(stored)) / float(stored) * 1e4
        strike_status = "validated" if err_bps <= strike_tol_bps else "failed"
    else:
        err_bps = None
        strike_status = "unverified"

    # --- spot: CLOSE of a candle PROVEN complete before the book tick ---
    # candle for minute M covers [M*60,(M+1)*60); its close is known at (M+1)*60.
    # complete strictly before tick_t  <=>  (M+1)*60 <= tick_t  <=>  M <= tick_t//60 - 1.
    m_spot = tick_t // 60 - 1
    sc = btc_by_minute.get(m_spot) or btc_by_minute.get(m_spot - 1) or btc_by_minute.get(m_spot - 2)
    if sc is None:
        return None
    S = sc["close"]  # close of an earlier, fully-closed candle => never leaks

    dist_bps = (S - K) / K * 1e4
    spot_dir = 1 if dist_bps > 0 else (-1 if dist_bps < 0 else 0)
    if spot_dir == 0:
        return None  # flat spot: no directional signal
    book_dir = 1 if p > 0.5 else (-1 if p < 0.5 else 0)

    yes_won = bool(m["yes_won"])
    spot_correct = (spot_dir == 1) == yes_won

    # side we would BUY (the side spot favors) and the price we'd pay for it
    direction = "LONG" if spot_dir > 0 else "SHORT"
    p_side = p if direction == "LONG" else (1.0 - p)

    # round-trip cost in price points: half-spread (book width) + fee rate on $1
    half_spread = _book_half_spread(m)
    fee_rate = rb.compute_fee(m.get("fee_schedule"), p_side, 1.0)
    cost = half_spread + fee_rate

    return {
        "market": m,
        "slug": m["slug"],
        "slug_ts": slug_ts,
        "entry_ts": tick_t,
        "p": p,
        "K": K,
        "S": S,
        "dist_bps": dist_bps,
        "spot_dir": spot_dir,
        "book_dir": book_dir,
        "direction": direction,
        "p_side": p_side,
        "cost": cost,
        "yes_won": yes_won,
        "spot_correct": spot_correct,
        "disagree": (book_dir != 0 and book_dir != spot_dir),
        "strike_status": strike_status,
        "strike_err_bps": err_bps,
        "fee_regime": rb.fee_regime_for(m.get("fee_schedule")),
    }


def _book_half_spread(m: Dict[str, Any]) -> float:
    """Mirror the width logic inside rb.realistic_fill_price (book snapshot)."""
    bb, ba = m.get("best_bid"), m.get("best_ask")
    if bb and ba and ba > bb:
        return (ba - bb) / 2.0
    sp = m.get("spread")
    if sp and sp > 0:
        return sp / 2.0
    return rb.DEFAULT_HALF_SPREAD


def eligible_universe(
    markets: List[Dict[str, Any]],
    decision_minute: int,
    btc_by_minute: Dict[int, Dict[str, float]],
    strike_tol_bps: float,
    unvalidated: str,
) -> List[Dict[str, Any]]:
    """Eligible observations for the diagnostic. Drops 'failed' strikes always
    (derived disagrees with stored => unreliable); keeps 'unverified' only when
    --unvalidated include."""
    obs = []
    for m in markets:
        o = observe(m, decision_minute, btc_by_minute, strike_tol_bps)
        if o is None:
            continue
        if o["strike_status"] == "failed":
            continue
        if o["strike_status"] == "unverified" and unvalidated == "exclude":
            continue
        obs.append(o)
    return obs


# --------------------------------------------------------------------------- #
# THE KEY DELIVERABLE — dislocation diagnostic (fill-independent).
# --------------------------------------------------------------------------- #
def print_dislocation_diagnostic(obs: List[Dict[str, Any]], decision_minute: int) -> None:
    n = len(obs)
    print(f"\n[minute {decision_minute}] DISLOCATION DIAGNOSTIC — eligible universe n={n}")
    if n == 0:
        print("  (no eligible markets)")
        return

    agree = sum(1 for o in obs if o["book_dir"] == o["spot_dir"])
    book_flat = sum(1 for o in obs if o["book_dir"] == 0)
    disagree = [o for o in obs if o["disagree"]]
    nd = len(disagree)
    print(f"  agreement : agree {agree} ({agree/n*100:.1f}%) | "
          f"disagree {nd} ({nd/n*100:.1f}%) | book_flat {book_flat}")

    # Histogram of disagreements by |dist_bps|, with spot-side WIN RATE per bucket.
    # This is the gate's true decision variable: when spot contradicts the book,
    # is spot actually right? (Independent of whether we'd get filled.)
    print("  disagreement |dist_bps| histogram (does the SPOT side actually win?):")
    print(f"    {'bucket':>12} {'n':>6} {'spot_wins':>10} {'avg_p_side':>11} {'avg_cost':>9}")
    for lo, hi in DIST_BUCKETS:
        bucket = [o for o in disagree if lo <= abs(o["dist_bps"]) < hi]
        if not bucket:
            continue
        wins = sum(1 for o in bucket if o["spot_correct"])
        avg_p = sum(o["p_side"] for o in bucket) / len(bucket)
        avg_c = sum(o["cost"] for o in bucket) / len(bucket)
        label = f"[{lo:g},{hi:g})" if hi != float("inf") else f"[{lo:g},inf)"
        print(f"    {label:>12} {len(bucket):>6} {wins/len(bucket)*100:>9.1f}% "
              f"{avg_p:>11.4f} {avg_c:>9.4f}")

    # Tradability summary on disagreements: spot win-rate vs break-even (price+cost).
    _tradability_line("  all disagreements ", disagree)
    _tradability_line("  |dist|>=2bp       ", [o for o in disagree if abs(o["dist_bps"]) >= 2.0])
    _tradability_line("  |dist|>=5bp       ", [o for o in disagree if abs(o["dist_bps"]) >= 5.0])

    # Validated vs unverified split — an edge that lives only in unverifiable
    # markets is a leakage tell, not an edge.
    val = [o for o in disagree if o["strike_status"] == "validated"]
    unv = [o for o in disagree if o["strike_status"] == "unverified"]
    if val and unv:
        print("  strike-source split (disagreements):")
        _tradability_line("    validated       ", val)
        _tradability_line("    unverified      ", unv)


def _tradability_line(label: str, bucket: List[Dict[str, Any]]) -> None:
    if not bucket:
        print(f"  {label}: n=0")
        return
    n = len(bucket)
    spot_win = sum(1 for o in bucket if o["spot_correct"]) / n
    avg_p = sum(o["p_side"] for o in bucket) / n
    avg_c = sum(o["cost"] for o in bucket) / n
    breakeven = avg_p + avg_c           # win prob needed to break even buying at p_side
    edge = spot_win - breakeven         # >0 => tradable (before fill realism)
    print(f"  {label}: n={n:>5}  spot_win={spot_win*100:5.1f}%  "
          f"break-even={breakeven*100:5.1f}%  edge={edge*100:+5.1f}pp")


# --------------------------------------------------------------------------- #
# Trading: turn disagreement observations into trades (reuse frozen fill/PnL).
# --------------------------------------------------------------------------- #
def make_trades(obs: List[Dict[str, Any]], threshold_bps: float) -> List[rb.TradeRecord]:
    trades: List[rb.TradeRecord] = []
    for o in obs:
        if not o["disagree"]:
            continue
        if abs(o["dist_bps"]) < threshold_bps:
            continue
        if o["p_side"] >= 1.0 - o["cost"]:
            continue  # no room to profit after cost

        m = o["market"]
        direction = o["direction"]
        p = o["p"]
        fill_p = rb.realistic_fill_price(direction, p, m)
        P = fill_p
        yes_won = o["yes_won"]
        size = 1.0
        if direction == "LONG":
            payout = size * (1.0 - P) / P if yes_won else -size
        else:
            payout = size * P / (1.0 - P) if not yes_won else -size
        fee = rb.compute_fee(m.get("fee_schedule"), P, size)
        pnl = payout - fee

        trades.append(rb.TradeRecord(
            slug=o["slug"],
            market_id=str(m.get("market_id", "")),
            entry_ts=o["entry_ts"],
            direction=direction,
            entry_price=p,
            fill_price=fill_p,
            entry_btc_spot=o["S"],
            final_btc_price=m.get("final_btc_price") or 0.0,
            btc_price_to_beat=m.get("btc_price_to_beat") or 0.0,
            yes_won=yes_won,
            fee_rate=fee / size if size else 0.0,
            payout=payout,
            fee=fee,
            pnl=pnl,
            outcome="WIN" if pnl > 0 else "LOSS",
            signal_score=o["dist_bps"],   # carry the signal magnitude into the CSV
            signal_confidence=o["p_side"],
            signal_count=1,
            fee_regime=o["fee_regime"],
        ))
    return trades


# --------------------------------------------------------------------------- #
# Evaluation — mirrors rb._print_summary stats (Wilson, bootstrap seed 42,
# fee-regime, monthly) and adds a Deflated-Sharpe line with honest n_configs.
# --------------------------------------------------------------------------- #
def cell_stats(trades: List[rb.TradeRecord]) -> Dict[str, float]:
    """Lightweight stats for one sweep cell (no printing)."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "win": float("nan"), "avg_pnl": float("nan"),
                "boot_lo": float("nan"), "total": 0.0}
    wins = sum(1 for t in trades if t.outcome == "WIN")
    pnls = [t.pnl for t in trades]
    total = sum(pnls)
    random.seed(42)
    boots = sorted(sum(random.choice(pnls) for _ in range(n)) / n for _ in range(2000))
    return {"n": n, "win": wins / n * 100, "avg_pnl": total / n,
            "boot_lo": boots[50], "total": total}  # 2.5th pct of 2000


def print_summary(trades: List[rb.TradeRecord], split: str, n_configs: int,
                  label: str, out_path: Optional[Path] = None) -> None:
    print("\n" + "=" * 80)
    print(f"LEAD-LAG STRATEGY RESULTS — {label}  [split={split}]")
    print("=" * 80)
    n = len(trades)
    print(f"Trades executed : {n}")
    if n == 0:
        print("No trades — nothing to summarise.")
        return

    wins = sum(1 for t in trades if t.outcome == "WIN")
    n_long = sum(1 for t in trades if t.direction == "LONG")
    p_hat = wins / n
    z = 1.96
    denom = 1 + z**2 / n
    center = (p_hat + z**2 / (2 * n)) / denom
    margin = z * math.sqrt(p_hat * (1 - p_hat) / n + z**2 / (4 * n**2)) / denom
    wlo, whi = max(0.0, center - margin), min(1.0, center + margin)

    total_pnl = sum(t.pnl for t in trades)
    avg_pnl = total_pnl / n
    random.seed(42)
    pnls = [t.pnl for t in trades]
    boots = sorted(sum(random.choice(pnls) for _ in range(n)) / n for _ in range(10000))
    boot_lo, boot_hi = boots[250], boots[9750]

    print(f"Direction split : LONG {n_long} ({n_long/n*100:.0f}%) | SHORT {n-n_long}")
    print(f"Win rate        : {p_hat*100:.1f}%  (Wilson 95% CI {wlo*100:.1f}–{whi*100:.1f}%)")
    print(f"Total P&L       : ${total_pnl:+.4f} on ${n:.0f} deployed   (ROI {total_pnl/n*100:+.2f}%)")
    print(f"Avg P&L / trade : ${avg_pnl:+.4f}  (bootstrap 95% CI ${boot_lo:+.4f} – ${boot_hi:+.4f})")

    dsr = deflated_sharpe(pnls, n_configs)
    print(f"Deflated Sharpe : DSR={dsr['dsr']:.3f}  (SR={dsr['sr']:+.4f}, "
          f"E[maxSR|N={n_configs}]={dsr['e_max_sr']:.4f}, T={dsr['T']}, valid={dsr['valid']})")

    # per-fee-regime
    regimes: Dict[str, List[rb.TradeRecord]] = {}
    for t in trades:
        regimes.setdefault(t.fee_regime, []).append(t)
    if len(regimes) > 1:
        print("Per-fee-regime  :")
        for regime in sorted(regimes):
            ts = regimes[regime]
            wr = sum(1 for t in ts if t.outcome == "WIN") / len(ts) * 100
            print(f"  {regime:<18} n={len(ts):>5} win={wr:5.1f}% "
                  f"avg_pnl={sum(t.pnl for t in ts)/len(ts):+.4f} total={sum(t.pnl for t in ts):+.4f}")

    # monthly walk-forward
    from datetime import datetime, timezone
    monthly: Dict[str, List[rb.TradeRecord]] = {}
    for t in trades:
        mk = datetime.fromtimestamp(t.entry_ts, tz=timezone.utc).strftime("%Y-%m")
        monthly.setdefault(mk, []).append(t)
    if len(monthly) > 1:
        print("Monthly         :")
        for month in sorted(monthly):
            ts = monthly[month]
            wr = sum(1 for t in ts if t.outcome == "WIN") / len(ts) * 100
            print(f"  {month}  n={len(ts):>5} win={wr:5.1f}% "
                  f"avg_pnl={sum(t.pnl for t in ts)/len(ts):+.4f} total={sum(t.pnl for t in ts):+.4f}")

    if out_path is not None:
        import csv
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["slug", "entry_ts", "direction", "entry_price", "fill_price",
                        "entry_btc_spot", "btc_price_to_beat", "yes_won", "dist_bps",
                        "p_side", "fee", "payout", "pnl", "outcome", "fee_regime"])
            for t in trades:
                w.writerow([t.slug, t.entry_ts, t.direction, t.entry_price, t.fill_price,
                            t.entry_btc_spot, t.btc_price_to_beat, t.yes_won, t.signal_score,
                            t.signal_confidence, t.fee, t.payout, t.pnl, t.outcome, t.fee_regime])
        print(f"Trade log       : {out_path}")
    print("=" * 80)


def print_lookahead_audit() -> None:
    print("\nLOOK-AHEAD AUDIT (every input proven available at decision time):")
    print("  K  (strike)     <- OPEN of candle at slug_ts//60; complete by slug_ts+60 <= tick_t. OK")
    print("  S  (spot)       <- CLOSE of candle at tick_t//60 - 1; complete by tick_t. OK  (the -1 is the guard)")
    print("  p, tick_t       <- the book tick itself. OK")
    print("  half_spread/fee <- static market metadata snapshot. OK")
    print("  yes_won         <- resolution; used ONLY for scoring/PnL, never in the signal. OK")
    print("  NOTE: find_entry_tick returns the LATEST tick in the minute — upper-bound-favorable")
    print("        for a real 1-2s home bot; treat strategy EV as an optimistic ceiling.")


# --------------------------------------------------------------------------- #
def run(split: str, decision_minutes: List[int], thresholds: List[float],
        strike_tol_bps: float, unvalidated: str, out_path: Optional[Path]) -> None:
    if split == "holdout":
        print("REFUSING: Phase 1 is train-only; the holdout stays sealed. "
              "Route any holdout shot through the pre-registered single-shot path.")
        return

    print(f"[leadlag] loading data… (split={split})")
    btc_by_minute = rb.load_coinbase_btc()
    all_markets = rb.load_markets()
    allowed = rb.load_split_slugs(split)
    markets = all_markets if allowed is None else [m for m in all_markets if m["slug"] in allowed]
    print(f"[leadlag]   markets: {len(markets)} (split={split}); "
          f"coinbase candles: {len(btc_by_minute)}")
    print_lookahead_audit()

    # Strike-validation diagnostic (population-level), on minute 13 universe.
    _print_strike_validation(markets, btc_by_minute, strike_tol_bps)

    # Diagnostic + per-cell stats over the (minute x threshold) grid.
    n_configs = max(1, len(decision_minutes) * len(thresholds))
    grid: Dict[Tuple[int, float], Dict[str, float]] = {}
    best: Optional[Tuple[Tuple[int, float], List[rb.TradeRecord]]] = None

    for dmin in decision_minutes:
        obs = eligible_universe(markets, dmin, btc_by_minute, strike_tol_bps, unvalidated)
        print_dislocation_diagnostic(obs, dmin)
        for thr in thresholds:
            trades = make_trades(obs, thr)
            st = cell_stats(trades)
            grid[(dmin, thr)] = st
            if st["n"] > 0 and (best is None or st["avg_pnl"] > grid[best[0]]["avg_pnl"]):
                best = ((dmin, thr), trades)

    # Sweep grid (full — never the argmax silently).
    print("\nSWEEP GRID  (minute x threshold_bps)  [report the WHOLE grid]")
    print(f"  {'minute':>6} {'thr_bps':>8} {'n':>6} {'win%':>7} {'avg_pnl':>10} {'EV_lo(2.5%)':>12}")
    for dmin in decision_minutes:
        for thr in thresholds:
            st = grid[(dmin, thr)]
            if st["n"] == 0:
                print(f"  {dmin:>6} {thr:>8g} {0:>6} {'—':>7} {'—':>10} {'—':>12}")
            else:
                print(f"  {dmin:>6} {thr:>8g} {st['n']:>6} {st['win']:>6.1f}% "
                      f"{st['avg_pnl']:>+10.4f} {st['boot_lo']:>+12.4f}")

    # Detailed summary + DSR for the best cell (deflated by the full grid size).
    if best is not None:
        (bmin, bthr), btrades = best
        print(f"\nBest cell by avg_pnl: minute={bmin}, threshold_bps={bthr} "
              f"(DSR deflated by n_configs={n_configs} = full grid)")
        print_summary(btrades, split, n_configs,
                      label=f"minute={bmin} thr={bthr}bps", out_path=out_path)
    else:
        print("\nNo cell produced any trades — no exploitable dislocation under any "
              "(minute, threshold) in the grid.")


def _print_strike_validation(markets, btc_by_minute, strike_tol_bps) -> None:
    errs = []
    for m in markets:
        sc = btc_by_minute.get(m["slug_ts"] // 60)
        stored = m.get("btc_price_to_beat")
        if sc and stored:
            errs.append(abs(sc["open"] - float(stored)) / float(stored) * 1e4)
    print("\nSTRIKE VALIDATION (derived OPEN@slug_ts vs stored btc_price_to_beat):")
    if not errs:
        print("  no markets with a stored price-to-beat in this split.")
        return
    errs.sort()
    n = len(errs)
    med = errs[n // 2]
    p99 = errs[min(n - 1, int(n * 0.99))]
    within1 = sum(1 for e in errs if e <= 1.0) / n * 100
    within5 = sum(1 for e in errs if e <= strike_tol_bps) / n * 100
    print(f"  n={n}  median={med:.2f}bp  p99={p99:.2f}bp  max={errs[-1]:.2f}bp  "
          f"within1bp={within1:.1f}%  within{strike_tol_bps:g}bp={within5:.1f}%")
    print(f"  (markets failing the {strike_tol_bps:g}bp tolerance are dropped from the tradable universe)")


def _parse_int_list(s: str) -> List[int]:
    return [int(x) for x in s.split(",") if x.strip()]


def _parse_float_list(s: str) -> List[float]:
    return [float(x) for x in s.split(",") if x.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description="Minute-scale lead-lag kill-test")
    ap.add_argument("--split", choices=["train", "holdout", "both"], default="train")
    ap.add_argument("--decision-minute", type=int, default=13)
    ap.add_argument("--sweep-minutes", type=str, default=None,
                    help='e.g. "10,11,12,13,14" (overrides --decision-minute)')
    ap.add_argument("--threshold-bps", type=float, default=0.0)
    ap.add_argument("--sweep-thresholds", type=str, default=None,
                    help='e.g. "0,1,2,5,10,20" (overrides --threshold-bps)')
    ap.add_argument("--strike-tol-bps", type=float, default=5.0)
    ap.add_argument("--unvalidated", choices=["include", "exclude"], default="include")
    ap.add_argument("--out", type=str, default="build-steps/data/runs/leadlag_trades.csv")
    args = ap.parse_args()

    minutes = _parse_int_list(args.sweep_minutes) if args.sweep_minutes else [args.decision_minute]
    thresholds = _parse_float_list(args.sweep_thresholds) if args.sweep_thresholds else [args.threshold_bps]
    out_path = Path(args.out) if args.out else None

    run(args.split, minutes, thresholds, args.strike_tol_bps, args.unvalidated, out_path)


if __name__ == "__main__":
    main()
