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

Six things about these feeds cost time to find:

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

**GOES-16 and later publish X-ray fluxes about 30-43% higher than GOES-15 did**,
for a physically identical flare. SWPC decided not to apply the historical
scaling factor to the new XRS (their note: "XRSB_Presented = 0.70 x
GOES15_XRSB_Observed" is what you must apply yourself for continuity). So the
classes here match what SWPC and SpaceWeatherLive show today, exactly, because
all three read the same unscaled numbers — but they are ~1.4x higher than the
same flare would have been labelled before December 2019. Anyone comparing a
class from this panel against a pre-2020 catalogue, or against a rule of thumb
learned then, is off by that factor.

**The holes in the X-ray series are usually not telemetry loss.** Every row in
today's 34-minute gap carries `flux: 0.0` with `electron_contaminaton: true` —
SWPC publishing a zero because the electron-correction algorithm could not
produce a valid number, which happens in quiet periods with high electron flux.
`split_xray_rows` drops non-positive flux, so it reads as a gap either way, but
"GOES dropped out" sends the next person looking for a spacecraft fault that is
not there.

**`electron_contaminaton` is SWPC's own spelling**, in the payload, with the `i`
missing. Nothing here reads it, because dropping non-positive flux already
removes exactly the rows it flags — in today's window the 31 flagged
long-channel rows are precisely the 31 zeroed ones. But anyone who adds a
contamination filter and spells it correctly will get None on every row and
conclude, wrongly, that nothing is ever flagged.

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

# One point per bucket on the wire. The panel is ~440px wide on the wall and the
# source is 1-minute cadence over 6 hours, so 720 points is roughly two per
# pixel — all of it paid for on every poll, for detail no one can see. 180 is a
# 2-minute bucket, still finer than the panel can draw.
GRAPH_BUCKETS = 180

LONG_CHANNEL = "0.1-0.8nm"    # the one flare class is defined on
SHORT_CHANNEL = "0.05-0.4nm"  # the harder band; its ratio to the long one rises in a real flare

# GOES clamps the short channel at 1e-9 W/m^2 and reports the clamp as a
# reading. In a sample of today's six-hour window, 298 of 358 short-channel
# rows were the float32 spelling of exactly 1e-9 — and not one long-channel row
# was, so this is the short channel's detection floor rather than a quiet Sun.
# Drawn, it is a flat line along the bottom of the graph for most of every day:
# a "less than" printed as an "equals". Dropped, the short trace appears only
# when there is something to see, which during a flare is exactly when its
# ratio to the long channel starts to matter.
SHORT_FLOOR_W_M2 = 1e-9

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
            return f"{letter}{_truncate_tenth(magnitude)}"
    return f"A{_truncate_tenth(flux_w_m2 / 1e-8)}"


def _truncate_tenth(magnitude: float) -> str:
    """One decimal, truncated, with the decimal dropped above 9.9.

    Rounded to nine significant figures before flooring. A magnitude that is an
    exact tenth can land a half-ULP low after the division — 3.1e-4 / 1e-4 is
    3.0999999999999996 — and flooring that gives X3.0 for a flare SWPC calls
    X3.1. Real feed values are arbitrary floats so this is vanishingly rare,
    but it is a tenth of a class in the wrong direction for a function whose
    entire job is agreeing digit for digit.
    """
    magnitude = float(f"{magnitude:.9g}")
    if magnitude >= 10:
        return f"{int(magnitude)}"
    return f"{math.floor(magnitude * 10) / 10:.1f}"


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
    if span <= 0:
        return None
    # `min(buckets, len(rows) - 1)` and not `buckets`, because downsample_peak
    # passes a short series through untouched rather than thinning it. Dividing
    # by 180 regardless describes buckets that were never emitted: 45 rows of
    # 1-minute data over 44 minutes gives 14.7s, the panel's 3-bucket gap test
    # becomes 44s, every real 60s step reads as a dropout, and the graph draws
    # as a row of disconnected single points — nothing at all. That is the
    # degraded feed this whole gap mechanism exists to render honestly, so
    # getting it backwards there is the worst place to get it wrong.
    return round(span / min(buckets, len(rows) - 1), 3)


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
        key = "long" if energy == LONG_CHANNEL else "short"
        if key == "short" and flux <= SHORT_FLOOR_W_M2:
            continue
        row = merged.setdefault(stamp, {"t": stamp, "long": None, "short": None})
        # A repeated (timestamp, channel) keeps the higher reading, for the same
        # reason the buckets do. Both are real measurements, and the one that
        # can mislead is the one that hides a flare.
        if row[key] is None or flux > row[key]:
            row[key] = float(flux)
    return [merged[k] for k in sorted(merged)
            if merged[k]["long"] is not None or merged[k]["short"] is not None]


def latest_kp(payload: object) -> tuple[float | None, str | None]:
    """The most recent planetary K index and the time it is for.

    Reads `estimated_kp` (SWPC's 1-minute running estimate) in preference to
    `Kp` (the official 3-hourly synoptic index), because the panel's question
    is "is there a storm now".

    The 3-hourly feed is published at the END of each synoptic period, so it is
    between zero and three hours behind — measured at 15:09 UTC today, its
    newest row was 12:00, while the 1-minute feed had 15:02. The estimate is
    also what SWPC's own dashboard and SpaceWeatherLive display as the current
    Kp, and a panel that disagrees with the operator's other tab is a panel
    they stop trusting — the same reasoning as the flare-class truncation.

    Both field names are accepted so either feed can be pointed at this.
    """
    if not isinstance(payload, list):
        return None, None
    best_at: datetime | None = None
    best_kp: float | None = None
    for item in payload:
        if not isinstance(item, dict):
            continue
        stamp = parse_ts(item.get("time_tag"))
        raw = item.get("estimated_kp")
        if raw is None:
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
    """NOAA's G scale from Kp. Kp 5=G1 … 9=G5; below 5 is G0, quiet.

    Used to annotate the Kp readout, not to synthesise a G of our own — the
    G cell comes from SWPC and means something different (see `snapshot`).

    The truncation is deliberate at one edge that looks like a bug. NOAA define
    G4 as "Kp = 8, including a 9-", and Kp arrives in thirds, so 8.67 — which
    is the "9-" they mean — must come out G4 and not G5. `int(8.67) - 4 = 4`
    does exactly that. Rounding instead would give G5 and over-report the
    second-worst storm level as the worst. Please do not "fix" it into a round.
    """
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
    """SWPC's OBSERVED MAXIMUM R / S / G, out of the day-offset-keyed object.

    Maximum, not current. SWPC label this display "24-Hour Observed Maximums"
    on their own front page, and the row is re-timestamped every few minutes as
    the window slides — measured at 15:09 UTC today it read 15:07, two minutes
    old. So it never misses a storm in progress, and it keeps showing one for
    up to a day after it ends. Presenting it as current conditions would mean a
    G3 at 02:00 UTC still reading G3 at midnight with Kp sitting at 1.

    Only `"0"` is used. `"-1"` is yesterday and `"1".."3"` are forecasts that
    carry probabilities instead of scales.
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

    def scale(key: str) -> int | None:
        # `(today.get(key) or {}).get(...)` reads defensively and is not: a
        # truthy non-dict — `"R": "0"` — raises AttributeError, which is not in
        # the poller's except clause, so one wrong-shaped field upstream takes
        # the loop down and the other four feeds with it on every restart.
        block = today.get(key)
        return _scale_int(block.get("Scale")) if isinstance(block, dict) else None

    return {
        "R": scale("R"),
        "S": scale("S"),
        "G": scale("G"),
        "observed_at": f"{date}T{clock}Z" if date and clock else None,
    }


def poll_state(failed: list[str], xray_failures: int) -> str:
    """The component health for a tick in which `failed` endpoints errored.

    `xray_failures` is the X-ray feed's OWN consecutive failure count, not the
    number of ticks that had some failure in them. Those differ the moment a
    second feed is broken: a permanently 500ing flare endpoint shares the
    120-second interval, so a shared counter would already be past the
    threshold when the X-ray feed has its first-ever blip, and the very first
    one would go straight to "down".

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
    return "down" if "xray" in failed and xray_failures >= 3 else "degraded"


class SpaceWeatherService:
    """Polls SWPC for the X-ray graph, the flare class, and the storm scales."""

    #: endpoint key -> path under the SWPC base
    PATHS = {
        "xray": "/json/goes/primary/xrays-6-hour.json",
        "flare": "/json/goes/primary/xray-flares-latest.json",
        "scales": "/products/noaa-scales.json",
        "kp": "/json/planetary_k_index_1m.json",
        "f107": "/products/summary/10cm-flux.json",
    }

    def __init__(self, settings: Settings, on_state=None) -> None:
        self.s = settings
        self.on_state = on_state or (lambda component, state, detail="": None)

        self.xray: list[dict] = []          # downsampled, ready for the wire
        self.bucket_s: float | None = None
        self.current_flux: float | None = None
        self.flare: dict | None = None
        self.current_class: str | None = None
        self.scales: dict = {"R": None, "S": None, "G": None, "observed_at": None}
        self.kp: float | None = None
        self.kp_at: str | None = None
        self.f107: float | None = None
        self.satellite: int | None = None

        # Two different ages, because they answer two different questions and
        # only one of them is what the panel means by "how old is this".
        #
        # `_xray_at` is monotonic and says when we last got an answer out of
        # SWPC. `_newest_sample` is the timestamp GOES put on the newest usable
        # reading, and it is the one the panel shows: a successful poll of a
        # file whose last half hour is all electron-contaminated zeros is a
        # fresh poll of stale data, and reporting that as "0s" under a
        # half-hour-old flare class is exactly the lie the monotonic stamp was
        # supposed to prevent. Today's real six-hour window carries 31 such
        # zeroed samples, so this is not hypothetical.
        #
        # Reading age has to come off the wall clock, since the timestamp does;
        # an NTP step distorts it. That is a smaller wrong than a poll age that
        # cannot see staleness at all.
        self._xray_at: float | None = None
        self._newest_sample: datetime | None = None

    @property
    def xray_age_s(self) -> float | None:
        """Seconds since the newest usable X-ray reading was taken."""
        if self._newest_sample is None:
            return None
        return (datetime.now(timezone.utc) - self._newest_sample).total_seconds()

    @property
    def polled_s(self) -> float | None:
        """Seconds since SWPC last answered at all. Distinguishes a stale feed
        from a stopped poller — the reading age alone cannot tell them apart."""
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
            # Raise rather than return. Returning counts the tick as a success,
            # so the chip goes green while the panel keeps republishing the
            # series from an hour ago. ValueError is what the loop already
            # catches, and a feed with no usable row in it is exactly that.
            raise ValueError("no usable X-ray rows in the SWPC payload")
        self.xray = downsample_peak(rows)
        self.bucket_s = bucket_seconds(rows)
        # The class readout comes from the last UNTHINNED row that carries a
        # long-channel value. Reading it off the graph instead would quote the
        # current bucket's peak, which can be minutes old and a tenth of a
        # class high; taking rows[-1] blindly would hand back None whenever the
        # newest row has only a short-channel reading, which is what a payload
        # cut between the two rows of one minute looks like — and SWPC writes
        # the short row first, so that is the cut you get.
        self.current_flux = next(
            (r["long"] for r in reversed(rows) if r["long"] is not None), None
        )
        self._newest_sample = next(
            (r["t"] for r in reversed(rows) if r["long"] is not None), None
        )
        self._xray_at = time.monotonic()
        if isinstance(payload, list) and payload:
            last = payload[-1]
            # Only overwrite with something real. Assigning None on a junk last
            # element throws away the spacecraft we already knew about.
            if isinstance(last, dict) and last.get("satellite") is not None:
                self.satellite = last.get("satellite")

    async def refresh_flare(self, client: httpx.AsyncClient) -> None:
        payload = await self._get(client, "flare")
        if not isinstance(payload, list) or not payload:
            # No flare event on record is a real answer, not a failure.
            self.flare = None
            self.current_class = None
            return
        event = payload[0]
        if not isinstance(event, dict):
            return
        # SWPC's own reading of the class right now. Preferred over ours.
        current = event.get("current_class")
        self.current_class = str(current) if current else None
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
        # SWPC's own word for the current class, when the flare feed has given
        # us one. We already poll that feed at the same cadence, so computing a
        # class we could simply read was reimplementing the thing we most need
        # to agree with. flare_class() stays as the fallback and as what the
        # graph's arithmetic is checked against.
        klass = self.current_class or flare_class(self.current_flux)

        # No arithmetic on the scales, and specifically no max() against a G
        # derived from Kp. That was written on a premise that turns out to be
        # backwards: the daily scales are NOT a once-a-day summary that lags
        # the three-hourly Kp — they re-timestamp every few minutes, while the
        # official Kp is published at the end of each synoptic period and can
        # be three hours behind. Taking the worse of the two therefore latched
        # the day's peak and reported it as the weather now, for up to 24
        # hours, which is the opposite of what it was meant to do.
        #
        # So the two readouts say two different things and are labelled that
        # way: R/S/G is SWPC's 24-hour observed maximum, and Kp is now.
        return {
            "source": "NOAA SWPC",
            "satellite": self.satellite,
            "age_s": None if self.xray_age_s is None else round(self.xray_age_s, 1),
            "polled_s": None if self.polled_s is None else round(self.polled_s, 1),
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
                "G": self.scales.get("G"),
                "window": "24h-max",
                "observed_at": self.scales.get("observed_at"),
            },
            "kp": self.kp,
            "kp_at": self.kp_at,
            # The G level this Kp corresponds to, so the live number is in the
            # same units as the cell above it. Not a substitute for that cell.
            "kp_g": g_scale_for_kp(self.kp),
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
        # Per endpoint, and reset by that endpoint's own success.
        consecutive = {key: 0 for key in self.PATHS}

        async with httpx.AsyncClient(
            timeout=self.s.spaceweather_timeout_s,
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
                        # httpx's `timeout=` is per socket operation, not per
                        # request: a server that dribbles one byte at a time
                        # resets the read timer with each one and holds the
                        # request open indefinitely. These five run in series,
                        # so one such server stops the X-ray refresh, stops
                        # publish(), and leaves every open browser on a frame
                        # whose age has frozen — under a health chip still
                        # showing green. This bounds the whole attempt.
                        async with asyncio.timeout(self.s.spaceweather_timeout_s):
                            await fn(client)
                        consecutive[key] = 0
                        ran = True
                    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
                        consecutive[key] += 1
                        failed.append(key)
                        log.warning("SWPC %s poll failed (%d): %s",
                                    key, consecutive[key], exc)
                        # Back off this one endpoint rather than retrying it on
                        # the next tick while the others are healthy.
                        due[key] = now + intervals[key]
                        continue
                    due[key] = now + intervals[key]

                if ran and not failed:
                    self.on_state("spaceweather", "ok")
                elif failed:
                    self.on_state("spaceweather",
                                  poll_state(failed, consecutive["xray"]),
                                  f"SWPC: {', '.join(failed)}")

                if ran or failed:
                    self.publish()

                await asyncio.sleep(tick)
