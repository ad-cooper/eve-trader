import { html } from '../lib.js';
import { fmtISK } from '../utils.js';

export function GuideTab({ deskMetrics }) {
  const egCap = deskMetrics?.evergreen?.cap;
  const tacCap = deskMetrics?.tactical?.cap;
  const hldCap = deskMetrics?.hold?.cap;

  return html`
    <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
      <div class="bg-eveCard border border-eveBorder p-6 rounded-lg space-y-3">
        <div class="flex items-center justify-between">
          <span class="text-xs font-mono px-2 py-0.5 rounded bg-cyan-950 text-eveCyan border border-cyan-800 font-bold uppercase">Desk 1</span>
          <span class="text-sm font-mono font-bold text-white">${egCap !== undefined ? `${fmtISK(egCap)} Allocated` : '35% Base Allocation'}</span>
        </div>
        <h3 class="text-lg font-bold text-white">Evergreen Desk (Core Turnover)</h3>
        <p class="text-xs text-slate-400 leading-relaxed">
          Fast 24-48h capital turnover on high-velocity staples. Exit pricing blends spot Jita asks with 30-day VWAP baselines and weekend spread-dip hysteresis so temporary Saturday undercuts hold bids instead of churning broker fees.
        </p>
        <div class="border-t border-slate-800 pt-3 space-y-1.5 text-xs font-mono">
          <div class="flex justify-between text-slate-400"><span>Target ROI:</span><span class="text-eveCyan font-semibold">6.0% – 10.0%</span></div>
          <div class="flex justify-between text-slate-400"><span>Turnover Velocity:</span><span class="text-slate-200">1 – 2 Days</span></div>
          <div class="flex justify-between text-slate-400"><span>Position Sizing:</span><span class="text-slate-200">≤ 22% Daily Volume</span></div>
        </div>
      </div>

      <div class="bg-eveCard border border-eveBorder p-6 rounded-lg space-y-3">
        <div class="flex items-center justify-between">
          <span class="text-xs font-mono px-2 py-0.5 rounded bg-purple-950 text-purple-400 border border-purple-800 font-bold uppercase">Desk 2</span>
          <span class="text-sm font-mono font-bold text-white">${tacCap !== undefined ? `${fmtISK(tacCap)} Allocated` : '45% Base Allocation'}</span>
        </div>
        <h3 class="text-lg font-bold text-white">Tactical Desk (Asymmetric Alpha)</h3>
        <p class="text-xs text-slate-400 leading-relaxed">
          Primary multi-day swing engine (3-5 days). Uses a 50/50 hybrid exit model: captures exploration and loot dump-dips while projecting recovery back toward the 30-day baseline VWAP.
        </p>
        <div class="border-t border-slate-800 pt-3 space-y-1.5 text-xs font-mono">
          <div class="flex justify-between text-slate-400"><span>Target ROI:</span><span class="text-purple-400 font-semibold">7.5% – 16.0%</span></div>
          <div class="flex justify-between text-slate-400"><span>Turnover Velocity:</span><span class="text-slate-200">3 – 5 Days</span></div>
          <div class="flex justify-between text-slate-400"><span>Position Sizing:</span><span class="text-slate-200">≤ 20% Daily Volume</span></div>
        </div>
      </div>

      <div class="bg-eveCard border border-eveBorder p-6 rounded-lg space-y-3">
        <div class="flex items-center justify-between">
          <span class="text-xs font-mono px-2 py-0.5 rounded bg-amber-950 text-eveAmber border border-amber-800 font-bold uppercase">Desk 3</span>
          <span class="text-sm font-mono font-bold text-white">${hldCap !== undefined ? `${fmtISK(hldCap)} Allocated` : '20% Base Allocation'}</span>
        </div>
        <h3 class="text-lg font-bold text-white">Hold Desk (Swing Speculation)</h3>
        <p class="text-xs text-slate-400 leading-relaxed">
          Structural swing speculation (1-3 weeks). Focuses on 180-day baseline VWAP recovery targets, exploiting deep-value market saturation dips.
        </p>
        <div class="border-t border-slate-800 pt-3 space-y-1.5 text-xs font-mono">
          <div class="flex justify-between text-slate-400"><span>Target ROI:</span><span class="text-eveAmber font-semibold">18.0% – 35.0%</span></div>
          <div class="flex justify-between text-slate-400"><span>Turnover Velocity:</span><span class="text-slate-200">1 – 3 Weeks</span></div>
          <div class="flex justify-between text-slate-400"><span>Position Sizing:</span><span class="text-slate-200">≤ 8% Daily Volume</span></div>
        </div>
      </div>
    </div>
  `;
}
