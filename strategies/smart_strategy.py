"""
Smart Strategy (Regime-Adaptive Micro-Scalping Engine)
======================================================
Autonomous multi-regime trading strategy that dynamically classifies the
1-minute microstructure and routes order execution between:
1. Momentum Breakout Engine (`EMA_CROSSOVER`) during strong directional trends.
2. Rotational Mean-Reversion Engine (`STOCH_RSI`) during balanced ranging markets.
3. Automated System Pauses during sub-ATR compression or volatility climaxes.

Features:
- O(1) Constant-Time Market Regime Classifier
- Universal Asset Generalization (thresholds scale by contract price_unit / tick size)
- Toggleable 200 EMA Direction Lock (defaults to OFF per empirical testing)
- Pure-Python Wilder's ATR, ADX, and Choppiness Index (CHOP) calculations
- Seamless compatibility with live execution, dry-run simulation, and backtesting
"""

from __future__ import annotations
import math
import time
import logging
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from kcex.market import KCEXMarket

from kcex.engine.models import OrderDirection, TradeSignal, TradeOutcome
from strategies.base import BaseStrategy
from strategies.ema_crossover import EMACrossoverStrategy, compute_ema_series
from strategies.stoch_rsi import StochasticRSIStrategy
from strategies.filters import compute_atr_series, compute_adx_series

logger = logging.getLogger("SmartStrategy")


class MarketRegime(str, Enum):
    SUB_ATR_COMPRESSION = "SUB_ATR_COMPRESSION"       # Volatility too low for target traversability
    VOLATILITY_CLIMAX = "VOLATILITY_CLIMAX"           # Parabolic surge / spread-sweep risk
    STRONG_BULL_MOMENTUM = "STRONG_BULL_MOMENTUM"     # Clean upward trend -> EMA Longs
    STRONG_BEAR_MOMENTUM = "STRONG_BEAR_MOMENTUM"     # Clean downward trend -> EMA Shorts
    BALANCED_RANGE = "BALANCED_RANGE"                 # Mean-reverting range -> Stoch RSI


def compute_chop_series(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14
) -> List[float]:
    """
    Computes the Choppiness Index (CHOP) series:
    CHOP = 100 * LOG10( SUM(TR, period) / (MAX(HIGH, period) - MIN(LOW, period)) ) / LOG10(period)
    Values >= 61.8 indicate extreme consolidation.
    Values <= 38.2 indicate strong trending momentum.
    """
    n = len(closes)
    if n == 0:
        return []
    if n != len(highs) or n != len(lows):
        raise ValueError("highs, lows, and closes series must have identical length")

    # 1. Compute True Range (TR)
    tr_series = [0.0] * n
    tr_series[0] = highs[0] - lows[0]
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr_series[i] = max(hl, hc, lc)

    chop_series = [50.0] * n
    log_period = math.log10(period) if period > 1 else 1.0

    for i in range(period - 1, n):
        tr_sum = sum(tr_series[i - period + 1:i + 1])
        highest_high = max(highs[i - period + 1:i + 1])
        lowest_low = min(lows[i - period + 1:i + 1])
        hl_diff = highest_high - lowest_low

        if hl_diff > 1e-12 and tr_sum > 0:
            ratio = tr_sum / hl_diff
            if ratio > 0:
                chop_val = 100.0 * (math.log10(ratio) / log_period)
                chop_series[i] = max(0.0, min(100.0, chop_val))
            else:
                chop_series[i] = 50.0
        else:
            chop_series[i] = 50.0

    return chop_series


class SmartStrategy(BaseStrategy):
    """
    Dynamic Regime-Adaptive Strategy Coordinator.
    Classifies market conditions in O(1) time and delegates execution
    to the optimal underlying sub-strategy.
    """

    def __init__(
        self,
        market: KCEXMarket,
        symbol: str,
        interval: str = "Min1",
        preferred_direction: Optional[OrderDirection] = None,
        cooldown_seconds: float = 10.0,
        require_closed_candle: bool = True,
        auto_start_feed: bool = False,
        # Smart Regime Gating Parameters
        atr_filter_enabled: bool = True,
        min_atr_ticks: float = 2.5,
        chop_ceiling: float = 58.0,
        adx_trend_threshold: float = 26.0,
        use_ema200_filter: bool = False,          # Default OFF per empirical validation
        ema200_period: int = 200,
        climax_filter_enabled: bool = True,
        max_atr_expansion: float = 2.2,
        # Sub-strategy hyperparameters
        ema_preset: str = "5/13",
        ema_fast: int = 5,
        ema_slow: int = 13,
        stoch_preset: str = "FAST_SCALP",
        stoch_rsi_period: int = 9,
        stoch_period: int = 9,
        stoch_k_period: int = 3,
        stoch_d_period: int = 3,
        stoch_oversold: float = 20.0,
        stoch_overbought: float = 80.0,
        stoch_zone_filter: bool = True,
        # Phase V2.1 & V2.2 Quantitative Feature Toggles
        invert_signal: bool = False,
        dynamic_regime_fading: bool = False,
        adx_fading_cutoff: float = 28.0,
    ):
        super().__init__(name="SmartStrategy")
        self.market = market
        self.symbol = symbol.upper()
        self.interval = interval
        self.preferred_direction = preferred_direction
        self.cooldown_seconds = cooldown_seconds
        self.require_closed_candle = require_closed_candle
        self.invert_signal = invert_signal
        self.dynamic_regime_fading = dynamic_regime_fading
        self.adx_fading_cutoff = adx_fading_cutoff

        # Regime Gating Thresholds
        self.atr_filter_enabled = atr_filter_enabled
        self.min_atr_ticks = min_atr_ticks
        self.chop_ceiling = chop_ceiling
        self.adx_trend_threshold = adx_trend_threshold
        self.use_ema200_filter = use_ema200_filter
        self.ema200_period = ema200_period
        self.climax_filter_enabled = climax_filter_enabled
        self.max_atr_expansion = max_atr_expansion

        # Internal state tracking
        self.trade_in_progress: bool = False
        self.last_trade_closed_at: Optional[float] = None
        self.last_signal_candle_ts: Optional[int] = None
        self.current_regime: MarketRegime = MarketRegime.BALANCED_RANGE
        self.last_rejection_reason: Optional[str] = None
        self.last_diagnostics: Dict[str, Any] = {}

        # Cached contract info for tick size scaling
        self._price_unit: float = 0.001
        self._price_precision: int = 4
        self._refresh_contract_spec()

        # Instantiate sub-strategies
        pref_dir = self.preferred_direction
        self.ema_strategy = EMACrossoverStrategy(
            market=self.market,
            symbol=self.symbol,
            fast_period=ema_fast,
            slow_period=ema_slow,
            ema_preset=ema_preset,
            interval=self.interval,
            preferred_direction=pref_dir,
            cooldown_seconds=self.cooldown_seconds,
            require_closed_candle=self.require_closed_candle,
            auto_start_feed=auto_start_feed
        )

        self.stoch_strategy = StochasticRSIStrategy(
            market=self.market,
            symbol=self.symbol,
            rsi_period=stoch_rsi_period,
            stoch_period=stoch_period,
            k_period=stoch_k_period,
            d_period=stoch_d_period,
            oversold=stoch_oversold,
            overbought=stoch_overbought,
            stoch_preset=stoch_preset,
            interval=self.interval,
            preferred_direction=pref_dir,
            cooldown_seconds=self.cooldown_seconds,
            zone_filter=stoch_zone_filter,
            require_closed_candle=self.require_closed_candle,
            auto_start_feed=auto_start_feed,
            invert_signal=self.invert_signal,
            dynamic_regime_fading=self.dynamic_regime_fading,
            adx_fading_cutoff=self.adx_fading_cutoff
        )

    def _refresh_contract_spec(self) -> None:
        """Inspects contract price unit and precision for universal scaling."""
        try:
            contract = self.market.get_contract_detail(self.symbol)
            if contract:
                self._price_unit = contract.price_unit
                self._price_precision = contract.price_precision
        except Exception:
            pass

    def start(self) -> None:
        self.ema_strategy.start()
        self.stoch_strategy.start()

    def stop(self) -> None:
        self.ema_strategy.stop()
        self.stoch_strategy.stop()

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

    def on_trade_completed(self, outcome: TradeOutcome) -> None:
        self.trade_in_progress = False
        now = time.time()
        self.last_trade_closed_at = now
        self.ema_strategy.on_trade_completed(outcome)
        self.stoch_strategy.on_trade_completed(outcome)

    def on_trade_rejected(self) -> None:
        self.trade_in_progress = False
        if hasattr(self.ema_strategy, "on_trade_rejected"):
            self.ema_strategy.on_trade_rejected()
        elif hasattr(self.ema_strategy, "trade_in_progress"):
            self.ema_strategy.trade_in_progress = False

        if hasattr(self.stoch_strategy, "on_trade_rejected"):
            self.stoch_strategy.on_trade_rejected()
        elif hasattr(self.stoch_strategy, "trade_in_progress"):
            self.stoch_strategy.trade_in_progress = False

    def classify_regime(
        self,
        candles: List[Any],
        pu: Optional[float] = None
    ) -> Tuple[MarketRegime, Dict[str, Any]]:
        """
        Classifies current market regime using 1m candlestick history.
        Returns:
            (MarketRegime, metrics_dict)
        """
        price_unit = pu or self._price_unit
        n = len(candles)
        if n < 30:
            return MarketRegime.BALANCED_RANGE, {"reason": "insufficient_history"}

        highs: List[float] = []
        lows: List[float] = []
        closes: List[float] = []

        for c in candles:
            if hasattr(c, "high") and hasattr(c, "low") and hasattr(c, "close"):
                highs.append(float(c.high))
                lows.append(float(c.low))
                closes.append(float(c.close))
            elif isinstance(c, dict):
                highs.append(float(c.get("high", 0.0)))
                lows.append(float(c.get("low", 0.0)))
                closes.append(float(c.get("close", 0.0)))
            elif isinstance(c, (list, tuple)) and len(c) >= 5:
                highs.append(float(c[2]))
                lows.append(float(c[3]))
                closes.append(float(c[4]))

        # Calculate indicators
        atr_series = compute_atr_series(highs, lows, closes, period=14)
        adx_series, plus_di, minus_di = compute_adx_series(highs, lows, closes, period=14)
        chop_series = compute_chop_series(highs, lows, closes, period=14)

        current_atr = atr_series[-1] if atr_series else 0.0
        current_adx = adx_series[-1] if adx_series else 0.0
        current_plus_di = plus_di[-1] if plus_di else 0.0
        current_minus_di = minus_di[-1] if minus_di else 0.0
        current_chop = chop_series[-1] if chop_series else 50.0
        current_price = closes[-1] if closes else 0.0

        # Rolling baseline ATR (SMA of last 30 bars) for expansion ratio checks
        baseline_slice = atr_series[-30:] if len(atr_series) >= 30 else atr_series
        atr_baseline = sum(baseline_slice) / len(baseline_slice) if baseline_slice else current_atr
        atr_expansion = (current_atr / atr_baseline) if atr_baseline > 1e-12 else 1.0

        atr_in_ticks = current_atr / price_unit if price_unit > 0 else 0.0

        last_candle_range = (highs[-1] - lows[-1]) / atr_baseline if atr_baseline > 1e-12 else 1.0

        metrics = {
            "atr": current_atr,
            "atr_ticks": atr_in_ticks,
            "atr_baseline": atr_baseline,
            "atr_expansion": atr_expansion,
            "last_candle_range_ratio": last_candle_range,
            "adx": current_adx,
            "plus_di": current_plus_di,
            "minus_di": current_minus_di,
            "chop": current_chop,
            "price": current_price
        }

        # Check 1: Sub-ATR Compression / Liquidity Gridlock (Pause)
        if self.atr_filter_enabled:
            if atr_in_ticks < self.min_atr_ticks or current_chop >= self.chop_ceiling:
                metrics["rejection"] = f"ATR {atr_in_ticks:.1f}t < {self.min_atr_ticks:.1f}t or CHOP {current_chop:.1f} >= {self.chop_ceiling:.1f}"
                return MarketRegime.SUB_ATR_COMPRESSION, metrics

        # Check 2: Volatility Climax / Spread-Sweep Risk (Circuit Breaker)
        if self.climax_filter_enabled:
            if atr_expansion > self.max_atr_expansion or last_candle_range > self.max_atr_expansion:
                metrics["rejection"] = f"Climax: ATR Exp {atr_expansion:.2f}x or Candle Range {last_candle_range:.2f}x > {self.max_atr_expansion:.2f}x"
                return MarketRegime.VOLATILITY_CLIMAX, metrics

        # Check 3: Macro Directional Trend Regimes
        if current_adx >= self.adx_trend_threshold:
            # 200 EMA check if enabled by user
            ema200_ok_long = True
            ema200_ok_short = True
            if self.use_ema200_filter and n >= 50:
                ema200_period = min(self.ema200_period, n)
                ema200_series = compute_ema_series(closes, ema200_period)
                if ema200_series:
                    ema200_val = ema200_series[-1]
                    metrics["ema200"] = ema200_val
                    ema200_ok_long = current_price >= ema200_val
                    ema200_ok_short = current_price <= ema200_val

            if current_plus_di > current_minus_di and ema200_ok_long:
                return MarketRegime.STRONG_BULL_MOMENTUM, metrics
            elif current_minus_di > current_plus_di and ema200_ok_short:
                return MarketRegime.STRONG_BEAR_MOMENTUM, metrics

        # Check 4: Balanced Rotational Range
        return MarketRegime.BALANCED_RANGE, metrics

    def generate_signal(self, symbol: str) -> Optional[TradeSignal]:
        now = time.time()
        if not self.should_generate_signal(now):
            return None

        # Fetch latest candles for regime classification
        # In live mode: query market klines; In backtest mode: candles already seeded
        bars = self.market.get_klines(self.symbol, interval=self.interval, limit=100)
        if not bars or len(bars) < 30:
            return None

        # Synchronize contract tick specifications
        self._refresh_contract_spec()

        regime, metrics = self.classify_regime(bars, pu=self._price_unit)
        self.current_regime = regime
        self.last_diagnostics = metrics

        # 1. Dormant or dangerous regimes -> Do not trade
        if regime == MarketRegime.SUB_ATR_COMPRESSION:
            self.last_rejection_reason = f"PAUSED: Sub-ATR Compression ({metrics.get('rejection', '')})"
            return None

        if regime == MarketRegime.VOLATILITY_CLIMAX:
            self.last_rejection_reason = f"CIRCUIT_BREAKER: Volatility Climax ({metrics.get('rejection', '')})"
            return None

        # 2. Trending Regimes -> Route to EMA_CROSSOVER (Direction-Locked)
        if regime in (MarketRegime.STRONG_BULL_MOMENTUM, MarketRegime.STRONG_BEAR_MOMENTUM):
            is_bull = (regime == MarketRegime.STRONG_BULL_MOMENTUM)
            target_dir = OrderDirection.LONG if is_bull else OrderDirection.SHORT

            # If user has configured a static preferred_direction, enforce it
            if self.preferred_direction is not None and self.preferred_direction != target_dir:
                self.last_rejection_reason = f"Trend direction {target_dir.value} blocked by preferred_direction {self.preferred_direction.value}"
                return None

            # Temporarily configure EMA strategy direction
            self.ema_strategy.preferred_direction = target_dir
            signal = self.ema_strategy.generate_signal(self.symbol)

            if signal:
                # Strictly enforce trend alignment
                if signal.direction == target_dir:
                    self.trade_in_progress = True
                    signal.metadata.update({
                        "strategy_mode": "SMART_STRATEGY",
                        "active_sub_strategy": "EMA_CROSSOVER",
                        "market_regime": regime.value,
                        "adx": metrics.get("adx", 0.0),
                        "atr_ticks": metrics.get("atr_ticks", 0.0)
                    })
                    signal.sub_strategy_name = f"SmartStrategy(EMA-{target_dir.value})"
                    return signal
                else:
                    logger.debug("Counter-trend EMA cross suppressed in %s", regime.value)
                    return None
            return None

        # 3. Balanced Range Regime -> Route to STOCH_RSI (Bi-Directional Scalp)
        if regime == MarketRegime.BALANCED_RANGE:
            self.stoch_strategy.preferred_direction = self.preferred_direction
            signal = self.stoch_strategy.generate_signal(self.symbol)

            if signal:
                self.trade_in_progress = True
                signal.metadata.update({
                    "strategy_mode": "SMART_STRATEGY",
                    "active_sub_strategy": "STOCH_RSI",
                    "market_regime": regime.value,
                    "adx": metrics.get("adx", 0.0),
                    "chop": metrics.get("chop", 0.0),
                    "atr_ticks": metrics.get("atr_ticks", 0.0)
                })
                signal.sub_strategy_name = f"SmartStrategy(StochRSI-{signal.direction.value})"
                return signal
            return None

        return None

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "strategy": self.name,
            "symbol": self.symbol,
            "interval": self.interval,
            "smart_atr_filter_enabled": self.atr_filter_enabled,
            "smart_min_atr_ticks": self.min_atr_ticks,
            "smart_chop_ceiling": self.chop_ceiling,
            "smart_adx_trend_threshold": self.adx_trend_threshold,
            "smart_use_ema200_filter": self.use_ema200_filter,
            "smart_ema200_period": self.ema200_period,
            "smart_climax_filter_enabled": self.climax_filter_enabled,
            "smart_max_atr_expansion": self.max_atr_expansion,
            "ema_params": self.ema_strategy.get_parameters(),
            "stoch_params": self.stoch_strategy.get_parameters(),
        }

    def get_diagnostics(self) -> Dict[str, Any]:
        diag = {
            "current_regime": self.current_regime.value if hasattr(self.current_regime, "value") else str(self.current_regime),
            "trade_in_progress": self.trade_in_progress,
            "last_rejection_reason": self.last_rejection_reason,
            "metrics": self.last_diagnostics,
            "ema_diagnostics": self.ema_strategy.get_diagnostics(),
            "stoch_diagnostics": self.stoch_strategy.get_diagnostics(),
        }
        return diag


# Backwards compatibility alias
SmartSubStrategy = SmartStrategy
