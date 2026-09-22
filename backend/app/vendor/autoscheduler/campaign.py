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
    recent_attempts: dict[int, list[tuple[datetime, datetime]]] | None = None,
) -> CampaignPreview:
    """Work out which stations should be asked to record `mission_norad`,
    and when, within the next ~48 hours.

    Read-only: this never books anything. The caller (CampaignService)
    decides whether/when to submit the resulting items.

    `recent_attempts` (station_id -> [(start, end), ...]) are windows this
    process itself already tried to book recently, treated as occupied
    alongside `network.future_bookings()`'s answer. SatNOGS's read API has
    been observed to lag several minutes behind a just-completed write, so
    the very next preview can otherwise recompute the identical "available"
    slot it just submitted - our own write history is ground truth sooner
    than their read view catches up to it.
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

    # PHASE 1 - GATHER. Every station is examined; nothing is selected yet.
    #
    # This loop used to also do the selecting, and stopped the moment the
    # booking budget was spent. Because all_stations() returns the SatNOGS
    # /api/stations/ order, which is strictly ascending by station id, and
    # nothing here sorted or rotated it, the budget was always spent on the
    # lowest ids and the tail of the network was never looked at. Measured on
    # a live run: 150 items filled at station 2937, 177 of 301 schedulable
    # stations never evaluated, West-Asia/India losing 100% of its stations.
    # Station id correlates with registration date, so the loss was
    # geographic, and identical on every run because the order never changed.
    #
    # Gathering first and selecting afterwards is what makes coverage a
    # property of the SELECTION rule rather than of the arrival order.
    work: list[_StationWork] = []
    examined = 0

    for station in stations:
        if exclude_station_id is not None and station.id == exclude_station_id:
            # This station's own local planner already controls it directly;
            # "requesting" it via the network API would be redundant.
            continue
        examined += 1

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
        for start, end in (recent_attempts or {}).get(station.id, []):
            calendar.add(start, end)

        # Conflict-free passes only. The calendar travels with the station
        # into phase 2, because selecting one pass can rule out another on the
        # same station and that has to be re-checked as we go.
        free = [p for p in sorted(gated, key=lambda p: p.aos)
                if not calendar.conflicts(p.aos, p.los)]
        if not free:
            preview.skipped.append({
                "station_id": station.id, "station_name": station.name,
                "reason": "every qualifying pass conflicts with an existing booking",
            })
            continue

        work.append(_StationWork(station=station, tx=tx, calendar=calendar, passes=free))

    # The honest number. This was len(stations), assigned before the loop, so a
    # run that stopped a third of the way through still told the operator it had
    # considered the whole network - and schedule.js renders it verbatim on the
    # confirm screen. It is now what was actually examined.
    preview.considered_stations = examined
    _select_spread(preview, work, max_per_station=max_per_station, max_total=max_total)
    return preview


# Elevation bands, high to low, expressed as their floors. A pass is in the
# first band whose floor it clears, so band 0 is 90..75 and the last is 15..0.
#
# Why band at all: the network's best passes are not evenly distributed, and a
# selection that simply takes the highest elevations first fills the whole
# budget out of one or two bands - in practice a handful of stations that
# happen to have a good geometry for this satellite in this window. Banding
# and cycling across the bands is what makes "cover the full 90..0 range"
# true of the plan rather than an accident of the window.
ELEVATION_BAND_FLOORS = (75.0, 60.0, 45.0, 30.0, 15.0, 0.0)


def _band_of(max_el: float) -> int:
    for index, floor in enumerate(ELEVATION_BAND_FLOORS):
        if max_el >= floor:
            return index
    return len(ELEVATION_BAND_FLOORS) - 1


@dataclass
class _StationWork:
    """One station that survived phase 1, carrying everything phase 2 needs.

    The calendar comes along because it is stateful: it already holds the
    station's existing SatNOGS bookings and our own recent attempts, and every
    pass we select has to be added to it so a second pick on the same station
    cannot overlap the first.
    """
    station: object
    tx: object
    calendar: Calendar
    passes: list


def _select_spread(
    preview: CampaignPreview,
    work: list[_StationWork],
    *,
    max_per_station: int,
    max_total: int,
) -> None:
    """Choose up to `max_total` passes, spread across stations and elevations.

    Two fairness rules, in priority order:

    1. STATIONS FIRST. Round r gives every eligible station its r-th booking
       before any station gets its r+1-th. That is what makes the cap buy
       coverage instead of depth: at max_total 150 and max_per_station 2, the
       old arrival-order scan reached ~75 stations, all of them the oldest ids
       on the network; one pass per station reaches ~150 before anyone gets a
       second. Same number of observations, twice the stations.

    2. ELEVATION SECOND. Within a round, each station contributes the pass
       from whichever band is currently least represented in the plan, so the
       90..0 range fills evenly instead of the plan becoming all high-elevation
       passes from well-placed stations.

    The station order is rotated per RUN, not just per round, and that detail
    is load-bearing. Rotating only by round number looks fair but is not: when
    the budget runs out during round 0 - which is exactly what happens whenever
    max_total is below the number of eligible stations - round 0 is the only
    round that ever executes, so a fixed starting point books the same prefix
    of the station list every single time. Measured with 330 stations and
    max_total 150: station ids 1..150, every run. Twice the coverage of the
    old arrival-order scan, and still positionally biased.

    So the starting point advances with the calendar hour of the run. Two
    consecutive daily runs begin at different places in the list, and over a
    few days the whole network is reached even at a cap far below its size.
    It is derived from generated_utc rather than persisted because this
    function has no state of its own, and a value the caller can reproduce
    from the preview payload is easier to reason about than a counter in a
    file that another process also writes.
    """
    if not work or max_total <= 0:
        return

    remaining = {index: list(entry.passes) for index, entry in enumerate(work)}
    band_counts = {index: 0 for index in range(len(ELEVATION_BAND_FLOORS))}

    # Hours since the epoch: advances every run in practice, and is stable
    # within one run so a preview and its commit agree.
    run_rotation = int(preview.generated_utc.timestamp() // 3600)

    for round_index in range(max(0, max_per_station)):
        if len(preview.items) >= max_total:
            break
        order = list(range(len(work)))
        if order:
            offset = (run_rotation + round_index) % len(order)
            order = order[offset:] + order[:offset]

        progressed = False
        for index in order:
            if len(preview.items) >= max_total:
                break
            entry = work[index]
            available = remaining[index]
            if not available:
                continue

            # Least-represented band first; among equals, the better pass.
            best = min(
                available,
                key=lambda p: (band_counts[_band_of(p.max_el)], -p.max_el, p.aos),
            )
            available.remove(best)
            # Re-check: an earlier pick on THIS station may now overlap it.
            if entry.calendar.conflicts(best.aos, best.los):
                continue

            preview.items.append(CampaignItem(
                station_id=entry.station.id, station_name=entry.station.name,
                transmitter_uuid=entry.tx.uuid, start=best.aos, end=best.los,
                max_elevation_deg=best.max_el,
            ))
            entry.calendar.add(best.aos, best.los)
            band_counts[_band_of(best.max_el)] += 1
            progressed = True

        if not progressed:
            # Nothing was selectable anywhere this round; further rounds cannot
            # do better, and looping on would spin to max_per_station for free.
            break

    # A station that reached phase 2 with usable passes but won nothing is a
    # budget casualty, not a conflict. Saying "every qualifying pass conflicts"
    # about it - which the old code did - is simply false, and it is the line
    # the operator reads when wondering why a station was left out.
    booked_ids = {item.station_id for item in preview.items}
    for entry in work:
        if entry.station.id not in booked_ids:
            preview.skipped.append({
                "station_id": entry.station.id, "station_name": entry.station.name,
                "reason": "had a free pass but the booking budget was spent first",
            })


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
