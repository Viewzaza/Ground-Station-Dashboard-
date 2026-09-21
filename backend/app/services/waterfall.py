"""The most recent waterfall for the tracked satellite, cropped to the signal.

SatNOGS publishes each observation's waterfall as a matplotlib render: 832x1603
with axes, tick labels and a power colourbar, most of which is furniture. On a
wall display the useful part is the middle of the band, where the satellite's
carrier sits, and the packet bursts in it.

Three things this does, in order, and why:

**Find the plot inside the PNG rather than hard-coding pixel offsets.** The
image is a matplotlib figure, so its margins move whenever the axis labels
change width — a two-digit power range and a three-digit one do not lay out the
same. The spectrogram is located by looking for the widest contiguous run of
non-white columns; the colourbar is the narrow run to its right and is
discarded by the same pass.

**Separate signal from noise with green-minus-blue, not brightness.** The
waterfall uses viridis, where rising power runs dark blue to teal to green. The
noise floor is blue — bright, but blue — so a luminance threshold keeps the
floor and loses the signal. In G-B the floor goes negative and only the signal
is positive, so clamping at zero is itself the discriminator.

**Crop to the middle of the band.** At 400 MHz a LEO pass Doppler-shifts by
about +/-9 kHz, and the waterfall spans roughly +/-28 kHz, so the centre half
holds the whole curve with margin and drops the empty edges.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import datetime, timezone

import httpx
import numpy as np
from PIL import Image, ImageChops, ImageOps

from ..config import Settings

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 60.0
USER_AGENT = "knacksat2-ground-station-dashboard/0.1 (+github.com/Viewzaza)"

WHITE = 235             # above this on every channel counts as page background
INK_FRACTION = 0.55     # how much of a line must be non-white to be "the plot"
SAMPLE_STEP = 6         # every 6th pixel is plenty to find a 600 px wide box
CENTRE_FRACTION = 0.5   # of the band, kept around the centre frequency
PANEL_W, PANEL_H = 760, 150
# How hard the surviving signal is lifted. Below 1 brightens, and the curve is
# steepest at the dark end where the faint mid-pass returns live. 1.0 is the
# old behaviour. Far below ~0.45 the residual noise that got through the
# stretch starts to read as signal, which is worse than a dim panel.
SIGNAL_GAMMA = 0.62
_GAMMA_LUT = [round(255 * (i / 255) ** SIGNAL_GAMMA) for i in range(256)]
# Retry interval with nothing cached. Shorter than the full TTL so a station
# that has just started shows a waterfall without waiting five minutes, but long
# enough that a rate-limited or unreachable API is not asked once per request.
FAILED_RETRY_S = 60.0


class WaterfallStore:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._meta: dict[int, dict] = {}        # norad -> observation summary
        self._png: dict[int, bytes] = {}        # norad -> processed image
        self._checked_at: dict[int, float] = {}  # norad -> monotonic stamp
        self._lock = asyncio.Lock()

    # --- discovery ---------------------------------------------------------
    async def _find_latest(self, norad: int) -> dict | None:
        """Most recent finished observation that actually produced a waterfall.

        `norad_cat_id` is the filter that works on the Network API;
        `satellite__norad_cat_id` is accepted, ignored, and returns every
        satellite — which looks like success until you read the frequencies.
        """
        params = {
            "norad_cat_id": norad,
            "ground_station": self.s.station_id,
            "end": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "format": "json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT_S, headers={"User-Agent": USER_AGENT}
            ) as client:
                resp = await client.get(
                    f"{self.s.satnogs_network}/observations/", params=params
                )
            if resp.status_code != 200:
                log.warning("waterfall lookup returned %s", resp.status_code)
                return None
            observations = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("waterfall lookup failed: %s", exc)
            return None

        if not isinstance(observations, list):
            return None

        withwf = [o for o in observations if o.get("waterfall")]
        if not withwf:
            return None
        withwf.sort(key=lambda o: o.get("start") or "", reverse=True)
        best = withwf[0]
        return {
            "id": best.get("id"),
            "start": best.get("start"),
            "end": best.get("end"),
            "status": best.get("status"),
            "vetted_status": best.get("vetted_status"),
            "frequency_hz": best.get("transmitter_downlink_low"),
            "mode": best.get("transmitter_mode"),
            # Decoded frames. This is the only unambiguous answer to "did we
            # actually hear it" — a waterfall can look busy with interference
            # and vetting is a human judgement that often never happens, but a
            # demodulated frame is a frame.
            "frames": len(best.get("demoddata") or []),
            "waterfall": best.get("waterfall"),
            "url": f"https://network.satnogs.org/observations/{best.get('id')}/",
        }

    # --- fetch + process ---------------------------------------------------
    async def refresh(self, norad: int, force: bool = False) -> dict | None:
        async with self._lock:
            # Gate on a TTL before touching the network at all. This used to
            # call the Network API on every request — and since the panel polls
            # and each request also asked, SatNOGS started answering 429 Too
            # Many Requests. A waterfall only changes when a pass finishes and
            # the station uploads it, which is minutes at best, so asking more
            # often than that cannot learn anything. Same etiquette as the
            # Celestrak rule: do not ask again before the answer can differ.
            #
            # The gate applies whether or not we already hold an image. Gating
            # only on a cached hit is the trap: the case that hammers the API is
            # the one where every attempt FAILS, because failure leaves nothing
            # cached and so skips the check entirely — and the commonest reason
            # for failing is that the API is already rate-limiting us.
            have = norad in self._png
            age = time.monotonic() - self._checked_at.get(norad, -1e9)
            interval = self.s.waterfall_ttl_s if have else FAILED_RETRY_S
            if not force and age < interval:
                return self._meta.get(norad)

            self._checked_at[norad] = time.monotonic()
            meta = await self._find_latest(norad)
            if meta is None:
                return None

            current = self._meta.get(norad)
            if not force and current and current.get("id") == meta.get("id") \
                    and norad in self._png:
                return current

            try:
                async with httpx.AsyncClient(
                    timeout=REQUEST_TIMEOUT_S, headers={"User-Agent": USER_AGENT},
                    follow_redirects=True,
                ) as client:
                    resp = await client.get(meta["waterfall"])
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("waterfall download failed: %s", exc)
                return None

            # Pillow is CPU-bound and these are 1.3 megapixel images; doing this
            # on the event loop would stall every other poller for a second.
            png = await asyncio.to_thread(render_signal, resp.content)
            if png is None:
                return None

            self._meta[norad] = meta
            self._png[norad] = png
            log.info("waterfall: observation %s for %s", meta["id"], norad)
            return meta

    def meta(self, norad: int) -> dict | None:
        return self._meta.get(norad)

    def png(self, norad: int) -> bytes | None:
        return self._png.get(norad)


# --------------------------------------------------------------------------
# image processing
# --------------------------------------------------------------------------

def find_plot_box(im: Image.Image) -> tuple[int, int, int, int] | None:
    """Bounding box of the spectrogram, excluding axes and colourbar."""
    # This was 84 of the 286 ms a pass costs, and almost none of it was the
    # work: `px[x, y]` on an 832x1603 figure is about 400k bound-method calls
    # and 400k tuple unpackings to answer a question that is one comparison
    # per pixel. Everything below is the same arithmetic as the loops it
    # replaces — same stride, same strictly-greater test against WHITE, same
    # threshold, same three-column merge, same first-wins tie between equally
    # wide runs — done once across the array instead of a pixel at a time.
    #
    # The old implementation is kept in `tests/test_waterfall.py` and both are
    # run against every figure shape the scan can meet, because the only
    # definition of "right" this function has is what it used to say. There is
    # no independent statement of where the plot is, so "close enough" cannot
    # be checked and would not be good enough anyway: two pixels of drift is
    # two pixels of axis inside the crop, on a display nobody looks at closely.
    #
    # Not converting first was a real bug for about an hour: `np.asarray` on
    # an L-mode image gives a 2-D array and the channel reduction below raises
    # on it, and on RGBA it would quietly fold alpha into the background test.
    # The loop this replaces refused both by failing to unpack three values,
    # which is the same outcome for `render_signal` — it converts already —
    # and a dead panel for anyone who calls this directly.
    arr = np.asarray(im if im.mode == "RGB" else im.convert("RGB"))

    # Background is over WHITE on every channel, so ink is that negated: at
    # least one channel that is not. Tested only on the pixels the pass
    # actually samples, which is what the loop did and is worth saying out
    # loud — the first draft built one background mask for the whole figure
    # and then read a sixth of it, and that single reduction over 1.3
    # megapixels cost 20 ms of the 50 the rewrite was meant to save. The
    # sampling is not a detail to apply at the end; it is most of the win.
    #
    # `np.all(... > WHITE, axis=2)` and not `min(axis=2) > WHITE`: they say
    # the same thing about integers, and the min is half as fast again on
    # uint8, which is not somewhere anybody would think to look.
    sampled = ~np.all(arr[::SAMPLE_STEP] > WHITE, axis=2)
    inky = sampled.sum(axis=0) / max(1, sampled.shape[0]) > INK_FRACTION
    xs = np.flatnonzero(inky)
    if xs.size == 0:
        return None

    # A run ends where the next inked column is more than three away. That is
    # the distance between surviving columns and not the width of the gap, so
    # two blank columns still merge and three do not — slack for a gridline
    # drawn across the spectrogram, not for a dead band.
    breaks = np.flatnonzero(np.diff(xs) > 3)
    starts = np.concatenate(([xs[0]], xs[breaks + 1]))
    ends = np.concatenate((xs[breaks], [xs[-1]]))

    # Widest run is the spectrogram; the colourbar is a ~20 px strip beside it.
    # `argmax` takes the first maximum, which is what `max` over the run list
    # did, and it is load-bearing: it is the whole of what puts a colourbar
    # exactly as wide as the plot on the losing side of the tie.
    widest = int(np.argmax(ends - starts))
    x0, x1 = int(starts[widest]), int(ends[widest])

    # Half-open on x1, as the `range(x0, x1, SAMPLE_STEP)` it replaces was, so
    # the run's last column is never sampled by the row pass. For a run one
    # column wide that leaves nothing to sample at all — and the empty slice,
    # divided by the max(1, ...) below, is exactly what makes a figure of empty
    # axes answer None rather than a box with no rows inside it.
    band = ~np.all(arr[:, x0:x1:SAMPLE_STEP] > WHITE, axis=2)
    lit = band.sum(axis=1) / max(1, band.shape[1]) > INK_FRACTION
    rows = np.flatnonzero(lit)
    if rows.size == 0:
        return None
    return (x0, int(rows[0]), x1 + 1, int(rows[-1]) + 1)


def render_signal(png_bytes: bytes) -> bytes | None:
    """Crop to the signal, lift it out of the noise, lay time left-to-right."""
    try:
        im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    except Exception as exc:
        log.warning("waterfall not an image: %s", exc)
        return None

    box = find_plot_box(im)
    if box is None:
        return None
    plot = im.crop(box)

    width = plot.width
    # Never wider than the plot it is cropping. The floor of 8 used to win on a
    # box only a few pixels across, which made `left` negative — and PIL pads a
    # crop that runs off the edge with BLACK rather than refusing it. Black is
    # 128 under the G-B+128 discriminator below, against a real noise floor's
    # ~18, so the padding did not merely survive the percentile stretch, it
    # outranked the sky: the panel drew a bright bar down each edge that read
    # as signal. Measured at row means of 187 at the edges against 74 in the
    # middle. Reachable from any upload whose widest non-white run is 2-7 px.
    keep = min(width, max(8, int(width * CENTRE_FRACTION)))
    left = max(0, (width - keep) // 2)
    plot = plot.crop((left, 0, left + keep, plot.height))

    # Viridis runs dark blue -> teal -> green as power rises, so G-B increases
    # with signal strength. It does NOT go positive: at these levels the signal
    # is teal, which still has more blue than green. Subtracting with a clamp at
    # zero therefore discarded all but 0.006% of the image and rendered an empty
    # panel. The +128 offset keeps the sign, and the percentile stretch below is
    # what actually separates signal from floor — the discriminator has to be
    # relative to this pass's own noise, not an absolute threshold, because the
    # floor moves with the receiver's gain and the sky temperature.
    _, g, b = plot.split()
    # Discarding the bottom 88% is not as aggressive as it sounds: a pass is
    # overwhelmingly empty sky, so the noise floor really is most of the image.
    # Compared against a human-vetted "good" observation, lower values leave the
    # floor bright enough to hide the bursts and higher ones start dimming them.
    score = ImageChops.subtract(g, b, 1, 128)
    signal = ImageOps.autocontrast(score, cutoff=(88, 0.03))

    # Then lift it. The stretch above decides what IS signal; this decides how
    # brightly what survived is drawn, and they are worth keeping separate —
    # widening the stretch to brighten the panel would drag the noise floor up
    # with the bursts and the strip would get lighter without getting more
    # legible.
    #
    # A gamma curve rather than a brightness offset, because the interesting
    # part of a pass is the faint end: the strong bursts near AOS are already
    # at the top of the range and adding a constant only clips them, while
    # gamma < 1 lifts the weak returns mid-pass that were previously a few
    # levels above black. The floor stays where it is, at zero, so the panel is
    # still an instrument window and not a grey rectangle.
    signal = signal.point(_GAMMA_LUT)

    # Tint: near-black ground, cyan signal. Reads on a light or a dark panel.
    # The two upper stops are brighter than the cyan this started with; the
    # black is not, for the same reason the gamma leaves the floor alone.
    coloured = ImageOps.colorize(signal, black="#0b1622", white="#c8fdff",
                                 mid="#34b9dd")

    # Time runs down a SatNOGS waterfall. Rotating puts it left-to-right, which
    # fits a short wide panel far better than a 300x1550 column would.
    coloured = coloured.rotate(90, expand=True).resize(
        (PANEL_W, PANEL_H), Image.LANCZOS
    )

    out = io.BytesIO()
    coloured.save(out, format="PNG", optimize=True)
    return out.getvalue()
