/* Decoded frames.

   Everything else on this wall says where the spacecraft should be and what to
   listen on. This says whether any of it worked: SatNOGS's own record of what
   was actually demodulated, newest first. The headline is the age of the newest
   frame, because a satellite that is propagating perfectly and one that has
   been silent for two days are indistinguishable on the map, the globe and the
   polar plot alike — "heard 3 min ago" is the only line on the display that
   can tell them apart.

   Two sources answer with two different shapes, and the panel has to render
   both. The public Network API needs no credentials and gives frame bytes with
   no interpretation. SatNOGS DB gives the decoder's output, including real
   scalars, but only to a station holding an API token. The backend reaches for
   whichever it can and names its choice in `source`, so a row here may or may
   not have AX.25 addressing, may or may not have decoded values, and may or may
   not have an observation to link to. None of that is an error state.
*/

import { api } from '../core/api.js';
import { store, set } from '../core/store.js';
import { bus } from '../core/bus.js';

const $ = (id) => document.getElementById(id);

// A frame can only appear when a pass happens, and the backend serves its own
// cached copy with an age attached. Polling faster would re-read the same object
// for a satellite that is overhead for ten minutes a day.
const REFRESH_MS = 30_000;

// A whole short pass, and then the list becomes a log — which is what the
// observation pages on SatNOGS are for.
const MAX_ROWS = 8;

// Past this the newest frame is history rather than news, and the headline
// gives up the live hue. An hour is roughly six KNACKSAT-2 passes' worth of
// waiting, so a green-lit panel still means the last pass worked.
const LIVE_S = 3600;

// The rows come from one API or the other, and which one matters: only the DB
// carries decoded values, so an operator seeing no values wants to know whether
// that means the spacecraft said nothing or that we are reading the feed that
// never says anything.
const SOURCE = {
  'satnogs-network': 'SatNOGS network',
  'satnogs-db': 'SatNOGS DB',
};

// A station between passes, or one without a DB token, is the ordinary state of
// this panel rather than a fault, so every branch names the situation instead of
// printing "no data". The backend's own `detail` is preferred wherever it has
// one — it knows which setting is missing and this file should not guess.
const TROUBLE = {
  no_token: 'no SatNOGS API token configured, so only public frames are read',
  bad_token: 'SatNOGS rejected the station API token',
  offline: 'offline — frames are not being fetched',
  unreachable: 'SatNOGS did not answer; this is the last copy we have',
};

let timer = null;

export function mountTelemetry() {
  bus.on('telemetry', render);
  // The selector can move the whole wall to another satellite; the frames follow
  // it rather than sitting on the previous one's history.
  bus.on('satellite', refresh);
  render();
  refresh();

  clearInterval(timer);
  timer = setInterval(refresh, REFRESH_MS);
}

async function refresh() {
  try {
    set('telemetry', await api.telemetry(store.satellite?.norad ?? null));
  } catch (err) {
    console.warn('[telemetry]', err);
    // Repaint anyway. The headline is an age measured against now, so a backend
    // that has stopped answering has to show the last heard time growing old.
    // Freezing it at "2 min ago" for the rest of the afternoon would be the one
    // failure this panel must not have.
    render();
  }
}

function render() {
  const body = $('tlm-body');
  if (!body) return;

  const t = store.telemetry;

  if (!t) {
    headline(null);
    body.innerHTML = '<p class="tlm-note">awaiting SatNOGS</p>';
    return;
  }

  headline(t);

  const frames = (t.frames || []).slice(0, MAX_ROWS);
  if (!t.available || !frames.length) {
    // bad_token is the only one of these an operator has to act on: something
    // was configured and is being refused, rather than not configured at all.
    const fix = t.status === 'bad_token' ? ' tlm-fix' : '';
    body.innerHTML = `<p class="tlm-note${fix}">${esc(explain(t))}</p>`;
    return;
  }

  body.innerHTML = `<div class="tlm-rows">${frames.map(row).join('')}</div>`;
}

function headline(t) {
  const age = $('tlm-age');
  const hint = $('tlm-hint');

  if (age) {
    const secs = t ? ageSeconds(t.last_heard) : null;
    age.textContent = secs === null ? '—' : relative(secs);
    age.className = secs !== null && secs < LIVE_S ? 'tlm-age live' : 'tlm-age';
  }

  if (!hint) return;
  const bits = [];
  if (t?.source) bits.push(SOURCE[t.source] || t.source);
  if (t?.count) bits.push(`${t.count} frame${t.count === 1 ? '' : 's'}`);
  // `stale` is about our copy of the feed, not about the spacecraft: the backend
  // has stopped refreshing. Nothing else on the panel would change to say so,
  // and a frozen list that looks current is worse than an empty one.
  if (t?.stale) bits.push('copy stale');
  // textContent, not innerHTML — this line carries an API-supplied source name.
  hint.textContent = bits.length ? bits.join(' · ') : '—';
}

function row(f) {
  const ours = isOurs(f);
  const values = decodedValues(f);
  // Only the Network shape carries a URL. A DB frame's observation_id is not
  // reliably a Network observation — app_source can be `sids`, a direct
  // submission with no page behind it — so a link is never synthesised from it,
  // and those rows render as plain text rather than as a promise that 404s.
  const url = f.observation_url;
  const tag = url ? 'a' : 'div';
  const label = f.observation_id != null ? `observation ${f.observation_id}` : 'this observation';
  const link = url
    ? ` href="${esc(url)}" target="_blank" rel="noopener noreferrer"` +
      ` title="${esc(label)} — open on SatNOGS"`
    : '';
  const preview = payload(f);

  return `
    <${tag} class="tlm-row${ours ? ' ours' : ''}"${link}>
      <span class="tlm-time">${esc(hms(f.timestamp))}</span>
      <span class="tlm-who">${esc(who(f))}</span>
      <span class="tlm-bytes">${esc(size(f.bytes))}</span>
      ${f.ax25 ? `<span class="tlm-ax">${esc(addressing(f.ax25))}</span>` : ''}
      ${values.length ? `<span class="tlm-vals">${values.map(chip).join('')}</span>` : ''}
      ${preview ? `<span class="tlm-head">${esc(preview)}</span>` : ''}
    </${tag}>`;
}

/* Which station heard it. The Network shape says outright whether it was ours;
   the DB shape does not, so it is settled the same way the backend settles it,
   by station id — /api/config already carries ours. */
function isOurs(f) {
  if (typeof f.ours === 'boolean') return f.ours;
  const mine = store.config?.station?.id;
  return mine != null && f.station_id === mine;
}

function who(f) {
  return f.station || f.observer
    || (f.station_id != null ? `#${f.station_id}` : 'unknown station');
}

/* Decoded scalars are the most valuable thing a row can carry, so they are
   shown ahead of the bytes they were decoded from. Four of them is as many as
   fit beside the rest of the row; a spacecraft with a fuller beacon is what the
   Grafana panels above are for. */
function decodedValues(f) {
  if (!f.values || typeof f.values !== 'object') return [];
  return Object.entries(f.values).slice(0, 4);
}

function chip([name, value]) {
  return `<span class="tlm-val">${esc(name)}<b>${esc(scalar(value))}</b></span>`;
}

function scalar(v) {
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  // Two decimals: these are bus volts and panel temperatures, where the third
  // decimal is the decoder's arithmetic rather than a measurement.
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(2);
  return String(v ?? '');
}

/* The hex head is a fingerprint — two frames being different is what it is for.
   When the backend found the frame is genuinely printable it says so in `text`,
   and a readable string beats sixteen bytes of hex for the same width. */
function payload(f) {
  return f.text || f.head || '';
}

function addressing(ax) {
  return `${ax.src || '?'}→${ax.dest || '?'}`;
}

function size(bytes) {
  // null means the frame object itself could not be downloaded, which is not
  // the same as a zero-length frame and must not print as one.
  return Number.isFinite(bytes) ? `${bytes} B` : '—';
}

function explain(t) {
  if (t.detail) return t.detail;
  return TROUBLE[t.status] || 'nothing decoded from this satellite yet';
}

function ageSeconds(iso) {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  // Clamped at zero: a station clock a few seconds ahead of ours should not
  // produce a frame heard in the future.
  return Math.max(0, (Date.now() - then) / 1000);
}

/* Deliberately one significant figure and one unit. Nobody standing at the far
   end of the room is reading "1 h 47 min", and the difference between 2 h and
   2 h 10 min does not change what anyone does next. */
function relative(secs) {
  if (secs < 90) return `${Math.round(secs)} s ago`;
  if (secs < 90 * 60) return `${Math.round(secs / 60)} min ago`;
  if (secs < 48 * 3600) return `${Math.round(secs / 3600)} h ago`;
  return `${Math.round(secs / 86_400)} d ago`;
}

function hms(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  // UTC, like every other time on this display — the header carries the local
  // clock and nothing else has to.
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}Z`;
}

function pad(n) {
  return String(n).padStart(2, '0');
}

function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}
