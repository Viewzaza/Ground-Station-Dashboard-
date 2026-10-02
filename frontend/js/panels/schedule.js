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

// State RUN NOW needs. It is disabled until BOTH SatNOGS tokens are set,
// because satnogs-auto-scheduler validates its whole configuration first.
let dbTokenSet = false;
let armedRealRun = false;      // RUN NOW's two-step confirm
let armedRealRunTimer = null;
let autoRunCfg = null;         // the config as last loaded/saved
let autoTimes = [];            // HH:MM chips, the editable copy
let autoDirty = false;
// Network Campaign walks every candidate station's booking history one at a
// time to stay polite to SatNOGS's rate limit - against real data (hundreds
// of stations) that has taken several minutes in testing. A short timeout
// would give up on a still-healthy real run and misreport it as unresponsive.
const CAMPAIGN_POLL_TIMEOUT_MS = 20 * 60_000;
// A looped submit recomputes the whole campaign between batches, so each extra
// round can cost as long as one full preview.
const CAMPAIGN_LOOP_POLL_TIMEOUT_MS = 90 * 60_000;

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
let campaignLoop = false;          // campaign_loop_until_exhausted, as last loaded/saved
// campaign_transmitter_policy, as last loaded/saved. 'pinned' until a config
// says otherwise: a backend that predates the setting pins the telemetry, and
// the prompts must not promise a fallback it will not use.
let campaignPolicy = 'pinned';
let campaignConfigDirty = false;   // invalidates a stale preview if config changes after it
let lastPreviewCapableStations = null;  // stations the last preview found usable, for the reach hint
let lastPreviewStoppedEarly = null;     // the last preview's stopped_early, or null if it was complete
let lastPreviewParams = null;           // the inputs the last preview was built from (preview.params)
let lastPreviewAt = null;               // generated_utc (ms) of the preview the three above describe
// One-click and MAX COVERAGE share one in-flight flag. updateCampaignGate()
// re-derives both buttons from the token alone, and it runs on every
// max-total slider move and every loadConfig() - so without this, touching a
// slider or opening ⚙ mid-run re-enabled a button whose run was still going.
let campaignOneClickBusy = false;

export function mountSchedule() {
  document.getElementById('schedule-toggle').addEventListener('click', open);
  document.getElementById('schedule-close').addEventListener('click', close);
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
    loadCampaignLastRun(), loadCampaignHistory(), loadCampaignPreviewStats(),
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
    // "selected", not "booked": the badge above is the authority on what
    // actually landed on the calendar.
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
    `${run.booked_state === 'confirmed' ? 'Booked' : 'Planned'} observations, `
    + `${run.observations.length} row(s)`));

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

/* How this run ended, said before anything else: "7 observations" means
   different things depending on whether they are on the calendar. */
function runBadge(run) {
  const badge = document.createElement('span');
  badge.className = 'sched-run-trigger';
  if (run.trigger === 'auto') badge.classList.add('manual');

  if (run.status === 'error') {
    badge.classList.add('danger');
    badge.textContent = run.booked_state === 'unconfirmed'
      ? 'RUN INTERRUPTED · CHECK SATNOGS' : 'RUN FAILED';
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
      'This runs satnogs-auto-scheduler for the station now and books real '
      + 'observations for every pass it selects. Click again within 5s to confirm.',
    );
    clearTimeout(armedRealRunTimer);
    armedRealRunTimer = setTimeout(disarmRealRun, 5000);
    return;
  }
  disarmRealRun();
  startRun();
}

function disarmRealRun() {
  clearTimeout(armedRealRunTimer);
  armedRealRun = false;
  const btn = document.getElementById('schedule-run');
  btn.classList.remove('is-armed');
  btn.textContent = 'RUN NOW';
  refreshRunGate();
}

async function startRun() {
  const btn = document.getElementById('schedule-run');
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'BOOKING…';
  try {
    const started = await api.runSchedule();
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

/* RUN NOW is gated on BOTH tokens, and the hint says which one is missing -
   otherwise a disabled button reads as a bug. */
function refreshRunGate() {
  const real = document.getElementById('schedule-run');
  const ready = dbTokenSet && networkTokenSet;
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
    + 'satnogs-auto-scheduler needs both of them to run.',
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
    campaignLoop = !!cfg.campaign_loop_until_exhausted;
    document.getElementById('campaign-loop').checked = campaignLoop;
    const maxPerInput = document.getElementById('campaign-max-per-station');
    maxPerInput.value = cfg.campaign_max_per_station || 2;
    document.getElementById('campaign-max-per-station-value').textContent = maxPerInput.value;
    campaignPolicy = CAMPAIGN_POLICY_TEXT[cfg.campaign_transmitter_policy]
      ? cfg.campaign_transmitter_policy : 'pinned';
    document.getElementById('campaign-tx-policy').value = campaignPolicy;
    // Every campaign control now shows the SAVED value, so nothing on screen
    // is unsaved any more. This used to be cleared by renderCampaignPreview
    // instead, which let a finished preview wipe the guard while the sliders
    // still held unsaved moves - one-click would then prompt with numbers the
    // backend was not going to use. Cleared before updateCampaignGate() below
    // on purpose: the controls are back on the saved values a preview is built
    // from, so a shown preview is no staler than it was, and CONFIRM still
    // sends exactly the rows it shows.
    campaignConfigDirty = false;
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
   disabled, and once enabled every run it fires books real observations -
   there is no dry-run-only mode. */
function mountAutoRun() {
  document.getElementById('schedule-auto-enabled')
    .addEventListener('change', () => { autoDirty = true; paintAutoRun(); });
  document.getElementById('schedule-auto-mode-times')
    .addEventListener('click', () => setAutoMode('times'));
  document.getElementById('schedule-auto-mode-interval')
    .addEventListener('click', () => setAutoMode('interval'));
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
  };
}

function setAutoMode(mode) {
  if (!autoRunCfg) return;
  autoRunCfg = { ...autoRunCfg, auto_run_mode: mode };
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
  if (document.getElementById('schedule-auto-enabled').checked) {
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
    // The messages name their satellite as "NORAD 12345 ..." - produced by
    // this backend, and the id is the first number in them.
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

/* What each downlink policy books, in the words the consent prompts use. Also
   the set of values this UI accepts from the config: anything else is treated
   as "unknown" rather than guessed at. The digipeater's weaker record is said
   out loud because it is the price of the extra stations - an operator who
   picks "most stations" should know some of them are the less productive kind. */
const CAMPAIGN_POLICY_TEXT = {
  preferred: 'telemetry (400.630 MHz) first, and the 145.825 MHz digipeater on '
    + 'stations that cannot hear telemetry (the digipeater has a much lower '
    + 'historical success rate)',
  pinned: 'telemetry only (400.630 MHz)',
  any: 'any KNACKSAT-2 downlink each station can hear, picked per station',
};
// How far a policy reaches, for "did the backend plan wider than agreed?".
const CAMPAIGN_POLICY_RANK = { pinned: 0, preferred: 1, any: 2 };

/* MAX COVERAGE's settings. 3 per station x ~222 reachable stations is ~651
   possible bookings (offline measurement), so 600 is what one build can
   actually fill rather than a number the network cannot reach; loop tops up
   whatever a round leaves. Frozen: the prompt quotes these and the post-preview
   holds compare against them, so nothing may change them in between. */
const MAX_COVERAGE = Object.freeze({ policy: 'preferred', per: 3, total: 600, loop: true });

/* The policy a preview was actually built with, read back from its params
   rather than from our own select. null when the preview predates params (an
   old one on disk) - "unknown", never a guess. A 'preferred' config whose
   fallback list is empty reads back as 'pinned', which is what it did. */
function policyFromParams(params) {
  if (!params || !('transmitter_uuid' in params)) return null;
  if (!params.transmitter_uuid) return 'any';
  const fallbacks = params.fallback_transmitter_uuids;
  return Array.isArray(fallbacks) && fallbacks.length ? 'preferred' : 'pinned';
}

/* A short, scannable name for a transmitter: "telemetry 400.630",
   "digipeater 145.825 (fallback)".

   The payload carries each transmitter's SatNOGS DB description - for
   KNACKSAT-2 "Mode U - FSK9k6 - AX.25 G3RUH -TLM" and "Mode V/V - FSK9k6 -
   Digipeater - AX.25 G3RUH" - which names the kind but not the frequency. The
   frequencies are looked up by uuid instead: this tab only ever targets
   KNACKSAT-2 (see its Target line), so its two uuids are the whole set.
   Anything unrecognised degrades to its description, then to a uuid prefix,
   never to a blank cell. */
const KNOWN_TX_MHZ = {
  UatCXtfDnoBPeVBGHgj4Bc: '400.630',
  JR28wAEjmpuDQ4FrPWAiwf: '145.825',
};

function txKind(description) {
  const desc = String(description || '');
  if (/digipeat/i.test(desc)) return 'digipeater';
  if (/\bTLM\b|telemetry/i.test(desc)) return 'telemetry';
  return '';
}

function txShortLabel(uuid, description, fallback = false) {
  const desc = String(description || '').trim();
  const kind = txKind(desc);
  const mhz = KNOWN_TX_MHZ[uuid] || '';
  let label = kind ? [kind, mhz].filter(Boolean).join(' ') : (mhz ? `${mhz} MHz` : '');
  if (!label) {
    if (desc) label = desc.length > 28 ? `${desc.slice(0, 28)}…` : desc;
    else label = uuid ? `${String(uuid).slice(0, 6)}…` : 'unknown';
  }
  return fallback ? `${label} (fallback)` : label;
}

/* " (K on the digipeater fallback)", for the sentences that say how many
   bookings a plan holds. Empty for a payload that predates per-item
   `fallback`: "0 on the fallback" would claim something it never measured. */
function fallbackPhrase(items) {
  if (!items.some((it) => typeof it.fallback === 'boolean')) return '';
  const fb = items.filter((it) => it.fallback);
  if (!fb.length) return ' (none on a fallback downlink)';
  const kinds = new Set(fb.map((it) => txKind(it.transmitter_description)));
  const kind = kinds.size === 1 ? [...kinds][0] : '';
  return ` (${fb.length} on ${kind ? `the ${kind} fallback` : 'a fallback downlink'})`;
}

/* "jobs 220 / observations 2": which read path served each calendar. jobs is
   the unthrottled anonymous feed; observations is the budgeted fallback, so a
   large second number is the early sign of a run heading for the read limit. */
function formatCalendarSources(sources) {
  if (!sources || typeof sources !== 'object') return '';
  const order = ['jobs', 'observations'];
  const rank = (k) => (order.includes(k) ? order.indexOf(k) : order.length);
  return Object.keys(sources)
    .filter((k) => Number(sources[k]) > 0)
    .sort((a, b) => rank(a) - rank(b))
    .map((k) => `${k} ${sources[k]}`)
    .join(' / ');
}

/* One labelled row of compact count chips (elevation bands, transmitters).
   Chips rather than a sentence: six band counts in prose is unreadable, and
   the point of the row is to see at a glance which band is thin. */
function chipRow(label, chips) {
  const row = document.createElement('p');
  row.className = 'sched-chip-row';
  const lead = document.createElement('span');
  lead.className = 'sched-chip-row-label';
  lead.textContent = label;
  row.appendChild(lead);
  for (const { text, cls, title } of chips) {
    const chip = document.createElement('span');
    chip.className = `sched-count-chip${cls ? ` ${cls}` : ''}`;
    chip.textContent = text;
    if (title) chip.title = title;
    row.appendChild(chip);
  }
  return row;
}

function bandChips(bandCounts) {
  return bandCounts.map((b) => ({
    text: `${b.band}° ${b.count}`,
    cls: b.count ? '' : 'zero',
    title: `${b.count} booking(s) peaking at ${b.band}° elevation`,
  }));
}

function transmitterChips(rows) {
  return rows.map((t) => ({
    text: `${txShortLabel(t.uuid, t.description, t.fallback)} · `
      + `${t.bookings ?? 0} on ${t.stations ?? 0} station(s)`,
    cls: t.fallback ? 'fallback' : '',
    title: t.description || t.uuid || '',
  }));
}

function mountCampaign() {
  document.getElementById('campaign-preview-btn').addEventListener('click', runCampaignPreview);
  document.getElementById('campaign-commit-btn').addEventListener('click', confirmCampaign);
  // Wrapped, not passed directly: runCampaignOneClick takes an options object
  // now, and a bare listener would hand it the click event as those options.
  document.getElementById('campaign-oneclick-btn')
    .addEventListener('click', () => runCampaignOneClick());
  document.getElementById('campaign-max-coverage').addEventListener('click', runCampaignMaxCoverage);
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
  document.getElementById('campaign-loop').addEventListener('change', () => {
    campaignConfigDirty = true;
  });
  // A different policy is a different plan - other stations, other
  // transmitters - so a shown preview no longer describes what the saved
  // setting would book, and CONFIRM must not stay offered on it.
  document.getElementById('campaign-tx-policy').addEventListener('change', () => {
    campaignConfigDirty = true;
    paintCampaignReach();
    updateCampaignGate();
  });
}

function updateCampaignGate() {
  const hint = document.getElementById('campaign-token-hint');
  const previewBtn = document.getElementById('campaign-preview-btn');
  hint.hidden = networkTokenSet;
  previewBtn.disabled = !networkTokenSet;
  // One-click previews and then books, so it needs the token at least as much
  // as PREVIEW does. It was left enabled, and would fail minutes into a run.
  setOneClickButtonsDisabled(!networkTokenSet || campaignOneClickBusy);
  if (campaignConfigDirty) invalidateCampaignPreview();
}

function setOneClickButtonsDisabled(disabled) {
  for (const id of ['campaign-oneclick-btn', 'campaign-max-coverage']) {
    const b = document.getElementById(id);
    if (b) b.disabled = disabled;
  }
}

function invalidateCampaignPreview() {
  campaignPreviewItems = null;
  document.getElementById('campaign-commit-btn').hidden = true;
}

/* The campaign settings exactly as SAVE would send them. A blank total is 0,
   the backend's "clear back to the default" sentinel; a blank per-station
   falls back to 2, as it always has. */
function readCampaignControls() {
  const val = document.getElementById('campaign-max-total').value.trim();
  const perVal = document.getElementById('campaign-max-per-station').value.trim();
  return {
    total: val ? parseInt(val, 10) : 0,
    per: perVal ? parseInt(perVal, 10) : 2,
    loop: document.getElementById('campaign-loop').checked,
    policy: document.getElementById('campaign-tx-policy').value,
  };
}

/* Returns whether the save landed, so MAX COVERAGE can refuse to go on with
   settings the backend never accepted. The SAVE button ignores the result -
   its status line already says. */
async function saveCampaignConfig() {
  const btn = document.getElementById('campaign-cfg-save');
  const status = document.getElementById('campaign-cfg-status');
  const sent = readCampaignControls();
  btn.disabled = true;
  status.textContent = 'saving…';
  try {
    await api.saveScheduleConfig({
      campaign_max_total: sent.total,
      campaign_max_per_station: sent.per,
      campaign_loop_until_exhausted: sent.loop,
      campaign_transmitter_policy: sent.policy,
    });
    campaignLoop = sent.loop;
    campaignPolicy = sent.policy;
    // Only "clean" if the controls still show what was sent. A slider moved
    // while the request was in flight is unsaved, and clearing the flag
    // regardless would let one-click - or MAX COVERAGE, which hands straight
    // over after this save - prompt with numbers the backend never got.
    const now = readCampaignControls();
    if (Object.keys(sent).every((k) => sent[k] === now[k])) campaignConfigDirty = false;
    status.textContent = 'saved';
    return { ok: true };
  } catch (err) {
    status.textContent = `failed: ${err}`;
    return { ok: false, error: String(err) };
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
      return 'busy';
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
      return 'timeout';
    }
    return 'ready';
  } catch (err) {
    console.error('[schedule] campaign preview', err);
    box.replaceChildren(alertNote(`Could not compute preview: ${err}`, 'error'));
    announceCampaign(`Preview failed: ${err}`);
    return 'error';
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

  // How far this plan reached and what it cost to find out. Every field is
  // optional: a preview written by an older backend has none of them, and a
  // missing count is left out rather than printed as a confident 0.
  const stats = [];
  if (Number.isFinite(preview.stations_reachable)) {
    stats.push(`${preview.stations_reachable} station(s) reachable`);
  }
  if (Number.isFinite(preview.stations_booked)) {
    stats.push(`${preview.stations_booked} in the plan`);
  }
  if (Number.isFinite(preview.calendars_read) || Number.isFinite(preview.calendars_cached)) {
    const sources = formatCalendarSources(preview.calendar_sources);
    stats.push(`calendars: ${preview.calendars_read ?? 0} read`
      + (sources ? ` (${sources})` : '')
      + (Number.isFinite(preview.calendars_cached) ? `, ${preview.calendars_cached} cached` : ''));
  }
  if (stats.length) {
    const line = document.createElement('p');
    line.className = 'muted sched-meta';
    line.textContent = stats.join(' · ');
    box.appendChild(line);
  }
  if (Array.isArray(preview.band_counts) && preview.band_counts.length) {
    box.appendChild(chipRow('Peak elevation', bandChips(preview.band_counts)));
  }
  if (Array.isArray(preview.transmitters) && preview.transmitters.length) {
    box.appendChild(chipRow('Downlink', transmitterChips(preview.transmitters)));
  }

  // Spelled out immediately above the table the operator is about to approve:
  // nothing here is booked yet, and this is exactly what CONFIRM would send.
  if (items.length) {
    const standby = document.createElement('p');
    standby.className = 'sched-preview-standby';
    standby.textContent = `Nothing has been submitted. CONFIRM & SUBMIT would book `
      + `${items.length} observation(s) across ${stationCount} community station(s)`
      + `${fallbackPhrase(items)}.`;
    box.appendChild(standby);
  }

  if (items.length) {
    const table = document.createElement('table');
    table.className = 'sched-table';
    const thead = document.createElement('thead');
    // The station ID is its own column, not a fallback for a missing name:
    // it is the identifier network.satnogs.org itself uses, so it is what an
    // operator reconciles this table against. Downlink is per row because
    // the two KNACKSAT-2 transmitters are not equally productive, and which
    // stations are getting the weaker one is part of what is being approved.
    thead.innerHTML = '<tr><th>ID</th><th>Station</th><th>Start UTC</th>'
      + '<th>End UTC</th><th>Max El</th><th>Downlink</th></tr>';
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
      const tdTx = document.createElement('td');
      tdTx.className = 'sched-tx-cell';
      tdTx.textContent = txShortLabel(
        item.transmitter_uuid, item.transmitter_description, !!item.fallback);
      if (item.transmitter_description) tdTx.title = item.transmitter_description;
      tr.appendChild(tdTx);
      if (item.fallback) tr.classList.add('is-fallback');
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
    ? `Preview ready: ${items.length} observation(s) across ${stationCount} station(s)`
      + `${fallbackPhrase(items)} would be booked. Nothing submitted yet.`
    : 'Preview ready: no observations would be booked.');

  // What CONFIRM actually submits — exactly what was just shown, not a
  // recompute at click time, so what's confirmed is what was reviewed.
  campaignPreviewItems = items;
  lastPreviewStoppedEarly = preview.stopped_early || null;
  lastPreviewParams = preview.params || null;
  const renderedAt = Date.parse(preview.generated_utc);
  lastPreviewAt = Number.isFinite(renderedAt) ? renderedAt : lastPreviewAt;
  if (lastPreviewStoppedEarly) {
    // Above the table, not in the collapsed report: a plan cut short by the
    // read limit looks exactly like a small network otherwise, and it is the
    // fact that changes what the operator should do next.
    const se = lastPreviewStoppedEarly;
    box.prepend(alertNote(
      `Incomplete plan — SatNOGS's read limit stopped this preview at station `
      + `${se.station_id}${se.station_name ? ` (${se.station_name})` : ''}, with `
      + `${se.unread_stations ?? 'some'} candidate station(s) never read. The plan `
      + 'below covers only the stations it reached. Adding a Network token raises '
      + 'the limit; otherwise try again in an hour.'));
  }
  lastPreviewCapableStations = previewCapableStations(preview, items);
  paintCampaignReach();
  // campaignConfigDirty is deliberately NOT cleared here. A preview is built
  // from the SAVED config, so finishing one says nothing about sliders moved
  // and never saved - clearing it let one-click prompt with those unsaved
  // numbers. Only a save or a load (which rewrites the controls) clears it.
  document.getElementById('campaign-commit-btn').hidden = items.length === 0;
}

/* Stations the run could use, for the reach hint and the consent prompts.

   The backend now counts this itself (stations_reachable: a campaign
   transmitter in range and at least one qualifying pass). The fallback below
   is the old inference from skip-reason prefixes - those it booked, plus those
   it had in hand but left out only because the booking budget ran out first;
   stations skipped for antenna range, no pass, conflicts, or never read are
   NOT evidence of capacity - kept only for previews written before the field
   existed. */
function previewCapableStations(preview, items) {
  if (Number.isFinite(preview.stations_reachable)) return preview.stations_reachable;
  const budgetSkipped = (preview.skipped || []).filter(
    (sk) => typeof sk.reason === 'string'
      && (sk.reason.startsWith('had a free pass') || sk.reason.startsWith('not reached')),
  ).length;
  return new Set(items.map((it) => it.station_id)).size + budgetSkipped;
}

/* Seeds the reach hint and the consent prompts' station count from the last
   preview on disk (the auto timer writes one every cycle), so a fresh page can
   say "up to about N stations" before anyone runs PREVIEW. Numbers only: its
   rows are never shown or made submittable from here - CONFIRM only ever
   sends a preview rendered in this session. Only a file NEWER than what this
   page already knows is taken (the timer may have run since), so it can never
   roll a just-rendered preview's numbers back. Previews without
   stations_reachable are ignored: the old inference needs the whole skip
   list to be trustworthy, and a stale file is no place to start guessing. */
async function loadCampaignPreviewStats() {
  try {
    const preview = await api.campaignPreview();
    if (preview?.status !== 'ok' || !Number.isFinite(preview.stations_reachable)) return;
    const at = Date.parse(preview.generated_utc);
    if (!Number.isFinite(at)) return;
    if (lastPreviewAt !== null && at <= lastPreviewAt) return;
    lastPreviewAt = at;
    lastPreviewCapableStations = preview.stations_reachable;
    lastPreviewStoppedEarly = preview.stopped_early || null;
    lastPreviewParams = preview.params || null;
    paintCampaignReach();
  } catch (err) {
    console.error('[schedule] campaign preview stats', err);
  }
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
    + `station(s)${fallbackPhrase(campaignPreviewItems)} via SatNOGS Network?\n\n`
    + (campaignLoop
      ? 'Loop is ON: after this batch the campaign is recomputed and the next '
        + 'batch submitted, again and again until no bookings are left — the '
        + 'total can be well above this number. Later batches use the saved '
        + `downlink setting: ${CAMPAIGN_POLICY_TEXT[campaignPolicy]}.\n\n`
      : '')
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
    const timeoutMs = campaignLoop ? CAMPAIGN_LOOP_POLL_TIMEOUT_MS : CAMPAIGN_POLL_TIMEOUT_MS;
    const deadline = Date.now() + timeoutMs;
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
        `Still submitting after ${timeoutMs / 60_000} minutes — some bookings may already have been `
        + 'accepted. Do not resubmit: reopen this panel later to check the result '
        + 'and history first.'));
      announceCampaign(`Still submitting after ${timeoutMs / 60_000} minutes. Check history before resubmitting.`);
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
  // The last preview's stations_reachable is the only real measurement of the
  // network we have here; before one exists we can only state the demand.
  const reach = lastPreviewCapableStations;
  let text = `${total} bookings at ${per} per station needs ${stationsNeeded} station(s).`;
  // Reach is a property of the downlink setting as much as of the network:
  // telemetry-only reaches ~145 stations where the digipeater fallback reaches
  // ~222. Quoting one setting's count against the other would tell an
  // operator who just switched to the fallback that 600 is out of reach.
  const previewPolicy = policyFromParams(lastPreviewParams);
  const shownPolicy = document.getElementById('campaign-tx-policy')?.value;
  if (reach != null && previewPolicy && shownPolicy && previewPolicy !== shownPolicy) {
    text += ` The last preview used a different downlink setting (${reach} usable `
      + 'there), so it cannot say how far this one reaches — run PREVIEW.';
    hint.textContent = text;
    return;
  }
  if (lastPreviewStoppedEarly) {
    // The last preview never finished reading, so its count describes the read
    // budget, not the network. Blaming the network here sends the operator to
    // raise passes/station - depth on the same stations - when breadth is what
    // was actually missing.
    text += ' The last preview was cut short by the read limit, so it cannot '
      + 'say how many stations are usable.';
    hint.textContent = text;
    return;
  }
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
   result is reported when it lands.

   `preset` is MAX COVERAGE handing over: it has already asked, with these
   exact numbers, and saved them (see runCampaignMaxCoverage for why it asks
   before saving), so this skips only the prompt. Every hold below still runs,
   and the holds compare against the preset - the numbers that were agreed. */
async function runCampaignOneClick({ preset = null } = {}) {
  const btn = document.getElementById(preset ? 'campaign-max-coverage' : 'campaign-oneclick-btn');
  const idleLabel = preset ? 'MAX COVERAGE' : 'BOOK WORLDWIDE — ONE CLICK';
  const box = document.getElementById('campaign-preview-result');
  if (campaignOneClickBusy) return;

  // THE NUMBERS IN THE PROMPT MUST BE THE NUMBERS USED. The prompt reads the
  // sliders, but the backend plans from the SAVED config - POST /preview
  // carries no body. So with unsaved slider moves the prompt could say "up to
  // 20, at most 1 per station" while the run booked 600: a consent prompt that
  // misstates the thing being consented to. Refusing while there are unsaved
  // changes makes slider and saved config the same thing at the moment of
  // asking. For a preset it also catches a slider moved while its save was in
  // flight: the saved caps would then no longer be the ones on screen.
  if (campaignConfigDirty) {
    box.prepend(alertNote(
      'The booking caps have unsaved changes. Press SAVE first — one-click books '
      + 'using the SAVED caps, so the numbers it asks you to confirm must be the '
      + 'saved ones. Nothing was started.'));
    announceCampaign('One-click not started: save the caps first.');
    return;
  }
  const total = preset
    ? preset.total : parseInt(document.getElementById('campaign-max-total').value, 10) || 0;
  const per = preset
    ? preset.per : parseInt(document.getElementById('campaign-max-per-station').value, 10) || 1;
  // campaignPolicy and campaignLoop are the SAVED settings - the refusal above
  // guarantees nothing on screen differs - so they are what the backend will do.
  const policy = preset ? preset.policy : campaignPolicy;

  if (!preset) {
    const ok = window.confirm(campaignConsentText({ total, per, loop: campaignLoop, policy }));
    if (!ok) return;
  }

  campaignOneClickBusy = true;
  setOneClickButtonsDisabled(true);
  btn.textContent = 'PLANNING…';
  try {
    const outcome = await runCampaignPreview();
    if (outcome !== 'ready') {
      // runCampaignPreview has already written why into the result box. What
      // must NOT happen is the old fall-through, which reported a busy
      // backend, a timeout or an error as "the preview found no free passes".
      announceCampaign(`One-click stopped: the preview ${outcome === 'busy'
        ? 'could not start because another campaign run is in progress'
        : outcome === 'timeout' ? 'did not finish in time' : 'failed'}. Nothing was submitted.`);
      return;
    }
    if (!campaignPreviewItems || !campaignPreviewItems.length) {
      box.prepend(note(
        'Nothing to book — the preview completed and found no free passes. '
        + 'Nothing was submitted.'));
      announceCampaign('One-click finished: nothing to book.');
      return;
    }

    // Belt and braces on the same promise: whatever produced this plan (a
    // second tab, a save from another operator, a config the backend clamped
    // differently), it is not submitted if it exceeds what was just agreed to.
    const perStation = new Map();
    for (const it of campaignPreviewItems) {
      perStation.set(it.station_id, (perStation.get(it.station_id) || 0) + 1);
    }
    const deepest = Math.max(...perStation.values());
    if (campaignPreviewItems.length > total || deepest > per) {
      box.prepend(alertNote(
        `Not submitted — the plan (${campaignPreviewItems.length} booking(s), up to `
        + `${deepest} on one station) exceeds what you confirmed (${total}, at most `
        + `${per} per station). The caps were likely changed elsewhere. Review the `
        + 'plan below and use CONFIRM & SUBMIT if it is what you want.', 'error'));
      announceCampaign('One-click stopped: the plan exceeded the confirmed caps. Nothing submitted.');
      return;
    }

    // The same promise, checked against what the backend says it was told
    // (preview.params) rather than only against the rows. This is not
    // redundant: a plan can fit the agreed caps while having been built from
    // larger ones - someone saved 1000 elsewhere and this network happens to
    // fill only 600 - and with loop ON every later round is rebuilt from those
    // saved caps, not from what was agreed here. Likewise the downlink: rows
    // on a fallback transmitter, or params wider than the agreed policy, mean
    // the saved setting changed under us. A preview without params (an older
    // backend) is checked on its rows alone, as before.
    const params = lastPreviewParams;
    const planPolicy = policyFromParams(params);
    const capsWider = !!params
      && (Number(params.max_total) > total || Number(params.max_per_station) > per);
    const policyWider = (planPolicy && CAMPAIGN_POLICY_RANK[planPolicy] > CAMPAIGN_POLICY_RANK[policy])
      || (policy === 'pinned' && campaignPreviewItems.some((it) => it.fallback));
    if (capsWider || policyWider) {
      box.prepend(alertNote(
        'Not submitted — this plan was built from settings wider than the ones you '
        + `confirmed (${total}, at most ${per} per station, ${CAMPAIGN_POLICY_TEXT[policy]}); `
        + 'the backend planned with '
        + `${params?.max_total ?? '?'}, at most ${params?.max_per_station ?? '?'} per station`
        + `${planPolicy ? `, ${CAMPAIGN_POLICY_TEXT[planPolicy]}` : ''}. The saved settings were `
        + 'likely changed elsewhere. Review the plan below and use CONFIRM & SUBMIT if it '
        + 'is what you want.', 'error'));
      announceCampaign('One-click stopped: the plan was built from wider settings than confirmed. Nothing submitted.');
      return;
    }

    // "Book worldwide" is not what a run cut short by the read limit
    // delivers. Submit that automatically and the operator believes the
    // network is covered when a random part of it was never looked at.
    if (lastPreviewStoppedEarly) {
      box.prepend(alertNote(
        'Not submitted automatically — this plan is incomplete (see above). '
        + 'Review it and use CONFIRM & SUBMIT to book what it found, or try again '
        + 'later for a complete plan.'));
      announceCampaign('One-click stopped: incomplete plan held for review. Nothing submitted.');
      return;
    }

    // The exact numbers exist only now, minutes after the prompt, so this is
    // where they are said - on the button and to screen readers - including
    // how many landed on the weaker fallback downlink.
    const stations = perStation.size;
    btn.textContent = `SUBMITTING ${campaignPreviewItems.length}…`;
    announceCampaign(
      `Plan ready: ${campaignPreviewItems.length} booking(s) on ${stations} station(s)`
      + `${fallbackPhrase(campaignPreviewItems)}. Submitting.`);
    await submitPreviewedItems();
  } catch (err) {
    console.error('[schedule] one-click campaign', err);
    box.prepend(alertNote(`One-click run failed: ${err}`, 'error'));
    announceCampaign(`One-click run failed: ${err}`);
  } finally {
    btn.textContent = idleLabel;
    campaignOneClickBusy = false;
    // Re-derive rather than blindly re-enable: without a token it must stay off.
    setOneClickButtonsDisabled(!networkTokenSet);
  }
}

/* "on up to about N community stations", for the consent prompts.

   The honest bound depends on what is known. Without loop, one round books at
   most `total` bookings, so at most `total` stations. With loop, later rounds
   can reach stations a capped first round did not, so only the network's own
   reach bounds it - which is known only from a preview built with the same
   downlink setting (reach differs a lot between settings, see
   paintCampaignReach). No such preview: say that the count is unknown rather
   than print a number that is not an upper bound. */
function stationBoundText(policy, total, loop) {
  const reach = lastPreviewCapableStations;
  const samePolicy = reach != null && policyFromParams(lastPreviewParams) === policy;
  if (samePolicy) {
    const n = loop ? reach : Math.min(reach, total);
    return `up to about ${n} community station(s) (the last preview with this downlink `
      + `setting found ${reach} it could use)`;
  }
  return loop
    ? 'every community station it can reach with this downlink setting — how many '
      + 'is only known once the plan is computed'
    : `up to ${total} community station(s)`;
}

/* The one-click consent prompt, and MAX COVERAGE's (`preset`), which differs
   in saying first that it overwrites the saved settings - and that the
   unattended timer, if on, inherits them. The per-station wording says "in
   the 48 h window, counting what is already booked" because that is what the
   cap now means: a second run tops stations up, it does not add `per` more. */
function campaignConsentText({ total, per, loop, policy, preset = false }) {
  const perText = `at most ${per} per station in the 48 h window (counting KNACKSAT-2 `
    + 'observations already booked on it)';
  let text;
  if (preset) {
    const autoOn = !!document.getElementById('campaign-auto-commit')?.checked;
    text = 'MAX COVERAGE — book KNACKSAT-2 on as many community stations as possible?\n\n'
      + 'This first SAVES these campaign settings, replacing the current ones:\n'
      + `  • Downlink: ${CAMPAIGN_POLICY_TEXT[policy]}\n`
      + `  • Up to ${total} bookings per round, ${perText}\n`
      + (loop
        ? '  • Loop ON — after each round it recomputes and books again until nothing '
          + `is left, so the total can be well above ${total}. It stops when a round `
          + 'finds nothing left, when SatNOGS accepts nothing, or at the round limit.\n\n'
        : '  • Loop off — one round only.\n\n')
      + (autoOn
        ? 'Automatic booking is ON: every later unattended cycle will book with '
          + 'these settings too.\n\n'
        : '');
  } else {
    // With looping on, the commit does not stop at `total`: it recomputes and
    // submits again, round after round, until nothing is left to book - capped
    // per station across ALL rounds, not per round. So `total` is only the
    // first round, and a prompt that named it as the ceiling would understate
    // what gets booked, which is the exact failure this prompt was rewritten
    // to prevent.
    text = (loop
      ? `Book observations on community stations via SatNOGS Network, looping `
        + `until nothing is left: up to ${total} per round, ${perText}, across all `
        + 'rounds?\n\n'
        + `Loop is ON — the total can be well above ${total}. It stops when a round `
        + 'finds nothing left, when SatNOGS accepts nothing, or at the round limit.\n\n'
      : `Book up to ${total} observation(s), ${perText}, on community stations via `
        + 'SatNOGS Network?\n\n')
      + `Downlink: ${CAMPAIGN_POLICY_TEXT[policy]}.\n\n`;
  }
  return text
    + `It books real observations on ${stationBoundText(policy, total, loop)}. `
    + 'It computes the plan and then SUBMITS IT AUTOMATICALLY — you will not be '
    + 'asked again. These are other operators\' ground stations, and accepted '
    + 'bookings cannot be undone from this dashboard.\n\n'
    + 'Use PREVIEW instead if you want to read the plan first.';
}

/* MAX COVERAGE: the preset in MAX_COVERAGE, saved, then the ordinary one-click.

   It asks BEFORE touching the saved settings, not after. Those settings are
   also what the unattended timer books with, so a save followed by a declined
   prompt would leave "600, loop on" armed for the next auto cycle - a booking
   nobody agreed to. Asking first means a "no" changes nothing at all.

   A failed save stops here, visibly: going on would ask the backend to plan
   from whatever it still has saved, which is not what was just agreed to. */
async function runCampaignMaxCoverage() {
  if (campaignOneClickBusy) return;
  const box = document.getElementById('campaign-preview-result');
  const btn = document.getElementById('campaign-max-coverage');
  const preset = MAX_COVERAGE;

  const ok = window.confirm(campaignConsentText({ ...preset, preset: true }));
  if (!ok) return;

  // Held busy across the save too, so a second press (or the other button)
  // cannot start a parallel run while the settings are still landing.
  campaignOneClickBusy = true;
  setOneClickButtonsDisabled(true);
  btn.textContent = 'SAVING…';
  let saved;
  try {
    applyCampaignControls(preset);
    saved = await saveCampaignConfig();
  } finally {
    campaignOneClickBusy = false;
    btn.textContent = 'MAX COVERAGE';
    setOneClickButtonsDisabled(!networkTokenSet);
  }
  if (!saved?.ok) {
    box.prepend(alertNote(
      `MAX COVERAGE not started — its settings could not be saved (${saved?.error || 'unknown error'}). `
      + 'Nothing was previewed or booked. The controls above show the MAX COVERAGE '
      + 'values, NOT saved: press SAVE to retry, or reopen the panel to go back to '
      + 'the saved ones.', 'error'));
    announceCampaign('MAX COVERAGE not started: its settings could not be saved. Nothing booked.');
    return;
  }
  await runCampaignOneClick({ preset });
}

/* Puts a preset on the controls exactly as if the operator had moved them:
   dirty until saved, any shown preview invalidated, reach hint repainted. */
function applyCampaignControls({ policy, per, total, loop }) {
  document.getElementById('campaign-tx-policy').value = policy;
  document.getElementById('campaign-max-per-station').value = per;
  document.getElementById('campaign-max-per-station-value').textContent = String(per);
  document.getElementById('campaign-max-total').value = total;
  document.getElementById('campaign-max-total-value').textContent = String(total);
  document.getElementById('campaign-loop').checked = loop;
  campaignConfigDirty = true;
  invalidateCampaignPreview();
  paintCampaignReach();
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
  let unknownNote = null;

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
    const uncertainItems = Array.isArray(run.uncertain_items) ? run.uncertain_items : [];
    // Unknown outcomes are not rejections: they may well be booked. Counting
    // them as "rejected" (as submitted - accepted alone did) invites exactly
    // the resubmit that would double-book those stations.
    const rejected = Math.max(0, submitted - accepted - uncertainItems.length);

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
    meta.append(okStat, badStat);
    if (uncertainItems.length) {
      const unknownStat = document.createElement('span');
      unknownStat.className = 'sched-stat warn';
      unknownStat.textContent = `${uncertainItems.length} outcome unknown`;
      meta.appendChild(unknownStat);
    }
    const ofText = document.createElement('span');
    ofText.textContent = `of ${submitted} submitted`
      + (Number.isFinite(run.stations_booked) ? ` · on ${run.stations_booked} station(s)` : '');
    meta.appendChild(ofText);
    // stopped_reason is shown whenever there is one, not only for looped runs:
    // a single-round commit (the auto timer with loop off) whose build was
    // cut short by the read limit reports that here, and it is the one line
    // explaining why the run booked less than the preview promised.
    if ((run.rounds ?? 1) > 1 || campaignLoop || run.stopped_reason) {
      const roundsText = document.createElement('span');
      roundsText.textContent = `in ${run.rounds ?? 1} round(s)`
        + (run.stopped_reason ? ` · stopped: ${run.stopped_reason}` : '');
      meta.appendChild(roundsText);
    }
    announceCampaign(
      `Submit finished: ${accepted} booked, ${rejected} rejected`
      + (uncertainItems.length ? `, ${uncertainItems.length} with unknown outcome` : '')
      + `, of ${submitted} submitted.`);

    if (uncertainItems.length) {
      // Same rule as UNKNOWN_REASON in the rejection report, said above it
      // because it is the one outcome that must change what happens next.
      // CROSS-CHECK below reads back only ACCEPTED bookings, so these stations
      // are named for checking on SatNOGS directly.
      const ids = [...new Set(uncertainItems.map((it) => it.station_id))];
      const shown = ids.slice(0, 12).join(', ') + (ids.length > 12 ? `, … (${ids.length} stations)` : '');
      unknownNote = alertNote(
        `${uncertainItems.length} booking(s) have an UNKNOWN outcome — SatNOGS may have `
        + 'accepted them. Do NOT resubmit them: check these stations\' schedules on '
        + `SatNOGS first (station ${shown}). This run did not retry them either.`);
    }
  }
  wrap.appendChild(meta);
  if (unknownNote) wrap.appendChild(unknownNote);

  if (run.status !== 'error') {
    if (Array.isArray(run.accepted_by_transmitter) && run.accepted_by_transmitter.length) {
      wrap.appendChild(chipRow('Booked by downlink', transmitterChips(run.accepted_by_transmitter)));
    }
    if (Array.isArray(run.accepted_band_counts) && run.accepted_band_counts.length) {
      wrap.appendChild(chipRow('Booked by peak elevation', bandChips(run.accepted_band_counts)));
    }
    const sources = formatCalendarSources(run.calendar_sources);
    if (sources) {
      const src = document.createElement('p');
      src.className = 'muted sched-meta';
      src.textContent = `Calendar reads during this submit: ${sources}`;
      wrap.appendChild(src);
    }
  }

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

/* The current per-item line, which leads with the station because "HTTP 409"
   alone did not say which of 77 stations refused:
     station <id> <start> transmitter <uuid>: HTTP <status> <body>
     station <id> <start> transmitter <uuid>: OUTCOME UNKNOWN - <why>
   The start still contains a space (format_api_datetime), so this anchors on
   " transmitter " exactly as the old pattern anchored on " norad-transmitter ".
   CAMPAIGN_ERROR_RE above is kept: run history persists the lines earlier runs
   wrote, and those are still in the old shape. */
const CAMPAIGN_ERROR_V2_RE =
  /^station\s+(\S+)\s+(.*?)\s+transmitter\s+(\S+):\s*(?:HTTP\s+(\d+)\s*([\s\S]*)|OUTCOME UNKNOWN\b[\s\S]*)$/;

// Never folded in with rejections. A rejection is known not to have booked; an
// unknown outcome may have, and the one thing an operator must not do with it
// is the thing a rejection invites - try again.
const UNKNOWN_REASON = 'OUTCOME UNKNOWN · may be booked — cross-check, do NOT resubmit';

function parseCampaignError(raw) {
  if (/^OUTCOME UNKNOWN\b/.test(raw)) {
    return { reason: UNKNOWN_REASON, detail: raw };
  }
  const v2 = CAMPAIGN_ERROR_V2_RE.exec(raw);
  if (v2) {
    const [, station, start, , status, body] = v2;
    const where = `station ${station}, ${rejectionTime(start)}`;
    if (status === undefined) {
      return { reason: UNKNOWN_REASON, detail: `${where} — outcome unknown` };
    }
    const reason = extractRejectionReason(body) || `HTTP ${status}`;
    return { reason: `HTTP ${status} · ${reason}`, detail: `${where} — ${reason}` };
  }
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
  thead.innerHTML = '<tr><th>Time UTC</th><th>Trigger</th><th>Booked</th><th>Stations</th>'
    + '<th>Rejected</th><th>Rounds</th><th>Status</th></tr>';
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
    // Breadth, next to depth: 600 booked on 220 stations and 600 on 120 are
    // different campaigns. Runs recorded before the count existed show a dash,
    // not a 0 they never measured.
    const tdStations = document.createElement('td');
    tdStations.textContent = Number.isFinite(run.stations_booked) ? String(run.stations_booked) : '—';
    // Only a non-zero rejection count earns the colour — a column of red
    // zeroes would make a run of clean submits look like a problem.
    const tdRejected = document.createElement('td');
    tdRejected.textContent = String(run.rejected ?? 0);
    if ((run.rejected ?? 0) > 0) tdRejected.className = 'sched-cell-bad';
    const tdRounds = document.createElement('td');
    tdRounds.textContent = String(run.rounds ?? 1);
    const tdStatus = document.createElement('td');
    tdStatus.textContent = run.status || '—';
    if (run.status === 'error') tdStatus.className = 'sched-cell-bad';

    tr.append(tdTime, tdTrigger, tdBooked, tdStations, tdRejected, tdRounds, tdStatus);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  // The backend keeps up to 200 runs; without a bounded scroll region this
  // table alone could push everything else out of the card.
  box.appendChild(scrollableTable(table, `Campaign run history, ${history.length} run(s)`));
}
