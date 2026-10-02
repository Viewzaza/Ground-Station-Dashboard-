/* Spherical geometry for the map panels.

   This is the JavaScript twin of backend/app/util/geo.py. Keep the two in step;
   the Python side has the unit tests. */

export const EARTH_RADIUS_KM = 6371.0088;

const rad = (d) => (d * Math.PI) / 180;
const degOf = (r) => (r * 180) / Math.PI;

export const wrapLon = (lon) => ((lon + 180) % 360 + 360) % 360 - 180;

export function footprintHalfAngleDeg(altKm, minElevationDeg = 0) {
  if (altKm <= 0) return 0;
  const eps = rad(minElevationDeg);
  const ratio = EARTH_RADIUS_KM / (EARTH_RADIUS_KM + altKm);
  const inner = Math.max(-1, Math.min(1, ratio * Math.cos(eps)));
  return Math.max(0, degOf(Math.acos(inner) - eps));
}

export function destinationPoint(latDeg, lonDeg, bearingDeg, angularDistanceDeg) {
  const lat1 = rad(latDeg);
  const lon1 = rad(lonDeg);
  const brg = rad(bearingDeg);
  const ang = rad(angularDistanceDeg);

  let sinLat2 = Math.sin(lat1) * Math.cos(ang) + Math.cos(lat1) * Math.sin(ang) * Math.cos(brg);
  sinLat2 = Math.max(-1, Math.min(1, sinLat2));
  const lat2 = Math.asin(sinLat2);
  const lon2 = lon1 + Math.atan2(
    Math.sin(brg) * Math.sin(ang) * Math.cos(lat1),
    Math.cos(ang) - Math.sin(lat1) * sinLat2,
  );
  return [degOf(lat2), wrapLon(degOf(lon2))];
}

export function footprintRing(latDeg, lonDeg, altKm, minElevationDeg = 0, points = 72) {
  const half = footprintHalfAngleDeg(altKm, minElevationDeg);
  if (half <= 0) return [];
  const ring = [];
  for (let i = 0; i <= points; i++) {
    ring.push(destinationPoint(latDeg, lonDeg, (360 * i) / points, half));
  }
  return ring;
}

/** +1 if the footprint swallows the north pole, -1 the south, else 0.
    Such a ring does not close on an equirectangular map — the caller has to
    fill to the top or bottom edge instead of closing the polygon. */
export function containsPole(latDeg, altKm, minElevationDeg = 0) {
  const half = footprintHalfAngleDeg(altKm, minElevationDeg);
  if (latDeg + half >= 90) return 1;
  if (latDeg - half <= -90) return -1;
  return 0;
}

/** Break a [lat, lon] path where it crosses ±180.

    Without this, a step from lon 179 to lon -179 draws a line straight across
    the map. The crossing latitude is interpolated and added to both segments so
    they meet the edge cleanly. */
export function splitAntimeridian(points) {
  if (points.length < 2) return points.length ? [points.slice()] : [];

  const segments = [];
  let current = [points[0]];

  for (let i = 1; i < points.length; i++) {
    const [latA, lonA] = points[i - 1];
    const [latB, lonB] = points[i];
    const delta = lonB - lonA;

    if (Math.abs(delta) > 180) {
      const unwrapped = lonB - 360 * Math.sign(delta);
      const edge = 180 * Math.sign(lonA);
      const span = unwrapped - lonA;
      const f = span ? (edge - lonA) / span : 0;
      const latCross = latA + f * (latB - latA);

      current.push([latCross, edge]);
      segments.push(current);
      current = [[latCross, -edge], [latB, lonB]];
    } else {
      current.push([latB, lonB]);
    }
  }
  if (current.length > 1) segments.push(current);
  return segments;
}

/** Approximate sub-solar point — good to a few tenths of a degree, far finer
    than a terminator line on a wall display can show. */
export function subsolarPoint(when = new Date()) {
  const jd = when.getTime() / 86400000 + 2440587.5;
  const n = jd - 2451545.0;

  const meanLon = (280.460 + 0.9856474 * n) % 360;
  const meanAnom = rad((357.528 + 0.9856003 * n) % 360);
  const eclipticLon = rad(meanLon + 1.915 * Math.sin(meanAnom) + 0.020 * Math.sin(2 * meanAnom));
  const obliquity = rad(23.439 - 0.0000004 * n);

  const declination = degOf(Math.asin(Math.sin(obliquity) * Math.sin(eclipticLon)));

  const utcHours = when.getUTCHours() + when.getUTCMinutes() / 60 + when.getUTCSeconds() / 3600;
  let eot = degOf(Math.atan2(
    Math.cos(obliquity) * Math.sin(eclipticLon),
    Math.cos(eclipticLon),
  )) - (meanLon % 360);
  eot = ((eot + 180) % 360 + 360) % 360 - 180;

  return [declination, wrapLon(-15 * (utcHours - 12) + eot)];
}

export function terminatorRing(when = new Date(), points = 180) {
  const [lat, lon] = subsolarPoint(when);
  const ring = [];
  for (let i = 0; i <= points; i++) {
    ring.push(destinationPoint(lat, lon, (360 * i) / points, 90));
  }
  return ring;
}
