import { html, useState, useEffect } from '../lib.js';
import { PerformanceTab } from './PerformanceTab.js';
import { SniperTab } from './SniperTab.js';
import { GuideTab } from './GuideTab.js';

export function AnalyticsTab({ performance, sniper, deskMetrics, initialSubTab = 'performance' }) {
  const [subTab, setSubTab] = useState(initialSubTab);

  useEffect(() => {
    if (initialSubTab) setSubTab(initialSubTab);
  }, [initialSubTab]);

  const dealCount = (sniper?.deals || []).length;

  return html`
    <div class="space-y-4">
      <div class="flex flex-wrap items-center justify-between bg-eveCard border border-eveBorder p-3 rounded-lg gap-3">
        <div class="flex flex-wrap items-center gap-1.5 bg-slate-950 p-1 rounded-lg border border-slate-800 text-xs">
          <button
            onClick=${() => setSubTab('performance')}
            class=${subTab === 'performance'
              ? 'px-3.5 py-1.5 rounded font-bold bg-evePink/20 text-evePink border border-evePink/40 transition'
              : 'px-3.5 py-1.5 rounded font-semibold text-slate-400 hover:text-slate-200 transition'}
          >
            Trade Profits (30d)
          </button>
          <button
            onClick=${() => setSubTab('sniper')}
            class=${subTab === 'sniper'
              ? 'px-3.5 py-1.5 rounded font-bold bg-eveCyan/20 text-eveCyan border border-eveCyan/40 transition'
              : 'px-3.5 py-1.5 rounded font-semibold text-slate-400 hover:text-slate-200 transition'}
          >
            Deal Sniper ${dealCount > 0 ? `(${dealCount})` : ''}
          </button>
          <button
            onClick=${() => setSubTab('guide')}
            class=${subTab === 'guide'
              ? 'px-3.5 py-1.5 rounded font-bold bg-slate-800 text-white border border-slate-600 transition'
              : 'px-3.5 py-1.5 rounded font-semibold text-slate-400 hover:text-slate-200 transition'}
          >
            Strategy Playbook
          </button>
        </div>
        <span class="text-xs text-slate-500 font-mono">Secondary Analytics, Firesale Scanner & Desk Reference</span>
      </div>

      ${subTab === 'performance' && html`<${PerformanceTab} performance=${performance} />`}
      ${subTab === 'sniper' && html`<${SniperTab} sniper=${sniper} />`}
      ${subTab === 'guide' && html`<${GuideTab} deskMetrics=${deskMetrics} />`}
    </div>
  `;
}
