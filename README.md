# PropDAO.finance Trading Bot & Institutional Backtester

An institutional-grade algorithmic trading system and quantitative backtester built specifically for **PropDAO.finance**, implementing **Vivek Yadav's Order Block + Demand & Supply Zone Strategy** with zero logic drift.

---

## 🌟 Highlights & Architecture

1. **Exact Vivek Yadav SMC Strategy (100% Faithful)**:
   - Rolling fractal swing pivot detection ($N=5$ bars).
   - Body-close Break of Structure (**BOS**).
   - Extrema origin discovery and backward scan for last opposite candle (Order Block).
   - Retest & directional confirmation bounce (Candle $T$ tap + $T$ or $T+1$ directional close).
   - Direct boundary breach invalidation.
   - Exact **1:2 Risk-to-Reward geometry** with optional **1:1 partial profit exit (50%) & breakeven lock**.

2. **PropDAO Execution Engine (Live & Paper Trading)**:
   - **Full PropDAO v1 REST API Integration**: Built to the official [PropDAO API Reference](https://www.propdao.finance/docs).
   - **Order Types**: `market`, `limit`, `stop_market`, `stop_limit`, `take_market`, `take_limit`, `scale`, `twap`.
   - **Time in Force**: `gtc` (default), `ioc` (immediate-or-cancel), `alo` (add-liquidity-only / post-only maker guarantee).
   - **Brackets & Exits**: Full & partial TP/SL, resting maker limit exits, trailing stops, and breakeven locks.
   - **Isolated Margin & Leverage**: Automatic leverage clamping (2x for crypto $\ge \$1\text{B}$, 1.5x for FX/equities, 1x for alts) and isolated margin tracking (`notional / leverage`).
   - **High-Fidelity Paper Trading**: Complete exchange emulator (`PropDAOPaperEngine`) with simulated orders, fills, fee schedule, and millisecond bracket evaluation — zero API key required.

3. **Prop Firm Risk Management & Drawdown Guards**:
   - **roomUsd Budgeting**: Always sizes trades from `GET /accounts/:id/risk` so that $(|\text{entry} - \text{SL}| \times \text{qty}) \le (\text{roomUsd} \times \text{risk\_fraction})$.
   - **Static Max Drawdown Floor**: 5% below starting balance.
   - **Daily Loss Limit Floor**: 2% hanging off 00:00 UTC anchor equity.
   - **Risk Guard Circuit Breaker**: Auto-flattens positions and cancels resting orders if room drops below 1.0% or $\$100$.
   - **Cadence Rules**: Enforces $\ge 1.0\text{s}$ minimum hold and $\ge 0.5\text{s}$ between market executions.
   - **Idempotent Execution**: Auto-generated `intentId` prevents duplicate fills across network blips.

4. **Comprehensive Backtester (Local CLI, Wizard & GitHub Actions)**:
   - **No-Lookahead Candle Scoping**: Feeds historical streams bar-by-bar to strategy.
   - **Auto-Downloader**: Automatically downloads and caches multi-month candlestick histories directly from public APIs.
   - **Detailed Reporting**: Outputs ASCII terminal dashboard, CSV of trades, streaming JSONL, and executive Markdown summary reports.
   - **GitHub Actions Integration**: Run backtests directly in the cloud via `.github/workflows/backtest.yml` with native `$GITHUB_STEP_SUMMARY` display and artifact uploads.

---

## 📁 Repository Structure

```text
D:\My_Bots\Trading\PropDao\
├── .env.example                               # Environment template
├── .gitignore                                 # Git ignore patterns
├── requirements.txt                           # Python dependencies
├── README.md                                  # System documentation
│
├── .github/
│   └── workflows/
│       ├── backtest.yml                       # GitHub Actions workflow for backtesting
│       └── paper_trading.yml                  # GitHub Actions test runner
│
├── propdao/                                   # PropDAO Platform SDK & Models
│   ├── __init__.py
│   ├── client.py                              # REST API client (rate limits, retries, 409 handling)
│   ├── models.py                              # Enums (OrderType, Side, etc.) & Dataclasses
│   ├── market.py                              # Market data provider (symbols, lot steps, fees, klines)
│   ├── risk_manager.py                        # roomUsd budget, floor monitoring & risk guards
│   ├── order_manager.py                       # Order placement, brackets, scale ladders, TWAP
│   ├── account_manager.py                     # Account discovery, evaluation tracking, pass/fail status
│   └── paper_engine.py                        # High-fidelity paper trading simulation engine
│
├── engine/                                    # Execution Architecture
│   ├── __init__.py
│   ├── execution_engine.py                    # Unified orchestrator for LIVE and PAPER trading
│   └── position_tracker.py                    # Real-time position tracking, hold duration & 1:1 TP
│
├── strategies/                                # Modular Trading Strategies
│   ├── __init__.py
│   ├── base.py                                # Abstract BaseStrategy interface
│   └── order_block_demand/                    # Vivek Yadav OB + Demand Zone Strategy Package
│       ├── __init__.py
│       ├── order_block_demand.py              # Strategy logic (100% faithful SMC implementation)
│       ├── Vivek_Yadav_OB_Strategy_PnL.js      # Reference KLineChart indicator engine
│       ├── (MANUAL - BACKTEST) Vivek Yadav - OB and Demand Zone -.txt
│       ├── NoteGPT_Transcript_Demand And Supply Trading Strategy...txt
│       └── NoteGPT_Transcript_Order Block Strategy...txt
│
├── backtester/                                # Institutional Backtesting Framework
│   ├── __init__.py
│   ├── config.py                              # BacktestConfig hyperparameters
│   ├── data_loader.py                         # Data loader with built-in auto-downloader
│   ├── market_sim.py                          # Zero-lookahead backtest market emulator
│   ├── execution_sim.py                       # PropDAO fee, margin, and SL/TP simulator
│   ├── metrics.py                             # Win Rate, Sharpe, Sortino, Drawdown calculator
│   ├── reporting.py                           # ASCII tables, CSV/JSONL/Markdown exporters
│   └── runner.py                              # Backtest execution runner
│
├── run_backtest.py                            # CLI and interactive wizard for backtesting
├── run_bot.py                                 # Main production entrypoint for Live and Paper bot
│
└── tests/                                     # Comprehensive Unit Test Suite
    ├── __init__.py
    ├── test_strategy.py                       # Strategy SMC rules and invalidation tests
    ├── test_execution_engine.py               # Paper engine, risk sizing, and SL/TP tests
    └── test_backtester.py                     # End-to-end backtest pipeline tests
```

---

## 🚀 Quick Start Guide

### 1. Installation

```powershell
cd D:\My_Bots\Trading\PropDao
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Run the Unit Test Suite

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

---

## 📈 Running the Backtester

### Option A: Command Line Interface (CLI)

Run backtest on `BTCUSDC` for 15-minute candles:
```powershell
python run_backtest.py --symbol BTCUSDC --timeframe 15m --start 2026-01-01 --end 2026-08-31 --balance 25000 --leverage 2.0 --risk 0.25
```

Custom parameters:
- `--symbol`: Trading symbol (e.g. `BTCUSDC`, `ETHUSDC`, `SOLUSDC`).
- `--timeframe`: Candle timeframe (`1m`, `5m`, `15m`, `1h`, `4h`, `1d`).
- `--start`: Start date (`YYYY-MM-DD`).
- `--end`: End date (`YYYY-MM-DD`).
- `--balance`: Account size (e.g. `25000`, `50000`, `100000`).
- `--leverage`: Leverage multiplier (`2.0` for majors, `1.0` for alts).
- `--risk`: Fraction of `roomUsd` risked on stop loss (default: `0.25` = 25%).
- `--pivot-len`: Swing pivot length (default: `5`).
- `--rr`: Target risk-to-reward ratio (default: `2.0`).
- `--no-partial-tp`: Disables 1:1 partial profit exit.

### Option B: Interactive Setup Wizard

Launch the interactive prompt:
```powershell
python run_backtest.py --interactive
```

### Option C: Cloud Backtesting via GitHub Actions

1. Go to your GitHub repository -> **Actions** tab.
2. Select **PropDAO Strategy Backtest**.
3. Click **Run workflow** and configure inputs:
   - Symbol: `BTCUSDC`
   - Timeframe: `15m`
   - Start Date: `2026-01-01`
   - End Date: `2026-08-31`
   - Balance: `25000`
   - Leverage: `2.0`
4. The workflow runs headlessly, displays the performance summary table directly in the **Job Summary**, and attaches the CSV, JSONL, and Markdown reports as downloadable artifacts!

---

## 🤖 Running the Trading Bot

The bot connects directly to the PropDAO trading API (`https://app.propdao.finance/api/v1`) using your `PROPDAO_API_KEY`. It allows trading on both your **Free Trial (Demo)** and **Evaluation (Real)** accounts.

### Available Accounts on PropDAO:
- **Trial / Demo Account (Paper)**: `PROP-L6AS4Z5H` ($50,000 Free Trial)
- **Live Challenge Account (Evaluation)**: `PROP-C137DA4E` ($25,000 Silver Evaluation)

### Account Selection Behavior:
- **Explicit Account**: Set `PROPDAO_ACCOUNT=PROP-L6AS4Z5H` in `.env` or pass `--account PROP-L6AS4Z5H`.
- **Interactive Prompt**: If no account ID is passed, the bot will **NOT** silently default to the first account. Instead, it will display a formatted list of all accounts found for your key and prompt you to pick which one to trade!

### 1. Paper / Demo Trading Mode (Trial Account)
Runs on your PropDAO $50K Free Trial account:
```powershell
# Set PROPDAO_MODE=paper in .env, or run with --mode paper:
python run_bot.py --mode paper --symbol BTCUSDC --timeframe 15m
```

### 2. Live Trading Mode (Evaluation / Real Account)
Runs on your PropDAO $25K Evaluation challenge account:
```powershell
# Set PROPDAO_MODE=live in .env, or run with --mode live:
python run_bot.py --mode live --symbol BTCUSDC --timeframe 15m
```

### 3. Local Mock Mode (Offline Testing)
If you wish to test offline without connecting to PropDAO servers:
```powershell
python run_bot.py --mode mock --symbol BTCUSDC --timeframe 15m --balance 25000
```

---

## 🛡️ PropDAO Risk Engine & Sizing Mechanics

PropDAO accounts are terminated immediately if equity touches the drawdown floor. The bot strictly enforces the following formulas:

$$\text{Floor} = \max(\text{MaxFloor}, \text{DailyFloor})$$
$$\text{roomUsd} = \text{Equity} - \text{Floor}$$
$$\text{Risk Budget} = \text{roomUsd} \times \text{risk\_fraction}$$
$$\text{Quantity} = \text{round}\left(\frac{\text{Risk Budget}}{|\text{Entry} - \text{SL}| \times \text{lotStep}}\right) \times \text{lotStep}$$
$$\text{Allocated Margin} = \frac{\text{Quantity} \times \text{Entry}}{\text{Leverage}}$$

### Partial Exit & Breakeven Lock (1:1 & 1:2 RR)
- **At Entry**: Stop Loss set to Order Block boundary ($OB_{low}$ for longs, $OB_{high}$ for shorts).
- **At 1:1 RR**: Bot closes **50% of the position** at mark price and moves Stop Loss to $\text{Entry} \pm \text{buffer}$, locking in a risk-free trade.
- **At 1:2 RR**: Bot exits the remaining 50% runner at the full take profit target.

---

## 📄 License & Credits
- **Strategy Design**: Advance Crypto Trader (Vivek Yadav)
- **Prop Firm Platform**: [PropDAO.finance](https://www.propdao.finance)
