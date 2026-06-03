# A 92%-Win-Rate Strategy That Loses Money — and the Analysis That Proves It

**TL;DR.** This bot trades the BTC up/down 15-minute binary on Polymarket at
minute 13 of each window. It wins **92% of the time**. It also has **negative
expected value** after real fees: over the full available history (19,182 markets,
17,202 trades) it returns **−$65.91, ROI −0.38%**. The high win rate is the
*symptom*, not the edge — it is a short-volatility "pick up pennies in front of a
steamroller" payoff where the ~7¢ round-trip fee in the current regime is larger
than the per-trade edge. This document shows the evidence, and the
statistical-honesty machinery built to keep us from fooling ourselves.

*All P&L is net of a realistic spread-crossed fill and the Polymarket fee curve.
Reproduce with `venv/bin/python build-steps/real_backtester.py --split both`.*

---

## 1. The result

Full-history backtest (all 19,182 resolved markets, current dataset):

| metric | value |
|---|---|
| trades executed | 17,202 |
| win rate | **92.2%** (Wilson 95% CI 91.8–92.6%) |
| total P&L | **−$65.91** on $17,202 deployed |
| ROI | **−0.38%** |
| avg P&L / trade | −$0.0038 (bootstrap 95% CI [−$0.0086, +$0.0008]) |

The per-trade CI straddles zero — but the point estimate is negative, and the
**fee-regime breakdown is decisive**:

| fee regime | n | win% | total P&L |
|---|---|---|---|
| no-fee (historical) | 4,954 | 93.6% | **+$22.41** |
| high (0.25) | 7,349 | 91.7% | −$24.21 |
| **current (0.07)** | 4,899 | 91.7% | **−$64.11** |

Every dollar of "profit" lives in a **no-fee historical regime that no longer
exists**. Under the fee regime the bot would actually trade in today, it loses
−$64.11. The monthly walk-forward confirms this is structural, not a bad month:

| month | n | win% | total P&L | regime |
|---|---|---|---|---|
| 2025-12 | 2,800 | 93.9% | +$24.06 | no-fee |
| 2026-02 | 2,521 | 91.4% | −$17.21 | high (0.25) |
| 2026-04 | 2,629 | 91.4% | **−$35.38** | current (0.07) |
| 2026-05 | 2,091 | 92.1% | **−$25.11** | current (0.07) |

Win rate barely moves across regimes (91–94%). **P&L flips entirely on the fee.**

---

## 2. Why 92% wins ≠ profit

A favorite priced at $0.92 that resolves YES pays you ~8¢; the rare time it
resolves NO it costs you ~92¢. To break even you must win ~92% of the time —
*before fees*. That is exactly the win rate observed. The structure is:

- **Negative skew:** many small wins, occasional full-stake losses (1,337 of them).
- **Tiny edge:** the gross per-trade advantage is sub-cent.
- **Fee > edge:** a ~7¢ round-trip taker fee in the current regime exceeds the
  gross edge, so a coin that lands heads 92% of the time still bleeds money.

No amount of signal tuning fixes "the toll is bigger than the trip." The only
escape is a sub-population where the gross edge clears the fee — which is what the
research loop hunts for, and (so far) fails to find.

---

## 3. Out-of-sample discipline

To avoid mistaking in-sample noise for edge, the dataset is split:

- **Train** = everything before the most recent 30 days (n=16,855 markets,
  15,084 trades). Train-only baseline EV = **−$0.0026/trade**, CI [−0.0078, +0.0025].
- **Holdout** = the most recent 30 days (n=2,135) — which is the **current fee
  regime**, the hardest and most relevant window. Reserved; scored at most once
  per surviving candidate.
- **48h embargo** straddles the boundary (not 24h): the sentiment feature reads a
  *daily* Fear & Greed index and the backtester's rolling price/tick buffers carry
  across markets, so a shorter embargo would leak.

The split, the null hypothesis, and the accept thresholds are **pre-registered**
before the loop runs — see [`build-steps/PREREGISTRATION.md`](build-steps/PREREGISTRATION.md).

---

## 4. Live confirmation (paper trading)

Paper trading the live Polymarket feed (real Gamma resolution, real book spread):

- 20 resolved trades, **90% win**, **net +$0.0214 on $20 staked** — fees
  ($0.1232) ate **~85% of gross** P&L. Naive per-trade Sharpe ≈ 0.003.
- The event-sourced decision log (`decisions.jsonl`, 29 resolved windows) records
  the counterfactual P&L of *every* window, taken or skipped. It shows **only 2
  losers** — so no feature can be shown to separate winners from losers at this n;
  the question is structurally undecidable until far more windows accrue.
- Three signal channels are **stuck constant** in the live capture
  (`sentiment_score`, `SentimentAnalysis`, `OrderBookImbalance`) — flagged, not
  trusted.

**The one lead — the tight-spread favorite pocket.** Filtering to a tight book
(`half_spread < 0.005`) shows positive in-sample P&L. But
`analysis/pocket_check.py` (run it) shows the catch: those windows contain **zero
losers**, so their confidence interval is *spuriously* narrow — the negative-skew
tail loss simply hasn't been sampled yet (the full pocket is n=2). It is a lead
for the deflated backtester, **not** a live signal.

---

## 5. The honesty machinery (the actual deliverable)

The point of this repo is not the strategy — it's the apparatus that **proved the
strategy doesn't work**, and refuses to launder overfitting into a false positive:

- **Fee-regime-aware backtester** over a full backfilled dataset (19,182 markets),
  with Wilson + bootstrap CIs and a monthly walk-forward.
- **Pre-registered two-stage gate**: an in-sample train screen (positive lift,
  CI lower bound > 0, **Deflated Sharpe ≥ 0.95**, ≥80% retention) followed by a
  single-shot holdout confirmation. A pass is a *candidate*, never "profitable."
- **Multiple-testing control**: the Deflated Sharpe Ratio deflates against the
  expected maximum Sharpe of N trials, with N = cumulative configurations ever
  tried. The bar rises as the search continues.
- **The result of running it:** **9 automated iterations, 0 accepts.** The gate
  working as designed — the honest output of a search over a no-edge space is
  "nothing survives."

---

## 6. What is actually valuable here

Per the project's own thesis, P&L was never the primary asset:

1. **The dataset** — a full backfilled history of Polymarket BTC 15-min markets
   with per-market price paths, BTC spot, and fee schedules.
2. **The honest backtester + decision log** — instrumentation that can *measure*
   EV net of frictions and tell you when there's no edge, including the
   counterfactual P&L of skipped trades.
3. **A portfolio-grade research repo** — pre-registration, OOS discipline, and
   multiple-testing deflation applied to a real, adversarial market.

A strategy correctly **killed with statistics** demonstrates more quant judgment
than a backtest curve that was overfit until it looked good.

---

## References

- Bailey & López de Prado (2014), *The Deflated Sharpe Ratio* — the N-penalty.
- Bailey, Borwein, López de Prado, Zhu (2017), *The Probability of Backtest Overfitting* (CSCV).
- Bailey & López de Prado (2012), *The Sharpe Ratio Efficient Frontier* — PSR / Minimum Track Record Length.
- López de Prado (2018), *The 10 Reasons Most Machine-Learning Funds Fail*.

*Reproduce the headline table: `venv/bin/python build-steps/real_backtester.py --split both`.
Reproduce the pocket analysis: `venv/bin/python analysis/pocket_check.py`.*
