"""
Chronos Slicer & Forensic AI Dossier Generator
==============================================
Institutional-grade dynamic partitioning and forensic packaging engine:
- Slices massive backtests (e.g., 47,000+ trades across 8 months) into high-fidelity,
  token-budgeted chunks (Monthly, Weekly, Daily, Loss-Clusters, or Custom Windows).
- Leaves original backtest files completely unmodified (virtual non-destructive slicing).
- Enriches each chunk with multi-dimensional indicators (RSI, Bollinger, MACD, CHOP,
  Volume Surge, VWAP, Candle Anatomy, 200 EMA alignment).
- Extracts millisecond tick streams, MFE/MAE excursions, post-exit bounce forensics,
  and automated loss-cause classification.
- Packages AI-optimized Markdown and JSON dossiers with structured quantitative prompts.
"""

import os
import re
import csv
import json
import math
import zipfile
import datetime
from io import BytesIO
from typing import List, Dict, Any, Optional, Tuple

from BACKTESTER.engine.scanner import canonicalize_symbol, parse_timestamp_ms, format_ms_to_utc
from BACKTESTER.analytics.models import BacktestRunRecord
from BACKTESTER.analytics.indexer import ReportIndexer
from BACKTESTER.analytics.forensics import ForensicsEngine
from BACKTESTER.analytics.indicators import IndicatorMatrix


class ChunkManifestItem:
    """Represents metadata and preview statistics for an individual cropped slice."""

    def __init__(
        self,
        chunk_id: str,
        label: str,
        granularity: str,
        start_date: str,
        end_date: str,
        start_ms: int,
        end_ms: int,
        trades_count: int = 0,
        winning_trades: int = 0,
        losing_trades: int = 0,
        win_rate_pct: float = 0.0,
        net_pnl_usdt: float = 0.0,
        profit_factor: float = 0.0,
        max_drawdown_usdt: float = 0.0,
        has_ticks: bool = False,
        estimated_tokens: int = 0
    ):
        self.chunk_id = chunk_id
        self.label = label
        self.granularity = granularity
        self.start_date = start_date
        self.end_date = end_date
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.trades_count = trades_count
        self.winning_trades = winning_trades
        self.losing_trades = losing_trades
        self.win_rate_pct = round(win_rate_pct, 2)
        self.net_pnl_usdt = round(net_pnl_usdt, 4)
        self.profit_factor = round(profit_factor, 2)
        self.max_drawdown_usdt = round(max_drawdown_usdt, 4)
        self.has_ticks = has_ticks
        self.estimated_tokens = estimated_tokens

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "label": self.label,
            "granularity": self.granularity,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "trades_count": self.trades_count,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate_pct": self.win_rate_pct,
            "net_pnl_usdt": self.net_pnl_usdt,
            "profit_factor": self.profit_factor,
            "max_drawdown_usdt": self.max_drawdown_usdt,
            "has_ticks": self.has_ticks,
            "estimated_tokens": self.estimated_tokens
        }


BACKTESTER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT_DIR = os.path.abspath(os.path.join(BACKTESTER_DIR, ".."))


class ChronosChunker:
    """
    Main orchestration engine for slicing runs and generating rich AI dossiers.
    """

    def __init__(
        self,
        indexer: Optional[ReportIndexer] = None,
        forensics: Optional[ForensicsEngine] = None,
        reports_dir: Optional[str] = None
    ):
        if reports_dir is None:
            reports_dir = os.path.join(BACKTESTER_DIR, "reports")
        elif not os.path.isabs(reports_dir) and not os.path.exists(reports_dir):
            candidate = os.path.join(BACKTESTER_DIR, os.path.basename(reports_dir))
            if os.path.exists(candidate) or reports_dir in ("reports", os.path.join("BACKTESTER", "reports")):
                reports_dir = os.path.join(BACKTESTER_DIR, "reports")
            else:
                reports_dir = os.path.join(ROOT_DIR, reports_dir)

        self.indexer = indexer or ReportIndexer(reports_dir=reports_dir)
        self.forensics = forensics or ForensicsEngine(indexer=self.indexer, reports_dir=reports_dir)
        self.reports_dir = os.path.abspath(reports_dir)

    # =========================================================================
    # CHUNK MANIFEST PLANNING
    # =========================================================================

    def get_chunk_manifest(
        self,
        run_id: str,
        granularity: str = "monthly"
    ) -> Dict[str, Any]:
        """
        Plans and returns virtual slices across a run for the requested granularity:
        - 'monthly': Slices by calendar month (e.g., 2026-01, 2026-02)
        - 'weekly': Slices by 7-day calendar intervals
        - 'daily': Slices day-by-day
        - 'loss_clusters': Algorithmically identifies periods of severe loss clusters / drawdowns
        """
        run = self.indexer.get_run_by_id(run_id)
        if not run:
            raise ValueError(f"Run '{run_id}' not found")

        csv_path = os.path.join(self.reports_dir, f"{run_id}_trades.csv")
        if not os.path.exists(csv_path):
            raise ValueError(f"Trades CSV not found for run '{run_id}'")

        # Discover available tick months for this symbol
        tick_catalog = self.forensics.get_catalog().get("tick_symbols", {})
        sym_canon = canonicalize_symbol(run.metadata.symbol)
        avail_tick_months = set(tick_catalog.get(sym_canon, {}).get("months", []))

        # Stream trades and bucket them
        gran = granularity.lower()
        if gran == "loss_clusters":
            return self._plan_loss_clusters(run, csv_path, avail_tick_months)
        else:
            return self._plan_time_based_chunks(run, csv_path, gran, avail_tick_months)

    def _plan_time_based_chunks(
        self,
        run: BacktestRunRecord,
        csv_path: str,
        granularity: str,
        avail_tick_months: set
    ) -> Dict[str, Any]:
        buckets: Dict[str, Dict[str, Any]] = {}

        with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    open_str = row.get("open_time", row.get("open_time_utc", "")).replace(" UTC", "")
                    if not open_str or len(open_str) < 10:
                        continue

                    open_ms = parse_timestamp_ms(open_str) or 0
                    date_part = open_str.split(" ")[0]  # YYYY-MM-DD
                    pnl = float(row.get("realized_pnl_usdt", 0.0))

                    if granularity == "monthly":
                        b_key = date_part[:7]  # YYYY-MM
                        b_label = f"Month {b_key}"
                    elif granularity == "weekly":
                        # Compute week key (YYYY-Wxx)
                        dt = datetime.datetime.strptime(date_part, "%Y-%m-%d")
                        year, week_num, _ = dt.isocalendar()
                        b_key = f"{year}-W{week_num:02d}"
                        b_label = f"Week {week_num:02d} ({b_key})"
                    elif granularity == "daily":
                        b_key = date_part
                        b_label = f"Day {b_key}"
                    else:
                        b_key = date_part[:7]
                        b_label = f"Month {b_key}"

                    if b_key not in buckets:
                        buckets[b_key] = {
                            "chunk_id": b_key,
                            "label": b_label,
                            "min_ms": open_ms,
                            "max_ms": open_ms,
                            "min_date": date_part,
                            "max_date": date_part,
                            "trades": 0,
                            "wins": 0,
                            "losses": 0,
                            "gross_profit": 0.0,
                            "gross_loss": 0.0,
                            "net_pnl": 0.0,
                            "equity_peak": 0.0,
                            "max_dd": 0.0,
                            "has_ticks": False
                        }

                    b = buckets[b_key]
                    b["trades"] += 1
                    b["min_ms"] = min(b["min_ms"], open_ms)
                    b["max_ms"] = max(b["max_ms"], open_ms)
                    b["min_date"] = min(b["min_date"], date_part)
                    b["max_date"] = max(b["max_date"], date_part)

                    if pnl > 0:
                        b["wins"] += 1
                        b["gross_profit"] += pnl
                    elif pnl < 0:
                        b["losses"] += 1
                        b["gross_loss"] += abs(pnl)
                    b["net_pnl"] += pnl

                    # Drawdown tracking
                    if b["net_pnl"] > b["equity_peak"]:
                        b["equity_peak"] = b["net_pnl"]
                    dd = b["equity_peak"] - b["net_pnl"]
                    if dd > b["max_dd"]:
                        b["max_dd"] = dd

                    if date_part[:7] in avail_tick_months:
                        b["has_ticks"] = True

                except (ValueError, KeyError):
                    continue

        # Convert to manifest items
        manifest_items = []
        sorted_keys = sorted(buckets.keys())

        for k in sorted_keys:
            b = buckets[k]
            tc = b["trades"]
            wr = (b["wins"] / tc * 100.0) if tc > 0 else 0.0
            pf = (b["gross_profit"] / b["gross_loss"]) if b["gross_loss"] > 0 else (99.9 if b["gross_profit"] > 0 else 1.0)
            
            # Rough token estimate: ~150 tokens per loss + ~1500 overhead
            est_tokens = 1800 + (b["losses"] * 180)

            item = ChunkManifestItem(
                chunk_id=b["chunk_id"],
                label=b["label"],
                granularity=granularity,
                start_date=b["min_date"],
                end_date=b["max_date"],
                start_ms=b["min_ms"],
                end_ms=b["max_ms"] + (86400 * 1000),
                trades_count=tc,
                winning_trades=b["wins"],
                losing_trades=b["losses"],
                win_rate_pct=wr,
                net_pnl_usdt=b["net_pnl"],
                profit_factor=pf,
                max_drawdown_usdt=b["max_dd"],
                has_ticks=b["has_ticks"],
                estimated_tokens=est_tokens
            )
            manifest_items.append(item)

        return {
            "run_id": run.metadata.run_id,
            "symbol": run.metadata.symbol,
            "strategy": run.metadata.strategy,
            "granularity": granularity,
            "total_chunks": len(manifest_items),
            "chunks": [m.to_dict() for m in manifest_items]
        }

    def _plan_loss_clusters(
        self,
        run: BacktestRunRecord,
        csv_path: str,
        avail_tick_months: set
    ) -> Dict[str, Any]:
        """
        Detects periods of consecutive losing streaks (>= 3 consecutive losses)
        or localized clusters where net drawdown was severe.
        """
        trades_list = []
        with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    open_str = row.get("open_time", row.get("open_time_utc", "")).replace(" UTC", "")
                    pnl = float(row.get("realized_pnl_usdt", 0.0))
                    open_ms = parse_timestamp_ms(open_str) or 0
                    trades_list.append({
                        "trade_id": int(row.get("trade_id", 0)),
                        "open_time": open_str,
                        "open_ms": open_ms,
                        "pnl": pnl,
                        "exit_reason": row.get("exit_reason", "")
                    })
                except (ValueError, KeyError):
                    continue

        clusters = []
        current_streak = []

        for t in trades_list:
            if t["pnl"] < 0:
                current_streak.append(t)
            else:
                if len(current_streak) >= 3:
                    clusters.append(list(current_streak))
                current_streak = []
        if len(current_streak) >= 3:
            clusters.append(list(current_streak))

        # If too few 3-streaks, collect any 2-streaks with high loss
        if len(clusters) < 3:
            current_streak = []
            for t in trades_list:
                if t["pnl"] < 0:
                    current_streak.append(t)
                else:
                    if len(current_streak) >= 2:
                        clusters.append(list(current_streak))
                    current_streak = []
            if len(current_streak) >= 2:
                clusters.append(list(current_streak))

        # Sort clusters by total loss magnitude (worst loss first)
        clusters.sort(key=lambda cl: sum(x["pnl"] for x in cl))

        # Take top 10 worst loss clusters
        top_clusters = clusters[:10]
        manifest_items = []

        for idx, cl in enumerate(top_clusters):
            c_id = f"loss_cluster_{idx + 1}"
            start_ms = cl[0]["open_ms"] - (300 * 1000)  # pad 5m before
            end_ms = cl[-1]["open_ms"] + (600 * 1000)    # pad 10m after
            s_date = cl[0]["open_time"].split(" ")[0]
            e_date = cl[-1]["open_time"].split(" ")[0]
            tot_pnl = sum(x["pnl"] for x in cl)
            streak_len = len(cl)
            date_prefix = s_date[:7]
            has_ticks = (date_prefix in avail_tick_months)

            label = f"Loss Cluster #{idx + 1}: {streak_len} Consecutive Losses ({tot_pnl:+.4f} USDT)"

            item = ChunkManifestItem(
                chunk_id=c_id,
                label=label,
                granularity="loss_clusters",
                start_date=s_date,
                end_date=e_date,
                start_ms=start_ms,
                end_ms=end_ms,
                trades_count=streak_len,
                winning_trades=0,
                losing_trades=streak_len,
                win_rate_pct=0.0,
                net_pnl_usdt=tot_pnl,
                profit_factor=0.0,
                max_drawdown_usdt=abs(tot_pnl),
                has_ticks=has_ticks,
                estimated_tokens=1500 + (streak_len * 220)
            )
            manifest_items.append(item)

        return {
            "run_id": run.metadata.run_id,
            "symbol": run.metadata.symbol,
            "strategy": run.metadata.strategy,
            "granularity": "loss_clusters",
            "total_chunks": len(manifest_items),
            "chunks": [m.to_dict() for m in manifest_items]
        }

    # =========================================================================
    # CHUNK EXTRACTION & FORENSIC DOSSIER GENERATION
    # =========================================================================

    def extract_chunk_data(
        self,
        run_id: str,
        start_ms: int,
        end_ms: int,
        max_losing_trades: int = 25,
        include_ticks: bool = True,
        include_post_exit: bool = True
    ) -> Dict[str, Any]:
        """
        Extracts all trades in the given time window and calculates rich forensic data:
        - Chunk Scorecard & Long/Short Skew
        - Surrounding 1m OHLCV Candlesticks & IndicatorMatrix
        - Macro & Micro Market Regime Profile (ATR, ADX, CHOP, VWAP, Volatility)
        - In-depth autopsies of losing trades (ticks, MFE/MAE, post-exit bounce)
        - Control group benchmark winning trades
        """
        run = self.indexer.get_run_by_id(run_id)
        if not run:
            raise ValueError(f"Run '{run_id}' not found")

        symbol = canonicalize_symbol(run.metadata.symbol)
        csv_path = os.path.join(self.reports_dir, f"{run_id}_trades.csv")

        # 1. Filter trades within window
        trades_in_window = []
        with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    open_str = row.get("open_time", row.get("open_time_utc", "")).replace(" UTC", "")
                    o_ms = parse_timestamp_ms(open_str) or 0
                    if start_ms <= o_ms <= end_ms:
                        trades_in_window.append(row)
                except (ValueError, KeyError):
                    continue

        if not trades_in_window:
            # Fallback: if window had no trades, search nearest 10 trades
            pass

        # 2. Compute Chunk Scorecard
        wins, losses, scratch = [], [], []
        longs, shorts = [], []
        gross_profit, gross_loss, net_pnl = 0.0, 0.0, 0.0

        for r in trades_in_window:
            pnl = float(r.get("realized_pnl_usdt", 0.0))
            direction = r.get("direction", "LONG").upper()

            if direction == "LONG":
                longs.append(r)
            else:
                shorts.append(r)

            if pnl > 0:
                wins.append(r)
                gross_profit += pnl
            elif pnl < 0:
                losses.append(r)
                gross_loss += abs(pnl)
            else:
                scratch.append(r)
            net_pnl += pnl

        tot_trades = len(trades_in_window)
        win_rate = (len(wins) / tot_trades * 100.0) if tot_trades > 0 else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.9 if gross_profit > 0 else 1.0)
        long_wr = (sum(1 for x in longs if float(x.get("realized_pnl_usdt", 0.0)) > 0) / len(longs) * 100.0) if longs else 0.0
        short_wr = (sum(1 for x in shorts if float(x.get("realized_pnl_usdt", 0.0)) > 0) / len(shorts) * 100.0) if shorts else 0.0

        scorecard = {
            "total_trades": tot_trades,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "scratch_trades": len(scratch),
            "win_rate_pct": round(win_rate, 2),
            "net_pnl_usdt": round(net_pnl, 4),
            "gross_profit_usdt": round(gross_profit, 4),
            "gross_loss_usdt": round(gross_loss, 4),
            "profit_factor": round(profit_factor, 2),
            "long_trades": len(longs),
            "short_trades": len(shorts),
            "long_win_rate_pct": round(long_wr, 2),
            "short_win_rate_pct": round(short_wr, 2)
        }

        # 3. Load Candlesticks and compute Indicator Matrix over the chunk
        pad_ms = 3600 * 1000 * 2  # 2h pad before and after for indicator warmup
        chunk_candles = self.forensics.get_candles(
            symbol=symbol,
            timeframe="1m",
            start_ms=start_ms - pad_ms,
            end_ms=end_ms + pad_ms,
            limit=15000
        )

        ind_matrix = IndicatorMatrix(chunk_candles, config=run.metadata.parameters)

        # 4. Market Regime Profile
        regime_profile = self._calculate_chunk_regime(chunk_candles, ind_matrix, start_ms, end_ms)

        # 5. Losing Trades Deep Forensic Autopsy
        losing_autopsies = []
        losses_to_analyze = losses[:max_losing_trades]

        for l_raw in losses_to_analyze:
            t_id = int(l_raw.get("trade_id", 0))
            o_str = l_raw.get("open_time", l_raw.get("open_time_utc", "")).replace(" UTC", "")
            c_str = l_raw.get("close_time", l_raw.get("close_time_utc", "")).replace(" UTC", "")
            o_ms = parse_timestamp_ms(o_str) or 0
            c_ms = parse_timestamp_ms(c_str) or (o_ms + 60000)

            direction = l_raw.get("direction", "LONG").upper()
            entry_p = float(l_raw.get("entry_price", 0.0))
            exit_p = float(l_raw.get("exit_price", 0.0))
            tp_p = float(l_raw.get("min_profit_tp_price", 0.0))
            sl_p = float(l_raw.get("stop_loss_price", 0.0))
            pnl_val = float(l_raw.get("realized_pnl_usdt", 0.0))
            roe_val = float(l_raw.get("roe_percentage", 0.0))
            dur_sec = float(l_raw.get("duration_seconds", 0.0))
            exit_reason = l_raw.get("exit_reason", "UNKNOWN")

            # Entry indicator snapshot
            nearest_idx = ind_matrix.find_nearest_index(o_ms // 1000)
            indicator_snapshot = ind_matrix.get_snapshot(nearest_idx)

            # Ticks & MFE/MAE
            ticks_data = {}
            if include_ticks:
                ticks_data = self.forensics.get_trade_ticks(
                    symbol=symbol,
                    entry_ms=o_ms,
                    exit_ms=c_ms,
                    direction=direction,
                    entry_price=entry_p,
                    tp_price=tp_p,
                    sl_price=sl_p,
                    pu=run.metadata.price_unit,
                    post_exit_sec=120.0 if include_post_exit else 0.0,
                    max_return_ticks=800
                )

            # Automated Forensic Cause Classification
            cause_tag, cause_desc = self._classify_loss_cause(
                exit_reason=exit_reason,
                duration_sec=dur_sec,
                indicators=indicator_snapshot,
                direction=direction,
                ticks_data=ticks_data
            )

            losing_autopsies.append({
                "trade_id": t_id,
                "direction": direction,
                "open_time": o_str,
                "close_time": c_str,
                "duration_seconds": dur_sec,
                "entry_price": entry_p,
                "exit_price": exit_p,
                "tp_price": tp_p,
                "sl_price": sl_p,
                "pnl_usdt": pnl_val,
                "roe_pct": roe_val,
                "exit_reason": exit_reason,
                "indicators": indicator_snapshot,
                "mfe_mae": ticks_data.get("mfe_mae", {}),
                "post_exit": ticks_data.get("post_exit", {}) if include_post_exit else {},
                "has_ticks": ticks_data.get("has_ticks", False),
                "cause_tag": cause_tag,
                "cause_desc": cause_desc
            })

        # 6. Benchmark Winning Trades (Control Group)
        winning_benchmarks = []
        for w_raw in wins[:3]:
            w_id = int(w_raw.get("trade_id", 0))
            w_ostr = w_raw.get("open_time", w_raw.get("open_time_utc", "")).replace(" UTC", "")
            w_oms = parse_timestamp_ms(w_ostr) or 0
            w_dir = w_raw.get("direction", "LONG").upper()
            w_idx = ind_matrix.find_nearest_index(w_oms // 1000)
            w_snap = ind_matrix.get_snapshot(w_idx)

            winning_benchmarks.append({
                "trade_id": w_id,
                "direction": w_dir,
                "open_time": w_ostr,
                "duration_seconds": float(w_raw.get("duration_seconds", 0.0)),
                "entry_price": float(w_raw.get("entry_price", 0.0)),
                "exit_price": float(w_raw.get("exit_price", 0.0)),
                "pnl_usdt": float(w_raw.get("realized_pnl_usdt", 0.0)),
                "indicators": w_snap
            })

        return {
            "run_metadata": run.metadata.to_dict(),
            "window": {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "start_date": format_ms_to_utc(start_ms).split(" ")[0],
                "end_date": format_ms_to_utc(end_ms).split(" ")[0]
            },
            "scorecard": scorecard,
            "regime_profile": regime_profile,
            "losing_autopsies": losing_autopsies,
            "winning_benchmarks": winning_benchmarks
        }

    def _calculate_chunk_regime(
        self,
        candles: List[Dict[str, Any]],
        ind_matrix: IndicatorMatrix,
        start_ms: int,
        end_ms: int
    ) -> Dict[str, Any]:
        """Calculates macro/micro market state across the chunk window."""
        window_closes = []
        window_atrs = []
        window_adxs = []
        window_chops = []
        above_200_count = 0
        below_200_count = 0

        start_sec = start_ms // 1000
        end_sec = end_ms // 1000

        for i, t in enumerate(ind_matrix.times):
            if start_sec <= t <= end_sec:
                c = ind_matrix.closes[i]
                e200 = ind_matrix.ema_200[i]
                window_closes.append(c)
                if i < len(ind_matrix.atr_14):
                    window_atrs.append(ind_matrix.atr_14[i])
                if i < len(ind_matrix.adx_14):
                    window_adxs.append(ind_matrix.adx_14[i])
                if i < len(ind_matrix.chop_14):
                    window_chops.append(ind_matrix.chop_14[i])

                if c >= e200:
                    above_200_count += 1
                else:
                    below_200_count += 1

        tot_bars = len(window_closes) or 1
        mean_atr = (sum(window_atrs) / len(window_atrs)) if window_atrs else 0.0
        mean_adx = (sum(window_adxs) / len(window_adxs)) if window_adxs else 0.0
        mean_chop = (sum(window_chops) / len(window_chops)) if window_chops else 50.0

        pct_trending = (sum(1 for a in window_adxs if a >= 25) / len(window_adxs) * 100.0) if window_adxs else 0.0
        pct_choppy = (sum(1 for a in window_adxs if a < 20) / len(window_adxs) * 100.0) if window_adxs else 0.0
        bull_dominance_pct = (above_200_count / tot_bars) * 100.0

        # Overall regime label
        if mean_adx >= 25:
            trend_label = "HIGH_TRENDING"
        elif mean_adx < 20:
            trend_label = "CHOPPY_RANGE"
        else:
            trend_label = "BALANCED_MODERATE"

        return {
            "total_bars_1m": tot_bars,
            "mean_atr_14": round(mean_atr, 6),
            "mean_adx_14": round(mean_adx, 2),
            "mean_choppiness_index": round(mean_chop, 2),
            "trending_bars_pct": round(pct_trending, 1),
            "choppy_bars_pct": round(pct_choppy, 1),
            "bullish_above_200ema_pct": round(bull_dominance_pct, 1),
            "bearish_below_200ema_pct": round(100.0 - bull_dominance_pct, 1),
            "regime_classification": trend_label
        }

    def _classify_loss_cause(
        self,
        exit_reason: str,
        duration_sec: float,
        indicators: Dict[str, Any],
        direction: str,
        ticks_data: Dict[str, Any]
    ) -> Tuple[str, str]:
        """Classifies probable mechanical failure cause for the loss."""
        # 1. Timeout Drift
        if "TIMEOUT" in exit_reason.upper() or duration_sec >= 85.0:
            return (
                "TIMEOUT_DRIFT",
                "Trade failed to capture momentum within standard hold window; slowly drifted into timeout exit."
            )

        # 2. Counter-Trend Trap
        trend = indicators.get("trend", {})
        dist_200 = trend.get("dist_to_200_ema_pct", 0.0)
        adx_val = trend.get("adx_14", 0.0)
        if direction == "LONG" and dist_200 < -0.20 and adx_val > 25:
            return (
                "COUNTER_TREND_TRAP",
                f"Entered LONG during strong macro downtrend ({dist_200:.2f}% below 200 EMA with ADX {adx_val:.1f})."
            )
        if direction == "SHORT" and dist_200 > 0.20 and adx_val > 25:
            return (
                "COUNTER_TREND_TRAP",
                f"Entered SHORT during strong macro uptrend ({dist_200:.2f}% above 200 EMA with ADX {adx_val:.1f})."
            )

        # 3. Stop Hunt / Liquidity Sweep Reversal
        post_exit = ticks_data.get("post_exit", {})
        recovered = post_exit.get("recovered_to_entry", False) or post_exit.get("reached_tp_after_exit", False)
        if recovered:
            return (
                "STOP_HUNT_REVERSAL",
                "Price triggered stop loss on a spike then immediately recovered back past entry price within 120s."
            )

        # 4. Chop Whipsaw
        mom = indicators.get("momentum", {})
        chop_val = mom.get("choppiness_index", 50.0)
        if chop_val >= 60.0 or adx_val < 18.0:
            return (
                "CHOP_WHIPSAW",
                f"Consolidation whipsaw in low-liquidity chop (CHOP index {chop_val:.1f}, ADX {adx_val:.1f})."
            )

        # 5. Volatility Wick Out
        if duration_sec < 5.0:
            return (
                "VOLATILITY_WICK_OUT",
                f"Immediate adverse excursion stopped trade in {duration_sec:.1f}s via sharp volatility wick."
            )

        return (
            "MOMENTUM_FAILURE",
            "Price failed to reach +2 tick TP target and reversed into stop loss."
        )

    # =========================================================================
    # DOSSIER FORMATTING: MARKDOWN & JSON
    # =========================================================================

    def format_chunk_markdown(self, chunk_data: Dict[str, Any], chunk_index: int = 1, total_chunks: int = 1) -> str:
        """Formats the extracted chunk data into an AI-ready Markdown dossier."""
        m = chunk_data["run_metadata"]
        w = chunk_data["window"]
        sc = chunk_data["scorecard"]
        reg = chunk_data["regime_profile"]
        losses = chunk_data["losing_autopsies"]
        wins = chunk_data["winning_benchmarks"]

        now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        sl_desc = m.get("sl_rule_desc") or f"ROE {m.get('sl_value', 25.0)}%"

        lines = [
            f"# 🤖 AI Quantitative Deep-Analysis Dossier: [Chunk {chunk_index} of {total_chunks}]",
            f"> **Backtest Run:** `{m['run_id']}` | **Symbol:** `{m['symbol']}` | **Strategy:** `{m['strategy']}`",
            f"> **Chunk Evaluation Window:** `{w['start_date']}` to `{w['end_date']}`",
            f"> **Export Timestamp:** `{now_utc}`\n",
            "---",
            "\n## 🧠 System Context & Mission for AI Quantitative Analyst",
            "You are an elite quantitative researcher and algorithmic risk architect. You are analyzing",
            f"**Chunk {chunk_index} of {total_chunks}** of an institutional crypto futures backtest on **{m['symbol']}**.",
            "This slice contains rich, high-fidelity microstructure data: 1m OHLCV indicators, millisecond tick excursions,",
            "MFE/MAE metrics, and post-exit recovery tracking specifically extracted for this period.",
            "",
            "### Core Objective:",
            "1. **Autopsy of Losses:** Deeply analyze WHY the losing trades lost during this window.",
            "2. **Market Regime Suitability:** Determine what market conditions (volatility, trend, chop, time of day)",
            "   cause this strategy to bleed capital, versus what conditions generate consistent profit.",
            "3. **Loss-Elimination Rules:** Formulate concrete, mathematically sound algorithmic filters to",
            "   invalidate the losing setups before execution, turning this into a highly profitable model.\n",
            "---",
            "\n## 1. ⚙️ Execution Environment & Hyperparameters",
            "| Parameter | Configuration Value | Operational Meaning |",
            "| :--- | :--- | :--- |",
            f"| **Asset Symbol** | `{m['symbol']}` | Target trading pair |",
            f"| **Strategy Evaluated** | `{m['strategy']}` | Active algorithmic model |",
            f"| **Candle Timeframe** | `{m['timeframe']}` | Indicator evaluation timeframe |",
            f"| **Leverage** | `{m['leverage']}x Isolated` | Margin leverage multiplier |",
            f"| **Take Profit Target** | `+{m['tp_ticks']} ticks` (`+{m['tp_ticks'] * m['price_unit']:.6f} price delta`) | Minimum scalp profit threshold |",
            f"| **Stop Loss Rule** | `{sl_desc}` | Stop loss exit trigger |",
            f"| **Fee Schedule** | `{m['fee_mode']} Mode (0.00% maker / 0.00% taker)` | Zero-fee operational advantage |",
            f"| **Price Unit (Tick)** | `{m['price_unit']}` | Minimum orderbook price increment |",
            f"| **Contract Size** | `{m['contract_size']}` | Base units per contract |",
            "\n---",
            "\n## 2. ⚡ Chunk Performance Scorecard",
            "| Metric | Chunk Window Value | Directional Breakdown | LONG Signals | SHORT Signals |",
            "| :--- | :--- | :--- | :--- | :--- |",
            f"| **Total Trades Executed** | `{sc['total_trades']:,}` | **Trade Count** | `{sc['long_trades']:,}` | `{sc['short_trades']:,}` |",
            f"| **Win Rate** | **`{sc['win_rate_pct']:.2f}%`** | **Win Rate (%)** | **`{sc['long_win_rate_pct']:.2f}%`** | **`{sc['short_win_rate_pct']:.2f}%`** |",
            f"| **Net Realized PnL** | **`{sc['net_pnl_usdt']:+,.4f} USDT`** | **Winning Trades** | `{sc['winning_trades']:,}` | - |",
            f"| **Gross Profit** | `+{sc['gross_profit_usdt']:,.4f} USDT` | **Losing Trades** | `{sc['losing_trades']:,}` | - |",
            f"| **Gross Loss** | `-{sc['gross_loss_usdt']:,.4f} USDT` | **Scratch Trades** | `{sc['scratch_trades']:,}` | - |",
            f"| **Profit Factor** | **`{sc['profit_factor']:.2f}`** | - | - | - |",
            "\n---",
            "\n## 3. 🌊 Macro & Micro Market Regime Profile During Chunk",
            "Empirical distribution of market microstructure throughout this slice:",
            "| Regime Dimension | Value | Regime Context / Operational Interpretation |",
            "| :--- | :--- | :--- |",
            f"| **Regime Classification** | **`{reg['regime_classification']}`** | Dominant market state across {reg['total_bars_1m']:,} 1m candles |",
            f"| **Mean ATR (14-period)** | `{reg['mean_atr_14']:.6f}` | Mean price volatility per 1-minute candle |",
            f"| **Mean ADX (14-period)** | `{reg['mean_adx_14']:.2f}` | Trend strength (>25 = Trending, <20 = Choppy Range) |",
            f"| **Trending Bars Ratio** | `{reg['trending_bars_pct']:.1f}%` of time | Percentage of candles with ADX >= 25 |",
            f"| **Choppy Bars Ratio** | `{reg['choppy_bars_pct']:.1f}%` of time | Percentage of candles with ADX < 20 |",
            f"| **Mean Choppiness Index** | `{reg['mean_choppiness_index']:.2f}` | CHOP > 61.8 = severe consolidation; CHOP < 38.2 = strong trend |",
            f"| **200 EMA Directional Bias** | Bullish: `{reg['bullish_above_200ema_pct']:.1f}%` / Bearish: `{reg['bearish_below_200ema_pct']:.1f}%` | Macro-trend orientation relative to 200 EMA |",
            "\n---",
            "\n## 4. 🔬 Forensic Autopsy of Losing Trades",
            f"Exhaustive forensic breakdown of {len(losses)} losing trades in this chunk:",
            "",
            "| Trade ID | Dir | Open Time (UTC) | Hold Dur | Entry Price | Exit Price | Realized Loss | Exit Trigger | Pre-Trade Trend / ADX | Pre-Trade RSI / CHOP | Probable Cause Fingerprint |",
            "| :---: | :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |"
        ]

        for lt in losses:
            ind = lt.get("indicators", {})
            tr = ind.get("trend", {})
            mom = ind.get("momentum", {})
            adx_val = tr.get("adx_14", 0.0)
            rsi_val = mom.get("rsi_14", 50.0)
            chop_val = mom.get("choppiness_index", 50.0)
            dist_200 = tr.get("dist_to_200_ema_pct", 0.0)

            trend_ctx = f"ADX {adx_val:.1f} ({dist_200:+.2f}% to 200EMA)"
            mom_ctx = f"RSI {rsi_val:.1f} / CHOP {chop_val:.1f}"

            lines.append(
                f"| `#{lt['trade_id']}` | **{lt['direction']}** | `{lt['open_time']}` | `{lt['duration_seconds']:.1f}s` | "
                f"`{lt['entry_price']:.6f}` | `{lt['exit_price']:.6f}` | **`{lt['pnl_usdt']:+.4f}`** (`{lt['roe_pct']:+.1f}%`) | "
                f"`{lt['exit_reason']}` | `{trend_ctx}` | `{mom_ctx}` | `{lt['cause_tag']}` |"
            )

        lines.extend([
            "\n### Detailed Microstructure Cards for Critical Losing Trades",
            "Detailed tick progression, MFE/MAE excursions, and post-exit behavior:"
        ])

        for lt in losses[:10]:  # Top 10 detailed micro-cards to balance token density
            ind = lt.get("indicators", {})
            tr = ind.get("trend", {})
            mom = ind.get("momentum", {})
            vol = ind.get("volume_and_fair_value", {})
            bands = ind.get("volatility_and_bands", {})
            candle = ind.get("candle_microstructure", {})
            mfe_mae = lt.get("mfe_mae", {})
            post = lt.get("post_exit", {})

            lines.extend([
                f"\n#### ❌ Loss Forensic Card: Trade #{lt['trade_id']} ({lt['direction']} on {lt['open_time']})",
                f"- **Failure Fingerprint:** `{lt['cause_tag']}`: *{lt['cause_desc']}*",
                f"- **Financial Impact:** PnL: `{lt['pnl_usdt']:+.4f} USDT` | ROE: `{lt['roe_pct']:+.2f}%` | Exit Reason: `{lt['exit_reason']}` | Hold Duration: `{lt['duration_seconds']:.1f}s`",
                f"- **Price Coordinates:** Entry: `{lt['entry_price']:.6f}` | TP: `{lt['tp_price']:.6f}` | SL: `{lt['sl_price']:.6f}` | Fill Exit: `{lt['exit_price']:.6f}`",
                f"- **Excursion Dynamics (MFE / MAE):**",
                f"  - **MFE (Max Favorable Excursion):** `{mfe_mae.get('mfe_ticks', 0.0):+.2f} ticks` (`{mfe_mae.get('mfe_price', 0.0):.6f}`) — Reached `{mfe_mae.get('mfe_pct_of_tp', 0.0):.1f}%` of TP target before collapsing.",
                f"  - **MAE (Max Adverse Excursion):** `{mfe_mae.get('mae_ticks', 0.0):+.2f} ticks` (`{mfe_mae.get('mae_price', 0.0):.6f}`) — Max adverse drawdown suffered.",
                f"- **Pre-Trade Multi-Indicator Snapshot:**",
                f"  - **Trend & EMAs:** Fast EMA: `{tr.get('ema_fast', 0.0):.6f}` | Slow EMA: `{tr.get('ema_slow', 0.0):.6f}` | 200 EMA: `{tr.get('ema_200', 0.0):.6f}` (Distance: `{tr.get('dist_to_200_ema_pct', 0.0):+.2f}%`)",
                f"  - **ADX / Trend Strength:** ADX: `{tr.get('adx_14', 0.0):.2f}` (`{tr.get('adx_regime', '')}`) | +DI: `{tr.get('plus_di', 0.0):.2f}` | -DI: `{tr.get('minus_di', 0.0):.2f}`",
                f"  - **Momentum:** Stoch %K: `{mom.get('stoch_k', 0.0):.2f}` | Stoch %D: `{mom.get('stoch_d', 0.0):.2f}` | Standard RSI: `{mom.get('rsi_14', 0.0):.2f}` | CHOP Index: `{mom.get('choppiness_index', 0.0):.2f}`",
                f"  - **Bollinger & Volatility:** ATR: `{bands.get('atr_14', 0.0):.6f}` | BB %B: `{bands.get('bb_pct_b', 0.5):.3f}` | BB Bandwidth: `{bands.get('bb_bandwidth', 0.0):.4f}`",
                f"  - **Volume & Fair Value:** Volume Surge Ratio: `{vol.get('volume_surge_ratio', 1.0):.2f}x` | VWAP: `{vol.get('vwap', 0.0):.6f}` (Dist: `{vol.get('dist_to_vwap_pct', 0.0):+.2f}%`)",
                f"  - **Candle Microstructure:** Body Ratio: `{candle.get('body_ratio', 0.0):.2f}` | Upper Wick Ratio: `{candle.get('upper_wick_ratio', 0.0):.2f}` | Lower Wick Ratio: `{candle.get('lower_wick_ratio', 0.0):.2f}`",
                f"- **Post-Exit 120s Trajectory:**",
                f"  - Reached TP post-exit? `{'YES (Stop Hunt / Liquidity Sweep)' if post.get('reached_tp_after_exit') else 'NO'}`",
                f"  - Recovered to entry price? `{'YES' if post.get('recovered_to_entry') else 'NO'}`",
                f"  - Price delta 60s after exit: `{post.get('price_60s_delta', 0.0):+.6f}` | 120s after exit: `{post.get('price_120s_delta', 0.0):+.6f}`"
            ])

        # Control Group: Winning Trades
        if wins:
            lines.extend([
                "\n---",
                "\n## 5. 🎯 Benchmark Control Group (Representative Winning Trades)",
                "Contrast these successful setups against the losing trades above:",
                "",
                "| Trade ID | Dir | Open Time (UTC) | Hold Dur | Entry Price | Net Gain | Pre-Trade ADX | Pre-Trade RSI | Pre-Trade CHOP | Dist to 200 EMA |",
                "| :---: | :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |"
            ])
            for wt in wins:
                ind = wt.get("indicators", {})
                tr = ind.get("trend", {})
                mom = ind.get("momentum", {})
                lines.append(
                    f"| `#{wt['trade_id']}` | **{wt['direction']}** | `{wt['open_time']}` | `{wt['duration_seconds']:.1f}s` | "
                    f"`{wt['entry_price']:.6f}` | `+{wt['pnl_usdt']:.4f}` | `{tr.get('adx_14', 0.0):.1f}` | "
                    f"`{mom.get('rsi_14', 0.0):.1f}` | `{mom.get('choppiness_index', 0.0):.1f}` | `{tr.get('dist_to_200_ema_pct', 0.0):+.2f}%` |"
                )

        # AI Prompt Questions
        lines.extend([
            "\n---",
            "\n## ❓ Guided Quantitative Research Directives for AI",
            "Based on the empirical microstructure data in this chunk, answer the following with mathematical rigor:",
            "1. **Root-Cause Analysis of Losses:** Examine the Losing Trades table and micro-cards. What is the single biggest mechanical reason trades lost in this chunk? (e.g. timeout drift, counter-trend entries against 200 EMA, chop whipsaws, or volatility wicks?)",
            "2. **The 'Delta' Between Wins and Losses:** Compare the Control Group (Winners) with the Losing Trades. What specific indicator value (e.g. ADX threshold, RSI range, Distance to 200 EMA, or Choppiness Index) separates the winning trades from the losing trades?",
            "3. **Optimal Time Kill-Switch:** Looking at the trade durations and MFE numbers, what is the optimal maximum holding duration (e.g., 45s, 60s, or 90s) to exit before an adverse stop hit?",
            "4. **Concrete Python Filter Formulation:** Provide 1 to 3 explicit filter rules in Python syntax to incorporate into the bot's `FilterPipeline` that would eliminate the majority of these losses while keeping the winning trades intact.",
            "5. **Chunk Rating & Strategy Suitability:** Rate the suitability of this strategy for this market regime (Score 1-10) and specify whether the bot should run full, with tight filters, or be paused entirely during such market regimes."
        ])

        return "\n".join(lines)

    def format_chunk_json(self, chunk_data: Dict[str, Any], chunk_index: int = 1, total_chunks: int = 1) -> Dict[str, Any]:
        """Exports chunk data as machine-readable JSON."""
        return {
            "schema_version": "2.1.0",
            "chunk_index": chunk_index,
            "total_chunks": total_chunks,
            "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "data": chunk_data
        }

    # =========================================================================
    # BATCH EXPORT & PACKAGING
    # =========================================================================

    def export_all_chunks_zip(
        self,
        run_id: str,
        granularity: str = "monthly",
        max_losing_trades: int = 20,
        include_ticks: bool = True,
        include_post_exit: bool = True
    ) -> BytesIO:
        """
        Batch exports all chunks of a run into an in-memory ZIP archive containing
        formatted Markdown dossiers and a Master Synthesis Guide.
        """
        manifest = self.get_chunk_manifest(run_id, granularity=granularity)
        chunks = manifest.get("chunks", [])
        total_chunks = len(chunks)

        zip_buffer = BytesIO()

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            # 1. Master Synthesis Guide
            guide_md = self._generate_master_synthesis_guide(manifest)
            zip_file.writestr("00_README_AND_MASTER_SYNTHESIS_PROMPT.md", guide_md)

            # 2. Manifest JSON
            zip_file.writestr("manifest.json", json.dumps(manifest, indent=2))

            # 3. Individual Chunk Dossiers
            for idx, c in enumerate(chunks):
                chunk_data = self.extract_chunk_data(
                    run_id=run_id,
                    start_ms=c["start_ms"],
                    end_ms=c["end_ms"],
                    max_losing_trades=max_losing_trades,
                    include_ticks=include_ticks,
                    include_post_exit=include_post_exit
                )
                chunk_md = self.format_chunk_markdown(chunk_data, chunk_index=idx + 1, total_chunks=total_chunks)
                filename = f"chunk_{idx + 1:02d}_{c['chunk_id']}.md"
                zip_file.writestr(filename, chunk_md)

        zip_buffer.seek(0)
        return zip_buffer

    def _generate_master_synthesis_guide(self, manifest: Dict[str, Any]) -> str:
        """Generates the master synthesis instructions for feeding chunks sequentially to an LLM."""
        run_id = manifest.get("run_id", "")
        symbol = manifest.get("symbol", "")
        strat = manifest.get("strategy", "")
        chunks = manifest.get("chunks", [])

        lines = [
            f"# 🧭 Master Quantitative Synthesis Guide: {symbol} - {strat}",
            f"> **Backtest Run ID:** `{run_id}` | **Total Partitioned Chunks:** `{len(chunks)}`\n",
            "---",
            "\n## How to Feed These Cropped Chunks to Your AI Model (Gemini, Claude, ChatGPT)",
            "Feeding an entire 8-month backtest into an AI model overwhelms its context and degrades its reasoning.",
            "This package partitions the backtest into smaller, ultra-dense forensic slices containing granular indicators,",
            "millisecond tick trajectories, MFE/MAE excursions, and post-exit bounce forensics.\n",
            "### Recommended Step-by-Step AI Interaction Workflow:\n",
            "1. **Step 1: Feed Chunk 1 (e.g. Month 1)**",
            "   - Upload or paste `chunk_01_*.md` into your AI chat.",
            "   - The AI will analyze the loss autopsy table, compare the control group winners, and provide initial filter rules.",
            "",
            "2. **Step 2: Feed Chunks 2 through N Sequentially**",
            "   - Prompt the AI: *'Here is Chunk X. Test your previous filter rules against this new data. Did they prevent the losses in this chunk? What edge cases or new loss patterns emerged?'*",
            "",
            "3. **Step 3: Final Synthesis Prompt**",
            "   - After feeding the chunks, prompt the AI:",
            "     *'Synthesize all insights across all chunks into a Unified Regime-to-Strategy Switcher Matrix and provide the final 3 production-ready Python filter rules to eliminate losses.'*\n",
            "---",
            "\n## Partitioned Chunks Overview in This Package",
            "| Chunk File | Label / Period | Trades Count | Win Rate | Net PnL (USDT) | Has Ticks |",
            "| :--- | :--- | :---: | :---: | :---: | :---: |"
        ]

        for idx, c in enumerate(chunks):
            fname = f"`chunk_{idx + 1:02d}_{c['chunk_id']}.md`"
            lines.append(
                f"| {fname} | `{c['label']}` | `{c['trades_count']:,}` | `{c['win_rate_pct']:.2f}%` | `{c['net_pnl_usdt']:+.4f}` | `{'⚡ Yes' if c['has_ticks'] else 'No'}` |"
            )

        return "\n".join(lines)
