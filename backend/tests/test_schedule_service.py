"""The mechanisms in ScheduleService that fail silently when they fail.

Nothing in this file touches the network or spawns a process: conftest.py
stubs the version probe and makes any attempt to spawn the scheduler raise.

**The save_config convention is deliberately not uniform.** For most overrides
a falsy value CLEARS the key back to its default, because blanking the station
id box means "use the configured station", not "station 0". A boolean switch
cannot work that way: if `campaign_auto_commit_enabled=False` cleared the key,
then every time the operator turned unattended campaign booking OFF the next
read would find no key, fall back to the default, and turn it back ON. The same
trap sits under `min_culmination_deg=0` - the horizon is a real elevation, not
an absence - and under `start_lead_minutes=0`, `auto_run_enabled=False`,
`auto_run_chain_campaign=False` and an emptied `auto_run_times`. Which half of that
split a key belongs to is one entry in one dict literal, invisible at every
call site, so these tests name each key explicitly.

**Only real runs exist.** There is no dry run and no simulated run: every
run spawns the real satnogs-auto-scheduler and books, whatever GS_MOCK says.
What stops a run is the offline, not-installed and token guards, and those are
tested here. So is the cleanup of what the removed modes left behind: a stored
simulated or dry-run result must never reach the board, and a config whose
auto-run only ever previewed must not wake up booking for real.

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
import time
from datetime import datetime, timedelta, timezone

import pytest

from pydantic import ValidationError

from app import scheduler as scheduler_module
from app.config import Settings
from app.schemas import ScheduleConfigUpdate, ScheduleRunRequest
from app.services import autoscheduler_cli
from app.services.autoscheduler_cli import ParsedRun, RunOutcome, ScheduleRow
from app.services.schedule_service import ScheduleService

# Captured at import, before conftest's autouse fixture stubs it, so the probe
# tests can call the real one against a faked subprocess.run.
_REAL_PROBE = ScheduleService.__dict__["_probe_cli_version"]

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
    mock=True matches the dev stack (rotator and cameras simulated) and has no
    effect on the schedule itself; conftest stubs the CLI version probe.
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
    Returns a fixed list, or raises, and counts calls so a test can prove a
    pre-launch failure never reaches it.
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
        transmitter_uuid="test-transmitter",
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


async def test_the_auto_run_switch_stores_false_literally(tmp_path):
    svc = make_service(tmp_path)
    await svc.save_config(auto_run_enabled=True)

    off = await svc.save_config(auto_run_enabled=False)

    assert off["auto_run_enabled"] is False, (
        "switching the auto-run timer off must stick. Clearing the key would "
        "restore the default at the next read and the station would keep "
        "running itself on a schedule the operator believes is disabled"
    )
    assert svc._config["auto_run_enabled"] is False
    assert svc.auto_run_enabled() is False


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


# --- campaign and chain settings --------------------------------------------
async def test_chaining_the_campaign_is_off_until_switched_on_and_off_sticks(tmp_path):
    """auto_run_chain_campaign books on community stations unattended at every
    auto-run slot, so it ships off and OFF must persist literally."""
    svc = make_service(tmp_path)
    assert (await svc.get_config())["auto_run_chain_campaign"] is False
    assert svc.auto_run_chain_campaign() is False

    on = await svc.save_config(
        **ScheduleConfigUpdate(auto_run_chain_campaign=True).model_dump(exclude_unset=True))
    assert on["auto_run_chain_campaign"] is True
    assert make_service(tmp_path).auto_run_chain_campaign() is True, "survives a restart"

    off = await svc.save_config(auto_run_chain_campaign=False)
    assert off["auto_run_chain_campaign"] is False
    assert svc._config["auto_run_chain_campaign"] is False, (
        "stored as False, not cleared - a cleared key is a switch that can come back on")
    assert make_service(tmp_path).auto_run_chain_campaign() is False


async def test_saving_another_setting_leaves_the_chain_toggle_alone(tmp_path):
    svc = make_service(tmp_path)
    await svc.save_config(auto_run_chain_campaign=True)

    await svc.save_config(**ScheduleConfigUpdate(auto_run_times=["11:00"]).model_dump(
        exclude_unset=True))

    assert svc.auto_run_chain_campaign() is True


async def test_the_downlink_policy_round_trips_and_clears_to_its_seed(tmp_path):
    svc = make_service(tmp_path)
    assert (await svc.get_config())["campaign_transmitter_policy"] == "preferred"

    saved = await svc.save_config(campaign_transmitter_policy="pinned")
    assert saved["campaign_transmitter_policy"] == "pinned"
    assert make_service(tmp_path).campaign_transmitter_policy() == "pinned"

    cleared = await svc.save_config(campaign_transmitter_policy="")
    assert cleared["campaign_transmitter_policy"] == "preferred", "back to the GS_ seed"

    # The primary and the fallbacks are reported so the panel can say what a
    # policy means - read-only, from the environment.
    assert saved["campaign_transmitter_uuid"] == "UatCXtfDnoBPeVBGHgj4Bc"
    assert saved["campaign_fallback_transmitter_uuids"] == ["JR28wAEjmpuDQ4FrPWAiwf"]


def test_an_unrecognised_stored_policy_falls_back_and_never_widens(tmp_path):
    svc = make_service(tmp_path)
    svc._config["campaign_transmitter_policy"] = "everything"
    assert svc.campaign_transmitter_policy() == "preferred", "the seed"

    seeded_badly = make_service(tmp_path / "b", campaign_transmitter_policy="bogus")
    seeded_badly._config["campaign_transmitter_policy"] = "everything"
    assert seeded_badly.campaign_transmitter_policy() == "pinned", (
        "a typo may shrink a campaign onto telemetry only, never widen it")


@pytest.mark.parametrize("body", [
    {"campaign_transmitter_policy": "everything"},
    {"campaign_max_total": 2001},
    {"campaign_max_total": -1},
    {"campaign_max_per_station": 13},
    {"campaign_max_per_station": -1},
    {"auto_run_chain_campaign": "maybe"},
])
def test_campaign_settings_out_of_range_are_refused(body):
    with pytest.raises(ValidationError):
        ScheduleConfigUpdate.model_validate(body)


@pytest.mark.parametrize("body", [
    {"campaign_transmitter_policy": "any"},
    {"campaign_max_total": 0},      # the "clear to default" sentinel
    {"campaign_max_total": 2000},
    {"campaign_max_per_station": 0},
    {"campaign_max_per_station": 12},
    {"auto_run_chain_campaign": False},
])
def test_campaign_settings_in_range_are_accepted(body):
    assert ScheduleConfigUpdate.model_validate(body).model_dump(exclude_unset=True) == body


# --- only real runs ------------------------------------------------------------
async def test_every_run_calls_the_real_tool_even_under_gs_mock(
    tmp_path, monkeypatch, no_replication_pause
):
    """GS_MOCK=1 simulates the rotator and cameras - never the schedule.

    This is the whole point of the change that removed the mock and dry-run
    modes: on this station GS_MOCK=1 is permanent, and it used to make every
    run - the auto-run timer's included - answer from a canned plan. The
    board showed a booking that did not exist, and nothing was ever booked.
    """
    svc = make_service(tmp_path)
    assert svc.s.mock is True
    await svc.save_config(db_token=DB_TOKEN, network_token=NETWORK_TOKEN)
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    seen = {}

    async def fake_run(cfg, on_line=None, log_path=None):
        seen["argv"] = autoscheduler_cli.build_argv(cfg)
        return RunOutcome(exit_code=0, parsed=parsed_with([planned_row(MISSION, start)]))

    monkeypatch.setattr(autoscheduler_cli, "run", fake_run)
    monkeypatch.setattr(svc, "_resolve_priorities_sync", lambda: ([], []))
    monkeypatch.setattr(
        svc, "_enrich_rows_sync", lambda rows: [svc._row_payload(r, {}) for r in rows]
    )
    monkeypatch.setattr(
        svc, "_future_bookings_sync", StubCalendar([StubBooking(MISSION, start)])
    )

    result = await svc.run_plan()

    assert "argv" in seen, (
        "under GS_MOCK=1 the run never reached satnogs-auto-scheduler, so the "
        "schedule is still being simulated - which is exactly the bug"
    )
    assert "-n" not in seen["argv"] and "--dryrun" not in seen["argv"], (
        f"every run books; a dry-run flag on the command line means nothing "
        f"would be: {seen['argv']}"
    )
    assert (result["booked"], result["booked_state"]) == (1, "confirmed"), (
        f"the planned pass is on the calendar, so the run booked it; got "
        f"{result['booked']} / {result['booked_state']!r}"
    )
    assert "dry_run" not in result, (
        "there is no dry run any more, so a result must not carry the flag - "
        "the panel and the startup purge both read it"
    )
    assert result["trigger"] == "manual"


class FakeCompleted:
    """Only what _probe_cli_version reads off a completed process."""

    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.mark.parametrize(
    "behaviour, expected",
    [
        (FakeCompleted(0, "satnogs-auto-scheduler 0.5.dev17+g0f7ec0177\n"),
         "satnogs-auto-scheduler 0.5.dev17+g0f7ec0177"),
        (FakeCompleted(1, stderr="ModuleNotFoundError: No module named 'auto_scheduler'"),
         "NOT INSTALLED - rebuild the backend image"),
        (OSError("exec format error"), "probe failed"),
    ],
    ids=["installed", "not-installed", "probe-failed"],
)
def test_the_version_probe_always_runs_and_says_what_the_image_has(
    tmp_path, monkeypatch, behaviour, expected
):
    """No mock short-circuit: this is what says whether the image built right."""
    svc = make_service(tmp_path)

    def fake_run(*args, **kwargs):
        if isinstance(behaviour, Exception):
            raise behaviour
        return behaviour

    monkeypatch.setattr("app.services.schedule_service.subprocess.run", fake_run)

    assert _REAL_PROBE(svc) == expected, (
        "GET /api/schedule/config shows this string, and it is the one-glance "
        "answer to 'can this backend book at all?'"
    )


async def test_a_missing_tool_refuses_the_run_without_spawning(tmp_path, monkeypatch):
    """An image built before the dependency must say so, not crash a run."""
    svc = make_service(tmp_path)
    await svc.save_config(db_token=DB_TOKEN, network_token=NETWORK_TOKEN)
    svc.cli_version = "NOT INSTALLED - rebuild the backend image"

    async def never(*args, **kwargs):
        raise Exploded("the not-installed guard let a run through to the tool")

    monkeypatch.setattr(autoscheduler_cli, "run", never)

    result = await svc.run_plan()

    assert result["status"] == "error" and result["booked_state"] == "failed", (
        "nothing can have been booked by a tool that is not there"
    )
    assert "Rebuild the backend image" in result["error"], (
        f"and the panel has to say what to do about it: {result['error']!r}"
    )


def test_the_header_chip_names_what_was_booked():
    confirmed = ScheduleService._state_detail(
        {"planned": 2, "booked": 2, "booked_state": "confirmed"}
    )
    partial = ScheduleService._state_detail(
        {"planned": 2, "booked": 1, "booked_state": "partial"}
    )

    assert confirmed == "booked 2", f"a confirmed run says what it booked; got {confirmed!r}"
    assert "partial" in partial and "1 booked" in partial, (
        f"a partial run has to say so, it is the one to act on; got {partial!r}"
    )
    for text in (confirmed, partial):
        assert "simulat" not in text.lower() and "dry" not in text.lower(), (
            f"there is no simulated or dry run any more: {text!r}"
        )


# --- what the removed modes left behind ------------------------------------------
@pytest.mark.parametrize(
    "stored",
    [
        {"status": "ok_with_warnings", "booked_state": "mock", "booked": 0,
         "cli_version": "mock", "observations": []},
        {"status": "ok", "booked_state": "dry_run", "dry_run": True, "booked": 0,
         "observations": []},
        {"status": "error", "booked_state": "dry_run", "dry_run": True,
         "observations": []},
    ],
    ids=["simulated", "dry-run", "failed-dry-run"],
)
def test_a_stored_booking_that_never_happened_is_removed_at_startup(tmp_path, stored):
    """The fake 'SIMULATED · 1 PLANNED' row must leave the board for good."""
    result_file = tmp_path / "schedule_last_run.json"
    result_file.write_text(json.dumps(stored), encoding="utf-8")

    svc = make_service(tmp_path)

    assert not result_file.exists(), (
        "a stored result from a run that could not book has to be deleted at "
        "startup - on the board it reads as a booking that never happened"
    )
    assert svc.get_last_run() == {"status": "never_run"}


def test_a_real_stored_result_survives_startup(tmp_path):
    result_file = tmp_path / "schedule_last_run.json"
    real = {"status": "ok", "booked_state": "confirmed", "booked": 2, "planned": 2,
            "cli_version": "satnogs-auto-scheduler 0.5.dev17+g0f7ec0177",
            "observations": []}
    result_file.write_text(json.dumps(real), encoding="utf-8")

    svc = make_service(tmp_path)

    assert result_file.exists() and svc.get_last_run() == real, (
        "the purge is for results that could not have booked; a real run is "
        "the operator's record of what is on the calendar"
    )


def test_a_not_real_result_is_hidden_even_if_it_could_not_be_deleted(tmp_path):
    svc = make_service(tmp_path)
    # Written after startup, standing in for a purge whose unlink failed.
    (tmp_path / "schedule_last_run.json").write_text(json.dumps(
        {"status": "ok_with_warnings", "booked_state": "mock", "cli_version": "mock",
         "observations": []}
    ), encoding="utf-8")

    assert svc.get_last_run() == {"status": "never_run"}, (
        "get_last_run is the board's only source, so it refuses such a result "
        "whether or not the file is still there"
    )


@pytest.mark.parametrize(
    "stored_dry_run, enabled, stays_enabled",
    [
        ("absent", True, False),
        (True, True, False),
        (False, True, True),
        ("absent", False, False),
        (True, False, False),
        (False, False, False),
    ],
)
def test_upgrading_never_turns_a_dry_run_only_auto_run_into_real_booking(
    tmp_path, stored_dry_run, enabled, stays_enabled
):
    """auto_run_dry_run used to default to True, so absent meant 'preview only'.

    Every fire books now. An auto-run the operator set up to preview must not
    start booking unattended just because the code changed under it - it is
    switched off instead. Only an explicit False, a deliberate BOOK FOR REAL,
    carries on.
    """
    config = {"auto_run_enabled": enabled, "auto_run_mode": "times",
              "auto_run_times": ["06:00", "18:00"]}
    if stored_dry_run != "absent":
        config["auto_run_dry_run"] = stored_dry_run
    config_file = tmp_path / "schedule_config.json"
    config_file.write_text(json.dumps(config), encoding="utf-8")

    svc = make_service(tmp_path)

    assert svc.auto_run_enabled() is stays_enabled, (
        f"stored auto_run_dry_run={stored_dry_run!r} with auto_run_enabled={enabled}: "
        f"expected enabled={stays_enabled}"
    )
    on_disk = json.loads(config_file.read_text(encoding="utf-8"))
    assert "auto_run_dry_run" not in on_disk, "the obsolete key is dropped from disk"
    assert on_disk["schedule_real_only"] is True, "and the one-time migration is marked done"
    assert on_disk["auto_run_enabled"] is stays_enabled
    assert on_disk["auto_run_times"] == ["06:00", "18:00"], (
        "switching auto-run off must not lose the operator's schedule"
    )


async def test_the_migration_runs_once(tmp_path):
    """A config written after the upgrade has no auto_run_dry_run key at all.

    Without the marker, that absence would read as 'dry-run only' at the next
    restart and switch a deliberately enabled auto-run off every time.
    """
    fresh = make_service(tmp_path)
    assert not (tmp_path / "schedule_config.json").exists(), (
        "a fresh install has nothing to migrate and no reason to create a file"
    )
    await fresh.save_config(auto_run_enabled=True, auto_run_times=["06:00"])

    restarted = make_service(tmp_path)

    assert restarted.auto_run_enabled() is True, (
        "an auto-run enabled after the upgrade must stay enabled across a restart"
    )


async def test_auto_run_dry_run_is_no_longer_a_setting(tmp_path):
    svc = make_service(tmp_path)

    with pytest.raises(ValueError):
        await svc.save_config(auto_run_dry_run=True)
    with pytest.raises(ValidationError):
        ScheduleConfigUpdate.model_validate({"auto_run_dry_run": True})
    assert "auto_run_dry_run" not in await svc.get_config()


@pytest.mark.parametrize(
    "body",
    [{}, {"dry_run": True}, {"dry_run": False}, {"book": False},
     {"book": True, "dry_run": True}, {"book": None}],
)
def test_a_run_request_must_say_book_true_and_nothing_else(body):
    """A stale tab's DRY RUN click must be a 422, never a real booking."""
    with pytest.raises(ValidationError):
        ScheduleRunRequest.model_validate(body)


def test_book_true_is_the_one_accepted_run_request():
    assert ScheduleRunRequest.model_validate({"book": True}).book is True


async def test_an_interrupted_run_is_recorded_not_lost(tmp_path, monkeypatch):
    """A backend restart mid-run - any source save, under uvicorn --reload.

    The run may already have booked. Losing its record leaves the previous
    result on the board, and the natural next move - running again - can
    double-book.
    """
    svc = make_service(tmp_path)
    started = asyncio.Event()

    async def hang(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(svc, "_execute_run", hang)
    task = asyncio.create_task(svc.run_plan(trigger="auto"))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    stored = svc.get_last_run()
    assert stored["status"] == "error" and stored["booked_state"] == "unconfirmed", (
        f"an interrupted run is neither failed nor confirmed; got {stored!r}"
    )
    assert "network.satnogs.org" in stored["error"], (
        "and it has to send the operator to check before running again"
    )
    assert stored["trigger"] == "auto"
    assert svc.is_running() is False, "and the next run must not be refused"


async def test_a_busy_slot_backs_off_instead_of_spinning(monkeypatch):
    """A slot that comes due during a manual run is retried - after a pause.

    run_plan refuses while a run is in flight and the mark is restored, which
    is right. Going straight round again was not: the loop re-marked, was
    refused and restored as fast as it could, writing the config twice per
    pass for the whole of a cold-cache run.
    """
    events = []

    class StubService:
        def next_auto_run(self):
            return datetime.now(timezone.utc) - timedelta(seconds=1)

        async def mark_auto_run(self):
            events.append("mark")
            return "previous"

        async def run_plan(self, *, trigger):
            events.append(("run", trigger))
            return {"status": "running"}

        async def restore_auto_run_mark(self, previous):
            events.append(("restore", previous))

    class Stop(Exception):
        pass

    async def fake_sleep(delay):
        events.append(("sleep", delay))
        raise Stop

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fake_sleep)
    loop_owner = scheduler_module.Scheduler.__new__(scheduler_module.Scheduler)
    loop_owner.schedule_service = StubService()

    with pytest.raises(Stop):
        await loop_owner._schedule_loop()

    assert events == [
        "mark", ("run", "auto"), ("restore", "previous"),
        ("sleep", scheduler_module.Scheduler._AUTO_RUN_BUSY_RETRY_S),
    ], f"the refused slot has to be restored and then waited on; got {events}"
    assert scheduler_module.Scheduler._AUTO_RUN_BUSY_RETRY_S >= 10


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
        parsed_with([planned_row(MISSION, start)]), clean_outcome()
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
        parsed_with([planned_row(MISSION, start)]), clean_outcome()
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
        parsed_with([planned_row(MISSION, start)]), clean_outcome()
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
        parsed_with([planned_row(MISSION, start)]), clean_outcome()
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
        parsed_with([planned_row(MISSION, start)]), clean_outcome()
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


async def test_a_run_that_crashed_before_its_table_is_failed_not_confirmed(
    tmp_path, monkeypatch
):
    """It used to read 'confirmed, 0 booked' - a clean result for a crash."""
    svc = make_service(tmp_path)
    calendar = StubCalendar([])
    monkeypatch.setattr(svc, "_future_bookings_sync", calendar)

    result = await svc._reconcile(parsed_with([]), failed_outcome("crashed"))

    assert result == (0, "failed", []), (
        f"the tool died before it printed a table or reached its booking step; "
        f"that is a failed run: {result}"
    )
    assert calendar.calls == 0, "and there is nothing on the calendar to look for"


async def test_a_clean_run_with_nothing_to_book_is_confirmed(tmp_path, monkeypatch):
    svc = make_service(tmp_path)
    monkeypatch.setattr(svc, "_future_bookings_sync", StubCalendar([]))

    result = await svc._reconcile(parsed_with([]), clean_outcome())

    assert result == (0, "confirmed", []), (
        "no passes to book is a legitimate, finished answer - not a failure"
    )


async def test_a_run_that_reached_booking_but_lost_its_table_is_unconfirmed(
    tmp_path, monkeypatch
):
    """The tool got as far as posting; what it booked is simply unknown."""
    svc = make_service(tmp_path)
    monkeypatch.setattr(svc, "_future_bookings_sync", StubCalendar([]))
    parsed = ParsedRun(planned=[], attempted_booking=True, booked_log=3)

    booked, state, notices = await svc._reconcile(parsed, failed_outcome("killed_idle"))

    assert (booked, state) == (3, "unconfirmed"), (
        "'failed, 0 booked' here invites a second run on top of three bookings "
        f"that may well exist; got {booked} / {state!r}"
    )
    assert notices and "network.satnogs.org" in notices[0]["message"]

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
    # The save is dated too, not just the query. auto_run_changed_utc floors
    # next_auto_run, so a save stamped with the real clock puts the floor above
    # a fixture-dated answer and the assertion below fails for reasons that
    # have nothing to do with what it is testing.
    await svc.save_config(
        auto_run_enabled=True, auto_run_times=["06:00"],
        now=datetime(2026, 9, 21, 11, 0, tzinfo=timezone.utc),
    )

    now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    # save_config stamps auto_run_changed_utc from the real clock; this test
    # injects a `now` in the past, so the stamp has to be moved into the
    # injected world or it becomes a floor no slot can clear once the real
    # date passes the one written here.
    svc._config["auto_run_changed_utc"] = (now - timedelta(days=1)).isoformat()
    nxt = svc.next_auto_run(now=now)

    assert nxt == datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc), (
        "with 06:00 configured and the clock at noon, the next run is tomorrow "
        f"morning; {nxt} means the panel's countdown and the loop's own sleep "
        "disagree about when the station will next book anything"
    )


async def test_a_fired_slot_is_remembered_across_a_restart(tmp_path):
    """Otherwise a container restart re-runs the slot that just ran."""
    svc = make_service(tmp_path, timezone="UTC")
    await svc.save_config(
        auto_run_enabled=True, auto_run_times=["06:00"],
        now=datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
    )
    fired = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)
    # Same as the forgetful case below: move save_config's real-clock stamp
    # into the injected world. mark_auto_run() then writes it to disk.
    svc._config["auto_run_changed_utc"] = (fired - timedelta(days=1)).isoformat()
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
    svc = make_service(tmp_path, offline=True)
    # Both tokens set, so the offline guard - not the token check - has to be
    # what stops it.
    await svc.save_config(db_token=DB_TOKEN, network_token=NETWORK_TOKEN)

    result = await svc.run_plan()

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
    progress, and the mark was being kept anyway - so a cold-cache manual run
    started at 05:58 would consume the 06:00 booking slot, nothing would be
    booked by it, and nothing anywhere would record that the slot had been
    skipped.
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
        return {"status": "ok", "planned": 0, "booked": 0,
                "booked_state": "confirmed", "observations": [], "notices": [],
                "log_tail": []}

    monkeypatch.setattr(svc, "_execute_run", slow)

    first = asyncio.create_task(svc.run_plan())
    await asyncio.sleep(0)          # let it take the guard
    while not svc.is_running():
        await asyncio.sleep(0)

    second = await svc.run_plan()
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
    with the run button enabled, while a real booking run was in flight.
    """
    svc = make_service(tmp_path)
    # No tokens, so this fails at validate_tokens without spawning anything -
    # and still writes a result, which is what is under test.
    await svc.run_plan()

    stored = svc.get_last_run()
    assert stored["status"] != "running", (
        "nothing writes 'running' to schedule_last_run.json, so anything that "
        "looks for it there will never find it"
    )
    assert svc.is_running() is False


# --- one dead satellite must not crash the run -----------------------------------
PREDICTION_CRASH = RunOutcome(
    exit_code=1,
    failure=("crashed", "satnogs-auto-scheduler crashed. The raw log below has the traceback."),
    lines=[
        "Traceback (most recent call last):",
        '  File ".../auto_scheduler/pass_predictor.py", line 98, in find_constrained_passes',
        '  File ".../satnogs_predict/propagation/propagator.py", line 227',
        "AssertionError: Set event without active pass",
    ],
)
DEAD = {"norad_cat_id": "51840", "name": "OBJECT S", "epoch": "2026-05-01"}


def _real_run_harness(svc, monkeypatch, outcomes, screens):
    """Drive _execute_run with a scripted tool and a scripted TLE screen."""
    calls = {"run": 0, "screen": 0}

    async def fake_run(cfg, on_line=None, log_path=None):
        calls["run"] += 1
        return outcomes[min(calls["run"], len(outcomes)) - 1]

    def fake_screen(cfg):
        calls["screen"] += 1
        return list(screens[min(calls["screen"], len(screens)) - 1])

    from app.vendor.autoscheduler.priorities import Priority

    monkeypatch.setattr(autoscheduler_cli, "run", fake_run)
    monkeypatch.setattr(svc, "_screen_tool_tles_sync", fake_screen)
    monkeypatch.setattr(
        svc, "_resolve_priorities_sync",
        lambda: ([Priority(norad_cat_id=MISSION, weight=1.0, transmitter_uuid="tx")], []),
    )
    monkeypatch.setattr(
        svc, "_enrich_rows_sync", lambda rows: [svc._row_payload(r, {}) for r in rows]
    )
    return calls


async def test_a_refresh_that_brings_a_dead_tle_back_is_retried_once(
    tmp_path, monkeypatch, no_replication_pause
):
    """The tool refreshes its TLE cache inside the run, after the pre-run screen.

    That is how the first real run crashed: a fresh download brought NORAD
    51840 back, and pass prediction died before anything was booked. Screening
    the fresh cache and going again is safe precisely because nothing was posted.
    """
    svc = make_service(tmp_path)
    await svc.save_config(db_token=DB_TOKEN, network_token=NETWORK_TOKEN)
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    good = RunOutcome(exit_code=0, parsed=parsed_with([planned_row(MISSION, start)]))
    monkeypatch.setattr(svc, "_future_bookings_sync", StubCalendar([StubBooking(MISSION, start)]))
    calls = _real_run_harness(svc, monkeypatch, [PREDICTION_CRASH, good], [[], [DEAD]])

    result = await svc.run_plan(trigger="auto")

    assert calls == {"run": 2, "screen": 2}, (
        f"one crash in prediction, one screen that removed something, one retry: {calls}"
    )
    assert (result["status"], result["booked_state"], result["booked"]) == (
        "ok_with_warnings", "confirmed", 1
    ), f"the retry booked the pass: {result['status']} / {result['booked_state']}"
    assert any("NORAD 51840 OBJECT S" in n["message"] for n in result["notices"]), (
        "and the board has to say which satellite was left out"
    )


async def test_a_prediction_crash_the_screen_cannot_explain_is_not_retried(
    tmp_path, monkeypatch
):
    svc = make_service(tmp_path)
    await svc.save_config(db_token=DB_TOKEN, network_token=NETWORK_TOKEN)
    calls = _real_run_harness(svc, monkeypatch, [PREDICTION_CRASH], [[], []])

    result = await svc.run_plan()

    assert calls["run"] == 1, (
        "going again with the same input would crash the same way - and a retry "
        "loop against SatNOGS is the last thing an unattended timer should run"
    )
    assert result["status"] == "error" and result["booked_state"] == "failed"


async def test_a_crash_after_booking_started_is_never_retried(tmp_path, monkeypatch):
    svc = make_service(tmp_path)
    await svc.save_config(db_token=DB_TOKEN, network_token=NETWORK_TOKEN)
    after_posting = RunOutcome(
        exit_code=1, failure=PREDICTION_CRASH.failure, lines=PREDICTION_CRASH.lines,
        parsed=ParsedRun(attempted_booking=True),
    )
    monkeypatch.setattr(svc, "_future_bookings_sync", StubCalendar([]))
    calls = _real_run_harness(svc, monkeypatch, [after_posting], [[], [DEAD]])

    await svc.run_plan()

    assert calls["run"] == 1, (
        "once the tool has started posting, a second run could double-book"
    )


async def test_a_crashed_run_does_not_blame_the_priority_transmitters(tmp_path, monkeypatch):
    """Every priority entry used to get 'its pinned transmitter is not in this
    station's candidate set' after a crash - eight wrong causes for one failure."""
    svc = make_service(tmp_path)
    await svc.save_config(db_token=DB_TOKEN, network_token=NETWORK_TOKEN)
    _real_run_harness(svc, monkeypatch, [PREDICTION_CRASH], [[], []])

    result = await svc.run_plan()

    assert not any("no pass was selected" in n["message"] for n in result["notices"]), (
        f"a run that crashed selected nothing, for reasons unrelated to any "
        f"transmitter: {[n['message'][:60] for n in result['notices']]}"
    )


def test_the_pre_run_screen_covers_the_window_the_tool_will_plan(tmp_path, monkeypatch):
    svc = make_service(tmp_path)
    seen = {}

    def fake_screen(cache_dir, start, end):
        seen.update(cache_dir=cache_dir, start=start, end=end)
        return []

    monkeypatch.setattr(autoscheduler_cli, "screen_tool_tles", fake_screen)
    now = datetime(2026, 9, 23, 11, 0, tzinfo=timezone.utc)
    cfg = autoscheduler_cli.RunConfig(station_id=5024, hours=24, start_lead_minutes=10, now=now)

    svc._screen_tool_tles_sync(cfg)

    assert seen["cache_dir"] == svc.cache_dir, "it must screen the cache the tool reads"
    assert seen["start"] == now + timedelta(minutes=10)
    assert seen["end"] == seen["start"] + timedelta(hours=24)


def test_a_screen_that_fails_never_stops_a_run(tmp_path, monkeypatch):
    svc = make_service(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("numpy went away")

    monkeypatch.setattr(autoscheduler_cli, "screen_tool_tles", broken)
    cfg = autoscheduler_cli.RunConfig(station_id=5024, now=datetime.now(timezone.utc))

    assert svc._screen_tool_tles_sync(cfg) == []
