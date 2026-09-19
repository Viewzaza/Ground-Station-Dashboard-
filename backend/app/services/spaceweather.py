"""Space weather, from NOAA SWPC.

Why this is here at all: when a pass produces no frames, the three panels that
could explain it — Radio, Last signal, Decoded frames — all report the same
nothing, and the operator is left guessing between a rotator that missed, a
transmitter that is off, and an ionosphere that was not cooperating. This panel
is the fourth answer, and it is the only one that can be checked *before* the
pass rather than after it.

**Why SWPC, and not the two sites an operator already has open.**
spaceweatherlive.com and spaceweather.com are both presentations of NOAA SWPC's
GOES and Kp feeds — the first credits "NOAA SWPC" for its solar activity,
sunspot and geophysical reports — and neither publishes an API. Scraping a page
built to be read by people is fragile in the worst way: one layout change and
the panel reports a wrong number rather than failing. So the numbers come from
SWPC, which is the upstream both of them cite, is US government work in the
public domain, and is served as JSON behind a CDN that expects to be polled.
The panel *links* to spaceweatherlive.com, because a human following up on an
M-class flare wants the interpretation those sites add, not another JSON blob.

Four things about these feeds cost time to find:

**There are two trees with different shapes.** `/json/...` is a plain array of
objects. `/products/...` is sometimes that and sometimes an array-of-arrays with
a header row, and `/products/noaa-scales.json` is neither — it is an object
keyed by day offset as a *string*, where `"0"` is today observed, `"-1"` is
yesterday, and `"1".."3"` are forecasts. Each parser here is written against the
shape it actually gets.

**`xrays-6-hour.json` interleaves both energy channels in one flat array**, one
row per (timestamp, channel), not two series. Splitting on `energy` is the whole
job; reading it as a single series gives a sawtooth between the 0.05-0.4 nm and
0.1-0.8 nm fluxes that looks like a flare every other minute.

**SWPC truncates the flare magnitude, it does not round it.** A long-channel
flux of 3.0689e-7 is published by SWPC as B3.0, not B3.1. This is worth matching
to the digit: an operator with SpaceWeatherLive open in another tab reading B3.0
there and B3.1 here has no way to tell which panel to trust, and will reasonably
stop trusting this one.

**`electron_contaminaton` is SWPC's own spelling**, in the payload, with the `i`
missing. Nothing here reads it yet — but anyone who adds a contamination filter
and spells it correctly will get None on every row and conclude, wrongly, that
no reading is ever flagged.

Also: `/primary/` follows whichever GOES spacecraft SWPC has currently
designated primary, so the `satellite` number in the payload changes without
notice. It is carried through to the panel as provenance and never pinned.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone

import httpx

from ..config import Settings
from ..hub import hub

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 20.0

# One point per bucket on the wire. The panel is ~440px wide on the wall and the
# source is 1-minute cadence over 6 hours, so 720 points is roughly two per
# pixel — all of it paid for on every poll, for detail no one can see. 180 is a
# 2-minute bucket, still finer than the panel can draw.
GRAPH_BUCKETS = 180

LONG_CHANNEL = "0.1-0.8nm"    # the one flare class is defined on
SHORT_CHANNEL = "0.05-0.4nm"  # the harder band; its ratio to the long one rises in a real flare

# Flare classes are decades of long-channel flux in W/m^2.
_CLASS_BANDS = (("X", 1e-4), ("M", 1e-5), ("C", 1e-6), ("B", 1e-7), ("A", 1e-8))


def flare_class(flux_w_m2: float | None) -> str | None:
    """The GOES flare class for a long-channel flux, as SWPC writes it.

    Truncated rather than rounded, because SWPC truncates: their B3.0 and our
    B3.1 for the same reading is the kind of disagreement that costs a panel
    its credibility for no gain.

    Above X9.9 the convention drops the decimal — a flare is X15, not X15.0 —
    and below A1.0 the value is still quoted in the A band (A0.5), which is what
    both SWPC and SpaceWeatherLive do rather than calling it zero.
    """
    if flux_w_m2 is None or flux_w_m2 <= 0 or not math.isfinite(flux_w_m2):
        return None
    for letter, floor in _CLASS_BANDS:
        if flux_w_m2 >= floor:
            magnitude = flux_w_m2 / floor
            # Truncate to one decimal. Multiplying by 10 first and flooring is
            # the same thing done in a way that does not depend on how the
            # formatter rounds.
            if magnitude >= 10:
                return f"{letter}{int(magnitude)}"
            return f"{letter}{math.floor(magnitude * 10) / 10:.1f}"
    return f"A{math.floor(flux_w_m2 / 1e-8 * 10) / 10:.1f}"


def flare_letter(klass: str | None) -> str | None:
    """Just the band — what the panel colours on. None for an unreadable class."""
    if not klass:
        return None
    return klass[0] if klass[0] in "ABCMX" else None


def parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    # The `/products/` tree omits the offset on some feeds (the Kp series is one)
    # and means UTC by it. A naive datetime compared against an aware one raises,
    # and silently treating it as local time would shift Kp by up to a day.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def bucket_seconds(rows: list[dict], buckets: int = GRAPH_BUCKETS) -> float | None:
    """The nominal width of one downsampled bucket, for the panel's gap test.

    GOES XRS drops out — today's six-hour window has a 67-minute hole in it —
    and a polyline drawn straight through that is not a quiet Sun, it is an
    hour of data this dashboard invented. The panel breaks its stroke on any
    step much wider than this, so a gap reads as a gap. Sending the number
    rather than letting the panel guess it keeps the two from disagreeing when
    GRAPH_BUCKETS changes.
    """
    if len(rows) < 2:
        return None
    span = (rows[-1]["t"] - rows[0]["t"]).total_seconds()
    return round(span / buckets, 3) if span > 0 else None


def downsample_peak(rows: list[dict], buckets: int = GRAPH_BUCKETS) -> list[dict]:
    """Thin a 1-minute series to `buckets` points, keeping each bucket's PEAK.

    The peak and not the mean, and this is the whole reason this function is
    not a slice. A flare is a spike a few minutes wide; averaged into a bucket
    it comes out a decade lower, and the graph would then contradict the flare
    class printed next to it. Taking the maximum makes the drawn curve an upper
    envelope of the real one — which for "did anything happen" is the reading
    that cannot mislead.

    `rows` are `{"t": datetime, "long": float|None, "short": float|None}`,
    already sorted. The two channels peak independently within a bucket, so each
    is maximised on its own and `t` is the bucket's start; a row's long and
    short values may therefore come from different minutes. That is acceptable
    for a trend graph and would not be for the ratio, which is why the ratio is
    read off the flare feed instead of computed here.
    """
    if not rows:
        return []
    if len(rows) <= buckets:
        return [{"t": r["t"].isoformat(), "long": r["long"], "short": r["short"]} for r in rows]

    span = (rows[-1]["t"] - rows[0]["t"]).total_seconds()
    if span <= 0:
        return [{"t": rows[-1]["t"].isoformat(), "long": rows[-1]["long"], "short": rows[-1]["short"]}]

    width = span / buckets
    start = rows[0]["t"]
    out: list[dict] = []
    current = -1
    for row in rows:
        # The final row lands exactly on the span and would index one past the
        # end; clamping is cheaper than a special case at the bottom.
        index = min(buckets - 1, int((row["t"] - start).total_seconds() / width))
        if index != current:
            out.append({
                # Rounded to the second. `width` is a fraction of a second wide,
                # so the raw boundary carries six decimal places of noise into
                # every timestamp on the wire for precision the axis cannot
                # show and the reader would only find distracting.
                "t": (start + timedelta(seconds=round(index * width))).isoformat(),
                "long": row["long"],
                "short": row["short"],
            })
            current = index
            continue
        tail = out[-1]
        for key in ("long", "short"):
            value = row[key]
            if value is not None and (tail[key] is None or value > tail[key]):
                tail[key] = value
    return out


def split_xray_rows(payload: object) -> list[dict]:
    """The interleaved GOES XRS array as one row per timestamp, both channels.

    Rows whose flux is missing, non-positive or not finite are dropped rather
    than carried as zero: this is drawn on a log axis, where a zero is not a low
    value but an undrawable one.
    """
    if not isinstance(payload, list):
        return []
    merged: dict[datetime, dict] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        stamp = parse_ts(item.get("time_tag"))
        if stamp is None:
            continue
        energy = item.get("energy")
        if energy not in (LONG_CHANNEL, SHORT_CHANNEL):
            continue
        flux = item.get("flux")
        if not isinstance(flux, (int, float)) or not math.isfinite(flux) or flux <= 0:
            continue
        row = merged.setdefault(stamp, {"t": stamp, "long": None, "short": None})
        key = "long" if energy == LONG_CHANNEL else "short"
        # A repeated (timestamp, channel) keeps the higher reading, for the same
        # reason the buckets do. Both are real measurements, and the one that
        # can mislead is the one that hides a flare.
        if row[key] is None or flux > row[key]:
            row[key] = float(flux)
    return [merged[k] for k in sorted(merged)]


def latest_kp(payload: object) -> tuple[float | None, str | None]:
    """The most recent planetary K index and its 3-hour window start."""
    if not isinstance(payload, list):
        return None, None
    best_at: datetime | None = None
    best_kp: float | None = None
    for item in payload:
        if not isinstance(item, dict):
            continue
        stamp = parse_ts(item.get("time_tag"))
        raw = item.get("Kp")
        if stamp is None or raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if best_at is None or stamp > best_at:
            best_at, best_kp = stamp, value
    return best_kp, best_at.isoformat() if best_at else None


def g_scale_for_kp(kp: float | None) -> int | None:
    """NOAA's G scale from Kp. Kp 5=G1 … 9=G5; below 5 is G0, quiet."""
    if kp is None:
        return None
    if kp < 5:
        return 0
    return min(5, int(kp) - 4)


def _scale_int(raw: object) -> int | None:
    """A NOAA scale value, which arrives as a string, or as null on forecast rows."""
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def parse_scales(payload: object) -> dict:
    """Today's observed R / S / G, out of the day-offset-keyed object.

    Only `"0"` is used. The forecast rows carry probabilities instead of scales
    and are a different question from "what is happening now", which is the only
    one a wall display has room to answer.
    """
    if not isinstance(payload, dict):
        return {"R": None, "S": None, "G": None, "observed_at": None}
    today = payload.get("0")
    if not isinstance(today, dict):
        return {"R": None, "S": None, "G": None, "observed_at": None}
    # Both halves or neither. Pasting them together unconditionally and
    # stripping the separator turns a row with no date at all into the string
    # "Z", which is not a timestamp and is not None either — so it survives
    # every "do we have one" check and fails at whoever tries to parse it.
    date, clock = today.get("DateStamp"), today.get("TimeStamp")
    return {
        "R": _scale_int((today.get("R") or {}).get("Scale")),
        "S": _scale_int((today.get("S") or {}).get("Scale")),
        "G": _scale_int((today.get("G") or {}).get("Scale")),
        "observed_at": f"{date}T{clock}Z" if date and clock else None,
    }


def poll_state(failed: list[str], consecutive: int) -> str:
    """The component health for a tick in which `failed` endpoints errored.

    Only the X-ray feed can take the chip red. It is the graph and the flare
    class — the panel's whole reason to exist — and everything else is a
    readout beside it that greys itself out. A permanently 500ing Kp endpoint
    marking this component "down" while the graph draws perfectly is the kind
    of red light operators learn to ignore, and then miss.

    Three consecutive ticks before "down", because one blip on a public CDN is
    not an outage either.
    """
    if not failed:
        return "ok"
    return "down" if "xray" in failed and consecutive >= 3 else "degraded"


class SpaceWeatherService:
    """Polls SWPC for the X-ray graph, the flare class, and the storm scales."""

    #: endpoint key -> path under the SWPC base
    PATHS = {
        "xray": "/json/goes/primary/xrays-6-hour.json",
        "flare": "/json/goes/primary/xray-flares-latest.json",
        "scales": "/products/noaa-scales.json",
        "kp": "/products/noaa-planetary-k-index.json",
        "f107": "/products/summary/10cm-flux.json",
    }

    def __init__(self, settings: Settings, on_state=None) -> None:
        self.s = settings
        self.on_state = on_state or (lambda component, state, detail="": None)

        self.xray: list[dict] = []          # downsampled, ready for the wire
        self.bucket_s: float | None = None
        self.current_flux: float | None = None
        self.flare: dict | None = None
        self.scales: dict = {"R": None, "S": None, "G": None, "observed_at": None}
        self.kp: float | None = None
        self.kp_at: str | None = None
        self.f107: float | None = None
        self.satellite: int | None = None

        # Monotonic, like the SatNOGS service and for the same reason: an NTP
        # step must not be able to make a stale reading look fresh. Nothing here
        # gates a rotator move, but a panel that quietly shows yesterday's
        # weather as today's is its own kind of wrong.
        self._xray_at: float | None = None

    @property
    def xray_age_s(self) -> float | None:
        return None if self._xray_at is None else time.monotonic() - self._xray_at

    # --- HTTP --------------------------------------------------------------
    async def _get(self, client: httpx.AsyncClient, key: str) -> object:
        resp = await client.get(f"{self.s.spaceweather_base}{self.PATHS[key]}")
        resp.raise_for_status()
        return resp.json()

    async def refresh_xray(self, client: httpx.AsyncClient) -> None:
        payload = await self._get(client, "xray")
        rows = split_xray_rows(payload)
        if not rows:
            return
        self.xray = downsample_peak(rows)
        self.bucket_s = bucket_seconds(rows)
        # The class readout comes from the last UNTHINNED row. Reading it off
        # the graph instead would quote the current minute's bucket peak, which
        # can be two minutes old and a tenth of a class high.
        self.current_flux = rows[-1]["long"]
        self._xray_at = time.monotonic()
        if isinstance(payload, list) and payload:
            last = payload[-1]
            self.satellite = last.get("satellite") if isinstance(last, dict) else None

    async def refresh_flare(self, client: httpx.AsyncClient) -> None:
        payload = await self._get(client, "flare")
        if not isinstance(payload, list) or not payload:
            # No flare event on record is a real answer, not a failure.
            self.flare = None
            return
        event = payload[0]
        if not isinstance(event, dict):
            return
        self.flare = {
            "begin": event.get("begin_time"),
            "max": event.get("max_time"),
            "end": event.get("end_time"),
            # SWPC's own class strings, not ours. This feed is the authority on
            # what a flare event peaked at — our flare_class() is for the live
            # flux, where there is no feed to ask.
            "max_class": event.get("max_class"),
            "begin_class": event.get("begin_class"),
            # `end_time` is null while the flare is still in progress, which is
            # the one case a wall display most wants marked.
            "in_progress": event.get("end_time") is None,
        }

    async def refresh_scales(self, client: httpx.AsyncClient) -> None:
        self.scales = parse_scales(await self._get(client, "scales"))

    async def refresh_kp(self, client: httpx.AsyncClient) -> None:
        payload = await self._get(client, "kp")
        # Only the latest. A 24-hour Kp strip would be worth drawing and there
        # is nowhere in a 470x150 cell to draw it, so it is not fetched into
        # the snapshot to sit there unread on every frame.
        self.kp, self.kp_at = latest_kp(payload)

    async def refresh_f107(self, client: httpx.AsyncClient) -> None:
        payload = await self._get(client, "f107")
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            try:
                self.f107 = float(payload[0]["flux"])
            except (KeyError, TypeError, ValueError):
                pass

    # --- published state ---------------------------------------------------
    def snapshot(self) -> dict:
        klass = flare_class(self.current_flux)
        # SWPC only publishes G once a day with the daily scales, and it is a
        # summary of the day rather than of this hour. Kp is three-hourly, so
        # during a storm that started this afternoon the derived value is the
        # current one and theirs is not. Take the worse of the two: this is a
        # "should I expect trouble" readout, and under-reporting a storm in
        # progress is the failure that matters.
        derived_g = g_scale_for_kp(self.kp)
        reported_g = self.scales.get("G")
        g = max([v for v in (derived_g, reported_g) if v is not None], default=None)
        return {
            "source": "NOAA SWPC",
            "satellite": self.satellite,
            "age_s": None if self.xray_age_s is None else round(self.xray_age_s, 1),
            "xray": {
                "series": self.xray,
                "current_flux": self.current_flux,
                "class": klass,
                "letter": flare_letter(klass),
                "bucket_s": self.bucket_s,
                "long_channel": LONG_CHANNEL,
                "short_channel": SHORT_CHANNEL,
            },
            "flare": self.flare,
            "scales": {
                "R": self.scales.get("R"),
                "S": self.scales.get("S"),
                "G": g,
                "g_from_kp": derived_g is not None and derived_g == g and derived_g != reported_g,
                "observed_at": self.scales.get("observed_at"),
            },
            "kp": self.kp,
            "kp_at": self.kp_at,
            "f107": self.f107,
        }

    def publish(self) -> None:
        hub.publish("spaceweather", self.snapshot())

    # --- lifecycle ---------------------------------------------------------
    async def run(self) -> None:
        if self.s.offline or not self.s.spaceweather_enabled:
            detail = "offline mode" if self.s.offline else "disabled"
            self.on_state("spaceweather", "degraded", detail)
            return

        due = {key: 0.0 for key in self.PATHS}
        intervals = {
            "xray": float(self.s.spaceweather_xray_poll_s),
            "flare": float(self.s.spaceweather_xray_poll_s),
            "scales": float(self.s.spaceweather_scales_poll_s),
            "kp": float(self.s.spaceweather_scales_poll_s),
            # F10.7 is a once-a-day observation from Penticton. Asking every
            # minute cannot learn anything it did not already know.
            "f107": float(self.s.spaceweather_f107_poll_s),
        }
        work = {
            "xray": self.refresh_xray,
            "flare": self.refresh_flare,
            "scales": self.refresh_scales,
            "kp": self.refresh_kp,
            "f107": self.refresh_f107,
        }
        tick = min(intervals.values())
        failures = 0

        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_S,
            headers={"User-Agent": f"knacksat2-groundstation/{self.s.station_id}"},
        ) as client:
            while True:
                now = time.monotonic()
                ran = False
                failed: list[str] = []
                # Each endpoint is caught on its own, unlike the SatNOGS poller
                # where the three calls answer one question. Here they do not:
                # the Kp feed 500ing must not also cost us the X-ray graph,
                # which is the panel's whole reason to exist.
                for key, fn in work.items():
                    if now < due[key]:
                        continue
                    try:
                        await fn(client)
                        ran = True
                    except (httpx.HTTPError, ValueError) as exc:
                        failed.append(key)
                        log.warning("SWPC %s poll failed: %s", key, exc)
                        # Back off this one endpoint rather than retrying it on
                        # the next tick while the others are healthy.
                        due[key] = now + intervals[key]
                        continue
                    due[key] = now + intervals[key]

                if ran and not failed:
                    failures = 0
                    self.on_state("spaceweather", "ok")
                elif failed:
                    failures += 1
                    self.on_state("spaceweather", poll_state(failed, failures),
                                  f"SWPC: {', '.join(failed)}")

                if ran or failed:
                    self.publish()

                await asyncio.sleep(tick)
