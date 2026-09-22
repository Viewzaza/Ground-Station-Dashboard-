# `schedule_dry_run.log` — a real transcript from a fake network

## What this is, honestly

`schedule_dry_run.log` is the **unedited** output of the **real, official
`satnogs-auto-scheduler`** binary (version `0.5.dev17+g0f7ec0177`, AGPL-3.0,
installed in `ground-station-dashboard-backend:latest`), run against a
**completely fake SatNOGS API** served from `127.0.0.1`.

So: real tool, real code path, real formatting — **fake data**.

It exists because `app/services/autoscheduler_cli.py` has to parse that tool's
summary table, and a hand-written fixture would only ever encode whatever the
author *believed* the format to be. That is precisely the mistake this file is
here to prevent. Every byte in it was produced by the tool's own
`print_scheduledpass_summary()`.

**What it is not:** it is not a recording of station 5024, or of any real
station. No pass in it was ever observed, no observation in it was ever booked,
and the station record it was generated against is invented. Do not read any
operational meaning into the numbers.

## Nothing touched the real SatNOGS

- The container runs with **`--network=none`**. It gets a network namespace with
  loopback and nothing else, so reaching `network.satnogs.org` or
  `db.satnogs.org` is not merely avoided, it is impossible.
- There are **no real SatNOGS tokens on this machine** and none were used. The
  two tokens are `deadbeef...` and `cafef00d...` — 40 lowercase hex characters
  of nonsense. Upstream's `settings.validate_token` is a purely local regex
  (`^[a-f0-9]{40}$`) with no network call, so nonsense that matches passes. They
  are not credentials and unlock nothing.
- The run passes **`-n` (dry run)**. The stub answers any `POST` with 403, and
  `harness.py` **fails the capture** if a booking is ever attempted. It was not.

## How it was produced

```bash
./capture/run_capture.sh
```

That runs, inside the backend image (which is where the tool is installed):

```
docker run --rm --network=none -v capture/:/w:ro -v <repo>:/repo:ro \
    -v <this dir>:/out ground-station-dashboard-backend:latest python /w/harness.py
```

`harness.py` starts two `http.server` threads on `127.0.0.1:8098` (Network) and
`127.0.0.1:8099` (DB), points `NETWORK_BASE_URL` / `DB_BASE_URL` at them, and
subprocess-runs the scheduler with **stderr merged into stdout** — mandatory,
because the summary table is logged to stderr.

The harness **invents nothing about the invocation**. It imports `RunConfig`,
`build_argv`, `build_env` and `_BOOTSTRAP` from the dashboard's own
`app/services/autoscheduler_cli.py` and uses them unmodified, so the transcript
comes from the exact command line and environment production uses. Change those
functions and re-running changes the transcript — which is the property we want.

The resulting command line was:

```
python -c <bootstrap> -s 5024 -t 2023-02-18T09:00:00 -d 24 -o 30 -m 3 \
       -P /w/priorities.txt -f -n
```

`RunConfig.now` is frozen to `2023-02-18T08:50:00Z`, so the capture is
**byte-reproducible**: re-running `run_capture.sh` reproduces this file with an
identical MD5 (`367600436e9e52287a7c39cd3c556d38`). Verified.

The window is anchored near the epoch of upstream's TLE fixture (2023-02-17) on
purpose. Those TLEs propagate to any date without erroring, but propagated three
years they yield physically nonsense geometry (160 passes a day for one
satellite). Anchoring at the epoch keeps the az/el and duration columns
realistic, which is the entire point of capturing a transcript.

## Which endpoints were stubbed

All seven requests the tool makes, and nothing else — anything unrecognised is
answered 404 and logged, so a missing endpoint shows as a log line rather than a
mystery. `stub_requests.log` is the verbatim record of what was served.

| Base | Path | Serves |
| --- | --- | --- |
| Network | `/api/stations/5024` | the (invented) ground-station record |
| Network | `/api/transmitters/` | transmitter statistics, 8 pages |
| Network | `/api/jobs/?ground_station=5024` | the (invented) existing observations |
| DB | `/api/satellites` | satellite catalogue, 4 pages |
| DB | `/api/transmitters` | active transmitters, 4 pages |
| DB | `/api/tle/` | TLEs, 4 pages (this is the one sent the DB token) |

Pagination uses the RFC 5988 `Link: <...>; rel="next"` header, because that is
what `satnogs_client`'s `while "next" in response.links` loop consumes. Serving
several real pages rather than one exercises that loop.

Two servers rather than one is not fussiness: SatNOGS DB and SatNOGS Network
both expose `/api/transmitters`, with completely different payload shapes.
Upstream tells them apart only by which base URL it used, so the stub must too.

## Where the data came from

**From upstream's own test fixtures, verbatim** — taken unmodified from
`satnogs-auto-scheduler` at `0f7ec0177`, MD5s confirmed against the source
checkout:

> **They are not committed here.** `capture/upstream_fixtures/` is gitignored
> and populated on demand by `capture/fetch_upstream_fixtures.sh`, which
> shallow-fetches that exact commit. That project is AGPL-3.0 and this
> repository is MIT; the entire reason the scheduler runs as a separate
> process is that none of its material lives in this tree, and quietly
> carrying 1.5 MB of its files in as test data would have made the README's
> Licence section untrue. `run_capture.sh` calls the fetch script for you, and
> the capture reproduces byte-for-byte either way (MD5
> `367600436e9e52287a7c39cd3c556d38`).

| File | MD5 |
| --- | --- |
| `satellites.json` | `9e158d13391c8b03afda23e365fd3d88` |
| `transmitters_receivable.json` | `d1902c3df32cfe2247ed9e1091b05c7c` |
| `transmitters_stats.json` | `aff8c47e3b2728438849d07259630f10` |
| `tles.json` | `e55a962dd12f6aa0ef03bd9b231ce808` |

That is where every satellite name, NORAD id, transmitter UUID, mode and TLE in
the transcript comes from. They are real catalogue entries.

**Synthesised for the capture** (all of it confined to the top of
`capture/stub_api.py` so a reader can see the whole of it at once):

- The **ground-station record**. Describes no real station.
- The **existing-observations list** (`/api/jobs/`). Describes no real booking.
- `downlink_low` and `status` on the DB transmitter records — necessary because
  upstream's `transmitters_receivable.json` is the *output* of
  `get_active_transmitter_info()` and no longer carries the two fields that
  function filters on.
- `capture/priorities.txt`. **Not** station 5024's real priority list.

## What the transcript deliberately exercises

51 table rows: **47 planned (`Sch` = `N`) and 4 already-scheduled (`Sch` = `Y`)**.

The four `Y` rows exist because that row shape is the one most likely to break a
parser — upstream prints it from a different branch with **zeroed az/el
(`  0  0   0`) and an empty Mode column**. They were chosen to produce every
shape that branch can:

- **60133** — not in the satellite catalogue, so an **empty Satellite name**.
  The line ends `| N      | ` with a trailing space, giving exactly 12
  `" | "`-separated fields, the parser's minimum.
- **48274** — `CSS (Tianhe)`, a name with a space and parentheses.
- **43768** — `is_frequency_violator`, so the `Freq` column reads `Y`.
- **25544** — ends on a `:30` second, so the duration is not a whole minute.

The priority list also covers a 4-digit NORAD printed zero-padded as `07530`, a
SatNOGS temporary id in the `99xxx` range, names containing `&`, `/`, `.` and
parentheses, and one entry (`43017`) deliberately left unmatched so that
`missing_priority_notices()` has something to find.

## Verifying the parser against it

```bash
python3 backend/tests/data/capture/verify_parser.py
```

Runs the dashboard's own `parse_output()` over the transcript and checks every
field of every row against the raw text. Host `python3` is enough —
`autoscheduler_cli` imports nothing third-party.

It reports one real discrepancy, which is a property of the tool rather than a
bug in the capture: **`ScheduleRow.duration_s` is derived from `end - start`,
but both endpoints are printed truncated to whole seconds**, so on 31 of the 51
rows the derived duration is **1 second longer** than the `Duration` column the
tool printed. Across the 47 planned passes that is +31 s against the tool's own
`27983 s` efficiency figure. See the findings that accompany this capture.

## Files

| Path | What |
| --- | --- |
| `schedule_dry_run.log` | the transcript, verbatim and unedited |
| `stub_requests.log` | every request the stub served, in order |
| `capture/run_capture.sh` | one-command re-capture |
| `capture/harness.py` | starts the stubs, runs the tool via the repo's own `build_argv` |
| `capture/stub_api.py` | the fake SatNOGS DB + Network |
| `capture/priorities.txt` | the `-P` file used for the capture |
| `capture/fetch_upstream_fixtures.sh` | fetches upstream's fixtures (gitignored, not committed) |
| `capture/upstream_fixtures/` | where that script puts them |
| `capture/verify_parser.py` | field-by-field parser check |
| `capture/probe_select.py` | helper used to pick priority entries that yield passes |
