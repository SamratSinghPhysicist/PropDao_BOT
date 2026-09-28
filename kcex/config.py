"""
KCEX Trading Bot - Configuration & Constants
=============================================
This module defines the API base URLs, endpoint routes, default headers,
and configuration management for connecting to the KCEX platform.

Reverse-Engineered Endpoints Catalog:
- Base Futures REST: https://www.kcex.com/fapi/v1
- Base Platform REST: https://www.kcex.com/api/platform
- Futures WebSocket: wss://www.kcex.com/...
"""

import os
from typing import Optional, Dict, Any


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def load_env_file(dotenv_path: Optional[str] = None) -> None:
    """
    Lightweight, zero-dependency .env loader.
    Searches workspace root, current working directory, and config directory.
    """
    if dotenv_path is None:
        candidates = [
            os.path.join(ROOT_DIR, ".env"),
            os.path.join(os.getcwd(), ".env"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                dotenv_path = os.path.abspath(path)
                break

    if not dotenv_path or not os.path.isfile(dotenv_path):
        return

    try:
        with open(dotenv_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        current_key = None
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            if "=" in stripped:
                k, v = stripped.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k:
                    if v:
                        os.environ[k] = v
                        current_key = None
                    else:
                        # Key present but value might be on next line
                        current_key = k
            elif current_key:
                val = stripped.strip("'\"")
                os.environ[current_key] = val
                current_key = None
    except Exception:
        pass


class KCEXConfig:
    """
    Configuration container for KCEX Futures Bot.
    
    Attributes:
        fapi_base_url (str): Base URL for KCEX Futures API (fapi/v1).
        platform_base_url (str): Base URL for KCEX Common Platform API (currency, assets).
        auth_token (str): The session Authorization token copied from browser DevTools.
        user_device (str): Base64-encoded User-Device fingerprint string.
        cookie (str): Optional cookie string.
        timeout (int): Request timeout in seconds.
    """

    # --- BASE API URLS ---
    DEFAULT_FAPI_BASE_URL: str = "https://www.kcex.com/fapi/v1"
    DEFAULT_PLATFORM_BASE_URL: str = "https://www.kcex.com/api/platform"

    # --- CLIENT VERSION HEADERS (Captured from web-futures v3.7.91) ---
    CLIENT_PLATFORM: str = "WEB"
    CLIENT_VERSION: str = "1.0.0"
    CLIENT_VERSION_TAG: str = "prd - v3.7.91 - fdef20f"
    CLIENT_LANGUAGE: str = "en-US"
    CLIENT_TIMEZONE: str = "Asia/Kolkata"

    # --- PUBLIC MARKET DATA ENDPOINTS ---
    ENDPOINT_CONTRACT_DETAIL: str = "/contract/detailV2"       # GET ?client=web or ?symbol=...
    ENDPOINT_CONTRACT_TICKER: str = "/contract/ticker"         # GET ?symbol=...
    ENDPOINT_CONTRACT_DEPTH: str = "/contract/depth_step/{symbol}" # GET ?step=0.001
    ENDPOINT_CONTRACT_KLINES: str = "/contract/kline/{symbol}" # GET ?interval=Min1&start=...&end=...
    ENDPOINT_CONTRACT_DEALS: str = "/contract/deals/{symbol}"  # GET recent trades
    ENDPOINT_FUNDING_RATE: str = "/contract/funding_rate/{symbol}" # GET funding rate
    ENDPOINT_PING: str = "/contract/ping"                      # GET connectivity probe
    
    # --- COMMON PLATFORM ENDPOINTS ---
    ENDPOINT_EXCHANGE_RATE: str = "/common/currency/exchange/rate" # GET real-time fiat rates (USD/INR)
    ENDPOINT_ASSET_CONVERT: str = "/asset/api/asset/overview/convert/v2" # GET display balances

    # --- PRIVATE ACCOUNT & POSITION ENDPOINTS ---
    ENDPOINT_ACCOUNT_ASSETS: str = "/private/account/assets"               # GET futures USDT balances
    ENDPOINT_TIERED_FEE_RATE: str = "/private/account/tiered_fee_rate"     # GET ?symbol=... effective fee
    ENDPOINT_OPEN_POSITIONS: str = "/private/position/open_positions"      # GET active open positions
    ENDPOINT_POSITION_HISTORY: str = "/private/position/list/history_positions" # GET closed positions
    ENDPOINT_POSITION_LEVERAGE: str = "/private/position/leverage"         # GET/POST leverage
    ENDPOINT_CALC_LIQUIDATE_PRICE: str = "/private/position/order/calc_liquidate_price/v2" # POST liq preview

    # --- PRIVATE ORDER ENDPOINTS ---
    ENDPOINT_ORDER_CREATE: str = "/private/order/create"                   # POST open/close market/limit order
    ENDPOINT_ORDER_CANCEL: str = "/private/order/cancel"                   # POST cancel single order
    ENDPOINT_ORDER_CANCEL_ALL: str = "/private/order/cancel_all"           # POST cancel all open orders
    ENDPOINT_OPEN_ORDERS: str = "/private/order/list/open_orders"          # GET open orders list
    ENDPOINT_ORDER_HISTORY: str = "/private/order/list/history_orders"     # GET historical orders
    ENDPOINT_ORDER_DEALS: str = "/private/order/list/order_deals"          # GET trade fills

    # --- PRIVATE STOP / TP / SL ENDPOINTS ---
    ENDPOINT_STOPORDER_PLACE: str = "/private/stoporder/place/v2"          # POST set position TP/SL
    ENDPOINT_STOPORDER_CHANGE_PLAN: str = "/private/stoporder/change_plan_order" # POST edit stop order
    ENDPOINT_STOPORDER_CHANGE_PRICE: str = "/private/stoporder/change_plan_price" # POST edit trigger price
    ENDPOINT_STOPORDER_OPEN: str = "/private/stoporder/open_orders"        # GET active stop orders
    ENDPOINT_STOPORDER_CANCEL: str = "/private/stoporder/cancel"           # POST cancel stop order
    ENDPOINT_STOPORDER_CANCEL_ALL: str = "/private/stoporder/cancel_all"   # POST cancel all stop orders
    ENDPOINT_PLANORDER_PLACE: str = "/private/planorder/place/v2"          # POST trigger order
    ENDPOINT_PLANORDER_CANCEL: str = "/private/planorder/cancel"           # POST cancel trigger order

    def __init__(
        self,
        auth_token: Optional[str] = None,
        user_device: Optional[str] = None,
        cookie: Optional[str] = None,
        fapi_base_url: Optional[str] = None,
        platform_base_url: Optional[str] = None,
        timeout: int = 10,
        env_file: Optional[str] = None
    ):
        """
        Initialize KCEX configuration, automatically loading .env if present.
        """
        load_env_file(env_file)
        self.auth_token = auth_token or os.getenv("KCEX_AUTH_TOKEN", "").strip()
        self.user_device = user_device or os.getenv("KCEX_USER_DEVICE", "").strip()
        self.cookie = cookie or os.getenv("KCEX_COOKIE", "").strip()
        self.fapi_base_url = fapi_base_url or self.DEFAULT_FAPI_BASE_URL
        self.platform_base_url = platform_base_url or self.DEFAULT_PLATFORM_BASE_URL
        self.timeout = timeout

    @property
    def is_authenticated(self) -> bool:
        """Returns True if an authorization token is configured."""
        return bool(self.auth_token)

    def to_dict(self) -> Dict[str, Any]:
        """Returns non-sensitive configuration dictionary."""
        return {
            "fapi_base_url": self.fapi_base_url,
            "platform_base_url": self.platform_base_url,
            "is_authenticated": self.is_authenticated,
            "has_user_device": bool(self.user_device),
            "timeout": self.timeout
        }
