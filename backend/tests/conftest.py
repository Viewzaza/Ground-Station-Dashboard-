"""Two guards every offline test runs under.

The Station Schedule has no mock mode: every run spawns the real
satnogs-auto-scheduler and books real observations. So nothing in the offline
suite may be able to reach that tool by accident.

* The startup version probe is stubbed. It is a real subprocess otherwise,
  run once per ScheduleService - dozens of times across this suite. Tests
  that are about the probe call the real one explicitly.
* Spawning the scheduler raises. A test that fakes `autoscheduler_cli.run`
  never gets here; one that forgot to - or patched the wrong name - fails
  loudly instead of talking to live SatNOGS with whatever tokens it set up.

Tests marked `network` are exempt: spawning the real tool is their point.
"""

from __future__ import annotations

import pytest

from app.services import autoscheduler_cli
from app.services.schedule_service import ScheduleService

INSTALLED_VERSION = "satnogs-auto-scheduler 0.5.dev17+g0f7ec0177"


@pytest.fixture(autouse=True)
def _never_spawn_the_scheduler(request, monkeypatch):
    if request.node.get_closest_marker("network"):
        return
    monkeypatch.setattr(
        ScheduleService, "_probe_cli_version", lambda self: INSTALLED_VERSION
    )

    async def _refuse(*args, **kwargs):
        raise AssertionError(
            "a test tried to spawn satnogs-auto-scheduler - every run books real "
            "observations, so fake autoscheduler_cli.run instead"
        )

    monkeypatch.setattr(autoscheduler_cli.asyncio, "create_subprocess_exec", _refuse)
