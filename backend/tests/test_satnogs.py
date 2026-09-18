"""SatNOGS polling.

The interlock reads its answers off this service, so "how old is this" is as
much a part of the contract as "what does it say".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.services.satnogs import SatnogsService, next_page_url


def service(**overrides) -> SatnogsService:
    return SatnogsService(Settings(station_id=5024, **overrides))


def iso(delta_s: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat()


# --------------------------------------------------------------------------
# pagination
# --------------------------------------------------------------------------

def test_next_page_comes_from_the_link_header():
    """The Network API has no ?page=; the next URL is in a Link header."""
    header = '<https://network.satnogs.org/api/observations/?cursor=cD0y>; rel="next"'
    assert next_page_url(header) == "https://network.satnogs.org/api/observations/?cursor=cD0y"


def test_no_link_header_means_no_next_page():
    assert next_page_url(None) is None
    assert next_page_url("") is None


def test_a_prev_only_link_header_has_no_next():
    header = '<https://network.satnogs.org/api/observations/?cursor=cD0x>; rel="prev"'
    assert next_page_url(header) is None


def test_next_is_found_among_several_links():
    header = (
        '<https://x/api/?cursor=a>; rel="prev", '
        '<https://x/api/?cursor=b>; rel="next"'
    )
    assert next_page_url(header) == "https://x/api/?cursor=b"


# --------------------------------------------------------------------------
# scheduled jobs
# --------------------------------------------------------------------------

def test_no_jobs_means_no_next_job():
    svc = service()
    svc.jobs = []
    assert svc.seconds_to_next_job() is None


def test_the_soonest_future_job_is_reported():
    svc = service()
    svc.jobs = [
        {"start": iso(3600), "end": iso(4000)},
        {"start": iso(600), "end": iso(900)},
        {"start": iso(7200), "end": iso(7500)},
    ]
    assert svc.seconds_to_next_job() == pytest.approx(600, abs=5)


def test_a_job_in_progress_reports_zero_not_a_negative():
    """A negative number would compare as comfortably outside a guard window,
    which is precisely backwards for a job that is recording right now."""
    svc = service()
    svc.jobs = [{"start": iso(-60), "end": iso(240)}]
    assert svc.seconds_to_next_job() == 0.0


def test_finished_jobs_are_ignored():
    svc = service()
    svc.jobs = [{"start": iso(-7200), "end": iso(-6900)}]
    assert svc.seconds_to_next_job() is None


def test_an_unparsable_start_is_skipped_not_fatal():
    svc = service()
    svc.jobs = [{"start": "not a date", "end": None}, {"start": iso(300), "end": iso(600)}]
    assert svc.seconds_to_next_job() == pytest.approx(300, abs=5)


# --------------------------------------------------------------------------
# freshness
# --------------------------------------------------------------------------

def test_nothing_fetched_yet_has_no_age():
    """None means unknown, and the interlock must not read it as fresh."""
    svc = service()
    assert svc.station_age_s is None
    assert svc.jobs_age_s is None
    assert svc.is_connected is None


def test_is_connected_reflects_the_station_record():
    svc = service()
    svc.station = {"is_connected": True}
    assert svc.is_connected is True
    svc.station = {"is_connected": False}
    assert svc.is_connected is False


# --------------------------------------------------------------------------
# the wire payload
# --------------------------------------------------------------------------

def test_observations_are_summarised_to_what_the_panel_draws():
    summary = SatnogsService._summarise({
        "id": 12345,
        "norad_cat_id": 67683,
        "tle0": "KNACKSAT-2",
        "start": "2026-09-13T02:00:00Z",
        "end": "2026-09-13T02:12:00Z",
        "status": "good",
        "vetted_status": "good",
        "waterfall": "https://example/waterfall.png",
        "demoddata": [{"payload_demod": "a"}, {"payload_demod": "b"}],
        "transmitter_description": "UHF Telemetry",
        "ignored": "x" * 5000,
    })
    assert summary["id"] == 12345
    assert summary["norad"] == 67683
    assert summary["waterfall"] is True
    assert summary["demoddata"] == 2
    assert summary["url"].endswith("/observations/12345/")
    assert "ignored" not in summary


def test_a_snapshot_is_serialisable_before_anything_is_fetched():
    """A browser connecting before the first poll must still get a frame."""
    snap = service().snapshot()
    assert snap["station"] is None
    assert snap["jobs"] == []
    assert snap["observations"] == []
    assert snap["seconds_to_next_job"] is None
