"""
Tick-Constrained Microstructure Market Making & Simultaneous Hedging Strategy
=============================================================================
High-frequency market making and micro-scalping engine tailored for zero-fee
contracts on KCEX (e.g. TRUMP_USDT).

Key Architectural Pillars:
1. Relative Tick Size Gatekeeper:
   - Verifies (tick_size / price) * 10,000 >= min_tick_bps (e.g. >= 4.0 bps).
   - Only quotes when the contract is in a large-tick (tick-constrained) regime where
     the bid-ask spread is pinned at 1 tick.
2. Order Flow Imbalance (OFI) Toxic Flow Detector:
   - Tracks rolling aggressive buyer-maker vs seller-taker ratio over recent trades.
   - Suppresses quoting into toxic momentum sweeps (|OFI| > max_ofi_threshold).
3. Higher-Timeframe (HTF) Sideways / Volatility Compression Gatekeeper:
   - Evaluates 15m/5m Bollinger Bandwidth (BBW) percentile squeeze (<= 40th percentile)
     and ADX (< 22) to prevent quoting right before macro breakout expansions.
4. Corrected Maker/Taker Passive Scratch Exits:
   - Takes full advantage of KCEX 0.00% maker & taker fee structure.
   - For LONG: passive scratch at entry price is filled by aggressive market BUY (is_buyer_maker=False).
   - For SHORT: passive scratch at entry price is filled by aggressive market SELL (is_buyer_maker=True).
5. Asymmetric Micro-Stops & Synchronized Leg Protection:
   - Uses tight 2-3 tick stops to strictly eliminate tail risk.
   - In simultaneous hedge mode, the moment Leg A hits +1 tick TP, Leg B is immediately
     ratcheted to breakeven or scratched before an adverse breakout.
"""

from __future__ import annotations
import math
import time
import logging
from dataclasses import dataclass, field
from collections import deque
from typing import Optional, Dict, Any, List, Tuple, TYPE_CHECKING
import numpy as np

if TYPE_CHECKING:
    from kcex.market import KCEXMarket

from kcex.engine.models import OrderDirection, TradeSignal, TradeOutcome
from strategies.base import BaseStrategy

logger = logging.getLogger("TickConstrainedMMStrategy")


@dataclass
class TickConstrainedConfig:
    """Configuration hyperparameters for Tick-Constrained Market Making."""
    tick_size: float = 0.001
    min_tick_bps: float = 4.0            # Min tick size in basis points to activate strategy
    tp_ticks: int = 1                   # Take Profit target in ticks (+1 tick)
    sl_ticks: int = 3                   # Stop Loss in ticks (micro-stop, 2-3 ticks max)
    entry_queue_qty: float = 200.0      # Estimated contracts ahead in queue at entry
    tp_queue_qty: float = 200.0         # Estimated contracts ahead in queue at TP limit exit
    ofi_window: int = 50                # Order Flow Imbalance window in trades
    max_ofi_threshold: float = 0.40     # Toxic order flow threshold (0.0 to 1.0)
    time_stop_sec: float = 60.0         # Max hold time before attempting passive scratch
    scratch_spread_penalty: float = 0.0 # Penalty in ticks if emergency market exit is required
    use_htf_filter: bool = True         # Require 15m sideways condition
    htf_timeframe: str = "Min15"        # KCEX interval for HTF filter
    bb_period: int = 20                 # Bollinger Bands period
    bb_std: float = 2.0                 # Bollinger Bands standard deviation
    bbw_percentile_cutoff: float = 40.0 # Max BBW rolling percentile (volatility compression)
    adx_period: int = 14                # ADX period
    max_adx_sideways: float = 22.0      # ADX ceiling for sideways consolidation
    cooldown_seconds: float = 0.0       # Cooldown between trade cycles
    simultaneous_mode: bool = False     # If True, emits dual Long+Short hedged signals


def compute_ofi_from_trades(trades: List[Dict[str, Any]], window: int = 50) -> float:
    """
    Computes Order Flow Imbalance (OFI) from recent trades:
    OFI = (V_buy - V_sell) / (V_buy + V_sell)
    Returns a float in [-1.0, 1.0].
    """
    if not trades:
        return 0.0
    
    recent = trades[-window:] if len(trades) > window else trades
    buy_vol = 0.0
    sell_vol = 0.0

    for t in recent:
        qty = float(t.get("qty", t.get("vol", t.get("v", t.get("amount", 1.0)))))
        # In Binance & KCEX: is_buyer_maker = True means market SELL hit a maker BUY
        # is_buyer_maker = False means market BUY hit a maker SELL
        bm = t.get("is_buyer_maker", t.get("isBuyerMaker", None))
        if bm is None:
            # Fallback to side or KCEX 'T' (1=Buy, 2=Sell)
            side = str(t.get("side", t.get("T", ""))).upper()
            if side in ("SELL", "2"):
                sell_vol += qty
            else:
                buy_vol += qty
        else:
            if bm:
                sell_vol += qty
            else:
                buy_vol += qty

    tot = buy_vol + sell_vol
    if tot <= 1e-9:
        return 0.0
    return (buy_vol - sell_vol) / tot


def compute_bollinger_bandwidth(closes: List[float], period: int = 20, num_std: float = 2.0) -> float:
    """Computes Bollinger Bandwidth = (Upper - Lower) / SMA."""
    if len(closes) < period:
        return 0.0
    window = closes[-period:]
    sma = sum(window) / period
    if sma <= 1e-9:
        return 0.0
    variance = sum((x - sma) ** 2 for x in window) / period
    std = math.sqrt(variance)
    upper = sma + (num_std * std)
    lower = sma - (num_std * std)
    return (upper - lower) / sma


def compute_adx_scalar(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """Computes ADX for the latest bar in the series."""
    n = len(closes)
    if n < period * 2:
        return 15.0  # Default neutral/sideways if insufficient bars

    tr_list: List[float] = []
    plus_dm_list: List[float] = []
    minus_dm_list: List[float] = []

    for i in range(1, n):
        h, l, c_prev = highs[i], lows[i], closes[i - 1]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        tr_list.append(tr)

        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        if up_move > down_move and up_move > 0:
            plus_dm_list.append(up_move)
        else:
            plus_dm_list.append(0.0)

        if down_move > up_move and down_move > 0:
            minus_dm_list.append(down_move)
        else:
            minus_dm_list.append(0.0)

    if len(tr_list) < period:
        return 15.0

    # Wilder's Smoothing for TR and DMs
    tr_smooth = sum(tr_list[:period])
    plus_dm_smooth = sum(plus_dm_list[:period])
    minus_dm_smooth = sum(minus_dm_list[:period])

    dx_list: List[float] = []
    for i in range(period, len(tr_list)):
        tr_smooth = tr_smooth - (tr_smooth / period) + tr_list[i]
        plus_dm_smooth = plus_dm_smooth - (plus_dm_smooth / period) + plus_dm_list[i]
        minus_dm_smooth = minus_dm_smooth - (minus_dm_smooth / period) + minus_dm_list[i]

        if tr_smooth <= 1e-9:
            continue

        plus_di = 100.0 * (plus_dm_smooth / tr_smooth)
        minus_di = 100.0 * (minus_dm_smooth / tr_smooth)

        di_sum = plus_di + minus_di
        if di_sum > 1e-9:
            dx = 100.0 * abs(plus_di - minus_di) / di_sum
            dx_list.append(dx)

    if not dx_list:
        return 15.0

    # ADX = smoothed DX
    adx_len = min(period, len(dx_list))
    return sum(dx_list[-adx_len:]) / adx_len


class TickConstrainedMMStrategy(BaseStrategy):
    """
    Tick-Constrained Microstructure Market Making Strategy for KCEX.
    """

    def __init__(
        self,
        market: Optional[KCEXMarket] = None,
        symbol: str = "TRUMP_USDT",
        config: Optional[TickConstrainedConfig] = None,
        preferred_direction: Optional[OrderDirection] = None
    ):
        super().__init__(name="TICK_CONSTRAINED_MM")
        self.market = market
        self.symbol = symbol.upper()
        self.cfg = config or TickConstrainedConfig()
        self.preferred_direction = preferred_direction
        self.last_trade_completed_time: float = 0.0
        self.recent_trades_buffer: deque = deque(maxlen=self.cfg.ofi_window * 2)

        # Diagnostics cache
        self.last_diagnostics: Dict[str, Any] = {
            "tick_bps": 0.0,
            "is_tick_constrained": False,
            "ofi_ratio": 0.0,
            "is_sideways": True,
            "bbw": 0.0,
            "bbw_pct": 0.0,
            "adx": 0.0,
            "status": "INITIALIZED"
        }

    def should_generate_signal(self, current_time: float) -> bool:
        """Checks if strategy is uncooled and ready to evaluate market microstructure."""
        rem = self.get_remaining_cooldown(current_time)
        return rem <= 0.0

    def get_remaining_cooldown(self, current_time: float) -> float:
        """Returns remaining cooldown time in seconds."""
        elapsed = current_time - self.last_trade_completed_time
        rem = self.cfg.cooldown_seconds - elapsed
        return max(0.0, rem)

    def on_trade_completed(self, outcome: TradeOutcome) -> None:
        """Callback when active trade or leg completes."""
        self.last_trade_completed_time = time.time()
        pnl_val = getattr(outcome, "realized_pnl_usdt", getattr(outcome, "pnl_usdt", 0.0))
        pnl_t = getattr(outcome, "pnl_ticks", 0.0)
        logger.info(
            f"[TickConstrainedMM] Trade completed: {outcome.direction.value} "
            f"exit={outcome.exit_reason.value} pnl={pnl_val:+.4f} USDT "
            f"({pnl_t:+.1f} ticks)"
        )

    def evaluate_market_state(self) -> Dict[str, Any]:
        """
        Evaluates current market conditions (tick bps, OFI, and HTF regime).
        """
        diag = dict(self.last_diagnostics)

        if not self.market:
            diag["status"] = "NO_MARKET"
            return diag

        # 1. Fetch ticker & contract info
        contract = self.market.get_contract_detail(self.symbol)
        pu = contract.price_unit if contract and contract.price_unit > 0 else self.cfg.tick_size
        self.cfg.tick_size = pu

        ticker = self.market.get_ticker(self.symbol)
        price = float(ticker.get("lastPrice", 0.0) or ticker.get("fairPrice", 1.0))
        if price <= 0:
            diag["status"] = "INVALID_PRICE"
            return diag

        # Calculate Relative Tick Size in Basis Points
        tick_bps = (pu / price) * 10000.0
        is_tick_constrained = (tick_bps >= self.cfg.min_tick_bps)
        diag["tick_bps"] = round(tick_bps, 2)
        diag["is_tick_constrained"] = is_tick_constrained

        # 2. Fetch Recent Trades & Calculate OFI
        try:
            try:
                trades = self.market.get_recent_trades(self.symbol, limit=self.cfg.ofi_window)
            except TypeError:
                trades = self.market.get_recent_trades(self.symbol)
            ofi_ratio = compute_ofi_from_trades(trades, window=self.cfg.ofi_window)
        except Exception as e:
            logger.debug("Failed to fetch recent trades for OFI: %s", e)
            ofi_ratio = 0.0
        diag["ofi_ratio"] = round(ofi_ratio, 4)

        # 3. Higher-Timeframe Sideways Filter (15m Candles)
        is_sideways = True
        bbw_val = 0.0
        adx_val = 15.0
        bbw_pct = 20.0

        if self.cfg.use_htf_filter:
            try:
                klines = self.market.get_klines(self.symbol, interval=self.cfg.htf_timeframe, limit=120)
                if len(klines) >= self.cfg.bb_period + 10:
                    closes = [float(k.get("close", k.get("c", 0))) for k in klines]
                    highs = [float(k.get("high", k.get("h", 0))) for k in klines]
                    lows = [float(k.get("low", k.get("l", 0))) for k in klines]

                    bbw_val = compute_bollinger_bandwidth(closes, period=self.cfg.bb_period, num_std=self.cfg.bb_std)
                    adx_val = compute_adx_scalar(highs, lows, closes, period=self.cfg.adx_period)

                    # Estimate BBW percentile over past bars
                    bbw_history = []
                    for i in range(self.cfg.bb_period, len(closes) + 1):
                        bbw_history.append(compute_bollinger_bandwidth(closes[:i], period=self.cfg.bb_period))
                    
                    if bbw_history:
                        current_bbw = bbw_history[-1]
                        min_b = min(bbw_history)
                        max_b = max(bbw_history)
                        if max_b - min_b <= 1e-9 or current_bbw <= 1e-6:
                            bbw_pct = 0.0
                        else:
                            bbw_pct = (sum(1 for b in bbw_history if b < current_bbw) / len(bbw_history)) * 100.0

                    is_sideways = (adx_val < self.cfg.max_adx_sideways) and (bbw_pct <= self.cfg.bbw_percentile_cutoff)
            except Exception as e:
                logger.debug("Error computing HTF indicators: %s", e)
                is_sideways = True

        diag["is_sideways"] = is_sideways
        diag["bbw"] = round(bbw_val, 6)
        diag["bbw_pct"] = round(bbw_pct, 1)
        diag["adx"] = round(adx_val, 2)
        diag["status"] = "OK"

        self.last_diagnostics = diag
        return diag

    def generate_signal(self, symbol: str) -> Optional[TradeSignal]:
        """
        Generates a microstructure trade signal if conditions are satisfied.
        """
        curr_time = time.time()
        if not self.should_generate_signal(curr_time):
            return None

        state = self.evaluate_market_state()
        if not state.get("is_tick_constrained", False):
            logger.debug(f"[TickConstrainedMM] Tick bps {state.get('tick_bps')} < {self.cfg.min_tick_bps}. Filtered.")
            return None

        if self.cfg.use_htf_filter and not state.get("is_sideways", True):
            logger.debug(f"[TickConstrainedMM] HTF regime not sideways (ADX={state.get('adx')}, BBW%={state.get('bbw_pct')}). Filtered.")
            return None

        ofi = state.get("ofi_ratio", 0.0)

        # Decide Direction based on OFI and settings
        if self.cfg.simultaneous_mode:
            # Emits dual simultaneous entry signal
            direction = OrderDirection.LONG if self.preferred_direction != OrderDirection.SHORT else OrderDirection.SHORT
            mode_tag = "SIMULTANEOUS_HEDGE"
        else:
            # Single-sided Micro-Market Making: quote in direction of supportive flow
            if ofi > -self.cfg.max_ofi_threshold and ofi <= 0.0:
                # Slight sell flow or neutral: quote BID (Long) to earn maker spread
                direction = OrderDirection.LONG
            elif ofi < self.cfg.max_ofi_threshold and ofi >= 0.0:
                # Slight buy flow or neutral: quote ASK (Short) to earn maker spread
                direction = OrderDirection.SHORT
            elif ofi > self.cfg.max_ofi_threshold:
                # Toxic buy surge: avoid quoting Short, or quote Long with momentum
                direction = OrderDirection.LONG
            else:
                # Toxic sell surge: avoid quoting Long, or quote Short with momentum
                direction = OrderDirection.SHORT
            mode_tag = "MICRO_MARKET_MAKER"

        if self.preferred_direction and direction != self.preferred_direction and not self.cfg.simultaneous_mode:
            return None

        metadata = {
            "strategy_mode": "TICK_CONSTRAINED_MM",
            "execution_mode": mode_tag,
            "target_ticks": self.cfg.tp_ticks,
            "target_sl_ticks": self.cfg.sl_ticks,
            "tick_bps": state.get("tick_bps"),
            "ofi_ratio": ofi,
            "htf_is_sideways": state.get("is_sideways"),
            "bbw_pct": state.get("bbw_pct"),
            "adx": state.get("adx"),
            "entry_style": "MAKER_HYBRID",
            "order_type": "LIMIT",
            "time_stop_sec": self.cfg.time_stop_sec,
            "entry_queue_qty": self.cfg.entry_queue_qty,
            "tp_queue_qty": self.cfg.tp_queue_qty
        }

        return TradeSignal(
            symbol=symbol,
            direction=direction,
            sub_strategy_name=self.name,
            timestamp=curr_time,
            metadata=metadata
        )

    def get_parameters(self) -> Dict[str, Any]:
        """Returns hyperparameters for reporting and analytics."""
        return {
            "strategy": self.name,
            "symbol": self.symbol,
            "min_tick_bps": self.cfg.min_tick_bps,
            "tp_ticks": self.cfg.tp_ticks,
            "sl_ticks": self.cfg.sl_ticks,
            "ofi_window": self.cfg.ofi_window,
            "max_ofi_threshold": self.cfg.max_ofi_threshold,
            "time_stop_sec": self.cfg.time_stop_sec,
            "use_htf_filter": self.cfg.use_htf_filter,
            "htf_timeframe": self.cfg.htf_timeframe,
            "bbw_percentile_cutoff": self.cfg.bbw_percentile_cutoff,
            "max_adx_sideways": self.cfg.max_adx_sideways,
            "simultaneous_mode": self.cfg.simultaneous_mode
        }

    def get_diagnostics(self) -> Dict[str, Any]:
        """Returns live diagnostics and indicators."""
        return dict(self.last_diagnostics)


# Backwards-compatibility alias
TickConstrainedSubStrategy = TickConstrainedMMStrategy


class TickConstrainedSimulator:
    """
    High-fidelity microstructure simulator for backtesting tick-constrained market making
    directly on historical tick trade DataFrames (Binance / KCEX dual-feed).
    Fixes all maker/taker inverted logic, includes realistic slippage on stop triggers,
    and supports symmetric FIFO queue modeling.
    """

    def __init__(self, config: Optional[TickConstrainedConfig] = None):
        self.cfg = config or TickConstrainedConfig()

    def run_simulation(
        self,
        df: Any,
        htf_is_sideways: Optional[np.ndarray] = None,
        save_csv_path: Optional[str] = None
    ) -> Dict[str, Any]:
        prices = df['price'].values
        buyer_maker = df['is_buyer_maker'].values
        qtys = df['qty'].values
        timestamps = df['time'].values
        n = len(prices)

        tick_size = self.cfg.tick_size
        avg_price = float(np.mean(prices))
        tick_bps = (tick_size / avg_price) * 10000.0

        # Check if asset is tick-constrained in this dataset
        is_tick_constrained = tick_bps >= self.cfg.min_tick_bps

        # Inventory states: 1 = Long, -1 = Short, 0 = Flat
        inventory = 0
        entry_price = 0.0
        entry_time = 0
        entry_ofi = 0.0

        resting_bid = 0.0
        resting_ask = 0.0
        bid_queue = 0.0
        ask_queue = 0.0
        tp_queue = 0.0

        realized_pnl_ticks = 0.0
        trades_closed = 0
        wins = 0
        losses = 0
        scratches = 0

        peak_pnl = 0.0
        max_dd = 0.0

        # Precompute taker buy/sell indicator for fast OFI
        taker_dirs = np.where(buyer_maker, -1.0, 1.0)
        vol_dirs = qtys * taker_dirs

        cum_vols = np.cumsum(qtys)
        cum_dir_vols = np.cumsum(vol_dirs)

        holding_times: List[float] = []
        trade_pnls: List[float] = []
        trade_records: List[Dict[str, Any]] = []

        w = self.cfg.ofi_window
        for i in range(w, n):
            p = prices[i]
            bm = buyer_maker[i]
            q = qtys[i]
            t = timestamps[i]

            # 1. Rolling Order Flow Imbalance (OFI)
            tot_vol = cum_vols[i] - cum_vols[i - w]
            net_dir_vol = cum_dir_vols[i] - cum_dir_vols[i - w]
            ofi_ratio = net_dir_vol / (tot_vol + 1e-9)

            # HTF sideways check
            sideways_ok = True
            if self.cfg.use_htf_filter and htf_is_sideways is not None:
                sideways_ok = bool(htf_is_sideways[i])

            # 2. Check Exits for Existing Open Position
            if inventory == 1:  # Holding LONG
                tp_price = round(entry_price + self.cfg.tp_ticks * tick_size, 6)
                sl_price = round(entry_price - self.cfg.sl_ticks * tick_size, 6)
                time_held_sec = (t - entry_time) / 1000.0
                closed = False
                exit_type = ""
                pnl = 0.0
                exit_p = p

                # Hard Stop Loss (market sell punched down through SL with realistic slippage)
                if p <= sl_price:
                    # Realistic execution fills at min(sl_price, p)
                    exit_p = min(sl_price, p)
                    raw_loss = (exit_p - entry_price) / tick_size
                    pnl = raw_loss
                    exit_type = "HARD_STOP_LOSS"
                    losses += 1
                    closed = True

                # Take Profit Limit Order (exits passively as maker ASK)
                # Filled when aggressive market BUY hits the ask (not bm) or market trades above tp_price
                elif p > tp_price:
                    pnl = float(self.cfg.tp_ticks)
                    exit_p = tp_price
                    exit_type = "TAKE_PROFIT"
                    wins += 1
                    closed = True

                elif p == tp_price and not bm:
                    tp_queue -= q
                    if tp_queue <= 0:
                        pnl = float(self.cfg.tp_ticks)
                        exit_p = tp_price
                        exit_type = "TAKE_PROFIT"
                        wins += 1
                        closed = True

                # Toxic Flow Exit: if OFI drops adversely below -threshold and price broke below entry
                elif ofi_ratio < -self.cfg.max_ofi_threshold and p < entry_price:
                    raw_pnl = (p - entry_price) / tick_size
                    pnl = raw_pnl - self.cfg.scratch_spread_penalty
                    exit_p = p
                    exit_type = "TOXIC_OFI_SCRATCH"
                    losses += 1
                    closed = True

                # Passive Time Stop: if trade lingers > time_stop_sec and price is at or above entry
                # FIXED: To exit Long passively (maker ask), an aggressive buyer must buy (not bm)
                elif time_held_sec > self.cfg.time_stop_sec and p >= entry_price and not bm:
                    pnl = 0.0
                    exit_p = entry_price
                    exit_type = "TIME_STOP_SCRATCH"
                    scratches += 1
                    closed = True

                if closed:
                    realized_pnl_ticks += pnl
                    inventory = 0
                    trades_closed += 1
                    holding_times.append(time_held_sec)
                    trade_pnls.append(pnl)

                    trade_records.append({
                        "trade_id": trades_closed,
                        "direction": "LONG",
                        "entry_time": t,
                        "exit_time": t,
                        "duration_sec": round(time_held_sec, 2),
                        "entry_price": entry_price,
                        "exit_price": exit_p,
                        "tp_target_price": tp_price,
                        "sl_target_price": sl_price,
                        "exit_type": exit_type,
                        "pnl_ticks": round(pnl, 2),
                        "ofi_at_entry": round(entry_ofi, 4),
                        "ofi_at_exit": round(ofi_ratio, 4),
                        "cum_pnl_ticks": round(realized_pnl_ticks, 2)
                    })

            elif inventory == -1:  # Holding SHORT
                tp_price = round(entry_price - self.cfg.tp_ticks * tick_size, 6)
                sl_price = round(entry_price + self.cfg.sl_ticks * tick_size, 6)
                time_held_sec = (t - entry_time) / 1000.0
                closed = False
                exit_type = ""
                pnl = 0.0
                exit_p = p

                # Hard Stop Loss (market buy punched up through SL with realistic slippage)
                if p >= sl_price:
                    exit_p = max(sl_price, p)
                    raw_loss = (entry_price - exit_p) / tick_size
                    pnl = raw_loss
                    exit_type = "HARD_STOP_LOSS"
                    losses += 1
                    closed = True

                # Take Profit Limit Order (exits passively as maker BID)
                # Filled when aggressive market SELL hits the bid (bm) or market trades below tp_price
                elif p < tp_price:
                    pnl = float(self.cfg.tp_ticks)
                    exit_p = tp_price
                    exit_type = "TAKE_PROFIT"
                    wins += 1
                    closed = True

                elif p == tp_price and bm:
                    tp_queue -= q
                    if tp_queue <= 0:
                        pnl = float(self.cfg.tp_ticks)
                        exit_p = tp_price
                        exit_type = "TAKE_PROFIT"
                        wins += 1
                        closed = True

                # Toxic Flow Exit: if OFI rises adversely above threshold and price broke above entry
                elif ofi_ratio > self.cfg.max_ofi_threshold and p > entry_price:
                    raw_pnl = (entry_price - p) / tick_size
                    pnl = raw_pnl - self.cfg.scratch_spread_penalty
                    exit_p = p
                    exit_type = "TOXIC_OFI_SCRATCH"
                    losses += 1
                    closed = True

                # Passive Time Stop: if trade lingers > time_stop_sec and price is at or below entry
                # FIXED: To exit Short passively (maker bid), an aggressive seller must sell (bm)
                elif time_held_sec > self.cfg.time_stop_sec and p <= entry_price and bm:
                    pnl = 0.0
                    exit_p = entry_price
                    exit_type = "TIME_STOP_SCRATCH"
                    scratches += 1
                    closed = True

                if closed:
                    realized_pnl_ticks += pnl
                    inventory = 0
                    trades_closed += 1
                    holding_times.append(time_held_sec)
                    trade_pnls.append(pnl)

                    trade_records.append({
                        "trade_id": trades_closed,
                        "direction": "SHORT",
                        "entry_time": t,
                        "exit_time": t,
                        "duration_sec": round(time_held_sec, 2),
                        "entry_price": entry_price,
                        "exit_price": exit_p,
                        "tp_target_price": tp_price,
                        "sl_target_price": sl_price,
                        "exit_type": exit_type,
                        "pnl_ticks": round(pnl, 2),
                        "ofi_at_entry": round(entry_ofi, 4),
                        "ofi_at_exit": round(ofi_ratio, 4),
                        "cum_pnl_ticks": round(realized_pnl_ticks, 2)
                    })

            # 3. Order Quoting & Fill Simulation
            if bm:
                curr_bid = p
                curr_ask = round(p + tick_size, 6)
            else:
                curr_ask = p
                curr_bid = round(p - tick_size, 6)

            if inventory == 0 and sideways_ok and is_tick_constrained:
                # 3a. Check Buy Quote Fill
                if resting_bid > 0:
                    if p < resting_bid:  # Adverse sweep
                        inventory = 1
                        entry_price = resting_bid
                        entry_time = t
                        entry_ofi = ofi_ratio
                        resting_bid = 0.0
                        resting_ask = 0.0
                        tp_queue = self.cfg.tp_queue_qty
                    elif p == resting_bid and bm:
                        bid_queue -= q
                        if bid_queue <= 0:
                            inventory = 1
                            entry_price = resting_bid
                            entry_time = t
                            entry_ofi = ofi_ratio
                            resting_bid = 0.0
                            resting_ask = 0.0
                            tp_queue = self.cfg.tp_queue_qty
                    elif abs(curr_bid - resting_bid) > 1e-6:
                        resting_bid = 0.0

                # 3b. Check Sell Quote Fill
                if resting_ask > 0 and inventory == 0:
                    if p > resting_ask:  # Adverse sweep
                        inventory = -1
                        entry_price = resting_ask
                        entry_time = t
                        entry_ofi = ofi_ratio
                        resting_bid = 0.0
                        resting_ask = 0.0
                        tp_queue = self.cfg.tp_queue_qty
                    elif p == resting_ask and not bm:
                        ask_queue -= q
                        if ask_queue <= 0:
                            inventory = -1
                            entry_price = resting_ask
                            entry_time = t
                            entry_ofi = ofi_ratio
                            resting_bid = 0.0
                            resting_ask = 0.0
                            tp_queue = self.cfg.tp_queue_qty
                    elif abs(curr_ask - resting_ask) > 1e-6:
                        resting_ask = 0.0

                # 3c. Place new resting quotes
                if inventory == 0:
                    if ofi_ratio > -self.cfg.max_ofi_threshold and resting_bid == 0.0:
                        resting_bid = curr_bid
                        bid_queue = self.cfg.entry_queue_qty
                    if ofi_ratio < self.cfg.max_ofi_threshold and resting_ask == 0.0:
                        resting_ask = curr_ask
                        ask_queue = self.cfg.entry_queue_qty
            elif inventory == 0:
                resting_bid = 0.0
                resting_ask = 0.0

            # Track Drawdown
            if realized_pnl_ticks > peak_pnl:
                peak_pnl = realized_pnl_ticks
            dd = peak_pnl - realized_pnl_ticks
            if dd > max_dd:
                max_dd = dd

        win_rate = (wins / trades_closed * 100.0) if trades_closed > 0 else 0.0
        scratch_rate = (scratches / trades_closed * 100.0) if trades_closed > 0 else 0.0
        loss_rate = (losses / trades_closed * 100.0) if trades_closed > 0 else 0.0

        gross_profit = sum(p for p in trade_pnls if p > 0)
        gross_loss = abs(sum(p for p in trade_pnls if p < 0))
        profit_factor = (gross_profit / (gross_loss + 1e-9)) if gross_loss > 0 else gross_profit
        avg_hold = float(np.mean(holding_times)) if holding_times else 0.0

        return {
            "tick_bps": tick_bps,
            "is_tick_constrained": is_tick_constrained,
            "total_trades": trades_closed,
            "realized_pnl_ticks": realized_pnl_ticks,
            "realized_pnl_usd_per_unit": realized_pnl_ticks * tick_size,
            "wins": wins,
            "losses": losses,
            "scratches": scratches,
            "win_rate_pct": win_rate,
            "scratch_rate_pct": scratch_rate,
            "loss_rate_pct": loss_rate,
            "profit_factor": profit_factor,
            "max_drawdown_ticks": max_dd,
            "avg_holding_time_sec": avg_hold,
            "trades": trade_records
        }
