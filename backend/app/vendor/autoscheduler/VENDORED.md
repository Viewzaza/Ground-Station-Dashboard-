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

## TODO

- Push `satnogs-autoscheduler` to a real GitHub remote and replace this
  vendored copy with a normal dependency (submodule or pinned pip package).
- Upstream both patches above to that repo.
- `satnogs-autoscheduler` currently has no LICENSE file. This dashboard is
  MIT. Confirm licensing intent before this vendored copy is redistributed
  beyond this repo (same author/org, so likely fine, but not yet explicit).
