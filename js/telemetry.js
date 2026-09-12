/* Telemetry sources.
 *
 * Every source emits packets in one shape. Swap the source, keep the UI:
 *
 *   {
 *     seq:   int      packet counter from the vehicle (used to detect loss)
 *     t:     float    mission elapsed time, seconds
 *     state: string   IDLE|ARMED|BOOST|COAST|APOGEE|DROGUE|MAIN|LANDED
 *     alt:   float    altitude above launch site, m
 *     vz:    float    vertical speed, m/s (+up)
 *     gs:    float    ground speed, m/s
 *     lat,lon: float  degrees
 *     roll,pitch,yaw: float  degrees
 *     temp:  float    C
 *     press: float    hPa
 *     volt:  float    battery V
 *     sats:  int      GPS satellites
 *     rssi:  float    dBm
 *   }
 *
 * A source is any object with connect(), disconnect() and an onPacket callback.
 */

/* ------------------------------------------------------------------ *
 * SimSource — flies a plausible sounding-rocket profile.
 * Used when there is no radio attached, so the UI can be developed and
 * demoed offline.
 * ------------------------------------------------------------------ */
class SimSource {
  constructor(opts = {}) {
    this.hz = opts.hz || 10;
    this.onPacket = null;
    this.vehicle = 'SIM-1';
    this._timer = null;
  }

  connect() {
    this._reset();
    this._timer = setInterval(() => this._step(), 1000 / this.hz);
  }

  disconnect() {
    clearInterval(this._timer);
    this._timer = null;
  }

  _reset() {
    this.t = 0;
    this.seq = 0;
    this.alt = 0;
    this.vz = 0;
    this.state = 'ARMED';
    this.lat = 13.7563;          // launch site
    this.lon = 100.5018;
    this.yaw = 45;
    this.volt = 8.31;
    this._armFor = 3;            // seconds on the pad before ignition
  }

  _step() {
    const dt = 1 / this.hz;
    this.t += dt;
    this.seq++;

    const BURN = 2.4;            // motor burn time, s
    const THRUST_A = 92;         // m/s^2 while burning
    const g = 9.81;

    // --- vertical flight model ---
    if (this.state === 'ARMED') {
      if (this.t > this._armFor) { this.state = 'BOOST'; this._t0 = this.t; }
    } else if (this.state === 'BOOST') {
      this.vz += (THRUST_A - g) * dt;
      if (this.t - this._t0 > BURN) this.state = 'COAST';
    } else if (this.state === 'COAST') {
      this.vz -= (g + 0.0009 * this.vz * this.vz) * dt;   // drag grows with v^2
      if (this.vz <= 0) { this.state = 'APOGEE'; this._apogeeAt = this.t; }
    } else if (this.state === 'APOGEE') {
      this.vz -= g * dt;
      if (this.t - this._apogeeAt > 0.8) this.state = 'DROGUE';
    } else if (this.state === 'DROGUE') {
      this.vz += (-g - 0.028 * this.vz * Math.abs(this.vz)) * dt;  // ~-22 m/s
      if (this.alt < 250) this.state = 'MAIN';
    } else if (this.state === 'MAIN') {
      this.vz += (-g - 0.35 * this.vz * Math.abs(this.vz)) * dt;   // ~-5 m/s
      if (this.alt <= 0) { this.alt = 0; this.vz = 0; this.state = 'LANDED'; }
    }

    if (this.state !== 'LANDED') this.alt = Math.max(0, this.alt + this.vz * dt);

    // --- drift downrange under canopy, plus a little wind on the way up ---
    const descending = this.state === 'DROGUE' || this.state === 'MAIN';
    const gs = this.state === 'LANDED' ? 0 : (descending ? 6.5 : 2.0);
    const windDir = 0.8;                                  // radians, ENE
    this.lat += (gs * Math.cos(windDir) * dt) / 111320;
    this.lon += (gs * Math.sin(windDir) * dt) /
                (111320 * Math.cos(this.lat * Math.PI / 180));

    // --- attitude: spins up under thrust, tips over at apogee, swings under chute ---
    let roll, pitch;
    if (this.state === 'ARMED' || this.state === 'LANDED') {
      roll = 0; pitch = this.state === 'LANDED' ? -84 : 90;
    } else if (descending) {
      roll = 14 * Math.sin(this.t * 1.9);                  // canopy oscillation
      pitch = -70 + 12 * Math.sin(this.t * 1.3);
    } else {
      roll = (this.t * 190) % 360 - 180;                   // roll rate under boost
      pitch = 90 - Math.min(95, Math.max(0, (this.t - this._t0) * 7));
    }
    this.yaw = (this.yaw + 24 * dt) % 360;

    // --- environment ---
    const temp = 24.5 - this.alt * 0.0065 + this._noise(0.15);
    const press = 1013.25 * Math.pow(1 - 2.25577e-5 * this.alt, 5.25588);
    this.volt -= (this.state === 'BOOST' ? 0.0016 : 0.00022) * dt * this.hz / 10;

    // --- link quality degrades with slant range ---
    const rssi = -42 - this.alt * 0.022 + this._noise(2.5);
    const sats = this.state === 'ARMED' ? 7 : Math.min(12, 8 + Math.round(this.alt / 900));

    const pkt = {
      seq: this.seq,
      t: this.t,
      state: this.state,
      alt: this.alt,
      vz: this.vz,
      gs,
      lat: this.lat,
      lon: this.lon,
      roll, pitch,
      yaw: this.yaw,
      temp,
      press,
      volt: this.volt,
      sats,
      rssi
    };

    // Drop the occasional packet so the loss counter is exercised.
    if (Math.random() > 0.012 && this.onPacket) this.onPacket(pkt);
  }

  _noise(scale) { return (Math.random() - 0.5) * 2 * scale; }
}

/* ------------------------------------------------------------------ *
 * WebSocketSource — for a real radio.
 *
 * Point it at whatever bridges your receiver to the browser (a small
 * Node/Python process reading the serial port and forwarding JSON, for
 * example). It expects one JSON packet per message in the shape above;
 * adapt _decode() if your frames differ.
 *
 *   station.setSource(new WebSocketSource('ws://localhost:8081'));
 * ------------------------------------------------------------------ */
class WebSocketSource {
  constructor(url) {
    this.url = url;
    this.onPacket = null;
    this.vehicle = 'LINK';
    this._ws = null;
  }

  connect() {
    this._ws = new WebSocket(this.url);
    this._ws.onmessage = (ev) => {
      const pkt = this._decode(ev.data);
      if (pkt && this.onPacket) this.onPacket(pkt);
    };
    this._ws.onerror = () => console.warn('[telemetry] socket error', this.url);
  }

  disconnect() {
    if (this._ws) { this._ws.close(); this._ws = null; }
  }

  _decode(raw) {
    try {
      return JSON.parse(raw);
    } catch (e) {
      console.warn('[telemetry] bad frame:', raw);
      return null;
    }
  }
}
