# KNACKSAT-2 Ground Station Dashboard

An operator display for the KNACKSAT-2 ground control station run by **SatNOGS
station 5024  INSTED-Ground Station (UHF)**, grid OK03gt, 60 m ASL.

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

Light paper chrome, dark instrument windows  see
[Light panel, dark instruments](#light-panel-dark-instruments).

## Status

| Area | State |
|---|---|
| Layout, health chips, config-driven frontend | done |
| Light instrument-panel theme, dark instrument windows | done |
| Camera tile (go2rtc, WebRTC with MSE/HLS/MJPEG/snapshot fallback) | done, **verified against the real camera** |
| 2D ground track, footprint, terminator | done |
| 3D orbit globe (three.js, NASA Blue Marble or drawn coastlines, offline) | done |
| Radio panel  transmitters and live Doppler from SatNOGS DB | done |
| "Last signal"  the previous pass's waterfall, cropped and lifted | done |
| Satellite selector, pass prediction, next-pass card | done |
| Rotator read-out (polar plot, predicted arc, cable wrap) | done, **verified against the real rotctld** |
| Live WebSocket (rotator, pointing error, status, reconnect) | done |
| Rotator **control** behind the SatNOGS interlock | done, refusal path verified on site |
| SatNOGS 5024 activity feed | done |
| Grafana telemetry strip | done, cut back to one stat's height |
| Decoded frames  the most recent frames SatNOGS demodulated | done, **no token needed** |
| Space weather  GOES X-ray graph, flare class, NOAA R/S/G scales | done, live against NOAA SWPC |

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
unreachable unless go2rtc is also running  that is a real state the UI is built
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
(go2rtc) and `groundstation`  the separate `sgoudelis/ground-station` suite,
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
WebSocket. There is no JPL ephemeris  propagation is SGP4 plus geodesy, so
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
  reason to prefer 2 vCPU over 1  on one core that burst competes with
  go2rtc.

**Video is passthrough, not transcode.** The camera is H.264 on both channels
and `go2rtc.yaml` applies no `#video=` transform, so WebRTC and MSE are pure
repacketisation of ~8 Mbit/s: a few percent of a core, no ffmpeg.

**The exception is the snapshot fallback, and it is the likeliest way this box
gets unexpectedly busy.** When `<video-stream>` produces no frame for 15 s the
tile polls `/api/cameras/{id}/snapshot.jpg` at 1 Hz, and that path makes go2rtc
decode H.264 and encode JPEG once a second, indefinitely. A display left stuck
in fallback  usually a firewall blocking the WebRTC candidate  costs an order
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
  limiter, not an optimisation**  never reset it as part of a deploy. N
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

- **CPU type `host`**, not `kvm64`  numpy's OpenBLAS dispatches on CPUID, and
  `kvm64` masks AVX silently. Costs nothing; there is no live-migration
  requirement for a single wall display. 2 vCPU, one socket, NUMA off.
- **Turn ballooning off** (`balloon: 0`). The footprint is flat and small, so
  ballooning buys nothing, but the balloon driver reclaims page cache under
  host pressure  and a reclaim stall during a pass is a rotctld read timing
  out, which flips the chip to `ROT down` and triggers the poll backoff.
- **Install qemu-guest-agent.** Without it there is no clean shutdown, so a
  host reboot SIGKILLs the containers and can interrupt the TLE cache write
  mid-`write_text`. The code survives that  and the cost of surviving it is
  one unnecessary Celestrak fetch, which is the thing being avoided.
- **VirtIO SCSI single** with `discard=on`, **VirtIO** network, `onboot=1`.
  Disk speed is irrelevant here: the largest write is a 21 KB JSON every half
  hour.
- **x86-64.** Nothing requires it, but `alexxit/go2rtc:latest` is unpinned and
  the SDR path is far better trodden on amd64. The workload is 2% of a core;
  there is no ARM upside to buy with that risk.
- Run a real NTP client **in the guest**. Doppler, pass times and the
  `GS_GATE_MAX_STALE_S` staleness gate all read the guest clock, and that gate
  fails *closed*  a drifting clock presents as rotator control being refused
  for no visible reason.

### Networking is the part that bites

The station's devices are plain IPs in `config.py`, with no DNS and no
discovery: rotctld `10.90.36.140:4533`, rigctld `:4534`, camera
`10.90.36.130:554`. **Bridge the VM onto the station LAN** so it holds a
`10.90.36.x` address itself. Docker's bridge handles container→LAN egress
fine; it cannot invent a route the guest does not have.

Everything outbound still works behind NAT  but **WebRTC does not**, and the
fix is one line. `go2rtc.yaml` ships `candidates: - stun:8555` with a comment
telling you to replace it, and on this LAN you must:

```yaml
webrtc:
  candidates:
    - 10.90.36.50:8555        # the VM's own LAN address, not stun:
```

`stun:` asks a public server for your external address, which is useless to a
display on the same subnet and **times out entirely on an isolated LAN**  the
same failure class as the Google Fonts `<link>` this project refuses for the
same reason. Open **TCP and UDP 8555** inbound, plus 80/443 for Caddy. If both
8555 paths are blocked the tile silently degrades to MSE and then to the 1 Hz
JPEG transcode above.

Caddy's `tls internal` mints its own CA, so install its root on every display
machine once  `docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt .`
 and **do not delete the `caddy_data` volume**, because recreating it
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
  that is the copy that actually reaches go2rtc  `deploy/secrets/camera_password.txt`
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
                    SatNOGS · Celestrak · NOAA SWPC · Grafana (iframes only)
```

**Skyfield owns pass prediction, not the browser.** The schedule has to keep
running with no browser open, and it is the same schedule the antenna will be
driven from. The browser runs its own SGP4 (satellite.js) purely for the smooth
1 Hz render of where the satellite is now.

**Everything the frontend knows comes from `/api/config`**  station
coordinates, Grafana panel ids, camera stream names, feature flags. Deploying to
a different station is an `.env` edit.

## Light panel, dark instruments

This was a dark console  near-black page, cyan accent. It is now a light
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
iframes are the same case  see below.

**Three data hues, and the same three everywhere**  2D map, 3D globe, polar
plot. `--track` #0086ad is the orbit and the ground track, `--contact` #b4670f
is happening-now (live values, the in-view arc, the satellite itself), and
`--observer` #c42a6e is us. Three is the number that survives: they stay far
apart in hue under deuteranopia, and lightness carries a fourth channel where a
fourth is needed. A wall display is read from across a room, by whoever is in
it.

**The fonts are system stacks, not Google Fonts, on purpose.** The station may
sit on an isolated LAN, and a webfont `<link>` fails silently  the wall display
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
is now in `backend/requirements.txt`. The crop runs in a worker thread  it is
CPU-bound on a megapixel image, and on the event loop it would stall every other
poller  and the PNG is proxied rather than linked, because the source is a
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

It sits on the same line as Grafana because Grafana's leftmost panel  *time
since last beacon*  is asking exactly the question the frame list answers, and
the two disagreeing is worth seeing at a glance rather than one above the other.

There are two sources, and the panel says which one it is showing.

**SatNOGS DB `/telemetry/` carries decoded fields**  named scalars a decoder
produced, `battery_v: 3.92`  and refuses anonymous requests. It is used when
`GS_SATNOGS_DB_TOKEN` is set, because named values beat bytes.

**SatNOGS Network carries the frames themselves, and they are public.** Every
observation publishes a `demoddata` list of URLs, and those objects come back
HTTP 200 with no token from the same bucket the waterfalls do. So on a station
with no token  which is this station  the panel is full rather than empty.
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
right now  Grafana's "time since last beacon" reads 8 hours against the frame
panel's 48 minutes, because they are measuring different things at different
points in the same pipeline. On one line, that is one glance.

**The satellite selector and the 5024 activity feed moved down here too**, out
of the right-hand column. That column was carrying four panels and losing: the
selector had collapsed to its own heading with no list under it. What is left
up there  the next pass and the rotator  is what is watched *during* a pass,
and what came down is what is consulted between them.

## Space weather

`GET /api/spaceweather` is the third panel on the radio row: GOES X-ray flux
over the last six hours on a log axis, the current flare class, and NOAA's
R/S/G storm scales beside Kp and the 10.7 cm solar flux.

It is there to answer a question the rest of the wall cannot. When a pass
produces nothing, Radio, Last signal and Decoded frames all report the same
nothing, and all three report it *afterwards*. This panel can be read before
the pass, and it is the only one that offers an explanation that is not a fault
in our own equipment.

**What it does not cover, which matters here more than most of what it does.**
This is a *solar* activity panel. The dominant ionospheric threat to a 400 MHz
LEO downlink from 13.8&nbsp;N is not on it: equatorial plasma bubbles, the
post-sunset spread-F that forms near the crest of the equatorial ionization
anomaly. Bangkok sits a few degrees off the magnetic dip equator, in one of the
worst regions on Earth for it; the risk window is roughly 19:00-24:00 local,
worst at the equinoxes; and scintillation strengthens as about f^-1.5 going
down in frequency, so 400&nbsp;MHz fares far worse than GNSS L-band, where
saturated S4 and 10-20&nbsp;dB fades are routine over South-East Asia. A pass
that starts cleanly and breaks up mid-way is its signature.

The trap is that this is **quiet-time** behaviour. Geomagnetic storms modulate
it in both directions — prompt penetration fields near sunset can trigger
bubbles, while the disturbance dynamo in a storm's recovery phase can suppress
them almost entirely. So an operator who reads "G0, Kp 1, all quiet" here and
then loses the 20:30 pass has been pointed *away* from the likeliest cause.
SWPC publishes no global scintillation product, so this cannot be fixed by
adding a feed; the dashboard does already know the station coordinates, the
pass times and the season, so a local-time-and-season risk flag is the obvious
next thing to build.

**Where the numbers come from, and why it is not where you might expect.**
[spaceweatherlive.com](https://www.spaceweatherlive.com/en/solar-activity.html)
and [spaceweather.com](https://spaceweather.com/) are the two sites an operator
is likely to already have open, and both are *presentations* of NOAA SWPC's
GOES and Kp feeds — SpaceWeatherLive credits "NOAA SWPC" for its solar
activity, sunspot and geophysical reports. Neither publishes an API. Scraping
a page built to be read by people breaks on their next redesign, and breaks by
quietly reporting the wrong number rather than by failing, so the panel reads
[NOAA SWPC](https://services.swpc.noaa.gov/) directly: the same data, one hop
earlier, public domain, no key, JSON, behind a CDN that expects to be polled.
The panel links out to SpaceWeatherLive, because a human following up on an
M-class flare wants the interpretation those sites add, not another JSON blob.

Five endpoints, each on its own cadence and each caught on its own — the Kp
feed returning a 500 must not also cost the X-ray graph, which is the reason
the panel exists:

| what | feed | polled |
| --- | --- | --- |
| X-ray flux, both channels | `/json/goes/primary/xrays-6-hour.json` | 120 s |
| the current flare event | `/json/goes/primary/xray-flares-latest.json` | 120 s |
| R / S / G scales | `/products/noaa-scales.json` | 300 s |
| planetary K index | `/json/planetary_k_index_1m.json` | 300 s |
| 10.7 cm solar flux | `/products/summary/10cm-flux.json` | 3600 s |

Served from the poller's cache, never proxied per request, for the same reason
the SatNOGS route is: several wall displays open for months must not become
several displays' worth of traffic aimed at a public service.

**Two time bases, labelled as two.** The R/S/G row is SWPC's *24-hour observed
maximum* — their own label for it — so a G3 at 02:00 UTC still reads G3 at
midnight. The Kp beside it is SWPC's 1-minute running estimate, which is
*now*, and which is what their dashboard and SpaceWeatherLive display. Earlier
this panel took the worse of the two and presented the result as current
conditions; that was built on a premise that turned out to be backwards, and
the details are in the list below.

**What it actually means for this station.** Station 5024 works a 400 MHz
downlink from 13.8&nbsp;N, and the honest answer for two of the three NOAA
scales is "not much" — which is in each scale's tooltip, because three ominous
letters with no context get either over-read or learned-and-ignored.

**R** is graded on solar X-ray peak flux alone. Non-deviative D-region
absorption goes as roughly 1/f², so relative to 10&nbsp;MHz the absorption at
400&nbsp;MHz is down by a factor of order a thousand: even an X-class flare
costs this link a fraction of a dB. The UHF hazard a flare can bring is a
metric-wavelength **radio burst**, and R does not measure that — reading R0 as
"no solar radio problem" is reading it wrong. A burst matters because a
tracking yagi is pointed at the sky the Sun is in: at 400&nbsp;MHz a 15&nbsp;dBi
antenna has an effective area of about 1.4&nbsp;m², so a few thousand SFU is
thousands of kelvin of antenna temperature and tens of dB of desense for
minutes — and 410&nbsp;MHz is one of the more burst-prone frequencies in the
RSTN record. What decides it is Sun–satellite angular separation, which this
dashboard already has the geometry to compute and does not yet show.

**S** is mostly a spacecraft problem, and barely even that here: at 51.6°
inclination and 361&nbsp;km neither end of the link sees the polar caps, where
the absorption would be.

**G** is the one that reaches this dashboard, but more slowly and more weakly
than the first version of this section claimed. KNACKSAT-2 is an ISS deployment
(`98067XZ`) at **361&nbsp;km**, with `MEAN_MOTION_DOT` 6.13e-4 rev/day² — about
350&nbsp;m of altitude a day. Working from that, an unmodelled density excess
of fraction *f* puts roughly `f x 3.4 s` of along-track timing error into a
one-day propagation and `f x 13.5 s` into two:

| storm | density excess at 361 km | AOS error after 1 day | after 2 days |
| --- | --- | --- | --- |
| G1 (Kp 5) | ~30% | ~1 s | ~4 s |
| G3–G4 | ~100% | ~3 s | ~14 s |
| G4–G5 (May 2024 class) | ~300% | ~10 s | ~40 s |

A second of AOS shift is invisible against a ten-minute pass and a beamwidth of
tens of degrees. This is worth watching from about **G3**, not G1.

Two things the earlier text got wrong and that are worth not repeating. A high
Kp and a drifting AOS are **not** the same event: the thermosphere responds in
hours, but element-set error grows with the square of propagation time, and a
TLE fitted *during* a storm carries a B\* tuned to a perturbed arc that then
over-predicts decay through the recovery. A red G mostly warns about tomorrow.
And `GS_TLE_TTL_S` is **not** the binding constraint — it is a download cache
TTL, and polling faster cannot make Celestrak publish sooner. What bounds the
propagation span is the *epoch age* of the newest published element set, which
is what the TLE chip in the header shows. Reading that chip alongside this
panel is still the right instinct; the reason given before was not.

Incidentally F10.7, already on the panel, is the index most satellite drag
models actually consume.

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
  Network  jobs, observations, and the waterfall's "latest pass" lookup  wants
  `norad_cat_id`. The waterfall paid for this lesson a second time, and the
  symptom there is worse, because a strip of someone else's signal still looks
  like a signal. Network pagination is also cursor-based, through the
  `Link: rel="next"` *header*; there is no `?page=`.

- **SWPC truncates a flare magnitude; it does not round it.** A GOES
  long-channel flux of 3.0689e-7 is published by SWPC as **B3.0**, not B3.1.
  `flare_class()` truncates to match, to the digit. This looks like pedantry
  and is not: an operator with SpaceWeatherLive open in another tab, reading
  B3.0 there and B3.1 here, has no way to tell which panel to trust and will
  reasonably stop trusting ours.

- **`xrays-6-hour.json` interleaves both energy channels in one flat array**,
  one row per (timestamp, channel) — not two series. Read as a single series it
  sawtooths between the 0.05–0.4 nm and 0.1–0.8 nm fluxes and looks like a
  flare every other minute. Splitting on `energy` is the whole job.

- **Downsample the X-ray series by peak, never by mean.** A flare is a spike a
  few minutes wide; averaged into a two-minute bucket it comes out a decade
  low, and the graph then contradicts the flare class printed beside it.
  `downsample_peak()` takes the maximum, so the drawn curve is an upper
  envelope — which for "did anything happen" is the reading that cannot
  mislead.

- **The X-ray series has holes, and a polyline drawn through one is invented
  data.** A six-hour window with a 34-minute hole in it is an ordinary day. The
  backend publishes the nominal bucket width and the panel breaks its stroke on
  any step wider than three of them, so a gap reads as a gap. Two traps here.
  The holes are mostly **not** telemetry loss: every row in today's is
  `flux: 0.0` with `electron_contaminaton: true`, SWPC publishing a zero
  because the electron-correction algorithm could not produce a number. And the
  bucket width must be derived from the points actually emitted —
  `downsample_peak` passes a short series through un-thinned, so dividing the
  span by 180 regardless once made 45 minutes of ordinary 1-minute data look
  like 45 consecutive dropouts and drew nothing at all.

- **SWPC's R/S/G is a 24-hour observed maximum, and their Kp feeds are not
  equally current.** Their own front page calls that row "24-Hour Observed
  Maximums". It re-timestamps every few minutes, so it never misses a storm in
  progress — and it keeps reporting one for the rest of the day. Meanwhile
  `/products/noaa-planetary-k-index.json` is the official 3-hourly index,
  published at the *end* of each synoptic period: measured at 15:09 UTC one
  day, its newest row was 12:00 while `/json/planetary_k_index_1m.json` had
  15:02. This panel briefly took `max()` of the reported G and a G derived from
  the 3-hourly Kp, on the belief that the scales were a stale daily summary and
  Kp was the live number. Both halves were backwards, and the result latched
  the day's peak and displayed it as the weather now. The scales are now passed
  through and labelled, and Kp comes from the 1-minute estimate — which is also
  what SWPC's own dashboard and SpaceWeatherLive show.

- **GOES-16 and later read about 30-43% high against GOES-15.** SWPC chose not
  to apply the historical scaling to the new XRS, so a physically identical
  flare is labelled ~1.4x larger than it would have been before December 2019,
  and the R-scale thresholds trip earlier. This panel matches SWPC and
  SpaceWeatherLive exactly *because* all three read the same unscaled numbers —
  but a class from here is not comparable with a pre-2020 catalogue.

- **The short channel's floor is a clamp, not a reading.** GOES pins
  0.05-0.4 nm at 1e-9 W/m² and publishes the clamp: 298 of 358 rows in one real
  window were the float32 spelling of exactly 1e-9, while not one long-channel
  row was. Drawn, that is a flat line along the bottom of the graph most of
  every day — a "less than" rendered as an "equals" — so those readings are
  dropped and the short trace appears only when there is something to see.

- **SWPC has two JSON trees with different shapes.** `/json/...` is an array of
  objects; `/products/...` is sometimes that, sometimes an array-of-arrays with
  a header row, and `/products/noaa-scales.json` is an object keyed by day
  offset as a *string* — `"0"` is today observed, `"-1"` yesterday, `"1".."3"`
  forecasts that carry probabilities instead of scales. Several `/products/`
  feeds also omit the UTC offset from their timestamps and mean UTC by it;
  read as local time, Kp shifts by up to a day.

- **`/primary/` is not a spacecraft.** It follows whichever GOES bird SWPC has
  currently designated primary, so the `satellite` field changes without
  notice. It is carried through to the panel as provenance and never pinned.

- **`electron_contaminaton` is SWPC's own spelling.** The `i` is missing in the
  payload. Correcting it while reading gives `None` on every row.

- **An unfiltered observation query answers entirely `future` passes.** Network
  returns scheduled observations alongside flown ones, newest `start` first,
  and a LEO satellite has far more scheduled than flown. So
  `?norad_cat_id=<n>` comes back as 25 rows of things that have not happened,
  every one with an empty `demoddata`  which is indistinguishable from a
  satellite nobody has ever heard. `status=good` is what makes the page dense:
  21 of 25 rows carried frames against 0 of 25 unfiltered.

- **`demoddata` is not in time order.** One observation's list came back
  10:28:06, 10:27:36, 10:27:06, 10:31:06  the newest frame was *fourth*.
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
  panel  see [Decoded frames](#decoded-frames).

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
   and because it was fetched rather than committed, a clone that skipped that
  step showed a black rectangle with no hint why. That is exactly how it was
  found, and it is the rule the globe has been held to since: **it must render
  correctly with nothing downloaded.**

  It now does both. `tools/fetch_vendor.sh` fetches NASA Blue Marble
  (public domain, 4096×2048 by default) to `assets/earth-surface.jpg`, which is
  gitignored and optional; when that file is absent  or wider than the GPU's
  `MAX_TEXTURE_SIZE`  the sphere's texture is drawn at load time into a canvas
  from `assets/ne_110m_land.json`, the same public-domain Natural Earth outline
  the 2D map uses, so the 3D and 2D coastlines cannot disagree. The panel's
  heading says `Blue Marble 4096` or `drawn coastlines`, with a tooltip naming
  the missing file and the script that fetches it. That readout is the actual
  fix for the Cesium bug: not "never download anything", but *never be silently
  wrong about what you are looking at*. Nothing is fetched at runtime either
  way  a station on an isolated LAN skips the script and the panel is still
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
  tracks literally the same line. Rendering is on demand  a
  `requestAnimationFrame` loop spinning a GPU at 60 Hz to move a marker that
  updates at 1 Hz is just heat in a rack that runs for months.

- **`satellite__norad_cat_id` filters the DB API but not the Network API.** The
  two SatNOGS services do not share a convention, and both fail silently in the
  same direction  an ignored filter returns every satellite rather than an
  error. Network wants `norad_cat_id`; DB, which is where transmitters come
  from, wants `satellite__norad_cat_id`.

- **A satellite's transmitters are not interchangeable.** KNACKSAT-2 publishes
  a 145.825 MHz V/V digipeater and a 400.630 MHz UHF telemetry downlink, and
  station 5024 is a UHF station: its three Yagis span 380–490 MHz. Taking "the
  first transmitter" would tune the panel to a band the antenna cannot hear, so
  `primary_downlink()` prefers a live transmitter inside the station's band.
  Both are still shown  the operator is told which one is primary, not denied
  the other.

- **Grafana's "Powered by Grafana" badge can only be covered, not removed.**
  The panels are cross-origin iframes, so no stylesheet or script of ours can
  reach inside them. `.graf-cell::after` masks the top-right corner where the
  badge sits; the panel title is top-left and the value is centred, so nothing
  readable is behind it. If Grafana moves the badge, move the mask.

- **Grafana sends no CORS headers**, so their telemetry cannot be fetched, only
  embedded. Their panels also need `var-DS_INFLUXDB` or they render empty. The
  embeds work because that instance has anonymous access enabled  if the
  KNACKSAT team turns it off, the panels go blank and `GS_GRAFANA_TOKEN` plus a
  backend proxy become necessary.

- **"CAMERA DOWN" is usually not the camera.** The tile had one word for every
  way of having no picture, and the reason was logged on the server  which is
  not where the person looking at the wall is standing. It was asked three
  times in one afternoon what was wrong with the camera; the answer each time
  was that go2rtc was not running. `/api/cameras` now returns a `bridge`
  object and the tile prints it under the badge, because a stopped bridge, a
  `GS_GO2RTC_URL` still pointing at the compose service name `video`, a
  timeout and an unplugged Hikvision are four different jobs  start a
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
  defaults  which are exactly the SPID 901's range, so against the only
  rotator we had it looked perfect. On any other model the clamp in
  `set_position` would have permitted a position the controller refuses. The
  parser now accepts both spellings and a test asserts a 903's range is read
  rather than assumed. Limits guard a physical end stop: never default them
  quietly.

- **One rotctld socket, process-wide.** rotctld spawns a thread per connection
  with no mutex around the shared rotator handle, so concurrent clients can
  interleave writes mid-frame and corrupt an in-progress track. N browser tabs
  must produce exactly one connection. Poll at 1 Hz  a 600-baud ROT2PROG
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
integration must stay disabled  this backend is the single writer.

`POST /api/control/{arm,release,goto,park,track,stop}`, and the same commands
over the WebSocket, all pass through one `ControlService`, so a gate shut to one
is shut to both. A refusal is a 409 naming the gates, which is what the panel
renders  an operator who presses GO and nothing happens can see *which* gate
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
.venv/Scripts/python -m pytest              # offline: 300 tests
.venv/Scripts/python -m pytest -m network   # cross-checks against live SatNOGS
```

Most of `tests/test_control.py` exists to prove the interlock refuses rather
than that it works: each test names the unsafe thing it prevents. That is the
file to read first if you are changing anything that can move the antenna.

To exercise the real rotctld parser without a rotator, run the fake and point
the backend at it  this is the code path that will meet the hardware, which
`GS_MOCK=1` does not touch:

```bash
python tools/fake_rotctld.py --model 903          # rotctld on 4533
python tools/fake_rotctld.py --kind rig          # a radio on 4532, must be refused
python tools/fake_rotctld.py --split-frames      # replies one byte at a time
```

## Development on a machine that cannot reach the station LAN

`GS_MOCK=1` is the only flag. It selects implementations at construction time,
so there are no `if mock:` branches in the business logic.

- **Cameras**  `deploy/go2rtc/go2rtc.mock.yaml` declares the *same stream
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
- **Rotator**  a simulator driven by the real predictor, so it tracks an actual
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
| Rotator on 4532 or 4533? | **4533.** 4532 is closed  there is no rigctld at all. |
| Rotator type | `Rot type: Az-El`  a rotator, not a radio. |
| SPID model 901 or 903? | **901**, `Model name: Rot2Prog`, Mfg `SPID`. |
| Azimuth / elevation range | −180…540 and −20…210, matching the defaults. |
| Serial | 600 baud 8N1, 300 ms post-write delay, 400 ms timeout, 3 retries. |
| Camera H.264 or H.265? | **H.264** on both channels  `profile-level-id=420029`, Baseline 4.1, `packetization-mode=1`. WebRTC carries it with no transcode. |
| One camera or two? | **One.** Hikvision DS-2CD1023G2-LIUF/SL, `INSTED-GS_1`, firmware V5.8.4. Channel 101 is 1920×1080, 102 is 640×360. |

Two of those answers changed the code:

- **`Can Park: N`.** The 901 has no park command. Asking for one returns an
  error and the antenna does not move, so park is an ordinary `set_pos` to the
  configured park coordinates. `Can Move: N` and `Can Reset: N` likewise  only
  `set_pos` and `stop` are actually available.
- **There is one camera, not two.** The tiles were labelled "Camera 1" and
  "Camera 2", which implies a redundancy that does not exist: both are views of
  the same device, so losing it blanks both. They now read `main · 1080p` and
  `sub · 360p`.

One more thing worth knowing before the rotator is switched on again: with the
SPID controller powered down, rotctld still answers `dump_caps` from its
compiled-in capabilities  model, ranges and all  while every `get_pos`
returns `RPRT -5` after 2.8–4.6 s of serial retries. So **capabilities being
readable is not evidence that the rotator is alive.** The dashboard reports
this state as `ROT down` with the polar plot's antenna marker absent, which is
correct, and the near-5 s cost of each failed read is why the poll loop backs
off rather than retrying at 1 Hz.

## Licence

MIT  see `LICENSE`. The `sgoudelis/ground-station` suite referenced in
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
operator's other tool  the same one this console's light palette follows. Its
runtime fetching is deliberately *not* adapted: see
[the globe bullet](#things-that-are-the-way-they-are-for-a-reason).
