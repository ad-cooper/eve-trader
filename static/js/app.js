import { html, render, useState, useEffect, useCallback, useRef, useMemo } from './lib.js';
import { Header } from './components/Header.js';
import { DeskTab } from './components/DeskTab.js';
import { OrderActionsTab } from './components/OrderActionsTab.js';
import { BuyOrdersTab } from './components/BuyOrdersTab.js';
import { SellAdvisorTab } from './components/SellAdvisorTab.js';
import { ImportsTab } from './components/ImportsTab.js';
import { AnalyticsTab } from './components/AnalyticsTab.js';

function normalizeInitialRoute(rawTab) {
  const t = String(rawTab || '').toLowerCase();
  if (['evergreen', 'tactical', 'hold'].includes(t)) {
    return { tab: 'desks', deskFilter: t, analyticsSub: 'performance' };
  }
  if (['performance', 'sniper', 'guide'].includes(t)) {
    return { tab: 'analytics', deskFilter: 'all', analyticsSub: t };
  }
  if (['desks', 'actions', 'buys', 'sells', 'imports', 'analytics'].includes(t)) {
    return { tab: t, deskFilter: 'all', analyticsSub: 'performance' };
  }
  return { tab: 'desks', deskFilter: 'all', analyticsSub: 'performance' };
}

function App() {
  const initialRoute = useMemo(() => {
    const param = new URLSearchParams(window.location.search).get('tab');
    return normalizeInitialRoute(param);
  }, []);

  const [currentTab, setCurrentTab] = useState(initialRoute.tab);
  const [deskFilter, setDeskFilter] = useState(initialRoute.deskFilter);
  const [analyticsSubTab, setAnalyticsSubTab] = useState(initialRoute.analyticsSub);
  const [globalData, setGlobalData] = useState(null);
  const [askPolling, setAskPolling] = useState(false);

  const fetchingRef = useRef(false);

  const fetchState = useCallback(async () => {
    if (fetchingRef.current) return;
    fetchingRef.current = true;
    try {
      const res = await fetch('/api/state');
      if (res.ok) {
        setGlobalData(await res.json());
      }
    } catch (err) {
      console.error('FetchData error:', err);
    } finally {
      fetchingRef.current = false;
    }
  }, []);

  useEffect(() => {
    fetchState();
  }, [fetchState]);

  useEffect(() => {
    const busy = askPolling || globalData?.status?.sniper_running || globalData?.status?.ledger_running;
    const intervalMs = busy ? 3000 : 60000;
    const id = setInterval(() => {
      if (!document.hidden) fetchState();
    }, intervalMs);
    return () => clearInterval(id);
  }, [askPolling, globalData?.status?.sniper_running, globalData?.status?.ledger_running, fetchState]);

  useEffect(() => {
    const onVis = () => {
      if (!document.hidden) fetchState();
    };
    document.addEventListener('visibilitychange', onVis);
    return () => document.removeEventListener('visibilitychange', onVis);
  }, [fetchState]);

  function handleSwitchTab(tab) {
    setCurrentTab(tab);
    const url = new URL(window.location.href);
    url.searchParams.set('tab', tab);
    window.history.replaceState({}, '', url);
  }

  async function handleRunSniper() {
    setAnalyticsSubTab('sniper');
    handleSwitchTab('analytics');
    try {
      await fetch('/api/sniper/run', { method: 'POST' });
      fetchState();
    } catch (e) {
      console.error('Failed to trigger deal sniper:', e);
    }
  }

  async function handleRunLedgers() {
    try {
      await fetch('/api/ledgers/run', { method: 'POST' });
      fetchState();
    } catch (e) {
      console.error('Failed to trigger ledger rebuild:', e);
    }
  }

  async function handleSyncEsi() {
    try {
      await fetch('/api/orders/sync-esi', { method: 'POST' });
      fetchState();
    } catch (e) {
      console.error('Failed to sync ESI:', e);
    }
  }

  async function handlePollAsks() {
    setAskPolling(true);
    try {
      await fetch('/api/market/refresh-prices', { method: 'POST' });
      setTimeout(fetchState, 4500);
      setTimeout(fetchState, 9000);
    } finally {
      setTimeout(() => setAskPolling(false), 9500);
    }
  }

  const unplacedCount = useMemo(() => {
    const d = globalData?.desks || {};
    const all = [...(d.evergreen || []), ...(d.tactical || []), ...(d.hold || [])];
    return all.filter(r => r.char_status === 'UNPLACED').length;
  }, [globalData]);

  const actionBadgeCount = globalData?.action_counts?.total ?? 0;

  const tabs = [
    { key: 'desks', label: '1. Active Desks', accent: 'cyan' },
    { key: 'actions', label: '2. Order Actions (Bump / Cancel)', badge: actionBadgeCount > 0 ? actionBadgeCount : null, accent: 'rose' },
    { key: 'buys', label: '3. Buy Orders (MultiBuy)', badge: unplacedCount > 0 ? unplacedCount : null, accent: 'blue' },
    { key: 'sells', label: '4. Hangar Sell (MultiSell)', accent: 'indigo' },
    { key: 'imports', label: '5. Imports (C-J6MT / Amarr)', accent: 'amber' },
    { key: 'analytics', label: 'Analytics & Tools', accent: 'pink' },
  ];

  return html`
    <div class="space-y-6">
      <${Header}
        globalData=${globalData}
        onRunSniper=${handleRunSniper}
        onRunLedgers=${handleRunLedgers}
        onSyncEsi=${handleSyncEsi}
      />

      <nav class="flex flex-wrap gap-2 border-b border-eveBorder pb-2">
        ${tabs.map(t => {
          const active = currentTab === t.key;
          const activeCls =
            t.accent === 'rose'
              ? 'border-rose-400 text-rose-300'
              : t.accent === 'blue'
                ? 'border-blue-400 text-blue-300'
              : t.accent === 'indigo'
                ? 'border-indigo-400 text-indigo-300'
                : t.accent === 'amber'
                  ? 'border-eveAmber text-eveAmber'
                  : t.accent === 'pink'
                    ? 'border-evePink text-evePink'
                    : 'border-eveCyan text-eveCyan';
          return html`
            <button
              key=${t.key}
              onClick=${() => handleSwitchTab(t.key)}
              class=${`px-4 py-2 border-b-2 font-semibold text-sm transition inline-flex items-center gap-2 ${active ? activeCls : 'border-transparent text-slate-400 hover:text-slate-200'}`}
            >
              <span>${t.label}</span>
              ${t.badge !== null && t.badge !== undefined && html`
                <span class="px-1.5 py-0.2 rounded-full text-[11px] font-mono bg-blue-950 text-blue-300 border border-blue-700">
                  ${t.badge}
                </span>
              `}
            </button>
          `;
        })}
      </nav>

      ${currentTab === 'desks' && html`
        <${DeskTab}
          desks=${globalData?.desks || {}}
          deskMetrics=${globalData?.desk_metrics || {}}
          initialDesk=${deskFilter}
          askPolling=${askPolling}
          onPollAsks=${handlePollAsks}
        />
      `}
      ${currentTab === 'actions' && html`<${OrderActionsTab} />`}
      ${currentTab === 'buys' && html`<${BuyOrdersTab} />`}
      ${currentTab === 'sells' && html`<${SellAdvisorTab} />`}
      ${currentTab === 'imports' && html`<${ImportsTab} />`}
      ${currentTab === 'analytics' && html`
        <${AnalyticsTab}
          performance=${globalData?.performance}
          sniper=${globalData?.sniper}
          deskMetrics=${globalData?.desk_metrics}
          initialSubTab=${analyticsSubTab}
        />
      `}

      <footer class="flex flex-wrap justify-between items-center text-xs text-slate-500 font-mono border-t border-eveBorder pt-4 gap-2">
        <div>Daily Auto-Run: <span class="text-slate-400">11:30 UTC</span> | ESI Polling: <span class="text-slate-400">Every 5 mins</span> | Market Asks: <span class="text-slate-400">Every 3 mins</span></div>
        <div>Last Ledger: ${globalData?.status?.last_ledger_run || 'Never'}</div>
      </footer>
    </div>
  `;
}

render(html`<${App} />`, document.getElementById('app'));
