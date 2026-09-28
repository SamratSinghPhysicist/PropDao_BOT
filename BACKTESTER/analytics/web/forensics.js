/**
 * KCEX Strategy Forensics & Replay Lab (Forensic Charting Controller)
 * ===================================================================
 * Powered by TradingView Lightweight Charts & High-Fidelity Tick Engine.
 * Features:
 * - Multi-timeframe Candlestick Chart (1m, 3m, 5m, 15m, 30m, 1h, 4h, 1d)
 * - Trade entry/exit markers, TP/SL price lines, and duration bounding
 * - Pure-Python strategy indicator reproduction (EMA 5/13, Stoch RSI %K/%D)
 * - Millisecond-level tick trade overlay & MFE/MAE forensics
 * - Historical Replay Engine (1x - 100x speeds, scrub bar, live unrealized PnL)
 * - "What Happened After Exit?" trajectory tracker
 * - Interactive What-If Exit simulator
 */

class ForensicsController {
  constructor() {
    this.chart = null;
    this.candleSeries = null;
    this.volumeSeries = null;
    this.fastEmaSeries = null;
    this.slowEmaSeries = null;
    
    this.oscChart = null;
    this.stochKSeries = null;
    this.stochDSeries = null;
    
    this.priceLines = [];
    this.activeRunId = null;
    this.activeTradeId = null;
    this.activeSymbol = "TRUMP_USDT";
    this.activeTimeframe = "1m";
    this.tradeContext = null;
    
    // Replay State
    this.isPlaying = false;
    this.replaySpeed = 1.0;
    this.currentTickIndex = 0;
    this.replayInterval = null;
    this.replayMode = "tick"; // "tick" = tick replay, "chart" = live chart replay
    this.chartReplayIndex = 0; // for progressive candle reveal
    this.chartReplayPriceLine = null; // live price line during chart replay
    this.fullCandleData = []; // full candle data for chart replay
    this.fullVolumeData = []; // full volume data for chart replay
    this.fullIndicators = null; // full indicator data for chart replay
    
    // All-trades overlay
    this.showAllTrades = false;

    // Replay Scissor State
    this.isScissorMode = false;
    this.scissorCutIndex = null;
    
    this.catalog = null;
    this.allTrades = [];
    this.filteredTrades = [];
    this.tradeCounts = { all: 0, wins: 0, losses: 0, timeouts: 0, with_ticks: 0 };
    this.activeTradeFilter = "ALL";
    this.modalFilter = "ALL";

    // Replay Live Executions & Positions
    this.tradesByOpenSec = new Map();
    this.tradesByCloseSec = new Map();
    this.replayActivePosition = null;
    this.revealedReplayMarkers = [];
    this.bannerTimeout = null;
    this.markerUpdateTimeout = null;
  }

  async init() {
    this.bindDOM();
    this.initCharts();
    await this.loadCatalog();
  }

  bindDOM() {
    // Run Selector
    const runSelect = document.getElementById("forensicRunSelect");
    if (runSelect) {
      runSelect.addEventListener("change", (e) => {
        this.activeRunId = e.target.value;
        this.onRunChanged();
      });
    }

    // Trade Selector
    const tradeSelect = document.getElementById("forensicTradeSelect");
    if (tradeSelect) {
      tradeSelect.addEventListener("change", (e) => {
        this.activeTradeId = parseInt(e.target.value, 10);
        this.loadTrade(this.activeRunId, this.activeTradeId, true);
      });
    }

    // Timeframe buttons
    document.querySelectorAll(".tf-btn").forEach(btn => {
      btn.addEventListener("click", async (e) => {
        document.querySelectorAll(".tf-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        this.activeTimeframe = btn.dataset.tf;
        if (this.activeRunId) {
          this.showChartBlur(`Switching to ${this.activeTimeframe}...`);
          await this.loadRunCandles();
          if (this.activeTradeId) {
            this.loadTrade(this.activeRunId, this.activeTradeId, false);
          }
          this.hideChartBlur();
        }
      });
    });

    // Replay Controls
    const btnPlay = document.getElementById("btnReplayPlay");
    const btnPause = document.getElementById("btnReplayPause");
    const btnReset = document.getElementById("btnReplayReset");
    const btnStepForward = document.getElementById("btnReplayStepFwd");
    const btnStepBack = document.getElementById("btnReplayStepBack");
    const scrubSlider = document.getElementById("replayScrubSlider");

    if (btnPlay) btnPlay.addEventListener("click", () => this.playReplay());
    if (btnPause) btnPause.addEventListener("click", () => this.pauseReplay());
    if (btnReset) btnReset.addEventListener("click", () => this.resetReplay());
    if (btnStepForward) btnStepForward.addEventListener("click", () => this.stepReplay(1));
    if (btnStepBack) btnStepBack.addEventListener("click", () => this.stepReplay(-1));

    if (scrubSlider) {
      scrubSlider.addEventListener("input", (e) => {
        this.seekReplay(parseInt(e.target.value, 10));
      });
    }

    // Speed chips
    document.querySelectorAll(".speed-chip").forEach(chip => {
      chip.addEventListener("click", () => {
        document.querySelectorAll(".speed-chip").forEach(c => c.classList.remove("active"));
        chip.classList.add("active");
        this.replaySpeed = parseFloat(chip.dataset.speed);
        if (this.isPlaying) {
          this.pauseReplay();
          this.playReplay();
        }
      });
    });

    // Next / Prev Trade Buttons
    const btnPrev = document.getElementById("btnPrevTrade");
    const btnNext = document.getElementById("btnNextTrade");
    if (btnPrev) btnPrev.addEventListener("click", () => this.stepTrade(-1));
    if (btnNext) btnNext.addEventListener("click", () => this.stepTrade(1));

    // Jump to Trade Input
    const jumpInput = document.getElementById("jumpTradeInput");
    const btnJump = document.getElementById("btnJumpTrade");
    if (btnJump) {
      btnJump.addEventListener("click", () => {
        if (jumpInput && jumpInput.value) {
          this.jumpToTrade(parseInt(jumpInput.value, 10));
        }
      });
    }
    if (jumpInput) {
      jumpInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && jumpInput.value) {
          this.jumpToTrade(parseInt(jumpInput.value, 10));
        }
      });
    }

    // Trade Filter Chips
    document.querySelectorAll("[data-trade-filter]").forEach(chip => {
      chip.addEventListener("click", () => {
        document.querySelectorAll("[data-trade-filter]").forEach(c => c.classList.remove("active"));
        chip.classList.add("active");
        this.applyTradeFilter(chip.dataset.tradeFilter);
      });
    });

    // All Trades Explorer Modal
    const btnOpenModal = document.getElementById("btnOpenAllTradesModal");
    const btnCloseModal = document.getElementById("btnCloseAllTradesModal");
    const btnCloseFooter = document.getElementById("btnCloseAllTradesFooter");
    const modalSearch = document.getElementById("modalTradeSearch");

    if (btnOpenModal) btnOpenModal.addEventListener("click", () => this.openAllTradesModal());
    if (btnCloseModal) btnCloseModal.addEventListener("click", () => this.closeAllTradesModal());
    if (btnCloseFooter) btnCloseFooter.addEventListener("click", () => this.closeAllTradesModal());

    if (modalSearch) {
      modalSearch.addEventListener("input", () => this.renderModalTable());
    }

    document.querySelectorAll("[data-modal-filter]").forEach(chip => {
      chip.addEventListener("click", () => {
        document.querySelectorAll("[data-modal-filter]").forEach(c => c.classList.remove("active"));
        chip.classList.add("active");
        this.modalFilter = chip.dataset.modalFilter;
        this.renderModalTable();
      });
    });

    // What-If Form
    const btnSimulate = document.getElementById("btnRunWhatIf");
    if (btnSimulate) {
      btnSimulate.addEventListener("click", () => this.runWhatIfSimulation());
    }

    // All Trades On Chart Toggle
    const toggleAllTrades = document.getElementById("toggleShowAllTrades");
    if (toggleAllTrades) {
      toggleAllTrades.addEventListener("change", (e) => {
        this.showAllTrades = e.target.checked;
        this.renderAllTradesMarkers();
      });
    }

    // Chart Viewport Navigation Controls
    const btnFit = document.getElementById("btnFitChart");
    if (btnFit) btnFit.addEventListener("click", () => this.fitChartContent());

    const btnLatest = document.getElementById("btnScrollToLatest");
    if (btnLatest) btnLatest.addEventListener("click", () => this.scrollToLatest());

    const btnCenter = document.getElementById("btnCenterTrade");
    if (btnCenter) btnCenter.addEventListener("click", () => this.centerSelectedTrade());

    // Chart Replay Mode Toggle
    const btnChartReplay = document.getElementById("btnChartReplay");
    if (btnChartReplay) {
      btnChartReplay.addEventListener("click", () => this.startChartReplay());
    }

    // Replay Scissor Tool
    const btnScissor = document.getElementById("btnReplayScissor");
    if (btnScissor) {
      btnScissor.addEventListener("click", () => this.toggleScissorMode());
    }

    // Chart mouse events for Scissor Cut & Preview
    const chartCanvas = document.getElementById("forensicMainChartContainer");
    if (chartCanvas) {
      chartCanvas.addEventListener("mousemove", (e) => this.onChartMouseMove(e));
      chartCanvas.addEventListener("mouseleave", () => this.onChartMouseLeave());
      chartCanvas.addEventListener("click", (e) => this.onChartClick(e));
    }
  }

  initCharts() {
    const mainContainer = document.getElementById("forensicMainChartContainer");
    const oscContainer = document.getElementById("forensicOscChartContainer");
    if (!mainContainer || !window.LightweightCharts) return;

    // Dark Institutional Theme
    const chartOptions = {
      layout: {
        background: { color: "#080c14" },
        textColor: "#8a99ad",
        fontSize: 11,
        fontFamily: "'JetBrains Mono', monospace"
      },
      grid: {
        vertLines: { color: "rgba(30, 41, 59, 0.45)" },
        horzLines: { color: "rgba(30, 41, 59, 0.45)" }
      },
      crosshair: {
        mode: LightweightCharts.CrosshairMode.Normal,
        vertLine: { color: "rgba(0, 210, 255, 0.4)", width: 1, style: 2 },
        horzLine: { color: "rgba(0, 210, 255, 0.4)", width: 1, style: 2 }
      },
      rightPriceScale: {
        borderColor: "rgba(30, 41, 59, 0.8)",
        scaleMargins: { top: 0.1, bottom: 0.25 }
      },
      timeScale: {
        borderColor: "rgba(30, 41, 59, 0.8)",
        timeVisible: true,
        secondsVisible: true
      }
    };

    // 1. Create Main Chart
    this.chart = LightweightCharts.createChart(mainContainer, chartOptions);

    // Candlestick Series
    this.candleSeries = this.chart.addCandlestickSeries({
      upColor: "#00f090",
      downColor: "#ff3366",
      borderVisible: false,
      wickUpColor: "#00f090",
      wickDownColor: "#ff3366"
    });

    // Volume Series
    this.volumeSeries = this.chart.addHistogramSeries({
      color: "#1e293b",
      priceFormat: { type: "volume" },
      priceScaleId: "",
      scaleMargins: { top: 0.82, bottom: 0 }
    });

    // EMA Overlays (Titles removed so right price scale is not blocked; shown in top-left HUD legend)
    this.fastEmaSeries = this.chart.addLineSeries({
      color: "#00d2ff",
      lineWidth: 1.5,
      lastValueVisible: false,
      priceLineVisible: false
    });

    this.slowEmaSeries = this.chart.addLineSeries({
      color: "#ffb800",
      lineWidth: 1.5,
      lastValueVisible: false,
      priceLineVisible: false
    });

    // Crosshair Subscriber for TradingView-style top-left HUD legend
    this.chart.subscribeCrosshairMove((param) => {
      this.onCrosshairMove(param);
    });

    // 2. Create Oscillator Chart (Stoch RSI)
    if (oscContainer) {
      const oscOptions = {
        ...chartOptions,
        rightPriceScale: {
          borderColor: "rgba(30, 41, 59, 0.8)",
          scaleMargins: { top: 0.1, bottom: 0.1 }
        }
      };
      this.oscChart = LightweightCharts.createChart(oscContainer, oscOptions);

      // Stoch RSI %K and %D
      this.stochKSeries = this.oscChart.addLineSeries({
        color: "#00d2ff",
        lineWidth: 1.5,
        title: "Stoch %K"
      });

      this.stochDSeries = this.oscChart.addLineSeries({
        color: "#ff3366",
        lineWidth: 1.5,
        title: "Stoch %D"
      });

      // Add 20 Oversold & 80 Overbought baseline reference lines
      this.stochKSeries.createPriceLine({
        price: 80,
        color: "rgba(255, 51, 102, 0.5)",
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: "OB (80)"
      });
      this.stochKSeries.createPriceLine({
        price: 20,
        color: "rgba(0, 240, 144, 0.5)",
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: "OS (20)"
      });

      // Synchronize time scales
      this.chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
        if (range && this.oscChart) {
          this.oscChart.timeScale().setVisibleLogicalRange(range);
        }
      });
      this.oscChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
        if (range && this.chart) {
          this.chart.timeScale().setVisibleLogicalRange(range);
        }
      });
    }

    // Auto-resize handler
    const resizeObserver = new ResizeObserver(() => {
      if (mainContainer && this.chart) {
        this.chart.applyOptions({
          width: mainContainer.clientWidth,
          height: mainContainer.clientHeight || 460
        });
      }
      if (oscContainer && this.oscChart) {
        this.oscChart.applyOptions({
          width: oscContainer.clientWidth,
          height: oscContainer.clientHeight || 150
        });
      }
    });
    resizeObserver.observe(mainContainer);
    if (oscContainer) resizeObserver.observe(oscContainer);
  }

  async loadCatalog() {
    try {
      const res = await fetch("/api/forensics/catalog");
      if (!res.ok) throw new Error("Could not fetch catalog");
      this.catalog = await res.json();

      const runSelect = document.getElementById("forensicRunSelect");
      if (!runSelect) return;

      runSelect.innerHTML = "";
      if (this.catalog.available_runs.length === 0) {
        runSelect.innerHTML = "<option value=''>No backtest runs found</option>";
        return;
      }

      this.catalog.available_runs.forEach(r => {
        const opt = document.createElement("option");
        opt.value = r.run_id;
        opt.textContent = `[${r.symbol}] ${r.run_name || r.run_id} (${r.total_trades} trades, ${r.win_rate_pct.toFixed(1)}% WR)`;
        runSelect.appendChild(opt);
      });

      // Default to first run
      this.activeRunId = this.catalog.available_runs[0].run_id;
      await this.onRunChanged();
    } catch (e) {
      console.error("[!] Error loading catalog:", e);
    }
  }

  async loadRunCandles() {
    if (!this.activeRunId) return;
    try {
      const res = await fetch(`/api/forensics/run/${this.activeRunId}/candles?timeframe=${this.activeTimeframe}`);
      if (!res.ok) throw new Error("Could not fetch run candles");
      const data = await res.json();

      this.activeSymbol = data.symbol || this.activeSymbol;
      const candles = data.candles || [];
      const indicators = data.indicators || {};

      this.fullCandleData = candles.map(c => ({
        time: c.time,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close
      }));

      this.fullVolumeData = candles.map(c => ({
        time: c.time,
        value: c.volume,
        color: c.close >= c.open ? "rgba(0, 240, 144, 0.25)" : "rgba(255, 51, 102, 0.25)"
      }));

      this.fullIndicators = indicators;

      if (this.candleSeries) this.candleSeries.setData(this.fullCandleData);
      if (this.volumeSeries) this.volumeSeries.setData(this.fullVolumeData);
      if (this.fastEmaSeries && indicators.ema_fast) this.fastEmaSeries.setData(indicators.ema_fast);
      if (this.slowEmaSeries && indicators.ema_slow) this.slowEmaSeries.setData(indicators.ema_slow);
      if (this.stochKSeries && indicators.stoch_rsi) {
        this.stochKSeries.setData(indicators.stoch_rsi.k || []);
        this.stochDSeries.setData(indicators.stoch_rsi.d || []);
      }

      this.updateLegendWithLatest();
      this.scrollToLatest();
    } catch (e) {
      console.error("[!] Error loading run candles:", e);
    }
  }

  /* =========================================================================
     Chart Viewport Navigation & Framing
     ========================================================================= */

  fitChartContent() {
    if (!this.chart) return;
    try {
      this.chart.timeScale().fitContent();
    } catch (e) {
      console.warn("[!] fitContent warning:", e);
    }
  }

  scrollToLatest() {
    if (!this.chart || !this.fullCandleData || this.fullCandleData.length === 0) return;
    const totalBars = this.fullCandleData.length;
    try {
      this.chart.timeScale().setVisibleLogicalRange({
        from: Math.max(0, totalBars - 120),
        to: totalBars + 6
      });
    } catch (e) {
      console.warn("[!] scrollToLatest warning:", e);
    }
  }

  centerSelectedTrade() {
    if (this.tradeContext && this.tradeContext.trade) {
      this.focusTradeOnChart(this.tradeContext.trade);
    } else if (this.allTrades && this.allTrades.length > 0) {
      this.focusTradeOnChart(this.allTrades[0]);
    }
  }

  showChartBlur(title = "Loading Strategy Backtest...", subtitle = "Streaming OHLCV candlesticks & calculating indicators") {
    const overlay = document.getElementById("chartSwitchingOverlay");
    const tEl = document.getElementById("switchingTitle");
    if (overlay) {
      if (tEl) tEl.textContent = title;
      overlay.classList.remove("fade-out");
      overlay.style.display = "flex";
    }
  }

  hideChartBlur() {
    const overlay = document.getElementById("chartSwitchingOverlay");
    if (overlay) {
      overlay.classList.add("fade-out");
      setTimeout(() => {
        if (overlay.classList.contains("fade-out")) {
          overlay.style.display = "none";
        }
      }, 300);
    }
  }

  throttledUpdateMarkers() {
    if (this.markerUpdateTimeout) return;
    this.markerUpdateTimeout = setTimeout(() => {
      this.markerUpdateTimeout = null;
      if (this.showAllTrades && this.candleSeries && !this.isPlaying) {
        const markers = this.buildTradeMarkers(this.tradeContext ? this.tradeContext.trade : null);
        this.candleSeries.setMarkers(markers);
      }
    }, 120);
  }

  buildTradesTimeIndex() {
    this.tradesByOpenSec = new Map();
    this.tradesByCloseSec = new Map();

    if (!this.allTrades || this.allTrades.length === 0) return;

    for (const t of this.allTrades) {
      if (!t.open_time_ms) continue;
      const openSec = Math.floor(t.open_time_ms / 1000);
      const closeSec = t.close_time_ms ? Math.floor(t.close_time_ms / 1000) : openSec + 60;

      const openBar = Math.floor(openSec / 60) * 60;
      const closeBar = Math.floor(closeSec / 60) * 60;

      if (!this.tradesByOpenSec.has(openBar)) {
        this.tradesByOpenSec.set(openBar, []);
      }
      this.tradesByOpenSec.get(openBar).push(t);

      if (!this.tradesByCloseSec.has(closeBar)) {
        this.tradesByCloseSec.set(closeBar, []);
      }
      this.tradesByCloseSec.get(closeBar).push(t);
    }
  }

  showExecutionBanner(type, icon, text) {
    const banner = document.getElementById("replayExecutionBanner");
    const iconEl = document.getElementById("replayBannerIcon");
    const textEl = document.getElementById("replayBannerText");
    if (!banner) return;

    if (iconEl) iconEl.textContent = icon;
    if (textEl) textEl.textContent = text;
    banner.className = `replay-execution-banner ${type}`;
    banner.style.display = "flex";

    if (this.bannerTimeout) clearTimeout(this.bannerTimeout);
    this.bannerTimeout = setTimeout(() => {
      if (banner) banner.style.display = "none";
    }, 3800);
  }

  updateReplayPositionHUD(trade, candle) {
    const hud = document.getElementById("replayPositionHUD");
    const badgeEl = document.getElementById("replayPosBadge");
    const infoEl = document.getElementById("replayPosInfo");
    const pnlEl = document.getElementById("replayPosPnl");
    const roeEl = document.getElementById("replayPosRoe");
    if (!hud || !trade || !candle) return;

    hud.style.display = "flex";
    const isLong = trade.direction === "LONG";
    if (badgeEl) {
      badgeEl.textContent = `${trade.direction} #${trade.trade_id}`;
      badgeEl.className = `pos-badge ${trade.direction.toLowerCase()}`;
    }

    if (infoEl) {
      infoEl.textContent = `Entry: ${trade.entry_price} | Mark: ${candle.close.toFixed(4)}`;
    }

    const deltaPrice = (candle.close - trade.entry_price) * (isLong ? 1 : -1);
    const leverage = trade.leverage || 75;
    const roePct = trade.entry_price > 0 ? (deltaPrice / trade.entry_price) * 100 * leverage : 0;
    const estPnl = deltaPrice * (trade.contracts || 1);

    const sign = estPnl >= 0 ? "+" : "";
    if (pnlEl) {
      pnlEl.textContent = `${sign}${estPnl.toFixed(4)} USDT`;
      pnlEl.style.color = estPnl >= 0 ? "#00f090" : "#ff3366";
    }
    if (roeEl) {
      roeEl.textContent = `(${sign}${roePct.toFixed(2)}% ROE)`;
      roeEl.style.color = estPnl >= 0 ? "#00f090" : "#ff3366";
    }
  }

  hideReplayPositionHUD() {
    const hud = document.getElementById("replayPositionHUD");
    if (hud) hud.style.display = "none";
  }

  updateLegend(candle, fastEma, slowEma) {
    const ohlcEl = document.getElementById("legendOhlcRow");
    const indEl = document.getElementById("legendIndicatorsRow");
    if (!ohlcEl || !candle) return;

    const diff = candle.close - candle.open;
    const diffPct = candle.open > 0 ? (diff / candle.open * 100) : 0;
    const isUp = diff >= 0;
    const sign = isUp ? "+" : "";
    const color = isUp ? "#00f090" : "#ff3366";

    ohlcEl.innerHTML = `
      <span class="leg-sym">${this.activeSymbol}</span>
      <span class="leg-tf">${this.activeTimeframe}</span>
      <span class="leg-val">O <span style="color: ${color}">${candle.open.toFixed(4)}</span></span>
      <span class="leg-val">H <span style="color: ${color}">${candle.high.toFixed(4)}</span></span>
      <span class="leg-val">L <span style="color: ${color}">${candle.low.toFixed(4)}</span></span>
      <span class="leg-val">C <span style="color: ${color}">${candle.close.toFixed(4)}</span></span>
      <span class="leg-val"><span style="color: ${color}">(${sign}${diffPct.toFixed(2)}%)</span></span>
    `;

    if (indEl) {
      const fastStr = (fastEma !== null && fastEma !== undefined) ? fastEma.toFixed(4) : "--";
      const slowStr = (slowEma !== null && slowEma !== undefined) ? slowEma.toFixed(4) : "--";
      indEl.innerHTML = `
        <span class="leg-ind-fast">EMA Fast (5): ${fastStr}</span>
        <span class="leg-ind-slow">EMA Slow (13): ${slowStr}</span>
      `;
    }
  }

  updateLegendWithLatest() {
    if (!this.fullCandleData || this.fullCandleData.length === 0) return;
    const latestCandle = this.fullCandleData[this.fullCandleData.length - 1];
    let fastVal = null;
    let slowVal = null;
    if (this.fullIndicators) {
      const fastList = this.fullIndicators.ema_fast || [];
      const slowList = this.fullIndicators.ema_slow || [];
      if (fastList.length > 0) fastVal = fastList[fastList.length - 1].value;
      if (slowList.length > 0) slowVal = slowList[slowList.length - 1].value;
    }
    this.updateLegend(latestCandle, fastVal, slowVal);
  }

  onCrosshairMove(param) {
    if (!param || !param.time || !this.candleSeries) {
      this.updateLegendWithLatest();
      return;
    }
    const candle = param.seriesData.get(this.candleSeries);
    if (!candle) {
      this.updateLegendWithLatest();
      return;
    }
    let fastVal = null;
    let slowVal = null;
    if (this.fastEmaSeries) {
      const fastData = param.seriesData.get(this.fastEmaSeries);
      if (fastData && fastData.value !== undefined) fastVal = fastData.value;
    }
    if (this.slowEmaSeries) {
      const slowData = param.seriesData.get(this.slowEmaSeries);
      if (slowData && slowData.value !== undefined) slowVal = slowData.value;
    }
    this.updateLegend(candle, fastVal, slowVal);
  }

  async onRunChanged() {
    if (!this.activeRunId) return;
    this.showChartBlur(`Loading ${this.activeRunId}...`);

    try {
      // 1. First load complete backtest date range candles & indicators
      await this.loadRunCandles();

      // 2. Load all trades catalog
      const res = await fetch(`/api/forensics/run/${this.activeRunId}/trades-all`);
      if (!res.ok) throw new Error("Could not fetch complete trades catalog");
      const data = await res.json();

      this.allTrades = data.trades || [];
      this.tradeCounts = data.counts || { all: 0, wins: 0, losses: 0, timeouts: 0, with_ticks: 0 };
      this.buildTradesTimeIndex();

      // Update chip counts
      const elAll = document.getElementById("countAllTrades");
      const elWins = document.getElementById("countWinTrades");
      const elLoss = document.getElementById("countLossTrades");
      const elTime = document.getElementById("countTimeoutTrades");
      const elTick = document.getElementById("countTicksTrades");

      if (elAll) elAll.textContent = this.tradeCounts.all;
      if (elWins) elWins.textContent = this.tradeCounts.wins;
      if (elLoss) elLoss.textContent = this.tradeCounts.losses;
      if (elTime) elTime.textContent = this.tradeCounts.timeouts;
      if (elTick) elTick.textContent = this.tradeCounts.with_ticks;

      this.applyTradeFilter(this.activeTradeFilter, true);

      // Anchor viewport at the END DATE (recent candles) on initial load
      this.scrollToLatest();
    } catch (e) {
      console.error("[!] Error loading all trades for run:", e);
    } finally {
      this.hideChartBlur();
    }
  }

  applyTradeFilter(filterType, isInitial = false) {
    this.activeTradeFilter = filterType;

    if (filterType === "WIN") {
      this.filteredTrades = this.allTrades.filter(t => t.pnl_usdt > 0);
    } else if (filterType === "LOSS") {
      this.filteredTrades = this.allTrades.filter(t => t.pnl_usdt < 0);
    } else if (filterType === "TIMEOUT") {
      this.filteredTrades = this.allTrades.filter(t => (t.exit_reason || "").includes("TIMEOUT"));
    } else if (filterType === "TICKS") {
      this.filteredTrades = this.allTrades.filter(t => t.has_ticks === true);
    } else {
      this.filteredTrades = [...this.allTrades];
    }

    this.populateTradeSelect(isInitial);
  }

  populateTradeSelect(isInitial = false) {
    const tradeSelect = document.getElementById("forensicTradeSelect");
    if (!tradeSelect) return;

    tradeSelect.innerHTML = "";
    if (this.filteredTrades.length === 0) {
      tradeSelect.innerHTML = "<option value=''>No trades match filter</option>";
      return;
    }

    this.filteredTrades.forEach(t => {
      const opt = document.createElement("option");
      opt.value = t.trade_id;
      const pnlVal = Number(t.pnl_usdt || 0);
      const sign = pnlVal >= 0 ? "+" : "";
      const pnlStr = `${sign}${pnlVal.toFixed(4)} USDT`;
      const tickIcon = t.has_ticks ? "⚡" : "📊";
      opt.textContent = `${tickIcon} #${t.trade_id} | ${t.direction} | ${t.exit_reason} | ${pnlStr} (${t.date})`;
      tradeSelect.appendChild(opt);
    });

    const tradeExists = this.filteredTrades.some(t => t.trade_id === this.activeTradeId);
    if (!tradeExists || isInitial) {
      this.activeTradeId = this.filteredTrades[0].trade_id;
    }
    tradeSelect.value = this.activeTradeId;

    const jumpInput = document.getElementById("jumpTradeInput");
    if (jumpInput) jumpInput.value = this.activeTradeId;

    // When initializing run, don't auto-center chart on trade #1 (keeps end date visible)
    this.loadTrade(this.activeRunId, this.activeTradeId, !isInitial);
  }

  stepTrade(delta) {
    if (!this.filteredTrades || this.filteredTrades.length === 0) return;
    const currIdx = this.filteredTrades.findIndex(t => t.trade_id === this.activeTradeId);
    const nextIdx = Math.max(0, Math.min(this.filteredTrades.length - 1, (currIdx === -1 ? 0 : currIdx) + delta));
    this.activeTradeId = this.filteredTrades[nextIdx].trade_id;

    const tradeSelect = document.getElementById("forensicTradeSelect");
    if (tradeSelect) tradeSelect.value = this.activeTradeId;

    const jumpInput = document.getElementById("jumpTradeInput");
    if (jumpInput) jumpInput.value = this.activeTradeId;

    this.loadTrade(this.activeRunId, this.activeTradeId);
  }

  jumpToTrade(tradeId) {
    if (!tradeId || isNaN(tradeId)) return;
    const target = this.allTrades.find(t => t.trade_id === tradeId);
    if (!target) {
      alert(`Trade #${tradeId} was not found in backtest run ${this.activeRunId}.`);
      return;
    }

    // Check if in current filtered list
    const inFilter = this.filteredTrades.some(t => t.trade_id === tradeId);
    if (!inFilter) {
      // Reset filter chips to ALL
      document.querySelectorAll("[data-trade-filter]").forEach(c => {
        c.classList.toggle("active", c.dataset.tradeFilter === "ALL");
      });
      this.activeTradeFilter = "ALL";
      this.filteredTrades = [...this.allTrades];
      this.populateTradeSelect();
    }

    this.activeTradeId = tradeId;
    const tradeSelect = document.getElementById("forensicTradeSelect");
    if (tradeSelect) tradeSelect.value = tradeId;

    const jumpInput = document.getElementById("jumpTradeInput");
    if (jumpInput) jumpInput.value = tradeId;

    this.loadTrade(this.activeRunId, tradeId);
  }

  openAllTradesModal() {
    const modal = document.getElementById("allTradesModal");
    const titleEl = document.getElementById("modalRunTitle");
    if (!modal) return;

    if (titleEl) titleEl.textContent = this.activeRunId || "--";
    this.modalFilter = "ALL";
    document.querySelectorAll("[data-modal-filter]").forEach(c => {
      c.classList.toggle("active", c.dataset.modalFilter === "ALL");
    });
    const searchInput = document.getElementById("modalTradeSearch");
    if (searchInput) searchInput.value = "";

    this.renderModalTable();
    modal.style.display = "flex";
  }

  closeAllTradesModal() {
    const modal = document.getElementById("allTradesModal");
    if (modal) modal.style.display = "none";
  }

  renderModalTable() {
    const tbody = document.getElementById("allTradesTableBody");
    const countEl = document.getElementById("modalTradesCount");
    const searchInput = document.getElementById("modalTradeSearch");
    if (!tbody) return;

    const query = (searchInput ? searchInput.value.trim().toLowerCase() : "");

    let list = this.allTrades;
    if (this.modalFilter === "WIN") {
      list = list.filter(t => t.pnl_usdt > 0);
    } else if (this.modalFilter === "LOSS") {
      list = list.filter(t => t.pnl_usdt < 0);
    } else if (this.modalFilter === "TIMEOUT") {
      list = list.filter(t => (t.exit_reason || "").includes("TIMEOUT"));
    } else if (this.modalFilter === "TICKS") {
      list = list.filter(t => t.has_ticks === true);
    }

    if (query) {
      list = list.filter(t => 
        String(t.trade_id).includes(query) ||
        (t.time_utc || "").toLowerCase().includes(query) ||
        (t.direction || "").toLowerCase().includes(query) ||
        (t.exit_reason || "").toLowerCase().includes(query) ||
        String(t.pnl_usdt).includes(query)
      );
    }

    if (countEl) countEl.textContent = `Showing ${list.length} of ${this.allTrades.length} trades`;

    // Display first 300 to maintain silky smooth 60fps
    const displayList = list.slice(0, 300);
    tbody.innerHTML = "";

    displayList.forEach(t => {
      const tr = document.createElement("tr");
      const isWin = t.pnl_usdt > 0;
      const isTimeout = (t.exit_reason || "").includes("TIMEOUT");
      const pnlClass = isWin ? "profit mono" : (t.pnl_usdt < 0 ? "loss mono" : "mono");
      const reasonBadgeClass = isWin ? "profit" : (isTimeout ? "timeout" : "loss");
      const sign = t.pnl_usdt >= 0 ? "+" : "";

      tr.innerHTML = `
        <td class="mono" style="font-weight: 700; color: var(--accent-cyan);">#${t.trade_id}</td>
        <td class="mono" style="font-size: 0.75rem;">${t.time_utc}</td>
        <td><span class="status-chip ${t.direction.toLowerCase()}">${t.direction}</span></td>
        <td class="mono">${t.duration_s}s</td>
        <td><span class="status-chip ${reasonBadgeClass}">${t.exit_reason}</span></td>
        <td class="${pnlClass}">${sign}${t.pnl_usdt.toFixed(4)} USDT</td>
        <td class="${pnlClass}">${sign}${t.roe_pct.toFixed(2)}%</td>
        <td>
          <span class="data-feed-badge ${t.has_ticks ? 'high-res' : 'candle-res'}">
            ${t.has_ticks ? '⚡ Millisecond Ticks' : '📊 1m Candle'}
          </span>
        </td>
        <td>
          <button class="btn btn-secondary btn-sm" onclick="window.forensicsLab.jumpToTrade(${t.trade_id}); window.forensicsLab.closeAllTradesModal();">
            Analyze 🔎
          </button>
        </td>
      `;
      tbody.appendChild(tr);
    });

    if (list.length > 300) {
      const trNotice = document.createElement("tr");
      trNotice.innerHTML = `<td colspan="9" style="text-align: center; color: var(--text-dim); padding: 0.75rem;">
        Displaying first 300 matching trades. Use search bar to narrow down further.
      </td>`;
      tbody.appendChild(trNotice);
    }
  }

  async loadTrade(runId, tradeId, autoCenter = true) {
    if (!runId || !tradeId) return;
    this.activeRunId = runId;
    this.activeTradeId = tradeId;

    // Reset replay state
    this.pauseReplay();
    this.currentTickIndex = 0;

    const overlay = document.getElementById("forensicLoadingOverlay");
    if (overlay) overlay.style.display = "flex";

    try {
      const hasFullCandles = (this.fullCandleData && this.fullCandleData.length > 0);
      const incParam = hasFullCandles ? "include_candles=false" : "include_candles=true";
      const res = await fetch(`/api/forensics/trade/${runId}/${tradeId}?timeframe=${this.activeTimeframe}&${incParam}`);
      if (!res.ok) throw new Error(`Trade #${tradeId} context fetch failed: ${res.statusText}`);
      this.tradeContext = await res.json();

      // If full candle data is already loaded for the run, just overlay this trade
      if (hasFullCandles) {
        this.renderTradeOverlay(autoCenter);
      } else {
        this.renderChartData(autoCenter);
      }

      this.renderTradeInspector();
      this.initReplaySlider();
    } catch (e) {
      console.error("[!] Failed loading forensic trade context:", e);
      alert(`Could not load forensic data for trade #${tradeId}: ${e.message}`);
    } finally {
      if (overlay) overlay.style.display = "none";
    }
  }

  renderChartData(autoCenter = true) {
    if (!this.tradeContext || !this.candleSeries) return;
    const { trade, candles, indicators } = this.tradeContext;

    // 1. Candlesticks
    const candleData = (candles || []).map(c => ({
      time: c.time,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close
    }));
    this.candleSeries.setData(candleData);

    // Store full data for chart replay
    this.fullCandleData = candleData;
    this.fullVolumeData = (candles || []).map(c => ({
      time: c.time,
      value: c.volume,
      color: c.close >= c.open ? "rgba(0, 240, 144, 0.25)" : "rgba(255, 51, 102, 0.25)"
    }));
    this.fullIndicators = indicators;

    // 2. Volumes
    if (this.volumeSeries) {
      this.volumeSeries.setData(this.fullVolumeData);
    }

    // 3. EMAs
    if (this.fastEmaSeries && indicators && indicators.ema_fast) {
      this.fastEmaSeries.setData(indicators.ema_fast);
    }
    if (this.slowEmaSeries && indicators.ema_slow) {
      this.slowEmaSeries.setData(indicators.ema_slow);
    }

    // 4. Stoch RSI
    if (this.stochKSeries && indicators && indicators.stoch_rsi) {
      this.stochKSeries.setData(indicators.stoch_rsi.k || []);
      this.stochDSeries.setData(indicators.stoch_rsi.d || []);
    }

    this.renderTradeOverlay(autoCenter);
    this.updateLegendWithLatest();
  }

  renderTradeOverlay(autoCenter = true) {
    if (!this.tradeContext || !this.candleSeries) return;
    const { trade } = this.tradeContext;

    // Clean up old price lines
    this.priceLines.forEach(pl => this.candleSeries.removePriceLine(pl));
    this.priceLines = [];
    if (this.chartReplayPriceLine) {
      try { this.candleSeries.removePriceLine(this.chartReplayPriceLine); } catch(e) {}
      this.chartReplayPriceLine = null;
    }

    // Draw Entry, TP, and SL Price Lines
    const entryLine = this.candleSeries.createPriceLine({
      price: trade.entry_price,
      color: "#00d2ff",
      lineWidth: 1.5,
      lineStyle: LightweightCharts.LineStyle.Solid,
      axisLabelVisible: true,
      title: `ENTRY (${trade.entry_price})`
    });
    this.priceLines.push(entryLine);

    if (trade.tp_price > 0) {
      const tpLine = this.candleSeries.createPriceLine({
        price: trade.tp_price,
        color: "#00f090",
        lineWidth: 1.5,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: `TP (${trade.tp_price})`
      });
      this.priceLines.push(tpLine);
    }

    if (trade.sl_price > 0) {
      const slLine = this.candleSeries.createPriceLine({
        price: trade.sl_price,
        color: "#ff3366",
        lineWidth: 1.5,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: `SL (${trade.sl_price})`
      });
      this.priceLines.push(slLine);
    }

    // Build markers — current trade entry/exit + all trades overlay
    const markers = this.buildTradeMarkers(trade);
    this.candleSeries.setMarkers(markers);

    // Focus viewport on trade without discarding full backtest data if autoCenter requested
    if (autoCenter) {
      this.focusTradeOnChart(trade);
    }
  }

  focusTradeOnChart(trade) {
    if (!trade || !this.chart || !this.fullCandleData || this.fullCandleData.length === 0) return;
    const entrySec = Math.floor(trade.open_time_ms / 1000);
    let entryIdx = this.fullCandleData.findIndex(c => c.time >= entrySec);
    if (entryIdx === -1) entryIdx = this.fullCandleData.length - 1;

    const fromIdx = Math.max(0, entryIdx - 40);
    const toIdx = Math.min(this.fullCandleData.length - 1, entryIdx + 60);

    try {
      this.chart.timeScale().setVisibleLogicalRange({ from: fromIdx, to: toIdx });
    } catch (e) {
      this.chart.timeScale().fitContent();
    }
  }

  /**
   * Builds a sorted array of chart markers including the active trade's
   * entry/exit and optionally all other trades in the run.
   */
  buildTradeMarkers(activeTrade) {
    const markers = [];

    if (activeTrade && activeTrade.open_time_ms) {
      const entryTimeSec = Math.floor(activeTrade.open_time_ms / 1000);
      const exitTimeSec = Math.floor(activeTrade.close_time_ms / 1000);
      const isLong = activeTrade.direction === "LONG";

      // Active trade markers (prominent)
      markers.push({
        time: entryTimeSec,
        position: isLong ? "belowBar" : "aboveBar",
        color: "#00d2ff",
        shape: isLong ? "arrowUp" : "arrowDown",
        text: `${activeTrade.direction} ENTRY @ ${activeTrade.entry_price}`
      });

      const isWin = activeTrade.realized_pnl_usdt > 0;
      const isTimeout = (activeTrade.exit_reason || "").includes("TIMEOUT");
      const exitColor = isWin ? "#00f090" : (isTimeout ? "#ffb800" : "#ff3366");

      markers.push({
        time: exitTimeSec,
        position: isLong ? "aboveBar" : "belowBar",
        color: exitColor,
        shape: "circle",
        text: `EXIT: ${activeTrade.exit_reason} (${activeTrade.exit_price})`
      });
    }

    // All-trades overlay markers (virtualized by visible time range for smooth 60fps)
    if (this.showAllTrades && this.allTrades && this.allTrades.length > 0) {
      let minTime = 0;
      let maxTime = Infinity;

      if (this.chart) {
        try {
          const visRange = this.chart.timeScale().getVisibleRange();
          if (visRange && visRange.from && visRange.to) {
            const span = visRange.to - visRange.from;
            minTime = visRange.from - span * 0.4;
            maxTime = visRange.to + span * 0.4;
          }
        } catch (e) {}
      }

      let count = 0;
      for (const t of this.allTrades) {
        if (activeTrade && t.trade_id === activeTrade.trade_id) continue;
        if (!t.open_time_ms || !t.close_time_ms) continue;

        const tEntrySec = Math.floor(t.open_time_ms / 1000);
        const tExitSec = Math.floor(t.close_time_ms / 1000);

        if ((tEntrySec < minTime || tEntrySec > maxTime) && (tExitSec < minTime || tExitSec > maxTime)) {
          continue;
        }

        const tIsLong = t.direction === "LONG";
        const tIsWin = t.pnl_usdt > 0;
        const tIsTimeout = (t.exit_reason || "").includes("TIMEOUT");

        if (tEntrySec >= minTime && tEntrySec <= maxTime) {
          markers.push({
            time: tEntrySec,
            position: tIsLong ? "belowBar" : "aboveBar",
            color: tIsLong ? "rgba(0, 210, 255, 0.55)" : "rgba(199, 125, 255, 0.55)",
            shape: tIsLong ? "arrowUp" : "arrowDown",
            text: `#${t.trade_id} ${t.direction}`
          });
          count++;
        }

        if (tExitSec >= minTime && tExitSec <= maxTime) {
          const tExitColor = tIsWin ? "rgba(0, 240, 144, 0.55)" : (tIsTimeout ? "rgba(255, 184, 0, 0.55)" : "rgba(255, 51, 102, 0.55)");
          markers.push({
            time: tExitSec,
            position: tIsLong ? "aboveBar" : "belowBar",
            color: tExitColor,
            shape: "circle",
            text: `#${t.trade_id} ${t.exit_reason}`
          });
          count++;
        }

        if (count > 200) break;
      }
    }

    // Lightweight Charts requires markers sorted by time
    markers.sort((a, b) => a.time - b.time);
    return markers;
  }

  /**
   * Re-renders all-trade markers when toggle changes.
   */
  renderAllTradesMarkers() {
    if (!this.tradeContext || !this.candleSeries) return;
    const markers = this.buildTradeMarkers(this.tradeContext.trade);
    this.candleSeries.setMarkers(markers);
  }

  renderTradeInspector() {
    if (!this.tradeContext) return;
    const { trade, mfe_mae, filter_state, post_exit, strategy_state, timeline } = this.tradeContext;

    // Header & Badges
    document.getElementById("inspTradeId").textContent = `#${trade.trade_id}`;
    
    const feedBadge = document.getElementById("inspFeedBadge");
    if (feedBadge) {
      if (this.tradeContext.is_synthetic) {
        feedBadge.className = "data-feed-badge candle-res";
        feedBadge.textContent = "📊 1m Candle Fallback";
        feedBadge.title = "Raw tick files not present for this trade's date window. Forensic replay evaluated against 1m candles.";
      } else {
        const tickCount = (this.tradeContext.ticks || []).length;
        feedBadge.className = "data-feed-badge high-res";
        feedBadge.textContent = `⚡ Raw Binance Ticks (${tickCount})`;
        feedBadge.title = "Millisecond-accurate raw Binance tick data loaded from local disk.";
      }
    }

    const dirBadge = document.getElementById("inspDirectionBadge");
    dirBadge.textContent = trade.direction;
    dirBadge.className = `status-chip ${trade.direction.toLowerCase()}`;

    const exitBadge = document.getElementById("inspExitReasonBadge");
    exitBadge.textContent = trade.exit_reason;
    if (trade.realized_pnl_usdt > 0) {
      exitBadge.className = "status-chip profit";
    } else if (trade.exit_reason.includes("TIMEOUT")) {
      exitBadge.className = "status-chip timeout";
    } else {
      exitBadge.className = "status-chip loss";
    }

    // Execution Prices
    document.getElementById("inspEntryPrice").textContent = trade.entry_price;
    document.getElementById("inspExitPrice").textContent = trade.exit_price;
    document.getElementById("inspTpPrice").textContent = trade.tp_price;
    document.getElementById("inspSlPrice").textContent = trade.sl_price;

    // Performance
    const pnlEl = document.getElementById("inspPnl");
    const sign = trade.realized_pnl_usdt >= 0 ? "+" : "";
    pnlEl.textContent = `${sign}${trade.realized_pnl_usdt.toFixed(4)} USDT`;
    pnlEl.className = trade.realized_pnl_usdt >= 0 ? "stat-val profit mono" : "stat-val loss mono";

    const roeEl = document.getElementById("inspRoe");
    roeEl.textContent = `${sign}${trade.roe_percentage.toFixed(2)}%`;
    roeEl.className = trade.roe_percentage >= 0 ? "stat-val profit mono" : "stat-val loss mono";

    document.getElementById("inspDuration").textContent = `${trade.duration_seconds.toFixed(1)}s`;
    document.getElementById("inspOpenTime").textContent = trade.open_time_utc;
    document.getElementById("inspCloseTime").textContent = trade.close_time_utc;

    // MFE / MAE
    if (mfe_mae) {
      document.getElementById("inspMfe").textContent = `+${mfe_mae.mfe_ticks} ticks (+${mfe_mae.mfe_pct}%)`;
      document.getElementById("inspMae").textContent = `-${mfe_mae.mae_ticks} ticks (-${mfe_mae.mae_pct}%)`;
    }

    // Filters
    const filterContainer = document.getElementById("inspFilterContainer");
    if (filterContainer && filter_state) {
      filterContainer.innerHTML = "";
      Object.entries(filter_state).forEach(([k, f]) => {
        const item = document.createElement("div");
        item.className = "filter-badge-item";
        const statusClass = f.status === "PASS" ? "filter-pass" : (f.status === "FAIL" ? "filter-fail" : "filter-disabled");
        item.innerHTML = `
          <span class="filter-name">${f.name}</span>
          <span class="filter-status ${statusClass}">${f.status}</span>
        `;
        filterContainer.appendChild(item);
      });
    }

    // Post Exit Summary
    const postExitEl = document.getElementById("inspPostExitSummary");
    if (postExitEl && post_exit) {
      postExitEl.textContent = post_exit.summary || "No post-exit activity recorded.";
      if (post_exit.tp_reached_after_exit) {
        postExitEl.className = "post-exit-box profit-glow";
      } else if (post_exit.sl_reached_after_exit) {
        postExitEl.className = "post-exit-box loss-glow";
      } else {
        postExitEl.className = "post-exit-box";
      }
    }

    // Timeline List
    const timelineContainer = document.getElementById("inspTimelineContainer");
    if (timelineContainer && timeline) {
      timelineContainer.innerHTML = "";
      timeline.forEach(t => {
        const row = document.createElement("div");
        row.className = "timeline-event-row";
        const deltaClass = t.delta_ticks > 0 ? "profit" : (t.delta_ticks < 0 ? "loss" : "");
        row.innerHTML = `
          <div class="tl-time">${t.elapsed_sec.toFixed(1)}s</div>
          <div class="tl-desc">${t.desc}</div>
          <div class="tl-price mono ${deltaClass}">${t.price} (${t.delta_ticks >= 0 ? "+" : ""}${t.delta_ticks}t)</div>
        `;
        timelineContainer.appendChild(row);
      });
    }
  }

  // =========================================================================
  // REPLAY ENGINE (Tick replay + Live Chart Replay)
  // =========================================================================

  initReplaySlider() {
    const slider = document.getElementById("replayScrubSlider");
    const ticks = (this.tradeContext && this.tradeContext.ticks) ? this.tradeContext.ticks : [];
    if (slider) {
      slider.min = 0;
      slider.max = Math.max(0, ticks.length - 1);
      slider.value = 0;
    }
    this.replayMode = "tick";
    this.updateReplayDisplay(0);
  }

  playReplay() {
    if (this.replayMode === "chart") {
      this.playChartReplay();
      return;
    }

    const ticks = (this.tradeContext && this.tradeContext.ticks) ? this.tradeContext.ticks : [];
    if (ticks.length === 0) return;

    // If at end, rewind to beginning
    if (this.currentTickIndex >= ticks.length - 1) {
      this.currentTickIndex = 0;
      this.seekReplay(0);
    }

    this.isPlaying = true;
    const btnPlay = document.getElementById("btnReplayPlay");
    const btnPause = document.getElementById("btnReplayPause");
    if (btnPlay) btnPlay.style.display = "none";
    if (btnPause) btnPause.style.display = "inline-flex";

    const stepMs = Math.max(30, Math.floor(600 / this.replaySpeed));
    this.replayInterval = setInterval(() => {
      if (this.currentTickIndex >= ticks.length - 1) {
        this.pauseReplay();
        if (btnPlay) btnPlay.innerHTML = "<span>🔄</span> Replay";
        return;
      }
      this.currentTickIndex++;
      this.seekReplay(this.currentTickIndex);
    }, stepMs);
  }

  pauseReplay() {
    this.isPlaying = false;
    if (this.replayInterval) {
      clearInterval(this.replayInterval);
      this.replayInterval = null;
    }
    const btnPlay = document.getElementById("btnReplayPlay");
    const btnPause = document.getElementById("btnReplayPause");
    if (btnPlay) {
      btnPlay.style.display = "inline-flex";
      btnPlay.innerHTML = "<span>▶</span> Play";
    }
    if (btnPause) btnPause.style.display = "none";

    // Update chart replay button state
    const btnChartReplay = document.getElementById("btnChartReplay");
    if (btnChartReplay && this.replayMode === "chart") {
      btnChartReplay.textContent = "▶ Chart Replay";
      btnChartReplay.classList.remove("active-pulse");
    }
  }

  resetReplay() {
    this.pauseReplay();
    this.replayActivePosition = null;
    this.hideReplayPositionHUD();
    const banner = document.getElementById("replayExecutionBanner");
    if (banner) banner.style.display = "none";
    const lastEventText = document.getElementById("replayLastEventText");
    if (lastEventText) lastEventText.textContent = "Replay Reset. Ready.";

    if (this.replayMode === "chart") {
      const resetIdx = this.getInitialReplayIndex();
      this.chartReplayIndex = resetIdx;
      this.renderChartReplayFrame(resetIdx);
      const slider = document.getElementById("replayScrubSlider");
      if (slider) slider.value = resetIdx;
      return;
    }

    this.seekReplay(0);
  }

  stepReplay(delta) {
    if (this.replayMode === "chart") {
      this.pauseReplay();
      this.chartReplayIndex = Math.max(0, Math.min(this.fullCandleData.length - 1, this.chartReplayIndex + delta));
      this.renderChartReplayFrame(this.chartReplayIndex);
      const slider = document.getElementById("replayScrubSlider");
      if (slider) slider.value = this.chartReplayIndex;
      return;
    }

    this.pauseReplay();
    const ticks = (this.tradeContext && this.tradeContext.ticks) ? this.tradeContext.ticks : [];
    if (ticks.length === 0) return;
    const nextIdx = Math.max(0, Math.min(ticks.length - 1, this.currentTickIndex + delta));
    this.seekReplay(nextIdx);
  }

  seekReplay(index) {
    if (this.replayMode === "chart") {
      this.chartReplayIndex = index;
      const slider = document.getElementById("replayScrubSlider");
      if (slider) slider.value = index;
      this.renderChartReplayFrame(index);
      return;
    }

    this.currentTickIndex = index;
    const slider = document.getElementById("replayScrubSlider");
    if (slider) slider.value = index;
    this.updateReplayDisplay(index);
  }

  updateReplayDisplay(index) {
    const ticks = (this.tradeContext && this.tradeContext.ticks) ? this.tradeContext.ticks : [];
    if (ticks.length === 0 || !ticks[index]) return;

    const tick = ticks[index];
    const trade = this.tradeContext.trade;
    const elapsedSec = ((tick.time_ms - trade.open_time_ms) / 1000.0).toFixed(1);

    document.getElementById("replayCurrentPrice").textContent = tick.price.toFixed(4);
    document.getElementById("replayElapsedSec").textContent = `${elapsedSec}s`;

    // Live Unrealized PnL & Distance to TP/SL
    const isLong = trade.direction === "LONG";
    const deltaTicks = tick.delta_ticks;
    const deltaEl = document.getElementById("replayCurrentDelta");
    if (deltaEl) {
      deltaEl.textContent = `${deltaTicks >= 0 ? "+" : ""}${deltaTicks} ticks`;
      deltaEl.className = deltaTicks >= 0 ? "mono profit" : "mono loss";
    }

    const toTp = isLong ? (trade.tp_price - tick.price) : (tick.price - trade.tp_price);
    const toSl = isLong ? (tick.price - trade.sl_price) : (trade.sl_price - tick.price);
    const pu = this.tradeContext.strategy_state.price_unit || 0.0001;

    document.getElementById("replayToTp").textContent = `${(toTp / pu).toFixed(1)}t`;
    document.getElementById("replayToSl").textContent = `${(toSl / pu).toFixed(1)}t`;

    const inPosPill = document.getElementById("replayInPositionPill");
    if (inPosPill) {
      if (tick.is_in_position) {
        inPosPill.textContent = "IN POSITION";
        inPosPill.className = "status-chip active-pulse";
      } else {
        inPosPill.textContent = "POST-EXIT";
        inPosPill.className = "status-chip neutral";
      }
    }
  }

  // =========================================================================
  // LIVE CHART REPLAY (TradingView-style progressive candle reveal)
  // =========================================================================

  getInitialReplayIndex() {
    if (this.scissorCutIndex !== null) return this.scissorCutIndex;
    if (!this.fullCandleData || this.fullCandleData.length === 0) return 0;
    if (!this.tradeContext || !this.tradeContext.trade) return 0;
    const entryTimeSec = Math.floor(this.tradeContext.trade.open_time_ms / 1000);
    for (let i = 0; i < this.fullCandleData.length; i++) {
      if (this.fullCandleData[i].time >= entryTimeSec) {
        return Math.max(0, i - 15);
      }
    }
    return 0;
  }

  startChartReplay() {
    if (!this.fullCandleData || this.fullCandleData.length === 0) return;

    // If already in chart replay mode and playing, just pause
    if (this.replayMode === "chart" && this.isPlaying) {
      this.pauseReplay();
      return;
    }

    // If already in chart replay mode and paused, resume from current position
    if (this.replayMode === "chart" && !this.isPlaying) {
      this.playChartReplay();
      return;
    }

    // Fresh start: switch to chart replay mode
    this.pauseReplay();
    this.replayMode = "chart";

    const startIdx = this.getInitialReplayIndex();
    this.chartReplayIndex = startIdx;

    // Update scrub slider to use candle count
    const slider = document.getElementById("replayScrubSlider");
    if (slider) {
      slider.min = 0;
      slider.max = this.fullCandleData.length - 1;
      slider.value = startIdx;
    }

    // Render initial frame and start playing
    this.renderChartReplayFrame(startIdx);
    this.playChartReplay();
  }

  playChartReplay() {
    if (!this.fullCandleData || this.fullCandleData.length === 0) return;

    // If at end, rewind to start point and replay
    if (this.chartReplayIndex >= this.fullCandleData.length - 1) {
      const startIdx = this.getInitialReplayIndex();
      this.chartReplayIndex = startIdx;
      this.renderChartReplayFrame(startIdx);
      const slider = document.getElementById("replayScrubSlider");
      if (slider) slider.value = startIdx;
    }

    this.isPlaying = true;

    const btnPlay = document.getElementById("btnReplayPlay");
    const btnPause = document.getElementById("btnReplayPause");
    if (btnPlay) btnPlay.style.display = "none";
    if (btnPause) btnPause.style.display = "inline-flex";

    const btnChartReplay = document.getElementById("btnChartReplay");
    if (btnChartReplay) {
      btnChartReplay.textContent = "⏸ Pause Replay";
      btnChartReplay.classList.add("active-pulse");
    }

    // Calibrated speed mapping: 1x = 1000ms (1 full second per candle), 0.5x = 2000ms
    const speedMsMap = {
      0.5: 2000,
      1: 1000,
      2: 500,
      5: 200,
      10: 100,
      25: 50,
      50: 25
    };
    const stepMs = speedMsMap[this.replaySpeed] || Math.max(25, Math.floor(1000 / this.replaySpeed));

    this.replayInterval = setInterval(() => {
      if (this.chartReplayIndex >= this.fullCandleData.length - 1) {
        this.pauseReplay();
        if (btnChartReplay) {
          btnChartReplay.textContent = "🔄 Replay Again";
          btnChartReplay.classList.remove("active-pulse");
        }
        if (btnPlay) {
          btnPlay.innerHTML = "<span>🔄</span> Replay";
        }
        return;
      }
      this.chartReplayIndex++;
      this.renderChartReplayFrame(this.chartReplayIndex);

      // Update scrub slider
      const slider = document.getElementById("replayScrubSlider");
      if (slider) slider.value = this.chartReplayIndex;
    }, stepMs);
  }

  renderChartReplayFrame(upToIndex) {
    const slicedCandles = this.fullCandleData.slice(0, upToIndex + 1);
    const slicedVolumes = this.fullVolumeData.slice(0, upToIndex + 1);

    // Set candle + volume data up to current index
    this.candleSeries.setData(slicedCandles);
    if (this.volumeSeries) this.volumeSeries.setData(slicedVolumes);

    const maxTime = slicedCandles[slicedCandles.length - 1]?.time || 0;

    // Slice indicators to match
    if (this.fullIndicators) {
      if (this.fastEmaSeries && this.fullIndicators.ema_fast) {
        this.fastEmaSeries.setData(this.fullIndicators.ema_fast.filter(d => d.time <= maxTime));
      }
      if (this.slowEmaSeries && this.fullIndicators.ema_slow) {
        this.slowEmaSeries.setData(this.fullIndicators.ema_slow.filter(d => d.time <= maxTime));
      }
      if (this.stochKSeries && this.fullIndicators.stoch_rsi) {
        this.stochKSeries.setData((this.fullIndicators.stoch_rsi.k || []).filter(d => d.time <= maxTime));
        this.stochDSeries.setData((this.fullIndicators.stoch_rsi.d || []).filter(d => d.time <= maxTime));
      }
    }

    // Moving live price line
    const currentCandle = slicedCandles[slicedCandles.length - 1];
    if (currentCandle) {
      if (this.chartReplayPriceLine) {
        try { this.candleSeries.removePriceLine(this.chartReplayPriceLine); } catch(e) {}
      }

      this.chartReplayPriceLine = this.candleSeries.createPriceLine({
        price: currentCandle.close,
        color: currentCandle.close >= currentCandle.open ? "#00f090" : "#ff3366",
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dotted,
        axisLabelVisible: true,
        title: "LIVE"
      });

      // Gently maintain visibility ONLY if candle moves beyond right edge of visible range
      // Does NOT aggressively reset or snap user's manual layout
      try {
        const visibleRange = this.chart.timeScale().getVisibleLogicalRange();
        if (visibleRange && upToIndex >= visibleRange.to - 2) {
          const span = visibleRange.to - visibleRange.from;
          this.chart.timeScale().setVisibleLogicalRange({
            from: upToIndex - span + 2,
            to: upToIndex + 2
          });
        }
      } catch (e) {}

      // Update top-left HUD legend for current candle
      let curFast = null;
      let curSlow = null;
      if (this.fullIndicators) {
        const fastPoints = (this.fullIndicators.ema_fast || []).filter(d => d.time <= maxTime);
        const slowPoints = (this.fullIndicators.ema_slow || []).filter(d => d.time <= maxTime);
        if (fastPoints.length > 0) curFast = fastPoints[fastPoints.length - 1].value;
        if (slowPoints.length > 0) curSlow = slowPoints[slowPoints.length - 1].value;
      }
      this.updateLegend(currentCandle, curFast, curSlow);
    }

    // --- Replay Trade Events & Live Position Tracking ---
    const curTime = currentCandle ? currentCandle.time : 0;
    const prevTime = upToIndex > 0 && this.fullCandleData[upToIndex - 1] ? this.fullCandleData[upToIndex - 1].time : (curTime - 60);

    const openedTrades = [];
    const closedTrades = [];
    let activeTradeForBar = null;

    // Check active inspected trade if present
    if (this.tradeContext && this.tradeContext.trade) {
      const t = this.tradeContext.trade;
      const tOpenSec = Math.floor(t.open_time_ms / 1000);
      const tCloseSec = Math.floor(t.close_time_ms / 1000);

      if (tOpenSec > prevTime && tOpenSec <= curTime) {
        openedTrades.push(t);
      }
      if (tCloseSec > prevTime && tCloseSec <= curTime) {
        closedTrades.push(t);
      }
      if (curTime >= tOpenSec && curTime <= tCloseSec) {
        activeTradeForBar = t;
      }
    }

    // Also check global indexed trades across the entire backtest
    if (this.tradesByOpenSec) {
      for (const [openBar, trades] of this.tradesByOpenSec.entries()) {
        if (openBar > prevTime && openBar <= curTime) {
          for (const t of trades) {
            if (!openedTrades.some(x => x.trade_id === t.trade_id)) openedTrades.push(t);
          }
        }
      }
    }
    if (this.tradesByCloseSec) {
      for (const [closeBar, trades] of this.tradesByCloseSec.entries()) {
        if (closeBar > prevTime && closeBar <= curTime) {
          for (const t of trades) {
            if (!closedTrades.some(x => x.trade_id === t.trade_id)) closedTrades.push(t);
          }
        }
      }
    }

    // Trigger Execution Notifications & Update Last Event Box
    const lastEventText = document.getElementById("replayLastEventText");

    for (const ot of openedTrades) {
      this.showExecutionBanner("entry", "🔔", `ENTERED #${ot.trade_id} ${ot.direction} @ ${ot.entry_price}`);
      if (lastEventText) {
        lastEventText.innerHTML = `<span style="color: var(--accent-cyan); font-weight: 600;">🔔 Entered #${ot.trade_id} ${ot.direction} @ ${ot.entry_price}</span>`;
      }
      this.replayActivePosition = ot;
    }

    for (const ct of closedTrades) {
      const pnlVal = ct.realized_pnl_usdt !== undefined ? ct.realized_pnl_usdt : (ct.pnl_usdt || 0);
      const isWin = pnlVal > 0;
      const sign = pnlVal >= 0 ? "+" : "";
      const bannerType = isWin ? "win-exit" : "loss-exit";
      const icon = isWin ? "🎯" : "🏁";
      const pnlText = `${sign}${Number(pnlVal).toFixed(4)} USDT`;
      this.showExecutionBanner(bannerType, icon, `CLOSED #${ct.trade_id} ${pnlText} (${ct.exit_reason || "EXIT"})`);
      if (lastEventText) {
        const color = isWin ? "var(--accent-green)" : "var(--accent-red)";
        lastEventText.innerHTML = `<span style="color: ${color}; font-weight: 600;">${icon} Closed #${ct.trade_id} ${pnlText} (${ct.exit_reason || "EXIT"})</span>`;
      }
      if (this.replayActivePosition && this.replayActivePosition.trade_id === ct.trade_id) {
        this.replayActivePosition = null;
      }
    }

    // Determine active position for HUD (supporting scrub, jumps, and continuous play)
    if (activeTradeForBar) {
      this.replayActivePosition = activeTradeForBar;
    } else if (this.allTrades && this.allTrades.length > 0 && !this.replayActivePosition) {
      const matching = this.allTrades.find(t => {
        const o = Math.floor(t.open_time_ms / 1000);
        const c = Math.floor((t.close_time_ms || t.open_time_ms) / 1000);
        return curTime >= o && curTime <= c;
      });
      if (matching) this.replayActivePosition = matching;
    }

    // Update or hide HUD
    if (this.replayActivePosition && currentCandle) {
      this.updateReplayPositionHUD(this.replayActivePosition, currentCandle);
    } else {
      this.hideReplayPositionHUD();
    }

    // Build markers only for candles revealed so far
    const markers = [];

    if (this.tradeContext && this.tradeContext.trade) {
      const trade = this.tradeContext.trade;
      const entryTimeSec = Math.floor(trade.open_time_ms / 1000);
      const exitTimeSec = Math.floor(trade.close_time_ms / 1000);
      const isLong = trade.direction === "LONG";

      // Show entry marker only if revealed past it
      if (maxTime >= entryTimeSec) {
        markers.push({
          time: entryTimeSec,
          position: isLong ? "belowBar" : "aboveBar",
          color: "#00d2ff",
          shape: isLong ? "arrowUp" : "arrowDown",
          text: `${trade.direction} ENTRY @ ${trade.entry_price}`
        });
      }

      // Show exit marker only if revealed past it
      if (maxTime >= exitTimeSec) {
        const pnl = trade.realized_pnl_usdt !== undefined ? trade.realized_pnl_usdt : (trade.pnl_usdt || 0);
        const isWin = pnl > 0;
        const isTimeout = (trade.exit_reason || "").includes("TIMEOUT");
        const exitColor = isWin ? "#00f090" : (isTimeout ? "#ffb800" : "#ff3366");
        markers.push({
          time: exitTimeSec,
          position: isLong ? "aboveBar" : "belowBar",
          color: exitColor,
          shape: "circle",
          text: `EXIT: ${trade.exit_reason} (${trade.exit_price})`
        });
      }
    }

    // Also include revealed markers for other trades if showAllTrades is active
    if (this.showAllTrades && this.allTrades && this.allTrades.length > 0) {
      const activeId = this.tradeContext ? this.tradeContext.trade.trade_id : -1;
      let count = 0;
      for (const t of this.allTrades) {
        if (t.trade_id === activeId) continue;
        const tOpenSec = Math.floor(t.open_time_ms / 1000);
        const tCloseSec = Math.floor(t.close_time_ms / 1000);

        if (maxTime >= tOpenSec) {
          const tIsLong = t.direction === "LONG";
          markers.push({
            time: tOpenSec,
            position: tIsLong ? "belowBar" : "aboveBar",
            color: tIsLong ? "rgba(0, 210, 255, 0.55)" : "rgba(199, 125, 255, 0.55)",
            shape: tIsLong ? "arrowUp" : "arrowDown",
            text: `#${t.trade_id} ${t.direction}`
          });
          count++;
        }

        if (maxTime >= tCloseSec) {
          const isWin = t.pnl_usdt > 0;
          const isTimeout = (t.exit_reason || "").includes("TIMEOUT");
          const exitColor = isWin ? "rgba(0, 240, 144, 0.55)" : (isTimeout ? "rgba(255, 184, 0, 0.55)" : "rgba(255, 51, 102, 0.55)");
          markers.push({
            time: tCloseSec,
            position: t.direction === "LONG" ? "aboveBar" : "belowBar",
            color: exitColor,
            shape: "circle",
            text: `#${t.trade_id} ${t.exit_reason}`
          });
          count++;
        }
        if (count > 200) break;
      }
    }

    markers.sort((a, b) => a.time - b.time);
    this.candleSeries.setMarkers(markers);

    // Update replay display info
    if (currentCandle && this.tradeContext) {
      const trade = this.tradeContext.trade;
      const elapsed = (currentCandle.time - Math.floor(trade.open_time_ms / 1000));
      const priceEl = document.getElementById("replayCurrentPrice");
      const elapsedEl = document.getElementById("replayElapsedSec");
      if (priceEl) priceEl.textContent = currentCandle.close.toFixed(4);
      if (elapsedEl) elapsedEl.textContent = `${elapsed}s`;

      const isLong = trade.direction === "LONG";
      const pu = this.tradeContext.strategy_state.price_unit || 0.0001;
      const delta = isLong ? (currentCandle.close - trade.entry_price) : (trade.entry_price - currentCandle.close);
      const deltaTicks = (delta / pu).toFixed(1);

      const deltaEl = document.getElementById("replayCurrentDelta");
      if (deltaEl) {
        deltaEl.textContent = `${delta >= 0 ? "+" : ""}${deltaTicks} ticks`;
        deltaEl.className = delta >= 0 ? "mono profit" : "mono loss";
      }

      const toTp = isLong ? (trade.tp_price - currentCandle.close) : (currentCandle.close - trade.tp_price);
      const toSl = isLong ? (currentCandle.close - trade.sl_price) : (trade.sl_price - currentCandle.close);
      const toTpEl = document.getElementById("replayToTp");
      const toSlEl = document.getElementById("replayToSl");
      if (toTpEl) toTpEl.textContent = `${(toTp / pu).toFixed(1)}t`;
      if (toSlEl) toSlEl.textContent = `${(toSl / pu).toFixed(1)}t`;

      const inPosPill = document.getElementById("replayInPositionPill");
      if (inPosPill) {
        const entryTimeSec = Math.floor(trade.open_time_ms / 1000);
        const exitTimeSec = Math.floor(trade.close_time_ms / 1000);
        if (currentCandle.time >= entryTimeSec && currentCandle.time <= exitTimeSec) {
          inPosPill.textContent = "IN POSITION";
          inPosPill.className = "status-chip active-pulse";
        } else if (currentCandle.time < entryTimeSec) {
          inPosPill.textContent = "PRE-ENTRY";
          inPosPill.className = "status-chip neutral";
        } else {
          inPosPill.textContent = "POST-EXIT";
          inPosPill.className = "status-chip neutral";
        }
      }
    }
  }

  /**
   * Stops chart replay and restores the full chart view.
   */
  stopChartReplay() {
    this.replayMode = "tick";
    this.chartReplayIndex = 0;
    this.scissorCutIndex = null;
    this.replayActivePosition = null;
    this.hideReplayPositionHUD();

    const banner = document.getElementById("replayExecutionBanner");
    if (banner) banner.style.display = "none";
    const lastEventText = document.getElementById("replayLastEventText");
    if (lastEventText) lastEventText.textContent = "Replay Stopped. Ready.";

    // Remove live price line
    if (this.chartReplayPriceLine) {
      try { this.candleSeries.removePriceLine(this.chartReplayPriceLine); } catch(e) {}
      this.chartReplayPriceLine = null;
    }

    // Restore buttons
    const btnChartReplay = document.getElementById("btnChartReplay");
    if (btnChartReplay) {
      btnChartReplay.textContent = "▶ Chart Replay";
      btnChartReplay.classList.remove("active-pulse");
    }
    const btnPlay = document.getElementById("btnReplayPlay");
    if (btnPlay) {
      btnPlay.innerHTML = "<span>▶</span> Play";
    }

    // Re-render full chart
    this.renderChartData();
    this.initReplaySlider();
  }

  // =========================================================================
  // REPLAY SCISSOR ("CUT & REPLAY") INTERACTIVE TOOL
  // =========================================================================

  toggleScissorMode() {
    this.isScissorMode = !this.isScissorMode;
    const btn = document.getElementById("btnReplayScissor");
    const container = document.getElementById("forensicMainChartContainer");
    const overlay = document.getElementById("scissorOverlayContainer");

    if (this.isScissorMode) {
      if (btn) btn.classList.add("active-scissor");
      if (container) container.classList.add("scissor-mode");
      if (overlay) overlay.style.display = "block";
    } else {
      if (btn) btn.classList.remove("active-scissor");
      if (container) container.classList.remove("scissor-mode");
      if (overlay) overlay.style.display = "none";
    }
  }

  onChartMouseMove(e) {
    if (!this.isScissorMode || !this.chart || !this.fullCandleData || this.fullCandleData.length === 0) return;
    const container = document.getElementById("forensicMainChartContainer");
    if (!container) return;

    const rect = container.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const chartWidth = rect.width;

    // Constrain mouseX within main chart canvas area (excluding right price scale)
    const clampedX = Math.max(10, Math.min(chartWidth - 65, mouseX));

    const line = document.getElementById("scissorDottedLine");
    const mask = document.getElementById("scissorBlurMask");
    const tip = document.getElementById("scissorTooltip");

    if (line) line.style.left = `${clampedX}px`;
    if (mask) mask.style.left = `${clampedX}px`;
    if (tip) {
      tip.style.left = `${clampedX}px`;
      const timeCoord = this.chart.timeScale().coordinateToTime(clampedX);
      if (timeCoord) {
        const d = new Date(timeCoord * 1000);
        const timeStr = d.toISOString().replace("T", " ").substring(0, 19) + " UTC";
        tip.innerHTML = `<span>✂️</span> Cut & Replay from <strong>${timeStr}</strong>`;
      } else {
        tip.innerHTML = `<span>✂️</span> Click to cut & replay from here`;
      }
    }
  }

  onChartMouseLeave() {
    if (!this.isScissorMode) return;
    const overlay = document.getElementById("scissorOverlayContainer");
    if (overlay) overlay.style.display = "none";
  }

  onChartClick(e) {
    if (!this.isScissorMode || !this.chart || !this.fullCandleData || this.fullCandleData.length === 0) return;
    const container = document.getElementById("forensicMainChartContainer");
    if (!container) return;

    const rect = container.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;

    // Convert mouseX to time coordinate
    let clickTime = this.chart.timeScale().coordinateToTime(mouseX);
    let targetIdx = -1;

    if (clickTime) {
      targetIdx = this.fullCandleData.findIndex(c => c.time >= clickTime);
    }
    if (targetIdx === -1) {
      const frac = Math.max(0, Math.min(1, mouseX / (rect.width - 65)));
      targetIdx = Math.floor(frac * (this.fullCandleData.length - 1));
    }

    targetIdx = Math.max(0, Math.min(this.fullCandleData.length - 1, targetIdx));

    // Cut chart at targetIdx!
    this.scissorCutIndex = targetIdx;
    this.chartReplayIndex = targetIdx;
    this.replayMode = "chart";

    // Update scrubber to cut point
    const slider = document.getElementById("replayScrubSlider");
    if (slider) {
      slider.min = 0;
      slider.max = this.fullCandleData.length - 1;
      slider.value = targetIdx;
    }

    // Render frame up to cut point (left visible, right hidden)
    this.renderChartReplayFrame(targetIdx);

    // Turn off scissor mode
    this.toggleScissorMode();

    // Update button states
    const btnChartReplay = document.getElementById("btnChartReplay");
    if (btnChartReplay) {
      btnChartReplay.textContent = "▶ Play Cut Replay";
      btnChartReplay.classList.add("active-pulse");
    }
    const btnPlay = document.getElementById("btnReplayPlay");
    if (btnPlay) {
      btnPlay.innerHTML = "<span>▶</span> Play";
    }
  }

  // =========================================================================
  // WHAT-IF COUNTERFACTUAL EXIT SIMULATOR
  // =========================================================================

  async runWhatIfSimulation() {
    if (!this.activeRunId || !this.activeTradeId) return;

    const timeoutVal = document.getElementById("whatIfTimeoutSelect").value;
    const tpVal = document.getElementById("whatIfTpSelect").value;

    const payload = {
      timeout_seconds: timeoutVal === "NONE" ? null : parseFloat(timeoutVal),
      tp_ticks: tpVal === "DEFAULT" ? null : parseInt(tpVal, 10)
    };

    try {
      const res = await fetch(`/api/forensics/trade/${this.activeRunId}/${this.activeTradeId}/what-if`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error("What-if calculation error");
      const result = await res.json();

      this.renderWhatIfResult(result);
    } catch (e) {
      console.error("[!] Error in what-if simulation:", e);
      alert(`Simulation failed: ${e.message}`);
    }
  }

  renderWhatIfResult(result) {
    const container = document.getElementById("whatIfResultContainer");
    if (!container) return;

    const { original_outcome, hypothetical_outcome, rules_applied } = result;
    const pnlDiff = hypothetical_outcome.pnl_delta_vs_actual;
    const diffSign = pnlDiff >= 0 ? "+" : "";
    const diffClass = pnlDiff > 0 ? "profit" : (pnlDiff < 0 ? "loss" : "neutral");

    container.innerHTML = `
      <div class="what-if-comparison-grid">
        <div class="wi-col original">
          <div class="wi-header">Original Backtest</div>
          <div class="wi-metric"><span class="wi-label">Reason:</span> <strong>${original_outcome.exit_reason}</strong></div>
          <div class="wi-metric"><span class="wi-label">Price:</span> <strong>${original_outcome.exit_price}</strong></div>
          <div class="wi-metric"><span class="wi-label">Duration:</span> <strong>${original_outcome.duration_seconds.toFixed(1)}s</strong></div>
          <div class="wi-metric"><span class="wi-label">PnL:</span> <strong class="${original_outcome.pnl_usdt >= 0 ? 'profit' : 'loss'}">${original_outcome.pnl_usdt.toFixed(4)} USDT</strong></div>
        </div>

        <div class="wi-col hypothetical">
          <div class="wi-header">Hypothetical Simulation</div>
          <div class="wi-metric"><span class="wi-label">Reason:</span> <strong>${hypothetical_outcome.exit_reason}</strong></div>
          <div class="wi-metric"><span class="wi-label">Price:</span> <strong>${hypothetical_outcome.exit_price}</strong></div>
          <div class="wi-metric"><span class="wi-label">Duration:</span> <strong>${hypothetical_outcome.duration_seconds.toFixed(1)}s</strong></div>
          <div class="wi-metric"><span class="wi-label">PnL:</span> <strong class="${hypothetical_outcome.pnl_usdt >= 0 ? 'profit' : 'loss'}">${hypothetical_outcome.pnl_usdt.toFixed(4)} USDT</strong></div>
        </div>
      </div>

      <div class="wi-delta-badge ${diffClass}">
        <span>PnL Delta:</span> <strong>${diffSign}${pnlDiff.toFixed(4)} USDT (${diffSign}${hypothetical_outcome.roe_pct - original_outcome.roe_pct > 0 ? '+' : ''}${(hypothetical_outcome.roe_pct - original_outcome.roe_pct).toFixed(2)}% ROE)</strong>
      </div>
    `;
    container.style.display = "block";
  }
}

// Global instance
window.forensicsLab = new ForensicsController();
