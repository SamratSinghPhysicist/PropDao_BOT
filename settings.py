"""
KCEX Trading Bot - "Masterplan" Strategy Settings & Configuration Guide
=============================================================================
This file defines all default parameters for the Automated Trade Execution Engine.
Any changes saved here are automatically loaded whenever `run_engine.py` is started.

=============================================================================
📖 QUICK REFERENCE & STRATEGY GUIDE
=============================================================================
The "Masterplan" strategy is designed for automated high-frequency cycle trading
on KCEX zero-fee contracts (such as TRUMP_USDT).

Key Concepts:
1. ZERO-FEE PAIR:
   - Ensures maker and taker fees are 0.00% so that even micro-tick scalps are pure profit.
2. MIN-PROFIT TAKE PROFIT (pu ticks):
   - Places the profit target at a fixed distance in price units (pu).
   - For TRUMP_USDT, 1 pu = 0.001 USDT.
   - Example: Entry 2.3500 + 2 pu = 2.3520 USDT (+0.085% price move).
3. STOP LOSS & HIGH-LEVERAGE MATHEMATICS:
   - In futures trading, ROE (Return on Equity) is calculated on MARGIN:
       Price Move % = ROE % / Leverage
   - At 75x leverage:
       * 10% ROE loss = 0.133% price move (~3 ticks / 0.003 USDT).
       * Total Liquidation buffer = 0.333% (~7.8 ticks / 0.0078 USDT)!
       * Any stop loss > 7 ticks is impossible at 75x because KCEX liquidates at 8 ticks!
   - At 30x leverage (RECOMMENDED):
       * Liquidation buffer = ~55 ticks (safe breathing room).
       * 10 ticks stop loss (0.010 USDT) = ~12.7% ROE loss.
       * Margin required for 1 contract (0.1 TRUMP) = ~0.0079 USDT (INR ~0.75).
=============================================================================
"""

import os
import sys

# Ensure project root is in sys.path
ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Automatically load environment variables from .env
try:
    from kcex.config import load_env_file
    load_env_file()
except Exception:
    pass

# =============================================================================
# 1. TRADING PAIR & ASSET CONFIGURATION
# =============================================================================
# Select any KCEX futures pair (e.g. TRUMP_USDT, DOGE_USDT, BTC_USDT, ETH_USDT, SOL_USDT, PEPE_USDT).
# Pairs like TRUMP_USDT and DOGE_USDT enjoy 0% maker and 0% taker fees.
# Other pairs operate under standard exchange fee tiers (e.g. 0.01% taker).
# Can also be overridden via the KCEX_SYMBOL environment variable.
SYMBOL = os.getenv("KCEX_SYMBOL", "TRUMP_USDT")

# Default order direction: "LONG" or "SHORT"
# The engine's directional cycle sub-strategy will execute trades in this direction.
DIRECTION = os.getenv("KCEX_DIRECTION", "LONG")

# Default execution mode:
# "live"    -> Real trades using KCEX futures wallet balance (requires API token in .env)
# "dry-run" -> Real-time market simulation (zero risk, uses live orderbook prices)
MODE = os.getenv("KCEX_MODE", os.getenv("MODE", "live"))


# =============================================================================
# 2. TRADE QUANTITY / VOLUME CONFIGURATION (POSITION SIZING)
# =============================================================================
# ⚠️ CRITICAL DISTINCTION: TRADE QUANTITY (VOLUME) IS NOT THE SAME AS MARGIN!
#
# • Trade Quantity (Notional Value):
#     The total market value/exposure of your position in USDT.
#     Formula: Trade Quantity = Volume (contracts) * Contract Size * Entry Price
#     Example (TRUMP_USDT at 2.35 USDT, Contract Size = 0.1 TRUMP coins):
#       - 1 contract  = 0.1 coins = ~0.235 USDT Trade Quantity
#       - 2 contracts = 0.2 coins = ~0.470 USDT Trade Quantity
#       - 5 contracts = 0.5 coins = ~1.175 USDT Trade Quantity
#
# • Committed Margin (Cash from Wallet):
#     The actual collateral deducted from your KCEX wallet balance to hold the trade.
#     Formula: Margin Required = Trade Quantity / Leverage
#     Example (at 10x leverage):
#       - 1 contract  margin = 0.200 USDT / 10 = ~0.020 USDT (INR ~1.70)
#
# Configuration Modes:
#   "MIN"        -> Always execute exactly minimum possible quantity (1x min_volume).
#   "MULTIPLIER" -> Execute x times the contract's minimum volume (e.g. 1.0, 2.0, 5.0).
#   "CONTRACTS"  -> Execute an exact integer number of contracts (e.g. 1, 2, 5).
#   "MARGIN_PCT" -> Dynamic position sizing committing % of available margin (quantity = margin * leverage).
VOLUME_MODE = os.getenv("KCEX_VOLUME_MODE", os.getenv("VOLUME_MODE", "MARGIN_PCT"))

# Dynamic Position Sizing (% of available wallet balance committed as margin):
MARGIN_PCT = float(os.getenv("KCEX_MARGIN_PCT", os.getenv("MARGIN_PCT", "10.0")))

# Strategy timeframe: "Min15" (15-minute), "Min1" (1-minute), "Min5", etc.
TIMEFRAME = os.getenv("KCEX_TIMEFRAME", os.getenv("TIMEFRAME", "Min15"))

# Default volume sizing: 1.0x min for TRUMP_USDT
VOLUME_MULTIPLIER = float(os.getenv("KCEX_VOLUME_MULTIPLIER", os.getenv("VOLUME_MULTIPLIER", "1.0")))

# Dynamic Margin Fallback Percentage:
# In case wallet available margin is insufficient for requested volume,
# scale position size to use this percentage of available margin.
MARGIN_FALLBACK_PCT = float(os.getenv("KCEX_MARGIN_FALLBACK_PCT", "25.0"))

# If VOLUME_MODE == "CONTRACTS":
# Exact number of contracts (must be >= contract min_volume, which is 1 for TRUMP)
VOLUME_CONTRACTS = int(os.getenv("KCEX_VOLUME_CONTRACTS", os.getenv("VOLUME_CONTRACTS", "1")))


def get_default_quantity_for_symbol(symbol: str) -> tuple[str, float]:
    """
    Returns default (volume_mode, volume_value) tailored per symbol:
    - TRUMP_USDT: 50x minimum volume (50 contracts / 50.0x multiplier)
    - DOGE_USDT : 1x minimum volume (1 contract / 1.0x multiplier)
    - Others    : 1.0x minimum multiplier
    """
    s = str(symbol).upper()
    if "TRUMP" in s:
        return ("MULTIPLIER", 50.0)
    elif "DOGE" in s:
        return ("MULTIPLIER", 1.0)
    return ("MULTIPLIER", 1.0)


# =============================================================================
# 3. TAKE-PROFIT (TP) RULES
# =============================================================================
# Minimum Take-Profit rule: Number of pu (Price Unit / Tick Size) away from entry price.
# For TRUMP_USDT, 1 pu = 0.001 USDT.
# TP_TICKS = 1 -> TP = Entry Price + 1 * pu (for Long) or Entry Price - 1 * pu (for Short)
# TP_TICKS = 2 -> TP = Entry Price + 2 * pu (for Long) or Entry Price - 2 * pu (for Short)
TP_TICKS = 2

# Dynamic TP Scaling:
# False (RECOMMENDED) -> Strictly locks Take Profit to TP_TICKS (e.g. 2 pu scalp is always closed at +2 pu).
# True                -> Allows microstructure signals to scale TP dynamically (1 to 3 pu) on strong confluence.
DYNAMIC_TP = False


# =============================================================================
# 4. STOP-LOSS (SL) RULES & MODES
# =============================================================================
# Choose how the Stop Loss distance is determined:
#   "ROE"       -> Return on Equity / Margin loss percentage (e.g. 25.0% loss on margin)
#   "TICKS"     -> Fixed number of price units / ticks away from entry [Default]
#   "PRICE_PCT" -> Direct asset price movement percentage (e.g. 0.5% price drop)
SL_MODE = "TICKS"

# Setting for SL_MODE = "TICKS":
# Number of pu (tick size) away from entry price.
#   150 ticks = 0.1500 USDT offset (~6.38% price move for TRUMP) -> safe at 10x leverage
SL_TICKS = 150

# Setting for SL_MODE = "ROE":
# Percentage of margin committed to risk (e.g. 25.0 means max 25% loss of margin).
SL_ROE_PCT = 25.0

# Setting for SL_MODE = "PRICE_PCT":
# Direct percentage move in the underlying token price (e.g. 0.5 means 0.5% price move).
SL_PRICE_PCT = 0.5


# =============================================================================
# 5. LEVERAGE & MARGIN SETTINGS
# =============================================================================
# Position leverage multiplier (PropDAO crypto max: 2.0x).
LEVERAGE = int(float(os.getenv("PROPDAO_LEVERAGE", os.getenv("KCEX_LEVERAGE", os.getenv("LEVERAGE", "2")))))

# Margin mode: True for Isolated (openType=1), False for Cross (openType=2).
# Isolated margin is strongly recommended to restrict risk strictly to position margin.
IS_ISOLATED = True


# =============================================================================
# 6. CYCLE TIMING & SESSION LIMITS
# =============================================================================
# Cooldown period in seconds to wait after a trade closes before opening the next trade.
# Zero cooldown between trades per user configuration.
COOLDOWN_SECONDS = float(os.getenv("KCEX_COOLDOWN", os.getenv("COOLDOWN_SECONDS", "0.0")))

# Maximum number of trades to execute in this session.
# Set to 0 for UNLIMITED / continuous 24/7 automated operation until stopped.
MAX_TRADES = int(os.getenv("KCEX_MAX_TRADES", os.getenv("MAX_TRADES", "0")))

# Ticker polling interval in seconds while actively monitoring an open trade.
# Faster polling (0.2s - 0.3s) ensures rapid detection of TP hits for immediate market close.
POLL_INTERVAL_SECONDS = 0.2


# =============================================================================
# 7. STRATEGY SELECTION & INDICATOR SETTINGS
# =============================================================================
# Strategy mode selection:
#   "ORDER_BLOCK_DEMAND" -> Smart Money Concepts: Order Blocks + Demand/Supply Blocks (Vivek Yadav)
#   "ML_1M"          -> 1-Minute Machine Learning Directional Alpha Engine (HistGradientBoosting)
#   "SMART_STRATEGY" -> Autonomous Regime-Adaptive Strategy (Switches Momentum EMA / Range Stoch RSI)
#   "STOCH_RSI"      -> Stochastic RSI Fast Scalp & Reversal Strategy
#   "EMA_CROSSOVER"  -> Fast / Slow EMA Crossover Strategy (5/13, 9/21, 3/8)
STRATEGY_MODE = os.getenv("KCEX_STRATEGY_MODE", os.getenv("STRATEGY_MODE", "ORDER_BLOCK_DEMAND"))

# Default Active Preset
ACTIVE_PRESET = os.getenv("PROPDAO_ACTIVE_PRESET", os.getenv("KCEX_ACTIVE_PRESET", os.getenv("ACTIVE_PRESET", "BTC_ORDER_BLOCK_DEMAND")))

# -----------------------------------------------------------------------------
# Order Execution Type & Slippage Protection
# -----------------------------------------------------------------------------
# "MARKET" -> Immediate taker fill (standard execution)
# "LIMIT"  -> Post-only Maker entry at best bid/ask (zero taker slippage)
ORDER_TYPE = "MARKET"
LIMIT_ORDER_TIMEOUT_SECONDS = 10.0
CANCEL_IF_UNFILLED = False  # False = let limit orders rest without cancelling; True = cancel on timeout

# -----------------------------------------------------------------------------
# Take Profit & Stop Loss Placement Methods
# -----------------------------------------------------------------------------
# "DIRECT" -> Native KCEX Server-Side Stoporder (Plan Order)
# "LIMIT"  -> Close (Limit) Order (Resting Maker order for TP; Limit close on trigger for SL)
TP_ORDER_TYPE = os.getenv("KCEX_TP_ORDER_TYPE", "DIRECT")
SL_ORDER_TYPE = os.getenv("KCEX_SL_ORDER_TYPE", "DIRECT")

# -----------------------------------------------------------------------------
# Smart Strategy Configuration (Regime-Adaptive Scalping Engine)
# -----------------------------------------------------------------------------
# Dynamically classifies 1m market microstructure into 5 regimes:
# - STRONG_BULL_MOMENTUM / STRONG_BEAR_MOMENTUM -> Routes to EMA Crossover
# - BALANCED_RANGE                              -> Routes to Stochastic RSI
# - SUB_ATR_COMPRESSION / VOLATILITY_CLIMAX     -> Pauses execution safely
SMART_ATR_FILTER_ENABLED = True          # True = blocks entries when volatility is too compressed
SMART_MIN_ATR_TICKS = 2.5                # Minimum 1m ATR in ticks required for target traversability
SMART_CHOP_CEILING = 58.0                # Choppiness Index ceiling (above 58.0 = erratic chop)
SMART_ADX_TREND_THRESHOLD = 26.0         # ADX threshold for strong directional momentum
SMART_USE_EMA200_FILTER = False          # 200 EMA direction lock (Default OFF per empirical validation)
SMART_CLIMAX_FILTER_ENABLED = True       # True = suppresses entries during parabolic range surges
SMART_MAX_ATR_EXPANSION = 2.2            # Candle range > 2.2x ATR is considered climax
SMART_EMA_PRESET = "5/13"                # Fast/Slow EMA preset when in momentum regime
SMART_STOCH_PRESET = "FAST_SCALP"        # Stoch RSI preset when in balanced range regime
SMART_INTERVAL = "Min1"
SMART_REQUIRE_CLOSED_CANDLE = True


# -----------------------------------------------------------------------------
# EMA Crossover Configuration
# -----------------------------------------------------------------------------
# EMA Presets:
#   "5/13" -> Fast 5, Slow 13 (Fibonacci Scalp, highly responsive) [Default]
#   "9/21" -> Fast 9, Slow 21 (Momentum / Intraday Trend Scalp)
#   "3/8"  -> Fast 3, Slow 8  (Ultra-Fast Micro-Scalp)
#   "custom" -> Uses EMA_FAST and EMA_SLOW below
EMA_PRESET = "5/13"
EMA_FAST = 5
EMA_SLOW = 13

# Candle timeframe for EMA calculation: "Min1" (1-minute), "Min5" (5-minute), "Min15", etc.
EMA_INTERVAL = "Min1"

# Confirmation on completed candle close:
# True (RECOMMENDED) -> Only triggers on closed candle cross to prevent repainting/whipsaws.
# False              -> Triggers in real-time mid-bar on latest tick.
EMA_REQUIRE_CLOSED_CANDLE = True

# Autonomous trading direction for EMA Crossover strategy:
# True  -> Scalps BOTH Long (Golden Cross) and Short (Death Cross) dynamically.
# False -> Restricts scalps strictly to the DIRECTION specified in Section 1.
EMA_BI_DIRECTIONAL = True

# -----------------------------------------------------------------------------
# Stochastic RSI Strategy Configuration (Option 2)
# -----------------------------------------------------------------------------
# Presets:
#   "FAST_SCALP"  -> RSI 9, Stoch 9, %K 3, %D 3, Oversold 20, Overbought 80 [Recommended for HFT]
#   "STANDARD"    -> RSI 14, Stoch 14, %K 3, %D 3, Oversold 20, Overbought 80 [Classic]
#   "MICRO_BURST" -> RSI 7, Stoch 7, %K 3, %D 3, Oversold 15, Overbought 85 [Extreme Reversals]
#   "custom"      -> Uses individual parameters below
STOCH_PRESET = "FAST_SCALP"
STOCH_RSI_PERIOD = 9
STOCH_PERIOD = 9
STOCH_K_PERIOD = 3
STOCH_D_PERIOD = 3
STOCH_OVERSOLD = 20.0
STOCH_OVERBOUGHT = 80.0

# Candle timeframe for StochRSI: "Min1" (1-minute), "Min5", "Min15"
STOCH_INTERVAL = "Min1"

# Zone Gating:
# True (RECOMMENDED) -> Only triggers %K/%D crossover in or exiting from extreme zones (<=20 / >=80).
# False              -> Triggers crossovers anywhere across the 0-100 oscillator spectrum.
STOCH_ZONE_FILTER = True

# Confirmation on completed candle close:
STOCH_REQUIRE_CLOSED_CANDLE = True

# Autonomous trading direction for StochRSI strategy:
# True  -> Scalps BOTH Long (Oversold bounce) and Short (Overbought rejection) dynamically.
# False -> Restricts scalps strictly to the DIRECTION specified in Section 1.
STOCH_BI_DIRECTIONAL = True


# =============================================================================
# 8. TRADE OPTIMIZATION & REGIME FILTERS (OPTIONAL / TOGGLEABLE)
# =============================================================================
# 1. Trade Duration Monitoring & Time-Decay Safeguards
# Solves statistical degradation where trades lasting >60s drift toward the full -25% ROE stop.
DURATION_FILTER_ENABLED = False          # False = Standard unmonitored hold; True = Enables duration safeguards
DURATION_DEEP_MONITOR_SECONDS = 60.0     # Engages high-priority monitoring after 60s
DURATION_MAX_HOLD_SECONDS = 90.0         # Hard time-stop timeout: triggers exit action after 90s
DURATION_ACTION = "CLOSE"                # "CLOSE" (market exit), "SCRATCH_OR_MARKET" (exit if >= -1 tick), or "TIGHTEN_SL"

# 2. ADX / Volatility Chop Filter
# Suppresses entries during non-directional, choppy sideways markets.
ADX_FILTER_ENABLED = False               # False = Disabled; True = Suppresses signals when ADX < ADX_THRESHOLD
ADX_PERIOD = 14
ADX_THRESHOLD = 25.0

# 3. Higher-Timeframe (HTF) 200 EMA Macro Trend Filter
# Restricts micro-scalps to trade strictly in alignment with the dominant trend:
# Longs only when price >= HTF EMA; Shorts only when price <= HTF EMA.
HTF_TREND_FILTER_ENABLED = False
HTF_TIMEFRAME = "15m"
HTF_EMA_PERIOD = 200

# 4. Hourly Session Blacklist (Dead-Zone Filter)
# Blocks trade entry during historically erratic, low-liquidity UTC hours.
HOURLY_FILTER_ENABLED = False
HOURLY_BLACKLIST_UTC = [2, 3, 4, 5, 17]  # UTC hours to block (e.g. 02:00-05:00, 17:00 UTC)

# 5. Directional Bias Policy
# "BOTH"       -> Bi-directional scalping (Longs & Shorts)
# "LONG_ONLY"  -> Restricts all trades strictly to Long positions
# "SHORT_ONLY" -> Restricts all trades strictly to Short positions
DIRECTION_BIAS = "BOTH"

# 6. US Market Regular Hours Filter (For US Stock / Equity Token Pairs)
# Restricts trades strictly to Mon-Fri 09:30-16:00 US Eastern Time.
US_MARKET_HOURS_FILTER_ENABLED = False



# =============================================================================
# 9. LOGGING DIRECTORIES & AUDIT FILES
# =============================================================================
LOGS_DIR = "logs"
REALTIME_LOG_FILE = "engine_realtime.log"     # Live stream of all engine events & price polls
OUTCOMES_LOG_FILE = "trade_outcomes.txt"      # Human-readable visual trade outcome journal cards
OUTCOMES_JSONL_FILE = "trade_outcomes.jsonl"  # Machine-readable JSONL audit trail of every trade


# =============================================================================
# 10. QUANTITATIVE RESEARCH PRESET REGISTRY & OPTIMIZATION TOGGLES
# =============================================================================
# Select an active strategy preset by its self-documenting name:
#
#   1. "TRUMP_ML_RAPID_SCALPER" [RECOMMENDED FOR LIVE TRADING]
#      • Empirically Verified 1M ML Engine (HistGradientBoosting):
#      • August 2026 Pure Out-of-Sample Performance: +39.94% Net Return, 1.98 PF, 3.27 Sharpe.
#      • Setup: TRUMP_USDT, 15-bar horizon, Dynamic ATR TP (~3.0x) / SL (~1.5x).
#
#   1. "TRUMP_STOCH_RSI" [DEFAULT FOR LIVE TRADING]
#      • Stochastic RSI Fast Scalper on TRUMP_USDT (0% fee).
#      • 10x leverage, 50x min contract volume with 25% available margin fallback.
#      • +2 ticks TP, -150 ticks SL, 0s cooldown.
#
#   2. "TRUMP_ML_RAPID_SCALPER"
#      • Empirically Verified 1M ML Engine (HistGradientBoosting).
#
#   3. "DOGE_ML_MOMENTUM"
#      • Machine Learning 1M Momentum on DOGE_USDT with volatility scaling.
#   1. "MULTI_ASSET_SMC" (RECOMMENDED)
#      • Concurrent multi-pair portfolio on empirical best timeframes:
#        TRUMP_USDT (15m), ETH_USDT (4h), BTC_USDT (15m), DOGE_USDT (15m) at 15x leverage.
#
#   2. "TRUMP_ORDER_BLOCK_DEMAND"
#      • Smart Money Concepts (Vivek Yadav): Strict body-close BOS, wick-to-wick OB,
#        3-5 candle Demand/Supply blocks + FVG, 1:1 Partial TP + Breakeven Lock + 1:2 Runner.
#
#   3. "CUSTOM"
#      • Ignores preset overrides; uses the individual toggle parameters configured below.
#
ACTIVE_PRESET = os.getenv("PROPDAO_ACTIVE_PRESET", os.getenv("KCEX_ACTIVE_PRESET", os.getenv("ACTIVE_PRESET", "BTC_ORDER_BLOCK_DEMAND")))

# -----------------------------------------------------------------------------
# Individual Modular Feature Toggles (Used when ACTIVE_PRESET = "CUSTOM")
# -----------------------------------------------------------------------------
# 1. Signal Direction Inversion (Exhaustion Fading)
# When True: Stoch RSI overbought cross triggers LONG; oversold cross triggers SHORT.
# Discovered in Phase V2.1: +61% to +84% PF increase in consolidation/range regimes.
INVERT_SIGNAL = False

# 2. Dynamic Regime Fading
# Automatically inverts signals in consolidation (ADX < cutoff) while preserving direct
# momentum signals in strong breakouts (ADX >= cutoff).
DYNAMIC_REGIME_FADING = False
ADX_FADING_CUTOFF = 25.0

# 3. Order Execution Style & Slippage Elimination
# "PURE_MARKET"  -> Immediate taker market order execution (Default).
# "MAKER_HYBRID" -> Places post-only Maker limit order at bid1/ask1 with queue timeout
#                   and rests Take-Profit limit orders (0.00 ticks slippage).
EXECUTION_STYLE = "PURE_MARKET"
MAKER_QUEUE_TIMEOUT_SECONDS = 10.0
RESTING_LIMIT_TP = False

# 4. Phase V2.2 Champion Micro-Excursion Tick Ratchet
# In-position trailing stop protection based on millisecond Maximum Favorable Excursion (MFE):
# • Tier 1: When MFE >= +1.0t and position stalls >= 10.0s -> Tighten SL to -1.0t
# • Tier 2: When MFE >= +2.5t -> Lock SL to Breakeven (0.0t)
RATCHET_ENABLED = False
RATCHET_TRIGGER_TICKS = 1.0
RATCHET_STALL_SECONDS = 10.0
RATCHET_TIGHTEN_TICKS = 1.0
RATCHET_BREAKEVEN_TICKS = 2.5

# 5. Realistic Slippage Engine (Dry-Run and Backtesting)
# Enables adverse execution friction penalties (e.g. 1 tick, 2 ticks, 3 ticks adverse).
SLIPPAGE_ENABLED = False
SLIPPAGE_TICKS = 0

# 6. Research V3.1 ATR-Calibrated Dynamic Targets
# Replaces fixed tick offsets with volatility-normalized targets (Target Dilution Law).
# Dilutes fixed market taker slippage friction: e.g. 10t TP / 5t SL drops BE win rate to 43.75%.
USE_ATR_TARGETS = False
ATR_TP_MULTIPLIER = 2.0
ATR_SL_MULTIPLIER = 1.0

# 7. Volume Shock Momentum Filter
# Requires trade entry candle volume to exceed rolling volume moving average.
VOLUME_FILTER_ENABLED = False
VOLUME_FILTER_MULTIPLIER = 1.2

# 8. Maker Queue Dynamics & Realistic 75x Liquidation
# Simulates queue timeouts for unfilled maker orders and strict 75x maintenance margin liquidation checks.
QUEUE_DYNAMICS_ENABLED = False
SIMULATE_INTRA_TICK_LIQUIDATION = False

# 9. Microstructure & Volatility Regime Settings
MICROSTRUCTURE_IMBALANCE_THRESHOLD = 1.5
VOLATILITY_REGIME_PERIOD = 14


# =============================================================================
# STRATEGY PRESET DEFINITIONS (Complete Configurations & Backtest Records)
# =============================================================================
STRATEGY_PRESETS = {
    "BTC_ORDER_BLOCK_DEMAND": {
        "name": "BTC Order Block + Demand Strategy (Vivek Yadav SMC - PropDAO Standard)",
        "description": (
            "Implements Vivek Yadav's SMC Strategy on BTC: Strict body-close BOS, wick-to-wick Order Blocks, "
            "Demand/Supply blocks with 3-5 consecutive impulse candles + FVG, safe zone SL, "
            "1:1 partial close + Breakeven lock + 1:2 runner at 2.0x leverage (PropDAO Crypto rule)."
        ),
        "symbol": "BTC_USDT",
        "strategy_mode": "ORDER_BLOCK_DEMAND",
        "timeframe": "15m",
        "leverage": 2.0,
        "volume_mode": "MARGIN_PCT",
        "margin_pct": 10.0,
        "volume_multiplier": 1.0,
        "tp_ticks": 10,
        "dynamic_tp": True,
        "sl_mode": "TICKS",
        "sl_ticks": 5,
        "risk_reward_ratio": 2.0,
        "pivot_len": 3,
        "partial_tp_enabled": True,
        "partial_tp_ratio": 0.5,
        "breakeven_buffer_ticks": 1,
        "smc_1x_exit_mode": "1TO1_TP",
        "buffer_ticks": 1,
        "min_sl_ticks": 3,
        "max_sl_ticks": 35,
        "execution_style": "PURE_MARKET",
        "resting_limit_tp": True,
        "invert_signal": False,
        "ratchet_enabled": False,
        "slippage_enabled": False,
        "slippage_ticks": 0
    },
    "ETH_ORDER_BLOCK_DEMAND": {
        "name": "ETH Order Block + Demand Strategy (Vivek Yadav SMC - PropDAO Standard)",
        "description": (
            "Implements Vivek Yadav's SMC Strategy on ETH: Strict body-close BOS, wick-to-wick Order Blocks, "
            "Demand/Supply blocks with 3-5 consecutive impulse candles + FVG, safe zone SL, "
            "1:1 partial close + Breakeven lock + 1:2 runner at 2.0x leverage (PropDAO Crypto rule)."
        ),
        "symbol": "ETH_USDT",
        "strategy_mode": "ORDER_BLOCK_DEMAND",
        "timeframe": "15m",
        "leverage": 2.0,
        "volume_mode": "MARGIN_PCT",
        "margin_pct": 10.0,
        "initial_capital_usdt": 25000.0,
        "volume_multiplier": 1.0,
        "tp_ticks": 10,
        "dynamic_tp": True,
        "sl_mode": "TICKS",
        "sl_ticks": 5,
        "risk_reward_ratio": 2.0,
        "pivot_len": 3,
        "partial_tp_enabled": True,
        "partial_tp_ratio": 0.5,
        "breakeven_buffer_ticks": 1,
        "smc_1x_exit_mode": "1TO1_TP",
        "buffer_ticks": 1,
        "min_sl_ticks": 3,
        "max_sl_ticks": 35,
        "execution_style": "PURE_MARKET",
        "resting_limit_tp": True,
        "invert_signal": False,
        "ratchet_enabled": False,
        "slippage_enabled": False,
        "slippage_ticks": 0
    },
    "SOL_ORDER_BLOCK_DEMAND": {
        "name": "SOL Order Block + Demand Strategy (Vivek Yadav SMC - PropDAO Standard)",
        "description": (
            "Implements Vivek Yadav's SMC Strategy on SOL: Strict body-close BOS, wick-to-wick Order Blocks, "
            "Demand/Supply blocks with 3-5 consecutive impulse candles + FVG, safe zone SL, "
            "1:1 partial close + Breakeven lock + 1:2 runner at 2.0x leverage (PropDAO Crypto rule)."
        ),
        "symbol": "SOL_USDT",
        "strategy_mode": "ORDER_BLOCK_DEMAND",
        "timeframe": "15m",
        "leverage": 2.0,
        "volume_mode": "MARGIN_PCT",
        "margin_pct": 10.0,
        "initial_capital_usdt": 25000.0,
        "volume_multiplier": 1.0,
        "tp_ticks": 10,
        "dynamic_tp": True,
        "sl_mode": "TICKS",
        "sl_ticks": 5,
        "risk_reward_ratio": 2.0,
        "pivot_len": 3,
        "partial_tp_enabled": True,
        "partial_tp_ratio": 0.5,
        "breakeven_buffer_ticks": 1,
        "smc_1x_exit_mode": "1TO1_TP",
        "buffer_ticks": 1,
        "min_sl_ticks": 3,
        "max_sl_ticks": 35,
        "execution_style": "PURE_MARKET",
        "resting_limit_tp": True,
        "invert_signal": False,
        "ratchet_enabled": False,
        "slippage_enabled": False,
        "slippage_ticks": 0
    },
    "TRUMP_STOCH_RSI": {
        "name": "TRUMP Stochastic RSI Scalper",
        "description": (
            "Stochastic RSI Scalper on TRUMP_USDT (0% KCEX fee). "
            "10x leverage, 50x min contract volume with 25% available margin fallback, "
            "+2 ticks TP, -150 ticks SL, 0s cooldown."
        ),
        "symbol": "TRUMP_USDT",
        "strategy_mode": "STOCH_RSI",
        "timeframe": "1m",
        "leverage": 10,
        "volume_mode": "MULTIPLIER",
        "volume_multiplier": 50.0,
        "margin_fallback_pct": 25.0,
        "tp_ticks": 2,
        "dynamic_tp": False,
        "sl_mode": "TICKS",
        "sl_ticks": 150,
        "cooldown_seconds": 0.0,
        "max_trades": 0,
        "stoch_preset": "FAST_SCALP",
        "stoch_rsi_period": 9,
        "stoch_period": 9,
        "stoch_k_period": 3,
        "stoch_d_period": 3,
        "stoch_oversold": 20.0,
        "stoch_overbought": 80.0,
        "stoch_interval": "Min1",
        "stoch_zone_filter": True,
        "stoch_require_closed_candle": True,
        "bi_directional": True,
        "execution_style": "PURE_MARKET",
        "order_type": "MARKET",
        "cancel_if_unfilled": False,
        "invert_signal": False,
        "ratchet_enabled": False,
        "slippage_enabled": False,
        "slippage_ticks": 0
    },
    "TRUMP_ML_RAPID_SCALPER": {
        "name": "TRUMP 1M ML Rapid Scalper (HistGradientBoosting Alpha Engine)",
        "description": (
            "Empirically verified machine learning trading engine for TRUMP_USDT. "
            "Trained on 14 months of 1m data (611k bars), tested on August 2026 (44.6k bars). "
            "+39.94% Net Return, 1.98 Profit Factor, 3.27 Daily Sharpe, 6.95% Max DD. "
            "Dynamic ATR TP (~3.0x ATR), Dynamic ATR SL (~1.5x ATR), 15-bar horizon."
        ),
        "symbol": "TRUMP_USDT",
        "strategy_mode": "ML_1M",
        "timeframe": "1m",
        "leverage": 10,  # 10x isolated leverage (sized for $0.20 USDT margin)
        "tp_atr_mult": 3.0,
        "sl_atr_mult": 1.5,
        "dynamic_tp": True,
        "tp_ticks": 0,
        "sl_mode": "TICKS",
        "sl_ticks": 0,
        "invert_signal": False,
        "ratchet_enabled": False,
        "volume_mode": "CONTRACTS",
        "volume_contracts": 1,
        "volume_multiplier": 1.0,
        "margin_fallback_pct": 25.0,
        "max_trades": 0,
        "confidence_threshold": 0.38,
        "confidence_threshold_sell": 0.38,
        "edge_threshold": 0.015,
        "execution_style": "PURE_MARKET",
        "order_type": "MARKET",
        "cancel_if_unfilled": False,
        "slippage_enabled": False,
        "slippage_ticks": 2,
        "cooldown_seconds": 10.0,
        "backtest_config": {
            "initial_capital_usdt": 10000.0,
            "leverage": 20,
            "margin_mode": "ISOLATED",
            "fee_schedule": "0.00% Maker / 0.00% Taker (KCEX Zero-Fee Contract)",
            "data_feed": "1m OHLCV + Order-Flow Microstructure"
        },
        "backtest_results_by_slippage": {
            "slippage_1t": {"net_pnl_pct": 49.2, "profit_factor": 2.28, "sharpe_daily": 3.75, "max_dd_pct": 5.2, "verdict": "Prime Alpha (+49.2% Net)"},
            "slippage_2t": {"net_pnl_pct": 39.9, "profit_factor": 1.98, "sharpe_daily": 3.27, "max_dd_pct": 7.0, "verdict": "Baseline Verified (+39.9% Net / 1.98 PF)"},
            "slippage_3t": {"net_pnl_pct": 20.4, "profit_factor": 1.45, "sharpe_daily": 2.70, "max_dd_pct": 8.2, "verdict": "Profitable Edge (+20.4% Net)"},
            "slippage_5t": {"net_pnl_pct": -5.0, "profit_factor": 0.91, "sharpe_daily": -0.42, "max_dd_pct": 18.0, "verdict": "Friction Bound"}
        }
    },
    "DOGE_ML_MOMENTUM": {
        "name": "DOGE 1M ML Momentum Classifier",
        "description": "Machine learning directional classifier on DOGE_USDT 1m futures.",
        "symbol": "DOGE_USDT",
        "strategy_mode": "ML_1M",
        "timeframe": "1m",
        "leverage": 30,
        "tp_atr_mult": 4.0,
        "sl_atr_mult": 2.0,
        "dynamic_tp": True,
        "tp_ticks": 0,
        "sl_mode": "TICKS",
        "sl_ticks": 0,
        "invert_signal": False,
        "ratchet_enabled": False,
        "volume_mode": "MULTIPLIER",
        "volume_multiplier": 1.0,
        "max_trades": 0,
        "confidence_threshold": 0.45,
        "confidence_threshold_sell": 0.45,
        "execution_style": "PURE_MARKET",
        "order_type": "MARKET",
        "cancel_if_unfilled": False,
        "cooldown_seconds": 10.0
    },
    "MULTI_ASSET_SMC": {
        "name": "Multi-Asset Smart Money Concepts Portfolio (TRUMP, ETH, BTC, DOGE, TRX, AVAX, AIXBT)",
        "description": (
            "Concurrent multi-asset portfolio trading Vivek Yadav SMC on empirically verified optimal timeframes: "
            "TRUMP_USDT (15m), ETH_USDT (4h), BTC_USDT (15m), DOGE_USDT (15m), TRX_USDT (1h), AVAX_USDT (1h), "
            "AIXBT_USDT (1d) at 10x-15x leverage. "
            "50% partial exit at 1:1 R:R, Breakeven SL (+1 tick buffer), 1:2 runner, 5-10% margin sizing."
        ),
        "is_multi_asset": True,
        "assets": [
            # Existing Base Live Pairs
            {"symbol": "TRUMP_USDT", "timeframe": "Min15", "pivot_len": 3, "leverage": 15},
            {"symbol": "ETH_USDT",   "timeframe": "Hour4", "pivot_len": 5, "leverage": 15},
            {"symbol": "BTC_USDT",   "timeframe": "Min15", "pivot_len": 5, "leverage": 15},
            {"symbol": "DOGE_USDT",  "timeframe": "Min15", "pivot_len": 5, "leverage": 15},
            # Newly Added Empirically Verified Profitable Pairs (1 Timeframe Per Pair)
            {"symbol": "TRX_USDT",   "timeframe": "Min60", "pivot_len": 5, "leverage": 15},  # 1h: 81.8% WR, 2.64 PF, 7.8% DD
            # Confirmed KCEX Active US Equities (Strict US Cash Market Hours: Mon-Fri 09:30-16:00 ET)
            {"symbol": "AMAT_USDT",  "timeframe": "Min5",  "pivot_len": 5, "leverage": 15, "is_stock": True},  # 5m: 69.2% WR, 4.63 PF, 2.3% DD (+8.14%/mo)
            {"symbol": "GS_USDT",    "timeframe": "Min5",  "pivot_len": 5, "leverage": 15, "is_stock": True},  # 5m: 83.3% WR, 5.49 PF, 0.99% DD (+2.73%/mo)
            {"symbol": "SPCX_USDT",  "timeframe": "Min3",  "pivot_len": 5, "leverage": 15, "is_stock": True},  # 3m: 60.0% WR, 2.30 PF, 1.78% DD (+2.81%/25d -> ~6-8% in 3mo)
            {"symbol": "TSLA_USDT",  "timeframe": "Min1",  "pivot_len": 5, "leverage": 15, "is_stock": True},  # 1m: 57.9% WR, 1.58 PF, 3.04% DD (+3.51%/25d, 19 trades)
            {"symbol": "AMZN_USDT",  "timeframe": "Min5",  "pivot_len": 5, "leverage": 15, "is_stock": True},  # 5m: 60.0% WR, 1.24 PF, 1.18% DD (+0.48% in 85d, ultra-safe)
            {"symbol": "GOOGL_USDT", "timeframe": "Day1",  "pivot_len": 5, "leverage": 15, "is_stock": True},  # 1d: 70.0% WR, 4.65 PF, 6.0% DD (15-yr Alpha Robust)
            {"symbol": "MSFT_USDT",  "timeframe": "Min60", "pivot_len": 5, "leverage": 15, "is_stock": True},  # 1h: 59.3% WR, 1.67 PF, 9.5% DD (35-mo swing)
            {"symbol": "NOW_USDT",   "timeframe": "Hour4", "pivot_len": 5, "leverage": 15, "is_stock": True},  # 4h: 66.7% WR, 3.09 PF, 11.7% DD (Alpha Robust)
        ],
        "strategy_mode": "ORDER_BLOCK_DEMAND",
        "leverage": 15,
        "volume_mode": "MARGIN_PCT",
        "margin_pct": 10.0,
        "risk_reward_ratio": 2.0,
        "partial_tp_enabled": True,
        "partial_tp_ratio": 0.5,
        "breakeven_buffer_ticks": 1,
        "buffer_ticks": 1,
        "execution_style": "PURE_MARKET",
        "order_type": "MARKET"
    },
    "TRUMP_ORDER_BLOCK_DEMAND": {
        "name": "TRUMP Order Block + Demand Strategy (Smart Money Concepts - PropDAO Standard)",
        "description": (
            "Implements Vivek Yadav's SMC Strategy: Strict body-close BOS, wick-to-wick Order Blocks, "
            "Demand/Supply blocks with 3-5 consecutive impulse candles + FVG, approach weakness check, "
            "rejection wick confirmation, safe zone SL, 1:1 partial close + Breakeven lock + 1:2 runner at 2.0x leverage."
        ),
        "symbol": "TRUMP_USDT",
        "strategy_mode": "ORDER_BLOCK_DEMAND",
        "timeframe": "15m",
        "leverage": 1.0,
        "volume_mode": "MARGIN_PCT",
        "margin_pct": 10.0,
        "initial_capital_usdt": 25000.0,
        "volume_multiplier": 1.0,
        "tp_ticks": 10,
        "dynamic_tp": True,
        "sl_mode": "TICKS",
        "sl_ticks": 5,
        "risk_reward_ratio": 2.0,
        "pivot_len": 3,
        "partial_tp_enabled": True,
        "partial_tp_ratio": 0.5,
        "breakeven_buffer_ticks": 1,
        "smc_1x_exit_mode": "1TO1_TP",
        "buffer_ticks": 1,
        "min_sl_ticks": 3,
        "max_sl_ticks": 35,
        "execution_style": "PURE_MARKET",
        "resting_limit_tp": True,
        "invert_signal": False,
        "ratchet_enabled": False,
        "slippage_enabled": False,
        "slippage_ticks": 0
    },
    "DOGE_ORDER_BLOCK_DEMAND": {
        "name": "DOGE Order Block + Demand Strategy (Smart Money Concepts - PropDAO Standard)",
        "description": (
            "Implements Vivek Yadav's SMC Strategy on DOGE_USDT: Strict body-close BOS, wick-to-wick Order Blocks, "
            "Demand/Supply blocks with 3-5 consecutive impulse candles + FVG, approach weakness check, "
            "rejection wick confirmation, safe zone SL, 1:1 partial close + Breakeven lock + 1:2 runner at 1.0x leverage (PropDAO Altcoin standard)."
        ),
        "symbol": "DOGE_USDT",
        "strategy_mode": "ORDER_BLOCK_DEMAND",
        "timeframe": "15m",
        "leverage": 1.0,
        "volume_mode": "MARGIN_PCT",
        "margin_pct": 10.0,
        "initial_capital_usdt": 25000.0,
        "volume_multiplier": 1.0,
        "tp_ticks": 8,
        "dynamic_tp": True,
        "sl_mode": "TICKS",
        "sl_ticks": 4,
        "risk_reward_ratio": 2.0,
        "pivot_len": 3,
        "partial_tp_enabled": True,
        "partial_tp_ratio": 0.5,
        "breakeven_buffer_ticks": 1,
        "smc_1x_exit_mode": "1TO1_TP",
        "buffer_ticks": 1,
        "min_sl_ticks": 2,
        "max_sl_ticks": 30,
        "execution_style": "PURE_MARKET",
        "resting_limit_tp": True,
        "invert_signal": False,
        "ratchet_enabled": False,
        "slippage_enabled": False,
        "slippage_ticks": 0
    },
    "TRUMP_TICK_CONSTRAINED_MM": {
        "name": "TRUMP Tick-Constrained Market Making & Scalper",
        "description": (
            "Tick-Constrained Microstructure Market Maker on TRUMP_USDT (0% KCEX fee). "
            "Operates when tick size >= 4.0 bps, quotes with post-only Limit orders, "
            "+1 tick TP, -3 ticks SL with OFI toxic sweep filter & 15m Bollinger compression gating."
        ),
        "symbol": "TRUMP_USDT",
        "strategy_mode": "TICK_CONSTRAINED_MM",
        "timeframe": "15m",
        "leverage": 10,
        "volume_mode": "MULTIPLIER",
        "volume_multiplier": 10.0,
        "margin_fallback_pct": 25.0,
        "tp_ticks": 2,
        "dynamic_tp": False,
        "sl_mode": "TICKS",
        "sl_ticks": 50,
        "cooldown_seconds": 0.0,
        "max_trades": 0,
        "min_tick_bps": 4.0,
        "ofi_window": 50,
        "max_ofi_threshold": 0.40,
        "time_stop_sec": 60.0,
        "entry_queue_qty": 200.0,
        "tp_queue_qty": 200.0,
        "htf_trend_filter_enabled": True,
        "htf_timeframe": "Min15",
        "bb_period": 20,
        "bb_std": 2.0,
        "bbw_percentile_cutoff": 40.0,
        "adx_period": 14,
        "max_adx_sideways": 22.0,
        "simultaneous_mode": False,
        "execution_style": "MAKER_HYBRID",
        "order_type": "LIMIT",
        "resting_limit_tp": True,
        "invert_signal": False,
        "ratchet_enabled": False,
        "slippage_enabled": False,
        "slippage_ticks": 0
    },
    "CUSTOM": {
        "name": "Custom User Configuration",
        "description": "Bypasses presets and uses manual individual toggle variables from settings.py.",
        "symbol": SYMBOL,
        "strategy_mode": STRATEGY_MODE
    }
}


def get_active_preset_config(preset_name: str = None) -> dict:
    """
    Returns the resolved configuration dictionary for the specified or ACTIVE_PRESET.
    Falls back to individual variables if preset is 'CUSTOM' or unrecognized.
    """
    key = (preset_name or ACTIVE_PRESET).upper()
    if key in STRATEGY_PRESETS and key != "CUSTOM":
        cfg = dict(STRATEGY_PRESETS[key])
        if key == "MULTI_ASSET_SMC":
            if "KCEX_LEVERAGE" in os.environ or "LEVERAGE" in os.environ:
                cfg["leverage"] = LEVERAGE
                for a in cfg.get("assets", []):
                    a["leverage"] = LEVERAGE
            if "KCEX_MARGIN_PCT" in os.environ or "MARGIN_PCT" in os.environ:
                cfg["margin_pct"] = MARGIN_PCT
        elif key == "TRUMP_STOCH_RSI":
            if "KCEX_LEVERAGE" in os.environ:
                cfg["leverage"] = LEVERAGE
            if "KCEX_VOLUME_MULTIPLIER" in os.environ:
                cfg["volume_multiplier"] = VOLUME_MULTIPLIER
            if "KCEX_MARGIN_FALLBACK_PCT" in os.environ:
                cfg["margin_fallback_pct"] = MARGIN_FALLBACK_PCT
            if "KCEX_COOLDOWN" in os.environ:
                cfg["cooldown_seconds"] = COOLDOWN_SECONDS
        elif key == "TRUMP_TICK_CONSTRAINED_MM":
            cfg["leverage"] = LEVERAGE
            cfg["volume_multiplier"] = VOLUME_MULTIPLIER
            cfg["margin_fallback_pct"] = MARGIN_FALLBACK_PCT
            cfg["cooldown_seconds"] = COOLDOWN_SECONDS
        elif "ORDER_BLOCK_DEMAND" in key:
            if "PROPDAO_LEVERAGE" in os.environ or "KCEX_LEVERAGE" in os.environ or "LEVERAGE" in os.environ:
                cfg["leverage"] = LEVERAGE
            if "PROPDAO_VOLUME_MODE" in os.environ or "KCEX_VOLUME_MODE" in os.environ or "VOLUME_MODE" in os.environ:
                cfg["volume_mode"] = VOLUME_MODE
            if "PROPDAO_MARGIN_PCT" in os.environ or "KCEX_MARGIN_PCT" in os.environ or "MARGIN_PCT" in os.environ:
                cfg["margin_pct"] = MARGIN_PCT
            if "PROPDAO_TIMEFRAME" in os.environ or "KCEX_TIMEFRAME" in os.environ or "TIMEFRAME" in os.environ:
                cfg["timeframe"] = TIMEFRAME
            if "KCEX_MARGIN_FALLBACK_PCT" in os.environ:
                cfg["margin_fallback_pct"] = MARGIN_FALLBACK_PCT
            if "KCEX_COOLDOWN" in os.environ:
                cfg["cooldown_seconds"] = COOLDOWN_SECONDS
        elif key == "TRUMP_ML_RAPID_SCALPER":
            cfg["leverage"] = LEVERAGE
            cfg["volume_mode"] = VOLUME_MODE
            cfg["volume_contracts"] = VOLUME_CONTRACTS
            cfg["volume_multiplier"] = VOLUME_MULTIPLIER
            cfg["margin_fallback_pct"] = MARGIN_FALLBACK_PCT
            cfg["cooldown_seconds"] = COOLDOWN_SECONDS
        return cfg
    return {
        "name": "Custom Manual Configuration",
        "symbol": SYMBOL,
        "strategy_mode": STRATEGY_MODE,
        "invert_signal": INVERT_SIGNAL,
        "dynamic_regime_fading": DYNAMIC_REGIME_FADING,
        "adx_fading_cutoff": ADX_FADING_CUTOFF,
        "execution_style": EXECUTION_STYLE,
        "maker_queue_timeout_seconds": MAKER_QUEUE_TIMEOUT_SECONDS,
        "resting_limit_tp": RESTING_LIMIT_TP,
        "ratchet_enabled": RATCHET_ENABLED,
        "ratchet_trigger_ticks": RATCHET_TRIGGER_TICKS,
        "ratchet_stall_seconds": RATCHET_STALL_SECONDS,
        "ratchet_tighten_ticks": RATCHET_TIGHTEN_TICKS,
        "ratchet_breakeven_ticks": RATCHET_BREAKEVEN_TICKS,
        "slippage_enabled": SLIPPAGE_ENABLED,
        "slippage_ticks": SLIPPAGE_TICKS,
        "tp_ticks": TP_TICKS,
        "dynamic_tp": (STRATEGY_MODE.upper() in ("ML", "ML_1M", "ML_MODEL", "ML_1M_MODEL")),
        "sl_mode": SL_MODE,
        "sl_ticks": SL_TICKS,
        "sl_roe_pct": SL_ROE_PCT,
        "volume_multiplier": VOLUME_MULTIPLIER,
        "margin_fallback_pct": MARGIN_FALLBACK_PCT,
        "max_trades": 0,
        "use_atr_targets": USE_ATR_TARGETS,
        "atr_tp_multiplier": ATR_TP_MULTIPLIER,
        "atr_sl_multiplier": ATR_SL_MULTIPLIER,
        "volume_filter_enabled": VOLUME_FILTER_ENABLED,
        "volume_filter_multiplier": VOLUME_FILTER_MULTIPLIER,
        "queue_dynamics_enabled": QUEUE_DYNAMICS_ENABLED,
        "simulate_intra_tick_liquidation": SIMULATE_INTRA_TICK_LIQUIDATION,
        "microstructure_imbalance_threshold": MICROSTRUCTURE_IMBALANCE_THRESHOLD,
        "volatility_regime_period": VOLATILITY_REGIME_PERIOD,
        "order_type": ORDER_TYPE,
        "cancel_if_unfilled": CANCEL_IF_UNFILLED,
        "tp_atr_mult": 3.0,
        "sl_atr_mult": 1.5,
        "confidence_threshold": 0.38,
        "confidence_threshold_sell": 0.38,
        "us_market_hours_filter_enabled": US_MARKET_HOURS_FILTER_ENABLED
    }



