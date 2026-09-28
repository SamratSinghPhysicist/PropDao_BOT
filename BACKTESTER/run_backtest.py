"""
BACKTESTER Runner & Interactive Wizard
======================================
Command-line interface and interactive wizard for running high-fidelity backtests
on historical OHLCV and tick trade data using the exact same strategy and execution
logic as the live trading engine.

Supports:
1. Cloud execution on GitHub Actions (free cloud compute, auto-downloads artifact ZIP)
2. Local execution directly on this machine
"""

import json
import sys
import os
import time
import argparse
import datetime
from typing import Tuple

# Ensure utf-8 output encoding on Windows consoles
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# Ensure project root is in path
BACKTESTER_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.abspath(os.path.join(BACKTESTER_DIR, ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Automatically load environment variables from .env
try:
    from kcex.config import load_env_file
    load_env_file()
except Exception:
    pass

from BACKTESTER.engine.config import BacktestConfig
from BACKTESTER.engine.scanner import DataScanner, canonicalize_symbol, format_ms_to_utc
from BACKTESTER.engine.market_sim import BacktestMarket
from BACKTESTER.engine.execution_sim import BacktestExecutionEngine
from BACKTESTER.engine.metrics import PerformanceCalculator
from BACKTESTER.engine.reporting import BacktestReporter
from BACKTESTER.engine.github_runner import GitHubBacktestRunner
from kcex.engine.models import OrderDirection
import settings


def print_banner():
    banner = r"""
==============================================================================
   ____          _____ _  _______ ______ _____ _______ ______ _____  
  |  _ \   /\   / ____| |/ /__   __|  ____|/ ____|__   __|  ____|  __ \ 
  | |_) | /  \ | |    | ' /   | |  | |__  | (___    | |  | |__  | |__) |
  |  _ < / /\ \| |    |  <    | |  |  __|  \___ \   | |  |  __| |  _  / 
  | |_) / ____ \ |____| . \   | |  | |____ ____) |  | |  | |____| | \ \ 
  |____/_/    \_\_____|_|\_\  |_|  |______|_____/   |_|  |______|_|  \_\
                                                                        
       HIGH-FIDELITY DUAL-FEED STRATEGY BACKTESTING ENGINE
=============================================================================="""
    print(banner)


def run_interactive_wizard(scanner: DataScanner) -> Tuple[BacktestConfig, str]:
    """
    Prompts the user interactively to configure their backtest run.
    Supports selecting empirically validated presets or crafting a custom manual run.
    Returns (BacktestConfig, target) where target is 'github' or 'local'.
    """
    print("\n" + "=" * 78)
    print("          INTERACTIVE BACKTEST WIZARD (Press [Enter] for defaults)")
    print("=" * 78)

    catalog = scanner.scan()
    available_symbols = list(catalog.keys())

    # 0. Strategy Preset Selection
    active_preset_name = getattr(settings, "ACTIVE_PRESET", "BTC_ORDER_BLOCK_DEMAND").upper()
    print("\n0. Strategy Preset Selection:")
    print("   [1] BTC_ORDER_BLOCK_DEMAND       -> Smart Money Concepts 15m (BOS Body-Close, Wick-to-Wick OB, FVG, 1:2 RR, 2.0x Lev) [DEFAULT / PROPDAO]")
    print("   [2] ETH_ORDER_BLOCK_DEMAND       -> Smart Money Concepts 15m (BOS Body-Close, Wick-to-Wick OB, FVG, 1:2 RR, 2.0x Lev) [PROPDAO]")
    print("   [3] SOL_ORDER_BLOCK_DEMAND       -> Smart Money Concepts 15m (BOS Body-Close, Wick-to-Wick OB, FVG, 1:2 RR, 2.0x Lev) [PROPDAO]")
    print("   [4] TRUMP_ORDER_BLOCK_DEMAND     -> Smart Money Concepts 15m (BOS Body-Close, Wick-to-Wick OB, FVG, 1:2 RR, 1.0x Lev) [PROPDAO]")
    print("   [5] DOGE_ORDER_BLOCK_DEMAND      -> Smart Money Concepts 15m (BOS Body-Close, Wick-to-Wick OB, FVG, 1:2 RR, 1.0x Lev) [PROPDAO]")
    print("   [6] CUSTOM / MANUAL SETUP        -> Step-by-step custom wizard configuration")

    preset_map = {
        "1": "BTC_ORDER_BLOCK_DEMAND",
        "2": "ETH_ORDER_BLOCK_DEMAND",
        "3": "SOL_ORDER_BLOCK_DEMAND",
        "4": "TRUMP_ORDER_BLOCK_DEMAND",
        "5": "DOGE_ORDER_BLOCK_DEMAND",
        "6": "CUSTOM"
    }

    def_preset_choice = "1"
    for k, v in preset_map.items():
        if v == active_preset_name:
            def_preset_choice = k
            break

    preset_choice = input(f"   Select Preset [default: {def_preset_choice} ({active_preset_name})]: ").strip()
    if not preset_choice:
        preset_choice = def_preset_choice

    if preset_choice in ("1", "2", "3", "4", "5"):
        chosen_preset = preset_map[preset_choice]
        preset_cfg = settings.get_active_preset_config(chosen_preset) if hasattr(settings, "get_active_preset_config") else {}

        is_ml_p = "ML" in chosen_preset or preset_cfg.get("strategy_mode", "").upper() in ("ML", "ML_1M", "ML_MODEL", "ML_1M_MODEL")
        is_smc_p = "ORDER_BLOCK" in chosen_preset or "DEMAND" in chosen_preset or preset_cfg.get("strategy_mode", "").upper() in ("ORDER_BLOCK_DEMAND", "SMC")
        is_mm_p = "TICK_CONSTRAINED" in chosen_preset or preset_cfg.get("strategy_mode", "").upper() in ("TICK_CONSTRAINED_MM", "MM")

        print(f"\n   ✅ Loaded Preset: {preset_cfg.get('name', chosen_preset)}")
        print(f"      • Trading Pair:    {preset_cfg.get('symbol')} ({preset_cfg.get('timeframe', '1m')} timeframe)")
        print(f"      • Strategy:        {preset_cfg.get('strategy_mode')}")
        print(f"      • Signal Mode:     {'INVERTED (Exhaustion Fading)' if preset_cfg.get('invert_signal') else 'DIRECT (Momentum)'}")
        if is_smc_p:
            print(f"      • Take Profit:     DYNAMIC 1:2 R:R (50% partial exit at 1:1 + Breakeven Runner) [Fallback: +{preset_cfg.get('tp_ticks', 10)}t]")
            print(f"      • Stop Loss:       DYNAMIC Structural Zone Extrema (Order Block Low/High + buffer) [Fallback: {preset_cfg.get('sl_ticks', 5)}t]")
        elif is_ml_p:
            print(f"      • Take Profit:     DYNAMIC ATR (~{preset_cfg.get('tp_atr_mult', 1.9)}x ATR)")
            print(f"      • Stop Loss:       DYNAMIC ATR (~{preset_cfg.get('sl_atr_mult', 1.0)}x ATR)")
        elif is_mm_p:
            print(f"      • Take Profit:     +{preset_cfg.get('tp_ticks', 1)} tick | Stop Loss: {preset_cfg.get('sl_ticks', 3)} ticks (Micro-Stop)")
            print(f"      • Microstructure:  OFI Threshold ({preset_cfg.get('max_ofi_threshold', 0.40):.2f}) | Min Tick: {preset_cfg.get('min_tick_bps', 4.0):.1f} bps")
        else:
            print(f"      • Take Profit:     +{preset_cfg.get('tp_ticks')} ticks | Stop Loss: {preset_cfg.get('sl_ticks')} ticks ({preset_cfg.get('sl_mode')})")
        print(f"      • Execution Style: {preset_cfg.get('execution_style')}")
        print(f"      • Tick Ratchet:    {'ENABLED' if preset_cfg.get('ratchet_enabled') else 'DISABLED'}")

        # Display empirical backtest matrix from settings.py if available
        b_res = preset_cfg.get("backtest_results_by_slippage", {})
        if b_res:
            print("\n      📈 Validated Performance Matrix by Slippage Friction:")
            for slip_key, res_data in b_res.items():
                s_label = slip_key.replace("slippage_", "").upper()
                print(f"         • {s_label:>3}: Net ${res_data.get('net_profit_usdt', 0):+.2f} USDT | PF: {res_data.get('profit_factor', 0):.2f} | Sortino: {res_data.get('sortino_ratio', 0):.2f} | DD: {res_data.get('max_drawdown_pct', 0):.3f}% ({res_data.get('verdict', '')})")

        # 1. Execution Target Selection
        print("\n1. Select Execution Target:")
        print("   [1] GitHub Actions Cloud -> Free cloud runner, no CPU lag, auto-downloads artifact ZIP [Default / PropDAO]")
        print("   [2] Locally              -> Run directly on this computer")
        target_choice = input("   Select target [default: 1 (GitHub Actions Cloud)]: ").strip()
        target = "local" if target_choice in ("2", "local", "l") else "github"

        # 2. Historical Date Range
        p_sym = preset_cfg.get("symbol", "BTCUSDC")
        p_tf = preset_cfg.get("timeframe", "15m")
        sym_cat = catalog.get(p_sym)
        start_val = "2026-01-01"
        end_val = "2026-08-31"
        print(f"\n2. Select Historical Date Range for {p_sym}:")
        date_options = []
        # Option 1: Standard PropDAO 2026 Evaluation Period
        date_options.append(("PropDAO Standard 2026 Evaluation (Default: 2026-01-01 to 2026-08-31)", "2026-01-01", "2026-08-31"))

        if sym_cat:
            ov_s, ov_e = sym_cat.get_overlap_range(p_tf)
            if ov_s and ov_e:
                date_options.append((f"High-Fidelity Overlap Range (with tick trades): {format_ms_to_utc(ov_s)[:10]} to {format_ms_to_utc(ov_e)[:10]}", format_ms_to_utc(ov_s)[:10], format_ms_to_utc(ov_e)[:10]))
            o_s, o_e = sym_cat.get_timeframe_range(p_tf)
            if o_s and o_e:
                date_options.append((f"Entire OHLCV Range: {format_ms_to_utc(o_s)[:10]} to {format_ms_to_utc(o_e)[:10]}", format_ms_to_utc(o_s)[:10], format_ms_to_utc(o_e)[:10]))

        for idx, (label, _, _) in enumerate(date_options, 1):
            print(f"   [{idx}] {label}")
        custom_idx = len(date_options) + 1
        print(f"   [{custom_idx}] Custom Date Range")

        r_choice = input(f"   Select Range [default: 1]: ").strip() or "1"
        if r_choice.isdigit() and 1 <= int(r_choice) <= len(date_options):
            _, start_val, end_val = date_options[int(r_choice) - 1]
        elif r_choice == str(custom_idx):
            start_val = input("   Enter Start Date (YYYY-MM-DD) [default: 2026-01-01]: ").strip() or "2026-01-01"
            end_val = input("   Enter End Date (YYYY-MM-DD) [default: 2026-08-31]: ").strip() or "2026-08-31"
        else:
            _, start_val, end_val = date_options[0]

        # 3. Adverse Slippage Stress Test
        print("\n3. Realistic Adverse Slippage Simulation:")
        print("   [1] 1 tick adverse  (Realistic PropDAO execution latency) [Default / Recommended]")
        print("   [2] 0 ticks (Zero Slippage / Maker Execution)")
        print("   [3] 2 ticks adverse (High-friction stress test)")
        print("   [4] 3 ticks adverse (Worst-case stress test)")
        print("   [5] Custom ticks")
        slip_choice = input("   Select Slippage Mode [default: 1 (1 tick adverse)]: ").strip()
        slip_enabled = True
        slip_ticks = 1
        if slip_choice == "2":
            slip_enabled = False
            slip_ticks = 0
        elif slip_choice == "3":
            slip_enabled = True
            slip_ticks = 2
        elif slip_choice == "4":
            slip_enabled = True
            slip_ticks = 3
        elif slip_choice == "5":
            s_t = input("   Enter adverse slippage in ticks [default: 1]: ").strip()
            slip_ticks = int(s_t) if s_t.isdigit() else 1
            slip_enabled = slip_ticks > 0

        # 4. Fee Schedule
        print("\n4. Fee Schedule Configuration:")
        print("   [1] PropDAO Standard (0.015% Maker / 0.045% Taker -> 0.09% Round-trip) [Default / PropDAO]")
        print("   [2] Zero Fees (0.0% Maker / 0.0% Taker)")
        print("   [3] Live KCEX API (0% for TRUMP/DOGE zero-fee pairs)")
        print("   [4] Manual Custom Rates")
        fee_choice = input("   Select Fee Mode [default: 1 (PropDAO Standard)]: ").strip()
        if not fee_choice or fee_choice == "1":
            fee_mode = "MANUAL"
            maker_fee = 0.00015
            taker_fee = 0.00045
        elif fee_choice == "2":
            fee_mode = "ZERO"
            maker_fee = 0.0
            taker_fee = 0.0
        elif fee_choice == "3":
            fee_mode = "LIVE"
            maker_fee = None
            taker_fee = None
        elif fee_choice == "4":
            fee_mode = "MANUAL"
            m_input = input("   Enter Maker Fee % [default: 0.015]: ").strip()
            t_input = input("   Enter Taker Fee % [default: 0.045]: ").strip()
            maker_fee = (float(m_input) / 100.0) if m_input else 0.00015
            taker_fee = (float(t_input) / 100.0) if t_input else 0.00045
        else:
            fee_mode = "MANUAL"
            maker_fee = 0.00015
            taker_fee = 0.00045

        # 5. Simulation Data Fidelity
        has_trades = sym_cat.has_trades if sym_cat else False
        print("\n5. Simulation Data Fidelity:")
        print("   [1] OHLCV Only (Fast candle simulation with 1m sub-candle disambiguation & conservative SL) [Default]")
        if has_trades:
            print("   [2] High-Fidelity Ticker Trades (Millisecond tick-by-tick streaming) [Available]")
        fid_choice = input("   Select Fidelity Mode [default: 1 (OHLCV Only)]: ").strip()
        use_tick_data = (fid_choice == "2") and has_trades

        is_ml = "ML" in chosen_preset or preset_cfg.get("strategy_mode", "").upper() in ("ML", "ML_1M", "ML_MODEL", "ML_1M_MODEL")
        is_smc = "ORDER_BLOCK" in chosen_preset or "DEMAND" in chosen_preset or preset_cfg.get("strategy_mode", "").upper() in ("ORDER_BLOCK_DEMAND", "SMC")
        v_mult = float(preset_cfg.get("volume_multiplier", 1.0 if is_ml else (1.0 if "DOGE" in p_sym else 2.0)))
        p_def_lev = int(preset_cfg.get("leverage", 2))

        # 6. Leverage Multiplier
        print(f"\n6. Select Leverage Multiplier for {p_sym}:")
        print(f"   Preset default: {p_def_lev}x Isolated (PropDAO Max: 2.0x, Most Pairs: 1.0x)")
        lev_input = input(f"   Enter leverage multiplier (1 to 2) [default: {p_def_lev}]: ").strip()
        lev_val = int(lev_input) if (lev_input.isdigit() and int(lev_input) > 0) else p_def_lev
        if lev_val > 2:
            print("   ⚠️ PropDAO enforces max 2.0x crypto leverage. Clamping leverage to 2.0x.")
            lev_val = 2

        # 7. Trade Volume / Quantity Sizing
        p_def_mult = v_mult
        preset_vol_mode = preset_cfg.get("volume_mode", "MARGIN_PCT")
        def_margin_pct = float(preset_cfg.get("margin_pct", 10.0))
        print("\n7. Trade Volume / Quantity Sizing:")
        if preset_vol_mode == "MARGIN_PCT":
            print(f"   [1] Percentage of Available Margin ({def_margin_pct:.1f}% of wallet balance) [Default]")
            print(f"   [2] Preset Multiplier ({p_def_mult:g}x min)")
            print("   [3] Exact Number of Contracts (e.g. 2, 5, 10)")
            print("   [4] Minimum Volume (1x min contract)")
            print("   [5] Fixed Margin per Trade in USDT")
            p_vol_choice = input(f"   Select sizing mode [default: 1 ({def_margin_pct:.1f}% Margin)]: ").strip()
            if not p_vol_choice or p_vol_choice == "1":
                p_vol_mode = "MARGIN_PCT"
                p_margin_pct = def_margin_pct
                p_vol_multiplier = None
                p_vol_contracts = None
                p_fixed_margin = None
            elif p_vol_choice == "2":
                p_vol_mode = "MULTIPLIER"
                p_vol_multiplier = p_def_mult
                p_vol_contracts = None
                p_margin_pct = None
                p_fixed_margin = None
            elif p_vol_choice == "3":
                p_vol_mode = "CONTRACTS"
                c_input = input(f"   Enter number of contracts per trade [default: 1]: ").strip()
                p_vol_contracts = int(c_input) if c_input.isdigit() else 1
                p_vol_multiplier = None
                p_margin_pct = None
                p_fixed_margin = None
            elif p_vol_choice == "4":
                p_vol_mode = "MIN"
                p_vol_multiplier = 1.0
                p_vol_contracts = None
                p_margin_pct = None
                p_fixed_margin = None
            elif p_vol_choice == "5":
                p_vol_mode = "FIXED_MARGIN"
                fm_in = input("   Enter fixed margin per trade in USDT [default: 50.0]: ").strip()
                p_fixed_margin = float(fm_in) if fm_in else 50.0
                p_vol_multiplier = None
                p_vol_contracts = None
                p_margin_pct = None
        else:
            print(f"   [1] Preset Default Multiplier ({p_def_mult:g}x min) [Default]")
            print("   [2] Exact Number of Contracts (e.g. 2, 5, 10)")
            print("   [3] Minimum Volume (1x min contract)")
            print(f"   [4] Percentage of Available Margin (e.g. 10% of wallet balance)")
            print(f"   [5] Fixed Margin per Trade in USDT")
            p_vol_choice = input(f"   Select sizing mode [default: 1 (Multiplier {p_def_mult:g}x min)]: ").strip()
            p_vol_mode = "MULTIPLIER"
            p_vol_contracts = None
            p_vol_multiplier = p_def_mult
            p_margin_pct = None
            p_fixed_margin = None
            if p_vol_choice == "2":
                p_vol_mode = "CONTRACTS"
                c_input = input(f"   Enter number of contracts per trade [default: {int(p_def_mult)}]: ").strip()
                p_vol_contracts = int(c_input) if c_input.isdigit() else int(p_def_mult)
                p_vol_multiplier = None
            elif p_vol_choice == "3":
                p_vol_mode = "MIN"
                p_vol_multiplier = 1.0
                p_vol_contracts = None
            elif p_vol_choice == "4":
                p_vol_mode = "MARGIN_PCT"
                pct_in = input("   Enter % of available margin to risk [default: 10.0]: ").strip()
                try:
                    p_margin_pct = float(pct_in) if pct_in else 10.0
                except ValueError:
                    p_margin_pct = 10.0
                p_vol_multiplier = None
                p_vol_contracts = None
            elif p_vol_choice == "5":
                p_vol_mode = "FIXED_MARGIN"
                fm_in = input("   Enter fixed margin per trade in USDT [default: 5.0]: ").strip()
                try:
                    p_fixed_margin = float(fm_in) if fm_in else 5.0
                except ValueError:
                    p_fixed_margin = 5.0
                p_vol_multiplier = None
                p_vol_contracts = None

        # 8. Initial Account Capital
        def_capital = float(preset_cfg.get("initial_capital_usdt", 25000.0))
        print("\n8. Account Initial Capital:")
        cap_input = input(f"   Enter initial capital in USDT [default: {def_capital:,.0f}]: ").strip()
        try:
            account_capital = float(cap_input) if cap_input else def_capital
        except ValueError:
            account_capital = def_capital

        config = BacktestConfig(
            symbol=p_sym,
            timeframe=p_tf,
            strategy_mode=preset_cfg.get("strategy_mode", "ML_1M" if is_ml else ("ORDER_BLOCK_DEMAND" if is_smc else "STOCH_RSI")),
            stoch_preset=preset_cfg.get("stoch_preset", "FAST_SCALP"),
            start_time=start_val,
            end_time=end_val,
            volume_mode=p_vol_mode,
            volume_contracts=p_vol_contracts,
            volume_multiplier=p_vol_multiplier,
            margin_pct=p_margin_pct,
            fixed_margin_usdt=p_fixed_margin,
            tp_ticks=preset_cfg.get("tp_ticks", 0 if is_ml else (10 if is_smc else 5)),
            dynamic_tp=preset_cfg.get("dynamic_tp", True if (is_ml or is_smc) else False),
            sl_mode=preset_cfg.get("sl_mode", "TICKS"),
            sl_ticks=preset_cfg.get("sl_ticks", 0 if is_ml else (5 if is_smc else 2)),
            sl_roe_pct=preset_cfg.get("sl_roe_pct", 25.0),
            leverage=lev_val,
            initial_balance_usdt=account_capital,
            max_trades=0,
            use_tick_data=use_tick_data,
            fee_mode=fee_mode,
            maker_fee_override=maker_fee,
            taker_fee_override=taker_fee,
            invert_signal=preset_cfg.get("invert_signal", False),
            ratchet_enabled=preset_cfg.get("ratchet_enabled", False),
            ratchet_trigger_ticks=preset_cfg.get("ratchet_trigger_ticks", 1.0),
            ratchet_stall_seconds=preset_cfg.get("ratchet_stall_seconds", 10.0),
            ratchet_tighten_ticks=preset_cfg.get("ratchet_tighten_ticks", 1.0),
            ratchet_breakeven_ticks=preset_cfg.get("ratchet_breakeven_ticks", 2.5),
            execution_style=preset_cfg.get("execution_style", "PURE_MARKET"),
            maker_queue_timeout_seconds=preset_cfg.get("maker_queue_timeout_seconds", 10.0),
            resting_limit_tp=preset_cfg.get("resting_limit_tp", True),
            slippage_enabled=slip_enabled,
            slippage_ticks=slip_ticks,
            use_atr_targets=preset_cfg.get("use_atr_targets", False),
            atr_tp_multiplier=preset_cfg.get("atr_tp_multiplier", 2.0),
            atr_sl_multiplier=preset_cfg.get("atr_sl_multiplier", 1.0),
            volume_filter_enabled=preset_cfg.get("volume_filter_enabled", False),
            volume_filter_multiplier=preset_cfg.get("volume_filter_multiplier", 1.2),
            queue_dynamics_enabled=preset_cfg.get("queue_dynamics_enabled", False),
            simulate_intra_tick_liquidation=preset_cfg.get("simulate_intra_tick_liquidation", True),
            risk_reward_ratio=float(preset_cfg.get("risk_reward_ratio", 2.0)),
            pivot_len=int(preset_cfg.get("pivot_len", 5))
        )
        return config, target

    # =========================================================================
    # CUSTOM / MANUAL STEP-BY-STEP CONFIGURATION
    # =========================================================================
    # 1. Execution Target Selection
    print("\n1. Select Execution Target:")
    print("   [1] GitHub Actions Cloud -> Free cloud runner, no CPU lag, auto-downloads artifact ZIP [Default / PropDAO]")
    print("   [2] Locally              -> Run directly on this computer")
    target_choice = input("   Select target [default: 1 (GitHub Actions Cloud)]: ").strip()
    target = "local" if target_choice in ("2", "local", "l") else "github"

    # 2. Symbol Selection
    print("\n2. Select Trading Pair:")
    default_sym = "TRUMP_USDT"
    if available_symbols:
        for i, s in enumerate(available_symbols, 1):
            item = catalog[s]
            tf_count = len(item.ohlcv_timeframes)
            trades_note = "With Tick Trades" if item.has_trades else "OHLCV Only"
            is_def = " [Default]" if s == default_sym else ""
            print(f"   [{i}] {s:<12} ({tf_count} timeframes, {trades_note}){is_def}")
        print("   Or enter any other pair (e.g. DOGE_USDT, BTC_USDT, SOL_USDT)")
        def_idx = available_symbols.index(default_sym) + 1 if default_sym in available_symbols else 1
        sym_choice = input(f"   Select pair [default: {def_idx} ({available_symbols[def_idx-1]})]: ").strip()
        if sym_choice.isdigit() and 1 <= int(sym_choice) <= len(available_symbols):
            selected_symbol = available_symbols[int(sym_choice) - 1]
        elif sym_choice:
            selected_symbol = canonicalize_symbol(sym_choice)
        else:
            selected_symbol = available_symbols[def_idx-1]
    else:
        sym_choice = input(f"   Enter symbol [default: {default_sym}]: ").strip()
        selected_symbol = canonicalize_symbol(sym_choice or default_sym)

    sym_catalog = catalog.get(selected_symbol)

    # 3. Timeframe Selection
    if sym_catalog and sym_catalog.ohlcv_timeframes:
        available_tfs = sorted(sym_catalog.ohlcv_timeframes.keys())
    else:
        available_tfs = ["1m", "3m", "5m", "15m", "1h", "1d"]

    default_tf = "1m" if "1m" in available_tfs else available_tfs[0]
    print(f"\n3. Select Strategy Candle Timeframe for {selected_symbol}:")
    for i, tf in enumerate(available_tfs, 1):
        info = sym_catalog.ohlcv_timeframes[tf] if (sym_catalog and tf in sym_catalog.ohlcv_timeframes) else None
        d_range = f" | {format_ms_to_utc(info.start_time_ms)[:10]} to {format_ms_to_utc(info.end_time_ms)[:10]}" if info else ""
        print(f"   [{i}] {tf:<6}{d_range}")

    tf_choice = input(f"   Select timeframe [default: {default_tf}]: ").strip()
    if tf_choice.isdigit() and 1 <= int(tf_choice) <= len(available_tfs):
        selected_tf = available_tfs[int(tf_choice) - 1]
    elif tf_choice in available_tfs:
        selected_tf = tf_choice
    else:
        selected_tf = default_tf

    # 4. Strategy Selection
    print("\n4. Select Strategy:")
    print("   [1] Stochastic RSI       (Fast Scalp & Reversals in extreme zones) [Default / Recommended]")
    print("   [2] EMA Crossover        (Trend-following Golden/Death Crosses)")
    print("   [3] Smart Strategy       (Autonomous Regime-Adaptive: Momentum EMA + Mean-Reversion Stoch RSI)")
    print("   [4] Order Block + Demand (Smart Money Concepts: BOS body-close, OB wick-to-wick, FVG, 1:2 RR)")
    strat_choice = input("   Select Strategy [default: 1 (Stochastic RSI)]: ").strip()
    if strat_choice == "2":
        strategy_mode = "EMA_CROSSOVER"
    elif strat_choice == "3":
        strategy_mode = "SMART_STRATEGY"
    elif strat_choice == "4":
        strategy_mode = "ORDER_BLOCK_DEMAND"
    else:
        strategy_mode = "STOCH_RSI"

    # Strategy Parameters
    ema_preset = "5/13"
    stoch_preset = "FAST_SCALP"
    invert_signal = False
    risk_reward_ratio = 2.0
    pivot_len = 5

    if strategy_mode == "ORDER_BLOCK_DEMAND":
        print("\n   🏛️ Vivek Yadav SMC (Order Block + Demand) Parameters:")
        print("   • Dynamic Target: SL anchored to Zone Extrema & 1:2 Risk-Reward Take Profit")
        rr_in = input("   Risk-to-Reward Ratio (e.g. 2.0 for 1:2 RR) [default: 2.0]: ").strip()
        try:
            risk_reward_ratio = float(rr_in) if rr_in else 2.0
        except ValueError:
            risk_reward_ratio = 2.0
        pivot_in = input("   Pivot Lookback Window (candles for swing highs/lows) [default: 5]: ").strip()
        try:
            pivot_len = int(pivot_in) if pivot_in else 5
        except ValueError:
            pivot_len = 5
        invert_signal = False

    elif strategy_mode == "EMA_CROSSOVER":
        print("\n   EMA Length Preset:")
        print("   [1] 5 / 13 (Fast Scalp) [Default]")
        print("   [2] 9 / 21 (Standard Momentum)")
        print("   [3] 3 / 8  (Micro Burst)")
        ep_choice = input("   Select preset [default: 1 (5/13)]: ").strip()
        if ep_choice == "2":
            ema_preset = "9/21"
        elif ep_choice == "3":
            ema_preset = "3/8"
        invert_signal = False

    elif strategy_mode == "SMART_STRATEGY":
        print("\n   Smart Strategy (Multi-Regime Autonomous Scalping):")
        print("   • Dynamically routes between Momentum (EMA 5/13) and Range (Stoch RSI Fast Scalp)")
        print("   • Uses sub-ATR compression filter and volatility climax circuit breaker")
        invert_signal = False

    else:
        # STOCH_RSI
        print("\n   Stochastic RSI Preset:")
        print("   [1] FAST_SCALP   (9/9/3/3, OS: 20, OB: 80) [Default / Recommended for HFT]")
        print("   [2] STANDARD     (14/14/3/3, OS: 20, OB: 80)")
        print("   [3] MICRO_BURST  (7/7/3/3, OS: 15, OB: 85)")
        sp_choice = input("   Select preset [default: 1 (FAST_SCALP)]: ").strip()
        if sp_choice == "2":
            stoch_preset = "STANDARD"
        elif sp_choice == "3":
            stoch_preset = "MICRO_BURST"

        # Signal Direction Paradigm
        print("\n   Signal Direction Mode (Paradigm):")
        print("   [1] DIRECT MOMENTUM            -> Oversold Long / Overbought Short [Default]")
        print("   [2] INVERTED EXHAUSTION FADING  -> Overbought Long / Oversold Short (Phase V2.1/V2.2 Discovery)")
        inv_choice = input("   Select Signal Mode [default: 1 (DIRECT)]: ").strip()
        invert_signal = True if inv_choice in ("2", "inverted", "inv", "fading") else False

    # 5. Date Range Selection
    start_val = "2026-01-01"
    end_val = "2026-08-31"
    print("\n5. Select Historical Date Range:")
    date_options = []
    date_options.append(("PropDAO Standard 2026 Evaluation (Default: 2026-01-01 to 2026-08-31)", "2026-01-01", "2026-08-31"))
    if sym_catalog:
        ov_s, ov_e = sym_catalog.get_overlap_range(selected_tf)
        if ov_s and ov_e:
            date_options.append((f"High-Fidelity Overlap Range (with tick trades): {format_ms_to_utc(ov_s)[:10]} to {format_ms_to_utc(ov_e)[:10]}", format_ms_to_utc(ov_s)[:10], format_ms_to_utc(ov_e)[:10]))
        o_s, o_e = sym_catalog.get_timeframe_range(selected_tf)
        if o_s and o_e:
            date_options.append((f"Entire OHLCV Range: {format_ms_to_utc(o_s)[:10]} to {format_ms_to_utc(o_e)[:10]}", format_ms_to_utc(o_s)[:10], format_ms_to_utc(o_e)[:10]))

    for idx, (label, _, _) in enumerate(date_options, 1):
        print(f"   [{idx}] {label}")
    custom_idx = len(date_options) + 1
    print(f"   [{custom_idx}] Custom Date Range")

    range_choice = input(f"   Select Range [default: 1]: ").strip() or "1"
    if range_choice.isdigit() and 1 <= int(range_choice) <= len(date_options):
        _, start_val, end_val = date_options[int(range_choice) - 1]
    elif range_choice == str(custom_idx):
        start_val = input("   Enter Start Date (YYYY-MM-DD) [default: 2026-01-01]: ").strip() or "2026-01-01"
        end_val = input("   Enter End Date (YYYY-MM-DD) [default: 2026-08-31]: ").strip() or "2026-08-31"
    else:
        _, start_val, end_val = date_options[0]

    # 6. TP & SL Parameters
    if strategy_mode == "ORDER_BLOCK_DEMAND":
        print("\n6. Execution & Risk Parameters:")
        print("   ✅ Dynamic SMC Order Execution: SL placed at Zone Boundary, TP placed at 1:2 R:R")
        tp_ticks = 10
        sl_mode = "TICKS"
        sl_ticks = 5
        sl_roe = 25.0
        sl_price = None

        lev_input = input("   Leverage Multiplier (1 to 2, PropDAO Max: 2.0x, Alts: 1.0x) [default: 1]: ").strip()
        leverage = int(lev_input) if (lev_input.isdigit() and int(lev_input) > 0) else 1
        if leverage > 2:
            print("   ⚠️ PropDAO enforces max 2.0x crypto leverage. Clamping leverage to 2.0x.")
            leverage = 2

        cap_input = input("   Initial Wallet Capital (USDT) [default: 25,000]: ").strip()
        capital = float(cap_input) if cap_input else 25000.0

        max_input = input("   Max Trades limit (0 = run entire period) [default: 0]: ").strip()
        max_trades = int(max_input) if max_input.isdigit() else 0
    else:
        print("\n6. Execution & Risk Parameters:")
        tp_input = input("   Take Profit Ticks (pu away from entry) [default: 5 for DOGE, 2 for TRUMP]: ").strip()
        def_tp = 5 if "DOGE" in selected_symbol else 2
        tp_ticks = int(tp_input) if tp_input.isdigit() else def_tp

        sl_price = None
        sl_mode_input = input("   Stop Loss Mode ([1] ROE % on margin, [2] TICKS, [3] PRICE %) [default: 2 (TICKS)]: ").strip()
        if sl_mode_input == "1":
            sl_mode = "ROE"
            sl_roe_input = input("   Stop Loss ROE % on margin [default: 25.0%]: ").strip()
            sl_roe = float(sl_roe_input) if sl_roe_input else 25.0
            sl_ticks = None
        elif sl_mode_input == "3":
            sl_mode = "PRICE_PCT"
            sl_p_input = input("   Stop Loss Price % [default: 0.5]: ").strip()
            sl_price = float(sl_p_input) if sl_p_input else 0.5
            sl_ticks = None
            sl_roe = 25.0
        else:
            sl_mode = "TICKS"
            sl_t_input = input("   Stop Loss Ticks [default: 2 for DOGE, 10 for TRUMP]: ").strip()
            def_sl_t = 2 if "DOGE" in selected_symbol else 10
            sl_ticks = int(sl_t_input) if sl_t_input.isdigit() else def_sl_t
            sl_roe = 25.0

        lev_input = input("   Leverage Multiplier (1 to 2, PropDAO Max: 2.0x, Alts: 1.0x) [default: 1]: ").strip()
        leverage = int(lev_input) if (lev_input.isdigit() and int(lev_input) > 0) else 1
        if leverage > 2:
            print("   ⚠️ PropDAO enforces max 2.0x crypto leverage. Clamping leverage to 2.0x.")
            leverage = 2

        cap_input = input("   Initial Wallet Capital (USDT) [default: 25,000]: ").strip()
        capital = float(cap_input) if cap_input else 25000.0

        max_input = input("   Max Trades limit (0 = run entire period) [default: 0]: ").strip()
        max_trades = int(max_input) if max_input.isdigit() else 0

    # 7. Phase V2.2 Champion Micro-Excursion Tick Ratchet
    ratchet_trigger_ticks = 1.0
    ratchet_stall_seconds = 10.0
    ratchet_tighten_ticks = 1.0
    ratchet_breakeven_ticks = 2.5

    if strategy_mode == "ORDER_BLOCK_DEMAND":
        ratchet_enabled = False
    else:
        print("\n7. Phase V2.2 Champion Micro-Excursion Tick Ratchet:")
        print("   • Dynamic in-position trailing protection based on millisecond excursion (MFE)")
        print("   • Tier 1: When MFE >= +1.0t and position stalls >= 10s -> Tightens SL to -1.0t")
        print("   • Tier 2: When MFE >= +2.5t -> Locks SL to Breakeven (0.0t)")
        r_in = input("   Enable Tick Ratchet? [Y/n]: ").strip().lower()
        ratchet_enabled = r_in not in ("n", "no", "0")
        if ratchet_enabled:
            custom_r = input("   Customize Ratchet thresholds? [y/N, default: N]: ").strip().lower()
            if custom_r in ("y", "yes", "1"):
                try:
                    ratchet_trigger_ticks = float(input("      Tier 1 trigger MFE ticks [default: 1.0]: ").strip() or "1.0")
                    ratchet_stall_seconds = float(input("      Tier 1 stall seconds [default: 10.0]: ").strip() or "10.0")
                    ratchet_tighten_ticks = float(input("      Tier 1 tightened SL distance ticks [default: 1.0]: ").strip() or "1.0")
                    ratchet_breakeven_ticks = float(input("      Tier 2 Breakeven trigger ticks [default: 2.5]: ").strip() or "2.5")
                except ValueError:
                    pass

    # 8. Realistic Adverse Slippage Simulation
    print("\n8. Realistic Adverse Slippage Engine:")
    print("   Simulates realistic microsecond spread crossing & queue latency penalties.")
    slip_in = input("   Enable Adverse Slippage? [Y/n, default: Y]: ").strip().lower()
    slippage_enabled = slip_in not in ("n", "no", "0")
    slippage_ticks = 1 if slippage_enabled else 0
    if slippage_enabled:
        s_in = input("   Adverse slippage distance in ticks (e.g. 1, 2, 3) [default: 1]: ").strip()
        slippage_ticks = int(s_in) if s_in.isdigit() else 1

    # 9. Order Execution Style
    print("\n9. Order Execution Style:")
    print("   [1] MAKER_HYBRID -> Post-Only Maker limit at bid1/ask1 (10s timeout) + Resting Limit TP [Default]")
    print("   [2] PURE_MARKET  -> Legacy market order taker execution")
    exec_choice = input("   Select Execution Style [default: 1 (MAKER_HYBRID)]: ").strip()
    execution_style = "PURE_MARKET" if exec_choice in ("2", "pure_market", "market") else "MAKER_HYBRID"

    # 10. Trade Volume / Quantity Sizing (2x min for TRUMP, 1x min for DOGE)
    def_mult = 2.0 if "TRUMP" in selected_symbol else 1.0
    print("\n10. Trade Volume / Quantity Sizing:")
    print("   [1] Multiplier of Minimum Contract Volume (e.g. 2x min, 1x min) [Default]")
    print("   [2] Exact Number of Contracts (e.g. 2, 5, 10)")
    print("   [3] Minimum Volume (1x min contract)")
    print("   [4] Percentage of Available Margin (e.g. 10% of wallet balance, margin * leverage = position size)")
    print("   [5] Fixed Margin per Trade in USDT (e.g. 5 USDT margin, margin * leverage = position size)")
    vol_choice = input(f"   Select sizing mode [default: 1 (Multiplier {def_mult:g}x min)]: ").strip()
    vol_mode = "MULTIPLIER"
    vol_contracts = None
    vol_multiplier = def_mult
    margin_pct = None
    fixed_margin_usdt = None
    if vol_choice == "2":
        vol_mode = "CONTRACTS"
        c_input = input(f"   Enter number of contracts per trade [default: {int(def_mult)}]: ").strip()
        vol_contracts = int(c_input) if c_input.isdigit() else int(def_mult)
        vol_multiplier = None
    elif vol_choice == "3":
        vol_mode = "MIN"
        vol_multiplier = 1.0
        vol_contracts = None
    elif vol_choice == "4":
        vol_mode = "MARGIN_PCT"
        pct_in = input("   Enter % of available margin to risk (e.g. 10 for 10%) [default: 10.0]: ").strip()
        try:
            margin_pct = float(pct_in) if pct_in else 10.0
        except ValueError:
            margin_pct = 10.0
        vol_multiplier = None
        vol_contracts = None
    elif vol_choice == "5":
        vol_mode = "FIXED_MARGIN"
        fm_in = input("   Enter fixed margin per trade in USDT (e.g. 5.0) [default: 5.0]: ").strip()
        try:
            fixed_margin_usdt = float(fm_in) if fm_in else 5.0
        except ValueError:
            fixed_margin_usdt = 5.0
        vol_multiplier = None
        vol_contracts = None
    else:
        vol_mode = "MULTIPLIER"
        m_input = input(f"   Enter Volume Multiplier [default: {def_mult:g}x min]: ").strip()
        try:
            vol_multiplier = float(m_input) if m_input else def_mult
        except ValueError:
            vol_multiplier = def_mult
        vol_contracts = None

    # 11. Fee Schedule Configuration
    print("\n11. Fee Schedule Configuration:")
    print("   [1] PropDAO Standard (0.015% Maker / 0.045% Taker -> 0.09% Round-trip) [Default / PropDAO]")
    print("   [2] Zero Fees (0.0% Maker / 0.0% Taker)")
    print("   [3] Live KCEX API (0% for TRUMP/DOGE zero-fee pairs)")
    print("   [4] Manual Custom Rates")
    fee_choice = input("   Select Fee Mode [default: 1 (PropDAO Standard)]: ").strip()
    if not fee_choice or fee_choice == "1":
        fee_mode = "MANUAL"
        maker_fee = 0.00015
        taker_fee = 0.00045
    elif fee_choice == "2":
        fee_mode = "ZERO"
        maker_fee = 0.0
        taker_fee = 0.0
    elif fee_choice == "3":
        fee_mode = "LIVE"
        maker_fee = None
        taker_fee = None
    elif fee_choice == "4":
        fee_mode = "MANUAL"
        m_input = input("   Enter Maker Fee % [default: 0.015]: ").strip()
        t_input = input("   Enter Taker Fee % [default: 0.045]: ").strip()
        maker_fee = (float(m_input) / 100.0) if m_input else 0.00015
        taker_fee = (float(t_input) / 100.0) if t_input else 0.00045
    else:
        fee_mode = "MANUAL"
        maker_fee = 0.00015
        taker_fee = 0.00045

    # 12. Trade Optimization & Regime Filters (Optional)
    dur_enabled = False
    dur_deep_s = 60.0
    dur_max_s = 90.0
    dur_act = "CLOSE"
    adx_enabled = False
    adx_per = 14
    adx_thresh = 25.0
    htf_enabled = False
    htf_per = 200
    htf_tf = "15m"
    hourly_enabled = False
    hourly_bl = []
    dir_bias = "BOTH"

    if strategy_mode != "ORDER_BLOCK_DEMAND":
        print("\n12. Trade Optimization & Regime Filters:")
        print("   [1] Baseline / Disabled (Standard raw strategy execution) [Default]")
        print("   [2] Enable Duration Time-Stop (Monitor >60s, Auto-Exit at 90s)")
        print("   [3] Enable Full Institutional Safeguards (Duration + HTF 200 EMA + ADX + Hourly)")
        print("   [4] Custom Filter Configuration")
        filter_choice = input("   Select Filter Mode [default: 1 (Baseline / Disabled)]: ").strip()

        if filter_choice == "2":
            dur_enabled = True
        elif filter_choice == "3":
            dur_enabled = True
            adx_enabled = True
            htf_enabled = True
            hourly_enabled = True
            hourly_bl = [2, 3, 4, 5, 17]
        elif filter_choice == "4":
            d_in = input("   Enable Duration Filter? [y/N]: ").strip().lower()
            if d_in in ("y", "yes", "1"):
                dur_enabled = True
                hold_in = input("   Max hold seconds [default: 90]: ").strip()
                dur_max_s = float(hold_in) if hold_in else 90.0
                act_in = input("   Action on timeout (CLOSE / SCRATCH_OR_MARKET / TIGHTEN_SL) [default: CLOSE]: ").strip().upper()
                dur_act = act_in if act_in in ("CLOSE", "SCRATCH_OR_MARKET", "TIGHTEN_SL") else "CLOSE"
            h_in = input("   Enable HTF 200 EMA Trend Filter? [y/N]: ").strip().lower()
            if h_in in ("y", "yes", "1"):
                htf_enabled = True
            a_in = input("   Enable ADX Chop Filter? [y/N]: ").strip().lower()
            if a_in in ("y", "yes", "1"):
                adx_enabled = True
            hr_in = input("   Enable Hourly Session Blacklist? [y/N]: ").strip().lower()
            if hr_in in ("y", "yes", "1"):
                hourly_enabled = True
                bl_in = input("   Comma-separated UTC hours to block [default: 2,3,4,5,17]: ").strip()
                hourly_bl = [int(x.strip()) for x in bl_in.split(",") if x.strip().isdigit()] if bl_in else [2, 3, 4, 5, 17]
            bias_in = input("   Directional Bias (BOTH / LONG_ONLY / SHORT_ONLY) [default: BOTH]: ").strip().upper()
            if bias_in in ("LONG_ONLY", "SHORT_ONLY"):
                dir_bias = bias_in

    # 13. Simulation Data Fidelity
    print("\n13. Simulation Data Fidelity:")
    print("   [1] High-Fidelity Ticker Trades (Millisecond tick-by-tick streaming) [Default / Most Realistic]")
    print("   [2] OHLCV Only (Fast candle simulation with 1m sub-candle disambiguation & conservative SL)")
    fid_choice = input("   Select Fidelity Mode [default: 1 (Ticker Trades)]: ").strip()
    use_tick_data = False if fid_choice in ("2", "ohlcv", "no-ticks", "candles") else True

    config = BacktestConfig(
        symbol=selected_symbol,
        timeframe=selected_tf,
        strategy_mode=strategy_mode,
        ema_preset=ema_preset,
        stoch_preset=stoch_preset,
        start_time=start_val,
        end_time=end_val,
        volume_mode=vol_mode,
        volume_contracts=vol_contracts,
        volume_multiplier=vol_multiplier,
        margin_pct=margin_pct,
        fixed_margin_usdt=fixed_margin_usdt,
        tp_ticks=tp_ticks,
        sl_mode=sl_mode,
        sl_ticks=sl_ticks,
        sl_roe_pct=sl_roe,
        sl_price_pct=sl_price,
        leverage=leverage,
        initial_balance_usdt=capital,
        max_trades=max_trades,
        use_tick_data=use_tick_data,
        fee_mode=fee_mode,
        maker_fee_override=maker_fee,
        taker_fee_override=taker_fee,
        duration_filter_enabled=dur_enabled,
        duration_deep_monitor_seconds=dur_deep_s,
        duration_max_hold_seconds=dur_max_s,
        duration_action=dur_act,
        adx_filter_enabled=adx_enabled,
        adx_period=adx_per,
        adx_threshold=adx_thresh,
        htf_trend_filter_enabled=htf_enabled,
        htf_ema_period=htf_per,
        htf_timeframe=htf_tf,
        hourly_filter_enabled=hourly_enabled,
        hourly_blacklist_utc=hourly_bl,
        direction_bias=dir_bias,
        invert_signal=invert_signal,
        ratchet_enabled=ratchet_enabled,
        ratchet_trigger_ticks=ratchet_trigger_ticks,
        ratchet_stall_seconds=ratchet_stall_seconds,
        ratchet_tighten_ticks=ratchet_tighten_ticks,
        ratchet_breakeven_ticks=ratchet_breakeven_ticks,
        slippage_enabled=slippage_enabled,
        slippage_ticks=slippage_ticks,
        execution_style=execution_style,
        maker_queue_timeout_seconds=10.0,
        resting_limit_tp=True,
        smart_atr_filter_enabled=True,
        smart_min_atr_ticks=2.5,
        smart_chop_ceiling=58.0,
        smart_adx_trend_threshold=26.0,
        smart_use_ema200_filter=False,
        smart_climax_filter_enabled=True,
        smart_max_atr_expansion=2.2,
        smart_ema_preset=ema_preset,
        smart_stoch_preset=stoch_preset,
        smart_interval=selected_tf,
        risk_reward_ratio=risk_reward_ratio,
        pivot_len=pivot_len
    )
    return config, target


def main():
    parser = argparse.ArgumentParser(
        description="KCEX & Binance High-Fidelity Dual-Feed Strategy Backtester"
    )
    parser.add_argument("--scan", action="store_true", help="Scan and display historical data catalog and exit")
    parser.add_argument("--interactive", action="store_true", help="Launch interactive setup wizard")
    parser.add_argument("--target", type=str, default=None, choices=["local", "github"], help="Target execution engine: 'github' or 'local'")
    parser.add_argument("--github", action="store_true", help="Run backtest on GitHub Actions cloud runner and auto-download artifact ZIP")
    parser.add_argument("--github-token", type=str, default=None, help="GitHub Personal Access Token for workflow dispatch")
    parser.add_argument("--symbol", type=str, default=None, help="Trading pair symbol (e.g. TRUMP_USDT, DOGE_USDT)")
    parser.add_argument("--timeframe", type=str, default="1m", help="Strategy candle timeframe (e.g. 1m, 5m, 15m, 1h, 1d)")
    parser.add_argument("--strategy", type=str, default="STOCH_RSI", choices=["STOCH_RSI", "EMA_CROSSOVER", "SMART_STRATEGY", "SMART", "stoch_rsi", "ema_crossover", "smart_strategy", "smart", "ML_1M_MODEL", "ML_1M", "ML", "ml_1m_model", "ml_1m", "ml", "ORDER_BLOCK_DEMAND", "ORDER_BOOK_DEMAND", "ORDER_BLOCK", "order_block_demand", "order_book_demand", "order_block", "smc", "SMC", "TICK_CONSTRAINED_MM", "tick_constrained_mm", "TICK_CONSTRAINED", "tick_constrained", "MM", "mm"], help="Strategy to evaluate")
    parser.add_argument("--ohlcv-dir", type=str, default=None, help="Custom path to OHLCV candlestick data directory")
    parser.add_argument("--trades-dir", type=str, default=None, help="Custom path to tick trades data directory")
    parser.add_argument("--smart-atr-filter", dest="smart_atr_filter", action="store_true", default=None, help="Enable Smart Strategy ATR compression filter")
    parser.add_argument("--no-smart-atr-filter", dest="smart_atr_filter", action="store_false", help="Disable Smart Strategy ATR compression filter")
    parser.add_argument("--smart-min-atr-ticks", type=float, default=2.5, help="Minimum ATR in ticks for Smart Strategy entry (default: 2.5)")
    parser.add_argument("--smart-chop-ceiling", type=float, default=58.0, help="Choppiness Index ceiling for Smart Strategy (default: 58.0)")
    parser.add_argument("--smart-adx-trend-threshold", type=float, default=26.0, help="ADX momentum threshold for Smart Strategy (default: 26.0)")
    parser.add_argument("--smart-ema200-filter", action="store_true", default=False, help="Enable 200 EMA direction lock on Smart Strategy (default: False)")
    parser.add_argument("--smart-climax-filter", dest="smart_climax_filter", action="store_true", default=True, help="Enable volatility climax filter on Smart Strategy (default: True)")
    parser.add_argument("--no-smart-climax-filter", dest="smart_climax_filter", action="store_false", help="Disable volatility climax filter on Smart Strategy")
    parser.add_argument("--smart-max-atr-expansion", type=float, default=2.2, help="Max candle range expansion vs ATR for climax filter (default: 2.2)")
    parser.add_argument("--ema-preset", type=str, default="5/13", help="EMA preset (5/13, 9/21, 3/8)")
    parser.add_argument("--stoch-preset", type=str, default="FAST_SCALP", help="Stoch RSI preset")
    parser.add_argument("--start", type=str, default=None, help="Start date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--end", type=str, default=None, help="End date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--ticks", dest="use_tick_data", action="store_true", help="Enable high-fidelity millisecond tick trades simulation")
    parser.add_argument("--no-ticks", dest="use_tick_data", action="store_false", help="Run OHLCV candle simulation only (discrepancies resolved via 1m sub-candles or conservative SL)")
    parser.set_defaults(use_tick_data=False)
    parser.add_argument("--fee-mode", type=str, default="LIVE", choices=["LIVE", "ZERO", "MANUAL"], help="Fee mode: LIVE, ZERO, or MANUAL")
    parser.add_argument("--maker-fee", type=float, default=None, help="Maker fee rate or percent (e.g. 0.0 or 0.02)")
    parser.add_argument("--taker-fee", type=float, default=None, help="Taker fee rate or percent (e.g. 0.0 or 0.05)")
    parser.add_argument("--tp-ticks", type=int, default=2, help="Take profit ticks away from entry")
    parser.add_argument("--sl-mode", type=str, default="ROE", choices=["ROE", "TICKS", "PRICE_PCT"], help="Stop loss mode")
    parser.add_argument("--volume-mode", type=str, default=None, choices=["MULTIPLIER", "CONTRACTS", "MIN", "MARGIN_PCT", "FIXED_MARGIN", "margin_pct", "fixed_margin", "multiplier", "contracts", "min"], help="Volume sizing mode: MULTIPLIER, CONTRACTS, MIN, MARGIN_PCT, or FIXED_MARGIN")
    parser.add_argument("--volume-value", type=float, default=None, help="Numeric value for the selected volume mode (multiplier, contracts, margin %, or fixed margin USDT)")
    parser.add_argument("--contracts", "--volume-contracts", dest="volume_contracts", type=int, default=None, help="Number of contracts per trade (e.g. 2, 5, 10)")
    parser.add_argument("--volume-multiplier", type=float, default=None, help="Multiplier of minimum contract volume (e.g. 2.0 = 2x min, 1.0 = 1x min)")
    parser.add_argument("--margin-pct", type=float, default=None, help="Percentage of available margin to risk per trade (e.g. 10.0 for 10%%)")
    parser.add_argument("--fixed-margin", "--fixed-margin-usdt", dest="fixed_margin_usdt", type=float, default=None, help="Fixed margin per trade in USDT (e.g. 5.0 for 5 USDT margin)")
    parser.add_argument("--sl-ticks", type=int, default=10, help="Stop loss ticks away from entry")
    parser.add_argument("--sl-roe", type=float, default=25.0, help="Stop loss ROE percent (e.g. 25.0)")
    parser.add_argument("--sl-price", type=float, default=None, help="Stop loss price percentage away from entry (e.g. 0.5)")
    parser.add_argument("--leverage", type=int, default=2, help="Leverage multiplier (default: 2)")
    parser.add_argument("--capital", type=float, default=25000.0, help="Initial wallet balance in USDT (default: 25000.0)")
    parser.add_argument("--max-trades", type=int, default=0, help="Max trades to execute (0 = unlimited)")
    parser.add_argument("--speed", type=float, default=0.0, help="Simulated real-time playback speed (0 = max batch speed)")
    parser.add_argument("--slippage", type=int, default=0, help="Slippage in ticks")
    parser.add_argument("--enable-slippage", dest="slippage_enabled", action="store_true", default=None, help="Enable realistic adverse order execution slippage")
    parser.add_argument("--disable-slippage", dest="slippage_enabled", action="store_false", help="Disable adverse order execution slippage (0 slippage)")
    parser.add_argument("--slippage-ticks", dest="slippage_ticks", type=int, default=None, help="Integer ticks of adverse friction (e.g. 1, 2, 3)")
    # Phase V2.1 & V2.2 Quantitative Feature Flags
    parser.add_argument("--preset", type=str, default=None, help="Strategy preset name to load (e.g. TRUMP_ML_RAPID_SCALPER, DOGE_ML_MOMENTUM, TRUMP_ORDER_BLOCK_DEMAND, DOGE_ORDER_BLOCK_DEMAND)")
    parser.add_argument("--invert-signal", action="store_true", default=False, help="Invert Stoch RSI signal direction (Exhaustion Fading mode)")
    parser.add_argument("--ratchet", dest="ratchet_enabled", action="store_true", default=False, help="Enable Phase V2.2 Champion Micro-Excursion Tick Ratchet (+1.0t/10s -> -1t, +2.5t -> BE)")
    parser.add_argument("--ratchet-trigger-ticks", type=float, default=1.0, help="MFE in ticks required for Ratchet Tier 1 tightening (default: 1.0)")
    parser.add_argument("--ratchet-stall-seconds", type=float, default=10.0, help="Hold duration stall in seconds for Ratchet Tier 1 tightening (default: 10.0)")
    parser.add_argument("--ratchet-tighten-ticks", type=float, default=1.0, help="Tightened SL distance in ticks (default: 1.0)")
    parser.add_argument("--ratchet-breakeven-ticks", type=float, default=2.5, help="MFE in ticks required for Ratchet Tier 2 Breakeven lock (default: 2.5)")
    parser.add_argument("--execution-style", type=str, default="PURE_MARKET", choices=["PURE_MARKET", "MAKER_HYBRID"], help="Order execution style: PURE_MARKET or MAKER_HYBRID")
    parser.add_argument("--pivot-len", type=int, default=5, help="SMC Pivot Lookback Window (candles for swing highs/lows, default: 5)")
    parser.add_argument("--risk-reward-ratio", "--rr", type=float, default=2.0, help="SMC Risk-to-Reward Ratio (default: 2.0)")

    # Research V3 & V3.1 Quantitative Feature Flags (Purely Toggleable)
    parser.add_argument("--use-atr-targets", dest="use_atr_targets", action="store_true", default=None, help="Enable ATR-calibrated dynamic profit targets and stops (Target Dilution Law)")
    parser.add_argument("--no-use-atr-targets", dest="use_atr_targets", action="store_false", help="Disable ATR dynamic targets (use fixed tick targets)")
    parser.add_argument("--atr-tp-multiplier", type=float, default=2.0, help="Multiplier of 1m ATR for Take-Profit distance (default: 2.0)")
    parser.add_argument("--atr-sl-multiplier", type=float, default=1.0, help="Multiplier of 1m ATR for Stop-Loss distance (default: 1.0)")
    parser.add_argument("--volume-filter", dest="volume_filter_enabled", action="store_true", default=None, help="Enable volume shock filter requiring volume surge for entry")
    parser.add_argument("--no-volume-filter", dest="volume_filter_enabled", action="store_false", help="Disable volume shock filter")
    parser.add_argument("--volume-filter-multiplier", type=float, default=1.2, help="Minimum volume multiplier above rolling baseline (default: 1.2)")
    parser.add_argument("--queue-dynamics", dest="queue_dynamics_enabled", action="store_true", default=None, help="Enable Maker limit order queue dynamics and timeouts")
    parser.add_argument("--no-queue-dynamics", dest="queue_dynamics_enabled", action="store_false", help="Disable Maker limit order queue dynamics")
    parser.add_argument("--resting-limit-tp", dest="resting_limit_tp", action="store_true", default=None, help="Enable resting limit order TP (0 slippage on winning exits)")
    parser.add_argument("--no-resting-limit-tp", dest="resting_limit_tp", action="store_false", help="Disable resting limit order TP")
    parser.add_argument("--simulate-liquidation", dest="simulate_intra_tick_liquidation", action="store_true", default=True, help="Simulate 75x MMR liquidation boundary checks")
    parser.add_argument("--no-simulate-liquidation", dest="simulate_intra_tick_liquidation", action="store_false", help="Disable 75x liquidation checks")
    # Trade Optimization & Regime Filter Flags
    parser.add_argument("--duration-filter", action="store_true", default=False, help="Enable trade duration monitoring and time-decay exits")
    parser.add_argument("--duration-deep-monitor", type=float, default=60.0, help="Seconds before high-frequency duration monitoring engages (default: 60.0)")
    parser.add_argument("--duration-max-hold", type=float, default=90.0, help="Maximum allowed trade hold duration in seconds (default: 90.0)")
    parser.add_argument("--duration-action", type=str, default="CLOSE", choices=["CLOSE", "SCRATCH_OR_MARKET", "TIGHTEN_SL"], help="Action on timeout (default: CLOSE)")
    parser.add_argument("--adx-filter", action="store_true", default=False, help="Enable ADX chop suppression filter")
    parser.add_argument("--adx-period", type=int, default=14, help="ADX smoothing period (default: 14)")
    parser.add_argument("--adx-threshold", type=float, default=25.0, help="Minimum ADX required to allow entry (default: 25.0)")
    parser.add_argument("--htf-trend-filter", action="store_true", default=False, help="Enable Higher-Timeframe 200 EMA trend filter")
    parser.add_argument("--htf-ema-period", type=int, default=200, help="HTF EMA period (default: 200)")
    parser.add_argument("--htf-timeframe", type=str, default="15m", help="HTF candle interval (default: 15m)")
    parser.add_argument("--hourly-filter", action="store_true", default=False, help="Enable UTC hourly session blacklist")
    parser.add_argument("--hourly-blacklist", type=str, default="", help="Comma-separated UTC hours to block (e.g. 2,3,4,5,17)")
    parser.add_argument("--direction-bias", type=str, default="BOTH", choices=["BOTH", "LONG_ONLY", "SHORT_ONLY"], help="Directional bias: BOTH, LONG_ONLY, or SHORT_ONLY")
    parser.add_argument("--filters-json", type=str, default=None, help="JSON string containing Trade Optimization and Regime Filter configurations")
    parser.add_argument("--quant-params-json", type=str, default=None, help="JSON string containing Research V2/V2.2 quantitative parameters (inversion, ratchet, slippage, execution style)")

    args = parser.parse_args()
    print_banner()

    scanner = DataScanner()

    # If --scan flag requested
    if args.scan:
        scanner.print_summary_table()
        return

    # If run with no arguments or with --interactive flag
    if (args.symbol is None and args.preset is None) or args.interactive:
        config, target = run_interactive_wizard(scanner)
        # Override target if explicit flag provided on CLI
        if args.github or args.target == "github":
            target = "github"
        elif args.target == "local":
            target = "local"
    else:
        preset_cfg = settings.get_active_preset_config(args.preset) if args.preset else {}
        target = "github" if (args.github or args.target == "github") else "local"

        maker_fee = None
        if args.maker_fee is not None:
            maker_fee = args.maker_fee / 100.0 if args.maker_fee > 0.01 else args.maker_fee

        taker_fee = None
        if args.taker_fee is not None:
            taker_fee = args.taker_fee / 100.0 if args.taker_fee > 0.01 else args.taker_fee

        sym_raw = args.symbol or preset_cfg.get("symbol", "TRUMP_USDT")
        sym = canonicalize_symbol(sym_raw)
        vol_mode = (args.volume_mode.upper() if args.volume_mode else None) or preset_cfg.get("volume_mode", "MULTIPLIER")
        vol_contracts = args.volume_contracts or preset_cfg.get("volume_contracts")
        margin_pct = args.margin_pct if args.margin_pct is not None else preset_cfg.get("margin_pct")
        fixed_margin_usdt = args.fixed_margin_usdt if args.fixed_margin_usdt is not None else preset_cfg.get("fixed_margin_usdt")
        if args.margin_pct is not None and not args.volume_mode:
            vol_mode = "MARGIN_PCT"
        elif args.fixed_margin_usdt is not None and not args.volume_mode:
            vol_mode = "FIXED_MARGIN"
        elif args.volume_contracts is not None and not args.volume_mode:
            vol_mode = "CONTRACTS"

        if args.volume_value is not None:
            if vol_mode == "MARGIN_PCT":
                margin_pct = float(args.volume_value)
            elif vol_mode == "FIXED_MARGIN":
                fixed_margin_usdt = float(args.volume_value)
            elif vol_mode == "CONTRACTS":
                vol_contracts = int(round(args.volume_value))
            elif vol_mode == "MULTIPLIER":
                vol_mult = float(args.volume_value)

        if args.volume_multiplier is not None:
            vol_mult = args.volume_multiplier
        elif preset_cfg.get("volume_multiplier") is not None:
            vol_mult = preset_cfg.get("volume_multiplier")
        else:
            vol_mult = 2.0 if "TRUMP" in sym else 1.0

        dur_enabled = args.duration_filter
        dur_deep = args.duration_deep_monitor
        dur_max = args.duration_max_hold
        dur_act = args.duration_action
        adx_enabled = args.adx_filter
        adx_per = args.adx_period
        adx_thresh = args.adx_threshold
        htf_enabled = args.htf_trend_filter
        htf_ema = args.htf_ema_period
        htf_tf = args.htf_timeframe
        hourly_enabled = args.hourly_filter
        hourly_bl = [int(x.strip()) for x in args.hourly_blacklist.split(",") if x.strip().isdigit()] if args.hourly_blacklist else []
        dir_bias = args.direction_bias

        if args.filters_json:
            try:
                raw_fj = args.filters_json.strip() if isinstance(args.filters_json, str) else args.filters_json
                try:
                    fj = json.loads(raw_fj) if isinstance(raw_fj, str) else raw_fj
                except Exception:
                    import ast
                    fj = ast.literal_eval(raw_fj)
                if isinstance(fj, dict):
                    if "duration_filter" in fj: dur_enabled = bool(fj["duration_filter"])
                    if "duration_deep_monitor" in fj: dur_deep = float(fj["duration_deep_monitor"])
                    if "duration_max_hold" in fj: dur_max = float(fj["duration_max_hold"])
                    if "duration_action" in fj: dur_act = str(fj["duration_action"])
                    if "adx_filter" in fj: adx_enabled = bool(fj["adx_filter"])
                    if "adx_period" in fj: adx_per = int(fj["adx_period"])
                    if "adx_threshold" in fj: adx_thresh = float(fj["adx_threshold"])
                    if "htf_trend_filter" in fj: htf_enabled = bool(fj["htf_trend_filter"])
                    if "htf_ema_period" in fj: htf_ema = int(fj["htf_ema_period"])
                    if "htf_timeframe" in fj: htf_tf = str(fj["htf_timeframe"])
                    if "hourly_filter" in fj: hourly_enabled = bool(fj["hourly_filter"])
                    if "hourly_blacklist" in fj:
                        bl_val = fj["hourly_blacklist"]
                        if isinstance(bl_val, list):
                            hourly_bl = [int(x) for x in bl_val]
                        elif isinstance(bl_val, str) and bl_val.strip():
                            hourly_bl = [int(x.strip()) for x in bl_val.split(",") if x.strip().isdigit()]
                    if "direction_bias" in fj: dir_bias = str(fj["direction_bias"])
            except Exception as e:
                print(f"[!] Warning: Failed to parse --filters-json: {e}")

        if args.quant_params_json:
            try:
                raw_qj = args.quant_params_json.strip() if isinstance(args.quant_params_json, str) else args.quant_params_json
                try:
                    qj = json.loads(raw_qj) if isinstance(raw_qj, str) else raw_qj
                except Exception:
                    import ast
                    qj = ast.literal_eval(raw_qj)
                if isinstance(qj, dict):
                    if "invert_signal" in qj: args.invert_signal = bool(qj["invert_signal"])
                    if "enable_slippage" in qj: args.slippage_enabled = bool(qj["enable_slippage"])
                    if "slippage_ticks" in qj: args.slippage_ticks = int(qj["slippage_ticks"])
                    if "ratchet" in qj: args.ratchet_enabled = bool(qj["ratchet"])
                    if "ratchet_trigger_ticks" in qj: args.ratchet_trigger_ticks = float(qj["ratchet_trigger_ticks"])
                    if "ratchet_stall_seconds" in qj: args.ratchet_stall_seconds = float(qj["ratchet_stall_seconds"])
                    if "ratchet_tighten_ticks" in qj: args.ratchet_tighten_ticks = float(qj["ratchet_tighten_ticks"])
                    if "ratchet_breakeven_ticks" in qj: args.ratchet_breakeven_ticks = float(qj["ratchet_breakeven_ticks"])
                    if "execution_style" in qj: args.execution_style = str(qj["execution_style"])
                    if "use_atr_targets" in qj: args.use_atr_targets = bool(qj["use_atr_targets"])
                    if "atr_tp_multiplier" in qj: args.atr_tp_multiplier = float(qj["atr_tp_multiplier"])
                    if "atr_sl_multiplier" in qj: args.atr_sl_multiplier = float(qj["atr_sl_multiplier"])
                    if "volume_filter_enabled" in qj: args.volume_filter_enabled = bool(qj["volume_filter_enabled"])
                    if "volume_filter_multiplier" in qj: args.volume_filter_multiplier = float(qj["volume_filter_multiplier"])
                    if "queue_dynamics_enabled" in qj: args.queue_dynamics_enabled = bool(qj["queue_dynamics_enabled"])
                    if "resting_limit_tp" in qj: args.resting_limit_tp = bool(qj["resting_limit_tp"])
                    if "simulate_intra_tick_liquidation" in qj: args.simulate_intra_tick_liquidation = bool(qj["simulate_intra_tick_liquidation"])
                    if "margin_pct" in qj and qj["margin_pct"] is not None: margin_pct = float(qj["margin_pct"])
                    if "fixed_margin_usdt" in qj and qj["fixed_margin_usdt"] is not None: fixed_margin_usdt = float(qj["fixed_margin_usdt"])
                    if "fixed_margin" in qj and qj["fixed_margin"] is not None: fixed_margin_usdt = float(qj["fixed_margin"])
                    if "volume_contracts" in qj and qj["volume_contracts"] is not None: vol_contracts = int(qj["volume_contracts"])
                    if "volume_multiplier" in qj and qj["volume_multiplier"] is not None: vol_mult = float(qj["volume_multiplier"])
            except Exception as e:
                print(f"[!] Warning: Failed to parse --quant-params-json: {e}")

        strat_upper = args.strategy.upper()
        if strat_upper in ("SMART", "SMART_STRATEGY"):
            strat_mode = "SMART_STRATEGY"
        elif strat_upper in ("ORDER_BLOCK_DEMAND", "ORDER_BOOK_DEMAND", "ORDER_BLOCK", "DEMAND_BLOCK", "SMC"):
            strat_mode = "ORDER_BLOCK_DEMAND"
        else:
            strat_mode = strat_upper

        if "strategy_mode" in preset_cfg and args.strategy == "STOCH_RSI":
            strat_mode = preset_cfg["strategy_mode"]

        is_ml_strat = strat_mode in ("ML", "ML_1M", "ML_MODEL", "ML_1M_MODEL")
        is_smc_strat = strat_mode in ("ORDER_BLOCK_DEMAND", "ORDER_BOOK_DEMAND", "ORDER_BLOCK", "DEMAND_BLOCK", "SMC")

        # Resolve parameters from preset if specified, allowing CLI overrides
        tp_ticks = args.tp_ticks if (args.tp_ticks != 2 or "tp_ticks" not in preset_cfg) else preset_cfg["tp_ticks"]
        sl_mode = args.sl_mode if (args.sl_mode != "ROE" or "sl_mode" not in preset_cfg) else preset_cfg["sl_mode"]
        sl_ticks = args.sl_ticks if (args.sl_ticks != 10 or "sl_ticks" not in preset_cfg) else preset_cfg["sl_ticks"]
        sl_roe = args.sl_roe if (args.sl_roe != 25.0 or "sl_roe_pct" not in preset_cfg) else preset_cfg["sl_roe_pct"]

        if is_ml_strat or is_smc_strat:
            if not args.invert_signal and "invert_signal" not in preset_cfg:
                invert_sig = False
            else:
                invert_sig = args.invert_signal or preset_cfg.get("invert_signal", False)
            if not args.ratchet_enabled and "ratchet_enabled" not in preset_cfg:
                ratchet_en = False
            else:
                ratchet_en = args.ratchet_enabled or preset_cfg.get("ratchet_enabled", False)
            dynamic_tp = preset_cfg.get("dynamic_tp", True)
            if args.volume_multiplier is None and "volume_multiplier" not in preset_cfg:
                vol_mult = 2.0 if "TRUMP" in sym else 1.0
            lev_to_use = args.leverage if args.leverage != 75 else int(preset_cfg.get("leverage", 75 if is_smc_strat else 30))
        else:
            invert_sig = args.invert_signal or preset_cfg.get("invert_signal", False)
            ratchet_en = args.ratchet_enabled or preset_cfg.get("ratchet_enabled", False)
            dynamic_tp = preset_cfg.get("dynamic_tp", False)
            lev_to_use = args.leverage

        exec_style = args.execution_style if (args.execution_style != "PURE_MARKET" or "execution_style" not in preset_cfg) else preset_cfg["execution_style"]
        r_trig = args.ratchet_trigger_ticks if (args.ratchet_trigger_ticks != 1.0 or "ratchet_trigger_ticks" not in preset_cfg) else preset_cfg["ratchet_trigger_ticks"]
        r_stall = args.ratchet_stall_seconds if (args.ratchet_stall_seconds != 10.0 or "ratchet_stall_seconds" not in preset_cfg) else preset_cfg["ratchet_stall_seconds"]
        r_tight = args.ratchet_tighten_ticks if (args.ratchet_tighten_ticks != 1.0 or "ratchet_tighten_ticks" not in preset_cfg) else preset_cfg["ratchet_tighten_ticks"]
        r_be = args.ratchet_breakeven_ticks if (args.ratchet_breakeven_ticks != 2.5 or "ratchet_breakeven_ticks" not in preset_cfg) else preset_cfg["ratchet_breakeven_ticks"]

        # V3 & V3.1 parameters
        use_atr = args.use_atr_targets if args.use_atr_targets is not None else preset_cfg.get("use_atr_targets", False)
        atr_tp_mult = args.atr_tp_multiplier if (args.atr_tp_multiplier != 2.0 or "atr_tp_multiplier" not in preset_cfg) else preset_cfg["atr_tp_multiplier"]
        atr_sl_mult = args.atr_sl_multiplier if (args.atr_sl_multiplier != 1.0 or "atr_sl_multiplier" not in preset_cfg) else preset_cfg["atr_sl_multiplier"]
        vol_filt = args.volume_filter_enabled if args.volume_filter_enabled is not None else preset_cfg.get("volume_filter_enabled", False)
        vol_filt_mult = args.volume_filter_multiplier if (args.volume_filter_multiplier != 1.2 or "volume_filter_multiplier" not in preset_cfg) else preset_cfg["volume_filter_multiplier"]
        queue_dyn = args.queue_dynamics_enabled if args.queue_dynamics_enabled is not None else preset_cfg.get("queue_dynamics_enabled", False)
        resting_tp = args.resting_limit_tp if args.resting_limit_tp is not None else preset_cfg.get("resting_limit_tp", False)
        sim_liq = args.simulate_intra_tick_liquidation if args.simulate_intra_tick_liquidation is not None else preset_cfg.get("simulate_intra_tick_liquidation", True)

        slip_ticks = args.slippage_ticks if args.slippage_ticks is not None else (args.slippage if args.slippage else preset_cfg.get("slippage_ticks", 0))
        if args.slippage_enabled is not None:
            slip_enabled = args.slippage_enabled
        elif "slippage_enabled" in preset_cfg:
            slip_enabled = preset_cfg["slippage_enabled"]
        else:
            slip_enabled = (slip_ticks > 0)

        user_ohlcv_def = r"D:\My_Bots\Trading\BINANCE_DATA\OHLCV_Data\binance_futures_ohlcv"
        user_trades_def = r"D:\My_Bots\Trading\BINANCE_DATA\Tick_Trades_Data\binance_futures_trades"
        ohlcv_dir_val = args.ohlcv_dir or (user_ohlcv_def if os.path.exists(user_ohlcv_def) else os.path.join(BACKTESTER_DIR, "OHLCV_Data_Binance"))
        trades_dir_val = args.trades_dir or (user_trades_def if os.path.exists(user_trades_def) else os.path.join(BACKTESTER_DIR, "Historical_Trades_Data_Binance"))

        config = BacktestConfig(
            symbol=sym,
            timeframe=args.timeframe,
            strategy_mode=strat_mode,
            ohlcv_data_dir=ohlcv_dir_val,
            trades_data_dir=trades_dir_val,
            ema_preset=args.ema_preset,
            stoch_preset=args.stoch_preset,
            start_time=args.start,
            end_time=args.end,
            volume_mode=vol_mode,
            volume_contracts=vol_contracts,
            volume_multiplier=vol_mult,
            margin_pct=margin_pct,
            fixed_margin_usdt=fixed_margin_usdt,
            tp_ticks=tp_ticks,
            dynamic_tp=dynamic_tp,
            sl_mode=sl_mode,
            sl_ticks=sl_ticks,
            sl_roe_pct=sl_roe,
            sl_price_pct=args.sl_price,
            leverage=lev_to_use,
            initial_balance_usdt=args.capital,
            max_trades=args.max_trades,
            use_tick_data=args.use_tick_data,
            playback_speed=args.speed,
            slippage_enabled=slip_enabled,
            slippage_ticks=slip_ticks,
            invert_signal=invert_sig,
            ratchet_enabled=ratchet_en,
            ratchet_trigger_ticks=r_trig,
            ratchet_stall_seconds=r_stall,
            ratchet_tighten_ticks=r_tight,
            ratchet_breakeven_ticks=r_be,
            execution_style=exec_style,
            fee_mode=args.fee_mode,
            maker_fee_override=maker_fee,
            taker_fee_override=taker_fee,
            smart_atr_filter_enabled=args.smart_atr_filter if args.smart_atr_filter is not None else True,
            smart_min_atr_ticks=args.smart_min_atr_ticks,
            smart_chop_ceiling=args.smart_chop_ceiling,
            smart_adx_trend_threshold=args.smart_adx_trend_threshold,
            smart_use_ema200_filter=args.smart_ema200_filter,
            smart_climax_filter_enabled=args.smart_climax_filter,
            smart_max_atr_expansion=args.smart_max_atr_expansion,
            smart_ema_preset=args.ema_preset,
            smart_stoch_preset=args.stoch_preset,
            smart_interval=args.timeframe,
            duration_filter_enabled=dur_enabled,
            duration_deep_monitor_seconds=dur_deep,
            duration_max_hold_seconds=dur_max,
            duration_action=dur_act,
            adx_filter_enabled=adx_enabled,
            adx_period=adx_per,
            adx_threshold=adx_thresh,
            htf_trend_filter_enabled=htf_enabled,
            htf_ema_period=htf_ema,
            htf_timeframe=htf_tf,
            hourly_filter_enabled=hourly_enabled,
            hourly_blacklist_utc=hourly_bl,
            direction_bias=dir_bias,
            use_atr_targets=use_atr,
            atr_tp_multiplier=atr_tp_mult,
            atr_sl_multiplier=atr_sl_mult,
            volume_filter_enabled=vol_filt,
            volume_filter_multiplier=vol_filt_mult,
            queue_dynamics_enabled=queue_dyn,
            resting_limit_tp=resting_tp,
            simulate_intra_tick_liquidation=sim_liq,
            risk_reward_ratio=args.risk_reward_ratio if (args.risk_reward_ratio is not None and args.risk_reward_ratio != 2.0 or "risk_reward_ratio" not in preset_cfg) else float(preset_cfg.get("risk_reward_ratio", 2.0)),
            pivot_len=args.pivot_len if (args.pivot_len is not None and args.pivot_len != 5 or "pivot_len" not in preset_cfg) else int(preset_cfg.get("pivot_len", 5))
        )

    # Dispatch to appropriate execution target
    if target == "github":
        runner = GitHubBacktestRunner(token=args.github_token)
        reports_dir = os.path.join(ROOT_DIR, "BACKTESTER", "reports")
        success = runner.run_cloud_backtest(config=config, output_dir=reports_dir)
        if not success:
            sys.exit(1)
        return

    # LOCAL EXECUTION
    engine = BacktestExecutionEngine(config=config)
    contract = engine.contract
    m_pct = contract.maker_fee_rate * 100.0
    t_pct = contract.taker_fee_rate * 100.0
    if contract.maker_fee_rate == 0.0 and contract.taker_fee_rate == 0.0:
        fee_summary = f"Maker {m_pct:.2f}% / Taker {t_pct:.2f}% (ZERO FEES CONFIRMED)"
    else:
        fee_summary = f"Maker {m_pct:.3f}% / Taker {t_pct:.3f}% ({config.fee_mode})"

    if config.volume_mode == "MARGIN_PCT":
        vol_desc = f"{config.margin_pct:.1f}% Available Margin (Dynamic Sizing)"
    elif config.volume_mode == "FIXED_MARGIN":
        vol_desc = f"{config.fixed_margin_usdt:.2f} USDT Fixed Margin"
    elif config.volume_mode == "CONTRACTS":
        vol_desc = f"{config.volume_contracts} contracts"
    elif config.volume_mode == "MIN":
        vol_desc = "1x min volume"
    else:
        vol_desc = f"{config.volume_multiplier}x min volume"

    print("\n" + "=" * 78)
    print("                    STARTING LOCAL BACKTEST EXECUTION")
    print("=" * 78)
    print(f"Symbol:           {config.symbol}")
    print(f"Timeframe:        {config.timeframe}")
    print(f"Strategy:         {config.strategy_mode}")
    print(f"Date Range:       {config.start_time or 'Earliest'} -> {config.end_time or 'Latest'}")
    print(f"Trade Volume:     {vol_desc} ({config.volume_mode})")
    print(f"Simulation Mode:  {'HIGH-FIDELITY TICK TRADES (Streaming ms trades)' if config.use_tick_data else 'OHLCV ONLY (1m Sub-Candle Disambiguation & Conservative SL)'}")
    print(f"Fee Schedule:     {fee_summary}")
    print(f"Initial Capital:  {config.initial_balance_usdt:.2f} USDT")
    print(f"Leverage:         {config.leverage}x")
    print(f"Take Profit:      +{config.tp_ticks} ticks")
    if config.sl_mode == "TICKS":
        sl_summary_str = f"{config.sl_ticks} ticks"
    elif config.sl_mode == "PRICE_PCT":
        sl_summary_str = f"-{config.sl_price_pct}% price"
    else:
        sl_summary_str = f"-{config.sl_roe_pct}% ROE"
    print(f"Stop Loss:        {sl_summary_str}")
    slip_desc = f"ENABLED ({config.slippage_ticks} ticks adverse)" if (getattr(config, "slippage_enabled", False) and config.slippage_ticks > 0) else "DISABLED (0 ticks)"
    print(f"Slippage Engine:  {slip_desc}")
    ratchet_desc = f"ENABLED (T1: +{config.ratchet_trigger_ticks:g}t/{config.ratchet_stall_seconds:.0f}s -> -{config.ratchet_tighten_ticks:g}t, T2: +{config.ratchet_breakeven_ticks:g}t -> BE)" if getattr(config, "ratchet_enabled", False) else "DISABLED"
    print(f"Tick Ratchet:     {ratchet_desc}")
    signal_desc = "INVERTED (Exhaustion Fading)" if getattr(config, "invert_signal", False) else "DIRECT (Momentum)"
    print(f"Signal Paradigm:  {signal_desc}")
    print(f"Execution Style:  {getattr(config, 'execution_style', 'PURE_MARKET')}")
    dur_desc = f"ENABLED (Monitor >{config.duration_deep_monitor_seconds}s, Action: {config.duration_action} at {config.duration_max_hold_seconds}s)" if config.duration_filter_enabled else "DISABLED"
    print(f"Duration Filter:  {dur_desc}")
    regime_parts = []
    if config.adx_filter_enabled:
        regime_parts.append(f"ADX({config.adx_period})>={config.adx_threshold}")
    if config.htf_trend_filter_enabled:
        regime_parts.append(f"HTF {config.htf_ema_period} EMA ({config.htf_timeframe})")
    if config.hourly_filter_enabled:
        regime_parts.append(f"Hourly Block [{','.join(str(x) for x in config.hourly_blacklist_utc)}]")
    if config.direction_bias != "BOTH":
        regime_parts.append(f"Bias: {config.direction_bias}")
    print(f"Regime Filters:   {', '.join(regime_parts) if regime_parts else 'DISABLED (Baseline)'}")
    print("=" * 78 + "\n")

    t_start = time.time()

    print("[*] Processing historical data...")
    outcomes = engine.run()
    elapsed = time.time() - t_start

    print(f"[*] Simulation completed in {elapsed:.2f} seconds ({len(outcomes)} trades executed).")

    # Metrics & Reporting
    summary = PerformanceCalculator.calculate(
        outcomes=outcomes,
        initial_balance_usdt=config.initial_balance_usdt,
        inr_rate=config.inr_rate
    )

    reporter = BacktestReporter()
    reporter.print_summary(summary)
    reporter.print_trades_table(outcomes, limit=15)
    reporter.export_all(outcomes=outcomes, summary=summary, config=config, contract=contract)


if __name__ == "__main__":
    main()
