/* Drives frontend/js/panels/control.js on the fake page and reports what got
   sent. Run by tests/test_control_panel_keys.py, one scenario per process so
   no module state carries over:

     node control_panel.mjs <scenario>

   prints one JSON object: each step's name, the requests it caused and where
   focus was left. The assertions live in the Python test, next to the reasons
   for them. */

import { Page, user } from './fake_page.mjs';

const page = new Page().install();
const polar = page.add('canvas', 'polar', { left: 0, top: 0, width: 400, height: 300 });
page.add('div', 'rot-control');

const LIMITS = { min_az: -90, max_az: 450, min_el: 0, max_el: 100 };

function controlState(armed) {
  return {
    enabled: true,
    armed,
    mode: 'idle',
    gates: { kill_switch: true, satnogs_idle: true, no_imminent_pass: true, armed },
    blocked_by: armed ? [] : ['armed'],
    lease_expires_at: armed ? new Date(Date.now() + 900_000).toISOString() : null,
    limits: LIMITS,
  };
}

const scenario = process.argv[2];
let state = controlState(scenario !== 'pointer-arm-then-keys' && scenario !== 'fill-from-plot');

// The backend, as far as the panel can tell: ARM and EXTEND leave it armed,
// RELEASE does not, everything else answers with the state unchanged.
globalThis.fetch = async (url, opts = {}) => {
  const path = new URL(url).pathname;
  page.requests.push(`${opts.method || 'GET'} ${path}`);
  if (path === '/api/control/arm' || path === '/api/control/extend') state = controlState(true);
  if (path === '/api/control/release') state = controlState(false);
  const body = structuredClone(state);
  return { ok: true, status: 200, json: async () => body, text: async () => '' };
};

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

const { store } = await import('../../../frontend/js/core/store.js');
store.config = { station: { timezone: 'UTC' } };
const { mountKeys } = await import('../../../frontend/js/core/keys.js');
const { mountControl } = await import('../../../frontend/js/panels/control.js');
mountKeys();
mountControl();
await settle();

const $ = (id) => document.getElementById(id);
const u = user(page);
const steps = [];

/** Runs one action and records the POSTs it caused. */
async function step(name, action) {
  const before = page.requests.length;
  action();
  await settle();
  await settle();
  steps.push({
    step: name,
    posts: page.requests.slice(before).filter((r) => r.startsWith('POST ')).map((r) => r.slice(5)),
    focus: page.activeElement.id || page.activeElement.tagName,
    armed: Boolean(store.control?.armed),
  });
}

function tabTo(id) {
  for (let i = 0; i < 20 && page.activeElement.id !== id; i += 1) u.key('Tab');
  if (page.activeElement.id !== id) throw new Error(`Tab never reached #${id}`);
}

const notice = () => ({ hidden: $('ctl-notice').hidden, text: $('ctl-notice').textContent });

switch (scenario) {
  case 'pointer-arm-then-keys':
    await step('click ARM', () => u.click($('ctl-arm')));
    await step('Enter', () => u.key('Enter'));
    await step('Space', () => u.key(' '));
    await step('click RELEASE', () => u.click($('ctl-arm')));
    await step('Enter after RELEASE', () => u.key('Enter'));
    break;

  case 'pointer-extend-then-held-enter':
    await step('click EXTEND', () => u.click($('ctl-extend')));
    await step('Enter held', () => u.key('Enter', { repeats: 4 }));
    await step('click GO', () => u.click($('ctl-goto')));
    await step('Enter after GO', () => u.key('Enter'));
    break;

  case 'press-and-drag-away':
    await step('press RELEASE, drag off', () => u.pressAndDragAway($('ctl-arm')));
    await step('Enter', () => u.key('Enter'));
    await step('Space', () => u.key(' '));
    break;

  case 'tab-then-keys':
    await step('Tab to EXTEND', () => tabTo('ctl-extend'));
    await step('Enter', () => u.key('Enter'));
    await step('Enter held', () => u.key('Enter', { repeats: 4 }));
    await step('Space held', () => u.key(' ', { repeats: 4 }));
    await step('click EXTEND, then Shift+Tab and Tab back', () => {
      u.click($('ctl-extend'));
      u.key('Tab', { shift: true });
      u.key('Tab');
    });
    await step('Enter after Tab back', () => u.key('Enter'));
    await step('click EXTEND while Tabbed to it', () => u.click($('ctl-extend')));
    await step('Enter after that click', () => u.key('Enter'));
    break;

  case 'help-list-hands-focus-back':
    await step('Tab to EXTEND', () => tabTo('ctl-extend'));
    await step('?', () => u.key('?', { shift: true }));
    await step('Esc', () => u.key('Escape'));
    await step('Enter', () => u.key('Enter'));
    break;

  case 'fill-from-plot': {
    const before = notice();
    // Due west, half way out: bearing 270, elevation 45.
    const r = Math.min(400, 300) / 2 - 12;
    await step('click the plot', () => u.click(polar, { clientX: 200 - r / 2, clientY: 150 }));
    steps.push({ step: 'notice', before, after: notice(), az: $('ctl-az').value, el: $('ctl-el').value });
    break;
  }

  default:
    throw new Error(`no scenario ${scenario}`);
}

console.log(JSON.stringify({ scenario, steps }));
