# Can a retail trader find a systematic edge from home? A four-category falsification.

This is the research memo behind the repo. It documents an honest, end-to-end attempt to find
a deployable systematic trading edge for an individual on a home machine with modest capital —
and why, across four structurally-distinct strategy categories and two markets, the answer was
**no**. The value of the document is not the conclusion; it is the *method by which each
promising candidate was killed* before it could cost real money.

A recurring discipline runs through every test:

- **Out-of-sample by construction** — frozen train/holdout splits with a time embargo; the
  holdout is **single-shot and pre-registered** (the config and pass/fail criteria are written
  down *before* the holdout is unsealed).
- **Deflated Sharpe Ratio** (Bailey & López de Prado, 2014) with an honest multiple-testing
  count — a 5-config sweep is deflated as a 5-config sweep, so a lucky cell can't masquerade as
  signal.
- **Realistic frictions** — fees, funding, spread-crossing fills, slippage — applied *before*
  any verdict.
- **Adversarial self-checks** — look-ahead is tested by running a deliberately-leaky variant;
  survivorship is hunted with explicit diagnostics rather than assumed away.

---

## Test 1 — Directional, trade the book (Polymarket BTC 15-minute)

**Hypothesis.** Enter at minute 13 of a 15-minute up/down market following the book's own
trend; the high observed win rate implies an edge.

**Result.** Over 19,182 markets / 17,202 trades: **92% win rate, −$65.91 total P&L, −0.38%
ROI** net of the real Polymarket fee curve — **−$64.11 in the current fee regime alone.** A
fee-regime decomposition showed *every* dollar of apparent profit lived in a no-longer-existing
zero-fee era.

**Why it died.** The 92% win rate is the *symptom*, not the edge: buying favorites at ~$0.92
to win ~8¢ while occasionally losing ~92¢ is a negative-skew "pick up pennies in front of a
steamroller" payoff. The ~7¢ round-trip fee exceeds the per-trade edge. **Win rate ≠
profitability.** (Full evidence: [REPORT.md](REPORT.md).)

**Method note.** A pre-registered self-improvement loop then ran 9 automated iterations trying
to repair it and **accepted 0** — the OOS + Deflated-Sharpe gate correctly refused every overfit.

---

## Test 2 — Directional, lead-lag (spot leads the stale book)

**Hypothesis.** The winning bots on these markets exploit a 2–10 second window where the
Polymarket book lags confirmed Binance/Coinbase spot. Is there a *minute-scale* residue a slow
(home-latency) trader could capture?

**Setup.** A dedicated backtester (`leadlag_backtester.py`) compared spot-vs-strike direction
to the book price at each minute, with a strict look-ahead guard (spot taken from a candle
*proven complete before* the book tick).

**Result.** Book and spot **agree on direction 97.4%** of the time. On the 2.5% where they
disagree, **the spot side won only ~40%** — i.e. the book is the *better* forecaster. No
minute-scale dislocation survives.

**Why it died.** The exploitable window is a sub-second speed race, already owned by sub-100ms
bots; by the time a minute passes the book has fully repriced. From a home machine, this game
is structurally unwinnable.

---

## Test 3 — Time-series momentum on majors (BTC/ETH/SOL perps)

**Hypothesis.** Vol-scaled time-series momentum — a genuinely documented systematic edge — on
liquid perps, sized to pass a crypto prop-firm evaluation.

**Setup.** `perps_backtester.py`: sign-of-trailing-return signal, volatility-targeted sizing,
funding + fees modelled, plus a **prop-eval simulator** (does equity hit a profit target before
breaching a trailing drawdown?). Look-ahead verified with a leaky-signal control (leaky +250%/yr
vs clean +90%/yr — the clean signal was not peeking; signal decayed monotonically with lag, the
fingerprint of real momentum).

**Result.** In-sample (2020–2025) looked *excellent*: **Sharpe +1.33, DSR 0.967** (above the
0.95 bar), bootstrap EV CI strictly positive, positive in 5 of 6 years including the 2022 bear.
The single pre-registered holdout (most recent 6 months): **Sharpe −0.19, −10.7%, DSR 0.447.**

**Why it died.** Three hand-picked assets over the greatest crypto trend era in history is close
to a tautology for a momentum strategy. The edge was regime/survivorship-dependent; the moment
the held-out window turned choppy, it bled. **An in-sample Deflated Sharpe of 0.967 still failed
out-of-sample** — the central humility of the field: in-sample significance does not imply
generalization.

---

## Test 4 — Cross-sectional momentum (80-perp universe) — the survivorship case study

**Hypothesis.** The *fair* test of momentum isn't 3 assets — it's broad **cross-sectional**
long/short across a wide universe (long top-quartile momentum, short bottom-quartile,
dollar-neutral). Diversification should kill the regime-dependence, and market-neutrality should
fix the drawdown profile.

**Setup.** `perps_fetch_universe.py` pulled the top-80 USDT perps by volume;
`perps_xs_backtester.py` ran the long/short book with the same discipline.

**Result.** In-sample: **Sharpe +1.60, DSR 0.998**, positive every year. Holdout: **Sharpe
+5.67, +1694% CAGR.**

**The catch.** A holdout Sharpe of 5.67 — *higher* than the in-sample 1.60 — is not a triumph;
it is an alarm. Real edges decay out-of-sample, they do not quadruple. When OOS dramatically
beats IS, a bias is peaking in the OOS window. A diagnostic confirmed it:

| Universe | Holdout Sharpe |
|---|---|
| Full 80 (selected by *current* volume) | **+5.67** |
| Established coins only (listed pre-2023) | **+0.39** |

**85% of the holdout P&L came from coins that listed in 2024–2025** — present in the universe
*only because* they had already pumped into 2026 and become liquid. The backtest was reading the
answer key. Strip the survivorship and the edge is ~0.39 Sharpe, optimistic before realistic alt
slippage. **The strategy was sound; the universe selection was the contamination** — and the
absurd number is what *triggered* the bias hunt rather than a victory lap.

---

## Test 5 — Non-directional funding carry

**Hypothesis.** Stop predicting direction entirely; harvest the structural premium where perp
longs persistently pay shorts (delta-neutral carry). The one category that is neither a speed
race nor a forecast.

**Result.** Real, but arbitraged away. BTC delta-neutral funding carry has compressed from
**+30.6% APR (2021) to +1.0% APR (2026)** (39% of funding prints now negative); negative on SOL.
A cross-sectional funding-harvest variant blows up (in-sample max drawdown **−86%**, holdout
**Sharpe −2.82**) — the same negative-skew steamroller.

**Why it died.** The premium is genuine but competed down to ~risk-free in the current regime,
and its true risk (exchange failure, depeg, liquidation cascade) lives *off-distribution* —
invisible to a backtest, which is exactly why carry strategies flatter themselves.

---

## The structural synthesis

Four categories, five tests, one answer. Every route to a retail bot's profit reduces to needing
exactly one of three things, none of which a home trader has:

1. **Speed** — lost from a home machine (lead-lag, arbitrage, market-making).
2. **Capital** — the premium pays ~nothing per dollar until you have millions (carry, MM).
3. **A negative-skew tail invisible to backtests** — collect pennies, get steamrolled
   (Polymarket favorites, funding-harvest, grid, vol-selling).

This is consistent with the published base rate: across millions of prediction-market and crypto
accounts, the large majority lose, and risk-adjusted profits concentrate at scales and
infrastructure levels unavailable to an individual. The disciplined conclusion is to **stop
hunting** — and to treat the apparatus and the rigor as the real output.

---

## What this work demonstrates

- Building point-in-time-correct datasets and friction-aware backtests from scratch (Polymarket
  CLOB + Gamma, Binance perp klines + funding).
- Out-of-sample design, embargo, single-shot pre-registered holdouts.
- Deflated Sharpe / multiple-testing correction applied honestly.
- Catching look-ahead and survivorship with adversarial diagnostics — including killing a
  strategy of one's own that showed a 5.67 Sharpe.
- Reasoning about market microstructure (fees, funding, adverse selection, capacity) to explain
  *why* each result is what it is.

In short: the ability to **kill a promising strategy before the market does.** That is the
deliverable.

---

*Reproduce any result with the commands in [README.md](README.md). Pre-registered protocol:
[build-steps/PREREGISTRATION.md](build-steps/PREREGISTRATION.md).*
