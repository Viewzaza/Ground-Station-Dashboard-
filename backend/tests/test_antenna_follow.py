"""The display that follows the antenna, and what it must never do.

Following moves this browser's selection to the satellite the antenna is
working. That is display, but the selection is also what the control panel's
TRACK button sends — so a selection that moves by itself under an operator
holding the lease changes what their next command does. These tests pin the
review findings against the follow mode:

- AO-1: while an operator holds the lease and drives by hand, the selection
  moves only on someone's input — no following, no pin lifting itself.
- AO-2: a focus that moves while a follow is loading is still followed.
- AO-3: a follow older than an operator's pick never lands over it.
- AO-4: one failed request is retried, with backoff, and the chip says what
  failed and offers to retry rather than blaming missing elements.
- AO-5: with the link to the backend gone, the line says the owner is
  unknown — not the last frame's "NOBODY".

frontend/js/panels/antenna.js runs in Node on tests/js/antenna_follow.mjs,
which supplies the page, a virtual clock and main.js's selectSatellite step
for step; nothing here touches a browser, a server or a rotator. Each test
fails against the follow mode as first shipped. Skipped where Node is not
installed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "antenna_follow.mjs"
MAIN_JS = Path(__file__).parents[2] / "frontend" / "js" / "main.js"
NODE = shutil.which("node")

KN, ISS, XIV = 67683, 25544, 28895

needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run(scenario: str) -> dict[str, dict]:
    out = subprocess.run(
        [NODE, HARNESS.name, scenario], cwd=HARNESS.parent,
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout.strip().splitlines()[-1])
    return {s["step"]: s for s in report["steps"]}


@needs_node
def test_follow_pin_and_idle_resume_still_work_without_a_lease():
    """The feature as it shipped, so the fixes below are seen not to cost it:
    the wall follows, a pick pins, ten untouched minutes resume, and a focus
    the catalogue cannot draw is never selected."""
    steps = run("basics")
    assert steps["focus XI-V"]["sel"] == XIV
    assert steps["focus XI-V"]["chip"] == "following antenna"
    assert steps["pick ISS"]["sel"] == ISS
    assert steps["pick ISS"]["chipKind"] == "pinned"
    assert steps["idle"]["sel"] == XIV
    assert steps["focus not in catalogue"]["sel"] == XIV
    assert "not in catalogue" in steps["focus not in catalogue"]["chip"]
    assert steps["focus not in catalogue"]["selects"] == [f"{XIV}:landed", f"{XIV}:landed"]


@needs_node
def test_a_pin_does_not_lift_by_itself_while_the_lease_is_held():
    """AO-1 as reported: ISS picked, armed, nobody at the screen. The idle
    resume put KNACKSAT-2 under the TRACK button — with no chip, since the
    default needs none — and TRACK then tracked KNACKSAT-2."""
    steps = run("armed-pin-idle")
    idle = steps["idle, still armed"]
    assert idle["sel"] == ISS
    assert idle["selects"] == []
    assert idle["chipKind"] == "pinned" and idle["chipButton"]
    assert "control lease" in idle["chipTitle"]
    assert "resumes by itself" not in idle["chipTitle"]
    # Clicking the chip is someone choosing: that still follows.
    assert steps["chip clicked"]["sel"] == KN


@needs_node
def test_the_pin_lifts_once_the_lease_is_given_back():
    steps = run("armed-pin-released")
    assert steps["idle, armed"]["sel"] == ISS
    assert steps["released"]["sel"] == KN


@needs_node
def test_an_armed_operator_driving_by_hand_is_not_followed_away():
    """AO-1, the other half: no pin at all, and the focus moves on — a SatNOGS
    job coming up while the client is down, say. The selection used to move
    with it, and TRACK with the selection. Now the view is held, and the
    chip says so and offers to follow."""
    steps = run("armed-focus-moves")
    held = steps["armed, focus XI-V"]
    assert held["sel"] == KN
    assert held["selects"] == []
    assert held["chipKind"] == "held" and held["chipButton"]
    assert "TRACK" in held["chipTitle"]
    assert steps["chip clicked"]["sel"] == XIV
    # One click is one follow: the next move is held again.
    moved = steps["focus ISS, still armed"]
    assert moved["sel"] == XIV
    assert moved["chipKind"] == "held"
    assert moved["selects"] == [f"{XIV}:landed"]


@needs_node
def test_an_unpinned_screen_still_follows_autopilot():
    """Autopilot cannot run without a lease, and what it works is what the
    wall exists to show. A pin still holds while armed, autopilot or not."""
    steps = run("autopilot-follows")
    assert steps["autopilot on XI-V"]["sel"] == XIV
    pinned = steps["pinned, idle, autopilot"]
    assert pinned["sel"] == ISS
    assert pinned["chipKind"] == "pinned"
    assert steps["disengaged, chip clicked"]["sel"] == XIV


@needs_node
def test_a_focus_that_moves_while_a_follow_loads_is_still_followed():
    """AO-2: XI-V's elements take 1.5 s, and the focus moves on to ISS 0.4 s
    in. Frames are sent on change only, so nothing re-ran the follow and the
    screen sat on XI-V, with no chip, for as long as ISS was tracked."""
    steps = run("focus-moves-mid-select")
    # While XI-V is still on its way the screen is off the focus, and says
    # it is getting there rather than nothing at all.
    loading = steps["t+1"]
    assert loading["sel"] == KN
    assert loading["chip"] == "following antenna…"
    assert not loading["chipButton"]
    for t in ("t+3", "t+15"):
        assert steps[t]["sel"] == ISS, t
        assert steps[t]["chip"] == "following antenna", t
    # XI-V's elements, once they came, were not drawn on the way past.
    assert steps["t+15"]["selects"] == [f"{XIV}:dropped", f"{ISS}:landed"]


@needs_node
def test_a_follow_older_than_a_pick_does_not_land_over_it():
    """AO-3: the operator picks ISS while XI-V's elements are on their way.
    XI-V then landed over the pick, the chip read "following antenna", and
    the pin still pointed at ISS — which nobody could see."""
    steps = run("pick-mid-select")
    for t in ("t+1.5", "t+5.5"):
        assert steps[t]["sel"] == ISS, t
        assert steps[t]["chipKind"] == "pinned", t
    assert steps["t+5.5"]["selects"] == [f"{XIV}:dropped"]


@needs_node
def test_one_failed_request_is_retried():
    """AO-4: one failed /api/tle refused the focus until the antenna moved on
    — a whole track — and the chip blamed missing elements."""
    steps = run("transient-failure")
    failed = steps["failed once"]
    assert failed["sel"] == KN
    assert "load failed, retry" in failed["chip"]
    assert failed["chipButton"]
    assert "no elements" not in failed["chipTitle"]
    assert "request failed" in failed["chipTitle"]
    assert steps["t+6"]["sel"] == XIV
    assert steps["t+6"]["selects"] == [f"{XIV}:failed", f"{XIV}:landed"]


@needs_node
def test_retries_back_off_and_a_click_or_a_reconnect_tries_at_once():
    """5 s, 30 s, then every two minutes: a backend that keeps failing gets
    one request every two minutes, not one a second."""
    steps = run("repeated-failure")
    tries = {name: len(steps[name]["selects"])
             for name in ("failed", "t+6", "t+36", "t+66", "chip clicked",
                          "reconnected", "healthy, chip clicked")}
    assert tries == {"failed": 1, "t+6": 2, "t+36": 3, "t+66": 3,
                     "chip clicked": 4, "reconnected": 5,
                     "healthy, chip clicked": 6}
    assert "3 times running" in steps["t+66"]["chipTitle"]
    assert steps["healthy, chip clicked"]["sel"] == XIV


@needs_node
def test_a_lost_link_says_the_owner_is_unknown_not_nobody():
    """AO-5: the socket closed and the line kept the last frame's NOBODY —
    the stale all-clear the backend's own rule 1 refuses to give."""
    steps = run("link-lost")
    assert steps["live"]["owner"] == "NOBODY"
    lost = steps["link lost"]
    assert lost["owner"] == "UNKNOWN"
    assert "own-unknown" in lost["ownerClass"]
    assert "link to the backend" in lost["ownerTitle"]
    assert "last heard: NOBODY" in lost["ownerTitle"]
    assert lost["activity"] == "link to the backend lost — owner unknown"
    # Reconnected is not yet re-read: the snapshot carries the next frame.
    assert steps["reconnected, no frame yet"]["owner"] == "UNKNOWN"
    assert steps["snapshot"]["owner"] == "NOBODY"
    assert steps["snapshot"]["activity"] == "idle"


def _body(source: str, name: str) -> str:
    start = source.index(f"async function {name}(")
    end = source.index("\n}\n", start)
    return source[start:end]


def test_select_satellite_asks_wanted_before_it_writes_anything():
    """The other half of AO-3, which the harness models and this pins in the
    real main.js: the follow's `wanted` is asked once the elements are here
    and before the store is touched."""
    body = _body(MAIN_JS.read_text(encoding="utf-8").replace("\r\n", "\n"), "selectSatellite")
    assert re.search(r"async function selectSatellite\(norad, \{ wanted \} = \{\}\)", body)
    fetched = body.index("await api.tle(norad)")
    asked = body.index("if (wanted && !wanted()) return;")
    written = body.index("set('tle', tle)")
    assert fetched < asked < written


def test_refresh_pass_drops_a_pass_for_a_satellite_no_longer_shown():
    """A pass that loads after the selection moved on is the old satellite's;
    set anyway, it put one satellite's next pass under another's name."""
    body = _body(MAIN_JS.read_text(encoding="utf-8").replace("\r\n", "\n"), "refreshPass")
    guard = "if (store.satellite?.norad !== norad) return;"
    assert body.index("await api.nextPass(norad)") < body.index(guard) < body.index("set('nextPass', next)")
    assert body.count(guard) == 2
