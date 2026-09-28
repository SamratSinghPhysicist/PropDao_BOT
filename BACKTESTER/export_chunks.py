"""
Standalone CLI Slicer & AI Dossier Exporter
===========================================
Allows quick command-line slicing and batch generation of cropped backtest AI dossiers.

Usage:
    python -m BACKTESTER.export_chunks --run-id <RUN_ID> --granularity monthly --out-dir ./exports/
    python -m BACKTESTER.export_chunks --list-runs
    python -m BACKTESTER.export_chunks --run-id <RUN_ID> --granularity loss_clusters
"""

import os
import sys
import argparse

# Reconfigure stdout for UTF-8 on Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BACKTESTER_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.abspath(os.path.join(BACKTESTER_DIR, ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from BACKTESTER.analytics.indexer import ReportIndexer
from BACKTESTER.analytics.chunker import ChronosChunker


def main():
    parser = argparse.ArgumentParser(description="KCEX Chronos Backtest Slicer & AI Dossier Exporter")
    parser.add_argument("--run-id", type=str, help="Backtest Run ID to partition")
    parser.add_argument("--list-runs", action="store_true", help="List all available backtest runs")
    parser.add_argument(
        "--granularity",
        type=str,
        default="monthly",
        choices=["monthly", "weekly", "daily", "loss_clusters"],
        help="Partitioning granularity (default: monthly)"
    )
    parser.add_argument("--out-dir", type=str, default="", help="Output directory to save chunked markdown files")
    parser.add_argument("--zip", action="store_true", help="Package all chunks into a single .zip file")
    parser.add_argument("--max-losses", type=int, default=25, help="Max losing trades to analyze in depth per chunk")
    parser.add_argument("--no-ticks", action="store_true", help="Disable tick-level streaming to speed up export")

    args = parser.parse_args()

    indexer = ReportIndexer()
    chunker = ChronosChunker(indexer=indexer)

    if args.list_runs or not args.run_id:
        runs = indexer.get_all_runs()
        print("\n📋 Available Indexed Backtest Runs:")
        print("=" * 85)
        print(f"{'Run ID':<35} | {'Symbol':<12} | {'Trades':<8} | {'Win Rate':<10} | {'Net PnL (USDT)'}")
        print("-" * 85)
        for r in runs:
            sc = r.scorecard
            print(f"{r.metadata.run_id:<35} | {r.metadata.symbol:<12} | {sc.total_trades:<8} | {sc.win_rate_pct:>7.2f}% | {sc.net_pnl_usdt:>+10.4f}")
        print("=" * 85)
        if not args.run_id:
            print("\n💡 Specify --run-id <RUN_ID> to partition and export.\n")
            return

    run_id = args.run_id
    run = indexer.get_run_by_id(run_id)
    if not run:
        print(f"❌ Error: Run '{run_id}' not found in index.")
        sys.exit(1)

    print(f"\n🚀 Slicing Backtest: {run.metadata.symbol} ({run.metadata.strategy})")
    print(f"📦 Granularity: {args.granularity.upper()}")

    manifest = chunker.get_chunk_manifest(run_id, granularity=args.granularity)
    chunks = manifest.get("chunks", [])
    print(f"✨ Found {len(chunks)} partition slices.\n")

    out_dir = args.out_dir or os.path.join(BACKTESTER_DIR, "reports", "exports", f"{run_id}_{args.granularity}_chunks")
    os.makedirs(out_dir, exist_ok=True)

    if args.zip:
        zip_path = os.path.join(out_dir, f"{run_id}_{args.granularity}_ai_dossiers.zip")
        print(f"📦 Generating ZIP archive: {zip_path} ...")
        zip_buf = chunker.export_all_chunks_zip(
            run_id=run_id,
            granularity=args.granularity,
            max_losing_trades=args.max_losses,
            include_ticks=not args.no_ticks,
            include_post_exit=not args.no_ticks
        )
        with open(zip_path, "wb") as f:
            f.write(zip_buf.getvalue())
        print(f"✅ Successfully wrote ZIP package to: {zip_path}")
        return

    # Export individual files + Guide
    print(f"📂 Writing chunk dossiers to: {out_dir}\n")
    guide_md = chunker._generate_master_synthesis_guide(manifest)
    with open(os.path.join(out_dir, "00_README_AND_MASTER_SYNTHESIS_PROMPT.md"), "w", encoding="utf-8") as f:
        f.write(guide_md)

    for idx, c in enumerate(chunks):
        print(f"  [{idx + 1}/{len(chunks)}] Processing {c['label']} ({c['trades_count']} trades, {c['losing_trades']} losses)...")
        chunk_data = chunker.extract_chunk_data(
            run_id=run_id,
            start_ms=c["start_ms"],
            end_ms=c["end_ms"],
            max_losing_trades=args.max_losses,
            include_ticks=not args.no_ticks,
            include_post_exit=not args.no_ticks
        )
        chunk_md = chunker.format_chunk_markdown(chunk_data, chunk_index=idx + 1, total_chunks=len(chunks))
        fname = f"chunk_{idx + 1:02d}_{c['chunk_id']}.md"
        with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as f:
            f.write(chunk_md)

    print(f"\n🎉 All {len(chunks)} chunks exported successfully!")
    print(f"👉 Open {out_dir} and start feeding the files to your AI model.\n")


if __name__ == "__main__":
    main()
