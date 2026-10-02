"""Network campaign scheduling: request community stations to record the
mission satellite, automating the manual multi-station workflow on
network.satnogs.org's own "Schedule Observations" form.

SatNOGS Network enforces a hard scheduling horizon of 48 hours 20 minutes,
confirmed against its own source: `check_end_datetime()` in
`network/base/validators.py` refuses any observation whose **end** is more
than OBSERVATION_DATE_MIN_START + OBSERVATION_DATE_MAX_RANGE (10 + 2890 =
2900) minutes from now, and `check_start_datetime()` refuses any **start**
inside the next 10 minutes. The network's own behaviour agrees: across the
furthest-future observations scheduled anywhere on it, none ends beyond that
edge.

Network also has no bulk "schedule across many stations" API - the real
form's multi-station "Calculate" step runs behind an authenticated Django
session this project cannot and should not call into. So the per-station pass
computation happens here, reusing the same Skyfield tooling the
single-station planner already uses, and the result is submitted through the
public, token-authenticated POST /api/observations/ (a plain list) via
NetworkClient.schedule() - unchanged, no new booking mechanism.

Reading one station's calendar costs at least one request against a read
budget, so a run over the whole catalogue is bounded by reads long before
anything else. What those budgets are, which endpoint a calendar read uses,
and what happens when a budget runs out, is in network_client.py
(RateLimitedSession, NetworkClient.future_bookings). A caller that has read a
calendar recently can hand it back in through `calendar_cache` so a second
build - a looped commit's next round - costs no reads at all.

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
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import DEFAULT_BUFFER_S
from .db_client import DbClient, pick_transmitter
from .network_client import NetworkClient, RateLimitedError
from .predictor import Predictor
from .selector import Calendar

log = logging.getLogger(__name__)

# Kept a minute inside the server's earliest-start edge
# (OBSERVATION_DATE_MIN_START, 10 minutes) so a booking is never rejected
# purely for landing exactly on the boundary.
WINDOW_START_MARGIN_MIN = 11

# The latest AOS this campaign will consider. Note that this is our own
# choice of horizon and not the server's edge - see WINDOW_HARD_END_MIN.
WINDOW_END_MARGIN_MIN = 2880

# The server's real refusal, and it applies to `end`, not to `start`:
# `check_end_datetime()` rejects anything ending more than
# OBSERVATION_DATE_MIN_START + OBSERVATION_DATE_MAX_RANGE = 10 + 2890 = 2900
# minutes from now ("End datetime should be in the future, at most 2900
# minutes from now"). That matters here because a pass rising just inside
# WINDOW_END_MARGIN_MIN may set as much as WINDOW_OVERRUN (30 minutes) later,
# i.e. at 2910 - past the edge - so the recording is trimmed to land a minute
# inside it rather than submitted as a guaranteed rejection.
WINDOW_HARD_END_MIN = 2899

# Long enough to be worth a station's time. The server's own floor is
# OBSERVATION_DURATION_MIN, which upstream defaults to 120 seconds; this is
# deliberately stricter, because a window this short is nearly always one
# clipped by a campaign-window edge rather than a real full pass, and
# submitting it just spends the station's per-run cap on a sliver of a pass.
MIN_OBSERVATION_DURATION_S = 180


@dataclass
class CampaignItem:
    station_id: int
    station_name: str
    transmitter_uuid: str
    start: datetime
    end: datetime
    max_elevation_deg: float
    transmitter_description: str = ""
    # True when this station could not hear the campaign's primary transmitter
    # and was given one of its fallbacks instead. Carried per item because the
    # two KNACKSAT-2 downlinks are not equally productive (see config.py's
    # campaign_transmitter_uuid note), so the operator has to be able to see
    # which bookings are the weaker kind before committing them.
    is_fallback: bool = False


@dataclass
class CampaignPreview:
    generated_utc: datetime
    window_start: datetime
    window_end: datetime
    considered_stations: int = 0
    items: list[CampaignItem] = field(default_factory=list)
    # [{"station_id": int|None, "station_name": str, "reason": str}]
    skipped: list[dict] = field(default_factory=list)
    # Station calendars actually fetched. Now that calendars are read lazily,
    # during selection, this is far below considered_stations on a normal run.
    calendars_read: int = 0
    # Calendars served from the caller's calendar_cache instead of a read.
    # Counted apart from calendars_read so "reads spent" stays an honest
    # number: a looped commit's later rounds are all cache hits.
    calendars_cached: int = 0
    # Stations that survived phase 1 - a campaign transmitter in antenna range
    # and at least one qualifying pass - i.e. the most this build could ever
    # have booked on. The UI used to infer it from skip-reason prefixes.
    stations_reachable: int = 0
    # The selection inputs this build actually used, so the UI can check a plan
    # against what the backend was told rather than against its own sliders.
    params: dict = field(default_factory=dict)
    # Set when SatNOGS's read limit stopped the run before every candidate
    # could be read: {"station_id", "station_name", "reason", "unread_stations"}.
    # None means the plan is complete. The operator must be told which, because
    # a truncated plan looks exactly like a small network otherwise.
    stopped_early: dict | None = None


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
    buffer_s: float = DEFAULT_BUFFER_S,
    booked_counts: dict[int, int] | None = None,
    fallback_transmitter_uuids: list[str] | None = None,
    calendar_cache: dict[int, list] | None = None,
    cap_counts_existing: bool = True,
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

    `booked_counts` (station_id -> n) is how many bookings each station has
    already been given earlier in the same looped commit (see
    CampaignService._commit_sync). They count against `max_per_station`, so
    looping round after round fills more stations rather than stacking more
    passes onto the ones the first round already reached.

    `fallback_transmitter_uuids` only means something alongside a
    `transmitter_uuid`: each station records the FIRST of
    [transmitter_uuid, *fallback_transmitter_uuids] it can hear, so the
    primary wins wherever both are audible. With no transmitter_uuid the
    fallbacks are ignored and pick_transmitter chooses, as before.

    `calendar_cache` (station_id -> bookings) is consulted before reading a
    station's calendar and filled by every successful read. Whether an entry
    is still fresh enough to trust is the CALLER's decision - this function
    has no clock of its own to judge it by.

    `cap_counts_existing` makes max_per_station mean "observations of this
    satellite on the station within the window", counting what its calendar
    already holds, rather than "added by this build". See load_calendar.
    """
    window_start = now + timedelta(minutes=WINDOW_START_MARGIN_MIN)
    window_end = now + timedelta(minutes=WINDOW_END_MARGIN_MIN)
    hard_end = now + timedelta(minutes=WINDOW_HARD_END_MIN)

    # The order a station tries transmitters in. Duplicates, and the primary
    # repeated as its own fallback, are dropped so "has fallbacks" below means
    # a genuinely different downlink is on offer.
    tx_order: list[str] = []
    if transmitter_uuid:
        for uuid in [transmitter_uuid, *(fallback_transmitter_uuids or [])]:
            if uuid and uuid not in tx_order:
                tx_order.append(uuid)

    preview = CampaignPreview(
        generated_utc=now, window_start=window_start, window_end=window_end,
        params={
            "max_per_station": max_per_station,
            "max_total": max_total,
            "transmitter_uuid": transmitter_uuid,
            # What was actually applied, not what was passed: fallbacks are
            # meaningless without a primary, so they are reported as none.
            "fallback_transmitter_uuids": tx_order[1:],
            "cap_counts_existing": cap_counts_existing,
        },
    )

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

        # Checked before any pass prediction or calendar read: on a looped
        # commit most stations hit their cap within a round or two, and
        # re-reading all their calendars every round is what gets 429s.
        allowance = max_per_station - (booked_counts or {}).get(station.id, 0)
        if allowance <= 0:
            preview.skipped.append({
                "station_id": station.id, "station_name": station.name,
                "reason": "already reached its per-station cap in this campaign",
            })
            continue

        candidates = db.transmitters_for_station(station.segments).get(mission_norad, [])
        if tx_order:
            # The campaign's transmitters are a hard, ORDERED filter here, not
            # the single preference pick_transmitter treats a uuid as. A
            # station that hears none of them has nothing to contribute, and
            # pick_transmitter's own fallback would quietly book whatever else
            # it can hear - a downlink nobody chose. Among the listed ones the
            # first audible wins, so on the 68 stations that hear both
            # KNACKSAT-2 downlinks the telemetry is recorded, not the
            # digipeater that DB order would otherwise hand pick_transmitter.
            #
            # pick_transmitter is deliberately not called with a uuid the
            # station lacks: it logs a "priority file pins transmitter"
            # warning for that, which is false here and would repeat for
            # every fallback station (~77) on every build.
            audible = {}
            for candidate in candidates:
                audible.setdefault(candidate.uuid, candidate)
            tx = next((audible[uuid] for uuid in tx_order if uuid in audible), None)
            if tx is None:
                preview.skipped.append({
                    "station_id": station.id, "station_name": station.name,
                    "reason": (
                        "none of the campaign's transmitters is in the station's antenna range"
                        if len(tx_order) > 1 else
                        "the pinned transmitter is not in the station's antenna range"
                    ),
                })
                continue
        else:
            tx = pick_transmitter(candidates, None) if candidates else None
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

        # `passes_for` works from the station's own published position and
        # `min_horizon`, so a satellite that never clears this station's
        # horizon yields nothing here and the station is skipped below. The
        # culmination gate is the station's own published figure too.
        passes = predictor.passes_for(mission_norad, window_start, window_end, station.min_horizon)
        gated = []
        for p in passes:
            # Trimmed rather than dropped: the part of a window-edge pass that
            # falls inside the server's hard end is still worth recording, and
            # the duration gate below then judges what is actually left.
            end = min(p.los, hard_end)
            if p.max_el < station.min_culmination:
                continue
            if (end - p.aos).total_seconds() < MIN_OBSERVATION_DURATION_S:
                continue
            gated.append((p, end))
        if not gated:
            preview.skipped.append({
                "station_id": station.id, "station_name": station.name,
                "reason": "no qualifying pass in the campaign window",
            })
            continue

        # No calendar read here. Phase 1 is pure geometry - Skyfield on this
        # machine, no network - so it can afford to look at every station.
        # Reading each station's calendar here is what used to spend the
        # whole SatNOGS read budget (240/hour with a token, against ~300
        # schedulable stations) on every run: the loop hit the limit, broke,
        # and because stations arrive in ascending id order the part that fit
        # inside the budget was always the network's oldest corner. The
        # per-run shuffle in phase 2 then only reordered that prefix, and the
        # geographic bias this split exists to remove came straight back.
        # Calendars are now read in phase 2, lazily, in shuffled order, and
        # only for stations about to be picked.
        work.append(_StationWork(
            station=station, tx=tx, calendar=None,
            passes=sorted(gated, key=lambda pair: pair[0].aos),
            allowance=allowance,
            booked_earlier=(booked_counts or {}).get(station.id, 0),
            # "Fallback" is relative to a chosen primary. With none (the auto
            # path) there is nothing to fall back FROM, so nothing is one.
            is_fallback=bool(transmitter_uuid) and tx.uuid != transmitter_uuid,
        ))

    # Every non-excluded station had its geometry examined, so this is now
    # both the honest number and the whole network.
    preview.considered_stations = examined
    preview.stations_reachable = len(work)

    def load_calendar(entry: "_StationWork") -> None:
        """Fetch one station's calendar and drop the passes it rules out.

        RateLimitedError is allowed to escape: a throttle is not this one
        station's problem, the budget is spent for every station still to
        come, and _select_spread stops reading on it. Any OTHER read failure
        raises _CalendarUnreadable: that one station is left out and the run
        carries on with everyone else.
        """
        station_id = entry.station.id
        if calendar_cache is not None and station_id in calendar_cache:
            # A hit costs no request at all. That is the whole point: a looped
            # commit rebuilds the plan every round, and re-reading every
            # calendar each time is what used to spend the read budget - the
            # loop, not any single build, was what ran it out. What we booked
            # ourselves since the cached read is not missing from the picture:
            # recent_attempts is overlaid below on every build.
            bookings = calendar_cache[station_id]
            preview.calendars_cached += 1
        else:
            try:
                bookings = network.future_bookings(station_id, now=now)
            except RateLimitedError:
                raise
            except Exception as exc:   # a slow/unavailable station must not abort the whole campaign
                # Fail CLOSED. A station whose existing schedule we cannot read is a
                # station we must not schedule on: reading it as an empty calendar
                # makes every pass look free and books blind over whatever it had
                # already agreed to record. Observed in the wild: serial reads, an
                # HTTP 429 part way through, and 86 stations in one run went down
                # that empty-calendar path at once. Calendars are read lazily now,
                # so this raises rather than `continue`s; _select_spread marks the
                # station unreadable and gives it this reason. Nothing is cached
                # for it either, so the next build asks again.
                log.warning("could not read existing bookings for station %d: %s",
                            station_id, exc)
                raise _CalendarUnreadable(str(exc)) from exc
            preview.calendars_read += 1
            if calendar_cache is not None:
                calendar_cache[station_id] = list(bookings)
        # buffer_s was hardcoded to 0.0 here, which threw away the rotator-reset
        # margin (DEFAULT_BUFFER_S, 30 s) that the single-station planner honours
        # via selector.select(). Two observations that merely touch are not
        # physically schedulable - the rotator has to slew back.
        calendar = Calendar(buffer_s=buffer_s)
        for booking in bookings:
            calendar.add(booking.start, booking.end)
        if cap_counts_existing:
            # max_per_station is "observations of this satellite per station
            # in the 48 h window", not "per click". Counting only what THIS
            # build adds meant every second click, and every auto-timer run,
            # stacked another full max_per_station onto each station on top of
            # what the last one had booked; counting what is already there
            # makes a repeat run top up instead. Only this satellite counts -
            # the owner's other work is a conflict, not a share of our cap -
            # and only inside [window_start, hard_end], because a cached
            # calendar can still list observations that have since ended.
            #
            # recent_attempts is NOT counted: it holds everything we tried,
            # refusals included, so counting it would under-book on the word
            # of our own failed requests. It stays a conflict overlay only.
            # booked_counts (a looped commit's earlier rounds) is already
            # subtracted from the allowance in phase 1; with a calendar cache
            # read before the commit's first round the two are disjoint. A
            # fresh re-read mid-loop could see round 1's bookings in both
            # places - that errs towards booking fewer, never more.
            entry.existing_mission = sum(
                1 for booking in bookings
                if booking.norad_cat_id == mission_norad
                and booking.start < hard_end and booking.end > window_start
            )
            entry.allowance -= entry.existing_mission
        for start, end in (recent_attempts or {}).get(station_id, []):
            calendar.add(start, end)
        entry.calendar = calendar
        # Carried WITH the trimmed end, never p.los: see VENDORED.md note 13.
        entry.passes = [(p, end) for p, end in entry.passes
                        if not calendar.conflicts(p.aos, end)]

    _select_spread(preview, work, max_per_station=max_per_station,
                   max_total=max_total, load_calendar=load_calendar)
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

# How far below its OWN best pass a station may be pushed to flatten the band
# histogram. Without a bound, band balancing is unbounded in what it will
# spend: the band term is the primary sort key and is global, so a station
# reached late in a round gets forced into whatever band is currently thinnest
# however bad the pass. Since the last band floor is 0.0 and a station's
# min_culmination defaults to 0.0, that legitimately selects grazing passes -
# a station with an 88 degree pass and a 3.7 degree pass in the same window
# was booking the 3.7. When the budget runs out in round 0, which is the norm
# whenever eligible stations >= max_total, that forced pick is the station's
# entire contribution.
#
# 20 degrees is a real tolerance rather than a round number: it is wide enough
# that the histogram still flattens across a network whose stations have very
# different best-available geometry, and narrow enough that no station ever
# trades a good pass for a horizon-scraper.
#
# It binds a station's FIRST pick in a build only. The bound exists so that a
# station's only booking is never a grazer; once the station has its good
# pass, holding its 2nd and 3rd picks to the same bound is what starved the
# low bands, against the operator's stated aim of an even spread over 90..0.
# Measured offline on a cached copy of the live catalogue (telemetry first,
# digipeater fallback, 3 per station, 600 bookings, empty calendars): the pool
# held 166 passes in the 15..0 band but only 11 were within 20 degrees of
# their own station's best, so with every pick bounded the plan's bands came
# out 166/144/106/87/72/25, high to low. Bounding only the first pick, the
# same pool and timestamp give 101/100/103/100/100/96.
MAX_ELEVATION_SACRIFICE_DEG = 20.0


def _band_of(max_el: float) -> int:
    for index, floor in enumerate(ELEVATION_BAND_FLOORS):
        if max_el >= floor:
            return index
    return len(ELEVATION_BAND_FLOORS) - 1


# "90-75", "75-60", ... "15-0": one label per ELEVATION_BAND_FLOORS entry, the
# band's ceiling (the previous floor, or zenith) down to its floor. Derived
# rather than written out so the labels cannot drift from the bands.
ELEVATION_BAND_LABELS = tuple(
    f"{ceiling:g}-{floor:g}"
    for ceiling, floor in zip((90.0, *ELEVATION_BAND_FLOORS[:-1]), ELEVATION_BAND_FLOORS)
)


def band_counts_payload(elevations) -> list[dict]:
    """[{"band": "90-75", "count": n}, ...] over max elevations, every band
    listed high to low even when empty, so a thin band shows as a zero rather
    than disappearing. Public so the commit result can label accepted bookings
    with exactly the bands the preview used."""
    counts = [0] * len(ELEVATION_BAND_FLOORS)
    for max_el in elevations:
        counts[_band_of(float(max_el))] += 1
    return [{"band": label, "count": count}
            for label, count in zip(ELEVATION_BAND_LABELS, counts)]


class _CalendarUnreadable(Exception):
    """One station's calendar could not be read (and it was not a throttle)."""


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
    # (Pass, end) pairs, where end is the pass's LOS trimmed to the server's
    # hard booking edge. Always book `end`, never `pass.los`.
    passes: list
    # How many more bookings this station may take in THIS build: the per-station
    # cap minus what a looped commit has already given it in earlier rounds
    # (booked_counts) and, once its calendar is loaded with cap_counts_existing,
    # minus the observations of this satellite already on it in the window.
    # Selection never gives a station more than this.
    allowance: int
    # booked_counts for this station, kept only to explain a capped station.
    booked_earlier: int = 0
    # Observations of this satellite its calendar already held in the window;
    # set by load_calendar when cap_counts_existing is on.
    existing_mission: int = 0
    # Its transmitter is a fallback rather than the campaign's primary.
    is_fallback: bool = False


def _select_spread(
    preview: CampaignPreview,
    work: list[_StationWork],
    *,
    max_per_station: int,
    max_total: int,
    load_calendar=None,
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
       passes from well-placed stations. A station's first pick is held within
       MAX_ELEVATION_SACRIFICE_DEG of its own best; later picks are not.

    The station order is rotated per RUN, not just per round, and that detail
    is load-bearing. Rotating only by round number looks fair but is not: when
    the budget runs out during round 0 - which is exactly what happens whenever
    max_total is below the number of eligible stations - round 0 is the only
    round that ever executes, so a fixed starting point books the same prefix
    of the station list every single time. Measured with 330 stations and
    max_total 150: station ids 1..150, every run. Twice the coverage of the
    old arrival-order scan, and still positionally biased.

    So the starting point is shuffled per run, seeded from the run's own
    timestamp in SECONDS. The seed is derived from generated_utc rather than
    persisted because this function has no state of its own, and a value the
    caller can reproduce from the preview payload beats a counter in a file
    that another process also writes.

    Seconds, and a shuffle rather than an offset, both matter. An earlier
    version used an offset of the hour index, which looks equivalent and is
    not: the campaign timer is campaign_poll_s = 86400, so the hour index
    advances by 24 between consecutive runs, and the reachable offsets are
    only the coset generated by 24 in Z_N - N/gcd(N,24) of them. For any N
    dividing 24 the offset never moves at all, and with 24 eligible stations
    and a cap of 10 the same ten were booked every day forever. Minutes would
    not have fixed it either; gcd(1440, N) is degenerate for the same N. A
    seconds-seeded shuffle has no such structure.
    """
    if not work or max_total <= 0:
        return

    # Calendars are read lazily: `remaining` gains a station only once its
    # calendar has been loaded and its conflicting passes removed. That keeps
    # reads close to the number of stations actually visited - roughly the
    # number booked, plus any found already at their cap - instead of one per
    # station on the network.
    remaining: dict[int, list] = {}
    unusable: set[int] = set()          # read, and every pass conflicted
    unreadable: dict[int, str] = {}     # read attempted and failed (not a throttle)
    capped: set[int] = set()            # read, and its calendar already fills its cap
    picks: dict[int, int] = {}          # bookings given to each station in this build
    band_counts = {index: 0 for index in range(len(ELEVATION_BAND_FLOORS))}
    stop_reading = False

    # Seeded from the run's own second, so two runs a day apart - or a minute
    # apart - always start somewhere different, whatever len(work) happens to
    # be. Stable within one run, so a preview and its commit agree. Because
    # calendars are read in THIS order, a run cut short by the read limit has
    # read a random sample of the network rather than its lowest ids.
    shuffler = random.Random(int(preview.generated_utc.timestamp()))
    base_order = list(range(len(work)))
    shuffler.shuffle(base_order)

    def ready(index: int) -> bool:
        """Load this station's calendar on first visit; is it selectable?"""
        nonlocal stop_reading
        if index in remaining:
            return True
        if index in unusable or index in unreadable or index in capped or stop_reading:
            return False
        entry = work[index]
        if load_calendar is not None:
            try:
                load_calendar(entry)
            except RateLimitedError as exc:
                # Stop READING, not selecting: stations already loaded are
                # still perfectly good candidates, and dropping them would turn
                # a throttle into an empty plan.
                stop_reading = True
                preview.stopped_early = {
                    "station_id": entry.station.id,
                    "station_name": entry.station.name,
                    "reason": f"SatNOGS is rate-limiting reads ({exc})",
                }
                log.warning("campaign stopped reading calendars at station %d: %s",
                            entry.station.id, exc)
                return False
            except _CalendarUnreadable as exc:
                unreadable[index] = str(exc)
                return False
            # calendars_read / calendars_cached are counted by the loader,
            # which is the only thing that knows whether a request was sent.
        if entry.calendar is None:
            # No loader means nothing is known to be booked there yet.
            entry.calendar = Calendar(buffer_s=0.0)
        if entry.allowance <= 0:
            # Its calendar already holds its whole share of this satellite
            # (cap_counts_existing). Checked before the passes: a station at
            # its cap is at its cap whether or not a pass is still free.
            capped.add(index)
            return False
        if not entry.passes:
            unusable.add(index)
            return False
        remaining[index] = list(entry.passes)
        return True

    for round_index in range(max(0, max_per_station)):
        if len(preview.items) >= max_total:
            break
        order = list(base_order)
        if order:
            offset = round_index % len(order)
            order = order[offset:] + order[:offset]

        progressed = False
        for index in order:
            if len(preview.items) >= max_total:
                break
            if picks.get(index, 0) >= work[index].allowance:
                # A looped commit already gave this station bookings in earlier
                # rounds; the per-station cap counts those too.
                continue
            if not ready(index):
                continue
            entry = work[index]
            available = remaining[index]
            if not available:
                continue

            # Least-represented band first. For the station's first pick in
            # this build, only among passes within MAX_ELEVATION_SACRIFICE_DEG
            # of its own best: that is what stops band balancing paying an
            # unbounded price in elevation for the one booking a station may
            # end up with. Its later picks may fill whatever band is thinnest;
            # see the constant's note.
            if picks.get(index, 0) == 0:
                best_el = max(p.max_el for p, _end in available)
                worth_having = [
                    pair for pair in available
                    if pair[0].max_el >= best_el - MAX_ELEVATION_SACRIFICE_DEG
                ]
            else:
                worth_having = available
            chosen = min(
                worth_having,
                key=lambda pair: (band_counts[_band_of(pair[0].max_el)],
                                  -pair[0].max_el, pair[0].aos),
            )
            available.remove(chosen)
            best, end = chosen
            # Re-check: an earlier pick on THIS station may now overlap it.
            if entry.calendar.conflicts(best.aos, end):
                continue

            preview.items.append(CampaignItem(
                station_id=entry.station.id, station_name=entry.station.name,
                transmitter_uuid=entry.tx.uuid, start=best.aos, end=end,
                max_elevation_deg=best.max_el,
                transmitter_description=getattr(entry.tx, "description", "") or "",
                is_fallback=entry.is_fallback,
            ))
            entry.calendar.add(best.aos, end)
            picks[index] = picks.get(index, 0) + 1
            band_counts[_band_of(best.max_el)] += 1
            progressed = True

        if not progressed:
            # Nothing was selectable anywhere this round; further rounds cannot
            # do better, and looping on would spin to max_per_station for free.
            break

    # Fairness decides WHICH passes are chosen; it has no business deciding the
    # order they are shown in. Selection emits them in shuffled-station,
    # round-by-round order, and schedule.js renders preview.items verbatim with
    # no sort of its own - into a table that deliberately lists every row so
    # nothing that gets booked goes unseen. Sorting here, once, restores a
    # table a person can check. It cannot change what is booked.
    preview.items.sort(key=lambda item: (item.station_id, item.start))

    # Account for every station that won nothing, with the reason that is
    # actually true of it. Four different things used to share one message.
    if preview.stopped_early is not None:
        preview.stopped_early["unread_stations"] = sum(
            1 for index in range(len(work))
            if index not in remaining and index not in unusable
            and index not in unreadable and index not in capped
        )
    booked_ids = {item.station_id for item in preview.items}
    for index, entry in enumerate(work):
        if entry.station.id in booked_ids:
            continue
        # No "had a free pass but lost on budget" case any more. A calendar is
        # loaded only for a station about to be picked, its passes are already
        # filtered against that calendar, so its first pick cannot conflict -
        # every loaded station with allowance left books at least once. Budget
        # casualties are therefore all stations that were never read.
        # (Mutation testing found the old branch unreachable: rewording it
        # changed nothing any test, or any run, could observe.)
        if index in unreadable:
            reason = f"could not read this station's existing bookings ({unreadable[index]})"
        elif index in capped:
            reason = (f"already has {entry.existing_mission} observation(s) of this satellite "
                      f"booked in the window (per-station cap {max_per_station})")
            if entry.booked_earlier:
                reason += f", plus {entry.booked_earlier} from earlier rounds of this commit"
        elif index in unusable:
            reason = "every qualifying pass conflicts with an existing booking"
        elif preview.stopped_early is not None:
            reason = "not read - SatNOGS's read limit was reached before this station"
        else:
            reason = "not reached - the booking budget was spent before this station was read"
        preview.skipped.append({
            "station_id": entry.station.id, "station_name": entry.station.name,
            "reason": reason,
        })


def _transmitters_payload(items: list[CampaignItem], primary: str | None,
                          fallbacks: list[str]) -> list[dict]:
    """Per-transmitter station and booking counts over the plan, primary
    first, then the fallbacks in the order they were offered - the order the
    operator reads "how much of this is the weaker downlink" in. Anything
    else (the auto path) follows, busiest first."""
    rows: dict[str, dict] = {}
    stations: dict[str, set] = {}
    for item in items:
        row = rows.setdefault(item.transmitter_uuid, {
            "uuid": item.transmitter_uuid,
            "description": item.transmitter_description,
            "fallback": item.is_fallback,
            "stations": 0,
            "bookings": 0,
        })
        row["bookings"] += 1
        stations.setdefault(item.transmitter_uuid, set()).add(item.station_id)
    for uuid, row in rows.items():
        row["stations"] = len(stations[uuid])
    order = [uuid for uuid in [primary, *fallbacks] if uuid]
    return sorted(rows.values(), key=lambda row: (
        order.index(row["uuid"]) if row["uuid"] in order else len(order),
        -row["bookings"], row["uuid"],
    ))


def _campaign_preview_payload(preview: CampaignPreview) -> dict:
    """The plain-dict/JSON shape CampaignService publishes over the API and
    caches to disk - mirrors report.py's _selection_payload() for the
    single-station planner."""
    params = preview.params or {}
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
                "transmitter_description": item.transmitter_description,
                "fallback": item.is_fallback,
                "start": item.start.isoformat(),
                "end": item.end.isoformat(),
                "max_elevation_deg": round(item.max_elevation_deg, 1),
            }
            for item in preview.items
        ],
        "skipped": preview.skipped,
        "calendars_read": preview.calendars_read,
        "calendars_cached": preview.calendars_cached,
        "stations_reachable": preview.stations_reachable,
        "stations_booked": len({item.station_id for item in preview.items}),
        # From the elevations exactly as the rows above publish them (rounded),
        # because the commit result can only band its accepted rows from those
        # published values. Banding the unrounded ones here would let a 74.96
        # pass count as 75-60 in the preview and 90-75 once accepted.
        "band_counts": band_counts_payload(
            round(item.max_elevation_deg, 1) for item in preview.items
        ),
        "transmitters": _transmitters_payload(
            preview.items, params.get("transmitter_uuid"),
            list(params.get("fallback_transmitter_uuids") or []),
        ),
        "params": dict(params),
        # None when the plan is complete. Anything else means the run was cut
        # short and the plan covers only part of the network - the UI must say
        # so rather than let a truncated plan pass for a small network.
        "stopped_early": preview.stopped_early,
    }
