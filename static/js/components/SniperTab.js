import { html, useState } from '../lib.js';
import { fmtISK } from '../utils.js';

export function SniperTab({ sniper }) {
  const [showRaw, setShowRaw] = useState(false);
  const deals = sniper?.deals || [];

  return html`
    <div class="space-y-4 font-mono">
      <div class="bg-eveCard border border-eveBorder rounded-lg overflow-hidden">
        <div class="p-3 bg-slate-900/60 border-b border-eveBorder flex flex-wrap justify-between items-center gap-2">
          <div class="flex items-center gap-2">
            <span class="text-xs font-bold text-eveCyan uppercase">Live Jita 4-4 Firesale Sniping Opportunities</span>
            <span class="text-[10px] bg-slate-800 text-slate-400 px-2 py-0.5 rounded">${deals.length} deals detected</span>
          </div>
          <div class="flex items-center gap-3">
            <span class="text-xs text-slate-500">Last scan: ${sniper?.timestamp || 'Never'}</span>
            <button onClick=${() => setShowRaw(v => !v)} class="text-xs bg-slate-900 hover:bg-slate-800 text-slate-400 border border-slate-700 px-2.5 py-1 rounded transition">
              ${showRaw ? 'Hide Console Log' : 'Show Console Log'}
            </button>
          </div>
        </div>

        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead class="bg-slate-900 border-b border-eveBorder text-slate-400 uppercase">
              <tr>
                <th class="p-3">#</th>
                <th class="p-3">Opportunity</th>
                <th class="p-3">Item Name</th>
                <th class="p-3 text-right">Units</th>
                <th class="p-3 text-right">Discount</th>
                <th class="p-3 text-right">Ask Price (Buy)</th>
                <th class="p-3 text-right">Target Exit</th>
                <th class="p-3 text-right">Net Margin</th>
                <th class="p-3 text-right">Capital</th>
                <th class="p-3 text-right">Est. Net Profit</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800">
              ${!deals.length
                ? html`<tr><td colspan="10" class="p-6 text-center text-slate-500">No active firesales detected. Click "Run Deal Sniper" above to trigger a fresh scan.</td></tr>`
                : deals.map((d, i) => {
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

                    return html`
                      <tr key=${`${typeId}-${i}`} class="hover:bg-slate-800/40 transition">
                        <td class="p-3 text-slate-500">${i + 1}</td>
                        <td class="p-3">
                          <span class=${`${isFiresale ? 'bg-amber-950/70 text-amber-300 border-amber-600/80' : 'bg-cyan-950/70 text-cyan-300 border-cyan-600/80'} border px-2 py-0.5 rounded text-[10px] font-bold`}>
                            ${d.deal_type || 'DEAL'}
                          </span>
                        </td>
                        <td class="p-3 font-semibold text-white">
                          <a href=${`https://evetycoon.com/market/${typeId}`} target="_blank" class="hover:text-eveCyan hover:underline">${name}</a>
                        </td>
                        <td class="p-3 text-right text-slate-300">${qty}</td>
                        <td class="p-3 text-right text-amber-400 font-semibold">-${discount}%</td>
                        <td class="p-3 text-right text-slate-200 font-semibold">${fmtISK(askPrice)}</td>
                        <td class="p-3 text-right text-slate-300">${fmtISK(targetExit)}</td>
                        <td class="p-3 text-right text-eveCyan font-bold">${netMargin}%</td>
                        <td class="p-3 text-right text-slate-300 font-semibold">${fmtISK(capital)}</td>
                        <td class="p-3 text-right text-eveGreen font-bold">+${fmtISK(profit)}</td>
                      </tr>
                    `;
                  })}
            </tbody>
          </table>
        </div>

        ${showRaw && html`
          <div class="p-4 border-t border-slate-800 bg-black/60">
            <pre class="text-xs text-emerald-400 overflow-x-auto">${sniper?.raw_output || 'No scans recorded.'}</pre>
          </div>
        `}
      </div>
    </div>
  `;
}
