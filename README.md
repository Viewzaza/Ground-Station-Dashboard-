# Ground Station Dashboard

A browser telemetry dashboard for a small rocket / CanSat-class vehicle. No
build step, no dependencies — open `index.html` and it runs.

It ships with a flight simulator so the whole dashboard works with nothing
attached. Point it at a real radio by swapping one line (see
[Attaching a real feed](#attaching-a-real-feed)).

## Running it

Open `index.html` directly, or serve the folder:

```bash
python -m http.server 8099
```

Then open <http://localhost:8099> and press **Connect**. The simulator flies a
sounding-rocket profile — pad, boost, coast, apogee, drogue, main, landing —
in about 90 seconds.

## What it shows

| Panel | Contents |
|---|---|
| Header | link state, RSSI, packet rate, lost-packet count, mission elapsed time |
| State bar | flight phase, plus caution/alarm flags for battery, temperature, GPS and signal |
| Tiles | altitude, vertical speed, ground speed, battery, temperature/pressure, GPS |
| Charts | altitude and vertical speed against mission time, 150 s window |
| Attitude | artificial horizon (roll/pitch) and heading rose |
| Ground track | downrange trail with range rings, launch site at the origin |
| Packet log | last 200 frames, newest first |

**Export CSV** writes the whole received history — every field, not just what
is on screen. **Pause** freezes the display without dropping the link;
**Clear** discards the history and starts a fresh log.

## Packet format

Every source emits one flat JSON object per packet:

```json
{
  "seq": 412, "t": 41.2, "state": "COAST",
  "alt": 1127.6, "vz": -17.5, "gs": 6.5,
  "lat": 13.75662, "lon": 100.50214,
  "roll": 9, "pitch": -59, "yaw": 170,
  "temp": 17.3, "press": 888.8,
  "volt": 8.30, "sats": 9, "rssi": -67
}
```

| Field | Meaning |
|---|---|
| `seq` | packet counter from the vehicle — gaps are counted as lost packets |
| `t` | mission elapsed time, seconds |
| `state` | `IDLE` `ARMED` `BOOST` `COAST` `APOGEE` `DROGUE` `MAIN` `LANDED` |
| `alt` | altitude above the launch site, m |
| `vz` | vertical speed, m/s, positive up |
| `gs` | ground speed, m/s |
| `lat` / `lon` | degrees; the first fix with `sats >= 4` becomes the track origin |
| `roll` / `pitch` / `yaw` | degrees |
| `temp` / `press` | °C / hPa |
| `volt` | battery volts |
| `sats` | GPS satellites; below 4 the dashboard reports no fix |
| `rssi` | dBm |

## Attaching a real feed

A browser page cannot open a serial port directly, so a small bridge process
reads the receiver and forwards JSON over a WebSocket. Then, at the bottom of
`js/app.js`, replace the simulator:

```js
station.setSource(new WebSocketSource('ws://localhost:8081'));
```

A minimal bridge (Node, with `npm i ws serialport`):

```js
const { WebSocketServer } = require('ws');
const { SerialPort, ReadlineParser } = require('serialport');

const wss = new WebSocketServer({ port: 8081 });
const port = new SerialPort({ path: 'COM5', baudRate: 57600 });

port.pipe(new ReadlineParser({ delimiter: '\n' })).on('data', line => {
  // Reshape your frame into the packet format above, then broadcast it.
  for (const client of wss.clients) client.send(line);
});
```

If your radio sends something other than one JSON object per line — a CSV
frame, or a packed binary struct — do the conversion in `_decode()` in
`js/telemetry.js` rather than changing the UI.

## Layout

```
index.html        markup and panel structure
css/styles.css    dark console theme; all colours are CSS variables
js/telemetry.js   packet sources: SimSource (built-in flight), WebSocketSource
js/charts.js      canvas drawing: strip charts, ADI, compass, ground track
js/app.js         history, panel updates, controls, CSV export
```

The UI never talks to a source directly — it reads `station.history` and the
latest packet. Anything that can produce packets in the format above works
without touching the panels.

## Tuning

| Where | Value | Meaning |
|---|---|---|
| `js/app.js` | `LIMITS` | caution/alarm thresholds for battery, temperature, signal |
| `js/app.js` | `MAX_POINTS` | packets kept in memory (6000 ≈ 10 min at 10 Hz) |
| `js/app.js` | `STALE_MS` | silence before the link is flagged stale |
| `js/app.js` | `StripChart` `window` | seconds of history visible on the charts |
| `js/telemetry.js` | `SimSource` constants | burn time, thrust, drag, descent rates |

The battery bar assumes a 2S pack (6.6–8.4 V); change the range in
`paintTiles()` for a different pack.
