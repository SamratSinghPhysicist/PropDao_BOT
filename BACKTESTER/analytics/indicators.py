"""
Expanded Quantitative Indicators & Market Microstructure Library
================================================================
Institutional-grade indicator calculators to provide LLM quantitative analysts
with deep multi-dimensional market regime, volatility, momentum, and volume context.

Indicators implemented:
- RSI (Relative Strength Index, Wilder's 14)
- Bollinger Bands, %B, and Bandwidth (20-period, 2.0 std dev)
- MACD (12, 26, 9: Line, Signal, Histogram)
- Choppiness Index (CHOP, 14-period)
- Volume Surge Ratio (Current Volume / 20-SMA Volume)
- Rolling Session VWAP & VWAP Distance (%)
- Candle Anatomy: Upper Wick, Lower Wick, Body-to-Range Ratio
- Multi-Horizon EMA Alignment (Fast, Slow, 50, 200 EMA) & Slope
"""

import math
from typing import List, Dict, Any, Optional, Tuple

from strategies.ema_crossover import compute_ema_series
from strategies.stoch_rsi import compute_stoch_rsi
from strategies.filters import compute_atr_series, compute_adx_series


# =============================================================================
# MOMENTUM & VOLATILITY CALCULATORS
# =============================================================================

def compute_rsi_series(closes: List[float], period: int = 14) -> List[float]:
    """
    Computes Wilder's Relative Strength Index (RSI).
    """
    n = len(closes)
    if n == 0:
        return []
    if n <= period:
        return [50.0] * n

    gains = [0.0] * n
    losses = [0.0] * n

    for i in range(1, n):
        delta = closes[i] - closes[i - 1]
        if delta > 0:
            gains[i] = delta
        else:
            losses[i] = -delta

    rsi_series = [50.0] * n

    # Seed initial averages with simple moving average
    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period

    if avg_loss == 0:
        rsi_series[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi_series[period] = round(100.0 - (100.0 / (1.0 + rs)), 2)

    # Wilder's smoothing
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi_series[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_series[i] = round(100.0 - (100.0 / (1.0 + rs)), 2)

    return rsi_series


def compute_bollinger_bands(
    closes: List[float],
    period: int = 20,
    num_std: float = 2.0
) -> Tuple[List[float], List[float], List[float], List[float], List[float]]:
    """
    Computes Bollinger Bands: Upper, Middle (SMA), Lower, %B, and Bandwidth.
    %B = (Price - Lower) / (Upper - Lower)
    Bandwidth = (Upper - Lower) / Middle
    """
    n = len(closes)
    if n == 0:
        return [], [], [], [], []

    upper = [0.0] * n
    middle = [0.0] * n
    lower = [0.0] * n
    pct_b = [0.5] * n
    bandwidth = [0.0] * n

    for i in range(n):
        window_start = max(0, i - period + 1)
        window = closes[window_start:i + 1]
        w_len = len(window)
        mean = sum(window) / w_len
        middle[i] = round(mean, 6)

        if w_len > 1:
            variance = sum((x - mean) ** 2 for x in window) / w_len
            std = math.sqrt(variance)
        else:
            std = 0.0

        up = mean + (num_std * std)
        dn = mean - (num_std * std)
        upper[i] = round(up, 6)
        lower[i] = round(dn, 6)

        width = up - dn
        if width > 0:
            pct_b[i] = round((closes[i] - dn) / width, 4)
            bandwidth[i] = round(width / mean, 4) if mean > 0 else 0.0
        else:
            pct_b[i] = 0.5
            bandwidth[i] = 0.0

    return upper, middle, lower, pct_b, bandwidth


def compute_macd_series(
    closes: List[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9
) -> Tuple[List[float], List[float], List[float]]:
    """
    Computes Moving Average Convergence Divergence (MACD).
    Returns (macd_line, signal_line, histogram).
    """
    n = len(closes)
    if n == 0:
        return [], [], []

    fast_ema = compute_ema_series(closes, fast_period)
    slow_ema = compute_ema_series(closes, slow_period)

    macd_line = [round(fast_ema[i] - slow_ema[i], 6) for i in range(n)]
    signal_line = compute_ema_series(macd_line, signal_period)
    signal_line = [round(x, 6) for x in signal_line]
    histogram = [round(macd_line[i] - signal_line[i], 6) for i in range(n)]

    return macd_line, signal_line, histogram


def compute_choppiness_index(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14
) -> List[float]:
    """
    Computes Choppiness Index (CHOP).
    CHOP = 100 * LOG10( SUM(TR, period) / (MaxHigh - MinLow) ) / LOG10(period)
    - CHOP > 61.8 indicates consolidation / severe chop.
    - CHOP < 38.2 indicates strong directional trend.
    """
    n = len(closes)
    if n == 0:
        return []

    tr_series = [0.0] * n
    tr_series[0] = highs[0] - lows[0]
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr_series[i] = max(hl, hc, lc)

    chop_series = [50.0] * n
    denom_log = math.log10(period) if period > 1 else 1.0

    for i in range(period - 1, n):
        sum_tr = sum(tr_series[i - period + 1:i + 1])
        max_h = max(highs[i - period + 1:i + 1])
        min_l = min(lows[i - period + 1:i + 1])
        range_hl = max_h - min_l

        if range_hl > 0 and sum_tr > 0:
            val = 100.0 * (math.log10(sum_tr / range_hl) / denom_log)
            chop_series[i] = round(max(0.0, min(100.0, val)), 2)
        else:
            chop_series[i] = 50.0

    return chop_series


def compute_volume_surge_series(volumes: List[float], period: int = 20) -> List[float]:
    """
    Computes Volume Surge Ratio: Current Volume / SMA(Volume, period).
    Values > 2.0 indicate volume explosion. Values < 0.5 indicate liquidity vacuum.
    """
    n = len(volumes)
    if n == 0:
        return []

    surge = [1.0] * n
    for i in range(n):
        w_start = max(0, i - period + 1)
        window = volumes[w_start:i + 1]
        mean_vol = sum(window) / len(window)
        if mean_vol > 0:
            surge[i] = round(volumes[i] / mean_vol, 2)
        else:
            surge[i] = 1.0

    return surge


def compute_vwap_series(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    volumes: List[float],
    window: int = 1440
) -> Tuple[List[float], List[float]]:
    """
    Computes rolling Volume-Weighted Average Price (VWAP) and distance (%).
    Default window: 1440 bars (equivalent to 24-hour rolling session on 1m candles).
    Returns (vwap_series, vwap_dist_pct_series).
    """
    n = len(closes)
    if n == 0:
        return [], []

    vwap = [0.0] * n
    dist_pct = [0.0] * n

    cum_pv = 0.0
    cum_vol = 0.0

    for i in range(n):
        typ_price = (highs[i] + lows[i] + closes[i]) / 3.0
        vol = volumes[i] if volumes[i] > 0 else 0.0001
        pv = typ_price * vol

        # Sliding window or session accumulation
        if i < window:
            cum_pv += pv
            cum_vol += vol
        else:
            # Shift window
            prev_idx = i - window
            prev_typ = (highs[prev_idx] + lows[prev_idx] + closes[prev_idx]) / 3.0
            prev_vol = volumes[prev_idx] if volumes[prev_idx] > 0 else 0.0001
            cum_pv = cum_pv - (prev_typ * prev_vol) + pv
            cum_vol = cum_vol - prev_vol + vol

        v = (cum_pv / cum_vol) if cum_vol > 0 else typ_price
        vwap[i] = round(v, 6)
        dist_pct[i] = round(((closes[i] - v) / v) * 100.0, 3) if v > 0 else 0.0

    return vwap, dist_pct


def compute_candle_anatomy(
    open_p: float,
    high_p: float,
    low_p: float,
    close_p: float
) -> Dict[str, float]:
    """
    Calculates detailed candle microstructure proportions:
    - total_range: High - Low
    - body_size: abs(Close - Open)
    - body_ratio: body_size / total_range (0.0 to 1.0)
    - upper_wick: High - max(Open, Close)
    - upper_wick_ratio: upper_wick / total_range
    - lower_wick: min(Open, Close) - Low
    - lower_wick_ratio: lower_wick / total_range
    - is_bullish: Close >= Open
    """
    total_range = max(1e-8, high_p - low_p)
    body_size = abs(close_p - open_p)
    upper_wick = high_p - max(open_p, close_p)
    lower_wick = min(open_p, close_p) - low_p

    return {
        "total_range": round(total_range, 6),
        "body_size": round(body_size, 6),
        "body_ratio": round(body_size / total_range, 3),
        "upper_wick": round(upper_wick, 6),
        "upper_wick_ratio": round(upper_wick / total_range, 3),
        "lower_wick": round(lower_wick, 6),
        "lower_wick_ratio": round(lower_wick / total_range, 3),
        "is_bullish": close_p >= open_p
    }


# =============================================================================
# UNIFIED COMPOSITE INDICATOR SUITE
# =============================================================================

class IndicatorMatrix:
    """
    Precomputes and caches the full multi-indicator matrix over a series of candles
    for instantaneous random-access lookup during trade forensics and slicing.
    """

    def __init__(self, candles: List[Dict[str, Any]], config: Optional[Dict[str, Any]] = None):
        self.candles = candles
        self.config = config or {}
        self.times = [c.get("time", c.get("open_time_ms", 0) // 1000) for c in candles]
        self.opens = [c["open"] for c in candles]
        self.highs = [c["high"] for c in candles]
        self.lows = [c["low"] for c in candles]
        self.closes = [c["close"] for c in candles]
        self.volumes = [c.get("volume", 0.0) for c in candles]

        self._time_index_map = {self.times[i]: i for i in range(len(self.times))}

        # Calculate all series
        self._calculate_all()

    def _calculate_all(self):
        n = len(self.closes)
        if n == 0:
            return

        cfg = self.config
        fast_p = int(cfg.get("param_ema_fast", 5))
        slow_p = int(cfg.get("param_ema_slow", 13))
        rsi_p = int(cfg.get("param_rsi_period", cfg.get("param_stoch_rsi_period", 9)))
        stoch_p = int(cfg.get("param_stoch_period", cfg.get("param_stoch_rsi_period", 9)))
        k_p = int(cfg.get("param_k_period", 3))
        d_p = int(cfg.get("param_d_period", 3))

        # 1. EMAs (Fast, Slow, 50, 200)
        self.ema_fast = compute_ema_series(self.closes, fast_p)
        self.ema_slow = compute_ema_series(self.closes, slow_p)
        self.ema_50 = compute_ema_series(self.closes, 50)
        self.ema_200 = compute_ema_series(self.closes, 200)

        # 2. Stochastic RSI
        self.stoch_k, self.stoch_d = compute_stoch_rsi(self.closes, rsi_p, stoch_p, k_p, d_p)

        # 3. Standard RSI (14)
        self.rsi_14 = compute_rsi_series(self.closes, 14)

        # 4. Bollinger Bands (20, 2)
        self.bb_upper, self.bb_mid, self.bb_lower, self.bb_pct_b, self.bb_width = compute_bollinger_bands(
            self.closes, period=20, num_std=2.0
        )

        # 5. MACD (12, 26, 9)
        self.macd_line, self.macd_signal, self.macd_hist = compute_macd_series(self.closes)

        # 6. ATR & ADX (14)
        self.atr_14 = compute_atr_series(self.highs, self.lows, self.closes, period=14)
        self.adx_14, self.plus_di, self.minus_di = compute_adx_series(self.highs, self.lows, self.closes, period=14)

        # 7. Choppiness Index (14)
        self.chop_14 = compute_choppiness_index(self.highs, self.lows, self.closes, period=14)

        # 8. Volume Surge & Rolling VWAP
        self.volume_surge = compute_volume_surge_series(self.volumes, period=20)
        self.vwap, self.vwap_dist = compute_vwap_series(self.highs, self.lows, self.closes, self.volumes)

    def find_nearest_index(self, timestamp_sec: int) -> int:
        """Finds closest candle index for a given unix timestamp (in seconds)."""
        if not self.times:
            return -1
        if timestamp_sec in self._time_index_map:
            return self._time_index_map[timestamp_sec]

        # Binary search for closest
        import bisect
        idx = bisect.bisect_left(self.times, timestamp_sec)
        if idx >= len(self.times):
            return len(self.times) - 1
        if idx > 0 and abs(self.times[idx] - timestamp_sec) > abs(self.times[idx - 1] - timestamp_sec):
            return idx - 1
        return idx

    def get_snapshot(self, idx: int) -> Dict[str, Any]:
        """
        Retrieves a complete multi-indicator snapshot at a specific candle index.
        """
        if idx < 0 or idx >= len(self.closes):
            return {}

        c_close = self.closes[idx]
        e50 = self.ema_50[idx] if idx < len(self.ema_50) else c_close
        e200 = self.ema_200[idx] if idx < len(self.ema_200) else c_close

        dist_50_pct = round(((c_close - e50) / e50) * 100.0, 3) if e50 > 0 else 0.0
        dist_200_pct = round(((c_close - e200) / e200) * 100.0, 3) if e200 > 0 else 0.0

        # EMA slope over 3 bars
        slope_fast = 0.0
        if idx >= 3:
            slope_fast = round(((self.ema_fast[idx] - self.ema_fast[idx - 3]) / self.ema_fast[idx - 3]) * 10000.0, 2)

        candle_meta = compute_candle_anatomy(
            self.opens[idx], self.highs[idx], self.lows[idx], self.closes[idx]
        )

        return {
            "time_sec": self.times[idx],
            "price": {
                "open": self.opens[idx],
                "high": self.highs[idx],
                "low": self.lows[idx],
                "close": self.closes[idx],
                "volume": self.volumes[idx]
            },
            "trend": {
                "ema_fast": self.ema_fast[idx],
                "ema_slow": self.ema_slow[idx],
                "ema_50": round(e50, 6),
                "ema_200": round(e200, 6),
                "dist_to_50_ema_pct": dist_50_pct,
                "dist_to_200_ema_pct": dist_200_pct,
                "fast_ema_slope_bps": slope_fast,
                "adx_14": self.adx_14[idx],
                "plus_di": self.plus_di[idx],
                "minus_di": self.minus_di[idx],
                "adx_regime": "STRONG_TREND" if self.adx_14[idx] >= 25 else ("CHOPPY_RANGE" if self.adx_14[idx] < 20 else "MODERATE"),
                "htf_alignment": "BULLISH_ABOVE_200EMA" if c_close >= e200 else "BEARISH_BELOW_200EMA"
            },
            "momentum": {
                "stoch_k": self.stoch_k[idx],
                "stoch_d": self.stoch_d[idx],
                "rsi_14": self.rsi_14[idx],
                "rsi_condition": "OVERBOUGHT" if self.rsi_14[idx] >= 70 else ("OVERSOLD" if self.rsi_14[idx] <= 30 else "NEUTRAL"),
                "macd_line": self.macd_line[idx],
                "macd_signal": self.macd_signal[idx],
                "macd_hist": self.macd_hist[idx],
                "choppiness_index": self.chop_14[idx],
                "chop_state": "CHOP_CONSOLIDATION" if self.chop_14[idx] >= 61.8 else ("STRONG_DIRECTIONAL" if self.chop_14[idx] <= 38.2 else "NEUTRAL")
            },
            "volatility_and_bands": {
                "atr_14": self.atr_14[idx],
                "bb_upper": self.bb_upper[idx],
                "bb_middle": self.bb_mid[idx],
                "bb_lower": self.bb_lower[idx],
                "bb_pct_b": self.bb_pct_b[idx],
                "bb_bandwidth": self.bb_width[idx]
            },
            "volume_and_fair_value": {
                "volume_surge_ratio": self.volume_surge[idx],
                "volume_state": "SURGE" if self.volume_surge[idx] >= 2.0 else ("VACUUM" if self.volume_surge[idx] <= 0.6 else "NORMAL"),
                "vwap": self.vwap[idx],
                "dist_to_vwap_pct": self.vwap_dist[idx]
            },
            "candle_microstructure": candle_meta
        }
