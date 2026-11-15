import { html } from '../lib.js';
import { fmtISK } from '../utils.js';

export function Header({
  globalData,
  onRunSniper,
  onRunLedgers,
  onSyncEsi,
}) {
  const m = globalData?.metrics || {};
  const c = globalData?.character || {};
  const st = globalData?.status || {};

  return html`
    <div class="space-y-6">
      <header class="flex flex-col md:flex-row justify-between items-start md:items-center border-b border-eveBorder pb-5 gap-4">
        <div>
          <div class="flex items-center gap-3">
            <div class="h-3 w-3 rounded-full bg-eveCyan shadow-[0_0_8px_#06b6d4]"></div>
            <h1 class="text-2xl font-bold tracking-wider uppercase text-white">EVE Trading Desk</h1>
            <span class="text-xs bg-cyan-950 text-eveCyan border border-cyan-800 px-2 py-0.5 rounded font-mono">AUTO-EQUITY</span>
          </div>
          <p class="text-xs text-slate-400 mt-1">Daily Waterfall Engine • Jita MultiBuy & Hangar MultiSell • Cross-Region Imports</p>
        </div>

        <div class="flex flex-wrap items-center gap-2.5">
          ${c.is_linked
            ? html`
              <div class="flex items-center gap-2 bg-emerald-950/60 border border-emerald-600/60 px-3 py-1.5 rounded text-xs font-mono text-emerald-300">
                <img class="w-6 h-6 rounded-full border border-emerald-400" src=${`https://images.evetech.net/characters/${c.character_id}/portrait?size=64`} />
                <span>${c.name}</span>
                <button onClick=${onSyncEsi} class="ml-1 text-slate-400 hover:text-white" title="Sync ESI Orders & Wallet">↻</button>
              </div>`
            : html`
              <a href="/sso/login" class="flex items-center gap-2 bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border border-amber-500/40 px-3.5 py-2 rounded text-sm font-semibold transition">
                <span>Authorize ESI (Hippie Father)</span>
              </a>`
          }

          <button onClick=${onRunLedgers} class="flex items-center gap-2 bg-eveCyan/20 hover:bg-eveCyan/30 text-eveCyan border border-eveCyan/40 px-3.5 py-2 rounded text-sm font-semibold transition">
            ${st.ledger_running && html`<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>`}
            <span>${st.ledger_running ? 'Rebuilding Ledgers...' : 'Rebuild Daily Ledgers'}</span>
          </button>

          <button onClick=${onRunSniper} class="flex items-center gap-2 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-600 px-3.5 py-2 rounded text-sm font-semibold transition">
            ${st.sniper_running && html`<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>`}
            <span>${st.sniper_running ? 'Scanning Firesales...' : 'Run Deal Sniper'}</span>
          </button>
        </div>
      </header>

      <div class="grid grid-cols-2 md:grid-cols-6 gap-4 font-mono">
        <div class="bg-eveCard border border-eveBorder p-4 rounded-lg">
          <div class="text-xs text-slate-400 uppercase">Capital Deployed</div>
          <div class="text-xl md:text-2xl font-bold text-white mt-1">${globalData ? fmtISK(m.total_capital) : '--'}</div>
          <div class="text-[10px] text-slate-500 mt-0.5">${globalData ? `Budget: ${fmtISK(m.portfolio_budget || m.total_capital)}` : 'Budget: Auto-Equity'}</div>
        </div>
        <div class="bg-eveCard border border-eveBorder p-4 rounded-lg">
          <div class="text-xs text-slate-400 uppercase">Projected Net</div>
          <div class="text-xl md:text-2xl font-bold text-eveGreen mt-1">${globalData ? `+${fmtISK(m.total_profit)}` : '--'}</div>
          <div class="text-[10px] text-slate-500 mt-0.5">Per Turnover Cycle</div>
        </div>
        <div class="bg-eveCard border border-eveBorder p-4 rounded-lg">
          <div class="text-xs text-slate-400 uppercase">Blended ROI</div>
          <div class="text-xl md:text-2xl font-bold text-eveCyan mt-1">${globalData ? `${(m.blended_roi || 0).toFixed(1)}%` : '--'}</div>
          <div class="text-[10px] text-slate-500 mt-0.5">Net After Jita Fees</div>
        </div>
        <div class="bg-eveCard border border-eveBorder p-4 rounded-lg">
          <div class="text-xs text-slate-400 uppercase">Liquid Reserve</div>
          <div class="text-xl md:text-2xl font-bold text-eveAmber mt-1">${globalData ? fmtISK(m.liquid_reserve) : '--'}</div>
          <div class="text-[10px] text-slate-500 mt-0.5">Available Wallet ISK</div>
        </div>
        <div class="bg-eveCard border border-eveBorder p-4 rounded-lg">
          <div class="text-xs text-slate-400 uppercase">Ledger Coverage</div>
          <div class="text-xl md:text-2xl font-bold text-blue-400 mt-1">${c.ledger_coverage || '--'}</div>
          <div class="text-[10px] text-slate-500 mt-0.5">ESI Tracked</div>
        </div>
        <div class="bg-eveCard border border-eveBorder p-4 rounded-lg">
          <div class="text-xs text-slate-400 uppercase">Active ESI Orders</div>
          <div class="text-xl md:text-2xl font-bold text-slate-200 mt-1">${globalData ? `${c.orders_count || 0} orders` : '0'}</div>
          <div class="text-[10px] text-slate-500 mt-0.5">Escrow: ${fmtISK(c.total_escrow || 0)}</div>
        </div>
      </div>
    </div>
  `;
}
