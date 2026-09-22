"""Capture a real satnogs-auto-scheduler transcript against a stub SatNOGS API.

Runs inside `ground-station-dashboard-backend:latest`, which is where the
official tool is installed. See ../README.md.

The point of this file is that it invents nothing about how the tool is
invoked. It imports `_BOOTSTRAP`, `RunConfig`, `build_argv` and `build_env`
from the dashboard's own `app/services/autoscheduler_cli.py` and uses them
unmodified, so the transcript is produced by the exact command line and exact
environment that production uses. If someone changes those functions, re-running
this harness changes the transcript too, which is the property we want.

What differs from production, and only this:

  * NETWORK_BASE_URL / DB_BASE_URL point at two loopback stub servers.
  * The tokens are 40 hex characters of nonsense. Upstream's `validate_token`
    is a local regex with no network call, so nonsense that matches passes.
  * `RunConfig.now` is frozen, so the run is byte-reproducible. Upstream's own
    field comment says that is what it is for.

`-n` (dry run) is passed, so nothing is booked; the stub answers any POST with
403 and this harness fails the capture if one is ever attempted.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stub_api  # noqa: E402

REPO_CLI = "/repo/backend/app/services/autoscheduler_cli.py"
FIXTURES = "/w/upstream_fixtures"
PRIORITIES = "/w/priorities.txt"
OUT_DIR = "/out"
CACHE_DIR = "/tmp/gs-cache"

DB_PORT = 8099
NETWORK_PORT = 8098

# 40 lowercase hex characters, which is all upstream's settings.validate_token
# checks. These are not credentials and unlock nothing.
FAKE_TOKEN_NETWORK = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
FAKE_TOKEN_DB = "cafef00dcafef00dcafef00dcafef00dcafef00d"

# Frozen so the capture is reproducible. build_argv turns this into
# -t 2023-02-18T09:00:00 via its own +10 minute lead.
#
# Anchored near the epoch of upstream's TLE fixture (2023-02-17) on purpose.
# Those TLEs propagate to any date without erroring, but propagated three and a
# half years they produce physically nonsense geometry - 160 passes a day for a
# single satellite. Anchoring at the epoch keeps the az/el and duration columns
# in the transcript realistic, which is the whole point of capturing one.
FROZEN_NOW = dt.datetime(2023, 2, 18, 8, 50, 0, tzinfo=dt.timezone.utc)


def load_repo_cli():
    spec = importlib.util.spec_from_file_location("autoscheduler_cli", REPO_CLI)
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves annotations through sys.modules[cls.__module__], so
    # the module has to be registered before it is executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    cli = load_repo_cli()

    os.makedirs(OUT_DIR, exist_ok=True)
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    request_log_path = os.path.join(OUT_DIR, "stub_requests.log")
    transcript_path = os.path.join(OUT_DIR, "schedule_dry_run.log")

    with open(request_log_path, "w") as request_log:
        db_payloads, network_payloads = stub_api.build_payloads(FIXTURES)
        db = stub_api.make_server("DB", DB_PORT, db_payloads, request_log)
        network = stub_api.make_server("NETWORK", NETWORK_PORT, network_payloads, request_log)

        cfg = cli.RunConfig(
            station_id=stub_api.STATION_ID,
            db_token=FAKE_TOKEN_DB,
            network_token=FAKE_TOKEN_NETWORK,
            cache_dir=CACHE_DIR,
            priorities_path=PRIORITIES,
            dry_run=True,
            network_base_url=f"http://127.0.0.1:{NETWORK_PORT}",
            db_base_url=f"http://127.0.0.1:{DB_PORT}",
            now=FROZEN_NOW,
        )

        argv = cli.build_argv(cfg)
        env = cli.build_env(cfg)

        # Printed to the harness's own stdout, never into the transcript file.
        redacted = [
            a if len(a) < 200 else f"<{len(a)} char bootstrap>" for a in argv
        ]
        print("argv:", redacted, file=sys.stderr)
        print(
            "env:",
            {k: (v if "TOKEN" not in k else "<redacted>") for k, v in sorted(env.items())},
            file=sys.stderr,
        )

        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            errors="replace",
        )

        db.shutdown()
        network.shutdown()

    with open(transcript_path, "w") as handle:
        handle.write(proc.stdout)

    print(proc.stdout, end="")
    print(f"\n--- exit code: {proc.returncode} ---", file=sys.stderr)

    with open(request_log_path) as handle:
        requests_seen = handle.read()
    print("--- stub request log ---", file=sys.stderr)
    print(requests_seen, end="", file=sys.stderr)

    if "BOOKING ATTEMPT" in requests_seen:
        print("FATAL: the dry run tried to book. Capture rejected.", file=sys.stderr)
        return 2
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
