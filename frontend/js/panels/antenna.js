/* ANTENNA: who has it, what it is doing, and a display that follows it.

   The backend works out the owner — SatNOGS, autopilot, an operator, nobody,
   or "unknown" when SatNOGS's status is too old to say — and the satellite the
   antenna is working (the focus). This paints that into the header and, on a
   phone scrolled past the header, into a thin strip along the top.

   It also lets the display follow the antenna. With follow on — per device,
   on by default — the selection moves to whatever the antenna is working, so
   the map, the polar plot and the next-pass card describe the pass actually
   being worked rather than whichever satellite this screen last showed. A
   manual pick pins the view instead: the operator who chose ISS keeps ISS.
   The pin lifts when the chip is clicked, or after ten minutes with no pointer
   or key input, which is how a wall display left on someone's pick finds its
   way back by itself.

   Following changes this browser's selection and nothing else. The backend
   ignores select_satellite, and nothing here can move the antenna. But the
   selection is what the control panel's TRACK button sends, so while an
   operator holds the lease and is driving by hand, the selection moves only
   when someone at a screen moves it: no following, and no pin lifting itself.
   The chip says the view is held and offers to follow. Engaged, autopilot is
   what the antenna is doing — it cannot run without a lease — so an unpinned
   screen still follows it; a pin still holds.

   A focus with no elements in the catalogue — SatNOGS schedules some objects
   under temporary catalogue numbers — is never selected: /api/tle has nothing
   for it and the selection would only 404. The chip says so instead. A
   selection that fails for any other reason is tried again after 5 s, 30 s,
   then every two minutes, at once on a reconnect, or when the chip is clicked.

   With the link to the backend gone, the last frame is not news: the line
   says the owner is unknown until the next frame arrives. */

import { bus } from '../core/bus.js';
import { store } from '../core/store.js';
import { countdown } from '../core/format.js';

const $ = (id) => document.getElementById(id);

const FOLLOW_KEY = 'gs.followAntenna';
const IDLE_RESUME_MS = 10 * 60 * 1000;
// After a failed follow: 5 s, 30 s, then every two minutes. A blip costs a few
// seconds; a backend that keeps failing gets one request every two minutes.
const RETRY_MS = [5_000, 30_000, 120_000];

const OWNER_LABEL = {
  satnogs: 'SATNOGS',
  autopilot: 'AUTOPILOT',
  operator: 'OPERATOR',
  commissioning: 'COMMISSIONING',
  unknown: 'UNKNOWN',
  none: 'NOBODY',
};

const OWNER_WHY = {
  satnogs: 'satnogs-client is driving the antenna for a SatNOGS observation',
  autopilot: 'autopilot is working the observation plan, through the interlock',
  operator: 'an operator is driving the antenna from this dashboard',
  commissioning: 'a commissioning run is driving the antenna',
  unknown: "SatNOGS's status is missing or too old to say who has the antenna — not the same as idle",
  none: 'nothing is driving the antenna',
};

let select = null;
let follow = readFollow();
let pinned = null;            // the norad a manual pick pinned the view to
let lastInput = Date.now();
let idleMs = IDLE_RESUME_MS;
let requested = null;         // a follow selection in flight
let landed = false;           // whether that selection ever reached its focus
let dropped = false;          // whether it was called off when its elements came
let failed = null;            // {norad, tries, retryAt}: the focus's last follow failed
let nudged = null;            // a focus the operator clicked to follow under a lease
let linkLost = false;         // the socket closed and no frame has come since
let headerInView = true;
let narrow = null;            // MediaQueryList for the phone layout

/* --- per-device preference ------------------------------------------------
   localStorage can throw (private windows, blocked site data) or be empty.
   Either way the answer is the default, and following is the default. */
function readFollow() {
  try {
    return localStorage.getItem(FOLLOW_KEY) !== '0';
  } catch {
    return true;
  }
}

function writeFollow(on) {
  try {
    localStorage.setItem(FOLLOW_KEY, on ? '1' : '0');
  } catch {
    /* not persisted: this screen still follows the choice until reload */
  }
}

/* --- mount ------------------------------------------------------------------ */
export function mountAntenna({ select: onSelect } = {}) {
  select = onSelect || null;

  const fromUrl = Number(new URLSearchParams(location.search).get('followIdleMs'));
  if (Number.isFinite(fromUrl) && fromUrl > 0) idleMs = fromUrl;

  buildActivity($('ant-activity'));
  buildStrip($('ant-strip'));

  const chip = $('ant-follow');
  chip?.addEventListener('click', onChip);
  chip?.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onChip(); }
  });

  // Any pointer or key input counts as someone at the screen. Capture phase,
  // so a panel that stops propagation cannot hide it.
  for (const type of ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart']) {
    window.addEventListener(type, () => { lastInput = Date.now(); },
                            { capture: true, passive: true });
  }

  watchHeader();

  bus.on('antenna', () => { linkLost = false; evaluate(); });
  bus.on('satellite', (sat) => {
    if (requested !== null && sat?.norad === requested) landed = true;
    evaluate();
  });
  bus.on('catalog', () => paint());
  // A lease taken or given back, autopilot engaged or not: each changes
  // whether this screen may follow.
  bus.on('control', () => evaluate());
  bus.on('autopilot', () => evaluate());
  // ws.js keeps the last antenna frame when the socket closes, and frames
  // are sent on change only — so a wall whose backend has gone kept saying
  // NOBODY, the stale all-clear the backend refuses to give. The flag holds
  // until the next frame, which the snapshot on reconnecting carries. A
  // reconnect is also a fresh chance for a follow that failed.
  bus.on('status', ({ component, state } = {}) => {
    if (component !== 'api') return;
    if (state === 'down') linkLost = true;
    else if (state === 'ok') failed = null;
    evaluate();
  });

  setInterval(tick, 1000);
  evaluate();
}

/** A manual pick in the selector: pin the view to it.

    Picking the satellite the antenna is already on is following, not a
    reason to stop. With follow switched off on this device there is nothing
    to pin. */
export function pauseFollow(norad) {
  lastInput = Date.now();
  if (!follow) return;
  const focus = store.antenna?.focus_norad;
  pinned = (focus != null && Number(norad) === focus) ? null : Number(norad);
  paint();
}

/* --- following ------------------------------------------------------------- */
function followable(a) {
  // focus_has_elements is true for a job's own elements as well, which are
  // enough for the backend's ERR readout but not for /api/tle. Only a
  // catalogued satellite can be selected without a 404.
  if (!a?.focus_has_elements) return false;
  return (a.focus_source ?? 'catalogue') === 'catalogue';
}

/** Whether an operator holds the control lease. Their TRACK button sends this
    screen's selection, so while it holds, a pin does not lift by itself. */
function armed() {
  return store.control?.armed === true;
}

/** Whether the lease holds the view still: armed, and driving by hand.
    Engaged, autopilot is driving — it cannot run without a lease — and what
    it works is what an unpinned screen should show. */
function held() {
  return armed() && store.autopilot?.enabled !== true;
}

/** Whether following should take this screen to `norad` now.

    Asked before a follow starts, and asked again by selectSatellite once the
    elements are here — seconds later on a busy server, in which time the
    operator may have picked something, armed, or the antenna moved on. A
    stale follow used to land anyway, over the pick it was older than. */
function mayFollow(norad) {
  const a = store.antenna;
  return Boolean(a && select && follow && !linkLost && pinned === null
    && a.focus_norad === norad && followable(a)
    && (!held() || nudged === norad));
}

function evaluate() {
  const a = store.antenna;
  const sel = store.satellite?.norad;
  const focus = a?.focus_norad;
  // A failure and a nudge are about the focus they happened to.
  if (failed !== null && failed.norad !== focus) failed = null;
  if (nudged !== null && nudged !== focus) nudged = null;

  // Not until boot has selected something: its own selection of the default
  // would otherwise land after ours and undo it.
  if (sel != null && focus != null && focus !== sel && requested === null
      && mayFollow(focus) && !(failed !== null && Date.now() < failed.retryAt)) {
    startFollow(focus);
  }
  paint();
}

function startFollow(norad) {
  requested = norad;
  landed = false;
  dropped = false;
  const wanted = () => {
    const ok = mayFollow(norad);
    if (!ok) dropped = true;
    return ok;
  };
  Promise.resolve()
    .then(() => select(norad, { wanted }))
    .catch((err) => console.warn('[antenna] follow failed:', err))
    .finally(() => {
      requested = null;
      // selectSatellite reports its own failures rather than throwing, so
      // the selection is the only evidence — and the evidence is whether it
      // ever reached the focus, not whether it is still there. It resolves
      // only once the pass has loaded, seconds later, and a pick made in
      // between is the operator choosing, not the focus failing. A follow
      // called off when its elements came is not a failure either.
      if (landed) {
        failed = null;
        nudged = null;
      } else if (!dropped) {
        const tries = (failed?.norad === norad ? failed.tries : 0) + 1;
        const wait = RETRY_MS[Math.min(tries, RETRY_MS.length) - 1];
        failed = { norad, tries, retryAt: Date.now() + wait };
      }
      landed = false;
      dropped = false;
      // The focus may have moved while this was in flight, and antenna
      // frames are sent on change only: through a long track none comes.
      // Painting alone left the screen on the old focus with no chip.
      evaluate();
    });
}

function tick() {
  if (pinned !== null && !armed() && Date.now() - lastInput >= idleMs) pinned = null;
  // Also how a failed follow's retry comes round.
  evaluate();
}

function onChip() {
  lastInput = Date.now();
  const state = chipState();
  if (!state?.action) return;
  state.action();
  evaluate();
}

/* --- painting ------------------------------------------------------------- */
function nameOf(norad, a = store.antenna) {
  if (norad == null) return '—';
  if (store.satellite?.norad === norad && store.satellite.name) return store.satellite.name;
  const hit = (store.catalog || []).find((s) => s.norad === norad);
  if (hit) return hit.name;
  if (a?.focus_norad === norad && a.focus_name) return a.focus_name;
  return `#${norad}`;
}

/** The chip's text and what clicking it does, or null when there is nothing
    worth saying — the display already on the antenna's satellite, which is
    the default one.

    Every way of being off the antenna's satellite has a chip: one that
    cannot be followed, a pin, following switched off, a follow that failed,
    a view the lease holds, one on its way. A screen silently off the focus
    looks exactly like one that is following. */
function chipState() {
  const a = store.antenna;
  // With the link gone the focus is as stale as the owner, and the badge
  // already says so.
  if (linkLost || !a || a.focus_norad == null) return null;
  const sel = store.satellite?.norad;
  // Until boot has selected something there is no "showing" to compare with.
  if (sel == null) return null;
  const onFocus = sel === a.focus_norad;
  const shown = nameOf(sel);
  const focusName = nameOf(a.focus_norad, a);
  const why = a.focus_reason || 'focus';

  // The chip names the focus by number: it rides on the eyebrow line, which
  // truncates first, and the activity line beside it already carries the
  // name. The name goes in the title instead.
  const n = a.focus_norad;
  const named = focusName && focusName !== `#${n}` ? `${focusName} (#${n})` : `#${n}`;
  if (onFocus) {
    if (follow && pinned === null && n !== store.config?.default_norad) {
      return {
        kind: 'following',
        text: 'following antenna',
        title: `showing ${focusName} because the antenna is on it (${why}). `
          + 'Click to stop following on this screen.',
        action: () => { follow = false; writeFollow(false); pinned = null; },
      };
    }
    return null;
  }
  if (!followable(a)) {
    return {
      kind: 'blind',
      text: `antenna on #${n} (not in catalogue) — showing ${shown}`,
      title: `the antenna is on ${named}. ` + (a.focus_source === 'job_tle'
        ? 'SatNOGS scheduled it with elements no public element set this station fetches '
          + "carries. ERR is measured against the job's own elements; this screen cannot draw its orbit."
        : 'No elements for it anywhere on this station — nothing to draw or measure against.'),
    };
  }
  if (pinned !== null) {
    return {
      kind: 'pinned',
      text: `pinned: ${shown} · follow antenna`,
      title: `the antenna is on ${focusName} (${why}). Click to follow it — `
        + (armed()
          ? 'it stays pinned while an operator holds the control lease, because TRACK sends the satellite shown.'
          : `or it resumes by itself after ${Math.round(idleMs / 60000) || '<1'} min untouched.`),
      action: () => { pinned = null; nudged = n; failed = null; },
    };
  }
  if (!follow) {
    return {
      kind: 'off',
      text: `antenna on ${focusName} · follow`,
      title: 'this screen is not following the antenna — click to follow it here from now on',
      action: () => { follow = true; writeFollow(true); pinned = null; nudged = n; },
    };
  }
  if (failed?.norad === n) {
    // Not "no elements": the backend says it has them (or this would be the
    // not-in-catalogue chip), so what failed was the request.
    const secs = Math.max(0, Math.ceil((failed.retryAt - Date.now()) / 1000));
    return {
      kind: 'blind',
      // Short enough that "retry" survives the eyebrow line's truncation.
      text: `antenna on #${n} · load failed, retry`,
      title: `the antenna is on ${named}; this screen still shows ${shown}. Following tried to `
        + `select it and the request failed${failed.tries > 1 ? ` ${failed.tries} times running` : ''}; `
        + `it tries again by itself in ${secs} s. Click to try now.`,
      action: () => { failed = null; nudged = n; },
    };
  }
  if (held() && nudged !== n) {
    return {
      kind: 'held',
      // As short as the pinned chip's: the eyebrow line truncates first.
      text: `armed: holding ${shown} · follow`,
      title: `the antenna is on ${focusName} (${why}). An operator holds the control lease, so this `
        + `screen keeps ${shown}: TRACK sends the satellite shown, and following must not change it `
        + 'unasked. Click to follow the antenna.',
      action: () => { nudged = n; },
    };
  }
  // On its way — including a follow the antenna has already moved past,
  // which is called off when it lands and the next one starts.
  if (requested !== null) {
    return {
      kind: 'following',
      text: 'following antenna…',
      title: `the antenna is on ${focusName} (${why}); loading it`,
    };
  }
  return null;
}

function buildActivity(host) {
  if (!host) return;
  host.replaceChildren(
    Object.assign(document.createElement('span'), { className: 'ant-text' }),
    // The countdown changes every second; inside a live region it would be
    // announced every second. The sentence beside it carries the time anyway.
    Object.assign(document.createElement('span'), { className: 'ant-cd' }),
  );
  host.lastElementChild.setAttribute('aria-hidden', 'true');
}

function buildStrip(host) {
  if (!host) return;
  host.setAttribute('aria-hidden', 'true');   // the header block is the announced copy
  host.replaceChildren(
    Object.assign(document.createElement('span'), { className: 'hdr-ant-label', textContent: 'ANTENNA' }),
    Object.assign(document.createElement('b'), { className: 'ant-badge' }),
    Object.assign(document.createElement('span'), { className: 'ant-text' }),
    Object.assign(document.createElement('span'), { className: 'ant-cd' }),
  );
}

function badge(el, a) {
  if (linkLost) {
    // The backend's own rule 1, applied to our own link: what we cannot see
    // is unknown, not idle. The last word goes in the title, marked as such.
    el.textContent = OWNER_LABEL.unknown;
    el.className = 'ant-badge own-unknown';
    el.title = 'the link to the backend is down, so who has the antenna is not known'
      + (a ? ` — last heard: ${OWNER_LABEL[a.owner] || a.owner || '?'}, ${a.activity || ''}` : '');
    return;
  }
  const owner = a?.owner || 'unknown';
  el.textContent = a ? (OWNER_LABEL[owner] || owner.toUpperCase()) : '—';
  el.className = ['ant-badge', a ? `own-${owner}` : '', a?.standby ? 'standby' : '',
                  a?.warn ? 'warn' : ''].filter(Boolean).join(' ');
  el.title = !a ? 'no word from the backend yet'
    : a.warn ? a.activity
    : a.standby ? 'satnogs-client is connected and may start a job at any moment'
    : (OWNER_WHY[owner] || '');
}

function until(a) {
  if (linkLost || !a?.until) return '';
  const secs = (new Date(a.until).getTime() - Date.now()) / 1000;
  return Number.isFinite(secs) && secs > 0 ? countdown(secs) : '';
}

function activityOf(a) {
  if (linkLost) {
    return store.status?.api === 'down'
      ? 'link to the backend lost — owner unknown'
      : 'reconnected — waiting for word of the owner';
  }
  return a ? (a.activity || '') : 'awaiting the backend';
}

function paint() {
  const a = store.antenna;
  const owner = $('ant-owner');
  if (owner) badge(owner, a);

  const host = $('ant-activity');
  const text = host?.querySelector('.ant-text');
  const cd = host?.querySelector('.ant-cd');
  const activity = activityOf(a);
  if (text && text.textContent !== activity) {
    text.textContent = activity;
    text.title = activity;
  }
  if (cd) cd.textContent = until(a);

  const chip = $('ant-follow');
  if (chip) {
    const state = chipState();
    chip.hidden = !state;
    if (state) {
      if (chip.textContent !== state.text) chip.textContent = state.text;
      chip.title = state.title || '';
      chip.className = `ant-follow ${state.kind}`;
      if (state.action) {
        chip.setAttribute('role', 'button');
        chip.tabIndex = 0;
      } else {
        chip.removeAttribute('role');
        chip.removeAttribute('tabindex');
      }
    }
  }

  paintStrip(a, activity);
}

/* --- the phone strip -------------------------------------------------------
   Below 1000 px the header scrolls away with the page, and with it the only
   line saying who has the antenna. The strip is that line, pinned to the top
   edge while the header is out of view. */
function watchHeader() {
  narrow = window.matchMedia?.('(max-width: 1000px)') || null;
  narrow?.addEventListener?.('change', () => paint());
  const hdr = document.querySelector('.hdr');
  if (!hdr || !('IntersectionObserver' in window)) return;
  new IntersectionObserver((entries) => {
    for (const entry of entries) headerInView = entry.isIntersecting;
    paint();
  }).observe(hdr);
}

function paintStrip(a, activity) {
  const strip = $('ant-strip');
  if (!strip) return;
  const show = Boolean((a || linkLost) && narrow?.matches && !headerInView);
  strip.hidden = !show;
  if (!show) return;
  badge(strip.querySelector('.ant-badge'), a);
  const text = strip.querySelector('.ant-text');
  if (text.textContent !== activity) text.textContent = activity;
  strip.querySelector('.ant-cd').textContent = until(a);
}
