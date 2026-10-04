"""Retrying an auto-run slot's work after a transient SatNOGS failure.

2026-10-04, the first live slot on this build: at 11:00Z the station run
stopped on "Download from SatNOGS Network failed." (network_download) and the
chained campaign's preview on "GET .../api/stations/ failed after 3 attempts:
... -> HTTP 500". Nothing retried, so the whole slot was lost and the next
chance was 23:00Z. SatNOGS was flaky, not down: 12 of our next 126 requests
got a 500, the rest a 200. What matters here:

* the one invariant: only work that sent NO booking POST is ever repeated -
  a campaign preview that failed transiently, and a station run the official
  tool refused before booking (network_download, booked 0). A commit result
  of any status is final;
* "transient" is read off the exception, not its text: unreachable or 5xx
  is, 4xx / RateLimitedError / anything else is not - checked against the
  incident's own error, produced by the real http/Cache/NetworkClient path;
* retries are background tasks, at most one of each kind, that stop when the
  toggles go off, the next slot is close, or a later slot did the work; they
  never touch the slot mark and Scheduler.stop() cancels them;
* the campaign's daily timer retries a transient preview failure every 10
  minutes (6 at most) instead of sleeping a day, and its busy and error
  retries cannot reset each other into a loop.

Nothing here touches the network: requests stop at a faked transport
adapter, and every sleep is faked.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import MaxRetryError, NewConnectionError

from app import scheduler as scheduler_module
from app.config import Settings
from app.services import autoscheduler_cli
from app.services import campaign_service as cs
from app.services import schedule_service as ss
from app.services.autoscheduler_cli import ParsedRun, RunOutcome, ScheduleRow
from app.services.campaign_service import CampaignService
from app.services.schedule_service import ScheduleService
from app.vendor.autoscheduler import http as http_mod
from app.vendor.autoscheduler import network_client as nc
from app.vendor.autoscheduler.cache import Cache
from app.vendor.autoscheduler.http import SatnogsHTTPError, SatnogsOutcomeUnknown
from app.vendor.autoscheduler.network_client import RateLimitedError, ScheduleResult

MISSION = 67683
DB_TOKEN = "a" * 40
NETWORK_TOKEN = "b1c2d3e4f5" * 4
STATIONS_URL = "https://network.satnogs.org/api/stations/"
INCIDENT_ERROR = (f"GET {STATIONS_URL} failed after 3 attempts: "
                  f"GET {STATIONS_URL} -> HTTP 500")
# campaign_service.AUTO_CYCLE_RETRY, the value the timer contract fixes.
RETRY = "retry"
NETWORK_DOWNLOAD_TRANSCRIPT = [
    "INFO\troot\tDownload list of scheduled passes from SatNOGS Network...",
    "ERROR\troot\tDownload from SatNOGS Network failed.",
]

_real_sleep = asyncio.sleep


class _Stop(Exception):
    pass


@pytest.fixture(autouse=True)
def offline_transport(monkeypatch):
    """No backoff sleeps, a fresh read budget, and no request can leave."""
    monkeypatch.setattr(http_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(nc, "_GATES", {})

    def refuse(self, request, **kwargs):
        raise AssertionError(f"a test tried to send {request.method} {request.url}")
    monkeypatch.setattr(HTTPAdapter, "send", refuse)


def response(status: int, request=None, body: bytes = b"[]", headers=None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = body
    r.encoding = "utf-8"
    r.headers.update(headers or {})
    if request is not None:
        r.request, r.url = request, request.url
    return r


def refused_connection():
    return requests.exceptions.ConnectionError(MaxRetryError(
        None, STATIONS_URL, reason=NewConnectionError(None, "Connection refused")))


class Clock:
    """Stands in for asyncio.sleep everywhere (scheduler_module.asyncio is the
    asyncio module). Records every delay; with `hold` set, a retry-length
    sleep (>= 600 s) waits until the test opens the gate, so a test can look
    at what happened before any retry ran."""

    def __init__(self, hold: bool = False):
        self.sleeps: list[float] = []
        self.gate = asyncio.Event()
        if not hold:
            self.gate.set()

    async def sleep(self, seconds, *args, **kwargs):
        self.sleeps.append(seconds)
        if seconds >= 600:
            await self.gate.wait()
        await _real_sleep(0)

    def long(self) -> list[float]:
        return [s for s in self.sleeps if s >= 600]


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(scheduler_module.asyncio, "sleep", c.sleep)
    return c


@pytest.fixture
def held_clock(monkeypatch):
    c = Clock(hold=True)
    monkeypatch.setattr(scheduler_module.asyncio, "sleep", c.sleep)
    return c


def planned_row(start: datetime) -> ScheduleRow:
    return ScheduleRow(
        norad=MISSION, start=start, end=start + timedelta(minutes=8), duration_s=480,
        az_rise=10.0, elevation=45.0, az_set=190.0, priority=1.0,
        transmitter_uuid="test-transmitter", mode="GFSK", frequency_violator=False,
        name="KNACKSAT-2", already_scheduled=False,
    )


def network_download_outcome() -> RunOutcome:
    """What autoscheduler_cli.run hands back for the incident's transcript."""
    lines = list(NETWORK_DOWNLOAD_TRANSCRIPT)
    return RunOutcome(exit_code=1, lines=lines, parsed=autoscheduler_cli.parse_output(lines),
                      failure=autoscheduler_cli.classify_failure(lines, 1))


def incident_exception() -> SatnogsHTTPError:
    """The error the 11:00Z preview died on, raised by the real http.request."""
    class Flaky:
        def request(self, method, url, **kwargs):
            return response(500)
    with pytest.raises(SatnogsHTTPError) as info:
        http_mod.request(Flaky(), "GET", STATIONS_URL)
    return info.value


def campaign_item(station_id: int, hours: int) -> dict:
    start = datetime.now(timezone.utc) + timedelta(hours=hours)
    return {"station_id": station_id, "station_name": f"S{station_id}",
            "transmitter_uuid": "tx", "start": start.isoformat(),
            "end": (start + timedelta(minutes=8)).isoformat(), "max_elevation_deg": 40.0}


class RecordingNetwork:
    """NetworkClient for a commit: records every booking POST."""

    def __init__(self):
        self.posts: list[int] = []
        self.calendar_sources: dict = {}

    def schedule(self, items, execute=False):
        assert execute
        self.posts.append(len(items))
        return ScheduleResult(submitted=len(items), accepted=len(items), accepted_items=list(items))


def real_services(tmp_path, monkeypatch):
    """A real ScheduleService and CampaignService on a throwaway data dir,
    neither able to reach SatNOGS: the station tool is faked by each test,
    and the campaign's commit goes to a RecordingNetwork."""
    settings = Settings(data_dir=tmp_path, mock=False, offline=False, campaign_mock=False,
                        default_norad=MISSION)
    schedule = ScheduleService(settings)
    campaign = CampaignService(settings, schedule)
    network = RecordingNetwork()
    monkeypatch.setattr(cs, "Cache", lambda *a, **k: None)
    monkeypatch.setattr(cs, "NetworkClient", lambda *a, **k: network)
    monkeypatch.setattr(cs, "DbClient", lambda *a, **k: None)
    return schedule, campaign, network


def scripted_previews(campaign, monkeypatch, script):
    """Each preview raises or returns the next entry of `script`."""
    script = iter(script)

    def preview_sync():
        nxt = next(script)
        if isinstance(nxt, BaseException):
            raise nxt
        return {"status": "ok", "generated_utc": datetime.now(timezone.utc).isoformat(),
                "items": nxt}
    monkeypatch.setattr(campaign, "_preview_sync", preview_sync)


def scripted_runs(schedule, monkeypatch, outcomes, bookings=()):
    runs: list = []

    async def fake_run(cfg, on_line=None, log_path=None):
        runs.append(cfg)
        return outcomes[len(runs) - 1]
    monkeypatch.setattr(autoscheduler_cli, "run", fake_run)
    monkeypatch.setattr(schedule, "_resolve_priorities_sync", lambda: ([], []))
    monkeypatch.setattr(schedule, "_screen_tool_tles_sync", lambda cfg: [])
    monkeypatch.setattr(schedule, "_enrich_rows_sync",
                        lambda rows: [schedule._row_payload(r, {}) for r in rows])
    monkeypatch.setattr(schedule, "_future_bookings_sync", lambda: list(bookings))
    return runs


def owner_for(schedule, campaign):
    owner = scheduler_module.Scheduler.__new__(scheduler_module.Scheduler)
    owner.schedule_service = schedule
    owner.campaign_service = campaign
    return owner


def background() -> list[asyncio.Task]:
    """Every task but the test itself: the retries the code under test started."""
    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]


async def drain():
    await asyncio.wait_for(asyncio.gather(*background()), 10)


def one_real_slot(schedule, monkeypatch):
    """Make _schedule_loop fire exactly one slot, then stop where it would
    wait for the next one (12 hours away, as on 2026-10-04)."""
    asked = {"n": 0}

    def next_auto_run(now=None):
        asked["n"] += 1
        now = datetime.now(timezone.utc)
        return now - timedelta(seconds=1) if asked["n"] == 1 else now + timedelta(hours=12)

    async def next_slot_wait(timeout):
        raise _Stop
    monkeypatch.setattr(schedule, "next_auto_run", next_auto_run)
    monkeypatch.setattr(schedule, "wait_for_config_change", next_slot_wait)


# --- the incident, end to end -------------------------------------------------------------

async def test_the_2026_10_04_slot_is_retried_and_books_once(tmp_path, monkeypatch,
                                                            held_clock, caplog):
    """The 11:00Z slot exactly: the station run stops on network_download and
    the chained preview on the real HTTP 500. Both are retried 600 s later in
    the background - the slot loop is already waiting for the next slot by
    then - and both retries go through: one more station run, and exactly ONE
    chained commit."""
    caplog.set_level(logging.INFO, logger="app.scheduler")
    schedule, campaign, network = real_services(tmp_path, monkeypatch)
    await schedule.save_config(db_token=DB_TOKEN, network_token=NETWORK_TOKEN,
                               auto_run_enabled=True, auto_run_chain_campaign=True,
                               campaign_loop_until_exhausted=False)
    start = datetime.now(timezone.utc) + timedelta(hours=2)
    booked_later = SimpleNamespace(norad_cat_id=MISSION, start=start)
    runs = scripted_runs(schedule, monkeypatch, [
        network_download_outcome(),
        RunOutcome(exit_code=0, parsed=ParsedRun(planned=[planned_row(start)])),
    ], bookings=[booked_later])
    scripted_previews(campaign, monkeypatch,
                      [incident_exception(), [campaign_item(40, 3), campaign_item(41, 4)]])
    one_real_slot(schedule, monkeypatch)
    owner = owner_for(schedule, campaign)

    with pytest.raises(_Stop):
        await owner._schedule_loop()

    assert schedule.get_last_run()["error"].startswith("Could not read the station's existing")
    assert campaign.get_last_preview()["error"] == INCIDENT_ERROR
    assert len(runs) == 1 and network.posts == [], (
        "the slot loop must not wait on retries: it is back at the next slot before either runs")
    mark = schedule._config["auto_run_last_fire_utc"]

    held_clock.gate.set()
    await drain()

    assert len(runs) == 2, "one more station run"
    assert held_clock.long() == [600.0, 600.0], "each half waited 600 s, once"
    assert (schedule.get_last_run()["booked"], schedule.get_last_run()["trigger"]) == (1, "auto")
    assert network.posts == [2], "exactly one booking POST, from the retry"
    assert [(h["trigger"], h["status"]) for h in campaign.get_history()] == [("chained", "ok")]
    assert schedule._config["auto_run_last_fire_utc"] == mark, "the slot mark is never touched"
    log = caplog.text
    assert "auto-run: chained campaign retry 1/5" in log and INCIDENT_ERROR in log
    assert "auto-run: station run retry 1/3" in log


# --- R0: what counts as transient ---------------------------------------------------------

def test_a_read_that_got_500_every_time_keeps_its_status():
    """The incident's error used to arrive with status None, which means
    "never connected": what SatNOGS actually answered was lost before
    anything could classify it."""
    exc = incident_exception()

    assert str(exc) == INCIDENT_ERROR
    assert exc.status == 500
    assert isinstance(exc.__cause__, SatnogsHTTPError) and exc.__cause__.status == 500


def caused_by(exc: BaseException, cause: BaseException) -> BaseException:
    """`exc` as if raised `from cause`."""
    exc.__cause__ = cause
    return exc


@pytest.mark.parametrize("exc, transient", [
    (SatnogsHTTPError("x -> HTTP 500", status=500), True),
    (SatnogsHTTPError("x -> HTTP 503", status=503), True),
    # http.request's final error when no attempt got an answer: chained from
    # the last transport error.
    (caused_by(SatnogsHTTPError("never connected", status=None), refused_connection()), True),
    (caused_by(SatnogsHTTPError("timed out", status=None),
               requests.exceptions.ReadTimeout("read timed out")), True),
    # paginate's: SatNOGS answered 200, just not with a list. Also status
    # None, but nothing failed to connect, and the same read gets the same
    # answer in ten minutes.
    (SatnogsHTTPError(f"expected a list from {STATIONS_URL}, got dict"), False),
    (requests.exceptions.Timeout("read timed out"), True),
    (requests.exceptions.ConnectTimeout("connect timed out"), True),
    (requests.exceptions.ConnectionError("reset"), True),
    (SatnogsHTTPError("x -> HTTP 400", status=400), False),
    (SatnogsHTTPError("x -> HTTP 404", status=404), False),
    (RateLimitedError("x -> HTTP 429", status=429), False),
    (RateLimitedError("budget spent", status=None), False),
    (SatnogsOutcomeUnknown("x -> HTTP 504", status=504), False),
    (ValueError("bad json"), False),
    (KeyError("lat"), False),
    (RuntimeError("--offline was given but there is no cached 'db-tle'"), False),
])
def test_only_unreachable_and_5xx_are_transient(exc, transient):
    assert cs.is_transient_error(exc) is transient


def seed_tle_cache(schedule) -> None:
    # build_campaign reads the mission TLE before the station catalogue; a
    # cached one keeps that read off the (refusing) transport.
    Cache(schedule.cache_dir).write("db-tle", [{
        "norad_cat_id": MISSION, "tle0": "KNACKSAT-2",
        "tle1": "1 67683U", "tle2": "2 67683"}])


@pytest.mark.parametrize("answer, retryable", [
    (lambda req: response(500, req, b"<h1>Server Error (500)</h1>"), True),
    (lambda req: response(502, req, b"Bad Gateway"), True),
    ("refused", True),
    (lambda req: response(400, req, b'{"detail": "bad"}'), False),
    (lambda req: response(429, req, b"{}", {"Retry-After": "3600"}), False),
    (lambda req: response(200, req, b'{"detail": "Invalid page."}'), False),
])
async def test_the_preview_s_verdict_comes_from_the_real_read_path(tmp_path, monkeypatch,
                                                                  answer, retryable):
    """CampaignService.preview_campaign -> _preview_sync -> build_campaign ->
    NetworkClient.all_stations -> Cache -> http.paginate/request ->
    RateLimitedSession -> requests, with only the transport adapter faked."""
    settings = Settings(data_dir=tmp_path, mock=False, offline=False, campaign_mock=False,
                        default_norad=MISSION)
    schedule = ScheduleService(settings)
    campaign = CampaignService(settings, schedule)
    seed_tle_cache(schedule)
    sent: list[str] = []

    def send(self, request, **kwargs):
        sent.append(request.url)
        assert request.url.startswith(STATIONS_URL), request.url
        if answer == "refused":
            raise refused_connection()
        return answer(request)
    monkeypatch.setattr(HTTPAdapter, "send", send)

    preview = await campaign.preview_campaign()

    assert preview["status"] == "error"
    assert preview["retryable"] is retryable, preview["error"]
    assert sent, "the read really went through the transport"
    if answer != "refused" and retryable:
        assert preview["error"].startswith(f"GET {STATIONS_URL} failed after 3 attempts")


@pytest.mark.parametrize("exc", [
    SatnogsHTTPError("GET x -> HTTP 404", status=404),
    RateLimitedError("GET x -> HTTP 429 with Retry-After 3600s", status=429),
    ValueError("unreadable payload"),
])
async def test_a_preview_error_that_is_not_transient_is_not_retried(tmp_path, monkeypatch,
                                                                   clock, exc):
    schedule, campaign, network = real_services(tmp_path, monkeypatch)
    await schedule.save_config(auto_run_enabled=True, auto_run_chain_campaign=True)
    scripted_previews(campaign, monkeypatch, [exc])
    owner = owner_for(schedule, campaign)

    await owner._after_auto_run({"status": "ok"})
    await drain()

    assert campaign.get_last_preview()["retryable"] is False
    assert background() == [] and clock.long() == [] and network.posts == []


async def test_a_network_download_run_carries_its_code_and_is_retryable(tmp_path, monkeypatch):
    schedule, _campaign, _network = real_services(tmp_path, monkeypatch)
    await schedule.save_config(db_token=DB_TOKEN, network_token=NETWORK_TOKEN)
    scripted_runs(schedule, monkeypatch, [network_download_outcome()])

    result = await schedule.run_plan(trigger="auto")

    assert (result["status"], result["booked"], result["booked_state"], result["failure_code"]) == (
        "error", 0, "failed", "network_download")
    assert ss.run_is_retryable(result) is True
    assert schedule.get_last_run()["failure_code"] == "network_download"


@pytest.mark.parametrize("line", [
    "CRITICAL\troot\tNo value for SATNOGS_NETWORK_API_TOKEN",
    "CRITICAL\troot\tInvalid value for SATNOGS_DB_API_TOKEN: bad format",
    "ERROR\troot\tStation is neither in 'online' nor in 'testing' mode",
    "ERROR\troot\tNo permission to schedule observations on this station",
    "ERROR\troot\tFailed to batch-schedule observations.",
    "ERROR\troot\tFailed to schedule pass at 2026-10-04T12:00:00",
    "Traceback (most recent call last):",
])
async def test_no_other_station_failure_is_retryable(tmp_path, monkeypatch, line):
    schedule, _campaign, _network = real_services(tmp_path, monkeypatch)
    await schedule.save_config(db_token=DB_TOKEN, network_token=NETWORK_TOKEN)
    lines = ["INFO\troot\tStarting up.", line]
    scripted_runs(schedule, monkeypatch, [RunOutcome(
        exit_code=1, lines=lines, parsed=autoscheduler_cli.parse_output(lines),
        failure=autoscheduler_cli.classify_failure(lines, 1))])

    result = await schedule.run_plan(trigger="auto")

    assert result["failure_code"] not in (None, "network_download")
    assert ss.run_is_retryable(result) is False


@pytest.mark.parametrize("change", [
    {"booked": 1}, {"booked_state": "unconfirmed"}, {"status": "ok_with_warnings"},
    {"failure_code": None}, {"failure_code": "killed_idle"},
])
def test_network_download_is_retryable_only_with_nothing_booked(change):
    result = {"status": "error", "booked": 0, "booked_state": "failed",
              "failure_code": "network_download"}
    assert ss.run_is_retryable(result) is True
    assert ss.run_is_retryable({**result, **change}) is False


# --- R1: the chained campaign -------------------------------------------------------------

class Schedule:
    """The Scheduler-facing half of ScheduleService, for the retry tasks."""

    def __init__(self, *, chain=True, enabled=True, next_in_s=12 * 3600, results=()):
        self.chain = chain
        self.enabled = enabled
        self.next_in_s = next_in_s
        self.results = list(results)
        self.runs: list[str] = []

    def auto_run_chain_campaign(self):
        return self.chain

    def auto_run_enabled(self):
        return self.enabled

    def next_auto_run(self):
        return datetime.now(timezone.utc) + timedelta(seconds=self.next_in_s)

    async def run_plan(self, *, trigger):
        self.runs.append(trigger)
        return self.results.pop(0)


class Campaign:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def run_chained_cycle(self):
        self.calls += 1
        return self.outcomes.pop(0)


TRANSIENT_SKIP = {"status": "skipped", "retryable": True,
                  "reason": f"the campaign preview failed: {INCIDENT_ERROR}"}
COMMITTED = {"status": "ok", "trigger": "chained", "accepted": 12}
NETWORK_DOWNLOAD = {"status": "error", "booked": 0, "booked_state": "failed",
                    "failure_code": "network_download", "error": "Could not read ..."}
BOOKED = {"status": "ok", "booked": 3, "booked_state": "confirmed", "failure_code": None}


async def test_a_transient_preview_failure_is_retried_600_s_later(clock, caplog):
    caplog.set_level(logging.INFO, logger="app.scheduler")
    campaign = Campaign([TRANSIENT_SKIP, COMMITTED])
    owner = owner_for(Schedule(), campaign)

    await owner._after_auto_run({"status": "ok"})
    await drain()

    assert campaign.calls == 2 and clock.long() == [600.0]
    assert "auto-run: chained campaign retry 1/5" in caplog.text


async def test_the_slot_returns_before_the_retry_runs(held_clock):
    campaign = Campaign([TRANSIENT_SKIP, COMMITTED])
    owner = owner_for(Schedule(), campaign)

    await asyncio.wait_for(owner._after_auto_run({"status": "ok"}), 1)

    assert campaign.calls == 1 and len(background()) == 1
    held_clock.gate.set()
    await drain()
    assert campaign.calls == 2


COMMIT_STATUSES = ["ok", "ok_with_warnings", "error", "blocked", "stale", "running"]


@pytest.mark.parametrize("status", COMMIT_STATUSES)
async def test_a_commit_of_any_status_is_never_retried(clock, status):
    """A commit may have sent bookings. Even one carrying a stray "retryable"
    is final: only a skip is ever looked at."""
    campaign = Campaign([{"status": status, "retryable": True, "accepted": 0}])
    owner = owner_for(Schedule(), campaign)

    await owner._after_auto_run({"status": "ok"})
    await drain()

    assert campaign.calls == 1 and clock.long() == []


@pytest.mark.parametrize("status", COMMIT_STATUSES)
async def test_a_retry_that_reaches_a_commit_of_any_status_is_the_last(clock, status):
    campaign = Campaign([TRANSIENT_SKIP, {"status": status, "retryable": True, "accepted": 0}])
    owner = owner_for(Schedule(), campaign)

    await owner._after_auto_run({"status": "ok"})
    await drain()

    assert campaign.calls == 2 and clock.long() == [600.0]


@pytest.mark.parametrize("skip", [
    {"status": "skipped", "reason": "the preview found nothing to book", "retryable": False},
    {"status": "skipped", "reason": "the campaign preview failed: HTTP 400", "retryable": False},
])
async def test_a_retry_that_is_skipped_for_good_is_the_last(clock, skip):
    campaign = Campaign([TRANSIENT_SKIP, skip])
    owner = owner_for(Schedule(), campaign)

    await owner._after_auto_run({"status": "ok"})
    await drain()

    assert campaign.calls == 2


async def test_the_chain_gives_up_after_five_retries(clock, caplog):
    campaign = Campaign([TRANSIENT_SKIP] * 6)
    owner = owner_for(Schedule(), campaign)

    await owner._after_auto_run({"status": "ok"})
    await drain()

    assert campaign.calls == 6 and clock.long() == [600.0] * 5
    assert scheduler_module.Scheduler.CHAIN_MAX_RETRIES == 5
    assert "still failing after 5 retries" in caplog.text


@pytest.mark.parametrize("next_in_s, retried", [(800, False), (1000, True)])
async def test_a_chain_retry_near_the_next_slot_is_left_to_that_slot(clock, next_in_s, retried):
    campaign = Campaign([TRANSIENT_SKIP, COMMITTED])
    owner = owner_for(Schedule(next_in_s=next_in_s), campaign)

    await owner._after_auto_run({"status": "ok"})
    await drain()

    assert campaign.calls == (2 if retried else 1)


@pytest.mark.parametrize("switch", ["chain", "enabled"])
async def test_switching_off_between_attempts_stops_the_chain_retry(held_clock, switch):
    schedule = Schedule()
    campaign = Campaign([TRANSIENT_SKIP, COMMITTED])
    owner = owner_for(schedule, campaign)
    await owner._after_auto_run({"status": "ok"})

    setattr(schedule, switch, False)
    held_clock.gate.set()
    await drain()

    assert campaign.calls == 1


async def test_a_campaign_crash_inside_a_retry_is_logged_not_raised(clock, caplog):
    class Exploding(Campaign):
        async def run_chained_cycle(self):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("SatNOGS exploded")
            return TRANSIENT_SKIP

    campaign = Exploding([])
    owner = owner_for(Schedule(), campaign)
    await owner._after_auto_run({"status": "ok"})
    await drain()      # gather would raise if the task had

    assert campaign.calls == 2
    assert "a chained campaign retry failed" in caplog.text


# --- R2: the station run ------------------------------------------------------------------

class LoopSchedule(Schedule):
    """Drives _schedule_loop: `slots` slots come due, then it waits for good."""

    def __init__(self, *, slots: int, **kwargs):
        super().__init__(**kwargs)
        self.slots = slots
        self.marks = 0

    def next_auto_run(self):
        if self.slots:
            self.slots -= 1
            return datetime.now(timezone.utc) - timedelta(seconds=1)
        return super().next_auto_run()

    async def wait_for_config_change(self, timeout):
        raise _Stop

    async def mark_auto_run(self):
        self.marks += 1
        return "previous"

    async def restore_auto_run_mark(self, previous):
        raise AssertionError("nothing here is a busy slot")


async def run_slots(owner):
    with pytest.raises(_Stop):
        await owner._schedule_loop()


async def test_a_network_download_slot_is_rerun_without_the_chain(clock, caplog):
    caplog.set_level(logging.INFO, logger="app.scheduler")
    schedule = LoopSchedule(slots=1, results=[dict(NETWORK_DOWNLOAD), dict(BOOKED)])
    campaign = Campaign([COMMITTED])
    owner = owner_for(schedule, campaign)

    await run_slots(owner)
    await drain()

    assert schedule.runs == ["auto", "auto"] and clock.long() == [600.0]
    assert campaign.calls == 1, "the chain ran for the slot, and only then"
    assert schedule.marks == 1
    assert "auto-run: station run retry 1/3" in caplog.text


@pytest.mark.parametrize("second", [
    {"status": "running"},
    {"status": "error", "booked": 0, "booked_state": "failed", "failure_code": "station_offline"},
    {"status": "error", "booked": 0, "booked_state": "unconfirmed", "failure_code": "crashed"},
    dict(BOOKED),
])
async def test_a_station_retry_stops_on_anything_but_another_network_download(clock, second):
    schedule = LoopSchedule(slots=1, chain=False, results=[dict(NETWORK_DOWNLOAD), second])
    owner = owner_for(schedule, Campaign([]))

    await run_slots(owner)
    await drain()

    assert schedule.runs == ["auto", "auto"]


@pytest.mark.parametrize("result", [
    {"status": "error", "booked": 0, "booked_state": "failed", "failure_code": code}
    for code in ("station_offline", "no_permission", "batch_failed", "pass_failed", "crashed",
                 "token_missing", "token_invalid", None)
])
async def test_other_station_failures_start_no_retry(clock, result):
    schedule = LoopSchedule(slots=1, chain=False, results=[result])
    owner = owner_for(schedule, Campaign([]))

    await run_slots(owner)
    await drain()

    assert schedule.runs == ["auto"] and clock.long() == []


async def test_the_station_gives_up_after_three_retries(clock):
    schedule = LoopSchedule(slots=1, chain=False, results=[dict(NETWORK_DOWNLOAD)] * 4)
    owner = owner_for(schedule, Campaign([]))

    await run_slots(owner)
    await drain()

    assert schedule.runs == ["auto"] * 4 and clock.long() == [600.0] * 3
    assert scheduler_module.Scheduler.STATION_MAX_RETRIES == 3


@pytest.mark.parametrize("next_in_s, retried", [(800, False), (1000, True)])
async def test_a_station_retry_near_the_next_slot_is_left_to_that_slot(clock, next_in_s, retried):
    schedule = LoopSchedule(slots=1, chain=False, next_in_s=next_in_s,
                            results=[dict(NETWORK_DOWNLOAD), dict(BOOKED)])
    owner = owner_for(schedule, Campaign([]))

    await run_slots(owner)
    await drain()

    assert len(schedule.runs) == (2 if retried else 1)


async def test_switching_the_auto_run_off_stops_the_station_retry(held_clock):
    schedule = LoopSchedule(slots=1, chain=False, results=[dict(NETWORK_DOWNLOAD), dict(BOOKED)])
    owner = owner_for(schedule, Campaign([]))
    await run_slots(owner)

    schedule.enabled = False
    held_clock.gate.set()
    await drain()

    assert schedule.runs == ["auto"]


# --- at most one of each, and a later slot's own work wins --------------------------------

async def test_two_failed_slots_back_to_back_leave_one_retry_of_each_kind(held_clock):
    schedule = LoopSchedule(slots=2, results=[dict(NETWORK_DOWNLOAD), dict(NETWORK_DOWNLOAD),
                                              dict(BOOKED)])
    campaign = Campaign([TRANSIENT_SKIP, TRANSIENT_SKIP, COMMITTED])
    owner = owner_for(schedule, campaign)

    await run_slots(owner)

    names = sorted(t.get_name() for t in asyncio.all_tasks()
                   if t.get_name() in ("chain-retry", "station-retry"))
    assert names == ["chain-retry", "station-retry"]
    held_clock.gate.set()
    await drain()
    assert schedule.runs == ["auto"] * 3, "two slots, then ONE retry"
    assert campaign.calls == 3, "two slots, then ONE retry"


async def test_a_pending_retry_stands_down_once_a_later_slot_did_the_work(held_clock):
    schedule = LoopSchedule(slots=2, results=[dict(NETWORK_DOWNLOAD), dict(BOOKED)])
    campaign = Campaign([TRANSIENT_SKIP, COMMITTED])
    owner = owner_for(schedule, campaign)

    await run_slots(owner)
    held_clock.gate.set()
    await drain()

    assert schedule.runs == ["auto", "auto"], "the second slot booked; nothing re-ran it"
    assert campaign.calls == 2, "the second slot committed; nothing re-ran it"


class FiringSlots(Schedule):
    """Drives _schedule_loop through slots A and B, holding B at `hold_at` -
    its mark, or its station run - until the test releases it: a slot that
    has fired but not chained yet. Like the real service, next_auto_run()
    moves past a slot as soon as it is marked (the mark is in memory before
    mark_auto_run() has finished writing it), and a second run_plan while
    B's is held gets "running"."""

    def __init__(self, *, hold_at=None, **kwargs):
        super().__init__(**kwargs)
        self.hold_at = hold_at
        self.marks = 0
        self.holding = False
        self.held = asyncio.Event()
        self.release = asyncio.Event()

    def next_auto_run(self):
        if self.marks < 2:
            return datetime.now(timezone.utc) - timedelta(seconds=1)
        return super().next_auto_run()

    async def wait_for_config_change(self, timeout):
        raise _Stop

    async def mark_auto_run(self):
        self.marks += 1
        await self._hold("mark")
        return "previous"

    async def restore_auto_run_mark(self, previous):
        raise AssertionError("nothing here is a busy slot")

    async def run_plan(self, *, trigger):
        if self.holding and self.hold_at == "run_plan":
            return {"status": "running"}
        await self._hold("run_plan")
        return await super().run_plan(trigger=trigger)

    async def _hold(self, at):
        if at == self.hold_at and self.marks == 2 and not self.held.is_set():
            self.held.set()
            self.holding = True
            try:
                await self.release.wait()
            finally:
                self.holding = False


class WatchedCampaign(Campaign):
    """Counts the chained cycles that ran while slot B was held."""

    def __init__(self, outcomes, schedule):
        super().__init__(outcomes)
        self.schedule = schedule
        self.during_slot = 0

    async def run_chained_cycle(self):
        if self.schedule.holding:
            self.during_slot += 1
        return await super().run_chained_cycle()


@pytest.mark.parametrize("hold_at", ["mark", "run_plan"])
async def test_a_retry_leaves_a_slot_that_has_fired_to_that_slot(held_clock, caplog, hold_at):
    """Interval auto-runs can be 5 minutes apart. Slot A's station run and
    chain both failed transiently, and their retries wake while slot B has
    fired but not chained yet. next_auto_run() already answers the slot
    after B, and B has run neither half yet, so the next-slot guard and the
    stand-down both used to miss B: A's chain retry committed, then B's own
    chain committed again straight after."""
    caplog.set_level(logging.INFO, logger="app.scheduler")
    schedule = FiringSlots(hold_at=hold_at, results=[dict(NETWORK_DOWNLOAD), dict(BOOKED)])
    campaign = WatchedCampaign([TRANSIENT_SKIP, COMMITTED], schedule)
    owner = owner_for(schedule, campaign)

    loop = asyncio.create_task(owner._schedule_loop())
    await asyncio.wait_for(schedule.held.wait(), 5)     # B has fired; A's retries sleep
    held_clock.gate.set()                               # ... and wake now
    await asyncio.wait_for(asyncio.gather(owner._chain_retry_task,
                                          owner._station_retry_task), 5)

    assert campaign.during_slot == 0, "A's chain retry ran while B was in progress"
    assert schedule.runs == ["auto"], "A's station retry ran while B was in progress"
    assert "chained campaign retry 1/5 not run: a slot is in progress" in caplog.text
    assert "station run retry 1/3 not run: a slot is in progress" in caplog.text
    schedule.release.set()
    with pytest.raises(_Stop):
        await asyncio.wait_for(loop, 5)
    await drain()
    assert schedule.runs == ["auto", "auto"], "A's run, then B's"
    assert campaign.calls == 2, "A's transient skip, then ONE commit: B's own"


async def test_a_slot_whose_own_chain_fails_still_gets_a_retry(held_clock):
    """The retry that left slot B to itself is gone by the time B's chain
    fails transiently too, so B arms a fresh one, which commits once."""
    schedule = FiringSlots(hold_at="run_plan", results=[dict(BOOKED), dict(BOOKED)])
    campaign = WatchedCampaign([TRANSIENT_SKIP, TRANSIENT_SKIP, COMMITTED], schedule)
    owner = owner_for(schedule, campaign)

    loop = asyncio.create_task(owner._schedule_loop())
    await asyncio.wait_for(schedule.held.wait(), 5)
    for_a = owner._chain_retry_task
    held_clock.gate.set()
    await asyncio.wait_for(for_a, 5)                    # left B to itself
    schedule.release.set()
    with pytest.raises(_Stop):
        await asyncio.wait_for(loop, 5)
    for_b = owner._chain_retry_task
    await drain()

    assert for_b is not for_a
    assert campaign.during_slot == 0
    assert campaign.calls == 3, "A's skip, B's skip, then B's retry commits"


async def test_a_station_retry_a_slot_re_armed_runs_while_that_slot_chains(held_clock):
    """A slot's station half is over once its run_plan returns. When that run
    hit network_download too, the pending station retry now stands for this
    slot, and it must not leave the station run to a slot that has already
    done it - even while that slot's chain is still going."""
    schedule = FiringSlots(results=[dict(NETWORK_DOWNLOAD), dict(NETWORK_DOWNLOAD), dict(BOOKED)])
    chaining, chain_done = asyncio.Event(), asyncio.Event()

    class SlowChain(Campaign):
        async def run_chained_cycle(self):
            self.calls += 1
            if self.calls == 2:                         # slot B's chain
                chaining.set()
                await chain_done.wait()
            return COMMITTED
    campaign = SlowChain([])
    owner = owner_for(schedule, campaign)

    loop = asyncio.create_task(owner._schedule_loop())
    await asyncio.wait_for(chaining.wait(), 5)
    held_clock.gate.set()
    await asyncio.wait_for(owner._station_retry_task, 5)

    assert schedule.runs == ["auto"] * 3, "A, B, then B's station retry during B's chain"
    chain_done.set()
    with pytest.raises(_Stop):
        await asyncio.wait_for(loop, 5)
    await drain()
    assert campaign.calls == 2


# --- R4: stop() ---------------------------------------------------------------------------

def stoppable(owner):
    async def nothing():
        return None
    owner.rotator = SimpleNamespace(stop=nothing)
    owner.rig = SimpleNamespace(stop=nothing)
    owner._tasks = []
    return owner


async def test_stop_cancels_pending_retries(held_clock):
    schedule = LoopSchedule(slots=1, results=[dict(NETWORK_DOWNLOAD)])
    campaign = Campaign([TRANSIENT_SKIP])
    owner = stoppable(owner_for(schedule, campaign))
    await run_slots(owner)
    retries = background()
    assert len(retries) == 2

    await asyncio.wait_for(owner.stop(), 5)

    assert all(task.cancelled() for task in retries)
    assert schedule.runs == ["auto"] and campaign.calls == 1


async def test_a_retry_cancelled_mid_run_releases_both_guards(tmp_path, monkeypatch, clock):
    """run_plan, preview_campaign and commit_campaign clear their guards in a
    finally, so a stop() landing mid-attempt cannot leave either service
    answering "running" for the rest of the process."""
    schedule, campaign, _network = real_services(tmp_path, monkeypatch)
    await schedule.save_config(db_token=DB_TOKEN, network_token=NETWORK_TOKEN,
                               auto_run_enabled=True, auto_run_chain_campaign=True)
    one_real_slot(schedule, monkeypatch)
    runs: list = []
    never = asyncio.Event()

    async def run_then_hang(cfg, on_line=None, log_path=None):
        runs.append(cfg)
        if len(runs) == 1:
            return network_download_outcome()   # the slot's run
        await never.wait()                      # the retry's: still going at stop()
    scripted_runs(schedule, monkeypatch, [])
    monkeypatch.setattr(autoscheduler_cli, "run", run_then_hang)
    previews: list = []
    release = threading.Event()
    slot_error = incident_exception()

    def preview_then_hang():
        previews.append(1)
        if len(previews) == 1:
            raise slot_error                    # the slot's preview: the HTTP 500
        release.wait(10)                        # the retry's: still going at stop()
        raise RuntimeError("released once the test is done")
    monkeypatch.setattr(campaign, "_preview_sync", preview_then_hang)
    owner = stoppable(owner_for(schedule, campaign))
    with pytest.raises(_Stop):
        await owner._schedule_loop()
    try:
        for _ in range(500):                    # both retries reach their real work
            if len(runs) == 2 and len(previews) == 2:
                break
            await _real_sleep(0.01)
        assert (len(runs), len(previews)) == (2, 2)
        assert schedule.is_running() and campaign.is_running()

        await asyncio.wait_for(owner.stop(), 5)

        assert not schedule.is_running() and not campaign.is_running()
        assert schedule.get_last_run()["booked_state"] == "unconfirmed", (
            "an interrupted run is still recorded as possibly booked")
    finally:
        release.set()


# --- R3: the campaign's own timer ---------------------------------------------------------

def timer_campaign(tmp_path, monkeypatch, exc, *, auto_commit=True):
    schedule, campaign, network = real_services(tmp_path, monkeypatch)
    monkeypatch.setattr(schedule, "campaign_auto_commit_enabled", lambda: auto_commit)
    scripted_previews(campaign, monkeypatch, [exc])
    return campaign, network


@pytest.mark.parametrize("auto_commit", [True, False])
async def test_a_transient_preview_failure_asks_the_timer_to_retry(tmp_path, monkeypatch,
                                                                  auto_commit):
    campaign, network = timer_campaign(tmp_path, monkeypatch, incident_exception(),
                                       auto_commit=auto_commit)

    assert await campaign.run_auto_cycle() == RETRY == cs.AUTO_CYCLE_RETRY
    assert network.posts == []
    assert campaign.auto_cycle_path.exists(), "the restart anchor is written as before"
    assert campaign.auto_cycle_delay_s() > 0, "so a reload now does not fire a cycle"


@pytest.mark.parametrize("exc", [
    SatnogsHTTPError("GET x -> HTTP 403", status=403),
    RateLimitedError("GET x -> HTTP 429", status=429),
    KeyError("lat"),
])
async def test_any_other_preview_failure_ends_the_cycle(tmp_path, monkeypatch, exc):
    campaign, _network = timer_campaign(tmp_path, monkeypatch, exc)

    assert await campaign.run_auto_cycle() == "done"


async def _timer(monkeypatch, outcomes) -> list:
    events: list = []
    outcomes = iter(outcomes)

    class TimerCampaign:
        def auto_cycle_delay_s(self):
            return 0.0

        async def run_auto_cycle(self):
            events.append("cycle")
            return next(outcomes)

    async def fake_sleep(seconds):
        events.append(("sleep", seconds))
        if seconds == 86400:
            raise _Stop
        # Instant sleeps never yield, so a loop that cannot end would hang
        # the suite rather than fail it.
        assert len(events) < 400, "the timer never ended the cycle"

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fake_sleep)
    owner = scheduler_module.Scheduler.__new__(scheduler_module.Scheduler)
    owner.campaign_service = TimerCampaign()
    owner.s = SimpleNamespace(campaign_poll_s=86400)
    with pytest.raises(_Stop):
        await owner._campaign_loop()
    return events


async def test_the_timer_retries_a_failed_preview_ten_minutes_later(monkeypatch):
    events = await _timer(monkeypatch, [RETRY, "done"])

    assert events == ["cycle", ("sleep", 600.0), "cycle", ("sleep", 86400)]


async def test_the_timer_gives_up_after_six_retries(monkeypatch):
    def always():
        while True:
            yield RETRY
    events = await _timer(monkeypatch, always())

    assert events == ["cycle", ("sleep", 600.0)] * 6 + ["cycle", ("sleep", 86400)]
    assert scheduler_module.Scheduler.CAMPAIGN_MAX_RETRIES == 6


async def test_busy_and_retry_answers_cannot_reset_each_other_into_a_loop(monkeypatch):
    """Alternating forever: each counter only ever counts its own answer, so
    the error retries run out (6) and the cycle ends."""
    def alternating():
        while True:
            yield cs.AUTO_CYCLE_BUSY
            yield RETRY
    events = await _timer(monkeypatch, alternating())

    expected = []
    for n in range(13):     # B R B R ... B: 7 busy and 6 error retries
        expected += ["cycle", ("sleep", 60.0 if n % 2 == 0 else 600.0)]
    assert events == expected + ["cycle", ("sleep", 86400)]


async def test_after_a_cycle_ends_both_counters_start_again(monkeypatch):
    def script():
        yield from [RETRY] * 7        # gives up: next day
        yield from [RETRY, "done"]    # a fresh allowance
    outcomes = script()
    events: list = []

    class TimerCampaign:
        def auto_cycle_delay_s(self):
            return 0.0

        async def run_auto_cycle(self):
            events.append("cycle")
            return next(outcomes)

    days = {"n": 0}

    async def fake_sleep(seconds):
        events.append(("sleep", seconds))
        if seconds == 86400:
            days["n"] += 1
            if days["n"] == 2:
                raise _Stop

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fake_sleep)
    owner = scheduler_module.Scheduler.__new__(scheduler_module.Scheduler)
    owner.campaign_service = TimerCampaign()
    owner.s = SimpleNamespace(campaign_poll_s=86400)
    with pytest.raises(_Stop):
        await owner._campaign_loop()

    assert events == (["cycle", ("sleep", 600.0)] * 6 + ["cycle", ("sleep", 86400)]
                      + ["cycle", ("sleep", 600.0), "cycle", ("sleep", 86400)])


# --- a booking sent between the failure and the retry makes the retry stand down ---------------

async def test_a_manual_commit_after_the_failed_chain_makes_its_retry_stand_down(
        tmp_path, monkeypatch, held_clock, caplog):
    # Review probe: 11:00Z the chain's preview hits the incident 500, a retry
    # is pending; 11:06Z the operator books the campaign by hand; 11:10Z the
    # retry wakes. It used to preview afresh and POST again minutes later,
    # from calendars that need not show the manual bookings yet - over the
    # per-station cap with no 409 to stop it.
    caplog.set_level(logging.INFO, logger="app.scheduler")
    schedule, campaign, network = real_services(tmp_path, monkeypatch)
    await schedule.save_config(db_token=DB_TOKEN, network_token=NETWORK_TOKEN,
                               auto_run_enabled=True, auto_run_chain_campaign=True,
                               campaign_loop_until_exhausted=False)
    scripted_previews(campaign, monkeypatch,
                      [incident_exception(), [campaign_item(40, 3), campaign_item(41, 4)]])
    owner = owner_for(schedule, campaign)
    await owner._after_auto_run({"status": "ok"})

    manual = await campaign.commit_campaign(items=[campaign_item(40, 3), campaign_item(41, 4)],
                                            trigger="manual")
    assert manual["status"] in ("ok", "ok_with_warnings") and network.posts == [2], manual

    held_clock.gate.set()
    await drain()

    assert network.posts == [2]
    assert [h["trigger"] for h in campaign.get_history()] == ["manual"]
    assert "already topped the stations up" in caplog.text


async def test_a_blocked_commit_in_between_does_not_stand_the_retry_down(
        tmp_path, monkeypatch, held_clock):
    # A "blocked" commit sends nothing and records no submit, so it is no
    # evidence the stations were topped up: the retry still runs and books.
    schedule, campaign, network = real_services(tmp_path, monkeypatch)
    await schedule.save_config(db_token=DB_TOKEN, network_token=NETWORK_TOKEN,
                               auto_run_enabled=True, auto_run_chain_campaign=True,
                               campaign_loop_until_exhausted=False)
    scripted_previews(campaign, monkeypatch,
                      [incident_exception(), [campaign_item(40, 3), campaign_item(41, 4)]])
    owner = owner_for(schedule, campaign)
    await owner._after_auto_run({"status": "ok"})

    offline = {"id": 5024, "status": "Offline", "last_seen": "2026-10-04T09:00:00Z", "age_s": 5}
    campaign._own_station = lambda: offline
    blocked = await campaign.commit_campaign(items=[campaign_item(40, 3)], trigger="manual")
    assert blocked["status"] == "blocked" and network.posts == []
    campaign._own_station = None

    held_clock.gate.set()
    await drain()

    assert network.posts == [2]
    assert [h["trigger"] for h in campaign.get_history()] == ["manual", "chained"]
