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

/** A refusal from the control interlock, carrying the gates that are shut. */
export class Refused extends Error {
  constructor(message, blockedBy) {
    super(message);
    this.blockedBy = blockedBy || [];
  }
}

async function post(path, body) {
  const resp = await fetch(new URL(path, location.origin), {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const payload = await resp.json().catch(() => null);
  if (resp.status === 409) {
    // The interlock refusing is an ordinary, expected answer, so it is modelled
    // as a typed result the panel can render rather than an unexpected failure.
    const detail = payload?.detail || {};
    throw new Refused(detail.error || 'refused', detail.blocked_by);
  }
  if (!resp.ok) {
    throw new Error(`${resp.status} ${path}: ${JSON.stringify(payload).slice(0, 160)}`);
  }
  return payload;
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
  satnogs:      ()            => get('/api/satnogs'),
  radio:        (norad)       => get('/api/radio', { norad }),
  waterfall:    (norad)       => get('/api/radio/waterfall', { norad }),
  telemetry:    (norad)       => get('/api/telemetry', { norad }),
  rig:          ()            => get('/api/rig'),

  control:      ()            => get('/api/control'),
  arm:          ()            => post('/api/control/arm'),
  release:      ()            => post('/api/control/release'),
  goto:         (az, el)      => post('/api/control/goto', { az, el }),
  park:         ()            => post('/api/control/park'),
  track:        (norad)       => post('/api/control/track', { norad }),
  stopRotator:  ()            => post('/api/control/stop'),
};
