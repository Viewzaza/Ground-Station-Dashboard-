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
    w, h = im.size
    px = im.load()

    def column_ink(x: int) -> float:
        n = hit = 0
        for y in range(0, h, SAMPLE_STEP):
            r, g, b = px[x, y]
            n += 1
            if not (r > WHITE and g > WHITE and b > WHITE):
                hit += 1
        return hit / max(1, n)

    runs: list[tuple[int, int]] = []
    start = prev = None
    for x in range(w):
        if column_ink(x) > INK_FRACTION:
            if start is None:
                start = prev = x
            elif x - prev <= 3:
                prev = x
            else:
                runs.append((start, prev))
                start = prev = x
    if start is not None:
        runs.append((start, prev))
    if not runs:
        return None

    # Widest run is the spectrogram; the colourbar is a ~20 px strip beside it.
    x0, x1 = max(runs, key=lambda r: r[1] - r[0])

    def row_ink(y: int) -> float:
        n = hit = 0
        for x in range(x0, x1, SAMPLE_STEP):
            r, g, b = px[x, y]
            n += 1
            if not (r > WHITE and g > WHITE and b > WHITE):
                hit += 1
        return hit / max(1, n)

    rows = [y for y in range(h) if row_ink(y) > INK_FRACTION]
    if not rows:
        return None
    return (x0, rows[0], x1 + 1, rows[-1] + 1)


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
    keep = max(8, int(width * CENTRE_FRACTION))
    left = (width - keep) // 2
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
