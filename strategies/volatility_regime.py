"""
Adaptive Multi-Timeframe Volatility & Regime Filter Engine
==========================================================
Implements Domain 2 & Domain 4 quantitative features:
1. BollingerBandwidthFilter: Bollinger Band Squeeze & Volatility Expansion
2. MultiTimeframeRegime: 1m scalp alignment with 5m EMA slope and 15m SuperTrend/EMA
3. AdaptiveRegimeSwitcher: Dynamic toggle between Trend-Following and Mean-Reverting engines
"""

import math
from typing import List, Dict, Any, Optional, Tuple
from enum import Enum


class MarketRegime(str, Enum):
    TREND_MOMENTUM = "TREND_MOMENTUM"   # High ADX (>30), Directional trend expansion -> Direct Momentum
    CHOP_CONSOLIDATION = "CHOP_CONSOLIDATION" # High Choppiness / Low ADX (<20) -> Inverted Exhaustion Fading
    VOLATILITY_BURST = "VOLATILITY_BURST" # Squeeze breakout -> Aggressive entry
    NEUTRAL = "NEUTRAL"


def compute_bollinger_bandwidth(closes: List[float], period: int = 20, num_std: float = 2.0) -> Tuple[List[float], List[float]]:
    """
    Computes Bollinger Bandwidth (BBW) and rolling lowest BBW:
    BBW = (Upper - Lower) / SMA
    """
    n = len(closes)
    if n < period:
        return [0.0] * n, [0.0] * n

    bbw_series = [0.0] * n
    for i in range(period - 1, n):
        slice_vals = closes[i - period + 1:i + 1]
        mean = sum(slice_vals) / period
        variance = sum((x - mean) ** 2 for x in slice_vals) / period
        std = math.sqrt(variance)
        upper = mean + (num_std * std)
        lower = mean - (num_std * std)
        bbw_series[i] = (upper - lower) / mean if mean > 0 else 0.0

    # Rolling lowest BBW over period
    lowest_bbw = [0.0] * n
    for i in range(period - 1, n):
        lowest_bbw[i] = min(bbw_series[max(0, i - period + 1):i + 1])

    return bbw_series, lowest_bbw


def compute_choppiness_index(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
    """
    Computes Choppiness Index (CHOP):
    CHOP = 100 * log10(sum(TR, period) / (max(high, period) - min(low, period))) / log10(period)
    Values > 61.8 indicate dead chop / consolidation.
    Values < 38.2 indicate strong trending.
    """
    n = len(closes)
    if n < period + 1:
        return [50.0] * n

    # Compute True Range
    tr_series = [0.0] * n
    tr_series[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr_series[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )

    chop_series = [50.0] * n
    log_period = math.log10(period)

    for i in range(period, n):
        sum_tr = sum(tr_series[i - period + 1:i + 1])
        max_high = max(highs[i - period + 1:i + 1])
        min_low = min(lows[i - period + 1:i + 1])
        range_hl = max_high - min_low

        if range_hl > 1e-9 and sum_tr > 1e-9:
            ratio = sum_tr / range_hl
            if ratio > 0:
                chop = 100.0 * (math.log10(ratio) / log_period)
                chop_series[i] = max(0.0, min(100.0, chop))

    return chop_series


class AdaptiveRegimeSwitcher:
    """
    Dynamically identifies current market regime and recommends optimal strategy mode:
    - In Chop / Consolidation (CHOP > 55 or ADX < 20): Use Inverted Exhaustion Fading
    - In Strong Breakout Trend (ADX > 30): Use Direct Momentum
    - In Volatility Squeeze Breakout: Use Volatility Compression Breakout
    """

    def __init__(self, chop_period: int = 14, adx_period: int = 14):
        self.chop_period = chop_period
        self.adx_period = adx_period

    def classify_regime(
        self,
        highs: List[float],
        lows: List[float],
        closes: List[float],
        adx_value: float
    ) -> Tuple[MarketRegime, bool]:
        """
        Returns: (MarketRegime, recommended_invert_signal)
        """
        if len(closes) < 20:
            return MarketRegime.NEUTRAL, False

        chop_series = compute_choppiness_index(highs, lows, closes, period=self.chop_period)
        curr_chop = chop_series[-1] if chop_series else 50.0

        bbw, lowest_bbw = compute_bollinger_bandwidth(closes, period=20)
        curr_bbw = bbw[-1] if bbw else 0.0
        min_bbw = lowest_bbw[-1] if lowest_bbw else 0.0
        is_squeeze = curr_bbw <= min_bbw * 1.05 and curr_bbw > 0.0

        if is_squeeze:
            return MarketRegime.VOLATILITY_BURST, False
        elif curr_chop > 55.0 or adx_value < 20.0:
            # Consolidation regime -> Invert signal for exhaustion fading
            return MarketRegime.CHOP_CONSOLIDATION, True
        elif adx_value > 30.0 and curr_chop < 45.0:
            # Strong trend regime -> Direct momentum
            return MarketRegime.TREND_MOMENTUM, False
        else:
            return MarketRegime.NEUTRAL, False
