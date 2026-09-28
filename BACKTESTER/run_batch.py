"""
High-Performance Multi-Asset Batch Backtest Runner
===================================================
Executes exhaustive parameter sweeps (Timeframes, Fees, Slippages) across
multiple trading assets using the Order Block + Demand strategy.
Generates comprehensive comparative CSV, Markdown reports, and detailed trade logs.
"""

import os
import sys
import glob
import time
import json
import csv
import argparse
import datetime
from typing import List, Dict, Any, Optional, Tuple

# Ensure utf-8 output encoding
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

BACKTESTER_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.abspath(os.path.join(BACKTESTER_DIR, ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from BACKTESTER.engine.config import BacktestConfig
from BACKTESTER.engine.scanner import canonicalize_symbol, format_ms_to_utc, parse_timestamp_ms
from BACKTESTER.engine.market_sim import BacktestMarket
from BACKTESTER.engine.data_loader import OHLCVLoader, normalize_timeframe
from BACKTESTER.engine.execution_sim import BacktestExecutionEngine
from BACKTESTER.engine.metrics import PerformanceCalculator, PerformanceSummary
from BACKTESTER.engine.downloader import ensure_market_data

DEFAULT_ASSETS = [
    "BTC_USDT",
    "XAU_USDT",
    "XAG_USDT",
    "ETH_USDT",
    "SOL_USDT",
    "DOGE_USDT",
    "TRUMP_USDT",
    "1000000MOG_USDT",
    "CL_USDT"
]

FEE_SCHEDULES = [
    ("Maker 0.02% / Taker 0.05%", 0.0002, 0.0005, 0.02, 0.05),
    ("Maker 0.00% / Taker 0.01%", 0.0000, 0.0001, 0.00, 0.01),
    ("Maker 0.10% / Taker 0.10%", 0.0010, 0.0010, 0.10, 0.10),
]

SLIPPAGE_TICKS = [1, 2, 3, 4, 5, 6, 7]
DEFAULT_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]


def run_batch_for_symbol(
    symbol: str,
    timeframes: List[str],
    fee_schedules: List[Tuple[str, float, float, float, float]],
    slippage_ticks: List[int],
    start_date: str = "2026-01-01",
    end_date: str = "2026-08-31",
    capital: float = 100.0,
    leverage: int = 10,
    margin_pct: float = 10.0,
    strategy: str = "ORDER_BLOCK_DEMAND",
    reports_dir: str = "BACKTESTER/reports",
    base_dir: str = "BACKTESTER",
    tag: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Runs all combinations for a single asset and returns (matrix_results, trade_results)."""
    canonical = canonicalize_symbol(symbol)
    if "MOG" in canonical:
        canonical = "1000000MOG_USDT"
    elif "XAU" in canonical:
        canonical = "XAU_USDT"
    elif "XAG" in canonical:
        canonical = "XAG_USDT"
    elif "CL" in canonical:
        canonical = "CL_USDT"

    # Handle asset listing dates
    effective_start = start_date
    if "CL" in canonical and start_date < "2026-04-01":
        effective_start = "2026-04-01"
        print(f"[*] Note: {canonical} listed on Binance on 2026-04-01. Using start date: {effective_start}")
    elif "XAG" in canonical and start_date < "2026-01-07":
        effective_start = "2026-01-07"
        print(f"[*] Note: {canonical} listed on Binance on 2026-01-07. Using start date: {effective_start}")

    os.makedirs(reports_dir, exist_ok=True)
    loader = OHLCVLoader(data_dir=os.path.join(base_dir, "OHLCV_Data_Binance"))
    start_ms = parse_timestamp_ms(effective_start)
    end_ms = parse_timestamp_ms(end_date)

    matrix_results: List[Dict[str, Any]] = []
    trade_results: List[Dict[str, Any]] = []

    print("\n" + "=" * 80)
    print(f"🚀 INITIATING BATCH MATRIX SWEEP: {canonical}")
    print(f"   Date Range:  {effective_start} to {end_date}")
    print(f"   Strategy:    {strategy} (Pure Market Execution)")
    print(f"   Capital:     ${capital:.2f} | Leverage: {leverage}x | Margin Sizing: {margin_pct}%")
    print(f"   Timeframes:  {', '.join(timeframes)}")
    print(f"   Combinations per timeframe: {len(fee_schedules)} fees x {len(slippage_ticks)} slips = {len(fee_schedules)*len(slippage_ticks)}")
    print("=" * 80)

    # Preload 1m disambiguation candles once if higher timeframes will be run
    sub_1m_candles = None
    needs_sub_1m = any(normalize_timeframe(tf) != "1m" for tf in timeframes)
    if needs_sub_1m:
        print(f"[*] Ensuring 1m sub-candles for {canonical} disambiguation...")
        ensure_market_data(
            symbol=canonical,
            timeframe="1m",
            start_date=effective_start,
            end_date=end_date,
            download_trades=False,
            base_dir=base_dir
        )
        sub_1m_candles = loader.load_candles(
            symbol=canonical,
            timeframe="1m",
            start_ms=start_ms,
            end_ms=end_ms
        )
        print(f"    Loaded {len(sub_1m_candles) if sub_1m_candles else 0} 1m sub-candles.")

    for tf in timeframes:
        norm_tf = normalize_timeframe(tf)
        use_ticks = norm_tf in ("1m", "5m")
        fidelity_label = "HIGH_FIDELITY_TICKS" if use_ticks else "OHLCV_ONLY"

        print(f"\n📂 [{canonical} - {norm_tf.upper()}] Mode: {fidelity_label}")
        # 1. Download/Verify market data for this timeframe
        ok = ensure_market_data(
            symbol=canonical,
            timeframe=norm_tf,
            start_date=effective_start,
            end_date=end_date,
            download_trades=use_ticks,
            base_dir=base_dir
        )

        # 2. Load primary candles into memory
        candles = loader.load_candles(
            symbol=canonical,
            timeframe=norm_tf,
            start_ms=start_ms,
            end_ms=end_ms
        )
        if not candles:
            print(f"⚠️  No candle data found for {canonical} {norm_tf}. Skipping timeframe.")
            continue

        print(f"    Successfully loaded {len(candles)} candles. Executing {len(fee_schedules)*len(slippage_ticks)} backtests...")

        # 3. Sweep fee schedules and slippages
        tf_run_count = 0
        t_tf_start = time.time()

        for fee_lbl, m_rate, t_rate, m_pct, t_pct in fee_schedules:
            for slip in slippage_ticks:
                tf_run_count += 1

                cfg = BacktestConfig(
                    symbol=canonical,
                    timeframe=norm_tf,
                    strategy_mode=strategy,
                    start_time=effective_start,
                    end_time=end_date,
                    initial_balance_usdt=capital,
                    leverage=leverage,
                    volume_mode="MARGIN_PCT",
                    margin_pct=margin_pct,
                    execution_style="PURE_MARKET",
                    fee_mode="MANUAL",
                    maker_fee_override=m_rate,
                    taker_fee_override=t_rate,
                    slippage_enabled=True,
                    slippage_ticks=slip,
                    use_tick_data=use_ticks,
                    ohlcv_data_dir=os.path.join(base_dir, "OHLCV_Data_Binance"),
                    trades_data_dir=os.path.join(base_dir, "Historical_Trades_Data_Binance"),
                    playback_speed=0.0,
                    show_progress=False,
                    verbose_ticks=False
                )

                engine = BacktestExecutionEngine(config=cfg)
                outcomes = engine.run(
                    preloaded_candles=candles,
                    preloaded_sub_candles_1m=sub_1m_candles if norm_tf != "1m" else None
                )

                summary: PerformanceSummary = PerformanceCalculator.calculate(
                    outcomes=outcomes,
                    initial_balance_usdt=capital,
                    inr_rate=cfg.inr_rate
                )

                res_row = {
                    "symbol": canonical,
                    "timeframe": norm_tf,
                    "fidelity": fidelity_label,
                    "fee_schedule": fee_lbl,
                    "maker_fee_pct": m_pct,
                    "taker_fee_pct": t_pct,
                    "slippage_ticks": slip,
                    "total_trades": summary.total_trades,
                    "winning_trades": summary.winning_trades,
                    "losing_trades": summary.losing_trades,
                    "win_rate_pct": round(summary.win_rate_pct, 2),
                    "profit_factor": round(summary.profit_factor, 2) if summary.profit_factor < 999 else 999.0,
                    "net_pnl_usdt": round(summary.net_pnl_usdt, 2),
                    "net_roi_pct": round(summary.net_roi_pct, 2),
                    "max_drawdown_pct": round(summary.max_drawdown_pct, 2),
                    "expectancy_usdt": round(summary.avg_trade_pnl_usdt, 4),
                    "total_fees_usdt": round(summary.total_fees_usdt, 2),
                    "final_balance_usdt": round(summary.final_balance_usdt, 2)
                }
                matrix_results.append(res_row)

                # Record individual trade-by-trade outcomes
                for o in outcomes:
                    trade_results.append({
                        "symbol": canonical,
                        "timeframe": norm_tf,
                        "fidelity": fidelity_label,
                        "fee_schedule": fee_lbl,
                        "maker_fee_pct": m_pct,
                        "taker_fee_pct": t_pct,
                        "slippage_ticks": slip,
                        "trade_id": o.trade_id,
                        "direction": o.direction.name if hasattr(o.direction, "name") else str(o.direction),
                        "entry_time_utc": format_ms_to_utc(int(o.open_time * 1000)),
                        "exit_time_utc": format_ms_to_utc(int(o.close_time * 1000)),
                        "duration_seconds": round(o.duration_seconds, 1),
                        "entry_price": o.entry_price,
                        "exit_price": o.exit_price,
                        "min_profit_tp_price": o.min_profit_tp_price,
                        "stop_loss_price": o.stop_loss_price,
                        "underlying_quantity": o.underlying_quantity,
                        "vol_contracts": o.vol_contracts,
                        "margin_used_usdt": round(o.margin_used_usdt, 2),
                        "notional_value_usdt": round(o.notional_value_usdt, 2),
                        "net_pnl_usdt": round(o.realized_pnl_usdt, 4),
                        "roe_percentage": round(o.roe_percentage, 2),
                        "pnl_percentage": round(o.pnl_percentage, 4),
                        "fee_open_usdt": round(o.fee_open_usdt, 4),
                        "fee_close_usdt": round(o.fee_close_usdt, 4),
                        "fee_total_usdt": round(o.fee_total_usdt, 4),
                        "exit_reason": o.exit_reason.name if hasattr(o.exit_reason, "name") else str(o.exit_reason),
                        "balance_after_trade_usdt": round(o.balance_after_trade_usdt, 2) if o.balance_after_trade_usdt is not None else None,
                        "smc_zone_id": getattr(o, "smc_zone_id", None),
                        "smc_zone_type": getattr(o, "smc_zone_type", None),
                        "smc_zone_low": getattr(o, "smc_zone_low", None),
                        "smc_zone_mid": getattr(o, "smc_zone_mid", None),
                        "smc_zone_high": getattr(o, "smc_zone_high", None),
                        "smc_candle_utc": getattr(o, "smc_zone_creation_time_utc", None),
                        "smc_origin_bar": getattr(o, "smc_zone_creation_bar_idx", None),
                        "smc_bos_bar": getattr(o, "smc_bos_bar_idx", None),
                        "smc_target_1to1": getattr(o, "smc_target_1to1", None),
                        "smc_target_1to2": getattr(o, "smc_target_1to2", None)
                    })

                roi_sign = "+" if res_row["net_roi_pct"] >= 0 else ""
                print(
                    f"    [{tf_run_count:02d}/21] Fee: Taker {t_pct:.2f}% | Slip: {slip}t -> "
                    f"Trades: {res_row['total_trades']:3d} | WR: {res_row['win_rate_pct']:5.1f}% | "
                    f"PF: {res_row['profit_factor']:4.2f} | Net: {roi_sign}{res_row['net_roi_pct']:6.1f}% (${res_row['net_pnl_usdt']:+.2f})"
                )

        t_tf_elapsed = time.time() - t_tf_start
        print(f"    Completed {norm_tf} sweep in {t_tf_elapsed:.1f}s.")

    # Generate Reports
    export_matrix_reports(
        symbol=canonical,
        results=matrix_results,
        trades=trade_results,
        reports_dir=reports_dir,
        tag=tag
    )
    return matrix_results, trade_results


def export_matrix_reports(
    symbol: str,
    results: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    reports_dir: str,
    tag: Optional[str] = None
):
    """Exports CSV, Markdown matrix reports, and trade-by-trade records."""
    suffix = f"_{tag}" if tag else ""

    # 1. Export Matrix CSV
    csv_file = os.path.join(reports_dir, f"{symbol}{suffix}_batch_matrix.csv")
    if results:
        headers = list(results[0].keys())
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(results)
        print(f"\n[+] Saved Matrix CSV: {csv_file}")

    # 2. Export Trade-by-Trade CSV
    trades_file = os.path.join(reports_dir, f"{symbol}{suffix}_trades.csv")
    if trades:
        trade_headers = list(trades[0].keys())
        with open(trades_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=trade_headers)
            writer.writeheader()
            writer.writerows(trades)
        print(f"[+] Saved Trades CSV ({len(trades)} trades): {trades_file}")

    # 3. Export Markdown Report
    md_file = os.path.join(reports_dir, f"{symbol}{suffix}_batch_matrix.md")
    sorted_by_pnl = sorted(results, key=lambda x: x["net_pnl_usdt"], reverse=True)
    top_5 = sorted_by_pnl[:5]

    lines = []
    lines.append(f"# 📊 Backtest Matrix Results: {symbol} {suffix.strip('_')}")
    lines.append(f"**Strategy:** `Order Block + Demand` | **Initial Capital:** `$100.00` | **Leverage:** `10x` | **Margin Sizing:** `10%`")
    lines.append(f"**Total Sweep Runs:** `{len(results)}` | **Total Trades Logged:** `{len(trades)}`")
    lines.append("\n---\n")

    lines.append("## 🏆 Top 5 Best Performing Configurations")
    lines.append("| Rank | Timeframe | Fee Schedule | Slippage | Trades | Win Rate | Profit Factor | Net PnL | ROI % | Max DD % |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    for idx, r in enumerate(top_5, 1):
        sign = "+" if r["net_roi_pct"] >= 0 else ""
        lines.append(
            f"| **#{idx}** | `{r['timeframe']}` | {r['fee_schedule']} | `{r['slippage_ticks']}t` | "
            f"`{r['total_trades']}` | `{r['win_rate_pct']}%` | `{r['profit_factor']}` | "
            f"**`${r['net_pnl_usdt']:+.2f}`** | **`{sign}{r['net_roi_pct']}%`** | `{r['max_drawdown_pct']}%` |"
        )
    lines.append("\n---\n")

    lines.append("## 📋 Comprehensive Results Table")
    lines.append("| Timeframe | Fidelity | Fee Schedule | Slip | Trades | Win Rate | PF | Net PnL (USDT) | ROI % | Max DD % | Final Balance |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    for r in results:
        sign = "+" if r["net_roi_pct"] >= 0 else ""
        fid_short = "TICKS" if "TICKS" in r["fidelity"] else "OHLCV"
        lines.append(
            f"| `{r['timeframe']}` | `{fid_short}` | {r['fee_schedule']} | `{r['slippage_ticks']}t` | "
            f"`{r['total_trades']}` | `{r['win_rate_pct']}%` | `{r['profit_factor']}` | "
            f"`${r['net_pnl_usdt']:+.2f}` | `{sign}{r['net_roi_pct']}%` | `{r['max_drawdown_pct']}%` | `${r['final_balance_usdt']:.2f}` |"
        )

    md_content = "\n".join(lines)
    with open(md_file, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"[+] Saved Matrix Markdown Report: {md_file}")

    # Publish to GitHub Step Summary if in GitHub Actions
    gh_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if gh_summary:
        try:
            with open(gh_summary, "a", encoding="utf-8") as gf:
                gf.write("\n\n" + md_content + "\n")
            print("[+] Appended report to GITHUB_STEP_SUMMARY.")
        except Exception as e:
            print(f"[!] Could not write to GITHUB_STEP_SUMMARY: {e}")


def consolidate_reports(symbol: str, reports_dir: str):
    """Gathers all partial matrix CSVs and trade CSVs for a symbol and generates unified reports."""
    canonical = canonicalize_symbol(symbol)
    if "MOG" in canonical:
        canonical = "1000000MOG_USDT"
    elif "XAU" in canonical:
        canonical = "XAU_USDT"
    elif "XAG" in canonical:
        canonical = "XAG_USDT"
    elif "CL" in canonical:
        canonical = "CL_USDT"

    print(f"\n[*] Consolidating reports for {canonical} in {reports_dir}...")

    # 1. Gather all matrix CSVs
    matrix_files = glob.glob(os.path.join(reports_dir, f"{canonical}*batch_matrix*.csv"))
    master_matrix_path = os.path.abspath(os.path.join(reports_dir, f"{canonical}_batch_matrix.csv"))
    chunk_matrix_files = [f for f in matrix_files if os.path.abspath(f) != master_matrix_path]
    if not chunk_matrix_files and os.path.exists(master_matrix_path):
        chunk_matrix_files = [master_matrix_path]

    all_matrix_rows: List[Dict[str, Any]] = []
    seen_matrix_keys = set()

    for mf in sorted(chunk_matrix_files):
        with open(mf, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                for k in ["total_trades", "winning_trades", "losing_trades", "slippage_ticks"]:
                    if k in row and row[k] != "":
                        try:
                            row[k] = int(float(row[k]))
                        except ValueError:
                            pass
                for k in ["win_rate_pct", "profit_factor", "net_pnl_usdt", "net_roi_pct", "max_drawdown_pct", "expectancy_usdt", "total_fees_usdt", "final_balance_usdt", "maker_fee_pct", "taker_fee_pct"]:
                    if k in row and row[k] != "":
                        try:
                            row[k] = float(row[k])
                        except ValueError:
                            pass

                key = (row.get("timeframe"), row.get("fee_schedule"), row.get("slippage_ticks"))
                if key not in seen_matrix_keys:
                    seen_matrix_keys.add(key)
                    all_matrix_rows.append(row)

    print(f"[+] Consolidated {len(all_matrix_rows)} matrix run configuration(s).")

    # 2. Gather all trade CSVs
    trade_files = glob.glob(os.path.join(reports_dir, f"{canonical}*trades*.csv"))
    master_trade_path = os.path.abspath(os.path.join(reports_dir, f"{canonical}_all_trades.csv"))
    chunk_trade_files = [f for f in trade_files if os.path.abspath(f) != master_trade_path]

    all_trades: List[Dict[str, Any]] = []
    seen_trade_keys = set()

    for tf in sorted(chunk_trade_files):
        with open(tf, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (row.get("timeframe"), row.get("fee_schedule"), row.get("slippage_ticks"), row.get("trade_id"), row.get("entry_time_utc"))
                if key not in seen_trade_keys:
                    seen_trade_keys.add(key)
                    all_trades.append(row)

    print(f"[+] Consolidated {len(all_trades)} trade-by-trade record(s).")

    # 3. Export unified files
    if all_matrix_rows:
        headers = []
        for r in all_matrix_rows:
            for k in r.keys():
                if k not in headers:
                    headers.append(k)
        with open(master_matrix_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_matrix_rows)
        print(f"[+] Saved Master Matrix CSV: {master_matrix_path}")

        md_file = os.path.join(reports_dir, f"{canonical}_batch_matrix.md")
        sorted_by_pnl = sorted(all_matrix_rows, key=lambda x: float(x.get("net_pnl_usdt", 0)), reverse=True)
        top_5 = sorted_by_pnl[:5]

        lines = []
        lines.append(f"# 📊 Backtest Matrix Results: {canonical}")
        lines.append(f"**Strategy:** `Order Block + Demand` | **Initial Capital:** `$100.00` | **Leverage:** `10x` | **Margin Sizing:** `10%`")
        lines.append(f"**Total Sweep Runs:** `{len(all_matrix_rows)}` | **Total Closed Trades Logged:** `{len(all_trades)}`")
        lines.append("\n---\n")

        lines.append("## 🏆 Top 5 Best Performing Configurations")
        lines.append("| Rank | Timeframe | Fee Schedule | Slippage | Trades | Win Rate | Profit Factor | Net PnL | ROI % | Max DD % |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for idx, r in enumerate(top_5, 1):
            roi = float(r.get('net_roi_pct', 0))
            sign = "+" if roi >= 0 else ""
            lines.append(
                f"| **#{idx}** | `{r['timeframe']}` | {r['fee_schedule']} | `{r['slippage_ticks']}t` | "
                f"`{r['total_trades']}` | `{r['win_rate_pct']}%` | `{r['profit_factor']}` | "
                f"**`${float(r['net_pnl_usdt']):+.2f}`** | **`{sign}{roi}%`** | `{r['max_drawdown_pct']}%` |"
            )
        lines.append("\n---\n")

        lines.append("## 📋 Comprehensive Results Table")
        lines.append("| Timeframe | Fidelity | Fee Schedule | Slip | Trades | Win Rate | PF | Net PnL (USDT) | ROI % | Max DD % | Final Balance |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for r in all_matrix_rows:
            roi = float(r.get('net_roi_pct', 0))
            sign = "+" if roi >= 0 else ""
            fid_short = "TICKS" if "TICKS" in str(r.get('fidelity', '')) else "OHLCV"
            lines.append(
                f"| `{r['timeframe']}` | `{fid_short}` | {r['fee_schedule']} | `{r['slippage_ticks']}t` | "
                f"`{r['total_trades']}` | `{r['win_rate_pct']}%` | `{r['profit_factor']}` | "
                f"`${float(r['net_pnl_usdt']):+.2f}` | `{sign}{roi}%` | `{r['max_drawdown_pct']}%` | `${float(r.get('final_balance_usdt', 0)):.2f}` |"
            )

        md_content = "\n".join(lines)
        with open(md_file, "w", encoding="utf-8") as f:
            f.write(md_content)
        print(f"[+] Saved Master Markdown Report: {md_file}")

        gh_summary = os.getenv("GITHUB_STEP_SUMMARY")
        if gh_summary:
            try:
                with open(gh_summary, "a", encoding="utf-8") as gf:
                    gf.write("\n\n" + md_content + "\n")
            except Exception:
                pass

    if all_trades:
        trade_headers = []
        for r in all_trades:
            for k in r.keys():
                if k not in trade_headers:
                    trade_headers.append(k)
        with open(master_trade_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=trade_headers, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_trades)
        print(f"[+] Saved Master Trades CSV: {master_trade_path}")


def main():
    parser = argparse.ArgumentParser(description="Multi-Asset Batch Strategy Backtest Runner")
    parser.add_argument("--symbols", type=str, default="BTC_USDT", help="Comma-separated trading symbols (e.g. BTC_USDT,ETH_USDT,ALL)")
    parser.add_argument("--symbol", type=str, default=None, help="Single trading symbol (alias for --symbols)")
    parser.add_argument("--timeframes", type=str, default="1m,5m,15m,1h,4h,1d", help="Comma-separated candle timeframes")
    parser.add_argument("--start", type=str, default="2026-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2026-08-31", help="End date (YYYY-MM-DD)")
    parser.add_argument("--capital", type=float, default=100.0, help="Initial capital in USDT")
    parser.add_argument("--leverage", type=int, default=10, help="Leverage multiplier")
    parser.add_argument("--margin-pct", type=float, default=10.0, help="Wallet balance margin allocation percent")
    parser.add_argument("--strategy", type=str, default="ORDER_BLOCK_DEMAND", help="Strategy to evaluate")
    parser.add_argument("--reports-dir", type=str, default="BACKTESTER/reports", help="Output directory for reports")
    parser.add_argument("--base-dir", type=str, default="BACKTESTER", help="Base data directory")
    parser.add_argument("--tag", type=str, default=None, help="Optional suffix tag for output report files")
    parser.add_argument("--fee-indices", type=str, default="0,1,2", help="Comma-separated fee schedule indices (0=0.02/0.05, 1=0.00/0.01, 2=0.10/0.10)")
    parser.add_argument("--slippage-ticks", type=str, default="1,2,3,4,5,6,7", help="Comma-separated slippage tick values (e.g. 1,2,3 or 4,5,6,7)")
    parser.add_argument("--consolidate", action="store_true", help="Consolidate partial chunk reports for symbol")

    args = parser.parse_args()

    sym_str = args.symbol or args.symbols
    if args.consolidate:
        consolidate_reports(symbol=sym_str, reports_dir=args.reports_dir)
        return

    if sym_str.strip().upper() == "ALL":
        target_symbols = DEFAULT_ASSETS
    else:
        target_symbols = [s.strip() for s in sym_str.split(",") if s.strip()]

    target_tfs = [t.strip().lower() for t in args.timeframes.split(",") if t.strip()]

    # Parse fee schedules and slippages
    fee_idxs = [int(x.strip()) for x in args.fee_indices.split(",") if x.strip()]
    target_fees = [FEE_SCHEDULES[i] for i in fee_idxs if 0 <= i < len(FEE_SCHEDULES)]
    target_slips = [int(x.strip()) for x in args.slippage_ticks.split(",") if x.strip()]

    t_all_start = time.time()
    for s in target_symbols:
        run_batch_for_symbol(
            symbol=s,
            timeframes=target_tfs,
            fee_schedules=target_fees,
            slippage_ticks=target_slips,
            start_date=args.start,
            end_date=args.end,
            capital=args.capital,
            leverage=args.leverage,
            margin_pct=args.margin_pct,
            strategy=args.strategy,
            reports_dir=args.reports_dir,
            base_dir=args.base_dir,
            tag=args.tag
        )

    t_all_elapsed = time.time() - t_all_start
    print(f"\n🎉 All batch sweeps completed in {t_all_elapsed/60:.2f} minutes.")


if __name__ == "__main__":
    main()
