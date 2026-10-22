"""
PropDAO Position Tracker & Trade Lifecycle Monitor
==================================================
Monitors open positions, calculates live unrealized and realized PnL,
enforces the 1-second minimum hold duration, and coordinates multi-stage partial exits
(1:1 Risk-to-Reward partial close + breakeven lock, leading to 1:2 final target).
"""

from __future__ import annotations
import logging
import time
from typing import Optional, Dict, Any, Callable
from propdao.models import Position, OrderSide, OrderDirection, TradeOutcome

logger = logging.getLogger("PositionTracker")


class PositionTracker:
    """
    Tracks an active trading position through its complete lifecycle:
    - Entry timestamp and hold duration
    - Real-time mark price and unrealized PnL
    - Partial profit booking milestones (Stage 1: 1:1 RR partial close, Stage 2: 1:2 RR full close)
    - Emits TradeOutcome on position close to notify strategy callbacks
    """

    def __init__(
        self,
        symbol: str,
        on_trade_closed_callback: Optional[Callable[[TradeOutcome], None]] = None
    ):
        self.symbol = symbol.upper()
        self.on_trade_closed_callback = on_trade_closed_callback

        self.current_position: Optional[Position] = None
        self.entry_price: float = 0.0
        self.entry_time: float = 0.0
        self.initial_qty: float = 0.0
        self.leverage: float = 1.0
        self.smc_zone_id: Optional[str] = None

        # Partial profit and multi-tier tracking
        self.partial_tp_taken: bool = False
        self.breakeven_locked: bool = False
        self.target_1to1_price: float = 0.0
        self.target_1to2_price: float = 0.0
        self.sl_price: float = 0.0

        # Multi-Tier Ladders: TP1..TP5 and SL1..SL5
        self.tp_levels: List[Dict[str, Any]] = []
        self.sl_levels: List[Dict[str, Any]] = []

        # Accumulated metrics
        self.cumulative_realized_pnl: float = 0.0
        self.cumulative_fees: float = 0.0
        self.trade_id_counter: int = 1

    @property
    def has_open_position(self) -> bool:
        return self.current_position is not None and self.current_position.qty > 0

    def register_entry(
        self,
        position: Position,
        target_1to1_price: float = 0.0,
        target_1to2_price: float = 0.0,
        sl_price: float = 0.0,
        smc_zone_id: Optional[str] = None,
        tp_levels: Optional[List[Dict[str, Any]]] = None,
        sl_levels: Optional[List[Dict[str, Any]]] = None
    ) -> None:
        """Invoked when a new trade entry order is confirmed."""
        self.current_position = position
        self.entry_price = position.entry
        self.entry_time = time.time()
        self.initial_qty = position.qty
        self.leverage = position.leverage
        self.target_1to1_price = target_1to1_price or position.tp_price or 0.0
        self.target_1to2_price = target_1to2_price or position.tp_price or 0.0
        self.sl_price = sl_price or position.sl_price or 0.0
        self.smc_zone_id = smc_zone_id
        self.partial_tp_taken = False
        self.breakeven_locked = False
        self.cumulative_realized_pnl = 0.0
        self.cumulative_fees = position.notional * 0.00045

        # Configure Multi-Tier TP1..TP5
        self.tp_levels = []
        if tp_levels:
            for item in tp_levels:
                self.tp_levels.append({
                    "id": item.get("id", f"tp{len(self.tp_levels)+1}"),
                    "price": float(item.get("price", item.get("triggerPrice", 0.0))),
                    "size_pct": float(item.get("size_pct", item.get("sizePct", 20.0))),
                    "hit": False
                })
        elif self.target_1to1_price > 0:
            # Default 2-tier: TP1 (50%) and TP2 (50%)
            self.tp_levels = [
                {"id": "tp1", "price": self.target_1to1_price, "size_pct": 50.0, "hit": False},
                {"id": "tp2", "price": self.target_1to2_price, "size_pct": 50.0, "hit": False}
            ]

        # Configure Multi-Tier SL1..SL5
        self.sl_levels = []
        if sl_levels:
            for item in sl_levels:
                self.sl_levels.append({
                    "id": item.get("id", f"sl{len(self.sl_levels)+1}"),
                    "price": float(item.get("price", item.get("triggerPrice", 0.0))),
                    "size_pct": float(item.get("size_pct", item.get("sizePct", 20.0))),
                    "hit": False
                })
        elif self.sl_price > 0:
            self.sl_levels = [
                {"id": "sl1", "price": self.sl_price, "size_pct": 100.0, "hit": False}
            ]

        tp_desc = ", ".join([f"{tp['id'].upper()}: ${tp['price']:.2f} ({tp['size_pct']}%)" for tp in self.tp_levels])
        sl_desc = ", ".join([f"{sl['id'].upper()}: ${sl['price']:.2f} ({sl['size_pct']}%)" for sl in self.sl_levels])

        logger.info(
            "📍 Position Registered: %s %s %.5f @ %.4f (Lev: %.1fx)\n"
            "   ├─ Take-Profits: %s\n"
            "   └─ Stop-Losses:  %s",
            position.side.value, self.symbol, position.qty, position.entry, position.leverage,
            tp_desc or "N/A", sl_desc or "N/A"
        )

    def check_hold_duration_satisfied(self) -> bool:
        """PropDAO mandates that a position must be open for >=1.0 second before closing."""
        if not self.has_open_position or self.entry_time <= 0:
            return True
        return (time.time() - self.entry_time) >= 1.0

    def register_partial_exit(
        self,
        closed_qty: float,
        exit_price: float,
        realized_pnl: float,
        fee: float
    ) -> None:
        """Records partial profit taking."""
        self.cumulative_realized_pnl += realized_pnl
        self.cumulative_fees += fee
        self.partial_tp_taken = True
        if self.current_position:
            self.current_position.qty = max(0.0, self.current_position.qty - closed_qty)
            self.current_position.notional = self.current_position.qty * self.entry_price

    def get_triggered_tp(self, cur_price: float) -> Optional[Dict[str, Any]]:
        """Returns the first unhit TP rung whose trigger price has been crossed."""
        if not self.has_open_position:
            return None
        is_long = self.current_position.side == OrderSide.BUY
        for tp in self.tp_levels:
            if tp.get("hit"):
                continue
            t_px = tp.get("price", 0.0)
            if t_px <= 0:
                continue
            if (is_long and cur_price >= t_px) or (not is_long and cur_price <= t_px):
                return tp
        return None

    def get_triggered_sl(self, cur_price: float) -> Optional[Dict[str, Any]]:
        """Returns the first unhit SL rung whose trigger price has been crossed."""
        if not self.has_open_position:
            return None
        is_long = self.current_position.side == OrderSide.BUY
        for sl in self.sl_levels:
            if sl.get("hit"):
                continue
            s_px = sl.get("price", 0.0)
            if s_px <= 0:
                continue
            if (is_long and cur_price <= s_px) or (not is_long and cur_price >= s_px):
                return sl
        return None

    def mark_tp_hit(self, tp_id: str) -> None:
        """Marks a TP level as filled."""
        for tp in self.tp_levels:
            if tp.get("id") == tp_id:
                tp["hit"] = True
                self.partial_tp_taken = True
                break

    def mark_sl_hit(self, sl_id: str) -> None:
        """Marks an SL level as filled."""
        for sl in self.sl_levels:
            if sl.get("id") == sl_id:
                sl["hit"] = True
                break

    def register_final_exit(
        self,
        exit_price: float,
        reason: str = "Manual Close",
        realized_pnl: Optional[float] = None,
        fee: Optional[float] = None
    ) -> TradeOutcome:
        """Records position closure and creates TradeOutcome."""
        close_time = time.time()
        closing_qty = self.current_position.qty if self.current_position else self.initial_qty

        if realized_pnl is None:
            if self.current_position and self.current_position.side == OrderSide.BUY:
                gross = (exit_price - self.entry_price) * closing_qty
            else:
                gross = (self.entry_price - exit_price) * closing_qty
            f = (closing_qty * exit_price) * 0.00045
            realized_pnl = gross - f
            fee = f

        self.cumulative_realized_pnl += realized_pnl
        self.cumulative_fees += (fee or 0.0)

        direction = OrderDirection.LONG if (self.current_position and self.current_position.side == OrderSide.BUY) else OrderDirection.SHORT

        outcome = TradeOutcome(
            trade_id=self.trade_id_counter,
            symbol=self.symbol,
            direction=direction,
            entry_price=self.entry_price,
            exit_price=exit_price,
            quantity=self.initial_qty,
            leverage=self.leverage,
            open_time=self.entry_time,
            close_time=close_time,
            realized_pnl_usdc=self.cumulative_realized_pnl,
            gross_pnl=self.cumulative_realized_pnl + self.cumulative_fees,
            fee=self.cumulative_fees,
            exit_reason=reason,
            smc_zone_id=self.smc_zone_id
        )
        self.trade_id_counter += 1
        self.current_position = None

        logger.info(
            "🏁 Trade Complete #%d (%s): Net PnL: $%.2f | Fees: -$%.2f | Reason: %s",
            outcome.trade_id, outcome.direction.value, outcome.realized_pnl_usdc, outcome.fee, reason
        )

        if self.on_trade_closed_callback:
            try:
                self.on_trade_closed_callback(outcome)
            except Exception as e:
                logger.error("Error in on_trade_closed_callback: %s", e)

        return outcome
