"""Who has the antenna, what it is doing, and which satellite it is working.

The wall shows a satellite, a pass and a pointing error, and until this nothing
on it said *whose* they were. Station 5024's antenna can be driven by four
different things — satnogs-client recording a SatNOGS job, autopilot working
the plan, an operator at the control panel, or nothing at all — and from across
the room they look identical: a triangle moving on the polar plot does not say
who is moving it. Meanwhile the display could be on a different satellite
altogether. SatNOGS records ISS while the wall shows KNACKSAT-2's arc and an
ERR of '—', and the only hint is "observation in progress" in the footer.

This module answers both questions, for display only. It is pure: it reads
attributes off the services it is handed and calls nothing that can command
the rotator. And nothing that *decides* reads what it produces —
ControlService's gates, its track loop and PlanExecutor never import it, and a
test pins that for control.py and planner_service.py. Ownership here is a
sentence for a person, never an input to the interlock, which asks SatNOGS for
itself on every command.

Two rules carried over from the interlock, because a display can be wrong in
the same ways a gate can:

**Unknown is not idle.** If SatNOGS has not answered, or its answer is older
than GS_GATE_MAX_STALE_S, the owner is "unknown". "Idle" on a wall reads as
"nobody is using the antenna", and a stale all-clear is exactly the window in
which satnogs-client may have picked up a job.

**An unarmed goto is not ownership.** A goto issued an hour ago leaves the
control mode "manual" indefinitely. Once the lease is gone nobody is driving on
the strength of it, so it does not make anyone the owner.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .planner import _separation

log = logging.getLogger(__name__)

# A job's elements are kept this long past its end: a recording that overruns,
# or a clock a few seconds from SatNOGS's, still finds them.
TLE_KEEP_AFTER_END = timedelta(minutes=10)
# A SatNOGS job this close is where the antenna is about to go, so it is what
# the display should already be showing.
FOCUS_SOON = timedelta(minutes=10)
# Fields that change without anything having happened. They are published, but
# a change in them alone is not worth a frame.
VOLATILE = ("updated_at", "satnogs_age_s")


def beam_error_deg(az1: float, el1: float, az2: float, el2: float) -> float:
    """How far apart two pointing directions are: the great-circle angle.

    Not sqrt(Δaz² + Δel²). Lines of azimuth converge towards zenith, so at 85°
    elevation a 40° azimuth difference is a beam about 3.5° off — and the
    readout used to say 40. The formula is the planner's own, so the ERR an
    operator reads and the keyhole lag the planner derates by are one measure.
    """
    return _separation(az1, el1, az2, el2)


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _clean_name(raw: Any) -> str:
    # SatNOGS and Celestrak both prefix line 0 with "0 " in places; the TLE
    # store strips it the same way.
    return str(raw).removeprefix("0 ").strip() if raw else ""


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


# --------------------------------------------------------------------------
# elements for the satellites SatNOGS schedules
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class JobElements:
    job_id: Any
    norad: int | None
    tle0: str
    tle1: str
    tle2: str
    end: datetime | None


class TleCache:
    """The elements each SatNOGS job was scheduled with, kept after it starts.

    SatNOGS schedules some objects under temporary catalogue numbers — 98329
    at the time of writing — that no public element set carries, so the
    predictor knows nothing about them. Every job in /api/jobs/ carries the TLE
    it was scheduled with, though, which is enough to say where the satellite
    is. But a job leaves /api/jobs/ the moment it starts, and
    SatnogsService.refresh_observations then replaces its running entry with a
    minimal dict that has no elements — so exactly while the job is recording,
    nothing holds them. This remembers them from the job list while they are
    in it, and forgets them ten minutes after each job ends.
    """

    def __init__(self) -> None:
        self._jobs: dict[Any, JobElements] = {}

    def update(self, jobs, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        for job in list(jobs or []):
            if not isinstance(job, dict):
                continue
            tle1, tle2 = job.get("tle1"), job.get("tle2")
            job_id = job.get("id")
            # A running entry with no elements must not replace one that has
            # them: that minimal dict is the reason this cache exists.
            if job_id is None or not (isinstance(tle1, str) and isinstance(tle2, str)):
                continue
            if not (tle1.startswith("1 ") and tle2.startswith("2 ")):
                continue
            norad = job.get("norad_cat_id")
            self._jobs[job_id] = JobElements(
                job_id=job_id,
                norad=norad if isinstance(norad, int) else None,
                tle0=_clean_name(job.get("tle0")),
                tle1=tle1, tle2=tle2,
                end=_parse_ts(job.get("end")),
            )
        for key, entry in list(self._jobs.items()):
            if entry.end is None or now >= entry.end + TLE_KEEP_AFTER_END:
                self._jobs.pop(key, None)

    def get(self, job_id: Any) -> JobElements | None:
        return None if job_id is None else self._jobs.get(job_id)

    def for_norad(self, norad: int | None) -> JobElements | None:
        """The elements of the latest-ending job for this satellite, if any."""
        found = [e for e in self._jobs.values() if e.norad == norad and norad is not None]
        if not found:
            return None
        far = datetime.max.replace(tzinfo=timezone.utc)
        return max(found, key=lambda e: e.end or far)

    def __len__(self) -> int:
        return len(self._jobs)


def focus_look(predictor, norad: int, override=None) -> tuple[str, float, float] | None:
    """(name, az, el) of a satellite from the station, now — or None.

    From the predictor's catalogue when it has the satellite; otherwise from
    `override`, an EarthSatellite built from a SatNOGS job's own elements.
    """
    if override is None:
        pos = predictor.position(norad)
        return None if pos is None else (getattr(pos, "name", "") or "",
                                         float(pos.az), float(pos.el))
    try:
        t = predictor.ts.from_datetime(datetime.now(timezone.utc))
        el, az, _ = (override - predictor.site).at(t).altaz()
        return (_clean_name(getattr(override, "name", "")), float(az.degrees),
                float(el.degrees))
    except Exception:
        # Elements SatNOGS scheduled with but that will not propagate are no
        # elements at all, as far as a readout is concerned.
        log.warning("could not propagate job elements for %s", norad, exc_info=True)
        return None


# --------------------------------------------------------------------------
# ownership
# --------------------------------------------------------------------------

def _commitments(satnogs) -> list[dict]:
    source = getattr(satnogs, "commitments", None)
    if source is None:
        source = getattr(satnogs, "jobs", None) or []
    return [j for j in list(source) if isinstance(j, dict)]


def _running(commitments: list[dict], now: datetime) -> dict | None:
    live = []
    for job in commitments:
        start, end = _parse_ts(job.get("start")), _parse_ts(job.get("end"))
        if start is not None and end is not None and start <= now < end:
            live.append((start, job))
    return min(live, key=lambda x: x[0])[1] if live else None


def _upcoming(commitments: list[dict], now: datetime) -> list[tuple[datetime, dict]]:
    out = []
    for job in commitments:
        start = _parse_ts(job.get("start"))
        if start is not None and start > now:
            out.append((start, job))
    return sorted(out, key=lambda x: x[0])


def _plan_pass(plan, key: str | None):
    """The plan's pass with this key — planned first, then any decision."""
    if plan is None or key is None:
        return None
    for cand in getattr(plan, "planned", None) or []:
        if cand.key == key:
            return cand
    for decision in getattr(plan, "decisions", None) or []:
        cand = getattr(decision, "candidate", None)
        if cand is not None and cand.key == key:
            return cand
    return None


def _has_catalogue(predictor, norad: int | None) -> bool:
    lookup = getattr(predictor, "satellite", None)
    if norad is None or not callable(lookup):
        return False
    try:
        return lookup(norad) is not None
    except Exception:
        return False


def _name(norad: int | None, *, predictor, tlecache: TleCache,
          job: dict | None = None) -> str:
    """The catalogue's name first — it is the one the selector shows — then
    the job's own line 0, then whatever the elements cache remembered."""
    lookup = getattr(predictor, "satellite", None)
    if norad is not None and callable(lookup):
        try:
            sat = lookup(norad)
        except Exception:
            sat = None
        if sat is not None and getattr(sat, "name", ""):
            return _clean_name(sat.name)
    if job is not None and job.get("tle0"):
        return _clean_name(job.get("tle0"))
    cached = tlecache.get(job.get("id")) if job is not None else None
    cached = cached or tlecache.for_norad(norad)
    if cached is not None and cached.tle0:
        return cached.tle0
    return f"#{norad}" if norad is not None else "an unknown satellite"


def _age_text(age: float | None) -> str:
    if age is None:
        return "not received yet"
    if age < 120:
        return f"{age:.0f} s old"
    return f"{age / 60:.0f} min old"


def _track_los(*, control, executor_state, plan, predictor, target: int,
               los_cache: dict, now: datetime) -> datetime | None:
    """LOS of the pass a track is following.

    From the plan when autopilot owns the track: that is the LOS it will stop
    at. Otherwise from the predictor — a 24 h search, so once per track and
    cached by track id rather than every second, and again only once that LOS
    has passed (an operator's track holds through a set and picks up the next
    pass).
    """
    if (executor_state is not None and executor_state.enabled
            and getattr(control, "last_origin", "") == "autopilot"):
        cand = _plan_pass(plan, executor_state.current)
        if cand is not None and cand.norad == target and cand.los > now:
            return cand.los
    key = (getattr(control, "track_id", None), target)
    if key in los_cache:
        los = los_cache[key]
        if los is None or los > now:
            return los
    los = None
    try:
        nxt = predictor.next_pass(target)
        los = nxt.los if nxt is not None else None
    except Exception:
        log.warning("could not predict LOS for %s", target, exc_info=True)
    los_cache.clear()
    los_cache[key] = los
    return los


def antenna_state(*, control_state, control, executor_state, satnogs, plan,
                  settings, predictor, tlecache: TleCache,
                  now: datetime | None = None, los_cache: dict | None = None) -> dict:
    """Who has the antenna and which satellite it is working. See the module
    docstring; the rules below are tried in order and the first one wins."""
    now = now or datetime.now(timezone.utc)
    los_cache = {} if los_cache is None else los_cache

    def names(norad: int | None, job: dict | None = None) -> str:
        return _name(norad, predictor=predictor, tlecache=tlecache, job=job)

    mode = getattr(control_state, "mode", "idle")
    target = getattr(control_state, "target_norad", None)
    origin = getattr(control, "last_origin", "") or ""
    if origin in ("", "none"):
        origin = "unknown"
    commitments = _commitments(satnogs)
    running = _running(commitments, now)
    upcoming = _upcoming(commitments, now)
    age = getattr(satnogs, "station_age_s", None)
    connected = getattr(satnogs, "is_connected", None)
    stale = age is None or age > float(settings.gate_max_stale_s)
    autopilot_on = bool(executor_state is not None and executor_state.enabled)

    own: dict[str, Any] = {
        "owner": "none", "standby": False, "warn": False, "activity": "idle",
        "norad": None, "name": None, "until": None, "job_id": None,
    }

    # 1. Not knowing what SatNOGS is doing means not knowing who has the
    #    antenna. Our own track is the exception: it is ours whatever SatNOGS
    #    says, and its loop ends it the moment the stale gate closes.
    if stale and mode != "track":
        own.update(owner="unknown",
                   activity=f"SatNOGS status {_age_text(age)} — owner unknown")

    # 2. A SatNOGS job inside its window. With the client disconnected nothing
    #    is recording it, which is worth more than a mention.
    elif running is not None:
        norad = running.get("norad_cat_id")
        end = _parse_ts(running.get("end"))
        job_id = running.get("id")
        name = names(norad, running)
        own.update(owner="satnogs", norad=norad, name=name, until=_iso(end),
                   job_id=job_id)
        if connected is False:
            own.update(warn=True, activity=(
                f"SatNOGS job #{job_id} due now but client disconnected — "
                "nothing is recording"))
        else:
            own["activity"] = (f"recording {name}"
                               + (f" until {end:%H:%M}Z" if end else ""))

    # 3. A track this service is running, owned by whoever started it.
    elif mode == "track" and target is not None:
        los = _track_los(control=control, executor_state=executor_state,
                         plan=plan, predictor=predictor, target=target,
                         los_cache=los_cache, now=now)
        name = names(target)
        own.update(owner=origin, norad=target, name=name, until=_iso(los),
                   activity=f"tracking {name}"
                            + (f" until LOS {los:%H:%M}Z" if los else ""))

    # 4. Autopilot engaged: waiting, positioning, blocked — its own words.
    elif autopilot_on:
        cand = _plan_pass(plan, executor_state.current)
        phase, detail = executor_state.phase, executor_state.detail
        own.update(owner="autopilot",
                   activity=f"{phase}: {detail}" if detail else phase)
        if cand is not None:
            own.update(norad=cand.norad, name=cand.name or names(cand.norad))
            if phase in ("waiting", "positioning") and cand.aos > now:
                own["until"] = _iso(cand.aos)

    # 5. An absolute move, but only while the lease it was made under holds.
    elif mode == "manual" and getattr(control, "armed", False):
        what = getattr(control, "_last_command", "") or "absolute move"
        own.update(owner=origin, activity=f"manual: {what}")

    # 6. satnogs-client is connected and could start at any moment.
    elif connected is True:
        nxt = upcoming[0] if upcoming else None
        jobs_age = getattr(satnogs, "jobs_age_s", None)
        own.update(owner="satnogs", standby=True)
        if nxt is not None:
            start, job = nxt
            norad = job.get("norad_cat_id")
            name = names(norad, job)
            own.update(norad=norad, name=name, until=_iso(start), job_id=job.get("id"),
                       activity=f"client connected · next job {name} {start:%H:%M}Z")
        elif jobs_age is None or jobs_age > float(settings.gate_max_stale_s):
            own["activity"] = "client connected · schedule not known"
        else:
            own["activity"] = "client connected · no job scheduled"

    # 7. Otherwise nobody — and, by rule 1, we know that rather than assume it.

    # --- focus: which satellite the display should be showing --------------
    focus, reason, focus_job = None, "", None
    if mode == "track" and target is not None:
        focus, reason = target, "track target"
    elif running is not None and isinstance(running.get("norad_cat_id"), int):
        focus, reason = running["norad_cat_id"], "SatNOGS recording"
        focus_job = running.get("id")
    elif autopilot_on and executor_state.phase in ("positioning", "tracking"):
        cand = _plan_pass(plan, executor_state.current)
        if cand is not None:
            focus, reason = cand.norad, "autopilot positioning"
    if focus is None:
        for start, job in upcoming:
            if start - now > FOCUS_SOON:
                break
            if isinstance(job.get("norad_cat_id"), int):
                focus, reason = job["norad_cat_id"], "SatNOGS job within 10 min"
                focus_job = job.get("id")
                break
    if focus is None:
        focus, reason = settings.default_norad, "default satellite"

    if _has_catalogue(predictor, focus):
        source = "catalogue"
    else:
        cached = tlecache.get(focus_job) or tlecache.for_norad(focus)
        source = "job_tle" if cached is not None else "none"
        if cached is not None:
            focus_job = cached.job_id

    return {
        **own,
        "focus_norad": focus,
        "focus_name": names(focus),
        "focus_reason": reason,
        "focus_has_elements": source != "none",
        "focus_source": source,
        "focus_job_id": focus_job if source == "job_tle" else None,
        "satnogs_age_s": None if age is None else round(float(age), 1),
        "updated_at": now.isoformat(),
    }


def fingerprint(state: dict | None) -> tuple:
    """What has to change for a new `antenna` frame to be worth sending."""
    if state is None:
        return ()
    return tuple(sorted((k, repr(v)) for k, v in state.items() if k not in VOLATILE))


def is_finite_look(look: tuple[str, float, float] | None) -> bool:
    return look is not None and math.isfinite(look[1]) and math.isfinite(look[2])
