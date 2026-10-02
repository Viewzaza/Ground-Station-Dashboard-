/* Shared between the 2D and 3D views, kept separate so globe3d.js can be
   dynamically imported without pulling in the canvas map. */

import { footprintHalfAngleDeg, EARTH_RADIUS_KM, wrapLon } from './geo.js';

/** Surface radius of the visibility circle, in kilometres. */
export function footprintRadiusKmOf(altKm, minElevationDeg = 0) {
  return EARTH_RADIUS_KM * (footprintHalfAngleDeg(altKm, minElevationDeg) * Math.PI) / 180;
}

/** Longitudes where a ground track crosses the equator going north.
 *
 *  This is the ascending node, and it is the only one of the classical element
 *  vectors that survives the trip into this console's frame. SattrackSlop draws
 *  the whole set — line of nodes, eccentricity vector, angular momentum,
 *  velocity split into transverse and radial — but every one of those is an
 *  inertial direction, and these panels are deliberately earth-fixed so that
 *  the 3D and 2D tracks are the same line. Drawn in an earth-fixed frame an
 *  inertial vector points at nothing in particular.
 *
 *  The equator crossing does not have that problem, because in this frame it is
 *  a place: the longitude the spacecraft passes over heading north. That is a
 *  number an operator already has a use for — successive crossings are one
 *  revolution apart and their spacing is the westward drift per orbit — so it
 *  is worth a mark of its own, which the rest of the element set is not.
 *
 *  Takes the [lat, lon] pairs the panels already have. The interpolation
 *  unwraps the longitude step first: a crossing that happens to straddle ±180
 *  would otherwise be placed at the average of 179 and -179, which is the
 *  Greenwich meridian and half a planet wrong.
 */
export function ascendingNodeLons(points) {
  const out = [];
  for (let i = 1; i < points.length; i++) {
    const [latA, lonA] = points[i - 1];
    const [latB, lonB] = points[i];
    if (latA >= 0 || latB < 0) continue;          // not a northbound crossing

    const span = latB - latA;
    const f = span ? -latA / span : 0;
    let step = lonB - lonA;
    if (Math.abs(step) > 180) step -= 360 * Math.sign(step);
    out.push(wrapLon(lonA + f * step));
  }
  return out;
}
