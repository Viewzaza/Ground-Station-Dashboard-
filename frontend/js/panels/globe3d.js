/* 3D orbit view.

   This was CesiumJS. Cesium is a 23 MB download for a panel that draws one
   globe, one orbit and two markers, and because it was fetched by a setup
   script rather than committed, a clone that skipped that step showed a black
   rectangle with no explanation. three.js is 1.3 MB, loads as a plain ES module
   with no build step, and everything drawn here comes from data the repository
   already has.

   The globe surface is NASA Blue Marble when the image is on disk and the
   drawn coastline canvas when it is not — see lib/globe-surface.js, which owns
   that decision and the reason it is a decision rather than a requirement.
   Nothing is fetched at runtime either way: the imagery arrives once, from
   tools/fetch_vendor.sh, and is served from our own origin, because this
   station may sit on an isolated LAN. Which surface is up is written into the
   panel's hint, so the Cesium failure — a panel that looks broken with no hint
   why — cannot come back in a new costume.

   The frame is earth-fixed, not inertial. An inertial scene with the planet
   spinning underneath looks better in isolation, but this panel sits beside a
   2D ground track and a polar plot, and all three should be answering the same
   question: where is the satellite relative to *our* ground. In an earth-fixed
   frame the 3D track and the 2D track are literally the same line.

   Rendering is on demand. A wall display runs for months, and a
   requestAnimationFrame loop spinning a GPU at 60 Hz to move a marker that
   updates at 1 Hz is just heat.

   The surface, its shaders, the atmosphere rim and the equator-crossing mark
   are adapted from SattrackSlop (https://github.com/ColaBear101/SattrackSlop,
   MIT); each borrowed piece says so where it sits. What was deliberately left
   there: its star catalogue and constellations, its whole-catalogue point
   cloud, its POV camera and minimap, and its runtime NASA GIBS fetches.
*/

import { store } from '../core/store.js';
import { footprintRadiusKmOf, ascendingNodeLons } from '../lib/globe-helpers.js';
import { subsolarPoint } from '../lib/geo.js';
import { buildEarthSurface, atmosphereMaterial } from '../lib/globe-surface.js';

const THREE_URL = '/js/vendor/three/three.module.js';
const LAND_URL = '/assets/ne_110m_land.json';
const HINT_ID = 'globe-hint';

const EARTH_R = 1;                  // scene units; km are scaled to this
const EARTH_KM = 6371.0;
const ORBIT_MINUTES = 100;          // a little over one LEO revolution
const ORBIT_STEP_S = 20;
const PATH_REBUILD_MS = 60_000;
const NODE_TICK_KM = 420;           // how far the equator-crossing mark stands off

/* Colours come from css/tokens.css, so the globe, the 2D map and the polar plot
   cannot drift apart. The literals here are fallbacks for the one case the
   stylesheet cannot cover — tokens.css missing — and each names the token it
   mirrors.

   The page around this panel is light. The globe is not, and must not be: it is
   an instrument window on `--void`, and a photographic Earth on a white card
   reads as a broken image. The `--globe-*` surface colours that feed the drawn
   texture are lit by a directional light, so every one of them is multiplied
   down before it reaches the screen; they are chosen for how they render
   *after* lighting, which is why they look too bright as flat swatches. Do not
   lighten them to match the page.

   The overlays are the page's three data hues. They are drawn with Basic
   materials, which are unlit, so they land on screen at exactly these values. */
const FALLBACK = {
  ocean:     '#0b2033',   // --globe-ocean
  land:      '#2c4437',   // --globe-land
  coast:     '#496b56',   // --globe-coast
  graticule: '#27455a',   // --globe-grat
  atmo:      '#3f8fb4',   // --globe-atmo   the limb
  specular:  '#0d1820',   // --globe-spec
  glint:     '#cfe4ee',   // --globe-glint  sun on water, photographic surface
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
    atmo:      v('--globe-atmo', FALLBACK.atmo),
    specular:  v('--globe-spec', FALLBACK.specular),
    glint:     v('--globe-glint', FALLBACK.glint),
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
let surface = null;       // whichever Earth surface won; see lib/globe-surface.js

let satMarker = null;
let satHalo = null;
let subMarker = null;
let nadirLine = null;
let trackLine = null;
let orbitLine = null;
let sightLine = null;
let nodeTicks = null;
let footprint = null;
let sun = null;

let cam = { lat: 20, lon: 100, dist: 3.2 };   // spherical camera, degrees + units
let lastPathBuild = 0;
// main.js holds the module as soon as the dynamic import resolves and starts
// calling updateGlobe from its 1 Hz tick, which can land while mountGlobe is
// still awaiting the texture. Guarding on `renderer` is not enough: that is
// assigned early, so the markers would still be null. Set this last.
let ready = false;
let mounting = null;      // in-flight mountGlobe(), so a second call joins it

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

// --------------------------------------------------------------------------
// the panel hint
// --------------------------------------------------------------------------

/* The lesson from the Cesium era, written down as code. A globe that looks
   wrong must say what it is, in the heading, where someone standing in front of
   the rack will see it without opening a console. The long form goes in the
   tooltip because the heading has room for three words. */
function setHint(text, detail) {
  const el = document.getElementById(HINT_ID);
  if (!el) return;
  el.textContent = text;
  if (detail) el.title = detail;
}

// --------------------------------------------------------------------------
// mount
// --------------------------------------------------------------------------

export async function mountGlobe(hostId = 'globe3d') {
  // Mounting is not instant — a dynamic import of three.js, then either
  // decoding a Blue Marble JPEG or painting a 2048x1024 texture — so a second
  // call can arrive while the first is still running, and that would build a
  // second renderer inside the same element with two of them fighting over one
  // canvas. The guard predates this module: it was written for Cesium, where
  // the window was several seconds. It is shorter now, not zero, and the
  // failure is just as silent.
  if (renderer) return renderer;
  if (mounting) return mounting;

  mounting = (async () => {
    try {
      return await build(hostId);
    } finally {
      mounting = null;
    }
  })();
  return mounting;
}

async function build(hostId) {
  // Module-scope, not local: resizeGlobe needs it on every window resize.
  host = document.getElementById(hostId);
  if (!host) return null;

  COLOR = readPalette();

  try {
    THREE = await import(THREE_URL);
  } catch (err) {
    host.innerHTML =
      '<p class="cam-placeholder">3D globe unavailable — run tools/fetch_vendor.sh</p>';
    setHint('no renderer', 'three.js is missing from js/vendor/three — run tools/fetch_vendor.sh.');
    console.warn('[globe3d]', err);
    return null;
  }

  scene = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(42, 1, 0.01, 100);

  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  host.replaceChildren(renderer.domElement);
  renderer.domElement.style.cssText = 'width:100%;height:100%;display:block;cursor:grab';

  // The renderer has to exist before the surface: the surface needs to ask it
  // how large a texture this GPU will take and how much anisotropy it will do.
  surface = await buildEarthSurface({
    THREE, renderer, colors: COLOR, landUrl: LAND_URL,
  });
  setHint(surface.hint, surface.detail);

  /* 160x96 where this was 96x64. The extra rings are all at the poles, and
     that is where they are needed: SattrackSlop found that at 64 height
     segments the polar triangle fan is coarse enough to visibly kink the
     coastline of Antarctica. It cost nothing to ignore while the surface was
     four flat colours. With a photograph on it, the kink is a crease across a
     recognisable place. 30k triangles, drawn once per second at most. */
  earth = new THREE.Mesh(new THREE.SphereGeometry(EARTH_R, 160, 96), surface.material);
  scene.add(earth);

  // A thin rim, which is what reads as "atmosphere" at this size without the
  // cost and fragility of a real scattering shader.
  scene.add(new THREE.Mesh(
    new THREE.SphereGeometry(EARTH_R * 1.018, 64, 48),
    atmosphereMaterial(THREE, COLOR.atmo),
  ));

  /* Lights, and only if the surface wants them. The drawn surface is a
     MeshPhongMaterial and gets its terminator from a directional light aimed at
     the subsolar point, with ambient kept deliberately low — the emissive pass
     already guarantees the night side is readable, so ambient's only job is to
     stop the terminator being a hard black edge, and raising it washes the
     terminator out entirely. The photographic surface does its own lighting in
     a shader and would ignore these, so they are not added: a light in a scene
     that nothing reads is a thing the next person has to rule out. */
  if (surface.lit) {
    sun = new THREE.DirectionalLight(0xffffff, 2.4);
    scene.add(sun);
    scene.add(new THREE.AmbientLight(0xffffff, 0.18));
  }

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

  /* The radius vector, split at the surface. SattrackSlop draws both halves as
     labelled arrows — centre to surface, then surface to spacecraft — and the
     second half is the one worth having here: it makes altitude a length you
     can see rather than a number you have to read, and it nails the satellite
     to the point on the ground track directly underneath it. That pairing is
     the whole claim this panel makes next to the 2D map, and until now the two
     markers floated with nothing joining them.

     --contact, because it is a now quantity, and half the opacity of the dashed
     sight line so the two do not compete: dashed goes to us, solid goes
     straight down. */
  subMarker = new THREE.Mesh(
    new THREE.SphereGeometry(0.0075, 10, 8),
    new THREE.MeshBasicMaterial({ color: COLOR.contact }),
  );
  nadirLine = new THREE.Line(
    new THREE.BufferGeometry(),
    new THREE.LineBasicMaterial({
      color: COLOR.contact, transparent: true, opacity: 0.5,
    }),
  );
  subMarker.visible = nadirLine.visible = false;
  scene.add(subMarker, nadirLine);

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

  /* Where the track crosses the equator going north — see ascendingNodeLons()
     for why this is the one element vector that means anything in an
     earth-fixed frame. LineSegments rather than Line so two crossings in the
     window do not get joined to each other by a chord through the planet.

     Radial, not tangential, and that is the point: nothing else on this globe
     stands off the surface in a straight line, so a reader has no reason to
     mistake it for a piece of track. --track, because it is orbit geometry
     rather than a live value. */
  nodeTicks = new THREE.LineSegments(
    new THREE.BufferGeometry(),
    new THREE.LineBasicMaterial({
      color: COLOR.track, transparent: true, opacity: 0.55,
    }),
  );
  nodeTicks.visible = false;

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
  scene.add(footprint, trackLine, orbitLine, sightLine, nodeTicks);

  addStation();
  attachControls();
  aimSun(new Date());
  resizeGlobe();
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
    requestRender();
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
    requestRender();
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

  // The sub-satellite point sits at the ground track's own altitude rather
  // than on the surface, so the dot lands ON the line instead of z-fighting
  // the sphere just under it.
  const subPos = toVec3(sample.lat, sample.lon, 12);
  subMarker.position.copy(subPos);
  subMarker.visible = true;
  setPoints(nadirLine, [subPos, satPos]);

  setPoints(footprint, footprintRing(sample));

  // The ground track is the same window the 2D map draws, so the two panels
  // cannot disagree.
  const track = store.groundTrack;
  if (track?.length) {
    setPoints(trackLine, track.map(([lat, lon]) => toVec3(lat, lon, 12)));
  }

  // The orbit arc is expensive and barely changes minute to minute. The
  // equator crossings come off the same clock: they move at the same rate the
  // track does, which is to say slowly.
  if (Date.now() - lastPathBuild > PATH_REBUILD_MS) {
    setPoints(orbitLine, buildOrbitPath(orbit, now));
    setNodeTicks(track);
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

  aimSun(now);
  requestRender();
}

/* One subsolar point for the whole console. This used to be a private copy of
   the solar-position series living in this file, while the 2D map shaded its
   night from lib/geo.js — two approximations of the same quantity, which is two
   chances to disagree about where the terminator is on two panels side by side.
   geo.js is the one with the Python twin and the tests.

   Called from build() as well as from the tick, and that is not belt and
   braces. updateGlobe returns early when there is no satellite — no TLE yet, a
   catalogue lookup that failed — and until this was hoisted out, a console in
   that state showed an Earth lit from wherever the shader's uniform happened to
   be initialised, indefinitely. The terminator is a reading in its own right;
   it should not depend on having picked a spacecraft. */
function aimSun(when) {
  const [subLat, subLon] = subsolarPoint(when);
  const dir = toVec3(subLat, subLon, 0);          // already unit length
  if (surface?.setSun) surface.setSun(dir);
  if (sun) sun.position.copy(dir).multiplyScalar(40);
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

/* Read off the ground track rather than from the orbit arc, even though the arc
   is sampled four times as finely: the mark then falls exactly where the line
   the operator can see crosses the equator, and on the 2D map beside it too.
   A node computed from a denser sample would be more nearly right and would
   visibly miss the line it is annotating. */
function setNodeTicks(track) {
  if (!track?.length) {
    nodeTicks.visible = false;
    return;
  }
  const points = [];
  for (const lon of ascendingNodeLons(track)) {
    points.push(toVec3(0, lon, 0), toVec3(0, lon, NODE_TICK_KM));
  }
  setPoints(nodeTicks, points);
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

/* Draw only when something moved, and ask for the frame only then.

   This used to be a permanent requestAnimationFrame loop that checked a dirty
   flag and usually did nothing. That is nearly on demand, and "nearly" is the
   wrong shape for the claim the README makes: an idle rAF loop still wakes the
   compositor sixty times a second for the life of the display, and it is
   indistinguishable from a real render loop to anyone reading a profiler
   wondering where the heat is coming from. Now nothing is scheduled between
   passes, so `renderer.info.render.frame` is a count of things that actually
   changed — updateGlobe at 1 Hz, a drag, a wheel, a resize.

   Coalescing on rafId matters: a drag fires pointermove far faster than the
   display refreshes, and one frame per refresh is all any of them can have. */
let rafId = 0;

function requestRender() {
  if (rafId || !renderer) return;
  rafId = requestAnimationFrame(() => {
    rafId = 0;
    placeCamera();
    renderer.render(scene, camera);
  });
}

export function resizeGlobe() {
  if (!renderer || !host) return;
  const w = host.clientWidth || 1;
  const h = host.clientHeight || 1;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  requestRender();
}

export function getViewer() {
  return renderer;
}

/** Which Earth surface is up: 'imagery', 'drawn', or null before mount. Here
 *  for the same reason the hint is: so a question about the globe's appearance
 *  has an answer that does not require guessing from pixels. */
export function getSurfaceKind() {
  return surface?.kind || null;
}
