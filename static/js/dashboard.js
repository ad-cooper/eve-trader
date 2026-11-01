let currentTab = 'performance';
let globalData = null;
let chartInstance = null;
let perfFilterMode = 'all';
let showSniperRaw = false;
let missingBuysData = null;
let multiSellData = null;
let multiSellMode = 'stock';

function fmtISK(val) {
  if (!val && val !== 0) return '0 ISK';
  if (Math.abs(val) >= 1e9) return (val / 1e9).toFixed(2) + 'B ISK';
  if (Math.abs(val) >= 1e6) return (val / 1e6).toFixed(1) + 'M ISK';
  return Math.round(val).toLocaleString() + ' ISK';
}

// Universal clipboard helper (works on localhost, HTTPS, and remote LAN HTTP)
async function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      console.warn('navigator.clipboard failed, attempting fallback', err);
    }
  }

  try {
    const textArea = document.createElement('textarea');
    textArea.value = text;
    textArea.style.position = 'fixed';
    textArea.style.left = '-999999px';
    textArea.style.top = '-999999px';
    textArea.setAttribute('readonly', '');
    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();
    const successful = document.execCommand('copy');
    textArea.remove();
    return successful;
  } catch (err) {
    console.error('Remote LAN copy fallback failed:', err);
    return false;
  }
}

async function fetchData() {
  try {
    const res = await fetch('/api/state');
    if (!res.ok) {
      console.error('Failed to fetch /api/state: HTTP', res.status);
      return;
    }
    globalData = await res.json();
    renderMetrics();
    renderActiveTab();
  } catch (err) {
    console.error('FetchData error:', err);
  }
}

function renderMetrics() {
  if (!globalData) return;
  const m = globalData.metrics;
  const c = globalData.character;
  
  document.getElementById('m-cap').innerText = fmtISK(m.total_capital);
  document.getElementById('m-prof').innerText = '+' + fmtISK(m.total_profit);
  document.getElementById('m-roi').innerText = m.blended_roi.toFixed(1) + '%';
  document.getElementById('m-res').innerText = fmtISK(m.liquid_reserve);
  document.getElementById('m-coverage').innerText = c.ledger_coverage;
  document.getElementById('m-orders-count').innerText = c.orders_count + ' orders';
  document.getElementById('m-escrow').innerText = 'Escrow: ' + fmtISK(c.total_escrow);

  document.getElementById('sniper-time').innerText = 'Last scan: ' + (globalData.sniper.timestamp || 'Never');
  document.getElementById('last-ledger-time').innerText = 'Last Ledger: ' + (globalData.status.last_ledger_run || 'Never');

  document.getElementById('spinner-snipe').classList.toggle('hidden', !globalData.status.sniper_running);
  document.getElementById('text-snipe').innerText = globalData.status.sniper_running ? 'Scanning...' : 'Run Deal Sniper';
  document.getElementById('spinner-ledger').classList.toggle('hidden', !globalData.status.ledger_running);
  document.getElementById('text-ledger').innerText = globalData.status.ledger_running ? 'Generating...' : 'Rebuild 50B';

  const sso = document.getElementById('sso-auth-container');
  if (c.is_linked) {
    sso.innerHTML = `
      <div class="flex items-center gap-2 bg-emerald-950/60 border border-emerald-600/60 px-3 py-1.5 rounded text-xs font-mono text-emerald-300">
        <img class="w-6 h-6 rounded-full border border-emerald-400" src="https://images.evetech.net/characters/${c.character_id}/portrait?size=64" />
        <span>${c.name}</span>
        <button onclick="triggerEsiSync()" class="ml-1 text-slate-400 hover:text-white" title="Refresh ESI">↻</button>
      </div>`;
  }
}

function switchTab(tab) {
  currentTab = tab;
  ['performance', 'sniper', 'evergreen', 'tactical', 'hold', 'guide'].forEach(t => {
    const btn = document.getElementById('tab-' + t);
    if (!btn) return;
    if (t === tab) {
      btn.className = `px-4 py-2 border-b-2 ${t === 'performance' ? 'border-evePink text-evePink' : 'border-eveCyan text-eveCyan'} font-semibold text-sm transition`;
    } else {
      btn.className = 'px-4 py-2 border-b-2 border-transparent text-slate-400 hover:text-slate-200 font-semibold text-sm transition';
    }
  });
  renderActiveTab();
}

function setPerfFilter(mode) {
  perfFilterMode = mode;
  ['all', 'winners', 'losers'].forEach(m => {
    const btn = document.getElementById('btn-perf-' + m);
    if (m === mode) {
      btn.className = 'px-3 py-1 rounded bg-evePink text-white font-semibold transition';
    } else {
      btn.className = 'px-3 py-1 rounded text-slate-400 hover:text-white transition';
    }
  });
  renderPerformanceTable();
}

function toggleSniperRaw() {
  showSniperRaw = !showSniperRaw;
  document.getElementById('sniper-raw-wrapper').classList.toggle('hidden', !showSniperRaw);
  document.getElementById('btn-toggle-raw').innerText = showSniperRaw ? 'Hide Console Log' : 'Show Console Log';
}

function renderActiveTab() {
  if (!globalData) return;
  const panels = ['performance', 'sniper', 'table', 'guide'];
  panels.forEach(p => {
    const el = document.getElementById('panel-' + p);
    if (el) el.classList.add('hidden');
  });

  document.getElementById('sniper-controls').classList.add('hidden');
  document.getElementById('table-filters').classList.add('hidden');

  if (currentTab === 'performance') {
    document.getElementById('panel-performance').classList.remove('hidden');
    renderPerformance(globalData.performance);
  } else if (currentTab === 'sniper') {
    document.getElementById('panel-sniper').classList.remove('hidden');
    document.getElementById('sniper-controls').classList.remove('hidden');
    renderSniperTable(globalData.sniper.deals || []);
    document.getElementById('sniper-output').innerText = globalData.sniper.raw_output || 'No scans recorded.';
  } else if (currentTab === 'guide') {
    document.getElementById('panel-guide').classList.remove('hidden');
  } else {
    document.getElementById('panel-table').classList.remove('hidden');
    document.getElementById('table-filters').classList.remove('hidden');

    const allDeskRows = globalData.desks[currentTab] || [];
    const deskCap = allDeskRows.reduce((sum, r) => sum + (parseFloat(r.capital) || 0), 0);
    const deskProf = allDeskRows.reduce((sum, r) => sum + (parseFloat(r.profit) || 0), 0);
    const deskRoi = deskCap > 0 ? (deskProf / deskCap * 100) : 0;

    const elCap = document.getElementById('ds-cap');
    const elProf = document.getElementById('ds-prof');
    const elRoi = document.getElementById('ds-roi');
    const elLbl = document.getElementById('ds-label');

    if (elCap) elCap.innerText = fmtISK(deskCap);
    if (elProf) elProf.innerText = '+' + fmtISK(deskProf);
    if (elRoi) elRoi.innerText = deskRoi.toFixed(1) + '%';
    if (elLbl) elLbl.innerText = `${currentTab.toUpperCase()} DESK`;

    let rows = allDeskRows;
    if (document.getElementById('filter-unplaced').checked) {
      rows = rows.filter(r => r.char_status === 'UNPLACED');
    }
    renderTableRows(rows);
  }
}

function renderPerformance(perf) {
  if (!perf) return;
  const k = perf.kpis;
  document.getElementById('perf-net').innerText = fmtISK(k.net_profit_30d);
  document.getElementById('perf-net-sub').innerText = fmtISK(k.net_profit_30d);
  document.getElementById('perf-income').innerText = fmtISK(k.trade_income_30d);
  document.getElementById('perf-purchases').innerText = '-' + fmtISK(k.trade_purchases_30d);
  document.getElementById('perf-margin').innerText = k.blended_margin.toFixed(1) + '%';
  renderChart(perf.chart);
  renderPerformanceTable();
}

function renderPerformanceTable() {
  if (!globalData?.performance?.items) return;
  const threshold = parseFloat(document.getElementById('perf-min-threshold').value) || 5000000;
  let items = globalData.performance.items.filter(it => Math.abs(it.profit) >= threshold);

  if (perfFilterMode === 'winners') items = items.filter(it => it.profit > 0).sort((a,b) => b.profit - a.profit);
  else if (perfFilterMode === 'losers') items = items.filter(it => it.profit < 0).sort((a,b) => a.profit - b.profit);
  else items.sort((a,b) => b.profit - a.profit);

  document.getElementById('perf-item-count').innerText = `${items.length} items (≥ ${fmtISK(threshold)})`;
  const tbody = document.getElementById('perf-table-body');
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="p-6 text-center text-slate-500">No items match criteria.</td></tr>`;
    return;
  }

  tbody.innerHTML = items.map((it, i) => {
    const isP = it.profit >= 0;
    return `
      <tr class="hover:bg-slate-800/40 transition">
        <td class="p-3 text-slate-500">${i + 1}</td>
        <td class="p-3 font-semibold text-white">
          <a href="https://evetycoon.com/market/${it.type_id}" target="_blank" rel="noopener noreferrer" class="hover:text-eveCyan hover:underline inline-flex items-center gap-1.5 transition">
            <span>${it.name}</span>
            <svg class="w-3 h-3 text-slate-500 inline shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"></path></svg>
          </a>
        </td>
        <td class="p-3 text-right text-slate-300 font-semibold">${it.qty_sold.toLocaleString()}</td>
        <td class="p-3 text-right text-slate-300">${fmtISK(it.revenue)}</td>
        <td class="p-3 text-right text-slate-400">${fmtISK(it.cost)}</td>
        <td class="p-3 text-right ${isP ? 'text-eveGreen font-bold' : 'text-eveRed font-bold'}">${isP ? '+' : '-'}${fmtISK(Math.abs(it.profit))}</td>
        <td class="p-3 text-right ${isP ? 'text-eveCyan' : 'text-eveRed'} font-semibold">${it.revenue > 0 ? (it.profit / it.revenue * 100).toFixed(1) : 0}%</td>
      </tr>`;
  }).join('');
}

function renderChart(chartData) {
  const ctx = document.getElementById('dailyProfitsChart').getContext('2d');
  if (chartInstance) {
    chartInstance.data.labels = chartData.labels;
    chartInstance.data.datasets[0].data = chartData.running_totals;
    chartInstance.data.datasets[1].data = chartData.daily_profits;
    chartInstance.data.datasets[1].backgroundColor = chartData.daily_profits.map(v => v >= 0 ? '#ec4899' : '#ef4444');
    chartInstance.update('none');
    return;
  }

  chartInstance = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: chartData.labels,
      datasets: [
        {
          type: 'line',
          label: '30d Running Total',
          data: chartData.running_totals,
          borderColor: '#94a3b8',
          backgroundColor: 'rgba(148, 163, 184, 0.1)',
          borderWidth: 2,
          pointRadius: 0,
          yAxisID: 'yRunning',
          tension: 0.2,
          fill: true
        },
        {
          type: 'bar',
          label: 'Daily Profit',
          data: chartData.daily_profits,
          backgroundColor: chartData.daily_profits.map(v => v >= 0 ? '#ec4899' : '#ef4444'),
          borderRadius: 3,
          yAxisID: 'yDaily'
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 600 },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${fmtISK(ctx.parsed.y)}` } }
      },
      scales: {
        x: { grid: { color: '#1e293b' }, ticks: { color: '#64748b', font: { family: 'monospace', size: 10 } } },
        yDaily: { position: 'left', grid: { color: '#1e293b' }, ticks: { color: '#ec4899', font: { family: 'monospace', size: 10 }, callback: v => fmtISK(v) } },
        yRunning: { position: 'right', grid: { display: false }, ticks: { color: '#94a3b8', font: { family: 'monospace', size: 10 }, callback: v => fmtISK(v) } }
      }
    }
  });
}

function renderSniperTable(deals) {
  const tbody = document.getElementById('sniper-table-body');
  if (!tbody) return;
  const countEl = document.getElementById('sniper-deal-count');
  if (countEl) countEl.innerText = `${deals ? deals.length : 0} deals detected`;

  if (!deals || !deals.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="p-6 text-center text-slate-500">No active firesales detected. Click "Run Deal Sniper" above to trigger a fresh scan.</td></tr>';
    return;
  }

  tbody.innerHTML = deals.map((d, i) => {
    const name = d.name || d.item_name || 'Unknown Item';
    const typeId = d.type_id || '';
    const qty = d.qty || d.quantity || 1;
    const discount = Math.abs(d.discount_pct || 0).toFixed(1);
    const askPrice = d.ask_price || 0;
    const targetExit = d.target_exit || d.target_sell || 0;
    const netMargin = (d.net_margin || 0).toFixed(1);
    const capital = d.capital || d.total_cost || 0;
    const profit = d.profit || d.est_profit || 0;
    const isFiresale = d.deal_type && d.deal_type.includes('FIRESALE');

    return `
    <tr class="hover:bg-slate-800/40 transition">
      <td class="p-3 text-slate-500">${i + 1}</td>
      <td class="p-3"><span class="${isFiresale ? 'bg-amber-950/70 text-amber-300 border-amber-600/80' : 'bg-cyan-950/70 text-cyan-300 border-cyan-600/80'} border px-2 py-0.5 rounded text-[10px] font-bold">${d.deal_type || 'DEAL'}</span></td>
      <td class="p-3 font-semibold text-white"><a href="https://evetycoon.com/market/${typeId}" target="_blank" class="hover:text-eveCyan hover:underline">${name}</a></td>
      <td class="p-3 text-right text-slate-300">${qty}</td>
      <td class="p-3 text-right text-amber-400 font-semibold">-${discount}%</td>
      <td class="p-3 text-right text-slate-200 font-semibold">${fmtISK(askPrice)}</td>
      <td class="p-3 text-right text-slate-300">${fmtISK(targetExit)}</td>
      <td class="p-3 text-right text-eveCyan font-bold">${netMargin}%</td>
      <td class="p-3 text-right text-slate-300 font-semibold">${fmtISK(capital)}</td>
      <td class="p-3 text-right text-eveGreen font-bold">+${fmtISK(profit)}</td>
    </tr>`;
  }).join('');
}

function renderTableRows(rows) {
  const tbody = document.getElementById('table-body');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="12" class="p-4 text-center text-slate-500">No matching items.</td></tr>';
    return;
  }

  tbody.innerHTML = rows.map((r, i) => {
    let badge = r.char_status === 'BUY_ACTIVE' 
      ? `<span class="bg-blue-900/60 text-blue-300 border border-blue-700/80 px-1.5 py-0.5 rounded text-[10px] font-bold">BUY ACTIVE</span>`
      : r.char_status === 'SELL_ACTIVE' 
      ? `<span class="bg-emerald-900/60 text-emerald-300 border border-emerald-700/80 px-1.5 py-0.5 rounded text-[10px] font-bold">SELL LISTED</span>`
      : `<span class="bg-slate-800 text-slate-400 border border-slate-700 px-1.5 py-0.5 rounded text-[10px]">UNPLACED</span>`;

    let proxBadge = '<span class="text-slate-600 font-mono">--</span>';
    let liveAskStr = r.live_ask ? fmtISK(r.live_ask) : "<span class=\"text-amber-400 font-bold text-[10px] bg-amber-950/70 border border-amber-800 px-1 py-0.5 rounded\">OUT OF STOCK</span>";

    if (r.proximity_pct !== null && r.proximity_pct !== undefined) {
      const p = r.proximity_pct;
      const diff = r.diff_pct;
      const diffSign = diff >= 0 ? '+' : '';

      if (p >= 99.9) {
        proxBadge = `
          <span class="bg-emerald-950/80 text-emerald-300 border border-emerald-600/80 px-2 py-0.5 rounded text-[10px] font-bold tracking-wider inline-flex items-center gap-1 font-mono">
            <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
            <span>TARGET MET 100.0%</span>
          </span>`;
      } else if (p >= 85) {
        proxBadge = `
          <span class="bg-cyan-950/80 text-cyan-300 border border-cyan-600/80 px-2 py-0.5 rounded text-[10px] font-bold tracking-wider inline-flex items-center gap-1 font-mono">
            <span class="w-1.5 h-1.5 rounded-full bg-cyan-400"></span>
            <span>${p.toFixed(1)}% (${diffSign}${diff.toFixed(1)}%)</span>
          </span>`;
      } else {
        proxBadge = `
          <span class="bg-amber-950/90 text-amber-300 border border-amber-600/80 px-2 py-0.5 rounded text-[10px] font-bold tracking-wider inline-flex items-center gap-1 font-mono">
            <span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>
            <span>DEEP DIP ${p.toFixed(1)}% (${diffSign}${diff.toFixed(1)}%)</span>
          </span>`;
      }
    }

    const isProf = (r.profit || 0) >= 0;
    return `
      <tr class="hover:bg-slate-800/40 transition">
        <td class="p-3 text-slate-500">${i + 1}</td>
        <td class="p-3">${badge}</td>
        <td class="p-3 font-semibold text-white"><a href="https://evetycoon.com/market/${r.type_id}" target="_blank" class="hover:text-eveCyan hover:underline">${r.name}</a></td>
        <td class="p-3 text-right text-slate-300">${r.qty}</td>
        <td class="p-3 text-right text-slate-400">${r.dump_freq.toFixed(1)}%</td>
        <td class="p-3 text-right text-slate-200">${fmtISK(r.target_buy)}</td>
        <td class="p-3 text-right text-slate-200 font-semibold">${fmtISK(r.target_sell)}</td>
        <td class="p-3 text-right text-slate-300 font-mono">${liveAskStr}</td>
        <td class="p-3 text-center">${proxBadge}</td>
        <td class="p-3 text-right ${isProf ? 'text-eveCyan font-semibold' : 'text-red-400 font-bold'}">${r.net_margin.toFixed(1)}%</td>
        <td class="p-3 text-right text-slate-300 font-semibold">${fmtISK(r.capital)}</td>
        <td class="p-3 text-right ${isProf ? 'text-eveGreen font-bold' : 'text-red-400 font-bold'}">${isProf ? '+' : ''}${fmtISK(r.profit)}</td>
      </tr>`;
  }).join('');
}

async function triggerSniper() {
  // Immediately switch view to the Deal Sniper tab
  if (typeof switchTab === 'function') {
    switchTab('sniper');
  }

  // Set scanning indicators
  const spinner = document.getElementById('spinner-snipe');
  const text = document.getElementById('text-snipe');
  if (spinner) spinner.classList.remove('hidden');
  if (text) text.innerText = 'Scanning...';

  try {
    await fetch('/api/sniper/run', { method: 'POST' });
  } catch (e) {
    console.error('Failed to trigger deal sniper:', e);
  }
}

async function triggerAskRefresh() {
  const spinner = document.getElementById('spinner-asks');
  const text = document.getElementById('text-asks');
  spinner.classList.remove('hidden');
  text.innerText = 'Polling Books...';
  try {
    await fetch('/api/market/refresh-prices', { method: 'POST' });
    setTimeout(fetchData, 2000);
  } finally {
    setTimeout(() => {
      spinner.classList.add('hidden');
      text.innerText = 'Poll Asks';
    }, 2500);
  }
}

async function triggerEsiSync() { await fetch('/api/orders/sync-esi', { method: 'POST' }); fetchData(); }
async function triggerLedgers() { await fetch('/api/ledgers/run', { method: 'POST' }); fetchData(); }

async function openMissingBuysModal() {
  const btn = document.getElementById('btn-missing-buys');
  const spinner = document.getElementById('spinner-missing');
  const btnText = document.getElementById('text-missing');
  
  spinner.classList.remove('hidden');
  btnText.innerText = 'Checking ESI Books...';
  btn.disabled = true;

  try {
    const deskParam = ['evergreen', 'tactical', 'hold'].includes(currentTab) ? currentTab : 'all';
    const res = await fetch(`/api/orders/missing-buys?desk=${deskParam}`);
    if (!res.ok) {
      throw new Error(`Server returned HTTP ${res.status}`);
    }
    missingBuysData = await res.json();

    if (!missingBuysData.items || !missingBuysData.items.length) {
      alert('All buy orders for this desk are already active and tracked in ESI!');
      return;
    }

    // Safely copy using LAN-compatible fallback
    if (missingBuysData.multibuy_text) {
      await copyToClipboard(missingBuysData.multibuy_text);
    }

    document.getElementById('modal-missing-count').innerText = `${missingBuysData.total_items} items`;
    document.getElementById('modal-missing-escrow').innerText = fmtISK(missingBuysData.total_escrow);
    
    const tbody = document.getElementById('modal-missing-body');
    tbody.innerHTML = missingBuysData.items.map((it, i) => `
      <tr class="hover:bg-slate-800/40 transition">
        <td class="p-2.5 text-slate-500">${i + 1}</td>
        <td class="p-2.5 font-semibold text-white">
          <a href="https://evetycoon.com/market/${it.type_id}" target="_blank" class="hover:text-eveCyan hover:underline">${it.name}</a>
          ${it.squeezed ? '<span class="ml-1 text-[9px] bg-red-950 text-red-400 border border-red-800 px-1 py-0.2 rounded font-bold">MARGIN CAP</span>' : ''}
        </td>
        <td class="p-2.5 text-right text-slate-300 font-semibold">${it.qty.toLocaleString()}</td>
        <td class="p-2.5 text-right text-slate-400 font-mono">${fmtISK(it.top_buy)}</td>
        <td class="p-2.5 text-right text-eveCyan font-bold font-mono">${fmtISK(it.recommended_buy)}</td>
        <td class="p-2.5 text-right text-eveGreen font-semibold">${it.margin_pct}%</td>
        <td class="p-2.5 text-right text-slate-300 font-mono">${fmtISK(it.line_escrow)}</td>
        <td class="p-2.5 text-center">
          <button onclick="copySinglePrice(${it.recommended_buy}, this)" class="bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-600 px-2 py-0.5 rounded text-[10px] font-mono transition">
            Copy
          </button>
        </td>
      </tr>
    `).join('');

    document.getElementById('modal-missing').classList.remove('hidden');
  } catch (err) {
    console.error('Missing buys error:', err);
    alert('Failed to audit live ESI market books: ' + err.message);
  } finally {
    spinner.classList.add('hidden');
    btnText.innerText = 'Missing Buy Orders';
    btn.disabled = false;
  }
}

function closeMissingBuysModal() {
  document.getElementById('modal-missing').classList.add('hidden');
}

async function copyMultibuyText(btn) {
  if (missingBuysData && missingBuysData.multibuy_text) {
    const success = await copyToClipboard(missingBuysData.multibuy_text);
    const orig = btn.innerText;
    btn.innerText = success ? 'Copied to Clipboard!' : 'Copy Failed';
    setTimeout(() => { btn.innerText = orig; }, 2000);
  }
}

async function copySinglePrice(price, btn) {
  const success = await copyToClipboard(price.toString());
  const orig = btn.innerText;
  btn.innerText = success ? 'Copied!' : 'Failed';
  if (success) btn.classList.add('text-eveGreen', 'border-emerald-600');
  setTimeout(() => {
    btn.innerText = orig;
    btn.classList.remove('text-eveGreen', 'border-emerald-600');
  }, 1200);
}

function openMultiSellModal() {
  document.getElementById('modal-multisell').classList.remove('hidden');
  calculateMultiSell();
}

function closeMultiSellModal() {
  document.getElementById('modal-multisell').classList.add('hidden');
}

function setMultiSellTab(mode) {
  multiSellMode = mode;
  document.getElementById('tab-ms-stock').className = mode === 'stock'
    ? 'px-3 py-1.5 border-b-2 border-emerald-400 text-emerald-400 font-bold transition'
    : 'px-3 py-1.5 border-b-2 border-transparent text-slate-400 hover:text-white transition';

  document.getElementById('tab-ms-paste').className = mode === 'paste'
    ? 'px-3 py-1.5 border-b-2 border-emerald-400 text-emerald-400 font-bold transition'
    : 'px-3 py-1.5 border-b-2 border-transparent text-slate-400 hover:text-white transition';

  document.getElementById('ms-stock-view').classList.toggle('hidden', mode !== 'stock');
  document.getElementById('ms-paste-view').classList.toggle('hidden', mode !== 'paste');
}

async function calculateMultiSell() {
  const spinner = document.getElementById('spinner-multisell');
  const btnText = document.getElementById('text-multisell');
  spinner.classList.remove('hidden');
  btnText.innerText = 'Pricing Books...';

  try {
    let payload = {};
    if (multiSellMode === 'paste') {
      const text = document.getElementById('ms-paste-input').value;
      if (!text.trim()) {
        alert('Please paste exported items from EVE MultiSell first.');
        spinner.classList.add('hidden');
        btnText.innerText = 'Calculate MultiSell Prices';
        return;
      }
      payload.raw_text = text;
    }

    const res = await fetch('/api/orders/multisell-price', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    multiSellData = await res.json();

    if (!multiSellData.items || !multiSellData.items.length) {
      document.getElementById('modal-ms-body').innerHTML = `
        <tr><td colspan="7" class="p-6 text-center text-slate-500">
          No stock items found in Jita 4-4 hangar. If using Export, switch to the "Paste MultiSell Export" tab above!
        </td></tr>`;
      document.getElementById('modal-ms-count').innerText = '0 items';
      document.getElementById('modal-ms-value').innerText = '0 ISK';
      return;
    }

    if (multiSellData.multisell_text) {
      await copyToClipboard(multiSellData.multisell_text);
    }

    document.getElementById('modal-ms-count').innerText = `${multiSellData.total_items} items`;
    document.getElementById('modal-ms-value').innerText = fmtISK(multiSellData.total_value);

    const tbody = document.getElementById('modal-ms-body');
    tbody.innerHTML = multiSellData.items.map((it, i) => `
      <tr class="hover:bg-slate-800/40 transition">
        <td class="p-2.5 text-slate-500">${i + 1}</td>
        <td class="p-2.5 font-semibold text-white">
          <a href="https://evetycoon.com/market/${it.type_id}" target="_blank" class="hover:text-eveCyan hover:underline">${it.name}</a>
        </td>
        <td class="p-2.5 text-right text-slate-300 font-semibold">${it.qty.toLocaleString()}</td>
        <td class="p-2.5 text-right text-slate-400 font-mono">${fmtISK(it.lowest_ask)}</td>
        <td class="p-2.5 text-right text-emerald-400 font-bold font-mono">${fmtISK(it.recommended_sell)}</td>
        <td class="p-2.5 text-right text-slate-300 font-mono">${fmtISK(it.line_value)}</td>
        <td class="p-2.5 text-center">
          <button onclick="copySinglePrice(${it.recommended_sell}, this)" class="bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-600 px-2 py-0.5 rounded text-[10px] font-mono transition">
            Copy
          </button>
        </td>
      </tr>
    `).join('');

  } catch (err) {
    console.error('MultiSell error:', err);
    alert('Failed to compute MultiSell prices.');
  } finally {
    spinner.classList.add('hidden');
    btnText.innerText = 'Recalculate Prices';
  }
}

async function copyMultiSellImportText(btn) {
  if (multiSellData && multiSellData.multisell_text) {
    const success = await copyToClipboard(multiSellData.multisell_text);
    const orig = btn.innerText;
    btn.innerText = success ? 'Copied to Clipboard!' : 'Copy Failed';
    setTimeout(() => { btn.innerText = orig; }, 2000);
  }
}

setInterval(fetchData, 3000);
fetchData();

// ==========================================
// UNIFIED MULTISELL & INVENTORY ADVISOR
// ==========================================
let advisorData = null;
let currentAdvisorFilter = 'all';

async function openInventoryAdvisor(customText = null) {
  const modal = document.getElementById('advisor-modal');
  if (!modal) return;
  modal.classList.remove('hidden');

  const tbody = document.getElementById('advisor-table-body');
  tbody.innerHTML = '<tr><td colspan="8" class="text-center py-8 text-slate-400 font-sans"><div class="inline-block animate-spin mr-2">⟳</div> Scanning inventory, live Jita books & cost basis...</td></tr>';

  try {
    let res;
    if (customText && customText.trim()) {
      res = await fetch('/api/inventory/advisor', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ raw_text: customText })
      });
    } else {
      res = await fetch('/api/inventory/advisor');
    }
    if (!res.ok) throw new Error('API returned status ' + res.status);
    advisorData = await res.json();
    renderAdvisorView();
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="8" class="text-center py-8 text-rose-400 font-sans">Error loading inventory: ${err.message}</td></tr>`;
  }
}

window.openMultiSellModal = () => openInventoryAdvisor();
window.openMultisell = () => openInventoryAdvisor();

function closeInventoryAdvisor() {
  const modal = document.getElementById('advisor-modal');
  if (modal) modal.classList.add('hidden');
}

function toggleCustomPasteDrawer() {
  const drawer = document.getElementById('custom-paste-drawer');
  if (drawer) drawer.classList.toggle('hidden');
}

function submitCustomPaste() {
  const textarea = document.getElementById('adv-custom-paste-text');
  if (!textarea || !textarea.value.trim()) return;
  openInventoryAdvisor(textarea.value.trim());
}

function reloadHangarAssets() {
  const textarea = document.getElementById('adv-custom-paste-text');
  if (textarea) textarea.value = '';
  const drawer = document.getElementById('custom-paste-drawer');
  if (drawer) drawer.classList.add('hidden');
  openInventoryAdvisor(null);
}

function filterAdvisor(filterType) {
  currentAdvisorFilter = filterType;
  ['all', 'ready', 'hold', 'unmanaged'].forEach(t => {
    const btn = document.getElementById(`tab-adv-${t}`);
    if (btn) {
      if (t === filterType) {
        btn.className = "px-2.5 py-1 rounded font-semibold bg-slate-800 text-slate-200 transition";
      } else {
        btn.className = "px-2.5 py-1 rounded font-semibold text-slate-400 hover:text-slate-200 transition";
      }
    }
  });
  renderAdvisorTable();
}

function renderAdvisorView() {
  if (!advisorData) return;

  const totalCount = advisorData.items ? advisorData.items.length : 0;
  const readyItems = (advisorData.items || []).filter(i => ['flip', 'met', 'unmanaged'].includes(i.action_type));

  const totalValEl = document.getElementById('adv-total-val');
  if (totalValEl) totalValEl.textContent = fmtISK(advisorData.total_value);

  const totalCountEl = document.getElementById('adv-total-count');
  if (totalCountEl) totalCountEl.textContent = `${totalCount} items evaluated`;

  const costValEl = document.getElementById('adv-cost-val');
  if (costValEl) costValEl.textContent = fmtISK(advisorData.total_cost);

  const pCard = document.getElementById('adv-profit-card');
  const pVal = document.getElementById('adv-profit-val');
  const mVal = document.getElementById('adv-margin-val');
  const isProfit = (advisorData.total_profit || 0) >= 0;

  if (pVal) pVal.textContent = (isProfit ? '+' : '') + fmtISK(advisorData.total_profit);
  const blendedMargin = advisorData.total_cost > 0 ? (advisorData.total_profit / advisorData.total_cost * 100).toFixed(1) : '0.0';
  if (mVal) mVal.textContent = `${isProfit ? '+' : ''}${blendedMargin}% net return`;

  if (pCard && pVal && mVal) {
    if (isProfit) {
      pCard.className = "bg-emerald-950/20 border border-emerald-800/40 rounded-lg p-3";
      pVal.className = "text-base font-bold text-emerald-300 mt-0.5";
      mVal.className = "text-[11px] text-emerald-400/80";
    } else {
      pCard.className = "bg-rose-950/20 border border-rose-800/40 rounded-lg p-3";
      pVal.className = "text-base font-bold text-rose-300 mt-0.5";
      mVal.className = "text-[11px] text-rose-400/80";
    }
  }

  const readyValEl = document.getElementById('adv-ready-val');
  if (readyValEl) readyValEl.textContent = fmtISK(advisorData.ready_value);

  const readyCountEl = document.getElementById('adv-ready-count');
  if (readyCountEl) readyCountEl.textContent = `${readyItems.length} items ready to list`;

  renderAdvisorTable();
}

function renderAdvisorTable() {
  if (!advisorData) return;
  const tbody = document.getElementById('advisor-table-body');
  if (!tbody) return;

  let rows = advisorData.items || [];
  if (currentAdvisorFilter === 'ready') {
    rows = rows.filter(i => ['flip', 'met', 'unmanaged'].includes(i.action_type));
  } else if (currentAdvisorFilter === 'hold') {
    rows = rows.filter(i => i.action_type === 'hold');
  } else if (currentAdvisorFilter === 'unmanaged') {
    rows = rows.filter(i => i.desk === 'UNMANAGED');
  }

  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="text-center py-8 text-slate-500 font-sans">No loose items matching this filter. (Items inside containers or other regions are ignored)</td></tr>';
    return;
  }

  let html = '';
  rows.forEach(item => {
    let deskBadge = '<span class="text-slate-400 font-semibold text-[10px] bg-slate-800 px-1.5 py-0.5 rounded border border-slate-700">UNMANAGED</span>';
    if (item.desk === 'EVERGREEN') {
      deskBadge = '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-cyan-950/70 text-cyan-300 border border-cyan-800">EVERGREEN</span>';
    } else if (item.desk === 'TACTICAL') {
      deskBadge = '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-purple-950/70 text-purple-300 border border-purple-800">TACTICAL</span>';
    } else if (item.desk === 'HOLD') {
      deskBadge = '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-950/70 text-amber-300 border border-amber-800">HOLD</span>';
    }

    let actionBadge = '';
    if (item.action_type === 'flip') {
      actionBadge = '<span class="px-2 py-0.5 rounded font-bold text-[10px] bg-emerald-950/80 border border-emerald-700 text-emerald-300">FAST FLIP</span>';
    } else if (item.action_type === 'met') {
      actionBadge = '<span class="px-2 py-0.5 rounded font-bold text-[10px] bg-blue-950/80 border border-blue-700 text-blue-300">TARGET MET</span>';
    } else if (item.action_type === 'hold') {
      actionBadge = '<span class="px-2 py-0.5 rounded font-bold text-[10px] bg-amber-950/80 border border-amber-700 text-amber-300">HOLD CYCLE</span>';
    } else {
      actionBadge = '<span class="px-2 py-0.5 rounded font-bold text-[10px] bg-indigo-950/80 border border-indigo-700 text-indigo-300">MARKET EXIT</span>';
    }

    let costDisplay = '<span class="text-slate-500 font-sans">--</span>';
    if (item.unit_cost > 0) {
      let tagColor = item.cost_source === 'WALLET' ? 'text-cyan-400' : (item.cost_source === 'LEDGER' ? 'text-amber-400' : 'text-slate-400');
      costDisplay = `<div>${fmtISK(item.unit_cost)}</div><div class="text-[9px] ${tagColor}">${item.cost_source}</div>`;
    }

    let plDisplay = '';
    if (item.cost_source === 'UNKNOWN') {
      plDisplay = `<span class="text-slate-400 font-bold">+${fmtISK(item.profit)}</span><div class="text-[9px] text-slate-500">Gross Proceeds</div>`;
    } else if (item.profit >= 0) {
      plDisplay = `<span class="text-emerald-400 font-bold">+${fmtISK(item.profit)}</span><div class="text-[9px] text-emerald-500/80 font-bold">+${item.margin_pct}%</div>`;
    } else {
      plDisplay = `<span class="text-rose-400 font-bold">${fmtISK(item.profit)}</span><div class="text-[9px] text-rose-500/80 font-bold">${item.margin_pct}%</div>`;
    }

    const liveAskStr = item.lowest_ask ? fmtISK(item.lowest_ask) : '<span class="text-amber-500 font-sans font-bold text-[10px]">STOCKOUT</span>';

    html += `
      <tr class="hover:bg-slate-800/40 transition">
        <td class="py-2.5 px-3 text-slate-200 font-medium font-sans">${item.name}</td>
        <td class="py-2.5 px-2">${deskBadge}</td>
        <td class="py-2.5 px-2 text-right text-slate-300">${item.qty}</td>
        <td class="py-2.5 px-2 text-right font-sans">${costDisplay}</td>
        <td class="py-2.5 px-2 text-right text-slate-300">${liveAskStr}</td>
        <td class="py-2.5 px-2 text-right font-bold text-slate-100">${fmtISK(item.recommended_sell)}</td>
        <td class="py-2.5 px-3 text-right font-sans">${plDisplay}</td>
        <td class="py-2.5 px-3 text-center">${actionBadge}</td>
      </tr>
    `;
  });

  tbody.innerHTML = html;
}

function copyAdvisorMultisell(type) {
  if (!advisorData) return;
  const text = (type === 'ready') ? advisorData.ready_multisell_text : advisorData.multisell_text;
  navigator.clipboard.writeText(text).then(() => {
    alert(`Copied ${type === 'ready' ? 'Ready-to-Sell' : 'All Strategy'} items in EVE MultiSell format!`);
  });
}
