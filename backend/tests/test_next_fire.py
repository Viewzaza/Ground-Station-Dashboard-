"""When the unattended scheduler fires - and, far more importantly, when it does not.

`next_fire()` is the only thing standing between "the station books its own
observations overnight" and two failure modes that both look like the software
working correctly:

* **The silent day.** The function returns `None`, or a time a day further out
  than the operator configured, and nothing is booked. Nobody is watching at
  03:00, so the first sign is an empty calendar the next morning and a satellite
  pass that will not come round again for hours. Every `None` this module can
  return is covered below, so that `None` only ever means "the operator has not
  said when yet", never "we could not work it out".

* **The burst.** The backend is down for an evening, comes back, and the
  scheduler decides it owes the station every period it missed. Six runs go out
  back to back against the SatNOGS network, booking a window that has largely
  passed, on a shared community resource with real rate limits. The single most
  important test in this file is
  `test_an_overdue_interval_fires_exactly_once_and_never_bursts`.

Between those two sits the grace window, and it is the only thing that tells
them apart. A slot missed by five minutes - a restart, a slow container start -
is still worth running, because the observations it would book are still in the
future. A slot missed by six hours is not: booking a planning window that late
is worse than not booking it at all. `catch_up_grace_s` draws that line and
nothing else does, so both sides of it are pinned here to the second.

Three classes of bug hide in the timezone handling, and all three are invisible
to a test written in UTC:

* **Midnight in the wrong zone.** The station is at UTC+7. "After the last slot
  of the day, use the first slot tomorrow" has to mean tomorrow in Bangkok; a
  06:00 local slot is 23:00 UTC the *previous* day. An implementation that rolls
  the day over in UTC passes in London and books seven hours late here.

* **The hour that never happened.** On a spring-forward day a 02:30 slot does
  not exist. Naive arithmetic raises, or skips the day entirely.

* **The hour that happened twice.** On an autumn day 02:30 comes round twice,
  one hour apart. The scheduler must pick one of them. If it fires on both, the
  station is booked twice for the same planning window - a real double booking
  against the network, not a cosmetic duplicate.

The DST tests use Europe/Berlin even though this station is in Asia/Bangkok,
which has never observed DST. That is deliberate: a dashboard configured for a
European station is a supported configuration, and Bangkok's total absence of
transitions means the station's own zone can never exercise this code. The 2026
transition dates are read out of the installed tz database rather than typed in
from memory - see `_dst_transition_days`.

Everything here is pure: no filesystem, no network, no clock. Every `now` is an
explicit instant, so a test that fails, fails for a reason.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.util.nextfire import (
    DEFAULT_CATCH_UP_GRACE_S,
    next_fire,
    normalize_times,
    resolve_zone,
)

# The real station's zone: UTC+7, and it has never observed DST.
BANGKOK = "Asia/Bangkok"
ICT = ZoneInfo(BANGKOK)

# Not this station's zone. Used only because Bangkok cannot reach the DST code
# at all, and an untested branch there double-books a European station.
BERLIN = "Europe/Berlin"
CET = ZoneInfo(BERLIN)

# The two slots the dashboard offers as its default.
DEFAULT_SLOTS = ["06:00", "18:00"]


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """A UTC instant, spelled out."""
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def assert_utc_instant(value, what: str) -> None:
    """Every answer must be an aware UTC instant.

    The caller stores it with `.isoformat()` and compares it against
    `datetime.now(timezone.utc)`. A naive datetime raises on that comparison and
    takes the whole auto-run loop down; one carrying a local offset is worse,
    because it compares fine and fires at the wrong time.
    """
    assert isinstance(value, datetime), f"{what} must be a datetime, got {value!r}"
    assert value.tzinfo is not None, (
        f"{what} came back naive; the auto-run loop compares it against an "
        f"aware now() and would crash the scheduler thread outright: {value!r}"
    )
    assert value.utcoffset() == timedelta(0), (
        f"{what} is not UTC ({value.isoformat()}); it is written to the config "
        f"file and read back as UTC, so a local offset silently shifts every "
        f"future run by that offset"
    )


class LooksLikeATime:
    """A stub implementing exactly one thing: `__str__` returning "06:00".

    No `strip()`, no comparison, nothing else. It stands in for whatever a
    hand-edited config or a JSON round trip can leave in the times list -
    something that prints like a time and is not one.
    """

    def __str__(self) -> str:  # pragma: no cover - only its existence matters
        return "06:00"


def _dst_transition_days(zone_name: str, year: int) -> list[date]:
    """The local days in `year` on which `zone_name` changes its UTC offset.

    Computed from the installed tz database, never typed in. A test that
    asserts "the last Sunday in March" from memory tests the author's memory;
    this one asks the same database the scheduler will ask in production, so it
    keeps telling the truth after a tzdata update moves a transition.
    """
    zone = ZoneInfo(zone_name)
    moment = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    offset = moment.astimezone(zone).utcoffset()
    days: list[date] = []
    while moment < end:
        moment += timedelta(hours=1)
        current = moment.astimezone(zone).utcoffset()
        if current != offset:
            days.append(moment.astimezone(zone).date())
            offset = current
    return days


SPRING_FORWARD, FALL_BACK = _dst_transition_days(BERLIN, 2026)


def run_scheduler(*, start: datetime, stop: datetime, last_run=None,
                  run_duration_s: float = 1.0, **schedule) -> list[datetime]:
    """Replay `app.scheduler`'s auto-run loop and list every time it fires.

    Only the part of that loop that matters here: ask when to fire, jump to
    that instant, fire, remember the fire the way `mark_auto_run()` does - the
    instant the run STARTED, not the slot it was for - and ask again.

    Counting fires is the only way to state the properties that matter. "Did
    not raise" says nothing about whether the station was booked twice.
    """
    fires: list[datetime] = []
    now, last = start, last_run
    while True:
        assert len(fires) < 200, (
            f"the scheduler never stopped firing: {len(fires)} runs between "
            f"{start.isoformat()} and {stop.isoformat()}. In production each "
            f"one is a batch of real bookings against SatNOGS."
        )
        due = next_fire(now=now, last_run=last, **schedule)
        if due is None:
            break
        assert_utc_instant(due, "the next fire time")
        now = max(due, now)          # a due time in the past means "fire now"
        if now >= stop:
            break
        fires.append(now)
        last = now                   # marked before the run, as the loop does
        now = now + timedelta(seconds=run_duration_s)
    return fires


# --- mode="interval" --------------------------------------------------------
def test_an_interval_that_has_never_run_starts_one_interval_from_now():
    now = utc(2026, 9, 21, 12, 0)

    due = next_fire(mode="interval", times=[], interval_min=180, tz=BANGKOK,
                    now=now, last_run=None)

    assert_utc_instant(due, "the first interval run")
    assert due == now + timedelta(minutes=180), (
        "a freshly enabled interval schedule must wait a full interval. Firing "
        "at once instead means every save of the settings page, and every "
        f"container restart, books a run: expected {now + timedelta(minutes=180)}, "
        f"got {due}"
    )


def test_an_interval_counts_from_the_last_run_not_from_now():
    now = utc(2026, 9, 21, 12, 0)
    last_run = now - timedelta(minutes=10)

    due = next_fire(mode="interval", times=[], interval_min=180, tz=BANGKOK,
                    now=now, last_run=last_run)

    assert due == last_run + timedelta(minutes=180), (
        "the period runs from the previous run, not from whenever we happened "
        "to ask. Restarting from `now` lets a restart loop push the next run "
        f"out indefinitely and the station never books again: expected "
        f"{last_run + timedelta(minutes=180)}, got {due}"
    )
    assert due == now + timedelta(minutes=170), (
        "ten minutes of a three-hour period are already spent, so 2h50m remain"
    )


def test_an_overdue_interval_fires_exactly_once_and_never_bursts():
    """THE most important test in this file.

    Nine hours down with a three-hour interval means three periods were missed.
    The only acceptable answer is one run, now. `last_run + interval` is an
    instant in the past: the loop would fire, mark, compute a due time still in
    the past, and fire again - a hot loop hammering SatNOGS with bookings.
    """
    now = utc(2026, 9, 21, 12, 0)
    last_run = now - timedelta(hours=9)

    due = next_fire(mode="interval", times=[], interval_min=180, tz=BANGKOK,
                    now=now, last_run=last_run)

    assert due == now, (
        "an overdue interval must resolve to exactly `now` - one run, "
        f"immediately. Got {due}, which is "
        f"{(due - now).total_seconds():+.0f}s from now"
    )
    assert due != last_run + timedelta(minutes=180), (
        "returning the first missed period hands the loop a due time three "
        "hours in the past; it fires, recomputes, and is still overdue, so it "
        "fires again immediately and keeps going until the network bans us"
    )

    # And having fired, it must settle: one clean interval, not another catch-up.
    after = next_fire(mode="interval", times=[], interval_min=180, tz=BANGKOK,
                      now=now + timedelta(seconds=1), last_run=now)
    assert after == now + timedelta(minutes=180), (
        "after the catch-up run the schedule must go back to normal spacing; "
        f"anything at or before now would be a second immediate run: got {after}"
    )


def test_an_overdue_interval_does_not_queue_the_periods_it_missed():
    """The same rule, stated as a count: three missed periods do not become
    three extra runs crowded into the first minutes after recovery."""
    start = utc(2026, 9, 21, 12, 0)
    fires = run_scheduler(start=start, stop=start + timedelta(hours=12),
                          last_run=start - timedelta(hours=9),
                          mode="interval", times=[], interval_min=180, tz=BANGKOK)

    assert fires[0] == start, "recovery fires once, immediately"
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(fires, fires[1:])]
    assert gaps == [180.0] * len(gaps), (
        "after the recovery run every gap must be a full three hours. A gap of "
        f"anything less is the burst this rule exists to prevent: gaps were "
        f"{gaps} minutes"
    )
    assert len(fires) == 4, (
        "twelve hours at a three-hour interval starting with one catch-up run "
        f"is four runs; {len(fires)} means the missed periods were queued "
        f"({[f.isoformat() for f in fires]})"
    )


@pytest.mark.parametrize("interval_min", [0, -1, -180])
def test_a_zero_or_negative_interval_is_never_rather_than_always(interval_min):
    now = utc(2026, 9, 21, 12, 0)

    due = next_fire(mode="interval", times=[], interval_min=interval_min,
                    tz=BANGKOK, now=now, last_run=None)

    assert due is None, (
        f"an interval of {interval_min} minutes cannot mean a schedule. It has "
        "to disable auto-run, not produce a due time at or before now - that "
        f"would be a run every time round the loop, forever: got {due}"
    )


# --- mode="times": the ordinary day -----------------------------------------
def test_the_next_slot_today_is_read_in_the_configured_zone():
    # 09:00 in Bangkok, which is 02:00 UTC. The 18:00 local slot is next.
    now = utc(2026, 9, 21, 2, 0)

    due = next_fire(mode="times", times=DEFAULT_SLOTS, interval_min=0,
                    tz=BANGKOK, now=now, last_run=None)

    assert_utc_instant(due, "the next slot")
    assert due == utc(2026, 9, 21, 11, 0), (
        "18:00 at the station is 11:00 UTC. Reading the slot as 18:00 UTC would "
        f"book seven hours late, in the middle of the operator's night: got {due}"
    )
    assert due.astimezone(ICT).strftime("%H:%M") == "18:00"


def test_after_the_last_slot_the_day_rolls_over_in_the_station_zone():
    """The midnight-crossing test, and it only bites outside UTC.

    19:00 in Bangkok is 12:00 UTC on the same date, so a rollover implemented in
    UTC still thinks there is a slot left today and returns 18:00 UTC - six
    hours late. The correct answer is tomorrow's 06:00 local, which is 23:00 UTC
    on the PREVIOUS UTC day. This test passes in London and fails here if the
    zone is ignored, which is exactly the shape of the bug.
    """
    now = utc(2026, 9, 21, 12, 0)   # 19:00 ICT, both slots today are gone

    due = next_fire(mode="times", times=DEFAULT_SLOTS, interval_min=0,
                    tz=BANGKOK, now=now, last_run=None)

    assert due == utc(2026, 9, 21, 23, 0), (
        "the next run is 06:00 tomorrow in Bangkok, which is 23:00 UTC tonight. "
        f"Got {due}"
    )
    local = due.astimezone(ICT)
    assert (local.date(), local.strftime("%H:%M")) == (date(2026, 9, 22), "06:00"), (
        f"the operator asked for 06:00 and must get 06:00 on their own wall "
        f"clock; they see {local.isoformat()}"
    )
    assert due.date() == date(2026, 9, 21), (
        "and it is still 'today' in UTC - a scheduler that advanced the UTC "
        "date instead of the local one would answer 2026-09-22 and be seven "
        "hours late"
    )
    assert due != utc(2026, 9, 22, 6, 0), (
        "06:00 UTC is 13:00 at the station: the wrong time of day entirely"
    )


# --- mode="times": the grace window -----------------------------------------
def test_a_slot_missed_by_minutes_still_fires_immediately():
    """A restart across a slot. The passes it would book are still ahead."""
    slot = utc(2026, 9, 21, 11, 0)              # 18:00 ICT
    now = slot + timedelta(minutes=5)

    due = next_fire(mode="times", times=DEFAULT_SLOTS, interval_min=0,
                    tz=BANGKOK, now=now, last_run=None)

    assert due == slot, (
        "a slot five minutes stale is inside the default half-hour grace "
        "window and must still run; skipping it costs the station a whole "
        f"planning window because the container was slow to start: got {due}"
    )
    assert due <= now, (
        "a due time at or before now is how this module says 'fire "
        "immediately'; the caller does not look at anything else"
    )


def test_a_slot_missed_by_six_hours_is_abandoned_not_booked_late():
    stale = utc(2026, 9, 20, 23, 0)             # 06:00 ICT on the 21st
    now = stale + timedelta(hours=6)            # 12:00 ICT on the 21st

    due = next_fire(mode="times", times=DEFAULT_SLOTS, interval_min=0,
                    tz=BANGKOK, now=now, last_run=None)

    assert due != stale, (
        "a six-hour-old slot must never be returned. Its planning window has "
        "largely passed, so running it books whatever is left of a schedule "
        "the operator expected at breakfast - worse than not booking at all"
    )
    assert due > now, (
        f"after abandoning a stale slot the answer must be a FUTURE slot, or "
        f"the loop fires on a past time it just decided to skip: got {due} "
        f"against now {now}"
    )
    assert due == utc(2026, 9, 21, 11, 0), (
        "the next real opportunity is today's 18:00 local slot"
    )


def test_the_grace_boundary_decides_consistently_on_either_side():
    """The whole difference between 'still worth running' and 'too late' is
    this one number, so both sides of it are pinned to the second."""
    slot = utc(2026, 9, 21, 11, 0)              # 18:00 ICT
    inside = slot + timedelta(seconds=DEFAULT_CATCH_UP_GRACE_S - 1)
    outside = slot + timedelta(seconds=DEFAULT_CATCH_UP_GRACE_S)

    just_in_time = next_fire(mode="times", times=DEFAULT_SLOTS, interval_min=0,
                             tz=BANGKOK, now=inside, last_run=None)
    assert just_in_time == slot, (
        f"one second inside the {DEFAULT_CATCH_UP_GRACE_S:.0f}s grace window the "
        f"slot still runs: got {just_in_time}"
    )

    too_late = next_fire(mode="times", times=DEFAULT_SLOTS, interval_min=0,
                         tz=BANGKOK, now=outside, last_run=None)
    assert too_late == utc(2026, 9, 21, 23, 0), (
        "at the boundary itself the slot is given up and the next one is "
        "tomorrow's 06:00 local. What matters to the operator is that the "
        "cutoff is decided once and does not flap between runs: got "
        f"{too_late}"
    )


# --- mode="times": what stops a restart re-firing ---------------------------
def test_a_slot_that_already_ran_is_not_offered_again_after_a_restart():
    """Without this, every restart inside the grace window re-books the slot
    the station has already booked - duplicate observations against SatNOGS,
    from nothing worse than a container being restarted twice."""
    slot = utc(2026, 9, 21, 11, 0)              # 18:00 ICT, already run
    now = slot + timedelta(minutes=5)           # still inside the grace window

    due = next_fire(mode="times", times=DEFAULT_SLOTS, interval_min=0,
                    tz=BANGKOK, now=now, last_run=slot + timedelta(seconds=30))

    assert due != slot, (
        "this slot has a recorded run against it; offering it again is a "
        "second batch of bookings for a planning window already booked"
    )
    assert due == utc(2026, 9, 21, 23, 0), (
        f"the next run is tomorrow's 06:00 local (23:00 UTC tonight): got {due}"
    )

    # Firing exactly on the slot is the normal case, not an edge one.
    exact = next_fire(mode="times", times=DEFAULT_SLOTS, interval_min=0,
                      tz=BANGKOK, now=now, last_run=slot)
    assert exact == utc(2026, 9, 21, 23, 0), (
        "a run marked at the precise slot instant must suppress that slot too; "
        f"an off-by-one here re-fires every on-time run: got {exact}"
    )


def test_a_normal_day_fires_once_per_configured_slot():
    start = utc(2026, 9, 20, 17, 0)             # 00:00 ICT on the 21st
    fires = run_scheduler(start=start, stop=start + timedelta(days=1),
                          mode="times", times=DEFAULT_SLOTS, interval_min=0,
                          tz=BANGKOK)

    assert [f.astimezone(ICT).strftime("%m-%d %H:%M") for f in fires] == [
        "09-21 06:00", "09-21 18:00"
    ], (
        "two configured slots means two runs in a local day - no more, no "
        f"fewer: the station saw {[f.astimezone(ICT).isoformat() for f in fires]}"
    )


# --- nothing to fire on -----------------------------------------------------
def test_no_times_configured_is_never_not_an_error():
    due = next_fire(mode="times", times=[], interval_min=180, tz=BANGKOK,
                    now=utc(2026, 9, 21, 12, 0), last_run=None)

    assert due is None, (
        "auto-run switched on with no times set is a real state - the operator "
        "has not said when yet. It must park quietly, not fall back to the "
        f"interval or to some invented default: got {due}"
    )


def test_a_times_list_that_is_entirely_invalid_is_never():
    due = next_fire(mode="times", times=["6:00", "25:00", ""], interval_min=180,
                    tz=BANGKOK, now=utc(2026, 9, 21, 12, 0), last_run=None)

    assert due is None, (
        "if every configured time was rejected there is nothing to schedule. "
        "Guessing at one would run the station at a time nobody chose: got "
        f"{due}"
    )


# --- normalize_times --------------------------------------------------------
def test_times_are_sorted_deduplicated_and_trimmed():
    cleaned = normalize_times(["18:00", " 06:00 ", "06:00", "\t00:30\n", "18:00"])

    assert cleaned == ["00:30", "06:00", "18:00"], (
        "the slot list drives an ordered walk through the day, so it has to be "
        "sorted; a duplicate left in would be a second booking run at the same "
        f"minute. Got {cleaned}"
    )


@pytest.mark.parametrize("bad", [
    "6:00",        # no leading zero - the common hand-edit
    "25:00",       # no such hour
    "24:00",       # midnight is 00:00; 24:00 is a different day's notation
    "18:60",       # no such minute
    "06:00:00",    # seconds are not part of the format
    "0600",        # no separator
    "6pm",         # human, not machine
    "",            # an empty row in the settings form
    "   ",
    None,
    600,
    ["06:00"],
])
def test_a_time_that_is_not_hh_mm_is_dropped_rather_than_guessed_at(bad):
    assert normalize_times([bad, "12:00"]) == ["12:00"], (
        f"{bad!r} means something obvious to a person and nothing reliable to "
        "a scheduler. Dropping it loses one slot the operator can see is "
        "missing; guessing at it books the station at an hour nobody chose"
    )


def test_something_that_merely_prints_like_a_time_is_not_a_time():
    assert normalize_times([LooksLikeATime()]) == [], (
        "the list is normalised straight off a JSON config file, so anything "
        "can be in it. Calling str() on whatever turns up would accept objects "
        "whose repr happens to look right and reject nothing"
    )
    assert next_fire(mode="times", times=[LooksLikeATime()], interval_min=0,
                     tz=BANGKOK, now=utc(2026, 9, 21, 12, 0), last_run=None) is None, (
        "and a list of nothing usable must park auto-run, not raise inside the "
        "scheduler thread where the operator will never see the traceback"
    )


# --- daylight saving --------------------------------------------------------
def test_the_2026_berlin_transitions_are_where_the_tz_database_says():
    """A guard on the two fixtures below: if these days are not really the
    pathological ones, the DST tests are asserting nothing at all."""
    assert (SPRING_FORWARD.month, FALL_BACK.month) == (3, 10), (
        f"expected a March and an October transition in 2026, got "
        f"{SPRING_FORWARD} and {FALL_BACK}"
    )

    imaginary = datetime(SPRING_FORWARD.year, SPRING_FORWARD.month,
                         SPRING_FORWARD.day, 2, 30, tzinfo=CET)
    assert imaginary.astimezone(timezone.utc).astimezone(CET) != imaginary, (
        f"02:30 on {SPRING_FORWARD} is supposed to be a wall-clock time that "
        "never happens; if it exists, the spring test below proves nothing"
    )

    ambiguous = datetime(FALL_BACK.year, FALL_BACK.month, FALL_BACK.day,
                         2, 30, tzinfo=CET)
    assert ambiguous.utcoffset() != ambiguous.replace(fold=1).utcoffset(), (
        f"02:30 on {FALL_BACK} is supposed to happen twice; if it happens once, "
        "the double-booking test below proves nothing"
    )


def test_a_slot_in_the_hour_that_never_happened_still_runs_once():
    """Spring forward: the clock jumps 02:00 -> 03:00, so a 02:30 slot does not
    exist that day. It must not raise, and it must not silently vanish - a
    station that skips a day because of a clock change books nothing."""
    start = datetime(SPRING_FORWARD.year, SPRING_FORWARD.month,
                     SPRING_FORWARD.day, tzinfo=CET)          # local midnight
    stop = start + timedelta(days=1)

    fires = run_scheduler(start=start.astimezone(timezone.utc),
                          stop=stop.astimezone(timezone.utc),
                          mode="times", times=["02:30"], interval_min=0, tz=BERLIN)

    assert len(fires) == 1, (
        f"the 02:30 slot must run exactly once on {SPRING_FORWARD}. Zero means "
        "the clock change cost the station a day of observations; more than "
        f"one is a repeat booking. Got {[f.isoformat() for f in fires]}"
    )
    local = fires[0].astimezone(CET)
    assert local.date() == SPRING_FORWARD, (
        f"and it has to land on the day it was configured for, not slide into "
        f"the next one: the operator sees {local.isoformat()}"
    )
    assert local.hour in (2, 3), (
        "02:30 does not exist, so shifting to 03:30 is the honest answer; any "
        f"other hour means the slot was reinterpreted: {local.isoformat()}"
    )


def test_a_slot_in_the_hour_that_happens_twice_runs_once_not_twice():
    """Autumn: 02:30 comes round twice, an hour apart. Firing on both books the
    station twice for the same planning window."""
    start = datetime(FALL_BACK.year, FALL_BACK.month, FALL_BACK.day, tzinfo=CET)
    stop = start + timedelta(days=1)
    first = datetime(FALL_BACK.year, FALL_BACK.month, FALL_BACK.day,
                     2, 30, fold=0, tzinfo=CET).astimezone(timezone.utc)
    second = datetime(FALL_BACK.year, FALL_BACK.month, FALL_BACK.day,
                      2, 30, fold=1, tzinfo=CET).astimezone(timezone.utc)

    fires = run_scheduler(start=start.astimezone(timezone.utc),
                          stop=stop.astimezone(timezone.utc),
                          mode="times", times=["02:30"], interval_min=0, tz=BERLIN)

    assert len(fires) == 1, (
        f"02:30 happens twice on {FALL_BACK}, and the station must be booked "
        f"once. Got {len(fires)} runs: {[f.isoformat() for f in fires]}"
    )
    assert fires[0] in (first, second), (
        f"the one run has to be one of the two real 02:30 instants "
        f"({first.isoformat()} or {second.isoformat()}), not some third time: "
        f"got {fires[0].isoformat()}"
    )


def test_the_second_pass_of_an_ambiguous_slot_is_not_a_second_booking():
    """The same day, stated directly: having run at the first 02:30, asking
    again an hour later must not offer the second one."""
    first = datetime(FALL_BACK.year, FALL_BACK.month, FALL_BACK.day,
                     2, 30, fold=0, tzinfo=CET).astimezone(timezone.utc)
    second = datetime(FALL_BACK.year, FALL_BACK.month, FALL_BACK.day,
                      2, 30, fold=1, tzinfo=CET).astimezone(timezone.utc)
    assert second - first == timedelta(hours=1), "the fixture itself"

    due = next_fire(mode="times", times=["02:30"], interval_min=0, tz=BERLIN,
                    now=second + timedelta(minutes=1), last_run=first)

    assert due != second, (
        "the run recorded at the first 02:30 covers the whole ambiguous hour. "
        "Offering the second occurrence books the station again for a window "
        "it is already booked for, and the operator's clock shows the same "
        "02:30 both times, so it looks like one run in the log"
    )
    assert_utc_instant(due, "the run after an ambiguous slot")
    assert due > second, (
        f"the next run belongs to the following day: got {due.isoformat()}"
    )
    assert due.astimezone(CET).date() == FALL_BACK + timedelta(days=1), (
        f"specifically tomorrow's 02:30 local: got {due.astimezone(CET).isoformat()}"
    )


# --- zones we cannot resolve ------------------------------------------------
@pytest.mark.parametrize("nonsense", [
    "Mars/Olympus_Mons",
    "Not/A/Zone",
    "",
    "../../etc/passwd",
])
def test_an_unusable_zone_falls_back_to_utc_instead_of_raising(nonsense):
    zone = resolve_zone(nonsense)

    assert zone is timezone.utc, (
        f"a base image with no tz database must cost the operator correct "
        f"local times, not the whole auto-run feature; {nonsense!r} resolved to "
        f"{zone!r}"
    )


def test_a_station_with_an_unusable_zone_still_gets_a_schedule():
    now = utc(2026, 9, 21, 12, 0)

    due = next_fire(mode="times", times=DEFAULT_SLOTS, interval_min=0,
                    tz="Mars/Olympus_Mons", now=now, last_run=None)

    assert due == utc(2026, 9, 21, 18, 0), (
        "with no usable zone the configured times are read as UTC and the "
        "station still books - wrong time of day, but running. Returning None "
        f"here would be a silent outage instead: got {due}"
    )


def test_the_configured_station_zone_resolves_on_this_machine():
    """Settings is constructed directly; `get_settings()` is lru_cache'd and
    would carry one test's environment into the next."""
    settings = Settings(timezone=BANGKOK)
    zone = resolve_zone(settings.timezone)

    assert zone is not timezone.utc, (
        f"{settings.timezone} fell back to UTC, which means this image has no "
        "tz database: every configured slot would run seven hours off, and the "
        "only warning is one log line at startup"
    )
    for month in (1, 7):
        offset = datetime(2026, month, 15, 12, tzinfo=zone).utcoffset()
        assert offset == timedelta(hours=7), (
            f"the station is UTC+7 all year round; the tz database gave "
            f"{offset} in month {month}"
        )


# --- the shape of every answer ----------------------------------------------
@pytest.mark.parametrize("schedule", [
    {"mode": "interval", "times": [], "interval_min": 180},
    {"mode": "interval", "times": [], "interval_min": 5},
    {"mode": "times", "times": DEFAULT_SLOTS, "interval_min": 0},
    {"mode": "times", "times": ["00:00", "23:59"], "interval_min": 0},
])
@pytest.mark.parametrize("tz", [BANGKOK, BERLIN, "UTC", "Mars/Olympus_Mons"])
def test_every_answer_is_an_aware_utc_instant(schedule, tz):
    # `now` is deliberately given in the station's local zone: the caller may
    # hand over anything aware, and the answer must still come back in UTC.
    now = datetime(2026, 9, 21, 19, 0, tzinfo=ICT)

    for last_run in (None, now - timedelta(hours=9), now - timedelta(minutes=2)):
        due = next_fire(tz=tz, now=now, last_run=last_run, **schedule)
        assert due is not None, (
            f"{schedule} with last_run={last_run} produced no schedule at all; "
            "every configuration here is one an operator can save in the panel"
        )
        assert_utc_instant(due, f"{schedule} in {tz} with last_run={last_run}")


# --- regressions found by review, after the first cut shipped ----------------

def test_a_small_backward_clock_step_does_not_re_offer_the_slot_that_just_fired():
    """The first fix for a future last_run introduced a worse bug.

    Clamping every future `last_run` back to `now` dropped the floor below the
    slot that had just run, so a ten-minute NTP correction backwards made
    `next_fire` offer that slot again - and with BOOK FOR REAL set, the loop
    would run it a second time, unattended, against a station that already had
    the bookings.
    """
    tz = "UTC"
    fired = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)
    # The clock has stepped back ten minutes; the recorded run is now "future".
    now = fired - timedelta(minutes=10)

    nxt = next_fire(
        mode="times", times=["06:00"], interval_min=180,
        tz=tz, now=now, last_run=fired,
    )
    assert nxt != fired, (
        "06:00 already ran - a backward clock step must not make it due again, "
        "or the station books the same window twice"
    )
    assert nxt == fired + timedelta(days=1), (
        f"it should wait for tomorrow's slot; it offered {nxt}"
    )


def test_a_wildly_future_last_run_is_discarded_rather_than_disabling_auto_run():
    """The bug the clamp was added for, still fixed.

    A `last_run` days ahead - a snapshot restore, a badly wrong RTC, a
    hand-edited config - would otherwise be a floor no candidate can clear, so
    next_fire returns None forever and the loop reads that as "auto-run is
    off". The station stops booking and nothing says why.
    """
    now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    nxt = next_fire(
        mode="times", times=["06:00", "18:00"], interval_min=180,
        tz="UTC", now=now, last_run=now + timedelta(days=400),
    )
    assert nxt is not None, (
        "a corrupt last-fire record must not silently switch auto-run off - "
        "the station would simply stop booking with no error anywhere"
    )
    assert nxt == datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc)


def test_a_newly_added_time_does_not_fire_retroactively():
    """Adding a time a few minutes ago must not book immediately.

    The 30-minute catch-up grace exists for a backend that was DOWN across a
    slot. Applied to a slot that has only just been configured, it fires a
    real booking run seconds after the operator presses SAVE, for a window
    they meant to start tomorrow.
    """
    now = datetime(2026, 9, 21, 12, 40, tzinfo=timezone.utc)
    saved_at = now - timedelta(seconds=5)

    without_guard = next_fire(
        mode="times", times=["12:30"], interval_min=180,
        tz="UTC", now=now, last_run=None,
    )
    assert without_guard <= now, (
        "sanity check: inside the grace window this slot really is due - that "
        "is the behaviour the guard below has to suppress"
    )

    guarded = next_fire(
        mode="times", times=["12:30"], interval_min=180,
        tz="UTC", now=now, last_run=None, not_before=saved_at,
    )
    assert guarded > now, (
        "a slot earlier than the moment these settings were saved was never "
        "missed - it was not configured yet - so it must not be caught up"
    )
    assert guarded == datetime(2026, 9, 22, 12, 30, tzinfo=timezone.utc)


def test_two_times_astride_the_spring_forward_gap_collapse_to_one_fire():
    """02:30 shifts onto 03:30's instant, so the day produces one run.

    This is deliberate, and it is the lesser of two evils. Skipping the
    nonexistent slot instead would avoid the collapse, but it would cost a
    station configured for only 02:30 a whole day of observations - see
    test_a_slot_in_the_hour_that_never_happened_still_runs_once, which pins
    that. Firing twice at one instant would be two bookings for one window.
    One fire is the only sane answer; nextfire logs a warning so it is not a
    silent surprise.
    """
    berlin = ZoneInfo("Europe/Berlin")
    gap_day_start = datetime(2026, 3, 28, 23, 0, tzinfo=timezone.utc)

    fires = []
    last = None
    now = gap_day_start
    for _ in range(5):
        nxt = next_fire(
            mode="times", times=["02:30", "03:30"], interval_min=180,
            tz="Europe/Berlin", now=now, last_run=last,
        )
        if nxt is None or nxt >= gap_day_start + timedelta(hours=24):
            break
        fires.append(nxt)
        last = nxt
        now = nxt + timedelta(seconds=1)

    assert len(fires) == 1, (
        f"the two slots share one instant on the transition day, so firing "
        f"more than once would book the same window twice; got {fires}"
    )
    local = fires[0].astimezone(berlin)
    assert (local.hour, local.minute) == (3, 30), (
        f"and the surviving instant is 03:30 local; got {local.strftime('%H:%M')}"
    )


def test_both_times_fire_on_an_ordinary_day():
    """The companion to the test above: nothing is lost on a normal day."""
    berlin = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 3, 21, 23, 0, tzinfo=timezone.utc)

    fires = []
    last = None
    now = start
    for _ in range(5):
        nxt = next_fire(
            mode="times", times=["02:30", "03:30"], interval_min=180,
            tz="Europe/Berlin", now=now, last_run=last,
        )
        if nxt is None or nxt >= start + timedelta(hours=24):
            break
        fires.append(nxt)
        last = nxt
        now = nxt + timedelta(seconds=1)

    local = [f.astimezone(berlin).strftime("%H:%M") for f in fires]
    assert local == ["02:30", "03:30"], (
        f"on a day with no transition both configured times must run; got {local}"
    )
