"""Transmitter selection, Doppler, and pulling the signal out of a waterfall.

The waterfall tests build their own images rather than fetching one, so they
run offline and so the thing being asserted is visible in the test itself.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.config import Settings
from app.services.transmitters import (
    TransmitterStore, _summarise, doppler_shift_hz,
)
from app.services.waterfall import find_plot_box, render_signal

KNACKSAT2 = 67683

# As published by SatNOGS DB: a VHF transceiver and the UHF telemetry downlink.
VHF = {
    "uuid": "vhf", "description": "Mode V/V - FSK9k6 - Digipeater",
    "type": "Transceiver", "downlink_low": 145825000, "uplink_low": 145825000,
    "mode": "FSK", "baud": 9600.0, "service": "Amateur", "status": "active",
    "alive": True,
}
UHF = {
    "uuid": "uhf", "description": "Mode U - FSK9k6 - AX.25 G3RUH -TLM",
    "type": "Transmitter", "downlink_low": 400630000, "mode": "FSK",
    "baud": 9600.0, "service": "Unknown", "status": "active", "alive": True,
}


def store(**overrides) -> TransmitterStore:
    return TransmitterStore(Settings(station_id=5024, **overrides))


def load(entries) -> TransmitterStore:
    s = store()
    s._by_norad[KNACKSAT2] = [_summarise(e) for e in entries]
    return s


# --------------------------------------------------------------------------
# which transmitter the station is actually listening to
# --------------------------------------------------------------------------

def test_the_uhf_downlink_wins_on_a_uhf_station():
    """Station 5024's antennas are 380-490 MHz. Tuning the panel to the VHF
    digipeater would display a frequency the antenna cannot hear."""
    assert load([VHF, UHF]).downlink_hz(KNACKSAT2) == 400630000


def test_order_from_the_api_does_not_decide():
    assert load([UHF, VHF]).downlink_hz(KNACKSAT2) == 400630000


def test_an_out_of_band_transmitter_is_still_offered_when_it_is_all_there_is():
    """Out of band is a reason to rank it last, not to pretend it is absent."""
    s = load([VHF])
    assert s.downlink_hz(KNACKSAT2) == 145825000
    assert len(s.get(KNACKSAT2)) == 1


def test_a_dead_in_band_transmitter_loses_to_a_live_one():
    dead_uhf = {**UHF, "uuid": "dead", "alive": False, "downlink_low": 400500000}
    live_uhf = {**UHF, "uuid": "live", "alive": True}
    assert load([dead_uhf, live_uhf]).downlink_hz(KNACKSAT2) == 400630000


def test_a_transmitter_with_no_downlink_is_not_a_candidate():
    uplink_only = {**VHF, "uuid": "up", "downlink_low": None, "downlink_high": None}
    assert load([uplink_only, UHF]).downlink_hz(KNACKSAT2) == 400630000


def test_nothing_known_is_none_not_a_guess():
    assert store().downlink_hz(KNACKSAT2) is None
    assert store().primary_downlink(KNACKSAT2) is None


def test_the_summary_drops_the_bulk_of_the_record():
    summary = _summarise({**UHF, "itu_notification": {"urls": ["x"] * 200}})
    assert summary["downlink_hz"] == 400630000
    assert summary["mode"] == "FSK"
    assert "itu_notification" not in summary


# --------------------------------------------------------------------------
# doppler
# --------------------------------------------------------------------------

def test_closing_raises_the_observed_frequency():
    """Negative range rate is closing. Getting this sign backwards puts the
    radio the wrong side of the carrier by twice the shift."""
    assert doppler_shift_hz(400_630_000, -7.0) > 0


def test_receding_lowers_it():
    assert doppler_shift_hz(400_630_000, 7.0) < 0


def test_the_shift_is_the_expected_size_for_a_leo_pass():
    # 400 MHz at 7 km/s is about 9.4 kHz, which is why a +/-14 kHz crop covers
    # a whole pass.
    assert doppler_shift_hz(400_630_000, -7.0) == pytest.approx(9350, rel=0.02)


def test_zero_rate_at_closest_approach_is_zero_shift():
    assert doppler_shift_hz(400_630_000, 0.0) == 0.0


# --------------------------------------------------------------------------
# finding the spectrogram inside the waterfall PNG
# --------------------------------------------------------------------------

def make_waterfall(plot=(74, 7, 678, 1557), bar=(738, 757), size=(832, 1603)):
    """A stand-in for the matplotlib figure: white page, a wide spectrogram,
    and a narrow colourbar to its right."""
    im = Image.new("RGB", size, "white")
    px = im.load()
    x0, y0, x1, y1 = plot
    for y in range(y0, y1):
        for x in range(x0, x1):
            px[x, y] = (40, 60, 120)          # noise floor: blue
    for y in range(y0, y1):
        for x in range(bar[0], bar[1]):
            px[x, y] = (60, 120, 90)          # colourbar
    return im


def test_the_plot_is_found_and_the_colourbar_excluded():
    box = find_plot_box(make_waterfall())
    assert box is not None
    x0, y0, x1, y1 = box
    assert x0 == pytest.approx(74, abs=2)
    assert x1 == pytest.approx(678, abs=2)
    # The colourbar starts at 738; including it would drag x1 past 700.
    assert x1 < 700


def test_the_box_moves_with_the_margins():
    """The margins are matplotlib's and shift when the axis labels change
    width, which is why the box is detected rather than hard-coded."""
    box = find_plot_box(make_waterfall(plot=(110, 20, 640, 1500)))
    assert box is not None
    assert box[0] == pytest.approx(110, abs=2)
    assert box[2] == pytest.approx(640, abs=2)


def test_a_blank_page_yields_no_box():
    assert find_plot_box(Image.new("RGB", (400, 400), "white")) is None


# --------------------------------------------------------------------------
# lifting the signal out of the noise
# --------------------------------------------------------------------------

def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_a_burst_survives_processing_and_the_output_is_panel_shaped():
    im = make_waterfall()
    px = im.load()
    # A packet burst: brighter, and greener, than the floor — teal, but still
    # with more blue than green, exactly as viridis renders it at this level.
    for y in range(700, 760):
        for x in range(360, 395):
            px[x, y] = (60, 110, 150)

    out = render_signal(_png(im))
    assert out is not None
    rendered = Image.open(io.BytesIO(out))
    assert rendered.size == (760, 150)

    # The burst must actually be brighter than the floor, or the panel is a
    # rectangle of noise.
    grey = rendered.convert("L")
    assert max(grey.getdata()) > 180


def test_the_signal_is_not_thrown_away_by_clamping_at_zero():
    """The regression this exists for: G-B stays negative for a real signal, so
    a clamp at zero discarded all but 0.006% of the image and rendered an empty
    panel. The floor must be relative to this pass's own noise."""
    im = make_waterfall()
    px = im.load()
    for y in range(700, 900):
        for x in range(360, 400):
            px[x, y] = (50, 90, 140)          # brighter than floor, still B > G

    out = render_signal(_png(im))
    grey = Image.open(io.BytesIO(out)).convert("L")
    histogram = grey.histogram()
    lit = sum(histogram[128:])
    assert lit > 0, "the burst was lost entirely"


def test_junk_in_is_none_out_not_an_exception():
    assert render_signal(b"this is not a png") is None
