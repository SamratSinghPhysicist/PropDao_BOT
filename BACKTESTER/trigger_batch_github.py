"""
GitHub Batch Backtest Dispatcher & Monitor
==========================================
Dispatches the Multi-Asset Matrix Backtest workflow to GitHub Actions,
monitors parallel runner execution across all assets, and downloads
consolidated matrix reports.
"""

import os
import sys
import time
import json
import argparse
import requests
from typing import Optional, Dict, Any, List

# Ensure utf-8 output encoding on Windows consoles
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

WORKFLOW_FILENAME = "batch_backtest.yml"


class GitHubBatchRunner:
    def __init__(self, token: Optional[str] = None):
        self.owner, self.repo = get_git_remote_repo()
        self.token = resolve_github_token(token)
        self.api_base = f"https://api.github.com/repos/{self.owner}/{self.repo}"

    @property
    def headers(self) -> Dict[str, str]:
        hdrs = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "KCEX-Batch-Backtester"
        }
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        return hdrs

    def dispatch(
        self,
        symbols: str = "ALL",
        start_date: str = "2026-01-01",
        end_date: str = "2026-08-31",
        strategy: str = "ORDER_BLOCK_DEMAND",
        capital: float = 100.0,
        leverage: int = 10,
        margin_pct: float = 10.0,
        timeframes: str = "1m,5m,15m,1h,4h,1d",
        ref: str = "main"
    ) -> Optional[int]:
        if not self.token:
            print(f"{Style.RED}[!] Missing GitHub Token. Please set GITHUB_TOKEN in .env or pass --token.{Style.RESET}")
            return None

        url = f"{self.api_base}/actions/workflows/{WORKFLOW_FILENAME}/dispatches"
        payload = {
            "ref": ref,
            "inputs": {
                "symbols": symbols,
                "start_date": start_date,
                "end_date": end_date,
                "strategy": strategy,
                "capital": str(capital),
                "leverage": str(leverage),
                "margin_pct": str(margin_pct),
                "timeframes": timeframes
            }
        }

        print(f"\n{Style.CYAN}{'='*78}{Style.RESET}")
        print(f"{Style.BOLD}     DISPATCHING MULTI-ASSET MATRIX BACKTEST TO GITHUB ACTIONS{Style.RESET}")
        print(f"{Style.CYAN}{'='*78}{Style.RESET}")
        print(f"Repository:   {self.owner}/{self.repo}")
        print(f"Workflow:     .github/workflows/{WORKFLOW_FILENAME}")
        print(f"Symbols:      {symbols}")
        print(f"Date Range:   {start_date} to {end_date}")
        print(f"Strategy:     {strategy}")
        print(f"Leverage:     {leverage}x | Margin Sizing: {margin_pct}% | Capital: ${capital:.2f}")
        print(f"Timeframes:   {timeframes}")
        print(f"{Style.CYAN}{'='*78}{Style.RESET}\n")

        t_dispatch = time.time()
        resp = requests.post(url, headers=self.headers, json=payload, timeout=15)
        if resp.status_code != 204:
            print(f"{Style.RED}[!] Dispatch failed: HTTP {resp.status_code} - {resp.text}{Style.RESET}")
            return None

        print(f"{Style.GREEN}✓ Workflow dispatch accepted by GitHub API.{Style.RESET}")
        print("[*] Locating dispatched workflow run...")

        # Poll for run ID
        runs_url = f"{self.api_base}/actions/workflows/{WORKFLOW_FILENAME}/runs?event=workflow_dispatch"
        for _ in range(15):
            time.sleep(3)
            try:
                r_resp = requests.get(runs_url, headers=self.headers, timeout=10)
                if r_resp.status_code == 200:
                    runs = r_resp.json().get("workflow_runs", [])
                    if runs:
                        latest = runs[0]
                        run_id = latest.get("id")
                        run_url = latest.get("html_url")
                        print(f"{Style.GREEN}✓ Active Workflow Run #{run_id} Found!{Style.RESET}")
                        print(f"  🔗 Direct Link: {Style.BOLD}{run_url}{Style.RESET}\n")
                        return run_id
            except Exception:
                pass

        print(f"{Style.YELLOW}[!] Run dispatched but polling timed out. Check GitHub Actions online.{Style.RESET}")
        return None

    def monitor(self, run_id: int, poll_interval: int = 10):
        url = f"{self.api_base}/actions/runs/{run_id}"
        jobs_url = f"{self.api_base}/actions/runs/{run_id}/jobs"
        start_time = time.time()

        print(f"{Style.CYAN}▶ Monitoring Matrix Execution across parallel runners...{Style.RESET}\n")

        while True:
            time.sleep(poll_interval)
            elapsed = time.time() - start_time
            m = int(elapsed // 60)
            s = int(elapsed % 60)

            try:
                resp = requests.get(url, headers=self.headers, timeout=10)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                status = data.get("status")
                conclusion = data.get("conclusion")

                # Get job breakdown
                jobs_resp = requests.get(jobs_url, headers=self.headers, timeout=10)
                job_summaries = []
                if jobs_resp.status_code == 200:
                    jobs = jobs_resp.json().get("jobs", [])
                    for j in jobs:
                        j_name = j.get("name", "").replace("Matrix ", "").replace(" (ORDER_BLOCK_DEMAND)", "")
                        j_status = j.get("status")
                        j_conc = j.get("conclusion")
                        if j_conc == "success":
                            job_summaries.append(f"{j_name}: {Style.GREEN}✓{Style.RESET}")
                        elif j_conc == "failure":
                            job_summaries.append(f"{j_name}: {Style.RED}✗{Style.RESET}")
                        elif j_status == "in_progress":
                            job_summaries.append(f"{j_name}: {Style.YELLOW}⏳{Style.RESET}")
                        else:
                            job_summaries.append(f"{j_name}: {Style.DIM}⋯{Style.RESET}")

                jobs_str = " | ".join(job_summaries)
                status_colored = f"{Style.YELLOW}{status}{Style.RESET}" if status != "completed" else (f"{Style.GREEN}SUCCESS{Style.RESET}" if conclusion == "success" else f"{Style.RED}{conclusion}{Style.RESET}")
                sys.stdout.write(f"\r⏱ [{m:02d}:{s:02d}] Status: {status_colored} | {jobs_str}   ")
                sys.stdout.flush()

                if status == "completed":
                    print("\n")
                    if conclusion == "success":
                        print(f"{Style.GREEN}{Style.BOLD}🎉 Multi-Asset Matrix Backtests Completed Successfully!{Style.RESET}")
                    else:
                        print(f"{Style.RED}⚠️ Matrix run concluded with status: {conclusion}{Style.RESET}")
                    self.download_artifacts(run_id)
                    break

            except Exception:
                pass

    def download_artifacts(self, run_id: int, output_dir: str = "BACKTESTER/reports"):
        """Downloads and extracts all matrix artifact ZIPs from the completed workflow run."""
        import zipfile
        os.makedirs(output_dir, exist_ok=True)
        url = f"{self.api_base}/actions/runs/{run_id}/artifacts"

        print(f"\n[*] Fetching generated matrix artifacts for Run #{run_id}...")
        resp = requests.get(url, headers=self.headers, timeout=10)
        if resp.status_code != 200:
            print(f"[!] Could not retrieve artifact list: HTTP {resp.status_code}")
            return

        artifacts = resp.json().get("artifacts", [])
        if not artifacts:
            print(f"[!] No artifacts found for run #{run_id}.")
            return

        print(f"[+] Found {len(artifacts)} artifact package(s). Downloading...")
        for art in artifacts:
            name = art.get("name")
            art_id = art.get("id")
            download_url = f"{self.api_base}/actions/artifacts/{art_id}/zip"
            zip_dest = os.path.join(output_dir, f"{name}.zip")

            try:
                dl = requests.get(download_url, headers=self.headers, stream=True, timeout=60)
                if dl.status_code == 200:
                    with open(zip_dest, "wb") as f:
                        for chunk in dl.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)
                    # Extract contents
                    with zipfile.ZipFile(zip_dest, "r") as z:
                        z.extractall(output_dir)
                    print(f"  ✓ Downloaded & Extracted: {name}")
            except Exception as e:
                print(f"  [!] Failed to download artifact {name}: {e}")

        print(f"\n[+] All matrix reports successfully saved to: {os.path.abspath(output_dir)}")


def main():
    parser = argparse.ArgumentParser(description="Trigger and Monitor GitHub Actions Batch Backtests")
    parser.add_argument("--symbols", type=str, default="ALL", help="Comma-separated symbols or ALL")
    parser.add_argument("--start", type=str, default="2026-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2026-08-31", help="End date (YYYY-MM-DD)")
    parser.add_argument("--strategy", type=str, default="ORDER_BLOCK_DEMAND", help="Strategy to evaluate")
    parser.add_argument("--capital", type=float, default=100.0, help="Initial capital in USDT")
    parser.add_argument("--leverage", type=int, default=10, help="Leverage multiplier")
    parser.add_argument("--margin-pct", type=float, default=10.0, help="Margin % per trade")
    parser.add_argument("--timeframes", type=str, default="1m,5m,15m,1h,4h,1d", help="Timeframes")
    parser.add_argument("--token", type=str, default=None, help="GitHub Personal Access Token")
    parser.add_argument("--no-monitor", action="store_true", help="Dispatch without waiting for completion")

    args = parser.parse_args()
    runner = GitHubBatchRunner(token=args.token)
    run_id = runner.dispatch(
        symbols=args.symbols,
        start_date=args.start,
        end_date=args.end,
        strategy=args.strategy,
        capital=args.capital,
        leverage=args.leverage,
        margin_pct=args.margin_pct,
        timeframes=args.timeframes
    )

    if run_id and not args.no_monitor:
        runner.monitor(run_id)


if __name__ == "__main__":
    main()
