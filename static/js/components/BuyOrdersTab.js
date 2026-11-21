import { html, useState, useEffect, useCallback } from '../lib.js';
import { fmtISK, fmtPreciseISK, copyToClipboard } from '../utils.js';
import { DeskBadge, MarketLink, FilterPills } from './ui.js';

export function BuyOrdersTab() {
  const [deskFilter, setDeskFilter] = useState('all');
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState(null);
  const [errorMsg, setErrorMsg] = useState('');
  const [copiedBulk, setCopiedBulk] = useState(false);
  const [copiedRow, setCopiedRow] = useState(null);

  const auditMissing = useCallback(async (desk) => {
    setLoading(true);
    setErrorMsg('');
    setCopiedBulk(false);
    try {
      const res = await fetch(`/api/orders/missing-buys?desk=${desk}`);
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
    auditMissing(deskFilter);
  }, [deskFilter, auditMissing]);

  async function handleCopyBulk() {
    if (!data?.multibuy_text) return;
    const ok = await copyToClipboard(data.multibuy_text);
    setCopiedBulk(ok);
    if (ok) {
      setTimeout(() => setCopiedBulk(false), 2500);
    }
  }

  async function handleCopyPrice(price, idx) {
    const ok = await copyToClipboard(String(price));
    setCopiedRow(ok ? idx : -1);
    setTimeout(() => setCopiedRow(null), 1200);
  }

  const items = data?.items || [];
  const totalNeeded = data ? (data.total_isk_needed ?? data.total_escrow ?? 0) : 0;
  const totalEscrow = data?.total_escrow ?? 0;
  const totalBuyFees = data?.total_buy_fees ?? 0;
  const totalExitFees = data?.total_exit_fees ?? 0;
  const totalFees = data?.total_fees ?? 0;
  const totalProfit = data?.total_expected_profit ?? 0;
  const totalGrossExit = data?.total_gross_exit ?? 0;
  const blendedRoi = Number(data?.blended_roi_pct ?? 0).toFixed(1);
  const isProfit = totalProfit >= 0;

  return html`
    <div class="space-y-4 font-mono text-xs">
      <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div class="bg-eveCard border border-eveBorder rounded-lg p-4">
          <div class="text-xs text-slate-400 uppercase">Total ISK Needed</div>
          <div class="text-xl font-bold text-white mt-1">${data ? fmtISK(totalNeeded) : '--'}</div>
          <div class="text-[11px] text-slate-400 mt-0.5">
            ${data ? `Escrow: ${fmtISK(totalEscrow)} + ${fmtISK(totalBuyFees)} buy fee` : 'Upfront wallet ISK'}
          </div>
        </div>

        <div class=${isProfit ? 'bg-emerald-950/20 border border-emerald-800/40 rounded-lg p-4' : 'bg-rose-950/20 border border-rose-800/40 rounded-lg p-4'}>
          <div class="text-xs text-emerald-400 uppercase">Expected Return (Net)</div>
          <div class=${`text-xl font-bold mt-1 ${isProfit ? 'text-eveGreen' : 'text-rose-300'}`}>
            ${data ? `${isProfit ? '+' : ''}${fmtISK(totalProfit)}` : '--'}
          </div>
          <div class=${`text-[11px] mt-0.5 ${isProfit ? 'text-emerald-400/80' : 'text-rose-400/80'}`}>
            ${data ? `+${blendedRoi}% net ROI • Exit: ${fmtISK(totalGrossExit)}` : 'After all Jita fees'}
          </div>
        </div>

        <div class="bg-eveCard border border-eveBorder rounded-lg p-4">
          <div class="text-xs text-amber-400 uppercase">Total Round-Trip Fees</div>
          <div class="text-xl font-bold text-eveAmber mt-1">${data ? fmtISK(totalFees) : '--'}</div>
          <div class="text-[11px] text-slate-400 mt-0.5">
            ${data ? `Buy (0.5%): ${fmtISK(totalBuyFees)} • Exit (4.6%): ${fmtISK(totalExitFees)}` : 'SCC buy + broker & tax'}
          </div>
        </div>

        <div class="bg-blue-950/20 border border-blue-800/40 rounded-lg p-4">
          <div class="text-xs text-blue-400 uppercase">Missing Buy Orders</div>
          <div class="text-xl font-bold text-blue-300 mt-1">${data ? `${data.total_items || 0} orders` : '--'}</div>
          <div class="text-[11px] text-blue-400/80 mt-0.5">Ready for EVE MultiBuy</div>
        </div>
      </div>

      <div class="bg-eveCard border border-eveBorder rounded-lg p-4 flex flex-wrap justify-between items-center gap-3">
        <div class="flex flex-wrap items-center gap-3">
          <${FilterPills}
            options=${[
              ['all', 'All Desks'],
              ['evergreen', 'Evergreen'],
              ['tactical', 'Tactical'],
              ['hold', 'Hold'],
            ]}
            value=${deskFilter}
            onChange=${setDeskFilter}
            activeClass="bg-blue-500/20 text-blue-300 border border-blue-500/40"
          />
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <button
            disabled=${loading}
            onClick=${() => auditMissing(deskFilter)}
            class="bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-600 px-3 py-1.5 rounded font-semibold transition flex items-center gap-1.5"
          >
            ${loading && html`<span class="animate-spin">⟳</span>`}
            <span>${loading ? 'Auditing ESI Books...' : 'Re-Audit Live Books'}</span>
          </button>

          <button
            disabled=${!data?.multibuy_text}
            onClick=${handleCopyBulk}
            class="bg-blue-600/30 hover:bg-blue-600/50 disabled:opacity-40 text-blue-200 border border-blue-500/50 px-4 py-1.5 rounded font-bold transition"
          >
            ${copiedBulk ? '✓ Copied EVE MultiBuy!' : 'Copy EVE MultiBuy Text'}
          </button>
        </div>
      </div>

      <div class="bg-blue-950/20 border border-blue-900/40 rounded-lg px-4 py-2.5 text-slate-300 flex flex-wrap justify-between items-center gap-2 text-[11px]">
        <span>In EVE Online, open <strong>Market → Multibuy</strong> and click <strong>"Import from Clipboard"</strong> to place all missing buy orders at once.</span>
        <span class="text-slate-400">Audited against live Jita 4-4 order books (+1 tick over top bid, capped at DeskProfile margin floor)</span>
      </div>

      <div class="bg-eveCard border border-eveBorder rounded-lg overflow-hidden">
        <div class="overflow-x-auto">
          <table class="w-full text-left">
            <thead class="bg-slate-900 border-b border-eveBorder text-slate-400 uppercase text-[10px]">
              <tr>
                <th class="p-3">#</th>
                ${deskFilter === 'all' && html`<th class="p-3">Desk</th>`}
                <th class="p-3">Item Name</th>
                <th class="p-3 text-right">Qty</th>
                <th class="p-3 text-right">Live Top Buy</th>
                <th class="p-3 text-right">Rec. Buy</th>
                <th class="p-3 text-right">ISK Needed</th>
                <th class="p-3 text-right">Est. Fees</th>
                <th class="p-3 text-right">Est. Return</th>
                <th class="p-3 text-right">Margin %</th>
                <th class="p-3 text-center">Copy Price</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800">
              ${loading && !data
                ? html`<tr><td colspan=${deskFilter === 'all' ? 11 : 10} class="p-8 text-center text-slate-400">Checking live ESI Jita 4-4 order books...</td></tr>`
                : errorMsg
                  ? html`<tr><td colspan=${deskFilter === 'all' ? 11 : 10} class="p-8 text-center text-rose-400">Failed to audit live ESI market books: ${errorMsg}</td></tr>`
                  : !items.length
                    ? html`<tr><td colspan=${deskFilter === 'all' ? 11 : 10} class="p-8 text-center text-emerald-300">All buy orders for this desk are already active and tracked in ESI!</td></tr>`
                    : items.map((it, i) => {
                      const lineNeeded = it.line_isk_needed ?? it.line_escrow ?? 0;
                      const lineFees = it.line_total_fees ?? 0;
                      const lineProfit = it.line_profit ?? 0;
                      return html`
                        <tr key=${it.type_id || i} class="hover:bg-slate-800/40 transition">
                          <td class="p-3 text-slate-500">${i + 1}</td>
                          ${deskFilter === 'all' && html`<td class="p-3"><${DeskBadge} desk=${it.desk} /></td>`}
                          <td class="p-3 font-semibold text-white">
                            <${MarketLink} typeId=${it.type_id} name=${it.name} />
                            ${it.squeezed && html`<span class="ml-1.5 text-[9px] bg-red-950 text-red-400 border border-red-800 px-1 py-0.5 rounded font-bold">MARGIN CAP</span>`}
                          </td>
                          <td class="p-3 text-right text-slate-300 font-semibold">${Number(it.qty || 0).toLocaleString()}</td>
                          <td class="p-3 text-right text-slate-400">${fmtPreciseISK(it.top_buy)}</td>
                          <td class="p-3 text-right text-eveCyan font-bold">${fmtPreciseISK(it.recommended_buy)}</td>
                          <td class="p-3 text-right text-slate-200 font-semibold">${fmtISK(lineNeeded)}</td>
                          <td class="p-3 text-right text-eveAmber/90">${fmtISK(lineFees)}</td>
                          <td class=${`p-3 text-right font-bold ${lineProfit >= 0 ? 'text-eveGreen' : 'text-rose-400'}`}>
                            ${lineProfit >= 0 ? '+' : ''}${fmtISK(lineProfit)}
                          </td>
                          <td class="p-3 text-right text-eveCyan font-semibold">${it.margin_pct}%</td>
                          <td class="p-3 text-center">
                            <button
                              onClick=${() => handleCopyPrice(it.recommended_buy, i)}
                              class=${`bg-slate-800 hover:bg-slate-700 border px-2.5 py-0.5 rounded text-[10px] transition ${copiedRow === i ? 'text-eveGreen border-emerald-600' : 'text-slate-200 border-slate-600'}`}
                            >
                              ${copiedRow === i ? 'Copied!' : 'Copy'}
                            </button>
                          </td>
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
