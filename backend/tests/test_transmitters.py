"""Transmitters, and which one this station is actually listening to.

The number on the radio panel is the one an operator types into a receiver, so
the failure that matters here is not an empty panel — it is a confident wrong
frequency. There are two ways to get one, and both are cheap to reintroduce:

**Picking the wrong transmitter.** A satellite usually publishes several and
they are not interchangeable. KNACKSAT-2 has a 145.825 MHz V/V digipeater and a
400.630 MHz UHF telemetry downlink; station 5024's three Yagis span 380-490 MHz
and hear only the second. "The first one in the list" tunes the wall to a band
the antenna physically cannot receive, and nothing on screen would say so.

**Sending the wrong filter.** `/transmitters/` on db.satnogs.org honours
`satellite__norad_cat_id`. The Network API silently ignores that spelling and
wants `norad_cat_id` instead. Both services answer 200 to the wrong one and
return *every* satellite, so the symptom is a populated panel showing somebody
else's spacecraft.

Nothing in this file touches the network, and `data_dir` is always a tmp_path,
so a real cache on the machine running the tests cannot leak in.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.services import transmitters as tx_mod
from app.services.transmitters import (
    TransmitterStore,
    UHF_HIGH_HZ,
    UHF_LOW_HZ,
    _summarise,
    doppler_shift_hz,
)

KNACKSAT2 = 67683
ISS = 25544

VHF_DIGI = 145_825_000.0
UHF_TLM = 400_630_000.0

REAL_CLIENT = httpx.AsyncClient


def tx(**overrides) -> dict:
    """A record shaped the way db.satnogs.org returns one."""
    base = {
        "uuid": "5bZHPjVmCPPhBMyqbQ4SnR",
        "description": "UHF Telemetry",
        "type": "Transmitter",
        "downlink_low": UHF_TLM,
        "downlink_high": None,
        "uplink_low": None,
        "mode": "FSK",
        "baud": 9600.0,
        "service": "Amateur",
        "status": "active",
        "alive": True,
        "invert": False,
        "iaru_coordination": "IARU Coordinated",
    }
    base.update(overrides)
    return base


def store(tmp_path, **overrides) -> TransmitterStore:
    base = dict(data_dir=tmp_path, station_id=5024, default_norad=KNACKSAT2)
    base.update(overrides)
    return TransmitterStore(Settings(**base))


def answer_with(monkeypatch, handler) -> list[httpx.Request]:
    """Point every client the store builds at `handler`. Returns the requests."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(**kwargs):
        return REAL_CLIENT(transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(tx_mod.httpx, "AsyncClient", factory)
    return seen


def no_network(monkeypatch) -> None:
    """Building a client at all is the failure, so fail at construction."""
    class Exploding:
        def __init__(self, **kwargs):
            raise AssertionError("the store reached for the network")

    monkeypatch.setattr(tx_mod.httpx, "AsyncClient", Exploding)


def ok(payload):
    return lambda request: httpx.Response(200, json=payload)


# --------------------------------------------------------------------------
# which transmitter, and why
# --------------------------------------------------------------------------

def test_a_vhf_downlink_is_not_chosen_for_a_uhf_station(tmp_path, monkeypatch):
    """The one this module exists for. KNACKSAT-2 lists both; 5024's Yagis
    span 380-490 MHz, so tuning the wall to 145.825 would be a number nobody
    on site can act on, displayed with total confidence."""
    answer_with(monkeypatch, ok([
        tx(uuid="vhf", description="V/V Digipeater", downlink_low=VHF_DIGI),
        tx(uuid="uhf", downlink_low=UHF_TLM),
    ]))
    s = store(tmp_path)

    # Listed lowest-frequency-first, which is the order that would trap a
    # "take the first one" implementation.
    assert (await_refresh(s))
    primary = s.primary_downlink(KNACKSAT2)

    assert primary["uuid"] == "uhf"
    assert primary["downlink_hz"] == UHF_TLM
    assert s.downlink_hz(KNACKSAT2) == UHF_TLM


def test_a_dead_transmitter_in_band_loses_to_a_live_one_in_band(
        tmp_path, monkeypatch):
    """`alive` is SatNOGS's own word for "this is still transmitting". A
    decommissioned beacon in the right band is still the wrong answer."""
    answer_with(monkeypatch, ok([
        tx(uuid="dead", downlink_low=400_000_000.0, alive=False),
        tx(uuid="live", downlink_low=UHF_TLM, alive=True),
    ]))
    s = store(tmp_path)
    assert (await_refresh(s))

    assert s.primary_downlink(KNACKSAT2)["uuid"] == "live"


def test_a_live_out_of_band_transmitter_beats_a_dead_in_band_one(
        tmp_path, monkeypatch):
    """Being audible is worth more than being in band when nothing is both.
    A satellite with no UHF at all should still put a real frequency on the
    panel rather than a decommissioned one."""
    answer_with(monkeypatch, ok([
        tx(uuid="dead-uhf", downlink_low=UHF_TLM, alive=False),
        tx(uuid="live-vhf", downlink_low=VHF_DIGI, alive=True),
    ]))
    s = store(tmp_path)
    assert (await_refresh(s))

    assert s.primary_downlink(KNACKSAT2)["uuid"] == "live-vhf"


def test_the_choice_is_stable_across_restarts(tmp_path, monkeypatch):
    """Two equally-ranked transmitters must not swap on restart. A tuned
    frequency that changes because a process was restarted is the kind of
    fault nobody thinks to look for."""
    answer_with(monkeypatch, ok([
        tx(uuid="higher", downlink_low=430_000_000.0),
        tx(uuid="lower", downlink_low=400_000_000.0),
    ]))
    s = store(tmp_path)
    assert (await_refresh(s))
    first = s.primary_downlink(KNACKSAT2)["uuid"]

    # Same records, opposite order from the API.
    answer_with(monkeypatch, ok([
        tx(uuid="lower", downlink_low=400_000_000.0),
        tx(uuid="higher", downlink_low=430_000_000.0),
    ]))
    again = store(tmp_path)
    assert (await_refresh(again, force=True))

    assert again.primary_downlink(KNACKSAT2)["uuid"] == first == "lower"


def test_the_station_band_only_ranks_and_never_hides(tmp_path, monkeypatch):
    """The operator is told which one is primary, not denied the other. The
    VHF downlink is real information about the spacecraft even on a station
    that cannot hear it."""
    answer_with(monkeypatch, ok([
        tx(uuid="vhf", downlink_low=VHF_DIGI),
        tx(uuid="uhf", downlink_low=UHF_TLM),
    ]))
    s = store(tmp_path)
    assert (await_refresh(s))

    assert {t["uuid"] for t in s.get(KNACKSAT2)} == {"vhf", "uhf"}


def test_a_transmitter_with_no_downlink_is_not_a_candidate(tmp_path, monkeypatch):
    """An uplink-only record has nothing to tune to. Returning it would put
    `None` on the panel where a frequency belongs."""
    answer_with(monkeypatch, ok([
        tx(uuid="uplink-only", downlink_low=None, downlink_high=None,
           uplink_low=145_900_000.0),
    ]))
    s = store(tmp_path)
    assert (await_refresh(s))

    assert s.primary_downlink(KNACKSAT2) is None
    assert s.downlink_hz(KNACKSAT2) is None


def test_no_transmitters_at_all_is_not_an_error(tmp_path, monkeypatch):
    answer_with(monkeypatch, ok([]))
    s = store(tmp_path)
    assert (await_refresh(s))

    assert s.get(KNACKSAT2) == []
    assert s.primary_downlink(KNACKSAT2) is None


def test_the_uhf_window_matches_the_antennas_on_the_mast():
    """380-490 MHz is not a round number someone liked — it is what station
    5024's three Yagis actually span. If the antennas change, this changes."""
    assert UHF_LOW_HZ <= UHF_TLM <= UHF_HIGH_HZ
    assert not (UHF_LOW_HZ <= VHF_DIGI <= UHF_HIGH_HZ)


# --------------------------------------------------------------------------
# the filter, which is the thing these APIs lie about
# --------------------------------------------------------------------------

async def test_the_filter_is_the_db_spelling_and_not_networks(
        tmp_path, monkeypatch):
    """`norad_cat_id` is what the Network API wants. Sent here it is accepted,
    ignored, and answered with every satellite in the database — a full panel
    of somebody else's frequencies."""
    seen = answer_with(monkeypatch, ok([tx()]))
    await store(tmp_path).refresh(KNACKSAT2)

    params = seen[0].url.params
    assert params["satellite__norad_cat_id"] == str(KNACKSAT2)
    assert "norad_cat_id" not in [k for k in params.keys()
                                 if k != "satellite__norad_cat_id"]
    assert seen[0].url.path.endswith("/transmitters/")


async def test_the_user_agent_identifies_this_station(tmp_path, monkeypatch):
    """A volunteer-run service should be able to see who is asking."""
    seen = answer_with(monkeypatch, ok([tx()]))
    await store(tmp_path).refresh(KNACKSAT2)

    assert "knacksat2-ground-station" in seen[0].headers["user-agent"]


async def test_a_token_is_sent_only_when_there_is_one(tmp_path, monkeypatch):
    """/transmitters/ is public. The token is optional here, unlike
    /telemetry/, and an empty one must not become a literal "Token "."""
    seen = answer_with(monkeypatch, ok([tx()]))
    await store(tmp_path, satnogs_db_token="").refresh(KNACKSAT2)
    assert "authorization" not in seen[0].headers

    seen = answer_with(monkeypatch, ok([tx()]))
    await store(tmp_path, satnogs_db_token="abc123").refresh(KNACKSAT2, force=True)
    assert seen[0].headers["authorization"] == "Token abc123"


# --------------------------------------------------------------------------
# not fetching
# --------------------------------------------------------------------------

async def test_a_fresh_cache_does_not_go_back_to_satnogs(tmp_path, monkeypatch):
    """Transmitters change on the scale of months. Every panel refresh asking
    the DB again is a lot of traffic at a volunteer-run service for an answer
    that was already right."""
    seen = answer_with(monkeypatch, ok([tx()]))
    s = store(tmp_path)
    await s.refresh(KNACKSAT2)
    await s.refresh(KNACKSAT2)

    assert len(seen) == 1


async def test_offline_does_not_reach_the_network(tmp_path, monkeypatch):
    no_network(monkeypatch)
    assert await store(tmp_path, offline=True).refresh(KNACKSAT2) is False


async def test_transmitters_survive_a_restart(tmp_path, monkeypatch):
    """A station that cannot reach the internet still needs to know what to
    tune to. That is the entire point of the disk cache."""
    answer_with(monkeypatch, ok([tx()]))
    await store(tmp_path).refresh(KNACKSAT2)

    no_network(monkeypatch)
    after = store(tmp_path)
    assert after.downlink_hz(KNACKSAT2) == UHF_TLM


async def test_a_failed_fetch_keeps_what_it_already_had(tmp_path, monkeypatch):
    """Last month's frequency still tunes the radio. An empty panel does not,
    and the frequency genuinely has not changed."""
    answer_with(monkeypatch, ok([tx()]))
    s = store(tmp_path)
    await s.refresh(KNACKSAT2)

    def dead(request):
        raise httpx.ConnectError("no route to host")

    answer_with(monkeypatch, dead)
    assert await s.refresh(KNACKSAT2, force=True) is False
    assert s.downlink_hz(KNACKSAT2) == UHF_TLM


async def test_a_non_200_does_not_overwrite_good_data(tmp_path, monkeypatch):
    answer_with(monkeypatch, ok([tx()]))
    s = store(tmp_path)
    await s.refresh(KNACKSAT2)

    answer_with(monkeypatch, lambda r: httpx.Response(503, text="later"))
    assert await s.refresh(KNACKSAT2, force=True) is False
    assert len(s.get(KNACKSAT2)) == 1


async def test_a_page_of_junk_is_not_read_as_no_transmitters(
        tmp_path, monkeypatch):
    """A captive portal answers 200 with an HTML login page. Reading that as
    "this satellite has no transmitters" would blank the radio panel."""
    answer_with(monkeypatch, ok([tx()]))
    s = store(tmp_path)
    await s.refresh(KNACKSAT2)

    answer_with(monkeypatch, ok({"detail": "not a list"}))
    assert await s.refresh(KNACKSAT2, force=True) is False
    assert len(s.get(KNACKSAT2)) == 1


def test_a_corrupt_cache_is_ignored_rather_than_fatal(tmp_path):
    """A half-written file from a power cut must not stop the backend."""
    (tmp_path / "transmitters.json").write_text("{not json", encoding="utf-8")
    assert store(tmp_path).get(KNACKSAT2) == []


# --------------------------------------------------------------------------
# what reaches the panel
# --------------------------------------------------------------------------

def test_the_summary_drops_the_catalogue_furniture():
    """The raw records carry ITU notification blobs and citation URLs, and
    this goes over the WebSocket to every display."""
    summary = _summarise(tx(
        citation="https://example.invalid/a-very-long-citation",
        itu_notification={"urls": ["https://example.invalid/itu"]},
    ))

    assert "citation" not in summary
    assert "itu_notification" not in summary
    assert summary["downlink_hz"] == UHF_TLM
    assert summary["mode"] == "FSK"
    json.dumps(summary)


def test_downlink_high_is_used_when_low_is_missing():
    """A transponder publishes a range. Either end is a better answer than
    None, which would take the frequency off the panel entirely."""
    summary = _summarise(tx(downlink_low=None, downlink_high=UHF_TLM))
    assert summary["downlink_hz"] == UHF_TLM


def test_a_record_with_no_frequency_at_all_summarises_to_none():
    summary = _summarise(tx(downlink_low=None, downlink_high=None))
    assert summary["downlink_hz"] is None


# --------------------------------------------------------------------------
# doppler
# --------------------------------------------------------------------------

def test_closing_raises_the_observed_frequency():
    """The sign is the whole content of this function. Inverted, the radio is
    tuned the wrong way by twice the shift for the entire pass — about 18 kHz
    at 400 MHz, which is a clean miss."""
    assert doppler_shift_hz(UHF_TLM, -7.0) > 0
    assert doppler_shift_hz(UHF_TLM, +7.0) < 0


def test_the_shift_is_zero_at_closest_approach():
    """Range rate passes through zero at TCA, and so must the correction."""
    assert doppler_shift_hz(UHF_TLM, 0.0) == 0.0


def test_the_shift_is_the_size_a_leo_pass_actually_produces():
    """A sanity bound, not a precise figure: ~7.5 km/s line-of-sight at
    400 MHz is about 10 kHz. A unit slip anywhere in here — km/s read as m/s —
    lands three orders out, and this is what catches it."""
    shift = doppler_shift_hz(UHF_TLM, -7.5)
    assert 8_000 < shift < 12_000


@pytest.mark.parametrize("rate", [-7.5, -1.0, 0.0, 1.0, 7.5])
def test_the_shift_is_antisymmetric_in_the_range_rate(rate):
    assert doppler_shift_hz(UHF_TLM, rate) == pytest.approx(
        -doppler_shift_hz(UHF_TLM, -rate))


# --------------------------------------------------------------------------
# helper
# --------------------------------------------------------------------------

def await_refresh(store_obj, force: bool = False) -> bool:
    """Run one refresh synchronously.

    The ranking tests are about `primary_downlink`, which is synchronous; the
    fetch is only how the records get in. Keeping those tests non-async says
    that, and keeps the assertion on the last line of the test.
    """
    return asyncio.run(store_obj.refresh(KNACKSAT2, force=force))
