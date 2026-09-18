/* Camera tiles — the centre of the display.

   go2rtc ships a <video-stream> custom element whose default mode chain is
   webrtc → mse → hls → mjpeg, so a viewer behind a firewall that blocks UDP
   still gets a picture instead of a black rectangle. We load that element from
   the go2rtc container itself (proxied at /video) rather than vendoring it, so
   the element and the server are always the same version.

   If the element never produces a frame — go2rtc down, camera unplugged — the
   tile degrades to a 1 Hz JPEG poll through the backend. The snapshot is
   proxied rather than linked because the camera speaks plain HTTP with Digest
   auth: a direct URL would be mixed content and would leak the password.
*/

import { api } from '../core/api.js';
import { set, setStatus, store } from '../core/store.js';

// One tile: the station has one camera, and 101/102 are its main and sub
// streams. Showing both meant half the wall was the same picture twice.
const SLOT_ID = 'cam-slot-0';
const STATE_LABEL_ID = 'cam1-state';
const PREFERRED_STREAM = 'cam_main';
const FALLBACK_AFTER_MS = 15000;
const SNAPSHOT_INTERVAL_MS = 1000;
const SNAPSHOT_RETRY_MS = 15000;     // after repeated failures, stop hammering

let elementLoaded = null;

/* What the backend said about the video bridge when the inventory was fetched.
   The tile discovers that it has no picture some seconds later, by which point
   the only thing it knows on its own is that no frame arrived — which is true
   of a stopped bridge, an unplugged camera and a blocked port alike. */
function bridgeDetail() {
  return store.cameraBridge?.detail || '';
}

/** Load go2rtc's custom element once, from the proxied go2rtc instance. */
function loadVideoStreamElement() {
  if (elementLoaded) return elementLoaded;
  elementLoaded = new Promise((resolve) => {
    if (customElements.get('video-stream')) return resolve(true);
    const script = document.createElement('script');
    script.type = 'module';
    script.src = '/video/video-stream.js';
    script.onload = () => resolve(true);
    script.onerror = () => resolve(false);
    document.head.appendChild(script);
  });
  return elementLoaded;
}

class CameraTile {
  constructor(slotId, labelId) {
    this.slot = document.getElementById(slotId);
    this.label = document.getElementById(labelId);
    this.badge = null;
    this.note = null;
    this.snapshotTimer = null;
    this.fallbackTimer = null;
  }

  clear() {
    clearTimeout(this.snapshotTimer);
    clearTimeout(this.fallbackTimer);
    this.snapshotTimer = this.fallbackTimer = null;
    this.slot.replaceChildren();
    // Both were children of the slot that has just been emptied; holding the
    // stale references would append the next badge to a detached node.
    this.badge = this.note = null;
  }

  setBadge(text, kind) {
    if (!this.badge) {
      this.badge = document.createElement('span');
      this.slot.appendChild(this.badge);
    }
    this.badge.className = `cam-badge ${kind}`;
    this.badge.textContent = text;
  }

  placeholder(text) {
    this.clear();
    const p = document.createElement('p');
    p.className = 'cam-placeholder';
    p.textContent = text;
    this.slot.appendChild(p);
  }

  /* A sentence under the badge saying what is actually wrong. Replaced rather
     than appended, so a tile that has been up for a month has one of these and
     not thirty. */
  explain(text) {
    if (!text) return;
    this.note?.remove();
    this.note = document.createElement('p');
    this.note.className = 'cam-note';
    this.note.textContent = text;
    this.slot.appendChild(this.note);
  }

  async mount(camera) {
    this.clear();
    if (this.label) this.label.textContent = camera.label || camera.id;

    const ok = await loadVideoStreamElement();
    if (!ok) {
      // The element is served BY go2rtc, so failing to load it is already
      // proof the bridge is not there — no need to wait out the video
      // timeout to say so.
      this.useSnapshot(camera, 'BRIDGE UNREACHABLE');
      return this.explain(bridgeDetail());
    }

    const el = document.createElement('video-stream');
    el.setAttribute('mode', 'webrtc,mse,hls,mjpeg');
    el.setAttribute('background', 'true');
    this.slot.appendChild(el);
    this.setBadge('CONNECTING', '');

    // The element takes an absolute ws:// URL.
    const wsUrl = new URL(`/video/api/ws?src=${encodeURIComponent(camera.stream)}`, location.href);
    wsUrl.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    el.src = wsUrl.toString();

    // A tile that never produces a frame is indistinguishable from a frozen
    // one, so give it a deadline and fall back rather than showing nothing.
    this.fallbackTimer = setTimeout(() => {
      const video = el.querySelector('video');
      if (!video || video.readyState < 2 || video.videoWidth === 0) {
        this.useSnapshot(camera, 'SNAPSHOT FALLBACK');
      }
    }, FALLBACK_AFTER_MS);

    const watch = setInterval(() => {
      const video = el.querySelector('video');
      if (video && video.videoWidth > 0) {
        clearTimeout(this.fallbackTimer);
        clearInterval(watch);
        this.setBadge('LIVE', 'live');
        setStatus('cam', 'ok');
      }
    }, 1000);

    this.slot.ondblclick = () => {
      if (document.fullscreenElement) document.exitFullscreen();
      else this.slot.requestFullscreen?.();
    };
  }

  useSnapshot(camera, reason) {
    this.clear();
    const img = document.createElement('img');
    // No alt text: a failed snapshot would otherwise render the browser's
    // broken-image icon and a caption across the tile, on top of the badge
    // that is already saying exactly what is wrong.
    img.alt = '';
    this.slot.appendChild(img);
    this.setBadge(reason, 'fallback');
    setStatus('cam', 'degraded', reason);

    // A tile that cannot fetch a frame must not keep asking once a second
    // forever — that is a request storm against a camera that is already in
    // trouble. Back off to a slow retry and say so.
    let failures = 0;
    let interval = SNAPSHOT_INTERVAL_MS;

    const schedule = () => {
      clearTimeout(this.snapshotTimer);
      this.snapshotTimer = setTimeout(tick, interval);
    };

    const tick = () => {
      // Without a cache-buster the browser shows the first frame forever.
      img.src = `${camera.snapshot_url}?t=${Date.now()}`;
    };

    img.onload = () => {
      img.hidden = false;
      if (failures) {
        failures = 0;
        interval = SNAPSHOT_INTERVAL_MS;
        this.setBadge(reason, 'fallback');
        setStatus('cam', 'degraded', reason);
      }
      schedule();
    };

    img.onerror = () => {
      failures += 1;
      if (failures >= 3) {
        interval = SNAPSHOT_RETRY_MS;
        img.hidden = true;                 // leave the tile empty, not broken
        this.setBadge('CAMERA DOWN', 'down');
        setStatus('cam', 'down', 'no frames from the bridge or the camera');
        // CAMERA DOWN on its own reads as a broken camera, and it usually is
        // not: the bridge said why when the inventory was fetched, and that
        // sentence is the difference between checking a mast and starting a
        // container. It goes under the badge rather than in it — the badge is
        // read from across the room, this is read by whoever walks over.
        this.explain(bridgeDetail());
      }
      schedule();
    };

    tick();
  }
}

export async function mountCameras() {
  const tile = new CameraTile(SLOT_ID, STATE_LABEL_ID);
  tile.placeholder('connecting…');

  let cameras = [];
  try {
    const resp = await api.cameras();
    cameras = resp.items || [];
    set('cameras', cameras);
    // Stored before the tile mounts, because the tile reads it on failure and
    // that happens seconds later, after the video element has given up.
    set('cameraBridge', resp.bridge || null);
  } catch (err) {
    console.error('[cameras]', err);
    setStatus('cam', 'down', String(err));
    tile.placeholder('camera service unreachable');
    return;
  }

  if (!cameras.length) {
    setStatus('cam', 'down', 'no streams configured');
    tile.placeholder('no streams configured');
    return;
  }

  // Prefer the main stream, but take whatever exists: if go2rtc is only
  // carrying the sub channel, a 360p picture beats an empty tile.
  const camera = cameras.find((c) => c.id === PREFERRED_STREAM) || cameras[0];
  tile.mount(camera);
}
