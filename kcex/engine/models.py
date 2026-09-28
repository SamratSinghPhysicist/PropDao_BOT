"""
KCEX Trade Execution Engine - Models & Data Structures
======================================================
Defines core data structures for strategies, signals, execution state,
and trade outcomes with dual-currency (USDT & INR) representations.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from enum import Enum
from datetime import datetime, timezone
import os
import time

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


class OrderDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class ExitReason(str, Enum):
    MIN_PROFIT_TP_HIT = "MIN_PROFIT_TP_HIT"
    IMMEDIATE_PROFIT_CLOSE = "IMMEDIATE_PROFIT_CLOSE"
    STOP_LOSS_HIT = "STOP_LOSS_HIT"
    SCRATCH_CLOSE = "SCRATCH_CLOSE"
    MANUAL_CLOSE = "MANUAL_CLOSE"
    TIMEOUT_CLOSE = "TIMEOUT_CLOSE"
    DURATION_SCRATCH = "DURATION_SCRATCH"
    # Phase V2.1, V2.2 & V3.1 Quantitative Trailing Stop, Queue Exit and Liquidation Markers
    RATCHET_TIGHTEN_HIT = "RATCHET_TIGHTEN_HIT"        # Exited at tightened -1 tick stop
    RATCHET_BREAKEVEN_HIT = "RATCHET_BREAKEVEN_HIT"    # Exited at 0.0 tick breakeven scratch
    QUEUE_TIMEOUT_CANCELLED = "QUEUE_TIMEOUT_CANCELLED" # Maker limit entry order timed out
    LIQUIDATION_HIT = "LIQUIDATION_HIT"                # 75x Maintenance margin barrier breached
    UNKNOWN = "UNKNOWN"


class EngineMode(str, Enum):
    LIVE = "live"
    DRY_RUN = "dry-run"


@dataclass
class TradeSignal:
    """Signal produced by a strategy or sub-strategy."""
    symbol: str
    direction: OrderDirection
    sub_strategy_name: str
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionConfig:
    """Execution parameters for the automated engine."""
    symbol: str = field(default_factory=lambda: os.getenv("KCEX_SYMBOL", "TRUMP_USDT"))
    direction: OrderDirection = OrderDirection.LONG
    mode: EngineMode = EngineMode.DRY_RUN
    leverage: int = 10
    is_isolated: bool = True
    cooldown_seconds: float = 0.0
    tp_ticks: int = 2               # Number of pu (tick size) away from entry (fixed TP)
    dynamic_tp: bool = False        # False = strictly enforce tp_ticks; True = allow dynamic 1..3 pu scaling
    sl_mode: str = "TICKS"          # "ROE", "TICKS", or "PRICE_PCT"
    sl_roe_pct: float = 25.0        # -25.0% ROE (Return on Equity/Margin)
    sl_ticks: Optional[int] = 150   # Number of pu ticks away from entry
    sl_price_pct: Optional[float] = None # Price move percentage away from entry
    # Trade Quantity / Volume settings (Note: Trade Quantity is NOT margin. Margin = Trade Quantity / Leverage)
    volume_mode: str = "MULTIPLIER"       # "MIN", "MULTIPLIER", "CONTRACTS", "MARGIN_PCT", or "FIXED_MARGIN"
    volume_multiplier: float = 50.0       # x times min_volume (e.g. 50.0 = 50x min quantity for TRUMP)
    volume_contracts: Optional[int] = None # Exact number of contracts (e.g. 50)
    margin_pct: Optional[float] = None    # Percentage of available margin to risk/commit (e.g. 10.0 = 10%)
    fixed_margin_usdt: Optional[float] = None # Fixed USDT margin per trade (e.g. 5.0 USDT)
    margin_fallback_pct: float = 25.0     # Percentage of available margin to use if requested volume exceeds balance
    simulated_balance_usdt: Optional[float] = None # Simulated wallet balance for dry-run/testing
    max_trades: int = 0                   # 0 = unlimited
    # Strategy selection
    strategy_mode: str = "STOCH_RSI"      # "STOCH_RSI", "ORDER_BLOCK_DEMAND", "ML_1M", etc.
    timeframe: str = "Min15"              # Strategy candle timeframe (e.g. "Min15", "Min1")
    bi_directional: bool = True           # True for autonomous Long/Short, False for fixed direction
    ema_preset: str = "5/13"              # "5/13", "9/21", "3/8", or "custom"
    ema_fast: int = 5                     # Fast EMA length
    ema_slow: int = 13                    # Slow EMA length
    ema_interval: str = "Min1"            # Candle timeframe e.g. "Min1", "Min5"
    ema_require_closed_candle: bool = True # Confirm cross on closed candle (prevents false whipsaw repainting)
    # Stochastic RSI Configuration
    stoch_preset: str = "FAST_SCALP"      # "FAST_SCALP", "STANDARD", "MICRO_BURST", "custom"
    stoch_rsi_period: int = 9             # RSI calculation period
    stoch_period: int = 9                 # Stochastic period over RSI
    stoch_k_period: int = 3               # %K smoothing period
    stoch_d_period: int = 3               # %D smoothing period
    stoch_oversold: float = 20.0          # Oversold threshold
    stoch_overbought: float = 80.0        # Overbought threshold
    stoch_interval: str = "Min1"          # Candle timeframe
    stoch_zone_filter: bool = True        # Gate crossovers to extreme zones
    stoch_require_closed_candle: bool = True # Confirm cross on closed candle
    # Trade Optimization & Regime Filter Configuration (Toggleable)
    duration_filter_enabled: bool = False       # Master toggle for duration monitoring and exits
    duration_deep_monitor_seconds: float = 60.0 # Time in trade after which high-frequency monitoring engages
    duration_max_hold_seconds: float = 90.0     # Maximum allowable trade duration before time-decay action
    duration_action: str = "CLOSE"              # "CLOSE", "SCRATCH_OR_MARKET", or "TIGHTEN_SL"
    adx_filter_enabled: bool = False            # Gate signals when ADX < threshold (chop suppression)
    adx_period: int = 14                        # ADX smoothing period
    adx_threshold: float = 25.0                 # Minimum ADX required to allow signal execution
    htf_trend_filter_enabled: bool = False      # Higher Timeframe Trend Filter (200 EMA baseline)
    htf_timeframe: str = "15m"                  # HTF candle interval
    htf_ema_period: int = 200                   # HTF EMA period
    hourly_filter_enabled: bool = False         # Blacklist low-liquidity UTC hours
    hourly_blacklist_utc: List[int] = field(default_factory=list) # e.g. [2, 3, 4, 5, 17]
    direction_bias: str = "BOTH"                # "BOTH", "LONG_ONLY", or "SHORT_ONLY"
    us_market_hours_filter_enabled: bool = False # Gate entries strictly to US regular market hours (Mon-Fri 09:30-16:00 ET)
    # Smart Strategy Configuration (Regime-Adaptive Architecture)
    smart_atr_filter_enabled: bool = True       # Suppress entries during sub-ATR compression
    smart_min_atr_ticks: float = 2.5            # Min ATR in ticks required to ensure target feasibility
    smart_chop_ceiling: float = 58.0            # CHOP index above which market is considered dead consolidation
    smart_adx_trend_threshold: float = 26.0     # ADX threshold separating trending from ranging regimes
    smart_use_ema200_filter: bool = False       # Direction lock via 200 EMA (Default OFF per empirical validation)
    smart_ema200_period: int = 200              # 200 EMA period
    smart_climax_filter_enabled: bool = True    # Circuit breaker on volatility spikes
    smart_max_atr_expansion: float = 2.2        # Current ATR / Baseline ATR ceiling
    smart_ema_preset: str = "5/13"              # Momentum sub-strategy preset
    smart_stoch_preset: str = "FAST_SCALP"      # Mean-reversion sub-strategy preset
    smart_interval: str = "Min1"                # Candle interval for Smart Strategy evaluation
    smart_require_closed_candle: bool = True    # Confirm cross on closed candle
    # Order Execution Mode (Zero Slippage Architecture)
    order_type: str = "MARKET"                  # "MARKET" or "LIMIT" (Post-Only Maker)
    limit_order_timeout_seconds: float = 10.0   # Timeout before canceling unfilled maker orders
    cancel_if_unfilled: bool = False            # If False, limit orders rest in orderbook until filled
    # -------------------------------------------------------------------------
    # RESEARCH V2 / V2.1 / V2.2 QUANTITATIVE FEATURE TOGGLES & ENHANCEMENTS
    # -------------------------------------------------------------------------
    # 1. Signal Inversion (Fading Momentum Crosses at Extremes)
    # Researched in Phase V2.1 & V2.2: Fading Stoch RSI overbought/oversold crosses
    # produces +61% to +84% higher Profit Factor in ranging/consolidation regimes.
    invert_signal: bool = False
    dynamic_regime_fading: bool = False  # Auto-fades when ADX < adx_fading_cutoff, direct when trending
    adx_fading_cutoff: float = 28.0

    # 2. Toggleable Order Execution Architecture (Maker vs Taker)
    # "MAKER_HYBRID": Post-only Maker Limit Entry at bid1/ask1 with queue timeout,
    #                 resting limit TP (+0.00 slippage), and Ratchet Market SL.
    # "PURE_MARKET": Legacy standard execution (Taker market entry + polling market exits).
    execution_style: str = "PURE_MARKET"
    maker_queue_timeout_seconds: float = 10.0
    resting_limit_tp: bool = False

    # 3. Phase V2.2 Champion Micro-Excursion Tick Ratchet
    # Dynamic trailing stop protection: locks breakeven at +2.5 ticks and tightens stalled
    # positions at +1.0 tick after 10s to cut stop loss drawdowns.
    ratchet_enabled: bool = False
    ratchet_trigger_ticks: float = 1.0   # Favorable excursion (MFE) required for Tier 1 (+1.0 tick)
    ratchet_stall_seconds: float = 10.0  # Seconds of stall before tightening SL (10.0 seconds)
    ratchet_tighten_ticks: float = 1.0   # Tightened SL distance (-1.0 tick)
    ratchet_breakeven_ticks: float = 2.5 # Favorable excursion required to lock at Breakeven (+2.5 ticks)

    # 4. Realistic Slippage Engine (Synchronized across Local, GitHub Actions & Dry-Run)
    # When enabled, shifts entry fill and market stop exits adversely by slippage_ticks.
    slippage_enabled: bool = False
    slippage_ticks: int = 1              # Integer ticks of adverse friction (e.g. 1t, 2t, 3t)

    # -------------------------------------------------------------------------
    # RESEARCH V3 / V3.1 QUANTITATIVE EXTENSIONS (PURELY TOGGLEABLE)
    # -------------------------------------------------------------------------
    # 5. ATR-Calibrated Volatility Dynamic Targets (Target Dilution Law)
    use_atr_targets: bool = False
    atr_tp_multiplier: float = 2.0
    atr_sl_multiplier: float = 1.0

    # 6. Volume Shock Filter
    volume_filter_enabled: bool = False
    volume_filter_multiplier: float = 1.2

    # 7. Queue Dynamics & Liquidation Checks
    queue_dynamics_enabled: bool = False
    simulate_intra_tick_liquidation: bool = False

    # 8. Microstructure & Volatility Parameters
    microstructure_imbalance_threshold: float = 1.5
    volatility_regime_period: int = 14

    # 9. Smart Money Concepts (Order Block + Demand Strategy) Controls
    partial_tp_enabled: bool = True             # Enable 1:1 Partial TP + Breakeven Lock + 1:2 Runner
    breakeven_buffer_ticks: int = 1             # Buffer in ticks added when locking Stop Loss to Breakeven (+1 to +5 ticks)
    smc_1x_exit_mode: str = "1TO1_TP"           # For 1-contract positions: "1TO1_TP" (Close 1 contract at 1:1) or "1TO2_WITH_BE" (Lock BE at 1:1, run to 1:2)
    risk_reward_ratio: float = 2.0              # Strict 1:2 Risk-Reward ratio from entry to SL
    pivot_len: int = 5                          # 5-bar rolling swing high / low confirmation

    # 10. Tick-Constrained Market Making & Simultaneous Scalping Controls
    min_tick_bps: float = 4.0                   # Minimum tick size in bps of price (large-tick gatekeeper)
    ofi_window: int = 50                        # Order Flow Imbalance calculation window
    max_ofi_threshold: float = 0.40             # Toxic flow threshold to pause quoting
    time_stop_sec: float = 60.0                 # Max hold time before passive scratch
    simultaneous_mode: bool = False             # Simultaneous dual Long+Short entry
    entry_queue_qty: float = 200.0              # Estimated entry queue depth
    tp_queue_qty: float = 200.0                 # Estimated TP queue depth

    # 11. Machine Learning 1M Alpha Engine ATR Targets (Triple Barrier 2:1 Asymmetric Payoff)
    tp_atr_mult: float = 3.0                    # Dynamic Take Profit: 3.0x ATR
    sl_atr_mult: float = 1.5                    # Dynamic Stop Loss: 1.5x ATR

    poll_interval_seconds: float = 0.5
    logs_dir: str = field(default_factory=lambda: os.path.join(ROOT_DIR, "logs"))

    realtime_log_file: str = "engine_realtime.log"
    outcomes_log_file: str = "trade_outcomes.txt"
    outcomes_jsonl_file: str = "trade_outcomes.jsonl"

    def to_config_snapshot(self) -> Dict[str, Any]:
        """Serializes the full configuration to a MongoDB-friendly dictionary."""
        snapshot = {}
        for f in self.__dataclass_fields__:
            val = getattr(self, f)
            if isinstance(val, Enum):
                val = val.value
            snapshot[f] = val
        return snapshot



@dataclass
class TradeOutcome:
    """Complete record of an executed and closed trade."""
    trade_id: int
    symbol: str
    direction: OrderDirection
    sub_strategy_name: str
    mode: EngineMode
    
    # Contract details
    leverage: int
    vol_contracts: int
    contract_size: float
    underlying_quantity: float
    
    # Prices
    entry_price: float
    exit_price: float
    min_profit_tp_price: float
    stop_loss_price: float
    price_unit: float              # pu / tick size
    
    # Timing
    open_time: float
    close_time: float
    duration_seconds: float
    
    # Financial metrics in USDT and INR
    notional_value_usdt: float
    notional_value_inr: float
    margin_used_usdt: float
    margin_used_inr: float
    
    realized_pnl_usdt: float
    realized_pnl_inr: float
    pnl_percentage: float          # Price move %
    roe_percentage: float          # ROE % on margin
    
    base_coin: str = ""
    price_precision: int = 4
    
    fee_open_usdt: float = 0.0
    fee_close_usdt: float = 0.0
    fee_total_usdt: float = 0.0
    fee_total_inr: float = 0.0
    
    inr_rate: float = 94.45
    exit_reason: ExitReason = ExitReason.UNKNOWN
    
    # Live wallet balance before and after this trade
    balance_before_trade_usdt: Optional[float] = None
    balance_before_trade_inr: Optional[float] = None
    balance_after_trade_usdt: Optional[float] = None
    balance_after_trade_inr: Optional[float] = None

    # Server references
    order_id: Optional[str] = None
    close_order_id: Optional[str] = None
    position_id: Optional[int] = None

    # Machine Learning Strategy Telemetry
    ml_confidence: Optional[float] = None
    ml_prob_buy: Optional[float] = None
    ml_prob_sell: Optional[float] = None
    ml_prob_wait: Optional[float] = None
    ml_tp_ticks: Optional[int] = None
    ml_sl_ticks: Optional[int] = None
    ml_atr_14: Optional[float] = None

    # Smart Money Concepts (Order Block + Demand Strategy) Telemetry
    smc_zone_id: Optional[str] = None
    smc_zone_type: Optional[str] = None
    smc_zone_high: Optional[float] = None
    smc_zone_low: Optional[float] = None
    smc_zone_mid: Optional[float] = None
    smc_zone_creation_bar_idx: Optional[int] = None
    smc_zone_creation_ts: Optional[int] = None
    smc_zone_creation_time_utc: Optional[str] = None
    smc_bos_bar_idx: Optional[int] = None
    smc_bos_price: Optional[float] = None
    smc_trigger_candle_time_utc: Optional[str] = None
    smc_trigger_bar_idx: Optional[int] = None
    smc_fvg_size: Optional[float] = None
    smc_target_1to1: Optional[float] = None
    smc_target_1to2: Optional[float] = None
    smc_partial_tp_hit: Optional[bool] = None

    # Tick-Constrained Market Making Telemetry
    mm_ofi_ratio: Optional[float] = None
    mm_tick_bps: Optional[float] = None
    mm_htf_sideways: Optional[bool] = None

    @property
    def is_profit(self) -> bool:
        return self.realized_pnl_usdt > 0.0

    @property
    def is_loss(self) -> bool:
        return self.realized_pnl_usdt < -1e-8

    @property
    def is_scratch(self) -> bool:
        return abs(self.realized_pnl_usdt) <= 1e-8

    @property
    def open_timestamp(self) -> float:
        """Alias for open_time for backward compatibility."""
        return self.open_time

    @property
    def close_timestamp(self) -> float:
        """Alias for close_time for backward compatibility."""
        return self.close_time

    def to_mongo_dict(self) -> Dict[str, Any]:
        """Serialize this trade outcome to a MongoDB-friendly dictionary."""
        return {
            "type": "EXECUTED",
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "base_coin": self.base_coin or self.symbol.split("_")[0],
            "direction": self.direction.value,
            "sub_strategy_name": self.sub_strategy_name,
            "mode": self.mode.value,
            "verified_from_kcex": (self.mode == EngineMode.LIVE),
            # Contract details
            "leverage": self.leverage,
            "vol_contracts": self.vol_contracts,
            "contract_size": self.contract_size,
            "underlying_quantity": self.underlying_quantity,
            # Prices
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "tp_set": self.min_profit_tp_price,
            "sl_set": self.stop_loss_price,
            "price_unit": self.price_unit,
            "price_precision": self.price_precision,
            # Timing (store as proper datetime objects)
            "entry_time": datetime.fromtimestamp(self.open_time, tz=timezone.utc),
            "exit_time": datetime.fromtimestamp(self.close_time, tz=timezone.utc),
            "duration_seconds": self.duration_seconds,
            # Financial metrics
            "notional_value_usdt": self.notional_value_usdt,
            "notional_value_inr": self.notional_value_inr,
            "margin_used_usdt": self.margin_used_usdt,
            "margin_used_inr": self.margin_used_inr,
            "realized_pnl_usdt": self.realized_pnl_usdt,
            "realized_pnl_inr": self.realized_pnl_inr,
            "pnl_percentage": self.pnl_percentage,
            "roe_percentage": self.roe_percentage,
            "yield_pct": self.roe_percentage,
            # Fees
            "fee_open_usdt": self.fee_open_usdt,
            "fee_close_usdt": self.fee_close_usdt,
            "fee_total_usdt": self.fee_total_usdt,
            "fee_total_inr": self.fee_total_inr,
            # Exit
            "exit_reason": self.exit_reason.value,
            # Balance
            "balance_before_trade_usdt": self.balance_before_trade_usdt,
            "balance_before_trade_inr": self.balance_before_trade_inr,
            "balance_after_trade_usdt": self.balance_after_trade_usdt,
            "balance_after_trade_inr": self.balance_after_trade_inr,
            # Server references
            "order_id": self.order_id,
            "close_order_id": self.close_order_id,
            "position_id": self.position_id,
            # Exchange rate
            "inr_rate": self.inr_rate,
            # Machine Learning Telemetry
            "ml_confidence": self.ml_confidence,
            "ml_prob_buy": self.ml_prob_buy,
            "ml_prob_sell": self.ml_prob_sell,
            "ml_prob_wait": self.ml_prob_wait,
            "ml_tp_ticks": self.ml_tp_ticks,
            "ml_sl_ticks": self.ml_sl_ticks,
            "ml_atr_14": self.ml_atr_14,
            # Smart Money Concepts (Order Block + Demand Strategy) Telemetry
            "smc_zone_id": self.smc_zone_id,
            "smc_zone_type": self.smc_zone_type,
            "smc_zone_high": self.smc_zone_high,
            "smc_zone_low": self.smc_zone_low,
            "smc_zone_mid": self.smc_zone_mid,
            "smc_zone_creation_bar_idx": self.smc_zone_creation_bar_idx,
            "smc_zone_creation_ts": self.smc_zone_creation_ts,
            "smc_zone_creation_time_utc": self.smc_zone_creation_time_utc,
            "smc_bos_bar_idx": self.smc_bos_bar_idx,
            "smc_bos_price": self.smc_bos_price,
            "smc_trigger_candle_time_utc": self.smc_trigger_candle_time_utc,
            "smc_trigger_bar_idx": self.smc_trigger_bar_idx,
            "smc_fvg_size": self.smc_fvg_size,
            "smc_target_1to1": self.smc_target_1to1,
            "smc_target_1to2": self.smc_target_1to2,
            "smc_partial_tp_hit": self.smc_partial_tp_hit,
            # Tick-Constrained Market Making Telemetry
            "mm_ofi_ratio": self.mm_ofi_ratio,
            "mm_tick_bps": self.mm_tick_bps,
            "mm_htf_sideways": self.mm_htf_sideways,
        }

    def to_dict(self) -> Dict[str, Any]:
        """Serialize this trade outcome to a standard Python dictionary."""
        return self.to_mongo_dict()



@dataclass
class CumulativeStats:
    """Cumulative performance statistics across multiple trades."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    scratch_trades: int = 0
    total_pnl_usdt: float = 0.0
    total_pnl_inr: float = 0.0
    total_fees_usdt: float = 0.0
    total_fees_inr: float = 0.0
    best_trade_usdt: float = 0.0
    worst_trade_usdt: float = 0.0

    @property
    def win_rate_pct(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return (self.winning_trades / self.total_trades) * 100.0

    def update(self, outcome: TradeOutcome) -> None:
        self.total_trades += 1
        self.total_pnl_usdt += outcome.realized_pnl_usdt
        self.total_pnl_inr += outcome.realized_pnl_inr
        self.total_fees_usdt += outcome.fee_total_usdt
        self.total_fees_inr += outcome.fee_total_inr

        if outcome.is_scratch:
            self.scratch_trades += 1
        elif outcome.is_profit:
            self.winning_trades += 1
        else:
            self.losing_trades += 1

        if outcome.realized_pnl_usdt > self.best_trade_usdt:
            self.best_trade_usdt = outcome.realized_pnl_usdt
        if outcome.realized_pnl_usdt < self.worst_trade_usdt:
            self.worst_trade_usdt = outcome.realized_pnl_usdt

