"""
Order Block + Demand/Supply Block Trading Strategy
===================================================
Faithfully designed and audited against Vivek Yadav's (Advance Crypto Trader) masterclasses
and the official 'Vivek_Yadav_OB_Strategy_PnL' indicator engine.

Core Rules & Mechanics:
1. Fractal Swing Pivot Detection:
   - pivot_len = 5: Swing high/low confirmed at checkIdx = i - pivot_len looking +/- 5 bars.
   - Retrospective, non-repainting pivot confirmation with 0 lookahead bias.
2. Break of Structure (BOS) & Order Block Discovery:
   - Bullish BOS: Candle close pierces previous swing high (prev.close <= swing_high and cur.close > swing_high).
     Origin is the structural extrema (minimum low) between the swing high and the BOS bar.
     Order Block: Scans backward up to 10 bars for the last RED candle (close < open).
     OB boundary: Full wick-to-wick range (top = high, bottom = low).
   - Bearish BOS: Candle close pierces previous swing low (prev.close >= swing_low and cur.close < swing_low).
     Origin is the structural extrema (maximum high) between the swing low and the BOS bar.
     Order Block: Scans backward up to 10 bars for the last GREEN candle (close > open).
     OB boundary: Full wick-to-wick range (top = high, bottom = low).
3. Invalidation & Mitigation Engine:
   - Bullish Invalidation: Candle body closes below the OB bottom (cur.close < ob.low).
   - Bullish Retest Entry: Candle touches the zone (cur.low <= ob.high and cur.high >= ob.low)
     and closes GREEN (cur.close > cur.open) above ob.low (or confirms green on T+1).
   - Bearish Invalidation: Candle body closes above the OB top (cur.close > ob.high).
   - Bearish Retest Entry: Candle touches the zone (cur.high >= ob.low and cur.low <= ob.high)
     and closes RED (cur.close < cur.open) below ob.top (or confirms red on T+1).
4. Exact 1:2 Risk-to-Reward Geometry:
   - Long: Entry = cur.close, SL = ob.low (- buffer_ticks), TP = entry + 2.0 * (entry - sl).
   - Short: Entry = cur.close, SL = ob.high (+ buffer_ticks), TP = entry - 2.0 * (sl - entry).
   - Optional 1:1 partial profit exit & breakeven lock support for multi-contract positions.
"""

from __future__ import annotations
import math
import time
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple, Set, TYPE_CHECKING
from dataclasses import dataclass, field

try:
    from propdao.models import OrderDirection, TradeSignal, TradeOutcome
except ImportError:
    try:
        from kcex.engine.models import OrderDirection, TradeSignal, TradeOutcome
    except ImportError:
        from enum import Enum
        class OrderDirection(str, Enum):
            LONG = "LONG"
            SHORT = "SHORT"

from strategies.base import BaseStrategy

logger = logging.getLogger("OrderBlockDemandStrategy")


class ZoneType(str, Enum):
    BULLISH_ORDER_BLOCK = "BULLISH_ORDER_BLOCK"
    BEARISH_ORDER_BLOCK = "BEARISH_ORDER_BLOCK"
    DEMAND_BLOCK = "DEMAND_BLOCK"
    SUPPLY_BLOCK = "SUPPLY_BLOCK"
    CONFLUENT_DEMAND_OB = "CONFLUENT_DEMAND_OB"
    CONFLUENT_SUPPLY_OB = "CONFLUENT_SUPPLY_OB"


class ZoneStatus(str, Enum):
    ACTIVE = "ACTIVE"              # Created, waiting for retest
    TESTED = "TESTED"              # Tapped zone
    CONFIRMED = "CONFIRMED"        # Directional confirmation candle closed
    INVALIDATED = "INVALIDATED"    # Broken directly (candle closed beyond zone boundary)
    MITIGATED = "MITIGATED"        # Trade executed
    EXPIRED = "EXPIRED"            # Aged out past max lookback


@dataclass
class SmartMoneyZone:
    """Represents an active or historical Order Block or Demand/Supply Zone."""
    zone_id: str
    zone_type: ZoneType
    symbol: str
    high: float                    # Upper boundary (highest wick of origin candle)
    low: float                     # Lower boundary (lowest wick of origin candle)
    body_high: float = 0.0         # max(open, close) of origin candle
    body_low: float = 0.0          # min(open, close) of origin candle
    creation_bar_idx: int = 0      # Bar index of origin candle
    creation_ts: int = 0           # Millisecond timestamp
    status: ZoneStatus = ZoneStatus.ACTIVE
    bos_bar_idx: Optional[int] = None
    bos_price: Optional[float] = None
    fvg_size: float = 0.0
    consecutive_candles: int = 0
    is_extrema: bool = False
    retest_bar_idx: Optional[int] = None
    retest_ts: Optional[int] = None
    retest_price: Optional[float] = None
    retest_wick_price: Optional[float] = None
    tested_count: int = 0
    confirmation_bar_idx: Optional[int] = None
    end_idx: Optional[int] = None

    @property
    def is_bullish(self) -> bool:
        return self.zone_type in (
            ZoneType.BULLISH_ORDER_BLOCK,
            ZoneType.DEMAND_BLOCK,
            ZoneType.CONFLUENT_DEMAND_OB
        )

    @property
    def is_bearish(self) -> bool:
        return self.zone_type in (
            ZoneType.BEARISH_ORDER_BLOCK,
            ZoneType.SUPPLY_BLOCK,
            ZoneType.CONFLUENT_SUPPLY_OB
        )

    @property
    def zone_height(self) -> float:
        return max(1e-12, self.high - self.low)


@dataclass
class SwingPoint:
    """Fractal pivot high or low."""
    bar_idx: int
    ts: int
    price: float
    is_high: bool  # True for swing high, False for swing low


class SwingStructureDetector:
    """
    Identifies fractal swing highs and lows and evaluates Break of Structure (BOS).
    Strict Body-Close Rule:
    A high or low is only broken when a candle body closes beyond it.
    """

    @staticmethod
    def find_swings(
        highs: List[float],
        lows: List[float],
        timestamps: List[int],
        left_bars: int = 5,
        right_bars: int = 5
    ) -> List[SwingPoint]:
        n = len(highs)
        swings: List[SwingPoint] = []
        if n < left_bars + right_bars + 1:
            return swings

        for i in range(left_bars, n - right_bars):
            curr_h = highs[i]
            is_sh = True
            for j in range(i - left_bars, i + right_bars + 1):
                if j != i and highs[j] >= curr_h:
                    is_sh = False
                    break
            if is_sh:
                swings.append(SwingPoint(bar_idx=i, ts=timestamps[i], price=curr_h, is_high=True))

            curr_l = lows[i]
            is_sl = True
            for j in range(i - left_bars, i + right_bars + 1):
                if j != i and lows[j] <= curr_l:
                    is_sl = False
                    break
            if is_sl:
                swings.append(SwingPoint(bar_idx=i, ts=timestamps[i], price=curr_l, is_high=False))

        swings.sort(key=lambda x: x.bar_idx)
        return swings


class OrderBlockDemandStrategy(BaseStrategy):
    """
    Unified Order Block + Demand/Supply Block Strategy.
    Faithfully mirrors Vivek Yadav's SMC masterclass and the KLineChart indicator:
    - 5-bar rolling swing high / low confirmation
    - Body-close Break of Structure (BOS)
    - Origin candle extrema detection + backward scan for last opposite candle (Order Block)
    - Retest & rejection confirmation: candle taps zone and closes in trade direction
    - Invalidation on direct breach of zone boundary
    - Strict 1:2 Risk-to-Reward ratio with optional 1:1 partial close & breakeven lock
    """

    def __init__(
        self,
        market: KCEXMarket,
        symbol: str,
        interval: str = "Min1",
        pivot_len: int = 3,
        swing_left_bars: int = 3,
        swing_right_bars: int = 3,
        min_impulse_candles: int = 2,
        max_impulse_candles: int = 5,
        min_rejection_wick_ratio: float = 0.15,
        risk_reward_ratio: float = 2.0,
        buffer_ticks: int = 0,
        min_sl_ticks: int = 1,
        max_sl_ticks: int = 100,
        max_zone_age_bars: int = 120,
        extrema_percentile: float = 0.25,
        trend_filter_enabled: bool = False,
        partial_tp_enabled: bool = True,
        breakeven_buffer_ticks: int = 1,
        preferred_direction: Optional[OrderDirection] = None,
        cooldown_seconds: float = 0.0,
        require_closed_candle: bool = True,
        auto_start_feed: bool = False,
        name: str = "OrderBlockDemand"
    ):
        super().__init__(name=name)
        self.market = market
        self.symbol = symbol.upper()
        try:
            from kcex.market import normalize_kcex_interval
            self.interval = normalize_kcex_interval(interval)
        except Exception:
            self.interval = interval
        self.pivot_len = pivot_len
        self.swing_left_bars = swing_left_bars or pivot_len
        self.swing_right_bars = swing_right_bars or pivot_len
        self.min_impulse_candles = min_impulse_candles
        self.max_impulse_candles = max_impulse_candles
        self.min_rejection_wick_ratio = min_rejection_wick_ratio
        self.risk_reward_ratio = risk_reward_ratio
        self.buffer_ticks = buffer_ticks
        self.min_sl_ticks = min_sl_ticks
        self.max_sl_ticks = max_sl_ticks
        self.max_zone_age_bars = max_zone_age_bars
        self.extrema_percentile = extrema_percentile
        self.trend_filter_enabled = trend_filter_enabled
        self.partial_tp_enabled = partial_tp_enabled
        self.breakeven_buffer_ticks = breakeven_buffer_ticks
        self.preferred_direction = preferred_direction
        self.cooldown_seconds = cooldown_seconds
        self.require_closed_candle = require_closed_candle

        # Execution tracking
        self.last_trade_closed_at: Optional[float] = None
        self.trade_in_progress: bool = False
        self.completed_trades_count: int = 0
        self.last_signal_candle_ts: Optional[int] = None

        # Contract tick specifications
        self._price_unit: float = 0.001
        self._price_precision: int = 4
        self._refresh_contract_spec()

        # Active zones registry: Dict[zone_id, SmartMoneyZone]
        self.active_zones: Dict[str, SmartMoneyZone] = {}
        # Anti-recreation memory
        self.resolved_origin_ts: Set[int] = set()
        self.history_zones: List[SmartMoneyZone] = []
        self.zone_counter: int = 0
        self.last_diagnostics: Dict[str, Any] = {}
        self.last_rejection_reason: str = ""

        # Kline cache and rate-limit backoff state
        self.kline_cache_interval: float = 2.0
        self._cached_candles: List[Any] = []
        self._last_kline_fetch_ts: float = 0.0
        self._rate_limit_backoff_until: float = 0.0

    @property
    def timeframe(self) -> str:
        return getattr(self, "interval", "Min15")

    def _refresh_contract_spec(self) -> None:
        """Inspects contract specifications for precise tick scaling."""
        try:
            if hasattr(self.market, "get_contract_detail"):
                contract = self.market.get_contract_detail(self.symbol)
                if contract:
                    self._price_unit = getattr(contract, "price_unit", self._price_unit)
                    self._price_precision = getattr(contract, "price_precision", self._price_precision)
            elif hasattr(self.market, "get_symbol_info"):
                info = self.market.get_symbol_info(self.symbol)
                if info:
                    self._price_unit = getattr(info, "price_unit", self._price_unit)
                    self._price_precision = getattr(info, "price_precision", self._price_precision)
        except Exception:
            pass

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
        self.last_trade_closed_at = outcome.close_time or time.time()
        self.completed_trades_count += 1

        # Double check: ensure any zone associated with this completed trade is permanently purged
        traded_zid = getattr(outcome, "smc_zone_id", None)
        if traded_zid:
            for zid in list(self.active_zones.keys()):
                az = self.active_zones[zid]
                if zid == traded_zid or (hasattr(az, "creation_ts") and str(az.creation_ts) in str(traded_zid)):
                    az.status = ZoneStatus.MITIGATED
                    self.resolved_origin_ts.add(az.creation_ts)
                    self.history_zones.append(az)
                    del self.active_zones[zid]

        logger.info(
            "[%s] Completed trade #%d. Realized PnL: $%.4f. Cooldown %ds initiated.",
            self.name, outcome.trade_id, outcome.realized_pnl_usdt, int(self.cooldown_seconds)
        )

    def on_trade_rejected(self) -> None:
        """Resets trade_in_progress when execution is canceled or rejected."""
        self.trade_in_progress = False

    def _mark_resolved(self, ts: Optional[int]) -> None:
        """Permanently marks an origin candle timestamp as resolved in both seconds and milliseconds."""
        if not ts:
            return
        self.resolved_origin_ts.add(ts)
        self.resolved_origin_ts.add(ts * 1000)
        self.resolved_origin_ts.add(ts // 1000)

    def _is_resolved(self, ts: Optional[int]) -> bool:
        """Checks if an origin candle timestamp is resolved, supporting both seconds and milliseconds."""
        if not ts:
            return False
        return (
            ts in self.resolved_origin_ts
            or (ts * 1000) in self.resolved_origin_ts
            or (ts // 1000) in self.resolved_origin_ts
        )

    def _extract_candle_series(self, raw_bars: List[Any]) -> Tuple[List[int], List[float], List[float], List[float], List[float], List[float]]:
        """Normalizes klines into parallel lists: timestamps, opens, highs, lows, closes, volumes."""
        timestamps: List[int] = []
        opens: List[float] = []
        highs: List[float] = []
        lows: List[float] = []
        closes: List[float] = []
        volumes: List[float] = []

        for b in raw_bars:
            if hasattr(b, "timestamp") and hasattr(b, "close"):
                ts = int(getattr(b, "timestamp", 0))
                timestamps.append(ts * 1000 if 0 < ts < 1e11 else ts)
                opens.append(float(getattr(b, "open", 0.0)))
                highs.append(float(getattr(b, "high", 0.0)))
                lows.append(float(getattr(b, "low", 0.0)))
                closes.append(float(getattr(b, "close", 0.0)))
                volumes.append(float(getattr(b, "volume", 0.0)))
            elif isinstance(b, dict):
                ts = int(b.get("timestamp", b.get("time", 0)))
                timestamps.append(ts * 1000 if 0 < ts < 1e11 else ts)
                opens.append(float(b.get("open", 0.0)))
                highs.append(float(b.get("high", 0.0)))
                lows.append(float(b.get("low", 0.0)))
                closes.append(float(b.get("close", 0.0)))
                volumes.append(float(b.get("volume", 0.0)))
            elif isinstance(b, (list, tuple)) and len(b) >= 6:
                ts = int(b[0])
                timestamps.append(ts * 1000 if 0 < ts < 1e11 else ts)
                opens.append(float(b[1]))
                highs.append(float(b[2]))
                lows.append(float(b[3]))
                closes.append(float(b[4]))
                volumes.append(float(b[5]))

        return timestamps, opens, highs, lows, closes, volumes

    def calc_indicator_zones_and_trades(
        self,
        timestamps: List[int],
        opens: List[float],
        highs: List[float],
        lows: List[float],
        closes: List[float],
        pivot_len: Optional[int] = None,
        resolved_origin_ts: Optional[Set[int]] = None,
        max_zone_age_bars: Optional[int] = None
    ) -> Tuple[List[SmartMoneyZone], List[Dict[str, Any]]]:
        """
        Pure Python implementation of Vivek Yadav's exact KLineChart indicator:
        'Vivek_Yadav_OB_Strategy_PnL'
        """
        p_len = pivot_len if pivot_len is not None else self.pivot_len
        n = len(closes)
        bull_obs: List[SmartMoneyZone] = []
        bear_obs: List[SmartMoneyZone] = []
        drawn_zones: List[SmartMoneyZone] = []
        drawn_trades: List[Dict[str, Any]] = []

        last_swing_high = None  # Dict: {'idx': int, 'val': float}
        last_swing_low = None   # Dict: {'idx': int, 'val': float}

        is_red = lambda idx: closes[idx] < opens[idx]
        is_green = lambda idx: closes[idx] > opens[idx]

        for i in range(p_len, n):
            cur_open = opens[i]
            cur_high = highs[i]
            cur_low = lows[i]
            cur_close = closes[i]
            prev_close = closes[i - 1]

            # 1. Detect Swing Pivots at checkIdx = i - p_len
            check_idx = i - p_len
            is_sh = True
            is_sl = True
            
            for j in range(check_idx - p_len, check_idx + p_len + 1):
                if j < 0 or j >= n:
                    continue
                if j != check_idx:
                    if highs[j] > highs[check_idx]:
                        is_sh = False
                    if lows[j] < lows[check_idx]:
                        is_sl = False

            if is_sh:
                last_swing_high = {"idx": check_idx, "val": highs[check_idx]}
            if is_sl:
                last_swing_low = {"idx": check_idx, "val": lows[check_idx]}

            # 2. Bullish BOS & Demand Zone Detection
            if last_swing_high and prev_close <= last_swing_high["val"] and cur_close > last_swing_high["val"]:
                sh_idx = last_swing_high["idx"]
                origin_idx = sh_idx
                min_val = lows[origin_idx]
                for j in range(sh_idx + 1, i):
                    if lows[j] < min_val:
                        min_val = lows[j]
                        origin_idx = j

                ob_idx = origin_idx
                while ob_idx >= max(0, origin_idx - 10) and not is_red(ob_idx):
                    ob_idx -= 1
                if ob_idx < 0 or not is_red(ob_idx):
                    ob_idx = origin_idx

                ob = SmartMoneyZone(
                    zone_id=f"OB_BULL_{timestamps[ob_idx]}",
                    zone_type=ZoneType.BULLISH_ORDER_BLOCK,
                    symbol=self.symbol,
                    high=highs[ob_idx],
                    low=lows[ob_idx],
                    body_high=max(opens[ob_idx], closes[ob_idx]),
                    body_low=min(opens[ob_idx], closes[ob_idx]),
                    creation_bar_idx=ob_idx,
                    creation_ts=timestamps[ob_idx],
                    status=ZoneStatus.ACTIVE,
                    bos_bar_idx=i,
                    bos_price=cur_close
                )
                bull_obs.append(ob)
                drawn_zones.append(ob)
                last_swing_high = None

            # 3. Bearish BOS & Supply Zone Detection
            if last_swing_low and prev_close >= last_swing_low["val"] and cur_close < last_swing_low["val"]:
                sl_idx = last_swing_low["idx"]
                origin_idx = sl_idx
                max_val = highs[origin_idx]
                for j in range(sl_idx + 1, i):
                    if highs[j] > max_val:
                        max_val = highs[j]
                        origin_idx = j

                ob_idx = origin_idx
                while ob_idx >= max(0, origin_idx - 10) and not is_green(ob_idx):
                    ob_idx -= 1
                if ob_idx < 0 or not is_green(ob_idx):
                    ob_idx = origin_idx

                ob = SmartMoneyZone(
                    zone_id=f"OB_BEAR_{timestamps[ob_idx]}",
                    zone_type=ZoneType.BEARISH_ORDER_BLOCK,
                    symbol=self.symbol,
                    high=highs[ob_idx],
                    low=lows[ob_idx],
                    body_high=max(opens[ob_idx], closes[ob_idx]),
                    body_low=min(opens[ob_idx], closes[ob_idx]),
                    creation_bar_idx=ob_idx,
                    creation_ts=timestamps[ob_idx],
                    status=ZoneStatus.ACTIVE,
                    bos_bar_idx=i,
                    bos_price=cur_close
                )
                bear_obs.append(ob)
                drawn_zones.append(ob)
                last_swing_low = None

            # 4. Test Zones for Mitigation, Invalidation & Trade Entry
            for ob in bull_obs:
                if ob.status != ZoneStatus.ACTIVE:
                    continue
                ob.end_idx = i

                if resolved_origin_ts:
                    is_res = (
                        ob.creation_ts in resolved_origin_ts
                        or (ob.creation_ts * 1000) in resolved_origin_ts
                        or (ob.creation_ts // 1000) in resolved_origin_ts
                    )
                    if is_res:
                        ob.status = ZoneStatus.INVALIDATED
                        continue

                if max_zone_age_bars and max_zone_age_bars > 0 and (i - ob.creation_bar_idx) > max_zone_age_bars:
                    ob.status = ZoneStatus.EXPIRED
                    continue

                if ob.bos_bar_idx is not None and i <= ob.bos_bar_idx:
                    continue

                if cur_close < ob.low:
                    ob.status = ZoneStatus.INVALIDATED
                elif cur_low <= ob.high and cur_high >= ob.low:
                    if is_green(i) and cur_close > ob.low:
                        ob.status = ZoneStatus.MITIGATED
                        entry = cur_close
                        sl = ob.low
                        tp = entry + self.risk_reward_ratio * (entry - sl)
                        trade = {
                            "type": "long",
                            "start_idx": i,
                            "end_idx": i,
                            "entry": entry,
                            "sl": sl,
                            "tp": tp,
                            "zone": ob,
                            "active": True
                        }
                        drawn_trades.append(trade)

            for ob in bear_obs:
                if ob.status != ZoneStatus.ACTIVE:
                    continue
                ob.end_idx = i

                if resolved_origin_ts:
                    is_res = (
                        ob.creation_ts in resolved_origin_ts
                        or (ob.creation_ts * 1000) in resolved_origin_ts
                        or (ob.creation_ts // 1000) in resolved_origin_ts
                    )
                    if is_res:
                        ob.status = ZoneStatus.INVALIDATED
                        continue

                if max_zone_age_bars and max_zone_age_bars > 0 and (i - ob.creation_bar_idx) > max_zone_age_bars:
                    ob.status = ZoneStatus.EXPIRED
                    continue

                if ob.bos_bar_idx is not None and i <= ob.bos_bar_idx:
                    continue

                if cur_close > ob.high:
                    ob.status = ZoneStatus.INVALIDATED
                elif cur_high >= ob.low and cur_low <= ob.high:
                    if is_red(i) and cur_close < ob.high:
                        ob.status = ZoneStatus.MITIGATED
                        entry = cur_close
                        sl = ob.high
                        tp = entry - self.risk_reward_ratio * (sl - entry)
                        trade = {
                            "type": "short",
                            "start_idx": i,
                            "end_idx": i,
                            "entry": entry,
                            "sl": sl,
                            "tp": tp,
                            "zone": ob,
                            "active": True
                        }
                        drawn_trades.append(trade)

        return drawn_zones, drawn_trades

    def scan_for_order_blocks(
        self,
        timestamps: List[int],
        opens: List[float],
        highs: List[float],
        lows: List[float],
        closes: List[float],
        swings: Optional[List[SwingPoint]] = None
    ) -> List[SmartMoneyZone]:
        """Discovers Order Blocks based on Vivek Yadav's displacement & BOS rules."""
        discovered: List[SmartMoneyZone] = []
        n = len(closes)
        if n < 4:
            return discovered

        for origin_idx in range(max(0, n - 60), n - 2):
            origin_ts = timestamps[origin_idx]
            if origin_ts in self.resolved_origin_ts:
                continue

            # Bullish Order Block Check
            if closes[origin_idx] <= opens[origin_idx]:
                consec_green = 0
                for k in range(origin_idx + 1, min(n, origin_idx + 1 + self.max_impulse_candles)):
                    if closes[k] > opens[k]:
                        consec_green += 1
                    else:
                        break

                if consec_green >= self.min_impulse_candles:
                    impulse_end = origin_idx + consec_green
                    impulse_max_high = max(highs[origin_idx + 1 : impulse_end + 1])

                    has_fvg = (origin_idx + 2 <= impulse_end) and (lows[origin_idx + 2] > highs[origin_idx])
                    breaks_origin_high = impulse_max_high > highs[origin_idx] and closes[impulse_end] > highs[origin_idx]

                    if swings:
                        prior_shs = [s for s in swings if s.is_high and s.bar_idx < origin_idx]
                        if not prior_shs:
                            continue
                        recent_sh = prior_shs[-1]
                        if impulse_max_high >= recent_sh.price:
                            if closes[impulse_end] <= recent_sh.price:
                                continue
                            else:
                                breaks_swing_high = True
                        else:
                            continue
                    else:
                        breaks_swing_high = breaks_origin_high or has_fvg

                    if breaks_swing_high:
                        zid = f"OB_BULL_{origin_ts}"
                        zone = SmartMoneyZone(
                            zone_id=zid,
                            zone_type=ZoneType.BULLISH_ORDER_BLOCK,
                            symbol=self.symbol,
                            high=highs[origin_idx],
                            low=lows[origin_idx],
                            body_high=max(opens[origin_idx], closes[origin_idx]),
                            body_low=min(opens[origin_idx], closes[origin_idx]),
                            creation_bar_idx=origin_idx,
                            creation_ts=origin_ts,
                            consecutive_candles=consec_green,
                            bos_bar_idx=impulse_end,
                            bos_price=closes[impulse_end]
                        )
                        discovered.append(zone)

            # Bearish Order Block Check
            elif closes[origin_idx] >= opens[origin_idx]:
                consec_red = 0
                for k in range(origin_idx + 1, min(n, origin_idx + 1 + self.max_impulse_candles)):
                    if closes[k] < opens[k]:
                        consec_red += 1
                    else:
                        break

                if consec_red >= self.min_impulse_candles:
                    impulse_end = origin_idx + consec_red
                    impulse_min_low = min(lows[origin_idx + 1 : impulse_end + 1])

                    has_fvg = (origin_idx + 2 <= impulse_end) and (highs[origin_idx + 2] < lows[origin_idx])
                    breaks_origin_low = impulse_min_low < lows[origin_idx] and closes[impulse_end] < lows[origin_idx]

                    if swings:
                        prior_sls = [s for s in swings if not s.is_high and s.bar_idx < origin_idx]
                        if not prior_sls:
                            continue
                        recent_sl = prior_sls[-1]
                        if impulse_min_low <= recent_sl.price:
                            if closes[impulse_end] >= recent_sl.price:
                                continue
                            else:
                                breaks_swing_low = True
                        else:
                            continue
                    else:
                        breaks_swing_low = breaks_origin_low or has_fvg

                    if breaks_swing_low:
                        zid = f"OB_BEAR_{origin_ts}"
                        zone = SmartMoneyZone(
                            zone_id=zid,
                            zone_type=ZoneType.BEARISH_ORDER_BLOCK,
                            symbol=self.symbol,
                            high=highs[origin_idx],
                            low=lows[origin_idx],
                            body_high=max(opens[origin_idx], closes[origin_idx]),
                            body_low=min(opens[origin_idx], closes[origin_idx]),
                            creation_bar_idx=origin_idx,
                            creation_ts=origin_ts,
                            consecutive_candles=consec_red,
                            bos_bar_idx=impulse_end,
                            bos_price=closes[impulse_end]
                        )
                        discovered.append(zone)

        return discovered

    def scan_for_demand_supply_blocks(
        self,
        timestamps: List[int],
        opens: List[float],
        highs: List[float],
        lows: List[float],
        closes: List[float]
    ) -> List[SmartMoneyZone]:
        """Discovers Demand and Supply Blocks based on 3-5 consecutive impulse candles + FVG."""
        discovered: List[SmartMoneyZone] = []
        n = len(closes)
        if n < 5:
            return discovered

        lookback_slice = slice(max(0, n - 60), n)
        recent_highest = max(highs[lookback_slice])
        recent_lowest = min(lows[lookback_slice])
        range_span = max(1e-12, recent_highest - recent_lowest)
        demand_ceiling = recent_lowest + range_span * self.extrema_percentile
        supply_floor = recent_highest - range_span * self.extrema_percentile

        for origin_idx in range(max(0, n - 50), n - 3):
            origin_ts = timestamps[origin_idx]
            if origin_ts in self.resolved_origin_ts:
                continue

            if closes[origin_idx] <= opens[origin_idx]:
                consec_green = 0
                for k in range(origin_idx + 1, min(n, origin_idx + 1 + self.max_impulse_candles)):
                    if closes[k] > opens[k]:
                        consec_green += 1
                    else:
                        break

                if consec_green >= 3 and lows[origin_idx] <= demand_ceiling:
                    fvg_gap = max(0.0, lows[origin_idx + 2] - highs[origin_idx]) if origin_idx + 2 < n else 0.0
                    zone = SmartMoneyZone(
                        zone_id=f"DEMAND_{origin_ts}",
                        zone_type=ZoneType.DEMAND_BLOCK,
                        symbol=self.symbol,
                        high=highs[origin_idx],
                        low=lows[origin_idx],
                        body_high=max(opens[origin_idx], closes[origin_idx]),
                        body_low=min(opens[origin_idx], closes[origin_idx]),
                        creation_bar_idx=origin_idx,
                        creation_ts=origin_ts,
                        fvg_size=fvg_gap,
                        consecutive_candles=consec_green,
                        is_extrema=True
                    )
                    discovered.append(zone)

            elif closes[origin_idx] >= opens[origin_idx]:
                consec_red = 0
                for k in range(origin_idx + 1, min(n, origin_idx + 1 + self.max_impulse_candles)):
                    if closes[k] < opens[k]:
                        consec_red += 1
                    else:
                        break

                if consec_red >= 3 and highs[origin_idx] >= supply_floor:
                    fvg_gap = max(0.0, lows[origin_idx] - highs[origin_idx + 2]) if origin_idx + 2 < n else 0.0
                    zone = SmartMoneyZone(
                        zone_id=f"SUPPLY_{origin_ts}",
                        zone_type=ZoneType.SUPPLY_BLOCK,
                        symbol=self.symbol,
                        high=highs[origin_idx],
                        low=lows[origin_idx],
                        body_high=max(opens[origin_idx], closes[origin_idx]),
                        body_low=min(opens[origin_idx], closes[origin_idx]),
                        creation_bar_idx=origin_idx,
                        creation_ts=origin_ts,
                        fvg_size=fvg_gap,
                        consecutive_candles=consec_red,
                        is_extrema=True
                    )
                    discovered.append(zone)

        return discovered

    def update_zone_lifecycle(
        self,
        new_zones: List[SmartMoneyZone],
        current_bar_idx: int,
        opens: List[float],
        highs: List[float],
        lows: List[float],
        closes: List[float]
    ) -> None:
        """Maintains active zones and invalidates breached zones."""
        for nz in new_zones:
            if nz.creation_ts in self.resolved_origin_ts:
                continue
            if not any(z.creation_ts == nz.creation_ts for z in self.active_zones.values()):
                self.active_zones[nz.zone_id] = nz

        c_close = closes[current_bar_idx] if current_bar_idx < len(closes) else closes[-1]
        for zid, zone in list(self.active_zones.items()):
            if zone.is_bullish and c_close < zone.low:
                zone.status = ZoneStatus.INVALIDATED
                self.resolved_origin_ts.add(zone.creation_ts)
                self.history_zones.append(zone)
                del self.active_zones[zid]
            elif zone.is_bearish and c_close > zone.high:
                zone.status = ZoneStatus.INVALIDATED
                self.resolved_origin_ts.add(zone.creation_ts)
                self.history_zones.append(zone)
                del self.active_zones[zid]

    def _build_signal_metadata(
        self,
        zone: SmartMoneyZone,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        risk_ticks: int,
        target_ticks: int,
        target_1to1_price: float,
        current_candle_ts: int,
        eval_idx: int,
        prec: int
    ) -> Dict[str, Any]:
        zone_mid = (zone.high + zone.low) / 2.0
        zone_created_utc = datetime.fromtimestamp(zone.creation_ts / 1000.0, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if zone.creation_ts else "N/A"
        trig_candle_utc = datetime.fromtimestamp(current_candle_ts / 1000.0, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if current_candle_ts else "N/A"

        return {
            "strategy_mode": "ORDER_BLOCK_DEMAND",
            "zone_id": zone.zone_id,
            "zone_type": zone.zone_type.value,
            "zone_high": zone.high,
            "zone_low": zone.low,
            "zone_mid": zone_mid,
            "zone_creation_bar_idx": zone.creation_bar_idx,
            "zone_creation_ts": zone.creation_ts,
            "zone_creation_time_utc": zone_created_utc,
            "bos_bar_idx": zone.bos_bar_idx,
            "bos_price": zone.bos_price,
            "retest_bar_idx": zone.retest_bar_idx,
            "confirmation_bar_idx": zone.confirmation_bar_idx,
            "eval_bar_idx": eval_idx,
            "trigger_candle_ts": current_candle_ts,
            "trigger_candle_time_utc": trig_candle_utc,
            "timeframe": self.timeframe,
            "entry_price": entry_price,
            "stop_loss_price": sl_price,
            "take_profit_price": tp_price,
            "target_ticks": target_ticks,
            "target_sl_ticks": risk_ticks,
            "target_1to1_ticks": risk_ticks,
            "target_1to1_price": target_1to1_price,
            "target_1to2_ticks": target_ticks,
            "target_1to2_price": tp_price,
            "risk_reward_ratio": self.risk_reward_ratio,
            "partial_tp_enabled": self.partial_tp_enabled,
            "breakeven_buffer_ticks": self.breakeven_buffer_ticks,
            "candle_timestamp": current_candle_ts,
            "pivot_len": self.pivot_len
        }

    def get_signal(self, symbol: Optional[str] = None) -> Optional[TradeSignal]:
        """Convenience alias for generate_signal(symbol)."""
        return self.generate_signal(symbol or self.symbol)

    def generate_signal(self, symbol: str) -> Optional[TradeSignal]:
        """
        Generates trading signals using the unified Vivek Yadav OB Strategy engine.
        Seamlessly supports:
        1. Exact single-candle tap + confirmation bounce (matches Vivek_Yadav_OB_Strategy_PnL.js)
        2. Multi-candle tap (Candle T) + confirmation (Candle T+1) for pre-registered zones
        """
        now = time.time()
        if not self.should_generate_signal(now):
            return None

        if now < self._rate_limit_backoff_until:
            return None

        # Fetch latest candlestick history with caching
        bars = None
        if (now - self._last_kline_fetch_ts < self.kline_cache_interval) and self._cached_candles:
            bars = self._cached_candles
        else:
            try:
                bars = self.market.get_klines(self.symbol, interval=self.interval, limit=120)
                if bars:
                    self._cached_candles = bars
                    self._last_kline_fetch_ts = now
            except Exception as e:
                is_rate_limit = ("510" in str(e)) or (getattr(e, "code", None) in (510, 429))
                if is_rate_limit:
                    self._rate_limit_backoff_until = now + 5.0
                bars = self._cached_candles

        if not bars or len(bars) < 10:
            self.last_rejection_reason = "Insufficient candle history"
            return None

        self._refresh_contract_spec()

        timestamps, opens, highs, lows, closes, volumes = self._extract_candle_series(bars)
        n = len(closes)
        eval_idx = n - 1 if not self.require_closed_candle else n - 2
        if eval_idx < 5:
            return None

        current_candle_ts = timestamps[eval_idx]
        if self.last_signal_candle_ts is not None and self.last_signal_candle_ts >= current_candle_ts:
            return None

        pu = self._price_unit if self._price_unit > 0 else 0.001
        prec = self._price_precision

        # 1. Run exact indicator engine calculation over available history
        drawn_zones, drawn_trades = self.calc_indicator_zones_and_trades(
            timestamps=timestamps[:eval_idx + 1],
            opens=opens[:eval_idx + 1],
            highs=highs[:eval_idx + 1],
            lows=lows[:eval_idx + 1],
            closes=closes[:eval_idx + 1],
            pivot_len=self.pivot_len,
            resolved_origin_ts=self.resolved_origin_ts,
            max_zone_age_bars=self.max_zone_age_bars
        )

        # Synchronize active zones with indicator zones: prune any invalidated / expired / mitigated
        drawn_zone_map = {z.zone_id: z for z in drawn_zones}
        drawn_ts_map = {z.creation_ts: z for z in drawn_zones}

        # 1. Register all resolved / invalidated / expired timestamps from indicator calculations
        for z in drawn_zones:
            if z.status in (ZoneStatus.INVALIDATED, ZoneStatus.EXPIRED):
                self._mark_resolved(z.creation_ts)
            elif z.status == ZoneStatus.MITIGATED:
                # Historical mitigations (prior to eval_idx) are marked resolved
                if z.end_idx is not None and z.end_idx < eval_idx:
                    self._mark_resolved(z.creation_ts)

        # 2. Prune existing active zones that have been resolved, invalidated, mitigated, or expired
        seen_creation_ts: Set[int] = set()
        for zid, az in list(self.active_zones.items()):
            # If resolved in current or prior cycles
            if self._is_resolved(az.creation_ts):
                del self.active_zones[zid]
                continue

            # If marked inactive in drawn_zones (by zone_id or creation_ts)
            ind_z = drawn_zone_map.get(zid) or drawn_ts_map.get(az.creation_ts)
            if ind_z and ind_z.status in (ZoneStatus.INVALIDATED, ZoneStatus.MITIGATED, ZoneStatus.EXPIRED):
                az.status = ind_z.status
                self._mark_resolved(az.creation_ts)
                self.history_zones.append(az)
                del self.active_zones[zid]
                continue

            # Max age expiry check
            if self.max_zone_age_bars > 0 and (eval_idx - az.creation_bar_idx) > self.max_zone_age_bars:
                az.status = ZoneStatus.EXPIRED
                self._mark_resolved(az.creation_ts)
                self.history_zones.append(az)
                del self.active_zones[zid]
                continue

            # Deduplication check: only one active zone per origin candle creation_ts
            if az.creation_ts and az.creation_ts in seen_creation_ts:
                del self.active_zones[zid]
                continue
            if az.creation_ts:
                seen_creation_ts.add(az.creation_ts)

        # 3. Add genuinely new ACTIVE zones from drawn_zones (avoiding duplicate creation_ts)
        for z in drawn_zones:
            if z.status == ZoneStatus.ACTIVE and not self._is_resolved(z.creation_ts):
                if self.max_zone_age_bars > 0 and (eval_idx - z.creation_bar_idx) > self.max_zone_age_bars:
                    z.status = ZoneStatus.EXPIRED
                    self._mark_resolved(z.creation_ts)
                    self.history_zones.append(z)
                    continue

                if not any(az.creation_ts == z.creation_ts for az in self.active_zones.values()):
                    self.active_zones[z.zone_id] = z

        # 2. Check for newly triggered trades on eval_idx from indicator calculation
        candidate_trade = None
        for t in drawn_trades:
            if t.get("start_idx") == eval_idx:
                t_zone = t.get("zone")
                if not t_zone:
                    continue
                # Invalidation check: never trade on a resolved or invalidated zone
                if self._is_resolved(t_zone.creation_ts):
                    continue
                # If zone exists in active_zones, it must not be marked inactive
                matching_az = self.active_zones.get(t_zone.zone_id) or next(
                    (az for az in self.active_zones.values() if az.creation_ts == t_zone.creation_ts),
                    None
                )
                if matching_az and matching_az.status not in (ZoneStatus.ACTIVE, ZoneStatus.TESTED):
                    continue
                candidate_trade = t
                break

        # 3. Check pre-registered / existing active zones against eval_idx candle
        c_open = opens[eval_idx]
        c_high = highs[eval_idx]
        c_low = lows[eval_idx]
        c_close = closes[eval_idx]
        c_range = max(1e-12, c_high - c_low)

        best_signal: Optional[TradeSignal] = None
        best_zone_id: Optional[str] = None

        if candidate_trade is not None:
            t_type = candidate_trade["type"]
            zone = candidate_trade["zone"]
            entry_price = candidate_trade["entry"]

            if t_type == "long":
                if self.preferred_direction is None or self.preferred_direction == OrderDirection.LONG:
                    sl_price = round(zone.low - (self.buffer_ticks * pu), prec)
                    risk_dist = entry_price - sl_price
                    risk_ticks = max(1, int(math.ceil(risk_dist / pu)))
                    target_ticks = max(1, int(round(risk_ticks * self.risk_reward_ratio)))
                    tp_price = round(entry_price + (target_ticks * pu), prec)
                    target_1to1_price = round(entry_price + (risk_ticks * pu), prec)

                    metadata = self._build_signal_metadata(
                        zone=zone,
                        entry_price=entry_price,
                        sl_price=sl_price,
                        tp_price=tp_price,
                        risk_ticks=risk_ticks,
                        target_ticks=target_ticks,
                        target_1to1_price=target_1to1_price,
                        current_candle_ts=current_candle_ts,
                        eval_idx=eval_idx,
                        prec=prec
                    )
                    best_signal = TradeSignal(
                        symbol=self.symbol,
                        direction=OrderDirection.LONG,
                        sub_strategy_name=f"{self.name}({zone.zone_type.value}-LONG)",
                        timestamp=now,
                        metadata=metadata
                    )
                    best_zone_id = zone.zone_id

            elif t_type == "short":
                if self.preferred_direction is None or self.preferred_direction == OrderDirection.SHORT:
                    sl_price = round(zone.high + (self.buffer_ticks * pu), prec)
                    risk_dist = sl_price - entry_price
                    risk_ticks = max(1, int(math.ceil(risk_dist / pu)))
                    target_ticks = max(1, int(round(risk_ticks * self.risk_reward_ratio)))
                    tp_price = round(entry_price - (target_ticks * pu), prec)
                    target_1to1_price = round(entry_price - (risk_ticks * pu), prec)

                    metadata = self._build_signal_metadata(
                        zone=zone,
                        entry_price=entry_price,
                        sl_price=sl_price,
                        tp_price=tp_price,
                        risk_ticks=risk_ticks,
                        target_ticks=target_ticks,
                        target_1to1_price=target_1to1_price,
                        current_candle_ts=current_candle_ts,
                        eval_idx=eval_idx,
                        prec=prec
                    )
                    best_signal = TradeSignal(
                        symbol=self.symbol,
                        direction=OrderDirection.SHORT,
                        sub_strategy_name=f"{self.name}({zone.zone_type.value}-SHORT)",
                        timestamp=now,
                        metadata=metadata
                    )
                    best_zone_id = zone.zone_id

        # 4. If no signal from indicator loop, evaluate existing active zones (supports 2-candle tap + confirmation tests)
        if best_signal is None:
            for zid, zone in list(self.active_zones.items()):
                if zone.creation_bar_idx >= eval_idx:
                    continue

                if self.max_zone_age_bars > 0 and (eval_idx - zone.creation_bar_idx) > self.max_zone_age_bars:
                    zone.status = ZoneStatus.EXPIRED
                    self._mark_resolved(zone.creation_ts)
                    self.history_zones.append(zone)
                    del self.active_zones[zid]
                    continue

                if zone.is_bullish:
                    # Invalidation check
                    if c_close < zone.low:
                        zone.status = ZoneStatus.INVALIDATED
                        self._mark_resolved(zone.creation_ts)
                        self.history_zones.append(zone)
                        del self.active_zones[zid]
                        continue

                    if self.preferred_direction is not None and self.preferred_direction != OrderDirection.LONG:
                        continue

                    # Tap & Rejection check (scan recent window [eval_idx - 2, eval_idx])
                    min_scan_bar = (zone.bos_bar_idx + 1) if zone.bos_bar_idx is not None else (zone.creation_bar_idx + 1)
                    if zone.status == ZoneStatus.ACTIVE:
                        for bar_k in range(max(min_scan_bar, eval_idx - 2), eval_idx + 1):
                            k_open = opens[bar_k]
                            k_high = highs[bar_k]
                            k_low = lows[bar_k]
                            k_close = closes[bar_k]
                            k_range = max(1e-12, k_high - k_low)
                            tapped = (k_low <= zone.high) and (k_high >= zone.low)
                            if tapped and k_close >= zone.low:
                                lower_wick = min(k_open, k_close) - k_low
                                if (lower_wick / k_range) >= self.min_rejection_wick_ratio:
                                    zone.status = ZoneStatus.TESTED
                                    zone.retest_bar_idx = bar_k
                                    zone.retest_wick_price = k_low
                                    break

                    # Confirmation check (Must confirm on the very next candle T+1)
                    if zone.status == ZoneStatus.TESTED and zone.retest_bar_idx is not None:
                        is_green_confirm = (c_close > c_open) and (c_close >= zone.low)
                        bars_since_retest = eval_idx - zone.retest_bar_idx

                        if bars_since_retest in (0, 1) and is_green_confirm:
                            wick_low = zone.retest_wick_price if zone.retest_wick_price is not None else zone.low
                            sl_price = round(min(zone.low, wick_low) - (self.buffer_ticks * pu), prec)
                            risk_dist = c_close - sl_price
                            risk_ticks = max(1, int(math.ceil(risk_dist / pu)))
                            target_ticks = max(1, int(round(risk_ticks * self.risk_reward_ratio)))
                            tp_price = round(c_close + (target_ticks * pu), prec)
                            target_1to1_price = round(c_close + (risk_ticks * pu), prec)

                            metadata = self._build_signal_metadata(
                                zone=zone,
                                entry_price=c_close,
                                sl_price=sl_price,
                                tp_price=tp_price,
                                risk_ticks=risk_ticks,
                                target_ticks=target_ticks,
                                target_1to1_price=target_1to1_price,
                                current_candle_ts=current_candle_ts,
                                eval_idx=eval_idx,
                                prec=prec
                            )
                            best_signal = TradeSignal(
                                symbol=self.symbol,
                                direction=OrderDirection.LONG,
                                sub_strategy_name=f"{self.name}({zone.zone_type.value}-LONG)",
                                timestamp=now,
                                metadata=metadata
                            )
                            best_zone_id = zid
                            break
                        elif bars_since_retest >= 1 and not is_green_confirm:
                            # Confirmation candle failed to close green on next bar -> INVALIDATE
                            zone.status = ZoneStatus.INVALIDATED
                            self._mark_resolved(zone.creation_ts)
                            self.history_zones.append(zone)
                            del self.active_zones[zid]
                            logger.info(
                                "[ZONE INVALIDATED] Bullish zone %s [%s - %s] invalidated: confirmation candle failed to close green on bar %d.",
                                zid, zone.low, zone.high, eval_idx
                            )
                            continue

                elif zone.is_bearish:
                    # Invalidation check (blowout above high)
                    if c_close > zone.high:
                        zone.status = ZoneStatus.INVALIDATED
                        self._mark_resolved(zone.creation_ts)
                        self.history_zones.append(zone)
                        del self.active_zones[zid]
                        continue

                    if self.preferred_direction is not None and self.preferred_direction != OrderDirection.SHORT:
                        continue

                    # Tap & Rejection check (scan recent window [eval_idx - 2, eval_idx])
                    min_scan_bar = (zone.bos_bar_idx + 1) if zone.bos_bar_idx is not None else (zone.creation_bar_idx + 1)
                    if zone.status == ZoneStatus.ACTIVE:
                        for bar_k in range(max(min_scan_bar, eval_idx - 2), eval_idx + 1):
                            k_open = opens[bar_k]
                            k_high = highs[bar_k]
                            k_low = lows[bar_k]
                            k_close = closes[bar_k]
                            k_range = max(1e-12, k_high - k_low)
                            tapped = (k_high >= zone.low) and (k_low <= zone.high)
                            if tapped and k_close <= zone.high:
                                upper_wick = k_high - max(k_open, k_close)
                                if (upper_wick / k_range) >= self.min_rejection_wick_ratio:
                                    zone.status = ZoneStatus.TESTED
                                    zone.retest_bar_idx = bar_k
                                    zone.retest_wick_price = k_high
                                    break

                    # Confirmation check (Must confirm on the very next candle T+1)
                    if zone.status == ZoneStatus.TESTED and zone.retest_bar_idx is not None:
                        is_red_confirm = (c_close < c_open) and (c_close <= zone.high)
                        bars_since_retest = eval_idx - zone.retest_bar_idx

                        if bars_since_retest in (0, 1) and is_red_confirm:
                            wick_high = zone.retest_wick_price if zone.retest_wick_price is not None else zone.high
                            sl_price = round(max(zone.high, wick_high) + (self.buffer_ticks * pu), prec)
                            risk_dist = sl_price - c_close
                            risk_ticks = max(1, int(math.ceil(risk_dist / pu)))
                            target_ticks = max(1, int(round(risk_ticks * self.risk_reward_ratio)))
                            tp_price = round(c_close - (target_ticks * pu), prec)
                            target_1to1_price = round(c_close - (risk_ticks * pu), prec)

                            metadata = self._build_signal_metadata(
                                zone=zone,
                                entry_price=c_close,
                                sl_price=sl_price,
                                tp_price=tp_price,
                                risk_ticks=risk_ticks,
                                target_ticks=target_ticks,
                                target_1to1_price=target_1to1_price,
                                current_candle_ts=current_candle_ts,
                                eval_idx=eval_idx,
                                prec=prec
                            )
                            best_signal = TradeSignal(
                                symbol=self.symbol,
                                direction=OrderDirection.SHORT,
                                sub_strategy_name=f"{self.name}({zone.zone_type.value}-SHORT)",
                                timestamp=now,
                                metadata=metadata
                            )
                            best_zone_id = zid
                            break
                        elif bars_since_retest >= 1 and not is_red_confirm:
                            # Confirmation candle failed to close red on next bar -> INVALIDATE
                            zone.status = ZoneStatus.INVALIDATED
                            self._mark_resolved(zone.creation_ts)
                            self.history_zones.append(zone)
                            del self.active_zones[zid]
                            logger.info(
                                "[ZONE INVALIDATED] Bearish zone %s [%s - %s] invalidated: confirmation candle failed to close red on bar %d.",
                                zid, zone.low, zone.high, eval_idx
                            )
                            continue

        if best_signal is not None:
            self.last_signal_candle_ts = current_candle_ts
            self.trade_in_progress = True

            traded_zid = best_zone_id or best_signal.metadata.get("zone_id")
            traded_creation_ts = best_signal.metadata.get("zone_creation_ts")

            # 1. Permanently register origin timestamp as resolved/consumed so it can never re-arm
            if traded_creation_ts:
                self._mark_resolved(traded_creation_ts)

            # 2. Immediately mark any matching zone in active_zones as MITIGATED and purge
            for zid in list(self.active_zones.keys()):
                az = self.active_zones[zid]
                if zid == traded_zid or (traded_creation_ts and az.creation_ts == traded_creation_ts):
                    az.status = ZoneStatus.MITIGATED
                    az.confirmation_bar_idx = eval_idx
                    self._mark_resolved(az.creation_ts)
                    self.history_zones.append(az)
                    del self.active_zones[zid]

            logger.info(
                "[ORDER BLOCK + DEMAND SIGNAL] %s on %s | Zone: %s [%.4f - %.4f] | TP: +%dt ($%.4f), SL: -%dt ($%.4f) | 1:%.1f RR",
                best_signal.direction.value,
                self.symbol,
                best_signal.metadata.get("zone_type"),
                best_signal.metadata.get("zone_low", 0.0),
                best_signal.metadata.get("zone_high", 0.0),
                best_signal.metadata.get("target_ticks", 0),
                best_signal.metadata.get("take_profit_price", 0.0),
                best_signal.metadata.get("target_sl_ticks", 0),
                best_signal.metadata.get("stop_loss_price", 0.0),
                best_signal.metadata.get("risk_reward_ratio", 2.0)
            )
            return best_signal

        return None

    def get_parameters(self) -> Dict[str, Any]:
        return {
            "strategy": "ORDER_BLOCK_DEMAND",
            "symbol": self.symbol,
            "interval": self.interval,
            "pivot_len": self.pivot_len,
            "risk_reward_ratio": self.risk_reward_ratio,
            "buffer_ticks": self.buffer_ticks,
            "active_zones_count": len(self.active_zones),
            "resolved_origin_count": len(self.resolved_origin_ts)
        }

    def get_diagnostics(self) -> Dict[str, Any]:
        cached_price = None
        if self._cached_candles:
            try:
                last_c = self._cached_candles[-1]
                if hasattr(last_c, "close"):
                    cached_price = float(last_c.close)
                elif isinstance(last_c, dict):
                    cached_price = float(last_c.get("close", 0.0))
                elif isinstance(last_c, (list, tuple)) and len(last_c) >= 5:
                    cached_price = float(last_c[4])
            except Exception:
                pass

        valid_active_zones = [
            z for z in self.active_zones.values()
            if z.status in (ZoneStatus.ACTIVE, ZoneStatus.TESTED)
            and z.creation_ts not in self.resolved_origin_ts
        ]
        bullish_zones = [z for z in valid_active_zones if z.is_bullish]
        bearish_zones = [z for z in valid_active_zones if z.is_bearish]

        sorted_zones = valid_active_zones
        if cached_price is not None:
            sorted_zones = sorted(
                valid_active_zones,
                key=lambda z: min(abs(cached_price - z.high), abs(cached_price - z.low))
            )

        return {
            "strategy": "ORDER_BLOCK_DEMAND",
            "timeframe": self.timeframe,
            "active_zones_count": len(valid_active_zones),
            "demand_zones_count": len(bullish_zones),
            "supply_zones_count": len(bearish_zones),
            "cached_price": cached_price,
            "zones": [
                {
                    "id": z.zone_id,
                    "type": z.zone_type.value,
                    "high": z.high,
                    "low": z.low,
                    "mid": (z.high + z.low) / 2.0,
                    "bar": z.creation_bar_idx,
                    "status": z.status.value
                }
                for z in sorted_zones
            ],
            "last_rejection_reason": self.last_rejection_reason
        }


# Backwards compatibility aliases
OrderBookDemandStrategy = OrderBlockDemandStrategy
OrderBlockDemandSubStrategy = OrderBlockDemandStrategy
