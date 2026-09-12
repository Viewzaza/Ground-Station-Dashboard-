/* Ground station application: holds the flight history, keeps the panels in
 * sync with it, and owns the connect / pause / export controls.
 */

const MAX_POINTS = 6000;      // ~10 min at 10 Hz; keeps memory flat
const MAX_LOG_ROWS = 200;
const STALE_MS = 1500;        // no packet for this long => link is stale

const STATES = ['IDLE', 'ARMED', 'BOOST', 'COAST', 'APOGEE', 'DROGUE', 'MAIN', 'LANDED'];

const LIMITS = {
  voltCaution: 7.4,
  voltAlarm: 7.0,
  tempCaution: 55,
  tempAlarm: 70,
  rssiCaution: -95
};

const $ = id => document.getElementById(id);

const station = {
  source: null,
  connected: false,
  paused: false,

  history: [],        // every packet received
  origin: null,       // first GPS fix, used as the track origin
  last: null,
  lastRxAt: 0,

  expectedSeq: null,
  lost: 0,
  rxTimes: [],        // arrival times, for the packet rate
  maxAlt: 0,
  maxVz: 0,

  setSource(src) {
    if (this.connected) this.disconnect();
    this.source = src;
  },

  connect() {
    if (!this.source || this.connected) return;
    this.source.onPacket = pkt => this.ingest(pkt);
    this.source.connect();
    this.connected = true;
    $('vehicle-id').textContent = this.source.vehicle || '';
    paintControls();
  },

  disconnect() {
    if (!this.connected) return;
    this.source.disconnect();
    this.connected = false;
    this.paused = false;
    paintControls();
  },

  clear() {
    this.history = [];
    this.origin = null;
    this.last = null;
    this.expectedSeq = null;
    this.lost = 0;
    this.rxTimes = [];
    this.maxAlt = 0;
    this.maxVz = 0;
    $('log-body').innerHTML = '';
    render();
  },

  ingest(pkt) {
    if (this.paused) return;

    const now = performance.now();
    this.lastRxAt = now;
    this.rxTimes.push(now);
    if (this.rxTimes.length > 60) this.rxTimes.shift();

    // Gaps in the sequence number are packets the radio lost.
    if (this.expectedSeq !== null && pkt.seq > this.expectedSeq) {
      this.lost += pkt.seq - this.expectedSeq;
    }
    this.expectedSeq = pkt.seq + 1;

    this.history.push(pkt);
    if (this.history.length > MAX_POINTS) this.history.shift();

    if (!this.origin && pkt.sats >= 4) this.origin = { lat: pkt.lat, lon: pkt.lon };
    if (pkt.alt > this.maxAlt) this.maxAlt = pkt.alt;
    if (pkt.vz > this.maxVz) this.maxVz = pkt.vz;

    this.last = pkt;
    logRow(pkt);
  },

  packetRate() {
    if (this.rxTimes.length < 2) return 0;
    const span = (this.rxTimes[this.rxTimes.length - 1] - this.rxTimes[0]) / 1000;
    return span > 0 ? (this.rxTimes.length - 1) / span : 0;
  }
};

/* ---------------- charts ---------------- */

const altChart = new StripChart($('chart-alt'), { color: C.accent, unit: 'm', window: 150 });
const vzChart = new StripChart($('chart-vz'), { color: C.ok, unit: 'm/s', window: 150, zeroLine: true });

/* ---------------- formatting ---------------- */

function fmtMET(t) {
  const s = Math.max(0, Math.floor(t));
  const hh = String(Math.floor(s / 3600)).padStart(2, '0');
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  return 'T+' + hh + ':' + mm + ':' + ss;
}

function num(v, dp) {
  return (v === undefined || v === null || Number.isNaN(v)) ? '--' : v.toFixed(dp);
}

/* Great-circle distance, metres. */
function haversine(a, b) {
  const R = 6371000;
  const toRad = d => d * Math.PI / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLon = toRad(b.lon - a.lon);
  const s = Math.sin(dLat / 2) ** 2 +
            Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(s));
}

/* ---------------- panels ---------------- */

function logRow(pkt) {
  const body = $('log-body');
  const tr = document.createElement('tr');
  tr.innerHTML =
    '<td>' + pkt.seq + '</td>' +
    '<td>' + fmtMET(pkt.t) + '</td>' +
    '<td>' + pkt.state + '</td>' +
    '<td>' + num(pkt.alt, 1) + '</td>' +
    '<td>' + num(pkt.vz, 1) + '</td>' +
    '<td>' + num(pkt.volt, 2) + '</td>' +
    '<td>' + num(pkt.rssi, 0) + '</td>';
  body.prepend(tr);
  while (body.children.length > MAX_LOG_ROWS) body.lastChild.remove();
  $('pkt-count').textContent = station.history.length + ' pkt';
}

function paintStates(state) {
  const at = STATES.indexOf(state);
  for (const li of document.querySelectorAll('.states li')) {
    const i = STATES.indexOf(li.dataset.state);
    li.classList.toggle('now', i === at);
    li.classList.toggle('past', at > -1 && i < at);
  }
}

function paintAlerts(pkt) {
  const out = [];
  if (!pkt) {
    $('alerts').innerHTML = '';
    return;
  }
  if (pkt.volt <= LIMITS.voltAlarm) out.push(['bad', 'BATTERY CRITICAL']);
  else if (pkt.volt <= LIMITS.voltCaution) out.push(['warn', 'BATTERY LOW']);

  if (pkt.temp >= LIMITS.tempAlarm) out.push(['bad', 'OVERTEMP']);
  else if (pkt.temp >= LIMITS.tempCaution) out.push(['warn', 'TEMP HIGH']);

  if (pkt.sats < 4) out.push(['warn', 'NO GPS FIX']);
  if (pkt.rssi <= LIMITS.rssiCaution) out.push(['warn', 'WEAK SIGNAL']);
  if (station.connected && performance.now() - station.lastRxAt > STALE_MS) {
    out.push(['bad', 'SIGNAL LOST']);
  }

  $('alerts').innerHTML = out
    .map(([k, t]) => '<span class="alert ' + k + '">' + t + '</span>')
    .join('');
}

function paintTiles(pkt) {
  $('v-alt').textContent = num(pkt.alt, 1);
  $('v-altmax').textContent = num(station.maxAlt, 1) + ' m';
  $('v-vz').textContent = num(pkt.vz, 1);
  $('v-vzmax').textContent = num(station.maxVz, 1) + ' m/s';
  $('v-gs').textContent = num(pkt.gs, 1);
  $('v-hdg').textContent = Math.round(pkt.yaw) + '°';

  $('v-volt').textContent = num(pkt.volt, 2);
  const pct = Math.max(0, Math.min(100, ((pkt.volt - 6.6) / (8.4 - 6.6)) * 100));
  const bar = $('v-voltbar');
  bar.style.width = pct + '%';
  bar.className = pkt.volt <= LIMITS.voltAlarm ? 'crit'
                : pkt.volt <= LIMITS.voltCaution ? 'low' : '';
  $('v-volt').closest('.tile').className =
    'tile' + (pkt.volt <= LIMITS.voltAlarm ? ' alarm'
            : pkt.volt <= LIMITS.voltCaution ? ' caution' : '');

  $('v-temp').textContent = num(pkt.temp, 1);
  $('v-press').textContent = num(pkt.press, 1);
  $('v-temp').closest('.tile').className =
    'tile' + (pkt.temp >= LIMITS.tempAlarm ? ' alarm'
            : pkt.temp >= LIMITS.tempCaution ? ' caution' : '');

  $('v-sats').textContent = pkt.sats;
  $('v-fix').textContent = pkt.sats >= 4 ? '3D fix' : 'no fix';
  $('v-sats').closest('.tile').className = 'tile' + (pkt.sats < 4 ? ' caution' : '');
}

function paintControls() {
  const b = $('btn-connect');
  b.textContent = station.connected ? 'Disconnect' : 'Connect';
  b.classList.toggle('on', station.connected);
  $('btn-pause').disabled = !station.connected;
  $('btn-pause').textContent = station.paused ? 'Resume' : 'Pause';
}

function paintLink() {
  const dot = $('link-dot');
  const stale = station.connected && performance.now() - station.lastRxAt > STALE_MS;

  dot.className = 'dot' + (station.connected ? (stale ? ' stale' : ' live') : '');
  $('link-state').textContent = !station.connected ? 'OFFLINE'
                              : stale ? 'STALE'
                              : station.paused ? 'PAUSED' : 'LOCKED';
  $('rssi').textContent = station.last ? Math.round(station.last.rssi) + ' dBm' : '--';
  $('rate').textContent = (station.connected && !stale)
    ? station.packetRate().toFixed(1) + ' Hz' : '0 Hz';
  $('lost').textContent = station.lost;
}

/* ---------------- render loop ---------------- */

function render() {
  const pkt = station.last;

  paintLink();
  paintAlerts(pkt);

  if (!pkt) {
    $('met').textContent = 'T+00:00:00';
    paintStates(null);
    altChart.draw([]);
    vzChart.draw([]);
    drawADI($('adi'), 0, 0);
    drawCompass($('compass'), 0);
    drawTrack($('track'), [], null);
    return;
  }

  $('met').textContent = fmtMET(pkt.t);
  paintStates(pkt.state);
  paintTiles(pkt);

  altChart.draw(station.history.map(d => ({ t: d.t, v: d.alt })));
  vzChart.draw(station.history.map(d => ({ t: d.t, v: d.vz })));

  drawADI($('adi'), pkt.roll, pkt.pitch);
  drawCompass($('compass'), pkt.yaw);
  $('v-roll').textContent = Math.round(pkt.roll) + '°';
  $('v-pitch').textContent = Math.round(pkt.pitch) + '°';
  $('v-yaw').textContent = Math.round(pkt.yaw) + '°';

  const fixes = station.history.filter(d => d.sats >= 4);
  drawTrack($('track'), fixes, station.origin);
  $('v-lat').textContent = pkt.lat.toFixed(5);
  $('v-lon').textContent = pkt.lon.toFixed(5);
  $('v-dist').textContent = station.origin
    ? Math.round(haversine(station.origin, pkt)) : '--';
}

/* Draw on a frame tick rather than per packet: a fast link would otherwise
 * redraw the whole dashboard dozens of times a second for no visible gain. */
function loop() {
  render();
  requestAnimationFrame(loop);
}

/* ---------------- export ---------------- */

const CSV_FIELDS = ['seq', 't', 'state', 'alt', 'vz', 'gs', 'lat', 'lon',
                    'roll', 'pitch', 'yaw', 'temp', 'press', 'volt', 'sats', 'rssi'];

function exportCSV() {
  if (!station.history.length) return;
  const rows = [CSV_FIELDS.join(',')];
  for (const p of station.history) {
    rows.push(CSV_FIELDS.map(k => {
      const v = p[k];
      return typeof v === 'number' ? v.toFixed(4) : v;
    }).join(','));
  }
  const blob = new Blob([rows.join('\n')], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'telemetry-' + new Date().toISOString().replace(/[:.]/g, '-') + '.csv';
  a.click();
  URL.revokeObjectURL(url);
}

/* ---------------- wiring ---------------- */

$('btn-connect').addEventListener('click', () => {
  station.connected ? station.disconnect() : station.connect();
});

$('btn-pause').addEventListener('click', () => {
  station.paused = !station.paused;
  paintControls();
});

$('btn-export').addEventListener('click', exportCSV);

$('btn-clear').addEventListener('click', () => {
  station.clear();
  station.expectedSeq = null;
});

// Default source: the built-in flight simulator. Swap it for a real radio with
//   station.setSource(new WebSocketSource('ws://localhost:8081'));
station.setSource(new SimSource({ hz: 10 }));

paintControls();
loop();
