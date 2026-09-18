/* Next-pass card.

   The numbers come from the backend (Skyfield), never from the browser's own
   SGP4 — the antenna is driven from the backend's schedule, and two predictors
   would eventually disagree. */

import { bus } from '../core/bus.js';
import { store } from '../core/store.js';
import { countdown, deg, shortTime } from '../core/format.js';

const card = () => document.getElementById('pass-card');

export function mountPasses() {
  bus.on('nextPass', render);
  setInterval(render, 1000);
  render();
}

function render() {
  const pass = store.nextPass;
  const el = card();

  if (!pass) {
    el.replaceChildren(Object.assign(document.createElement('p'), {
      className: 'muted',
      textContent: store.tle ? 'no pass in the next 24 h' : 'no prediction yet',
    }));
    return;
  }

  const tz = store.config?.station?.timezone || 'UTC';
  const now = Date.now();
  const aos = new Date(pass.aos).getTime();
  const los = new Date(pass.los).getTime();
  const inPass = now >= aos && now < los;
  const progress = inPass ? (now - aos) / (los - aos) : 0;

  el.replaceChildren();
  el.appendChild(row('AOS', `${shortTime(pass.aos, tz)}  ${countdown((aos - now) / 1000)}`));
  el.appendChild(row('TCA', shortTime(pass.tca, tz)));
  el.appendChild(row('LOS', `${shortTime(pass.los, tz)}  ${countdown((los - now) / 1000)}`));

  const maxEl = row('MAX EL', deg(pass.max_el, 1));
  const maxElValue = maxEl.querySelector('b');
  maxElValue.classList.add('pass-el');
  // Below the station's culmination threshold SatNOGS will not schedule the
  // pass, so flag it rather than letting it look like a normal opportunity.
  const floor = store.config?.station?.min_culmination_deg ?? 10;
  if (pass.max_el < floor) {
    maxElValue.classList.add('low');
    maxElValue.title = `below the station's ${floor}° scheduling threshold`;
  }
  el.appendChild(maxEl);

  el.appendChild(row('AZ', `${deg(pass.aos_az, 0)} → ${deg(pass.los_az, 0)}`));
  el.appendChild(row('DURATION', `${Math.round(pass.duration_s / 60)} min`));

  const bar = document.createElement('div');
  bar.className = 'pass-bar';
  const fill = document.createElement('i');
  fill.style.width = `${Math.max(0, Math.min(1, progress)) * 100}%`;
  bar.appendChild(fill);
  el.appendChild(bar);
}

function row(label, value) {
  const div = document.createElement('div');
  div.className = 'pass-row';
  const s = document.createElement('span');
  s.textContent = label;
  const b = document.createElement('b');
  b.textContent = value;
  div.append(s, b);
  return div;
}
