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

## TODO

- Push `satnogs-autoscheduler` to a real GitHub remote and replace this
  vendored copy with a normal dependency (submodule or pinned pip package).
- Upstream patches 1-6, 8 and 9 above to that repo (7 is dashboard-specific,
  not upstream material). 8 in particular is a plain bug for any consumer
  that books on a station it does not own.
- `satnogs-autoscheduler` currently has no LICENSE file. This dashboard is
  MIT. Confirm licensing intent before this vendored copy is redistributed
  beyond this repo (same author/org, so likely fine, but not yet explicit).
