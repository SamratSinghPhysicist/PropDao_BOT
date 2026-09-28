"""
KCEX "Masterplan" Strategy Coordinator
======================================
Coordinates modular trading strategies (EMA Crossover, Stochastic RSI) with:
- Pair fee & contract validation (zero-fee and standard fee tiers)
- Guaranteed Min-Profit TP (Entry + pu for Long / Entry - pu for Short)
- Multi-mode Stop Loss (ROE %, ticks, price %) with liquidation safety guard
- Immediate Profit Closing evaluation
"""

import os
import sys
import logging
from typing import Optional, Dict, Any

# Ensure root is in sys.path for strategies import
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from kcex.market import KCEXMarket, ContractInfo
from kcex.engine.models import (
    OrderDirection,
    TradeSignal,
    TradeOutcome,
    ExitReason,
    ExecutionConfig
)
from strategies.base import (
    BaseStrategy,
    BaseSubStrategy
)
from strategies.ema_crossover import (
    EMACrossoverStrategy,
    EMACrossoverSubStrategy,
    EMA_PRESETS,
    compute_ema_series
)
from strategies.stoch_rsi import (
    StochasticRSIStrategy,
    StochasticRSISubStrategy,
    STOCH_RSI_PRESETS,
    compute_rsi_series,
    compute_stoch_rsi
)
from strategies.smart_strategy import (
    SmartStrategy,
    SmartSubStrategy,
    MarketRegime,
    compute_chop_series
)
try:
    from strategies.ml_strategy import (
        MLStrategy,
        MLSubStrategy
    )
except ImportError:
    MLStrategy = None
    MLSubStrategy = None
from strategies.order_block_demand import (
    OrderBlockDemandStrategy,
    OrderBlockDemandSubStrategy
)
from strategies.tick_constrained_mm import (
    TickConstrainedMMStrategy,
    TickConstrainedSubStrategy,
    TickConstrainedConfig
)

logger = logging.getLogger("KCEXStrategy")


class MasterplanStrategy:
    """
    Masterplan Trading Strategy Coordinator.
    
    Responsibilities:
    1. Validates trading pair fee and contract configuration.
    2. Coordinates active strategy to generate entry signals.
    3. Calculates exact Min-Profit Take Profit price:
       - Long: Entry Price + (tp_ticks * pu)
       - Short: Entry Price - (tp_ticks * pu)
       where pu is the contract's tick size (price_unit).
    4. Calculates Stop Loss (by ROE, ticks, or price %) with liquidation buffer clamping.
    5. Evaluates immediate-profit conditions: if current market price is already
       better than or equal to min-profit, triggers instant market close.
    """

    def __init__(
        self,
        market: KCEXMarket,
        config: Optional[ExecutionConfig] = None,
        sub_strategy: Optional[BaseStrategy] = None
    ):
        self.market = market
        self.config = config or ExecutionConfig()
        if sub_strategy is not None:
            self.sub_strategy = sub_strategy
        else:
            strat_mode = getattr(self.config, "strategy_mode", "STOCH_RSI") or "STOCH_RSI"
            strat_upper = str(strat_mode).upper()
            pref_dir = None if getattr(self.config, "bi_directional", True) else self.config.direction

            if strat_upper in ("SMART", "SMART_STRATEGY"):
                self.sub_strategy = SmartStrategy(
                    market=self.market,
                    symbol=self.config.symbol,
                    interval=getattr(self.config, "smart_interval", "Min1"),
                    preferred_direction=pref_dir,
                    cooldown_seconds=self.config.cooldown_seconds,
                    require_closed_candle=getattr(self.config, "smart_require_closed_candle", True),
                    atr_filter_enabled=getattr(self.config, "smart_atr_filter_enabled", True),
                    min_atr_ticks=getattr(self.config, "smart_min_atr_ticks", 2.5),
                    chop_ceiling=getattr(self.config, "smart_chop_ceiling", 58.0),
                    adx_trend_threshold=getattr(self.config, "smart_adx_trend_threshold", 26.0),
                    use_ema200_filter=getattr(self.config, "smart_use_ema200_filter", False),
                    climax_filter_enabled=getattr(self.config, "smart_climax_filter_enabled", True),
                    max_atr_expansion=getattr(self.config, "smart_max_atr_expansion", 2.2),
                    ema_preset=getattr(self.config, "smart_ema_preset", getattr(self.config, "ema_preset", "5/13")),
                    stoch_preset=getattr(self.config, "smart_stoch_preset", getattr(self.config, "stoch_preset", "FAST_SCALP")),
                    invert_signal=getattr(self.config, "invert_signal", False),
                    dynamic_regime_fading=getattr(self.config, "dynamic_regime_fading", False),
                    adx_fading_cutoff=getattr(self.config, "adx_fading_cutoff", 28.0)
                )
            elif strat_upper in ("EMA", "EMA_CROSSOVER", "CROSSOVER"):
                self.sub_strategy = EMACrossoverStrategy(
                    market=self.market,
                    symbol=self.config.symbol,
                    fast_period=getattr(self.config, "ema_fast", 5),
                    slow_period=getattr(self.config, "ema_slow", 13),
                    ema_preset=getattr(self.config, "ema_preset", "5/13"),
                    interval=getattr(self.config, "ema_interval", "Min1"),
                    preferred_direction=pref_dir,
                    cooldown_seconds=self.config.cooldown_seconds,
                    require_closed_candle=getattr(self.config, "ema_require_closed_candle", True)
                )
            elif strat_upper in ("ML", "ML_MODEL", "ML_1M", "ML_1M_MODEL"):
                self.sub_strategy = MLStrategy(
                    market=self.market,
                    symbol=self.config.symbol,
                    timeframe=getattr(self.config, "timeframe", "1m"),
                    cooldown_seconds=self.config.cooldown_seconds,
                    confidence_threshold=getattr(self.config, "confidence_threshold", None),
                    confidence_threshold_sell=getattr(self.config, "confidence_threshold_sell", None),
                    edge_threshold=getattr(self.config, "edge_threshold", None),
                    preferred_direction=pref_dir,
                    require_closed_candle=getattr(self.config, "ml_require_closed_candle", getattr(self.config, "require_closed_candle", True))
                )
            elif strat_upper in ("ORDER_BLOCK_DEMAND", "ORDER_BOOK_DEMAND", "ORDER_BLOCK", "DEMAND_BLOCK", "SMC"):
                self.sub_strategy = OrderBlockDemandStrategy(
                    market=self.market,
                    symbol=self.config.symbol,
                    interval=getattr(self.config, "timeframe", getattr(self.config, "ema_interval", "Min1")),
                    preferred_direction=pref_dir,
                    cooldown_seconds=self.config.cooldown_seconds,
                    require_closed_candle=getattr(self.config, "smart_require_closed_candle", True),
                    risk_reward_ratio=getattr(self.config, "risk_reward_ratio", 2.0),
                    pivot_len=getattr(self.config, "pivot_len", 5),
                    buffer_ticks=getattr(self.config, "buffer_ticks", 1),
                    min_sl_ticks=getattr(self.config, "min_sl_ticks", 3),
                    max_sl_ticks=getattr(self.config, "max_sl_ticks", 35)
                )
            elif strat_upper in ("TICK_CONSTRAINED_MM", "TICK_CONSTRAINED", "MICRO_MARKET_MAKER", "MM", "SIMULTANEOUS_MM", "SIMULTANEOUS"):
                mm_cfg = TickConstrainedConfig(
                    tick_size=getattr(self.config, "tick_size", 0.001),
                    min_tick_bps=getattr(self.config, "min_tick_bps", 4.0),
                    tp_ticks=getattr(self.config, "tp_ticks", 1),
                    sl_ticks=getattr(self.config, "sl_ticks", 3),
                    entry_queue_qty=getattr(self.config, "entry_queue_qty", 200.0),
                    tp_queue_qty=getattr(self.config, "tp_queue_qty", 200.0),
                    ofi_window=getattr(self.config, "ofi_window", 50),
                    max_ofi_threshold=getattr(self.config, "max_ofi_threshold", 0.40),
                    time_stop_sec=getattr(self.config, "time_stop_sec", 60.0),
                    use_htf_filter=getattr(self.config, "htf_trend_filter_enabled", True),
                    htf_timeframe=getattr(self.config, "htf_timeframe", "Min15"),
                    bb_period=getattr(self.config, "bb_period", 20),
                    bb_std=getattr(self.config, "bb_std", 2.0),
                    bbw_percentile_cutoff=getattr(self.config, "bbw_percentile_cutoff", 40.0),
                    adx_period=getattr(self.config, "adx_period", 14),
                    max_adx_sideways=getattr(self.config, "max_adx_sideways", 22.0),
                    cooldown_seconds=self.config.cooldown_seconds,
                    simultaneous_mode=getattr(self.config, "simultaneous_mode", False) or ("SIMULTANEOUS" in strat_upper)
                )
                self.sub_strategy = TickConstrainedMMStrategy(
                    market=self.market,
                    symbol=self.config.symbol,
                    config=mm_cfg,
                    preferred_direction=pref_dir
                )
            else:
                # Default to Stochastic RSI
                self.sub_strategy = StochasticRSIStrategy(
                    market=self.market,
                    symbol=self.config.symbol,
                    rsi_period=getattr(self.config, "stoch_rsi_period", 9),
                    stoch_period=getattr(self.config, "stoch_period", 9),
                    k_period=getattr(self.config, "stoch_k_period", 3),
                    d_period=getattr(self.config, "stoch_d_period", 3),
                    oversold=getattr(self.config, "stoch_oversold", 20.0),
                    overbought=getattr(self.config, "stoch_overbought", 80.0),
                    stoch_preset=getattr(self.config, "stoch_preset", "FAST_SCALP"),
                    interval=getattr(self.config, "stoch_interval", "Min1"),
                    preferred_direction=pref_dir,
                    cooldown_seconds=self.config.cooldown_seconds,
                    zone_filter=getattr(self.config, "stoch_zone_filter", True),
                    require_closed_candle=getattr(self.config, "stoch_require_closed_candle", True),
                    invert_signal=getattr(self.config, "invert_signal", False),
                    dynamic_regime_fading=getattr(self.config, "dynamic_regime_fading", False),
                    adx_fading_cutoff=getattr(self.config, "adx_fading_cutoff", 28.0)
                )

        self.name = "Masterplan"

    def validate_zero_fee_pair(self, symbol: str) -> Dict[str, Any]:
        """
        Checks whether the selected symbol offers zero maker and taker fees,
        or operates under standard exchange fee tiers.
        """
        symbol_upper = symbol.upper()
        contract = self.market.get_contract_detail(symbol_upper)

        maker_fee = contract.maker_fee_rate
        taker_fee = contract.taker_fee_rate
        tier_data = {}

        # If client is authenticated, check account tier fee endpoint
        if self.market.client.config.is_authenticated:
            try:
                tier = self.market.get_account_tier_fees(symbol_upper)
                maker_fee = tier.get("makerFee", maker_fee)
                taker_fee = tier.get("takerFee", taker_fee)
                tier_data = tier
            except Exception as e:
                logger.warning("Could not query tiered_fee_rate for %s: %s", symbol_upper, e)

        is_zero_fee = (maker_fee == 0.0 and taker_fee == 0.0)
        result = {
            "symbol": symbol_upper,
            "maker_fee": maker_fee,
            "taker_fee": taker_fee,
            "is_zero_fee": is_zero_fee,
            "contract_size": contract.contract_size,
            "price_unit": contract.price_unit,
            "min_volume": contract.min_volume,
            "max_leverage": contract.max_leverage,
            "tier_data": tier_data
        }

        if not is_zero_fee:
            logger.warning(
                "Notice: Pair %s reported non-zero fees: maker=%s, taker=%s",
                symbol_upper, maker_fee, taker_fee
            )
        else:
            logger.info("Verified Zero-Fee Pair: %s (Maker: 0%%, Taker: 0%%)", symbol_upper)

        return result

    def calculate_min_profit_tp(
        self,
        direction: OrderDirection,
        entry_price: float,
        price_unit: float,
        tp_ticks: int = 1,
        precision: Optional[int] = None,
        atr_value: Optional[float] = None
    ) -> float:
        """
        Calculates the Guaranteed Min-Profit TP Price.
        Supports fixed tick targets or ATR-calibrated volatility targets (Target Dilution Law).
        For Long:  TP = Entry Price + (tp_ticks * pu)
        For Short: TP = Entry Price - (tp_ticks * pu)
        """
        if precision is None:
            try:
                contract = self.market.get_contract_detail(self.config.symbol)
                precision = contract.price_precision
            except Exception:
                precision = 4

        # Check if ATR dynamic targets are toggled on
        if atr_value is not None and getattr(self.config, "use_atr_targets", False) and price_unit > 0:
            atr_mult = float(getattr(self.config, "atr_tp_multiplier", 2.0) or 2.0)
            effective_tp_ticks = max(1, int(round((atr_value * atr_mult) / price_unit)))
        else:
            effective_tp_ticks = tp_ticks

        tick_offset = effective_tp_ticks * price_unit
        if direction == OrderDirection.LONG:
            tp_price = entry_price + tick_offset
        else:
            tp_price = entry_price - tick_offset
        return round(tp_price, precision)

    def calculate_stop_loss(
        self,
        direction: OrderDirection,
        entry_price: float,
        leverage: int,
        sl_roe_pct: Optional[float] = None,
        sl_ticks: Optional[int] = None,
        sl_price_pct: Optional[float] = None,
        price_unit: Optional[float] = None,
        precision: Optional[int] = None,
        atr_value: Optional[float] = None
    ) -> float:
        """
        Calculates Stop Loss price supporting multiple modes:
        1. atr_value (when use_atr_targets is enabled): Volatility-normalized SL ticks = (ATR * mult) / pu.
        2. sl_ticks: Stop loss offset by integer price units (e.g. 10 ticks = 10 * pu).
        3. sl_price_pct: Stop loss by pure price change % (e.g. 0.5% = 0.005 * entry_price).
        4. sl_roe_pct (default): Stop loss by ROE % (e.g. 10.0% ROE -> price move = ROE / (100 * leverage)).
        Includes liquidation guard to ensure SL is never placed beyond liquidation price.
        """
        mmr = 0.01
        try:
            contract = self.market.get_contract_detail(self.config.symbol)
            if contract:
                if price_unit is None:
                    price_unit = contract.price_unit
                if precision is None:
                    precision = contract.price_precision
                if contract.maintenance_margin_ratio > 0:
                    mmr = contract.maintenance_margin_ratio
        except Exception:
            pass

        if price_unit is None:
            price_unit = 0.001
        if precision is None:
            precision = 4

        # 1. Determine price offset
        if atr_value is not None and getattr(self.config, "use_atr_targets", False) and price_unit > 0:
            atr_mult = float(getattr(self.config, "atr_sl_multiplier", 1.0) or 1.0)
            effective_sl_ticks = max(1, int(round((atr_value * atr_mult) / price_unit)))
            price_offset = effective_sl_ticks * price_unit
        elif sl_ticks is not None and sl_ticks > 0:
            price_offset = sl_ticks * price_unit
        elif sl_price_pct is not None and sl_price_pct > 0:
            price_offset = entry_price * (sl_price_pct / 100.0)
        else:
            effective_roe = sl_roe_pct if sl_roe_pct is not None else 10.0
            price_drop_fraction = (effective_roe / 100.0) / float(max(1, leverage))
            price_offset = entry_price * price_drop_fraction

        if direction == OrderDirection.LONG:
            sl_price = entry_price - price_offset
            # Liquidation price for Long: Entry * (1 - 1/leverage + mmr)
            approx_liq = entry_price * (1.0 - (1.0 / float(max(1, leverage))) + mmr)
            if sl_price <= approx_liq:
                # Clamp safely within 85% of liquidation distance
                clamped_sl = entry_price - (entry_price - approx_liq) * 0.85
                clamped_sl = min(clamped_sl, entry_price - price_unit)
                clamped_ticks = int(round((entry_price - clamped_sl) / price_unit))
                max_liq_ticks = (entry_price - approx_liq) / price_unit
                logger.warning(
                    f"Liquidation Guard: Requested SL {sl_price:.{precision}f} was past liq price {approx_liq:.{precision}f} "
                    f"(Total liq buffer is only ~{max_liq_ticks:.1f} ticks at {leverage}x). Clamped safely to {clamped_sl:.{precision}f} (~{clamped_ticks} ticks)."
                )
                sl_price = clamped_sl
        else:
            sl_price = entry_price + price_offset
            # Liquidation price for Short: Entry * (1 + 1/leverage - mmr)
            approx_liq = entry_price * (1.0 + (1.0 / float(max(1, leverage))) - mmr)
            if sl_price >= approx_liq:
                # Clamp safely within 85% of liquidation distance
                clamped_sl = entry_price + (approx_liq - entry_price) * 0.85
                clamped_sl = max(clamped_sl, entry_price + price_unit)
                clamped_ticks = int(round((clamped_sl - entry_price) / price_unit))
                max_liq_ticks = (approx_liq - entry_price) / price_unit
                logger.warning(
                    f"Liquidation Guard: Requested SL {sl_price:.{precision}f} was past liq price {approx_liq:.{precision}f} "
                    f"(Total liq buffer is only ~{max_liq_ticks:.1f} ticks at {leverage}x). Clamped safely to {clamped_sl:.{precision}f} (~{clamped_ticks} ticks)."
                )
                sl_price = clamped_sl

        return round(sl_price, precision)

    def is_better_than_min_profit(
        self,
        direction: OrderDirection,
        current_price: float,
        min_profit_tp_price: float,
        entry_price: Optional[float] = None
    ) -> bool:
        """
        Checks if current market/executable price is already at or better than minimum profit TP:
        - For Long:  current_price >= min_profit_tp_price (and strictly > entry_price if given)
        - For Short: current_price <= min_profit_tp_price (and strictly < entry_price if given)
        """
        if direction == OrderDirection.LONG:
            is_hit = current_price >= min_profit_tp_price
            if entry_price is not None:
                is_hit = is_hit and (current_price > entry_price)
            return is_hit
        else:
            is_hit = current_price <= min_profit_tp_price
            if entry_price is not None:
                is_hit = is_hit and (current_price < entry_price)
            return is_hit

    def get_signal(self) -> Optional[TradeSignal]:
        """Polls active sub-strategy for next entry signal with exception protection."""
        try:
            return self.sub_strategy.generate_signal(self.config.symbol)
        except Exception as e:
            logger.warning(
                "Exception during signal generation for %s (%s): %s",
                self.config.symbol, getattr(self.sub_strategy, "name", "sub_strategy"), e,
                exc_info=True
            )
            return None

    def on_trade_completed(self, outcome: TradeOutcome) -> None:
        """Notifies active sub-strategy when trade completes."""
        try:
            self.sub_strategy.on_trade_completed(outcome)
        except Exception as e:
            logger.warning("Error in on_trade_completed callback: %s", e)

    def on_trade_rejected(self) -> None:
        """Notifies active sub-strategy when candidate trade signal is suppressed by a regime filter."""
        try:
            if hasattr(self.sub_strategy, "on_trade_rejected"):
                self.sub_strategy.on_trade_rejected()
            elif hasattr(self.sub_strategy, "trade_in_progress"):
                self.sub_strategy.trade_in_progress = False
        except Exception as e:
            logger.warning("Error in on_trade_rejected callback: %s", e)

    def start(self) -> None:
        """Starts sub-strategy resources (e.g. WebSocket feeds)."""
        try:
            self.sub_strategy.start()
        except Exception as e:
            logger.warning("Error starting sub-strategy resources: %s", e)

    def stop(self) -> None:
        """Stops sub-strategy resources."""
        try:
            self.sub_strategy.stop()
        except Exception as e:
            logger.warning("Error stopping sub-strategy resources: %s", e)

    def get_diagnostics(self) -> Dict[str, Any]:
        """Returns live sub-strategy diagnostics with exception protection."""
        try:
            return self.sub_strategy.get_diagnostics()
        except Exception as e:
            logger.debug("Exception fetching strategy diagnostics: %s", e)
            return {}


__all__ = [
    "BaseStrategy",
    "BaseSubStrategy",
    "MLStrategy",
    "MLSubStrategy",
    "EMACrossoverStrategy",
    "EMACrossoverSubStrategy",
    "StochasticRSIStrategy",
    "StochasticRSISubStrategy",
    "SmartStrategy",
    "SmartSubStrategy",
    "MarketRegime",
    "EMA_PRESETS",
    "STOCH_RSI_PRESETS",
    "compute_ema_series",
    "compute_rsi_series",
    "compute_stoch_rsi",
    "compute_chop_series",
    "MasterplanStrategy",
]
