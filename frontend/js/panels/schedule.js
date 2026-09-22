/* Station Schedule: the autoscheduler's last run for this station, and an
   editable priority order that feeds its next one.

   Shown as an overlay rather than a grid tile (see schedule.css for why),
   opened from a chip in the header. */

import { api } from '../core/api.js';
import { shortTime, shortDateTime } from '../core/format.js';

const POLL_MS = 3000;
// 25 minutes, not the 120s this was. A Station Schedule run now shells out to
// satnogs-auto-scheduler, and on a cold cache that tool refetches every
// transmitter's statistics - it logs "this will take some minutes" itself.
// Its booking POST also carries no timeout of its own. A 120s budget would
// abandon a perfectly healthy run and leave the operator with no idea whether
// anything was booked, which is the worst possible state on this tab.
const POLL_TIMEOUT_MS = 25 * 60_000;

// State the two run buttons need. Both are disabled until BOTH SatNOGS tokens
// are set - including DRY RUN, because satnogs-auto-scheduler validates its
// whole configuration before it looks at the dry-run flag.
let dbTokenSet = false;
let armedRealRun = false;      // RUN NOW's two-step confirm
let armedRealRunTimer = null;
let autoRunCfg = null;         // the config as last loaded/saved
let autoTimes = [];            // HH:MM chips, the editable copy
let autoDirty = false;
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
let lastPreviewCapableStations = null;  // stations the last preview found usable, for the reach hint

export function mountSchedule() {
  document.getElementById('schedule-toggle').addEventListener('click', open);
  document.getElementById('schedule-close').addEventListener('click', close);
  document.getElementById('schedule-dry-run').addEventListener('click', () => startRun(true));
  document.getElementById('schedule-run').addEventListener('click', onRealRunClick);
  mountAutoRun();

  // The browser's own guard. It cannot show our copy, but it is the only
  // thing standing between a reloaded tab and a lost priority list.
  window.addEventListener('beforeunload', (ev) => {
    if (!prioritiesDirty && !autoDirty) return;
    ev.preventDefault();
    ev.returnValue = '';
  });
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

/* Unsaved priority edits.

   Note where the discard actually happens: close() only hides the panel and
   leaves `priorities` in module state, so a reopened panel still shows the
   edits until open() -> loadPriorities() overwrites them. The guard therefore
   has to sit on both doors, and close() must not be trusted to reset it. */
let prioritiesDirty = false;

function markPrioritiesDirty() {
  prioritiesDirty = true;
  const status = document.getElementById('schedule-save-status');
  if (status && !status.textContent) status.textContent = 'unsaved changes';
}

/* The same two-step idiom the RUN NOW button uses, for the same reason: this
   codebase has no modal, and silently throwing away typed work is worse than
   asking twice. */
let armedDiscard = null;
let armedDiscardTimer = null;

/* The armed label is swapped by hiding the button's real children and adding
   a sibling, never by setting textContent.

   #schedule-list-current contains <span id="schedule-list-name">, and
   `button.textContent = '...'` replaces every child with one text node -
   deleting that span for good. Every later renderListName() then threw on a
   null element, so one armed-and-abandoned discard permanently broke the list
   picker. Restoring by textContent could not bring the span back either. */
function setArmedLabel(button, text) {
  let overlay = button.querySelector(':scope > .sched-armed-label');
  if (text === null) {
    if (overlay) overlay.remove();
    for (const child of button.children) {
      if (child !== overlay) child.hidden = false;
    }
    button.dataset.armedHidden = '';
    return;
  }
  if (!overlay) {
    overlay = document.createElement('span');
    overlay.className = 'sched-armed-label';
    button.appendChild(overlay);
  }
  overlay.textContent = text;
  for (const child of button.children) {
    if (child !== overlay) child.hidden = true;
  }
  button.dataset.armedHidden = '1';
}

function confirmDiscard(button, onConfirm) {
  if (!prioritiesDirty) { onConfirm(); return; }
  if (armedDiscard === button) {
    clearTimeout(armedDiscardTimer);
    setArmedLabel(button, null);
    armedDiscard = null;
    prioritiesDirty = false;
    onConfirm();
    return;
  }
  if (armedDiscard) disarmDiscard();
  armedDiscard = button;
  setArmedLabel(button, 'DISCARD CHANGES?');
  armedDiscardTimer = setTimeout(disarmDiscard, 5000);
}

function disarmDiscard() {
  clearTimeout(armedDiscardTimer);
  if (armedDiscard) {
    setArmedLabel(armedDiscard, null);
    armedDiscard = null;
  }
}

function close() {
  confirmDiscard(document.getElementById('schedule-close'), () => {
    panel().hidden = true;
    openTxNorad = null;
    settingsOpen = false;
    listPickerOpen = false;
    disarmRealRun();
  });
}

async function loadLastRun() {
  try {
    const run = await api.scheduleLastRun();
    renderLastRun(run);
    renderNextAutoRun(run?.auto_run_next_utc);
    noteRunWarnings(run);
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
  // `running` is the live flag the route adds; the STORED result never says
  // "running", so keying off run.status here showed the previous run as
  // current, with both buttons enabled, while a real booking run was in
  // flight. run.status is still checked for the benefit of the immediate
  // POST response, which does use it.
  if (run.running || run.status === 'running') {
    box.appendChild(note(
      run.progress ? `a run is in progress… ${run.progress}` : 'a run is in progress…',
    ));
    return;
  }
  if (run.status === 'error') {
    box.appendChild(runBadge(run));
    box.appendChild(alertNote(`Last run failed: ${run.error}`, 'error'));
    appendNotices(box, run.notices || []);
    appendLogDisclosure(box);
    return;
  }

  box.appendChild(runBadge(run));

  const meta = document.createElement('p');
  meta.className = 'muted sched-meta';
  const text = document.createElement('span');
  const parts = [
    `Generated ${shortTime(run.generated_utc, 'UTC')} UTC`,
    // "selected", not "booked": on a dry run nothing was booked at all, and
    // on a real run the badge above is the authority on what actually landed.
    `${run.observations.length} of ${run.considered || run.observations.length} candidate pass(es) selected`,
  ];
  if (run.already_scheduled?.length) {
    parts.push(`${run.already_scheduled.length} already on the calendar`);
  }
  if (run.run_duration_s) parts.push(`took ${Math.round(run.run_duration_s)}s`);
  text.textContent = `${parts.join(' · ')} · `;
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
    table,
    `${run.dry_run ? 'Planned' : 'Booked'} observations, ${run.observations.length} row(s)`));

  if (run.already_scheduled?.length) {
    // Kept visually apart rather than merged into the table above: the tool
    // prints these with zeroed azimuth and elevation, so rendering them as if
    // they were planned now would show a column of confident-looking 0°.
    const already = document.createElement('p');
    already.className = 'muted sched-meta';
    already.textContent =
      `${run.already_scheduled.length} pass(es) were already booked on this station `
      + 'and were left alone.';
    box.appendChild(already);
  }
  appendLogDisclosure(box);
}

/* What this run was, said before anything else, because "7 observations" means
   two completely different things depending on it. */
function runBadge(run) {
  const badge = document.createElement('span');
  badge.className = 'sched-run-trigger';
  if (run.trigger === 'auto') badge.classList.add('manual');

  if (run.status === 'error') {
    badge.classList.add('danger');
    badge.textContent = run.dry_run ? 'DRY RUN FAILED' : 'RUN FAILED';
  } else if (run.booked_state === 'mock') {
    // Deliberately not styled as a booking: nothing was started and nothing
    // reached SatNOGS, whichever button was pressed.
    badge.textContent = `SIMULATED · ${run.planned ?? run.observations.length} PLANNED`;
  } else if (run.dry_run) {
    badge.textContent = `DRY RUN · ${run.planned ?? run.observations.length} PLANNED`;
  } else if (run.booked_state === 'confirmed') {
    badge.classList.add('danger');
    badge.textContent = `BOOKED ${run.booked} of ${run.planned}`;
  } else if (run.booked_state === 'partial') {
    badge.classList.add('danger');
    badge.textContent = `PLANNED ${run.planned} · CONFIRMED ${run.booked}`;
  } else {
    badge.classList.add('danger');
    badge.textContent = `PLANNED ${run.planned} · ${String(run.booked_state || 'unknown').toUpperCase()}`;
  }
  const wrap = document.createElement('p');
  wrap.className = 'sched-badge-row';
  wrap.appendChild(badge);
  if (run.trigger === 'auto') {
    const who = document.createElement('span');
    who.className = 'hint';
    who.textContent = ' fired by the auto-run timer';
    wrap.appendChild(who);
  }
  return wrap;
}

/* The parsed result cannot carry everything the tool said, and when a run does
   something surprising the transcript is where the answer actually is. */
function appendLogDisclosure(box) {
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'sched-notices-toggle';
  toggle.textContent = 'Raw output ▾';
  const pre = document.createElement('pre');
  pre.className = 'sched-log';
  pre.id = nextDomId('sched-log');
  pre.hidden = true;
  toggle.setAttribute('aria-expanded', 'false');
  toggle.setAttribute('aria-controls', pre.id);
  let loaded = false;
  toggle.addEventListener('click', async () => {
    const showing = pre.hidden;
    pre.hidden = !showing;
    toggle.setAttribute('aria-expanded', String(showing));
    toggle.textContent = `Raw output ${showing ? '▴' : '▾'}`;
    if (showing && !loaded) {
      loaded = true;
      pre.textContent = 'loading…';
      try {
        pre.textContent = await api.scheduleLog(400);
      } catch (err) {
        pre.textContent = `could not load the run log: ${err}`;
      }
    }
  });
  box.append(toggle, pre);
}

/* Extracted so the error path can show warnings too - it used to return before
   ever rendering them, which is exactly when they matter most. */
function appendNotices(box, notices) {
  if (!notices.length) return;
  const list = document.createElement('ul');
  list.className = 'sched-notices';
  for (const n of notices) {
    const li = document.createElement('li');
    li.className = `sched-notice ${n.severity === 'error' ? 'error' : ''}`.trim();
    li.textContent = n.message;
    list.appendChild(li);
  }
  box.appendChild(list);
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

/* RUN NOW books real observations, so it arms first and fires second.

   A two-step inline confirm rather than a modal: this codebase has no modal,
   and the Network Campaign tab's preview -> CONFIRM is the nearest existing
   idiom for "this one actually does something irreversible". */
function onRealRunClick() {
  const btn = document.getElementById('schedule-run');
  if (!armedRealRun) {
    armedRealRun = true;
    btn.classList.add('is-armed');
    btn.textContent = 'BOOK FOR REAL?';
    setRunHint(
      'This runs the scheduler again from scratch and books what it selects. '
      + 'It recomputes, so the result can differ from the preview above. '
      + 'Click again within 5s to confirm.',
    );
    clearTimeout(armedRealRunTimer);
    armedRealRunTimer = setTimeout(disarmRealRun, 5000);
    return;
  }
  disarmRealRun();
  startRun(false);
}

function disarmRealRun() {
  clearTimeout(armedRealRunTimer);
  armedRealRun = false;
  const btn = document.getElementById('schedule-run');
  btn.classList.remove('is-armed');
  btn.textContent = 'RUN NOW';
  refreshRunGate();
}

async function startRun(dryRun) {
  const btn = document.getElementById(dryRun ? 'schedule-dry-run' : 'schedule-run');
  const other = document.getElementById(dryRun ? 'schedule-run' : 'schedule-dry-run');
  const label = btn.textContent;
  btn.disabled = true;
  other.disabled = true;
  btn.textContent = dryRun ? 'DRY RUN…' : 'BOOKING…';
  try {
    const started = await api.runSchedule(dryRun);
    if (started?.status === 'running') {
      // Nothing was started - a run was already in flight (the auto-run
      // timer, or another tab). Polling from here would wait for THAT run and
      // then render its result as though it were this one, which after
      // pressing BOOK FOR REAL is the worst possible thing to get wrong.
      document.getElementById('schedule-last-run').prepend(alertNote(
        'A run was already in progress, so this one did not start. '
        + 'Nothing was booked by this click. Wait for the current run to '
        + 'finish, then try again.',
      ));
      return;
    }
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    while (Date.now() < deadline) {
      await sleep(POLL_MS);
      const run = await api.scheduleLastRun();
      // Poll the live `running` flag rather than diffing generated_utc: a run
      // that fails now writes a result too, so a changed timestamp is no
      // longer the only signal, and `running` cannot be confused by a run
      // triggered from somewhere else.
      if (run && !run.running) {
        renderLastRun(run);
        return;
      }
      if (run?.progress) btn.textContent = shorten(run.progress);
    }
    document.getElementById('schedule-last-run').prepend(alertNote(
      `This run has taken longer than ${Math.round(POLL_TIMEOUT_MS / 60000)} minutes. `
      + 'It may still be going - reopen this panel to check before running again, '
      + 'so nothing is booked twice.',
    ));
  } catch (err) {
    console.error('[schedule] run', err);
    document.getElementById('schedule-last-run').prepend(
      alertNote(`Could not start the run: ${err}`, 'error'));
  } finally {
    btn.textContent = label;
    refreshRunGate();
  }
}

/* The tool's progress lines are long and its module names are noise in a
   button. */
function shorten(text) {
  const clean = String(text).replace(/\s+/g, ' ').trim();
  return clean.length > 28 ? `${clean.slice(0, 27)}…` : clean;
}

function setRunHint(text) {
  const hint = document.getElementById('schedule-run-hint');
  hint.textContent = text || '';
  hint.hidden = !text;
}

/* Both buttons are gated on BOTH tokens, and the hint has to say why DRY RUN
   is included - otherwise a disabled DRY RUN button reads as a bug. */
function refreshRunGate() {
  const dry = document.getElementById('schedule-dry-run');
  const real = document.getElementById('schedule-run');
  const ready = dbTokenSet && networkTokenSet;
  dry.disabled = !ready;
  real.disabled = !ready;
  if (ready) {
    if (!armedRealRun) setRunHint('');
    return;
  }
  const missing = [];
  if (!dbTokenSet) missing.push('SatNOGS DB');
  if (!networkTokenSet) missing.push('SatNOGS Network');
  setRunHint(
    `Set the ${missing.join(' and ')} token${missing.length > 1 ? 's' : ''} in ⚙ first. `
    + 'satnogs-auto-scheduler checks its whole configuration before it looks at '
    + 'the dry-run flag, so DRY RUN needs both of them too.',
  );
}

async function loadPriorities() {
  openTxNorad = null;
  // This is where edits are actually thrown away - see the note on close().
  prioritiesDirty = false;
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

/* Keep focus and scroll across a re-render.

   Every edit - delete, mode toggle, transmitter pick, drop, arrow reorder -
   rebuilds the whole <ul>, which throws away the focused control and scrolls
   the list back to the top. Patching rows in place instead would avoid the
   rebuild, but every row closes over its own index and a delete shifts every
   index after it, so a partial patch has to renumber anyway - the rebuild is
   the honest implementation and this restores what it costs.

   Identified by NORAD rather than position, because the row the operator was
   working on is the one that should keep focus even after a reorder moves it. */
function renderPriorities() {
  const ul = document.getElementById('schedule-priorities');

  // The <ul> is not the scroll container - .schedule-section is, via its own
  // overflow-y: auto. Saving ul.scrollTop would always read 0.
  const scroller = ul.closest('.schedule-section') || ul;
  const active = document.activeElement;
  const activeRow = active?.closest?.('.sched-prio-row');
  const keep = activeRow
    ? {
        norad: Number(activeRow.dataset.norad),
        // Which control within the row, so focus lands back on the weight box
        // rather than the row itself if that is where it was.
        control: active === activeRow ? null : [...activeRow.querySelectorAll('button,input')].indexOf(active),
        scrollTop: scroller.scrollTop,
      }
    : { scrollTop: scroller.scrollTop };

  renderPriorityRows(ul);

  scroller.scrollTop = keep.scrollTop;
  if (keep.norad === undefined) return;
  const row = ul.querySelector(`.sched-prio-row[data-norad="${keep.norad}"]`);
  if (!row) return;   // the row the operator was on is the one they deleted
  let target = keep.control === null || keep.control < 0
    ? row
    : row.querySelectorAll('button,input')[keep.control] || row;
  // A move to a list boundary disables the very control that was pressed
  // (⤒ on the top row, ▲ on the top row, ▼ on the bottom). focus() on a
  // disabled element does nothing at all, so focus would drop to <body> and
  // a keyboard operator would lose their place entirely.
  if (target.disabled) target = row;
  target.focus({ preventScroll: true });
  scroller.scrollTop = keep.scrollTop;
}

function renderPriorityRows(ul) {
  ul.replaceChildren();
  if (!priorities.length) {
    ul.appendChild(note('no priority file yet'));
    return;
  }

  priorities.forEach((p, i) => {
    const li = document.createElement('li');
    li.className = 'sched-prio-row';
    li.dataset.norad = String(p.norad_cat_id);
    // Not draggable while its own transmitter picker is open — a dragstart
    // fired from inside the open popover would otherwise drag the row instead
    // of letting the click land on an option.
    li.draggable = openTxNorad !== p.norad_cat_id;
    // Focusable and announced, so the arrow buttons below have something to
    // return focus to and a screen reader can say which row is being moved.
    li.tabIndex = 0;
    li.setAttribute('aria-label',
      `${p.satellite || `NORAD ${p.norad_cat_id}`}, priority ${i + 1} of ${priorities.length}`);
    li.addEventListener('keydown', (ev) => {
      if (!ev.altKey) return;
      if (ev.key === 'ArrowUp') { ev.preventDefault(); moveRow(i, i - 1); }
      if (ev.key === 'ArrowDown') { ev.preventDefault(); moveRow(i, i + 1); }
      if (ev.key === 'Home') { ev.preventDefault(); moveRow(i, 0); }
    });

    const handle = document.createElement('span');
    handle.className = 'sched-drag';
    handle.textContent = '⠿';

    // Per-row reorder controls. The drag handle stays, but it is mouse-only.
    const moves = document.createElement('span');
    moves.className = 'sched-prio-moves';
    for (const [label, target, title] of [
      ['▲', i - 1, 'Move up (Alt+Up)'],
      ['▼', i + 1, 'Move down (Alt+Down)'],
      ['⤒', 0, 'Move to top (Alt+Home)'],
    ]) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'sched-move-btn';
      btn.textContent = label;
      btn.title = title;
      btn.setAttribute('aria-label',
        `${title.split(' (')[0]}: ${p.satellite || `NORAD ${p.norad_cat_id}`}`);
      btn.disabled = target === i || target < 0 || target >= priorities.length;
      btn.addEventListener('click', () => moveRow(i, target));
      moves.appendChild(btn);
    }

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
    const runWarning = runWarningsByNorad.get(p.norad_cat_id);
    if (runWarning) {
      const flag = document.createElement('span');
      // Reuses the existing dead-transmitter tag rather than inventing a
      // second warning vocabulary for the same row.
      flag.className = 'rx-tag dead';
      flag.textContent = 'LAST RUN';
      flag.title = runWarning;
      name.appendChild(document.createTextNode(' '));
      name.appendChild(flag);
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
      markPrioritiesDirty();
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
      markPrioritiesDirty();
    });

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'sched-prio-del';
    del.title = `remove NORAD ${p.norad_cat_id}`;
    del.textContent = '×';
    del.addEventListener('click', () => {
      priorities.splice(i, 1);
      markPrioritiesDirty();
      renderPriorities();
    });

    li.append(handle, moves, info, mode, weight, del);

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
      markPrioritiesDirty();
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
    // Seeded, then immediately re-derived by rerank() below. A new row landing
    // at a flat 0.5 while every other auto row is spaced by position made the
    // list look reordered when it was not: the row sat at the bottom but
    // outranked half the rows above it.
    weight: DEFAULT_ADD_WEIGHT,
    transmitter_uuid: null,
    mode: 'auto',
    satellite: name,
    transmitter_desc: '',
    transmitter_status: null,
  });
  rerank();
  markPrioritiesDirty();
  search.value = '';
  pendingAdd = null;
  hideSuggestions();
  search.focus();
  renderPriorities();
}

/* Move a row without a mouse.

   The HTML5 drag handlers below are the only reorder this panel had, which
   made the whole feature unreachable by keyboard and awkward on a touch
   screen. These arrow buttons and Alt+Arrow do the same job on the same
   array, so there is one reorder implementation and one rerank() call. */
function moveRow(from, to) {
  if (to < 0 || to >= priorities.length || from === to) return;
  const [moved] = priorities.splice(from, 1);
  priorities.splice(to, 0, moved);
  rerank();
  markPrioritiesDirty();
  // renderPriorities() restores focus to the same control on the same row by
  // NORAD, so the operator can press the arrow again immediately - no manual
  // refocus by position here, which would land on whichever row moved into
  // that slot instead.
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
    prioritiesDirty = false;
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
    const maxPerInput = document.getElementById('campaign-max-per-station');
    maxPerInput.value = cfg.campaign_max_per_station || 2;
    document.getElementById('campaign-max-per-station-value').textContent = maxPerInput.value;
    const autoBox = document.getElementById('campaign-auto-commit');
    autoBox.checked = !!cfg.campaign_auto_commit_enabled;
    document.getElementById('campaign-auto-warn').hidden = !autoBox.checked;
    paintCampaignReach();
    networkTokenSet = !!cfg.network_token_set;
    dbTokenSet = !!cfg.db_token_set;
    updateCampaignGate();
    renderAutoRun(cfg, { keepEdits: true });
    refreshRunGate();
  } catch (err) {
    console.error('[schedule] config', err);
  }
}

/* --- auto run -------------------------------------------------------------

   The timer that runs the station scheduler without anyone present. It ships
   disabled AND dry-run-only: two deliberate switches between a fresh install
   and anything booking unattended, mirroring campaign_auto_commit_enabled. */
function mountAutoRun() {
  document.getElementById('schedule-auto-enabled')
    .addEventListener('change', () => { autoDirty = true; paintAutoRun(); });
  document.getElementById('schedule-auto-mode-times')
    .addEventListener('click', () => setAutoMode('times'));
  document.getElementById('schedule-auto-mode-interval')
    .addEventListener('click', () => setAutoMode('interval'));
  document.getElementById('schedule-auto-dry')
    .addEventListener('click', () => setAutoBooking(true));
  document.getElementById('schedule-auto-book')
    .addEventListener('click', () => setAutoBooking(false));
  document.getElementById('schedule-auto-time-add')
    .addEventListener('click', addAutoTime);
  document.getElementById('schedule-auto-time-input')
    .addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); addAutoTime(); }
    });
  document.getElementById('schedule-auto-save')
    .addEventListener('click', saveAutoRun);
  for (const id of ['schedule-auto-interval', 'schedule-opt-hours',
                    'schedule-opt-culmination', 'schedule-opt-maxobs',
                    'schedule-opt-onlyprio']) {
    document.getElementById(id).addEventListener('input', () => { autoDirty = true; });
  }
  const toggle = document.getElementById('schedule-runopts-toggle');
  const box = document.getElementById('schedule-runopts');
  toggle.addEventListener('click', () => {
    const showing = box.hidden;
    box.hidden = !showing;
    toggle.setAttribute('aria-expanded', String(showing));
    toggle.textContent = `Run options ${showing ? '▴' : '▾'}`;
  });
}

function renderAutoRun(cfg, { keepEdits = false } = {}) {
  // loadConfig() runs on every panel open AND every time the ⚙ popover is
  // toggled. Blowing away unsaved auto-run edits on a popover toggle is a
  // silent data loss the operator has no way to anticipate, so a refresh
  // that is not an explicit reload leaves a dirty form alone.
  if (keepEdits && autoDirty) {
    autoRunCfg = { ...cfg, ...pendingAutoRunEdits() };
    paintAutoRun();
    return;
  }
  autoRunCfg = cfg;
  autoDirty = false;
  autoTimes = Array.isArray(cfg.auto_run_times) ? [...cfg.auto_run_times] : [];
  document.getElementById('schedule-auto-enabled').checked = !!cfg.auto_run_enabled;
  document.getElementById('schedule-auto-interval').value = cfg.auto_run_interval_min || 180;
  document.getElementById('schedule-opt-hours').value = cfg.schedule_hours ?? 24;
  document.getElementById('schedule-opt-culmination').value = cfg.min_culmination_deg ?? 3;
  document.getElementById('schedule-opt-maxobs').value = cfg.max_observation_minutes ?? 30;
  document.getElementById('schedule-opt-onlyprio').checked = cfg.only_priority !== false;
  paintAutoRun();
}

/* The parts of the form the operator has changed but not saved. */
function pendingAutoRunEdits() {
  return {
    auto_run_mode: autoRunCfg?.auto_run_mode,
    auto_run_dry_run: autoRunCfg?.auto_run_dry_run,
  };
}

function setAutoMode(mode) {
  if (!autoRunCfg) return;
  autoRunCfg = { ...autoRunCfg, auto_run_mode: mode };
  autoDirty = true;
  paintAutoRun();
}

function setAutoBooking(dryOnly) {
  if (!autoRunCfg) return;
  autoRunCfg = { ...autoRunCfg, auto_run_dry_run: dryOnly };
  autoDirty = true;
  paintAutoRun();
}

function paintAutoRun() {
  const cfg = autoRunCfg || {};
  const mode = cfg.auto_run_mode === 'interval' ? 'interval' : 'times';
  const timesTab = document.getElementById('schedule-auto-mode-times');
  const intervalTab = document.getElementById('schedule-auto-mode-interval');
  timesTab.classList.toggle('is-active', mode === 'times');
  intervalTab.classList.toggle('is-active', mode === 'interval');
  timesTab.setAttribute('aria-selected', String(mode === 'times'));
  intervalTab.setAttribute('aria-selected', String(mode === 'interval'));
  document.getElementById('schedule-auto-times-block').hidden = mode !== 'times';
  document.getElementById('schedule-auto-interval-block').hidden = mode !== 'interval';

  const dryOnly = cfg.auto_run_dry_run !== false;
  document.getElementById('schedule-auto-dry').classList.toggle('is-active', dryOnly);
  document.getElementById('schedule-auto-book').classList.toggle('is-active', !dryOnly);

  renderTimeChips();

  // The station publishes its own minimum culmination; -m REPLACES it rather
  // than adding to it, and only when the station has not marked that limit
  // hard. Saying so is the difference between a surprising empty run and an
  // understood one.
  const note = document.getElementById('schedule-runopts-note');
  const culmination = document.getElementById('schedule-opt-culmination').value;
  note.textContent =
    `Min culmination ${culmination}° replaces the station's own published minimum, `
    + 'not adds to it - so a lower value accepts grazing passes the station would '
    + 'normally skip. If the station marks that limit as hard, SatNOGS keeps the '
    + 'higher of the two and this value is ignored.';

  const status = document.getElementById('schedule-auto-status');
  if (!dryOnly && document.getElementById('schedule-auto-enabled').checked) {
    status.textContent = 'Unattended runs will BOOK real observations.';
    status.classList.add('sched-status-danger');
  } else {
    status.textContent = autoDirty ? 'unsaved changes' : '';
    status.classList.remove('sched-status-danger');
  }
}

function renderTimeChips() {
  const ul = document.getElementById('schedule-auto-times');
  ul.replaceChildren();
  if (!autoTimes.length) {
    const li = document.createElement('li');
    li.className = 'muted';
    li.textContent = 'no times set — auto run will never fire';
    ul.appendChild(li);
    return;
  }
  for (const time of autoTimes) {
    const li = document.createElement('li');
    li.className = 'sched-time-chip';
    const label = document.createElement('span');
    label.textContent = time;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'sched-chip-x';
    remove.textContent = '×';
    remove.setAttribute('aria-label', `Remove ${time}`);
    remove.addEventListener('click', () => {
      autoTimes = autoTimes.filter((t) => t !== time);
      autoDirty = true;
      paintAutoRun();
    });
    li.append(label, remove);
    ul.appendChild(li);
  }
}

function addAutoTime() {
  const input = document.getElementById('schedule-auto-time-input');
  const value = (input.value || '').trim();
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) return;
  if (!autoTimes.includes(value)) {
    autoTimes = [...autoTimes, value].sort();
    autoDirty = true;
  }
  input.value = '';
  paintAutoRun();
}

/* Blank means "unchanged", not zero. */
function numberOr(raw, fallback) {
  const text = String(raw ?? '').trim();
  if (text === '') return fallback;
  const value = Number(text);
  return Number.isFinite(value) ? value : fallback;
}

async function saveAutoRun() {
  const btn = document.getElementById('schedule-auto-save');
  const status = document.getElementById('schedule-auto-status');
  btn.disabled = true;
  btn.textContent = 'SAVING…';
  try {
    const cfg = await api.saveScheduleConfig({
      auto_run_enabled: document.getElementById('schedule-auto-enabled').checked,
      auto_run_mode: (autoRunCfg?.auto_run_mode === 'interval') ? 'interval' : 'times',
      auto_run_times: autoTimes,
      auto_run_interval_min: numberOr(
        document.getElementById('schedule-auto-interval').value,
        autoRunCfg?.auto_run_interval_min ?? 180,
      ),
      auto_run_dry_run: autoRunCfg?.auto_run_dry_run !== false,
      schedule_hours: numberOr(
        document.getElementById('schedule-opt-hours').value,
        autoRunCfg?.schedule_hours ?? 24,
      ),
      // Number("") is 0, and 0 is a legal minimum culmination, so an empty
      // box would silently save "accept every grazing pass" rather than
      // leaving the value alone.
      min_culmination_deg: numberOr(
        document.getElementById('schedule-opt-culmination').value,
        autoRunCfg?.min_culmination_deg ?? 3,
      ),
      max_observation_minutes: numberOr(
        document.getElementById('schedule-opt-maxobs').value,
        autoRunCfg?.max_observation_minutes ?? 30,
      ),
      only_priority: document.getElementById('schedule-opt-onlyprio').checked,
    });
    renderAutoRun(cfg);
    renderNextAutoRun(cfg.auto_run_next_utc);
    status.textContent = 'saved';
  } catch (err) {
    console.error('[schedule] auto run save', err);
    status.textContent = `could not save: ${err}`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'SAVE';
  }
}

/* NORAD -> the last run's complaint about it.

   These warnings are the only way the operator ever learns about the quiet
   failure mode of `-f`: a satellite whose pinned transmitter is not in this
   station's candidate set is simply never scheduled, and upstream says
   nothing at all. Surfacing it on the row is what turns "why do I never get
   this satellite" into something answerable. */
let runWarningsByNorad = new Map();

function noteRunWarnings(run) {
  runWarningsByNorad = new Map();
  for (const notice of run?.notices || []) {
    // The messages name their satellite as "NORAD 12345" or "12345 was not
    // considered: ..." - both produced by this backend, both start with the id.
    const match = /(?:NORAD\s+)?(\d{4,6})\b/.exec(notice.message || '');
    if (!match) continue;
    const norad = Number(match[1]);
    if (!runWarningsByNorad.has(norad)) runWarningsByNorad.set(norad, notice.message);
  }
  // The list may already be on screen from a parallel load.
  if (priorities.length) renderPriorities();
}

function renderNextAutoRun(iso) {
  const line = document.getElementById('schedule-auto-next');
  if (!iso) {
    line.hidden = true;
    return;
  }
  const when = new Date(iso);
  const mins = Math.round((when - Date.now()) / 60000);
  const rel = mins <= 0 ? 'due now'
    : mins < 60 ? `in ${mins} min`
    : `in ${Math.floor(mins / 60)} h ${mins % 60} m`;
  line.textContent = `Next auto run: ${shortDateTime(iso)} (${rel})`;
  line.hidden = false;
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
  // Switching lists reloads priorities from the new list, throwing away
  // anything typed into the current one. Guarded with the same two-step as
  // close(); the button that armed it is the list picker's own entry.
  if (prioritiesDirty) {
    const current = document.getElementById('schedule-list-current');
    let proceed = false;
    confirmDiscard(current, () => { proceed = true; });
    if (!proceed) return;
  }

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
  document.getElementById('campaign-oneclick-btn').addEventListener('click', runCampaignOneClick);
  document.getElementById('campaign-auto-commit').addEventListener('change', saveCampaignAutoCommit);
  document.getElementById('campaign-cfg-save').addEventListener('click', saveCampaignConfig);
  document.getElementById('campaign-verify-btn').addEventListener('click', verifyCampaign);
  document.getElementById('campaign-max-per-station').addEventListener('input', (e) => {
    document.getElementById('campaign-max-per-station-value').textContent = e.target.value;
    campaignConfigDirty = true;
    paintCampaignReach();
  });
  document.getElementById('campaign-max-total').addEventListener('input', (e) => {
    document.getElementById('campaign-max-total-value').textContent = e.target.value;
    paintCampaignReach();
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
  const perInput = document.getElementById('campaign-max-per-station');
  const val = input.value.trim();
  const perVal = perInput.value.trim();
  btn.disabled = true;
  status.textContent = 'saving…';
  try {
    await api.saveScheduleConfig({
      campaign_max_total: val ? parseInt(val, 10) : 0,
      campaign_max_per_station: perVal ? parseInt(perVal, 10) : 2,
    });
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
    // The station ID is its own column, not a fallback for a missing name:
    // it is the identifier network.satnogs.org itself uses, so it is what an
    // operator reconciles this table against.
    thead.innerHTML = '<tr><th>ID</th><th>Station</th><th>Start UTC</th>'
      + '<th>End UTC</th><th>Max El</th></tr>';
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    // Every candidate is shown, not just the first N - CONFIRM & SUBMIT
    // books real time on other people's stations, so truncating the review
    // list would let some of what gets booked go unseen.
    for (const item of items) {
      const tr = document.createElement('tr');
      for (const text of [
        String(item.station_id),
        item.station_name || '—',
        shortDateTime(item.start, 'UTC'),
        shortDateTime(item.end, 'UTC'),
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
  // Stations the run could actually use: those it booked, plus those it looked
  // at and rejected only because the budget ran out. Stations skipped for
  // antenna range or no pass are NOT usable and must not inflate the ceiling.
  const budgetSkipped = (preview.skipped || []).filter(
    (sk) => typeof sk.reason === 'string' && sk.reason.startsWith('had a free pass'),
  ).length;
  lastPreviewCapableStations = new Set(items.map((it) => it.station_id)).size + budgetSkipped;
  paintCampaignReach();
  campaignConfigDirty = false;
  document.getElementById('campaign-commit-btn').hidden = items.length === 0;
}

/* The read-back. One live call per station in the last run's accepted set,
   so this answers in seconds where a full campaign computation takes
   minutes — but it is still real network I/O, hence the disabled button. */
async function verifyCampaign() {
  const btn = document.getElementById('campaign-verify-btn');
  const box = document.getElementById('campaign-verify-result');
  btn.disabled = true;
  btn.textContent = 'CHECKING…';
  box.replaceChildren(note('reading each station\'s calendar back from SatNOGS…'));
  try {
    renderCampaignVerify(await api.verifyCampaign());
  } catch (err) {
    console.error('[schedule] campaign verify', err);
    box.replaceChildren(note(`could not cross-check: ${err}`));
  } finally {
    btn.disabled = false;
    btn.textContent = 'CROSS-CHECK';
  }
}

const VERIFY_STATES = {
  on_schedule: { label: 'ON SCHEDULE', cls: 'ok' },
  missing:     { label: 'NOT FOUND',   cls: 'bad' },
  started:     { label: 'UNDER WAY',   cls: '' },
  unknown:     { label: 'UNKNOWN',     cls: 'warn' },
};

function renderCampaignVerify(result) {
  const box = document.getElementById('campaign-verify-result');
  box.replaceChildren();

  if (result.status === 'nothing_to_check') {
    box.appendChild(note('the last run booked nothing, so there is nothing to cross-check.'));
    return;
  }

  const items = result.items || [];
  const counts = items.reduce((acc, it) => {
    acc[it.state] = (acc[it.state] || 0) + 1;
    return acc;
  }, {});

  const meta = document.createElement('p');
  meta.className = 'muted sched-meta';
  meta.textContent = `Checked ${items.length} booking(s) across `
    + `${result.stations_checked} station(s) · `
    + `${counts.on_schedule || 0} confirmed on schedule`
    + (counts.missing ? `, ${counts.missing} not found` : '');
  box.appendChild(meta);

  // "Not found" is the one an operator has to act on, so it is called out
  // rather than left to be spotted among the confirmed rows.
  if (counts.missing) {
    const warn = document.createElement('p');
    warn.className = 'sched-notice error';
    warn.textContent = `${counts.missing} booking(s) the API accepted are not on the `
      + 'station calendar now. They may have been cancelled by the station owner, '
      + 'or superseded by another observation.';
    box.appendChild(warn);
  }

  if (!items.length) return;

  const table = document.createElement('table');
  table.className = 'sched-table';
  const thead = document.createElement('thead');
  thead.innerHTML = '<tr><th>ID</th><th>Station</th><th>Start UTC</th><th>End UTC</th>'
    + '<th>On schedule</th></tr>';
  table.appendChild(thead);
  const tbody = document.createElement('tbody');
  for (const item of items) {
    const tr = document.createElement('tr');
    for (const text of [
      String(item.station_id),
      item.station_name || '—',
      shortDateTime(item.start, 'UTC'),
      shortDateTime(item.end, 'UTC'),
    ]) {
      const td = document.createElement('td');
      td.textContent = text;
      tr.appendChild(td);
    }
    const state = VERIFY_STATES[item.state] || VERIFY_STATES.unknown;
    const tdState = document.createElement('td');
    const tag = document.createElement('span');
    tag.className = `sched-verify-state ${state.cls}`.trim();
    tag.textContent = state.label;
    if (item.detail) tag.title = item.detail;
    tdState.appendChild(tag);
    tr.appendChild(tdState);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  box.appendChild(scrollableTable(
    table, `Cross-check of the last run, ${items.length} row(s)`));

  announceCampaign(`Cross-check complete: ${counts.on_schedule || 0} of ${items.length} `
    + 'booking(s) confirmed on the stations\' schedules.');
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
  await submitPreviewedItems();
}

/* The submit half of CONFIRM & SUBMIT, with no prompt of its own.

   Split out so the one-click path can reuse it verbatim instead of growing a
   second copy of the poll-and-report logic. The prompt lives in the caller,
   because the two callers ask at different moments: the two-step flow asks
   after the operator has read the plan, one-click asks before it exists. */
async function submitPreviewedItems() {
  if (!campaignPreviewItems || !campaignPreviewItems.length) return;
  const btn = document.getElementById('campaign-commit-btn');
  const box = document.getElementById('campaign-preview-result');

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

/* The arithmetic the two sliders imply, shown next to them.

   Raising "max bookings / run" on its own does nothing once it exceeds
   (stations that can hear the transmitter) x (passes / station) - the plan
   just stops at the lower number. That ceiling is invisible in the UI, and
   without it an operator who wants 600 reasonably assumes the total slider is
   the control for that. It is not; passes/station is. */
function paintCampaignReach() {
  const hint = document.getElementById('campaign-reach-hint');
  if (!hint) return;
  const total = parseInt(document.getElementById('campaign-max-total').value, 10) || 0;
  const per = parseInt(document.getElementById('campaign-max-per-station').value, 10) || 1;
  const stationsNeeded = Math.ceil(total / per);
  // considered_stations from the last preview is the only real measurement of
  // the network we have here; before one exists we can only state the demand.
  const reach = lastPreviewCapableStations;
  let text = `${total} bookings at ${per} per station needs ${stationsNeeded} station(s).`;
  if (reach != null) {
    const ceiling = reach * per;
    text += ceiling < total
      ? ` Last preview found ${reach} usable — so this run tops out at ${ceiling}.`
      : ` Last preview found ${reach} usable, enough for this.`;
  }
  hint.textContent = text;
}

/* One press: compute the plan, then submit it.

   The two-step PREVIEW / CONFIRM flow is still there and is still the right
   one when the plan needs reading. This exists because the preview takes
   minutes - it walks every candidate station's calendar - and requiring
   someone to come back afterwards to press a second button is the actual
   friction, not the clicking.

   It still asks once, and it asks BEFORE the wait rather than after, naming
   the two numbers that bound what can happen. That ordering is deliberate: a
   prompt at the end, minutes later, is one an operator has stopped paying
   attention to. What it cannot name is the exact count, because that does not
   exist until the preview has run - so it names the ceiling instead and the
   result is reported when it lands. */
async function runCampaignOneClick() {
  const btn = document.getElementById('campaign-oneclick-btn');
  const box = document.getElementById('campaign-preview-result');
  const total = parseInt(document.getElementById('campaign-max-total').value, 10) || 0;
  const per = parseInt(document.getElementById('campaign-max-per-station').value, 10) || 1;

  const ok = window.confirm(
    `Book up to ${total} observation(s), at most ${per} per station, on community `
    + 'stations via SatNOGS Network?\n\n'
    + 'This computes the plan and then SUBMITS IT AUTOMATICALLY — you will not be '
    + 'asked again. These are other operators\' ground stations, and accepted '
    + 'bookings cannot be undone from this dashboard.\n\n'
    + 'Use PREVIEW instead if you want to read the plan first.',
  );
  if (!ok) return;

  btn.disabled = true;
  btn.textContent = 'PLANNING…';
  try {
    await runCampaignPreview();
    if (!campaignPreviewItems || !campaignPreviewItems.length) {
      // A preview that found nothing is a normal outcome, not a failure, and
      // submitting it would be a no-op that still writes a run record.
      box.prepend(note('Nothing to book — the preview found no free passes. Nothing was submitted.'));
      announceCampaign('One-click finished: nothing to book.');
      return;
    }
    const stations = new Set(campaignPreviewItems.map((it) => it.station_id)).size;
    btn.textContent = `SUBMITTING ${campaignPreviewItems.length}…`;
    announceCampaign(
      `Plan ready: ${campaignPreviewItems.length} booking(s) on ${stations} station(s). Submitting.`);
    await submitPreviewedItems();
  } catch (err) {
    console.error('[schedule] one-click campaign', err);
    box.prepend(alertNote(`One-click run failed: ${err}`, 'error'));
    announceCampaign(`One-click run failed: ${err}`);
  } finally {
    btn.disabled = false;
    btn.textContent = 'BOOK WORLDWIDE — ONE CLICK';
  }
}

/* The unattended switch. Saved immediately rather than behind the SAVE button
   next to the sliders, because a checkbox that looks set but is not saved is
   exactly the wrong failure mode for "book without asking" - and because the
   two belong to different decisions. */
async function saveCampaignAutoCommit() {
  const box = document.getElementById('campaign-auto-commit');
  const status = document.getElementById('campaign-auto-status');
  const warn = document.getElementById('campaign-auto-warn');
  const want = box.checked;

  if (want) {
    const ok = window.confirm(
      'Let the campaign book automatically, with nobody watching?\n\n'
      + 'Every cycle will submit real bookings to other operators\' stations. No '
      + 'plan is shown to anyone first, and nothing asks for confirmation.',
    );
    if (!ok) { box.checked = false; return; }
  }

  box.disabled = true;
  status.textContent = 'saving…';
  try {
    await api.saveScheduleConfig({ campaign_auto_commit_enabled: want });
    status.textContent = want ? 'automatic booking ON' : 'automatic booking off';
    warn.hidden = !want;
  } catch (err) {
    // Put the control back to what the backend still believes, so the UI never
    // claims a setting that did not land.
    box.checked = !want;
    warn.hidden = !box.checked;
    status.textContent = `failed: ${err}`;
  } finally {
    box.disabled = false;
    setTimeout(() => { status.textContent = ''; }, 4000);
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
