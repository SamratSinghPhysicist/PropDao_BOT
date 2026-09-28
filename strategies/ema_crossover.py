"""
Exponential Moving Average (EMA) Crossover Strategy
===================================================
Institutional Fast / Slow EMA Crossover trading strategy.
Features:
- Pure-Python EMA series computation with Wilder-compatible alpha smoothing
- Fibonacci (5/13), Momentum (9/21), and Micro-Scalp (3/8) presets
- Closed candle confirmation to prevent mid-candle repainting
- Strict per-bar signal deduplication
- Autonomous bi-directional or single-direction gating
- Real-time diagnostic metrics and parameter reporting
"""

from __future__ import annotations
import time
import logging
from typing import Optional, Dict, Any, List, TYPE_CHECKING
if TYPE_CHECKING:
    from kcex.market import KCEXMarket
from kcex.engine.models import OrderDirection, TradeSignal, TradeOutcome
from strategies.base import BaseStrategy

logger = logging.getLogger("EMACrossoverStrategy")

EMA_PRESETS: Dict[str, Dict[str, Any]] = {
    "5/13": {"fast": 5, "slow": 13, "desc": "Fibonacci Scalp (5 / 13) [Default]"},
    "9/21": {"fast": 9, "slow": 21, "desc": "Momentum / Trend Scalp (9 / 21)"},
    "3/8":  {"fast": 3, "slow": 8,  "desc": "Ultra-Fast Micro-Scalp (3 / 8)"},
}


def compute_ema_series(prices: List[float], period: int) -> List[float]:
    """
    Computes an Exponential Moving Average (EMA) series:
    alpha = 2 / (period + 1)
    Initial seed at index period - 1 is the SMA of the first `period` prices,
    followed by recursive smoothing: EMA[i] = Price[i] * alpha + EMA[i-1] * (1 - alpha).
    Indices 0 to period - 2 are populated with partial running SMA to preserve exact length.
    """
    if not prices:
        return []
    if period <= 1:
        return list(prices)

    n = len(prices)
    alpha = 2.0 / (period + 1.0)
    if n < period:
        res = [prices[0]]
        for p in prices[1:]:
            res.append(p * alpha + res[-1] * (1.0 - alpha))
        return res

    # Running partial averages for warmup
    res: List[float] = []
    running_sum = 0.0
    for i in range(period - 1):
        running_sum += prices[i]
        res.append(running_sum / (i + 1))

    # SMA at index period - 1
    running_sum += prices[period - 1]
    sma = running_sum / period
    res.append(sma)

    # Recursive EMA smoothing
    for p in prices[period:]:
        ema_val = p * alpha + res[-1] * (1.0 - alpha)
        res.append(ema_val)

    return res


class EMACrossoverStrategy(BaseStrategy):
    """
    Fast / Slow Exponential Moving Average (EMA) Crossover Strategy.
    Generates:
      - LONG on Golden Cross (Fast EMA crosses above Slow EMA)
      - SHORT on Death Cross (Fast EMA crosses below Slow EMA)
    """

    def __init__(
        self,
        market: KCEXMarket,
        symbol: str,
        fast_period: int = 5,
        slow_period: int = 13,
        ema_preset: Optional[str] = "5/13",
        interval: str = "Min1",
        preferred_direction: Optional[OrderDirection] = None,
        cooldown_seconds: float = 10.0,
        require_closed_candle: bool = True,
        lookback_bars: int = 1,
        auto_start_feed: bool = False,
        name: str = "EMACrossover"
    ):
        super().__init__(name=name)
        self.market = market
        self.symbol = symbol.upper()
        self.interval = interval
        self.preferred_direction = preferred_direction
        self.cooldown_seconds = cooldown_seconds
        self.require_closed_candle = require_closed_candle
        self.lookback_bars = max(1, lookback_bars)

        # Resolve preset
        if ema_preset and ema_preset in EMA_PRESETS:
            self.fast_period = EMA_PRESETS[ema_preset]["fast"]
            self.slow_period = EMA_PRESETS[ema_preset]["slow"]
            self.ema_preset = ema_preset
        else:
            self.fast_period = fast_period
            self.slow_period = slow_period
            self.ema_preset = f"{fast_period}/{slow_period}"

        if self.fast_period >= self.slow_period:
            logger.warning(
                "Warning: Fast EMA (%d) >= Slow EMA (%d). Swapping to maintain fast < slow.",
                self.fast_period, self.slow_period
            )
            self.fast_period, self.slow_period = self.slow_period, self.fast_period

        self.last_trade_closed_at: Optional[float] = None
        self.trade_in_progress: bool = False
        self.completed_trades_count: int = 0

        # State tracking
        self.candles: List[Dict[str, Any]] = []
        self.last_kline_fetch_ts: float = 0.0
        self.last_signal_candle_ts: Optional[int] = None
        self.last_crossover_type: Optional[str] = None
        self.latest_deal_price: Optional[float] = None

        # Optional feed for real-time tick streaming
        self.feed = None
        if auto_start_feed:
            try:
                from kcex.feed import KCEXWebSocketFeed
                contract = self.market.get_contract_detail(self.symbol)
                depth_step = contract.depth_steps[0] if contract.depth_steps else str(contract.price_unit)
                self.feed = KCEXWebSocketFeed(
                    symbol=self.symbol,
                    depth_step=depth_step,
                    on_deal=self._on_deal
                )
                self.feed.start()
            except Exception as e:
                logger.warning("Could not start WS feed for EMA strategy: %s", e)

        # Seed initial candles
        self._refresh_klines(force=True)

    def _on_deal(self, price: float, volume: float, side: str, ts: float) -> None:
        self.latest_deal_price = price
        if not self.require_closed_candle and self.candles:
            self.candles[-1]["close"] = price

    def _refresh_klines(self, force: bool = False) -> None:
        now = time.time()
        # Rate-limit kline fetching to at most once per 1.5 seconds unless forced
        if not force and (now - self.last_kline_fetch_ts < 1.5):
            return
        try:
            bars = self.market.get_klines(self.symbol, interval=self.interval, limit=100)
            if bars and len(bars) >= 5:
                self.candles = bars
                self.last_kline_fetch_ts = now
        except Exception as e:
            logger.warning("Failed to refresh klines for %s: %s", self.symbol, e)

    def start(self) -> None:
        if self.feed and not self.feed.is_connected:
            self.feed.start()

    def stop(self) -> None:
        if self.feed:
            self.feed.stop()

    def should_generate_signal(self, current_time: float) -> bool:
        if self.trade_in_progress:
            return False
        if self.last_trade_closed_at is None:
            return True
        elapsed = current_time - self.last_trade_closed_at
        return elapsed >= self.cooldown_seconds

    def get_remaining_cooldown(self, current_time: float) -> float:
        if self.trade_in_progress or self.last_trade_closed_at is None:
            return 0.0
        elapsed = current_time - self.last_trade_closed_at
        remaining = self.cooldown_seconds - elapsed
        return max(0.0, remaining)

    def generate_signal(self, symbol: str) -> Optional[TradeSignal]:
        now = time.time()
        if not self.should_generate_signal(now):
            return None

        self._refresh_klines()
        min_required = max(self.slow_period + 3, 10)
        if len(self.candles) < min_required:
            logger.debug("EMA Crossover waiting for candle history (have %d, need %d)", len(self.candles), min_required)
            return None

        closes = [float(c.get("close", 0.0)) for c in self.candles]
        fast_series = compute_ema_series(closes, self.fast_period)
        slow_series = compute_ema_series(closes, self.slow_period)

        # Determine evaluation candle index:
        # If require_closed_candle=True, candle[-1] is still forming, so closed candle is at index -2.
        eval_idx = len(closes) - 2 if self.require_closed_candle else len(closes) - 1
        if eval_idx < 1:
            return None

        detected_signal_dir: Optional[OrderDirection] = None
        crossover_type: Optional[str] = None
        signal_candle_ts: Optional[int] = None
        candle_close: float = closes[eval_idx]

        # Scan lookback_bars backwards for the most recent crossover
        for offset in range(self.lookback_bars):
            idx = eval_idx - offset
            if idx < 1:
                break

            prev_fast = fast_series[idx - 1]
            prev_slow = slow_series[idx - 1]
            curr_fast = fast_series[idx]
            curr_slow = slow_series[idx]

            # Bullish Golden Cross
            if prev_fast <= prev_slow and curr_fast > curr_slow:
                if fast_series[eval_idx] > slow_series[eval_idx]:
                    detected_signal_dir = OrderDirection.LONG
                    crossover_type = "GOLDEN_CROSS"
                    signal_candle_ts = int(self.candles[idx].get("timestamp", 0))
                    candle_close = closes[idx]
                    break

            # Bearish Death Cross
            elif prev_fast >= prev_slow and curr_fast < curr_slow:
                if fast_series[eval_idx] < slow_series[eval_idx]:
                    detected_signal_dir = OrderDirection.SHORT
                    crossover_type = "DEATH_CROSS"
                    signal_candle_ts = int(self.candles[idx].get("timestamp", 0))
                    candle_close = closes[idx]
                    break

        if not detected_signal_dir or signal_candle_ts is None:
            return None

        # Deduplication: avoid multiple signals on the same crossover bar
        if self.last_signal_candle_ts is not None and self.last_signal_candle_ts >= signal_candle_ts:
            return None

        # Preferred direction filtering
        if self.preferred_direction and detected_signal_dir != self.preferred_direction:
            logger.debug(
                "EMA %s signal ignored due to preferred direction (%s)",
                detected_signal_dir.value,
                self.preferred_direction.value
            )
            return None

        # Lock signal
        self.last_signal_candle_ts = signal_candle_ts
        self.last_crossover_type = crossover_type
        self.trade_in_progress = True

        cur_f = fast_series[eval_idx]
        cur_s = slow_series[eval_idx]
        diff = cur_f - cur_s
        diff_pct = (diff / cur_s * 100.0) if cur_s != 0 else 0.0

        metadata = {
            "preset": self.ema_preset,
            "fast_period": self.fast_period,
            "slow_period": self.slow_period,
            "interval": self.interval,
            "crossover_type": crossover_type,
            "candle_timestamp": signal_candle_ts,
            "candle_close": candle_close,
            "fast_ema": cur_f,
            "slow_ema": cur_s,
            "ema_diff": diff,
            "ema_diff_pct": diff_pct
        }

        logger.info(
            "🚀 [EMA CROSSOVER SIGNAL] %s (%s) on %s %s | Fast(%d)=%.4f, Slow(%d)=%.4f (Diff: %+.4f / %+.2f%%)",
            detected_signal_dir.value,
            crossover_type,
            self.symbol,
            self.interval,
            self.fast_period,
            cur_f,
            self.slow_period,
            cur_s,
            diff,
            diff_pct
        )

        return TradeSignal(
            symbol=self.symbol,
            direction=detected_signal_dir,
            sub_strategy_name=f"{self.name}({self.ema_preset}-{detected_signal_dir.value})",
            timestamp=now,
            metadata=metadata
        )

    def on_trade_completed(self, outcome: TradeOutcome) -> None:
        self.trade_in_progress = False
        self.last_trade_closed_at = outcome.close_time or time.time()
        self.completed_trades_count += 1
        logger.info(
            "EMA strategy completed trade #%d. Cooldown %ds initiated.",
            outcome.trade_id,
            int(self.cooldown_seconds)
        )

    def on_trade_rejected(self) -> None:
        """Resets trade_in_progress when candidate signal is suppressed by a regime filter."""
        self.trade_in_progress = False

    def get_parameters(self) -> Dict[str, Any]:
        """Returns strategy configuration parameters for reporting and analytics."""
        return {
            "strategy": "EMA_CROSSOVER",
            "ema_preset": self.ema_preset,
            "ema_fast": self.fast_period,
            "ema_slow": self.slow_period,
            "ema_interval": self.interval,
            "ema_require_closed_candle": self.require_closed_candle,
            "lookback_bars": self.lookback_bars,
            "cooldown_seconds": self.cooldown_seconds,
            "preferred_direction": self.preferred_direction.value if self.preferred_direction else "BOTH"
        }

    def get_diagnostics(self) -> Dict[str, Any]:
        now = time.time()
        fast_val = 0.0
        slow_val = 0.0
        diff = 0.0
        diff_pct = 0.0
        trend = "NEUTRAL"
        time_to_bar_close = 0.0

        if self.candles and len(self.candles) >= max(self.slow_period, 5):
            closes = [float(c.get("close", 0.0)) for c in self.candles]
            fast_series = compute_ema_series(closes, self.fast_period)
            slow_series = compute_ema_series(closes, self.slow_period)
            fast_val = fast_series[-1]
            slow_val = slow_series[-1]
            diff = fast_val - slow_val
            diff_pct = (diff / slow_val * 100.0) if slow_val != 0 else 0.0
            trend = "BULLISH" if diff > 0 else ("BEARISH" if diff < 0 else "NEUTRAL")

            span_map = {"Min1": 60, "Min5": 300, "Min15": 900, "Min30": 1800, "Min60": 3600}
            bar_span = span_map.get(self.interval, 60)
            last_ts = self.candles[-1].get("timestamp", now)
            elapsed_in_bar = now - last_ts
            time_to_bar_close = max(0.0, bar_span - elapsed_in_bar)

        return {
            "strategy": "EMA_CROSSOVER",
            "preset": self.ema_preset,
            "fast_period": self.fast_period,
            "slow_period": self.slow_period,
            "interval": self.interval,
            "fast_ema": fast_val,
            "slow_ema": slow_val,
            "diff": diff,
            "diff_pct": diff_pct,
            "trend": trend,
            "time_to_bar_close_s": round(time_to_bar_close, 1),
            "last_crossover": self.last_crossover_type,
            "cooldown_remaining_s": round(self.get_remaining_cooldown(now), 1),
            "trade_in_progress": self.trade_in_progress
        }


# Backwards compatibility alias
EMACrossoverSubStrategy = EMACrossoverStrategy
