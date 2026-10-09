/* Station logbook.

   The record of what the station did and why: every command and who sent it,
   leases taken, extended and lapsed, gates opening and closing, autopilot's
   decisions, SatNOGS recordings, plan changes, restarts and faults. It is read
   after the fact — "who armed, from which machine, and what refused it" — so
   it is a page of its own, laid out for reading rather than for a wall.

   History, never permission. Nothing on this page can move anything, and
   nothing in the backend reads the log back to decide anything either.

   History comes from GET /api/events, newest first, paged backwards by id.
   The live tail is this page's own WebSocket to /ws, which carries every
   record as a `log` frame. The same record can arrive more than one way —
   the hub replays the latest `log` frame in every snapshot, and a reconnect
   re-reads from the newest id held — so records are kept by id, once each.

   The fetch helpers are local on purpose: this page shares no state with the
   dashboard, and api.js stays the dashboard's. */

const $ = (id) => document.getElementById(id);

const PAGE = 200;
// The backend's ceiling for one query. A reconnect that finds more than this
// waiting re-reads from the top rather than leaving a hole in the middle.
const CATCH_UP = 1000;
const SEARCH_DEBOUNCE_MS = 250;
// The same timings as the dashboard's link (core/ws.js): the backend sends a
// heartbeat every 10 s, so 25 s of silence is a dead socket, not a quiet one.
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 15000;
const SILENCE_LIMIT_MS = 25000;
// The day list changes once a day; a new day's first record also refreshes it.
const DAYS_REFRESH_MS = 10 * 60_000;

const SEV = { info: 0, warn: 1, bad: 2 };

// Each chip is a query the backend understands, and the same test applied to
// live records, so a filtered view and its live tail can never disagree.
const FILTERS = [
  { id: 'all',       label: 'all' },
  { id: 'control',   label: 'control',   kinds: ['control'],
    title: 'commands and who sent them, leases, gates, tracks ending' },
  { id: 'autopilot', label: 'autopilot', kinds: ['autopilot'] },
  { id: 'faults',    label: 'faults',    minSev: 'warn',
    title: 'everything at warn or worse, whatever its kind' },
  { id: 'satnogs',   label: 'SatNOGS',   kinds: ['satnogs'],
    title: 'the station client, and recordings starting and ending' },
  { id: 'plan',      label: 'plan',      kinds: ['plan'] },
  { id: 'config',    label: 'config',    kinds: ['config_change', 'boot', 'shutdown', 'unclean_restart'],
    title: 'boots, restarts and settings that changed between them' },
  { id: 'audit',     label: 'audit',     kinds: ['audit'],
    title: 'requests that could change what the station does, and from where' },
];

const state = {
  filter: 'all',
  q: '',
  day: '',              // a UTC day file, or '' for the latest records
  records: new Map(),   // id → record, for everything currently shown
  more: false,          // the backend has older matches than the oldest shown
  days: [],
  tz: 'UTC',
  loading: false,
  error: '',
  generation: 0,        // bumped by every reload, so a stale reply is dropped
};

let fmt = null;
let socket = null;
let backoff = RECONNECT_MIN_MS;
let lastFrameAt = 0;
let watchdog = null;
let everConnected = false;
let daysAskedAt = 0;

// --------------------------------------------------------------------------
// fetching
// --------------------------------------------------------------------------

async function getJSON(path, params) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
  }
  const resp = await fetch(url, { headers: { accept: 'application/json' } });
  if (!resp.ok) {
    const body = await resp.text().catch(() => '');
    throw new Error(`${resp.status} ${path}: ${body.slice(0, 160)}`);
  }
  return resp.json();
}

function filterOf(id) {
  return FILTERS.find((f) => f.id === id) || FILTERS[0];
}

/** The day picker's bounds, as the API's since/until. Days are UTC because
    the files are, and a day here is exactly the file "download day" fetches. */
function dayBounds(day) {
  if (!day) return {};
  const start = new Date(`${day}T00:00:00Z`);
  const end = new Date(start.getTime() + 86_400_000);
  return { since: start.toISOString(), until: end.toISOString() };
}

function queryParams(extra = {}) {
  const f = filterOf(state.filter);
  return {
    kinds: f.kinds?.join(','),
    min_sev: f.minSev,
    q: state.q,
    ...dayBounds(state.day),
    ...extra,
  };
}

async function reload() {
  const gen = ++state.generation;
  state.loading = true;
  paintFoot();
  try {
    const page = await getJSON('/api/events', queryParams({ limit: PAGE }));
    if (gen !== state.generation) return;
    const fresh = new Map(page.items.map((r) => [r.id, r]));
    // A record that arrived live while this was in flight may be newer than
    // anything the reply holds. Dropping it would leave a hole at the top
    // that nothing fills until the next reconnect.
    const top = page.items[0] ? idKey(page.items[0].id) : [0, 0];
    for (const r of state.records.values()) {
      const [ms, n] = idKey(r.id);
      if ((ms > top[0] || (ms === top[0] && n > top[1])) && matches(r)) fresh.set(r.id, r);
    }
    state.records = fresh;
    state.more = Boolean(page.more);
    state.error = '';
  } catch (err) {
    if (gen !== state.generation) return;
    state.error = `could not read the log — ${err.message}`;
  } finally {
    if (gen === state.generation) state.loading = false;
  }
  render();
}

async function loadOlder() {
  const oldest = byRecording().at(-1);
  if (!oldest) return;
  const gen = state.generation;
  state.loading = true;
  paintFoot();
  try {
    // `until` is an id here, which is exclusive and exact: a second-resolution
    // time would drop or repeat records that share the boundary second.
    const page = await getJSON('/api/events', queryParams({ until: oldest.id, limit: PAGE }));
    if (gen !== state.generation) return;
    for (const r of page.items) state.records.set(r.id, r);
    state.more = Boolean(page.more);
    state.error = '';
  } catch (err) {
    if (gen !== state.generation) return;
    state.error = `could not read older records — ${err.message}`;
  } finally {
    if (gen === state.generation) state.loading = false;
  }
  render();
}

/** After a reconnect: whatever was recorded while the link was down. The
    socket only carries what happens from now on, and a restarted backend
    has written its boot (and maybe an unclean_restart) before anyone asks. */
async function catchUp() {
  const newest = byRecording()[0];
  if (!newest || state.day) return reload();
  const gen = state.generation;
  try {
    const page = await getJSON('/api/events', queryParams({ since: newest.id, limit: CATCH_UP }));
    if (gen !== state.generation) return;
    if (page.more) return reload();
    for (const r of page.items) state.records.set(r.id, r);
    state.error = '';
  } catch (err) {
    if (gen !== state.generation) return;
    state.error = `could not catch up — ${err.message}`;
  }
  render();
}

async function refreshDays() {
  daysAskedAt = Date.now();
  try {
    state.days = await getJSON('/api/events/days');
  } catch {
    // The list is a convenience; the records still load without it.
    return;
  }
  paintDays();
}

// --------------------------------------------------------------------------
// the live tail
// --------------------------------------------------------------------------

function wsUrl() {
  const u = new URL('/ws', location.href);
  u.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return u.toString();
}

function connect() {
  if (socket && (socket.readyState === WebSocket.OPEN
              || socket.readyState === WebSocket.CONNECTING)) return;
  paintLive(everConnected ? 'reconnecting' : 'connecting');
  socket = new WebSocket(wsUrl());

  socket.onopen = () => {
    backoff = RECONNECT_MIN_MS;
    lastFrameAt = Date.now();
    startWatchdog();
  };
  socket.onmessage = (ev) => {
    lastFrameAt = Date.now();
    let frame;
    try {
      frame = JSON.parse(ev.data);
    } catch {
      return;
    }
    onFrame(frame);
  };
  socket.onclose = () => {
    stopWatchdog();
    paintLive('reconnecting');
    const wait = backoff + Math.random() * backoff * 0.3;
    backoff = Math.min(backoff * 2, RECONNECT_MAX_MS);
    setTimeout(connect, wait);
  };
  socket.onerror = () => socket?.close();
}

function startWatchdog() {
  stopWatchdog();
  watchdog = setInterval(() => {
    if (Date.now() - lastFrameAt > SILENCE_LIMIT_MS) socket?.close();
  }, 5000);
}

function stopWatchdog() {
  clearInterval(watchdog);
  watchdog = null;
}

function onFrame(frame) {
  switch (frame.type) {
    case 'hello':
      // Every hello may be a different process — a deploy, a crash, the
      // restart this page exists to make visible. Re-read from the newest
      // record held rather than trusting the gap to have been empty. The
      // first hello too: anything recorded between the first read and the
      // socket opening is in no reply and, but for the latest, in no frame.
      paintLive('live');
      catchUp();
      if (everConnected) refreshDays();
      everConnected = true;
      break;
    case 'snapshot':
      for (const inner of frame.data?.frames || []) {
        if (inner.type === 'log') addLive(inner.data);
      }
      break;
    case 'log':
      addLive(frame.data);
      break;
    default:
      break;
  }
}

function addLive(record) {
  if (!record?.id || state.records.has(record.id) || !matches(record)) return;
  state.records.set(record.id, record);
  // The first record of a new UTC day means a new file. Asked at most once a
  // minute: with the disk degraded there are no files, and every record would
  // otherwise ask again.
  if (!state.days.some((d) => d.day === record.ts?.slice(0, 10))
      && Date.now() - daysAskedAt > 60_000) refreshDays();
  render();
}

/** The backend's own filter, for records that arrive live. */
function matches(r) {
  const f = filterOf(state.filter);
  if (f.minSev && (SEV[r.sev] ?? 0) < SEV[f.minSev]) return false;
  const kind = String(r.kind || '');
  if (f.kinds && !f.kinds.some((p) => kind === p || kind.startsWith(`${p}.`))) return false;
  const needle = state.q.trim().toLowerCase();
  if (needle && !String(r.text || '').toLowerCase().includes(needle)
      && !kind.toLowerCase().includes(needle)) return false;
  if (state.day && String(r.ts || '').slice(0, 10) !== state.day) return false;
  return true;
}

// --------------------------------------------------------------------------
// rendering
// --------------------------------------------------------------------------

function idKey(id) {
  const [ms, n] = String(id).split('-').map(Number);
  return [ms || 0, n || 0];
}

const byId = (a, b) => {
  const [am, an] = idKey(a.id);
  const [bm, bn] = idKey(b.id);
  return bm - am || bn - an;
};

/** Newest first by id: the order records were made, which is the order the
    backend serves and pages them in. */
function byRecording() {
  return [...state.records.values()].sort(byId);
}

/** Newest first by when it happened, for reading. A recording SatNOGS started
    is dated by its own window, a minute or two before the poll that noticed
    it; listed by id it would sit above lines whose times are later than its
    own, and a list whose times run out of order reads as a broken clock. */
function sorted() {
  return [...state.records.values()].sort((a, b) => {
    const at = Date.parse(a.ts) || 0;
    const bt = Date.parse(b.ts) || 0;
    return bt - at || byId(a, b);
  });
}

function makeFormatters(tz) {
  const build = (timeZone) => ({
    day: new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' }),
    head: new Intl.DateTimeFormat('en-GB', { timeZone, weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' }),
    time: new Intl.DateTimeFormat('en-GB', { timeZone, hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }),
  });
  try {
    return build(tz);
  } catch {
    // A timezone the browser does not know: UTC is wrong in a known way,
    // which beats an empty page.
    state.tz = 'UTC';
    return build('UTC');
  }
}

function escape(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function render() {
  const host = $('lb-list');
  const rows = sorted();
  const err = $('lb-error');
  err.hidden = !state.error;
  err.textContent = state.error;

  if (!rows.length) {
    host.innerHTML = state.loading ? '' : `<p class="lb-empty">${emptyText()}</p>`;
    paintFoot();
    paintNote(0);
    return;
  }

  // Grouped by the station's own day, which is how anyone on site talks about
  // "last night's pass". The day files are UTC; the headings say whose day.
  const groups = [];
  for (const r of rows) {
    const when = new Date(r.ts);
    const key = Number.isNaN(when.getTime()) ? '?' : fmt.day.format(when);
    let group = groups.at(-1);
    if (!group || group.key !== key) {
      group = { key, when, items: [] };
      groups.push(group);
    }
    group.items.push(r);
  }

  // A live record re-renders the list. Whatever the reader had open stays
  // open, and whatever they were reading stays where it was on screen,
  // rather than being pushed down by every new line at the top.
  const open = new Set([...host.querySelectorAll('details[open]')]
    .map((el) => el.closest('li')?.dataset.id));
  const anchor = readingAnchor(host);
  host.innerHTML = groups.map((g) => `
    <section class="lb-day">
      <h2><span>${escape(g.key === '?' ? 'undated' : fmt.head.format(g.when))}</span>
        <span class="lb-count">${g.items.length}</span></h2>
      <ol>${g.items.map(entry).join('')}</ol>
    </section>`).join('');
  for (const li of host.querySelectorAll('li.ev')) {
    if (open.has(li.dataset.id)) li.querySelector('details')?.setAttribute('open', '');
  }
  if (anchor) {
    const li = host.querySelector(`li[data-id="${CSS.escape(anchor.id)}"]`);
    if (li) window.scrollBy(0, li.getBoundingClientRect().top - anchor.top);
  }
  paintFoot();
  paintNote(rows.length);
}

/** The first entry on screen, and where — unless the reader is at the top,
    where new records are exactly what they are waiting for. */
function readingAnchor(host) {
  if (window.scrollY < 40) return null;
  for (const li of host.querySelectorAll('li.ev')) {
    const top = li.getBoundingClientRect().top;
    if (top >= 0) return { id: li.dataset.id, top };
  }
  return null;
}

function emptyText() {
  if (state.error) return 'nothing to show';
  const f = filterOf(state.filter);
  const what = state.q ? `matching “${escape(state.q)}”` : (f.id === 'all' ? '' : `under ${escape(f.label)}`);
  const when = state.day ? `on ${escape(state.day)} (UTC)` : 'yet';
  return `Nothing recorded ${what} ${when}.`.replace(/\s+/g, ' ');
}

/** One record. A record with data is a <details> whose summary is the row
    itself, so the data is a click (or Enter) away without a "details" line
    under every entry doubling the length of the page. */
function entry(r) {
  const when = new Date(r.ts);
  const valid = !Number.isNaN(when.getTime());
  const time = valid ? fmt.time.format(when) : '--:--:--';
  const utc = valid ? `${r.ts.slice(0, 19).replace('T', ' ')} UTC` : '';
  const sev = SEV[r.sev] !== undefined ? r.sev : 'info';
  const family = String(r.kind || '').split('.')[0];
  const row = `
      <time datetime="${escape(r.ts)}" title="${escape(utc)}">${time}</time>
      <span class="ev-kind">${escape(r.kind)}</span>
      <span class="ev-text">${escape(r.text)}</span>
      ${meta(r)}`;
  const data = r.data || {};
  const body = Object.keys(data).length
    ? `<details><summary class="ev-row">${row}<i class="ev-more" aria-hidden="true"></i></summary>
         <pre>${escape(JSON.stringify(data, null, 1))}</pre></details>`
    : `<div class="ev-row">${row}</div>`;
  return `<li class="ev sev-${sev} k-${escape(family)}" data-id="${escape(r.id)}">${body}</li>`;
}

/** The second line a few kinds earn, from their data: who and what an audit
    line was, and the times a sentence names — a lease's expiry, the last
    sign of life before an unclean restart — in station time. The backend's
    sentence says them in UTC, which is right for a file read without this
    page and is not what anyone on site has on their wrist. */
function meta(r) {
  const d = r.data || {};
  let text = '';
  if (r.kind === 'audit') {
    const parts = [d.ua];
    const fields = Object.entries(d.fields || {}).map(([k, v]) => `${k}=${v}`);
    if (fields.length) parts.push(fields.join(' '));
    if (d.peer) parts.push(`via ${d.peer}`);
    text = parts.filter(Boolean).join(' · ');
  } else if (r.kind === 'control.lease' && d.expires_at) {
    text = stationTime('until', d.expires_at);
  } else if (r.kind === 'unclean_restart' && d.last_seen) {
    text = stationTime('last record', d.last_seen);
  } else if (d.happened_at) {
    // Noticed over an hour after it happened — a recording's end seen once
    // SatNOGS answered again — so the row is dated by the noticing. Say
    // when it was, with the day, since an outage can span one.
    const when = new Date(d.happened_at);
    if (!Number.isNaN(when.getTime())) {
      const zone = state.tz === 'UTC' ? 'UTC' : 'station time';
      text = `happened ${fmt.day.format(when)} ${fmt.time.format(when)} ${zone}, noticed late`;
    }
  }
  return text ? `<span class="ev-meta">${escape(text)}</span>` : '';
}

function stationTime(label, iso) {
  const when = new Date(iso);
  if (state.tz === 'UTC' || Number.isNaN(when.getTime())) return '';
  return `${label} ${fmt.time.format(when)} station time`;
}

function paintFoot() {
  const older = $('lb-older');
  const end = $('lb-end');
  older.hidden = !state.more || !state.records.size;
  older.disabled = state.loading;
  older.textContent = state.loading ? 'loading…' : 'load older';
  if (state.loading && !state.records.size) end.textContent = 'loading…';
  else if (!state.more && state.records.size) {
    end.textContent = state.day ? 'start of this day' : 'start of the log';
  } else end.textContent = '';
}

function paintNote(count) {
  const zone = state.tz === 'UTC' ? 'UTC' : `${state.tz} time`;
  const scope = state.day ? `${state.day} (UTC day)` : 'latest first';
  $('lb-note').textContent =
    `${count} shown${state.more ? ', more below' : ''} · ${scope} · times in ${zone}`;
}

function paintLive(which) {
  const el = $('lb-live');
  el.dataset.state = which;
  $('lb-live-text').textContent = which;
}

function paintChips() {
  $('lb-chips').innerHTML = FILTERS.map((f) => `
    <button type="button" class="lb-chip" data-filter="${f.id}"
            aria-pressed="${f.id === state.filter}"
            ${f.title ? `title="${escape(f.title)}"` : ''}>${escape(f.label)}</button>`).join('');
}

function paintDays() {
  const select = $('lb-day');
  const options = ['<option value="">latest</option>'];
  const known = new Set();
  for (const d of state.days) {
    known.add(d.day);
    options.push(`<option value="${escape(d.day)}">${escape(d.day)} UTC · ${size(d.bytes)}</option>`);
  }
  // A day from the URL that has no file (retention took it) still shows as
  // chosen, so the empty page explains itself.
  if (state.day && !known.has(state.day)) {
    options.push(`<option value="${escape(state.day)}">${escape(state.day)} UTC · none</option>`);
  }
  select.innerHTML = options.join('');
  select.value = state.day;
  paintDownload();
}

function paintDownload() {
  const link = $('lb-dl');
  const day = state.day || state.days[0]?.day || '';
  link.hidden = !day || !state.days.some((d) => d.day === day);
  if (link.hidden) return;
  link.href = `/api/events/day/${encodeURIComponent(day)}.jsonl`;
  link.setAttribute('download', `station-log-${day}.jsonl`);
  link.textContent = state.day ? 'download day' : `download ${day}`;
  link.title = `the ${day} file (a UTC day), exactly as written: one JSON record per line`;
}

function size(bytes) {
  if (!Number.isFinite(bytes)) return '?';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// --------------------------------------------------------------------------
// the URL: a view worth sending to someone, e.g. logbook.html#faults
// --------------------------------------------------------------------------

function readHash() {
  const raw = location.hash.replace(/^#/, '');
  const params = new URLSearchParams(raw.includes('=') ? raw : `filter=${raw}`);
  const filter = params.get('filter');
  state.filter = FILTERS.some((f) => f.id === filter) ? filter : 'all';
  state.q = params.get('q') || '';
  const day = params.get('day') || '';
  state.day = /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : '';
}

function writeHash() {
  const params = new URLSearchParams();
  if (state.filter !== 'all') params.set('filter', state.filter);
  if (state.q) params.set('q', state.q);
  if (state.day) params.set('day', state.day);
  const next = params.toString();
  // A lone filter reads best as the plain word: #faults, not #filter=faults.
  const simple = state.filter !== 'all' && !state.q && !state.day ? state.filter : next;
  history.replaceState(null, '', simple ? `#${simple}` : location.pathname + location.search);
}

// --------------------------------------------------------------------------
// boot
// --------------------------------------------------------------------------

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

async function boot() {
  readHash();
  paintChips();
  $('lb-q').value = state.q;

  try {
    const cfg = await getJSON('/api/config');
    state.tz = cfg.station?.timezone || 'UTC';
    const st = cfg.station || {};
    $('lb-station').textContent =
      [`SatNOGS ${st.id ?? ''}`.trim(), st.grid].filter(Boolean).join(' · ');
  } catch {
    // Without config the log still reads; it is only in UTC.
    state.tz = 'UTC';
  }
  fmt = makeFormatters(state.tz);

  $('lb-chips').addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-filter]');
    if (!btn || btn.dataset.filter === state.filter) return;
    state.filter = btn.dataset.filter;
    paintChips();
    writeHash();
    reload();
  });
  $('lb-q').addEventListener('input', debounce(() => {
    const q = $('lb-q').value.trim();
    if (q === state.q) return;
    state.q = q;
    writeHash();
    reload();
  }, SEARCH_DEBOUNCE_MS));
  $('lb-day').addEventListener('change', () => {
    state.day = $('lb-day').value;
    writeHash();
    paintDownload();
    reload();
  });
  $('lb-older').addEventListener('click', loadOlder);
  window.addEventListener('hashchange', () => {
    readHash();
    paintChips();
    paintDays();
    $('lb-q').value = state.q;
    reload();
  });

  paintDays();
  await Promise.all([reload(), refreshDays()]);
  connect();
  setInterval(refreshDays, DAYS_REFRESH_MS);
}

boot();
