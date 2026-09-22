"""Does the scheduler we ship actually exist and run?

Every other test in this suite exercises our own code around
`satnogs-auto-scheduler` - the command line we build, the environment we hand
it, the transcript we read back. None of them prove the thing is installed.
It arrives from a git URL pinned to one commit, built by a `hatch-vcs` backend
that derives its version from tags a shallow clone might not fetch, in a
Dockerfile layer that installs `git`, uses it and purges it again. That is
several ways for the image to come out subtly wrong while the offline suite
stays perfectly green.

So this module is the one that spawns the real binary. It is marked `network`
and excluded by default (`pytest.ini` sets `-m "not network"`); run it with
`-m network` inside the built image, or on a box where the package is
installed.

The second test needs both SatNOGS tokens and reaches the live API. It SKIPS
rather than fails when they are absent, in the same style as
`test_satnogs_oracle.py`: a developer without tokens should not see a red
suite, but a wrong answer with tokens present must not be hidden.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from app.services.autoscheduler_cli import (
    RunConfig,
    build_argv,
    build_env,
    classify_failure,
    parse_output,
)

pytestmark = pytest.mark.network

# The commit backend/requirements.txt pins. If the resolver ever silently takes
# a different one, this is where it shows up.
EXPECTED_VERSION = "0.5.dev17+g0f7ec0177"


def _installed() -> bool:
    try:
        import auto_scheduler  # noqa: F401
    except ImportError:
        return False
    return True


requires_install = pytest.mark.skipif(
    not _installed(),
    reason="satnogs-auto-scheduler is not installed here; run inside the backend image",
)


@requires_install
def test_the_pinned_scheduler_is_installed_and_runnable():
    """Spawn it exactly the way a real run does, but only ask its version."""
    cfg = RunConfig(station_id=5024, launcher="module")
    # build_argv's launcher prefix is the part under test - the flags are not.
    argv = build_argv(cfg)[:3] + ["--version"]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)

    assert proc.returncode == 0, (
        "the scheduler we ship could not even report its version, so no run "
        f"will ever succeed in this image: {proc.stderr[-400:]}"
    )
    # --version is one of the few things this tool writes to stdout.
    reported = (proc.stdout + proc.stderr).strip()
    assert EXPECTED_VERSION in reported, (
        f"expected the pinned {EXPECTED_VERSION} but got {reported!r}. A "
        "different build means the behaviours this dashboard relies on - the "
        "-f double inversion, the summary table's columns - may not hold."
    )


@requires_install
def test_the_console_script_entry_point_also_resolves():
    """The `script` launcher fallback has to be real, or GS_SCHEDULE_LAUNCHER
    is a setting that breaks the feature when anyone uses it."""
    import shutil

    path = shutil.which("schedule_single_station")
    assert path, (
        "GS_SCHEDULE_LAUNCHER=script falls back to this console script; if it "
        "is not on PATH that setting silently cannot work"
    )
    proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-400:]


@requires_install
def test_a_short_dry_run_against_live_satnogs():
    """The whole path: our argv, our environment, their tool, our parser.

    Deliberately `-n` and a half-hour window. Nothing here can book: `-n`
    skips the scheduling call entirely.
    """
    db_token = os.environ.get("GS_SATNOGS_DB_TOKEN", "")
    net_token = os.environ.get("GS_SATNOGS_NETWORK_TOKEN", "")
    if not (db_token and net_token):
        pytest.skip(
            "needs GS_SATNOGS_DB_TOKEN and GS_SATNOGS_NETWORK_TOKEN; the tool "
            "validates both before it looks at --dryrun, so a dry run cannot "
            "be done without them"
        )

    cfg = RunConfig(
        station_id=int(os.environ.get("GS_STATION_ID", "5024")),
        db_token=db_token,
        network_token=net_token,
        cache_dir=os.environ.get("GS_CACHE_DIR", "/tmp/satnogs-cache-test"),
        dry_run=True,
        hours=0.5,
        now=datetime.now(timezone.utc),
    )
    proc = subprocess.run(
        build_argv(cfg),
        env=build_env(cfg),
        capture_output=True,
        text=True,
        # A cold cache genuinely takes minutes: the tool refetches every
        # transmitter's statistics and says so itself.
        timeout=1800,
    )
    lines = (proc.stdout + proc.stderr).splitlines()
    failure = classify_failure(lines, proc.returncode)
    assert failure is None, (
        f"a dry run against live SatNOGS failed ({failure[0]}): {failure[1]}\n"
        + "\n".join(lines[-30:])
    )

    parsed = parse_output(lines)
    # A half-hour window over one station may legitimately select nothing, so
    # the assertion is about the transcript being INTELLIGIBLE, not full.
    assert parsed.efficiency is not None or parsed.no_passes, (
        "the run produced neither an efficiency report nor a 'no appropriate "
        "passes' line, which means our parser no longer recognises this "
        "tool's output - the most likely cause is an upstream version change:\n"
        + "\n".join(lines[-30:])
    )
    for row in parsed.planned:
        assert row.start.tzinfo is not None, "parsed times must be timezone-aware"
        assert row.duration_s > 0, f"a selected pass with no duration: {row}"
