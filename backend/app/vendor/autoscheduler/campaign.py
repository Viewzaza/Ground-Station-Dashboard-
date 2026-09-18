"""Network campaign scheduling: request community stations to record the
mission satellite, automating the manual multi-station workflow on
network.satnogs.org's own "Schedule Observations" form.

SatNOGS Network enforces a hard ~48h scheduling horizon
(OBSERVATION_DATE_MIN_START / OBSERVATION_DATE_MAX_RANGE, confirmed against
its own source: 10 and 2890 minutes by default) and has no bulk
"schedule across many stations" API - the real form's multi-station
"Calculate" step runs behind an authenticated Django session this project
cannot and should not call into. So the per-station pass computation
happens here, reusing the same Skyfield tooling the single-station planner
already uses, and the result is submitted through the public,
token-authenticated POST /api/observations/ (a plain list) via
NetworkClient.schedule() - unchanged, no new booking mechanism.

Pass splitting is NOT replicated: SatNOGS' server splits long passes into
short segments when its own authenticated web form submits them, but that
logic also lives behind the session this project cannot reach. This module
submits one item per full pass window and lets the server split or reject
it - an honest, visible assumption rather than a guess at undocumented
behavior. If SatNOGS starts rejecting long single-window bookings, that
rejection surfaces per-item in the commit result (see ScheduleResult.errors
in network_client.py), which is the signal to revisit this.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .db_client import DbClient, pick_transmitter
from .network_client import NetworkClient
from .predictor import Predictor
from .selector import Calendar

log = logging.getLogger(__name__)

# Kept a minute inside each server-enforced edge (10 / 2890 minutes) so a
# booking is never rejected purely for landing exactly on the boundary.
WINDOW_START_MARGIN_MIN = 11
WINDOW_END_MARGIN_MIN = 2880

# The server's own minimum ("Duration of observation should be at least 180
# seconds"). A pass this short is usually one clipped by the campaign
# window's own start/end edge rather than a real full pass - submitting it
# just spends the station's per-run cap on a guaranteed rejection.
MIN_OBSERVATION_DURATION_S = 180


@dataclass
class CampaignItem:
    station_id: int
    station_name: str
    transmitter_uuid: str
    start: datetime
    end: datetime
    max_elevation_deg: float


@dataclass
class CampaignPreview:
    generated_utc: datetime
    window_start: datetime
    window_end: datetime
    considered_stations: int = 0
    items: list[CampaignItem] = field(default_factory=list)
    # [{"station_id": int|None, "station_name": str, "reason": str}]
    skipped: list[dict] = field(default_factory=list)


def build_campaign(
    network: NetworkClient,
    db: DbClient,
    mission_norad: int,
    transmitter_uuid: str | None,
    now: datetime,
    exclude_station_id: int | None,
    max_per_station: int,
    max_total: int,
) -> CampaignPreview:
    """Work out which stations should be asked to record `mission_norad`,
    and when, within the next ~48 hours.

    Read-only: this never books anything. The caller (CampaignService)
    decides whether/when to submit the resulting items.
    """
    window_start = now + timedelta(minutes=WINDOW_START_MARGIN_MIN)
    window_end = now + timedelta(minutes=WINDOW_END_MARGIN_MIN)
    preview = CampaignPreview(generated_utc=now, window_start=window_start, window_end=window_end)

    tle = db.tles().get(mission_norad)
    if tle is None:
        preview.skipped.append({
            "station_id": None, "station_name": "",
            "reason": f"SatNOGS DB has no TLE for NORAD {mission_norad}",
        })
        return preview

    stations = network.all_stations()
    preview.considered_stations = len(stations)

    for station in stations:
        if exclude_station_id is not None and station.id == exclude_station_id:
            # This station's own local planner already controls it directly;
            # "requesting" it via the network API would be redundant.
            continue

        candidates = db.transmitters_for_station(station.segments).get(mission_norad, [])
        tx = pick_transmitter(candidates, transmitter_uuid) if candidates else None
        if tx is None:
            preview.skipped.append({
                "station_id": station.id, "station_name": station.name,
                "reason": "no transmitter for this satellite is in the station's antenna range",
            })
            continue

        predictor = Predictor(station.lat, station.lng, station.altitude_m)
        stale = predictor.load_tles({mission_norad: tle}, now=now)
        if mission_norad in stale:
            preview.skipped.append({
                "station_id": station.id, "station_name": station.name,
                "reason": stale[mission_norad],
            })
            continue

        passes = predictor.passes_for(mission_norad, window_start, window_end, station.min_horizon)
        gated = [
            p for p in passes
            if p.max_el >= station.min_culmination
            and (p.los - p.aos).total_seconds() >= MIN_OBSERVATION_DURATION_S
        ]
        if not gated:
            preview.skipped.append({
                "station_id": station.id, "station_name": station.name,
                "reason": "no qualifying pass in the campaign window",
            })
            continue

        try:
            bookings = network.future_bookings(station.id, now=now)
        except Exception as exc:   # a slow/unavailable station must not abort the whole campaign
            log.warning("could not read existing bookings for station %d: %s", station.id, exc)
            bookings = []
        calendar = Calendar(buffer_s=0.0)
        for booking in bookings:
            calendar.add(booking.start, booking.end)

        booked_here = 0
        for p in sorted(gated, key=lambda p: p.aos):
            if booked_here >= max_per_station or len(preview.items) >= max_total:
                break
            if calendar.conflicts(p.aos, p.los):
                continue
            preview.items.append(CampaignItem(
                station_id=station.id, station_name=station.name,
                transmitter_uuid=tx.uuid, start=p.aos, end=p.los,
                max_elevation_deg=p.max_el,
            ))
            calendar.add(p.aos, p.los)
            booked_here += 1

        if booked_here == 0:
            preview.skipped.append({
                "station_id": station.id, "station_name": station.name,
                "reason": "every qualifying pass conflicts with an existing booking",
            })

        if len(preview.items) >= max_total:
            break

    return preview


def _campaign_preview_payload(preview: CampaignPreview) -> dict:
    """The plain-dict/JSON shape CampaignService publishes over the API and
    caches to disk - mirrors report.py's _selection_payload() for the
    single-station planner."""
    return {
        "status": "ok",
        "generated_utc": preview.generated_utc.isoformat(),
        "window_start": preview.window_start.isoformat(),
        "window_end": preview.window_end.isoformat(),
        "considered_stations": preview.considered_stations,
        "items": [
            {
                "station_id": item.station_id,
                "station_name": item.station_name,
                "transmitter_uuid": item.transmitter_uuid,
                "start": item.start.isoformat(),
                "end": item.end.isoformat(),
                "max_elevation_deg": round(item.max_elevation_deg, 1),
            }
            for item in preview.items
        ],
        "skipped": preview.skipped,
    }
