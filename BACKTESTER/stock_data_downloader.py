"""
Stock Market Historical OHLCV Data Downloader & Resampler
=========================================================
Fetches high-quality historical candlestick data for US equities
from Yahoo Finance with automatic timeframe resolution, pagination,
resampling, and local disk caching.

Supported Timeframes:
- 1d:  Up to 15 years historical daily bars.
- 1h:  Up to 730 days (2 years) 60m bars.
- 4h:  Synthesized by resampling 1h bars into 4-hour session blocks.
- 30m: Up to 60 days 30m bars.
- 15m: Up to 60 days 15m bars.
- 5m:  Up to 60 days 5m bars.
- 1m:  Up to 30 days 1m bars (paginated in 7-day chunks).
- 3m:  Synthesized by resampling 1m bars into 3-minute blocks.
"""

from __future__ import annotations
import os
import sys
import csv
import json
import time
import math
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple

# Add workspace root to sys.path
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from BACKTESTER.engine.data_loader import Candle


# Mapping from KCEX crypto equivalent pair to Yahoo Finance ticker
STOCK_SYMBOL_MAPPING: Dict[str, str] = {
    "BRK_USDT": "BRK-B",
    "BRKB_USDT": "BRK-B",
    "BRK.B": "BRK-B",
    "BF_USDT": "BF-B",
}

# Standard Top 50 US Equities by Market Capitalization
TOP_50_US_STOCKS: List[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "META", "TSLA", "BRK-B", "AVGO", "LLY",
    "JPM", "WMT", "V", "XOM", "UNH",
    "MA", "PG", "COST", "JNJ", "HD",
    "ORCL", "BAC", "ABBV", "NFLX", "CRM",
    "CVX", "MRK", "KO", "AMD", "PEP",
    "LIN", "TMO", "ADBE", "WFC", "QCOM",
    "DIS", "CSCO", "TXN", "GE", "PM",
    "IBM", "AMAT", "ISRG", "INTC", "CAT",
    "NOW", "VZ", "BKNG", "GS", "PLTR"
]


def resolve_stock_ticker(symbol: str) -> str:
    """
    Normalizes a symbol string (e.g. 'AAPL_USDT', 'AAPL', 'BRK_USDT')
    to the standard Yahoo Finance equity ticker (e.g. 'AAPL', 'BRK-B').
    """
    sym = symbol.strip().upper()
    if sym in STOCK_SYMBOL_MAPPING:
        return STOCK_SYMBOL_MAPPING[sym]
    if sym.endswith("_USDT"):
        base = sym[:-5]
        return STOCK_SYMBOL_MAPPING.get(sym, STOCK_SYMBOL_MAPPING.get(base, base))
    return sym


def fetch_yahoo_chart_raw(
    ticker: str,
    interval: str,
    period1: Optional[int] = None,
    period2: Optional[int] = None,
    range_str: Optional[str] = None,
    max_retries: int = 4
) -> List[Dict[str, Any]]:
    """
    Queries Yahoo Finance Chart API v8 endpoint with retries and exponential backoff.
    Returns list of dicts with keys: timestamp_s, open, high, low, close, volume.
    """
    if range_str:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&range={range_str}"
    elif period1 and period2:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&period1={period1}&period2={period2}"
    else:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&range=1mo"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }

    req = urllib.request.Request(url, headers=headers)
    last_err = None

    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                if resp.status == 200:
                    payload = json.loads(resp.read().decode("utf-8"))
                    chart = payload.get("chart", {})
                    results = chart.get("result")
                    if not results:
                        err = chart.get("error")
                        return []
                    data = results[0]
                    timestamps = data.get("timestamp", [])
                    indicators = data.get("indicators", {})
                    quote = indicators.get("quote", [{}])[0]
                    opens = quote.get("open", [])
                    highs = quote.get("high", [])
                    lows = quote.get("low", [])
                    closes = quote.get("close", [])
                    volumes = quote.get("volume", [])

                    candles_raw: List[Dict[str, Any]] = []
                    for i in range(len(timestamps)):
                        t = timestamps[i]
                        o = opens[i] if i < len(opens) else None
                        h = highs[i] if i < len(highs) else None
                        l = lows[i] if i < len(lows) else None
                        c = closes[i] if i < len(closes) else None
                        v = volumes[i] if i < len(volumes) else 0

                        # Filter out missing/null data bars
                        if None in (t, o, h, l, c):
                            continue
                        if o <= 0 or h <= 0 or l <= 0 or c <= 0:
                            continue

                        candles_raw.append({
                            "timestamp_s": int(t),
                            "open": float(o),
                            "high": float(h),
                            "low": float(l),
                            "close": float(c),
                            "volume": float(v) if v is not None else 0.0
                        })
                    return candles_raw
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 422 or e.code == 400:
                # Requested range out of bounds for interval
                return []
            sleep_sec = attempt * 2.0
            time.sleep(sleep_sec)
        except Exception as e:
            last_err = e
            time.sleep(1.0)

    return []


def resample_raw_candles(
    candles_raw: List[Dict[str, Any]],
    factor: int
) -> List[Dict[str, Any]]:
    """
    Resamples a sorted list of raw candle dicts by aggregating every `factor` consecutive bars.
    e.g. factor=3 converts 1m -> 3m. factor=4 converts 1h -> 4h.
    """
    if not candles_raw or factor <= 1:
        return candles_raw

    resampled: List[Dict[str, Any]] = []
    n = len(candles_raw)
    i = 0
    while i < n:
        chunk = candles_raw[i:i + factor]
        if not chunk:
            break
        agg_open = chunk[0]["open"]
        agg_high = max(c["high"] for c in chunk)
        agg_low = min(c["low"] for c in chunk)
        agg_close = chunk[-1]["close"]
        agg_vol = sum(c["volume"] for c in chunk)
        agg_ts = chunk[0]["timestamp_s"]

        resampled.append({
            "timestamp_s": agg_ts,
            "open": agg_open,
            "high": agg_high,
            "low": agg_low,
            "close": agg_close,
            "volume": agg_vol
        })
        i += factor

    return resampled


def fetch_stock_candles(
    ticker: str,
    timeframe: str,
    lookback_years: float = 15.0
) -> List[Candle]:
    """
    Retrieves full available historical OHLCV data for an equity ticker
    matching the timeframe specifications.
    
    Data Availability:
    - 1d:  Up to lookback_years (e.g. 15 years)
    - 1h:  Up to 730 days (2 years)
    - 4h:  Resampled from 1h (up to 730 days)
    - 30m: Up to 60 days
    - 15m: Up to 60 days
    - 5m:  Up to 60 days
    - 1m:  Up to 30 days (in 7-day paginated chunks)
    - 3m:  Resampled from 1m (up to 30 days)
    """
    tf = timeframe.lower().strip()
    norm_ticker = resolve_stock_ticker(ticker)

    raw_bars: List[Dict[str, Any]] = []

    if tf in ("1d", "day1", "daily", "d"):
        # Fetch 15 years daily data
        raw_bars = fetch_yahoo_chart_raw(norm_ticker, interval="1d", range_str=f"{int(lookback_years)}y")
        if not raw_bars:
            raw_bars = fetch_yahoo_chart_raw(norm_ticker, interval="1d", range_str="10y")
        interval_ms = 86400 * 1000

    elif tf in ("1h", "60m", "min60", "hour1"):
        # Fetch up to 730 days 1-hour data
        raw_bars = fetch_yahoo_chart_raw(norm_ticker, interval="60m", range_str="730d")
        interval_ms = 3600 * 1000

    elif tf in ("4h", "hour4", "240m"):
        # Resample from 1h (60m)
        one_h_bars = fetch_yahoo_chart_raw(norm_ticker, interval="60m", range_str="730d")
        raw_bars = resample_raw_candles(one_h_bars, factor=4)
        interval_ms = 4 * 3600 * 1000

    elif tf in ("30m", "min30"):
        raw_bars = fetch_yahoo_chart_raw(norm_ticker, interval="30m", range_str="60d")
        interval_ms = 30 * 60 * 1000

    elif tf in ("15m", "min15"):
        raw_bars = fetch_yahoo_chart_raw(norm_ticker, interval="15m", range_str="60d")
        interval_ms = 15 * 60 * 1000

    elif tf in ("5m", "min5"):
        raw_bars = fetch_yahoo_chart_raw(norm_ticker, interval="5m", range_str="60d")
        interval_ms = 5 * 60 * 1000

    elif tf in ("1m", "min1"):
        # Paginates in 7-day chunks up to 30 days
        now_ts = int(time.time())
        all_1m: List[Dict[str, Any]] = []
        for days_back in [0, 7, 14, 21]:
            p2 = now_ts - (days_back * 86400)
            p1 = max(0, p2 - (7 * 86400))
            chunk = fetch_yahoo_chart_raw(norm_ticker, interval="1m", period1=p1, period2=p2)
            if chunk:
                all_1m.extend(chunk)
            time.sleep(0.3)
        # Deduplicate and sort by timestamp
        seen = set()
        deduped = []
        for bar in sorted(all_1m, key=lambda b: b["timestamp_s"]):
            ts = bar["timestamp_s"]
            if ts not in seen:
                seen.add(ts)
                deduped.append(bar)
        raw_bars = deduped
        interval_ms = 60 * 1000

    elif tf in ("3m", "min3"):
        # Resample from 1m
        now_ts = int(time.time())
        all_1m = []
        for days_back in [0, 7, 14, 21]:
            p2 = now_ts - (days_back * 86400)
            p1 = max(0, p2 - (7 * 86400))
            chunk = fetch_yahoo_chart_raw(norm_ticker, interval="1m", period1=p1, period2=p2)
            if chunk:
                all_1m.extend(chunk)
            time.sleep(0.3)
        seen = set()
        deduped = []
        for bar in sorted(all_1m, key=lambda b: b["timestamp_s"]):
            ts = bar["timestamp_s"]
            if ts not in seen:
                seen.add(ts)
                deduped.append(bar)
        raw_bars = resample_raw_candles(deduped, factor=3)
        interval_ms = 3 * 60 * 1000

    else:
        raise ValueError(f"Unsupported stock timeframe: {timeframe}")

    if not raw_bars:
        return []

    # Convert to Candle objects
    candles: List[Candle] = []
    for r in sorted(raw_bars, key=lambda x: x["timestamp_s"]):
        o_ms = r["timestamp_s"] * 1000
        c_ms = o_ms + interval_ms
        c_vol = r["volume"]
        c_close = r["close"]
        candles.append(Candle(
            open_time_ms=o_ms,
            open=r["open"],
            high=r["high"],
            low=r["low"],
            close=c_close,
            volume=c_vol,
            close_time_ms=c_ms,
            quote_volume=c_vol * c_close,
            trades_count=0
        ))

    return candles


def get_cached_or_download_stock_candles(
    ticker: str,
    timeframe: str,
    data_dir: str = "BACKTESTER/OHLCV_Data_Stocks",
    lookback_years: float = 15.0,
    force_refresh: bool = False
) -> List[Candle]:
    """
    Loads candles from local cache if present, otherwise downloads from Yahoo Finance
    and saves to disk cache.
    """
    norm_ticker = resolve_stock_ticker(ticker)
    tf = timeframe.lower().strip()
    stock_dir = os.path.join(data_dir, norm_ticker, tf)
    cache_file = os.path.join(stock_dir, f"{norm_ticker}_{tf}.csv")

    if not force_refresh and os.path.isfile(cache_file):
        candles: List[Candle] = []
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    candles.append(Candle(
                        open_time_ms=int(row["open_time"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        close_time_ms=int(row["close_time"]),
                        quote_volume=float(row.get("quote_volume", 0.0)),
                        trades_count=int(row.get("count", 0))
                    ))
            if len(candles) >= 30:
                print(f"[+] Loaded {len(candles)} cached bars for {norm_ticker} [{tf}] from {cache_file}")
                return candles
        except Exception as e:
            print(f"[!] Cache read error for {cache_file}: {e}. Re-downloading...")

    print(f"[*] Downloading {norm_ticker} [{tf}] historical data from Yahoo Finance...")
    candles = fetch_stock_candles(norm_ticker, tf, lookback_years=lookback_years)

    if candles:
        os.makedirs(stock_dir, exist_ok=True)
        try:
            with open(cache_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "count"])
                for c in candles:
                    writer.writerow([
                        c.open_time_ms, c.open, c.high, c.low, c.close,
                        c.volume, c.close_time_ms, c.quote_volume, c.trades_count
                    ])
            print(f"[+] Saved {len(candles)} bars to cache: {cache_file}")
        except Exception as e:
            print(f"[!] Warning: Could not write cache file {cache_file}: {e}")

    return candles
