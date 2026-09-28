/**
 * KCEX Backtest Analytics & Comparison Studio - Client Logic
 * ========================================================
 */

// Color Palette for Multi-Run Comparison
const RUN_COLORS = [
  { border: '#00d2ff', bg: 'rgba(0, 210, 255, 0.15)' }, // Electric Cyan
  { border: '#00e676', bg: 'rgba(0, 230, 118, 0.15)' }, // Emerald
  { border: '#ffab00', bg: 'rgba(255, 171, 0, 0.15)' }, // Amber
  { border: '#c77dff', bg: 'rgba(199, 125, 255, 0.15)' }, // Neon Purple
  { border: '#ff2a55', bg: 'rgba(255, 42, 85, 0.15)' },  // Crimson
  { border: '#ffd166', bg: 'rgba(255, 209, 102, 0.15)' }, // Warm Gold
  { border: '#06d6a0', bg: 'rgba(6, 214, 160, 0.15)' },  // Mint
  { border: '#118ab2', bg: 'rgba(17, 138, 178, 0.15)' }, // Ocean Blue
];

// App State
const state = {
  runs: [],
  allFactors: [],
  selectedFactors: new Set([
    'net_roi_pct',
    'net_pnl_usdt',
    'profit_factor',
    'win_rate_pct',
    'max_drawdown_pct',
    'sharpe_ratio',
    'win_loss_payoff',
    'total_trades',
    'avg_duration_seconds',
    'long_win_rate_pct',
    'short_win_rate_pct'
  ]),
  selectedRunIds: new Set(),
  activePairFilter: 'ALL',
  activeStratFilter: 'ALL',
  searchQuery: '',
  deepDiveRunId: null,
  tradesPagination: {
    page: 1,
    pageSize: 50,
    totalPages: 1,
    direction: 'ALL',
    exitReason: 'ALL',
    search: ''
  }
};

// Global Chart Instances
let chartEquityOverlay = null;
let chartDrawdownOverlay = null;
let chartRadar = null;
let chartExitBreakdown = null;
let chartSingleEquity = null;
let chartDurationDist = null;
let chartDirectional = null;

// ============================================================================
// INITIALIZATION
// ============================================================================

document.addEventListener('DOMContentLoaded', async () => {
  setupTabs();
  setupEventListeners();
  await loadFactors();
  await loadRuns();
  await loadStorageStats();
  if (window.forensicsLab) {
    await window.forensicsLab.init();
  }
});

function setupTabs() {
  const tabs = document.querySelectorAll('.tab-btn');
  tabs.forEach(btn => {
    btn.addEventListener('click', () => {
      tabs.forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));

      btn.classList.add('active');
      const paneId = `pane-${btn.dataset.tab}`;
      const pane = document.getElementById(paneId);
      if (pane) pane.classList.add('active');

      if (btn.dataset.tab === 'comparison') {
        renderComparisonStudio();
      } else if (btn.dataset.tab === 'deepdive') {
        if (!state.deepDiveRunId && state.runs.length > 0) {
          state.deepDiveRunId = state.runs[0].metadata.run_id;
        }
        renderDeepDive();
      } else if (btn.dataset.tab === 'forensics') {
        if (window.forensicsLab) {
          if (!window.forensicsLab.chart) {
            window.forensicsLab.init();
          } else {
            setTimeout(() => {
              const c = document.getElementById('forensicMainChartContainer');
              if (c && window.forensicsLab.chart) {
                window.forensicsLab.chart.applyOptions({ width: c.clientWidth });
                window.forensicsLab.chart.timeScale().fitContent();
              }
            }, 50);
          }
        }
      }
    });
  });
}

function switchToTab(tabName) {
  const targetBtn = document.querySelector(`.tab-btn[data-tab="${tabName}"]`);
  if (targetBtn) {
    targetBtn.click();
  }
}

function setupEventListeners() {
  // Global Re-index
  document.getElementById('btnReindex').addEventListener('click', async () => {
    const btn = document.getElementById('btnReindex');
    btn.innerHTML = '<span>⏳</span> Re-indexing...';
    try {
      const res = await fetch('/api/reindex', { method: 'POST' });
      const data = await res.json();
      await loadRuns();
      showToast(`Indexed ${data.indexed_count} backtest runs!`);
    } catch (e) {
      alert('Reindex error: ' + e);
    } finally {
      btn.innerHTML = '<span>🔄</span> Re-Index';
    }
  });

  // Launch Compare from Nav
  document.getElementById('btnLaunchCompare').addEventListener('click', () => {
    switchToTab('comparison');
  });
  document.getElementById('btnLaunchCompareFloating').addEventListener('click', () => {
    switchToTab('comparison');
  });

  // Clear Selection
  document.getElementById('btnClearSelection').addEventListener('click', () => {
    state.selectedRunIds.clear();
    updateRunsTableSelection();
    updateFloatingBar();
  });

  // Select All Checkbox
  document.getElementById('selectAllRuns').addEventListener('change', (e) => {
    const checked = e.target.checked;
    state.runs.forEach(r => {
      if (checked) state.selectedRunIds.add(r.metadata.run_id);
      else state.selectedRunIds.delete(r.metadata.run_id);
    });
    updateRunsTableSelection();
    updateFloatingBar();
  });

  // Library Search & Filters
  document.getElementById('librarySearch').addEventListener('input', (e) => {
    state.searchQuery = e.target.value.toLowerCase();
    renderRunsTable();
  });

  // Factor Drawer Toggles
  document.getElementById('btnToggleFactorsDrawer').addEventListener('click', () => {
    const drawer = document.getElementById('factorsDrawer');
    drawer.style.display = drawer.style.display === 'none' ? 'block' : 'none';
  });

  document.getElementById('btnSelectAllFactors').addEventListener('click', () => {
    state.allFactors.forEach(f => state.selectedFactors.add(f.key));
    renderFactorsCheckboxes();
    renderComparisonStudio();
  });

  document.getElementById('btnPresetReturnRisk').addEventListener('click', () => {
    state.selectedFactors = new Set([
      'net_roi_pct', 'net_pnl_usdt', 'profit_factor', 'win_rate_pct',
      'max_drawdown_pct', 'sharpe_ratio', 'calmar_ratio', 'win_loss_payoff'
    ]);
    renderFactorsCheckboxes();
    renderComparisonStudio();
  });

  document.getElementById('btnResetFactors').addEventListener('click', () => {
    state.selectedFactors = new Set([
      'net_roi_pct', 'net_pnl_usdt', 'profit_factor', 'win_rate_pct',
      'max_drawdown_pct', 'sharpe_ratio', 'win_loss_payoff', 'total_trades',
      'avg_duration_seconds', 'long_win_rate_pct', 'short_win_rate_pct'
    ]);
    renderFactorsCheckboxes();
    renderComparisonStudio();
  });

  document.getElementById('btnRefreshCompare').addEventListener('click', () => {
    renderComparisonStudio();
  });

  // Deep Dive Run Selector
  document.getElementById('ddRunSelector').addEventListener('change', (e) => {
    state.deepDiveRunId = e.target.value;
    renderDeepDive();
  });

  // Paged Trades Filters
  document.getElementById('tradesSearch').addEventListener('input', debounce((e) => {
    state.tradesPagination.search = e.target.value;
    state.tradesPagination.page = 1;
    loadPagedTrades();
  }, 300));

  document.getElementById('tradesDirectionFilter').addEventListener('change', (e) => {
    state.tradesPagination.direction = e.target.value;
    state.tradesPagination.page = 1;
    loadPagedTrades();
  });

  document.getElementById('tradesExitFilter').addEventListener('change', (e) => {
    state.tradesPagination.exitReason = e.target.value;
    state.tradesPagination.page = 1;
    loadPagedTrades();
  });

  document.getElementById('btnPrevPage').addEventListener('click', () => {
    if (state.tradesPagination.page > 1) {
      state.tradesPagination.page--;
      loadPagedTrades();
    }
  });

  document.getElementById('btnNextPage').addEventListener('click', () => {
    if (state.tradesPagination.page < state.tradesPagination.totalPages) {
      state.tradesPagination.page++;
      loadPagedTrades();
    }
  });

  // Storage Purge Action
  document.getElementById('btnPurgeJsonl').addEventListener('click', async () => {
    if (!confirm('Are you sure you want to purge raw .jsonl files? Full trade records remain in CSV and ZIP archives.')) {
      return;
    }
    try {
      const res = await fetch('/api/storage/purge-jsonl', { method: 'POST' });
      const data = await res.json();
      showToast(`Purged ${data.purged_count} JSONL files, reclaimed ${data.reclaimed_mb} MB!`);
      await loadStorageStats();
      await loadRuns();
    } catch (e) {
      alert('Error purging: ' + e);
    }
  });

  // AI Export Triggers
  document.getElementById('btnExportAllAI').addEventListener('click', () => {
    openExportModal('all');
  });

  document.getElementById('btnExportCompareAI').addEventListener('click', () => {
    openExportModal('compare');
  });

  document.getElementById('btnExportDeepAI').addEventListener('click', () => {
    openExportModal('single');
  });

  document.getElementById('btnCloseExportModal').addEventListener('click', closeExportModal);
  document.getElementById('btnCancelExportModal').addEventListener('click', closeExportModal);

  document.getElementById('btnCopyExportAI').addEventListener('click', handleCopyExportAI);
  document.getElementById('btnDownloadExportAI').addEventListener('click', handleDownloadExportAI);

  // Chronos AI Slicer Triggers
  const btnChronos = document.getElementById('btnOpenChronosSlicer');
  if (btnChronos) {
    btnChronos.addEventListener('click', () => openChronosModal());
  }

  const btnCloseChronos = document.getElementById('btnCloseChronosModal');
  if (btnCloseChronos) {
    btnCloseChronos.addEventListener('click', closeChronosModal);
  }

  const chipsContainer = document.getElementById('chronosGranularityChips');
  if (chipsContainer) {
    chipsContainer.addEventListener('click', (e) => {
      const chip = e.target.closest('.filter-chip');
      if (!chip) return;
      chipsContainer.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      const gran = chip.getAttribute('data-granularity');
      loadChronosManifest(gran);
    });
  }

  const btnCopyChunk = document.getElementById('btnChronosCopyChunk');
  if (btnCopyChunk) {
    btnCopyChunk.addEventListener('click', handleChronosCopyChunk);
  }

  const btnDownloadChunk = document.getElementById('btnChronosDownloadChunk');
  if (btnDownloadChunk) {
    btnDownloadChunk.addEventListener('click', handleChronosDownloadChunk);
  }

  const btnBatchZip = document.getElementById('btnChronosBatchZip');
  if (btnBatchZip) {
    btnBatchZip.addEventListener('click', handleChronosBatchZip);
  }

  const modalChronos = document.getElementById('modalChronosSlicer');
  if (modalChronos) {
    modalChronos.addEventListener('click', (e) => {
      if (e.target === modalChronos) {
        closeChronosModal();
      }
    });
  }
}

let currentExportContext = 'compare'; // 'compare' | 'single' | 'all'

function openExportModal(context) {
  currentExportContext = context;
  const modal = document.getElementById('modalExportAI');
  const title = document.getElementById('exportModalTitle');
  const subtitle = document.getElementById('exportModalSubtitle');

  if (context === 'all') {
    title.textContent = `Export All Backtests for AI (${state.runs.length} Runs)`;
    subtitle.textContent = 'Complete repository quantitative dossier for deep LLM analysis';
  } else if (context === 'compare') {
    title.textContent = `Export Strategy Comparison for AI (${state.selectedRunIds.size} Runs)`;
    subtitle.textContent = 'Comparative parameter diffs, scorecards, and radar scores for AI';
  } else {
    const run = state.runs.find(r => r.metadata.run_id === state.deepDiveRunId);
    title.textContent = `Export Single Run for AI (${run ? run.metadata.symbol : ''})`;
    subtitle.textContent = 'Comprehensive single-strategy analytical dossier with hourly heatmaps and duration distributions';
  }

  modal.classList.add('open');
}

function closeExportModal() {
  const modal = document.getElementById('modalExportAI');
  modal.classList.remove('open');
}

async function handleCopyExportAI() {
  const btn = document.getElementById('btnCopyExportAI');
  const origText = btn.innerHTML;
  btn.innerHTML = '<span>⏳</span> Generating Dossier...';

  try {
    const format = document.querySelector('input[name="exportFormat"]:checked').value;
    let content = '';

    if (currentExportContext === 'all') {
      const res = await fetch(`/api/export/all?format=${format}`);
      content = format === 'json' ? JSON.stringify(await res.json(), null, 2) : await res.text();
    } else if (currentExportContext === 'compare') {
      const res = await fetch('/api/export/compare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          run_ids: Array.from(state.selectedRunIds),
          selected_factors: Array.from(state.selectedFactors),
          format: format
        })
      });
      content = format === 'json' ? JSON.stringify(await res.json(), null, 2) : await res.text();
    } else {
      const res = await fetch(`/api/export/run/${state.deepDiveRunId}?format=${format}`);
      content = format === 'json' ? JSON.stringify(await res.json(), null, 2) : await res.text();
    }

    await navigator.clipboard.writeText(content);
    showToast('✓ AI Quantitative Dossier copied to clipboard! Ready to paste into AI chat.');
    closeExportModal();
  } catch (e) {
    alert('Could not copy to clipboard: ' + e);
  } finally {
    btn.innerHTML = origText;
  }
}

async function handleDownloadExportAI() {
  const btn = document.getElementById('btnDownloadExportAI');
  const origText = btn.innerHTML;
  btn.innerHTML = '<span>⏳</span> Preparing...';

  try {
    const format = document.querySelector('input[name="exportFormat"]:checked').value;
    const ext = format === 'json' ? 'json' : 'md';
    let filename = `strategy_ai_dossier_${Date.now()}.${ext}`;
    let content = '';

    if (currentExportContext === 'all') {
      filename = `all_backtests_ai_dossier_${Date.now()}.${ext}`;
      const res = await fetch(`/api/export/all?format=${format}`);
      content = format === 'json' ? JSON.stringify(await res.json(), null, 2) : await res.text();
    } else if (currentExportContext === 'compare') {
      filename = `strategy_comparison_${state.selectedRunIds.size}_runs_ai_dossier_${Date.now()}.${ext}`;
      const res = await fetch('/api/export/compare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          run_ids: Array.from(state.selectedRunIds),
          selected_factors: Array.from(state.selectedFactors),
          format: format
        })
      });
      content = format === 'json' ? JSON.stringify(await res.json(), null, 2) : await res.text();
    } else {
      filename = `${state.deepDiveRunId}_ai_dossier.${ext}`;
      const res = await fetch(`/api/export/run/${state.deepDiveRunId}?format=${format}`);
      content = format === 'json' ? JSON.stringify(await res.json(), null, 2) : await res.text();
    }

    const mime = format === 'json' ? 'application/json' : 'text/markdown';
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    showToast(`✓ Downloaded ${filename}`);
    closeExportModal();
  } catch (e) {
    alert('Error downloading: ' + e);
  } finally {
    btn.innerHTML = origText;
  }
}

// =============================================================================
// CHRONOS SLICER & CROPPED AI DOSSIER CONTROLLER
// =============================================================================

let chronosState = {
  runId: null,
  granularity: 'monthly',
  manifest: null,
  selectedChunk: null,
  selectedIndex: 0
};

function openChronosModal(runId) {
  const targetRunId = runId || state.deepDiveRunId || (state.runs[0] ? state.runs[0].metadata.run_id : null);
  if (!targetRunId) {
    alert('Please select a backtest run first.');
    return;
  }
  chronosState.runId = targetRunId;
  const run = state.runs.find(r => r.metadata.run_id === targetRunId);
  const badge = document.getElementById('chronosActiveRunBadge');
  if (badge) {
    badge.textContent = run ? `${run.metadata.symbol} (${run.metadata.strategy})` : targetRunId;
  }

  const modal = document.getElementById('modalChronosSlicer');
  if (modal) {
    modal.classList.add('open');
  }

  loadChronosManifest(chronosState.granularity);
}

function closeChronosModal() {
  const modal = document.getElementById('modalChronosSlicer');
  if (modal) {
    modal.classList.remove('open');
  }
}

async function loadChronosManifest(granularity) {
  chronosState.granularity = granularity;
  const listEl = document.getElementById('chronosChunksList');
  const countEl = document.getElementById('chronosTotalChunksCount');
  if (listEl) {
    listEl.innerHTML = '<div style="color: var(--text-dim); padding: 1.5rem; text-align: center;">⏳ Slicing backtest into partition chunks...</div>';
  }

  try {
    const res = await fetch(`/api/chunks/manifest/${chronosState.runId}?granularity=${granularity}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    chronosState.manifest = data;

    if (countEl) countEl.textContent = data.total_chunks || 0;

    renderChronosChunks(data.chunks || []);
  } catch (err) {
    if (listEl) {
      listEl.innerHTML = `<div style="color: var(--accent-red); padding: 1rem;">Failed to load slices: ${err.message}</div>`;
    }
  }
}

function renderChronosChunks(chunks) {
  const listEl = document.getElementById('chronosChunksList');
  if (!listEl) return;
  listEl.innerHTML = '';

  if (!chunks || chunks.length === 0) {
    listEl.innerHTML = '<div style="color: var(--text-dim); padding: 1.5rem; text-align: center;">No partition chunks found for this granularity.</div>';
    selectChronosChunk(null, 0);
    return;
  }

  chunks.forEach((c, idx) => {
    const card = document.createElement('div');
    card.className = `chronos-chunk-card ${idx === 0 ? 'selected' : ''}`;
    card.id = `chunkCard_${idx}`;

    const isProfit = c.net_pnl_usdt >= 0;
    const pnlClass = isProfit ? 'win' : 'loss';
    const tickBadge = c.has_ticks ? '<span class="chunk-pill" style="color: var(--accent-cyan); border-color: rgba(0,210,255,0.4);">⚡ Ticks</span>' : '';

    card.innerHTML = `
      <div class="chronos-chunk-card-header">
        <span class="chronos-chunk-card-title">${c.label}</span>
        <span class="chronos-chunk-card-meta">${c.start_date} → ${c.end_date}</span>
      </div>
      <div class="chronos-chunk-metrics">
        <span class="chunk-pill neutral">${c.trades_count.toLocaleString()} trades</span>
        <span class="chunk-pill ${pnlClass}">${c.win_rate_pct.toFixed(1)}% WR</span>
        <span class="chunk-pill ${pnlClass}">${c.net_pnl_usdt > 0 ? '+' : ''}${c.net_pnl_usdt.toFixed(4)} USDT</span>
        ${tickBadge}
        <span class="chunk-pill tokens">~${Math.round(c.estimated_tokens / 1000)}k tokens</span>
      </div>
    `;

    card.addEventListener('click', () => {
      document.querySelectorAll('.chronos-chunk-card').forEach(el => el.classList.remove('selected'));
      card.classList.add('selected');
      selectChronosChunk(c, idx);
    });

    listEl.appendChild(card);
  });

  // Select first by default
  selectChronosChunk(chunks[0], 0);
}

function selectChronosChunk(chunk, idx) {
  chronosState.selectedChunk = chunk;
  chronosState.selectedIndex = idx;

  const lbl = document.getElementById('chronosSelectedLabel');
  const dates = document.getElementById('chronosSelectedDates');
  const trades = document.getElementById('chronosSelectedTrades');
  const wr = document.getElementById('chronosSelectedWinRate');
  const pnl = document.getElementById('chronosSelectedPnL');
  const losses = document.getElementById('chronosSelectedLosses');

  if (!chunk) {
    if (lbl) lbl.textContent = 'No chunk selected';
    if (dates) dates.textContent = '--';
    if (trades) trades.textContent = '--';
    if (wr) wr.textContent = '--';
    if (pnl) pnl.textContent = '--';
    if (losses) losses.textContent = '--';
    return;
  }

  if (lbl) lbl.textContent = `Chunk ${idx + 1}: ${chunk.label}`;
  if (dates) dates.textContent = `${chunk.start_date} to ${chunk.end_date}`;
  if (trades) trades.textContent = `${chunk.trades_count.toLocaleString()}`;
  if (wr) wr.textContent = `${chunk.win_rate_pct.toFixed(1)}%`;
  if (pnl) {
    pnl.textContent = `${chunk.net_pnl_usdt > 0 ? '+' : ''}${chunk.net_pnl_usdt.toFixed(4)} USDT`;
    pnl.style.color = chunk.net_pnl_usdt >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
  }
  if (losses) losses.textContent = `${chunk.losing_trades.toLocaleString()}`;
}

async function handleChronosCopyChunk() {
  const c = chronosState.selectedChunk;
  if (!c) {
    alert('Please select a partition chunk first.');
    return;
  }

  const btn = document.getElementById('btnChronosCopyChunk');
  const origText = btn.innerHTML;
  btn.innerHTML = '<span>⏳</span> Packaging Chunk...';

  try {
    const fmt = document.getElementById('selChronosFormat').value;
    const maxLosses = parseInt(document.getElementById('selChronosMaxLosses').value, 10);
    const incTicks = document.getElementById('chkChronosTicks').checked;
    const incPost = document.getElementById('chkChronosPostExit').checked;

    const res = await fetch('/api/chunks/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        run_id: chronosState.runId,
        start_ms: c.start_ms,
        end_ms: c.end_ms,
        chunk_index: chronosState.selectedIndex + 1,
        total_chunks: (chronosState.manifest ? chronosState.manifest.total_chunks : 1),
        max_losing_trades: maxLosses,
        include_ticks: incTicks,
        include_post_exit: incPost,
        format: fmt
      })
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const content = fmt === 'json' ? JSON.stringify(await res.json(), null, 2) : await res.text();

    await navigator.clipboard.writeText(content);
    showToast(`✓ Chunk ${chronosState.selectedIndex + 1} (${c.label}) copied! Ready to paste into AI chat.`);
  } catch (err) {
    alert('Could not copy chunk: ' + err.message);
  } finally {
    btn.innerHTML = origText;
  }
}

async function handleChronosDownloadChunk() {
  const c = chronosState.selectedChunk;
  if (!c) {
    alert('Please select a partition chunk first.');
    return;
  }

  const btn = document.getElementById('btnChronosDownloadChunk');
  const origText = btn.innerHTML;
  btn.innerHTML = '<span>⏳</span> Exporting...';

  try {
    const fmt = document.getElementById('selChronosFormat').value;
    const maxLosses = parseInt(document.getElementById('selChronosMaxLosses').value, 10);
    const incTicks = document.getElementById('chkChronosTicks').checked;
    const incPost = document.getElementById('chkChronosPostExit').checked;

    const res = await fetch('/api/chunks/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        run_id: chronosState.runId,
        start_ms: c.start_ms,
        end_ms: c.end_ms,
        chunk_index: chronosState.selectedIndex + 1,
        total_chunks: (chronosState.manifest ? chronosState.manifest.total_chunks : 1),
        max_losing_trades: maxLosses,
        include_ticks: incTicks,
        include_post_exit: incPost,
        format: fmt
      })
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const ext = fmt === 'json' ? 'json' : 'md';
    const filename = `${chronosState.runId}_chunk_${chronosState.selectedIndex + 1}_${c.chunk_id}.${ext}`;

    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);

    showToast(`✓ Downloaded Chunk ${chronosState.selectedIndex + 1}!`);
  } catch (err) {
    alert('Could not download chunk: ' + err.message);
  } finally {
    btn.innerHTML = origText;
  }
}

async function handleChronosBatchZip() {
  const btn = document.getElementById('btnChronosBatchZip');
  const origText = btn.innerHTML;
  btn.innerHTML = '<span>⏳</span> Packaging ZIP Archive...';

  try {
    const maxLosses = parseInt(document.getElementById('selChronosMaxLosses').value, 10);
    const incTicks = document.getElementById('chkChronosTicks').checked;
    const incPost = document.getElementById('chkChronosPostExit').checked;

    const res = await fetch('/api/chunks/batch-export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        run_id: chronosState.runId,
        granularity: chronosState.granularity,
        max_losing_trades: maxLosses,
        include_ticks: incTicks,
        include_post_exit: incPost
      })
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const filename = `${chronosState.runId}_${chronosState.granularity}_ai_chunks.zip`;

    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);

    showToast(`✓ Batch ZIP downloaded successfully!`);
  } catch (err) {
    alert('Could not batch download chunks: ' + err.message);
  } finally {
    btn.innerHTML = origText;
  }
}


function switchToTab(tabName) {
  const btn = document.querySelector(`.tab-btn[data-tab="${tabName}"]`);
  if (btn) btn.click();
}

// ============================================================================
// DATA LOADING
// ============================================================================

async function loadFactors() {
  try {
    const res = await fetch('/api/factors');
    state.allFactors = await res.json();
    renderFactorsCheckboxes();
  } catch (e) {
    console.error('Error loading factors:', e);
  }
}

async function loadRuns() {
  try {
    const res = await fetch('/api/runs');
    state.runs = await res.json();

    // Default select top 3-4 runs for comparison if none selected
    if (state.selectedRunIds.size === 0 && state.runs.length > 0) {
      state.runs.slice(0, Math.min(4, state.runs.length)).forEach(r => {
        state.selectedRunIds.add(r.metadata.run_id);
      });
    }

    updateGlobalStats();
    populateFilters();
    renderRunsTable();
    updateFloatingBar();
    populateDeepDiveSelector();
  } catch (e) {
    console.error('Error loading runs:', e);
  }
}

async function loadStorageStats() {
  try {
    const res = await fetch('/api/storage');
    const data = await res.json();
    document.getElementById('storageTotalMb').textContent = `${data.total_mb} MB`;
    document.getElementById('storageJsonlMb').textContent = `${data.jsonl_size_mb} MB`;
    document.getElementById('storageJsonlCount').textContent = `${data.jsonl_files_count} files`;
    document.getElementById('storageCsvMb').textContent = `${data.csv_size_mb} MB`;
    document.getElementById('storageCsvCount').textContent = `${data.csv_files_count} files`;
    document.getElementById('storageZipMb').textContent = `${data.zip_size_mb} MB`;
    document.getElementById('storageZipCount').textContent = `${data.zip_files_count} archives`;
  } catch (e) {
    console.error('Error loading storage stats:', e);
  }
}

function updateGlobalStats() {
  const runCount = state.runs.length;
  document.getElementById('statRunCount').textContent = runCount;
  document.getElementById('libraryBadge').textContent = runCount;

  let totalTrades = 0;
  let bestWR = 0.0;

  state.runs.forEach(r => {
    totalTrades += (r.scorecard.total_trades || 0);
    if (r.scorecard.win_rate_pct > bestWR) {
      bestWR = r.scorecard.win_rate_pct;
    }
  });

  document.getElementById('statTotalTrades').textContent = totalTrades.toLocaleString();
  document.getElementById('statBestWR').textContent = `${bestWR.toFixed(2)}%`;
}

function populateFilters() {
  const pairs = new Set(['ALL']);
  const strats = new Set(['ALL']);

  state.runs.forEach(r => {
    if (r.metadata.symbol) pairs.add(r.metadata.symbol);
    if (r.metadata.strategy) strats.add(r.metadata.strategy);
  });

  const pairContainer = document.getElementById('filterPairs');
  pairContainer.innerHTML = '<span style="font-size: 0.75rem; color: var(--text-muted); font-weight: 600;">PAIR:</span>';
  pairs.forEach(p => {
    const chip = document.createElement('button');
    chip.className = `filter-chip ${state.activePairFilter === p ? 'active' : ''}`;
    chip.textContent = p;
    chip.addEventListener('click', () => {
      document.querySelectorAll('#filterPairs .filter-chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      state.activePairFilter = p;
      renderRunsTable();
    });
    pairContainer.appendChild(chip);
  });

  const stratContainer = document.getElementById('filterStrategies');
  stratContainer.innerHTML = '<span style="font-size: 0.75rem; color: var(--text-muted); font-weight: 600;">STRATEGY:</span>';
  strats.forEach(s => {
    const chip = document.createElement('button');
    chip.className = `filter-chip ${state.activeStratFilter === s ? 'active' : ''}`;
    chip.textContent = s;
    chip.addEventListener('click', () => {
      document.querySelectorAll('#filterStrategies .filter-chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      state.activeStratFilter = s;
      renderRunsTable();
    });
    stratContainer.appendChild(chip);
  });
}

function populateDeepDiveSelector() {
  const sel = document.getElementById('ddRunSelector');
  sel.innerHTML = '';
  state.runs.forEach(r => {
    const opt = document.createElement('option');
    opt.value = r.metadata.run_id;
    opt.textContent = `${r.metadata.symbol} - ${r.metadata.strategy} [TF: ${r.metadata.timeframe || '1m'} | Window: ${r.metadata.date_range || 'N/A'}] [PnL: ${r.scorecard.net_pnl_usdt >= 0 ? '+' : ''}${r.scorecard.net_pnl_usdt} USDT]`;
    if (r.metadata.run_id === state.deepDiveRunId) {
      opt.selected = true;
    }
    sel.appendChild(opt);
  });
}

// ============================================================================
// TAB 1: RUNS TABLE
// ============================================================================

function renderRunsTable() {
  const tbody = document.getElementById('runsTableBody');
  tbody.innerHTML = '';

  const filtered = state.runs.filter(r => {
    if (state.activePairFilter !== 'ALL' && r.metadata.symbol !== state.activePairFilter) return false;
    if (state.activeStratFilter !== 'ALL' && r.metadata.strategy !== state.activeStratFilter) return false;
    if (state.searchQuery) {
      const txt = `${r.metadata.symbol} ${r.metadata.strategy} ${r.metadata.timeframe} ${r.metadata.date_range} ${r.metadata.run_name}`.toLowerCase();
      if (!txt.includes(state.searchQuery)) return false;
    }
    return true;
  });

  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="14" style="text-align: center; color: var(--text-dim); padding: 2rem;">No matching backtest runs found.</td></tr>`;
    return;
  }

  filtered.forEach(r => {
    const tr = document.createElement('tr');
    const isChecked = state.selectedRunIds.has(r.metadata.run_id);
    const pnl = r.scorecard.net_pnl_usdt || 0;
    const roi = r.scorecard.net_roi_pct || 0;
    const pnlClass = pnl >= 0 ? 'profit' : 'loss';
    const pnlSign = pnl >= 0 ? '+' : '';
    const roiSign = roi >= 0 ? '+' : '';

    tr.innerHTML = `
      <td class="run-select-cell">
        <input type="checkbox" class="run-checkbox" data-id="${r.metadata.run_id}" ${isChecked ? 'checked' : ''}>
      </td>
      <td>
        <div class="run-title-cell">
          <span class="run-main-name">
            ${r.metadata.symbol} 
            <span style="color: var(--text-dim); font-weight: normal; font-size: 0.78rem;">${r.metadata.timestamp_utc || ''}</span>
          </span>
          <div class="run-tags">
            <span class="tag tag-blue" style="font-weight: 600;">📅 ${r.metadata.date_range || 'N/A'}</span>
            <span class="tag tag-cyan" style="font-weight: 700;">⏱️ ${r.metadata.timeframe || '1m'}</span>
            <span class="tag tag-purple">⚡ ${r.metadata.leverage}x</span>
            <span class="tag tag-green">🎯 TP +${r.metadata.tp_ticks}pu</span>
            <span class="tag tag-red">🛑 SL ${r.metadata.sl_mode}${r.metadata.sl_value}</span>
            <span class="tag">📦 ${r.metadata.contracts} cs</span>
            ${r.metadata.high_fidelity_ticks ? '<span class="tag tag-cyan">⚡ Tick High-Res</span>' : '<span class="tag">🕯️ Candle</span>'}
            ${r.metadata.source === 'github_cloud' ? '<span class="tag tag-cyan">☁️ Cloud</span>' : '<span class="tag">💻 Local</span>'}
          </div>
        </div>
      </td>
      <td><strong>${r.metadata.symbol}</strong></td>
      <td>
        <span class="mono" style="font-weight: 700;">${r.metadata.strategy}</span>
        ${(r.metadata.strategy_preset || (r.metadata.parameters && r.metadata.parameters.preset)) ? `<span class="tag tag-cyan" style="font-size: 0.65rem; margin-left: 4px; padding: 1px 5px; border-radius: 4px;">${r.metadata.strategy_preset || r.metadata.parameters.preset}</span>` : ''}
      </td>
      <td><span class="mono font-weight-bold" style="color: var(--accent-cyan);">${r.metadata.timeframe || '1m'}</span></td>
      <td><span class="mono" style="font-size: 0.76rem; white-space: nowrap; color: var(--text-bright);">${r.metadata.date_range || 'N/A'}</span></td>
      <td><span class="mono">${r.metadata.leverage}x</span></td>
      <td class="mono">${(r.scorecard.total_trades || 0).toLocaleString()}</td>
      <td class="mono font-weight-bold ${r.scorecard.win_rate_pct >= 80 ? 'profit' : ''}">
        ${(r.scorecard.win_rate_pct || 0).toFixed(2)}%
      </td>
      <td class="mono font-weight-bold ${pnlClass}">
        ${pnlSign}${pnl.toFixed(4)}
      </td>
      <td class="mono font-weight-bold ${pnlClass}">
        ${roiSign}${roi.toFixed(2)}%
      </td>
      <td class="mono">${(r.scorecard.profit_factor || 0).toFixed(2)}</td>
      <td class="mono loss">-${(r.scorecard.max_drawdown_pct || 0).toFixed(2)}%</td>
      <td>
        <div style="display: flex; gap: 0.4rem;">
          <button class="btn btn-secondary btn-sm btn-action-deep" data-id="${r.metadata.run_id}" title="Inspect deep analytics">
            🔬 Deep Dive
          </button>
        </div>
      </td>
    `;

    // Row Checkbox Event
    const cb = tr.querySelector('.run-checkbox');
    cb.addEventListener('change', (e) => {
      if (e.target.checked) state.selectedRunIds.add(r.metadata.run_id);
      else state.selectedRunIds.delete(r.metadata.run_id);
      updateFloatingBar();
    });

    // Deep dive button
    tr.querySelector('.btn-action-deep').addEventListener('click', () => {
      state.deepDiveRunId = r.metadata.run_id;
      switchToTab('deepdive');
    });

    tbody.appendChild(tr);
  });
}

function updateRunsTableSelection() {
  document.querySelectorAll('.run-checkbox').forEach(cb => {
    cb.checked = state.selectedRunIds.has(cb.dataset.id);
  });
}

function updateFloatingBar() {
  const bar = document.getElementById('floatingCompareBar');
  const count = state.selectedRunIds.size;
  document.getElementById('selectedCountText').textContent = `${count} run${count !== 1 ? 's' : ''} selected`;
  document.getElementById('compareBadge').textContent = count;

  if (count >= 1) {
    bar.classList.add('visible');
  } else {
    bar.classList.remove('visible');
  }
}

// ============================================================================
// TAB 2: INTERACTIVE COMPARISON STUDIO (Core Feature)
// ============================================================================

function renderFactorsCheckboxes() {
  const container = document.getElementById('factorsCheckboxesContainer');
  container.innerHTML = '';

  const groups = {};
  state.allFactors.forEach(f => {
    if (!groups[f.category]) groups[f.category] = [];
    groups[f.category].push(f);
  });

  for (const [catName, factors] of Object.entries(groups)) {
    const box = document.createElement('div');
    box.className = 'factor-category-box';
    box.innerHTML = `
      <div class="factor-category-title">
        <span>${catName} Factors</span>
        <span style="font-size: 0.68rem; color: var(--text-muted);">${factors.length} metrics</span>
      </div>
      <div class="factor-options"></div>
    `;

    const optContainer = box.querySelector('.factor-options');
    factors.forEach(f => {
      const lbl = document.createElement('label');
      lbl.className = 'factor-label';
      const checked = state.selectedFactors.has(f.key) ? 'checked' : '';
      lbl.innerHTML = `
        <input type="checkbox" data-factor="${f.key}" ${checked}>
        <span>${f.name}</span>
      `;

      lbl.querySelector('input').addEventListener('change', (e) => {
        if (e.target.checked) state.selectedFactors.add(f.key);
        else state.selectedFactors.delete(f.key);
        renderComparisonStudio();
      });

      optContainer.appendChild(lbl);
    });

    container.appendChild(box);
  }
}

async function renderComparisonStudio() {
  const runIds = Array.from(state.selectedRunIds);
  if (runIds.length === 0) {
    alert('Please select at least 1 backtest run to compare.');
    switchToTab('library');
    return;
  }

  // Render Run Chips
  renderActiveRunChips();

  // Fetch comparison payload from backend
  try {
    const res = await fetch('/api/compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        run_ids: runIds,
        selected_factors: Array.from(state.selectedFactors)
      })
    });
    const data = await res.json();
    if (data.error) {
      alert(data.error);
      return;
    }

    renderParameterDiffCard(data.parameter_diffs, data.run_ids);
    renderComparisonMatrix(data.comparison_matrix, data.run_ids, data.runs_meta);
    renderEquityOverlayChart(data.equity_overlays);
    renderDrawdownOverlayChart(data.equity_overlays);
    renderRadarChart(data.radar_footprints);
    renderExitComparisonChart(data.exit_comparison);
  } catch (e) {
    console.error('Comparison error:', e);
  }
}

function renderActiveRunChips() {
  const container = document.getElementById('activeRunsChips');
  container.innerHTML = '';

  let idx = 0;
  state.selectedRunIds.forEach(rid => {
    const run = state.runs.find(r => r.metadata.run_id === rid);
    if (!run) return;

    const color = RUN_COLORS[idx % RUN_COLORS.length];
    const chip = document.createElement('div');
    chip.className = 'run-chip';
    chip.innerHTML = `
      <span class="chip-dot" style="background: ${color.border};"></span>
      <span>${run.metadata.symbol} ${run.metadata.strategy} (${run.metadata.timeframe})</span>
      <button class="chip-remove" data-id="${rid}" title="Remove from comparison">&times;</button>
    `;

    chip.querySelector('.chip-remove').addEventListener('click', (e) => {
      e.stopPropagation();
      state.selectedRunIds.delete(rid);
      updateRunsTableSelection();
      updateFloatingBar();
      renderComparisonStudio();
    });

    container.appendChild(chip);
    idx++;
  });
}

function renderParameterDiffCard(diffs, runIds) {
  const container = document.getElementById('diffGridContainer');
  const badge = document.getElementById('diffCountBadge');
  container.innerHTML = '';

  const actualDiffs = diffs.filter(d => d.is_diff);
  badge.textContent = `${actualDiffs.length} Differences Found`;
  badge.style.background = actualDiffs.length > 0 ? 'rgba(0, 210, 255, 0.15)' : 'rgba(0, 230, 118, 0.15)';
  badge.style.color = actualDiffs.length > 0 ? 'var(--accent-cyan)' : 'var(--profit-green)';

  if (diffs.length === 0) {
    container.innerHTML = `<p style="color: var(--text-dim); font-size: 0.85rem;">No parameter differences detected.</p>`;
    return;
  }

  diffs.forEach(d => {
    const item = document.createElement('div');
    item.className = 'diff-item';
    if (!d.is_diff) {
      item.style.borderLeftColor = 'var(--text-muted)';
      item.style.opacity = '0.7';
    }

    let rowsHtml = '';
    runIds.forEach((rid, idx) => {
      const run = state.runs.find(r => r.metadata.run_id === rid);
      const runName = run ? `${run.metadata.symbol} (${run.metadata.strategy})` : `Run #${idx + 1}`;
      const val = d.values[rid] || '—';
      rowsHtml += `
        <div class="diff-val-row">
          <span class="diff-run-label">${runName}:</span>
          <span class="diff-val-badge" style="${!d.is_diff ? 'color: var(--text-dim); background: none;' : ''}">${val}</span>
        </div>
      `;
    });

    item.innerHTML = `
      <div class="diff-param-name">
        ${d.name} ${d.is_diff ? '<span style="color: var(--accent-cyan); font-weight: 700;">[VARIED]</span>' : ''}
      </div>
      <div class="diff-values-list">
        ${rowsHtml}
      </div>
    `;
    container.appendChild(item);
  });
}

function renderComparisonMatrix(matrix, runIds, runsMeta) {
  const headerRow = document.getElementById('matrixHeaderRow');
  const tbody = document.getElementById('matrixBody');

  // Headers
  headerRow.innerHTML = `<th style="width: 280px;">Comparison Factor</th>`;
  runIds.forEach((rid, idx) => {
    const meta = runsMeta.find(m => m.run_id === rid);
    const color = RUN_COLORS[idx % RUN_COLORS.length];
    const th = document.createElement('th');
    th.innerHTML = `
      <div style="display: flex; align-items: center; gap: 0.5rem;">
        <span style="width: 8px; height: 8px; border-radius: 50%; background: ${color.border};"></span>
        <span>${meta ? `${meta.symbol} ${meta.strategy}` : `Run ${idx+1}`}</span>
      </div>
      <div style="font-size: 0.72rem; color: var(--text-dim); font-weight: normal; margin-top: 0.15rem;">
        ${meta ? `${meta.timeframe} | ${meta.leverage}x | TP${meta.tp_ticks}` : ''}
      </div>
    `;
    headerRow.appendChild(th);
  });

  // Body Rows grouped by category
  tbody.innerHTML = '';
  let currentCategory = '';

  matrix.forEach(row => {
    if (row.category !== currentCategory) {
      currentCategory = row.category;
      const catTr = document.createElement('tr');
      catTr.className = 'matrix-category-header';
      catTr.innerHTML = `<td colspan="${runIds.length + 1}">⚡ ${currentCategory} Analysis</td>`;
      tbody.appendChild(catTr);
    }

    const tr = document.createElement('tr');
    let cellsHtml = `<td class="metric-name-cell">${row.name}</td>`;

    runIds.forEach(rid => {
      const val = row.values[rid];
      const isBest = (rid === row.best_run_id);
      const formatted = formatMetricValue(val, row.format);
      const cellClass = getMetricColorClass(val, row.key);

      cellsHtml += `
        <td class="val-cell ${cellClass}">
          ${formatted}
          ${isBest ? '<span class="best-badge">★ #1 Best</span>' : ''}
        </td>
      `;
    });

    tr.innerHTML = cellsHtml;
    tbody.appendChild(tr);
  });
}

function formatMetricValue(val, format) {
  if (val === undefined || val === null) return '—';
  if (typeof val !== 'number') return String(val);

  switch (format) {
    case 'currency':
      return `${val >= 0 ? '+' : ''}${val.toFixed(4)} USDT`;
    case 'inr':
      return `₹${val.toFixed(2)}`;
    case 'currency_sub':
      return `${val >= 0 ? '+' : ''}${val.toFixed(4)} USDT`;
    case 'fee':
      return `${val.toFixed(6)} USDT`;
    case 'pct':
      return `${val >= 0 ? '+' : ''}${val.toFixed(2)}%`;
    case 'float2':
      return val.toFixed(2);
    case 'int':
      return val.toLocaleString();
    case 'duration':
      return formatSeconds(val);
    default:
      return String(val);
  }
}

function getMetricColorClass(val, key) {
  if (typeof val !== 'number') return '';
  if (key.includes('roi') || key.includes('pnl')) {
    return val > 0 ? 'profit' : (val < 0 ? 'loss' : '');
  }
  if (key.includes('drawdown')) {
    return 'loss';
  }
  if (key.includes('win_rate')) {
    return val >= 80 ? 'profit' : '';
  }
  return '';
}

function formatSeconds(sec) {
  if (sec < 60) return `${sec.toFixed(1)}s`;
  if (sec < 3600) return `${Math.floor(sec/60)}m ${Math.floor(sec%60)}s`;
  return `${Math.floor(sec/3600)}h ${Math.floor((sec%3600)/60)}m`;
}

// ============================================================================
// CHARTS GENERATION (CHART.JS)
// ============================================================================

function renderEquityOverlayChart(overlays) {
  const ctx = document.getElementById('chartEquityOverlay').getContext('2d');
  if (chartEquityOverlay) chartEquityOverlay.destroy();

  const datasets = overlays.series.map((s, idx) => {
    const color = RUN_COLORS[idx % RUN_COLORS.length];
    return {
      label: `${s.symbol} ${s.strategy}`,
      data: s.points.map(p => ({ x: p.time, y: p.roi_pct })),
      borderColor: color.border,
      backgroundColor: color.bg,
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 4,
      tension: 0.1,
      fill: false
    };
  });

  chartEquityOverlay = new Chart(ctx, {
    type: 'line',
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'nearest', intersect: false },
      plugins: {
        legend: { labels: { color: '#c2d6f5', font: { family: 'Outfit', size: 11 } } },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y >= 0 ? '+' : ''}${ctx.parsed.y.toFixed(2)}% ROI`
          }
        }
      },
      scales: {
        x: {
          ticks: { color: '#566782', maxTicksLimit: 8, font: { family: 'JetBrains Mono', size: 10 } },
          grid: { color: 'rgba(255, 255, 255, 0.04)' }
        },
        y: {
          ticks: {
            color: '#8b9bb4',
            font: { family: 'JetBrains Mono', size: 10 },
            callback: (v) => `${v >= 0 ? '+' : ''}${v}%`
          },
          grid: { color: 'rgba(255, 255, 255, 0.04)' }
        }
      }
    }
  });
}

function renderDrawdownOverlayChart(overlays) {
  const ctx = document.getElementById('chartDrawdownOverlay').getContext('2d');
  if (chartDrawdownOverlay) chartDrawdownOverlay.destroy();

  const datasets = overlays.series.map((s, idx) => {
    const color = RUN_COLORS[idx % RUN_COLORS.length];
    return {
      label: `${s.symbol} ${s.strategy}`,
      data: s.points.map(p => ({ x: p.time, y: -Math.abs(p.drawdown_pct) })),
      borderColor: color.border,
      backgroundColor: color.bg,
      borderWidth: 1.5,
      pointRadius: 0,
      fill: true,
      tension: 0.1
    };
  });

  chartDrawdownOverlay = new Chart(ctx, {
    type: 'line',
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#c2d6f5', font: { family: 'Outfit', size: 11 } } },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(2)}% Drawdown`
          }
        }
      },
      scales: {
        x: {
          ticks: { color: '#566782', maxTicksLimit: 8, font: { family: 'JetBrains Mono', size: 10 } },
          grid: { color: 'rgba(255, 255, 255, 0.04)' }
        },
        y: {
          ticks: {
            color: '#ff2a55',
            font: { family: 'JetBrains Mono', size: 10 },
            callback: (v) => `${v}%`
          },
          grid: { color: 'rgba(255, 255, 255, 0.04)' }
        }
      }
    }
  });
}

function renderRadarChart(radarData) {
  const ctx = document.getElementById('chartRadar').getContext('2d');
  if (chartRadar) chartRadar.destroy();

  const datasets = radarData.series.map((s, idx) => {
    const color = RUN_COLORS[idx % RUN_COLORS.length];
    return {
      label: s.run_name.split(' (')[0],
      data: s.scores,
      borderColor: color.border,
      backgroundColor: color.bg,
      borderWidth: 2,
      pointBackgroundColor: color.border
    };
  });

  chartRadar = new Chart(ctx, {
    type: 'radar',
    data: {
      labels: radarData.dimensions,
      datasets
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#c2d6f5', font: { family: 'Outfit', size: 11 } } }
      },
      scales: {
        r: {
          angleLines: { color: 'rgba(255, 255, 255, 0.08)' },
          grid: { color: 'rgba(255, 255, 255, 0.05)' },
          pointLabels: { color: '#8b9bb4', font: { family: 'Outfit', size: 10 } },
          suggestedMin: 0,
          suggestedMax: 100,
          ticks: { display: false }
        }
      }
    }
  });
}

function renderExitComparisonChart(exitData) {
  const ctx = document.getElementById('chartExitBreakdown').getContext('2d');
  if (chartExitBreakdown) chartExitBreakdown.destroy();

  const labels = exitData.series.map(s => s.run_name.split(' (')[0]);
  const tpCounts = exitData.series.map(s => {
    const tpIdx = exitData.reasons.indexOf('MIN_PROFIT_TP_HIT');
    return tpIdx !== -1 ? s.percentages[tpIdx] : 0;
  });
  const slCounts = exitData.series.map(s => {
    const slIdx = exitData.reasons.indexOf('STOP_LOSS_HIT');
    return slIdx !== -1 ? s.percentages[slIdx] : 0;
  });

  chartExitBreakdown = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        {
          label: 'Take Profit Hit %',
          data: tpCounts,
          backgroundColor: '#00e676',
        },
        {
          label: 'Stop Loss Hit %',
          data: slCounts,
          backgroundColor: '#ff2a55',
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: {
          stacked: true,
          ticks: { color: '#8b9bb4', font: { family: 'Outfit', size: 10 } },
          grid: { color: 'rgba(255, 255, 255, 0.04)' }
        },
        y: {
          stacked: true,
          max: 100,
          ticks: {
            color: '#8b9bb4',
            font: { family: 'JetBrains Mono', size: 10 },
            callback: (v) => `${v}%`
          },
          grid: { color: 'rgba(255, 255, 255, 0.04)' }
        }
      }
    }
  });
}

// ============================================================================
// TAB 3: DEEP DIVE SINGLE-RUN INSPECTOR
// ============================================================================

async function renderDeepDive() {
  if (!state.deepDiveRunId) return;

  try {
    const res = await fetch(`/api/run/${state.deepDiveRunId}`);
    const run = await res.json();

    const curveRes = await fetch(`/api/run/${state.deepDiveRunId}/curve`);
    const curveData = await curveRes.json();

    document.getElementById('ddTitle').textContent = `${run.metadata.symbol} — ${run.metadata.strategy} (${run.metadata.timeframe || '1m'})`;
    document.getElementById('ddSubtitle').textContent = `📅 Window: ${run.metadata.date_range || 'N/A'} | ⏱️ Timeframe: ${run.metadata.timeframe || '1m'} | ⚡ Leverage: ${run.metadata.leverage}x | 🎯 TP: +${run.metadata.tp_ticks} ticks | 🛑 SL: ${run.metadata.sl_mode} ${run.metadata.sl_value} | 📦 Sizing: ${run.metadata.contracts} contract(s)`;

    renderDeepDiveParametersCard(run);
    renderDeepDiveScorecard(run);
    renderSingleEquityChart(curveData.points);
    renderDurationDistributionChart(run.detailed?.duration_buckets);
    renderDirectionalChart(run.directional);
    renderHourlyHeatmap(run.detailed?.hourly_distribution);

    // Reset and load trades
    state.tradesPagination.page = 1;
    loadPagedTrades();
  } catch (e) {
    console.error('Deep dive error:', e);
  }
}

function renderDeepDiveParametersCard(run) {
  const container = document.getElementById('ddParametersContent');
  if (!container) return;
  const m = run.metadata;
  const params = m.parameters || {};
  const filters = m.filters || {};

  const strat = m.strategy || 'UNKNOWN';
  const preset = m.strategy_preset || params.preset || 'STANDARD';

  let paramBadgesHtml = '';
  if (Object.keys(params).length > 0) {
    for (const [k, v] of Object.entries(params)) {
      if (k === 'preset') continue;
      const label = k.replace(/_/g, ' ').toUpperCase();
      paramBadgesHtml += `
        <div class="param-pill">
          <span class="param-pill-key">${label}</span>
          <span class="param-pill-val">${v}</span>
        </div>
      `;
    }
  } else {
    paramBadgesHtml = `<span style="color: var(--text-dim); font-size: 0.8rem; padding: 4px 0;">Standard factory calibration</span>`;
  }

  let filterBadgesHtml = '';
  if (Object.keys(filters).length > 0) {
    for (const [k, v] of Object.entries(filters)) {
      const label = k.replace(/_/g, ' ').toUpperCase();
      const isTrue = v === true || v === 'ENABLED';
      const isFalse = v === false || v === 'DISABLED';
      const valStr = isTrue ? 'ENABLED' : (isFalse ? 'DISABLED' : v);
      const valClass = isTrue ? 'val-active' : (isFalse ? 'val-inactive' : 'val-custom');
      filterBadgesHtml += `
        <div class="param-pill">
          <span class="param-pill-key">${label}</span>
          <span class="param-pill-val ${valClass}">${valStr}</span>
        </div>
      `;
    }
  } else {
    filterBadgesHtml = `
      <div class="param-pill">
        <span class="param-pill-key">DURATION FILTER</span>
        <span class="param-pill-val val-inactive">DISABLED</span>
      </div>
      <div class="param-pill">
        <span class="param-pill-key">ADX REGIME</span>
        <span class="param-pill-val val-inactive">DISABLED</span>
      </div>
      <div class="param-pill">
        <span class="param-pill-key">200 EMA HTF</span>
        <span class="param-pill-val val-inactive">DISABLED</span>
      </div>
      <div class="param-pill">
        <span class="param-pill-key">HOURLY FILTER</span>
        <span class="param-pill-val val-inactive">DISABLED</span>
      </div>
    `;
  }

  container.innerHTML = `
    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 1.25rem;">
      <!-- Group 1: Market & Backtest Window -->
      <div class="params-subgroup">
        <div class="params-subgroup-title">
          <span>📅 Backtest Window & Market Scope</span>
          <span class="tag tag-blue">${m.timeframe || '1m'}</span>
        </div>
        <div class="params-pills-wrap">
          <div class="param-pill">
            <span class="param-pill-key">TIMEFRAME</span>
            <span class="param-pill-val" style="color: var(--accent-cyan); font-weight: 700;">${m.timeframe || '1m'} Candles</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">EVALUATION WINDOW</span>
            <span class="param-pill-val" style="color: #82b1ff; font-weight: 700;">${m.date_range || 'N/A'}</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">START DATE</span>
            <span class="param-pill-val">${m.start_date || '2026-01-01'}</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">END DATE</span>
            <span class="param-pill-val">${m.end_date || '2026-08-31'}</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">STARTING CAPITAL</span>
            <span class="param-pill-val">$${(m.starting_capital_usdt || 100).toFixed(2)} USDT (₹${(m.starting_capital_inr || 9445).toFixed(2)})</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">MATCHING MODE</span>
            <span class="param-pill-val">${m.high_fidelity_ticks ? '⚡ High-Fidelity Ticks' : '🕯️ Candle OHLC'}</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">SLIPPAGE</span>
            <span class="param-pill-val">${m.slippage_ticks} ticks</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">RUN SOURCE</span>
            <span class="param-pill-val">${m.source === 'github_cloud' ? '☁️ GitHub Cloud Runner' : '💻 Local Workstation'}</span>
          </div>
        </div>
      </div>

      <!-- Group 2: Strategy & Indicators -->
      <div class="params-subgroup">
        <div class="params-subgroup-title">
          <span>⚙️ Strategy & Indicators</span>
          <span class="tag tag-cyan">${preset}</span>
        </div>
        <div class="params-pills-wrap">
          <div class="param-pill">
            <span class="param-pill-key">STRATEGY</span>
            <span class="param-pill-val" style="color: var(--accent-cyan); font-weight: 700;">${strat}</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">PRESET PROFILE</span>
            <span class="param-pill-val" style="color: #00e676; font-weight: 700;">${preset}</span>
          </div>
          ${paramBadgesHtml}
        </div>
      </div>

      <!-- Group 3: Execution, Sizing & Risk Rules -->
      <div class="params-subgroup">
        <div class="params-subgroup-title">
          <span>🛡️ Execution & Risk Rules</span>
          <span class="tag tag-purple">${m.leverage}x Leverage</span>
        </div>
        <div class="params-pills-wrap">
          <div class="param-pill">
            <span class="param-pill-key">LEVERAGE</span>
            <span class="param-pill-val" style="color: #c77dff; font-weight: 700;">${m.leverage}x Isolated</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">POSITION SIZING</span>
            <span class="param-pill-val">${m.volume_desc || (m.contracts + ' contract(s)')}</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">SIZING MODE</span>
            <span class="param-pill-val">${m.sizing_mode}</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">TAKE PROFIT</span>
            <span class="param-pill-val" style="color: var(--profit-green); font-weight: 700;">${m.tp_target_desc || ('+' + m.tp_ticks + ' ticks')}</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">STOP LOSS</span>
            <span class="param-pill-val" style="color: var(--loss-red); font-weight: 700;">${m.sl_rule_desc || (m.sl_mode + ' ' + m.sl_value)}</span>
          </div>
        </div>
      </div>

      <!-- Group 4: Exchange Specs & Fee Structure -->
      <div class="params-subgroup">
        <div class="params-subgroup-title">
          <span>🏦 KCEX Specifications & Fees</span>
          <span class="tag tag-amber">${m.fee_mode || 'ZERO'} Fee Mode</span>
        </div>
        <div class="params-pills-wrap">
          <div class="param-pill">
            <span class="param-pill-key">FEE SCHEDULE</span>
            <span class="param-pill-val" style="color: #ffab00; font-weight: 700;">${m.fee_mode || 'ZERO'} Mode</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">MAKER FEE</span>
            <span class="param-pill-val">${(m.maker_fee_pct || 0).toFixed(4)}%</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">TAKER FEE</span>
            <span class="param-pill-val">${(m.taker_fee_pct || 0).toFixed(4)}%</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">CONTRACT SIZE (CS)</span>
            <span class="param-pill-val">${m.contract_size} ${m.base_asset}</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">PRICE UNIT (PU)</span>
            <span class="param-pill-val">${m.price_unit} USDT</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">PRICE PRECISION</span>
            <span class="param-pill-val">${m.price_precision || 3} Decimals</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">MIN ORDER VOLUME</span>
            <span class="param-pill-val">${m.min_volume || 1.0} Contracts</span>
          </div>
          <div class="param-pill">
            <span class="param-pill-key">MAX LEVERAGE</span>
            <span class="param-pill-val">${m.max_leverage || m.leverage}x</span>
          </div>
        </div>
      </div>

      <!-- Group 5: Trade Optimization & Regime Filters -->
      <div class="params-subgroup">
        <div class="params-subgroup-title">
          <span>🎛️ Optimization & Regime Filters</span>
          <span class="tag">${filters.duration_filter ? 'PROTECTED' : 'STANDARD'}</span>
        </div>
        <div class="params-pills-wrap">
          ${filterBadgesHtml}
        </div>
      </div>
    </div>
  `;
}

function renderDeepDiveScorecard(run) {
  const grid = document.getElementById('ddScorecardGrid');
  const sc = run.scorecard;
  const inrRate = 94.45;
  const pnlUsdt = sc.net_pnl_usdt || 0;
  const pnlInr = sc.net_pnl_inr || (pnlUsdt * inrRate);
  const pnlClass = pnlUsdt >= 0 ? 'profit' : 'loss';
  const pnlSign = pnlUsdt >= 0 ? '+' : '';

  grid.innerHTML = `
    <div class="scorecard-metric">
      <span class="scorecard-label">Net Realized PnL (Dual)</span>
      <span class="scorecard-val ${pnlClass}">${pnlSign}${pnlUsdt.toFixed(4)} USDT</span>
      <span class="scorecard-sub ${pnlClass}">₹${pnlInr.toFixed(2)} INR (${sc.net_roi_pct >= 0 ? '+' : ''}${sc.net_roi_pct.toFixed(2)}% ROI)</span>
    </div>
    <div class="scorecard-metric">
      <span class="scorecard-label">Profit Factor</span>
      <span class="scorecard-val ${sc.profit_factor >= 1.0 ? 'profit' : 'loss'}">${sc.profit_factor.toFixed(2)}</span>
      <span class="scorecard-sub">Win/Loss Payoff: ${sc.win_loss_payoff.toFixed(2)}</span>
    </div>
    <div class="scorecard-metric">
      <span class="scorecard-label">Win Rate</span>
      <span class="scorecard-val profit">${sc.win_rate_pct.toFixed(2)}%</span>
      <span class="scorecard-sub">${(sc.winning_trades||0).toLocaleString()} Wins / ${(sc.losing_trades||0).toLocaleString()} Losses</span>
    </div>
    <div class="scorecard-metric">
      <span class="scorecard-label">Max Drawdown</span>
      <span class="scorecard-val loss">-${sc.max_drawdown_pct.toFixed(2)}%</span>
      <span class="scorecard-sub">-${sc.max_drawdown_usdt.toFixed(4)} USDT</span>
    </div>
    <div class="scorecard-metric">
      <span class="scorecard-label">Risk-Adjusted (Sharpe)</span>
      <span class="scorecard-val cyan">${sc.sharpe_ratio.toFixed(2)}</span>
      <span class="scorecard-sub">Sortino: ${sc.sortino_ratio.toFixed(2)} | Calmar: ${sc.calmar_ratio.toFixed(2)}</span>
    </div>
    <div class="scorecard-metric">
      <span class="scorecard-label">Average Trade Duration</span>
      <span class="scorecard-val">${formatSeconds(sc.avg_duration_seconds)}</span>
      <span class="scorecard-sub">Streaks: ${sc.max_consecutive_wins} W / ${sc.max_consecutive_losses} L</span>
    </div>
  `;
}

function renderSingleEquityChart(points) {
  const ctx = document.getElementById('chartSingleEquity').getContext('2d');
  if (chartSingleEquity) chartSingleEquity.destroy();

  chartSingleEquity = new Chart(ctx, {
    type: 'line',
    data: {
      labels: points.map(p => p.time_utc),
      datasets: [
        {
          label: 'Balance (USDT)',
          data: points.map(p => p.balance_usdt),
          borderColor: '#00d2ff',
          backgroundColor: 'rgba(0, 210, 255, 0.08)',
          borderWidth: 2,
          pointRadius: 0,
          fill: true,
          tension: 0.1,
          yAxisID: 'y'
        },
        {
          label: 'Drawdown (%)',
          data: points.map(p => -Math.abs(p.drawdown_pct)),
          borderColor: 'rgba(255, 42, 85, 0.6)',
          backgroundColor: 'rgba(255, 42, 85, 0.12)',
          borderWidth: 1,
          pointRadius: 0,
          fill: true,
          tension: 0.1,
          yAxisID: 'y1'
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: '#c2d6f5', font: { family: 'Outfit', size: 11 } } }
      },
      scales: {
        x: {
          ticks: { color: '#566782', maxTicksLimit: 8, font: { family: 'JetBrains Mono', size: 10 } },
          grid: { color: 'rgba(255, 255, 255, 0.04)' }
        },
        y: {
          position: 'left',
          ticks: { color: '#00d2ff', font: { family: 'JetBrains Mono', size: 10 } },
          grid: { color: 'rgba(255, 255, 255, 0.04)' }
        },
        y1: {
          position: 'right',
          ticks: { color: '#ff2a55', font: { family: 'JetBrains Mono', size: 10 }, callback: (v) => `${v}%` },
          grid: { display: false }
        }
      }
    }
  });
}

function renderDurationDistributionChart(buckets) {
  const ctx = document.getElementById('chartDurationDistribution').getContext('2d');
  if (chartDurationDist) chartDurationDist.destroy();
  if (!buckets) return;

  const labels = Object.keys(buckets);
  const counts = labels.map(k => buckets[k].count);
  const winRates = labels.map(k => buckets[k].win_rate_pct);

  chartDurationDist = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        {
          label: 'Trade Count',
          data: counts,
          backgroundColor: 'rgba(0, 210, 255, 0.4)',
          borderColor: '#00d2ff',
          borderWidth: 1,
          yAxisID: 'y'
        },
        {
          label: 'Win Rate %',
          data: winRates,
          type: 'line',
          borderColor: '#00e676',
          backgroundColor: '#00e676',
          borderWidth: 2,
          pointRadius: 4,
          yAxisID: 'y1'
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { ticks: { color: '#8b9bb4' }, grid: { display: false } },
        y: { ticks: { color: '#00d2ff' }, grid: { color: 'rgba(255,255,255,0.04)' } },
        y1: {
          position: 'right',
          max: 100,
          ticks: { color: '#00e676', callback: (v) => `${v}%` },
          grid: { display: false }
        }
      }
    }
  });
}

function renderDirectionalChart(dir) {
  const ctx = document.getElementById('chartDirectional').getContext('2d');
  if (chartDirectional) chartDirectional.destroy();
  if (!dir) return;

  chartDirectional = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: ['LONG Signals', 'SHORT Signals'],
      datasets: [
        {
          label: 'Winning Trades',
          data: [dir.long_wins, dir.short_wins],
          backgroundColor: '#00e676'
        },
        {
          label: 'Losing Trades',
          data: [dir.long_losses, dir.short_losses],
          backgroundColor: '#ff2a55'
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { ticks: { color: '#8b9bb4' }, grid: { display: false } },
        y: { ticks: { color: '#8b9bb4' }, grid: { color: 'rgba(255,255,255,0.04)' } }
      }
    }
  });
}

function renderHourlyHeatmap(hourly) {
  const container = document.getElementById('ddHourlyHeatmap');
  container.innerHTML = '';
  if (!hourly) return;

  hourly.forEach(h => {
    const cell = document.createElement('div');
    cell.className = 'heatmap-cell';
    const isProfit = h.pnl >= 0;
    const wr = h.win_rate_pct || 0;

    // Green to cyan or red gradient
    if (isProfit && h.trades > 0) {
      cell.style.background = `rgba(0, 230, 118, ${Math.min(0.35, 0.08 + wr/300)})`;
      cell.style.borderColor = 'rgba(0, 230, 118, 0.3)';
    } else if (h.trades > 0) {
      cell.style.background = 'rgba(255, 42, 85, 0.12)';
      cell.style.borderColor = 'rgba(255, 42, 85, 0.25)';
    }

    cell.innerHTML = `
      <span class="heatmap-hour">${String(h.hour).padStart(2, '0')}:00</span>
      <span class="mono" style="font-weight: 700; font-size: 0.78rem;">${wr.toFixed(0)}%</span>
      <span style="font-size: 0.65rem; color: var(--text-dim);">${h.trades} trds</span>
      <span class="mono ${isProfit ? 'profit' : 'loss'}" style="font-size: 0.65rem;">
        ${h.pnl >= 0 ? '+' : ''}${h.pnl.toFixed(3)}
      </span>
    `;
    container.appendChild(cell);
  });
}

async function loadPagedTrades() {
  if (!state.deepDiveRunId) return;

  const { page, pageSize, direction, exitReason, search } = state.tradesPagination;
  const url = new URL(`/api/run/${state.deepDiveRunId}/trades`, window.location.origin);
  url.searchParams.set('page', page);
  url.searchParams.set('page_size', pageSize);
  if (direction && direction !== 'ALL') url.searchParams.set('direction', direction);
  if (exitReason && exitReason !== 'ALL') url.searchParams.set('exit_reason', exitReason);
  if (search) url.searchParams.set('search', search);

  try {
    const res = await fetch(url);
    const data = await res.json();
    state.tradesPagination.totalPages = data.total_pages || 1;

    document.getElementById('pageIndicator').textContent = `Page ${data.page} of ${data.total_pages} (${(data.total_count||0).toLocaleString()} trades)`;
    document.getElementById('tradesSummarySubtitle').textContent = `Showing ${data.trades.length} of ${(data.total_count||0).toLocaleString()} executed trades.`;

    const tbody = document.getElementById('tradesTableBody');
    tbody.innerHTML = '';

    if (data.trades.length === 0) {
      tbody.innerHTML = `<tr><td colspan="11" style="text-align: center; color: var(--text-dim); padding: 1.5rem;">No matching trade records.</td></tr>`;
      return;
    }

    data.trades.forEach(t => {
      const tr = document.createElement('tr');
      const pnl = parseFloat(t.realized_pnl_usdt || 0);
      const roe = parseFloat(t.roe_percentage || 0);
      const isProfit = pnl >= 0;

      tr.innerHTML = `
        <td class="mono">#${t.trade_id}</td>
        <td><strong class="${t.direction === 'LONG' ? 'profit' : 'cyan'}">${t.direction}</strong></td>
        <td class="mono">${t.entry_price}</td>
        <td class="mono">${t.exit_price}</td>
        <td class="mono ${isProfit ? 'profit' : 'loss'}"><strong>${isProfit ? '+' : ''}${pnl.toFixed(4)}</strong></td>
        <td class="mono ${isProfit ? 'profit' : 'loss'}">${roe >= 0 ? '+' : ''}${roe.toFixed(2)}%</td>
        <td class="mono">${formatSeconds(parseFloat(t.duration_seconds || 0))}</td>
        <td><span class="tag ${t.exit_reason === 'MIN_PROFIT_TP_HIT' ? 'tag-cyan' : 'loss'}">${t.exit_reason}</span></td>
        <td class="mono" style="font-size: 0.75rem; color: var(--text-dim);">${t.close_time}</td>
        <td class="mono">$${parseFloat(t.balance_after_trade_usdt || 0).toFixed(4)}</td>
        <td>
          <button class="btn btn-secondary btn-xs btn-inspect-trade" title="Open under Forensic Microscope & Historical Replay">
            <span>🔬</span> Inspect
          </button>
        </td>
      `;

      const btnInspect = tr.querySelector('.btn-inspect-trade');
      if (btnInspect) {
        btnInspect.addEventListener('click', async () => {
          switchToTab('forensics');
          if (window.forensicsLab) {
            const runSelect = document.getElementById('forensicRunSelect');
            if (runSelect && state.deepDiveRunId && window.forensicsLab.activeRunId !== state.deepDiveRunId) {
              runSelect.value = state.deepDiveRunId;
              window.forensicsLab.activeRunId = state.deepDiveRunId;
              await window.forensicsLab.onRunChanged();
            }
            window.forensicsLab.jumpToTrade(parseInt(t.trade_id, 10));
          }
        });
      }

      tbody.appendChild(tr);
    });
  } catch (e) {
    console.error('Error loading paged trades:', e);
  }
}

// Helpers
function debounce(fn, delay) {
  let timer = null;
  return function(...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), delay);
  };
}

function showToast(msg) {
  const toast = document.createElement('div');
  toast.style.position = 'fixed';
  toast.style.bottom = '24px';
  toast.style.right = '24px';
  toast.style.background = 'rgba(19, 28, 51, 0.95)';
  toast.style.backdropFilter = 'blur(16px)';
  toast.style.border = '1px solid var(--accent-cyan)';
  toast.style.color = '#fff';
  toast.style.padding = '0.75rem 1.25rem';
  toast.style.borderRadius = 'var(--radius-md)';
  toast.style.zIndex = '9999';
  toast.style.boxShadow = '0 10px 30px rgba(0,0,0,0.8)';
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 3500);
}
