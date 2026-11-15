import { html, useState, useEffect } from '../lib.js';
import { fmtISK, copyToClipboard } from '../utils.js';

export function MissingBuysModal({ isOpen, onClose, currentDesk }) {
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState(null);
  const [errorMsg, setErrorMsg] = useState('');
  const [copiedBulk, setCopiedBulk] = useState(false);
  const [copiedRow, setCopiedRow] = useState(null);

  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;

    async function auditMissing() {
      setLoading(true);
      setErrorMsg('');
      setData(null);
      try {
        const deskParam = ['evergreen', 'tactical', 'hold'].includes(currentDesk) ? currentDesk : 'all';
        const res = await fetch(`/api/orders/missing-buys?desk=${deskParam}`);
        if (!res.ok) throw new Error(`Server returned HTTP ${res.status}`);
        const json = await res.json();
        if (cancelled) return;
        setData(json);
        if (json.multibuy_text) {
          const ok = await copyToClipboard(json.multibuy_text);
          if (!cancelled) setCopiedBulk(ok);
        }
      } catch (err) {
        if (!cancelled) setErrorMsg(err.message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    auditMissing();
    return () => {
      cancelled = true;
    };
  }, [isOpen, currentDesk]);

  if (!isOpen) return null;

  async function handleCopyBulk() {
    if (!data?.multibuy_text) return;
    const ok = await copyToClipboard(data.multibuy_text);
    setCopiedBulk(ok);
  }

  async function handleCopyPrice(price, idx) {
    const ok = await copyToClipboard(String(price));
    setCopiedRow(ok ? idx : -1);
    setTimeout(() => setCopiedRow(null), 1200);
  }

  const items = data?.items || [];

  return html`
    <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4">
      <div class="bg-eveCard border border-eveBorder rounded-xl max-w-5xl w-full max-h-[90vh] flex flex-col shadow-2xl font-mono text-xs">
        <div class="p-4 border-b border-eveBorder flex justify-between items-center bg-slate-900/60 rounded-t-xl">
          <div class="flex items-center gap-3">
            <div class="h-2.5 w-2.5 rounded-full bg-blue-400"></div>
            <h3 class="text-sm font-bold uppercase tracking-wider text-white">Live ESI Audited Buy Orders</h3>
            <span class="bg-blue-950 text-blue-300 border border-blue-800 px-2 py-0.5 rounded text-[10px]">${data?.total_items || 0} items</span>
            <span class="text-slate-400">Total Escrow: <strong class="text-white">${data ? fmtISK(data.total_escrow) : '--'}</strong></span>
          </div>
          <button onClick=${onClose} class="text-slate-400 hover:text-white text-lg px-2 py-0.5">×</button>
        </div>

        <div class="p-3 bg-blue-950/20 border-b border-blue-900/40 flex flex-col sm:flex-row justify-between items-start sm:items-center gap-2 text-slate-300">
          <div class="flex items-center gap-2 text-[11px]">
            ${copiedBulk && html`<span class="text-emerald-400 font-bold">✓ Copied to clipboard!</span>`}
            <span>In EVE, open <strong>Market → Multibuy</strong> and click <strong>"Import from Clipboard"</strong>.</span>
          </div>
          <button onClick=${handleCopyBulk} class="bg-blue-600/30 hover:bg-blue-600/40 text-blue-200 border border-blue-500/50 px-2.5 py-1 rounded text-[11px] transition">
            Copy EVE Multibuy Text
          </button>
        </div>

        <div class="overflow-y-auto flex-1 p-0">
          <table class="w-full text-left">
            <thead class="bg-slate-900 sticky top-0 border-b border-eveBorder text-slate-400 uppercase text-[10px]">
              <tr>
                <th class="p-2.5">#</th>
                <th class="p-2.5">Item Name</th>
                <th class="p-2.5 text-right">Qty</th>
                <th class="p-2.5 text-right">Live Top Buy</th>
                <th class="p-2.5 text-right">Rec. Buy (+0.01)</th>
                <th class="p-2.5 text-right">Margin %</th>
                <th class="p-2.5 text-right">Escrow</th>
                <th class="p-2.5 text-center">Action</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800">
              ${loading
                ? html`<tr><td colspan="8" class="p-8 text-center text-slate-400">Checking live ESI Jita 4-4 order books...</td></tr>`
                : errorMsg
                  ? html`<tr><td colspan="8" class="p-8 text-center text-rose-400">Failed to audit live ESI market books: ${errorMsg}</td></tr>`
                  : !items.length
                    ? html`<tr><td colspan="8" class="p-8 text-center text-emerald-300">All buy orders for this desk are already active and tracked in ESI!</td></tr>`
                    : items.map((it, i) => html`
                      <tr key=${it.type_id || i} class="hover:bg-slate-800/40 transition">
                        <td class="p-2.5 text-slate-500">${i + 1}</td>
                        <td class="p-2.5 font-semibold text-white">
                          <a href=${`https://evetycoon.com/market/${it.type_id}`} target="_blank" class="hover:text-eveCyan hover:underline">${it.name}</a>
                          ${it.squeezed && html`<span class="ml-1.5 text-[9px] bg-red-950 text-red-400 border border-red-800 px-1 py-0.5 rounded font-bold">MARGIN CAP</span>`}
                        </td>
                        <td class="p-2.5 text-right text-slate-300 font-semibold">${Number(it.qty || 0).toLocaleString()}</td>
                        <td class="p-2.5 text-right text-slate-400">${fmtISK(it.top_buy)}</td>
                        <td class="p-2.5 text-right text-eveCyan font-bold">${fmtISK(it.recommended_buy)}</td>
                        <td class="p-2.5 text-right text-eveGreen font-semibold">${it.margin_pct}%</td>
                        <td class="p-2.5 text-right text-slate-300">${fmtISK(it.line_escrow)}</td>
                        <td class="p-2.5 text-center">
                          <button
                            onClick=${() => handleCopyPrice(it.recommended_buy, i)}
                            class=${`bg-slate-800 hover:bg-slate-700 border px-2 py-0.5 rounded text-[10px] transition ${copiedRow === i ? 'text-eveGreen border-emerald-600' : 'text-slate-200 border-slate-600'}`}
                          >
                            ${copiedRow === i ? 'Copied!' : 'Copy'}
                          </button>
                        </td>
                      </tr>
                    `)}
            </tbody>
          </table>
        </div>

        <div class="p-3 border-t border-eveBorder bg-slate-900/40 flex justify-between items-center text-slate-500 text-[11px] rounded-b-xl">
          <div>Prices calculated with 0.50% Citadel SCC broker fee and Jita 4-4 exits.</div>
          <button onClick=${onClose} class="bg-slate-800 hover:bg-slate-700 text-slate-200 px-4 py-1.5 rounded transition">Close</button>
        </div>
      </div>
    </div>
  `;
}
