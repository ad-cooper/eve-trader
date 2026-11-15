export function fmtISK(val) {
  if (!val && val !== 0) return '0 ISK';
  if (Math.abs(val) >= 1e9) return (val / 1e9).toFixed(2) + 'B ISK';
  if (Math.abs(val) >= 1e6) return (val / 1e6).toFixed(1) + 'M ISK';
  return Math.round(val).toLocaleString() + ' ISK';
}

export async function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      console.warn('navigator.clipboard failed, attempting fallback', err);
    }
  }

  try {
    const textArea = document.createElement('textarea');
    textArea.value = text;
    textArea.style.position = 'fixed';
    textArea.style.left = '-999999px';
    textArea.style.top = '-999999px';
    textArea.setAttribute('readonly', '');
    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();
    const successful = document.execCommand('copy');
    textArea.remove();
    return successful;
  } catch (err) {
    console.error('Remote LAN copy fallback failed:', err);
    return false;
  }
}
