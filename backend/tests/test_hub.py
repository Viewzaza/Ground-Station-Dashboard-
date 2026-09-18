"""Fan-out tests.

The snapshot is what a browser sees when it opens mid-pass, so the thing worth
testing is that it is actually complete — and that one slow client cannot make
the dashboard stale for everyone else.
"""

from __future__ import annotations

from app.hub import QUEUE_LIMIT, Hub


def drain(conn) -> list:
    out = []
    while not conn.queue.empty():
        out.append(conn.queue.get_nowait())
    return out


def test_every_client_receives_a_published_frame():
    hub = Hub()
    a, b = hub.register(), hub.register()
    hub.publish("rotator", {"az_raw": 12.0})

    assert [f.data["az_raw"] for f in drain(a)] == [12.0]
    assert [f.data["az_raw"] for f in drain(b)] == [12.0]


def test_sequence_numbers_are_monotonic_across_types():
    """One counter across all types is what lets a client detect a gap."""
    hub = Hub()
    conn = hub.register()
    hub.publish("rotator", {})
    hub.publish("satpos", {})
    hub.publish("tle", {})

    seqs = [f.seq for f in drain(conn)]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 3


def test_snapshot_carries_the_latest_frame_of_every_type():
    hub = Hub()
    hub.publish("rotator", {"az_raw": 1.0})
    hub.publish("rotator", {"az_raw": 2.0})       # supersedes the first
    hub.publish("satpos", {"lat": 13.8})

    types = {f["type"]: f["data"] for f in hub.snapshot().data["frames"]}
    assert types["rotator"]["az_raw"] == 2.0
    assert types["satpos"]["lat"] == 13.8


def test_status_is_kept_per_component_not_per_type():
    """Several producers publish `status`. Keeping only the newest by type
    would tell a new client about one component and nothing about the others."""
    hub = Hub()
    hub.publish("status", {"component": "rotctld", "state": "ok"})
    hub.publish("status", {"component": "tle", "state": "degraded"})
    hub.publish("status", {"component": "predictor", "state": "ok"})

    statuses = {
        f["data"]["component"]: f["data"]["state"]
        for f in hub.snapshot().data["frames"]
        if f["type"] == "status"
    }
    assert statuses == {"rotctld": "ok", "tle": "degraded", "predictor": "ok"}


def test_a_later_status_for_the_same_component_supersedes_it():
    hub = Hub()
    hub.publish("status", {"component": "rotctld", "state": "ok"})
    hub.publish("status", {"component": "rotctld", "state": "down"})

    statuses = [
        f["data"]["state"] for f in hub.snapshot().data["frames"]
        if f["type"] == "status"
    ]
    assert statuses == ["down"]


def test_a_stalled_client_drops_its_own_oldest_frames():
    """A dashboard wants the newest value, never a backlog — and a slow client
    must not be able to block a producer."""
    hub = Hub()
    slow = hub.register()

    for i in range(QUEUE_LIMIT + 20):
        hub.publish("rotator", {"az_raw": float(i)})

    frames = drain(slow)
    assert len(frames) <= QUEUE_LIMIT
    # Whatever was dropped, the most recent value must have survived.
    assert frames[-1].data["az_raw"] == float(QUEUE_LIMIT + 19)


def test_unregistered_clients_stop_receiving():
    hub = Hub()
    conn = hub.register()
    hub.unregister(conn)
    hub.publish("rotator", {"az_raw": 5.0})
    assert drain(conn) == []
    assert hub.client_count == 0


def test_subscriptions_filter_by_topic():
    """A phone can ask for less than a wall display."""
    hub = Hub()
    conn = hub.register()
    conn.topics = {"rotator"}

    hub.publish("rotator", {})
    hub.publish("satpos", {})

    assert [f.type for f in drain(conn)] == ["rotator"]
