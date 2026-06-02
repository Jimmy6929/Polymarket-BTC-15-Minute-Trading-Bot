import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
import math
from decimal import Decimal
import time
from dataclasses import dataclass
from typing import List, Optional, Dict
import random

# Add project to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))


try:
    from patch_gamma_markets import apply_gamma_markets_patch, verify_patch
    patch_applied = apply_gamma_markets_patch()
    if patch_applied:
        verify_patch()
    else:
        print("ERROR: Failed to apply gamma_market patch")
        sys.exit(1)
except ImportError as e:
    print(f"ERROR: Could not import patch module: {e}")
    print("Make sure patch_gamma_markets.py is in the same directory")
    sys.exit(1)

# Now import Nautilus
from nautilus_trader.config import (
    InstrumentProviderConfig,
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LiveRiskEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.adapters.polymarket import (
    PolymarketDataClientConfig,
    PolymarketExecClientConfig,
)
from nautilus_trader.adapters.polymarket.factories import (
    PolymarketLiveDataClientFactory,
    PolymarketLiveExecClientFactory,
)
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.identifiers import InstrumentId, ClientOrderId
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.data import QuoteTick

from dotenv import load_dotenv
from loguru import logger
import redis

# Import our phases
from core.strategy_brain.signal_processors.spike_detector import SpikeDetectionProcessor
from core.strategy_brain.signal_processors.sentiment_processor import SentimentProcessor
from core.strategy_brain.signal_processors.divergence_processor import PriceDivergenceProcessor
from core.strategy_brain.signal_processors.orderbook_processor import OrderBookImbalanceProcessor
from core.strategy_brain.signal_processors.tick_velocity_processor import TickVelocityProcessor
from core.strategy_brain.signal_processors.deribit_pcr_processor import DeribitPCRProcessor
from core.strategy_brain.fusion_engine.signal_fusion import get_fusion_engine
from execution.risk_engine import get_risk_engine
from monitoring.performance_tracker import get_performance_tracker
from monitoring.grafana_exporter import get_grafana_exporter
from feedback.learning_engine import get_learning_engine
load_dotenv()
from patch_market_orders import apply_market_order_patch
patch_applied = apply_market_order_patch()
if patch_applied:
    logger.info("Market order patch applied successfully")
else:
    logger.warning("Market order patch failed - orders may be rejected")


# =============================================================================
# CONSTANTS
# =============================================================================
QUOTE_STABILITY_REQUIRED = 3      # Need only 3 valid ticks to be stable (faster startup)
QUOTE_MIN_SPREAD = 0.001          # Both bid AND ask must be at least this
MARKET_INTERVAL_SECONDS = 900     # 15-minute markets

# Polymarket taker fee approximation. Real schedule peaks ~1.56% at p=0.50 and
# tapers toward 0% at extremes. Modeled as 4·p·(1-p)·peak — exact at p=0.5,
# matches the qualitative shape elsewhere. Source: Polymarket Q4-2025 fee curve.
POLYMARKET_FEE_PEAK = 0.0156


@dataclass
class PaperTrade:
    """Track paper/simulation trades.

    A trade starts in PENDING state at entry. The resolver settles it when
    the 15-min market closes by comparing entry vs exit Coinbase BTC spot —
    no RNG, no fabricated movement. Win/loss flows from real BTC direction;
    P&L from the binary payout formula minus the Polymarket fee approximation.
    """
    timestamp: datetime
    direction: str
    size_usd: float
    price: float                          # mid / decision price at minute 13 (Polymarket YES prob)
    signal_score: float
    signal_confidence: float
    outcome: str = "PENDING"
    # Realistic-fill fields — what you'd ACTUALLY transact at (cross the spread)
    fill_price: Optional[float] = None    # ask for LONG, bid for SHORT; P&L is computed on this
    half_spread: Optional[float] = None   # (ask - bid) / 2 paid at entry
    # Market identity — needed to fetch the ACTUAL Polymarket settled outcome
    slug: Optional[str] = None
    condition_id: Optional[str] = None
    yes_token_id: Optional[str] = None
    # Resolution fields — populated when the market settles
    trade_id: Optional[str] = None
    entry_btc_spot: Optional[float] = None
    resolution_time: Optional[datetime] = None
    exit_btc_spot: Optional[float] = None
    exit_price: Optional[float] = None    # 1.0 or 0.0 — binary outcome
    resolution_source: Optional[str] = None  # "gamma_actual" once settled via Gamma
    pnl: Optional[float] = None
    fee: Optional[float] = None

    def to_dict(self):
        return {
            'timestamp': self.timestamp.isoformat(),
            'direction': self.direction,
            'size_usd': self.size_usd,
            'price': self.price,
            'signal_score': self.signal_score,
            'signal_confidence': self.signal_confidence,
            'outcome': self.outcome,
            'fill_price': self.fill_price,
            'half_spread': self.half_spread,
            'slug': self.slug,
            'condition_id': self.condition_id,
            'yes_token_id': self.yes_token_id,
            'trade_id': self.trade_id,
            'entry_btc_spot': self.entry_btc_spot,
            'resolution_time': self.resolution_time.isoformat() if self.resolution_time else None,
            'exit_btc_spot': self.exit_btc_spot,
            'exit_price': self.exit_price,
            'resolution_source': self.resolution_source,
            'pnl': self.pnl,
            'fee': self.fee,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PaperTrade":
        """Rebuild a PaperTrade from persisted JSON (for crash-recovery reload)."""
        def _dt(v):
            return datetime.fromisoformat(v) if v else None
        return cls(
            timestamp=_dt(d.get('timestamp')) or datetime.now(timezone.utc),
            direction=d.get('direction', ''),
            size_usd=float(d.get('size_usd', 0.0)),
            price=float(d.get('price', 0.0)),
            signal_score=d.get('signal_score', 0.0),
            signal_confidence=d.get('signal_confidence', 0.0),
            outcome=d.get('outcome', 'PENDING'),
            fill_price=d.get('fill_price'),
            half_spread=d.get('half_spread'),
            slug=d.get('slug'),
            condition_id=d.get('condition_id'),
            yes_token_id=d.get('yes_token_id'),
            trade_id=d.get('trade_id'),
            entry_btc_spot=d.get('entry_btc_spot'),
            resolution_time=_dt(d.get('resolution_time')),
            exit_btc_spot=d.get('exit_btc_spot'),
            exit_price=d.get('exit_price'),
            resolution_source=d.get('resolution_source'),
            pnl=d.get('pnl'),
            fee=d.get('fee'),
        )


def init_redis():
    """Initialize Redis connection for simulation mode control."""
    try:
        redis_client = redis.Redis(
            host=os.getenv('REDIS_HOST', 'localhost'),
            port=int(os.getenv('REDIS_PORT', 6379)),
            db=int(os.getenv('REDIS_DB', 2)),
            decode_responses=True,
            socket_connect_timeout=5,
            socket_keepalive=True
        )
        redis_client.ping()
        logger.info("Redis connection established")
        return redis_client
    except Exception as e:
        logger.warning(f"Redis connection failed: {e}")
        logger.warning("Simulation mode will be static (from .env)")
        return None


class IntegratedBTCStrategy(Strategy):
    """
    Integrated BTC Strategy - FIXED VERSION
    - Subscribes immediately at startup
    - Forces stability for first trade
    - Correct timing for market switching
    """

    def __init__(self, redis_client=None, enable_grafana=True, test_mode=False, data_only=False):
        super().__init__()

        self.bot_start_time = datetime.now(timezone.utc)
        self.restart_after_minutes = 90

        # Nautilus
        self.instrument_id = None
        self.redis_client = redis_client
        # data_only: the node was built with NO execution client (paper-only, no account).
        # It can only read the public market data feed and record paper trades — it is
        # physically incapable of placing an order. A Redis flip to "live" is refused.
        self.data_only = data_only
        self.current_simulation_mode = True if data_only else False

        # Store ALL BTC instruments
        self.all_btc_instruments: List[Dict] = []
        self.current_instrument_index: int = -1
        self.next_switch_time: Optional[datetime] = None

        # Quote-stability tracking
        self._stable_tick_count = 0
        self._market_stable = False
        self._last_instrument_switch = None
        
        # =========================================================================
        # FIX 1: Force first trade by setting last_trade_time to -1
        # =========================================================================
        self.last_trade_time = -1  # Force first trade immediately!
        self._waiting_for_market_open = False  # True when waiting for a future market to open
        self._last_bid_ask = None  # (bid_decimal, ask_decimal) from last tick, for liquidity checks

        # Tick buffer: rolling 90s of ticks for TickVelocityProcessor
        from collections import deque
        self._tick_buffer: deque = deque(maxlen=500)  # ~500 ticks = well over 90s

        # YES token id for the current market (set in _load_all_btc_instruments)
        self._yes_token_id: Optional[str] = None

        # Phase 4: Signal Processors
        self.spike_detector = SpikeDetectionProcessor(
            spike_threshold=0.05,       # FIXED: was 0.15 (too high for probabilities)
            lookback_periods=20,
        )
        self.sentiment_processor = SentimentProcessor(
            extreme_fear_threshold=25,
            extreme_greed_threshold=75,
        )
        self.divergence_processor = PriceDivergenceProcessor(
            divergence_threshold=0.05,
        )
        self.orderbook_processor = OrderBookImbalanceProcessor(
            imbalance_threshold=0.30,   # 30% skew to signal
            min_book_volume=50.0,       # ignore illiquid books
        )
        self.tick_velocity_processor = TickVelocityProcessor(
            velocity_threshold_60s=0.015,  # 1.5% move in 60s
            velocity_threshold_30s=0.010,  # 1.0% move in 30s
        )
        self.deribit_pcr_processor = DeribitPCRProcessor(
            bullish_pcr_threshold=1.20,
            bearish_pcr_threshold=0.70,
            max_days_to_expiry=2,
            cache_seconds=300,          # refresh every 5 min
        )

        # Phase 4: Signal Fusion — update weights for 6 processors
        self.fusion_engine = get_fusion_engine()
        # Rebalanced weights (must sum ≤ 1.0; higher = more influence)
        self.fusion_engine.set_weight("OrderBookImbalance", 0.30)  # best real-time signal
        self.fusion_engine.set_weight("TickVelocity",       0.25)  # fast poly momentum
        self.fusion_engine.set_weight("PriceDivergence",    0.18)  # spot momentum
        self.fusion_engine.set_weight("SpikeDetection",     0.12)  # mean reversion
        self.fusion_engine.set_weight("DeribitPCR",         0.10)  # institutional sentiment
        self.fusion_engine.set_weight("SentimentAnalysis",  0.05)  # daily F&G (weak)

        # Phase 5: Risk Management
        self.risk_engine = get_risk_engine()

        # Phase 6: Performance Tracking
        self.performance_tracker = get_performance_tracker()

        # Phase 7: Learning Engine
        self.learning_engine = get_learning_engine()

        # Phase 6: Grafana (optional)
        if enable_grafana:
            self.grafana_exporter = get_grafana_exporter()
        else:
            self.grafana_exporter = None

        # Price history
        self.price_history = []
        self.max_history = 100

        # Paper trading tracker
        self.paper_trades: List[PaperTrade] = []
        # Pending paper trades awaiting 15-min market resolution.
        # Resolved by _resolve_pending_paper_trades() in the timer loop.
        self._pending_paper_trades: List[PaperTrade] = []
        # Crash recovery: reload any prior paper trades and re-queue PENDING ones.
        self._load_paper_trades()

        # --- Research decision log (event-sourced, append-only JSONL) ---------
        # Every minute-13 decision (executed AND skipped) is captured with its
        # point-in-time feature vector + per-processor signals, then a separate
        # `resolution` event records the realized outcome + counterfactual P&L
        # (what each choice WOULD have earned), so the trades the guards block
        # are studyable, not lost. The blotter (paper_trades.json) is a view of
        # the executed subset; this is the primary research store.
        self._decisions_log_path = 'decisions.jsonl'
        # decision_id -> pending entry awaiting counterfactual resolution.
        self._pending_decisions: dict = {}
        # window_key (market_start_ts, sub_interval) -> last recorded action,
        # so the retry-on-block storm collapses to one decision record per window
        # ('opened' is terminal and upgrades a prior 'skipped').
        self._captured_decision_windows: dict = {}
        # Stashed by on_quote_tick just before dispatching a decision, so the
        # decision coroutine (which only receives a price) knows which window it
        # is deciding. None until the first in-window tick.
        self._decision_window_ctx: Optional[dict] = None

        self.test_mode = test_mode

        if test_mode:
            logger.info("=" * 80)
            logger.info("  TEST MODE ACTIVE - Trading every minute!")
            logger.info("=" * 80)

        logger.info("=" * 80)
        logger.info("INTEGRATED BTC STRATEGY INITIALIZED - FIXED VERSION")
        logger.info("  Phase 4: Signal processors ready")
        logger.info("  Phase 5: Risk engine ready")
        logger.info("  Phase 6: Performance tracking ready")
        logger.info("  Phase 7: Learning engine ready")
        logger.info("  $1 per trade maximum")
        logger.info("=" * 80)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _seconds_to_next_15min_boundary(self) -> float:
        """Return seconds until the next 15-minute UTC boundary."""
        now_ts = datetime.now(timezone.utc).timestamp()
        next_boundary = (math.floor(now_ts / MARKET_INTERVAL_SECONDS) + 1) * MARKET_INTERVAL_SECONDS
        return next_boundary - now_ts

    def _is_quote_valid(self, bid, ask) -> bool:
        """Return True only when BOTH bid and ask are present and make sense."""
        if bid is None or ask is None:
            return False
        try:
            b = float(bid)
            a = float(ask)
        except (TypeError, ValueError):
            return False
        if b < QUOTE_MIN_SPREAD or a < QUOTE_MIN_SPREAD:
            return False
        if b > 0.999 or a > 0.999:
            return False
        return True

    def _reset_stability(self, reason: str = ""):
        """Mark the market as unstable and reset the counter."""
        if self._market_stable:
            logger.warning(f"Market stability RESET{' – ' + reason if reason else ''}")
        self._market_stable = False
        self._stable_tick_count = 0

    # ------------------------------------------------------------------
    # Redis
    # ------------------------------------------------------------------

    async def check_simulation_mode(self) -> bool:
        """Check Redis for current simulation mode."""
        # Data-only paper mode: there is no execution client, so live trading is
        # impossible. Refuse any Redis flip to live and stay in simulation for the
        # node's lifetime. Going live requires an explicit restart in --live mode.
        if self.data_only:
            if self.redis_client:
                try:
                    if self.redis_client.get('btc_trading:simulation_mode') == '0':
                        logger.warning(
                            "Redis requested LIVE TRADING but this node is data-only "
                            "(no execution client) — REFUSING. Staying in simulation. "
                            "Restart with --live to trade for real."
                        )
                except Exception as e:
                    logger.warning(f"Failed to check Redis simulation mode: {e}")
            return True
        if not self.redis_client:
            return self.current_simulation_mode
        try:
            sim_mode = self.redis_client.get('btc_trading:simulation_mode')
            if sim_mode is not None:
                redis_simulation = sim_mode == '1'
                if redis_simulation != self.current_simulation_mode:
                    self.current_simulation_mode = redis_simulation
                    mode_text = "SIMULATION" if redis_simulation else "LIVE TRADING"
                    logger.warning(f"Trading mode changed to: {mode_text}")
                    if not redis_simulation:
                        logger.warning("LIVE TRADING ACTIVE - Real money at risk!")
                return redis_simulation
        except Exception as e:
            logger.warning(f"Failed to check Redis simulation mode: {e}")
        return self.current_simulation_mode

    # ------------------------------------------------------------------
    # Strategy lifecycle
    # ------------------------------------------------------------------

    def on_start(self):
        """Called when strategy starts - LOAD ALL MARKETS AND SUBSCRIBE IMMEDIATELY"""
        logger.info("=" * 80)
        logger.info("INTEGRATED BTC STRATEGY STARTED - FIXED VERSION")
        logger.info("=" * 80)

        # =========================================================================
        # FIX 2: Load ALL BTC instruments at startup
        # =========================================================================
        self._load_all_btc_instruments()

        # =========================================================================
        # FIX 3: Force subscribe to current market IMMEDIATELY
        # =========================================================================
        if self.instrument_id:
            self.subscribe_quote_ticks(self.instrument_id)
            logger.info(f"✓ SUBSCRIBED to market: {self.instrument_id}")
            
            # Try to get current price from cache
            try:
                quote = self.cache.quote_tick(self.instrument_id)
                if quote and quote.bid_price and quote.ask_price:
                    current_price = (quote.bid_price + quote.ask_price) / 2
                    self.price_history.append(current_price)
                    logger.info(f"✓ Initial price: ${float(current_price):.4f}")
            except Exception as e:
                logger.debug(f"No initial price yet: {e}")

        # Generate synthetic history if needed
        if len(self.price_history) < 20:
            self._generate_synthetic_history(target_count=20, existing_count=len(self.price_history))

        # =========================================================================
        # FIX 4: Start the timer loop (but don't rely on it for trading)
        # =========================================================================
        self.run_in_executor(self._start_timer_loop)

        if self.grafana_exporter:
            import threading
            threading.Thread(target=self._start_grafana_sync, daemon=True).start()

        logger.info("=" * 80)
        logger.info("Strategy active - will trade every 15 minutes")
        logger.info(f"Price history: {len(self.price_history)} points")
        if len(self.price_history) >= 20:
            logger.info("✓ READY TO TRADE NOW!")
        else:
            logger.warning(f"⚠ Need more history ({len(self.price_history)}/20)")
        logger.info("=" * 80)

    def _generate_synthetic_history(self, target_count: int = 20, existing_count: int = 0):
        """Generate synthetic price history for testing"""
        if self.price_history:
            base_price = self.price_history[-1]
        else:
            base_price = Decimal("0.5")
        needed = target_count - existing_count
        if needed <= 0:
            return
        for _ in range(needed):
            change = Decimal(str(random.uniform(-0.03, 0.03)))
            new_price = base_price * (Decimal("1.0") + change)
            new_price = max(Decimal("0.01"), min(Decimal("0.99"), new_price))
            self.price_history.append(new_price)
            base_price = new_price

    # ------------------------------------------------------------------
    # Load all BTC instruments at once
    # ------------------------------------------------------------------

    def _load_all_btc_instruments(self):
        """Load ALL BTC instruments from cache and sort by start time"""
        instruments = self.cache.instruments()
        logger.info(f"Loading ALL BTC instruments from {len(instruments)} total...")
        
        now = datetime.now(timezone.utc)
        current_timestamp = int(now.timestamp())
        
        btc_instruments = []
        
        for instrument in instruments:
            try:
                if hasattr(instrument, 'info') and instrument.info:
                    question = instrument.info.get('question', '').lower()
                    slug = instrument.info.get('market_slug', '').lower()
                    
                    if ('btc' in question or 'btc' in slug) and '15m' in slug:
                        try:
                            timestamp_part = slug.split('-')[-1]
                            market_timestamp = int(timestamp_part)
                            
                            # The slug timestamp IS the market start time (Unix, no offset).
                            # end_date_iso is a DATE-only string (e.g. "2026-02-20"), NOT a datetime,
                            # so parsing it gives midnight UTC which is wrong for intraday markets.
                            # Always derive end_timestamp from the slug: start + 900s.
                            real_start_ts = market_timestamp
                            end_timestamp = market_timestamp + 900  # 15-min markets always
                            time_diff = real_start_ts - current_timestamp
                            
                            # Only include markets that haven't ended yet
                            if end_timestamp > current_timestamp:
                                # Extract YES token ID for CLOB order book API.
                                # Nautilus instrument ID format:
                                #   {condition_id}-{token_id}.POLYMARKET
                                # The CLOB /book endpoint only accepts the token_id
                                # (the part after the dash, before .POLYMARKET).
                                raw_id = str(instrument.id)
                                # Strip .POLYMARKET suffix first
                                without_suffix = raw_id.split('.')[0] if '.' in raw_id else raw_id
                                # Then take the token_id after the condition_id dash
                                yes_token_id = without_suffix.split('-')[-1] if '-' in without_suffix else without_suffix

                                btc_instruments.append({
                                    'instrument': instrument,
                                    'slug': slug,
                                    'start_time': datetime.fromtimestamp(real_start_ts, tz=timezone.utc),
                                    'end_time': datetime.fromtimestamp(end_timestamp, tz=timezone.utc),
                                    'market_timestamp': market_timestamp,
                                    'end_timestamp': end_timestamp,
                                    'time_diff_minutes': time_diff / 60,
                                    'yes_token_id': yes_token_id,
                                })
                        except (ValueError, IndexError):
                            continue
            except Exception:
                continue
        
        # Pair YES and NO tokens by slug.
        # Each Polymarket market has two tokens loaded as separate Nautilus instruments.
        # The first instrument found for a slug is stored as the primary (YES/UP).
        # The second instrument found for the same slug is the NO/DOWN token.
        seen_slugs = {}
        deduped = []
        for inst in btc_instruments:
            slug = inst['slug']
            if slug not in seen_slugs:
                # First token seen = YES (UP)
                inst['yes_instrument_id'] = inst['instrument'].id
                inst['no_instrument_id'] = None  # will be filled when second token found
                seen_slugs[slug] = inst
                deduped.append(inst)
            else:
                # Second token seen = NO (DOWN) — store it on the existing entry
                seen_slugs[slug]['no_instrument_id'] = inst['instrument'].id
        btc_instruments = deduped
        
        # Sort by start time (absolute timestamp, not time-of-day)
        btc_instruments.sort(key=lambda x: x['market_timestamp'])
        
        logger.info("=" * 80)
        logger.info(f"FOUND {len(btc_instruments)} BTC 15-MIN MARKETS:")
        for i, inst in enumerate(btc_instruments):
            # A market is ACTIVE if it has started AND not yet ended
            is_active = inst['time_diff_minutes'] <= 0 and inst['end_timestamp'] > current_timestamp
            status = "ACTIVE" if is_active else "FUTURE" if inst['time_diff_minutes'] > 0 else "PAST"
            logger.info(f"  [{i}] {inst['slug']}: {status} (starts at {inst['start_time'].strftime('%H:%M:%S')}, ends at {inst['end_time'].strftime('%H:%M:%S')})")
        logger.info("=" * 80)
        
        self.all_btc_instruments = btc_instruments
        
        # Find current market and SUBSCRIBE IMMEDIATELY
        # FIXED: A market is current if it has STARTED and not yet ENDED (use end_time, not a hardcoded 15-min window)
        for i, inst in enumerate(btc_instruments):
            is_active = inst['time_diff_minutes'] <= 0 and inst['end_timestamp'] > current_timestamp
            if is_active:
                self.current_instrument_index = i
                self.instrument_id = inst['instrument'].id
                self.next_switch_time = inst['end_time']
                self._yes_token_id = inst.get('yes_token_id')
                self._yes_instrument_id = inst.get('yes_instrument_id', inst['instrument'].id)
                self._no_instrument_id = inst.get('no_instrument_id')
                logger.info(f"✓ CURRENT MARKET: {inst['slug']} (index {i})")
                logger.info(f"  Next switch at: {self.next_switch_time.strftime('%H:%M:%S')}")
                logger.info(f"  YES token: {self._yes_token_id[:16]}…" if self._yes_token_id else "  YES token: unknown")
                
                # =========================================================================
                # CRITICAL FIX: Subscribe immediately!
                # =========================================================================
                self.subscribe_quote_ticks(self.instrument_id)
                logger.info(f"  ✓ SUBSCRIBED to current market")
                break
        
        if self.current_instrument_index == -1 and btc_instruments:
            # No currently-active market — find the NEAREST upcoming one
            # (smallest positive time_diff_minutes = starts soonest)
            future_markets = [inst for inst in btc_instruments if inst['time_diff_minutes'] > 0]
            if future_markets:
                nearest = min(future_markets, key=lambda x: x['time_diff_minutes'])
                nearest_idx = btc_instruments.index(nearest)
            else:
                # All markets are in the past — use the last one
                nearest = btc_instruments[-1]
                nearest_idx = len(btc_instruments) - 1

            self.current_instrument_index = nearest_idx
            inst = nearest
            self.instrument_id = inst['instrument'].id
            self._yes_token_id = inst.get('yes_token_id')
            self._yes_instrument_id = inst.get('yes_instrument_id', inst['instrument'].id)
            self._no_instrument_id = inst.get('no_instrument_id')
            self.next_switch_time = inst['start_time']  # switch_time = when it OPENS
            logger.info(f"⚠ NO CURRENT MARKET - WAITING FOR NEAREST FUTURE: {inst['slug']}")
            logger.info(f"  Starts in {inst['time_diff_minutes']:.1f} min at {self.next_switch_time.strftime('%H:%M:%S')} UTC")

            # Subscribe so we get ticks when it opens
            self.subscribe_quote_ticks(self.instrument_id)
            logger.info(f"  ✓ SUBSCRIBED to future market")
            # Block trading until the market actually opens (timer loop sets _market_open flag)
            self._waiting_for_market_open = True
            
    def _switch_to_next_market(self):
        """Switch to the next market in the pre-loaded list"""
        if not self.all_btc_instruments:
            logger.error("No instruments loaded!")
            return False
        
        next_index = self.current_instrument_index + 1
        if next_index >= len(self.all_btc_instruments):
            logger.warning("No more markets available - will restart bot")
            return False
        
        next_market = self.all_btc_instruments[next_index]
        now = datetime.now(timezone.utc)
        
        # Check if next market is ready
        if now < next_market['start_time']:
            logger.info(f"Waiting for next market at {next_market['start_time'].strftime('%H:%M:%S')}")
            return False
        
        # Switch to next market
        self.current_instrument_index = next_index
        self.instrument_id = next_market['instrument'].id
        self.next_switch_time = next_market['end_time']
        self._yes_token_id = next_market.get('yes_token_id')
        self._yes_instrument_id = next_market.get('yes_instrument_id', next_market['instrument'].id)
        self._no_instrument_id = next_market.get('no_instrument_id')
        
        logger.info("=" * 80)
        logger.info(f"SWITCHING TO NEXT MARKET: {next_market['slug']}")
        logger.info(f"  Current time: {now.strftime('%H:%M:%S')}")
        logger.info(f"  Market ends at: {self.next_switch_time.strftime('%H:%M:%S')}")
        logger.info("=" * 80)
        
        # =========================================================================
        # FIX 5: Force stability for new market and reset trade timer correctly
        # =========================================================================
        self._stable_tick_count = QUOTE_STABILITY_REQUIRED  # Force stable immediately
        self._market_stable = True
        self._waiting_for_market_open = False  # Market is now active
        
        # Reset trade timer so we trade at the NEXT quote we receive
        # Use -1 so any interval will trigger (same as startup)
        self.last_trade_time = -1
        logger.info(f"  Trade timer reset — will trade on next tick")
        
        self.subscribe_quote_ticks(self.instrument_id)
        return True

    # ------------------------------------------------------------------
    # Timer loop - SIMPLIFIED
    # ------------------------------------------------------------------

    def _start_timer_loop(self):
        """Start timer loop in executor"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._timer_loop())
        finally:
            loop.close()

    async def _timer_loop(self):
        """
        Timer loop: checks every 10 seconds if it's time to switch markets.
        Also handles the case where we're waiting for a future market to open.
        """
        while True:
            # --- auto-restart check ---
            uptime_minutes = (datetime.now(timezone.utc) - self.bot_start_time).total_seconds() / 60
            if uptime_minutes >= self.restart_after_minutes:
                logger.warning("AUTO-RESTART TIME - Loading fresh filters")
                import signal as _signal
                os.kill(os.getpid(), _signal.SIGTERM)
                return

            now = datetime.now(timezone.utc)

            if self.next_switch_time and now >= self.next_switch_time:
                if self._waiting_for_market_open:
                    # The future market we were waiting for has now opened
                    # Treat it like a market switch so trade timer resets
                    logger.info("=" * 80)
                    logger.info(f"⏰ WAITING MARKET NOW OPEN: {now.strftime('%H:%M:%S')} UTC")
                    logger.info("=" * 80)
                    # Update next_switch_time to the market's END time
                    if (self.current_instrument_index >= 0 and
                            self.current_instrument_index < len(self.all_btc_instruments)):
                        current_market = self.all_btc_instruments[self.current_instrument_index]
                        self.next_switch_time = current_market['end_time']
                        logger.info(f"  Market ends at {self.next_switch_time.strftime('%H:%M:%S')} UTC")
                    self._waiting_for_market_open = False
                    self._market_stable = True
                    self._stable_tick_count = QUOTE_STABILITY_REQUIRED
                    self.last_trade_time = -1  # Trade immediately on next tick
                    logger.info("  ✓ MARKET OPEN — ready to trade on next tick")
                else:
                    # Normal market switch
                    self._switch_to_next_market()

            # Settle any paper trades whose resolution time has arrived
            try:
                await self._resolve_pending_paper_trades()
            except Exception as e:
                logger.warning(f"Paper trade resolver crashed: {e}")

            # Settle the research decision log's counterfactuals (executed + skipped)
            try:
                await self._resolve_pending_decisions()
            except Exception as e:
                logger.warning(f"Decision resolver crashed: {e}")

            await asyncio.sleep(10)

    # ------------------------------------------------------------------
    # Quote tick handler - SIMPLIFIED
    # ------------------------------------------------------------------

    def on_quote_tick(self, tick: QuoteTick):
        """Handle quote tick - TRADE when market opens and at each 15-min boundary"""
        try:
            # Only process ticks from current instrument
            if self.instrument_id is None or tick.instrument_id != self.instrument_id:
                return

            now = datetime.now(timezone.utc)
            bid = tick.bid_price
            ask = tick.ask_price

            if bid is None or ask is None:
                return
                
            try:
                bid_decimal = bid.as_decimal()
                ask_decimal = ask.as_decimal()
            except:
                return

            # Always store price history
            mid_price = (bid_decimal + ask_decimal) / 2
            self.price_history.append(mid_price)
            if len(self.price_history) > self.max_history:
                self.price_history.pop(0)
            
            # Store latest bid/ask for liquidity check before order placement
            self._last_bid_ask = (bid_decimal, ask_decimal)

            # Tick buffer for TickVelocityProcessor (rolling 90s window)
            self._tick_buffer.append({'ts': now, 'price': mid_price})

            # Stability gate
            if not self._market_stable:
                self._stable_tick_count += 1
                if self._stable_tick_count >= 1:
                    self._market_stable = True
                    logger.info(f"✓ Market STABLE immediately")
                else:
                    return

            # =========================================================================
            # FIXED TRADING LOGIC:
            # 
            # We trade once per 15-min market interval.
            # Instead of checking wall-clock 15-min boundaries (which caused the 2-hour
            # wait), we use a simple counter keyed to the Polymarket market's OWN
            # start time.
            #
            # The market's start_time is stored in all_btc_instruments[current_index].
            # Within each market, we compute a "sub-interval" index:
            #   sub_interval = elapsed_seconds_since_market_open // 900
            # Trade ID = (market_start_timestamp, sub_interval)
            # This fires once at market open AND once after every 15 min within
            # the same market if it's a multi-interval market.
            #
            # If _waiting_for_market_open is True (started before market opens),
            # we block trading until the timer loop calls _switch_to_next_market.
            # =========================================================================

            # Block trading if waiting for a future market to open
            if self._waiting_for_market_open:
                return

            # Get current market info
            if (self.current_instrument_index < 0 or
                    self.current_instrument_index >= len(self.all_btc_instruments)):
                return

            current_market = self.all_btc_instruments[self.current_instrument_index]
            market_start_ts = current_market['market_timestamp']  # Slug timestamp = market start (Unix)

            # How many 15-min intervals have elapsed since this market opened?
            elapsed_secs = now.timestamp() - market_start_ts
            if elapsed_secs < 0:
                # Market hasn't started yet — block
                return

            sub_interval = int(elapsed_secs // MARKET_INTERVAL_SECONDS)

            # Unique trade key: (market_start_timestamp, sub_interval)
            trade_key = (market_start_ts, sub_interval)

            # =========================================================================
            # TRADE WINDOW: minutes 13–14 of each 15-min market (780–840 seconds in)
            #
            # WHY LATE IN THE MARKET:
            #   At 13 minutes in, the UP/DOWN result is nearly decided. The price IS
            #   the trend — if YES is at $0.78, BTC went up during this interval.
            #   We're not predicting anymore, we're reading a nearly-resolved outcome.
            #
            # WHY NOT EARLIER (the old 30–90s window):
            #   At 30 seconds in, nobody knows which way BTC will move. The signals
            #   have no edge. This is why we were losing at prices near $0.50.
            #
            # TREND FILTER (applied in _make_trading_decision):
            #   Price > 0.60 → clear UP trend → buy YES
            #   Price < 0.40 → clear DOWN trend → buy NO
            #   Price 0.40–0.60 → coin flip → SKIP (don't trade)
            #
            # Share count intuition:
            #   1.4 shares = price $0.71 → strong trend, win rate ~71%
            #   1.9 shares = price $0.53 → weak trend, near coin flip
            #   2.0+ shares = price $0.50 → pure coin flip, SKIP
            # =========================================================================
            seconds_into_sub_interval = elapsed_secs % MARKET_INTERVAL_SECONDS
            TRADE_WINDOW_START = 780   # 13 minutes in
            TRADE_WINDOW_END   = 840   # 14 minutes in (60s window)

            if TRADE_WINDOW_START <= seconds_into_sub_interval < TRADE_WINDOW_END and trade_key != self.last_trade_time:
                self.last_trade_time = trade_key

                # Stash the window identity so the decision coroutine (which only
                # receives a price) can attribute its record to this market window.
                self._decision_window_ctx = {
                    "slug": current_market.get("slug"),
                    "condition_id": current_market.get("condition_id"),
                    "yes_token_id": self._yes_token_id,
                    "market_start_ts": market_start_ts,
                    "sub_interval": sub_interval,
                    "seconds_into_window": float(seconds_into_sub_interval),
                }

                logger.info("=" * 80)
                logger.info(f" LATE-WINDOW TRADE: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                logger.info(f"   Market: {current_market['slug']}")
                logger.info(f"   Sub-interval #{sub_interval} ({seconds_into_sub_interval:.1f}s in = {seconds_into_sub_interval/60:.1f} min)")
                logger.info(f"   Price: ${float(mid_price):,.4f} | Bid: ${float(bid_decimal):,.4f} | Ask: ${float(ask_decimal):,.4f}")
                logger.info(f"   Trend strength: {'STRONG ✓' if float(mid_price) > 0.60 or float(mid_price) < 0.40 else 'WEAK — may skip'}")
                logger.info(f"   Price history: {len(self.price_history)} points")
                logger.info("=" * 80)

                self.run_in_executor(lambda: self._make_trading_decision_sync(float(mid_price)))

        except Exception as e:
            logger.error(f"Error processing quote tick: {e}")

    # ------------------------------------------------------------------
    # Trading decision (unchanged)
    # ------------------------------------------------------------------

    def _make_trading_decision_sync(self, current_price):
        from decimal import Decimal
        price_decimal = Decimal(str(current_price))
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._make_trading_decision(price_decimal))
        finally:
            loop.close()
    
    def _make_trading_decision_sync(self, current_price):
        """Synchronous wrapper for trading decision (called from executor)."""
        # Convert float back to Decimal for processing
        from decimal import Decimal
        price_decimal = Decimal(str(current_price))
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._make_trading_decision(price_decimal))
        finally:
            loop.close()
            
    async def _fetch_market_context(self, current_price: Decimal) -> dict:
        """
        Fetch REAL external data to populate signal processor metadata.

        Returns a dict with:
          - sentiment_score (float 0-100): live Fear & Greed index, or None
          - spot_price (float): live BTC-USD from Coinbase, or None
          - deviation (float): polymarket price vs SMA-20 (always computed)
          - momentum (float): 5-period rate of change (always computed)
          - volatility (float): price std-dev over last 20 ticks (always computed)
        """
        current_price_float = float(current_price)

        # --- Always-available stats from local price_history ---
        recent_prices = [float(p) for p in self.price_history[-20:]]
        sma_20 = sum(recent_prices) / len(recent_prices)
        deviation = (current_price_float - sma_20) / sma_20
        momentum = (
            (current_price_float - float(self.price_history[-5])) / float(self.price_history[-5])
            if len(self.price_history) >= 5 else 0.0
        )
        variance = sum((p - sma_20) ** 2 for p in recent_prices) / len(recent_prices)
        volatility = math.sqrt(variance)

        metadata = {
            "deviation": deviation,
            "momentum": momentum,
            "volatility": volatility,
            # Tick buffer for TickVelocityProcessor
            "tick_buffer": list(self._tick_buffer),
            # YES token id for OrderBookImbalanceProcessor
            "yes_token_id": self._yes_token_id,
        }

        # --- Real sentiment: Fear & Greed Index via NewsSocialDataSource ---
        try:
            from data_sources.news_social.adapter import NewsSocialDataSource
            news_source = NewsSocialDataSource()
            await news_source.connect()
            fg = await news_source.get_fear_greed_index()
            await news_source.disconnect()
            if fg and "value" in fg:
                metadata["sentiment_score"] = float(fg["value"])
                metadata["sentiment_classification"] = fg.get("classification", "")
                logger.info(
                    f"Fear & Greed: {metadata['sentiment_score']:.0f} "
                    f"({metadata['sentiment_classification']})"
                )
            else:
                logger.warning("Fear & Greed fetch returned no data — sentiment processor skipped")
        except Exception as e:
            logger.warning(f"Could not fetch Fear & Greed index: {e} — sentiment processor skipped")

        # --- Real spot price: Coinbase BTC-USD REST API ---
        try:
            from data_sources.coinbase.adapter import CoinbaseDataSource
            coinbase = CoinbaseDataSource()
            await coinbase.connect()
            spot = await coinbase.get_current_price()
            await coinbase.disconnect()
            if spot:
                metadata["spot_price"] = float(spot)
                logger.info(f"Coinbase spot price: ${float(spot):,.2f}")
            else:
                logger.warning("Coinbase price fetch returned None — divergence processor skipped")
        except Exception as e:
            logger.warning(f"Could not fetch Coinbase spot price: {e} — divergence processor skipped")

        logger.info(
            f"Market context — deviation={deviation:.2%}, "
            f"momentum={momentum:.2%}, volatility={volatility:.4f}, "
            f"sentiment={'%.0f' % metadata['sentiment_score'] if 'sentiment_score' in metadata else 'N/A'}, "
            f"spot=${'%.2f' % metadata['spot_price'] if 'spot_price' in metadata else 'N/A'}"
        )
        return metadata

    async def _make_trading_decision(self, current_price: Decimal):
        """
        Make trading decision using our 7-phase system.

        Position size is always $1.00 — no variable sizing, no risk-engine
        calculation needed. The risk engine is still used to check that we
        don't already have too many open positions.
        """
        # --- Mode check ---
        is_simulation = await self.check_simulation_mode()
        logger.info(f"Mode: {'SIMULATION' if is_simulation else 'LIVE TRADING'}")

        # --- Minimum history guard ---
        if len(self.price_history) < 20:
            logger.warning(f"Not enough price history ({len(self.price_history)}/20)")
            self._capture_decision("skipped", "insufficient_history", None, current_price, None, None, None)
            return

        logger.info(f"Current price: ${float(current_price):,.4f}")

        # --- Phase 4a: Build real metadata for processors ---
        metadata = await self._fetch_market_context(current_price)

        # --- Phase 4b: Run all three signal processors ---
        signals = self._process_signals(current_price, metadata)

        if not signals:
            logger.info("No signals generated — no trade this interval")
            self._capture_decision("skipped", "no_signal", None, current_price, None, None, metadata)
            return

        logger.info(f"Generated {len(signals)} signal(s):")
        for sig in signals:
            logger.info(
                f"  [{sig.source}] {sig.direction.value}: "
                f"score={sig.score:.1f}, confidence={sig.confidence:.2%}"
            )

        # --- Phase 4c: Fuse signals into one consensus ---
        # min_score lowered to 40 because the TREND FILTER (price at min 11-13)
        # is now the primary decision maker. Fusion is informational context,
        # not the trade gate. The trend gate below is the real filter.
        fused = self.fusion_engine.fuse_signals(signals, min_signals=1, min_score=40.0)
        if not fused:
            logger.info("Fusion produced no actionable signal — no trade this interval")
            self._capture_decision("skipped", "no_fusion", None, current_price, signals, None, metadata)
            return

        logger.info(
            f"FUSED SIGNAL: {fused.direction.value} "
            f"(score={fused.score:.1f}, confidence={fused.confidence:.2%})"
        )

        # --- Phase 5: Position size is always exactly $1.00 ---
        POSITION_SIZE_USD = Decimal("1.00")

        # =========================================================================
        # TREND FILTER — replaces signal-based direction at the late trade window
        #
        # At minute 13, the Polymarket price IS the market's verdict on BTC direction.
        # We ignore what the signal processors say and simply follow the price:
        #
        #   price > 0.60 → market says UP with >60% confidence → buy YES
        #   price < 0.40 → market says DOWN with >60% confidence → buy NO
        #   price 0.40–0.60 → too close to call → SKIP (this is where we were losing)
        #
        # This directly addresses the observation that trades at 1.9–2.0+ shares
        # (price near $0.50) almost always lose, while trades at 1.4 shares
        # (price ~$0.71) mostly win.
        # =========================================================================
        TREND_UP_THRESHOLD   = 0.60   # price above this → buy YES (UP)
        TREND_DOWN_THRESHOLD = 0.40   # price below this → buy NO (DOWN)

        price_float = float(current_price)

        if price_float > TREND_UP_THRESHOLD:
            direction = "long"
            trend_confidence = price_float  # e.g. 0.72 = 72% confident UP
            logger.info(
                f" TREND: UP ({price_float:.2%} YES probability) → buying YES"
            )
        elif price_float < TREND_DOWN_THRESHOLD:
            direction = "short"
            trend_confidence = 1.0 - price_float  # e.g. 0.31 price = 69% confident DOWN
            logger.info(
                f" TREND: DOWN ({price_float:.2%} YES probability = {1-price_float:.2%} NO) → buying NO"
            )
        else:
            logger.info(
                f"⏭ TREND: NEUTRAL ({price_float:.2%}) — price too close to 0.50, SKIPPING trade "
                f"(coin flip territory: {TREND_DOWN_THRESHOLD:.0%}–{TREND_UP_THRESHOLD:.0%})"
            )
            self._capture_decision("skipped", "neutral", None, current_price, signals, fused, metadata)
            return

        # Risk engine: only check position-count / exposure limits (no sizing math)
        is_valid, error = self.risk_engine.validate_new_position(
            size=POSITION_SIZE_USD,
            direction=direction,
            current_price=current_price,
        )
        if not is_valid:
            logger.warning(f"Risk engine blocked trade: {error}")
            self._capture_decision("skipped", "risk_blocked", direction, current_price, signals, fused, metadata)
            return

        logger.info(f"Position size: $1.00 (fixed) | Direction: {direction.upper()}")

        # --- Edge guard: skip near-resolved prices with negligible upside ---
        # By minute 13 the book is usually pinned near $0 or $1. Entering there
        # means risking ~$1 to win pennies, with a catastrophic loss on a wrong
        # resolution — zero-to-negative EV after fees. Block BOTH extremes
        # symmetrically. (The old check only caught the cheap side via `<= 0.02`,
        # so a LONG at ask≈0.999 — same garbage, mirror image — still slipped
        # through.) Fill is the crossed price: ask for a BUY, bid for a SELL.
        #   LONG  (buy YES): upside (1-fill)/fill → vanishes as ask → 1
        #   SHORT (buy NO) : upside fill/(1-fill) → vanishes as bid → 0
        # Require at least MIN_EDGE of room from the unfavorable bound.
        last_tick = getattr(self, '_last_bid_ask', None)
        if last_tick:
            last_bid, last_ask = last_tick
            MIN_EDGE = Decimal("0.02")
            if direction == "long" and last_ask >= (Decimal("1") - MIN_EDGE):
                logger.warning(
                    f"⚠ No edge for BUY: ask=${float(last_ask):.4f} ≥ {float(1 - MIN_EDGE):.2f} — skipping trade, will retry next tick"
                )
                self._capture_decision("skipped", "no_edge", direction, current_price, signals, fused, metadata)
                self.last_trade_time = -1  # Allow retry next tick
                return
            if direction == "short" and last_bid <= MIN_EDGE:
                logger.warning(
                    f"⚠ No edge for SELL: bid=${float(last_bid):.4f} ≤ {float(MIN_EDGE):.2f} — skipping trade, will retry next tick"
                )
                self._capture_decision("skipped", "no_edge", direction, current_price, signals, fused, metadata)
                self.last_trade_time = -1  # Allow retry next tick
                return

        # --- Phase 5 / 6: Execute ---
        if is_simulation:
            await self._record_paper_trade(fused, POSITION_SIZE_USD, current_price, direction, metadata)
            self._capture_decision("opened", f"trend_{direction}", direction, current_price, signals, fused, metadata)
        else:
            await self._place_real_order(fused, POSITION_SIZE_USD, current_price, direction)

    async def _record_paper_trade(self, signal, position_size, current_price, direction, metadata):
        """Open a paper trade and queue it for resolution at market close.

        No exit price, no P&L at this point. The trade enters _pending_paper_trades
        and is settled by _resolve_pending_paper_trades when the market closes —
        using the actual BTC spot direction from Coinbase, not RNG.
        """
        # SAFETY INVARIANT: this is the simulation-only path. The caller reaches it
        # exclusively via `if is_simulation:` and the bot defaults to simulation
        # (only `--live` flips it). This method records to paper_trades.json and must
        # NEVER call the order-placement path. Do not add live order submission here.
        entry_btc_spot = metadata.get("spot_price") if metadata else None
        if entry_btc_spot is None:
            logger.warning(
                "Cannot open paper trade — no entry BTC spot available "
                "(Coinbase fetch failed earlier). Skipping this trade entirely "
                "rather than recording one we can't honestly resolve."
            )
            return

        # Resolution time: end of current 15-min market in normal mode, +1min in test mode.
        if self.test_mode:
            resolution_time = datetime.now(timezone.utc) + timedelta(minutes=1)
        elif self.next_switch_time:
            # next_switch_time is the end of the active market — add 5s buffer for settlement.
            resolution_time = self.next_switch_time + timedelta(seconds=5)
        else:
            resolution_time = datetime.now(timezone.utc) + timedelta(minutes=15)

        trade_id = f"paper_{int(datetime.now(timezone.utc).timestamp())}"

        # --- Realistic fill: you cannot transact at the mid. A YES buyer (LONG)
        # crosses to the ASK; a NO buyer (SHORT) enters at the BID. Use the REAL
        # live book cached on the last quote tick — this is the ground truth the
        # backtester could only approximate with a half-spread haircut.
        mid = float(current_price)
        DEFAULT_HALF_SPREAD = 0.01  # fallback only if no live book is cached
        bid_ask = getattr(self, "_last_bid_ask", None)
        if bid_ask and bid_ask[0] is not None and bid_ask[1] is not None:
            bid_f, ask_f = float(bid_ask[0]), float(bid_ask[1])
            if ask_f > bid_f:
                half_spread = (ask_f - bid_f) / 2.0
                fill_price = ask_f if direction == "long" else bid_f
            else:  # degenerate/crossed book — fall back to a symmetric haircut
                half_spread = DEFAULT_HALF_SPREAD
                fill_price = mid + half_spread if direction == "long" else mid - half_spread
        else:
            half_spread = DEFAULT_HALF_SPREAD
            fill_price = mid + half_spread if direction == "long" else mid - half_spread
        fill_price = min(0.9999, max(0.0001, fill_price))  # keep p strictly in (0,1)

        # Market identity for ACTUAL Polymarket resolution (resolved via Gamma by slug).
        cur_slug = None
        cur_condition_id = None
        try:
            cur = self.all_btc_instruments[self.current_instrument_index]
            cur_slug = cur.get("slug")
            cur_condition_id = cur.get("condition_id")
        except (AttributeError, IndexError, TypeError):
            pass
        cur_yes_token = (metadata.get("yes_token_id") if metadata else None) or getattr(self, "_yes_token_id", None)

        paper_trade = PaperTrade(
            timestamp=datetime.now(timezone.utc),
            direction=direction.upper(),
            size_usd=float(position_size),
            price=mid,
            signal_score=signal.score,
            signal_confidence=signal.confidence,
            outcome="PENDING",
            fill_price=fill_price,
            half_spread=half_spread,
            slug=cur_slug,
            condition_id=cur_condition_id,
            yes_token_id=cur_yes_token,
            trade_id=trade_id,
            entry_btc_spot=float(entry_btc_spot),
            resolution_time=resolution_time,
        )
        self.paper_trades.append(paper_trade)
        self._pending_paper_trades.append(paper_trade)

        logger.info("=" * 80)
        logger.info("[SIMULATION] PAPER TRADE OPENED — awaiting resolution")
        logger.info(f"  Trade ID: {trade_id}")
        logger.info(f"  Direction: {direction.upper()} ({'YES' if direction == 'long' else 'NO'} token)")
        logger.info(f"  Size: ${float(position_size):.2f}")
        logger.info(f"  Entry mid: ${mid:,.4f} | FILL (crossed): ${fill_price:,.4f} | half-spread paid: ${half_spread:.4f}")
        logger.info(f"  Market slug: {cur_slug or 'UNKNOWN (resolution will be unavailable!)'}")
        logger.info(f"  Entry BTC Spot: ${float(entry_btc_spot):,.2f}")
        logger.info(f"  Resolves at: {resolution_time.strftime('%H:%M:%S')} UTC")
        logger.info(f"  Pending queue size: {len(self._pending_paper_trades)}")
        logger.info("=" * 80)

        self._save_paper_trades()

    async def _resolve_pending_paper_trades(self):
        """Settle pending paper trades using the ACTUAL Polymarket settled outcome.

        Resolution mechanism (forward paper-trading harness):
          1. A trade is "due" once its market's close time has passed.
          2. Query Polymarket Gamma for the market's settled outcome by slug
             (paper_resolution.fetch_market_resolution). Returns yes_won, or None
             if the market has not settled yet (UMA lag) / fetch failed — in which
             case we leave the trade PENDING and retry on the next timer tick. None
             is NEVER treated as a loss.
          3. P&L is computed on the REALISTIC fill price recorded at entry (the
             spread-crossed price), not the mid:
               long  + yes_won  → payout = size * (1 - f) / f   [bought YES at fill f]
               long  + !yes_won → payout = -size
               short + !yes_won → payout = size * f / (1 - f)   [bought NO; YES-equiv fill f]
               short + yes_won  → payout = -size
          4. Fee approx: size * 4·f·(1-f)·POLYMARKET_FEE_PEAK
          5. P&L = payout - fee

        Gamma resolves any closed market retroactively, so trades stuck PENDING
        across a restart settle correctly once reloaded.
        """
        if not self._pending_paper_trades:
            return

        now = datetime.now(timezone.utc)
        due = [t for t in self._pending_paper_trades if t.resolution_time and now >= t.resolution_time]
        if not due:
            return

        from paper_resolution import fetch_market_resolution

        resolved_now = []
        for trade in due:
            if not trade.slug:
                logger.warning(
                    f"Resolver: paper trade {trade.trade_id} has no market slug — "
                    f"cannot fetch its actual Polymarket outcome. Leaving PENDING."
                )
                continue

            yes_won = fetch_market_resolution(trade.slug)
            if yes_won is None:
                # Not settled on-chain yet (UMA lag) or transient fetch failure — retry next tick.
                logger.debug(f"Resolver: {trade.slug} not resolved on Gamma yet; will retry.")
                continue

            f = trade.fill_price if trade.fill_price is not None else trade.price
            size = trade.size_usd
            direction = trade.direction.lower()

            if direction == "long":
                payout = size * (1.0 - f) / f if yes_won else -size
            else:  # short → bought NO; YES-equivalent entry price is the fill f
                payout = size * f / (1.0 - f) if (not yes_won) else -size

            fee = size * 4.0 * f * (1.0 - f) * POLYMARKET_FEE_PEAK
            pnl = payout - fee

            trade.exit_price = 1.0 if yes_won else 0.0
            trade.resolution_source = "gamma_actual"
            trade.pnl = pnl
            trade.fee = fee
            trade.outcome = "WIN" if pnl > 0 else "LOSS"
            resolved_now.append(trade)

            logger.info("=" * 80)
            logger.info("[SIMULATION] PAPER TRADE RESOLVED — actual Polymarket outcome")
            logger.info(f"  Trade ID: {trade.trade_id} | slug: {trade.slug}")
            logger.info(f"  Direction: {trade.direction} ({'YES' if direction == 'long' else 'NO'})")
            logger.info(f"  Fill: ${f:,.4f} (mid ${trade.price:,.4f}, half-spread ${trade.half_spread or 0.0:.4f})")
            logger.info(f"  Settled: {'YES won' if yes_won else 'NO won'} (Gamma outcomePrices)")
            logger.info(f"  Gross payout: ${payout:+.4f}")
            logger.info(f"  Fee (≈{(fee/size)*100:.2f}%): ${fee:.4f}")
            logger.info(f"  Net P&L: ${pnl:+.4f} → {trade.outcome}")
            logger.info("=" * 80)

            try:
                self.performance_tracker.record_trade(
                    trade_id=trade.trade_id,
                    direction=direction,
                    entry_price=Decimal(str(f)),
                    exit_price=Decimal(str(trade.exit_price)),
                    size=Decimal(str(size)),
                    entry_time=trade.timestamp,
                    exit_time=now,
                    signal_score=trade.signal_score,
                    signal_confidence=trade.signal_confidence,
                    metadata={
                        "simulated": True,
                        "fill_price": f,
                        "half_spread": trade.half_spread,
                        "resolution_source": "gamma_actual",
                        "fee": fee,
                        "pnl": pnl,
                    },
                )
            except Exception as e:
                logger.warning(f"performance_tracker.record_trade failed: {e}")

            if self.grafana_exporter:
                try:
                    self.grafana_exporter.increment_trade_counter(won=(pnl > 0))
                    duration = (now - trade.timestamp).total_seconds()
                    self.grafana_exporter.record_trade_duration(duration)
                except Exception as e:
                    logger.debug(f"grafana update failed: {e}")

        # Remove only the trades that actually resolved; None-results stay PENDING.
        if resolved_now:
            self._pending_paper_trades = [
                t for t in self._pending_paper_trades if t not in resolved_now
            ]
            self._save_paper_trades()

    def _save_paper_trades(self):
        """Persist atomically: write a temp file then os.replace (atomic rename),
        so a crash mid-write can never corrupt paper_trades.json."""
        import json, os, tempfile
        try:
            trades_data = [t.to_dict() for t in self.paper_trades]
            d = os.path.dirname(os.path.abspath('paper_trades.json')) or '.'
            fd, tmp = tempfile.mkstemp(dir=d, prefix='.paper_trades.', suffix='.tmp')
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump(trades_data, f, indent=2)
                os.replace(tmp, 'paper_trades.json')
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        except Exception as e:
            logger.error(f"Failed to save paper trades: {e}")

    # ------------------------------------------------------------------
    # Research decision log (event-sourced JSONL) — Phase 1 & 2
    # ------------------------------------------------------------------
    def _append_decision_event(self, record: dict) -> None:
        """Append one JSON line to decisions.jsonl. Append-only + flushed, so it
        is crash-safe and never rewrites prior events. NEVER raises — capture must
        not be able to break the trade path."""
        import json
        try:
            with open(self._decisions_log_path, 'a') as f:
                f.write(json.dumps(record) + '\n')
                f.flush()
        except Exception as e:
            logger.debug(f"decision-log append failed (non-fatal): {e}")

    def _capture_decision(self, action: str, reason: str, direction: Optional[str],
                          current_price, signals=None, fused=None, metadata=None) -> None:
        """Record ONE decision event per market window (executed or skipped) with its
        point-in-time feature vector + per-processor signals. Deduped per
        (market_start_ts, sub_interval) so the retry-on-block storm collapses to one
        record; 'opened' is terminal and upgrades a prior 'skipped'. The would-be
        fill is captured even when skipped, so the counterfactual is computable later.
        NEVER raises."""
        try:
            ctx = self._decision_window_ctx
            if not ctx:
                return  # no window context (decision fired outside a tracked window)
            window_key = (ctx.get("market_start_ts"), ctx.get("sub_interval"))
            prev = self._captured_decision_windows.get(window_key)
            if prev == "opened" or prev == action:
                return  # terminal, or already recorded in this state (retry storm)
            self._captured_decision_windows[window_key] = action

            now = datetime.now(timezone.utc)
            decision_id = f"dec_{ctx.get('market_start_ts')}_{ctx.get('sub_interval')}"

            bid = ask = None
            ba = getattr(self, "_last_bid_ask", None)
            if ba and ba[0] is not None and ba[1] is not None:
                bid, ask = float(ba[0]), float(ba[1])
            mid = float(current_price) if current_price is not None else None
            would_fill_long = ask if ask is not None else mid    # a BUY crosses the ask
            would_fill_short = bid if bid is not None else mid    # a SELL rests at the bid
            half_spread = (ask - bid) / 2.0 if (bid is not None and ask is not None and ask > bid) else None

            md = metadata or {}
            features = {
                k: md.get(k) for k in
                ("deviation", "momentum", "volatility", "sentiment_score",
                 "sentiment_classification", "spot_price")
            }

            sig_list = []
            for s in (signals or []):
                try:
                    sig_list.append({
                        "source": s.source,
                        "direction": s.direction.value,
                        "strength": s.strength.value,
                        "score": round(float(s.score), 4),
                        "confidence": round(float(s.confidence), 4),
                        "metadata": s.metadata or {},
                    })
                except Exception:
                    continue

            fused_obj = None
            if fused is not None:
                try:
                    fused_obj = {
                        "direction": fused.direction.value,
                        "score": round(float(fused.score), 4),
                        "confidence": round(float(fused.confidence), 4),
                    }
                except Exception:
                    fused_obj = None

            record = {
                "schema_version": 1,
                "event": "decision",
                "decision_id": decision_id,
                "ts": now.isoformat(),
                "market": {
                    "slug": ctx.get("slug"),
                    "condition_id": ctx.get("condition_id"),
                    "yes_token_id": ctx.get("yes_token_id"),
                    "market_start_ts": ctx.get("market_start_ts"),
                    "sub_interval": ctx.get("sub_interval"),
                    "seconds_into_window": ctx.get("seconds_into_window"),
                },
                "action": action,
                "reason": reason,
                "direction": direction,
                "poly": {
                    "mid": mid, "bid": bid, "ask": ask,
                    "half_spread": half_spread,
                    "would_fill_long": would_fill_long,
                    "would_fill_short": would_fill_short,
                },
                "spot_btc": md.get("spot_price"),
                "features": features,
                "signals": sig_list,
                "fused": fused_obj,
                "trade_id": (f"paper_{int(now.timestamp())}" if action == "opened" else None),
            }
            self._append_decision_event(record)

            # Queue for counterfactual resolution (executed AND skipped). resolution_time
            # mirrors the trade resolver: end of this market + small buffer.
            if self.test_mode:
                res_time = now + timedelta(minutes=1)
            elif self.next_switch_time:
                res_time = self.next_switch_time + timedelta(seconds=5)
            else:
                res_time = now + timedelta(minutes=15)
            self._pending_decisions[decision_id] = {
                "decision_id": decision_id,
                "slug": ctx.get("slug"),
                "resolution_time": res_time,
                "direction": direction,
                "would_fill_long": would_fill_long,
                "would_fill_short": would_fill_short,
                "mid": mid,
            }
        except Exception as e:
            logger.debug(f"decision capture failed (non-fatal): {e}")

    async def _resolve_pending_decisions(self) -> None:
        """Append a `resolution` event for each due decision (executed AND skipped),
        computing the realized counterfactual P&L of BOTH directions via the same
        Gamma outcome the trade resolver uses. This is what makes the trades the
        guards blocked studyable. NEVER raises into the timer loop."""
        try:
            if not self._pending_decisions:
                return
            now = datetime.now(timezone.utc)
            due = [e for e in self._pending_decisions.values()
                   if e.get("resolution_time") and now >= e["resolution_time"]]
            if not due:
                return

            from paper_resolution import fetch_market_resolution

            def _pnl(direction: str, f: Optional[float], yes_won: bool):
                """Net P&L for a $1 bet at fill f (YES-equivalent price), binary payout."""
                if f is None:
                    return None
                f = min(0.9999, max(0.0001, float(f)))
                size = 1.0
                if direction == "long":
                    payout = size * (1.0 - f) / f if yes_won else -size
                else:  # short
                    payout = size * f / (1.0 - f) if (not yes_won) else -size
                fee = size * 4.0 * f * (1.0 - f) * POLYMARKET_FEE_PEAK
                return round(payout - fee, 6)

            resolved_ids = []
            for entry in due:
                slug = entry.get("slug")
                if not slug:
                    resolved_ids.append(entry["decision_id"])  # unresolvable — drop
                    continue
                yes_won = fetch_market_resolution(slug)
                if yes_won is None:
                    continue  # not settled yet / transient — retry next tick

                pnl_if_long = _pnl("long", entry.get("would_fill_long"), yes_won)
                pnl_if_short = _pnl("short", entry.get("would_fill_short"), yes_won)
                taken = entry.get("direction")
                pnl_if_taken = (pnl_if_long if taken == "long"
                                else pnl_if_short if taken == "short" else None)

                self._append_decision_event({
                    "schema_version": 1,
                    "event": "resolution",
                    "decision_id": entry["decision_id"],
                    "ts": now.isoformat(),
                    "yes_won": bool(yes_won),
                    "exit_price": 1.0 if yes_won else 0.0,
                    "pnl_if_long": pnl_if_long,
                    "pnl_if_short": pnl_if_short,
                    "pnl_if_taken": pnl_if_taken,
                    "source": "gamma_actual",
                })
                resolved_ids.append(entry["decision_id"])

            for did in resolved_ids:
                self._pending_decisions.pop(did, None)
        except Exception as e:
            logger.debug(f"decision resolution failed (non-fatal): {e}")

    def _load_paper_trades(self):
        """Reload persisted paper trades on startup (crash recovery). PENDING trades
        are re-queued so the resolver settles them via Gamma once they've closed —
        a multi-day paper run survives restarts without losing or stranding trades."""
        import json, os
        if not os.path.exists('paper_trades.json'):
            return
        try:
            with open('paper_trades.json') as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load paper_trades.json on startup: {e}")
            return
        try:
            self.paper_trades = [PaperTrade.from_dict(d) for d in data]
            self._pending_paper_trades = [t for t in self.paper_trades if t.outcome == "PENDING"]
            logger.info(
                f"[recovery] Reloaded {len(self.paper_trades)} paper trades "
                f"({len(self._pending_paper_trades)} still PENDING — will resolve via Gamma)."
            )
        except Exception as e:
            logger.error(f"Failed to rebuild paper trades from file: {e}")

    # ------------------------------------------------------------------
    # Real order (unchanged)
    # ------------------------------------------------------------------

    async def _place_real_order(self, signal, position_size, current_price, direction):
        if not self.instrument_id:
            logger.error("No instrument available")
            return

        try:
            # instrument is fetched below after determining YES vs NO token

            logger.info("=" * 80)
            logger.info("LIVE MODE - PLACING REAL ORDER!")
            logger.info("=" * 80)

            # On Polymarket, both UP and DOWN are BUY orders.
            # Bullish = buy YES token (self._yes_instrument_id)
            # Bearish = buy NO token  (self._no_instrument_id)
            # There is NO sell — you always buy whichever side you want.
            side = OrderSide.BUY

            if direction == "long":
                trade_instrument_id = getattr(self, '_yes_instrument_id', self.instrument_id)
                trade_label = "YES (UP)"
            else:
                no_id = getattr(self, '_no_instrument_id', None)
                if no_id is None:
                    logger.warning(
                        "NO token instrument not found for this market — "
                        "cannot bet DOWN. Skipping trade."
                    )
                    return
                trade_instrument_id = no_id
                trade_label = "NO (DOWN)"

            instrument = self.cache.instrument(trade_instrument_id)
            if not instrument:
                logger.error(f"Instrument not in cache: {trade_instrument_id}")
                return

            logger.info(f"Buying {trade_label} token: {trade_instrument_id}")

            trade_price = float(current_price)
            max_usd_amount = float(position_size)

            precision = instrument.size_precision

            # Always BUY — the market-order patch converts this to a USD amount.
            # Pass dummy qty=5 (minimum) so Nautilus risk engine doesn't deny it.
            min_qty_val = float(getattr(instrument, 'min_quantity', None) or 5.0)
            token_qty = max(min_qty_val, 5.0)
            token_qty = round(token_qty, precision)
            logger.info(
                f"BUY {trade_label}: dummy qty={token_qty:.6f} "
                f"(patch converts to ${max_usd_amount:.2f} USD)"
            )

            qty = Quantity(token_qty, precision=precision)
            timestamp_ms = int(time.time() * 1000)
            unique_id = f"BTC-15MIN-${max_usd_amount:.0f}-{timestamp_ms}"

            order = self.order_factory.market(
                instrument_id=trade_instrument_id,
                order_side=side,
                quantity=qty,
                client_order_id=ClientOrderId(unique_id),
                quote_quantity=False,
                time_in_force=TimeInForce.IOC,
            )

            self.submit_order(order)

            logger.info(f"REAL ORDER SUBMITTED!")
            logger.info(f"  Order ID: {unique_id}")
            logger.info(f"  Direction: {trade_label}")
            logger.info(f"  Side: BUY")
            logger.info(f"  Token Quantity: {token_qty:.6f}")
            logger.info(f"  Estimated Cost: ~${max_usd_amount:.2f}")
            logger.info(f"  Price: ${trade_price:.4f}")
            logger.info("=" * 80)

            self._track_order_event("placed")

        except Exception as e:
            logger.error(f"Error placing real order: {e}")
            import traceback
            traceback.print_exc()
            self._track_order_event("rejected")

    # ------------------------------------------------------------------
    # Signal processing
    # ------------------------------------------------------------------

    def _process_signals(self, current_price, metadata=None):
        signals = []
        if metadata is None:
            metadata = {}

        processed_metadata = {}
        for key, value in metadata.items():
            if isinstance(value, float):
                processed_metadata[key] = Decimal(str(value))
            else:
                processed_metadata[key] = value

        spike_signal = self.spike_detector.process(
            current_price=current_price,
            historical_prices=self.price_history,
            metadata=processed_metadata,
        )
        if spike_signal:
            signals.append(spike_signal)

        if 'sentiment_score' in processed_metadata:
            sentiment_signal = self.sentiment_processor.process(
                current_price=current_price,
                historical_prices=self.price_history,
                metadata=processed_metadata,
            )
            if sentiment_signal:
                signals.append(sentiment_signal)

        if 'spot_price' in processed_metadata:
            divergence_signal = self.divergence_processor.process(
                current_price=current_price,
                historical_prices=self.price_history,
                metadata=processed_metadata,
            )
            if divergence_signal:
                signals.append(divergence_signal)

        # --- Order Book Imbalance (real-time Polymarket CLOB depth) ---
        if processed_metadata.get('yes_token_id'):
            ob_signal = self.orderbook_processor.process(
                current_price=current_price,
                historical_prices=self.price_history,
                metadata=processed_metadata,
            )
            if ob_signal:
                signals.append(ob_signal)

        # --- Tick Velocity (last 60s of Polymarket probability movement) ---
        if processed_metadata.get('tick_buffer'):
            tv_signal = self.tick_velocity_processor.process(
                current_price=current_price,
                historical_prices=self.price_history,
                metadata=processed_metadata,
            )
            if tv_signal:
                signals.append(tv_signal)

        # --- Deribit Put/Call Ratio (institutional options sentiment) ---
        pcr_signal = self.deribit_pcr_processor.process(
            current_price=current_price,
            historical_prices=self.price_history,
            metadata=processed_metadata,
        )
        if pcr_signal:
            signals.append(pcr_signal)

        return signals

    # ------------------------------------------------------------------
    # Order events
    # ------------------------------------------------------------------

    def _track_order_event(self, event_type: str) -> None:
        """
        Safely track an order event on the performance tracker.

        PerformanceTracker does not expose `increment_order_counter`, so we
        use whichever method is actually available, or fall back to a no-op.
        Supported event_type values: "placed", "filled", "rejected".
        """
        try:
            pt = self.performance_tracker
            # Try the method that actually exists first
            if hasattr(pt, 'record_order_event'):
                pt.record_order_event(event_type)
            elif hasattr(pt, 'increment_counter'):
                pt.increment_counter(event_type)
            elif hasattr(pt, 'increment_order_counter'):
                pt.increment_order_counter(event_type)
            else:
                # No suitable method found – log and carry on
                logger.debug(
                    f"PerformanceTracker has no order-counter method; "
                    f"ignoring event '{event_type}'"
                )
        except Exception as e:
            logger.warning(f"Failed to track order event '{event_type}': {e}")

    def on_order_filled(self, event):
        logger.info("=" * 80)
        logger.info(f"ORDER FILLED!")
        logger.info(f"  Order: {event.client_order_id}")
        logger.info(f"  Fill Price: ${float(event.last_px):.4f}")
        logger.info(f"  Quantity: {float(event.last_qty):.6f}")
        logger.info("=" * 80)
        self._track_order_event("filled")

    def on_order_denied(self, event):
        logger.error("=" * 80)
        logger.error(f"ORDER DENIED!")
        logger.error(f"  Order: {event.client_order_id}")
        logger.error(f"  Reason: {event.reason}")
        logger.error("=" * 80)
        self._track_order_event("rejected")

    def on_order_rejected(self, event):
        """Handle order rejection — reset trade timer so we can retry next tick."""
        reason = str(getattr(event, 'reason', ''))
        reason_lower = reason.lower()
        if 'no orders found' in reason_lower or 'fak' in reason_lower or 'no match' in reason_lower:
            logger.warning(
                f"⚠ FAK rejected (no liquidity) — resetting timer to retry next tick\n"
                f"  Reason: {reason}"
            )
            self.last_trade_time = -1  # Allow retry on next quote tick
        else:
            logger.warning(f"Order rejected: {reason}")

    # ------------------------------------------------------------------
    # Grafana / stop
    # ------------------------------------------------------------------

    def _start_grafana_sync(self):
        import asyncio
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.grafana_exporter.start())
            logger.info("Grafana metrics started on port 8000")
        except Exception as e:
            logger.error(f"Failed to start Grafana: {e}")

    def on_stop(self):
        logger.info("Integrated BTC strategy stopped")
        logger.info(f"Total paper trades recorded: {len(self.paper_trades)}")
        if self.grafana_exporter:
            import asyncio
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(self.grafana_exporter.stop())
            except Exception:
                pass

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_integrated_bot(simulation: bool = False, enable_grafana: bool = True, test_mode: bool = False):
    """Run the integrated BTC 15-min trading bot - LOADS ALL BTC MARKETS FOR THE DAY"""
    
    print("=" * 80)
    print("INTEGRATED POLYMARKET BTC 15-MIN TRADING BOT")
    print("Nautilus + 7-Phase System + Redis Control")
    print("=" * 80)

    redis_client = init_redis()

    if redis_client:
        try:
            # ALWAYS overwrite Redis with the current session mode.
            # This prevents a stale value from a previous --live run
            # silently overriding --test-mode or --simulation runs.
            mode_value = '1' if simulation else '0'
            redis_client.set('btc_trading:simulation_mode', mode_value)
            mode_label = 'SIMULATION' if simulation else 'LIVE'
            logger.info(f"Redis simulation_mode forced to: {mode_label} ({mode_value})")
        except Exception as e:
            logger.warning(f"Could not set Redis simulation mode: {e}")

    print(f"\nConfiguration:")
    print(f"  Initial Mode: {'SIMULATION' if simulation else 'LIVE TRADING'}")
    print(f"  Redis Control: {'Enabled' if redis_client else 'Disabled'}")
    print(f"  Grafana: {'Enabled' if enable_grafana else 'Disabled'}")
    print(f"  Max Trade Size: ${os.getenv('MARKET_BUY_USD', '1.00')}")
    print(f"  Quote stability gate: {QUOTE_STABILITY_REQUIRED} valid ticks")
    print()

    now = datetime.now(timezone.utc)
    
    # =========================================================================
    # Slug timestamps ARE standard Unix timestamps (no offset) aligned to
    # 15-min boundaries. Generate slugs for current + next 24 hours.
    # =========================================================================
    now = datetime.now(timezone.utc)
    unix_interval_start = (int(now.timestamp()) // 900) * 900  # current 15-min boundary

    btc_slugs = []
    for i in range(-1, 97):  # include 1 prior interval (in case we're just after boundary)
        timestamp = unix_interval_start + (i * 900)
        btc_slugs.append(f"btc-updown-15m-{timestamp}")

    filters = {
        "active": True,
        "closed": False,
        "archived": False,
        "slug": tuple(btc_slugs),
        "limit": 100,
    }

    logger.info("=" * 80)
    logger.info("LOADING BTC 15-MIN MARKETS BY SLUG")
    logger.info(f"  Interval start: {unix_interval_start} | Count: {len(btc_slugs)}")
    logger.info(f"  First: {btc_slugs[0]}  Last: {btc_slugs[-1]}")
    logger.info("=" * 80)

    instrument_cfg = InstrumentProviderConfig(
        load_all=True,
        filters=filters,
        use_gamma_markets=True,
    )

    # =========================================================================
    # Paper mode is DATA-ONLY: no execution client is registered, so the node
    # cannot place an order even in principle. The Polymarket market data feed
    # (public market websocket + public REST) needs no real account, so when the
    # operator has no credentials we inject safe placeholders. Verified by spike:
    # a data-only node connects to wss://.../ws/market and streams quotes with
    # these placeholders. The --live path keeps real creds + the exec client.
    # =========================================================================
    if simulation and not os.getenv("POLYMARKET_PK"):
        logger.warning(
            "Paper (data-only) mode with no POLYMARKET_PK — injecting placeholder "
            "credentials. The public market data feed needs no account; this node "
            "registers NO execution client and cannot place orders."
        )
        os.environ.setdefault("POLYMARKET_PK", "0x" + "1" * 64)  # valid-format throwaway key
        os.environ.setdefault("POLYMARKET_API_KEY", "paper-no-account")
        os.environ.setdefault("POLYMARKET_API_SECRET", "paper-no-account")
        os.environ.setdefault("POLYMARKET_PASSPHRASE", "paper-no-account")
        os.environ.setdefault("POLYMARKET_FUNDER", "0x" + "0" * 40)

    poly_data_cfg = PolymarketDataClientConfig(
        private_key=os.getenv("POLYMARKET_PK"),
        api_key=os.getenv("POLYMARKET_API_KEY"),
        api_secret=os.getenv("POLYMARKET_API_SECRET"),
        passphrase=os.getenv("POLYMARKET_PASSPHRASE"),
        signature_type=1,
        instrument_provider=instrument_cfg,
    )

    # Execution client is registered ONLY in --live mode. In paper mode it is
    # deliberately absent — the strongest possible guarantee against a real order.
    exec_clients = {}
    if not simulation:
        exec_clients[POLYMARKET] = PolymarketExecClientConfig(
            private_key=os.getenv("POLYMARKET_PK"),
            api_key=os.getenv("POLYMARKET_API_KEY"),
            api_secret=os.getenv("POLYMARKET_API_SECRET"),
            passphrase=os.getenv("POLYMARKET_PASSPHRASE"),
            signature_type=1,
            instrument_provider=instrument_cfg,
        )

    config = TradingNodeConfig(
        environment="live",
        trader_id="BTC-15MIN-INTEGRATED-001",
        logging=LoggingConfig(
            log_level="INFO",
            log_directory="./logs/nautilus",
        ),
        data_engine=LiveDataEngineConfig(qsize=6000),
        exec_engine=LiveExecEngineConfig(qsize=6000),
        risk_engine=LiveRiskEngineConfig(bypass=simulation),
        data_clients={POLYMARKET: poly_data_cfg},
        exec_clients=exec_clients,
    )

    strategy = IntegratedBTCStrategy(
        redis_client=redis_client,
        enable_grafana=enable_grafana,
        test_mode=test_mode,
        data_only=simulation,  # paper mode => no exec client => refuse live-switch
    )

    print("\nBuilding Nautilus node...")
    node = TradingNode(config=config)
    node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
    if not simulation:
        node.add_exec_client_factory(POLYMARKET, PolymarketLiveExecClientFactory)
    node.trader.add_strategy(strategy)
    node.build()
    logger.info(
        f"Nautilus node built successfully "
        f"({'DATA-ONLY paper' if simulation else 'LIVE with execution'} mode)"
    )

    print()
    print("=" * 80)
    print("BOT STARTING")
    print("=" * 80)

    try:
        node.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        node.dispose()
        logger.info("Bot stopped")

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Integrated BTC 15-Min Trading Bot")
    parser.add_argument("--live", action="store_true",
                        help="Run in LIVE mode (real money at risk!). Default is simulation.")
    parser.add_argument("--no-grafana", action="store_true", help="Disable Grafana metrics")
    parser.add_argument("--test-mode", action="store_true",
                        help="Run in TEST MODE (trade every minute for faster testing)")

    args = parser.parse_args()
    enable_grafana = not args.no_grafana
    test_mode = args.test_mode

    # --test-mode ALWAYS forces simulation even if --live is also passed
    if args.test_mode:
        simulation = True
    else:
        simulation = not args.live

    if not simulation:
        logger.warning("=" * 80)
        logger.warning("LIVE TRADING MODE — REAL MONEY AT RISK!")
        logger.warning("=" * 80)
    else:
        logger.info("=" * 80)
        logger.info(f"SIMULATION MODE — {'TEST MODE (fast clock)' if test_mode else 'paper trading only'}")
        logger.info("No real orders will be placed.")
        logger.info("=" * 80)

    run_integrated_bot(simulation=simulation, enable_grafana=enable_grafana, test_mode=test_mode)


if __name__ == "__main__":
    main()