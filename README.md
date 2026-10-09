# KNACKSAT-2 Ground Station Dashboard

An operator display for the KNACKSAT-2 ground control station run by **SatNOGS
station 5024 — INSTED-Ground Station (UHF)**, grid OK03gt, 60 m ASL.

The camera sits in the centre of the screen, with live satellite tracking to its
left, the next pass and the antenna to its right, radio and the last pass's
signal under them. Along the bottom is one band of four equal panels: the frames
SatNOGS decoded, which satellite is being tracked, what station 5024 has been
doing, and the team's Grafana. It is built for a wall-mounted display that is
left running, and reflows down to a phone.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ KNACKSAT-2 · 67683 │ UTC/ICT │ NEXT AOS -00:14:22 │ ● API ● CAM ● ROT ● TLE  │
├─────────────────┬────────────────────────────────┬───────────────────────────┤
│  GROUND TRACK   │                                │ NEXT PASS  AOS/TCA/LOS    │
│  footprint,     │            CAMERA              │ ROTATOR    polar plot,    │
│  terminator     │        one tile, main          │            cable wrap     │
├─────────────────┤    (dark instrument window)    │            + control      │
│  ORBIT (3D)     │                                │                           │
│  earth-fixed    │                                │                           │
├─────────────────┼────────────────────────────────┴───────────────────────────┤
│ RADIO           │ LAST SIGNAL   waterfall of the most recent 5024 pass       │
│ tuned + doppler │ time ─────────────────────────────────────────────────▶    │
├─────────────────┴─┬───────────────────┬───────────────────┬──────────────────┤
│ DECODED FRAMES    │ SATELLITE         │ SATNOGS 5024      │ GRAFANA       ↗  │
│ 48 min ago        │ search the        │ client, next job  │ [beacon][batt V] │
│  01:34Z 165 B HS0K│ amateur catalogue │ recent obs        │ [solar][batt °C] │
└───────────────────┴───────────────────┴───────────────────┴──────────────────┘
```

Light paper chrome, dark instrument windows — see
[Light panel, dark instruments](#light-panel-dark-instruments).

## Status

| Area | State |
|---|---|
| Layout, health chips, config-driven frontend | done |
| Light instrument-panel theme, dark instrument windows | done |
| Camera tile (go2rtc, WebRTC with MSE/HLS/MJPEG/snapshot fallback) | done, **verified against the real camera** |
| 2D ground track, footprint, terminator | done |
| 3D orbit globe (three.js, NASA Blue Marble or drawn coastlines, offline) | done |
| Radio panel — transmitters and live Doppler from SatNOGS DB | done |
| "Last signal" — the previous pass's waterfall, cropped and lifted | done |
| Satellite selector, pass prediction, next-pass card | done |
| Rotator read-out (polar plot, predicted arc, cable wrap) | done, **verified against the real rotctld** |
| Live WebSocket (rotator, pointing error, status, reconnect) | done |
| Rotator **control** behind the SatNOGS interlock | done, refusal path verified on site |
| SatNOGS 5024 activity feed | done |
| Grafana telemetry strip | done, cut back to one stat's height |
| Decoded frames — the most recent frames SatNOGS demodulated | done, **no token needed** |
| Observation planner — which passes to work when they compete | done, verified against the live SatNOGS schedule |
| Autopilot — works the plan through the interlock | done, **never yet run against the hardware** |
| Observation plan panel — strip, reasons, ENGAGE / DISENGAGE | done, verified in the browser against the mock rotator |

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

### What size VM this wants

|  | vCPU | RAM | Disk |
|---|---|---|---|
| Minimum that works | 1 | 1.5 GB | 20 GB |
| **Recommended** | **2** | **4 GB** | **32 GB** |
| With `--profile sdr` | 4+ | 8 GB | 64 GB+ |

Smaller than it looks, because **this is timer- and I/O-bound, not
compute-bound**. Measured, not estimated: the backend sits at **81 MB RSS**
with all six scheduler loops running and 96 satellites loaded, CPU at idle is
below measurement resolution, and each browser costs **0.6 kB/s** on the
WebSocket. There is no JPL ephemeris — propagation is SGP4 plus geodesy, so
nothing mmaps a 120 MB kernel; `load.timescale()` uses builtin IERS data.

The two real costs, both small:

- **Pass prediction.** `_satpos_loop` recomputes `next_pass()` every second
  while the satellite is up: a 24 h `find_events` at 8.0 ms plus look angles,
  **~15 ms**, so about 1.5% of one core during a pass. It is recomputed from
  scratch each tick rather than cached, which is the obvious thing to fix if
  this ever needs to be cheaper.
- **The waterfall.** One **286 ms** single-threaded burst per finished pass
  and ~15 MB transient, of which 84 ms is `find_plot_box()` doing per-pixel
  reads in Python. That is why it runs in `asyncio.to_thread`, and the main
  reason to prefer 2 vCPU over 1 — on one core that burst competes with
  go2rtc.

**Video is passthrough, not transcode.** The camera is H.264 on both channels
and `go2rtc.yaml` applies no `#video=` transform, so WebRTC and MSE are pure
repacketisation of ~8 Mbit/s: a few percent of a core, no ffmpeg.

**The exception is the snapshot fallback, and it is the likeliest way this box
gets unexpectedly busy.** When `<video-stream>` produces no frame for 15 s the
tile polls `/api/cameras/{id}/snapshot.jpg` at 1 Hz, and that path makes go2rtc
decode H.264 and encode JPEG once a second, indefinitely. A display left stuck
in fallback — usually a firewall blocking the WebRTC candidate — costs an order
of magnitude more CPU than a working one. So a networking mistake here shows up
as a *CPU* problem, and the tile now names the reason it fell back (see
["CAMERA DOWN" is usually not the camera](#things-that-are-the-way-they-are-for-a-reason)).

### One VM, and deliberately not two

**Do not scale this horizontally, and do not autoscale it.** Two reasons, both
load-bearing:

- **Celestrak bans by IP.** `tle_store.py` never fetches while the cache is
  under `GS_TLE_TTL_S` old and treats any non-200 as terminal, because 50
  errors in two hours gets the station firewalled. `Scheduler.start()` fetches
  eagerly on every process start, so the on-disk cache is what makes a restart
  cost *zero* requests. `./backend/data:/data` is therefore **the rate
  limiter, not an optimisation** — never reset it as part of a deploy. N
  replicas mean N empty caches, N startup fetches and N× the steady rate from
  one source IP.
- **One writer to rotctld.** rotctld shares a single rotator handle across
  connections with no mutex, so two clients can interleave writes mid-frame on
  a 600-baud serial link. The code guarantees one socket *per process*; that
  guarantee ends at the process boundary. Two instances is two sockets and 2 Hz
  on a line that cannot sustain it. Want redundancy? A cold standby that is not
  running.

There is also nothing to scale *for*: one backend serves N browsers off one
fan-out hub, computing the schedule once regardless of viewer count.

### Proxmox specifics

- **CPU type `host`**, not `kvm64` — numpy's OpenBLAS dispatches on CPUID, and
  `kvm64` masks AVX silently. Costs nothing; there is no live-migration
  requirement for a single wall display. 2 vCPU, one socket, NUMA off.
- **Turn ballooning off** (`balloon: 0`). The footprint is flat and small, so
  ballooning buys nothing, but the balloon driver reclaims page cache under
  host pressure — and a reclaim stall during a pass is a rotctld read timing
  out, which flips the chip to `ROT down` and triggers the poll backoff.
- **Install qemu-guest-agent.** Without it there is no clean shutdown, so a
  host reboot SIGKILLs the containers and can interrupt the TLE cache write
  mid-`write_text`. The code survives that — and the cost of surviving it is
  one unnecessary Celestrak fetch, which is the thing being avoided.
- **VirtIO SCSI single** with `discard=on`, **VirtIO** network, `onboot=1`.
  Disk speed is irrelevant here: the largest write is a 21 KB JSON every half
  hour.
- **x86-64.** Nothing requires it, but `alexxit/go2rtc:latest` is unpinned and
  the SDR path is far better trodden on amd64. The workload is 2% of a core;
  there is no ARM upside to buy with that risk.
- Run a real NTP client **in the guest**. Doppler, pass times and the
  `GS_GATE_MAX_STALE_S` staleness gate all read the guest clock, and that gate
  fails *closed* — a drifting clock presents as rotator control being refused
  for no visible reason.

### Networking is the part that bites

The station's devices are plain IPs in `config.py`, with no DNS and no
discovery: rotctld `10.90.36.140:4533`, rigctld `:4534`, camera
`10.90.36.130:554`. **Bridge the VM onto the station LAN** so it holds a
`10.90.36.x` address itself. Docker's bridge handles container→LAN egress
fine; it cannot invent a route the guest does not have.

Everything outbound still works behind NAT — but **WebRTC does not**, and the
fix is one line. `go2rtc.yaml` ships `candidates: - stun:8555` with a comment
telling you to replace it, and on this LAN you must:

```yaml
webrtc:
  candidates:
    - 10.90.36.50:8555        # the VM's own LAN address, not stun:
```

`stun:` asks a public server for your external address, which is useless to a
display on the same subnet and **times out entirely on an isolated LAN** — the
same failure class as the Google Fonts `<link>` this project refuses for the
same reason. Open **TCP and UDP 8555** inbound, plus 80/443 for Caddy. If both
8555 paths are blocked the tile silently degrades to MSE and then to the 1 Hz
JPEG transcode above.

Caddy's `tls internal` mints its own CA, so install its root on every display
machine once — `docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt .`
— and **do not delete the `caddy_data` volume**, because recreating it
regenerates the CA and every display starts failing its certificate check.

### Before you call it deployed

- **Check `/api/health` reports `"mock": false`.** `.env.example` ships
  `GS_MOCK=1`, and the mock rotator is convincing: green `ROT` chip, moving
  polar plot, a realistic fault every ten minutes. A VM deployed with the
  default `.env` looks perfectly healthy and is talking to a simulator.
- **Monitor component state, not the container.** The backend's healthcheck
  returns `{"ok": true}` while `rotctld` is down and `satnogs` is degraded.
  Poll `/api/health` and alert on the `components` map.
- Compose **v2** is required (`profiles:`, the long-form `depends_on`), so
  install from Docker's own apt repo, not distro `docker-compose`.
- `chmod 600` the `.env` too. The camera password is in it in cleartext, and
  that is the copy that actually reaches go2rtc — `deploy/secrets/camera_password.txt`
  must exist for compose to start, but no backend code reads it.
- `/video/*` is reverse-proxied with no authentication, and go2rtc's config
  holds the camera credentials after env expansion. Fine on a closed LAN;
  check it before the dashboard is reachable from anywhere you do not control.

Log rotation is already configured (`x-logging` in `docker-compose.yml`, 10 MB
× 3 per service). It is not optional on a box that runs for months: Caddy logs
every request, one display is ~50k requests a day, and the default `json-file`
driver never rotates.

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

## Light panel, dark instruments

This was a dark console — near-black page, cyan accent. It is now a light
instrument panel, following the operator's other tool,
[sattrackslop](https://github.com/colabear101/sattrackslop), so the two read as
one set of instruments rather than two unrelated pages. `--surface` #f2f4f5 is
the page, `--panel` #ffffff the cards, `--ink` #0e1a1f the text, `--rule`
#cbd6db the borders. Every colour is in `frontend/css/tokens.css`; nothing else
holds a literal.

**The instrument windows stay dark.** `--void` #0b1116 paints the camera tile,
the 3D globe and the waterfall, and it is deliberately not derived from the rest
of the palette. Space has no light mode: a night rooftop camera, a black globe
or a spectrogram rendered on a white card reads as a broken image rather than a
view. So the chrome is paper and the windows into the sky are not. The Grafana
iframes are the same case — see below.

**Three data hues, and the same three everywhere** — 2D map, 3D globe, polar
plot. `--track` #0086ad is the orbit and the ground track, `--contact` #b4670f
is happening-now (live values, the in-view arc, the satellite itself), and
`--observer` #c42a6e is us. Three is the number that survives: they stay far
apart in hue under deuteranopia, and lightness carries a fourth channel where a
fourth is needed. A wall display is read from across a room, by whoever is in
it.

**The fonts are system stacks, not Google Fonts, on purpose.** The station may
sit on an isolated LAN, and a webfont `<link>` fails silently — the wall display
would simply be in fallback with nothing to say so. Numbers are monospaced and
tabular so a changing digit does not shift the ones beside it.

## Radio and the last signal

`GET /api/radio` lists the satellite's transmitters from SatNOGS DB with the
Doppler shift applied, so the large number on the panel is the frequency to tune
to *now*. The shift is computed on the server, from the same range rate the pass
schedule and the pointing error come from, rather than a second propagation in
the browser that would disagree in the third decimal. Selection is band-aware —
see [transmitters are not
interchangeable](#things-that-are-the-way-they-are-for-a-reason).

`GET /api/radio/waterfall` says which observation is being shown;
`GET /api/radio/waterfall.png` is the image. It finds the most recent finished
observation for this satellite **at station 5024**, downloads the SatNOGS
waterfall, locates the spectrogram inside the matplotlib figure rather than
assuming pixel offsets, crops to the middle of the band, lifts the signal out of
the noise floor and serves a 760x150 strip with time running left to right. The
panel answers the question the radio panel cannot: not what to tune to next
time, but what was actually heard last time.

This is the one thing in the backend that needs an image library, so **Pillow**
is now in `backend/requirements.txt`. The crop runs in a worker thread — it is
CPU-bound on a megapixel image, and on the event loop it would stall every other
poller — and the PNG is proxied rather than linked, because the source is a
1.6 MB S3 object and every wall display would otherwise fetch all of it to show
a strip.

## Decoded frames

`GET /api/telemetry` is the most recent frames SatNOGS has for the tracked
satellite, newest first. It is the leftmost of the four panels along the bottom.
The headline is the **age of the newest frame**.
That is the point of the panel: a satellite propagating perfectly and a
satellite that has been silent for two days look identical on the map, the
globe and the polar plot, and "heard 2 h ago" is the only line on this display
that tells them apart.

It sits on the same line as Grafana because Grafana's leftmost panel — *time
since last beacon* — is asking exactly the question the frame list answers, and
the two disagreeing is worth seeing at a glance rather than one above the other.

There are two sources, and the panel says which one it is showing.

**SatNOGS DB `/telemetry/` carries decoded fields** — named scalars a decoder
produced, `battery_v: 3.92` — and refuses anonymous requests. It is used when
`GS_SATNOGS_DB_TOKEN` is set, because named values beat bytes.

**SatNOGS Network carries the frames themselves, and they are public.** Every
observation publishes a `demoddata` list of URLs, and those objects come back
HTTP 200 with no token from the same bucket the waterfalls do. So on a station
with no token — which is this station — the panel is full rather than empty.
What is lost is the decode: these are bytes off the air. What is recovered from
them is the AX.25 header, which is a published standard rather than a
per-spacecraft guess, so the row can say *who sent it*: KNACKSAT-2's beacons
decode to `HS0K → HS0AK-11`, and SatNOGS DB agrees, publishing that transmitter
as `Mode U - FSK9k6 - AX.25 G3RUH -TLM`. Everything past the header is
spacecraft-specific and stays hex. Naming fields in a beacon whose format we do
not have would put numbers on a wall display that nobody can check.

**The panel is network-wide, not station 5024 only.** 5024 has decoded
KNACKSAT-2 on 4 of its last 25 good passes, so a panel filtered to our own
station would be empty most of the week while the network as a whole was
hearing the spacecraft several times a day. "Is it alive" is answered by
anyone's frame; "did *we* hear it" is a different question, and it gets a
marked row rather than an empty panel.

**Grafana gave up most of its width** to make room, from the full span of the
wall to a quarter of the band, and its four stat panels now wrap two-by-two
inside that quarter. Four stat panels are four numbers however much room they
are given, so narrowing them costs nothing that the frame list beside them does
not use better.

Grafana and the frames are not equally direct: Grafana shows whatever last
reached the team's InfluxDB, which is downstream of everything, while the frames
are what SatNOGS demodulated out of the air. The gap is visible on the wall
right now — Grafana's "time since last beacon" reads 8 hours against the frame
panel's 48 minutes, because they are measuring different things at different
points in the same pipeline. On one line, that is one glance.

**The satellite selector and the 5024 activity feed moved down here too**, out
of the right-hand column. That column was carrying four panels and losing: the
selector had collapsed to its own heading with no list under it. What is left
up there — the next pass and the rotator — is what is watched *during* a pass,
and what came down is what is consulted between them.

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

- **`satellite__norad_cat_id` filters SatNOGS DB but not the Network API.** The
  two services do not share a convention, and both fail in the same direction:
  an ignored filter returns *every* satellite rather than an error, so it looks
  like success until you notice the frequencies belong to other spacecraft. DB,
  which is where transmitters come from, wants `satellite__norad_cat_id`;
  Network — jobs, observations, and the waterfall's "latest pass" lookup — wants
  `norad_cat_id`. The waterfall paid for this lesson a second time, and the
  symptom there is worse, because a strip of someone else's signal still looks
  like a signal. Network pagination is also cursor-based, through the
  `Link: rel="next"` *header*; there is no `?page=`.

- **An unfiltered observation query answers entirely `future` passes.** Network
  returns scheduled observations alongside flown ones, newest `start` first,
  and a LEO satellite has far more scheduled than flown. So
  `?norad_cat_id=<n>` comes back as 25 rows of things that have not happened,
  every one with an empty `demoddata` — which is indistinguishable from a
  satellite nobody has ever heard. `status=good` is what makes the page dense:
  21 of 25 rows carried frames against 0 of 25 unfiltered.

- **`demoddata` is not in time order.** One observation's list came back
  10:28:06, 10:27:36, 10:27:06, 10:31:06 — the newest frame was *fourth*.
  Taking the head of the list as the latest frame therefore usually works and
  occasionally, silently, does not, on the one panel whose whole job is to say
  when the spacecraft was last heard. Every URL is stamped and sorted. The
  per-frame timestamp is in the object name (`data_<obs>_2026-09-17T10-28-06`)
  and nowhere else in the record.

- **A frame's bytes are public; its decode is not.** `/telemetry/` on
  db.satnogs.org is the only SatNOGS endpoint this dashboard touches that
  answers 401 anonymously, and for as long as it was the only source this panel
  had never once had anything on it. The frames were reachable the whole time,
  one API over. A missing token is now a sentence on the panel, not an empty
  panel — see [Decoded frames](#decoded-frames).

- **The AX.25 parser is deliberately strict.** A loose one finds a plausible
  callsign in any sixteen bytes of binary and prints it with exactly the same
  confidence as a real one. Every field is checked: the shift bit on each
  address character, the end-of-address marker landing exactly once, and a
  UI / no-layer-3 control pair. Anything else shows hex, which is honest.

- **The antimeridian.** A ground track stepping from lon 179 to −179 draws a
  line across the entire map unless the path is split and the crossing latitude
  interpolated. Every path goes through `split_antimeridian()`. The same applies
  to the footprint ring, which additionally does not close at all when it
  contains a pole.

- **The globe draws its own surface when there is no imagery, and says which
  one it is using.** It was CesiumJS, which is 23 MB fetched by a setup script
  — and because it was fetched rather than committed, a clone that skipped that
  step showed a black rectangle with no hint why. That is exactly how it was
  found, and it is the rule the globe has been held to since: **it must render
  correctly with nothing downloaded.**

  It now does both. `tools/fetch_vendor.sh` fetches NASA Blue Marble
  (public domain, 4096×2048 by default) to `assets/earth-surface.jpg`, which is
  gitignored and optional; when that file is absent — or wider than the GPU's
  `MAX_TEXTURE_SIZE` — the sphere's texture is drawn at load time into a canvas
  from `assets/ne_110m_land.json`, the same public-domain Natural Earth outline
  the 2D map uses, so the 3D and 2D coastlines cannot disagree. The panel's
  heading says `Blue Marble 4096` or `drawn coastlines`, with a tooltip naming
  the missing file and the script that fetches it. That readout is the actual
  fix for the Cesium bug: not "never download anything", but *never be silently
  wrong about what you are looking at*. Nothing is fetched at runtime either
  way — a station on an isolated LAN skips the script and the panel is still
  right.

- **A dark palette plus a directional light is a black disc**, and the two
  surfaces need different answers to it. The first version used the console's
  own near-black colours and let lighting do the rest; every one multiplied
  down to indistinguishable black and the globe rendered as a silhouette with a
  track floating on it.

  The **drawn** surface uses its texture as an `emissiveMap` as well as a
  `map`: emissive is a floor that puts the coastlines on screen wherever the
  sun is, and the directional light adds the day side on top. That works
  because it is four flat colours. Doing the same to a **photograph** lifts
  both hemispheres equally and flattens the terminator into a smear, so the
  imagery path is a small shader with a *multiplicative* night floor instead —
  the dark half is dimmed rather than lit, which keeps the day side's full
  contrast and leaves a terminator you can actually read. Its gamma is explicit
  because three.js's output-colourspace conversion is a chunk only its own
  materials include; a raw `ShaderMaterial` that omits it comes out washed out.
  Ambient stays low either way, for the same reason as before.

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

- **"CAMERA DOWN" is usually not the camera.** The tile had one word for every
  way of having no picture, and the reason was logged on the server — which is
  not where the person looking at the wall is standing. It was asked three
  times in one afternoon what was wrong with the camera; the answer each time
  was that go2rtc was not running. `/api/cameras` now returns a `bridge`
  object and the tile prints it under the badge, because a stopped bridge, a
  `GS_GO2RTC_URL` still pointing at the compose service name `video`, a
  timeout and an unplugged Hikvision are four different jobs — start a
  container, edit an env file, look at the network, walk to the mast. The
  commonest by a distance is the second: running the backend outside Docker
  leaves that default pointing at a hostname with no DNS behind it, so the
  failure is a name lookup and has nothing to do with a camera.

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

### EXTEND, the keyboard, and a link that is down

**EXTEND keeps a lease without tearing anything down.** Once armed, ARM becomes
RELEASE, and RELEASE stops the antenna and disengages autopilot — so with no
other button, autopilot could not outlive one `GS_CONTROL_LEASE_S`. EXTEND sits
between RELEASE and TRACK while a lease is held. `POST /api/control/extend`
pushes a *live* lease out to a full one and is refused with `armed` when there
is none, so a click that raced the expiry is told "no lease to extend — press
ARM" instead of quietly becoming an arm nobody consciously took. Like re-arming
it is not journaled, so it neither disengages autopilot nor reads as an
operator taking over; like ARM it does not check the other gates, which is how
an operator waits a SatNOGS job out. Only a press extends — no timer, reconnect
or key repeat calls it, and a lease nobody is watching runs out. The countdown
turns amber with three minutes left and red in the last one. While autopilot is
engaged it also says when autopilot will stand down and, if that is before the
LOS of the pass it is working (or else the next planned one), by how long.

**A typed position survives live frames.** The panel used to be rebuilt from a
template on every `control` frame — ARM, a gate flip, any autopilot command —
and the template said `value="0"`. A typed AZ/EL became 0/0 between typing it
and pressing GO, and focus dropped to the page. It is now built once and
updated in place. Enter in AZ or EL is GO, and only when GO is enabled.

**Clicking the polar plot fills AZ/EL and sends nothing.** GO is still a
separate press. AZ is filled with the representation of the clicked bearing
nearest where the antenna is now, inside the limits in force — `GET
/api/control` reports them as `limits` — because a compass bearing sends a SPID
the long way round: due west from an antenna at 0 is −90, not 270.
The note that says so does not move the plot: the notice line under the
buttons is kept whether or not it has anything to say, so a second click to
refine lands on the same sky as the first.

**The keyboard reaches only the safe direction.** `?` lists every binding and
whether it would do anything right now.

| Key | Does |
|---|---|
| Shift+S | STOP. While satnogs-client is connected — SatNOGS may be the one driving — a second Shift+S within 2 s is needed |
| Shift+E | EXTEND, only while armed |
| Shift+D | DISENGAGE autopilot, only while engaged |
| F | full screen on / off |

No shortcut arms, engages, goes, tracks or parks. Keys match the physical key
(`KeyboardEvent.code`), so they keep working with the Thai layout active; they
are ignored while typing in a text field but not in the AZ/EL number boxes, and
key auto-repeat is never a second press.

Past the shortcuts, a key reaches a control in two ways only, both on purpose:
Enter in AZ or EL is GO, and a control button the operator has Tabbed to
answers Enter or Space — Tab rings it, so the key presses what they can see it
will, once however long it is held. A button clicked with the mouse keeps focus
as well, with no ring to show it, and the browser would make the next stray
Enter another press of it: a RELEASE clicked an hour ago became an ARM, the
same key again a RELEASE. So a button that was clicked — even after Tab
reached it — pressed and dragged off, or handed focus back by the key list
answers no key: the key is swallowed, and the focus goes with it.
`tests/test_control_panel_keys.py` drives the panel through each of those in
Node, on a fake page that does what Chromium does with a click or a key; it is
skipped where Node is not installed.

**A rotator link that is down reads as down, to everything.**
`RotatorService` used to publish a link-down sample and go on saying "up" in
`last`, which is what the interlock, the track loop's guard and the planner
read. A reconnect sets `verified` again as soon as `dump_caps` answers — which
rotctld does from compiled-in capabilities with the SPID controller powered off
— so for the 2.8–4.6 s a failing `get_pos` takes, a command passed both checks.
Now `last` stays down, with `stale_s` counting from the last good read, until a
position has actually been read: `GET /api/rotator` reports `link: "down"`,
every move is refused with "rotator link is down", and the planner treats the
antenna's position as unknown. `tests/test_rotator_link.py` drives that window
through the real poll loop.

### Who has the antenna

Four things can drive the antenna — satnogs-client recording a SatNOGS job,
autopilot working the plan, an operator at the control panel, or nothing — and
from across the room they look the same. The ANTENNA line in the header, between
NEXT AOS and the chips, says which, what it is doing, and how long for:
`SATNOGS recording ISS (ZARYA) until 12:06Z`, `AUTOPILOT positioning: …`,
`OPERATOR manual: goto az=120.0 el=30.0`, `NOBODY idle`. `GET /api/antenna`
returns the same state; the control loop publishes it as the `antenna` frame, on
change only. The rules live in `backend/app/services/antenna.py` and are tried
in order:

1. SatNOGS's status is missing or older than `GS_GATE_MAX_STALE_S`, and we are
   not tracking: **unknown**, never idle. Unknown is not permission, on the wall
   any more than at the gate.
2. A SatNOGS job inside its window: **SatNOGS**, recording until its end — or,
   with the client disconnected, a warning that nothing is recording it.
3. Our own track: whoever started it, until LOS.
4. Autopilot engaged: autopilot, in its own words (waiting, positioning, …).
5. A manual move, **only while the lease it was made under holds**. A goto from
   an hour ago leaves the mode "manual" for ever; it does not make anyone the
   owner.
6. satnogs-client connected: SatNOGS on standby, with its next job.
7. Otherwise nobody.

It is display only. Nothing that decides whether the antenna may move reads it
— a test pins that `control.py` and `planner_service.py` do not import it — and
the interlock still asks SatNOGS for itself on every command.

The same module works out the antenna's **focus**, the satellite it is working:
the track target, then a running SatNOGS job, then the pass autopilot is
positioning for, then a SatNOGS job within ten minutes, then the default
satellite. Two things follow it:

- **The ERR readout.** It used to measure against KNACKSAT-2 whatever the
  antenna was doing, as √(Δaz² + Δel²) on compass azimuth — so at 85° elevation
  a 40° azimuth difference read 40° for a beam about 3.5° off. It now shows the
  great-circle beam error (the planner's own formula) against the focus, names
  that satellite when it is not the one on screen, and gives Δaz and Δel in its
  tooltip. SatNOGS schedules some objects under temporary catalogue numbers no
  public element set carries; for those the error is measured against the job's
  own TLE, which the backend keeps after the job leaves `/api/jobs/`. With no
  elements anywhere, ERR shows "—" and its tooltip says why. ControlService's
  track loop computes its own error and is unaffected.
- **The display, if this screen follows.** Follow mode is per device (the
  `gs.followAntenna` key in localStorage) and on by default: the selection moves
  to the focus, with a "following antenna" chip. Picking a satellite in the
  selector pins the view instead — the operator who chose ISS keeps ISS — and
  the chip offers to resume; following also resumes by itself after ten minutes
  with no pointer or key input (`?followIdleMs=` shortens that for testing). A
  focus the catalogue cannot draw is never selected, so there is no `/api/tle`
  404; the chip says the antenna is on it and what is shown instead. Following
  changes only this browser's selection — the backend ignores
  `select_satellite`, and nothing here can move the antenna.

  But the selection is what TRACK sends. So while an operator holds the lease
  and drives by hand, the selection moves only when someone at the screen
  moves it: the view is held, with an "armed: holding …" chip that offers to
  follow, and a pin does not lift by itself until the lease is given back.
  An adversarial review found the idle resume swapping an armed operator's
  ISS for KNACKSAT-2 — with no chip, since the default needs none — so that
  TRACK tracked KNACKSAT-2. Engaged, autopilot is what the antenna is doing
  (it cannot run without a lease), and an unpinned screen still follows it.

  A follow asks again whether it is still wanted once its elements arrive,
  so one that a pick, a lease or the antenna has overtaken never lands; and a
  focus that moved while it was loading is followed when it finishes. A
  follow that failed is retried after 5 s, 30 s, then every two minutes, at
  once on a reconnect, or when its chip is clicked. If the link to the
  backend is lost, the ANTENNA line says the owner is unknown until the next
  frame — the last one is not news. `tests/test_antenna_follow.py` runs the
  panel in Node and pins each of these.

`pass_next` frames now say which satellite they are about, and each browser
applies only the ones about the satellite it shows. They used to be applied
unconditionally, so an operator who selected ISS saw the next-pass card and the
header's AOS snap back to KNACKSAT-2 within five seconds, under a map still
showing ISS.

At 1000 px and below the ANTENNA line takes a header row of its own, and once
the header has scrolled away a 36 px strip along the top edge repeats it.

## Observation planner

One rotator and one radio means passes compete. The planner decides which ones
to work over the next `GS_PLANNER_HORIZON_H` hours, and explains every decision
— `GET /api/plan` returns each pass with a status and a reason.

| Status | Meaning |
|---|---|
| `planned` | this station will work it |
| `satnogs` | SatNOGS already has it scheduled; nothing for us to do |
| `reserved` | overlaps a SatNOGS job for a different satellite |
| `conflict` | lost to a better overlapping pass — the reason names which one |
| `infeasible` | the antenna cannot slew there in time from the pass before it |
| `low` | peaks below the station's 10° culmination threshold |

**How it chooses.** This is weighted interval scheduling with a twist: the time
the antenna needs between two passes depends on *which* two. Leaving a pass in
the north-east and catching the next rising in the south-west is a long slew;
two passes that set and rise in the same part of the sky need almost none. That
rules out the textbook sort-by-finish-time method, which assumes compatibility
depends on time alone. Instead the plan is a longest path through a DAG — one
node per pass, an edge wherever the antenna can get from the end of one to the
start of the next — which finds the true optimum in O(n²).
`tests/test_planner.py` checks it against an exhaustive search on random
schedules.

The obvious alternative, greedy-by-score, is wrong in a common way: it takes one
excellent pass that blocks two good ones worth more together.

**How it scores.** Each pass gets a 0–1 value for elevation, duration and how
long since this station last heard that satellite, weighted and then
*multiplied* by the satellite's priority. Elevation is scored on link budget,
not degrees: free-space path loss goes as 20·log₁₀(range), and at 420 km an
overhead pass is about 10 dB closer than a 10° one. Priority multiplies rather
than adds so that a middling KNACKSAT-2 pass beats a perfect pass of a
satellite nobody here is responsible for.

**Overhead passes score lower, on purpose.** An az/el mount's required azimuth
rate near zenith goes as 1/cos(el) — about 6°/s at 80° for a 400 km pass, about
60°/s at 89°. A SPID turning at 1.5–3°/s cannot keep up, so through TCA, the
best part of the pass, the beam points well behind the satellite. The planner
simulates a rate-limited rotator chasing each high pass and derates the score
by the beam loss of its worst lag; on this hardware an 89° pass scores below a
70° one. The simulation agrees with an independently computed table to within a
few tenths of a degree, and `tests/test_planner.py` holds it there.

```ini
GS_PLANNER_PRIORITIES=67683:10    # norad:weight, comma separated
GS_PLANNER_INCLUDE_CATALOG=0      # 1 = plan the whole amateur catalogue too
GS_ROTATOR_AZ_RATE_DEG_S=1.5      # the slowest plausible SPID; time a 180°
GS_ROTATOR_EL_RATE_DEG_S=1.5      #   slew on site and raise these to match
GS_PLANNER_SETUP_S=30             # retune and start recording between passes
```

**SatNOGS comes first.** Its scheduled observations are hard reservations,
with the same guard band the interlock uses. A job that cannot be parsed
reserves the whole horizon rather than being ignored, and a satellite SatNOGS
schedules under a temporary catalogue number — which no public element set
carries — is listed under `unplannable` rather than silently dropped. Its window
is still protected.

### Autopilot

Autopilot works the plan: pre-positions the antenna where the next pass rises,
tracks it from AOS, stops at LOS, and moves on. It is built to be timid:

- **It goes through `ControlService`.** All four gates apply to every command it
  issues, exactly as for an operator.
- **It never takes a lease.** It can only be engaged while an operator holds
  one (`POST /api/plan/autopilot {"enabled": true}`), and it disengages itself
  when that lease expires or is released. Re-engaging is a human decision.
- **The operator always wins.** Any command an operator issues — STOP, a goto,
  a park, a track, a release — disengages autopilot and is left to stand.
  Autopilot stops only motion it started, named by track id.
- **A closed gate is not an operator.** If SatNOGS reconnects mid-pass the
  interlock stops the track; autopilot reports it is blocked and resumes when
  the gate reopens, within the same lease.
- **Engaging is a handover.** From the moment it is engaged, the antenna is
  autopilot's to drive — including away from a track the operator had running.
  Engaging twice changes nothing.

**How it knows who did what.** `ControlService` keeps a command journal: every
command it *accepts* increments `command_seq` and records its origin
(`operator` or `autopilot`), and nothing else does. "Has anyone else touched the
antenna since autopilot last did?" is then exactly `command_seq != mine`. The
first version inferred this from the control mode, and an adversarial review
showed why that cannot work: a track ended by a closing gate and one ended by an
operator's STOP both leave the mode `idle`, and an operator who parks after
autopilot pre-positioned leaves it `manual` either way. Releasing is journaled
too, so a release followed by a re-arm is still seen; extending a lease is not,
so it does not disengage anything.

**Where it meets each pass.** A SPID holds every bearing more than once, so the
planner plans over (pass, wrap branch) pairs from where the antenna actually is,
and each planned pass carries the exact bearing it was costed on. Autopilot
drives there. Costing turnarounds from the compass LOS instead under-estimated a
slew by up to 320° after a wrapped pass. Branches are checked against the
**limits actually in force** — the configured station limits narrowed by what
rotctld reports — which on station 5024 are −90…450, not the −180…540 that
`dump_caps` claims.

`tests/test_autopilot.py` runs all of this against the real `ControlService`
and its real track loop, with a recording rotator client. Each test names the
review scenario it pins, and they fail against the previous code. An earlier
version tested against a hand-written fake of `ControlService`, and every bug
the review found passed it.

Like manual control, **autopilot has never commanded the real antenna**. The
same precondition applies: eyes on the mast, and the station out of the SatNOGS
schedule.

### The plan panel

The "Observation plan" panel sits under the rotator, because ENGAGE only means
anything once ARM above it has been pressed. It shows the next planned AOS, a
timeline strip of the plan's horizon in station time, and every pass with the
planner's own reason for working it or not — what it lost to, that SatNOGS has
it, that it peaks too low. Hovering a row lights the pass and the pass that
beat it.

ENGAGE follows `/api/control`: it is greyed out until control is enabled and an
operator holds a lease, and its tooltip says which. DISENGAGE is always there
while autopilot is engaged. A refusal is shown as the backend worded it. On
every reconnect the panel re-reads the plan and autopilot state from the API,
and the executor publishes its state when it starts, so a wall display that
outlives a backend restart shows the new process's "off" rather than the old
one's "engaged".

## Station logbook

Before the first hardware run somebody will ask who armed, from which machine,
what was refused and by which gate, and what autopilot did about it. Nothing
could answer: the journal kept the origin of the last 64 commands in memory,
autopilot overwrote `disengaged_because` each time, a lapsed lease or a restart
that quietly left autopilot off left no trace, and docker rotates the backend's
own log at 10 MB × 3 behind a request line per poll. `LOG` in the header opens
`logbook.html`, which reads `GS_DATA_DIR/events/YYYY-MM-DD.jsonl` — one JSON
record per line, one file per UTC day:

```json
{"id": "1791534731123-42", "ts": "2026-10-09T07:12:11.123+00:00",
 "kind": "control.cmd", "sev": "warn", "text": "operator: stop", "data": {...}}
```

The id is when the line was recorded and `ts` is when the thing happened, which
can be a little earlier; a record is filed under the day of its `ts`. Something
noticed more than an hour after it happened — a recording's end seen only once
SatNOGS answers again after an outage — is dated when it was noticed, with
`happened_at` beside it. That bound is what lets paging find a record dated
23:59 but noticed after midnight without opening every file.

**Most of it is derived, not reported.** The services already publish every
change to the hub, so the log subscribes like a browser does and works out
what changed between two consecutive frames. The one change to the services is
that `ControlService`'s journal now records *what* each command was and when it
was accepted, beside who sent it. Each rule is a pure function, pinned by
`tests/test_events.py` against payloads the real services publish:

| Kind | Read from |
|---|---|
| `control.cmd` | each new journal entry: `operator: goto az=120.0 el=0.0`, `autopilot: track 67683`, `lease: stop (lease expired)` |
| `control.lease` | `armed` and the journal together — armed going false with a journaled release is a release; without one, the lease *expired*. A later expiry while still armed is an extension, not a command |
| `control.gate` | a gate's boolean flipping, e.g. `satnogs_idle closed` |
| `control.track_end` | a new `track_end_reason`: `track of 67683 ended — gate closed: satnogs_idle` |
| `autopilot.*` | engaging, standing down with its reason, and each change of phase. `blocked` is one line until it clears, however often its detail changes |
| `satnogs.*` | the station's client connecting or not, and recordings starting and ending, dated by their own window |
| `plan.change` | one line per rebuild that changes what is planned in the next 6 h, matching passes the way autopilot does, so the 1 s re-key between rebuilds is not news |
| `status.*`, `log.*` | component state changes, and the backend's own warnings; the same warning within 10 min is one line, then a count |
| `audit` | every POST, PUT, PATCH or DELETE under `/api/control/`, `/api/plan/`, `/api/commissioning/` and `/api/alerts/`: status, client, browser family, and the gates of a refusal — `POST /api/control/goto 409 blocked_by satnogs_idle from 192.168.1.20`. Dated by when the request arrived, so it reads before the lease line or journal entry it caused |
| `boot`, `shutdown`, `unclean_restart`, `config_change` | each start records a redacted settings snapshot, and every setting that differs from the last boot's (`rot_limit_max_az 450 → 540`, a warning for anything the interlock reads). No shutdown record before a boot means the last run did not end cleanly |

Five things it is careful about:

- **History, never permission.** Nothing reads the log to decide anything.
  After a restart it *says* that autopilot was engaged and is now off —
  re-engaging is a human decision — and that is all it does. Leases, autopilot
  state and gate freshness are never restored from it.
- **It cannot slow control down.** It reads from its own bounded hub queue,
  which drops the oldest frame of a type when full, so a stall costs log lines,
  never commands. Disk work runs in a worker thread. Each record is flushed;
  `control.*`, `boot` and `shutdown` are also fsynced, because those are what an
  investigation after a power cut wants.
- **No secret reaches a file.** Any setting whose name contains token, password,
  secret, key, webhook, ntfy, telegram, auth or credential is recorded only as
  `<set>` or `<unset>`; every other URL loses its userinfo and query string; and
  the secret values themselves are scrubbed from warnings and audit text. An
  audit line keeps only the body fields `az`, `el`, `norad`, `enabled`,
  `eyes_on_mast` and `hours`.
- **It survives what it records.** A torn last line from a power cut is skipped
  on reading and the next record starts on a fresh line. Text no UTF-8 encoder
  will take — a lone surrogate in a request body, or in a setting whose bytes
  are not UTF-8 — becomes `?` before it is recorded. If the directory cannot
  be written the log keeps its 2,000-record ring in memory and reports
  `events degraded`, rather than taking anything else down with it. A boot reads
  back only as far as the ring and the last run's autopilot state need, so a
  long history does not hold up the start.
- **The client is the first `X-Forwarded-For` hop**, which Caddy sets, with the
  socket peer kept beside it when they differ. Commands sent over the WebSocket
  bypass the HTTP audit; they still appear, through the journal, without a
  client.

```ini
GS_EVENTS_RETAIN_DAYS=180   # day files older than this go first
GS_EVENTS_MAX_MB=200        # then the oldest, until the directory is under this
```

`GET /api/events?since=&until=&kinds=control,autopilot&min_sev=warn&q=&limit=`
returns records newest first with `more` set when there are older matches —
page with `until=<oldest id>`. `GET /api/events/days` lists the files and
`GET /api/events/day/YYYY-MM-DD.jsonl` downloads one as written. The page groups
by the station's own day, filters by chip (`faults` is everything at warn or
worse), keeps the filter in the URL (`logbook.html#faults`), and tails live over
its own WebSocket, re-reading from its newest record every time the socket
opens, so a restart's `boot` and `unclean_restart` lines appear on a page left
open. Times are the station's; a sentence that names a time in UTC — a lease's
expiry, the last record before an unclean restart — gets it in station time on
the line beneath.

## Tests

```bash
cd backend
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest              # offline: 539 tests
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
  differs between the two. Running `tools/dev_server.py` on its own does **not**
  start it, so the tile will say the bridge is unreachable and name the reason:
  `GS_GO2RTC_URL` defaults to `http://video:1984`, which is the compose service
  and does not resolve outside it. For a picture on a laptop, run go2rtc beside
  the dev server and point the backend at it:

  ```bash
  go2rtc -config deploy/go2rtc/go2rtc.mock.yaml
  GS_GO2RTC_URL=http://127.0.0.1:1984 python tools/dev_server.py
  ```
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
`tools/fetch_vendor.sh` (with its licence) rather than committed. The globe's
optional surface imagery is NASA Blue Marble, a work of the U.S. Government and
therefore public domain; it is fetched by the same script and is not committed.
Transmitter data comes from SatNOGS DB at runtime and is cached, not vendored.

The globe's day/night shading, its atmosphere rim, the GPU texture-ceiling and
anisotropy checks and the WMS request that gets the imagery are adapted from
[SattrackSlop](https://github.com/ColaBear101/SattrackSlop) (MIT), the
operator's other tool — the same one this console's light palette follows. Its
runtime fetching is deliberately *not* adapted: see
[the globe bullet](#things-that-are-the-way-they-are-for-a-reason).
