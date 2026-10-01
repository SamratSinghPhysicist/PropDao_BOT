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
        Prioritizes Hyperliquid (native to PropDAO, zero cloud geo-blocking),
        falls back to Bybit and Binance with local caching to maintain 0-lag updates.
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

        coin = sym.replace("USDC", "").replace("USDT", "").replace("-PERP", "").replace("_PERP", "")

        # 1. Primary Source: Hyperliquid Official API (Native PropDAO liquidity layer, never blocks cloud IPs)
        hl_intervals = {
            "1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000,
            "30m": 1800000, "1h": 3600000, "4h": 14400000, "1d": 86400000
        }
        if norm_int in hl_intervals:
            try:
                step_ms = hl_intervals[norm_int]
                now_ms = int(time.time() * 1000)
                start_ms = now_ms - (limit * step_ms)
                payload = json.dumps({
                    "type": "candleSnapshot",
                    "req": {"coin": coin, "interval": norm_int, "startTime": start_ms}
                }).encode("utf-8")
                req = urllib.request.Request(
                    "https://api.hyperliquid.xyz/info",
                    data=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                    if raw and isinstance(raw, list):
                        candles: List[Dict[str, Any]] = []
                        for r in raw:
                            candles.append({
                                "timestamp": int(r["t"]),
                                "open": float(r["o"]),
                                "high": float(r["h"]),
                                "low": float(r["l"]),
                                "close": float(r["c"]),
                                "volume": float(r["v"]),
                            })
                        if candles:
                            self._kline_cache[cache_key] = (now, candles)
                            return candles
            except Exception as e:
                logger.debug("Hyperliquid kline fetch failed for %s: %s. Trying Bybit fallback...", coin, e)

        # 2. Secondary Fallback: Bybit Public Linear API (global cloud friendly)
        bybit_map = {
            "1m": "1", "3m": "3", "5m": "5", "15m": "15",
            "30m": "30", "1h": "60", "4h": "240", "1d": "D"
        }
        bb_int = bybit_map.get(norm_int, "60")
        bb_pair = f"{coin}USDT"
        try:
            url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={bb_pair}&interval={bb_int}&limit={limit}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
                rows = raw.get("result", {}).get("list", [])
                if rows:
                    rows.reverse()
                    candles = []
                    for r in rows:
                        candles.append({
                            "timestamp": int(r[0]),
                            "open": float(r[1]),
                            "high": float(r[2]),
                            "low": float(r[3]),
                            "close": float(r[4]),
                            "volume": float(r[5]),
                        })
                    if candles:
                        self._kline_cache[cache_key] = (now, candles)
                        return candles
        except Exception as e:
            logger.debug("Bybit kline fallback failed for %s: %s. Trying Binance...", bb_pair, e)

        # 3. Tertiary Fallback: Binance Public Market Data API
        pair = sym.replace("USDC", "USDT")
        url = f"https://api.binance.com/api/v3/klines?symbol={pair}&interval={norm_int}&limit={limit}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6.0) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
                candles = []
                for row in raw:
                    candles.append({
                        "timestamp": int(row[0]),
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                    })
                if candles:
                    self._kline_cache[cache_key] = (now, candles)
                    return candles
        except Exception as e:
            logger.warning("All primary/fallback kline feeds failed for %s (%s): %s", sym, norm_int, e)

        if cache_key in self._kline_cache:
            return self._kline_cache[cache_key][1]
        return []

    def get_current_price(self, symbol: str) -> float:
        """Fetches latest mark/last price for symbol."""
        candles = self.get_klines(symbol, interval="1m", limit=2)
        if candles:
            return float(candles[-1]["close"])
        return 0.0
