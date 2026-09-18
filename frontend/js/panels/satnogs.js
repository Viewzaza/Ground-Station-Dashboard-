/* SatNOGS station 5024 activity.

   What the operator wants from this panel, in order: is the station's client
   actually connected, what is it going to record next, and did the last few
   observations produce anything. Everything else on network.satnogs.org is one
   click away and does not belong on a wall display.

   The station line doubles as the explanation for the rotator control panel
   sitting shut: "connected" there is the same fact as the `satnogs_idle` gate
   being closed, so the two are worth reading together. */

import { api } from '../core/api.js';
import { store, set } from '../core/store.js';
import { bus } from '../core/bus.js';

const $ = (id) => document.getElementById(id);
const FEED_ROWS = 5;

export function mountSatnogs() {
  bus.on('satnogs', render);
  render();

  // The poller publishes on its own cadence, so a browser opened between two
  // polls would otherwise sit on "awaiting SatNOGS" for a minute.
  api.satnogs()
    .then((data) => set('satnogs', data))
    .catch((err) => console.warn('[satnogs]', err));
}

function render() {
  const host = $('satnogs-activity');
  if (!host) return;

  const s = store.satnogs;
  if (!s) {
    host.innerHTML = '<p class="muted">awaiting SatNOGS</p>';
    return;
  }

  host.innerHTML = `
    ${stationLine(s)}
    ${nextJobLine(s)}
    <ul class="sn-feed">${feedRows(s)}</ul>`;
}

function stationLine(s) {
  const st = s.station;
  if (!st) return '<div class="sn-station muted">station unknown</div>';

  // is_connected is what the rotator interlock reads. Naming it the same way
  // in both places stops the two panels looking like they disagree.
  const connected = st.is_connected;
  return `
    <div class="sn-station">
      <span class="sn-dot ${connected ? 'on' : 'off'}"></span>
      <b>${escape(st.status || '—')}</b>
      <span class="muted">client ${connected ? 'connected' : 'not connected'}</span>
      <span class="sn-count">${st.observations ?? '—'} obs</span>
    </div>`;
}

function nextJobLine(s) {
  const secs = s.seconds_to_next_job;
  if (secs === null || secs === undefined) {
    return '<div class="sn-next muted">nothing scheduled</div>';
  }
  if (secs === 0) {
    return '<div class="sn-next live">observation in progress</div>';
  }
  const job = (s.jobs || [])
    .slice()
    .sort((a, b) => new Date(a.start) - new Date(b.start))
    .find((j) => new Date(j.end) > new Date());

  const name = job?.tle0 ? escape(String(job.tle0).replace(/^0 /, '')) : `#${job?.norad ?? '—'}`;
  return `<div class="sn-next">next <b>${name}</b> in ${countdown(secs)}</div>`;
}

function feedRows(s) {
  const rows = (s.observations || []).slice(0, FEED_ROWS);
  if (!rows.length) return '<li class="muted">no recent observations</li>';

  return rows.map((o) => {
    const name = escape(String(o.name || '').replace(/^0 /, '') || `#${o.norad}`);
    // "future" is SatNOGS's own word for a scheduled-but-not-yet-run
    // observation, and it is the commonest status on a busy station.
    const status = o.vetted_status && o.vetted_status !== 'unknown'
      ? o.vetted_status : (o.status || '');
    return `
      <li class="sn-row">
        <a href="${o.url}" target="_blank" rel="noopener noreferrer">${name}</a>
        <span class="sn-time">${shortTime(o.start)}</span>
        <span class="sn-status s-${escape(status)}">${escape(status)}</span>
        <span class="sn-data">${o.demoddata ? `${o.demoddata}▾` : (o.waterfall ? '≋' : '')}</span>
      </li>`;
  }).join('');
}

function countdown(secs) {
  if (secs < 60) return `${Math.round(secs)}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
}

function shortTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? ''
    : `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}Z`;
}

function escape(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}
