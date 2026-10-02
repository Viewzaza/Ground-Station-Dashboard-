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
   holding the socket. */

import { api, Refused } from '../core/api.js';
import { store, set } from '../core/store.js';
import { bus } from '../core/bus.js';

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

let host = null;
let notice = '';

export function mountControl() {
  host = $('rot-control');
  if (!host) return;

  bus.on('control', render);
  render();

  api.control()
    .then((state) => set('control', state))
    .catch((err) => console.warn('[control]', err));
}

function render() {
  if (!host) return;
  const c = store.control;

  if (!c) {
    host.innerHTML = '<span class="muted">control state unknown</span>';
    return;
  }

  if (!c.enabled) {
    // Not an error state: control being off is the normal deployment default.
    host.innerHTML = `
      <div class="ctl-off">
        <span class="muted">rotator control disabled</span>
        <span class="ctl-hint">set GS_ROTATOR_CONTROL_ENABLED=1 to enable</span>
      </div>`;
    return;
  }

  const gates = c.gates || {};
  const clear = (c.blocked_by || []).length === 0;

  host.innerHTML = `
    <div class="ctl">
      <div class="ctl-gates">
        ${Object.entries(GATE_LABELS).map(([key, label]) => `
          <span class="gate ${gates[key] ? 'gate-ok' : 'gate-shut'}"
                title="${gates[key] ? 'open' : (GATE_WHY[key] || '')}">
            ${gates[key] ? '●' : '○'} ${label}
          </span>`).join('')}
        <span class="ctl-lease" id="ctl-lease"></span>
      </div>

      <div class="ctl-row">
        <label>AZ <input id="ctl-az" type="number" step="0.5" min="-180" max="540" value="0"></label>
        <label>EL <input id="ctl-el" type="number" step="0.5" min="-20" max="210" value="0"></label>
        <button id="ctl-goto" class="ctl-btn" ${clear ? '' : 'disabled'}>GO</button>
      </div>

      <div class="ctl-row">
        <button id="ctl-arm" class="ctl-btn ${c.armed ? 'armed' : ''}">${c.armed ? 'RELEASE' : 'ARM'}</button>
        <button id="ctl-track" class="ctl-btn ${c.mode === 'track' ? 'active' : ''}"
                ${clear ? '' : 'disabled'}>TRACK</button>
        <button id="ctl-park" class="ctl-btn" ${clear ? '' : 'disabled'}>PARK</button>
        <button id="ctl-stop" class="ctl-btn stop">STOP</button>
      </div>

      ${notice ? `<div class="ctl-notice">${notice}</div>` : ''}
    </div>`;

  $('ctl-arm').onclick = () => run(c.armed ? api.release() : api.arm());
  $('ctl-goto').onclick = () => run(api.goto(num('ctl-az'), num('ctl-el')));
  $('ctl-track').onclick = () => run(api.track(store.satellite?.norad ?? null));
  $('ctl-park').onclick = () => run(api.park());
  $('ctl-stop').onclick = () => run(api.stopRotator());

  paintLease();
}

function num(id) {
  const value = Number($(id)?.value);
  return Number.isFinite(value) ? value : 0;
}

async function run(promise) {
  try {
    notice = '';
    set('control', await promise);
  } catch (err) {
    // A refusal names the gates, so the operator is told what to fix rather
    // than that "it didn't work".
    notice = err instanceof Refused
      ? `refused — ${(err.blockedBy || []).map((g) => GATE_LABELS[g] || g).join(', ') || err.message}`
      : String(err.message || err);
    // Re-read rather than trusting the panel's idea of state after a failure.
    try {
      set('control', await api.control());
    } catch {
      render();
    }
  }
}

/** The lease is a countdown, so it has to tick even when nothing else changes. */
function paintLease() {
  const el = $('ctl-lease');
  const expires = store.control?.lease_expires_at;
  if (!el) return;
  if (!expires) { el.textContent = ''; return; }

  const left = Math.max(0, (new Date(expires) - Date.now()) / 1000);
  const mm = String(Math.floor(left / 60)).padStart(2, '0');
  const ss = String(Math.floor(left % 60)).padStart(2, '0');
  el.textContent = left > 0 ? `lease ${mm}:${ss}` : 'lease expired';
  el.classList.toggle('expiring', left > 0 && left < 60);
}

export function tickControl() {
  paintLease();
}
