"""
Automated Historical Market Data Downloader
==========================================
Automatically downloads and extracts Binance Vision monthly archive datasets
(OHLCV klines and tick-by-tick trades) into the expected BACKTESTER folder structure.
Ensures seamless execution on fresh GitHub Actions runners and local machines.
"""

import os
import sys
import re
import io
import zipfile
import urllib.request
import datetime
import argparse
from typing import List, Tuple, Optional

# Ensure project root is in path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

def canonicalize_symbol(sym: str) -> str:
    """Normalizes symbol representations (e.g. 'TRUMPUSDT' -> 'TRUMP_USDT')."""
    s = sym.strip().upper().replace("-", "_")
    if "_" not in s and s.endswith("USDT"):
        base = s[:-4]
        return f"{base}_USDT"
    return s


def normalize_timeframe(tf: str) -> str:
    """Normalizes timeframes like '15m', '1h', '1d'."""
    s = tf.strip().lower()
    m = re.search(r"(\d+)\s*([a-zA-Z]+)", s)
    if m:
        num, unit = m.group(1), m.group(2)
        if unit.startswith("m") and not unit.startswith("mo"):
            return f"{num}m"
        elif unit.startswith("h"):
            return f"{num}h"
        elif unit.startswith("d"):
            return f"{num}d"
    return s

BINANCE_VISION_BASE = "https://data.binance.vision/data/futures/um/monthly"

BINANCE_SYMBOL_MAP = {
    "MOG_USDT": "1000000MOGUSDT",
    "MOGUSDT": "1000000MOGUSDT",
    "1000000MOG_USDT": "1000000MOGUSDT",
    "1000000MOGUSDT": "1000000MOGUSDT",
    "XAU_USDT": "XAUUSDT",
    "XAUUSDT": "XAUUSDT",
    "XAG_USDT": "XAGUSDT",
    "XAGUSDT": "XAGUSDT",
    "CL_USDT": "CLUSDT",
    "CLUSDT": "CLUSDT",
    "BTC_USDT": "BTCUSDT",
    "BTCUSDT": "BTCUSDT",
    "BTC_USDC": "BTCUSDT",
    "BTCUSDC": "BTCUSDT",
    "ETH_USDT": "ETHUSDT",
    "ETHUSDT": "ETHUSDT",
    "ETH_USDC": "ETHUSDT",
    "ETHUSDC": "ETHUSDT",
    "SOL_USDT": "SOLUSDT",
    "SOLUSDT": "SOLUSDT",
    "SOL_USDC": "SOLUSDT",
    "SOLUSDC": "SOLUSDT",
    "DOGE_USDT": "DOGEUSDT",
    "DOGEUSDT": "DOGEUSDT",
    "TRUMP_USDT": "TRUMPUSDT",
    "TRUMPUSDT": "TRUMPUSDT",
}


def generate_month_list(start_date_str: str, end_date_str: str) -> List[Tuple[int, int]]:
    """Generates a list of (year, month) tuples between two dates."""
    s_dt = datetime.datetime.strptime(start_date_str[:10], "%Y-%m-%d")
    e_dt = datetime.datetime.strptime(end_date_str[:10], "%Y-%m-%d")

    months = []
    curr = datetime.datetime(s_dt.year, s_dt.month, 1)
    end = datetime.datetime(e_dt.year, e_dt.month, 1)

    while curr <= end:
        months.append((curr.year, curr.month))
        # Advance by 1 month
        next_m = curr.month + 1 if curr.month < 12 else 1
        next_y = curr.year if curr.month < 12 else curr.year + 1
        curr = datetime.datetime(next_y, next_m, 1)

    return months


def download_and_extract_zip(url: str, extract_to: str, expected_csv_name: str) -> bool:
    """Downloads a zip archive in memory and extracts it into extract_to directory."""
    os.makedirs(extract_to, exist_ok=True)
    target_csv = os.path.join(extract_to, expected_csv_name)
    if os.path.exists(target_csv) and os.path.getsize(target_csv) > 0:
        print(f"[+] File already exists, skipping: {target_csv}")
        return True

    print(f"[*] Downloading {url} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                print(f"[!] Failed to download {url}: HTTP {resp.status}")
                return False
            zip_bytes = resp.read()

        print(f"[*] Extracting to {extract_to} ...")
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(extract_to)

        if os.path.exists(target_csv):
            print(f"[+] Successfully extracted {target_csv} ({os.path.getsize(target_csv)/1024/1024:.2f} MB)")
            return True
        else:
            # Check extracted files
            extracted = os.listdir(extract_to)
            print(f"[+] Extracted archive contents: {extracted}")
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"[!] Archive not found on Binance Vision (404): {url}")
        else:
            print(f"[!] HTTP error {e.code} downloading {url}")
        return False
    except Exception as e:
        print(f"[!] Error downloading {url}: {e}")
        return False


BACKTESTER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def ensure_market_data(
    symbol: str,
    timeframe: str = "1m",
    start_date: str = "2026-01-01",
    end_date: str = "2026-08-31",
    download_trades: bool = True,
    base_dir: Optional[str] = None
) -> bool:
    """
    Ensures that OHLCV and (optionally) Trades data exist for the requested
    symbol, timeframe, and date range. If missing, downloads from Binance Vision.
    """
    if base_dir is None or base_dir == "BACKTESTER":
        base_dir = BACKTESTER_DIR
    elif not os.path.isabs(base_dir) and not os.path.exists(base_dir):
        candidate = os.path.join(BACKTESTER_DIR, os.path.basename(base_dir))
        if os.path.exists(candidate):
            base_dir = candidate
        else:
            base_dir = os.path.join(ROOT_DIR, base_dir)

    canonical = canonicalize_symbol(symbol)
    sym_clean = canonical.replace("_", "")
    binance_sym = BINANCE_SYMBOL_MAP.get(canonical, BINANCE_SYMBOL_MAP.get(sym_clean, sym_clean))
    norm_tf = normalize_timeframe(timeframe)

    months = generate_month_list(start_date, end_date)
    print(f"[*] Verifying historical market data for {canonical} (Binance: {binance_sym}, {norm_tf}) across {len(months)} month(s)...")

    ohlcv_dest_dir = os.path.join(base_dir, "OHLCV_Data_Binance", binance_sym, norm_tf)
    trades_dest_dir = os.path.join(base_dir, "Historical_Trades_Data_Binance", binance_sym)

    all_ok = True

    for y, m in months:
        ym_str = f"{y:04d}-{m:02d}"

        # 1. Check / Download Primary OHLCV
        expected_kline_csv = f"{binance_sym}-{norm_tf}-{ym_str}.csv"
        kline_path = os.path.join(ohlcv_dest_dir, expected_kline_csv)
        if not os.path.exists(kline_path):
            kline_url = f"{BINANCE_VISION_BASE}/klines/{binance_sym}/{norm_tf}/{binance_sym}-{norm_tf}-{ym_str}.zip"
            ok = download_and_extract_zip(kline_url, ohlcv_dest_dir, expected_kline_csv)
            if not ok:
                all_ok = False
        else:
            print(f"[+] OHLCV exists: {expected_kline_csv}")

        # 1b. If primary timeframe != "1m", also ensure 1m OHLCV exists for sub-candle disambiguation
        if norm_tf != "1m":
            ohlcv_1m_dir = os.path.join(base_dir, "OHLCV_Data_Binance", binance_sym, "1m")
            expected_1m_csv = f"{binance_sym}-1m-{ym_str}.csv"
            kline_1m_path = os.path.join(ohlcv_1m_dir, expected_1m_csv)
            if not os.path.exists(kline_1m_path):
                kline_1m_url = f"{BINANCE_VISION_BASE}/klines/{binance_sym}/1m/{binance_sym}-1m-{ym_str}.zip"
                download_and_extract_zip(kline_1m_url, ohlcv_1m_dir, expected_1m_csv)

        # 2. Check / Download Trades (if requested)
        if download_trades:
            expected_trades_csv = f"{binance_sym}-trades-{ym_str}.csv"
            trades_path = os.path.join(trades_dest_dir, expected_trades_csv)
            if not os.path.exists(trades_path):
                trades_url = f"{BINANCE_VISION_BASE}/trades/{binance_sym}/{binance_sym}-trades-{ym_str}.zip"
                ok = download_and_extract_zip(trades_url, trades_dest_dir, expected_trades_csv)
                if not ok:
                    all_ok = False
            else:
                print(f"[+] Trades exist: {expected_trades_csv}")

    return all_ok


def main():
    parser = argparse.ArgumentParser(description="Download Binance Vision historical data for backtesting")
    parser.add_argument("--symbol", type=str, default="TRUMP_USDT", help="Trading pair symbol")
    parser.add_argument("--timeframe", type=str, default="1m", help="Candle timeframe")
    parser.add_argument("--start", type=str, default="2026-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2026-08-31", help="End date (YYYY-MM-DD)")
    parser.add_argument("--ticks", dest="use_ticks", action="store_true", default=False, help="Download tick trades (WARNING: ~5-10 GB per month per symbol)")
    parser.add_argument("--no-ticks", dest="use_ticks", action="store_false", help="Skip downloading tick trades (default: OHLCV only)")
    parser.add_argument("--base-dir", type=str, default="BACKTESTER", help="Base BACKTESTER directory")

    args = parser.parse_args()
    success = ensure_market_data(
        symbol=args.symbol,
        timeframe=args.timeframe,
        start_date=args.start,
        end_date=args.end,
        download_trades=args.use_ticks,
        base_dir=args.base_dir
    )
    if success:
        print("\n[+] Market data verification/download completed successfully.")
    else:
        print("\n[!] Notice: Some historical files could not be downloaded. Check output above.")


if __name__ == "__main__":
    main()
