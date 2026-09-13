/* Single mutable state object. Panels read from it; only the transport and
   panel actions write to it, always through set(). */

import { bus } from './bus.js';

export const store = {
  config: null,
  status: {},          // component -> 'ok' | 'degraded' | 'down'
  satellite: null,     // {norad, name}
  catalog: [],
  tle: null,
  satpos: null,        // browser-propagated, 1 Hz
  groundTrack: [],
  nextPass: null,
  passes: [],
  rotator: null,
  pointing: null,
  satnogs: null,
  cameras: [],
};

export function set(key, value) {
  store[key] = value;
  bus.emit(key, value);
}

export function setStatus(component, state, detail = '') {
  if (store.status[component] === state) return;
  store.status[component] = state;
  bus.emit('status', { component, state, detail });
}
