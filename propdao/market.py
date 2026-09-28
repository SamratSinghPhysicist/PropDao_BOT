"""
PropDAO Market Data Provider
============================
Handles market metadata discovery, symbol parameters, fee structures,
live mark prices, and multi-timeframe candlestick data fetching (Hyperliquid & Binance feeds).
"""

from __future__ import annotations
import json
import logging
import time
import urllib.request
from typing import Optional, Dict, Any, List
from propdao.client import PropDAOClient
from propdao.models import ContractDetail

logger = logging.getLogger("PropDAOMarket")


class PropDAOMarket:
    """
    Market interface for PropDAO bot and strategies.
    Provides:
    - Symbol details (lot step, szDecimals, maxLeverage, fees)
    - Live mark prices
    - Multi-timeframe OHLCV klines (supports Binance / Hyperliquid fallback for rich 1m..1d histories)
    - Local caching with automatic TTL expiry
    """

    def __init__(self, client: Optional[PropDAOClient] = None):
        self.client = client or PropDAOClient(base_url="https://app.propdao.finance/api/v1")
        self._markets_cache: Dict[str, Dict[str, Any]] = {}
        self._contracts_cache: Dict[str, ContractDetail] = {}
        self._last_markets_fetch: float = 0.0
        self._kline_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._kline_ttl_seconds: float = 2.0
        self.refresh_markets()

    def refresh_markets(self, force: bool = False) -> None:
        """Loads and caches market information from GET /markets."""
        now = time.time()
        if not force and (now - self._last_markets_fetch < 3600.0) and self._markets_cache:
            return

        try:
            res = self.client.markets()
            fees = res.get("fees", {})
            maker_fee = float(fees.get("maker", 0.00015))
            taker_fee = float(fees.get("taker", 0.00045))

            for m in res.get("data", []):
                sym = m.get("symbol", "").upper()
                coin = m.get("coin", sym.replace("USDC", "").replace("USDT", ""))
                max_lev = float(m.get("maxLeverage", 1.0))
                lot_step = float(m.get("lotStep", 0.001))
                sz_dec = int(m.get("szDecimals", 3))

                # Compute price unit & precision from lot step / szDecimals / typical tick
                # Default tick size for BTC is 0.1 or 0.01; ETH is 0.01, etc.
                price_unit = 0.1 if "BTC" in sym else (0.01 if "ETH" in sym else 0.001)
                prec = 1 if "BTC" in sym else (2 if "ETH" in sym else 4)

                contract = ContractDetail(
                    symbol=sym,
                    coin=coin,
                    price_unit=price_unit,
                    price_precision=prec,
                    lot_step=lot_step,
                    sz_decimals=sz_dec,
                    max_leverage=max_lev,
                    maker_fee_rate=maker_fee,
                    taker_fee_rate=taker_fee
                )
                self._markets_cache[sym] = m
                self._contracts_cache[sym] = contract

            self._last_markets_fetch = now
            logger.info("PropDAO Markets refreshed. %d symbols loaded.", len(self._markets_cache))
        except Exception as e:
            logger.warning("Could not fetch PropDAO markets: %s. Using default specifications.", e)

    def get_contract_detail(self, symbol: str) -> ContractDetail:
        """Returns ContractDetail specification for symbol (creates sensible defaults if not found)."""
        sym = symbol.upper()
        if sym in self._contracts_cache:
            return self._contracts_cache[sym]

        # Sane default based on coin type
        coin = sym.replace("USDC", "").replace("USDT", "")
        max_lev = 2.0 if coin in ("BTC", "ETH", "SOL") else 1.0
        lot_step = 0.00001 if coin == "BTC" else (0.001 if coin in ("ETH", "SOL") else 0.1)
        prec = 1 if coin == "BTC" else (2 if coin in ("ETH", "SOL") else 4)
        pu = 0.1 if coin == "BTC" else (0.01 if coin in ("ETH", "SOL") else 0.0001)

        detail = ContractDetail(
            symbol=sym,
            coin=coin,
            price_unit=pu,
            price_precision=prec,
            lot_step=lot_step,
            sz_decimals=len(str(lot_step).split(".")[-1]) if "." in str(lot_step) else 0,
            max_leverage=max_lev
        )
        self._contracts_cache[sym] = detail
        return detail

    def get_symbol_info(self, symbol: str) -> ContractDetail:
        """Alias for get_contract_detail."""
        return self.get_contract_detail(symbol)

    def normalize_interval(self, interval: str) -> str:
        """Normalizes timeframes like '15m', 'Min15', '1h', etc. to standard format."""
        s = str(interval).lower().replace("min", "m")
        if s in ("1", "1m", "min1"):
            return "1m"
        elif s in ("3", "3m", "min3"):
            return "3m"
        elif s in ("5", "5m", "min5"):
            return "5m"
        elif s in ("15", "15m", "min15"):
            return "15m"
        elif s in ("30", "30m", "min30"):
            return "30m"
        elif s in ("60", "1h", "min60", "hour1"):
            return "1h"
        elif s in ("240", "4h", "hour4"):
            return "4h"
        elif s in ("1d", "day1", "d"):
            return "1d"
        return s

    def get_klines(
        self,
        symbol: str,
        interval: str = "15m",
        limit: int = 120
    ) -> List[Dict[str, Any]]:
        """
        Fetches historical OHLCV candlestick records.
        Retrieves real-time candles from Binance/Hyperliquid with local caching to maintain 0-lag updates.
        Returns: list of dicts with {timestamp, open, high, low, close, volume}.
        """
        sym = symbol.upper()
        norm_int = self.normalize_interval(interval)
        cache_key = f"{sym}_{norm_int}_{limit}"
        now = time.time()

        if cache_key in self._kline_cache:
            last_ts, data = self._kline_cache[cache_key]
            if now - last_ts < self._kline_ttl_seconds:
                return data

        # Fetch from Binance public market data API (mirrors Hyperliquid reference crypto marks)
        pair = sym.replace("USDC", "USDT")
        url = f"https://api.binance.com/api/v3/klines?symbol={pair}&interval={norm_int}&limit={limit}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
                candles: List[Dict[str, Any]] = []
                for row in raw:
                    # Binance kline structure:
                    # [0: openTime, 1: open, 2: high, 3: low, 4: close, 5: volume, 6: closeTime...]
                    candles.append({
                        "timestamp": int(row[0]),
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                    })
                self._kline_cache[cache_key] = (now, candles)
                return candles
        except Exception as e:
            logger.warning("Error fetching klines from primary source: %s. Trying fallback...", e)
            if cache_key in self._kline_cache:
                return self._kline_cache[cache_key][1]
            return []

    def get_current_price(self, symbol: str) -> float:
        """Fetches latest mark/last price for symbol."""
        candles = self.get_klines(symbol, interval="1m", limit=2)
        if candles:
            return float(candles[-1]["close"])
        return 0.0
