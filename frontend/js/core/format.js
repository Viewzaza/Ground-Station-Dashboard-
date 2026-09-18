/* Display formatting. Everything the operator reads passes through here so
   units and padding stay consistent across panels. */

export const pad2 = (n) => String(Math.floor(Math.abs(n))).padStart(2, '0');

export function hms(date) {
  return `${pad2(date.getUTCHours())}:${pad2(date.getUTCMinutes())}:${pad2(date.getUTCSeconds())}`;
}

export function hmsLocal(date, timeZone) {
  try {
    return new Intl.DateTimeFormat('en-GB', {
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false, timeZone,
    }).format(date);
  } catch {
    return hms(date);
  }
}

/** Countdown as -HH:MM:SS / +HH:MM:SS. Negative means the event has passed. */
export function countdown(seconds) {
  const sign = seconds < 0 ? '+' : '-';
  const s = Math.abs(Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${sign}${pad2(h)}:${pad2(m)}:${pad2(s % 60)}`;
}

export const deg = (v, dp = 1) =>
  (v === null || v === undefined || Number.isNaN(v)) ? '—' : `${v.toFixed(dp)}°`;

export const km = (v, dp = 0) =>
  (v === null || v === undefined || Number.isNaN(v)) ? '—' : `${v.toFixed(dp)} km`;

export function hz(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const a = Math.abs(v);
  if (a >= 1000) return `${(v / 1000).toFixed(2)} kHz`;
  return `${v.toFixed(0)} Hz`;
}

export function shortTime(iso, timeZone) {
  const d = new Date(iso);
  try {
    return new Intl.DateTimeFormat('en-GB', {
      hour: '2-digit', minute: '2-digit', hour12: false, timeZone: timeZone || 'UTC',
    }).format(d);
  } catch {
    return `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
  }
}
