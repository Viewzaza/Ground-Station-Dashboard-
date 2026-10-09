"""The station logbook.

Mislabelling is the risk this file exists for. A log that says "lease expired"
for a release, or "operator: stop" for a stop the interlock sent, sends the
person reading it after an incident looking for a fault that is not there —
or past one that is. So the derivations are driven with the payloads the real
ControlService and PlanExecutor publish, captured off the hub, and each test
names the sequence of states it reads and the sentence it must produce.

The rest pins what makes it a record rather than a scroll: it survives a
restart, says when the last run did not end cleanly, never writes a secret,
and keeps going in memory when the disk will not take it.

Nothing here can move hardware: the rotators record the commands they are sent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.services.control as control_mod
import app.services.events as events_mod
from app.config import Settings
from app.hub import hub
from app.services.control import ControlRefused
from app.services.events import (
    AuditMiddleware,
    EventLog,
    derive_autopilot,
    derive_control,
    derive_plan,
    derive_satnogs,
    derive_status,
    id_key,
    redact_settings,
    warning_capture,
)
from tests.test_autopilot import NORAD_A, NOW, cand, make_rig  # noqa: F401 (fixture)
from tests.test_control import FakePredictor, FakeSatnogs, build


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fast_track_loop(monkeypatch):
    monkeypatch.setattr(control_mod, "TRACK_STEP_S", 0.01)


@pytest.fixture
def frames(monkeypatch):
    """Every frame published to the hub during the test, as (type, data)."""
    seen: list[tuple[str, dict]] = []
    real = hub.publish

    def publish(frame_type, data):
        seen.append((frame_type, data))
        return real(frame_type, data)

    monkeypatch.setattr(hub, "publish", publish)
    return seen


@pytest.fixture
def make_log(tmp_path):
    made: list[EventLog] = []

    def _make(settings=None, **kw):
        log_ = EventLog(settings or Settings(data_dir=tmp_path), **kw)
        made.append(log_)
        return log_

    yield _make
    for log_ in made:
        log_.detach()


@pytest.fixture
def capture_warnings():
    logger = logging.getLogger("app")
    added = warning_capture not in logger.handlers
    if added:
        logger.addHandler(warning_capture)
    yield
    if added:
        logger.removeHandler(warning_capture)


def replay_control(service, frames, *, engaged=False) -> list[dict]:
    """Run derive_control over the control frames, exactly as EventLog does."""
    out, prev = [], None
    for kind, data in frames:
        if kind != "control":
            continue
        since = int(prev["command_seq"]) if prev else 0
        out += derive_control(prev, data, service.journal_since(since),
                              autopilot_engaged=engaged)
        prev = data
    return out


def texts(drafts, kind=None) -> list[str]:
    return [d["text"] for d in drafts if kind is None or d["kind"] == kind]


def on_disk(tmp_path) -> list[dict]:
    out = []
    for path in sorted((tmp_path / "events").glob("*.jsonl")):
        out += EventLog._read_file(path)
    return out


# --------------------------------------------------------------------------
# the journal
# --------------------------------------------------------------------------

def test_the_journal_records_what_each_command_was_and_when():
    service = build()
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    seq = service._journal("operator", "goto az=1.0 el=2.0")
    [entry] = service.journal_since(seq - 1)
    assert (entry["seq"], entry["origin"], entry["what"]) == (seq, "operator",
                                                              "goto az=1.0 el=2.0")
    at = datetime.fromisoformat(entry["at"])
    assert before <= at <= datetime.now(timezone.utc) + timedelta(seconds=1)


def test_journal_since_returns_only_newer_entries_and_copies_them():
    service = build()
    for i in range(5):
        service._journal("operator", f"cmd {i}")
    assert [e["what"] for e in service.journal_since(3)] == ["cmd 3", "cmd 4"]
    assert service.journal_since(5) == []
    service.journal_since(0)[0]["what"] = "tampered"
    assert service.journal_since(0)[0]["what"] == "cmd 0"


@pytest.mark.asyncio
async def test_every_command_journals_what_it_was():
    service = build()
    service.arm()
    await service.goto(10.0, 20.0)
    await service.park()
    await service.track(67683)
    await service.stop()
    await service.release()
    assert [e["what"] for e in service.journal_since(0)] == [
        "goto az=10.0 el=20.0", "park az=0.0 el=0.0", "track 67683", "stop", "release",
    ]
    # The journal autopilot reads is untouched: origins, by position.
    assert [service.origin_of(n) for n in range(1, 6)] == ["operator"] * 5


@pytest.mark.asyncio
async def test_a_lease_lapse_that_stops_a_slew_is_journaled_and_logged(frames):
    """check_lease's stop is a command nobody pressed. It must read as the
    lease's, after the line saying the lease ran out — not as an operator
    stop, and not as nothing."""
    service = build()
    service.arm()
    await service.goto(200.0, 10.0)
    service._lease_expires = datetime.now(timezone.utc) - timedelta(seconds=1)
    await service.check_lease()

    [entry] = service.journal_since(1)
    assert (entry["origin"], entry["what"]) == ("lease", "stop (lease expired)")
    lines = texts(replay_control(service, frames))
    assert "lease expired" in lines
    assert "lease: stop (lease expired)" in lines
    assert lines.index("lease expired") < lines.index("lease: stop (lease expired)")


# --------------------------------------------------------------------------
# control frames, as ControlService publishes them
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commands_are_logged_with_their_origins(frames):
    """ARM is not a journaled command — it takes a lease — so it reads as a
    lease line, and each command after it as one line naming who sent it."""
    service = build()
    service.arm()
    await service.goto(123.0, 45.0)
    await service.park(origin="autopilot")
    await service.stop()
    drafts = replay_control(service, frames)
    assert texts(drafts, "control.cmd") == [
        "operator: goto az=123.0 el=45.0",
        "autopilot: park az=0.0 el=0.0",
        "operator: stop",
    ]
    [armed] = texts(drafts, "control.lease")
    assert armed.startswith("operator armed until ") and armed.endswith("Z")
    assert all(d["sev"] == "info" for d in drafts)


@pytest.mark.asyncio
async def test_an_expired_lease_is_not_a_release_and_a_release_is_not_an_expiry(frames):
    service = build()
    service.arm()
    service._lease_expires = datetime.now(timezone.utc) - timedelta(seconds=1)
    service.publish()                 # what the scheduler's control loop does
    expired = replay_control(service, frames)
    assert texts(expired, "control.lease")[-1] == "lease expired"
    assert texts(expired, "control.cmd") == []

    frames.clear()
    other = build()
    other.arm()
    await other.release()
    released = replay_control(other, frames)
    assert texts(released, "control.cmd") == ["operator: release"]
    assert not any("expired" in line for line in texts(released))


@pytest.mark.asyncio
async def test_rearming_while_armed_is_an_extension_not_a_command(frames):
    service = build()
    service.arm()
    await asyncio.sleep(0.05)
    service.arm()
    drafts = replay_control(service, frames)
    lines = texts(drafts)
    assert lines[0].startswith("operator armed until")
    assert lines[1].startswith("lease extended to")
    assert texts(drafts, "control.cmd") == []


@pytest.mark.asyncio
async def test_a_gate_closing_mid_track_says_why_the_track_ended(frames):
    sat = FakeSatnogs(is_connected=False)
    service = build(sat=sat, pred=FakePredictor(az=180.0, el=45.0))
    service.arm()
    await service.track(67683)
    await asyncio.sleep(0.05)

    sat.is_connected = True           # satnogs-client comes back mid-pass
    service.publish()                 # the control loop sees the gate move
    for _ in range(200):
        await asyncio.sleep(0.01)
        if service._mode == "idle":
            break
    drafts = replay_control(service, frames)
    [ended] = [d for d in drafts if d["kind"] == "control.track_end"]
    assert ended["text"] == "track of 67683 ended — gate closed: satnogs_idle"
    assert ended["sev"] == "warn"
    assert "satnogs_idle closed" in texts(drafts, "control.gate")
    assert texts(drafts, "control.cmd") == ["operator: track 67683"]


def test_the_armed_gate_is_not_logged_twice():
    """armed is a gate too; the lease lines say it, and say more."""
    prev = {"armed": False, "command_seq": 0, "lease_expires_at": None,
            "gates": {"armed": False, "satnogs_idle": True}}
    cur = {"armed": True, "command_seq": 0, "lease_expires_at": "2026-10-09T07:15:00+00:00",
           "gates": {"armed": True, "satnogs_idle": True}}
    drafts = derive_control(prev, cur, [])
    assert texts(drafts) == ["operator armed until 07:15:00Z"]


def test_a_journal_that_moved_on_is_said_rather_than_hidden():
    prev = {"armed": True, "command_seq": 10, "gates": {}}
    cur = {"armed": True, "command_seq": 13, "gates": {}}
    entries = [{"seq": 13, "origin": "operator", "what": "stop", "at": None}]
    drafts = derive_control(prev, cur, entries)
    assert texts(drafts)[0].startswith("2 command(s) not recorded")
    assert texts(drafts, "control.cmd")[-1] == "operator: stop"


# --------------------------------------------------------------------------
# autopilot, against the real executor
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_operator_stop_under_autopilot_is_logged_before_the_disengage(make_rig,
                                                                               make_log):
    aos = NOW - timedelta(seconds=10)
    rig = make_rig([cand("p", NORAD_A, aos, minutes=10)])
    log_ = make_log(control=rig.control, executor=rig.ex)
    rig.ex._publish()                   # what run() says at start: the baseline
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(NOW)
    await rig.settle()

    await rig.control.stop()                              # operator
    await rig.ex.step(NOW + timedelta(seconds=1))
    log_.pump()

    lines = [(r["kind"], r["text"], r["sev"]) for r in log_._ring]
    kinds = [k for k, _, _ in lines]
    assert "autopilot.engaged" in kinds
    assert ("control.cmd", "autopilot: track 67683", "info") in lines
    stop = lines.index(("control.cmd", "operator: stop", "warn"))
    [off] = [i for i, (k, t, _) in enumerate(lines)
             if k == "autopilot.disengaged" and "operator took control (stop)" in t]
    assert stop < off
    assert lines[off][2] == "warn"


def test_autopilot_phases_collapse_and_standing_down_is_graded():
    off = {"enabled": False, "phase": "off", "detail": "", "current": None,
           "disengaged_because": ""}
    on = {**off, "enabled": True, "phase": "waiting", "detail": "engaged"}
    blocked = {**on, "phase": "blocked", "detail": "waiting for the interlock: satnogs_idle"}
    churn = {**blocked, "detail": "waiting for the interlock: satnogs_idle, no_imminent_pass"}

    assert derive_autopilot(None, off) == []
    assert texts(derive_autopilot(off, on)) == ["autopilot engaged"]
    [b] = derive_autopilot(on, blocked)
    assert (b["kind"], b["sev"]) == ("autopilot.phase", "warn")
    assert derive_autopilot(blocked, churn) == [], "detail churn is not a new line"

    by_hand = {**off, "disengaged_because": "switched off by the operator"}
    lapsed = {**off, "disengaged_because": "control lease ended — re-arm and re-engage"}
    crashed = {**off, "disengaged_because": "internal error: boom"}
    assert derive_autopilot(blocked, by_hand)[0]["sev"] == "info"
    assert derive_autopilot(blocked, lapsed)[0]["sev"] == "warn"
    assert derive_autopilot(blocked, crashed)[0]["sev"] == "bad"


# --------------------------------------------------------------------------
# status, SatNOGS, the plan
# --------------------------------------------------------------------------

def test_components_are_logged_on_change_and_not_for_a_healthy_boot():
    assert derive_status(None, {"component": "tle", "state": "ok"}) == []
    [down] = derive_status(None, {"component": "rotctld", "state": "down",
                                  "detail": "RPRT -5"})
    assert (down["sev"], down["text"]) == ("bad", "rotctld down — RPRT -5")
    [deg] = derive_status("ok", {"component": "satnogs", "state": "degraded"})
    assert deg["sev"] == "warn"
    assert derive_status("ok", {"component": "satnogs", "state": "ok"}) == []


def test_satnogs_recordings_are_dated_by_their_own_window():
    start, end = "2026-10-09T07:00:00Z", "2026-10-09T07:10:00Z"
    station = {"id": 5024, "status": "Online", "is_connected": True}
    queued = {"station": station, "running": [], "observations": [],
              "jobs": [{"id": 9, "norad": 67683, "tle0": "0 KNACKSAT-2",
                        "start": start, "end": end}]}
    recording = {"station": station, "jobs": [], "observations": [],
                 "running": [{"id": 9, "norad": 67683, "start": start, "end": end}]}
    done = {**recording, "running": []}

    [began] = derive_satnogs(queued, recording)
    assert began["kind"] == "satnogs.job_started"
    assert began["text"] == "SatNOGS started recording KNACKSAT-2 (67683) — job 9"
    assert began["ts"] == start
    [ended] = derive_satnogs(recording, done)
    assert (ended["kind"], ended["ts"]) == ("satnogs.job_ended", end)

    gone = {**done, "station": {**station, "is_connected": False}}
    [st] = derive_satnogs(done, gone)
    assert st["text"] == "station 5024: satnogs-client disconnected"
    assert derive_satnogs(None, queued) == [], "the first frame is a baseline"


@pytest.mark.asyncio
async def test_a_recording_that_ends_is_named_though_its_job_is_long_gone(make_log):
    """A running job carries no name, and has left the jobs list by the time
    it ends; the name it was queued under is remembered."""
    log_ = make_log()
    start, end = "2026-10-09T07:00:00Z", "2026-10-09T07:10:00Z"
    station = {"id": 5024, "status": "Online", "is_connected": True}
    queued = {"station": station, "running": [], "observations": [],
              "jobs": [{"id": 9, "norad": 67683, "tle0": "0 KNACKSAT-2",
                        "start": start, "end": end}]}
    recording = {"station": station, "jobs": [], "observations": [],
                 "running": [{"id": 9, "norad": 67683, "start": start, "end": end}]}
    for snap in (queued, {**recording}, {**recording}, {**recording, "running": []}):
        hub.publish("satnogs", snap)
    log_.pump()
    assert [r["text"] for r in log_._ring] == [
        "SatNOGS started recording KNACKSAT-2 (67683) — job 9",
        "SatNOGS finished recording KNACKSAT-2 (67683) — job 9",
    ]


def _planned(norad, aos, minutes=8, name="P"):
    return {"key": f"{norad}-{aos:%Y%m%d%H%M%S}", "norad": norad, "name": name,
            "aos": aos.isoformat(), "los": (aos + timedelta(minutes=minutes)).isoformat(),
            "max_el": 40.0}


def test_a_pass_rekeyed_by_a_second_is_not_a_plan_change():
    now = datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc)
    a = now + timedelta(hours=1)
    far = now + timedelta(hours=10)
    prev = {"built_at": now.isoformat(), "decisions": [],
            "planned": [_planned(1, a), _planned(2, far)]}
    # The same pass, its AOS refined across a second boundary — a new key —
    # and a pass outside the window dropped, which is not this window's news.
    cur = {"built_at": (now + timedelta(minutes=5)).isoformat(), "decisions": [],
           "planned": [_planned(1, a + timedelta(seconds=1))]}
    assert derive_plan(prev, cur, now) == []


def test_dropping_a_planned_pass_is_exactly_one_plan_change():
    now = datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc)
    a, b = now + timedelta(hours=1), now + timedelta(hours=2)
    prev = {"built_at": now.isoformat(), "decisions": [],
            "planned": [_planned(1, a, name="KNACKSAT-2"), _planned(2, b, name="ISS")]}
    cur = {"built_at": (now + timedelta(minutes=5)).isoformat(),
           "planned": [_planned(1, a + timedelta(seconds=1), name="KNACKSAT-2")],
           "decisions": [{**_planned(2, b, name="ISS"), "status": "satnogs",
                          "reason": "SatNOGS has it scheduled"}]}
    drafts = derive_plan(prev, cur, now)
    assert len(drafts) == 1
    [change] = drafts
    assert change["kind"] == "plan.change"
    assert change["text"] == "plan: −ISS 08:00Z (satnogs)"
    assert change["data"]["gained"] == []
    assert change["data"]["lost"][0]["reason"] == "SatNOGS has it scheduled"


@pytest.mark.asyncio
async def test_the_log_derives_one_line_per_plan_rebuild_from_hub_frames(make_log):
    log_ = make_log()
    now = datetime.now(timezone.utc)
    a, b = now + timedelta(hours=1), now + timedelta(hours=2)
    first = {"built_at": now.isoformat(), "decisions": [],
             "planned": [_planned(1, a), _planned(2, b)]}
    rekeyed = {**first, "planned": [_planned(1, a + timedelta(seconds=1)),
                                    _planned(2, b - timedelta(seconds=1))]}
    dropped = {**first, "planned": [_planned(1, a)]}
    for snap in (first, rekeyed, dropped):
        hub.publish("plan", snap)
    log_.pump()
    assert [r["kind"] for r in log_._ring] == ["plan.change"]


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_records_go_to_a_new_file_at_utc_midnight(make_log, tmp_path):
    clock = [datetime(2026, 10, 9, 23, 59, 59, 500000, tzinfo=timezone.utc)]
    log_ = make_log(clock=lambda: clock[0])
    log_.emit("note", "info", "before midnight")
    clock[0] = datetime(2026, 10, 10, 0, 0, 0, 500000, tzinfo=timezone.utc)
    log_.emit("note", "info", "after midnight")
    await log_._flush()
    events = tmp_path / "events"
    assert [r["text"] for r in EventLog._read_file(events / "2026-10-09.jsonl")] \
        == ["before midnight"]
    assert [r["text"] for r in EventLog._read_file(events / "2026-10-10.jsonl")] \
        == ["after midnight"]


@pytest.mark.asyncio
async def test_retention_removes_old_days_then_the_oldest_until_under_the_cap(make_log,
                                                                              tmp_path):
    events = tmp_path / "events"
    events.mkdir()
    today = date(2026, 10, 9)
    sizes = {200: 10, 181: 10, 180: 10, 30: 400_000, 10: 400_000, 0: 400_000}
    for days_ago, size in sizes.items():
        (events / f"{today - timedelta(days=days_ago)}.jsonl").write_bytes(b"\n" * size)
    settings = Settings(data_dir=tmp_path, events_retain_days=180, events_max_mb=1.0)
    log_ = make_log(settings, clock=lambda: datetime(2026, 10, 9, 12, tzinfo=timezone.utc))
    await log_._retention()

    left = sorted(p.name for p in events.glob("*.jsonl"))
    # 181 and 200 days old go on age; then 1.2 MB is over the 1 MB cap, so
    # the oldest go until it is not — and today's file is never one of them.
    assert left == [f"{today - timedelta(days=10)}.jsonl", f"{today}.jsonl"]
    [note] = [r for r in log_._ring if r["kind"] == "retention"]
    assert note["data"]["removed"] == sorted(
        str(today - timedelta(days=n)) for n in (200, 181, 180, 30))


@pytest.mark.asyncio
async def test_a_torn_last_line_is_skipped_and_the_next_record_starts_clean(make_log,
                                                                            tmp_path):
    events = tmp_path / "events"
    events.mkdir()
    then = datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc)
    good = {"id": f"{int(then.timestamp() * 1000)}-1", "ts": then.isoformat(),
            "kind": "control.cmd", "sev": "info", "text": "operator: stop", "data": {}}
    (events / "2026-10-09.jsonl").write_bytes(
        json.dumps(good).encode() + b"\n" + b'{"id": "17910000-2", "ts": "2026-10-09T08:0')

    log_ = make_log(clock=lambda: datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc))
    await log_.start()
    assert [r["text"] for r in log_._ring][0] == "operator: stop"
    [unclean] = [r for r in log_._ring if r["kind"] == "unclean_restart"]
    assert unclean["data"]["last_seen"] == then.isoformat()
    # The torn tail did not swallow the records written after it.
    assert [r["kind"] for r in EventLog._read_file(events / "2026-10-09.jsonl")] \
        == ["control.cmd", "boot", "unclean_restart"]


@pytest.mark.asyncio
async def test_an_unwritable_directory_falls_back_to_the_ring_and_says_so(make_log,
                                                                          tmp_path):
    (tmp_path / "events").write_text("not a directory")
    states = []
    log_ = make_log(on_state=lambda c, s, d="": states.append((c, s, d)))
    await log_.start()
    log_.emit("note", "info", "kept in memory")
    await log_._flush()

    assert any(c == "events" and s == "degraded" and d for c, s, d in states)
    assert (tmp_path / "events").read_text() == "not a directory"
    items, _ = await log_.query()
    assert items[0]["text"] == "kept in memory"
    assert any(r["kind"] == "boot" for r in items)
    assert await log_.days() == []


@pytest.mark.asyncio
async def test_a_query_older_than_the_ring_reads_the_day_files(make_log, tmp_path,
                                                               monkeypatch):
    monkeypatch.setattr(events_mod, "RING_SIZE", 5)
    writer = make_log()
    for i in range(12):
        writer.emit("note", "info", f"note {i}")
    await writer._flush()
    writer.detach()

    reader = make_log()
    await reader.start()
    assert len(reader._ring) == 5
    everything = sorted(on_disk(tmp_path), key=lambda r: id_key(r["id"]), reverse=True)
    items, more = await reader.query(limit=50)
    assert [r["id"] for r in items] == [r["id"] for r in everything]
    assert more is False

    page, more = await reader.query(kinds="note", limit=4)
    assert more is True
    older, _ = await reader.query(kinds="note", until=page[-1]["id"], limit=50)
    assert [r["text"] for r in page + older] == [f"note {i}" for i in range(11, -1, -1)]


# --------------------------------------------------------------------------
# boots, restarts and the configuration
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_records_without_a_shutdown_mean_an_unclean_restart(make_log):
    first = make_log()
    await first.start()
    first.emit("control.cmd", "info", "operator: stop")
    await first._flush()
    first.detach()                         # killed: no shutdown record

    second = make_log()
    await second.start()
    [unclean] = [r for r in second._ring if r["kind"] == "unclean_restart"]
    assert unclean["data"]["last_kind"] == "control.cmd"
    await second.close()

    third = make_log()
    await third.start()
    assert not [r for r in third._ring if r["kind"] == "unclean_restart"
                and r["id"] > [b for b in third._ring if b["kind"] == "boot"][-1]["id"]]


@pytest.mark.asyncio
async def test_a_restart_while_engaged_says_autopilot_is_now_off_once(make_log):
    """Said, never acted on: the executor starts off whatever the log says."""
    first = make_log()
    await first.start()
    first.emit("autopilot.engaged", "info", "autopilot engaged")
    await first.close()

    second = make_log()
    await second.start()
    warned = [r for r in second._ring if r["kind"] == "autopilot.disengaged"]
    assert len(warned) == 1 and warned[0]["sev"] == "warn"
    assert warned[0]["text"] == ("backend restarted — autopilot was engaged and is now "
                                 "off; re-engaging is a human decision")
    await second.close()

    third = make_log()
    await third.start()
    assert len([r for r in third._ring if r["kind"] == "autopilot.disengaged"]) == 1


@pytest.mark.asyncio
async def test_a_setting_changed_between_boots_is_logged(make_log, tmp_path):
    first = make_log(Settings(data_dir=tmp_path, rot_limit_max_az=450.0))
    await first.start()
    await first.close()
    second = make_log(Settings(data_dir=tmp_path, rot_limit_max_az=540.0))
    await second.start()
    [change] = [r for r in second._ring if r["kind"] == "config_change"]
    assert change["text"] == "rot_limit_max_az 450 → 540"
    assert change["sev"] == "warn", "a travel limit is a safety setting"


class AlertSettings(Settings):
    """The secret names other features bring: alert webhooks and tokens."""
    alert_webhook_url: str = ""
    alert_telegram_token: str = ""
    alert_ntfy_topic: str = ""
    satnogs_network_token: str = ""


@pytest.mark.asyncio
async def test_no_secret_value_reaches_any_byte_written(make_log, tmp_path, frames,
                                                        capture_warnings):
    secrets = {
        "grafana_token": "graf-SECRET-1111",
        "satnogs_db_token": "db-SECRET-2222",
        "alert_webhook_url": "https://hooks.example/SECRET-3333",
        "alert_telegram_token": "tg-SECRET-4444",
        "alert_ntfy_topic": "ntfy-SECRET-5555",
        "satnogs_network_token": "net-SECRET-6666",
    }
    settings = AlertSettings(
        data_dir=tmp_path,
        go2rtc_url="http://admin:cam-SECRET-7777@video:1984/api?pass=SECRET-8888",
        **secrets,
    )
    log_ = make_log(settings)
    await log_.start()
    logging.getLogger("app.services.satnogs").warning(
        "poll failed with token %s", "db-SECRET-2222")
    log_.audit({"method": "POST", "path": "/api/control/goto", "client": ("127.0.0.1", 1),
                "headers": [(b"user-agent", b"curl/8.0")]}, 409,
               json.dumps({"az": "tg-SECRET-4444", "token": "graf-SECRET-1111"}).encode(),
               json.dumps({"detail": {"error": "refused: net-SECRET-6666",
                                      "blocked_by": []}}).encode())
    await log_.close()

    written = b"".join(p.read_bytes() for p in (tmp_path / "events").iterdir())
    published = json.dumps([d for t, d in frames if t == "log"]).encode()
    for value in [*secrets.values(), "cam-SECRET-7777", "SECRET-8888"]:
        assert value.encode() not in written, value
        assert value.encode() not in published, value
    snapshot = redact_settings(settings)
    assert snapshot["grafana_token"] == "<set>"
    assert snapshot["alert_webhook_url"] == "<set>"
    assert redact_settings(Settings(data_dir=tmp_path))["grafana_token"] == "<unset>"
    assert snapshot["go2rtc_url"] == "http://video:1984/api"


# --------------------------------------------------------------------------
# warnings and requests
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warnings_are_logged_once_and_repeats_are_counted(make_log, capture_warnings):
    mono = [1000.0]
    log_ = make_log(monotonic=lambda: mono[0])
    await log_.start()

    def logged():
        return [r for r in log_._ring if r["kind"].startswith("log.")]

    satnogs = logging.getLogger("app.services.satnogs")
    for i in range(5):
        satnogs.warning("SatNOGS poll failed (%d): %s", i + 1, "timeout")
    assert [r["text"] for r in logged()] == ["satnogs: SatNOGS poll failed (1): timeout"]
    mono[0] += 601
    log_.flush_repeats()
    assert logged()[-1]["text"] == ("satnogs: repeated 4 times in 10 min — last: "
                                    "SatNOGS poll failed (5): timeout")

    logging.getLogger("app.scheduler").error("task %s crashed", "rig")
    assert (logged()[-1]["kind"], logged()[-1]["sev"]) == ("log.error", "bad")

    # From a worker thread, as the planner's compute logs.
    await asyncio.to_thread(satnogs.warning, "from a thread %s", 1)
    await asyncio.sleep(0)
    assert logged()[-1]["text"] == "satnogs: from a thread 1"

    count = len(logged())
    # Already said by a derived line; and never this module's own warnings.
    logging.getLogger("app.services.planner_service").warning("autopilot ENGAGED")
    logging.getLogger("app.services.events").warning("cannot write")
    assert len(logged()) == count


class RefusingControl:
    """A control service whose interlock is shut, and which remembers what
    the route handed it."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.at: datetime | None = None

    async def goto(self, az, el, origin="operator"):
        self.calls.append((az, el))
        self.at = datetime.now(timezone.utc)
        raise ControlRefused(["satnogs_idle", "armed"])


def test_a_refused_goto_is_audited_with_its_gates_and_its_client(make_log):
    from app.routes import control as control_routes

    api = FastAPI()
    api.add_middleware(AuditMiddleware)
    api.include_router(control_routes.router, prefix="/api")
    api.state.control = RefusingControl()
    log_ = make_log()
    api.state.events = log_
    edge = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")
    with TestClient(api) as client:
        resp = client.post("/api/control/goto",
                           json={"az": 123.0, "el": 45.0, "note": "not kept"},
                           headers={"X-Forwarded-For": "192.0.2.10, 172.18.0.2",
                                    "User-Agent": edge})
    assert resp.status_code == 409
    # The middleware read the body, and the route still received all of it.
    assert api.state.control.calls == [(123.0, 45.0)]

    [audit] = [r for r in log_._ring if r["kind"] == "audit"]
    assert audit["text"] == ("POST /api/control/goto 409 blocked_by satnogs_idle, armed "
                             "from 192.0.2.10")
    assert audit["sev"] == "warn"
    assert audit["data"]["fields"] == {"az": 123.0, "el": 45.0}
    assert audit["data"]["ua"] == "Edge"
    assert audit["data"]["blocked_by"] == ["satnogs_idle", "armed"]
    assert audit["data"]["peer"] == "testclient"
    # Dated by arrival, so it reads as coming before what the route did.
    assert datetime.fromisoformat(audit["ts"]) <= api.state.control.at


def test_reads_and_websockets_are_not_audited(make_log):
    from app.routes import control as control_routes

    api = FastAPI()
    api.add_middleware(AuditMiddleware)
    api.include_router(control_routes.router, prefix="/api")
    log_ = make_log()
    api.state.events = log_
    with TestClient(api) as client:
        client.get("/api/control")
        client.post("/api/satellites", json={})
    assert [r for r in log_._ring if r["kind"] == "audit"] == []


def test_the_app_registers_the_audit_the_capture_and_the_routes():
    import app.main as main

    assert any(m.cls is AuditMiddleware for m in main.app.user_middleware)
    assert warning_capture in logging.getLogger("app").handlers
    paths = {getattr(r, "path", "") for r in main.app.routes}
    assert {"/api/events", "/api/events/days", "/api/events/day/{day}.jsonl"} <= paths


@pytest.mark.asyncio
async def test_the_events_api_filters_pages_and_downloads(make_log, tmp_path):
    from app.routes import events as event_routes

    api = FastAPI()
    api.include_router(event_routes.router, prefix="/api")
    log_ = make_log()
    api.state.events = log_
    first = log_.emit("control.cmd", "info", "operator: stop")
    log_.emit("audit", "warn", "POST /api/control/goto 409 blocked_by satnogs_idle")
    log_.emit("plan.change", "info", "plan: +KNACKSAT-2 08:00Z")
    await log_._flush()
    day = first["ts"][:10]

    with TestClient(api) as client:
        def get(**params):
            resp = client.get("/api/events", params=params)
            assert resp.status_code == 200, resp.text
            return resp.json()

        assert [r["kind"] for r in get()["items"]] == ["plan.change", "audit", "control.cmd"]
        assert [r["text"] for r in get(kinds="control")["items"]] == ["operator: stop"]
        assert [r["kind"] for r in get(min_sev="warn")["items"]] == ["audit"]
        assert [r["kind"] for r in get(q="knacksat")["items"]] == ["plan.change"]
        page = get(limit=2)
        assert len(page["items"]) == 2 and page["more"] is True
        assert [r["kind"] for r in get(since=first["id"])["items"]] == ["plan.change", "audit"]

        assert client.get("/api/events", params={"since": "yesterday"}).status_code == 422
        assert client.get("/api/events", params={"min_sev": "loud"}).status_code == 422
        assert client.get("/api/events", params={"limit": 5000}).status_code == 422

        assert client.get("/api/events/days").json() == [
            {"day": day, "bytes": (tmp_path / "events" / f"{day}.jsonl").stat().st_size}]
        download = client.get(f"/api/events/day/{day}.jsonl")
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("text/plain")
        assert "attachment" in download.headers["content-disposition"]
        assert [json.loads(line)["kind"] for line in download.text.splitlines()] \
            == ["control.cmd", "audit", "plan.change"]
        for bad in ("2026-02-30", "2026-1-9", "..%2F..%2Fsecret", "last_boot"):
            assert client.get(f"/api/events/day/{bad}.jsonl").status_code == 404, bad
        assert client.get("/api/events/day/2001-01-01.jsonl").status_code == 404


# --------------------------------------------------------------------------
# what a record can carry, and finding it again on disk
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_lone_surrogate_in_a_request_costs_nothing_but_itself(make_log, tmp_path,
                                                                     frames):
    """A lone surrogate is valid JSON ("\\ud800"), so a request body can carry
    one, and no UTF-8 encoder will take it. Kept as it came, one audited goto
    stopped the writer task, every open WebSocket and the log's own API."""
    from app.routes import control as control_routes
    from app.routes import events as event_routes

    api = FastAPI()
    api.add_middleware(AuditMiddleware)
    api.include_router(control_routes.router, prefix="/api")
    api.include_router(event_routes.router, prefix="/api")
    api.state.control = RefusingControl()
    log_ = make_log()
    api.state.events = log_
    # FastAPI's own 422 echoes the surrogate back and cannot render it either;
    # that answer is FastAPI's. What matters here is that nothing moved and the
    # log kept going.
    with TestClient(api, raise_server_exceptions=False) as client:
        client.post("/api/control/goto", content=b'{"az": "\\ud800", "el": 1.0}',
                    headers={"content-type": "application/json"})
        assert api.state.control.calls == []
        await log_._flush()
        listed = client.get("/api/events", params={"kinds": "audit"})
    assert listed.status_code == 200, listed.text
    [audit] = listed.json()["items"]
    assert audit["data"]["fields"] == {"az": "?", "el": 1.0}
    # A WebSocket encodes strictly; every log frame must survive it.
    for kind, data in frames:
        if kind == "log":
            json.dumps(data, ensure_ascii=False).encode("utf-8")
    assert [r["kind"] for r in on_disk(tmp_path)] == ["audit"]


@pytest.mark.asyncio
async def test_text_no_encoder_will_take_is_written_anyway(make_log, tmp_path):
    """An environment variable whose bytes are not UTF-8 reaches Python as
    lone surrogates. It must not stop the boot record, last_boot.json, or a
    record that reached the writer some other way."""
    settings = Settings(data_dir=tmp_path, go2rtc_url="http://video:1984/\udcff")
    log_ = make_log(settings)
    await log_.start()
    [boot] = [r for r in on_disk(tmp_path) if r["kind"] == "boot"]
    assert boot["data"]["settings"]["go2rtc_url"] == "http://video:1984/?"
    saved = json.loads((tmp_path / "events" / "last_boot.json").read_text("utf-8"))
    assert saved["go2rtc_url"] == "http://video:1984/?"

    stray = {"id": "1791000000000-1", "ts": "2026-10-09T08:00:00.000+00:00",
             "kind": "note", "sev": "info", "text": "bad \ud800 text", "data": {}}
    await asyncio.to_thread(log_._write, [stray])
    assert "bad ? text" in [r["text"] for r in on_disk(tmp_path)]


@pytest.mark.asyncio
async def test_a_run_restarted_after_a_crash_says_it_is_back(make_log):
    """The scheduler marks a crashed task down and runs it again; nothing
    else would ever have marked the logbook ok after that."""
    states = []
    log_ = make_log(on_state=lambda c, s, d="": states.append((c, s)))
    await log_.start()

    async def one_turn():
        task = asyncio.create_task(log_.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    await one_turn()
    assert states == [], "a first run is not news"
    await one_turn()
    assert states == [("events", "ok")]


def test_a_record_is_dated_no_later_than_it_was_made_nor_long_before(make_log):
    """ts may be a little before the id — when something happened rather than
    when it was noticed — but never after it, and never by much: that bound
    is what lets a reader find a record without opening every file."""
    now = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)
    log_ = make_log(clock=lambda: now)
    soon = log_.emit("note", "info", "two minutes ago", ts=now - timedelta(minutes=2))
    assert datetime.fromisoformat(soon["ts"]) == now - timedelta(minutes=2)
    ahead = log_.emit("note", "info", "in the future", ts=now + timedelta(minutes=2))
    assert datetime.fromisoformat(ahead["ts"]) == now
    # A recording's end, noticed only when SatNOGS came back hours later.
    late = log_.emit("satnogs.job_ended", "info", "ended", {"end": "x"},
                     ts=now - timedelta(hours=5))
    assert datetime.fromisoformat(late["ts"]) == now
    assert datetime.fromisoformat(late["data"]["happened_at"]) == now - timedelta(hours=5)
    assert late["data"]["end"] == "x"


def _lay_down(events, entries) -> list[dict]:
    """Write (recorded, dated, kind, text) entries as EventLog files them: by
    the day they are dated, in the order they were recorded."""
    out = []
    for n, (recorded, dated, kind, text) in enumerate(entries, start=1):
        record = {"id": f"{int(recorded.timestamp() * 1000)}-{n}",
                  "ts": dated.isoformat(timespec="milliseconds"), "kind": kind,
                  "sev": "info", "text": text, "data": {}}
        with open(events / f"{dated.date()}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        out.append(record)
    return out


@pytest.fixture
def opened(monkeypatch):
    """The day files a test's log opened, by whichever reader."""
    seen: list[str] = []
    for name in ("_read_file", "_scan_markers"):
        real = getattr(EventLog, name, None)
        if real is None:
            continue

        def spy(path, _real=real):
            seen.append(path.name)
            return _real(path)
        monkeypatch.setattr(EventLog, name, staticmethod(spy))
    return seen


def test_a_boot_reads_back_only_as_far_as_it_needs(make_log, tmp_path, monkeypatch, opened):
    """Station 5024 has never engaged autopilot, so there is no engage to
    find. Looking for one used to parse every retained day file at every
    boot, before the API or any loop could start."""
    monkeypatch.setattr(events_mod, "RING_SIZE", 5)
    events = tmp_path / "events"
    events.mkdir()
    entries = []
    for n in range(30):
        noon = datetime(2026, 9, 1, 12, tzinfo=timezone.utc) + timedelta(days=n)
        entries.append((noon, noon, "boot", f"boot {n}"))
        for i in range(1, 9):
            at = noon + timedelta(seconds=i)
            entries.append((at, at, "note", f"day {n} note {i}"))
        at = noon + timedelta(seconds=10)
        entries.append((at, at, "shutdown", "backend stopped"))
    written = _lay_down(events, entries)

    records, loaded_all, _, last_autopilot = make_log()._load()
    assert [r["id"] for r in records] == [r["id"] for r in written[-5:]]
    assert loaded_all is False
    assert last_autopilot["text"] == "boot 29"
    assert opened == ["2026-09-30.jsonl"]


@pytest.mark.asyncio
async def test_an_engage_older_than_the_ring_is_still_found_and_a_boot_ends_it(
        make_log, tmp_path, monkeypatch, opened):
    """A run that went on for days past its engage still restarts with the
    warning. And a boot settles the question for the run before it: every
    run starts with autopilot off."""
    monkeypatch.setattr(events_mod, "RING_SIZE", 5)
    events = tmp_path / "events"
    events.mkdir()
    noon = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    entries = [(noon, noon, "boot", "boot"),
               (noon + timedelta(minutes=1), noon + timedelta(minutes=1),
                "autopilot.engaged", "autopilot engaged")]
    for n in range(1, 4):
        for i in range(10):
            at = noon + timedelta(days=n, seconds=i)
            entries.append((at, at, "note", f"day {n} note {i}"))
    _lay_down(events, entries)

    _, _, _, last_autopilot = make_log()._load()
    assert last_autopilot["kind"] == "autopilot.engaged"
    # The ring came from the newest file; the older ones were only scanned.
    assert opened[0] == "2026-09-04.jsonl" and len(opened) == 4

    # A later run whose own start never said "now off" (its history could
    # not be read) still began with autopilot off. Its boot is the answer.
    later = noon + timedelta(days=5)
    _lay_down(events, [(later, later, "boot", "boot"),
                       (later + timedelta(seconds=1), later + timedelta(seconds=1),
                        "note", "after")])
    log_ = make_log()
    await log_.start()
    assert not [r for r in log_._ring if r["kind"] == "autopilot.disengaged"]


@pytest.mark.asyncio
async def test_paging_finds_a_record_dated_before_midnight_but_noticed_after(
        make_log, tmp_path, monkeypatch):
    """A recording that ended at 23:59:30 UTC and was noticed at 00:01 is
    filed under the old day with a new day's id. Paging stopped at the new
    day's file once a page was full, and skipped it."""
    clock = [datetime(2026, 10, 9, 23, 59, tzinfo=timezone.utc)]
    writer = make_log(clock=lambda: clock[0])
    writer.emit("note", "info", "A")
    clock[0] = datetime(2026, 10, 10, 0, 0, 5, tzinfo=timezone.utc)
    for i in range(5):
        clock[0] += timedelta(seconds=5)
        writer.emit("note", "info", f"B{i}")
    clock[0] = datetime(2026, 10, 10, 0, 1, tzinfo=timezone.utc)
    writer.emit("satnogs.job_ended", "info", "C", ts="2026-10-09T23:59:30Z")
    for i in range(4):
        clock[0] += timedelta(seconds=10)
        writer.emit("note", "info", f"D{i}")
    await writer._flush()
    writer.detach()
    assert [r["text"] for r in EventLog._read_file(tmp_path / "events" / "2026-10-09.jsonl")] \
        == ["A", "C"]
    newest_first = [r["text"] for r in sorted(on_disk(tmp_path), key=lambda r: id_key(r["id"]),
                                              reverse=True)]

    monkeypatch.setattr(events_mod, "RING_SIZE", 7)
    records, _, _, _ = make_log()._load()
    assert [r["text"] for r in reversed(records)] == newest_first[:7]

    # The same at the ring's edge: once a boot pushes B3 and B4 out, its
    # oldest record is C, dated before midnight. That does not make the ring
    # reach back past midnight; B0 to B4 are after it, and on disk.
    after_midnight = make_log(clock=lambda: clock[0] + timedelta(minutes=5))
    await after_midnight.start()
    assert after_midnight._ring[0]["text"] == "C"
    items, _ = await after_midnight.query(kinds="note", since="2026-10-10T00:00:00Z")
    assert [r["text"] for r in items] == ["D3", "D2", "D1", "D0", "B4", "B3", "B2", "B1", "B0"]
    await after_midnight.close()

    monkeypatch.setattr(events_mod, "RING_SIZE", 3)
    reader = make_log(clock=lambda: clock[0] + timedelta(minutes=5))
    await reader.start()
    page, more = await reader.query(kinds="note,satnogs", limit=2)
    paged = [r["text"] for r in page]
    while more:
        page, more = await reader.query(kinds="note,satnogs", until=page[-1]["id"], limit=2)
        paged += [r["text"] for r in page]
    assert paged == newest_first


@pytest.mark.asyncio
async def test_a_bound_from_the_edge_of_the_calendar_is_answered_not_a_500(make_log,
                                                                           monkeypatch):
    from app.routes import events as event_routes

    writer = make_log()
    for i in range(6):
        writer.emit("note", "info", f"note {i}")
    await writer._flush()
    writer.detach()
    monkeypatch.setattr(events_mod, "RING_SIZE", 3)
    api = FastAPI()
    api.include_router(event_routes.router, prefix="/api")
    log_ = make_log()
    await log_.start()
    api.state.events = log_
    with TestClient(api) as client:
        def get(**params):
            return client.get("/api/events", params={"limit": 50, **params})

        everything = get(since="0001-01-01T00:00:00Z")
        assert everything.status_code == 200
        assert [r["text"] for r in everything.json()["items"] if r["kind"] == "note"] \
            == [f"note {i}" for i in range(5, -1, -1)]
        for bad in ("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00",
                    "99999999999999999999-1"):
            assert get(since=bad).status_code == 422, bad
            assert get(until=bad).status_code == 422, bad
