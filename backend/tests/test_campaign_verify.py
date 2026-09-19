"""Cross-checking a campaign run against the stations' real calendars.

`network.schedule()` reporting a booking accepted and that booking actually
sitting on a community station's calendar are two different claims, and only
the second one means anything will be recorded. Everything here is about the
gap between them.

Three distinctions carry the whole feature:

* **accepted is not scheduled.** A run whose POST came back clean can still
  have nothing on the calendar — the owner can cancel, or another observation
  can supersede it. That is what `missing` is for, and it is the only state an
  operator has to act on.
* **absent is not missing.** `future_bookings()` lists only observations that
  have not started yet, so a booking already under way has legitimately
  vanished from that feed. Reporting it `missing` would send someone chasing a
  problem that does not exist, and it is reachable on any check made more than
  a few minutes after the fact.
* **matching is by overlap, not by equality.** SatNOGS splits long passes into
  segments of its own choosing, so what comes back need not share our
  boundaries — only our window.

Nothing here touches the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.campaign_service import CampaignService
from app.vendor.autoscheduler.network_client import Booking

MISSION = 67683
OTHER_SAT = 25544


class StubNetwork:
    """Just the one method build/verify actually calls."""

    def __init__(self, by_station: dict[int, list[Booking]], fail: set[int] | None = None):
        self.by_station = by_station
        self.fail = fail or set()
        self.calls: list[int] = []

    def future_bookings(self, station_id: int, now=None) -> list[Booking]:
        self.calls.append(station_id)
        if station_id in self.fail:
            raise RuntimeError("station unreachable")
        return self.by_station.get(station_id, [])


def booking(bid: int, start: datetime, minutes: int = 10, norad: int = MISSION) -> Booking:
    return Booking(id=bid, norad_cat_id=norad, start=start,
                   end=start + timedelta(minutes=minutes), status="future")


@pytest.fixture
def service(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, mock=False, campaign_mock=False,
                        default_norad=MISSION)

    class StubSchedule:
        cache_dir = tmp_path / "cache"

        def _build_autoscheduler_settings(self, hours):
            return SimpleNamespace(network_token="")

        def _effective_network_token(self):
            return "token"

        def _effective_station_id(self):
            return 5024

    svc = CampaignService(settings, StubSchedule())
    return svc


def run_with(svc, network, items, monkeypatch):
    """Seed a last-run record and point the service at a stub network."""
    monkeypatch.setattr("app.services.campaign_service.NetworkClient",
                        lambda *a, **k: network)
    monkeypatch.setattr("app.services.campaign_service.Cache", lambda *a, **k: object())
    svc._write_json(svc.result_path, {
        "status": "ok", "trigger": "manual",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "submitted": len(items), "accepted": len(items), "errors": [],
        "accepted_items": items,
    })
    return svc._verify_sync()


def item(station_id: int, start: datetime, minutes: int = 10) -> dict:
    return {
        "station_id": station_id, "station_name": f"Station {station_id}",
        "transmitter_uuid": "tx", "start": start.isoformat(),
        "end": (start + timedelta(minutes=minutes)).isoformat(),
    }


def test_booking_present_on_the_calendar_is_confirmed(service, monkeypatch):
    start = datetime.now(timezone.utc) + timedelta(hours=3)
    network = StubNetwork({26: [booking(555, start)]})

    result = run_with(service, network, [item(26, start)], monkeypatch)

    assert result["status"] == "ok"
    (row,) = result["items"]
    assert row["state"] == "on_schedule"
    assert row["observation_id"] == 555


def test_accepted_but_absent_is_reported_missing(service, monkeypatch):
    """The state the whole feature exists for: the POST was taken, the
    calendar disagrees."""
    start = datetime.now(timezone.utc) + timedelta(hours=3)
    network = StubNetwork({26: []})

    result = run_with(service, network, [item(26, start)], monkeypatch)

    (row,) = result["items"]
    assert row["state"] == "missing"


def test_a_booking_already_under_way_is_not_called_missing(service, monkeypatch):
    """future_bookings() only lists what has not started, so a pass in
    progress is absent for a reason that is not a problem."""
    start = datetime.now(timezone.utc) - timedelta(minutes=5)
    network = StubNetwork({26: []})

    result = run_with(service, network, [item(26, start)], monkeypatch)

    (row,) = result["items"]
    assert row["state"] == "started"


def test_a_segment_overlapping_our_window_counts_as_scheduled(service, monkeypatch):
    """SatNOGS may split a pass, so the observation on the calendar can be
    shorter than and offset from what we asked for."""
    start = datetime.now(timezone.utc) + timedelta(hours=3)
    segment = booking(777, start + timedelta(minutes=4), minutes=3)
    network = StubNetwork({26: [segment]})

    result = run_with(service, network, [item(26, start, minutes=12)], monkeypatch)

    (row,) = result["items"]
    assert row["state"] == "on_schedule"
    assert row["observation_id"] == 777


def test_another_satellites_observation_does_not_count(service, monkeypatch):
    """An overlapping window booked for something else is somebody else's
    observation, not ours."""
    start = datetime.now(timezone.utc) + timedelta(hours=3)
    network = StubNetwork({26: [booking(888, start, norad=OTHER_SAT)]})

    result = run_with(service, network, [item(26, start)], monkeypatch)

    (row,) = result["items"]
    assert row["state"] == "missing"


def test_one_read_per_station_not_per_booking(service, monkeypatch):
    """Two accepted passes on one station is one calendar, and SatNOGS rate
    limits hard enough that the difference matters."""
    start = datetime.now(timezone.utc) + timedelta(hours=3)
    later = start + timedelta(hours=5)
    network = StubNetwork({26: [booking(1, start), booking(2, later)]})

    result = run_with(service, network,
                      [item(26, start), item(26, later)], monkeypatch)

    assert network.calls == [26]
    assert [r["state"] for r in result["items"]] == ["on_schedule", "on_schedule"]


def test_an_unreachable_station_is_unknown_not_missing(service, monkeypatch):
    """Failing to read a calendar says nothing about what is on it, and must
    not be reported as though the booking had vanished."""
    start = datetime.now(timezone.utc) + timedelta(hours=3)
    network = StubNetwork({}, fail={26})

    result = run_with(service, network, [item(26, start)], monkeypatch)

    (row,) = result["items"]
    assert row["state"] == "unknown"


def test_a_run_that_booked_nothing_has_nothing_to_check(service, monkeypatch):
    network = StubNetwork({})

    result = run_with(service, network, [], monkeypatch)

    assert result["status"] == "nothing_to_check"
    assert network.calls == []


def test_only_the_accepted_half_of_a_partial_batch_is_recorded(service, monkeypatch):
    """The commit record has to name the bookings that landed, not just count
    them — a partially-rejected batch is the normal case against a busy
    network, and it is what the cross-check later reads.

    `_commit_sync` maps the API's answer back onto the richer preview rows by
    object identity, which holds only because `schedule()` hands back the very
    dicts it was given. This pins that: copy them there and the run record
    silently starts claiming nothing was accepted.
    """
    start = datetime.now(timezone.utc) + timedelta(hours=3)
    kept, dropped = item(26, start), item(39, start)

    class PartialNetwork:
        def schedule(self, schedule_items, execute=False):
            assert execute is True
            taken = [s for s in schedule_items if s["ground_station"] == 26]
            return SimpleNamespace(submitted=len(schedule_items), accepted=len(taken),
                                   errors=["station 39 said no"], accepted_items=taken)

    monkeypatch.setattr("app.services.campaign_service.NetworkClient",
                        lambda *a, **k: PartialNetwork())
    monkeypatch.setattr("app.services.campaign_service.Cache", lambda *a, **k: object())
    monkeypatch.setattr(service, "_build_autoscheduler_settings",
                        lambda: SimpleNamespace(network_token="token"))

    result = service._commit_sync([kept, dropped], "manual")

    assert result["accepted"] == 1
    assert [r["station_id"] for r in result["accepted_items"]] == [26]
    # The rejected one must not be recorded as booked anywhere.
    assert all(r["station_id"] != 39 for r in result["accepted_items"])
