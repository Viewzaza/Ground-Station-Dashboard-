/* Canvas drawing: strip charts, attitude indicator, compass, ground track.
 * No dependencies -- everything is drawn by hand so the dashboard runs
 * offline from a file:// URL.
 */

const CSS = getComputedStyle(document.documentElement);
const C = {
  line:   CSS.getPropertyValue('--line').trim()   || '#1f2937',
  muted:  CSS.getPropertyValue('--muted').trim()  || '#7c8ba1',
  dim:    CSS.getPropertyValue('--dim').trim()    || '#4b5a70',
  text:   CSS.getPropertyValue('--text').trim()   || '#dbe4ef',
  accent: CSS.getPropertyValue('--accent').trim() || '#37d2f0',
  ok:     CSS.getPropertyValue('--ok').trim()     || '#3ddc84',
  warn:   CSS.getPropertyValue('--warn').trim()   || '#ffb020',
  bad:    CSS.getPropertyValue('--bad').trim()    || '#ff4d5e'
};

/* Size the backing store to the device pixel ratio, else lines look furry. */
function fit(canvas) {
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

function niceStep(span, targetTicks) {
  const raw = span / Math.max(1, targetTicks);
  const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const norm = raw / mag;
  const step = norm > 5 ? 10 : norm > 2 ? 5 : norm > 1 ? 2 : 1;
  return step * mag;
}

/* ------------------------------------------------------------------ *
 * StripChart -- value against mission time, newest on the right.
 * ------------------------------------------------------------------ */
class StripChart {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.color = opts.color || C.accent;
    this.unit = opts.unit || '';
    this.window = opts.window || 120;   // seconds visible
    this.zeroLine = !!opts.zeroLine;
    this.pad = { l: 44, r: 8, t: 8, b: 18 };
  }

  draw(points) {
    const { ctx, w, h } = fit(this.canvas);
    const p = this.pad;
    ctx.clearRect(0, 0, w, h);

    const plotW = w - p.l - p.r;
    const plotH = h - p.t - p.b;
    if (plotW <= 0 || plotH <= 0) return;

    if (!points.length) {
      ctx.fillStyle = C.dim;
      ctx.font = '11px monospace';
      ctx.textAlign = 'center';
      ctx.fillText('awaiting telemetry', w / 2, h / 2);
      return;
    }

    // --- time window ---
    const tEnd = points[points.length - 1].t;
    const tStart = Math.max(points[0].t, tEnd - this.window);
    const vis = points.filter(d => d.t >= tStart);
    const span = Math.max(1e-6, tEnd - tStart);

    // --- value range, padded and snapped to a round step ---
    let lo = Infinity, hi = -Infinity;
    for (const d of vis) { if (d.v < lo) lo = d.v; if (d.v > hi) hi = d.v; }
    if (this.zeroLine) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
    if (hi - lo < 1e-6) { hi += 1; lo -= 1; }
    const margin = (hi - lo) * 0.12;
    lo -= margin; hi += margin;
    const step = niceStep(hi - lo, 4);
    lo = Math.floor(lo / step) * step;
    hi = Math.ceil(hi / step) * step;

    const X = t => p.l + ((t - tStart) / span) * plotW;
    const Y = v => p.t + plotH - ((v - lo) / (hi - lo)) * plotH;

    // --- gridlines and value labels ---
    ctx.font = '10px monospace';
    ctx.textBaseline = 'middle';
    for (let v = lo; v <= hi + step / 2; v += step) {
      const y = Math.round(Y(v)) + 0.5;
      ctx.strokeStyle = (this.zeroLine && Math.abs(v) < step / 100) ? C.dim : C.line;
      ctx.beginPath(); ctx.moveTo(p.l, y); ctx.lineTo(w - p.r, y); ctx.stroke();
      ctx.fillStyle = C.dim;
      ctx.textAlign = 'right';
      ctx.fillText(this._fmt(v), p.l - 6, y);
    }

    // --- time axis ---
    const tStep = niceStep(span, 4);
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (let t = Math.ceil(tStart / tStep) * tStep; t <= tEnd; t += tStep) {
      ctx.fillStyle = C.dim;
      ctx.fillText('+' + Math.round(t) + 's', X(t), h - p.b + 4);
    }

    // --- filled area under the trace ---
    ctx.save();
    ctx.beginPath();
    ctx.rect(p.l, p.t, plotW, plotH);
    ctx.clip();

    const baseY = this.zeroLine ? Y(0) : p.t + plotH;
    ctx.beginPath();
    ctx.moveTo(X(vis[0].t), baseY);
    for (const d of vis) ctx.lineTo(X(d.t), Y(d.v));
    ctx.lineTo(X(vis[vis.length - 1].t), baseY);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, p.t, 0, p.t + plotH);
    grad.addColorStop(0, this.color + '38');
    grad.addColorStop(1, this.color + '04');
    ctx.fillStyle = grad;
    ctx.fill();

    // --- trace ---
    ctx.beginPath();
    vis.forEach((d, i) => i ? ctx.lineTo(X(d.t), Y(d.v)) : ctx.moveTo(X(d.t), Y(d.v)));
    ctx.strokeStyle = this.color;
    ctx.lineWidth = 1.6;
    ctx.lineJoin = 'round';
    ctx.stroke();
    ctx.restore();

    // --- head marker and current value ---
    const last = vis[vis.length - 1];
    ctx.beginPath();
    ctx.arc(X(last.t), Y(last.v), 3, 0, Math.PI * 2);
    ctx.fillStyle = this.color;
    ctx.fill();

    ctx.font = '600 12px monospace';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'top';
    ctx.fillStyle = this.color;
    ctx.fillText(this._fmt(last.v) + ' ' + this.unit, w - p.r, p.t);
  }

  _fmt(v) {
    const a = Math.abs(v);
    if (a >= 1000) return Math.round(v).toString();
    if (a >= 10) return v.toFixed(0);
    return v.toFixed(1);
  }
}

/* ------------------------------------------------------------------ *
 * Attitude indicator -- artificial horizon for roll and pitch.
 * ------------------------------------------------------------------ */
function drawADI(canvas, roll, pitch) {
  const { ctx, w, h } = fit(canvas);
  const cx = w / 2, cy = h / 2, r = Math.min(w, h) / 2 - 6;
  ctx.clearRect(0, 0, w, h);

  ctx.save();
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.clip();

  // Horizon rotates with roll and slides with pitch.
  ctx.translate(cx, cy);
  ctx.rotate(-roll * Math.PI / 180);
  const pxPerDeg = r / 55;
  ctx.translate(0, pitch * pxPerDeg);

  ctx.fillStyle = '#1b4a6b';                       // sky
  ctx.fillRect(-r * 2.2, -r * 4, r * 4.4, r * 4);
  ctx.fillStyle = '#4a3420';                       // ground
  ctx.fillRect(-r * 2.2, 0, r * 4.4, r * 4);

  ctx.strokeStyle = '#cfe0ee';
  ctx.lineWidth = 1.4;
  ctx.beginPath(); ctx.moveTo(-r * 2, 0); ctx.lineTo(r * 2, 0); ctx.stroke();

  // Pitch ladder every 10 degrees.
  ctx.font = '9px monospace';
  ctx.fillStyle = '#cfe0ee';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.lineWidth = 1;
  for (let d = -60; d <= 60; d += 10) {
    if (!d) continue;
    const y = -d * pxPerDeg;
    const half = (d % 20 === 0) ? r * 0.38 : r * 0.2;
    ctx.beginPath(); ctx.moveTo(-half, y); ctx.lineTo(half, y); ctx.stroke();
    if (d % 20 === 0) ctx.fillText(Math.abs(d), -half - 12, y);
  }
  ctx.restore();

  // Fixed vehicle reference.
  ctx.strokeStyle = C.accent;
  ctx.lineWidth = 2.2;
  ctx.beginPath();
  ctx.moveTo(cx - r * 0.42, cy); ctx.lineTo(cx - r * 0.14, cy);
  ctx.moveTo(cx + r * 0.14, cy); ctx.lineTo(cx + r * 0.42, cy);
  ctx.stroke();
  ctx.beginPath(); ctx.arc(cx, cy, 2.4, 0, Math.PI * 2);
  ctx.fillStyle = C.accent; ctx.fill();

  // Roll pointer.
  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate(-roll * Math.PI / 180);
  ctx.beginPath();
  ctx.moveTo(0, -r + 1); ctx.lineTo(-5, -r + 10); ctx.lineTo(5, -r + 10);
  ctx.closePath();
  ctx.fillStyle = C.warn; ctx.fill();
  ctx.restore();

  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.strokeStyle = C.line; ctx.lineWidth = 1.5; ctx.stroke();
}

/* ------------------------------------------------------------------ *
 * Compass -- heading rose.
 * ------------------------------------------------------------------ */
function drawCompass(canvas, yaw) {
  const { ctx, w, h } = fit(canvas);
  const cx = w / 2, cy = h / 2, r = Math.min(w, h) / 2 - 6;
  ctx.clearRect(0, 0, w, h);

  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.fillStyle = '#0d131b'; ctx.fill();
  ctx.strokeStyle = C.line; ctx.lineWidth = 1.5; ctx.stroke();

  const CARDINALS = { 0: 'N', 90: 'E', 180: 'S', 270: 'W' };

  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate(-yaw * Math.PI / 180);
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  for (let a = 0; a < 360; a += 15) {
    const rad = a * Math.PI / 180;
    const major = a % 90 === 0;
    const len = major ? r * 0.18 : r * 0.09;
    ctx.beginPath();
    ctx.moveTo(Math.sin(rad) * r, -Math.cos(rad) * r);
    ctx.lineTo(Math.sin(rad) * (r - len), -Math.cos(rad) * (r - len));
    ctx.strokeStyle = major ? C.muted : C.line;
    ctx.lineWidth = major ? 1.6 : 1;
    ctx.stroke();
    if (major) {
      const rr = r - len - 10;
      ctx.save();
      ctx.translate(Math.sin(rad) * rr, -Math.cos(rad) * rr);
      ctx.rotate(yaw * Math.PI / 180);      // keep letters upright
      ctx.fillStyle = a === 0 ? C.bad : C.muted;
      ctx.font = '600 11px monospace';
      ctx.fillText(CARDINALS[a], 0, 0);
      ctx.restore();
    }
  }
  ctx.restore();

  // Fixed lubber line and readout.
  ctx.beginPath();
  ctx.moveTo(cx, cy - r + 2); ctx.lineTo(cx - 5, cy - r + 12); ctx.lineTo(cx + 5, cy - r + 12);
  ctx.closePath();
  ctx.fillStyle = C.accent; ctx.fill();

  ctx.fillStyle = C.text;
  ctx.font = '600 15px monospace';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(String(Math.round(yaw)).padStart(3, '0') + '°', cx, cy);
}

/* ------------------------------------------------------------------ *
 * Ground track -- lat/lon trail, launch site fixed at the origin.
 * ------------------------------------------------------------------ */
function drawTrack(canvas, path, origin) {
  const { ctx, w, h } = fit(canvas);
  ctx.clearRect(0, 0, w, h);

  if (!path.length || !origin) {
    ctx.fillStyle = C.dim;
    ctx.font = '11px monospace';
    ctx.textAlign = 'center';
    ctx.fillText('no GPS fix', w / 2, h / 2);
    return;
  }

  // Project to metres east/north of the launch site.
  const mPerDegLat = 111320;
  const mPerDegLon = 111320 * Math.cos(origin.lat * Math.PI / 180);
  const pts = path.map(p => ({
    x: (p.lon - origin.lon) * mPerDegLon,
    y: (p.lat - origin.lat) * mPerDegLat
  }));

  let ext = 60;
  for (const p of pts) ext = Math.max(ext, Math.abs(p.x), Math.abs(p.y));
  ext *= 1.25;

  const s = Math.min(w, h) / (2 * ext);
  const cx = w / 2, cy = h / 2;
  const X = x => cx + x * s;
  const Y = y => cy - y * s;

  // Range rings at a round interval.
  const ring = niceStep(ext, 3);
  ctx.font = '9px monospace';
  ctx.textAlign = 'left';
  ctx.textBaseline = 'bottom';
  for (let d = ring; d <= ext; d += ring) {
    ctx.beginPath(); ctx.arc(cx, cy, d * s, 0, Math.PI * 2);
    ctx.strokeStyle = C.line; ctx.lineWidth = 1; ctx.stroke();
    ctx.fillStyle = C.dim;
    ctx.fillText(d >= 1000 ? (d / 1000) + 'km' : d + 'm', cx + 3, cy - d * s - 2);
  }
  ctx.strokeStyle = C.line;
  ctx.beginPath();
  ctx.moveTo(0, cy); ctx.lineTo(w, cy);
  ctx.moveTo(cx, 0); ctx.lineTo(cx, h);
  ctx.stroke();

  // Trail.
  ctx.beginPath();
  pts.forEach((p, i) => i ? ctx.lineTo(X(p.x), Y(p.y)) : ctx.moveTo(X(p.x), Y(p.y)));
  ctx.strokeStyle = C.accent;
  ctx.lineWidth = 1.6;
  ctx.lineJoin = 'round';
  ctx.stroke();

  // Launch site.
  ctx.beginPath(); ctx.arc(cx, cy, 4, 0, Math.PI * 2);
  ctx.fillStyle = C.ok; ctx.fill();

  // Current position.
  const last = pts[pts.length - 1];
  ctx.beginPath(); ctx.arc(X(last.x), Y(last.y), 4.5, 0, Math.PI * 2);
  ctx.fillStyle = C.warn; ctx.fill();
  ctx.strokeStyle = '#0b0f14'; ctx.lineWidth = 1.5; ctx.stroke();
}
