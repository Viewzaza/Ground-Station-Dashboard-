/* Canvas helpers. `fit` and `niceStep` are carried over unchanged from the
   previous dashboard in this repo — they were already correct. */

export function fit(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const r = canvas.getBoundingClientRect();
  const w = Math.max(1, Math.round(r.width));
  const h = Math.max(1, Math.round(r.height));
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr;
    canvas.height = h * dpr;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

export function niceStep(span, targetTicks) {
  const raw = span / Math.max(1, targetTicks);
  const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const norm = raw / mag;
  const step = norm > 5 ? 10 : norm > 2 ? 5 : norm > 1 ? 2 : 1;
  return step * mag;
}

/** Read the palette out of CSS so canvas drawing follows the theme tokens. */
export function palette() {
  const css = getComputedStyle(document.documentElement);
  const v = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
  return {
    line:    v('--line', '#1e2836'),
    lineSoft:v('--line-soft', '#161e29'),
    muted:   v('--muted', '#7a8ba3'),
    dim:     v('--dim', '#46566d'),
    text:    v('--text', '#dce5f0'),
    accent:  v('--accent', '#37d2f0'),
    accent2: v('--accent-2', '#8b7cf6'),
    ok:      v('--ok', '#3ddc84'),
    warn:    v('--warn', '#ffb020'),
    bad:     v('--bad', '#ff4d5e'),
    panel2:  v('--panel-2', '#0b1119'),
  };
}
