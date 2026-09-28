"""
Multi-Pair GitHub Actions Backtest Dispatcher & Monitor
======================================================
Dispatches independent 4-worker workflow runs for each requested asset pair,
monitors their concurrent execution in real time, and downloads consolidated
matrix reports and detailed trade CSVs directly into:
BACKTESTER/reports/backtests_of_OB_and_demand_zone_strategy/
"""

import os
import sys
import time
import json
import zipfile
import argparse
import requests
from typing import List, Dict, Any, Optional

# Ensure utf-8 output encoding
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from BACKTESTER.engine.github_runner import (
    resolve_github_token,
    get_git_remote_repo,
    Style
)

WORKFLOW_FILE = "pair_backtest.yml"
TARGET_PAIRS_REQUIRED = [
    "BTC_USDT",
    "ETH_USDT",
    "SOL_USDT",
    "DOGE_USDT",
    "CL_USDT",
    "XAG_USDT"
]

ALL_PAIRS = [
    "BTC_USDT",
    "ETH_USDT",
    "SOL_USDT",
    "DOGE_USDT",
    "CL_USDT",
    "XAG_USDT",
    "TRUMP_USDT",
    "1000000MOG_USDT",
    "XAU_USDT"
]

REPORTS_DIR = os.path.join(ROOT_DIR, "BACKTESTER", "reports", "backtests_of_OB_and_demand_zone_strategy")


class PairBacktestManager:
    def __init__(self, token: Optional[str] = None):
        self.owner, self.repo = get_git_remote_repo()
        self.token = resolve_github_token(token)
        self.api_base = f"https://api.github.com/repos/{self.owner}/{self.repo}"

    @property
    def headers(self) -> Dict[str, str]:
        hdrs = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "KCEX-Pair-Backtest-Manager"
        }
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        return hdrs

    def dispatch_pair(
        self,
        symbol: str,
        start_date: str = "2026-01-01",
        end_date: str = "2026-08-31",
        strategy: str = "ORDER_BLOCK_DEMAND",
        capital: float = 100.0,
        leverage: int = 10,
        margin_pct: float = 10.0,
        ref: str = "main"
    ) -> Optional[int]:
        """Triggers a 4-worker workflow run for a single symbol."""
        url = f"{self.api_base}/actions/workflows/{WORKFLOW_FILE}/dispatches"
        payload = {
            "ref": ref,
            "inputs": {
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "strategy": strategy,
                "capital": str(capital),
                "leverage": str(leverage),
                "margin_pct": str(margin_pct)
            }
        }

        resp = requests.post(url, headers=self.headers, json=payload, timeout=15)
        if resp.status_code != 204:
            print(f"{Style.RED}[!] Dispatch failed for {symbol}: HTTP {resp.status_code} - {resp.text}{Style.RESET}")
            return None

        # Short pause to let GitHub create the run object
        time.sleep(2)
        runs_url = f"{self.api_base}/actions/workflows/{WORKFLOW_FILE}/runs?event=workflow_dispatch&per_page=5"
        for _ in range(8):
            try:
                r = requests.get(runs_url, headers=self.headers, timeout=10)
                if r.status_code == 200:
                    runs = r.json().get("workflow_runs", [])
                    if runs:
                        latest = runs[0]
                        run_id = latest.get("id")
                        print(f"  {Style.GREEN}✓ Dispatched {symbol:15s} -> Run #{run_id} ({latest.get('html_url')}){Style.RESET}")
                        return run_id
            except Exception:
                pass
            time.sleep(1.5)

        print(f"  {Style.YELLOW}⚠️ Dispatched {symbol} but could not immediately capture Run ID.{Style.RESET}")
        return None

    def dispatch_all(
        self,
        symbols: List[str],
        start_date: str = "2026-01-01",
        end_date: str = "2026-08-31",
        strategy: str = "ORDER_BLOCK_DEMAND",
        capital: float = 100.0,
        leverage: int = 10,
        margin_pct: float = 10.0,
        ref: str = "main"
    ) -> Dict[str, Optional[int]]:
        """Dispatches runs for all symbols sequentially with slight stagger."""
        print(f"\n{Style.CYAN}{'='*80}{Style.RESET}")
        print(f"{Style.BOLD}     DISPATCHING PARALLEL PAIR BACKTEST RUNS (10 WORKERS PER PAIR){Style.RESET}")
        print(f"{Style.CYAN}{'='*80}{Style.RESET}")
        print(f"Target Assets: {', '.join(symbols)}")
        print(f"Date Range:    {start_date} to {end_date}")
        print(f"Strategy:      {strategy} (10x Leverage, 10% Margin Sizing, Pure Market)")
        print(f"{Style.CYAN}{'='*80}{Style.RESET}\n")

        pair_runs: Dict[str, Optional[int]] = {}
        for s in symbols:
            run_id = self.dispatch_pair(
                symbol=s,
                start_date=start_date,
                end_date=end_date,
                strategy=strategy,
                capital=capital,
                leverage=leverage,
                margin_pct=margin_pct,
                ref=ref
            )
            pair_runs[s] = run_id
            time.sleep(3)  # Stagger dispatches slightly so GitHub creates unique runs

        return pair_runs

    def monitor_and_download(
        self,
        pair_runs: Dict[str, Optional[int]],
        dest_dir: str = REPORTS_DIR,
        poll_interval: int = 15
    ):
        """Monitors all active pair runs until completion and downloads artifacts."""
        os.makedirs(dest_dir, exist_ok=True)
        active = {s: rid for s, rid in pair_runs.items() if rid is not None}
        completed: Dict[str, str] = {}
        downloaded: set = set()
        t_start = time.time()

        print(f"\n{Style.CYAN}▶ Monitoring {len(active)} active pair workflows in real time...{Style.RESET}\n")

        while len(completed) < len(active):
            time.sleep(poll_interval)
            elapsed = time.time() - t_start
            m = int(elapsed // 60)
            s = int(elapsed % 60)

            status_tokens = []
            for sym, rid in active.items():
                if sym in completed:
                    conc = completed[sym]
                    if conc == "success":
                        status_tokens.append(f"{sym}: {Style.GREEN}✓{Style.RESET}")
                    else:
                        status_tokens.append(f"{sym}: {Style.RED}{conc}{Style.RESET}")
                    continue

                try:
                    r = requests.get(f"{self.api_base}/actions/runs/{rid}", headers=self.headers, timeout=10)
                    if r.status_code == 200:
                        d = r.json()
                        st = d.get("status")
                        conc = d.get("conclusion")
                        if st == "completed":
                            completed[sym] = conc or "finished"
                            if conc == "success":
                                status_tokens.append(f"{sym}: {Style.GREEN}✓{Style.RESET}")
                            else:
                                status_tokens.append(f"{sym}: {Style.RED}{conc}{Style.RESET}")

                            # Download artifacts right away
                            if sym not in downloaded:
                                self.download_artifacts_for_run(rid, sym, dest_dir)
                                downloaded.add(sym)
                        else:
                            status_tokens.append(f"{sym}: {Style.YELLOW}⏳{Style.RESET}")
                except Exception:
                    status_tokens.append(f"{sym}: ?")

            sys.stdout.write(f"\r⏱ [{m:02d}:{s:02d}] Progress: " + " | ".join(status_tokens) + "   ")
            sys.stdout.flush()

        print("\n\n" + "=" * 80)
        print(f"{Style.GREEN}{Style.BOLD}🎉 ALL PAIR BACKTESTS COMPLETED!{Style.RESET}")
        print("=" * 80)

        # Generate Cross-Pair Comparison Report
        self.generate_cross_asset_summary(dest_dir)

    def download_artifacts_for_run(self, run_id: int, symbol: str, dest_dir: str):
        """Downloads all artifacts for a run and unpacks them into dest_dir."""
        url = f"{self.api_base}/actions/runs/{run_id}/artifacts"
        try:
            resp = requests.get(url, headers=self.headers, timeout=10)
            if resp.status_code != 200:
                return
            artifacts = resp.json().get("artifacts", [])
            for art in artifacts:
                art_id = art.get("id")
                art_name = art.get("name")
                dl_url = f"{self.api_base}/actions/artifacts/{art_id}/zip"
                zip_path = os.path.join(dest_dir, f"{art_name}.zip")

                r_dl = requests.get(dl_url, headers=self.headers, stream=True, timeout=60)
                if r_dl.status_code == 200:
                    with open(zip_path, "wb") as f:
                        for chunk in r_dl.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)
                    with zipfile.ZipFile(zip_path, "r") as zf:
                        zf.extractall(dest_dir)
                    print(f"\n  [+] Downloaded & Extracted {art_name} for {symbol}")
        except Exception as e:
            print(f"\n  [!] Failed downloading artifacts for {symbol}: {e}")

    def generate_cross_asset_summary(self, reports_dir: str):
        """Builds a cross-asset summary Markdown file comparing top performers across all pairs."""
        import glob
        import csv

        # Filter out chunk files (master file is strictly {SYMBOL}_batch_matrix.csv)
        matrix_files = glob.glob(os.path.join(reports_dir, "*_batch_matrix.csv"))
        master_files = [
            f for f in matrix_files
            if not any(x in os.path.basename(f) for x in ("_1m", "_5m", "_15m", "_4h", "_htf", "_chunk"))
        ]

        all_best = []
        for mf in sorted(master_files):
            sym = os.path.basename(mf).replace("_batch_matrix.csv", "")
            rows = []
            with open(mf, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    rows.append(r)
            if rows:
                sorted_rows = sorted(rows, key=lambda x: float(x.get("net_pnl_usdt", 0)), reverse=True)
                best = sorted_rows[0]
                all_best.append(best)

        if not all_best:
            return

        summary_md = os.path.join(reports_dir, "ALL_PAIRS_SUMMARY.md")
        lines = []
        lines.append("# 🏆 Multi-Asset Strategy Benchmark Summary: Order Block + Demand Strategy")
        lines.append(f"**Date Generated:** `{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}`")
        lines.append(f"**Evaluation Period:** `2026-01-01` to `2026-08-31` | **Capital:** `$100.00` | **Leverage:** `10x` | **Margin Sizing:** `10%`")
        lines.append("\n---\n")
        lines.append("## 🥇 Best Performing Configuration per Asset")
        lines.append("| Asset | Best TF | Fee Schedule | Slip | Trades | Win Rate | Profit Factor | Net PnL (USDT) | ROI % | Max DD % | Final Balance |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")

        for b in sorted(all_best, key=lambda x: float(x.get("net_pnl_usdt", 0)), reverse=True):
            roi = float(b.get("net_roi_pct", 0))
            sign = "+" if roi >= 0 else ""
            lines.append(
                f"| **`{b.get('symbol')}`** | `{b.get('timeframe')}` | {b.get('fee_schedule')} | `{b.get('slippage_ticks')}t` | "
                f"`{b.get('total_trades')}` | `{b.get('win_rate_pct')}%` | `{b.get('profit_factor')}` | "
                f"**`${float(b.get('net_pnl_usdt', 0)):+.2f}`** | **`{sign}{roi}%`** | `{b.get('max_drawdown_pct')}%` | `${float(b.get('final_balance_usdt', 0)):.2f}` |"
            )

        content = "\n".join(lines)
        with open(summary_md, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[+] Saved Cross-Asset Master Benchmark Summary: {summary_md}")


def main():
    parser = argparse.ArgumentParser(description="Multi-Pair GitHub Actions Backtest Dispatcher")
    parser.add_argument("--symbols", type=str, default="REQUIRED", help="Comma-separated symbols, 'REQUIRED', or 'ALL'")
    parser.add_argument("--start", type=str, default="2026-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2026-08-31", help="End date (YYYY-MM-DD)")
    parser.add_argument("--strategy", type=str, default="ORDER_BLOCK_DEMAND", help="Strategy to evaluate")
    parser.add_argument("--capital", type=float, default=100.0, help="Initial capital in USDT")
    parser.add_argument("--leverage", type=int, default=10, help="Leverage multiplier")
    parser.add_argument("--margin-pct", type=float, default=10.0, help="Margin percent per trade")
    parser.add_argument("--token", type=str, default=None, help="GitHub Token")
    parser.add_argument("--no-monitor", action="store_true", help="Dispatch without monitoring")

    args = parser.parse_args()

    if args.symbols.upper() == "REQUIRED":
        target_symbols = TARGET_PAIRS_REQUIRED
    elif args.symbols.upper() == "ALL":
        target_symbols = ALL_PAIRS
    else:
        target_symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    manager = PairBacktestManager(token=args.token)
    pair_runs = manager.dispatch_all(
        symbols=target_symbols,
        start_date=args.start,
        end_date=args.end,
        strategy=args.strategy,
        capital=args.capital,
        leverage=args.leverage,
        margin_pct=args.margin_pct
    )

    if not args.no_monitor:
        manager.monitor_and_download(pair_runs)


if __name__ == "__main__":
    main()
