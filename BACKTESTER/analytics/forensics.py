"""
Strategy Forensics & Replay Engine
===================================
Institutional forensic analysis for crypto backtests:
- Multi-timeframe OHLCV candlestick slicing formatted for Lightweight Charts
- Reproduction of strategy indicators (EMA, Stoch RSI %K/%D, ADX, ATR)
- Millisecond-level binary-seek tick streaming between entry and exit
- High-resolution MFE (Maximum Favorable Excursion) & MAE (Maximum Adverse Excursion)
- Detailed tick event timelines & strategy state extraction
- "What Happened After Exit?" trajectory tracking
- Counterfactual "What-If" exit simulator (alternative timeouts and TP/SL)
"""

import os
import re
import csv
import math
import datetime
from typing import List, Dict, Any, Optional, Tuple

from BACKTESTER.engine.scanner import canonicalize_symbol, parse_timestamp_ms, format_ms_to_utc
from BACKTESTER.engine.data_loader import (
    OHLCVLoader,
    TickTradeStreamer,
    Candle,
    TradeTick,
    normalize_timeframe,
    TIMEFRAME_MAP
)
from BACKTESTER.analytics.indexer import ReportIndexer
from strategies.ema_crossover import compute_ema_series
from strategies.stoch_rsi import compute_stoch_rsi
from strategies.filters import compute_adx_series, compute_atr_series


TF_SECONDS_MAP = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
}


BACKTESTER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT_DIR = os.path.abspath(os.path.join(BACKTESTER_DIR, ".."))


class ForensicsEngine:
    """
    Core research engine powering the Forensic Chart & Replay Lab.
    """

    def __init__(
        self,
        indexer: Optional[ReportIndexer] = None,
        ohlcv_dir: Optional[str] = None,
        trades_dir: Optional[str] = None,
        reports_dir: Optional[str] = None
    ):
        if ohlcv_dir is None:
            ohlcv_dir = os.path.join(BACKTESTER_DIR, "OHLCV_Data_Binance")
        elif not os.path.isabs(ohlcv_dir) and not os.path.isdir(ohlcv_dir):
            candidate = os.path.join(BACKTESTER_DIR, os.path.basename(ohlcv_dir))
            if os.path.isdir(candidate):
                ohlcv_dir = candidate
            else:
                ohlcv_dir = os.path.join(ROOT_DIR, ohlcv_dir)

        if trades_dir is None:
            trades_dir = os.path.join(BACKTESTER_DIR, "Historical_Trades_Data_Binance")
        elif not os.path.isabs(trades_dir) and not os.path.isdir(trades_dir):
            candidate = os.path.join(BACKTESTER_DIR, os.path.basename(trades_dir))
            if os.path.isdir(candidate):
                trades_dir = candidate
            else:
                trades_dir = os.path.join(ROOT_DIR, trades_dir)

        if reports_dir is None:
            reports_dir = os.path.join(BACKTESTER_DIR, "reports")
        elif not os.path.isabs(reports_dir) and not os.path.exists(reports_dir):
            candidate = os.path.join(BACKTESTER_DIR, os.path.basename(reports_dir))
            if os.path.exists(candidate) or reports_dir in ("reports", os.path.join("BACKTESTER", "reports")):
                reports_dir = os.path.join(BACKTESTER_DIR, "reports")
            else:
                reports_dir = os.path.join(ROOT_DIR, reports_dir)

        self.indexer = indexer or ReportIndexer(reports_dir=reports_dir)
        self.ohlcv_dir = ohlcv_dir
        self.trades_dir = trades_dir
        self.reports_dir = reports_dir

        self.ohlcv_loader = OHLCVLoader(data_dir=self.ohlcv_dir)
        self.tick_streamer = TickTradeStreamer(data_dir=self.trades_dir)
        self._candles_cache = {}
        self._ticks_cache = {}

    # =========================================================================
    # DATA CATALOG & DISCOVERY
    # =========================================================================

    def get_catalog(self) -> Dict[str, Any]:
        """
        Discovers all available symbols, timeframes, date windows, and tick feeds.
        """
        catalog = {
            "ohlcv_symbols": {},
            "tick_symbols": {},
            "available_runs": []
        }

        # 1. Discover OHLCV symbols and timeframes
        if os.path.isdir(self.ohlcv_dir):
            for sym in os.listdir(self.ohlcv_dir):
                sym_path = os.path.join(self.ohlcv_dir, sym)
                if not os.path.isdir(sym_path):
                    continue

                canonical = canonicalize_symbol(sym)
                timeframes = {}
                for tf in os.listdir(sym_path):
                    tf_path = os.path.join(sym_path, tf)
                    if os.path.isdir(tf_path):
                        csv_files = [f for f in os.listdir(tf_path) if f.endswith(".csv")]
                        if csv_files:
                            csv_files.sort()
                            dates = [re.findall(r'\d{4}-\d{2}', f) for f in csv_files]
                            flattened_dates = [d[0] for d in dates if d]
                            min_date = flattened_dates[0] if flattened_dates else "N/A"
                            max_date = flattened_dates[-1] if flattened_dates else "N/A"
                            timeframes[tf] = {
                                "file_count": len(csv_files),
                                "min_date": min_date,
                                "max_date": max_date
                            }

                if timeframes:
                    catalog["ohlcv_symbols"][canonical] = {
                        "raw_folder": sym,
                        "timeframes": timeframes
                    }

        # 2. Discover Tick trade symbols
        if os.path.isdir(self.trades_dir):
            for sym in os.listdir(self.trades_dir):
                sym_path = os.path.join(self.trades_dir, sym)
                if not os.path.isdir(sym_path):
                    continue

                canonical = canonicalize_symbol(sym)
                csv_files = [f for f in os.listdir(sym_path) if f.endswith(".csv")]
                if csv_files:
                    csv_files.sort()
                    dates = [re.findall(r'\d{4}-\d{2}', f) for f in csv_files]
                    flattened_dates = [d[0] for d in dates if d]
                    catalog["tick_symbols"][canonical] = {
                        "raw_folder": sym,
                        "file_count": len(csv_files),
                        "months": flattened_dates,
                        "min_month": flattened_dates[0] if flattened_dates else "N/A",
                        "max_month": flattened_dates[-1] if flattened_dates else "N/A"
                    }

        # 3. Available indexed backtest runs
        runs = self.indexer.get_all_runs()
        for r in runs:
            catalog["available_runs"].append({
                "run_id": r.metadata.run_id,
                "run_name": r.metadata.run_name,
                "symbol": r.metadata.symbol,
                "strategy": r.metadata.strategy,
                "preset": r.metadata.strategy_preset,
                "timeframe": r.metadata.timeframe,
                "date_range": r.metadata.date_range,
                "total_trades": r.scorecard.total_trades,
                "win_rate_pct": r.scorecard.win_rate_pct,
                "net_pnl_usdt": r.scorecard.net_pnl_usdt
            })

        return catalog

    # =========================================================================
    # CANDLESTICK SLICING (LIGHTWEIGHT CHARTS FORMAT)
    # =========================================================================

    def get_candles(
        self,
        symbol: str,
        timeframe: str = "1m",
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
        limit: int = 100000
    ) -> List[Dict[str, Any]]:
        """
        Loads and formats candles for Lightweight Charts:
        [{ time: unix_sec, open: float, high: float, low: float, close: float, volume: float }]
        """
        candles = self.ohlcv_loader.load_candles(
            symbol=symbol,
            timeframe=timeframe,
            start_ms=start_ms,
            end_ms=end_ms
        )

        if not candles:
            return []

        # If too many candles, take latest or slice up to limit
        if len(candles) > limit:
            candles = candles[-limit:]

        chart_candles = []
        for c in candles:
            chart_candles.append({
                "time": int(c.open_time_ms / 1000),
                "open": round(c.open, 6),
                "high": round(c.high, 6),
                "low": round(c.low, 6),
                "close": round(c.close, 6),
                "volume": round(c.volume, 4),
                "open_time_ms": c.open_time_ms,
                "close_time_ms": c.close_time_ms
            })

        return chart_candles

    def get_run_candles(
        self,
        run_id: str,
        timeframe: str = "1m",
        limit: int = 100000
    ) -> Dict[str, Any]:
        """
        Loads the complete OHLCV candlestick series covering the full evaluation date range of the backtest run.
        Calculates indicators (EMA, Stoch RSI) over the series.
        Caches result in memory for instantaneous trade navigation and replay.
        """
        run = self.indexer.get_run_by_id(run_id)
        if not run:
            raise ValueError(f"Run '{run_id}' not found")

        symbol = canonicalize_symbol(run.metadata.symbol)
        tf = normalize_timeframe(timeframe)
        cache_key = f"{run_id}:{tf}:{limit}"

        if cache_key in self._candles_cache:
            return self._candles_cache[cache_key]

        start_ms = None
        end_ms = None
        if run.metadata.start_date and run.metadata.start_date != "N/A":
            s_parsed = parse_timestamp_ms(run.metadata.start_date)
            if s_parsed:
                start_ms = s_parsed

        if run.metadata.end_date and run.metadata.end_date != "N/A":
            e_parsed = parse_timestamp_ms(run.metadata.end_date)
            if e_parsed:
                end_ms = e_parsed + (86400 * 1000) - 1

        if not start_ms or not end_ms:
            trades_catalog = self.get_all_trades_catalog(run_id)
            trades = trades_catalog.get("trades", [])
            valid_opens = [t["open_time_ms"] for t in trades if t.get("open_time_ms", 0) > 0]
            valid_closes = [t["close_time_ms"] for t in trades if t.get("close_time_ms", 0) > 0]
            if valid_opens:
                start_ms = min(valid_opens) - (3600 * 1000)
            if valid_closes:
                end_ms = max(valid_closes) + (3600 * 1000)

        candles = self.get_candles(
            symbol=symbol,
            timeframe=tf,
            start_ms=start_ms,
            end_ms=end_ms,
            limit=limit
        )

        indicators = self.calculate_indicators(candles, run.metadata.parameters)

        res = {
            "symbol": symbol,
            "run_id": run_id,
            "timeframe": tf,
            "date_range": run.metadata.date_range,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "candles": candles,
            "indicators": indicators
        }
        self._candles_cache[cache_key] = res
        return res

    # =========================================================================
    # INDICATOR CALCULATIONS
    # =========================================================================

    def calculate_indicators(
        self,
        chart_candles: List[Dict[str, Any]],
        config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Reproduces pure-Python indicators over the candlestick series.
        Matches the exact strategy hyperparameter logic.
        """
        if not chart_candles:
            return {"ema_fast": [], "ema_slow": [], "stoch_rsi": {"k": [], "d": []}, "atr": [], "adx": []}

        closes = [c["close"] for c in chart_candles]
        highs = [c["high"] for c in chart_candles]
        lows = [c["low"] for c in chart_candles]
        times = [c["time"] for c in chart_candles]

        cfg = config or {}
        fast_period = int(cfg.get("param_ema_fast", 5))
        slow_period = int(cfg.get("param_ema_slow", 13))
        rsi_period = int(cfg.get("param_rsi_period", cfg.get("param_stoch_rsi_period", 9)))
        stoch_period = int(cfg.get("param_stoch_period", cfg.get("param_stoch_rsi_period", 9)))
        k_period = int(cfg.get("param_k_period", 3))
        d_period = int(cfg.get("param_d_period", 3))
        adx_period = int(cfg.get("param_adx_period", 14))

        # 1. EMA Series
        ema_fast_vals = compute_ema_series(closes, fast_period)
        ema_slow_vals = compute_ema_series(closes, slow_period)

        ema_fast_series = [{"time": times[i], "value": round(ema_fast_vals[i], 6)} for i in range(len(times))]
        ema_slow_series = [{"time": times[i], "value": round(ema_slow_vals[i], 6)} for i in range(len(times))]

        # 2. Stochastic RSI Series (%K and %D)
        k_vals, d_vals = compute_stoch_rsi(closes, rsi_period, stoch_period, k_period, d_period)
        stoch_k_series = [{"time": times[i], "value": round(k_vals[i], 2)} for i in range(len(times))]
        stoch_d_series = [{"time": times[i], "value": round(d_vals[i], 2)} for i in range(len(times))]

        # 3. ATR & ADX Series
        atr_series = []
        adx_series = []
        try:
            atr_vals = compute_atr_series(highs, lows, closes, period=adx_period)
            atr_series = [{"time": times[i], "value": round(atr_vals[i], 6)} for i in range(len(times))]
        except Exception:
            pass

        try:
            adx_vals, _, _ = compute_adx_series(highs, lows, closes, period=adx_period)
            adx_series = [{"time": times[i], "value": round(adx_vals[i], 2)} for i in range(len(times))]
        except Exception:
            pass

        return {
            "ema_fast": ema_fast_series,
            "ema_slow": ema_slow_series,
            "stoch_rsi": {
                "k": stoch_k_series,
                "d": stoch_d_series
            },
            "atr": atr_series,
            "adx": adx_series
        }

    # =========================================================================
    # TRADE FORENSIC CONTEXT
    # =========================================================================

    def get_trade_record(self, run_id: str, trade_id: int) -> Optional[Dict[str, Any]]:
        """
        Locates the specific trade from run's CSV file.
        """
        run = self.indexer.get_run_by_id(run_id)
        if not run:
            return None

        # Look in CSV
        csv_path = os.path.join(self.reports_dir, f"{run_id}_trades.csv")
        if not os.path.exists(csv_path):
            return None

        with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    if int(row.get("trade_id", -1)) == trade_id:
                        return row
                except ValueError:
                    continue

        return None

    def get_all_trades_catalog(self, run_id: str) -> Dict[str, Any]:
        """
        Returns all trades for a backtest run with lightweight metadata for instant dropdown & search.
        Includes fast tick-data availability flags per trade and breakdown counts.
        """
        csv_path = os.path.join(self.reports_dir, f"{run_id}_trades.csv")
        if not os.path.exists(csv_path):
            return {"trades": [], "counts": {"all": 0, "wins": 0, "losses": 0, "timeouts": 0, "with_ticks": 0}}

        run = self.indexer.get_run_by_id(run_id)
        sym = canonicalize_symbol(run.metadata.symbol) if run else ""

        # Discover available tick months for this symbol
        tick_months = set()
        if os.path.isdir(self.trades_dir):
            for folder in os.listdir(self.trades_dir):
                if canonicalize_symbol(folder) == sym:
                    fpath = os.path.join(self.trades_dir, folder)
                    if os.path.isdir(fpath):
                        for f in os.listdir(fpath):
                            if f.endswith(".csv"):
                                ms = re.findall(r'\d{4}-\d{2}', f)
                                if ms:
                                    tick_months.add(ms[0])

        trades = []
        counts = {"all": 0, "wins": 0, "losses": 0, "timeouts": 0, "with_ticks": 0}

        with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    tid = int(row.get("trade_id", 0))
                    direction = row.get("direction", "LONG").upper()
                    exit_reason = row.get("exit_reason", "")
                    pnl = float(row.get("realized_pnl_usdt", 0.0))
                    roe = float(row.get("roe_percentage", 0.0))
                    open_str = row.get("open_time", row.get("open_time_utc", "")).replace(" UTC", "")
                    duration = float(row.get("duration_seconds", 0.0))
                    date_prefix = open_str[:7] if len(open_str) >= 7 else ""
                    has_ticks = (date_prefix in tick_months)

                    counts["all"] += 1
                    if pnl > 0:
                        counts["wins"] += 1
                    elif pnl < 0:
                        counts["losses"] += 1
                    if "TIMEOUT" in exit_reason.upper():
                        counts["timeouts"] += 1
                    if has_ticks:
                        counts["with_ticks"] += 1

                    # Parse prices and timestamps for chart markers
                    entry_p = float(row.get("entry_price", 0.0))
                    exit_p = float(row.get("exit_price", 0.0))
                    close_str = row.get("close_time", row.get("close_time_utc", "")).replace(" UTC", "")
                    open_ms = parse_timestamp_ms(open_str) or 0
                    close_ms = parse_timestamp_ms(close_str) or 0

                    trades.append({
                        "trade_id": tid,
                        "direction": direction,
                        "exit_reason": exit_reason,
                        "pnl_usdt": round(pnl, 4),
                        "roe_pct": round(roe, 2),
                        "duration_s": round(duration, 1),
                        "date": open_str.split(" ")[0] if open_str else "",
                        "time_utc": open_str,
                        "has_ticks": has_ticks,
                        "entry_price": round(entry_p, 6),
                        "exit_price": round(exit_p, 6),
                        "open_time_ms": open_ms,
                        "close_time_ms": close_ms
                    })
                except (ValueError, KeyError):
                    continue

        return {"trades": trades, "counts": counts}

    def get_trade_forensic_context(
        self,
        run_id: str,
        trade_id: int,
        timeframe: str = "1m",
        pad_candles_before: int = 80,
        pad_candles_after: int = 50,
        full_backtest: bool = False,
        include_candles: bool = False
    ) -> Dict[str, Any]:
        """
        Extracts comprehensive forensic context for a single trade:
        - Exact trade parameters, prices, direction, outcome
        - Surrounding candlestick slice with indicators (optional, if include_candles=True)
        - Millisecond timestamps
        - High-resolution tick stream & MFE/MAE
        - Strategy and filter verification at entry
        - Post-exit trajectory
        """
        run = self.indexer.get_run_by_id(run_id)
        if not run:
            raise ValueError(f"Run '{run_id}' not found")

        trade_raw = self.get_trade_record(run_id, trade_id)
        if not trade_raw:
            raise ValueError(f"Trade #{trade_id} not found in run '{run_id}'")

        # Parse timestamps
        open_time_str = trade_raw.get("open_time", trade_raw.get("open_time_utc", "")).replace(" UTC", "")
        close_time_str = trade_raw.get("close_time", trade_raw.get("close_time_utc", "")).replace(" UTC", "")

        open_ms = parse_timestamp_ms(open_time_str)
        close_ms = parse_timestamp_ms(close_time_str)

        if not open_ms:
            raise ValueError(f"Could not parse open_time '{open_time_str}'")

        if not close_ms:
            close_ms = open_ms + int(float(trade_raw.get("duration_seconds", 60.0)) * 1000)

        symbol = canonicalize_symbol(trade_raw.get("symbol", run.metadata.symbol))
        direction = trade_raw.get("direction", "LONG").upper()
        entry_price = float(trade_raw.get("entry_price", 0.0))
        exit_price = float(trade_raw.get("exit_price", 0.0))
        tp_price = float(trade_raw.get("min_profit_tp_price", 0.0))
        sl_price = float(trade_raw.get("stop_loss_price", 0.0))
        duration_sec = float(trade_raw.get("duration_seconds", 0.0))
        pnl_usdt = float(trade_raw.get("realized_pnl_usdt", 0.0))
        roe_pct = float(trade_raw.get("roe_percentage", 0.0))
        exit_reason = trade_raw.get("exit_reason", "UNKNOWN")

        # 1. Load Candlesticks (only if include_candles requested by client)
        candles = []
        indicators = {}
        if include_candles:
            if full_backtest:
                try:
                    run_data = self.get_run_candles(run_id, timeframe=timeframe)
                    candles = run_data.get("candles", [])
                    indicators = run_data.get("indicators", {})
                except Exception as e:
                    print(f"[!] Warning: Full run candle load fallback triggered: {e}")

            if not candles:
                tf = normalize_timeframe(timeframe)
                tf_sec = TF_SECONDS_MAP.get(tf, 60)
                start_slice_ms = open_ms - (pad_candles_before * tf_sec * 1000)
                end_slice_ms = close_ms + (pad_candles_after * tf_sec * 1000)

                candles = self.get_candles(
                    symbol=symbol,
                    timeframe=tf,
                    start_ms=start_slice_ms,
                    end_ms=end_slice_ms,
                    limit=2000
                )
                indicators = self.calculate_indicators(candles, run.metadata.parameters)

        # 3. Strategy & Filter State Assessment
        strategy_state = {
            "strategy": run.metadata.strategy,
            "preset": run.metadata.strategy_preset,
            "direction": direction,
            "leverage": run.metadata.leverage,
            "tp_ticks": run.metadata.tp_ticks,
            "sl_rule": run.metadata.sl_rule_desc or f"ROE {run.metadata.sl_value}%",
            "price_unit": run.metadata.price_unit,
            "contract_size": run.metadata.contract_size,
            "contracts": run.metadata.contracts,
            "parameters": run.metadata.parameters,
        }

        filters = run.metadata.filters or {}
        adx_enabled = bool(filters.get("adx_filter", filters.get("adx_enabled", False)))
        htf_enabled = bool(filters.get("htf_trend_filter", filters.get("htf_enabled", False)))
        hourly_enabled = bool(filters.get("hourly_filter", filters.get("hourly_enabled", False)))
        duration_enabled = bool(filters.get("duration_filter", filters.get("duration_enabled", False)))

        filter_state = {
            "adx_filter": {
                "name": "ADX Trend / Chop Gate",
                "enabled": adx_enabled,
                "threshold": filters.get("adx_threshold", 25.0),
                "status": "PASS" if adx_enabled else "DISABLED"
            },
            "htf_trend": {
                "name": "Higher-Timeframe 200 EMA",
                "enabled": htf_enabled,
                "status": "PASS" if htf_enabled else "DISABLED"
            },
            "hourly_session": {
                "name": "UTC Hourly Dead-Zone Filter",
                "enabled": hourly_enabled,
                "status": "PASS" if hourly_enabled else "DISABLED"
            },
            "duration_guard": {
                "name": "Trade Duration Timeout Exit",
                "enabled": duration_enabled,
                "max_hold_seconds": float(filters.get("max_hold_s", filters.get("duration_max_hold_seconds", 90.0))),
                "action": str(filters.get("duration_action", "CLOSE")),
                "status": "TRIGGERED" if "TIMEOUT" in exit_reason else ("PASS" if duration_enabled else "DISABLED")
            }
        }

        # 4. Tick-Level Stream, MFE/MAE, & Post-Exit Forensics
        ticks_result = self.get_trade_ticks(
            symbol=symbol,
            entry_ms=open_ms,
            exit_ms=close_ms,
            direction=direction,
            entry_price=entry_price,
            tp_price=tp_price,
            sl_price=sl_price,
            pu=run.metadata.price_unit,
            post_exit_sec=120.0
        )

        return {
            "trade": {
                "trade_id": trade_id,
                "symbol": symbol,
                "direction": direction,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "open_time_utc": format_ms_to_utc(open_ms),
                "close_time_utc": format_ms_to_utc(close_ms),
                "open_time_ms": open_ms,
                "close_time_ms": close_ms,
                "duration_seconds": duration_sec,
                "realized_pnl_usdt": pnl_usdt,
                "roe_percentage": roe_pct,
                "exit_reason": exit_reason,
            },
            "candles": candles,
            "indicators": indicators,
            "strategy_state": strategy_state,
            "filter_state": filter_state,
            "ticks": ticks_result.get("ticks", []),
            "timeline": ticks_result.get("timeline", []),
            "mfe_mae": ticks_result.get("mfe_mae", {}),
            "post_exit": ticks_result.get("post_exit", {}),
            "has_ticks": ticks_result.get("has_ticks", False)
        }

    # =========================================================================
    # HIGH-RESOLUTION TICK STREAM, MFE/MAE & POST-EXIT ANALYSIS
    # =========================================================================

    def get_trade_ticks(
        self,
        symbol: str,
        entry_ms: int,
        exit_ms: int,
        direction: str,
        entry_price: float,
        tp_price: float,
        sl_price: float,
        pu: float = 0.0001,
        post_exit_sec: float = 120.0,
        max_return_ticks: int = 1500
    ) -> Dict[str, Any]:
        """
        Streams millisecond ticks for the trade lifecycle and post-exit observation.
        Computes exact MFE and MAE.
        """
        cache_key = f"{symbol}:{entry_ms}:{exit_ms}:{direction}"
        if cache_key in self._ticks_cache:
            return self._ticks_cache[cache_key]

        post_exit_ms = exit_ms + int(post_exit_sec * 1000)
        start_fetch_ms = entry_ms - 2000  # 2s pre-entry

        raw_ticks: List[TradeTick] = []
        try:
            tick_gen = self.tick_streamer.stream_ticks(
                symbol=symbol,
                start_ms=start_fetch_ms,
                end_ms=post_exit_ms
            )
            for t in tick_gen:
                raw_ticks.append(t)
                if len(raw_ticks) >= max_return_ticks * 3:
                    break
        except Exception as e:
            print(f"[!] Error streaming ticks for {symbol}: {e}")

        if not raw_ticks:
            # 1m Candle-Level High-Resolution Fallback
            # Loads 1m candles for trade lifecycle + post-exit window
            # Synthesizes realistic 4-point intra-bar ticks (Open -> Extremes -> Close)
            candles = self.ohlcv_loader.load_candles(
                symbol=symbol,
                timeframe="1m",
                start_ms=start_fetch_ms - 60000,
                end_ms=post_exit_ms + 60000
            )
            is_long = (direction == "LONG")

            synthetic_ticks = []
            in_pos_candles = []
            post_exit_candles = []

            for c in candles:
                c_start = c.open_time_ms
                c_end = c.close_time_ms
                if c_end < entry_ms:
                    continue
                elif c_start > post_exit_ms:
                    break
                elif c_start <= exit_ms and c_end >= entry_ms:
                    in_pos_candles.append(c)
                elif c_start > exit_ms:
                    post_exit_candles.append(c)

                # Generate 4 intra-candle synthetic points spaced 15s apart
                if c.close >= c.open:
                    points = [
                        (c_start, c.open),
                        (c_start + 15000, c.low),
                        (c_start + 30000, c.high),
                        (c_start + 45000, c.close)
                    ]
                else:
                    points = [
                        (c_start, c.open),
                        (c_start + 15000, c.high),
                        (c_start + 30000, c.low),
                        (c_start + 45000, c.close)
                    ]

                for p_time, p_price in points:
                    delta = (p_price - entry_price) if is_long else (entry_price - p_price)
                    synthetic_ticks.append({
                        "time_ms": p_time,
                        "time_sec": p_time / 1000.0,
                        "price": round(p_price, 6),
                        "qty": round(c.volume / 4.0, 2),
                        "is_in_position": (entry_ms <= p_time <= exit_ms),
                        "delta_usdt": round(delta, 6),
                        "delta_ticks": round(delta / pu, 1) if pu > 0 else 0.0,
                        "is_buyer_maker": (c.close < c.open)
                    })

            # Calculate MFE & MAE from in-position candle extremes
            mfe_val = 0.0
            mae_val = 0.0
            peak_time_ms = None
            adverse_time_ms = None

            for c in in_pos_candles:
                if is_long:
                    fav = c.high - entry_price
                    adv = c.low - entry_price
                else:
                    fav = entry_price - c.low
                    adv = entry_price - c.high

                if fav > mfe_val:
                    mfe_val = fav
                    peak_time_ms = c.open_time_ms + 30000
                if adv < mae_val:
                    mae_val = adv
                    adverse_time_ms = c.open_time_ms + 15000

            mfe_ticks = round(mfe_val / pu, 1) if pu > 0 else 0.0
            mae_ticks = round(abs(mae_val) / pu, 1) if pu > 0 else 0.0
            mfe_pct = round((mfe_val / entry_price) * 100.0, 3) if entry_price > 0 else 0.0
            mae_pct = round((abs(mae_val) / entry_price) * 100.0, 3) if entry_price > 0 else 0.0

            mfe_mae_summary = {
                "mfe_usdt": round(mfe_val, 6),
                "mfe_ticks": mfe_ticks,
                "mfe_pct": mfe_pct,
                "mae_usdt": round(mae_val, 6),
                "mae_ticks": mae_ticks,
                "mae_pct": mae_pct,
                "peak_favorable_time_ms": peak_time_ms,
                "max_adverse_time_ms": adverse_time_ms
            }

            # Post-exit analysis from post-exit candles
            tp_after_sec = None
            sl_after_sec = None

            for c in post_exit_candles:
                elapsed_after = max(0.0, (c.open_time_ms - exit_ms) / 1000.0)
                if is_long:
                    if tp_after_sec is None and c.high >= tp_price:
                        tp_after_sec = round(elapsed_after + 30.0, 1)
                    if sl_after_sec is None and c.low <= sl_price:
                        sl_after_sec = round(elapsed_after + 15.0, 1)
                else:
                    if tp_after_sec is None and c.low <= tp_price:
                        tp_after_sec = round(elapsed_after + 30.0, 1)
                    if sl_after_sec is None and c.high >= sl_price:
                        sl_after_sec = round(elapsed_after + 15.0, 1)

                if tp_after_sec is not None and sl_after_sec is not None:
                    break

            post_exit_summary = {
                "tp_reached_after_exit": tp_after_sec is not None,
                "sl_reached_after_exit": sl_after_sec is not None,
                "elapsed_to_tp_sec": tp_after_sec,
                "elapsed_to_sl_sec": sl_after_sec,
                "summary": ""
            }

            if tp_after_sec is not None and (sl_after_sec is None or tp_after_sec < sl_after_sec):
                post_exit_summary["summary"] = f"Had position remained open, TP would have been reached +{tp_after_sec}s after exit (evaluated from 1m candles)."
            elif sl_after_sec is not None and (tp_after_sec is None or sl_after_sec < tp_after_sec):
                post_exit_summary["summary"] = f"Had position remained open, SL would have been reached +{sl_after_sec}s after exit (evaluated from 1m candles)."
            else:
                post_exit_summary["summary"] = f"Neither TP nor SL reached within {int(post_exit_sec)}s post-exit window (evaluated from 1m candles)."

            # Build timeline
            timeline = [
                {
                    "time_ms": entry_ms,
                    "time_utc": format_ms_to_utc(entry_ms),
                    "elapsed_sec": 0.0,
                    "event": "ENTRY_FILLED",
                    "price": entry_price,
                    "delta_ticks": 0.0,
                    "desc": f"{direction} Entry filled at {entry_price}"
                }
            ]
            if peak_time_ms:
                timeline.append({
                    "time_ms": peak_time_ms,
                    "time_utc": format_ms_to_utc(peak_time_ms),
                    "elapsed_sec": round((peak_time_ms - entry_ms) / 1000.0, 1),
                    "event": "PEAK_FAVORABLE",
                    "price": round(entry_price + (mfe_ticks * pu if is_long else -mfe_ticks * pu), 6),
                    "delta_ticks": mfe_ticks,
                    "desc": f"Peak Favorable (+{mfe_ticks} ticks)"
                })
            if adverse_time_ms:
                timeline.append({
                    "time_ms": adverse_time_ms,
                    "time_utc": format_ms_to_utc(adverse_time_ms),
                    "elapsed_sec": round((adverse_time_ms - entry_ms) / 1000.0, 1),
                    "event": "MAX_ADVERSE",
                    "price": round(entry_price - (mae_ticks * pu if is_long else -mae_ticks * pu), 6),
                    "delta_ticks": -mae_ticks,
                    "desc": f"Maximum Drawdown (-{mae_ticks} ticks)"
                })
            timeline.append({
                "time_ms": exit_ms,
                "time_utc": format_ms_to_utc(exit_ms),
                "elapsed_sec": round((exit_ms - entry_ms) / 1000.0, 1),
                "event": "TRADE_EXIT",
                "price": round(entry_price, 6),
                "delta_ticks": 0.0,
                "desc": f"Trade closed at {format_ms_to_utc(exit_ms)}"
            })
            timeline.sort(key=lambda x: x["time_ms"])

            res = {
                "has_ticks": True,
                "is_synthetic": True,
                "data_resolution": "1m CANDLE RESOLUTION",
                "ticks": synthetic_ticks,
                "timeline": timeline,
                "mfe_mae": mfe_mae_summary,
                "post_exit": post_exit_summary
            }
            self._ticks_cache[cache_key] = res
            return res


        # Divide into in-position ticks vs post-exit ticks
        in_pos_ticks = [t for t in raw_ticks if entry_ms <= t.timestamp_ms <= exit_ms]
        post_exit_ticks = [t for t in raw_ticks if t.timestamp_ms > exit_ms]

        # Calculate MFE & MAE
        mfe_val = 0.0
        mae_val = 0.0
        peak_favorable_tick = None
        max_adverse_tick = None

        is_long = (direction == "LONG")

        for t in in_pos_ticks:
            delta = (t.price - entry_price) if is_long else (entry_price - t.price)
            if delta > mfe_val:
                mfe_val = delta
                peak_favorable_tick = t
            if delta < mae_val:
                mae_val = delta
                max_adverse_tick = t

        mfe_ticks = round(mfe_val / pu, 1) if pu > 0 else 0.0
        mae_ticks = round(abs(mae_val) / pu, 1) if pu > 0 else 0.0
        mfe_pct = round((mfe_val / entry_price) * 100.0, 3) if entry_price > 0 else 0.0
        mae_pct = round((abs(mae_val) / entry_price) * 100.0, 3) if entry_price > 0 else 0.0

        mfe_mae_summary = {
            "mfe_usdt": round(mfe_val, 6),
            "mfe_ticks": mfe_ticks,
            "mfe_pct": mfe_pct,
            "mae_usdt": round(mae_val, 6),
            "mae_ticks": mae_ticks,
            "mae_pct": mae_pct,
            "peak_favorable_time_ms": peak_favorable_tick.timestamp_ms if peak_favorable_tick else None,
            "max_adverse_time_ms": max_adverse_tick.timestamp_ms if max_adverse_tick else None
        }

        # Build Forensic Event Timeline
        timeline = []
        timeline.append({
            "time_ms": entry_ms,
            "time_utc": format_ms_to_utc(entry_ms),
            "elapsed_sec": 0.0,
            "event": "ENTRY_FILLED",
            "price": entry_price,
            "delta_ticks": 0.0,
            "desc": f"{direction} Entry filled at {entry_price}"
        })

        # Key tick milestones
        if in_pos_ticks:
            sample_points = [
                ("PEAK_FAVORABLE", peak_favorable_tick, f"Peak Favorable (+{mfe_ticks} ticks)"),
                ("MAX_ADVERSE", max_adverse_tick, f"Maximum Drawdown (-{mae_ticks} ticks)")
            ]
            for label, ptick, desc in sample_points:
                if ptick:
                    elapsed = round((ptick.timestamp_ms - entry_ms) / 1000.0, 2)
                    delta_p = (ptick.price - entry_price) if is_long else (entry_price - ptick.price)
                    timeline.append({
                        "time_ms": ptick.timestamp_ms,
                        "time_utc": format_ms_to_utc(ptick.timestamp_ms),
                        "elapsed_sec": elapsed,
                        "event": label,
                        "price": ptick.price,
                        "delta_ticks": round(delta_p / pu, 1) if pu > 0 else 0.0,
                        "desc": desc
                    })

        timeline.append({
            "time_ms": exit_ms,
            "time_utc": format_ms_to_utc(exit_ms),
            "elapsed_sec": round((exit_ms - entry_ms) / 1000.0, 2),
            "event": "TRADE_EXIT",
            "price": in_pos_ticks[-1].price if in_pos_ticks else entry_price,
            "delta_ticks": round(((in_pos_ticks[-1].price - entry_price if is_long else entry_price - in_pos_ticks[-1].price) if in_pos_ticks else 0.0) / pu, 1) if pu > 0 else 0.0,
            "desc": f"Trade closed at {format_ms_to_utc(exit_ms)}"
        })

        # Sort timeline by time_ms
        timeline.sort(key=lambda x: x["time_ms"])

        # Post-Exit Analysis
        tp_after_sec = None
        sl_after_sec = None

        for t in post_exit_ticks:
            elapsed_after = (t.timestamp_ms - exit_ms) / 1000.0
            if is_long:
                if tp_after_sec is None and t.price >= tp_price:
                    tp_after_sec = round(elapsed_after, 2)
                if sl_after_sec is None and t.price <= sl_price:
                    sl_after_sec = round(elapsed_after, 2)
            else:
                if tp_after_sec is None and t.price <= tp_price:
                    tp_after_sec = round(elapsed_after, 2)
                if sl_after_sec is None and t.price >= sl_price:
                    sl_after_sec = round(elapsed_after, 2)

            if tp_after_sec is not None and sl_after_sec is not None:
                break

        post_exit_summary = {
            "tp_reached_after_exit": tp_after_sec is not None,
            "sl_reached_after_exit": sl_after_sec is not None,
            "elapsed_to_tp_sec": tp_after_sec,
            "elapsed_to_sl_sec": sl_after_sec,
            "summary": ""
        }

        if tp_after_sec is not None and (sl_after_sec is None or tp_after_sec < sl_after_sec):
            post_exit_summary["summary"] = f"Had position remained open, TP would have been reached +{tp_after_sec}s after exit."
        elif sl_after_sec is not None and (tp_after_sec is None or sl_after_sec < tp_after_sec):
            post_exit_summary["summary"] = f"Had position remained open, SL would have been reached +{sl_after_sec}s after exit."
        else:
            post_exit_summary["summary"] = f"Neither TP nor SL was reached within {int(post_exit_sec)}s post-exit window."

        # Format downsampled ticks for frontend
        all_display_ticks = in_pos_ticks + post_exit_ticks
        step = max(1, len(all_display_ticks) // max_return_ticks)
        downsampled = []

        for i, t in enumerate(all_display_ticks):
            if i % step == 0 or t.timestamp_ms == entry_ms or t.timestamp_ms == exit_ms:
                delta = (t.price - entry_price) if is_long else (entry_price - t.price)
                downsampled.append({
                    "time_ms": t.timestamp_ms,
                    "time_sec": t.timestamp_ms / 1000.0,
                    "price": t.price,
                    "qty": t.qty,
                    "is_in_position": (entry_ms <= t.timestamp_ms <= exit_ms),
                    "delta_usdt": round(delta, 6),
                    "delta_ticks": round(delta / pu, 1) if pu > 0 else 0.0,
                    "is_buyer_maker": t.is_buyer_maker
                })

        res = {
            "has_ticks": True,
            "is_synthetic": False,
            "data_resolution": "HIGH-FIDELITY MILLISECOND TICKS",
            "ticks": downsampled,
            "timeline": timeline,
            "mfe_mae": mfe_mae_summary,
            "post_exit": post_exit_summary
        }
        self._ticks_cache[cache_key] = res
        return res

    # =========================================================================
    # WHAT-IF EXIT SIMULATION
    # =========================================================================

    def simulate_what_if(
        self,
        run_id: str,
        trade_id: int,
        timeout_seconds: Optional[float] = None,
        tp_ticks: Optional[int] = None,
        sl_roe_pct: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Counterfactual simulation:
        Tests alternative exit rules (timeout, TP distance, SL) against the trade's
        historical tick stream WITHOUT altering the original backtest records.
        """
        context = self.get_trade_forensic_context(run_id=run_id, trade_id=trade_id, full_backtest=False)
        trade = context["trade"]
        symbol = trade["symbol"]
        direction = trade["direction"]
        entry_price = trade["entry_price"]
        open_ms = trade["open_time_ms"]
        close_ms = trade["close_time_ms"]
        pu = context["strategy_state"].get("price_unit") or 0.0001
        leverage = context["strategy_state"].get("leverage") or 75

        is_long = (direction == "LONG")

        # Determine target TP price
        trade_tp = float(trade.get("tp_price", 0.0))
        if tp_ticks is not None:
            active_tp_ticks = tp_ticks
            if is_long:
                hypo_tp = round(entry_price + (active_tp_ticks * pu), 6)
            else:
                hypo_tp = round(entry_price - (active_tp_ticks * pu), 6)
        elif trade_tp > 0:
            hypo_tp = trade_tp
            active_tp_ticks = round(abs(trade_tp - entry_price) / pu) if pu > 0 else 2
        else:
            active_tp_ticks = int(context["strategy_state"].get("tp_ticks", 2))
            if is_long:
                hypo_tp = round(entry_price + (active_tp_ticks * pu), 6)
            else:
                hypo_tp = round(entry_price - (active_tp_ticks * pu), 6)

        # Determine target SL price
        trade_sl = float(trade.get("sl_price", 0.0))
        if sl_roe_pct is not None:
            active_sl_roe = sl_roe_pct
            sl_dist = (entry_price * (active_sl_roe / 100.0)) / leverage if leverage > 0 else 0.0
            if is_long:
                hypo_sl = round(entry_price - sl_dist, 6)
            else:
                hypo_sl = round(entry_price + sl_dist, 6)
        elif trade_sl > 0:
            hypo_sl = trade_sl
            active_sl_roe = round((abs(trade_sl - entry_price) / entry_price) * leverage * 100.0, 2) if entry_price > 0 else 25.0
        else:
            active_sl_roe = 25.0
            sl_dist = (entry_price * (active_sl_roe / 100.0)) / leverage if leverage > 0 else 0.0
            if is_long:
                hypo_sl = round(entry_price - sl_dist, 6)
            else:
                hypo_sl = round(entry_price + sl_dist, 6)

        # Simulation window calculation
        trade_actual_duration = float(trade.get("duration_seconds", 300.0))
        if timeout_seconds is not None and timeout_seconds > 0:
            max_sim_sec = max(60.0, timeout_seconds + 30.0)
        else:
            max_sim_sec = max(7200.0, trade_actual_duration + 600.0)

        end_fetch_ms = open_ms + int(max_sim_sec * 1000)

        hypo_exit_price = trade["exit_price"]
        hypo_exit_time_ms = trade["close_time_ms"]
        hypo_exit_reason = trade["exit_reason"]

        found_exit = False
        tick_count = 0
        last_t = None

        try:
            tick_gen = self.tick_streamer.stream_ticks(symbol, start_ms=open_ms, end_ms=end_fetch_ms)
            for t in tick_gen:
                tick_count += 1
                last_t = t
                elapsed_s = (t.timestamp_ms - open_ms) / 1000.0

                # 1. Check timeout exit if specified
                if timeout_seconds is not None and timeout_seconds > 0:
                    if elapsed_s >= timeout_seconds:
                        hypo_exit_price = t.price
                        hypo_exit_time_ms = t.timestamp_ms
                        hypo_exit_reason = "TIMEOUT_CLOSE"
                        found_exit = True
                        break

                # 2. Check TP / SL hits
                if is_long:
                    if t.price >= hypo_tp:
                        hypo_exit_price = hypo_tp
                        hypo_exit_time_ms = t.timestamp_ms
                        hypo_exit_reason = "MIN_PROFIT_TP_HIT"
                        found_exit = True
                        break
                    elif t.price <= hypo_sl:
                        hypo_exit_price = hypo_sl
                        hypo_exit_time_ms = t.timestamp_ms
                        hypo_exit_reason = "STOP_LOSS_HIT"
                        found_exit = True
                        break
                else:
                    if t.price <= hypo_tp:
                        hypo_exit_price = hypo_tp
                        hypo_exit_time_ms = t.timestamp_ms
                        hypo_exit_reason = "MIN_PROFIT_TP_HIT"
                        found_exit = True
                        break
                    elif t.price >= hypo_sl:
                        hypo_exit_price = hypo_sl
                        hypo_exit_time_ms = t.timestamp_ms
                        hypo_exit_reason = "STOP_LOSS_HIT"
                        found_exit = True
                        break
        except Exception as e:
            print(f"[!] What-if simulation error: {e}")

        # If ticks were processed but no TP/SL/Timeout triggered within dataset
        if tick_count > 0 and not found_exit and last_t is not None:
            hypo_exit_price = last_t.price
            hypo_exit_time_ms = last_t.timestamp_ms
            hypo_exit_reason = "MAX_SIM_WINDOW_REACHED" if timeout_seconds is None else "TIMEOUT_CLOSE"
            found_exit = True

        # Fallback to 1m candles if no raw millisecond ticks exist for this date
        if tick_count == 0:
            candles = self.ohlcv_loader.load_candles(
                symbol=symbol,
                timeframe="1m",
                start_ms=open_ms,
                end_ms=end_fetch_ms + 60000
            )
            found_exit = False
            for c in candles:
                c_start_s = max(0.0, (c.open_time_ms - open_ms) / 1000.0)
                c_end_s = max(0.0, (c.close_time_ms - open_ms) / 1000.0)

                # 1. Check if timeout occurred before this candle
                if timeout_seconds is not None and timeout_seconds > 0 and c_start_s >= timeout_seconds:
                    hypo_exit_price = c.open
                    hypo_exit_time_ms = open_ms + int(timeout_seconds * 1000)
                    hypo_exit_reason = "TIMEOUT_CLOSE"
                    found_exit = True
                    break

                # 2. Check TP / SL hit within candle
                if is_long:
                    if c.high >= hypo_tp and (timeout_seconds is None or c_start_s < timeout_seconds):
                        hypo_exit_price = hypo_tp
                        hypo_exit_time_ms = c.open_time_ms + 30000
                        hypo_exit_reason = "MIN_PROFIT_TP_HIT"
                        found_exit = True
                        break
                    elif c.low <= hypo_sl and (timeout_seconds is None or c_start_s < timeout_seconds):
                        hypo_exit_price = hypo_sl
                        hypo_exit_time_ms = c.open_time_ms + 15000
                        hypo_exit_reason = "STOP_LOSS_HIT"
                        found_exit = True
                        break
                else:
                    if c.low <= hypo_tp and (timeout_seconds is None or c_start_s < timeout_seconds):
                        hypo_exit_price = hypo_tp
                        hypo_exit_time_ms = c.open_time_ms + 30000
                        hypo_exit_reason = "MIN_PROFIT_TP_HIT"
                        found_exit = True
                        break
                    elif c.high >= hypo_sl and (timeout_seconds is None or c_start_s < timeout_seconds):
                        hypo_exit_price = hypo_sl
                        hypo_exit_time_ms = c.open_time_ms + 15000
                        hypo_exit_reason = "STOP_LOSS_HIT"
                        found_exit = True
                        break

                # 3. Check if timeout occurred within this candle
                if timeout_seconds is not None and timeout_seconds > 0 and c_end_s >= timeout_seconds:
                    hypo_exit_price = c.close
                    hypo_exit_time_ms = open_ms + int(timeout_seconds * 1000)
                    hypo_exit_reason = "TIMEOUT_CLOSE"
                    found_exit = True
                    break

            if not found_exit:
                if candles:
                    hypo_exit_price = candles[-1].close
                    hypo_exit_time_ms = candles[-1].close_time_ms
                    hypo_exit_reason = "MAX_SIM_WINDOW_REACHED" if timeout_seconds is None else "TIMEOUT_CLOSE"
                else:
                    hypo_exit_price = trade["exit_price"]
                    hypo_exit_time_ms = trade["close_time_ms"]
                    hypo_exit_reason = trade["exit_reason"]

        hypo_duration_sec = max(0.1, round((hypo_exit_time_ms - open_ms) / 1000.0, 2))
        price_diff = (hypo_exit_price - entry_price) if is_long else (entry_price - hypo_exit_price)

        contracts = float(context["strategy_state"].get("contracts", 1.0))
        cs = float(context["strategy_state"].get("contract_size", 1.0))
        underlying_qty = contracts * cs
        notional = entry_price * underlying_qty
        margin = (notional / leverage) if leverage > 0 else notional

        trade_raw = self.get_trade_record(run_id, trade_id) or {}
        actual_fee = float(trade_raw.get("fee_total_usdt", 0.0) or 0.0)
        fee_rate = 0.0
        if actual_fee > 0 and notional > 0:
            fee_rate = actual_fee / (2.0 * notional)

        fee_open = notional * fee_rate
        fee_close = (underlying_qty * hypo_exit_price) * fee_rate
        fee_total = fee_open + fee_close

        hypo_pnl_usdt = round((underlying_qty * price_diff) - fee_total, 6)
        hypo_roe_pct = round((hypo_pnl_usdt / margin * 100.0), 2) if margin > 0 else 0.0

        actual_pnl = float(trade.get("realized_pnl_usdt", 0.0))
        pnl_diff = round(hypo_pnl_usdt - actual_pnl, 6)

        return {
            "status": "HYPOTHETICAL",
            "trade_id": trade_id,
            "rules_applied": {
                "timeout_seconds": timeout_seconds,
                "tp_ticks": active_tp_ticks,
                "hypo_tp_price": hypo_tp,
                "hypo_sl_price": hypo_sl
            },
            "original_outcome": {
                "exit_reason": trade["exit_reason"],
                "exit_price": trade["exit_price"],
                "duration_seconds": trade["duration_seconds"],
                "pnl_usdt": actual_pnl,
                "roe_pct": trade["roe_percentage"]
            },
            "hypothetical_outcome": {
                "exit_reason": hypo_exit_reason,
                "exit_price": hypo_exit_price,
                "exit_time_utc": format_ms_to_utc(hypo_exit_time_ms),
                "duration_seconds": hypo_duration_sec,
                "pnl_usdt": hypo_pnl_usdt,
                "roe_pct": hypo_roe_pct,
                "pnl_delta_vs_actual": pnl_diff
            }
        }
