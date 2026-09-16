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
  radio: null,         // transmitters + live Doppler for the tracked satellite
  waterfall: null,     // most recent observation's cropped signal image
  rig: null,           // what the station's receiver is tuned to, live
  control: null,       // interlock state: gates, lease, mode
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
