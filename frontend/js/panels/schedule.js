/* Station Schedule: the autoscheduler's last run for this station, and an
   editable priority order that feeds its next one.

   Shown as an overlay rather than a grid tile (see schedule.css for why),
   opened from a chip in the header. */

import { api } from '../core/api.js';
import { shortTime } from '../core/format.js';

const POLL_MS = 3000;
const POLL_TIMEOUT_MS = 120_000;

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
  await Promise.all([loadLastRun(), loadPriorities(), loadConfig(), loadPriorityLists()]);
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
    list.hidden = true;
    for (const n of notices) {
      const li = document.createElement('li');
      li.className = `sched-notice ${n.severity === 'error' ? 'error' : ''}`.trim();
      li.textContent = n.message;
      list.appendChild(li);
    }
    toggle.addEventListener('click', () => { list.hidden = !list.hidden; });
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
  box.appendChild(table);
}

function note(text) {
  const p = document.createElement('p');
  p.className = 'muted';
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

  if (openTxNorad === p.norad_cat_id) {
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
  if (settingsOpen) loadConfig();
}

function closeSettings() {
  settingsOpen = false;
  document.getElementById('schedule-settings').hidden = true;
}

async function loadConfig() {
  try {
    const cfg = await api.scheduleConfig();
    document.getElementById('schedule-cfg-station').value = cfg.station_id || '';
    document.getElementById('schedule-cfg-token').placeholder =
      cfg.db_token_set ? 'saved (hidden) — leave blank to keep' : 'unchanged';
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

  const stationVal = stationInput.value.trim();
  // Blank clears the override back to the dashboard's own default (0 is
  // falsy but not None, so the backend reads it as "clear this").
  const stationId = stationVal ? parseInt(stationVal, 10) : 0;
  // Blank token means "leave whatever is already saved alone" — the field
  // never redisplays a saved secret, so blank cannot mean "clear it".
  const tokenVal = tokenInput.value ? tokenInput.value : undefined;

  btn.disabled = true;
  status.textContent = 'saving…';
  try {
    await api.saveScheduleConfig(stationId, tokenVal);
    tokenInput.value = '';
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
}

function closeListPicker() {
  listPickerOpen = false;
  document.getElementById('schedule-list-picker').hidden = true;
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
