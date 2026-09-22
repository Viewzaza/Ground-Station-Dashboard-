"""Pin the command line and the environment we hand satnogs-auto-scheduler.

`run()` spawns a child process, so everything that decides what that child
actually does is settled before the spawn — in `build_argv`, `build_env` and
`validate_tokens`. Those three functions are pure, which means every way of
getting this wrong is cheap to catch here and expensive to catch on the
station. Each test below names a specific way a run goes wrong in the field.

What is actually at stake, test by test:

* **`-f` reads backwards, twice.** Upstream declares it `action="store_false"`
  on top of `set_defaults(only_priority=True)`, and the branch it gates adds
  the NON-priority passes. Getting it wrong in either direction is a real
  incident: one way station 5024 books nothing all night, the other way it
  books every satellite it can hear and fills the calendar. The comment on that
  test is the deliverable; the assertions only hold it in place.

* **Secrets on the command line.** `ps`, container logs, and an `ExecStart=`
  line in a unit file are all readable by anyone who can see the host. Tokens
  belong in the environment. The test joins the *entire* argv, bootstrap
  snippet included, because the bootstrap is a `-c` string and a future edit
  could easily interpolate something into it.

* **`-t` and time zones.** The tool parses `-t` with
  `strptime(...).replace(tzinfo=utc)` — it does not read an offset, it asserts
  one. Hand it a local-time string and the whole planning window silently
  shifts by the host's offset, so the station plans for hours that have already
  passed or have not arrived. The non-UTC case here is the one that matters:
  `cfg.now` is injectable precisely so this is assertable.

* **Environment leakage, both directions.** Nothing named `GS_*` may reach the
  child: upstream reads its configuration through decouple, and a stray
  dashboard variable would change the tool's behaviour with nothing in the log
  to say why. In the other direction, `DB_BASE_URL` missing means DB traffic
  goes to the public db.satnogs.org no matter what this dashboard is pointed
  at, and a base URL that keeps its `/api` suffix produces requests for
  `/api/api/...` that simply 404.

* **Token names that do not say what they hold.** `SATNOGS_API_TOKEN` is the
  *Network* token and `SATNOGS_DB_API_TOKEN` is the *DB* one. Swap them and the
  tool starts, validates both, and then fails somewhere deep in a download with
  a message about neither.

* **"A dry run needs the token too."** Upstream's `validate_config()` runs
  before it ever looks at `--dryrun`. An operator who has only set up a dry run
  will read a bare `exit(1)` as a bug in this dashboard. The sentence
  `validate_tokens` produces is the only thing standing between them and a bug
  report, so its wording is pinned here.

Nothing here touches the network, spawns a process, or reads the real clock:
every config carries an injected `cfg.now`. No `Settings` is constructed
because this module needs none — it takes a `RunConfig` and nothing else.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.autoscheduler_cli import (
    START_TIME_FORMAT,
    RunConfig,
    build_argv,
    build_env,
    start_time,
    validate_tokens,
)

STATION_ID = 5024

# Valid-looking tokens: 40 characters of lowercase hex, and distinguishable
# from each other so a swap is visible rather than a coin flip.
NETWORK_TOKEN = "a" * 40
DB_TOKEN = "b" * 40

# 2026-03-01T12:00:00Z. Chosen so the +05:30 case below lands on a different
# wall-clock hour AND a different date, which a naive implementation cannot
# fake its way past.
PINNED_UTC = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
KOLKATA = timezone(timedelta(hours=5, minutes=30))
HONOLULU = timezone(timedelta(hours=-10))


def cfg(**overrides) -> RunConfig:
    """A RunConfig with both tokens set and the clock pinned."""
    base = dict(
        station_id=STATION_ID,
        network_token=NETWORK_TOKEN,
        db_token=DB_TOKEN,
        cache_dir="/data/satnogs-cache",
        now=PINNED_UTC,
    )
    base.update(overrides)
    return RunConfig(**base)


def flag_value(argv: list[str], flag: str) -> str | None:
    """The token following `flag` in argv, or None when the flag is absent."""
    return argv[argv.index(flag) + 1] if flag in argv else None


class FakeEnviron:
    """A stand-in for `os.environ` implementing exactly one method: `.get()`.

    That is the whole contract `build_env` uses — it names the keys it wants
    and asks for them one at a time. Nothing else is implemented: no
    `__iter__`, no `keys()`, no `copy()`. If anyone rewrites `build_env` to
    copy or filter the parent environment wholesale, this stub stops satisfying
    it and the leak test fails loudly instead of quietly passing secrets on.
    """

    def __init__(self, values: dict[str, str]):
        self._values = dict(values)
        self.asked: list[str] = []

    def get(self, key, default=None):
        self.asked.append(key)
        return self._values.get(key, default)


# --------------------------------------------------------------------------
# build_argv: the flags
# --------------------------------------------------------------------------


def test_dash_f_is_present_exactly_when_only_priority_is_wanted():
    # READ THIS BEFORE "FIXING" THE MODULE. The mapping really is
    # `only_priority=True -> pass -f`, and it survives a double inversion:
    #
    #   1. Upstream's parser declares the flag `action="store_false"` on top of
    #      `set_defaults(only_priority=True)`. So PRESENT on the command line
    #      means `args.only_priority is False` inside the tool. Inversion one.
    #   2. The branch that value gates in `utils.get_priority_passes` is
    #      `elif only_priority:` — the branch that APPENDS the non-priority
    #      passes. So `False` there means "do not add the rest of the
    #      catalogue". Inversion two.
    #
    # Two inversions cancel: passing -f restricts the run to the priority
    # file, exactly as the flag's own help text claims. A reader who "corrects"
    # this to `if not cfg.only_priority` breaks the station in one of two ways,
    # and neither raises anything:
    #   * Priority-only night, -f now dropped: the tool schedules the station's
    #     entire receivable catalogue and the calendar fills with passes nobody
    #     asked for, displacing the mission satellite.
    #   * Catalogue-wide night, -f now sent: the run is clamped to the priority
    #     file and books nothing outside it.
    # The assertions below are just the fence. This comment is the point.
    restricted = build_argv(cfg(only_priority=True))
    assert "-f" in restricted, (
        "'only priority' was requested but -f was left off the command line. "
        "Without -f satnogs-auto-scheduler adds every non-priority pass it can "
        "hear, so the station books its whole receivable catalogue and the "
        "mission satellite gets displaced from its own calendar"
    )

    catalogue_wide = build_argv(cfg(only_priority=False))
    assert "-f" not in catalogue_wide, (
        "a catalogue-wide run was requested but -f was sent anyway. -f clamps "
        "satnogs-auto-scheduler to the priority file, so the station would book "
        "only the pinned satellites and quietly skip everything else it was "
        "asked to record"
    )


def test_dash_n_is_present_exactly_when_the_run_is_a_dry_run():
    dry = build_argv(cfg(dry_run=True))
    assert "-n" in dry, (
        "a dry run was requested but -n was left off, so this invocation would "
        "really book observations on SatNOGS for station "
        f"{STATION_ID} while the operator believed they were previewing"
    )

    live = build_argv(cfg(dry_run=False))
    assert "-n" not in live, (
        "a real booking run was requested but -n was sent, so the tool would "
        "print a plan and book nothing — the operator sees a full schedule and "
        "the station records none of it"
    )


def test_dash_capital_p_is_present_exactly_when_a_priorities_file_is_configured():
    path = "/data/satnogs/priorities.txt"
    with_file = build_argv(cfg(priorities_path=path))
    assert flag_value(with_file, "-P") == path, (
        f"a priorities file at {path} is configured but -P does not point at "
        f"it, so satnogs-auto-scheduler falls back to its own default location "
        f"and the station's pinned satellites are silently ignored"
    )

    without_file = build_argv(cfg(priorities_path=""))
    assert "-P" not in without_file, (
        "no priorities file is configured but -P was passed anyway; the tool "
        "would be pointed at an empty or missing path and the run would fail "
        "or plan nothing"
    )


def test_the_station_id_is_always_passed():
    argv = build_argv(cfg())
    assert flag_value(argv, "-s") == str(STATION_ID), (
        "the station id is missing from the command line. Without -s the tool "
        "prints its usage text and exits 0, which looks exactly like a clean "
        "run that happened to find no passes"
    )


# --------------------------------------------------------------------------
# build_argv: no secrets on the command line
# --------------------------------------------------------------------------


def test_no_token_ever_appears_anywhere_on_the_command_line():
    """Every shape of run, and the whole argv including the -c bootstrap."""
    shapes = {
        "dry run, priority only": cfg(dry_run=True, only_priority=True),
        "real booking run, catalogue wide": cfg(dry_run=False, only_priority=False),
        "with a priorities file": cfg(priorities_path="/data/priorities.txt"),
        "console-script launcher": cfg(launcher="script"),
        "with base URLs configured": cfg(
            network_base_url="https://network.satnogs.org/api",
            db_base_url="https://db.satnogs.org/api",
        ),
    }
    for label, config in shapes.items():
        joined = " ".join(build_argv(config))
        assert NETWORK_TOKEN not in joined, (
            f"the SatNOGS Network token is on the command line for the "
            f"'{label}' case. Anyone who can run ps on this host, or read the "
            f"container logs or the systemd unit, can now book and cancel "
            f"observations as this station's owner. Tokens go in the "
            f"environment, never in argv"
        )
        assert DB_TOKEN not in joined, (
            f"the SatNOGS DB token is on the command line for the '{label}' "
            f"case. ps output and container logs are not secret; rotate the "
            f"token and move it back into the environment"
        )


# --------------------------------------------------------------------------
# build_argv: -t, and the time zone trap
# --------------------------------------------------------------------------


def test_the_start_time_is_exactly_now_plus_the_configured_lead():
    argv = build_argv(cfg(now=PINNED_UTC, start_lead_minutes=10))
    assert flag_value(argv, "-t") == "2026-03-01T12:10:00", (
        f"-t is {flag_value(argv, '-t')!r} but the pinned clock says "
        f"2026-03-01T12:00:00Z plus a 10 minute lead. If -t drifts the station "
        f"either plans passes it can no longer book in time, or skips the first "
        f"pass of the window entirely"
    )

    longer_lead = build_argv(cfg(now=PINNED_UTC, start_lead_minutes=45))
    assert flag_value(longer_lead, "-t") == "2026-03-01T12:45:00", (
        "the configured start lead is not being added to -t, so the planning "
        "window ignores the operator's setting and can start before the "
        "rotator has time to slew"
    )


def test_the_start_time_is_a_utc_instant_in_the_format_the_tool_reparses():
    computed = start_time(cfg(now=PINNED_UTC, start_lead_minutes=10))
    assert computed.utcoffset() == timedelta(0), (
        f"start_time() returned an offset of {computed.utcoffset()} rather than "
        f"UTC. The tool re-reads -t with .replace(tzinfo=utc), so any other "
        f"offset shifts the entire planning window by that amount without a "
        f"word in the log"
    )

    emitted = flag_value(build_argv(cfg(now=PINNED_UTC, start_lead_minutes=10)), "-t")
    reparsed = datetime.strptime(emitted, START_TIME_FORMAT).replace(
        tzinfo=timezone.utc
    )
    assert reparsed == PINNED_UTC + timedelta(minutes=10), (
        f"reading -t back the way satnogs-auto-scheduler does gives "
        f"{reparsed.isoformat()}, not the intended "
        f"{(PINNED_UTC + timedelta(minutes=10)).isoformat()}. The tool asserts "
        f"UTC on this string rather than parsing an offset from it"
    )


def test_a_now_in_a_non_utc_zone_is_emitted_as_the_utc_instant():
    """The trap: the tool asserts UTC on -t, it does not convert to it."""
    # 17:30 in +05:30 is 12:00Z — same instant as PINNED_UTC.
    kolkata_now = datetime(2026, 3, 1, 17, 30, 0, tzinfo=KOLKATA)
    emitted = flag_value(build_argv(cfg(now=kolkata_now, start_lead_minutes=10)), "-t")
    assert emitted == "2026-03-01T12:10:00", (
        f"-t is {emitted!r} for a clock reading 17:30+05:30. That wall-clock "
        f"time is not a UTC instant, and the tool stamps UTC onto whatever "
        f"string it is given, so the station would plan a window 5h30m away "
        f"from the sky it is actually looking at"
    )

    # Same instant again, from the other side of the date line, so the correct
    # answer requires a real conversion and not a lucky coincidence.
    honolulu_now = datetime(2026, 3, 1, 2, 0, 0, tzinfo=HONOLULU)
    rolled = flag_value(build_argv(cfg(now=honolulu_now, start_lead_minutes=10)), "-t")
    assert rolled == "2026-03-01T12:10:00", (
        f"-t is {rolled!r} for a clock reading 02:00-10:00 on 1 March, which is "
        f"12:00Z the same day. A local-time string here moves the planning "
        f"window onto the wrong calendar day and the whole night is lost"
    )


# --------------------------------------------------------------------------
# build_env
# --------------------------------------------------------------------------


def test_both_tokens_reach_the_child_under_the_right_names_and_are_not_swapped():
    env = build_env(cfg(), base={})

    assert env["SATNOGS_API_TOKEN"] == NETWORK_TOKEN, (
        "SATNOGS_API_TOKEN is the SatNOGS NETWORK token, and it does not hold "
        "the network token here. The name does not say which service it is "
        "for, which is exactly how these two get swapped; a swap makes the "
        "tool start, validate both tokens happily, and then fail deep inside a "
        "download with a message about neither"
    )
    assert env["SATNOGS_DB_API_TOKEN"] == DB_TOKEN, (
        "SATNOGS_DB_API_TOKEN is the SatNOGS DB token, and it does not hold "
        "the DB token here. With the two crossed the station cannot read "
        "transmitter data and plans nothing, for a reason the log will not name"
    )
    assert env["SATNOGS_API_TOKEN"] != env["SATNOGS_DB_API_TOKEN"], (
        "both token variables carry the same value, so one of the two services "
        "is being handed a credential it will reject"
    )


def test_the_cache_and_pass_tuning_variables_reach_the_child():
    env = build_env(cfg(cache_dir="/data/satnogs-cache"), base={})

    assert env["CACHE_DIR"] == "/data/satnogs-cache", (
        "CACHE_DIR is not set, so satnogs-auto-scheduler caches to its own "
        "default inside the container. That cache dies with the container and "
        "every single run pays the multi-minute transmitter-statistics refetch "
        "again"
    )
    assert "CACHE_AGE" in env, (
        "CACHE_AGE is missing, so the operator's cache-lifetime setting is "
        "ignored and the tool falls back to its own — either refetching far "
        "more often than configured, or planning from stale transmitter data"
    )
    assert "MIN_PASS_DURATION" in env, (
        "MIN_PASS_DURATION is missing, so the station's minimum useful pass "
        "length is ignored and the calendar fills with passes too short to "
        "decode anything from"
    )


def test_no_dashboard_gs_variable_or_foreign_token_reaches_the_child():
    """Nothing named GS_* is forwarded, and the parent env is never copied."""
    parent = FakeEnviron(
        {
            "GS_SATNOGS_NETWORK_TOKEN": "c" * 40,
            "GS_SATNOGS_DB_TOKEN": "d" * 40,
            "GS_STATION_ID": "9999",
            "GS_DATA_DIR": "/srv/dashboard/data",
            "GS_MIN_ELEVATION_DEG": "5",
            "GS_MOCK": "1",
            "PATH": "/usr/local/bin:/usr/bin",
        }
    )
    env = build_env(cfg(), base=parent)

    leaked = sorted(key for key in env if key.startswith("GS_"))
    assert leaked == [], (
        f"the dashboard's own configuration leaked into the child: {leaked}. "
        f"satnogs-auto-scheduler reads its settings through decouple, so a "
        f"stray GS_* variable can change which station it books or where it "
        f"writes, with nothing in the transcript to explain it"
    )
    for secret in ("c" * 40, "d" * 40):
        assert secret not in env.values(), (
            "a credential from the dashboard's own environment was copied into "
            "the child process. The child is third-party AGPL code we do not "
            "control; it gets the two tokens this run needs and nothing else"
        )
    assert "9999" not in env.values(), (
        "GS_STATION_ID leaked into the child. The station id belongs on the "
        "command line via -s; a second source for it means a run can book "
        "against a station the operator did not choose"
    )

    assert env["PATH"] == "/usr/local/bin:/usr/bin", (
        "PATH did not survive into the child environment, so the tool cannot "
        "find the interpreter's own helpers and the run dies at spawn. (This "
        "also proves the base environment really was consulted, which is what "
        "makes the leak assertions above meaningful)"
    )


def test_the_dashboards_api_suffix_is_stripped_from_both_base_urls():
    env = build_env(
        cfg(
            network_base_url="https://network.satnogs.org/api",
            db_base_url="https://db.satnogs.org/api",
        ),
        base={},
    )

    assert env["NETWORK_BASE_URL"] == "https://network.satnogs.org", (
        f"NETWORK_BASE_URL is {env['NETWORK_BASE_URL']!r}. The dashboard stores "
        f"base URLs with /api because its own clients append bare paths, but "
        f"satnogs-auto-scheduler appends /api/... itself — forwarding ours "
        f"unchanged asks SatNOGS for /api/api/observations/ and every request "
        f"404s"
    )
    assert env["DB_BASE_URL"] == "https://db.satnogs.org", (
        f"DB_BASE_URL is {env['DB_BASE_URL']!r}; the same /api/api/ double "
        f"prefix applies to DB lookups, and a failed transmitter fetch means "
        f"the run plans nothing"
    )


def test_a_trailing_slash_does_not_hide_the_api_suffix():
    env = build_env(
        cfg(
            network_base_url="https://network.satnogs.org/api/",
            db_base_url="https://db.satnogs.org/api/",
        ),
        base={},
    )

    assert env["NETWORK_BASE_URL"] == "https://network.satnogs.org", (
        "a base URL typed with a trailing slash kept its /api suffix. An "
        "operator pasting a URL from a browser address bar would silently get "
        "a station that talks to /api/api/ and books nothing"
    )
    assert env["DB_BASE_URL"] == "https://db.satnogs.org", (
        "the DB base URL typed with a trailing slash kept its /api suffix, so "
        "transmitter lookups go to /api/api/ and fail"
    )


def test_a_base_url_without_an_api_suffix_is_left_exactly_alone():
    env = build_env(
        cfg(
            network_base_url="https://network.example.org",
            db_base_url="https://db.example.org",
        ),
        base={},
    )

    assert env["NETWORK_BASE_URL"] == "https://network.example.org", (
        f"NETWORK_BASE_URL was rewritten to {env['NETWORK_BASE_URL']!r}. A URL "
        f"that does not end in /api is already in the shape the tool wants; "
        f"trimming anything off it points the station at a host that does not "
        f"answer"
    )
    assert env["DB_BASE_URL"] == "https://db.example.org", (
        f"DB_BASE_URL was rewritten to {env['DB_BASE_URL']!r}, pointing DB "
        f"lookups at a host that does not answer"
    )


def test_the_db_base_url_is_emitted_at_all():
    """Omitting it is invisible: the tool just uses the public DB instead."""
    env = build_env(
        cfg(
            network_base_url="https://network.example.org/api",
            db_base_url="https://db.example.org/api",
        ),
        base={},
    )

    assert "DB_BASE_URL" in env, (
        "DB_BASE_URL is not being passed to the child. There is no error when "
        "this happens — satnogs-auto-scheduler simply falls back to the public "
        "db.satnogs.org, so a station configured against a private or mirrored "
        "DB plans from somebody else's transmitter data and nothing says so"
    )


# --------------------------------------------------------------------------
# validate_tokens
# --------------------------------------------------------------------------


def test_two_empty_tokens_are_reported_as_two_separate_problems():
    problems = validate_tokens(db_token="", network_token="")

    assert len(problems) == 2, (
        f"two missing tokens produced {len(problems)} problem(s). The operator "
        f"needs to be told about both at once; reporting one at a time means "
        f"they fix it, re-run, and hit the second failure minutes later"
    )


def test_a_missing_token_message_says_a_dry_run_needs_it_too():
    for label, problems in (
        ("Network", validate_tokens(db_token=DB_TOKEN, network_token="")),
        ("DB", validate_tokens(db_token="", network_token=NETWORK_TOKEN)),
    ):
        assert len(problems) == 1, (
            f"exactly one token is missing but {len(problems)} problems were "
            f"reported, so the operator is being sent to check credentials "
            f"that are already fine"
        )
        assert "dry run" in problems[0].lower(), (
            f"the message about the missing {label} token does not say that a "
            f"DRY RUN needs it too: {problems[0]!r}. satnogs-auto-scheduler "
            f"validates its whole configuration before it looks at --dryrun, so "
            f"without that sentence an operator who only wanted a preview reads "
            f"a bare exit(1) as a bug in this dashboard and files a report"
        )

    named = validate_tokens(db_token="", network_token=NETWORK_TOKEN)[0]
    assert "SATNOGS_DB_API_TOKEN" in named, (
        f"the message does not name the environment variable to set: {named!r}. "
        f"The two token variable names do not say which service they belong "
        f"to, so without the name the operator has a 50/50 guess"
    )


def test_an_uppercase_hex_token_is_rejected():
    """Upstream's regex is lowercase-only, so 40 valid hex digits is not enough."""
    problems = validate_tokens(db_token="A" * 40, network_token=NETWORK_TOKEN)

    assert len(problems) == 1, (
        "a 40-character UPPERCASE hex token was accepted. It is still hex and "
        "still the right length, but satnogs-auto-scheduler matches "
        "^[a-f0-9]{40}$ and will refuse it — so the station spawns the tool, "
        "waits, and gets an opaque exit(1) instead of a sentence it could act on"
    )
    assert "uppercase" in problems[0].lower(), (
        f"the rejection does not tell the operator what is wrong with the "
        f"token: {problems[0]!r}. 'Invalid token' sends them to regenerate a "
        f"credential that is actually fine apart from its case"
    )


def test_a_thirty_nine_character_token_is_rejected():
    problems = validate_tokens(db_token=DB_TOKEN, network_token="a" * 39)

    assert len(problems) == 1, (
        "a 39-character token was accepted. A token one character short is "
        "what a truncated copy-paste looks like, and letting it through trades "
        "an instant, readable error for a child process that exits 1 with no "
        "explanation"
    )
    assert "39" in problems[0], (
        f"the rejection does not say how long the token actually is: "
        f"{problems[0]!r}. The character count is the single fact that makes a "
        f"truncated paste obvious to the person holding it"
    )


def test_two_well_formed_tokens_raise_no_problems():
    problems = validate_tokens(db_token=DB_TOKEN, network_token=NETWORK_TOKEN)

    assert problems == [], (
        f"two valid 40-character lowercase-hex tokens were rejected: "
        f"{problems}. This check runs before every spawn, so a false alarm "
        f"here blocks scheduling entirely and the station records nothing"
    )


# --------------------------------------------------------------------------
# purity
# --------------------------------------------------------------------------


def test_build_argv_is_pure_for_a_pinned_clock():
    """Same config in, same command line out — no hidden clock, no state."""
    config = cfg(now=PINNED_UTC, priorities_path="/data/priorities.txt")

    first = build_argv(config)
    second = build_argv(config)

    assert first == second, (
        f"build_argv returned two different command lines for one pinned "
        f"config:\n  {first}\n  {second}\nThat means something other than the "
        f"RunConfig is feeding into the invocation — most likely the wall "
        f"clock — and a run can no longer be reproduced from the record we "
        f"keep of it, which is the only evidence available after a bad night"
    )
