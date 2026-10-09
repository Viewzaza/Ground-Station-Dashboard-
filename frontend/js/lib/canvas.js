/* Canvas helpers. `fit` and `niceStep` are carried over unchanged from the
   previous dashboard in this repo — they were already correct. */

export function fit(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const r = canvas.getBoundingClientRect();
  const w = Math.max(1, Math.round(r.width));
  const h = Math.max(1, Math.round(r.height));
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr;
    canvas.height = h * dpr;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

export function niceStep(span, targetTicks) {
  const raw = span / Math.max(1, targetTicks);
  const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const norm = raw / mag;
  const step = norm > 5 ? 10 : norm > 2 ? 5 : norm > 1 ? 2 : 1;
  return step * mag;
}

/** Read the palette out of CSS so canvas drawing follows the theme tokens.

    The two canvases this serves — the 2D ground track and the rotator polar
    plot — sit on white cards. The 3D globe does not: it stays a dark instrument
    window, and it reads its own surface colours from the `--globe-*` tokens.
    So every value here is picked to survive a near-white background, including
    the fallbacks, which mirror the light tokens rather than the dark theme they
    replaced.

    The keys are the names map2d.js and rotator.js already ask for. Several of
    those names no longer describe what they carry, so each is commented with
    what it actually draws. Everything that is data resolves to one of the three
    hues; everything else is ink or rule.

    map2d.js reads `--panel` and `--night` directly for the two surfaces this
    function does not carry, and caches them rather than calling
    getComputedStyle from inside its per-tick draw. */
export function palette() {
  const css = getComputedStyle(document.documentElement);
  const v = (name, fallback) => css.getPropertyValue(name).trim() || fallback;

  // The three data hues, same three as the globe and the rest of the page.
  const track = v('--track', '#0086ad');        // the path: where it runs
  const contact = v('--contact', '#b4670f');    // happening now: the satellite
  const observer = v('--observer', '#c42a6e');  // us: the station and its antenna

  return {
    track,
    contact,
    observer,

    // --- structure and type ---
    line:     v('--rule', '#cbd6db'),       // equator; the 0 deg horizon ring
    lineSoft: v('--rule-soft', '#dde4e8'),  // graticule; elevation rings, spokes
    muted:    v('--ink-3', '#536169'),      // cardinal labels
    dim:      v('--ink-4', '#7c8c95'),      // ring labels, "no rotator link"
    text:     v('--ink', '#0e1a1f'),        // the satellite name on the map
    panel2:   v('--sunk', '#e9edef'),

    // --- legacy names, mapped onto the three hues ---
    // Ground track (map2d) and predicted pass arc (rotator): both are the path.
    accent2: track,
    // The satellite while it is in view, in both panels. This key also draws
    // the station cross in map2d, which wants `observer` instead — one word in
    // map2d.js, and the key is here ready for it.
    ok: contact,
    // Where the antenna is actually pointing. Named `warn`, but it is us, so it
    // takes the observer hue — and it has to differ from the satellite dot
    // beside it, since the gap between the two is what the polar plot is read
    // for. On --warn they would be the same colour.
    warn: observer,
    // The satellite while it is NOT in view. Neutral rather than a data hue:
    // on `track` the dot would vanish into the line it sits on, and on
    // `contact` it would no longer be distinguishable from being in view.
    // rotator.js already draws its below-horizon satellite in `dim`, so the two
    // panels agree — not live is ink, live is contact.
    accent: v('--ink-2', '#3c4d56'),
    // North label; "ROTATOR LINK DOWN".
    bad: v('--bad', '#c42a6e'),
  };
}
