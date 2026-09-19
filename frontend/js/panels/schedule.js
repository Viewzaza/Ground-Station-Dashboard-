/* Station Schedule: the autoscheduler's last run for this station, and an
   editable priority order that feeds its next one.

   Shown as an overlay rather than a grid tile (see schedule.css for why),
   opened from a chip in the header. */

import { api } from '../core/api.js';
import { shortTime } from '../core/format.js';

const POLL_MS = 3000;
const POLL_TIMEOUT_MS = 120_000;
// Network Campaign walks every candidate station's booking history one at a
// time to stay polite to SatNOGS's rate limit - against real data (hundreds
// of stations) that has taken several minutes in testing, not the seconds a
// Station Schedule run or mock data returns in. The 120s timeout above would
// give up on a still-healthy real run and misreport it as unresponsive.
const CAMPAIGN_POLL_TIMEOUT_MS = 20 * 60_000;

let priorities = [];   // [{norad_cat_id, weight, transmitter_uuid, mode}], current display order
let dragFrom = -1;
let pendingAdd = null;  // {norad, name} once a search suggestion is picked
let searchDebounce;
let openTxNorad = null;          // NORAD of the row whose transmitter picker is open
const txCache = new Map();       // norad -> transmitters[] already fetched this session

let priorityLists = [];          // [{slug, name}], as last fetched from the backend
let activeListSlug = 'default';
let listPickerOpen = false;
let settingsOpen = false;

let networkTokenSet = false;
let campaignPreviewItems = null;   // the exact items last previewed, so CONFIRM submits what was shown
let campaignConfigDirty = false;   // invalidates a stale preview if config changes after it

export function mountSchedule() {
  document.getElementById('schedule-toggle').addEventListener('click', open);
  document.getElementById('schedule-close').addEventListener('click', close);
  document.getElementById('schedule-run').addEventListener('click', runNow);
  document.getElementById('schedule-save').addEventListener('click', save);

  const search = document.getElementById('schedule-add-search');
  search.addEventListener('input', onSearchInput);
  search.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); addEntry(); }
    if (ev.key === 'Escape') hideSuggestions();
  });
  // A delay, not an immediate hide: a suggestion's own mousedown must land
  // before blur clears the list, or a click on it would never register.
  search.addEventListener('blur', () => setTimeout(hideSuggestions, 150));

  mountSettings();
  mountPriorityLists();
  mountModeTabs();
  mountCampaign();

  // The transmitter picker, the settings popover and the list picker each
  // have no input to blur — all three are dismissed by clicking anywhere
  // outside them, or Escape. One shared pair of listeners for all three.
  document.addEventListener('click', (ev) => {
    if (openTxNorad !== null && !ev.target.closest('.sched-prio-tx-wrap')) {
      closeTxPicker();
    }
    if (settingsOpen && !ev.target.closest('.sched-settings-wrap')) {
      closeSettings();
    }
    if (listPickerOpen && !ev.target.closest('.sched-list-picker-wrap')) {
      closeListPicker();
    }
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Escape') return;
    if (openTxNorad !== null) closeTxPicker();
    if (settingsOpen) closeSettings();
    if (listPickerOpen) closeListPicker();
  });
}

const panel = () => document.getElementById('schedule-panel');

async function open() {
  panel().hidden = false;
  await Promise.all([
    loadLastRun(), loadPriorities(), loadConfig(), loadPriorityLists(),
    loadCampaignLastRun(), loadCampaignHistory(),
  ]);
}

function close() {
  panel().hidden = true;
  openTxNorad = null;
  settingsOpen = false;
  listPickerOpen = false;
}

async function loadLastRun() {
  try {
    renderLastRun(await api.scheduleLastRun());
  } catch (err) {
    console.error('[schedule] last run', err);
    document.getElementById('schedule-last-run').replaceChildren(note(`could not load: ${err}`));
  }
}

function renderLastRun(run) {
  const box = document.getElementById('schedule-last-run');
  box.replaceChildren();

  if (!run || run.status === 'never_run') {
    box.appendChild(note('no auto schedule has run yet'));
    return;
  }
  if (run.status === 'running') {
    box.appendChild(note('a run is in progress…'));
    return;
  }
  if (run.status === 'error') {
    box.appendChild(note(`last run failed: ${run.error}`));
    return;
  }

  const meta = document.createElement('p');
  meta.className = 'muted sched-meta';
  const text = document.createElement('span');
  text.textContent = `Generated ${shortTime(run.generated_utc, 'UTC')} UTC · `
    + `${run.observations.length} of ${run.considered} candidate pass(es) booked`;
  meta.appendChild(text);

  const notices = run.notices || [];
  if (notices.length) {
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'sched-notices-toggle';
    toggle.textContent = `${notices.length} warning${notices.length === 1 ? '' : 's'} ▾`;
    const list = document.createElement('ul');
    list.className = 'sched-notices';
    list.id = nextDomId('sched-notices');
    list.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-controls', list.id);
    for (const n of notices) {
      const li = document.createElement('li');
      li.className = `sched-notice ${n.severity === 'error' ? 'error' : ''}`.trim();
      li.textContent = n.message;
      list.appendChild(li);
    }
    toggle.addEventListener('click', () => {
      const showing = list.hidden;
      list.hidden = !showing;
      toggle.setAttribute('aria-expanded', String(showing));
      toggle.textContent = `${notices.length} warning${notices.length === 1 ? '' : 's'} ${showing ? '▴' : '▾'}`;
    });
    meta.appendChild(toggle);
    box.appendChild(meta);
    box.appendChild(list);
  } else {
    const ok = document.createElement('span');
    ok.className = 'sched-status-ok';
    ok.textContent = 'OK';
    meta.appendChild(ok);
    box.appendChild(meta);
  }

  if (!run.observations.length) return;

  const table = document.createElement('table');
  table.className = 'sched-table';
  const thead = document.createElement('thead');
  thead.innerHTML = '<tr><th>Start UTC</th><th>Min</th><th>El</th><th>NORAD</th>'
    + '<th>Satellite</th><th>MHz</th><th>Mode</th><th>Seen</th><th>Score</th></tr>';
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  for (const o of run.observations) {
    const tr = document.createElement('tr');
    if (o.is_mission) tr.classList.add('mission');
    for (const text of [
      shortTime(o.start, 'UTC'),
      String(Math.round(o.duration_s / 60)),
      `${o.max_elevation_deg.toFixed(0)}°`,
      String(o.norad_cat_id),
      (o.is_mission ? '★ ' : '') + o.satellite,
      (o.downlink_hz / 1e6).toFixed(3),
      o.mode || '-',
      String(o.observed_here),
      o.score.toFixed(2),
    ]) {
      const td = document.createElement('td');
      td.textContent = text;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  box.appendChild(scrollableTable(
    table, `Booked observations, ${run.observations.length} row(s)`));
}

/* Both tabs' tables get the same treatment now: a bounded, keyboard-reachable
   scroll region with a sticky header, rather than one tab scrolling its table
   and the other growing the modal until rows fall off the bottom. */
function scrollableTable(table, label) {
  const wrap = document.createElement('div');
  wrap.className = 'sched-table-wrap';
  // Focusable so the region can be scrolled from the keyboard — a div with
  // overflow is not in the tab order by default in every browser, and these
  // lists routinely run past their 280px window.
  wrap.tabIndex = 0;
  wrap.setAttribute('role', 'group');
  wrap.setAttribute('aria-label', label);
  wrap.appendChild(table);
  return wrap;
}

let domIdSeq = 0;
function nextDomId(prefix) {
  domIdSeq += 1;
  return `${prefix}-${domIdSeq}`;
}

function note(text) {
  const p = document.createElement('p');
  p.className = 'muted';
  p.textContent = text;
  return p;
}

/* A note that has to be noticed: "nothing was submitted", "this timed out",
   "that failed". These were plain grey text before, indistinguishable from
   the idle "no preview yet" copy right next to them — on this tab the
   difference between "nothing happened" and "something went wrong" is the
   whole message. Reuses .sched-notice, the panel's existing warning band. */
function alertNote(text, severity = 'warn') {
  const p = document.createElement('p');
  p.className = `sched-notice sched-inline-notice${severity === 'error' ? ' error' : ''}`;
  p.textContent = text;
  return p;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function runNow() {
  const btn = document.getElementById('schedule-run');
  btn.disabled = true;
  btn.textContent = 'RUNNING…';
  try {
    const before = (await api.scheduleLastRun())?.generated_utc;
    await api.runSchedule();
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    while (Date.now() < deadline) {
      await sleep(POLL_MS);
      const run = await api.scheduleLastRun();
      if (run?.status === 'error' || (run?.generated_utc && run.generated_utc !== before)) {
        renderLastRun(run);
        break;
      }
    }
  } catch (err) {
    console.error('[schedule] run', err);
  } finally {
    btn.disabled = false;
    btn.textContent = 'RUN NOW';
  }
}

async function loadPriorities() {
  openTxNorad = null;
  try {
    const resp = await api.getPriorities();
    priorities = resp.entries || [];
  } catch (err) {
    console.error('[schedule] priorities', err);
    priorities = [];
  }
  // Unflagged rows (anything saved before this feature existed) default to
  // "auto" — preserves today's exact drag-reorder behavior for anyone who
  // has not touched this yet.
  for (const p of priorities) p.mode = p.mode === 'manual' ? 'manual' : 'auto';
  renderPriorities();
}

function renderPriorities() {
  const ul = document.getElementById('schedule-priorities');
  ul.replaceChildren();
  if (!priorities.length) {
    ul.appendChild(note('no priority file yet'));
    return;
  }

  priorities.forEach((p, i) => {
    const li = document.createElement('li');
    li.className = 'sched-prio-row';
    // Not draggable while its own transmitter picker is open — a dragstart
    // fired from inside the open popover would otherwise drag the row instead
    // of letting the click land on an option.
    li.draggable = openTxNorad !== p.norad_cat_id;

    const handle = document.createElement('span');
    handle.className = 'sched-drag';
    handle.textContent = '⠿';

    const info = document.createElement('span');
    info.className = 'sched-prio-info';

    const name = document.createElement('span');
    name.className = 'sched-prio-name';
    if (p.satellite) {
      name.textContent = p.satellite;
    } else {
      name.textContent = 'unresolved — save to look up';
      name.classList.add('unknown');
    }
    const norad = document.createElement('span');
    norad.className = 'sched-prio-norad';
    norad.textContent = `NORAD ${p.norad_cat_id}`;
    info.append(name, norad, buildTxControl(p));

    const mode = document.createElement('button');
    mode.type = 'button';
    mode.className = 'sched-prio-mode';
    const isManual = p.mode === 'manual';
    if (isManual) mode.classList.add('is-manual');
    mode.textContent = isManual ? 'MANUAL' : 'AUTO';
    mode.title = isManual
      ? 'weight is pinned — reordering other rows will not change it'
      : 'weight follows list position — dragging any row recomputes it';
    mode.addEventListener('click', () => {
      priorities[i].mode = isManual ? 'auto' : 'manual';
      if (isManual) {
        // Switching back to auto: don't leave the weight stale until the
        // next drag — recompute right away.
        rerank();
      }
      renderPriorities();
    });

    const weight = document.createElement('input');
    weight.type = 'number';
    weight.min = '0';
    weight.max = '1';
    weight.step = '0.05';
    weight.value = p.weight.toFixed(2);
    weight.addEventListener('input', () => {
      priorities[i].weight = clamp01(parseFloat(weight.value) || 0);
    });

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'sched-prio-del';
    del.title = `remove NORAD ${p.norad_cat_id}`;
    del.textContent = '×';
    del.addEventListener('click', () => {
      priorities.splice(i, 1);
      renderPriorities();
    });

    li.append(handle, info, mode, weight, del);

    li.addEventListener('dragstart', (ev) => {
      dragFrom = i;
      ev.dataTransfer.effectAllowed = 'move';
      ev.dataTransfer.setData('text/plain', String(i));
      // Deferred: the browser snapshots the drag ghost synchronously at
      // dragstart, before any class added in the same tick can affect it —
      // add the faded look one tick later so the ghost still shows the row
      // at full opacity while the row left behind fades.
      setTimeout(() => li.classList.add('dragging'), 0);
    });
    li.addEventListener('dragend', () => {
      li.classList.remove('dragging');
      ul.querySelectorAll('.drag-over').forEach((el) => el.classList.remove('drag-over'));
      dragFrom = -1;
    });
    li.addEventListener('dragover', (ev) => {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'move';
      if (i !== dragFrom) li.classList.add('drag-over');
    });
    li.addEventListener('dragleave', () => li.classList.remove('drag-over'));
    li.addEventListener('drop', (ev) => {
      ev.preventDefault();
      li.classList.remove('drag-over');
      if (dragFrom < 0 || dragFrom === i) return;
      const [moved] = priorities.splice(dragFrom, 1);
      priorities.splice(i, 0, moved);
      dragFrom = -1;
      rerank();
      renderPriorities();
    });
    ul.appendChild(li);
  });
}

function buildTxControl(p) {
  const wrap = document.createElement('span');
  wrap.className = 'sched-prio-tx-wrap';

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'sched-prio-tx';
  if (p.transmitter_uuid) btn.classList.add('is-pinned');
  btn.appendChild(document.createTextNode(
    p.transmitter_desc || (p.transmitter_uuid ? p.transmitter_uuid : 'auto (best available)'),
  ));
  // A pin's status can drift after it was saved (the picker only ever offers
  // "active" candidates, but nothing re-checks a pin once it is stored) - and
  // pick_transmitter() silently drops a pin that is not active at plan time,
  // with no other visible sign it happened. Surface it here, in the same
  // .rx-tag vocabulary the radio panel uses for exactly this state.
  if (p.transmitter_status && p.transmitter_status !== 'active') {
    const tag = document.createElement('span');
    tag.className = 'rx-tag dead';
    tag.textContent = p.transmitter_status.toUpperCase();
    btn.appendChild(tag);
  }
  btn.addEventListener('click', () => {
    openTxNorad = openTxNorad === p.norad_cat_id ? null : p.norad_cat_id;
    renderPriorities();
  });
  wrap.appendChild(btn);

  if (openTxNorad !== p.norad_cat_id) {
    // Spelled out rather than left absent: a toggle that only ever asserts
    // "expanded" reads to a screen reader as stuck open once it has been used.
    btn.setAttribute('aria-expanded', 'false');
  } else {
    btn.classList.add('is-editing');
    btn.setAttribute('aria-expanded', 'true');
    const picker = document.createElement('ul');
    picker.className = 'sched-tx-picker';
    picker.appendChild(loadingItem());
    wrap.appendChild(picker);
    loadTxOptions(picker, p);
  }

  return wrap;
}

function closeTxPicker() {
  openTxNorad = null;
  renderPriorities();
}

function loadingItem() {
  const li = document.createElement('li');
  li.className = 'sched-tx-loading';
  li.textContent = 'loading transmitters…';
  return li;
}

async function loadTxOptions(picker, p) {
  let list = txCache.get(p.norad_cat_id);
  if (!list) {
    try {
      const resp = await api.transmittersFor(p.norad_cat_id);
      list = resp.transmitters || [];
      txCache.set(p.norad_cat_id, list);
    } catch (err) {
      console.error('[schedule] transmitters', err);
      // The picker for this row may already be closed, or replaced by a
      // fresh render, by the time this resolves — only paint into it if it
      // is still the one the operator is looking at.
      if (openTxNorad === p.norad_cat_id) {
        picker.replaceChildren(emptyItem(`could not load: ${err}`));
      }
      return;
    }
  }
  if (openTxNorad === p.norad_cat_id) {
    renderTxOptions(picker, list, p);
  }
}

function emptyItem(text) {
  const li = document.createElement('li');
  li.className = 'sched-tx-empty';
  li.textContent = text;
  return li;
}

function renderTxOptions(picker, list, p) {
  picker.replaceChildren();

  const auto = document.createElement('li');
  auto.className = 'sched-tx-opt sched-tx-auto';
  if (!p.transmitter_uuid) auto.classList.add('is-selected');
  auto.textContent = 'Auto — best available';
  auto.addEventListener('click', () => pickTransmitter(p.norad_cat_id, null, ''));
  picker.appendChild(auto);

  if (!list.length) {
    picker.appendChild(emptyItem('no transmitters found for this satellite'));
    return;
  }

  for (const tx of list) {
    const li = document.createElement('li');
    li.className = 'sched-tx-opt';
    if (tx.uuid === p.transmitter_uuid) li.classList.add('is-selected');

    const mhz = (tx.downlink_hz / 1e6).toFixed(3);
    const freq = document.createElement('span');
    freq.className = 'rx-freq';
    freq.textContent = mhz;
    const unit = document.createElement('i');
    unit.textContent = 'MHz';
    freq.appendChild(unit);

    const mode = document.createElement('span');
    mode.className = 'rx-mode';
    mode.textContent = tx.mode || '—';

    li.append(freq, mode);
    const desc = `${mhz} MHz ${tx.mode || ''}`.trim();
    // The picker only ever lists transmitters transmitters_for_station()
    // already filtered to status "active", so this pick is known-active right
    // now — worth recording immediately rather than leaving the row's status
    // unknown until the next reload.
    li.addEventListener('click', () => pickTransmitter(p.norad_cat_id, tx.uuid, desc, 'active'));
    picker.appendChild(li);
  }
}

function pickTransmitter(norad, uuid, desc, status = null) {
  // Looked up by NORAD, not a captured array index: the priorities array can
  // be reordered or have a different row deleted while this picker's fetch
  // was in flight, which would leave a stale index pointing at the wrong
  // satellite.
  const entry = priorities.find((p) => p.norad_cat_id === norad);
  if (!entry) return;   // the row itself was removed while its picker was open
  entry.transmitter_uuid = uuid;
  entry.transmitter_desc = desc;
  entry.transmitter_status = status;
  openTxNorad = null;
  renderPriorities();
}

function suggestBox() {
  return document.getElementById('schedule-add-suggest');
}

function hideSuggestions() {
  suggestBox().hidden = true;
}

function onSearchInput() {
  pendingAdd = null;
  const q = document.getElementById('schedule-add-search').value.trim();
  clearTimeout(searchDebounce);
  if (!q) { hideSuggestions(); return; }
  searchDebounce = setTimeout(async () => {
    try {
      const resp = await api.satellites(q);
      renderSuggestions((resp.items || []).slice(0, 8));
    } catch (err) {
      console.error('[schedule] satellite search', err);
    }
  }, 150);
}

function renderSuggestions(items) {
  const box = suggestBox();
  box.replaceChildren();
  if (!items.length) {
    hideSuggestions();
    return;
  }
  for (const it of items) {
    const li = document.createElement('li');
    li.textContent = `${it.name} — NORAD ${it.norad}`;
    // mousedown, not click: it has to fire before the search input's own
    // blur handler hides this list, or the click would land on nothing.
    li.addEventListener('mousedown', (ev) => {
      ev.preventDefault();
      pendingAdd = { norad: it.norad, name: it.name };
      addEntry();
    });
    box.appendChild(li);
  }
  box.hidden = false;
}

const DEFAULT_ADD_WEIGHT = 0.5;

function addEntry() {
  const search = document.getElementById('schedule-add-search');

  let norad;
  let name = '';
  if (pendingAdd) {
    ({ norad, name } = pendingAdd);
  } else {
    // No suggestion picked — take the box literally, as a NORAD id. This
    // still works for a satellite the name search does not cover.
    norad = parseInt(search.value, 10);
  }
  if (!Number.isInteger(norad) || norad <= 0) {
    search.focus();
    return;
  }
  if (priorities.some((p) => p.norad_cat_id === norad)) {
    // Already listed — edit its weight in place rather than duplicating it.
    search.focus();
    search.select();
    return;
  }

  priorities.push({
    norad_cat_id: norad,
    // No weight box on the add bar — a new entry starts neutral and the
    // operator adjusts it in the row itself, same as any existing entry.
    weight: DEFAULT_ADD_WEIGHT,
    transmitter_uuid: null,
    mode: 'auto',
    satellite: name,
    transmitter_desc: '',
    transmitter_status: null,
  });
  search.value = '';
  pendingAdd = null;
  hideSuggestions();
  search.focus();
  renderPriorities();
}

function rerank() {
  // A drag reorders the DOM, but the scheduler only ever reads weight, never
  // file line order — so the reorder has to move the number too, or the drag
  // would look interactive while doing nothing. Rows pinned to "Manual" are
  // skipped entirely, and the spacing is spread across only the remaining
  // "Auto" rows — by their rank among themselves, not raw list index — so a
  // manual row sitting in the middle of the list doesn't leave a gap in the
  // auto rows' weights.
  const autoRows = priorities.filter((p) => p.mode !== 'manual');
  const n = autoRows.length;
  autoRows.forEach((p, i) => {
    p.weight = clamp01(n <= 1 ? 1 : 1 - i / (n - 1));
  });
}

function clamp01(v) {
  return Math.max(0, Math.min(1, v));
}

async function save() {
  const btn = document.getElementById('schedule-save');
  const status = document.getElementById('schedule-save-status');
  btn.disabled = true;
  status.textContent = 'saving…';
  try {
    const resp = await api.savePriorities(priorities);
    priorities = resp.entries || priorities;
    renderPriorities();
    status.textContent = 'saved';
  } catch (err) {
    status.textContent = `failed: ${err}`;
  } finally {
    btn.disabled = false;
    setTimeout(() => { status.textContent = ''; }, 3000);
  }
}

// --- station id / token settings ---------------------------------------------

function mountSettings() {
  document.getElementById('schedule-settings-toggle').addEventListener('click', toggleSettings);
  document.getElementById('schedule-cfg-verify').addEventListener('click', verifyStation);
  document.getElementById('schedule-cfg-save').addEventListener('click', saveConfig);
}

function toggleSettings() {
  settingsOpen = !settingsOpen;
  document.getElementById('schedule-settings').hidden = !settingsOpen;
  document.getElementById('schedule-settings-toggle')
    .setAttribute('aria-expanded', String(settingsOpen));
  if (settingsOpen) loadConfig();
}

function closeSettings() {
  settingsOpen = false;
  document.getElementById('schedule-settings').hidden = true;
  document.getElementById('schedule-settings-toggle').setAttribute('aria-expanded', 'false');
}

async function loadConfig() {
  try {
    const cfg = await api.scheduleConfig();
    document.getElementById('schedule-cfg-station').value = cfg.station_id || '';
    document.getElementById('schedule-cfg-token').placeholder =
      cfg.db_token_set ? 'saved (hidden) — leave blank to keep' : 'unchanged';
    document.getElementById('schedule-cfg-network-token').placeholder =
      cfg.network_token_set ? 'saved (hidden) — leave blank to keep' : 'unchanged';
    const maxTotalInput = document.getElementById('campaign-max-total');
    maxTotalInput.value = cfg.campaign_max_total || 150;
    document.getElementById('campaign-max-total-value').textContent = maxTotalInput.value;
    networkTokenSet = !!cfg.network_token_set;
    updateCampaignGate();
  } catch (err) {
    console.error('[schedule] config', err);
  }
}

async function verifyStation() {
  const input = document.getElementById('schedule-cfg-station');
  const result = document.getElementById('schedule-cfg-verify-result');
  const stationId = parseInt(input.value, 10);
  if (!Number.isInteger(stationId) || stationId <= 0) {
    result.textContent = 'enter a station id first';
    return;
  }
  result.textContent = 'checking…';
  try {
    const resp = await api.verifyStation(stationId);
    result.textContent = resp.ok
      ? `✓ ${resp.station_name || 'found'} (${resp.status || 'unknown'})`
      : `✗ ${resp.error}`;
  } catch (err) {
    result.textContent = `✗ ${err}`;
  }
}

async function saveConfig() {
  const btn = document.getElementById('schedule-cfg-save');
  const status = document.getElementById('schedule-cfg-save-status');
  const stationInput = document.getElementById('schedule-cfg-station');
  const tokenInput = document.getElementById('schedule-cfg-token');
  const networkTokenInput = document.getElementById('schedule-cfg-network-token');

  const stationVal = stationInput.value.trim();
  // Blank clears the override back to the dashboard's own default (0 is
  // falsy but not None, so the backend reads it as "clear this").
  const stationId = stationVal ? parseInt(stationVal, 10) : 0;
  // Blank token means "leave whatever is already saved alone" — the field
  // never redisplays a saved secret, so blank cannot mean "clear it".
  const tokenVal = tokenInput.value ? tokenInput.value : undefined;
  const networkTokenVal = networkTokenInput.value ? networkTokenInput.value : undefined;

  btn.disabled = true;
  status.textContent = 'saving…';
  try {
    await api.saveScheduleConfig({
      station_id: stationId, db_token: tokenVal, network_token: networkTokenVal,
    });
    tokenInput.value = '';
    networkTokenInput.value = '';
    await loadConfig();
    status.textContent = 'saved';
  } catch (err) {
    status.textContent = `failed: ${err}`;
  } finally {
    btn.disabled = false;
    setTimeout(() => { status.textContent = ''; }, 3000);
  }
}

// --- named priority lists ------------------------------------------------------

function mountPriorityLists() {
  document.getElementById('schedule-list-current').addEventListener('click', toggleListPicker);
  document.getElementById('schedule-list-rename').addEventListener('click', startRename);

  const renameInput = document.getElementById('schedule-list-rename-input');
  renameInput.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); commitRename(); }
    if (ev.key === 'Escape') cancelRename();
  });
  renameInput.addEventListener('blur', commitRename);

  document.getElementById('schedule-saveas').addEventListener('click', toggleSaveAs);
  const saveAsInput = document.getElementById('schedule-saveas-input');
  saveAsInput.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); confirmSaveAs(); }
    if (ev.key === 'Escape') cancelSaveAs();
  });
}

async function loadPriorityLists() {
  try {
    const resp = await api.priorityLists();
    priorityLists = resp.lists || [];
    activeListSlug = resp.active || activeListSlug;
  } catch (err) {
    console.error('[schedule] priority lists', err);
  }
  renderListName();
}

function renderListName() {
  const entry = priorityLists.find((l) => l.slug === activeListSlug);
  document.getElementById('schedule-list-name').textContent = entry ? entry.name : activeListSlug;
}

function toggleListPicker() {
  listPickerOpen = !listPickerOpen;
  if (listPickerOpen) renderListPicker();
  document.getElementById('schedule-list-picker').hidden = !listPickerOpen;
  document.getElementById('schedule-list-current')
    .setAttribute('aria-expanded', String(listPickerOpen));
}

function closeListPicker() {
  listPickerOpen = false;
  document.getElementById('schedule-list-picker').hidden = true;
  document.getElementById('schedule-list-current').setAttribute('aria-expanded', 'false');
}

function renderListPicker() {
  const ul = document.getElementById('schedule-list-picker');
  ul.replaceChildren();
  for (const l of priorityLists) {
    const li = document.createElement('li');
    li.className = 'sched-list-opt' + (l.slug === activeListSlug ? ' is-selected' : '');
    li.textContent = l.name;
    li.addEventListener('click', () => selectList(l.slug));
    ul.appendChild(li);
  }
}

async function selectList(slug) {
  closeListPicker();
  if (slug === activeListSlug) return;
  try {
    const resp = await api.loadPriorityList(slug);
    activeListSlug = resp.active;
    priorities = resp.entries || [];
    for (const p of priorities) p.mode = p.mode === 'manual' ? 'manual' : 'auto';
    renderListName();
    renderPriorities();
  } catch (err) {
    console.error('[schedule] load list', err);
  }
}

function startRename() {
  const btn = document.getElementById('schedule-list-current');
  const input = document.getElementById('schedule-list-rename-input');
  const entry = priorityLists.find((l) => l.slug === activeListSlug);
  input.value = entry ? entry.name : '';
  btn.hidden = true;
  input.hidden = false;
  input.focus();
  input.select();
}

function cancelRename() {
  document.getElementById('schedule-list-current').hidden = false;
  document.getElementById('schedule-list-rename-input').hidden = true;
}

async function commitRename() {
  const input = document.getElementById('schedule-list-rename-input');
  if (input.hidden) return;   // already committed or cancelled
  const name = input.value.trim();
  cancelRename();
  if (!name) return;
  try {
    const resp = await api.renamePriorityList(activeListSlug, name);
    priorityLists = resp.lists || priorityLists;
    renderListName();
  } catch (err) {
    console.error('[schedule] rename list', err);
  }
}

function toggleSaveAs() {
  const input = document.getElementById('schedule-saveas-input');
  const btn = document.getElementById('schedule-saveas');
  const opening = input.hidden;
  input.hidden = !opening;
  btn.hidden = opening;
  if (opening) { input.value = ''; input.focus(); }
}

function cancelSaveAs() {
  document.getElementById('schedule-saveas-input').hidden = true;
  document.getElementById('schedule-saveas').hidden = false;
}

async function confirmSaveAs() {
  const input = document.getElementById('schedule-saveas-input');
  const name = input.value.trim();
  if (!name) { cancelSaveAs(); return; }
  const status = document.getElementById('schedule-save-status');
  status.textContent = 'saving…';
  try {
    // Created empty, not a duplicate: the new list gets whatever is
    // currently in the editor (via savePriorities below), not whatever was
    // last written to disk for the list being "saved as".
    const created = await api.createPriorityList(name, false);
    const entry = created.lists[created.lists.length - 1];
    await api.loadPriorityList(entry.slug);
    activeListSlug = entry.slug;
    priorityLists = created.lists;
    const saved = await api.savePriorities(priorities);
    priorities = saved.entries || priorities;
    renderListName();
    renderPriorities();
    status.textContent = 'saved as new list';
  } catch (err) {
    status.textContent = `failed: ${err}`;
  } finally {
    cancelSaveAs();
    setTimeout(() => { status.textContent = ''; }, 3000);
  }
}

// --- mode tabs (Station Schedule vs Network Campaign) -----------------------

function mountModeTabs() {
  document.getElementById('schedule-mode-station').addEventListener('click', () => setMode('station'));
  document.getElementById('schedule-mode-campaign').addEventListener('click', () => setMode('campaign'));
}

function setMode(mode) {
  const stationTab = document.getElementById('schedule-mode-station');
  const campaignTab = document.getElementById('schedule-mode-campaign');
  const stationPanel = document.getElementById('schedule-mode-station-panel');
  const campaignPanel = document.getElementById('schedule-mode-campaign-panel');

  const isCampaign = mode === 'campaign';
  stationTab.classList.toggle('is-active', !isCampaign);
  campaignTab.classList.toggle('is-active', isCampaign);
  stationTab.setAttribute('aria-selected', String(!isCampaign));
  campaignTab.setAttribute('aria-selected', String(isCampaign));
  stationPanel.hidden = isCampaign;
  campaignPanel.hidden = !isCampaign;
}

// --- network campaign ---------------------------------------------------------

function mountCampaign() {
  document.getElementById('campaign-preview-btn').addEventListener('click', runCampaignPreview);
  document.getElementById('campaign-commit-btn').addEventListener('click', confirmCampaign);
  document.getElementById('campaign-cfg-save').addEventListener('click', saveCampaignConfig);
  document.getElementById('campaign-max-total').addEventListener('input', (e) => {
    document.getElementById('campaign-max-total-value').textContent = e.target.value;
    campaignConfigDirty = true;
    updateCampaignGate();
  });
}

function updateCampaignGate() {
  const hint = document.getElementById('campaign-token-hint');
  const previewBtn = document.getElementById('campaign-preview-btn');
  hint.hidden = networkTokenSet;
  previewBtn.disabled = !networkTokenSet;
  if (campaignConfigDirty) invalidateCampaignPreview();
}

function invalidateCampaignPreview() {
  campaignPreviewItems = null;
  document.getElementById('campaign-commit-btn').hidden = true;
}

async function saveCampaignConfig() {
  const btn = document.getElementById('campaign-cfg-save');
  const status = document.getElementById('campaign-cfg-status');
  const input = document.getElementById('campaign-max-total');
  const val = input.value.trim();
  btn.disabled = true;
  status.textContent = 'saving…';
  try {
    await api.saveScheduleConfig({ campaign_max_total: val ? parseInt(val, 10) : 0 });
    campaignConfigDirty = false;
    status.textContent = 'saved';
  } catch (err) {
    status.textContent = `failed: ${err}`;
  } finally {
    btn.disabled = false;
    setTimeout(() => { status.textContent = ''; }, 3000);
  }
}

async function runCampaignPreview() {
  const btn = document.getElementById('campaign-preview-btn');
  const box = document.getElementById('campaign-preview-result');
  invalidateCampaignPreview();
  btn.disabled = true;
  btn.textContent = 'CALCULATING…';
  box.replaceChildren(note(
    'Computing candidate stations and passes… this walks every candidate '
    + 'station\'s calendar and can take several minutes. Nothing is booked by a '
    + 'preview.'));
  announceCampaign('Computing preview…');
  try {
    // A stale result already on disk from a previous run has a status other
    // than 'running' too, so checking status alone can't tell "just
    // finished" from "hasn't started yet" - only a changed generated_utc can.
    const before = (await api.campaignPreview())?.generated_utc;
    const started = await api.runCampaignPreview();
    if (started?.status === 'running') {
      box.replaceChildren(alertNote(
        'A campaign run (preview or submit) is already in progress — wait for it '
        + 'to finish, then try again.'));
      announceCampaign('A campaign run is already in progress.');
      return;
    }
    const deadline = Date.now() + CAMPAIGN_POLL_TIMEOUT_MS;
    let finished = false;
    while (Date.now() < deadline) {
      await sleep(POLL_MS);
      const preview = await api.campaignPreview();
      if (preview?.status && preview.status !== 'running' && preview.generated_utc !== before) {
        renderCampaignPreview(preview);
        finished = true;
        break;
      }
    }
    if (!finished) {
      box.replaceChildren(alertNote(
        'Still computing after 20 minutes — it may still finish. Nothing has been '
        + 'booked. Reopen this panel later to check, or use PREVIEW again once it has.'));
      announceCampaign('Preview still computing after 20 minutes. Nothing booked.');
    }
  } catch (err) {
    console.error('[schedule] campaign preview', err);
    box.replaceChildren(alertNote(`Could not compute preview: ${err}`, 'error'));
    announceCampaign(`Preview failed: ${err}`);
  } finally {
    btn.disabled = false;
    btn.textContent = 'PREVIEW';
  }
}

function renderCampaignPreview(preview) {
  const box = document.getElementById('campaign-preview-result');
  box.replaceChildren();

  if (preview.status === 'error') {
    box.appendChild(note(`preview failed: ${preview.error}`));
    return;
  }

  const items = preview.items || [];
  const stationCount = new Set(items.map((it) => it.station_id)).size;
  const meta = document.createElement('p');
  meta.className = 'muted sched-meta';
  meta.textContent = `Preview: ${preview.considered_stations} station(s) considered · `
    + `${items.length} observation(s) would be booked`;
  box.appendChild(meta);

  // Spelled out immediately above the table the operator is about to approve:
  // nothing here is booked yet, and this is exactly what CONFIRM would send.
  if (items.length) {
    const standby = document.createElement('p');
    standby.className = 'sched-preview-standby';
    standby.textContent = `Nothing has been submitted. CONFIRM & SUBMIT would book `
      + `${items.length} observation(s) across ${stationCount} community station(s).`;
    box.appendChild(standby);
  }

  if (items.length) {
    const table = document.createElement('table');
    table.className = 'sched-table';
    const thead = document.createElement('thead');
    thead.innerHTML = '<tr><th>Station</th><th>Start UTC</th><th>End UTC</th><th>Max El</th></tr>';
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    // Every candidate is shown, not just the first N - CONFIRM & SUBMIT
    // books real time on other people's stations, so truncating the review
    // list would let some of what gets booked go unseen.
    for (const item of items) {
      const tr = document.createElement('tr');
      for (const text of [
        item.station_name || `Station ${item.station_id}`,
        shortTime(item.start, 'UTC'),
        shortTime(item.end, 'UTC'),
        `${item.max_elevation_deg.toFixed(0)}°`,
      ]) {
        const td = document.createElement('td');
        td.textContent = text;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    box.appendChild(scrollableTable(
      table,
      `Observations that would be booked, ${items.length} row(s) across `
        + `${stationCount} station(s)`,
    ));
  }

  announceCampaign(items.length
    ? `Preview ready: ${items.length} observation(s) across ${stationCount} station(s) `
      + 'would be booked. Nothing submitted yet.'
    : 'Preview ready: no observations would be booked.');

  // What CONFIRM actually submits — exactly what was just shown, not a
  // recompute at click time, so what's confirmed is what was reviewed.
  campaignPreviewItems = items;
  campaignConfigDirty = false;
  document.getElementById('campaign-commit-btn').hidden = items.length === 0;
}

/* Short status text for screen readers only. Deliberately not wrapped around
   the results box itself: that box holds a table that can run to 150+ rows,
   and making it a live region would re-read the whole thing on every update. */
function announceCampaign(text) {
  const live = document.getElementById('campaign-status-live');
  if (live) live.textContent = text;
}

async function confirmCampaign() {
  if (!campaignPreviewItems || !campaignPreviewItems.length) return;
  const btn = document.getElementById('campaign-commit-btn');
  const box = document.getElementById('campaign-preview-result');

  // The last gate before real bookings land on real strangers' hardware. The
  // preview above is the review step; this is only here so that the single
  // click that spends other people's antenna time cannot be a stray one. It
  // names the numbers rather than asking "are you sure?".
  const stationCount = new Set(campaignPreviewItems.map((it) => it.station_id)).size;
  const ok = window.confirm(
    `Book ${campaignPreviewItems.length} observation(s) on ${stationCount} community `
    + 'station(s) via SatNOGS Network?\n\n'
    + 'These are other operators\' ground stations. Accepted bookings cannot be '
    + 'undone from this dashboard.',
  );
  if (!ok) return;

  btn.disabled = true;
  btn.textContent = 'SUBMITTING…';
  announceCampaign(`Submitting ${campaignPreviewItems.length} booking(s)…`);
  try {
    // Same staleness problem as the preview poll: the last-run file already
    // has a non-'running' status from any earlier commit, so only a changed
    // generated_utc proves *this* submit actually finished.
    const before = (await api.campaignLastRun())?.generated_utc;
    const started = await api.commitCampaign(campaignPreviewItems);
    if (started?.status === 'running') {
      // Preview and commit share one "is a campaign op running" flag on the
      // backend, so a still-running preview silently blocks this - nothing
      // was submitted. Say so instead of quietly discarding the click.
      box.prepend(alertNote(
        'Not submitted — a campaign preview or submit is already running. Nothing '
        + 'was sent. Wait for it to finish, then click CONFIRM & SUBMIT again.'));
      announceCampaign('Not submitted: a campaign run is already in progress.');
      return;
    }
    const deadline = Date.now() + CAMPAIGN_POLL_TIMEOUT_MS;
    let last = null;
    while (Date.now() < deadline) {
      await sleep(POLL_MS);
      const run = await api.campaignLastRun();
      if (run?.status && run.status !== 'running' && run.generated_utc !== before) { last = run; break; }
    }
    if (last) {
      renderCampaignLastRun(last);
      await loadCampaignHistory();
      invalidateCampaignPreview();
    } else {
      box.prepend(alertNote(
        'Still submitting after 20 minutes — some bookings may already have been '
        + 'accepted. Do not resubmit: reopen this panel later to check the result '
        + 'and history first.'));
      announceCampaign('Still submitting after 20 minutes. Check history before resubmitting.');
    }
  } catch (err) {
    console.error('[schedule] campaign commit', err);
    box.prepend(alertNote(
      `Submit failed: ${err}. Some bookings may still have been accepted — check `
      + 'the run history below before trying again.', 'error'));
    announceCampaign(`Submit failed: ${err}`);
  } finally {
    btn.disabled = false;
    btn.textContent = 'CONFIRM & SUBMIT';
  }
}

async function loadCampaignLastRun() {
  try {
    renderCampaignLastRun(await api.campaignLastRun());
  } catch (err) {
    console.error('[schedule] campaign last run', err);
  }
}

function renderCampaignLastRun(run) {
  if (!run || run.status === 'never_run' || run.status === 'running') return;
  const box = document.getElementById('campaign-preview-result');

  // Built as one block and prepended once. The previous version prepended the
  // rejection list and the summary line separately, which put them at the top
  // in reverse order of construction and was easy to get wrong.
  const wrap = document.createElement('div');
  wrap.className = 'sched-campaign-outcome';

  const meta = document.createElement('p');
  meta.className = 'muted sched-meta';

  const trigger = document.createElement('span');
  trigger.className = `sched-run-trigger ${run.trigger === 'manual' ? 'manual' : ''}`.trim();
  trigger.textContent = run.trigger === 'auto' ? 'AUTO' : 'MANUAL';
  meta.appendChild(trigger);

  if (run.status === 'error') {
    const text = document.createElement('span');
    text.className = 'sched-stat bad';
    text.textContent = 'SUBMIT FAILED';
    meta.appendChild(text);
    const detail = document.createElement('span');
    detail.textContent = run.error || 'no detail reported';
    meta.appendChild(detail);
    announceCampaign(`Submit failed: ${run.error || 'no detail reported'}`);
  } else {
    const submitted = run.submitted ?? 0;
    const accepted = run.accepted ?? 0;
    const rejected = Math.max(0, submitted - accepted);

    const label = document.createElement('span');
    label.textContent = 'Last submit:';
    meta.appendChild(label);

    // Accepted first and always coloured, rejected second and only red when
    // it is actually non-zero — a green 0 or a red 0 both misread at a glance.
    const okStat = document.createElement('span');
    okStat.className = `sched-stat ${accepted > 0 ? 'ok' : 'zero'}`;
    okStat.textContent = `${accepted} booked`;
    const badStat = document.createElement('span');
    badStat.className = `sched-stat ${rejected > 0 ? 'bad' : 'zero'}`;
    badStat.textContent = `${rejected} rejected`;
    const ofText = document.createElement('span');
    ofText.textContent = `of ${submitted} submitted`;
    meta.append(okStat, badStat, ofText);
    announceCampaign(
      `Submit finished: ${accepted} booked, ${rejected} rejected, of ${submitted} submitted.`);
  }
  wrap.appendChild(meta);

  if (run.errors && run.errors.length) {
    wrap.appendChild(buildRejectionReport(run.errors));
  }
  box.prepend(wrap);
}

/* ── rejection reasons ────────────────────────────────────────────────────
   The backend hands back one raw line per rejected booking, each of the form

     <start> norad-transmitter <uuid>: HTTP <status> <body[:300]>

   A real submit produces dozens of these, but nearly always for a handful of
   underlying causes (too short, no permission on that station, overlaps an
   existing booking). Dumped flat that is a wall of near-identical JSON; what
   an operator actually needs first is "which reasons, and how many of each",
   with the per-item detail one click away. Purely a display transform — the
   raw line is still what gets shown inside each group.

   Note the start is NOT ISO: it comes from the schedule item that was POSTed,
   whose start went through format_api_datetime() — "YYYY-MM-DD HH:MM:SS",
   space-separated and always UTC. Because that timestamp contains a space,
   anchoring on " norad-transmitter " rather than on the first run of
   whitespace is what makes this pattern match real data at all. */
const CAMPAIGN_ERROR_RE = /^(.*?)\s+norad-transmitter\s+(\S+):\s*HTTP\s+(\d+)\s*([\s\S]*)$/;

function parseCampaignError(raw) {
  const m = CAMPAIGN_ERROR_RE.exec(raw);
  if (!m) return { reason: raw, detail: raw };
  const [, start, , status, body] = m;
  const reason = extractRejectionReason(body) || `HTTP ${status}`;
  return { reason: `HTTP ${status} · ${reason}`, detail: `${rejectionTime(start)} — ${reason}` };
}

/* new Date("2026-09-20 04:12:00") is read as *local* time by every engine that
   accepts it at all, which would print a shifted hour under a "UTC" label —
   on a panel where the operator is matching rows against a pass calendar,
   that is worse than showing nothing. Normalise to ISO first, and fall back
   to the raw string rather than guess. */
function rejectionTime(start) {
  const text = (start || '').trim();
  const iso = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(text)
    ? `${text.replace(' ', 'T')}Z`
    : text;
  if (Number.isNaN(new Date(iso).getTime())) return text || 'unknown time';
  return `${shortTime(iso, 'UTC')} UTC`;
}

/* Best-effort: the body is a DRF error payload truncated to 300 characters, so
   it may well not parse as JSON. Pull the human sentence out when one of the
   familiar shapes is recognisable, and fall back to the trimmed body — never
   to nothing, since an unrecognised rejection is exactly the one worth
   reading. */
function extractRejectionReason(body) {
  const text = (body || '').trim();
  if (!text) return '';
  const patterns = [
    /"non_field_errors"\s*:\s*\[\s*"([^"]+)"/,
    /"detail"\s*:\s*"([^"]+)"/,
    /"(?:start|end|ground_station|transmitter_uuid)"\s*:\s*\[\s*"([^"]+)"/,
  ];
  for (const re of patterns) {
    const m = re.exec(text);
    if (m) return m[1];
  }
  return text.length > 140 ? `${text.slice(0, 140)}…` : text;
}

function groupCampaignErrors(errors) {
  const groups = new Map();
  for (const raw of errors) {
    const { reason, detail } = parseCampaignError(raw);
    if (!groups.has(reason)) groups.set(reason, { reason, items: [] });
    groups.get(reason).items.push(detail);
  }
  // Commonest cause first: that is the one worth fixing before a retry.
  return [...groups.values()].sort((a, b) => b.items.length - a.items.length);
}

function buildRejectionReport(errors) {
  const groups = groupCampaignErrors(errors);
  const wrap = document.createElement('div');
  wrap.className = 'sched-reject-summary';

  const groupList = document.createElement('div');
  groupList.className = 'sched-reject-groups';
  groupList.id = nextDomId('sched-reject-groups');
  groupList.hidden = true;

  // Two levels of disclosure, both collapsed: the whole report is hidden until
  // asked for, and each reason's itemised list is hidden inside that. A clean
  // run should not have to scroll past a previous run's failures.
  const top = document.createElement('button');
  top.type = 'button';
  top.className = 'sched-notices-toggle';
  top.setAttribute('aria-controls', groupList.id);
  const topLabel = (open) =>
    `${errors.length} rejection${errors.length === 1 ? '' : 's'}, `
    + `${groups.length} reason${groups.length === 1 ? '' : 's'} ${open ? '▴' : '▾'}`;
  top.textContent = topLabel(false);
  top.setAttribute('aria-expanded', 'false');
  top.addEventListener('click', () => {
    const showing = groupList.hidden;
    groupList.hidden = !showing;
    top.setAttribute('aria-expanded', String(showing));
    top.textContent = topLabel(showing);
  });
  wrap.appendChild(top);

  for (const group of groups) {
    const items = document.createElement('ul');
    items.className = 'sched-notices sched-reject-group-items';
    items.id = nextDomId('sched-reject-items');
    items.hidden = true;
    items.tabIndex = 0;
    items.setAttribute('aria-label', `${group.items.length} rejection(s): ${group.reason}`);
    for (const detail of group.items) {
      const li = document.createElement('li');
      li.className = 'sched-notice error';
      li.textContent = detail;
      items.appendChild(li);
    }

    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'sched-reject-group-toggle';
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-controls', items.id);

    const count = document.createElement('span');
    count.className = 'sched-reject-group-count';
    count.textContent = `${group.items.length}×`;
    const reason = document.createElement('span');
    reason.className = 'sched-reject-group-reason';
    reason.textContent = group.reason;
    reason.title = group.reason;   // the full text, when it is ellipsised
    const caret = document.createElement('span');
    caret.className = 'sched-reject-group-caret';
    caret.setAttribute('aria-hidden', 'true');
    caret.textContent = '▾';
    toggle.append(count, reason, caret);
    toggle.addEventListener('click', () => {
      const showing = items.hidden;
      items.hidden = !showing;
      toggle.setAttribute('aria-expanded', String(showing));
      caret.textContent = showing ? '▴' : '▾';
    });

    const groupEl = document.createElement('div');
    groupEl.className = 'sched-reject-group';
    groupEl.append(toggle, items);
    groupList.appendChild(groupEl);
  }

  wrap.appendChild(groupList);
  return wrap;
}

async function loadCampaignHistory() {
  try {
    const resp = await api.campaignHistory();
    renderCampaignHistory(resp.history || []);
  } catch (err) {
    console.error('[schedule] campaign history', err);
  }
}

function renderCampaignHistory(history) {
  const box = document.getElementById('campaign-history');
  box.replaceChildren();
  if (!history.length) {
    box.appendChild(note('no runs yet'));
    return;
  }
  const table = document.createElement('table');
  table.className = 'sched-table';
  const thead = document.createElement('thead');
  thead.innerHTML = '<tr><th>Time UTC</th><th>Trigger</th><th>Booked</th><th>Rejected</th><th>Status</th></tr>';
  table.appendChild(thead);
  const tbody = document.createElement('tbody');
  for (const run of history.slice().reverse()) {
    const tr = document.createElement('tr');

    const tdTime = document.createElement('td');
    tdTime.textContent = run.generated_utc ? shortTime(run.generated_utc, 'UTC') : '—';
    const tdTrigger = document.createElement('td');
    const tag = document.createElement('span');
    tag.className = `sched-run-trigger ${run.trigger === 'manual' ? 'manual' : ''}`.trim();
    tag.textContent = run.trigger === 'auto' ? 'AUTO' : 'MANUAL';
    tdTrigger.appendChild(tag);
    const tdBooked = document.createElement('td');
    tdBooked.textContent = String(run.accepted ?? 0);
    // Only a non-zero rejection count earns the colour — a column of red
    // zeroes would make a run of clean submits look like a problem.
    const tdRejected = document.createElement('td');
    tdRejected.textContent = String(run.rejected ?? 0);
    if ((run.rejected ?? 0) > 0) tdRejected.className = 'sched-cell-bad';
    const tdStatus = document.createElement('td');
    tdStatus.textContent = run.status || '—';
    if (run.status === 'error') tdStatus.className = 'sched-cell-bad';

    tr.append(tdTime, tdTrigger, tdBooked, tdRejected, tdStatus);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  // The backend keeps up to 200 runs; without a bounded scroll region this
  // table alone could push everything else out of the card.
  box.appendChild(scrollableTable(table, `Campaign run history, ${history.length} run(s)`));
}
