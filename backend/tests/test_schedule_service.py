"""The four mechanisms in ScheduleService that fail silently when they fail.

Nothing in this file touches the network or spawns a process, and two of the
tests exist specifically to prove that.

**The save_config convention is deliberately not uniform.** For most overrides
a falsy value CLEARS the key back to its default, because blanking the station
id box means "use the configured station", not "station 0". A boolean switch
cannot work that way: if `campaign_auto_commit_enabled=False` cleared the key,
then every time the operator turned unattended campaign booking OFF the next
read would find no key, fall back to the default, and turn it back ON. The same
trap sits under `min_culmination_deg=0` - the horizon is a real elevation, not
an absence - and under `start_lead_minutes=0`, `auto_run_enabled=False`,
`auto_run_dry_run=False` and an emptied `auto_run_times`. Which half of that
split a key belongs to is one entry in one dict literal, invisible at every
call site, so these tests name each key explicitly.

**GS_MOCK=1 has to be airtight.** A dev box that is mocked everywhere else but
quietly shells out to satnogs-auto-scheduler would talk to the live SatNOGS
Network with whatever token happened to be lying in the data dir - and with
`dry_run=False`, book real observations on a real station. So the mock tests
here break `asyncio.create_subprocess_exec` and `subprocess.run` outright: if
anything reaches for a child process, the test fails rather than the network.
The mock result also has to make DRY RUN and RUN NOW look different, because
otherwise the one control that decides whether real observations get booked is
the one control never exercised in development.

**Reconciliation is about honesty, not optimism.** The transcript alone cannot
say what is on the calendar: the tool logs its confirmation at DEBUG, and its
booking POST carries no timeout, so a run we killed may still have created
observations server-side. After a real run the service goes and looks. The
distinction that matters is between "SatNOGS says no" and "SatNOGS did not
answer": a failed booking is `failed`, but an empty or unreadable calendar is
`unconfirmed`. Absence of evidence is not evidence of failure, and telling an
operator a run failed when SatNOGS is merely lagging is how a pass gets booked
twice. The +/-90s match window is the other half of that: wide enough for clock
skew, narrow enough that the next pass of the same satellite is never mistaken
for this one.

**The auto-run timer must survive a restart.** `mark_auto_run` is the only
thing standing between a container restart and re-firing a slot that already
ran, and `wait_for_config_change` is the only thing that makes an edit in the
browser take effect without one.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.services.autoscheduler_cli import ParsedRun, RunOutcome, ScheduleRow
from app.services.schedule_service import ScheduleService

# 40 lowercase hex characters each - the shape validate_tokens() demands, so
# these are indistinguishable from real credentials to everything downstream.
DB_TOKEN = "a" * 40
NETWORK_TOKEN = "b1c2d3e4f5" * 4

MISSION = 67683
OTHER_SAT = 25544


def make_service(tmp_path, **overrides) -> ScheduleService:
    """A service on a throwaway data dir.

    Settings is constructed directly rather than through the lru_cache'd
    get_settings(), which would leak one test's data dir into the next.
    mock=True is what makes construction spawn nothing: the CLI version probe
    returns "mock" without running anything.
    """
    settings = Settings(data_dir=tmp_path, mock=True, **overrides)
    return ScheduleService(settings)


# --- hand-built stand-ins ---------------------------------------------------
class StubBooking:
    """One row of a station's SatNOGS calendar.

    _reconcile() reads exactly two attributes off a booking - the satellite and
    the start instant - so those two are all this implements. The real
    network_client.Booking also carries id, end and status; none of them
    participate in matching.
    """

    def __init__(self, norad_cat_id: int, start: datetime):
        self.norad_cat_id = norad_cat_id
        self.start = start


class StubCalendar:
    """Stands in for _future_bookings_sync, the one network read _reconcile does.

    Callable with no arguments, exactly as asyncio.to_thread invokes it.
    Returns a fixed list, or raises, and counts calls so a test can prove the
    dry-run path never reaches it.
    """

    def __init__(self, bookings=None, error: Exception | None = None):
        self.bookings = list(bookings or [])
        self.error = error
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.bookings)


class Exploded(AssertionError):
    """Raised by the guards that stand in for process spawning."""


def planned_row(norad: int, start: datetime) -> ScheduleRow:
    """A real ScheduleRow, so a rename upstream breaks this file loudly."""
    return ScheduleRow(
        norad=norad,
        start=start,
        end=start + timedelta(minutes=8),
        duration_s=480,
        az_rise=10.0,
        elevation=45.0,
        az_set=190.0,
        priority=1.0,
        transmitter_uuid="mock-transmitter",
        mode="GFSK",
        frequency_violator=False,
        name="KNACKSAT-2",
        already_scheduled=False,
    )


def parsed_with(rows, booked_log=None) -> ParsedRun:
    return ParsedRun(planned=list(rows), booked_log=booked_log)


def clean_outcome() -> RunOutcome:
    """A run the transcript gives no reason to call failed."""
    return RunOutcome(exit_code=0, failure=None)


def failed_outcome(code: str) -> RunOutcome:
    return RunOutcome(exit_code=1, failure=(code, "SatNOGS rejected the booking."))


_real_sleep = asyncio.sleep


@pytest.fixture
def no_replication_pause(monkeypatch):
    """Collapse the reconciler's 5s wait for SatNOGS's read API to catch up.

    Only that one delay: every other sleep still sleeps, so this cannot hide a
    timing bug in wait_for_config_change.
    """

    async def _sleep(delay, *args, **kwargs):
        return await _real_sleep(0 if delay == 5 else delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", _sleep)


# --- save_config: which keys clear and which do not -------------------------
async def test_a_falsy_value_clears_the_five_override_fields(tmp_path):
    """Blanking a box means "use the default", not "use zero"."""
    svc = make_service(tmp_path)
    await svc.save_config(
        station_id=9999,
        db_token=DB_TOKEN,
        network_token=NETWORK_TOKEN,
        campaign_max_per_station=7,
        campaign_max_total=99,
    )

    cleared = await svc.save_config(
        station_id=0,
        db_token="",
        network_token="",
        campaign_max_per_station=0,
        campaign_max_total=0,
    )

    assert cleared["station_id"] == svc.s.station_id, (
        "clearing the station id box must fall back to the station this "
        f"dashboard is configured for, not to {cleared['station_id']} - a run "
        "against the wrong station id books passes on somebody else's antenna"
    )
    assert cleared["station_id_is_override"] is False, (
        "the panel shows this as 'overridden'; leaving it true after a clear "
        "tells the operator a value is in force that is not"
    )
    assert cleared["db_token_set"] is False and cleared["network_token_set"] is False, (
        "a cleared token must read as unset, or the panel shows a green tick "
        "for a credential that is gone and the next run fails at the child"
    )
    assert cleared["campaign_max_per_station"] == svc.s.campaign_max_per_station, (
        "an emptied cap must revert to the configured backstop; a stored 0 "
        "would book nothing at all and look like a broken campaign"
    )
    assert cleared["campaign_max_total"] == svc.s.campaign_max_total, (
        "same for the total cap - 0 is how a caller says 'unset', so it can "
        "never be allowed to mean 'book zero observations'"
    )

    restart = make_service(tmp_path)
    assert (await restart.get_config())["station_id"] == svc.s.station_id, (
        "and the clear has to survive a restart, or the old override comes "
        "back the next time the container is rebuilt"
    )


async def test_turning_off_campaign_auto_commit_stores_false_rather_than_clearing(tmp_path):
    svc = make_service(tmp_path)
    await svc.save_config(campaign_auto_commit_enabled=True)

    off = await svc.save_config(campaign_auto_commit_enabled=False)

    assert off["campaign_auto_commit_enabled"] is False, (
        "turning unattended campaign booking OFF must persist as False. If a "
        "falsy value cleared this key instead, the next read would fall back "
        "to the default and silently re-enable unattended booking every single "
        "time the operator switched it off - the station would keep committing "
        "bookings to other people's ground stations with nobody watching"
    )
    assert svc._config["campaign_auto_commit_enabled"] is False, (
        "it has to be stored literally, not merely absent: an absent key is "
        "what re-enables the feature at the next default lookup"
    )

    restart = make_service(tmp_path)
    assert restart.campaign_auto_commit_enabled() is False, (
        "and OFF must still be OFF after a restart, which is exactly when an "
        "unattended booking run would otherwise fire"
    )


async def test_zero_is_a_real_value_for_the_horizon_and_the_start_lead(tmp_path):
    """0 degrees and 0 minutes are settings an operator can legitimately mean."""
    svc = make_service(tmp_path)

    saved = await svc.save_config(min_culmination_deg=0, start_lead_minutes=0)

    assert saved["min_culmination_deg"] == 0.0, (
        "0 degrees is the horizon, a real filter setting - if a falsy value "
        "cleared this the run would quietly go back to 3 degrees and drop "
        "every low pass the operator had just asked to keep"
    )
    assert saved["start_lead_minutes"] == 0, (
        "0 minutes means 'start planning from now'; reverting to 10 would put "
        "the planning window somewhere the operator did not ask for"
    )

    restart = make_service(tmp_path)
    after = await restart.get_config()
    assert after["min_culmination_deg"] == 0.0 and after["start_lead_minutes"] == 0, (
        "both have to survive a restart as zero rather than reappearing as "
        "the defaults"
    )


async def test_the_auto_run_switches_store_false_literally(tmp_path):
    svc = make_service(tmp_path)
    await svc.save_config(auto_run_enabled=True, auto_run_dry_run=True)

    off = await svc.save_config(auto_run_enabled=False, auto_run_dry_run=False)

    assert off["auto_run_enabled"] is False, (
        "switching the auto-run timer off must stick. Clearing the key would "
        "restore the default at the next read and the station would keep "
        "running itself on a schedule the operator believes is disabled"
    )
    assert off["auto_run_dry_run"] is False, (
        "auto_run_dry_run defaults to True so a fresh install cannot book "
        "unattended; a False that cleared itself would be read back as True "
        "and the operator's deliberate 'yes, really book these' would be "
        "silently downgraded to a dry run that records nothing"
    )
    assert svc.auto_run_dry_run() is False and svc.auto_run_enabled() is False


async def test_clearing_every_auto_run_time_does_not_restore_the_default_pair(tmp_path):
    """An empty schedule is a real state: the feature is on, no time chosen."""
    svc = make_service(tmp_path)
    await svc.save_config(auto_run_enabled=True, auto_run_times=["06:00", "18:00"])

    emptied = await svc.save_config(auto_run_times=[])

    assert svc._config["auto_run_times"] == [], (
        "an emptied list must be stored as empty, not cleared back to absent"
    )
    assert emptied["auto_run_times"] == [], (
        "with no times configured the timer will never fire (next_fire returns "
        "None for an empty slot list), so reporting the default 06:00/18:00 "
        "pair back to the panel shows the operator two scheduled runs that "
        "cannot happen - they will wait all day for a run nobody queued"
    )
    assert emptied["auto_run_next_utc"] is None, (
        "and the panel's 'next run' must agree with the times it is showing"
    )


async def test_an_unknown_setting_is_a_valueerror_not_a_silent_no_op(tmp_path):
    svc = make_service(tmp_path)

    with pytest.raises(ValueError) as caught:
        await svc.save_config(auto_run_enabled=True, auto_run_tiems=["06:00"])

    assert "auto_run_tiems" in str(caught.value), (
        "the route turns this into a 400 naming the key; without the name a "
        "typo in a client is a setting that silently never takes effect"
    )


async def test_get_config_never_echoes_a_token_back(tmp_path):
    """A token in a GET response ends up in a screenshot or a browser cache."""
    svc = make_service(tmp_path)
    await svc.save_config(db_token=DB_TOKEN, network_token=NETWORK_TOKEN)

    config = await svc.get_config()
    blob = json.dumps(config)

    assert DB_TOKEN not in blob, (
        "the SatNOGS DB token must never appear in the config response - it is "
        f"read by the browser on every panel load: {blob}"
    )
    assert NETWORK_TOKEN not in blob, (
        "the SatNOGS Network token is the credential that can book real "
        f"observations, and it must never leave the backend: {blob}"
    )
    assert config["db_token_set"] is True and config["network_token_set"] is True, (
        "the panel still has to be able to say a credential is present - that "
        "is what the booleans are for, and they are all it may say"
    )


# --- GS_MOCK=1 ---------------------------------------------------------------
async def test_mock_mode_never_spawns_the_scheduler(tmp_path, monkeypatch):
    """On a mocked dev box, RUN NOW must not reach the real SatNOGS Network."""

    def never(*args, **kwargs):
        raise Exploded("GS_MOCK=1 tried to spawn satnogs-auto-scheduler")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", never)
    svc = make_service(tmp_path)

    dry = await svc.run_plan(dry_run=True)
    real = await svc.run_plan(dry_run=False)

    for label, result in (("DRY RUN", dry), ("RUN NOW", real)):
        assert result["status"] != "error", (
            f"{label} under GS_MOCK=1 came back as an error, which is how a "
            f"blocked spawn shows up here - mock mode must answer from the "
            f"canned plan and never try to start a child: {result.get('error')}"
        )
        assert "Simulated run" in " ".join(result["log_tail"]), (
            f"{label} did not return the canned mock plan, so something other "
            "than _mock_result() produced this - the mock switch is not the "
            "single gate it is supposed to be"
        )


def test_mock_mode_skips_the_startup_version_probe(tmp_path, monkeypatch):
    """Constructing the service must not shell out either."""

    def never(*args, **kwargs):
        raise Exploded("GS_MOCK=1 probed the CLI version with a subprocess")

    monkeypatch.setattr(subprocess, "run", never)

    svc = make_service(tmp_path)

    assert svc.cli_version == "mock", (
        "under GS_MOCK=1 the version probe must short-circuit before it runs "
        "anything; a dev box without satnogs-auto-scheduler installed would "
        "otherwise pay a 15s timeout on every backend start"
    )


async def test_dry_run_and_run_now_produce_visibly_different_results(tmp_path, monkeypatch):
    """The one control that decides whether real observations get booked."""

    def never(*args, **kwargs):
        raise Exploded("the mock path spawned a process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", never)
    svc = make_service(tmp_path)

    dry = await svc.run_plan(dry_run=True)
    real = await svc.run_plan(dry_run=False)

    assert dry["dry_run"] is True and real["dry_run"] is False
    assert dry["booked_state"] == "dry_run", (
        "a dry run books nothing, so it must say dry_run - reporting it as "
        "confirmed would put a green 'booked' chip on a run that created no "
        "observations at all"
    )
    assert dry["booked"] == 0, (
        "and it must count zero bookings, or the operator reads the dashboard "
        "as proof the pass is on the calendar when nothing was ever sent"
    )
    # RUN NOW must still be visibly different from DRY RUN - otherwise the one
    # control that decides whether real observations get booked is the one
    # control never exercised in development. But it must NOT claim a booking:
    # a simulated run returns before anything is spawned, so nothing reaches
    # SatNOGS whichever button was pressed. Reporting "confirmed" here is what
    # put a red "BOOKED 1 of 1" on a stack that had booked nothing at all.
    assert real["booked_state"] == "mock", (
        "a simulated RUN NOW must report its own state, not 'confirmed' - "
        "claiming a booking that never happened is worse than any amount of "
        "missing dev fidelity, because the operator acts on it"
    )
    assert real["booked"] == 0, (
        "nothing was sent to SatNOGS, so the count has to be zero; a non-zero "
        "count here is a booking the operator will believe exists"
    )
    assert real["booked_state"] != dry["booked_state"], (
        "the two buttons must still be distinguishable in development, or the "
        "booking path is never exercised before it is pointed at a live station"
    )
    assert any("Simulated run" in n["message"] for n in real["notices"]), (
        "and the panel has to say plainly that nothing was booked, on the run "
        "itself, not only in a log tail nobody opens"
    )


def test_the_header_chip_never_calls_a_dry_run_a_booking(tmp_path):
    svc = make_service(tmp_path)

    dry = ScheduleService._state_detail(svc._mock_result(dry_run=True))
    real = ScheduleService._state_detail(svc._mock_result(dry_run=False))

    assert "dry run" in dry.lower(), (
        f"the status chip after a dry run has to say so; it said {dry!r}"
    )
    assert "booked" not in dry.lower(), (
        f"a dry run created no observations, so the chip must not contain the "
        f"word 'booked' - an operator reading {dry!r} would believe the passes "
        f"are on the station's calendar and would not run the real booking"
    )
    assert "booked" in real.lower(), (
        f"and a confirmed run must say what it booked; it said {real!r}"
    )


# --- wait_for_config_change --------------------------------------------------
async def test_a_save_wakes_the_auto_run_loop_immediately(tmp_path):
    """A settings change must take effect without restarting the process."""
    svc = make_service(tmp_path)
    timeout = 30.0

    started = time.monotonic()
    waiter = asyncio.create_task(svc.wait_for_config_change(timeout))
    await asyncio.sleep(0)
    await svc.save_config(auto_run_times=["07:30"])
    woke = await asyncio.wait_for(waiter, timeout=5.0)
    elapsed = time.monotonic() - started

    assert woke is True, (
        "the auto-run loop sits in this wait between runs. If a save does not "
        "wake it, the new schedule does not take effect until the current "
        "sleep ends - up to the whole poll interval - and the operator watches "
        "the old times keep firing after they changed them"
    )
    assert elapsed < 1.0, (
        f"it woke only after {elapsed:.1f}s of a {timeout:.0f}s timeout, which "
        "means it slept out the wait rather than being woken by the save"
    )


async def test_waiting_past_the_timeout_returns_false_rather_than_raising(tmp_path):
    svc = make_service(tmp_path)

    woke = await svc.wait_for_config_change(0.05)

    assert woke is False, (
        "an ordinary timeout is the normal case - it is how the loop rechecks "
        "the clock - and it must be a plain False. A TimeoutError escaping "
        "here would kill the auto-run task and the station would stop running "
        "itself with nothing in the UI to say so"
    )


# --- reconciliation ----------------------------------------------------------
async def test_a_booking_at_the_planned_start_is_confirmed(tmp_path, monkeypatch, no_replication_pause):
    svc = make_service(tmp_path)
    start = datetime(2026, 9, 21, 7, 13, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(svc, "_future_bookings_sync", StubCalendar([StubBooking(MISSION, start)]))

    booked, state, notices = await svc._reconcile(
        parsed_with([planned_row(MISSION, start)]), clean_outcome(), dry_run=False
    )

    assert (booked, state) == (1, "confirmed"), (
        "the pass we planned is on the station's calendar at the instant we "
        f"planned it, which is the only evidence that it will be recorded; got "
        f"{booked} / {state!r}"
    )
    assert notices == [], "a fully confirmed run has nothing to warn about"


@pytest.mark.parametrize("offset_s, matches", [(0, True), (89, True), (-89, True),
                                               (90, True), (91, False), (-91, False)])
async def test_the_match_window_is_ninety_seconds_either_way(
    tmp_path, monkeypatch, no_replication_pause, offset_s, matches
):
    """Wide enough for clock skew and SatNOGS rounding, no wider.

    The tool books exactly the start it printed, so anything further out than
    this is a different observation - and matching it would report a pass
    confirmed on the strength of somebody else's booking.
    """
    svc = make_service(tmp_path)
    start = datetime(2026, 9, 21, 7, 13, 20, tzinfo=timezone.utc)
    booking = StubBooking(MISSION, start + timedelta(seconds=offset_s))
    monkeypatch.setattr(svc, "_future_bookings_sync", StubCalendar([booking]))

    booked, state, _ = await svc._reconcile(
        parsed_with([planned_row(MISSION, start)]), clean_outcome(), dry_run=False
    )

    if matches:
        assert (booked, state) == (1, "confirmed"), (
            f"a booking {offset_s}s from the planned start is the same "
            "observation - clocks and SatNOGS rounding move it by seconds, and "
            "calling it unconfirmed invites the operator to book it a second time"
        )
    else:
        assert (booked, state) == (0, "unconfirmed"), (
            f"a booking {offset_s}s away is past the tolerance and must not be "
            "credited to this run; the next pass of the same satellite is a "
            "different observation and confirming against it would hide a "
            "booking that never landed"
        )


async def test_the_right_satellite_at_the_wrong_time_does_not_count(
    tmp_path, monkeypatch, no_replication_pause
):
    svc = make_service(tmp_path)
    start = datetime(2026, 9, 21, 7, 13, 20, tzinfo=timezone.utc)
    later = StubBooking(MISSION, start + timedelta(hours=3))
    monkeypatch.setattr(svc, "_future_bookings_sync", StubCalendar([later]))

    booked, state, _ = await svc._reconcile(
        parsed_with([planned_row(MISSION, start)]), clean_outcome(), dry_run=False
    )

    assert (booked, state) == (0, "unconfirmed"), (
        "the station already had a later pass of this satellite on its "
        "calendar; counting it would report our booking confirmed on the "
        "strength of an observation this run did not create"
    )


async def test_the_right_time_for_the_wrong_satellite_does_not_count(
    tmp_path, monkeypatch, no_replication_pause
):
    svc = make_service(tmp_path)
    start = datetime(2026, 9, 21, 7, 13, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(svc, "_future_bookings_sync", StubCalendar([StubBooking(OTHER_SAT, start)]))

    booked, state, _ = await svc._reconcile(
        parsed_with([planned_row(MISSION, start)]), clean_outcome(), dry_run=False
    )

    assert (booked, state) == (0, "unconfirmed"), (
        "an overlapping slot booked for a different satellite is somebody "
        "else's observation - the dish cannot record both, and reporting ours "
        "confirmed would hide a booking that never landed"
    )


async def test_a_partly_confirmed_run_is_partial_and_says_so(
    tmp_path, monkeypatch, no_replication_pause
):
    svc = make_service(tmp_path)
    # Relative to the real clock on purpose. SatNOGS's upcoming-passes feed
    # cannot list a pass that has already started, so a pass dated in the past
    # is unverifiable rather than missing - and a fixed 2026 timestamp becomes
    # exactly that the day after it is written.
    first = datetime.now(timezone.utc) + timedelta(hours=1)
    second = first + timedelta(hours=2)
    monkeypatch.setattr(svc, "_future_bookings_sync", StubCalendar([StubBooking(MISSION, first)]))

    booked, state, notices = await svc._reconcile(
        parsed_with([planned_row(MISSION, first), planned_row(MISSION, second)]),
        clean_outcome(),
        dry_run=False,
    )

    assert (booked, state) == (1, "partial"), (
        "one of two planned passes is on the calendar; calling that confirmed "
        "would leave the operator believing a pass is covered when nothing "
        "will be recording during it"
    )
    assert notices and notices[0]["severity"] == "warning", (
        "and a partial result is the one the operator has to act on, so it has "
        "to arrive as a visible warning rather than only a state string"
    )


async def test_an_empty_calendar_is_unconfirmed_never_failed(
    tmp_path, monkeypatch, no_replication_pause
):
    """Absence of evidence is not evidence of failure."""
    svc = make_service(tmp_path)
    start = datetime(2026, 9, 21, 7, 13, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(svc, "_future_bookings_sync", StubCalendar([]))

    booked, state, notices = await svc._reconcile(
        parsed_with([planned_row(MISSION, start)]), clean_outcome(), dry_run=False
    )

    assert booked == 0
    assert state == "unconfirmed", (
        f"nothing was found on the calendar, but the run itself reported no "
        f"failure - SatNOGS's read API trails its write API. Reporting {state!r} "
        "as 'failed' tells the operator to run again, and if the bookings were "
        "merely lagging they now have every pass booked twice"
    )
    assert notices and "network.satnogs.org" in notices[0]["message"], (
        "and the warning has to send the operator to look for themselves "
        "before they rerun anything"
    )


async def test_a_failed_batch_still_reconciles_because_upstream_retries_singly(
    tmp_path, monkeypatch, no_replication_pause
):
    """A rejected batch is NOT evidence that nothing was booked.

    satnogs-auto-scheduler reacts to a failed batch POST by re-posting every
    observation one at a time - `logger.info("Fall-back to single-pass
    scheduling...")` followed by a `schedule_observation()` per pass. So
    "Failed to batch-schedule observations." routinely appears in runs that
    went on to book most of their passes.

    Treating it as a definite failure skipped the SatNOGS read entirely and
    reported "nothing booked" for a run that had booked. The operator's
    obvious next move - run it again - would then double-book every pass that
    did land.
    """
    svc = make_service(tmp_path)
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    calendar = StubCalendar([StubBooking(MISSION, start)])
    monkeypatch.setattr(svc, "_future_bookings_sync", calendar)

    booked, state, _ = await svc._reconcile(
        parsed_with([planned_row(MISSION, start)]),
        failed_outcome("batch_failed"),
        dry_run=False,
    )

    assert calendar.calls == 1, (
        "a booking-stage failure is the case where reading the calendar back "
        "matters most, not one where it can be skipped"
    )
    assert (booked, state) == (1, "confirmed"), (
        "the pass is on the station's calendar, so it was booked by the "
        "single-pass fallback however the batch went; reporting 'failed' here "
        "invites the operator to book it a second time"
    )


async def test_a_prelaunch_failure_is_failed_without_a_calendar_read(
    tmp_path, monkeypatch, no_replication_pause
):
    """The contrast that makes the test above meaningful.

    A token or station failure happens before the tool could post anything, so
    there is genuinely nothing to look for and no reason to spend a request.
    """
    svc = make_service(tmp_path)
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    calendar = StubCalendar([])
    monkeypatch.setattr(svc, "_future_bookings_sync", calendar)

    booked, state, _ = await svc._reconcile(
        parsed_with([planned_row(MISSION, start)]),
        failed_outcome("station_offline"),
        dry_run=False,
    )

    assert (booked, state) == (0, "failed"), (
        "the station was refused before any observation was posted, so this "
        "is a definite answer and must not be softened to 'unconfirmed'"
    )
    assert calendar.calls == 0, (
        "and nothing can have been booked, so it must not cost a SatNOGS read"
    )


async def test_a_calendar_read_that_fails_leaves_the_run_standing(
    tmp_path, monkeypatch, no_replication_pause
):
    svc = make_service(tmp_path)
    start = datetime(2026, 9, 21, 7, 13, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(
        svc,
        "_future_bookings_sync",
        StubCalendar(error=RuntimeError("connection reset by peer")),
    )

    booked, state, notices = await svc._reconcile(
        parsed_with([planned_row(MISSION, start)], booked_log=1),
        clean_outcome(),
        dry_run=False,
    )

    assert state == "unconfirmed", (
        "failing to READ the calendar says nothing whatsoever about what is on "
        f"it; {state!r} must not be 'failed', or a SatNOGS outage turns every "
        "successful booking into a run the operator is told to repeat"
    )
    assert booked == 1, (
        "the transcript said one pass was scheduled and that is still the best "
        "evidence we have - throwing it away would under-report real bookings"
    )
    assert len(notices) == 1 and notices[0]["severity"] == "warning", (
        "the operator has to be told the check did not happen, as a warning "
        "and not an error: the run itself was fine"
    )
    assert "connection reset by peer" in notices[0]["message"], (
        "and the actual reason has to reach the panel, or diagnosing it means "
        "reading backend logs nobody has access to"
    )


async def test_a_dry_run_never_reads_the_calendar_at_all(tmp_path, monkeypatch):
    svc = make_service(tmp_path)
    start = datetime(2026, 9, 21, 7, 13, 20, tzinfo=timezone.utc)
    calendar = StubCalendar([StubBooking(MISSION, start)])
    monkeypatch.setattr(svc, "_future_bookings_sync", calendar)

    result = await svc._reconcile(
        parsed_with([planned_row(MISSION, start)]), clean_outcome(), dry_run=True
    )

    assert result == (0, "dry_run", []), (
        "a dry run booked nothing, so there is nothing to confirm; returning "
        f"anything else here would put booking language on a preview: {result}"
    )
    assert calendar.calls == 0, (
        "and it must not cost a SatNOGS request either - a dry run is the "
        "cheap, safe preview, and the reconciler's 5s pause plus a calendar "
        "read would make every preview slower for no information"
    )


# --- the auto-run timer ------------------------------------------------------
async def test_next_auto_run_is_none_while_the_timer_is_off(tmp_path):
    svc = make_service(tmp_path, timezone="UTC")
    await svc.save_config(auto_run_enabled=False, auto_run_times=["06:00", "18:00"])

    assert svc.next_auto_run() is None, (
        "times can stay configured while the feature is switched off - that is "
        "how an operator pauses it without losing their schedule. Returning a "
        "time here would have the station run itself while the panel shows the "
        "timer disabled"
    )


async def test_next_auto_run_is_a_real_instant_once_enabled(tmp_path):
    svc = make_service(tmp_path, timezone="UTC")
    await svc.save_config(auto_run_enabled=True, auto_run_times=["06:00"])

    now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    nxt = svc.next_auto_run(now=now)

    assert nxt == datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc), (
        "with 06:00 configured and the clock at noon, the next run is tomorrow "
        f"morning; {nxt} means the panel's countdown and the loop's own sleep "
        "disagree about when the station will next book anything"
    )


async def test_a_fired_slot_is_remembered_across_a_restart(tmp_path):
    """Otherwise a container restart re-runs the slot that just ran."""
    svc = make_service(tmp_path, timezone="UTC")
    await svc.save_config(auto_run_enabled=True, auto_run_times=["06:00"])
    fired = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)
    await svc.mark_auto_run(fired)

    just_after = fired + timedelta(seconds=30)
    restarted = make_service(tmp_path, timezone="UTC")

    assert restarted._last_auto_fire() == fired, (
        "the last fire time has to be on disk, not only in memory - it is read "
        "exactly once, by a process that has just started"
    )
    assert restarted.next_auto_run(now=just_after) == fired + timedelta(days=1), (
        "thirty seconds after the 06:00 slot ran, a restarted backend must "
        "wait for tomorrow's slot. Re-firing today's would book the same "
        "window again, and SatNOGS would either reject the duplicates or the "
        "station would record the same pass twice"
    )

    forgetful = make_service(tmp_path / "no_memory", timezone="UTC")
    await forgetful.save_config(auto_run_enabled=True, auto_run_times=["06:00"])
    # save_config stamps auto_run_changed_utc from the real clock, so that a
    # slot predating the operator's edit is not "missed" and caught up. This
    # test injects a `now` in the past, which production never does, so the
    # stamp has to be moved into the injected world or it would suppress the
    # very slot under test.
    forgetful._config["auto_run_changed_utc"] = (fired - timedelta(days=1)).isoformat()
    assert forgetful.next_auto_run(now=just_after) == fired, (
        "sanity check on the assertion above: with no record of the slot "
        "having fired, the grace window makes it due immediately - which is "
        "precisely the re-fire that mark_auto_run() exists to prevent"
    )


# --- regressions found by review, after the first cut shipped ----------------

async def test_offline_mode_refuses_to_run_rather_than_booking_live(tmp_path, monkeypatch):
    """GS_OFFLINE=1 must not book real observations.

    The flag means "serve fixtures instead of hitting the internet", and every
    other SatNOGS-touching call in this service honours it. The child process
    cannot: satnogs-auto-scheduler has no offline mode and would go straight
    to the live API. Spawning it anyway meant an operator who had explicitly
    told the dashboard to stay off the network could still book real radio
    time by pressing one button.
    """
    def never(*args, **kwargs):
        raise Exploded("GS_OFFLINE=1 spawned the scheduler")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", never)
    # schedule_mock=False takes the run past the mock short-circuit, which is
    # the whole point: the offline guard has to be what stops it. The startup
    # version probe uses the blocking subprocess API, so it gets a harmless
    # stand-in rather than the exploding one - it is not what this test is
    # about, and letting it spawn a real interpreter would just be slow.
    class FakeProbe:
        """Only what _probe_cli_version reads off a completed process."""
        returncode = 0
        stdout = "satnogs-auto-scheduler 0.5.dev17+g0f7ec0177"
        stderr = ""

    monkeypatch.setattr(
        "app.services.schedule_service.subprocess.run",
        lambda *a, **k: FakeProbe(),
        raising=True,
    )
    svc = make_service(tmp_path, offline=True, schedule_mock=False)

    result = await svc.run_plan(dry_run=False)

    assert result["status"] == "error", (
        "an offline dashboard must refuse the run outright, not book against "
        "the live SatNOGS API"
    )
    assert "GS_OFFLINE" in (result.get("error") or ""), (
        f"and it has to say which switch stopped it, or the operator sees an "
        f"unexplained failure: {result.get('error')!r}"
    )
    assert result["booked"] == 0 and result["booked_state"] == "failed"


async def test_a_collision_does_not_consume_the_auto_run_slot(tmp_path, monkeypatch):
    """A manual run in flight must not silently eat a scheduled booking.

    mark_auto_run() is written BEFORE run_plan so that a crash mid-run cannot
    re-fire the slot. But run_plan refuses outright when a run is already in
    progress, and the mark was being kept anyway - so a cold-cache DRY RUN
    started at 05:58 would consume the 06:00 booking slot, nothing would be
    booked, and nothing anywhere would record that the slot had been skipped.
    """
    svc = make_service(tmp_path, timezone="UTC")
    await svc.save_config(auto_run_enabled=True, auto_run_times=["06:00"])
    original = svc._config.get("auto_run_last_fire_utc")

    fired = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)
    previous = await svc.mark_auto_run(fired)
    assert svc._last_auto_fire() == fired

    # run_plan refuses; the slot did not actually happen.
    await svc.restore_auto_run_mark(previous)

    assert svc._config.get("auto_run_last_fire_utc") == original, (
        "the slot must be left unfired so the loop retries it; keeping the "
        "mark loses a scheduled booking with no error anywhere"
    )
    reloaded = make_service(tmp_path, timezone="UTC")
    assert reloaded._config.get("auto_run_last_fire_utc") == original, (
        "and the restore has to reach disk, or a restart still sees the slot "
        "as fired"
    )


async def test_saving_the_auto_run_form_does_not_fire_a_past_slot(tmp_path):
    """Adding a time a few minutes ago must not book immediately.

    The 30-minute catch-up grace exists for a backend that was DOWN across a
    slot. Applied to a slot that has only just been configured, it made
    _schedule_loop fire a REAL booking run seconds after the operator pressed
    SAVE, for a window they meant to start the following day.
    """
    svc = make_service(tmp_path, timezone="UTC")
    now = datetime.now(timezone.utc)
    just_gone = (now - timedelta(minutes=10)).strftime("%H:%M")

    await svc.save_config(
        auto_run_enabled=True, auto_run_mode="times", auto_run_times=[just_gone]
    )

    nxt = svc.next_auto_run(now=now)
    assert nxt is not None and nxt > now, (
        f"a slot earlier than the moment these settings were saved was never "
        f"missed - it was not configured yet - so it must not be due now; "
        f"next_auto_run returned {nxt} against now={now}"
    )


async def test_a_second_run_is_refused_while_one_is_in_flight(tmp_path, monkeypatch):
    """run_plan must refuse, and say so, rather than starting a second run.

    The panel relies on this answer: `POST /api/schedule/run` returns
    `{"status": "running"}` and starts nothing, and the frontend has to notice
    that rather than polling until the OTHER run finishes and presenting its
    result as the outcome of the click. After pressing BOOK FOR REAL, being
    shown someone else's run as though it were yours is the worst available
    outcome short of a double booking.
    """
    svc = make_service(tmp_path)

    released = asyncio.Event()

    async def slow(*args, **kwargs):
        await released.wait()
        return svc._mock_result(dry_run=True)

    monkeypatch.setattr(svc, "_execute_run", slow)

    first = asyncio.create_task(svc.run_plan(dry_run=True))
    await asyncio.sleep(0)          # let it take the guard
    while not svc.is_running():
        await asyncio.sleep(0)

    second = await svc.run_plan(dry_run=False)
    assert second == {"status": "running"}, (
        f"a colliding run must be refused with the running marker so the "
        f"caller knows nothing started; got {second!r}"
    )

    released.set()
    await first
    assert not svc.is_running()


async def test_the_stored_result_never_carries_status_running(tmp_path):
    """The panel's in-progress branch cannot key off the stored file.

    get_last_run() returns what is on disk, and 'running' is never written
    there - it is only ever the live flag the route adds. A frontend testing
    run.status === 'running' therefore showed the PREVIOUS run as current,
    with both run buttons enabled, while a real booking run was in flight.
    """
    svc = make_service(tmp_path)
    await svc.run_plan(dry_run=True)

    stored = svc.get_last_run()
    assert stored["status"] != "running", (
        "nothing writes 'running' to schedule_last_run.json, so anything that "
        "looks for it there will never find it"
    )
    assert svc.is_running() is False
