"""
Backtest Reporting & Outcome Exporters
======================================
Produces colorized terminal summary cards, dual-currency trade tables,
ASCII equity charts, and exports full trade outcomes to CSV, JSONL, and Markdown.
"""

import os
import csv
import json
import datetime
from typing import List, Dict, Any, Optional

from kcex.engine.models import TradeOutcome
from BACKTESTER.engine.metrics import PerformanceSummary
from BACKTESTER.engine.scanner import format_ms_to_utc


def format_duration(seconds: float) -> str:
    """Formats seconds into human-readable duration strings."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m {s:02d}s"
    else:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h}h {m:02d}m {s:02d}s"


BACKTESTER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT_DIR = os.path.abspath(os.path.join(BACKTESTER_DIR, ".."))


class BacktestReporter:
    """
    Renders formatted terminal cards and exports trade history reports.
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
        self.reports_dir = reports_dir
        os.makedirs(self.reports_dir, exist_ok=True)

    def print_summary(self, summary: PerformanceSummary) -> None:
        """Prints an executive summary card of backtest performance."""
        sep = "=" * 80
        sub_sep = "-" * 80

        print("\n" + sep)
        print(f"        BACKTEST PERFORMANCE REPORT - {summary.symbol}")
        print(sep)

        # Overview
        pnl_sign = "+" if summary.net_pnl_usdt >= 0 else ""
        roi_sign = "+" if summary.net_roi_pct >= 0 else ""
        print(f"Total Trades:           {summary.total_trades:<6} | Win Rate:             {summary.win_rate_pct:.2f}%")
        print(f"Wins / Losses / Scratch: {summary.winning_trades} / {summary.losing_trades} / {summary.scratch_trades}")
        print(sub_sep)

        # Financials
        print(f"Initial Capital:        {summary.initial_balance_usdt:>12.4f} USDT  (INR {summary.initial_balance_inr:>10.2f})")
        print(f"Final Balance:          {summary.final_balance_usdt:>12.4f} USDT  (INR {summary.final_balance_inr:>10.2f})")
        print(f"Net Realized PnL:       {pnl_sign}{summary.net_pnl_usdt:>11.4f} USDT  ({roi_sign}{summary.net_roi_pct:.2f}%)")
        print(f"Net Realized PnL (INR): {pnl_sign}{summary.net_pnl_inr:>11.2f} INR")
        print(f"Total Taker Fees Paid:  {summary.total_fees_usdt:>12.4f} USDT  (INR {summary.total_fees_inr:>10.2f})")
        print(f"Profit Factor:          {summary.profit_factor:>12.2f}  | Win/Loss Payoff:      {summary.win_loss_ratio:>6.2f}")
        print(sub_sep)

        # Risk & Drawdown
        print(f"Max Drawdown:           {summary.max_drawdown_usdt:>12.4f} USDT  (-{summary.max_drawdown_pct:.2f}%)")
        print(f"Sharpe Ratio (est):     {summary.sharpe_ratio:>12.2f}  | Sortino Ratio:        {summary.sortino_ratio:>6.2f}")
        print(f"Calmar Ratio:           {summary.calmar_ratio:>12.2f}")
        print(sub_sep)

        # Execution Stats
        print(f"Avg Trade PnL:          {pnl_sign}{summary.avg_trade_pnl_usdt:>11.4f} USDT  | Avg Duration:         {summary.avg_duration_seconds:.1f}s")
        print(f"Avg Winning Trade:      +{summary.avg_win_pnl_usdt:>11.4f} USDT  | Avg Losing Trade:     -{summary.avg_loss_pnl_usdt:>6.4f} USDT")
        print(f"Max Consecutive Wins:   {summary.max_consecutive_wins:<6}       | Max Consecutive Losses: {summary.max_consecutive_losses}")
        print(f"Long Trades:            {summary.long_trades:<6} (Win: {summary.long_win_rate_pct:.1f}%) | Short Trades:         {summary.short_trades:<6} (Win: {summary.short_win_rate_pct:.1f}%)")
        print(sub_sep)

        # Exit Reasons Breakdown
        print("Exit Triggers Breakdown:")
        for r_name, count in summary.exit_reasons.items():
            pct = (count / summary.total_trades * 100.0) if summary.total_trades > 0 else 0.0
            print(f"  +-- {r_name:<26}: {count:>4} trades ({pct:>5.1f}%)")
        print(sep + "\n")

    def print_trades_table(self, outcomes: List[TradeOutcome], limit: int = 15) -> None:
        """Prints a tabular log of the recent trades."""
        if not outcomes:
            return

        print(f"--- RECENT TRADES (Showing {min(len(outcomes), limit)} of {len(outcomes)}) ---")
        header = f"{'#':<4} | {'Dir':<5} | {'Entry Price':<11} | {'Exit Price':<11} | {'PnL (USDT)':<12} | {'ROE %':<8} | {'Exit Reason':<22} | {'Duration':<8}"
        print(header)
        print("-" * len(header))

        # Show first few or last few
        display_items = outcomes[-limit:] if len(outcomes) > limit else outcomes
        for o in display_items:
            pnl_str = f"{'+' if o.realized_pnl_usdt >= 0 else ''}{o.realized_pnl_usdt:.4f}"
            roe_str = f"{'+' if o.roe_percentage >= 0 else ''}{o.roe_percentage:.1f}%"
            reason_str = o.exit_reason.value if hasattr(o.exit_reason, "value") else str(o.exit_reason)
            dur_str = f"{o.duration_seconds:.1f}s"
            print(f"{o.trade_id:<4} | {o.direction.value:<5} | {o.entry_price:<11.4f} | {o.exit_price:<11.4f} | {pnl_str:<12} | {roe_str:<8} | {reason_str:<22} | {dur_str:<8}")
        print("-" * len(header) + "\n")

    def generate_detailed_markdown(
        self,
        outcomes: List[TradeOutcome],
        summary: PerformanceSummary,
        config: Optional[Any] = None,
        contract: Optional[Any] = None
    ) -> str:
        """
        Generates an extensive, institutional-grade Markdown report including
        all settings, configurations, metrics, directional analysis, and trade journals.
        """
        now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Resolve config parameters
        sym = summary.symbol
        tf = getattr(config, "timeframe", "1m") if config else "1m"
        strat = getattr(config, "strategy_mode", "EMA_CROSSOVER") if config else "EMA_CROSSOVER"
        start_date = getattr(config, "start_time", "Earliest") or "Earliest"
        end_date = getattr(config, "end_time", "Latest") or "Latest"
        vol_mode = getattr(config, "volume_mode", "CONTRACTS") if config else "CONTRACTS"
        vol_contracts = getattr(config, "volume_contracts", None) if config else None
        vol_mult = getattr(config, "volume_multiplier", 1.0) if config else 1.0
        leverage = getattr(config, "leverage", 30) if config else 30
        tp_ticks = getattr(config, "tp_ticks", 2) if config else 2
        sl_mode = getattr(config, "sl_mode", "TICKS") if config else "TICKS"
        sl_ticks = getattr(config, "sl_ticks", 10) if config else 10
        sl_roe = getattr(config, "sl_roe_pct", 25.0) if config else 25.0
        fee_mode = getattr(config, "fee_mode", "LIVE") if config else "LIVE"
        slippage = getattr(config, "slippage_ticks", 0) if config else 0
        use_ticks = getattr(config, "use_tick_data", True) if config else True
        inr_rate = getattr(config, "inr_rate", 94.45) if config else 94.45
        ema_preset = getattr(config, "ema_preset", "5/13") if config else "5/13"
        stoch_preset = getattr(config, "stoch_preset", "FAST_SCALP") if config else "FAST_SCALP"

        # Resolve contract specifications
        base_coin = getattr(contract, "base_coin", sym.split("_")[0]) if contract else sym.split("_")[0]
        quote_coin = getattr(contract, "quote_coin", "USDT") if contract else "USDT"
        cs = getattr(contract, "contract_size", 1.0) if contract else 1.0
        pu = getattr(contract, "price_unit", 0.001) if contract else 0.001
        ps = getattr(contract, "price_precision", 4) if contract else 4
        min_vol = getattr(contract, "min_volume", 1.0) if contract else 1.0
        max_lev = getattr(contract, "max_leverage", leverage) if contract else leverage
        m_fee_rate = getattr(contract, "maker_fee_rate", 0.0) if contract else 0.0
        t_fee_rate = getattr(contract, "taker_fee_rate", 0.0) if contract else 0.0

        margin_pct = getattr(config, "margin_pct", None) if config else None
        fixed_margin = getattr(config, "fixed_margin_usdt", None) if config else None

        # Detailed Volume Sizing Description
        if vol_mode == "MARGIN_PCT" or (margin_pct is not None and vol_mode not in ("CONTRACTS", "MULTIPLIER", "MIN")):
            vol_desc = f"{margin_pct:.1f}% Available Margin per trade (Dynamic Sizing, Margin × Leverage = Position Size)"
        elif vol_mode == "FIXED_MARGIN" or (fixed_margin is not None and vol_mode not in ("CONTRACTS", "MULTIPLIER", "MIN")):
            vol_desc = f"{fixed_margin:.2f} USDT Fixed Margin per trade (Margin × Leverage = Position Size)"
        elif vol_contracts is not None:
            vol_desc = f"{vol_contracts} contract(s) ({vol_contracts * cs:.4g} {base_coin} per trade)"
        elif vol_mode == "MIN":
            vol_desc = f"Minimum volume: {int(min_vol)} contract(s) ({int(min_vol) * cs:.4g} {base_coin})"
        else:
            vol_desc = f"{vol_mult:g}x minimum volume ({int(min_vol * vol_mult)} contract(s))"

        # Strategy Description
        strat_desc_map = {
            "EMA_CROSSOVER": f"EMA Crossover Trend Follower (Preset: {ema_preset} ; Closed Candle Confirmation: True)",
            "STOCH_RSI": f"Stochastic RSI Momentum Scalper (Preset: {stoch_preset} ; Overbought/Oversold Reversal)",
            "CYCLE": "Directional Cycle Sub-Strategy (Fixed direction trade cycling)",
            "MICROSTRUCTURE": "Order Book Imbalance (OBI) & Deal Flow Delta Bursts"
        }
        strat_full_desc = strat_desc_map.get(strat, strat)

        # Strategy specific parameter resolution
        active_preset = ema_preset if strat == "EMA_CROSSOVER" else (stoch_preset if strat == "STOCH_RSI" else "DEFAULT")
        ema_fast = getattr(config, "ema_fast", 5) if config else 5
        ema_slow = getattr(config, "ema_slow", 13) if config else 13
        ema_candle_conf = getattr(config, "ema_require_closed_candle", True) if config else True
        stoch_rsi_p = getattr(config, "stoch_rsi_period", 9) if config else 9
        stoch_p = getattr(config, "stoch_period", 9) if config else 9
        stoch_k = getattr(config, "stoch_k_period", 3) if config else 3
        stoch_d = getattr(config, "stoch_d_period", 3) if config else 3
        stoch_os = getattr(config, "stoch_oversold", 20.0) if config else 20.0
        stoch_ob = getattr(config, "stoch_overbought", 80.0) if config else 80.0
        stoch_zone = getattr(config, "stoch_zone_filter", True) if config else True
        stoch_candle_conf = getattr(config, "stoch_require_closed_candle", True) if config else True
        bi_directional = getattr(config, "bi_directional", True) if config else True

        # Trade Optimization & Filter parameters
        dur_filter_enabled = getattr(config, "duration_filter_enabled", False) if config else False
        dur_deep_s = getattr(config, "duration_deep_monitor_seconds", 60.0) if config else 60.0
        dur_max_s = getattr(config, "duration_max_hold_seconds", 90.0) if config else 90.0
        dur_action = getattr(config, "duration_action", "CLOSE") if config else "CLOSE"
        adx_filter_enabled = getattr(config, "adx_filter_enabled", False) if config else False
        adx_period = getattr(config, "adx_period", 14) if config else 14
        adx_threshold = getattr(config, "adx_threshold", 25.0) if config else 25.0
        htf_trend_enabled = getattr(config, "htf_trend_filter_enabled", False) if config else False
        htf_timeframe = getattr(config, "htf_timeframe", "15m") if config else "15m"
        htf_ema = getattr(config, "htf_ema_period", 200) if config else 200
        hourly_filter_enabled = getattr(config, "hourly_filter_enabled", False) if config else False
        hourly_blacklist = getattr(config, "hourly_blacklist_utc", []) if config else []
        direction_bias = getattr(config, "direction_bias", "BOTH") if config else "BOTH"

        # Stop loss description
        if sl_mode == "ROE":
            sl_desc = f"-{sl_roe:.1f}% ROE on committed margin"
        elif sl_mode == "TICKS":
            sl_desc = f"-{sl_ticks} ticks away from entry ({sl_ticks * pu:.{ps}f} USDT)"
        else:
            sl_desc = f"Price move percent"

        # Directional statistics
        long_trades = [o for o in outcomes if "LONG" in str(o.direction).upper()]
        short_trades = [o for o in outcomes if "SHORT" in str(o.direction).upper()]

        def calc_sub_stats(group: List[TradeOutcome]):
            count = len(group)
            if count == 0:
                return {"count": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "pnl": 0.0, "pnl_inr": 0.0, "gross_profit": 0.0, "gross_loss": 0.0, "pf": 0.0}
            wins = sum(1 for o in group if o.realized_pnl_usdt > 0)
            losses = sum(1 for o in group if o.realized_pnl_usdt < 0)
            win_rate = (wins / count * 100.0) if count > 0 else 0.0
            pnl = sum(o.realized_pnl_usdt for o in group)
            pnl_inr = pnl * inr_rate
            gross_p = sum(o.realized_pnl_usdt for o in group if o.realized_pnl_usdt > 0)
            gross_l = abs(sum(o.realized_pnl_usdt for o in group if o.realized_pnl_usdt < 0))
            pf = (gross_p / gross_l) if gross_l > 0 else (999.99 if gross_p > 0 else 0.0)
            return {
                "count": count,
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
                "pnl": pnl,
                "pnl_inr": pnl_inr,
                "gross_profit": gross_p,
                "gross_loss": gross_l,
                "pf": pf
            }

        l_stats = calc_sub_stats(long_trades)
        s_stats = calc_sub_stats(short_trades)

        # Extreme trades & Durations
        best_trade = max(outcomes, key=lambda x: x.realized_pnl_usdt) if outcomes else None
        worst_trade = min(outcomes, key=lambda x: x.realized_pnl_usdt) if outcomes else None
        shortest_trade = min(outcomes, key=lambda x: x.duration_seconds) if outcomes else None
        longest_trade = max(outcomes, key=lambda x: x.duration_seconds) if outcomes else None
        total_duration_in_pos = sum(o.duration_seconds for o in outcomes)

        # Financial assessment
        pf_text = "Exceptional (Institutional Grade)" if summary.profit_factor >= 2.0 else ("Profitable" if summary.profit_factor >= 1.0 else "Unprofitable / Needs Optimization")

        lines = []
        lines.append(f"# 📊 Institutional Backtest Performance Report: {sym}\n")
        lines.append(f"> **Generated:** `{now_utc} UTC` | **Engine:** `KCEX High-Fidelity Dual-Feed Simulator v1.3`\n")
        lines.append("---\n")

        # 1. Executive Scorecard
        lines.append("## ⚡ Executive Scorecard\n")
        lines.append("| Performance Metric | USDT Value | INR Value (₹" + f"{inr_rate:.2f}" + ") | % Return / Ratio |")
        lines.append("| :--- | :--- | :--- | :--- |")
        lines.append(f"| **Initial Capital** | `{summary.initial_balance_usdt:,.4f} USDT` | `₹{summary.initial_balance_inr:,.2f}` | Baseline (100.0%) |")
        lines.append(f"| **Final Balance** | `{summary.final_balance_usdt:,.4f} USDT` | `₹{summary.final_balance_inr:,.2f}` | `{((summary.final_balance_usdt/summary.initial_balance_usdt)-1)*100:+.2f}%` |")
        lines.append(f"| **Net Realized PnL** | **`{summary.net_pnl_usdt:+,.4f} USDT`** | **`₹{summary.net_pnl_inr:+,.2f}`** | **`{summary.net_roi_pct:+.2f}% Net ROI`** |")
        lines.append(f"| **Gross Profit** | `+{summary.gross_profit_usdt:,.4f} USDT` | `₹{summary.gross_profit_usdt * inr_rate:,.2f}` | Total positive trade returns |")
        lines.append(f"| **Gross Loss** | `-{summary.gross_loss_usdt:,.4f} USDT` | `₹{summary.gross_loss_usdt * inr_rate:,.2f}` | Total negative trade drawdowns |")
        lines.append(f"| **Total Taker Fees Paid** | `{summary.total_fees_usdt:,.6f} USDT` | `₹{summary.total_fees_inr:,.2f}` | `{(summary.total_fees_usdt / summary.initial_balance_usdt * 100):.4f}% of capital` |")
        lines.append(f"| **Profit Factor** | **`{summary.profit_factor:.2f}`** | — | {pf_text} |")
        lines.append(f"| **Win / Loss Payoff** | `{summary.win_loss_ratio:.2f}` | — | Average Win vs Average Loss ratio |")
        lines.append(f"| **Max Drawdown** | `-{summary.max_drawdown_usdt:,.4f} USDT` | `₹{summary.max_drawdown_usdt * inr_rate:,.2f}` | **`-{summary.max_drawdown_pct:.2f}%` Peak-to-Trough** |")
        lines.append(f"| **Win Rate** | **`{summary.win_rate_pct:.2f}%`** | — | `{summary.winning_trades} Wins / {summary.losing_trades} Losses / {summary.scratch_trades} Scratch` |")
        lines.append(f"| **Sharpe Ratio (est)** | `{summary.sharpe_ratio:.2f}` | — | Annualized risk-adjusted excess return |")
        lines.append(f"| **Sortino Ratio** | `{summary.sortino_ratio:.2f}` | — | Downside risk-adjusted return ratio |")
        lines.append(f"| **Calmar Ratio** | `{summary.calmar_ratio:.2f}` | — | Net ROI divided by Max Drawdown |")
        lines.append("\n---\n")

        # 2. Complete Settings & Parameters
        lines.append("## 🛠️ Complete Configuration & Settings Used\n")
        lines.append("### Strategy & Market Setup")
        lines.append("| Configuration Setting | Value | Operational Details |")
        lines.append("| :--- | :--- | :--- |")
        lines.append(f"| **Trading Pair Symbol** | `{sym}` | Base Asset: `{base_coin}` / Quote Asset: `{quote_coin}` |")
        lines.append(f"| **Candle Timeframe** | `{tf}` | Dynamic candle granularity evaluated by strategy indicators |")
        lines.append(f"| **Strategy Evaluated** | `{strat}` | {strat_full_desc} |")
        lines.append(f"| **Strategy Preset** | `{active_preset}` | Configured indicator preset profile |")
        lines.append(f"| **Evaluation Date Range** | `{start_date}` → `{end_date}` | Historical evaluation window |")
        lines.append(f"| **High-Fidelity Simulation** | `{'ENABLED (Tick Trades)' if use_ticks else 'DISABLED (Candle OHLC)'}` | Millisecond-level trade order matching & stop triggering |")
        lines.append(f"| **Slippage Tolerance** | `{slippage} ticks` (`{slippage * pu:.{ps}f} USDT` per fill) | Adverse fill penalty applied to entry and exit orders |")
        lines.append("")

        lines.append("### Strategy & Indicator Hyperparameters")
        lines.append("| Hyperparameter | Value | Technical Context |")
        lines.append("| :--- | :--- | :--- |")
        lines.append(f"| **Active Strategy Engine** | `{strat}` | Quantitative model evaluated |")
        lines.append(f"| **Active Strategy Preset** | `{active_preset}` | Selected preset configuration |")
        if strat == "EMA_CROSSOVER":
            lines.append(f"| **Fast EMA Period** | `{ema_fast}` | Short-term fast moving average |")
            lines.append(f"| **Slow EMA Period** | `{ema_slow}` | Baseline slow moving average |")
            lines.append(f"| **Candle Close Confirmation** | `{'ENABLED' if ema_candle_conf else 'DISABLED'}` | Requires bar to close before emitting cross |")
        elif strat == "STOCH_RSI":
            lines.append(f"| **RSI Period** | `{stoch_rsi_p}` | Relative Strength Index calculation length |")
            lines.append(f"| **Stoch Lookback Period** | `{stoch_p}` | Stochastic window over RSI |")
            lines.append(f"| **%K Smoothing** | `{stoch_k}` | Fast stochastic line smoothing period |")
            lines.append(f"| **%D Smoothing** | `{stoch_d}` | Slow signal line smoothing period |")
            lines.append(f"| **Oversold Threshold (OS)** | `{stoch_os}` | Extreme oversold boundary (Bullish entry gate) |")
            lines.append(f"| **Overbought Threshold (OB)** | `{stoch_ob}` | Extreme overbought boundary (Bearish entry gate) |")
            lines.append(f"| **Extreme Zone Filter** | `{'ENABLED' if stoch_zone else 'DISABLED'}` | Suppresses non-extreme neutral whipsaws |")
            lines.append(f"| **Candle Close Confirmation** | `{'ENABLED' if stoch_candle_conf else 'DISABLED'}` | Requires bar to close before emitting cross |")
        lines.append(f"| **Directional Flow Mode** | `{'Autonomous Bi-Directional (LONG & SHORT)' if bi_directional else 'Single-Direction'}` | Order generation policy |")
        lines.append("")

        lines.append("### Trade Optimization & Regime Filters")
        lines.append("| Filter Dimension | Configuration | Operational Action & Trigger |")
        lines.append("| :--- | :--- | :--- |")
        lines.append(f"| **Trade Duration Monitoring** | `{'ENABLED' if dur_filter_enabled else 'DISABLED'}` | Deep in-position monitoring at `{dur_deep_s}s` elapsed |")
        lines.append(f"| **Time-Stop Protective Exit** | `{'ENABLED' if dur_filter_enabled else 'DISABLED'}` | Action `{dur_action}` triggered if open duration > `{dur_max_s}s` |")
        lines.append(f"| **ADX Trend Regime Filter** | `{'ENABLED' if adx_filter_enabled else 'DISABLED'}` | Period: `{adx_period}` / Threshold: `{adx_threshold}` |")
        lines.append(f"| **HTF Trend Baseline (200 EMA)** | `{'ENABLED' if htf_trend_enabled else 'DISABLED'}` | Timeframe: `{htf_timeframe}` / Period: `{htf_ema}` |")
        lines.append(f"| **Hourly Session Filter** | `{'ENABLED' if hourly_filter_enabled else 'DISABLED'}` | Blacklisted UTC Hours: `{hourly_blacklist if hourly_blacklist else 'None'}` |")
        lines.append(f"| **Directional Bias Policy** | `{direction_bias}` | Pre-trade signal directional allowance |")
        lines.append("")

        lines.append("### Position Sizing, Leverage & Risk Management")
        lines.append("| Risk Parameter | Value | Operational Details |")
        lines.append("| :--- | :--- | :--- |")
        lines.append(f"| **Sizing Mode** | `{vol_mode}` | Mode: `CONTRACTS`, `MULTIPLIER`, or `MIN` |")
        lines.append(f"| **Trade Volume / Quantity** | `{vol_desc}` | Quantity committed per trade signal |")
        lines.append(f"| **Leverage Multiplier** | `{leverage}x` | Margin required = Position Notional / Leverage |")
        lines.append(f"| **Starting Capital** | `{summary.initial_balance_usdt:,.2f} USDT` | `₹{summary.initial_balance_inr:,.2f} INR` (`1 USDT = ₹{inr_rate:.2f}`) |")
        lines.append(f"| **Take Profit Target** | `+{tp_ticks} ticks` (`+{tp_ticks * pu:.{ps}f} USDT`) | Guaranteed Min-Profit TP (`entry + N*pu`) |")
        lines.append(f"| **Stop Loss Rule** | `{sl_desc}` | Stop loss evaluation logic |")
        lines.append("")

        lines.append("### Exchange Contract Specifications & Fees")
        lines.append("| Specification | Value | Notes |")
        lines.append("| :--- | :--- | :--- |")
        lines.append(f"| **Fee Schedule Mode** | `{fee_mode}` | Live KCEX API, 0.0% zero-fee pair, or manual rate |")
        lines.append(f"| **Maker Fee Rate** | `{m_fee_rate * 100.0:.4f}%` | Rate for passive limit orders |")
        lines.append(f"| **Taker Fee Rate** | `{t_fee_rate * 100.0:.4f}%` | Rate for aggressive market / stop triggers |")
        lines.append(f"| **Contract Size (cs)** | `{cs} {base_coin}` | 1 contract = {cs} underlying coin |")
        lines.append(f"| **Price Unit (pu / tick)** | `{pu}` | Minimum tick increment on order book |")
        lines.append(f"| **Price Precision** | `{ps} decimal places` | Precision formatting for quotes and orders |")
        lines.append(f"| **Min Volume** | `{min_vol} contract(s)` | Minimum permissible order size |")
        lines.append(f"| **Max Leverage** | `{max_lev}x` | Maximum allowed leverage on exchange |")
        lines.append("\n---\n")

        # 3. Trade Execution & Statistical Performance
        lines.append("## 📈 Trade Execution & Statistical Breakdown\n")
        lines.append("| Metric | Value | Context / Benchmark |")
        lines.append("| :--- | :--- | :--- |")
        lines.append(f"| **Total Trades Executed** | `{summary.total_trades}` | Total completed trade lifecycle events |")
        lines.append(f"| **Winning Trades** | `{summary.winning_trades}` | `{summary.win_rate_pct:.2f}%` of total trades |")
        lines.append(f"| **Losing Trades** | `{summary.losing_trades}` | `{(summary.losing_trades / summary.total_trades * 100) if summary.total_trades > 0 else 0.0:.2f}%` of total trades |")
        lines.append(f"| **Scratch / Break-even** | `{summary.scratch_trades}` | `{(summary.scratch_trades / summary.total_trades * 100) if summary.total_trades > 0 else 0.0:.2f}%` of total trades |")
        lines.append(f"| **Average Trade PnL** | `{summary.avg_trade_pnl_usdt:+,.4f} USDT` (`₹{summary.avg_trade_pnl_usdt * inr_rate:+,.2f}`) | Expected return per signal |")
        lines.append(f"| **Average Winning Trade** | `+{summary.avg_win_pnl_usdt:,.4f} USDT` | Average gain when trade hits TP |")
        lines.append(f"| **Average Losing Trade** | `-{summary.avg_loss_pnl_usdt:,.4f} USDT` | Average loss when trade hits SL |")

        win_trades = [o for o in outcomes if o.realized_pnl_usdt > 0]
        loss_trades = [o for o in outcomes if o.realized_pnl_usdt < 0]
        best_win = max(win_trades, key=lambda x: x.realized_pnl_usdt) if win_trades else None
        worst_loss = min(loss_trades, key=lambda x: x.realized_pnl_usdt) if loss_trades else None

        if best_win:
            b_pnl = f"+{best_win.realized_pnl_usdt:.4f} USDT (+{best_win.roe_percentage:.1f}% ROE)"
            lines.append(f"| **Largest Winning Trade** | `{b_pnl}` | Trade #{best_win.trade_id} ({best_win.direction.value}) |")
        else:
            lines.append(f"| **Largest Winning Trade** | `None` | Zero winning trades |")

        if worst_loss:
            w_pnl = f"{worst_loss.realized_pnl_usdt:.4f} USDT ({worst_loss.roe_percentage:.1f}% ROE)"
            lines.append(f"| **Largest Losing Trade** | `{w_pnl}` | Trade #{worst_loss.trade_id} ({worst_loss.direction.value}) |")
        else:
            lines.append(f"| **Largest Losing Trade** | `None` | Zero losing trades (100% Win Rate) |")

        lines.append(f"| **Max Consecutive Wins** | `{summary.max_consecutive_wins}` trades | Peak winning streak |")
        lines.append(f"| **Max Consecutive Losses** | `{summary.max_consecutive_losses}` trades | Peak losing streak |")
        lines.append(f"| **Average Trade Duration** | `{format_duration(summary.avg_duration_seconds)}` | Mean time from entry to exit fill |")
        if shortest_trade:
            lines.append(f"| **Fastest Trade Fill** | `{format_duration(shortest_trade.duration_seconds)}` | Trade #{shortest_trade.trade_id} |")
        if longest_trade:
            lines.append(f"| **Longest Trade In-Position** | `{format_duration(longest_trade.duration_seconds)}` | Trade #{longest_trade.trade_id} |")
        lines.append(f"| **Cumulative Time In Position** | `{format_duration(total_duration_in_pos)}` | Total market exposure duration |")
        lines.append("\n---\n")

        # 4. Directional Performance
        lines.append("## 🧭 Directional Performance Analysis (LONG vs SHORT)\n")
        lines.append("| Metric | LONG Trades | SHORT Trades | Combined Total |")
        lines.append("| :--- | :--- | :--- | :--- |")
        lines.append(f"| **Total Trades** | `{l_stats['count']}` ({l_stats['count']/max(1, summary.total_trades)*100:.1f}%) | `{s_stats['count']}` ({s_stats['count']/max(1, summary.total_trades)*100:.1f}%) | `{summary.total_trades}` |")
        lines.append(f"| **Wins / Losses** | `{l_stats['wins']} W / {l_stats['losses']} L` | `{s_stats['wins']} W / {s_stats['losses']} L` | `{summary.winning_trades} W / {summary.losing_trades} L` |")
        lines.append(f"| **Win Rate** | **`{l_stats['win_rate']:.2f}%`** | **`{s_stats['win_rate']:.2f}%`** | **`{summary.win_rate_pct:.2f}%`** |")
        lines.append(f"| **Gross Profit** | `+{l_stats['gross_profit']:.4f} USDT` | `+{s_stats['gross_profit']:.4f} USDT` | `+{summary.gross_profit_usdt:.4f} USDT` |")
        lines.append(f"| **Gross Loss** | `-{l_stats['gross_loss']:.4f} USDT` | `-{s_stats['gross_loss']:.4f} USDT` | `-{summary.gross_loss_usdt:.4f} USDT` |")
        lines.append(f"| **Net Realized PnL** | **`{l_stats['pnl']:+,.4f} USDT`** | **`{s_stats['pnl']:+,.4f} USDT`** | **`{summary.net_pnl_usdt:+,.4f} USDT`** |")
        lines.append(f"| **Net PnL (INR)** | `₹{l_stats['pnl_inr']:+,.2f}` | `₹{s_stats['pnl_inr']:+,.2f}` | `₹{summary.net_pnl_inr:+,.2f}` |")
        lines.append(f"| **Profit Factor** | `{l_stats['pf']:.2f}` | `{s_stats['pf']:.2f}` | `{summary.profit_factor:.2f}` |")
        lines.append("\n---\n")

        # 5. Exit Reason Attribution
        lines.append("## 🎯 Exit Reason & Outcome Attribution\n")
        lines.append("| Exit Reason Trigger | Count | % of Trades | Total PnL (USDT) | Total PnL (INR) | Win Rate | Avg Duration |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for r_name, count in summary.exit_reasons.items():
            r_outcomes = [o for o in outcomes if (o.exit_reason.value if hasattr(o.exit_reason, "value") else str(o.exit_reason)) == r_name]
            r_pnl = sum(o.realized_pnl_usdt for o in r_outcomes)
            r_pnl_inr = r_pnl * inr_rate
            r_wins = sum(1 for o in r_outcomes if o.realized_pnl_usdt > 0)
            r_wr = (r_wins / len(r_outcomes) * 100.0) if r_outcomes else 0.0
            r_dur = (sum(o.duration_seconds for o in r_outcomes) / len(r_outcomes)) if r_outcomes else 0.0
            pct = (count / summary.total_trades * 100.0) if summary.total_trades > 0 else 0.0
            lines.append(f"| `{r_name}` | `{count}` | `{pct:.1f}%` | `{r_pnl:+,.4f} USDT` | `₹{r_pnl_inr:+,.2f}` | `{r_wr:.1f}%` | `{format_duration(r_dur)}` |")
        lines.append("\n---\n")

        # 6. Detailed Trade Journal Table
        lines.append("## 📜 Detailed Trade Journal\n")
        if not outcomes:
            lines.append("*No trades executed during this backtesting run.*\n")
        else:
            lines.append("| # | Dir | Entry Time (UTC) | Exit Time (UTC) | Duration | Entry Price | Exit Price | Notional | Margin | Fee (USDT) | Net PnL (USDT) | ROE % | Exit Reason | Ending Balance |")
            lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")

            def format_row(o: TradeOutcome) -> str:
                dir_str = o.direction.value if hasattr(o.direction, "value") else str(o.direction)
                o_time = format_ms_to_utc(int(o.open_time * 1000))
                c_time = format_ms_to_utc(int(o.close_time * 1000))
                dur = format_duration(o.duration_seconds)
                pnl = f"{o.realized_pnl_usdt:+,.4f}"
                roe = f"{o.roe_percentage:+.1f}%"
                r_str = o.exit_reason.value if hasattr(o.exit_reason, "value") else str(o.exit_reason)
                bal = f"{o.balance_after_trade_usdt:,.4f}" if o.balance_after_trade_usdt else "—"
                return (
                    f"| {o.trade_id} | `{dir_str}` | {o_time} | {c_time} | {dur} | "
                    f"`{o.entry_price:.{ps}f}` | `{o.exit_price:.{ps}f}` | ${o.notional_value_usdt:.2f} | "
                    f"${o.margin_used_usdt:.2f} | ${o.fee_total_usdt:.6f} | **{pnl}** | `{roe}` | `{r_str}` | ${bal} |"
                )

            if len(outcomes) <= 100:
                for o in outcomes:
                    lines.append(format_row(o))
            else:
                # Show first 25
                for o in outcomes[:25]:
                    lines.append(format_row(o))
                lines.append(f"| ... | ... | *({len(outcomes) - 50} intermediate trades logged in full .csv report)* | ... | ... | ... | ... | ... | ... | ... | ... | ... | ... | ... |")
                # Show last 25
                for o in outcomes[-25:]:
                    lines.append(format_row(o))

            lines.append(f"\n> 💡 *Full granular dataset with all {len(outcomes)} trades is stored in the accompanying `trades.csv` and `trades.jsonl` artifacts.*\n")

        return "\n".join(lines)

    def export_all(
        self,
        outcomes: List[TradeOutcome],
        summary: PerformanceSummary,
        config: Optional[Any] = None,
        contract: Optional[Any] = None,
        prefix: str = "backtest"
    ) -> Dict[str, str]:
        """
        Exports full trade journals to CSV, JSONL, and summary Markdown.
        """
        now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        run_name = f"{prefix}_{summary.symbol}_{now_str}"

        csv_path = os.path.join(self.reports_dir, f"{run_name}_trades.csv")
        jsonl_path = os.path.join(self.reports_dir, f"{run_name}_trades.jsonl")
        md_path = os.path.join(self.reports_dir, f"{run_name}_summary.md")

        # 1. Export CSV
        if outcomes:
            keys = [
                "trade_id", "symbol", "direction", "sub_strategy_name", "vol_contracts",
                "underlying_quantity", "entry_price", "exit_price", "min_profit_tp_price",
                "stop_loss_price", "open_time", "close_time", "duration_seconds",
                "notional_value_usdt", "margin_used_usdt", "fee_total_usdt",
                "realized_pnl_usdt", "realized_pnl_inr", "roe_percentage", "exit_reason",
                "balance_after_trade_usdt"
            ]
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(keys)
                for o in outcomes:
                    row = [
                        o.trade_id,
                        o.symbol,
                        o.direction.value if hasattr(o.direction, "value") else str(o.direction),
                        o.sub_strategy_name,
                        o.vol_contracts,
                        o.underlying_quantity,
                        o.entry_price,
                        o.exit_price,
                        o.min_profit_tp_price,
                        o.stop_loss_price,
                        format_ms_to_utc(int(o.open_time * 1000)),
                        format_ms_to_utc(int(o.close_time * 1000)),
                        round(o.duration_seconds, 2),
                        round(o.notional_value_usdt, 4),
                        round(o.margin_used_usdt, 4),
                        round(o.fee_total_usdt, 6),
                        round(o.realized_pnl_usdt, 6),
                        round(o.realized_pnl_inr, 2),
                        round(o.roe_percentage, 2),
                        o.exit_reason.value if hasattr(o.exit_reason, "value") else str(o.exit_reason),
                        round(o.balance_after_trade_usdt or 0.0, 4)
                    ]
                    writer.writerow(row)

        # 2. Export JSONL
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for o in outcomes:
                d = {
                    "trade_id": o.trade_id,
                    "symbol": o.symbol,
                    "direction": o.direction.value if hasattr(o.direction, "value") else str(o.direction),
                    "sub_strategy_name": o.sub_strategy_name,
                    "entry_price": o.entry_price,
                    "exit_price": o.exit_price,
                    "min_profit_tp_price": o.min_profit_tp_price,
                    "stop_loss_price": o.stop_loss_price,
                    "open_time_utc": format_ms_to_utc(int(o.open_time * 1000)),
                    "close_time_utc": format_ms_to_utc(int(o.close_time * 1000)),
                    "duration_seconds": o.duration_seconds,
                    "realized_pnl_usdt": o.realized_pnl_usdt,
                    "realized_pnl_inr": o.realized_pnl_inr,
                    "roe_percentage": o.roe_percentage,
                    "exit_reason": o.exit_reason.value if hasattr(o.exit_reason, "value") else str(o.exit_reason),
                    "balance_after_trade_usdt": o.balance_after_trade_usdt
                }
                f.write(json.dumps(d) + "\n")

        # 3. Export Comprehensive Markdown Summary
        detailed_md = self.generate_detailed_markdown(
            outcomes=outcomes,
            summary=summary,
            config=config,
            contract=contract
        )
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(detailed_md)

        print(f"[+] Reports generated successfully in '{self.reports_dir}':")
        print(f"    - CSV:  {csv_path}")
        print(f"    - JSON: {jsonl_path}")
        print(f"    - MD:   {md_path}")

        # 4. Auto-Index for Comparison & Analytics Studio
        try:
            from BACKTESTER.analytics.indexer import ReportIndexer
            indexer = ReportIndexer(reports_dir=self.reports_dir)
            indexer.get_all_runs(force_reindex=True)
            print(f"    - Studio: Cached for instant interactive comparison\n")
        except Exception:
            print("")

        return {
            "csv": csv_path,
            "jsonl": jsonl_path,
            "markdown": md_path
        }

