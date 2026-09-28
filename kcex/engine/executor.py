"""
KCEX Automated Trade Execution Engine
=====================================
The core orchestrator that continuously runs the Masterplan strategy:
1. Connects to KCEX and validates pair specifications and fee status.
2. Generates signals via the active sub-strategy.
3. Sizes orders based on user configuration (multiplier, contracts, or minimum volume).
4. Submits market order with attached TP/SL in a single request.
5. Immediately reconciles fill:
   - If market price is already at or better than Min-Profit TP (entry + pu), closes immediately!
   - Otherwise, verifies/adjusts server-side TP to exact entry + pu and SL to requested risk limit.
6. Actively monitors the position until closed.
7. Logs detailed execution and outcome metrics in both USDT and INR.
8. Enforces configured cooldown before the next trade cycle.
9. Supports DRY-RUN (safe simulation) and LIVE execution modes.
"""

import time
import math
import signal
import sys
import os
import threading
from typing import Optional, Dict, Any
from datetime import datetime, timezone

from kcex.config import KCEXConfig
from kcex.client import KCEXClient, KCEXAPIError
from kcex.market import KCEXMarket, ContractInfo
from kcex.risk import KCEXRiskCalculator
from kcex.trade import KCEXTrader
from kcex.engine.models import (
    OrderDirection,
    ExitReason,
    EngineMode,
    TradeSignal,
    TradeOutcome,
    ExecutionConfig
)
from kcex.engine.logger import DualCurrencyLogger, TradeOutcomeLogger
from kcex.engine.mongo_logger import MongoTradeLogger
from kcex.engine.strategy import (
    MasterplanStrategy,
    EMACrossoverStrategy,
    StochasticRSIStrategy,
    SmartStrategy,
    MLStrategy
)
from strategies.tick_constrained_mm import (
    TickConstrainedMMStrategy,
    TickConstrainedConfig
)
from strategies.order_block_demand import OrderBlockDemandStrategy
from strategies.filters import FilterPipeline


class TradeExecutionEngine:
    """
    Automated execution engine coordinating strategy, order execution,
    risk management, and dual-currency trade journals.
    """

    def __init__(
        self,
        config: Optional[ExecutionConfig] = None,
        client: Optional[KCEXClient] = None,
        market: Optional[KCEXMarket] = None,
        trader: Optional[KCEXTrader] = None,
        risk: Optional[KCEXRiskCalculator] = None,
        strategy: Optional[MasterplanStrategy] = None,
        mongo_logger: Optional[MongoTradeLogger] = None,
        runtime_limit_seconds: float = 0
    ):
        self.config = config or ExecutionConfig()
        self.client = client or KCEXClient()
        self.market = market or KCEXMarket(self.client)
        self.risk = risk or KCEXRiskCalculator(self.market, self.client)
        self.trader = trader or KCEXTrader(self.client, self.market, self.risk)

        # Loggers
        inr_rate = self.market.get_inr_rate()
        self.logger = DualCurrencyLogger(
            log_file=f"{self.config.logs_dir}/{self.config.realtime_log_file}",
            inr_rate=inr_rate
        )
        self.outcome_logger = TradeOutcomeLogger(
            txt_file=f"{self.config.logs_dir}/{self.config.outcomes_log_file}",
            jsonl_file=f"{self.config.logs_dir}/{self.config.outcomes_jsonl_file}"
        )

        # MongoDB Trade Logger (optional, gracefully degrades if unavailable)
        self.mongo_logger = mongo_logger

        # Runtime limit for GitHub Actions (0 = unlimited)
        self.runtime_limit_seconds = runtime_limit_seconds
        self._runtime_start: float = 0.0
        self._in_active_trade: bool = False
        self._cancelled_order_count: int = 0

        # Strategy Selection
        if strategy is not None:
            self.strategy = strategy
        else:
            strat_mode = getattr(self.config, "strategy_mode", "STOCH_RSI") or "STOCH_RSI"
            strat_upper = str(strat_mode).upper()
            pref_dir = None if getattr(self.config, "bi_directional", True) else self.config.direction
            if strat_upper in ("EMA", "EMA_CROSSOVER", "CROSSOVER"):
                sub_strat = EMACrossoverStrategy(
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
            elif strat_upper in ("ML", "ML_1M", "ML_MODEL", "ML_1M_MODEL"):
                sub_strat = MLStrategy(
                    market=self.market,
                    symbol=self.config.symbol,
                    timeframe="1m",
                    cooldown_seconds=self.config.cooldown_seconds,
                    preferred_direction=pref_dir
                )
            elif strat_upper in ("SMART", "SMART_STRATEGY"):
                sub_strat = SmartStrategy(
                    market=self.market,
                    symbol=self.config.symbol,
                    preferred_direction=pref_dir,
                    cooldown_seconds=self.config.cooldown_seconds
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
                sub_strat = TickConstrainedMMStrategy(
                    market=self.market,
                    symbol=self.config.symbol,
                    config=mm_cfg,
                    preferred_direction=pref_dir
                )
            elif strat_upper in ("ORDER_BLOCK_DEMAND", "ORDER_BOOK_DEMAND", "ORDER_BLOCK", "DEMAND_BLOCK", "SMC"):
                sub_strat = OrderBlockDemandStrategy(
                    market=self.market,
                    symbol=self.config.symbol,
                    interval=getattr(self.config, "timeframe", getattr(self.config, "ema_interval", "Min15")),
                    preferred_direction=pref_dir,
                    cooldown_seconds=self.config.cooldown_seconds,
                    require_closed_candle=getattr(self.config, "smart_require_closed_candle", True),
                    risk_reward_ratio=getattr(self.config, "risk_reward_ratio", 2.0),
                    pivot_len=getattr(self.config, "pivot_len", 5),
                    buffer_ticks=getattr(self.config, "buffer_ticks", 1),
                    min_sl_ticks=getattr(self.config, "min_sl_ticks", 3),
                    max_sl_ticks=getattr(self.config, "max_sl_ticks", 35)
                )
            else:
                sub_strat = StochasticRSIStrategy(
                    market=self.market,
                    symbol=self.config.symbol,
                    stoch_preset=getattr(self.config, "stoch_preset", "FAST_SCALP"),
                    rsi_period=getattr(self.config, "stoch_rsi_period", 9),
                    stoch_period=getattr(self.config, "stoch_period", 9),
                    k_period=getattr(self.config, "stoch_k_period", 3),
                    d_period=getattr(self.config, "stoch_d_period", 3),
                    oversold=getattr(self.config, "stoch_oversold", 20.0),
                    overbought=getattr(self.config, "stoch_overbought", 80.0),
                    interval=getattr(self.config, "stoch_interval", "Min1"),
                    zone_filter=getattr(self.config, "stoch_zone_filter", True),
                    preferred_direction=pref_dir,
                    cooldown_seconds=self.config.cooldown_seconds,
                    require_closed_candle=getattr(self.config, "stoch_require_closed_candle", True)
                )
            self.strategy = MasterplanStrategy(
                market=self.market,
                config=self.config,
                sub_strategy=sub_strat
            )

        self.filter_pipeline = FilterPipeline.from_config(self.config)

        self.running: bool = False
        self.trade_counter: int = 0
        self._current_position_id: Optional[int] = None
        self._shutdown_requested: bool = False
        self.simulated_balance_usdt: Optional[float] = None

    def stop(self) -> None:
        """Requests graceful engine stop."""
        self._shutdown_requested = True
        self.running = False
        try:
            self.strategy.stop()
        except Exception:
            pass
        self.logger.info("Graceful shutdown requested...")


    # =========================================================================
    # PRE-FLIGHT VERIFICATIONS
    # =========================================================================

    def pre_flight_checks(self) -> ContractInfo:
        """
        Runs initial validation checks:
        1. Connectivity ping
        2. Contract detail & zero-fee status
        3. Wallet balance & live USD/INR exchange rate
        4. Open positions sanity check
        """
        self.logger.section("PRE-FLIGHT CHECKS & SYSTEM INITIALIZATION")

        # 1. Connectivity
        self.logger.info("Testing connectivity to KCEX API...")
        if not self.market.ping():
            self.logger.warning("Ping returned non-standard status, verifying ticker connectivity...")

        # 2. INR Rate
        inr_rate = self.market.get_inr_rate()
        self.logger.set_inr_rate(inr_rate)
        self.logger.info(f"Live USD/INR Exchange Rate: INR {inr_rate:.2f} per USD")

        # 3. Contract & Zero-Fee Verification
        symbol = self.config.symbol.upper()
        contract = self.market.get_contract_detail(symbol)
        fee_info = self.strategy.validate_zero_fee_pair(symbol)

        self.logger.info(
            f"Trading Pair: {symbol} | Tick Size (pu): {contract.price_unit} | "
            f"Contract Size (cs): {contract.contract_size} | Min Volume: {contract.min_volume} contract(s)"
        )
        self.logger.info(
            f"Effective Fees: Maker {fee_info['maker_fee']*100:.2f}% / Taker {fee_info['taker_fee']*100:.2f}% "
            f"({'ZERO FEES CONFIRMED' if fee_info['is_zero_fee'] else 'NON-ZERO FEES WARNING'})"
        )

        # 4. Balances (if live or authenticated)
        if self.config.mode == EngineMode.LIVE:
            if not self.client.config.is_authenticated:
                raise ValueError("LIVE mode requires KCEX_AUTH_TOKEN configured in .env.")

            try:
                balances = self.trader.get_usdt_balance()
                avail_usdt = balances.get("available_usdt", 0.0)
                avail_inr = balances.get("available_inr", 0.0)
                self.logger.info(
                    f"Futures Wallet Available: {avail_usdt} USDT (INR {avail_inr:.2f})"
                )

                # Check if an existing open position exists
                open_positions = self.trader.get_open_positions(symbol)
                if open_positions:
                    for pos in open_positions:
                        hold_vol = float(pos.get("holdVol", 0) or pos.get("vol", 0))
                        if hold_vol > 0:
                            self.logger.warning(
                                f"Warning: Found existing open position on {symbol}: {hold_vol} contracts. "
                                f"Position ID: {pos.get('positionId')}"
                            )
            except KCEXAPIError as e:
                if e.code == 401 or "authority" in str(e).lower() or "unauthorized" in str(e).lower():
                    self.logger.error("=" * 78)
                    self.logger.error("  [AUTHENTICATION ERROR] KCEX API Rejected Session Token (HTTP 401)")
                    self.logger.error("=" * 78)
                    self.logger.error("  KCEX returned: [KCEX Error 401] No authority!")
                    self.logger.error("  Your KCEX_AUTH_TOKEN is invalid, expired, or rejected by KCEX.\n")
                    self.logger.error("  HOW TO UPDATE YOUR TOKEN ON RAILWAY:")
                    self.logger.error("  1. Log into your KCEX account in Chrome / Edge (https://www.kcex.com).")
                    self.logger.error("  2. Press F12 -> Network tab -> click any futures/private request.")
                    self.logger.error("  3. Copy the entire 'Authorization' header value.")
                    self.logger.error("  4. Go to Railway -> Your Service -> Variables.")
                    self.logger.error("  5. Update KCEX_AUTH_TOKEN with the fresh token and Redeploy.")
                    self.logger.error("=" * 78)
                    time.sleep(30)
                    sys.exit(1)
                raise

        self.logger.info(f"Engine Mode: {self.config.mode.value.upper()}")
        self.logger.info(f"Sub-strategy: {self.strategy.sub_strategy.name}")
        vol_mode = (getattr(self.config, "volume_mode", "MULTIPLIER") or "MULTIPLIER").upper()
        if vol_mode == "CONTRACTS" and getattr(self.config, "volume_contracts", None):
            vol_summary = f"{self.config.volume_contracts} contract(s)"
        elif vol_mode == "MARGIN_PCT" or getattr(self.config, "margin_pct", None) is not None:
            pct_val = float(getattr(self.config, "margin_pct", 10.0) or 10.0)
            vol_summary = f"{pct_val:g}% available margin ({pct_val:g}% Margin x {self.config.leverage}x Lev = Position Size)"
        elif vol_mode == "MULTIPLIER" and getattr(self.config, "volume_multiplier", None):
            vol_summary = f"{self.config.volume_multiplier:g}x min quantity ({int(contract.min_volume)} min)"
        else:
            vol_summary = f"1x min quantity ({int(contract.min_volume)} min)"

        self.logger.info(f"Position Sizing: {vol_summary} [Trade Qty != Margin; Committed Margin = Trade Qty / {self.config.leverage}x leverage]")
        self.logger.info(f"Target Leverage: {self.config.leverage}x isolated")
        is_ml_strat = getattr(self.config, "strategy_mode", "").upper() in ("ML", "ML_1M", "ML_MODEL", "ML_1M_MODEL")
        is_smc_strat = getattr(self.config, "strategy_mode", "").upper() in ("ORDER_BLOCK_DEMAND", "ORDER_BOOK_DEMAND", "ORDER_BLOCK", "DEMAND_BLOCK", "SMC")
        if is_smc_strat:
            self.logger.info("Min-Profit Take Profit rule: Dynamic 1:2 R:R (50% partial close at 1:1 & Breakeven lock)")
            self.logger.info(f"Stop Loss rule: Structural Order Block boundary + {getattr(self.config, 'buffer_ticks', 1)}t buffer")
        elif is_ml_strat or getattr(self.config, "dynamic_tp", False):
            tp_mult = getattr(self.config, "tp_atr_mult", 3.0)
            sl_mult = getattr(self.config, "sl_atr_mult", 1.5)
            self.logger.info(f"Min-Profit Take Profit rule: Dynamic ATR Target (~{tp_mult:.1f}x ATR, calibrated per signal)")
            self.logger.info(f"Stop Loss rule: Dynamic ATR Stop (~{sl_mult:.1f}x ATR, calibrated per signal)")
        else:
            self.logger.info(f"Min-Profit Take Profit rule: Entry Price +/- {self.config.tp_ticks} pu (Tick Size)")
            if getattr(self.config, "sl_ticks", None):
                sl_rule_desc = f"{self.config.sl_ticks} ticks"
            elif getattr(self.config, "sl_price_pct", None):
                sl_rule_desc = f"{self.config.sl_price_pct}% price move"
            else:
                sl_rule_desc = f"{self.config.sl_roe_pct}% ROE on margin"
            self.logger.info(f"Stop Loss rule: -{sl_rule_desc}")
        self.logger.info(f"Post-trade cooldown: {self.config.cooldown_seconds}s")
        self.logger.section("PRE-FLIGHT CHECKS PASSED - ENGINE READY")
        return contract

    # =========================================================================
    # TRADE EXECUTION LIFECYCLE
    # =========================================================================

    def execute_single_trade_cycle(self, contract: ContractInfo) -> Optional[TradeOutcome]:
        """
        Executes one full trade cycle:
        1. Checks for signal
        2. Sizes order to min_volume (1 contract)
        3. Submits order with attached TP/SL
        4. Reconciles exact fill
        5. Checks immediate profit close
        6. Monitors until position is closed
        7. Logs outcome to dual-currency journal
        """
        try:
            signal = self.strategy.get_signal()
        except Exception as e:
            self.logger.warning("Error retrieving trade signal: %s", e)
            return None

        if not signal:
            return None

        # Regime & Trend Filter Evaluation
        try:
            htf_tf = getattr(self.config, "htf_timeframe", "15m")
            tf_map = {
                "1m": "Min1", "3m": "Min3", "5m": "Min5", "15m": "Min15",
                "30m": "Min30", "1h": "Min60", "2h": "Hour2", "4h": "Hour4", "1d": "Day1"
            }
            kline_interval = tf_map.get(htf_tf, "Min15") if getattr(self.config, "htf_trend_filter_enabled", False) else "Min1"
            filter_candles = self.market.get_klines(contract.symbol, interval=kline_interval, limit=250)
        except Exception as e:
            self.logger.debug("Could not fetch candles for regime filter evaluation: %s", e)
            filter_candles = []

        allowed, reject_reason = self.filter_pipeline.evaluate(signal, filter_candles, time.time())
        if not allowed:
            self.logger.info(f"[REGIME FILTER] Signal {signal.direction.value} suppressed: {reject_reason}")
            if hasattr(self.strategy, "on_trade_rejected"):
                self.strategy.on_trade_rejected()
            elif hasattr(self.strategy.sub_strategy, "trade_in_progress"):
                self.strategy.sub_strategy.trade_in_progress = False
            return None

        # Check active position concurrency to prevent duplicate positions in the same direction
        # and avoid conflict with manual trades on the same account
        if self.config.mode == EngineMode.LIVE:
            try:
                open_pos_list = self.trader.get_open_positions(contract.symbol)
                for p in open_pos_list:
                    h_vol = float(p.get("holdVol", 0) or p.get("vol", 0))
                    if h_vol > 0:
                        p_type = p.get("positionType")
                        p_side = p.get("side")
                        is_pos_long = (p_type == 1 or str(p_side).upper() in ("1", "LONG", "BUY"))
                        if (signal.direction == OrderDirection.LONG and is_pos_long):
                            self.logger.info(
                                f"[CONCURRENCY] Active LONG already exists on {contract.symbol} (Hold: {h_vol:g} contracts). "
                                f"Skipping new LONG signal to prevent duplicate exposure or manual trade conflict."
                            )
                            if hasattr(self.strategy, "on_trade_rejected"):
                                self.strategy.on_trade_rejected()
                            elif hasattr(self.strategy.sub_strategy, "trade_in_progress"):
                                self.strategy.sub_strategy.trade_in_progress = False
                            return None
                        elif (signal.direction == OrderDirection.SHORT and not is_pos_long):
                            self.logger.info(
                                f"[CONCURRENCY] Active SHORT already exists on {contract.symbol} (Hold: {h_vol:g} contracts). "
                                f"Skipping new SHORT signal to prevent duplicate exposure or manual trade conflict."
                            )
                            if hasattr(self.strategy, "on_trade_rejected"):
                                self.strategy.on_trade_rejected()
                            elif hasattr(self.strategy.sub_strategy, "trade_in_progress"):
                                self.strategy.sub_strategy.trade_in_progress = False
                            return None
            except Exception as pe:
                self.logger.debug("Concurrency position check error: %s", pe)

        self.trade_counter += 1
        trade_id = self.trade_counter
        symbol = contract.symbol
        direction = signal.direction
        is_long = (direction == OrderDirection.LONG)

        # Capture wallet balance BEFORE trade entry (for MongoDB logging and margin sizing)
        balance_before_usdt = getattr(self.config, "simulated_balance_usdt", None)
        balance_before_inr = None
        if self.config.mode == EngineMode.LIVE:
            try:
                pre_balances = self.trader.get_usdt_balance()
                balance_before_usdt = pre_balances.get("available_usdt", 0.0)
                balance_before_inr = pre_balances.get("available_inr", 0.0)
            except Exception:
                pass
        pu = contract.price_unit
        cs = contract.contract_size
        if self.config.leverage > contract.max_leverage:
            self.logger.warning(
                f"Notice: Configured leverage {self.config.leverage}x exceeds {symbol} max allowed ({contract.max_leverage}x). "
                f"Clamped to {contract.max_leverage}x."
            )
        leverage = min(self.config.leverage, contract.max_leverage)

        # Determine trade quantity (volume in contracts)
        # Note: Trade Quantity (Notional Value) is NOT the same as Margin!
        # Trade Quantity = Contracts * Contract Size * Price
        # Committed Margin = Trade Quantity / Leverage
        min_vol = int(contract.min_volume)
        vol_mode = (getattr(self.config, "volume_mode", "MULTIPLIER") or "MULTIPLIER").upper()
        if vol_mode == "MARGIN_PCT" or getattr(self.config, "margin_pct", None) is not None:
            pct = float(getattr(self.config, "margin_pct", 10.0) or 10.0)
            avail_margin = 100.0
            if self.config.mode == EngineMode.LIVE:
                try:
                    balances = self.trader.get_usdt_balance()
                    avail_margin = float(balances.get("available_usdt", 100.0) or 100.0)
                except Exception:
                    avail_margin = 100.0
            elif getattr(self.config, "simulated_balance_usdt", None):
                avail_margin = float(self.config.simulated_balance_usdt)

            desired_margin = (pct / 100.0) * avail_margin
            target_notional = desired_margin * leverage if leverage > 0 else desired_margin
            current_price = getattr(self.strategy, "last_price", 0.0)
            if not current_price or current_price <= 0:
                try:
                    t = self.market.get_ticker(symbol)
                    current_price = float(t.get("lastPrice", 1.0))
                except Exception:
                    current_price = 1.0
            one_contract_notional = cs * current_price
            if one_contract_notional > 0:
                raw_contracts = target_notional / one_contract_notional
                vol_contracts = max(min_vol, int(round(raw_contracts)))
            else:
                vol_contracts = min_vol
            vol_spec_desc = f"{vol_contracts} contract(s) ({pct:g}% margin -> ~{desired_margin} USDT)"
        elif vol_mode == "FIXED_MARGIN" or getattr(self.config, "fixed_margin_usdt", None) is not None:
            desired_margin = float(getattr(self.config, "fixed_margin_usdt", 5.0) or 5.0)
            target_notional = desired_margin * leverage if leverage > 0 else desired_margin
            current_price = getattr(self.strategy, "last_price", 0.0)
            if not current_price or current_price <= 0:
                try:
                    t = self.market.get_ticker(symbol)
                    current_price = float(t.get("lastPrice", 1.0))
                except Exception:
                    current_price = 1.0
            one_contract_notional = cs * current_price
            if one_contract_notional > 0:
                raw_contracts = target_notional / one_contract_notional
                vol_contracts = max(min_vol, int(round(raw_contracts)))
            else:
                vol_contracts = min_vol
            vol_spec_desc = f"{vol_contracts} contract(s) ({desired_margin:.2f} USDT fixed margin)"
        elif vol_mode == "CONTRACTS" and getattr(self.config, "volume_contracts", None):
            vol_contracts = max(min_vol, int(self.config.volume_contracts))
            vol_spec_desc = f"{vol_contracts} contract(s)"
        elif vol_mode == "MULTIPLIER" and getattr(self.config, "volume_multiplier", None):
            mult = float(self.config.volume_multiplier)
            vol_contracts = max(min_vol, int(round(min_vol * mult)))
            vol_spec_desc = f"{vol_contracts} contract(s) ({mult:g}x min)"
        else:
            vol_contracts = min_vol
            vol_spec_desc = f"{vol_contracts} contract(s) (1x min)"

        underlying_qty = vol_contracts * cs

        self.logger.section(f"EXECUTING TRADE #{trade_id} [{direction.value}] - {symbol}")

        # Determine effective TP ticks:
        # If dynamic_tp is enabled, ML strategy, or OrderBlockDemand strategy emitted target_ticks:
        is_ml_sig = signal.sub_strategy_name in ("ML_1M_MODEL", "MLStrategy")
        is_smc_sig = ("ORDER_BLOCK" in str(signal.metadata.get("strategy_mode", "")).upper()) or ("OrderBlock" in signal.sub_strategy_name)
        if (getattr(self.config, "dynamic_tp", False) or is_ml_sig or is_smc_sig) and "target_ticks" in signal.metadata:
            target_tp_ticks = int(signal.metadata["target_ticks"])
        else:
            target_tp_ticks = int(self.config.tp_ticks)

        # Determine effective SL:
        if (is_ml_sig or is_smc_sig) and "target_sl_ticks" in signal.metadata:
            sl_ticks_to_use = int(signal.metadata["target_sl_ticks"])
            sl_roe_to_use = None
        else:
            sl_ticks_to_use = self.config.sl_ticks
            sl_roe_to_use = self.config.sl_roe_pct

        if "agreeing_signals" in signal.metadata:
            agreeing = signal.metadata.get("agreeing_signals", [])
            obi_z = signal.metadata.get("obi_z", 0.0)
            delta_z = signal.metadata.get("delta_z", 0.0)
            vamp_z = signal.metadata.get("vamp_z", 0.0)
            rec = signal.metadata.get("delta_recency", 0.0)
            tp_desc = f"+{target_tp_ticks} pu ticks" if not getattr(self.config, "dynamic_tp", False) else f"+{target_tp_ticks} pu ticks (dynamic)"
            self.logger.info(
                f"[MICROSTRUCTURE TRIGGER] Confluence: {agreeing} | Target TP: {tp_desc} | "
                f"OBI z={obi_z:+.2f} | Delta z={delta_z:+.2f} (rec={rec:.2f}) | VAMP z={vamp_z:+.2f}"
            )
        elif is_ml_sig:
            conf_val = signal.metadata.get("confidence", 0.0)
            rr_val = signal.metadata.get("risk_reward_ratio", 2.0)
            self.logger.info(
                f"[ML ALPHA TRIGGER] Confidence: {conf_val:.1%} | Target TP: +{target_tp_ticks}t | SL: -{sl_ticks_to_use}t | R:R={rr_val}"
            )
        elif is_smc_sig:
            ps = contract.price_precision
            z_type = signal.metadata.get("zone_type", "ORDER_BLOCK")
            z_id = signal.metadata.get("zone_id", "N/A")
            z_low = float(signal.metadata.get("zone_low", 0.0) or 0.0)
            z_high = float(signal.metadata.get("zone_high", 0.0) or 0.0)
            z_mid = float(signal.metadata.get("zone_mid", (z_low + z_high) / 2.0) or 0.0)
            z_bar = signal.metadata.get("zone_creation_bar_idx")
            z_time = signal.metadata.get("zone_creation_time_utc", "N/A")
            bos_bar = signal.metadata.get("bos_bar_idx")
            bos_p = signal.metadata.get("bos_price")
            retest_bar = signal.metadata.get("retest_bar_idx")
            trig_time = signal.metadata.get("trigger_candle_time_utc", "N/A")
            eval_bar = signal.metadata.get("eval_bar_idx")
            rr_val = signal.metadata.get("risk_reward_ratio", 2.0)
            t1_p = signal.metadata.get("target_1to1_price")
            t2_p = signal.metadata.get("target_1to2_price")
            tf = signal.metadata.get("timeframe", getattr(self.config, "timeframe", "15m"))

            loc_str = f"Bar #{z_bar} ({z_time})" if z_bar is not None else str(z_time)
            bos_str = f"Bar #{bos_bar} @ {bos_p:.{ps}f} USDT" if (bos_bar is not None and bos_p is not None) else (f"Bar #{bos_bar}" if bos_bar is not None else "N/A")
            retest_str = f" | Retest Bar: #{retest_bar}" if retest_bar is not None else ""
            t1_str = f"{t1_p:.{ps}f} USDT" if t1_p is not None else "N/A"
            t2_str = f"{t2_p:.{ps}f} USDT" if t2_p is not None else "N/A"

            self.logger.info(
                f"\n{'='*78}\n"
                f"🎯 [SMC ORDER BLOCK IDENTIFIED & TRIGGERED]\n"
                f"{'='*78}\n"
                f"Pair & Direction   : {symbol} [{direction.value}] | Timeframe: {tf} | Leverage: {leverage}x\n"
                f"Identified Zone    : {z_type} (#{z_id})\n"
                f"Candle Location    : {loc_str}\n"
                f"Structure Break    : BOS {bos_str}{retest_str}\n"
                f"Trigger Candle     : Bar #{eval_bar} ({trig_time})\n"
                f"Zone Boundaries    : Low: {z_low:.{ps}f} <---> Mid (50%): {z_mid:.{ps}f} <---> High: {z_high:.{ps}f} USDT\n"
                f"Target Levels      : 1:1 TP: {t1_str} (+{signal.metadata.get('target_1to1_ticks', target_tp_ticks // 2)}t) | "
                f"1:2 TP: {t2_str} (+{target_tp_ticks}t) | SL: -{sl_ticks_to_use}t | R:R = 1:{rr_val}\n"
                f"{'='*78}"
            )

        # Get fresh market snapshot
        try:
            ticker = self.market.get_ticker(symbol)
            last_price = float(ticker.get("lastPrice", 0.0) or ticker.get("fairPrice", 1.0))
            ref_price = last_price
            inr_rate = self.market.get_inr_rate()
            self.logger.set_inr_rate(inr_rate)
        except Exception as e:
            self.logger.warning("Failed to fetch fresh market snapshot for %s: %s", symbol, e)
            if hasattr(self.strategy, "on_trade_rejected"):
                self.strategy.on_trade_rejected()
            elif hasattr(self.strategy.sub_strategy, "trade_in_progress"):
                self.strategy.sub_strategy.trade_in_progress = False
            return None

        # Calculate estimated TP & SL prices
        ps = contract.price_precision
        base_coin = contract.base_coin or symbol.split('_')[0]

        est_tp = self.strategy.calculate_min_profit_tp(
            direction=direction,
            entry_price=ref_price,
            price_unit=pu,
            tp_ticks=target_tp_ticks,
            precision=ps
        )
        est_sl = self.strategy.calculate_stop_loss(
            direction=direction,
            entry_price=ref_price,
            leverage=leverage,
            sl_roe_pct=sl_roe_to_use,
            sl_ticks=sl_ticks_to_use,
            sl_price_pct=self.config.sl_price_pct,
            price_unit=pu,
            precision=ps
        )

        # Dynamic Margin Sizing & Fallback Validation
        if balance_before_usdt is not None and balance_before_usdt > 0:
            initial_req_margin = (underlying_qty * ref_price) / leverage
            if initial_req_margin > balance_before_usdt:
                fallback_pct = float(getattr(self.config, "margin_fallback_pct", 25.0) or 25.0)
                target_margin = (fallback_pct / 100.0) * balance_before_usdt
                target_notional = target_margin * leverage
                if cs * ref_price > 0:
                    scaled_contracts = int(target_notional / (cs * ref_price))
                    target_contracts = max(min_vol, scaled_contracts)
                else:
                    target_contracts = min_vol

                self.logger.warning(
                    f"⚠️ Insufficient Margin: Required {initial_req_margin:.4f} USDT for {vol_spec_desc} "
                    f"exceeds available balance ({balance_before_usdt:.4f} USDT). "
                    f"Applying {fallback_pct:g}% available margin fallback: sizing to {target_contracts} contract(s) "
                    f"(~{((target_contracts * cs * ref_price) / leverage):.4f} USDT margin)."
                )
                vol_contracts = target_contracts
                underlying_qty = vol_contracts * cs
                vol_spec_desc = f"{vol_contracts} contract(s) ({fallback_pct:g}% margin fallback)"

        notional_est_usdt = underlying_qty * ref_price
        notional_est_inr = notional_est_usdt * inr_rate
        margin_est_usdt = notional_est_usdt / leverage
        margin_est_inr = margin_est_usdt * inr_rate

        sl_desc = (
            f"-{sl_ticks_to_use} ticks" if sl_ticks_to_use
            else f"-{self.config.sl_price_pct}% price" if self.config.sl_price_pct
            else f"-{self.config.sl_roe_pct}% ROE"
        )

        self.logger.info(
            f"Pre-Trade Spec: Vol: {vol_spec_desc} ({underlying_qty:g} {base_coin}) | "
            f"Trade Qty (Notional): {self.logger.format_dual(notional_est_usdt)} | "
            f"Committed Margin (Qty/{leverage}x): {self.logger.format_dual(margin_est_usdt)}"
        )
        self.logger.info(
            f"Reference Price: {ref_price:.{ps}f} USDT | "
            f"Attached Min-Profit TP: {est_tp:.{ps}f} USDT (+{target_tp_ticks} pu) | "
            f"Attached SL: {est_sl:.{ps}f} USDT ({sl_desc})"
        )

        open_time = time.time()
        self._in_active_trade = True

        # =====================================================================
        # SUBMIT ORDER
        # =====================================================================
        try:
            if self.config.mode == EngineMode.LIVE:
                outcome = self._execute_live_trade(
                    trade_id=trade_id,
                    contract=contract,
                    direction=direction,
                    vol_contracts=vol_contracts,
                    leverage=leverage,
                    est_tp=est_tp,
                    est_sl=est_sl,
                    open_time=open_time,
                    sub_strategy_name=signal.sub_strategy_name,
                    signal=signal
                )
            else:
                outcome = self._simulate_dry_run_trade(
                    trade_id=trade_id,
                    contract=contract,
                    direction=direction,
                    vol_contracts=vol_contracts,
                    leverage=leverage,
                    open_time=open_time,
                    sub_strategy_name=signal.sub_strategy_name,
                    target_tp_ticks=target_tp_ticks,
                    signal=signal
                )
        except Exception as e:
            self.logger.error("Error executing trade cycle #%d for %s: %s", trade_id, symbol, e, exc_info=True)
            outcome = None
        finally:
            self._in_active_trade = False

        if outcome:
            # Attach balance_before to outcome for MongoDB
            outcome.balance_before_trade_usdt = balance_before_usdt
            outcome.balance_before_trade_inr = balance_before_inr

            # Propagate ML Strategy Telemetry to outcome and MongoDB
            if "confidence" in signal.metadata:
                outcome.ml_confidence = signal.metadata.get("confidence")
                outcome.ml_prob_buy = signal.metadata.get("prob_buy")
                outcome.ml_prob_sell = signal.metadata.get("prob_sell")
                outcome.ml_prob_wait = signal.metadata.get("prob_wait")
                outcome.ml_tp_ticks = signal.metadata.get("target_ticks")
                outcome.ml_sl_ticks = signal.metadata.get("target_sl_ticks")
                outcome.ml_atr_14 = signal.metadata.get("atr_14")

            # Propagate SMC Strategy Telemetry to outcome and MongoDB
            is_smc_sig = ("ORDER_BLOCK" in str((signal.metadata if signal and signal.metadata else {}).get("strategy_mode", "")).upper()) or ("OrderBlock" in getattr(outcome, "sub_strategy_name", "")) or ("DEMAND" in getattr(outcome, "sub_strategy_name", "").upper()) or (getattr(self.config, "strategy_mode", "").upper() in ("ORDER_BLOCK_DEMAND", "ORDER_BOOK_DEMAND", "ORDER_BLOCK", "DEMAND_BLOCK", "SMC"))
            if is_smc_sig and signal and signal.metadata:
                outcome.smc_zone_id = signal.metadata.get("zone_id")
                outcome.smc_zone_type = signal.metadata.get("zone_type")
                outcome.smc_zone_high = signal.metadata.get("zone_high")
                outcome.smc_zone_low = signal.metadata.get("zone_low")
                outcome.smc_zone_mid = signal.metadata.get("zone_mid")
                outcome.smc_zone_creation_bar_idx = signal.metadata.get("zone_creation_bar_idx")
                outcome.smc_zone_creation_ts = signal.metadata.get("zone_creation_ts")
                outcome.smc_zone_creation_time_utc = signal.metadata.get("zone_creation_time_utc")
                outcome.smc_bos_bar_idx = signal.metadata.get("bos_bar_idx")
                outcome.smc_bos_price = signal.metadata.get("bos_price")
                outcome.smc_trigger_candle_time_utc = signal.metadata.get("trigger_candle_time_utc")
                outcome.smc_trigger_bar_idx = signal.metadata.get("eval_bar_idx")
                outcome.smc_fvg_size = signal.metadata.get("fvg_size")
                outcome.smc_target_1to1 = signal.metadata.get("target_1to1_price")
                outcome.smc_target_1to2 = signal.metadata.get("target_1to2_price")
                outcome.smc_partial_tp_hit = bool(signal.metadata.get("partial_tp_hit", False))


            # Propagate Tick-Constrained Market Making Telemetry
            if signal.metadata and ("ofi_ratio" in signal.metadata or "tick_bps" in signal.metadata):
                outcome.mm_ofi_ratio = signal.metadata.get("ofi_ratio")
                outcome.mm_tick_bps = signal.metadata.get("tick_bps")
                outcome.mm_htf_sideways = signal.metadata.get("htf_is_sideways")

            # Output and record outcome
            card = self.outcome_logger.log_outcome(outcome)
            self.logger.info("\n" + card)
            self.strategy.on_trade_completed(outcome)

            # Log to MongoDB (real-time, immediately after verification)
            if self.mongo_logger and self.config.mode == EngineMode.LIVE:
                self.mongo_logger.log_executed_trade(
                    outcome=outcome,
                    config=self.config,
                    balance_before_usdt=balance_before_usdt,
                    balance_before_inr=balance_before_inr
                )
        else:
            self.strategy.on_trade_rejected()

        return outcome

    # =========================================================================
    # LIVE EXECUTION & POSITION MONITORING
    # =========================================================================

    def _execute_live_trade(
        self,
        trade_id: int,
        contract: ContractInfo,
        direction: OrderDirection,
        vol_contracts: int,
        leverage: int,
        est_tp: float,
        est_sl: float,
        open_time: float,
        sub_strategy_name: str,
        signal: Optional[TradeSignal] = None
    ) -> Optional[TradeOutcome]:
        symbol = contract.symbol
        side_str = "LONG" if direction == OrderDirection.LONG else "SHORT"
        pu = contract.price_unit
        cs = contract.contract_size
        underlying_qty = vol_contracts * cs
        exec_style = getattr(self.config, "execution_style", "PURE_MARKET") or "PURE_MARKET"
        order_type = getattr(self.config, "order_type", "MARKET") or "MARKET"
        is_maker = (str(exec_style).upper() == "MAKER_HYBRID") or (str(order_type).upper() == "LIMIT")
        timeout_sec = getattr(self.config, "maker_queue_timeout_seconds", getattr(self.config, "limit_order_timeout_seconds", 10.0))

        if is_maker:
            ticker = self.market.get_ticker(symbol)
            last_price = float(ticker.get("lastPrice", 0.0) or 1.0)
            bid1 = float(ticker.get("bid1", 0.0) or last_price)
            ask1 = float(ticker.get("ask1", 0.0) or last_price)

            # Limit order placed at maker side to capture zero maker fee and avoid 1-tick taker slippage
            limit_price = bid1 if direction == OrderDirection.LONG else ask1
            limit_price = round(limit_price, contract.price_precision)

            self.logger.info(
                f"Submitting live Post-Only LIMIT order at {limit_price:.{contract.price_precision}f} USDT..."
            )
            try:
                order_res = self.trader.create_order(
                    symbol=symbol,
                    side=side_str,
                    vol_contracts=vol_contracts,
                    order_type="LIMIT",
                    price=limit_price,
                    leverage=leverage,
                    is_isolated=self.config.is_isolated
                )
            except Exception as e:
                self.logger.error("Failed to submit limit order: %s", e)
                return None

            data = order_res.get("data", {})
            order_id = str(data.get("orderId") or "")
            self.logger.info(f"Live LIMIT order accepted by KCEX. Order ID: {order_id}. Waiting up to {timeout_sec}s for fill...")

            # Poll for order fill
            start_poll = time.time()
            is_filled = False
            while (time.time() - start_poll < timeout_sec) and not self._shutdown_requested:
                time.sleep(0.5)
                # 1. Check if position has opened
                open_positions = self.trader.get_open_positions(symbol)
                for p in open_positions:
                    h_vol = float(p.get("holdVol", 0) or p.get("vol", 0))
                    if h_vol > 0:
                        is_filled = True
                        break
                if is_filled:
                    break

                # 2. Check if order is still resting in open orders
                try:
                    open_orders = self.trader.get_open_orders()
                    order_still_open = any(str(o.get("orderId")) == order_id for o in open_orders)
                    if not order_still_open:
                        # Order no longer open, give exchange 300ms to persist position
                        time.sleep(0.3)
                        open_positions = self.trader.get_open_positions(symbol)
                        for p in open_positions:
                            h_vol = float(p.get("holdVol", 0) or p.get("vol", 0))
                            if h_vol > 0:
                                is_filled = True
                                break
                        break
                except Exception as oe:
                    self.logger.debug("Error checking open orders: %s", oe)

            if not is_filled:
                cancel_unfilled = getattr(self.config, "cancel_if_unfilled", False)
                if cancel_unfilled:
                    self.logger.warning(
                        f"LIMIT order {order_id} not filled within {timeout_sec}s timeout. Cancelling order to protect execution..."
                    )
                    try:
                        self.trader.cancel_order(order_id)
                    except Exception as ce:
                        self.logger.warning("Error cancelling unfilled limit order: %s", ce)

                    # Log cancelled order to MongoDB
                    self._cancelled_order_count += 1
                    if self.mongo_logger and self.config.mode == EngineMode.LIVE:
                        try:
                            cancel_ticker = self.market.get_ticker(symbol)
                            market_snap = {
                                "bid1": float(cancel_ticker.get("bid1", 0)),
                                "ask1": float(cancel_ticker.get("ask1", 0)),
                                "last_price": float(cancel_ticker.get("lastPrice", 0)),
                            }
                        except Exception:
                            market_snap = {}
                        cancel_bal_usdt = None
                        cancel_bal_inr = None
                        try:
                            cb = self.trader.get_usdt_balance()
                            cancel_bal_usdt = cb.get("available_usdt", 0.0)
                            cancel_bal_inr = cb.get("available_inr", 0.0)
                        except Exception:
                            pass
                        self.mongo_logger.log_cancelled_order(
                            symbol=symbol,
                            direction=side_str,
                            intended_entry_price=limit_price,
                            order_id=order_id,
                            timeout_seconds=timeout_sec,
                            strategy_name=sub_strategy_name,
                            config=self.config,
                            market_snapshot=market_snap,
                            balance_usdt=cancel_bal_usdt,
                            balance_inr=cancel_bal_inr,
                            inr_rate=self.market.get_inr_rate()
                        )
                    return None
                else:
                    self.logger.info(
                        f"LIMIT order {order_id} resting in book (cancel_if_unfilled=False). Waiting for fill..."
                    )
                    while not is_filled and not self._shutdown_requested:
                        time.sleep(1.0)
                        open_positions = self.trader.get_open_positions(symbol)
                        for p in open_positions:
                            h_vol = float(p.get("holdVol", 0) or p.get("vol", 0))
                            if h_vol > 0:
                                is_filled = True
                                break
                    if not is_filled:
                        return None
        else:
            self.logger.info("Submitting live MARKET order...")
            order_res = self.trader.create_order(
                symbol=symbol,
                side=side_str,
                vol_contracts=vol_contracts,
                order_type="MARKET",
                leverage=leverage,
                is_isolated=self.config.is_isolated
            )
            data = order_res.get("data", {})
            order_id = str(data.get("orderId") or "")
            self.logger.info(f"Live order accepted by KCEX. Order ID: {order_id}")
            # Short pause to allow order book fill
            time.sleep(0.3)

        # Reconcile open position to obtain exact entry price and positionId
        entry_price = est_tp - pu if direction == OrderDirection.LONG else est_tp + pu
        position_id = None
        open_positions = self.trader.get_open_positions(symbol)
        
        for p in open_positions:
            h_vol = float(p.get("holdVol", 0) or p.get("vol", 0))
            if h_vol > 0:
                p_type = p.get("positionType")
                p_side = p.get("side")
                is_pos_long = (p_type == 1 or str(p_side).upper() in ("1", "LONG", "BUY"))
                if (direction == OrderDirection.LONG and is_pos_long) or (direction == OrderDirection.SHORT and not is_pos_long):
                    position_id = int(p.get("positionId"))
                    entry_price = float(p.get("openAvgPrice") or p.get("holdAvgPrice") or entry_price)
                    break

        # Calculate exact min-profit TP and exact SL from actual filled entry price
        atr_val = None
        if getattr(self.config, "use_atr_targets", False):
            try:
                c_list = self.market.get_klines(symbol, interval="Min1", limit=30)
                if len(c_list) >= 15:
                    from strategies.filters import compute_atr_series
                    highs = [float(c.high if hasattr(c, "high") else c[2]) for c in c_list]
                    lows = [float(c.low if hasattr(c, "low") else c[3]) for c in c_list]
                    closes = [float(c.close if hasattr(c, "close") else c[4]) for c in c_list]
                    atrs = compute_atr_series(highs, lows, closes, period=14)
                    if atrs and atrs[-1] > 0:
                        atr_val = atrs[-1]
            except Exception:
                pass

        is_ml_sig = (sub_strategy_name in ("ML_1M_MODEL", "MLStrategy")) or (getattr(self.config, "strategy_mode", "").upper() in ("ML", "ML_1M", "ML_MODEL", "ML_1M_MODEL")) or getattr(self.config, "dynamic_tp", False)
        is_smc_sig = ("ORDER_BLOCK" in str((signal.metadata if signal and signal.metadata else {}).get("strategy_mode", "")).upper()) or ("OrderBlock" in sub_strategy_name) or (getattr(self.config, "strategy_mode", "").upper() in ("ORDER_BLOCK_DEMAND", "ORDER_BOOK_DEMAND", "ORDER_BLOCK", "DEMAND_BLOCK", "SMC"))
        if (is_ml_sig or is_smc_sig) and signal and signal.metadata and "target_ticks" in signal.metadata:
            effective_tp_ticks = int(signal.metadata["target_ticks"])
        else:
            effective_tp_ticks = self.config.tp_ticks

        if (is_ml_sig or is_smc_sig) and signal and signal.metadata and "target_sl_ticks" in signal.metadata:
            effective_sl_ticks = int(signal.metadata["target_sl_ticks"])
            effective_sl_roe = None
        else:
            effective_sl_ticks = self.config.sl_ticks
            effective_sl_roe = self.config.sl_roe_pct

        exact_tp = self.strategy.calculate_min_profit_tp(
            direction=direction,
            entry_price=entry_price,
            price_unit=pu,
            tp_ticks=effective_tp_ticks,
            precision=contract.price_precision,
            atr_value=atr_val
        )
        exact_sl = self.strategy.calculate_stop_loss(
            direction=direction,
            entry_price=entry_price,
            leverage=leverage,
            sl_roe_pct=effective_sl_roe,
            sl_ticks=effective_sl_ticks,
            sl_price_pct=self.config.sl_price_pct,
            price_unit=pu,
            precision=contract.price_precision,
            atr_value=atr_val
        )

        ps = contract.price_precision
        if is_smc_sig and signal and signal.metadata:
            z_type = signal.metadata.get("zone_type", "ORDER_BLOCK")
            z_id = signal.metadata.get("zone_id", "N/A")
            z_low = float(signal.metadata.get("zone_low", 0.0) or 0.0)
            z_high = float(signal.metadata.get("zone_high", 0.0) or 0.0)
            z_mid = float(signal.metadata.get("zone_mid", (z_low + z_high) / 2.0) or 0.0)
            z_bar = signal.metadata.get("zone_creation_bar_idx")
            z_time = signal.metadata.get("zone_creation_time_utc", "N/A")
            bos_bar = signal.metadata.get("bos_bar_idx")
            bos_p = signal.metadata.get("bos_price")
            retest_bar = signal.metadata.get("retest_bar_idx")
            trig_time = signal.metadata.get("trigger_candle_time_utc", "N/A")
            eval_bar = signal.metadata.get("eval_bar_idx")
            t1_p = signal.metadata.get("target_1to1_price", exact_tp)
            t2_p = signal.metadata.get("target_1to2_price", exact_tp)
            tf = signal.metadata.get("timeframe", getattr(self.config, "timeframe", "15m"))

            loc_str = f"Bar #{z_bar} ({z_time})" if z_bar is not None else str(z_time)
            bos_str = f"Bar #{bos_bar} @ {bos_p:.{ps}f} USDT" if (bos_bar is not None and bos_p is not None) else (f"Bar #{bos_bar}" if bos_bar is not None else "N/A")
            retest_str = f" | Retest Bar: #{retest_bar}" if retest_bar is not None else ""
            t1_str = f"{t1_p:.{ps}f} USDT" if t1_p is not None else "N/A"
            t2_str = f"{t2_p:.{ps}f} USDT" if t2_p is not None else "N/A"

            self.logger.info(
                f"\n{'='*78}\n"
                f"🎯 [LIVE ORDER BLOCK TRADE EXECUTED & ACTIVE]\n"
                f"{'='*78}\n"
                f"Pair & Direction   : {symbol} [{direction.value}] | Timeframe: {tf} | Leverage: {leverage}x\n"
                f"Volume Executed    : {vol_contracts} contract(s) ({underlying_qty:g} {contract.base_coin})\n"
                f"Identified Zone    : {z_type} (#{z_id})\n"
                f"Candle Location    : {loc_str}\n"
                f"Structure Break    : BOS {bos_str}{retest_str}\n"
                f"Trigger Candle     : Bar #{eval_bar} ({trig_time})\n"
                f"Zone Boundaries    : Low: {z_low:.{ps}f} <---> Mid (50%): {z_mid:.{ps}f} <---> High: {z_high:.{ps}f} USDT\n"
                f"Execution Levels   :\n"
                f"  • Entry Fill     : {entry_price:.{ps}f} USDT\n"
                f"  • Stop Loss      : {exact_sl:.{ps}f} USDT\n"
                f"  • 1:1 TP Target  : {t1_str} (+{effective_tp_ticks // 2}t | 50% Partial Close + BE Lock)\n"
                f"  • 1:2 TP Runner  : {t2_str} (+{effective_tp_ticks}t | Full Runner Exit)\n"
                f"{'='*78}"
            )
        else:
            self.logger.info(
                f"Position Filled: Entry Price = {entry_price:.{ps}f} USDT | "
                f"Exact Min-Profit TP = {exact_tp:.{ps}f} USDT | Exact SL = {exact_sl:.{ps}f} USDT"
            )

        # Check immediate profit close condition using executable price (bid for LONG, ask for SHORT)
        ticker = self.market.get_ticker(symbol)
        last_p = float(ticker.get("lastPrice", entry_price))
        bid1 = float(ticker.get("bid1", 0.0))
        ask1 = float(ticker.get("ask1", 0.0))
        exec_price = (bid1 if bid1 > 0 else last_p) if direction == OrderDirection.LONG else (ask1 if ask1 > 0 else last_p)

        exit_price = last_p
        exit_reason = ExitReason.UNKNOWN
        close_order_id = None

        if self.strategy.is_better_than_min_profit(direction, exec_price, exact_tp, entry_price=entry_price):
            op_sym = ">=" if direction == OrderDirection.LONG else "<="
            self.logger.info(
                f"[IMMEDIATE PROFIT TRIGGER] Executable price ({exec_price:.{ps}f} USDT) is already {op_sym} "
                f"Min-Profit TP ({exact_tp:.{ps}f} USDT)! Closing immediately..."
            )
            close_res = self.trader.close_position(
                position_id=position_id or 0,
                symbol=symbol,
                side=side_str,
                vol_contracts=vol_contracts,
                leverage=leverage,
                is_isolated=self.config.is_isolated,
                is_market=True,
                price=exec_price
            )
            close_order_id = str(close_res.get("data", {}).get("orderId") or "")
            exit_price = exec_price
            exit_reason = ExitReason.IMMEDIATE_PROFIT_CLOSE
            time.sleep(0.5)
        else:
            # Register & verify server-side position TP/SL and pre-placed 1:1 limit orders (checking 2-3+ times)
            pre_placed_tp1_order_id = None
            tp1_contracts = (vol_contracts // 2) if vol_contracts >= 2 else 1
            if position_id:
                pre_placed_tp1_order_id, tp1_contracts = self._setup_and_verify_position_orders(
                    symbol=symbol,
                    position_id=position_id,
                    direction=direction,
                    vol_contracts=vol_contracts,
                    leverage=leverage,
                    exact_tp=exact_tp,
                    exact_sl=exact_sl,
                    precision=ps,
                    entry_price=entry_price,
                    signal=signal,
                    is_smc_sig=is_smc_sig
                )

            # Active position monitoring loop
            self.logger.set_has_active_positions(True)
            self.logger.info("Entering active position monitoring loop...")
            try:
                exit_price, exit_reason, close_order_id = self._monitor_live_position(
                    symbol=symbol,
                    position_id=position_id,
                    direction=direction,
                    vol_contracts=vol_contracts,
                    leverage=leverage,
                    exact_tp=exact_tp,
                    exact_sl=exact_sl,
                    precision=ps,
                    entry_price=entry_price,
                    open_time=open_time,
                    signal=signal,
                    pre_placed_tp1_order_id=pre_placed_tp1_order_id,
                    tp1_contracts=tp1_contracts
                )
            finally:
                self.logger.set_has_active_positions(False)

        close_time = time.time()
        duration = max(0.1, close_time - open_time)

        # Reconcile exact closing order, exit price, profit, and exit reason from KCEX history
        reconciled_exit_price, reconciled_pnl, reconciled_reason, hist_close_id, reconciled_pos_id = (
            self._reconcile_closed_trade_from_kcex(
                symbol=symbol,
                open_order_id=order_id,
                position_id=position_id,
                direction=direction,
                entry_price=entry_price,
                pu=pu,
                default_exit_price=exit_price,
                initial_reason=exit_reason if exit_reason != ExitReason.UNKNOWN else None,
                open_time=open_time
            )
        )

        exit_price = reconciled_exit_price
        realized_pnl_usdt = reconciled_pnl
        if reconciled_reason:
            exit_reason = reconciled_reason
        if hist_close_id:
            close_order_id = hist_close_id
        if reconciled_pos_id:
            position_id = reconciled_pos_id

        # Calculate math fallback if history returned zero but price moved
        price_diff = (exit_price - entry_price) if direction == OrderDirection.LONG else (entry_price - exit_price)
        if realized_pnl_usdt == 0.0 and abs(price_diff) > 1e-6:
            realized_pnl_usdt = underlying_qty * price_diff

        inr_rate = self.market.get_inr_rate()
        notional_usdt = underlying_qty * entry_price
        notional_inr = notional_usdt * inr_rate
        margin_usdt = notional_usdt / leverage
        margin_inr = margin_usdt * inr_rate
        realized_pnl_inr = realized_pnl_usdt * inr_rate
        roe_pct = (realized_pnl_usdt / margin_usdt) * 100.0 if margin_usdt > 0 else 0.0
        pnl_pct = (price_diff / entry_price) * 100.0 if entry_price > 0 else 0.0

        # Fetch fresh live account balance after trade
        balance_after_usdt = None
        balance_after_inr = None
        try:
            balances = self.trader.get_usdt_balance()
            balance_after_usdt = balances.get("available_usdt", 0.0)
            balance_after_inr = balances.get("available_inr", 0.0)
            equity_usdt = balances.get("equity_usdt", 0.0)
            equity_inr = balances.get("equity_inr", 0.0)
            self.logger.info(
                f"[BALANCE AFTER TRADE #{trade_id}] Available: {balance_after_usdt} USDT (INR {balance_after_inr:.2f}) | "
                f"Equity: {equity_usdt} USDT (INR {equity_inr:.2f})"
            )
        except Exception as e:
            self.logger.debug("Could not fetch balance after trade: %s", e)

        fee_open_rate = contract.maker_fee_rate if is_maker else contract.taker_fee_rate
        is_tp_close = (exit_reason in (ExitReason.MIN_PROFIT_TP_HIT, ExitReason.IMMEDIATE_PROFIT_CLOSE) and getattr(self.config, "resting_limit_tp", False))
        fee_close_rate = contract.maker_fee_rate if is_tp_close else contract.taker_fee_rate
        fee_open_usdt = notional_usdt * fee_open_rate
        fee_close_usdt = (underlying_qty * exit_price) * fee_close_rate
        fee_total_usdt = fee_open_usdt + fee_close_usdt
        fee_total_inr = fee_total_usdt * inr_rate

        return TradeOutcome(
            trade_id=trade_id,
            symbol=symbol,
            direction=direction,
            sub_strategy_name=sub_strategy_name,
            mode=EngineMode.LIVE,
            leverage=leverage,
            vol_contracts=vol_contracts,
            contract_size=cs,
            underlying_quantity=underlying_qty,
            base_coin=contract.base_coin or symbol.split('_')[0],
            entry_price=entry_price,
            exit_price=exit_price,
            min_profit_tp_price=exact_tp,
            stop_loss_price=exact_sl,
            price_unit=pu,
            price_precision=contract.price_precision,
            open_time=open_time,
            close_time=close_time,
            duration_seconds=duration,
            notional_value_usdt=notional_usdt,
            notional_value_inr=notional_inr,
            margin_used_usdt=margin_usdt,
            margin_used_inr=margin_inr,
            realized_pnl_usdt=realized_pnl_usdt,
            realized_pnl_inr=realized_pnl_inr,
            pnl_percentage=pnl_pct,
            roe_percentage=roe_pct,
            fee_open_usdt=fee_open_usdt,
            fee_close_usdt=fee_close_usdt,
            fee_total_usdt=fee_total_usdt,
            fee_total_inr=fee_total_inr,
            inr_rate=inr_rate,
            exit_reason=exit_reason,
            balance_after_trade_usdt=balance_after_usdt,
            balance_after_trade_inr=balance_after_inr,
            order_id=order_id,
            close_order_id=close_order_id,
            position_id=position_id
        )

    def _reconcile_closed_trade_from_kcex(
        self,
        symbol: str,
        open_order_id: Optional[str],
        position_id: Optional[int],
        direction: OrderDirection,
        entry_price: float,
        pu: float,
        default_exit_price: float,
        initial_reason: Optional[ExitReason] = None,
        open_time: Optional[float] = None
    ) -> tuple[float, float, ExitReason, Optional[str], Optional[int]]:
        """
        Queries KCEX history_orders and history_positions to reliably determine
        the exact exit price, realized PnL, exit reason (TAKE_PROFIT vs STOP_LOSS),
        and server IDs.
        """
        time.sleep(0.5)  # Allow KCEX backend to persist post-close records

        exit_price = default_exit_price
        realized_pnl = 0.0
        exit_reason = initial_reason or ExitReason.UNKNOWN
        close_order_id = None
        reconciled_pos_id = position_id

        closing_side = 4 if direction == OrderDirection.LONG else 2
        min_ts = int((open_time - 5.0) * 1000) if open_time else 0

        # 1. Query order history for the closing order
        try:
            res = self.client.get_private(
                KCEXConfig.ENDPOINT_ORDER_HISTORY,
                params={"symbol": symbol.upper(), "category": 1, "page_num": 1, "page_size": 10}
            )
            orders = res.get("data", [])
            if isinstance(orders, dict):
                orders = orders.get("list", [])

            for o in orders:
                if o.get("side") == closing_side and float(o.get("dealVol", 0)) > 0:
                    order_time = int(o.get("createTime", 0) or o.get("updateTime", 0))
                    if min_ts > 0 and order_time > 0 and order_time < min_ts:
                        continue
                    close_order_id = str(o.get("orderId"))
                    deal_price = float(o.get("dealAvgPrice") or o.get("price") or 0.0)
                    if deal_price > 0:
                        exit_price = deal_price
                    realized_pnl = float(o.get("profit", 0.0))
                    if o.get("positionId"):
                        reconciled_pos_id = int(o.get("positionId"))

                    external_oid = str(o.get("externalOid") or "")
                    if realized_pnl > 0:
                        exit_reason = ExitReason.MIN_PROFIT_TP_HIT
                    elif realized_pnl < 0:
                        exit_reason = ExitReason.STOP_LOSS_HIT
                    elif "TAKE_PROFIT" in external_oid:
                        exit_reason = ExitReason.MIN_PROFIT_TP_HIT
                    elif "STOP_LOSS" in external_oid:
                        exit_reason = ExitReason.STOP_LOSS_HIT
                    break
        except Exception as e:
            self.logger.debug("Error inspecting history_orders: %s", e)

        # 2. If exit reason is still uncertain, inspect history_positions
        if exit_reason in (None, ExitReason.UNKNOWN) or realized_pnl == 0.0:
            try:
                hist_positions = self.trader.get_position_history(page_size=5)
                for h in hist_positions:
                    h_pos_id = int(h.get("positionId") or 0)
                    h_sym = str(h.get("symbol") or "")
                    if (reconciled_pos_id and h_pos_id == reconciled_pos_id) or (h_sym == symbol.upper()):
                        reconciled_pos_id = h_pos_id
                        close_p = float(h.get("closeAvgPrice") or 0.0)
                        if close_p > 0:
                            exit_price = close_p
                        pnl = float(h.get("closeProfitLoss", 0.0))
                        if pnl != 0:
                            realized_pnl = pnl
                        if pnl > 0:
                            exit_reason = ExitReason.MIN_PROFIT_TP_HIT
                        elif pnl < 0:
                            exit_reason = ExitReason.STOP_LOSS_HIT
                        break
            except Exception as e:
                self.logger.debug("Error inspecting history_positions: %s", e)

        # 3. Final mathematical fallback check
        if exit_reason in (None, ExitReason.UNKNOWN):
            if direction == OrderDirection.LONG:
                if exit_price >= entry_price + (0.5 * pu):
                    exit_reason = ExitReason.MIN_PROFIT_TP_HIT
                elif exit_price < entry_price - (0.5 * pu):
                    exit_reason = ExitReason.STOP_LOSS_HIT
                else:
                    exit_reason = ExitReason.SCRATCH_CLOSE
            else:
                if exit_price <= entry_price - (0.5 * pu):
                    exit_reason = ExitReason.MIN_PROFIT_TP_HIT
                elif exit_price > entry_price + (0.5 * pu):
                    exit_reason = ExitReason.STOP_LOSS_HIT
                else:
                    exit_reason = ExitReason.SCRATCH_CLOSE

        return exit_price, realized_pnl, exit_reason, close_order_id, reconciled_pos_id

    def _setup_and_verify_position_orders(
        self,
        symbol: str,
        position_id: Optional[int],
        direction: OrderDirection,
        vol_contracts: int,
        leverage: int,
        exact_tp: float,
        exact_sl: float,
        precision: int,
        entry_price: Optional[float],
        signal: Optional[TradeSignal],
        is_smc_sig: bool,
        max_attempts: int = 4
    ) -> tuple[Optional[str], int]:
        """
        Registers and verifies server-side TP/SL stop orders and pre-placed 1:1 limit orders.
        Checks 2-3 or more times (up to max_attempts) to ensure orders are confirmed on KCEX.
        """
        if not position_id:
            return None, 1

        pu = 10 ** -precision
        is_long = (direction == OrderDirection.LONG)

        # 1. Register & Verify Server-Side Position TP/SL (checking 2-3+ times)
        tp_sl_verified = False
        for attempt in range(1, max_attempts + 1):
            try:
                self.trader.set_position_tp_sl(
                    symbol=symbol,
                    position_id=position_id,
                    take_profit_price=exact_tp,
                    stop_loss_price=exact_sl
                )
                self.logger.info(
                    f"📤 [{symbol}] Position TP/SL submitted to KCEX (Attempt {attempt}/{max_attempts}): "
                    f"TP {exact_tp:.{precision}f}, SL {exact_sl:.{precision}f}"
                )
            except Exception as e:
                self.logger.warning(f"⚠️ [{symbol}] Attempt {attempt}/{max_attempts} submitting TP/SL: {e}")

            time.sleep(0.5)

            try:
                open_stops = self.trader.get_open_stop_orders()
                for s in open_stops:
                    pos_id_match = s.get("positionId") and int(s.get("positionId")) == int(position_id)
                    sym_match = s.get("symbol") == symbol.upper()
                    if pos_id_match or sym_match:
                        tp_p = float(s.get("takeProfitPrice") or 0.0)
                        sl_p = float(s.get("stopLossPrice") or 0.0)
                        tp_match = (tp_p > 0 and abs(tp_p - exact_tp) <= (2 * pu))
                        sl_match = (sl_p > 0 and abs(sl_p - exact_sl) <= (2 * pu))
                        if tp_match or sl_match:
                            tp_sl_verified = True
                            self.logger.info(
                                f"✅ [{symbol}] Server-side TP/SL VERIFIED on KCEX (Attempt {attempt}/{max_attempts})! "
                                f"TP: {exact_tp:.{precision}f} | SL: {exact_sl:.{precision}f}"
                            )
                            break
                if tp_sl_verified:
                    break
            except Exception as ce:
                self.logger.debug(f"[{symbol}] Error querying open stop orders: {ce}")

            if not tp_sl_verified:
                self.logger.warning(f"⚠️ [{symbol}] Position TP/SL not yet confirmed on KCEX (Attempt {attempt}/{max_attempts}). Retrying...")

        if not tp_sl_verified:
            self.logger.warning(f"⚠️ [{symbol}] Could not verify server-side TP/SL after {max_attempts} checks. Software monitor will act as active fallback.")

        # 2. Pre-place & Verify 1:1 Partial Close Limit Order for SMC trades
        pre_placed_tp1_order_id = None
        tp1_contracts = (vol_contracts // 2) if vol_contracts >= 2 else 1
        pct_label = "50%" if vol_contracts >= 2 else "100% (min 1 contract)"

        if is_smc_sig and signal:
            if signal.metadata and (not signal.metadata.get("target_1to1_price")) and exact_sl:
                ref_entry = entry_price if (entry_price and entry_price > 0) else float(signal.price or 0.0)
                if ref_entry > 0:
                    risk_dist = abs(ref_entry - exact_sl)
                    t1_calc = ref_entry + risk_dist if is_long else ref_entry - risk_dist
                    signal.metadata["target_1to1_price"] = round(t1_calc, precision)
            target_1to1 = float((signal.metadata or {}).get("target_1to1_price", 0.0))
            partial_tp_enabled = bool((signal.metadata or {}).get("partial_tp_enabled", getattr(self.config, "partial_tp_enabled", True)))

            if partial_tp_enabled and target_1to1 > 0:
                limit_order_verified = False
                for attempt in range(1, max_attempts + 1):
                    if pre_placed_tp1_order_id is None:
                        try:
                            self.logger.info(
                                f"📋 [{symbol}] Pre-placing 1:1 TP Limit Order on KCEX for {tp1_contracts} contract(s) ({pct_label}) "
                                f"at {target_1to1:.{precision}f} USDT (Attempt {attempt}/{max_attempts})..."
                            )
                            close_res = self.trader.close_position_limit(
                                symbol=symbol,
                                side=direction.value,
                                price=target_1to1,
                                vol_contracts=tp1_contracts,
                                position_id=position_id,
                                leverage=leverage,
                                is_isolated=self.config.is_isolated
                            )
                            pre_placed_tp1_order_id = str((close_res.get("data") or {}).get("orderId") or "")
                        except Exception as e:
                            self.logger.warning(f"[{symbol}] Attempt {attempt}/{max_attempts} placing 1:1 limit order: {e}")

                    time.sleep(0.6)
                    if pre_placed_tp1_order_id:
                        try:
                            open_orders = self.trader.get_open_orders()
                            for o in open_orders:
                                oid = str(o.get("orderId") or "")
                                if oid == pre_placed_tp1_order_id or (o.get("symbol") == symbol.upper() and abs(float(o.get("price") or 0.0) - target_1to1) <= (2 * pu)):
                                    limit_order_verified = True
                                    self.logger.info(
                                        f"✅ [{symbol}] 1:1 TP Limit Order #{pre_placed_tp1_order_id} VERIFIED on KCEX book! "
                                        f"Vol: {tp1_contracts} contract(s) ({pct_label}) @ {target_1to1:.{precision}f} USDT"
                                    )
                                    break
                            if limit_order_verified:
                                break
                        except Exception as ce:
                            self.logger.debug(f"[{symbol}] Error querying open orders: {ce}")

                    if not limit_order_verified:
                        self.logger.warning(f"⚠️ [{symbol}] 1:1 Limit order not yet verified on book (Attempt {attempt}/{max_attempts}). Retrying...")

                if not limit_order_verified and pre_placed_tp1_order_id is None:
                    self.logger.warning(f"⚠️ [{symbol}] Could not pre-place 1:1 limit order. Active software monitor will execute 1:1 exit at market.")

        return pre_placed_tp1_order_id, tp1_contracts

    def _update_and_verify_be_sl(
        self,
        symbol: str,
        position_id: int,
        exact_tp: float,
        be_sl: float,
        precision: int,
        max_attempts: int = 3
    ) -> bool:
        """Checks 2-3 times until server-side breakeven SL is confirmed updated on KCEX."""
        pu = 10 ** -precision
        for attempt in range(1, max_attempts + 1):
            try:
                self.trader.set_position_tp_sl(
                    symbol=symbol,
                    position_id=position_id,
                    take_profit_price=exact_tp,
                    stop_loss_price=be_sl
                )
            except Exception as e:
                self.logger.debug(f"[{symbol}] Attempt {attempt} updating BE SL: {e}")

            time.sleep(0.5)
            try:
                open_stops = self.trader.get_open_stop_orders()
                for s in open_stops:
                    pos_id_match = s.get("positionId") and int(s.get("positionId")) == int(position_id)
                    sym_match = s.get("symbol") == symbol.upper()
                    if pos_id_match or sym_match:
                        sl_p = float(s.get("stopLossPrice") or 0.0)
                        if abs(sl_p - be_sl) <= (2 * pu):
                            self.logger.info(
                                f"✅ [{symbol}] Breakeven SL VERIFIED on KCEX (Attempt {attempt}/{max_attempts}): SL {be_sl:.{precision}f} USDT"
                            )
                            return True
            except Exception:
                pass
            time.sleep(0.5)

        self.logger.warning(f"⚠️ [{symbol}] Could not confirm server-side BE SL on KCEX after {max_attempts} attempts. Software monitor will guard breakeven.")
        return False

    def _monitor_live_position(
        self,
        symbol: str,
        position_id: Optional[int],
        direction: OrderDirection,
        vol_contracts: int,
        leverage: int,
        exact_tp: float,
        exact_sl: float,
        precision: int = 4,
        entry_price: Optional[float] = None,
        open_time: Optional[float] = None,
        signal: Optional[TradeSignal] = None,
        pre_placed_tp1_order_id: Optional[str] = None,
        tp1_contracts: int = 1
    ) -> tuple[float, ExitReason, Optional[str]]:
        """
        Polls ticker and open positions until the position closes.
        Prioritizes server-side TP/SL stoporder execution, using executable bid/ask
        for immediate local profit close safeguards.
        """
        side_str = "LONG" if direction == OrderDirection.LONG else "SHORT"
        close_order_id = None
        last_seen_price = exact_tp
        exec_price = exact_tp
        initial_sl = exact_sl
        deep_alert_logged = False
        monitor_start_time = open_time if open_time is not None else time.time()
        last_heartbeat_time = 0.0

        # SMC 1:1 Partial TP and Breakeven Runner state
        is_smc = bool(
            (signal and signal.metadata and "ORDER_BLOCK" in str(signal.metadata.get("strategy_mode", "")).upper())
            or ("OrderBlock" in str(getattr(signal, "sub_strategy_name", "")))
            or (getattr(self.config, "strategy_mode", "").upper() in ("ORDER_BLOCK_DEMAND", "ORDER_BOOK_DEMAND", "ORDER_BLOCK", "DEMAND_BLOCK", "SMC"))
        )
        target_1to1 = float((signal.metadata or {}).get("target_1to1_price", 0.0)) if (is_smc and signal and signal.metadata) else None
        if is_smc and (target_1to1 is None or target_1to1 == 0.0) and entry_price and exact_sl:
            risk_dist = abs(entry_price - exact_sl)
            target_1to1 = round(entry_price + risk_dist if direction == OrderDirection.LONG else entry_price - risk_dist, precision)
        partial_tp_enabled = bool((signal.metadata or {}).get("partial_tp_enabled", getattr(self.config, "partial_tp_enabled", True))) if (is_smc and signal and signal.metadata) else getattr(self.config, "partial_tp_enabled", True)
        be_buf_ticks = int((signal.metadata or {}).get("breakeven_buffer_ticks", getattr(self.config, "breakeven_buffer_ticks", 1))) if (is_smc and signal and signal.metadata) else getattr(self.config, "breakeven_buffer_ticks", 1)
        smc_1x_mode = str(getattr(self.config, "smc_1x_exit_mode", "1TO1_TP")).upper()
        partial_tp_executed = False
        remaining_vol = vol_contracts

        while not self._shutdown_requested:
            time.sleep(self.config.poll_interval_seconds)

            # 1. Fetch latest price & executable bid/ask prices
            try:
                ticker = self.market.get_ticker(symbol)
                current_price = float(ticker.get("lastPrice") or ticker.get("fairPrice", 0.0))
                bid1 = float(ticker.get("bid1", 0.0))
                ask1 = float(ticker.get("ask1", 0.0))
                last_seen_price = current_price
            except Exception as e:
                self.logger.debug("Ticker poll error: %s", e)
                continue

            # 2. Check if position closed via server-side attached TP or SL first
            try:
                open_pos = self.trader.get_open_positions(symbol)
                pos_still_open = False
                current_hold_vol = 0
                for p in open_pos:
                    if position_id and int(p.get("positionId", 0)) == int(position_id):
                        current_hold_vol = int(p.get("holdVol", 0))
                        if current_hold_vol > 0:
                            pos_still_open = True
                            break
                    elif not position_id and float(p.get("holdVol", 0)) > 0:
                        p_type = p.get("positionType")
                        p_side = p.get("side")
                        is_pos_long = (p_type == 1 or str(p_side).upper() in ("1", "LONG", "BUY"))
                        if (direction == OrderDirection.LONG and is_pos_long) or (direction == OrderDirection.SHORT and not is_pos_long):
                            current_hold_vol = int(p.get("holdVol", 0))
                            pos_still_open = True
                            break

                if not pos_still_open:
                    self.logger.info("Position closed on KCEX. Reconciling fill records...")
                    if pre_placed_tp1_order_id and not partial_tp_executed:
                        try:
                            self.trader.cancel_order(pre_placed_tp1_order_id)
                        except Exception:
                            pass
                    if vol_contracts == 1 and pre_placed_tp1_order_id and target_1to1:
                        pu = (10 ** -precision)
                        reached_1to1 = (last_seen_price >= target_1to1 - (0.5 * pu)) if direction == OrderDirection.LONG else (last_seen_price <= target_1to1 + (0.5 * pu))
                        if reached_1to1:
                            return target_1to1, ExitReason.MIN_PROFIT_TP_HIT, pre_placed_tp1_order_id
                        elif (last_seen_price <= exact_sl + (0.5 * pu) if direction == OrderDirection.LONG else last_seen_price >= exact_sl - (0.5 * pu)):
                            return exact_sl, ExitReason.STOP_LOSS_HIT, None
                    return last_seen_price, ExitReason.UNKNOWN, None
            except Exception as e:
                self.logger.debug("Position check error: %s", e)

            # -----------------------------------------------------------------
            # Phase V2.2 Champion Micro-Excursion Tick Ratchet
            # -----------------------------------------------------------------
            if getattr(self.config, "ratchet_enabled", False) and entry_price is not None:
                pu = (10 ** -precision)
                favorable_ticks = (current_price - entry_price) / pu if direction == OrderDirection.LONG else (entry_price - current_price) / pu
                elapsed_hold = time.time() - monitor_start_time

                # Tier 1: Stalled >= 10s at >= +1.0 tick -> Tighten SL to -1.0 tick
                t1_ticks = float(getattr(self.config, "ratchet_trigger_ticks", 1.0))
                t1_stall = float(getattr(self.config, "ratchet_stall_seconds", 10.0))
                tight_ticks = float(getattr(self.config, "ratchet_tighten_ticks", 1.0))
                if favorable_ticks >= t1_ticks and elapsed_hold >= t1_stall:
                    new_sl = entry_price - (tight_ticks * pu) if direction == OrderDirection.LONG else entry_price + (tight_ticks * pu)
                    if (direction == OrderDirection.LONG and new_sl > exact_sl) or (direction == OrderDirection.SHORT and new_sl < exact_sl):
                        exact_sl = round(new_sl, precision)
                        self.logger.info(
                            f"⚙️ [TICK RATCHET TIER 1] Excursion >= +{t1_ticks:g}t stalled >= {t1_stall:.0f}s. "
                            f"Stop tightened to -{tight_ticks:g}t ({exact_sl:.{precision}f} USDT)."
                        )

                # Tier 2: Favorable excursion >= +2.5 ticks -> Lock at Breakeven (0.0 ticks)
                t2_ticks = float(getattr(self.config, "ratchet_breakeven_ticks", 2.5))
                if favorable_ticks >= t2_ticks:
                    be_sl = round(entry_price, precision)
                    if (direction == OrderDirection.LONG and be_sl > exact_sl) or (direction == OrderDirection.SHORT and be_sl < exact_sl):
                        exact_sl = be_sl
                        self.logger.info(
                            f"🔒 [TICK RATCHET TIER 2] Excursion reached >= +{t2_ticks:g}t. "
                            f"Stop locked at BREAKEVEN 0.0t ({exact_sl:.{precision}f} USDT). Position is risk-free."
                        )

            # Executable price (bid for LONG, ask for SHORT)
            exec_price = (bid1 if bid1 > 0 else current_price) if direction == OrderDirection.LONG else (ask1 if ask1 > 0 else current_price)

            # Periodic Real-Time Position Telemetry (single-line dynamic updates)
            now = time.time()
            if (now - last_heartbeat_time) >= 1.0:
                last_heartbeat_time = now
                pu = (10 ** -precision)
                dist_tp_ticks = (exact_tp - exec_price) / pu if direction == OrderDirection.LONG else (exec_price - exact_tp) / pu
                dist_sl_ticks = (exec_price - exact_sl) / pu if direction == OrderDirection.LONG else (exact_sl - exec_price) / pu
                if entry_price and entry_price > 0:
                    u_diff = (exec_price - entry_price) if direction == OrderDirection.LONG else (entry_price - exec_price)
                    u_ticks = u_diff / pu
                    u_roe = (u_diff / entry_price) * leverage * 100.0
                else:
                    u_ticks = 0.0
                    u_roe = 0.0
                elapsed_hold = now - monitor_start_time
                status_msg = (
                    f"[LIVE POSITION] {direction.value} @ {entry_price:.{precision}f} | Mark: {exec_price:.{precision}f} | "
                    f"TP: {exact_tp:.{precision}f} ({dist_tp_ticks:+.1f}t) | SL: {exact_sl:.{precision}f} ({dist_sl_ticks:+.1f}t) | "
                    f"Unrealized: {u_ticks:+.1f}t ({u_roe:+.2f}% ROE) | Hold: {elapsed_hold:.1f}s"
                )
                self.logger.update_status_line(status_msg, price=exec_price, tag="LIVE_POS")

            # -----------------------------------------------------------------
            # Smart Money Concepts: 1:1 Partial Take Profit & Breakeven Lock
            # -----------------------------------------------------------------
            if is_smc and partial_tp_enabled and target_1to1 and not partial_tp_executed and entry_price:
                # 1:1 is filled if:
                # (A) Exchange holdVol has decreased by tp1_contracts (pre-placed limit order executed on book)
                # (B) Executable price touched or surpassed target_1to1
                limit_filled = bool(pos_still_open and current_hold_vol > 0 and current_hold_vol <= (vol_contracts - tp1_contracts))
                price_hit_1to1 = (exec_price >= target_1to1) if direction == OrderDirection.LONG else (exec_price <= target_1to1)

                if limit_filled or price_hit_1to1:
                    partial_tp_executed = True
                    if signal and signal.metadata:
                        signal.metadata["partial_tp_hit"] = True

                    pu = (10 ** -precision)
                    if vol_contracts >= 2:
                        remaining_vol = current_hold_vol if (pos_still_open and current_hold_vol > 0) else (vol_contracts - tp1_contracts)
                        if limit_filled:
                            self.logger.info(
                                f"🎉 [SMC 1:1 PRE-PLACED LIMIT TP FILLED] Resting limit order #{pre_placed_tp1_order_id} filled on KCEX book! "
                                f"Closed 50% ({tp1_contracts} contracts) at exact 1:1 target ({target_1to1:.{precision}f} USDT)."
                            )
                        else:
                            self.logger.info(
                                f"🎉 [SMC 1:1 TARGET REACHED] Executable price ({exec_price:.{precision}f} USDT) reached 1:1. "
                                f"Closing 50% ({tp1_contracts} contracts) at market..."
                            )
                            if pre_placed_tp1_order_id:
                                try:
                                    self.trader.cancel_order(pre_placed_tp1_order_id)
                                except Exception:
                                    pass
                            try:
                                self.trader.close_position(
                                    position_id=position_id or 0,
                                    symbol=symbol,
                                    side=side_str,
                                    vol_contracts=tp1_contracts,
                                    leverage=leverage,
                                    is_isolated=self.config.is_isolated,
                                    is_market=True,
                                    price=exec_price
                                )
                            except Exception as e:
                                self.logger.warning("Market close error on 1:1 fallback: %s", e)

                        # Move SL to Breakeven (+buffer ticks in profit)
                        new_be_sl = entry_price + (be_buf_ticks * pu) if direction == OrderDirection.LONG else entry_price - (be_buf_ticks * pu)
                        exact_sl = round(new_be_sl, precision)
                        self.logger.info(
                            f"🔒 [SMC BREAKEVEN SL LOCKED] Stop Loss moved to BREAKEVEN +{be_buf_ticks}t ({exact_sl:.{precision}f} USDT). "
                            f"Remaining {remaining_vol} contract(s) now running risk-free towards 1:2 R:R target ({exact_tp:.{precision}f} USDT)!"
                        )
                        if position_id:
                            self._update_and_verify_be_sl(
                                symbol=symbol,
                                position_id=position_id,
                                exact_tp=exact_tp,
                                be_sl=exact_sl,
                                precision=precision
                            )

                    else:
                        # 1 contract volume: 50% partial closure is not allowed by exchange (min 1 contract), so close 100% at 1:1
                        if limit_filled or not pos_still_open:
                            self.logger.info(
                                f"🎯 [SMC 1-CONTRACT 1:1 TP FILLED] Pre-placed limit order #{pre_placed_tp1_order_id} filled on KCEX! "
                                f"Closed full 1-contract position at 1:1 target ({target_1to1:.{precision}f} USDT) as 50% split is disallowed."
                            )
                            return target_1to1, ExitReason.MIN_PROFIT_TP_HIT, pre_placed_tp1_order_id
                        else:
                            self.logger.info(
                                f"🎯 [SMC 1-CONTRACT 1:1 TP HIT] Price reached 1:1 target ({exec_price:.{precision}f} USDT). "
                                f"Closing full 1-contract position at market (min contract is 1)..."
                            )
                            if pre_placed_tp1_order_id:
                                try:
                                    self.trader.cancel_order(pre_placed_tp1_order_id)
                                except Exception:
                                    pass
                            try:
                                res = self.trader.close_position(
                                    position_id=position_id or 0,
                                    symbol=symbol,
                                    side=side_str,
                                    vol_contracts=1,
                                    leverage=leverage,
                                    is_isolated=self.config.is_isolated,
                                    is_market=True,
                                    price=exec_price
                                )
                                close_order_id = str(res.get("data", {}).get("orderId") or "")
                                return exec_price, ExitReason.MIN_PROFIT_TP_HIT, close_order_id
                            except Exception as e:
                                self.logger.warning("Market close error on 1-contract 1:1 TP: %s", e)

            # 3. Check if executable price reached Min-Profit TP (bid for LONG, ask for SHORT)
            if self.strategy.is_better_than_min_profit(direction, exec_price, exact_tp, entry_price=entry_price):
                op_sym = ">=" if direction == OrderDirection.LONG else "<="
                self.logger.info(
                    f"[IMMEDIATE PROFIT TRIGGER] Executable price ({exec_price:.{precision}f} USDT) reached Min-Profit TP: {op_sym} {exact_tp:.{precision}f} USDT. "
                    f"Executing market close..."
                )
                try:
                    res = self.trader.close_position(
                        position_id=position_id or 0,
                        symbol=symbol,
                        side=side_str,
                        vol_contracts=vol_contracts,
                        leverage=leverage,
                        is_isolated=self.config.is_isolated,
                        is_market=True,
                        price=exec_price
                    )
                    close_order_id = str(res.get("data", {}).get("orderId") or "")
                    return exec_price, ExitReason.MIN_PROFIT_TP_HIT, close_order_id
                except Exception as e:
                    self.logger.warning("Market close error (position may already be closed by TP): %s", e)

            # 3b. Check if dynamic Stop Loss was breached locally
            sl_breached = (exec_price <= exact_sl) if direction == OrderDirection.LONG else (exec_price >= exact_sl)
            if sl_breached:
                # Distinguish between standard SL, ratchet tightened SL, and ratchet breakeven
                pu = (10 ** -precision)
                if partial_tp_executed or abs(exact_sl - entry_price) <= (0.2 * pu):
                    exit_reason = ExitReason.RATCHET_BREAKEVEN_HIT
                    sl_label = "SMC BREAKEVEN SL" if partial_tp_executed else "RATCHET BREAKEVEN"
                elif abs(exact_sl - entry_price) < abs(initial_sl - entry_price):
                    exit_reason = ExitReason.RATCHET_TIGHTEN_HIT
                    sl_label = "RATCHET TIGHTENED SL"
                else:
                    exit_reason = ExitReason.STOP_LOSS_HIT
                    sl_label = "STOP LOSS"

                self.logger.warning(
                    f"[{sl_label} HIT] Price reached stop at {exec_price:.{precision}f} USDT. Executing market close..."
                )
                try:
                    res = self.trader.close_position(
                        position_id=position_id or 0,
                        symbol=symbol,
                        side=side_str,
                        vol_contracts=vol_contracts,
                        leverage=leverage,
                        is_isolated=self.config.is_isolated,
                        is_market=True,
                        price=exec_price
                    )
                    close_order_id = str(res.get("data", {}).get("orderId") or "")
                    return exec_price, exit_reason, close_order_id
                except Exception as e:
                    self.logger.warning("Market close error on SL: %s", e)


            # 4. Duration-Based Monitoring & Time-Decay Exit Actions
            if getattr(self.config, "duration_filter_enabled", False):
                elapsed = time.time() - monitor_start_time
                deep_thresh = float(getattr(self.config, "duration_deep_monitor_seconds", 60.0))
                if elapsed >= deep_thresh and not deep_alert_logged:
                    deep_alert_logged = True
                    self.logger.info(
                        f"[LIVE DURATION IN-DEPTH MONITOR] Position open for {elapsed:.1f}s (threshold {deep_thresh:.0f}s). "
                        f"Close monitoring engaged..."
                    )

                max_hold = float(getattr(self.config, "duration_max_hold_seconds", 90.0))
                if elapsed >= max_hold:
                    action = (getattr(self.config, "duration_action", "CLOSE") or "CLOSE").upper()
                    if action in ("CLOSE", "SCRATCH_OR_MARKET"):
                        exit_reason = ExitReason.TIMEOUT_CLOSE if action == "CLOSE" else ExitReason.DURATION_SCRATCH
                        self.logger.warning(
                            f"[LIVE TIME-STOP TRIGGERED] Position open {elapsed:.1f}s >= max {max_hold:.0f}s. "
                            f"Action: {action}. Executing market close at {exec_price:.{precision}f} USDT..."
                        )
                        try:
                            res = self.trader.close_position(
                                position_id=position_id or 0,
                                symbol=symbol,
                                side=side_str,
                                vol_contracts=vol_contracts,
                                leverage=leverage,
                                is_isolated=self.config.is_isolated,
                                is_market=True,
                                price=exec_price
                            )
                            close_order_id = str(res.get("data", {}).get("orderId") or "")
                            return exec_price, exit_reason, close_order_id
                        except Exception as e:
                            self.logger.warning("Error executing live time-stop close: %s", e)

        # If shutdown was requested during trade, market close immediately
        self.logger.warning("Shutdown received while in position. Closing position...")
        if pre_placed_tp1_order_id and not partial_tp_executed:
            try:
                self.trader.cancel_order(pre_placed_tp1_order_id)
            except Exception:
                pass
        try:
            self.trader.close_position(
                position_id=position_id or 0,
                symbol=symbol,
                side=side_str,
                vol_contracts=vol_contracts,
                leverage=leverage,
                is_isolated=self.config.is_isolated,
                is_market=True
            )
        except Exception:
            pass
        return last_seen_price, ExitReason.MANUAL_CLOSE, None

    # =========================================================================
    # DRY RUN (SIMULATION MODE)
    # =========================================================================

    def _simulate_dry_run_trade(
        self,
        trade_id: int,
        contract: ContractInfo,
        direction: OrderDirection,
        vol_contracts: int,
        leverage: int,
        open_time: float,
        sub_strategy_name: str,
        target_tp_ticks: Optional[int] = None,
        signal: Optional[TradeSignal] = None
    ) -> TradeOutcome:
        """
        High-fidelity DRY-RUN execution simulation using live market ticker data.
        """
        symbol = contract.symbol
        pu = contract.price_unit
        cs = contract.contract_size
        underlying_qty = vol_contracts * cs

        # SMC 1:1 Partial TP and Breakeven Runner state
        is_smc = bool(signal and signal.metadata and "ORDER_BLOCK" in str(signal.metadata.get("strategy_mode", "")).upper())
        target_1to1 = float(signal.metadata.get("target_1to1_price", 0.0)) if is_smc else None
        partial_tp_enabled = bool(signal.metadata.get("partial_tp_enabled", getattr(self.config, "partial_tp_enabled", True))) if is_smc else False
        be_buf_ticks = int(signal.metadata.get("breakeven_buffer_ticks", getattr(self.config, "breakeven_buffer_ticks", 1))) if is_smc else 1
        smc_1x_mode = str(getattr(self.config, "smc_1x_exit_mode", "1TO2_WITH_BE")).upper()
        partial_tp_executed = False
        partial_fill_price = None
        remaining_vol = vol_contracts

        # 1. Realistic Entry Price:
        # Market orders: Long executes against best ask (ask1), Short against best bid (bid1)
        # Limit orders: Long rests at best bid (bid1), Short rests at best ask (ask1)
        ticker = self.market.get_ticker(symbol)
        last_price = float(ticker.get("lastPrice", 0.0) or ticker.get("fairPrice", 1.0))
        ask1 = float(ticker.get("ask1", 0.0) or last_price)
        bid1 = float(ticker.get("bid1", 0.0) or last_price)

        exec_style = getattr(self.config, "execution_style", "PURE_MARKET") or "PURE_MARKET"
        order_type = getattr(self.config, "order_type", "MARKET") or "MARKET"
        is_maker = (str(exec_style).upper() == "MAKER_HYBRID") or (str(order_type).upper() == "LIMIT")

        if is_maker:
            if direction == OrderDirection.LONG:
                entry_price = bid1 if bid1 > 0 else last_price
            else:
                entry_price = ask1 if ask1 > 0 else last_price
        else:
            if direction == OrderDirection.LONG:
                entry_price = ask1 if ask1 > 0 else last_price
            else:
                entry_price = bid1 if bid1 > 0 else last_price

        # Realistic Slippage Engine (Dry-Run Entry)
        # Taker market entries incur adverse spread-crossing slippage
        # Limit maker entries capture exact bid/ask quote with 0 slippage
        if getattr(self.config, "slippage_enabled", False) and getattr(self.config, "slippage_ticks", 0) > 0 and not is_maker:
            slip_delta = self.config.slippage_ticks * pu
            entry_price = entry_price + slip_delta if direction == OrderDirection.LONG else entry_price - slip_delta
            self.logger.info(
                f"[DRY-RUN SLIPPAGE] Applied {self.config.slippage_ticks} tick(s) adverse entry penalty -> Fill: {entry_price:.{contract.price_precision}f} USDT"
            )

        entry_price = round(entry_price, contract.price_precision)

        is_ml_sig = (sub_strategy_name in ("ML_1M_MODEL", "MLStrategy")) or (getattr(self.config, "strategy_mode", "").upper() in ("ML", "ML_1M", "ML_MODEL", "ML_1M_MODEL")) or getattr(self.config, "dynamic_tp", False)
        is_smc_sig = ("ORDER_BLOCK" in str((signal.metadata if signal and signal.metadata else {}).get("strategy_mode", "")).upper()) or ("OrderBlock" in sub_strategy_name) or (getattr(self.config, "strategy_mode", "").upper() in ("ORDER_BLOCK_DEMAND", "ORDER_BOOK_DEMAND", "ORDER_BLOCK", "DEMAND_BLOCK", "SMC"))
        if (is_ml_sig or is_smc_sig) and signal and signal.metadata and "target_ticks" in signal.metadata:
            effective_tp_ticks = int(signal.metadata["target_ticks"])
        elif target_tp_ticks is not None:
            effective_tp_ticks = target_tp_ticks
        else:
            effective_tp_ticks = self.config.tp_ticks

        if (is_ml_sig or is_smc_sig) and signal and signal.metadata and "target_sl_ticks" in signal.metadata:
            effective_sl_ticks = int(signal.metadata["target_sl_ticks"])
            effective_sl_roe = None
        else:
            effective_sl_ticks = self.config.sl_ticks
            effective_sl_roe = self.config.sl_roe_pct

        atr_val = None
        if getattr(self.config, "use_atr_targets", False):
            try:
                c_list = self.market.get_klines(symbol, interval="Min1", limit=30)
                if len(c_list) >= 15:
                    from strategies.filters import compute_atr_series
                    highs = [float(c.high if hasattr(c, "high") else c[2]) for c in c_list]
                    lows = [float(c.low if hasattr(c, "low") else c[3]) for c in c_list]
                    closes = [float(c.close if hasattr(c, "close") else c[4]) for c in c_list]
                    atrs = compute_atr_series(highs, lows, closes, period=14)
                    if atrs and atrs[-1] > 0:
                        atr_val = atrs[-1]
            except Exception:
                pass

        exact_tp = self.strategy.calculate_min_profit_tp(
            direction=direction,
            entry_price=entry_price,
            price_unit=pu,
            tp_ticks=effective_tp_ticks,
            precision=contract.price_precision,
            atr_value=atr_val
        )
        exact_sl = self.strategy.calculate_stop_loss(
            direction=direction,
            entry_price=entry_price,
            leverage=leverage,
            sl_roe_pct=effective_sl_roe,
            sl_ticks=effective_sl_ticks,
            sl_price_pct=self.config.sl_price_pct,
            price_unit=pu,
            precision=contract.price_precision,
            atr_value=atr_val
        )
        initial_sl = exact_sl

        sl_desc = (
            f"-{effective_sl_ticks} ticks" if effective_sl_ticks
            else f"-{self.config.sl_price_pct}% price" if self.config.sl_price_pct
            else f"-{self.config.sl_roe_pct}% ROE"
        )

        ps = contract.price_precision
        base_coin = contract.base_coin or symbol.split('_')[0]

        exec_desc = "MAKER LIMIT QUEUE (0 Slippage Target)" if is_maker else "MARKET TAKER"
        if is_smc and signal and signal.metadata:
            z_type = signal.metadata.get("zone_type", "ORDER_BLOCK")
            z_id = signal.metadata.get("zone_id", "N/A")
            z_low = float(signal.metadata.get("zone_low", 0.0) or 0.0)
            z_high = float(signal.metadata.get("zone_high", 0.0) or 0.0)
            z_mid = float(signal.metadata.get("zone_mid", (z_low + z_high) / 2.0) or 0.0)
            z_bar = signal.metadata.get("zone_creation_bar_idx")
            z_time = signal.metadata.get("zone_creation_time_utc", "N/A")
            bos_bar = signal.metadata.get("bos_bar_idx")
            bos_p = signal.metadata.get("bos_price")
            retest_bar = signal.metadata.get("retest_bar_idx")
            trig_time = signal.metadata.get("trigger_candle_time_utc", "N/A")
            eval_bar = signal.metadata.get("eval_bar_idx")
            t1_p = signal.metadata.get("target_1to1_price", exact_tp)
            t2_p = signal.metadata.get("target_1to2_price", exact_tp)
            tf = signal.metadata.get("timeframe", getattr(self.config, "timeframe", "15m"))

            loc_str = f"Bar #{z_bar} ({z_time})" if z_bar is not None else str(z_time)
            bos_str = f"Bar #{bos_bar} @ {bos_p:.{ps}f} USDT" if (bos_bar is not None and bos_p is not None) else (f"Bar #{bos_bar}" if bos_bar is not None else "N/A")
            retest_str = f" | Retest Bar: #{retest_bar}" if retest_bar is not None else ""
            t1_str = f"{t1_p:.{ps}f} USDT" if t1_p is not None else "N/A"
            t2_str = f"{t2_p:.{ps}f} USDT" if t2_p is not None else "N/A"

            self.logger.info(
                f"\n{'='*78}\n"
                f"🎯 [DRY-RUN ORDER BLOCK TRADE EXECUTED & ACTIVE]\n"
                f"{'='*78}\n"
                f"Pair & Direction   : {symbol} [{direction.value}] | Timeframe: {tf} | Leverage: {leverage}x\n"
                f"Volume Executed    : {vol_contracts} contract(s) ({underlying_qty:g} {contract.base_coin}) [{exec_desc}]\n"
                f"Identified Zone    : {z_type} (#{z_id})\n"
                f"Candle Location    : {loc_str}\n"
                f"Structure Break    : BOS {bos_str}{retest_str}\n"
                f"Trigger Candle     : Bar #{eval_bar} ({trig_time})\n"
                f"Zone Boundaries    : Low: {z_low:.{ps}f} <---> Mid (50%): {z_mid:.{ps}f} <---> High: {z_high:.{ps}f} USDT\n"
                f"Execution Levels   :\n"
                f"  • Entry Fill     : {entry_price:.{ps}f} USDT\n"
                f"  • Stop Loss      : {exact_sl:.{ps}f} USDT ({sl_desc})\n"
                f"  • 1:1 TP Target  : {t1_str} (+{effective_tp_ticks // 2}t | 50% Partial Close + BE Lock)\n"
                f"  • 1:2 TP Runner  : {t2_str} (+{effective_tp_ticks}t | Full Runner Exit)\n"
                f"{'='*78}"
            )
        else:
            self.logger.info(
                f"[DRY-RUN] Simulated Order Filled ({exec_desc}): Entry = {entry_price:.{ps}f} USDT | "
                f"Min-Profit TP = {exact_tp:.{ps}f} USDT (+{effective_tp_ticks} pu) | SL = {exact_sl:.{ps}f} USDT ({sl_desc})"
            )

        # 2. Check immediate profit condition at fill
        if self.strategy.is_better_than_min_profit(direction, entry_price, exact_tp):
            op_sym = ">=" if direction == OrderDirection.LONG else "<="
            self.logger.info(
                f"[DRY-RUN] Immediate profit condition met at fill: {entry_price:.{ps}f} {op_sym} TP {exact_tp:.{ps}f}"
            )
            exit_price = exact_tp
            exit_reason = ExitReason.IMMEDIATE_PROFIT_CLOSE
        else:
            # 3. Active Real-Time Market Monitoring Loop
            self.logger.info(
                f"[DRY-RUN] Actively monitoring live market prices for TP ({exact_tp:.{ps}f}) or SL ({exact_sl:.{ps}f})..."
            )
            exit_price = entry_price
            exit_reason = ExitReason.UNKNOWN
            poll_count = 0
            deep_alert_logged = False

            while not self._shutdown_requested:
                time.sleep(self.config.poll_interval_seconds)
                poll_count += 1

                try:
                    cur_ticker = self.market.get_ticker(symbol)
                    cur_last = float(cur_ticker.get("lastPrice", 0.0) or cur_ticker.get("fairPrice", 0.0))
                    cur_bid = float(cur_ticker.get("bid1", 0.0) or cur_last)
                    cur_ask = float(cur_ticker.get("ask1", 0.0) or cur_last)
                except Exception as e:
                    self.logger.debug("Dry-run ticker fetch error: %s", e)
                    continue

                # -----------------------------------------------------------------
                # Phase V2.2 Champion Micro-Excursion Tick Ratchet (Dry-Run Mode)
                # -----------------------------------------------------------------
                if getattr(self.config, "ratchet_enabled", False):
                    favorable_ticks = (cur_last - entry_price) / pu if direction == OrderDirection.LONG else (entry_price - cur_last) / pu
                    elapsed_hold = time.time() - open_time

                    # Tier 1: Stalled >= 10s at >= +1.0 tick -> Tighten SL to -1.0 tick
                    t1_trig = float(getattr(self.config, "ratchet_trigger_ticks", 1.0))
                    t1_stall = float(getattr(self.config, "ratchet_stall_seconds", 10.0))
                    t1_tight = float(getattr(self.config, "ratchet_tighten_ticks", 1.0))
                    if favorable_ticks >= t1_trig and elapsed_hold >= t1_stall:
                        new_sl = entry_price - (t1_tight * pu) if direction == OrderDirection.LONG else entry_price + (t1_tight * pu)
                        if (direction == OrderDirection.LONG and new_sl > exact_sl) or (direction == OrderDirection.SHORT and new_sl < exact_sl):
                            exact_sl = round(new_sl, ps)
                            self.logger.info(
                                f"⚙️ [DRY-RUN TICK RATCHET TIER 1] Excursion >= +{t1_trig:g}t stalled >= {t1_stall:.0f}s. "
                                f"Stop tightened to -{t1_tight:g}t ({exact_sl:.{ps}f} USDT)."
                            )

                    # Tier 2: Favorable excursion >= +2.5 ticks -> Lock SL at Breakeven (0.0 ticks)
                    t2_trig = float(getattr(self.config, "ratchet_breakeven_ticks", 2.5))
                    if favorable_ticks >= t2_trig:
                        be_sl = round(entry_price, ps)
                        if (direction == OrderDirection.LONG and be_sl > exact_sl) or (direction == OrderDirection.SHORT and be_sl < exact_sl):
                            exact_sl = be_sl
                            self.logger.info(
                                f"🔒 [DRY-RUN TICK RATCHET TIER 2] Excursion reached >= +{t2_trig:g}t. "
                                f"Stop locked at BREAKEVEN 0.0t ({exact_sl:.{ps}f} USDT). Position is risk-free."
                            )

                # Intra-tick / Intra-poll 75x Maintenance Margin Liquidation Barrier
                if getattr(self.config, "simulate_intra_tick_liquidation", True) and leverage > 0:
                    mmr = float(contract.maintenance_margin_ratio or 0.01)
                    if direction == OrderDirection.LONG:
                        approx_liq = entry_price * (1.0 - (1.0 / float(leverage)) + mmr)
                        if cur_last <= approx_liq or cur_bid <= approx_liq:
                            exit_price = approx_liq
                            exit_reason = ExitReason.LIQUIDATION_HIT
                            self.logger.warning(
                                f"💥 [DRY-RUN 75X LIQUIDATION] Adverse price move breached MMR barrier ({approx_liq:.{ps}f} USDT). "
                                f"Position Liquidated (-100% Margin Loss)!"
                            )
                            break
                    else:
                        approx_liq = entry_price * (1.0 + (1.0 / float(leverage)) - mmr)
                        if cur_last >= approx_liq or cur_ask >= approx_liq:
                            exit_price = approx_liq
                            exit_reason = ExitReason.LIQUIDATION_HIT
                            self.logger.warning(
                                f"💥 [DRY-RUN 75X LIQUIDATION] Adverse price move breached MMR barrier ({approx_liq:.{ps}f} USDT). "
                                f"Position Liquidated (-100% Margin Loss)!"
                            )
                            break

                # -----------------------------------------------------------------
                # Smart Money Concepts: 1:1 Partial Take Profit & Breakeven Lock (Dry-Run)
                # -----------------------------------------------------------------
                if is_smc and partial_tp_enabled and target_1to1 and not partial_tp_executed:
                    hit_1to1 = (cur_bid >= target_1to1 or cur_last >= target_1to1) if direction == OrderDirection.LONG else (cur_ask <= target_1to1 or cur_last <= target_1to1)
                    if hit_1to1:
                        if remaining_vol >= 2:
                            close_vol = remaining_vol // 2
                            partial_fill_price = target_1to1
                            partial_tp_executed = True
                            if signal and signal.metadata:
                                signal.metadata["partial_tp_hit"] = True
                            new_be_sl = entry_price + (be_buf_ticks * pu) if direction == OrderDirection.LONG else entry_price - (be_buf_ticks * pu)
                            exact_sl = round(new_be_sl, ps)
                            remaining_vol -= close_vol
                            self.logger.info(
                                f"🎉 [DRY-RUN SMC 1:1 PARTIAL TP] Reached 1:1 target ({target_1to1:.{ps}f} USDT). "
                                f"Closed 50% ({close_vol} contracts). Stop Loss locked at BREAKEVEN +{be_buf_ticks}t ({exact_sl:.{ps}f} USDT). "
                                f"Remaining {remaining_vol} contract(s) running to 1:2 TP ({exact_tp:.{ps}f} USDT)!"
                            )
                        else:
                            if smc_1x_mode == "1TO1_TP":
                                exit_price = target_1to1
                                exit_reason = ExitReason.MIN_PROFIT_TP_HIT
                                self.logger.info(
                                    f"🎯 [DRY-RUN SMC 1X 1:1 TP] Reached 1:1 target ({target_1to1:.{ps}f} USDT). "
                                    f"Closed single-contract position at 1:1 TP!"
                                )
                                break
                            else:  # 1TO2_WITH_BE
                                partial_tp_executed = True
                                if signal and signal.metadata:
                                    signal.metadata["partial_tp_hit"] = True
                                new_be_sl = entry_price + (be_buf_ticks * pu) if direction == OrderDirection.LONG else entry_price - (be_buf_ticks * pu)
                                exact_sl = round(new_be_sl, ps)
                                self.logger.info(
                                    f"🔒 [DRY-RUN SMC 1-CONTRACT BE LOCK] Reached 1:1 target ({target_1to1:.{ps}f} USDT). "
                                    f"Stop Loss locked at BREAKEVEN +{be_buf_ticks}t ({exact_sl:.{ps}f} USDT). "
                                    f"Position is risk-free, targeting 1:2 R:R ({exact_tp:.{ps}f} USDT)!"
                                )

                # For LONG: Close fills by selling at best bid (bid1) or last trade
                if direction == OrderDirection.LONG:
                    effective_close_price = cur_bid
                    # TP check (Resting Maker Limit TP fills at exact target)
                    if effective_close_price >= exact_tp or cur_last >= exact_tp:
                        exit_price = exact_tp
                        exit_reason = ExitReason.MIN_PROFIT_TP_HIT
                        self.logger.info(
                            f"[DRY-RUN TARGET HIT] Market reached TP! Exit: {exit_price:.{ps}f} USDT (Market Bid: {cur_bid:.{ps}f}, Last: {cur_last:.{ps}f})"
                        )
                        break
                    # SL check
                    elif effective_close_price <= exact_sl or cur_last <= exact_sl:
                        exit_price = exact_sl
                        # Realistic Slippage on Market Stop-Loss Exits
                        if getattr(self.config, "slippage_enabled", False) and getattr(self.config, "slippage_ticks", 0) > 0:
                            slip_delta = self.config.slippage_ticks * pu
                            exit_price = exact_sl - slip_delta
                            exit_price = round(exit_price, ps)
                            self.logger.info(
                                f"[DRY-RUN SLIPPAGE] Applied {self.config.slippage_ticks} tick(s) adverse stop exit penalty -> Exit: {exit_price:.{ps}f} USDT"
                            )

                        if abs(exact_sl - entry_price) <= (0.2 * pu):
                            exit_reason = ExitReason.RATCHET_BREAKEVEN_HIT
                            label = "RATCHET BREAKEVEN"
                        elif abs(exact_sl - entry_price) < abs(initial_sl - entry_price):
                            exit_reason = ExitReason.RATCHET_TIGHTEN_HIT
                            label = "RATCHET TIGHTENED SL"
                        else:
                            exit_reason = ExitReason.STOP_LOSS_HIT
                            label = "STOP LOSS"

                        self.logger.info(
                            f"[DRY-RUN {label} HIT] Market reached SL! Exit: {exit_price:.{ps}f} USDT"
                        )
                        break

                # For SHORT: Close fills by buying back at best ask (ask1) or last trade
                else:
                    effective_close_price = cur_ask
                    # TP check (Resting Maker Limit TP fills at exact target)
                    if effective_close_price <= exact_tp or cur_last <= exact_tp:
                        exit_price = exact_tp
                        exit_reason = ExitReason.MIN_PROFIT_TP_HIT
                        self.logger.info(
                            f"[DRY-RUN TARGET HIT] Market reached TP! Exit: {exit_price:.{ps}f} USDT (Market Ask: {cur_ask:.{ps}f}, Last: {cur_last:.{ps}f})"
                        )
                        break
                    # SL check
                    elif effective_close_price >= exact_sl or cur_last >= exact_sl:
                        exit_price = exact_sl
                        # Realistic Slippage on Market Stop-Loss Exits
                        if getattr(self.config, "slippage_enabled", False) and getattr(self.config, "slippage_ticks", 0) > 0:
                            slip_delta = self.config.slippage_ticks * pu
                            exit_price = exact_sl + slip_delta
                            exit_price = round(exit_price, ps)
                            self.logger.info(
                                f"[DRY-RUN SLIPPAGE] Applied {self.config.slippage_ticks} tick(s) adverse stop exit penalty -> Exit: {exit_price:.{ps}f} USDT"
                            )

                        if abs(exact_sl - entry_price) <= (0.2 * pu):
                            exit_reason = ExitReason.RATCHET_BREAKEVEN_HIT
                            label = "RATCHET BREAKEVEN"
                        elif abs(exact_sl - entry_price) < abs(initial_sl - entry_price):
                            exit_reason = ExitReason.RATCHET_TIGHTEN_HIT
                            label = "RATCHET TIGHTENED SL"
                        else:
                            exit_reason = ExitReason.STOP_LOSS_HIT
                            label = "STOP LOSS"

                        self.logger.info(
                            f"[DRY-RUN {label} HIT] Market reached SL! Exit: {exit_price:.{ps}f} USDT"
                        )
                        break

                # Duration Monitoring & Time-Decay Safeguards
                if getattr(self.config, "duration_filter_enabled", False):
                    elapsed = time.time() - open_time
                    deep_thresh = float(getattr(self.config, "duration_deep_monitor_seconds", 60.0))
                    if elapsed >= deep_thresh and not deep_alert_logged:
                        deep_alert_logged = True
                        self.logger.info(
                            f"[DRY-RUN DURATION IN-DEPTH MONITOR] Trade has been open for {elapsed:.1f}s "
                            f"(threshold: {deep_thresh:.0f}s). Heightened monitoring active."
                        )

                    max_hold = float(getattr(self.config, "duration_max_hold_seconds", 90.0))
                    if elapsed >= max_hold:
                        action = (getattr(self.config, "duration_action", "CLOSE") or "CLOSE").upper()
                        if action == "CLOSE":
                            exit_price = effective_close_price
                            exit_reason = ExitReason.TIMEOUT_CLOSE
                            self.logger.warning(
                                f"[DRY-RUN TIME-STOP] Trade open {elapsed:.1f}s >= max {max_hold:.0f}s. "
                                f"Forced Market Exit at {exit_price:.{ps}f} USDT."
                            )
                            break
                        elif action == "SCRATCH_OR_MARKET":
                            u_diff = (effective_close_price - entry_price) if direction == OrderDirection.LONG else (entry_price - effective_close_price)
                            if u_diff >= -1.0 * pu:
                                exit_price = effective_close_price
                                exit_reason = ExitReason.DURATION_SCRATCH
                                self.logger.warning(
                                    f"[DRY-RUN DURATION SCRATCH] Trade open {elapsed:.1f}s >= max {max_hold:.0f}s. "
                                    f"Price near breakeven ({u_diff / pu:+.1f} pu). Scratching at {exit_price:.{ps}f} USDT."
                                )
                                break
                            else:
                                if direction == OrderDirection.LONG:
                                    exact_sl = max(exact_sl, entry_price)
                                else:
                                    exact_sl = min(exact_sl, entry_price)
                                self.logger.info(
                                    f"[DRY-RUN DURATION TIGHTEN] Trade open {elapsed:.1f}s. SL tightened to entry {exact_sl:.{ps}f} USDT."
                                )
                        elif action == "TIGHTEN_SL":
                            if direction == OrderDirection.LONG:
                                exact_sl = max(exact_sl, entry_price)
                            else:
                                exact_sl = min(exact_sl, entry_price)
                            self.logger.info(
                                f"[DRY-RUN DURATION TIGHTEN] Trade open {elapsed:.1f}s. SL tightened to entry {exact_sl:.{ps}f} USDT."
                            )

                # Periodic status report (single-line dynamic updates)
                poll_interval = max(0.1, self.config.poll_interval_seconds)
                status_freq = int(max(1, 1.0 / poll_interval))
                if poll_count % status_freq == 0:
                    u_diff = (effective_close_price - entry_price) if direction == OrderDirection.LONG else (entry_price - effective_close_price)
                    u_ticks = u_diff / pu if pu > 0 else 0.0
                    u_pnl_usdt = underlying_qty * u_diff
                    inr_rate = self.market.get_inr_rate()
                    u_pnl_inr = u_pnl_usdt * inr_rate
                    notional_u = underlying_qty * entry_price
                    margin_u = notional_u / leverage if leverage > 0 else 1.0
                    u_roe = (u_pnl_usdt / margin_u) * 100.0 if margin_u > 0 else 0.0
                    dist_tp_ticks = (exact_tp - effective_close_price) / pu if direction == OrderDirection.LONG else (effective_close_price - exact_tp) / pu
                    dist_sl_ticks = (effective_close_price - exact_sl) / pu if direction == OrderDirection.LONG else (exact_sl - effective_close_price) / pu
                    elapsed_hold = time.time() - open_time
                    status_msg = (
                        f"[DRY-RUN POSITION] {direction.value} @ {entry_price:.{ps}f} | Mark: {effective_close_price:.{ps}f} | "
                        f"TP: {exact_tp:.{ps}f} ({dist_tp_ticks:+.1f}t) | Stop: {exact_sl:.{ps}f} ({dist_sl_ticks:+.1f}t) | "
                        f"Unrealized: {u_ticks:+.1f}t ({u_pnl_usdt:+.4f} USDT / INR {u_pnl_inr:+.2f} | {u_roe:+.2f}% ROE) | Hold: {elapsed_hold:.1f}s"
                    )
                    self.logger.update_status_line(status_msg, price=effective_close_price, tag="DRY_POS")

            if self._shutdown_requested and exit_reason == ExitReason.UNKNOWN:
                exit_price = last_price
                exit_reason = ExitReason.MANUAL_CLOSE
                self.logger.warning("[DRY-RUN] Manual close requested during trade.")

        self.logger.clear_status_line()

        close_time = time.time()
        duration = max(0.1, close_time - open_time)

        # Blended outcome pricing if 50% was closed at 1:1 TP and runner exited separately
        if partial_tp_executed and partial_fill_price is not None and remaining_vol < vol_contracts:
            closed_partial_vol = vol_contracts - remaining_vol
            blended_exit_price = ((closed_partial_vol * partial_fill_price) + (remaining_vol * exit_price)) / vol_contracts
            exit_price = round(blended_exit_price, ps)

        price_diff = (exit_price - entry_price) if direction == OrderDirection.LONG else (entry_price - exit_price)

        inr_rate = self.market.get_inr_rate()
        notional_usdt = underlying_qty * entry_price
        notional_inr = notional_usdt * inr_rate
        margin_usdt = notional_usdt / leverage
        margin_inr = margin_usdt * inr_rate

        fee_open_rate = contract.maker_fee_rate if is_maker else contract.taker_fee_rate
        is_tp_close = (exit_reason == ExitReason.MIN_PROFIT_TP_HIT and getattr(self.config, "resting_limit_tp", False))
        fee_close_rate = contract.maker_fee_rate if is_tp_close else contract.taker_fee_rate
        fee_open_usdt = notional_usdt * fee_open_rate
        fee_close_usdt = (underlying_qty * exit_price) * fee_close_rate
        fee_total_usdt = fee_open_usdt + fee_close_usdt
        fee_total_inr = fee_total_usdt * inr_rate

        realized_pnl_usdt = (underlying_qty * price_diff) - fee_total_usdt
        realized_pnl_inr = realized_pnl_usdt * inr_rate
        roe_pct = (realized_pnl_usdt / margin_usdt) * 100.0 if margin_usdt > 0 else 0.0
        pnl_pct = (price_diff / entry_price) * 100.0 if entry_price > 0 else 0.0

        # Virtual Simulated Balance Tracking:
        # Tracks realistic balance progression across simulation cycles
        if self.simulated_balance_usdt is None:
            if self.client.config.is_authenticated:
                try:
                    balances = self.trader.get_usdt_balance()
                    self.simulated_balance_usdt = float(balances.get("available_usdt", 10.0))
                except Exception:
                    self.simulated_balance_usdt = 10.0
            else:
                self.simulated_balance_usdt = 10.0

        self.simulated_balance_usdt += realized_pnl_usdt
        balance_after_usdt = self.simulated_balance_usdt
        balance_after_inr = balance_after_usdt * inr_rate
        self.logger.info(
            f"[SIMULATED WALLET] Balance: {balance_after_usdt:.4f} USDT (INR {balance_after_inr:.2f})"
        )

        return TradeOutcome(
            trade_id=trade_id,
            symbol=symbol,
            direction=direction,
            sub_strategy_name=sub_strategy_name,
            mode=EngineMode.DRY_RUN,
            leverage=leverage,
            vol_contracts=vol_contracts,
            contract_size=cs,
            underlying_quantity=underlying_qty,
            base_coin=base_coin,
            entry_price=entry_price,
            exit_price=exit_price,
            min_profit_tp_price=exact_tp,
            stop_loss_price=exact_sl,
            price_unit=pu,
            price_precision=ps,
            open_time=open_time,
            close_time=close_time,
            duration_seconds=duration,
            notional_value_usdt=notional_usdt,
            notional_value_inr=notional_inr,
            margin_used_usdt=margin_usdt,
            margin_used_inr=margin_inr,
            realized_pnl_usdt=realized_pnl_usdt,
            realized_pnl_inr=realized_pnl_inr,
            pnl_percentage=pnl_pct,
            roe_percentage=roe_pct,
            fee_open_usdt=fee_open_usdt,
            fee_close_usdt=fee_close_usdt,
            fee_total_usdt=fee_total_usdt,
            fee_total_inr=fee_total_inr,
            inr_rate=inr_rate,
            exit_reason=exit_reason,
            balance_after_trade_usdt=balance_after_usdt,
            balance_after_trade_inr=balance_after_inr,
            order_id="SIMULATED_ORDER_001",
            close_order_id="SIMULATED_CLOSE_001",
            position_id=12345678,
            smc_zone_id=signal.metadata.get("zone_id") if is_smc and signal and signal.metadata else None,
            smc_zone_type=signal.metadata.get("zone_type") if is_smc and signal and signal.metadata else None,
            smc_zone_high=signal.metadata.get("zone_high") if is_smc and signal and signal.metadata else None,
            smc_zone_low=signal.metadata.get("zone_low") if is_smc and signal and signal.metadata else None,
            smc_zone_mid=signal.metadata.get("zone_mid") if is_smc and signal and signal.metadata else None,
            smc_zone_creation_bar_idx=signal.metadata.get("zone_creation_bar_idx") if is_smc and signal and signal.metadata else None,
            smc_zone_creation_ts=signal.metadata.get("zone_creation_ts") if is_smc and signal and signal.metadata else None,
            smc_zone_creation_time_utc=signal.metadata.get("zone_creation_time_utc") if is_smc and signal and signal.metadata else None,
            smc_bos_bar_idx=signal.metadata.get("bos_bar_idx") if is_smc and signal and signal.metadata else None,
            smc_bos_price=signal.metadata.get("bos_price") if is_smc and signal and signal.metadata else None,
            smc_trigger_candle_time_utc=signal.metadata.get("trigger_candle_time_utc") if is_smc and signal and signal.metadata else None,
            smc_trigger_bar_idx=signal.metadata.get("eval_bar_idx") if is_smc and signal and signal.metadata else None,
            smc_fvg_size=signal.metadata.get("fvg_size") if is_smc and signal and signal.metadata else None,
            smc_target_1to1=signal.metadata.get("target_1to1_price") if is_smc and signal and signal.metadata else None,
            smc_target_1to2=signal.metadata.get("target_1to2_price") if is_smc and signal and signal.metadata else None,
            smc_partial_tp_hit=partial_tp_executed if is_smc else False
        )


    # =========================================================================
    # MAIN ENGINE RUN LOOP
    # =========================================================================

    def _check_runtime_limit(self) -> bool:
        """
        Check if runtime limit has been exceeded.
        Only triggers shutdown between trade cycles (not during active trades).
        Returns True if limit exceeded and shutdown should proceed.
        """
        if self.runtime_limit_seconds <= 0:
            return False
        elapsed = time.time() - self._runtime_start
        if elapsed >= self.runtime_limit_seconds and not self._in_active_trade:
            remaining_h = int((elapsed) // 3600)
            remaining_m = int((elapsed % 3600) // 60)
            self.logger.warning(
                f"[RUNTIME LIMIT] Session duration {remaining_h}h {remaining_m}m exceeded "
                f"limit of {self.runtime_limit_seconds / 3600:.1f}h. Initiating graceful shutdown..."
            )
            return True
        return False

    def _write_github_step_summary(self) -> None:
        """
        Write rich markdown session summary to GitHub Actions Step Summary.
        Only writes if running in GitHub Actions environment.
        """
        summary_path = os.getenv("GITHUB_STEP_SUMMARY")
        if not summary_path:
            return

        stats = self.outcome_logger.cumulative
        session_duration = time.time() - self._runtime_start
        hours = int(session_duration // 3600)
        minutes = int((session_duration % 3600) // 60)

        pnl_emoji = "🟢" if stats.total_pnl_usdt >= 0 else "🔴"
        pnl_sign = "+" if stats.total_pnl_usdt >= 0 else ""

        # Get final balance
        final_bal_usdt = "N/A"
        final_bal_inr = "N/A"
        try:
            if self.config.mode == EngineMode.LIVE:
                bal = self.trader.get_usdt_balance()
                final_bal_usdt = f"{bal.get('available_usdt', 0)}"
                final_bal_inr = f"{bal.get('available_inr', 0):.2f}"
        except Exception:
            pass

        run_id = os.getenv("GITHUB_RUN_ID", "N/A")
        run_num = os.getenv("GITHUB_RUN_NUMBER", "N/A")

        md_lines = [
            f"## {pnl_emoji} KCEX Live Trading Session Report",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| **Total Trades** | {stats.total_trades} |",
            f"| **Wins / Losses / Scratch** | {stats.winning_trades} / {stats.losing_trades} / {stats.scratch_trades} |",
            f"| **Win Rate** | {stats.win_rate_pct:.1f}% |",
            f"| **Net PnL** | {pnl_sign}{stats.total_pnl_usdt:.6f} USDT (₹{pnl_sign}{stats.total_pnl_inr:.4f}) |",
            f"| **Best Trade** | {'+' if stats.best_trade_usdt >= 0 else ''}{stats.best_trade_usdt:.6f} USDT |",
            f"| **Worst Trade** | {stats.worst_trade_usdt:.6f} USDT |",
            f"| **Cancelled Orders** | {self._cancelled_order_count} |",
            f"| **Total Fees** | {stats.total_fees_usdt:.6f} USDT |",
            f"| **Session Duration** | {hours}h {minutes}m |",
            f"| **Final Balance** | {final_bal_usdt} USDT (₹{final_bal_inr}) |",
            f"| **Environment** | GitHub Actions (Run #{run_num}, ID: {run_id}) |",
            f"| **Symbol** | {self.config.symbol} |",
            f"| **Strategy** | {self.config.strategy_mode} ({self.config.execution_style}) |",
            f"| **Leverage** | {self.config.leverage}x |",
            "",
        ]

        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write("\n".join(md_lines) + "\n")
            self.logger.info("[GITHUB] ✅ Step summary written to $GITHUB_STEP_SUMMARY")
        except Exception as e:
            self.logger.warning(f"[GITHUB] ⚠️ Failed to write step summary: {e}")

    def run(self) -> None:
        """
        Main engine execution loop:
        Repeatedly executes trade cycles, enforces cooldown, and manages graceful stops.
        """
        self.running = True
        self._shutdown_requested = False
        self._runtime_start = time.time()

        # Set up signal handler for Ctrl+C
        def handle_sigint(signum, frame):
            self.logger.warning("\n[STOP] Caught SIGINT / Interrupt signal.")
            self.stop()

        signal.signal(signal.SIGINT, handle_sigint)

        contract = self.pre_flight_checks()

        # Log session start to MongoDB
        if self.mongo_logger:
            self.mongo_logger.log_session_start(self.config)

        self.strategy.start()
        self.logger.info("Starting Masterplan Automated Execution Loop...")
        self.logger.info(f"Target Trades: {self.config.max_trades if self.config.max_trades > 0 else 'Unlimited'}")
        if self.runtime_limit_seconds > 0:
            limit_h = self.runtime_limit_seconds / 3600
            self.logger.info(f"Runtime Limit: {limit_h:.1f}h ({self.runtime_limit_seconds:.0f}s) — will gracefully stop after this duration")
        if self.mongo_logger:
            self.logger.info(f"MongoDB Logging: ENABLED | Session ID: {self.mongo_logger.session_id}")

        last_diag_log = 0.0
        consecutive_errors = 0
        while self.running and not self._shutdown_requested:
            try:
                # Check max trades
                if self.config.max_trades > 0 and self.trade_counter >= self.config.max_trades:
                    self.logger.info(f"Reached configured maximum trades limit ({self.config.max_trades}). Stopping.")
                    break

                # Check runtime limit (only between trade cycles)
                if self._check_runtime_limit():
                    break

                # Execute single cycle
                outcome = self.execute_single_trade_cycle(contract)

                if self._shutdown_requested:
                    break

                # Post-trade Cooldown
                if outcome:
                    cooldown = self.config.cooldown_seconds
                    self.logger.info(f"Initiating {cooldown:.0f}s cooldown before next trade cycle...")
                    start_cool = time.time()
                    while time.time() - start_cool < cooldown and not self._shutdown_requested:
                        remaining = int(cooldown - (time.time() - start_cool))
                        if remaining > 0 and remaining % 10 == 0:
                            self.logger.info(f"Cooldown active: {remaining}s remaining...")
                        time.sleep(1.0)
                else:
                    # Log periodic diagnostics while hunting for entry signal
                    now = time.time()
                    diag_interval = 2.0 if (self.logger._is_tty and not self.logger._is_cloud_ci) else 10.0
                    if now - last_diag_log >= diag_interval:
                        last_diag_log = now
                        try:
                            diag = self.strategy.get_diagnostics()
                            prec = contract.price_precision
                            if diag and (diag.get("strategy") == "ORDER_BLOCK_DEMAND" or "active_zones_count" in diag):
                                tf = diag.get("timeframe", getattr(self.config, "timeframe", "15m"))
                                z_cnt = diag.get("active_zones_count", 0)
                                d_cnt = diag.get("demand_zones_count", 0)
                                s_cnt = diag.get("supply_zones_count", 0)
                                p_val = diag.get("cached_price")
                                p_str = f"Price: {p_val:.{prec}f} USDT | " if p_val is not None else ""
                                rej = diag.get("last_rejection_reason", "Hunting setups")
                                zones_list = diag.get("zones", [])
                                nearest_desc = " | Active OB: None"
                                if zones_list:
                                    nz = zones_list[0]
                                    nearest_desc = f" | Active OB: {nz.get('type')} [{nz.get('low', 0):.{prec}f}-{nz.get('high', 0):.{prec}f}]"
                                scan_msg = (
                                    f"[SCANNING] {contract.symbol} [{tf}] | {p_str}Zones: {z_cnt} ({d_cnt} Demand, {s_cnt} Supply){nearest_desc} | Status: {rej}"
                                )
                                self.logger.update_status_line(scan_msg, price=p_val, tag="SCANNING")
                            elif diag and "fast_ema" in diag:
                                c_f = diag.get('fast_ema', 0.0)
                                c_s = diag.get('slow_ema', 0.0)
                                diff = diag.get('diff', 0.0)
                                diff_pct = diag.get('diff_pct', 0.0)
                                scan_msg = (
                                    f"[SCANNING] {contract.symbol} | EMA({diag.get('preset', '5/13')}) {diag.get('interval', 'Min1')} | "
                                    f"Fast: {c_f:.{prec}f} | Slow: {c_s:.{prec}f} | "
                                    f"Diff: {diff:+.{prec}f} ({diff_pct:+.2f}%) | "
                                    f"Trend: {diag.get('trend', 'NEUTRAL')} | Close In: {diag.get('time_to_bar_close_s', 0):.0f}s"
                                )
                                self.logger.update_status_line(scan_msg, price=c_f, tag="SCANNING")
                            elif diag and (diag.get("strategy") == "STOCHASTIC_RSI" or ("k" in diag and "d" in diag)):
                                k_val = diag.get('k', 50.0)
                                d_val = diag.get('d', 50.0)
                                diff_kd = diag.get('diff', 0.0)
                                zone = diag.get('zone', 'NEUTRAL')
                                preset = diag.get('preset', 'FAST_SCALP')
                                inv = diag.get('interval', 'Min1')
                                scan_msg = (
                                    f"[SCANNING] {contract.symbol} | StochRSI({preset}) {inv} | "
                                    f"%K: {k_val:.1f} | %D: {d_val:.1f} (Diff: {diff_kd:+.1f}) | "
                                    f"Zone: {zone} | Trend: {diag.get('trend', 'NEUTRAL')} | Close In: {diag.get('time_to_bar_close_s', 0):.0f}s"
                                )
                                self.logger.update_status_line(scan_msg, price=k_val, tag="SCANNING")
                            elif diag and "obi_z" in diag:
                                feed_info = diag.get("feed", {})
                                ws_status = "WS" if feed_info.get("connected") else "REST"
                                b_bid = f"{diag.get('best_bid'):.{prec}f}" if diag.get('best_bid') else "N/A"
                                b_ask = f"{diag.get('best_ask'):.{prec}f}" if diag.get('best_ask') else "N/A"
                                scan_msg = (
                                    f"[SCANNING] {contract.symbol} [{ws_status}] | Bid/Ask: {b_bid} / {b_ask} (Spread: {diag.get('spread_ticks', 0):.1f}t) | "
                                    f"OBI: {diag.get('obi_z', 0):+.2f} | Delta: {diag.get('delta_z', 0):+.2f} | VAMP: {diag.get('vamp_z', 0):+.2f}"
                                )
                                self.logger.update_status_line(scan_msg, price=diag.get('best_bid'), tag="SCANNING")
                            elif diag and (diag.get("strategy") == "ML_1M_MODEL" or "last_prediction" in diag):
                                last_p = diag.get("last_prediction") or {}
                                if last_p:
                                    p_buy = last_p.get("prob_buy", 0.0)
                                    p_sell = last_p.get("prob_sell", 0.0)
                                    p_wait = last_p.get("prob_wait", 0.0)
                                    act = last_p.get("action", "WAIT")
                                    rem_cd = diag.get("remaining_cooldown_sec", 0.0)
                                    ds = diag.get("data_source") or ("WS" if diag.get("feed", {}).get("connected") else "REST")
                                    curr_p = last_p.get("curr_price")
                                    curr_atr = last_p.get("curr_atr")
                                    atr_t = last_p.get("atr_ticks")
                                    p_prec = max(4, prec)
                                    price_str = f"Price: {curr_p:.{p_prec}f} USDT | " if curr_p is not None else ""
                                    if curr_atr is not None and atr_t is not None:
                                        atr_str = f"ATR(14): {curr_atr:.{p_prec}f} ({atr_t:.1f}t) | "
                                    elif curr_atr is not None:
                                        atr_str = f"ATR(14): {curr_atr:.{p_prec}f} | "
                                    else:
                                        atr_str = ""
                                    cd_str = f" | CD: {rem_cd:.1f}s" if rem_cd > 0 else ""
                                    scan_msg = (
                                        f"[SCANNING] {contract.symbol} [{ds}] | {price_str}{atr_str}"
                                        f"P(BUY): {p_buy:.1%} | P(SELL): {p_sell:.1%} | P(WAIT): {p_wait:.1%} -> Action: {act}"
                                        f"{cd_str}"
                                    )
                                    self.logger.update_status_line(scan_msg, price=curr_p, tag="SCANNING")
                        except Exception as de:
                            self.logger.debug("Diagnostics fetch error: %s", de)

                consecutive_errors = 0
                time.sleep(0.3)

            except KCEXAPIError as ke:
                consecutive_errors += 1
                is_rate_limit = ("510" in str(ke)) or (getattr(ke, "code", None) in (510, 429))
                backoff = min(30.0, 5.0 * (1.5 ** min(consecutive_errors - 1, 4))) if is_rate_limit else 2.0
                self.logger.warning(
                    f"[API EXCEPTION] KCEX API error in execution loop ({ke}). "
                    f"Engaging {backoff:.1f}s backoff before resuming cycle (consecutive errors: {consecutive_errors})."
                )
                time.sleep(backoff)

            except KeyboardInterrupt:
                self.logger.info("KeyboardInterrupt received in execution loop. Initiating graceful shutdown...")
                self._shutdown_requested = True
                break

            except Exception as e:
                consecutive_errors += 1
                backoff = min(15.0, 2.0 * consecutive_errors)
                self.logger.error(
                    f"[UNEXPECTED LOOP ERROR] Error in execution loop: {e}. "
                    f"Pausing {backoff:.1f}s before resuming loop (consecutive errors: {consecutive_errors}).",
                    exc_info=True
                )
                time.sleep(backoff)

        self.running = False
        try:
            self.strategy.stop()
        except Exception:
            pass

        self.logger.section("ENGINE EXECUTION SESSION ENDED")

        stats = self.outcome_logger.cumulative
        self.logger.info(
            f"Total Trades Completed: {stats.total_trades} | "
            f"Wins: {stats.winning_trades} | Losses: {stats.losing_trades} | "
            f"Win Rate: {stats.win_rate_pct:.1f}%"
        )
        self.logger.info(
            f"Net Session PnL: {self.logger.format_dual(stats.total_pnl_usdt)}"
        )
        if self._cancelled_order_count > 0:
            self.logger.info(f"Cancelled Orders (Limit Timeout): {self._cancelled_order_count}")

        # Log session end to MongoDB
        if self.mongo_logger:
            final_bal_usdt = None
            final_bal_inr = None
            try:
                if self.config.mode == EngineMode.LIVE:
                    bal = self.trader.get_usdt_balance()
                    final_bal_usdt = bal.get("available_usdt", 0.0)
                    final_bal_inr = bal.get("available_inr", 0.0)
            except Exception:
                pass

            self.mongo_logger.log_session_end(
                total_trades=stats.total_trades,
                winning_trades=stats.winning_trades,
                losing_trades=stats.losing_trades,
                scratch_trades=stats.scratch_trades,
                cancelled_orders=self._cancelled_order_count,
                total_pnl_usdt=stats.total_pnl_usdt,
                total_pnl_inr=stats.total_pnl_inr,
                win_rate=stats.win_rate_pct,
                best_trade_usdt=stats.best_trade_usdt,
                worst_trade_usdt=stats.worst_trade_usdt,
                total_fees_usdt=stats.total_fees_usdt,
                total_fees_inr=stats.total_fees_inr,
                final_balance_usdt=final_bal_usdt,
                final_balance_inr=final_bal_inr
            )
            self.mongo_logger.close()

        # Write GitHub Actions Step Summary
        self._write_github_step_summary()
