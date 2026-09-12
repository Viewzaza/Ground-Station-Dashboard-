/* Boot.

   Order matters: config first (the whole UI is driven by it), then the cameras,
   because they are the operator's primary view and should appear before the
   slower orbit work finishes.

   The render loop runs at 1 Hz, not per animation frame. Nothing on this
   display changes faster than that, and a wall panel runs for months. */

import { api } from './core/api.js';
import { store, set, setStatus } from './core/store.js';
import { Orbit } from './lib/orbit.js';
import { Map2D } from './panels/map2d.js';
import { PolarPlot, paintRotatorReadout } from './panels/rotator.js';
import { mountCameras } from './panels/cameras.js';
import { mountHeader } from './panels/header.js';
import { mountSatSelect } from './panels/satselect.js';
import { mountPasses } from './panels/passes.js';
import { mountGrafana } from './panels/grafana.js';

let orbit = null;
let map = null;
let polar = null;
let globe = null;

async function boot() {
  // --- config ------------------------------------------------------------
  try {
    set('config', await api.config());
    setStatus('api', 'ok');
  } catch (err) {
    console.error('[boot] backend unreachable:', err);
    setStatus('api', 'down', String(err));
    return;
  }

  mountHeader();
  mountGrafana();
  mountPasses();

  // Cameras first — they are why the operator is looking at this screen.
  mountCameras();

  map = new Map2D(document.getElementById('map2d'));
  polar = new PolarPlot(document.getElementById('polar'));

  // Cesium is 23 MB. Load it after the panels that matter are already up,
  // and only when this display is wide enough to be showing the globe.
  if (store.config.features?.globe3d && window.innerWidth > 1000) {
    import('./panels/globe3d.js').then(async (mod) => {
      globe = mod;
      await mod.mountGlobe();
    }).catch((err) => console.warn('[globe3d]', err));
  }

  await mountSatSelect(selectSatellite);
  await selectSatellite(store.config.default_norad);

  // --- loops -------------------------------------------------------------
  setInterval(tick, 1000);
  setInterval(refreshPass, 60_000);
  window.addEventListener('resize', debounce(() => {
    map?.draw();
    polar?.draw();
    globe?.resizeGlobe();
  }, 150));
  tick();
}

async function selectSatellite(norad) {
  try {
    const tle = await api.tle(norad);
    set('tle', tle);
    set('satellite', { norad, name: tle.name });
    setStatus('tle', tle.state === 'stale' ? 'down'
                   : tle.state === 'aging' ? 'degraded' : 'ok');

    orbit = new Orbit(
      { tle1: tle.tle1, tle2: tle.tle2, name: tle.name, norad },
      store.config.station,
    );
    set('groundTrack', orbit.groundTrack());
    await refreshPass();
    tick();
  } catch (err) {
    console.error('[select]', err);
    setStatus('tle', 'down', String(err));
  }
}

async function refreshPass() {
  const norad = store.satellite?.norad;
  if (!norad) return;
  try {
    const next = await api.nextPass(norad);
    set('nextPass', next);
    if (next) {
      const track = await api.passTrack(next.pass_id);
      polar?.setTrack(track.samples);
    } else {
      polar?.setTrack(null);
    }
  } catch (err) {
    console.warn('[passes]', err);
  }
}

let trackRefreshedAt = 0;

function tick() {
  if (orbit) {
    const downlink = store.satellite?.norad === 67683 ? 400_630_000 : null;
    const sample = orbit.sample(new Date(), downlink);
    if (sample) set('satpos', sample);

    // The ground track only needs rebuilding as the window slides.
    if (Date.now() - trackRefreshedAt > 60_000) {
      set('groundTrack', orbit.groundTrack());
      trackRefreshedAt = Date.now();
    }
  }
  map?.draw();
  polar?.draw();
  globe?.updateGlobe(orbit);
  paintRotatorReadout();
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

boot();
