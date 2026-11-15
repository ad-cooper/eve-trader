import { html, useState, useMemo } from '../lib.js';
import { fmtISK } from '../utils.js';

function OrderStatusBadge({ status }) {
  if (status === 'BUY_ACTIVE') {
    return html`<span class="bg-blue-900/60 text-blue-300 border border-blue-700/80 px-1.5 py-0.5 rounded text-[10px] font-bold">BUY ACTIVE</span>`;
  }
  if (status === 'SELL_ACTIVE') {
    return html`<span class="bg-emerald-900/60 text-emerald-300 border border-emerald-700/80 px-1.5 py-0.5 rounded text-[10px] font-bold">SELL LISTED</span>`;
  }
  return html`<span class="bg-slate-800 text-slate-400 border border-slate-700 px-1.5 py-0.5 rounded text-[10px]">UNPLACED</span>`;
}

function ActionGuidanceBadge({ code, text }) {
  if (code === 'TOP_BOOK') {
    return html`
      <span class="bg-emerald-950/80 text-emerald-300 border border-emerald-600/80 px-2 py-0.5 rounded text-[10px] font-bold tracking-wider inline-flex items-center gap-1 font-mono">
        <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
        <span>TOP BID</span>
      </span>`;
  }
  if (code === 'PRIME_DIP') {
    return html`
      <span class="bg-amber-950/90 text-amber-300 border border-amber-500/90 px-2 py-0.5 rounded text-[10px] font-bold tracking-wider inline-flex items-center gap-1 font-mono">
        <span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>
        <span>★ PRIME DIP</span>
      </span>`;
  }
  if (code === 'BUMP_VIABLE') {
    return html`
      <span class="bg-cyan-950/90 text-cyan-300 border border-cyan-500/90 px-2 py-0.5 rounded text-[10px] font-bold tracking-wider inline-flex items-center gap-1 font-mono">
        <span class="w-1.5 h-1.5 rounded-full bg-cyan-400"></span>
        <span>${text || 'BUMP VIABLE'}</span>
      </span>`;
  }
  if ((code || '').startsWith('CANCEL_')) {
    return html`
      <span class="bg-rose-950/90 text-rose-300 border border-rose-600/90 px-2 py-0.5 rounded text-[10px] font-bold tracking-wider inline-flex items-center gap-1 font-mono" title="Top bid exceeds profitable threshold. Cancel order to free escrow for active rotation.">
        <span class="w-1.5 h-1.5 rounded-full bg-rose-400"></span>
        <span>${text || 'CANCEL (CEILING)'}</span>
      </span>`;
  }
  if (code === 'PATIENT') {
    return html`
      <span class="bg-slate-800/80 text-slate-300 border border-slate-700 px-2 py-0.5 rounded text-[10px] font-mono">
        ${text || 'HOLD FIRM'}
      </span>`;
  }
  if (code === 'SELL_ACTIVE') {
    return html`<span class="text-emerald-400 font-mono text-[10px]">SELLING</span>`;
  }
  if (code === 'UNPLACED') {
    return html`<span class="text-amber-500 font-mono text-[10px]">NEW ORDER</span>`;
  }
  return html`<span class="text-slate-600 font-mono text-[10px]">--</span>`;
}

function ProximityBadge({ proximityPct, diffPct }) {
  if (proximityPct === null || proximityPct === undefined) {
    return html`<span class="text-slate-600 font-mono">--</span>`;
  }
  const diffSign = diffPct >= 0 ? '+' : '';
  if (proximityPct >= 99.9) {
    return html`
      <span class="bg-emerald-950/80 text-emerald-300 border border-emerald-600/80 px-2 py-0.5 rounded text-[10px] font-bold tracking-wider inline-flex items-center gap-1 font-mono">
        <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
        <span>TARGET MET 100.0%</span>
      </span>`;
  }
  if (proximityPct >= 85) {
    return html`
      <span class="bg-cyan-950/80 text-cyan-300 border border-cyan-600/80 px-2 py-0.5 rounded text-[10px] font-bold tracking-wider inline-flex items-center gap-1 font-mono">
        <span class="w-1.5 h-1.5 rounded-full bg-cyan-400"></span>
        <span>${proximityPct.toFixed(1)}% (${diffSign}${diffPct.toFixed(1)}%)</span>
      </span>`;
  }
  return html`
    <span class="bg-amber-950/90 text-amber-300 border border-amber-600/80 px-2 py-0.5 rounded text-[10px] font-bold tracking-wider inline-flex items-center gap-1 font-mono">
      <span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>
      <span>DEEP DIP ${proximityPct.toFixed(1)}% (${diffSign}${diffPct.toFixed(1)}%)</span>
    </span>`;
}

export function DeskTab({ deskKey, rows = [], askPolling, onPollAsks }) {
  const [unplacedOnly, setUnplacedOnly] = useState(false);
  const [cancelOnly, setCancelOnly] = useState(false);

  const stats = useMemo(() => {
    const deskCap = rows.reduce((s, r) => s + (parseFloat(r.capital) || 0), 0);
    const deskProf = rows.reduce((s, r) => s + (parseFloat(r.profit) || 0), 0);
    const deskRoi = deskCap > 0 ? (deskProf / deskCap) * 100 : 0;
    const unplaced = rows.filter(r => r.char_status === 'UNPLACED');
    const buyActive = rows.filter(r => r.char_status === 'BUY_ACTIVE');
    const deskNeeded = unplaced.reduce((s, r) => s + (parseFloat(r.capital) || 0), 0);
    const deskEscrow = buyActive.reduce((s, r) => s + (parseFloat(r.escrow) || parseFloat(r.capital) || 0), 0);
    return { deskCap, deskProf, deskRoi, unplacedCount: unplaced.length, deskNeeded, deskEscrow };
  }, [rows]);

  const visibleRows = useMemo(() => {
    let out = rows;
    if (unplacedOnly) out = out.filter(r => r.char_status === 'UNPLACED');
    if (cancelOnly) out = out.filter(r => (r.action_code || '').startsWith('CANCEL_'));
    return out;
  }, [rows, unplacedOnly, cancelOnly]);

  return html`
    <div class="font-mono space-y-3">
      <div class="flex flex-wrap items-center justify-between bg-slate-900/80 border border-eveBorder p-3.5 rounded-lg text-xs gap-3">
        <div class="flex flex-wrap items-center gap-5">
          <div class="flex items-center gap-2">
            <span class="text-slate-500 uppercase font-semibold">Active Desk Capital:</span>
            <span class="font-bold text-white text-sm">${fmtISK(stats.deskCap)}</span>
          </div>
          <div class="h-4 w-px bg-slate-700"></div>
          <div class="flex items-center gap-2">
            <span class="text-slate-500 uppercase font-semibold">Tied in Escrow:</span>
            <span class="font-bold text-blue-400 text-sm">${fmtISK(stats.deskEscrow)}</span>
          </div>
          <div class="h-4 w-px bg-slate-700"></div>
          <div class="flex items-center gap-2">
            <span class="text-slate-500 uppercase font-semibold">ISK Needed:</span>
            <span class="font-bold text-amber-400 text-sm">${fmtISK(stats.deskNeeded)}</span>
            <span class="text-[10px] bg-amber-950/80 border border-amber-800 text-amber-300 px-1.5 py-0.5 rounded font-bold">${stats.unplacedCount} unplaced</span>
          </div>
          <div class="h-4 w-px bg-slate-700"></div>
          <div class="flex items-center gap-2">
            <span class="text-slate-500 uppercase font-semibold">Projected Net:</span>
            <span class="font-bold text-eveGreen text-sm">+${fmtISK(stats.deskProf)}</span>
          </div>
          <div class="h-4 w-px bg-slate-700"></div>
          <div class="flex items-center gap-2">
            <span class="text-slate-500 uppercase font-semibold">Blended ROI:</span>
            <span class="font-bold text-eveCyan text-sm">${stats.deskRoi.toFixed(1)}%</span>
          </div>
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <label class="text-xs text-slate-400 flex items-center gap-1.5 cursor-pointer bg-slate-900 border border-slate-700 px-2.5 py-1 rounded">
            <input type="checkbox" checked=${unplacedOnly} onChange=${e => setUnplacedOnly(e.target.checked)} class="rounded bg-slate-800 border-slate-600 text-eveCyan focus:ring-0" />
            <span>Unplaced Only</span>
          </label>
          <label class="text-xs text-rose-300 font-semibold flex items-center gap-1.5 cursor-pointer bg-slate-900 border border-slate-700 px-2.5 py-1 rounded">
            <input type="checkbox" checked=${cancelOnly} onChange=${e => setCancelOnly(e.target.checked)} class="rounded border-slate-700 bg-slate-900 text-rose-500 focus:ring-0" />
            <span>Cancel / Drop Only</span>
          </label>
          <button onClick=${onPollAsks} title="Poll live Jita 4-4 sell orders" class="text-xs bg-slate-900 hover:bg-slate-800 text-slate-300 border border-slate-700 px-2.5 py-1 rounded transition flex items-center gap-1.5">
            ${askPolling && html`<svg class="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>`}
            <span>${askPolling ? 'Polling Books...' : 'Poll Asks'}</span>
          </button>
          <span class="text-xs uppercase font-bold text-slate-300 bg-slate-800 px-2.5 py-1 rounded border border-slate-700">
            ${deskKey.toUpperCase()} DESK
          </span>
        </div>
      </div>

      <div class="bg-eveCard border border-eveBorder rounded-lg overflow-hidden">
        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead class="bg-slate-900 border-b border-eveBorder text-slate-400 uppercase">
              <tr>
                <th class="p-3">#</th>
                <th class="p-3">Order Status</th>
                <th class="p-3 text-center text-slate-300">Action Guidance</th>
                <th class="p-3">Item Name</th>
                <th class="p-3 text-right">Qty</th>
                <th class="p-3 text-right">Dump Freq</th>
                <th class="p-3 text-right">Target Buy</th>
                <th class="p-3 text-right">Target Sell</th>
                <th class="p-3 text-right text-slate-300">Live Ask</th>
                <th class="p-3 text-center text-slate-300">Target Proximity</th>
                <th class="p-3 text-right">Net Margin</th>
                <th class="p-3 text-right">Capital</th>
                <th class="p-3 text-right">Net Profit</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800">
              ${!visibleRows.length
                ? html`<tr><td colspan="13" class="p-4 text-center text-slate-500">No matching items.</td></tr>`
                : visibleRows.map((r, i) => {
                    const isProf = (r.profit || 0) >= 0;
                    return html`
                      <tr key=${r.type_id || i} class="hover:bg-slate-800/40 transition">
                        <td class="p-3 text-slate-500">${i + 1}</td>
                        <td class="p-3"><${OrderStatusBadge} status=${r.char_status} /></td>
                        <td class="p-3 text-center"><${ActionGuidanceBadge} code=${r.action_code} text=${r.action_text} /></td>
                        <td class="p-3 font-semibold text-white">
                          <a href=${`https://evetycoon.com/market/${r.type_id}`} target="_blank" class="hover:text-eveCyan hover:underline">${r.name}</a>
                        </td>
                        <td class="p-3 text-right text-slate-300">${r.qty}</td>
                        <td class="p-3 text-right text-slate-400">${Number(r.dump_freq || 0).toFixed(1)}%</td>
                        <td class="p-3 text-right text-slate-200">${fmtISK(r.target_buy)}</td>
                        <td class="p-3 text-right text-slate-200 font-semibold">${fmtISK(r.target_sell)}</td>
                        <td class="p-3 text-right text-slate-300">
                          ${r.live_ask
                            ? fmtISK(r.live_ask)
                            : r.ask_polled
                              ? html`<span class="text-amber-400 font-bold text-[10px] bg-amber-950/70 border border-amber-800 px-1 py-0.5 rounded">OUT OF STOCK</span>`
                              : html`<span class="text-slate-400 text-[10px] bg-slate-800/80 border border-slate-700 px-1.5 py-0.5 rounded">UNPOLLED</span>`}
                        </td>
                        <td class="p-3 text-center"><${ProximityBadge} proximityPct=${r.proximity_pct} diffPct=${r.diff_pct} /></td>
                        <td class=${`p-3 text-right ${isProf ? 'text-eveCyan font-semibold' : 'text-red-400 font-bold'}`}>${Number(r.net_margin || 0).toFixed(1)}%</td>
                        <td class="p-3 text-right">
                          ${r.char_status === 'UNPLACED'
                            ? html`<div class="font-bold text-amber-300">${fmtISK(r.capital)}</div><div class="text-[9px] text-amber-500 font-bold uppercase tracking-wider">Needed</div>`
                            : r.char_status === 'BUY_ACTIVE'
                              ? html`<div class="font-bold text-blue-300">${fmtISK(r.escrow || r.capital)}</div><div class="text-[9px] text-blue-500 font-bold uppercase tracking-wider">In Escrow</div>`
                              : html`<div class="font-semibold text-slate-300">${fmtISK(r.capital)}</div><div class="text-[9px] text-emerald-500 font-semibold uppercase tracking-wider">Sell Listed</div>`}
                        </td>
                        <td class=${`p-3 text-right font-bold ${isProf ? 'text-eveGreen' : 'text-red-400'}`}>${isProf ? '+' : ''}${fmtISK(r.profit)}</td>
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
