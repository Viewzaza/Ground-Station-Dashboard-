"""When the station scheduler should next run itself.

Split out as a pure function for the same reason `util/geo.py` is: the rules
have edges that are tedious to reach through a running scheduler loop and
trivial to reach in a test - a slot missed while the backend was down, a slot
that does not exist because the clock jumped forward, a slot that happens twice
because it jumped back.

Two ideas do all the work here.

**Never burst.** If the backend was off for six hours, an interval schedule is
overdue by many periods. Firing once for each of them would book six runs back
to back. Overdue means "run once, now", never "catch up".

**A grace window, not a catch-up queue.** A slot missed by a couple of minutes
- a restart, a slow boot - should still run: the observations it would book are
still in the future. A slot missed by six hours should not: its planning window
has largely passed, and booking it late is worse than not booking it. The
boundary is `catch_up_grace_s`, and it is the only thing separating those two
cases.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

# 24-hour wall clock, no seconds. Anything else is dropped rather than guessed
# at: "6pm" and "18:0" both mean something obvious to a person and nothing
# reliable to a scheduler.
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Half an hour. Long enough to cover a restart, a slow container start or a
# short outage; short enough that a run which fires is still planning a window
# the operator would recognise.
DEFAULT_CATCH_UP_GRACE_S = 1800.0

# How far ahead a remembered run may sit before we stop believing it. Anything
# inside this is treated as clock skew and left alone (so the slot it records
# stays suppressed); anything beyond it is discarded as corrupt.
MAX_CLOCK_SKEW = timedelta(hours=6)


def normalize_times(times) -> list[str]:
    """Sorted, de-duplicated, valid `HH:MM` strings."""
    seen = set()
    for value in times or []:
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if TIME_RE.fullmatch(candidate):
            seen.add(candidate)
    return sorted(seen)


def resolve_zone(name: str) -> tzinfo:
    """The named zone, or UTC if this system has no database for it.

    python:3.12-slim does carry Debian's tzdata, so this normally resolves and
    no `tzdata` wheel is needed. Falling back loudly rather than raising means
    a stripped-down base image costs the operator correct local times, not the
    whole auto-run feature.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning(
            "no timezone database entry for %r; auto-run times will be read as "
            "UTC. Times of day will be wrong until this is fixed.",
            name,
        )
        return timezone.utc


def _as_utc(value: datetime) -> datetime:
    """Read a datetime as UTC, including a naive one.

    `astimezone()` on a naive datetime assumes the HOST's timezone, which on
    this station is UTC+7. `auto_run_last_fire_utc` says UTC in its name but is
    an operator-editable string in a JSON file, so a hand-written
    "2026-09-21T12:00:00" with no offset would be read seven hours early -
    old enough for a slot that already ran to fall back inside the grace
    window and be booked a second time.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _at(day: date, clock: str, zone: tzinfo) -> datetime:
    """A wall-clock time on a given local day, as a UTC instant.

    Both DST edges resolve to exactly one instant, via `zoneinfo`'s default
    `fold=0`:

      * a time the clock jumps over each spring does not exist, and maps to
        the instant an hour later - so the slot still runs that day, shifted.
        Skipping it instead would be tidier for the rare case of two slots an
        hour apart straddling the gap, but it would cost a single-slot station
        a whole day of observations, which is the worse failure;
      * a time that happens twice each autumn resolves to the first of the
        two, so the slot runs once rather than twice.

    Neither raises, and "exactly once" is the property that matters.
    """
    hour, minute = (int(part) for part in clock.split(":"))
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
    return local.astimezone(timezone.utc)


def next_fire(
    *,
    mode: str,
    times,
    interval_min: int,
    tz: str,
    now: datetime,
    last_run: datetime | None,
    catch_up_grace_s: float = DEFAULT_CATCH_UP_GRACE_S,
    not_before: datetime | None = None,
) -> datetime | None:
    """The next instant an auto-run should happen, in UTC, or None for never.

    A returned time at or before `now` means "fire immediately". The caller
    does not need to distinguish the overdue case from the on-time one.
    """
    now = _as_utc(now)
    last = _as_utc(last_run) if last_run is not None else None
    if last is not None and last - now > MAX_CLOCK_SKEW:
        # A remembered run this far ahead cannot be a clock wobble - it is a
        # snapshot restore, a badly wrong RTC, or a hand-edited config. Left
        # alone it becomes a floor no candidate can clear, next_fire returns
        # None for good, and the loop reads that as "auto-run is off": the
        # station quietly stops booking with nothing anywhere saying why.
        #
        # Only a LARGE offset is clamped, deliberately. Clamping every future
        # last_run would drop the floor below the slot that has just fired, so
        # a ten-minute NTP step backwards would re-offer that slot and run it
        # a second time - unattended, and for real. A small step is better
        # absorbed by leaving the floor where it is.
        log.warning(
            "last auto-run is recorded %s in the future, which is too far to "
            "be clock skew; ignoring it so auto-run does not stop.",
            last - now,
        )
        last = now

    if mode == "interval":
        minutes = int(interval_min or 0)
        if minutes <= 0:
            return None
        due = (last or now) + timedelta(minutes=minutes)
        if due <= now:
            # Overdue, possibly by many periods. One run, now - stepping
            # forward period by period would book a burst of back-to-back runs
            # for a window that has already mostly passed.
            return now
        return due

    slots = normalize_times(times)
    if not slots:
        # No times configured is a real state, not an error: the operator has
        # the feature on but has not said when yet.
        return None

    zone = resolve_zone(tz)

    # The earliest thing we are willing to fire. A slot before this is either
    # already done (last_run) or too stale to be worth booking (the grace
    # window). Taking the later of the two is what stops a restart re-firing a
    # slot that already ran.
    floor = now - timedelta(seconds=catch_up_grace_s)
    if last is not None and last > floor:
        floor = last
    if not_before is not None:
        # A slot earlier than the last settings change was never missed - it
        # was not configured yet. Without this, adding a time a few minutes in
        # the past and pressing SAVE fires that slot immediately.
        floor = max(floor, _as_utc(not_before))

    # Yesterday too: a slot late yesterday can still be inside the grace window
    # just after midnight, and a naive "today and tomorrow" would skip it.
    local_today = now.astimezone(zone).date()
    # Keyed by (day, wall clock). _at returns None for a wall clock that does
    # not exist on that day - see its docstring for why skipping beats
    # shifting.
    by_wall_clock = {
        (offset, clock): _at(local_today + timedelta(days=offset), clock, zone)
        for offset in (-1, 0, 1)
        for clock in slots
    }
    # On a spring-forward day a slot inside the gap shifts onto the instant an
    # adjacent slot already occupies, and the two become one fire. That is the
    # right outcome - firing twice at one instant would be two bookings for
    # one window - but it must not be silent, because the operator configured
    # two runs and will get one.
    collapsed = len(by_wall_clock) - len(set(by_wall_clock.values()))
    if collapsed:
        log.warning(
            "%d auto-run slot(s) fall on an instant another slot already "
            "occupies, almost certainly a daylight-saving transition; they "
            "will produce a single run rather than one each.",
            collapsed,
        )
    candidates = sorted(set(by_wall_clock.values()))
    for candidate in candidates:
        if candidate > floor:
            return candidate
    return None
