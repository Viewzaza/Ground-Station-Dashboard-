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
import { set, setStatus } from '../core/store.js';

const SLOTS = ['cam-slot-0', 'cam-slot-1'];
const STATE_LABELS = ['cam1-state', 'cam2-state'];
const FALLBACK_AFTER_MS = 15000;
const SNAPSHOT_INTERVAL_MS = 1000;
const SNAPSHOT_RETRY_MS = 15000;     // after repeated failures, stop hammering

let elementLoaded = null;

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
    this.snapshotTimer = null;
    this.fallbackTimer = null;
  }

  clear() {
    clearTimeout(this.snapshotTimer);
    clearTimeout(this.fallbackTimer);
    this.snapshotTimer = this.fallbackTimer = null;
    this.slot.replaceChildren();
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

  async mount(camera) {
    this.clear();
    if (this.label) this.label.textContent = camera.label || camera.id;

    const ok = await loadVideoStreamElement();
    if (!ok) return this.useSnapshot(camera, 'BRIDGE UNREACHABLE');

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
      }
      schedule();
    };

    tick();
  }
}

export async function mountCameras() {
  const tiles = SLOTS.map((id, i) => new CameraTile(id, STATE_LABELS[i]));
  tiles.forEach((t) => t.placeholder('connecting…'));

  let cameras = [];
  try {
    const resp = await api.cameras();
    cameras = resp.items || [];
    set('cameras', cameras);
  } catch (err) {
    console.error('[cameras]', err);
    setStatus('cam', 'down', String(err));
    tiles.forEach((t) => t.placeholder('camera service unreachable'));
    return;
  }

  if (!cameras.length) {
    setStatus('cam', 'down', 'no streams configured');
    tiles.forEach((t) => t.placeholder('no streams configured'));
    return;
  }

  cameras.slice(0, tiles.length).forEach((camera, i) => tiles[i].mount(camera));
  for (let i = cameras.length; i < tiles.length; i++) {
    tiles[i].placeholder('no second stream');
  }
}
