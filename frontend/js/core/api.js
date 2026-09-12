/* Every call to the backend goes through here, so retry and error handling live
   in one place and panels never build URLs by hand. */

async function get(path, params) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null) url.searchParams.set(k, v);
  }
  const resp = await fetch(url, { headers: { accept: 'application/json' } });
  if (!resp.ok) {
    const body = await resp.text().catch(() => '');
    throw new Error(`${resp.status} ${path}: ${body.slice(0, 160)}`);
  }
  return resp.json();
}

export const api = {
  health:       ()            => get('/api/health'),
  config:       ()            => get('/api/config'),
  satellites:   (q)           => get('/api/satellites', { q }),
  tle:          (norad)       => get('/api/tle', { norad }),
  satpos:       (norad)       => get('/api/satpos', { norad }),
  groundtrack:  (norad)       => get('/api/groundtrack', { norad }),
  passes:       (norad, hours)=> get('/api/passes', { norad, hours }),
  nextPass:     (norad)       => get('/api/passes/next', { norad }),
  passTrack:    (passId)      => get(`/api/passes/${encodeURIComponent(passId)}/track`),
  cameras:      ()            => get('/api/cameras'),
};
