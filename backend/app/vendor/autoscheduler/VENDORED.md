# Vendored: satnogs-autoscheduler

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

4. **`priorities.py`: `Priority` gained a `mode: str = "auto"` field**, with
   a matching optional 4th column in the file format (`... [transmitter_uuid|-]
   [manual]`). This is dashboard-side bookkeeping only — the scheduler itself
   never reads `mode`, only `weight` — added so the dashboard's per-row
   Auto/Manual weight toggle (a row pinned to "Manual" keeps its typed weight
   across drag-reorders of other rows) survives a save/reload. Fully
   backward compatible: a plain 2-3 field line (everything written before
   this, and everything the official tool writes) still parses as
   `mode="auto"` with no change in behavior, and `write_priority_file()`
   only ever emits the 4th field for a "manual" entry, so an all-auto list's
   file is byte-identical to what it would have been before this patch.

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

## TODO

- Push `satnogs-autoscheduler` to a real GitHub remote and replace this
  vendored copy with a normal dependency (submodule or pinned pip package).
- Upstream both patches above to that repo.
- `satnogs-autoscheduler` currently has no LICENSE file. This dashboard is
  MIT. Confirm licensing intent before this vendored copy is redistributed
  beyond this repo (same author/org, so likely fine, but not yet explicit).
