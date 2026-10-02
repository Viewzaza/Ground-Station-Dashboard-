# Vendored: satnogs-autoscheduler

> **Scope note.** Since the Station Schedule rewrite this package no longer
> plans or books this station's own observations — that is done by the Libre
> Space Foundation's official `satnogs-auto-scheduler`, a pip dependency run as
> a separate process (see the README's *Station Schedule* section). The two are
> different projects with similar names. What is still used from here:
> **Network Campaign** (`campaign.py`, `network_client.py`), **priority
> enrichment** and **the transmitter picker** (`db_client.pick_transmitter`),
> and the priority file reader/writer. `cli.plan()`, `report._selection_payload`
> and `PlanReport` are no longer called by `ScheduleService`.

Copy-vendored rather than a git submodule or pip dependency, because the
source repo (`D:\Claude\satnogs-autoscheduler`, worktree
`.claude/worktrees/autoscheduler-build`) had no pushed git remote at the time
this was vendored — a submodule or `pip install git+https://...` both need
one, and a local editable install would break the Docker build context
(`docker-compose.yml` builds `./backend` as its own context).

- **Source commit**: `212e6b4` on branch `worktree-autoscheduler-build`
  (worktree of `D:\Claude\satnogs-autoscheduler`), 2026-09-18.
- **Not copied**: `__main__.py` (this package is only ever imported, never
  run as `python -m autoscheduler` from inside the dashboard) and the test
  suite.

## Local patches (not upstream yet)

1. **`priorities.py`: added `write_priority_file(path, entries)`.** The
   upstream module can only *read* a priority file
   (`parse_priority_file`) or *regenerate* one from scratch
   (`generate_priority_file`, which reseeds flat 0.5 weights and would
   discard existing weights/UUIDs). The dashboard's editable priority-list
   panel needs to round-trip an edited list back to disk, so this adds a
   plain writer using the same atomic tmp-write + `Path.replace()` pattern
   `cache.py` already uses elsewhere in this package.

2. **`report.py`: extracted `_selection_payload()` out of `write_json()`.**
   Behaviourally identical — `write_json()` calls it and writes the result
   to disk exactly as before. The dashboard's `ScheduleService` needs the
   same JSON-shaped dict in memory (to publish over the API and cache as
   `schedule_last_run.json`) without writing to a throwaway file and
   reading it back.

3. **`network_client.py`: `get_station()` now goes through a new
   `raw_station()`, cached via the same `Cache.get_or_fetch()` every other
   catalogue read in this package already uses** (new `STATION_TTL_S = 3600`
   in `config.py`). Upstream, this was an uncached request on every call.
   The dashboard's transmitter picker calls `get_station()` on demand
   (whenever an operator opens a row's picker) purely to read the station's
   antenna segments, so leaving it uncached would mean one live SatNOGS
   Network request per picker open. This also means `cli.plan()` itself now
   costs one fewer network round trip on a warm cache — a strict
   improvement, not a behavior change (station connection/antenna info
   does not change fast enough for an hour-old cache to matter).

4. **`priorities.py`: `Priority` gained a `mode: str = "auto"` field.** This is
   dashboard-side bookkeeping only — the scheduler itself never reads `mode`,
   only `weight` — added so the dashboard's per-row Auto/Manual weight toggle
   (a row pinned to "Manual" keeps its typed weight across drag-reorders of
   other rows) survives a save/reload.

   **Amended.** `mode` was originally written into the file as an optional 4th
   column (`... [transmitter_uuid|-] [manual]`), on the reasoning that it was
   backward compatible because the *reader* tolerated it. That reasoning was
   wrong about the reader that matters. The official
   `satnogs-auto-scheduler`, which the Station Schedule tab now shells out to,
   parses with `csv.reader(delimiter=" ")` and discards any line that is not
   **exactly three fields** — so every 4-column line, and every `-`
   placeholder line, was dropped in full. Verified against the real tool: a
   file of such lines parses to `{}`. Under `-f` that is a run which books
   nothing and exits 0.

   So `write_priority_file()` is now strict: exactly `{norad} {weight:.3f}
   {uuid}`, single spaces, bare NORAD ids, and no row without a UUID.
   `mode`, and any row with no transmitter pinned, live in a
   `<slug>.meta.json` sidecar owned by `ScheduleService` instead.
   `parse_priority_file()` is deliberately **unchanged** and still accepts the
   4-field and `-` shapes, so every file written before this keeps loading;
   that asymmetry is the backward-compatible path, and
   `ScheduleService._migrate_priority_files()` harvests the old modes into the
   sidecar once and rewrites the file strict, keeping a `.bak`.

5. **`cli.py`/`report.py`: `plan()` now returns a 3-tuple
   `(station, selection, PlanReport)` instead of `(station, selection)`.**
   `PlanReport` (new dataclass in `report.py`) carries everything `plan()`
   already computed and printed via `console.print()` but previously
   discarded — priority-file validation `findings`, per-satellite "was not
   considered" `skipped` reasons, the thin-history warning, and pass/gate
   counts. Every existing `console.print()` call is untouched, so CLI output
   is identical; `command_plan`/`command_schedule` just unpack and ignore the
   third element (`_report`). The dashboard's `ScheduleService` uses it to
   report whether the last run was a clean success or had problems worth
   surfacing, instead of a run that quietly excluded satellites looking
   identical to a totally clean one.

6. **`network_client.py`: added `all_stations()`**, cached like every other
   catalogue read in this package (new `STATIONS_ALL_TTL_S` in `config.py`).
   Upstream has no "list every station" call because nothing in the CLI ever
   needed one - the dashboard's network campaign scheduler needs the whole
   catalogue to find candidate stations for the mission satellite, since
   there is no server-side "stations that can hear this transmitter" filter.

7. **New file `campaign.py`, not from upstream at all.** The satnogs-network
   web app's own multi-station "Schedule Observations" form does this same
   job (given one satellite, find bookable windows across many stations)
   but its calculation runs behind an authenticated Django session, not the
   public token-authed API this package talks to. `campaign.py` reimplements
   the equivalent computation using the Skyfield/`Predictor`/`Calendar`
   pieces already in this package, then submits through the same
   `NetworkClient.schedule()` this package already had. See its module
   docstring for the deliberate simplification (no pass-splitting) this
   entails.

8. **`network_client.py`: `Station.schedulable` also requires
   `status == "Online"`.** Upstream checked only `is_connected` and that the
   station has coordinates. That is right for the single-station CLI, which
   only ever asks about the operator's *own* station, but wrong the moment
   you book on someone else's: a `Testing` station accepts scheduling from
   its owner alone and rejects everyone else with `HTTP 400 "No permission
   to schedule observations on station: N"`, and an `Offline` one will never
   record what it accepts. Found by hitting exactly that error against the
   real API across 24 such stations. Note two pre-existing `cli.py` messages
   still phrase a false `schedulable` as "not connected", which is now only
   one of the reasons it can be false.

9. **`network_client.py`: `ScheduleResult` carries `accepted_items`.**
   Upstream returns counts only, which cannot answer "which of my bookings
   landed?" — and on a partial batch rejection the accepted set is not
   recoverable from `errors` without parsing its prose back apart. The
   dashboard needs the per-item answer to cross-check a run against the
   stations' real calendars afterwards.

10. **`priorities.py`: added `render_priority_file(entries)`.** The body of
    `write_priority_file()`, split out so the dashboard's
    `GET /api/schedule/priorities/export` endpoint can hand the operator the
    exact bytes the scheduler reads without going through a file. One
    definition of the format rather than two that can drift.

11. **`network_client.py`: new `RateLimitedSession` and `RateLimitedError`,
    wrapped around the session `NetworkClient` builds.** Upstream sends reads
    at whatever rate the caller asks for, and `http.py` classifies a 429 as an
    ordinary 4xx — raised at once, `Retry-After` never read. That is survivable
    for the single-station CLI, which makes a handful of reads per run, and not
    survivable for the campaign, which reads one calendar per station across
    the whole catalogue. The Network API publishes its budgets
    (`DEFAULT_THROTTLE_RATES` in satnogs-network's `network/settings.py`, wired
    to views in `network/api/throttling.py`): **60 observation-list reads an
    hour anonymously, 240 with a token, 256 station-list reads an hour**, on
    the `list` action only, with POST and PUT explicitly exempt. The wrapper
    counts against those budgets over the same sliding hour the server uses,
    obeys a 429's `Retry-After` (DRF sends whole seconds) with a small bounded
    number of retries, and refuses locally — `RateLimitedError`, no request
    sent — once a budget is spent, rather than earning the 429 to find out.

    It wraps the *session* rather than `http.request()` on purpose:
    `http.paginate()` fetches later pages by calling `session.request()`
    itself, so a gate installed any higher would pace the first page of a
    crawl and none of the rest. This leaves `http.py` untouched.

12. **`campaign.py`: a rate limit stops the run; every other read failure
    still does not.** `build_campaign` treats a station whose calendar it
    cannot read as having an empty one — a fair trade for a single flaky
    station, and the wrong one for a throttle, because the budget is spent for
    every station still to come. Unchanged, the run would read the entire rest
    of the catalogue as free and book on top of other people's observations.
    It now catches `RateLimitedError` separately, records why it stopped in
    `skipped`, and returns the partial preview. The pre-existing broad
    `except Exception` fallback is deliberately left as it was.

13. **`campaign.py`: recordings are trimmed to the server's real booking
    edge.** The edge is on `end`, not on `start`, and it is 2900 minutes, not
    2890: `check_end_datetime()` in satnogs-network's `network/base/validators.py`
    refuses anything ending more than `OBSERVATION_DATE_MIN_START +
    OBSERVATION_DATE_MAX_RANGE` (10 + 2890) minutes from now. A pass rising
    just inside this module's own 2880-minute horizon may set up to
    `WINDOW_OVERRUN` (30 minutes) later — at 2910, past the edge — so those
    bookings were guaranteed rejections. New `WINDOW_HARD_END_MIN = 2899`
    trims the window instead of dropping the pass, and the existing duration
    gate then judges what is left. The comments claiming 2890 was the enforced
    edge, and that the server's duration floor is 180 seconds (upstream
    `OBSERVATION_DURATION_MIN` defaults to 120), were corrected in place; the
    conservative 180-second value itself is unchanged.

14. **`http.py`: a write is retried only when it provably never left.**
    `request()` used to retry every method alike - on any transport error and
    any 5xx, three times - which is right for a read and wrong for the booking
    POST: it cannot tell "never arrived" from "the reply was lost". A read
    timeout after SatNOGS had created the observations, or a 504 from a gateway
    in front of an application that had, re-sent the batch, and `schedule()`
    then re-posted each item alone. A fake server that persists and then drops
    the reply turned 3 bookings into 18 rows with `accepted 0`. Now a non-GET/
    HEAD/OPTIONS request is retried only on a connect timeout, a refused
    connection or an unresolvable name (urllib3 raises `NewConnectionError`
    only while connecting); everything after the request is on the wire raises
    the new `SatnogsOutcomeUnknown`, a `SatnogsHTTPError` subclass. Writes do
    not follow redirects, and a 3xx on a write is an unknown outcome. In
    `network_client.py`, `schedule()` catches it first, never falls back to
    item-by-item after an unknown outcome (that fallback is for a batch the
    server REJECTED), records those items in the new
    `ScheduleResult.uncertain_items`, stops the per-item loop once the server
    is unreachable, and names the station in every per-item error. Pinned by
    `tests/test_booking_writes.py` and `tests/test_transport_safety.py`.

15. **`campaign.py`: gather, then select - and read calendars lazily.**
    `build_campaign` walked `all_stations()` in the API's order, strictly
    ascending by id, and stopped when the budget filled, so every run booked
    the network's oldest corner (177 of 301 stations never examined on a live
    run). Phase 1 is now geometry only - Skyfield, no network - over every
    station. Phase 2, `_select_spread`, reads a station's calendar only when it
    is about to be picked, in an order shuffled per run from the run's
    timestamp in seconds; gives every station its r-th booking before any gets
    its (r+1)-th; balances elevation bands only among passes within
    `MAX_ELEVATION_SACRIFICE_DEG` of the station's own best; and sorts the
    result by station then time for review. A rate limit stops READING but
    keeps selecting among stations already read, and the preview carries
    `stopped_early` and `calendars_read` so a truncated plan is never mistaken
    for a small network. Reads fall to roughly the number of stations booked
    (150 of a 240 budget where it used to spend all 240). Pinned by
    `tests/test_campaign_selection.py`; fifteen deliberate mutations, all
    caught.

16. **`network_client.py`: one read budget per credential, and a long
    Retry-After is a stop.** `RateLimitedSession` (patch 11) kept its count per
    instance, and `CampaignService` builds a client per operation, so every
    preview, commit and verify started from zero while the server did not.
    The count now lives in a process-wide gate keyed by base URL and a hash of
    the token, shared by every client on that credential and locked. A
    `Retry-After` longer than `MAX_RETRY_AFTER_WAIT_S` is no longer capped and
    re-sent twice; it raises at once and records the deadline in the gate, so
    nothing in the process asks again before then.

17. **`campaign.py`: more stations per run - a fallback transmitter, a
    calendar cache, a cap that counts what is already booked, and a sacrifice
    bound for the first pick only.** Four changes to `build_campaign`, all
    behind new optional keywords, so existing callers get today's behaviour
    except where noted.
    - `fallback_transmitter_uuids`: each station records the *first* of
      `[transmitter_uuid, *fallbacks]` it can hear. Pinning KNACKSAT-2's
      400.630 MHz telemetry alone reached 145 stations; falling back to the
      145.825 MHz digipeater where telemetry is out of antenna range reaches
      222 (measured offline on the cached catalogue). Telemetry still wins on
      the 68 stations that hear both, where `pick_transmitter`'s tie-break
      (DB order) would have chosen the digipeater. `pick_transmitter` is no
      longer called with a uuid the station lacks, which logged a false
      "priority file pins transmitter" warning per fallback station.
      `CampaignItem` gains `transmitter_description` and `is_fallback`.
    - `calendar_cache` (station id -> bookings): consulted before
      `future_bookings()`, filled by every successful read, never by a failed
      one. Freshness is the caller's job. A looped commit's later rounds are
      what used to spend the read budget, re-reading every calendar each
      round; with the cache they send no reads. Hits count in the new
      `calendars_cached`, not in `calendars_read`.
    - `cap_counts_existing` (default **on**, a behaviour change): observations
      of the mission satellite already on a station's calendar inside the
      booking window count against `max_per_station`, so a second click or the
      auto timer tops a station up instead of stacking another full cap on it.
      Other satellites' observations and our own `recent_attempts` stay
      conflicts only.
    - `MAX_ELEVATION_SACRIFICE_DEG` now binds a station's first pick in a
      build only, so later picks can fill the thin low bands. Offline, at 600
      bookings with the fallback and 3 per station, the bands went from
      166/144/106/87/72/25 to 101/100/103/100/100/96 (90-75 down to 15-0).
    The preview payload also publishes `stations_reachable`,
    `stations_booked`, `calendars_cached`, `band_counts`, `transmitters` and
    the `params` the build actually used. Pinned by
    `tests/test_campaign_selection.py` and `tests/test_campaign_network.py`;
    24 deliberate mutations, all caught.

18. **`network_client.py`: calendars from `/api/jobs/`, and a refused batch
    costs one POST per culprit station, not one per item.** Five changes, all
    in service of the full-network campaign (~222 calendars, ~600 bookings):
    - `future_bookings(station_id, now=None, source="auto")` reads the
      station's calendar from `GET /api/jobs/?ground_station=N` first, the
      read the official auto-scheduler itself uses. JobView has no
      `throttle_classes` and serves the whole `start__gte=now()` queryset as
      one plain JSON list (1147 live answers for 5024, none paginated, no
      429). The `/api/observations/` walk it replaces costs ~1.2-1.33 pages a
      station against a 240/hour token budget, so a 222-station preview
      needed more than an hour's budget, and every looped commit round
      re-spent it. On any failure of the jobs read (HTTP error, 429, our own
      ceiling, unparseable JSON or rows) `"auto"` falls back to the old walk,
      unchanged; `"jobs"` and `"observations"` force one source. Rows are
      filtered (start >= now, and `ground_station` when present), not walked
      to a stop. A new `calendar_sources` Counter says which source answered
      each call, and the campaign payloads publish it.
    - The jobs read goes through a separate, **anonymous** session
      (`jobs_session`: no token, ever). With the owner's token, JobView treats
      `/api/jobs/?ground_station=<own station>` as that station's client
      polling for work and stamps `last_seen=now` - reading 5024's calendar
      with our token would mark it alive whether it is or not. Its gate key
      carries no token either, so the whole process shares one budget.
    - `RateLimitedSession` has a new `"jobs"` scope (checked before
      `"/observations"`, so a jobs read is never charged to that budget) with
      `JOBS_LIST_PER_HOUR = 1200` - a courtesy ceiling of our own, since the
      server publishes none - and `JOBS_TIMEOUT_S = 30` instead of the 90 s
      sized for `?norad_cat_id=` queries.
    - `JOBS_BYPASS_AFTER_FAILURES = 3`: after three jobs failures in a row a
      client reads the rest of its calendars from `/observations/` directly.
      Each failed jobs read costs its retries and backoff before the fallback
      starts (~96 s when the endpoint times out), which over ~222 stations
      would add hours to a preview only to end on the same fallback. A success
      resets the count; the next client (one per preview, commit and verify)
      tries `/jobs/` again.
    - `schedule()`: a batch refused with HTTP 409 "One or more observations of
      station N overlap ..." sends only station N's items one at a time and
      resubmits the rest as a batch, repeating while 409s keep naming stations
      (bounded at distinct stations + 1 batch attempts). A batch refused with
      HTTP 400 "No permission to schedule observations on station(s): ..."
      records the listed stations' items as refused WITHOUT resending them -
      the server lists every such station in the batch, and the verdict
      depends only on the account and the station - and resubmits the rest
      the same way. Both are safe because the server validates every item
      before it saves any, so a refused batch created nothing. The old rule
      sent every item on its own after any refusal: one stale slot in 600
      items meant ~600 sequential POSTs, and on 2026-10-02, with our own
      station offline (an account that owns no Online station may not book
      anyone else's), a refused batch of 100 cost 101 POSTs where it now costs
      one. Any other refusal, or a permission list that cannot be read in full,
      keeps the old one-at-a-time fallback. `accepted_items` and
      `uncertain_items` are re-sorted into input order and stay the very dicts
      passed in, so callers can still map them back by identity.
    Pinned by `tests/test_jobs_calendar.py`, `tests/test_booking_writes.py`
    and `tests/test_transport_safety.py`.

## TODO

- Push `satnogs-autoscheduler` to a real GitHub remote and replace this
  vendored copy with a normal dependency (submodule or pinned pip package).
- Upstream patches 1-6, 8-11, 14, 16 and 18 above to that repo. 7, 12, 13, 15
  and 17 are `campaign.py`, which upstream does not have (see 7), so they are
  not upstream material. 8 in particular is a plain bug for any consumer that
  books on a station it does not own; 11 and 16 matter to any consumer that
  reads more than a few dozen times an hour; 14 matters to any consumer that
  books at all. 18's anonymous `/api/jobs/` read matters to any consumer that
  reads a calendar of a station its token owns (the heartbeat side effect),
  and its 409/permission pruning to any consumer that books in batches.
- `cache.py`'s module docstring still says the SatNOGS APIs are "public and
  slow rather than rate-limited". That was never quite true and is now
  actively misleading — see patch 11 for the published rates. Left alone here
  only because nothing else in that file is being touched.
- `satnogs-autoscheduler` currently has no LICENSE file. This dashboard is
  MIT. Confirm licensing intent before this vendored copy is redistributed
  beyond this repo (same author/org, so likely fine, but not yet explicit).
