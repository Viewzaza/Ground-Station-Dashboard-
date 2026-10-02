/* Observation plan.

   One rotator and one radio, and more passes than either can work: the planner
   on the backend picks which ones this station takes. This panel exists to make
   that choice legible. An operator looking at any pass in the next day should
   see not only whether it is being worked but *why* — what it lost to, or that
   SatNOGS already has it — without opening a log. So every decision carries
   the backend's own reason, on screen, and the strip and the list are two views
   of one set: hover a row and the strip lights the same pass, and the pass that
   beat it.

   Autopilot is the one control here, and it follows the rotator panel's rules.
   The backend is the authority on every gate: ENGAGE is disabled to match what
   /api/control reports — control switched off, or nobody holding a lease —
   and never because this file decided something looked unsafe. A shut gate
   does not disable it either: autopilot engaged behind a closed interlock
   waits, and says so, which is the backend's call to make, not ours. DISENGAGE
   is always available, like STOP — standing down is never the unsafe
   direction.

   Nothing here re-plans. The strip, the countdown and the list are read
   straight off the snapshot the planner published; the only arithmetic is
   where on the strip a time falls. */

import { api, Refused } from '../core/api.js';
import { store, set } from '../core/store.js';
import { bus } from '../core/bus.js';
import { countdown, deg, hmsLocal, pad2, shortTime } from '../core/format.js';

const $ = (id) => document.getElementById(id);

const HOUR_MS = 3_600_000;

// How much of the past the strip keeps. With none, "now" is the left edge and
// a pass under way looks as if it had only just begun; half an hour puts the
// marker inside the strip and leaves the elapsed part of that pass in view.
const LEAD_MS = 30 * 60_000;

// The strip shows the plan's own horizon, held between these. Under twelve
// hours a quiet night is an empty bar; over a day, a ten-minute pass on the
// wall column is narrower than its own outline.
const SPAN_MIN_H = 12;
const SPAN_MAX_H = 24;

// The strip slides about a pixel every three minutes on the wall, so laying it
// out every second would be work for nothing. The countdown does tick at 1 Hz.
const RELAYOUT_MS = 15_000;

// The header's threshold for "imminent", so the two countdowns turn together.
const IMMINENT_S = 600;

// When a pass's keyhole lag is worth a word on its row. The planner already
// priced it into the score; this only says so. A quarter of a typical 30-50°
// UHF Yagi beam is about where the loss stops being a rounding error (~0.75 dB
// at 40°) — below it, the note would be on every pass over 55° and mean
// nothing. The figure shown is the backend's own, never recomputed here.
const KEYHOLE_NOTE_DEG = 10;

// Passes the plan considered and turned down. They are drawn in the strip's
// lower lane, dim; `low` goes there too, dimmer still, because a pass that
// never clears the threshold was never really in contention.
const LOSERS = new Set(['conflict', 'infeasible', 'reserved']);

// Tick spacings, in hours. The first that leaves room for an "18:00" wins.
// Every one divides 24, so local midnight is always a tick.
const TICK_STEPS_H = [1, 2, 3, 6, 12];
const TICK_MIN_PX = 46;

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

let mounted = false;
let rows = [];          // decisions with their times parsed once, not per tick
let planned = [];
let blockEls = new Map();
let listSig = '';
let laidOutAt = 0;
let planError = '';
let rebuildError = '';
let autoError = '';
let notice = '';
let noticeWanted = false;   // the enabled state the request behind `notice` asked for
let lastArmed = false;
let busy = false;
let rebuilding = false;

export function mountPlan() {
  if (!$('plan-panel')) return;
  mounted = true;
  lastArmed = Boolean(store.control?.armed);

  const strip = $('plan-strip');
  strip.innerHTML = '<div class="pl-ticks"></div><div class="pl-blks"></div><i class="pl-now"></i>';

  bus.on('plan', (p) => {
    planError = '';
    rebuildError = '';
    ingest(p);
    buildBlocks();
    layout();
    renderList();
    paintNext();
    paintHint();
    paintWarn();
    renderBlind();
  });
  bus.on('autopilot', (a) => {
    autoError = '';
    // A notice is history once the state it complained about has changed: a
    // failed ENGAGE once autopilot is engaged, a failed DISENGAGE once it is
    // off. A failed DISENGAGE must survive "still engaged" — that is the news.
    if (notice && Boolean(a?.enabled) === noticeWanted) notice = '';
    renderAuto();
    buildBlocks();
    layout();
    renderList();
  });
  // ENGAGE follows the lease, which comes and goes on its own — it expires,
  // or the operator presses RELEASE on the rotator panel. A refusal that said
  // "arm control first" is stale the moment the lease changes hands.
  bus.on('control', (c) => {
    if (Boolean(c?.armed) !== lastArmed) notice = '';
    lastArmed = Boolean(c?.armed);
    renderAuto();
  });
  // Names for the unplannable line, which arrive after this mounts.
  bus.on('catalog', renderBlind);
  // Every (re)connect may be to a different process — a deploy, a crash — and
  // the live link only pushes changes. Re-read both from the authority rather
  // than going on showing what the previous process said.
  // (The first hello repeats the reads below; two small GETs, once.)
  bus.on('hello', refetch);

  const list = $('plan-list');
  list.addEventListener('mouseover', onHover);
  list.addEventListener('mouseout', onHover);
  $('plan-rebuild').onclick = rebuild;

  // The tick spacing depends on how wide the strip is, and it is a different
  // width in each of the three layouts.
  if (window.ResizeObserver) new ResizeObserver(() => layout()).observe(strip);

  ingest(store.plan);
  buildBlocks();
  layout();
  renderList();
  renderAuto();
  paintNext();
  paintHint();
  paintWarn();
  renderBlind();

  // Both are pushed on the live link, but only when they change — a browser
  // opened between two rebuilds would otherwise wait for the next one.
  refetch();
}

function refetch() {
  api.plan()
    .then((p) => set('plan', p))
    .catch((err) => {
      console.warn('[plan]', err);
      planError = String(err.message || err);
      renderList();
      paintHint();
    });
  api.autopilot()
    .then((a) => set('autopilot', a))
    .catch((err) => {
      console.warn('[autopilot]', err);
      autoError = String(err.message || err);
      renderAuto();
    });
}

/** Called from the 1 Hz loop in main.js. */
export function tickPlan() {
  if (!mounted) return;
  paintNext();
  if (Date.now() - laidOutAt >= RELAYOUT_MS) layout();
  // Cheap when nothing has changed: it compares a signature before touching
  // the DOM, so a row under the cursor keeps its tooltip.
  renderList();
}

// ---------------------------------------------------------------------------
// data

function ingest(p) {
  const parse = (d) => ({ ...d, aosMs: Date.parse(d.aos), losMs: Date.parse(d.los) });
  rows = (p?.decisions || []).map(parse);
  planned = (p?.planned || []).map(parse);
}

const tz = () => store.config?.station?.timezone || 'UTC';
const isLive = (d, now) => d.aosMs <= now && now < d.losMs;
const nameOf = (d) => d.name || `#${d.norad}`;
const statusClass = (s) => `s-${String(s || '').replace(/[^a-z_-]/gi, '')}`;

// ---------------------------------------------------------------------------
// countdown

function paintNext() {
  const wrap = $('plan-next');
  const label = $('plan-next-label');
  const value = $('plan-next-cd');
  const what = $('plan-next-what');
  if (!wrap) return;

  const now = Date.now();
  const next = planned.find((c) => c.losMs > now);

  if (!next) {
    label.textContent = 'NEXT PLANNED AOS';
    value.textContent = '--:--:--';
    what.textContent = store.plan?.built_at
      ? `nothing planned in the next ${Math.round(store.plan.horizon_h || 24)} h`
      : 'awaiting the planner';
    wrap.className = 'pl-next';
    return;
  }

  const inPass = next.aosMs <= now;
  const secs = ((inPass ? next.losMs : next.aosMs) - now) / 1000;
  label.textContent = inPass ? 'PLANNED · LOS IN' : 'NEXT PLANNED AOS';
  value.textContent = countdown(secs);
  what.textContent =
    `${nameOf(next)} · ${shortTime(next.aos, tz())} · max ${deg(next.max_el, 0)}`;
  wrap.className = 'pl-next' + (inPass ? ' in-pass' : secs < IMMINENT_S ? ' imminent' : '');
}

function paintHint() {
  const hint = $('plan-hint');
  const btn = $('plan-rebuild');
  if (!hint) return;

  const p = store.plan;
  if (!p?.built_at) {
    hint.textContent = planError ? 'planner unavailable' : '—';
    hint.title = planError;
  } else {
    const n = p.counts?.planned ?? planned.length;
    hint.textContent = rebuildError
      ? 'rebuild failed'
      : `${n} of ${rows.length} planned · built ${shortTime(p.built_at, tz())}`;
    const age = p.schedule_age_s;
    hint.title = [
      rebuildError,
      Object.entries(p.counts || {}).map(([k, v]) => `${v} ${k}`).join(' · '),
      p.schedule_known === false ? 'SatNOGS schedule: not loaded'
        : typeof age === 'number' ? `SatNOGS schedule ${ageText(age)} old when built` : '',
    ].filter(Boolean).join('\n');
  }
  btn.disabled = rebuilding;
  btn.textContent = rebuilding ? 'rebuilding…' : 'rebuild';
}

async function rebuild() {
  if (rebuilding) return;
  rebuilding = true;
  rebuildError = '';
  paintHint();
  try {
    set('plan', await api.planRebuild());
  } catch (err) {
    console.warn('[plan] rebuild', err);
    // Said in the heading rather than only the console: an operator who
    // pressed it is looking at the panel, and the plan below is still the
    // old one. Cleared by the next plan to arrive.
    rebuildError = String(err.message || err);
  } finally {
    rebuilding = false;
    paintHint();
  }
}

const ageText = (s) => (s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min`
                        : `${(s / 3600).toFixed(1)} h`);

/** The plan was built before SatNOGS's job list ever loaded, so none of its
    observations were reserved — the plan may sit on top of them. The backend
    says so in `schedule_known`; this only puts it where it cannot be missed.
    What autopilot does about it is the executor's call, and its own detail
    line reports that. */
function paintWarn() {
  const el = $('plan-warn');
  if (!el) return;
  const p = store.plan;
  const show = Boolean(p?.built_at) && p.schedule_known === false;
  el.hidden = !show;
  el.textContent = show
    ? "SatNOGS schedule not loaded — this plan does not yet avoid SatNOGS's observations"
    : '';
  el.title = show ? 'the next rebuild after the schedule loads will take them into account' : '';
}

/** Satellites the plan is blind to: watched, but with no elements to predict
    from. One line, because there is nothing to do about them from here; the
    reasons are one hover away, and are usually all the same one. */
function renderBlind() {
  const el = $('plan-blind');
  if (!el) return;
  const list = store.plan?.unplannable || [];
  el.hidden = list.length === 0;
  if (!list.length) { el.innerHTML = ''; return; }

  const byNorad = new Map((store.catalog || []).map((s) => [s.norad, s.name]));
  const label = (u) => (byNorad.get(u.norad) ? `${byNorad.get(u.norad)} (#${u.norad})` : `#${u.norad}`);
  const reasons = new Set(list.map((u) => u.reason || ''));
  const shared = reasons.size === 1 ? [...reasons][0] : '';

  el.title = list.map((u) => `${label(u)}: ${u.reason || 'no reason given'}`).join('\n');
  el.innerHTML = `
    <span class="pl-label">NOT PLANNABLE</span>
    <span class="pl-blind-list">${list.map((u) =>
      `<span class="pl-blind-sat" title="${esc(u.reason || '')}">${esc(label(u))}</span>`).join(', ')}${
      shared ? ` <span class="pl-blind-why">— ${esc(shared)}</span>` : ''}</span>`;
}

// ---------------------------------------------------------------------------
// timeline strip

function stripWindow(now) {
  const h = Math.min(SPAN_MAX_H, Math.max(SPAN_MIN_H, store.plan?.horizon_h || SPAN_MAX_H));
  return { t0: now - LEAD_MS, t1: now + h * HOUR_MS };
}

/** Rebuilt only when the plan or autopilot's target changes. Between those,
    layout() moves the same elements, so a block under the cursor keeps its
    tooltip while the strip slides. */
function buildBlocks() {
  const host = document.querySelector('#plan-strip .pl-blks');
  if (!host) return;

  const cur = store.autopilot?.current || '';
  // Lower lane first, so a block in the upper lane is never painted under one.
  const rank = (d) => (d.status === 'planned' || d.status === 'satnogs' ? 1 : 0);
  const ordered = [...rows].sort((a, b) => rank(a) - rank(b));

  host.innerHTML = ordered.map((d) => {
    const cls = ['pl-blk', statusClass(d.status)];
    if (LOSERS.has(d.status)) cls.push('lose');
    if (d.key === cur) cls.push('cur');
    return `<i class="${cls.join(' ')}" data-key="${esc(d.key)}" title="${esc(blockTitle(d))}"></i>`;
  }).join('');

  blockEls = new Map([...host.children].map((el) => [el.dataset.key, el]));
}

function blockTitle(d) {
  const z = tz();
  const kh = keyholeNote(d);
  return `${nameOf(d)} · ${shortTime(d.aos, z)}–${shortTime(d.los, z)} · max ${deg(d.max_el, 0)}`
       + ` · ${d.status}\n${d.reason || ''}${kh ? `\n${kh}` : ''}`;
}

/** "keyhole lag 14° · −1.5 dB", or '' for a pass whose lag is not worth a
    word. Both numbers are the planner's: the lag is its simulation of the
    rotator chasing the pass through zenith, and the loss is read back off the
    keyhole factor it multiplied the score by. */
function keyholeNote(d) {
  const err = d.keyhole_error_deg;
  if (typeof err !== 'number' || !(err >= KEYHOLE_NOTE_DEG)) return '';
  const factor = d.breakdown?.keyhole;
  const db = typeof factor === 'number' && factor > 0 && factor < 1
    ? ` · −${(-10 * Math.log10(factor)).toFixed(1)} dB` : '';
  return `keyhole lag ${err.toFixed(0)}°${db}`;
}

function layout() {
  const strip = $('plan-strip');
  if (!strip) return;
  const now = Date.now();
  laidOutAt = now;

  const { t0, t1 } = stripWindow(now);
  const span = t1 - t0;
  const pct = (ms) => `${(((ms - t0) / span) * 100).toFixed(3)}%`;

  strip.querySelector('.pl-now').style.left = pct(now);

  for (const d of rows) {
    const el = blockEls.get(d.key);
    if (!el) continue;
    if (d.losMs <= t0 || d.aosMs >= t1) { el.hidden = true; continue; }
    const a = Math.max(d.aosMs, t0);
    const b = Math.min(d.losMs, t1);
    el.hidden = false;
    el.style.left = pct(a);
    el.style.width = `${(((b - a) / span) * 100).toFixed(3)}%`;
  }

  const width = strip.clientWidth;
  if (width > 0) paintTicks(strip.querySelector('.pl-ticks'), t0, t1, width);
}

/** Hour ticks on the station's own clock, not UTC — the list beside them is in
    local time, and the two have to agree when read together. */
function paintTicks(host, t0, t1, width) {
  const z = tz();
  const span = t1 - t0;
  const pxPerHour = width / (span / HOUR_MS);
  const step = (TICK_STEPS_H.find((h) => h * pxPerHour >= TICK_MIN_PX) || 24) * HOUR_MS;
  const off0 = tzOffsetMs(t0, z);

  let html = '';
  // `wall` is local wall-clock time expressed as if it were UTC, so the
  // UTC getters below read local hours off it. Each tick finds its own
  // offset: one taken at t0 put every tick after a DST change an hour out
  // from the list beside it.
  for (let wall = Math.ceil((t0 + off0) / step) * step; ; wall += step) {
    let inst = wall - tzOffsetMs(wall - off0, z);
    inst = wall - tzOffsetMs(inst, z);
    if (inst > t1) break;
    // On the night the clocks go forward this local hour never happens.
    if (inst < t0 || inst + tzOffsetMs(inst, z) !== wall) continue;
    const at = new Date(wall);
    const hour = at.getUTCHours();
    const day = hour === 0;
    const x = (((inst - t0) / span) * 100).toFixed(3);
    html += `<i class="pl-tick${day ? ' day' : ''}" style="left:${x}%">`
          + `<span>${day ? WEEKDAYS[at.getUTCDay()] : `${pad2(hour)}:00`}</span></i>`;
  }
  host.innerHTML = html;
}

// ---------------------------------------------------------------------------
// decision list

function renderList() {
  const host = $('plan-list');
  if (!host) return;

  const p = store.plan;
  if (!p?.built_at) {
    const text = planError ? `planner unavailable — ${planError}` : 'awaiting the planner';
    if (listSig !== text) {
      host.innerHTML = `<li class="pl-empty">${esc(text)}</li>`;
      listSig = text;
    }
    return;
  }

  const now = Date.now();
  const upcoming = rows.filter((d) => d.losMs > now);
  const cur = store.autopilot?.current || '';
  const sig = `${p.built_at}#${cur}#`
            + upcoming.map((d) => d.key + (isLive(d, now) ? '*' : '')).join('|');
  if (sig === listSig) return;
  listSig = sig;

  // Rebuilding the rows drops whatever the strip was lighting for the old ones.
  for (const el of blockEls.values()) el.classList.remove('hot', 'blocker');

  if (!upcoming.length) {
    host.innerHTML = `<li class="pl-empty">no passes in the next ${Math.round(p.horizon_h || 24)} h</li>`;
    return;
  }

  const z = tz();
  const home = store.config?.default_norad;
  const today = dayKey(now, z);

  host.innerHTML = upcoming.map((d) => {
    const cls = ['pl-row', statusClass(d.status)];
    if (LOSERS.has(d.status)) cls.push('lose');
    if (d.norad === home) cls.push('home');
    if (d.key === cur) cls.push('cur');
    if (isLive(d, now)) cls.push('live');

    // A day ahead, "07:00" is ambiguous; the weekday is only shown when the
    // pass is not today, so the common case stays a bare time.
    const day = dayKey(d.aosMs, z) === today ? '' : `<i>${weekday(d.aosMs, z)}</i>`;
    const blocked = d.blocked_by ? ` data-blocked-by="${esc(d.blocked_by)}"` : '';
    const kh = keyholeNote(d);
    if (kh) cls.push('keyhole');
    const why = (d.reason || '') + (kh ? ` · ${kh}` : '');

    return `
      <li class="${cls.join(' ')}" data-key="${esc(d.key)}"${blocked} title="${esc(rowTitle(d, z))}">
        <span class="pl-time">${day}${shortTime(d.aos, z)}</span>
        <span class="pl-name">${esc(nameOf(d))}</span>
        <span class="pl-el">${deg(d.max_el, 0)}</span>
        <span class="pl-dur">${Math.round((d.duration_s || 0) / 60)}m</span>
        <span class="pl-st">${esc(d.status)}</span>
        <span class="pl-why" title="${esc(why)}">${esc(d.reason || '')}${
          kh ? `${d.reason ? ' · ' : ''}<span class="pl-kh">${esc(kh)}</span>` : ''}</span>
      </li>`;
  }).join('');
}

/** The row's tooltip: everything the planner weighed, for the operator who
    wants to know why a 35° pass outscored a 50° one. */
function rowTitle(d, z) {
  const t = (iso) => hmsLocal(new Date(iso), z);
  const parts = Object.entries(d.breakdown || {})
    .map(([k, v]) => `${k} ${typeof v === 'number' ? +v.toFixed(2) : v}`);
  return `AOS ${t(d.aos)} · TCA ${t(d.tca)} · LOS ${t(d.los)}\n`
       + `az ${deg(d.aos_az, 0)} → ${deg(d.los_az, 0)} · NORAD ${d.norad}\n`
       + `score ${typeof d.score === 'number' ? d.score.toFixed(2) : '—'}`
       + (parts.length ? ` (${parts.join(', ')})` : '');
}

/** Hovering a row lights its block on the strip, and — for a pass that lost —
    the pass it lost to, in both places. */
function onHover(ev) {
  const li = ev.target.closest?.('.pl-row');
  if (!li || li.contains(ev.relatedTarget)) return;
  const on = ev.type === 'mouseover';

  blockEls.get(li.dataset.key)?.classList.toggle('hot', on);
  const rival = li.dataset.blockedBy;
  if (rival) {
    blockEls.get(rival)?.classList.toggle('blocker', on);
    for (const row of $('plan-list').querySelectorAll('.pl-row')) {
      if (row.dataset.key === rival) row.classList.toggle('blocker', on);
    }
  }
}

// ---------------------------------------------------------------------------
// autopilot

const ENGAGE_HINT = 'work the plan through the interlock while your lease lasts';

/** Why ENGAGE is shut, from /api/control's own answer. The text follows the
    backend's refusal, so the tooltip and the 409 say the same thing. */
function engageBlocked(c) {
  if (!c) return 'control state unknown';
  if (!c.enabled) return 'rotator control is disabled in the deployment (GS_ROTATOR_CONTROL_ENABLED=0)';
  if (!c.armed) return 'arm control first — autopilot never takes a lease itself';
  return '';
}

function renderAuto() {
  const host = $('plan-auto');
  if (!host) return;
  const a = store.autopilot;

  if (!a) {
    host.innerHTML = `
      <div class="pl-auto-row">
        <span class="pl-label">AUTOPILOT</span>
        <span class="pl-detail muted">${autoError ? 'unavailable' : 'state unknown'}</span>
      </div>`;
    return;
  }

  const phase = String(a.phase || (a.enabled ? 'waiting' : 'off'));
  const detail = a.detail || a.disengaged_because || (a.enabled ? '' : 'not engaged');

  let control;
  if (a.enabled) {
    control = `
      <span class="pl-engage" title="stop working the plan">
        <button id="plan-engage" type="button" class="ctl-btn active" ${busy ? 'disabled' : ''}>DISENGAGE</button>
      </span>`;
  } else {
    const why = engageBlocked(store.control);
    // The tooltip is on the wrapper as well as the button: a disabled button
    // does not receive the hover in every browser, and a disabled control is
    // exactly the one whose tooltip the operator needs.
    control = `
      <span class="pl-engage" title="${esc(why || ENGAGE_HINT)}">
        <button id="plan-engage" type="button" class="ctl-btn" ${why || busy ? 'disabled' : ''}>ENGAGE</button>
      </span>`;
  }

  host.innerHTML = `
    <div class="pl-auto-row">
      <span class="pl-label">AUTOPILOT</span>
      <span class="pl-phase ph-${phase.replace(/[^a-z_-]/gi, '')}">${esc(phase)}</span>
      <span class="pl-detail" title="${esc(detail)}">${esc(detail)}</span>
      ${control}
    </div>
    ${notice ? `<div class="ctl-notice">${esc(notice)}</div>` : ''}`;

  $('plan-engage').onclick = () => engage(!a.enabled);
}

async function engage(enabled) {
  if (busy) return;
  busy = true;
  notice = '';
  renderAuto();
  try {
    set('autopilot', await api.setAutopilot(enabled));
  } catch (err) {
    // The refusal is shown as the backend worded it rather than paraphrased,
    // so the panel cannot drift from what the executor actually checks.
    notice = err instanceof Refused
      ? `refused — ${err.reason || err.message}`
      : `${enabled ? 'ENGAGE' : 'DISENGAGE'} failed — ${err.message || err}`;
    noticeWanted = enabled;
    // Re-read rather than trusting the panel's idea of state after a failure.
    try {
      set('autopilot', await api.autopilot());
    } catch { /* repainted below either way */ }
  } finally {
    busy = false;
    renderAuto();
  }
}

// ---------------------------------------------------------------------------
// local time

const offsetFormats = new Map();

/** The station zone's offset from UTC at `ms`, in ms. Intl has no direct way to
    ask, so this formats the instant in that zone and reads the difference. */
function tzOffsetMs(ms, zone) {
  try {
    let f = offsetFormats.get(zone);
    if (!f) {
      f = new Intl.DateTimeFormat('en-US', {
        timeZone: zone, hourCycle: 'h23',
        year: 'numeric', month: 'numeric', day: 'numeric',
        hour: 'numeric', minute: 'numeric', second: 'numeric',
      });
      offsetFormats.set(zone, f);
    }
    const p = {};
    for (const { type, value } of f.formatToParts(new Date(ms))) p[type] = value;
    const wall = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
    return wall - (ms - (ms % 1000));
  } catch {
    return 0;
  }
}

const dayKey = (ms, zone) => new Date(ms + tzOffsetMs(ms, zone)).toISOString().slice(0, 10);
const weekday = (ms, zone) => WEEKDAYS[new Date(ms + tzOffsetMs(ms, zone)).getUTCDay()];

function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}
