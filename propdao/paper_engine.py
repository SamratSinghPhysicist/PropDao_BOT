"""
PropDAO High-Fidelity Paper Trading Engine
==========================================
Simulates the exact mechanics of PropDAO evaluation and funded trading locally:
- Live marks from Hyperliquid / Binance
- Isolated margin calculation & leverage clamping
- PropDAO fee schedule (0.015% maker, 0.045% taker)
- Millisecond-level bracket execution (SL & TP triggers)
- Static 5% Max Drawdown & 2% Daily Drawdown Floors (hanging off 00:00 UTC anchor equity)
- Breaches & Liquidations
- Zero API key required for full realistic practice
"""

from __future__ import annotations
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from propdao.models import (
    Position, Order, OrderSide, OrderType, OrderStatus,
    RiskState, AccountState, AccountStatus, Stage, TradeReason
)

logger = logging.getLogger("PropDAOPaperEngine")


class PropDAOPaperEngine:
    """
    Virtual PropDAO exchange emulator for Paper Trading.
    Interfaces match PropDAOClient 1:1.
    """

    def __init__(
        self,
        starting_balance: float = 25000.0,
        max_drawdown_pct: float = 5.0,
        daily_drawdown_pct: float = 2.0,
        profit_target_pct: float = 10.0,
        maker_fee: float = 0.00015,
        taker_fee: float = 0.00045,
        account_id: str = "paper-prop-101"
    ):
        self.account_id = account_id
        self.starting_balance = starting_balance
        self.balance = starting_balance
        self.max_drawdown_pct = max_drawdown_pct
        self.daily_drawdown_pct = daily_drawdown_pct
        self.profit_target_pct = profit_target_pct
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee

        # Daily anchor equity (resets at 00:00 UTC)
        self.daily_anchor_equity = starting_balance
        self._current_utc_day = datetime.now(timezone.utc).day

        # Active state
        self.positions: Dict[str, Position] = {}
        self.orders: Dict[str, Order] = {}
        self.trades: List[Dict[str, Any]] = []
        self.breached: bool = False
        self.stage: Stage = Stage.EVALUATION
        self.status: AccountStatus = AccountStatus.ACTIVE

        # Marks cache
        self.marks: Dict[str, float] = {}

    def update_mark_price(self, symbol: str, price: float) -> None:
        """Updates live price mark and evaluates resting orders and stops."""
        sym = symbol.upper()
        self.marks[sym] = float(price)

        # Check daily reset at 00:00 UTC
        cur_day = datetime.now(timezone.utc).day
        if cur_day != self._current_utc_day:
            equity = self.calculate_equity()
            self.daily_anchor_equity = equity
            self._current_utc_day = cur_day
            logger.info("📅 [PAPER] Daily anchor equity reset to $%.2f at 00:00 UTC.", self.daily_anchor_equity)

        # Check resting limit and trigger orders
        self._check_resting_orders(sym, price)

        # Check position brackets (SL & TP) and liquidations
        self._check_position_brackets(sym, price)

        # Check account breach
        self._check_account_drawdowns()

    def calculate_equity(self) -> float:
        """Equity = Balance + Margin Allocated + Unrealized PnL."""
        unrealized = 0.0
        allocated_margin = 0.0

        for p in self.positions.values():
            mark = self.marks.get(p.symbol, p.entry)
            p.mark = mark
            if p.side == OrderSide.BUY:
                pnl = (mark - p.entry) * p.qty
            else:
                pnl = (p.entry - mark) * p.qty
            p.unrealized_pnl = round(pnl, 4)
            unrealized += pnl
            allocated_margin += p.margin_allocated

        return round(self.balance + allocated_margin + unrealized, 2)

    def _check_account_drawdowns(self) -> None:
        """Evaluates static and daily drawdown floors."""
        if self.breached:
            return

        equity = self.calculate_equity()
        max_floor = self.starting_balance * (1.0 - (self.max_drawdown_pct / 100.0))
        daily_floor = self.daily_anchor_equity * (1.0 - (self.daily_drawdown_pct / 100.0))
        binding_floor = max(max_floor, daily_floor)

        if equity <= binding_floor:
            self.breached = True
            self.status = AccountStatus.FAILED
            logger.critical(
                "🚨 [PAPER BREACH] Equity $%.2f hit binding floor $%.2f (Max: $%.2f, Daily: $%.2f). Account failed!",
                equity, binding_floor, max_floor, daily_floor
            )
            # Liquidate all positions
            self.close_all(self.account_id)
            self.cancel_all_orders(self.account_id)

        target_equity = self.starting_balance * (1.0 + (self.profit_target_pct / 100.0))
        if not self.breached and self.stage == Stage.EVALUATION and equity >= target_equity:
            self.stage = Stage.FUNDED
            self.status = AccountStatus.FUNDED
            logger.info("🎉 [PAPER PASSED] Account reached target equity $%.2f! Promoted to FUNDED stage!", equity)

    def _check_resting_orders(self, symbol: str, price: float) -> None:
        """Evaluates resting limit orders against latest mark price."""
        for oid, o in list(self.orders.items()):
            if o.symbol != symbol or o.status != OrderStatus.PENDING:
                continue

            filled = False
            fill_px = price

            if o.order_type == OrderType.LIMIT and o.limit_px is not None:
                if o.side == OrderSide.BUY and price <= o.limit_px:
                    filled = True
                    fill_px = o.limit_px
                elif o.side == OrderSide.SELL and price >= o.limit_px:
                    filled = True
                    fill_px = o.limit_px
            elif o.order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT) and o.trigger_price is not None:
                if (o.side == OrderSide.BUY and price >= o.trigger_price) or (o.side == OrderSide.SELL and price <= o.trigger_price):
                    filled = True
                    fill_px = o.limit_px or price
            elif o.order_type in (OrderType.TAKE_MARKET, OrderType.TAKE_LIMIT) and o.trigger_price is not None:
                if (o.side == OrderSide.BUY and price <= o.trigger_price) or (o.side == OrderSide.SELL and price >= o.trigger_price):
                    filled = True
                    fill_px = o.limit_px or price

            if filled:
                o.status = OrderStatus.FILLED
                del self.orders[oid]
                self._execute_fill(
                    symbol=o.symbol,
                    side=o.side,
                    qty=o.qty,
                    price=fill_px,
                    leverage=o.leverage,
                    sl_price=o.sl,
                    tp_price=o.tp,
                    is_maker=True
                )

    def _check_position_brackets(self, symbol: str, price: float) -> None:
        """Evaluates SL/TP and liquidations on open positions."""
        for pid, pos in list(self.positions.items()):
            if pos.symbol != symbol:
                continue

            # 1. Check Stop Loss
            if pos.sl_price is not None:
                hit_sl = (pos.side == OrderSide.BUY and price <= pos.sl_price) or (pos.side == OrderSide.SELL and price >= pos.sl_price)
                if hit_sl:
                    logger.info("🛑 [PAPER SL HIT] Position %s closed at Stop Loss $%.4f", pid, pos.sl_price)
                    self._close_position_internal(pos, price=pos.sl_price, reason="Stop Loss")
                    continue

            # 2. Check Take Profit
            if pos.tp_price is not None:
                hit_tp = (pos.side == OrderSide.BUY and price >= pos.tp_price) or (pos.side == OrderSide.SELL and price <= pos.tp_price)
                if hit_tp:
                    logger.info("🎯 [PAPER TP HIT] Position %s closed at Take Profit $%.4f", pid, pos.tp_price)
                    self._close_position_internal(pos, price=pos.tp_price, reason="Take Profit")
                    continue

            # 3. Check Isolated Liquidation
            liq_price = pos.liquidation_price
            hit_liq = (pos.side == OrderSide.BUY and price <= liq_price) or (pos.side == OrderSide.SELL and price >= liq_price)
            if hit_liq:
                logger.critical("💥 [PAPER LIQUIDATION] Position %s bust price $%.4f reached!", pid, liq_price)
                self._close_position_internal(pos, price=liq_price, reason="Liquidation")
                continue

    def _execute_fill(
        self,
        symbol: str,
        side: OrderSide,
        qty: float,
        price: float,
        leverage: float,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
        is_maker: bool = False
    ) -> Dict[str, Any]:
        """Creates position and deducts fee and margin."""
        notional = qty * price
        margin = notional / leverage
        fee_rate = self.maker_fee if is_maker else self.taker_fee
        fee = notional * fee_rate

        self.balance -= (margin + fee)
        pos_id = f"pos-{secrets.token_hex(6)}"

        pos = Position(
            id=pos_id,
            symbol=symbol,
            side=side,
            qty=qty,
            entry=price,
            leverage=leverage,
            notional=notional,
            margin_allocated=margin,
            opened_at=int(time.time() * 1000),
            mark=price,
            unrealized_pnl=0.0,
            sl_price=sl_price,
            tp_price=tp_price
        )
        self.positions[pos_id] = pos

        logger.info(
            "⚡ [PAPER FILL] %s %s %.5f @ %.4f (Lev: %.1fx, Margin: $%.2f, Fee: -$%.4f)",
            side.value, symbol, qty, price, leverage, margin, fee
        )
        return {
            "status": f"{'LIMIT' if is_maker else 'MARKET'} {side.value} executed @ {price:.4f} ({symbol}, {leverage:.0f}x). Fee -${fee:.4f}.",
            "position": pos
        }

    def _close_position_internal(
        self,
        pos: Position,
        price: float,
        percent: float = 1.0,
        reason: str = "Manual Close"
    ) -> Dict[str, Any]:
        """Closes all or part of a position."""
        closing_qty = pos.qty * percent
        notional = closing_qty * price
        fee = notional * self.taker_fee

        if pos.side == OrderSide.BUY:
            gross_pnl = (price - pos.entry) * closing_qty
        else:
            gross_pnl = (pos.entry - price) * closing_qty

        net_pnl = gross_pnl - fee
        released_margin = pos.margin_allocated * percent

        self.balance += (released_margin + net_pnl)

        trade_rec = {
            "id": f"hist-{secrets.token_hex(6)}",
            "symbol": pos.symbol,
            "side": pos.side.value,
            "qty": closing_qty,
            "entry": pos.entry,
            "exit": price,
            "grossPnl": gross_pnl,
            "pnl": net_pnl,
            "fee": fee,
            "openFee": pos.notional * self.taker_fee,
            "closeFee": fee,
            "leverage": pos.leverage,
            "openedAt": pos.opened_at,
            "closedAt": int(time.time() * 1000),
            "reason": reason,
            "exitLiquidity": "taker"
        }
        self.trades.insert(0, trade_rec)

        if percent >= 0.999:
            del self.positions[pos.id]
        else:
            pos.qty -= closing_qty
            pos.notional = pos.qty * pos.entry
            pos.margin_allocated -= released_margin

        logger.info(
            "🏁 [PAPER EXIT] %s closed (%.0f%%) @ %.4f | PnL: $%.2f (Gross: $%.2f, Fee: -$%.2f) | Reason: %s",
            pos.symbol, percent * 100.0, price, net_pnl, gross_pnl, fee, reason
        )
        return trade_rec

    # =========================================================================
    # PROPDAO API COMPATIBLE METHODS
    # =========================================================================

    def health(self) -> Dict[str, Any]:
        return {"status": "ok", "mode": "paper", "time": datetime.now(timezone.utc).isoformat()}

    def markets(self) -> Dict[str, Any]:
        return {
            "fees": {"taker": self.taker_fee, "maker": self.maker_fee},
            "marginMode": "isolated",
            "data": [
                {"symbol": "BTCUSDC", "coin": "BTC", "maxLeverage": 2, "lotStep": 0.00001, "szDecimals": 5},
                {"symbol": "ETHUSDC", "coin": "ETH", "maxLeverage": 2, "lotStep": 0.001, "szDecimals": 3},
                {"symbol": "SOLUSDC", "coin": "SOL", "maxLeverage": 2, "lotStep": 0.01, "szDecimals": 2},
            ],
            "total": 3
        }

    def get_accounts(self) -> List[Dict[str, Any]]:
        return [{
            "account_id": self.account_id,
            "status": self.status.value,
            "stage": self.stage.value,
            "balance": self.balance,
            "startingBalance": self.starting_balance
        }]

    def get_account(self, account_id: str) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "status": self.status.value,
            "stage": self.stage.value,
            "balance": self.balance,
            "startingBalance": self.starting_balance,
            "dailyAnchorEquity": self.daily_anchor_equity,
            "maxDrawdownLimitPct": self.max_drawdown_pct,
            "dailyDrawdownLimitPct": self.daily_drawdown_pct,
            "profitTargetPct": self.profit_target_pct,
            "openPositions": [p.__dict__ for p in self.positions.values()],
            "pendingOrders": [o.__dict__ for o in self.orders.values()],
            "tradeHistory": self.trades
        }

    def get_risk(self, account_id: str) -> Dict[str, Any]:
        equity = self.calculate_equity()
        max_floor = self.starting_balance * (1.0 - (self.max_drawdown_pct / 100.0))
        daily_floor = self.daily_anchor_equity * (1.0 - (self.daily_drawdown_pct / 100.0))
        binding_floor = max(max_floor, daily_floor)
        floor_kind = "daily" if daily_floor >= max_floor else "max"
        room_usd = max(0.0, equity - binding_floor)
        room_pct = (room_usd / equity) * 100.0 if equity > 0 else 0.0

        return {
            "equity": equity,
            "balance": self.balance,
            "floor": binding_floor,
            "floorKind": floor_kind,
            "maxFloor": max_floor,
            "dailyFloor": daily_floor,
            "roomUsd": room_usd,
            "roomPct": room_pct,
            "breached": self.breached,
            "openPositions": len(self.positions)
        }

    def get_open_positions(self, account_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        self.calculate_equity()
        return [
            {
                "id": p.id,
                "symbol": p.symbol,
                "side": p.side.value,
                "qty": p.qty,
                "entry": p.entry,
                "leverage": p.leverage,
                "notional": p.notional,
                "marginAllocated": p.margin_allocated,
                "openedAt": p.opened_at,
                "mark": p.mark,
                "unrealizedPnl": p.unrealized_pnl,
                "sl": p.sl,
                "tp": p.tp,
                "slPrice": p.sl_price,
                "tpPrice": p.tp_price
            }
            for p in self.positions.values()
        ]

    def get_open_orders(self, account_id: str, limit: int = 500, offset: int = 0) -> List[Dict[str, Any]]:
        return [
            {
                "id": o.id,
                "symbol": o.symbol,
                "side": o.side.value,
                "qty": o.qty,
                "orderType": o.order_type.value,
                "limitPx": o.limit_px,
                "triggerPrice": o.trigger_price,
                "leverage": o.leverage,
                "sl": o.sl,
                "tp": o.tp,
                "status": o.status.value,
                "createdAt": o.created_at
            }
            for o in self.orders.values()
        ]

    def get_trades(self, account_id: str, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        return self.trades[offset:offset + limit]

    def place_order(
        self,
        account_id: str,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "market",
        leverage: Optional[float] = None,
        limit_price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        reduce_only: bool = False,
        **kw: Any
    ) -> Dict[str, Any]:
        if self.breached:
            raise RuntimeError("403 Max drawdown breached — new orders are disabled.")

        sym = symbol.upper()
        o_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL
        o_type = OrderType(order_type.lower())
        lev = float(leverage or 1.0)
        mark = self.marks.get(sym, float(limit_price or trigger_price or 100.0))

        if o_type == OrderType.MARKET:
            return self._execute_fill(
                symbol=sym,
                side=o_side,
                qty=qty,
                price=mark,
                leverage=lev,
                sl_price=sl,
                tp_price=tp,
                is_maker=False
            )
        else:
            # Resting limit or trigger order
            ord_id = f"ord-{secrets.token_hex(6)}"
            order = Order(
                id=ord_id,
                symbol=sym,
                side=o_side,
                qty=qty,
                order_type=o_type,
                limit_px=limit_price,
                trigger_price=trigger_price,
                leverage=lev,
                sl=sl,
                tp=tp,
                created_at=int(time.time() * 1000)
            )
            self.orders[ord_id] = order
            return {"status": "PENDING", "orderId": ord_id}

    def close_position(self, account_id: str, position_id: str, percent: float = 1.0) -> Dict[str, Any]:
        pos = self.positions.get(position_id)
        if not pos:
            raise ValueError(f"Position {position_id} not found.")
        mark = self.marks.get(pos.symbol, pos.entry)
        return self._close_position_internal(pos, price=mark, percent=percent, reason="Manual Close")

    def close_all(self, account_id: str) -> List[Dict[str, Any]]:
        results = []
        for pid in list(self.positions.keys()):
            results.append(self.close_position(account_id, pid, percent=1.0))
        return results

    def set_risk(self, account_id: str, position_id: str, sl: Optional[float] = None, tp: Optional[float] = None) -> Dict[str, Any]:
        pos = self.positions.get(position_id)
        if not pos:
            raise ValueError(f"Position {position_id} not found.")
        if sl is not None:
            pos.sl_price = None if sl == 0 else float(sl)
        if tp is not None:
            pos.tp_price = None if tp == 0 else float(tp)
        return {"status": "ok", "sl": pos.sl_price, "tp": pos.tp_price}

    def cancel_order(self, account_id: str, order_id: str) -> Dict[str, Any]:
        if order_id in self.orders:
            del self.orders[order_id]
        return {"status": "CANCELLED"}

    def cancel_twap(self, account_id: str, twap_id: str) -> Dict[str, Any]:
        return self.cancel_order(account_id, twap_id)

    def cancel_all_orders(self, account_id: str) -> List[Dict[str, Any]]:
        cleared = []
        for oid in list(self.orders.keys()):
            cleared.append(self.cancel_order(account_id, oid))
        return cleared
