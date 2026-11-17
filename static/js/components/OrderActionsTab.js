import { html, useState, useEffect, useCallback } from '../lib.js';
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

export function OrderActionsTab() {
  const [deskFilter, setDeskFilter] = useState('all');
  const [viewMode, setViewMode] = useState('all');
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState(null);
  const [errorMsg, setErrorMsg] = useState('');
  const [copiedKey, setCopiedKey] = useState(null);

  const loadActions = useCallback(async (desk, refresh = false) => {
    setLoading(true);
    setErrorMsg('');
    try {
      const res = await fetch(`/api/orders/actions?desk=${desk}&refresh=${refresh}`);
      if (!res.ok) throw new Error(`Server returned HTTP ${res.status}`);
      const json = await res.json();
      setData(json);
    } catch (err) {
      setErrorMsg(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadActions(deskFilter, false);
  }, [deskFilter, loadActions]);

  async function handleCopyPrice(price, key) {
    const ok = await copyToClipboard(String(price));
    setCopiedKey(ok ? key : null);
    setTimeout(() => setCopiedKey(null), 1400);
  }

  const allBumps = data?.bumps || [];
  const sellBumps = data?.sell_bumps || allBumps.filter(b => b.side === 'SELL');
  const buyBumps = data?.buy_bumps || allBumps.filter(b => b.side === 'BUY');
  const cancels = data?.cancels || [];

  const sellBumpCount = data?.sell_bump_count ?? sellBumps.length;
  const buyBumpCount = data?.buy_bump_count ?? buyBumps.length;
  const cancelCount = data?.cancel_count ?? cancels.length;
  const totalActions = data?.total_actions ?? (sellBumpCount + buyBumpCount + cancelCount);
  const escrowFreed = data?.total_escrow_freed ?? 0;
  const bumpEscrowDelta = data?.total_bump_escrow_delta ?? 0;
  const netIskImpact = data?.net_isk_impact ?? (escrowFreed - bumpEscrowDelta);

  const showSells = viewMode === 'all' || viewMode === 'sells';
  const showBuys = viewMode === 'all' || viewMode === 'buys';
  const showCancels = viewMode === 'all' || viewMode === 'cancels';

  return html`
    <div class="space-y-4 font-mono text-xs">
      <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div class="bg-emerald-950/20 border border-emerald-800/40 rounded-lg p-4">
          <div class="text-xs text-emerald-400 uppercase">Sell Undercuts (Reprice)</div>
          <div class="text-xl font-bold text-eveGreen mt-1">${data ? `${sellBumpCount} orders` : '--'}</div>
          <div class="text-[11px] text-emerald-400/80 mt-0.5">Active sells undercut above cost floor</div>
        </div>

        <div class="bg-cyan-950/20 border border-cyan-800/40 rounded-lg p-4">
          <div class="text-xs text-cyan-400 uppercase">Buy Bid Bumps</div>
          <div class="text-xl font-bold text-eveCyan mt-1">${data ? `${buyBumpCount} orders` : '--'}</div>
          <div class="text-[11px] text-cyan-400/80 mt-0.5">Meaningfully outbid & above margin floor</div>
        </div>

        <div class="bg-rose-950/20 border border-rose-800/40 rounded-lg p-4">
          <div class="text-xs text-rose-400 uppercase">Orders to Cancel</div>
          <div class="text-xl font-bold text-rose-300 mt-1">${data ? `${cancelCount} orders` : '--'}</div>
          <div class="text-[11px] text-rose-400/80 mt-0.5">Frees +${fmtISK(escrowFreed)} escrow</div>
        </div>

        <div class="bg-eveCard border border-eveBorder rounded-lg p-4">
          <div class="text-xs text-slate-400 uppercase">Extra Escrow for Bumps</div>
          <div class="text-xl font-bold text-eveAmber mt-1">${data ? fmtISK(bumpEscrowDelta) : '--'}</div>
          <div class="text-[11px] text-slate-400 mt-0.5">
            ${data ? `Net wallet impact: ${netIskImpact >= 0 ? '+' : ''}${fmtISK(netIskImpact)}` : 'Top-up needed to match top book'}
          </div>
        </div>
      </div>

      <div class="bg-eveCard border border-eveBorder rounded-lg p-4 flex flex-wrap justify-between items-center gap-3">
        <div class="flex flex-wrap items-center gap-3">
          <${FilterPills}
            options=${[
              ['all', `All Actions (${totalActions})`],
              ['sells', `Sell Undercuts (${sellBumpCount})`],
              ['buys', `Buy Bumps (${buyBumpCount})`],
              ['cancels', `Cancels (${cancelCount})`],
            ]}
            value=${viewMode}
            onChange=${setViewMode}
            activeClass="bg-eveCyan/20 text-eveCyan border border-eveCyan/40"
          />

          <${FilterPills}
            options=${[
              ['all', 'All Desks'],
              ['evergreen', 'Evergreen'],
              ['tactical', 'Tactical'],
              ['hold', 'Hold'],
            ]}
            value=${deskFilter}
            onChange=${setDeskFilter}
          />
        </div>

        <button
          disabled=${loading}
          onClick=${() => loadActions(deskFilter, true)}
          class="bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-600 px-3.5 py-1.5 rounded font-semibold transition flex items-center gap-1.5"
        >
          ${loading && html`<span class="animate-spin">⟳</span>`}
          <span>${loading ? 'Re-Auditing Live Books...' : 'Re-Audit Live Books'}</span>
        </button>
      </div>

      <div class="bg-slate-900/70 border border-eveBorder rounded-lg px-4 py-2.5 text-slate-300 flex flex-wrap justify-between items-center gap-2 text-[11px]">
        <span>In EVE Online <strong>Market → My Orders</strong>: click <strong>Copy</strong>, right-click the order → <strong>Modify Order</strong>, and paste.</span>
        <span class="text-slate-400">Hold bids & &lt;0.5%–1.0% penny-war ticks are held firm to save relist fees</span>
      </div>

      ${errorMsg && html`
        <div class="bg-rose-950/40 border border-rose-700 rounded-lg p-4 text-rose-300">
          Failed to evaluate active order actions: ${errorMsg}
        </div>
      `}

      ${showSells && html`
        <div class="bg-eveCard border border-eveBorder rounded-lg overflow-hidden">
          <div class="px-4 py-3 bg-emerald-950/20 border-b border-eveBorder flex flex-wrap justify-between items-center gap-2">
            <div class="flex items-center gap-2">
              <span class="w-2 h-2 rounded-full bg-eveGreen"></span>
              <h3 class="text-xs font-bold uppercase tracking-wider text-white">Sell Orders to Undercut (${sellBumpCount})</h3>
            </div>
            <span class="text-[11px] text-slate-400">Active sell orders undercut by competitors that still clear your real cost basis + margin floor</span>
          </div>

          <div class="overflow-x-auto">
            <table class="w-full text-left">
              <thead class="bg-slate-900 border-b border-eveBorder text-slate-400 uppercase text-[10px]">
                <tr>
                  <th class="p-3">#</th>
                  ${deskFilter === 'all' && html`<th class="p-3">Desk</th>`}
                  <th class="p-3">Item Name</th>
                  <th class="p-3 text-right">Qty</th>
                  <th class="p-3 text-right">Your Ask</th>
                  <th class="p-3 text-right">Lowest Ask</th>
                  <th class="p-3 text-right">New Ask</th>
                  <th class="p-3 text-right">Cost Basis</th>
                  <th class="p-3 text-right">New Margin</th>
                  <th class="p-3 text-right">Est. Profit</th>
                  <th class="p-3 text-center">Modify Price</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-800">
                ${loading && !data
                  ? html`<tr><td colspan=${deskFilter === 'all' ? 11 : 10} class="p-6 text-center text-slate-400">Evaluating active sell orders...</td></tr>`
                  : !sellBumps.length
                    ? html`<tr><td colspan=${deskFilter === 'all' ? 11 : 10} class="p-6 text-center text-emerald-300">No profitable sell undercuts needed right now.</td></tr>`
                    : sellBumps.map((it, i) => {
                      const rowKey = `sell-${it.type_id}-${i}`;
                      return html`
                        <tr key=${rowKey} class="hover:bg-slate-800/40 transition">
                          <td class="p-3 text-slate-500">${i + 1}</td>
                          ${deskFilter === 'all' && html`<td class="p-3"><${DeskBadge} desk=${it.desk} /></td>`}
                          <td class="p-3 font-semibold text-white">
                            <${MarketLink} typeId=${it.type_id} name=${it.name} />
                          </td>
                          <td class="p-3 text-right text-slate-300 font-semibold">${Number(it.qty || 0).toLocaleString()}</td>
                          <td class="p-3 text-right text-slate-400">${fmtPreciseISK(it.current_price)}</td>
                          <td class="p-3 text-right text-slate-300">${fmtPreciseISK(it.market_price)}</td>
                          <td class="p-3 text-right text-eveGreen font-bold">${fmtPreciseISK(it.recommended_price)}</td>
                          <td class="p-3 text-right text-slate-400">${it.unit_cost > 0 ? fmtISK(it.unit_cost) : '--'}</td>
                          <td class="p-3 text-right text-eveCyan font-semibold">${it.new_margin_pct}%</td>
                          <td class="p-3 text-right text-eveGreen font-bold">+${fmtISK(it.expected_profit)}</td>
                          <td class="p-3 text-center">
                            <button
                              onClick=${() => handleCopyPrice(it.recommended_price, rowKey)}
                              class=${`bg-emerald-950/60 hover:bg-emerald-900/70 border px-2.5 py-1 rounded text-[10px] font-bold transition ${copiedKey === rowKey ? 'text-white border-emerald-400' : 'text-eveGreen border-emerald-600/70'}`}
                            >
                              ${copiedKey === rowKey ? '✓ Copied!' : 'Copy Price'}
                            </button>
                          </td>
                        </tr>
                      `;
                    })}
              </tbody>
            </table>
          </div>
        </div>
      `}

      ${showBuys && html`
        <div class="bg-eveCard border border-eveBorder rounded-lg overflow-hidden">
          <div class="px-4 py-3 bg-cyan-950/20 border-b border-eveBorder flex flex-wrap justify-between items-center gap-2">
            <div class="flex items-center gap-2">
              <span class="w-2 h-2 rounded-full bg-eveCyan"></span>
              <h3 class="text-xs font-bold uppercase tracking-wider text-white">Buy Orders to Bump (${buyBumpCount})</h3>
            </div>
            <span class="text-[11px] text-slate-400">Outbid buy orders (&ge;0.5% Evergreen / &ge;1.0% Tactical) that still clear your desk margin floor</span>
          </div>

          <div class="overflow-x-auto">
            <table class="w-full text-left">
              <thead class="bg-slate-900 border-b border-eveBorder text-slate-400 uppercase text-[10px]">
                <tr>
                  <th class="p-3">#</th>
                  ${deskFilter === 'all' && html`<th class="p-3">Desk</th>`}
                  <th class="p-3">Item Name</th>
                  <th class="p-3 text-right">Qty</th>
                  <th class="p-3 text-right">Your Bid</th>
                  <th class="p-3 text-right">Top Bid</th>
                  <th class="p-3 text-right">New Bid</th>
                  <th class="p-3 text-right">Extra Escrow</th>
                  <th class="p-3 text-right">New Margin</th>
                  <th class="p-3 text-right">Est. Profit</th>
                  <th class="p-3 text-center">Modify Price</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-800">
                ${loading && !data
                  ? html`<tr><td colspan=${deskFilter === 'all' ? 11 : 10} class="p-6 text-center text-slate-400">Evaluating active buy orders...</td></tr>`
                  : !buyBumps.length
                    ? html`<tr><td colspan=${deskFilter === 'all' ? 11 : 10} class="p-6 text-center text-emerald-300">No buy orders need bumping right now.</td></tr>`
                    : buyBumps.map((it, i) => {
                      const rowKey = `buy-${it.type_id}-${i}`;
                      return html`
                        <tr key=${rowKey} class="hover:bg-slate-800/40 transition">
                          <td class="p-3 text-slate-500">${i + 1}</td>
                          ${deskFilter === 'all' && html`<td class="p-3"><${DeskBadge} desk=${it.desk} /></td>`}
                          <td class="p-3 font-semibold text-white">
                            <${MarketLink} typeId=${it.type_id} name=${it.name} />
                          </td>
                          <td class="p-3 text-right text-slate-300 font-semibold">${Number(it.qty || 0).toLocaleString()}</td>
                          <td class="p-3 text-right text-slate-400">${fmtPreciseISK(it.current_price)}</td>
                          <td class="p-3 text-right text-slate-300">${fmtPreciseISK(it.market_price)}</td>
                          <td class="p-3 text-right text-eveCyan font-bold">${fmtPreciseISK(it.recommended_price)}</td>
                          <td class="p-3 text-right text-eveAmber/90">+${fmtISK(it.escrow_delta)}</td>
                          <td class="p-3 text-right text-eveCyan font-semibold">${it.new_margin_pct}%</td>
                          <td class="p-3 text-right text-eveGreen font-bold">+${fmtISK(it.expected_profit)}</td>
                          <td class="p-3 text-center">
                            <button
                              onClick=${() => handleCopyPrice(it.recommended_price, rowKey)}
                              class=${`bg-cyan-950/60 hover:bg-cyan-900/70 border px-2.5 py-1 rounded text-[10px] font-bold transition ${copiedKey === rowKey ? 'text-eveGreen border-emerald-500' : 'text-eveCyan border-cyan-600/70'}`}
                            >
                              ${copiedKey === rowKey ? '✓ Copied!' : 'Copy Price'}
                            </button>
                          </td>
                        </tr>
                      `;
                    })}
              </tbody>
            </table>
          </div>
        </div>
      `}

      ${showCancels && html`
        <div class="bg-eveCard border border-eveBorder rounded-lg overflow-hidden">
          <div class="px-4 py-3 bg-rose-950/20 border-b border-eveBorder flex flex-wrap justify-between items-center gap-2">
            <div class="flex items-center gap-2">
              <span class="w-2 h-2 rounded-full bg-rose-400"></span>
              <h3 class="text-xs font-bold uppercase tracking-wider text-white">Orders to Cancel (${cancelCount})</h3>
            </div>
            <span class="text-[11px] text-slate-400">Cancel these orders in EVE to free ${fmtISK(escrowFreed)} escrow and open order slots</span>
          </div>

          <div class="overflow-x-auto">
            <table class="w-full text-left">
              <thead class="bg-slate-900 border-b border-eveBorder text-slate-400 uppercase text-[10px]">
                <tr>
                  <th class="p-3">#</th>
                  <th class="p-3">Side</th>
                  ${deskFilter === 'all' && html`<th class="p-3">Desk</th>`}
                  <th class="p-3">Cancel Reason</th>
                  <th class="p-3">Item Name</th>
                  <th class="p-3 text-right">Qty</th>
                  <th class="p-3 text-right">Your Price</th>
                  <th class="p-3 text-right">Market Top</th>
                  <th class="p-3 text-right">Target Exit</th>
                  <th class="p-3 text-right">Est. Profit</th>
                  <th class="p-3 text-right">Margin %</th>
                  <th class="p-3 text-right">Escrow Freed</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-800">
                ${loading && !data
                  ? html`<tr><td colspan=${deskFilter === 'all' ? 12 : 11} class="p-6 text-center text-slate-400">Checking active orders for cancellation triggers...</td></tr>`
                  : !cancels.length
                    ? html`<tr><td colspan=${deskFilter === 'all' ? 12 : 11} class="p-6 text-center text-emerald-300">No active orders need to be cancelled!</td></tr>`
                    : cancels.map((it, i) => {
                      const estProf = it.expected_profit ?? 0;
                      return html`
                        <tr key=${`cancel-${it.side}-${it.type_id}-${i}`} class="hover:bg-slate-800/40 transition">
                          <td class="p-3 text-slate-500">${i + 1}</td>
                          <td class="p-3">
                            <span class=${it.side === 'SELL'
                              ? 'bg-emerald-950/80 text-emerald-300 border border-emerald-700/80 px-1.5 py-0.5 rounded text-[10px] font-bold'
                              : 'bg-blue-950/80 text-blue-300 border border-blue-700/80 px-1.5 py-0.5 rounded text-[10px] font-bold'}>
                              ${it.side}
                            </span>
                          </td>
                          ${deskFilter === 'all' && html`<td class="p-3"><${DeskBadge} desk=${it.desk} /></td>`}
                          <td class="p-3">
                            <span class="bg-rose-950/90 text-rose-300 border border-rose-600/80 px-2 py-0.5 rounded text-[10px] font-bold">
                              ${it.reason}
                            </span>
                          </td>
                          <td class="p-3 font-semibold text-white">
                            <${MarketLink} typeId=${it.type_id} name=${it.name} />
                          </td>
                          <td class="p-3 text-right text-slate-300 font-semibold">${Number(it.qty || 0).toLocaleString()}</td>
                          <td class="p-3 text-right text-slate-300">${fmtPreciseISK(it.current_price)}</td>
                          <td class="p-3 text-right text-slate-400">${it.market_price ? fmtPreciseISK(it.market_price) : '--'}</td>
                          <td class="p-3 text-right text-slate-300">${it.target_sell ? fmtPreciseISK(it.target_sell) : '--'}</td>
                          <td class=${`p-3 text-right font-bold ${estProf >= 0 ? 'text-eveGreen' : 'text-rose-400'}`}>
                            ${estProf >= 0 ? '+' : ''}${fmtISK(estProf)}
                          </td>
                          <td class=${`p-3 text-right font-semibold ${it.margin_pct >= 0 ? 'text-amber-300' : 'text-rose-400'}`}>
                            ${it.margin_pct}%
                          </td>
                          <td class="p-3 text-right text-eveGreen font-bold">+${fmtISK(it.escrow_freed)}</td>
                        </tr>
                      `;
                    })}
              </tbody>
            </table>
          </div>
        </div>
      `}
    </div>
  `;
}
