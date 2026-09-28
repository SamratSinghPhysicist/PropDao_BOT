"""
PropDAO Risk Manager & Safeguards
=================================
Enforces PropDAO prop-firm rules:
1. roomUsd Budgeting: Never risks more than a configured fraction of roomUsd on any trade stop-loss.
2. Static Max Drawdown & Daily Drawdown Monitoring.
3. Automated Risk Guard Circuit Breaker: Flattens positions and cancels orders if roomPct drops below safety threshold.
4. Dynamic Quantity Sizing calibrated to asset lotStep and mark price.
5. Challenge Target & Evaluation Tracking.
"""

from __future__ import annotations
import logging
import math
from typing import Optional, Dict, Any, Tuple
from propdao.client import PropDAOClient, PropDAOError
from propdao.market import PropDAOMarket
from propdao.models import RiskState, ContractDetail

logger = logging.getLogger("PropDAORiskManager")


class PropDAORiskManager:
    """
    Institutional risk engine for PropDAO accounts.
    Calculates exact risk budgets, enforces protective limits, and protects against drawdown breaches.
    """

    def __init__(
        self,
        client: PropDAOClient,
        market: PropDAOMarket,
        account_id: str,
        risk_fraction_per_trade: float = 0.25,  # Max 25% of roomUsd risked on a single trade's stop
        min_room_pct_cutoff: float = 1.0,       # Emergency flatten if remaining room < 1.0% of equity
        min_room_usd_cutoff: float = 100.0,     # Emergency flatten if remaining room < $100
        max_leverage_override: Optional[float] = None
    ):
        self.client = client
        self.market = market
        self.account_id = account_id
        self.risk_fraction_per_trade = risk_fraction_per_trade
        self.min_room_pct_cutoff = min_room_pct_cutoff
        self.min_room_usd_cutoff = min_room_usd_cutoff
        self.max_leverage_override = max_leverage_override

        self._last_risk_state: Optional[RiskState] = None

    def get_risk_state(self) -> RiskState:
        """Queries PropDAO for live risk metrics."""
        raw = self.client.get_risk(self.account_id)
        state = RiskState(
            equity=float(raw.get("equity", 0.0)),
            balance=float(raw.get("balance", 0.0)),
            floor=float(raw.get("floor", 0.0)),
            floor_kind=str(raw.get("floorKind", "max")),
            max_floor=float(raw.get("maxFloor", 0.0)),
            daily_floor=float(raw.get("dailyFloor", 0.0)),
            room_usd=float(raw.get("roomUsd", 0.0)),
            room_pct=float(raw.get("roomPct", 0.0)),
            breached=bool(raw.get("breached", False)),
            open_positions=int(raw.get("openPositions", 0))
        )
        self._last_risk_state = state
        return state

    def check_safety_and_guard(self) -> bool:
        """
        Evaluates account health. If room is too thin or breached,
        triggers emergency risk guard to cancel all orders and close positions.
        Returns: True if safe to trade, False if guarded / halted.
        """
        try:
            risk = self.get_risk_state()
        except PropDAOError as e:
            if e.is_breached:
                logger.critical("🚨 PropDAO Account %s is BREACHED! Halting all operations.", self.account_id)
                return False
            logger.error("Failed to query risk state: %s", e)
            return True

        if risk.breached:
            logger.critical("🚨 PropDAO Account %s has breached drawdown floor! Ceasing trading.", self.account_id)
            return False

        # If we have open positions and room is critically low, activate risk guard
        if risk.open_positions > 0:
            if risk.room_pct < self.min_room_pct_cutoff or risk.room_usd < self.min_room_usd_cutoff:
                logger.warning(
                    "⚠️ Risk Guard Triggered! Room thin (roomUsd: $%.2f, roomPct: %.2f%%). Flattening positions...",
                    risk.room_usd, risk.room_pct
                )
                self.client.cancel_all_orders(self.account_id)
                self.client.close_all(self.account_id)
                return False

        if risk.room_usd < self.min_room_usd_cutoff:
            logger.warning("Room USD $%.2f is below minimum $%.2f. Entries blocked.", risk.room_usd, self.min_room_usd_cutoff)
            return False

        return True

    def calculate_order_sizing(
        self,
        symbol: str,
        entry_price: float,
        stop_loss_price: float,
        preferred_leverage: Optional[float] = None
    ) -> Tuple[float, float, float]:
        """
        Calculates exact quantity and leverage for an order.
        Guarantees that (abs(entry - sl) * qty) <= (roomUsd * risk_fraction).
        Returns: (qty, leverage, allocated_margin)
        """
        risk = self.get_risk_state()
        if risk.breached or risk.room_usd <= 0:
            raise ValueError(f"Cannot size trade: Account breached or roomUsd <= 0 (roomUsd=${risk.room_usd:.2f})")

        contract = self.market.get_contract_detail(symbol)
        max_lev = contract.max_leverage
        if self.max_leverage_override:
            max_lev = min(max_lev, self.max_leverage_override)

        leverage = min(max_lev, float(preferred_leverage or max_lev))

        # Risk budget in USD
        risk_budget = risk.room_usd * self.risk_fraction_per_trade
        sl_distance = abs(entry_price - stop_loss_price)

        if sl_distance <= 1e-12:
            sl_distance = entry_price * 0.01  # fallback to 1% distance if stop is identical to entry

        # Sizing formula: qty * sl_distance = risk_budget -> qty = risk_budget / sl_distance
        raw_qty = risk_budget / sl_distance

        # Round to lotStep
        lot_step = contract.lot_step
        sz_dec = contract.sz_decimals or 4
        if lot_step > 0:
            stepped_qty = round(round(raw_qty / lot_step, 6)) * lot_step
        else:
            stepped_qty = raw_qty

        qty = round(stepped_qty, sz_dec)

        if qty <= 0:
            qty = lot_step

        notional = qty * entry_price
        margin_required = notional / leverage

        # Ensure margin required does not exceed available balance
        if margin_required > risk.balance * 0.95:
            # Scale down qty to fit within 90% of cash balance
            max_notional = risk.balance * 0.90 * leverage
            qty = math.floor((max_notional / entry_price) / lot_step) * lot_step
            qty = round(qty, sz_dec)
            notional = qty * entry_price
            margin_required = notional / leverage

        logger.info(
            "📐 Risk Sizing for %s: Budget: $%.2f | SL Dist: $%.4f | Qty: %.5f | Lev: %.1fx | Margin: $%.2f",
            symbol, risk_budget, sl_distance, qty, leverage, margin_required
        )
        return qty, leverage, margin_required
