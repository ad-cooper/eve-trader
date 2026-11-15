import { html, useState, useEffect, useCallback } from '../lib.js';
import { fmtISK, copyToClipboard } from '../utils.js';
import { MarketLink, FilterPills } from './ui.js';

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

  return html`
    <div class="space-y-4 font-mono text-xs">
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

          <div class="flex items-center gap-3 text-slate-300">
            <span class="bg-blue-950 text-blue-300 border border-blue-800 px-2.5 py-1 rounded font-bold">
              ${data?.total_items || 0} missing orders
            </span>
            <span>Total Escrow Needed: <strong class="text-white text-sm">${data ? fmtISK(data.total_escrow) : '--'}</strong></span>
          </div>
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
        <span class="text-slate-400">Audited against live Jita 4-4 order books (+0.01 ISK over top bid, capped at DeskProfile margin floor)</span>
      </div>

      <div class="bg-eveCard border border-eveBorder rounded-lg overflow-hidden">
        <div class="overflow-x-auto">
          <table class="w-full text-left">
            <thead class="bg-slate-900 border-b border-eveBorder text-slate-400 uppercase text-[10px]">
              <tr>
                <th class="p-3">#</th>
                <th class="p-3">Item Name</th>
                <th class="p-3 text-right">Qty</th>
                <th class="p-3 text-right">Live Top Buy</th>
                <th class="p-3 text-right">Rec. Buy (+0.01)</th>
                <th class="p-3 text-right">Margin %</th>
                <th class="p-3 text-right">Escrow</th>
                <th class="p-3 text-center">Copy Price</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800">
              ${loading && !data
                ? html`<tr><td colspan="8" class="p-8 text-center text-slate-400">Checking live ESI Jita 4-4 order books...</td></tr>`
                : errorMsg
                  ? html`<tr><td colspan="8" class="p-8 text-center text-rose-400">Failed to audit live ESI market books: ${errorMsg}</td></tr>`
                  : !items.length
                    ? html`<tr><td colspan="8" class="p-8 text-center text-emerald-300">All buy orders for this desk are already active and tracked in ESI!</td></tr>`
                    : items.map((it, i) => html`
                      <tr key=${it.type_id || i} class="hover:bg-slate-800/40 transition">
                        <td class="p-3 text-slate-500">${i + 1}</td>
                        <td class="p-3 font-semibold text-white">
                          <${MarketLink} typeId=${it.type_id} name=${it.name} />
                          ${it.squeezed && html`<span class="ml-1.5 text-[9px] bg-red-950 text-red-400 border border-red-800 px-1 py-0.5 rounded font-bold">MARGIN CAP</span>`}
                        </td>
                        <td class="p-3 text-right text-slate-300 font-semibold">${Number(it.qty || 0).toLocaleString()}</td>
                        <td class="p-3 text-right text-slate-400">${fmtISK(it.top_buy)}</td>
                        <td class="p-3 text-right text-eveCyan font-bold">${fmtISK(it.recommended_buy)}</td>
                        <td class="p-3 text-right text-eveGreen font-semibold">${it.margin_pct}%</td>
                        <td class="p-3 text-right text-slate-300">${fmtISK(it.line_escrow)}</td>
                        <td class="p-3 text-center">
                          <button
                            onClick=${() => handleCopyPrice(it.recommended_buy, i)}
                            class=${`bg-slate-800 hover:bg-slate-700 border px-2.5 py-0.5 rounded text-[10px] transition ${copiedRow === i ? 'text-eveGreen border-emerald-600' : 'text-slate-200 border-slate-600'}`}
                          >
                            ${copiedRow === i ? 'Copied!' : 'Copy'}
                          </button>
                        </td>
                      </tr>
                    `)}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  `;
}
