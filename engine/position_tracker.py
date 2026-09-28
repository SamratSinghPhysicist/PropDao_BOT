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

        # Partial profit tracking
        self.partial_tp_taken: bool = False
        self.target_1to1_price: float = 0.0
        self.target_1to2_price: float = 0.0
        self.sl_price: float = 0.0

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
        target_1to1_price: float,
        target_1to2_price: float,
        sl_price: float,
        smc_zone_id: Optional[str] = None
    ) -> None:
        """Invoked when a new trade entry order is confirmed."""
        self.current_position = position
        self.entry_price = position.entry
        self.entry_time = time.time()
        self.initial_qty = position.qty
        self.leverage = position.leverage
        self.target_1to1_price = target_1to1_price
        self.target_1to2_price = target_1to2_price
        self.sl_price = sl_price
        self.smc_zone_id = smc_zone_id
        self.partial_tp_taken = False
        self.cumulative_realized_pnl = 0.0
        self.cumulative_fees = position.notional * 0.00045

        logger.info(
            "📍 Position Registered: %s %s %.5f @ %.4f | 1:1 TP: %.4f | 1:2 TP: %.4f | SL: %.4f",
            position.side.value, self.symbol, position.qty, position.entry,
            target_1to1_price, target_1to2_price, sl_price
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
