import { html, render, useState, useEffect, useCallback, useRef } from './lib.js';
import { fmtISK } from './utils.js';
import { Header } from './components/Header.js';
import { PerformanceTab } from './components/PerformanceTab.js';
import { SniperTab } from './components/SniperTab.js';
import { DeskTab } from './components/DeskTab.js';
import { ImportsTab } from './components/ImportsTab.js';
import { GuideTab } from './components/GuideTab.js';
import { MissingBuysModal } from './components/MissingBuysModal.js';
import { AdvisorModal } from './components/AdvisorModal.js';

function App() {
  const initialTab = new URLSearchParams(window.location.search).get('tab') || 'performance';
  const [currentTab, setCurrentTab] = useState(initialTab);
  const [globalData, setGlobalData] = useState(null);
  const [askPolling, setAskPolling] = useState(false);
  const [missingModalOpen, setMissingModalOpen] = useState(false);
  const [advisorModalOpen, setAdvisorModalOpen] = useState(false);

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
    handleSwitchTab('sniper');
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

  const dm = globalData?.desk_metrics || {};
  const tabs = [
    { key: 'performance', label: 'Trade Profits (30d)', pink: true },
    { key: 'sniper', label: 'Deal Sniper' },
    { key: 'evergreen', label: dm.evergreen?.cap !== undefined ? `Evergreen (${fmtISK(dm.evergreen.cap)})` : 'Evergreen' },
    { key: 'tactical', label: dm.tactical?.cap !== undefined ? `Tactical (${fmtISK(dm.tactical.cap)})` : 'Tactical' },
    { key: 'hold', label: dm.hold?.cap !== undefined ? `Hold (${fmtISK(dm.hold.cap)})` : 'Hold' },
    { key: 'imports', label: 'Imports (Amarr / C-J6MT)' },
    { key: 'guide', label: 'Strategy Playbook' },
  ];

  return html`
    <div class="space-y-6">
      <${Header}
        globalData=${globalData}
        onRunSniper=${handleRunSniper}
        onRunLedgers=${handleRunLedgers}
        onSyncEsi=${handleSyncEsi}
        onOpenMissingBuys=${() => setMissingModalOpen(true)}
        onOpenAdvisor=${() => setAdvisorModalOpen(true)}
      />

      <div class="flex flex-wrap gap-2 border-b border-eveBorder pb-2">
        ${tabs.map(t => {
          const active = currentTab === t.key;
          const activeCls = t.pink ? 'border-evePink text-evePink' : 'border-eveCyan text-eveCyan';
          return html`
            <button
              key=${t.key}
              onClick=${() => handleSwitchTab(t.key)}
              class=${`px-4 py-2 border-b-2 font-semibold text-sm transition ${active ? activeCls : 'border-transparent text-slate-400 hover:text-slate-200'}`}
            >
              ${t.label}
            </button>
          `;
        })}
      </div>

      ${currentTab === 'performance' && html`<${PerformanceTab} performance=${globalData?.performance} />`}
      ${currentTab === 'sniper' && html`<${SniperTab} sniper=${globalData?.sniper} />`}
      ${['evergreen', 'tactical', 'hold'].includes(currentTab) && html`
        <${DeskTab}
          deskKey=${currentTab}
          rows=${globalData?.desks?.[currentTab] || []}
          askPolling=${askPolling}
          onPollAsks=${handlePollAsks}
        />
      `}
      ${currentTab === 'imports' && html`<${ImportsTab} />`}
      ${currentTab === 'guide' && html`<${GuideTab} deskMetrics=${globalData?.desk_metrics} />`}

      <footer class="flex flex-wrap justify-between items-center text-xs text-slate-500 font-mono border-t border-eveBorder pt-4 gap-2">
        <div>Daily Auto-Run: <span class="text-slate-400">11:30 UTC</span> | ESI Polling: <span class="text-slate-400">Every 5 mins</span> | Market Asks: <span class="text-slate-400">Every 3 mins</span></div>
        <div>Last Ledger: ${globalData?.status?.last_ledger_run || 'Never'}</div>
      </footer>

      <${MissingBuysModal}
        isOpen=${missingModalOpen}
        onClose=${() => setMissingModalOpen(false)}
        currentDesk=${currentTab}
      />

      <${AdvisorModal}
        isOpen=${advisorModalOpen}
        onClose=${() => setAdvisorModalOpen(false)}
      />
    </div>
  `;
}

render(html`<${App} />`, document.getElementById('app'));
