"""Run the Libre Space Foundation's `satnogs-auto-scheduler` as a child process.

The Station Schedule tab no longer plans station 5024's own observations with
the vendored clean-room package. It runs the official tool — the same
`schedule_single_station` that has been booking this station's passes from a
PowerShell script — and reports what that tool actually did.

Why a separate process rather than an import:

  * **Licence.** `satnogs-auto-scheduler` is AGPL-3.0 and this repo is MIT.
    Invoking it as its own process keeps the two an aggregation rather than a
    derived work, which is the same posture the README already takes toward
    the GPL-3.0 ground-station container. It is tempting to import
    `satnogs_client.schedule_observations_batch()` to get an exact
    preview-then-book flow; that is precisely the import this boundary exists
    to avoid.
  * **It is a CLI, not a library.** `main()` calls `sys.exit()` on every error
    path and reconfigures the root logger. Neither belongs inside a web server.

Everything except `run()` is a pure function, so the parts that are easy to get
quietly wrong — which flags we pass, what environment the child sees, how its
output is read back — are unit-testable without spawning anything.

Four behaviours of that tool drive the design here, and all four look like bugs
until you read its source:

  1. `-f` is declared `action="store_false"` against `set_defaults(True)`, so it
     reads backwards. It does what its help says: restrict scheduling to the
     priority file. Pass it only when that is what you want. See `build_argv`.
  2. The summary table goes to **stderr**, not stdout: `main()` calls
     `logging.basicConfig()` with no `stream=`, and the table is printed with
     `printer=logging.info`. We merge stderr into stdout so the transcript is
     in order.
  3. A **dry run still needs both tokens** — `validate_config()` runs before the
     tool ever looks at `--dryrun`. `validate_tokens` exists so that becomes a
     sentence the operator can act on instead of an opaque child `exit(1)`.
  4. `logging.basicConfig` is a no-op once the root logger has handlers, and
     the level is set inside that same guard. Pre-seeding lets us keep severity
     visible — but only if the pre-seed sets a level itself. See `_BOOTSTRAP`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# The tool parses -t with `strptime(...).replace(tzinfo=utc)`, so the string we
# hand it is read as UTC no matter what the container's clock is set to.
START_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"

# Anchor for the summary table. Matched after the logging prefix is stripped and
# the line is stripped of leading space, because the real header begins with two
# spaces that we do not want to depend on.
TABLE_HEADER = "GS | Sch | NORAD | Start time"

# A data row is 12 " | "-separated fields. We match structurally rather than by
# column offset, because satellite names are unbounded and the duration column
# is right-justified into a width the tool is free to change.
ROW_FIELDS = 12

# The whole transcript of a cold run is thousands of lines of progress bar. Keep
# a bounded tail in memory; the full text goes to the log file on disk.
MAX_TRANSCRIPT_LINES = 4000

_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")

# Exactly upstream's own rule (settings.py), so a token we accept is a token the
# child will accept, and a token we reject never costs a process spawn.
_TOKEN_RE = re.compile(r"^[a-f0-9]{40}$")

_EFFICIENCY_RE = re.compile(
    r"(\d+)\s+passes selected out of\s+(\d+),\s+"
    r"(\d+)\s+s out of\s+(\d+)\s+s\s+at\s+([\d.]+)%\s+efficiency"
)
_SCHEDULED_RE = re.compile(r"Scheduled\s+(\d+)\s+passes!")

# Environment the child inherits from us. Deliberately a fixed list rather than
# "everything but the secrets": nothing named GS_* is ever forwarded, so the
# dashboard's own configuration cannot leak into the tool's decouple lookups and
# change its behaviour by accident.
_PASSTHROUGH = (
    "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
)

# Run the tool through this interpreter instead of the console script. Three
# reasons: it needs no PATH, it is guaranteed to be the interpreter we were
# installed alongside, and — because `logging.basicConfig` does nothing once the
# root logger has handlers — it lets us install our own format FIRST so every
# line carries its severity. Without that, `format="%(message)s"` makes a
# warning indistinguishable from a progress message.
#
# `level=logging.INFO` is load-bearing and not decoration. `basicConfig` sets
# the root level inside the same `if not root.handlers` guard, so pre-seeding
# without a level leaves the root logger at its default WARNING and the tool's
# entire summary table — every line of it is `logging.info` — is silently
# dropped. The run then exits 0 having printed almost nothing.
#
# Raising just `auto_scheduler.satnogs_client` to DEBUG makes its
# "Scheduled N passes!" confirmation visible (it logs that at DEBUG) without
# turning on the pass-predictor's per-sample debug flood. A record from that
# logger passes its own level check and is then emitted by the root handler,
# whose level is NOTSET — the root logger's INFO does not filter it.
_BOOTSTRAP = (
    "import logging, sys; "
    "logging.basicConfig("
    "level=logging.INFO, "
    "format='%(levelname)s\\t%(name)s\\t%(message)s', "
    "stream=sys.stderr); "
    "logging.getLogger('auto_scheduler.satnogs_client').setLevel(logging.DEBUG); "
    "from auto_scheduler.cli.schedule_single_station import main; "
    "main()"
)


@dataclass(frozen=True)
class RunConfig:
    """Everything one invocation needs. Frozen so a run cannot mutate it midway."""

    station_id: int
    db_token: str = ""
    network_token: str = ""
    cache_dir: str = ""
    priorities_path: str = ""
    dry_run: bool = True

    # Defaults mirror the station's proven run_scheduler.ps1, not the
    # dashboard's older 48h planning window.
    hours: float = 24.0
    min_culmination_deg: float = 3.0
    max_observation_minutes: int = 30
    only_priority: bool = True
    start_lead_minutes: int = 10

    cache_age_h: float = 24.0
    min_pass_duration_min: float = 3.0
    network_base_url: str = ""
    db_base_url: str = ""

    launcher: str = "module"
    timeout_s: int = 1800
    idle_timeout_s: int = 900

    # Injected so build_argv is a pure function of its input and a test can
    # assert on an exact -t. None means "read the clock now".
    now: datetime | None = None


@dataclass
class ScheduleRow:
    """One line of the tool's summary table."""

    norad: int
    start: datetime
    end: datetime
    duration_s: int
    az_rise: float
    elevation: float
    az_set: float
    priority: float
    transmitter_uuid: str
    mode: str
    frequency_violator: bool
    name: str
    already_scheduled: bool


@dataclass
class ParsedRun:
    """What the transcript says happened."""

    planned: list[ScheduleRow] = field(default_factory=list)
    already_scheduled: list[ScheduleRow] = field(default_factory=list)
    efficiency: dict | None = None
    notices: list[dict] = field(default_factory=list)
    # From "Scheduled N passes!", which the tool logs at DEBUG. None means the
    # line never appeared, which is NOT the same as zero.
    booked_log: int | None = None
    # True once "Scheduling all unscheduled passes listed above." is seen. After
    # that point a booking POST may have reached SatNOGS even if we killed the
    # child, so the result can never be reported as a clean failure.
    attempted_booking: bool = False
    no_passes: bool = False


@dataclass
class RunOutcome:
    exit_code: int | None = None
    killed_by: str = ""
    duration_s: float = 0.0
    parsed: ParsedRun = field(default_factory=ParsedRun)
    failure: tuple[str, str] | None = None
    lines: list[str] = field(default_factory=list)


def _strip_api_suffix(url: str) -> str:
    """Drop a trailing `/api` from a dashboard base URL.

    The dashboard stores `https://network.satnogs.org/api` because its own
    clients append bare paths. The tool stores `https://network.satnogs.org`
    and appends `/api/...` itself, so forwarding ours unchanged would ask
    SatNOGS for `/api/api/observations/`.
    """
    trimmed = url.strip().rstrip("/")
    if trimmed.endswith("/api"):
        trimmed = trimmed[: -len("/api")]
    return trimmed


def _launch_prefix(cfg: RunConfig) -> list[str]:
    if cfg.launcher == "script":
        # No pre-seeded logging on this path, so lines arrive with no severity
        # prefix. parse_output tolerates both shapes.
        return [shutil.which("schedule_single_station") or "schedule_single_station"]
    return [sys.executable, "-c", _BOOTSTRAP]


def start_time(cfg: RunConfig) -> datetime:
    now = cfg.now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc) + timedelta(minutes=cfg.start_lead_minutes)


def build_argv(cfg: RunConfig) -> list[str]:
    """The exact command line to spawn. Contains no secrets, ever.

    Tokens go through the environment instead: `ps` output and container logs
    are not secret.

    `-t` is always passed explicitly rather than relying on the tool's own
    "now + 10 minutes" default, so a run is reproducible and this function is
    testable against a fixed clock.
    """
    argv = _launch_prefix(cfg)
    argv += [
        "-s", str(int(cfg.station_id)),
        "-t", start_time(cfg).strftime(START_TIME_FORMAT),
        "-d", f"{float(cfg.hours):g}",
        "-o", str(int(cfg.max_observation_minutes)),
        "-m", f"{float(cfg.min_culmination_deg):g}",
    ]
    if cfg.priorities_path:
        argv += ["-P", str(cfg.priorities_path)]
    if cfg.only_priority:
        # Reads backwards on purpose. Upstream declares this flag
        # `action="store_false"` on top of `set_defaults(only_priority=True)`,
        # so PRESENT means only_priority=False in the parsed args — and the
        # branch it gates in utils.get_priority_passes is `elif only_priority:`,
        # the one that adds NON-priority passes. Net: passing -f restricts the
        # run to the priority file, exactly as its help text claims.
        # Do not "fix" this to `if not cfg.only_priority`. That books the
        # station's entire receivable catalogue.
        argv.append("-f")
    if cfg.dry_run:
        argv.append("-n")
    # -l is deliberately never passed: our pre-seeded root logger makes the
    # tool's own basicConfig a no-op, so the flag would be silently ignored.
    return argv


def build_env(cfg: RunConfig, base: dict | None = None) -> dict[str, str]:
    """The child's whole environment, built up rather than filtered down."""
    source = os.environ if base is None else base
    env: dict[str, str] = {
        # Network first, DB second — the names do not say which is which.
        "SATNOGS_API_TOKEN": cfg.network_token or "",
        "SATNOGS_DB_API_TOKEN": cfg.db_token or "",
        # Keeping the cache on the data volume is what stops every run paying
        # the "this will take some minutes" transmitter-statistics refetch.
        "CACHE_DIR": str(cfg.cache_dir or ""),
        "CACHE_AGE": f"{float(cfg.cache_age_h):g}",
        "MIN_PASS_DURATION": f"{float(cfg.min_pass_duration_min):g}",
        "PYTHONUNBUFFERED": "1",
    }
    if cfg.network_base_url:
        env["NETWORK_BASE_URL"] = _strip_api_suffix(cfg.network_base_url)
    if cfg.db_base_url:
        # Without this the tool talks to the public db.satnogs.org whatever the
        # dashboard is configured against.
        env["DB_BASE_URL"] = _strip_api_suffix(cfg.db_base_url)
    for key in _PASSTHROUGH:
        value = source.get(key)
        if value:
            env[key] = str(value)
    return env


def validate_tokens(db_token: str, network_token: str) -> list[str]:
    """Problems that would make the child exit(1) before doing any work.

    Mirrors upstream's rule exactly: both tokens present, both 40 lowercase hex
    characters. Returned as sentences because this is what the UI shows, and
    because "a dry run needs the Network token too" is surprising enough that
    it reads as a bug unless it is spelled out.
    """
    problems: list[str] = []
    for value, label, env_name in (
        (network_token, "SatNOGS Network", "SATNOGS_API_TOKEN"),
        (db_token, "SatNOGS DB", "SATNOGS_DB_API_TOKEN"),
    ):
        token = (value or "").strip()
        if not token:
            problems.append(
                f"The {label} token is not set. satnogs-auto-scheduler validates "
                f"its whole configuration before it looks at the dry-run flag, so "
                f"even a DRY RUN needs it ({env_name})."
            )
            continue
        if _TOKEN_RE.fullmatch(token):
            continue
        if len(token) != 40:
            detail = f"this one is {len(token)} characters"
        elif token.lower() != token:
            detail = "this one has uppercase letters in it"
        else:
            detail = "this one has characters outside 0-9 and a-f"
        problems.append(
            f"The {label} token does not look like a SatNOGS token: it must be "
            f"exactly 40 characters of lowercase hex, and {detail}."
        )
    return problems


def split_prefix(line: str) -> tuple[str, str, str]:
    """Split our pre-seeded `LEVEL\\tlogger\\tmessage` prefix off a line.

    Returns ("", "", line) when the prefix is absent, which is what happens
    under the `script` launcher and for anything the child writes directly to
    its own stdout. Rows and the efficiency line parse identically either way;
    severity does NOT, because upstream's own format carries no level field -
    see `_CONTENT_SEVERITY`, which recovers it from the text instead.
    """
    parts = line.split("\t", 2)
    if len(parts) == 3 and parts[0] in _LEVELS:
        return parts[0], parts[1], parts[2]
    return "", "", line


def _parse_duration(field: str) -> int | None:
    """Seconds from a `str(timedelta)` field like `` 0:08:05``."""
    text = field.strip()
    if not text:
        return None
    days = 0
    if "day" in text:
        head, _, text = text.partition(",")
        try:
            days = int(head.split()[0])
        except (ValueError, IndexError):
            return None
        text = text.strip()
    bits = text.split(":")
    if len(bits) != 3:
        return None
    try:
        hours, minutes, seconds = (int(float(b)) for b in bits)
    except ValueError:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


# When there is no severity prefix to read - the `script` launcher, where
# upstream's own format="%(message)s" carries no level - severity has to come
# from the text. Without this, a script-launcher run reports zero notices
# however many warnings it actually printed.
_CONTENT_SEVERITY: tuple[tuple[str, str], ...] = (
    ("Failed to", "error"),
    ("Download from SatNOGS", "error"),
    ("No ground station information found", "error"),
    ("neither in 'online' nor in 'testing' mode", "error"),
    ("No permission to schedule observations", "error"),
    ("Traceback (most recent call last)", "error"),
    ("Malformed line in priority file", "warning"),
    ("No TLE found for", "warning"),
    ("Azimuth window not in", "warning"),
    ("Extra args in --pointing", "warning"),
    ("Could not read priority file", "warning"),
)


def _content_severity(message: str) -> str | None:
    for needle, severity in _CONTENT_SEVERITY:
        if needle in message:
            return severity
    return None


def _parse_row(message: str) -> ScheduleRow | None:
    """One summary-table row, or None if this is not one.

    Returns None rather than raising on a malformed line so that a transcript
    truncated mid-table — which is exactly what a killed child leaves behind —
    still parses as far as it goes.
    """
    parts = message.split(" | ")
    if len(parts) < ROW_FIELDS:
        return None
    sch = parts[1].strip()
    if sch not in ("Y", "N"):
        return None
    try:
        norad = int(parts[2].strip())
        start = datetime.strptime(parts[3].strip(), START_TIME_FORMAT).replace(
            tzinfo=timezone.utc
        )
        end = datetime.strptime(parts[4].strip(), START_TIME_FORMAT).replace(
            tzinfo=timezone.utc
        )
    except (ValueError, IndexError):
        return None

    # The tool prints az/el as one space-separated group, and zeroes the whole
    # group for rows that were already on the calendar.
    az_rise = elevation = az_set = 0.0
    geometry = parts[6].split()
    if len(geometry) == 3:
        try:
            az_rise, elevation, az_set = (float(v) for v in geometry)
        except ValueError:
            pass
    try:
        priority = float(parts[7].strip())
    except ValueError:
        priority = 0.0

    # The printed Duration column is authoritative, not (end - start).
    # The tool formats both timestamps with strftime, truncating sub-second
    # precision, but formats Duration from the full-precision timedelta - so
    # subtracting the printed endpoints overstates by one second on any pass
    # whose fractions cross a second boundary. Measured against a real
    # transcript that was 31 rows out of 51, and it put our totals at odds
    # with the tool's own efficiency line.
    duration_s = _parse_duration(parts[5])
    if duration_s is None:
        duration_s = int((end - start).total_seconds())

    return ScheduleRow(
        norad=norad,
        start=start,
        end=end,
        duration_s=duration_s,
        az_rise=az_rise,
        elevation=elevation,
        az_set=az_set,
        priority=priority,
        transmitter_uuid=parts[8].strip(),
        mode=parts[9].strip(),
        # Labelled "Freq" in the header, but the tool emits the
        # is-frequency-violator flag here and the sub-header calls it "misuse".
        # There is no frequency column; downlink_hz has to be enriched from DB.
        frequency_violator=parts[10].strip() == "Y",
        name=" | ".join(parts[11:]).strip(),
        already_scheduled=sch == "Y",
    )


def parse_output(lines: list[str]) -> ParsedRun:
    """Read a transcript back into structure.

    Anchors on the literal table header and then skips exactly one sub-header
    line, rather than trusting column offsets.
    """
    out = ParsedRun()
    in_table = False
    skip_subheader = False
    # Index of the notice the previous line produced, so a continuation can be
    # appended to it. Upstream logs at least one WARNING with an embedded
    # newline (io.py's malformed-priority-line message), which arrives as two
    # physical lines - the second carrying no prefix. Without this the half
    # that gets dropped is the half that says WHAT was wrong.
    open_notice: int | None = None

    for raw in lines:
        level, _logger, message = split_prefix(raw.rstrip("\n"))
        stripped = message.strip()

        # A prefix-less line beginning with whitespace, directly after a
        # notice, is the rest of that notice.
        if open_notice is not None and not level and message.startswith(" ") and stripped:
            out.notices[open_notice]["message"] += f" {stripped}"
            continue

        if stripped.startswith(TABLE_HEADER):
            in_table = True
            skip_subheader = True
            open_notice = None
            continue
        if skip_subheader:
            # `f"{' ' * 136} | misuse | "` — one line, always.
            skip_subheader = False
            continue

        if in_table:
            row = _parse_row(message)
            if row is not None:
                if row.already_scheduled:
                    out.already_scheduled.append(row)
                else:
                    out.planned.append(row)
                open_notice = None
                continue
            # Any other non-blank line ends the table - in practice the
            # trailing "Done.". Note the efficiency line comes BEFORE the
            # header, not after it, which is why its regex runs on every line
            # below rather than only once the table has closed. Do not
            # "optimise" it into this branch.
            if stripped:
                in_table = False

        match = _EFFICIENCY_RE.search(stripped)
        if match:
            out.efficiency = {
                "selected": int(match.group(1)),
                "considered": int(match.group(2)),
                "scheduled_s": int(match.group(3)),
                "total_s": int(match.group(4)),
                "percent": float(match.group(5)),
            }
        if "No appropriate passes found for scheduling." in stripped:
            out.no_passes = True
        if "Scheduling all unscheduled passes listed above." in stripped:
            out.attempted_booking = True
        booked = _SCHEDULED_RE.search(stripped)
        if booked:
            out.booked_log = int(booked.group(1))

        severity = None
        if level in ("WARNING", "ERROR", "CRITICAL"):
            severity = "warning" if level == "WARNING" else "error"
        elif not level:
            # No prefix to read - the `script` launcher. Fall back to the text,
            # or a run there reports zero notices however many warnings it
            # actually printed.
            severity = _content_severity(stripped)
        if severity and stripped:
            out.notices.append({"severity": severity, "message": stripped})
            open_notice = len(out.notices) - 1
        else:
            open_notice = None

    return out


# Ordered most specific first: several of these co-occur, and the earliest
# match is the one that explains the run. Exit codes cannot do this job — every
# failure path in the tool is a bare sys.exit(1).
_FAILURES: tuple[tuple[str, str, str], ...] = (
    (
        "token_missing",
        "No value for SATNOGS_",
        "satnogs-auto-scheduler refused to start: one of its two API tokens was "
        "empty. It checks both before it looks at the dry-run flag.",
    ),
    (
        "token_invalid",
        "Invalid value for SATNOGS_",
        "satnogs-auto-scheduler rejected an API token's format. It must be "
        "exactly 40 characters of lowercase hex.",
    ),
    (
        "station_unknown",
        "No ground station information found!",
        "SatNOGS Network returned no information for this station id. Check the "
        "station id, and that the Network token belongs to an account that can "
        "see it.",
    ),
    (
        "station_offline",
        "neither in 'online' nor in 'testing' mode",
        "The station is neither online nor in testing mode, so SatNOGS will not "
        "accept bookings for it. A DRY RUN skips this check, which is why one "
        "can succeed where a real run refuses.",
    ),
    (
        "no_permission",
        "No permission to schedule observations",
        "SatNOGS refused the booking: this Network token's account is not "
        "permitted to schedule on that station.",
    ),
    (
        "network_download",
        "Download from SatNOGS Network failed.",
        "Could not read the station's existing schedule from SatNOGS Network, so "
        "the run stopped rather than risk double-booking.",
    ),
    (
        "batch_failed",
        "Failed to batch-schedule observations.",
        "SatNOGS rejected the booking request. Nothing from this run was "
        "scheduled.",
    ),
    (
        "pass_failed",
        "Failed to schedule pass at",
        "SatNOGS rejected at least one individual pass. The run may be partly "
        "booked.",
    ),
    (
        "crashed",
        "Traceback (most recent call last)",
        "satnogs-auto-scheduler crashed. The raw log below has the traceback.",
    ),
)


def classify_failure(lines: list[str], exit_code: int | None) -> tuple[str, str] | None:
    """Turn a transcript into a cause, or None if the run looks clean.

    Exit codes are not diagnostic here, so this reads the text. It still
    reports an unexplained non-zero exit rather than staying silent about it.
    """
    haystack = "\n".join(split_prefix(line.rstrip("\n"))[2] for line in lines)
    for code, needle, explanation in _FAILURES:
        if needle in haystack:
            return code, explanation

    # Defensive: forgetting -s makes argparse print the full help to stdout and
    # exit 0, which would otherwise be indistinguishable from a clean run that
    # simply found nothing. build_argv always passes -s, so this should be
    # unreachable.
    if "usage: schedule_single_station" in haystack:
        return (
            "bad_invocation",
            "satnogs-auto-scheduler printed its usage text instead of running, "
            "which means it was called without a station id.",
        )
    if exit_code not in (0, None):
        return (
            "exit_nonzero",
            f"satnogs-auto-scheduler exited with status {exit_code} without "
            f"saying why. The raw log below is the whole transcript.",
        )
    return None


def missing_priority_notices(
    priority_norads: list[int], parsed: ParsedRun
) -> list[dict]:
    """Warn about priority entries that produced no pass.

    Upstream is completely silent about this, and under `-f` it is the exact
    symptom of a transmitter UUID that the tool's candidate set does not carry:
    the satellite is simply never scheduled and nothing says so.
    """
    seen = {row.norad for row in parsed.planned} | {
        row.norad for row in parsed.already_scheduled
    }
    return [
        {
            "severity": "warning",
            "message": (
                f"NORAD {norad} is in the priority list but no pass was selected "
                f"for it. Under 'only priority' that usually means its pinned "
                f"transmitter is not in this station's candidate set."
            ),
        }
        for norad in priority_norads
        if norad not in seen
    ]


async def run(
    cfg: RunConfig,
    on_line=None,
    log_path: Path | None = None,
) -> RunOutcome:
    """Spawn the tool and stream its output back.

    stderr is merged into stdout deliberately: the summary table only exists on
    stderr, and merging keeps the transcript in true emission order.
    """
    argv = build_argv(cfg)
    env = build_env(cfg)

    # Upstream's CacheManager uses a single-level os.mkdir with no exist_ok, so
    # it raises FileNotFoundError on a missing parent and can lose a race with
    # itself. Create the whole tree ourselves before it looks.
    if cfg.cache_dir:
        Path(cfg.cache_dir).mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    started = loop.time()
    outcome = RunOutcome()
    transcript: deque[str] = deque(maxlen=MAX_TRANSCRIPT_LINES)
    sink = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        sink = log_path.open("w", encoding="utf-8", errors="replace")

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            # Its own session, so a kill reaches anything it spawned rather
            # than travelling up into the web server.
            start_new_session=True,
        )
    except OSError as exc:
        if sink is not None:
            sink.close()
        outcome.exit_code = None
        outcome.failure = (
            "spawn_failed",
            f"Could not start satnogs-auto-scheduler: {exc}",
        )
        return outcome

    deadline = started + cfg.timeout_s
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                outcome.killed_by = "timeout"
                break
            try:
                raw = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=min(cfg.idle_timeout_s, remaining)
                )
            except asyncio.TimeoutError:
                outcome.killed_by = "timeout" if loop.time() >= deadline else "idle"
                break
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            transcript.append(line)
            if sink is not None:
                sink.write(line + "\n")
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:  # noqa: BLE001 - a bad callback must not kill a run
                    log.exception("schedule: on_line callback failed")

        if outcome.killed_by:
            await _terminate(proc)
        outcome.exit_code = await proc.wait()
    finally:
        if sink is not None:
            sink.close()

    outcome.duration_s = loop.time() - started
    outcome.lines = list(transcript)
    outcome.parsed = parse_output(outcome.lines)
    outcome.failure = classify_failure(outcome.lines, outcome.exit_code)
    if outcome.killed_by and outcome.failure is None:
        outcome.failure = (
            f"killed_{outcome.killed_by}",
            (
                "satnogs-auto-scheduler produced no output for "
                f"{cfg.idle_timeout_s}s and was stopped."
                if outcome.killed_by == "idle"
                else f"satnogs-auto-scheduler ran past {cfg.timeout_s}s and was stopped."
            ),
        )
    return outcome


async def _terminate(proc) -> None:
    """Ask, then insist. Ten seconds is generous for a tool with no cleanup."""
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
