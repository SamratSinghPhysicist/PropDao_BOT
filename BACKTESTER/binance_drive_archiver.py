#!/usr/bin/env python3
"""
Binance Vision to Google Drive Archiver (Unzipped CSV Extractor)
==============================================================
Streams official Binance Vision historical USD-M futures market data,
extracts uncompressed CSV files from downloaded ZIP archives, and uploads
the extracted CSV files directly to Google Drive via Rclone.

Key Features:
- Uncompressed CSV Archival: Extracts and stores clean .csv files (not .zip) in Google Drive.
- Zero Persistent Local Disk Usage: Downloads each .zip to /tmp, unzips the .csv, deletes
  the .zip immediately, uploads the .csv to Google Drive, and deletes the .csv immediately.
- Idempotent & Resumable: Scans Google Drive directory to skip already-extracted CSVs.
- Detailed Timestamped Logging: Full real-time metrics with timestamps, compression ratios,
  file sizes, and progress tracking.
- Complete Data Type & Subtype Support:
  - Kline Types: klines, markPriceKlines, indexPriceKlines, premiumIndexKlines
  - Direct Types: trades, aggTrades, bookTicker, fundingRate
"""

import os
import sys
import time
import shutil
import zipfile
import argparse
import tempfile
import datetime
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Set, Optional, Tuple
import requests

# Default list of major symbols to include alongside the shortlisted pairs
DEFAULT_MAJOR_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "TRUMPUSDT"]

# Supported Data Types
ALL_KLINE_TYPES = ["klines", "markPriceKlines", "indexPriceKlines", "premiumIndexKlines"]
ALL_DIRECT_TYPES = ["trades", "aggTrades", "bookTicker", "fundingRate"]
ALL_DATA_TYPES = ALL_KLINE_TYPES + ALL_DIRECT_TYPES

# Standard Kline Timeframes on Binance Vision
ALL_TIMEFRAMES = [
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1mo"
]

# S3 Bucket URL for Binance Vision
BINANCE_S3_BUCKET = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
BINANCE_DATA_BASE_URL = "https://data.binance.vision"


def log(msg: str, level: str = "INFO"):
    """Prints a structured, timestamped log line to stdout."""
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    prefix = f"[{now_str}] [{level}]"
    print(f"{prefix} {msg}", flush=True)


def get_shortlisted_symbols(csv_path: Optional[str] = None) -> List[str]:
    """Reads shortlisted binance symbols from the generated CSV file."""
    if not csv_path:
        default_csv = Path(__file__).resolve().parent.parent / "Network_logs_by_codex" / "ALL_PAIRS_DETAILS_18Sep2026" / "SHORTLISTED_kcex_shortlisted_pairs.csv"
        csv_path = str(default_csv)

    symbols = []
    if os.path.exists(csv_path):
        import pandas as pd
        df = pd.read_csv(csv_path)
        if "binance_symbol" in df.columns:
            symbols = [str(s).strip() for s in df["binance_symbol"].dropna().tolist()]
        elif "symbol" in df.columns:
            symbols = [str(s).replace("_", "").strip() for s in df["symbol"].dropna().tolist()]
    return list(dict.fromkeys(symbols))


def list_s3_keys_with_prefix(prefix: str, max_retries: int = 4) -> List[str]:
    """Lists all S3 keys matching a prefix using AWS S3 XML ListObjects API."""
    keys = []
    marker = ""
    session = requests.Session()
    session.headers.update({"User-Agent": "BinanceVisionArchiver/2.0"})

    while True:
        url = f"{BINANCE_S3_BUCKET}?prefix={prefix}"
        if marker:
            url += f"&marker={marker}"

        for attempt in range(1, max_retries + 1):
            try:
                r = session.get(url, timeout=600)
                if r.status_code == 200:
                    root = ET.fromstring(r.text)
                    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
                    page_keys = [
                        elem.text for elem in root.findall(".//s3:Contents/s3:Key", ns)
                        if elem.text and elem.text.endswith(".zip")
                    ]
                    keys.extend(page_keys)

                    is_truncated_elem = root.find(".//s3:IsTruncated", ns)
                    is_truncated = is_truncated_elem is not None and is_truncated_elem.text.lower() == "true"
                    if is_truncated and page_keys:
                        marker = page_keys[-1]
                        break
                    else:
                        return keys
                elif r.status_code == 404:
                    return keys
                else:
                    time.sleep(1.5 * attempt)
            except Exception as e:
                if attempt == max_retries:
                    log(f"S3 request failed after {max_retries} attempts: {url} ({e})", "WARN")
                    return keys
                time.sleep(1.5 * attempt)
        else:
            break

    return keys


def extract_month_from_key(key: str) -> Optional[str]:
    """Extracts YYYY-MM from a Binance archive S3 key."""
    fn = key.split("/")[-1]
    if not fn.endswith(".zip"):
        return None
    stem = fn[:-4]
    parts = stem.split("-")
    if len(parts) >= 2:
        y, m = parts[-2], parts[-1]
        if len(y) == 4 and y.isdigit() and len(m) == 2 and m.isdigit():
            return f"{y}-{m}"
    return None


class BinanceDriveArchiver:
    def __init__(
        self,
        rclone_remote: str = "gdrive",
        gdrive_folder: str = "Binance_Historical_Data",
        market: str = "futures/um",
        temp_dir: Optional[str] = None,
        dry_run: bool = False
    ):
        self.rclone_remote = rclone_remote
        self.gdrive_folder = gdrive_folder.strip("/\\").replace("\\", "/")
        self.market = market
        self.temp_dir = temp_dir or tempfile.mkdtemp(prefix="binance_archive_")
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "BinanceVisionArchiver/2.0"})

        os.makedirs(self.temp_dir, exist_ok=True)
        self.existing_remote_files: Dict[str, Set[str]] = {}
        self._rclone_checked = False
        self.rclone_bin = "rclone"

    def check_rclone_available(self) -> bool:
        """Verifies if rclone is installed and located."""
        possible_bins = [
            "rclone",
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Rclone.Rclone_Microsoft.Winget.Source_8wekyb3d8bbwe\rclone-v1.75.1-windows-amd64\rclone.exe")
        ]
        for cmd in possible_bins:
            try:
                res = subprocess.run([cmd, "version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if res.returncode == 0:
                    self._rclone_checked = True
                    self.rclone_bin = cmd
                    return True
            except (FileNotFoundError, OSError):
                pass
        return False

    def get_existing_remote_filenames(self, remote_subfolder: str) -> Set[str]:
        """Fetches existing filenames in a remote Google Drive folder using rclone lsf."""
        if self.dry_run or not self._rclone_checked:
            return set()

        if remote_subfolder in self.existing_remote_files:
            return self.existing_remote_files[remote_subfolder]

        full_remote_path = f"{self.rclone_remote}:{self.gdrive_folder}/{remote_subfolder}".strip("/")
        try:
            res = subprocess.run(
                [self.rclone_bin, "lsf", full_remote_path, "--files-only"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3000
            )
            if res.returncode == 0:
                files = set(line.strip() for line in res.stdout.splitlines() if line.strip())
                self.existing_remote_files[remote_subfolder] = files
                return files
        except Exception as e:
            log(f"Failed to query remote directory '{full_remote_path}': {e}", "DEBUG")

        self.existing_remote_files[remote_subfolder] = set()
        return set()

    def discover_keys_for_symbol_and_type(
        self,
        symbol: str,
        data_type: str,
        timeframe: Optional[str] = None,
        start_month: Optional[str] = None,
        end_month: Optional[str] = None
    ) -> List[str]:
        """Discovers all S3 keys matching symbol, data type, timeframe, and date range."""
        if data_type in ALL_KLINE_TYPES:
            tf = timeframe or "1m"
            prefix = f"data/{self.market}/monthly/{data_type}/{symbol}/{tf}/"
        else:
            prefix = f"data/{self.market}/monthly/{data_type}/{symbol}/"

        keys = list_s3_keys_with_prefix(prefix)

        filtered_keys = []
        for k in keys:
            m = extract_month_from_key(k)
            if m:
                if start_month and m < start_month:
                    continue
                if end_month and m > end_month:
                    continue
            filtered_keys.append(k)

        return sorted(filtered_keys)

    def download_file_to_temp(self, s3_key: str, dest_path: str, max_retries: int = 4) -> Tuple[bool, int]:
        """Downloads a single archive file from Binance Vision. Returns (success, size_in_bytes)."""
        url = f"{BINANCE_DATA_BASE_URL}/{s3_key}"
        for attempt in range(1, max_retries + 1):
            try:
                with self.session.get(url, stream=True, timeout=4500) as r:
                    if r.status_code == 200:
                        total_downloaded = 0
                        with open(dest_path, "wb") as f:
                            for chunk in r.iter_content(chunk_size=1024 * 512):
                                if chunk:
                                    f.write(chunk)
                                    total_downloaded += len(chunk)
                        return True, total_downloaded
                    elif r.status_code == 404:
                        log(f"HTTP 404 File Not Found: {url}", "WARN")
                        return False, 0
            except Exception as e:
                if attempt == max_retries:
                    log(f"Failed to download {url} after {max_retries} attempts: {e}", "ERROR")
                    return False, 0
                time.sleep(2 * attempt)
        return False, 0

    def extract_csvs_from_zip(self, zip_path: str) -> List[Tuple[str, int]]:
        """
        Extracts all .csv files from the given ZIP archive into temp_dir.
        Returns a list of tuples: [(extracted_csv_path, uncompressed_size_bytes)]
        """
        extracted_files = []
        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                csv_members = [m for m in z.namelist() if m.lower().endswith(".csv")]
                if not csv_members:
                    log(f"No CSV file found inside {os.path.basename(zip_path)}", "ERROR")
                    return []

                for member in csv_members:
                    # Sanitize filename (prevent path traversal)
                    target_filename = os.path.basename(member)
                    target_path = os.path.join(self.temp_dir, target_filename)

                    # Extract file
                    with z.open(member) as source, open(target_path, "wb") as dest:
                        shutil.copyfileobj(source, dest, length=1024 * 512)

                    extracted_size = os.path.getsize(target_path)
                    extracted_files.append((target_path, extracted_size))
        except Exception as e:
            log(f"Extraction failed for {os.path.basename(zip_path)}: {e}", "ERROR")
            return []

        return extracted_files

    def upload_temp_file_to_drive(self, local_path: str, remote_subfolder: str) -> bool:
        """Uploads a single local CSV file to Google Drive using rclone copyto."""
        if self.dry_run:
            return True

        filename = os.path.basename(local_path)
        full_remote_path = f"{self.rclone_remote}:{self.gdrive_folder}/{remote_subfolder}/{filename}".replace("\\", "/")
        try:
            res = subprocess.run(
                [self.rclone_bin, "copyto", local_path, full_remote_path, "--retries", "3", "--low-level-retries", "10"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120000
            )
            if res.returncode == 0:
                return True
            else:
                log(f"rclone copyto returned non-zero code for {filename}: {res.stderr.strip()}", "ERROR")
                return False
        except Exception as e:
            log(f"rclone upload exception for {filename}: {e}", "ERROR")
            return False

    def sync_key_to_drive(self, s3_key: str) -> Tuple[str, int, int]:
        """
        Processes a single S3 key:
        1. Identifies expected uncompressed CSV filename.
        2. Checks if CSV already exists in Google Drive. If yes, skip.
        3. Downloads ZIP to temp folder.
        4. Extracts uncompressed CSV file(s) and purges the ZIP immediately.
        5. Uploads the uncompressed CSV(s) to Google Drive.
        6. Purges the uncompressed CSV(s) locally.

        Returns: (status: 'uploaded'|'skipped'|'error', downloaded_bytes, extracted_bytes)
        """
        parts = s3_key.split("/")
        zip_filename = parts[-1]
        expected_csv_filename = zip_filename[:-4] + ".csv"

        # Remote subfolder structure:
        # e.g., "klines/BTCUSDT/1m" or "trades/BTCUSDT"
        if len(parts) >= 6 and parts[3] in ALL_KLINE_TYPES:
            remote_subfolder = f"{parts[4]}/{parts[5]}/{parts[6]}"
        elif len(parts) >= 5:
            remote_subfolder = f"{parts[4]}/{parts[5]}"
        else:
            remote_subfolder = "misc"

        # 1. Check if the UNCOMPRESSED CSV already exists in Google Drive
        existing_files = self.get_existing_remote_filenames(remote_subfolder)
        if expected_csv_filename in existing_files:
            log(f"SKIP  | Drive already contains: {remote_subfolder}/{expected_csv_filename}", "CHECK")
            return "skipped", 0, 0

        if self.dry_run:
            log(f"PLAN  | Would download, extract and upload: {expected_csv_filename} -> {remote_subfolder}/", "DRYRUN")
            return "uploaded", 0, 0

        local_temp_zip = os.path.join(self.temp_dir, zip_filename)
        downloaded_bytes = 0
        extracted_bytes = 0
        extracted_csvs: List[Tuple[str, int]] = []

        try:
            # 2. Download ZIP
            t0 = time.time()
            success, downloaded_bytes = self.download_file_to_temp(s3_key, local_temp_zip)
            if not success or downloaded_bytes == 0:
                log(f"FAIL  | Download failed for {zip_filename}", "ERROR")
                return "error", 0, 0

            download_duration = max(time.time() - t0, 0.01)
            download_speed = (downloaded_bytes / (1024 * 1024)) / download_duration
            log(f"DL    | Downloaded {zip_filename} ({downloaded_bytes / (1024 * 1024):.2f} MB @ {download_speed:.2f} MB/s)", "STAGE")

            # 3. Extract CSV(s)
            extracted_csvs = self.extract_csvs_from_zip(local_temp_zip)
            if not extracted_csvs:
                log(f"FAIL  | Could not extract CSV from {zip_filename}", "ERROR")
                return "error", downloaded_bytes, 0

            # 4. Immediate cleanup of the local ZIP file
            try:
                os.remove(local_temp_zip)
            except Exception:
                pass

            # 5. Upload each extracted CSV to Google Drive
            all_uploaded = True
            for csv_path, csv_size in extracted_csvs:
                extracted_bytes += csv_size
                csv_name = os.path.basename(csv_path)

                t_up = time.time()
                log(f"EXTR  | Unzipped {csv_name} (Uncompressed: {csv_size / (1024 * 1024):.2f} MB)", "STAGE")
                log(f"UP    | Uploading {csv_name} -> {self.rclone_remote}:{self.gdrive_folder}/{remote_subfolder}/", "STAGE")

                upload_ok = self.upload_temp_file_to_drive(csv_path, remote_subfolder)
                up_duration = max(time.time() - t_up, 0.01)
                up_speed = (csv_size / (1024 * 1024)) / up_duration

                if upload_ok:
                    log(f"DONE  | Uploaded {csv_name} ({csv_size / (1024 * 1024):.2f} MB in {up_duration:.1f}s @ {up_speed:.2f} MB/s)", "SUCCESS")
                    if remote_subfolder in self.existing_remote_files:
                        self.existing_remote_files[remote_subfolder].add(csv_name)
                else:
                    log(f"FAIL  | Upload failed for {csv_name}", "ERROR")
                    all_uploaded = False

            return ("uploaded" if all_uploaded else "error"), downloaded_bytes, extracted_bytes

        finally:
            # 6. Always purge local CSVs and leftover ZIP files immediately
            if os.path.exists(local_temp_zip):
                try:
                    os.remove(local_temp_zip)
                except Exception:
                    pass
            for csv_path, _ in extracted_csvs:
                if os.path.exists(csv_path):
                    try:
                        os.remove(csv_path)
                    except Exception:
                        pass

    def cleanup(self):
        """Purges the temporary directory entirely."""
        if os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir, ignore_errors=True)
            except Exception:
                pass


def run_archival_pipeline(
    symbols: List[str],
    data_types: List[str],
    timeframes: List[str],
    start_month: Optional[str] = None,
    end_month: Optional[str] = "2026-08",
    rclone_remote: str = "gdrive",
    gdrive_folder: str = "Binance_Historical_Data",
    dry_run: bool = False
):
    """Executes the complete unzipped archival pipeline across symbols and data types."""
    archiver = BinanceDriveArchiver(
        rclone_remote=rclone_remote,
        gdrive_folder=gdrive_folder,
        dry_run=dry_run
    )

    if not dry_run:
        has_rclone = archiver.check_rclone_available()
        if not has_rclone:
            log("'rclone' executable was not found on PATH. Please install and configure rclone.", "ERROR")
            sys.exit(1)

    print("=" * 90)
    print("  BINANCE VISION -> GOOGLE DRIVE HISTORICAL ARCHIVER (UNZIPPED CSV)")
    print("=" * 90)
    log(f"Target Google Drive Folder : {gdrive_folder}")
    log(f"Remote Name                : {rclone_remote}")
    log(f"Dry Run Mode               : {dry_run}")
    log(f"Date Cutoff                : {start_month or 'Earliest'} -> {end_month or 'Latest'}")
    log(f"Total Target Symbols       : {len(symbols)} ({', '.join(symbols[:8])}{'...' if len(symbols)>8 else ''})")
    log(f"Data Types                 : {', '.join(data_types)}")
    log(f"Kline Timeframes           : {', '.join(timeframes)}")
    print("=" * 90)

    total_discovered = 0
    total_uploaded = 0
    total_skipped = 0
    total_errors = 0
    total_downloaded_bytes = 0
    total_uncompressed_bytes = 0

    pipeline_start_time = time.time()

    for s_idx, symbol in enumerate(symbols, 1):
        print("\n" + "-" * 90)
        log(f"[{s_idx}/{len(symbols)}] PROCESSING SYMBOL: {symbol}", "SYMBOL")
        print("-" * 90)

        for dt in data_types:
            if dt in ALL_KLINE_TYPES:
                for tf in timeframes:
                    keys = archiver.discover_keys_for_symbol_and_type(
                        symbol, dt, timeframe=tf, start_month=start_month, end_month=end_month
                    )
                    if not keys:
                        continue

                    total_discovered += len(keys)
                    log(f"[{symbol}] Discovered {len(keys)} monthly archives for '{dt}' [interval: {tf}]", "DISCOVER")

                    up_count = 0
                    skip_count = 0
                    err_count = 0

                    for k_idx, k in enumerate(keys, 1):
                        fn = k.split("/")[-1]
                        log(f"[{symbol} | {dt} {tf}] ({k_idx}/{len(keys)}) Processing: {fn}", "QUEUE")

                        status, dl_bytes, ext_bytes = archiver.sync_key_to_drive(k)
                        total_downloaded_bytes += dl_bytes
                        total_uncompressed_bytes += ext_bytes

                        if status == "uploaded":
                            up_count += 1
                            total_uploaded += 1
                        elif status == "skipped":
                            skip_count += 1
                            total_skipped += 1
                        else:
                            err_count += 1
                            total_errors += 1

                    log(f"[{symbol} | {dt} {tf}] Finished: {up_count} uploaded, {skip_count} skipped, {err_count} errors", "STATUS")
            else:
                keys = archiver.discover_keys_for_symbol_and_type(
                    symbol, dt, start_month=start_month, end_month=end_month
                )
                if not keys:
                    continue

                total_discovered += len(keys)
                log(f"[{symbol}] Discovered {len(keys)} monthly archives for '{dt}'", "DISCOVER")

                up_count = 0
                skip_count = 0
                err_count = 0

                for k_idx, k in enumerate(keys, 1):
                    fn = k.split("/")[-1]
                    log(f"[{symbol} | {dt}] ({k_idx}/{len(keys)}) Processing: {fn}", "QUEUE")

                    status, dl_bytes, ext_bytes = archiver.sync_key_to_drive(k)
                    total_downloaded_bytes += dl_bytes
                    total_uncompressed_bytes += ext_bytes

                    if status == "uploaded":
                        up_count += 1
                        total_uploaded += 1
                    elif status == "skipped":
                        skip_count += 1
                        total_skipped += 1
                    else:
                        err_count += 1
                        total_errors += 1

                log(f"[{symbol} | {dt}] Finished: {up_count} uploaded, {skip_count} skipped, {err_count} errors", "STATUS")

    archiver.cleanup()

    elapsed = time.time() - pipeline_start_time
    print("\n" + "=" * 90)
    print("  ARCHIVAL PIPELINE COMPLETED")
    print("=" * 90)
    log(f"Total Archives Discovered      : {total_discovered}")
    log(f"CSV Files Successfully Uploaded: {total_uploaded}")
    log(f"CSV Files Skipped (In Drive)   : {total_skipped}")
    log(f"Errors Encountered             : {total_errors}")
    log(f"Total Compressed Data Streamed : {total_downloaded_bytes / (1024 * 1024 * 1024):.2f} GB")
    log(f"Total Uncompressed CSV Archived: {total_uncompressed_bytes / (1024 * 1024 * 1024):.2f} GB")
    log(f"Total Elapsed Time             : {elapsed / 60:.2f} minutes")
    print("=" * 90)


def main():
    parser = argparse.ArgumentParser(description="Binance Vision Historical Data to Google Drive Archiver (Unzipped CSV)")
    parser.add_argument(
        "--symbols",
        type=str,
        default="ALL_TARGETED",
        help="Symbols mode: 'ALL_TARGETED' (shortlisted + majors), 'SHORTLISTED_ONLY', 'MAJORS_ONLY', or comma-separated list"
    )
    parser.add_argument(
        "--shortlisted-csv",
        type=str,
        default="",
        help="Path to SHORTLISTED_kcex_shortlisted_pairs.csv (defaults to auto-detect)"
    )
    parser.add_argument(
        "--data-types",
        type=str,
        default="ALL",
        help="Comma-separated data types (e.g. klines,trades,aggTrades,fundingRate) or 'ALL'"
    )
    parser.add_argument(
        "--timeframes",
        type=str,
        default="ALL",
        help="Comma-separated timeframes (e.g. 1m,5m,1h,1d) or 'ALL'"
    )
    parser.add_argument(
        "--start-month",
        type=str,
        default=None,
        help="Earliest month to fetch (format YYYY-MM, e.g. 2024-01)"
    )
    parser.add_argument(
        "--end-month",
        type=str,
        default="2026-08",
        help="Latest month to fetch (format YYYY-MM, default: 2026-08)"
    )
    parser.add_argument(
        "--rclone-remote",
        type=str,
        default="gdrive",
        help="Rclone remote name (default: gdrive)"
    )
    parser.add_argument(
        "--gdrive-folder",
        type=str,
        default="Binance_Historical_Data",
        help="Target folder in Google Drive"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report available files without downloading or uploading"
    )

    args = parser.parse_args()

    shortlisted = get_shortlisted_symbols(args.shortlisted_csv or None)
    if args.symbols == "ALL_TARGETED":
        symbols = list(dict.fromkeys(DEFAULT_MAJOR_SYMBOLS + shortlisted))
    elif args.symbols == "SHORTLISTED_ONLY":
        symbols = shortlisted
    elif args.symbols == "MAJORS_ONLY":
        symbols = DEFAULT_MAJOR_SYMBOLS
    else:
        symbols = [s.strip().upper().replace("_", "") for s in args.symbols.split(",") if s.strip()]

    if args.data_types.upper() == "ALL":
        data_types = ALL_DATA_TYPES
    else:
        data_types = [dt.strip() for dt in args.data_types.split(",") if dt.strip() in ALL_DATA_TYPES]

    if args.timeframes.upper() == "ALL":
        timeframes = ALL_TIMEFRAMES
    else:
        timeframes = [tf.strip() for tf in args.timeframes.split(",") if tf.strip() in ALL_TIMEFRAMES]

    run_archival_pipeline(
        symbols=symbols,
        data_types=data_types,
        timeframes=timeframes,
        start_month=args.start_month,
        end_month=args.end_month,
        rclone_remote=args.rclone_remote,
        gdrive_folder=args.gdrive_folder,
        dry_run=args.dry_run
    )


if __name__ == "__main__":
    main()
