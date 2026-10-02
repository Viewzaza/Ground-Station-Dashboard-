"""The waterfall strip, built out of drawn figures rather than downloaded ones.

Every image here is drawn in memory. The real inputs are 1.6 MB objects in
somebody else's bucket, and a suite that fetched a few of them on every run
would be both slow and rude — and worse, it would pin the module to whatever
the sky did on the day the fixture was captured. So each test draws a
matplotlib-shaped page instead: white, a spectrogram rectangle, a narrow
colourbar beside it, sparse tick labels in the margins.

Each test is named for the wrong rendering it exists to prevent, and none of
these look like a failure from across the room:

* **A dead panel.** This runs on a wall display with no keyboard. An exception
  anywhere in here is a black rectangle that stays black until somebody walks
  past and wonders, so the module answers None for everything it cannot use —
  and these tests push non-images, truncated PNGs and blank figures through it.
* **Time running backwards.** The source has time running *down* and the
  module rotates to lay it across. A rotation the other way is still a
  plausible-looking strip, and nobody reads a spectrogram closely enough to
  catch it.
* **The colourbar cropped instead of the spectrogram.** Both are tall blocks of
  viridis a few tens of pixels apart. Crop the wrong one and the panel is a
  smooth ramp: all signal, every pass, forever.
* **The noise floor drawn as signal.** Viridis's floor is bright blue, so
  anything reaching for luminance keeps the floor and loses the bursts.
* **A crop that moved.** The margins of a matplotlib figure are a function of
  how wide its tick labels render, so the same station's waterfalls do not
  agree on where the plot starts. That is the whole reason `find_plot_box`
  exists instead of four constants, and two of the figures below differ only
  in their margins.

The colours below are not viridis, only shaped like it where it matters: the
floor is *brighter* than the signal and has less green than blue. That is the
whole reason the module subtracts channels instead of thresholding brightness,
and a fixture whose signal was simply the bright part would not be testing it.
"""

from __future__ import annotations

import io
import logging
import random

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services import waterfall as wf
from app.services.waterfall import find_plot_box, render_signal

PAGE = (255, 255, 255)

# Shaped like viridis where this module cares and nowhere else. G-B is what
# separates them: the floor scores 18 and the signal 178, while in luminance
# the floor is the BRIGHTER of the two (93.6 against 77.4). Every assertion
# about the discriminator leans on that inversion, so it is asserted outright
# in `test_the_signal_is_lifted_out_of_a_floor_that_outshines_it` rather than
# left as a claim in a comment.
FLOOR = (60, 90, 200)
SIGNAL = (20, 110, 60)
INK = (34, 34, 34)               # tick labels and spines

# A page half the size of a real 832x1603 waterfall. The pure-Python plot
# finder is ~72 ms at full size and ~16 ms at this one, which is the
# difference between a suite that is pleasant to run and one that is not;
# the full size appears where it earns its keep, in the equivalence sweep.
SIZE = (416, 802)
PLOT = (48, 24, 360, 752)        # left, top, right, bottom — right/bottom open
COLOURBAR = (378, 24, 392, 752)


# --------------------------------------------------------------------------
# drawing a page that has the shape of a waterfall
# --------------------------------------------------------------------------

def _rect(draw, box, fill) -> None:
    """`ImageDraw.rectangle` includes both corners; every box here is open."""
    left, top, right, bottom = box
    draw.rectangle([left, top, right - 1, bottom - 1], fill=fill)


def _colourbar(draw, box) -> None:
    """A viridis ramp, dark purple to yellow.

    The yellow end is (253, 231, 37), which is over `WHITE` on two channels
    and not the third — so it exercises the fact that the background test is
    an AND across all three and not a brightness threshold.
    """
    left, top, right, bottom = box
    span = max(1, bottom - top - 1)
    for y in range(top, bottom):
        t = (y - top) / span
        draw.rectangle(
            [left, y, right - 1, y],
            fill=(int(68 + 185 * t), int(1 + 230 * t), int(84 - 47 * t)),
        )


def _furniture(draw, plot, step) -> None:
    """Tick labels: real ink in the margins, never enough to be the plot.

    Sparse at the default spacing — about 6% ink in the y label columns
    against the 55% threshold, and about 23% in the rows carrying the x
    labels. `step` tightens them, because "sparse" is doing a lot of work in
    that sentence: a label column at 44% is still not the plot, and a
    threshold set low enough to be safe against noise would take it.
    """
    left, top, right, bottom = plot
    for y in range(top, bottom, step):
        _rect(draw, (left - 30, y - 3, left - 8, y + 4), INK)
    for x in range(left, right, 90):
        _rect(draw, (x - 10, bottom + 6, x + 11, bottom + 13), INK)


def figure(*, size=SIZE, plot=PLOT, colourbar=COLOURBAR, floor=FLOOR,
           paint=(), furniture=True, page=PAGE, label_step=120) -> Image.Image:
    """A matplotlib-shaped waterfall page.

    `paint` is a list of (box, colour) in *plot* coordinates, so a test can
    say "the first tenth of the time axis" without knowing where on the page
    the plot landed — which is the same ignorance the module is built around.
    A `floor` of None draws the axes outline and leaves the plot area empty,
    which is what a figure with nothing in it looks like.
    """
    im = Image.new("RGB", size, page)
    draw = ImageDraw.Draw(im)
    if plot is not None:
        if floor is None:
            draw.rectangle([plot[0], plot[1], plot[2] - 1, plot[3] - 1],
                           outline=INK)
        else:
            _rect(draw, plot, floor)
    if colourbar is not None:
        _colourbar(draw, colourbar)
    for box, colour in paint:
        left, top, right, bottom = box
        _rect(draw, (plot[0] + left, plot[1] + top,
                     plot[0] + right, plot[1] + bottom), colour)
    if furniture and plot is not None:
        _furniture(draw, plot, label_step)
    return im


def comb(span: tuple[int, int], period: int, phase: int,
         size=(120, 300)) -> Image.Image:
    """A block of ink on every `period`-th row, starting at `phase`.

    What horizontal banding looks like to a scan that samples rows rather
    than reading them all.
    """
    im = Image.new("RGB", size, PAGE)
    draw = ImageDraw.Draw(im)
    for y in range(phase, size[1], period):
        draw.rectangle([span[0], y, span[1] - 1, y], fill=FLOOR)
    return im


def part_filled(sampled_rows: int, size=(120, 120)) -> Image.Image:
    """A page inked over exactly `sampled_rows` of the rows the scan reads.

    120 tall is 20 sampled rows, so the ink fraction moves a twentieth at a
    time and can be landed exactly on 0.55 — which is the only way to see the
    difference between `>` and `>=` at all.
    """
    im = Image.new("RGB", size, PAGE)
    ImageDraw.Draw(im).rectangle(
        [20, 0, 79, (sampled_rows - 1) * wf.SAMPLE_STEP], fill=FLOOR)
    return im


def part_lit(sampled_cols: int, size=(160, 120)) -> Image.Image:
    """A solid plot with a partly-inked tail below it.

    The same trick as `part_filled` turned ninety degrees, for the row pass:
    the plot is 120 columns wide so the row scan samples exactly 20 of them,
    and the tail inks exactly `sampled_cols` of those. The rows of the tail
    therefore sit on a twentieth-grained fraction and can land on 0.55 dead.
    """
    im = Image.new("RGB", size, PAGE)
    draw = ImageDraw.Draw(im)
    draw.rectangle([20, 0, 139, 100], fill=FLOOR)
    for i in range(sampled_cols):
        x = 20 + i * wf.SAMPLE_STEP
        draw.rectangle([x, 101, x, size[1] - 1], fill=FLOOR)
    return im


def stripes(w: int, h: int, *spans: tuple[int, int]) -> Image.Image:
    """Bare vertical bars on a white page: a figure stripped back to its runs.

    For the cases where the furniture is beside the point and the only
    question is which columns the scan groups together.
    """
    im = Image.new("RGB", (w, h), PAGE)
    draw = ImageDraw.Draw(im)
    for left, right in spans:
        draw.rectangle([left, 2, right - 1, h - 3], fill=FLOOR)
    return im


def png(im: Image.Image) -> bytes:
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


def panel(im: Image.Image) -> Image.Image:
    """Render a figure and reopen the strip that came back."""
    raw = render_signal(png(im))
    assert raw is not None, "the figure rendered nothing at all"
    return Image.open(io.BytesIO(raw))


def lum(im: Image.Image) -> np.ndarray:
    """The panel as luminance, which is how it is actually read on a wall."""
    return np.asarray(im.convert("L"), dtype=float)


def gradient(rows: int, cols: int, lo: int, hi: int) -> Image.Image:
    """A burst whose strength ramps down the time axis.

    A two-valued fixture cannot test a gamma curve: after the percentile
    stretch every pixel is 0 or 255 and those are the two points the curve
    does not move. The ramp is what gives it midtones to lift.
    """
    burst = Image.new("RGB", (cols, rows))
    draw = ImageDraw.Draw(burst)
    for y in range(rows):
        g = lo + round((hi - lo) * y / max(1, rows - 1))
        draw.rectangle([0, y, cols - 1, y], fill=(20, g, 200))
    return burst


# --------------------------------------------------------------------------
# finding the plot, which is the thing that cannot be a constant
# --------------------------------------------------------------------------

def test_the_spectrogram_is_found_and_the_colourbar_beside_it_is_not():
    """Both are tall blocks of viridis about twenty pixels apart, and the
    narrow one is a power scale — a smooth ramp with no signal in it at all.
    Cropping it gives a panel that looks alive on every pass ever flown."""
    box = find_plot_box(figure())

    assert box == PLOT
    assert box[2] <= COLOURBAR[0], "the crop reaches into the colourbar"


def test_the_plot_is_found_wherever_the_axis_labels_pushed_it():
    """The reason this is a search and not four constants. A matplotlib figure
    lays its margins out from the rendered width of the tick labels, so a
    two-digit power range and a three-digit one do not put the plot in the
    same place — and the offsets that fit yesterday's waterfall silently slice
    a strip of axis into today's."""
    narrow = (48, 24, 360, 752)
    wide = (96, 24, 360, 752)           # a wider label column moves the left edge

    assert find_plot_box(figure(plot=narrow)) == narrow
    assert find_plot_box(figure(plot=wide)) == wide
    assert find_plot_box(figure(plot=narrow)) != find_plot_box(figure(plot=wide))


def test_the_box_is_the_plot_and_not_the_margins_it_was_found_in():
    """Off by a few pixels either way is axis furniture inside the crop, which
    the G-B pass then reads as whatever colour the tick labels happen to be."""
    plot = (60, 33, 300, 700)
    box = find_plot_box(figure(plot=plot, colourbar=(320, 33, 336, 700)))

    assert box == plot


def test_tick_labels_in_the_margin_are_not_mistaken_for_the_plot():
    """A column of y labels is ink on a white page for the whole height of the
    plot, which is exactly what the scan is looking for — it is only the
    *fraction* that tells them apart. Without the threshold the crop would
    start at the axis and every panel would carry a strip of numbers."""
    box = find_plot_box(figure())

    assert box[0] == PLOT[0], "the crop swallowed the y tick labels"
    assert box[3] == PLOT[3], "the crop swallowed the x tick labels"


def test_a_colourbar_as_wide_as_the_plot_does_not_take_it():
    """Ties are decided by which run came first, which is the left one, which
    is the spectrogram — matplotlib puts the colourbar on the right. Nothing
    enforces that but `max` returning its first maximum, so it is pinned:
    changing the scan to prefer the later run would be invisible until a
    figure came out square."""
    plot = (48, 24, 200, 752)
    bar = (240, 24, 392, 752)           # same width, to the pixel
    assert plot[2] - plot[0] == bar[2] - bar[0]

    assert find_plot_box(figure(plot=plot, colourbar=bar)) == plot


def test_a_figure_with_no_plot_in_it_is_no_box_rather_than_an_exception():
    """A pass that produced no waterfall, or an upload that arrived blank. The
    module runs on a wall display: an exception here is a dead panel, and a
    dead panel outlives whoever could have noticed it."""
    assert find_plot_box(figure(plot=None, colourbar=None,
                                furniture=False)) is None


def test_empty_axes_are_not_read_as_a_plot_that_is_one_pixel_wide():
    """A figure with axes and nothing drawn in them is two 1-px spines a
    plot's width apart, and the widest of those is one column. The row scan then
    samples the half-open range between the run's ends, which for one column
    is nothing at all — so this answers None. It is the narrowest path through
    the function and the one most likely to divide by zero."""
    assert find_plot_box(figure(floor=None, colourbar=None)) is None


def test_a_dense_column_of_tick_labels_is_still_not_the_plot():
    """How much slack the 55% threshold actually has, which is less than the
    ordinary figure suggests. A tightly labelled axis puts nearly half its
    column under ink, and anything that lowered the threshold far enough to
    feel safe against a speckled upload would take that column and start every
    crop thirty pixels to the left, inside the numbers."""
    page = figure(label_step=13)
    sampled = ~np.all(np.asarray(page)[0::wf.SAMPLE_STEP] > wf.WHITE, axis=2)
    label_column = sampled[:, PLOT[0] - 20].mean()

    assert 0.40 < label_column < wf.INK_FRACTION, \
        "the fixture no longer puts the label column near the threshold"
    assert find_plot_box(page) == PLOT


def test_a_plot_is_found_just_over_the_ink_threshold_and_not_just_under():
    """The other side of the same constant, and the side that costs a panel
    rather than a few pixels of it. 55% of a *page* is a low bar for a
    spectrogram and a high one for anything else, but a waterfall whose plot
    is a little shorter than usual — a pass that was cut off, a figure with
    room made for a longer title — sits closer to it than it looks. Raise the
    threshold and those render as nothing at all.

    Two figures 15 rows apart, at 56% and 54% of the sampled height. No
    furniture on either: the point here is the plot's own ink and not what the
    tick labels underneath it contribute.
    """
    over = figure(plot=(48, 180, 360, 630), colourbar=(378, 180, 392, 630),
                  furniture=False)
    under = figure(plot=(48, 180, 360, 615), colourbar=(378, 180, 392, 615),
                   furniture=False)
    fraction = (~np.all(np.asarray(over)[0::wf.SAMPLE_STEP] > wf.WHITE,
                        axis=2))[:, 200].mean()

    assert wf.INK_FRACTION < fraction < wf.INK_FRACTION + 0.02, \
        "the fixture no longer straddles the threshold"
    assert find_plot_box(over) == (48, 180, 360, 630)
    assert find_plot_box(under) is None


def test_a_column_exactly_at_the_threshold_belongs_to_the_page_not_the_plot():
    """`> INK_FRACTION` and not `>=`, which is a character nobody would defend
    in review and which nothing else in this file can see: every other figure
    here sits either side of the threshold by a comfortable margin, so a scan
    that let ties through would pass all of them.

    0.55 is 11/20, so a page 120 rows tall — 20 rows sampled — can land on it
    exactly. Whether that matters to a waterfall is not the point; whether the
    fast implementation and the one it replaced agree about it is.
    """
    assert wf.INK_FRACTION == 0.55 == 11 / 20

    assert find_plot_box(part_filled(11)) is None, "a tie went to the plot"
    assert find_plot_box(part_filled(12)) == (20, 0, 80, 67)

    # And again for the row pass, which is a separate comparison and so a
    # separate chance to write it the other way. A tail lighting exactly 11 of
    # the 20 sampled columns is not part of the plot; twelve is.
    assert find_plot_box(part_lit(11)) == (20, 0, 140, 101)
    assert find_plot_box(part_lit(12)) == (20, 0, 140, 120)


def test_the_row_sampling_aliases_against_a_pattern_at_its_own_period():
    """`SAMPLE_STEP` reads every sixth row, which is plenty for a 600 px box
    and blind by construction to anything banded at six rows.

    Both halves are the same fixture one row apart, and they answer opposite
    things: a comb in phase with the sampling reads as a solid plot, and the
    same comb offset by a row is not there at all. That is what sampling
    costs, it is worth it at 400k reads, and it is written down here so that
    changing the step is a decision somebody makes rather than one they
    discover from a panel that went blank.
    """
    assert wf.SAMPLE_STEP == 6

    assert find_plot_box(comb((40, 90), wf.SAMPLE_STEP, 0)) == (40, 0, 90, 295)
    for phase in range(1, wf.SAMPLE_STEP):
        assert find_plot_box(comb((40, 90), wf.SAMPLE_STEP, phase)) is None, phase


def test_a_page_that_is_exactly_at_the_background_threshold_is_all_ink():
    """`WHITE` is 235 and the test is strictly greater, so 235 is ink and 236
    is page. Worth pinning because a page drawn at the threshold is not a
    contrived input: it is what a JPEG-compressed or slightly-off-white
    upload looks like, and the whole figure then reads as one enormous plot."""
    assert find_plot_box(figure(page=(235, 235, 235), plot=None,
                                colourbar=None, furniture=False)) == (0, 0, 416, 802)
    assert find_plot_box(figure(page=(236, 236, 236), plot=None,
                                colourbar=None, furniture=False)) is None


def test_a_blank_band_inside_the_plot_splits_it_rather_than_bridging_it():
    """Runs are merged across gaps of up to three columns, which is slack for
    a gridline, not for a dead band. A 40-column white notch is two plots, and
    the wider half wins — the alternative is a crop that spans the notch and
    hands the G-B pass a block of page white to call signal."""
    notch = figure(paint=[((100, 0, 140, 728), PAGE)])
    box = find_plot_box(notch)

    assert box == (PLOT[0] + 140, PLOT[1], PLOT[2], PLOT[3]), \
        "the wider half of the split plot is the one on the right"


def test_a_gap_of_two_columns_is_bridged_and_one_of_three_is_not():
    """Where the slack runs out, exactly. The rule is written as the distance
    between the two surviving columns rather than the width of the gap, so
    two blank columns still merge and three do not — and getting that boundary
    one out picks a different widest run and crops somewhere else entirely.

    Not hypothetical: a gridline drawn over the spectrogram is a column or two
    of page white in the middle of the plot, and it must not halve the crop.
    """
    whole = find_plot_box(figure(paint=[((100, 0, 102, 728), PAGE)]))
    split = find_plot_box(figure(paint=[((100, 0, 103, 728), PAGE)]))

    assert whole == PLOT, "a two-column gridline split the plot in half"
    assert split == (PLOT[0] + 103, PLOT[1], PLOT[2], PLOT[3])


# --------------------------------------------------------------------------
# what comes back when nothing usable arrived
# --------------------------------------------------------------------------

def test_a_figure_with_no_plot_in_it_renders_nothing_rather_than_raising():
    """The other half of the dead-panel rule: `find_plot_box` answering None
    has to travel out through `render_signal` as None and not as a crop of
    (None, None, None, None)."""
    assert render_signal(png(figure(plot=None, colourbar=None,
                                    furniture=False))) is None


@pytest.mark.parametrize("name,blob", [
    ("nothing", b""),
    ("text", b"not a png at all"),
    # What a captive portal and an expired object actually answer with. Both
    # arrive as a 200 full of bytes, which is why this is not hypothetical.
    ("html", b"<!DOCTYPE html><html><head><title>502 Bad Gateway</title>"),
    ("json", b'{"detail": "Not found"}'),
    ("png magic only", b"\x89PNG\r\n\x1a\n"),
])
def test_bytes_that_are_not_an_image_are_refused_rather_than_thrown(name, blob):
    assert render_signal(blob) is None, name


def test_a_truncated_png_is_refused_rather_than_thrown():
    """Half an object is what a dropped connection leaves behind, and Pillow
    raises for it at decode rather than at open — which is after the `try`
    would have ended if the convert were not inside it."""
    whole = png(figure())

    assert render_signal(whole) is not None, "the whole object should render"
    assert render_signal(whole[:len(whole) // 2]) is None
    assert render_signal(whole[:64]) is None


def test_something_that_is_not_an_image_says_so_in_the_log():
    """The panel goes blank either way. The line in the journal is the only
    difference between "SatNOGS sent us a login page" and an afternoon spent
    reading the crop code."""
    caplog_logger = logging.getLogger("app.services.waterfall")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    caplog_logger.addHandler(handler)
    try:
        assert render_signal(b'{"detail": "Not found"}') is None
    finally:
        caplog_logger.removeHandler(handler)

    assert any("not an image" in r.getMessage() for r in records), records


# --------------------------------------------------------------------------
# the strip itself
# --------------------------------------------------------------------------

def test_the_panel_is_a_png_of_exactly_the_size_the_display_reserves():
    """The tile is a fixed box in the layout and the image is served straight
    to it. A strip of any other size is a panel that either crops itself or
    leaves a gap, depending on which browser is on the wall that week."""
    out = panel(figure())

    assert out.format == "PNG"
    assert out.size == (wf.PANEL_W, wf.PANEL_H) == (760, 150)
    assert out.mode == "RGB"


def test_time_runs_left_to_right_although_it_runs_down_the_source():
    """SatNOGS draws time downwards. This rotates, and a rotation the wrong
    way round produces a strip that looks exactly as convincing while showing
    the pass backwards — LOS on the left, AOS on the right — which is the sort
    of thing that survives for a year because nobody reads a spectrogram
    closely enough to notice.

    The mark is in the first tenth of the time rows, so it belongs at the
    left-hand end and nowhere else.
    """
    rows = (PLOT[3] - PLOT[1]) // 10
    band = PLOT[2] - PLOT[0]

    early = lum(panel(figure(paint=[((0, 0, band, rows), SIGNAL)]))).mean(axis=0)
    late = lum(panel(figure(
        paint=[((0, (PLOT[3] - PLOT[1]) - rows, band, PLOT[3] - PLOT[1]),
                SIGNAL)]))).mean(axis=0)

    bright = np.flatnonzero(early > (early.min() + early.max()) / 2)
    assert bright.min() == 0, "the start of the pass is not at the left edge"
    assert bright.max() < wf.PANEL_W * 0.2, "the start of the pass drifted right"

    bright = np.flatnonzero(late > (late.min() + late.max()) / 2)
    assert bright.max() == wf.PANEL_W - 1, "the end of the pass is not at the right"
    assert bright.min() > wf.PANEL_W * 0.8, "the end of the pass drifted left"


def test_the_centre_crop_keeps_the_middle_of_the_band_and_drops_the_edges():
    """The Doppler curve lives in the middle half of the band; the edges are
    empty sky and the reason the panel is worth cropping at all.

    Asserted as identity rather than as a threshold: a figure whose only
    signal sits outside the kept half has to render the same bytes as a figure
    with no signal in it, because what reaches the stretch is the same pixels.
    A crop that were even slightly wider would let an edge in and the two
    would diverge.
    """
    rows = PLOT[3] - PLOT[1]
    band = PLOT[2] - PLOT[0]

    empty = render_signal(png(figure()))
    outside = render_signal(png(figure(       # 6% to 10% of the band, far out
        paint=[((round(0.06 * band), 0, round(0.10 * band), rows), SIGNAL)])))
    inside = render_signal(png(figure(
        paint=[((round(0.45 * band), 0, round(0.55 * band), rows), SIGNAL)])))

    assert outside == empty, "signal outside the middle half reached the panel"
    assert inside != empty, "signal inside the middle half was cropped away"
    assert lum(Image.open(io.BytesIO(inside))).max() > \
        lum(Image.open(io.BytesIO(empty))).max() + 50


def test_the_kept_band_is_half_the_plot_and_centred_on_it():
    """The test above only proves the edges are gone. This one measures where
    what is left ends up, which is the half of the statement that says how
    *much* was kept — a crop of the middle 90% also drops the far edges and
    would pass everything above.

    Two thin carriers at known points across the band, read back as the rows
    they land on. They fix the scale and the offset of the mapping between
    them, so this pins the width of the kept band and its centring at once,
    and it re-reads the frequency axis on the way: a SatNOGS waterfall has low
    frequency on the left, the rotation puts left at the bottom, so the
    carrier further up the band is the one nearer the top of the strip.
    """
    rows = PLOT[3] - PLOT[1]
    band = PLOT[2] - PLOT[0]

    def lands_at(fraction: float) -> int:
        centre = round(fraction * band)
        strip = lum(panel(figure(
            paint=[((centre - 4, 0, centre + 4, rows), SIGNAL)]))).mean(axis=1)
        return int(np.argmax(strip))

    low, high = lands_at(0.30), lands_at(0.70)

    # 0.30 of the band sits a tenth of the way into a kept half that runs from
    # 0.25 to 0.75, so it lands nine tenths of the way down a 150 px strip.
    assert low == pytest.approx(137, abs=6), "the kept band is not centred"
    assert high == pytest.approx(12, abs=6), "the kept band is not half the plot"
    assert high < low, "the frequency axis came out upside down"


def test_the_signal_is_lifted_out_of_a_floor_that_outshines_it():
    """The floor of a viridis waterfall is bright blue: brighter, in
    luminance, than the teal the signal is drawn in. So a brightness threshold
    keeps the sky and throws the satellite away, and subtracting blue from
    green is what inverts that. The fixture is built to have the inversion in
    it — asserted here, because a fixture whose signal was simply the brighter
    colour would pass this test while proving nothing."""
    floor_l = 0.299 * FLOOR[0] + 0.587 * FLOOR[1] + 0.114 * FLOOR[2]
    signal_l = 0.299 * SIGNAL[0] + 0.587 * SIGNAL[1] + 0.114 * SIGNAL[2]
    assert floor_l > signal_l, "the fixture does not have the inversion in it"
    assert SIGNAL[1] - SIGNAL[2] > FLOOR[1] - FLOOR[2]

    rows = PLOT[3] - PLOT[1]
    band = PLOT[2] - PLOT[0]
    marked = lum(panel(figure(
        paint=[((0, rows // 3, band, rows // 3 + rows // 12), SIGNAL)])))

    strip = marked.mean(axis=0)
    assert strip.max() > strip.min() + 80, "the signal did not separate at all"


def test_the_stretch_keeps_about_the_top_eighth_and_crushes_the_rest():
    """`cutoff=(88, 0.03)` is the number that decides what counts as signal at
    all, and it sounds far more aggressive than it is: a pass is overwhelmingly
    empty sky, so the bottom 88% of the histogram really is the noise floor.

    Measured against a plot that is a smooth ramp from floor to peak — the
    hardest case for a percentile, because there is no floor to find, only a
    continuum, so wherever the cut lands is exactly what survives. About an
    eighth of the panel comes out above the floor. Half of it coming out lit
    is the stretch having been widened to brighten the panel, which is the
    thing the gamma curve exists to do instead.
    """
    rows = PLOT[3] - PLOT[1]
    band = PLOT[2] - PLOT[0]
    ramp = figure()
    ramp.paste(gradient(rows, band, 90, 200), (PLOT[0], PLOT[1]))

    lit = lum(panel(ramp))
    above_floor = float((lit > lit.min() + 0.05 * (lit.max() - lit.min())).mean())

    assert 0.05 < above_floor < 0.25, above_floor


def test_a_pass_with_nothing_in_it_still_draws_a_panel():
    """Most passes hear nothing, and "nothing" is a fact the display is meant
    to show. An empty spectrogram has one value in its histogram, which is the
    input that makes a percentile stretch divide by its own range."""
    out = panel(figure())

    assert out.size == (wf.PANEL_W, wf.PANEL_H)
    assert lum(out).std() < 1.0, "an empty pass is not an empty panel"


def test_a_plot_narrower_than_the_crop_does_not_take_the_panel_down():
    """A degenerate figure — a thin dark stripe on a white page — locates a
    box only a few pixels wide, and the centre crop's floor of 8 px then asks
    for more columns than exist.

    It used to get them. Pillow pads a crop that runs off the edge with black
    rather than refusing it, black is (0, 0, 0), and G-B on black is +128
    against this floor's 18 — so the padding was not merely kept by the
    percentile stretch, it outranked the sky, and the strip drew a bright bar
    down each edge that was the edge of the image pretending to be signal.

    So this asserts the padding is gone. It is checked on ROWS, not columns:
    the padding is added along the frequency axis, and `render_signal` rotates
    the strip 90 degrees so that time runs left to right — which puts the
    frequency axis, and therefore the bars, along the top and bottom edges.
    Asserting on columns passes whether the bug is there or not, which is a
    test that would have shipped the bug back.

    Reachable from any upload whose widest non-white run is 2-7 px.
    """
    stripe = stripes(100, 200, (50, 54))
    box = find_plot_box(stripe)
    assert box is not None and box[2] - box[0] < 8

    out = panel(stripe)
    assert out.size == (wf.PANEL_W, wf.PANEL_H)

    grey = out.convert("L")
    width, height = grey.size

    def row_mean(y: int) -> float:
        return sum(grey.getpixel((x, y)) for x in range(width)) / width

    edges = max(row_mean(0), row_mean(1),
                row_mean(height - 2), row_mean(height - 1))
    middle = row_mean(height // 2)

    assert edges <= middle + 20, (
        f"the panel's top/bottom rows ({edges:.0f}) outshine its middle "
        f"({middle:.0f}) — the centre crop is padding with black again")


# --------------------------------------------------------------------------
# the gamma lift, which decides how brightly what survived is drawn
# --------------------------------------------------------------------------

def test_the_gamma_lut_lifts_the_midtones_and_moves_neither_end():
    """Black has to stay black: the panel is an instrument window, and a floor
    lifted off zero turns it into a grey rectangle with a satellite somewhere
    in it. White has to stay white or the strong bursts near AOS lose their
    top end to make room for nothing."""
    assert wf.SIGNAL_GAMMA == 0.62 < 1.0, "a gamma of 1 or more does not lift"
    assert len(wf._GAMMA_LUT) == 256, "point() wants one entry per level"

    assert wf._GAMMA_LUT[0] == 0
    assert wf._GAMMA_LUT[255] == 255
    assert wf._GAMMA_LUT[64] > 64
    assert wf._GAMMA_LUT[128] > 128


def test_the_gamma_lut_is_monotonic_so_it_cannot_reorder_the_signal():
    """A LUT that dipped anywhere would draw a weaker return brighter than a
    stronger one, which is a spectrogram that lies about which burst was
    which — and it would look entirely normal."""
    assert all(b >= a for a, b in zip(wf._GAMMA_LUT, wf._GAMMA_LUT[1:]))


def test_the_lift_is_steepest_at_the_dark_end_where_the_faint_returns_are():
    """The reason it is a curve and not an offset. A constant added to
    everything only clips the bursts that were already at the top; the whole
    point is the mid-pass returns sitting a few levels above black."""
    assert wf._GAMMA_LUT[32] - 32 > wf._GAMMA_LUT[200] - 200 > 0


def test_the_gamma_lift_brightens_the_signal_without_lifting_the_floor(
        monkeypatch):
    """End to end, against the same figure rendered with an identity LUT —
    which is what this module did before the curve went in, so the comparison
    is against real previous behaviour rather than against a constant.

    Both halves matter and they pull opposite ways. Brightening by widening
    the percentile stretch instead would raise the upper end *and* the floor,
    and the panel would get lighter without getting more legible; that is the
    failure this separation exists to prevent, so the floor is asserted as
    hard as the signal.
    """
    rows = PLOT[3] - PLOT[1]
    band = PLOT[2] - PLOT[0]
    # A tenth of the plot, which keeps the floor above the stretch's 88% cut
    # and so keeps the floor itself at the bottom of the histogram.
    burst = gradient(rows // 10, band, 100, 200)
    page = figure()
    page.paste(burst, (PLOT[0], PLOT[1] + rows // 2))

    lifted = lum(panel(page))
    monkeypatch.setattr(wf, "_GAMMA_LUT", list(range(256)))
    flat = lum(panel(page))

    assert np.percentile(lifted, 95) > np.percentile(flat, 95) + 15, \
        "the curve did not brighten what survived the stretch"
    assert np.median(lifted) == pytest.approx(np.median(flat), abs=1.0), \
        "the curve moved the noise floor"
    assert np.median(lifted) < 40, "the floor is not at the floor"


# --------------------------------------------------------------------------
# the plot finder, against the implementation it replaced
# --------------------------------------------------------------------------

def original_find_plot_box(im: Image.Image) -> tuple[int, int, int, int] | None:
    """`find_plot_box` as it was before numpy: per-pixel reads in Python.

    Kept verbatim, as the thing the fast one has to agree with. It is the only
    definition of "right" this module has — there is no independent statement
    of where the plot is, so a rewrite can only be checked against what the
    old code said, exactly, on every figure it could be handed. A crop that
    moved by two pixels on a wall display is worse than the 72 ms.
    """
    w, h = im.size
    px = im.load()

    def column_ink(x: int) -> float:
        n = hit = 0
        for y in range(0, h, wf.SAMPLE_STEP):
            r, g, b = px[x, y]
            n += 1
            if not (r > wf.WHITE and g > wf.WHITE and b > wf.WHITE):
                hit += 1
        return hit / max(1, n)

    runs: list[tuple[int, int]] = []
    start = prev = None
    for x in range(w):
        if column_ink(x) > wf.INK_FRACTION:
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

    x0, x1 = max(runs, key=lambda r: r[1] - r[0])

    def row_ink(y: int) -> float:
        n = hit = 0
        for x in range(x0, x1, wf.SAMPLE_STEP):
            r, g, b = px[x, y]
            n += 1
            if not (r > wf.WHITE and g > wf.WHITE and b > wf.WHITE):
                hit += 1
        return hit / max(1, n)

    rows = [y for y in range(h) if row_ink(y) > wf.INK_FRACTION]
    if not rows:
        return None
    return (x0, rows[0], x1 + 1, rows[-1] + 1)


# Every shape the scan can be handed, including the ones it answers None for.
# The margins and the plot size vary because that is the axis matplotlib
# actually moves; the rest are the edges of the algorithm, and each of them
# took a figure of its own to find. Both sides of the 55% ink threshold and
# both sides of the three-column merge rule are here because those are the two
# places where a rewrite can be *nearly* right — a run that merges in one
# implementation and splits in the other picks a different widest run and
# crops somewhere else entirely, and no assertion about "roughly the same box"
# would catch it.
FIGURES = {
    "the ordinary page": lambda: figure(),
    "a full-size page": lambda: figure(
        size=(832, 1603), plot=(70, 40, 700, 1500),
        colourbar=(730, 40, 750, 1500)),
    "wide tick labels": lambda: figure(plot=(110, 24, 360, 752)),
    "narrow tick labels": lambda: figure(plot=(20, 24, 360, 752)),
    "an off-centre plot": lambda: figure(plot=(20, 24, 180, 752),
                                         colourbar=(380, 24, 394, 752)),
    # 75 of the 134 sampled rows: one sample over 55%.
    "a plot that just clears the ink threshold": lambda: figure(
        plot=(48, 180, 360, 630), colourbar=(378, 180, 392, 630)),
    # 73 samples, one under — and then the x tick labels beneath the plot add
    # a 74th to the twenty-one columns they cover, which tips those columns
    # over on their own. The scan answers a box the width of a tick label,
    # somewhere in the middle of the page. Nothing here is contrived: it is
    # what a plot near the threshold does, and it is the sharpest test in the
    # set of whether two implementations agree about the threshold itself.
    "a plot under the threshold, tick labels over it": lambda: figure(
        plot=(48, 180, 360, 615), colourbar=(378, 180, 392, 615)),
    "a wide colourbar": lambda: figure(plot=(48, 24, 300, 752),
                                       colourbar=(330, 24, 392, 752)),
    "a colourbar as wide as the plot": lambda: figure(
        plot=(48, 24, 200, 752), colourbar=(240, 24, 392, 752)),
    # The scan takes the widest run and nothing else, so here it takes the
    # colourbar. That is wrong for a panel and right for this sweep: the fast
    # implementation has to be wrong in the same place.
    "a colourbar wider than the plot": lambda: figure(
        plot=(48, 24, 120, 752), colourbar=(200, 24, 392, 752)),
    "a plot flush to the left edge": lambda: figure(plot=(0, 24, 300, 752),
                                                    furniture=False),
    "a plot flush to every edge": lambda: figure(plot=(0, 0, 416, 802),
                                                 colourbar=None,
                                                 furniture=False),
    "a blank band through the plot": lambda: figure(
        paint=[((100, 0, 140, 728), PAGE)]),
    "a two-column gap, which merges": lambda: figure(
        paint=[((100, 0, 102, 728), PAGE)]),
    "a three-column gap, which splits": lambda: figure(
        paint=[((100, 0, 103, 728), PAGE)]),
    "no plot at all": lambda: figure(plot=None, colourbar=None,
                                     furniture=False),
    "axes with nothing in them": lambda: figure(floor=None, colourbar=None),
    "a page that is all ink": lambda: figure(page=(235, 235, 235), plot=None,
                                             colourbar=None, furniture=False),
    "a one-column plot": lambda: stripes(120, 300, (60, 61)),
    "a plot narrower than the crop": lambda: stripes(120, 300, (60, 64)),
    "two runs, the wider one second": lambda: stripes(
        200, 300, (20, 50), (60, 130)),
    "a densely labelled axis": lambda: figure(label_step=13),
    # Exactly 0.55 against 0.60: the only figures here that can tell `>` from
    # `>=`, and the reason they exist at all.
    "a column exactly on the threshold": lambda: part_filled(11),
    "a column one sample over it": lambda: part_filled(12),
    "a row exactly on the threshold": lambda: part_lit(11),
    "a row one sample over it": lambda: part_lit(12),
    # The sampling stride, read off the answer. A comb in phase reads solid
    # and the same comb one row over is not there, so an implementation that
    # sampled from a different offset or with a different step could not
    # possibly agree on both.
    "banding in phase with the sampling": lambda: comb((40, 90), 6, 0),
    "banding one row out of phase": lambda: comb((40, 90), 6, 1),
}


@pytest.mark.parametrize("name", sorted(FIGURES))
def test_the_fast_plot_finder_answers_exactly_what_the_slow_one_did(name):
    """The whole warrant for the numpy rewrite.

    `find_plot_box` is 84 ms of the 286 ms this module costs per pass, all of
    it Python-level pixel reads — about 400k of them. numpy does the same
    arithmetic in one pass, but "same arithmetic" is a claim, and the only
    acceptable evidence for it is the identical tuple out of both
    implementations on every figure shape the scan can meet: moved margins,
    an off-centre plot, a colourbar wider than usual, a tie between two runs,
    gaps either side of the three-column merge rule, a plot one column wide,
    a page with no plot on it and a page with nothing but.

    Identical, not close. Two pixels of drift is two pixels of axis inside
    the crop, on a display nobody is looking at closely.
    """
    im = FIGURES[name]()

    assert find_plot_box(im) == original_find_plot_box(im)


def random_page(rng: random.Random) -> Image.Image:
    """A page built to land on the scan's boundaries rather than away from them.

    Twenty hand-drawn figures cover the shapes somebody thought of. This
    covers the ones nobody did: channel values clustered on 235 so the
    background test is decided by one level either way, bands at strides
    around `SAMPLE_STEP` so the row sampling aliases or does not, and gaps of
    every width so runs merge or split. Sizes down to a single pixel, because
    the interesting divisions in this function are the ones with nothing in
    the numerator.
    """
    w, h = rng.randrange(1, 140), rng.randrange(1, 220)
    page = np.full((h, w, 3), rng.choice([255, 250, 236, 235, 234]), dtype=np.uint8)
    for _ in range(rng.randrange(0, 6)):
        x0 = rng.randrange(0, w)
        y0 = rng.randrange(0, h)
        block = (slice(y0, min(h, y0 + rng.randrange(1, h + 1)),
                       rng.choice([1, 1, 1, 2, 3, 5, 6, 7])),
                 slice(x0, min(w, x0 + rng.randrange(1, 40))))
        page[block] = [rng.randrange(230, 241) if rng.random() < 0.6
                       else rng.randrange(0, 256) for _ in range(3)]
    return Image.fromarray(page, "RGB")


def test_the_fast_plot_finder_agrees_on_pages_nobody_thought_to_draw():
    """The hand-drawn sweep above proves agreement on the figures somebody
    designed, which is the weaker half of the claim: the shapes that get drawn
    on purpose are the shapes both implementations were written with in mind.

    A fixed seed, so a failure here is a failure that can be reproduced rather
    than a flake that gets rerun until it goes away.
    """
    rng = random.Random(20260921)
    pages = [random_page(rng) for _ in range(500)]
    boxes = [find_plot_box(page) for page in pages]

    for page, box in zip(pages, boxes):
        assert box == original_find_plot_box(page), np.asarray(page).tobytes().hex()[:64]

    found = sum(box is not None for box in boxes)
    assert 100 < found < 400, \
        f"{found}/500 pages found a plot; the generator has stopped covering both"


def test_a_figure_that_is_not_rgb_is_read_rather_than_refused():
    """The one place the fast scan deliberately does not reproduce the loop it
    replaced, recorded here so it is a decision and not a drift.

    `r, g, b = px[x, y]` refused every other mode by failing to unpack: a
    TypeError on a greyscale or palette PNG, a ValueError on one with an alpha
    channel. `render_signal` converts before it calls this so the panel never
    met any of them, but a raise is the wrong answer for a function whose
    whole contract is to hand back None when it cannot help — and the numpy
    version has a worse failure available to it than a raise, because on RGBA
    the background test would quietly fold alpha in as a fourth channel and
    read transparent page as ink. Converting closes both.
    """
    page = figure()
    for mode in ("L", "P", "RGBA", "CMYK"):
        with pytest.raises((TypeError, ValueError)):
            original_find_plot_box(page.convert(mode))
        assert find_plot_box(page.convert(mode)) == PLOT, mode


def test_the_figure_set_covers_both_answers_and_not_only_one():
    """A sweep in which every figure answered None would agree perfectly and
    prove nothing at all."""
    answers = [original_find_plot_box(FIGURES[name]()) for name in FIGURES]

    assert sum(a is None for a in answers) >= 3, "nothing exercises the None path"
    assert sum(a is not None for a in answers) >= 10, "almost nothing finds a plot"
    assert len({a for a in answers if a is not None}) >= 8, \
        "the figures that do find a plot mostly find the same one"
