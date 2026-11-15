import { html, useState, useEffect, useMemo, useRef } from '../lib.js';
import { fmtISK } from '../utils.js';

function DailyProfitsChart({ chartData }) {
  const canvasRef = useRef(null);
  const instanceRef = useRef(null);

  const chartSig = useMemo(
    () => (chartData ? JSON.stringify([chartData.labels, chartData.running_totals, chartData.daily_profits]) : ''),
    [chartData]
  );

  useEffect(() => {
    if (!chartData || !canvasRef.current || typeof Chart === 'undefined') return;
    const ctx = canvasRef.current.getContext('2d');

    if (instanceRef.current) {
      instanceRef.current.data.labels = chartData.labels;
      instanceRef.current.data.datasets[0].data = chartData.running_totals;
      instanceRef.current.data.datasets[1].data = chartData.daily_profits;
      instanceRef.current.data.datasets[1].backgroundColor = chartData.daily_profits.map(v => (v >= 0 ? '#ec4899' : '#ef4444'));
      instanceRef.current.update('none');
      return;
    }

    instanceRef.current = new Chart(ctx, {
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
            fill: true,
          },
          {
            type: 'bar',
            label: 'Daily Profit',
            data: chartData.daily_profits,
            backgroundColor: chartData.daily_profits.map(v => (v >= 0 ? '#ec4899' : '#ef4444')),
            borderRadius: 3,
            yAxisID: 'yDaily',
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        devicePixelRatio: Math.min(window.devicePixelRatio || 1, 1.5),
        animation: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: c => `${c.dataset.label}: ${fmtISK(c.parsed.y)}` } },
        },
        scales: {
          x: { grid: { color: '#1e293b' }, ticks: { color: '#64748b', font: { family: 'monospace', size: 10 } } },
          yDaily: { position: 'left', grid: { color: '#1e293b' }, ticks: { color: '#ec4899', font: { family: 'monospace', size: 10 }, callback: v => fmtISK(v) } },
          yRunning: { position: 'right', grid: { display: false }, ticks: { color: '#94a3b8', font: { family: 'monospace', size: 10 }, callback: v => fmtISK(v) } },
        },
      },
    });
  }, [chartSig]);

  useEffect(() => {
    return () => {
      if (instanceRef.current) {
        instanceRef.current.destroy();
        instanceRef.current = null;
      }
    };
  }, []);

  return html`<canvas ref=${canvasRef}></canvas>`;
}

export function PerformanceTab({ performance }) {
  const [filterMode, setFilterMode] = useState('all');
  const [minThreshold, setMinThreshold] = useState(5000000);

  const k = performance?.kpis || {};

  const filteredItems = useMemo(() => {
    let items = (performance?.items || []).filter(it => Math.abs(it.profit) >= minThreshold);
    if (filterMode === 'winners') {
      items = items
        .filter(it => it.profit > 0)
        .sort((a, b) => (a.name || a.type_name || '').localeCompare(b.name || b.type_name || ''));
    } else if (filterMode === 'losers') {
      items = items.filter(it => it.profit < 0).sort((a, b) => a.profit - b.profit);
    } else {
      items = [...items].sort((a, b) => (a.name || a.type_name || '').localeCompare(b.name || b.type_name || ''));
    }
    return items;
  }, [performance, filterMode, minThreshold]);

  return html`
    <div class="space-y-6">
      <div class="grid grid-cols-1 md:grid-cols-4 gap-4 font-mono">
        <div class="bg-eveCard border border-eveBorder p-4 rounded-lg">
          <div class="text-xs text-slate-400 uppercase">30d Realized Net Profit</div>
          <div class="text-2xl font-bold text-eveGreen mt-1">${performance ? fmtISK(k.net_profit_30d) : '--'}</div>
          <div class="text-[11px] text-slate-500 mt-1">Trade: <span class="text-slate-300">${performance ? fmtISK(k.net_profit_30d) : '--'}</span></div>
        </div>
        <div class="bg-eveCard border border-eveBorder p-4 rounded-lg">
          <div class="text-xs text-slate-400 uppercase">30d Trade Income (Sales)</div>
          <div class="text-2xl font-bold text-white mt-1">${performance ? fmtISK(k.trade_income_30d) : '--'}</div>
          <div class="text-[11px] text-slate-500 mt-1">Gross Inflow</div>
        </div>
        <div class="bg-eveCard border border-eveBorder p-4 rounded-lg">
          <div class="text-xs text-slate-400 uppercase">30d Trade Purchases</div>
          <div class="text-2xl font-bold text-eveRed mt-1">${performance ? `-${fmtISK(k.trade_purchases_30d)}` : '--'}</div>
          <div class="text-[11px] text-slate-500 mt-1">Capital Outflow</div>
        </div>
        <div class="bg-eveCard border border-eveBorder p-4 rounded-lg">
          <div class="text-xs text-slate-400 uppercase">Realized Profit Margin</div>
          <div class="text-2xl font-bold text-eveCyan mt-1">${performance ? `${(k.blended_margin || 0).toFixed(1)}%` : '--'}</div>
          <div class="text-[11px] text-slate-500 mt-1">Net Yield After Taxes</div>
        </div>
      </div>

      <div class="bg-eveCard border border-eveBorder p-5 rounded-lg">
        <div class="flex justify-between items-center mb-4">
          <h3 class="text-sm font-mono font-bold uppercase tracking-wider text-white">Daily Trade Profits (Last 30 Days)</h3>
        </div>
        <div class="h-64 md:h-72 w-full">
          <${DailyProfitsChart} chartData=${performance?.chart} />
        </div>
      </div>

      <div class="bg-eveCard border border-eveBorder rounded-lg overflow-hidden font-mono">
        <div class="p-4 bg-slate-900/60 border-b border-eveBorder flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3">
          <div class="flex items-center gap-3">
            <div class="text-xs font-bold uppercase text-white">Trade Profits by Item (Realized FIFO)</div>
            <span class="text-xs text-slate-500">${filteredItems.length} items (≥ ${fmtISK(minThreshold)})</span>
          </div>
          <div class="flex flex-wrap items-center gap-3">
            <div class="flex bg-slate-800 rounded p-0.5 border border-slate-700 text-xs">
              ${['all', 'winners', 'losers'].map(mode => html`
                <button
                  onClick=${() => setFilterMode(mode)}
                  class=${filterMode === mode
                    ? 'px-3 py-1 rounded bg-evePink text-white font-semibold transition'
                    : 'px-3 py-1 rounded text-slate-400 hover:text-white transition'}
                >
                  ${mode === 'all' ? 'All' : mode === 'winners' ? 'Top Winners' : 'Top Losers'}
                </button>
              `)}
            </div>
            <select
              value=${minThreshold}
              onChange=${e => setMinThreshold(parseFloat(e.target.value) || 5000000)}
              class="bg-slate-800 border border-slate-700 text-slate-200 rounded px-2.5 py-1 text-xs font-mono"
            >
              <option value="5000000">≥ 5M ISK</option>
              <option value="10000000">≥ 10M ISK</option>
              <option value="25000000">≥ 25M ISK</option>
              <option value="1000000">≥ 1M ISK</option>
            </select>
          </div>
        </div>

        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead class="bg-slate-900 border-b border-eveBorder text-slate-400 uppercase">
              <tr>
                <th class="p-3">#</th>
                <th class="p-3">Item Name</th>
                <th class="p-3 text-right">Quantity Sold</th>
                <th class="p-3 text-right">Gross Revenue</th>
                <th class="p-3 text-right">FIFO Cost</th>
                <th class="p-3 text-right">Net Profit</th>
                <th class="p-3 text-right">Margin %</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800">
              ${!filteredItems.length
                ? html`<tr><td colspan="7" class="p-6 text-center text-slate-500">No items match criteria.</td></tr>`
                : filteredItems.map((it, i) => {
                    const isP = it.profit >= 0;
                    const margin = it.revenue > 0 ? ((it.profit / it.revenue) * 100).toFixed(1) : 0;
                    return html`
                      <tr key=${it.type_id || i} class="hover:bg-slate-800/40 transition">
                        <td class="p-3 text-slate-500">${i + 1}</td>
                        <td class="p-3 font-semibold text-white">
                          <a href=${`https://evetycoon.com/market/${it.type_id}`} target="_blank" rel="noopener noreferrer" class="hover:text-eveCyan hover:underline inline-flex items-center gap-1.5 transition">
                            <span>${it.name}</span>
                            <svg class="w-3 h-3 text-slate-500 inline shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"></path></svg>
                          </a>
                        </td>
                        <td class="p-3 text-right text-slate-300 font-semibold">${Number(it.qty_sold || 0).toLocaleString()}</td>
                        <td class="p-3 text-right text-slate-300">${fmtISK(it.revenue)}</td>
                        <td class="p-3 text-right text-slate-400">${fmtISK(it.cost)}</td>
                        <td class=${`p-3 text-right font-bold ${isP ? 'text-eveGreen' : 'text-eveRed'}`}>${isP ? '+' : '-'}${fmtISK(Math.abs(it.profit))}</td>
                        <td class=${`p-3 text-right font-semibold ${isP ? 'text-eveCyan' : 'text-eveRed'}`}>${margin}%</td>
                      </tr>
                    `;
                  })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  `;
}
