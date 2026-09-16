/* Radio / transmitter panel.

   The number that matters during a pass is the *tuned* frequency — the
   published downlink plus the Doppler shift at this instant — so that is what
   is large. The nominal frequency is beside it in small type, because an
   operator needs to know which transmitter they are looking at, not to read it
   off the screen digit by digit.

   Doppler is only shown while the satellite is above the horizon. Below it the
   shift is still arithmetically defined and completely meaningless, and a
   confident wrong number on a wall display is worse than a blank.

   The backend computes the shift, from the same range rate the pass schedule
   and pointing error come from. Recomputing it here from the browser's own
   propagation would give a second answer that disagrees in the third decimal
   for no reason.
*/

import { api } from '../core/api.js';
import { store, set } from '../core/store.js';
import { bus } from '../core/bus.js';

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 2000;
// The waterfall only changes when a pass finishes and SatNOGS has processed it,
// which is minutes at best. Polling it at the Doppler rate would re-crop a
// 1.6 MB PNG for nothing.
const WATERFALL_MS = 120_000;

let timer = null;
let waterfallTimer = null;

export function mountRadio() {
  bus.on('radio', render);
  bus.on('waterfall', renderWaterfall);
  // The tracked satellite can change from the selector; the panel follows it.
  bus.on('satellite', () => { refresh(); refreshWaterfall(); });
  render();
  refresh();
  refreshWaterfall();

  clearInterval(timer);
  timer = setInterval(refresh, REFRESH_MS);
  clearInterval(waterfallTimer);
  waterfallTimer = setInterval(refreshWaterfall, WATERFALL_MS);
}

async function refresh() {
  try {
    set('radio', await api.radio(store.satellite?.norad ?? null));
  } catch (err) {
    console.warn('[radio]', err);
  }
}

async function refreshWaterfall() {
  try {
    set('waterfall', await api.waterfall(store.satellite?.norad ?? null));
  } catch (err) {
    console.warn('[waterfall]', err);
  }
}

function renderWaterfall() {
  const host = $('waterfall-panel');
  const hint = $('waterfall-hint');
  if (!host) return;

  const wf = store.waterfall;
  if (!wf || !wf.available) {
    host.innerHTML = '<p class="muted">no waterfall recorded yet</p>';
    if (hint) hint.textContent = '—';
    return;
  }

  const when = wf.start ? new Date(wf.start) : null;
  if (hint) {
    const stamp = when
      ? `${when.toISOString().slice(5, 16).replace('T', ' ')}Z`
      : `obs ${wf.id}`;
    // Decoded frames are the only unambiguous "we heard it": a waterfall can
    // look busy with interference, and vetting often never happens.
    hint.textContent = wf.frames
      ? `${stamp} · ${wf.frames} frame${wf.frames === 1 ? '' : 's'}`
      : stamp;
  }

  // Cache-bust on the observation id, not on Date.now(): the backend sets a
  // 2-minute cache header, and busting every poll would defeat it.
  host.innerHTML = `
    <a class="wf-frame" href="${wf.url}" target="_blank" rel="noopener noreferrer"
       title="observation ${wf.id} — open on SatNOGS">
      <img src="${wf.image_url}&v=${wf.id}" alt="">
      <span class="wf-axis wf-axis-x">time →</span>
      <span class="wf-axis wf-axis-y">±14 kHz</span>
      <span class="wf-badge s-${esc(wf.status || '')}">${esc(wf.status || '')}</span>
    </a>`;
}

function render() {
  const host = $('radio-panel');
  const hint = $('radio-hint');
  if (!host) return;

  const data = store.radio;
  if (!data || !data.transmitters?.length) {
    host.innerHTML = '<p class="muted">no published transmitters</p>';
    if (hint) hint.textContent = '—';
    return;
  }

  if (hint) {
    hint.textContent = data.visible
      ? `doppler live · el ${data.el?.toFixed(0)}°`
      : 'below horizon';
  }

  // Primary first: on a UHF station the VHF downlink is listed but is not what
  // anyone is tuning to.
  const rows = data.transmitters
    .slice()
    .sort((a, b) => (b.primary ? 1 : 0) - (a.primary ? 1 : 0));

  host.innerHTML = `<ul class="rx-list">${rows.map((tx) => row(tx, data)).join('')}</ul>`;
}

function row(tx, data) {
  const live = data.visible && tx.tuned_hz != null;
  const shown = live ? tx.tuned_hz : tx.downlink_hz;

  return `
    <li class="rx ${tx.primary ? 'rx-primary' : ''} ${tx.alive ? '' : 'rx-dead'}">
      <div class="rx-top">
        <span class="rx-freq ${live ? 'rx-live' : ''}">${mhz(shown)}<i>MHz</i></span>
        ${live ? `<span class="rx-shift ${tx.doppler_hz > 0 ? 'up' : 'down'}">
            ${tx.doppler_hz > 0 ? '+' : ''}${Math.round(tx.doppler_hz)} Hz</span>` : ''}
      </div>
      <div class="rx-meta">
        <span class="rx-mode">${esc(tx.mode)}</span>
        ${tx.baud ? `<span>${fmtBaud(tx.baud)}</span>` : ''}
        <span class="rx-type">${esc(tx.type)}</span>
        ${tx.primary ? '<span class="rx-tag">PRIMARY</span>' : ''}
        ${tx.alive ? '' : '<span class="rx-tag dead">INACTIVE</span>'}
      </div>
      <div class="rx-desc" title="${esc(tx.description)}">${esc(tx.description)}</div>
      ${live ? `<div class="rx-nominal">nominal ${mhz(tx.downlink_hz)} MHz</div>` : ''}
    </li>`;
}

function mhz(hz) {
  if (hz == null) return '—';
  // Four decimals is 100 Hz resolution: finer than any radio here tunes, and
  // enough to see the Doppler curve move during a pass.
  return (hz / 1e6).toFixed(4);
}

function fmtBaud(baud) {
  return baud >= 1000 ? `${baud / 1000}k` : `${baud}`;
}

function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}
