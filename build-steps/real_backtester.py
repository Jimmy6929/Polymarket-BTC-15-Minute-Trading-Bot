"""Real-data backtester for the Polymarket BTC 15-min bot.

Consumes the three datasets pulled by fetch_data.py:
  - polymarket_btc_15m.json — market metadata + per-market YES price history
  - coinbase_btc_1min.csv   — BTC spot at every minute
  - fear_greed.json         — daily F&G index

Replays the bot's decision logic against real data: for each resolved
market, snapshots inputs at minute 13 (the bot's trade window), runs the
actual signal processors, applies the trend filter, computes P&L using
the market's REAL feeSchedule and Chainlink resolution.

NOT a full Deflated Sharpe / purged k-fold backtest. This is a forward
replay on 14 days of recent history — a real-data smoke test. Reports
Wilson 95% CI on win rate and bootstrap 95% CI on per-trade EV so the
uncertainty is visible.
"""
import csv
import json
import math
import random
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Silence loguru's INFO/DEBUG spam from signal processors
from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")

from core.strategy_brain.signal_processors.spike_detector import SpikeDetectionProcessor
from core.strategy_brain.signal_processors.sentiment_processor import SentimentProcessor
from core.strategy_brain.signal_processors.divergence_processor import PriceDivergenceProcessor
from core.strategy_brain.signal_processors.tick_velocity_processor import TickVelocityProcessor
from core.strategy_brain.fusion_engine.signal_fusion import SignalFusionEngine

DATA_DIR = Path(__file__).resolve().parent / "data"
TRADE_WINDOW_START = 780     # min 13
TRADE_WINDOW_END = 840       # min 14
TREND_UP = 0.60
TREND_DOWN = 0.40
PRICE_HISTORY_MAX = 100


@dataclass
class TradeRecord:
    slug: str
    market_id: str
    entry_ts: int
    direction: str            # LONG (YES) or SHORT (NO)
    entry_price: float
    entry_btc_spot: float
    final_btc_price: float
    btc_price_to_beat: float
    yes_won: bool
    fee_rate: float
    payout: float
    fee: float
    pnl: float
    outcome: str
    signal_score: float
    signal_confidence: float
    signal_count: int
    fee_regime: str


def load_coinbase_btc() -> Dict[int, Dict[str, float]]:
    """Index Coinbase 1-min candles by unix minute."""
    path = DATA_DIR / "coinbase_btc_1min.csv"
    by_minute = {}
    with path.open() as f:
        for row in csv.DictReader(f):
            ts = int(row["ts_unix"])
            by_minute[ts // 60] = {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
    return by_minute


def load_fear_greed() -> Dict[str, int]:
    """Index F&G by UTC date string."""
    path = DATA_DIR / "fear_greed.json"
    by_date = {}
    with path.open() as f:
        for row in json.load(f):
            ts = int(row["timestamp"])
            date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            by_date[date_str] = int(row["value"])
    return by_date


def load_markets() -> List[Dict[str, Any]]:
    path = DATA_DIR / "polymarket_btc_15m.json"
    with path.open() as f:
        data = json.load(f)
    data.sort(key=lambda m: m["slug_ts"])
    return data


def find_entry_tick(ticks: List[Dict[str, Any]], window_start: int, window_end: int) -> Optional[Dict[str, Any]]:
    """Latest tick within [window_start, window_end). None if no tick."""
    in_window = [t for t in ticks if window_start <= t["t"] < window_end]
    return in_window[-1] if in_window else None


def compute_fee(fee_schedule: Optional[Dict[str, Any]], p: float, size: float) -> float:
    """Real Polymarket fee. fee = size × rate × (p·(1-p))^exponent.

    Returns 0 when the schedule is absent — matches the pre-Nov-2025 era
    when Polymarket 15-min markets had no taker fees. Using a fallback
    approximation here would mis-attribute fees that never existed.
    """
    if not fee_schedule:
        return 0.0
    rate = fee_schedule.get("rate", 0.0)
    exponent = fee_schedule.get("exponent", 1)
    fee_rate = rate * (p * (1.0 - p)) ** exponent
    return size * fee_rate


def fee_regime_for(fee_schedule: Optional[Dict[str, Any]]) -> str:
    """Classify a market into a fee-rate regime for segmentation."""
    if not fee_schedule:
        return "no-fee"
    rate = fee_schedule.get("rate", 0.0)
    if rate <= 0.01:
        return "no-fee"
    if rate < 0.15:
        return f"current ({rate})"
    return f"high ({rate})"


SPLITS_DIR = DATA_DIR / "splits"


def load_split_slugs(split: str) -> Optional[set]:
    """Load the set of allowed slugs for a given split. Returns None for 'both'."""
    if split == "both":
        return None
    path = SPLITS_DIR / f"{split}_market_ids.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Split file not found: {path}. Run `--build-splits` first."
        )
    with path.open() as f:
        data = json.load(f)
    return set(data["slugs"])


class RealDataBacktester:
    def __init__(self, split: str = "both", out_path: Optional[Path] = None):
        print(f"[bt] loading data… (split={split})")
        self.split = split
        self.out_path = out_path or (DATA_DIR / "backtest_trades.csv")
        self.btc_by_minute = load_coinbase_btc()
        self.fng_by_date = load_fear_greed()
        all_markets = load_markets()

        allowed = load_split_slugs(split)
        if allowed is None:
            self.markets = all_markets
        else:
            self.markets = [m for m in all_markets if m["slug"] in allowed]
            print(f"[bt]   filtered from {len(all_markets)} → {len(self.markets)} markets in '{split}' split")

        print(f"[bt]   coinbase: {len(self.btc_by_minute)} 1-min candles")
        print(f"[bt]   F&G:      {len(self.fng_by_date)} daily values")
        print(f"[bt]   markets:  {len(self.markets)} resolved BTC 15-min markets")

        # Same processor config as bot.py
        self.spike_detector = SpikeDetectionProcessor(spike_threshold=0.05, lookback_periods=20)
        self.sentiment_processor = SentimentProcessor(extreme_fear_threshold=25, extreme_greed_threshold=75)
        self.divergence_processor = PriceDivergenceProcessor(divergence_threshold=0.05)
        self.tick_velocity_processor = TickVelocityProcessor(velocity_threshold_60s=0.015, velocity_threshold_30s=0.010)

        # Renormalized 4-processor weights (orderbook + deribit not run here)
        self.fusion_engine = SignalFusionEngine()
        self.fusion_engine.set_weight("SpikeDetection", 0.22)
        self.fusion_engine.set_weight("TickVelocity", 0.45)
        self.fusion_engine.set_weight("PriceDivergence", 0.24)
        self.fusion_engine.set_weight("SentimentAnalysis", 0.09)

        # Rolling state — mirrors bot.py: deque is NOT cleared on market switch
        self.price_history: deque = deque(maxlen=PRICE_HISTORY_MAX)
        self.tick_buffer: deque = deque(maxlen=500)

        self.trades: List[TradeRecord] = []
        self.skipped_no_price = 0
        self.skipped_no_spot = 0
        self.skipped_no_signal = 0
        self.skipped_neutral = 0
        self.skipped_short_history = 0
        self.processed = 0
        self.signal_fires: Dict[str, int] = {}

    def run(self):
        print(f"[bt] running backtest over {len(self.markets)} markets…")
        for m in self.markets:
            self.processed += 1
            ticks = m.get("yes_price_history") or []
            # Ingest ticks into rolling history (price_history and tick_buffer)
            for tick in ticks:
                p_dec = Decimal(str(tick["p"]))
                self.price_history.append(p_dec)
                ts = datetime.fromtimestamp(tick["t"], tz=timezone.utc)
                self.tick_buffer.append({"ts": ts, "price": p_dec})

            slug_ts = m["slug_ts"]
            entry_tick = find_entry_tick(ticks, slug_ts + TRADE_WINDOW_START, slug_ts + TRADE_WINDOW_END)
            if entry_tick is None:
                self.skipped_no_price += 1
                continue

            entry_p = entry_tick["p"]
            entry_ts = entry_tick["t"]
            entry_minute = entry_ts // 60
            spot = self.btc_by_minute.get(entry_minute) or self.btc_by_minute.get(entry_minute - 1)
            if not spot:
                self.skipped_no_spot += 1
                continue
            entry_btc = spot["close"]

            date_str = datetime.fromtimestamp(entry_ts, tz=timezone.utc).strftime("%Y-%m-%d")
            sentiment = self.fng_by_date.get(date_str, 50)

            if len(self.price_history) < 20:
                self.skipped_short_history += 1
                continue

            self._make_decision(m, entry_p, entry_ts, entry_btc, sentiment)

        self._print_summary()

    def _make_decision(self, market, entry_p, entry_ts, entry_btc, sentiment):
        metadata = {
            "spot_price": Decimal(str(entry_btc)),
            "sentiment_score": Decimal(str(sentiment)),
            "tick_buffer": list(self.tick_buffer),
            "deviation": Decimal("0"),
        }
        current_price = Decimal(str(entry_p))
        history = list(self.price_history)

        signals = []
        for name, proc in [
            ("SpikeDetection", self.spike_detector),
            ("SentimentAnalysis", self.sentiment_processor),
            ("PriceDivergence", self.divergence_processor),
            ("TickVelocity", self.tick_velocity_processor),
        ]:
            try:
                sig = proc.process(current_price=current_price, historical_prices=history, metadata=metadata)
                if sig:
                    signals.append(sig)
                    self.signal_fires[name] = self.signal_fires.get(name, 0) + 1
            except Exception as e:
                print(f"[bt] {name} crashed on {market['slug']}: {e}")

        fused = self.fusion_engine.fuse_signals(signals, min_signals=1, min_score=40.0)
        if not fused:
            self.skipped_no_signal += 1
            return

        if entry_p > TREND_UP:
            direction = "LONG"
        elif entry_p < TREND_DOWN:
            direction = "SHORT"
        else:
            self.skipped_neutral += 1
            return

        yes_won = bool(market["yes_won"])
        size = 1.0
        p = entry_p
        if direction == "LONG":
            payout = size * (1.0 - p) / p if yes_won else -size
        else:
            payout = size * p / (1.0 - p) if not yes_won else -size

        fee_schedule = market.get("fee_schedule")
        fee = compute_fee(fee_schedule, p, size)
        pnl = payout - fee

        self.trades.append(TradeRecord(
            slug=market["slug"],
            market_id=str(market.get("market_id", "")),
            entry_ts=entry_ts,
            direction=direction,
            entry_price=entry_p,
            entry_btc_spot=entry_btc,
            final_btc_price=market.get("final_btc_price") or 0.0,
            btc_price_to_beat=market.get("btc_price_to_beat") or 0.0,
            yes_won=yes_won,
            fee_rate=fee / size if size else 0.0,
            payout=payout,
            fee=fee,
            pnl=pnl,
            outcome="WIN" if pnl > 0 else "LOSS",
            signal_score=float(fused.score),
            signal_confidence=float(fused.confidence),
            signal_count=len(signals),
            fee_regime=fee_regime_for(fee_schedule),
        ))

    def _print_summary(self):
        print()
        print("=" * 80)
        print(f"REAL-DATA BACKTEST RESULTS — Polymarket BTC 15-min  [split={self.split}]")
        print("=" * 80)
        print(f"Total markets processed     : {self.processed}")
        print(f"  Skipped (no price@min13)  : {self.skipped_no_price}")
        print(f"  Skipped (no BTC spot)     : {self.skipped_no_spot}")
        print(f"  Skipped (price_history<20): {self.skipped_short_history}")
        print(f"  Skipped (no fused signal) : {self.skipped_no_signal}")
        print(f"  Skipped (neutral 0.4-0.6) : {self.skipped_neutral}")
        print(f"  Trades executed           : {len(self.trades)}")

        if not self.trades:
            print("\nNo trades — cannot compute summary statistics.")
            return

        n = len(self.trades)
        wins = sum(1 for t in self.trades if t.outcome == "WIN")
        losses = n - wins
        n_long = sum(1 for t in self.trades if t.direction == "LONG")
        n_short = n - n_long

        # Wilson 95% CI on win rate
        p_hat = wins / n
        z = 1.96
        denom = 1 + z**2 / n
        center = (p_hat + z**2 / (2 * n)) / denom
        margin = z * math.sqrt(p_hat * (1 - p_hat) / n + z**2 / (4 * n**2)) / denom
        wlo = max(0.0, center - margin)
        whi = min(1.0, center + margin)

        total_pnl = sum(t.pnl for t in self.trades)
        total_fees = sum(t.fee for t in self.trades)
        capital = n * 1.0
        roi = total_pnl / capital * 100.0
        avg_pnl = total_pnl / n
        avg_fee = total_fees / n

        # Bootstrap 95% CI on per-trade mean P&L (10k resamples)
        random.seed(42)
        pnls = [t.pnl for t in self.trades]
        boots = sorted(sum(random.choice(pnls) for _ in range(n)) / n for _ in range(10000))
        boot_lo = boots[250]
        boot_hi = boots[9750]

        print(f"\nDirection split             : LONG (YES) {n_long} ({n_long/n*100:.0f}%) | SHORT (NO) {n_short}")
        print(f"Wins / losses               : {wins} / {losses}")
        print(f"Win rate                    : {p_hat*100:.1f}%  (Wilson 95% CI: {wlo*100:.1f}% – {whi*100:.1f}%)")
        print(f"Total P&L                   : ${total_pnl:+.4f}  on ${capital:.0f} cumulative capital deployed")
        print(f"ROI                         : {roi:+.2f}%")
        print(f"Avg P&L per trade           : ${avg_pnl:+.4f}  (bootstrap 95% CI: ${boot_lo:+.4f} – ${boot_hi:+.4f})")
        print(f"Avg fee per trade           : ${avg_fee:.4f}  ({avg_fee/1.0*100:.2f}% on $1 size)")
        print(f"\nSignal fires:")
        for src, count in sorted(self.signal_fires.items(), key=lambda x: -x[1]):
            print(f"  {src:24s}: {count}")

        # --- Per-regime breakdown ---
        regimes: Dict[str, List[TradeRecord]] = {}
        for t in self.trades:
            regimes.setdefault(t.fee_regime, []).append(t)
        if len(regimes) > 1:
            print(f"\nPer-fee-regime breakdown:")
            print(f"  {'regime':<18} {'n':>6} {'win%':>7} {'avg_pnl':>10} {'total_pnl':>12} {'avg_fee':>10}")
            for regime in sorted(regimes):
                ts = regimes[regime]
                wins_r = sum(1 for t in ts if t.outcome == "WIN")
                wr_r = wins_r / len(ts) * 100
                avg_p = sum(t.pnl for t in ts) / len(ts)
                total_p = sum(t.pnl for t in ts)
                avg_f = sum(t.fee for t in ts) / len(ts)
                print(f"  {regime:<18} {len(ts):>6} {wr_r:>6.1f}% {avg_p:>+10.4f} {total_p:>+12.4f} {avg_f:>10.4f}")

        # --- Monthly walk-forward ---
        monthly: Dict[str, List[TradeRecord]] = {}
        for t in self.trades:
            month_key = datetime.fromtimestamp(t.entry_ts, tz=timezone.utc).strftime("%Y-%m")
            monthly.setdefault(month_key, []).append(t)
        if len(monthly) > 1:
            print(f"\nMonthly walk-forward:")
            print(f"  {'month':<10} {'n':>6} {'win%':>7} {'avg_pnl':>10} {'total_pnl':>12} {'regime':>18}")
            for month in sorted(monthly):
                ts = monthly[month]
                wins_m = sum(1 for t in ts if t.outcome == "WIN")
                wr_m = wins_m / len(ts) * 100
                avg_p = sum(t.pnl for t in ts) / len(ts)
                total_p = sum(t.pnl for t in ts)
                regimes_in_month = sorted(set(t.fee_regime for t in ts))
                regime_str = ",".join(regimes_in_month) if len(regimes_in_month) <= 1 else "mixed"
                print(f"  {month:<10} {len(ts):>6} {wr_m:>6.1f}% {avg_p:>+10.4f} {total_p:>+12.4f} {regime_str:>18}")

        print("=" * 80)

        out_csv = self.out_path
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with out_csv.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["slug", "market_id", "entry_ts", "direction", "entry_price",
                        "entry_btc_spot", "final_btc_price", "btc_price_to_beat", "yes_won",
                        "fee_rate", "payout", "fee", "pnl", "outcome",
                        "signal_score", "signal_confidence", "signal_count", "fee_regime"])
            for t in self.trades:
                w.writerow([t.slug, t.market_id, t.entry_ts, t.direction, t.entry_price,
                            t.entry_btc_spot, t.final_btc_price, t.btc_price_to_beat, t.yes_won,
                            t.fee_rate, t.payout, t.fee, t.pnl, t.outcome,
                            t.signal_score, t.signal_confidence, t.signal_count, t.fee_regime])
        print(f"\nTrade log: {out_csv}")


def build_splits(holdout_days: int = 30, embargo_hours: int = 24) -> Dict[str, Any]:
    """Generate time-based train/holdout split files.

    Holdout = the most recent `holdout_days` of the dataset.
    Embargo = `embargo_hours` straddling the boundary, dropped from both splits.
    Train  = everything before the embargo.

    Writes:
      data/splits/train_market_ids.json
      data/splits/holdout_market_ids.json

    Both files have shape: {"slugs": [...], "boundary_ts": int, "embargo_seconds": int, "generated_at": iso}
    """
    SPLITS_DIR.mkdir(parents=True, exist_ok=True)
    markets = load_markets()
    if not markets:
        raise RuntimeError("No markets loaded — run fetch_data.py first")

    max_ts = max(m["slug_ts"] for m in markets)
    min_ts = min(m["slug_ts"] for m in markets)
    holdout_start = max_ts - holdout_days * 86400
    embargo_seconds = embargo_hours * 3600
    embargo_end = holdout_start             # everything in [embargo_end - embargo_seconds, embargo_end) is dropped
    train_cutoff = embargo_end - embargo_seconds

    train_slugs = [m["slug"] for m in markets if m["slug_ts"] < train_cutoff]
    holdout_slugs = [m["slug"] for m in markets if m["slug_ts"] >= holdout_start]
    dropped = len(markets) - len(train_slugs) - len(holdout_slugs)

    now_iso = datetime.now(timezone.utc).isoformat()
    train_payload = {
        "slugs": sorted(train_slugs),
        "boundary_ts": train_cutoff,
        "embargo_seconds": embargo_seconds,
        "generated_at": now_iso,
        "n": len(train_slugs),
    }
    holdout_payload = {
        "slugs": sorted(holdout_slugs),
        "boundary_ts": holdout_start,
        "embargo_seconds": embargo_seconds,
        "generated_at": now_iso,
        "n": len(holdout_slugs),
    }
    with (SPLITS_DIR / "train_market_ids.json").open("w") as f:
        json.dump(train_payload, f, indent=2)
    with (SPLITS_DIR / "holdout_market_ids.json").open("w") as f:
        json.dump(holdout_payload, f, indent=2)

    summary = {
        "total_markets": len(markets),
        "min_ts": min_ts,
        "max_ts": max_ts,
        "min_date": datetime.fromtimestamp(min_ts, tz=timezone.utc).isoformat(),
        "max_date": datetime.fromtimestamp(max_ts, tz=timezone.utc).isoformat(),
        "train_cutoff_ts": train_cutoff,
        "train_cutoff_date": datetime.fromtimestamp(train_cutoff, tz=timezone.utc).isoformat(),
        "holdout_start_ts": holdout_start,
        "holdout_start_date": datetime.fromtimestamp(holdout_start, tz=timezone.utc).isoformat(),
        "train_n": len(train_slugs),
        "holdout_n": len(holdout_slugs),
        "dropped_in_embargo": dropped,
    }
    print("[splits] generated:")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return summary


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Real-data backtester")
    parser.add_argument("--build-splits", action="store_true",
                        help="Generate data/splits/{train,holdout}_market_ids.json from current data and exit")
    parser.add_argument("--holdout-days", type=int, default=30,
                        help="Holdout window length in days (only used with --build-splits)")
    parser.add_argument("--embargo-hours", type=int, default=24,
                        help="Embargo straddling the train/holdout boundary (only used with --build-splits)")
    parser.add_argument("--split", choices=["train", "holdout", "both"], default="both",
                        help="Which split to backtest. 'both' = all markets (default).")
    parser.add_argument("--out", type=str, default=None,
                        help="Where to write the per-trade CSV. Default: data/backtest_trades.csv")
    args = parser.parse_args()

    if args.build_splits:
        build_splits(holdout_days=args.holdout_days, embargo_hours=args.embargo_hours)
        return

    out = Path(args.out) if args.out else None
    RealDataBacktester(split=args.split, out_path=out).run()


if __name__ == "__main__":
    main()
