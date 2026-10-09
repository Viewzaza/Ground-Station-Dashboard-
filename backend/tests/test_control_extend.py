"""Extending a lease, and only extending one.

EXTEND exists because an armed panel had no way to keep its lease: ARM turned
into RELEASE, and RELEASE stops the antenna and disengages autopilot. So
autopilot could not outlive one GS_CONTROL_LEASE_S without the operator
tearing down the very thing they wanted kept running.

The unsafe versions of EXTEND are the ones these tests rule out: one that arms
when there is nothing to extend — a stale click from a panel that has not seen
the lease lapse, turned into a lease nobody consciously took — and one that
reads to autopilot as an operator taking over, which would disengage it on the
press meant to keep it going.

Nothing here can move hardware: the rotators are the recording fakes from
test_control.py and test_autopilot.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import control as control_routes
from app.services.control import ControlRefused
from app.services.rotator_mock import MockRotator
from app.services.rotctld_client import RotctldClient

# Helpers only, imported by name: importing the modules' test functions would
# have pytest collect them a second time, here.
from tests.test_autopilot import NORAD_A, NOW, cand, fast_track_loop, make_rig  # noqa: F401
from tests.test_control import FakePredictor, FakeSatnogs, build, settings


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------

def test_extend_without_a_lease_is_refused_and_does_not_arm():
    """The unsafe case: a stale EXTEND, sent after the lease the panel was
    showing had already lapsed, becoming a fresh lease. Extending is refused
    unless there is something to extend; arming stays ARM's job."""
    service = build()
    with pytest.raises(ControlRefused) as exc:
        service.extend()
    assert exc.value.blocked_by == ["armed"]
    assert "press ARM" in str(exc.value)
    assert service._lease_expires is None
    assert not service.armed


def test_extend_after_the_lease_has_lapsed_is_refused():
    """The same case, with a lease that existed and ran out a moment ago: the
    expiry is still on record, but nothing is held."""
    service = build()
    service.arm()
    lapsed = datetime.now(timezone.utc) - timedelta(seconds=1)
    service._lease_expires = lapsed
    with pytest.raises(ControlRefused) as exc:
        service.extend()
    assert exc.value.blocked_by == ["armed"]
    assert service._lease_expires == lapsed
    assert not service.armed


def test_extend_is_refused_while_the_kill_switch_is_off():
    """An extended lease on a disabled system would be an armed light on a
    dead control, exactly as an arm would."""
    service = build(rotator_control_enabled=False)
    with pytest.raises(ControlRefused) as exc:
        service.extend()
    assert exc.value.blocked_by == ["kill_switch"]


def test_extend_pushes_a_live_lease_out_to_a_full_one():
    service = build()
    service.arm()
    service._lease_expires = datetime.now(timezone.utc) + timedelta(seconds=10)
    seq = service.command_seq

    state = service.extend()

    floor = datetime.now(timezone.utc) + timedelta(seconds=899)
    assert state.armed
    assert state.lease_expires_at >= floor
    assert service._lease_expires >= floor
    # Not journaled: extending consent is not a command to the antenna.
    assert service.command_seq == seq


def test_extend_does_not_check_the_other_gates():
    """Like arm(): a lease kept while SatNOGS has the station is how an
    operator waits a job out without losing autopilot."""
    service = build(sat=FakeSatnogs(is_connected=True))
    service.arm()
    service._lease_expires = datetime.now(timezone.utc) + timedelta(seconds=10)
    state = service.extend()
    assert state.armed
    assert "satnogs_idle" in state.blocked_by


# --------------------------------------------------------------------------
# autopilot
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extend_does_not_disengage_autopilot(make_rig):
    """The point of the button. Autopilot reads the command journal to see an
    operator take over; an EXTEND that moved it would disengage the autopilot
    it was pressed to keep running."""
    aos = NOW + timedelta(minutes=10)
    rig = make_rig([cand("p", NORAD_A, aos)])
    rig.control.arm()
    rig.ex.enable()
    await rig.ex.step(NOW)
    phase = rig.ex.state.phase
    seq = rig.control.command_seq

    rig.control.extend()
    await rig.ex.step(NOW)

    assert rig.ex.state.enabled
    assert rig.ex.state.phase == phase
    assert rig.control.command_seq == seq


# --------------------------------------------------------------------------
# the route
# --------------------------------------------------------------------------

def _client(service) -> TestClient:
    app = FastAPI()
    app.include_router(control_routes.router, prefix="/api")
    app.state.control = service
    return TestClient(app)


def test_route_refuses_extend_while_unarmed_with_the_gate_named():
    """A refusal is a 409 naming the gate, so the panel can say what to do."""
    service = build()
    resp = _client(service).post("/api/control/extend")
    assert resp.status_code == 409
    assert resp.json()["detail"]["blocked_by"] == ["armed"]
    assert service._lease_expires is None


def test_route_extends_a_live_lease():
    service = build()
    service.arm()
    service._lease_expires = datetime.now(timezone.utc) + timedelta(seconds=10)
    resp = _client(service).post("/api/control/extend")
    assert resp.status_code == 200
    expires = datetime.fromisoformat(resp.json()["lease_expires_at"])
    assert expires >= datetime.now(timezone.utc) + timedelta(seconds=899)


def test_get_control_reports_the_limits_in_force():
    """The limits a click on the plot is turned into an azimuth against. The
    client's own limits() wins over the compiled caps — on station 5024 those
    differ, and the compiled ones are the wrong ones."""
    service = build()
    service.rotator.client.limits = lambda: (-90.0, 450.0, 0.0, 100.0)
    body = _client(service).get("/api/control").json()
    assert body["limits"] == {"min_az": -90.0, "max_az": 450.0,
                              "min_el": 0.0, "max_el": 100.0}


def test_get_control_reports_the_configured_limits_before_dump_caps_answers():
    """The shipped rotctld client, before identification: no caps yet, and
    still the station's own range — not None, which only a client without
    limits() can produce. The client is built but never connected."""
    service = build()
    service.rotator.client = RotctldClient("127.0.0.1", 1, min_az=-90.0, max_az=450.0,
                                           min_el=0.0, max_el=100.0)
    assert service.rotator.client.caps is None
    body = _client(service).get("/api/control").json()
    assert body["limits"] == {"min_az": -90.0, "max_az": 450.0,
                              "min_el": 0.0, "max_el": 100.0}


def test_get_control_reports_the_mock_rotators_limits():
    service = build()
    service.rotator.client = MockRotator(settings(), FakePredictor())
    body = _client(service).get("/api/control").json()
    assert body["limits"] == {"min_az": -90.0, "max_az": 450.0,
                              "min_el": 0.0, "max_el": 100.0}


def test_get_control_falls_back_to_caps_then_to_nothing():
    """For a client without limits(). None ships today — the test fakes are
    the only ones — but a rotator added later should not take the panel down
    with it."""
    service = build()
    body = _client(service).get("/api/control").json()
    assert body["limits"] == {"min_az": -180.0, "max_az": 540.0,
                              "min_el": -20.0, "max_el": 210.0}

    service.rotator.client.caps = None
    body = _client(service).get("/api/control").json()
    assert body["limits"] is None
