import { html, useState, useEffect, useMemo } from '../lib.js';
import { fmtISK, copyToClipboard } from '../utils.js';

export function ImportsTab() {
  const [importData, setImportData] = useState(null);
  const [dest, setDest] = useState('cj6mt');
  const [cj6mtRate, setCj6mtRate] = useState(800);
  const [amarrWarps, setAmarrWarps] = useState(35);
  const [hideStocked, setHideStocked] = useState(true);
  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  const [copyLabel, setCopyLabel] = useState('Copy Jita MultiBuy (Top 100)');

  const [bundlerOpen, setBundlerOpen] = useState(false);
  const [bundlePaste, setBundlePaste] = useState('');
  const [bundleLoading, setBundleLoading] = useState(false);
  const [bundleData, setBundleData] = useState(null);
  const [bundleError, setBundleError] = useState('');
  const [copiedBundleIdx, setCopiedBundleIdx] = useState(null);

  async function scanImports(force = false) {
    setLoading(true);
    setErrorMsg('');
    try {
      const url = `/api/import/opportunities?amarr_warps=${amarrWarps}&cj6mt_rate=${cj6mtRate}&force=${force}`;
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setImportData(data);
    } catch (err) {
      setErrorMsg(err.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    scanImports(false);
  }, []);

  const sec = importData?.[dest];
  const isCj = dest === 'cj6mt';

  const visibleItems = useMemo(() => {
    const raw = sec?.items || [];
    return hideStocked ? raw.filter(r => (Number(r.suggested_qty) || 0) > 0) : raw;
  }, [sec, hideStocked]);

  const totals = useMemo(() => {
    const allJitaBuy = (sec?.items || []).reduce((acc, r) => acc + ((Number(r.jita_ask) || 0) * (Number(r.suggested_qty) || 0)), 0);
    const totQty = visibleItems.reduce((s, r) => s + (Number(r.suggested_qty) || 0), 0);
    const totM3 = visibleItems.reduce((s, r) => s + ((Number(r.m3) || 0) * (Number(r.suggested_qty) || 0)), 0);
    const totJita = visibleItems.reduce((s, r) => s + ((Number(r.jita_ask) || 0) * (Number(r.suggested_qty) || 0)), 0);
    const totFreight = visibleItems.reduce((s, r) => s + ((Number(r.freight_cost) || 0) * (Number(r.suggested_qty) || 0)), 0);
    const totProf = visibleItems.reduce((s, r) => s + (Number(r.est_total_profit) || 0), 0);
    return { allJitaBuy, totQty, totM3, totJita, totFreight, totProf };
  }, [sec, visibleItems]);

  async function handleCopyMultibuy() {
    const actionable = (sec?.items || []).filter(r => (Number(r.suggested_qty) || 0) > 0).slice(0, 100);
    const text = actionable.length
      ? actionable.map(r => `${r.name} ${Number(r.suggested_qty)}`).join('\n')
      : (sec?.multibuy_text || '');
    if (!text) {
      alert('No MultiBuy items available to copy.');
      return;
    }
    const ok = await copyToClipboard(text);
    setCopyLabel(ok ? `Copied Top-${actionable.length || 100} MultiBuy!` : 'Copy Failed');
    setTimeout(() => setCopyLabel('Copy Jita MultiBuy (Top 100)'), 2000);
  }

  async function runBundler(fromHangar = false) {
    setBundleLoading(true);
    setBundleError('');
    try {
      const rateOverride = dest === 'amarr' ? amarrWarps : cj6mtRate;
      const res = await fetch('/api/import/bundles', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          dest_key: dest,
          raw_text: fromHangar ? null : bundlePaste,
          from_jita_hangar: fromHangar,
          rate_override: rateOverride,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setBundleData(await res.json());
    } catch (err) {
      setBundleError(err.message);
    } finally {
      setBundleLoading(false);
    }
  }

  async function handleCopyBundle(idx, copyText) {
    if (!copyText) return;
    const ok = await copyToClipboard(copyText);
    setCopiedBundleIdx(ok ? idx : -1);
    setTimeout(() => setCopiedBundleIdx(null), 1500);
  }

  const rateNote = useMemo(() => {
    if (!sec) return '';
    if (sec.pricing_model === 'hisec_tiered') {
      const splitNote = sec.split_plan_top20 ? ` | Top-100 Split: ${sec.split_plan_top20}` : '';
      return `Highsec ${sec.warps || 35}w Tiered (<=1.5B: 52.5M DST / 87.5M Freighter)${splitNote}`;
    }
    const colPct = ((Number(sec.collateral_pct) || 0) * 100).toFixed(1);
    const minNote = sec.min_freight > 0 ? ` | ${fmtISK(sec.min_freight)} Min / ${fmtISK(sec.max_freight)} Cap` : '';
    const trackNote = (isCj && sec.tracking_snapshots)
      ? ` | Burn Tracking: ${(Number(sec.tracking_days) || 0).toFixed(1)}d (${sec.tracking_snapshots} snaps, ${Math.round(Number(sec.tracking_confidence_pct) || 0)}% real)`
      : '';
    return `Courier: ${sec.freight_rate_m3} ISK/m³ | ${colPct}% Collateral${minNote}${trackNote}`;
  }, [sec, isCj]);

  return html`
    <div class="space-y-4">
      <div class="bg-evePanel border border-eveBorder rounded-lg p-4 flex flex-wrap justify-between items-center gap-3">
        <div class="flex flex-wrap items-center gap-2">
          <button
            onClick=${() => setDest('cj6mt')}
            class=${dest === 'cj6mt'
              ? 'px-3.5 py-1.5 rounded text-xs font-bold bg-eveCyan/20 text-eveCyan border border-eveCyan/50 transition'
              : 'px-3.5 py-1.5 rounded text-xs font-bold bg-slate-800 text-slate-400 border border-slate-700 hover:text-slate-200 transition'}
          >
            C-J6MT (1st Taj Mahgoon)
          </button>
          <button
            onClick=${() => setDest('amarr')}
            class=${dest === 'amarr'
              ? 'px-3.5 py-1.5 rounded text-xs font-bold bg-eveAmber/20 text-eveAmber border border-eveAmber/50 transition'
              : 'px-3.5 py-1.5 rounded text-xs font-bold bg-slate-800 text-slate-400 border border-slate-700 hover:text-slate-200 transition'}
          >
            Amarr VIII (Oris)
          </button>
          <div class="flex items-center gap-1.5 ml-2 text-xs text-slate-400">
            <span>JF ISK/m³:</span>
            <input type="number" value=${cj6mtRate} step="50" onInput=${e => setCj6mtRate(parseFloat(e.target.value) || 800)} class="w-20 bg-slate-900 border border-slate-700 rounded px-2 py-1 text-white text-xs font-mono" />
          </div>
          <div class="flex items-center gap-1.5 text-xs text-slate-400">
            <span>Amarr Warps:</span>
            <input type="number" value=${amarrWarps} step="1" onInput=${e => setAmarrWarps(parseInt(e.target.value || '35', 10))} class="w-16 bg-slate-900 border border-slate-700 rounded px-2 py-1 text-white text-xs font-mono" />
          </div>
          <button disabled=${loading} onClick=${() => scanImports(true)} class="bg-eveCyan/20 hover:bg-eveCyan/30 text-eveCyan border border-eveCyan/50 px-3 py-1.5 rounded text-xs font-bold transition flex items-center gap-1.5">
            ${loading && html`<span class="animate-spin">⟳</span>`}
            <span>${loading ? 'Scanning Books...' : 'Scan Live Books'}</span>
          </button>
        </div>
        <div class="flex items-center gap-3 text-xs text-slate-400">
          <span class="font-semibold text-slate-300">${sec?.count || 0} items</span>
          <span>Last scan: ${importData?.timestamp || '--'}</span>
        </div>
      </div>

      <div class="grid grid-cols-2 md:grid-cols-5 gap-3">
        <div class="bg-evePanel border border-eveBorder rounded-lg p-3">
          <div class="text-[10px] uppercase tracking-wider text-slate-400">Top-100 Batch Volume</div>
          <div class="text-base font-bold text-white mt-0.5">${Math.round(Number(sec?.multibuy_m3) || 0).toLocaleString()} m³</div>
        </div>
        <div class="bg-evePanel border border-eveBorder rounded-lg p-3">
          <div class="text-[10px] uppercase tracking-wider text-slate-400">Top-100 Collateral</div>
          <div class="text-base font-bold text-eveCyan mt-0.5">${fmtISK(Number(sec?.multibuy_collateral) || 0)}</div>
        </div>
        <div class="bg-evePanel border border-eveBorder rounded-lg p-3">
          <div class="text-[10px] uppercase tracking-wider text-slate-400">All-Items Jita Buy</div>
          <div class="text-base font-bold text-slate-200 mt-0.5">${fmtISK(totals.allJitaBuy)}</div>
        </div>
        <div class="bg-evePanel border border-eveBorder rounded-lg p-3" title=${sec?.pricing_model === 'hisec_tiered' ? `All-Items Split (${sec.count} items): ${sec.split_plan_all || ''} (${fmtISK(sec.all_freight || 0)})` : ''}>
          <div class="text-[10px] uppercase tracking-wider text-slate-400">Top-100 Freight Est.</div>
          <div class="text-base font-bold text-eveAmber mt-0.5">${fmtISK(Number(sec?.multibuy_freight) || 0)}</div>
        </div>
        <div class="bg-evePanel border border-eveBorder rounded-lg p-3">
          <div class="text-[10px] uppercase tracking-wider text-slate-400">Top-100 Net Profit</div>
          <div class="text-base font-bold text-eveGreen mt-0.5">+${fmtISK(Number(sec?.multibuy_profit) || 0)}</div>
        </div>
      </div>

      <div class="flex flex-wrap justify-between items-center gap-2 bg-evePanel border border-eveBorder rounded-lg p-3">
        <div class="flex flex-wrap items-center gap-2">
          <button onClick=${() => setBundlerOpen(v => !v)} class="bg-indigo-600/30 hover:bg-indigo-600/50 text-indigo-200 border border-indigo-500/50 px-3 py-1.5 rounded text-xs font-semibold transition">
            Bundle Purchased Items
          </button>
          <label class="inline-flex items-center gap-1.5 text-xs text-slate-300 bg-slate-900 border border-slate-700 px-2.5 py-1.5 rounded cursor-pointer select-none">
            <input type="checkbox" checked=${hideStocked} onChange=${e => setHideStocked(e.target.checked)} class="accent-eveCyan" />
            <span>Hide Stocked</span>
          </label>
          <button onClick=${handleCopyMultibuy} class="bg-emerald-600/30 hover:bg-emerald-600/40 text-emerald-200 border border-emerald-500/50 px-3 py-1.5 rounded text-xs font-semibold transition">
            ${copyLabel}
          </button>
        </div>
        <a href="/sso/login?role=cj6mt" class="bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border border-amber-500/40 px-3 py-1.5 rounded text-xs font-semibold transition">
          ${importData?.cj6mt?.character_name ? `C-J6MT Seller: ${importData.cj6mt.character_name} (Switch)` : 'C-J6MT Seller (Switch)'}
        </a>
      </div>

      ${bundlerOpen && html`
        <div class="bg-slate-950/90 border border-indigo-500/40 rounded-lg p-4 space-y-3">
          <div class="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-2 border-b border-slate-800 pb-2.5">
            <div>
              <h4 class="text-xs font-bold uppercase tracking-wider text-indigo-300">Courier Contract Bundler (Jita 4-4 → Destination)</h4>
              <p class="text-[11px] text-slate-400 mt-0.5">Paste purchased items from EVE Inventory or MultiBuy, or auto-detect matching import stock in your Jita hangar.</p>
            </div>
            <div class="flex items-center gap-2">
              <button onClick=${() => runBundler(true)} class="bg-indigo-600/30 hover:bg-indigo-600/50 text-indigo-200 border border-indigo-500/50 px-2.5 py-1 rounded text-[11px] font-semibold transition">
                Auto-Detect Jita Hangar
              </button>
              <button onClick=${() => setBundlerOpen(false)} class="text-slate-400 hover:text-white text-sm px-2">×</button>
            </div>
          </div>

          <div class="grid grid-cols-1 lg:grid-cols-3 gap-3">
            <div class="space-y-2">
              <textarea
                rows="5"
                value=${bundlePaste}
                onInput=${e => setBundlePaste(e.target.value)}
                class="w-full bg-slate-900 border border-slate-700 rounded p-2 text-xs text-slate-200 font-mono focus:outline-none focus:border-indigo-500"
                placeholder=${'Rattlesnake 2\nGuardian 7\nHarvester Mining Drone 3\nLarge Trimark Armor Pump I 126'}
              ></textarea>
              <button onClick=${() => runBundler(false)} class="w-full bg-eveCyan/20 hover:bg-eveCyan/30 text-eveCyan border border-eveCyan/40 py-1.5 rounded text-xs font-bold transition">
                Calculate Optimal Contracts
              </button>
            </div>

            <div class="lg:col-span-2 space-y-2 max-h-80 overflow-y-auto pr-1">
              ${bundleLoading
                ? html`<div class="text-xs text-slate-400 p-4 text-center">Packing items into minimum-cost courier contracts...</div>`
                : bundleError
                  ? html`<div class="text-xs text-rose-400 p-4 text-center">Bundler error: ${bundleError}</div>`
                  : !bundleData?.bundles?.length
                    ? html`<div class="text-xs text-slate-500 p-4 border border-dashed border-slate-800 rounded text-center">${bundleData ? 'No matching purchased items found to bundle.' : 'Paste purchased items on the left or click "Auto-Detect Jita Hangar" to pack into optimal courier contracts.'}</div>`
                    : html`
                      <div class="flex flex-wrap justify-between items-center bg-slate-900 border border-slate-700 px-3 py-2 rounded text-xs">
                        <span class="font-bold text-white">${bundleData.destination}: ${bundleData.contract_count} Optimal Contract${bundleData.contract_count > 1 ? 's' : ''}</span>
                        <span class="text-slate-300">Total Vol: <strong>${Math.round(bundleData.total_m3).toLocaleString()} m³</strong> | Collateral: <strong>${fmtISK(bundleData.total_collateral)}</strong> | Total Reward: <strong class="text-eveAmber">${fmtISK(bundleData.total_reward)}</strong></span>
                      </div>
                      ${bundleData.bundles.map((b, idx) => html`
                        <div key=${b.bundle_num || idx} class="bg-slate-900/70 border border-slate-800 rounded p-2.5 space-y-1.5 text-xs">
                          <div class="flex flex-wrap justify-between items-center gap-2">
                            <div class="flex items-center gap-2">
                              <span class="bg-indigo-950 text-indigo-300 border border-indigo-700 px-2 py-0.5 rounded font-bold">Contract #${b.bundle_num}</span>
                              <span class="text-eveCyan font-bold">${b.tier}</span>
                              <span class="text-slate-400">${Math.round(b.total_m3).toLocaleString()} m³ | Col: ${fmtISK(b.collateral)} | Reward: <strong class="text-eveAmber">${fmtISK(b.reward)}</strong></span>
                            </div>
                            <button onClick=${() => handleCopyBundle(idx, b.copy_text)} class="bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-600 px-2 py-0.5 rounded text-[11px] transition">
                              ${copiedBundleIdx === idx ? 'Copied!' : 'Copy Items'}
                            </button>
                          </div>
                          <div class="text-[11px] text-slate-300 pl-1">
                            ${(b.items || []).map(it => html`<span class="inline-block mr-3">${it.qty.toLocaleString()}x <strong class="text-white">${it.name}</strong> <span class="text-slate-500">(${fmtISK(it.collateral)})</span></span>`)}
                          </div>
                        </div>
                      `)}
                    `}
            </div>
          </div>
        </div>
      `}

      <div class="bg-evePanel border border-eveBorder rounded-lg overflow-hidden">
        <div class="p-3 bg-slate-900/80 border-b border-eveBorder flex flex-wrap justify-between items-center gap-2">
          <h3 class="text-xs font-bold uppercase tracking-wider text-eveCyan">
            ${isCj
              ? `Jita 4-4 → C-J6MT (1st Taj Mahgoon) Import Opportunities${sec?.character_name ? ` [Seller: ${sec.character_name}]` : ''}`
              : 'Jita 4-4 → Amarr VIII (Oris) Import Opportunities'}
          </h3>
          <span class="text-[11px] text-slate-400">${rateNote}</span>
        </div>

        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead class="bg-slate-900 border-b border-eveBorder text-slate-400 uppercase text-[10px]">
              <tr>
                <th class="p-3">#</th>
                <th class="p-3">Status</th>
                <th class="p-3">Item</th>
                <th class="p-3 text-right">Buy Qty</th>
                <th class="p-3 text-right">Imported / Stock</th>
                <th class="p-3 text-right">Vol (m³)</th>
                <th class="p-3 text-right">Jita Ask</th>
                <th class="p-3 text-right">Freight</th>
                <th class="p-3 text-right">Target Sell</th>
                <th class="p-3 text-right">Net Margin</th>
                <th class="p-3 text-right">ISK/m³</th>
                <th class="p-3 text-right">Est. Profit</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800">
              ${errorMsg
                ? html`<tr><td colspan="12" class="p-6 text-center text-rose-400">Failed to load import opportunities: ${errorMsg}</td></tr>`
                : loading && !importData
                  ? html`<tr><td colspan="12" class="p-6 text-center text-slate-400">Scanning Jita 4-4, Amarr VIII & C-J6MT structure books...</td></tr>`
                  : !visibleItems.length
                    ? html`<tr><td colspan="12" class="p-6 text-center text-slate-500">All candidate items are already stocked or no profitable opportunities found. Uncheck "Hide Stocked" to inspect existing inventory.</td></tr>`
                    : visibleItems.map((r, i) => {
                        const st = String(r.status || 'ACTIVE');
                        let badgeClass = 'bg-slate-800 text-slate-300 border-slate-700';
                        if (st === 'FULLY_STOCKED') badgeClass = 'bg-emerald-950/80 text-emerald-300 border-emerald-600/80';
                        else if (st === 'RESTOCK') badgeClass = 'bg-indigo-950/80 text-indigo-300 border-indigo-600/80';
                        else if (st === 'LOW_STOCK') badgeClass = 'bg-amber-950/80 text-amber-300 border-amber-600/80';
                        else if (st.startsWith('STOCKOUT')) badgeClass = 'bg-rose-950/80 text-rose-300 border-rose-600/80';
                        else if (st === 'ACTIVE_BOOK' || st === 'UNDERCUT_LIST') badgeClass = 'bg-emerald-950/80 text-emerald-300 border-emerald-700/80';

                        const qty = Number(r.suggested_qty) || 0;
                        const targetQty = Number(r.target_qty) || qty;
                        const localStock = Number(r.my_local_stock) || 0;
                        const sellQty = Number(r.my_sell_qty) || 0;
                        const hangarQty = Number(r.my_hangar_qty) || 0;
                        const jitaQty = Number(r.my_jita_qty) || 0;
                        const iskPerM3 = Number(r.isk_per_m3) || 0;
                        const iskM3 = iskPerM3 >= 1e6 ? (iskPerM3 / 1e6).toFixed(1) + 'M' : Math.round(iskPerM3).toLocaleString();
                        const volTip = r.vol_source && r.vol_source !== 'REGIONAL_30D'
                          ? `C-J6MT Burn: ~${r.dest_daily_vol}/day [${r.vol_source}]${r.observed_daily_vol !== null && r.observed_daily_vol !== undefined ? ` (Observed: ${r.observed_daily_vol}/d)` : ''} | Jita: ${r.jita_vol_30d}/d | Shelf Depth: ${r.dest_sell_vol}`
                          : `Amarr 30d Daily Vol: ${r.dest_daily_vol}/day`;

                        return html`
                          <tr key=${r.type_id || i} class="hover:bg-slate-800/40 transition" title=${volTip}>
                            <td class="p-3 text-slate-500">${i + 1}</td>
                            <td class="p-3"><span class=${`border px-2 py-0.5 rounded text-[10px] font-bold ${badgeClass}`}>${st}</span></td>
                            <td class="p-3 font-semibold text-white">
                              <a href=${`https://evetycoon.com/market/${r.type_id}`} target="_blank" class="hover:text-eveCyan hover:underline">${r.name}</a>
                              <span class="text-[10px] text-slate-500 ml-1">(${r.group || ''})</span>
                            </td>
                            <td class=${`p-3 text-right font-semibold ${qty > 0 ? 'text-slate-200' : 'text-emerald-400'}`}>
                              ${qty.toLocaleString()}
                              ${localStock > 0 && html`<div class="text-[10px] text-slate-400 font-normal">of ${targetQty.toLocaleString()} target</div>`}
                            </td>
                            <td class="p-3 text-right text-[11px]">
                              ${localStock > 0
                                ? html`
                                  <span class="text-cyan-300 font-semibold">${localStock.toLocaleString()} on hand</span>
                                  <div class="text-[10px] text-slate-400">
                                    ${[
                                      sellQty > 0 ? `${sellQty} listed` : '',
                                      hangarQty > 0 ? `${hangarQty} hangar` : '',
                                      jitaQty > 0 ? `${jitaQty} Jita` : '',
                                    ].filter(Boolean).join(' · ')}
                                  </div>`
                                : html`<span class="text-slate-600">0</span>`}
                            </td>
                            <td class="p-3 text-right text-slate-400">${Math.round((Number(r.m3) || 0) * qty).toLocaleString()}</td>
                            <td class="p-3 text-right text-slate-300">${fmtISK(r.jita_ask)}</td>
                            <td class="p-3 text-right text-eveAmber/90">${fmtISK(r.freight_cost)}</td>
                            <td class="p-3 text-right text-white font-semibold">${fmtISK(r.suggested_sell)}</td>
                            <td class="p-3 text-right text-eveCyan font-bold">
                              ${(Number(r.margin_pct) || 0).toFixed(1)}% <span class="text-[10px] text-slate-500 font-normal">(${(Number(r.solo_margin_pct) || Number(r.margin_pct) || 0).toFixed(1)}%)</span>
                            </td>
                            <td class="p-3 text-right text-slate-300">${iskM3}</td>
                            <td class="p-3 text-right text-eveGreen font-bold">+${fmtISK(r.est_total_profit)}</td>
                          </tr>
                        `;
                      })}
            </tbody>
            ${visibleItems.length > 0 && html`
              <tfoot class="bg-slate-900/90 border-t border-eveBorder font-bold text-xs">
                <tr>
                  <td class="p-3 text-slate-400" colspan="3">TOTAL (${visibleItems.length} ITEMS)</td>
                  <td class="p-3 text-right text-white">${totals.totQty.toLocaleString()}</td>
                  <td class="p-3"></td>
                  <td class="p-3 text-right text-slate-300">${Math.round(totals.totM3).toLocaleString()} m³</td>
                  <td class="p-3 text-right text-eveCyan">${fmtISK(totals.totJita)}</td>
                  <td class="p-3 text-right text-eveAmber">${fmtISK(totals.totFreight)}</td>
                  <td class="p-3 text-right text-slate-500" colspan="3">Total Jita Buy & Marginal Freight</td>
                  <td class="p-3 text-right text-eveGreen">+${fmtISK(totals.totProf)}</td>
                </tr>
              </tfoot>
            `}
          </table>
        </div>
      </div>
    </div>
  `;
}
