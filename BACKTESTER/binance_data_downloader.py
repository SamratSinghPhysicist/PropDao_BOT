"""
Binance Public Data Vision Automated Downloader
===============================================
A high-speed, multi-threaded standalone downloader for official Binance Vision
historical market data archives (https://data.binance.vision/).

Supported Data Types:
1. 'klines'     -> OHLCV Candlesticks (1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d)
2. 'trades'     -> High-Resolution Millisecond Tick-by-Tick Trades (id, price, qty, time, isBuyerMaker)
3. 'bookTicker' -> Top-of-Book Best Bid/Ask Quotes (bestBidPrice, bestBidQty, bestAskPrice, bestAskQty, timestamp)

Usage:
  Interactive Mode:
    python binance_data_downloader.py

  CLI Scripting Mode:
    python binance_data_downloader.py --symbol DOGEUSDT --types klines bookTicker trades --timeframes 1m 5m --start 2026-01 --end 2026-08 --unzip
"""

import os
import sys
import time
import zipfile
import argparse
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple

# Ensure utf-8 output encoding on Windows consoles
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

try:
    import requests
except ImportError:
    print("Error: 'requests' library is required. Install via: pip install requests")
    sys.exit(1)

# Base URL for Binance USD-M Perpetual Futures
BASE_URL_FUTURES_UM = "https://data.binance.vision/data/futures/um"
BASE_URL_SPOT = "https://data.binance.vision/data/spot"


def get_available_s3_date_range(market_type: str, data_type: str, symbol: str, timeframe: str = "1m") -> Tuple[Optional[str], Optional[str]]:
    """Discovers exact first and last available monthly archive date from Binance Vision S3 bucket."""
    symbol_clean = symbol.upper().replace("_", "")
    prefix = f"data/{market_type}/monthly/{data_type}/{symbol_clean}/"
    if data_type == "klines":
        prefix = f"data/{market_type}/monthly/klines/{symbol_clean}/{timeframe}/"
    s3_url = f"https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix={prefix}"
    try:
        r = requests.get(s3_url, timeout=6)
        if r.status_code == 200:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(r.text)
            ns = {'s3': 'http://s3.amazonaws.com/doc/2006-03-01/'}
            keys = [elem.text for elem in root.findall('.//s3:Key', ns) if elem.text and elem.text.endswith('.zip')]
            if keys:
                # Extract dates from filenames (e.g. DOGEUSDT-trades-2024-01.zip or DOGEUSDT-1m-2024-01.zip)
                first_fn = keys[0].split('/')[-1].replace('.zip', '')
                last_fn = keys[-1].split('/')[-1].replace('.zip', '')
                first_date = first_fn.split('-')[-2] + '-' + first_fn.split('-')[-1]
                last_date = last_fn.split('-')[-2] + '-' + last_fn.split('-')[-1]
                return first_date, last_date
    except Exception:
        pass
    return None, None


def generate_monthly_date_list(start_ym: str, end_ym: str) -> List[str]:
    """Generates a list of YYYY-MM strings between start and end (inclusive)."""
    start_dt = datetime.datetime.strptime(start_ym.strip(), "%Y-%m")
    end_dt = datetime.datetime.strptime(end_ym.strip(), "%Y-%m")
    
    current = start_dt
    months = []
    while current <= end_dt:
        months.append(current.strftime("%Y-%m"))
        year = current.year + (1 if current.month == 12 else 0)
        month = 1 if current.month == 12 else current.month + 1
        current = current.replace(year=year, month=month, day=1)
    return months


def generate_daily_date_list(start_ymd: str, end_ymd: str) -> List[str]:
    """Generates a list of YYYY-MM-DD strings between start and end (inclusive)."""
    start_dt = datetime.datetime.strptime(start_ymd.strip(), "%Y-%m-%d")
    end_dt = datetime.datetime.strptime(end_ymd.strip(), "%Y-%m-%d")
    
    current = start_dt
    days = []
    while current <= end_dt:
        days.append(current.strftime("%Y-%m-%d"))
        current += datetime.timedelta(days=1)
    return days


class BinanceDataDownloader:
    def __init__(
        self,
        market_type: str = "futures/um",
        output_dir: str = "downloaded_data",
        max_workers: int = 6,
        auto_unzip: bool = False
    ):
        self.market_type = market_type
        self.base_url = BASE_URL_FUTURES_UM if market_type == "futures/um" else BASE_URL_SPOT
        self.output_dir = os.path.abspath(output_dir)
        self.max_workers = max_workers
        self.auto_unzip = auto_unzip
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "BinanceDataDownloader/1.0"})

    def _build_url_and_path(
        self,
        symbol: str,
        data_type: str,
        period: str, # "monthly" or "daily"
        date_str: str,
        timeframe: Optional[str] = None
    ) -> Tuple[str, str]:
        """
        Constructs the exact Binance Vision download URL and local destination path.
        """
        symbol_upper = symbol.upper().replace("_", "")
        
        if data_type == "klines":
            tf = timeframe or "1m"
            filename = f"{symbol_upper}-{tf}-{date_str}.zip"
            url = f"{self.base_url}/{period}/klines/{symbol_upper}/{tf}/{filename}"
            local_folder = os.path.join(self.output_dir, "OHLCV_Data_Binance", symbol_upper, tf)
        elif data_type == "trades":
            filename = f"{symbol_upper}-trades-{date_str}.zip"
            url = f"{self.base_url}/{period}/trades/{symbol_upper}/{filename}"
            local_folder = os.path.join(self.output_dir, "Historical_Trades_Data_Binance", symbol_upper)
        elif data_type == "bookTicker":
            filename = f"{symbol_upper}-bookTicker-{date_str}.zip"
            url = f"{self.base_url}/{period}/bookTicker/{symbol_upper}/{filename}"
            local_folder = os.path.join(self.output_dir, "BookTicker_Data_Binance", symbol_upper)
        else:
            raise ValueError(f"Unsupported data type: {data_type}")

        os.makedirs(local_folder, exist_ok=True)
        local_path = os.path.join(local_folder, filename)
        return url, local_path

    def download_file(self, url: str, dest_path: str) -> Tuple[bool, str]:
        """Downloads a single ZIP file from Binance Vision and extracts if requested."""
        csv_path = dest_path.replace(".zip", ".csv")
        
        # Check if already downloaded and extracted
        if os.path.exists(csv_path) and os.path.getsize(csv_path) > 100:
            return True, f"Already exists: {os.path.basename(csv_path)}"
        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 100 and not self.auto_unzip:
            return True, f"Already exists: {os.path.basename(dest_path)}"

        try:
            res = self.session.get(url, stream=True, timeout=20)
            if res.status_code == 404:
                return False, f"404 Not Found: {url}"
            elif res.status_code != 200:
                return False, f"HTTP {res.status_code}: {url}"

            # Stream download to temporary file
            temp_path = dest_path + ".tmp"
            with open(temp_path, "wb") as f:
                for chunk in res.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)

            # Atomically rename
            if os.path.exists(dest_path):
                os.remove(dest_path)
            os.rename(temp_path, dest_path)

            # Auto unzip if requested
            if self.auto_unzip and os.path.exists(dest_path):
                try:
                    with zipfile.ZipFile(dest_path, "r") as z:
                        z.extractall(os.path.dirname(dest_path))
                    os.remove(dest_path) # Remove zip after clean extraction
                    return True, f"Downloaded & Extracted: {os.path.basename(csv_path)}"
                except Exception as ze:
                    return True, f"Downloaded (Unzip error: {ze}): {os.path.basename(dest_path)}"

            return True, f"Downloaded: {os.path.basename(dest_path)} ({os.path.getsize(dest_path)/1024:.1f} KB)"

        except Exception as e:
            if os.path.exists(dest_path + ".tmp"):
                try:
                    os.remove(dest_path + ".tmp")
                except Exception:
                    pass
            return False, f"Error downloading {url}: {e}"

    def run_download_batch(
        self,
        symbols: List[str],
        data_types: List[str],
        start_date: str,
        end_date: str,
        timeframes: Optional[List[str]] = None,
        frequency: str = "monthly"
    ):
        """Dispatches parallel download tasks for all requested symbols and datasets."""
        tfs = timeframes or ["1m"]
        dates = (
            generate_monthly_date_list(start_date[:7], end_date[:7])
            if frequency == "monthly"
            else generate_daily_date_list(start_date, end_date)
        )

        tasks = []
        for symbol in symbols:
            for dt in data_types:
                if dt == "klines":
                    for tf in tfs:
                        for d_str in dates:
                            url, dest = self._build_url_and_path(symbol, dt, frequency, d_str, tf)
                            tasks.append((url, dest))
                else:
                    for d_str in dates:
                        url, dest = self._build_url_and_path(symbol, dt, frequency, d_str)
                        tasks.append((url, dest))

        total_files = len(tasks)
        print(f"\n🚀 Starting download queue: {total_files} file(s) across {self.max_workers} worker threads...")
        print(f"📁 Destination Directory: {self.output_dir}\n")

        start_time = time.time()
        success_count = 0
        fail_count = 0

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_url = {executor.submit(self.download_file, url, dest): url for url, dest in tasks}
            for i, future in enumerate(as_completed(future_to_url), 1):
                ok, msg = future.result()
                if ok:
                    success_count += 1
                    print(f"[{i:03d}/{total_files:03d}] ✅ {msg}")
                else:
                    fail_count += 1
                    print(f"[{i:03d}/{total_files:03d}] ⚠️  {msg}")

        elapsed = time.time() - start_time
        print(f"\n==============================================================================")
        print(f"🎉 Batch Download Complete in {elapsed:.1f}s!")
        print(f"   • Successfully processed: {success_count} file(s)")
        if fail_count > 0:
            print(f"   • Missing/404 archives:   {fail_count} (Note: Unreleased future dates or new pairs return 404)")
        print(f"==============================================================================\n")


def interactive_wizard():
    print("=" * 78)
    print("        BINANCE PUBLIC DATA VISION AUTOMATED DOWNLOAD WIZARD")
    print("=" * 78)

    # 1. Market Selection
    print("\n1. Select Market Type:")
    print("   [1] USD-M Perpetual Futures (futures/um) [Default - for KCEX bot data]")
    print("   [2] Spot Market (spot)")
    m_choice = input("   Select [default: 1]: ").strip()
    market_type = "spot" if m_choice == "2" else "futures/um"

    # 2. Symbol Selection
    print("\n2. Enter Trading Pair Symbol (e.g., DOGEUSDT, TRUMPUSDT, BTCUSDT):")
    sym_in = input("   Symbol [default: DOGEUSDT]: ").strip() or "DOGEUSDT"
    symbols = [s.strip().upper().replace("_", "") for s in sym_in.split(",") if s.strip()]

    # 3. Data Types
    print("\n3. Select Data Types to Download:")
    print("   [1] ALL: OHLCV Klines + Tick Trades + BookTicker Bid/Ask Quotes [Recommended]")
    print("   [2] Only BookTicker (Best Bid & Ask Quotes - Top of Book Depth)")
    print("   [3] Only Tick Trades (Millisecond Trade Stream)")
    print("   [4] Only OHLCV Klines (Candlesticks)")
    dt_choice = input("   Select [default: 1]: ").strip() or "1"
    if dt_choice == "2":
        data_types = ["bookTicker"]
    elif dt_choice == "3":
        data_types = ["trades"]
    elif dt_choice == "4":
        data_types = ["klines"]
    else:
        data_types = ["klines", "trades", "bookTicker"]

    # 4. Timeframes (if klines included)
    ALL_TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"]
    timeframes = ["1m"]
    if "klines" in data_types:
        print("\n4. Select Candle Timeframes for OHLCV:")
        print("   [1] 1m only [Default - Ultra High Frequency]")
        print("   [2] ALL Timeframes (1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d) [Complete Suite]")
        print("   [3] Standard Multi-TF Suite (1m, 5m, 15m, 1h, 1d)")
        print("   [4] Custom comma-separated (e.g. 1m,5m,15m,1h)")
        tf_choice = input("   Select [default: 1]: ").strip() or "1"
        if tf_choice == "2":
            timeframes = ALL_TIMEFRAMES
        elif tf_choice == "3":
            timeframes = ["1m", "5m", "15m", "1h", "1d"]
        elif tf_choice == "4":
            raw_tf = input("   Enter timeframes: ").strip()
            timeframes = [t.strip() for t in raw_tf.split(",") if t.strip()]
        else:
            timeframes = ["1m"]

    # 5. Date Range Discovery across all selected data types
    print("\n5. Select Date Range (Monthly Archives):")
    primary_symbol = symbols[0]
    discovered_ranges = {}
    for dt in data_types:
        tf_check = timeframes[0] if (dt == "klines" and timeframes) else "1m"
        s_d, e_d = get_available_s3_date_range(market_type, dt, primary_symbol, tf_check)
        if s_d and e_d:
            discovered_ranges[dt] = (s_d, e_d)
            print(f"   ℹ️  Binance Vision available for {primary_symbol} ({dt}): {s_d} to {e_d}")

    # Determine smart default overlap
    if discovered_ranges:
        def_s = max(r[0] for r in discovered_ranges.values())
        def_e = min(r[1] for r in discovered_ranges.values())
    else:
        def_s = "2024-01"
        def_e = "2024-12"

    s_date = input(f"   Start Month (YYYY-MM) [default: {def_s}]: ").strip() or def_s
    e_date = input(f"   End Month   (YYYY-MM) [default: {def_e}]: ").strip() or def_e

    # 6. Destination Folder
    default_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "BACKTESTER"))
    if not os.path.exists(default_dir):
        default_dir = os.path.abspath("./data")
    print(f"\n6. Local Destination Directory:")
    dest_dir = input(f"   Directory [default: {default_dir}]: ").strip() or default_dir

    # 7. Auto Unzip
    unzip_in = input("\n7. Auto-extract CSVs and remove ZIP files? (y/n) [default: y]: ").strip().lower()
    auto_unzip = False if unzip_in in ("n", "no") else True

    # Execute
    downloader = BinanceDataDownloader(
        market_type=market_type,
        output_dir=dest_dir,
        max_workers=6,
        auto_unzip=auto_unzip
    )
    downloader.run_download_batch(
        symbols=symbols,
        data_types=data_types,
        start_date=s_date,
        end_date=e_date,
        timeframes=timeframes,
        frequency="monthly"
    )


def main():
    parser = argparse.ArgumentParser(description="Automated Binance Vision Data Downloader")
    parser.add_argument("--symbol", type=str, help="Trading pair symbol (e.g. DOGEUSDT, TRUMPUSDT, BTCUSDT)")
    parser.add_argument("--market", type=str, default="futures/um", choices=["futures/um", "spot"], help="Market type")
    parser.add_argument("--types", nargs="+", default=["klines", "trades", "bookTicker"], choices=["klines", "trades", "bookTicker"], help="Data types to fetch")
    parser.add_argument("--timeframes", nargs="+", default=["1m"], help="Candlestick intervals (e.g. 1m 5m 15m 1h)")
    parser.add_argument("--start", type=str, default="2026-01", help="Start month (YYYY-MM) or date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2026-08", help="End month (YYYY-MM) or date (YYYY-MM-DD)")
    parser.add_argument("--freq", type=str, default="monthly", choices=["monthly", "daily"], help="Archive frequency")
    parser.add_argument("--output", type=str, default=None, help="Destination directory path")
    parser.add_argument("--workers", type=int, default=6, help="Concurrent download threads")
    parser.add_argument("--unzip", action="store_true", help="Auto-extract ZIP archives to CSV")

    args = parser.parse_args()

    # If no symbol specified, run interactive wizard
    if not args.symbol:
        interactive_wizard()
        return

    output_dir = args.output or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "BACKTESTER"))
    symbols = [s.strip().upper().replace("_", "") for s in args.symbol.split(",") if s.strip()]

    downloader = BinanceDataDownloader(
        market_type=args.market,
        output_dir=output_dir,
        max_workers=args.workers,
        auto_unzip=args.unzip
    )
    downloader.run_download_batch(
        symbols=symbols,
        data_types=args.types,
        start_date=args.start,
        end_date=args.end,
        timeframes=args.timeframes,
        frequency=args.freq
    )


if __name__ == "__main__":
    main()
