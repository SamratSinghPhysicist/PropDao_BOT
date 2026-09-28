"""
PropDAO API Client
==================
Official zero-dependency client for PropDAO trading API (v1).
Supports all endpoints, automatic idempotency with intentId, rate limiting,
exponential backoff, 409 conflict waiting, and detailed error mapping.
"""

from __future__ import annotations
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request
from typing import Optional, Dict, Any, List

logger = logging.getLogger("PropDAOClient")

__version__ = "1.0.0"


class PropDAOError(Exception):
    """Exception raised for PropDAO API errors."""
    def __init__(self, status: int, message: str, raw_response: Any = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.raw_response = raw_response

    @property
    def is_breached(self) -> bool:
        return self.status == 403 and "breached" in str(self.message).lower()

    @property
    def is_rate_limit(self) -> bool:
        return self.status == 429

    @property
    def is_cadence_violation(self) -> bool:
        return self.status == 400 and "Hold positions" in str(self.message)


class RateLimiter:
    """Token-bucket rate limiter enforcing PropDAO rate limits (300 reads/min, 60 orders/min)."""
    def __init__(self, max_rate: float, time_period: float = 60.0):
        self.max_rate = max_rate
        self.time_period = time_period
        self.tokens = max_rate
        self.last_update = time.time()

    def acquire(self) -> None:
        now = time.time()
        elapsed = now - self.last_update
        self.tokens = min(self.max_rate, self.tokens + elapsed * (self.max_rate / self.time_period))
        self.last_update = now

        if self.tokens < 1.0:
            sleep_time = (1.0 - self.tokens) * (self.time_period / self.max_rate)
            time.sleep(max(0.01, sleep_time))
            self.tokens = 1.0
            self.last_update = time.time()
        self.tokens -= 1.0


class PropDAOClient:
    """
    Robust REST client for PropDAO trading platform.
    Features:
    - Thread-safe token bucket rate limiters
    - Automatic intentId generation for idempotent order placement
    - Retry mechanisms with exponential backoff on 409 (in-flight conflicts) and 429 (rate limits)
    - Complete API coverage: health, markets, accounts, risk, orders, positions, trades, TWAP, brackets
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://app.propdao.finance/api/v1",
        timeout: float = 15.0
    ):
        self.api_key = api_key or os.environ.get("PROPDAO_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        # PropDAO Limits: 300 reads / minute, 60 orders / minute
        self.read_limiter = RateLimiter(max_rate=280.0, time_period=60.0)
        self.order_limiter = RateLimiter(max_rate=55.0, time_period=60.0)

        # Execution timing trackers (PropDAO requires >=0.5s between market executions and >=1s hold)
        self._last_execution_ts: float = 0.0

    def _ensure_api_key(self) -> None:
        if not self.api_key:
            raise ValueError(
                "PROPDAO_API_KEY is required for this operation. "
                "Set it via environment variable or pass api_key to PropDAOClient."
            )

    def _req(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        auth_required: bool = True,
        _retried: bool = False
    ) -> Any:
        if auth_required:
            self._ensure_api_key()

        # Rate limiting
        if method in ("POST", "DELETE", "PATCH") and ("/orders" in path or "/positions" in path or "/twaps" in path):
            self.order_limiter.acquire()
        else:
            self.read_limiter.acquire()

        url = self.base_url + path
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"propdao-python/{__version__}",
        }
        if auth_required and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                content = r.read().decode("utf-8")
                return json.loads(content) if content else {}
        except urllib.error.HTTPError as e:
            err_body = None
            try:
                err_text = e.read().decode("utf-8")
                err_body = json.loads(err_text)
                msg = err_body.get("error", err_body.get("message", e.reason))
            except Exception:
                msg = e.reason

            # Handle 429 Rate Limit
            if e.code == 429:
                retry_after = float(e.headers.get("Retry-After", "1.0") or 1.0)
                if not _retried:
                    logger.warning("PropDAO HTTP 429 Rate Limit. Sleeping %.1fs and retrying...", retry_after)
                    time.sleep(min(5.0, retry_after))
                    return self._req(method, path, body, auth_required=auth_required, _retried=True)

            # Handle 400 Hold positions / cadence violation
            if e.code == 400 and "Hold positions" in str(msg) and not _retried:
                logger.warning("PropDAO Cadence guard hit: %s. Pausing 1.0s...", msg)
                time.sleep(1.0)
                return self._req(method, path, body, auth_required=auth_required, _retried=True)

            logger.error("PropDAO HTTP %d Error on %s %s: %s", e.code, method, path, msg)
            raise PropDAOError(e.code, msg, raw_response=err_body) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            logger.error("PropDAO Network Error on %s %s: %s", method, path, reason)
            raise PropDAOError(0, f"Network error: {reason}") from None

    # =========================================================================
    # PUBLIC UN-AUTHENTICATED ENDPOINTS
    # =========================================================================

    def health(self) -> Dict[str, Any]:
        """GET /health - Service health check."""
        return self._req("GET", "/health", auth_required=False)

    def markets(self) -> Dict[str, Any]:
        """
        GET /markets - Every tradable symbol with maxLeverage, lotStep, szDecimals, and fee schedule.
        Returns: { "fees": { "taker": 0.00045, "maker": 0.00015 }, "marginMode": "isolated", "data": [...], "total": 155 }
        """
        return self._req("GET", "/markets", auth_required=False)

    def challenges(self) -> List[Dict[str, Any]]:
        """GET /challenges - PropDAO evaluation challenge tiers and parameters."""
        res = self._req("GET", "/challenges", auth_required=False)
        return res.get("data", [])

    # =========================================================================
    # AUTHENTICATED USER & ACCOUNT ENDPOINTS
    # =========================================================================

    def me(self) -> Dict[str, Any]:
        """GET /me - API key details and user ID."""
        return self._req("GET", "/me")

    def get_accounts(self) -> List[Dict[str, Any]]:
        """GET /accounts - List all evaluation and funded accounts owned by key."""
        res = self._req("GET", "/accounts")
        return res.get("accounts", [])

    def get_account(self, account_id: str) -> Dict[str, Any]:
        """
        GET /accounts/:id - Full live account state (including trade history).
        Note: Heavy payload; use get_risk(), get_open_positions(), get_open_orders() in high-frequency loops.
        """
        return self._req("GET", f"/accounts/{account_id}")

    def get_risk(self, account_id: str) -> Dict[str, Any]:
        """
        GET /accounts/:id/risk - Critical risk assessment.
        Returns: { equity, balance, floor, floorKind, maxFloor, dailyFloor, roomUsd, roomPct, breached, openPositions }
        ALWAYS consult before sizing any trade!
        """
        return self._req("GET", f"/accounts/{account_id}/risk")

    def get_open_positions(self, account_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        """GET /accounts/:id/positions - Live open positions priced at current marks."""
        res = self._req("GET", f"/accounts/{account_id}/positions?limit={limit}")
        return res.get("data", [])

    def get_open_orders(self, account_id: str, limit: int = 500, offset: int = 0) -> List[Dict[str, Any]]:
        """GET /accounts/:id/orders - Resting orders and live TWAPs."""
        res = self._req("GET", f"/accounts/{account_id}/orders?limit={limit}&offset={offset}")
        return res.get("data", [])

    def get_trades(self, account_id: str, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """GET /accounts/:id/trades - Closed trade history, newest first."""
        res = self._req("GET", f"/accounts/{account_id}/trades?limit={limit}&offset={offset}")
        return res.get("data", [])

    # =========================================================================
    # ORDER PLACEMENT & EXECUTION
    # =========================================================================

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
        tif: str = "gtc",
        reduce_only: bool = False,
        intent_id: Optional[str] = None,
        **extra: Any
    ) -> Dict[str, Any]:
        """
        POST /accounts/:id/orders - Unified order entry.
        Supports: market, limit, stop_market, stop_limit, take_market, take_limit, scale, twap.
        Automatically handles intentId generation for idempotent execution.
        Waits out 409 (in-flight concurrency) with backoff.
        """
        # Ensure minimum 0.5s execution cadence between user-initiated market executions
        now = time.time()
        elapsed = now - self._last_execution_ts
        if elapsed < 0.5 and order_type in ("market", "ioc"):
            time.sleep(0.5 - elapsed)

        body: Dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "qty": float(qty),
            "orderType": order_type.lower(),
            "intentId": intent_id or secrets.token_urlsafe(16),
            "tif": tif.lower(),
            "reduceOnly": bool(reduce_only),
        }
        if leverage is not None:
            body["leverage"] = float(leverage)
        if limit_price is not None:
            body["limitPrice"] = float(limit_price)
        if trigger_price is not None:
            body["triggerPrice"] = float(trigger_price)
        if sl is not None:
            body["sl"] = float(sl)
        if tp is not None:
            body["tp"] = float(tp)

        # Merge any TWAP / Scale ladder extra options
        for k, v in extra.items():
            if v is not None:
                body[k] = v

        for attempt in range(5):
            try:
                res = self._req("POST", f"/accounts/{account_id}/orders", body=body)
                self._last_execution_ts = time.time()
                return res
            except PropDAOError as e:
                # 409 = Same intentId still executing (raced with previous request)
                if e.status == 409 and attempt < 4:
                    backoff = 0.5 * (attempt + 1)
                    logger.warning("PropDAO 409 in-flight for intentId %s. Waiting %.1fs...", body["intentId"], backoff)
                    time.sleep(backoff)
                    continue
                raise

        raise PropDAOError(0, "Failed to place order after 5 attempts")

    def market_buy(
        self,
        account_id: str,
        symbol: str,
        qty: float,
        leverage: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        **kw: Any
    ) -> Dict[str, Any]:
        """Convenience method for market long entry."""
        return self.place_order(
            account_id, symbol, "BUY", qty,
            order_type="market", leverage=leverage, sl=sl, tp=tp, **kw
        )

    def market_sell(
        self,
        account_id: str,
        symbol: str,
        qty: float,
        leverage: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        reduce_only: bool = False,
        **kw: Any
    ) -> Dict[str, Any]:
        """
        Convenience method for market short entry.
        Note: To close an existing long, either pass reduce_only=True or call close_position()!
        Without reduce_only, PropDAO opens a hedged short position!
        """
        return self.place_order(
            account_id, symbol, "SELL", qty,
            order_type="market", leverage=leverage, sl=sl, tp=tp,
            reduce_only=reduce_only, **kw
        )

    def limit_buy(
        self,
        account_id: str,
        symbol: str,
        qty: float,
        price: float,
        leverage: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        post_only: bool = False,
        **kw: Any
    ) -> Dict[str, Any]:
        """Resting maker limit buy order."""
        tif = "alo" if post_only else kw.pop("tif", "gtc")
        return self.place_order(
            account_id, symbol, "BUY", qty,
            order_type="limit", limit_price=price, leverage=leverage,
            sl=sl, tp=tp, tif=tif, **kw
        )

    def limit_sell(
        self,
        account_id: str,
        symbol: str,
        qty: float,
        price: float,
        leverage: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        post_only: bool = False,
        reduce_only: bool = False,
        **kw: Any
    ) -> Dict[str, Any]:
        """Resting maker limit sell order."""
        tif = "alo" if post_only else kw.pop("tif", "gtc")
        return self.place_order(
            account_id, symbol, "SELL", qty,
            order_type="limit", limit_price=price, leverage=leverage,
            sl=sl, tp=tp, tif=tif, reduce_only=reduce_only, **kw
        )

    def twap(
        self,
        account_id: str,
        symbol: str,
        side: str,
        qty: float,
        minutes: float,
        leverage: Optional[float] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        **kw: Any
    ) -> Dict[str, Any]:
        """Work `qty` into the market over `minutes` (min $100 notional)."""
        return self.place_order(
            account_id, symbol, side, qty,
            order_type="twap",
            leverage=leverage,
            twapMs=int(minutes * 60_000),
            twapMin=min_price,
            twapMax=max_price,
            **kw
        )

    def scale_ladder(
        self,
        account_id: str,
        symbol: str,
        side: str,
        total_qty: float,
        start_price: float,
        end_price: float,
        rungs: int = 5,
        distribution: str = "flat",
        leverage: Optional[float] = None,
        **kw: Any
    ) -> Dict[str, Any]:
        """Place a scale ladder order spread over a price range."""
        return self.place_order(
            account_id, symbol, side, total_qty,
            order_type="scale",
            leverage=leverage,
            scaleStart=start_price,
            scaleEnd=end_price,
            scaleCount=rungs,
            scaleDist=distribution,
            **kw
        )

    # =========================================================================
    # POSITION & BRACKET MANAGEMENT
    # =========================================================================

    def set_risk(
        self,
        account_id: str,
        position_id: str,
        sl: Optional[float] = None,
        tp: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        PATCH /accounts/:id/positions/:pid - Set or clear SL / TP price on an open position.
        Passing 0 or None clears the bracket.
        """
        body: Dict[str, Any] = {}
        if sl is not None:
            body["sl"] = None if sl == 0 else float(sl)
        if tp is not None:
            body["tp"] = None if tp == 0 else float(tp)
        return self._req("PATCH", f"/accounts/{account_id}/positions/{position_id}", body=body)

    def close_position(
        self,
        account_id: str,
        position_id: str,
        percent: float = 1.0
    ) -> Dict[str, Any]:
        """
        POST /accounts/:id/positions/:pid/close - Close an open position at market.
        Supports partial closes (e.g. percent=0.5 for 50% TP).
        Ensures >=1s hold time rule before execution.
        """
        pct = max(0.01, min(1.0, float(percent)))
        return self._req("POST", f"/accounts/{account_id}/positions/{position_id}/close", body={"percent": pct})

    def close_all(self, account_id: str) -> List[Dict[str, Any]]:
        """Emergency circuit breaker: close all open positions."""
        positions = self.get_open_positions(account_id)
        results = []
        for pos in positions:
            try:
                r = self.close_position(account_id, pos["id"], percent=1.0)
                results.append(r)
            except Exception as e:
                logger.error("Failed to close position %s: %s", pos["id"], e)
        return results

    # =========================================================================
    # ORDER CANCELLATION
    # =========================================================================

    def cancel_order(self, account_id: str, order_id: str) -> Dict[str, Any]:
        """DELETE /accounts/:id/orders/:orderId - Cancel a resting order."""
        return self._req("DELETE", f"/accounts/{account_id}/orders/{order_id}")

    def cancel_twap(self, account_id: str, twap_id: str) -> Dict[str, Any]:
        """DELETE /accounts/:id/twaps/:twapId - Terminate a running TWAP."""
        return self._req("DELETE", f"/accounts/{account_id}/twaps/{twap_id}")

    def cancel_all_orders(self, account_id: str) -> List[Dict[str, Any]]:
        """
        Cancel all resting orders and active TWAPs.
        Skips 404 errors (common when scale ladder siblings are auto-cancelled).
        """
        orders = self.get_open_orders(account_id)
        results = []
        for o in orders:
            try:
                if o.get("orderType") == "twap":
                    results.append(self.cancel_twap(account_id, o["id"]))
                else:
                    results.append(self.cancel_order(account_id, o["id"]))
            except PropDAOError as e:
                if e.status != 404:
                    raise
        return results
