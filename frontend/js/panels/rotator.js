/* Rotator polar plot.

   Zenith at the centre, north up, horizon at the rim — the view an operator
   reads to see where the antenna is against where the satellite is.

   When the rotctld link is down the antenna marker disappears and the plot
   says so, rather than leaving the last known position drawn as though it were
   current. A dashboard that shows a stale pointing position during a pass is
   worse than one that admits it does not know.
*/

import { fit, palette } from '../lib/canvas.js';
import { store } from '../core/store.js';
import { deg } from '../core/format.js';

const $ = (id) => document.getElementById(id);

export class PolarPlot {
  constructor(canvas) {
    this.canvas = canvas;
    this.pal = palette();
    this.track = null;          // [{az, el}] for the current pass
  }

  setTrack(samples) {
    this.track = samples;
  }

  /** Elevation to radius: 90 deg (zenith) at the centre, 0 at the rim. */
  _rk(el) { return Math.max(0, Math.min(1, (90 - el) / 90)); }

  _xy(az, el, cx, cy, r) {
    const a = ((az - 90) * Math.PI) / 180;    // 0 deg az = up
    const k = this._rk(el) * r;
    return [cx + k * Math.cos(a), cy + k * Math.sin(a)];
  }

  draw() {
    const { ctx, w, h } = fit(this.canvas);
    const cx = w / 2;
    const cy = h / 2;
    const r = Math.min(w, h) / 2 - 12;
    ctx.clearRect(0, 0, w, h);
    if (r <= 10) return;

    // --- rings at 0 / 30 / 60 degrees elevation ---
    ctx.font = '9px monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    for (const el of [0, 30, 60]) {
      ctx.beginPath();
      ctx.arc(cx, cy, this._rk(el) * r, 0, Math.PI * 2);
      ctx.strokeStyle = el === 0 ? this.pal.line : this.pal.lineSoft;
      ctx.lineWidth = 1;
      ctx.stroke();
      if (el) {
        ctx.fillStyle = this.pal.dim;
        ctx.fillText(`${el}°`, cx + 2, cy - this._rk(el) * r);
      }
    }

    // --- cardinal spokes ---
    ctx.strokeStyle = this.pal.lineSoft;
    ctx.beginPath();
    for (let az = 0; az < 360; az += 30) {
      const [x, y] = this._xy(az, 0, cx, cy, r);
      ctx.moveTo(cx, cy); ctx.lineTo(x, y);
    }
    ctx.stroke();

    ctx.fillStyle = this.pal.muted;
    ctx.font = '600 10px monospace';
    for (const [label, az] of [['N', 0], ['E', 90], ['S', 180], ['W', 270]]) {
      const [x, y] = this._xy(az, -7, cx, cy, r);
      ctx.fillStyle = az === 0 ? this.pal.bad : this.pal.muted;
      ctx.fillText(label, x, y);
    }

    // --- predicted arc for the current pass ---
    if (this.track?.length) {
      ctx.beginPath();
      let pen = false;
      for (const s of this.track) {
        if (s.el < 0) { pen = false; continue; }
        const [x, y] = this._xy(s.az, s.el, cx, cy, r);
        if (!pen) { ctx.moveTo(x, y); pen = true; } else { ctx.lineTo(x, y); }
      }
      ctx.setLineDash([4, 3]);
      ctx.strokeStyle = this.pal.accent2;
      ctx.lineWidth = 1.4;
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // --- live satellite ---
    const pos = store.satpos;
    if (pos && pos.el > -2) {
      const [x, y] = this._xy(pos.az, Math.max(0, pos.el), cx, cy, r);
      ctx.beginPath();
      ctx.arc(x, y, 4.5, 0, Math.PI * 2);
      ctx.fillStyle = pos.el >= 0 ? this.pal.ok : this.pal.dim;
      ctx.fill();
      ctx.strokeStyle = '#05080d';
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }

    // --- antenna, when the rotator bridge is connected ---
    const rot = store.rotator;
    if (rot && rot.link === 'up') {
      const [x, y] = this._xy(rot.az_rose, Math.max(0, rot.el), cx, cy, r);
      ctx.beginPath();
      ctx.moveTo(x, y - 6); ctx.lineTo(x - 5, y + 4); ctx.lineTo(x + 5, y + 4);
      ctx.closePath();
      ctx.strokeStyle = this.pal.warn;
      ctx.lineWidth = 1.6;
      ctx.stroke();
    } else {
      ctx.fillStyle = rot ? this.pal.bad : this.pal.dim;
      ctx.font = '9px monospace';
      ctx.fillText(rot ? 'ROTATOR LINK DOWN' : 'no rotator link', cx, cy + r + 6);
    }
  }
}

export function paintRotatorReadout() {
  const rot = store.rotator;

  if (!rot) {
    $('rot-az').textContent = '—';
    $('rot-el').textContent = '—';
    $('rot-err').textContent = '—';
    $('rot-source').textContent = 'offline';
    return;
  }

  // The raw azimuth is shown first: a reading of 412° means the rotator is
  // wound past north, which matters for cable wrap. The 0-360 value is only a
  // convenience for reading against a compass.
  const wound = rot.wrap && rot.wrap !== 'none';
  $('rot-az').textContent = wound
    ? `${rot.az_raw.toFixed(1)}° (${rot.az_rose.toFixed(1)}°)`
    : deg(rot.az_raw, 1);
  $('rot-az').classList.toggle('wound', !!wound);
  $('rot-az').title = wound
    ? `wound ${rot.wrap === 'cw' ? 'clockwise' : 'anticlockwise'} past north`
    : '';

  $('rot-el').textContent = deg(rot.el, 1);
  $('rot-source').textContent = rot.link === 'up'
    ? `${rot.source} · ${Math.round(rot.latency_ms)} ms`
    : `${rot.source} · LINK ${rot.link.toUpperCase()}`;

  // The pointing error is computed by the backend, against the same schedule
  // the antenna is driven from — not re-derived here from a different source.
  const err = store.pointing;
  const errEl = $('rot-err');
  if (!err) {
    errEl.textContent = '—';
    errEl.className = '';
  } else {
    errEl.textContent = deg(err.total_error_deg, 1);
    // SatNOGS itself tracks with a 4-degree deadband, so anything under about
    // 5 degrees is normal operation, not a fault worth colouring red.
    errEl.className = err.total_error_deg > 5 ? 'err-bad' : '';
  }
}
