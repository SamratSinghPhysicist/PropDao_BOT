"""
PropDAO GitHub Actions Matrix Backtest Dispatcher & Monitor
===========================================================
Dispatches the 9-runner PropDAO Multi-Asset Matrix Backtest workflow to GitHub Actions,
monitors real-time parallel execution across all assets, and automatically downloads
consolidated reports and all trade CSV files locally into:
BACKTESTER/reports/propdao_matrix/
"""

import os
import sys
import time
import json
import zipfile
import io
import argparse
import requests
from typing import Optional, Dict, Any, List

# Ensure UTF-8 output encoding
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

WORKFLOW_FILE = "propdao_backtest_matrix.yml"
REPORTS_DIR = os.path.join(ROOT_DIR, "BACKTESTER", "reports", "propdao_matrix")


class PropDaoMatrixDispatcher:
    def __init__(self, token: Optional[str] = None):
        self.owner, self.repo = get_git_remote_repo()
        self.token = resolve_github_token(token)
        self.api_base = f"https://api.github.com/repos/{self.owner}/{self.repo}"

    @property
    def headers(self) -> Dict[str, str]:
        hdrs = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "PropDAO-Matrix-Backtester"
        }
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        return hdrs

    def dispatch(
        self,
        start_date: str = "2026-01-01",
        end_date: str = "2026-08-31",
        capital: float = 25000.0,
        maker_fee: float = 0.015,
        taker_fee: float = 0.045,
        max_dd_pct: float = 2.0,
        timeframes: str = "5m,15m,30m,1h,4h,1d",
        leverages: str = "1,2",
        risks: str = "1,2,5,10,15,20,25,50",
        slippages: str = "0,1,2",
        ref: str = "main"
    ) -> Optional[int]:
        if not self.token:
            print(f"{Style.RED}[!] Missing GitHub Token. Please set GITHUB_TOKEN in .env.{Style.RESET}")
            return None

        url = f"{self.api_base}/actions/workflows/{WORKFLOW_FILE}/dispatches"
        payload = {
            "ref": ref,
            "inputs": {
                "start_date": start_date,
                "end_date": end_date,
                "capital": str(capital),
                "maker_fee": str(maker_fee),
                "taker_fee": str(taker_fee),
                "max_dd_pct": str(max_dd_pct),
                "timeframes": timeframes,
                "leverages": leverages,
                "risks": risks,
                "slippages": slippages
            }
        }

        print(f"\n{Style.CYAN}{'='*80}{Style.RESET}")
        print(f"{Style.BOLD}     DISPATCHING PROPDAO MATRIX BACKTEST TO GITHUB ACTIONS{Style.RESET}")
        print(f"{Style.CYAN}{'='*80}{Style.RESET}")
        print(f"Repository:       {self.owner}/{self.repo}")
        print(f"Workflow:         .github/workflows/{WORKFLOW_FILE}")
        print(f"Date Range:       {start_date} to {end_date}")
        print(f"Capital:          ${capital:,.2f} | Max DD Floor: {max_dd_pct:.2f}% (${capital*(max_dd_pct/100.0):,.2f})")
        print(f"Fees:             Maker {maker_fee:.3f}% / Taker {taker_fee:.3f}% ({taker_fee*2:.3f}% RT)")
        print(f"Timeframes:       {timeframes}")
        print(f"Leverages:        {leverages}")
        print(f"Risks (%):        {risks}")
        print(f"Slippages:        {slippages}")
        print(f"{Style.CYAN}{'='*80}{Style.RESET}\n")

        resp = requests.post(url, headers=self.headers, json=payload, timeout=15)
        if resp.status_code != 204:
            print(f"{Style.RED}[!] Dispatch failed: HTTP {resp.status_code} - {resp.text}{Style.RESET}")
            return None

        print(f"{Style.GREEN}✓ Workflow dispatch accepted by GitHub API.{Style.RESET}")
        print("[*] Locating dispatched workflow run...")

        runs_url = f"{self.api_base}/actions/workflows/{WORKFLOW_FILE}/runs?event=workflow_dispatch"
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

        print(f"{Style.YELLOW}[!] Run dispatched. Check GitHub Actions console.{Style.RESET}")
        return None

    def monitor(self, run_id: int, poll_interval: int = 10) -> bool:
        url = f"{self.api_base}/actions/runs/{run_id}"
        jobs_url = f"{self.api_base}/actions/runs/{run_id}/jobs"
        start_time = time.time()

        print(f"{Style.CYAN}▶ Monitoring Matrix Execution across 9 parallel runners...{Style.RESET}\n")

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

                jobs_resp = requests.get(jobs_url, headers=self.headers, timeout=10)
                job_summaries = []
                if jobs_resp.status_code == 200:
                    jobs = jobs_resp.json().get("jobs", [])
                    for j in jobs:
                        j_name = j.get("name", "").replace(" Matrix", "").replace("Consolidate Master Matrix", "Consolidate")
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
                        print(f"{Style.GREEN}{Style.BOLD}🎉 PropDAO Matrix Backtest Completed Successfully on GitHub Actions!{Style.RESET}")
                    else:
                        print(f"{Style.YELLOW}⚠️ Matrix run concluded with status: {conclusion}{Style.RESET}")
                    self.download_artifacts(run_id)
                    return conclusion == "success"

            except Exception as e:
                pass

    def download_artifacts(self, run_id: int):
        print(f"\n{Style.CYAN}[*] Downloading generated reports and trade CSVs from GitHub Actions...{Style.RESET}")
        os.makedirs(REPORTS_DIR, exist_ok=True)

        url = f"{self.api_base}/actions/runs/{run_id}/artifacts"
        resp = requests.get(url, headers=self.headers, timeout=15)
        if resp.status_code != 200:
            print(f"{Style.RED}[!] Could not fetch artifacts: HTTP {resp.status_code}{Style.RESET}")
            return

        artifacts = resp.json().get("artifacts", [])
        if not artifacts:
            print(f"{Style.YELLOW}[!] No artifacts found for run #{run_id}.{Style.RESET}")
            return

        for a in artifacts:
            name = a.get("name")
            dl_url = a.get("archive_download_url")
            print(f"    • Downloading artifact: {Style.BOLD}{name}{Style.RESET}...")

            a_resp = requests.get(dl_url, headers=self.headers, timeout=60)
            if a_resp.status_code == 200:
                with zipfile.ZipFile(io.BytesIO(a_resp.content)) as zf:
                    for f in zf.namelist():
                        zf.extract(f, REPORTS_DIR)
                        print(f"      [+] Extracted: {os.path.join(REPORTS_DIR, f)}")
            else:
                print(f"      [!] Failed to download {name}: HTTP {a_resp.status_code}")

        print(f"\n{Style.GREEN}✓ All artifacts extracted locally into: {REPORTS_DIR}{Style.RESET}")


def main():
    parser = argparse.ArgumentParser(description="Dispatch PropDAO Multi-Asset Matrix Backtest to GitHub Actions")
    parser.add_argument("--start", type=str, default="2026-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2026-08-31", help="End date (YYYY-MM-DD)")
    parser.add_argument("--capital", type=float, default=25000.0, help="Initial capital in USDT")
    parser.add_argument("--maker-fee", type=float, default=0.015, help="Maker fee %")
    parser.add_argument("--taker-fee", type=float, default=0.045, help="Taker fee %")
    parser.add_argument("--max-dd-pct", type=float, default=2.0, help="Max drawdown %")
    parser.add_argument("--timeframes", type=str, default="5m,15m,30m,1h,4h,1d", help="Timeframes")
    parser.add_argument("--leverages", type=str, default="1,2", help="Leverages")
    parser.add_argument("--risks", type=str, default="1,2,5,10,15,20,25,50", help="Risk percentages")
    parser.add_argument("--slippages", type=str, default="0,1,2", help="Slippages")
    parser.add_argument("--ref", type=str, default="main", help="Git branch ref")

    args = parser.parse_args()

    dispatcher = PropDaoMatrixDispatcher()
    run_id = dispatcher.dispatch(
        start_date=args.start,
        end_date=args.end,
        capital=args.capital,
        maker_fee=args.maker_fee,
        taker_fee=args.taker_fee,
        max_dd_pct=args.max_dd_pct,
        timeframes=args.timeframes,
        leverages=args.leverages,
        risks=args.risks,
        slippages=args.slippages,
        ref=args.ref
    )

    if run_id:
        dispatcher.monitor(run_id)


if __name__ == "__main__":
    main()
