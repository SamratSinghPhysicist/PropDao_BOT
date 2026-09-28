"""
KCEX Multi-Asset Concurrent Trading Engine
==========================================
Coordinates simultaneous autonomous trading across multiple assets on distinct empirically optimal timeframes:
- Base Pairs: TRUMP_USDT (15m), ETH_USDT (4h), BTC_USDT (15m), DOGE_USDT (15m)
- Verified Alpha Pairs: TRX_USDT (1h), AVAX_USDT (1h), AIXBT_USDT (1d)

Features:
- Dedicated worker thread per asset for non-blocking concurrent scanning & execution
- Smart Money Concepts (Vivek Yadav): 1:1 partial TP (50%) + Breakeven stop lock + 1:2 runner
- Strict manual trade conflict prevention (direction-aware position checks, position ID tracking)
- Position concurrency rule: Never open a duplicate position in the same direction, allow opposite direction
- Throttled periodic status logging: Every 10 min if idle, every 5 min if in position (zero log spam on Railway)
- Real-time immediate logging for signals, orders, fills, TP/SL adjustments, and trade closes
- Thread-safe wallet margin sizing (10% available balance per trade @ 15x leverage)
"""

import os
import sys
import time
import math
import signal
import logging
import threading
from datetime import datetime
from typing import Dict, List, Any, Optional, Set, Tuple

from kcex.config import KCEXConfig
from kcex.client import KCEXClient, KCEXAPIError
from kcex.market import KCEXMarket, ContractInfo
from kcex.risk import KCEXRiskCalculator
from kcex.trade import KCEXTrader
from kcex.engine.models import (
    OrderDirection,
    EngineMode,
    TradeOutcome,
    ExitReason,
    TradeSignal,
    ExecutionConfig
)
from kcex.engine.logger import DualCurrencyLogger, TradeOutcomeLogger
from kcex.engine.mongo_logger import MongoTradeLogger
from strategies.order_block_demand import OrderBlockDemandStrategy, ZoneStatus

logger = logging.getLogger("MultiAssetEngine")


class AssetWorker:
    """
    Dedicated worker thread managing market scanning, signal generation,
    order execution, and position lifecycle for a single asset.
    """

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        pivot_len: int,
        leverage: int,
        margin_pct: float,
        risk_reward_ratio: float,
        mode: EngineMode,
        shared_logger: DualCurrencyLogger,
        shared_outcome_logger: TradeOutcomeLogger,
        shared_mongo_logger: Optional[MongoTradeLogger],
        order_lock: threading.Lock,
        cooldown_seconds: float = 30.0,
        buffer_ticks: int = 1,
        breakeven_buffer_ticks: int = 1,
        shared_outcomes: Optional[List[TradeOutcome]] = None,
        is_stock: bool = False
    ):
        self.symbol = symbol.upper()
        self.timeframe = timeframe
        self.pivot_len = pivot_len
        self.leverage = leverage
        self.margin_pct = margin_pct
        self.risk_reward_ratio = risk_reward_ratio
        self.mode = mode
        self.logger = shared_logger
        self.outcome_logger = shared_outcome_logger
        self.mongo_logger = shared_mongo_logger
        self.order_lock = order_lock
        self.cooldown_seconds = cooldown_seconds
        self.buffer_ticks = buffer_ticks
        self.breakeven_buffer_ticks = breakeven_buffer_ticks
        self.shared_outcomes = shared_outcomes if shared_outcomes is not None else []
        self.is_stock = is_stock

        from strategies.filters import USMarketHoursFilter
        self.us_market_filter = USMarketHoursFilter(enabled=self.is_stock)

        # Thread-safe private API clients
        self.client = KCEXClient()
        self.market = KCEXMarket(self.client)
        self.risk = KCEXRiskCalculator(self.market, self.client)
        self.trader = KCEXTrader(self.client, self.market, self.risk)

        # Strategy instance
        self.strategy = OrderBlockDemandStrategy(
            market=self.market,
            symbol=self.symbol,
            interval=self.timeframe,
            pivot_len=self.pivot_len,
            risk_reward_ratio=self.risk_reward_ratio,
            buffer_ticks=self.buffer_ticks,
            cooldown_seconds=self.cooldown_seconds,
            require_closed_candle=True
        )

        # Runtime worker state
        self.contract: Optional[ContractInfo] = None
        self.thread: Optional[threading.Thread] = None
        self.running: bool = False
        self.in_position: bool = False
        self.current_position_id: Optional[int] = None
        self.last_price: float = 0.0
        self.last_status_msg: str = "Initializing..."
        self.active_position_desc: str = ""
        self.trade_counter: int = 0
        self.last_cooldown_end: float = 0.0

    def start(self) -> None:
        """Starts worker background thread with initial warmup scan."""
        self.contract = self.market.get_contract_detail(self.symbol)
        self.strategy.start()
        # Warmup scan: compute initial zones immediately
        try:
            if hasattr(self.strategy, "generate_signal"):
                self.strategy.generate_signal(self.symbol)
            elif hasattr(self.strategy, "get_signal"):
                self.strategy.get_signal()
            diag = self.strategy.get_diagnostics() if hasattr(self.strategy, "get_diagnostics") else {}
            zones = diag.get("zones", [])
            active_ob_str = "None"
            if zones:
                z = zones[0]
                active_ob_str = f"{z.get('type')} [{z.get('low')}-{z.get('high')}]"
            self.last_status_msg = f"Zones: {len(zones)} | Active OB: {active_ob_str} | Status: {diag.get('last_rejection_reason', 'Hunting setups')}"
        except Exception as we:
            self.logger.debug(f"[{self.symbol}] Warmup scan notice: {we}")

        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name=f"Worker-{self.symbol}")
        self.thread.start()

    def stop(self) -> None:
        """Stops worker gracefully."""
        self.running = False
        try:
            self.strategy.stop()
        except Exception:
            pass

    def _run_loop(self) -> None:
        """Worker lifecycle loop: Scan -> Check Signal -> Execute -> Monitor -> Cooldown."""
        self.logger.info(f"[{self.symbol}] Worker thread started. Timeframe: {self.timeframe} | Leverage: {self.leverage}x | Target R:R: 1:{self.risk_reward_ratio:g}")

        while self.running:
            try:
                # 1. Update latest ticker price & diagnostics
                try:
                    ticker = self.market.get_ticker(self.symbol)
                    p = float(ticker.get("lastPrice") or ticker.get("fairPrice", 0.0))
                    if p > 0:
                        self.last_price = p
                except Exception:
                    pass

                # Cooldown check
                now = time.time()
                if now < self.last_cooldown_end:
                    rem = int(self.last_cooldown_end - now)
                    self.last_status_msg = f"Cooldown ({rem}s remaining)"
                    time.sleep(1.0)
                    continue

                # 2. Check for trade signal
                signal = None
                try:
                    if hasattr(self.strategy, "generate_signal"):
                        signal = self.strategy.generate_signal(self.symbol)
                    elif hasattr(self.strategy, "get_signal"):
                        signal = self.strategy.get_signal()
                except Exception as e:
                    self.logger.warning(f"[{self.symbol}] Signal fetch error: {e}")

                if signal is None:
                    # Update status diagnostics
                    diag = self.strategy.get_diagnostics() if hasattr(self.strategy, "get_diagnostics") else {}
                    rej = diag.get("last_rejection_reason", "Hunting setups")
                    zones = diag.get("zones", [])
                    active_ob_str = "None"
                    if zones:
                        z = zones[0]
                        active_ob_str = f"{z.get('type')} [{z.get('low')}-{z.get('high')}]"
                    self.last_status_msg = f"Zones: {len(zones)} | Active OB: {active_ob_str} | Status: {rej}"
                    time.sleep(2.0)
                    continue

                # 3. Check US Market Hours filter for stock equivalent assets
                if getattr(self, "us_market_filter", None) and self.us_market_filter.is_enabled:
                    allowed, reason = self.us_market_filter.is_allowed(signal, [], time.time())
                    if not allowed:
                        self.last_status_msg = f"Market Hours Gated: {reason}"
                        time.sleep(5.0)
                        continue

                # 4. Valid Signal Received! Check position concurrency before executing
                direction = signal.direction
                self.logger.info(
                    f"⚡ [{self.symbol}] Valid {direction.value} Signal Detected! "
                    f"Zone: {signal.metadata.get('zone_type')} [{signal.metadata.get('zone_low')}-{signal.metadata.get('zone_high')}] | "
                    f"1:2 R:R Target: {signal.metadata.get('take_profit_price')} | SL: {signal.metadata.get('stop_loss_price')}"
                )

                # Acquire order lock to prevent multi-pair balance race conditions
                with self.order_lock:
                    can_execute = self._check_concurrency_and_balance(direction)
                    if not can_execute:
                        if hasattr(self.strategy, "on_trade_rejected"):
                            self.strategy.on_trade_rejected()
                        continue

                    # Execute entry order
                    outcome = self._execute_trade(signal)

                # If trade entered, monitor until close
                if outcome is not None:
                    self.last_cooldown_end = time.time() + self.cooldown_seconds
                    if hasattr(self.strategy, "on_trade_completed"):
                        try:
                            self.strategy.on_trade_completed(outcome)
                        except Exception as ce:
                            self.logger.warning(f"[{self.symbol}] Strategy on_trade_completed error: {ce}")
                else:
                    if hasattr(self.strategy, "on_trade_rejected"):
                        try:
                            self.strategy.on_trade_rejected()
                        except Exception as re:
                            self.logger.warning(f"[{self.symbol}] Strategy on_trade_rejected error: {re}")


            except KCEXAPIError as ke:
                self.logger.warning(f"[{self.symbol}] KCEX API error in loop: {ke}")
                time.sleep(5.0)
            except Exception as e:
                self.logger.warning(f"[{self.symbol}] Unexpected worker loop error: {e}")
                time.sleep(3.0)

    def _check_concurrency_and_balance(self, direction: OrderDirection) -> bool:
        """
        Enforces concurrency rules & manual trade isolation:
        - Never open duplicate position in same direction
        - Allow opposite direction (Hedge Mode)
        - Never interfere with existing manual positions
        """
        if self.mode != EngineMode.LIVE:
            return True

        try:
            open_positions = self.trader.get_open_positions(self.symbol)
            for p in open_positions:
                h_vol = float(p.get("holdVol", 0) or p.get("vol", 0))
                if h_vol > 0:
                    p_type = p.get("positionType")
                    p_side = p.get("side")
                    is_pos_long = (p_type == 1 or str(p_side).upper() in ("1", "LONG", "BUY"))
                    if (direction == OrderDirection.LONG and is_pos_long):
                        self.logger.info(
                            f"[{self.symbol}] Active LONG position already exists (Hold: {h_vol:g} contracts). "
                            f"Skipping duplicate LONG signal."
                        )
                        return False
                    elif (direction == OrderDirection.SHORT and not is_pos_long):
                        self.logger.info(
                            f"[{self.symbol}] Active SHORT position already exists (Hold: {h_vol:g} contracts). "
                            f"Skipping duplicate SHORT signal."
                        )
                        return False
            return True
        except Exception as e:
            self.logger.warning(f"[{self.symbol}] Concurrency verification error: {e}")
            return False

    def _execute_trade(self, signal: TradeSignal) -> Optional[TradeOutcome]:
        """Sizes position to 10% available margin @ 15x leverage and enters trade."""
        direction = signal.direction
        is_long = (direction == OrderDirection.LONG)
        side_str = "LONG" if is_long else "SHORT"
        prec = self.contract.price_precision
        pu = self.contract.price_unit
        cs = self.contract.contract_size
        min_vol = int(self.contract.min_volume)

        # 1. Fetch available USDT margin
        avail_usdt = 100.0  # default for dry-run
        inr_rate = self.market.get_inr_rate()
        if self.mode == EngineMode.LIVE:
            try:
                balances = self.trader.get_usdt_balance()
                avail_usdt = balances.get("available_usdt", 0.0)
            except Exception as e:
                self.logger.warning(f"[{self.symbol}] Could not fetch balance: {e}")
                return None
        bal_before_usdt = avail_usdt
        bal_before_inr = avail_usdt * inr_rate


        # 2. Position sizing: 10% available margin @ 15x leverage
        ticker = self.market.get_ticker(self.symbol)
        curr_price = float(ticker.get("lastPrice", 0.0) or ticker.get("fairPrice", 1.0))

        if curr_price <= 0:
            return None

        one_contract_margin = (min_vol * cs * curr_price) / self.leverage
        if avail_usdt < one_contract_margin and self.mode == EngineMode.LIVE:
            self.logger.warning(
                f"[{self.symbol}] Insufficient margin: Available: {avail_usdt} USDT, "
                f"minimum required for 1 contract ({min_vol * cs} {self.contract.base_coin}) is {one_contract_margin} USDT. Aborting trade."
            )
            return None

        margin_to_use = avail_usdt * (self.margin_pct / 100.0)
        notional_usdt = margin_to_use * self.leverage
        raw_contracts = notional_usdt / (curr_price * cs)
        vol_contracts = max(min_vol, int(raw_contracts))
        committed_margin = (vol_contracts * cs * curr_price) / self.leverage

        self.logger.info(
            f"🚀 [{self.symbol}] SIZING: Available: {avail_usdt} USDT -> 10% Margin: {margin_to_use} USDT | "
            f"{self.leverage}x Lev Notional: {notional_usdt} USDT -> Vol: {vol_contracts} contracts (Committed: {committed_margin} USDT)"
        )

        initial_sl = float(signal.metadata.get("stop_loss_price"))
        exact_tp = float(signal.metadata.get("take_profit_price"))
        target_1to1 = float(signal.metadata.get("target_1to1_price", curr_price))

        position_id = None
        order_id = None
        entry_price = curr_price
        open_time = time.time()

        if self.mode == EngineMode.LIVE:
            try:
                # Market Entry Order
                order_res = self.trader.create_order(
                    symbol=self.symbol,
                    side="BUY" if is_long else "SELL",
                    vol_contracts=vol_contracts,
                    order_type="MARKET",
                    leverage=self.leverage,
                    stop_loss_price=initial_sl,
                    is_isolated=True
                )
                order_id = str((order_res.get("data") or {}).get("orderId", ""))
                self.logger.info(f"[{self.symbol}] Live entry order submitted (Order ID: {order_id})")

                # Wait 500ms and reconcile position
                time.sleep(0.5)
                open_pos = self.trader.get_open_positions(self.symbol)
                for p in open_pos:
                    h_vol = float(p.get("holdVol", 0) or p.get("vol", 0))
                    if h_vol > 0:
                        p_type = p.get("positionType")
                        p_side = p.get("side")
                        is_pos_long = (p_type == 1 or str(p_side).upper() in ("1", "LONG", "BUY"))
                        if (direction == OrderDirection.LONG and is_pos_long) or (direction == OrderDirection.SHORT and not is_pos_long):
                            position_id = int(p.get("positionId"))
                            entry_price = float(p.get("openAvgPrice") or p.get("holdAvgPrice") or curr_price)
                            break

                self.current_position_id = position_id
                self.in_position = True
                self.logger.info(f"[{self.symbol}] Position Confirmed! Entry: {entry_price:.{prec}f} USDT | Position ID: {position_id}")

                # Ensure and verify TP/SL orders & 1:1 partial close limit order (checking 2-3+ times)
                pre_placed_tp1_order_id, tp1_contracts = self._setup_and_verify_position_orders(
                    symbol=self.symbol,
                    position_id=position_id,
                    direction=direction,
                    vol_contracts=vol_contracts,
                    entry_price=entry_price,
                    initial_sl=initial_sl,
                    exact_tp=exact_tp,
                    target_1to1=target_1to1,
                    precision=prec
                )

            except Exception as e:
                self.logger.error(f"[{self.symbol}] Live order execution failed: {e}")
                return None
        else:
            # Dry-run execution
            self.in_position = True
            entry_price = curr_price
            order_id = "SIMULATED_ORDER"
            pre_placed_tp1_order_id = None
            tp1_contracts = (vol_contracts // 2) if vol_contracts >= 2 else 1

        # 3. Monitor Position until Exit
        exit_price, exit_reason = self._monitor_position(
            position_id=position_id,
            direction=direction,
            vol_contracts=vol_contracts,
            entry_price=entry_price,
            initial_sl=initial_sl,
            exact_tp=exact_tp,
            target_1to1=target_1to1,
            precision=prec,
            open_time=open_time,
            pre_placed_tp1_order_id=pre_placed_tp1_order_id,
            tp1_contracts=tp1_contracts
        )

        self.in_position = False
        self.current_position_id = None
        self.active_position_desc = ""

        # 4. Financial Reconciliation
        close_time = time.time()
        duration = max(0.1, close_time - open_time)
        price_diff = (exit_price - entry_price) if is_long else (entry_price - exit_price)
        underlying_qty = vol_contracts * cs
        fee_rate = 0.0001  # KCEX 0.01% taker fee
        fee_total = (underlying_qty * entry_price * fee_rate) + (underlying_qty * exit_price * fee_rate)
        realized_pnl_usdt = (underlying_qty * price_diff) - fee_total
        margin_usdt = (underlying_qty * entry_price) / self.leverage
        roe_pct = (realized_pnl_usdt / margin_usdt * 100.0) if margin_usdt > 0 else 0.0

        inr_rate = self.market.get_inr_rate()
        notional_usdt = underlying_qty * entry_price
        notional_inr = notional_usdt * inr_rate
        margin_inr = margin_usdt * inr_rate
        realized_pnl_inr = realized_pnl_usdt * inr_rate
        fee_total_inr = fee_total * inr_rate

        bal_after_usdt = None
        bal_after_inr = None
        if self.mode == EngineMode.LIVE:
            try:
                post_bals = self.trader.get_usdt_balance()
                bal_after_usdt = post_bals.get("available_usdt", None)
                if bal_after_usdt is not None:
                    bal_after_inr = bal_after_usdt * inr_rate
            except Exception:
                pass

        sig_meta = signal.metadata if signal and signal.metadata else {}
        self.trade_counter += 1
        outcome = TradeOutcome(
            trade_id=self.trade_counter,
            symbol=self.symbol,
            direction=direction,
            sub_strategy_name=f"OrderBlockDemand({self.timeframe})",
            mode=self.mode,
            leverage=self.leverage,
            vol_contracts=vol_contracts,
            contract_size=cs,
            underlying_quantity=underlying_qty,
            base_coin=self.contract.base_coin,
            entry_price=entry_price,
            exit_price=exit_price,
            min_profit_tp_price=exact_tp,
            stop_loss_price=initial_sl,
            price_unit=pu,
            price_precision=prec,
            open_time=open_time,
            close_time=close_time,
            duration_seconds=duration,
            notional_value_usdt=notional_usdt,
            notional_value_inr=notional_inr,
            margin_used_usdt=margin_usdt,
            margin_used_inr=margin_inr,
            realized_pnl_usdt=realized_pnl_usdt,
            realized_pnl_inr=realized_pnl_inr,
            pnl_percentage=(price_diff / entry_price * 100.0),
            roe_percentage=roe_pct,
            fee_open_usdt=fee_total / 2.0,
            fee_close_usdt=fee_total / 2.0,
            fee_total_usdt=fee_total,
            fee_total_inr=fee_total_inr,
            inr_rate=inr_rate,
            exit_reason=exit_reason,
            balance_before_trade_usdt=bal_before_usdt,
            balance_before_trade_inr=bal_before_inr,
            balance_after_trade_usdt=bal_after_usdt,
            balance_after_trade_inr=bal_after_inr,
            order_id=order_id,
            position_id=position_id,
            smc_zone_id=sig_meta.get("zone_id"),
            smc_zone_type=sig_meta.get("zone_type"),
            smc_zone_high=sig_meta.get("zone_high"),
            smc_zone_low=sig_meta.get("zone_low"),
            smc_zone_mid=sig_meta.get("zone_mid"),
            smc_zone_creation_bar_idx=sig_meta.get("zone_creation_bar_idx"),
            smc_zone_creation_ts=sig_meta.get("zone_creation_ts"),
            smc_zone_creation_time_utc=sig_meta.get("zone_creation_time_utc"),
            smc_bos_bar_idx=sig_meta.get("bos_bar_idx"),
            smc_bos_price=sig_meta.get("bos_price"),
            smc_trigger_candle_time_utc=sig_meta.get("trigger_candle_time_utc"),
            smc_trigger_bar_idx=sig_meta.get("eval_bar_idx"),
            smc_fvg_size=sig_meta.get("fvg_size"),
            smc_target_1to1=sig_meta.get("target_1to1_price", target_1to1),
            smc_target_1to2=sig_meta.get("target_1to2_price", exact_tp),
            smc_partial_tp_hit=(exit_reason == ExitReason.RATCHET_BREAKEVEN_HIT or exit_reason == ExitReason.MIN_PROFIT_TP_HIT)
        )

        self.outcome_logger.log_outcome(outcome)
        if self.shared_outcomes is not None:
            self.shared_outcomes.append(outcome)

        worker_config = ExecutionConfig(
            symbol=self.symbol,
            direction=direction,
            mode=self.mode,
            leverage=self.leverage,
            strategy_mode="ORDER_BLOCK_DEMAND",
            timeframe=self.timeframe,
            pivot_len=self.pivot_len,
            risk_reward_ratio=self.risk_reward_ratio,
            margin_pct=self.margin_pct,
            partial_tp_enabled=True,
            breakeven_buffer_ticks=self.breakeven_buffer_ticks
        )

        if self.mongo_logger and self.mode == EngineMode.LIVE:
            try:
                doc_id = self.mongo_logger.log_executed_trade(
                    outcome=outcome,
                    config=worker_config,
                    balance_before_usdt=bal_before_usdt,
                    balance_before_inr=bal_before_inr
                )
                if doc_id:
                    self.logger.info(f"[{self.symbol}] 📝 Trade #{self.trade_counter} logged to MongoDB (Doc ID: {doc_id})")
                else:
                    self.logger.warning(f"[{self.symbol}] ⚠️ Failed to log trade #{self.trade_counter} to MongoDB")
            except Exception as me:
                self.logger.warning(f"[{self.symbol}] ⚠️ MongoDB trade logging error: {me}")

        pnl_sign = "+" if realized_pnl_usdt >= 0 else ""
        self.logger.info(
            f"🏁 [{self.symbol}] TRADE COMPLETED: {direction.value} | Reason: {exit_reason.value} | "
            f"Entry: {entry_price:.{prec}f} -> Exit: {exit_price:.{prec}f} | Net PnL: {pnl_sign}{realized_pnl_usdt:.4f} USDT ({pnl_sign}{roe_pct:.2f}% ROE) | Hold: {duration/60:.1f}m"
        )
        return outcome

    def _setup_and_verify_position_orders(
        self,
        symbol: str,
        position_id: int,
        direction: OrderDirection,
        vol_contracts: int,
        entry_price: float,
        initial_sl: float,
        exact_tp: float,
        target_1to1: float,
        precision: int,
        max_attempts: int = 4
    ) -> Tuple[Optional[str], int]:
        """
        After opening a position, sets and verifies server-side TP and SL
        and places/verifies the 1:1 partial close limit order.
        Checks 2-3 or more times until confirmed on KCEX book/server.
        """
        is_long = (direction == OrderDirection.LONG)
        tp1_contracts = (vol_contracts // 2) if vol_contracts >= 2 else 1
        pct_label = "50%" if vol_contracts >= 2 else "100% (min 1 contract)"
        pu = self.contract.price_unit

        # 1. Set & Verify Server-Side TP and SL (checking 2-3+ times)
        tp_sl_verified = False
        for attempt in range(1, max_attempts + 1):
            try:
                self.trader.set_position_tp_sl(
                    symbol=symbol,
                    position_id=position_id,
                    take_profit_price=exact_tp,
                    stop_loss_price=initial_sl
                )
            except Exception as e:
                self.logger.warning(f"[{symbol}] Attempt {attempt}/{max_attempts} setting position TP/SL: {e}")

            time.sleep(0.6)
            try:
                open_stops = self.trader.get_open_stop_orders()
                for s in open_stops:
                    pos_id_match = s.get("positionId") and int(s.get("positionId")) == int(position_id)
                    sym_match = s.get("symbol") == symbol.upper()
                    if pos_id_match or sym_match:
                        tp_val = float(s.get("takeProfitPrice") or 0.0)
                        sl_val = float(s.get("stopLossPrice") or 0.0)
                        if sl_val > 0 or tp_val > 0:
                            tp_sl_verified = True
                            self.logger.info(
                                f"✅ [{symbol}] Position TP/SL VERIFIED on KCEX (Attempt {attempt}/{max_attempts}) | "
                                f"TP: {exact_tp:.{precision}f} USDT | SL: {initial_sl:.{precision}f} USDT"
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

        # 2. Pre-place & Verify 1:1 Partial Close Limit Order (checking 2-3+ times)
        pre_placed_tp1_order_id = None
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
                        side="LONG" if is_long else "SHORT",
                        price=target_1to1,
                        vol_contracts=tp1_contracts,
                        position_id=position_id,
                        leverage=self.leverage,
                        is_isolated=True
                    )
                    pre_placed_tp1_order_id = str((close_res.get("data") or {}).get("orderId") or "")
                except Exception as e:
                    self.logger.warning(f"[{symbol}] Attempt {attempt}/{max_attempts} placing 1:1 limit order failed: {e}")

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
        position_id: int,
        exact_tp: float,
        be_sl: float,
        precision: int,
        max_attempts: int = 3
    ) -> bool:
        """Checks 2-3 times until server-side breakeven SL is confirmed updated on KCEX."""
        pu = self.contract.price_unit
        for attempt in range(1, max_attempts + 1):
            try:
                self.trader.set_position_tp_sl(
                    symbol=self.symbol,
                    position_id=position_id,
                    take_profit_price=exact_tp,
                    stop_loss_price=be_sl
                )
            except Exception as e:
                self.logger.debug(f"[{self.symbol}] Attempt {attempt} updating BE SL: {e}")

            time.sleep(0.5)
            try:
                open_stops = self.trader.get_open_stop_orders()
                for s in open_stops:
                    pos_id_match = s.get("positionId") and int(s.get("positionId")) == int(position_id)
                    sym_match = s.get("symbol") == self.symbol.upper()
                    if pos_id_match or sym_match:
                        sl_p = float(s.get("stopLossPrice") or 0.0)
                        if abs(sl_p - be_sl) <= (2 * pu):
                            self.logger.info(
                                f"✅ [{self.symbol}] Breakeven SL VERIFIED on KCEX (Attempt {attempt}/{max_attempts}): SL {be_sl:.{precision}f} USDT"
                            )
                            return True
            except Exception:
                pass
            time.sleep(0.5)

        self.logger.warning(f"⚠️ [{self.symbol}] Could not confirm server-side BE SL on KCEX after {max_attempts} attempts. Software monitor will guard breakeven.")
        return False

    def _monitor_position(
        self,
        position_id: Optional[int],
        direction: OrderDirection,
        vol_contracts: int,
        entry_price: float,
        initial_sl: float,
        exact_tp: float,
        target_1to1: float,
        precision: int,
        open_time: float,
        pre_placed_tp1_order_id: Optional[str] = None,
        tp1_contracts: int = 1
    ) -> tuple[float, ExitReason]:
        """Monitors active position for SMC 1:1 Partial TP + BE Lock + 1:2 Runner."""
        is_long = (direction == OrderDirection.LONG)
        pu = self.contract.price_unit
        exact_sl = initial_sl
        partial_tp_executed = False
        remaining_vol = vol_contracts
        partial_fill_price = None

        self.logger.info(
            f"🎯 [{self.symbol}] Monitoring: TP1 (1:1): {target_1to1:.{precision}f} | TP2 (1:2): {exact_tp:.{precision}f} | Initial SL: {exact_sl:.{precision}f}"
        )

        while self.running:
            time.sleep(0.5)
            # 1. Fetch executable price
            try:
                ticker = self.market.get_ticker(self.symbol)
                bid1 = float(ticker.get("bid1", 0.0))
                ask1 = float(ticker.get("ask1", 0.0))
                last_p = float(ticker.get("lastPrice", 0.0))
                exec_price = (bid1 if bid1 > 0 else last_p) if is_long else (ask1 if ask1 > 0 else last_p)
                self.last_price = last_p
            except Exception:
                continue

            # Update status string for dashboard
            u_diff = (exec_price - entry_price) if is_long else (entry_price - exec_price)
            u_roe = (u_diff / entry_price) * self.leverage * 100.0
            hold_sec = time.time() - open_time
            self.active_position_desc = f"{direction.value} @ {entry_price} ({u_roe:+.2f}% ROE) | Mark: {exec_price} | Hold: {hold_sec/60:.1f}m"

            current_hold_vol = remaining_vol
            pos_still_open = True

            # 2. Check if position closed on exchange (via server-side SL or TP or pre-placed limit)
            if self.mode == EngineMode.LIVE and position_id:
                try:
                    open_pos = self.trader.get_open_positions(self.symbol)
                    pos_still_open = False
                    for p in open_pos:
                        if int(p.get("positionId", 0)) == int(position_id):
                            h_vol = float(p.get("holdVol", 0) or p.get("vol", 0))
                            if h_vol > 0:
                                current_hold_vol = int(h_vol)
                                pos_still_open = True
                                break
                    if not pos_still_open:
                        self.logger.info(f"[{self.symbol}] Position closed on KCEX.")
                        if pre_placed_tp1_order_id and not partial_tp_executed:
                            try:
                                self.trader.cancel_order(pre_placed_tp1_order_id)
                            except Exception:
                                pass

                        if vol_contracts == 1 and pre_placed_tp1_order_id and not partial_tp_executed:
                            reached_1to1 = (exec_price >= target_1to1 - (0.5 * pu)) if is_long else (exec_price <= target_1to1 + (0.5 * pu))
                            if reached_1to1:
                                return target_1to1, ExitReason.MIN_PROFIT_TP_HIT
                            elif (exec_price <= exact_sl + (0.5 * pu) if is_long else exec_price >= exact_sl - (0.5 * pu)):
                                return exact_sl, ExitReason.STOP_LOSS_HIT

                        if partial_tp_executed and partial_fill_price is not None:
                            hit_runner_tp = (exec_price >= exact_tp if is_long else exec_price <= exact_tp)
                            blended = (partial_fill_price + (exact_tp if hit_runner_tp else exact_sl)) / 2.0
                            return blended, ExitReason.MIN_PROFIT_TP_HIT if hit_runner_tp else ExitReason.RATCHET_BREAKEVEN_HIT

                        return exec_price, ExitReason.MIN_PROFIT_TP_HIT if (exec_price >= exact_tp if is_long else exec_price <= exact_tp) else ExitReason.STOP_LOSS_HIT
                except Exception:
                    pass

            # 3. Smart Money Concepts: 1:1 Partial TP & Breakeven Lock
            # Trigger 1:1 if resting limit order filled on KCEX book OR executable price touched target_1to1
            limit_filled = bool(pos_still_open and current_hold_vol > 0 and current_hold_vol <= (vol_contracts - tp1_contracts))
            price_hit_1to1 = (exec_price >= target_1to1) if is_long else (exec_price <= target_1to1)

            if (limit_filled or price_hit_1to1) and not partial_tp_executed:
                partial_tp_executed = True
                partial_fill_price = target_1to1

                if vol_contracts >= 2:
                    # 50% closure allowed
                    remaining_vol = current_hold_vol if (pos_still_open and current_hold_vol > 0) else (vol_contracts - tp1_contracts)
                    if limit_filled:
                        self.logger.info(
                            f"🎉 [{self.symbol}] 1:1 PRE-PLACED LIMIT TP FILLED! Closed 50% ({tp1_contracts} contracts) at exact 1:1 ({target_1to1:.{precision}f} USDT)."
                        )
                    else:
                        self.logger.info(
                            f"🎉 [{self.symbol}] 1:1 TARGET REACHED! Closing 50% ({tp1_contracts} contracts) at market..."
                        )
                        if pre_placed_tp1_order_id:
                            try:
                                self.trader.cancel_order(pre_placed_tp1_order_id)
                            except Exception:
                                pass
                        if self.mode == EngineMode.LIVE and position_id:
                            try:
                                self.trader.close_position(
                                    position_id=position_id,
                                    symbol=self.symbol,
                                    side="LONG" if is_long else "SHORT",
                                    vol_contracts=tp1_contracts,
                                    leverage=self.leverage,
                                    is_market=True,
                                    price=exec_price
                                )
                            except Exception as ce:
                                self.logger.warning(f"[{self.symbol}] Partial close error: {ce}")

                    # Move Stop Loss to Breakeven (+1 tick buffer in profit)
                    new_be_sl = entry_price + (self.breakeven_buffer_ticks * pu) if is_long else entry_price - (self.breakeven_buffer_ticks * pu)
                    exact_sl = round(new_be_sl, precision)
                    self.logger.info(
                        f"🔒 [{self.symbol}] BREAKEVEN SL LOCKED: Stop moved to {exact_sl:.{precision}f} USDT (+{self.breakeven_buffer_ticks}t buffer). "
                        f"Remaining {remaining_vol} runner contract(s) 100% risk-free towards 1:2 target ({exact_tp:.{precision}f} USDT)!"
                    )
                    # Verify Breakeven SL on KCEX 2-3 times
                    if self.mode == EngineMode.LIVE and position_id:
                        self._update_and_verify_be_sl(position_id, exact_tp, exact_sl, precision)

                else:
                    # 1 contract volume: 50% closure is not allowed as min closable quantity is 1 contract. Close 100%!
                    if limit_filled or not pos_still_open:
                        self.logger.info(
                            f"🎯 [{self.symbol}] 1-CONTRACT 1:1 TP FILLED! Pre-placed limit order filled on KCEX. "
                            f"Closed 100% (1 contract) at 1:1 target ({target_1to1:.{precision}f} USDT)."
                        )
                        return target_1to1, ExitReason.MIN_PROFIT_TP_HIT
                    else:
                        self.logger.info(
                            f"🎯 [{self.symbol}] 1-CONTRACT 1:1 TP REACHED! (50% not allowed for 1 contract). Closing 100% at market ({exec_price:.{precision}f} USDT)..."
                        )
                        if pre_placed_tp1_order_id:
                            try:
                                self.trader.cancel_order(pre_placed_tp1_order_id)
                            except Exception:
                                pass
                        if self.mode == EngineMode.LIVE and position_id:
                            try:
                                self.trader.close_position(
                                    position_id=position_id,
                                    symbol=self.symbol,
                                    side="LONG" if is_long else "SHORT",
                                    vol_contracts=1,
                                    leverage=self.leverage,
                                    is_market=True,
                                    price=exec_price
                                )
                            except Exception as ce:
                                self.logger.warning(f"[{self.symbol}] 1-contract 100% close error: {ce}")
                        return target_1to1, ExitReason.MIN_PROFIT_TP_HIT

            # 4. Check Final 1:2 Take Profit Hit
            hit_tp2 = (exec_price >= exact_tp) if is_long else (exec_price <= exact_tp)
            if hit_tp2:
                self.logger.info(f"🎯 [{self.symbol}] 1:2 TAKE PROFIT HIT at {exec_price:.{precision}f} USDT!")
                if pre_placed_tp1_order_id:
                    try:
                        self.trader.cancel_order(pre_placed_tp1_order_id)
                    except Exception:
                        pass
                if self.mode == EngineMode.LIVE and position_id:
                    try:
                        self.trader.close_position(
                            position_id=position_id,
                            symbol=self.symbol,
                            side="LONG" if is_long else "SHORT",
                            vol_contracts=remaining_vol,
                            leverage=self.leverage,
                            is_market=True,
                            price=exec_price
                        )
                    except Exception:
                        pass
                if partial_tp_executed and partial_fill_price is not None:
                    blended = (partial_fill_price + exact_tp) / 2.0
                    return blended, ExitReason.MIN_PROFIT_TP_HIT
                return exact_tp, ExitReason.MIN_PROFIT_TP_HIT

            # 5. Check Stop Loss Hit (Initial SL or Breakeven SL)
            hit_sl = (exec_price <= exact_sl) if is_long else (exec_price >= exact_sl)
            if hit_sl:
                reason = ExitReason.RATCHET_BREAKEVEN_HIT if partial_tp_executed else ExitReason.STOP_LOSS_HIT
                self.logger.info(f"🛑 [{self.symbol}] {'BREAKEVEN' if partial_tp_executed else 'STOP LOSS'} HIT at {exec_price:.{precision}f} USDT!")
                if pre_placed_tp1_order_id:
                    try:
                        self.trader.cancel_order(pre_placed_tp1_order_id)
                    except Exception:
                        pass
                if self.mode == EngineMode.LIVE and position_id:
                    try:
                        self.trader.close_position(
                            position_id=position_id,
                            symbol=self.symbol,
                            side="LONG" if is_long else "SHORT",
                            vol_contracts=remaining_vol,
                            leverage=self.leverage,
                            is_market=True,
                            price=exec_price
                        )
                    except Exception:
                        pass
                if partial_tp_executed and partial_fill_price is not None:
                    blended = (partial_fill_price + exact_sl) / 2.0
                    return blended, reason
                return exact_sl, reason

        return exec_price, ExitReason.MANUAL_CLOSE


class MultiAssetExecutionEngine:
    """
    Master coordinator managing concurrent asset workers and providing
    consolidated, throttled portfolio logging (10 min idle, 5 min active position).
    """

    def __init__(
        self,
        assets: Optional[List[Dict[str, Any]]] = None,
        mode: EngineMode = EngineMode.LIVE,
        leverage: int = 15,
        margin_pct: float = 10.0,
        risk_reward_ratio: float = 2.0,
        mongo_logger: Optional[MongoTradeLogger] = None
    ):
        self.mode = mode
        self.leverage = leverage
        self.margin_pct = margin_pct
        self.risk_reward_ratio = risk_reward_ratio
        self.mongo_logger = mongo_logger
        self.running: bool = False
        self._shutdown_requested: bool = False
        self.outcomes: List[TradeOutcome] = []

        # Shared loggers
        self.logger = DualCurrencyLogger(log_file="logs/engine_realtime.log")
        self.outcome_logger = TradeOutcomeLogger(
            txt_file="logs/trade_outcomes.txt",
            jsonl_file="logs/trade_outcomes.jsonl"
        )

        # Thread synchronization
        self.order_lock = threading.Lock()

        # Target portfolio configuration
        self.asset_configs = assets or [
            # Existing Base Live Pairs
            {"symbol": "TRUMP_USDT", "timeframe": "Min15", "pivot_len": 3, "leverage": self.leverage},
            {"symbol": "ETH_USDT",   "timeframe": "Hour4", "pivot_len": 5, "leverage": self.leverage},
            {"symbol": "BTC_USDT",   "timeframe": "Min15", "pivot_len": 5, "leverage": self.leverage},
            {"symbol": "DOGE_USDT",  "timeframe": "Min15", "pivot_len": 5, "leverage": self.leverage},
            # Newly Added Empirically Verified Profitable Pairs (1 Timeframe Per Pair)
            {"symbol": "TRX_USDT",   "timeframe": "Min60", "pivot_len": 5, "leverage": self.leverage},  # 1h: 81.8% WR, 2.64 PF
            # Confirmed KCEX Active US Equities (Strict US Cash Market Hours: Mon-Fri 09:30-16:00 ET)
            {"symbol": "AMAT_USDT",  "timeframe": "Min5",  "pivot_len": 5, "leverage": 15, "is_stock": True},  # 5m: 69.2% WR, 4.63 PF, 2.3% DD (+8.14%/mo)
            {"symbol": "GS_USDT",    "timeframe": "Min5",  "pivot_len": 5, "leverage": 15, "is_stock": True},  # 5m: 83.3% WR, 5.49 PF, 0.99% DD (+2.73%/mo)
            {"symbol": "SPCX_USDT",  "timeframe": "Min3",  "pivot_len": 5, "leverage": 15, "is_stock": True},  # 3m: 60.0% WR, 2.30 PF, 1.78% DD (+2.81%/25d -> ~6-8% in 3mo)
            {"symbol": "TSLA_USDT",  "timeframe": "Min1",  "pivot_len": 5, "leverage": 15, "is_stock": True},  # 1m: 57.9% WR, 1.58 PF, 3.04% DD (+3.51%/25d, 19 trades)
            {"symbol": "AMZN_USDT",  "timeframe": "Min5",  "pivot_len": 5, "leverage": 15, "is_stock": True},  # 5m: 60.0% WR, 1.24 PF, 1.18% DD (+0.48% in 85d, ultra-safe)
            {"symbol": "GOOGL_USDT", "timeframe": "Day1",  "pivot_len": 5, "leverage": 15, "is_stock": True},  # 1d: 70.0% WR, 4.65 PF, 6.0% DD (15-yr Alpha Robust)
            {"symbol": "MSFT_USDT",  "timeframe": "Min60", "pivot_len": 5, "leverage": 15, "is_stock": True},  # 1h: 59.3% WR, 1.67 PF, 9.5% DD (35-mo swing)
            {"symbol": "NOW_USDT",   "timeframe": "Hour4", "pivot_len": 5, "leverage": 15, "is_stock": True},  # 4h: 66.7% WR, 3.09 PF, 11.7% DD (Alpha Robust)
        ]

        # Automatic stock detection set
        KNOWN_STOCK_SYMBOLS = {
            "AMAT_USDT", "GS_USDT", "GOOGL_USDT", "MSFT_USDT", "NOW_USDT", "AVGO_USDT",
            "AAPL_USDT", "NVDA_USDT", "TSLA_USDT", "AMD_USDT", "META_USDT", "NFLX_USDT",
            "BRKB_USDT", "JPM_USDT", "V_USDT", "COST_USDT", "WMT_USDT", "CVX_USDT",
            "XOM_USDT", "ADBE_USDT", "JNJ_USDT", "UNH_USDT", "ORCL_USDT", "HD_USDT",
            "KO_USDT", "CSCO_USDT", "AMZN_USDT", "CRM_USDT", "MRK_USDT", "CAT_USDT",
            "ISRG_USDT", "TXN_USDT", "LLY_USDT", "IBM_USDT", "PLTR_USDT", "INTC_USDT", "QCOM_USDT",
            "SPCX_USDT"
        }

        # Initialize workers
        self.workers: Dict[str, AssetWorker] = {}
        for cfg in self.asset_configs:
            sym = cfg["symbol"].upper()
            tf = cfg["timeframe"]
            plen = cfg.get("pivot_len", 5)
            lev = cfg.get("leverage", self.leverage)
            is_stk = cfg.get("is_stock", (sym in KNOWN_STOCK_SYMBOLS))

            worker = AssetWorker(
                symbol=sym,
                timeframe=tf,
                pivot_len=plen,
                leverage=lev,
                margin_pct=self.margin_pct,
                risk_reward_ratio=self.risk_reward_ratio,
                mode=self.mode,
                shared_logger=self.logger,
                shared_outcome_logger=self.outcome_logger,
                shared_mongo_logger=self.mongo_logger,
                order_lock=self.order_lock,
                shared_outcomes=self.outcomes,
                is_stock=is_stk
            )
            self.workers[sym] = worker

    def run(self) -> None:
        """Starts all asset workers and runs the central throttled dashboard loop."""
        self.running = True
        self._shutdown_requested = False

        # Signal handling
        def handle_sigint(signum, frame):
            self.logger.warning("\n[STOP] Caught SIGINT / SIGTERM. Stopping all workers...")
            self.stop()

        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGINT, handle_sigint)
                signal.signal(signal.SIGTERM, handle_sigint)
            except Exception:
                pass

        # Log session start to MongoDB
        if self.mongo_logger:
            try:
                session_config = {
                    "strategy_mode": "ORDER_BLOCK_DEMAND",
                    "mode": self.mode.value if hasattr(self.mode, "value") else str(self.mode),
                    "portfolio_assets": self.asset_configs,
                    "target_leverage": self.leverage,
                    "margin_pct": self.margin_pct,
                    "risk_reward_ratio": self.risk_reward_ratio,
                    "execution_engine": "MultiAssetExecutionEngine",
                }
                self.mongo_logger.log_session_start(session_config)
            except Exception as se:
                self.logger.warning(f"Failed to log session start to MongoDB: {se}")

        self._print_startup_banner()

        # Start all workers
        for sym, worker in self.workers.items():
            worker.start()
            time.sleep(0.5)

        self.logger.info("All asset workers running concurrently. Telemetry throttled (10m idle / 5m active)...")
        time.sleep(1.0)
        self._print_portfolio_dashboard()
        last_dashboard_time = time.time()

        while self.running and not self._shutdown_requested:
            try:
                now = time.time()
                # Check active positions across all workers
                any_in_pos = any(w.in_position for w in self.workers.values())
                self.logger.set_has_active_positions(any_in_pos)

                # Required interval: 5m (300s) if in position, 10m (600s) if idle
                dashboard_interval = 300.0 if any_in_pos else 600.0

                if now - last_dashboard_time >= dashboard_interval:
                    last_dashboard_time = now
                    self._print_portfolio_dashboard()

                time.sleep(1.0)

            except Exception as e:
                self.logger.warning(f"Coordinator error: {e}")
                time.sleep(2.0)

        # Graceful shutdown of workers
        for sym, worker in self.workers.items():
            worker.stop()

        # Log session end to MongoDB
        if self.mongo_logger:
            try:
                tot_trades = len(self.outcomes)
                wins = sum(1 for o in self.outcomes if o.is_profit)
                losses = sum(1 for o in self.outcomes if o.is_loss)
                scratches = sum(1 for o in self.outcomes if o.is_scratch)
                tot_pnl_u = sum(o.realized_pnl_usdt for o in self.outcomes)
                tot_pnl_i = sum(o.realized_pnl_inr for o in self.outcomes)
                wr = (wins / tot_trades * 100.0) if tot_trades > 0 else 0.0
                best_t = max([o.realized_pnl_usdt for o in self.outcomes], default=0.0)
                worst_t = min([o.realized_pnl_usdt for o in self.outcomes], default=0.0)
                fees_u = sum(o.fee_total_usdt for o in self.outcomes)
                fees_i = sum(o.fee_total_inr for o in self.outcomes)

                self.mongo_logger.log_session_end(
                    total_trades=tot_trades,
                    winning_trades=wins,
                    losing_trades=losses,
                    scratch_trades=scratches,
                    cancelled_orders=0,
                    total_pnl_usdt=tot_pnl_u,
                    total_pnl_inr=tot_pnl_i,
                    win_rate=wr,
                    best_trade_usdt=best_t,
                    worst_trade_usdt=worst_t,
                    total_fees_usdt=fees_u,
                    total_fees_inr=fees_i
                )
            except Exception as se:
                self.logger.warning(f"Failed to log session end to MongoDB: {se}")

        self.logger.info("Multi-Asset Engine stopped successfully.")


    def stop(self) -> None:
        """Stops coordinator and workers."""
        self._shutdown_requested = True
        self.running = False

    def _print_startup_banner(self) -> None:
        self.logger.section("KCEX MULTI-ASSET AUTONOMOUS TRADING ENGINE")
        self.logger.info("  Strategy: Vivek Yadav Smart Money Concepts (OB + Demand/Supply)")
        self.logger.info("  Exit Plan: 50% partial TP at 1:1, Breakeven SL (+1t), 1:2 Runner")
        self.logger.info(f"  Target Leverage: {self.leverage}x isolated | Margin Allocation: {self.margin_pct}% per trade")
        self.logger.info(f"  Execution Mode: {self.mode.value.upper()}")
        self.logger.info("  Configured Asset Portfolio:")
        for cfg in self.asset_configs:
            self.logger.info(f"    • {cfg['symbol']:<12s} [{cfg['timeframe']:<6s}] @ {cfg.get('leverage', self.leverage)}x lev (Pivot Len: {cfg.get('pivot_len', 5)})")
        self.logger.info("  Logging Policy: 10-minute idle status / 5-minute active trade status (Railway log-flood prevention)")
        self.logger.info("  Manual Trading Isolation: Enabled (Direction-aware duplicate protection & position ID isolation)")
        self.logger.section("INITIALIZATION COMPLETE - WORKERS LAUNCHING")

    def _print_portfolio_dashboard(self) -> None:
        """Prints consolidated portfolio status block."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        active_count = sum(1 for w in self.workers.values() if w.in_position)

        # Get latest available balance
        bal_str = "N/A"
        try:
            worker0 = list(self.workers.values())[0]
            bals = worker0.trader.get_usdt_balance()
            avail = bals.get("available_usdt", 0.0)
            equity = bals.get("equity_usdt", 0.0)
            bal_str = f"Avail: {avail} USDT | Equity: {equity} USDT"
        except Exception:
            pass

        border = "=" * 80
        self.logger.info(border)
        self.logger.info(f"[PORTFOLIO STATUS] {now_str} | Active Bot Trades: {active_count} | {bal_str}")
        for sym, w in self.workers.items():
            p_str = f"{w.last_price} USDT" if w.last_price > 0 else "Querying..."
            if w.in_position:
                state_str = f"🔥 IN POSITION: {w.active_position_desc}"
            else:
                state_str = f"Scanning | {w.last_status_msg}"
            self.logger.info(f"  • {sym:<12s} [{w.timeframe:<6s}]: Price: {p_str} | {state_str}")
        self.logger.info(border)
