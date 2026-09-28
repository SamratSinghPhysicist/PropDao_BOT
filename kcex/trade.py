"""
KCEX Trade & Order Manager
==========================
Handles order creation (Market/Limit, Long/Short), attached TP/SL, post-trade TP/SL
placement, partial position closing, order cancellations, and account/position reconciliation.
"""
import math
import logging
from typing import Dict, List, Any, Optional
from kcex.client import KCEXClient, KCEXAPIError
from kcex.config import KCEXConfig
from kcex.market import KCEXMarket
from kcex.risk import KCEXRiskCalculator

logger = logging.getLogger("KCEXTrader")


def format_price_str(price: float, precision: int) -> str:
    """Formats price to fixed decimal string avoiding scientific notation."""
    s = f"{price:.{precision}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
        if not s:
            s = "0"
    return s


class KCEXTrader:
    """
    Manages futures orders, positions, attached/post-trade TP/SL, and balances.
    """

    def __init__(
        self,
        client: Optional[KCEXClient] = None,
        market: Optional[KCEXMarket] = None,
        risk_calculator: Optional[KCEXRiskCalculator] = None
    ):
        self.client = client or KCEXClient()
        self.market = market or KCEXMarket(self.client)
        self.risk = risk_calculator or KCEXRiskCalculator(self.market, self.client)

    # =========================================================================
    # ACCOUNT & BALANCES
    # =========================================================================

    def get_assets(self) -> Dict[str, Any]:
        """
        Fetches futures account assets and balances.
        Endpoint: GET /fapi/v1/private/account/assets
        
        Returns:
            Dict of asset balances (e.g. USDT equity, available balance, unrealized PnL).
        """
        res = self.client.get_private(KCEXConfig.ENDPOINT_ACCOUNT_ASSETS)
        return res.get("data", {})

    def get_usdt_balance(self) -> Dict[str, float]:
        """
        Convenience helper to get available and total USDT balances.
        """
        assets = self.get_assets()
        # Assets can be returned as a dict keyed by currency (e.g. {"USDT": {...}}) or list
        usdt_info = {}
        if isinstance(assets, dict):
            if "USDT" in assets:
                usdt_info = assets["USDT"]
            elif "available" in assets:
                usdt_info = assets
        elif isinstance(assets, list):
            for item in assets:
                if item.get("currency") == "USDT" or item.get("symbol") == "USDT":
                    usdt_info = item
                    break

        available = float(usdt_info.get("available", 0.0) or usdt_info.get("availableBalance", 0.0))
        equity = float(usdt_info.get("equity", 0.0) or usdt_info.get("total", available))
        unrealized = float(usdt_info.get("unrealized", 0.0) or usdt_info.get("unrealizedPnl", 0.0))
        inr_rate = self.market.get_inr_rate()

        return {
            "available_usdt": available,
            "available_inr": available * inr_rate,
            "equity_usdt": equity,
            "equity_inr": equity * inr_rate,
            "unrealized_pnl_usdt": unrealized,
            "unrealized_pnl_inr": unrealized * inr_rate,
            "inr_rate": inr_rate
        }

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Retrieves active open futures positions.
        Endpoint: GET /fapi/v1/private/position/open_positions
        
        Args:
            symbol (str, optional): Filter by trading pair.

        Returns:
            List of active position dictionaries.
        """
        params = {"symbol": symbol.upper()} if symbol else None
        res = self.client.get_private(KCEXConfig.ENDPOINT_OPEN_POSITIONS, params=params)
        data = res.get("data", [])
        if isinstance(data, dict):
            data = data.get("list", [])
        if not data and symbol:
            # Fallback: query without symbol filter to prevent API query-param glitches
            try:
                res_all = self.client.get_private(KCEXConfig.ENDPOINT_OPEN_POSITIONS)
                data_all = res_all.get("data", [])
                if isinstance(data_all, dict):
                    data_all = data_all.get("list", [])
                data = [p for p in data_all if p.get("symbol") == symbol.upper()]
            except Exception:
                pass
        return data

    def get_position_history(self, page_num: int = 1, page_size: int = 20) -> List[Dict[str, Any]]:
        """
        Retrieves closed position history.
        Endpoint: GET /fapi/v1/private/position/list/history_positions?page_num=1&page_size=20
        """
        params = {"page_num": page_num, "page_size": page_size}
        res = self.client.get_private(KCEXConfig.ENDPOINT_POSITION_HISTORY, params=params)
        data = res.get("data", {})
        if isinstance(data, dict):
            return data.get("list", [])
        return data if isinstance(data, list) else []

    def get_open_orders(self, page_size: int = 200) -> List[Dict[str, Any]]:
        """
        Retrieves active limit/market orders.
        Endpoint: GET /fapi/v1/private/order/list/open_orders?page_size=200
        """
        params = {"page_size": page_size}
        res = self.client.get_private(KCEXConfig.ENDPOINT_OPEN_ORDERS, params=params)
        data = res.get("data", [])
        return data.get("list", []) if isinstance(data, dict) else data

    def get_open_stop_orders(self) -> List[Dict[str, Any]]:
        """
        Retrieves active stop / plan / TP / SL orders.
        Endpoint: GET /fapi/v1/private/stoporder/open_orders
        """
        res = self.client.get_private(KCEXConfig.ENDPOINT_STOPORDER_OPEN)
        data = res.get("data", [])
        return data.get("list", []) if isinstance(data, dict) else data

    # =========================================================================
    # ORDER EXECUTION (MARKET / LIMIT / ATTACHED TP/SL)
    # =========================================================================

    def create_order(
        self,
        symbol: str,
        side: str,
        vol_contracts: int,
        order_type: str = "MARKET",
        price: Optional[float] = None,
        leverage: Optional[int] = None,
        is_isolated: bool = True,
        take_profit_price: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
        tp_pct: Optional[float] = None,
        sl_pct: Optional[float] = None,
        tp_roe_pct: Optional[float] = None,
        sl_roe_pct: Optional[float] = None,
        price_protect: str = "0"
    ) -> Dict[str, Any]:
        """
        Creates a futures order (Market or Limit, Long or Short) with optional attached TP/SL.
        Endpoint: POST /fapi/v1/private/order/create

        Args:
            symbol (str): e.g. "TRUMP_USDT".
            side (str): "BUY", "LONG", "SELL", or "SHORT".
            vol_contracts (int): Order volume in integer contracts.
            order_type (str): "MARKET" or "LIMIT".
            price (float, optional): Limit price (required if order_type is "LIMIT").
            leverage (int, optional): Leverage multiplier (e.g. 75).
            is_isolated (bool): True for Isolated margin (openType=1), False for Cross (openType=2).
            take_profit_price (float, optional): Attached absolute TP price.
            stop_loss_price (float, optional): Attached absolute SL price.
            tp_pct (float, optional): Attached TP by price move percentage.
            sl_pct (float, optional): Attached SL by price move percentage.
            tp_roe_pct (float, optional): Attached TP by ROE/Margin percentage.
            sl_roe_pct (float, optional): Attached SL by ROE/Margin percentage.
            price_protect (str): Price protection flag ("0" by default).

        Returns:
            Dict: Response containing orderId and timestamp.
        """
        symbol_upper = symbol.upper()
        contract = self.market.get_contract_detail(symbol_upper)

        # Validate minimum volume
        if vol_contracts < contract.min_volume:
            raise ValueError(
                f"Volume {vol_contracts} contracts is below minimum allowed ({contract.min_volume}) for {symbol_upper}."
            )

        # Determine side integer:
        # KCEX sides: 1 = Open Long, 3 = Open Short (2 = Close Short, 4 = Close Long)
        is_long = side.upper() in ("BUY", "LONG")
        side_int = 1 if is_long else 3
        dir_str = "LONG" if is_long else "SHORT"

        # Determine leverage
        if leverage is None:
            leverage = min(contract.max_leverage, 20)
        leverage = max(contract.min_leverage, min(leverage, contract.max_leverage))

        # Order type:
        # "5" = Market Order, "1" = Limit Order
        is_market = order_type.upper() == "MARKET"
        type_str = "5" if is_market else "1"

        # Ticker price for reference/validation
        ticker = self.market.get_ticker(symbol_upper)
        current_price = float(ticker.get("lastPrice", 1.0))
        ref_price = current_price if is_market else (price or current_price)

        # Compute attached TP/SL if percentage or ROE targets provided
        final_tp = take_profit_price
        final_sl = stop_loss_price

        if final_tp is None and tp_pct is not None:
            final_tp = self.risk.calculate_tp_sl_from_price_pct(dir_str, ref_price, tp_pct=tp_pct)["take_profit_price"]
        if final_sl is None and sl_pct is not None:
            final_sl = self.risk.calculate_tp_sl_from_price_pct(dir_str, ref_price, sl_pct=sl_pct)["stop_loss_price"]

        if final_tp is None and tp_roe_pct is not None:
            final_tp = self.risk.calculate_tp_sl_from_roe_pct(dir_str, ref_price, leverage, tp_roe_pct=tp_roe_pct)["take_profit_price"]
        if final_sl is None and sl_roe_pct is not None:
            final_sl = self.risk.calculate_tp_sl_from_roe_pct(dir_str, ref_price, leverage, sl_roe_pct=sl_roe_pct)["stop_loss_price"]

        # Build request payload
        payload: Dict[str, Any] = {
            "symbol": symbol_upper,
            "side": side_int,
            "openType": 1 if is_isolated else 2,
            "type": type_str,
            "vol": int(vol_contracts),
            "leverage": int(leverage),
            "marketCeiling": False,
            "bboPriceType": 0,
            "priceProtect": price_protect
        }

        # If limit order, add price
        if not is_market:
            if price is None:
                raise ValueError("Limit order requires 'price' parameter.")
            payload["price"] = format_price_str(price, contract.price_precision)

        # Attached Stop Loss
        if final_sl is not None:
            payload["stopLossPrice"] = format_price_str(final_sl, contract.price_precision)
            payload["lossTrend"] = "1"

        # Attached Take Profit
        if final_tp is not None:
            payload["takeProfitPrice"] = format_price_str(final_tp, contract.price_precision)
            payload["profitTrend"] = "1"

        logger.info("Submitting order: %s", payload)
        res = self.client.post_private(KCEXConfig.ENDPOINT_ORDER_CREATE, json_data=payload)
        return res

    # =========================================================================
    # POSITION CLOSING & PARTIAL CLOSE
    # =========================================================================

    def close_position(
        self,
        position_id: int,
        symbol: str,
        side: str,
        vol_contracts: int,
        leverage: int,
        is_isolated: bool = True,
        is_market: bool = True,
        price: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Closes a position (or part of it) using a market or limit close order.
        Endpoint: POST /fapi/v1/private/order/create

        Args:
            position_id (int): ID of the open position.
            symbol (str): Trading pair symbol.
            side (str): "LONG" (to close long) or "SHORT" (to close short).
            vol_contracts (int): Contracts to close.
            leverage (int): Position leverage.
            is_isolated (bool): Margin mode.
            is_market (bool): True for market close.
            price (float, optional): Price for limit close, or reference price for market.
        """
        symbol_upper = symbol.upper()
        contract = self.market.get_contract_detail(symbol_upper)

        # Closing side: 4 = Close Long, 2 = Close Short
        is_closing_long = side.upper() in ("LONG", "BUY")
        close_side = 4 if is_closing_long else 2

        if price is None:
            if not is_market:
                raise ValueError("Limit close orders require an explicit 'price' parameter.")
            ticker = self.market.get_ticker(symbol_upper)
            price = float(ticker.get("lastPrice", 1.0))

        # For market close: aggressively cross the order book to ensure instant taker execution
        final_price = price
        if is_market:
            pu = contract.price_unit
            if is_closing_long:
                # Sell into the bids: price below market crosses bid queue
                final_price = min(price * 0.985, price - 10 * pu)
            else:
                # Buy from the asks: price above market crosses ask queue
                final_price = max(price * 1.015, price + 10 * pu)

        payload = {
            "symbol": symbol_upper,
            "openType": 1 if is_isolated else 2,
            "positionId": int(position_id),
            "leverage": int(leverage),
            "type": 5 if is_market else 1,  # 5 for Market order, 1 for Limit order
            "vol": int(vol_contracts),
            "side": close_side,
            "flashClose": False,
            "price": format_price_str(final_price, contract.price_precision),
            "priceProtect": "0"
        }

        logger.info("Submitting close order: %s", payload)
        return self.client.post_private(KCEXConfig.ENDPOINT_ORDER_CREATE, json_data=payload)

    def close_position_limit(
        self,
        symbol: str,
        side: str,
        price: float,
        vol_contracts: Optional[int] = None,
        position_id: Optional[int] = None,
        leverage: Optional[int] = None,
        is_isolated: bool = True
    ) -> Dict[str, Any]:
        """
        Closes an open position (or part of it) using a Limit Order.
        Endpoint: POST /fapi/v1/private/order/create (type: 1)

        Args:
            symbol (str): Trading pair (e.g. "MOG_USDT" or "TRUMP_USDT").
            side (str): Direction being closed: "SHORT" (side=2) or "LONG" (side=4).
            price (float): Limit price to exit at.
            vol_contracts (int, optional): Contracts to close. If None, closes entire active position.
            position_id (int, optional): Open position ID. If None, auto-detected from open positions.
            leverage (int, optional): Position leverage. If None, auto-detected from active position.
            is_isolated (bool): True for Isolated margin (openType=1), False for Cross (openType=2).

        Returns:
            Dict: API response containing orderId and ts.
        """
        symbol_upper = symbol.upper()
        contract = self.market.get_contract_detail(symbol_upper)
        side_norm = side.upper()
        if side_norm not in ("LONG", "SHORT", "BUY", "SELL"):
            raise ValueError(f"Invalid side: {side}. Must be 'LONG' or 'SHORT'.")

        target_side_type = "LONG" if side_norm in ("LONG", "BUY") else "SHORT"

        # If position_id, leverage, or vol_contracts are not provided, auto-resolve from open positions
        if position_id is None or leverage is None or vol_contracts is None:
            open_positions = self.get_open_positions(symbol_upper)
            matching = []
            for p in open_positions:
                pos_side = p.get("side") or p.get("positionType")
                # KCEX open positions convention: 1 = Long, 2 = Short, or string 'LONG'/'SHORT'
                is_match = False
                if target_side_type == "LONG" and (pos_side == 1 or str(pos_side).upper() == "LONG" or (p.get("holdVol", 0) > 0 and pos_side != 2)):
                    is_match = True
                elif target_side_type == "SHORT" and (pos_side in (2, 3) or str(pos_side).upper() == "SHORT"):
                    is_match = True
                if is_match or len(open_positions) == 1:
                    matching.append(p)

            if matching:
                pos = matching[0]
                if position_id is None:
                    position_id = int(pos.get("positionId") or pos.get("id"))
                if leverage is None:
                    leverage = int(pos.get("leverage", contract.max_leverage))
                if vol_contracts is None:
                    vol_contracts = int(pos.get("holdVol", 0))

        if position_id is None:
            raise ValueError(f"No active {target_side_type} position found for {symbol_upper} to close. Specify position_id explicitly.")

        if vol_contracts is None or vol_contracts <= 0:
            raise ValueError(f"Invalid volume: {vol_contracts}. Must be a positive integer of contracts.")

        if price is None or price <= 0:
            raise ValueError(f"Invalid limit price: {price}. Limit orders require a valid positive price.")

        if leverage is None:
            leverage = contract.max_leverage

        return self.close_position(
            position_id=position_id,
            symbol=symbol_upper,
            side=target_side_type,
            vol_contracts=vol_contracts,
            leverage=leverage,
            is_isolated=is_isolated,
            is_market=False,
            price=price
        )

    def close_short_limit(
        self,
        symbol: str,
        price: float,
        vol_contracts: Optional[int] = None,
        position_id: Optional[int] = None,
        leverage: Optional[int] = None,
        is_isolated: bool = True
    ) -> Dict[str, Any]:
        """
        Convenience helper to close a SHORT position using a Limit Order (side=2, type=1).
        """
        return self.close_position_limit(
            symbol=symbol,
            side="SHORT",
            price=price,
            vol_contracts=vol_contracts,
            position_id=position_id,
            leverage=leverage,
            is_isolated=is_isolated
        )

    def close_long_limit(
        self,
        symbol: str,
        price: float,
        vol_contracts: Optional[int] = None,
        position_id: Optional[int] = None,
        leverage: Optional[int] = None,
        is_isolated: bool = True
    ) -> Dict[str, Any]:
        """
        Convenience helper to close a LONG position using a Limit Order (side=4, type=1).
        """
        return self.close_position_limit(
            symbol=symbol,
            side="LONG",
            price=price,
            vol_contracts=vol_contracts,
            position_id=position_id,
            leverage=leverage,
            is_isolated=is_isolated
        )

    def close_partial_position(
        self,
        position_id: int,
        symbol: str,
        side: str,
        total_vol: int,
        leverage: int,
        close_percentage: float = 50.0,
        is_isolated: bool = True
    ) -> Dict[str, Any]:
        """
        Partially closes a position by percentage (e.g. 50%, 25%, 75%).
        Validates that both the closed volume and remaining volume meet the symbol's min_volume.
        """
        contract = self.market.get_contract_detail(symbol)
        ratio = max(0.01, min(close_percentage / 100.0, 1.0))
        vol_to_close = int(math.floor((total_vol * ratio) / contract.volume_unit) * contract.volume_unit)

        if vol_to_close < contract.min_volume:
            raise ValueError(
                f"Partial close of {close_percentage}% results in {vol_to_close} contracts, "
                f"which is less than the minimum required ({contract.min_volume} contracts)."
            )

        return self.close_position(
            position_id=position_id,
            symbol=symbol,
            side=side,
            vol_contracts=vol_to_close,
            leverage=leverage,
            is_isolated=is_isolated,
            is_market=True
        )

    # =========================================================================
    # POST-TRADE TP / SL MANAGEMENT
    # =========================================================================

    def set_position_tp_sl(
        self,
        symbol: str,
        position_id: int,
        take_profit_price: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
        stop_plan_order_id: Optional[int] = None,
        vol_type: int = 2,
        price_protect: str = "0"
    ) -> Dict[str, Any]:
        """
        Places or modifies TP/SL on an open position.
        
        Handles two cases seamlessly:
        1. If an attached TP/SL or stop order already exists on this position:
           Updates the trigger prices via POST /fapi/v1/private/stoporder/change_plan_price.
        2. If no stop order exists yet:
           Places a new stop order via POST /fapi/v1/private/stoporder/place/v2 with volType=2.

        Args:
            symbol (str): Trading pair symbol (e.g. "TRUMP_USDT").
            position_id (int): The open position ID.
            take_profit_price (float, optional): Take profit trigger price.
            stop_loss_price (float, optional): Stop loss trigger price.
            stop_plan_order_id (int, optional): Existing stop order ID if known.
            vol_type (int): 2 for POSITION_VOL, 1 for BATCH_VOL.
            price_protect (str): "0" or "1".
        """
        symbol_upper = symbol.upper()
        contract = self.market.get_contract_detail(symbol_upper)

        # 1. Check if a stop order already exists for this position or symbol
        existing_stop_id = stop_plan_order_id
        if existing_stop_id is None:
            try:
                open_stops = self.get_open_stop_orders()
                for s in open_stops:
                    pos_id_in_stop = s.get("positionId")
                    if pos_id_in_stop and int(pos_id_in_stop) == int(position_id):
                        existing_stop_id = s.get("id")
                        break
                    elif s.get("symbol") == symbol_upper:
                        existing_stop_id = s.get("id")
                        break
            except Exception as e:
                logger.debug("Could not inspect open stop orders: %s", e)

        # Case 1: Existing stop order found -> modify it via change_plan_price
        if existing_stop_id:
            change_payload: Dict[str, Any] = {
                "stopPlanOrderId": int(existing_stop_id),
                "positionId": int(position_id),
                "volType": 2,
                "takeProfitReverse": 2,
                "stopLossReverse": 2
            }
            if take_profit_price is not None:
                change_payload["takeProfitPrice"] = str(round(take_profit_price, contract.price_precision))
                change_payload["profitTrend"] = "1"
            if stop_loss_price is not None:
                change_payload["stopLossPrice"] = str(round(stop_loss_price, contract.price_precision))
                change_payload["lossTrend"] = "1"

            try:
                logger.info("Modifying existing stop order %s: %s", existing_stop_id, change_payload)
                return self.client.post_private(KCEXConfig.ENDPOINT_STOPORDER_CHANGE_PRICE, json_data=change_payload)
            except KCEXAPIError as err:
                logger.warning("change_plan_price failed (%s), will cancel and replace via place/v2: %s", err, err.message)
                try:
                    self.cancel_stop_order(int(existing_stop_id))
                except Exception:
                    pass

        # Case 2: Fresh stop order placement via place/v2
        place_payload: Dict[str, Any] = {
            "symbol": symbol_upper,
            "positionId": int(position_id),
            "volType": int(vol_type),         # Integer 2 (POSITION_VOL)
            "takeProfitReverse": 2,          # Integer 2 (UNCHECKED)
            "stopLossReverse": 2,            # Integer 2 (UNCHECKED)
            "priceProtect": str(price_protect)
        }

        if take_profit_price is not None:
            place_payload["takeProfitPrice"] = str(round(take_profit_price, contract.price_precision))
            place_payload["profitTrend"] = "1"

        if stop_loss_price is not None:
            place_payload["stopLossPrice"] = str(round(stop_loss_price, contract.price_precision))
            place_payload["lossTrend"] = "1"

        logger.info("Placing new position TP/SL: %s", place_payload)
        return self.client.post_private(KCEXConfig.ENDPOINT_STOPORDER_PLACE, json_data=place_payload)

    # =========================================================================
    # ORDER CANCELLATION
    # =========================================================================

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """
        Cancels an ordinary limit/market order.
        Endpoint: POST /fapi/v1/private/order/cancel
        Payload: [ order_id ]
        """
        payload = [str(order_id)]
        return self.client.post_private(KCEXConfig.ENDPOINT_ORDER_CANCEL, json_data=payload)

    def cancel_stop_order(self, stop_plan_order_id: int) -> Dict[str, Any]:
        """
        Cancels a stop / plan / TP / SL order.
        Endpoint: POST /fapi/v1/private/stoporder/cancel
        Payload: [{ "stopPlanOrderId": int }]
        """
        payload = [{"stopPlanOrderId": int(stop_plan_order_id)}]
        return self.client.post_private(KCEXConfig.ENDPOINT_STOPORDER_CANCEL, json_data=payload)

    def cancel_all_orders(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """
        Cancels all active open orders (ordinary orders and stop/plan orders).
        """
        results = {"cancelled_orders": 0, "cancelled_stop_orders": 0}

        # 1. Cancel ordinary orders
        try:
            open_orders = self.get_open_orders()
            if symbol:
                open_orders = [o for o in open_orders if o.get("symbol") == symbol.upper()]
            if open_orders:
                order_ids = [str(o.get("orderId")) for o in open_orders if o.get("orderId")]
                if order_ids:
                    self.client.post_private(KCEXConfig.ENDPOINT_ORDER_CANCEL, json_data=order_ids)
                    results["cancelled_orders"] = len(order_ids)
        except Exception as e:
            logger.warning("Error cancelling ordinary orders: %s", e)

        # 2. Cancel stop/plan orders
        try:
            open_stops = self.get_open_stop_orders()
            if symbol:
                open_stops = [s for s in open_stops if s.get("symbol") == symbol.upper()]
            if open_stops:
                stop_payload = [{"stopPlanOrderId": int(s.get("id"))} for s in open_stops if s.get("id")]
                if stop_payload:
                    self.client.post_private(KCEXConfig.ENDPOINT_STOPORDER_CANCEL, json_data=stop_payload)
                    results["cancelled_stop_orders"] = len(stop_payload)
        except Exception as e:
            logger.warning("Error cancelling stop orders: %s", e)

        return results
