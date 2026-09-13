/* 3D orbit view.

   CesiumJS, vendored locally and configured to run with no Cesium Ion account
   and no network: the imagery is the Natural Earth II tile set that ships
   inside Cesium's own Assets folder, and the terrain is a plain ellipsoid. A
   ground station may sit on an isolated LAN, so nothing here may reach a CDN.

   The three settings that would otherwise phone home are baseLayerPicker (its
   picker list is Ion-backed), geocoder (Ion's search service) and any use of
   createWorldTerrain. All three are off.

   This module is imported dynamically so a phone never downloads 23 MB of
   Cesium to look at the camera feeds.
*/

import { store } from '../core/store.js';
import { footprintRadiusKmOf } from '../lib/globe-helpers.js';

const CESIUM_BASE = '/js/vendor/cesium/';

let viewer = null;
let entities = null;
let orbitPathPositions = [];
let lastPathBuild = 0;
let pumpTimer = null;
let mounting = null;      // in-flight mountGlobe(), so a second call joins it

function loadCesium() {
  if (window.Cesium) return Promise.resolve(window.Cesium);
  return new Promise((resolve, reject) => {
    window.CESIUM_BASE_URL = CESIUM_BASE;

    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = `${CESIUM_BASE}Widgets/widgets.css`;
    document.head.appendChild(css);

    const script = document.createElement('script');
    script.src = `${CESIUM_BASE}Cesium.js`;
    script.onload = () => resolve(window.Cesium);
    script.onerror = () => reject(new Error('Cesium failed to load'));
    document.head.appendChild(script);
  });
}

export async function mountGlobe(hostId = 'globe3d') {
  // Cesium takes several seconds to parse, so a second call can arrive while
  // the first is still loading. Without this guard that builds a second viewer
  // inside the same element, and two render loops fight over one canvas.
  if (viewer) return viewer;
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
  const host = document.getElementById(hostId);
  if (!host) return null;

  let Cesium;
  try {
    Cesium = await loadCesium();
  } catch (err) {
    host.innerHTML = '<p class="cam-placeholder">3D globe unavailable — run tools/fetch_vendor.sh</p>';
    console.warn('[globe3d]', err);
    return null;
  }

  viewer = new Cesium.Viewer(host, {
    baseLayer: Cesium.ImageryLayer.fromProviderAsync(
      Cesium.TileMapServiceImageryProvider.fromUrl(
        Cesium.buildModuleUrl('Assets/Textures/NaturalEarthII'),
      ),
    ),
    terrainProvider: new Cesium.EllipsoidTerrainProvider(),
    baseLayerPicker: false,     // its layer list is served by Ion
    geocoder: false,            // Ion search
    animation: false,
    timeline: false,
    fullscreenButton: false,
    homeButton: false,
    sceneModePicker: false,
    navigationHelpButton: false,
    infoBox: false,
    selectionIndicator: false,
    // requestRenderMode looks like the obvious choice for a display that runs
    // for months, but it starves Cesium's own tile loader: the globe surface
    // never finishes loading and you get markers floating in a black void.
    requestRenderMode: false,
  });

  // Cesium's own loop is driven by requestAnimationFrame, which a browser is
  // free to throttle to nothing when the window is occluded, backgrounded or
  // — as here — embedded. A wall display that silently stops repainting is
  // worse than a slow one, so drive the renderer ourselves (see pump()).
  viewer.useDefaultRenderLoop = false;

  // Realistic lighting would leave half the globe unreadable, and the half an
  // operator wants is usually the dark one — a station works its passes at
  // night. Day/night belongs on the 2D map, which shows it without hiding
  // anything.
  viewer.scene.globe.enableLighting = false;
  viewer.scene.skyAtmosphere.show = true;
  viewer.scene.backgroundColor = Cesium.Color.fromCssColorString('#05080d');

  // Coarser tiles than the default: this is a 500 px orbit view, not a map.
  viewer.scene.globe.maximumScreenSpaceError = 4;

  const station = store.config?.station;
  entities = {
    station: viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(station.lon, station.lat, station.alt_m),
      point: { pixelSize: 9, color: Cesium.Color.fromCssColorString('#3ddc84') },
      label: {
        text: `GS ${station.id}`,
        font: '11px monospace',
        fillColor: Cesium.Color.fromCssColorString('#3ddc84'),
        pixelOffset: new Cesium.Cartesian2(0, -16),
        showBackground: true,
        backgroundColor: Cesium.Color.fromCssColorString('#080b10cc'),
      },
    }),

    satellite: viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(0, 0, 400000),
      point: {
        pixelSize: 11,
        color: Cesium.Color.fromCssColorString('#37d2f0'),
        outlineColor: Cesium.Color.fromCssColorString('#05080d'),
        outlineWidth: 2,
      },
      label: {
        text: '',
        font: '600 11px monospace',
        fillColor: Cesium.Color.fromCssColorString('#dce5f0'),
        pixelOffset: new Cesium.Cartesian2(0, -18),
        showBackground: true,
        backgroundColor: Cesium.Color.fromCssColorString('#080b10cc'),
      },
    }),

    // Dropped from the satellite to the ground, so the sub-satellite point is
    // unambiguous when the globe is tilted.
    nadir: viewer.entities.add({
      polyline: {
        positions: [Cesium.Cartesian3.ZERO, Cesium.Cartesian3.ZERO],
        width: 1,
        material: new Cesium.PolylineDashMaterialProperty({
          color: Cesium.Color.fromCssColorString('#37d2f0').withAlpha(0.6),
        }),
      },
    }),

    footprint: viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(0, 0),
      ellipse: {
        semiMajorAxis: 1, semiMinorAxis: 1,
        material: Cesium.Color.fromCssColorString('#37d2f0').withAlpha(0.12),
        outline: true,
        outlineColor: Cesium.Color.fromCssColorString('#37d2f0').withAlpha(0.5),
        height: 0,
      },
    }),

    path: viewer.entities.add({
      polyline: {
        positions: [],
        width: 2,
        material: Cesium.Color.fromCssColorString('#8b7cf6').withAlpha(0.85),
        arcType: Cesium.ArcType.NONE,      // already a 3D path, do not drape it
      },
    }),

    // Line of sight, drawn only while the satellite is actually visible.
    sight: viewer.entities.add({
      polyline: {
        positions: [Cesium.Cartesian3.ZERO, Cesium.Cartesian3.ZERO],
        width: 1.5,
        material: Cesium.Color.fromCssColorString('#3ddc84').withAlpha(0.7),
        arcType: Cesium.ArcType.NONE,
      },
      show: false,
    }),
  };

  // Close enough that the globe fills a wide, short panel. Much higher and
  // the Earth becomes a small disc adrift in the starfield.
  viewer.camera.setView({
    destination: Cesium.Cartesian3.fromDegrees(station.lon, station.lat, 13_500_000),
  });

  pump();
  return viewer;
}

/* Self-regulating render loop.

   Cesium's quadtree needs several consecutive frames to select and load
   surface tiles, so run hot while tiles are still arriving and drop to a
   lazy 1 Hz once the globe has settled. Nothing in this view moves faster
   than a satellite, so 1 Hz is the right idle cost for a machine that will
   be left running for months. */
function pump() {
  if (!viewer || viewer.isDestroyed()) return;
  viewer.render();
  const settling = !viewer.scene.globe.tilesLoaded;
  clearTimeout(pumpTimer);
  pumpTimer = setTimeout(pump, settling ? 60 : 1000);
}

/** Called once a second from the main loop. */
export function updateGlobe(orbit) {
  if (!viewer || !window.Cesium || !orbit) return;
  const Cesium = window.Cesium;
  const pos = store.satpos;
  if (!pos) return;

  const satCart = Cesium.Cartesian3.fromDegrees(pos.lon, pos.lat, pos.alt_km * 1000);
  const subCart = Cesium.Cartesian3.fromDegrees(pos.lon, pos.lat, 0);

  entities.satellite.position = satCart;
  entities.satellite.label.text = store.satellite?.name || '';
  entities.nadir.polyline.positions = [satCart, subCart];

  entities.footprint.position = subCart;
  const radiusM = footprintRadiusKmOf(pos.alt_km) * 1000;
  entities.footprint.ellipse.semiMajorAxis = radiusM;
  entities.footprint.ellipse.semiMinorAxis = radiusM;

  const station = store.config?.station;
  if (station) {
    const gs = Cesium.Cartesian3.fromDegrees(station.lon, station.lat, station.alt_m);
    entities.sight.show = pos.el > 0;
    if (pos.el > 0) entities.sight.polyline.positions = [gs, satCart];
  }

  // One full revolution of path, rebuilt every minute rather than every frame.
  if (Date.now() - lastPathBuild > 60_000 || !orbitPathPositions.length) {
    orbitPathPositions = buildPath(Cesium, orbit);
    entities.path.polyline.positions = orbitPathPositions;
    lastPathBuild = Date.now();
  }

}

function buildPath(Cesium, orbit, minutesEitherSide = 50, stepS = 20) {
  const out = [];
  const start = Date.now() - minutesEitherSide * 60000;
  const total = minutesEitherSide * 2 * 60000;
  for (let t = 0; t <= total; t += stepS * 1000) {
    const s = orbit.sample(new Date(start + t));
    if (s) out.push(Cesium.Cartesian3.fromDegrees(s.lon, s.lat, s.alt_km * 1000));
  }
  return out;
}

/** The Cesium viewer, for panels that want to point the camera somewhere. */
export function getViewer() {
  return viewer;
}

export function resizeGlobe() {
  if (!viewer) return;
  viewer.resize();
  viewer.render();
}
