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

export function mountSchedule() {
  document.getElementById('schedule-toggle').addEventListener('click', open);
  document.getElementById('schedule-close').addEventListener('click', close);
  document.getElementById('schedule-run').addEventListener('click', runNow);
  document.getElementById('schedule-save').addEventListener('click', save);
  document.getElementById('schedule-add').addEventListener('click', addEntry);
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
    li.addEventListener('dragstart', () => { dragFrom = i; });
    li.addEventListener('dragover', (ev) => ev.preventDefault());
    li.addEventListener('drop', (ev) => {
      ev.preventDefault();
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

function addEntry() {
  const noradInput = document.getElementById('schedule-add-norad');
  const weightInput = document.getElementById('schedule-add-weight');

  const norad = parseInt(noradInput.value, 10);
  if (!Number.isInteger(norad) || norad <= 0) {
    noradInput.focus();
    return;
  }
  if (priorities.some((p) => p.norad_cat_id === norad)) {
    // Already listed — edit its weight in place rather than duplicating it.
    noradInput.focus();
    noradInput.select();
    return;
  }

  priorities.push({
    norad_cat_id: norad,
    weight: clamp01(parseFloat(weightInput.value) || 0.5),
    transmitter_uuid: null,
    satellite: '',
    transmitter_desc: '',
  });
  noradInput.value = '';
  weightInput.value = '0.50';
  noradInput.focus();
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
