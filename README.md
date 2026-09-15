# KNACKSAT-2 Ground Station Dashboard

An operator display for the KNACKSAT-2 ground control station run by **SatNOGS
station 5024 — INSTED-Ground Station (UHF)**, grid OK03gt, 60 m ASL.

Two camera feeds sit in the centre of the screen, with live satellite tracking
to their left, pass and antenna information to their right, and KNACKSAT-2
telemetry along the bottom. It is built for a wall-mounted display that is left
running, and reflows down to a phone.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ KNACKSAT-2 · 67683 │ UTC / ICT │ NEXT AOS -00:14:22 │ ● API ● CAM ● ROT ● TLE │
├─────────────────┬────────────────────────────────┬───────────────────────────┤
│  GROUND TRACK   │                                │ SATELLITE (amateur cat.)  │
│  footprint,     │            CAMERA              │ NEXT PASS  AOS/TCA/LOS    │
│  terminator     │      (one camera, main)        │ ROTATOR    polar + control │
├─────────────────┤                                │ SATNOGS    5024 activity  │
│  ORBIT (3D)     │                                │                           │
├─────────────────┼────────────────────────────────┴───────────────────────────┤
│ RADIO           │ GRAFANA  [beacon] [batt V] [solar W] [batt °C]   open full ↗│
│ tuned + doppler │                                                            │
└─────────────────┴────────────────────────────────────────────────────────────┘
```

## Status

| Area | State |
|---|---|
| Layout, health chips, config-driven frontend | done |
| Camera tiles (go2rtc, WebRTC with MSE/HLS/MJPEG/snapshot fallback) | done, **verified against the real camera** |
| 2D ground track, footprint, terminator | done |
| 3D orbit globe (three.js, offline, no imagery download) | done |
| Radio panel — transmitters and live Doppler from SatNOGS DB | done |
| Satellite selector, pass prediction, next-pass card | done |
| Rotator read-out (polar plot, predicted arc, cable wrap) | done, **verified against the real rotctld** |
| Live WebSocket (rotator, pointing error, status, reconnect) | done |
| Rotator **control** behind the SatNOGS interlock | done, refusal path verified on site |
| SatNOGS 5024 activity feed | done |
| Grafana telemetry strip | done |

The rotator control path has been exercised against the station's own rotctld
and correctly **refused** every command, because satnogs-client was connected.
No command that moves the antenna has yet been executed against the hardware —
see [Rotator control](#rotator-control).

## Running it

### On this machine, without Docker

```bash
python -m venv backend/.venv
backend/.venv/Scripts/python -m pip install -r backend/requirements.txt
sh tools/fetch_vendor.sh            # three.js, ~1.3 MB, not committed
backend/.venv/Scripts/python tools/dev_server.py
```

Then open <http://localhost:8000>. The API and the frontend are served from one
origin, so there is no CORS anywhere. The camera tiles will report the bridge as
unreachable unless go2rtc is also running — that is a real state the UI is built
to show, not a failure.

### With Docker

```bash
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

Development uses generated video test patterns. In production:

```bash
cp .env.example .env      # set GS_MOCK=0 and fill in the real values
echo -n 'camera-password' > deploy/secrets/camera_password.txt
chmod 600 deploy/secrets/camera_password.txt
docker compose up -d
```

Four services: `caddy` (single origin, `tls internal`), `backend`, `video`
(go2rtc) and `groundstation` — the separate `sgoudelis/ground-station` suite,
started only with `--profile sdr`.

## Architecture

```
browser ──HTTPS──▶ Caddy ──┬── /            frontend (static, no build step)
                           ├── /api, /ws    backend   (FastAPI + Skyfield)
                           ├── /video/*     go2rtc    (RTSP → WebRTC)
                           └── /sdr/*       ground-station suite (separate, GPL)
                                  │
                    rotctld 10.90.36.140 ── ONE socket, 1 Hz
                    camera  10.90.36.130 ── RTSP over TCP
                    SatNOGS · Celestrak · Grafana (iframes only)
```

**Skyfield owns pass prediction, not the browser.** The schedule has to keep
running with no browser open, and it is the same schedule the antenna will be
driven from. The browser runs its own SGP4 (satellite.js) purely for the smooth
1 Hz render of where the satellite is now.

**Everything the frontend knows comes from `/api/config`** — station
coordinates, Grafana panel ids, camera stream names, feature flags. Deploying to
a different station is an `.env` edit.

## Things that are the way they are for a reason

Each of these cost time to find. Please read before changing them.

- **Celestrak enforces its fetch etiquette.** Repeating a download before the
  data has changed returns HTTP 403, and 50 errors in two hours puts your IP in
  their firewall. `tle_store.py` never touches the network while the cache is
  under `GS_TLE_TTL_S` (7200 s) old and treats any non-200 as terminal. Do not
  lower that TTL, and do not add a retry loop.

- **Match the station's horizon mask.** Station 5024 publishes
  `min_horizon = 0`. With a plausible-looking 5° default our AOS ran a
  consistent 74–96 seconds late against SatNOGS's own schedule; at 0° the two
  agree to within a few seconds. `tests/test_satnogs_oracle.py` asserts this
  against the live API.

- **`satellite__norad_cat_id` does not filter the SatNOGS Network API.** It is
  silently ignored and you get every satellite. Use `norad_cat_id`. Network API
  pagination is cursor-based through the `Link: rel="next"` *header* — there is
  no `?page=`.

- **The antimeridian.** A ground track stepping from lon 179 to −179 draws a
  line across the entire map unless the path is split and the crossing latitude
  interpolated. Every path goes through `split_antimeridian()`. The same applies
  to the footprint ring, which additionally does not close at all when it
  contains a pole.

- **The globe draws its own surface; there is no imagery to download.** It was
  CesiumJS, which is 23 MB fetched by a setup script — and because it was
  fetched rather than committed, a clone that skipped that step showed a black
  rectangle with no hint why. That is exactly how it was found. It is now
  three.js (1.3 MB, MIT, plain ES module) and the sphere's texture is drawn at
  load time into a 2048×1024 canvas from `assets/ne_110m_land.json`, the same
  public-domain Natural Earth outline the 2D map uses. So the 3D and 2D
  coastlines cannot disagree, and nothing is fetched at runtime.

- **A dark palette plus a directional light is a black disc.** The first
  version used the console's own near-black surface colours and let lighting do
  the rest; every one of them multiplied down to indistinguishable black, and
  the globe rendered as a silhouette with a track floating on it. The surface
  now uses its texture as an `emissiveMap` as well as a `map`: emissive is a
  floor that puts the coastlines on screen wherever the sun is, and the
  directional light adds the day side on top so the terminator is still
  visible. Ambient is kept low, because raising it washes the terminator out.

- **The frame is earth-fixed, not inertial.** Spinning the planet under a fixed
  orbit ring looks better in isolation, but this panel sits beside a 2D ground
  track and a polar plot, and all three should answer the same question: where
  is the satellite relative to *our* ground. Earth-fixed makes the 3D and 2D
  tracks literally the same line. Rendering is on demand — a
  `requestAnimationFrame` loop spinning a GPU at 60 Hz to move a marker that
  updates at 1 Hz is just heat in a rack that runs for months.

- **`satellite__norad_cat_id` filters the DB API but not the Network API.** The
  two SatNOGS services do not share a convention, and both fail silently in the
  same direction — an ignored filter returns every satellite rather than an
  error. Network wants `norad_cat_id`; DB, which is where transmitters come
  from, wants `satellite__norad_cat_id`.

- **A satellite's transmitters are not interchangeable.** KNACKSAT-2 publishes
  a 145.825 MHz V/V digipeater and a 400.630 MHz UHF telemetry downlink, and
  station 5024 is a UHF station: its three Yagis span 380–490 MHz. Taking "the
  first transmitter" would tune the panel to a band the antenna cannot hear, so
  `primary_downlink()` prefers a live transmitter inside the station's band.
  Both are still shown — the operator is told which one is primary, not denied
  the other.

- **Grafana's "Powered by Grafana" badge can only be covered, not removed.**
  The panels are cross-origin iframes, so no stylesheet or script of ours can
  reach inside them. `.graf-cell::after` masks the top-right corner where the
  badge sits; the panel title is top-left and the value is centred, so nothing
  readable is behind it. If Grafana moves the badge, move the mask.

- **Grafana sends no CORS headers**, so their telemetry cannot be fetched, only
  embedded. Their panels also need `var-DS_INFLUXDB` or they render empty. The
  embeds work because that instance has anonymous access enabled — if the
  KNACKSAT team turns it off, the panels go blank and `GS_GRAFANA_TOKEN` plus a
  backend proxy become necessary.

- **The camera password never reaches the browser.** Snapshots are proxied
  through `/api/cameras/{id}/snapshot.jpg` rather than linked, because the
  camera speaks plain HTTP with Digest auth: a direct URL would be blocked as
  mixed content and would expose the credentials in page source.

- **Hamlib prints `Min Azimuth`, not `Minimum Azimuth`.** The caps parser
  originally looked for the long spelling, found nothing, and fell back to its
  defaults — which are exactly the SPID 901's range, so against the only
  rotator we had it looked perfect. On any other model the clamp in
  `set_position` would have permitted a position the controller refuses. The
  parser now accepts both spellings and a test asserts a 903's range is read
  rather than assumed. Limits guard a physical end stop: never default them
  quietly.

- **One rotctld socket, process-wide.** rotctld spawns a thread per connection
  with no mutex around the shared rotator handle, so concurrent clients can
  interleave writes mid-frame and corrupt an in-progress track. N browser tabs
  must produce exactly one connection. Poll at 1 Hz — a 600-baud ROT2PROG
  cannot sustain 2 Hz.

## Rotator control

Control is **off by default** (`GS_ROTATOR_CONTROL_ENABLED=0`) and, when
enabled, is refused unless all four gates pass:

| Gate | Source |
|---|---|
| SatNOGS client is down | station 5024 reports `is_connected = false` |
| No imminent pass | no scheduled job within `GS_GATE_GUARD_S` of now |
| Operator armed | an explicit arm, expiring after `GS_CONTROL_LEASE_S` |
| Kill switch | `GS_ROTATOR_CONTROL_ENABLED=1` |

The station's own `is_connected` flag is used as the signal that satnogs-client
is running, rather than mounting the Docker socket, so the backend keeps minimal
privilege. If the reference `ground-station` suite is also deployed, its rotator
integration must stay disabled — this backend is the single writer.

`POST /api/control/{arm,release,goto,park,track,stop}`, and the same commands
over the WebSocket, all pass through one `ControlService`, so a gate shut to one
is shut to both. A refusal is a 409 naming the gates, which is what the panel
renders — an operator who presses GO and nothing happens can see *which* gate
is closed without reading a log.

Three details that are easy to get wrong, and are covered by tests:

- **Unknown is not permission.** If SatNOGS has not answered, or its answer is
  older than `GS_GATE_MAX_STALE_S`, the gates fail. A four-minute-old all-clear
  is precisely the window in which satnogs-client would have picked up a job.
- **The gates are re-read during a track, not only on entry.** A job gets
  scheduled or the lease expires, and the track abandons itself.
- **`stop` is deliberately not gated.** If the antenna is moving and the
  operator wants it stopped, an expired lease is not a reason to keep driving.
  The STOP button stays enabled whatever the gates say.

On site this refuses correctly today: station 5024 is connected, so
`satnogs_idle` is shut and every move is answered with
`{"error": "refused: satnogs_idle", "blocked_by": ["satnogs_idle"]}`. Nothing
has yet commanded the real antenna to move. Before it does, someone should have
eyes on the mast and the station should be out of the SatNOGS schedule.

## Tests

```bash
cd backend
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest              # offline: 96 tests
.venv/Scripts/python -m pytest -m network   # cross-checks against live SatNOGS
```

Most of `tests/test_control.py` exists to prove the interlock refuses rather
than that it works: each test names the unsafe thing it prevents. That is the
file to read first if you are changing anything that can move the antenna.

To exercise the real rotctld parser without a rotator, run the fake and point
the backend at it — this is the code path that will meet the hardware, which
`GS_MOCK=1` does not touch:

```bash
python tools/fake_rotctld.py --model 903          # rotctld on 4533
python tools/fake_rotctld.py --kind rig          # a radio on 4532, must be refused
python tools/fake_rotctld.py --split-frames      # replies one byte at a time
```

## Development on a machine that cannot reach the station LAN

`GS_MOCK=1` is the only flag. It selects implementations at construction time,
so there are no `if mock:` branches in the business logic.

- **Cameras** — `deploy/go2rtc/go2rtc.mock.yaml` declares the *same stream
  names* as production, backed by generated video. No frontend or backend code
  differs between the two.
- **Rotator** — a simulator driven by the real predictor, so it tracks an actual
  KNACKSAT-2 pass, crosses 360° into the cable-wrap range and injects link
  faults. `tools/fake_rotctld.py` additionally speaks the real wire protocol, so
  the actual parser can be exercised without hardware.
- **SatNOGS, Celestrak and Grafana** are public, so development exercises the
  production path. `GS_OFFLINE=1` falls back to fixtures.

## What the hardware actually is

These were open questions. They were settled on 2026-09-13 by probing the
station directly; `deploy/commissioning/` holds the scripts that did it, and
re-running them is the way to check whether any of it has changed.

| Question | Answer |
|---|---|
| Rotator on 4532 or 4533? | **4533.** 4532 is closed — there is no rigctld at all. |
| Rotator type | `Rot type: Az-El` — a rotator, not a radio. |
| SPID model 901 or 903? | **901**, `Model name: Rot2Prog`, Mfg `SPID`. |
| Azimuth / elevation range | −180…540 and −20…210, matching the defaults. |
| Serial | 600 baud 8N1, 300 ms post-write delay, 400 ms timeout, 3 retries. |
| Camera H.264 or H.265? | **H.264** on both channels — `profile-level-id=420029`, Baseline 4.1, `packetization-mode=1`. WebRTC carries it with no transcode. |
| One camera or two? | **One.** Hikvision DS-2CD1023G2-LIUF/SL, `INSTED-GS_1`, firmware V5.8.4. Channel 101 is 1920×1080, 102 is 640×360. |

Two of those answers changed the code:

- **`Can Park: N`.** The 901 has no park command. Asking for one returns an
  error and the antenna does not move, so park is an ordinary `set_pos` to the
  configured park coordinates. `Can Move: N` and `Can Reset: N` likewise — only
  `set_pos` and `stop` are actually available.
- **There is one camera, not two.** The tiles were labelled "Camera 1" and
  "Camera 2", which implies a redundancy that does not exist: both are views of
  the same device, so losing it blanks both. They now read `main · 1080p` and
  `sub · 360p`.

One more thing worth knowing before the rotator is switched on again: with the
SPID controller powered down, rotctld still answers `dump_caps` from its
compiled-in capabilities — model, ranges and all — while every `get_pos`
returns `RPRT -5` after 2.8–4.6 s of serial retries. So **capabilities being
readable is not evidence that the rotator is alive.** The dashboard reports
this state as `ROT down` with the polar plot's antenna marker absent, which is
correct, and the near-5 s cost of each failed read is why the poll loop backs
off rather than retrying at 1 Hz.

## Licence

MIT — see `LICENSE`. The `sgoudelis/ground-station` suite referenced in
`docker-compose.yml` is GPL-3.0 and runs as a **separate container**; no code
from it is present in this repository.

Coastlines are Natural Earth (public domain). three.js is MIT and is fetched by
`tools/fetch_vendor.sh` (with its licence) rather than committed. Transmitter
data comes from SatNOGS DB at runtime and is cached, not vendored.
