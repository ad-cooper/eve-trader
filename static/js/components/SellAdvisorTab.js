import { html, useState, useEffect, useMemo, useCallback } from '../lib.js';
import { fmtISK, copyToClipboard } from '../utils.js';
import { DeskBadge, MarketLink, FilterPills } from './ui.js';

function fmtPreciseISK(val) {
  if (val === null || val === undefined) return '--';
  const num = Number(val);
  if (!num && num !== 0) return '--';
  if (Math.abs(num) >= 1e9) return (num / 1e9).toFixed(3) + 'B ISK';
  if (Math.abs(num) >= 1e6) return (num / 1e6).toFixed(2) + 'M ISK';
  return num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ISK';
}

export function SellAdvisorTab() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  const [filter, setFilter] = useState('all');
  const [pasteOpen, setPasteOpen] = useState(false);
  const [pasteText, setPasteText] = useState('');
  const [copyStatus, setCopyStatus] = useState('');

  const loadAdvisor = useCallback(async (customText = null, forceRefresh = false) => {
    setLoading(true);
    setErrorMsg('');
    try {
      let res;
      if (customText && customText.trim()) {
        res = await fetch('/api/inventory/advisor', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ raw_text: customText }),
        });
      } else {
        const url = forceRefresh ? '/api/inventory/advisor?force=true' : '/api/inventory/advisor';
        res = await fetch(url);
      }
      if (!res.ok) throw new Error(`API returned status ${res.status}`);
      setData(await res.json());
    } catch (err) {
      setErrorMsg(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadAdvisor(null, false);
  }, [loadAdvisor]);

  const rows = useMemo(() => {
    let list = [...(data?.items || [])];
    if (filter === 'ready') {
      list = list.filter(i => ['flip', 'met', 'unmanaged', 'time_stop'].includes(i.action_type));
    } else if (filter === 'hold') {
      list = list.filter(i => i.action_type === 'hold');
    } else if (filter === 'unmanaged') {
      list = list.filter(i => i.desk === 'UNMANAGED');
    }
    list.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    return list;
  }, [data, filter]);

  const totalCount = data?.items?.length || 0;
  const readyCount = (data?.items || []).filter(i => ['flip', 'met', 'unmanaged', 'time_stop'].includes(i.action_type)).length;
  const isProfit = (data?.total_profit || 0) >= 0;
  const blendedMargin = data?.total_cost > 0 ? ((data.total_profit / data.total_cost) * 100).toFixed(1) : '0.0';

  async function handleCopyMultisell(type) {
    if (!data?.items) return;
    let items = [...data.items];
    if (type === 'ready') {
      items = items.filter(i => ['flip', 'met', 'unmanaged', 'time_stop'].includes(i.action_type));
    }
    items.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    const text = items.map(i => `${i.name}\t${Number(i.recommended_sell).toFixed(2)}`).join('\n');
    if (!text.trim()) {
      setCopyStatus('empty');
      setTimeout(() => setCopyStatus(''), 2000);
      return;
    }
    const ok = await copyToClipboard(text);
    setCopyStatus(ok ? type : 'failed');
    setTimeout(() => setCopyStatus(''), 2500);
  }

  return html`
    <div class="space-y-4">
      <div class="grid grid-cols-2 md:grid-cols-4 gap-4 font-mono">
        <div class="bg-eveCard border border-eveBorder rounded-lg p-4">
          <div class="text-xs text-slate-400 uppercase">Gross Exit Value</div>
          <div class="text-xl font-bold text-slate-100 mt-1">${data ? fmtISK(data.total_value) : '--'}</div>
          <div class="text-[11px] text-slate-400 mt-0.5">${totalCount} hangar lines evaluated</div>
        </div>
        <div class="bg-eveCard border border-eveBorder rounded-lg p-4">
          <div class="text-xs text-slate-400 uppercase">Invested Cost Basis</div>
          <div class="text-xl font-bold text-slate-300 mt-1">${data ? fmtISK(data.total_cost) : '--'}</div>
          <div class="text-[11px] text-slate-400 mt-0.5">FIFO / Wallet Fill Cost</div>
        </div>
        <div class=${isProfit ? 'bg-emerald-950/20 border border-emerald-800/40 rounded-lg p-4' : 'bg-rose-950/20 border border-rose-800/40 rounded-lg p-4'}>
          <div class="text-xs text-emerald-400 uppercase">Net Est. Profit / Loss</div>
          <div class=${`text-xl font-bold mt-1 ${isProfit ? 'text-emerald-300' : 'text-rose-300'}`}>${data ? `${isProfit ? '+' : ''}${fmtISK(data.total_profit)}` : '--'}</div>
          <div class=${`text-[11px] mt-0.5 ${isProfit ? 'text-emerald-400/80' : 'text-rose-400/80'}`}>${isProfit ? '+' : ''}${blendedMargin}% net return</div>
        </div>
        <div class="bg-indigo-950/20 border border-indigo-800/40 rounded-lg p-4">
          <div class="text-xs text-indigo-400 uppercase">Ready to Liquidate</div>
          <div class="text-xl font-bold text-indigo-300 mt-1">${data ? fmtISK(data.ready_value) : '--'}</div>
          <div class="text-[11px] text-indigo-400/80 mt-0.5">${readyCount} items ready for MultiSell</div>
        </div>
      </div>

      <div class="bg-eveCard border border-eveBorder rounded-lg p-3.5 flex flex-wrap items-center justify-between gap-3">
        <${FilterPills}
          options=${[
            ['all', `All Items (${totalCount})`],
            ['ready', `Ready to Sell (${readyCount})`],
            ['hold', 'Hold Cycle'],
            ['unmanaged', 'Unmanaged / Custom'],
          ]}
          value=${filter}
          onChange=${setFilter}
        />

        <div class="flex flex-wrap items-center gap-2">
          <button onClick=${() => setPasteOpen(v => !v)} class="px-3 py-1.5 text-xs rounded bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition">
            📋 Custom Paste
          </button>
          <button disabled=${loading} onClick=${() => { setPasteText(''); setPasteOpen(false); loadAdvisor(null, true); }} class="px-3 py-1.5 text-xs rounded bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition">
            ${loading ? '⟳ Scanning...' : '⟳ Refresh Hangar'}
          </button>
          <button onClick=${() => handleCopyMultisell('ready')} class="px-3.5 py-1.5 bg-emerald-600/30 hover:bg-emerald-600/50 border border-emerald-500/50 text-emerald-200 rounded text-xs font-bold transition">
            ${copyStatus === 'ready' ? '✓ Copied Ready MultiSell!' : 'Copy Ready-to-Sell (MultiSell)'}
          </button>
          <button onClick=${() => handleCopyMultisell('all')} class="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 border border-slate-600 text-slate-200 rounded text-xs font-semibold transition">
            ${copyStatus === 'all' ? '✓ Copied All Prices!' : 'Copy All Strategy Prices'}
          </button>
        </div>
      </div>

      ${pasteOpen && html`
        <div class="bg-slate-950 border border-indigo-500/40 rounded-lg p-4 space-y-2">
          <div class="text-xs text-slate-300 font-medium">Paste in-game inventory list or item quantities to price manually:</div>
          <textarea
            rows="3"
            value=${pasteText}
            onInput=${e => setPasteText(e.target.value)}
            placeholder=${'Gila 2\nPithum C-Type Medium Shield Booster 5'}
            class="w-full bg-slate-900 border border-slate-700 rounded-lg p-2.5 text-xs text-slate-200 font-mono focus:outline-none focus:border-indigo-500"
          ></textarea>
          <div class="flex justify-end gap-2">
            <button onClick=${() => setPasteOpen(false)} class="px-3 py-1 bg-slate-800 text-xs text-slate-400 hover:text-slate-200 rounded">Cancel</button>
            <button onClick=${() => pasteText.trim() && loadAdvisor(pasteText.trim(), false)} class="px-3 py-1 bg-indigo-600 hover:bg-indigo-500 text-xs text-white font-semibold rounded">Analyze & Price MultiSell</button>
          </div>
        </div>
      `}

      <div class="bg-eveCard border border-eveBorder rounded-lg overflow-hidden">
        <div class="overflow-x-auto">
          <table class="w-full text-left border-collapse text-xs">
            <thead class="bg-slate-900 border-b border-eveBorder text-slate-400 text-[11px] uppercase tracking-wider">
              <tr>
                <th class="py-3 px-3">Item Name</th>
                <th class="py-3 px-2">Desk</th>
                <th class="py-3 px-2 text-right">Qty</th>
                <th class="py-3 px-2 text-right">Cost Basis</th>
                <th class="py-3 px-2 text-right">Live Ask</th>
                <th class="py-3 px-2 text-right font-bold text-slate-300">Suggested Sell</th>
                <th class="py-3 px-3 text-right">Est. Net P&L</th>
                <th class="py-3 px-3 text-center">Action</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800/60 font-mono">
              ${loading && !data
                ? html`<tr><td colspan="8" class="text-center py-8 text-slate-400 font-sans">Scanning Jita hangar inventory, live order books & cost basis...</td></tr>`
                : errorMsg
                  ? html`<tr><td colspan="8" class="text-center py-8 text-rose-400 font-sans">Error loading inventory: ${errorMsg}</td></tr>`
                  : !rows.length
                    ? html`<tr><td colspan="8" class="text-center py-8 text-slate-500 font-sans">No loose items matching this filter. (Items inside containers or other regions are ignored)</td></tr>`
                    : rows.map((item, idx) => {
                        const deskBadge = html`<${DeskBadge} desk=${item.desk} />`;

                        let actionBadge;
                        if (item.collision?.has_collision) {
                          const c = item.collision;
                          const cStyle = c.action === 'CANCEL_MERGE'
                            ? 'bg-amber-950/80 text-amber-300 border border-amber-500/80'
                            : c.action === 'HOLD_HANGAR'
                              ? 'bg-rose-950/80 text-rose-300 border border-rose-500/80'
                              : c.action === 'MODIFY_HOLD'
                                ? 'bg-cyan-950/80 text-cyan-300 border border-cyan-500/80'
                                : 'bg-emerald-950/80 text-emerald-300 border border-emerald-500/80';
                          actionBadge = html`<span class=${`px-2 py-0.5 rounded font-bold text-[10px] ${cStyle}`} title=${c.reason}>${c.badge}</span>`;
                        } else if (item.action_type === 'flip') {
                          actionBadge = html`<span class="px-2 py-0.5 rounded font-bold text-[10px] bg-emerald-950/80 border border-emerald-700 text-emerald-300">FAST FLIP</span>`;
                        } else if (item.action_type === 'met') {
                          actionBadge = html`<span class="px-2 py-0.5 rounded font-bold text-[10px] bg-blue-950/80 border border-blue-700 text-blue-300">TARGET MET</span>`;
                        } else if (item.action_type === 'time_stop') {
                          const hd = item.hold_data || {};
                          actionBadge = html`<span class="px-2 py-0.5 rounded font-bold text-[10px] bg-rose-950/80 border border-rose-600 text-rose-300" title=${`Held ${hd.days_held || 0}d (max ${hd.max_days || 60}d)`}>⏰ TIME STOP (${hd.days_held || 0}d)</span>`;
                        } else if (item.action_type === 'hold') {
                          const hd = item.hold_data || {};
                          actionBadge = html`<span class="px-2 py-0.5 rounded font-bold text-[10px] bg-amber-950/80 border border-amber-700 text-amber-300">HOLD CYCLE${hd.days_held ? ` (${hd.days_held}d)` : ''}</span>`;
                        } else {
                          actionBadge = html`<span class="px-2 py-0.5 rounded font-bold text-[10px] bg-indigo-950/80 border border-indigo-700 text-indigo-300">MARKET EXIT</span>`;
                        }

                        const tagColor = item.cost_source === 'WALLET' ? 'text-cyan-400' : item.cost_source === 'LEDGER' ? 'text-amber-400' : 'text-slate-400';

                        return html`
                          <tr key=${item.type_id || idx} class="hover:bg-slate-800/40 transition">
                            <td class="py-2.5 px-3 text-slate-200 font-medium font-sans">
                              ${item.collision?.has_collision && html`
                                <span class="inline-block mr-1.5 px-1 rounded text-[9px] font-bold bg-amber-950 text-amber-300 border border-amber-500/60" title=${item.collision.reason}>COLLISION</span>
                              `}
                              <${MarketLink} typeId=${item.type_id} name=${item.name} />
                            </td>
                            <td class="py-2.5 px-2">${deskBadge}</td>
                            <td class="py-2.5 px-2 text-right text-slate-300">${item.qty}</td>
                            <td class="py-2.5 px-2 text-right font-sans">
                              ${item.unit_cost > 0
                                ? html`<div>${fmtISK(item.unit_cost)}</div><div class=${`text-[9px] ${tagColor}`}>${item.cost_source}</div>`
                                : html`<span class="text-slate-500">--</span>`}
                            </td>
                            <td class="py-2.5 px-2 text-right text-slate-300">
                              ${item.lowest_ask ? fmtPreciseISK(item.lowest_ask) : html`<span class="text-amber-500 font-sans font-bold text-[10px]">STOCKOUT</span>`}
                            </td>
                            <td class="py-2.5 px-2 text-right font-bold text-slate-100">${fmtPreciseISK(item.recommended_sell)}</td>
                            <td class="py-2.5 px-3 text-right font-sans">
                              ${item.cost_source === 'UNKNOWN'
                                ? html`<span class="text-slate-400 font-bold">+${fmtISK(item.profit)}</span><div class="text-[9px] text-slate-500">Gross Proceeds</div>`
                                : html`
                                  <span class=${`font-bold ${item.profit >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                    ${item.profit >= 0 ? '+' : ''}${fmtISK(item.profit)}
                                  </span>
                                  <div class=${`text-[9px] font-bold ${item.profit >= 0 ? 'text-emerald-500/80' : 'text-rose-500/80'}`}>
                                    ${item.action_type === 'hold' ? 'Target: ' : ''}${item.profit >= 0 ? '+' : ''}${item.margin_pct}%
                                  </div>
                                  ${item.action_type === 'hold' && item.lowest_ask > 0 && html`
                                    <div class=${`text-[9px] font-mono ${item.spot_profit >= 0 ? 'text-cyan-400' : 'text-slate-500'}`}>
                                      Now: ${item.spot_profit >= 0 ? '+' : ''}${fmtISK(item.spot_profit)} (${item.spot_profit >= 0 ? '+' : ''}${item.spot_margin_pct}%)
                                    </div>
                                  `}
                                `}
                            </td>
                            <td class="py-2.5 px-3 text-center">${actionBadge}</td>
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
