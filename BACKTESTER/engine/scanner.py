"""
Dynamic Data Catalog & Range Scanner
=====================================
Scans BACKTESTER/OHLCV_Data_Binance and BACKTESTER/Historical_Trades_Data_Binance.
Automatically discovers available trading pairs, timeframes, and computes
exact historical timestamp ranges using high-speed binary seek (without reading
gigabyte files into memory).
"""

import os
import re
import glob
import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any


def canonicalize_symbol(sym: str) -> str:
    """Normalizes symbol representations (e.g. 'TRUMPUSDT' -> 'TRUMP_USDT')."""
    s = sym.strip().upper().replace("-", "_")
    if "_" not in s and s.endswith("USDT"):
        base = s[:-4]
        return f"{base}_USDT"
    if "_" not in s and s.endswith("USDC"):
        base = s[:-4]
        return f"{base}_USDC"
    return s


def parse_timestamp_ms(val: Any) -> Optional[int]:
    """Parses various timestamp inputs (int, float, ISO string) into epoch milliseconds."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        # If timestamp is in seconds (10 digits), convert to ms
        if val < 1e11:
            return int(val * 1000)
        return int(val)
    if isinstance(val, str):
        v = val.strip()
        if v.isdigit():
            iv = int(v)
            return iv * 1000 if iv < 1e11 else iv
        # Parse ISO date strings e.g. "2026-07-01" or "2026-07-01 12:00:00"
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
            try:
                dt = datetime.datetime.strptime(v, fmt)
                dt = dt.replace(tzinfo=datetime.timezone.utc)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
    return None


def format_ms_to_utc(ms: Optional[int]) -> str:
    """Formats millisecond epoch timestamp to UTC ISO string."""
    if ms is None:
        return "N/A"
    dt = datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_bytes(num_bytes: int) -> str:
    """Formats byte counts into human-readable strings."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:3.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


def read_first_and_last_timestamp(csv_path: str, ts_col_idx: int) -> Tuple[Optional[int], Optional[int]]:
    """
    Reads the earliest timestamp (line 2) and latest timestamp (last line via binary seek)
    from a CSV file in milliseconds.
    """
    if not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0:
        return None, None

    first_ts = None
    last_ts = None

    # 1. Read first data line
    try:
        with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
            f.readline() # header
            first_line = f.readline()
            if first_line:
                parts = first_line.strip().split(",")
                if len(parts) > ts_col_idx:
                    first_ts = int(float(parts[ts_col_idx]))
    except Exception:
        pass

    # 2. Seek near the end of file for the last line
    try:
        file_size = os.path.getsize(csv_path)
        with open(csv_path, "rb") as f:
            # Read up to 2048 bytes from end
            read_size = min(2048, file_size)
            f.seek(file_size - read_size)
            chunk = f.read(read_size).decode("utf-8", errors="ignore")
            lines = [l.strip() for l in chunk.split("\n") if l.strip()]
            if lines:
                last_line = lines[-1]
                parts = last_line.split(",")
                if len(parts) > ts_col_idx:
                    last_ts = int(float(parts[ts_col_idx]))
    except Exception:
        pass

    return first_ts, last_ts


@dataclass
class TimeframeDataInfo:
    timeframe: str
    files: List[str] = field(default_factory=list)
    start_time_ms: Optional[int] = None
    end_time_ms: Optional[int] = None
    total_bytes: int = 0
    total_files: int = 0


@dataclass
class SymbolDataCatalog:
    symbol: str
    ohlcv_timeframes: Dict[str, TimeframeDataInfo] = field(default_factory=dict)
    trade_files: List[str] = field(default_factory=list)
    trades_start_time_ms: Optional[int] = None
    trades_end_time_ms: Optional[int] = None
    trades_total_bytes: int = 0
    trades_file_count: int = 0

    @property
    def has_trades(self) -> bool:
        return bool(self.trade_files and self.trades_start_time_ms and self.trades_end_time_ms)

    @property
    def has_ohlcv(self) -> bool:
        return bool(self.ohlcv_timeframes)

    def _normalize_tf(self, tf: str) -> str:
        s = tf.strip().lower()
        mapping = {
            "min1": "1m", "min3": "3m", "min5": "5m", "min15": "15m", "min30": "30m",
            "min60": "1h", "hour1": "1h", "hour2": "2h", "hour4": "4h", "hour6": "6h",
            "hour8": "8h", "hour12": "12h", "day1": "1d", "d1": "1d"
        }
        return mapping.get(s, s)

    def get_timeframe_range(self, tf: str) -> Tuple[Optional[int], Optional[int]]:
        norm_tf = self._normalize_tf(tf)
        info = self.ohlcv_timeframes.get(norm_tf) or self.ohlcv_timeframes.get(tf)
        if info:
            return info.start_time_ms, info.end_time_ms
        return None, None

    def get_overall_ohlcv_range(self) -> Tuple[Optional[int], Optional[int]]:
        starts = [t.start_time_ms for t in self.ohlcv_timeframes.values() if t.start_time_ms]
        ends = [t.end_time_ms for t in self.ohlcv_timeframes.values() if t.end_time_ms]
        if not starts or not ends:
            return None, None
        return min(starts), max(ends)

    def get_overlap_range(self, tf: Optional[str] = None) -> Tuple[Optional[int], Optional[int]]:
        """Returns the common range where both OHLCV and Trades are available."""
        if tf:
            norm_tf = self._normalize_tf(tf)
            if norm_tf in self.ohlcv_timeframes:
                o_start, o_end = self.ohlcv_timeframes[norm_tf].start_time_ms, self.ohlcv_timeframes[norm_tf].end_time_ms
            elif tf in self.ohlcv_timeframes:
                o_start, o_end = self.ohlcv_timeframes[tf].start_time_ms, self.ohlcv_timeframes[tf].end_time_ms
            else:
                o_start, o_end = self.get_overall_ohlcv_range()
        else:
            o_start, o_end = self.get_overall_ohlcv_range()

        t_start, t_end = self.trades_start_time_ms, self.trades_end_time_ms

        if not (o_start and o_end and t_start and t_end):
            return None, None

        start = max(o_start, t_start)
        end = min(o_end, t_end)
        if start <= end:
            return start, end
        return None, None


BACKTESTER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT_DIR = os.path.abspath(os.path.join(BACKTESTER_DIR, ".."))


class DataScanner:
    """
    Scans and catalogues all historical OHLCV and Trade data files.
    """

    def __init__(
        self,
        ohlcv_dir: Optional[str] = None,
        trades_dir: Optional[str] = None
    ):
        if ohlcv_dir is None:
            user_ohlcv_def = r"D:\My_Bots\Trading\BINANCE_DATA\OHLCV_Data\binance_futures_ohlcv"
            kcex_ohlcv_def = r"D:\My_Bots\Trading\KCEX\BACKTESTER\OHLCV_Data_Binance"
            if os.path.isdir(user_ohlcv_def):
                ohlcv_dir = user_ohlcv_def
            elif os.path.isdir(kcex_ohlcv_def):
                ohlcv_dir = kcex_ohlcv_def
            else:
                ohlcv_dir = os.path.join(BACKTESTER_DIR, "OHLCV_Data_Binance")
        elif not os.path.isabs(ohlcv_dir) and not os.path.isdir(ohlcv_dir):
            candidate = os.path.join(BACKTESTER_DIR, os.path.basename(ohlcv_dir))
            if os.path.isdir(candidate):
                ohlcv_dir = candidate
            else:
                ohlcv_dir = os.path.join(ROOT_DIR, ohlcv_dir)

        if trades_dir is None:
            user_trades_def = r"D:\My_Bots\Trading\BINANCE_DATA\Tick_Trades_Data\binance_futures_trades"
            kcex_trades_def = r"D:\My_Bots\Trading\KCEX\BACKTESTER\Historical_Trades_Data_Binance"
            if os.path.isdir(user_trades_def):
                trades_dir = user_trades_def
            elif os.path.isdir(kcex_trades_def):
                trades_dir = kcex_trades_def
            else:
                trades_dir = os.path.join(BACKTESTER_DIR, "Historical_Trades_Data_Binance")
        elif not os.path.isabs(trades_dir) and not os.path.isdir(trades_dir):
            candidate = os.path.join(BACKTESTER_DIR, os.path.basename(trades_dir))
            if os.path.isdir(candidate):
                trades_dir = candidate
            else:
                trades_dir = os.path.join(ROOT_DIR, trades_dir)

        self.ohlcv_dir = ohlcv_dir
        self.trades_dir = trades_dir
        self._catalog: Dict[str, SymbolDataCatalog] = {}

    def scan(self, force_refresh: bool = False) -> Dict[str, SymbolDataCatalog]:
        """Performs a full scan across both OHLCV and Trades directories."""
        if self._catalog and not force_refresh:
            return self._catalog

        catalog: Dict[str, SymbolDataCatalog] = {}

        # 1. Scan OHLCV Directory: OHLCV_Data_Binance/<SYMBOL_FOLDER>/<TIMEFRAME_FOLDER>/*.csv
        if os.path.isdir(self.ohlcv_dir):
            for sym_entry in os.listdir(self.ohlcv_dir):
                sym_path = os.path.join(self.ohlcv_dir, sym_entry)
                if not os.path.isdir(sym_path):
                    continue

                canonical_sym = canonicalize_symbol(sym_entry)
                if canonical_sym not in catalog:
                    catalog[canonical_sym] = SymbolDataCatalog(symbol=canonical_sym)

                # Scan timeframes in this symbol directory
                for tf_entry in os.listdir(sym_path):
                    tf_path = os.path.join(sym_path, tf_entry)
                    if not os.path.isdir(tf_path):
                        continue

                    tf_name = tf_entry.lower()
                    csv_files = sorted(glob.glob(os.path.join(tf_path, "*.csv")))
                    if not csv_files:
                        continue

                    tf_info = TimeframeDataInfo(
                        timeframe=tf_name,
                        files=csv_files,
                        total_files=len(csv_files),
                        total_bytes=sum(os.path.getsize(f) for f in csv_files)
                    )

                    # Extract start timestamp from earliest file and end from latest file
                    # In OHLCV CSVs: column 0 is open_time
                    s_ts, _ = read_first_and_last_timestamp(csv_files[0], ts_col_idx=0)
                    _, e_ts = read_first_and_last_timestamp(csv_files[-1], ts_col_idx=0)
                    tf_info.start_time_ms = s_ts
                    tf_info.end_time_ms = e_ts

                    catalog[canonical_sym].ohlcv_timeframes[tf_name] = tf_info

        # 2. Scan Trades Directory: Historical_Trades_Data_Binance/<SYMBOL_FOLDER>/*.csv
        if os.path.isdir(self.trades_dir):
            for sym_entry in os.listdir(self.trades_dir):
                sym_path = os.path.join(self.trades_dir, sym_entry)
                if not os.path.isdir(sym_path):
                    continue

                canonical_sym = canonicalize_symbol(sym_entry)
                if canonical_sym not in catalog:
                    catalog[canonical_sym] = SymbolDataCatalog(symbol=canonical_sym)

                csv_files = sorted(glob.glob(os.path.join(sym_path, "*.csv")))
                if not csv_files:
                    continue

                catalog[canonical_sym].trade_files = csv_files
                catalog[canonical_sym].trades_file_count = len(csv_files)
                catalog[canonical_sym].trades_total_bytes = sum(os.path.getsize(f) for f in csv_files)

                # In Trades CSVs: column 4 is time
                s_ts, _ = read_first_and_last_timestamp(csv_files[0], ts_col_idx=4)
                _, e_ts = read_first_and_last_timestamp(csv_files[-1], ts_col_idx=4)
                catalog[canonical_sym].trades_start_time_ms = s_ts
                catalog[canonical_sym].trades_end_time_ms = e_ts

        self._catalog = catalog
        return self._catalog

    def get_symbol_catalog(self, symbol: str) -> Optional[SymbolDataCatalog]:
        """Retrieves catalog for a specific symbol."""
        cat = self.scan()
        can = canonicalize_symbol(symbol)
        return cat.get(can)

    def print_summary_table(self) -> None:
        """Prints a human-readable table summarizing all discovered data."""
        catalog = self.scan()
        if not catalog:
            print("No market data found in OHLCV or Historical Trades folders.")
            return

        sep = "=" * 92
        print("\n" + sep)
        print("          BACKTESTER HISTORICAL DATA CATALOG & TIMEFRAME DISCOVERY")
        print(sep)
        print(f"{'Symbol':<14} | {'Type':<8} | {'Details / Timeframes':<24} | {'Date Range (UTC)':<38}")
        print("-" * 92)

        for sym, item in catalog.items():
            # OHLCV details
            if item.ohlcv_timeframes:
                tfs = ", ".join(sorted(item.ohlcv_timeframes.keys()))
                o_start, o_end = item.get_overall_ohlcv_range()
                o_range_str = f"{format_ms_to_utc(o_start)[:10]} -> {format_ms_to_utc(o_end)[:10]}"
                total_ohlcv_size = sum(t.total_bytes for t in item.ohlcv_timeframes.values())
                print(f"{sym:<14} | {'OHLCV':<8} | {f'{len(item.ohlcv_timeframes)} TFs ({format_bytes(total_ohlcv_size)})':<24} | {o_range_str:<38}")
                for tf, tinfo in sorted(item.ohlcv_timeframes.items()):
                    tr = f"{format_ms_to_utc(tinfo.start_time_ms)[:10]} -> {format_ms_to_utc(tinfo.end_time_ms)[:10]}"
                    print(f"{'':<14} | {' ':>8} | {f'  +-- {tf} ({tinfo.total_files} files, {format_bytes(tinfo.total_bytes)})':<24} | {tr:<38}")

            # Trades details
            if item.trade_files:
                t_range_str = f"{format_ms_to_utc(item.trades_start_time_ms)[:10]} -> {format_ms_to_utc(item.trades_end_time_ms)[:10]}"
                desc = f"{item.trades_file_count} files ({format_bytes(item.trades_total_bytes)})"
                print(f"{sym:<14} | {'TRADES':<8} | {desc:<24} | {t_range_str:<38}")

            # Common Overlap
            ov_s, ov_e = item.get_overlap_range()
            if ov_s and ov_e:
                ov_str = f"[*] High-Fidelity Overlap: {format_ms_to_utc(ov_s)} -> {format_ms_to_utc(ov_e)}"
                print(f"{'':<14} | {'OVERLAP':<8} | {ov_str}")
            print("-" * 92)
        print(sep + "\n")
