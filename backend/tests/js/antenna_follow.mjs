/* Drives frontend/js/panels/antenna.js in Node and reports what the display
   did. Run by tests/test_antenna_follow.py, one scenario per process so no
   module state carries over:

     node antenna_follow.mjs <scenario>

   prints one JSON object: each step's name, the selection, the chip, the
   ANTENNA line and the selections following asked for. The assertions live
   in the Python test, next to the reasons for them.

   The module is vanilla ES with no build step, so Node imports it as it
   stands. What Node lacks is the page, the clock and main.js's
   selectSatellite; this file is those three, cut down to what antenna.js
   touches:

   - a page of four elements, by id, with the few properties painted;
   - a virtual clock: Date.now and setInterval answer to `advance`, so ten
     idle minutes take no time and every timing is exact;
   - `select`, which does what selectSatellite does in the order it does it:
     ask the backend for elements, which may be slow or fail; if the caller
     passed `wanted`, ask it again once they are here; then write `tle` and
     `satellite` to the store. A failure is swallowed, as selectSatellite
     swallows it. A manual pick goes through main.js's wrapper, which pins
     with pauseFollow and then selects — modelled by `pick`. */

const KN = 67683;
const ISS = 25544;
const XIV = 28895;
const NAMES = { [KN]: 'KNACKSAT-2', [ISS]: 'ISS (ZARYA)', [XIV]: 'CUBESAT XI-V' };
const IDLE_MS = 60_000;

/* --- the clock ------------------------------------------------------------ */
let now = Date.parse('2026-10-10T12:00:00Z');
Date.now = () => now;
const timers = [];
globalThis.setInterval = (fn, ms) => { timers.push({ fn, ms, due: now + ms }); return timers.length; };

const settle = async () => {
  for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

/** Elements that arrive at a virtual time: [{due, resolve}]. */
const responses = [];

/** Moves the clock on, running whatever falls due in time order. */
async function advance(ms) {
  const end = now + ms;
  for (;;) {
    const next = [...timers.map((t) => t.due), ...responses.map((r) => r.due)]
      .filter((d) => d <= end).sort((x, y) => x - y)[0];
    if (next === undefined) break;
    now = next;
    for (let i = responses.length - 1; i >= 0; i -= 1) {
      if (responses[i].due <= now) responses.splice(i, 1)[0].resolve();
    }
    await settle();
    for (const t of timers) {
      if (t.due <= now) { t.due += t.ms; t.fn(); }
    }
    await settle();
  }
  now = end;
  await settle();
}

/* --- the page ------------------------------------------------------------- */
class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.id = '';
    this.className = '';
    this.textContent = '';
    this.title = '';
    this.hidden = false;
    this.tabIndex = -1;
    this.attrs = {};
    this.children = [];
    this.listeners = {};
  }

  setAttribute(k, v) { this.attrs[k] = String(v); }

  removeAttribute(k) { delete this.attrs[k]; }

  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }

  replaceChildren(...kids) { this.children = kids; }

  get lastElementChild() { return this.children[this.children.length - 1] || null; }

  querySelector(sel) {
    const cls = sel.replace(/^\./, '');
    const walk = (el) => {
      for (const c of el.children) {
        if (c.className.split(/\s+/).includes(cls)) return c;
        const hit = walk(c);
        if (hit) return hit;
      }
      return null;
    };
    return walk(this);
  }

  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }

  fire(type, ev = {}) { for (const fn of this.listeners[type] || []) fn({ preventDefault() {}, ...ev }); }
}

const ids = {};
for (const id of ['ant-owner', 'ant-activity', 'ant-follow', 'ant-strip']) {
  ids[id] = new El(id === 'ant-owner' ? 'b' : 'span');
  ids[id].id = id;
}
ids['ant-follow'].hidden = true;

const windowListeners = {};
globalThis.window = {
  addEventListener(type, fn) { (windowListeners[type] ||= []).push(fn); },
};
globalThis.document = {
  getElementById: (id) => ids[id] || null,
  createElement: (tag) => new El(tag),
  querySelector: () => null,
};
globalThis.location = { search: `?followIdleMs=${IDLE_MS}` };
const saved = {};
globalThis.localStorage = {
  getItem: (k) => (k in saved ? saved[k] : null),
  setItem: (k, v) => { saved[k] = String(v); },
};

/* --- the app around the panel --------------------------------------------- */
const { store, set, setStatus } = await import('../../../frontend/js/core/store.js');
const antenna = await import('../../../frontend/js/panels/antenna.js');

const selects = [];             // every selection following asked for
const behaviour = {};           // norad -> {delay, fail}: how the backend answers

/** selectSatellite, step for step (see the header). */
async function select(norad, { wanted } = {}) {
  const how = behaviour[norad] || {};
  const call = { norad, at: now, outcome: 'pending' };
  if (!select.manual) selects.push(call);
  await new Promise((resolve) => {
    if (how.delay) responses.push({ due: now + how.delay, resolve });
    else resolve();
  });
  if (how.fail > 0) {
    how.fail -= 1;
    call.outcome = 'failed';
    setStatus('tle', 'down', 'TypeError: Failed to fetch');
    return;
  }
  if (wanted && !wanted()) { call.outcome = 'dropped'; return; }
  set('tle', { norad, name: NAMES[norad] });
  set('satellite', { norad, name: NAMES[norad] });
  call.outcome = 'landed';
}

/** A pick in the selector: main.js's wrapper, which pins and then selects. */
async function pick(norad) {
  for (const fn of windowListeners.pointerdown || []) fn();
  antenna.pauseFollow(norad);
  select.manual = true;
  const p = select(norad);
  select.manual = false;
  return p;
}

function frame(focus, extra = {}) {
  set('antenna', {
    owner: 'autopilot', standby: false, warn: false,
    activity: `tracking ${NAMES[focus] || `#${focus}`}`,
    norad: focus, name: NAMES[focus] || null, until: null, job_id: null,
    focus_norad: focus, focus_reason: 'track target', focus_has_elements: true,
    focus_source: 'catalogue', satnogs_age_s: 10, ...extra,
  });
}

function control(armed) {
  set('control', { enabled: true, armed, mode: 'idle', gates: {}, blocked_by: [] });
}

const chip = ids['ant-follow'];
const steps = [];

function report(name) {
  steps.push({
    step: name,
    t: (now - start) / 1000,
    sel: store.satellite?.norad ?? null,
    chip: chip.hidden ? null : chip.textContent,
    chipKind: chip.hidden ? null : chip.className.replace('ant-follow', '').trim(),
    chipButton: !chip.hidden && chip.getAttribute('role') === 'button',
    chipTitle: chip.hidden ? null : chip.title,
    owner: ids['ant-owner'].textContent,
    ownerClass: ids['ant-owner'].className,
    ownerTitle: ids['ant-owner'].title,
    activity: ids['ant-activity'].querySelector('.ant-text')?.textContent ?? null,
    selects: selects.map((c) => `${c.norad}:${c.outcome}`),
  });
}

/* --- boot, as main.js does it -------------------------------------------- */
const start = now;
store.config = { default_norad: KN };
set('catalog', Object.entries(NAMES).map(([n, name]) => ({ norad: Number(n), name })));
setStatus('api', 'ok');
antenna.mountAntenna({ select });
control(false);
set('autopilot', { enabled: false, phase: 'off' });
frame(KN, { owner: 'none', activity: 'idle', focus_reason: 'default' });
select.manual = true;
await select(KN);
select.manual = false;
await settle();

const scenario = process.argv[2];
switch (scenario) {
  case 'basics': {
    // Unarmed, the wall follows the antenna, a pick pins, and ten untouched
    // minutes bring following back: the feature as it shipped.
    report('boot');
    frame(XIV);
    await advance(1000);
    report('focus XI-V');
    await pick(ISS);
    await advance(1000);
    report('pick ISS');
    await advance(IDLE_MS + 2000);
    report('idle');
    frame(98329, { focus_has_elements: false, focus_source: 'none', name: null });
    await advance(1000);
    report('focus not in catalogue');
    break;
  }

  case 'armed-pin-idle': {
    // AO-1 as reported: a pick, a lease, and nobody at the screen.
    await pick(ISS);
    control(true);
    await advance(1000);
    report('picked + armed');
    await advance(IDLE_MS + 5000);
    report('idle, still armed');
    // The pin is the operator's choice; clicking the chip is too.
    chip.fire('click');
    await advance(1000);
    report('chip clicked');
    break;
  }

  case 'armed-pin-released': {
    await pick(ISS);
    control(true);
    await advance(IDLE_MS + 5000);
    report('idle, armed');
    control(false);
    await advance(2000);
    report('released');
    break;
  }

  case 'armed-focus-moves': {
    // AO-1, the other half: no pin, an operator driving by hand, and the
    // focus moves on (a SatNOGS job coming up, say).
    control(true);
    frame(XIV, { owner: 'satnogs', standby: true, focus_reason: 'SatNOGS job soon' });
    await advance(3000);
    report('armed, focus XI-V');
    chip.fire('click');
    await advance(1000);
    report('chip clicked');
    frame(ISS, { owner: 'satnogs', standby: true, focus_reason: 'SatNOGS job soon' });
    await advance(3000);
    report('focus ISS, still armed');
    break;
  }

  case 'autopilot-follows': {
    // Autopilot needs a lease to run at all. Engaged, it is what the antenna
    // is doing, and the wall follows it — the reason following exists.
    control(true);
    set('autopilot', { enabled: true, phase: 'tracking' });
    frame(XIV);
    await advance(1000);
    report('autopilot on XI-V');
    await pick(ISS);
    await advance(IDLE_MS + 5000);
    report('pinned, idle, autopilot');
    set('autopilot', { enabled: false, phase: 'off', disengaged_because: 'operator' });
    chip.fire('click');
    await advance(1000);
    report('disengaged, chip clicked');
    break;
  }

  case 'focus-moves-mid-select': {
    // AO-2: the focus moves while the follow's elements are on their way.
    behaviour[XIV] = { delay: 1500 };
    frame(XIV);
    await advance(400);
    frame(ISS);
    await advance(600);
    report('t+1');
    await advance(2000);
    report('t+3');
    await advance(12000);
    report('t+15');
    break;
  }

  case 'pick-mid-select': {
    // AO-3: the operator picks while the follow's elements are on their way.
    behaviour[XIV] = { delay: 3000 };
    frame(XIV);
    await advance(500);
    await pick(ISS);
    await advance(1000);
    report('t+1.5');
    await advance(4000);
    report('t+5.5');
    break;
  }

  case 'transient-failure': {
    // AO-4: one request fails, then the backend is fine again.
    behaviour[XIV] = { fail: 1 };
    frame(XIV);
    await advance(1000);
    report('failed once');
    await advance(5000);
    report('t+6');
    break;
  }

  case 'repeated-failure': {
    behaviour[XIV] = { fail: 99 };
    frame(XIV);
    await advance(1000);
    report('failed');
    await advance(5000);
    report('t+6');
    await advance(30_000);
    report('t+36');
    await advance(30_000);
    report('t+66');
    chip.fire('click');
    await advance(500);
    report('chip clicked');
    setStatus('api', 'down', 'link closed');
    await advance(500);
    setStatus('api', 'ok');
    frame(XIV, { activity: 'tracking CUBESAT XI-V until LOS 12:30Z' });
    await advance(500);
    report('reconnected');
    behaviour[XIV].fail = 0;
    chip.fire('click');
    await advance(500);
    report('healthy, chip clicked');
    break;
  }

  case 'link-lost': {
    // AO-5: the socket closes and the last frame said nobody.
    report('live');
    setStatus('api', 'down', 'link closed');
    await advance(40_000);
    report('link lost');
    setStatus('api', 'ok');
    await advance(500);
    report('reconnected, no frame yet');
    frame(KN, { owner: 'none', activity: 'idle', focus_reason: 'default' });
    await advance(500);
    report('snapshot');
    break;
  }

  default:
    throw new Error(`no scenario ${scenario}`);
}

console.log(JSON.stringify({ scenario, steps }));
process.exit(0);
