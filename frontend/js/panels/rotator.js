/* Rotator polar plot.

   Zenith at the centre, north up, horizon at the rim — the view an operator
   reads to see where the antenna is against where the satellite is.

   Until the rotctld bridge lands (phase 5) this draws the predicted arc and the
   live satellite marker, with the antenna marker absent rather than faked. A
   dashboard that invents a pointing position is worse than one that admits it
   does not know.
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
    if (rot) {
      const [x, y] = this._xy(rot.az_rose, Math.max(0, rot.el), cx, cy, r);
      ctx.beginPath();
      ctx.moveTo(x, y - 6); ctx.lineTo(x - 5, y + 4); ctx.lineTo(x + 5, y + 4);
      ctx.closePath();
      ctx.strokeStyle = this.pal.warn;
      ctx.lineWidth = 1.6;
      ctx.stroke();
    } else {
      ctx.fillStyle = this.pal.dim;
      ctx.font = '9px monospace';
      ctx.fillText('no rotator link', cx, cy + r + 6);
    }
  }
}

export function paintRotatorReadout() {
  const rot = store.rotator;
  const pos = store.satpos;

  if (!rot) {
    $('rot-az').textContent = '—';
    $('rot-el').textContent = '—';
    $('rot-err').textContent = '—';
    $('rot-source').textContent = 'offline';
    return;
  }

  // The raw azimuth is shown first: a reading of 412 deg means the rotator is
  // wound past north, which matters for cable wrap. The 0-360 value is only a
  // convenience for reading against a compass.
  $('rot-az').textContent =
    `${rot.az_raw.toFixed(1)}° (${rot.az_rose.toFixed(1)}°)`;
  $('rot-el').textContent = deg(rot.el, 1);
  $('rot-source').textContent = rot.source;

  if (pos && pos.el > 0) {
    const dAz = Math.abs(((rot.az_rose - pos.az + 540) % 360) - 180);
    const dEl = Math.abs(rot.el - pos.el);
    $('rot-err').textContent = deg(Math.hypot(dAz, dEl), 1);
  } else {
    $('rot-err').textContent = '—';
  }
}
