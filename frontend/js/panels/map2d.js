/* 2D ground projection.

   Equirectangular, because that projection *is* the ground track: x is linear
   in longitude and y linear in latitude, so no projection maths is needed and
   the drawing stays exact. Coastlines come from Natural Earth 110m (public
   domain), vendored locally so the panel works on an isolated network.

   The antimeridian is the thing that breaks naive implementations: a step from
   lon 179 to -179 draws a line across the entire map. Every path here goes
   through splitAntimeridian() first.
*/

import { fit, palette } from '../lib/canvas.js';
import {
  splitAntimeridian, footprintRing, containsPole, subsolarPoint,
} from '../lib/geo.js';
import { store } from '../core/store.js';

let land = null;
let landPending = null;

async function loadLand() {
  if (land) return land;
  if (!landPending) {
    landPending = fetch('assets/ne_110m_land.json')
      .then((r) => r.json())
      .then((geo) => { land = geo; return land; })
      .catch((err) => { console.warn('[map2d] coastlines unavailable:', err); return null; });
  }
  return landPending;
}

/** Ring longitudes that jump more than 180 degrees wrap the map edge. */
function ringWraps(ring) {
  for (let i = 1; i < ring.length; i++) {
    if (Math.abs(ring[i][0] - ring[i - 1][0]) > 180) return true;
  }
  return false;
}

export class Map2D {
  constructor(canvas) {
    this.canvas = canvas;
    this.pal = palette();
    loadLand().then(() => this.draw());
  }

  // --- projection --------------------------------------------------------
  _x(lon) { return ((lon + 180) / 360) * this.w; }
  _y(lat) { return ((90 - lat) / 180) * this.h; }

  _path(ctx, points, close = false) {
    for (const segment of splitAntimeridian(points)) {
      ctx.beginPath();
      segment.forEach(([lat, lon], i) => {
        const x = this._x(lon);
        const y = this._y(lat);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      if (close) ctx.closePath();
      ctx.stroke();
    }
  }

  // --- layers ------------------------------------------------------------
  _drawLand(ctx) {
    if (!land) return;
    ctx.lineWidth = 1;

    for (const feature of land.features) {
      const geom = feature.geometry;
      const polys = geom.type === 'Polygon' ? [geom.coordinates] : geom.coordinates;

      for (const poly of polys) {
        for (const ring of poly) {
          const wraps = ringWraps(ring);

          ctx.beginPath();
          let pen = false;
          for (let i = 0; i < ring.length; i++) {
            const [lon, lat] = ring[i];
            if (i > 0 && Math.abs(lon - ring[i - 1][0]) > 180) {
              pen = false;                 // break rather than streak across
            }
            const x = this._x(lon);
            const y = this._y(lat);
            if (!pen) { ctx.moveTo(x, y); pen = true; } else { ctx.lineTo(x, y); }
          }

          if (!wraps) {
            ctx.closePath();
            ctx.fillStyle = '#111c27';
            ctx.fill();
          }
          ctx.strokeStyle = '#24384b';
          ctx.stroke();
        }
      }
    }
  }

  _drawGraticule(ctx) {
    ctx.strokeStyle = this.pal.lineSoft;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let lon = -180; lon <= 180; lon += 30) {
      const x = Math.round(this._x(lon)) + 0.5;
      ctx.moveTo(x, 0); ctx.lineTo(x, this.h);
    }
    for (let lat = -60; lat <= 60; lat += 30) {
      const y = Math.round(this._y(lat)) + 0.5;
      ctx.moveTo(0, y); ctx.lineTo(this.w, y);
    }
    ctx.stroke();

    ctx.strokeStyle = this.pal.line;
    ctx.beginPath();
    const eq = Math.round(this._y(0)) + 0.5;
    ctx.moveTo(0, eq); ctx.lineTo(this.w, eq);
    ctx.stroke();
  }

  /* Night is shaded column by column. The terminator latitude for a given
     longitude is analytic — tan(lat) = -cos(H)/tan(decl) — which avoids all the
     polygon-winding trouble a terminator ring causes near the poles. */
  _drawNight(ctx, when) {
    const [decl, subLon] = subsolarPoint(when);
    const tanDecl = Math.tan((decl * Math.PI) / 180);
    if (Math.abs(tanDecl) < 1e-6) return;          // equinox: degenerate, skip

    const nightIsSouth = decl > 0;
    const steps = 180;

    const curve = [];
    for (let i = 0; i <= steps; i++) {
      const lon = -180 + (360 * i) / steps;
      const H = ((lon - subLon) * Math.PI) / 180;
      const lat = Math.atan(-Math.cos(H) / tanDecl) * 180 / Math.PI;
      curve.push([this._x(lon), this._y(lat)]);
    }

    ctx.beginPath();
    curve.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.lineTo(this.w, nightIsSouth ? this.h : 0);
    ctx.lineTo(0, nightIsSouth ? this.h : 0);
    ctx.closePath();
    ctx.fillStyle = 'rgba(2, 4, 9, 0.62)';
    ctx.fill();

    // Near an equinox the shading alone is almost invisible, because the
    // terminator runs nearly pole to pole. Draw the boundary itself.
    ctx.beginPath();
    curve.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.strokeStyle = 'rgba(255, 176, 32, 0.30)';
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  _drawFootprint(ctx, pos) {
    const ring = footprintRing(pos.lat, pos.lon, pos.alt_km, 0, 96);
    if (!ring.length) return;

    ctx.save();
    ctx.strokeStyle = 'rgba(55, 210, 240, 0.55)';
    ctx.lineWidth = 1.2;

    const pole = containsPole(pos.lat, pos.alt_km);
    if (pole === 0) {
      // Ordinary case: a closed cap that can be filled directly.
      for (const segment of splitAntimeridian(ring)) {
        ctx.beginPath();
        segment.forEach(([lat, lon], i) => {
          const x = this._x(lon), y = this._y(lat);
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        });
        ctx.fillStyle = 'rgba(55, 210, 240, 0.08)';
        ctx.fill();
        ctx.stroke();
      }
    } else {
      // The ring encloses a pole, so it never closes on this projection.
      // Fill to the map edge instead.
      const sorted = ring.slice().sort((a, b) => a[1] - b[1]);
      ctx.beginPath();
      sorted.forEach(([lat, lon], i) => {
        const x = this._x(lon), y = this._y(lat);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.lineTo(this.w, pole > 0 ? 0 : this.h);
      ctx.lineTo(0, pole > 0 ? 0 : this.h);
      ctx.closePath();
      ctx.fillStyle = 'rgba(55, 210, 240, 0.08)';
      ctx.fill();
      ctx.stroke();
    }
    ctx.restore();
  }

  _drawTrack(ctx, track) {
    if (!track || track.length < 2) return;
    ctx.strokeStyle = this.pal.accent2;
    ctx.lineWidth = 1.6;
    ctx.setLineDash([]);
    this._path(ctx, track);
  }

  _drawStation(ctx, station) {
    const x = this._x(station.lon);
    const y = this._y(station.lat);
    ctx.strokeStyle = this.pal.ok;
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    ctx.moveTo(x - 5, y); ctx.lineTo(x + 5, y);
    ctx.moveTo(x, y - 5); ctx.lineTo(x, y + 5);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, Math.PI * 2);
    ctx.fillStyle = this.pal.ok;
    ctx.fill();
  }

  _drawSatellite(ctx, pos, station) {
    const x = this._x(pos.lon);
    const y = this._y(pos.lat);

    if (pos.el > 0 && station) {
      ctx.save();
      ctx.strokeStyle = 'rgba(61, 220, 132, 0.5)';
      ctx.setLineDash([3, 3]);
      ctx.lineWidth = 1;
      this._path(ctx, [[station.lat, station.lon], [pos.lat, pos.lon]]);
      ctx.restore();
    }

    ctx.beginPath();
    ctx.arc(x, y, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = pos.el > 0 ? this.pal.ok : this.pal.accent;
    ctx.fill();
    ctx.strokeStyle = '#05080d';
    ctx.lineWidth = 1.5;
    ctx.stroke();

    ctx.font = '10px monospace';
    ctx.fillStyle = this.pal.text;
    ctx.textAlign = x > this.w - 90 ? 'right' : 'left';
    ctx.textBaseline = 'bottom';
    const dx = x > this.w - 90 ? -8 : 8;
    ctx.fillText(store.satellite?.name || '', x + dx, y - 4);
  }

  // --- render ------------------------------------------------------------
  draw() {
    const { ctx, w, h } = fit(this.canvas);
    this.w = w; this.h = h;
    ctx.clearRect(0, 0, w, h);

    ctx.fillStyle = '#070c14';
    ctx.fillRect(0, 0, w, h);

    this._drawLand(ctx);
    this._drawGraticule(ctx);
    this._drawNight(ctx, new Date());

    const pos = store.satpos;
    if (pos) {
      this._drawFootprint(ctx, pos);
      this._drawTrack(ctx, store.groundTrack);
    }
    if (store.config?.station) this._drawStation(ctx, store.config.station);
    if (pos) this._drawSatellite(ctx, pos, store.config?.station);

    if (!land) {
      ctx.fillStyle = this.pal.dim;
      ctx.font = '11px monospace';
      ctx.textAlign = 'center';
      ctx.fillText('loading coastlines…', w / 2, h / 2);
    }
  }
}
