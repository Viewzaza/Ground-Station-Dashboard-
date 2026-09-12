/* Shared between the 2D and 3D views, kept separate so globe3d.js can be
   dynamically imported without pulling in the canvas map. */

import { footprintHalfAngleDeg, EARTH_RADIUS_KM } from './geo.js';

/** Surface radius of the visibility circle, in kilometres. */
export function footprintRadiusKmOf(altKm, minElevationDeg = 0) {
  return EARTH_RADIUS_KM * (footprintHalfAngleDeg(altKm, minElevationDeg) * Math.PI) / 180;
}
