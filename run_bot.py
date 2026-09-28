"""
PropDAO Automated Trading Bot Entrypoint
========================================
Launches the automated Order Block + Demand Strategy execution engine in LIVE or PAPER mode.

Usage:
    # Paper trading mode (default, no API key required)
    python run_bot.py --mode paper --symbol BTCUSDC --timeframe 15m

    # Live trading mode (requires PROPDAO_API_KEY)
    PROPDAO_API_KEY=pd_live_... python run_bot.py --mode live --symbol BTCUSDC --timeframe 15m
"""

from __future__ import annotations
import argparse
import logging
import os
import sys
from propdao.models import EngineMode
from engine.execution_engine import ExecutionEngine

# Load .env file if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Reconfigure stdout to UTF-8 on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("propdao_bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("PropDAOBot")


def main():
    default_mode = os.getenv("PROPDAO_MODE", "paper").lower()
    if default_mode not in ("live", "paper", "mock"):
        default_mode = "paper"

    parser = argparse.ArgumentParser(description="PropDAO Trading Bot")
    parser.add_argument("--mode", type=str, choices=["live", "paper", "mock"], default=default_mode, help="Execution mode (live=Real Eval Account, paper=PropDAO Trial Account, mock=Local offline mock)")
    parser.add_argument("--symbol", type=str, default=os.getenv("PROPDAO_SYMBOL", "BTCUSDC"), help="Trading symbol (e.g. BTCUSDC)")
    parser.add_argument("--timeframe", type=str, default=os.getenv("PROPDAO_TIMEFRAME", "15m"), help="Strategy candle timeframe")
    parser.add_argument("--account", type=str, default=os.getenv("PROPDAO_ACCOUNT", None), help="PropDAO account ID (e.g. PROP-L6AS4Z5H for Trial, PROP-C137DA4E for Eval)")
    parser.add_argument("--api-key", type=str, default=os.getenv("PROPDAO_API_KEY", None), help="PropDAO Bearer API Key")
    parser.add_argument("--balance", type=float, default=25000.0, help="Initial balance for offline mock testing")
    parser.add_argument("--leverage", type=float, default=float(os.getenv("PROPDAO_LEVERAGE", 2.0)), help="Target leverage (e.g. 2.0)")
    parser.add_argument("--risk", type=float, default=float(os.getenv("PROPDAO_RISK_FRACTION", 0.25)), help="Risk percentage or fraction of roomUsd budget (e.g. 25 for 25%% or 0.25)")
    parser.add_argument("--interval", type=float, default=3.0, help="Main polling loop interval in seconds")
    parser.add_argument("--heartbeat", type=float, default=15.0, help="Heartbeat telemetry interval in seconds (default: 15.0)")
    parser.add_argument("--no-partial-tp", action="store_true", help="Disable 1:1 partial TP and breakeven lock")

    args = parser.parse_args()

    # Normalize risk fraction (accepts 25 or 0.25)
    risk_frac = (args.risk / 100.0) if args.risk > 1.0 else args.risk

    if args.mode == "live":
        engine_mode = EngineMode.LIVE
        if not args.api_key:
            logger.critical("[ERROR] LIVE mode selected but no PROPDAO_API_KEY was provided! Set PROPDAO_API_KEY in .env or use --api-key.")
            sys.exit(1)
    elif args.mode == "paper":
        if args.api_key:
            engine_mode = EngineMode.PAPER
        else:
            logger.warning("[WARNING] No PROPDAO_API_KEY found for PropDAO Trial mode. Falling back to local offline mock engine.")
            engine_mode = EngineMode.MOCK
    else:
        engine_mode = EngineMode.MOCK

    bot = ExecutionEngine(
        symbol=args.symbol.upper(),
        timeframe=args.timeframe,
        mode=engine_mode,
        api_key=args.api_key,
        account_id=args.account,
        starting_balance=args.balance,
        risk_fraction=risk_frac,
        leverage=args.leverage,
        enable_partial_tp=not args.no_partial_tp,
        loop_interval_seconds=args.interval,
        heartbeat_interval_seconds=args.heartbeat
    )

    try:
        bot.start()
    except KeyboardInterrupt:
        logger.info("Bot interrupted by user. Shutting down...")
        bot.stop()


if __name__ == "__main__":
    main()
