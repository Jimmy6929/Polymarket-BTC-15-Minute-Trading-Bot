# Crypto Trading-Edge Research — An Honest Negative Result

[![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Result](https://img.shields.io/badge/result-no%20deployable%20edge%20found-critical)](RESEARCH_JOURNEY.md)
[![Method](https://img.shields.io/badge/method-OOS%20%2B%20Deflated%20Sharpe%20%2B%20pre--registration-brightgreen)](build-steps/PREREGISTRATION.md)

> **What this is.** A quant research apparatus that asks one question honestly: *can a
> retail trader, on a home machine with modest capital, find a systematic trading edge?*
> It tests that question across **four structurally-distinct strategy categories and two
> markets**, and documents a **rigorous negative result** — including the statistical
> machinery that repeatedly caught promising-looking strategies as artifacts before any
> capital was risked.
>
> **The headline finding is the deliverable.** No deployable edge was found. Every candidate
> that looked good in-sample was killed out-of-sample, by fees, by survivorship bias, or by
> negative skew. That is not a failed project — in systematic research, a disciplined "no"
> that stops you from funding a losing strategy *is* the result. **Do not run any of this
> against live capital.**

---

## The four-category kill-journey

Each row is a strategy class tested with the same discipline (out-of-sample holdout,
Deflated Sharpe, realistic frictions). Each one died, and the *way* it died is the lesson.

| # | Strategy class | Market | In-sample | Out-of-sample / honest verdict |
|---|---|---|---|---|
| 1 | Directional, trade the book (minute-13 trend) | Polymarket BTC 15m | 92% win rate | **−$65.91, EV-negative net of fees**; win rate is a negative-skew mirage ([REPORT.md](REPORT.md)) |
| 2 | Directional, lead-lag (spot vs stale book) | Polymarket BTC 15m | — | **The book beats raw spot** when they disagree; no home-reachable edge |
| 3 | Time-series momentum (vol-scaled) | BTC/ETH/SOL perps | Sharpe **+1.33**, DSR 0.967 | **Holdout Sharpe −0.19** — regime/survivorship artifact, dead OOS |
| 4 | Cross-sectional momentum (long/short) | 80-perp universe | Sharpe **+1.60**, DSR 0.998 | Holdout Sharpe **+5.67** → *too good* → diagnosed as **survivorship** (de-biased Sharpe 0.39) |
| 5 | Non-directional funding carry | Perps | +30% APR (2021) | **+1% APR (2026), compressed away**; cross-sectional variant blows up (−86% DD) |

**The structural conclusion:** every way to make money with a retail bot reduces to needing
**speed** (lost from home), **capital** (the premium pays ~nothing per dollar until you have
millions), or **bearing a negative-skew tail a backtest can't see**. Four categories, one
answer. See **[RESEARCH_JOURNEY.md](RESEARCH_JOURNEY.md)** for the full write-up.

---

## Why this repo is worth reading

It is a worked example of the discipline that separates research from hope:

- **Out-of-sample by construction.** Frozen train/holdout splits with embargo; the holdout is
  single-shot and pre-registered ([build-steps/PREREGISTRATION.md](build-steps/PREREGISTRATION.md)).
- **Deflated Sharpe Ratio** (Bailey & López de Prado 2014) on every result, with an honest
  multiple-testing count `n_configs` — so a 5-config sweep is penalised for being a 5-config sweep.
- **Look-ahead is unit-tested.** Signals use only data through `t-1`; a deliberately-leaky
  variant is run to *prove* the real one isn't peeking.
- **Frictions are first-class:** real Polymarket fee curve, perp funding costs, spread-crossing
  fills, slippage. The 92%-win Polymarket strategy is EV-negative *because* of them.
- **Survivorship is hunted, not assumed.** When cross-sectional momentum returned an absurd
  out-of-sample Sharpe of 5.67, a diagnostic traced 85% of the P&L to coins that listed in
  2024–2025 and were only in the universe *because* they had already pumped. The "edge" was
  the backtest reading the answer key.
- **A pre-registered self-improvement loop** (`.claude/`) that ran 9 automated iterations and
  **accepted 0** — the multiple-testing immune system working as designed.

---

## What's in here (and what's archived)

**Real, runnable research code** — `build-steps/`:

| File | What it does |
|---|---|
| `fetch_data.py` | Builds the Polymarket BTC-15m dataset (19,182 markets) + Coinbase spot, atomic-write + manifest |
| `real_backtester.py` | Polymarket binary backtester: real fee curve, fee-regime split, bootstrap CI |
| `leadlag_backtester.py` | Minute-scale spot-vs-book lead-lag kill-test + dislocation diagnostic |
| `perps_fetch.py` / `perps_fetch_universe.py` | Binance perp klines + funding (3 majors / 80-name universe) |
| `perps_backtester.py` | Vol-scaled time-series momentum + **prop-firm-eval simulator** |
| `perps_xs_backtester.py` | Cross-sectional long/short momentum (the survivorship case study) |
| `stats/deflated_sharpe.py` | Deflated Sharpe Ratio implementation (with self-test) |

Plus [`REPORT.md`](REPORT.md) (the Polymarket evidence) and [`RESEARCH_JOURNEY.md`](RESEARCH_JOURNEY.md)
(the full four-category narrative).

**Archived — `archive/`:** an earlier, never-wired live-execution stack (Nautilus integration,
Redis mode-switching, Grafana exporter, a "self-learning" engine that read from an empty buffer).
It was built *before* an edge was established — the wrong order — and is kept, honestly labelled,
as a record of that lesson rather than deleted. It does not run and was never validated. See
[`archive/README.md`](archive/README.md).

---

## Reproduce the headline results

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Polymarket: the 92%-win-rate strategy that loses money
python build-steps/real_backtester.py --split both

# Perp momentum: in-sample edge that dies out-of-sample
python build-steps/perps_fetch.py
python build-steps/perps_backtester.py --build-splits --holdout-months 6
python build-steps/perps_backtester.py --split train --sweep-lookbacks 7,14,30,60,90

# Cross-sectional momentum: the survivorship mirage
python build-steps/perps_fetch_universe.py --top 80
python build-steps/perps_xs_backtester.py --split train --sweep-lookbacks 7,14,30,60,90
```

---

## Disclaimer

Educational research only. Nothing here is investment advice, and none of it should be run
against live capital — the entire point of the project is that it found **no edge worth
trading.** Past backtest performance, especially the in-sample numbers, does **not** indicate
future results; that is precisely the failure mode this repo documents.
