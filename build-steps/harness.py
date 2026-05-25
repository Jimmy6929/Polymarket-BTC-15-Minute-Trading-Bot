"""Smoke-test harness for the Polymarket BTC 15-min bot.

Bypasses Nautilus + Polymarket auth entirely. Instantiates the SAME signal
processors and fusion engine the production strategy uses, runs them against
synthetic market data under three regimes, and applies the SAME decision
logic and resolution math we just fixed in bot.py.

Goal: operational smoke test. See if the components compose, signals fire at
sane rates, the trend filter behaves, and the binary-payout resolution math
produces honest numbers.

NOT a backtest. NOT evidence of edge. The strategy is being run against
synthetic data — the resolved P&L tells us only what the strategy WOULD
have done in this synthetic world, not what it will do in the real one.
"""
import asyncio
import math
import random
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

# Project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Silence loguru's INFO/DEBUG spam from signal processors during the smoke test.
# We want our own summary output to be readable.
from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")

from core.strategy_brain.signal_processors.spike_detector import SpikeDetectionProcessor
from core.strategy_brain.signal_processors.sentiment_processor import SentimentProcessor
from core.strategy_brain.signal_processors.divergence_processor import PriceDivergenceProcessor
from core.strategy_brain.signal_processors.tick_velocity_processor import TickVelocityProcessor
from core.strategy_brain.fusion_engine.signal_fusion import SignalFusionEngine

# --- Constants mirrored from bot.py ---
POLYMARKET_FEE_PEAK = 0.0156
TREND_UP_THRESHOLD = 0.60
TREND_DOWN_THRESHOLD = 0.40
TRADE_WINDOW_START = 780   # 13 min into the 15-min market
TRADE_WINDOW_END = 840     # 14 min in
MARKET_INTERVAL_SECONDS = 900
TICK_CADENCE_SECONDS = 10  # synthetic tick every 10s → 90 ticks per market


@dataclass
class PaperTrade:
    trade_id: str
    timestamp: datetime
    direction: str            # "LONG" or "SHORT" (i.e. YES or NO)
    size_usd: float
    entry_price: float        # Polymarket YES probability
    entry_btc_spot: float
    resolution_time: datetime
    signal_score: float
    signal_confidence: float
    outcome: str = "PENDING"
    exit_btc_spot: Optional[float] = None
    pnl: Optional[float] = None
    fee: Optional[float] = None


@dataclass
class RegimeSummary:
    regime: str
    decisions_made: int = 0
    skipped_no_signal: int = 0
    skipped_neutral: int = 0      # price in [0.40, 0.60]
    trades_opened: int = 0
    trades_resolved: int = 0
    long_count: int = 0
    short_count: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_fees: float = 0.0
    signal_fires: Dict[str, int] = field(default_factory=dict)

    def add_signal_fire(self, source: str):
        self.signal_fires[source] = self.signal_fires.get(source, 0) + 1


class SmokeTestHarness:
    def __init__(self, regime: str, seed: int = 42):
        random.seed(seed)
        self.regime = regime
        self.summary = RegimeSummary(regime=regime)

        # Fresh processors per regime — avoids singleton state bleed
        self.spike_detector = SpikeDetectionProcessor(
            spike_threshold=0.05,
            lookback_periods=20,
        )
        self.sentiment_processor = SentimentProcessor(
            extreme_fear_threshold=25,
            extreme_greed_threshold=75,
        )
        self.divergence_processor = PriceDivergenceProcessor(
            divergence_threshold=0.05,
        )
        self.tick_velocity_processor = TickVelocityProcessor(
            velocity_threshold_60s=0.015,
            velocity_threshold_30s=0.010,
        )

        # Same weight scheme as bot.py — minus the two processors we don't run.
        # Renormalize so the 4 active weights still sum to ~1.0.
        self.fusion_engine = SignalFusionEngine()
        self.fusion_engine.set_weight("SpikeDetection", 0.22)
        self.fusion_engine.set_weight("TickVelocity", 0.45)
        self.fusion_engine.set_weight("PriceDivergence", 0.24)
        self.fusion_engine.set_weight("SentimentAnalysis", 0.09)

        self.price_history: List[Decimal] = []
        self.tick_buffer: deque = deque(maxlen=500)
        self.paper_trades: List[PaperTrade] = []
        self.pending_trades: List[PaperTrade] = []
        self._traded_market_indices: set = set()

    # ------------------------------------------------------------------
    # Synthetic data generation
    # ------------------------------------------------------------------

    def generate_btc_walk(self, num_markets: int) -> List[List[float]]:
        """Synthetic BTC spot prices, one 15-min market at a time, 10s ticks."""
        ticks_per_market = MARKET_INTERVAL_SECONDS // TICK_CADENCE_SECONDS  # 90
        btc = 50000.0

        # Per-tick sigma & drift, derived from 15-min targets
        if self.regime == "quiet":
            target_sigma_15m = 0.003   # ±0.3%
            target_drift_15m = 0.0
        elif self.regime == "trending":
            target_sigma_15m = 0.005
            target_drift_15m = 0.010   # +1% per 15m, persistent up-trend
        elif self.regime == "volatile":
            target_sigma_15m = 0.020   # ±2%, no drift
            target_drift_15m = 0.0
        else:
            raise ValueError(f"unknown regime: {self.regime}")

        tick_sigma = target_sigma_15m / math.sqrt(ticks_per_market)
        tick_drift = target_drift_15m / ticks_per_market

        all_markets = []
        for _ in range(num_markets):
            market_ticks = []
            for _ in range(ticks_per_market):
                btc *= (1.0 + random.gauss(tick_drift, tick_sigma))
                market_ticks.append(btc)
            all_markets.append(market_ticks)
        return all_markets

    def generate_polymarket_walk(self, btc_walk_one_market: List[float]) -> List[float]:
        """Synthetic Polymarket YES probability that reacts to BTC direction.

        Starts near 0.50. Drifts toward 0/1 as the market resolves, weighted
        by the BTC move so far and time remaining. This mimics a real
        prediction market consolidating around the eventual outcome as
        information arrives.
        """
        ticks = len(btc_walk_one_market)
        start_btc = btc_walk_one_market[0]
        poly = []
        for t in range(ticks):
            time_progress = t / ticks            # 0 → 1
            decisiveness = time_progress ** 0.7  # accelerates toward end
            current_btc = btc_walk_one_market[t]
            move_pct = (current_btc - start_btc) / start_btc

            # Heuristic mapping: 1% BTC move at end of market ≈ ±0.40 from 0.50
            implied_prob = 0.5 + 40.0 * move_pct * decisiveness
            # Tighter noise as market resolves
            noise_sigma = 0.04 * (1.0 - 0.6 * decisiveness)
            noisy = implied_prob + random.gauss(0, noise_sigma)
            noisy = max(0.02, min(0.98, noisy))
            poly.append(noisy)
        return poly

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    async def run(self, num_markets: int = 96):
        btc_walks = self.generate_btc_walk(num_markets)
        base_ts = int(datetime.now(timezone.utc).timestamp())

        for m_idx in range(num_markets):
            market_btc = btc_walks[m_idx]
            market_poly = self.generate_polymarket_walk(market_btc)
            market_start_time = datetime.fromtimestamp(
                base_ts + m_idx * MARKET_INTERVAL_SECONDS, tz=timezone.utc
            )

            for tick_idx, (btc_price, poly_price) in enumerate(zip(market_btc, market_poly)):
                tick_time = market_start_time + timedelta(seconds=tick_idx * TICK_CADENCE_SECONDS)
                secs_in = tick_idx * TICK_CADENCE_SECONDS

                # Update rolling history + tick buffer (mirrors bot.py:on_quote_tick)
                self.price_history.append(Decimal(str(poly_price)))
                if len(self.price_history) > 100:
                    self.price_history.pop(0)
                self.tick_buffer.append({"ts": tick_time, "price": Decimal(str(poly_price))})

                # Trade window
                if TRADE_WINDOW_START <= secs_in < TRADE_WINDOW_END and m_idx not in self._traded_market_indices:
                    self._traded_market_indices.add(m_idx)
                    await self._make_decision(
                        poly_price=poly_price,
                        btc_spot=btc_price,
                        market_idx=m_idx,
                        market_start_time=market_start_time,
                        tick_time=tick_time,
                    )

                # Resolve any pending trades whose time has come
                await self._resolve_pending(tick_time, btc_price)

            # One more resolution sweep at end-of-market for any still pending
            end_time = market_start_time + timedelta(seconds=MARKET_INTERVAL_SECONDS + 5)
            await self._resolve_pending(end_time, market_btc[-1])

    # ------------------------------------------------------------------
    # Decision logic (mirrors bot.py:_make_trading_decision)
    # ------------------------------------------------------------------

    async def _make_decision(self, poly_price, btc_spot, market_idx, market_start_time, tick_time):
        self.summary.decisions_made += 1

        if len(self.price_history) < 20:
            return

        # Build metadata with whatever signals can use
        sentiment = 50.0 + random.gauss(0, 18)
        sentiment = max(0.0, min(100.0, sentiment))
        metadata = {
            "spot_price": Decimal(str(btc_spot)),
            "sentiment_score": Decimal(str(sentiment)),
            "tick_buffer": list(self.tick_buffer),
            "deviation": Decimal("0"),
        }

        # Run signal processors. Wrap each in try/except — a crash in one
        # processor shouldn't kill the smoke test.
        current_price_dec = Decimal(str(poly_price))
        signals = []
        for name, proc in [
            ("SpikeDetection", self.spike_detector),
            ("SentimentAnalysis", self.sentiment_processor),
            ("PriceDivergence", self.divergence_processor),
            ("TickVelocity", self.tick_velocity_processor),
        ]:
            try:
                sig = proc.process(
                    current_price=current_price_dec,
                    historical_prices=self.price_history,
                    metadata=metadata,
                )
                if sig:
                    signals.append(sig)
                    self.summary.add_signal_fire(name)
            except Exception as e:
                print(f"[harness] {name} crashed: {e}")

        # Fuse — same gate as bot.py
        fused = self.fusion_engine.fuse_signals(signals, min_signals=1, min_score=40.0)
        if not fused:
            self.summary.skipped_no_signal += 1
            return

        # Trend filter — same as bot.py:920-942
        if poly_price > TREND_UP_THRESHOLD:
            direction = "LONG"
            self.summary.long_count += 1
        elif poly_price < TREND_DOWN_THRESHOLD:
            direction = "SHORT"
            self.summary.short_count += 1
        else:
            self.summary.skipped_neutral += 1
            return

        # Open paper trade
        resolution_time = market_start_time + timedelta(seconds=MARKET_INTERVAL_SECONDS + 5)
        trade = PaperTrade(
            trade_id=f"paper_{int(tick_time.timestamp())}_m{market_idx}",
            timestamp=tick_time,
            direction=direction,
            size_usd=1.0,
            entry_price=float(poly_price),
            entry_btc_spot=float(btc_spot),
            resolution_time=resolution_time,
            signal_score=float(fused.score),
            signal_confidence=float(fused.confidence),
        )
        self.paper_trades.append(trade)
        self.pending_trades.append(trade)
        self.summary.trades_opened += 1

    # ------------------------------------------------------------------
    # Resolver (mirrors bot.py:_resolve_pending_paper_trades)
    # ------------------------------------------------------------------

    async def _resolve_pending(self, now: datetime, current_btc: float):
        due = [t for t in self.pending_trades if now >= t.resolution_time]
        for trade in due:
            p = trade.entry_price
            size = trade.size_usd
            yes_won = current_btc >= trade.entry_btc_spot
            direction = trade.direction.lower()

            if direction == "long":
                payout = size * (1.0 - p) / p if yes_won else -size
            else:
                payout = size * p / (1.0 - p) if (not yes_won) else -size

            fee = size * 4.0 * p * (1.0 - p) * POLYMARKET_FEE_PEAK
            pnl = payout - fee

            trade.exit_btc_spot = current_btc
            trade.pnl = pnl
            trade.fee = fee
            trade.outcome = "WIN" if pnl > 0 else "LOSS"

            self.summary.trades_resolved += 1
            self.summary.total_pnl += pnl
            self.summary.total_fees += fee
            if pnl > 0:
                self.summary.wins += 1
            else:
                self.summary.losses += 1

        self.pending_trades = [t for t in self.pending_trades if t not in due]


# ------------------------------------------------------------------
# Reporting
# ------------------------------------------------------------------

def print_summary(s: RegimeSummary):
    print(f"\n{'=' * 72}")
    print(f"REGIME: {s.regime.upper()}")
    print(f"{'=' * 72}")
    print(f"Decisions evaluated     : {s.decisions_made}")
    print(f"  Skipped (no signal)   : {s.skipped_no_signal}")
    print(f"  Skipped (neutral 0.4-0.6): {s.skipped_neutral}")
    print(f"  Trades opened         : {s.trades_opened}")
    print(f"    Long (YES)          : {s.long_count}")
    print(f"    Short (NO)          : {s.short_count}")
    print(f"Trades resolved         : {s.trades_resolved}")
    if s.trades_resolved:
        win_rate = s.wins / s.trades_resolved * 100
        avg_pnl = s.total_pnl / s.trades_resolved
        print(f"  Wins                  : {s.wins} ({win_rate:.1f}%)")
        print(f"  Losses                : {s.losses}")
        print(f"Total P&L               : ${s.total_pnl:+.4f}")
        print(f"Total fees              : ${s.total_fees:.4f}")
        print(f"Avg P&L per trade       : ${avg_pnl:+.4f}")
        # ROI on capital deployed
        capital_deployed = s.trades_resolved * 1.0
        roi = s.total_pnl / capital_deployed * 100 if capital_deployed > 0 else 0.0
        print(f"ROI on capital deployed : {roi:+.2f}%")
    if s.signal_fires:
        print(f"Signal fire counts:")
        for src, count in sorted(s.signal_fires.items(), key=lambda x: -x[1]):
            print(f"  {src:24s}: {count}")
    print(f"{'=' * 72}")


async def main():
    print("=" * 72)
    print("POLYMARKET BTC 15-MIN BOT — SMOKE TEST HARNESS")
    print("Synthetic data only. NOT a backtest. Operational smoke test.")
    print("=" * 72)
    summaries = []
    for regime in ["quiet", "trending", "volatile"]:
        harness = SmokeTestHarness(regime, seed=42)
        # 96 markets = 24 hours of simulated time, runs in seconds
        await harness.run(num_markets=96)
        summaries.append(harness.summary)
        print_summary(harness.summary)

    # Cross-regime comparison
    print(f"\n{'=' * 72}")
    print("CROSS-REGIME COMPARISON")
    print(f"{'=' * 72}")
    print(f"{'Metric':<24} {'quiet':>12} {'trending':>12} {'volatile':>12}")
    print("-" * 64)
    print(f"{'Trades opened':<24} {summaries[0].trades_opened:>12} {summaries[1].trades_opened:>12} {summaries[2].trades_opened:>12}")
    print(f"{'Trades resolved':<24} {summaries[0].trades_resolved:>12} {summaries[1].trades_resolved:>12} {summaries[2].trades_resolved:>12}")
    print(f"{'Win rate':<24} ", end="")
    for s in summaries:
        wr = (s.wins / s.trades_resolved * 100) if s.trades_resolved else 0.0
        print(f"{wr:>11.1f}%", end=" ")
    print()
    print(f"{'Total P&L':<24} ", end="")
    for s in summaries:
        print(f"${s.total_pnl:>+10.4f}", end=" ")
    print()
    print(f"{'Total fees':<24} ", end="")
    for s in summaries:
        print(f"${s.total_fees:>10.4f}", end=" ")
    print()
    print(f"{'=' * 72}\n")


if __name__ == "__main__":
    asyncio.run(main())
