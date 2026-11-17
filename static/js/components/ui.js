import { html, useState } from '../lib.js';
import { copyToClipboard } from '../utils.js';

export function DeskBadge({ desk }) {
  const d = String(desk || '').toUpperCase();
  if (d === 'EVERGREEN') {
    return html`<span class="bg-cyan-950/70 text-cyan-300 border border-cyan-800 px-1.5 py-0.5 rounded text-[10px] font-bold">EVERGREEN</span>`;
  }
  if (d === 'TACTICAL') {
    return html`<span class="bg-purple-950/70 text-purple-300 border border-purple-800 px-1.5 py-0.5 rounded text-[10px] font-bold">TACTICAL</span>`;
  }
  if (d === 'HOLD') {
    return html`<span class="bg-amber-950/70 text-amber-300 border border-amber-800 px-1.5 py-0.5 rounded text-[10px] font-bold">HOLD</span>`;
  }
  return html`<span class="text-slate-400 font-semibold text-[10px] bg-slate-800 px-1.5 py-0.5 rounded border border-slate-700">${d || 'UNMANAGED'}</span>`;
}

export function MarketLink({ name, className = 'hover:text-eveCyan hover:underline' }) {
  const [copied, setCopied] = useState(false);

  if (!name) return null;

  async function handleCopy(e) {
    e.preventDefault();
    e.stopPropagation();
    const ok = await copyToClipboard(String(name));
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    }
  }

  return html`
    <button
      type="button"
      onClick=${handleCopy}
      title="Click to copy item name to clipboard"
      class=${`text-left cursor-pointer inline-flex items-center gap-1.5 transition ${className}`}
    >
      <span>${name}</span>
      ${copied && html`<span class="text-[9px] font-mono font-bold bg-emerald-950/90 text-eveGreen border border-emerald-600/80 px-1.5 py-0.2 rounded">✓ Copied</span>`}
    </button>
  `;
}

export function FilterPills({ options, value, onChange, activeClass = 'bg-slate-800 text-slate-100', className = 'text-xs' }) {
  return html`
    <div class=${`flex flex-wrap items-center gap-1.5 bg-slate-950 p-1 rounded-lg border border-slate-800 ${className}`}>
      ${options.map(([key, label]) => html`
        <button
          key=${key}
          onClick=${() => onChange(key)}
          class=${value === key
            ? `px-3 py-1.5 rounded font-bold transition ${activeClass}`
            : 'px-3 py-1.5 rounded font-semibold text-slate-400 hover:text-slate-200 transition'}
        >
          ${label}
        </button>
      `)}
    </div>
  `;
}
