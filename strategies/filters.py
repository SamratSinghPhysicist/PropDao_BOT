"""
Modular Regime & Trend Filter Pipeline
======================================
Quantitative filters and regime gates to filter out false signals,
low-liquidity chop, counter-trend entries, and adverse sessions.

Components:
- compute_atr_series: Pure-Python Wilder's Average True Range
- compute_adx_series: Pure-Python Wilder's Average Directional Index (ADX)
- BaseFilter: Abstract interface for all signal and regime filters
- HTFTrendFilter: 200 EMA macro-trend baseline gate
- ADXRegimeFilter: Volatility and trend strength chop gate
- HourlySessionFilter: UTC hourly session blacklist (dead-zone filter)
- DirectionalBiasFilter: Long-only / Short-only / Bi-directional gate
- FilterPipeline: Composite pipeline orchestrating all active filters
"""

from __future__ import annotations
import math
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from kcex.engine.models import TradeSignal, OrderDirection

from strategies.ema_crossover import compute_ema_series

logger = logging.getLogger("FilterPipeline")


# =============================================================================
# QUANTITATIVE INDICATOR SERIES CALCULATORS
# =============================================================================

def compute_atr_series(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14
) -> List[float]:
    """
    Computes Wilder's Average True Range (ATR) series:
    TR = max(high - low, abs(high - close_prev), abs(low - close_prev))
    ATR[0] = TR[0]
    ATR[i] = (ATR[i-1] * (period - 1) + TR[i]) / period  for i >= period
    Initial seed at period - 1 is the SMA of the first `period` TRs.
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

    if n < period:
        # Not enough bars for smoothed ATR; return running average of TR
        atr = [0.0] * n
        running = 0.0
        for i in range(n):
            running += tr_series[i]
            atr[i] = running / (i + 1)
        return atr

    atr_series = [0.0] * n
    # Seed: SMA of first `period` TR values
    seed_atr = sum(tr_series[:period]) / period
    for i in range(period - 1):
        atr_series[i] = sum(tr_series[:i + 1]) / (i + 1)
    atr_series[period - 1] = seed_atr

    # Wilder's smoothing for subsequent bars
    for i in range(period, n):
        atr_series[i] = (atr_series[i - 1] * (period - 1) + tr_series[i]) / period

    return atr_series


def compute_adx_series(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14
) -> Tuple[List[float], List[float], List[float]]:
    """
    Computes Wilder's Average Directional Index (ADX), +DI, and -DI:
    - +DM = (high[i] - high[i-1]) if > (low[i-1] - low[i]) and > 0 else 0
    - -DM = (low[i-1] - low[i]) if > (high[i] - high[i-1]) and > 0 else 0
    - Smooth +DM, -DM, and TR over `period` bars via Wilder's smoothing
    - +DI = 100 * (smoothed +DM / smoothed TR)
    - -DI = 100 * (smoothed -DM / smoothed TR)
    - DX = 100 * (| +DI - -DI | / (+DI + -DI))
    - ADX = Wilder smoothed DX over `period`

    Returns:
        (adx_series, plus_di_series, minus_di_series)
    """
    n = len(closes)
    if n == 0:
        return [], [], []
    if n != len(highs) or n != len(lows):
        raise ValueError("highs, lows, and closes series must have identical length")

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n

    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move
        else:
            plus_dm[i] = 0.0

        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move
        else:
            minus_dm[i] = 0.0

        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr[i] = max(hl, hc, lc)

    if n < (period * 2):
        # Fallback for short series: approximate with partial sums
        zeros = [0.0] * n
        return zeros, zeros, zeros

    # Smoothed +DM, -DM, and TR using Wilder's smoothing
    smooth_plus = [0.0] * n
    smooth_minus = [0.0] * n
    smooth_tr = [0.0] * n

    smooth_plus[period - 1] = sum(plus_dm[:period])
    smooth_minus[period - 1] = sum(minus_dm[:period])
    smooth_tr[period - 1] = sum(tr[:period])

    for i in range(period, n):
        smooth_plus[i] = smooth_plus[i - 1] - (smooth_plus[i - 1] / period) + plus_dm[i]
        smooth_minus[i] = smooth_minus[i - 1] - (smooth_minus[i - 1] / period) + minus_dm[i]
        smooth_tr[i] = smooth_tr[i - 1] - (smooth_tr[i - 1] / period) + tr[i]

    # Calculate +DI, -DI, and DX
    plus_di = [0.0] * n
    minus_di = [0.0] * n
    dx = [0.0] * n

    for i in range(period - 1, n):
        tr_val = smooth_tr[i]
        if tr_val > 1e-12:
            p_di = 100.0 * (smooth_plus[i] / tr_val)
            m_di = 100.0 * (smooth_minus[i] / tr_val)
        else:
            p_di = 0.0
            m_di = 0.0

        plus_di[i] = p_di
        minus_di[i] = m_di

        di_sum = p_di + m_di
        if di_sum > 1e-12:
            dx[i] = 100.0 * (abs(p_di - m_di) / di_sum)
        else:
            dx[i] = 0.0

    # Smooth DX into ADX
    adx = [0.0] * n
    start_adx_idx = 2 * period - 1
    if n > start_adx_idx:
        seed_adx = sum(dx[period - 1:start_adx_idx + 1]) / period
        adx[start_adx_idx] = seed_adx
        for i in range(start_adx_idx + 1, n):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

    return adx, plus_di, minus_di


# =============================================================================
# BASE FILTER INTERFACE
# =============================================================================

class BaseFilter(ABC):
    """Abstract interface for all trade optimization and regime filters."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the filter."""
        pass

    @property
    @abstractmethod
    def is_enabled(self) -> bool:
        """Whether the filter is actively enabled."""
        pass

    @abstractmethod
    def is_allowed(
        self,
        signal: TradeSignal,
        candles: List[Any],
        current_time: float
    ) -> Tuple[bool, Optional[str]]:
        """
        Evaluates whether a candidate TradeSignal is allowed to execute.

        Args:
            signal: The candidate TradeSignal.
            candles: List of historical Candle objects or dictionaries with open/high/low/close.
            current_time: Current timestamp in seconds (epoch).

        Returns:
            (True, None) if allowed, or (False, rejection_reason) if rejected.
        """
        pass

    @abstractmethod
    def get_parameters(self) -> Dict[str, Any]:
        """Exports filter parameters for reporting and analytics."""
        pass


# =============================================================================
# HIGHER TIMEFRAME 200 EMA TREND FILTER
# =============================================================================

TF_MS_MAP: Dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "60m": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "Min1": 60_000,
    "Min3": 180_000,
    "Min5": 300_000,
    "Min15": 900_000,
    "Min30": 1_800_000,
    "Min60": 3_600_000,
    "Hour2": 7_200_000,
    "Hour4": 14_400_000,
    "Hour6": 21_600_000,
    "Hour8": 28_800_000,
    "Day1": 86_400_000,
}


def resample_closes_to_timeframe(candles: List[Any], target_timeframe: str) -> List[float]:
    """
    Resamples smaller granularity candles (e.g. 1m) into the target timeframe's closes.
    If candles are already at or larger than the target timeframe or lack timestamps,
    returns the raw candle closes directly.
    """
    if not candles:
        return []

    target_bucket_ms = TF_MS_MAP.get(target_timeframe, 900_000)

    # Check if candles have timestamp information
    first_c = candles[0]
    ts_field = None
    if hasattr(first_c, "open_time_ms"):
        ts_field = "open_time_ms"
    elif hasattr(first_c, "timestamp"):
        ts_field = "timestamp"
    elif isinstance(first_c, dict):
        if "openTime" in first_c:
            ts_field = "openTime"
        elif "timestamp_ms" in first_c:
            ts_field = "timestamp_ms"
        elif "open_time_ms" in first_c:
            ts_field = "open_time_ms"

    # If no timestamps available or only 1 candle, extract raw closes
    def get_c_close(c: Any) -> float:
        if hasattr(c, "close"):
            return float(c.close)
        elif isinstance(c, dict) and "close" in c:
            return float(c["close"])
        elif isinstance(c, (list, tuple)) and len(c) >= 5:
            return float(c[4])
        return 0.0

    def get_c_ts(c: Any) -> int:
        if ts_field and hasattr(c, ts_field):
            val = getattr(c, ts_field)
            return int(val * 1000) if val < 1e11 else int(val)
        elif ts_field and isinstance(c, dict) and ts_field in c:
            val = c[ts_field]
            return int(val * 1000) if val < 1e11 else int(val)
        return 0

    if not ts_field or len(candles) < 2:
        return [get_c_close(c) for c in candles]

    # Detect candle granularity
    ts0 = get_c_ts(candles[0])
    ts1 = get_c_ts(candles[1])
    candle_step_ms = abs(ts1 - ts0) if (ts1 > 0 and ts0 > 0) else 0

    # If candles are already at or above target interval, return closes directly
    if candle_step_ms >= target_bucket_ms or candle_step_ms <= 0:
        return [get_c_close(c) for c in candles]

    # Resample into target timeframe buckets
    buckets: Dict[int, float] = {}
    for c in candles:
        ts = get_c_ts(c)
        if ts <= 0:
            continue
        bucket_id = ts // target_bucket_ms
        buckets[bucket_id] = get_c_close(c)

    if not buckets:
        return [get_c_close(c) for c in candles]

    return list(buckets.values())


class HTFTrendFilter(BaseFilter):
    """
    Higher Timeframe (HTF) Trend Filter (e.g. 200 EMA baseline).
    Enforces that micro-scalp entries align with the macro trend:
    - Long signals permitted ONLY when current_price >= HTF EMA.
    - Short signals permitted ONLY when current_price <= HTF EMA.
    Automatically resamples lower-timeframe candles to the target HTF timeframe.
    """

    def __init__(
        self,
        enabled: bool = False,
        ema_period: int = 200,
        timeframe: str = "15m"
    ):
        self._enabled = enabled
        self.ema_period = ema_period
        self.timeframe = timeframe

    @property
    def name(self) -> str:
        return "HTFTrendFilter"

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def is_allowed(
        self,
        signal: TradeSignal,
        candles: List[Any],
        current_time: float
    ) -> Tuple[bool, Optional[str]]:
        if not self._enabled:
            return True, None

        if not candles:
            return True, None

        # Extract current price from the latest candle
        latest_c = candles[-1]
        if hasattr(latest_c, "close"):
            current_price = float(latest_c.close)
        elif isinstance(latest_c, dict) and "close" in latest_c:
            current_price = float(latest_c["close"])
        elif isinstance(latest_c, (list, tuple)) and len(latest_c) >= 5:
            current_price = float(latest_c[4])
        else:
            return True, None

        # Resample closes into target HTF timeframe (e.g. 15m)
        htf_closes = resample_closes_to_timeframe(candles, self.timeframe)
        if len(htf_closes) < 10:
            # Need minimum history to establish a reliable baseline
            return True, None

        period = min(self.ema_period, len(htf_closes))
        ema_series = compute_ema_series(htf_closes, period)
        if not ema_series:
            return True, None

        htf_ema = ema_series[-1]

        direction_str = str(getattr(signal, "direction", "")).upper()
        is_long = "LONG" in direction_str or "BUY" in direction_str

        if is_long and current_price < htf_ema:
            return False, f"HTF Trend: Long rejected (Price {current_price:.4f} < {self.timeframe} {period} EMA {htf_ema:.4f})"

        if not is_long and current_price > htf_ema:
            return False, f"HTF Trend: Short rejected (Price {current_price:.4f} > {self.timeframe} {period} EMA {htf_ema:.4f})"

        return True, None

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "htf_trend_filter_enabled": self._enabled,
            "htf_ema_period": self.ema_period,
            "htf_timeframe": self.timeframe
        }


# =============================================================================
# ADX VOLATILITY & CHOP REGIME FILTER
# =============================================================================

class ADXRegimeFilter(BaseFilter):
    """
    Average Directional Index (ADX) Regime Filter.
    Suppresses entries during non-directional sideways chop (ADX < threshold).
    """

    def __init__(
        self,
        enabled: bool = False,
        period: int = 14,
        threshold: float = 25.0
    ):
        self._enabled = enabled
        self.period = period
        self.threshold = threshold

    @property
    def name(self) -> str:
        return "ADXRegimeFilter"

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def is_allowed(
        self,
        signal: TradeSignal,
        candles: List[Any],
        current_time: float
    ) -> Tuple[bool, Optional[str]]:
        if not self._enabled:
            return True, None

        if not candles or len(candles) < (self.period * 2):
            return True, None

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

        adx_series, _, _ = compute_adx_series(highs, lows, closes, period=self.period)
        if not adx_series:
            return True, None

        latest_adx = adx_series[-1]
        if latest_adx < self.threshold:
            return False, f"ADX Regime: Chop detected (ADX {latest_adx:.1f} < threshold {self.threshold:.1f})"

        return True, None

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "adx_filter_enabled": self._enabled,
            "adx_period": self.period,
            "adx_threshold": self.threshold
        }


# =============================================================================
# HOURLY DEAD-ZONE SESSION FILTER
# =============================================================================

class HourlySessionFilter(BaseFilter):
    """
    UTC Hourly Session Blacklist Filter.
    Blocks trade entries during known low-liquidity or erratic transition hours
    (e.g., 02:00, 03:00, 04:00, 05:00, 17:00 UTC).
    """

    def __init__(
        self,
        enabled: bool = False,
        blacklist_utc_hours: Optional[List[int]] = None
    ):
        self._enabled = enabled
        self.blacklist_utc_hours = blacklist_utc_hours or []

    @property
    def name(self) -> str:
        return "HourlySessionFilter"

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def is_allowed(
        self,
        signal: TradeSignal,
        candles: List[Any],
        current_time: float
    ) -> Tuple[bool, Optional[str]]:
        if not self._enabled or not self.blacklist_utc_hours:
            return True, None

        try:
            dt = datetime.fromtimestamp(current_time, tz=timezone.utc)
            hour = dt.hour
            if hour in self.blacklist_utc_hours:
                return False, f"Hourly Filter: Blocked UTC hour {hour:02d}:00 (Blacklist: {self.blacklist_utc_hours})"
        except Exception as e:
            logger.debug("Hourly filter timestamp parsing error: %s", e)

        return True, None

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "hourly_filter_enabled": self._enabled,
            "hourly_blacklist_utc": list(self.blacklist_utc_hours)
        }


# =============================================================================
# DIRECTIONAL BIAS FILTER
# =============================================================================

class DirectionalBiasFilter(BaseFilter):
    """
    Directional Bias Filter.
    Enforces trading strictly in a preferred direction (BOTH, LONG_ONLY, SHORT_ONLY).
    """

    def __init__(
        self,
        enabled: bool = False,
        direction_bias: str = "BOTH"
    ):
        self._enabled = enabled
        self.direction_bias = (direction_bias or "BOTH").upper()

    @property
    def name(self) -> str:
        return "DirectionalBiasFilter"

    @property
    def is_enabled(self) -> bool:
        return self._enabled and self.direction_bias in ("LONG_ONLY", "SHORT_ONLY")

    def is_allowed(
        self,
        signal: TradeSignal,
        candles: List[Any],
        current_time: float
    ) -> Tuple[bool, Optional[str]]:
        if not self.is_enabled:
            return True, None

        direction_str = str(getattr(signal, "direction", "")).upper()
        is_long = "LONG" in direction_str or "BUY" in direction_str

        if self.direction_bias == "LONG_ONLY" and not is_long:
            return False, "Directional Bias: Short rejected (Policy: LONG_ONLY)"

        if self.direction_bias == "SHORT_ONLY" and is_long:
            return False, "Directional Bias: Long rejected (Policy: SHORT_ONLY)"

        return True, None

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "direction_bias": self.direction_bias
        }


# =============================================================================
# VOLUME SHOCK FILTER (RESEARCH V3.1)
# =============================================================================

def get_c_vol(c: Any) -> float:
    if hasattr(c, "volume"):
        return float(c.volume)
    elif hasattr(c, "vol"):
        return float(c.vol)
    elif isinstance(c, dict):
        return float(c.get("volume", c.get("vol", 0.0)))
    elif isinstance(c, (list, tuple)) and len(c) >= 6:
        return float(c[5])
    return 0.0


class VolumeShockFilter(BaseFilter):
    """
    Volume Shock Momentum Filter (Research V3.1).
    Gates trade signals to ensure sufficient market liquidity and momentum:
    Current candle volume must be >= multiplier * rolling SMA(volume, period).
    """

    def __init__(
        self,
        enabled: bool = False,
        multiplier: float = 1.2,
        period: int = 20
    ):
        self._enabled = enabled
        self.multiplier = float(multiplier)
        self.period = int(period)

    @property
    def name(self) -> str:
        return "VolumeShockFilter"

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def is_allowed(
        self,
        signal: TradeSignal,
        candles: List[Any],
        current_time: float
    ) -> Tuple[bool, Optional[str]]:
        if not self._enabled:
            return True, None

        if len(candles) < 2:
            return True, None

        vols = [get_c_vol(c) for c in candles]
        if not vols or len(vols) < 2:
            return True, None

        curr_vol = vols[-1]
        baseline_slice = vols[max(0, len(vols) - 1 - self.period):-1]
        if not baseline_slice:
            return True, None

        avg_vol = sum(baseline_slice) / len(baseline_slice)
        threshold = avg_vol * self.multiplier

        if curr_vol < threshold:
            return False, f"Volume Shock: Current volume {curr_vol:.1f} < {self.multiplier:.1f}x baseline ({threshold:.1f})"

        return True, None

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "volume_filter_enabled": self._enabled,
            "volume_filter_multiplier": self.multiplier,
            "volume_filter_period": self.period
        }


# =============================================================================
# COMPOSITE FILTER PIPELINE
# =============================================================================

class FilterPipeline:
    """
    Composite pipeline chaining multiple regime and trend filters together.
    Evaluates candidate signals sequentially and reports aggregated parameters.
    """

    def __init__(self, filters: Optional[List[BaseFilter]] = None):
        self.filters: List[BaseFilter] = filters or []

    def add_filter(self, f: BaseFilter) -> None:
        """Appends a filter to the pipeline."""
        self.filters.append(f)

    def evaluate(
        self,
        signal: TradeSignal,
        candles: List[Any],
        current_time: Optional[float] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Evaluates a candidate signal against all active filters in the pipeline.

        Returns:
            (True, None) if all active filters allow the signal.
            (False, rejection_reason) on the first filter that rejects the signal.
        """
        now = current_time if current_time is not None else getattr(signal, "timestamp", 0.0)
        for f in self.filters:
            if f.is_enabled:
                allowed, reason = f.is_allowed(signal, candles, now)
                if not allowed:
                    return False, reason
        return True, None

    def get_parameters(self) -> Dict[str, Any]:
        """Aggregates parameter dictionaries from all filters."""
        params: Dict[str, Any] = {}
        for f in self.filters:
            params.update(f.get_parameters())
        return params

    @classmethod
    def from_config(cls, config: Any) -> FilterPipeline:
        """Factory constructor instantiating all configured filters from ExecutionConfig or BacktestConfig."""
        pipeline = cls()

        # 1. HTF Trend Filter
        htf_enabled = getattr(config, "htf_trend_filter_enabled", False)
        htf_ema = getattr(config, "htf_ema_period", 200)
        htf_tf = getattr(config, "htf_timeframe", "15m")
        pipeline.add_filter(HTFTrendFilter(enabled=htf_enabled, ema_period=htf_ema, timeframe=htf_tf))

        # 2. ADX Chop Filter
        adx_enabled = getattr(config, "adx_filter_enabled", False)
        adx_period = getattr(config, "adx_period", 14)
        adx_threshold = getattr(config, "adx_threshold", 25.0)
        pipeline.add_filter(ADXRegimeFilter(enabled=adx_enabled, period=adx_period, threshold=adx_threshold))

        # 3. Hourly Session Filter
        hourly_enabled = getattr(config, "hourly_filter_enabled", False)
        blacklist = getattr(config, "hourly_blacklist_utc", []) or []
        pipeline.add_filter(HourlySessionFilter(enabled=hourly_enabled, blacklist_utc_hours=blacklist))

        # 4. Directional Bias Filter
        dir_bias = getattr(config, "direction_bias", "BOTH")
        pipeline.add_filter(DirectionalBiasFilter(enabled=(dir_bias != "BOTH"), direction_bias=dir_bias))

        # 5. Volume Shock Filter (Research V3.1)
        vol_enabled = getattr(config, "volume_filter_enabled", False)
        if vol_enabled:
            vol_mult = getattr(config, "volume_filter_multiplier", 1.2)
            pipeline.add_filter(VolumeShockFilter(enabled=vol_enabled, multiplier=vol_mult))

        # 6. US Market Hours Filter (for Stock / Equity Tokens)
        us_hours_enabled = getattr(config, "us_market_hours_filter_enabled", False)
        if us_hours_enabled:
            pipeline.add_filter(USMarketHoursFilter(enabled=True))

        return pipeline


# =============================================================================
# US MARKET HOURS FILTER (EQUITY & TOKENIZED STOCKS)
# =============================================================================

class USMarketHoursFilter(BaseFilter):
    """
    US Equities Market Regular Trading Hours Gate.
    Restricts trade entries strictly to US regular market trading hours:
    Monday through Friday, 09:30 AM to 04:00 PM US Eastern Time (ET).
    Essential for crypto-equivalent tokenized stocks that trade 24/7 on exchanges
    to only take positions when the underlying US cash market is actively trading.
    """

    def __init__(self, enabled: bool = False):
        self._enabled = enabled

    @property
    def name(self) -> str:
        return "USMarketHoursFilter"

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def is_allowed(
        self,
        signal: TradeSignal,
        candles: List[Any],
        current_time: float
    ) -> Tuple[bool, Optional[str]]:
        if not self._enabled:
            return True, None

        try:
            import zoneinfo
            ny_tz = zoneinfo.ZoneInfo("America/New_York")
            dt = datetime.fromtimestamp(current_time, tz=ny_tz)
        except Exception:
            dt = datetime.fromtimestamp(current_time, tz=timezone(timedelta(hours=-4)))

        # 0 = Monday, 4 = Friday, 5 = Saturday, 6 = Sunday
        if dt.weekday() >= 5:
            return False, f"US Market Closed: Weekend ({dt.strftime('%A')})"

        market_open_min = 9 * 60 + 30   # 09:30 ET
        market_close_min = 16 * 60      # 16:00 ET
        current_min = dt.hour * 60 + dt.minute

        if current_min < market_open_min or current_min >= market_close_min:
            return False, f"US Market Closed: Outside regular hours ({dt.strftime('%H:%M:%S ET')}, open is 09:30-16:00 ET)"

        return True, None

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "us_market_hours_filter_enabled": self._enabled
        }

