"""
KCEX Risk & Margin Calculator
=============================
Computes pre-trade and post-trade risks, liquidation prices, fee estimations,
take-profit/stop-loss scenarios, and real-time dual currency conversions (USDT and INR).
"""

import math
import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass
from kcex.market import KCEXMarket, ContractInfo
from kcex.client import KCEXClient
from kcex.config import KCEXConfig

logger = logging.getLogger("KCEXRisk")


@dataclass
class RiskAnalysisReport:
    """
    Comprehensive risk metrics for an order or open position.
    Presented in both USDT and INR.
    """
    symbol: str
    direction: str                     # "LONG" or "SHORT"
    leverage: int
    entry_price: float
    vol_contracts: int                 # Contract units
    contract_size: float               # cs
    underlying_quantity: float         # vol * cs (amount of coins)
    notional_value_usdt: float         # underlying_qty * entry_price
    notional_value_inr: float          # notional_usdt * inr_rate
    initial_margin_usdt: float         # notional_usdt / leverage
    initial_margin_inr: float          # initial_margin_usdt * inr_rate
    liquidation_price: float
    distance_to_liquidation_pct: float # Percent drop/rise to liquidation
    fee_open_usdt: float
    fee_close_usdt: float
    fee_total_usdt: float
    fee_total_inr: float
    take_profit_price: Optional[float] = None
    tp_profit_usdt: Optional[float] = None
    tp_profit_inr: Optional[float] = None
    tp_roe_pct: Optional[float] = None
    stop_loss_price: Optional[float] = None
    sl_loss_usdt: Optional[float] = None
    sl_loss_inr: Optional[float] = None
    sl_roe_pct: Optional[float] = None
    risk_reward_ratio: Optional[float] = None
    inr_rate: float = 94.45
    price_precision: int = 4

    def format_summary(self) -> str:
        """Generates a clean human-readable risk summary table."""
        ps = getattr(self, "price_precision", 4) or 4
        base_coin = self.symbol.split('_')[0]
        dir_label = "[LONG]" if self.direction.upper() == "LONG" else "[SHORT]"
        lines = [
            f"=== RISK & PRE-TRADE REPORT: {self.symbol} {dir_label} ({self.leverage}x) ===",
            f"Entry Price          : {self.entry_price:.{ps}f} USDT",
            f"Contract Size (cs)   : {self.contract_size} {base_coin}/contract",
            f"Volume (Contracts)   : {self.vol_contracts} contract(s)",
            f"Underlying Quantity  : {self.underlying_quantity:g} {base_coin}",
            f"Position Exposure    : {self.notional_value_usdt:.4f} USDT  |  INR {self.notional_value_inr:.2f}",
            f"Initial Margin Req   : {self.initial_margin_usdt:.4f} USDT  |  INR {self.initial_margin_inr:.2f}",
            f"Liquidation Price    : {self.liquidation_price:.{ps}f} USDT ({self.distance_to_liquidation_pct:.2f}% distance)",
            f"Estimated Fees (R/T) : {self.fee_total_usdt:.4f} USDT  |  INR {self.fee_total_inr:.2f}",
            f"Live USD/INR Rate    : INR {self.inr_rate:.2f} per USD",
            "----------------------------------------------------------------------"
        ]

        if self.take_profit_price:
            lines.append(
                f"Take Profit (TP)     : {self.take_profit_price:.{ps}f} USDT -> Profit: +{self.tp_profit_usdt:.4f} USDT "
                f"(+INR {self.tp_profit_inr:.2f}) | ROE: +{self.tp_roe_pct:.2f}%"
            )
        if self.stop_loss_price:
            lines.append(
                f"Stop Loss (SL)       : {self.stop_loss_price:.{ps}f} USDT -> Loss: -{self.sl_loss_usdt:.4f} USDT "
                f"(-INR {self.sl_loss_inr:.2f}) | ROE: -{self.sl_roe_pct:.2f}%"
            )
        if self.risk_reward_ratio is not None:
            lines.append(f"Risk/Reward Ratio    : 1 : {self.risk_reward_ratio:.2f}")

        return "\n".join(lines)


class KCEXRiskCalculator:
    """
    Calculator for liquidation prices, order sizing, fees, TP/SL levels, and dual currency metrics.
    """

    def __init__(self, market: Optional[KCEXMarket] = None, client: Optional[KCEXClient] = None):
        self.market = market or KCEXMarket()
        self.client = client or self.market.client

    def convert_usdt_to_contracts(
        self,
        symbol: str,
        target_usdt: float,
        price: Optional[float] = None
    ) -> int:
        """
        Calculates the number of integer contracts needed to achieve a target USDT notional exposure.
        
        Args:
            symbol (str): Trading pair symbol.
            target_usdt (float): Desired notional USDT value (e.g. 10 USDT).
            price (float, optional): Price to calculate at (defaults to latest ticker lastPrice).

        Returns:
            int: Number of contract units (vol), at least min_volume.
        """
        contract = self.market.get_contract_detail(symbol)
        if price is None:
            ticker = self.market.get_ticker(symbol)
            price = float(ticker.get("lastPrice", 1.0))

        # 1 contract notional = cs * price
        one_contract_notional = contract.contract_size * price
        if one_contract_notional <= 0:
            return int(contract.min_volume)

        contracts = target_usdt / one_contract_notional
        vol = max(int(math.floor(contracts / contract.volume_unit) * contract.volume_unit), int(contract.min_volume))
        return vol

    def convert_inr_to_contracts(
        self,
        symbol: str,
        target_inr: float,
        price: Optional[float] = None
    ) -> int:
        """
        Calculates the number of contracts for a target amount in INR.
        """
        inr_rate = self.market.get_inr_rate()
        target_usdt = target_inr / inr_rate
        return self.convert_usdt_to_contracts(symbol, target_usdt, price=price)

    def convert_coin_qty_to_contracts(
        self,
        symbol: str,
        target_coin_qty: float
    ) -> int:
        """
        Converts coin quantity (e.g. 50 DOGE or 2 TRUMP) into contract volume units.
        """
        contract = self.market.get_contract_detail(symbol)
        contracts = target_coin_qty / contract.contract_size
        vol = max(int(math.floor(contracts / contract.volume_unit) * contract.volume_unit), int(contract.min_volume))
        return vol

    def calculate_liquidation_price(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        leverage: int,
        is_isolated: bool = True
    ) -> float:
        """
        Calculates the estimated liquidation price.
        
        Uses exchange's official liquidation preview endpoint if authenticated,
        or the mathematical formula based on MMR (maintenance margin ratio).

        Formulas (Isolated Margin):
            Long Liq  = EntryPrice * (1 - (1 / Leverage) + MMR)
            Short Liq = EntryPrice * (1 + (1 / Leverage) - MMR)
        """
        contract = self.market.get_contract_detail(symbol)
        mmr = contract.maintenance_margin_ratio
        is_long = direction.upper() in ("LONG", "BUY")

        # Try exchange liquidation preview endpoint if authenticated
        if self.client.config.is_authenticated:
            try:
                payload = {
                    "leverage": leverage,
                    "longSideVol": 1,
                    "shortSideVol": 1,
                    "longSidePrice": entry_price,
                    "shortSidePrice": entry_price,
                    "positionOpenType": 1 if is_isolated else 2,
                    "orderType": "5",
                    "symbol": symbol.upper()
                }
                res = self.client.post_private(KCEXConfig.ENDPOINT_CALC_LIQUIDATE_PRICE, json_data=payload)
                data = res.get("data", {})
                if is_long and "longSideLiquidatePrice" in data:
                    return float(data["longSideLiquidatePrice"])
                elif not is_long and "shortSideLiquidatePrice" in data:
                    return float(data["shortSideLiquidatePrice"])
            except Exception as e:
                logger.debug("Exchange calc_liquidate_price endpoint call failed, using formula: %s", e)

        # Mathematical formula fallback
        if is_long:
            liq_price = entry_price * (1.0 - (1.0 / leverage) + mmr)
            return max(0.0, round(liq_price, contract.price_precision))
        else:
            liq_price = entry_price * (1.0 + (1.0 / leverage) - mmr)
            return round(liq_price, contract.price_precision)

    def calculate_tp_sl_from_price_pct(
        self,
        direction: str,
        entry_price: float,
        tp_pct: Optional[float] = None,
        sl_pct: Optional[float] = None
    ) -> Dict[str, Optional[float]]:
        """
        Calculates absolute TP and SL prices based on percentage price movement.
        
        Example for Long:
            tp_pct = 2.0  -> TP = EntryPrice * 1.02
            sl_pct = 1.0  -> SL = EntryPrice * 0.99
        """
        is_long = direction.upper() in ("LONG", "BUY")
        tp_price = None
        sl_price = None

        if tp_pct is not None and tp_pct > 0:
            if is_long:
                tp_price = entry_price * (1.0 + (tp_pct / 100.0))
            else:
                tp_price = entry_price * (1.0 - (tp_pct / 100.0))

        if sl_pct is not None and sl_pct > 0:
            if is_long:
                sl_price = entry_price * (1.0 - (sl_pct / 100.0))
            else:
                sl_price = entry_price * (1.0 + (sl_pct / 100.0))

        return {"take_profit_price": tp_price, "stop_loss_price": sl_price}

    def calculate_tp_sl_from_roe_pct(
        self,
        direction: str,
        entry_price: float,
        leverage: int,
        tp_roe_pct: Optional[float] = None,
        sl_roe_pct: Optional[float] = None
    ) -> Dict[str, Optional[float]]:
        """
        Calculates absolute TP and SL prices based on desired Return on Equity (ROE / Margin %).
        
        Since ROE% = PriceChangePct * Leverage:
            PriceChangePct = ROE% / Leverage
        """
        tp_price = None
        sl_price = None

        if tp_roe_pct is not None and tp_roe_pct > 0:
            price_pct = tp_roe_pct / leverage
            tp_price = self.calculate_tp_sl_from_price_pct(direction, entry_price, tp_pct=price_pct)["take_profit_price"]

        if sl_roe_pct is not None and sl_roe_pct > 0:
            price_pct = sl_roe_pct / leverage
            sl_price = self.calculate_tp_sl_from_price_pct(direction, entry_price, sl_pct=price_pct)["stop_loss_price"]

        return {"take_profit_price": tp_price, "stop_loss_price": sl_price}

    def analyze_order_risk(
        self,
        symbol: str,
        direction: str,
        vol_contracts: int,
        entry_price: Optional[float] = None,
        leverage: Optional[int] = None,
        is_market_order: bool = True,
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        tp_pct: Optional[float] = None,
        sl_pct: Optional[float] = None,
        tp_roe_pct: Optional[float] = None,
        sl_roe_pct: Optional[float] = None,
        is_isolated: bool = True
    ) -> RiskAnalysisReport:
        """
        Generates a complete pre-trade risk report in both USDT and INR.
        
        Calculates notional value, margin required, estimated taker/maker fees,
        liquidation price, expected profit at TP, expected loss at SL, and RRR.
        """
        symbol_upper = symbol.upper()
        contract = self.market.get_contract_detail(symbol_upper)
        inr_rate = self.market.get_inr_rate()

        if entry_price is None:
            ticker = self.market.get_ticker(symbol_upper)
            entry_price = float(ticker.get("lastPrice", 1.0))

        if leverage is None:
            leverage = min(contract.max_leverage, 20)

        # Enforce valid leverage bounds
        leverage = max(contract.min_leverage, min(leverage, contract.max_leverage))

        # Position sizing
        underlying_qty = vol_contracts * contract.contract_size
        notional_usdt = underlying_qty * entry_price
        notional_inr = notional_usdt * inr_rate
        initial_margin_usdt = notional_usdt / leverage
        initial_margin_inr = initial_margin_usdt * inr_rate

        # Liquidation price
        is_long = direction.upper() in ("LONG", "BUY")
        dir_str = "LONG" if is_long else "SHORT"
        liq_price = self.calculate_liquidation_price(
            symbol=symbol_upper,
            direction=dir_str,
            entry_price=entry_price,
            leverage=leverage,
            is_isolated=is_isolated
        )

        if is_long:
            dist_pct = ((entry_price - liq_price) / entry_price) * 100.0 if entry_price > 0 else 0.0
        else:
            dist_pct = ((liq_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0

        # Fees
        open_fee_rate = contract.taker_fee_rate if is_market_order else contract.maker_fee_rate
        close_fee_rate = contract.taker_fee_rate  # Assume taker on emergency close
        fee_open = notional_usdt * open_fee_rate
        fee_close = notional_usdt * close_fee_rate
        fee_total_usdt = fee_open + fee_close
        fee_total_inr = fee_total_usdt * inr_rate

        # Determine final TP/SL prices
        final_tp = tp_price
        final_sl = sl_price

        # Check % price move targets
        if final_tp is None and tp_pct is not None:
            final_tp = self.calculate_tp_sl_from_price_pct(dir_str, entry_price, tp_pct=tp_pct)["take_profit_price"]
        if final_sl is None and sl_pct is not None:
            final_sl = self.calculate_tp_sl_from_price_pct(dir_str, entry_price, sl_pct=sl_pct)["stop_loss_price"]

        # Check % ROE targets
        if final_tp is None and tp_roe_pct is not None:
            final_tp = self.calculate_tp_sl_from_roe_pct(dir_str, entry_price, leverage, tp_roe_pct=tp_roe_pct)["take_profit_price"]
        if final_sl is None and sl_roe_pct is not None:
            final_sl = self.calculate_tp_sl_from_roe_pct(dir_str, entry_price, leverage, sl_roe_pct=sl_roe_pct)["stop_loss_price"]

        # Calculate TP Profit / SL Loss
        tp_profit_usdt = None
        tp_profit_inr = None
        tp_roe = None
        if final_tp is not None:
            final_tp = round(final_tp, contract.price_precision)
            price_diff = (final_tp - entry_price) if is_long else (entry_price - final_tp)
            tp_profit_usdt = underlying_qty * price_diff
            tp_profit_inr = tp_profit_usdt * inr_rate
            tp_roe = (tp_profit_usdt / initial_margin_usdt) * 100.0 if initial_margin_usdt > 0 else 0.0

        sl_loss_usdt = None
        sl_loss_inr = None
        sl_roe = None
        if final_sl is not None:
            final_sl = round(final_sl, contract.price_precision)
            price_diff = (entry_price - final_sl) if is_long else (final_sl - entry_price)
            sl_loss_usdt = underlying_qty * price_diff
            sl_loss_inr = sl_loss_usdt * inr_rate
            sl_roe = (sl_loss_usdt / initial_margin_usdt) * 100.0 if initial_margin_usdt > 0 else 0.0

        # Risk Reward Ratio (RRR)
        rrr = None
        if tp_profit_usdt is not None and sl_loss_usdt is not None and sl_loss_usdt > 0:
            rrr = tp_profit_usdt / sl_loss_usdt

        return RiskAnalysisReport(
            symbol=symbol_upper,
            direction=dir_str,
            leverage=leverage,
            entry_price=entry_price,
            vol_contracts=vol_contracts,
            contract_size=contract.contract_size,
            underlying_quantity=underlying_qty,
            notional_value_usdt=notional_usdt,
            notional_value_inr=notional_inr,
            initial_margin_usdt=initial_margin_usdt,
            initial_margin_inr=initial_margin_inr,
            liquidation_price=liq_price,
            distance_to_liquidation_pct=dist_pct,
            fee_open_usdt=fee_open,
            fee_close_usdt=fee_close,
            fee_total_usdt=fee_total_usdt,
            fee_total_inr=fee_total_inr,
            take_profit_price=final_tp,
            tp_profit_usdt=tp_profit_usdt,
            tp_profit_inr=tp_profit_inr,
            tp_roe_pct=tp_roe,
            stop_loss_price=final_sl,
            sl_loss_usdt=sl_loss_usdt,
            sl_loss_inr=sl_loss_inr,
            sl_roe_pct=sl_roe,
            risk_reward_ratio=rrr,
            inr_rate=inr_rate,
            price_precision=contract.price_precision
        )
