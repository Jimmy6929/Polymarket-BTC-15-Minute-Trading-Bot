#!/usr/bin/env python3
"""Evaluate the tight-spread favorite "pocket" directly from the paper-trading
decision log — no special harness needed.

Context: a prior memory note claimed the paper trader was "blind" to the spread
(pinned at a 0.005 fallback), making the tight-spread pocket untestable live.
That was wrong. `bot.py:_capture_decision` records the REAL half_spread from the
live Polymarket book (it varies 0.0005–0.025 in decisions.jsonl; the modal 0.005
is a genuine 1-cent book on liquid favorites, and the actual fallback is 0.01).
So the pocket is directly testable from the decision log. This script does that.

The candidate pocket (see memory tight-spread-favorite-pocket): a favorite with
mid in [0.80, 0.97] and half_spread < 0.005. We report it net of the real fee
curve + spread-crossed fill already baked into `pnl_if_taken`.

Run: ./venv/bin/python analysis/pocket_check.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from decision_log import load_decisions


def _stats(pnls):
    n = len(pnls)
    if n == 0:
        return dict(n=0)
    mean = sum(pnls) / n
    wins = sum(1 for p in pnls if p > 0)
    sd = math.sqrt(sum((p - mean) ** 2 for p in pnls) / n) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n else 0.0
    # Normal-approx 95% CI on the mean. With tiny n and ~0 losers this is only
    # a sanity bound, not a real test — the point of the script is to show that.
    lo, hi = mean - 1.96 * se, mean + 1.96 * se
    return dict(n=n, mean=mean, win_rate=wins / n, ci=(lo, hi), losers=n - wins)


def _line(label, s):
    if s.get("n", 0) == 0:
        print(f"  {label:<34} n=0")
        return
    lo, hi = s["ci"]
    flag = "  <-- CI includes 0" if lo <= 0 <= hi else ""
    print(
        f"  {label:<34} n={s['n']:<4d} mean=${s['mean']:+.4f}/win  "
        f"win={s['win_rate']:.0%}  losers={s['losers']}  CI=[${lo:+.4f},${hi:+.4f}]{flag}"
    )


def main():
    df = load_decisions()
    r = df[df["pnl_if_taken"].notna()].copy()
    print(f"=== Tight-spread favorite pocket — from decisions.jsonl (n_resolved={len(r)}) ===\n")
    print("half_spread is REAL (captured from the live book), not a 0.005 pin.")
    print(f"  half_spread range: {r['half_spread'].min():.4f}–{r['half_spread'].max():.4f}, "
          f"median {r['half_spread'].median():.4f}\n")

    pnl = "pnl_if_taken"
    _line("all resolved windows", _stats(list(r[pnl])))
    _line("tight spread (<0.005)", _stats(list(r[r["half_spread"] < 0.005][pnl])))
    _line("wide spread (>=0.005)", _stats(list(r[r["half_spread"] >= 0.005][pnl])))
    fav = r[(r["mid"] >= 0.80) & (r["mid"] <= 0.97)]
    _line("favorite [0.80,0.97]", _stats(list(fav[pnl])))
    pocket = fav[fav["half_spread"] < 0.005]
    _line("POCKET fav[0.80,0.97]+tight", _stats(list(pocket[pnl])))

    print(
        "\nVerdict: the pocket is observable and positive in-sample. But READ THE CIs\n"
        "CAREFULLY. The broad cuts (all, wide-spread) include 0. The tight/favorite/\n"
        "pocket cuts EXCLUDE 0 — yet that is a trap, not a green light: they contain\n"
        "ZERO losers, so the variance-based CI is spuriously narrow. This is exactly\n"
        "the negative-skew payoff (many small wins, a rare full-$1 loss not yet\n"
        "sampled): the pocket is n=2 here. A single tail loss flips the sign. Tight-\n"
        "spread favorite windows are RARE live, so a powered sample would take a long\n"
        "time. This is a lead for the BACKTESTER (full history, deflated), NOT a live\n"
        "signal to trade on these few windows."
    )


if __name__ == "__main__":
    main()
