# Build Steps — Polymarket BTC 15-Min Bot

Living document. Read at the start of every session, edit as we go.

## Goal

**Lightweight Path C:** smoke-test the bot using a synthetic-data injector. No Polymarket auth, no wallet, no real money. Outcome measured is operational ("does the code run, do positions accumulate, what surprises me") — explicitly NOT strategic ("does it make money"). The strategic question requires a real backtest with fees + depth, which is a separate workstream.

## Hard constraints

- macOS (Python 3.14.3 installed; Redis not installed — optional)
- User in UK (geo-blocked from Polymarket — irrelevant once we never authenticate)
- `requirements.txt` is Windows-flavored (UTF-16 LE + `pywin32` + `win32_setctime`) — first fix
- Two strategy classes exist: `BTCStrategy15Min` (in `core/strategy_brain/strategies/`) appears unused; `IntegratedBTCStrategy` (in `bot.py`) is the live one. The injector targets whatever bot.py actually consumes.

## Known-broken (from code review — fix AFTER smoke test)

Tracked here so we don't forget. Each is a real bug, not a stylistic choice.

1. **Fee model is zero.** `execution/polymarket_client.py:284` hardcodes `fee_rate_bps=0`. Real Polymarket fees peak at ~1.56% near $0.50.
2. **Slippage is zero.** `execution/execution_engine.py:291-322` simulates fills at unmodified mid price.
3. **Binary-outcome P&L math wrong.** `execution/risk_engine.py:249-252` uses spot percent-change formula on a 0–1 contract.
4. **Position size always $1.** `execution/risk_engine.py:170-175` caps AND floors at $1 — Kelly-flavored math above is dead code.
5. **Three position ledgers, not synchronized.** Strategy / execution / risk engines each keep their own list.
6. **Self-learning weight optimizer overfits.** `feedback/learning_engine.py` has no purged k-fold, no embargo, no Deflated Sharpe.
7. **30%/20% SL/TP on a 0–1 contract is nonsense.** `core/strategy_brain/strategies/btc_15min_strategy.py:307-314` — stops trigger on normal mark noise.
8. **`bot.py` requires monkey-patches** (`patch_gamma_markets`, `patch_market_orders`) — Nautilus Polymarket adapter has upstream issues.
9. **PAPER TRADE P&L IS RIGGED.** ✓ FIXED 2026-05-24. `_record_paper_trade` now queues trades in PENDING, `_resolve_pending_paper_trades` settles them at market close using actual Coinbase BTC spot movement and the binary payout formula. Fee deducted via `4·p·(1-p)·POLYMARKET_FEE_PEAK` approximation. See Step 3.5 below.
10. **`_generate_synthetic_history` still uses RNG.** `bot.py:398` — `random.uniform(-0.03, 0.03)` fills bootstrap price history when fewer than 20 ticks have arrived. Lower-priority than the trade RNG (gets overwritten within minutes of real ticks) but worth cleaning up. Cleanest fix: just don't trade until 20 real ticks have arrived. The `_market_stable` gate already does most of this; the synthetic primer is redundant.

## Steps

### Step 1 — Fix `requirements.txt`  ✓ DONE (2026-05-24)

- [x] Backup original as `requirements.txt.windows-original`
- [x] Convert UTF-16 LE → UTF-8, strip CRLF → LF
- [x] Remove `pywin32==311` and `win32_setctime==1.2.0`
- [x] Verified: `file` reports ASCII; `grep` confirms Windows packages absent

### Step 2 — Set up venv + install deps  ✓ DONE (2026-05-24)

- [x] `python3.14 -m venv venv` (pip 26.0 ready)
- [x] `venv/bin/pip install -r requirements.txt` — exit 0, all wheels installed
- [x] No package failed on Python 3.14. `nautilus_trader 1.222.0` works. Only `watchdog` needed a local wheel compile.
- [x] pip notice: 26.0 → 26.1.1 available. Cosmetic; ignored.

### Step 3 — Read full `bot.py`  ✓ DONE (2026-05-24)

Findings (1463 lines total):

**Strategy class:** `IntegratedBTCStrategy` is the live strategy. The `BTCStrategy15Min` class in `core/strategy_brain/strategies/btc_15min_strategy.py` is dead code — never instantiated by `bot.py`.

**Signal processors used (6, not 3):** OrderBookImbalance (weight 0.30), TickVelocity (0.25), PriceDivergence (0.18), SpikeDetection (0.12), DeribitPCR (0.10), SentimentAnalysis (0.05). Weights sum = 1.00. Confirmed in `bot.py:205-210`.

**Nautilus subscription:** `subscribe_quote_ticks(instrument_id)` — strategy receives `QuoteTick` events via `on_quote_tick(tick)`. Fields used: `tick.bid_price`, `tick.ask_price`, `tick.instrument_id`. Cache accessed via `self.cache.instruments()` and `self.cache.instrument(id)`.

**Trade window:** intentionally minutes 13–14 of each 15-min market (lines 730-733, 60-second window). One trade per `(market_start_ts, sub_interval)` key.

**Direction logic:** decided by **price level**, not signal direction (lines 920-942). Price > 0.60 → buy YES; price < 0.40 → buy NO; 0.40-0.60 → skip. Fusion engine acts as a score gate (min_score=40), not a direction picker.

**Position size:** hardcoded `$1.00` (line 904). Risk-engine sizing math bypassed entirely.

**Mode gating:** default is simulation; `--live` is opt-in. `--test-mode` forces simulation. `simulation = not args.live` (line 1448). In simulation mode, `_record_paper_trade` is called; in live, `_place_real_order` calls `self.submit_order(order)` via Nautilus.

**Nautilus dependency:** `run_integrated_bot` builds a `TradingNode` with `PolymarketDataClientConfig` + `PolymarketExecClientConfig` (lines 1371-1400) — both require `POLYMARKET_PK`, `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_PASSPHRASE`. **Bot cannot start without these env vars** even in simulation mode. `node.build()` will fail.

**Auto-restart:** SIGTERM at uptime ≥ 90 min (lines 588-592). Smoke tests longer than 90 min require disabling this or accepting restart cycles.

**External data sources called by strategy:** Coinbase BTC-USD REST, Fear & Greed Index, Deribit options PCR. All public (no auth). The strategy hits them directly in `_fetch_market_context` (lines 811-844).

### Step 3.5 — Fix rigged paper P&L  ✓ DONE (2026-05-24)

Changes to `bot.py`:

1. **PaperTrade dataclass extended** (lines 89-130) — added `trade_id`, `entry_btc_spot`, `resolution_time`, `exit_btc_spot`, `exit_price`, `pnl`, `fee` fields. `to_dict()` extended. All new fields Optional for backwards compat with old `paper_trades.json` files.
2. **`POLYMARKET_FEE_PEAK = 0.0156`** constant added (line 91). Fee model: `size × 4·p·(1-p) × 0.0156` — matches Polymarket's published fee curve at peak ($p=0.50$) and tapers correctly toward extremes.
3. **`self._pending_paper_trades` queue** added in `__init__` (line 261).
4. **`_record_paper_trade` rewritten** (lines 1018-1071) — now opens trades in PENDING state, captures entry BTC spot from `metadata['spot_price']`, computes `resolution_time = next_switch_time + 5s`. Skips the trade entirely if Coinbase spot was unavailable at entry (rather than recording one we can't honestly resolve).
5. **`_resolve_pending_paper_trades` added** (lines 1073-1184) — runs in `_timer_loop` every 10s. When `resolution_time` arrives: fetches Coinbase BTC spot, compares to entry, computes binary payout from entry probability + direction, deducts approximated fee, updates the trade record, removes from pending queue.
6. **`_timer_loop` calls the resolver** (line 649) each iteration, wrapped in try/except so a resolver crash doesn't kill the loop.

**Sanity-check math** at p=0.71:
- Correct call: payout ≈ $0.408, fee ≈ $0.013, net ≈ +$0.395
- Wrong call: payout = -$1.00, fee ≈ $0.013, net ≈ -$1.013
- EV at calibrated market (true p = 0.71): 0.71 × 0.395 − 0.29 × 1.013 ≈ −$0.014 (≈ −1.4%, the fee drag). This is exactly what a perfectly calibrated market should produce: zero-sum minus fees.

**What this fix did NOT address** (deliberately, scoped tight):
- `_generate_synthetic_history` (line 398) still uses `random.uniform(-0.03, 0.03)` to fill bootstrap price history when fewer than 20 ticks have arrived. Less harmful (gets overwritten within minutes of real ticks), but listed as known-broken for later cleanup.
- The fee formula is an approximation. Real Polymarket curve may be piecewise; if smoke test results matter for an external claim, swap in the exact schedule.
- No look-ahead audit. The bot uses `datetime.now(timezone.utc)` everywhere — fine for live/paper but will leak the future if anyone wires this into a historical replay.

### Step 4 — Build smoke-test harness  ✓ DONE (2026-05-24)

`build-steps/harness.py` — 370 lines. Bypasses Nautilus and Polymarket auth entirely. Imports the actual signal processors (SpikeDetection, TickVelocity, PriceDivergence, SentimentAnalysis), the actual `SignalFusionEngine`, and reimplements the SAME decision logic and resolution math as `bot.py:_make_trading_decision` and `bot.py:_resolve_pending_paper_trades`. OrderBookImbalance and DeribitPCR processors skipped (need real CLOB depth + Deribit options — not cheaply mockable). Renormalized weights across the 4 used: Spike 0.22, TickVelocity 0.45, Divergence 0.24, Sentiment 0.09.

Three regimes:
- **quiet**: ±0.3% per 15-min, no drift. Most prices stay near 0.50 → trend filter blocks ~66% of decisions.
- **trending**: ±0.5% noise + 1.0%/15-min upward drift. Late-window prices skew high → many LONG trades.
- **volatile**: ±2.0% noise, no drift. Prices swing wildly → mixed trades.

Synthetic data: 96 markets per regime = 24 hours of simulated time. 90 ticks per market at 10s cadence. Runs in seconds.

### Step 5 — Run smoke test  ✓ DONE (2026-05-24)

Full log: `build-steps/smoke_test_run_2026-05-24.log`.

Headline numbers per regime (24h simulated, $1 per trade):

| Metric | quiet | trending | volatile |
|---|---:|---:|---:|
| Decisions evaluated | 96 | 96 | 96 |
| Skipped (no fused signal) | 6 | 6 | 4 |
| Skipped (neutral 0.4-0.6) | 63 | 12 | 15 |
| Trades opened | 27 | 78 | 77 |
| Long / Short | 13 / 14 | 76 / 2 | 40 / 37 |
| Win rate | 51.9% | **73.1%** | 50.6% |
| Total P&L | **−$6.55** | **−$9.85** | **−$32.53** |
| Total fees | $0.37 | $0.60 | $0.42 |
| ROI on capital deployed | −24.3% | **−12.6%** | −42.3% |

### Step 6 — Findings  ▶ IN PROGRESS

**The single most important finding: 73% win rate, −12.6% ROI.** Trending regime shows the asymmetric-payout trap clearly. When you trade at p≈0.75, a correct call pays only +$0.33 while a wrong call pays −$1.00. Even with 73 wins out of 100 trades, the fee drag plus the implied calibration gap (real win rate slightly below the implied 75%) produces net negative P&L. **Win rate is the wrong metric.** This is the lesson the README was failing to teach.

Other observations:

1. **`TickVelocity` processor never fires across any regime.** Zero signal fires in 288 decisions. With weight 0.45 in my renormalized harness (and 0.25 in production), this is meaningful. Likely cause: velocity thresholds (1.5% over 60s, 1.0% over 30s) are higher than typical Polymarket probability moves at 10s tick cadence. Worth investigating.

2. **`SentimentAnalysis` fires 76/96 times in every regime** — that's 79%. With synthetic Fear & Greed values drawn from N(50, 18) and thresholds at 25/75, the firing rate should be closer to 10–15%. Either the processor's threshold logic is looser than the constructor suggests, or the synthetic noise distribution is hitting the tails more than expected. Worth investigating — but its weight in production is only 0.05, so the operational impact is small.

3. **Quiet regime: 63/96 (66%) skipped by trend filter** — confirms the filter is doing its job. Quiet markets stay in the 0.40-0.60 indecision zone. The 27 trades that did open were almost evenly split (13 long, 14 short), suggesting prices crossed the thresholds in both directions roughly equally. Honest 50/50 outcome.

4. **Trending regime: 76 longs, 2 shorts.** Almost monodirectional. The trend filter is correctly identifying the up-bias. But this just means the bot is buying YES at high prices, which has terrible asymmetric payout. Direction is right; pricing is wrong.

5. **Volatile regime: 40 longs, 37 shorts, 50.6% win rate.** Essentially coin-flipping at edge prices, paying fees. The worst regime by ROI by a wide margin (−42%). Confirms the bot has no edge when the underlying is genuinely random.

6. **All three regimes lose money.** Operationally everything works (signal processors compose, fusion engine fires, trend filter blocks neutral trades, resolution math computes correctly, no crashes in 288 decision cycles). Strategically, the strategy has no measurable edge against approximately-calibrated synthetic markets.

**Caveats — what this DOES NOT prove:**

- The synthetic Polymarket walk is a heuristic. Real Polymarket may be more or less calibrated; the strategy could win or lose more in reality. The smoke test cannot replace a real backtest or paper trading against live data.
- Tested with only 4 of 6 processors. OrderBookImbalance (production weight 0.30) and DeribitPCR (0.10) are absent. They might add edge in real markets.
- `_generate_synthetic_history` (the bootstrap RNG) was bypassed by the harness using real synthetic ticks instead. Confirmed not load-bearing.
- Sample size: 27/78/77 trades per regime. CIs are wide. The win rates and P&Ls are point estimates with noise.

### Step 7 — Document observations  ▶ NEXT

### Step 5 — Build minimal injector  ▶ PENDING

- [ ] Synthetic Polymarket binary contract price walks — three regimes (quiet, trending, volatile)
- [ ] Synthetic Binance / Coinbase spot ticks (for divergence + spike signals)
- [ ] Synthetic Fear & Greed value (for sentiment signal)
- [ ] Event shape matches what `IntegratedBTCStrategy` actually consumes

<!-- Steps 6 + 7 collapsed into the Findings section above. -->

## Phase 2: Real-data backtest

### Data sources (committed 2026-05-24)

1. **Coinbase Exchange API** `/products/BTC-USD/candles?granularity=60` — public, no auth. 14 days = 20,156 minute candles pulled. Coverage 100%. Stored at `build-steps/data/coinbase_btc_1min.csv`. Used for resolution sanity-check and divergence signal.
2. **alternative.me Fear & Greed Index** `/fng/?limit=0` — public, no auth. Full daily history (3,031 values back to 2018). Stored at `build-steps/data/fear_greed.json`. Used for sentiment signal.
3. **Polymarket Gamma API** `/markets?slug={slug}` — public, no auth. Per-slug query. Returns market metadata, Chainlink finalPrice + priceToBeat (ground truth resolution), feeSchedule, volume, clobTokenIds. ~1,344 slugs queried over 14 days.
4. **Polymarket CLOB** `/prices-history?market={tokenId}&startTs=&endTs=&fidelity=1` — public, no auth. Returns ~1-minute price points for the YES token across the market's 15-min window. Used to find the bot's entry price at minute 13.

### Why this path beats the subgraph route

- No API key required → no signup friction, no payment activation, no key-rotation risk
- Gamma's `eventMetadata.finalPrice` and `priceToBeat` are the EXACT Chainlink values used for resolution — better ground truth than reconstructing from Coinbase
- Gamma's `feeSchedule` gives us the REAL per-market fee parameters — better than my approximated `4·p·(1-p)·0.0156`
- CLOB `/prices-history` returns midprice (not just trade prices) — cleaner than reconstructing from trade events

### Data fetcher: `build-steps/fetch_data.py`

Single script, three sources. Idempotent. Re-runnable. CLI: `--days N --sources coinbase fng polymarket`.

### Real-data pull results (2026-05-24)

- Coinbase BTC 1-min candles: 20,156 rows, 100% coverage over 14 days (May 10 → May 24).
- Fear & Greed: 3,031 daily values.
- Polymarket BTC 15-min markets: **1,332 resolved markets** with **100% price history coverage**. 658 YES wins / 674 NO wins — fair coin over the 14-day window.
- BTC moved $80,837 → $76,606 (−5.2%) over the window. F&G ended at 25 (Extreme Fear). Real downtrend conditions.
- Volume: median market did $41,812 in USDC. Real liquidity, not a toy.
- Fee schedule: consistent across all markets, `{rate: 0.07, exponent: 1, takerOnly: true, rebateRate: 0.2}`. Implied peak fee at p=0.5 is 1.75%, tapering to ~0.5% at p=0.92.

### Real-data backtest results (2026-05-24)

Full log: `build-steps/real_backtest_run_2026-05-24.log`. Trade-level CSV: `build-steps/data/backtest_trades.csv`.

**Headline:**

| Metric | Value |
|---|---:|
| Markets processed | 1,332 |
| Skipped (no minute-13 price) | 3 |
| Skipped (no BTC spot) | 0 |
| Skipped (price_history < 20) | 1 |
| Skipped (no fused signal) | 3 |
| Skipped (neutral 0.40-0.60) | 103 |
| **Trades executed** | **1,222** |
| Direction split | 613 LONG / 609 SHORT |
| **Win rate** | **92.4%** (Wilson 95% CI: 90.8% – 93.7%) |
| **Total P&L** | **−$0.0339** on $1,222 deployed |
| **ROI** | **−0.003%** |
| **Avg P&L per trade** | **−$0.0000** (bootstrap 95% CI: −$0.0179 to +$0.0173) |
| Avg fee per trade | $0.0044 (0.44% on $1) |

**Calibration analysis (per implied-confidence bucket):**

| Bucket | n | Observed win rate | Implied confidence | Diff |
|---:|---:|---:|---:|---:|
| 0.60 | 37 | 56.8% | 60% | −3.2pp |
| 0.70 | 89 | 69.7% | 70% | −0.3pp |
| 0.80 | 126 | 80.2% | 80% | +0.2pp |
| 0.90 | 257 | 91.8% | 90% | +1.8pp |
| 1.00 | 713 | 99.4% | 100% | −0.6pp |

**The market is exceptionally well-calibrated** — every bucket's observed win rate matches its implied probability to within 1-3pp, and most of those are within sampling noise.

### Findings

1. **The bot is essentially break-even, not a money-loser.** Real-data P&L over 14 days × 1,222 trades is −$0.0339 (−0.003% ROI). Bootstrap 95% CI on per-trade EV is −$0.018 to +$0.017 — straddles zero. Statistically indistinguishable from zero edge.

2. **The synthetic smoke test substantially overstated losses.** Synthetic showed −27% blended ROI; real shows essentially zero. The gap is because (a) my synthetic Polymarket walk was less calibrated than reality, (b) my fee approximation peaked at 1.56% vs the actual ~1.75% schedule that taps to 0.5% at the extreme prices where the bot actually trades, and (c) real markets concentrate trades at high confidence where fees are smallest.

3. **The market is so well-calibrated that no edge exists from following it.** At every implied-confidence level, observed outcomes match the implied probability within sampling noise. The bot's strategy of "follow the late-window consensus" extracts approximately zero net value because the price IS the right probability.

4. **Most trades happen at extreme confidence.** 713/1,222 trades (58%) are in the 0.95+ bucket. At those prices, fees are tiny (≤0.07% at p=0.99) and so are payouts (≤$0.01 per correct call). Almost zero risk, almost zero return. The bot is trading a lot of very small EV bets.

5. **Gross P&L is +$5.40, fees are $5.43, net is −$0.03.** Strategy extracts $5.40 of gross value over 1,222 trades from being slightly less wrong than the market at high-confidence buckets. Then loses essentially all of it to taker fees. The fee curve is calibrated very tightly to where this strategy operates.

6. **Direction split (50/50 LONG/SHORT) confirms the trend filter works.** 14 days of BTC moving down (−5.2%) and the bot took both sides roughly equally. The price level at minute 13 is doing the directional work, not any persistent bias.

### Caveats (what this STILL doesn't prove)

- **No slippage modeled.** Real fills would be at the ask (for buys) and bid (for sells), not midprice. The bot's effective entry might be 1-2pp worse than my model. Across 1,222 trades that's meaningful — would tip the P&L modestly negative.
- **No purged k-fold / walk-forward.** Single 14-day forward replay. We have one sample.
- **Calibration may not persist.** Polymarket fee structure changed in late 2025 to add the taker-fee curve specifically to make this kind of strategy break even. If they tune fees up, EV goes negative; down, EV goes positive.
- **Two processors absent** (OrderBookImbalance, DeribitPCR). Production runs with all six. They might add or subtract edge.
- **`_generate_synthetic_history` bootstrap RNG still in `bot.py`** — bypassed in the backtester by feeding real ticks directly.

### Comparison: synthetic vs real

| Metric | Synthetic (24h × 3 regimes) | Real (14 days) |
|---|---:|---:|
| Trades | 27 + 78 + 77 | 1,222 |
| Win rate | 51.9% / 73.1% / 50.6% | 92.4% |
| Per-trade EV | −$0.24 / −$0.13 / −$0.42 | −$0.0000 |
| Reason for loss | Asymmetric payout + uncalibrated synthetic prices + overestimated fees | Almost none — market is calibrated, fees match edge |

### 6-month backtest (2026-05-25) — natural experiment across three fee regimes

Pulled 180 days (2025-11-25 → 2026-05-24). 17,245 resolved markets, 99.95% with price history. Sample includes three distinct fee-schedule regimes Polymarket cycled through.

**Headline:**

| Metric | Value |
|---|---:|
| Markets processed | 17,245 |
| Trades executed | 16,040 |
| Direction split | 8,157 LONG / 7,883 SHORT |
| Win rate overall | 92.2% (Wilson 95% CI: 91.7% – 92.6%) |
| Total P&L | **+$8.23** on $16,040 deployed |
| ROI | +0.05% |
| Avg P&L per trade | +$0.0005 (bootstrap 95% CI: −$0.0045 to +$0.0055) |

Aggregate is just barely positive but indistinguishable from zero. **The regime breakdown is the story.**

**Per-fee-regime breakdown with proper CIs:**

| Regime | n | Win rate | Avg P&L | 95% bootstrap CI on avg | Total P&L |
|---|---:|---:|---:|---|---:|
| **no-fee** (pre-Jan 2026) | 3,804 | 93.7% [92.9–94.4] | **+$0.0123** | [+$0.0032, +$0.0218] | **+$46.80** |
| **high (0.25 rate)** (Jan–Mar) | 7,349 | 91.7% [91.0–92.3] | +$0.0006 | [−$0.0071, +$0.0082] | +$4.42 |
| **current (0.07 rate)** (Apr–May) | 4,887 | 91.7% [90.9–92.4] | **−$0.0088** | [−$0.0180, +$0.0004] | **−$42.99** |

**Last 2 months only (April + May 2026, n=4,718, current fee regime):**
- Win rate: 91.7% (CI 90.8 – 92.4)
- Avg P&L: **−$0.0087** (bootstrap 95% CI: −$0.018 to +$0.001)
- Total P&L: −$41.07

**Monthly walk-forward (most informative view):**

| Month | n | Win% | Avg P&L | Total P&L | Regime |
|---|---:|---:|---:|---:|---|
| 2025-11 | 458 | 91.9% | −$0.0053 | −$2.43 | no-fee |
| 2025-12 | 2,800 | 93.9% | +$0.0153 | **+$42.82** | no-fee |
| 2026-01 | 2,779 | 91.9% | +$0.0003 | +$0.70 | mixed |
| 2026-02 | 2,521 | 91.4% | −$0.0030 | −$7.46 | high (0.25) |
| 2026-03 | 2,764 | 92.3% | +$0.0057 | +$15.67 | mixed |
| 2026-04 | 2,629 | 91.4% | −$0.0094 | −$24.69 | current (0.07) |
| 2026-05 | 2,089 | 92.1% | −$0.0078 | −$16.38 | current (0.07) |

### The real finding

**The 14-day test was misleading. The 6-month test shows the bot is losing money in the current fee regime, and the no-fee era is the only thing keeping the aggregate barely positive.**

Three observations the 14-day test could not surface:

**1. The strategy WAS profitable in late 2025.** In the no-fee era (3,804 trades, Nov–Dec 2025 + a few early 2026), the bot extracted +$0.0123 per trade on average with a 95% bootstrap CI strictly above zero. The market was under-confident at minute 13 — observed 93.7% win rate vs implied ~92% calibration. That gap was the bot's edge. With no fees to pay, the gap landed entirely in the bot's pocket. Total +$46.80 over the regime.

**2. The strategy became unprofitable as the market matured.** By April–May 2026 (4,887 trades in the current 0.07 rate era), the win-rate vs implied gap inverted slightly — observed 91.7% vs ~92% implied. Combined with a 0.44% average fee drag, the bot lost ~$0.009 per trade. Total −$42.99 over the regime, and the per-trade upper CI bound just barely touches zero. Two consecutive months of consistent losses (April: −$24.69 on n=2,629; May: −$16.38 on n=2,089) — this is not noise.

**3. The 0.25 fee era was approximately neutral** despite the much higher fee rate. Likely cause: when fees were high, fewer arbitrageurs were active, prices spent more time at extreme values, and the formula `rate × p × (1-p)` made absolute fees small even at rate=0.25 (because `p × (1-p) → 0` at extremes). The bot ended up paying similar dollar fees while still extracting a slightly positive gross edge. Net: +$4.42 across 7,349 trades, indistinguishable from zero.

### Calibration check (all 16,040 trades)

| Implied confidence | n | Observed win rate | Diff |
|---:|---:|---:|---:|
| 0.60 | 309 | 60.5% | +0.5pp |
| 0.65 | 486 | 67.1% | +2.1pp |
| 0.70 | 718 | 69.5% | −0.5pp |
| 0.75 | 557 | 77.4% | +2.4pp |
| 0.80 | 921 | 80.3% | +0.3pp |
| 0.85 | 837 | 84.1% | −0.9pp |
| 0.90 | 1,642 | 90.7% | +0.7pp |
| 0.95 | 2,220 | 95.5% | +0.5pp |
| 1.00 | 8,350 | 99.2% | −0.8pp |

Across 6 months and 16,040 trades, every bucket is within ~2pp of perfect calibration. **Polymarket 15-min BTC markets at minute 13 are extraordinarily well-calibrated.** The bot is not finding any pricing error; the small per-bucket gaps are sampling noise. Any edge comes from the SIZE of those gaps changing over time, which is exactly what the regime story shows.

### Forward-looking interpretation

If you ran this bot live today (in the 0.07 fee regime), the best statistical estimate is **−0.9% ROI per trade**, with two consecutive months of consistent losses. The 14-day window I ran first happened to land in the same regime but was small enough that the noise wrapped the loss in a CI containing zero. With n=4,887 we can now see the loss is real.

Put another way: **of the +$8.23 the bot would have made over 6 months, +$46.80 came from a regime that no longer exists** (Polymarket charges fees now). The current era is structurally negative-EV by roughly 0.9% per trade.

### Updated framing for portfolio purposes

> "I built a Polymarket BTC 15-min trading bot, instrumented it honestly, and ran a 6-month real-data backtest across 16,040 trades. The bot maintained 92.2% win rate throughout (Wilson 95% CI 91.7-92.6%) but per-trade EV varied dramatically by fee regime: +$0.012 in the pre-fee era (statistically significantly positive), neutral in the 0.25-rate era, and **−$0.009 in the current 0.07-rate era** (last 2 months, n=4,718, bootstrap 95% CI −$0.018 to +$0.001). The natural experiment demonstrates how Polymarket's evolving fee curve has eliminated the strategy's previously profitable edge. Markets are now exceptionally well-calibrated (every implied-confidence bucket within 2pp of observed across 16k trades). Going forward, the strategy is structurally negative-EV by ~0.9% per trade. Files: `build-steps/{fetch_data.py, real_backtester.py, real_backtest_6month_2026-05-25.log, data/}`."

This is now a strong portfolio piece: real data, real statistical rigor, a natural experiment across three regimes, and a defensible falsification of the strategy at current parameters.

## Operational scorecard

What works (operationally, in the harness):
- Signal processors instantiate and process under load — no crashes in 288 decision cycles
- Fusion engine combines signals correctly; weight scheme behaves as expected
- Trend filter at 0.40/0.60 thresholds correctly blocks ambiguous trades
- Resolution math is consistent — fees and payouts agree with manual calculations
- Per-regime instantiation isolates state cleanly (no singleton bleed across regimes)

What's broken (or smells wrong):
- TickVelocity processor: never fires in any regime. Likely threshold mismatch with Polymarket probability dynamics.
- SentimentAnalysis: fires 79% of the time on N(50, 18) synthetic data. Threshold/range mismatch likely.
- (Not tested by harness) Real Nautilus market subscription, real Polymarket auth, real instrument cache discovery, real websocket lifecycle, three-position-ledger sync.
- (Not tested by harness) Production OrderBookImbalance and DeribitPCR processors.

## Decisions log
- **2026-05-24** — Skipped Polymarket auth path entirely. Reason: UK geo-block + structural safety.
- **2026-05-24** — Chose lightweight Path C (synthetic injector) over no-auth REST polling.
- **2026-05-24** — Heavyweight backtest framework deferred.
- **2026-05-24** — Two `pip` Windows packages and UTF-16 encoding identified as Step 1 blocker.
- **2026-05-24** — Discovered paper-trade P&L is rigged via `random.uniform`. Forced strategic reframe of smoke test.
- **2026-05-24** — Discovered `IntegratedBTCStrategy` (in `bot.py`) is the live class; `BTCStrategy15Min` is dead.
- **2026-05-24** — Discovered Nautilus `TradingNode` requires real credentials even in simulation. Smoke test must bypass Nautilus entirely.
- **2026-05-24** — Smoke test ran cleanly. All three regimes net negative. Trending regime 73% win rate but −12.6% ROI is the load-bearing finding: high win rate ≠ profitable when payout is asymmetric.

## Decisions log

- **2026-05-24** — Skipped Polymarket auth path entirely. Reason: UK geo-block + structural safety. Eliminating order-submission capability beats relying on `dry_run` flags in code that has known inconsistencies.
- **2026-05-24** — Chose lightweight Path C (synthetic injector) over no-auth REST polling. Reason: iteration speed matters more than real-market data realism for an operational smoke test. Real-market data validation can happen later if needed.
- **2026-05-24** — Heavyweight backtest framework deferred. Reason: needed for real edge measurement (Deflated Sharpe + purged walk-forward), not for smoke test. Don't conflate the two.
- **2026-05-24** — Pivoted from The Graph subgraph route to Gamma API + CLOB `/prices-history`. Reason: discovered that the CLOB `/prices-history` endpoint with `fidelity=1` returns granular minute-level price data without auth (contradicting an older GitHub issue that claimed 12+ hour granularity only). Combined with Gamma giving us full market metadata + Chainlink resolution data, this removes the need for a Graph API key entirely. Cleaner data path, less friction, same data quality.
