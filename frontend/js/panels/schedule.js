/* Station Schedule: the autoscheduler's last run for this station, and an
   editable priority order that feeds its next one.

   Shown as an overlay rather than a grid tile (see schedule.css for why),
   opened from a chip in the header. */

import { api } from '../core/api.js';
import { shortTime } from '../core/format.js';

const POLL_MS = 3000;
const POLL_TIMEOUT_MS = 120_000;

let priorities = [];   // [{norad_cat_id, weight, transmitter_uuid}], current display order
let dragFrom = -1;
let pendingAdd = null;  // {norad, name} once a search suggestion is picked
let searchDebounce;

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
}

const panel = () => document.getElementById('schedule-panel');

async function open() {
  panel().hidden = false;
  await Promise.all([loadLastRun(), loadPriorities()]);
}

function close() {
  panel().hidden = true;
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
  meta.textContent = `Generated ${shortTime(run.generated_utc, 'UTC')} UTC · `
    + `${run.observations.length} of ${run.considered} candidate pass(es) booked`;
  box.appendChild(meta);

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
  try {
    const resp = await api.getPriorities();
    priorities = resp.entries || [];
  } catch (err) {
    console.error('[schedule] priorities', err);
    priorities = [];
  }
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
    li.draggable = true;

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
    const tx = document.createElement('span');
    tx.className = 'sched-prio-tx';
    tx.textContent = p.transmitter_desc || (p.transmitter_uuid ? p.transmitter_uuid : 'auto (best available)');
    info.append(name, norad, tx);

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

    li.append(handle, info, weight, del);

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
    satellite: name,
    transmitter_desc: '',
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
  // would look interactive while doing nothing.
  const n = priorities.length;
  priorities.forEach((p, i) => {
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
