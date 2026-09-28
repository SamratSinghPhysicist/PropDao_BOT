# KCEX & Binance High-Fidelity Dual-Feed Backtester

A high-performance, modular backtesting engine that simulates real-time trading against historical market data using the exact same strategy, execution, and risk management logic as the live KCEX engine.

---

## 🌟 Key Features

1. **Zero Logic Drift (100% Live Strategy Reuse)**:
   - Evaluates identical strategies from the modular `strategies/` package:
     - `EMACrossoverStrategy` (Moving average crossover with candle confirmation and lookback deduplication)
     - `StochasticRSIStrategy` (Overbought/oversold reversal scalping with zone filtering)
     - `MasterplanStrategy` (Unified trade orchestrator coordinating TP/SL and execution)
   - Guaranteed Min-Profit TP (`entry + N*pu`) and Stop Loss (ROE %, ticks, or price move %) formulas match live production trading.
2. **Dual-Feed Synchronization**:
   - **OHLCV Candles**: Feeds dynamic timeframes (`1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `6h`, `8h`, `12h`, `1d`) to update indicators and generate trade signals without lookahead bias.
   - **Tick-by-Tick Trades**: Streams millisecond-level executed trades from `Historical_Trades_Data_Binance` to test whether rapid price spikes hit Take Profit or Stop Loss, with ultra-fast binary seek jumping directly to entry timestamps.
3. **Dynamic Discovery & Range Scanner**:
   - Automatically scans `BACKTESTER/OHLCV_Data_Binance` and `BACKTESTER/Historical_Trades_Data_Binance`.
   - Identifies all available pairs, timeframes, and calculates date ranges across multi-gigabyte files within milliseconds.
   - Discovers common high-fidelity overlap ranges where both OHLCV and tick trades exist.
4. **Institutional Performance Analytics & Reporting**:
   - Calculates Win Rate, Net Profit (USDT & INR), Gross Profit/Loss, Total Taker Fees, Profit Factor, Max Drawdown (USDT and %), Sharpe Ratio, Sortino Ratio, Calmar Ratio, and streak statistics.
   - Automatically exports trade logs and executive summaries to `BACKTESTER/reports/` in **CSV**, **JSONL**, and **Markdown** formats.
5. **Interactive Comparison Studio & Deep Analytics (Web & CLI)**:
   - **Multi-Factor Comparison Matrix**: Side-by-side evaluation of arbitrary runs with automatic detection of parameter tweaks and best-in-class highlights.
   - **Normalized Equity Curve Overlays & Radar**: Synchronized multi-run ROI charts, peak-to-trough drawdowns, and 6-pillar radar footprints.
   - **High-Efficiency Caching**: Zero-lag downsampling turns 50MB+ raw files into instant sub-millisecond comparisons without RAM bloat.
   - **Web UI**: Launch with `python run_analytics.py` (opens `http://localhost:8000`).
   - **Terminal CLI**: Quick terminal inspection with `python BACKTESTER/analyzer.py --compare`.
6. **Completely Isolated**:
   - All backtester code resides strictly inside `BACKTESTER/`.
   - The live trading engine (`run_engine.py`, `semi_auto_trader.py`, `kcex/`) remains 100% untouched.

---


## 📁 Directory & Data Structure

```text
BACKTESTER/
├── Historical_Trades_Data_Binance/       <-- Tick-by-tick monthly trades CSVs
│   └── TRUMP_USDT/
│       ├── TRUMPUSDT-trades-2026-07.csv
│       └── TRUMPUSDT-trades-2026-08.csv
│
├── OHLCV_Data_Binance/                  <-- Candlestick data per symbol & timeframe
│   ├── DOGEUSDT/
│   │   ├── 1m/  (e.g. DOGEUSDT-1m-2025-01.csv ... 2026-08.csv)
│   │   ├── 5m/
│   │   ├── 15m/
│   │   ├── 1h/
│   │   └── 1d/
│   └── TRUMPUSDT/
│       ├── 1m/  (e.g. TRUMPUSDT-1m-2025-01.csv ... 2026-08.csv)
│       ├── 15m/
│       └── ...
│
├── engine/                              <-- Core backtester modules
│   ├── __init__.py
│   ├── config.py                        <-- BacktestConfig (inherits ExecutionConfig)
│   ├── scanner.py                       <-- Dynamic DataCatalog & binary-seek scanner
│   ├── data_loader.py                   <-- Streaming OHLCVLoader & TickTradeStreamer
│   ├── market_sim.py                    <-- BacktestMarket (emulates KCEXMarket)
│   ├── execution_sim.py                 <-- BacktestExecutionEngine & VirtualClock
│   ├── metrics.py                       <-- PerformanceCalculator & statistical metrics
│   └── reporting.py                     <-- Terminal reports & CSV/JSONL/MD exporters
│
├── reports/                             <-- Generated backtest run artifacts
│   ├── backtest_<SYMBOL>_<TIMESTAMP>_trades.csv
│   ├── backtest_<SYMBOL>_<TIMESTAMP>_trades.jsonl
│   └── backtest_<SYMBOL>_<TIMESTAMP>_summary.md
│
├── run_backtest.py                      <-- CLI runner & interactive wizard
├── test_backtester.py                   <-- Comprehensive test suite
└── README.md
```

---

## 🚀 Quick Start & Usage

### 1. Data Catalog & Range Discovery
Scan all available historical data, timeframes, and date ranges:
```powershell
python BACKTESTER/run_backtest.py --scan
```

Sample output:
```text
============================================================================================
          BACKTESTER HISTORICAL DATA CATALOG & TIMEFRAME DISCOVERY
============================================================================================
Symbol         | Type     | Details / Timeframes     | Date Range (UTC)                      
--------------------------------------------------------------------------------------------
TRUMP_USDT     | OHLCV    | 12 TFs (161.8 MB)        | 2025-01-18 -> 2026-08-31              
               |          |   +-- 1m (20 files)      | 2025-01-18 -> 2026-08-31              
               |          |   +-- 15m (20 files)     | 2025-01-18 -> 2026-08-31              
               |          |   +-- 1h (20 files)      | 2025-01-18 -> 2026-08-31              
TRUMP_USDT     | TRADES   | 2 files (1.7 GB)         | 2026-07-01 -> 2026-08-31              
               | OVERLAP  | [*] High-Fidelity Overlap: 2026-07-01 00:00:01 UTC -> 2026-08-31 23:59:00 UTC
--------------------------------------------------------------------------------------------
```

### 2. Interactive Setup Wizard
Run with no arguments to launch the step-by-step interactive wizard:
```powershell
python BACKTESTER/run_backtest.py
```
Guides you through:
- Selecting symbol (e.g. `TRUMP_USDT`, `DOGE_USDT`)
- Selecting timeframe (dynamically populated from available folders)
- Selecting strategy (`STOCH_RSI`, `EMA_CROSSOVER`)
- Choosing date range (High-Fidelity Overlap, Full OHLCV, or Custom)
- Configuring leverage, capital, and TP/SL rules

### 3. Command-Line Flag Execution

#### Run EMA Crossover on TRUMP_USDT with 1m Candles and Tick-by-Tick Simulation:
```powershell
python BACKTESTER/run_backtest.py --symbol TRUMP_USDT --timeframe 1m --strategy EMA_CROSSOVER --ema-preset 5/13 --start 2026-07-01 --end 2026-07-31 --tp-ticks 2 --sl-ticks 10 --leverage 30
```

#### Run Stochastic RSI Scalper on TRUMP_USDT:
```powershell
python BACKTESTER/run_backtest.py --symbol TRUMP_USDT --timeframe 1m --strategy STOCH_RSI --start 2026-07-01 --end 2026-07-15 --tp-ticks 2 --sl-ticks 10
```

#### Run on Higher Timeframes (e.g. 15m, 1h) with Candle Fallback:
```powershell
python BACKTESTER/run_backtest.py --symbol DOGE_USDT --timeframe 15m --strategy EMA_CROSSOVER --start 2026-06-01 --end 2026-08-31
```

#### Simulated Real-Time Visual Playback:
Watch trades execute with paced delays at a custom speed multiplier (e.g. 10x):
```powershell
python BACKTESTER/run_backtest.py --symbol TRUMP_USDT --timeframe 1m --strategy EMA_CROSSOVER --start 2026-07-01 --speed 10.0
```

---

## ⚙️ Available CLI Options

| Flag | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--scan` | Flag | `False` | Scans and prints data catalog and exits |
| `--interactive` | Flag | `False` | Forces interactive wizard mode |
| `--symbol` | String | None | Trading pair symbol (e.g. `TRUMP_USDT`, `DOGE_USDT`) |
| `--timeframe` | String | `1m` | Strategy timeframe (`1m`, `3m`, `5m`, `15m`, `1h`, `1d`, etc.) |
| `--strategy` | Choice | `STOCH_RSI` | `STOCH_RSI`, `EMA_CROSSOVER` |
| `--ema-preset` | Choice | `5/13` | Length presets: `5/13`, `9/21`, `3/8` |
| `--stoch-preset` | Choice | `FAST_SCALP` | Stoch preset: `FAST_SCALP`, `STANDARD`, `MICRO_BURST` |
| `--start` | String | None | Start date (`YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS`) |
| `--end` | String | None | End date (`YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS`) |
| `--ticks` / `--no-ticks` | Flag | `True` | Toggle high-fidelity millisecond tick streaming |
| `--fee-mode` | Choice | `LIVE` | Fee mode: `LIVE` (query KCEX API), `ZERO` (0.0%), or `MANUAL` |
| `--maker-fee` | Float | None | Maker fee rate or % (e.g. `0.0` or `0.02`) |
| `--taker-fee` | Float | None | Taker fee rate or % (e.g. `0.0` or `0.05`) |
| `--volume-mode` | Choice | `CONTRACTS` | Sizing mode: `CONTRACTS`, `MULTIPLIER`, or `MIN` |
| `--contracts` | Integer | None | Exact number of contracts per trade (e.g. `10`, `50`, `100`) |
| `--volume-multiplier` | Float | `1.0` | Multiplier of min contract volume (e.g. `1.0`, `2.0`, `5.0`) |
| `--tp-ticks` | Integer | `2` | Take Profit in ticks (`pu`) from entry price |
| `--sl-mode` | Choice | `TICKS` | Stop loss mode: `TICKS`, `ROE`, `PRICE_PCT` |
| `--sl-ticks` | Integer | `10` | SL distance in ticks |
| `--sl-roe` | Float | `25.0` | SL limit in ROE % on margin |
| `--leverage` | Integer | `30` | Leverage multiplier |
| `--capital` | Float | `100.0` | Starting wallet balance in USDT |
| `--max-trades` | Integer | `0` | Max trades limit (0 = run entire period) |
| `--speed` | Float | `0.0` | Paced playback speed (0 = warp speed) |
| `--slippage` | Integer | `0` | Adverse fill slippage in ticks |

---

## ⚡ GitHub Actions Interactive Cloud Runner

You can trigger backtests on demand directly from the GitHub web interface without uploading large data sets to Git!

### How it works:
1. Navigate to your repository on GitHub and click the **Actions** tab.
2. Select the **Run Strategy Backtest** workflow on the left sidebar.
3. Click the **Run workflow** dropdown button.
4. Customize your backtest parameters interactively:
   - **Symbol**: e.g., `TRUMP_USDT`, `DOGE_USDT`, `BTC_USDT`
   - **Timeframe**: `1m`, `5m`, `15m`, `1h`, `1d`, etc.
   - **Strategy**: `STOCH_RSI`, `EMA_CROSSOVER`
   - **Presets**: EMA lengths (`5/13`, `9/21`, `3/8`), Stoch RSI presets (`FAST_SCALP`, `STANDARD`, `MICRO_BURST`)
   - **Dates**: Start & End dates (`YYYY-MM-DD`)
   - **High-Fidelity Ticks**: Toggle millisecond tick simulation
   - **Fee Schedule**: `LIVE` (KCEX live API query), `ZERO` (0%), or `MANUAL`
   - **Risk Parameters**: TP ticks, SL mode (`ROE`, `TICKS`, `PRICE_PCT`), Leverage, Starting Capital
5. Click the green **Run workflow** button.

### What happens automatically:
- **On-Demand Data Fetching**: The runner automatically downloads the requested Binance Vision monthly archives and caches them using `actions/cache@v4` for instant subsequent runs.
- **Backtest Execution**: Simulates the exact live strategy and risk engine against historical data.
- **Job Summary Dashboard**: Markdown executive performance summary is embedded directly into the GitHub Actions run summary page.
- **Downloadable Artifacts**: The trade log CSV (`trades.csv`), JSONL log (`trades.jsonl`), and summary (`summary.md`) are zipped and made available as downloadable artifacts in the workflow run.

---

## 🧪 Testing

Run the automated test suite to verify data loaders, market simulation, strategy logic, and math:
```powershell
python -m unittest BACKTESTER/test_backtester.py
```
