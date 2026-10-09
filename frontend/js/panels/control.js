/* Rotator control.

   The panel's job is not to look like a remote control. It is to make the
   interlock legible: an operator who presses GO and nothing happens must be
   able to see *which* gate is shut without reading a log.

   So the gates are always on screen, named, and coloured — not hidden behind
   a disabled button. The controls are disabled to match, which means the panel
   never invites a command the backend is going to refuse.

   Two deliberate asymmetries:

   STOP is always enabled. If the antenna is moving and the operator wants it to
   stop, an expired lease is not a reason to keep driving.

   The backend is the authority on every gate. Nothing here re-derives whether a
   move is allowed; the panel only renders what /api/control reports and then
   asks. A UI that decided for itself would eventually disagree with the thing
   holding the socket.

   The markup is built once and then updated in place. It used to be rebuilt
   from a template on every `control` frame — ARM, any gate flip, any command
   autopilot sent, any lease publish — and the template said value="0". A
   typed AZ/EL silently became 0/0 and focus dropped to the page, so the next
   GO sent the antenna to 0/0: the panel overwrote the operator between their
   typing a position and their pressing GO. Now only the skeleton's shape
   changes the DOM — state first known, control switched on or off — and every
   frame after that touches classes, labels and disabled flags, never an
   input.

   The rebuild had also been dropping focus on every frame, by accident, and
   that had been hiding a hazard: a clicked button keeps focus, and the
   browser turns Enter or Space on a focused button into a click. Updated in
   place, the panel needs that as a rule rather than a side effect — see
   guardKeys. */

import { api, Refused } from '../core/api.js';
import { store, set } from '../core/store.js';
import { bus } from '../core/bus.js';
import { bindKey } from '../core/keys.js';
import { hmsLocal, shortTime } from '../core/format.js';

const $ = (id) => document.getElementById(id);

const GATE_LABELS = {
  kill_switch: 'enabled',
  satnogs_idle: 'SatNOGS idle',
  no_imminent_pass: 'no imminent pass',
  armed: 'armed',
};

const GATE_WHY = {
  kill_switch: 'GS_ROTATOR_CONTROL_ENABLED is 0 — control is switched off in the deployment',
  satnogs_idle: 'satnogs-client is connected to the network and may start a track at any moment',
  no_imminent_pass: 'an observation is scheduled within the guard window, or the schedule is stale',
  armed: 'no operator lease — press ARM',
};

// The lease countdown turns amber with three minutes left — time to decide
// whether to EXTEND — and red in the last one.
const LEASE_WARN_S = 180;
const LEASE_EXPIRING_S = 60;

// A second Shift+S within this long confirms a STOP while SatNOGS may be
// driving. Long enough to mean it, short enough that a stray press earlier on
// cannot be "confirmed" by a later one.
const STOP_CONFIRM_MS = 2000;

const TRACK_NAME_MAX = 14;

// The travel limits the backend reports in force. Until it has said, the
// station's configured ones: -90..450 is what station 5024 actually accepts,
// and narrower than dump_caps's compiled -180..540.
const FALLBACK_LIMITS = { min_az: -90, max_az: 450, min_el: 0, max_el: 100 };

// PolarPlot.draw() insets the horizon ring this far from the canvas edge.
const POLAR_INSET_PX = 12;

let host = null;
let shape = '';                 // which skeleton is mounted: unknown | off | on
let notice = { text: '', kind: '' };
let limits = null;              // only GET /api/control carries them; frames do not
let stopAskedAt = 0;
let stopAskTimer = null;
let tabbing = false;            // the last key down anywhere was Tab
let tabbedTo = null;            // the control Tab last put focus on

export function mountControl() {
  host = $('rot-control');
  if (!host) return;

  bus.on('control', render);
  // The lease line says when autopilot will stand down and how that falls
  // against the pass it is working; both arrive on their own.
  bus.on('autopilot', paintLease);
  bus.on('plan', paintLease);
  bus.on('satellite', paintTrack);
  render();

  const polar = $('polar');
  if (polar) polar.addEventListener('click', fillFromPlot);

  // How focus reached a control — see guardKeys. The window sees every Tab
  // and every pointer press, wherever focus was when it happened. A pointer
  // press anywhere ends what Tab granted: a button Tabbed to and then
  // clicked keeps its focus but loses its ring. On the host, not the
  // skeleton, so a rebuilt skeleton is guarded too.
  window.addEventListener('keydown', (ev) => { tabbing = ev.key === 'Tab'; }, true);
  window.addEventListener('pointerdown', () => { tabbing = false; tabbedTo = null; }, true);
  host.addEventListener('focusin', (ev) => { tabbedTo = tabbing ? ev.target : null; });
  host.addEventListener('keydown', guardKeys);
  host.addEventListener('keyup', guardKeys);

  bindKeys();

  api.control()
    .then((state) => set('control', state))
    .catch((err) => console.warn('[control]', err));
}

// ---------------------------------------------------------------------------
// skeleton

function render() {
  if (!host) return;
  const c = store.control;
  if (c && 'limits' in c) limits = c.limits;

  const want = !c ? 'unknown' : c.enabled ? 'on' : 'off';
  if (want !== shape) mountSkeleton(want);
  if (want === 'on') update(c);
}

function mountSkeleton(kind) {
  shape = kind;
  $('polar')?.classList.toggle('fillable', kind === 'on');

  if (kind === 'unknown') {
    host.innerHTML = '<span class="muted">control state unknown</span>';
    return;
  }

  if (kind === 'off') {
    // Not an error state: control being off is the normal deployment default.
    host.innerHTML = `
      <div class="ctl-off">
        <span class="muted">rotator control disabled</span>
        <span class="ctl-hint">set GS_ROTATOR_CONTROL_ENABLED=1 to enable</span>
      </div>`;
    return;
  }

  host.innerHTML = `
    <div class="ctl">
      <div class="ctl-gates">
        ${Object.entries(GATE_LABELS).map(([key, label]) => `
          <span class="gate gate-shut" data-gate="${key}" title="${GATE_WHY[key] || ''}">○ ${label}</span>`).join('')}
        <span class="ctl-lease" id="ctl-lease"></span>
      </div>

      <div class="ctl-row">
        <label>AZ <input id="ctl-az" type="number" step="0.5" min="-180" max="540" value="0"></label>
        <label>EL <input id="ctl-el" type="number" step="0.5" min="-20" max="210" value="0"></label>
        <button id="ctl-goto" class="ctl-btn" disabled>GO</button>
      </div>

      <div class="ctl-row ctl-cmds">
        <button id="ctl-arm" class="ctl-btn">ARM</button>
        <button id="ctl-extend" class="ctl-btn extend" hidden
                title="push the lease out to a full one — never arms (Shift+E)">EXTEND</button>
        <button id="ctl-track" class="ctl-btn" disabled>TRACK</button>
        <button id="ctl-park" class="ctl-btn" disabled>PARK</button>
        <button id="ctl-stop" class="ctl-btn stop" title="always available (Shift+S)">STOP</button>
      </div>

      <div class="ctl-notice" id="ctl-notice" role="status"></div>
    </div>`;

  // Handlers read the store when pressed, never a copy taken when the markup
  // was built: the skeleton outlives every frame it was not rebuilt for.
  $('ctl-arm').onclick = () => run(store.control?.armed ? api.release() : api.arm());
  $('ctl-extend').onclick = extend;
  $('ctl-goto').onclick = go;
  $('ctl-track').onclick = () => run(api.track(store.satellite?.norad ?? null));
  $('ctl-park').onclick = () => run(api.park());
  $('ctl-stop').onclick = () => run(api.stopRotator());

  for (const id of ['ctl-az', 'ctl-el']) {
    $(id).addEventListener('keydown', (ev) => {
      // Enter is GO, and only when GO itself could be pressed — a key must
      // not reach a command the button would refuse. A held Enter is one GO.
      if (ev.key !== 'Enter' || ev.repeat || ev.isComposing) return;
      ev.preventDefault();
      if (!$('ctl-goto').disabled) go();
    });
  }
}

/** Everything a frame can change, changed in place. */
function update(c) {
  const gates = c.gates || {};
  const clear = (c.blocked_by || []).length === 0;

  for (const el of host.querySelectorAll('.gate[data-gate]')) {
    const key = el.dataset.gate;
    const ok = Boolean(gates[key]);
    el.className = `gate ${ok ? 'gate-ok' : 'gate-shut'}`;
    el.title = ok ? 'open' : (GATE_WHY[key] || '');
    el.textContent = `${ok ? '●' : '○'} ${GATE_LABELS[key]}`;
  }

  $('ctl-goto').disabled = !clear;
  $('ctl-track').disabled = !clear;
  $('ctl-park').disabled = !clear;

  const arm = $('ctl-arm');
  arm.textContent = c.armed ? 'RELEASE' : 'ARM';
  arm.classList.toggle('armed', Boolean(c.armed));
  $('ctl-extend').hidden = !c.armed;
  $('ctl-track').classList.toggle('active', c.mode === 'track');

  paintTrack();
  paintNotice();
  paintLease();
}

/** TRACK says what it will track: the satellite selected on this screen,
    which is what api.track sends. */
function paintTrack() {
  const btn = $('ctl-track');
  if (!btn) return;
  const sat = store.satellite;
  const name = sat?.name || (sat?.norad ? `#${sat.norad}` : '');
  const short = name.length > TRACK_NAME_MAX ? `${name.slice(0, TRACK_NAME_MAX - 1)}…` : name;
  btn.textContent = short ? `TRACK ${short}` : 'TRACK';
  btn.title = name ? `track ${name}${sat?.norad ? ` (NORAD ${sat.norad})` : ''}` : 'track the selected satellite';
}

function setNotice(text, kind = '') {
  notice = { text, kind };
  paintNotice();
}

/** Emptied, never hidden: its line is reserved in panels.css. #polar is the
    flex:1 sibling above, so a notice that appeared by un-hiding took its
    height from the plot — under the pointer that had just clicked it to
    fill, squashed until the next 1 Hz redraw and then smaller and higher, so
    a second click to refine meant another bearing. */
function paintNotice() {
  const el = $('ctl-notice');
  if (!el) return;
  el.textContent = notice.text;
  el.classList.toggle('ctl-fill-note', notice.kind === 'fill');
}

// ---------------------------------------------------------------------------
// commands

function num(id) {
  const value = Number($(id)?.value);
  return Number.isFinite(value) ? value : 0;
}

function go() {
  return run(api.goto(num('ctl-az'), num('ctl-el')));
}

/** EXTEND only ever happens on a press — the button or Shift+E. Nothing on a
    timer, a reconnect or a frame calls it; a lease nobody is watching should
    run out. The backend refuses it with `armed` when there is no lease, so a
    click that raced the expiry is told to press ARM rather than quietly
    becoming an arm. */
function extend() {
  return run(api.extend(), { sentence: true });
}

async function run(promise, { sentence = false } = {}) {
  try {
    setNotice('');
    set('control', await promise);
  } catch (err) {
    // A refusal names the gates, so the operator is told what to fix rather
    // than that "it didn't work". Where the backend's own sentence says it
    // better — "no lease to extend — press ARM" — that is shown instead.
    setNotice(err instanceof Refused
      ? `refused — ${sentence ? err.message
          : (err.blockedBy || []).map((g) => GATE_LABELS[g] || g).join(', ') || err.message}`
      : String(err.message || err));
    // Re-read rather than trusting the panel's idea of state after a failure.
    try {
      set('control', await api.control());
    } catch {
      render();
    }
  }
}

// ---------------------------------------------------------------------------
// click-to-fill

/** A click (or tap) on the polar plot fills AZ and EL with that point of the
    sky. It never sends anything: GO is still a separate, deliberate press.

    The plot is a compass, and a compass bearing is the wrong thing to send a
    SPID — it holds every bearing more than once, and 270 from an antenna at 0
    is three-quarters of a turn the long way round when -90 is a quarter turn
    the short way. So AZ is filled with the representation of the bearing
    nearest where the antenna is now, inside the limits in force. */
function fillFromPlot(ev) {
  if (shape !== 'on' || !$('ctl-az')) return;
  const canvas = ev.currentTarget;
  const rect = canvas.getBoundingClientRect();
  // PolarPlot.draw()'s own geometry: centre of the canvas, horizon ring
  // POLAR_INSET_PX inside the shorter side, zenith in the middle, north up.
  const w = Math.round(rect.width);
  const h = Math.round(rect.height);
  const r = Math.min(w, h) / 2 - POLAR_INSET_PX;
  if (r <= 10) return;
  const dx = ev.clientX - rect.left - w / 2;
  const dy = ev.clientY - rect.top - h / 2;
  const d = Math.hypot(dx, dy);
  if (d > r) return;                     // outside the horizon: not a direction

  const lim = limits || FALLBACK_LIMITS;
  const bearing = half(((Math.atan2(dx, -dy) * 180) / Math.PI + 360) % 360);
  const az = nearestBranch(bearing, store.rotator?.az_raw ?? 0, lim.min_az, lim.max_az);
  const elLo = Math.max(0, lim.min_el);
  const elHi = Math.min(90, lim.max_el);
  const el = half(Math.min(elHi, Math.max(elLo, 90 * (1 - d / r))));

  $('ctl-az').value = String(az);
  $('ctl-el').value = String(el);
  setNotice('filled from plot — press GO to move', 'fill');
}

const half = (v) => Math.round(v * 2) / 2;

/** The representation of `bearing` (0..360) nearest `from`, among those inside
    [lo, hi]. On a tie — the antenna exactly opposite — the one nearer the
    middle of the range, which leaves the cable less wound. */
function nearestBranch(bearing, from, lo, hi) {
  const mid = (lo + hi) / 2;
  let best = null;
  for (let k = -2; k <= 2; k += 1) {
    const cand = bearing + 360 * k;
    if (cand < lo || cand > hi) continue;
    const better = best === null
      || Math.abs(cand - from) < Math.abs(best - from)
      || (Math.abs(cand - from) === Math.abs(best - from)
          && Math.abs(cand - mid) < Math.abs(best - mid));
    if (better) best = cand;
  }
  // A range narrower than a turn may hold no representation at all; the
  // nearest end is then the closest the antenna can get.
  return best ?? Math.min(hi, Math.max(lo, bearing));
}

// ---------------------------------------------------------------------------
// keyboard

/** Enter and Space on a focused button are that button's click, and the
    browser sends them wherever focus happens to be. A button clicked with
    the mouse keeps focus, with no ring to say so. Unguarded, a RELEASE
    clicked an hour ago made the next stray Enter an ARM — a fifteen-minute
    lease nobody decided to take — the same key again a RELEASE, which stops
    the antenna and disengages autopilot, and a held Enter on EXTEND one
    extend per auto-repeat.

    So a control button answers a key only when Tab put the focus on it: the
    keyboard user's own path, which rings the button it lands on, so they can
    see what Enter will press. And a held key is one press, as it is for every
    binding in keys.js. Anything else is swallowed before the browser can make
    it a click, and the focus goes with it — Chromium rings a clicked button
    the moment a key is pressed on it, and a ring that a second Enter would
    not honour is the panel saying one thing and doing another.

    Keyed on how focus arrived, not on dropping focus after a click: a press
    dragged off the button is a decision not to, never becomes a click, and
    leaves the button focused all the same. So does closing the key list,
    which hands focus back by Esc, not by Tab. */
function guardKeys(ev) {
  if (ev.key !== 'Enter' && ev.key !== ' ') return;
  const btn = ev.target.closest?.('.ctl-btn');
  if (!btn) return;
  if (btn !== tabbedTo) {
    ev.preventDefault();
    btn.blur();
  } else if (ev.repeat) {
    ev.preventDefault();
  }
}

function bindKeys() {
  bindKey({
    combo: 'Shift+S',
    label: 'STOP the rotator',
    // Not while control is switched off: there is no STOP button then, and
    // whatever is driving the antenna is not this dashboard.
    when: () => Boolean(store.control?.enabled),
    run: keyStop,
  });
  bindKey({
    combo: 'Shift+E',
    label: 'EXTEND the lease (only while armed)',
    when: () => Boolean(store.control?.enabled && store.control?.armed),
    run: extend,
  });
  bindKey({
    combo: 'Shift+D',
    label: 'DISENGAGE autopilot',
    when: () => Boolean(store.autopilot?.enabled),
    run: disengage,
  });
  bindKey({
    combo: 'F',
    label: 'full screen on / off',
    when: () => Boolean(document.fullscreenEnabled),
    run: toggleFullscreen,
  });
}

function toggleFullscreen() {
  const pending = document.fullscreenElement
    ? document.exitFullscreen()
    : document.documentElement.requestFullscreen();
  // Refused when the page is not allowed it (an iframe, a kiosk policy);
  // there is nothing to tell the operator that the screen does not show.
  pending?.catch?.((err) => console.warn('[keys] fullscreen', err));
}

/** STOP from the keyboard. The button is never confirmed, and nor is this
    while SatNOGS is known to be idle. But while satnogs-client is connected
    it may be the one driving — mid-recording — and a key is far easier to
    press by accident than a button is to click, so the key asks once more. */
function keyStop() {
  const now = Date.now();
  const satnogsMayDrive = store.control?.gates?.satnogs_idle === false;
  if (satnogsMayDrive && now - stopAskedAt > STOP_CONFIRM_MS) {
    stopAskedAt = now;
    setNotice('SatNOGS may be driving — press Shift+S again to STOP');
    clearTimeout(stopAskTimer);
    stopAskTimer = setTimeout(() => {
      if (notice.text.startsWith('SatNOGS may be driving')) setNotice('');
    }, STOP_CONFIRM_MS);
    return;
  }
  stopAskedAt = 0;
  clearTimeout(stopAskTimer);
  run(api.stopRotator());
}

async function disengage() {
  try {
    set('autopilot', await api.setAutopilot(false));
  } catch (err) {
    setNotice(`DISENGAGE failed — ${err instanceof Refused ? (err.reason || err.message) : (err.message || err)}`);
  }
}

// ---------------------------------------------------------------------------
// lease

/** The lease is a countdown, so it has to tick even when nothing else changes.

    While autopilot is engaged it also says when autopilot will stand down —
    it never extends a lease itself, so lease expiry is its end — and, if that
    falls before the LOS of the pass it is working, by how much. That is the
    moment to press EXTEND, and the reason to. */
function paintLease() {
  const el = $('ctl-lease');
  const expires = store.control?.lease_expires_at;
  if (!el) return;
  if (!expires) {
    el.textContent = '';
    el.className = 'ctl-lease';
    return;
  }

  const expiresMs = Date.parse(expires);
  const left = Math.max(0, (expiresMs - Date.now()) / 1000);
  const mm = String(Math.floor(left / 60)).padStart(2, '0');
  const ss = String(Math.floor(left % 60)).padStart(2, '0');
  const ap = left > 0 && store.autopilot?.enabled ? standDown(expiresMs) : '';

  el.textContent = left > 0 ? `lease ${mm}:${ss}` : 'lease expired';
  if (ap) {
    const span = document.createElement('span');
    span.className = 'ctl-lease-ap';
    span.textContent = ap;
    el.appendChild(span);
  }
  el.className = 'ctl-lease';
  el.classList.toggle('warn', left >= LEASE_EXPIRING_S && left < LEASE_WARN_S);
  el.classList.toggle('expiring', left > 0 && left < LEASE_EXPIRING_S);
  el.classList.toggle('ap', Boolean(ap));
}

function standDown(expiresMs) {
  const tz = store.config?.station?.timezone || 'UTC';
  let text = ` · autopilot stands down ${hmsLocal(new Date(expiresMs), tz)}`;

  const now = Date.now();
  const planned = store.plan?.planned || [];
  const cur = store.autopilot?.current;
  const pass = (cur && planned.find((p) => p.key === cur))
            || planned.find((p) => Date.parse(p.los) > now);
  const losMs = pass ? Date.parse(pass.los) : NaN;
  if (Number.isFinite(losMs) && losMs > expiresMs) {
    text += ` · lease ends ${duration(losMs - expiresMs)} before LOS of ${pass.name || `#${pass.norad}`}`
          + ` ${dayPrefix(losMs, tz)}${shortTime(pass.los, tz)}`;
  }
  return text;
}

/** "12 min" for the near case this is mostly about; hours past that, since
    the next planned pass is often tomorrow's, and "1415 min" says nothing. */
function duration(ms) {
  const min = Math.ceil(ms / 60_000);
  if (min < 90) return `${min} min`;
  return `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, '0')} min`;
}

/** A LOS on another day than today, in the station's zone, gets its weekday:
    "19:08" alone would read as this evening. Same rule as the plan list. */
function dayPrefix(ms, tz) {
  try {
    const day = new Intl.DateTimeFormat('en-CA', { timeZone: tz, dateStyle: 'short' });
    if (day.format(ms) === day.format(Date.now())) return '';
    return `${new Intl.DateTimeFormat('en-GB', { timeZone: tz, weekday: 'short' }).format(ms)} `;
  } catch {
    return '';
  }
}

export function tickControl() {
  paintLease();
}
