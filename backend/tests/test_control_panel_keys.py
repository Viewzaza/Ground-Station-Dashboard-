"""What a key can do to the rotator control panel's buttons.

The panel is updated in place now, so it no longer drops focus on every
`control` frame — which it used to do only because it rebuilt itself. A
button clicked with the mouse keeps focus, with no ring to show it, and the
browser sends Enter and Space to a focused button as a click: on every
auto-repeat for Enter. So a RELEASE clicked an hour ago became a stray Enter
that armed, the same key pressed again released — stopping the antenna and
disengaging autopilot — and a held Enter on EXTEND extended once per repeat.
The unsafe thing these tests rule out is a key reaching a control nobody
chose with the keyboard.

What stays allowed is the keyboard user's own path: Tab to a button, which
rings it, and Enter or Space presses it — once per press, however long held.
Enter or Space reaching a control button any other way is swallowed, and
takes the focus with it.

The panel runs in Node on tests/js/fake_page.mjs, which does what Chromium
does natively with a click or a key; nothing here touches a browser, a
server or a rotator. Skipped where Node is not installed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "control_panel.mjs"
PANELS_CSS = Path(__file__).parents[2] / "frontend" / "css" / "panels.css"
NODE = shutil.which("node")

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
def test_a_key_after_a_mouse_click_does_not_arm_or_release():
    """The finding as it happened: RELEASE clicked, operator gone, and the
    next person to touch Enter or Space takes a fifteen-minute lease."""
    steps = run("pointer-arm-then-keys")
    assert steps["click ARM"]["posts"] == ["/api/control/arm"]
    assert steps["Enter"]["posts"] == []
    # And the swallowed key takes the focus with it: Chromium rings a
    # clicked button once a key is pressed on it, and a ring that a second
    # Enter would not honour says one thing and does another.
    assert steps["Enter"]["focus"] == "BODY"
    assert steps["Space"]["posts"] == []
    assert steps["click RELEASE"]["posts"] == ["/api/control/release"]
    assert steps["Enter after RELEASE"]["posts"] == []
    assert not steps["Enter after RELEASE"]["armed"]


@needs_node
def test_a_held_enter_after_a_mouse_click_sends_nothing():
    """EXTEND must only happen on a press; GO must not be re-sent by a key
    once the gates open again."""
    steps = run("pointer-extend-then-held-enter")
    assert steps["click EXTEND"]["posts"] == ["/api/control/extend"]
    assert steps["Enter held"]["posts"] == []
    assert steps["click GO"]["posts"] == ["/api/control/goto"]
    assert steps["Enter after GO"]["posts"] == []


@needs_node
def test_a_press_that_never_became_a_click_does_not_arm_a_key():
    """Pressed on RELEASE and dragged off — a decision not to — still leaves
    the button focused. Dropping focus on click alone would miss this."""
    steps = run("press-and-drag-away")
    assert steps["press RELEASE, drag off"]["posts"] == []
    assert steps["press RELEASE, drag off"]["focus"] == "ctl-arm"
    assert steps["Enter"]["posts"] == []
    assert steps["Enter"]["focus"] == "BODY"
    assert steps["Space"]["posts"] == []
    assert steps["Space"]["armed"]


@needs_node
def test_tab_then_enter_still_presses_the_button_once_per_press():
    """The keyboard user's path keeps working, and a held key is one press —
    the rule keys.js gives every binding."""
    steps = run("tab-then-keys")
    assert steps["Tab to EXTEND"]["focus"] == "ctl-extend"
    assert steps["Enter"]["posts"] == ["/api/control/extend"]
    assert steps["Enter held"]["posts"] == ["/api/control/extend"]
    assert steps["Space held"]["posts"] == ["/api/control/extend"]
    # A mouse click in between does not cost the keyboard its button: Tab
    # back to it and it answers again.
    assert steps["click EXTEND, then Shift+Tab and Tab back"]["posts"] == ["/api/control/extend"]
    assert steps["Enter after Tab back"]["posts"] == ["/api/control/extend"]
    # But a click on the very button Tab reached ends what Tab granted: it
    # keeps the focus and loses the ring, like any other clicked button.
    assert steps["click EXTEND while Tabbed to it"]["posts"] == ["/api/control/extend"]
    assert steps["Enter after that click"]["posts"] == []


@needs_node
def test_focus_handed_back_by_the_key_list_is_not_a_tab():
    """Closing the key list with Esc puts focus back on the button that had
    it. That is the browser's doing, not the operator choosing the button
    again, so it answers no key until it is Tabbed to."""
    steps = run("help-list-hands-focus-back")
    assert steps["Tab to EXTEND"]["focus"] == "ctl-extend"
    assert steps["?"]["focus"] != "ctl-extend"
    assert steps["Esc"]["focus"] == "ctl-extend"
    assert steps["Enter"]["posts"] == []
    assert steps["Enter"]["focus"] == "BODY"


@needs_node
def test_filling_from_the_plot_does_not_change_the_panel_height():
    """#polar is the flex:1 sibling of the controls. A notice that appeared
    by un-hiding took its line from the plot under the pointer — squashed
    until the next 1 Hz redraw, then smaller and higher — so a second click
    to refine meant a different bearing. The notice keeps its line, said or
    not; only its text changes."""
    steps = run("fill-from-plot")
    assert steps["click the plot"]["posts"] == []
    notice = steps["notice"]
    assert notice["az"] == "-90" and notice["el"] == "45"
    assert notice["after"]["text"] == "filled from plot — press GO to move"
    assert notice["before"]["hidden"] is False
    assert notice["after"]["hidden"] is False


def test_the_notice_line_is_reserved_in_the_stylesheet():
    """The other half of the above: an empty notice still takes its line."""
    css = PANELS_CSS.read_text(encoding="utf-8")
    rules = re.findall(r"\.ctl-notice\s*\{([^}]*)\}", css)
    assert any("min-height" in body for body in rules), rules
