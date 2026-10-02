/* SGP4 in the browser, for the smooth 1 Hz render only.

   The backend (Skyfield) remains the single source of truth for pass schedules
   — it has to be, because the antenna is driven from it and it must keep
   running with no browser open. What happens here is purely visual: current
   position, a ground track either side of now, and the live look angles.

   Two easy mistakes this module exists to prevent:
     * satellite.js returns radians and kilometres, not degrees and metres;
     * dopplerFactor needs an ECF velocity — handing it the ECI velocity is
       silently wrong by several km/s.
*/

import {
  twoline2satrec, propagate, gstime,
  eciToEcf, eciToGeodetic, geodeticToEcf, ecfToLookAngles,
  dopplerFactor, degreesLat, degreesLong,
  degreesToRadians, radiansToDegrees,
} from '../vendor/satellite-7.1.0.esm.js';

const C_KM_S = 299792.458;

export class Orbit {
  /**
   * @param {{tle1:string, tle2:string, name?:string, norad?:number}} tle
   * @param {{lat:number, lon:number, alt_m:number}} site
   */
  constructor(tle, site) {
    this.name = tle.name || '';
    this.norad = tle.norad;
    this.satrec = twoline2satrec(tle.tle1, tle.tle2);

    this.observerGd = {
      latitude: degreesToRadians(site.lat),
      longitude: degreesToRadians(site.lon),
      height: (site.alt_m || 0) / 1000,     // satellite.js wants KILOMETRES
    };
    this.observerEcf = geodeticToEcf(this.observerGd);
  }

  /** Full state at an instant, or null if SGP4 gave up. */
  sample(date = new Date(), downlinkHz = null) {
    const pv = propagate(this.satrec, date, { communityDecayCheckEnabled: true });
    if (!pv || !pv.position) return null;

    const gmst = gstime(date);
    const posEcf = eciToEcf(pv.position, gmst);
    const velEcf = eciToEcf(pv.velocity, gmst);       // must be ECF, not ECI
    const gd = eciToGeodetic(pv.position, gmst);
    const look = ecfToLookAngles(this.observerGd, posEcf);

    const factor = dopplerFactor(this.observerEcf, posEcf, velEcf);   // 1 - rdot/c
    const rangeRate = (1 - factor) * C_KM_S;                          // + = receding

    const speed = Math.hypot(pv.velocity.x, pv.velocity.y, pv.velocity.z);

    return {
      t: date,
      lat: degreesLat(gd.latitude),
      lon: degreesLong(gd.longitude),
      alt_km: gd.height,
      vel_km_s: speed,
      az: (radiansToDegrees(look.azimuth) + 360) % 360,
      el: radiansToDegrees(look.elevation),
      range_km: look.rangeSat,
      range_rate_km_s: rangeRate,
      doppler_hz: downlinkHz ? -downlinkHz * rangeRate / C_KM_S : null,
    };
  }

  /** Sub-satellite points either side of now, for the 2D map. */
  groundTrack(minutesBefore = 45, minutesAfter = 45, stepS = 30) {
    const out = [];
    const start = Date.now() - minutesBefore * 60000;
    const total = (minutesBefore + minutesAfter) * 60000;
    for (let t = 0; t <= total; t += stepS * 1000) {
      const s = this.sample(new Date(start + t));
      if (s) out.push([s.lat, s.lon]);
    }
    return out;
  }
}
