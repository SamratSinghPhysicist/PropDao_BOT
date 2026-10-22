"""
PropDAO Unified Execution Engine
================================
Coordinates automated strategy execution across both LIVE and PAPER modes on PropDAO:
1. Signal evaluation from OrderBlockDemandStrategy.
2. Dynamic risk sizing based on live roomUsd budget.
3. Order submission (Market, Limit, Trigger) and bracket management (SL & TP).
4. Automated 1:1 Partial TP + Breakeven Lock & 1:2 Runner TP execution.
5. Real-time PropDAO risk guard and drawdown breach protection.
6. Unified high-visibility logging and diagnostic telemetry (heartbeat, SMC zone monitor, PnL).
"""

from __future__ import annotations
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Union
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
    Main automated trade execution engine for PropDAO.
    """

    def __init__(
        self,
        symbol: str = "BTCUSDC",
        timeframe: str = "15m",
        mode: EngineMode = EngineMode.PAPER,
        api_key: Optional[str] = None,
        account_id: Optional[str] = None,
        starting_balance: float = 25000.0,
        risk_fraction: float = 0.25,        # 25% of roomUsd budget
        leverage: float = 2.0,
        enable_partial_tp: bool = True,     # 50% partial exit at 1:1 RR + Breakeven lock
        loop_interval_seconds: float = 3.0, # PropDAO recommends polling no faster than 2-3s
        heartbeat_interval_seconds: float = 15.0
    ):
        self.symbol = symbol.upper()
        self.timeframe = timeframe
        self.mode = mode
        self.risk_fraction = risk_fraction
        self.preferred_leverage = leverage
        self.enable_partial_tp = enable_partial_tp
        self.loop_interval = max(2.0, loop_interval_seconds)
        self.heartbeat_interval = max(5.0, heartbeat_interval_seconds)
        self._last_heartbeat_time: float = 0.0

        self.running: bool = False
        self.account_info: Dict[str, Any] = {}

        # 1. Initialize Client or Local Mock Engine
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

        # 2. Market Data Provider
        self.market = PropDAOMarket(client=self.client if isinstance(self.client, PropDAOClient) else None)

        # 3. Risk & Order Managers
        self.risk_manager = PropDAORiskManager(
            client=self.client,
            market=self.market,
            account_id=self.account_id,
            risk_fraction_per_trade=self.risk_fraction,
            max_leverage_override=self.preferred_leverage
        )
        self.order_manager = PropDAOOrderManager(client=self.client, account_id=self.account_id)

        # 4. Strategy Initialization
        self.strategy = OrderBlockDemandStrategy(
            market=self.market,
            symbol=self.symbol,
            interval=self.timeframe,
            pivot_len=5,
            risk_reward_ratio=2.0,
            partial_tp_enabled=self.enable_partial_tp,
            cooldown_seconds=30.0
        )

        # 5. Position Tracker
        self.tracker = PositionTracker(
            symbol=self.symbol,
            on_trade_closed_callback=self._on_trade_closed
        )

        # Signal handlers for clean shutdown
        signal.signal(signal.SIGINT, self._handle_exit)
        signal.signal(signal.SIGTERM, self._handle_exit)

    def _on_trade_closed(self, outcome: TradeOutcome) -> None:
        """Callback when position is fully closed."""
        self.strategy.on_trade_completed(outcome)
        hold_sec = int(outcome.close_time - outcome.open_time) if outcome.close_time and outcome.open_time else 0
        hold_str = f"{hold_sec // 60}m {hold_sec % 60}s"
        pnl_pct = (outcome.realized_pnl_usdc / (outcome.notional / outcome.leverage)) * 100 if outcome.notional > 0 else 0.0

        logger.info(
            "\n" + "=" * 76 + "\n"
            "🏁 [TRADE COMPLETED] Trade #%d Closed (%s %s)\n"
            "   ├─ Entry Price:   $%.4f\n"
            "   ├─ Exit Price:    $%.4f\n"
            "   ├─ Position Qty:  %.5f (%.1fx Leverage Isolated)\n"
            "   ├─ Gross PnL:     %+$%.2f\n"
            "   ├─ Fees Paid:     -$%.2f\n"
            "   ├─ Net PnL:       %+$%.2f (%+.2f%%)\n"
            "   ├─ Exit Reason:   %s\n"
            "   ├─ Hold Duration: %s\n"
            "   └─ Zone ID:       %s\n"
            + "=" * 76,
            outcome.trade_id, outcome.direction.value, outcome.symbol,
            outcome.entry_price, outcome.exit_price,
            outcome.quantity, outcome.leverage,
            outcome.gross_pnl, outcome.fee, outcome.realized_pnl_usdc, pnl_pct,
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
        print("          PROPDAO AUTOMATED TRADING BOT - VIVEK YADAV OB+DEMAND SMC")
        print("=" * 80)
        print(f"  Environment Mode:  [{mode_name}]")
        print(f"  Account ID:        {self.account_id.upper()} ({ch_name})")
        print(f"  Account Status:    {status_name}")
        print(f"  Balance / Equity:  {bal_str} / {eq_str}")
        print(f"  Drawdown Floor:    {floor_str} | Available Room: {room_str}")
        print(f"  Trading Pair:      {self.symbol} @ {self.timeframe} Timeframe")
        print(f"  Leverage:          {self.preferred_leverage:.1f}x (Isolated Margin)")
        print(f"  Risk Budget:       {self.risk_fraction * 100:.1f}% of roomUsd budget per trade")
        print(f"  Target Geometry:   1:1 Partial TP (50% exit + Breakeven Lock) -> 1:2 Runner TP")
        print(f"  Polling Cadence:   Every {self.loop_interval:.1f}s loop | {self.heartbeat_interval:.1f}s heartbeat")
        print(f"  Telemetry Log:     propdao_bot.log")
        print("=" * 80 + "\n")

        # Warm up strategy
        self.strategy.start()

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
        self.strategy.stop()
        logger.info("[STOPPED] Bot execution stopped successfully.")

    def _iteration(self) -> None:
        """Single tick iteration of the trading engine."""
        # 1. Update mark price
        cur_price = self.market.get_current_price(self.symbol)
        if cur_price <= 0:
            return

        # If in paper mode, update virtual exchange mark
        if isinstance(self.client, PropDAOPaperEngine):
            self.client.update_mark_price(self.symbol, cur_price)

        # 2. Risk check & protective guard
        safe = self.risk_manager.check_safety_and_guard()
        if not safe:
            return

        # 3. Synchronize open positions
        positions = self.order_manager.refresh_positions()
        active_pos = next((p for p in positions if p.symbol == self.symbol), None)

        # 4. If we have an active position, monitor brackets and partial take-profit
        if active_pos:
            self._manage_open_position(active_pos, cur_price)
            self._log_heartbeat(cur_price, active_pos=active_pos)
            return

        # If tracker thought we had a position but positions are empty, register closed trade
        if self.tracker.has_open_position and not active_pos:
            logger.info("[POSITION CLOSED] Position cleared from exchange. Registering closed trade...")
            self.tracker.register_final_exit(exit_price=cur_price, reason="Exit Filled")
            try:
                self.order_manager.cancel_all_orders()
            except Exception:
                pass

        # 5. Heartbeat while scanning for setups
        self._log_heartbeat(cur_price, active_pos=None)

        # 6. No active position: Evaluate strategy for entry signals
        now = time.time()
        if not self.strategy.should_generate_signal(now):
            return

        signal = self.strategy.generate_signal(self.symbol)
        if signal:
            self._execute_entry_signal(signal, cur_price)

    def _log_heartbeat(self, cur_price: float, active_pos: Optional[Position]) -> None:
        """Emits structured heartbeat log with mark price, account metrics, zones, and status."""
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

        if active_pos:
            is_long = active_pos.side == OrderSide.BUY
            unrealized_usd = (cur_price - active_pos.entry) * active_pos.qty if is_long else (active_pos.entry - cur_price) * active_pos.qty
            margin_val = getattr(active_pos, "margin", getattr(active_pos, "margin_allocated", None))
            margin_used = float(margin_val) if (margin_val and margin_val > 0) else (active_pos.qty * active_pos.entry / (active_pos.leverage or 1.0))
            unrealized_pct = (unrealized_usd / margin_used) * 100 if margin_used > 0 else 0.0
            hold_sec = int(time.time() - self.tracker.entry_time) if self.tracker.entry_time > 0 else 0
            hold_str = f"{hold_sec // 60}m {hold_sec % 60}s"

            sl_px = self.tracker.sl_price or active_pos.sl_price or 0.0
            tp1_px = self.tracker.target_1to1_price or 0.0
            tp2_px = self.tracker.target_1to2_price or active_pos.tp_price or 0.0

            sl_dist = abs(cur_price - sl_px)
            tp2_dist = abs(cur_price - tp2_px)

            ptp_state = "[TAKEN - Breakeven Locked]" if self.tracker.partial_tp_taken else f"Pending (${abs(cur_price - tp1_px):.2f} away)"
            state_desc = "MANAGING RUNNER (Awaiting 1:2 TP)" if self.tracker.partial_tp_taken else "IN POSITION (Awaiting 1:1 TP target)"

            logger.info(
                "\n" + "-" * 76 + "\n"
                "[HEARTBEAT] %s | %s @ %s | Mark: $%.2f | Acct: %s (%s)\n"
                "  * Position:    %s %.5f %s (%.1fx Lev) | Entry: $%.2f | Hold: %s\n"
                "  * Unrealized:  %+$%.2f (%+.2f%%) | Margin: $%.2f\n"
                "  * Brackets:    SL: $%.2f ($%.2f away) | 1:1 TP: $%.2f | 1:2 TP: $%.2f ($%.2f away)\n"
                "  * 1:1 Exit:    %s\n"
                "  * Risk Limits: Balance: $%.2f | Floor: $%.2f | Room: $%.2f (%.2f%%)\n"
                "  * State:       %s\n"
                + "-" * 76,
                ts_str, self.symbol, self.timeframe, cur_price, self.account_id.upper(), mode_str,
                active_pos.side.value, active_pos.qty, self.symbol, active_pos.leverage, active_pos.entry, hold_str,
                unrealized_usd, unrealized_pct, margin_used,
                sl_px, sl_dist, tp1_px, tp2_px, tp2_dist,
                ptp_state,
                risk.balance, risk.floor, risk.room_usd, risk.room_pct,
                state_desc
            )
        else:
            bull_zones = [z for z in self.strategy.active_zones.values() if z.is_bullish]
            bear_zones = [z for z in self.strategy.active_zones.values() if z.is_bearish]
            nearest_bull = max(bull_zones, key=lambda z: z.high) if bull_zones else None
            nearest_bear = min(bear_zones, key=lambda z: z.low) if bear_zones else None

            bull_info = "None in lookback"
            if nearest_bull:
                pct_away = ((cur_price - nearest_bull.high) / cur_price) * 100
                bull_info = f"[{nearest_bull.low:.2f} - {nearest_bull.high:.2f}] ({pct_away:+.2f}% away)"

            bear_info = "None in lookback"
            if nearest_bear:
                pct_away = ((nearest_bear.low - cur_price) / cur_price) * 100
                bear_info = f"[{nearest_bear.low:.2f} - {nearest_bear.high:.2f}] ({pct_away:+.2f}% away)"

            logger.info(
                "\n" + "-" * 76 + "\n"
                "[HEARTBEAT] %s | %s @ %s | Mark: $%.2f | Acct: %s (%s)\n"
                "  * Account:     Equity: $%.2f | Balance: $%.2f | Floor: $%.2f\n"
                "  * Room:        $%.2f (%.2f%% room above drawdown floor)\n"
                "  * Watchlist:   %d Active SMC Zones (%d Bullish Demand/OB, %d Bearish Supply/OB)\n"
                "  * Nearest Support (Bullish OB):    %s\n"
                "  * Nearest Resistance (Bearish OB): %s\n"
                "  * State:       SCANNING (Awaiting price retest into zone & confirmation candle)\n"
                + "-" * 76,
                ts_str, self.symbol, self.timeframe, cur_price, self.account_id.upper(), mode_str,
                risk.equity, risk.balance, risk.floor,
                risk.room_usd, risk.room_pct,
                len(self.strategy.active_zones), len(bull_zones), len(bear_zones),
                bull_info,
                bear_info
            )

    def _manage_open_position(self, pos: Position, cur_price: float) -> None:
        """Monitors active position for multi-tier TP1..TP5 partial profit taking and runner trailing."""
        if not self.tracker.has_open_position:
            # Sync existing position into tracker
            sl_px = pos.sl_price or (pos.entry * 0.98 if pos.side == OrderSide.BUY else pos.entry * 1.02)
            dist = abs(pos.entry - sl_px)
            tp_1to1 = pos.entry + dist if pos.side == OrderSide.BUY else pos.entry - dist
            tp_1to2 = pos.entry + (2.0 * dist) if pos.side == OrderSide.BUY else pos.entry - (2.0 * dist)
            is_long = pos.side == OrderSide.BUY
            default_tps = [
                {"id": "tp1", "price": round(pos.entry + (1.0 * dist) if is_long else pos.entry - (1.0 * dist), 2), "size_pct": 50.0},
                {"id": "tp2", "price": round(pos.entry + (2.0 * dist) if is_long else pos.entry - (2.0 * dist), 2), "size_pct": 50.0}
            ]
            self.tracker.register_entry(pos, target_1to1_price=tp_1to1, target_1to2_price=tp_1to2, sl_price=sl_px, tp_levels=default_tps)

        # Multi-Tier TP Evaluation (TP1..TP5)
        triggered_tp = self.tracker.get_triggered_tp(cur_price)
        if triggered_tp and self.tracker.check_hold_duration_satisfied():
            tp_id = triggered_tp["id"]
            tp_px = triggered_tp["price"]
            size_pct = triggered_tp["size_pct"]

            # Check if this is the final remaining TP
            unhit_tps = [t for t in self.tracker.tp_levels if not t["hit"] and t["id"] != tp_id]
            is_final_tp = len(unhit_tps) == 0

            # Compute close fraction relative to remaining position
            remaining_qty = pos.qty
            target_close_qty = self.tracker.initial_qty * (size_pct / 100.0)
            if is_final_tp or target_close_qty >= remaining_qty * 0.95:
                close_fraction = 1.0
            else:
                close_fraction = min(1.0, max(0.05, target_close_qty / remaining_qty))

            closed_qty = remaining_qty * close_fraction
            gross = abs(cur_price - pos.entry) * closed_qty
            fee = (closed_qty * cur_price) * 0.00045
            net_pnl = gross - fee

            logger.info(
                "\n" + "=" * 76 + "\n"
                "🎯 [%s TARGET HIT] Price touched $%.4f (Target: $%.4f)!\n"
                "   ├─ Closing %.1f%% Position: %.5f %s at Market\n"
                "   ├─ Gross Profit:         %+$%.2f\n"
                "   ├─ Estimated Fee:        -$%.2f\n"
                "   ├─ Net Realized Profit:  %+$%.2f\n"
                "   └─ Remaining Exposure:   %.5f %s\n"
                + "=" * 76,
                tp_id.upper(), cur_price, tp_px,
                close_fraction * 100.0, closed_qty, self.symbol,
                gross, fee, net_pnl,
                max(0.0, remaining_qty - closed_qty), self.symbol
            )

            try:
                self.order_manager.close_position_market(pos.id, percent=close_fraction)
                self.tracker.register_partial_exit(closed_qty, cur_price, net_pnl, fee)
                self.tracker.mark_tp_hit(tp_id)

                # Breakeven lock once TP1 is hit
                if self.enable_partial_tp and not self.tracker.breakeven_locked:
                    contract = self.market.get_contract_detail(self.symbol)
                    self.order_manager.move_sl_to_breakeven(
                        pos, buffer_ticks=1, tick_size=contract.price_unit, current_mark=cur_price
                    )
                    self.tracker.breakeven_locked = True
                    logger.info("🛡️ [BREAKEVEN LOCKED] Stop Loss adjusted to entry ($%.4f + 1 tick buffer)", pos.entry)

            except Exception as e:
                logger.error("[TP EXECUTION ERROR] Failed executing %s: %s", tp_id.upper(), e)

    def _execute_entry_signal(self, signal: TradeSignal, current_price: float) -> None:
        """Executes a confirmed strategy signal with risk budgeting."""
        entry_px = signal.metadata.get("entry_price", current_price)
        sl_px = signal.metadata.get("stop_loss_price")
        tp_px = signal.metadata.get("take_profit_price")
        tp_1to1_px = signal.metadata.get("target_1to1_price", entry_px)
        zone_id = signal.metadata.get("zone_id")

        if not sl_px:
            logger.warning("[SIGNAL REJECTED] Signal missing stop loss price. Rejecting entry.")
            return

        # 1. Calculate sizing constrained by roomUsd
        try:
            qty, lev, margin = self.risk_manager.calculate_order_sizing(
                symbol=self.symbol,
                entry_price=entry_px,
                stop_loss_price=sl_px,
                preferred_leverage=self.preferred_leverage
            )
        except Exception as e:
            logger.warning("[SIZING ERROR] Could not size order for %s: %s", self.symbol, e)
            self.strategy.on_trade_rejected()
            return

        side = OrderSide.BUY if signal.direction == OrderDirection.LONG else OrderSide.SELL
        risk = self.risk_manager.get_risk_state()
        risk_budget = risk.room_usd * self.risk_fraction
        sl_dist = abs(entry_px - sl_px)
        sl_dist_pct = (sl_dist / entry_px) * 100

        logger.info(
            "\n" + "=" * 76 + "\n"
            "[SIGNAL CONFIRMED] %s %s Setup Triggered from Zone %s\n"
            "  * Entry Price:     $%.4f (Current Mark: $%.4f)\n"
            "  * Stop Loss:       $%.4f (Distance: $%.4f | %.2f%%)\n"
            "  * 1:1 TP Target:   $%.4f (50%% partial exit + Breakeven Lock)\n"
            "  * 1:2 TP Target:   $%.4f (Final runner target)\n"
            "[RISK BUDGET SIZING]\n"
            "  * Room to Floor:   $%.2f\n"
            "  * Risk Allocation: %.1f%% of Room -> Budget: $%.2f\n"
            "  * Order Quantity:  %.5f %s ($%.2f notional)\n"
            "  * Leverage:        %.1fx (Isolated Initial Margin: $%.2f)\n"
            "  * Remaining Cash:  $%.2f\n"
            "[SUBMITTING ORDER] %s Market %.5f %s with attached SL/TP brackets...\n"
            + "=" * 76,
            signal.direction.value, self.symbol, zone_id or "SMC_ZONE",
            entry_px, current_price,
            sl_px, sl_dist, sl_dist_pct,
            tp_1to1_px,
            tp_px,
            risk.room_usd,
            self.risk_fraction * 100, risk_budget,
            qty, self.symbol, qty * entry_px,
            lev, margin,
            risk.balance - margin,
            side.value, qty, self.symbol
        )

        try:
            res = self.order_manager.open_market(
                symbol=self.symbol,
                side=side,
                qty=qty,
                leverage=lev,
                sl_price=sl_px,
                tp_price=tp_px
            )

            # Wait briefly for fill to register and fetch created position
            time.sleep(1.0)
            positions = self.order_manager.refresh_positions()
            created_pos = next((p for p in positions if p.symbol == self.symbol), None)

            if created_pos:
                # Configure Multi-Tier TP1..TP5 and SL1..SL5
                dist = abs(entry_px - sl_px)
                is_long = side == OrderSide.BUY
                tp_levels = [
                    {"id": "tp1", "price": round(entry_px + (1.0 * dist) if is_long else entry_px - (1.0 * dist), 2), "size_pct": 20.0},
                    {"id": "tp2", "price": round(entry_px + (1.5 * dist) if is_long else entry_px - (1.5 * dist), 2), "size_pct": 20.0},
                    {"id": "tp3", "price": round(entry_px + (2.0 * dist) if is_long else entry_px - (2.0 * dist), 2), "size_pct": 20.0},
                    {"id": "tp4", "price": round(entry_px + (2.5 * dist) if is_long else entry_px - (2.5 * dist), 2), "size_pct": 20.0},
                    {"id": "tp5", "price": round(entry_px + (3.0 * dist) if is_long else entry_px - (3.0 * dist), 2), "size_pct": 20.0},
                ]
                sl_levels = [
                    {"id": "sl1", "price": sl_px, "size_pct": 100.0}
                ]
                self.tracker.register_entry(
                    position=created_pos,
                    target_1to1_price=tp_1to1_px,
                    target_1to2_price=tp_px,
                    sl_price=sl_px,
                    smc_zone_id=zone_id,
                    tp_levels=tp_levels,
                    sl_levels=sl_levels
                )
                c_margin = getattr(created_pos, "margin", getattr(created_pos, "margin_allocated", 0.0))
                logger.info(
                    "✅ [POSITION CONFIRMED] %s %.5f %s @ $%.4f (Margin: $%.2f, Lev: %.1fx, ID: %s)",
                    created_pos.side.value, created_pos.qty, self.symbol,
                    created_pos.entry, c_margin, created_pos.leverage, created_pos.id
                )
            else:
                logger.info("[ORDER SUBMITTED] Order sent, awaiting position synchronization in next tick...")
        except Exception as e:
            logger.error("[SUBMISSION FAILED] Failed submitting entry order: %s", e)
            self.strategy.on_trade_rejected()
