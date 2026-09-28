"""
Deep Analytics & Interactive Comparison Engine
=============================================
Calculates multi-run parameter diffs, dynamic factor scorecards, normalized
equity overlays, 6-pillar radar footprints, and paged trade queries.
"""

import os
import csv
import math
from typing import List, Dict, Any, Optional, Tuple

from BACKTESTER.analytics.models import BacktestRunRecord
from BACKTESTER.analytics.indexer import ReportIndexer


METRIC_DEFINITIONS = {
    # Financial Factors
    "net_pnl_usdt": {"name": "Net Realized PnL (USDT)", "category": "Financial", "higher_is_better": True, "format": "currency"},
    "net_pnl_inr": {"name": "Net Realized PnL (INR)", "category": "Financial", "higher_is_better": True, "format": "inr"},
    "net_roi_pct": {"name": "Net Return (ROI %)", "category": "Financial", "higher_is_better": True, "format": "pct"},
    "profit_factor": {"name": "Profit Factor", "category": "Financial", "higher_is_better": True, "format": "float2"},
    "gross_profit_usdt": {"name": "Gross Profit (USDT)", "category": "Financial", "higher_is_better": True, "format": "currency"},
    "gross_loss_usdt": {"name": "Gross Loss (USDT)", "category": "Financial", "higher_is_better": False, "format": "currency"},
    "total_fees_usdt": {"name": "Total Fees Paid (USDT)", "category": "Financial", "higher_is_better": False, "format": "fee"},
    "final_balance_usdt": {"name": "Final Balance (USDT)", "category": "Financial", "higher_is_better": True, "format": "currency"},

    # Risk Factors
    "max_drawdown_pct": {"name": "Max Drawdown (%)", "category": "Risk", "higher_is_better": False, "format": "pct"},
    "max_drawdown_usdt": {"name": "Max Drawdown (USDT)", "category": "Risk", "higher_is_better": False, "format": "currency"},
    "sharpe_ratio": {"name": "Sharpe Ratio (est)", "category": "Risk", "higher_is_better": True, "format": "float2"},
    "sortino_ratio": {"name": "Sortino Ratio", "category": "Risk", "higher_is_better": True, "format": "float2"},
    "calmar_ratio": {"name": "Calmar Ratio", "category": "Risk", "higher_is_better": True, "format": "float2"},
    "win_loss_payoff": {"name": "Win / Loss Payoff", "category": "Risk", "higher_is_better": True, "format": "float2"},

    # Execution Factors
    "total_trades": {"name": "Total Trades Executed", "category": "Execution", "higher_is_better": None, "format": "int"},
    "win_rate_pct": {"name": "Win Rate (%)", "category": "Execution", "higher_is_better": True, "format": "pct"},
    "avg_trade_pnl_usdt": {"name": "Avg Trade Return (USDT)", "category": "Execution", "higher_is_better": True, "format": "currency_sub"},
    "avg_win_pnl_usdt": {"name": "Avg Winning Trade", "category": "Execution", "higher_is_better": True, "format": "currency_sub"},
    "avg_loss_pnl_usdt": {"name": "Avg Losing Trade", "category": "Execution", "higher_is_better": False, "format": "currency_sub"},
    "avg_duration_seconds": {"name": "Avg Trade Duration", "category": "Execution", "higher_is_better": None, "format": "duration"},
    "max_consecutive_wins": {"name": "Max Consecutive Wins", "category": "Execution", "higher_is_better": True, "format": "int"},
    "max_consecutive_losses": {"name": "Max Consecutive Losses", "category": "Execution", "higher_is_better": False, "format": "int"},

    # Directional Factors
    "long_win_rate_pct": {"name": "LONG Win Rate (%)", "category": "Directional", "higher_is_better": True, "format": "pct"},
    "short_win_rate_pct": {"name": "SHORT Win Rate (%)", "category": "Directional", "higher_is_better": True, "format": "pct"},
    "long_net_pnl_usdt": {"name": "LONG Net Realized PnL", "category": "Directional", "higher_is_better": True, "format": "currency"},
    "short_net_pnl_usdt": {"name": "SHORT Net Realized PnL", "category": "Directional", "higher_is_better": True, "format": "currency"},
}

PARAMETER_KEYS = [
    ("symbol", "Trading Pair Symbol"),
    ("strategy", "Strategy Evaluated"),
    ("strategy_preset", "Strategy Preset"),
    ("timeframe", "Candle Timeframe"),
    ("date_range", "Evaluation Date Window"),
    ("start_date", "Backtest Start Date"),
    ("end_date", "Backtest End Date"),
    ("leverage", "Leverage Multiplier"),
    ("tp_ticks", "Take Profit (ticks)"),
    ("sl_rule_desc", "Stop Loss Rule"),
    ("sizing_mode", "Sizing Mode"),
    ("contracts", "Contracts per Trade"),
    ("starting_capital_usdt", "Initial Capital (USDT)"),
    ("slippage_ticks", "Slippage (ticks)"),
    ("high_fidelity_ticks", "Tick Trade Matching"),
    ("fee_mode", "Fee Schedule Mode"),
    ("maker_fee_pct", "Maker Fee Rate (%)"),
    ("taker_fee_pct", "Taker Fee Rate (%)"),
    ("contract_size", "Contract Size (cs)"),
    ("price_unit", "Price Unit (pu / tick)"),
    ("price_precision", "Price Precision"),
    ("min_volume", "Min Order Volume"),
    ("max_leverage", "Max Exchange Leverage"),
    # Indicator Hyperparameters:
    ("param_ema_fast", "Fast EMA Period"),
    ("param_ema_slow", "Slow EMA Period"),
    ("param_stoch_rsi_period", "Stoch RSI Period"),
    ("param_stoch_period", "Stoch Lookback Period"),
    ("param_stoch_k_period", "Stoch %K Smoothing"),
    ("param_stoch_d_period", "Stoch %D Smoothing"),
    ("param_stoch_oversold", "Stoch Oversold Threshold"),
    ("param_stoch_overbought", "Stoch Overbought Threshold"),
    ("param_candle_close_confirmation", "Candle Close Confirmation"),
    # Trade Optimization & Regime Filters:
    ("filter_duration", "Duration Time-Stop Filter"),
    ("filter_deep_monitor_s", "Duration Deep Monitor Threshold"),
    ("filter_max_hold_s", "Duration Max Hold / Exit Threshold"),
    ("filter_duration_action", "Duration Exit Action"),
    ("filter_adx", "ADX Regime Filter"),
    ("filter_adx_threshold", "ADX Threshold"),
    ("filter_htf_trend", "HTF Trend 200 EMA Filter"),
    ("filter_hourly", "Hourly Session Filter"),
    ("filter_direction_bias", "Directional Bias Policy"),
]


class AnalyticsEngine:
    """
    High-level analytics and comparison processor.
    """

    def __init__(self, indexer: Optional[ReportIndexer] = None):
        self.indexer = indexer or ReportIndexer()

    def get_all_factors(self) -> List[Dict[str, Any]]:
        """Returns catalogue of all factors available for interactive comparison."""
        factors = []
        for k, v in METRIC_DEFINITIONS.items():
            factors.append({
                "key": k,
                "name": v["name"],
                "category": v["category"],
                "higher_is_better": v["higher_is_better"],
                "format": v["format"]
            })
        return factors

    def compare_runs(
        self,
        run_ids: List[str],
        selected_factors: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Executes a multi-run comparative matrix analysis across the requested runs.
        """
        all_runs = self.indexer.get_all_runs()
        runs_map = {r.metadata.run_id: r for r in all_runs}

        # Filter target runs maintaining input order
        target_runs: List[BacktestRunRecord] = []
        for rid in run_ids:
            if rid in runs_map:
                target_runs.append(runs_map[rid])

        if not target_runs:
            return {"error": "No valid runs found matching the requested IDs."}

        factors_to_use = selected_factors or list(METRIC_DEFINITIONS.keys())

        # Build comparison sub-components
        comparison_matrix = self._build_comparison_matrix(target_runs, factors_to_use)
        parameter_matrix = self._build_parameter_diffs(target_runs)
        equity_overlays = self._build_equity_overlays(target_runs)
        radar_footprints = self._build_radar_footprints(target_runs)
        exit_comparison = self._build_exit_comparison(target_runs)

        return {
            "run_ids": [r.metadata.run_id for r in target_runs],
            "runs_meta": [r.metadata.to_dict() for r in target_runs],
            "comparison_matrix": comparison_matrix,
            "parameter_diffs": parameter_matrix,
            "equity_overlays": equity_overlays,
            "radar_footprints": radar_footprints,
            "exit_comparison": exit_comparison,
        }

    def _build_parameter_diffs(self, runs: List[BacktestRunRecord]) -> List[Dict[str, Any]]:
        """Identifies which parameters vary and which are constants across runs."""
        diffs = []
        for key, display_name in PARAMETER_KEYS:
            values = {}
            unique_vals = set()
            has_meaningful_val = False

            for r in runs:
                val = None
                if key.startswith("param_"):
                    p_key = key.replace("param_", "")
                    val = r.metadata.parameters.get(p_key) if r.metadata.parameters else None
                elif key.startswith("filter_"):
                    f_key = key.replace("filter_", "")
                    val = r.metadata.filters.get(f_key) if r.metadata.filters else None
                else:
                    val = getattr(r.metadata, key, None)

                if val is not None and val != "":
                    has_meaningful_val = True

                if key in ("leverage", "max_leverage"):
                    val_str = f"{val}x" if val is not None else "—"
                elif key == "tp_ticks":
                    val_str = f"+{val} pu ticks" if val is not None else "—"
                elif key in ("contracts", "min_volume"):
                    val_str = f"{val} contract(s)" if val is not None else "—"
                elif key == "starting_capital_usdt":
                    val_str = f"${float(val):.2f} USDT" if val is not None else "—"
                elif key in ("maker_fee_pct", "taker_fee_pct"):
                    val_str = f"{float(val):.4f}%" if val is not None else "—"
                elif key == "contract_size":
                    val_str = f"{val} {r.metadata.base_asset}" if val is not None else "—"
                elif key == "price_precision":
                    val_str = f"{val} decimals" if val is not None else "—"
                elif isinstance(val, bool):
                    val_str = "ENABLED" if val else "DISABLED"
                elif val is None or val == "":
                    val_str = "—"
                else:
                    val_str = str(val)

                values[r.metadata.run_id] = val_str
                unique_vals.add(val_str)

            # Filter out irrelevant indicators if no runs use them
            always_keep = (
                "symbol", "strategy", "strategy_preset", "timeframe", "date_range",
                "start_date", "end_date", "leverage", "tp_ticks", "sl_rule_desc",
                "sizing_mode", "contracts", "starting_capital_usdt", "high_fidelity_ticks",
                "slippage_ticks", "fee_mode", "contract_size", "price_unit"
            )
            if not has_meaningful_val and key not in always_keep:
                continue

            if "ema" in key and not any("EMA" in r.metadata.strategy.upper() for r in runs):
                continue
            if "stoch" in key and not any("STOCH" in r.metadata.strategy.upper() for r in runs):
                continue

            is_different = len(unique_vals) > 1
            diffs.append({
                "key": key,
                "name": display_name,
                "values": values,
                "is_diff": is_different,
                "unique_count": len(unique_vals)
            })

        # Sort with varied tweaks at the very top!
        diffs.sort(key=lambda x: (not x["is_diff"], x["name"]))
        return diffs

    def _build_comparison_matrix(
        self,
        runs: List[BacktestRunRecord],
        selected_factors: List[str]
    ) -> List[Dict[str, Any]]:
        """Constructs matrix of selected metrics, identifying best performer in each."""
        matrix = []

        for f_key in selected_factors:
            if f_key not in METRIC_DEFINITIONS:
                continue
            f_def = METRIC_DEFINITIONS[f_key]
            higher_is_better = f_def["higher_is_better"]

            row_values: Dict[str, Any] = {}
            raw_nums: List[Tuple[str, float]] = []

            for r in runs:
                # Extract value from scorecard or directional
                rid = r.metadata.run_id
                if hasattr(r.scorecard, f_key):
                    val = getattr(r.scorecard, f_key)
                elif hasattr(r.directional, f_key):
                    val = getattr(r.directional, f_key)
                else:
                    val = 0.0

                row_values[rid] = val
                if isinstance(val, (int, float)) and not math.isnan(val):
                    raw_nums.append((rid, float(val)))

            # Determine best performer
            best_run_id = None
            if raw_nums and higher_is_better is not None:
                if higher_is_better:
                    best_run_id = max(raw_nums, key=lambda x: x[1])[0]
                else:
                    best_run_id = min(raw_nums, key=lambda x: x[1])[0]

            matrix.append({
                "key": f_key,
                "name": f_def["name"],
                "category": f_def["category"],
                "higher_is_better": higher_is_better,
                "format": f_def["format"],
                "values": row_values,
                "best_run_id": best_run_id,
            })

        return matrix

    def _build_equity_overlays(self, runs: List[BacktestRunRecord]) -> Dict[str, Any]:
        """Aligns downsampled equity curves for synchronized multi-curve charting."""
        series = []

        for r in runs:
            curve = self.indexer.get_downsampled_curve(r.metadata.run_id)
            if not curve:
                continue

            points = []
            for p in curve:
                points.append({
                    "time": p["time_utc"],
                    "balance": p["balance_usdt"],
                    "roi_pct": p["roi_pct"],
                    "drawdown_pct": p["drawdown_pct"],
                    "trade_id": p["trade_id"]
                })

            series.append({
                "run_id": r.metadata.run_id,
                "run_name": r.metadata.run_name,
                "symbol": r.metadata.symbol,
                "strategy": r.metadata.strategy,
                "points": points
            })

        return {"series": series}

    def _build_radar_footprints(self, runs: List[BacktestRunRecord]) -> Dict[str, Any]:
        """
        Computes 0-100 normalized scores across 6 key strategy dimensions:
        1. Win Rate Edge
        2. Profit Factor
        3. Drawdown Defense (inverse of drawdown)
        4. Risk-Adjusted Alpha (Sharpe/Sortino)
        5. Payoff Quality (Win/Loss Payoff)
        6. Fee Drag Resistance
        """
        dimensions = [
            "Win Rate Edge",
            "Profit Factor",
            "Drawdown Defense",
            "Risk-Adjusted Alpha",
            "Payoff Quality",
            "Fee Drag Resistance"
        ]

        series = []
        for r in runs:
            sc = r.scorecard

            # 1. Win Rate Edge (0% -> 0, 50% -> 50, 80%+ -> 85-100)
            score_wr = min(100.0, max(0.0, sc.win_rate_pct))

            # 2. Profit Factor (0.0 -> 0, 1.0 -> 50, 2.0+ -> 100)
            score_pf = min(100.0, max(0.0, sc.profit_factor * 50.0))

            # 3. Drawdown Defense (0% DD -> 100, 20% DD -> 80, 100%+ DD -> 0)
            score_dd = min(100.0, max(0.0, 100.0 - min(100.0, sc.max_drawdown_pct)))

            # 4. Risk-Adjusted Alpha (Sharpe: -2 -> 0, 0 -> 40, 2.0+ -> 100)
            score_alpha = min(100.0, max(0.0, (sc.sharpe_ratio + 2.0) * 25.0))

            # 5. Payoff Quality (0.0 -> 0, 1.0 -> 50, 2.0+ -> 100)
            score_payoff = min(100.0, max(0.0, sc.win_loss_payoff * 50.0))

            # 6. Fee Drag Resistance (fees as % of capital: 0% -> 100, 10%+ -> 0)
            fee_ratio = (sc.total_fees_usdt / sc.initial_capital_usdt * 100.0) if sc.initial_capital_usdt > 0 else 0.0
            score_fees = min(100.0, max(0.0, 100.0 - fee_ratio * 10.0))

            series.append({
                "run_id": r.metadata.run_id,
                "run_name": r.metadata.run_name,
                "scores": [
                    round(score_wr, 1),
                    round(score_pf, 1),
                    round(score_dd, 1),
                    round(score_alpha, 1),
                    round(score_payoff, 1),
                    round(score_fees, 1),
                ]
            })

        return {
            "dimensions": dimensions,
            "series": series
        }

    def _build_exit_comparison(self, runs: List[BacktestRunRecord]) -> Dict[str, Any]:
        """Aggregates exit attribution counts and percentages across runs."""
        all_reasons = set()
        for r in runs:
            for ea in r.exit_attributions:
                all_reasons.add(ea.reason)

        reason_list = sorted(list(all_reasons))
        series = []

        for r in runs:
            counts = {ea.reason: ea.count for ea in r.exit_attributions}
            pcts = {ea.reason: ea.pct_of_trades for ea in r.exit_attributions}
            series.append({
                "run_id": r.metadata.run_id,
                "run_name": r.metadata.run_name,
                "counts": [counts.get(reason, 0) for reason in reason_list],
                "percentages": [pcts.get(reason, 0.0) for reason in reason_list],
            })

        return {
            "reasons": reason_list,
            "series": series
        }

    def get_paged_trades(
        self,
        run_id: str,
        page: int = 1,
        page_size: int = 50,
        direction: Optional[str] = None,
        exit_reason: Optional[str] = None,
        search: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Streams trades from CSV with low memory consumption and server-side filtering.
        """
        csv_path = os.path.join(self.indexer.reports_dir, f"{run_id}_trades.csv")
        if not os.path.exists(csv_path):
            return {"trades": [], "total_count": 0, "page": page, "total_pages": 0}

        matched_trades = []
        try:
            with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Filter direction
                    if direction and direction.upper() != "ALL":
                        if row.get("direction", "").upper() != direction.upper():
                            continue

                    # Filter exit reason
                    if exit_reason and exit_reason.upper() != "ALL":
                        if exit_reason.upper() not in row.get("exit_reason", "").upper():
                            continue

                    # Search text
                    if search:
                        q = search.lower()
                        match = False
                        for val in row.values():
                            if q in str(val).lower():
                                match = True
                                break
                        if not match:
                            continue

                    matched_trades.append(row)
        except Exception as e:
            return {"error": str(e), "trades": [], "total_count": 0}

        total_count = len(matched_trades)
        total_pages = max(1, math.ceil(total_count / page_size))
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_trades = matched_trades[start_idx:end_idx]

        return {
            "trades": page_trades,
            "total_count": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages
        }
