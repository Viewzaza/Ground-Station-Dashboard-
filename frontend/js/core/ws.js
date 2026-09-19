/* The live link to the backend.

   Panels never touch this module. It validates frames, writes them into the
   store and publishes on the bus — which is what lets a panel be driven by a
   recorded frame log in a test instead of a live station.

   Two things a dashboard that runs for months needs and a naive client lacks:
   it reconnects with backoff, and it notices silence. A socket that is open but
   dead looks exactly like a quiet pass otherwise. */

import { set, setStatus, store } from './store.js';
import { bus } from './bus.js';

const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 15000;
const SILENCE_LIMIT_MS = 25000;

let socket = null;
let backoff = RECONNECT_MIN_MS;
let lastFrameAt = 0;
let lastSeq = null;
let watchdog = null;

function url() {
  const u = new URL('/ws', location.href);
  u.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return u.toString();
}

export function connect() {
  if (socket && (socket.readyState === WebSocket.OPEN
              || socket.readyState === WebSocket.CONNECTING)) return;

  socket = new WebSocket(url());

  socket.onopen = () => {
    backoff = RECONNECT_MIN_MS;
    lastFrameAt = Date.now();
    setStatus('api', 'ok');
    startWatchdog();
  };

  socket.onmessage = (ev) => {
    lastFrameAt = Date.now();
    let frame;
    try {
      frame = JSON.parse(ev.data);
    } catch {
      return console.warn('[ws] unparsable frame');
    }
    handle(frame);
  };

  socket.onclose = () => {
    stopWatchdog();
    setStatus('api', 'down', 'link closed');
    scheduleReconnect();
  };

  socket.onerror = () => socket?.close();
}

function scheduleReconnect() {
  const wait = backoff + Math.random() * backoff * 0.3;
  backoff = Math.min(backoff * 2, RECONNECT_MAX_MS);
  setTimeout(connect, wait);
}

function startWatchdog() {
  stopWatchdog();
  watchdog = setInterval(() => {
    if (Date.now() - lastFrameAt > SILENCE_LIMIT_MS) {
      // An open-but-dead socket is indistinguishable from a quiet pass unless
      // you time the silence. Drop it and let the reconnect path run.
      console.warn('[ws] no frames for %ds, reconnecting', SILENCE_LIMIT_MS / 1000);
      socket?.close();
    }
  }, 5000);
}

function stopWatchdog() {
  clearInterval(watchdog);
  watchdog = null;
}

function send(type, data = {}) {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ v: 1, type, data }));
  }
}

function handle(frame) {
  // A gap in the sequence means we missed something; ask for a fresh picture
  // rather than rendering a half-stale dashboard.
  if (frame.seq && lastSeq !== null && frame.seq > lastSeq + 1 && frame.type !== 'snapshot') {
    send('resync', { last_seq: lastSeq });
  }
  if (frame.seq) lastSeq = frame.seq;

  switch (frame.type) {
    case 'hello':
      bus.emit('hello', frame.data);
      break;

    case 'snapshot':
      for (const inner of frame.data.frames || []) handle(inner);
      break;

    case 'rotator':
      set('rotator', frame.data);
      break;

    case 'pointing':
      set('pointing', frame.data.valid ? frame.data : null);
      break;

    case 'satpos':
      // The browser propagates its own position for a smooth render; the
      // server's copy is only used when we have no elements of our own.
      if (!store.satpos) set('satpos', frame.data);
      break;

    case 'pass_next':
      set('nextPass', frame.data && frame.data.pass_id ? frame.data : null);
      break;

    case 'tle':
      set('tle', frame.data);
      break;

    case 'satnogs':
      set('satnogs', frame.data);
      break;

    case 'spaceweather':
      set('spaceweather', frame.data);
      break;

    case 'rig':
      // satnogs-client's own Doppler-corrected frequency, read back from the
      // station's rigctld. An independent answer to the one we compute.
      set('rig', frame.data);
      break;

    case 'control':
      // The interlock closes on its own — a lease expires, SatNOGS picks up a
      // job — so this arrives unprompted and must repaint the panel.
      set('control', frame.data);
      break;

    case 'status':
      if (frame.data.component !== 'backend') {
        setStatus(chipFor(frame.data.component), frame.data.state, frame.data.detail);
      }
      break;

    case 'error':
      console.warn('[ws] server error:', frame.data);
      break;

    default:
      break;
  }
}

/** Map a backend component name onto the header chip that shows it.

    A component with no chip here still lands in store.status under its own
    name, and paintChips simply finds nothing to paint — which is what happens
    to `spaceweather`. It has no chip on purpose: the Space weather panel shows
    the age of its own newest reading, so a poller that has stopped is already
    visible where someone is looking at it, and /api/health still reports the
    component for anything watching from outside. */
function chipFor(component) {
  return { rotctld: 'rot', tle: 'tle', satnogs: 'satnogs', camera: 'cam' }[component]
      || component;
}

export const ws = { connect, send };
