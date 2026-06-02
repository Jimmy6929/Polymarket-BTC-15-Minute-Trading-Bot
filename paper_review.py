#!/usr/bin/env python3
"""Paper-trade Reviewer for the SLOW outer self-improvement loop.

Reads `paper_trades.json` (REAL forward paper-trading outcomes, resolved via actual
Polymarket settlement — see paper_resolution.py) and finds the single most promising
SKIP that would improve the strategy's overall EV, framed as one hypothesis for a human.

Why this is different from the backtest Reviewer:
  The backtest loop can only act on signal features, because the *frozen* backtester
  never plumbs spread / hour / strike into allowlisted code (this killed EXP-007/008).
  But the LIVE bot's decision path (bot.py:_make_trading_decision) genuinely SEES the
  real order book (self._last_bid_ask -> half_spread), the clock, the entry price and
  the signals. So in the paper loop those features ARE implementable and testable.

Methodology (this matters):
  When the whole book is EV-negative, almost every large slice has a negative mean — so
  "this cohort loses" is nearly useless. The right question for a SKIP is: does removing
  this cohort make the REST of the book better? We therefore rank candidate skips by the
  overall-EV LIFT from skipping (mean of the kept trades minus mean of all trades), and we
  only keep a candidate if it is (a) significantly WORSE than its complement (bootstrap CI
  on the difference excludes 0), (b) leaves enough trades (retention floor), and (c) has
  enough trades to bucket. We also test one-sided THRESHOLD gates (e.g. half_spread >= θ)
  because that is the shape an implementable skip actually takes.

Cadence: run after accumulating WEEKS of paper trades. Paper data is scarce, so with
small n every candidate is LOW POWER — the job is to surface ONE candidate honestly, not
to certify an edge. The only real confirmation is another paper-trading round of the change.

Usage:
    python paper_review.py                       # reads paper_trades.json
    python paper_review.py --input some.json --out report.md --min-cohort-n 25
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

random.seed(42)

RETENTION_FLOOR = 0.60   # never recommend skipping so much that <60% of trades survive


def load_resolved(path: str) -> List[dict]:
    """Load only RESOLVED paper trades (WIN/LOSS with a numeric pnl). PENDING excluded."""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        data = json.load(f)
    return [t for t in data
            if t.get("outcome") in ("WIN", "LOSS") and isinstance(t.get("pnl"), (int, float))]


def _mean(xs: List[float]) -> float:
    return statistics.mean(xs) if xs else 0.0


def bootstrap_ci(xs: List[float], resamples: int = 1000) -> List[float]:
    n = len(xs)
    if n == 0:
        return [0.0, 0.0]
    if n == 1:
        return [round(xs[0], 5), round(xs[0], 5)]
    boots = sorted(sum(random.choice(xs) for _ in range(n)) / n for _ in range(resamples))
    return [round(boots[int(0.025 * resamples)], 5), round(boots[int(0.975 * resamples)], 5)]


def bootstrap_diff_ci(cohort: List[float], rest: List[float], resamples: int = 1000) -> List[float]:
    """95% CI on (mean_rest - mean_cohort). Lower bound > 0 => cohort reliably WORSE
    than the rest of the book (the differential that justifies skipping it)."""
    nc, nr = len(cohort), len(rest)
    if nc == 0 or nr == 0:
        return [0.0, 0.0]
    diffs = sorted(
        (sum(random.choice(rest) for _ in range(nr)) / nr) -
        (sum(random.choice(cohort) for _ in range(nc)) / nc)
        for _ in range(resamples)
    )
    return [round(diffs[int(0.025 * resamples)], 5), round(diffs[int(0.975 * resamples)], 5)]


def overall_stats(rows: List[dict]) -> dict:
    pnls = [float(r["pnl"]) for r in rows]
    n = len(pnls)
    wins = sum(1 for r in rows if r.get("outcome") == "WIN")
    return {"n": n, "mean_pnl": round(_mean(pnls), 5), "ci_95": bootstrap_ci(pnls),
            "total_pnl": round(sum(pnls), 4), "win_rate": round(wins / n, 4) if n else 0.0}


# ----- candidate SKIP predicates (all live-observable in bot.py:_make_trading_decision) -----

def _hour(t: dict) -> Optional[int]:
    ts = t.get("timestamp")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).hour
    except (ValueError, TypeError):
        return None


def candidate_skips() -> List[Tuple[str, str, Callable[[dict], bool]]]:
    """(label, feature_meaning, predicate) — predicate(t) True == trade is IN the skip set."""
    c: List[Tuple[str, str, Callable[[dict], bool]]] = []
    book = "the spread you crossed at entry (self._last_bid_ask)"
    for th in (0.005, 0.0075, 0.01, 0.015, 0.02):
        c.append((f"half_spread >= {th:g}", book,
                  lambda t, th=th: isinstance(t.get("half_spread"), (int, float)) and t["half_spread"] >= th))
    for th in (50, 60, 70):
        c.append((f"signal_score < {th}", "fused signal score",
                  lambda t, th=th: isinstance(t.get("signal_score"), (int, float)) and t["signal_score"] < th))
    for th in (0.6, 0.7, 0.8):
        c.append((f"signal_confidence < {th:g}", "fused signal confidence",
                  lambda t, th=th: isinstance(t.get("signal_confidence"), (int, float)) and t["signal_confidence"] < th))
    c.append(("direction == SHORT", "trade side", lambda t: t.get("direction") == "SHORT"))
    c.append(("direction == LONG", "trade side", lambda t: t.get("direction") == "LONG"))
    for lo, hi in ((0, 6), (6, 12), (12, 18), (18, 24)):
        c.append((f"hour_utc in [{lo:02d},{hi:02d})", "UTC hour at decision time",
                  lambda t, lo=lo, hi=hi: (_hour(t) is not None) and (lo <= _hour(t) < hi)))
    return c


def find_skip_candidates(rows: List[dict], min_cohort_n: int) -> List[dict]:
    """Rank candidate skips by overall-EV LIFT, keeping only those that are significantly
    worse than their complement and respect the retention floor."""
    all_pnl = [float(r["pnl"]) for r in rows]
    n_all = len(all_pnl)
    mean_all = _mean(all_pnl)
    out = []
    for label, desc, pred in candidate_skips():
        cohort = [float(r["pnl"]) for r in rows if pred(r)]
        rest = [float(r["pnl"]) for r in rows if not pred(r)]
        n_c = len(cohort)
        if n_c < min_cohort_n or len(rest) < min_cohort_n:
            continue
        retention = len(rest) / n_all
        if retention < RETENTION_FLOOR:
            continue
        lift = _mean(rest) - mean_all                 # improvement to overall mean from skipping
        diff_ci = bootstrap_diff_ci(cohort, rest)     # mean_rest - mean_cohort
        significant = diff_ci[0] > 0                   # cohort reliably worse than the rest
        if lift > 0 and significant:
            out.append({
                "label": label, "desc": desc, "n_skip": n_c,
                "mean_skip": round(_mean(cohort), 5), "mean_kept": round(_mean(rest), 5),
                "lift": round(lift, 5), "retention": round(retention, 3), "diff_ci": diff_ci,
            })
    out.sort(key=lambda d: d["lift"], reverse=True)
    return out


def build_report(rows: List[dict], min_cohort_n: int) -> str:
    ov = overall_stats(rows)
    n = ov["n"]
    L = ["# Paper-Trade Review (slow outer loop)\n",
         f"Generated: {datetime.now(timezone.utc).isoformat()}",
         f"Resolved paper trades analysed: **{n}**\n"]

    if n < 100:
        L.append("> **LOW POWER WARNING.** Fewer than 100 resolved trades. Any candidate below is "
                 "tentative; treat it as a hypothesis to test with more paper trading, not a finding.\n")

    L.append("## Overall (the strategy as-is, in real paper fills)")
    L.append(f"- mean PnL/trade: **${ov['mean_pnl']:+.5f}**  (95% CI {ov['ci_95']})")
    L.append(f"- total PnL: ${ov['total_pnl']:+.4f} | win rate: {ov['win_rate']:.1%} | n={n}")
    verdict = ("reliably POSITIVE" if ov["ci_95"][0] > 0 else
               "reliably NEGATIVE" if ov["ci_95"][1] < 0 else "indistinguishable from zero")
    L.append(f"- verdict: **{verdict}**\n")

    cands = find_skip_candidates(rows, min_cohort_n) if n >= 2 * min_cohort_n else []
    L.append(f"## Skip candidates — ranked by overall-EV LIFT (n ≥ {min_cohort_n}/side, "
             f"retention ≥ {RETENTION_FLOOR:.0%}, cohort significantly worse than the rest)")
    if not cands:
        L.append("_None qualify._ No skip both improves overall EV AND is significantly worse than "
                 "the rest of the book at the current sample size. Do NOT invent a change — keep "
                 "paper-trading. (When the whole book is negative, slicing it doesn't help; you need "
                 "a cohort that is *differentially* bad.)\n")
    else:
        L.append("| rank | skip gate | n_skip | mean(skip) | mean(kept) | LIFT | retention | Δ-CI (rest−skip) |")
        L.append("|---|---|---|---|---|---|---|---|")
        for i, c in enumerate(cands[:8], 1):
            L.append(f"| {i} | `{c['label']}` | {c['n_skip']} | ${c['mean_skip']:+.5f} | "
                     f"${c['mean_kept']:+.5f} | **${c['lift']:+.5f}** | {c['retention']:.0%} | {c['diff_ci']} |")
        L.append("")
        L.append("> **MULTIPLE-TESTING CAVEAT.** This reviewer tests ~17 candidate gates and the "
                 "Δ-CI above is NOT corrected for that — with scarce data, even the top candidate can "
                 "be a false positive (a random feature that happens to align with the losers). This is "
                 "exactly why a MECHANISM is mandatory and why the ONLY real confirmation is the next "
                 "paper-trading round (true out-of-sample). Never ship a gate you cannot explain.\n")

    L.append("## Recommended hypothesis (ONE — for the next paper-trading round)")
    if cands:
        top = cands[0]
        L.append(f"**Skip gate:** `{top['label']}`  ({top['desc']})")
        L.append(f"**Effect (in-sample, paper):** removes {top['n_skip']} trades "
                 f"(mean ${top['mean_skip']:+.5f}); kept book mean rises to ${top['mean_kept']:+.5f} "
                 f"(**lift ${top['lift']:+.5f}**), retaining {top['retention']:.0%} of trades. "
                 f"Cohort-vs-rest difference CI {top['diff_ci']} (excludes 0).")
        L.append(f"**Hypothesis (falsifiable):** Adding `if {top['label']}: skip` to "
                 f"`bot.py:_make_trading_decision` raises overall paper-trade mean PnL on the NEXT round.")
        L.append("**Implementable?** YES — live in `bot.py` (real book / clock / signals). Code the gate, "
                 "then validate by another paper-trading round of equal length.")
        L.append("**Mechanism (FILL IN before acting):** state WHY this cohort loses — a mechanism, not a "
                 "fitted bin. On scarce data, a mechanism-free gate is almost certainly overfitting.")
    else:
        L.append("_No candidate qualifies. Keep paper-trading; do not force a change._")
    L.append("")
    L.append("> SLOW loop: ONE change per round, then weeks of paper trading to test it. "
             "Don't batch changes — attribution becomes impossible.")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Paper-trade Reviewer for the slow outer loop")
    ap.add_argument("--input", default="paper_trades.json")
    ap.add_argument("--out", default=None,
                    help="report path (default: .claude/state/paper_review/paper_hypothesis_<ts>.md)")
    ap.add_argument("--min-cohort-n", type=int, default=25, help="min trades per side for a candidate")
    args = ap.parse_args()

    rows = load_resolved(args.input)
    if not rows:
        print(f"PAPER-REVIEW: no resolved paper trades in {args.input} "
              f"(missing/empty, or all PENDING). Run `python bot.py` to paper-trade first.")
        return

    report = build_report(rows, args.min_cohort_n)
    out = args.out
    if out is None:
        os.makedirs(".claude/state/paper_review", exist_ok=True)
        out = os.path.join(".claude/state/paper_review",
                           f"paper_hypothesis_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md")
    with open(out, "w") as f:
        f.write(report)

    cands = find_skip_candidates(rows, args.min_cohort_n) if len(rows) >= 2 * args.min_cohort_n else []
    if cands:
        t = cands[0]
        print(f"PAPER-REVIEW n={len(rows)} top_skip='{t['label']}' lift=${t['lift']:+.5f} "
              f"retention={t['retention']:.0%} -> {out}")
    else:
        print(f"PAPER-REVIEW n={len(rows)} no_qualifying_skip (keep paper-trading) -> {out}")


if __name__ == "__main__":
    main()
