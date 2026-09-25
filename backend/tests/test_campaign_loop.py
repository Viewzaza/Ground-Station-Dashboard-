"""Looping a campaign commit until nothing is left to book.

With campaign_loop_until_exhausted on, one commit submits a batch of up to
campaign_max_total, recomputes, and submits again until a round comes back
empty. The properties that matter:

* it actually stops once no bookings are left;
* the per-station cap holds across the whole loop, not per round, so looping
  reaches more stations instead of stacking passes onto the same ones;
* a round where SatNOGS rejects everything ends the loop instead of hammering it;
* with the setting off, a commit is still exactly one batch.

Nothing here touches the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services import campaign_service as cs
from app.services.campaign_service import CampaignService
from app.vendor.autoscheduler.campaign import build_campaign
from app.vendor.autoscheduler.network_client import ScheduleResult

MISSION = 67683


def item(station_id: int, n: int) -> dict:
    start = datetime(2026, 9, 24, tzinfo=timezone.utc) + timedelta(hours=station_id, minutes=n * 20)
    return {
        "station_id": station_id, "station_name": f"S{station_id}",
        "transmitter_uuid": "tx", "start": start.isoformat(),
        "end": (start + timedelta(minutes=8)).isoformat(),
    }


class StubNetwork:
    def __init__(self, reject_all: bool = False):
        self.reject_all = reject_all
        self.batches: list[int] = []

    def schedule(self, items, execute=False):
        assert execute
        self.batches.append(len(items))
        if self.reject_all:
            return ScheduleResult(submitted=len(items), errors=["x: HTTP 400 no"] * len(items))
        return ScheduleResult(submitted=len(items), accepted=len(items), accepted_items=list(items))


def make_service(tmp_path, monkeypatch, *, loop: bool, network: StubNetwork):
    settings = Settings(data_dir=tmp_path, mock=False, campaign_mock=False, default_norad=MISSION)

    class StubSchedule:
        cache_dir = tmp_path / "cache"

        def _build_autoscheduler_settings(self, hours):
            return SimpleNamespace(network_token="", buffer_s=30.0)

        def _effective_network_token(self):
            return "token"

        def _effective_station_id(self):
            return None

        def campaign_loop_until_exhausted(self):
            return loop

    monkeypatch.setattr(cs, "Cache", lambda *a, **k: None)
    monkeypatch.setattr(cs, "NetworkClient", lambda *a, **k: network)
    monkeypatch.setattr(cs, "DbClient", lambda *a, **k: None)
    return CampaignService(settings, StubSchedule())


def fake_builder(stations: int, per_station: int, max_total: int):
    """Stands in for build_campaign: every station has `per_station` passes,
    booked_counts removes the ones already taken, capped at max_total."""
    def build(network, db, auto_settings, booked_counts=None):
        out = []
        for sid in range(1, stations + 1):
            for n in range((booked_counts or {}).get(sid, 0), per_station):
                if len(out) >= max_total:
                    return out
                out.append(item(sid, n))
        return out
    return build


def test_loop_keeps_submitting_until_nothing_is_left(tmp_path, monkeypatch):
    network = StubNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=True, network=network)
    monkeypatch.setattr(svc, "_build_items", fake_builder(stations=7, per_station=2, max_total=5))

    result = svc._commit_sync(None, "manual")

    assert network.batches == [5, 5, 4]
    assert result["accepted"] == 14 == len(result["accepted_items"])
    assert result["rounds"] == 3
    assert result["stopped_reason"] == "no bookings left"
    # Every accepted booking is also in the recent-attempts guard, so a later
    # preview cannot resubmit any of them.
    assert sum(len(v) for v in svc._recent_bookings_by_station(datetime.now(timezone.utc)).values()) == 14


def test_loop_off_is_a_single_batch(tmp_path, monkeypatch):
    network = StubNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=False, network=network)
    monkeypatch.setattr(svc, "_build_items", fake_builder(stations=7, per_station=2, max_total=5))

    result = svc._commit_sync(None, "manual")

    assert network.batches == [5]
    assert result["rounds"] == 1


def test_loop_stops_when_a_whole_round_is_rejected(tmp_path, monkeypatch):
    network = StubNetwork(reject_all=True)
    svc = make_service(tmp_path, monkeypatch, loop=True, network=network)
    monkeypatch.setattr(svc, "_build_items", fake_builder(stations=7, per_station=2, max_total=5))

    result = svc._commit_sync(None, "manual")

    assert network.batches == [5]
    assert result["accepted"] == 0
    assert "rejected" in result["stopped_reason"]


def test_loop_has_a_round_limit(tmp_path, monkeypatch):
    network = StubNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=True, network=network)
    # A builder that never runs dry - only the backstop can end this.
    monkeypatch.setattr(svc, "_build_items", lambda *a, **k: [item(1, 0)])

    result = svc._commit_sync(None, "manual")

    assert result["rounds"] == cs.MAX_LOOP_ROUNDS
    assert "safety limit" in result["stopped_reason"]


def test_previewed_items_are_the_first_round(tmp_path, monkeypatch):
    network = StubNetwork()
    svc = make_service(tmp_path, monkeypatch, loop=True, network=network)
    seen: list[dict] = []

    def build(network, db, auto_settings, booked_counts=None):
        seen.append(dict(booked_counts or {}))
        return []
    monkeypatch.setattr(svc, "_build_items", build)

    result = svc._commit_sync([item(3, 0), item(3, 1), item(4, 0)], "manual")

    assert network.batches == [3]
    # The follow-up round was told what the reviewed batch already gave each station.
    assert seen == [{3: 2, 4: 1}]
    assert result["stopped_reason"] == "no bookings left"


def test_build_campaign_skips_stations_already_at_their_cap():
    stations = [SimpleNamespace(id=1, name="full", segments=[]),
                SimpleNamespace(id=2, name="other", segments=[])]
    touched: list[int] = []

    class Net:
        def all_stations(self):
            return stations

    class Db:
        def tles(self):
            return {MISSION: object()}

        def transmitters_for_station(self, segments):
            touched.append(id(segments))
            return {}

    preview = build_campaign(
        Net(), Db(), mission_norad=MISSION, transmitter_uuid=None,
        now=datetime.now(timezone.utc), exclude_station_id=None,
        max_per_station=2, max_total=150, booked_counts={1: 2},
    )

    reasons = {s["station_id"]: s["reason"] for s in preview.skipped}
    assert "per-station cap" in reasons[1]
    # Station 1 was dropped before any further lookups; only station 2 got that far.
    assert touched == [id(stations[1].segments)]
