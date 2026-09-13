/* KNACKSAT telemetry, embedded from the team's own Grafana.

   Grafana sends no CORS headers, so fetching its data from the browser is
   impossible — iframes are the only route, and they work because that instance
   has anonymous access and allow_embedding turned on. If the team ever closes
   anonymous access these panels will go blank; that is the day GS_GRAFANA_TOKEN
   and a backend proxy earn their keep.

   Panel ids come from /api/config, so changing which panels are on the wall is
   an .env edit rather than a code change.
*/

import { store } from '../core/store.js';

export function mountGrafana() {
  const cfg = store.config?.grafana;
  const strip = document.getElementById('graf-strip');
  const open = document.getElementById('graf-open');
  if (!cfg || !strip) return;

  const dashboard = `${cfg.base}/d/${cfg.uid}/${cfg.slug}`;
  open.href = `${dashboard}?orgId=1&from=${cfg.range}&to=now&kiosk`;

  strip.replaceChildren();
  for (const panelId of cfg.panels) {
    const params = new URLSearchParams({
      orgId: '1',
      panelId: String(panelId),
      from: cfg.range,
      to: 'now',
      theme: 'dark',
      refresh: '1m',
    });
    // Their panels are parameterised; DS_INFLUXDB in particular selects the
    // datasource, and a panel loaded without it renders empty.
    for (const [name, value] of Object.entries(cfg.vars || {})) {
      params.set(`var-${name}`, value);
    }

    const url = new URL(`${cfg.base}/d-solo/${cfg.uid}/${cfg.slug}`);
    url.search = params.toString();

    const frame = document.createElement('iframe');
    frame.src = url.toString();
    frame.loading = 'lazy';
    frame.referrerPolicy = 'no-referrer';
    frame.title = `Grafana panel ${panelId}`;
    strip.appendChild(frame);
  }
}
