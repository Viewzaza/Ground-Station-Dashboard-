"""A stub SatNOGS DB + SatNOGS Network for capturing a real auto-scheduler transcript.

This is NOT a SatNOGS emulator. It serves exactly the seven requests that
`auto_scheduler.satnogs_client` makes, with exactly the JSON fields that module
reads, and nothing else. Anything else is answered 404 and logged, so a missing
endpoint shows up as a line in the request log rather than as a mystery.

Two servers, not one, because SatNOGS DB and SatNOGS Network both expose
`/api/transmitters` and the two payloads are completely different shapes: DB
returns transmitter records (`downlink_low`, `status`, `mode`), Network returns
observation statistics (`uuid`, `stats`). Upstream tells them apart only by
which base URL it used, so the stub has to as well.

Payload provenance, in full:

  * satellites, transmitter modes/NORAD ids, transmitter statistics and TLEs
    come verbatim from upstream's own test fixtures, checked out from
    satnogs-auto-scheduler at 0.5.dev17+g0f7ec0177 and copied unmodified into
    ./upstream_fixtures/.
  * `downlink_low` and `status` on the DB transmitter records are SYNTHESISED
    here, because upstream's `transmitters_receivable.json` fixture is the
    *output* of `get_active_transmitter_info()` and therefore no longer carries
    the two fields that function filters on. See `_db_transmitters`.
  * the ground station record and the existing-observations list are
    SYNTHESISED here. They describe no real station and no real booking.

Nothing in this file talks to the internet, and the capture runs the container
with `--network=none`, so it cannot.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------
# The synthesised half. Everything invented rather than taken from a fixture
# lives here, in one block, so a reader can see the whole of it at once.
# --------------------------------------------------------------------------

STATION_ID = 5024

# A plausible UHF station. The coordinates are a placeholder; this record does
# not describe the real station 5024 or any other real station.
STATION = {
    "id": STATION_ID,
    "name": "stub-station",
    "status": "Online",
    "lat": 52.0,
    "lng": 4.35,
    "altitude": 10,
    "min_horizon": 5.0,
    "horizon_hard_limit": False,
    "min_culmination": 0.0,
    "min_culmination_hard_limit": False,
    # Copied from upstream's tests/test_cache.py GROUND_STATION_ANTENNA.
    "antenna": [
        {
            "frequency": 430000000,
            "frequency_max": 470000000,
            "band": "UHF",
            "antenna_type": "cross-yagi",
            "antenna_type_name": "Cross Yagi",
        }
    ],
}

# Existing observations, newest first, which is the order the real
# /api/jobs/ endpoint returns them in and which upstream's
# `stop_criterion_callback` depends on.
#
# Chosen to make the already-scheduled ('Sch' == 'Y') branch of
# print_scheduledpass_summary produce every shape it can:
#   60133 - not in the satellites catalogue     -> empty Satellite name
#   48274 - 'CSS (Tianhe)'                      -> name with a space and parens
#   43768 - is_frequency_violator == true       -> 'Freq' column 'Y'
#   25544 - ordinary, and ends on a :30 second  -> duration is not a whole minute
# The last entry is deliberately before the window so that it is filtered out
# and trips the stop criterion.
JOBS = [
    {
        "id": 900004,
        "start": "2023-02-19T06:15:00Z",
        "end": "2023-02-19T06:24:30Z",
        "ground_station": STATION_ID,
        "transmitter": "ZZstub0000NotARealUUID",
        "norad_cat_id": 60133,
    },
    {
        "id": 900003,
        "start": "2023-02-19T02:30:00Z",
        "end": "2023-02-19T02:41:00Z",
        "ground_station": STATION_ID,
        "transmitter": "SYNaLxjDbU2XNih337XsEs",
        "norad_cat_id": 48274,
    },
    {
        "id": 900002,
        "start": "2023-02-18T18:42:11Z",
        "end": "2023-02-18T18:53:47Z",
        "ground_station": STATION_ID,
        "transmitter": "XJsHG6GRFqrBrmP3uvF93E",
        "norad_cat_id": 43768,
    },
    {
        "id": 900001,
        "start": "2023-02-18T11:05:00Z",
        "end": "2023-02-18T11:17:30Z",
        "ground_station": STATION_ID,
        "transmitter": "eozSf5mKyzNxoascs8V4bV",
        "norad_cat_id": 25544,
    },
    {
        "id": 900000,
        "start": "2023-02-18T07:00:00Z",
        "end": "2023-02-18T07:10:00Z",
        "ground_station": STATION_ID,
        "transmitter": "eozSf5mKyzNxoascs8V4bV",
        "norad_cat_id": 25544,
    },
]

# Every entry in upstream's transmitters_receivable.json fixture was produced by
# filtering the real DB with an antenna covering 430-470 MHz, so re-deriving a
# downlink_low anywhere inside that band reproduces the filter's outcome
# exactly. The exact value is invented; only the fact that it lands in the band
# is meaningful.
_DOWNLINK_BASE_HZ = 435_000_000
_DOWNLINK_SPREAD_HZ = 3_000_000

PAGE_SIZE = 250


def _db_transmitters(receivable: dict) -> list[dict]:
    """Rebuild DB /api/transmitters records from upstream's filtered fixture.

    Returns only the five fields `get_active_transmitter_info` actually reads.
    Omitting the rest is deliberate: a field the stub does not serve is a field
    the tool provably does not need, which is information worth keeping.
    """
    out = []
    for index, (uuid, entry) in enumerate(sorted(receivable.items())):
        out.append(
            {
                "uuid": uuid,
                "norad_cat_id": entry["norad_cat_id"],
                "mode": entry["mode"],
                # SYNTHESISED, see module docstring.
                "status": "active",
                "downlink_low": _DOWNLINK_BASE_HZ + (index * 1000) % _DOWNLINK_SPREAD_HZ,
            }
        )
    return out


def _network_transmitter_stats(stats: dict) -> list[dict]:
    """Rebuild Network /api/transmitters/ records from upstream's fixture.

    The fixture is `{uuid: stats}` because that is how CacheManager stores it;
    the API returns a list of objects carrying a nested `stats`.
    """
    return [{"uuid": uuid, "stats": value} for uuid, value in sorted(stats.items())]


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------


class _Stub(BaseHTTPRequestHandler):
    # Set by make_server
    payloads: dict = {}
    label: str = ""
    request_log = None
    port: int = 0

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D102 - silence the default stderr spam
        pass

    def _record(self, method: str, path: str, status: int) -> None:
        auth = self.headers.get("Authorization", "")
        if auth:
            # Never echo a token, even a fake one.
            auth = f"Token <{len(auth.split()[-1])} chars>"
        line = f"{self.label:7s} {method:4s} {status} {path}  auth={auth or '-'}"
        if self.request_log is not None:
            self.request_log.write(line + "\n")
            self.request_log.flush()

    def _send(self, status: int, body: bytes, link: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if link:
            self.send_header("Link", link)
        self.end_headers()
        self.wfile.write(body)

    def _paginate(self, items: list, path: str, query: dict):
        """Serve one page and advertise the next with a real Link header.

        Upstream loops `while "next" in response.links`, which is requests'
        parse of exactly this header, so paginating for real is the only way to
        know that loop is exercised.
        """
        page = int(query.get("page", ["1"])[0])
        start = (page - 1) * PAGE_SIZE
        chunk = items[start : start + PAGE_SIZE]
        link = ""
        if start + PAGE_SIZE < len(items):
            nxt = f"http://127.0.0.1:{self.port}{path}?page={page + 1}"
            link = f'<{nxt}>; rel="next"'
        return json.dumps(chunk).encode(), link

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in self.payloads:
            body, link = self._paginate(self.payloads[path], path, query)
            self._record("GET", self.path, 200)
            self._send(200, body, link)
            return

        if path == f"/api/stations/{STATION_ID}":
            self._record("GET", self.path, 200)
            self._send(200, json.dumps(STATION).encode())
            return

        self._record("GET", self.path, 404)
        self._send(404, json.dumps({"detail": "Not found."}).encode())

    def do_POST(self):  # noqa: N802
        # A dry run must never reach this. If it ever does, the request log says
        # so in capitals and the harness fails the capture.
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._record("POST", "!!! BOOKING ATTEMPT !!! " + self.path, 403)
        self._send(403, json.dumps({"detail": "stub refuses to book"}).encode())


def make_server(label: str, port: int, payloads: dict, request_log) -> ThreadingHTTPServer:
    handler = type(
        f"_Stub{label}",
        (_Stub,),
        {"payloads": payloads, "label": label, "request_log": request_log, "port": port},
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def build_payloads(fixtures_dir: str):
    """Load upstream's fixtures and shape them back into API responses."""

    def load(name):
        with open(f"{fixtures_dir}/{name}") as handle:
            return json.load(handle)

    satellites = load("satellites.json")
    receivable = load("transmitters_receivable.json")
    stats = load("transmitters_stats.json")
    tles = load("tles.json")

    db_payloads = {
        # CacheManager -> get_satellite_info
        "/api/satellites": list(satellites.values()),
        # CacheManager -> get_active_transmitter_info
        "/api/transmitters": _db_transmitters(receivable),
        # CacheManager -> get_tles (sends the DB token)
        "/api/tle/": tles,
    }
    network_payloads = {
        # CacheManager -> get_transmitter_stats
        "/api/transmitters/": _network_transmitter_stats(stats),
        # schedule_single_station -> get_scheduled_passes_from_network
        "/api/jobs/": JOBS,
    }
    return db_payloads, network_payloads
