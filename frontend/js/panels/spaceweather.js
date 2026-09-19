/* Space weather: what the Sun is doing to the path.

   The one question this panel answers that no other panel on the wall can:
   when a pass produces no frames, was the ionosphere a plausible reason? Radio,
   Last signal and Decoded frames all report the same nothing in that case, and
   all three report it afterwards. This one can be read before the pass.

   The graph is GOES X-ray flux over six hours on a log axis — the plot
   spaceweatherlive.com leads with, for the same reason: a flare is a shape, and
   a single number that changed does not show you one.

   The numbers come from NOAA SWPC via our own backend, never from the browser.
   A wall display left open for months would otherwise be one more client
   hammering a public service every minute for data it already has. */

import { api } from '../core/api.js';
import { store, set } from '../core/store.js';
import { bus } from '../core/bus.js';
import { fit } from '../lib/canvas.js';

const $ = (id) => document.getElementById(id);

/* Fixed decades, 1e-9 to 1e-3, and deliberately NOT auto-scaled.

   An axis that fits itself to its data draws a quiet Sun and an active one
   identically — same curve, same height, different labels nobody reads at a
   glance. Pinning the decades is what makes "flat along the bottom" mean quiet
   and "climbing into the M band" mean something, which is the entire
   comparison this panel exists to support. It costs the ability to see fine
   structure in a quiet background, which is not information anyone here needs. */
const FLUX_MIN = 1e-9;
/* 1e-2 and not 1e-3, which is X10 — the R4 threshold, and a level real flares
   reach. Clamped there, the largest flare anyone will ever see on this display
   would flat-line along the top of the plot while the headline beside it read
   X15: the graph contradicting the number next to it, which is the exact
   failure the peak-not-mean downsampling exists to prevent, arriving on the
   one event that matters most. SWPC's own X-ray plot runs to 1e-2. The extra
   decade costs about 14% of vertical resolution. */
const FLUX_MAX = 1e-2;

/* The class boundaries, as decade lines with a letter each. A1 is 1e-8. */
const BANDS = [
  { letter: 'A', flux: 1e-8 },
  { letter: 'B', flux: 1e-7 },
  { letter: 'C', flux: 1e-6 },
  { letter: 'M', flux: 1e-5 },
  { letter: 'X', flux: 1e-4 },
];

/* A stroke is broken when the step between two samples exceeds this many
   nominal buckets. GOES XRS drops out — a six-hour window with a 67-minute
   hole in it is an ordinary day — and a polyline drawn straight through that
   is not a quiet Sun, it is an hour of data this dashboard invented. Three
   buckets is loose enough to survive one or two missing minutes, which are
   noise, and tight enough to catch a real outage. */
const GAP_BUCKETS = 3;

/* The window the backend's feed covers, and the domain of the time axis. */
const WINDOW_MS = 6 * 3600 * 1000;

const LINKS = {
  // Where the bytes come from. Credited first because it is the answer to
  // "says who", and because SWPC is the upstream both of the sites below are
  // presenting.
  swpc: 'https://www.swpc.noaa.gov/',
  // Where to go next. Written for people, with the interpretation a JSON feed
  // cannot carry: what an M-class flare in this part of the cycle actually
  // means for tonight.
  swl: 'https://www.spaceweatherlive.com/en/solar-activity.html',
  sw: 'https://spaceweather.com/',
};

let canvas = null;
let colours = null;

export function mountSpaceWeather() {
  bus.on('spaceweather', render);
  render();

  // The poller publishes on a two-minute cadence, so a browser opened between
  // two of them would otherwise sit on "awaiting SWPC" for that long.
  api.spaceweather()
    .then((data) => set('spaceweather', data))
    .catch((err) => console.warn('[spaceweather]', err));
}

/** Repaint just the canvas. main.js calls this on resize; the DOM is untouched. */
export function resizeSpaceWeather() {
  if (canvas) drawGraph();
}

function palette() {
  // Cached: this is read on every repaint, and getComputedStyle on the root
  // element forces a style recalculation each time it is called.
  if (colours) return colours;
  const css = getComputedStyle(document.documentElement);
  const v = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
  colours = {
    long: v('--contact', '#b4670f'),   // the live measured value: the same hue
    short: v('--ink-4', '#7c8c95'),    // the harder band, secondary
    rule: v('--rule', '#cbd6db'),
    ruleSoft: v('--rule-soft', '#dde4e8'),
    label: v('--ink-4', '#7c8c95'),
    sunk: v('--sunk', '#e9edef'),
  };
  return colours;
}

function render() {
  const host = $('sw-panel');
  const hint = $('sw-hint');
  if (!host) return;

  const sw = store.spaceweather;
  if (!sw) {
    host.innerHTML = '<p class="muted">awaiting SWPC</p>';
    canvas = null;
    return;
  }

  // The heading carries provenance, not status: which spacecraft and how old.
  // `/primary/` follows whichever GOES bird SWPC has designated, so the number
  // is read from the payload and never assumed.
  if (hint) {
    const bird = sw.satellite ? `GOES-${sw.satellite}` : 'GOES';
    hint.textContent = `NOAA SWPC · ${bird}${sw.age_s == null ? '' : ` · ${age(sw.age_s)}`}`;
    // The age is the newest READING's, not the newest poll's — a successful
    // fetch of a file whose last half hour is electron-contaminated zeros is a
    // fresh poll of stale data. The poll age is here too, because the two
    // together are what distinguish "SWPC is behind" from "we stopped asking".
    hint.title = 'GOES XRS long channel (0.1–0.8 nm), last 6 h, log scale.\n\n'
      + 'The age is of the newest usable reading.'
      + (sw.polled_s == null ? '' : ` SWPC last answered ${age(sw.polled_s)} ago.`)
      + '\n\nSource: NOAA SWPC.';
  }
  citeLink();

  host.innerHTML = `
    <div class="sw-readouts">
      ${classLine(sw)}
      ${flareLine(sw)}
      ${scalesRow(sw)}
      ${indicesRow(sw)}
    </div>
    <div class="sw-plot"><canvas id="sw-canvas"></canvas></div>`;

  canvas = $('sw-canvas');
  drawGraph();
}

/** The "open full"-style link, added to the heading once. */
function citeLink() {
  const h2 = document.querySelector('.sw h2');
  if (!h2 || h2.querySelector('.sw-cite')) return;
  const a = document.createElement('a');
  a.className = 'sw-cite';
  a.href = LINKS.swl;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  a.textContent = 'spaceweatherlive ↗';
  a.title = `Interpretation and forecasts: spaceweatherlive.com. `
          + `See also spaceweather.com (${LINKS.sw}). Data: NOAA SWPC (${LINKS.swpc}).`;
  h2.appendChild(a);
}

function classLine(sw) {
  const klass = sw.xray?.class;
  if (!klass) {
    return '<div class="sw-class unknown">no X-ray data</div>';
  }
  return `<div class="sw-class c-${escape(sw.xray.letter || '')}"
               title="GOES long-channel flux ${flux(sw.xray.current_flux)} W/m²"
          >${escape(klass)}</div>`;
}

function flareLine(sw) {
  const flare = sw.flare;
  if (!flare || !flare.max_class) {
    return '<div class="sw-sub">no flare on record</div>';
  }
  // `end_time` null means SWPC has not seen it decay yet. That is the one state
  // on this panel worth interrupting someone for, so it says so in words rather
  // than leaving the operator to notice a missing timestamp.
  if (flare.in_progress) {
    return `<div class="sw-sub live">${escape(flare.max_class)} flare in progress</div>`;
  }
  /* With the date, once it is not today's. SWPC keeps serving the last flare
     event indefinitely — today's ended at 06:06 UTC and was still the "latest"
     nine hours later — so through a quiet stretch this line can be days old,
     and a bare "05:36Z" reads as this morning. */
  return `<div class="sw-sub" title="Peaked ${escape(flare.max || '')}">
            last flare ${escape(flare.max_class)} · ${stamp(flare.max)}
          </div>`;
}

/* The three NOAA scales, with what each one actually costs this station in the
   tooltip. Station 5024 works a 400 MHz UHF downlink from 13.8°N, and the
   honest answer for two of the three is "not much" — saying so is worth more
   than three ominous acronyms that an operator either over-reads or learns to
   ignore.

   Every one of these is a 24-HOUR OBSERVED MAXIMUM, which is SWPC's own label
   for this row, so each tooltip says so. A G3 at 02:00 UTC still reads G3 at
   midnight. */
const MAX_NOTE = '\n\nThis is SWPC\'s 24-hour observed maximum, not the level '
               + 'right now — it stays up for the rest of the day after an event.';

const SCALE_HELP = {
  // R is X-ray peak flux and nothing else. It is worth being exact about that,
  // because the thing that can actually hurt a UHF link — a metric-wavelength
  // radio burst — is not what R measures, and an operator who reads R0 as "no
  // solar radio problem" has read it wrong.
  R: 'R — radio blackout, graded on solar X-ray peak flux alone. It is an HF '
   + 'scale: D-region absorption falls off as roughly 1/f², so at 400 MHz even '
   + 'an X-class flare costs this link a fraction of a dB.\n\nThe UHF hazard a '
   + 'flare can bring is a 10 cm radio burst, which R does NOT measure. A burst '
   + 'of a few thousand SFU with the Sun near the antenna boresight is tens of '
   + 'dB of receiver desense for minutes; the same burst with the Sun well off '
   + 'boresight is nothing. Sun–satellite separation decides it.' + MAX_NOTE,
  S: 'S — solar radiation storm, from energetic protons. Mostly a spacecraft '
   + 'problem (single-event upsets, degraded solar cells) rather than a link '
   + 'one: at 13.8°N and 51.6° inclination neither end sees the polar caps, '
   + 'where the absorption would be.' + MAX_NOTE,
  // The drag story is real but slower and smaller than it first looks — see
  // the README for the arithmetic. What it is NOT is an explanation for a pass
  // that failed tonight.
  G: 'G — geomagnetic storm, from Kp. The one that reaches this dashboard, but '
   + 'on a delay: thermospheric heating raises drag, element sets then age '
   + 'faster than usual, and AOS drifts. At G1 that is about a second a day — '
   + 'invisible. It is worth watching from about G3, and mostly for TOMORROW\'s '
   + 'passes rather than tonight\'s.\n\nIt does not cover equatorial '
   + 'scintillation, which at this latitude is the likelier ionospheric reason '
   + 'a pass breaks up — and which happens on quiet days.' + MAX_NOTE,
};

function scalesRow(sw) {
  const scales = sw.scales || {};
  const cells = ['R', 'S', 'G'].map((key) => {
    const level = scales[key];
    const known = level !== null && level !== undefined;
    const cls = known ? `lvl-${level}` : 'lvl-none';
    let help = SCALE_HELP[key];
    if (!known) help += '\n\nNot published yet — this is unknown, not quiet.';
    // When the daily scale and the three-hourly Kp disagree, the panel is
    // showing the Kp-derived value, and saying so is the difference between a
    // number the operator can check against SWPC and one that looks wrong.
    if (key === 'G' && scales.g_from_kp) help += '\n\nDerived from the current Kp, which is more recent than SWPC\'s daily G.';
    return `<span class="sw-scale ${cls}" title="${escape(help)}">${key}${known ? level : '?'}</span>`;
  }).join('');
  // "24 h" in the row itself, not only in the tooltips. Three scale cells with
  // no time base beside a Kp that means "now" is the kind of thing a wall
  // display gets read wrong from across the room.
  return `<div class="sw-scales">${cells}<span class="sw-window">24 h max</span></div>`;
}

function indicesRow(sw) {
  const kp = sw.kp;
  // Kp 5 is NOAA's storm threshold and also roughly where drag starts moving a
  // LEO element set faster than the two-hour TLE refresh can follow.
  const storm = kp !== null && kp !== undefined && kp >= 5;
  // The G level this Kp is, so the live number and the cell above it are in
  // the same units and can be compared without doing the mapping in your head.
  const level = sw.kp_g ? ` G${sw.kp_g}` : '';
  const kpHelp = 'Planetary K index — SWPC\'s 1-minute running estimate, which '
               + 'is what their dashboard and spaceweatherlive.com show as the '
               + 'current Kp. Unlike the scales beside it, this one is NOW.'
               + (sw.kp_at ? `\n\nFor ${shortTime(sw.kp_at)}.` : '');
  const kpCell = kp === null || kp === undefined
    ? '<span>Kp <b>—</b></span>'
    : `<span class="${storm ? 'storm' : ''}" title="${escape(kpHelp)}">Kp <b>${kp.toFixed(1)}${level}</b></span>`;
  const f107 = sw.f107 === null || sw.f107 === undefined
    ? '<span>F10.7 <b>—</b></span>'
    : `<span title="10.7 cm solar radio flux, daily, from Penticton">F10.7 <b>${Math.round(sw.f107)}</b></span>`;
  return `<div class="sw-indices">${kpCell}${f107}</div>`;
}

// --------------------------------------------------------------------------
// the graph
// --------------------------------------------------------------------------

function drawGraph() {
  if (!canvas) return;
  const series = store.spaceweather?.xray?.series || [];
  const { ctx, w, h } = fit(canvas);
  const c = palette();

  ctx.clearRect(0, 0, w, h);

  // Room on the right for the band letters. Nothing on the left: the Y axis is
  // labelled by the bands themselves, and a column of "1e-6" strings would cost
  // a third of a 300px-wide plot to say the same thing less legibly.
  const padRight = 13;
  const plotW = Math.max(1, w - padRight);
  const top = 2;
  const plotH = Math.max(1, h - top - 1);

  const y = (value) => {
    const clamped = Math.min(FLUX_MAX, Math.max(FLUX_MIN, value));
    const frac = (Math.log10(clamped) - Math.log10(FLUX_MIN))
               / (Math.log10(FLUX_MAX) - Math.log10(FLUX_MIN));
    return top + plotH * (1 - frac);
  };

  // --- bands -------------------------------------------------------------
  ctx.lineWidth = 1;
  // A literal stack, not var(--mono): canvas parses its own font shorthand and
  // does not resolve CSS custom properties. An unparsable value is dropped
  // silently and the band letters come out in the 10px sans default.
  ctx.font = '9px "Cascadia Mono", "SF Mono", ui-monospace, Menlo, Consolas, monospace';
  ctx.textBaseline = 'middle';
  for (const band of BANDS) {
    const py = Math.round(y(band.flux)) + 0.5;
    // M and X are the two that mean anything operationally, so they get the
    // stronger rule. The rest are there to make the decades readable.
    ctx.strokeStyle = (band.letter === 'M' || band.letter === 'X') ? c.rule : c.ruleSoft;
    ctx.beginPath();
    ctx.moveTo(0, py);
    ctx.lineTo(plotW, py);
    ctx.stroke();

    ctx.fillStyle = c.label;
    ctx.fillText(band.letter, plotW + 3, py);
  }

  // Nothing to plot leaves the bare axis. The readout column beside it already
  // says "no X-ray data" in type the panel can style; a second copy painted
  // into the canvas only lands on top of a band line and strikes it through.
  if (!series.length) return;

  // --- the two channels --------------------------------------------------
  /* The time axis is pinned to the last six hours, not stretched to fit
     whatever the feed happened to return — the same argument the y decades are
     pinned for, which was not applied here first time round. After a dropout
     at the start of the window the series begins late, and scaling it to the
     full width drew five hours as though they were six, with no axis labels to
     give it away. The right edge takes whichever is later of now and the last
     sample, so a browser clock a little behind SWPC cannot push the newest
     point off the end. */
  const stamps = series.map((p) => Date.parse(p.t));
  const tEnd = Math.max(Date.now(), stamps[stamps.length - 1]);
  const tStart = tEnd - WINDOW_MS;
  const x = (ms) => (ms - tStart) / WINDOW_MS * plotW;

  // The backend sends the nominal bucket width so the two cannot disagree
  // about what counts as a gap. Without it, fall back to the median step,
  // which is the same number by another route.
  const bucketMs = (store.spaceweather?.xray?.bucket_s || medianStep(stamps) / 1000) * 1000;
  const gapMs = bucketMs * GAP_BUCKETS;

  // Short channel first, so the long one — the channel the class is defined on
  // — is drawn over it rather than under.
  strokeChannel(ctx, series, stamps, 'short', x, y, gapMs, c.short, 1);
  strokeChannel(ctx, series, stamps, 'long', x, y, gapMs, c.long, 1.6);
}

function strokeChannel(ctx, series, stamps, key, x, y, gapMs, colour, width) {
  ctx.strokeStyle = colour;
  ctx.lineWidth = width;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.beginPath();

  let drawing = false;
  for (let i = 0; i < series.length; i += 1) {
    const value = series[i][key];
    if (value === null || value === undefined) {
      drawing = false;
      continue;
    }
    const broke = drawing && (stamps[i] - stamps[i - 1]) > gapMs;
    if (!drawing || broke) {
      ctx.moveTo(x(stamps[i]), y(value));
      drawing = true;
    } else {
      ctx.lineTo(x(stamps[i]), y(value));
    }
  }
  ctx.stroke();
}

function medianStep(stamps) {
  if (stamps.length < 2) return 120_000;
  const steps = [];
  for (let i = 1; i < stamps.length; i += 1) steps.push(stamps[i] - stamps[i - 1]);
  steps.sort((a, b) => a - b);
  return steps[Math.floor(steps.length / 2)] || 120_000;
}

// --------------------------------------------------------------------------
// formatting
// --------------------------------------------------------------------------

function age(seconds) {
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

function flux(value) {
  return (value === null || value === undefined) ? '—' : value.toExponential(1);
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** `14:20Z` if it happened today UTC, `18 Sep 05:36Z` if it did not. */
function stamp(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const now = new Date();
  const sameDay = d.getUTCFullYear() === now.getUTCFullYear()
               && d.getUTCMonth() === now.getUTCMonth()
               && d.getUTCDate() === now.getUTCDate();
  return sameDay
    ? shortTime(iso)
    : `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${shortTime(iso)}`;
}

function shortTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? ''
    : `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}Z`;
}

function escape(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}
