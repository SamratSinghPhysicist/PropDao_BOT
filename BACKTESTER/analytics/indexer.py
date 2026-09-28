"""
High-Performance Report Indexer & Summary Cache
===============================================
Discovers, parses, indexes, and caches backtest reports (Markdown summaries,
trade CSVs, JSONLs, and GitHub Actions ZIP archives) with zero-lag streaming
downsampling and incremental file mtime invalidation.
"""

import os
import re
import csv
import json
import zipfile
import datetime
from typing import List, Dict, Any, Optional, Tuple

from BACKTESTER.analytics.models import (
    RunMetadata,
    RunScorecard,
    DirectionalStats,
    ExitAttribution,
    DownsampledPoint,
    DetailedAnalytics,
    BacktestRunRecord,
)


def parse_number(text: str, default: float = 0.0) -> float:
    """Extracts floating point number from formatted text like '+14.9632 USDT' or '₹1,413.27' or '-3672.57%'."""
    if not text:
        return default
    # Remove currency symbols, commas, percent, quotes, backticks
    cleaned = re.sub(r"[^\d\.\-\+eE]", "", text.replace(",", ""))
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return default


def parse_int(text: str, default: int = 0) -> int:
    """Extracts integer from text."""
    if not text:
        return default
    cleaned = re.sub(r"[^\d\-]", "", text.replace(",", ""))
    try:
        return int(cleaned)
    except (ValueError, TypeError):
        return default


BACKTESTER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT_DIR = os.path.abspath(os.path.join(BACKTESTER_DIR, ".."))


class ReportIndexer:
    """
    Manages scanning and caching of backtest reports.
    """

    def __init__(self, reports_dir: Optional[str] = None):
        if reports_dir is None:
            reports_dir = os.path.join(BACKTESTER_DIR, "reports")
        elif not os.path.isabs(reports_dir) and not os.path.exists(reports_dir):
            candidate = os.path.join(BACKTESTER_DIR, os.path.basename(reports_dir))
            if os.path.exists(candidate) or reports_dir in ("reports", os.path.join("BACKTESTER", "reports")):
                reports_dir = os.path.join(BACKTESTER_DIR, "reports")
            else:
                reports_dir = os.path.join(ROOT_DIR, reports_dir)
        self.reports_dir = os.path.abspath(reports_dir)
        self.cache_dir = os.path.join(self.reports_dir, ".cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.index_file = os.path.join(self.cache_dir, "index.json")

    def get_all_runs(self, force_reindex: bool = False) -> List[BacktestRunRecord]:
        """
        Retrieves all indexed backtest runs. Re-indexes if files have changed or force_reindex is True.
        """
        cached_index = self._load_cached_index()
        files_state = self._scan_directory_files()

        # Check if cache is still completely valid
        if not force_reindex and cached_index is not None:
            cache_mtimes = cached_index.get("_files_mtime", {})
            current_mtimes = {k: v["mtime"] for k, v in files_state.items()}
            cache_ver = cached_index.get("_version", 1)
            if cache_mtimes == current_mtimes and cache_ver >= 3:
                # Cache is 100% up to date!
                records = [
                    BacktestRunRecord.from_dict(r)
                    for r in cached_index.get("runs", [])
                ]
                return sorted(
                    records,
                    key=lambda x: x.metadata.timestamp_utc or x.metadata.run_id,
                    reverse=True
                )

        # Indexing needed
        records = self._perform_indexing(files_state)

        # Save to cache
        cache_data = {
            "_version": 3,
            "_updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "_files_mtime": {k: v["mtime"] for k, v in files_state.items()},
            "runs": [r.to_dict() for r in records]
        }
        try:
            with open(self.index_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2)
        except Exception as e:
            print(f"[!] Warning: Could not write cache file: {e}")

        return sorted(
            records,
            key=lambda x: x.metadata.timestamp_utc or x.metadata.run_id,
            reverse=True
        )

    def get_run_by_id(self, run_id: str) -> Optional[BacktestRunRecord]:
        """Fetches a single run by run_id."""
        runs = self.get_all_runs()
        for r in runs:
            if r.metadata.run_id == run_id:
                return r
        return None

    def get_downsampled_curve(self, run_id: str) -> List[Dict[str, Any]]:
        """Loads or computes downsampled equity curve points for a run."""
        curve_cache_path = os.path.join(self.cache_dir, f"{run_id}_curve.json")
        if os.path.exists(curve_cache_path):
            try:
                with open(curve_cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

        # Compute on demand
        run = self.get_run_by_id(run_id)
        if not run:
            return []

        curve = self._compute_curve_and_details(run_id, run.scorecard.initial_capital_usdt)
        return [p.to_dict() for p in curve]

    def _scan_directory_files(self) -> Dict[str, Dict[str, Any]]:
        """Gathers file stats of all relevant files in reports_dir."""
        files = {}
        if not os.path.exists(self.reports_dir):
            return files

        for fname in os.listdir(self.reports_dir):
            if fname.startswith("."):
                continue
            full_path = os.path.join(self.reports_dir, fname)
            if not os.path.isfile(full_path):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext in (".md", ".csv", ".jsonl", ".zip", ".json"):
                st = os.stat(full_path)
                files[fname] = {
                    "path": full_path,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "ext": ext
                }
        return files

    def _load_cached_index(self) -> Optional[Dict[str, Any]]:
        if not os.path.exists(self.index_file):
            return None
        try:
            with open(self.index_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _perform_indexing(self, files_state: Dict[str, Dict[str, Any]]) -> List[BacktestRunRecord]:
        """Indexes all discovered reports."""
        run_groups: Dict[str, Dict[str, Any]] = {}
        zip_mappings: Dict[str, Dict[str, Any]] = {}

        # 1. Scan ZIP files to inspect contents and cloud metadata
        for fname, f_info in files_state.items():
            if f_info["ext"] == ".zip":
                try:
                    with zipfile.ZipFile(f_info["path"], "r") as z:
                        namelist = z.namelist()
                        zip_mappings[fname] = {
                            "members": namelist,
                            "path": f_info["path"],
                            "size_mb": round(f_info["size"] / (1024 * 1024), 2)
                        }
                        # Check if any summary file inside zip matches an ID
                        for m in namelist:
                            if m.endswith("_summary.md"):
                                rid = m.replace("_summary.md", "")
                                if rid not in run_groups:
                                    run_groups[rid] = {}
                                run_groups[rid]["zip_file"] = fname
                                run_groups[rid]["zip_size_mb"] = zip_mappings[fname]["size_mb"]
                except Exception as e:
                    print(f"[!] Error inspecting zip {fname}: {e}")

        # 2. Discover loose files
        for fname, f_info in files_state.items():
            if f_info["ext"] in (".md", ".csv", ".jsonl"):
                for suffix in ("_summary.md", "_trades.csv", "_trades.jsonl"):
                    if fname.endswith(suffix):
                        rid = fname.replace(suffix, "")
                        if rid not in run_groups:
                            run_groups[rid] = {}
                        if suffix == "_summary.md":
                            run_groups[rid]["md_file"] = f_info["path"]
                        elif suffix == "_trades.csv":
                            run_groups[rid]["csv_file"] = f_info["path"]
                            run_groups[rid]["csv_size_mb"] = round(f_info["size"] / (1024 * 1024), 2)
                        elif suffix == "_trades.jsonl":
                            run_groups[rid]["jsonl_file"] = f_info["path"]
                            run_groups[rid]["jsonl_size_mb"] = round(f_info["size"] / (1024 * 1024), 2)
                        break

        # 3. Process each identified run
        records = []
        for rid, files in run_groups.items():
            try:
                record = self._parse_single_run(rid, files)
                if record:
                    records.append(record)
            except Exception as e:
                print(f"[!] Error parsing run {rid}: {e}")

        return records

    def _parse_single_run(self, rid: str, files: Dict[str, Any]) -> Optional[BacktestRunRecord]:
        """Parses markdown summary and trade csv for a single run ID."""
        md_content = ""

        # 1. Read Markdown content
        if "md_file" in files and os.path.exists(files["md_file"]):
            with open(files["md_file"], "r", encoding="utf-8", errors="replace") as f:
                md_content = f.read()
        elif "zip_file" in files:
            # Extract or read directly from zip
            zip_path = os.path.join(self.reports_dir, files["zip_file"])
            try:
                with zipfile.ZipFile(zip_path, "r") as z:
                    target_md = f"{rid}_summary.md"
                    if target_md in z.namelist():
                        with z.open(target_md) as zm:
                            md_content = zm.read().decode("utf-8", errors="replace")
            except Exception:
                pass

        if not md_content:
            return None

        # 2. Parse Markdown
        meta, scorecard, directional, exit_attrs = self._parse_markdown_summary(rid, md_content)

        # Enhance metadata with file stats
        meta.has_md = "md_file" in files or "zip_file" in files
        meta.has_csv = "csv_file" in files
        meta.has_jsonl = "jsonl_file" in files
        meta.has_zip = "zip_file" in files
        meta.zip_filename = files.get("zip_file")
        meta.csv_size_mb = files.get("csv_size_mb", 0.0)
        meta.jsonl_size_mb = files.get("jsonl_size_mb", 0.0)
        meta.zip_size_mb = files.get("zip_size_mb", 0.0)
        if meta.has_zip:
            meta.source = "github_cloud"

        # 3. Check / compute details and downsampled equity curve
        details = self._get_or_compute_details(rid, scorecard.initial_capital_usdt)

        return BacktestRunRecord(
            metadata=meta,
            scorecard=scorecard,
            directional=directional,
            exit_attributions=exit_attrs,
            detailed=details
        )

    def _parse_markdown_summary(
        self,
        run_id: str,
        md_text: str
    ) -> Tuple[RunMetadata, RunScorecard, DirectionalStats, List[ExitAttribution]]:
        """Parses Markdown report text into typed models."""
        meta = RunMetadata(run_id=run_id, run_name=run_id)
        scorecard = RunScorecard()
        directional = DirectionalStats()
        exit_attrs: List[ExitAttribution] = []

        # Extract Header
        header_m = re.search(r"# 📊 Institutional Backtest Performance Report:\s*([^\n\r]+)", md_text)
        if header_m:
            meta.symbol = header_m.group(1).strip()
            if "_" in meta.symbol:
                parts = meta.symbol.split("_")
                meta.base_asset, meta.quote_asset = parts[0], parts[1]

        gen_m = re.search(r"\*\*Generated:\*\*\s*`([^`]+)`", md_text)
        if gen_m:
            meta.timestamp_utc = gen_m.group(1).strip()

        # Helper to extract Markdown table row value by key pattern
        def get_table_cell(pattern: str) -> str:
            match = re.search(pattern, md_text, re.IGNORECASE)
            return match.group(1).strip() if match else ""

        # Scorecard
        scorecard.initial_capital_usdt = parse_number(get_table_cell(r"\|\s*\*\*Initial Capital\*\*\s*\|\s*`([^`]+)`"))
        scorecard.final_balance_usdt = parse_number(get_table_cell(r"\|\s*\*\*Final Balance\*\*\s*\|\s*`([^`]+)`"))
        scorecard.net_pnl_usdt = parse_number(get_table_cell(r"\|\s*\*\*Net Realized PnL\*\*\s*\|\s*\*\*`([^`]+)`\*\*"))
        scorecard.net_pnl_inr = parse_number(get_table_cell(r"\|\s*\*\*Net Realized PnL\*\*\s*\|[^\|]+\|\s*\*\*`([^`]+)`\*\*"))
        scorecard.net_roi_pct = parse_number(get_table_cell(r"\|\s*\*\*Net Realized PnL\*\*\s*\|[^\|]+\|[^\|]+\|\s*\*\*`([^\s%`]+)"))
        scorecard.gross_profit_usdt = parse_number(get_table_cell(r"\|\s*\*\*Gross Profit\*\*\s*\|\s*`([^`]+)`"))
        scorecard.gross_loss_usdt = abs(parse_number(get_table_cell(r"\|\s*\*\*Gross Loss\*\*\s*\|\s*`([^`]+)`")))
        scorecard.total_fees_usdt = parse_number(get_table_cell(r"\|\s*\*\*Total Taker Fees Paid\*\*\s*\|\s*`([^`]+)`"))
        scorecard.profit_factor = parse_number(get_table_cell(r"\|\s*\*\*Profit Factor\*\*\s*\|\s*\*\*`([^`]+)`\*\*"))
        scorecard.win_loss_payoff = parse_number(get_table_cell(r"\|\s*\*\*Win / Loss Payoff\*\*\s*\|\s*`([^`]+)`"))
        scorecard.max_drawdown_usdt = abs(parse_number(get_table_cell(r"\|\s*\*\*Max Drawdown\*\*\s*\|\s*`([^`]+)`")))
        scorecard.max_drawdown_pct = abs(parse_number(get_table_cell(r"\|\s*\*\*Max Drawdown\*\*\s*\|[^\|]+\|[^\|]+\|\s*\*\*`-?([^\s%`]+)")) )
        scorecard.win_rate_pct = parse_number(get_table_cell(r"\|\s*\*\*Win Rate\*\*\s*\|\s*\*\*`([^%`]+)%`\*\*"))
        scorecard.sharpe_ratio = parse_number(get_table_cell(r"\|\s*\*\*Sharpe Ratio[^\*]*\*\*\s*\|\s*`([^`]+)`"))
        scorecard.sortino_ratio = parse_number(get_table_cell(r"\|\s*\*\*Sortino Ratio\*\*\s*\|\s*`([^`]+)`"))
        scorecard.calmar_ratio = parse_number(get_table_cell(r"\|\s*\*\*Calmar Ratio\*\*\s*\|\s*`([^`]+)`"))

        meta.starting_capital_usdt = scorecard.initial_capital_usdt

        # Settings
        tf_cell_m = re.search(r"\|\s*\*\*Candle Timeframe\*\*\s*\|\s*([^\|]+)\|", md_text)
        if tf_cell_m:
            tf_m = re.search(r"`([^`]+)`", tf_cell_m.group(1))
            meta.timeframe = tf_m.group(1).strip() if tf_m else tf_cell_m.group(1).strip()
        else:
            meta.timeframe = get_table_cell(r"\|\s*\*\*Candle Timeframe\*\*\s*\|\s*`([^`]+)`")
        if not meta.timeframe:
            tf_match = re.search(r"_(1m|3m|5m|15m|30m|1h|4h|1d)_", run_id.lower())
            meta.timeframe = tf_match.group(1) if tf_match else "1m"
        
        strat_m = re.search(r"\|\s*\*\*Strategy Evaluated\*\*\s*\|\s*`([^`]+)`\s*\|\s*([^\r\n\|]+(?:\s*[;\|]\s*[^\r\n\|]+)*)\s*\|", md_text)
        if strat_m:
            meta.strategy = strat_m.group(1).strip()
            meta.strategy_desc = strat_m.group(2).strip()
        else:
            meta.strategy = get_table_cell(r"\|\s*\*\*Strategy Evaluated\*\*\s*\|\s*`([^`]+)`")
            meta.strategy_desc = get_table_cell(r"\|\s*\*\*Strategy Evaluated\*\*\s*\|[^\|]+\|\s*([^\|]+)\|")

        # Strategy Preset
        meta.strategy_preset = get_table_cell(r"\|\s*\*\*Strategy Preset\*\*\s*\|\s*`([^`]+)`")
        if not meta.strategy_preset:
            meta.strategy_preset = get_table_cell(r"\|\s*\*\*Active Strategy Preset\*\*\s*\|\s*`([^`]+)`")
        if not meta.strategy_preset and meta.strategy_desc:
            p_m = re.search(r"Preset:\s*([^\s;|\)]+)", meta.strategy_desc)
            if p_m:
                meta.strategy_preset = p_m.group(1).strip()

        # Build meta.parameters & meta.filters
        params: Dict[str, Any] = {}
        filters: Dict[str, Any] = {}

        if meta.strategy_preset:
            params["preset"] = meta.strategy_preset

        fast_ema = parse_number(get_table_cell(r"\|\s*\*\*Fast EMA Period\*\*\s*\|\s*`([^`]+)`"))
        if fast_ema:
            params["ema_fast"] = int(fast_ema)
        slow_ema = parse_number(get_table_cell(r"\|\s*\*\*Slow EMA Period\*\*\s*\|\s*`([^`]+)`"))
        if slow_ema:
            params["ema_slow"] = int(slow_ema)

        rsi_p = parse_number(get_table_cell(r"\|\s*\*\*RSI Period\*\*\s*\|\s*`([^`]+)`"))
        if rsi_p:
            params["stoch_rsi_period"] = int(rsi_p)
        stoch_p = parse_number(get_table_cell(r"\|\s*\*\*Stoch Lookback Period\*\*\s*\|\s*`([^`]+)`"))
        if stoch_p:
            params["stoch_period"] = int(stoch_p)
        stoch_k = parse_number(get_table_cell(r"\|\s*\*\*\%K Smoothing\*\*\s*\|\s*`([^`]+)`"))
        if stoch_k:
            params["stoch_k_period"] = int(stoch_k)
        stoch_d = parse_number(get_table_cell(r"\|\s*\*\*\%D Smoothing\*\*\s*\|\s*`([^`]+)`"))
        if stoch_d:
            params["stoch_d_period"] = int(stoch_d)
        os_val = parse_number(get_table_cell(r"\|\s*\*\*Oversold Threshold[^\*]*\*\*\s*\|\s*`([^`]+)`"))
        if os_val:
            params["stoch_oversold"] = os_val
        ob_val = parse_number(get_table_cell(r"\|\s*\*\*Overbought Threshold[^\*]*\*\*\s*\|\s*`([^`]+)`"))
        if ob_val:
            params["stoch_overbought"] = ob_val

        candle_conf = get_table_cell(r"\|\s*\*\*Candle Close Confirmation\*\*\s*\|\s*`([^`]+)`")
        if candle_conf:
            params["candle_close_confirmation"] = ("ENABLED" in candle_conf.upper())

        dir_flow = get_table_cell(r"\|\s*\*\*Directional Flow Mode\*\*\s*\|\s*`([^`]+)`")
        if dir_flow:
            params["directional_flow"] = dir_flow

        # Smart fallback inference for existing reports
        strat_upper = meta.strategy.upper()
        if "EMA" in strat_upper:
            if "ema_fast" not in params or "ema_slow" not in params:
                if meta.strategy_preset == "9/21" or "9/21" in meta.strategy_desc:
                    params["preset"] = meta.strategy_preset = "9/21"
                    params["ema_fast"], params["ema_slow"] = 9, 21
                elif meta.strategy_preset == "3/8" or "3/8" in meta.strategy_desc:
                    params["preset"] = meta.strategy_preset = "3/8"
                    params["ema_fast"], params["ema_slow"] = 3, 8
                else:
                    params["preset"] = meta.strategy_preset = meta.strategy_preset or "5/13"
                    params["ema_fast"], params["ema_slow"] = 5, 13
        elif "STOCH" in strat_upper:
            if "stoch_rsi_period" not in params:
                if meta.strategy_preset == "MICRO_BURST" or "MICRO_BURST" in meta.strategy_desc:
                    params["preset"] = meta.strategy_preset = "MICRO_BURST"
                    params["stoch_rsi_period"] = 7
                    params["stoch_period"] = 7
                    params["stoch_k_period"] = 3
                    params["stoch_d_period"] = 3
                    params["stoch_oversold"] = 15.0
                    params["stoch_overbought"] = 85.0
                elif meta.strategy_preset == "STANDARD" or "STANDARD" in meta.strategy_desc:
                    params["preset"] = meta.strategy_preset = "STANDARD"
                    params["stoch_rsi_period"] = 14
                    params["stoch_period"] = 14
                    params["stoch_k_period"] = 3
                    params["stoch_d_period"] = 3
                    params["stoch_oversold"] = 20.0
                    params["stoch_overbought"] = 80.0
                else:
                    params["preset"] = meta.strategy_preset = meta.strategy_preset or "FAST_SCALP"
                    params["stoch_rsi_period"] = 9
                    params["stoch_period"] = 9
                    params["stoch_k_period"] = 3
                    params["stoch_d_period"] = 3
                    params["stoch_oversold"] = 20.0
                    params["stoch_overbought"] = 80.0

        # Extract Trade Optimization & Regime Filters
        dur_mon = get_table_cell(r"\|\s*\*\*Trade Duration Monitoring\*\*\s*\|\s*`([^`]+)`")
        dur_action_line = get_table_cell(r"\|\s*\*\*Time-Stop Protective Exit\*\*\s*\|[^\|]+\|\s*([^\|]+)\|")
        if dur_mon:
            filters["duration_filter"] = ("ENABLED" in dur_mon.upper())
            deep_m = re.search(r"(\d+(\.\d+)?)\s*s", get_table_cell(r"\|\s*\*\*Trade Duration Monitoring\*\*\s*\|[^\|]+\|\s*([^\|]+)\|"))
            if deep_m:
                filters["deep_monitor_s"] = float(deep_m.group(1))
            hold_m = re.search(r">\s*`?(\d+(\.\d+)?)\s*s", dur_action_line)
            if hold_m:
                filters["max_hold_s"] = float(hold_m.group(1))
            if "Action" in dur_action_line:
                act_m = re.search(r"Action\s*`?([A-Z_]+)`?", dur_action_line)
                if act_m:
                    filters["duration_action"] = act_m.group(1)
        else:
            filters["duration_filter"] = False

        adx_stat = get_table_cell(r"\|\s*\*\*ADX Trend Regime Filter\*\*\s*\|\s*`([^`]+)`")
        if adx_stat:
            filters["adx_filter"] = ("ENABLED" in adx_stat.upper())
            adx_det = get_table_cell(r"\|\s*\*\*ADX Trend Regime Filter\*\*\s*\|[^\|]+\|\s*([^\|]+)\|")
            adx_p_m = re.search(r"Period:\s*`?(\d+)`?", adx_det)
            if adx_p_m:
                filters["adx_period"] = int(adx_p_m.group(1))
            adx_th_m = re.search(r"Threshold:\s*`?(\d+(\.\d+)?)`?", adx_det)
            if adx_th_m:
                filters["adx_threshold"] = float(adx_th_m.group(1))
        else:
            filters["adx_filter"] = False

        htf_stat = get_table_cell(r"\|\s*\*\*HTF Trend Baseline \(200 EMA\)\*\*\s*\|\s*`([^`]+)`")
        if htf_stat:
            filters["htf_trend_filter"] = ("ENABLED" in htf_stat.upper())
        else:
            filters["htf_trend_filter"] = False

        hourly_stat = get_table_cell(r"\|\s*\*\*Hourly Session Filter\*\*\s*\|\s*`([^`]+)`")
        if hourly_stat:
            filters["hourly_filter"] = ("ENABLED" in hourly_stat.upper())
        else:
            filters["hourly_filter"] = False

        bias_val = get_table_cell(r"\|\s*\*\*Directional Bias Policy\*\*\s*\|\s*`([^`]+)`")
        filters["direction_bias"] = bias_val or "BOTH"

        meta.parameters = params
        meta.filters = filters
        # Robust Date Range Extraction
        dr_cell_m = re.search(r"\|\s*\*\*Evaluation Date Range\*\*\s*\|\s*([^\|]+)\|", md_text)
        if dr_cell_m:
            dr_cell = dr_cell_m.group(1).strip()
            dates = re.findall(r"\d{4}-\d{2}-\d{2}", dr_cell)
            if len(dates) >= 2:
                meta.start_date = dates[0]
                meta.end_date = dates[1]
                meta.date_range = f"{meta.start_date} → {meta.end_date}"
            elif len(dates) == 1:
                meta.start_date = dates[0]
                meta.end_date = dates[0]
                meta.date_range = meta.start_date
            else:
                clean_dr = re.sub(r"[`*]", "", dr_cell).strip()
                meta.date_range = clean_dr
                if "→" in clean_dr:
                    parts = clean_dr.split("→")
                    meta.start_date = parts[0].strip()
                    meta.end_date = parts[1].strip()
        else:
            meta.date_range = "N/A"

        meta.high_fidelity_ticks = "ENABLED" in get_table_cell(r"\|\s*\*\*High-Fidelity Simulation\*\*\s*\|\s*`([^`]+)`")
        meta.slippage_ticks = parse_int(get_table_cell(r"\|\s*\*\*Slippage Tolerance\*\*\s*\|\s*`([^\s`]+)"))
        meta.sizing_mode = get_table_cell(r"\|\s*\*\*Sizing Mode\*\*\s*\|\s*`([^`]+)`") or "MULTIPLIER"
        meta.volume_desc = get_table_cell(r"\|\s*\*\*Trade Volume / Quantity\*\*\s*\|\s*`([^`]+)`")
        vol_m = re.search(r"(\d+(\.\d+)?)\s*contract", meta.volume_desc)
        if vol_m:
            meta.contracts = float(vol_m.group(1))

        meta.leverage = parse_int(get_table_cell(r"\|\s*\*\*Leverage Multiplier\*\*\s*\|\s*`([^\s`x]+)"))
        meta.tp_target_desc = get_table_cell(r"\|\s*\*\*Take Profit Target\*\*\s*\|\s*`([^`]+)`")
        tp_m = re.search(r"\+(\d+)\s*ticks?", meta.tp_target_desc)
        if tp_m:
            meta.tp_ticks = int(tp_m.group(1))

        meta.sl_rule_desc = get_table_cell(r"\|\s*\*\*Stop Loss Rule\*\*\s*\|\s*`([^`]+)`")
        if "ROE" in meta.sl_rule_desc:
            meta.sl_mode = "ROE"
            sl_m = re.search(r"(\d+(\.\d+)?)\s*%\s*ROE", meta.sl_rule_desc)
            if sl_m:
                meta.sl_value = float(sl_m.group(1))
        else:
            meta.sl_mode = "TICKS"
            sl_m = re.search(r"(\d+)\s*ticks?", meta.sl_rule_desc)
            if sl_m:
                meta.sl_value = float(sl_m.group(1))

        meta.contract_size = parse_number(get_table_cell(r"\|\s*\*\*Contract Size \(cs\)\*\*\s*\|\s*`([^\s`]+)"))
        meta.price_unit = parse_number(get_table_cell(r"\|\s*\*\*Price Unit \(pu / tick\)\*\*\s*\|\s*`([^`]+)`"))

        # Exchange Specifications & Fee Rates
        meta.fee_mode = get_table_cell(r"\|\s*\*\*Fee Schedule Mode\*\*\s*\|\s*`([^`]+)`") or "ZERO"
        meta.maker_fee_pct = parse_number(get_table_cell(r"\|\s*\*\*Maker Fee Rate\*\*\s*\|\s*`([^%`]+)%?`"))
        meta.taker_fee_pct = parse_number(get_table_cell(r"\|\s*\*\*Taker Fee Rate\*\*\s*\|\s*`([^%`]+)%?`"))
        meta.price_precision = parse_int(get_table_cell(r"\|\s*\*\*Price Precision\*\*\s*\|\s*`(\d+)")) or 4
        meta.min_volume = parse_number(get_table_cell(r"\|\s*\*\*Min Volume\*\*\s*\|\s*`([^\s`]+)")) or 1.0
        meta.max_leverage = parse_int(get_table_cell(r"\|\s*\*\*Max Leverage\*\*\s*\|\s*`([^\s`x]+)")) or meta.leverage

        # Trade Stats
        scorecard.total_trades = parse_int(get_table_cell(r"\|\s*\*\*Total Trades Executed\*\*\s*\|\s*`([^`]+)`"))
        scorecard.winning_trades = parse_int(get_table_cell(r"\|\s*\*\*Winning Trades\*\*\s*\|\s*`([^`]+)`"))
        scorecard.losing_trades = parse_int(get_table_cell(r"\|\s*\*\*Losing Trades\*\*\s*\|\s*`([^`]+)`"))
        scorecard.scratch_trades = parse_int(get_table_cell(r"\|\s*\*\*Scratch / Break-even\*\*\s*\|\s*`([^`]+)`"))
        scorecard.avg_trade_pnl_usdt = parse_number(get_table_cell(r"\|\s*\*\*Average Trade PnL\*\*\s*\|\s*`([^\s`]+)"))
        scorecard.avg_win_pnl_usdt = parse_number(get_table_cell(r"\|\s*\*\*Average Winning Trade\*\*\s*\|\s*`([^\s`]+)"))
        scorecard.avg_loss_pnl_usdt = abs(parse_number(get_table_cell(r"\|\s*\*\*Average Losing Trade\*\*\s*\|\s*`([^\s`]+)")))
        scorecard.max_consecutive_wins = parse_int(get_table_cell(r"\|\s*\*\*Max Consecutive Wins\*\*\s*\|\s*`([^\s`]+)"))
        scorecard.max_consecutive_losses = parse_int(get_table_cell(r"\|\s*\*\*Max Consecutive Losses\*\*\s*\|\s*`([^\s`]+)"))

        # Parse duration string (e.g. 56.1s)
        dur_str = get_table_cell(r"\|\s*\*\*Average Trade Duration\*\*\s*\|\s*`([^`]+)`")
        if dur_str:
            scorecard.avg_duration_seconds = self._parse_duration_string(dur_str)

        # Directional Breakdown
        directional.long_trades = parse_int(get_table_cell(r"\|\s*\*\*Total Trades\*\*\s*\|\s*`(\d+)`"))
        directional.short_trades = parse_int(get_table_cell(r"\|\s*\*\*Total Trades\*\*\s*\|[^\|]+\|\s*`(\d+)`"))
        
        # Wins/Losses e.g. `18926 W / 4359 L`
        long_wl = get_table_cell(r"\|\s*\*\*Wins / Losses\*\*\s*\|\s*`([^`]+)`")
        short_wl = get_table_cell(r"\|\s*\*\*Wins / Losses\*\*\s*\|[^\|]+\|\s*`([^`]+)`")
        if "W" in long_wl:
            m = re.search(r"(\d+)\s*W\s*/\s*(\d+)\s*L", long_wl)
            if m:
                directional.long_wins, directional.long_losses = int(m.group(1)), int(m.group(2))
        if "W" in short_wl:
            m = re.search(r"(\d+)\s*W\s*/\s*(\d+)\s*L", short_wl)
            if m:
                directional.short_wins, directional.short_losses = int(m.group(1)), int(m.group(2))

        directional.long_win_rate_pct = parse_number(get_table_cell(r"\|\s*\*\*Win Rate\*\*\s*\|\s*\*\*`([^%`]+)%`\*\*"))
        directional.short_win_rate_pct = parse_number(get_table_cell(r"\|\s*\*\*Win Rate\*\*\s*\|[^\|]+\|\s*\*\*`([^%`]+)%`\*\*"))
        directional.long_gross_profit = parse_number(get_table_cell(r"\|\s*\*\*Gross Profit\*\*\s*\|\s*`([^`]+)`"))
        directional.short_gross_profit = parse_number(get_table_cell(r"\|\s*\*\*Gross Profit\*\*\s*\|[^\|]+\|\s*`([^`]+)`"))
        directional.long_gross_loss = abs(parse_number(get_table_cell(r"\|\s*\*\*Gross Loss\*\*\s*\|\s*`([^`]+)`")))
        directional.short_gross_loss = abs(parse_number(get_table_cell(r"\|\s*\*\*Gross Loss\*\*\s*\|[^\|]+\|\s*`([^`]+)`")))
        directional.long_net_pnl_usdt = parse_number(get_table_cell(r"\|\s*\*\*Net Realized PnL\*\*\s*\|\s*\*\*`([^`]+)`\*\*"))
        directional.short_net_pnl_usdt = parse_number(get_table_cell(r"\|\s*\*\*Net Realized PnL\*\*\s*\|[^\|]+\|\s*\*\*`([^`]+)`\*\*"))
        directional.long_profit_factor = parse_number(get_table_cell(r"\|\s*\*\*Profit Factor\*\*\s*\|\s*`([^`]+)`"))
        directional.short_profit_factor = parse_number(get_table_cell(r"\|\s*\*\*Profit Factor\*\*\s*\|[^\|]+\|\s*`([^`]+)`"))

        # Exit Attributions
        exit_matches = re.finditer(
            r"\|\s*`([^`]+)`\s*\|\s*`(\d+)`\s*\|\s*`([^%`]+)%`\s*\|\s*`([^`]+)`\s*\|[^\|]+\|\s*`([^%`]+)%`\s*\|\s*`([^`]+)`\s*\|",
            md_text
        )
        for em in exit_matches:
            reason = em.group(1).strip()
            count = int(em.group(2))
            pct = float(em.group(3))
            pnl = parse_number(em.group(4))
            wr = float(em.group(5))
            dur = self._parse_duration_string(em.group(6).strip())
            exit_attrs.append(ExitAttribution(
                reason=reason,
                count=count,
                pct_of_trades=pct,
                total_pnl_usdt=pnl,
                win_rate_pct=wr,
                avg_duration_seconds=dur
            ))

        # Generate a readable run name
        meta.run_name = f"{meta.symbol} {meta.strategy} {meta.timeframe} ({meta.leverage}x TP{meta.tp_ticks} {meta.sl_mode}{int(meta.sl_value)})"

        return meta, scorecard, directional, exit_attrs

    def _parse_duration_string(self, text: str) -> float:
        """Parses durations like '56.1s', '1m 54s', '1h 10m 45s'."""
        total_sec = 0.0
        h_m = re.search(r"(\d+)h", text)
        if h_m:
            total_sec += float(h_m.group(1)) * 3600
        m_m = re.search(r"(\d+)m", text)
        if m_m:
            total_sec += float(m_m.group(1)) * 60
        s_m = re.search(r"(\d+(\.\d+)?)s", text)
        if s_m:
            total_sec += float(s_m.group(1))
        return round(total_sec, 2) if total_sec > 0 else parse_number(text)

    def _get_or_compute_details(self, run_id: str, initial_balance: float) -> Optional[DetailedAnalytics]:
        """Loads cached details or computes from trades.csv."""
        details_cache_path = os.path.join(self.cache_dir, f"{run_id}_details.json")
        if os.path.exists(details_cache_path):
            try:
                with open(details_cache_path, "r", encoding="utf-8") as f:
                    return DetailedAnalytics.from_dict(json.load(f))
            except Exception:
                pass

        # Compute
        self._compute_curve_and_details(run_id, initial_balance)
        if os.path.exists(details_cache_path):
            try:
                with open(details_cache_path, "r", encoding="utf-8") as f:
                    return DetailedAnalytics.from_dict(json.load(f))
            except Exception:
                pass
        return None

    def _compute_curve_and_details(
        self,
        run_id: str,
        initial_balance: float
    ) -> List[DownsampledPoint]:
        """
        Streams trades.csv with zero memory bloat to generate:
        1. Downsampled equity curve points (< 600 points)
        2. Detailed analytics (durations, hourly distribution, day-of-week, pnl quantiles)
        """
        csv_path = os.path.join(self.reports_dir, f"{run_id}_trades.csv")
        target_curve_file = os.path.join(self.cache_dir, f"{run_id}_curve.json")
        target_details_file = os.path.join(self.cache_dir, f"{run_id}_details.json")

        if not os.path.exists(csv_path):
            return []

        curve_points: List[DownsampledPoint] = []
        duration_bins = {
            "<10s": {"count": 0, "wins": 0, "pnl": 0.0},
            "10s-1m": {"count": 0, "wins": 0, "pnl": 0.0},
            "1m-5m": {"count": 0, "wins": 0, "pnl": 0.0},
            "5m-15m": {"count": 0, "wins": 0, "pnl": 0.0},
            "15m-1h": {"count": 0, "wins": 0, "pnl": 0.0},
            ">1h": {"count": 0, "wins": 0, "pnl": 0.0},
        }
        hourly_bins = [{"hour": h, "trades": 0, "wins": 0, "pnl": 0.0} for h in range(24)]
        days_of_week = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        dow_bins = [{"day": d, "trades": 0, "wins": 0, "pnl": 0.0} for d in days_of_week]

        pnl_sample = []
        roe_sample = []

        total_trades = 0
        peak_balance = initial_balance
        curr_balance = initial_balance

        # First pass or line count estimate
        try:
            with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                total_trades = len(rows)
        except Exception as e:
            print(f"[!] Error reading trades CSV: {e}")
            return []

        if total_trades == 0:
            return []

        sample_step = max(1, total_trades // 500)

        # Baseline starting point
        curve_points.append(DownsampledPoint(
            trade_id=0,
            time_utc=rows[0].get("open_time", ""),
            balance_usdt=round(initial_balance, 4),
            roi_pct=0.0,
            drawdown_pct=0.0,
            cum_pnl_usdt=0.0
        ))

        for idx, row in enumerate(rows):
            t_id = int(row.get("trade_id", idx + 1))
            pnl = float(row.get("realized_pnl_usdt", 0.0))
            roe = float(row.get("roe_percentage", 0.0))
            dur = float(row.get("duration_seconds", 0.0))
            close_time = row.get("close_time", "")
            bal = float(row.get("balance_after_trade_usdt", curr_balance + pnl))
            curr_balance = bal

            if curr_balance > peak_balance:
                peak_balance = curr_balance
            
            dd_pct = ((peak_balance - curr_balance) / peak_balance * 100.0) if peak_balance > 0 else 0.0
            roi_pct = ((curr_balance - initial_balance) / initial_balance * 100.0) if initial_balance > 0 else 0.0
            cum_pnl = curr_balance - initial_balance

            # Duration binning
            if dur < 10:
                bin_k = "<10s"
            elif dur < 60:
                bin_k = "10s-1m"
            elif dur < 300:
                bin_k = "1m-5m"
            elif dur < 900:
                bin_k = "5m-15m"
            elif dur < 3600:
                bin_k = "15m-1h"
            else:
                bin_k = ">1h"

            duration_bins[bin_k]["count"] += 1
            if pnl > 0:
                duration_bins[bin_k]["wins"] += 1
            duration_bins[bin_k]["pnl"] = round(duration_bins[bin_k]["pnl"] + pnl, 4)

            # Time distribution
            try:
                # Format: 2026-01-01 00:32:59 UTC
                dt_str = close_time.replace(" UTC", "").strip()
                dt = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                h = dt.hour
                hourly_bins[h]["trades"] += 1
                if pnl > 0:
                    hourly_bins[h]["wins"] += 1
                hourly_bins[h]["pnl"] = round(hourly_bins[h]["pnl"] + pnl, 4)

                dow = dt.weekday()
                dow_bins[dow]["trades"] += 1
                if pnl > 0:
                    dow_bins[dow]["wins"] += 1
                dow_bins[dow]["pnl"] = round(dow_bins[dow]["pnl"] + pnl, 4)
            except Exception:
                pass

            # Quantile sampling (sample 1000 items)
            if idx % max(1, total_trades // 1000) == 0:
                pnl_sample.append(pnl)
                roe_sample.append(roe)

            # Sample equity curve point
            if idx % sample_step == 0 or idx == total_trades - 1:
                curve_points.append(DownsampledPoint(
                    trade_id=t_id,
                    time_utc=close_time,
                    balance_usdt=round(curr_balance, 4),
                    roi_pct=round(roi_pct, 2),
                    drawdown_pct=round(dd_pct, 2),
                    cum_pnl_usdt=round(cum_pnl, 4)
                ))

        # Compute win rates for duration bins
        for k, v in duration_bins.items():
            cnt = v["count"]
            v["win_rate_pct"] = round((v["wins"] / cnt * 100.0), 1) if cnt > 0 else 0.0

        for hb in hourly_bins:
            cnt = hb["trades"]
            hb["win_rate_pct"] = round((hb["wins"] / cnt * 100.0), 1) if cnt > 0 else 0.0

        for db in dow_bins:
            cnt = db["trades"]
            db["win_rate_pct"] = round((db["wins"] / cnt * 100.0), 1) if cnt > 0 else 0.0

        pnl_sample.sort()
        roe_sample.sort()

        def get_quantiles(arr):
            if not arr:
                return {}
            n = len(arr)
            return {
                "min": round(arr[0], 4),
                "p10": round(arr[int(n * 0.10)], 4),
                "p25": round(arr[int(n * 0.25)], 4),
                "median": round(arr[int(n * 0.50)], 4),
                "p75": round(arr[int(n * 0.75)], 4),
                "p90": round(arr[int(n * 0.90)], 4),
                "max": round(arr[-1], 4),
            }

        details = DetailedAnalytics(
            duration_buckets=duration_bins,
            hourly_distribution=hourly_bins,
            day_of_week_distribution=dow_bins,
            pnl_distribution=get_quantiles(pnl_sample),
            roe_distribution=get_quantiles(roe_sample)
        )

        # Cache curve & details
        try:
            with open(target_curve_file, "w", encoding="utf-8") as f:
                json.dump([p.to_dict() for p in curve_points], f)
            with open(target_details_file, "w", encoding="utf-8") as f:
                json.dump(details.to_dict(), f)
        except Exception as e:
            print(f"[!] Warning: Could not cache curve/details: {e}")

        return curve_points
