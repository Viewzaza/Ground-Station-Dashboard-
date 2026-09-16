/* 3D orbit view.

   This was CesiumJS. Cesium is a 23 MB download for a panel that draws one
   globe, one orbit and two markers, and because it was fetched by a setup
   script rather than committed, a clone that skipped that step showed a black
   rectangle with no explanation. three.js is 1.3 MB, loads as a plain ES module
   with no build step, and everything drawn here comes from data the repository
   already has.

   The globe surface is not an image. The coastlines are drawn once into an
   equirectangular canvas from `assets/ne_110m_land.json` — the same public
   domain Natural Earth outline the 2D map uses — and that canvas becomes the
   sphere's texture. So there is no imagery to download, nothing is fetched at
   runtime, and the land matches the 2D panel exactly rather than being a
   photograph that disagrees with it.

   The frame is earth-fixed, not inertial. An inertial scene with the planet
   spinning underneath looks better in isolation, but this panel sits beside a
   2D ground track and a polar plot, and all three should be answering the same
   question: where is the satellite relative to *our* ground. In an earth-fixed
   frame the 3D track and the 2D track are literally the same line.

   Rendering is on demand. A wall display runs for months, and a
   requestAnimationFrame loop spinning a GPU at 60 Hz to move a marker that
   updates at 1 Hz is just heat.
*/

import { store } from '../core/store.js';
import { footprintRadiusKmOf } from '../lib/globe-helpers.js';

const THREE_URL = '/js/vendor/three/three.module.js';
const LAND_URL = '/assets/ne_110m_land.json';

const EARTH_R = 1;                  // scene units; km are scaled to this
const EARTH_KM = 6371.0;
const TEX_W = 2048;
const TEX_H = 1024;
const ORBIT_MINUTES = 100;          // a little over one LEO revolution
const ORBIT_STEP_S = 20;
const PATH_REBUILD_MS = 60_000;

/* Colours come from css/tokens.css, so the globe, the 2D map and the polar plot
   cannot drift apart. The literals here are fallbacks for the one case the
   stylesheet cannot cover — tokens.css missing — and each names the token it
   mirrors.

   The page around this panel is light. The globe is not, and must not be: it is
   an instrument window on `--void`, and a starfield-and-terminator view on a
   white card reads as a broken image. The `--globe-*` surface colours are lit by
   a directional light, so every one of them is multiplied down before it reaches
   the screen; they are chosen for how they render *after* lighting, which is why
   they look too bright as flat swatches. Do not lighten them to match the page.

   The overlays are the page's three data hues. They are drawn with Basic
   materials, which are unlit, so they land on screen at exactly these values. */
const FALLBACK = {
  ocean:     '#0b2033',   // --globe-ocean
  land:      '#2c4437',   // --globe-land
  coast:     '#496b56',   // --globe-coast
  graticule: '#27455a',   // --globe-grat
  track:     '#0086ad',   // --track     ground track, orbit path
  contact:   '#b4670f',   // --contact   happening now: the satellite
  observer:  '#c42a6e',   // --observer  us, the ground station
};

// Resolved in mountGlobe rather than here: this module is imported dynamically,
// and reading computed styles at module scope would tie the palette to whenever
// that import happens to land.
let COLOR = { ...FALLBACK };

function readPalette() {
  const css = getComputedStyle(document.documentElement);
  const v = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
  return {
    ocean:     v('--globe-ocean', FALLBACK.ocean),
    land:      v('--globe-land', FALLBACK.land),
    coast:     v('--globe-coast', FALLBACK.coast),
    graticule: v('--globe-grat', FALLBACK.graticule),
    track:     v('--track', FALLBACK.track),
    contact:   v('--contact', FALLBACK.contact),
    observer:  v('--observer', FALLBACK.observer),
  };
}

let THREE = null;
let renderer = null;
let scene = null;
let camera = null;
let earth = null;
let host = null;

let satMarker = null;
let satHalo = null;
let trackLine = null;
let orbitLine = null;
let sightLine = null;
let footprint = null;
let sun = null;

let cam = { lat: 20, lon: 100, dist: 3.2 };   // spherical camera, degrees + units
let needsRender = true;
let lastPathBuild = 0;
// main.js holds the module as soon as the dynamic import resolves and starts
// calling updateGlobe from its 1 Hz tick, which can land while mountGlobe is
// still awaiting the texture. Guarding on `renderer` is not enough: that is
// assigned early, so the markers would still be null. Set this last.
let ready = false;

// --------------------------------------------------------------------------
// geometry helpers
// --------------------------------------------------------------------------

/** Geodetic degrees to a point on (or above) the sphere. */
function toVec3(lat, lon, altKm = 0) {
  const r = EARTH_R * (1 + altKm / EARTH_KM);
  const phi = (90 - lat) * Math.PI / 180;
  const theta = (lon + 180) * Math.PI / 180;
  return new THREE.Vector3(
    -r * Math.sin(phi) * Math.cos(theta),
    r * Math.cos(phi),
    r * Math.sin(phi) * Math.sin(theta),
  );
}

/** Subsolar point, for the day/night terminator.

    Low-precision solar position (Astronomical Almanac): good to about an
    arcminute, which is far finer than a terminator drawn across 2048 pixels. */
function subsolarPoint(date) {
  const n = date.getTime() / 86400000 + 2440587.5 - 2451545.0;
  const L = (280.460 + 0.9856474 * n) % 360;
  const g = ((357.528 + 0.9856003 * n) % 360) * Math.PI / 180;
  const lambda = (L + 1.915 * Math.sin(g) + 0.020 * Math.sin(2 * g)) * Math.PI / 180;
  const eps = (23.439 - 0.0000004 * n) * Math.PI / 180;

  const dec = Math.asin(Math.sin(eps) * Math.sin(lambda));
  const ra = Math.atan2(Math.cos(eps) * Math.sin(lambda), Math.cos(lambda));

  // Greenwich hour angle of the sun, via GMST.
  const gmstDeg = (280.46061837 + 360.98564736629 * n) % 360;
  const lon = (((ra * 180 / Math.PI - gmstDeg) % 360) + 540) % 360 - 180;
  return { lat: dec * 180 / Math.PI, lon };
}

// --------------------------------------------------------------------------
// the surface texture
// --------------------------------------------------------------------------

async function buildEarthTexture() {
  const canvas = document.createElement('canvas');
  canvas.width = TEX_W;
  canvas.height = TEX_H;
  const g = canvas.getContext('2d');

  g.fillStyle = COLOR.ocean;
  g.fillRect(0, 0, TEX_W, TEX_H);

  // Graticule first, so coastlines sit on top of it.
  g.strokeStyle = COLOR.graticule;
  g.lineWidth = 1;
  g.beginPath();
  for (let lon = -180; lon <= 180; lon += 30) {
    const x = (lon + 180) / 360 * TEX_W;
    g.moveTo(x, 0); g.lineTo(x, TEX_H);
  }
  for (let lat = -60; lat <= 60; lat += 30) {
    const y = (90 - lat) / 180 * TEX_H;
    g.moveTo(0, y); g.lineTo(TEX_W, y);
  }
  g.stroke();

  try {
    const land = await fetch(LAND_URL).then((r) => r.json());
    g.beginPath();
    for (const feature of land.features || []) {
      const geom = feature.geometry;
      if (!geom) continue;
      const polys = geom.type === 'Polygon' ? [geom.coordinates] : geom.coordinates;
      for (const poly of polys) {
        for (const ring of poly) {
          ring.forEach(([lon, lat], i) => {
            const x = (lon + 180) / 360 * TEX_W;
            const y = (90 - lat) / 180 * TEX_H;
            if (i) g.lineTo(x, y); else g.moveTo(x, y);
          });
          g.closePath();
        }
      }
    }
    g.fillStyle = COLOR.land;
    g.fill();
    g.strokeStyle = COLOR.coast;
    g.lineWidth = 1.6;
    g.stroke();
  } catch (err) {
    // A globe with a graticule and no coastlines is still a usable globe.
    console.warn('[globe3d] coastlines unavailable', err);
  }

  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 4;
  return texture;
}

// --------------------------------------------------------------------------
// mount
// --------------------------------------------------------------------------

export async function mountGlobe(hostId = 'globe3d') {
  host = document.getElementById(hostId);
  if (!host) return null;

  COLOR = readPalette();

  try {
    THREE = await import(THREE_URL);
  } catch (err) {
    host.innerHTML =
      '<p class="cam-placeholder">3D globe unavailable — run tools/fetch_vendor.sh</p>';
    console.warn('[globe3d]', err);
    return null;
  }

  scene = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(42, 1, 0.01, 100);

  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  host.replaceChildren(renderer.domElement);
  renderer.domElement.style.cssText = 'width:100%;height:100%;display:block;cursor:grab';

  const surface = await buildEarthTexture();
  earth = new THREE.Mesh(
    new THREE.SphereGeometry(EARTH_R, 96, 64),
    // The same texture is both map and emissiveMap. The emissive pass is a
    // floor: it puts the coastlines on screen regardless of where the sun is,
    // so the night half of the planet is still a map rather than a black hole.
    // The directional light then adds the day side on top, which is what makes
    // the terminator visible at all. Lighting alone gave a black disc.
    new THREE.MeshPhongMaterial({
      map: surface,
      emissive: 0xffffff,
      emissiveMap: surface,
      emissiveIntensity: 0.55,
      shininess: 8,
      specular: 0x0d1820,
    }),
  );
  scene.add(earth);

  // A thin rim, which is what reads as "atmosphere" at this size without the
  // cost and fragility of a real scattering shader.
  scene.add(new THREE.Mesh(
    new THREE.SphereGeometry(EARTH_R * 1.015, 64, 48),
    new THREE.MeshBasicMaterial({
      color: 0x2b6f8a, transparent: true, opacity: 0.10,
      side: THREE.BackSide,
    }),
  ));

  // Directional light aimed at the subsolar point gives the terminator for
  // free; ambient keeps the night side legible rather than pure black.
  // Ambient is deliberately low: the emissive pass already guarantees the night
  // side is readable, so ambient's only job here is to stop the terminator
  // being a hard black edge. Raising it washes the terminator out entirely.
  sun = new THREE.DirectionalLight(0xffffff, 2.4);
  scene.add(sun);
  scene.add(new THREE.AmbientLight(0xffffff, 0.18));

  // The satellite and everything attached to it are --contact: this is the
  // "happening now" hue, the same one the readouts and in-view badges use.
  satMarker = new THREE.Mesh(
    new THREE.SphereGeometry(0.018, 16, 12),
    new THREE.MeshBasicMaterial({ color: COLOR.contact }),
  );
  satHalo = new THREE.Mesh(
    new THREE.RingGeometry(0.030, 0.038, 32),
    new THREE.MeshBasicMaterial({
      color: COLOR.contact, transparent: true, opacity: 0.6,
      side: THREE.DoubleSide,
    }),
  );
  satMarker.visible = satHalo.visible = false;
  scene.add(satMarker, satHalo);

  // The footprint travels with the satellite and is only ever "now", so it
  // belongs to --contact as well — it is the halo at ground scale. Faint,
  // because it is a large shape and the marker inside it is the reading.
  footprint = new THREE.Line(
    new THREE.BufferGeometry(),
    new THREE.LineBasicMaterial({
      color: COLOR.contact, transparent: true, opacity: 0.45,
    }),
  );

  /* Ground track and orbit path share --track, because they are one thing:
     where the satellite runs relative to our ground. They are told apart by
     weight rather than hue. The ground track is the emphatic one — it is what
     this panel is for, and it is opaque — while the orbit path is context and
     sits at a third of that.

     Opacity has to carry the whole distinction: WebGL ignores
     LineBasicMaterial.linewidth on every desktop driver, so both lines render
     one pixel wide no matter what width is asked for. */
  trackLine = new THREE.Line(
    new THREE.BufferGeometry(),
    new THREE.LineBasicMaterial({ color: COLOR.track }),
  );
  orbitLine = new THREE.Line(
    new THREE.BufferGeometry(),
    new THREE.LineBasicMaterial({
      color: COLOR.track, transparent: true, opacity: 0.32,
    }),
  );

  // Drawn only while the satellite is above the horizon, which makes it a
  // contact line, not a path.
  sightLine = new THREE.Line(
    new THREE.BufferGeometry(),
    new THREE.LineDashedMaterial({
      color: COLOR.contact, dashSize: 0.03, gapSize: 0.02,
      transparent: true, opacity: 0.85,
    }),
  );
  sightLine.visible = false;
  scene.add(footprint, trackLine, orbitLine, sightLine);

  addStation();
  attachControls();
  resizeGlobe();
  startPump();
  ready = true;
  return true;
}

function addStation() {
  const site = store.config?.station;
  if (!site) return;
  const marker = new THREE.Mesh(
    new THREE.SphereGeometry(0.012, 12, 10),
    new THREE.MeshBasicMaterial({ color: COLOR.observer }),
  );
  marker.position.copy(toVec3(site.lat, site.lon, 0));
  scene.add(marker);

  // Point the camera at the station on first paint: an operator opening this
  // wants their own horizon, not a view of the Pacific.
  cam.lat = site.lat;
  cam.lon = site.lon;
}

// --------------------------------------------------------------------------
// interaction
// --------------------------------------------------------------------------

function attachControls() {
  const el = renderer.domElement;
  let dragging = false;
  let lastX = 0;
  let lastY = 0;

  el.addEventListener('pointerdown', (ev) => {
    dragging = true;
    lastX = ev.clientX;
    lastY = ev.clientY;
    el.setPointerCapture(ev.pointerId);
    el.style.cursor = 'grabbing';
  });

  el.addEventListener('pointermove', (ev) => {
    if (!dragging) return;
    cam.lon -= (ev.clientX - lastX) * 0.32;
    cam.lat += (ev.clientY - lastY) * 0.32;
    // Stop just short of the poles: at exactly ±90 the up vector is parallel
    // to the view direction and the camera flips.
    cam.lat = Math.max(-85, Math.min(85, cam.lat));
    lastX = ev.clientX;
    lastY = ev.clientY;
    needsRender = true;
  });

  const release = (ev) => {
    dragging = false;
    el.style.cursor = 'grab';
    if (ev.pointerId !== undefined) el.releasePointerCapture?.(ev.pointerId);
  };
  el.addEventListener('pointerup', release);
  el.addEventListener('pointercancel', release);

  el.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    cam.dist = Math.max(1.35, Math.min(9, cam.dist * (ev.deltaY > 0 ? 1.1 : 0.9)));
    needsRender = true;
  }, { passive: false });
}

function placeCamera() {
  const phi = (90 - cam.lat) * Math.PI / 180;
  const theta = (cam.lon + 180) * Math.PI / 180;
  camera.position.set(
    -cam.dist * Math.sin(phi) * Math.cos(theta),
    cam.dist * Math.cos(phi),
    cam.dist * Math.sin(phi) * Math.sin(theta),
  );
  camera.lookAt(0, 0, 0);
}

// --------------------------------------------------------------------------
// per-tick update
// --------------------------------------------------------------------------

export function updateGlobe(orbit) {
  if (!ready || !orbit) return;

  const now = new Date();
  const sample = store.satpos || orbit.sample(now);
  if (!sample) return;

  const satPos = toVec3(sample.lat, sample.lon, sample.alt_km);
  satMarker.position.copy(satPos);
  satHalo.position.copy(satPos);
  satHalo.lookAt(0, 0, 0);
  satMarker.visible = satHalo.visible = true;

  setPoints(footprint, footprintRing(sample));

  // The ground track is the same window the 2D map draws, so the two panels
  // cannot disagree.
  const track = store.groundTrack;
  if (track?.length) {
    setPoints(trackLine, track.map(([lat, lon]) => toVec3(lat, lon, 12)));
  }

  // The orbit arc is expensive and barely changes minute to minute.
  if (Date.now() - lastPathBuild > PATH_REBUILD_MS) {
    setPoints(orbitLine, buildOrbitPath(orbit, now));
    lastPathBuild = Date.now();
  }

  const site = store.config?.station;
  if (site && sample.el > 0) {
    setPoints(sightLine, [toVec3(site.lat, site.lon, 0), satPos]);
    sightLine.computeLineDistances();
    sightLine.visible = true;
  } else {
    sightLine.visible = false;
  }

  const sub = subsolarPoint(now);
  sun.position.copy(toVec3(sub.lat, sub.lon, 0).multiplyScalar(40));

  needsRender = true;
}

function footprintRing(sample) {
  const radiusKm = footprintRadiusKmOf(sample.alt_km);
  const angular = radiusKm / EARTH_KM;             // radians of arc
  const latC = sample.lat * Math.PI / 180;
  const lonC = sample.lon * Math.PI / 180;
  const points = [];

  for (let i = 0; i <= 72; i++) {
    const bearing = (i / 72) * 2 * Math.PI;
    const lat = Math.asin(
      Math.sin(latC) * Math.cos(angular)
      + Math.cos(latC) * Math.sin(angular) * Math.cos(bearing),
    );
    const lon = lonC + Math.atan2(
      Math.sin(bearing) * Math.sin(angular) * Math.cos(latC),
      Math.cos(angular) - Math.sin(latC) * Math.sin(lat),
    );
    // Drawn just off the surface: coplanar with the sphere it z-fights.
    points.push(toVec3(lat * 180 / Math.PI, lon * 180 / Math.PI, 18));
  }
  return points;
}

function buildOrbitPath(orbit, now) {
  const points = [];
  const start = now.getTime() - (ORBIT_MINUTES / 2) * 60_000;
  for (let s = 0; s <= ORBIT_MINUTES * 60; s += ORBIT_STEP_S) {
    const p = orbit.sample(new Date(start + s * 1000));
    if (p) points.push(toVec3(p.lat, p.lon, p.alt_km));
  }
  return points;
}

function setPoints(line, points) {
  if (!points?.length) {
    line.visible = false;
    return;
  }
  line.geometry.dispose();
  line.geometry = new THREE.BufferGeometry().setFromPoints(points);
  line.visible = true;
}

// --------------------------------------------------------------------------
// rendering
// --------------------------------------------------------------------------

function startPump() {
  // Draw only when something moved. updateGlobe and the controls set the flag;
  // between passes this settles to nothing at all.
  const pump = () => {
    if (needsRender && renderer) {
      placeCamera();
      renderer.render(scene, camera);
      needsRender = false;
    }
    requestAnimationFrame(pump);
  };
  requestAnimationFrame(pump);
}

export function resizeGlobe() {
  if (!renderer || !host) return;
  const w = host.clientWidth || 1;
  const h = host.clientHeight || 1;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  needsRender = true;
}

export function getViewer() {
  return renderer;
}
