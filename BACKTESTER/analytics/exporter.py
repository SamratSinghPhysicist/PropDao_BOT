"""
AI Deep-Analytics Exporter & Dossier Generator
=============================================
Generates comprehensive, AI-optimized analytical dossiers (in Markdown and JSON)
specifically structured for LLM quantitative reasoning, parameter sensitivity
analysis, risk profiling, and strategy optimization suggestions.
"""

import os
import json
import datetime
from typing import List, Dict, Any, Optional

from BACKTESTER.analytics.models import BacktestRunRecord
from BACKTESTER.analytics.engine import AnalyticsEngine, METRIC_DEFINITIONS


class AIDossierExporter:
    """
    Exports comprehensive backtest analytics formatted specifically for AI consumption.
    """

    def __init__(self, engine: Optional[AnalyticsEngine] = None):
        self.engine = engine or AnalyticsEngine()

    def export_single_markdown(self, run: BacktestRunRecord) -> str:
        """Generates an extensive markdown dossier for a single run optimized for AI analysis."""
        m = run.metadata
        sc = run.scorecard
        d = run.directional
        det = run.detailed
        now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        lines = [
            f"# 🤖 AI Quantitative Analysis Dossier: {m.symbol} — {m.strategy}",
            f"> **Generated for Deep AI Analysis:** `{now_utc}`",
            f"> **Backtest Engine:** `KCEX High-Fidelity Dual-Feed Engine v2.0` | **Run ID:** `{m.run_id}`\n",
            "---",
            "\n## 🧠 System Context for AI Quantitative Analyst",
            "You are an elite hedge-fund quantitative trader and risk management specialist.",
            "Analyze this crypto futures backtest report in exhaustive depth. Evaluate strategy robustness,",
            "holding period edge, tail-risk, directional skew (long vs short), fee drag, and parameter fragility.",
            "Provide concrete, mathematically justified optimization recommendations.\n",
            "---",
            "\n## 1. ⚙️ Hyperparameters & Configuration",
            "### General Execution & Market Setup",
            "| Parameter | Value | Notes / Operational Meaning |",
            "| :--- | :--- | :--- |",
            f"| **Trading Pair Symbol** | `{m.symbol}` | Base: `{m.base_asset}` / Quote: `{m.quote_asset}` |",
            f"| **Strategy Evaluated** | `{m.strategy}` | {m.strategy_desc or m.strategy} |",
            f"| **Strategy Preset** | `{m.strategy_preset or (m.parameters.get('preset') if m.parameters else 'STANDARD')}` | Calibrated preset profile |",
            f"| **Candle Timeframe** | `{m.timeframe}` | Granularity for indicator calculations |",
            f"| **Leverage Multiplier** | `{m.leverage}x` | Isolated margin trading leverage |",
            f"| **Take Profit Rule** | `{m.tp_target_desc or f'+{m.tp_ticks} ticks'}` | Min-profit target formula |",
            f"| **Stop Loss Rule** | `{m.sl_rule_desc or f'{m.sl_mode} {m.sl_value}'}` | Stop loss evaluation logic |",
            f"| **Position Sizing** | `{m.volume_desc or f'{m.contracts} contract(s)'}` | Mode: `{m.sizing_mode}` |",
            f"| **Historical Window** | `{m.date_range}` | Start: `{m.start_date}` → End: `{m.end_date}` |",
            f"| **Starting Capital** | `${m.starting_capital_usdt:.2f} USDT` | ₹{m.starting_capital_inr:.2f} INR (Rate: ₹94.45) |",
            f"| **Tick Simulation** | `{'ENABLED (Tick-Level)' if m.high_fidelity_ticks else 'DISABLED (Candle)'}` | High-fidelity millisecond fills |",
            f"| **Slippage Tolerance** | `{m.slippage_ticks} ticks` | Fill penalty applied to orders |",
            f"| **Fee Schedule Mode** | `{m.fee_mode} Mode` | Maker: `{m.maker_fee_pct:.4f}%` / Taker: `{m.taker_fee_pct:.4f}%` |",
            f"| **Contract Size (cs)** | `{m.contract_size}` | 1 contract = {m.contract_size} {m.base_asset} |",
            f"| **Price Unit (pu / tick)** | `{m.price_unit}` | Minimum orderbook tick increment |",
            f"| **Price Precision** | `{m.price_precision} decimals` | Quote price precision |",
            f"| **Min / Max Leverage** | `{m.min_volume} contracts min / {m.max_leverage}x max lev` | Exchange order constraints |",
            "",
            "### Strategy & Indicator Detailed Parameters",
            "| Indicator Parameter | Value | Functional Role |",
            "| :--- | :--- | :--- |"
        ]

        if m.parameters:
            for p_k, p_v in m.parameters.items():
                lines.append(f"| **{p_k.replace('_', ' ').title()}** | `{p_v}` | Active model configuration |")
        else:
            lines.append(f"| **Default Calibration** | `Standard Preset: {m.strategy_preset or 'DEFAULT'}` | Factory settings |")

        lines.extend([
            "",
            "### Trade Optimization & Regime Filters",
            "| Filter Dimension | Value | Operational Trigger / Safeguard |",
            "| :--- | :--- | :--- |"
        ])
        if m.filters:
            for f_k, f_v in m.filters.items():
                lines.append(f"| **{f_k.replace('_', ' ').title()}** | `{'ENABLED' if f_v is True else ('DISABLED' if f_v is False else f_v)}` | Guardrail condition |")
        else:
            lines.append("| **Filters Status** | `Unconfigured / Default` | Standard execution without additional filters |")

        lines.extend([
            "\n---",
            "\n## 2. ⚡ Executive Performance Scorecard",
            "| Metric | Value | Baseline / Context |",
            "| :--- | :--- | :--- |",
            f"| **Initial Capital** | `{sc.initial_capital_usdt:,.4f} USDT` (`₹{sc.initial_capital_usdt * 94.45:,.2f}`) | Baseline Capital |",
            f"| **Final Balance** | `{sc.final_balance_usdt:,.4f} USDT` (`₹{sc.final_balance_usdt * 94.45:,.2f}`) | Ending Realized Capital |",
            f"| **Net Realized PnL** | **`{sc.net_pnl_usdt:+,.4f} USDT`** (`₹{sc.net_pnl_inr:+,.2f}`) | **`{sc.net_roi_pct:+.2f}% Net ROI`** |",
            f"| **Gross Profit** | `+{sc.gross_profit_usdt:,.4f} USDT` | Sum of all winning trade gains |",
            f"| **Gross Loss** | `-{sc.gross_loss_usdt:,.4f} USDT` | Sum of all losing trade drawdowns |",
            f"| **Profit Factor** | **`{sc.profit_factor:.2f}`** | Ratio of Gross Profit to Gross Loss |",
            f"| **Win / Loss Payoff** | `{sc.win_loss_payoff:.2f}` | Avg Win vs Avg Loss size ratio |",
            f"| **Win Rate** | **`{sc.win_rate_pct:.2f}%`** | `{sc.winning_trades:,} Wins / {sc.losing_trades:,} Losses` |",
            f"| **Max Drawdown (Peak-to-Trough)** | **`-{sc.max_drawdown_pct:.2f}%`** (`-{sc.max_drawdown_usdt:,.4f} USDT`) | Downside capital impairment |",
            f"| **Sharpe Ratio (est)** | `{sc.sharpe_ratio:.2f}` | Risk-adjusted excess return |",
            f"| **Sortino Ratio** | `{sc.sortino_ratio:.2f}` | Downside-adjusted volatility ratio |",
            f"| **Calmar Ratio** | `{sc.calmar_ratio:.2f}` | Net ROI / Max Drawdown ratio |",
            f"| **Total Taker Fees Paid** | `{sc.total_fees_usdt:,.6f} USDT` | Total exchange fee drag |",
            "\n---",
            "\n## 3. 📈 Trade Execution & Streak Statistics",
            "| Execution Statistic | Value | Analysis Context |",
            "| :--- | :--- | :--- |",
            f"| **Total Trades Executed** | `{sc.total_trades:,}` | Total round-trip signals filled |",
            f"| **Average Trade PnL** | `{sc.avg_trade_pnl_usdt:+,.4f} USDT` | Expected value per trade signal |",
            f"| **Average Winning Trade** | `+{sc.avg_win_pnl_usdt:,.4f} USDT` | Average reward on TP target hit |",
            f"| **Average Losing Trade** | `-{sc.avg_loss_pnl_usdt:,.4f} USDT` | Average penalty on SL hit |",
            f"| **Max Consecutive Wins** | `{sc.max_consecutive_wins}` trades | Peak winning streak |",
            f"| **Max Consecutive Losses** | `{sc.max_consecutive_losses}` trades | Peak losing streak (cluster risk) |",
            f"| **Average Trade Duration** | `{sc.avg_duration_seconds:.1f} seconds` | Mean time from entry to exit fill |",
            "\n---",
            "\n## 4. 🧭 Directional Performance Analysis (LONG vs SHORT)",
            "| Metric | LONG Signals | SHORT Signals | Asymmetry Analysis |",
            "| :--- | :--- | :--- | :--- |",
            f"| **Total Trades** | `{d.long_trades:,}` | `{d.short_trades:,}` | LONG vs SHORT signal frequency |",
            f"| **Win Rate** | **`{d.long_win_rate_pct:.2f}%`** | **`{d.short_win_rate_pct:.2f}%`** | Directional accuracy edge |",
            f"| **Gross Profit** | `+{d.long_gross_profit:,.4f} USDT` | `+{d.short_gross_profit:,.4f} USDT` | Total positive returns |",
            f"| **Gross Loss** | `-{d.long_gross_loss:,.4f} USDT` | `-{d.short_gross_loss:,.4f} USDT` | Total negative returns |",
            f"| **Net Realized PnL** | **`{d.long_net_pnl_usdt:+,.4f} USDT`** | **`{d.short_net_pnl_usdt:+,.4f} USDT`** | Net directional contribution |",
            f"| **Profit Factor** | `{d.long_profit_factor:.2f}` | `{d.short_profit_factor:.2f}` | Long PF vs Short PF |",
            "\n---",
            "\n## 5. 🎯 Exit Reasons & Trigger Attribution",
            "| Exit Trigger Reason | Trades Count | % of Total | Net PnL (USDT) | Win Rate | Avg Duration |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |"
        ])

        for ea in run.exit_attributions:
            lines.append(
                f"| `{ea.reason}` | `{ea.count:,}` | `{ea.pct_of_trades:.1f}%` | `{ea.total_pnl_usdt:+,.4f}` | `{ea.win_rate_pct:.1f}%` | `{ea.avg_duration_seconds:.1f}s` |"
            )

        # 6. Duration Buckets
        if det and det.duration_buckets:
            lines.extend([
                "\n---",
                "\n## 6. ⏱️ Trade Holding Duration Distribution",
                "| Duration Bucket | Trades Count | % of Volume | Win Rate | Net PnL (USDT) |",
                "| :--- | :--- | :--- | :--- | :--- |"
            ])
            for b_name, b_val in det.duration_buckets.items():
                pct = (b_val["count"] / sc.total_trades * 100.0) if sc.total_trades > 0 else 0.0
                lines.append(
                    f"| `{b_name}` | `{b_val['count']:,}` | `{pct:.1f}%` | `{b_val.get('win_rate_pct', 0.0):.1f}%` | `{b_val['pnl']:+,.4f}` |"
                )

        # 7. Hourly Heatmap (Top/Worst Sessions)
        if det and det.hourly_distribution:
            lines.extend([
                "\n---",
                "\n## 7. 🌍 24-Hour UTC Performance Distribution",
                "| Hour (UTC) | Trades Count | Win Rate (%) | Net Realized PnL (USDT) | Session Context |",
                "| :--- | :--- | :--- | :--- | :--- |"
            ])
            for hb in det.hourly_distribution:
                h = hb["hour"]
                sess = "Asian Session" if 0 <= h < 8 else ("European Session" if 8 <= h < 14 else "US / Overlap Session")
                lines.append(
                    f"| `{h:02d}:00 UTC` | `{hb['trades']:,}` | `{hb['win_rate_pct']:.1f}%` | `{hb['pnl']:+,.4f}` | {sess} |"
                )

        # 8. Quantiles
        if det and det.pnl_distribution:
            lines.extend([
                "\n---",
                "\n## 8. 📊 Return & ROE Quantile Distribution",
                "| Percentile / Stat | Trade PnL (USDT) | Trade ROE (%) |",
                "| :--- | :--- | :--- |",
                f"| **Min (Worst)** | `{det.pnl_distribution.get('min', 0.0):+.4f}` | `{det.roe_distribution.get('min', 0.0):+.2f}%` |",
                f"| **10th Percentile (p10)** | `{det.pnl_distribution.get('p10', 0.0):+.4f}` | `{det.roe_distribution.get('p10', 0.0):+.2f}%` |",
                f"| **25th Percentile (p25)** | `{det.pnl_distribution.get('p25', 0.0):+.4f}` | `{det.roe_distribution.get('p25', 0.0):+.2f}%` |",
                f"| **Median (p50)** | `{det.pnl_distribution.get('median', 0.0):+.4f}` | `{det.roe_distribution.get('median', 0.0):+.2f}%` |",
                f"| **75th Percentile (p75)** | `{det.pnl_distribution.get('p75', 0.0):+.4f}` | `{det.roe_distribution.get('p75', 0.0):+.2f}%` |",
                f"| **90th Percentile (p90)** | `{det.pnl_distribution.get('p90', 0.0):+.4f}` | `{det.roe_distribution.get('p90', 0.0):+.2f}%` |",
                f"| **Max (Best)** | `{det.pnl_distribution.get('max', 0.0):+.4f}` | `{det.roe_distribution.get('max', 0.0):+.2f}%` |",
            ])

        # 9. AI Guided Questions
        lines.extend([
            "\n---",
            "\n## ❓ Guided Questions for AI Deep-Dive",
            "Based on the rigorous empirical data above, please answer:",
            "1. **Payoff Asymmetry Risk**: Given the Win Rate vs Payoff ratio (average loss vs average win), does this strategy rely excessively on high win rate to survive, and what happens if win rate drops by 5%?",
            "2. **Holding Duration Analysis**: Which duration buckets contribute the bulk of positive alpha vs drawdowns? Should we enforce a time-based stop or timeout?",
            "3. **Hourly / Session Filtering**: Are there specific UTC hours or sessions where trading should be paused to avoid chop and adverse fills?",
            "4. **Directional Imbalance**: Is there significant alpha skew between LONG and SHORT signals? Would trading LONG-only or SHORT-only improve the Sharpe ratio?",
            "5. **Concrete Parameter Tweaks**: Suggest 3 specific parameter changes (e.g. TP ticks, SL ROE %, candle timeframe, leverage) with explicit expected mathematical outcomes."
        ])

        return "\n".join(lines)

    def export_comparison_markdown(
        self,
        runs: List[BacktestRunRecord],
        selected_factors: Optional[List[str]] = None
    ) -> str:
        """Generates an extensive multi-run comparative dossier for AI analysis."""
        cmp = self.engine.compare_runs(
            run_ids=[r.metadata.run_id for r in runs],
            selected_factors=selected_factors
        )

        now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        run_names = [f"{r.metadata.symbol} {r.metadata.strategy} ({r.metadata.timeframe})" for r in runs]

        lines = [
            "# 🤖 AI Quantitative Multi-Strategy Comparison Dossier",
            f"> **Generated for AI Deep Comparison:** `{now_utc}`",
            f"> **Strategies Compared ({len(runs)}):** `{', '.join(run_names)}`\n",
            "---",
            "\n## 🧠 System Context for AI Quantitative Analyst",
            "You are comparing multiple backtest runs for high-frequency crypto futures strategies.",
            "Each run tests different parameter tweaks (e.g. timeframe, take-profit ticks, stop-loss rules, strategy logic).",
            "Identify the statistical winners and losers, analyze the parameter sensitivity frontier,",
            "and determine which configuration provides true structural edge versus overfitted noise.\n",
            "---",
            "\n## 1. 🎛️ Parameter Tweaks & Variation Diff Matrix",
            "The table below isolates what changed between the runs versus what was kept constant:",
            "| Parameter Name | Status | " + " | ".join([f"Run #{i+1}: {r.metadata.symbol} ({r.metadata.strategy})" for i, r in enumerate(runs)]) + " |",
            "| :--- | :---: | " + " | ".join([":---:" for _ in runs]) + " |"
        ]

        for d in cmp["parameter_diffs"]:
            status = "**[VARIED]**" if d["is_diff"] else "Constant"
            row = [f"**{d['name']}**", status]
            for r in runs:
                row.append(f"`{d['values'].get(r.metadata.run_id, '—')}`")
            lines.append("| " + " | ".join(row) + " |")

        lines.extend([
            "\n---",
            "\n## 2. ⚡ Side-by-Side Multi-Factor Performance Scorecard",
            "Comprehensive comparison across financial returns, risk ratios, and execution dynamics:",
            "| Factor / Metric | " + " | ".join([f"Run #{i+1}: {r.metadata.symbol} ({r.metadata.strategy})" for i, r in enumerate(runs)]) + " |",
            "| :--- | " + " | ".join([":---:" for _ in runs]) + " |"
        ])


        curr_cat = ""
        for row in cmp["comparison_matrix"]:
            if row["category"] != curr_cat:
                curr_cat = row["category"]
                lines.append(f"| **=== {curr_cat.upper()} FACTORS ===** | " + " | ".join(["---" for _ in runs]) + " |")

            line = [f"**{row['name']}**"]
            for r in runs:
                rid = r.metadata.run_id
                val = row["values"].get(rid, 0.0)
                is_best = (rid == row["best_run_id"])
                
                fmt = row["format"]
                if fmt == "pct":
                    val_str = f"{val:+.2f}%" if isinstance(val, (int, float)) else str(val)
                elif fmt in ("currency", "currency_sub"):
                    val_str = f"{val:+.4f} USDT" if isinstance(val, (int, float)) else str(val)
                elif fmt == "float2":
                    val_str = f"{val:.2f}" if isinstance(val, (int, float)) else str(val)
                elif fmt == "int":
                    val_str = f"{val:,}" if isinstance(val, (int, float)) else str(val)
                elif fmt == "duration":
                    val_str = f"{val:.1f}s" if isinstance(val, (int, float)) else str(val)
                else:
                    val_str = str(val)

                if is_best:
                    line.append(f"**`{val_str}` 🏆 Best**")
                else:
                    line.append(f"`{val_str}`")

            lines.append("| " + " | ".join(line) + " |")

        # 3. 6-Pillar Radar Scores
        radar = cmp.get("radar_footprints", {})
        if radar and "series" in radar:
            lines.extend([
                "\n---",
                "\n## 3. 🕸️ 6-Pillar Strategy Radar Scores (0 to 100)",
                "Normalized multi-dimensional footprint:",
                "| Strategy Dimension | " + " | ".join([f"Run #{i+1}: {r.metadata.symbol}" for i, r in enumerate(runs)]) + " |",
                "| :--- | " + " | ".join([":---:" for _ in runs]) + " |"
            ])
            dims = radar.get("dimensions", [])
            for d_idx, dim in enumerate(dims):
                line = [f"**{dim}**"]
                for s in radar["series"]:
                    score = s["scores"][d_idx] if d_idx < len(s["scores"]) else 0.0
                    line.append(f"`{score:.1f} / 100`")
                lines.append("| " + " | ".join(line) + " |")

        # 4. Exit Comparison
        exit_cmp = cmp.get("exit_comparison", {})
        if exit_cmp and "series" in exit_cmp:
            lines.extend([
                "\n---",
                "\n## 4. 🎯 Exit Trigger Sensitivity (TP Hit vs SL Hit)",
                "| Exit Trigger | " + " | ".join([f"Run #{i+1}" for i, r in enumerate(runs)]) + " |",
                "| :--- | " + " | ".join([":---:" for _ in runs]) + " |"
            ])
            reasons = exit_cmp.get("reasons", [])
            for r_idx, reason in enumerate(reasons):
                line = [f"`{reason}`"]
                for s in exit_cmp["series"]:
                    pct = s["percentages"][r_idx] if r_idx < len(s["percentages"]) else 0.0
                    cnt = s["counts"][r_idx] if r_idx < len(s["counts"]) else 0
                    line.append(f"`{cnt:,} ({pct:.1f}%)`")
                lines.append("| " + " | ".join(line) + " |")

        # 5. AI Guided Comparative Questions
        lines.extend([
            "\n---",
            "\n## ❓ Guided Questions for AI Comparison Analysis",
            "1. **Dominant Strategy Identification**: Which strategy demonstrates true statistical robustness rather than curve-fitting? Rank them from best to worst with reasoning.",
            "2. **Tweak Sensitivity**: For the parameters that varied between runs, how did changing them impact Win Rate, Max Drawdown, and Sharpe Ratio?",
            "3. **Risk Profile & Tail Events**: Compare the downside tail risks (Max Drawdown and consecutive losses) across the strategies.",
            "4. **Production Recommendation**: If you had to deploy one of these configurations to production trading on KCEX, which one would you pick and with what risk controls?"
        ])

        return "\n".join(lines)

    def export_json_dossier(
        self,
        runs: List[BacktestRunRecord],
        is_comparison: bool = True
    ) -> Dict[str, Any]:
        """Exports a complete structured JSON dossier for programmatic AI processing."""
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        
        runs_data = []
        for r in runs:
            r_dict = r.to_dict()
            # Include downsampled curve
            curve = self.engine.indexer.get_downsampled_curve(r.metadata.run_id)
            r_dict["downsampled_equity_curve"] = curve
            runs_data.append(r_dict)

        payload = {
            "schema_version": "2.0.0",
            "export_timestamp_utc": now_utc,
            "ai_prompt": (
                "You are an expert quantitative researcher. Analyze this JSON dataset containing "
                "complete backtesting metadata, scorecards, hourly heatmaps, duration distributions, "
                "and downsampled equity curves. Provide exhaustive risk and optimization recommendations."
            ),
            "runs_count": len(runs),
            "runs": runs_data
        }

        if is_comparison and len(runs) > 1:
            run_ids = [r.metadata.run_id for r in runs]
            payload["comparison_engine"] = self.engine.compare_runs(run_ids)

        return payload
