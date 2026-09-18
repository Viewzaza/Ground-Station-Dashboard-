/* Header: identity, clocks, AOS countdown, component health chips. */

import { bus } from '../core/bus.js';
import { store } from '../core/store.js';
import { hms, hmsLocal, countdown, deg } from '../core/format.js';

const $ = (id) => document.getElementById(id);

export function mountHeader() {
  const cfg = store.config;
  if (cfg?.station) {
    $('station-name').textContent =
      `SatNOGS ${cfg.station.id} · ${cfg.station.grid}`;
    $('clock-local-label').textContent =
      (cfg.station.timezone || 'LOCAL').split('/').pop().toUpperCase();
  }

  bus.on('satellite', (sat) => {
    if (!sat) return;
    $('sat-name').textContent = sat.name || '—';
    $('sat-norad').textContent = `NORAD ${sat.norad}`;
  });

  bus.on('status', () => paintChips());
  bus.on('nextPass', () => tick());
  paintChips();

  setInterval(tick, 1000);
  tick();
}

function tick() {
  const now = new Date();
  $('clock-utc').textContent = hms(now);
  $('clock-local').textContent =
    hmsLocal(now, store.config?.station?.timezone || 'UTC');

  const el = $('aos-countdown');
  const wrap = el.parentElement;
  const pass = store.nextPass;

  if (!pass) {
    el.textContent = '--:--:--';
    $('aos-detail').textContent = store.tle ? 'no pass in the next 24 h' : 'awaiting elements';
    wrap.className = 'hdr-aos';
    return;
  }

  const aos = new Date(pass.aos);
  const los = new Date(pass.los);
  const inPass = now >= aos && now < los;

  if (inPass) {
    $('aos-label').textContent = 'LOS IN';
    el.textContent = countdown((los - now) / 1000);
    wrap.className = 'hdr-aos in-pass';
  } else {
    const secs = (aos - now) / 1000;
    $('aos-label').textContent = 'NEXT AOS';
    el.textContent = countdown(secs);
    wrap.className = 'hdr-aos' + (secs < 600 ? ' imminent' : '');
  }

  $('aos-detail').textContent =
    `max el ${deg(pass.max_el, 0)} · az ${deg(pass.aos_az, 0)}→${deg(pass.los_az, 0)}`;
}

function paintChips() {
  for (const chip of document.querySelectorAll('.chip')) {
    const state = store.status[chip.dataset.chip];
    chip.className = 'chip' + (state ? ` ${state}` : '');
    if (chip.dataset.chip === 'tle' && store.tle) {
      chip.textContent = `TLE ${store.tle.age_days.toFixed(1)}d`;
    }
  }
}
