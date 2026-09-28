"""
KCEX Backtest Terminal CLI Analyzer & Comparison Tool
====================================================
Command-line tool powered by Rich for instant terminal inspection,
side-by-side parameter diffs, and scorecard comparisons.

Usage:
    python BACKTESTER/analyzer.py --list
    python BACKTESTER/analyzer.py --compare
    python BACKTESTER/analyzer.py --deep <run_id_or_number>
"""

import os
import sys
import json
import argparse
import datetime

# Ensure UTF-8 on Windows consoles
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from BACKTESTER.analytics.indexer import ReportIndexer
from BACKTESTER.analytics.engine import AnalyticsEngine
from BACKTESTER.analytics.exporter import AIDossierExporter


console = Console()


def print_runs_list(runs):
    """Prints a styled Rich table of all indexed backtests."""
    table = Table(title="📊 Indexed Backtest Reports Catalog", title_style="bold cyan")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Symbol", style="bold")
    table.add_column("Strategy", style="magenta")
    table.add_column("TF", justify="center")
    table.add_column("Lev", justify="center")
    table.add_column("TP / SL", justify="center")
    table.add_column("Trades", justify="right")
    table.add_column("Win Rate", justify="right")
    table.add_column("Net PnL (USDT)", justify="right")
    table.add_column("Net ROI", justify="right")
    table.add_column("Profit Factor", justify="right")
    table.add_column("Max DD", justify="right", style="red")
    table.add_column("Source", justify="center", style="dim")

    for idx, r in enumerate(runs, 1):
        sc = r.scorecard
        m = r.metadata
        pnl = sc.net_pnl_usdt or 0.0
        roi = sc.net_roi_pct or 0.0
        pnl_style = "bold green" if pnl >= 0 else "bold red"
        pnl_str = f"{pnl:+.4f}"
        roi_str = f"{roi:+.2f}%"

        src = "☁️ Cloud" if m.source == "github_cloud" else "💻 Local"

        table.add_row(
            str(idx),
            m.symbol,
            m.strategy,
            m.timeframe,
            f"{m.leverage}x",
            f"+{m.tp_ticks}t / {m.sl_mode}{m.sl_value:g}",
            f"{sc.total_trades:,}",
            f"{sc.win_rate_pct:.2f}%",
            f"[{pnl_style}]{pnl_str}[/]",
            f"[{pnl_style}]{roi_str}[/]",
            f"{sc.profit_factor:.2f}",
            f"-{sc.max_drawdown_pct:.2f}%",
            src
        )

    console.print(table)
    console.print(f"[dim]Tip: Run [bold cyan]python BACKTESTER/analyzer.py --compare[/] for side-by-side comparison.[/dim]\n")


def print_comparison(runs, engine, selected_indices=None):
    """Prints side-by-side parameter diffs and scorecard matrix."""
    if selected_indices:
        target_runs = [runs[i - 1] for i in selected_indices if 1 <= i <= len(runs)]
    else:
        target_runs = runs

    if not target_runs:
        console.print("[red]No valid runs selected for comparison.[/red]")
        return

    run_ids = [r.metadata.run_id for r in target_runs]
    cmp = engine.compare_runs(run_ids)

    # 1. Parameter Tweaks Diff
    diffs = [d for d in cmp["parameter_diffs"] if d["is_diff"]]
    if diffs:
        p_table = Table(title="🎛️ Strategy Parameter Tweaks & Variations", title_style="bold yellow")
        p_table.add_column("Parameter", style="cyan bold")
        for r in target_runs:
            p_table.add_column(f"{r.metadata.symbol} ({r.metadata.strategy})", justify="center")

        for d in diffs:
            row = [d["name"]]
            for rid in run_ids:
                row.append(f"[bold]{d['values'].get(rid, '—')}[/bold]")
            p_table.add_row(*row)

        console.print(p_table)
        console.print("")

    # 2. Scorecard Comparison Matrix
    m_table = Table(title="⚡ Side-by-Side Multi-Factor Scorecard", title_style="bold green")
    m_table.add_column("Performance Factor", style="cyan")
    for r in target_runs:
        m_table.add_column(f"{r.metadata.symbol}\n[dim]{r.metadata.strategy} ({r.metadata.timeframe})[/dim]", justify="right")

    curr_cat = ""
    for row in cmp["comparison_matrix"]:
        if row["category"] != curr_cat:
            curr_cat = row["category"]
            m_table.add_section()

        line = [row["name"]]
        for rid in run_ids:
            val = row["values"].get(rid, 0.0)
            is_best = (rid == row["best_run_id"])
            
            # Format
            fmt = row["format"]
            if fmt == "pct":
                val_str = f"{val:+.2f}%" if isinstance(val, (int, float)) else str(val)
            elif fmt in ("currency", "currency_sub"):
                val_str = f"{val:+.4f}" if isinstance(val, (int, float)) else str(val)
            elif fmt == "float2":
                val_str = f"{val:.2f}" if isinstance(val, (int, float)) else str(val)
            elif fmt == "int":
                val_str = f"{val:,}" if isinstance(val, (int, float)) else str(val)
            else:
                val_str = str(val)

            if is_best:
                line.append(f"[bold green]{val_str} ★[/bold green]")
            else:
                line.append(val_str)

        m_table.add_row(*line)

    console.print(m_table)
    console.print("")


def print_deep_dive(run):
    """Prints comprehensive terminal details for a single run."""
    m = run.metadata
    sc = run.scorecard
    d = run.directional

    title = f"🔬 Deep Dive Analysis: {m.symbol} — {m.strategy} ({m.timeframe})"
    console.print(Panel.fit(
        f"[bold cyan]Symbol:[/] {m.symbol}  |  [bold cyan]Strategy:[/] {m.strategy}  |  [bold cyan]TF:[/] {m.timeframe}\n"
        f"[bold cyan]Leverage:[/] {m.leverage}x  |  [bold cyan]Take Profit:[/] +{m.tp_ticks} ticks  |  [bold cyan]Stop Loss:[/] {m.sl_mode}{m.sl_value:g}\n"
        f"[bold cyan]Date Range:[/] {m.date_range}  |  [bold cyan]Tick Trades:[/] {'Enabled' if m.high_fidelity_ticks else 'Disabled'}\n"
        f"[bold cyan]Initial Capital:[/] {sc.initial_capital_usdt} USDT  |  [bold cyan]Final Balance:[/] {sc.final_balance_usdt} USDT",
        title=title,
        border_style="cyan"
    ))

    # Scorecard Table
    t = Table(title="Executive Scorecard")
    t.add_column("Metric", style="dim")
    t.add_column("Value", style="bold")
    t.add_column("Metric", style="dim")
    t.add_column("Value", style="bold")

    pnl_col = "green" if sc.net_pnl_usdt >= 0 else "red"
    t.add_row("Net Realized PnL:", f"[{pnl_col}]{sc.net_pnl_usdt:+.4f} USDT[/]", "Profit Factor:", f"{sc.profit_factor:.2f}")
    t.add_row("Net ROI:", f"[{pnl_col}]{sc.net_roi_pct:+.2f}%[/]", "Win / Loss Payoff:", f"{sc.win_loss_payoff:.2f}")
    t.add_row("Total Trades:", f"{sc.total_trades:,}", "Win Rate:", f"[bold green]{sc.win_rate_pct:.2f}%[/]")
    t.add_row("Winning Trades:", f"{sc.winning_trades:,}", "Losing Trades:", f"{sc.losing_trades:,}")
    t.add_row("Max Drawdown:", f"[bold red]-{sc.max_drawdown_pct:.2f}%[/]", "Sharpe Ratio (est):", f"{sc.sharpe_ratio:.2f}")
    t.add_row("Sortino Ratio:", f"{sc.sortino_ratio:.2f}", "Calmar Ratio:", f"{sc.calmar_ratio:.2f}")
    t.add_row("Max Streak Wins:", f"{sc.max_consecutive_wins}", "Max Streak Losses:", f"{sc.max_consecutive_losses}")
    t.add_row("LONG Win Rate:", f"{d.long_win_rate_pct:.2f}%", "SHORT Win Rate:", f"{d.short_win_rate_pct:.2f}%")
    console.print(t)
    console.print("")


def main():
    parser = argparse.ArgumentParser(description="KCEX Backtest Terminal Analyzer")
    parser.add_argument("--list", "-l", action="store_true", help="List all indexed backtests")
    parser.add_argument("--compare", "-c", nargs="*", type=int, help="Compare all or specific runs by numbers (e.g. -c 1 2)")
    parser.add_argument("--deep", "-d", help="Inspect specific run by number or run_id")
    parser.add_argument("--reindex", action="store_true", help="Force re-indexing of reports")
    parser.add_argument("--export-ai", nargs="*", type=int, help="Export AI-ready quantitative dossier (e.g. --export-ai or --export-ai 1 2)")
    parser.add_argument("--format", choices=["markdown", "json", "both"], default="both", help="Export format (default: both)")
    args = parser.parse_args()

    indexer = ReportIndexer()
    if args.reindex:
        console.print("[yellow]Re-indexing reports directory...[/yellow]")
        runs = indexer.get_all_runs(force_reindex=True)
        console.print(f"[green]Successfully re-indexed {len(runs)} reports.[/green]\n")
    else:
        runs = indexer.get_all_runs()

    if not runs:
        console.print("[red]No backtest reports found in BACKTESTER/reports.[/red]")
        return

    engine = AnalyticsEngine(indexer)
    exporter = AIDossierExporter(engine)

    # Export AI dossier mode
    if args.export_ai is not None:
        selected_indices = args.export_ai
        if selected_indices and len(selected_indices) > 0:
            target_runs = [runs[i - 1] for i in selected_indices if 1 <= i <= len(runs)]
        else:
            target_runs = runs

        if not target_runs:
            console.print("[red]No valid runs found for AI export.[/red]")
            return

        exports_dir = os.path.join(indexer.reports_dir, "exports")
        os.makedirs(exports_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        exported_files = []

        if len(target_runs) == 1:
            r = target_runs[0]
            prefix = f"{r.metadata.run_id}_ai_dossier"
            if args.format in ("markdown", "both"):
                md_path = os.path.join(exports_dir, f"{prefix}.md")
                with open(md_path, "w", encoding="utf-8") as f:
                    f.write(exporter.export_single_markdown(r))
                exported_files.append(md_path)
            if args.format in ("json", "both"):
                json_path = os.path.join(exports_dir, f"{prefix}.json")
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(exporter.export_json_dossier([r], is_comparison=False), f, indent=2)
                exported_files.append(json_path)
        else:
            prefix = f"strategy_comparison_{len(target_runs)}_runs_{ts}"
            if args.format in ("markdown", "both"):
                md_path = os.path.join(exports_dir, f"{prefix}.md")
                with open(md_path, "w", encoding="utf-8") as f:
                    f.write(exporter.export_comparison_markdown(target_runs))
                exported_files.append(md_path)
            if args.format in ("json", "both"):
                json_path = os.path.join(exports_dir, f"{prefix}.json")
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(exporter.export_json_dossier(target_runs, is_comparison=True), f, indent=2)
                exported_files.append(json_path)

        console.print(Panel(
            "[bold green]✓ AI Quantitative Analysis Dossier Exported Successfully![/bold green]\n\n" +
            "\n".join([f"  📄 [cyan]{p}[/cyan]" for p in exported_files]) +
            "\n\n[dim]You can provide these files directly to any AI (Claude, Gemini, ChatGPT) for deep mathematical strategy analysis.[/dim]",
            title="🤖 AI Export Ready",
            border_style="green"
        ))
        return

    if args.deep:
        target = None
        if args.deep.isdigit():
            idx = int(args.deep) - 1
            if 0 <= idx < len(runs):
                target = runs[idx]
        else:
            for r in runs:
                if args.deep in r.metadata.run_id:
                    target = r
                    break
        if target:
            print_deep_dive(target)
        else:
            console.print(f"[red]Could not find run matching '{args.deep}'.[/red]")
        return

    if args.compare is not None:
        selected_indices = args.compare if len(args.compare) > 0 else None
        print_comparison(runs, engine, selected_indices)
        return

    # Default action: list
    print_runs_list(runs)


if __name__ == "__main__":
    main()
