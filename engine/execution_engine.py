"""
PropDAO Unified Multi-Asset Execution Engine
============================================
Coordinates automated strategy execution across both LIVE and PAPER modes on PropDAO:
1. Multi-symbol SMC OrderBlockDemandStrategy signal evaluation (1m..4h timeframes).
2. Dynamic risk sizing based on live roomUsd budget.
3. Order submission (Market, Limit, Trigger) and bracket management (SL & TP).
4. Automated 1:1 Partial TP + Breakeven Lock & 1:2 Runner TP execution.
5. Real-time PropDAO risk guard and drawdown breach protection.
6. Unified high-visibility logging and diagnostic telemetry (heartbeat, watchlist monitor, PnL).
"""

from __future__ import annotations
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Union, List
from propdao.client import PropDAOClient, PropDAOError
from propdao.market import PropDAOMarket
from propdao.models import (
    EngineMode, OrderSide, OrderDirection, TradeSignal,
    Position, OrderType, TradeOutcome
)
from propdao.risk_manager import PropDAORiskManager
from propdao.order_manager import PropDAOOrderManager
from propdao.account_manager import PropDAOAccountManager
from propdao.paper_engine import PropDAOPaperEngine
from engine.position_tracker import PositionTracker
from strategies.order_block_demand.order_block_demand import OrderBlockDemandStrategy

logger = logging.getLogger("ExecutionEngine")


class ExecutionEngine:
    """
    Main automated trade execution engine for PropDAO supporting concurrent multi-symbol trading.
    """

    def __init__(
        self,
        symbol: Optional[str] = None,
        symbols: Optional[Union[str, List[str]]] = None,
        timeframe: str = "1m",
        mode: EngineMode = EngineMode.PAPER,
        api_key: Optional[str] = None,
        account_id: Optional[str] = None,
        starting_balance: float = 25000.0,
        risk_fraction: float = 0.25,        # 25% of roomUsd budget per trade
        leverage: float = 1.0,
        enable_partial_tp: bool = True,     # 50% partial exit at 1:1 RR + Breakeven lock
        loop_interval_seconds: float = 3.0, # PropDAO recommends polling no faster than 2-3s
        heartbeat_interval_seconds: float = 15.0,
        max_open_positions: int = 3,
        pivot_len: Optional[int] = None
    ):
        # 1. Parse and normalize symbols universe
        raw_symbols = symbols if symbols is not None else (symbol or os.getenv("PROPDAO_SYMBOLS") or os.getenv("PROPDAO_SYMBOL") or "BTCUSDC")
        if isinstance(raw_symbols, str):
            parts = [p.strip() for p in raw_symbols.replace(";", ",").split(",") if p.strip()]
            self.symbols = [PropDAOMarket.normalize_symbol(p) for p in parts]
        elif isinstance(raw_symbols, (list, tuple)):
            self.symbols = [PropDAOMarket.normalize_symbol(str(p)) for p in raw_symbols if p]
        else:
            self.symbols = ["BTCUSDC"]

        if not self.symbols:
            self.symbols = ["BTCUSDC"]

        # Primary symbol for backward compatibility
        self.symbol = self.symbols[0]

        self.timeframe = timeframe
        self.mode = mode
        self.risk_fraction = risk_fraction
        self.preferred_leverage = leverage
        self.enable_partial_tp = enable_partial_tp
        self.loop_interval = max(2.0, loop_interval_seconds)
        self.heartbeat_interval = max(5.0, heartbeat_interval_seconds)
        self.max_open_positions = max(1, max_open_positions)
        self._last_heartbeat_time: float = 0.0

        self.running: bool = False
        self.account_info: Dict[str, Any] = {}

        # 2. Initialize Client or Local Mock Engine
        if self.mode in (EngineMode.LIVE, EngineMode.PAPER, "live", "paper"):
            is_paper = self.mode in (EngineMode.PAPER, "paper")
            mode_label = "PAPER (DEMO / TRIAL)" if is_paper else "LIVE (EVALUATION / REAL)"
            logger.info("Initializing Execution Engine in %s MODE on PropDAO...", mode_label)

            self.client: Union[PropDAOClient, PropDAOPaperEngine] = PropDAOClient(api_key=api_key)
            self.account_manager = PropDAOAccountManager(self.client)
            self.account_info = self.account_manager.get_preferred_account(
                requested_id=account_id,
                mode="paper" if is_paper else "live"
            )
            self.account_id = self.account_info["account_id"]
            ch_name = self.account_info.get("challenge", {}).get("name", self.account_info.get("live_stage", "Challenge"))
            logger.info("Connected to PropDAO Account: %s (%s | Status: %s)", self.account_id.upper(), ch_name, self.account_info.get("status"))
        else:
            logger.info("Initializing Execution Engine in LOCAL MOCK SIMULATION MODE (offline)...")
            self.client = PropDAOPaperEngine(
                starting_balance=starting_balance,
                account_id=account_id or "prop-paper-25k"
            )
            self.account_id = self.client.account_id
            self.account_manager = None
            self.account_info = {"status": "ACTIVE", "challenge": {"name": "Local Mock Simulator"}}

        # 3. Market Data Provider
        self.market = PropDAOMarket(client=self.client if isinstance(self.client, PropDAOClient) else None)
        self.timeframe = self.market.normalize_interval(self.timeframe)

        # 4. Risk & Order Managers
        self.risk_manager = PropDAORiskManager(
            client=self.client,
            market=self.market,
            account_id=self.account_id,
            risk_fraction_per_trade=self.risk_fraction,
            max_leverage_override=self.preferred_leverage
        )
        self.order_manager = PropDAOOrderManager(client=self.client, account_id=self.account_id)

        # 5. Multi-Symbol Strategy & Position Tracker Initialization
        p_len = pivot_len if pivot_len is not None else (3 if self.timeframe in ("1m", "3m", "5m") else 5)
        self.strategies: Dict[str, OrderBlockDemandStrategy] = {}
        self.trackers: Dict[str, PositionTracker] = {}

        for sym in self.symbols:
            self.strategies[sym] = OrderBlockDemandStrategy(
                market=self.market,
                symbol=sym,
                interval=self.timeframe,
                pivot_len=p_len,
                risk_reward_ratio=2.0,
                partial_tp_enabled=self.enable_partial_tp,
                cooldown_seconds=30.0
            )
            self.trackers[sym] = PositionTracker(
                symbol=sym,
                on_trade_closed_callback=self._on_trade_closed
            )

        # Backward compatibility aliases for primary symbol
        self.strategy = self.strategies[self.symbol]
        self.tracker = self.trackers[self.symbol]

        # Signal handlers for clean shutdown
        signal.signal(signal.SIGINT, self._handle_exit)
        signal.signal(signal.SIGTERM, self._handle_exit)

    def _on_trade_closed(self, outcome: TradeOutcome) -> None:
        """Callback when position is fully closed."""
        sym = outcome.symbol
        if sym in self.strategies:
            self.strategies[sym].on_trade_completed(outcome)
        hold_sec = int(outcome.close_time - outcome.open_time) if outcome.close_time and outcome.open_time else 0
        hold_str = f"{hold_sec // 60}m {hold_sec % 60}s"
        pnl_pct = (outcome.realized_pnl_usdc / (outcome.notional / outcome.leverage)) * 100 if outcome.notional > 0 else 0.0

        gross_sign = "+$" if outcome.gross_pnl >= 0 else "-$"
        net_sign = "+$" if outcome.realized_pnl_usdc >= 0 else "-$"

        logger.info(
            "\n" + "=" * 76 + "\n"
            "🏁 [TRADE COMPLETED] Trade #%d Closed (%s %s)\n"
            "   ├─ Entry Price:   $%.4f\n"
            "   ├─ Exit Price:    $%.4f\n"
            "   ├─ Position Qty:  %.5f (%.1fx Leverage Isolated)\n"
            "   ├─ Gross PnL:     %s%.2f\n"
            "   ├─ Fees Paid:     -$%.2f\n"
            "   ├─ Net PnL:       %s%.2f (%+.2f%%)\n"
            "   ├─ Exit Reason:   %s\n"
            "   ├─ Hold Duration: %s\n"
            "   └─ Zone ID:       %s\n"
            + "=" * 76,
            outcome.trade_id, outcome.direction.value, outcome.symbol,
            outcome.entry_price, outcome.exit_price,
            outcome.quantity, outcome.leverage,
            gross_sign, abs(outcome.gross_pnl), outcome.fee,
            net_sign, abs(outcome.realized_pnl_usdc), pnl_pct,
            outcome.exit_reason, hold_str, outcome.smc_zone_id or "N/A"
        )

    def _handle_exit(self, signum: int, frame: Any) -> None:
        logger.info("[SHUTDOWN] Received termination signal. Stopping execution loop cleanly...")
        self.running = False

    def start(self) -> None:
        """Starts main automated trading loop."""
        self.running = True

        mode_name = self.mode.value.upper() if hasattr(self.mode, "value") else str(self.mode).upper()
        ch_name = self.account_info.get("challenge", {}).get("name", self.account_info.get("live_stage", "Challenge"))
        status_name = self.account_info.get("status", "ACTIVE")

        # Initial risk query for startup banner
        try:
            init_risk = self.risk_manager.get_risk_state()
            bal_str = f"${init_risk.balance:,.2f}"
            eq_str = f"${init_risk.equity:,.2f}"
            floor_str = f"${init_risk.floor:,.2f}"
            room_str = f"${init_risk.room_usd:,.2f} ({init_risk.room_pct:.2f}%)"
        except Exception:
            bal_str, eq_str, floor_str, room_str = "Querying...", "Querying...", "Querying...", "Querying..."

        print("\n" + "=" * 80)
        print("     PROPDAO MULTI-ASSET AUTOMATED TRADING BOT - VIVEK YADAV OB+DEMAND SMC")
        print("=" * 80)
        print(f"  Environment Mode:  [{mode_name}]")
        print(f"  Account ID:        {self.account_id.upper()} ({ch_name})")
        print(f"  Account Status:    {status_name}")
        print(f"  Balance / Equity:  {bal_str} / {eq_str}")
        print(f"  Drawdown Floor:    {floor_str} | Available Room: {room_str}")
        print(f"  Candle Timeframe:  {self.timeframe}")
        print(f"  Active Watchlist:  {len(self.symbols)} Assets: {', '.join(self.symbols)}")
        print(f"  Max Concurrent:    {self.max_open_positions} positions")
        print(f"  Leverage:          {self.preferred_leverage:.1f}x (Isolated Margin)")
        print(f"  Risk Budget:       {self.risk_fraction * 100:.1f}% of roomUsd budget per trade")
        print(f"  Target Geometry:   1:1 Partial TP (50% exit + Breakeven Lock) -> 1:2 Runner TP")
        print(f"  Polling Cadence:   Every {self.loop_interval:.1f}s loop | {self.heartbeat_interval:.1f}s heartbeat")
        print(f"  Telemetry Log:     propdao_bot.log")
        print("=" * 80 + "\n")

        # Warm up strategies
        for strat in self.strategies.values():
            try:
                strat.start()
            except Exception as e:
                logger.warning("Could not warm up strategy for %s: %s", strat.symbol, e)

        consecutive_errors = 0

        while self.running:
            try:
                self._iteration()
                consecutive_errors = 0
            except PropDAOError as e:
                if e.is_breached:
                    logger.critical("[CRITICAL BREACH] PropDAO Account is BREACHED (%s). Halting execution.", e.message)
                    break
                elif e.is_rate_limit:
                    logger.warning("[RATE LIMIT] Rate limit encountered: %s. Pausing 5s...", e)
                    time.sleep(5.0)
                else:
                    logger.error("[API ERROR] PropDAO error: %s", e)
            except Exception as e:
                consecutive_errors += 1
                logger.exception("[ERROR] Unexpected error in execution iteration: %s", e)
                if consecutive_errors > 10:
                    logger.critical("[FATAL] Too many consecutive errors. Stopping bot.")
                    break

            time.sleep(self.loop_interval)

        self.stop()

    def stop(self) -> None:
        """Stops trading and performs cleanup."""
        self.running = False
        for strat in self.strategies.values():
            try:
                strat.stop()
            except Exception:
                pass
        logger.info("[STOPPED] Bot execution stopped successfully.")

    def _iteration(self) -> None:
        """Single tick iteration across the multi-symbol universe."""
        # 1. Protective Risk Check & Guard
        safe = self.risk_manager.check_safety_and_guard()
        if not safe:
            return

        # 2. Synchronize all open positions from exchange
        positions = self.order_manager.refresh_positions()
        pos_by_sym = {p.symbol: p for p in positions}

        # 3. Manage positions and track state for each symbol
        for sym in self.symbols:
            cur_price = self.market.get_current_price(sym)
            if cur_price <= 0:
                continue

            if isinstance(self.client, PropDAOPaperEngine):
                self.client.update_mark_price(sym, cur_price)

            active_pos = pos_by_sym.get(sym)
            tracker = self.trackers.get(sym)

            if active_pos:
                self._manage_open_position(sym, active_pos, cur_price)
            else:
                if tracker and tracker.has_open_position:
                    logger.info("[%s POSITION CLOSED] Position cleared from exchange. Registering trade close...", sym)
                    trade_record = tracker.register_final_exit(exit_price=cur_price, reason="Exit Filled")
                    strat = self.strategies.get(sym)
                    if strat and hasattr(strat, "on_trade_closed"):
                        realized = getattr(trade_record, "realized_pnl", 0.0) if trade_record else 0.0
                        strat.on_trade_closed(pnl=realized, exit_price=cur_price)

        # 4. Structured Heartbeat Log
        self._log_heartbeat(pos_by_sym)

        # 5. Scan for new entries across symbols not currently in position
        active_positions_count = len(pos_by_sym)
        if active_positions_count >= self.max_open_positions:
            return

        now = time.time()
        for sym in self.symbols:
            if sym in pos_by_sym:
                continue
            if active_positions_count >= self.max_open_positions:
                break

            strat = self.strategies.get(sym)
            if not strat or not strat.should_generate_signal(now):
                continue

            cur_price = self.market.get_current_price(sym)
            if cur_price <= 0:
                continue

            signal = strat.generate_signal(sym)
            if signal:
                opened = self._execute_entry_signal(sym, signal, cur_price)
                if opened:
                    active_positions_count += 1
                    positions = self.order_manager.refresh_positions()
                    pos_by_sym = {p.symbol: p for p in positions}

    def _log_heartbeat(self, open_pos_map: Dict[str, Position]) -> None:
        """Emits structured multi-symbol heartbeat log."""
        now = time.time()
        if (now - self._last_heartbeat_time) < self.heartbeat_interval:
            return
        self._last_heartbeat_time = now

        try:
            risk = self.risk_manager.get_risk_state()
        except Exception:
            return

        ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        mode_str = self.mode.value.upper() if hasattr(self.mode, "value") else str(self.mode).upper()

        lines = [
            "\n" + "-" * 80,
            f"[HEARTBEAT] {ts_str} | PropDAO {mode_str} | Acct: {self.account_id.upper()} | TF: {self.timeframe}",
            f"  * Capital:     Equity: ${risk.equity:,.2f} | Balance: ${risk.balance:,.2f} | Floor: ${risk.floor:,.2f}",
            f"  * Drawdown:    Room: ${risk.room_usd:,.2f} ({risk.room_pct:.2f}% above floor) | Max Concur: {self.max_open_positions}",
            f"  * Positions:   {len(open_pos_map)} Active / {len(self.symbols)} Monitored",
        ]

        if open_pos_map:
            lines.append("  * Active Positions:")
            for sym, pos in open_pos_map.items():
                cur_px = self.market.get_current_price(sym)
                is_long = pos.side == OrderSide.BUY
                unrealized = (cur_px - pos.entry) * pos.qty if is_long else (pos.entry - cur_px) * pos.qty
                tracker = self.trackers.get(sym)
                hold_sec = int(time.time() - tracker.entry_time) if (tracker and tracker.entry_time > 0) else 0
                hold_str = f"{hold_sec // 60}m {hold_sec % 60}s"
                sl_px = tracker.sl_price if tracker else (pos.sl_price or 0.0)
                tp_px = tracker.target_1to2_price if tracker else (pos.tp_price or 0.0)
                pnl_sign = "+$" if unrealized >= 0 else "-$"
                lines.append(
                    f"     - {sym}: {pos.side.value} {pos.qty:.4f} @ ${pos.entry:.4f} (Mark: ${cur_px:.4f} | PnL: {pnl_sign}{abs(unrealized):.2f} | SL: ${sl_px:.4f} | TP: ${tp_px:.4f} | Hold: {hold_str})"
                )

        scan_parts = []
        for sym in self.symbols:
            strat = self.strategies.get(sym)
            px = self.market.get_current_price(sym)
            zones = len(strat.active_zones) if strat else 0
            in_pos = " [IN POS]" if sym in open_pos_map else ""
            scan_parts.append(f"{sym}: ${px:.2f} ({zones}z){in_pos}")

        lines.append(f"  * Watchlist:   {' | '.join(scan_parts[:6])}")
        if len(scan_parts) > 6:
            lines.append(f"                 {' | '.join(scan_parts[6:])}")
        lines.append("-" * 80)

        logger.info("\n".join(lines))

    def _manage_open_position(self, sym: str, pos: Position, cur_price: float) -> None:
        """Monitors active position for multi-tier TP1..TP5 partial profit taking and runner trailing."""
        tracker = self.trackers.get(sym)
        if not tracker:
            return

        if not tracker.has_open_position:
            # Sync existing position into tracker
            contract = self.market.get_contract_detail(sym)
            prec = getattr(contract, "price_precision", 4)
            sl_px = pos.sl_price or (pos.entry * 0.98 if pos.side == OrderSide.BUY else pos.entry * 1.02)
            dist = abs(pos.entry - sl_px)
            tp_1to1 = round(pos.entry + dist if pos.side == OrderSide.BUY else pos.entry - dist, prec)
            tp_1to2 = round(pos.entry + (2.0 * dist) if pos.side == OrderSide.BUY else pos.entry - (2.0 * dist), prec)
            is_long = pos.side == OrderSide.BUY
            default_tps = [
                {"id": "tp1", "price": round(pos.entry + (1.0 * dist) if is_long else pos.entry - (1.0 * dist), prec), "size_pct": 50.0},
                {"id": "tp2", "price": round(pos.entry + (2.0 * dist) if is_long else pos.entry - (2.0 * dist), prec), "size_pct": 50.0}
            ]
            tracker.register_entry(pos, target_1to1_price=tp_1to1, target_1to2_price=tp_1to2, sl_price=sl_px, tp_levels=default_tps)

        # Multi-Tier TP Evaluation (TP1..TP5)
        triggered_tp = tracker.get_triggered_tp(cur_price)
        if triggered_tp and tracker.check_hold_duration_satisfied():
            tp_id = triggered_tp["id"]
            tp_px = triggered_tp["price"]
            size_pct = triggered_tp["size_pct"]

            # Check if this is the final remaining TP
            unhit_tps = [t for t in tracker.tp_levels if not t["hit"] and t["id"] != tp_id]
            is_final_tp = len(unhit_tps) == 0

            # Compute close fraction relative to remaining position
            remaining_qty = pos.qty
            target_close_qty = tracker.initial_qty * (size_pct / 100.0)
            if is_final_tp or target_close_qty >= remaining_qty * 0.95:
                close_fraction = 1.0
            else:
                close_fraction = min(1.0, max(0.05, target_close_qty / remaining_qty))

            closed_qty = remaining_qty * close_fraction
            gross = abs(cur_price - pos.entry) * closed_qty
            fee = (closed_qty * cur_price) * 0.00045
            net_pnl = gross - fee
            gross_sign = "+$" if gross >= 0 else "-$"
            net_sign = "+$" if net_pnl >= 0 else "-$"

            logger.info(
                "\n" + "=" * 76 + "\n"
                "🎯 [%s TARGET HIT] %s touched $%.4f (Target: $%.4f)!\n"
                "   ├─ Closing %.1f%% Position: %.5f %s at Market\n"
                "   ├─ Gross Profit:         %s%.2f\n"
                "   ├─ Estimated Fee:        -$%.2f\n"
                "   ├─ Net Realized Profit:  %s%.2f\n"
                "   └─ Remaining Exposure:   %.5f %s\n"
                + "=" * 76,
                tp_id.upper(), sym, cur_price, tp_px,
                close_fraction * 100.0, closed_qty, sym,
                gross_sign, abs(gross), fee, net_sign, abs(net_pnl),
                max(0.0, remaining_qty - closed_qty), sym
            )

            try:
                self.order_manager.close_position_market(pos.id, percent=close_fraction)
                tracker.register_partial_exit(closed_qty, cur_price, net_pnl, fee)
                tracker.mark_tp_hit(tp_id)

                # Breakeven lock once TP1 is hit
                if self.enable_partial_tp and not tracker.breakeven_locked:
                    contract = self.market.get_contract_detail(sym)
                    self.order_manager.move_sl_to_breakeven(
                        pos, buffer_ticks=1, tick_size=contract.price_unit, current_mark=cur_price
                    )
                    tracker.breakeven_locked = True
                    logger.info("🛡️ [%s BREAKEVEN LOCKED] Stop Loss adjusted to entry ($%.4f + 1 tick buffer)", sym, pos.entry)

            except Exception as e:
                logger.error("[%s TP EXECUTION ERROR] Failed executing %s: %s", sym, tp_id.upper(), e)

    def _execute_entry_signal(self, sym: str, signal: TradeSignal, current_price: float) -> bool:
        """Executes a confirmed strategy signal with risk budgeting."""
        tracker = self.trackers.get(sym)
        strat = self.strategies.get(sym)
        raw_sl_px = signal.metadata.get("stop_loss_price")
        zone_id = signal.metadata.get("zone_id")

        if not raw_sl_px:
            logger.warning("[%s SIGNAL REJECTED] Missing stop loss price.", sym)
            return False

        contract = self.market.get_contract_detail(sym)
        prec = getattr(contract, "price_precision", 4)
        pu = getattr(contract, "price_unit", 0.0001)
        is_long = signal.direction == OrderDirection.LONG
        side = OrderSide.BUY if is_long else OrderSide.SELL

        # 1. Pre-execution setup validation: check if market price has already breached stop-loss
        if is_long and current_price <= (raw_sl_px + pu):
            logger.warning(
                "[%s SETUP EXPIRED] Current mark ($%.4f) has dropped to/below Stop Loss ($%.4f). Skipping stale signal.",
                sym, current_price, raw_sl_px
            )
            if strat:
                strat.on_trade_rejected()
            return False
        elif (not is_long) and current_price >= (raw_sl_px - pu):
            logger.warning(
                "[%s SETUP EXPIRED] Current mark ($%.4f) has risen to/above Stop Loss ($%.4f). Skipping stale signal.",
                sym, current_price, raw_sl_px
            )
            if strat:
                strat.on_trade_rejected()
            return False

        # 2. Dynamic execution calibration relative to live market fill price
        exec_entry_px = current_price
        rr = getattr(strat, "risk_reward_ratio", 2.0) if strat else 2.0

        if is_long:
            exec_sl_px = round(min(raw_sl_px, current_entry := (exec_entry_px - (pu * 2))), prec)
            sl_dist = max(pu * 2, exec_entry_px - exec_sl_px)
            exec_tp_1to1_px = round(exec_entry_px + sl_dist, prec)
            exec_tp_px = round(exec_entry_px + (rr * sl_dist), prec)
            if exec_tp_px <= exec_entry_px:
                exec_tp_px = round(exec_entry_px + (pu * 2), prec)
        else:
            exec_sl_px = round(max(raw_sl_px, exec_entry_px + (pu * 2)), prec)
            sl_dist = max(pu * 2, exec_sl_px - exec_entry_px)
            exec_tp_1to1_px = round(exec_entry_px - sl_dist, prec)
            exec_tp_px = round(exec_entry_px - (rr * sl_dist), prec)
            if exec_tp_px >= exec_entry_px:
                exec_tp_px = round(exec_entry_px - (pu * 2), prec)

        # 3. Calculate sizing constrained by roomUsd using live execution prices
        try:
            qty, lev, margin = self.risk_manager.calculate_order_sizing(
                symbol=sym,
                entry_price=exec_entry_px,
                stop_loss_price=exec_sl_px,
                preferred_leverage=self.preferred_leverage
            )
        except Exception as e:
            logger.warning("[%s SIZING ERROR] Could not size order: %s", sym, e)
            if strat:
                strat.on_trade_rejected()
            return False

        risk = self.risk_manager.get_risk_state()
        risk_budget = risk.room_usd * self.risk_fraction
        sl_dist_pct = (sl_dist / exec_entry_px) * 100

        logger.info(
            "\n" + "=" * 76 + "\n"
            "[SIGNAL CONFIRMED] %s %s Setup Triggered from Zone %s\n"
            "  * Market Entry:    $%.4f (Live Mark Price)\n"
            "  * Stop Loss:       $%.4f (Distance: $%.4f | %.2f%%)\n"
            "  * 1:1 TP Target:   $%.4f (50%% partial exit + Breakeven Lock)\n"
            "  * 1:2 TP Target:   $%.4f (Final runner target)\n"
            "[RISK BUDGET SIZING]\n"
            "  * Room to Floor:   $%.2f\n"
            "  * Risk Allocation: %.1f%% of Room -> Budget: $%.2f\n"
            "  * Order Quantity:  %.5f %s ($%.2f notional)\n"
            "  * Leverage:        %.1fx (Isolated Initial Margin: $%.2f)\n"
            "  * Remaining Cash:  $%.2f\n"
            "[SUBMITTING ORDER] %s Market %.5f %s (SL: $%.4f | TP: $%.4f)...\n"
            + "=" * 76,
            signal.direction.value, sym, zone_id or "SMC_ZONE",
            exec_entry_px,
            exec_sl_px, sl_dist, sl_dist_pct,
            exec_tp_1to1_px,
            exec_tp_px,
            risk.room_usd,
            self.risk_fraction * 100, risk_budget,
            qty, sym, qty * exec_entry_px,
            lev, margin,
            risk.balance - margin,
            side.value, qty, sym, exec_sl_px, exec_tp_px
        )

        try:
            try:
                res = self.order_manager.open_market(
                    symbol=sym,
                    side=side,
                    qty=qty,
                    leverage=lev,
                    sl_price=exec_sl_px,
                    tp_price=exec_tp_px
                )
            except Exception as order_err:
                err_msg = str(order_err).lower()
                # If attached brackets were rejected due to micro-transit slippage boundary checks,
                # immediately submit a clean market order and attach brackets on the confirmed fill
                if any(k in err_msg for k in ("take-profit", "stop-loss", "winning side", "losing side", "400")):
                    logger.warning(
                        "[%s ATTACHED BRACKETS REJECTED] %s. Executing clean market order and syncing brackets post-fill...",
                        sym, order_err
                    )
                    res = self.order_manager.open_market(
                        symbol=sym,
                        side=side,
                        qty=qty,
                        leverage=lev
                    )
                else:
                    raise

            # Wait briefly for fill to register and fetch created position
            time.sleep(1.0)
            positions = self.order_manager.refresh_positions()
            created_pos = next((p for p in positions if p.symbol == sym), None)

            if created_pos and tracker:
                fill_entry = created_pos.entry
                fill_dist = abs(fill_entry - exec_sl_px)
                if fill_dist <= 1e-8:
                    fill_dist = fill_entry * 0.005

                # Recalibrate TP/SL precisely to actual confirmed fill entry
                if is_long:
                    confirmed_sl = round(min(exec_sl_px, fill_entry - (pu * 2)), prec)
                    confirmed_tp = round(fill_entry + (rr * fill_dist), prec)
                    confirmed_tp_1to1 = round(fill_entry + fill_dist, prec)
                else:
                    confirmed_sl = round(max(exec_sl_px, fill_entry + (pu * 2)), prec)
                    confirmed_tp = round(fill_entry - (rr * fill_dist), prec)
                    confirmed_tp_1to1 = round(fill_entry - fill_dist, prec)

                # Update position brackets on exchange to ensure active protection
                try:
                    self.order_manager.update_brackets(created_pos.id, sl_price=confirmed_sl, tp_price=confirmed_tp)
                except Exception as bracket_err:
                    logger.debug("[%s BRACKET SYNC] Brackets already applied or update skipped: %s", sym, bracket_err)

                # Configure Multi-Tier TP1..TP5 and SL1..SL5
                tp_levels = [
                    {"id": "tp1", "price": round(fill_entry + (1.0 * fill_dist) if is_long else fill_entry - (1.0 * fill_dist), prec), "size_pct": 20.0},
                    {"id": "tp2", "price": round(fill_entry + (1.5 * fill_dist) if is_long else fill_entry - (1.5 * fill_dist), prec), "size_pct": 20.0},
                    {"id": "tp3", "price": round(fill_entry + (2.0 * fill_dist) if is_long else fill_entry - (2.0 * fill_dist), prec), "size_pct": 20.0},
                    {"id": "tp4", "price": round(fill_entry + (2.5 * fill_dist) if is_long else fill_entry - (2.5 * fill_dist), prec), "size_pct": 20.0},
                    {"id": "tp5", "price": round(fill_entry + (3.0 * fill_dist) if is_long else fill_entry - (3.0 * fill_dist), prec), "size_pct": 20.0},
                ]
                sl_levels = [
                    {"id": "sl1", "price": confirmed_sl, "size_pct": 100.0}
                ]
                tracker.register_entry(
                    position=created_pos,
                    target_1to1_price=confirmed_tp_1to1,
                    target_1to2_price=confirmed_tp,
                    sl_price=confirmed_sl,
                    smc_zone_id=zone_id,
                    tp_levels=tp_levels,
                    sl_levels=sl_levels
                )
                c_margin = getattr(created_pos, "margin", getattr(created_pos, "margin_allocated", 0.0))
                logger.info(
                    "✅ [POSITION CONFIRMED] %s %.5f %s @ $%.4f (Margin: $%.2f, Lev: %.1fx, SL: $%.4f, TP: $%.4f, ID: %s)",
                    created_pos.side.value, created_pos.qty, sym,
                    created_pos.entry, c_margin, created_pos.leverage,
                    confirmed_sl, confirmed_tp, created_pos.id
                )
                return True
            else:
                logger.info("[%s ORDER SUBMITTED] Order sent, awaiting position synchronization in next tick...", sym)
                return True
        except Exception as e:
            logger.error("[%s SUBMISSION FAILED] Failed submitting entry order: %s", sym, e)
            if strat:
                strat.on_trade_rejected()
            return False
