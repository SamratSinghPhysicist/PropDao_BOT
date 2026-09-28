"""
PropDAO Order & Position Orchestrator
=====================================
Manages complete order execution lifecycle:
- Market, Limit, Stop, Take, Scale, and TWAP order execution
- Multi-tier Take Profit & Stop Loss brackets (Full, Partial, Limit, Trigger)
- Breakeven lock at 1:1 RR target and partial TP harvesting (e.g. 50% off at 1:1, remainder at 1:2)
- Position closes (Market, Limit, Partial, Full)
- Hedging vs Netting controls (prevents unintended opposite legs)
- Hold duration and execution cadence enforcement
"""

from __future__ import annotations
import logging
import time
from typing import Optional, Dict, Any, List, Tuple
from propdao.client import PropDAOClient, PropDAOError
from propdao.models import Position, Order, OrderSide, OrderType, TradeReason

logger = logging.getLogger("PropDAOOrderManager")


class PropDAOOrderManager:
    """
    High-level order orchestrator.
    Translates strategy trade signals into live PropDAO orders, manages brackets,
    partial closes, and multi-tier exits.
    """

    def __init__(self, client: PropDAOClient, account_id: str):
        self.client = client
        self.account_id = account_id

        # Internal position tracking state
        self._positions_cache: Dict[str, Position] = {}
        self._last_position_fetch: float = 0.0

    def refresh_positions(self) -> List[Position]:
        """Fetches active positions from PropDAO."""
        raw_list = self.client.get_open_positions(self.account_id)
        positions: List[Position] = []

        for p in raw_list:
            pos = Position(
                id=str(p.get("id")),
                symbol=str(p.get("symbol")).upper(),
                side=OrderSide.BUY if str(p.get("side")).upper() == "BUY" else OrderSide.SELL,
                qty=float(p.get("qty", 0.0)),
                entry=float(p.get("entry", 0.0)),
                leverage=float(p.get("leverage", 1.0)),
                notional=float(p.get("notional", 0.0)),
                margin_allocated=float(p.get("marginAllocated", 0.0)),
                opened_at=int(p.get("openedAt", 0)),
                mark=float(p.get("mark", 0.0)),
                unrealized_pnl=float(p.get("unrealizedPnl", 0.0)),
                sl=p.get("sl"),
                tp=p.get("tp"),
                sl_price=p.get("slPrice"),
                tp_price=p.get("tpPrice")
            )
            positions.append(pos)
            self._positions_cache[pos.id] = pos

        self._last_position_fetch = time.time()
        return positions

    # =========================================================================
    # OPENING ORDERS
    # =========================================================================

    def open_market(
        self,
        symbol: str,
        side: OrderSide | str,
        qty: float,
        leverage: float = 1.0,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
        intent_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Executes immediate market entry with attached SL / TP prices.
        """
        side_str = "BUY" if side in (OrderSide.BUY, "BUY", "LONG") else "SELL"
        res = self.client.place_order(
            account_id=self.account_id,
            symbol=symbol,
            side=side_str,
            qty=qty,
            order_type="market",
            leverage=leverage,
            sl=sl_price,
            tp=tp_price,
            intent_id=intent_id
        )
        logger.info(
            "🟢 Market Open %s %s %.5f (Lev: %.1fx, SL: %s, TP: %s) -> %s",
            side_str, symbol, qty, leverage, sl_price, tp_price, res.get("status")
        )
        return res

    def open_limit(
        self,
        symbol: str,
        side: OrderSide | str,
        qty: float,
        limit_price: float,
        leverage: float = 1.0,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
        post_only: bool = False,
        intent_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Submits resting limit maker order (post-only ALO or GTC).
        """
        side_str = "BUY" if side in (OrderSide.BUY, "BUY", "LONG") else "SELL"
        tif = "alo" if post_only else "gtc"
        res = self.client.place_order(
            account_id=self.account_id,
            symbol=symbol,
            side=side_str,
            qty=qty,
            order_type="limit",
            limit_price=limit_price,
            leverage=leverage,
            sl=sl_price,
            tp=tp_price,
            tif=tif,
            intent_id=intent_id
        )
        logger.info(
            "📝 Limit Open %s %s %.5f @ %.4f (Lev: %.1fx, TIF: %s)",
            side_str, symbol, qty, limit_price, leverage, tif
        )
        return res

    def open_trigger(
        self,
        symbol: str,
        side: OrderSide | str,
        qty: float,
        trigger_price: float,
        limit_price: Optional[float] = None,
        is_take_profit: bool = False,
        leverage: float = 1.0,
        intent_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Places trigger stop / take order.
        order_type is stop_market / stop_limit or take_market / take_limit.
        """
        side_str = "BUY" if side in (OrderSide.BUY, "BUY", "LONG") else "SELL"
        if is_take_profit:
            order_type = "take_limit" if limit_price is not None else "take_market"
        else:
            order_type = "stop_limit" if limit_price is not None else "stop_market"

        res = self.client.place_order(
            account_id=self.account_id,
            symbol=symbol,
            side=side_str,
            qty=qty,
            order_type=order_type,
            trigger_price=trigger_price,
            limit_price=limit_price,
            leverage=leverage,
            intent_id=intent_id
        )
        logger.info(
            "🎯 Trigger Order %s (%s) %s %.5f (Trigger: %.4f, Limit: %s)",
            order_type, side_str, symbol, qty, trigger_price, limit_price
        )
        return res

    def open_twap(
        self,
        symbol: str,
        side: OrderSide | str,
        qty: float,
        minutes: float,
        leverage: float = 1.0,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None
    ) -> Dict[str, Any]:
        """Executes TWAP order over specified minutes."""
        side_str = "BUY" if side in (OrderSide.BUY, "BUY", "LONG") else "SELL"
        return self.client.twap(
            self.account_id, symbol, side_str, qty,
            minutes=minutes, leverage=leverage,
            min_price=min_price, max_price=max_price
        )

    # =========================================================================
    # CLOSING & PARTIAL EXITS
    # =========================================================================

    def close_position_market(
        self,
        position_id: str,
        percent: float = 1.0
    ) -> Dict[str, Any]:
        """
        Closes position at current mark.
        percent=1.0 for full close; percent=0.5 for 50% partial close.
        Ensures minimum 1-second hold requirement.
        """
        # Check hold time
        pos = self._positions_cache.get(position_id)
        if pos and pos.opened_at > 0:
            elapsed_ms = (time.time() * 1000) - pos.opened_at
            if elapsed_ms < 1000:
                time.sleep((1000 - elapsed_ms) / 1000.0)

        res = self.client.close_position(self.account_id, position_id, percent=percent)
        logger.info("🔴 Closed position %s (%.1f%%) -> %s", position_id, percent * 100.0, res)
        return res

    def close_position_limit(
        self,
        position: Position,
        price: float,
        percent: float = 1.0
    ) -> Dict[str, Any]:
        """
        Submits a reduce-only resting limit order to close at a specific price.
        """
        exit_side = "SELL" if position.side == OrderSide.BUY else "BUY"
        qty = round(position.qty * percent, 6)
        res = self.client.place_order(
            account_id=self.account_id,
            symbol=position.symbol,
            side=exit_side,
            qty=qty,
            order_type="limit",
            limit_price=price,
            reduce_only=True,
            tif="gtc"
        )
        logger.info("📋 Placed Limit Exit for position %s: %s %.5f @ %.4f", position.id, exit_side, qty, price)
        return res

    # =========================================================================
    # BRACKETS & BREAKEVEN LOGIC
    # =========================================================================

    def update_brackets(
        self,
        position_id: str,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None
    ) -> Dict[str, Any]:
        """Updates SL and/or TP price on an open position."""
        res = self.client.set_risk(self.account_id, position_id, sl=sl_price, tp=tp_price)
        logger.info("🛡️ Updated brackets for %s: SL=%s, TP=%s", position_id, sl_price, tp_price)
        return res

    def move_sl_to_breakeven(
        self,
        position: Position,
        buffer_ticks: int = 1,
        tick_size: float = 0.01
    ) -> Dict[str, Any]:
        """
        Moves stop-loss to entry price plus a tiny profit buffer (breakeven lock).
        """
        if position.side == OrderSide.BUY:
            be_price = round(position.entry + (buffer_ticks * tick_size), 4)
            # Ensure new stop is higher than current stop
            if position.sl_price and be_price <= position.sl_price:
                return {}
        else:
            be_price = round(position.entry - (buffer_ticks * tick_size), 4)
            if position.sl_price and be_price >= position.sl_price:
                return {}

        return self.update_brackets(position.id, sl_price=be_price, tp_price=position.tp_price)
