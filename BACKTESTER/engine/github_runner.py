"""
GitHub Actions Cloud Backtest Runner
====================================
Dispatches, monitors, reports failures, and downloads final artifact ZIPs for
backtests executed on GitHub Actions cloud runners.
"""

import os
import sys
import time
import re
import io
import json
import zipfile
import subprocess
from typing import Optional, Dict, Any, Tuple
from dataclasses import asdict

import requests

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Automatically load environment variables from .env
try:
    from kcex.config import load_env_file
    load_env_file()
except Exception:
    pass

from BACKTESTER.engine.config import BacktestConfig


class Style:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    BG_GREEN = "\033[42m\033[30m"
    BG_RED = "\033[41m\033[37m"


def get_git_remote_repo() -> Tuple[str, str]:
    """Detects GitHub repository owner and name from local git remote."""
    try:
        res = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            cwd=ROOT_DIR,
            check=True
        )
        url = res.stdout.strip()
        # Handle SSH and HTTPS formats:
        # e.g. https://github.com/SamratSinghPhysicist/KCEX_bot.git
        # or git@github.com:SamratSinghPhysicist/KCEX_bot.git
        match = re.search(r"github\.com[:/]([^/]+)/([^/\.]+)", url)
        if match:
            return match.group(1), match.group(2)
    except Exception:
        pass

    # Default fallback to known repository
    return "SamratSinghPhysicist", "PropDao_BOT"


def resolve_github_token(cli_token: Optional[str] = None) -> Optional[str]:
    """
    Resolves GitHub Personal Access Token from:
    1. CLI argument
    2. GITHUB_TOKEN or GH_TOKEN environment variables
    3. .env file in root directory
    4. gh auth token CLI command
    """
    if cli_token and cli_token.strip():
        return cli_token.strip()

    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        t = os.getenv(var)
        if t and t.strip():
            return t.strip()

    candidates = [
        os.path.join(ROOT_DIR, ".env"),
        r"D:\My_Bots\Trading\KCEX\.env"
    ]
    for env_path in candidates:
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("GITHUB_TOKEN=") or line.startswith("GH_TOKEN="):
                            val = line.split("=", 1)[1].strip().strip('"').strip("'")
                            if val:
                                return val
            except Exception:
                pass

    # Try gh auth token if gh CLI is available
    try:
        res = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=3
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass

    return None


class GitHubBacktestRunner:
    """Manages cloud backtest runs via GitHub Actions."""

    WORKFLOW_FILENAME = "backtest.yml"

    def __init__(
        self,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        token: Optional[str] = None,
        workflow_filename: str = "backtest.yml"
    ):
        det_owner, det_repo = get_git_remote_repo()
        self.owner = owner or os.getenv("GITHUB_REPOSITORY_OWNER") or det_owner
        self.repo = repo or os.getenv("GITHUB_REPOSITORY_NAME") or det_repo
        self.token = resolve_github_token(token)
        self.WORKFLOW_FILENAME = workflow_filename
        self.api_base = f"https://api.github.com/repos/{self.owner}/{self.repo}"

    @property
    def headers(self) -> Dict[str, str]:
        hdrs = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "KCEX-Backtester-Client"
        }
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        return hdrs

    def prompt_token_if_needed(self) -> bool:
        """Prompts the user interactively for a GitHub token if not present."""
        if self.token:
            return True

        print(f"\n{Style.YELLOW}{Style.BOLD}[!] GitHub Authentication Required{Style.RESET}")
        print("To trigger GitHub Actions workflows and download artifacts, a GitHub Personal Access Token is needed.")
        print(f"You can create a token with 'repo' or 'actions:write' scope at:")
        print(f"  👉 https://github.com/settings/tokens\n")
        print("Tip: You can also save it in your .env file as:")
        print("  GITHUB_TOKEN=ghp_yourTokenHere\n")

        inp = input("Enter GitHub Token (or press Enter to cancel): ").strip()
        if not inp:
            return False

        self.token = inp
        # Ask if user wants to save to .env
        save = input("Would you like to save this token to your local .env file? [Y/n]: ").strip().lower()
        if save not in ("n", "no"):
            env_path = os.path.join(ROOT_DIR, ".env")
            try:
                with open(env_path, "a", encoding="utf-8") as f:
                    f.write(f"\n# GitHub Personal Access Token for Cloud Backtesting\nGITHUB_TOKEN={inp}\n")
                print(f"{Style.GREEN}✓ Saved GITHUB_TOKEN to .env{Style.RESET}")
            except Exception as e:
                print(f"[!] Could not write to .env: {e}")

        return True

    def build_workflow_inputs(self, config: BacktestConfig) -> Dict[str, str]:
        """Converts BacktestConfig to GitHub Actions workflow_dispatch inputs."""
        # Clean symbol (e.g. TRUMP_USDT)
        sym = config.symbol.upper()
        if "_" not in sym and sym.endswith("USDT"):
            sym = f"{sym[:-4]}_USDT"

        start_date = str(config.start_time)[:10] if config.start_time else "2026-01-01"
        end_date = str(config.end_time)[:10] if config.end_time else "2026-08-31"

        vol_mode = config.volume_mode or "MULTIPLIER"
        if vol_mode == "MARGIN_PCT":
            vol_val = str(config.margin_pct if config.margin_pct is not None else 10.0)
        elif vol_mode == "FIXED_MARGIN":
            vol_val = str(config.fixed_margin_usdt if config.fixed_margin_usdt is not None else 5.0)
        elif vol_mode == "CONTRACTS":
            vol_val = str(config.volume_contracts or (2 if "TRUMP" in sym else 1))
        elif vol_mode == "MIN":
            vol_val = "1.0"
        else: # MULTIPLIER
            vol_val = str(config.volume_multiplier if config.volume_multiplier is not None else (2.0 if "TRUMP" in sym else 1.0))

        maker_fee = str(config.maker_fee_override * 100.0) if config.maker_fee_override is not None else "0.0"
        taker_fee = str(config.taker_fee_override * 100.0) if config.taker_fee_override is not None else "0.0"

        sl_price = str(config.sl_price_pct if config.sl_price_pct is not None else 0.5)

        filters_dict = {
            "duration_filter": getattr(config, "duration_filter_enabled", False),
            "duration_deep_monitor": getattr(config, "duration_deep_monitor_seconds", 60.0),
            "duration_max_hold": getattr(config, "duration_max_hold_seconds", 90.0),
            "duration_action": getattr(config, "duration_action", "CLOSE") or "CLOSE",
            "adx_filter": getattr(config, "adx_filter_enabled", False),
            "adx_period": getattr(config, "adx_period", 14),
            "adx_threshold": getattr(config, "adx_threshold", 25.0),
            "htf_trend_filter": getattr(config, "htf_trend_filter_enabled", False),
            "htf_ema_period": getattr(config, "htf_ema_period", 200),
            "htf_timeframe": getattr(config, "htf_timeframe", "15m") or "15m",
            "hourly_filter": getattr(config, "hourly_filter_enabled", False),
            "hourly_blacklist": ",".join(str(x) for x in (getattr(config, "hourly_blacklist_utc", []) or [])),
            "direction_bias": getattr(config, "direction_bias", "BOTH") or "BOTH"
        }

        strat = config.strategy_mode.upper() if config.strategy_mode else "STOCH_RSI"
        if strat in ("SMART", "SMART_STRATEGY"):
            strat = "SMART_STRATEGY"
        elif strat in ("EMA", "EMA_CROSSOVER", "CROSSOVER"):
            strat = "EMA_CROSSOVER"
        elif strat in ("ML", "ML_1M", "ML_MODEL", "ML_1M_MODEL"):
            strat = "ML_1M_MODEL"
        elif strat in ("ORDER_BLOCK_DEMAND", "ORDER_BOOK_DEMAND", "ORDER_BLOCK", "DEMAND_BLOCK", "SMC"):
            strat = "ORDER_BLOCK_DEMAND"
        else:
            strat = "STOCH_RSI"

        quant_dict = {
            "invert_signal": bool(getattr(config, "invert_signal", False)),
            "enable_slippage": bool(getattr(config, "slippage_enabled", False)),
            "slippage_ticks": int(getattr(config, "slippage_ticks", 0)),
            "ratchet": bool(getattr(config, "ratchet_enabled", False)),
            "ratchet_trigger_ticks": float(getattr(config, "ratchet_trigger_ticks", 1.0)),
            "ratchet_stall_seconds": float(getattr(config, "ratchet_stall_seconds", 10.0)),
            "ratchet_tighten_ticks": float(getattr(config, "ratchet_tighten_ticks", 1.0)),
            "ratchet_breakeven_ticks": float(getattr(config, "ratchet_breakeven_ticks", 2.5)),
            "execution_style": str(getattr(config, "execution_style", "PURE_MARKET")),
            "use_atr_targets": bool(getattr(config, "use_atr_targets", False)),
            "atr_tp_multiplier": float(getattr(config, "atr_tp_multiplier", 2.0)),
            "atr_sl_multiplier": float(getattr(config, "atr_sl_multiplier", 1.0)),
            "volume_filter_enabled": bool(getattr(config, "volume_filter_enabled", False)),
            "volume_filter_multiplier": float(getattr(config, "volume_filter_multiplier", 1.2)),
            "queue_dynamics_enabled": bool(getattr(config, "queue_dynamics_enabled", False)),
            "resting_limit_tp": bool(getattr(config, "resting_limit_tp", False)),
            "simulate_intra_tick_liquidation": bool(getattr(config, "simulate_intra_tick_liquidation", True)),
            "margin_pct": float(config.margin_pct) if config.margin_pct is not None else None,
            "fixed_margin_usdt": float(config.fixed_margin_usdt) if config.fixed_margin_usdt is not None else None,
            "volume_contracts": int(config.volume_contracts) if config.volume_contracts is not None else None,
            "volume_multiplier": float(config.volume_multiplier) if config.volume_multiplier is not None else None
        }

        return {
            "symbol": sym,
            "timeframe": config.timeframe,
            "strategy": strat,
            "ema_preset": config.ema_preset or "5/13",
            "stoch_preset": config.stoch_preset or "FAST_SCALP",
            "start_date": start_date,
            "end_date": end_date,
            "use_ticks": "true" if config.use_tick_data else "false",
            "fee_mode": config.fee_mode,
            "maker_fee": maker_fee,
            "taker_fee": taker_fee,
            "volume_mode": vol_mode,
            "volume_value": vol_val,
            "tp_ticks": str(config.tp_ticks),
            "sl_mode": config.sl_mode,
            "sl_ticks": str(config.sl_ticks or 10),
            "sl_roe": str(config.sl_roe_pct),
            "sl_price": sl_price,
            "leverage": str(config.leverage),
            "capital": str(config.initial_balance_usdt),
            "max_trades": str(config.max_trades),
            "filters_json": json.dumps(filters_dict),
            "quant_params_json": json.dumps(quant_dict)
        }

    def dispatch_workflow(self, inputs: Dict[str, str], ref: str = "main") -> bool:
        """Sends workflow_dispatch trigger to GitHub API."""
        url = f"{self.api_base}/actions/workflows/{self.WORKFLOW_FILENAME}/dispatches"
        payload = {
            "ref": ref,
            "inputs": inputs
        }
        resp = requests.post(url, headers=self.headers, json=payload, timeout=15)
        if resp.status_code == 204:
            return True
        elif resp.status_code == 401:
            print(f"{Style.RED}[!] Authentication Failed (401): Check your GitHub Token permissions.{Style.RESET}")
            return False
        elif resp.status_code == 404:
            print(f"{Style.RED}[!] Workflow or Repository not found (404): {self.owner}/{self.repo}/{self.WORKFLOW_FILENAME}{Style.RESET}")
            print("Ensure the workflow file is committed and pushed to GitHub.")
            return False
        else:
            print(f"{Style.RED}[!] Failed to trigger workflow: HTTP {resp.status_code} - {resp.text}{Style.RESET}")
            return False

    def find_dispatched_run(self, dispatch_time_epoch: float, timeout_seconds: int = 45) -> Optional[Dict[str, Any]]:
        """Polls for the newly dispatched workflow run."""
        url = f"{self.api_base}/actions/workflows/{self.WORKFLOW_FILENAME}/runs?event=workflow_dispatch"
        start_poll = time.time()
        
        while time.time() - start_poll < timeout_seconds:
            time.sleep(3)
            try:
                resp = requests.get(url, headers=self.headers, timeout=10)
                if resp.status_code == 200:
                    runs = resp.json().get("workflow_runs", [])
                    if runs:
                        latest = runs[0]
                        created_str = latest.get("created_at", "")
                        # Check if this run is recent (within 2 minutes of dispatch)
                        return latest
            except Exception:
                pass
        return None

    def poll_workflow_run(self, run_id: int, poll_interval: int = 5) -> Tuple[str, Optional[str]]:
        """
        Polls a running workflow until completion.
        Returns (status, conclusion).
        """
        url = f"{self.api_base}/actions/runs/{run_id}"
        jobs_url = f"{self.api_base}/actions/runs/{run_id}/jobs"
        start_time = time.time()

        print(f"\n{Style.CYAN}{Style.BOLD}▶ Monitoring Cloud Execution in Real-Time...{Style.RESET}")
        
        last_step_name = ""

        while True:
            elapsed = time.time() - start_time
            try:
                resp = requests.get(url, headers=self.headers, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    status = data.get("status")
                    conclusion = data.get("conclusion")

                    # Check current active step if in progress
                    current_step = ""
                    try:
                        jobs_resp = requests.get(jobs_url, headers=self.headers, timeout=5)
                        if jobs_resp.status_code == 200:
                            jobs_data = jobs_resp.json().get("jobs", [])
                            if jobs_data:
                                steps = jobs_data[0].get("steps", [])
                                for s in steps:
                                    if s.get("status") == "in_progress":
                                        current_step = f" -> Step: {s.get('name')}"
                                        break
                    except Exception:
                        pass

                    elapsed_str = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
                    status_colored = f"{Style.YELLOW}{status}{Style.RESET}" if status != "completed" else (f"{Style.GREEN}completed{Style.RESET}" if conclusion == "success" else f"{Style.RED}completed ({conclusion}){Style.RESET}")
                    
                    sys.stdout.write(f"\r⏱  [{elapsed_str}] Status: {status_colored}{current_step}          ")
                    sys.stdout.flush()

                    if status == "completed":
                        print("\n")
                        return status, conclusion

            except Exception as e:
                pass

            time.sleep(poll_interval)

    def diagnose_failure(self, run_id: int) -> None:
        """Fetches failed steps and job details to inform the user clearly."""
        url = f"{self.api_base}/actions/runs/{run_id}/jobs"
        print(f"\n{Style.BG_RED}{Style.BOLD} ⚠️ WORKFLOW EXECUTION FAILED ⚠️ {Style.RESET}")
        print(f"Direct link to full GitHub logs: https://github.com/{self.owner}/{self.repo}/actions/runs/{run_id}\n")

        try:
            resp = requests.get(url, headers=self.headers, timeout=10)
            if resp.status_code == 200:
                jobs = resp.json().get("jobs", [])
                for job in jobs:
                    job_name = job.get("name", "Backtest Job")
                    conclusion = job.get("conclusion", "unknown")
                    if conclusion != "success":
                        print(f"  • Job '{job_name}' ended with status: {Style.RED}{conclusion}{Style.RESET}")
                        for step in job.get("steps", []):
                            s_conc = step.get("conclusion")
                            if s_conc and s_conc not in ("success", "skipped"):
                                print(f"    ❌ Failed Step: {Style.BOLD}{step.get('name')}{Style.RESET} (Conclusion: {s_conc})")
        except Exception as e:
            print(f"[!] Could not retrieve failure step details: {e}")

    def download_and_save_artifacts(
        self,
        run_id: int,
        config: BacktestConfig,
        output_dir: str = "BACKTESTER/reports"
    ) -> Optional[str]:
        """
        Downloads artifacts for the workflow run, generates a clear, descriptive
        zip file name with the pair and configurations, and extracts reports.
        """
        os.makedirs(output_dir, exist_ok=True)
        url = f"{self.api_base}/actions/runs/{run_id}/artifacts"

        resp = requests.get(url, headers=self.headers, timeout=10)
        if resp.status_code != 200:
            print(f"[!] Could not retrieve artifact list: HTTP {resp.status_code}")
            return None

        artifacts = resp.json().get("artifacts", [])
        if not artifacts:
            print(f"{Style.YELLOW}[!] No artifacts found for run #{run_id}.{Style.RESET}")
            return None

        art = artifacts[0]
        art_id = art.get("id")
        art_size_mb = art.get("size_in_bytes", 0) / (1024 * 1024)

        # Construct clear descriptive filename
        sym = config.symbol.upper()
        strat = config.strategy_mode
        tf = config.timeframe
        lev = f"{config.leverage}x"
        tp = f"tp{config.tp_ticks}pu"
        sl = f"slROE{config.sl_roe_pct:g}" if config.sl_mode == "ROE" else f"sl{config.sl_ticks}t"
        start = str(config.start_time)[:10] if config.start_time else "start"
        end = str(config.end_time)[:10] if config.end_time else "end"
        
        descriptive_name = f"backtest_{sym}_{strat}_{tf}_{lev}_{tp}_{sl}_{start}_to_{end}_run{run_id}.zip"
        target_path = os.path.join(output_dir, descriptive_name)

        download_url = f"{self.api_base}/actions/artifacts/{art_id}/zip"
        print(f"[*] Downloading backtest artifact ZIP ({art_size_mb:.2f} MB)...")
        print(f"    Destination: {Style.BOLD}{target_path}{Style.RESET}")

        dl_resp = requests.get(download_url, headers=self.headers, stream=True, timeout=60)
        if dl_resp.status_code != 200:
            print(f"[!] Artifact download failed: HTTP {dl_resp.status_code}")
            return None

        # Write zip file
        with open(target_path, "wb") as f:
            for chunk in dl_resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)

        print(f"{Style.GREEN}✓ Successfully downloaded and saved artifact ZIP:{Style.RESET}")
        print(f"  📦 {os.path.abspath(target_path)} ({os.path.getsize(target_path)/1024:.1f} KB)\n")

        # Extract and display summary if contained
        try:
            with zipfile.ZipFile(target_path, 'r') as z:
                z.extractall(output_dir)
                for member in z.namelist():
                    if member.endswith("_summary.md"):
                        md_path = os.path.join(output_dir, member)
                        if os.path.exists(md_path):
                            print(f"{Style.CYAN}{'='*78}{Style.RESET}")
                            print(f"{Style.BOLD}                📊 CLOUD BACKTEST RESULTS SUMMARY{Style.RESET}")
                            print(f"{Style.CYAN}{'='*78}{Style.RESET}")
                            with open(md_path, "r", encoding="utf-8") as sm:
                                print(sm.read())
        except Exception as e:
            print(f"[!] Note: Could not auto-extract summary preview: {e}")

        # Auto-Index for Comparison & Analytics Studio
        try:
            from BACKTESTER.analytics.indexer import ReportIndexer
            ReportIndexer(reports_dir=output_dir).get_all_runs(force_reindex=True)
            print(f"  ⚡ {Style.GREEN}Auto-indexed into Comparison Studio! (Run `python run_analytics.py`){Style.RESET}\n")
        except Exception:
            pass

        return target_path


    def run_cloud_backtest(
        self,
        config: BacktestConfig,
        output_dir: str = "BACKTESTER/reports"
    ) -> bool:
        """Complete end-to-end cloud backtest execution pipeline."""
        if not self.prompt_token_if_needed():
            print("[!] Cloud execution cancelled: Missing GitHub Token.")
            return False

        inputs = self.build_workflow_inputs(config)

        print(f"\n{Style.CYAN}{'='*78}{Style.RESET}")
        print(f"{Style.BOLD}           DISPATCHING STRATEGY BACKTEST TO GITHUB ACTIONS{Style.RESET}")
        print(f"{Style.CYAN}{'='*78}{Style.RESET}")
        print(f"Repository:       {self.owner}/{self.repo}")
        print(f"Workflow:         .github/workflows/{self.WORKFLOW_FILENAME}")
        print(f"Trading Pair:     {inputs['symbol']}")
        print(f"Candle Timeframe: {inputs['timeframe']}")
        print(f"Strategy:         {inputs['strategy']}")
        print(f"Leverage:         {inputs['leverage']}x Isolated")
        v_mode = inputs.get("volume_mode", "MULTIPLIER")
        v_val = inputs.get("volume_value", "1.0")
        if v_mode == "MARGIN_PCT":
            v_desc = f"{float(v_val):.1f}% Available Margin (Dynamic Sizing)"
        elif v_mode == "FIXED_MARGIN":
            v_desc = f"{float(v_val):.2f} USDT Fixed Margin"
        elif v_mode == "CONTRACTS":
            v_desc = f"{int(float(v_val))} contracts"
        elif v_mode == "MIN":
            v_desc = "1x min volume"
        else:
            v_desc = f"{float(v_val):g}x min volume"
        print(f"Trade Volume:     {v_desc} ({v_mode})")
        strat_name = inputs.get('strategy', '')
        if strat_name in ('ORDER_BLOCK_DEMAND', 'ORDER_BOOK_DEMAND'):
            print(f"Take Profit:      DYNAMIC (1:2 R:R | 50% exit at 1:1 + Breakeven Runner) [Fallback: +{inputs['tp_ticks']}t]")
            print(f"Stop Loss:        DYNAMIC (Order Block Structural Extrema + 1t buffer) [Fallback: {inputs['sl_ticks']}t]")
        elif strat_name in ('ML_1M_MODEL', 'ML'):
            print(f"Take Profit:      DYNAMIC (Multi-Horizon HistGBDT ATR Scaler)")
            print(f"Stop Loss:        DYNAMIC (Multi-Horizon HistGBDT ATR Scaler)")
        else:
            print(f"Take Profit:      +{inputs['tp_ticks']} pu ticks")
            sl_lbl = f"{inputs['sl_mode']} ({inputs['sl_roe']}% ROE)" if inputs['sl_mode'] == 'ROE' else f"{inputs['sl_ticks']} ticks"
            print(f"Stop Loss:        {sl_lbl}")
        print(f"Date Range:       {inputs['start_date']} to {inputs['end_date']}")
        print(f"Simulation Mode:  {'HIGH-FIDELITY TICK TRADES (Streaming ms trades)' if inputs['use_ticks'] == 'true' else 'OHLCV ONLY (1m Sub-Candle Disambiguation & Conservative SL)'}")
        filters = json.loads(inputs.get("filters_json", "{}")) if "filters_json" in inputs else {}
        dur_desc = (
            f"ENABLED (Monitor >{filters.get('duration_deep_monitor', 60.0)}s, Action: {filters.get('duration_action', 'CLOSE')} at {filters.get('duration_max_hold', 90.0)}s)"
            if filters.get("duration_filter", False)
            else "DISABLED"
        )
        print(f"Duration Filter:  {dur_desc}")
        regime_parts = []
        if filters.get("adx_filter", False):
            regime_parts.append(f"ADX({filters.get('adx_period', 14)})>={filters.get('adx_threshold', 25.0)}")
        if filters.get("htf_trend_filter", False):
            regime_parts.append(f"HTF {filters.get('htf_ema_period', 200)} EMA ({filters.get('htf_timeframe', '15m')})")
        if filters.get("hourly_filter", False):
            regime_parts.append(f"Hourly Block [{filters.get('hourly_blacklist', '')}]")
        if filters.get("direction_bias", "BOTH") != "BOTH":
            regime_parts.append(f"Bias: {filters.get('direction_bias')}")
        print(f"Regime Filters:   {', '.join(regime_parts) if regime_parts else 'DISABLED (Baseline)'}")
        quant = json.loads(inputs.get("quant_params_json", "{}")) if "quant_params_json" in inputs else {}
        slip_str = f"ENABLED ({quant.get('slippage_ticks', 0)} ticks adverse)" if quant.get("enable_slippage") else "DISABLED (0 ticks)"
        print(f"Slippage Engine:  {slip_str}")
        sig_str = "INVERTED (Exhaustion Fading)" if quant.get("invert_signal") else "DIRECT (Momentum)"
        print(f"Signal Paradigm:  {sig_str}")
        ratch_str = (
            f"ENABLED (T1: +{quant.get('ratchet_trigger_ticks', 1.0)}t/{quant.get('ratchet_stall_seconds', 10.0)}s -> -{quant.get('ratchet_tighten_ticks', 1.0)}t, T2: +{quant.get('ratchet_breakeven_ticks', 2.5)}t -> BE)"
            if quant.get("ratchet")
            else "DISABLED"
        )
        print(f"Tick Ratchet:     {ratch_str}")
        print(f"Execution Style:  {quant.get('execution_style', 'PURE_MARKET')}")
        print(f"{Style.CYAN}{'='*78}{Style.RESET}\n")

        t_dispatch = time.time()
        print("[*] Triggering workflow dispatch via GitHub REST API...")
        if not self.dispatch_workflow(inputs):
            return False

        print(f"{Style.GREEN}✓ Workflow dispatch accepted by GitHub.{Style.RESET}")
        print("[*] Locating newly created workflow run...")

        run_info = self.find_dispatched_run(t_dispatch)
        if not run_info:
            print("[!] Could not locate workflow run within 45s. Check GitHub Actions tab online.")
            return False

        run_id = run_info.get("id")
        run_url = run_info.get("html_url")
        print(f"{Style.GREEN}✓ Active Workflow Run #{run_id} Found!{Style.RESET}")
        print(f"  🔗 Direct Link: {Style.BOLD}{run_url}{Style.RESET}")

        # Poll workflow execution
        status, conclusion = self.poll_workflow_run(run_id)

        if conclusion != "success":
            self.diagnose_failure(run_id)
            # Try to see if any partial artifact was created
            print("[*] Checking for any partial reports or failure artifacts...")
            self.download_and_save_artifacts(run_id, config, output_dir=output_dir)
            return False

        print(f"{Style.GREEN}{Style.BOLD}🎉 Cloud Backtest Execution Completed Successfully!{Style.RESET}")
        
        # Download and save artifact ZIP
        zip_path = self.download_and_save_artifacts(run_id, config, output_dir=output_dir)
        return zip_path is not None
