"""Cross-check our pass predictions against SatNOGS's own.

SatNOGS runs an independent propagator to decide when station 5024 will record.
Its job list is therefore an oracle: if our Skyfield predictions and their
schedule disagree, the antenna will be pointed at the wrong patch of sky at the
wrong time.

This test found a real bug. With a 5 degree horizon mask our AOS ran a
consistent 74-96 seconds late; station 5024 publishes min_horizon = 0, and at
that mask the two agree to within a few seconds. The lesson is in the assertion
below: match the station's configured horizon, not a plausible-looking default.

Needs the network, so it is marked and skipped by default:
    pytest -m network
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from app.config import Settings
from app.services.predictor import Predictor
from app.services.tle_store import TleStore

pytestmark = pytest.mark.network

KNACKSAT2 = 67683
TOLERANCE_S = 30.0


@pytest.fixture(scope="module")
def live():
    settings = Settings()
    store = TleStore(settings)
    if store.get(KNACKSAT2) is None:
        pytest.skip("no cached elements; run the backend once to populate them")
    return settings, Predictor(settings, store)


def _fetch(url: str, params: dict):
    try:
        resp = httpx.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        pytest.skip(f"SatNOGS unreachable: {exc}")


def test_station_horizon_matches_our_configuration(live):
    """Our mask must equal the station's, or every pass time will be offset."""
    settings, _ = live
    station = _fetch(
        f"{settings.satnogs_network}/stations/", {"id": settings.station_id}
    )[0]

    assert station["min_horizon"] == pytest.approx(settings.min_elevation_deg), (
        f"station {settings.station_id} uses a {station['min_horizon']}° horizon "
        f"but GS_MIN_ELEVATION_DEG is {settings.min_elevation_deg}°; pass times "
        f"will not agree with the SatNOGS schedule"
    )


def test_passes_agree_with_the_satnogs_schedule(live):
    settings, predictor = live
    jobs = _fetch(
        f"{settings.satnogs_network}/jobs/", {"ground_station": settings.station_id}
    )

    # `norad_cat_id` is the filter that works here. `satellite__norad_cat_id`
    # is silently ignored by the Network API and returns every satellite.
    scheduled = [j for j in jobs if j["norad_cat_id"] == KNACKSAT2]
    if not scheduled:
        pytest.skip("no KNACKSAT-2 observations currently scheduled on 5024")

    ours = predictor.passes(KNACKSAT2, hours=48.0)
    assert ours, "we predict no passes at all in the next 48 hours"

    deltas = []
    for job in scheduled:
        start = datetime.fromisoformat(job["start"].replace("Z", "+00:00"))
        nearest = min(ours, key=lambda p: abs((p.aos - start).total_seconds()))
        deltas.append((p_aos := nearest.aos, start, (p_aos - start).total_seconds()))

    for ours_aos, theirs, delta in deltas:
        assert abs(delta) < TOLERANCE_S, (
            f"AOS disagrees with SatNOGS by {delta:+.1f}s "
            f"(ours {ours_aos.isoformat()}, theirs {theirs.isoformat()})"
        )

    # A consistent one-sided offset means a systematic error — a wrong horizon
    # mask, site coordinates or time base — not just prediction noise.
    mean = sum(d for _, _, d in deltas) / len(deltas)
    assert abs(mean) < TOLERANCE_S / 2, f"systematic {mean:+.1f}s offset across all passes"
